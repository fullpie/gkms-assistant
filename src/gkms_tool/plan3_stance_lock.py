"""Standalone Plan 3 semantics for ``ProduceExamEffectType_StanceLock``.

Android v3.2.3 proves both halves of this effect: the unique, merge-on-add
``StanceLockStatusEffect`` lifecycle and the early gate inside
``TryInternalSetStance``.  The immutable runtime below keeps native ``turn``,
``isPassingTurnStart``, UID allocation, and the collection's recently-used
UID set separate.  In particular, ``GetStanceLock(isUse=True)`` marks the UID
but does not spend a turn.

``Plan3State.stance_lock_runtime`` now owns this exact bounded collection
projection.  The Plan3 adapter installs or merges the same immutable runtime
used by the standalone executor; no listener-enchant shadow field is used.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from enum import Enum, IntEnum
from typing import Generic, TypeAlias, TypeVar


EFFECT_TYPE = "ProduceExamEffectType_StanceLock"
EFFECT_ID = "e_effect-stance_lock-01"
PLAN3 = "ProducePlanType_Plan3"
CARD_ID = "p_card-03-men-2_079"
CATEGORY_MENTAL_SKILL = "ProduceCardCategory_MentalSkill"
MOVE_GRAVE = "ProduceCardMovePositionType_Grave"
MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"
STATUS_EFFECT_TYPE = "Campus.InGame.Exam.StanceLockStatusEffect"
EXECUTOR_TYPE = "Campus.InGame.Exam.StanceLockEffectExecutor"

PRODUCE_EFFECT_ENUM_VALUE = 141
STATUS_EFFECT_ENUM_VALUE = 51
EFFECT_COUNT = 0
EFFECT_TURN = 1
INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1
UINT32_MASK = 2**32 - 1


JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...]


class ExecutionStatus(str, Enum):
    """Whether an effect result is executable in the current core."""

    EXECUTED = "executed"
    BLOCKED = "blocked"
    UNRESOLVED = "unresolved"


class UnresolvedReason(str, Enum):
    """Fail-closed reasons exposed by the resolver/executor boundary."""

    UNSUPPORTED_EFFECT = "unsupported_effect"
    UNSUPPORTED_ROW_SHAPE = "unsupported_effect_row_shape"
    STATE_FIELD_MISSING = "plan3_state_has_no_exam_status_collection"
    CORE_HOOK_REQUIRED = "plan3_native_status_hook_required"
    CONSUMPTION_TRIGGER_UNRESOLVED = "stance_lock_consume_trigger_unresolved"
    INVALID_RUNTIME = "invalid_stance_lock_runtime"


class NativeStance(IntEnum):
    """``ExamIdolStatusType`` values read by ``TryInternalSetStance``."""

    UNKNOWN = 0
    CONCENTRATION = 1
    PRESERVATION = 2
    FULL_POWER = 3
    OVER_PRESERVATION = 4


class AddOutcome(str, Enum):
    CREATED = "created"
    MERGED = "merged"
    BLOCKED = "blocked"
    UNRESOLVED = "unresolved"


class LifecycleOutcome(str, Enum):
    ABSENT = "absent"
    MARKED_PASSING = "marked_passing"
    ALREADY_PASSING = "already_passing"
    FRESH_RETAINED = "fresh_retained"
    SPENT = "spent"
    EXPIRED = "expired"


class StanceGateOutcome(str, Enum):
    PASSED = "passed"
    BLOCKED = "blocked"
    PRECHECK_NO_OP = "precheck_no_op"


class StanceGateReason(str, Enum):
    NO_LOCK = "no_positive_generic_stance_lock"
    GENERIC_STANCE_LOCK = "generic_stance_lock"
    SAME_STATUS_STEP_MAX = "same_status_step_max"
    OVER_TO_PRESERVATION_NO_OP = "over_to_preservation_no_op"


def _require_i32(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not INT32_MIN <= value <= INT32_MAX
    ):
        raise ValueError(f"{field} must be a signed Int32")
    return value


def _add_i32(left: int, right: int) -> int:
    unsigned = (left + right) & UINT32_MASK
    return unsigned - 2**32 if unsigned > INT32_MAX else unsigned


def _freeze_json(value: JsonValue) -> JsonValue:
    if isinstance(value, tuple):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _json_value(value: JsonValue) -> JsonScalar | list[object]:
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class TraceOperation:
    """One ordered native-equivalent operation in a standalone result."""

    sequence: int
    kind: str
    values: tuple[JsonScalar, ...] = ()

    def __post_init__(self) -> None:
        if self.sequence < 0 or not self.kind:
            raise ValueError("trace operation identity is invalid")
        object.__setattr__(self, "values", tuple(self.values))

    def to_json(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "values": list(self.values),
        }


def _op(
    operations: list[TraceOperation], kind: str, *values: JsonScalar
) -> None:
    operations.append(TraceOperation(len(operations), kind, tuple(values)))


@dataclass(frozen=True, slots=True)
class StanceLockStatus:
    """The fields of one native ``StanceLockStatusEffect`` instance."""

    uid: int
    turn: int
    is_passing_turn_start: bool = False

    def __post_init__(self) -> None:
        _require_i32(self.uid, "uid")
        _require_i32(self.turn, "turn")
        if not isinstance(self.is_passing_turn_start, bool):
            raise ValueError("is_passing_turn_start must be boolean")

    def to_json(self) -> dict[str, object]:
        return {
            "uid": self.uid,
            "turn": self.turn,
            "is_passing_turn_start": self.is_passing_turn_start,
        }


@dataclass(frozen=True, slots=True)
class StanceLockRuntime:
    """Bounded immutable projection of ``ExamStatusEffectCollection``."""

    status: StanceLockStatus | None = None
    effect_create_count: int = 0
    recently_used_uids: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        if self.status is not None and not isinstance(self.status, StanceLockStatus):
            raise TypeError("status must be StanceLockStatus or None")
        _require_i32(self.effect_create_count, "effect_create_count")
        uids = frozenset(self.recently_used_uids)
        for uid in uids:
            _require_i32(uid, "recently_used_uid")
        object.__setattr__(self, "recently_used_uids", uids)

    @property
    def active_count(self) -> int:
        return int(self.status is not None)

    def to_json(self) -> dict[str, object]:
        return {
            "status": self.status.to_json() if self.status is not None else None,
            "effect_create_count": self.effect_create_count,
            "recently_used_uids": sorted(self.recently_used_uids),
        }


@dataclass(frozen=True, slots=True)
class StanceLockQueryResult:
    """Result of native ``GetStanceLock(isUse)``."""

    turn: int
    is_use: bool
    runtime_before: StanceLockRuntime
    runtime_after: StanceLockRuntime
    operations: tuple[TraceOperation, ...]

    @property
    def consumed(self) -> bool:
        return False

    @property
    def recently_used_changed(self) -> bool:
        return (
            self.runtime_before.recently_used_uids
            != self.runtime_after.recently_used_uids
        )


@dataclass(frozen=True, slots=True)
class StanceLockAddResult:
    outcome: AddOutcome
    runtime_before: StanceLockRuntime
    runtime_after: StanceLockRuntime
    turn_before: int
    turn_after: int
    operations: tuple[TraceOperation, ...]

    @property
    def changed(self) -> bool:
        return self.runtime_after != self.runtime_before


@dataclass(frozen=True, slots=True)
class StanceLockLifecycleResult:
    outcome: LifecycleOutcome
    runtime_before: StanceLockRuntime
    runtime_after: StanceLockRuntime
    operations: tuple[TraceOperation, ...]

    @property
    def changed(self) -> bool:
        return self.runtime_after != self.runtime_before


@dataclass(frozen=True, slots=True)
class StatusDifference:
    status_effect_type: int
    before: int
    after: int

    def to_json(self) -> dict[str, int]:
        return {
            "status_effect_type": self.status_effect_type,
            "before": self.before,
            "after": self.after,
        }


@dataclass(frozen=True, slots=True)
class StanceMutationSnapshot:
    """State whose mutation occurs strictly after the generic lock gate."""

    status_type: NativeStance
    status_step: int
    stance_change_count: int = 0
    concentration_change_count: int = 0
    preservation_change_count: int = 0
    full_power_change_count: int = 0
    callback_events: tuple[str, ...] = ()
    difference_events: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        try:
            status_type = NativeStance(self.status_type)
        except (TypeError, ValueError) as error:
            raise ValueError("unsupported native stance") from error
        object.__setattr__(self, "status_type", status_type)
        for field in (
            "status_step",
            "stance_change_count",
            "concentration_change_count",
            "preservation_change_count",
            "full_power_change_count",
        ):
            _require_i32(getattr(self, field), field)
        valid_steps = {
            NativeStance.UNKNOWN: (0,),
            NativeStance.CONCENTRATION: (1, 2),
            NativeStance.PRESERVATION: (1, 2),
            NativeStance.FULL_POWER: (1,),
            NativeStance.OVER_PRESERVATION: (1,),
        }
        if self.status_step not in valid_steps[status_type]:
            raise ValueError("status_step is invalid for status_type")
        callbacks = tuple(self.callback_events)
        differences = tuple(self.difference_events)
        if not all(isinstance(value, str) for value in callbacks + differences):
            raise ValueError("callback/difference events must be strings")
        object.__setattr__(self, "callback_events", callbacks)
        object.__setattr__(self, "difference_events", differences)


@dataclass(frozen=True, slots=True)
class StanceGateResult:
    outcome: StanceGateOutcome
    reason: StanceGateReason
    target_status: NativeStance
    requested_step: int
    lock_before: StanceLockRuntime
    lock_after: StanceLockRuntime
    mutation_before: StanceMutationSnapshot
    mutation_after: StanceMutationSnapshot
    operations: tuple[TraceOperation, ...]

    @property
    def may_mutate(self) -> bool:
        return self.outcome is StanceGateOutcome.PASSED

    @property
    def mutation_side_effects_unchanged(self) -> bool:
        return self.mutation_before is self.mutation_after


def get_stance_lock(
    runtime: StanceLockRuntime,
    *,
    is_use: bool = True,
) -> StanceLockQueryResult:
    """Return the first lock's turn and optionally mark its UID as used.

    Native ``GetEffectEnumerableSafe<T>(isAllowAddUseList)`` snapshots the
    typed effects and adds their UIDs to a ``HashSet`` when ``is_use`` is
    true.  Neither that helper nor ``GetStanceLock`` calls ``ConsumeTurn``.
    """

    if not isinstance(runtime, StanceLockRuntime):
        raise TypeError("runtime must be StanceLockRuntime")
    if not isinstance(is_use, bool):
        raise TypeError("is_use must be boolean")
    operations: list[TraceOperation] = []
    _op(operations, "collection.snapshot_stance_lock_statuses")
    if runtime.status is None:
        _op(operations, "collection.get_stance_lock.absent", 0)
        return StanceLockQueryResult(0, is_use, runtime, runtime, tuple(operations))

    after = runtime
    if is_use:
        _op(operations, "collection.mark_recently_used", runtime.status.uid)
        after = replace(
            runtime,
            recently_used_uids=runtime.recently_used_uids | {runtime.status.uid},
        )
    else:
        _op(operations, "collection.skip_recently_used_marker")
    _op(operations, "collection.get_stance_lock.return_turn", runtime.status.turn)
    return StanceLockQueryResult(
        runtime.status.turn,
        is_use,
        runtime,
        after,
        tuple(operations),
    )


def try_add_stance_lock_status(
    runtime: StanceLockRuntime,
    turn: int,
    *,
    add_guard_blocked: bool = False,
) -> StanceLockAddResult:
    """Apply the proven add/merge body after resolving ``IsBlockAddStatus``.

    ``add_guard_blocked`` is the boolean returned by the external native
    guard.  The guard's own status interactions are outside this bounded
    runtime; once true, ``TryAddStanceLockStatus`` performs no local write.
    Native signed Int32 addition is retained for duplicate merges and UID
    allocation.
    """

    if not isinstance(runtime, StanceLockRuntime):
        raise TypeError("runtime must be StanceLockRuntime")
    _require_i32(turn, "turn")
    if not isinstance(add_guard_blocked, bool):
        raise TypeError("add_guard_blocked must be boolean")
    operations: list[TraceOperation] = []
    _op(operations, "collection.is_block_add_status", add_guard_blocked)
    before_turn = runtime.status.turn if runtime.status is not None else 0
    if add_guard_blocked:
        _op(operations, "collection.try_add.blocked")
        return StanceLockAddResult(
            AddOutcome.BLOCKED,
            runtime,
            runtime,
            before_turn,
            before_turn,
            tuple(operations),
        )

    if runtime.status is not None:
        after_turn = _add_i32(runtime.status.turn, turn)
        after = replace(
            runtime,
            status=replace(runtime.status, turn=after_turn),
        )
        _op(operations, "collection.last_stance_lock.merge", turn, after_turn)
        _op(operations, "status.on_turn_change")
        return StanceLockAddResult(
            AddOutcome.MERGED,
            runtime,
            after,
            before_turn,
            after_turn,
            tuple(operations),
        )

    uid = _add_i32(runtime.effect_create_count, 1)
    status = StanceLockStatus(uid=uid, turn=turn, is_passing_turn_start=False)
    after = replace(runtime, status=status, effect_create_count=uid)
    _op(operations, "status.construct", turn, False)
    _op(operations, "collection.assign_uid", uid)
    _op(operations, "collection.add_active_status")
    _op(operations, "collection.on_value_changed.invoke_if_present")
    return StanceLockAddResult(
        AddOutcome.CREATED,
        runtime,
        after,
        0,
        turn,
        tuple(operations),
    )


def set_stance_lock_passing_turn_start(
    runtime: StanceLockRuntime,
) -> StanceLockLifecycleResult:
    """Project the turn-start callback that marks existing effects passing."""

    if not isinstance(runtime, StanceLockRuntime):
        raise TypeError("runtime must be StanceLockRuntime")
    operations: list[TraceOperation] = []
    if runtime.status is None:
        _op(operations, "turn_start.no_stance_lock")
        return StanceLockLifecycleResult(
            LifecycleOutcome.ABSENT, runtime, runtime, tuple(operations)
        )
    if runtime.status.is_passing_turn_start:
        _op(operations, "turn_start.already_passing", runtime.status.uid)
        return StanceLockLifecycleResult(
            LifecycleOutcome.ALREADY_PASSING,
            runtime,
            runtime,
            tuple(operations),
        )
    after = replace(
        runtime,
        status=replace(runtime.status, is_passing_turn_start=True),
    )
    _op(operations, "turn_start.set_passing", runtime.status.uid)
    return StanceLockLifecycleResult(
        LifecycleOutcome.MARKED_PASSING,
        runtime,
        after,
        tuple(operations),
    )


def spend_stance_lock_turn(
    runtime: StanceLockRuntime,
) -> StanceLockLifecycleResult:
    """Run StanceLock's branch of collection ``SpendTurn``.

    A fresh positive effect is retained.  A passing effect spends first; the
    extension then reads its new turn and removes it from the active list
    when the value is non-positive.  Expiry does not add it to the separate
    ``_removedEffectList`` and does not clear the recently-used UID set.
    """

    if not isinstance(runtime, StanceLockRuntime):
        raise TypeError("runtime must be StanceLockRuntime")
    operations: list[TraceOperation] = []
    status = runtime.status
    if status is None:
        _op(operations, "collection.spend_turn.no_stance_lock")
        return StanceLockLifecycleResult(
            LifecycleOutcome.ABSENT, runtime, runtime, tuple(operations)
        )

    _op(operations, "status.read_is_turn_limited", True)
    _op(operations, "status.read_is_passing_turn_start", status.is_passing_turn_start)
    after_status = status
    if status.is_passing_turn_start:
        after_status = replace(status, turn=_add_i32(status.turn, -1))
        _op(operations, "status.spend_turn", status.turn, after_status.turn)
        _op(operations, "status.on_turn_change")
    else:
        _op(operations, "status.spend_turn.skip_fresh")

    _op(operations, "status.read_turn_after_spend", after_status.turn)
    if after_status.turn <= 0:
        _op(operations, "collection.remove_active_status", after_status.uid)
        _op(operations, "collection.on_value_changed.invoke_if_present")
        after = replace(runtime, status=None)
        return StanceLockLifecycleResult(
            LifecycleOutcome.EXPIRED,
            runtime,
            after,
            tuple(operations),
        )

    after = runtime if after_status is status else replace(runtime, status=after_status)
    _op(operations, "collection.on_value_changed.invoke_if_present")
    outcome = (
        LifecycleOutcome.SPENT
        if status.is_passing_turn_start
        else LifecycleOutcome.FRESH_RETAINED
    )
    return StanceLockLifecycleResult(
        outcome,
        runtime,
        after,
        tuple(operations),
    )


def clear_stance_lock_recently_used(
    runtime: StanceLockRuntime,
) -> StanceLockRuntime:
    """Project ``ClearRecentlyUseEffectUidList`` as an immutable operation."""

    if not isinstance(runtime, StanceLockRuntime):
        raise TypeError("runtime must be StanceLockRuntime")
    if not runtime.recently_used_uids:
        return runtime
    return replace(runtime, recently_used_uids=frozenset())


def _is_native_step_max(snapshot: StanceMutationSnapshot) -> bool:
    # IsIdolStatusIsStepMax returns step > 1 for types 1/2 and true otherwise.
    if snapshot.status_type in {
        NativeStance.CONCENTRATION,
        NativeStance.PRESERVATION,
    }:
        return snapshot.status_step > 1
    return True


def evaluate_try_internal_set_stance_gate(
    lock: StanceLockRuntime,
    mutation: StanceMutationSnapshot,
    *,
    target_status: NativeStance,
    requested_step: int,
) -> StanceGateResult:
    """Evaluate every branch through the generic lock gate, and no later.

    The returned mutation snapshot is intentionally untouched even when the
    gate passes: stance-specific locks, releases, counters, callbacks,
    differences, and the final stance write all belong to the next core
    integration layer.  A positive generic lock returns false before any of
    those operations, while retaining the recently-used marker made by
    ``GetStanceLock(true)``.
    """

    if not isinstance(lock, StanceLockRuntime):
        raise TypeError("lock must be StanceLockRuntime")
    if not isinstance(mutation, StanceMutationSnapshot):
        raise TypeError("mutation must be StanceMutationSnapshot")
    try:
        target = NativeStance(target_status)
    except (TypeError, ValueError) as error:
        raise ValueError("unsupported target_status") from error
    step = _require_i32(requested_step, "requested_step")
    operations: list[TraceOperation] = []
    _op(
        operations,
        "utility.read_current_stance",
        int(mutation.status_type),
        mutation.status_step,
    )

    if mutation.status_type is target and _is_native_step_max(mutation):
        _op(operations, "utility.precheck.same_status_step_max.return_false")
        return StanceGateResult(
            StanceGateOutcome.PRECHECK_NO_OP,
            StanceGateReason.SAME_STATUS_STEP_MAX,
            target,
            step,
            lock,
            lock,
            mutation,
            mutation,
            tuple(operations),
        )

    if (
        target is NativeStance.PRESERVATION
        and mutation.status_type is NativeStance.OVER_PRESERVATION
    ):
        _op(operations, "utility.precheck.over_to_preservation.return_false")
        return StanceGateResult(
            StanceGateOutcome.PRECHECK_NO_OP,
            StanceGateReason.OVER_TO_PRESERVATION_NO_OP,
            target,
            step,
            lock,
            lock,
            mutation,
            mutation,
            tuple(operations),
        )

    _op(operations, "utility.call_get_stance_lock", True)
    query = get_stance_lock(lock, is_use=True)
    for operation in query.operations:
        _op(operations, operation.kind, *operation.values)
    if query.turn > 0:
        _op(operations, "utility.generic_stance_lock.return_false", query.turn)
        return StanceGateResult(
            StanceGateOutcome.BLOCKED,
            StanceGateReason.GENERIC_STANCE_LOCK,
            target,
            step,
            lock,
            query.runtime_after,
            mutation,
            mutation,
            tuple(operations),
        )

    _op(operations, "utility.generic_stance_lock.passed", query.turn)
    return StanceGateResult(
        StanceGateOutcome.PASSED,
        StanceGateReason.NO_LOCK,
        target,
        step,
        lock,
        query.runtime_after,
        mutation,
        mutation,
        tuple(operations),
    )


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """A local source and the claim for which it is used."""

    source: str
    locator: str
    claim: str

    def __post_init__(self) -> None:
        if not self.source or not self.locator or not self.claim:
            raise ValueError("evidence references require source, locator, claim")

    def to_json(self) -> dict[str, str]:
        return {
            "source": self.source,
            "locator": self.locator,
            "claim": self.claim,
        }


@dataclass(frozen=True, slots=True)
class EffectField:
    """One ordered, hashable field from the Master effect payload."""

    name: str
    value: JsonValue

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("effect field name must not be empty")
        object.__setattr__(self, "value", _freeze_json(self.value))

    def to_json(self) -> dict[str, object]:
        return {"name": self.name, "value": _json_value(self.value)}


EXECUTION_EFFECT_FIELD_ORDER: tuple[str, ...] = (
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


@dataclass(frozen=True, slots=True)
class StanceLockEffectRow:
    """The one canonical Master row in this bounded family."""

    source_effect_id: str
    effect_type: str
    catalog_order: int
    fields: tuple[EffectField, ...]

    def __post_init__(self) -> None:
        if not self.source_effect_id or self.effect_type != EFFECT_TYPE:
            raise ValueError("invalid StanceLock effect identity")
        if self.catalog_order < 0:
            raise ValueError("catalog order must be non-negative")
        fields = tuple(self.fields)
        object.__setattr__(self, "fields", fields)
        names = tuple(field.name for field in fields)
        if names != EXECUTION_EFFECT_FIELD_ORDER:
            raise ValueError("effect fields must retain Master execution order")
        if self.field_value("id") != self.source_effect_id:
            raise ValueError("source effect id does not match the id field")
        if self.field_value("effectType") != self.effect_type:
            raise ValueError("effect type does not match the effectType field")

    @property
    def effect_id(self) -> str:
        return self.source_effect_id

    @property
    def argument_fields(self) -> tuple[str, ...]:
        """Native coverage's exact non-empty argument mask for this family."""

        return ("effectTurn",)

    def field_value(self, name: str) -> JsonValue:
        for field in self.fields:
            if field.name == name:
                return field.value
        raise KeyError(name)

    @property
    def value1(self) -> int:
        return _int_field(self, "effectValue1")

    @property
    def value2(self) -> int:
        return _int_field(self, "effectValue2")

    @property
    def effect_count(self) -> int:
        return _int_field(self, "effectCount")

    @property
    def effect_turn(self) -> int:
        return _int_field(self, "effectTurn")

    @property
    def effect_group_ids(self) -> tuple[str, ...]:
        value = self.field_value("effectGroupIds")
        if not isinstance(value, tuple) or not all(
            isinstance(item, str) for item in value
        ):
            raise TypeError("effectGroupIds must be a string tuple")
        return value

    @property
    def raw_field_order(self) -> tuple[str, ...]:
        return (
            *EXECUTION_EFFECT_FIELD_ORDER,
            "produceDescriptions",
            "customizeProduceDescriptions",
        )

    def to_json(self) -> dict[str, object]:
        return {
            "source_effect_id": self.source_effect_id,
            "effect_type": self.effect_type,
            "catalog_order": self.catalog_order,
            "argument_fields": list(self.argument_fields),
            "fields": [field.to_json() for field in self.fields],
            "raw_field_order": list(self.raw_field_order),
            "ui_description_fields_omitted": [
                "produceDescriptions",
                "customizeProduceDescriptions",
            ],
        }


