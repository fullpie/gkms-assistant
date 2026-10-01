"""Bounded typed Plan2 catalog adapter for two exact block families.

The adapter owns eight Common-plan card versions: four ``ExamBlockFix`` rows
and four ``ExamBlockRestriction`` rows.  It keeps the Master effect-link order
and reports every sibling effect as a companion blocker.  The two direct
effect operations are delegated to the already-audited leaf runtimes; this
module adds only the typed catalog boundary, status identity, and immutable
turn/lifecycle projection.

The supported direct surface is deliberately small:

* BlockFix uses the native ``max(-Block, value1)``/SetBlock tail and exposes
  the committed delta and zero floor.
* BlockRestriction uses the exact anti-debuff gate, fresh install, same-type
  merge, difference order, and passing-turn lifetime.
* Normal, forced, and extra play origins share the direct target executor.
  Card payment and sibling effects remain explicit companions.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field, replace
from enum import Enum
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Any, Final, Literal

from .master_db import DEFAULT_DATABASE
from . import plan2_block_restriction as _restriction
from . import plan2_stamina_up500_block_fix as _block_fix


SCHEMA_VERSION: Final = 1
PLAN2_NATIVE_CATALOG_BLOCK_FIX_RESTRICTION_SCHEMA_VERSION: Final = SCHEMA_VERSION
BLOCK_FIX_RESTRICTION_CATALOG_SCHEMA_VERSION: Final = SCHEMA_VERSION
ADAPTER_ID: Final = "plan2.native.catalog.block-fix-restriction"
BLOCK_FIX_RESTRICTION_ADAPTER_ID: Final = ADAPTER_ID

COMMON_PLAN: Final = "ProducePlanType_Common"
PLAN2: Final = COMMON_PLAN
MENTAL_SKILL_CATEGORY: Final = "ProduceCardCategory_MentalSkill"
MOVE_LOST: Final = "ProduceCardMovePositionType_Lost"
MOVE_UNKNOWN: Final = "ProduceCardMovePositionType_Unknown"
MOVE_TRIGGER_UNKNOWN: Final = "ProduceCardMoveEffectTriggerType_Unknown"
COST_UNKNOWN: Final = "ExamCostType_Unknown"
EFFECT_UNKNOWN: Final = "ProduceExamEffectType_Unknown"

BLOCK_FIX_FAMILY: Final = "block_fix"
BLOCK_RESTRICTION_FAMILY: Final = "block_restriction"
TARGET_FAMILIES: Final = (BLOCK_FIX_FAMILY, BLOCK_RESTRICTION_FAMILY)

BLOCK_FIX_CARD_ID: Final = "p_card-00-sup-3_161"
BLOCK_RESTRICTION_CARD_ID: Final = "p_card-00-men-2_014"
TARGET_CARD_IDS: Final = (BLOCK_FIX_CARD_ID, BLOCK_RESTRICTION_CARD_ID)
TARGET_UPGRADES: Final = (0, 1, 2, 3)
TARGET_VERSION_COUNT: Final = 8
BLOCK_FIX_TARGET_UPGRADES: Final = TARGET_UPGRADES
BLOCK_RESTRICTION_TARGET_UPGRADES: Final = TARGET_UPGRADES
BLOCK_FIX_TARGET_VERSION_COUNT: Final = 4
BLOCK_RESTRICTION_TARGET_VERSION_COUNT: Final = 4
EXPECTED_AFFECTED_VERSION_COUNT: Final = TARGET_VERSION_COUNT
EXPECTED_EXECUTABLE_VERSION_COUNT: Final = TARGET_VERSION_COUNT
EXPECTED_TARGET_REFS: Final = tuple(
    sorted(
        (card_id, upgrade)
        for card_id in TARGET_CARD_IDS
        for upgrade in TARGET_UPGRADES
    )
)
TARGET_REFS: Final = EXPECTED_TARGET_REFS

EXAM_BLOCK_FIX_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamBlockFix"
)
EXAM_BLOCK_RESTRICTION_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamBlockRestriction"
)
BLOCK_FIX_EFFECT_TYPE: Final = EXAM_BLOCK_FIX_EFFECT_TYPE
BLOCK_RESTRICTION_EFFECT_TYPE: Final = EXAM_BLOCK_RESTRICTION_EFFECT_TYPE
PLAYABLE_VALUE_ADD_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamPlayableValueAdd"
)

BLOCK_FIX_EFFECT_IDS: Final = (
    "e_effect-exam_block_fix-0005",
    "e_effect-exam_block_fix-0010",
    "e_effect-exam_block_fix-0012",
    "e_effect-exam_block_fix-0014",
)
BLOCK_FIX_VALUE_BY_UPGRADE: Final = MappingProxyType(
    {0: 5, 1: 10, 2: 12, 3: 14}
)
BLOCK_FIX_TARGET_EFFECT_IDS: Final = BLOCK_FIX_EFFECT_IDS
BLOCK_FIX_TARGET_EFFECT_ID_BY_UPGRADE: Final = BLOCK_FIX_VALUE_BY_UPGRADE
BLOCK_FIX_PLAYABLE_VALUE_ADD_EFFECT_ID: Final = (
    "e_effect-exam_playable_value_add-01"
)

BLOCK_RESTRICTION_EFFECT_ID: Final = "e_effect-exam_block_restriction-02"
BLOCK_RESTRICTION_TARGET_EFFECT_ID: Final = BLOCK_RESTRICTION_EFFECT_ID
BLOCK_RESTRICTION_BLOCK_EFFECT_IDS: Final = (
    "e_effect-exam_block-0011",
    "e_effect-exam_block-0013",
    "e_effect-exam_block-0015",
    "e_effect-exam_block-0017",
)
BLOCK_RESTRICTION_STAMINA_EFFECT_IDS: Final = (
    "e_effect-exam_stamina_consumption_down-03",
    "e_effect-exam_stamina_consumption_down-04",
    "e_effect-exam_stamina_consumption_down-04",
    "e_effect-exam_stamina_consumption_down-04",
)

BLOCK_FIX_EFFECT_GROUP_ID: Final = "effect_group-visible-exam_block-000"
BLOCK_RESTRICTION_EFFECT_GROUP_ID: Final = (
    "effect_group-visible-exam_block_restriction-000"
)
BLOCK_DIFFERENCE_STATUS_EFFECT_TYPE: Final = 3
BLOCK_RESTRICTION_EFFECT_TYPE_VALUE: Final = 18
BLOCK_RESTRICTION_TURN: Final = 2
LOWER_BOUND: Final = 0

# Combined aliases keep the standalone surface discoverable without adding a
# second runtime family or a central-catalog dependency.
BLOCK_FIX_RESTRICTION_FAMILY: Final = "block_fix_restriction"
BLOCK_FIX_RESTRICTION_EFFECT_ID: Final = BLOCK_RESTRICTION_EFFECT_ID
BLOCK_FIX_RESTRICTION_EFFECT_TYPE: Final = frozenset(
    (BLOCK_FIX_EFFECT_TYPE, BLOCK_RESTRICTION_EFFECT_TYPE)
)

PLAY_ORIGINS: Final = ("normal", "forced", "extra")
SUPPORTED_PLAY_ORIGINS: Final = PLAY_ORIGINS
INT32_MIN: Final = -(2**31)
INT32_MAX: Final = 2**31 - 1

EXPECTED_ORDERED_EFFECT_IDS: Final[dict[tuple[str, int], tuple[str, ...]]] = {
    **{
        (BLOCK_FIX_CARD_ID, upgrade): (
            BLOCK_FIX_EFFECT_IDS[upgrade],
            BLOCK_FIX_PLAYABLE_VALUE_ADD_EFFECT_ID,
        )
        for upgrade in TARGET_UPGRADES
    },
    **{
        (BLOCK_RESTRICTION_CARD_ID, upgrade): (
            BLOCK_RESTRICTION_BLOCK_EFFECT_IDS[upgrade],
            BLOCK_RESTRICTION_EFFECT_ID,
            BLOCK_RESTRICTION_STAMINA_EFFECT_IDS[upgrade],
        )
        for upgrade in TARGET_UPGRADES
    },
}
TARGET_EFFECT_ID_BY_REF: Final[dict[tuple[str, int], str]] = {
    **{
        (BLOCK_FIX_CARD_ID, upgrade): BLOCK_FIX_EFFECT_IDS[upgrade]
        for upgrade in TARGET_UPGRADES
    },
    **{
        (BLOCK_RESTRICTION_CARD_ID, upgrade): BLOCK_RESTRICTION_EFFECT_ID
        for upgrade in TARGET_UPGRADES
    },
}
TARGET_SLOT_INDEX_BY_REF: Final[dict[tuple[str, int], int]] = {
    **{(BLOCK_FIX_CARD_ID, upgrade): 0 for upgrade in TARGET_UPGRADES},
    **{(BLOCK_RESTRICTION_CARD_ID, upgrade): 1 for upgrade in TARGET_UPGRADES},
}

PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
DEFAULT_AUDIT_PATH: Final = (
    PROJECT_ROOT
    / "var"
    / "coverage"
    / "plan2_native_catalog_block_fix_restriction_audit.json"
)


class Plan2NativeBlockFixRestrictionError(ValueError):
    """A Master, native, or immutable caller shape is outside this leaf."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class Plan2NativeBlockFixRestrictionInputError(
    Plan2NativeBlockFixRestrictionError
):
    """A runtime, program, origin, or handoff input cannot be resolved."""


CatalogContractError = Plan2NativeBlockFixRestrictionError
InputContractError = Plan2NativeBlockFixRestrictionInputError


class Plan2BlockFixRestrictionFamily(str, Enum):
    BLOCK_FIX = BLOCK_FIX_FAMILY
    BLOCK_RESTRICTION = BLOCK_RESTRICTION_FAMILY


class Plan2BlockFixRestrictionPlayOrigin(str, Enum):
    NORMAL = "normal"
    FORCED = "forced"
    EXTRA = "extra"


PlayOrigin = Plan2BlockFixRestrictionPlayOrigin
BlockFixRestrictionPlayOrigin = Plan2BlockFixRestrictionPlayOrigin


def _i32(value: object, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or type(value) is not int:
        raise Plan2NativeBlockFixRestrictionError("signed-int32-required", label)
    if value < INT32_MIN or value > INT32_MAX:
        raise Plan2NativeBlockFixRestrictionError("signed-int32-out-of-range", label)
    if minimum is not None and value < minimum:
        raise Plan2NativeBlockFixRestrictionError("nonnegative-required", label)
    return value


def _nonnegative_i32(value: object, label: str) -> int:
    return _i32(value, label, minimum=0)


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value):
        raise Plan2NativeBlockFixRestrictionError("text-shape", label)
    return value


def _bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise Plan2NativeBlockFixRestrictionError("bool-shape", label)
    return value


def _json_object(value: object, label: str) -> Mapping[str, object]:
    if type(value) is not str:
        raise Plan2NativeBlockFixRestrictionError("json-object-shape", label)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise Plan2NativeBlockFixRestrictionError("json-object-shape", label) from error
    if not isinstance(parsed, Mapping):
        raise Plan2NativeBlockFixRestrictionError("json-object-shape", label)
    return parsed


def _json_array(value: object, label: str) -> tuple[object, ...]:
    if type(value) is not str:
        raise Plan2NativeBlockFixRestrictionError("json-array-shape", label)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise Plan2NativeBlockFixRestrictionError("json-array-shape", label) from error
    if not isinstance(parsed, list):
        raise Plan2NativeBlockFixRestrictionError("json-array-shape", label)
    return tuple(parsed)


def _string_tuple(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise Plan2NativeBlockFixRestrictionError("string-array-shape", label)
    result = tuple(value)
    if any(type(item) is not str or (not allow_empty and not item) for item in result):
        raise Plan2NativeBlockFixRestrictionError("string-array-shape", label)
    return result


def _raw_equal(raw: Mapping[str, object], key: str, expected: object, label: str) -> None:
    if key not in raw or type(raw[key]) is not type(expected) or raw[key] != expected:
        raise Plan2NativeBlockFixRestrictionError("master-raw-drift", f"{label}.{key}")


def _normalize_origin(value: object) -> str | None:
    if isinstance(value, Plan2BlockFixRestrictionPlayOrigin):
        return value.value
    if type(value) is str:
        if value in PLAY_ORIGINS:
            return value
        if value == "ordinary":
            return "normal"
    return None


@dataclass(frozen=True, slots=True)
class Plan2NativeBlockFixRestrictionNativeEvidence:
    """Pinned native call-order facts carried by the leaf catalog."""

    android_version: str
    block_fix_executor: str
    block_restriction_executor: str
    calculate_add_block: str
    add_block_fix: str
    set_block: str
    block_difference_status_type: int
    restriction_effect_type_value: int
    restriction_turn_rule: str
    play_origins: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.android_version, "native.android_version")
        for name in (
            "block_fix_executor",
            "block_restriction_executor",
            "calculate_add_block",
            "add_block_fix",
            "set_block",
            "restriction_turn_rule",
        ):
            _text(getattr(self, name), f"native.{name}")
        _i32(self.block_difference_status_type, "native.block_difference_status_type", minimum=0)
        _i32(self.restriction_effect_type_value, "native.restriction_effect_type_value", minimum=0)
        origins = _string_tuple(self.play_origins, "native.play_origins")
        if origins != PLAY_ORIGINS:
            raise Plan2NativeBlockFixRestrictionError("native-origin-shape")
        object.__setattr__(self, "play_origins", origins)

    def to_dict(self) -> dict[str, object]:
        return {
            "androidVersion": self.android_version,
            "blockFixExecutor": self.block_fix_executor,
            "blockRestrictionExecutor": self.block_restriction_executor,
            "calculateAddBlock": self.calculate_add_block,
            "addBlockFix": self.add_block_fix,
            "setBlock": self.set_block,
            "blockDifferenceStatusType": self.block_difference_status_type,
            "restrictionEffectTypeValue": self.restriction_effect_type_value,
            "restrictionTurnRule": self.restriction_turn_rule,
            "playOrigins": list(self.play_origins),
        }


