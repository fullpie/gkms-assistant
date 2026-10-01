"""Exact standalone semantics for Plan 3 ``ExamOverPreservation``.

The Android v3.2.3 executor does not bind any field from ``IExamEffect``.
It rejects Full Power, then calls ``SetStance(OverPreservation, 1)``.  The
shared stance utility treats Preservation and OverPreservation as one family:
moving from ordinary Preservation to OverPreservation does not release the
old stance and does not increment either stance-change counter.  Moving from
Neutral or Concentration does increment both counters before the native
status is changed.

``Plan3State`` represents native ``(OverPreservation, step=1)`` as
``(stance="preservation", stance_level=3)``.  The native projection below is
also public so signed Int32 wraparound remains executable even though the
current Plan3 state deliberately rejects negative counters.  The Plan3
adapter fails closed when that exact native result cannot be represented.

Phase/count callbacks are returned as ordered operations.  This module does
not invent an ``ExamStatusEffectCollection`` or execute arbitrary nested
enchant effects; a caller can consume the operations at its own scheduler
boundary without losing their native order.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, replace
from enum import Enum, IntEnum
import json
from pathlib import Path
import sqlite3
from typing import Any, TypeAlias

from .master_db import DEFAULT_DATABASE
from .plan3_engine import (
    Plan3State,
    STANCE_CONCENTRATION,
    STANCE_FULL_POWER,
    STANCE_NEUTRAL,
    STANCE_PRESERVATION,
)


EFFECT_ID = "e_effect-exam_over_preservation"
EFFECT_TYPE = "ProduceExamEffectType_ExamOverPreservation"
EFFECT_ENUM_VALUE = 167
EXECUTOR_TYPE = "Campus.InGame.Exam.OverPreservationEffectExecutor"
PLAN_TYPE = "ProducePlanType_Plan3"

NATIVE_TARGET_STATUS = 4
NATIVE_TARGET_STEP = 1
PLAN3_TARGET_STANCE = STANCE_PRESERVATION
PLAN3_TARGET_LEVEL = 3

PHASE_STANCE_CHANGE_COUNT_INTERVAL = 35
PHASE_STANCE_CHANGE_COUNT = 36
PHASE_STANCE_CHANGE_PRESERVATION_INTERVAL = 55

ANDROID_BINARY_SHA256 = (
    "107E2DA660CEA4193E0BDAC95920472E3CB1B8B2B38C6C894D6F7723FA1C802B"
)
PC_METADATA_SHA256 = (
    "71093CA43831321491C150A5767C4A39C2AAFE59023EB37F4368BE4C2EDF0884"
)
PC_GAME_ASSEMBLY_SHA256 = (
    "8BDA021D8198B9B70F28959E332D62D65263C7B1AF102133859643435D067B25"
)
PC_STAGE_ONE_RUNTIME_SHA256 = (
    "A52817CBC3BA696AA552D1BBD3D662963A72DE96D9F2E692B25E6D948584D44B"
)

INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1
UINT32_MASK = 2**32 - 1

EXECUTION_FIELD_ORDER = (
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
)

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...]


class OverPreservationContractError(ValueError):
    """A row or state cannot satisfy the pinned standalone contract."""


class NativeIdolStatus(IntEnum):
    UNKNOWN = 0
    CONCENTRATION = 1
    PRESERVATION = 2
    FULL_POWER = 3
    OVER_PRESERVATION = 4


class ExecutionStatus(str, Enum):
    APPLIED = "applied"
    NO_OP = "no_op"
    BLOCKED = "blocked"
    UNRESOLVED = "unresolved"


class OutcomeReason(str, Enum):
    APPLIED = "applied"
    FULL_POWER_GUARD = "full_power_guard"
    ALREADY_OVER_PRESERVATION = "already_over_preservation"
    GENERIC_STANCE_LOCK = "generic_stance_lock"
    PRESERVATION_STANCE_LOCK = "preservation_stance_lock"
    UNSUPPORTED_EFFECT_ROW = "unsupported_effect_row"
    INVALID_PLAN3_NATIVE_PROJECTION = "invalid_plan3_native_projection"
    PLAN3_COUNTER_WRAP_UNREPRESENTABLE = "plan3_counter_wrap_unrepresentable"


def _freeze_json(value: object) -> JsonValue:
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _json_value(value: JsonValue) -> object:
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _require_i32(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not INT32_MIN <= value <= INT32_MAX
    ):
        raise OverPreservationContractError(f"{field} must be a signed Int32")
    return value


def _add_i32(left: int, right: int) -> int:
    unsigned = (left + right) & UINT32_MASK
    return unsigned - 2**32 if unsigned > INT32_MAX else unsigned


@dataclass(frozen=True, slots=True)
class EffectField:
    name: str
    value: JsonValue

    def __post_init__(self) -> None:
        if not self.name:
            raise OverPreservationContractError("effect field name is empty")
        object.__setattr__(self, "value", _freeze_json(self.value))

    def to_json(self) -> dict[str, object]:
        return {"name": self.name, "value": _json_value(self.value)}


@dataclass(frozen=True, slots=True)
class OverPreservationEffectRow:
    source_effect_id: str
    effect_type: str
    fields: tuple[EffectField, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", tuple(self.fields))
        if tuple(field.name for field in self.fields) != EXECUTION_FIELD_ORDER:
            raise OverPreservationContractError(
                "effect fields do not retain Master execution order"
            )
        if self.field_value("id") != self.source_effect_id:
            raise OverPreservationContractError("effect id field disagrees")
        if self.field_value("effectType") != self.effect_type:
            raise OverPreservationContractError("effect type field disagrees")

    @property
    def effect_id(self) -> str:
        return self.source_effect_id

    @property
    def argument_fields(self) -> tuple[str, ...]:
        # The constructor tail-calls ExamEffectExecutorBase(.ctor) with null,
        # so even the all-zero Master payload is not read by this executor.
        return ()

    def field_value(self, name: str) -> JsonValue:
        for field in self.fields:
            if field.name == name:
                return field.value
        raise KeyError(name)

    @property
    def canonical(self) -> bool:
        if self.source_effect_id != EFFECT_ID or self.effect_type != EFFECT_TYPE:
            return False
        zero_fields = (
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
        return all(self.field_value(name) == 0 for name in zero_fields)

    def to_json(self) -> dict[str, object]:
        return {
            "source_effect_id": self.source_effect_id,
            "effect_type": self.effect_type,
            "argument_fields": list(self.argument_fields),
            "canonical": self.canonical,
            "fields": [field.to_json() for field in self.fields],
        }


@dataclass(frozen=True, slots=True)
class StatusEnchantConsumer:
    enchant_id: str
    trigger_id: str
    effect_ids: tuple[str, ...]
    owner_kind: str

    def to_json(self) -> dict[str, object]:
        return {
            "enchant_id": self.enchant_id,
            "trigger_id": self.trigger_id,
            "effect_ids": list(self.effect_ids),
            "owner_kind": self.owner_kind,
        }


@dataclass(frozen=True, slots=True)
class AffectedCardVersion:
    card_id: str
    upgrade_count: int
    name: str
    plan_type: str
    category: str
    effect_slot: int
    status_enchant_effect_id: str
    status_enchant_id: str
    trigger_id: str

    def to_json(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "upgrade_count": self.upgrade_count,
            "name": self.name,
            "plan_type": self.plan_type,
            "category": self.category,
            "effect_slot": self.effect_slot,
            "status_enchant_effect_id": self.status_enchant_effect_id,
            "status_enchant_id": self.status_enchant_id,
            "trigger_id": self.trigger_id,
        }


@dataclass(frozen=True, slots=True)
class NativeEvidence:
    factory_enum_value: int = EFFECT_ENUM_VALUE
    factory_va: str = "0x7E5BB60"
    factory_branch_target_va: str = "0x7E5D674"
    factory_type_global_va: str = "0xE757208"
    executor_type_usage_cell_va: str = "0xEB32008"
    constructor_va: str = "0x7E86B08"
    constructor_android_method_index: int = 18611
    constructor_android_token: str = "0x060048B4"
    execute_va: str = "0x7E86B10"
    execute_android_token: str = "0x060048B5"
    set_stance_va: str = "0x7E60940"
    try_internal_set_stance_va: str = "0x7E60948"
    increment_stance_count_va: str = "0x7E61FBC"
    set_over_preservation_va: str = "0x7EA467C"
    is_preservation_va: str = "0x680B3E0"
    pc_type_index: int = 3619
    pc_constructor_method_index: int = 18006
    pc_constructor_token: str = "0x06004657"
    pc_execute_method_index: int = 18007
    pc_execute_token: str = "0x06004658"
    pc_set_stance_method_index: int = 17602
    pc_set_stance_token: str = "0x060044C3"
    pc_try_internal_set_stance_method_index: int = 17603
    pc_try_internal_set_stance_token: str = "0x060044C4"
    pc_set_over_preservation_method_index: int = 18508
    pc_set_over_preservation_token: str = "0x0600484D"
    android_binary_sha256: str = ANDROID_BINARY_SHA256
    pc_metadata_sha256: str = PC_METADATA_SHA256
    pc_game_assembly_sha256: str = PC_GAME_ASSEMBLY_SHA256
    pc_stage_one_runtime_sha256: str = PC_STAGE_ONE_RUNTIME_SHA256
    pc_native_body_mapping_proven: bool = False
    uses_float_arithmetic: bool = False

    def to_json(self) -> dict[str, object]:
        return {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class Coverage:
    effect_rows_cataloged: int
    executable_effect_rows: int
    consumer_card_versions_cataloged: int
    effect_semantics_available_card_versions: int
    whole_card_versions_unlocked: int
    remaining_outer_trigger_gaps: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "effect_rows_cataloged": self.effect_rows_cataloged,
            "executable_effect_rows": self.executable_effect_rows,
            "consumer_card_versions_cataloged": self.consumer_card_versions_cataloged,
            "effect_semantics_available_card_versions": (
                self.effect_semantics_available_card_versions
            ),
            "whole_card_versions_unlocked": self.whole_card_versions_unlocked,
            "remaining_outer_trigger_gaps": list(self.remaining_outer_trigger_gaps),
        }


@dataclass(frozen=True, slots=True)
class OverPreservationCatalog:
    effect: OverPreservationEffectRow
    consumers: tuple[StatusEnchantConsumer, ...]
    affected_card_versions: tuple[AffectedCardVersion, ...]
    native: NativeEvidence
    coverage: Coverage

    def to_json(self) -> dict[str, object]:
        return {
            "effect": self.effect.to_json(),
            "status_enchant_consumers": [value.to_json() for value in self.consumers],
            "affected_card_versions": [
                value.to_json() for value in self.affected_card_versions
            ],
            "native": self.native.to_json(),
            "coverage": self.coverage.to_json(),
        }


@dataclass(frozen=True, slots=True)
class CallbackContext:
    """Inputs read by native callback gates plus ordered matching cards."""

    generic_stance_lock: int = 0
    preservation_stance_lock_turn: int = 0
    playing_item: bool = False
    playing_enchant_uid: int = 0
    playing_enchant_is_trigger_active: bool = False
    playing_gimmick: bool = False
    matching_card_status_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_i32(self.generic_stance_lock, "generic_stance_lock")
        _require_i32(
            self.preservation_stance_lock_turn,
            "preservation_stance_lock_turn",
        )
        _require_i32(self.playing_enchant_uid, "playing_enchant_uid")
        for field in (
            "playing_item",
            "playing_enchant_is_trigger_active",
            "playing_gimmick",
        ):
            if not isinstance(getattr(self, field), bool):
                raise OverPreservationContractError(f"{field} must be boolean")
        ids = tuple(self.matching_card_status_ids)
        if not all(isinstance(value, str) and value for value in ids):
            raise OverPreservationContractError(
                "matching_card_status_ids must contain non-empty strings"
            )
        object.__setattr__(self, "matching_card_status_ids", ids)

    @property
    def callback_dispatch_enabled(self) -> bool:
        return (
            not self.playing_item
            and not (
                self.playing_enchant_uid != 0
                and not self.playing_enchant_is_trigger_active
            )
            and not self.playing_gimmick
        )


@dataclass(frozen=True, slots=True)
class NativeSnapshot:
    status_type: NativeIdolStatus
    status_step: int
    stance_change_count: int
    preservation_change_count: int
    block: int
    enthusiasm: int
    plays_remaining: int
    full_power_points: int

    def __post_init__(self) -> None:
        try:
            status = NativeIdolStatus(self.status_type)
        except (TypeError, ValueError) as error:
            raise OverPreservationContractError("unsupported native status type") from error
        object.__setattr__(self, "status_type", status)
        for field in (
            "status_step",
            "stance_change_count",
            "preservation_change_count",
            "block",
            "enthusiasm",
            "plays_remaining",
            "full_power_points",
        ):
            _require_i32(getattr(self, field), field)
        expected_steps = {
            NativeIdolStatus.UNKNOWN: (0,),
            NativeIdolStatus.CONCENTRATION: (1, 2),
            NativeIdolStatus.PRESERVATION: (1, 2),
            NativeIdolStatus.FULL_POWER: (1,),
            NativeIdolStatus.OVER_PRESERVATION: (1,),
        }
        if self.status_step not in expected_steps[status]:
            raise OverPreservationContractError(
                "status step is not representable for the native status type"
            )


@dataclass(frozen=True, slots=True)
class TraceOperation:
    sequence: int
    kind: str
    values: tuple[JsonScalar, ...] = ()

    def to_json(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "values": list(self.values),
        }


@dataclass(frozen=True, slots=True)
class NativeExecutionResult:
    status: ExecutionStatus
    reason: OutcomeReason
    before: NativeSnapshot
    after: NativeSnapshot
    changed: bool
    operations: tuple[TraceOperation, ...]

    @property
    def difference_kinds(self) -> tuple[str, ...]:
        return tuple(
            operation.kind
            for operation in self.operations
            if operation.kind.startswith("difference.append")
        )


@dataclass(frozen=True, slots=True)
class Plan3ExecutionResult:
    status: ExecutionStatus
    reason: OutcomeReason
    state_before: Plan3State
    state_after: Plan3State
    native_result: NativeExecutionResult | None

    @property
    def changed(self) -> bool:
        return self.state_after != self.state_before

    @property
    def resolved(self) -> bool:
        return self.status is not ExecutionStatus.UNRESOLVED


def _load_effect_row(connection: sqlite3.Connection) -> OverPreservationEffectRow:
    row = connection.execute(
        "SELECT raw_json FROM effect WHERE id = ? AND effect_type = ?",
        (EFFECT_ID, EFFECT_TYPE),
    ).fetchone()
    if row is None:
        raise OverPreservationContractError("canonical Master effect row is missing")
    payload = json.loads(row[0])
    if not isinstance(payload, dict):
        raise OverPreservationContractError("effect raw_json is not an object")
    try:
        fields = tuple(
            EffectField(name, _freeze_json(payload[name]))
            for name in EXECUTION_FIELD_ORDER
        )
    except KeyError as error:
        raise OverPreservationContractError(
            f"canonical effect field is missing: {error.args[0]}"
        ) from error
    result = OverPreservationEffectRow(EFFECT_ID, EFFECT_TYPE, fields)
    if not result.canonical:
        raise OverPreservationContractError("Master row does not have canonical shape")
    return result


def _load_consumers(
    connection: sqlite3.Connection,
) -> tuple[StatusEnchantConsumer, ...]:
    rows: list[StatusEnchantConsumer] = []
    for row in connection.execute(
        "SELECT id, produce_exam_trigger_id, produce_exam_effect_ids_json "
        "FROM produce_exam_status_enchant ORDER BY id"
    ):
        effect_ids = tuple(json.loads(row[2]))
        if EFFECT_ID not in effect_ids:
            continue
        owner_kind = "card" if row[0].startswith("enchant-p_card-") else "item"
        rows.append(StatusEnchantConsumer(row[0], row[1], effect_ids, owner_kind))
    return tuple(rows)


def _load_affected_cards(
    connection: sqlite3.Connection,
    consumers: tuple[StatusEnchantConsumer, ...],
) -> tuple[AffectedCardVersion, ...]:
    card_consumers = {
        value.enchant_id: value for value in consumers if value.owner_kind == "card"
    }
    effect_to_consumer: dict[str, StatusEnchantConsumer] = {}
    for row in connection.execute(
        "SELECT id, status_enchant_id FROM effect WHERE status_enchant_id != ''"
    ):
        consumer = card_consumers.get(row[1])
        if consumer is not None:
            effect_to_consumer[row[0]] = consumer
    cards: list[AffectedCardVersion] = []
    for row in connection.execute(
        "SELECT id, upgrade_count, name, plan_type, category, play_effects_json "
        "FROM card ORDER BY id, upgrade_count"
    ):
        for slot, effect in enumerate(json.loads(row[5])):
            effect_id = effect.get("produceExamEffectId", "")
            consumer = effect_to_consumer.get(effect_id)
            if consumer is None:
                continue
            cards.append(
                AffectedCardVersion(
                    card_id=row[0],
                    upgrade_count=row[1],
                    name=row[2],
                    plan_type=row[3],
                    category=row[4],
                    effect_slot=slot,
                    status_enchant_effect_id=effect_id,
                    status_enchant_id=consumer.enchant_id,
                    trigger_id=consumer.trigger_id,
                )
            )
    return tuple(cards)


def load_over_preservation_catalog(
    database: str | Path = DEFAULT_DATABASE,
) -> OverPreservationCatalog:
    with closing(sqlite3.connect(Path(database))) as connection:
        effect = _load_effect_row(connection)
        consumers = _load_consumers(connection)
        cards = _load_affected_cards(connection, consumers)
    trigger_gaps = tuple(
        sorted(
            {
                "trigger-start-play-shape:e_trigger-start_play-preservation_up",
            }
        )
    )
    coverage = Coverage(
        effect_rows_cataloged=1,
        executable_effect_rows=1,
        consumer_card_versions_cataloged=len(cards),
        effect_semantics_available_card_versions=len(cards),
        whole_card_versions_unlocked=1,
        remaining_outer_trigger_gaps=trigger_gaps,
    )
    return OverPreservationCatalog(effect, consumers, cards, NativeEvidence(), coverage)


def _op(
    operations: list[TraceOperation], kind: str, *values: JsonScalar
) -> None:
    operations.append(TraceOperation(len(operations), kind, tuple(values)))


def execute_native_over_preservation(
    before: NativeSnapshot,
    *,
    context: CallbackContext | None = None,
) -> NativeExecutionResult:
    """Execute the native effect projection, including signed Int32 wrap."""

    if not isinstance(before, NativeSnapshot):
        raise TypeError("before must be NativeSnapshot")
    context = context or CallbackContext()
    if not isinstance(context, CallbackContext):
        raise TypeError("context must be CallbackContext")
    operations: list[TraceOperation] = []
    _op(operations, "executor.read_idol_status", int(before.status_type))

    # OverPreservationEffectExecutor checks this before calling SetStance.
    if before.status_type is NativeIdolStatus.FULL_POWER:
        _op(operations, "executor.guard_full_power.blocked")
        return NativeExecutionResult(
            ExecutionStatus.BLOCKED,
            OutcomeReason.FULL_POWER_GUARD,
            before,
            before,
            False,
            tuple(operations),
        )

    _op(
        operations,
        "utility.call_set_stance",
        NATIVE_TARGET_STATUS,
        NATIVE_TARGET_STEP,
    )
    if before.status_type is NativeIdolStatus.OVER_PRESERVATION:
        _op(operations, "utility.guard_same_status_step_max.blocked")
        _op(operations, "executor.skip_effect_marker")
        return NativeExecutionResult(
            ExecutionStatus.NO_OP,
            OutcomeReason.ALREADY_OVER_PRESERVATION,
            before,
            before,
            False,
            tuple(operations),
        )
    _op(operations, "utility.guard_same_status_step_max.passed")

    if context.generic_stance_lock > 0:
        _op(
            operations,
            "utility.guard_generic_stance_lock.blocked",
            context.generic_stance_lock,
        )
        _op(operations, "executor.skip_effect_marker")
        return NativeExecutionResult(
            ExecutionStatus.BLOCKED,
            OutcomeReason.GENERIC_STANCE_LOCK,
            before,
            before,
            False,
            tuple(operations),
        )
    _op(operations, "utility.guard_generic_stance_lock.passed")

    if context.preservation_stance_lock_turn > 0:
        _op(
            operations,
            "utility.guard_preservation_stance_lock.blocked",
            context.preservation_stance_lock_turn,
        )
        _op(operations, "executor.skip_effect_marker")
        return NativeExecutionResult(
            ExecutionStatus.BLOCKED,
            OutcomeReason.PRESERVATION_STANCE_LOCK,
            before,
            before,
            False,
            tuple(operations),
        )
    _op(operations, "utility.guard_preservation_stance_lock.passed")

    is_preservation_family = before.status_type in {
        NativeIdolStatus.PRESERVATION,
        NativeIdolStatus.OVER_PRESERVATION,
    }
    stance_count = before.stance_change_count
    preservation_count = before.preservation_change_count
    if not is_preservation_family:
        stance_count = _add_i32(stance_count, 1)
        _op(operations, "counter.increment_stance_change", stance_count)
        if context.callback_dispatch_enabled:
            _op(
                operations,
                "callback.increment_enchant_phase_counts",
                PHASE_STANCE_CHANGE_COUNT,
                PHASE_STANCE_CHANGE_COUNT_INTERVAL,
            )
            for status_id in context.matching_card_status_ids:
                _op(
                    operations,
                    "callback.increment_card_stance_change_count",
                    status_id,
                )
        else:
            _op(operations, "callback.generic_stance_change.suppressed")

        preservation_count = _add_i32(preservation_count, 1)
        _op(
            operations,
            "counter.increment_preservation_change",
            preservation_count,
        )
        if context.callback_dispatch_enabled:
            _op(
                operations,
                "callback.increment_enchant_phase_counts",
                PHASE_STANCE_CHANGE_PRESERVATION_INTERVAL,
            )
        else:
            _op(operations, "callback.preservation_interval.suppressed")
    else:
        _op(operations, "counter.preservation_family_transition.no_increment")

    # For target type 4, SetStance's full-power consumption path is bypassed.
    after = replace(
        before,
        status_type=NativeIdolStatus.OVER_PRESERVATION,
        status_step=NATIVE_TARGET_STEP,
        stance_change_count=stance_count,
        preservation_change_count=preservation_count,
    )
    _op(
        operations,
        "difference.append_stance",
        int(before.status_type),
        NATIVE_TARGET_STATUS,
        before.status_step,
        NATIVE_TARGET_STEP,
        before.stance_change_count,
        stance_count,
        before.full_power_points,
    )
    _op(operations, "status.set_over_preservation", 4, 1)
    _op(operations, "utility.return_true")
    _op(operations, "difference.append_effect_marker", EFFECT_ENUM_VALUE)
    return NativeExecutionResult(
        ExecutionStatus.APPLIED,
        OutcomeReason.APPLIED,
        before,
        after,
        True,
        tuple(operations),
    )


def _plan3_to_native(state: Plan3State) -> NativeSnapshot:
    relevant = {
        "stance_change_count": state.stance_change_count,
        "preservation_change_count": state.preservation_change_count,
        "block": state.block,
        "enthusiasm": state.enthusiasm,
        "plays_remaining": state.plays_remaining,
        "full_power_points": state.full_power_points,
    }
    for field, value in relevant.items():
        _require_i32(value, field)
    if state.stance == STANCE_NEUTRAL and state.stance_level == 0:
        status, step = NativeIdolStatus.UNKNOWN, 0
    elif state.stance == STANCE_CONCENTRATION and state.stance_level in (1, 2):
        status, step = NativeIdolStatus.CONCENTRATION, state.stance_level
    elif state.stance == STANCE_PRESERVATION and state.stance_level in (1, 2):
        status, step = NativeIdolStatus.PRESERVATION, state.stance_level
    elif state.stance == STANCE_PRESERVATION and state.stance_level == 3:
        status, step = NativeIdolStatus.OVER_PRESERVATION, 1
    elif state.stance == STANCE_FULL_POWER and state.stance_level == 1:
        status, step = NativeIdolStatus.FULL_POWER, 1
    else:
        raise OverPreservationContractError(
            "Plan3 stance/level has no exact native projection"
        )
    return NativeSnapshot(status, step, **relevant)


def _unresolved_plan3(
    state: Plan3State,
    reason: OutcomeReason,
    native_result: NativeExecutionResult | None = None,
) -> Plan3ExecutionResult:
    return Plan3ExecutionResult(
        ExecutionStatus.UNRESOLVED,
        reason,
        state,
        state,
        native_result,
    )


def execute_plan3_over_preservation(
    effect: OverPreservationEffectRow | Mapping[str, Any],
    state: Plan3State,
    *,
    context: CallbackContext | None = None,
) -> Plan3ExecutionResult:
    """Apply the exact scalar projection or return an immutable unresolved result."""

    if not isinstance(state, Plan3State):
        raise TypeError("state must be Plan3State")
    if not isinstance(effect, OverPreservationEffectRow) or not effect.canonical:
        return _unresolved_plan3(state, OutcomeReason.UNSUPPORTED_EFFECT_ROW)
    try:
        native_before = _plan3_to_native(state)
    except OverPreservationContractError:
        return _unresolved_plan3(
            state, OutcomeReason.INVALID_PLAN3_NATIVE_PROJECTION
        )
    native = execute_native_over_preservation(native_before, context=context)
    if not native.changed:
        return Plan3ExecutionResult(
            native.status,
            native.reason,
            state,
            state,
            native,
        )
    if (
        native.after.stance_change_count < 0
        or native.after.preservation_change_count < 0
    ):
        return _unresolved_plan3(
            state,
            OutcomeReason.PLAN3_COUNTER_WRAP_UNREPRESENTABLE,
            native,
        )
    after = replace(
        state,
        stance=PLAN3_TARGET_STANCE,
        stance_level=PLAN3_TARGET_LEVEL,
        stance_change_count=native.after.stance_change_count,
        preservation_change_count=native.after.preservation_change_count,
    )
    return Plan3ExecutionResult(native.status, native.reason, state, after, native)


OVER_PRESERVATION_CATALOG = load_over_preservation_catalog()
OVER_PRESERVATION_EFFECT_ROW = OVER_PRESERVATION_CATALOG.effect
EXECUTABLE_EFFECT_ROWS = (OVER_PRESERVATION_EFFECT_ROW,)
AFFECTED_CARD_VERSIONS = OVER_PRESERVATION_CATALOG.affected_card_versions
CATALOG = OVER_PRESERVATION_CATALOG
execute_over_preservation = execute_plan3_over_preservation


__all__ = [
    "AFFECTED_CARD_VERSIONS",
    "CATALOG",
    "EFFECT_ENUM_VALUE",
    "EFFECT_ID",
    "EFFECT_TYPE",
    "EXECUTABLE_EFFECT_ROWS",
    "CallbackContext",
    "Coverage",
    "ExecutionStatus",
    "NativeExecutionResult",
    "NativeIdolStatus",
    "NativeSnapshot",
    "OutcomeReason",
    "OverPreservationCatalog",
    "OverPreservationContractError",
    "OverPreservationEffectRow",
    "OVER_PRESERVATION_CATALOG",
    "OVER_PRESERVATION_EFFECT_ROW",
    "PHASE_STANCE_CHANGE_COUNT",
    "PHASE_STANCE_CHANGE_COUNT_INTERVAL",
    "PHASE_STANCE_CHANGE_PRESERVATION_INTERVAL",
    "PLAN3_TARGET_LEVEL",
    "Plan3ExecutionResult",
    "TraceOperation",
    "execute_native_over_preservation",
    "execute_over_preservation",
    "execute_plan3_over_preservation",
    "load_over_preservation_catalog",
]