def _int_field(row: StanceLockEffectRow, name: str) -> int:
    value = row.field_value(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


@dataclass(frozen=True, slots=True)
class CardEffectSlot:
    """One ordered ``card.play_effects_json`` slot."""

    effect_order: int
    source_effect_id: str
    effect_type: str
    produce_exam_trigger_id: str = ""
    hide_icon: bool = False
    is_once_play_effect: bool = False

    def __post_init__(self) -> None:
        if self.effect_order < 0 or not self.source_effect_id or not self.effect_type:
            raise ValueError("card effect slot identity/order is invalid")
        if not self.produce_exam_trigger_id:
            object.__setattr__(self, "produce_exam_trigger_id", "")

    @property
    def slot(self) -> int:
        return self.effect_order

    @property
    def effect_id(self) -> str:
        return self.source_effect_id

    @property
    def once_only(self) -> bool:
        return self.is_once_play_effect

    def to_json(self) -> dict[str, object]:
        return {
            "effect_order": self.effect_order,
            "source_effect_id": self.source_effect_id,
            "effect_type": self.effect_type,
            "produce_exam_trigger_id": self.produce_exam_trigger_id,
            "hide_icon": self.hide_icon,
            "is_once_play_effect": self.is_once_play_effect,
        }


@dataclass(frozen=True, slots=True)
class StanceLockCardVersion:
    """One of the four Plan 3 versions affected by the family."""

    card_id: str
    upgrade: int
    plan_type: str
    category: str
    stamina_cost: int
    move_position_type: str
    effect_slots: tuple[CardEffectSlot, ...]

    def __post_init__(self) -> None:
        if self.card_id != CARD_ID or self.plan_type != PLAN3:
            raise ValueError("card version is outside the bounded Plan3 scope")
        if self.upgrade not in range(4):
            raise ValueError("only upgrades 0 through 3 are in scope")
        if self.stamina_cost < 0:
            raise ValueError("stamina cost must be non-negative")
        slots = tuple(self.effect_slots)
        object.__setattr__(self, "effect_slots", slots)
        if tuple(slot.effect_order for slot in slots) != tuple(range(len(slots))):
            raise ValueError("card effect slots must retain contiguous source order")

    @property
    def upgrade_count(self) -> int:
        return self.upgrade

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return tuple(slot.source_effect_id for slot in self.effect_slots)

    @property
    def stance_lock_slots(self) -> tuple[CardEffectSlot, ...]:
        return tuple(
            slot for slot in self.effect_slots if slot.effect_type == EFFECT_TYPE
        )

    @property
    def stance_lock_slot(self) -> CardEffectSlot:
        slots = self.stance_lock_slots
        if len(slots) != 1:
            raise LookupError("card version does not have exactly one StanceLock slot")
        return slots[0]

    def to_json(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "plan_type": self.plan_type,
            "category": self.category,
            "stamina_cost": self.stamina_cost,
            "move_position_type": self.move_position_type,
            "ordered_effect_ids": list(self.ordered_effect_ids),
            "effect_slots": [slot.to_json() for slot in self.effect_slots],
        }


@dataclass(frozen=True, slots=True)
class ExecutorEvidence:
    """Native identity and the body-level lifecycle facts admitted here."""

    effect_type: str
    produce_effect_enum_value: int
    executor_type: str
    constructor_rva: str
    constructor_token: str
    execute_rva: str
    status_effect_type: str
    status_effect_enum_value: int
    try_add_rva: str
    get_stance_lock_rva: str
    set_stance_rva: str
    try_internal_set_stance_rva: str
    add_status_rva: str
    spend_turn_rva: str
    status_spend_turn_rva: str
    spend_turn_extension_rva: str
    set_passing_turn_start_rva: str
    turn_start_marker_callback_rva: str
    consume_turn_rva: str
    remove_turn_status_rva: str
    android_body_proven: bool
    pc_metadata_proven: bool
    pc_body_proven: bool
    effect_turn_to_executor_turn_proven: bool
    all_stance_gate_proven: bool
    try_add_block_gate_proven: bool
    merge_proven: bool
    duplicate_status_prevented_proven: bool
    get_turn_proven: bool
    use_marker_proven: bool
    is_use_does_not_consume_turn_proven: bool
    fresh_passing_boundary_proven: bool
    turn_spend_proven: bool
    expiry_path_proven: bool
    expiry_active_remove_order_proven: bool
    blocked_gate_atomicity_proven: bool
    full_power_internal_gate_proven: bool
    over_preservation_internal_gate_proven: bool
    consume_trigger_proven: bool
    effect_order_proven: bool
    evidence_refs: tuple[EvidenceRef, ...]

    @property
    def consume_proven(self) -> bool:
        """Whether a StanceLock-specific consume trigger is fully resolved."""

        return self.consume_trigger_proven

    @property
    def expire_proven(self) -> bool:
        return self.expiry_path_proven

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))

    def to_json(self) -> dict[str, object]:
        return {
            "effect_type": self.effect_type,
            "produce_effect_enum_value": self.produce_effect_enum_value,
            "executor_type": self.executor_type,
            "constructor_rva": self.constructor_rva,
            "constructor_token": self.constructor_token,
            "execute_rva": self.execute_rva,
            "status_effect_type": self.status_effect_type,
            "status_effect_enum_value": self.status_effect_enum_value,
            "try_add_rva": self.try_add_rva,
            "get_stance_lock_rva": self.get_stance_lock_rva,
            "set_stance_rva": self.set_stance_rva,
            "try_internal_set_stance_rva": self.try_internal_set_stance_rva,
            "add_status_rva": self.add_status_rva,
            "spend_turn_rva": self.spend_turn_rva,
            "status_spend_turn_rva": self.status_spend_turn_rva,
            "spend_turn_extension_rva": self.spend_turn_extension_rva,
            "set_passing_turn_start_rva": self.set_passing_turn_start_rva,
            "turn_start_marker_callback_rva": self.turn_start_marker_callback_rva,
            "consume_turn_rva": self.consume_turn_rva,
            "remove_turn_status_rva": self.remove_turn_status_rva,
            "android_body_proven": self.android_body_proven,
            "pc_metadata_proven": self.pc_metadata_proven,
            "pc_body_proven": self.pc_body_proven,
            "effect_turn_to_executor_turn_proven": self.effect_turn_to_executor_turn_proven,
            "all_stance_gate_proven": self.all_stance_gate_proven,
            "try_add_block_gate_proven": self.try_add_block_gate_proven,
            "merge_proven": self.merge_proven,
            "duplicate_status_prevented_proven": self.duplicate_status_prevented_proven,
            "get_turn_proven": self.get_turn_proven,
            "use_marker_proven": self.use_marker_proven,
            "is_use_does_not_consume_turn_proven": self.is_use_does_not_consume_turn_proven,
            "fresh_passing_boundary_proven": self.fresh_passing_boundary_proven,
            "turn_spend_proven": self.turn_spend_proven,
            "expiry_path_proven": self.expiry_path_proven,
            "expiry_active_remove_order_proven": self.expiry_active_remove_order_proven,
            "blocked_gate_atomicity_proven": self.blocked_gate_atomicity_proven,
            "full_power_internal_gate_proven": self.full_power_internal_gate_proven,
            "over_preservation_internal_gate_proven": self.over_preservation_internal_gate_proven,
            "consume_trigger_proven": self.consume_trigger_proven,
            "effect_order_proven": self.effect_order_proven,
            "consume_proven": self.consume_proven,
            "expire_proven": self.expire_proven,
            "evidence_refs": [item.to_json() for item in self.evidence_refs],
        }