NATIVE_EVIDENCE: Final = Plan2NativeBlockFixRestrictionNativeEvidence(
    android_version="Android v3.2.3",
    block_fix_executor="Campus.InGame.Exam.BlockFixEffectExecutor@0x7E72A40",
    block_restriction_executor=(
        "Campus.InGame.Exam.BlockRestrictionEffectExecutor@0x7E73328"
    ),
    calculate_add_block="ExamEffectUtility.CalculateAddBlock@0x7E5E6E4",
    add_block_fix="ExamParameterModel.AddBlockFix@0x7E5EA94",
    set_block="ExamParameterModel.SetBlock@0x7EBB524",
    block_difference_status_type=BLOCK_DIFFERENCE_STATUS_EFFECT_TYPE,
    restriction_effect_type_value=BLOCK_RESTRICTION_EFFECT_TYPE_VALUE,
    restriction_turn_rule=(
        "fresh status skips its first turn-start spend; passing status spends "
        "one turn; zero removes the status"
    ),
    play_origins=PLAY_ORIGINS,
)


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeBlockFixRestrictionEffect:
    """Normalized immutable Master effect row used by target and companions."""

    effect_id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str = ""
    chain_effect_id: str = ""
    effect_group_ids: tuple[str, ...] = ()
    produce_description_count: int = 0
    customize_description_count: int = 0

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect.effect_id")
        _text(self.effect_type, "effect.effect_type")
        for name in ("value1", "value2", "effect_count", "effect_turn"):
            _i32(getattr(self, name), f"effect.{name}")
        for name in ("produce_description_count", "customize_description_count"):
            _nonnegative_i32(getattr(self, name), f"effect.{name}")
        _text(self.status_enchant_id, "effect.status_enchant_id", empty=True)
        _text(self.chain_effect_id, "effect.chain_effect_id", empty=True)
        groups = _string_tuple(self.effect_group_ids, "effect.effect_group_ids")
        if not groups:
            raise Plan2NativeBlockFixRestrictionError("effect-group-missing", self.effect_id)
        object.__setattr__(self, "effect_group_ids", groups)

    @property
    def id(self) -> str:
        return self.effect_id

    @property
    def count(self) -> int:
        return self.effect_count

    @property
    def turn(self) -> int:
        return self.effect_turn

    def to_dict(self) -> dict[str, object]:
        return {
            "effectId": self.effect_id,
            "effectType": self.effect_type,
            "value1": self.value1,
            "value2": self.value2,
            "count": self.effect_count,
            "turn": self.effect_turn,
            "statusEnchantId": self.status_enchant_id,
            "chainEffectId": self.chain_effect_id,
            "effectGroupIds": list(self.effect_group_ids),
            "produceDescriptionCount": self.produce_description_count,
            "customizeDescriptionCount": self.customize_description_count,
        }


EffectRow = Plan2NativeBlockFixRestrictionEffect
Plan2NativeBlockFixRestrictionEffectRow = Plan2NativeBlockFixRestrictionEffect


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeBlockFixRestrictionEffectSlot:
    slot_index: int
    effect: Plan2NativeBlockFixRestrictionEffect
    trigger_id: str = ""
    hide_icon: bool = False
    is_once_play_effect: bool = False

    def __post_init__(self) -> None:
        _nonnegative_i32(self.slot_index, "slot.slot_index")
        if not isinstance(self.effect, Plan2NativeBlockFixRestrictionEffect):
            raise TypeError("slot.effect must be typed")
        _text(self.trigger_id, "slot.trigger_id", empty=True)
        _bool(self.hide_icon, "slot.hide_icon")
        _bool(self.is_once_play_effect, "slot.is_once_play_effect")

    @property
    def effect_id(self) -> str:
        return self.effect.effect_id

    @property
    def effect_type(self) -> str:
        return self.effect.effect_type

    @property
    def value1(self) -> int:
        return self.effect.value1

    @property
    def value2(self) -> int:
        return self.effect.value2

    @property
    def effect_count(self) -> int:
        return self.effect.effect_count

    @property
    def effect_turn(self) -> int:
        return self.effect.effect_turn

    def to_dict(self) -> dict[str, object]:
        return {
            "slotIndex": self.slot_index,
            "effectId": self.effect_id,
            "effectType": self.effect_type,
            "triggerId": self.trigger_id,
            "hideIcon": self.hide_icon,
            "isOncePlayEffect": self.is_once_play_effect,
            "value1": self.value1,
            "value2": self.value2,
            "count": self.effect_count,
            "turn": self.effect_turn,
            "effectGroupIds": list(self.effect.effect_group_ids),
        }


EffectSlot = Plan2NativeBlockFixRestrictionEffectSlot


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeBlockFixRestrictionBlocker:
    card_id: str
    upgrade: int
    slot_index: int
    code: str
    family: str
    detail: str
    effect_id: str = ""
    companion: bool = True

    def __post_init__(self) -> None:
        _text(self.card_id, "blocker.card_id")
        _nonnegative_i32(self.upgrade, "blocker.upgrade")
        _i32(self.slot_index, "blocker.slot_index")
        if self.slot_index < -1:
            raise Plan2NativeBlockFixRestrictionError("blocker-slot-shape")
        _text(self.code, "blocker.code")
        _text(self.family, "blocker.family")
        _text(self.detail, "blocker.detail")
        _text(self.effect_id, "blocker.effect_id", empty=True)
        _bool(self.companion, "blocker.companion")
        if self.slot_index >= 0 and not self.effect_id:
            raise Plan2NativeBlockFixRestrictionError("blocker-effect-missing")

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def version_ref(self) -> str:
        return f"{self.card_id}#{self.upgrade}"

    def to_dict(self) -> dict[str, object]:
        return {
            "cardId": self.card_id,
            "upgrade": self.upgrade,
            "versionRef": self.version_ref,
            "slotIndex": self.slot_index,
            "code": self.code,
            "family": self.family,
            "detail": self.detail,
            "effectId": self.effect_id,
            "companion": self.companion,
        }


CompanionBlocker = Plan2NativeBlockFixRestrictionBlocker
Plan2NativeBlockFixRestrictionCompanionBlocker = Plan2NativeBlockFixRestrictionBlocker


def _target_shape_reason(
    family: str | Plan2BlockFixRestrictionFamily,
    ref: tuple[str, int],
    effect: object,
) -> str | None:
    try:
        family_value = Plan2BlockFixRestrictionFamily(family).value
    except (TypeError, ValueError):
        return "unknown-target-family"
    if not isinstance(effect, Plan2NativeBlockFixRestrictionEffect):
        return "target-effect-row-type"
    expected_id = TARGET_EFFECT_ID_BY_REF.get(ref)
    if expected_id is None:
        return "unknown-target-ref"
    if effect.effect_id != expected_id:
        return "target-effect-id"
    if family_value == BLOCK_FIX_FAMILY:
        upgrade = ref[1]
        expected = (
            EXAM_BLOCK_FIX_EFFECT_TYPE,
            BLOCK_FIX_VALUE_BY_UPGRADE.get(upgrade),
            0,
            0,
            0,
            "",
            "",
            (BLOCK_FIX_EFFECT_GROUP_ID,),
        )
    else:
        expected = (
            EXAM_BLOCK_RESTRICTION_EFFECT_TYPE,
            0,
            0,
            0,
            BLOCK_RESTRICTION_TURN,
            "",
            "",
            (BLOCK_RESTRICTION_EFFECT_GROUP_ID,),
        )
    actual = (
        effect.effect_type,
        effect.value1,
        effect.value2,
        effect.effect_count,
        effect.effect_turn,
        effect.status_enchant_id,
        effect.chain_effect_id,
        effect.effect_group_ids,
    )
    if actual != expected:
        if effect.effect_type != expected[0]:
            return "target-effect-type"
        if effect.effect_group_ids != expected[7]:
            return "target-effect-group-shape"
        if effect.status_enchant_id or effect.chain_effect_id:
            return "target-effect-nested-shape"
        if family_value == BLOCK_RESTRICTION_FAMILY and effect.effect_turn != BLOCK_RESTRICTION_TURN:
            return "target-effect-turn-shape"
        return "target-effect-value-shape"
    return None


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeBlockFixRestrictionVersion:
    """One exact card version containing one direct target effect."""

    family: Plan2BlockFixRestrictionFamily | str
    card_id: str
    upgrade: int
    name: str
    plan_type: str
    category: str
    stamina: int
    force_stamina: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    move_position_type: str
    effect_slots: tuple[Plan2NativeBlockFixRestrictionEffectSlot, ...]
    target_slot_index: int
    companion_blockers: tuple[Plan2NativeBlockFixRestrictionBlocker, ...] = ()

    def __post_init__(self) -> None:
        try:
            family = Plan2BlockFixRestrictionFamily(self.family)
        except (TypeError, ValueError) as error:
            raise Plan2NativeBlockFixRestrictionError("version-family-shape") from error
        object.__setattr__(self, "family", family)
        _text(self.card_id, "version.card_id")
        _nonnegative_i32(self.upgrade, "version.upgrade")
        _text(self.name, "version.name")
        _text(self.plan_type, "version.plan_type")
        _text(self.category, "version.category")
        _nonnegative_i32(self.stamina, "version.stamina")
        _nonnegative_i32(self.force_stamina, "version.force_stamina")
        _text(self.cost_type, "version.cost_type")
        _nonnegative_i32(self.cost_value, "version.cost_value")
        _text(self.play_trigger_id, "version.play_trigger_id", empty=True)
        _text(self.move_position_type, "version.move_position_type")
        slots = tuple(self.effect_slots)
        if not slots or any(not isinstance(item, Plan2NativeBlockFixRestrictionEffectSlot) for item in slots):
            raise TypeError("version.effect_slots must contain typed slots")
        if tuple(item.slot_index for item in slots) != tuple(range(len(slots))):
            raise Plan2NativeBlockFixRestrictionError("card-effect-order", self.version_ref)
        object.__setattr__(self, "effect_slots", slots)
        _nonnegative_i32(self.target_slot_index, "version.target_slot_index")
        if self.target_slot_index >= len(slots):
            raise Plan2NativeBlockFixRestrictionError("target-slot-range", self.version_ref)
        expected_ref = (self.card_id, self.upgrade)
        expected_card_id = (
            BLOCK_FIX_CARD_ID
            if self.family is Plan2BlockFixRestrictionFamily.BLOCK_FIX
            else BLOCK_RESTRICTION_CARD_ID
        )
        if self.card_id != expected_card_id:
            raise Plan2NativeBlockFixRestrictionError("version-family-card-shape", self.version_ref)
        if TARGET_EFFECT_ID_BY_REF.get(expected_ref) != slots[self.target_slot_index].effect_id:
            raise Plan2NativeBlockFixRestrictionError("target-slot-id", self.version_ref)
        # Keep construction immutable but let the execution boundary report a
        # typed target-shape mismatch as non-executable.  The read-only Master
        # compiler already rejects malformed rows; this second boundary must
        # fail closed for a caller-supplied, structurally typed mutation.
        blockers = tuple(self.companion_blockers)
        if any(not isinstance(item, Plan2NativeBlockFixRestrictionBlocker) for item in blockers):
            raise TypeError("version.companion_blockers must contain typed blockers")
        if any(item.ref != expected_ref or not item.companion for item in blockers):
            raise Plan2NativeBlockFixRestrictionError("blocker-version-shape", self.version_ref)
        object.__setattr__(self, "companion_blockers", blockers)

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def version_ref(self) -> str:
        return f"{self.card_id}#{self.upgrade}"

    @property
    def family_name(self) -> str:
        return self.family.value

    @property
    def target_family(self) -> str:
        return self.family.value

    @property
    def version_ref_text(self) -> str:
        return self.version_ref

    @property
    def card(self) -> "Plan2NativeBlockFixRestrictionVersion":
        return self

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return tuple(item.effect_id for item in self.effect_slots)

    @property
    def target_slot(self) -> Plan2NativeBlockFixRestrictionEffectSlot:
        return self.effect_slots[self.target_slot_index]

    @property
    def target_effect(self) -> Plan2NativeBlockFixRestrictionEffect:
        return self.target_slot.effect

    @property
    def effect(self) -> Plan2NativeBlockFixRestrictionEffect:
        return self.target_effect

    @property
    def target_effect_id(self) -> str:
        return self.target_effect.effect_id

    @property
    def effect_id(self) -> str:
        return self.target_effect_id

    @property
    def slot_index(self) -> int:
        return self.target_slot_index

    @property
    def target_effect_type(self) -> str:
        return self.target_effect.effect_type

    @property
    def value1(self) -> int:
        return self.target_effect.value1

    @property
    def value2(self) -> int:
        return self.target_effect.value2

    @property
    def effect_count(self) -> int:
        return self.target_effect.effect_count

    @property
    def count(self) -> int:
        return self.effect_count

    @property
    def effect_turn(self) -> int:
        return self.target_effect.effect_turn

    @property
    def turn(self) -> int:
        return self.effect_turn

    @property
    def prior_effect_ids(self) -> tuple[str, ...]:
        return self.ordered_effect_ids[: self.target_slot_index]

    @property
    def companion_effect_ids(self) -> tuple[str, ...]:
        return tuple(
            item.effect_id
            for item in self.effect_slots
            if item.slot_index != self.target_slot_index
        )

    @property
    def supported_play_origins(self) -> tuple[str, ...]:
        return PLAY_ORIGINS

    @property
    def play_origins(self) -> tuple[str, ...]:
        return self.supported_play_origins

    @property
    def target_effect_executable(self) -> bool:
        return _target_shape_reason(self.family, self.ref, self.target_effect) is None

    @property
    def target_executable(self) -> bool:
        return self.target_effect_executable

    @property
    def executable(self) -> bool:
        return self.target_effect_executable

    @property
    def whole_card_executable(self) -> bool:
        return self.target_effect_executable and not self.companion_blockers

    @property
    def companion_blocker_families(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.family for item in self.companion_blockers))

    @property
    def lifetime(self) -> str:
        return "finite" if self.family is Plan2BlockFixRestrictionFamily.BLOCK_RESTRICTION else "instant"

    def to_dict(self) -> dict[str, object]:
        return {
            "family": self.family.value,
            "cardId": self.card_id,
            "upgrade": self.upgrade,
            "versionRef": self.version_ref,
            "name": self.name,
            "planType": self.plan_type,
            "category": self.category,
            "stamina": self.stamina,
            "forceStamina": self.force_stamina,
            "costType": self.cost_type,
            "costValue": self.cost_value,
            "playTriggerId": self.play_trigger_id,
            "movePositionType": self.move_position_type,
            "targetSlotIndex": self.target_slot_index,
            "targetEffect": self.target_effect.to_dict(),
            "orderedEffectIds": list(self.ordered_effect_ids),
            "priorEffectIds": list(self.prior_effect_ids),
            "companionEffectIds": list(self.companion_effect_ids),
            "supportedPlayOrigins": list(self.supported_play_origins),
            "lifetime": self.lifetime,
            "targetEffectExecutable": self.target_effect_executable,
            "wholeCardExecutable": self.whole_card_executable,
            "companionBlockers": [item.to_dict() for item in self.companion_blockers],
        }


