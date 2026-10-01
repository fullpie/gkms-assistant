"""Bounded exact Plan2 catalog adapter for ``ExamStaminaRecoverMultiple``.

The leaf owns the four current-Master versions of
``p_card-02-ido-3_081``.  It deliberately stops at the target effect:
``ExamStaminaRecoverMultiple`` is compiled, executed, and handed off as an
immutable typed row; the StartTurn trigger and the three other PlayEffect
rows stay visible as per-version companion blockers.

The target executor is pinned to the Android v3.2.3 native path and the PC
metadata cross-check.  It snapshots the execution-time signed Int32 stamina
and MaxStamina values, computes the MaxStamina permille request with ordered
binary32 arithmetic and ceiling, then delegates the fixed request to the
reviewed ``plan2_stamina_recover_fix`` primitive.  No central catalog,
horizon, core runtime, formal coverage, Plan3, GUI, controller, or proxy is
called by this module.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
import json
import math
from pathlib import Path
import sqlite3
from typing import Final

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    INT32_MAX,
    INT32_MIN,
    NativeFormulaDomainError,
    ceil_f32_to_i32,
    f32,
)
from .plan2_stamina_recover_fix import (
    StaminaDifference,
    StaminaRecoverFixContract,
    StaminaRecoverFixEvaluation,
    StaminaRecoverFixRuntime,
    evaluate_stamina_recover_fix,
)


PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_AUDIT_PATH: Final = (
    PROJECT_ROOT
    / "var"
    / "coverage"
    / "plan2_native_catalog_stamina_recover_multiple_audit.json"
)

PLAN2_NATIVE_CATALOG_STAMINA_RECOVER_MULTIPLE_SCHEMA_VERSION: Final = 1
STAMINA_RECOVER_MULTIPLE_CATALOG_SCHEMA_VERSION: Final = (
    PLAN2_NATIVE_CATALOG_STAMINA_RECOVER_MULTIPLE_SCHEMA_VERSION
)
STAMINA_RECOVER_MULTIPLE_ADAPTER_ID: Final = (
    "plan2.native.catalog.stamina_recover_multiple"
)

PLAN2: Final = "ProducePlanType_Plan2"
MENTAL_SKILL: Final = "ProduceCardCategory_MentalSkill"
MOVE_LOST: Final = "ProduceCardMovePositionType_Lost"
MOVE_UNKNOWN: Final = "ProduceCardMovePositionType_Unknown"
MOVE_TRIGGER_UNKNOWN: Final = "ProduceCardMoveEffectTriggerType_Unknown"
COST_UNKNOWN: Final = "ExamCostType_Unknown"
MULTIPLE_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamStaminaRecoverMultiple"
)
REVIEW_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamReview"
BLOCK_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamBlock"
PLAYABLE_VALUE_ADD_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamPlayableValueAdd"
)
PHASE_EXAM_START_TURN: Final = "ProduceExamPhaseType_ExamStartTurn"
FIELD_REVIEW_UP: Final = "ProduceExamFieldStatusType_ReviewUp"
TARGET_TRIGGER_ID: Final = "e_trigger-exam_start_turn-review_up-1"
TARGET_REVIEW_THRESHOLD: Final = 1

TARGET_CARD_ID: Final = "p_card-02-ido-3_081"
TARGET_UPGRADES: Final = (0, 1, 2, 3)
TARGET_VERSION_COUNT: Final = len(TARGET_UPGRADES)
TARGET_CARD_STAMINA_BY_UPGRADE: Final = {0: 6, 1: 6, 2: 5, 3: 5}
TARGET_MULTIPLE_EFFECT_IDS_BY_UPGRADE: Final = {
    0: "e_effect-exam_stamina_recover_multiple-0100",
    1: "e_effect-exam_stamina_recover_multiple-0100",
    2: "e_effect-exam_stamina_recover_multiple-0100",
    3: "e_effect-exam_stamina_recover_multiple-0150",
}
TARGET_MULTIPLE_PERMILLE_BY_UPGRADE: Final = {
    0: 100,
    1: 100,
    2: 100,
    3: 150,
}
TARGET_EFFECT_IDS_BY_UPGRADE: Final = {
    0: (
        "e_effect-exam_stamina_recover_multiple-0100",
        "e_effect-exam_review-0003",
        "e_effect-exam_block-0005",
        "e_effect-exam_playable_value_add-01",
    ),
    1: (
        "e_effect-exam_stamina_recover_multiple-0100",
        "e_effect-exam_review-0005",
        "e_effect-exam_block-0010",
        "e_effect-exam_playable_value_add-01",
    ),
    2: (
        "e_effect-exam_stamina_recover_multiple-0100",
        "e_effect-exam_review-0005",
        "e_effect-exam_block-0010",
        "e_effect-exam_playable_value_add-01",
    ),
    3: (
        "e_effect-exam_stamina_recover_multiple-0150",
        "e_effect-exam_review-0005",
        "e_effect-exam_block-0014",
        "e_effect-exam_playable_value_add-01",
    ),
}

PLAY_ORIGINS: Final = ("normal", "forced", "extra")
SUPPORTED_PLAY_ORIGINS: Final = PLAY_ORIGINS
TARGET_EFFECT_GROUP_IDS: Final = (
    "effect_group-visible-stamina_recover_fix-000",
)
TARGET_CARD_EFFECT_GROUP_IDS: Final = (
    "effect_group-visible-exam_block-000",
    "effect_group-visible-exam_playable_value_add-000",
    "effect_group-visible-exam_review-000",
    "effect_group-visible-stamina_recover_fix-000",
)

ANDROID_V323_NATIVE_SOURCE: Final = (
    "_research/android/game-v3.2.3/native-analysis/lib/arm64-v8a/"
    "libil2cpp.so"
)
ANDROID_V323_TARGET_METADATA_SOURCE: Final = (
    "_research/android/game-v3.2.3/native-analysis/target-metadata.json"
)
ANDROID_V323_EXECUTOR_MAPPING_SOURCE: Final = (
    "_research/android/game-v3.2.3/native-analysis/create-executor-mapping.json"
)
ANDROID_V323_PARAMETER_MODEL_SOURCE: Final = (
    "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/"
    "Assembly-CSharp/Campus/InGame/Exam/ExamParameterModel.txt"
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

NATIVE_EVIDENCE: Final[dict[str, object]] = {
    "effect_type": MULTIPLE_EFFECT_TYPE,
    "android": {
        "version": "Android v3.2.3",
        "executor": "Campus.InGame.Exam.StaminaRecoverMultipleEffectExecutor",
        "constructor_va": "0x7E8D78C",
        "execute_va": "0x7E8D844",
        "calculate_stamina_recover_va": "0x7E5EC2C",
        "add_stamina_fix_va": "0x7E5EDE0",
        "current_stamina_getter": {
            "method": "ExamParameterModel.get_Stamina",
            "metadata_va": "0x7EB7ABC",
            "value": "signed execution-time current Stamina Int32",
        },
        "max_stamina_getter": {
            "method": "ExamParameterModel.get_MaxStamina",
            "metadata_va": "0x7EB7BB0",
            "value": "signed execution-time MaxStamina Int32",
        },
        "execution_order": [
            "constructor reads effectValue1 into executor _value",
            "snapshot execution-time MaxStamina and current Stamina",
            "compute MaxStamina permille request",
            "CalculateStaminaRecover",
            "AddStaminaFix",
            "create/set/append Stamina difference",
            "SetStamina",
            "EffectDifferenceExecuted callback",
        ],
    },
    "pc_metadata_cross_check": {
        "executor_type_index": 3658,
        "executor_value_field_index": 19643,
        "constructor_method_index": 18084,
        "execute_effect_method_index": 18085,
        "parameter_model_type_index": 3893,
        "current_stamina_getter_method_index": 19474,
        "max_stamina_getter_method_index": 19476,
        "set_stamina_method_index": 19604,
        "native_body_authoritative": False,
    },
    "formula": {
        "base": "MaxStamina",
        "not_base": ["current Stamina", "missing Stamina"],
        "unit": "permille",
        "expression": "ceil_f32(f32(f32(MaxStamina)*f32(effectValue1))/f32(1000.0f))",
        "rounding": [
            "binary32 MaxStamina conversion",
            "binary32 effectValue1 conversion",
            "binary32 multiply",
            "binary32 divide by 1000.0f",
            "FRINTP/FCVTPS ceiling; no truncation",
            "no -0.0001f epsilon in Multiple; epsilon belongs to recover-add adjustment",
            "non-finite or out-of-range Int32 conversion fails closed in this leaf",
        ],
    },
    "recover_fix": {
        "helper": "src/gkms_tool/plan2_stamina_recover_fix.py",
        "restriction": "active StaminaRecoverRestriction returns 0",
        "add_status": "active StaminaRecoverAdd adjusts with binary32 ratio ceil",
        "first_cap": "min(MaxStamina, int32(current+adjusted))-current",
        "second_cap": "max(0, min(int32(current+calculated), MaxStamina))",
    },
    "shape": {
        "effect_value1": "signed Int32 permille; current rows 100 or 150",
        "effect_value2": 0,
        "effect_count": 0,
        "effect_turn": 0,
        "target_slot_order": 0,
        "master_order": "Multiple, Review, Block, PlayableValueAdd",
    },
    "bounds": {
        "zero_max_zero_current": "supported no-op",
        "effect_value_le_zero": "known value<1 path produces zero and never decreases stamina",
        "negative_max": "fail closed",
        "negative_current": "fail closed",
        "current_over_max": "fail closed",
    },
}


class Plan2NativeStaminaRecoverMultipleError(ValueError):
    """A catalog/runtime value is outside this exact standalone boundary."""


Plan2NativeCatalogStaminaRecoverMultipleContractError = (
    Plan2NativeStaminaRecoverMultipleError
)
Plan2NativeStaminaRecoverMultipleContractError = (
    Plan2NativeStaminaRecoverMultipleError
)
StaminaRecoverMultipleContractError = Plan2NativeStaminaRecoverMultipleError


class PlayOrigin(str, Enum):
    NORMAL = "normal"
    FORCED = "forced"
    EXTRA = "extra"

    # Neighboring Plan2 adapters call the ordinary path this way.
    ORDINARY = "normal"


Plan2StaminaRecoverMultiplePlayOrigin = PlayOrigin


def _i32(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Plan2NativeStaminaRecoverMultipleError(
            f"{label} must be a signed Int32"
        )
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2NativeStaminaRecoverMultipleError(
            f"{label} is outside signed Int32"
        )
    return value


def _nonnegative_i32(value: object, label: str) -> int:
    result = _i32(value, label)
    if result < 0:
        raise Plan2NativeStaminaRecoverMultipleError(
            f"{label} must be non-negative"
        )
    return result


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        qualifier = "text" if empty else "non-empty text"
        raise Plan2NativeStaminaRecoverMultipleError(
            f"{label} must be {qualifier}"
        )
    return value


def _json_value(value: object, label: str) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise Plan2NativeStaminaRecoverMultipleError(
                f"{label} is invalid JSON"
            ) from error
    return value


def _json_object(value: object, label: str) -> Mapping[str, object]:
    parsed = _json_value(value, label)
    if not isinstance(parsed, Mapping):
        raise Plan2NativeStaminaRecoverMultipleError(
            f"{label} must be a JSON object"
        )
    return parsed


def _json_array(value: object, label: str) -> tuple[object, ...]:
    parsed = _json_value(value, label)
    if not isinstance(parsed, list):
        raise Plan2NativeStaminaRecoverMultipleError(
            f"{label} must be a JSON array"
        )
    return tuple(parsed)


def _strict_equal(actual: object, expected: object, label: str) -> None:
    if type(actual) is not type(expected):
        raise Plan2NativeStaminaRecoverMultipleError(
            f"{label} changed; expected {expected!r}, got {actual!r}"
        )
    if isinstance(expected, Mapping):
        if set(actual) != set(expected):  # type: ignore[arg-type]
            raise Plan2NativeStaminaRecoverMultipleError(
                f"{label} keys changed"
            )
        for key, value in expected.items():
            _strict_equal(actual[key], value, f"{label}.{key}")  # type: ignore[index]
    elif isinstance(expected, (list, tuple)):
        if len(actual) != len(expected):  # type: ignore[arg-type]
            raise Plan2NativeStaminaRecoverMultipleError(
                f"{label} length changed"
            )
        for index, (got, wanted) in enumerate(
            zip(actual, expected, strict=True)  # type: ignore[arg-type]
        ):
            _strict_equal(got, wanted, f"{label}[{index}]")
    elif actual != expected:
        raise Plan2NativeStaminaRecoverMultipleError(
            f"{label} changed; expected {expected!r}, got {actual!r}"
        )


_EFFECT_RAW_CORE_KEYS: Final = {
    "id",
    "effectType",
    "effectValue1",
    "effectValue2",
    "effectCount",
    "effectTurn",
    "effectGroupIds",
}
_EFFECT_RAW_NEUTRAL: Final[dict[str, object]] = {
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
    "produceExamStatusEnchantId": "",
    "produceCardStatusEnchantId": "",
    "produceCardGrowEffectIds": [],
}
_EFFECT_RAW_ALLOWED: Final = (
    _EFFECT_RAW_CORE_KEYS
    | set(_EFFECT_RAW_NEUTRAL)
    | {"produceDescriptions", "customizeProduceDescriptions"}
)


def _validate_effect_raw(
    raw: Mapping[str, object],
    *,
    expected_id: str,
    expected_type: str,
    expected_value1: int,
    expected_value2: int = 0,
    expected_count: int = 0,
    expected_turn: int = 0,
    expected_groups: tuple[str, ...] | None,
) -> None:
    unknown = set(raw) - _EFFECT_RAW_ALLOWED
    if unknown:
        raise Plan2NativeStaminaRecoverMultipleError(
            f"{expected_id}: unsupported raw effect keys {sorted(unknown)!r}"
        )
    required = {
        "id": expected_id,
        "effectType": expected_type,
        "effectValue1": expected_value1,
        "effectValue2": expected_value2,
        "effectCount": expected_count,
        "effectTurn": expected_turn,
    }
    for key, value in required.items():
        if key not in raw or raw[key] != value:
            raise Plan2NativeStaminaRecoverMultipleError(
                f"{expected_id}: raw {key} does not match normalized Master"
            )
    for key, value in _EFFECT_RAW_NEUTRAL.items():
        if key not in raw:
            raise Plan2NativeStaminaRecoverMultipleError(
                f"{expected_id}: raw {key} is missing"
            )
        _strict_equal(raw[key], value, f"{expected_id}.raw.{key}")
    groups = raw.get("effectGroupIds")
    if not isinstance(groups, list) or any(
        type(item) is not str or not item for item in groups
    ):
        raise Plan2NativeStaminaRecoverMultipleError(
            f"{expected_id}: raw effectGroupIds are not strings"
        )
    if expected_groups is not None:
        _strict_equal(groups, list(expected_groups), f"{expected_id}.raw.effectGroupIds")


@dataclass(frozen=True, slots=True)
class Plan2NativeStaminaRecoverMultipleEffect:
    """One exact target effect row retained in signed Int32 form."""

    effect_id: str
    effect_value1: int
    effect_value2: int = 0
    effect_count: int = 0
    effect_turn: int = 0
    effect_group_ids: tuple[str, ...] = TARGET_EFFECT_GROUP_IDS
    effect_type: str = MULTIPLE_EFFECT_TYPE
    status_enchant_id: str = ""
    chain_effect_id: str = ""

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect_id")
        if self.effect_type != MULTIPLE_EFFECT_TYPE:
            raise Plan2NativeStaminaRecoverMultipleError(
                "effect_type is not ExamStaminaRecoverMultiple"
            )
        for name in (
            "effect_value1",
            "effect_value2",
            "effect_count",
            "effect_turn",
        ):
            _i32(getattr(self, name), name)
        if self.effect_value2 != 0:
            raise Plan2NativeStaminaRecoverMultipleError(
                "effectValue2 is not the proven Multiple shape"
            )
        if self.effect_count != 0:
            raise Plan2NativeStaminaRecoverMultipleError(
                "effectCount is not the proven Multiple shape"
            )
        if self.effect_turn != 0:
            raise Plan2NativeStaminaRecoverMultipleError(
                "effectTurn is not the proven Multiple shape"
            )
        if tuple(self.effect_group_ids) != TARGET_EFFECT_GROUP_IDS:
            raise Plan2NativeStaminaRecoverMultipleError(
                "effectGroupIds are not the proven Multiple shape"
            )
        _text(self.status_enchant_id, "status_enchant_id", empty=True)
        _text(self.chain_effect_id, "chain_effect_id", empty=True)
        if self.status_enchant_id or self.chain_effect_id:
            raise Plan2NativeStaminaRecoverMultipleError(
                "nested status/chain data is not the proven Multiple shape"
            )

    @property
    def value1(self) -> int:
        return self.effect_value1

    @property
    def value1_permille(self) -> int:
        return self.effect_value1

    @property
    def count(self) -> int:
        return self.effect_count

    @property
    def turn(self) -> int:
        return self.effect_turn

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.effect_id,
            "effectType": self.effect_type,
            "value1": self.effect_value1,
            "value2": self.effect_value2,
            "count": self.effect_count,
            "turn": self.effect_turn,
            "effectGroupIds": list(self.effect_group_ids),
            "statusEnchantId": self.status_enchant_id,
            "chainEffectId": self.chain_effect_id,
        }

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, object]
    ) -> "Plan2NativeStaminaRecoverMultipleEffect":
        if not isinstance(raw, Mapping):
            raise Plan2NativeStaminaRecoverMultipleError(
                "effect payload must be a mapping"
            )
        allowed = {
            "id",
            "effect_id",
            "effectType",
            "effect_type",
            "effectValue1",
            "value1",
            "effect_value1",
            "effectValue2",
            "value2",
            "effect_value2",
            "effectCount",
            "count",
            "effect_count",
            "effectTurn",
            "turn",
            "effect_turn",
            "effectGroupIds",
            "effect_group_ids",
            "statusEnchantId",
            "status_enchant_id",
            "produceExamStatusEnchantId",
            "chainEffectId",
            "chain_effect_id",
            "chainProduceExamEffectId",
            "chainProduceExamEffectIds",
            "raw_json",
            *_EFFECT_RAW_NEUTRAL.keys(),
            "produceDescriptions",
            "customizeProduceDescriptions",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise Plan2NativeStaminaRecoverMultipleError(
                f"unknown Multiple effect keys: {sorted(unknown)!r}"
            )

        def pick(*names: str, required: bool = True) -> object:
            for name in names:
                if name in raw:
                    return raw[name]
            if required:
                raise Plan2NativeStaminaRecoverMultipleError(
                    f"missing effect field: {names[0]}"
                )
            return ""

        effect_id = _text(pick("id", "effect_id"), "effect_id")
        effect_type = _text(
            pick("effectType", "effect_type"), "effect_type"
        )
        value1 = _i32(
            pick("effectValue1", "value1", "effect_value1"),
            "effect_value1",
        )
        value2 = _i32(
            pick("effectValue2", "value2", "effect_value2"),
            "effect_value2",
        )
        count = _i32(
            pick("effectCount", "count", "effect_count"),
            "effect_count",
        )
        turn = _i32(
            pick("effectTurn", "turn", "effect_turn"),
            "effect_turn",
        )
        status_id = _text(
            pick(
                "produceExamStatusEnchantId",
                "statusEnchantId",
                "status_enchant_id",
                required=False,
            ),
            "status_enchant_id",
            empty=True,
        )
        chain_id = _text(
            pick(
                "chainProduceExamEffectId",
                "chainEffectId",
                "chain_effect_id",
                required=False,
            ),
            "chain_effect_id",
            empty=True,
        )
        if status_id or chain_id:
            raise Plan2NativeStaminaRecoverMultipleError(
                "nested status/chain data is not supported"
            )
        if "chainProduceExamEffectIds" in raw and raw["chainProduceExamEffectIds"] not in (
            [],
            (),
            "",
            None,
        ):
            raise Plan2NativeStaminaRecoverMultipleError(
                "nested chain effect list is not supported"
            )

        raw_json = raw.get("raw_json")
        raw_effect: Mapping[str, object] | None = None
        if raw_json is not None:
            raw_effect = _json_object(raw_json, f"{effect_id}.raw_json")
            _validate_effect_raw(
                raw_effect,
                expected_id=effect_id,
                expected_type=effect_type,
                expected_value1=value1,
                expected_groups=TARGET_EFFECT_GROUP_IDS,
            )
        groups_value = raw.get("effectGroupIds", raw.get("effect_group_ids"))
        if groups_value is None and raw_effect is not None:
            groups_value = raw_effect.get("effectGroupIds")
        if isinstance(groups_value, tuple):
            groups_value = list(groups_value)
        if groups_value != list(TARGET_EFFECT_GROUP_IDS):
            raise Plan2NativeStaminaRecoverMultipleError(
                "effectGroupIds are not the proven Multiple shape"
            )
        for key, expected in _EFFECT_RAW_NEUTRAL.items():
            if key in raw:
                _strict_equal(raw[key], expected, f"effect.{key}")
        return cls(
            effect_id=effect_id,
            effect_type=effect_type,
            effect_value1=value1,
            effect_value2=value2,
            effect_count=count,
            effect_turn=turn,
            effect_group_ids=TARGET_EFFECT_GROUP_IDS,
            status_enchant_id=status_id,
            chain_effect_id=chain_id,
        )


def try_parse_stamina_recover_multiple(
    raw: Mapping[str, object],
) -> Plan2NativeStaminaRecoverMultipleEffect | None:
    """Parse only the known target shape; unknown shapes resolve to ``None``."""

    try:
        return Plan2NativeStaminaRecoverMultipleEffect.from_mapping(raw)
    except (KeyError, TypeError, ValueError, Plan2NativeStaminaRecoverMultipleError):
        return None


@dataclass(frozen=True, slots=True)
class Plan2NativeStaminaRecoverMultipleEffectSlot:
    slot_index: int
    effect_id: str
    effect_type: str
    trigger_id: str = ""
    hide_icon: bool = False
    is_once_play_effect: bool = False

    def __post_init__(self) -> None:
        _nonnegative_i32(self.slot_index, "slot_index")
        _text(self.effect_id, "slot.effect_id")
        _text(self.effect_type, "slot.effect_type")
        _text(self.trigger_id, "slot.trigger_id", empty=True)
        if type(self.hide_icon) is not bool:
            raise Plan2NativeStaminaRecoverMultipleError(
                "slot.hide_icon must be bool"
            )
        if type(self.is_once_play_effect) is not bool:
            raise Plan2NativeStaminaRecoverMultipleError(
                "slot.is_once_play_effect must be bool"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "slot": self.slot_index,
            "effectId": self.effect_id,
            "effectType": self.effect_type,
            "triggerId": self.trigger_id,
            "hideIcon": self.hide_icon,
            "isOncePlayEffect": self.is_once_play_effect,
        }


@dataclass(frozen=True, slots=True)
class Plan2NativeStaminaRecoverMultipleTrigger:
    trigger_id: str
    phase_types: tuple[str, ...]
    phase_values: tuple[int, ...]
    field_status_check_types: tuple[str, ...]
    field_status_types: tuple[str, ...]
    field_status_values: tuple[int, ...]
    field_status_produce_card_search_ids: tuple[str, ...]
    produce_card_search_id: str
    upper_search_count: int
    lower_search_count: int
    card_move_position_type: str
    effect_types: tuple[str, ...]
    lesson_type: str

    def __post_init__(self) -> None:
        _text(self.trigger_id, "trigger_id")
        for name in (
            "phase_types",
            "field_status_check_types",
            "field_status_types",
            "field_status_produce_card_search_ids",
            "effect_types",
        ):
            values = tuple(getattr(self, name))
            if any(type(item) is not str for item in values):
                raise Plan2NativeStaminaRecoverMultipleError(
                    f"{name} must contain text"
                )
            object.__setattr__(self, name, values)
        for name in ("phase_values", "field_status_values"):
            values = tuple(getattr(self, name))
            if any(isinstance(item, bool) or type(item) is not int for item in values):
                raise Plan2NativeStaminaRecoverMultipleError(
                    f"{name} must contain integers"
                )
            for item in values:
                _i32(item, f"{name} item")
            object.__setattr__(self, name, values)
        _text(self.produce_card_search_id, "produce_card_search_id", empty=True)
        _nonnegative_i32(self.upper_search_count, "upper_search_count")
        _nonnegative_i32(self.lower_search_count, "lower_search_count")
        _text(self.card_move_position_type, "card_move_position_type")
        _text(self.lesson_type, "lesson_type")

    @property
    def exact_target_shape(self) -> bool:
        return (
            self.trigger_id == TARGET_TRIGGER_ID
            and self.phase_types == (PHASE_EXAM_START_TURN,)
            and self.phase_values == ()
            and self.field_status_check_types == ()
            and self.field_status_types == (FIELD_REVIEW_UP,)
            and self.field_status_values == (TARGET_REVIEW_THRESHOLD,)
            and self.field_status_produce_card_search_ids == ()
            and self.produce_card_search_id == ""
            and self.upper_search_count == 0
            and self.lower_search_count == 0
            and self.card_move_position_type == MOVE_UNKNOWN
            and self.effect_types == ()
            and self.lesson_type == "ProduceStepLessonType_Unknown"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.trigger_id,
            "phaseTypes": list(self.phase_types),
            "phaseValues": list(self.phase_values),
            "fieldStatusCheckTypes": list(self.field_status_check_types),
            "fieldStatusTypes": list(self.field_status_types),
            "fieldStatusValues": list(self.field_status_values),
            "fieldStatusProduceCardSearchIds": list(
                self.field_status_produce_card_search_ids
            ),
            "produceCardSearchId": self.produce_card_search_id,
            "upperSearchCount": self.upper_search_count,
            "lowerSearchCount": self.lower_search_count,
            "cardMovePositionType": self.card_move_position_type,
            "effectTypes": list(self.effect_types),
            "lessonType": self.lesson_type,
        }


def _trigger_from_row(row: Mapping[str, object]) -> Plan2NativeStaminaRecoverMultipleTrigger:
    trigger_id = _text(row.get("id"), "trigger.id")
    raw = _json_object(row.get("raw_json"), f"{trigger_id}.raw_json")
    expected_raw = {
        "id": trigger_id,
        "phaseTypes": _json_array(row.get("phase_types_json"), "trigger.phaseTypes"),
        "phaseValues": _json_array(row.get("phase_values_json"), "trigger.phaseValues"),
        "fieldStatusCheckTypes": _json_array(
            row.get("field_status_check_types_json"),
            "trigger.fieldStatusCheckTypes",
        ),
        "fieldStatusTypes": _json_array(
            row.get("field_status_types_json"),
            "trigger.fieldStatusTypes",
        ),
        "fieldStatusValues": _json_array(
            row.get("field_status_values_json"),
            "trigger.fieldStatusValues",
        ),
        "fieldStatusProduceCardSearchIds": _json_array(
            row.get("field_status_produce_card_search_ids_json"),
            "trigger.fieldStatusProduceCardSearchIds",
        ),
        "produceCardSearchId": row.get("produce_card_search_id"),
        "upperSearchCount": row.get("upper_search_count"),
        "lowerSearchCount": row.get("lower_search_count"),
        "cardMovePositionType": row.get("card_move_position_type"),
        "effectTypes": _json_array(row.get("effect_types_json"), "trigger.effectTypes"),
        "lessonType": row.get("lesson_type"),
    }
    for key, value in expected_raw.items():
        if key not in raw or raw[key] != (
            list(value) if isinstance(value, tuple) else value
        ):
            raise Plan2NativeStaminaRecoverMultipleError(
                f"{trigger_id}: raw {key} does not match normalized trigger"
            )
    return Plan2NativeStaminaRecoverMultipleTrigger(
        trigger_id=trigger_id,
        phase_types=tuple(str(value) for value in expected_raw["phaseTypes"]),
        phase_values=tuple(
            _i32(value, "trigger.phase_value")
            for value in expected_raw["phaseValues"]
        ),
        field_status_check_types=tuple(
            str(value) for value in expected_raw["fieldStatusCheckTypes"]
        ),
        field_status_types=tuple(
            str(value) for value in expected_raw["fieldStatusTypes"]
        ),
        field_status_values=tuple(
            _i32(value, "trigger.field_status_value")
            for value in expected_raw["fieldStatusValues"]
        ),
        field_status_produce_card_search_ids=tuple(
            str(value)
            for value in expected_raw["fieldStatusProduceCardSearchIds"]
        ),
        produce_card_search_id=_text(
            expected_raw["produceCardSearchId"],
            "trigger.produce_card_search_id",
            empty=True,
        ),
        upper_search_count=_nonnegative_i32(
            expected_raw["upperSearchCount"], "trigger.upper_search_count"
        ),
        lower_search_count=_nonnegative_i32(
            expected_raw["lowerSearchCount"], "trigger.lower_search_count"
        ),
        card_move_position_type=_text(
            expected_raw["cardMovePositionType"],
            "trigger.card_move_position_type",
        ),
        effect_types=tuple(str(value) for value in expected_raw["effectTypes"]),
        lesson_type=_text(expected_raw["lessonType"], "trigger.lesson_type"),
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeStaminaRecoverMultipleCompanionBlocker:
    version_ref: tuple[str, int]
    family: str
    code: str
    detail: str
    effect_id: str = ""

    def __post_init__(self) -> None:
        if (
            type(self.version_ref) is not tuple
            or len(self.version_ref) != 2
            or type(self.version_ref[0]) is not str
            or type(self.version_ref[1]) is not int
            or self.version_ref[1] < 0
        ):
            raise Plan2NativeStaminaRecoverMultipleError(
                "blocker.version_ref must be (card_id, non-negative upgrade)"
            )
        _text(self.family, "blocker.family")
        _text(self.code, "blocker.code")
        if type(self.detail) is not str:
            raise Plan2NativeStaminaRecoverMultipleError(
                "blocker.detail must be text"
            )
        if type(self.effect_id) is not str:
            raise Plan2NativeStaminaRecoverMultipleError(
                "blocker.effect_id must be text"
            )

    @property
    def ref(self) -> tuple[str, int]:
        return self.version_ref

    @property
    def version_ref_text(self) -> str:
        return f"{self.version_ref[0]}#{self.version_ref[1]}"

    def to_dict(self) -> dict[str, object]:
        return {
            "versionRef": self.version_ref_text,
            "cardId": self.version_ref[0],
            "upgrade": self.version_ref[1],
            "family": self.family,
            "code": self.code,
            "effectId": self.effect_id,
            "detail": self.detail,
        }


Plan2NativeStaminaRecoverMultipleBlocker = (
    Plan2NativeStaminaRecoverMultipleCompanionBlocker
)
Plan2NativeCatalogStaminaRecoverMultipleBlocker = (
    Plan2NativeStaminaRecoverMultipleCompanionBlocker
)
Plan2NativeStaminaRecoverMultipleBlocker = (
    Plan2NativeStaminaRecoverMultipleCompanionBlocker
)


def _companion_family(effect_type: str) -> str:
    return {
        REVIEW_EFFECT_TYPE: "exam_review",
        BLOCK_EFFECT_TYPE: "exam_block",
        PLAYABLE_VALUE_ADD_EFFECT_TYPE: "exam_playable_value_add",
    }.get(effect_type, "unknown_companion")


@dataclass(frozen=True, slots=True)
class Plan2NativeStaminaRecoverMultipleProgram:
    card_id: str
    upgrade: int
    card_name: str
    plan_type: str
    category: str
    stamina: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    play_trigger: Plan2NativeStaminaRecoverMultipleTrigger | None
    move_position_type: str
    effect_slots: tuple[Plan2NativeStaminaRecoverMultipleEffectSlot, ...]
    target_slot_index: int
    effect: Plan2NativeStaminaRecoverMultipleEffect
    companion_effect_ids: tuple[str, ...]
    companion_effect_types: tuple[str, ...]
    companion_blockers: tuple[Plan2NativeStaminaRecoverMultipleCompanionBlocker, ...]

    def __post_init__(self) -> None:
        _text(self.card_id, "card_id")
        _nonnegative_i32(self.upgrade, "upgrade")
        _text(self.card_name, "card_name")
        if self.plan_type != PLAN2:
            raise Plan2NativeStaminaRecoverMultipleError(
                "target card is not Plan2"
            )
        _text(self.category, "category")
        _nonnegative_i32(self.stamina, "stamina")
        _text(self.cost_type, "cost_type")
        _nonnegative_i32(self.cost_value, "cost_value")
        _text(self.play_trigger_id, "play_trigger_id", empty=True)
        if self.play_trigger is not None and not isinstance(
            self.play_trigger, Plan2NativeStaminaRecoverMultipleTrigger
        ):
            raise TypeError("play_trigger must be a typed trigger or None")
        _text(self.move_position_type, "move_position_type")
        slots = tuple(self.effect_slots)
        if not slots or any(
            not isinstance(value, Plan2NativeStaminaRecoverMultipleEffectSlot)
            for value in slots
        ):
            raise TypeError("effect_slots must contain typed slots")
        if tuple(value.slot_index for value in slots) != tuple(range(len(slots))):
            raise Plan2NativeStaminaRecoverMultipleError(
                "effect slots do not preserve contiguous Master order"
            )
        object.__setattr__(self, "effect_slots", slots)
        _nonnegative_i32(self.target_slot_index, "target_slot_index")
        if self.target_slot_index >= len(slots):
            raise Plan2NativeStaminaRecoverMultipleError(
                "target slot is outside card order"
            )
        target = slots[self.target_slot_index]
        if target.effect_id != self.effect.effect_id or target.effect_type != MULTIPLE_EFFECT_TYPE:
            raise Plan2NativeStaminaRecoverMultipleError(
                "target effect does not match target slot"
            )
        if len(self.companion_effect_ids) != len(self.companion_effect_types):
            raise Plan2NativeStaminaRecoverMultipleError(
                "companion IDs/types are not aligned"
            )
        if any(not isinstance(value, str) or not value for value in self.companion_effect_ids):
            raise TypeError("companion_effect_ids must contain text")
        if any(not isinstance(value, str) or not value for value in self.companion_effect_types):
            raise TypeError("companion_effect_types must contain text")
        blockers = tuple(self.companion_blockers)
        if any(
            not isinstance(value, Plan2NativeStaminaRecoverMultipleCompanionBlocker)
            for value in blockers
        ):
            raise TypeError("companion_blockers must contain typed blockers")
        if any(value.ref != self.ref for value in blockers):
            raise Plan2NativeStaminaRecoverMultipleError(
                "companion blocker ref does not match program"
            )
        object.__setattr__(self, "companion_effect_ids", tuple(self.companion_effect_ids))
        object.__setattr__(self, "companion_effect_types", tuple(self.companion_effect_types))
        object.__setattr__(self, "companion_blockers", blockers)

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def version_ref(self) -> str:
        return f"{self.card_id}#{self.upgrade}"

    @property
    def target_effect(self) -> Plan2NativeStaminaRecoverMultipleEffect:
        return self.effect

    @property
    def target_effect_id(self) -> str:
        return self.effect.effect_id

    @property
    def target_effect_type(self) -> str:
        return self.effect.effect_type

    @property
    def slot_index(self) -> int:
        return self.target_slot_index

    @property
    def value1(self) -> int:
        return self.effect.effect_value1

    @property
    def value1_permille(self) -> int:
        return self.effect.effect_value1

    @property
    def value2(self) -> int:
        return self.effect.effect_value2

    @property
    def count(self) -> int:
        return self.effect.effect_count

    @property
    def turn(self) -> int:
        return self.effect.effect_turn

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return tuple(value.effect_id for value in self.effect_slots)

    @property
    def prior_effect_ids(self) -> tuple[str, ...]:
        return self.ordered_effect_ids[: self.target_slot_index]

    @property
    def supported_play_origins(self) -> tuple[str, ...]:
        return PLAY_ORIGINS

    @property
    def play_origins(self) -> tuple[str, ...]:
        return self.supported_play_origins

    @property
    def target_effect_executable(self) -> bool:
        return True

    @property
    def target_executable(self) -> bool:
        return self.target_effect_executable

    @property
    def whole_card_executable(self) -> bool:
        return not self.companion_blockers

    @property
    def companion_blocker_families(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(value.family for value in self.companion_blockers))

    def to_dict(self) -> dict[str, object]:
        return {
            "versionRef": self.version_ref,
            "cardId": self.card_id,
            "upgrade": self.upgrade,
            "cardName": self.card_name,
            "planType": self.plan_type,
            "category": self.category,
            "stamina": self.stamina,
            "costType": self.cost_type,
            "costValue": self.cost_value,
            "playTriggerId": self.play_trigger_id,
            "playTrigger": (
                None if self.play_trigger is None else self.play_trigger.to_dict()
            ),
            "movePositionType": self.move_position_type,
            "targetSlotIndex": self.target_slot_index,
            "targetEffect": self.effect.to_dict(),
            "orderedEffectIds": list(self.ordered_effect_ids),
            "priorEffectIds": list(self.prior_effect_ids),
            "effectSlots": [value.to_dict() for value in self.effect_slots],
            "companionEffectIds": list(self.companion_effect_ids),
            "companionEffectTypes": list(self.companion_effect_types),
            "supportedPlayOrigins": list(self.supported_play_origins),
            "targetEffectExecutable": self.target_effect_executable,
            "wholeCardExecutable": self.whole_card_executable,
            "companionBlockers": [
                value.to_dict() for value in self.companion_blockers
            ],
        }


Plan2NativeStaminaRecoverMultipleCompiledProgram = (
    Plan2NativeStaminaRecoverMultipleProgram
)
Plan2NativeStaminaRecoverMultipleCardVersion = (
    Plan2NativeStaminaRecoverMultipleProgram
)


@dataclass(frozen=True, slots=True)
class Plan2NativeStaminaRecoverMultipleCatalog:
    database: str
    programs: tuple[Plan2NativeStaminaRecoverMultipleProgram, ...]
    effects: tuple[Plan2NativeStaminaRecoverMultipleEffect, ...]
    triggers: tuple[Plan2NativeStaminaRecoverMultipleTrigger, ...] = ()

    def __post_init__(self) -> None:
        _text(self.database, "database")
        programs = tuple(self.programs)
        if any(
            not isinstance(value, Plan2NativeStaminaRecoverMultipleProgram)
            for value in programs
        ):
            raise TypeError("programs must contain typed target programs")
        refs = tuple(value.ref for value in programs)
        if refs != tuple(sorted(refs)) or len(refs) != len(set(refs)):
            raise Plan2NativeStaminaRecoverMultipleError(
                "catalog programs are not sorted and unique"
            )
        effects = tuple(self.effects)
        if any(
            not isinstance(value, Plan2NativeStaminaRecoverMultipleEffect)
            for value in effects
        ):
            raise TypeError("effects must contain typed target effects")
        effect_ids = tuple(value.effect_id for value in effects)
        if len(effect_ids) != len(set(effect_ids)):
            raise Plan2NativeStaminaRecoverMultipleError(
                "catalog effect IDs are not unique"
            )
        if any(value.target_effect_id not in effect_ids for value in programs):
            raise Plan2NativeStaminaRecoverMultipleError(
                "program target effect is not indexed"
            )
        triggers = tuple(self.triggers)
        if any(
            not isinstance(value, Plan2NativeStaminaRecoverMultipleTrigger)
            for value in triggers
        ):
            raise TypeError("triggers must contain typed triggers")
        object.__setattr__(self, "programs", programs)
        object.__setattr__(self, "effects", effects)
        object.__setattr__(self, "triggers", triggers)

    @property
    def versions(self) -> tuple[Plan2NativeStaminaRecoverMultipleProgram, ...]:
        return self.programs

    @property
    def affected_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(value.ref for value in self.programs)

    @property
    def affected_version_count(self) -> int:
        return len(self.programs)

    @property
    def target_effect_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.affected_refs

    @property
    def executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.target_effect_executable_refs

    @property
    def executable_version_count(self) -> int:
        return len(self.executable_refs)

    @property
    def companion_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(value.ref for value in self.programs if value.companion_blockers)

    @property
    def whole_card_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(value.ref for value in self.programs if value.whole_card_executable)

    @property
    def companion_blockers(self) -> tuple[Plan2NativeStaminaRecoverMultipleCompanionBlocker, ...]:
        return tuple(
            blocker
            for program in self.programs
            for blocker in program.companion_blockers
        )

    @property
    def companion_blocker_code_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(value.code for value in self.companion_blockers).items()))

    @property
    def companion_family_version_counts(self) -> dict[str, int]:
        refs: dict[str, set[tuple[str, int]]] = {}
        for blocker in self.companion_blockers:
            refs.setdefault(blocker.family, set()).add(blocker.ref)
        return {family: len(values) for family, values in sorted(refs.items())}

    def get(
        self, card_id: str, upgrade: int
    ) -> Plan2NativeStaminaRecoverMultipleProgram | None:
        return next(
            (value for value in self.programs if value.ref == (card_id, upgrade)),
            None,
        )

    def resolve(
        self, card_id: str, upgrade: int
    ) -> Plan2NativeStaminaRecoverMultipleProgram:
        program = self.get(card_id, upgrade)
        if program is None:
            raise KeyError(f"uncompiled Multiple version: {card_id}#{upgrade}")
        return program

    @property
    def effect_by_id(self) -> dict[str, Plan2NativeStaminaRecoverMultipleEffect]:
        return {value.effect_id: value for value in self.effects}

    def to_dict(self) -> dict[str, object]:
        return {
            "database": self.database,
            "affectedVersionCount": self.affected_version_count,
            "executableVersionCount": self.executable_version_count,
            "companionBlockedVersionCount": len(self.companion_blocked_refs),
            "wholeCardExecutableVersionCount": len(self.whole_card_executable_refs),
            "effects": [value.to_dict() for value in self.effects],
            "triggers": [value.to_dict() for value in self.triggers],
            "programs": [value.to_dict() for value in self.programs],
        }


Plan2NativeCatalogStaminaRecoverMultiple = Plan2NativeStaminaRecoverMultipleCatalog


@dataclass(frozen=True, slots=True)
class Plan2NativeStaminaRecoverMultipleCompilation:
    schema_version: int
    database: str
    affected_refs: tuple[tuple[str, int], ...]
    catalog: Plan2NativeStaminaRecoverMultipleCatalog
    blockers: tuple[Plan2NativeStaminaRecoverMultipleCompanionBlocker, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != PLAN2_NATIVE_CATALOG_STAMINA_RECOVER_MULTIPLE_SCHEMA_VERSION:
            raise Plan2NativeStaminaRecoverMultipleError(
                "unsupported catalog schema version"
            )
        refs = tuple(self.affected_refs)
        if refs != tuple(sorted(refs)) or len(refs) != len(set(refs)):
            raise Plan2NativeStaminaRecoverMultipleError(
                "affected refs are not sorted and unique"
            )
        if not isinstance(self.catalog, Plan2NativeStaminaRecoverMultipleCatalog):
            raise TypeError("catalog must be a Multiple catalog")
        if any(value not in refs for value in self.catalog.affected_refs):
            raise Plan2NativeStaminaRecoverMultipleError(
                "catalog contains a version outside affected refs"
            )
        blockers = tuple(self.blockers)
        if any(
            not isinstance(value, Plan2NativeStaminaRecoverMultipleCompanionBlocker)
            for value in blockers
        ):
            raise TypeError("blockers must contain typed blockers")
        object.__setattr__(self, "affected_refs", refs)
        object.__setattr__(self, "blockers", blockers)

    @property
    def compiled_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.affected_refs

    @property
    def compiled_version_count(self) -> int:
        return len(self.compiled_refs)

    @property
    def failed_refs(self) -> tuple[tuple[str, int], ...]:
        compiled = set(self.compiled_refs)
        return tuple(value for value in self.affected_refs if value not in compiled)

    @property
    def affected_version_count(self) -> int:
        return len(self.affected_refs)

    @property
    def failed_version_count(self) -> int:
        return len(self.failed_refs)

    @property
    def executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.executable_refs

    @property
    def executable_version_count(self) -> int:
        return self.catalog.executable_version_count

    @property
    def target_effect_executable(self) -> bool:
        return (
            self.affected_refs == tuple(
                (TARGET_CARD_ID, upgrade) for upgrade in TARGET_UPGRADES
            )
            and not self.failed_refs
            and not self.blockers
        )

    @property
    def fully_compiled(self) -> bool:
        return not self.failed_refs and not self.blockers

    @property
    def companion_blockers(self) -> tuple[Plan2NativeStaminaRecoverMultipleCompanionBlocker, ...]:
        return self.catalog.companion_blockers

    @property
    def companion_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.companion_blocked_refs

    @property
    def whole_card_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.whole_card_executable_refs

    @property
    def companion_blocker_code_counts(self) -> dict[str, int]:
        return self.catalog.companion_blocker_code_counts

    @property
    def companion_family_version_counts(self) -> dict[str, int]:
        return self.catalog.companion_family_version_counts

    def blockers_for(
        self, ref: tuple[str, int]
    ) -> tuple[Plan2NativeStaminaRecoverMultipleCompanionBlocker, ...]:
        return tuple(value for value in self.blockers if value.ref == ref)

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "database": self.database,
            "affectedVersionCount": self.affected_version_count,
            "compiledVersionCount": self.compiled_version_count,
            "failedVersionCount": self.failed_version_count,
            "executableVersionCount": self.executable_version_count,
            "targetEffectExecutable": self.target_effect_executable,
            "fullyCompiled": self.fully_compiled,
            "affectedRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.affected_refs
            ],
            "compiledRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.compiled_refs
            ],
            "failedRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.failed_refs
            ],
            "blockers": [value.to_dict() for value in self.blockers],
            "companionBlockers": [
                value.to_dict() for value in self.companion_blockers
            ],
            "companionBlockedRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.companion_blocked_refs
            ],
            "wholeCardExecutableRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.whole_card_executable_refs
            ],
            "companionBlockerCodeCounts": self.companion_blocker_code_counts,
            "companionFamilyVersionCounts": self.companion_family_version_counts,
        }


Plan2NativeCatalogStaminaRecoverMultipleCompilation = (
    Plan2NativeStaminaRecoverMultipleCompilation
)
Plan2NativeStaminaRecoverMultipleCatalogCompilation = (
    Plan2NativeStaminaRecoverMultipleCompilation
)


def _validate_card_raw(
    row: Mapping[str, object], links: tuple[object, ...], expected_ref: tuple[str, int]
) -> Mapping[str, object]:
    card_id, upgrade = expected_ref
    raw = _json_object(row.get("raw_json"), f"{card_id}#{upgrade}.raw_json")
    expected = {
        "id": card_id,
        "upgradeCount": upgrade,
        "planType": row.get("plan_type"),
        "category": row.get("category"),
        "stamina": row.get("stamina"),
        "costType": row.get("cost_type"),
        "costValue": row.get("cost_value"),
        "playProduceExamTriggerId": row.get("play_trigger_id"),
        "playEffects": list(links),
        "playMovePositionType": row.get("move_position_type"),
        "moveEffectTriggerType": MOVE_TRIGGER_UNKNOWN,
        "moveProduceExamEffectIds": [],
        "moveProduceExamTriggerIds": [],
        "produceCardStatusEnchantId": "",
        "forceStamina": 0,
        "effectGroupIds": list(TARGET_CARD_EFFECT_GROUP_IDS),
    }
    for key, value in expected.items():
        if key not in raw or raw[key] != value:
            raise Plan2NativeStaminaRecoverMultipleError(
                f"{card_id}#{upgrade}: raw card {key} changed"
            )
    return raw


def _link_slot(
    link: object,
    index: int,
    expected_effect_id: str,
) -> Plan2NativeStaminaRecoverMultipleEffectSlot:
    if not isinstance(link, Mapping):
        raise Plan2NativeStaminaRecoverMultipleError(
            f"card effect link {index} is not an object"
        )
    if set(link) != {
        "produceExamTriggerId",
        "produceExamEffectId",
        "hideIcon",
        "isOncePlayEffect",
    }:
        raise Plan2NativeStaminaRecoverMultipleError(
            f"card effect link {index} keys changed"
        )
    effect_id = _text(link.get("produceExamEffectId"), f"slot[{index}].effect_id")
    if effect_id != expected_effect_id:
        raise Plan2NativeStaminaRecoverMultipleError(
            f"slot[{index}] expected {expected_effect_id}, got {effect_id}"
        )
    trigger_id = _text(
        link.get("produceExamTriggerId"), f"slot[{index}].trigger_id", empty=True
    )
    if trigger_id:
        raise Plan2NativeStaminaRecoverMultipleError(
            f"slot[{index}] has an unowned effect trigger"
        )
    if type(link.get("hideIcon")) is not bool or type(link.get("isOncePlayEffect")) is not bool:
        raise Plan2NativeStaminaRecoverMultipleError(
            f"slot[{index}] bool shape changed"
        )
    return Plan2NativeStaminaRecoverMultipleEffectSlot(
        slot_index=index,
        effect_id=effect_id,
        effect_type="unresolved",  # filled after the effect row is resolved
        trigger_id=trigger_id,
        hide_icon=link["hideIcon"],
        is_once_play_effect=link["isOncePlayEffect"],
    )


def _companion_row_shape(
    row: Mapping[str, object], expected_effect_id: str
) -> tuple[str, str]:
    effect_id = _text(row.get("id"), "companion.id")
    if effect_id != expected_effect_id:
        raise Plan2NativeStaminaRecoverMultipleError(
            f"companion expected {expected_effect_id}, got {effect_id}"
        )
    effect_type = _text(row.get("effect_type"), f"{effect_id}.effect_type")
    value1 = _i32(row.get("value1"), f"{effect_id}.value1")
    value2 = _i32(row.get("value2"), f"{effect_id}.value2")
    count = _i32(row.get("effect_count"), f"{effect_id}.effect_count")
    turn = _i32(row.get("effect_turn"), f"{effect_id}.effect_turn")
    status_id = _text(row.get("status_enchant_id"), f"{effect_id}.status", empty=True)
    chain_id = _text(row.get("chain_effect_id"), f"{effect_id}.chain", empty=True)
    if status_id or chain_id:
        raise Plan2NativeStaminaRecoverMultipleError(
            f"{effect_id}: nested companion status/chain is unowned"
        )
    raw = _json_object(row.get("raw_json"), f"{effect_id}.raw_json")
    _validate_effect_raw(
        raw,
        expected_id=effect_id,
        expected_type=effect_type,
        expected_value1=value1,
        expected_value2=value2,
        expected_count=count,
        expected_turn=turn,
        expected_groups=None,
    )
    if effect_type == PLAYABLE_VALUE_ADD_EFFECT_TYPE:
        if value1 != 0 or value2 != 0 or count < 1 or turn != 0:
            raise Plan2NativeStaminaRecoverMultipleError(
                f"{effect_id}: PlayableValueAdd scalar shape changed"
            )
    elif effect_type in (REVIEW_EFFECT_TYPE, BLOCK_EFFECT_TYPE):
        if value1 < 0 or value2 != 0 or count != 0 or turn != 0:
            raise Plan2NativeStaminaRecoverMultipleError(
                f"{effect_id}: scalar companion shape changed"
            )
    return effect_id, effect_type


def _make_companion_blocker(
    ref: tuple[str, int],
    *,
    family: str,
    code: str,
    detail: str,
    effect_id: str = "",
) -> Plan2NativeStaminaRecoverMultipleCompanionBlocker:
    return Plan2NativeStaminaRecoverMultipleCompanionBlocker(
        version_ref=ref,
        family=family,
        code=code,
        detail=detail,
        effect_id=effect_id,
    )


def _compile_card(
    connection: sqlite3.Connection,
    row: Mapping[str, object],
    *,
    target_effects: Mapping[str, Plan2NativeStaminaRecoverMultipleEffect],
    trigger: Plan2NativeStaminaRecoverMultipleTrigger | None,
    trigger_error: str | None,
) -> Plan2NativeStaminaRecoverMultipleProgram:
    card_id = _text(row.get("id"), "card.id")
    upgrade = _nonnegative_i32(row.get("upgrade_count"), "card.upgrade_count")
    ref = (card_id, upgrade)
    if card_id != TARGET_CARD_ID or upgrade not in TARGET_UPGRADES:
        raise Plan2NativeStaminaRecoverMultipleError(
            f"unexpected target version: {card_id}#{upgrade}"
        )
    if row.get("plan_type") != PLAN2:
        raise Plan2NativeStaminaRecoverMultipleError(
            f"{card_id}#{upgrade}: card is not Plan2"
        )
    if row.get("category") != MENTAL_SKILL:
        raise Plan2NativeStaminaRecoverMultipleError(
            f"{card_id}#{upgrade}: category changed"
        )
    if _nonnegative_i32(row.get("stamina"), "card.stamina") != TARGET_CARD_STAMINA_BY_UPGRADE[upgrade]:
        raise Plan2NativeStaminaRecoverMultipleError(
            f"{card_id}#{upgrade}: stamina changed"
        )
    if row.get("cost_type") != COST_UNKNOWN or _nonnegative_i32(row.get("cost_value"), "card.cost_value") != 0:
        raise Plan2NativeStaminaRecoverMultipleError(
            f"{card_id}#{upgrade}: cost shape changed"
        )
    play_trigger_id = _text(row.get("play_trigger_id"), "card.play_trigger_id")
    if play_trigger_id != TARGET_TRIGGER_ID:
        raise Plan2NativeStaminaRecoverMultipleError(
            f"{card_id}#{upgrade}: play trigger changed"
        )
    if row.get("move_position_type") != MOVE_LOST:
        raise Plan2NativeStaminaRecoverMultipleError(
            f"{card_id}#{upgrade}: move position changed"
        )
    links = _json_array(row.get("play_effects_json"), f"{card_id}#{upgrade}.playEffects")
    expected_ids = TARGET_EFFECT_IDS_BY_UPGRADE[upgrade]
    if len(links) != len(expected_ids):
        raise Plan2NativeStaminaRecoverMultipleError(
            f"{card_id}#{upgrade}: effect slot count changed"
        )
    _validate_card_raw(row, links, ref)

    slots: list[Plan2NativeStaminaRecoverMultipleEffectSlot] = []
    resolved_types: list[str] = []
    companion_ids: list[str] = []
    companion_types: list[str] = []
    blockers: list[Plan2NativeStaminaRecoverMultipleCompanionBlocker] = []
    for index, (link, expected_id) in enumerate(zip(links, expected_ids, strict=True)):
        slot = _link_slot(link, index, expected_id)
        companion_row = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (expected_id,)
        ).fetchone()
        if companion_row is None:
            raise Plan2NativeStaminaRecoverMultipleError(
                f"{card_id}#{upgrade}: missing effect row {expected_id}"
            )
        companion_id, effect_type = _companion_row_shape(dict(companion_row), expected_id)
        if index == 0:
            target = target_effects.get(expected_id)
            if target is None or target.effect_value1 != TARGET_MULTIPLE_PERMILLE_BY_UPGRADE[upgrade]:
                raise Plan2NativeStaminaRecoverMultipleError(
                    f"{card_id}#{upgrade}: target effect row changed"
                )
            slot = Plan2NativeStaminaRecoverMultipleEffectSlot(
                slot_index=index,
                effect_id=slot.effect_id,
                effect_type=MULTIPLE_EFFECT_TYPE,
                trigger_id=slot.trigger_id,
                hide_icon=slot.hide_icon,
                is_once_play_effect=slot.is_once_play_effect,
            )
            target_effect = target
        else:
            if effect_type == MULTIPLE_EFFECT_TYPE:
                raise Plan2NativeStaminaRecoverMultipleError(
                    f"{card_id}#{upgrade}: multiple target appears more than once"
                )
            slot = Plan2NativeStaminaRecoverMultipleEffectSlot(
                slot_index=index,
                effect_id=slot.effect_id,
                effect_type=effect_type,
                trigger_id=slot.trigger_id,
                hide_icon=slot.hide_icon,
                is_once_play_effect=slot.is_once_play_effect,
            )
            companion_ids.append(companion_id)
            companion_types.append(effect_type)
        slots.append(slot)
        resolved_types.append(effect_type)

    if trigger_error is not None or trigger is None or not trigger.exact_target_shape:
        blockers.append(
            _make_companion_blocker(
                ref,
                family="card_trigger",
                code="companion-trigger-unbound"
                if trigger_error is None
                else "companion-trigger-shape-invalid",
                detail=(
                    "StartTurn ReviewUp trigger is typed but not executed by this leaf"
                    if trigger_error is None
                    else trigger_error
                ),
                effect_id=play_trigger_id,
            )
        )
    else:
        # The trigger is retained as exact data, but this leaf owns only the
        # target effect.  Central trigger execution must explicitly clear it.
        blockers.append(
            _make_companion_blocker(
                ref,
                family="card_trigger",
                code="companion-trigger-unbound",
                detail="StartTurn ReviewUp trigger is typed but not executed by this leaf",
                effect_id=play_trigger_id,
            )
        )
    for effect_id, effect_type in zip(companion_ids, companion_types, strict=True):
        blockers.append(
            _make_companion_blocker(
                ref,
                family=_companion_family(effect_type),
                code="companion-effect-unbound",
                detail=(
                    f"{effect_type} remains a downstream companion; this leaf owns "
                    "only ExamStaminaRecoverMultiple"
                ),
                effect_id=effect_id,
            )
        )

    return Plan2NativeStaminaRecoverMultipleProgram(
        card_id=card_id,
        upgrade=upgrade,
        card_name=_text(row.get("name"), "card.name"),
        plan_type=PLAN2,
        category=MENTAL_SKILL,
        stamina=int(row["stamina"]),
        cost_type=COST_UNKNOWN,
        cost_value=0,
        play_trigger_id=play_trigger_id,
        play_trigger=trigger if trigger_error is None else None,
        move_position_type=MOVE_LOST,
        effect_slots=tuple(slots),
        target_slot_index=0,
        effect=target_effect,
        companion_effect_ids=tuple(companion_ids),
        companion_effect_types=tuple(companion_types),
        companion_blockers=tuple(blockers),
    )


def _target_effects_from_connection(
    connection: sqlite3.Connection,
) -> dict[str, Plan2NativeStaminaRecoverMultipleEffect]:
    effects: dict[str, Plan2NativeStaminaRecoverMultipleEffect] = {}
    expected_ids = tuple(sorted(set(TARGET_MULTIPLE_EFFECT_IDS_BY_UPGRADE.values())))
    for effect_id in expected_ids:
        row = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (effect_id,)
        ).fetchone()
        if row is None:
            raise Plan2NativeStaminaRecoverMultipleError(
                f"missing target effect row: {effect_id}"
            )
        normalized = dict(row)
        raw = _json_object(normalized.get("raw_json"), f"{effect_id}.raw_json")
        value1 = _i32(normalized.get("value1"), f"{effect_id}.value1")
        expected_value = next(
            value
            for version, value in TARGET_MULTIPLE_PERMILLE_BY_UPGRADE.items()
            if TARGET_MULTIPLE_EFFECT_IDS_BY_UPGRADE[version] == effect_id
        )
        if value1 != expected_value:
            raise Plan2NativeStaminaRecoverMultipleError(
                f"{effect_id}: value1 changed"
            )
        for key, expected in {
            "effect_type": MULTIPLE_EFFECT_TYPE,
            "value2": 0,
            "effect_count": 0,
            "effect_turn": 0,
            "status_enchant_id": "",
            "chain_effect_id": "",
        }.items():
            if normalized.get(key) != expected:
                raise Plan2NativeStaminaRecoverMultipleError(
                    f"{effect_id}: normalized {key} changed"
                )
        _validate_effect_raw(
            raw,
            expected_id=effect_id,
            expected_type=MULTIPLE_EFFECT_TYPE,
            expected_value1=value1,
            expected_groups=TARGET_EFFECT_GROUP_IDS,
        )
        effects[effect_id] = Plan2NativeStaminaRecoverMultipleEffect(
            effect_id=effect_id,
            effect_value1=value1,
            effect_group_ids=TARGET_EFFECT_GROUP_IDS,
        )
    return effects


def _load_target_cards(connection: sqlite3.Connection) -> tuple[dict[str, object], ...]:
    rows = tuple(
        dict(row)
        for row in connection.execute(
            """
            SELECT DISTINCT c.*
              FROM card AS c
              JOIN json_each(c.play_effects_json) AS link
              JOIN effect AS e
                ON e.id = json_extract(link.value, '$.produceExamEffectId')
             WHERE c.plan_type = ?
               AND e.effect_type = ?
             ORDER BY c.id, c.upgrade_count
            """,
            (PLAN2, MULTIPLE_EFFECT_TYPE),
        )
    )
    return rows


def compile_plan2_native_catalog_stamina_recover_multiple(
    database: str | Path = DEFAULT_DATABASE,
    *,
    enforce_current_master_count: bool = True,
) -> Plan2NativeStaminaRecoverMultipleCompilation:
    """Compile the exact four target versions from Master read-only."""

    database_path = Path(database).resolve()
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    with closing(
        sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        rows = _load_target_cards(connection)
        discovered = tuple((str(row["id"]), int(row["upgrade_count"])) for row in rows)
        expected = tuple((TARGET_CARD_ID, upgrade) for upgrade in TARGET_UPGRADES)
        if enforce_current_master_count and discovered != expected:
            raise Plan2NativeStaminaRecoverMultipleError(
                f"target version inventory changed: expected {expected!r}, got {discovered!r}"
            )
        affected_refs = discovered if discovered else expected
        target_effects = _target_effects_from_connection(connection)
        trigger: Plan2NativeStaminaRecoverMultipleTrigger | None = None
        trigger_error: str | None = None
        trigger_row = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?",
            (TARGET_TRIGGER_ID,),
        ).fetchone()
        if trigger_row is None:
            trigger_error = f"missing trigger row: {TARGET_TRIGGER_ID}"
        else:
            try:
                trigger = _trigger_from_row(dict(trigger_row))
            except (KeyError, TypeError, ValueError, Plan2NativeStaminaRecoverMultipleError) as error:
                trigger_error = f"{type(error).__name__}: {error}"

        programs: list[Plan2NativeStaminaRecoverMultipleProgram] = []
        hard_blockers: list[Plan2NativeStaminaRecoverMultipleCompanionBlocker] = []
        for row in rows:
            ref = (str(row["id"]), int(row["upgrade_count"]))
            try:
                program = _compile_card(
                    connection,
                    row,
                    target_effects=target_effects,
                    trigger=trigger,
                    trigger_error=trigger_error,
                )
            except (KeyError, TypeError, ValueError, Plan2NativeStaminaRecoverMultipleError) as error:
                hard_blockers.append(
                    _make_companion_blocker(
                        ref,
                        family="target_effect",
                        code="target-shape-invalid",
                        detail=f"{type(error).__name__}: {error}",
                    )
                )
            else:
                programs.append(program)

    catalog = Plan2NativeStaminaRecoverMultipleCatalog(
        database=str(database_path),
        programs=tuple(sorted(programs, key=lambda value: value.ref)),
        effects=tuple(target_effects[key] for key in sorted(target_effects)),
        triggers=() if trigger is None else (trigger,),
    )
    return Plan2NativeStaminaRecoverMultipleCompilation(
        schema_version=PLAN2_NATIVE_CATALOG_STAMINA_RECOVER_MULTIPLE_SCHEMA_VERSION,
        database=str(database_path),
        affected_refs=tuple(sorted(affected_refs)),
        catalog=catalog,
        blockers=tuple(hard_blockers),
    )


compile_plan2_stamina_recover_multiple_catalog = (
    compile_plan2_native_catalog_stamina_recover_multiple
)
compile_stamina_recover_multiple_catalog = (
    compile_plan2_native_catalog_stamina_recover_multiple
)


def load_plan2_native_catalog_stamina_recover_multiple(
    database: str | Path = DEFAULT_DATABASE,
) -> Plan2NativeStaminaRecoverMultipleCatalog:
    """Load a complete exact target catalog, failing closed on hard drift."""

    compilation = compile_plan2_native_catalog_stamina_recover_multiple(database)
    if compilation.blockers:
        first = compilation.blockers[0]
        raise Plan2NativeStaminaRecoverMultipleError(
            f"{first.version_ref_text}: {first.detail}"
        )
    expected = tuple((TARGET_CARD_ID, upgrade) for upgrade in TARGET_UPGRADES)
    if compilation.affected_refs != expected or compilation.compiled_refs != expected:
        raise Plan2NativeStaminaRecoverMultipleError(
            "unexpected target version count or order"
        )
    return compilation.catalog


load_plan2_stamina_recover_multiple_catalog = (
    load_plan2_native_catalog_stamina_recover_multiple
)
load_stamina_recover_multiple_catalog = load_plan2_native_catalog_stamina_recover_multiple


@dataclass(frozen=True, slots=True)
class Plan2NativeStaminaRecoverMultipleHandoffRow:
    """One immutable row accepted by a future central catalog/horizon."""

    program: Plan2NativeStaminaRecoverMultipleProgram

    def __post_init__(self) -> None:
        if not isinstance(self.program, Plan2NativeStaminaRecoverMultipleProgram):
            raise TypeError("program must be a target program")

    @property
    def ref(self) -> tuple[str, int]:
        return self.program.ref

    @property
    def version_ref(self) -> str:
        return self.program.version_ref

    @property
    def card_id(self) -> str:
        return self.program.card_id

    @property
    def upgrade(self) -> int:
        return self.program.upgrade

    @property
    def target_effect_id(self) -> str:
        return self.program.target_effect_id

    @property
    def target_slot_index(self) -> int:
        return self.program.target_slot_index

    @property
    def value1_permille(self) -> int:
        return self.program.value1_permille

    @property
    def value2(self) -> int:
        return self.program.value2

    @property
    def count(self) -> int:
        return self.program.count

    @property
    def turn(self) -> int:
        return self.program.turn

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return self.program.ordered_effect_ids

    @property
    def prior_effect_ids(self) -> tuple[str, ...]:
        return self.program.prior_effect_ids

    @property
    def supported_play_origins(self) -> tuple[str, ...]:
        return self.program.supported_play_origins

    @property
    def target_effect_executable(self) -> bool:
        return self.program.target_effect_executable

    @property
    def target_executable(self) -> bool:
        return self.program.target_executable

    @property
    def whole_card_executable(self) -> bool:
        return self.program.whole_card_executable

    @property
    def companion_blockers(self) -> tuple[Plan2NativeStaminaRecoverMultipleCompanionBlocker, ...]:
        return self.program.companion_blockers

    def to_dict(self) -> dict[str, object]:
        return self.program.to_dict()


@dataclass(frozen=True, slots=True)
class Plan2NativeStaminaRecoverMultipleCentralHandoff:
    adapter_id: str
    schema_version: int
    affected_refs: tuple[tuple[str, int], ...]
    executable_refs: tuple[tuple[str, int], ...]
    rows: tuple[Plan2NativeStaminaRecoverMultipleHandoffRow, ...]

    def __post_init__(self) -> None:
        if self.adapter_id != STAMINA_RECOVER_MULTIPLE_ADAPTER_ID:
            raise Plan2NativeStaminaRecoverMultipleError("handoff adapter id changed")
        if self.schema_version != PLAN2_NATIVE_CATALOG_STAMINA_RECOVER_MULTIPLE_SCHEMA_VERSION:
            raise Plan2NativeStaminaRecoverMultipleError("handoff schema version changed")
        if self.affected_refs != tuple(sorted(self.affected_refs)):
            raise Plan2NativeStaminaRecoverMultipleError("handoff refs are not sorted")
        if self.executable_refs != tuple(sorted(self.executable_refs)):
            raise Plan2NativeStaminaRecoverMultipleError(
                "handoff executable refs are not sorted"
            )
        if not set(self.executable_refs).issubset(self.affected_refs):
            raise Plan2NativeStaminaRecoverMultipleError(
                "handoff executable refs are not a subset"
            )
        if tuple(value.ref for value in self.rows) != self.affected_refs:
            raise Plan2NativeStaminaRecoverMultipleError(
                "handoff rows do not preserve version order"
            )

    @property
    def affected_version_count(self) -> int:
        return len(self.affected_refs)

    @property
    def executable_version_count(self) -> int:
        return len(self.executable_refs)

    @property
    def companion_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(value.ref for value in self.rows if value.companion_blockers)

    @property
    def whole_card_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(value.ref for value in self.rows if value.whole_card_executable)

    @property
    def companion_blockers(self) -> tuple[Plan2NativeStaminaRecoverMultipleCompanionBlocker, ...]:
        return tuple(
            blocker
            for row in self.rows
            for blocker in row.companion_blockers
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "adapterId": self.adapter_id,
            "schemaVersion": self.schema_version,
            "affectedVersionCount": self.affected_version_count,
            "executableVersionCount": self.executable_version_count,
            "affectedRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.affected_refs
            ],
            "executableRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.executable_refs
            ],
            "companionBlockedRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.companion_blocked_refs
            ],
            "wholeCardExecutableRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.whole_card_executable_refs
            ],
            "rows": [value.to_dict() for value in self.rows],
        }


Plan2StaminaRecoverMultipleCentralHandoff = (
    Plan2NativeStaminaRecoverMultipleCentralHandoff
)
Plan2NativeStaminaRecoverMultipleHandoff = (
    Plan2NativeStaminaRecoverMultipleCentralHandoff
)


def build_plan2_native_stamina_recover_multiple_handoff(
    source: Plan2NativeStaminaRecoverMultipleCompilation
    | Plan2NativeStaminaRecoverMultipleCatalog,
) -> Plan2NativeStaminaRecoverMultipleCentralHandoff:
    if isinstance(source, Plan2NativeStaminaRecoverMultipleCompilation):
        if source.blockers:
            first = source.blockers[0]
            raise Plan2NativeStaminaRecoverMultipleError(
                f"cannot hand off incomplete target catalog: {first.version_ref_text}: "
                f"{first.detail}"
            )
        catalog = source.catalog
    elif isinstance(source, Plan2NativeStaminaRecoverMultipleCatalog):
        catalog = source
    else:
        raise TypeError("source must be a Multiple catalog or compilation")
    rows = tuple(
        Plan2NativeStaminaRecoverMultipleHandoffRow(program)
        for program in catalog.programs
    )
    return Plan2NativeStaminaRecoverMultipleCentralHandoff(
        adapter_id=STAMINA_RECOVER_MULTIPLE_ADAPTER_ID,
        schema_version=PLAN2_NATIVE_CATALOG_STAMINA_RECOVER_MULTIPLE_SCHEMA_VERSION,
        affected_refs=catalog.affected_refs,
        executable_refs=catalog.executable_refs,
        rows=rows,
    )


build_plan2_stamina_recover_multiple_central_handoff = (
    build_plan2_native_stamina_recover_multiple_handoff
)
build_stamina_recover_multiple_central_handoff = (
    build_plan2_native_stamina_recover_multiple_handoff
)


def exact_plan2_native_stamina_recover_multiple_rows(
    source: Plan2NativeStaminaRecoverMultipleCompilation
    | Plan2NativeStaminaRecoverMultipleCatalog,
) -> tuple[dict[str, object], ...]:
    return tuple(
        row.to_dict()
        for row in build_plan2_native_stamina_recover_multiple_handoff(source).rows
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeStaminaRecoverMultipleSnapshot:
    """Explicit execution-time snapshot; it is not a mutable Plan2 state."""

    current_stamina: int
    max_stamina: int
    stamina_recover_restricted: bool = False
    stamina_recover_add_permil: int | None = None
    block: int = 0

    def __post_init__(self) -> None:
        StaminaRecoverFixRuntime(
            stamina=self.current_stamina,
            max_stamina=self.max_stamina,
            stamina_recover_restricted=self.stamina_recover_restricted,
            stamina_recover_add_permil=self.stamina_recover_add_permil,
            block=self.block,
        )

    @property
    def stamina(self) -> int:
        return self.current_stamina

    def as_recover_fix_runtime(self) -> StaminaRecoverFixRuntime:
        return StaminaRecoverFixRuntime(
            stamina=self.current_stamina,
            max_stamina=self.max_stamina,
            stamina_recover_restricted=self.stamina_recover_restricted,
            stamina_recover_add_permil=self.stamina_recover_add_permil,
            block=self.block,
        )


# The existing exact Fix helper uses these field names.  Exporting the alias
# keeps the target-family API compatible with the reviewed Plan2 recovery
# tests while the explicit Snapshot above documents the native read boundary.
StaminaRecoverMultipleRuntime = StaminaRecoverFixRuntime
Plan2NativeStaminaRecoverMultipleRuntime = StaminaRecoverFixRuntime


@dataclass(frozen=True, slots=True)
class Plan2NativeStaminaRecoverMultipleEvaluation:
    contract: Plan2NativeStaminaRecoverMultipleEffect | None
    runtime_before: StaminaRecoverFixRuntime
    runtime_after: StaminaRecoverFixRuntime
    executable: bool
    simulated: bool
    reason: str | None
    base_max_stamina: int | None
    scaled_float32: float | None
    requested_value: int | None
    adjusted_recovery_value: int | None
    calculated_recovery: int | None
    predicted_stamina: int | None
    difference: StaminaDifference | None
    fix_evaluation: StaminaRecoverFixEvaluation | None
    trace: tuple[str, ...]

    @property
    def actual_stamina(self) -> int:
        return self.runtime_after.stamina

    @property
    def fail_closed(self) -> bool:
        return not self.executable

    @property
    def snapshot_before(self) -> Plan2NativeStaminaRecoverMultipleSnapshot:
        return Plan2NativeStaminaRecoverMultipleSnapshot(
            current_stamina=self.runtime_before.stamina,
            max_stamina=self.runtime_before.max_stamina,
            stamina_recover_restricted=self.runtime_before.stamina_recover_restricted,
            stamina_recover_add_permil=self.runtime_before.stamina_recover_add_permil,
            block=self.runtime_before.block,
        )


def _normalize_runtime(
    runtime: StaminaRecoverFixRuntime | Plan2NativeStaminaRecoverMultipleSnapshot,
) -> StaminaRecoverFixRuntime:
    if isinstance(runtime, StaminaRecoverFixRuntime):
        return runtime
    if isinstance(runtime, Plan2NativeStaminaRecoverMultipleSnapshot):
        return runtime.as_recover_fix_runtime()
    raise TypeError(
        "runtime must be StaminaRecoverMultipleRuntime or an execution snapshot"
    )


def _unresolved_evaluation(
    runtime: StaminaRecoverFixRuntime,
    *,
    contract: Plan2NativeStaminaRecoverMultipleEffect | None,
    simulate: bool,
    reason: str,
    trace: tuple[str, ...],
    base_max_stamina: int | None = None,
    scaled_float32: float | None = None,
    requested_value: int | None = None,
    fix_evaluation: StaminaRecoverFixEvaluation | None = None,
) -> Plan2NativeStaminaRecoverMultipleEvaluation:
    return Plan2NativeStaminaRecoverMultipleEvaluation(
        contract=contract,
        runtime_before=runtime,
        runtime_after=runtime,
        executable=False,
        simulated=simulate,
        reason=reason,
        base_max_stamina=base_max_stamina,
        scaled_float32=scaled_float32,
        requested_value=requested_value,
        adjusted_recovery_value=(
            None if fix_evaluation is None else fix_evaluation.adjusted_recovery_value
        ),
        calculated_recovery=(
            None if fix_evaluation is None else fix_evaluation.calculated_recovery
        ),
        predicted_stamina=(
            None if fix_evaluation is None else fix_evaluation.predicted_stamina
        ),
        difference=None if fix_evaluation is None else fix_evaluation.difference,
        fix_evaluation=fix_evaluation,
        trace=trace,
    )


def _native_multiple_request(max_stamina: int, effect_value1: int) -> tuple[float, int]:
    """Compute the ordered finite binary32 MaxStamina permille request."""

    maximum = f32(max_stamina)
    value = f32(effect_value1)
    product = f32(maximum * value)
    scaled = f32(product / f32(1000.0))
    if not math.isfinite(scaled):
        raise NativeFormulaDomainError("Multiple scaled request is non-finite")
    return scaled, ceil_f32_to_i32(scaled)


def evaluate_stamina_recover_multiple(
    effect: Plan2NativeStaminaRecoverMultipleEffect | Mapping[str, object] | Plan2NativeStaminaRecoverMultipleProgram,
    runtime: StaminaRecoverFixRuntime | Plan2NativeStaminaRecoverMultipleSnapshot,
    *,
    simulate: bool = False,
) -> Plan2NativeStaminaRecoverMultipleEvaluation:
    """Evaluate one target effect with an immutable execution-time snapshot."""

    normalized_runtime = _normalize_runtime(runtime)
    if type(simulate) is not bool:
        raise TypeError("simulate must be bool")
    if isinstance(effect, Plan2NativeStaminaRecoverMultipleProgram):
        contract: Plan2NativeStaminaRecoverMultipleEffect | None = effect.effect
    elif isinstance(effect, Plan2NativeStaminaRecoverMultipleEffect):
        contract = effect
    elif isinstance(effect, Mapping):
        contract = try_parse_stamina_recover_multiple(effect)
    else:
        contract = None
    if contract is None:
        return _unresolved_evaluation(
            normalized_runtime,
            contract=None,
            simulate=simulate,
            reason="unknown or unsupported ExamStaminaRecoverMultiple shape",
            trace=("contract:unresolved",),
        )

    trace_prefix = (
        "multiple:read_current_stamina",
        "multiple:read_max_stamina",
        "multiple:read_effect_value1",
    )
    try:
        scaled, requested = _native_multiple_request(
            normalized_runtime.max_stamina,
            contract.effect_value1,
        )
    except (NativeFormulaDomainError, OverflowError, ValueError) as error:
        return _unresolved_evaluation(
            normalized_runtime,
            contract=contract,
            simulate=simulate,
            reason=f"Multiple float32/rounding domain is unproven: {error}",
            trace=(*trace_prefix, "multiple:float32-unresolved"),
            base_max_stamina=normalized_runtime.max_stamina,
        )

    fix_contract = StaminaRecoverFixContract(
        effect_id=f"{contract.effect_id}:fixed-request",
        effect_value1=requested,
    )
    fix_evaluation = evaluate_stamina_recover_fix(
        fix_contract,
        normalized_runtime,
        simulate=simulate,
    )
    trace = (
        *trace_prefix,
        "multiple:float32_max_stamina",
        "multiple:float32_effect_value1_permille",
        "multiple:float32_mul",
        "multiple:float32_div_1000_permille",
        "multiple:frintp_fcvtps_ceil",
        *fix_evaluation.trace,
    )
    if not fix_evaluation.executable:
        return _unresolved_evaluation(
            normalized_runtime,
            contract=contract,
            simulate=simulate,
            reason=fix_evaluation.reason or "shared recovery primitive unresolved",
            trace=trace,
            base_max_stamina=normalized_runtime.max_stamina,
            scaled_float32=scaled,
            requested_value=requested,
            fix_evaluation=fix_evaluation,
        )
    return Plan2NativeStaminaRecoverMultipleEvaluation(
        contract=contract,
        runtime_before=normalized_runtime,
        runtime_after=fix_evaluation.runtime_after,
        executable=True,
        simulated=simulate,
        reason=None,
        base_max_stamina=normalized_runtime.max_stamina,
        scaled_float32=scaled,
        requested_value=requested,
        adjusted_recovery_value=fix_evaluation.adjusted_recovery_value,
        calculated_recovery=fix_evaluation.calculated_recovery,
        predicted_stamina=fix_evaluation.predicted_stamina,
        difference=fix_evaluation.difference,
        fix_evaluation=fix_evaluation,
        trace=trace,
    )


# Keep the short name used by prior focused recovery adapters.
evaluate_multiple = evaluate_stamina_recover_multiple


def _normalize_origin(value: PlayOrigin | str) -> PlayOrigin:
    if isinstance(value, PlayOrigin):
        return value
    if value == "ordinary":
        return PlayOrigin.NORMAL
    try:
        return PlayOrigin(value)
    except (TypeError, ValueError) as error:
        raise Plan2NativeStaminaRecoverMultipleError(
            f"unsupported play origin: {value!r}"
        ) from error


@dataclass(frozen=True, slots=True)
class Plan2NativeStaminaRecoverMultipleExecution:
    program: Plan2NativeStaminaRecoverMultipleProgram
    play_origin: PlayOrigin
    evaluation: Plan2NativeStaminaRecoverMultipleEvaluation
    event_trace: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.program, Plan2NativeStaminaRecoverMultipleProgram):
            raise TypeError("program must be a target program")
        if not isinstance(self.play_origin, PlayOrigin):
            raise TypeError("play_origin must be PlayOrigin")
        if not isinstance(self.evaluation, Plan2NativeStaminaRecoverMultipleEvaluation):
            raise TypeError("evaluation must be a Multiple evaluation")
        object.__setattr__(self, "event_trace", tuple(self.event_trace))

    @property
    def executable(self) -> bool:
        return self.evaluation.executable

    @property
    def fail_closed(self) -> bool:
        return not self.executable

    @property
    def runtime_before(self) -> StaminaRecoverFixRuntime:
        return self.evaluation.runtime_before

    @property
    def runtime_after(self) -> StaminaRecoverFixRuntime:
        return self.evaluation.runtime_after

    @property
    def actual_stamina(self) -> int:
        return self.evaluation.actual_stamina

    @property
    def requested_value(self) -> int | None:
        return self.evaluation.requested_value

    @property
    def calculated_recovery(self) -> int | None:
        return self.evaluation.calculated_recovery

    @property
    def predicted_stamina(self) -> int | None:
        return self.evaluation.predicted_stamina

    @property
    def reason(self) -> str | None:
        return self.evaluation.reason


def _program_from_target(
    target: Plan2NativeStaminaRecoverMultipleProgram
    | Plan2NativeStaminaRecoverMultipleHandoffRow,
) -> Plan2NativeStaminaRecoverMultipleProgram:
    if isinstance(target, Plan2NativeStaminaRecoverMultipleProgram):
        return target
    if isinstance(target, Plan2NativeStaminaRecoverMultipleHandoffRow):
        return target.program
    raise TypeError("target must be a Multiple program or handoff row")


def execute_plan2_native_stamina_recover_multiple(
    target: Plan2NativeStaminaRecoverMultipleProgram
    | Plan2NativeStaminaRecoverMultipleHandoffRow,
    runtime: StaminaRecoverFixRuntime | Plan2NativeStaminaRecoverMultipleSnapshot,
    *,
    play_origin: PlayOrigin | str = PlayOrigin.NORMAL,
    simulate: bool = False,
) -> Plan2NativeStaminaRecoverMultipleExecution:
    """Execute one typed target slot without mutating the supplied snapshot."""

    program = _program_from_target(target)
    origin = _normalize_origin(play_origin)
    evaluation = evaluate_stamina_recover_multiple(
        program.effect,
        runtime,
        simulate=simulate,
    )
    event_trace = (
        f"card-play:{origin.value}:direct-slot:{program.target_slot_index}:"
        "ExamStaminaRecoverMultiple:execute",
        *evaluation.trace,
    )
    return Plan2NativeStaminaRecoverMultipleExecution(
        program=program,
        play_origin=origin,
        evaluation=evaluation,
        event_trace=event_trace,
    )


execute_stamina_recover_multiple = execute_plan2_native_stamina_recover_multiple
execute_plan2_native_catalog_stamina_recover_multiple = (
    execute_plan2_native_stamina_recover_multiple
)
execute_plan2_native_stamina_recover_multiple_handoff = (
    execute_plan2_native_stamina_recover_multiple
)


def stamina_recover_multiple_catalog_summary(
    source: Plan2NativeStaminaRecoverMultipleCompilation
    | Plan2NativeStaminaRecoverMultipleCatalog,
) -> dict[str, object]:
    compilation = (
        source if isinstance(source, Plan2NativeStaminaRecoverMultipleCompilation) else None
    )
    catalog = source.catalog if compilation is not None else source
    assert isinstance(catalog, Plan2NativeStaminaRecoverMultipleCatalog)
    return {
        "affected_versions": compilation.affected_version_count if compilation else catalog.affected_version_count,
        "compiled_versions": compilation.compiled_version_count if compilation else catalog.affected_version_count,
        "failed_versions": compilation.failed_version_count if compilation else 0,
        "target_effect_executable_versions": catalog.executable_version_count,
        "companion_blocked_versions": len(catalog.companion_blocked_refs),
        "whole_card_executable_versions": len(catalog.whole_card_executable_refs),
        "affected_refs": [
            f"{card_id}#{upgrade}" for card_id, upgrade in (
                compilation.affected_refs if compilation else catalog.affected_refs
            )
        ],
        "target_effect_executable_refs": [
            f"{card_id}#{upgrade}" for card_id, upgrade in catalog.executable_refs
        ],
        "companion_blocked_refs": [
            f"{card_id}#{upgrade}" for card_id, upgrade in catalog.companion_blocked_refs
        ],
        "whole_card_executable_refs": [
            f"{card_id}#{upgrade}" for card_id, upgrade in catalog.whole_card_executable_refs
        ],
        "companion_blocker_code_counts": catalog.companion_blocker_code_counts,
        "companion_family_version_counts": catalog.companion_family_version_counts,
    }


catalog_summary = stamina_recover_multiple_catalog_summary


def build_native_audit(
    source: Plan2NativeStaminaRecoverMultipleCompilation
    | Plan2NativeStaminaRecoverMultipleCatalog
    | None = None,
) -> dict[str, object]:
    """Build the bounded audit payload without touching central artifacts."""

    if source is None:
        source = compile_plan2_native_catalog_stamina_recover_multiple()
    compilation = (
        source if isinstance(source, Plan2NativeStaminaRecoverMultipleCompilation) else None
    )
    catalog = source.catalog if compilation is not None else source
    assert isinstance(catalog, Plan2NativeStaminaRecoverMultipleCatalog)
    handoff = build_plan2_native_stamina_recover_multiple_handoff(source)
    return {
        "schema_version": PLAN2_NATIVE_CATALOG_STAMINA_RECOVER_MULTIPLE_SCHEMA_VERSION,
        "adapter_id": STAMINA_RECOVER_MULTIPLE_ADAPTER_ID,
        "scope": "bounded Plan2 native catalog standalone",
        "implementation": {
            "module": "src/gkms_tool/plan2_native_catalog_stamina_recover_multiple.py",
            "reuses": ["src/gkms_tool/plan2_stamina_recover_fix.py"],
            "immutable_compile_execute_handoff": True,
            "central_catalog_modified": False,
            "horizon_modified": False,
            "core_or_formal_coverage_modified": False,
            "plan3_modified": False,
            "gui_or_controller_modified": False,
            "runtime_agent_calls": False,
        },
        "source": {
            "database": "var/master.sqlite3",
            "evidence_sources": {
                "android_executor_mapping": ANDROID_V323_EXECUTOR_MAPPING_SOURCE,
                "android_native": ANDROID_V323_NATIVE_SOURCE,
                "android_parameter_model": ANDROID_V323_PARAMETER_MODEL_SOURCE,
                "android_target_metadata": ANDROID_V323_TARGET_METADATA_SOURCE,
                "master": "var/master.sqlite3",
                "pc_metadata": PC_METADATA_SOURCE,
                "pc_native_scan": PC_NATIVE_SCAN_SOURCE,
            },
            "plan_type": PLAN2,
            "target_card_id": TARGET_CARD_ID,
            "target_upgrades": list(TARGET_UPGRADES),
            "target_effect_type": MULTIPLE_EFFECT_TYPE,
            "target_version_count": TARGET_VERSION_COUNT,
            "target_slot_index": 0,
            "uses_descriptions": False,
            "uses_ocr": False,
            "uses_approximation": False,
        },
        "master": {
            "trigger": (
                None
                if not catalog.triggers
                else catalog.triggers[0].to_dict()
            ),
            "effects": [value.to_dict() for value in catalog.effects],
            "versions": [value.to_dict() for value in catalog.programs],
        },
        "native_evidence": NATIVE_EVIDENCE,
        "accounting": {
            **stamina_recover_multiple_catalog_summary(source),
            "affected_occurrences": TARGET_VERSION_COUNT,
            "target_effect_executable": True,
            "whole_card_executable": False,
            "companion_blocker_occurrences": len(handoff.companion_blockers),
            "claim": (
                "All four target effect rows are executable as an exact standalone "
                "owner; trigger and companion rows remain blockers and no whole-card "
                "central unlock is claimed."
            ),
        },
        "central_handoff": {
            "adapter_id": handoff.adapter_id,
            "schema_version": handoff.schema_version,
            "affected_rows": handoff.affected_version_count,
            "target_effect_executable_rows": handoff.executable_version_count,
            "companion_blocked_rows": len(handoff.companion_blocked_refs),
            "whole_card_executable_rows": len(handoff.whole_card_executable_refs),
            "rows_are_exact_and_ordered": True,
            "whole_card_claim_requires_companion_clearance": True,
        },
        "blockers": {
            "per_version": [
                {
                    "version_ref": row.version_ref,
                    "target_executable": row.target_effect_executable,
                    "companion_blockers": [
                        value.to_dict() for value in row.companion_blockers
                    ],
                }
                for row in handoff.rows
            ],
            "hard_compile": [] if compilation is None else [
                value.to_dict() for value in compilation.blockers
            ],
        },
        "verification": {
            "focused_test": "tests/test_plan2_native_catalog_stamina_recover_multiple.py",
            "full_pytest_run": False,
            "security_probe_run": False,
            "hash_probe_run": False,
            "clock_or_timing_probe_run": False,
            "runtime_proxy_calls": 0,
        },
    }


__all__ = [
    "ANDROID_V323_EXECUTOR_MAPPING_SOURCE",
    "ANDROID_V323_NATIVE_SOURCE",
    "ANDROID_V323_PARAMETER_MODEL_SOURCE",
    "ANDROID_V323_TARGET_METADATA_SOURCE",
    "BLOCK_EFFECT_TYPE",
    "COST_UNKNOWN",
    "DEFAULT_AUDIT_PATH",
    "DEFAULT_DATABASE",
    "FIELD_REVIEW_UP",
    "INT32_MAX",
    "INT32_MIN",
    "MENTAL_SKILL",
    "MOVE_LOST",
    "MOVE_UNKNOWN",
    "MULTIPLE_EFFECT_TYPE",
    "NATIVE_EVIDENCE",
    "PC_METADATA_SOURCE",
    "PC_NATIVE_SCAN_SOURCE",
    "PHASE_EXAM_START_TURN",
    "PLAYABLE_VALUE_ADD_EFFECT_TYPE",
    "PLAY_ORIGINS",
    "PLAN2",
    "PLAN2_NATIVE_CATALOG_STAMINA_RECOVER_MULTIPLE_SCHEMA_VERSION",
    "Plan2NativeCatalogStaminaRecoverMultiple",
    "Plan2NativeCatalogStaminaRecoverMultipleBlocker",
    "Plan2NativeCatalogStaminaRecoverMultipleCompilation",
    "Plan2NativeCatalogStaminaRecoverMultipleContractError",
    "Plan2NativeStaminaRecoverMultipleBlocker",
    "Plan2NativeStaminaRecoverMultipleCardVersion",
    "Plan2NativeStaminaRecoverMultipleCatalog",
    "Plan2NativeStaminaRecoverMultipleCatalogCompilation",
    "Plan2NativeStaminaRecoverMultipleCentralHandoff",
    "Plan2NativeStaminaRecoverMultipleCompanionBlocker",
    "Plan2NativeStaminaRecoverMultipleCompiledProgram",
    "Plan2NativeStaminaRecoverMultipleCompilation",
    "Plan2NativeStaminaRecoverMultipleContractError",
    "Plan2NativeStaminaRecoverMultipleEffect",
    "Plan2NativeStaminaRecoverMultipleEffectSlot",
    "Plan2NativeStaminaRecoverMultipleError",
    "Plan2NativeStaminaRecoverMultipleEvaluation",
    "Plan2NativeStaminaRecoverMultipleExecution",
    "Plan2NativeStaminaRecoverMultipleHandoff",
    "Plan2NativeStaminaRecoverMultipleHandoffRow",
    "Plan2NativeStaminaRecoverMultipleProgram",
    "Plan2NativeStaminaRecoverMultipleRuntime",
    "Plan2NativeStaminaRecoverMultipleSnapshot",
    "Plan2NativeStaminaRecoverMultipleTrigger",
    "Plan2StaminaRecoverMultipleCentralHandoff",
    "Plan2StaminaRecoverMultiplePlayOrigin",
    "REVIEW_EFFECT_TYPE",
    "STAMINA_RECOVER_MULTIPLE_ADAPTER_ID",
    "STAMINA_RECOVER_MULTIPLE_CATALOG_SCHEMA_VERSION",
    "StaminaRecoverMultipleContractError",
    "StaminaRecoverMultipleRuntime",
    "SUPPORTED_PLAY_ORIGINS",
    "TARGET_CARD_ID",
    "TARGET_CARD_EFFECT_GROUP_IDS",
    "TARGET_CARD_STAMINA_BY_UPGRADE",
    "TARGET_EFFECT_GROUP_IDS",
    "TARGET_EFFECT_IDS_BY_UPGRADE",
    "TARGET_MULTIPLE_EFFECT_IDS_BY_UPGRADE",
    "TARGET_MULTIPLE_PERMILLE_BY_UPGRADE",
    "TARGET_REVIEW_THRESHOLD",
    "TARGET_TRIGGER_ID",
    "TARGET_UPGRADES",
    "TARGET_VERSION_COUNT",
    "build_native_audit",
    "build_plan2_native_stamina_recover_multiple_handoff",
    "build_plan2_stamina_recover_multiple_central_handoff",
    "build_stamina_recover_multiple_central_handoff",
    "catalog_summary",
    "compile_plan2_native_catalog_stamina_recover_multiple",
    "compile_plan2_stamina_recover_multiple_catalog",
    "compile_stamina_recover_multiple_catalog",
    "exact_plan2_native_stamina_recover_multiple_rows",
    "evaluate_multiple",
    "evaluate_stamina_recover_multiple",
    "execute_plan2_native_catalog_stamina_recover_multiple",
    "execute_plan2_native_stamina_recover_multiple",
    "execute_plan2_native_stamina_recover_multiple_handoff",
    "execute_stamina_recover_multiple",
    "load_plan2_native_catalog_stamina_recover_multiple",
    "load_plan2_stamina_recover_multiple_catalog",
    "load_stamina_recover_multiple_catalog",
    "stamina_recover_multiple_catalog_summary",
    "try_parse_stamina_recover_multiple",
]