@dataclass(frozen=True, slots=True)
class StanceLockContract:
    """The immutable state-transition contract for this family."""

    effect_type: str
    recognized_effect_ids: tuple[str, ...]
    executable_effect_ids: tuple[str, ...]
    affected_card_version_count: int
    sole_unlock_card_version_count: int
    unresolved_reasons: tuple[UnresolvedReason, ...]
    state_policy: str
    failure_status: ExecutionStatus
    standalone_runtime_executable: bool
    core_integrated: bool

    @property
    def executable_count(self) -> int:
        return len(self.executable_effect_ids)

    @property
    def executable_card_version_count(self) -> int:
        return self.affected_card_version_count if self.executable_effect_ids else 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "recognized_effect_ids", tuple(self.recognized_effect_ids))
        object.__setattr__(self, "executable_effect_ids", tuple(self.executable_effect_ids))
        object.__setattr__(self, "unresolved_reasons", tuple(self.unresolved_reasons))
        if self.affected_card_version_count != 4:
            raise ValueError("bounded StanceLock scope must contain four versions")
        if self.sole_unlock_card_version_count != 4:
            raise ValueError("StanceLock integration directly unlocks four versions")
        if self.executable_effect_ids != (EFFECT_ID,):
            raise ValueError("standalone runtime must execute the canonical row only")
        if not self.standalone_runtime_executable or not self.core_integrated:
            raise ValueError("standalone and Plan3 core runtimes must be executable")

    def to_json(self) -> dict[str, object]:
        return {
            "effect_type": self.effect_type,
            "recognized_effect_ids": list(self.recognized_effect_ids),
            "executable_effect_ids": list(self.executable_effect_ids),
            "affected_card_version_count": self.affected_card_version_count,
            "sole_unlock_card_version_count": self.sole_unlock_card_version_count,
            "executable_card_version_count": self.executable_card_version_count,
            "unresolved_reasons": [reason.value for reason in self.unresolved_reasons],
            "state_policy": self.state_policy,
            "failure_status": self.failure_status.value,
            "standalone_runtime_executable": self.standalone_runtime_executable,
            "core_integrated": self.core_integrated,
        }