Plan2NativeBlockFixRestrictionProgram = Plan2NativeBlockFixRestrictionVersion
Plan2NativeBlockFixRestrictionCardVersion = Plan2NativeBlockFixRestrictionVersion
Plan2NativeBlockFixRestrictionCompiledProgram = Plan2NativeBlockFixRestrictionVersion


@dataclass(frozen=True, slots=True)
class Plan2NativeBlockFixRestrictionCatalog:
    database: str
    programs: tuple[Plan2NativeBlockFixRestrictionVersion, ...]
    native: Plan2NativeBlockFixRestrictionNativeEvidence = NATIVE_EVIDENCE

    def __post_init__(self) -> None:
        _text(self.database, "catalog.database")
        programs = tuple(self.programs)
        if any(not isinstance(item, Plan2NativeBlockFixRestrictionVersion) for item in programs):
            raise TypeError("catalog.programs must contain typed versions")
        refs = tuple(item.ref for item in programs)
        if refs != tuple(sorted(refs)) or len(refs) != len(set(refs)):
            raise Plan2NativeBlockFixRestrictionError("catalog-version-order")
        if not isinstance(self.native, Plan2NativeBlockFixRestrictionNativeEvidence):
            raise TypeError("catalog.native must be native evidence")
        object.__setattr__(self, "programs", programs)

    @property
    def versions(self) -> tuple[Plan2NativeBlockFixRestrictionVersion, ...]:
        return self.programs

    @property
    def affected_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(item.ref for item in self.programs)

    @property
    def affected_version_count(self) -> int:
        return len(self.affected_refs)

    @property
    def executable_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(item.ref for item in self.programs if item.target_effect_executable)

    @property
    def target_effect_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.executable_refs

    @property
    def executable_version_count(self) -> int:
        return len(self.executable_refs)

    @property
    def target_effect_executable(self) -> bool:
        return self.affected_version_count == TARGET_VERSION_COUNT and self.executable_version_count == TARGET_VERSION_COUNT

    @property
    def companion_blockers(self) -> tuple[Plan2NativeBlockFixRestrictionBlocker, ...]:
        return tuple(item for program in self.programs for item in program.companion_blockers)

    @property
    def companion_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(program.ref for program in self.programs if program.companion_blockers)

    @property
    def whole_card_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(program.ref for program in self.programs if program.whole_card_executable)

    @property
    def whole_card_executable(self) -> bool:
        return len(self.whole_card_executable_refs) == self.affected_version_count

    @property
    def companion_blocker_code_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.code for item in self.companion_blockers).items()))

    @property
    def companion_family_version_counts(self) -> dict[str, int]:
        refs: dict[str, set[tuple[str, int]]] = {}
        for blocker in self.companion_blockers:
            refs.setdefault(blocker.family, set()).add(blocker.ref)
        return {family: len(values) for family, values in sorted(refs.items())}

    def get(self, card_id: str, upgrade: int) -> Plan2NativeBlockFixRestrictionVersion | None:
        return next((item for item in self.programs if item.ref == (card_id, upgrade)), None)

    def resolve(self, card_id: str, upgrade: int) -> Plan2NativeBlockFixRestrictionVersion:
        program = self.get(card_id, upgrade)
        if program is None:
            raise KeyError(f"uncompiled target version: {card_id}#{upgrade}")
        return program

    def family_versions(self, family: str | Plan2BlockFixRestrictionFamily) -> tuple[Plan2NativeBlockFixRestrictionVersion, ...]:
        target = Plan2BlockFixRestrictionFamily(family)
        return tuple(item for item in self.programs if item.family is target)

    def to_dict(self) -> dict[str, object]:
        return {
            "adapterId": ADAPTER_ID,
            "schemaVersion": SCHEMA_VERSION,
            "database": self.database,
            "affectedVersionCount": self.affected_version_count,
            "executableVersionCount": self.executable_version_count,
            "targetEffectExecutable": self.target_effect_executable,
            "affectedRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.affected_refs
            ],
            "companionBlockedRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.companion_blocked_refs
            ],
            "wholeCardExecutableRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.whole_card_executable_refs
            ],
            "native": self.native.to_dict(),
            "programs": [item.to_dict() for item in self.programs],
        }


Plan2NativeCatalogBlockFixRestriction = Plan2NativeBlockFixRestrictionCatalog


@dataclass(frozen=True, slots=True)
class Plan2NativeBlockFixRestrictionCompilation:
    schema_version: int
    database: str
    affected_refs: tuple[tuple[str, int], ...]
    occurrence_count: int
    catalog: Plan2NativeBlockFixRestrictionCatalog
    blockers: tuple[Plan2NativeBlockFixRestrictionBlocker, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise Plan2NativeBlockFixRestrictionError("unsupported-schema-version")
        _text(self.database, "compilation.database")
        refs = tuple(self.affected_refs)
        if refs != tuple(sorted(refs)) or len(refs) != len(set(refs)):
            raise Plan2NativeBlockFixRestrictionError("affected-ref-order")
        _nonnegative_i32(self.occurrence_count, "compilation.occurrence_count")
        if not isinstance(self.catalog, Plan2NativeBlockFixRestrictionCatalog):
            raise TypeError("compilation.catalog must be typed")
        if any(item.ref not in refs for item in self.catalog.programs):
            raise Plan2NativeBlockFixRestrictionError("compiled-ref-outside-target")
        blockers = tuple(self.blockers)
        if any(not isinstance(item, Plan2NativeBlockFixRestrictionBlocker) for item in blockers):
            raise TypeError("compilation.blockers must be typed")
        if any(item.companion for item in blockers):
            raise Plan2NativeBlockFixRestrictionError("hard-blocker-marked-companion")
        object.__setattr__(self, "affected_refs", refs)
        object.__setattr__(self, "blockers", blockers)

    @property
    def compiled_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.affected_refs

    @property
    def compiled_version_count(self) -> int:
        return len(self.compiled_refs)

    @property
    def affected_version_count(self) -> int:
        return len(self.affected_refs)

    @property
    def failed_refs(self) -> tuple[tuple[str, int], ...]:
        compiled = set(self.compiled_refs)
        return tuple(ref for ref in self.affected_refs if ref not in compiled)

    @property
    def failed_version_count(self) -> int:
        return len(self.failed_refs)

    @property
    def executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.executable_refs

    @property
    def target_effect_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.executable_refs

    @property
    def executable_version_count(self) -> int:
        return self.catalog.executable_version_count

    @property
    def target_effect_executable(self) -> bool:
        return self.affected_version_count == TARGET_VERSION_COUNT and self.compiled_version_count == TARGET_VERSION_COUNT and self.executable_version_count == TARGET_VERSION_COUNT and not self.blockers

    @property
    def fully_compiled(self) -> bool:
        return not self.blockers and not self.failed_refs

    @property
    def companion_blockers(self) -> tuple[Plan2NativeBlockFixRestrictionBlocker, ...]:
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

    def blockers_for(self, ref: tuple[str, int]) -> tuple[Plan2NativeBlockFixRestrictionBlocker, ...]:
        return tuple(item for item in self.blockers if item.ref == ref)

    def to_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "database": self.database,
            "affectedRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.affected_refs
            ],
            "occurrenceCount": self.occurrence_count,
            "compiledRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.compiled_refs
            ],
            "failedRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.failed_refs
            ],
            "fullyCompiled": self.fully_compiled,
            "targetEffectExecutable": self.target_effect_executable,
            "blockers": [item.to_dict() for item in self.blockers],
            "companionBlockers": [item.to_dict() for item in self.companion_blockers],
            "catalog": self.catalog.to_dict(),
        }


Plan2NativeBlockFixRestrictionCatalogCompilation = Plan2NativeBlockFixRestrictionCompilation


