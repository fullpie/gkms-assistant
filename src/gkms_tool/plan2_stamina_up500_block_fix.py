"""Bounded Plan2 standalone model for the four ``執念キャッチャー`` rows.

This module is deliberately a leaf adapter.  It reads the exact Master rows,
reuses only the already-audited binary32 stamina predicate, and models the
native ``ExamBlockFix`` operation without importing the central engine,
``native_search``, or formal coverage code.

The trigger is a direct card-effect trigger.  Android evidence places its
snapshot at the pre-payment card-play build boundary; the ordered PlayEffect
commands are then executed after payment.  The target card's first effect is
an unconditional BlockFix and its second effect is the conditional
PlayableValueAdd.  A failed or unavailable proof never commits a partial
transition.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Final, Literal

from .master_db import DEFAULT_DATABASE
from .plan3_none_stamina_trigger import (
    NONE_STAMINA_UP_MULTIPLE_500,
    NoneStaminaTrigger,
    PHASE_NONE,
    TRIGGER_NONE_STAMINA_UP_MULTIPLE_500,
)
from .plan3_stamina_multiple_trigger import (
    FIELD_STAMINA_UP_MULTIPLE,
    NATIVE_BRANCHES,
    NATIVE_FORMULA_EVIDENCE,
    Comparison,
    StaminaField,
    StaminaMultipleEvaluation,
    StaminaSnapshot,
    evaluate_stamina_multiple,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FORMAL_COVERAGE_PATH: Final = (
    PROJECT_ROOT / "var" / "coverage" / "plan2_card_executable_coverage.json"
)
DEFAULT_AUDIT_PATH: Final = (
    PROJECT_ROOT
    / "var"
    / "coverage"
    / "plan2_stamina_up500_block_fix_native_audit.json"
)

TARGET_CARD_ID: Final = "p_card-00-sup-3_161"
CARD_ID: Final = TARGET_CARD_ID
TARGET_CARD_NAME: Final = "\u57f7\u5ff5\u30ad\u30e3\u30c3\u30c1\u30e3\u30fc"
TARGET_CARD_NAMES: Final = tuple(
    f"{TARGET_CARD_NAME}{'+' * upgrade}" for upgrade in range(4)
)
TARGET_UPGRADES: Final = (0, 1, 2, 3)
UPGRADES: Final = TARGET_UPGRADES

COMMON_PLAN_TYPE: Final = "ProducePlanType_Common"
MENTAL_SKILL_CATEGORY: Final = "ProduceCardCategory_MentalSkill"
CARD_MOVE_LOST: Final = "ProduceCardMovePositionType_Lost"
MOVE_UNKNOWN: Final = "ProduceCardMovePositionType_Unknown"
LESSON_UNKNOWN: Final = "ProduceStepLessonType_Unknown"

TRIGGER_ID: Final = TRIGGER_NONE_STAMINA_UP_MULTIPLE_500
TRIGGER_PHASE_TYPE: Final = PHASE_NONE
TRIGGER_PHASE: Final = PHASE_NONE
TRIGGER_FIELD_TYPE: Final = FIELD_STAMINA_UP_MULTIPLE
TRIGGER_THRESHOLD: Final = 500
TARGET_TRIGGER_ROW: Final = NONE_STAMINA_UP_MULTIPLE_500
TARGET_TRIGGER_SHAPE: Final = TARGET_TRIGGER_ROW

BLOCK_FIX_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamBlockFix"
PLAYABLE_VALUE_ADD_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamPlayableValueAdd"
)
BLOCK_DIFFERENCE_STATUS_EFFECT_TYPE: Final = 3
TARGET_BLOCK_EFFECT_IDS: Final = (
    "e_effect-exam_block_fix-0005",
    "e_effect-exam_block_fix-0010",
    "e_effect-exam_block_fix-0012",
    "e_effect-exam_block_fix-0014",
)
TARGET_PLAYABLE_EFFECT_ID: Final = "e_effect-exam_playable_value_add-01"
TARGET_EFFECT_IDS: Final = TARGET_BLOCK_EFFECT_IDS + (TARGET_PLAYABLE_EFFECT_ID,)
TARGET_EFFECT_TYPES: Final = (
    BLOCK_FIX_EFFECT_TYPE,
    BLOCK_FIX_EFFECT_TYPE,
    BLOCK_FIX_EFFECT_TYPE,
    BLOCK_FIX_EFFECT_TYPE,
    PLAYABLE_VALUE_ADD_EFFECT_TYPE,
)
# Keep the value binding explicit and keyed by upgrade, not inferred from text.
TARGET_BLOCK_VALUE_BY_UPGRADE: Final = {0: 5, 1: 10, 2: 12, 3: 14}

FORMAL_TRIGGER_GAP: Final = (
    "C:effect-trigger:e_trigger-none-stamina_up_multiple-500"
)
FORMAL_BLOCK_FIX_GAP: Final = (
    "C:effect:ProduceExamEffectType_ExamBlockFix"
)
FORMAL_TARGET_GAPS: Final = (FORMAL_TRIGGER_GAP, FORMAL_BLOCK_FIX_GAP)

INT32_MIN: Final = -(2**31)
INT32_MAX: Final = 2**31 - 1


class CardExecutionKind(str, Enum):
    """Execution paths kept separate by native evidence."""

    ORDINARY = "ordinary-card-play"
    FORCED = "forced-card-play"
    EXTRA = "extra-card-play"
    NORMAL = "ordinary-card-play"
    FORCED_PLAY = "forced-card-play"
    EXTRA_PLAY = "extra-card-play"


class TriggerSnapshotBoundary(str, Enum):
    """Only the pre-payment card-play snapshot is proven for this row."""

    PRE_PAYMENT_CARD_PLAY = "pre-payment-card-play"
    POST_PAYMENT = "post-payment"
    POST_DIRECT_EFFECTS = "post-direct-effects"
    POST_CARD_MOVE = "post-card-move"


class Plan2StaminaUp500BlockFixError(ValueError):
    """Base error for an unsupported local contract or native boundary."""


class CatalogContractError(Plan2StaminaUp500BlockFixError):
    """The selected Master rows are not the exact target shape."""


class NativeBoundaryError(Plan2StaminaUp500BlockFixError):
    """A state/effect boundary is not proven by the native evidence."""


class CoverageContractError(Plan2StaminaUp500BlockFixError):
    """The formal coverage slice cannot support exact overlap accounting."""


def _plain_i32(value: object, label: str) -> int:
    if type(value) is not int:
        raise NativeBoundaryError(f"{label} must be a plain signed Int32")
    if not INT32_MIN <= value <= INT32_MAX:
        raise NativeBoundaryError(f"{label} is outside signed Int32")
    return value


def _signed_i32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _json_value(value: object, label: str, issues: list[str]) -> object:
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        issues.append(f"invalid-json:{label}")
        return None


def _json_list(value: object, label: str, issues: list[str]) -> tuple[Any, ...]:
    parsed = _json_value(value, label, issues)
    if not isinstance(parsed, list):
        issues.append(f"json-array-required:{label}")
        return ()
    return tuple(parsed)


def _strict_string_tuple(
    value: object, label: str, issues: list[str]
) -> tuple[str, ...]:
    parsed = _json_list(value, label, issues)
    if any(type(item) is not str for item in parsed):
        issues.append(f"string-array-required:{label}")
        return ()
    return tuple(parsed)


def _strict_int_tuple(
    value: object, label: str, issues: list[str]
) -> tuple[int, ...]:
    parsed = _json_list(value, label, issues)
    if any(type(item) is not int for item in parsed):
        issues.append(f"int-array-required:{label}")
        return ()
    return tuple(parsed)


def _text(value: object, label: str, issues: list[str]) -> str:
    if type(value) is not str:
        issues.append(f"string-required:{label}")
        return ""
    return value


def _plain_int(value: object, label: str, issues: list[str]) -> int:
    if type(value) is not int:
        issues.append(f"int-required:{label}")
        return 0
    return value


def _master_trigger_contract(
    row: sqlite3.Row | None, issues: list[str]
) -> dict[str, Any] | None:
    if row is None:
        issues.append("missing-trigger-row")
        return None
    trigger_id = _text(row["id"], "trigger.id", issues)
    return {
        "id": trigger_id,
        "phase_types": list(
            _strict_string_tuple(
                row["phase_types_json"], f"{trigger_id}.phase_types", issues
            )
        ),
        "phase_values": list(
            _strict_int_tuple(
                row["phase_values_json"], f"{trigger_id}.phase_values", issues
            )
        ),
        "field_check_types": list(
            _strict_string_tuple(
                row["field_status_check_types_json"],
                f"{trigger_id}.field_check_types",
                issues,
            )
        ),
        "field_types": list(
            _strict_string_tuple(
                row["field_status_types_json"],
                f"{trigger_id}.field_types",
                issues,
            )
        ),
        "field_values": list(
            _strict_int_tuple(
                row["field_status_values_json"],
                f"{trigger_id}.field_values",
                issues,
            )
        ),
        "field_card_search_ids": list(
            _strict_string_tuple(
                row["field_status_produce_card_search_ids_json"],
                f"{trigger_id}.field_card_search_ids",
                issues,
            )
        ),
        "produce_card_search_id": _text(
            row["produce_card_search_id"],
            f"{trigger_id}.produce_card_search_id",
            issues,
        ),
        "upper_search_count": _plain_int(
            row["upper_search_count"], f"{trigger_id}.upper_search_count", issues
        ),
        "lower_search_count": _plain_int(
            row["lower_search_count"], f"{trigger_id}.lower_search_count", issues
        ),
        "card_move_position_type": _text(
            row["card_move_position_type"],
            f"{trigger_id}.card_move_position_type",
            issues,
        ),
        "effect_types": list(
            _strict_string_tuple(
                row["effect_types_json"], f"{trigger_id}.effect_types", issues
            )
        ),
        "lesson_type": _text(row["lesson_type"], f"{trigger_id}.lesson_type", issues),
    }


def _shape_differences(
    actual: Mapping[str, Any] | None,
    expected: Mapping[str, Any],
) -> tuple[str, ...]:
    if actual is None:
        return ()
    return tuple(
        f"trigger-shape-mismatch:{key}"
        for key, expected_value in expected.items()
        if actual.get(key) != expected_value
    )


@dataclass(frozen=True, slots=True)
class Plan2NoneStaminaSnapshot:
    """Signed getter values captured before card payment is committed."""

    current_stamina: int
    max_stamina: int
    boundary: TriggerSnapshotBoundary = (
        TriggerSnapshotBoundary.PRE_PAYMENT_CARD_PLAY
    )
    phase: str = PHASE_NONE

    def __post_init__(self) -> None:
        _plain_i32(self.current_stamina, "current_stamina")
        _plain_i32(self.max_stamina, "max_stamina")
        object.__setattr__(self, "boundary", TriggerSnapshotBoundary(self.boundary))
        if type(self.phase) is not str or not self.phase:
            raise NativeBoundaryError("snapshot phase must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_stamina": self.current_stamina,
            "max_stamina": self.max_stamina,
            "boundary": self.boundary.value,
            "phase": self.phase,
        }


Plan2StaminaSnapshot = Plan2NoneStaminaSnapshot


def _source_value(source: object, *names: str) -> tuple[bool, object]:
    if isinstance(source, Mapping):
        for name in names:
            if name in source:
                return True, source[name]
        return False, None
    for name in names:
        try:
            return True, getattr(source, name)
        except AttributeError:
            continue
        except Exception:
            return True, None
    return False, None


def _adapt_snapshot(
    source: object,
    *,
    default_boundary: TriggerSnapshotBoundary,
    default_phase: str,
) -> tuple[Plan2NoneStaminaSnapshot | None, tuple[str, ...]]:
    if isinstance(source, Plan2NoneStaminaSnapshot):
        return source, ()
    if isinstance(source, StaminaSnapshot):
        current, maximum = source.current_stamina, source.max_stamina
        try:
            return (
                Plan2NoneStaminaSnapshot(
                    current, maximum, default_boundary, default_phase
                ),
                (),
            )
        except (TypeError, ValueError, NativeBoundaryError):
            return None, ("snapshot-values-not-signed-int32",)
    if source is None:
        return None, ("none-trigger-snapshot-unavailable",)

    current_found, current = _source_value(
        source, "current_stamina", "currentStamina", "stamina"
    )
    max_found, maximum = _source_value(
        source, "max_stamina", "maxStamina", "maximum_stamina"
    )
    if not current_found or not max_found:
        return None, ("none-trigger-snapshot-unavailable",)
    boundary_found, boundary = _source_value(source, "boundary", "capture_boundary")
    if not boundary_found:
        boundary = default_boundary
    phase_found, phase = _source_value(source, "phase", "event_phase")
    if not phase_found:
        phase = default_phase
    try:
        return (
            Plan2NoneStaminaSnapshot(current, maximum, boundary, phase),
            (),
        )
    except (TypeError, ValueError, NativeBoundaryError):
        return None, ("snapshot-values-not-signed-int32-or-finite",)


adapt_plan2_none_stamina_snapshot = _adapt_snapshot


@dataclass(frozen=True, slots=True)
class StaminaUp500TriggerEvaluation:
    trigger_id: str
    event_phase: str | None
    snapshot: Plan2NoneStaminaSnapshot | None
    snapshot_boundary: TriggerSnapshotBoundary | None
    formula: StaminaMultipleEvaluation | None
    fires: bool | None
    execution_kind: CardExecutionKind
    reasons: tuple[str, ...] = ()
    state_unchanged: bool = True
    trigger_selected_before_payment: bool = True
    trigger_rechecked_at_effect: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "execution_kind", CardExecutionKind(self.execution_kind))
        if self.snapshot_boundary is not None:
            object.__setattr__(
                self, "snapshot_boundary", TriggerSnapshotBoundary(self.snapshot_boundary)
            )
        object.__setattr__(self, "reasons", tuple(dict.fromkeys(self.reasons)))

    @property
    def resolved(self) -> bool:
        return self.fires is not None

    @property
    def supported(self) -> bool:
        return self.fires is not None and not self.reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "event_phase": self.event_phase,
            "snapshot": None if self.snapshot is None else self.snapshot.to_dict(),
            "snapshot_boundary": (
                None if self.snapshot_boundary is None else self.snapshot_boundary.value
            ),
            "formula": None if self.formula is None else self.formula.to_dict(),
            "fires": self.fires,
            "execution_kind": self.execution_kind.value,
            "reasons": list(self.reasons),
            "state_unchanged": self.state_unchanged,
            "trigger_selected_before_payment": self.trigger_selected_before_payment,
            "trigger_rechecked_at_effect": self.trigger_rechecked_at_effect,
        }


def _trigger_contract_from_source(source: object) -> dict[str, Any] | None:
    if not isinstance(source, Mapping):
        return None
    expected_keys = set(NONE_STAMINA_UP_MULTIPLE_500.master_contract())
    if set(source) != expected_keys:
        return None
    contract = dict(source)
    for name in (
        "phase_types",
        "field_check_types",
        "field_types",
        "field_card_search_ids",
        "effect_types",
    ):
        if not isinstance(contract[name], list) or any(
            type(item) is not str for item in contract[name]
        ):
            return None
    for name in ("phase_values", "field_values"):
        if not isinstance(contract[name], list) or any(
            type(item) is not int for item in contract[name]
        ):
            return None
    for name in (
        "id",
        "produce_card_search_id",
        "card_move_position_type",
        "lesson_type",
    ):
        if type(contract[name]) is not str:
            return None
    for name in ("upper_search_count", "lower_search_count"):
        if type(contract[name]) is not int:
            return None
    return contract


def _trigger_shape_reasons(source: object) -> tuple[str, ...]:
    expected = NONE_STAMINA_UP_MULTIPLE_500.master_contract()
    if isinstance(source, str):
        return () if source == TRIGGER_ID else ("unknown-trigger-id",)
    if isinstance(source, NoneStaminaTrigger):
        actual = source.master_contract()
    else:
        actual = _trigger_contract_from_source(source)
    if actual is None:
        return ("unknown-trigger-shape",)
    reasons: list[str] = []
    if actual.get("id") != TRIGGER_ID:
        reasons.append("unknown-trigger-id")
    reasons.extend(_shape_differences(actual, expected))
    return tuple(dict.fromkeys(reasons))


def _trigger_label(source: object) -> str:
    if isinstance(source, str):
        return source
    if isinstance(source, NoneStaminaTrigger):
        return source.id
    if isinstance(source, Mapping):
        value = source.get("id", "<unknown-trigger-shape>")
        return value if type(value) is str else "<unknown-trigger-shape>"
    return "<unknown-trigger-shape>"


_MISSING: Final = object()


def evaluate_plan2_none_stamina_up_multiple(
    trigger_or_id: object = TRIGGER_ID,
    snapshot: object | None = None,
    *,
    event_phase: str | None = PHASE_NONE,
    current_stamina: object = _MISSING,
    max_stamina: object = _MISSING,
    boundary: TriggerSnapshotBoundary | str = (
        TriggerSnapshotBoundary.PRE_PAYMENT_CARD_PLAY
    ),
    execution_kind: CardExecutionKind | str = CardExecutionKind.ORDINARY,
) -> StaminaUp500TriggerEvaluation:
    """Evaluate only the exact ordinary direct-effect trigger contract."""

    trigger_id = _trigger_label(trigger_or_id)
    reasons = list(_trigger_shape_reasons(trigger_or_id))
    try:
        kind = CardExecutionKind(execution_kind)
    except (TypeError, ValueError):
        kind = CardExecutionKind.ORDINARY
        reasons.append("execution-kind-unproven")

    if event_phase != PHASE_NONE:
        reasons.append("event-phase-mismatch-or-unproven")
    if kind is not CardExecutionKind.ORDINARY:
        reasons.append("forced-or-extra-execution-unproven")

    source = snapshot
    if source is None and (
        current_stamina is not _MISSING or max_stamina is not _MISSING
    ):
        source = {
            "current_stamina": (
                None if current_stamina is _MISSING else current_stamina
            ),
            "max_stamina": None if max_stamina is _MISSING else max_stamina,
            "boundary": boundary,
            "phase": event_phase,
        }

    try:
        default_boundary = TriggerSnapshotBoundary(boundary)
    except (TypeError, ValueError):
        default_boundary = TriggerSnapshotBoundary.PRE_PAYMENT_CARD_PLAY
        reasons.append("snapshot-boundary-unproven")
    adapted, adaptation_reasons = (
        _adapt_snapshot(
            source,
            default_boundary=default_boundary,
            default_phase=event_phase or PHASE_NONE,
        )
        if source is not None
        else (None, ("none-trigger-snapshot-unavailable",))
    )
    reasons.extend(adaptation_reasons)

    formula: StaminaMultipleEvaluation | None = None
    snapshot_boundary: TriggerSnapshotBoundary | None = None
    if adapted is not None:
        snapshot_boundary = adapted.boundary
        if adapted.phase != PHASE_NONE:
            reasons.append("snapshot-phase-is-not-none")
        if adapted.boundary is not TriggerSnapshotBoundary.PRE_PAYMENT_CARD_PLAY:
            reasons.append("snapshot-is-after-card-payment-or-effect")
        formula = evaluate_stamina_multiple(
            StaminaField.STAMINA_UP_MULTIPLE,
            TRIGGER_THRESHOLD,
            StaminaSnapshot(adapted.current_stamina, adapted.max_stamina),
        )
        reasons.extend(formula.reasons)

    unique_reasons = tuple(dict.fromkeys(reasons))
    fires = (
        formula.fires
        if formula is not None and not unique_reasons
        else None
    )
    return StaminaUp500TriggerEvaluation(
        trigger_id=trigger_id,
        event_phase=event_phase,
        snapshot=adapted,
        snapshot_boundary=snapshot_boundary,
        formula=formula,
        fires=fires,
        execution_kind=kind,
        reasons=unique_reasons,
    )


evaluate_trigger = evaluate_plan2_none_stamina_up_multiple
evaluate = evaluate_plan2_none_stamina_up_multiple


@dataclass(frozen=True, slots=True)
class Plan2CardEffectSlot:
    slot_index: int
    effect_id: str
    trigger_id: str
    hide_icon: bool
    is_once_play_effect: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_index": self.slot_index,
            "effect_id": self.effect_id,
            "trigger_id": self.trigger_id,
            "hide_icon": self.hide_icon,
            "is_once_play_effect": self.is_once_play_effect,
        }


@dataclass(frozen=True, slots=True)
class Plan2ExamEffectRow:
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str
    chain_effect_id: str
    effect_group_ids: tuple[str, ...]
    produce_description_count: int
    customize_description_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "effect_group_ids", tuple(self.effect_group_ids))

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect_id": self.effect_id,
            "effect_type": self.effect_type,
            "value1": self.value1,
            "value2": self.value2,
            "effect_count": self.effect_count,
            "effect_turn": self.effect_turn,
            "status_enchant_id": self.status_enchant_id,
            "chain_effect_id": self.chain_effect_id,
            "effect_group_ids": list(self.effect_group_ids),
            "produce_description_count": self.produce_description_count,
            "customize_description_count": self.customize_description_count,
        }


@dataclass(frozen=True, slots=True)
class Plan2CardVersion:
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
    move_effect_trigger_type: str
    move_effect_ids: tuple[str, ...]
    effect_slots: tuple[Plan2CardEffectSlot, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "move_effect_ids", tuple(self.move_effect_ids))
        object.__setattr__(self, "effect_slots", tuple(self.effect_slots))

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return tuple(slot.effect_id for slot in self.effect_slots)

    @property
    def ref(self) -> str:
        return f"{self.card_id}#{self.upgrade_count}"

    def to_dict(self) -> dict[str, Any]:
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
            "move_effect_trigger_type": self.move_effect_trigger_type,
            "move_effect_ids": list(self.move_effect_ids),
            "effect_slots": [slot.to_dict() for slot in self.effect_slots],
            "ordered_effect_ids": list(self.ordered_effect_ids),
        }


@dataclass(frozen=True, slots=True)
class Plan2StaminaUp500BlockFixCatalog:
    database: str
    trigger: NoneStaminaTrigger
    actual_trigger_contract: Mapping[str, Any] | None
    card_versions: tuple[Plan2CardVersion, ...]
    effect_rows_by_id: Mapping[str, Plan2ExamEffectRow]
    shape_issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "card_versions", tuple(self.card_versions))
        object.__setattr__(self, "effect_rows_by_id", dict(self.effect_rows_by_id))
        object.__setattr__(self, "shape_issues", tuple(dict.fromkeys(self.shape_issues)))

    @property
    def affected_card_versions(self) -> tuple[Plan2CardVersion, ...]:
        return self.card_versions

    @property
    def exact_shape_supported(self) -> bool:
        return (
            not self.shape_issues
            and tuple(row.upgrade_count for row in self.card_versions) == TARGET_UPGRADES
            and set(self.effect_rows_by_id) == set(TARGET_EFFECT_IDS)
        )

    def card(self, card_id: str, upgrade_count: int) -> Plan2CardVersion | None:
        return next(
            (
                row
                for row in self.card_versions
                if row.card_id == card_id and row.upgrade_count == upgrade_count
            ),
            None,
        )

    def summary(self) -> dict[str, Any]:
        refs = [row.ref for row in self.card_versions]
        return {
            "affected_card_versions": len(refs),
            "affected_card_refs": refs,
            "exact_shape_supported": self.exact_shape_supported,
            "shape_issues": list(self.shape_issues),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "trigger": self.trigger.master_contract(),
            "actual_trigger_contract": (
                None
                if self.actual_trigger_contract is None
                else dict(self.actual_trigger_contract)
            ),
            "card_versions": [row.to_dict() for row in self.card_versions],
            "effect_rows": [
                self.effect_rows_by_id[id_].to_dict()
                for id_ in TARGET_EFFECT_IDS
                if id_ in self.effect_rows_by_id
            ],
            "summary": self.summary(),
        }


_CARD_SLOT_KEYS: Final = frozenset(
    {"produceExamTriggerId", "produceExamEffectId", "hideIcon", "isOncePlayEffect"}
)
_EFFECT_RAW_KEYS: Final = frozenset(
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


def _effect_spec(effect_id: str) -> dict[str, Any]:
    if effect_id in TARGET_BLOCK_EFFECT_IDS:
        upgrade = TARGET_BLOCK_EFFECT_IDS.index(effect_id)
        return {
            "effect_type": BLOCK_FIX_EFFECT_TYPE,
            "value1": TARGET_BLOCK_VALUE_BY_UPGRADE[upgrade],
            "value2": 0,
            "effect_count": 0,
            "effect_turn": 0,
            "status_enchant_id": "",
            "chain_effect_id": "",
            "effect_group_ids": ("effect_group-visible-exam_block-000",),
        }
    if effect_id == TARGET_PLAYABLE_EFFECT_ID:
        return {
            "effect_type": PLAYABLE_VALUE_ADD_EFFECT_TYPE,
            "value1": 0,
            "value2": 0,
            "effect_count": 1,
            "effect_turn": 0,
            "status_enchant_id": "",
            "chain_effect_id": "",
            "effect_group_ids": (
                "effect_group-visible-exam_playable_value_add-000",
            ),
        }
    raise KeyError(effect_id)


def _parse_effect_row(
    row: sqlite3.Row | None,
    effect_id: str,
    issues: list[str],
) -> Plan2ExamEffectRow | None:
    if row is None:
        issues.append(f"missing-target-effect:{effect_id}")
        return None
    raw = _json_value(row["raw_json"], f"effect:{effect_id}.raw_json", issues)
    if not isinstance(raw, Mapping):
        issues.append(f"effect-raw-object-required:{effect_id}")
        raw = {}
    if set(raw) != _EFFECT_RAW_KEYS:
        issues.append(f"effect-raw-key-shape:{effect_id}")
    spec = _effect_spec(effect_id)
    normalized = {
        "effect_type": row["effect_type"],
        "value1": row["value1"],
        "value2": row["value2"],
        "effect_count": row["effect_count"],
        "effect_turn": row["effect_turn"],
        "status_enchant_id": row["status_enchant_id"],
        "chain_effect_id": row["chain_effect_id"],
    }
    for key, expected in spec.items():
        if normalized.get(key, normalized.get(key)) != expected and key in normalized:
            issues.append(f"effect-shape-mismatch:{effect_id}:{key}")
    raw_expected = {
        "id": effect_id,
        "effectType": spec["effect_type"],
        "effectValue1": spec["value1"],
        "effectValue2": spec["value2"],
        "effectCount": spec["effect_count"],
        "effectTurn": spec["effect_turn"],
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
        "effectGroupIds": list(spec["effect_group_ids"]),
    }
    for key, expected in raw_expected.items():
        if raw.get(key) != expected:
            issues.append(f"effect-raw-mismatch:{effect_id}:{key}")
    for key in ("produceDescriptions", "customizeProduceDescriptions"):
        if not isinstance(raw.get(key), list) or any(
            not isinstance(item, Mapping) for item in raw.get(key, [])
        ):
            issues.append(f"effect-description-shape:{effect_id}:{key}")
    return Plan2ExamEffectRow(
        effect_id=effect_id,
        effect_type=row["effect_type"] if type(row["effect_type"]) is str else "",
        value1=row["value1"] if type(row["value1"]) is int else 0,
        value2=row["value2"] if type(row["value2"]) is int else 0,
        effect_count=row["effect_count"] if type(row["effect_count"]) is int else 0,
        effect_turn=row["effect_turn"] if type(row["effect_turn"]) is int else 0,
        status_enchant_id=(
            row["status_enchant_id"] if type(row["status_enchant_id"]) is str else ""
        ),
        chain_effect_id=(
            row["chain_effect_id"] if type(row["chain_effect_id"]) is str else ""
        ),
        effect_group_ids=tuple(raw.get("effectGroupIds", ()))
        if isinstance(raw.get("effectGroupIds"), list)
        else (),
        produce_description_count=(
            len(raw["produceDescriptions"])
            if isinstance(raw.get("produceDescriptions"), list)
            else 0
        ),
        customize_description_count=(
            len(raw["customizeProduceDescriptions"])
            if isinstance(raw.get("customizeProduceDescriptions"), list)
            else 0
        ),
    )


def _parse_card_effect_slots(
    card_id: str,
    upgrade: int,
    value: object,
    issues: list[str],
) -> tuple[Plan2CardEffectSlot, ...]:
    parsed = _json_list(value, f"card:{card_id}#{upgrade}.play_effects", issues)
    if len(parsed) != 2:
        issues.append(f"card-effect-slot-count:{card_id}#{upgrade}")
    expected_ids = (
        TARGET_BLOCK_EFFECT_IDS[upgrade]
        if upgrade in TARGET_UPGRADES
        else "<unknown-block-effect>",
        TARGET_PLAYABLE_EFFECT_ID,
    )
    expected_triggers = ("", TRIGGER_ID)
    slots: list[Plan2CardEffectSlot] = []
    for index, item in enumerate(parsed):
        if not isinstance(item, Mapping):
            issues.append(f"card-effect-slot-not-object:{card_id}#{upgrade}:{index}")
            continue
        if set(item) != _CARD_SLOT_KEYS:
            issues.append(f"card-effect-slot-key-shape:{card_id}#{upgrade}:{index}")
        effect_id = item.get("produceExamEffectId")
        trigger_id = item.get("produceExamTriggerId")
        hide_icon = item.get("hideIcon")
        once = item.get("isOncePlayEffect")
        if index < 2 and effect_id != expected_ids[index]:
            issues.append(f"card-effect-id-mismatch:{card_id}#{upgrade}:{index}")
        if index < 2 and trigger_id != expected_triggers[index]:
            issues.append(f"card-effect-trigger-mismatch:{card_id}#{upgrade}:{index}")
        if type(effect_id) is not str or not effect_id:
            issues.append(f"card-effect-id-invalid:{card_id}#{upgrade}:{index}")
            effect_id = ""
        if type(trigger_id) is not str:
            issues.append(f"card-effect-trigger-invalid:{card_id}#{upgrade}:{index}")
            trigger_id = ""
        if hide_icon is not False:
            issues.append(f"card-effect-hide-icon-shape:{card_id}#{upgrade}:{index}")
        if once is not False:
            issues.append(f"card-effect-once-shape:{card_id}#{upgrade}:{index}")
        slots.append(
            Plan2CardEffectSlot(
                slot_index=index,
                effect_id=effect_id,
                trigger_id=trigger_id,
                hide_icon=hide_icon if type(hide_icon) is bool else False,
                is_once_play_effect=once if type(once) is bool else False,
            )
        )
    return tuple(slots)


def _parse_card(row: sqlite3.Row, issues: list[str]) -> Plan2CardVersion:
    card_id = _text(row["id"], "card.id", issues)
    upgrade = _plain_int(row["upgrade_count"], f"card:{card_id}.upgrade", issues)
    raw = _json_value(row["raw_json"], f"card:{card_id}#{upgrade}.raw_json", issues)
    if not isinstance(raw, Mapping):
        issues.append(f"card-raw-object-required:{card_id}#{upgrade}")
        raw = {}
    move_trigger = raw.get("moveEffectTriggerType")
    move_effect_ids = raw.get("moveProduceExamEffectIds")
    if move_trigger != "ProduceCardMoveEffectTriggerType_Unknown":
        issues.append(f"card-move-trigger-shape:{card_id}#{upgrade}")
    if move_effect_ids != []:
        issues.append(f"card-move-effect-shape:{card_id}#{upgrade}")
    if type(move_trigger) is not str:
        move_trigger = ""
    if not isinstance(move_effect_ids, list) or any(
        type(value) is not str for value in move_effect_ids
    ):
        issues.append(f"card-move-effect-ids-shape:{card_id}#{upgrade}")
        move_effect_ids = []
    return Plan2CardVersion(
        card_id=card_id,
        upgrade_count=upgrade,
        name=_text(row["name"], f"card:{card_id}.name", issues),
        plan_type=_text(row["plan_type"], f"card:{card_id}.plan_type", issues),
        category=_text(row["category"], f"card:{card_id}.category", issues),
        stamina=_plain_int(row["stamina"], f"card:{card_id}.stamina", issues),
        cost_type=_text(row["cost_type"], f"card:{card_id}.cost_type", issues),
        cost_value=_plain_int(row["cost_value"], f"card:{card_id}.cost_value", issues),
        play_trigger_id=_text(
            row["play_trigger_id"], f"card:{card_id}.play_trigger_id", issues
        ),
        move_position_type=_text(
            row["move_position_type"], f"card:{card_id}.move_position_type", issues
        ),
        move_effect_trigger_type=move_trigger,
        move_effect_ids=tuple(move_effect_ids),
        effect_slots=_parse_card_effect_slots(
            card_id, upgrade, row["play_effects_json"], issues
        ),
    )


def _validate_card(card: Plan2CardVersion, issues: list[str]) -> None:
    label = card.ref
    if card.card_id != TARGET_CARD_ID:
        issues.append(f"card-id-mismatch:{label}")
    if card.upgrade_count not in TARGET_UPGRADES:
        issues.append(f"card-upgrade-out-of-scope:{label}")
    elif card.name != TARGET_CARD_NAMES[card.upgrade_count]:
        issues.append(f"card-name-mismatch:{label}")
    if card.plan_type != COMMON_PLAN_TYPE:
        issues.append(f"card-plan-not-common:{label}")
    if card.category != MENTAL_SKILL_CATEGORY:
        issues.append(f"card-category-mismatch:{label}")
    if card.stamina != 0:
        issues.append(f"card-stamina-mismatch:{label}")
    if card.cost_type != "ExamCostType_Unknown" or card.cost_value != 0:
        issues.append(f"card-cost-shape:{label}")
    if card.play_trigger_id != "":
        issues.append(f"card-play-trigger-not-empty:{label}")
    if card.move_position_type != CARD_MOVE_LOST:
        issues.append(f"card-move-position-mismatch:{label}")
    if card.ordered_effect_ids != (
        (TARGET_BLOCK_EFFECT_IDS[card.upgrade_count], TARGET_PLAYABLE_EFFECT_ID)
        if card.upgrade_count in TARGET_UPGRADES
        else ()
    ):
        issues.append(f"card-ordered-effects-mismatch:{label}")


def load_plan2_stamina_up500_block_fix_catalog(
    database: Path | str = DEFAULT_DATABASE,
) -> Plan2StaminaUp500BlockFixCatalog:
    """Read the exact target rows from Master through a read-only connection."""

    path = Path(database)
    if not path.is_file():
        raise CatalogContractError(f"Master database not found: {path}")
    issues: list[str] = []
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as error:
        raise CatalogContractError(f"cannot open Master read-only: {path}") from error
    connection.row_factory = sqlite3.Row
    with closing(connection):
        trigger_row = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?", (TRIGGER_ID,)
        ).fetchone()
        actual_trigger_contract = _master_trigger_contract(trigger_row, issues)
        issues.extend(
            _shape_differences(
                actual_trigger_contract, NONE_STAMINA_UP_MULTIPLE_500.master_contract()
            )
        )
        raw_cards = connection.execute(
            "SELECT id, upgrade_count, name, plan_type, category, stamina, "
            "cost_type, cost_value, play_trigger_id, move_position_type, "
            "play_effects_json, raw_json FROM card WHERE id = ? "
            "ORDER BY upgrade_count",
            (TARGET_CARD_ID,),
        ).fetchall()
        parsed_cards: list[Plan2CardVersion] = []
        for raw_card in raw_cards:
            card = _parse_card(raw_card, issues)
            parsed_cards.append(card)
            _validate_card(card, issues)
        target_cards = tuple(
            card
            for card in parsed_cards
            if card.upgrade_count in TARGET_UPGRADES
        )
        if len(target_cards) != len(TARGET_UPGRADES):
            issues.append(f"unexpected-target-card-version-count:{len(target_cards)}")
        if tuple(card.upgrade_count for card in target_cards) != TARGET_UPGRADES:
            issues.append("target-upgrades-not-exact-0-1-2-3")

        placeholders = ",".join("?" for _ in TARGET_EFFECT_IDS)
        effect_rows = connection.execute(
            f"SELECT * FROM effect WHERE id IN ({placeholders})", TARGET_EFFECT_IDS
        ).fetchall()
        effect_by_id = {str(row["id"]): row for row in effect_rows}
        parsed_effects: dict[str, Plan2ExamEffectRow] = {}
        for effect_id in TARGET_EFFECT_IDS:
            parsed = _parse_effect_row(effect_by_id.get(effect_id), effect_id, issues)
            if parsed is not None:
                parsed_effects[effect_id] = parsed

    return Plan2StaminaUp500BlockFixCatalog(
        database=str(path),
        trigger=NONE_STAMINA_UP_MULTIPLE_500,
        actual_trigger_contract=actual_trigger_contract,
        card_versions=target_cards,
        effect_rows_by_id=parsed_effects,
        shape_issues=tuple(dict.fromkeys(issues)),
    )


load_catalog = load_plan2_stamina_up500_block_fix_catalog


@dataclass(frozen=True, slots=True)
class Plan2BlockFixRuntime:
    """Only the native fields touched by AddBlockFix/SetBlock."""

    block: int = 0
    block_consumption_sum_count: int = 0

    def __post_init__(self) -> None:
        _plain_i32(self.block, "block")
        _plain_i32(
            self.block_consumption_sum_count,
            "block_consumption_sum_count",
        )


@dataclass(frozen=True, slots=True)
class Plan2BlockDifference:
    kind: Literal["block", "effect"]
    preview: int
    current: int
    block_consumption_sum_count: int | None
    status_effect_type: int | None
    is_consumption: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "preview": self.preview,
            "current": self.current,
            "block_consumption_sum_count": self.block_consumption_sum_count,
            "status_effect_type": self.status_effect_type,
            "is_consumption": self.is_consumption,
        }


@dataclass(frozen=True, slots=True)
class Plan2BlockFixTransition:
    before: Plan2BlockFixRuntime
    after: Plan2BlockFixRuntime
    effect: Plan2ExamEffectRow
    negative_current_block: int
    effective_value: int
    differences: tuple[Plan2BlockDifference, ...]
    callback_order: tuple[str, ...]
    direct_callbacks: tuple[str, ...]
    event_trace: tuple[str, ...]

    @property
    def block_difference(self) -> int:
        return self.after.block - self.before.block

    def to_dict(self) -> dict[str, Any]:
        return {
            "before": {
                "block": self.before.block,
                "block_consumption_sum_count": self.before.block_consumption_sum_count,
            },
            "after": {
                "block": self.after.block,
                "block_consumption_sum_count": self.after.block_consumption_sum_count,
            },
            "effect": self.effect.to_dict(),
            "negative_current_block": self.negative_current_block,
            "effective_value": self.effective_value,
            "differences": [item.to_dict() for item in self.differences],
            "callback_order": list(self.callback_order),
            "direct_callbacks": list(self.direct_callbacks),
            "event_trace": list(self.event_trace),
        }


def execute_plan2_block_fix(
    runtime: Plan2BlockFixRuntime,
    effect: Plan2ExamEffectRow,
) -> Plan2BlockFixTransition:
    """Execute the proven native AddBlockFix tail and its difference order.

    The direct target executor does not call ``CalculateAddBlock``.  It uses
    the supplied effect value as-is, takes ``max(i32(-Block), value1)``, then
    calls ``SetBlock(newBlock, isConsumption=false)``.  There is no independent
    native upper cap in the evidence; a projected signed-Int32 wrap is refused
    here because the normal non-negative runtime domain cannot be claimed past
    that boundary.
    """

    if not isinstance(runtime, Plan2BlockFixRuntime):
        raise NativeBoundaryError("unknown-block-fix-runtime-shape")
    if not isinstance(effect, Plan2ExamEffectRow):
        raise NativeBoundaryError("unknown-block-fix-effect-shape")
    spec = _effect_spec(effect.effect_id) if effect.effect_id in TARGET_EFFECT_IDS else None
    if (
        spec is None
        or effect.effect_type != BLOCK_FIX_EFFECT_TYPE
        or any(getattr(effect, key) != spec[key] for key in (
            "value1",
            "value2",
            "effect_count",
            "effect_turn",
            "status_enchant_id",
            "chain_effect_id",
            "effect_group_ids",
        ))
    ):
        raise NativeBoundaryError("unknown-or-mismatched-block-fix-effect-shape")
    if runtime.block < 0:
        raise NativeBoundaryError("negative-block-runtime-unproven")
    if runtime.block_consumption_sum_count < 0:
        raise NativeBoundaryError("negative-block-consumption-sum-unproven")

    negative_current = _signed_i32(-runtime.block)
    effective = max(negative_current, effect.value1)
    candidate = runtime.block + effective
    if candidate > INT32_MAX:
        raise NativeBoundaryError("block-fix-output-signed-int32-wrap-unproven")
    if candidate < INT32_MIN:
        raise NativeBoundaryError("block-fix-output-outside-signed-int32")
    after = replace(runtime, block=candidate)
    block_difference = Plan2BlockDifference(
        kind="block",
        preview=runtime.block,
        current=after.block,
        block_consumption_sum_count=runtime.block_consumption_sum_count,
        status_effect_type=None,
        is_consumption=False,
    )
    effect_difference = Plan2BlockDifference(
        kind="effect",
        preview=runtime.block,
        current=after.block,
        block_consumption_sum_count=None,
        status_effect_type=BLOCK_DIFFERENCE_STATUS_EFFECT_TYPE,
        is_consumption=False,
    )
    callback_order = (
        "AddBlockFix:append:block-difference",
        "AddBlockFix:SetBlock(isConsumption=false)",
        "ExamEffectExecutor:append:effect-difference(statusEffectType=3)",
        "ExamSequence:EffectDifferenceExecutedAsync(deferred)",
    )
    return Plan2BlockFixTransition(
        before=runtime,
        after=after,
        effect=effect,
        negative_current_block=negative_current,
        effective_value=effective,
        differences=(block_difference, effect_difference),
        callback_order=callback_order,
        direct_callbacks=(),
        event_trace=callback_order,
    )


execute_block_fix = execute_plan2_block_fix


@dataclass(frozen=True, slots=True)
class Plan2CardRuntime:
    block: int = 0
    block_consumption_sum_count: int = 0
    remain_can_play_card_count: int = 0
    current_stamina: int = 0
    max_stamina: int = 1

    def __post_init__(self) -> None:
        for name in (
            "block",
            "block_consumption_sum_count",
            "remain_can_play_card_count",
            "current_stamina",
            "max_stamina",
        ):
            _plain_i32(getattr(self, name), name)

    def to_dict(self) -> dict[str, int]:
        return {
            "block": self.block,
            "block_consumption_sum_count": self.block_consumption_sum_count,
            "remain_can_play_card_count": self.remain_can_play_card_count,
            "current_stamina": self.current_stamina,
            "max_stamina": self.max_stamina,
        }


@dataclass(frozen=True, slots=True)
class Plan2CardPlayTransition:
    card_id: str
    upgrade_count: int
    execution_kind: CardExecutionKind
    before: Plan2CardRuntime
    after: Plan2CardRuntime
    trigger: StaminaUp500TriggerEvaluation | None
    block_fix: Plan2BlockFixTransition | None
    playable_value_added: int
    committed: bool
    reasons: tuple[str, ...] = ()
    event_trace: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "execution_kind", CardExecutionKind(self.execution_kind))
        object.__setattr__(self, "reasons", tuple(dict.fromkeys(self.reasons)))
        object.__setattr__(self, "event_trace", tuple(self.event_trace))

    @property
    def supported(self) -> bool:
        return self.committed and not self.reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "upgrade_count": self.upgrade_count,
            "execution_kind": self.execution_kind.value,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "trigger": None if self.trigger is None else self.trigger.to_dict(),
            "block_fix": None if self.block_fix is None else self.block_fix.to_dict(),
            "playable_value_added": self.playable_value_added,
            "committed": self.committed,
            "reasons": list(self.reasons),
            "event_trace": list(self.event_trace),
        }


def execute_plan2_target_card(
    catalog: Plan2StaminaUp500BlockFixCatalog,
    runtime: Plan2CardRuntime,
    *,
    card_id: str = TARGET_CARD_ID,
    upgrade_count: int = 0,
    execution_kind: CardExecutionKind | str = CardExecutionKind.ORDINARY,
) -> Plan2CardPlayTransition:
    """Run the two ordered target effects for an ordinary card play.

    The trigger is selected from the immutable pre-payment snapshot.  Forced
    and extra-play paths have no target-specific native proof and therefore
    return an unchanged state with ``committed=False``.
    """

    if not isinstance(catalog, Plan2StaminaUp500BlockFixCatalog):
        raise CatalogContractError("unknown-plan2-catalog-shape")
    if not isinstance(runtime, Plan2CardRuntime):
        raise NativeBoundaryError("unknown-card-runtime-shape")
    try:
        kind = CardExecutionKind(execution_kind)
    except (TypeError, ValueError):
        kind = CardExecutionKind.ORDINARY
        return Plan2CardPlayTransition(
            card_id=card_id,
            upgrade_count=upgrade_count,
            execution_kind=kind,
            before=runtime,
            after=runtime,
            trigger=None,
            block_fix=None,
            playable_value_added=0,
            committed=False,
            reasons=("execution-kind-unproven",),
            event_trace=("card-play:rejected-before-effect-0",),
        )
    card = catalog.card(card_id, upgrade_count)
    if card is None:
        return Plan2CardPlayTransition(
            card_id=card_id,
            upgrade_count=upgrade_count,
            execution_kind=kind,
            before=runtime,
            after=runtime,
            trigger=None,
            block_fix=None,
            playable_value_added=0,
            committed=False,
            reasons=("unknown-plan-card-or-version",),
            event_trace=("card-play:rejected-before-effect-0",),
        )
    if not catalog.exact_shape_supported:
        return Plan2CardPlayTransition(
            card_id=card_id,
            upgrade_count=upgrade_count,
            execution_kind=kind,
            before=runtime,
            after=runtime,
            trigger=None,
            block_fix=None,
            playable_value_added=0,
            committed=False,
            reasons=("master-shape-not-exact",),
            event_trace=("card-play:rejected-before-effect-0",),
        )

    snapshot = Plan2NoneStaminaSnapshot(
        runtime.current_stamina,
        runtime.max_stamina,
        TriggerSnapshotBoundary.PRE_PAYMENT_CARD_PLAY,
        PHASE_NONE,
    )
    trigger_result = evaluate_plan2_none_stamina_up_multiple(
        catalog.trigger,
        snapshot,
        event_phase=PHASE_NONE,
        execution_kind=kind,
    )
    base_trace = (
        "ordinary-card-play:trigger-snapshot:pre-payment"
        if kind is CardExecutionKind.ORDINARY
        else f"{kind.value}:trigger-snapshot:unproven"
    )
    if not trigger_result.supported:
        return Plan2CardPlayTransition(
            card_id=card_id,
            upgrade_count=upgrade_count,
            execution_kind=kind,
            before=runtime,
            after=runtime,
            trigger=trigger_result,
            block_fix=None,
            playable_value_added=0,
            committed=False,
            reasons=trigger_result.reasons,
            event_trace=(base_trace, "card-play:rejected-before-effect-0"),
        )

    effect = catalog.effect_rows_by_id.get(card.effect_slots[0].effect_id)
    if effect is None:
        return Plan2CardPlayTransition(
            card_id=card_id,
            upgrade_count=upgrade_count,
            execution_kind=kind,
            before=runtime,
            after=runtime,
            trigger=trigger_result,
            block_fix=None,
            playable_value_added=0,
            committed=False,
            reasons=("ordered-block-fix-effect-unavailable",),
            event_trace=(base_trace, "card-play:rejected-before-effect-0"),
        )
    try:
        block_transition = execute_plan2_block_fix(
            Plan2BlockFixRuntime(
                runtime.block, runtime.block_consumption_sum_count
            ),
            effect,
        )
        working = replace(
            runtime,
            block=block_transition.after.block,
            block_consumption_sum_count=block_transition.after.block_consumption_sum_count,
        )
        playable_added = 0
        trace = [base_trace, "ordinary-card-play:payment:settled"]
        trace.append(
            f"ordinary-card-play:effect-slot:0:{effect.effect_id}:BlockFix"
        )
        trace.extend(block_transition.event_trace)
        if trigger_result.fires:
            playable = catalog.effect_rows_by_id.get(TARGET_PLAYABLE_EFFECT_ID)
            if playable is None or (
                playable.effect_type != PLAYABLE_VALUE_ADD_EFFECT_TYPE
                or playable.effect_count != 1
                or playable.value1 != 0
                or playable.value2 != 0
            ):
                raise NativeBoundaryError("ordered-playable-value-add-shape-unproven")
            if working.remain_can_play_card_count > INT32_MAX - playable.effect_count:
                raise NativeBoundaryError("playable-value-add-output-signed-int32-wrap-unproven")
            playable_added = playable.effect_count
            working = replace(
                working,
                remain_can_play_card_count=(
                    working.remain_can_play_card_count + playable_added
                ),
            )
            trace.append(
                f"ordinary-card-play:effect-slot:1:{playable.effect_id}:PlayableValueAdd"
            )
        else:
            trace.append(
                "ordinary-card-play:effect-slot:1:trigger-false:skip:"
                f"{TARGET_PLAYABLE_EFFECT_ID}"
            )
        trace.extend(
            (
                "ordinary-card-play:post-effects:EffectDifferenceExecutedAsync(deferred)",
                "ordinary-card-play:move:ProduceCardMovePositionType_Lost",
            )
        )
    except NativeBoundaryError as error:
        return Plan2CardPlayTransition(
            card_id=card_id,
            upgrade_count=upgrade_count,
            execution_kind=kind,
            before=runtime,
            after=runtime,
            trigger=trigger_result,
            block_fix=None,
            playable_value_added=0,
            committed=False,
            reasons=(str(error),),
            event_trace=(base_trace, "card-play:rejected-before-effect-0"),
        )
    return Plan2CardPlayTransition(
        card_id=card_id,
        upgrade_count=upgrade_count,
        execution_kind=kind,
        before=runtime,
        after=working,
        trigger=trigger_result,
        block_fix=block_transition,
        playable_value_added=playable_added,
        committed=True,
        event_trace=tuple(trace),
    )


execute_card = execute_plan2_target_card


@dataclass(frozen=True, slots=True)
class _FormalCoverageRow:
    ref: str
    gaps: tuple[str, ...]


def _load_formal_coverage_rows(path: Path) -> tuple[_FormalCoverageRow, ...]:
    if not path.is_file():
        raise CoverageContractError(f"formal coverage not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise CoverageContractError(f"formal coverage is not valid JSON: {path}") from error
    cards = payload.get("cards") if isinstance(payload, Mapping) else None
    if not isinstance(cards, list):
        raise CoverageContractError("formal coverage cards list unavailable")
    rows: list[_FormalCoverageRow] = []
    seen: set[str] = set()
    for index, row in enumerate(cards):
        if not isinstance(row, Mapping):
            raise CoverageContractError(f"formal coverage row {index} is not an object")
        card_id = row.get("card_id")
        upgrade = row.get("upgrade")
        gaps = row.get("gaps")
        integration_overlay = row.get(
            "plan2_stamina_timer_move_batch_overlay"
        )
        if isinstance(integration_overlay, Mapping):
            families = integration_overlay.get("families")
            previous_gaps = integration_overlay.get("previous_gaps")
            if (
                isinstance(families, list)
                and "stamina_up500_block_fix" in families
                and isinstance(previous_gaps, list)
                and all(type(gap) is str for gap in previous_gaps)
            ):
                # Once the central integration has landed, recover the exact
                # pre-batch slice retained by its historical overlay.  This
                # keeps this standalone audit reproducible without pretending
                # the current formal gaps are still unresolved.
                gaps = previous_gaps
        if type(card_id) is not str or type(upgrade) is not int:
            raise CoverageContractError(f"formal coverage row {index} identity is invalid")
        if not isinstance(gaps, list) or any(type(gap) is not str for gap in gaps):
            raise CoverageContractError(f"formal coverage row {index} gaps are invalid")
        ref = f"{card_id}#{upgrade}"
        if ref in seen:
            raise CoverageContractError(f"duplicate formal coverage ref: {ref}")
        seen.add(ref)
        rows.append(_FormalCoverageRow(ref, tuple(gaps)))
    return tuple(rows)


def build_standalone_coverage(
    catalog: Plan2StaminaUp500BlockFixCatalog,
    formal_coverage_path: Path | str = DEFAULT_FORMAL_COVERAGE_PATH,
) -> dict[str, Any]:
    """Compute family/co-block/overlap counts from formal rows, never constants."""

    if not isinstance(catalog, Plan2StaminaUp500BlockFixCatalog):
        raise CoverageContractError("unknown catalog shape")
    rows = _load_formal_coverage_rows(Path(formal_coverage_path))
    by_ref = {row.ref: row for row in rows}
    target_refs = tuple(row.ref for row in catalog.card_versions)
    if not target_refs or len(set(target_refs)) != len(target_refs):
        raise CoverageContractError("catalog target refs are unavailable or duplicated")
    for ref in target_refs:
        if ref not in by_ref:
            raise CoverageContractError(f"target ref missing from formal coverage: {ref}")

    both_refs = {
        row.ref
        for row in rows
        if FORMAL_TRIGGER_GAP in row.gaps and FORMAL_BLOCK_FIX_GAP in row.gaps
    }
    if both_refs != set(target_refs):
        raise CoverageContractError(
            "formal coverage overlap differs from the exact Master target versions"
        )

    def family(gap: str) -> dict[str, Any]:
        affected = sorted(row.ref for row in rows if gap in row.gaps)
        direct: list[str] = []
        co_blocked: list[str] = []
        remaining: dict[str, list[str]] = {}
        for ref in affected:
            after_fix = [item for item in by_ref[ref].gaps if item != gap]
            remaining[ref] = after_fix
            (direct if not after_fix else co_blocked).append(ref)
        return {
            "gap": gap,
            "affected_count": len(affected),
            "direct_count": len(direct),
            "co_blocked_count": len(co_blocked),
            "affected_refs": affected,
            "direct_refs": direct,
            "co_blocked_refs": co_blocked,
            "remaining_gaps_after_family_fix": remaining,
        }

    trigger_family = family(FORMAL_TRIGGER_GAP)
    block_family = family(FORMAL_BLOCK_FIX_GAP)
    trigger_set = set(trigger_family["affected_refs"])
    block_set = set(block_family["affected_refs"])
    overlap = sorted(trigger_set & block_set)
    union = sorted(trigger_set | block_set)
    combined_direct: list[str] = []
    combined_co: list[str] = []
    combined_remaining: dict[str, list[str]] = {}
    for ref in union:
        remaining = [
            item
            for item in by_ref[ref].gaps
            if item not in {FORMAL_TRIGGER_GAP, FORMAL_BLOCK_FIX_GAP}
        ]
        combined_remaining[ref] = remaining
        (combined_direct if not remaining else combined_co).append(ref)
    direct_alone = set(trigger_family["direct_refs"]) | set(
        block_family["direct_refs"]
    )
    overlap_unlocked = sorted(
        set(overlap) & set(combined_direct) - direct_alone
    )
    target_direct = sorted(set(target_refs) & set(combined_direct))
    target_co = sorted(set(target_refs) - set(target_direct))
    return {
        "formal_coverage_path": str(Path(formal_coverage_path)),
        "target_card_id": TARGET_CARD_ID,
        "family_reports": {
            "trigger": trigger_family,
            "block_fix": block_family,
        },
        "overlap": {
            "affected_count": len(overlap),
            "affected_refs": overlap,
            "union_count": len(union),
            "union_refs": union,
            "unlocked_only_after_combined_batch_count": len(overlap_unlocked),
            "unlocked_only_after_combined_batch_refs": overlap_unlocked,
        },
        "combined_batch": {
            "direct_executable_count": len(combined_direct),
            "direct_executable_refs": combined_direct,
            "co_blocked_count": len(combined_co),
            "co_blocked_refs": combined_co,
            "remaining_gaps_after_both_fixes": combined_remaining,
            "target_scope_direct_executable_count": len(target_direct),
            "target_scope_direct_executable_refs": target_direct,
            "target_scope_co_blocked_count": len(target_co),
            "target_scope_co_blocked_refs": target_co,
            "all_four_target_versions_direct_executable": (
                set(target_direct) == set(target_refs)
                and len(target_refs) == len(TARGET_UPGRADES)
                and catalog.exact_shape_supported
            ),
        },
    }


coverage_report = build_standalone_coverage


def build_native_audit(
    catalog: Plan2StaminaUp500BlockFixCatalog | None = None,
    formal_coverage_path: Path | str = DEFAULT_FORMAL_COVERAGE_PATH,
) -> dict[str, Any]:
    """Return the JSON-shaped evidence/audit payload for this leaf module."""

    catalog = catalog or load_catalog()
    coverage = build_standalone_coverage(catalog, formal_coverage_path)
    return {
        "schema_version": 1,
        "audit_id": "plan2-stamina-up500-block-fix",
        "scope": {
            "plan": "Plan2",
            "card_id": TARGET_CARD_ID,
            "card_name": TARGET_CARD_NAME,
            "upgrades": list(TARGET_UPGRADES),
            "formal_gaps": list(FORMAL_TARGET_GAPS),
            "central_coverage_rebuilt": False,
            "central_engine_modified": False,
            "native_search_modified": False,
            "plan3_modified": False,
            "gui_modified": False,
            "security_hash_clock_tamper_work_performed": False,
        },
        "master": {
            "trigger": catalog.trigger.master_contract(),
            "card_versions": [row.to_dict() for row in catalog.card_versions],
            "effect_rows": [
                catalog.effect_rows_by_id[id_].to_dict()
                for id_ in TARGET_EFFECT_IDS
                if id_ in catalog.effect_rows_by_id
            ],
            "shape_issues": list(catalog.shape_issues),
            "exact_shape_supported": catalog.exact_shape_supported,
        },
        "native_android_v323": {
            "trigger": {
                "method": "Campus.InGame.Exam.ExamExtensions.IsFieldStatusTriggerStatusEffect",
                "method_va": "0x68082D4",
                "field_type": TRIGGER_FIELD_TYPE,
                "field_enum_value": 4,
                "current_getter": "ExamParameterModel.get_Stamina",
                "max_getter": "ExamParameterModel.get_MaxStamina",
                "formula": "f32(signed Stamina) / f32(signed MaxStamina) >= f32(500) / f32(1000.0f)",
                "comparison": ">=",
                "inclusive": True,
                "signed_inputs": True,
                "float32_division": True,
                "integer_percentage_rounding": False,
                "invalid_domain_fail_closed": [
                    "max-stamina-nonpositive",
                    "current-stamina-negative",
                    "current-stamina-exceeds-max",
                    "non-finite-or-non-Int32-input",
                ],
            },
            "block_fix": {
                "effect_enum": "ProduceExamEffectType_ExamBlockFix",
                "effect_enum_value": 119,
                "executor_type": "Campus.InGame.Exam.BlockFixEffectExecutor",
                "executor_execute_va": "0x7E72AF8",
                "value_field": "_value@0x10 (int32)",
                "add_block_fix_va": "0x7E5EA94",
                "formula": "effective=max(i32(-Block), effectValue1); nextBlock=i32(Block+effective)",
                "lower_bound": "zero floor for non-negative normal Block state",
                "upper_bound": "no independent native cap proven; signed Int32 wrap is refused by this adapter",
                "state_fields": [
                    "ExamParameterModel.Block@0x1C8",
                    "ExamParameterModel.BlockConsumptionSumCount@0x36C",
                ],
                "set_block": "SetBlock(nextBlock, isConsumption=false); BlockConsumptionSumCount unchanged",
                "difference_type": "Block=2",
                "outer_status_effect_type": 3,
                "callback": "no direct callback; ExamSequence.EffectDifferenceExecutedAsync consumes the deferred difference list",
                "order": [
                    "read Block",
                    "max(-Block, value)",
                    "append block difference",
                    "SetBlock(isConsumption=false)",
                    "append effect difference(statusEffectType=3)",
                    "deferred EffectDifferenceExecutedAsync",
                ],
            },
        },
        "snapshot_and_execution": {
            "trigger_snapshot_boundary": TriggerSnapshotBoundary.PRE_PAYMENT_CARD_PLAY.value,
            "direct_effect_dispatch": "PlayEffect",
            "trigger_rechecked_at_effect": False,
            "ordinary": True,
            "normal": True,
            "forced": False,
            "extra": False,
            "forced_or_extra_reason": "target-specific native trigger/transaction proof unavailable",
        },
        "coverage": coverage,
        "implementation": {
            "module": "src/gkms_tool/plan2_stamina_up500_block_fix.py",
            "tests": "tests/test_plan2_stamina_up500_block_fix.py",
            "shared_formula": "gkms_tool.plan3_stamina_multiple_trigger.evaluate_stamina_multiple",
            "formal_coverage_source_read_only": True,
        },
        "evidence": [
            {
                "source": "var/master.sqlite3",
                "locator": "produce_exam_trigger/effect/card target rows",
            },
            {
                "source": "var/coverage/plan2_block_depend_block_consumption_sum_native_audit.json",
                "locator": "AddBlockFix/SetBlock non-consumption order",
            },
            {
                "source": "_research/android/game-v3.2.3/native-analysis/create-executor-mapping.json",
                "locator": "ExamBlockFix enum 119 / branch 0x7E5C720",
            },
            {
                "source": "_research/android/game-v3.2.3/il2cppdumper/dump.cs",
                "locator": "BlockFixEffectExecutor and ExamEffectUtility.AddBlockFix",
            },
            {
                "source": "docs/android-v323-card-transaction-order-report.md",
                "locator": "UseHand -> PlayEffect -> UserCardAfterCheck -> MovePlayCard; pre-payment trigger snapshot",
            },
        ],
    }


__all__ = [
    "BLOCK_DIFFERENCE_STATUS_EFFECT_TYPE",
    "BLOCK_FIX_EFFECT_TYPE",
    "CARD_ID",
    "CARD_MOVE_LOST",
    "CardExecutionKind",
    "CatalogContractError",
    "COMMON_PLAN_TYPE",
    "CoverageContractError",
    "DEFAULT_AUDIT_PATH",
    "DEFAULT_FORMAL_COVERAGE_PATH",
    "FORMAL_BLOCK_FIX_GAP",
    "FORMAL_TARGET_GAPS",
    "FORMAL_TRIGGER_GAP",
    "INT32_MAX",
    "INT32_MIN",
    "MENTAL_SKILL_CATEGORY",
    "NativeBoundaryError",
    "NATIVE_BRANCHES",
    "NATIVE_FORMULA_EVIDENCE",
    "PHASE_NONE",
    "Plan2BlockDifference",
    "Plan2BlockFixRuntime",
    "Plan2BlockFixTransition",
    "Plan2CardEffectSlot",
    "Plan2CardPlayTransition",
    "Plan2CardRuntime",
    "Plan2CardVersion",
    "Plan2ExamEffectRow",
    "Plan2NoneStaminaSnapshot",
    "Plan2StaminaSnapshot",
    "Plan2StaminaUp500BlockFixCatalog",
    "Plan2StaminaUp500BlockFixError",
    "PLAYABLE_VALUE_ADD_EFFECT_TYPE",
    "TARGET_BLOCK_EFFECT_IDS",
    "TARGET_BLOCK_VALUE_BY_UPGRADE",
    "TARGET_CARD_ID",
    "TARGET_CARD_NAME",
    "TARGET_CARD_NAMES",
    "TARGET_EFFECT_IDS",
    "TARGET_EFFECT_TYPES",
    "TARGET_PLAYABLE_EFFECT_ID",
    "TARGET_TRIGGER_ROW",
    "TARGET_TRIGGER_SHAPE",
    "TARGET_UPGRADES",
    "TRIGGER_FIELD_TYPE",
    "TRIGGER_ID",
    "TRIGGER_PHASE",
    "TRIGGER_PHASE_TYPE",
    "TRIGGER_THRESHOLD",
    "TriggerSnapshotBoundary",
    "StaminaUp500TriggerEvaluation",
    "adapt_plan2_none_stamina_snapshot",
    "build_native_audit",
    "build_standalone_coverage",
    "coverage_report",
    "evaluate",
    "evaluate_plan2_none_stamina_up_multiple",
    "evaluate_stamina_multiple",
    "evaluate_trigger",
    "execute_block_fix",
    "execute_card",
    "execute_plan2_block_fix",
    "execute_plan2_target_card",
    "load_catalog",
    "load_plan2_stamina_up500_block_fix_catalog",
]