@dataclass(frozen=True, slots=True)
class StanceLockCatalog:
    """Immutable catalog, native evidence, and scope accounting."""

    scope: str
    master_effect_rows: tuple[StanceLockEffectRow, ...]
    affected_card_versions: tuple[StanceLockCardVersion, ...]
    executor: ExecutorEvidence
    evidence: tuple[EvidenceRef, ...]
    contract: StanceLockContract
    order_basis: str
    runtime_order_proven: bool
    directly_unlocked_if_fixed_alone: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "master_effect_rows", tuple(self.master_effect_rows))
        object.__setattr__(self, "affected_card_versions", tuple(self.affected_card_versions))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if len(self.master_effect_rows) != 1:
            raise ValueError("bounded StanceLock catalog must contain one Master row")
        if len(self.affected_card_versions) != 4:
            raise ValueError("bounded StanceLock catalog must contain four versions")
        if self.directly_unlocked_if_fixed_alone != 4:
            raise ValueError("coverage proves four directly unlocked versions")

    @property
    def master_shape_count(self) -> int:
        return len(self.master_effect_rows)

    @property
    def affected_version_count(self) -> int:
        return len(self.affected_card_versions)

    @property
    def affected_unique_card_count(self) -> int:
        return len({item.card_id for item in self.affected_card_versions})

    @property
    def executable_count(self) -> int:
        return self.contract.executable_count

    @property
    def executable_card_version_count(self) -> int:
        return self.contract.executable_card_version_count

    @property
    def sole_unlock_card_version_count(self) -> int:
        return self.directly_unlocked_if_fixed_alone

    def row(self, source_effect_id: str) -> StanceLockEffectRow:
        for row in self.master_effect_rows:
            if row.source_effect_id == source_effect_id:
                return row
        raise KeyError(source_effect_id)

    def to_json(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "master_shape_count": self.master_shape_count,
            "affected_version_count": self.affected_version_count,
            "affected_unique_card_count": self.affected_unique_card_count,
            "executable_count": self.executable_count,
            "executable_card_version_count": self.executable_card_version_count,
            "sole_unlock_card_version_count": self.sole_unlock_card_version_count,
            "directly_unlocked_if_fixed_alone": self.directly_unlocked_if_fixed_alone,
            "master_effect_rows": [row.to_json() for row in self.master_effect_rows],
            "affected_card_versions": [
                card.to_json() for card in self.affected_card_versions
            ],
            "executor": self.executor.to_json(),
            "evidence": [item.to_json() for item in self.evidence],
            "contract": self.contract.to_json(),
            "order_basis": self.order_basis,
            "runtime_order_proven": self.runtime_order_proven,
        }