_RAW_EFFECT_KEYS: Final = frozenset(
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
_LINK_KEYS: Final = frozenset(
    {"produceExamTriggerId", "produceExamEffectId", "hideIcon", "isOncePlayEffect"}
)


def _load_effect_row(connection: sqlite3.Connection, effect_id: str) -> Plan2NativeBlockFixRestrictionEffect:
    row = connection.execute("SELECT * FROM effect WHERE id = ?", (effect_id,)).fetchone()
    if row is None:
        raise Plan2NativeBlockFixRestrictionError("missing-effect-row", effect_id)
    raw = _json_object(row["raw_json"], f"{effect_id}.raw_json")
    if set(raw) != _RAW_EFFECT_KEYS:
        raise Plan2NativeBlockFixRestrictionError("effect-raw-key-shape", effect_id)
    normalized = {
        "id": effect_id,
        "effectType": row["effect_type"],
        "effectValue1": row["value1"],
        "effectValue2": row["value2"],
        "effectCount": row["effect_count"],
        "effectTurn": row["effect_turn"],
        "produceExamStatusEnchantId": row["status_enchant_id"],
        "chainProduceExamEffectId": row["chain_effect_id"],
    }
    for key, value in normalized.items():
        _raw_equal(raw, key, value, effect_id)
    _raw_equal(raw, "targetProduceCardId", "", effect_id)
    _raw_equal(raw, "targetUpgradeCount", 0, effect_id)
    _raw_equal(raw, "targetExamEffectType", EFFECT_UNKNOWN, effect_id)
    _raw_equal(raw, "produceCardSearchId", "", effect_id)
    _raw_equal(raw, "movePositionType", MOVE_UNKNOWN, effect_id)
    _raw_equal(raw, "pickRangeType", "ProducePickRangeType_Unknown", effect_id)
    _raw_equal(raw, "pickCountReferenceProduceCardSearchId", "", effect_id)
    _raw_equal(raw, "pickCountType", "ProducePickCountType_Unknown", effect_id)
    _raw_equal(raw, "pickCountMin", 0, effect_id)
    _raw_equal(raw, "pickCountMax", 0, effect_id)
    _raw_equal(raw, "produceCardSearchId2", "", effect_id)
    _raw_equal(raw, "pickRangeType2", "ProducePickRangeType_Unknown", effect_id)
    _raw_equal(raw, "pickCountReferenceProduceCardSearchId2", "", effect_id)
    _raw_equal(raw, "pickCountType2", "ProducePickCountType_Unknown", effect_id)
    _raw_equal(raw, "pickCountMin2", 0, effect_id)
    _raw_equal(raw, "pickCountMax2", 0, effect_id)
    _raw_equal(raw, "chainProduceExamEffectIds", [], effect_id)
    _raw_equal(raw, "produceCardStatusEnchantId", "", effect_id)
    _raw_equal(raw, "produceCardGrowEffectIds", [], effect_id)
    groups = _string_tuple(raw.get("effectGroupIds"), f"{effect_id}.effectGroupIds")
    if (
        not isinstance(raw.get("produceDescriptions"), list)
        or any(not isinstance(item, Mapping) for item in raw.get("produceDescriptions", ()))
        or not isinstance(raw.get("customizeProduceDescriptions"), list)
        or any(not isinstance(item, Mapping) for item in raw.get("customizeProduceDescriptions", ()))
    ):
        raise Plan2NativeBlockFixRestrictionError("effect-description-shape", effect_id)
    return Plan2NativeBlockFixRestrictionEffect(
        effect_id=effect_id,
        effect_type=_text(row["effect_type"], f"{effect_id}.effect_type"),
        value1=_i32(row["value1"], f"{effect_id}.value1"),
        value2=_i32(row["value2"], f"{effect_id}.value2"),
        effect_count=_i32(row["effect_count"], f"{effect_id}.effect_count"),
        effect_turn=_i32(row["effect_turn"], f"{effect_id}.effect_turn"),
        status_enchant_id=_text(row["status_enchant_id"], f"{effect_id}.status_enchant_id", empty=True),
        chain_effect_id=_text(row["chain_effect_id"], f"{effect_id}.chain_effect_id", empty=True),
        effect_group_ids=groups,
        produce_description_count=len(raw["produceDescriptions"]),
        customize_description_count=len(raw["customizeProduceDescriptions"]),
    )


def _load_card_raw(row: sqlite3.Row, links: tuple[object, ...], ref: tuple[str, int], family: str) -> Mapping[str, object]:
    raw = _json_object(row["raw_json"], f"{ref[0]}#{ref[1]}.raw_json")
    expected_force = 1 if family == BLOCK_FIX_FAMILY else 0
    expected = {
        "id": ref[0],
        "upgradeCount": ref[1],
        "planType": COMMON_PLAN,
        "category": MENTAL_SKILL_CATEGORY,
        "stamina": 0,
        "forceStamina": expected_force,
        "costType": COST_UNKNOWN,
        "costValue": 0,
        "playProduceExamTriggerId": "",
        "playEffects": list(links),
        "playMovePositionType": MOVE_LOST,
        "moveEffectTriggerType": MOVE_TRIGGER_UNKNOWN,
        "moveProduceExamEffectIds": [],
        "isEndTurnLost": False,
        "produceCardStatusEnchantId": "",
    }
    for key, value in expected.items():
        _raw_equal(raw, key, value, f"{ref[0]}#{ref[1]}")
    return raw


def _build_companion_blockers(
    ref: tuple[str, int],
    family: str,
    slots: tuple[Plan2NativeBlockFixRestrictionEffectSlot, ...],
    target_slot: int,
) -> tuple[Plan2NativeBlockFixRestrictionBlocker, ...]:
    blockers: list[Plan2NativeBlockFixRestrictionBlocker] = []
    for slot in slots:
        if slot.slot_index == target_slot:
            continue
        if family == BLOCK_FIX_FAMILY:
            sibling_family = "playable_value_add"
        elif slot.slot_index == 0:
            sibling_family = "block"
        else:
            sibling_family = "stamina_consumption_down"
        blockers.append(
            Plan2NativeBlockFixRestrictionBlocker(
                card_id=ref[0],
                upgrade=ref[1],
                slot_index=slot.slot_index,
                code="companion-adapter-pending",
                family=sibling_family,
                detail=(
                    f"ordered companion slot {slot.slot_index}: "
                    f"{slot.effect_id}:{slot.effect_type} is retained but not "
                    "executed by this target adapter"
                ),
                effect_id=slot.effect_id,
            )
        )
    return tuple(blockers)


def _compile_version(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    family: str,
) -> Plan2NativeBlockFixRestrictionVersion:
    card_id = _text(row["id"], "card.id")
    upgrade = _nonnegative_i32(row["upgrade_count"], f"{card_id}.upgrade")
    ref = (card_id, upgrade)
    expected_ids = EXPECTED_ORDERED_EFFECT_IDS.get(ref)
    if expected_ids is None or family not in TARGET_FAMILIES:
        raise Plan2NativeBlockFixRestrictionError("target-ref-shape", f"{card_id}#{upgrade}")
    if row["plan_type"] != COMMON_PLAN:
        raise Plan2NativeBlockFixRestrictionError("card-plan-type", f"{card_id}#{upgrade}")
    if row["category"] != MENTAL_SKILL_CATEGORY:
        raise Plan2NativeBlockFixRestrictionError("card-category", f"{card_id}#{upgrade}")
    if _nonnegative_i32(row["stamina"], f"{card_id}#{upgrade}.stamina") != 0:
        raise Plan2NativeBlockFixRestrictionError("card-stamina", f"{card_id}#{upgrade}")
    if row["cost_type"] != COST_UNKNOWN or _nonnegative_i32(row["cost_value"], f"{card_id}#{upgrade}.cost_value") != 0:
        raise Plan2NativeBlockFixRestrictionError("card-cost", f"{card_id}#{upgrade}")
    if row["play_trigger_id"] != "" or row["move_position_type"] != MOVE_LOST:
        raise Plan2NativeBlockFixRestrictionError("card-boundary-shape", f"{card_id}#{upgrade}")
    links = _json_array(row["play_effects_json"], f"{card_id}#{upgrade}.play_effects_json")
    _load_card_raw(row, links, ref, family)
    if tuple(
        item.get("produceExamEffectId")
        for item in links
        if isinstance(item, Mapping)
    ) != expected_ids:
        raise Plan2NativeBlockFixRestrictionError("card-effect-order", f"{card_id}#{upgrade}")
    slots: list[Plan2NativeBlockFixRestrictionEffectSlot] = []
    for index, link in enumerate(links):
        if not isinstance(link, Mapping) or set(link) != _LINK_KEYS:
            raise Plan2NativeBlockFixRestrictionError("card-effect-link-shape", f"{card_id}#{upgrade}:{index}")
        effect_id = _text(link.get("produceExamEffectId"), f"{card_id}#{upgrade}:{index}.effect_id")
        trigger_id = _text(link.get("produceExamTriggerId"), f"{card_id}#{upgrade}:{index}.trigger_id", empty=True)
        expected_trigger = (
            _block_fix.TRIGGER_ID
            if family == BLOCK_FIX_FAMILY and index == 1
            else ""
        )
        if trigger_id != expected_trigger:
            raise Plan2NativeBlockFixRestrictionError("card-effect-trigger", f"{card_id}#{upgrade}:{index}")
        if _bool(link.get("hideIcon"), f"{card_id}#{upgrade}:{index}.hideIcon") or _bool(link.get("isOncePlayEffect"), f"{card_id}#{upgrade}:{index}.isOncePlayEffect"):
            raise Plan2NativeBlockFixRestrictionError("card-effect-link-flags", f"{card_id}#{upgrade}:{index}")
        slots.append(
            Plan2NativeBlockFixRestrictionEffectSlot(
                slot_index=index,
                effect=_load_effect_row(connection, effect_id),
                trigger_id=trigger_id,
                hide_icon=False,
                is_once_play_effect=False,
            )
        )
    typed_slots = tuple(slots)
    target_slot = TARGET_SLOT_INDEX_BY_REF[ref]
    target_effect = typed_slots[target_slot].effect
    reason = _target_shape_reason(family, ref, target_effect)
    if reason is not None:
        raise Plan2NativeBlockFixRestrictionError(reason, f"{card_id}#{upgrade}")
    return Plan2NativeBlockFixRestrictionVersion(
        family=family,
        card_id=card_id,
        upgrade=upgrade,
        name=_text(row["name"], f"{card_id}#{upgrade}.name"),
        plan_type=_text(row["plan_type"], f"{card_id}#{upgrade}.plan_type"),
        category=_text(row["category"], f"{card_id}#{upgrade}.category"),
        stamina=_nonnegative_i32(row["stamina"], f"{card_id}#{upgrade}.stamina"),
        force_stamina=_nonnegative_i32(_load_card_raw(row, links, ref, family)["forceStamina"], f"{card_id}#{upgrade}.forceStamina"),
        cost_type=_text(row["cost_type"], f"{card_id}#{upgrade}.cost_type"),
        cost_value=_nonnegative_i32(row["cost_value"], f"{card_id}#{upgrade}.cost_value"),
        play_trigger_id=_text(row["play_trigger_id"], f"{card_id}#{upgrade}.play_trigger_id", empty=True),
        move_position_type=_text(row["move_position_type"], f"{card_id}#{upgrade}.move_position_type"),
        effect_slots=typed_slots,
        target_slot_index=target_slot,
        companion_blockers=_build_companion_blockers(ref, family, typed_slots, target_slot),
    )


def _expected_family(ref: tuple[str, int]) -> str:
    if ref[0] == BLOCK_FIX_CARD_ID:
        return BLOCK_FIX_FAMILY
    if ref[0] == BLOCK_RESTRICTION_CARD_ID:
        return BLOCK_RESTRICTION_FAMILY
    raise KeyError(ref)


def compile_plan2_native_catalog_block_fix_restriction(
    database: str | Path = DEFAULT_DATABASE,
) -> Plan2NativeBlockFixRestrictionCompilation:
    """Compile exactly the eight current target versions read-only."""

    database_path = Path(database).resolve()
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    hard_blockers: list[Plan2NativeBlockFixRestrictionBlocker] = []
    programs: list[Plan2NativeBlockFixRestrictionVersion] = []
    occurrence_count = 0
    with closing(sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        for ref in EXPECTED_TARGET_REFS:
            family = _expected_family(ref)
            row = connection.execute(
                "SELECT * FROM card WHERE id = ? AND upgrade_count = ?",
                ref,
            ).fetchone()
            if row is None:
                hard_blockers.append(
                    Plan2NativeBlockFixRestrictionBlocker(
                        card_id=ref[0],
                        upgrade=ref[1],
                        slot_index=-1,
                        code="target-card-missing",
                        family="target_card",
                        detail="exact Master card version is missing",
                        effect_id=TARGET_EFFECT_ID_BY_REF[ref],
                        companion=False,
                    )
                )
                continue
            try:
                program = _compile_version(connection, row, family)
            except (KeyError, IndexError, sqlite3.Error, TypeError, ValueError) as error:
                hard_blockers.append(
                    Plan2NativeBlockFixRestrictionBlocker(
                        card_id=ref[0],
                        upgrade=ref[1],
                        slot_index=-1,
                        code=getattr(error, "code", "target-compile-failed"),
                        family="target_effect",
                        detail=getattr(error, "detail", str(error)) or str(error),
                        effect_id=TARGET_EFFECT_ID_BY_REF[ref],
                        companion=False,
                    )
                )
                continue
            occurrence_count += 1
            programs.append(program)
        target_effect_ids = (*BLOCK_FIX_EFFECT_IDS, BLOCK_RESTRICTION_EFFECT_ID)
        effect_placeholders = ", ".join("?" for _ in target_effect_ids)
        card_placeholders = ", ".join("?" for _ in TARGET_CARD_IDS)
        discovered = connection.execute(
            f"""
            SELECT COUNT(*) FROM card AS c
            JOIN json_each(c.play_effects_json) AS link
              ON json_extract(link.value, '$.produceExamEffectId') IN ({effect_placeholders})
            WHERE c.id IN ({card_placeholders}) AND c.upgrade_count IN (0, 1, 2, 3)
            """,
            (*target_effect_ids, *TARGET_CARD_IDS),
        ).fetchone()
        if discovered is not None:
            discovered_count = _nonnegative_i32(discovered[0], "inventory.occurrence_count")
            occurrence_count = discovered_count
            if discovered_count != TARGET_VERSION_COUNT:
                for ref in EXPECTED_TARGET_REFS:
                    if ref not in {item.ref for item in programs} and not any(item.ref == ref for item in hard_blockers):
                        hard_blockers.append(
                            Plan2NativeBlockFixRestrictionBlocker(
                                card_id=ref[0],
                                upgrade=ref[1],
                                slot_index=-1,
                                code="target-inventory-drift",
                                family="target_inventory",
                                detail=f"expected {TARGET_VERSION_COUNT} occurrences, got {discovered_count}",
                                effect_id=TARGET_EFFECT_ID_BY_REF[ref],
                                companion=False,
                            )
                        )
    catalog = Plan2NativeBlockFixRestrictionCatalog(
        database=str(database_path),
        programs=tuple(sorted(programs, key=lambda item: item.ref)),
        native=NATIVE_EVIDENCE,
    )
    return Plan2NativeBlockFixRestrictionCompilation(
        schema_version=SCHEMA_VERSION,
        database=str(database_path),
        affected_refs=EXPECTED_TARGET_REFS,
        occurrence_count=occurrence_count,
        catalog=catalog,
        blockers=tuple(sorted(hard_blockers, key=lambda item: item.ref)),
    )


compile_block_fix_restriction_catalog = compile_plan2_native_catalog_block_fix_restriction
compile_plan2_block_fix_restriction_catalog = compile_plan2_native_catalog_block_fix_restriction
compile_plan2_native_block_fix_restriction_catalog = compile_plan2_native_catalog_block_fix_restriction
compile_native_catalog = compile_plan2_native_catalog_block_fix_restriction


def load_plan2_native_catalog_block_fix_restriction(
    database: str | Path = DEFAULT_DATABASE,
) -> Plan2NativeBlockFixRestrictionCatalog:
    compilation = compile_plan2_native_catalog_block_fix_restriction(database)
    if compilation.blockers or compilation.failed_refs:
        first = compilation.blockers[0] if compilation.blockers else None
        if first is not None:
            raise Plan2NativeBlockFixRestrictionError(first.code, f"{first.version_ref}:{first.detail}")
        raise Plan2NativeBlockFixRestrictionError("incomplete-target-catalog", repr(compilation.failed_refs))
    return compilation.catalog


load_block_fix_restriction_catalog = load_plan2_native_catalog_block_fix_restriction
load_plan2_block_fix_restriction_catalog = load_plan2_native_catalog_block_fix_restriction
load_plan2_native_block_fix_restriction_catalog = load_plan2_native_catalog_block_fix_restriction
load_native_catalog = load_plan2_native_catalog_block_fix_restriction


@dataclass(frozen=True, slots=True)
class Plan2NativeBlockFixRestrictionHandoffRow:
    version_ref: str
    family: str
    card_id: str
    upgrade: int
    target_effect_id: str
    target_effect_type: str
    target_slot_index: int
    value1: int
    value2: int
    count: int
    turn: int
    lifetime: str
    ordered_effect_ids: tuple[str, ...]
    prior_effect_ids: tuple[str, ...]
    supported_play_origins: tuple[str, ...]
    target_effect_executable: bool
    whole_card_executable: bool
    companion_blockers: tuple[Plan2NativeBlockFixRestrictionBlocker, ...]

    def __post_init__(self) -> None:
        _text(self.version_ref, "handoff.version_ref")
        try:
            family = Plan2BlockFixRestrictionFamily(self.family)
        except (TypeError, ValueError) as error:
            raise Plan2NativeBlockFixRestrictionError("handoff-family-shape") from error
        object.__setattr__(self, "family", family.value)
        _text(self.card_id, "handoff.card_id")
        _nonnegative_i32(self.upgrade, "handoff.upgrade")
        _text(self.target_effect_id, "handoff.target_effect_id")
        _text(self.target_effect_type, "handoff.target_effect_type")
        _nonnegative_i32(self.target_slot_index, "handoff.target_slot_index")
        for name in ("value1", "value2", "count", "turn"):
            _i32(getattr(self, name), f"handoff.{name}")
        if self.lifetime not in {"instant", "finite"}:
            raise Plan2NativeBlockFixRestrictionError("handoff-lifetime-shape")
        object.__setattr__(self, "ordered_effect_ids", _string_tuple(self.ordered_effect_ids, "handoff.ordered_effect_ids"))
        object.__setattr__(self, "prior_effect_ids", _string_tuple(self.prior_effect_ids, "handoff.prior_effect_ids", allow_empty=True))
        origins = _string_tuple(self.supported_play_origins, "handoff.supported_play_origins")
        if origins != PLAY_ORIGINS:
            raise Plan2NativeBlockFixRestrictionError("handoff-origin-shape")
        object.__setattr__(self, "supported_play_origins", origins)
        if self.version_ref != f"{self.card_id}#{self.upgrade}":
            raise Plan2NativeBlockFixRestrictionError("handoff-version-ref-shape")
        ref = (self.card_id, self.upgrade)
        expected_family = _expected_family(ref) if ref in TARGET_EFFECT_ID_BY_REF else None
        if expected_family != self.family:
            raise Plan2NativeBlockFixRestrictionError("handoff-family-card-shape", self.version_ref)
        if self.target_effect_id != TARGET_EFFECT_ID_BY_REF.get(ref):
            raise Plan2NativeBlockFixRestrictionError("handoff-target-effect-id", self.version_ref)
        expected_slot = TARGET_SLOT_INDEX_BY_REF.get(ref)
        if expected_slot is None or self.target_slot_index != expected_slot:
            raise Plan2NativeBlockFixRestrictionError("handoff-target-slot", self.version_ref)
        if self.ordered_effect_ids != EXPECTED_ORDERED_EFFECT_IDS.get(ref):
            raise Plan2NativeBlockFixRestrictionError("handoff-effect-order", self.version_ref)
        if self.prior_effect_ids != self.ordered_effect_ids[: self.target_slot_index]:
            raise Plan2NativeBlockFixRestrictionError("handoff-prior-effect-order", self.version_ref)
        expected_type = (
            BLOCK_FIX_EFFECT_TYPE
            if self.family == BLOCK_FIX_FAMILY
            else BLOCK_RESTRICTION_EFFECT_TYPE
        )
        if self.target_effect_type != expected_type:
            raise Plan2NativeBlockFixRestrictionError("handoff-target-effect-type", self.version_ref)
        if self.target_effect_executable:
            expected_values = (
                (BLOCK_FIX_VALUE_BY_UPGRADE[self.upgrade], 0, 0, 0)
                if self.family == BLOCK_FIX_FAMILY
                else (0, 0, 0, BLOCK_RESTRICTION_TURN)
            )
            if (self.value1, self.value2, self.count, self.turn) != expected_values:
                raise Plan2NativeBlockFixRestrictionError("handoff-target-value-shape", self.version_ref)
        expected_lifetime = "instant" if self.family == BLOCK_FIX_FAMILY else "finite"
        if self.lifetime != expected_lifetime:
            raise Plan2NativeBlockFixRestrictionError("handoff-lifetime-shape", self.version_ref)
        _bool(self.target_effect_executable, "handoff.target_effect_executable")
        _bool(self.whole_card_executable, "handoff.whole_card_executable")
        blockers = tuple(self.companion_blockers)
        if any(not isinstance(item, Plan2NativeBlockFixRestrictionBlocker) for item in blockers):
            raise TypeError("handoff.companion_blockers must be typed")
        if any(item.ref != ref or not item.companion for item in blockers):
            raise Plan2NativeBlockFixRestrictionError("handoff-blocker-version-shape", self.version_ref)
        if self.whole_card_executable and blockers:
            raise Plan2NativeBlockFixRestrictionError("handoff-whole-card-blocked", self.version_ref)
        object.__setattr__(self, "companion_blockers", blockers)

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def target_family(self) -> str:
        return self.family

    @property
    def version_ref_text(self) -> str:
        return self.version_ref

    @property
    def effect_id(self) -> str:
        return self.target_effect_id

    @property
    def slot_index(self) -> int:
        return self.target_slot_index

    @property
    def target_executable(self) -> bool:
        return self.target_effect_executable

    def to_dict(self) -> dict[str, object]:
        return {
            "versionRef": self.version_ref,
            "family": self.family,
            "cardId": self.card_id,
            "upgrade": self.upgrade,
            "targetEffectId": self.target_effect_id,
            "targetEffectType": self.target_effect_type,
            "targetSlotIndex": self.target_slot_index,
            "value1": self.value1,
            "value2": self.value2,
            "count": self.count,
            "turn": self.turn,
            "lifetime": self.lifetime,
            "orderedEffectIds": list(self.ordered_effect_ids),
            "priorEffectIds": list(self.prior_effect_ids),
            "supportedPlayOrigins": list(self.supported_play_origins),
            "targetEffectExecutable": self.target_effect_executable,
            "wholeCardExecutable": self.whole_card_executable,
            "companionBlockers": [item.to_dict() for item in self.companion_blockers],
        }


Plan2NativeBlockFixRestrictionCentralHandoffRow = Plan2NativeBlockFixRestrictionHandoffRow


@dataclass(frozen=True, slots=True)
class Plan2NativeBlockFixRestrictionCentralHandoff:
    adapter_id: str
    schema_version: int
    affected_refs: tuple[tuple[str, int], ...]
    executable_refs: tuple[tuple[str, int], ...]
    rows: tuple[Plan2NativeBlockFixRestrictionHandoffRow, ...]

    def __post_init__(self) -> None:
        _text(self.adapter_id, "handoff.adapter_id")
        if self.schema_version != SCHEMA_VERSION:
            raise Plan2NativeBlockFixRestrictionError("handoff-schema-version")
        affected = tuple(self.affected_refs)
        executable = tuple(self.executable_refs)
        rows = tuple(self.rows)
        if affected != tuple(sorted(affected)) or len(affected) != len(set(affected)):
            raise Plan2NativeBlockFixRestrictionError("handoff-affected-order")
        if executable != tuple(sorted(executable)) or set(executable) - set(affected):
            raise Plan2NativeBlockFixRestrictionError("handoff-executable-order")
        if tuple(row.ref for row in rows) != affected:
            raise Plan2NativeBlockFixRestrictionError("handoff-row-order")
        if any(not isinstance(row, Plan2NativeBlockFixRestrictionHandoffRow) for row in rows):
            raise TypeError("handoff.rows must contain typed rows")
        object.__setattr__(self, "affected_refs", affected)
        object.__setattr__(self, "executable_refs", executable)
        object.__setattr__(self, "rows", rows)

    @property
    def affected_version_count(self) -> int:
        return len(self.affected_refs)

    @property
    def executable_version_count(self) -> int:
        return len(self.executable_refs)

    @property
    def target_effect_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.executable_refs

    @property
    def target_effect_executable(self) -> bool:
        return len(self.executable_refs) == self.affected_version_count

    @property
    def whole_card_executable(self) -> bool:
        return len(self.whole_card_executable_refs) == self.affected_version_count

    @property
    def companion_blockers(self) -> tuple[Plan2NativeBlockFixRestrictionBlocker, ...]:
        return tuple(item for row in self.rows for item in row.companion_blockers)

    @property
    def companion_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(row.ref for row in self.rows if row.companion_blockers)

    @property
    def whole_card_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(row.ref for row in self.rows if row.whole_card_executable)

    @property
    def companion_blocker_code_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.code for item in self.companion_blockers).items()))

    def to_dict(self) -> dict[str, object]:
        return {
            "adapterId": self.adapter_id,
            "schemaVersion": self.schema_version,
            "affectedVersionCount": self.affected_version_count,
            "executableVersionCount": self.executable_version_count,
            "affectedRefs": [{"cardId": card_id, "upgrade": upgrade} for card_id, upgrade in self.affected_refs],
            "executableRefs": [{"cardId": card_id, "upgrade": upgrade} for card_id, upgrade in self.executable_refs],
            "companionBlockedRefs": [{"cardId": card_id, "upgrade": upgrade} for card_id, upgrade in self.companion_blocked_refs],
            "wholeCardExecutableRefs": [{"cardId": card_id, "upgrade": upgrade} for card_id, upgrade in self.whole_card_executable_refs],
            "rows": [row.to_dict() for row in self.rows],
        }