def _stance_lock_fields() -> tuple[EffectField, ...]:
    return (
        EffectField("id", EFFECT_ID),
        EffectField("effectType", EFFECT_TYPE),
        EffectField("effectValue1", 0),
        EffectField("effectValue2", 0),
        EffectField("effectCount", EFFECT_COUNT),
        EffectField("effectTurn", EFFECT_TURN),
        EffectField("targetProduceCardId", ""),
        EffectField("targetUpgradeCount", 0),
        EffectField("targetExamEffectType", "ProduceExamEffectType_Unknown"),
        EffectField("produceCardSearchId", ""),
        EffectField("movePositionType", MOVE_UNKNOWN),
        EffectField("pickRangeType", "ProducePickRangeType_Unknown"),
        EffectField("pickCountReferenceProduceCardSearchId", ""),
        EffectField("pickCountType", "ProducePickCountType_Unknown"),
        EffectField("pickCountMin", 0),
        EffectField("pickCountMax", 0),
        EffectField("produceCardSearchId2", ""),
        EffectField("pickRangeType2", "ProducePickRangeType_Unknown"),
        EffectField("pickCountReferenceProduceCardSearchId2", ""),
        EffectField("pickCountType2", "ProducePickCountType_Unknown"),
        EffectField("pickCountMin2", 0),
        EffectField("pickCountMax2", 0),
        EffectField("chainProduceExamEffectId", ""),
        EffectField("chainProduceExamEffectIds", ()),
        EffectField("produceExamStatusEnchantId", ""),
        EffectField("produceCardStatusEnchantId", ""),
        EffectField("produceCardGrowEffectIds", ()),
        EffectField("effectGroupIds", ("effect_group-visible-stance_lock-000",)),
    )


STANCE_LOCK_EFFECT_ROW = StanceLockEffectRow(
    source_effect_id=EFFECT_ID,
    effect_type=EFFECT_TYPE,
    catalog_order=0,
    fields=_stance_lock_fields(),
)
MASTER_EFFECT_ROWS: tuple[StanceLockEffectRow, ...] = (STANCE_LOCK_EFFECT_ROW,)
RESOLVED_EFFECT_ROWS = MASTER_EFFECT_ROWS


_BLOCK = "ProduceExamEffectType_ExamBlock"
_FULL_POWER = "ProduceExamEffectType_ExamFullPowerPoint"
_CARD_MOVE = "ProduceExamEffectType_ExamCardMove"
_PLAYABLE = "ProduceExamEffectType_ExamPlayableValueAdd"


def _slot(order: int, source_effect_id: str, effect_type: str) -> CardEffectSlot:
    return CardEffectSlot(
        effect_order=order,
        source_effect_id=source_effect_id,
        effect_type=effect_type,
    )


def _card_version(
    upgrade: int,
    stamina_cost: int,
    effect_ids: tuple[tuple[str, str], ...],
) -> StanceLockCardVersion:
    return StanceLockCardVersion(
        card_id=CARD_ID,
        upgrade=upgrade,
        plan_type=PLAN3,
        category=CATEGORY_MENTAL_SKILL,
        stamina_cost=stamina_cost,
        move_position_type=MOVE_GRAVE,
        effect_slots=tuple(
            _slot(order, source_effect_id, effect_type)
            for order, (source_effect_id, effect_type) in enumerate(effect_ids)
        ),
    )


_MOVE_EFFECT = (
    "e_effect-exam_card_move-p_card_search-deck_grave-hold-select-1_1",
    _CARD_MOVE,
)
_PLAYABLE_EFFECT = ("e_effect-exam_playable_value_add-01", _PLAYABLE)
_STANCE_LOCK_EFFECT = (EFFECT_ID, EFFECT_TYPE)


AFFECTED_CARD_VERSIONS: tuple[StanceLockCardVersion, ...] = (
    _card_version(
        0,
        1,
        (
            ("e_effect-exam_full_power_point-0002", _FULL_POWER),
            _MOVE_EFFECT,
            _PLAYABLE_EFFECT,
            _STANCE_LOCK_EFFECT,
        ),
    ),
    _card_version(
        1,
        0,
        (
            ("e_effect-exam_full_power_point-0003", _FULL_POWER),
            _MOVE_EFFECT,
            _PLAYABLE_EFFECT,
            _STANCE_LOCK_EFFECT,
        ),
    ),
    _card_version(
        2,
        0,
        (
            ("e_effect-exam_block-0003", _BLOCK),
            ("e_effect-exam_full_power_point-0003", _FULL_POWER),
            _MOVE_EFFECT,
            _PLAYABLE_EFFECT,
            _STANCE_LOCK_EFFECT,
        ),
    ),
    _card_version(
        3,
        0,
        (
            ("e_effect-exam_block-0005", _BLOCK),
            ("e_effect-exam_full_power_point-0003", _FULL_POWER),
            _MOVE_EFFECT,
            _PLAYABLE_EFFECT,
            _STANCE_LOCK_EFFECT,
        ),
    ),
)


EVIDENCE_REFS: tuple[EvidenceRef, ...] = (
    EvidenceRef(
        "var/coverage/plan3_blocker_priority.json",
        "family=effect-type:ProduceExamEffectType_StanceLock",
        "exactly four Plan3 versions of one card are affected and zero are sole unlocks",
    ),
    EvidenceRef(
        "var/coverage/plan3_master_effect_schema_native.json",
        "card_graph.ordered_relations; effect_argument_masks; native_executor_mapping",
        "the row is the ordered last play slot, its only argument is effectTurn, and factory mapping is executable",
    ),
    EvidenceRef(
        "var/master.sqlite3",
        "effect.id=e_effect-stance_lock-01; card.id=p_card-03-men-2_079",
        "Master values, raw effect shape, four upgrades, and complete declared play-effect order",
    ),
    EvidenceRef(
        "_research/gakumasu-diff/ProduceExamEffect.yaml",
        "e_effect-stance_lock-01; contrast e_effect-stance_lock-02",
        "effectCount=0 and effectTurn=1 are data facts; the neighboring 02 row has a different turn",
    ),
    EvidenceRef(
        "_research/gakumasu-diff/ProduceCard.yaml",
        "p_card-03-men-2_079 upgrades 0,1,2,3",
        "card-level effect order and the final StanceLock slot are source-declared, not name-derived",
    ),
    EvidenceRef(
        "_research/android/game-v3.2.3/cpp2il-plugin/DiffableCs/Assembly-CSharp/Campus/Ingame/Exam/StanceLockEffectExecutor.cs",
        "StanceLockEffectExecutor._turn; .ctor; ExecuteEffect",
        "the native executor and its turn field are the mapped status producer",
    ),
    EvidenceRef(
        "_research/android/game-v3.2.3/il2cppdumper/dump.cs",
        "0x7E8E7AC, 0x7E8E864, 0x7E9D630, 0x7E9D740",
        "constructor reads effectTurn, TryAdd gates then merges/adds, and GetStanceLock returns remaining turn",
    ),
    EvidenceRef(
        "_research/android/game-v3.2.3/extracted/lib/arm64-v8a/libil2cpp.so",
        "RVA 0x7E8E864; 0x7E9D630; 0x7E9D740; 0x7E60948",
        "executor differences, pre-add blocking, merge/no-duplicate behavior, isUse marking, and the positive-lock stance gate are body-proven",
    ),
    EvidenceRef(
        "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/StanceLockStatusEffect.txt",
        "get_Type; .ctor(int turn)",
        "status type is 51 and the positive turn is stored on ExamStatusEffectBase",
    ),
    EvidenceRef(
        "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/ExamRuleCalculator.txt",
        "GetStanceLock(isUse=true)",
        "the rule calculator uses the lock query for stance calculations; GetStanceLock itself does not decrement turn",
    ),
    EvidenceRef(
        "_research/android/game-v3.2.3/extracted/lib/arm64-v8a/libil2cpp.so",
        "RVA 0x7E929D4; 0x7E92C48; 0x8F7168C; 0x7EA46D8; callback 0x7EDA368",
        "fresh effects skip spending; turn-start marks passing; passing effects spend before the extension tests turn<=0 and removes from the active list",
    ),
    EvidenceRef(
        "_research/android/game-v3.2.3/extracted/lib/arm64-v8a/libil2cpp.so",
        "TryInternalSetStance RVA 0x7E60B2C-0x7E60C30",
        "same/max and Over-to-Preservation no-ops precede GetStanceLock(true); every remaining target including FullPower and OverPreservation crosses the generic gate before counters, callbacks, differences, or mutation",
    ),
    EvidenceRef(
        "_research/il2cpp/B6120AF66E75E72757E3D4086E2C48D0E7D31DA9F3A3DFBCDC7B3ED5BFFD61DA/targeted-metadata-index.json",
        "StanceLockEffectExecutor; StanceLockStatusEffect; TryAddStanceLockStatus; GetStanceLock",
        "PC metadata identity matches the Android-mapped family; no PC body semantics are claimed",
    ),
)


EXECUTOR_EVIDENCE = ExecutorEvidence(
    effect_type=EFFECT_TYPE,
    produce_effect_enum_value=PRODUCE_EFFECT_ENUM_VALUE,
    executor_type=EXECUTOR_TYPE,
    constructor_rva="0x7E8E7AC",
    constructor_token="0x0600490E",
    execute_rva="0x7E8E864",
    status_effect_type=STATUS_EFFECT_TYPE,
    status_effect_enum_value=STATUS_EFFECT_ENUM_VALUE,
    try_add_rva="0x7E9D630",
    get_stance_lock_rva="0x7E9D740",
    set_stance_rva="0x7E60940",
    try_internal_set_stance_rva="0x7E60948",
    add_status_rva="0x8F70ACC",
    spend_turn_rva="0x7EA46D8",
    status_spend_turn_rva="0x7E92C48",
    spend_turn_extension_rva="0x8F7168C",
    set_passing_turn_start_rva="0x7E929D4",
    turn_start_marker_callback_rva="0x7EDA368",
    consume_turn_rva="0x7E94410",
    remove_turn_status_rva="0x7EA56A8",
    android_body_proven=True,
    pc_metadata_proven=True,
    pc_body_proven=False,
    effect_turn_to_executor_turn_proven=True,
    all_stance_gate_proven=True,
    try_add_block_gate_proven=True,
    merge_proven=True,
    duplicate_status_prevented_proven=True,
    get_turn_proven=True,
    use_marker_proven=True,
    is_use_does_not_consume_turn_proven=True,
    fresh_passing_boundary_proven=True,
    turn_spend_proven=True,
    expiry_path_proven=True,
    expiry_active_remove_order_proven=True,
    blocked_gate_atomicity_proven=True,
    full_power_internal_gate_proven=True,
    over_preservation_internal_gate_proven=True,
    consume_trigger_proven=False,
    effect_order_proven=True,
    evidence_refs=EVIDENCE_REFS,
)