Plan2NativeBlockFixRestrictionHandoff = Plan2NativeBlockFixRestrictionCentralHandoff


def build_plan2_native_block_fix_restriction_handoff(
    source: Plan2NativeBlockFixRestrictionCompilation | Plan2NativeBlockFixRestrictionCatalog,
) -> Plan2NativeBlockFixRestrictionCentralHandoff:
    if isinstance(source, Plan2NativeBlockFixRestrictionCompilation):
        if source.blockers or source.failed_refs:
            raise Plan2NativeBlockFixRestrictionError("handoff-source-incomplete")
        catalog = source.catalog
    elif isinstance(source, Plan2NativeBlockFixRestrictionCatalog):
        catalog = source
    else:
        raise TypeError("source must be a typed catalog or compilation")
    rows = tuple(
        Plan2NativeBlockFixRestrictionHandoffRow(
            version_ref=program.version_ref,
            family=program.family.value,
            card_id=program.card_id,
            upgrade=program.upgrade,
            target_effect_id=program.target_effect_id,
            target_effect_type=program.target_effect_type,
            target_slot_index=program.target_slot_index,
            value1=program.value1,
            value2=program.value2,
            count=program.count,
            turn=program.turn,
            lifetime=program.lifetime,
            ordered_effect_ids=program.ordered_effect_ids,
            prior_effect_ids=program.prior_effect_ids,
            supported_play_origins=program.supported_play_origins,
            target_effect_executable=program.target_effect_executable,
            whole_card_executable=program.whole_card_executable,
            companion_blockers=program.companion_blockers,
        )
        for program in catalog.programs
    )
    return Plan2NativeBlockFixRestrictionCentralHandoff(
        adapter_id=ADAPTER_ID,
        schema_version=SCHEMA_VERSION,
        affected_refs=catalog.affected_refs,
        executable_refs=catalog.executable_refs,
        rows=rows,
    )


build_plan2_block_fix_restriction_central_handoff = build_plan2_native_block_fix_restriction_handoff
build_block_fix_restriction_central_handoff = build_plan2_native_block_fix_restriction_handoff
build_native_handoff = build_plan2_native_block_fix_restriction_handoff


def exact_plan2_native_block_fix_restriction_rows(
    source: Plan2NativeBlockFixRestrictionCompilation | Plan2NativeBlockFixRestrictionCatalog,
) -> tuple[dict[str, object], ...]:
    return tuple(row.to_dict() for row in build_plan2_native_block_fix_restriction_handoff(source).rows)


def block_fix_restriction_catalog_summary(
    source: Plan2NativeBlockFixRestrictionCompilation | Plan2NativeBlockFixRestrictionCatalog,
) -> dict[str, object]:
    compilation = source if isinstance(source, Plan2NativeBlockFixRestrictionCompilation) else None
    handoff = build_plan2_native_block_fix_restriction_handoff(source)
    return {
        "affected_versions": handoff.affected_version_count,
        "compiled_versions": compilation.compiled_version_count if compilation else handoff.executable_version_count,
        "failed_versions": compilation.failed_version_count if compilation else 0,
        "target_effect_executable_versions": handoff.executable_version_count,
        "companion_blocked_versions": len(handoff.companion_blocked_refs),
        "whole_card_executable_versions": len(handoff.whole_card_executable_refs),
        "companion_blocker_occurrences": len(handoff.companion_blockers),
        "companion_blocker_code_counts": handoff.companion_blocker_code_counts,
        "companion_blocked_refs": [f"{card_id}#{upgrade}" for card_id, upgrade in handoff.companion_blocked_refs],
    }


catalog_summary = block_fix_restriction_catalog_summary