RUNTIME_UNRESOLVED_REASONS: tuple[UnresolvedReason, ...] = (
    UnresolvedReason.INVALID_RUNTIME,
)


STANCE_LOCK_CONTRACT = StanceLockContract(
    effect_type=EFFECT_TYPE,
    recognized_effect_ids=(EFFECT_ID,),
    executable_effect_ids=(EFFECT_ID,),
    affected_card_version_count=4,
    sole_unlock_card_version_count=4,
    unresolved_reasons=(),
    state_policy="Plan3State owns the immutable native stance-lock runtime; invalid or unknown rows fail closed with state identity",
    failure_status=ExecutionStatus.UNRESOLVED,
    standalone_runtime_executable=True,
    core_integrated=True,
)


STANCE_LOCK_CATALOG = StanceLockCatalog(
    scope="Plan3 / ProduceExamEffectType_StanceLock",
    master_effect_rows=MASTER_EFFECT_ROWS,
    affected_card_versions=AFFECTED_CARD_VERSIONS,
    executor=EXECUTOR_EVIDENCE,
    evidence=EVIDENCE_REFS,
    contract=STANCE_LOCK_CONTRACT,
    order_basis="card.play_effects_json source array index",
    runtime_order_proven=True,
    directly_unlocked_if_fixed_alone=4,
)

CATALOG = STANCE_LOCK_CATALOG
EXECUTABLE_EFFECT_ROWS: tuple[StanceLockEffectRow, ...] = (STANCE_LOCK_EFFECT_ROW,)


@dataclass(frozen=True, slots=True)
class StanceLockResolution:
    """Typed resolution of a Master/Plan3 effect into the canonical row."""

    status: ExecutionStatus
    source_effect_id: str
    effect_type: str
    effect: StanceLockEffectRow | None
    reasons: tuple[UnresolvedReason, ...]

    @property
    def resolved(self) -> bool:
        return self.status is ExecutionStatus.EXECUTED and self.effect is not None

    def to_json(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "source_effect_id": self.source_effect_id,
            "effect_type": self.effect_type,
            "resolved": self.resolved,
            "reasons": [reason.value for reason in self.reasons],
            "effect": self.effect.to_json() if self.effect is not None else None,
        }


def _attribute(effect: object, *names: str) -> object:
    for name in names:
        if hasattr(effect, name):
            return getattr(effect, name)
    raise AttributeError(names[0])


def _effect_identity(effect: object) -> tuple[str, str]:
    source_effect_id = _attribute(effect, "source_effect_id", "effect_id", "id")
    effect_type = _attribute(effect, "effect_type", "effectType")
    if not isinstance(source_effect_id, str) or not isinstance(effect_type, str):
        raise TypeError("effect identity must be strings")
    return source_effect_id, effect_type


def _scalar_shape_matches(effect: object) -> bool:
    expected = {
        "value1": 0,
        "value2": 0,
        "effect_count": EFFECT_COUNT,
        "effect_turn": EFFECT_TURN,
    }
    for name, wanted in expected.items():
        try:
            value = _attribute(effect, name, {
                "value1": "effectValue1",
                "value2": "effectValue2",
                "effect_count": "effectCount",
                "effect_turn": "effectTurn",
            }[name])
        except AttributeError:
            return False
        if isinstance(value, bool) or not isinstance(value, int) or value != wanted:
            return False
    return True


def resolve_plan3_stance_lock(effect: object) -> StanceLockResolution:
    """Resolve only the exact ``e_effect-stance_lock-01`` row.

    ``Plan3Effect`` is accepted structurally so this module remains
    independent of the engine module.  Resolution does not imply execution:
    the latter still requires the native status hook represented in the
    contract above.
    """

    try:
        source_effect_id, effect_type = _effect_identity(effect)
    except (AttributeError, TypeError):
        return StanceLockResolution(
            status=ExecutionStatus.UNRESOLVED,
            source_effect_id="<unknown>",
            effect_type="<unknown>",
            effect=None,
            reasons=(UnresolvedReason.UNSUPPORTED_ROW_SHAPE,),
        )

    if source_effect_id != EFFECT_ID or effect_type != EFFECT_TYPE:
        return StanceLockResolution(
            status=ExecutionStatus.UNRESOLVED,
            source_effect_id=source_effect_id,
            effect_type=effect_type,
            effect=None,
            reasons=(UnresolvedReason.UNSUPPORTED_EFFECT,),
        )

    if not _scalar_shape_matches(effect):
        return StanceLockResolution(
            status=ExecutionStatus.UNRESOLVED,
            source_effect_id=source_effect_id,
            effect_type=effect_type,
            effect=effect if isinstance(effect, StanceLockEffectRow) else None,
            reasons=(UnresolvedReason.UNSUPPORTED_ROW_SHAPE,),
        )

    if isinstance(effect, StanceLockEffectRow) and effect != STANCE_LOCK_EFFECT_ROW:
        return StanceLockResolution(
            status=ExecutionStatus.UNRESOLVED,
            source_effect_id=source_effect_id,
            effect_type=effect_type,
            effect=effect,
            reasons=(UnresolvedReason.UNSUPPORTED_ROW_SHAPE,),
        )

    return StanceLockResolution(
        status=ExecutionStatus.EXECUTED,
        source_effect_id=source_effect_id,
        effect_type=effect_type,
        effect=STANCE_LOCK_EFFECT_ROW,
        reasons=(),
    )


resolve_stance_lock_effect = resolve_plan3_stance_lock


StateT = TypeVar("StateT")


@dataclass(frozen=True, slots=True)
class StanceLockStandaloneExecution:
    """Pure execution of the canonical effect against ``StanceLockRuntime``."""

    status: ExecutionStatus
    outcome: AddOutcome
    runtime_before: StanceLockRuntime
    runtime_after: StanceLockRuntime
    resolution: StanceLockResolution
    add_result: StanceLockAddResult | None
    difference: StatusDifference | None
    operations: tuple[TraceOperation, ...]

    @property
    def executable(self) -> bool:
        return self.status is not ExecutionStatus.UNRESOLVED

    @property
    def changed(self) -> bool:
        return self.runtime_after != self.runtime_before

    def to_json(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "outcome": self.outcome.value,
            "executable": self.executable,
            "changed": self.changed,
            "runtime_before": self.runtime_before.to_json(),
            "runtime_after": self.runtime_after.to_json(),
            "resolution": self.resolution.to_json(),
            "difference": (
                self.difference.to_json() if self.difference is not None else None
            ),
            "operations": [operation.to_json() for operation in self.operations],
        }