@dataclass(frozen=True, slots=True)
class Plan2NativeBlockFixRestrictionRuntime:
    """Minimal immutable state touched by both target executors."""

    block: int = 0
    block_consumption_sum_count: int = 0
    restriction: _restriction.BlockRestrictionRuntime = field(
        default_factory=_restriction.BlockRestrictionRuntime
    )
    restriction_status_uid: int | None = None
    next_status_uid: int = 1
    anti_debuff: _restriction.AntiDebuffRuntime = field(
        default_factory=_restriction.AntiDebuffRuntime
    )

    def __post_init__(self) -> None:
        _i32(self.block, "runtime.block")
        _i32(self.block_consumption_sum_count, "runtime.block_consumption_sum_count")
        if not isinstance(self.restriction, _restriction.BlockRestrictionRuntime):
            raise TypeError("runtime.restriction must use the exact restriction runtime")
        if self.restriction_status_uid is not None:
            _nonnegative_i32(self.restriction_status_uid, "runtime.restriction_status_uid")
            if self.restriction_status_uid < 1:
                raise Plan2NativeBlockFixRestrictionError("status-uid-shape")
            if not self.restriction.status_present:
                raise Plan2NativeBlockFixRestrictionError("absent-status-has-uid")
        elif self.restriction.status_present:
            raise Plan2NativeBlockFixRestrictionError("present-status-missing-uid")
        _nonnegative_i32(self.next_status_uid, "runtime.next_status_uid")
        if self.next_status_uid < 1:
            raise Plan2NativeBlockFixRestrictionError("next-status-uid-shape")
        if self.restriction_status_uid is not None and self.next_status_uid <= self.restriction_status_uid:
            raise Plan2NativeBlockFixRestrictionError("next-status-uid-order")
        if not isinstance(self.anti_debuff, _restriction.AntiDebuffRuntime):
            raise TypeError("runtime.anti_debuff must use the exact gate runtime")

    @property
    def block_restriction(self) -> _restriction.BlockRestrictionRuntime:
        return self.restriction

    @property
    def restriction_runtime(self) -> _restriction.BlockRestrictionRuntime:
        return self.restriction

    @property
    def restriction_turns(self) -> int:
        return self.restriction.turns

    @property
    def is_passing_turn_start(self) -> bool:
        return self.restriction.is_passing_turn_start

    @property
    def status_present(self) -> bool:
        return self.restriction.status_present

    @property
    def status_uid(self) -> int | None:
        return self.restriction_status_uid

    @property
    def active_status_uids(self) -> tuple[int, ...]:
        return () if self.restriction_status_uid is None else (self.restriction_status_uid,)

    @property
    def listeners(self) -> tuple["Plan2NativeBlockRestrictionStatus", ...]:
        if self.restriction_status_uid is None:
            return ()
        return (
            Plan2NativeBlockRestrictionStatus(
                status_uid=self.restriction_status_uid,
                turns=self.restriction.turns,
                is_passing_turn_start=self.restriction.is_passing_turn_start,
            ),
        )


Plan2NativeBlockFixRestrictionState = Plan2NativeBlockFixRestrictionRuntime
Plan2BlockFixRestrictionRuntime = Plan2NativeBlockFixRestrictionRuntime
# The combined state above owns the adapter boundary.  This alias remains the
# exact reused leaf runtime for callers that need only the restriction field.
Plan2NativeBlockRestrictionRuntime = _restriction.BlockRestrictionRuntime
Plan2BlockRestrictionRuntime = _restriction.BlockRestrictionRuntime


@dataclass(frozen=True, slots=True)
class Plan2NativeBlockRestrictionStatus:
    status_uid: int
    turns: int
    is_passing_turn_start: bool = False

    def __post_init__(self) -> None:
        _nonnegative_i32(self.status_uid, "status.status_uid")
        if self.status_uid < 1:
            raise Plan2NativeBlockFixRestrictionError("status-uid-shape")
        if type(self.turns) is not int or self.turns < 0 or self.turns > INT32_MAX:
            raise Plan2NativeBlockFixRestrictionError("status-turn-shape")
        _bool(self.is_passing_turn_start, "status.is_passing_turn_start")
        if self.turns == 0 and self.is_passing_turn_start:
            raise Plan2NativeBlockFixRestrictionError("absent-status-passing")

    @property
    def uid(self) -> int:
        return self.status_uid

    @property
    def native(self) -> _restriction.BlockRestrictionRuntime:
        return _restriction.BlockRestrictionRuntime(self.turns, self.is_passing_turn_start)

    @property
    def status_present(self) -> bool:
        return self.turns > 0


@dataclass(frozen=True, slots=True)
class Plan2NativeBlockFixExecution:
    before: object
    after: object
    program: Plan2NativeBlockFixRestrictionVersion
    play_origin: str
    resolved: bool
    committed: bool
    actual_delta: int
    requested_value: int
    lower_bound: int
    reason: str = ""
    operations: tuple[str, ...] = ()
    native: object | None = None

    @property
    def executable(self) -> bool:
        return self.resolved and self.committed

    @property
    def target_effect_executable(self) -> bool:
        return self.resolved

    @property
    def state_unchanged(self) -> bool:
        return self.after is self.before

    @property
    def block_before(self) -> int:
        return getattr(self.before, "block")

    @property
    def block_after(self) -> int:
        return getattr(self.after, "block")

    @property
    def block_difference(self) -> int:
        return self.actual_delta

    @property
    def actual_block_delta(self) -> int:
        return self.actual_delta

    @property
    def lower_bound_applied(self) -> bool:
        return self.block_before + self.requested_value < self.lower_bound


@dataclass(frozen=True, slots=True)
class Plan2NativeBlockRestrictionInstallTransition:
    before: Plan2NativeBlockFixRestrictionRuntime
    after: Plan2NativeBlockFixRestrictionRuntime
    program: Plan2NativeBlockFixRestrictionVersion
    play_origin: str
    native: object | None
    resolved: bool
    committed: bool
    installed: bool
    operation: str
    created_status_uid: int | None
    merged_status_uid: int | None
    difference_before: int
    difference_after: int
    reason: str = ""
    operations: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return self.resolved and self.committed

    @property
    def target_effect_executable(self) -> bool:
        return self.resolved

    @property
    def status_uid(self) -> int | None:
        return self.created_status_uid or self.merged_status_uid

    @property
    def installed_status_uid(self) -> int | None:
        return self.status_uid

    @property
    def stack_order_after(self) -> tuple[int, ...]:
        return self.after.active_status_uids

    @property
    def state_unchanged(self) -> bool:
        return self.after is self.before

    @property
    def turns_before(self) -> int:
        return self.difference_before

    @property
    def turns_after(self) -> int:
        return self.difference_after

    @property
    def replaced(self) -> bool:
        return self.created_status_uid is not None and self.before.restriction_status_uid is not None


install_transition = Plan2NativeBlockRestrictionInstallTransition


def _failed_runtime_execution(
    runtime: Plan2NativeBlockFixRestrictionRuntime,
    program: Plan2NativeBlockFixRestrictionVersion,
    origin: str,
    reason: str,
    *,
    requested_value: int = 0,
    operations: tuple[str, ...] = (),
) -> Plan2NativeBlockFixRestrictionExecution:
    return Plan2NativeBlockFixRestrictionExecution(
        before=runtime,
        after=runtime,
        program=program,
        play_origin=origin,
        resolved=False,
        committed=False,
        actual_delta=0,
        requested_value=requested_value,
        lower_bound=LOWER_BOUND,
        reason=reason,
        operations=operations,
    )


def _check_prior_effects(
    program: Plan2NativeBlockFixRestrictionVersion,
    completed_effect_ids: Sequence[str],
) -> str | None:
    try:
        completed = tuple(completed_effect_ids)
    except TypeError:
        return "completed-effect-shape"
    if any(type(item) is not str for item in completed):
        return "completed-effect-shape"
    if completed != program.prior_effect_ids:
        return "ordered-prior-effects-unproven"
    return None


def _native_block_fix_effect(effect: Plan2NativeBlockFixRestrictionEffect) -> _block_fix.Plan2ExamEffectRow:
    return _block_fix.Plan2ExamEffectRow(
        effect_id=effect.effect_id,
        effect_type=effect.effect_type,
        value1=effect.value1,
        value2=effect.value2,
        effect_count=effect.effect_count,
        effect_turn=effect.effect_turn,
        status_enchant_id=effect.status_enchant_id,
        chain_effect_id=effect.chain_effect_id,
        effect_group_ids=effect.effect_group_ids,
        produce_description_count=effect.produce_description_count,
        customize_description_count=effect.customize_description_count,
    )


def _native_restriction_effect(effect: Plan2NativeBlockFixRestrictionEffect) -> _restriction.EffectRow:
    return _restriction.EffectRow(
        id=effect.effect_id,
        effect_type=effect.effect_type,
        value1=effect.value1,
        value2=effect.value2,
        effect_count=effect.effect_count,
        effect_turn=effect.effect_turn,
        effect_group_ids=effect.effect_group_ids,
        context=_restriction.DIRECT_CARD_CONTEXT,
    )


def execute_plan2_native_block_fix(
    runtime: _block_fix.Plan2BlockFixRuntime,
    effect: Plan2NativeBlockFixRestrictionEffect,
) -> Plan2NativeBlockFixExecution:
    """Execute the exact direct BlockFix tail over the reused leaf runtime."""

    if not isinstance(runtime, _block_fix.Plan2BlockFixRuntime):
        raise TypeError("runtime must be Plan2BlockFixRuntime")
    if not isinstance(effect, Plan2NativeBlockFixRestrictionEffect):
        raise TypeError("effect must be typed")
    ref = next(
        (item for item, effect_id in TARGET_EFFECT_ID_BY_REF.items() if effect_id == effect.effect_id),
        (BLOCK_FIX_CARD_ID, 0),
    )
    program = Plan2NativeBlockFixRestrictionVersion(
        family=BLOCK_FIX_FAMILY,
        card_id=ref[0],
        upgrade=ref[1],
        name="direct",
        plan_type=COMMON_PLAN,
        category=MENTAL_SKILL_CATEGORY,
        stamina=0,
        force_stamina=1,
        cost_type=COST_UNKNOWN,
        cost_value=0,
        play_trigger_id="",
        move_position_type=MOVE_LOST,
        effect_slots=(
            Plan2NativeBlockFixRestrictionEffectSlot(0, effect),
        ),
        target_slot_index=0,
        companion_blockers=(),
    )
    try:
        native = _block_fix.execute_plan2_block_fix(runtime, _native_block_fix_effect(effect))
    except (TypeError, ValueError, _block_fix.Plan2StaminaUp500BlockFixError) as error:
        return Plan2NativeBlockFixExecution(
            runtime,
            runtime,
            program,
            "normal",
            False,
            False,
            0,
            effect.value1,
            LOWER_BOUND,
            str(error),
            (),
            None,
        )
    after = native.after
    return Plan2NativeBlockFixExecution(
        before=runtime,
        after=after,
        program=program,
        play_origin="normal",
        resolved=True,
        committed=True,
        actual_delta=after.block - runtime.block,
        requested_value=native.effective_value,
        lower_bound=LOWER_BOUND,
        operations=native.event_trace,
        native=native,
    )


execute_block_fix = execute_plan2_native_block_fix


def install_plan2_native_block_restriction(
    runtime: Plan2NativeBlockFixRestrictionRuntime,
    program: Plan2NativeBlockFixRestrictionVersion,
    *,
    play_origin: Plan2BlockFixRestrictionPlayOrigin | str = Plan2BlockFixRestrictionPlayOrigin.NORMAL,
    completed_effect_ids: Sequence[str] = (),
) -> Plan2NativeBlockRestrictionInstallTransition:
    """Install/merge the exact target restriction with immutable UID state."""

    if not isinstance(runtime, Plan2NativeBlockFixRestrictionRuntime):
        raise TypeError("runtime must be Plan2NativeBlockFixRestrictionRuntime")
    if not isinstance(program, Plan2NativeBlockFixRestrictionVersion):
        raise TypeError("program must be Plan2NativeBlockFixRestrictionVersion")
    origin = _normalize_origin(play_origin)
    origin_text = origin if origin is not None else str(play_origin)
    if origin is None:
        return Plan2NativeBlockRestrictionInstallTransition(
            runtime,
            runtime,
            program,
            origin_text,
            None,
            False,
            False,
            False,
            "unresolved",
            None,
            None,
            runtime.restriction.turns,
            runtime.restriction.turns,
            "unknown-play-origin",
            (),
        )
    if program.family is not Plan2BlockFixRestrictionFamily.BLOCK_RESTRICTION:
        return Plan2NativeBlockRestrictionInstallTransition(
            runtime,
            runtime,
            program,
            origin,
            None,
            False,
            False,
            False,
            "unresolved",
            None,
            None,
            runtime.restriction.turns,
            runtime.restriction.turns,
            "wrong-target-family",
            (),
        )
    prior_reason = _check_prior_effects(program, completed_effect_ids)
    if prior_reason is not None:
        return Plan2NativeBlockRestrictionInstallTransition(
            runtime,
            runtime,
            program,
            origin,
            None,
            False,
            False,
            False,
            "unresolved",
            None,
            None,
            runtime.restriction.turns,
            runtime.restriction.turns,
            prior_reason,
            (f"{origin}:reject-before-target-slot:{program.target_slot_index}",),
        )
    target_reason = _target_shape_reason(program.family, program.ref, program.target_effect)
    if target_reason is not None:
        return Plan2NativeBlockRestrictionInstallTransition(
            runtime,
            runtime,
            program,
            origin,
            None,
            False,
            False,
            False,
            "unresolved",
            None,
            None,
            runtime.restriction.turns,
            runtime.restriction.turns,
            target_reason,
            (),
        )
    if runtime.restriction.turns == 0 and runtime.next_status_uid == INT32_MAX:
        return Plan2NativeBlockRestrictionInstallTransition(
            runtime,
            runtime,
            program,
            origin,
            None,
            False,
            False,
            False,
            "unresolved",
            None,
            None,
            0,
            0,
            "status-uid-i32-overflow",
            ("reject-before-status-install",),
        )
    try:
        native = _restriction.execute_plan2_block_restriction(
            _native_restriction_effect(program.target_effect),
            runtime.restriction,
            runtime.anti_debuff,
            context=_restriction.DIRECT_CARD_CONTEXT,
            plan_type=COMMON_PLAN,
        )
    except (TypeError, ValueError, _restriction.Plan2BlockRestrictionResolutionError) as error:
        return Plan2NativeBlockRestrictionInstallTransition(
            runtime,
            runtime,
            program,
            origin,
            None,
            False,
            False,
            False,
            "unresolved",
            None,
            None,
            runtime.restriction.turns,
            runtime.restriction.turns,
            str(error),
            (),
        )
    gate = getattr(native, "anti_debuff_gate", None)
    anti_after = getattr(gate, "state_after", runtime.anti_debuff)
    if not isinstance(anti_after, _restriction.AntiDebuffRuntime):
        anti_after = runtime.anti_debuff
    if not native.executable:
        return Plan2NativeBlockRestrictionInstallTransition(
            runtime,
            runtime,
            program,
            origin,
            native,
            False,
            False,
            False,
            "unresolved",
            None,
            None,
            runtime.restriction.turns,
            runtime.restriction.turns,
            "exact-effect-context-contract",
            tuple(getattr(native, "trace", ())),
        )
    if native.operation == "blocked-by-anti-debuff":
        after = replace(runtime, anti_debuff=anti_after)
        return Plan2NativeBlockRestrictionInstallTransition(
            runtime,
            after,
            program,
            origin,
            native,
            True,
            True,
            False,
            native.operation,
            None,
            None,
            runtime.restriction.turns,
            runtime.restriction.turns,
            "",
            (f"{origin}:direct-slot:{program.target_slot_index}:BlockRestriction", *native.trace),
        )
    if runtime.restriction_status_uid is None:
        uid = runtime.next_status_uid
        after = replace(
            runtime,
            restriction=native.after,
            restriction_status_uid=uid,
            next_status_uid=uid + 1,
            anti_debuff=anti_after,
        )
        created, merged = uid, None
    else:
        uid = runtime.restriction_status_uid
        after = replace(runtime, restriction=native.after, anti_debuff=anti_after)
        created, merged = None, uid
    return Plan2NativeBlockRestrictionInstallTransition(
        runtime,
        after,
        program,
        origin,
        native,
        True,
        True,
        True,
        native.operation,
        created,
        merged,
        runtime.restriction.turns,
        after.restriction.turns,
        "",
        (f"{origin}:direct-slot:{program.target_slot_index}:BlockRestriction", *native.trace, f"status-uid:{uid}"),
    )


install_block_restriction = install_plan2_native_block_restriction
install_restriction = install_plan2_native_block_restriction
install_plan2_native_block_fix_restriction = install_plan2_native_block_restriction


@dataclass(frozen=True, slots=True)
class Plan2NativeBlockFixRestrictionExecution:
    before: Plan2NativeBlockFixRestrictionRuntime
    after: Plan2NativeBlockFixRestrictionRuntime
    program: Plan2NativeBlockFixRestrictionVersion
    play_origin: str
    resolved: bool
    committed: bool
    actual_delta: int
    requested_value: int
    lower_bound: int
    block_fix: Plan2NativeBlockFixExecution | None = None
    restriction_install: Plan2NativeBlockRestrictionInstallTransition | None = None
    reason: str = ""
    operations: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return self.resolved and self.committed

    @property
    def target_effect_executable(self) -> bool:
        return self.resolved

    @property
    def state_unchanged(self) -> bool:
        return self.after is self.before

    @property
    def block_before(self) -> int:
        return self.before.block

    @property
    def block_after(self) -> int:
        return self.after.block

    @property
    def block_difference(self) -> int:
        return self.actual_delta

    @property
    def actual_block_delta(self) -> int:
        return self.actual_delta

    @property
    def created_status_uid(self) -> int | None:
        return None if self.restriction_install is None else self.restriction_install.created_status_uid

    @property
    def merged_status_uid(self) -> int | None:
        return None if self.restriction_install is None else self.restriction_install.merged_status_uid

    @property
    def status_uid(self) -> int | None:
        return self.created_status_uid or self.merged_status_uid

    @property
    def operation(self) -> str:
        if self.restriction_install is not None:
            return self.restriction_install.operation
        return "block-fix" if self.block_fix is not None else "unresolved"


def execute_plan2_native_block_fix_restriction(
    runtime: Plan2NativeBlockFixRestrictionRuntime,
    program: Plan2NativeBlockFixRestrictionVersion,
    *,
    play_origin: Plan2BlockFixRestrictionPlayOrigin | str = Plan2BlockFixRestrictionPlayOrigin.NORMAL,
    completed_effect_ids: Sequence[str] = (),
) -> Plan2NativeBlockFixRestrictionExecution:
    """Execute one target effect atomically over the immutable combined state."""

    if not isinstance(runtime, Plan2NativeBlockFixRestrictionRuntime):
        raise TypeError("runtime must be Plan2NativeBlockFixRestrictionRuntime")
    if not isinstance(program, Plan2NativeBlockFixRestrictionVersion):
        raise TypeError("program must be Plan2NativeBlockFixRestrictionVersion")
    origin = _normalize_origin(play_origin)
    origin_text = origin if origin is not None else str(play_origin)
    if origin is None:
        return Plan2NativeBlockFixRestrictionExecution(runtime, runtime, program, origin_text, False, False, 0, 0, LOWER_BOUND, reason="unknown-play-origin")
    prior_reason = _check_prior_effects(program, completed_effect_ids)
    if prior_reason is not None:
        return Plan2NativeBlockFixRestrictionExecution(runtime, runtime, program, origin, False, False, 0, program.value1, LOWER_BOUND, reason=prior_reason)
    target_reason = _target_shape_reason(program.family, program.ref, program.target_effect)
    if target_reason is not None:
        return Plan2NativeBlockFixRestrictionExecution(runtime, runtime, program, origin, False, False, 0, program.value1, LOWER_BOUND, reason=target_reason)
    if program.family is Plan2BlockFixRestrictionFamily.BLOCK_FIX:
        try:
            native = _block_fix.execute_plan2_block_fix(
                _block_fix.Plan2BlockFixRuntime(runtime.block, runtime.block_consumption_sum_count),
                _native_block_fix_effect(program.target_effect),
            )
        except (TypeError, ValueError, _block_fix.Plan2StaminaUp500BlockFixError) as error:
            return Plan2NativeBlockFixRestrictionExecution(runtime, runtime, program, origin, False, False, 0, program.value1, LOWER_BOUND, reason=str(error))
        after = replace(runtime, block=native.after.block, block_consumption_sum_count=native.after.block_consumption_sum_count)
        direct = Plan2NativeBlockFixExecution(
            before=runtime,
            after=after,
            program=program,
            play_origin=origin,
            resolved=True,
            committed=True,
            actual_delta=after.block - runtime.block,
            requested_value=native.effective_value,
            lower_bound=LOWER_BOUND,
            operations=(
                f"{origin}:direct-slot:{program.target_slot_index}:BlockFix",
                *native.event_trace,
            ),
            native=native,
        )
        return Plan2NativeBlockFixRestrictionExecution(
            before=runtime,
            after=after,
            program=program,
            play_origin=origin,
            resolved=True,
            committed=True,
            actual_delta=direct.actual_delta,
            requested_value=direct.requested_value,
            lower_bound=LOWER_BOUND,
            block_fix=direct,
            operations=direct.operations,
        )
    install = install_plan2_native_block_restriction(
        runtime,
        program,
        play_origin=origin,
        completed_effect_ids=completed_effect_ids,
    )
    return Plan2NativeBlockFixRestrictionExecution(
        before=runtime,
        after=install.after,
        program=program,
        play_origin=origin,
        resolved=install.resolved,
        committed=install.committed,
        actual_delta=0,
        requested_value=0,
        lower_bound=LOWER_BOUND,
        restriction_install=install,
        reason=install.reason,
        operations=install.operations,
    )


execute_plan2_native_block_fix_restriction_effect = execute_plan2_native_block_fix_restriction
execute_block_fix_restriction = execute_plan2_native_block_fix_restriction
execute_target = execute_plan2_native_block_fix_restriction
execute_plan2_native_block_restriction = execute_plan2_native_block_fix_restriction
execute_plan2_native_block_restriction_effect = execute_plan2_native_block_fix_restriction
execute_block_restriction = execute_plan2_native_block_fix_restriction


@dataclass(frozen=True, slots=True)
class Plan2NativeBlockFixRestrictionTurnTransition:
    before: Plan2NativeBlockFixRestrictionRuntime
    after: Plan2NativeBlockFixRestrictionRuntime
    resolved: bool
    committed: bool
    fresh_status_uids: tuple[int, ...] = ()
    spent_status_uids: tuple[int, ...] = ()
    expired_status_uids: tuple[int, ...] = ()
    permanent_status_uids: tuple[int, ...] = ()
    reason: str = ""
    operations: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return self.resolved and self.committed

    @property
    def state_unchanged(self) -> bool:
        return self.after is self.before

    @property
    def active_status_uids(self) -> tuple[int, ...]:
        return self.after.active_status_uids

    @property
    def turns_before(self) -> int:
        return self.before.restriction.turns

    @property
    def turns_after(self) -> int:
        return self.after.restriction.turns


def advance_plan2_native_block_fix_restriction_turn_start(
    runtime: Plan2NativeBlockFixRestrictionRuntime,
) -> Plan2NativeBlockFixRestrictionTurnTransition:
    """Run one exact restriction turn-start boundary immutably."""

    if not isinstance(runtime, Plan2NativeBlockFixRestrictionRuntime):
        raise TypeError("runtime must be Plan2NativeBlockFixRestrictionRuntime")
    if not runtime.restriction.status_present:
        return Plan2NativeBlockFixRestrictionTurnTransition(
            runtime,
            runtime,
            True,
            True,
            operations=("turn-start:no-block-restriction-status",),
        )
    before_native = runtime.restriction
    after_native = before_native.spend_turn_boundary()
    uid = runtime.restriction_status_uid
    if uid is None:
        return Plan2NativeBlockFixRestrictionTurnTransition(
            runtime,
            runtime,
            False,
            False,
            reason="present-status-missing-uid",
        )
    fresh: tuple[int, ...] = ()
    spent: tuple[int, ...] = ()
    expired: tuple[int, ...] = ()
    if not before_native.is_passing_turn_start:
        fresh = (uid,)
    else:
        spent = (uid,)
        if not after_native.status_present:
            expired = (uid,)
    after = replace(
        runtime,
        restriction=after_native,
        restriction_status_uid=uid if after_native.status_present else None,
    )
    return Plan2NativeBlockFixRestrictionTurnTransition(
        before=runtime,
        after=after,
        resolved=True,
        committed=True,
        fresh_status_uids=fresh,
        spent_status_uids=spent,
        expired_status_uids=expired,
        operations=(
            "phase:ProduceExamPhaseType_ExamStartTurn",
            f"restriction-status:{uid}:before={before_native.turns}",
            "restriction:mark-passing" if fresh else "restriction:spend-turn",
            f"restriction-status:{uid}:after={after_native.turns}",
            "restriction:expire" if expired else "restriction:retain",
        ),
    )


advance_plan2_native_block_fix_restriction_turn = advance_plan2_native_block_fix_restriction_turn_start
advance_plan2_native_block_fix_restriction_end_turn = advance_plan2_native_block_fix_restriction_turn_start
advance_block_fix_restriction_turn = advance_plan2_native_block_fix_restriction_turn_start
advance_plan2_native_block_restriction_turn_start = advance_plan2_native_block_fix_restriction_turn_start
advance_plan2_native_block_restriction_turn = advance_plan2_native_block_fix_restriction_turn_start
start_turn = advance_plan2_native_block_fix_restriction_turn_start
end_turn = advance_plan2_native_block_fix_restriction_turn_start


@dataclass(frozen=True, slots=True)
class Plan2NativeBlockAddExecution:
    before: Plan2NativeBlockFixRestrictionRuntime
    after: Plan2NativeBlockFixRestrictionRuntime
    requested_value: int
    calculated_value: int
    actual_delta: int
    restriction_active: bool
    resolved: bool
    committed: bool
    reason: str = ""
    operations: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return self.resolved and self.committed

    @property
    def state_unchanged(self) -> bool:
        return self.after is self.before


def execute_plan2_native_block_add(
    runtime: Plan2NativeBlockFixRestrictionRuntime,
    value: object,
) -> Plan2NativeBlockAddExecution:
    """Expose the narrow native set/add/restriction order for callers."""

    if not isinstance(runtime, Plan2NativeBlockFixRestrictionRuntime):
        raise TypeError("runtime must be Plan2NativeBlockFixRestrictionRuntime")
    try:
        requested = _i32(value, "block-add.value")
    except Plan2NativeBlockFixRestrictionError as error:
        return Plan2NativeBlockAddExecution(runtime, runtime, 0, 0, 0, runtime.status_present, False, False, str(error))
    operations = ["get-block:before", "CalculateAddBlock:value<1=>0"]
    if requested < 1:
        calculated = 0
    elif runtime.status_present:
        calculated = 0
        operations.append("CalculateAddBlock:restriction=>0")
    else:
        calculated = requested
    candidate = runtime.block + calculated
    if candidate < INT32_MIN or candidate > INT32_MAX:
        return Plan2NativeBlockAddExecution(runtime, runtime, requested, calculated, 0, runtime.status_present, False, False, "block-add-output-signed-int32-wrap-unproven", tuple(operations))
    after = replace(runtime, block=max(LOWER_BOUND, candidate))
    operations.extend(("CalculateAddBlock:lower-bound0", "SetBlock(isConsumption=false)"))
    return Plan2NativeBlockAddExecution(
        runtime,
        after,
        requested,
        calculated,
        after.block - runtime.block,
        runtime.status_present,
        True,
        True,
        operations=tuple(operations),
    )