def execute_standalone_stance_lock(
    effect: object,
    runtime: StanceLockRuntime,
    *,
    add_guard_blocked: bool = False,
) -> StanceLockStandaloneExecution:
    """Execute the one cataloged row using its native ``effectTurn=1``."""

    if not isinstance(runtime, StanceLockRuntime):
        raise TypeError("runtime must be StanceLockRuntime")
    resolution = resolve_plan3_stance_lock(effect)
    operations: list[TraceOperation] = []
    if not resolution.resolved or resolution.effect is None:
        _op(operations, "executor.fail_closed.unsupported_effect")
        return StanceLockStandaloneExecution(
            ExecutionStatus.UNRESOLVED,
            AddOutcome.UNRESOLVED,
            runtime,
            runtime,
            resolution,
            None,
            None,
            tuple(operations),
        )

    before_query = get_stance_lock(runtime, is_use=False)
    for operation in before_query.operations:
        _op(operations, operation.kind, *operation.values)
    add_result = try_add_stance_lock_status(
        before_query.runtime_after,
        resolution.effect.effect_turn,
        add_guard_blocked=add_guard_blocked,
    )
    for operation in add_result.operations:
        _op(operations, operation.kind, *operation.values)
    if add_result.outcome is AddOutcome.BLOCKED:
        _op(operations, "executor.skip_status_difference")
        return StanceLockStandaloneExecution(
            ExecutionStatus.BLOCKED,
            AddOutcome.BLOCKED,
            runtime,
            runtime,
            resolution,
            add_result,
            None,
            tuple(operations),
        )

    after_query = get_stance_lock(add_result.runtime_after, is_use=False)
    for operation in after_query.operations:
        _op(operations, operation.kind, *operation.values)
    difference = StatusDifference(
        STATUS_EFFECT_ENUM_VALUE,
        before_query.turn,
        after_query.turn,
    )
    _op(
        operations,
        "executor.append_status_difference",
        difference.status_effect_type,
        difference.before,
        difference.after,
    )
    return StanceLockStandaloneExecution(
        ExecutionStatus.EXECUTED,
        add_result.outcome,
        runtime,
        after_query.runtime_after,
        resolution,
        add_result,
        difference,
        tuple(operations),
    )


@dataclass(frozen=True, slots=True)
class StanceLockUnresolvedExecution(Generic[StateT]):
    """Pure fail-closed executor result; it never mutates or shadows state."""

    status: ExecutionStatus
    source_effect_id: str
    effect_type: str
    state_before: StateT
    state_after: StateT
    reasons: tuple[UnresolvedReason, ...]
    effect: StanceLockEffectRow | None
    resolution: StanceLockResolution

    def __post_init__(self) -> None:
        if self.status is not ExecutionStatus.UNRESOLVED:
            raise ValueError("StanceLock executor currently only returns unresolved")
        object.__setattr__(self, "reasons", tuple(self.reasons))

    @property
    def executable(self) -> bool:
        return False

    @property
    def changed(self) -> bool:
        return False

    @property
    def state_unchanged(self) -> bool:
        return self.state_before is self.state_after

    def to_json(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "source_effect_id": self.source_effect_id,
            "effect_type": self.effect_type,
            "reasons": [reason.value for reason in self.reasons],
            "executable": False,
            "changed": False,
            "state_policy": "state_after_is_state_before",
            "resolution": self.resolution.to_json(),
            "effect": self.effect.to_json() if self.effect is not None else None,
        }


UnresolvedExecution = StanceLockUnresolvedExecution


@dataclass(frozen=True, slots=True)
class Plan3StanceLockExecution(Generic[StateT]):
    """Successful Plan3 adapter result backed by the standalone runtime."""

    status: ExecutionStatus
    source_effect_id: str
    effect_type: str
    state_before: StateT
    state_after: StateT
    reasons: tuple[UnresolvedReason, ...]
    effect: StanceLockEffectRow
    resolution: StanceLockResolution
    runtime_result: StanceLockStandaloneExecution

    def __post_init__(self) -> None:
        if self.status is ExecutionStatus.UNRESOLVED:
            raise ValueError("resolved Plan3 execution cannot be unresolved")
        object.__setattr__(self, "reasons", tuple(self.reasons))

    @property
    def executable(self) -> bool:
        return True

    @property
    def changed(self) -> bool:
        return self.state_after != self.state_before

    @property
    def state_unchanged(self) -> bool:
        return self.state_after is self.state_before


def execute_plan3_stance_lock(
    effect: object,
    state: StateT,
) -> StanceLockUnresolvedExecution[StateT] | Plan3StanceLockExecution[StateT]:
    """Install the canonical row in Plan3's typed native-status projection."""

    resolution = resolve_plan3_stance_lock(effect)
    if not resolution.resolved or resolution.effect is None:
        return StanceLockUnresolvedExecution(
            status=ExecutionStatus.UNRESOLVED,
            source_effect_id=resolution.source_effect_id,
            effect_type=resolution.effect_type,
            state_before=state,
            state_after=state,
            reasons=resolution.reasons,
            effect=resolution.effect,
            resolution=resolution,
        )
    runtime = getattr(state, "stance_lock_runtime", None)
    if not isinstance(runtime, StanceLockRuntime):
        return StanceLockUnresolvedExecution(
            status=ExecutionStatus.UNRESOLVED,
            source_effect_id=resolution.source_effect_id,
            effect_type=resolution.effect_type,
            state_before=state,
            state_after=state,
            reasons=RUNTIME_UNRESOLVED_REASONS,
            effect=resolution.effect,
            resolution=resolution,
        )
    runtime_result = execute_standalone_stance_lock(effect, runtime)
    if not runtime_result.executable:
        return StanceLockUnresolvedExecution(
            status=ExecutionStatus.UNRESOLVED,
            source_effect_id=resolution.source_effect_id,
            effect_type=resolution.effect_type,
            state_before=state,
            state_after=state,
            reasons=(UnresolvedReason.INVALID_RUNTIME,),
            effect=resolution.effect,
            resolution=resolution,
        )
    after = replace(state, stance_lock_runtime=runtime_result.runtime_after)
    return Plan3StanceLockExecution(
        status=runtime_result.status,
        source_effect_id=resolution.source_effect_id,
        effect_type=resolution.effect_type,
        state_before=state,
        state_after=after,
        reasons=(),
        effect=resolution.effect,
        resolution=resolution,
        runtime_result=runtime_result,
    )


execute_stance_lock = execute_plan3_stance_lock


def catalog_to_json() -> dict[str, object]:
    """Return a JSON-compatible snapshot without exposing mutable catalog data."""

    return json.loads(json.dumps(STANCE_LOCK_CATALOG.to_json(), ensure_ascii=False))


__all__ = [
    "AFFECTED_CARD_VERSIONS",
    "AddOutcome",
    "CARD_ID",
    "CATALOG",
    "EFFECT_COUNT",
    "EFFECT_ID",
    "EFFECT_TURN",
    "EFFECT_TYPE",
    "EXECUTABLE_EFFECT_ROWS",
    "EXECUTOR_EVIDENCE",
    "ExecutionStatus",
    "EvidenceRef",
    "LifecycleOutcome",
    "MASTER_EFFECT_ROWS",
    "RESOLVED_EFFECT_ROWS",
    "STANCE_LOCK_CATALOG",
    "STANCE_LOCK_CONTRACT",
    "STANCE_LOCK_EFFECT_ROW",
    "NativeStance",
    "StanceGateOutcome",
    "StanceGateReason",
    "StanceGateResult",
    "StanceLockCardVersion",
    "StanceLockCatalog",
    "StanceLockContract",
    "StanceLockEffectRow",
    "StanceLockAddResult",
    "StanceLockLifecycleResult",
    "Plan3StanceLockExecution",
    "StanceLockQueryResult",
    "StanceLockRuntime",
    "StanceLockResolution",
    "StanceLockStandaloneExecution",
    "StanceLockStatus",
    "StanceLockUnresolvedExecution",
    "StanceMutationSnapshot",
    "StatusDifference",
    "TraceOperation",
    "UnresolvedExecution",
    "UnresolvedReason",
    "catalog_to_json",
    "clear_stance_lock_recently_used",
    "evaluate_try_internal_set_stance_gate",
    "execute_plan3_stance_lock",
    "execute_standalone_stance_lock",
    "execute_stance_lock",
    "get_stance_lock",
    "resolve_plan3_stance_lock",
    "resolve_stance_lock_effect",
    "set_stance_lock_passing_turn_start",
    "spend_stance_lock_turn",
    "try_add_stance_lock_status",
]