execute_block_add = execute_plan2_native_block_add
calculate_block_add = execute_plan2_native_block_add


def _program_for_handoff(
    source: Plan2NativeBlockFixRestrictionCompilation | Plan2NativeBlockFixRestrictionCatalog,
    row: Plan2NativeBlockFixRestrictionVersion | Plan2NativeBlockFixRestrictionHandoffRow,
) -> Plan2NativeBlockFixRestrictionVersion:
    if isinstance(source, Plan2NativeBlockFixRestrictionCompilation):
        if source.blockers or source.failed_refs:
            raise Plan2NativeBlockFixRestrictionError("handoff-source-incomplete")
        catalog = source.catalog
    elif isinstance(source, Plan2NativeBlockFixRestrictionCatalog):
        catalog = source
    else:
        raise TypeError("source must be typed catalog or compilation")
    if isinstance(row, Plan2NativeBlockFixRestrictionVersion):
        return catalog.resolve(row.card_id, row.upgrade)
    if not isinstance(row, Plan2NativeBlockFixRestrictionHandoffRow):
        raise TypeError("handoff row must be typed")
    program = catalog.resolve(row.card_id, row.upgrade)
    if (
        row.version_ref != program.version_ref
        or row.family != program.family.value
        or row.target_effect_id != program.target_effect_id
        or row.target_effect_type != program.target_effect_type
        or row.target_slot_index != program.target_slot_index
        or row.value1 != program.value1
        or row.value2 != program.value2
        or row.count != program.count
        or row.turn != program.turn
        or row.ordered_effect_ids != program.ordered_effect_ids
        or row.prior_effect_ids != program.prior_effect_ids
        or row.target_effect_executable != program.target_effect_executable
    ):
        raise Plan2NativeBlockFixRestrictionError("handoff-row-drift", row.version_ref)
    return program


def execute_plan2_native_block_fix_restriction_handoff(
    source: Plan2NativeBlockFixRestrictionRuntime
    | Plan2NativeBlockFixRestrictionCatalog
    | Plan2NativeBlockFixRestrictionCompilation,
    handoff_or_program: Plan2NativeBlockFixRestrictionVersion
    | Plan2NativeBlockFixRestrictionHandoffRow
    | Plan2NativeBlockFixRestrictionRuntime,
    runtime: Plan2NativeBlockFixRestrictionRuntime | None = None,
    *,
    play_origin: Plan2BlockFixRestrictionPlayOrigin | str = Plan2BlockFixRestrictionPlayOrigin.NORMAL,
    completed_effect_ids: Sequence[str] = (),
) -> Plan2NativeBlockFixRestrictionExecution:
    """Execute a typed program directly or resolve one immutable handoff row."""

    if isinstance(source, Plan2NativeBlockFixRestrictionRuntime):
        if runtime is not None:
            raise TypeError("runtime must be omitted when source is runtime")
        before = source
        candidate = handoff_or_program
        if not isinstance(candidate, Plan2NativeBlockFixRestrictionVersion):
            raise TypeError("direct form requires a typed program")
        program = candidate
    else:
        if runtime is None:
            raise TypeError("catalog form requires runtime")
        before = runtime
        program = _program_for_handoff(source, handoff_or_program)  # type: ignore[arg-type]
    return execute_plan2_native_block_fix_restriction(
        before,
        program,
        play_origin=play_origin,
        completed_effect_ids=completed_effect_ids,
    )


execute_block_fix_restriction_handoff = execute_plan2_native_block_fix_restriction_handoff
execute_native_handoff = execute_plan2_native_block_fix_restriction_handoff


def build_native_audit(
    source: Plan2NativeBlockFixRestrictionCompilation | Plan2NativeBlockFixRestrictionCatalog | None = None,
) -> dict[str, object]:
    compilation = source if isinstance(source, Plan2NativeBlockFixRestrictionCompilation) else None
    catalog = source.catalog if compilation is not None else (source or load_plan2_native_catalog_block_fix_restriction())
    handoff = build_plan2_native_block_fix_restriction_handoff(catalog)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "auditId": ADAPTER_ID,
        "scope": {
            "plan": "Plan2",
            "families": list(TARGET_FAMILIES),
            "cardIds": list(TARGET_CARD_IDS),
            "targetVersionCount": TARGET_VERSION_COUNT,
            "programCatalogModified": False,
            "horizonModified": False,
            "coreRuntimeModified": False,
            "formalCoverageModified": False,
            "plan3Modified": False,
            "guiModified": False,
            "controllerModified": False,
            "securityHashTimeWorkPerformed": False,
        },
        "master": {
            "database": catalog.database,
            "exactShapeSupported": catalog.affected_refs == EXPECTED_TARGET_REFS and catalog.target_effect_executable,
            "affectedRefs": [{"cardId": card_id, "upgrade": upgrade} for card_id, upgrade in catalog.affected_refs],
            "orderedEffectIds": {f"{card_id}#{upgrade}": list(program.ordered_effect_ids) for program in catalog.programs for card_id, upgrade in (program.ref,)},
            "targetEffects": [program.target_effect.to_dict() for program in catalog.programs],
        },
        "native": {
            **NATIVE_EVIDENCE.to_dict(),
            "setAddRestrictionOrder": [
                "set-block-fix:read Block",
                "add-block:CalculateAddBlock checks value<1 then active BlockRestriction",
                "set-block-fix:AddBlockFix max(-Block,value1)",
                "set-block:nonnegative lower bound 0",
                "restriction:anti-debuff gate before install/merge/difference",
            ],
            "actualDelta": "afterBlock-beforeBlock",
            "restrictionLifecycle": ["fresh", "merge-same-type", "new-uid-after-expiry"],
            "differenceOrder": [
                "BlockFix append block difference",
                "SetBlock(isConsumption=false)",
                "append outer status-effect type 3 difference",
                "deferred difference callback",
            ],
        },
        "accounting": {
            "affectedVersionCount": handoff.affected_version_count,
            "compiledVersionCount": compilation.compiled_version_count if compilation else handoff.affected_version_count,
            "targetEffectExecutableVersionCount": handoff.executable_version_count,
            "companionBlockedVersionCount": len(handoff.companion_blocked_refs),
            "companionBlockerOccurrenceCount": len(handoff.companion_blockers),
            "wholeCardExecutableVersionCount": len(handoff.whole_card_executable_refs),
            "companionBlockerCodeCounts": handoff.companion_blocker_code_counts,
            "companionFamilyVersionCounts": catalog.companion_family_version_counts,
            "affectedRefs": [f"{card_id}#{upgrade}" for card_id, upgrade in handoff.affected_refs],
            "targetEffectExecutableRefs": [f"{card_id}#{upgrade}" for card_id, upgrade in handoff.executable_refs],
            "wholeCardExecutableRefs": [f"{card_id}#{upgrade}" for card_id, upgrade in handoff.whole_card_executable_refs],
        },
        "handoff": handoff.to_dict(),
        "verification": {
            "focusedOnly": True,
            "fullRun": False,
            "securityRun": False,
            "hashRun": False,
            "timeRun": False,
            "runtimeAgentCalls": False,
        },
    }


native_audit = build_native_audit
catalog_to_dict = block_fix_restriction_catalog_summary


__all__ = [
    "ADAPTER_ID",
    "BLOCK_DIFFERENCE_STATUS_EFFECT_TYPE",
    "BLOCK_FIX_CARD_ID",
    "BLOCK_FIX_EFFECT_IDS",
    "BLOCK_FIX_EFFECT_TYPE",
    "BLOCK_FIX_FAMILY",
    "BLOCK_FIX_PLAYABLE_VALUE_ADD_EFFECT_ID",
    "BLOCK_FIX_TARGET_EFFECT_ID_BY_UPGRADE",
    "BLOCK_FIX_TARGET_EFFECT_IDS",
    "BLOCK_FIX_TARGET_UPGRADES",
    "BLOCK_FIX_TARGET_VERSION_COUNT",
    "BLOCK_FIX_RESTRICTION_FAMILY",
    "BLOCK_FIX_RESTRICTION_EFFECT_ID",
    "BLOCK_FIX_RESTRICTION_EFFECT_TYPE",
    "BLOCK_RESTRICTION_CARD_ID",
    "BLOCK_RESTRICTION_BLOCK_EFFECT_IDS",
    "BLOCK_RESTRICTION_EFFECT_GROUP_ID",
    "BLOCK_RESTRICTION_EFFECT_TYPE",
    "BLOCK_RESTRICTION_EFFECT_TYPE_VALUE",
    "BLOCK_RESTRICTION_STAMINA_EFFECT_IDS",
    "BLOCK_RESTRICTION_TARGET_EFFECT_ID",
    "BLOCK_RESTRICTION_TARGET_UPGRADES",
    "BLOCK_RESTRICTION_TARGET_VERSION_COUNT",
    "BLOCK_RESTRICTION_TURN",
    "BLOCK_RESTRICTION_FAMILY",
    "COMMON_PLAN",
    "CompanionBlocker",
    "DEFAULT_AUDIT_PATH",
    "EffectRow",
    "EffectSlot",
    "EXPECTED_ORDERED_EFFECT_IDS",
    "EXPECTED_TARGET_REFS",
    "EXPECTED_AFFECTED_VERSION_COUNT",
    "EXPECTED_EXECUTABLE_VERSION_COUNT",
    "INT32_MAX",
    "INT32_MIN",
    "LOWER_BOUND",
    "NATIVE_EVIDENCE",
    "PLAN2_NATIVE_CATALOG_BLOCK_FIX_RESTRICTION_SCHEMA_VERSION",
    "Plan2BlockFixRestrictionFamily",
    "Plan2BlockFixRestrictionPlayOrigin",
    "Plan2NativeBlockAddExecution",
    "Plan2NativeBlockFixExecution",
    "Plan2NativeBlockFixRestrictionBlocker",
    "Plan2NativeBlockFixRestrictionCatalog",
    "Plan2NativeBlockFixRestrictionCatalogCompilation",
    "Plan2NativeBlockFixRestrictionCentralHandoff",
    "Plan2NativeBlockFixRestrictionCompilation",
    "Plan2NativeBlockFixRestrictionEffect",
    "Plan2NativeBlockFixRestrictionEffectRow",
    "Plan2NativeBlockFixRestrictionEffectSlot",
    "Plan2NativeBlockFixRestrictionError",
    "Plan2NativeBlockFixRestrictionExecution",
    "Plan2NativeBlockFixRestrictionHandoff",
    "Plan2NativeBlockFixRestrictionHandoffRow",
    "Plan2NativeBlockFixRestrictionInputError",
    "Plan2NativeBlockFixRestrictionNativeEvidence",
    "Plan2NativeBlockFixRestrictionProgram",
    "Plan2NativeBlockFixRestrictionRuntime",
    "Plan2NativeBlockFixRestrictionState",
    "Plan2NativeBlockFixRestrictionTurnTransition",
    "Plan2NativeBlockFixRestrictionVersion",
    "Plan2BlockRestrictionRuntime",
    "Plan2NativeBlockRestrictionRuntime",
    "Plan2NativeBlockRestrictionInstallTransition",
    "Plan2NativeBlockRestrictionStatus",
    "PlayOrigin",
    "PLAY_ORIGINS",
    "SUPPORTED_PLAY_ORIGINS",
    "TARGET_CARD_IDS",
    "TARGET_EFFECT_ID_BY_REF",
    "TARGET_SLOT_INDEX_BY_REF",
    "TARGET_UPGRADES",
    "TARGET_VERSION_COUNT",
    "advance_block_fix_restriction_turn",
    "advance_plan2_native_block_fix_restriction_end_turn",
    "advance_plan2_native_block_fix_restriction_turn",
    "advance_plan2_native_block_fix_restriction_turn_start",
    "advance_plan2_native_block_restriction_turn",
    "advance_plan2_native_block_restriction_turn_start",
    "block_fix_restriction_catalog_summary",
    "build_block_fix_restriction_central_handoff",
    "build_native_audit",
    "build_native_handoff",
    "build_plan2_block_fix_restriction_central_handoff",
    "build_plan2_native_block_fix_restriction_handoff",
    "calculate_block_add",
    "catalog_summary",
    "compile_block_fix_restriction_catalog",
    "compile_native_catalog",
    "compile_plan2_block_fix_restriction_catalog",
    "compile_plan2_native_block_fix_restriction_catalog",
    "compile_plan2_native_catalog_block_fix_restriction",
    "end_turn",
    "execute_block_add",
    "execute_block_fix",
    "execute_block_fix_restriction",
    "execute_block_restriction",
    "execute_block_fix_restriction_handoff",
    "execute_native_handoff",
    "execute_plan2_native_block_add",
    "execute_plan2_native_block_fix",
    "execute_plan2_native_block_fix_restriction",
    "execute_plan2_native_block_fix_restriction_effect",
    "execute_plan2_native_block_fix_restriction_handoff",
    "execute_plan2_native_block_restriction",
    "execute_plan2_native_block_restriction_effect",
    "exact_plan2_native_block_fix_restriction_rows",
    "install_block_restriction",
    "install_plan2_native_block_restriction",
    "install_plan2_native_block_fix_restriction",
    "install_restriction",
    "load_block_fix_restriction_catalog",
    "load_native_catalog",
    "load_plan2_native_catalog_block_fix_restriction",
    "load_plan2_block_fix_restriction_catalog",
    "load_plan2_native_block_fix_restriction_catalog",
    "native_audit",
    "start_turn",
]
