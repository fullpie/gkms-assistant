"""Typed Plan 3 ``FullPowerPointAdditive`` bounded runtime.

This module is deliberately a sidecar to :mod:`plan3_engine`.  Its scope is
only the four current-coverage versions of ``p_card-03-men-2_103``.  The
existing scalar Plan3 state has no full-power additive layer field, so this
module keeps the native finite layers in an immutable runtime value and
exposes one small point-gain hook for a future core call site.

The Master rows bind ``effectValue1`` and ``effectTurn``.  The Android
v3.2.3 body proves that the value is stored as a status integer and is later
read as ``value / 1000.0f``.  The status collection merges an incoming layer
only when its current turn value matches; otherwise the layers stack.  A
finite layer spends one turn at the turn boundary and is active immediately
when the effect slot executes.

The current PC evidence is metadata-shape compatibility, not a PC native
instruction dump.  The Android formula constants below are therefore
version-pinned evidence and intentionally do not claim byte identity for PC.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
import sqlite3
import struct
from typing import Any, TypeAlias

from .master_db import DEFAULT_DATABASE
from .plan3_engine import Plan3State


EFFECT_FULL_POWER_POINT_ADDITIVE = (
    "ProduceExamEffectType_ExamFullPowerPointAdditive"
)
MASTER_EFFECT_TYPE = EFFECT_FULL_POWER_POINT_ADDITIVE

TARGET_CARD_ID = "p_card-03-men-2_103"
TARGET_EFFECT_ID_03 = (
    "e_effect-exam_full_power_point_additive-0500-03"
)
TARGET_EFFECT_ID_04 = (
    "e_effect-exam_full_power_point_additive-0500-04"
)
TARGET_EFFECT_IDS = (TARGET_EFFECT_ID_03, TARGET_EFFECT_ID_04)
TARGET_EFFECT_ID_BY_UPGRADE = {
    0: TARGET_EFFECT_ID_03,
    1: TARGET_EFFECT_ID_04,
    2: TARGET_EFFECT_ID_04,
    3: TARGET_EFFECT_ID_04,
}
TARGET_DURATION_BY_UPGRADE = {0: 3, 1: 4, 2: 4, 3: 4}
TARGET_CARD_PLAY_EFFECT_IDS = (
    "e_effect-exam_full_power_point-0003",
    "e_effect-exam_full_power_point_additive-0500-03",
    "e_effect-exam_playable_value_add-01",
    "e_effect-exam_card_draw-0002",
    "e_effect-exam_card_create_id-p_card-00-acc-0_002-0-deck_random-1_1",
)
TARGET_CARD_PLAY_EFFECT_IDS_BY_UPGRADE = {
    upgrade: (
        TARGET_CARD_PLAY_EFFECT_IDS[0],
        TARGET_EFFECT_ID_BY_UPGRADE[upgrade],
        *TARGET_CARD_PLAY_EFFECT_IDS[2:],
    )
    for upgrade in range(4)
}
TARGET_EFFECT_SLOT = 1
TARGET_EFFECT_GROUP_IDS = ("effect_group-visible-exam_full_power-000",)

AFFECTED_CARD_VERSIONS = tuple(
    (TARGET_CARD_ID, upgrade) for upgrade in range(4)
)
REPRESENTATIVE_EFFECT_IDS = TARGET_EFFECT_IDS
TARGET_PLAY_EFFECT_ORDER = (
    "slot-0:ExamFullPower",
    "slot-1:ExamFullPowerPointAdditive",
    "slot-2:ExamPlayableValueAdd",
    "slot-3:ExamCardDraw",
    "slot-4:ExamCardCreateId",
)

# Android v3.2.3 native evidence.
NATIVE_EXECUTOR_TYPE = (
    "Campus.InGame.Exam.FullPowerPointAdditiveEffectExecutor"
)
NATIVE_EXECUTOR_ENUM_VALUE = 178
NATIVE_CONSTRUCTOR_VA = 0x7E7B4B4
NATIVE_EXECUTE_VA = 0x7E7B5CC
NATIVE_POINT_EXECUTE_VA = 0x7E7BE40
NATIVE_TRY_ADD_POINT_VA = 0x7E9DCE8
NATIVE_TRY_ADD_STATUS_VA = 0x7E9E580
NATIVE_GET_MULTIPLE_VA = 0x7E9E034
NATIVE_STATUS_CONSTRUCTOR_VA = 0x7E9E730
NATIVE_STATUS_SPEND_TURN_VA = 0x7E92C48
NATIVE_ADD_STATUS_VA = 0x8F70ACC
NATIVE_GET_EFFECT_ENUMERABLE_SAFE_VA = 0x8F70C7C

# Names used by the analogous Plan 3 additive modules.
NATIVE_TRY_ADD_VA = NATIVE_TRY_ADD_STATUS_VA
NATIVE_GET_BUFF_VA = NATIVE_GET_MULTIPLE_VA

NATIVE_OPERATION_ORDER = (
    "read-additive-multiple-before-status",
    "try-add-full-power-point-additive-status(value,effectTurn,context)",
    "read-additive-multiple-after-status",
)
NATIVE_MERGE_RULE = (
    "LastOrDefault layer with the same current turn is selected; AddValue is "
    "used; different current turns remain separate layers"
)
NATIVE_DURATION_RULE = (
    "effectTurn=-1 is unlimited; a positive effectTurn is active in the "
    "current turn and spends once at the turn boundary"
)
NATIVE_MULTIPLE_RULE = (
    "float32 accumulator starts at 1.0f and adds each status value divided "
    "by 1000.0f"
)
NATIVE_POINT_RULE = (
    "TryAddFullPowerPoint computes ceil(float32((base+fixed)*multiple)); "
    "the stored gauge has a zero lower clamp and no upper cap in that body"
)

# Current PC metadata carries the same executor fields and collection method
# signatures.  No PC native instruction body is present in this workspace.
PC_METADATA_EXECUTOR_TYPE = NATIVE_EXECUTOR_TYPE
PC_METADATA_EXECUTOR_FIELDS = ("_value", "_turn")
PC_METADATA_COLLECTION_METHODS = (
    "TryAddFullPowerPointAdditiveStatus",
    "GetFullPowerPointAdditiveMultiple",
)
PC_NATIVE_BODY_AVAILABLE = False

_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1
_UINT32_MASK = 2**32 - 1
_MISSING = object()


class Plan3FullPowerPointAdditiveContractError(ValueError):
    """A Master row or native-shaped runtime value is unsupported."""


class Plan3FullPowerPointAdditiveUnresolvedError(RuntimeError):
    """An unresolved resolution was asked to mutate the runtime directly."""


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise Plan3FullPowerPointAdditiveContractError(
            f"{field} must be non-empty text"
        )
    return value


def _int32(value: object, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not _INT32_MIN <= value <= _INT32_MAX
    ):
        raise Plan3FullPowerPointAdditiveContractError(
            f"{field} must be an Int32"
        )
    return value


def _row_dict(row: Mapping[str, object] | sqlite3.Row) -> dict[str, object]:
    if isinstance(row, Mapping):
        return dict(row)
    keys = getattr(row, "keys", None)
    if callable(keys):
        try:
            return {key: row[key] for key in keys()}
        except (KeyError, IndexError, TypeError) as error:
            raise Plan3FullPowerPointAdditiveContractError(
                "invalid Master row"
            ) from error
    raise Plan3FullPowerPointAdditiveContractError("row must be a mapping")


def _master_payload(row: Mapping[str, object] | sqlite3.Row) -> dict[str, object]:
    outer = _row_dict(row)
    raw = outer.get("raw_json", _MISSING)
    if raw is _MISSING or raw is None:
        return outer
    if isinstance(raw, Mapping):
        payload = dict(raw)
    elif isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            raise Plan3FullPowerPointAdditiveContractError(
                "raw_json is not valid JSON"
            ) from error
        if not isinstance(parsed, Mapping):
            raise Plan3FullPowerPointAdditiveContractError(
                "raw_json must contain an object"
            )
        payload = dict(parsed)
    else:
        raise Plan3FullPowerPointAdditiveContractError(
            "raw_json must be JSON text"
        )
    if "id" in outer and "id" in payload and outer["id"] != payload["id"]:
        raise Plan3FullPowerPointAdditiveContractError(
            "outer/raw effect id mismatch"
        )
    return payload


def _field(payload: Mapping[str, object], *names: str) -> object:
    for name in names:
        if name in payload:
            return payload[name]
    raise Plan3FullPowerPointAdditiveContractError(
        f"missing structural field: {names[0]}"
    )


def _text_tuple(value: object, field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise Plan3FullPowerPointAdditiveContractError(
            f"{field} must be a list"
        )
    result = tuple(_text(item, f"{field} item") for item in value)
    return result


def _signed_int32(value: int) -> int:
    bits = value & _UINT32_MASK
    return bits if bits <= _INT32_MAX else bits - 2**32


def _native_nonnegative_add(current: int, delta: int) -> int:
    """Mirror the native Int32 add followed by the zero lower clamp."""

    bits = (current + delta) & _UINT32_MASK
    return max(0, _signed_int32(bits))


def _f32(value: float | int) -> float:
    """Round one operation to the IEEE-754 binary32 value used by IL2CPP."""

    return struct.unpack("<f", struct.pack("<f", float(value)))[0]


@dataclass(frozen=True, slots=True)
class Plan3FullPowerPointAdditiveContract:
    """Immutable binding for one bounded Master additive effect row."""

    effect_id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    effect_group_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect_id")
        if self.effect_type != MASTER_EFFECT_TYPE:
            raise Plan3FullPowerPointAdditiveContractError(
                f"unexpected effect_type: {self.effect_type!r}"
            )
        _int32(self.value1, "effectValue1")
        _int32(self.value2, "effectValue2")
        _int32(self.effect_count, "effectCount")
        _int32(self.effect_turn, "effectTurn")
        if self.effect_turn < -1:
            raise Plan3FullPowerPointAdditiveContractError(
                "effectTurn below -1 is not a supported native shape"
            )
        if self.value2 != 0:
            raise Plan3FullPowerPointAdditiveContractError(
                "effectValue2 is not the neutral additive shape"
            )
        if self.effect_count != 0:
            raise Plan3FullPowerPointAdditiveContractError(
                "effectCount is not the neutral additive shape"
            )
        groups = tuple(self.effect_group_ids)
        if any(not isinstance(group, str) or not group for group in groups):
            raise Plan3FullPowerPointAdditiveContractError(
                "effect_group_ids must contain non-empty text"
            )
        object.__setattr__(self, "effect_group_ids", groups)

    @classmethod
    def from_master_row(
        cls, row: Mapping[str, object] | sqlite3.Row
    ) -> "Plan3FullPowerPointAdditiveContract":
        payload = _master_payload(row)
        effect_group_ids = payload.get(
            "effectGroupIds", payload.get("effect_group_ids", ())
        )
        if effect_group_ids is None:
            effect_group_ids = ()
        return cls(
            effect_id=_text(_field(payload, "id"), "id"),
            effect_type=_text(
                _field(payload, "effectType", "effect_type"), "effectType"
            ),
            value1=_int32(
                _field(payload, "effectValue1", "value1"), "effectValue1"
            ),
            value2=_int32(
                _field(payload, "effectValue2", "value2"), "effectValue2"
            ),
            effect_count=_int32(
                _field(payload, "effectCount", "effect_count"), "effectCount"
            ),
            effect_turn=_int32(
                _field(payload, "effectTurn", "effect_turn"), "effectTurn"
            ),
            effect_group_ids=_text_tuple(effect_group_ids, "effectGroupIds"),
        )

    @property
    def value_permille(self) -> int:
        return self.value1

    @property
    def duration_turns(self) -> int | None:
        return None if self.effect_turn == -1 else self.effect_turn

    @property
    def is_turn_limited(self) -> bool:
        return self.effect_turn >= 0

    def to_dict(self) -> dict[str, object]:
        return {
            "effect_id": self.effect_id,
            "effect_type": self.effect_type,
            "value1": self.value1,
            "value_permille": self.value_permille,
            "value2": self.value2,
            "effect_count": self.effect_count,
            "effect_turn": self.effect_turn,
            "effect_group_ids": list(self.effect_group_ids),
            "duration_turns": self.duration_turns,
            "is_turn_limited": self.is_turn_limited,
        }

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)


@dataclass(frozen=True, slots=True)
class Plan3FullPowerPointAdditiveLayer:
    """One native additive status layer with its current turn counter."""

    value_permille: int
    remaining_turns: int
    source_effect_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _int32(self.value_permille, "value_permille")
        if (
            isinstance(self.remaining_turns, bool)
            or not isinstance(self.remaining_turns, int)
            or self.remaining_turns < -1
            or self.remaining_turns == 0
        ):
            raise Plan3FullPowerPointAdditiveContractError(
                "remaining_turns must be -1 or a positive integer"
            )
        sources = tuple(self.source_effect_ids)
        if any(not isinstance(source, str) or not source for source in sources):
            raise Plan3FullPowerPointAdditiveContractError(
                "source_effect_ids must contain non-empty text"
            )
        object.__setattr__(self, "source_effect_ids", sources)

    @property
    def is_turn_limited(self) -> bool:
        return self.remaining_turns >= 0

    def to_dict(self) -> dict[str, object]:
        return {
            "value_permille": self.value_permille,
            "remaining_turns": self.remaining_turns,
            "source_effect_ids": list(self.source_effect_ids),
            "is_turn_limited": self.is_turn_limited,
        }

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)


@dataclass(frozen=True, slots=True)
class Plan3FullPowerPointAdditiveRuntime:
    """Immutable sidecar state for the finite native additive layers."""

    layers: tuple[Plan3FullPowerPointAdditiveLayer, ...] = ()

    def __post_init__(self) -> None:
        layers = tuple(self.layers)
        if any(
            not isinstance(layer, Plan3FullPowerPointAdditiveLayer)
            for layer in layers
        ):
            raise TypeError("layers must contain typed additive layers")
        turns = [layer.remaining_turns for layer in layers]
        if len(turns) != len(set(turns)):
            raise Plan3FullPowerPointAdditiveContractError(
                "runtime layers with equal current turns must be merged"
            )
        object.__setattr__(self, "layers", layers)

    @property
    def layer_count(self) -> int:
        return len(self.layers)

    def get_full_power_point_additive_multiple(
        self, *, is_use: bool = True
    ) -> float:
        """Mirror ``GetFullPowerPointAdditiveMultiple`` in float32."""

        if not isinstance(is_use, bool):
            raise TypeError("is_use must be a boolean")
        # The sidecar contains only the active status collection.  Native's
        # isUse flag chooses the collection view; it does not consume a layer.
        multiple = _f32(1.0)
        for layer in self.layers:
            value = _f32(layer.value_permille)
            term = _f32(value / _f32(1000.0))
            multiple = _f32(multiple + term)
        return multiple

    @property
    def multiple(self) -> float:
        return self.get_full_power_point_additive_multiple()

    def install(
        self, contract: Plan3FullPowerPointAdditiveContract
    ) -> "Plan3FullPowerPointAdditiveRuntime":
        """Install one status, merging only the native-equal turn layer."""

        if not isinstance(contract, Plan3FullPowerPointAdditiveContract):
            raise TypeError("contract must be a typed additive contract")
        if contract.effect_turn == 0:
            raise Plan3FullPowerPointAdditiveContractError(
                "zero-turn additive status is outside this bounded runtime"
            )
        incoming = Plan3FullPowerPointAdditiveLayer(
            value_permille=contract.value_permille,
            remaining_turns=contract.effect_turn,
            source_effect_ids=(contract.effect_id,),
        )
        for index in range(len(self.layers) - 1, -1, -1):
            layer = self.layers[index]
            if layer.remaining_turns != incoming.remaining_turns:
                continue
            merged = Plan3FullPowerPointAdditiveLayer(
                value_permille=_native_nonnegative_add(
                    layer.value_permille, incoming.value_permille
                ),
                remaining_turns=layer.remaining_turns,
                source_effect_ids=(
                    *layer.source_effect_ids,
                    *incoming.source_effect_ids,
                ),
            )
            layers = list(self.layers)
            layers[index] = merged
            return Plan3FullPowerPointAdditiveRuntime(tuple(layers))
        return Plan3FullPowerPointAdditiveRuntime(
            (*self.layers, incoming)
        )

    def spend_turn(self) -> "Plan3FullPowerPointAdditiveRuntime":
        """Spend one native status turn at the explicit turn boundary."""

        spent: list[Plan3FullPowerPointAdditiveLayer] = []
        for layer in self.layers:
            if layer.remaining_turns == -1:
                spent.append(layer)
            elif layer.remaining_turns > 1:
                spent.append(
                    Plan3FullPowerPointAdditiveLayer(
                        value_permille=layer.value_permille,
                        remaining_turns=layer.remaining_turns - 1,
                        source_effect_ids=layer.source_effect_ids,
                    )
                )
            # A layer at one remaining turn is removed, matching SpendTurn.
        return Plan3FullPowerPointAdditiveRuntime(tuple(spent))

    def to_dict(self) -> dict[str, object]:
        return {
            "layers": [layer.to_dict() for layer in self.layers],
            "multiple": self.multiple,
        }

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)


@dataclass(frozen=True, slots=True)
class Plan3FullPowerPointAdditiveExecutable:
    """A bounded contract executable through the typed finite layer."""

    contract: Plan3FullPowerPointAdditiveContract
    card_id: str = ""
    upgrade: int | None = None
    effect_slot: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(
            self.contract, Plan3FullPowerPointAdditiveContract
        ):
            raise TypeError("contract must be a typed additive contract")
        if not isinstance(self.card_id, str):
            raise TypeError("card_id must be text")
        if self.upgrade is not None and (
            isinstance(self.upgrade, bool) or not isinstance(self.upgrade, int)
        ):
            raise TypeError("upgrade must be an integer or None")
        if self.effect_slot is not None and (
            isinstance(self.effect_slot, bool)
            or not isinstance(self.effect_slot, int)
            or self.effect_slot < 0
        ):
            raise ValueError("effect_slot must be non-negative or None")

    @property
    def executable(self) -> bool:
        return True

    @property
    def resolved(self) -> bool:
        return True

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "executable",
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "effect_slot": self.effect_slot,
            "contract": self.contract.to_dict(),
            "operation_order": list(NATIVE_OPERATION_ORDER),
        }

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)


@dataclass(frozen=True, slots=True)
class Plan3FullPowerPointAdditiveUnresolved:
    """Typed fail-closed resolution with no runtime mutation."""

    effect_id: str
    blocker_code: str
    detail: str
    contract: Plan3FullPowerPointAdditiveContract | None = None
    card_id: str = ""
    upgrade: int | None = None

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect_id")
        _text(self.blocker_code, "blocker_code")
        _text(self.detail, "detail")
        if self.contract is not None and not isinstance(
            self.contract, Plan3FullPowerPointAdditiveContract
        ):
            raise TypeError("contract must be a typed additive contract or None")
        if not isinstance(self.card_id, str):
            raise TypeError("card_id must be text")

    @property
    def executable(self) -> bool:
        return False

    @property
    def resolved(self) -> bool:
        return False

    @property
    def required_native_inputs(self) -> tuple[str, ...]:
        return (
            "an ordered FullPowerPoint additive status layer collection",
            "a turn-boundary SpendTurn call for finite layers",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "unresolved",
            "effect_id": self.effect_id,
            "blocker_code": self.blocker_code,
            "detail": self.detail,
            "contract": None if self.contract is None else self.contract.to_dict(),
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "required_native_inputs": list(self.required_native_inputs),
        }

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)


Plan3FullPowerPointAdditiveResolution: TypeAlias = (
    Plan3FullPowerPointAdditiveExecutable
    | Plan3FullPowerPointAdditiveUnresolved
)


@dataclass(frozen=True, slots=True)
class Plan3FullPowerPointAdditiveExecution:
    """Pure ordered installation transition for the additive status."""

    resolution: Plan3FullPowerPointAdditiveResolution
    before: Plan3FullPowerPointAdditiveRuntime
    after: Plan3FullPowerPointAdditiveRuntime
    operation_order: tuple[str, ...] = NATIVE_OPERATION_ORDER
    before_multiple: float = 1.0
    after_multiple: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(
            self.resolution,
            (
                Plan3FullPowerPointAdditiveExecutable,
                Plan3FullPowerPointAdditiveUnresolved,
            ),
        ):
            raise TypeError("invalid FullPowerPointAdditive resolution")
        if not isinstance(self.before, Plan3FullPowerPointAdditiveRuntime):
            raise TypeError("before must be additive runtime")
        if not isinstance(self.after, Plan3FullPowerPointAdditiveRuntime):
            raise TypeError("after must be additive runtime")
        order = tuple(self.operation_order)
        if order != NATIVE_OPERATION_ORDER:
            raise Plan3FullPowerPointAdditiveContractError(
                "native operation order changed"
            )
        if isinstance(self.resolution, Plan3FullPowerPointAdditiveUnresolved):
            if self.after is not self.before:
                raise Plan3FullPowerPointAdditiveContractError(
                    "unresolved additive execution must preserve runtime identity"
                )
        if not math.isfinite(self.before_multiple) or not math.isfinite(
            self.after_multiple
        ):
            raise ValueError("native additive multiples must be finite")
        object.__setattr__(self, "operation_order", order)

    @property
    def runtime(self) -> Plan3FullPowerPointAdditiveRuntime:
        return self.after

    @property
    def state(self) -> Plan3FullPowerPointAdditiveRuntime:
        return self.after

    @property
    def applied(self) -> bool:
        return isinstance(self.resolution, Plan3FullPowerPointAdditiveExecutable)

    @property
    def executable(self) -> bool:
        return self.applied

    @property
    def resolved(self) -> bool:
        return self.applied

    @property
    def contract(self) -> Plan3FullPowerPointAdditiveContract | None:
        return self.resolution.contract

    def to_dict(self) -> dict[str, object]:
        return {
            "resolution": self.resolution.to_dict(),
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "operation_order": list(self.operation_order),
            "before_multiple": self.before_multiple,
            "after_multiple": self.after_multiple,
            "applied": self.applied,
        }

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)


def _unresolved(
    effect_id: str,
    blocker_code: str,
    detail: str,
    *,
    contract: Plan3FullPowerPointAdditiveContract | None = None,
    card_id: str = "",
    upgrade: int | None = None,
) -> Plan3FullPowerPointAdditiveUnresolved:
    return Plan3FullPowerPointAdditiveUnresolved(
        effect_id=effect_id or "<unknown-effect>",
        blocker_code=blocker_code,
        detail=detail or blocker_code,
        contract=contract,
        card_id=card_id,
        upgrade=upgrade,
    )


def _effect_id_hint(row: Mapping[str, object] | sqlite3.Row) -> str:
    try:
        payload = _master_payload(row)
        value = payload.get("id")
        return value if isinstance(value, str) and value else "<invalid-effect-id>"
    except (Plan3FullPowerPointAdditiveContractError, TypeError):
        return "<invalid-effect-id>"


def _resolve_bounded_contract(
    contract: Plan3FullPowerPointAdditiveContract,
    *,
    card_id: str = "",
    upgrade: int | None = None,
    effect_slot: int | None = None,
) -> Plan3FullPowerPointAdditiveResolution:
    expected_duration = {
        TARGET_EFFECT_ID_03: 3,
        TARGET_EFFECT_ID_04: 4,
    }.get(contract.effect_id)
    if expected_duration is None:
        return _unresolved(
            contract.effect_id,
            "outside-bounded-full-power-point-additive-family",
            "effect id is not one of the two current-coverage 0500 rows",
            contract=contract,
            card_id=card_id,
            upgrade=upgrade,
        )
    if contract.value1 != 500:
        return _unresolved(
            contract.effect_id,
            "full-power-point-additive-value-mismatch",
            f"effectValue1={contract.value1} but bounded family requires 500",
            contract=contract,
            card_id=card_id,
            upgrade=upgrade,
        )
    if contract.effect_turn != expected_duration:
        return _unresolved(
            contract.effect_id,
            "full-power-point-additive-duration-mismatch",
            (
                f"effectTurn={contract.effect_turn} but {contract.effect_id} "
                f"requires {expected_duration}"
            ),
            contract=contract,
            card_id=card_id,
            upgrade=upgrade,
        )
    return Plan3FullPowerPointAdditiveExecutable(
        contract=contract,
        card_id=card_id,
        upgrade=upgrade,
        effect_slot=effect_slot,
    )


def resolve_plan3_full_power_point_additive_row(
    row: Mapping[str, object] | sqlite3.Row,
) -> Plan3FullPowerPointAdditiveResolution:
    """Resolve one Master row, strictly within the two-row bounded family."""

    effect_id = _effect_id_hint(row)
    try:
        contract = Plan3FullPowerPointAdditiveContract.from_master_row(row)
    except (Plan3FullPowerPointAdditiveContractError, TypeError) as error:
        return _unresolved(
            effect_id,
            "unsupported-full-power-point-additive-master-shape",
            str(error) or type(error).__name__,
        )
    return _resolve_bounded_contract(contract)


def _read_card_and_effect(
    card_id: str,
    upgrade: int,
    database: Path,
) -> tuple[sqlite3.Row, sqlite3.Row] | Plan3FullPowerPointAdditiveUnresolved:
    if card_id != TARGET_CARD_ID:
        return _unresolved(
            "<unknown-effect>",
            "outside-bounded-full-power-point-additive-card-family",
            f"card_id={card_id!r} is outside the bounded family",
            card_id=card_id,
            upgrade=upgrade,
        )
    if upgrade not in TARGET_EFFECT_ID_BY_UPGRADE:
        return _unresolved(
            "<unknown-effect>",
            "outside-bounded-full-power-point-additive-card-family",
            f"upgrade={upgrade!r} is outside the bounded 0..3 family",
            card_id=card_id,
            upgrade=upgrade,
        )
    expected_effect_id = TARGET_EFFECT_ID_BY_UPGRADE[upgrade]
    try:
        uri = f"file:{Path(database).resolve().as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            card = connection.execute(
                """
                SELECT id, upgrade_count, plan_type, play_effects_json
                FROM card
                WHERE id = ? AND upgrade_count = ?
                """,
                (card_id, upgrade),
            ).fetchone()
            if card is None:
                return _unresolved(
                    expected_effect_id,
                    "target-card-version-missing",
                    "target card version was not found in Master",
                    card_id=card_id,
                    upgrade=upgrade,
                )
            effect = connection.execute(
                """
                SELECT id, effect_type, value1, value2, effect_count,
                       effect_turn, raw_json
                FROM effect
                WHERE id = ?
                """,
                (expected_effect_id,),
            ).fetchone()
            if effect is None:
                return _unresolved(
                    expected_effect_id,
                    "target-effect-row-missing",
                    "target additive effect row was not found in Master",
                    card_id=card_id,
                    upgrade=upgrade,
                )
            return card, effect
    except (OSError, sqlite3.Error) as error:
        return _unresolved(
            expected_effect_id,
            "master-read-failed",
            str(error) or type(error).__name__,
            card_id=card_id,
            upgrade=upgrade,
        )


def resolve_plan3_full_power_point_additive_card(
    card_id: str,
    upgrade: int = 0,
    database: Path = DEFAULT_DATABASE,
) -> Plan3FullPowerPointAdditiveResolution:
    """Resolve one exact target card version from read-only Master data."""

    if isinstance(upgrade, bool) or not isinstance(upgrade, int):
        return _unresolved(
            "<unknown-effect>",
            "invalid-target-card-upgrade",
            "upgrade must be an integer",
            card_id=card_id if isinstance(card_id, str) else "",
        )
    rows = _read_card_and_effect(card_id, upgrade, database)
    if isinstance(rows, Plan3FullPowerPointAdditiveUnresolved):
        return rows
    card, effect = rows
    expected_effect_id = TARGET_EFFECT_ID_BY_UPGRADE[upgrade]
    try:
        if card["plan_type"] != "ProducePlanType_Plan3":
            return _unresolved(
                expected_effect_id,
                "target-card-plan-type-mismatch",
                f"plan_type={card['plan_type']!r} is not Plan3",
                card_id=card_id,
                upgrade=upgrade,
            )
        play_effects = json.loads(str(card["play_effects_json"]))
        if not isinstance(play_effects, list) or not all(
            isinstance(entry, dict) for entry in play_effects
        ):
            raise Plan3FullPowerPointAdditiveContractError(
                "play_effects_json must be an object list"
            )
        effect_ids = tuple(
            entry.get("produceExamEffectId", "") for entry in play_effects
        )
        if effect_ids != TARGET_CARD_PLAY_EFFECT_IDS_BY_UPGRADE[upgrade]:
            return _unresolved(
                expected_effect_id,
                "target-card-effect-order-mismatch",
                f"ordered play effects do not match the bounded evidence: {effect_ids!r}",
                card_id=card_id,
                upgrade=upgrade,
            )
        if not (
            len(play_effects) > TARGET_EFFECT_SLOT
            and play_effects[TARGET_EFFECT_SLOT].get(
                "produceExamTriggerId", ""
            )
            == ""
            and play_effects[TARGET_EFFECT_SLOT].get(
                "isOncePlayEffect", False
            )
            is False
        ):
            return _unresolved(
                expected_effect_id,
                "target-card-effect-slot-shape-mismatch",
                "the additive slot is not an immediate non-once effect",
                card_id=card_id,
                upgrade=upgrade,
            )
        contract = Plan3FullPowerPointAdditiveContract.from_master_row(effect)
    except (
        json.JSONDecodeError,
        Plan3FullPowerPointAdditiveContractError,
        TypeError,
        ValueError,
    ) as error:
        return _unresolved(
            expected_effect_id,
            "unsupported-full-power-point-additive-master-shape",
            str(error) or type(error).__name__,
            card_id=card_id,
            upgrade=upgrade,
        )
    if contract.effect_group_ids != TARGET_EFFECT_GROUP_IDS:
        return _unresolved(
            expected_effect_id,
            "target-effect-group-mismatch",
            f"effectGroupIds={contract.effect_group_ids!r} do not match bounded evidence",
            contract=contract,
            card_id=card_id,
            upgrade=upgrade,
        )
    return _resolve_bounded_contract(
        contract,
        card_id=card_id,
        upgrade=upgrade,
        effect_slot=TARGET_EFFECT_SLOT,
    )


def resolve_plan3_full_power_point_additive(
    source: Mapping[str, object] | sqlite3.Row | str,
    upgrade: int | None = None,
    database: Path = DEFAULT_DATABASE,
) -> Plan3FullPowerPointAdditiveResolution:
    """Resolve either a Master row or an exact target card version."""

    if isinstance(source, (Mapping, sqlite3.Row)):
        if upgrade is not None:
            raise TypeError("upgrade is only valid for card-id resolution")
        return resolve_plan3_full_power_point_additive_row(source)
    if not isinstance(source, str):
        raise TypeError("source must be a card id or Master row")
    if upgrade is None:
        raise TypeError("card-id resolution requires upgrade")
    return resolve_plan3_full_power_point_additive_card(
        source, upgrade, database
    )


def execute_plan3_full_power_point_additive(
    resolution: Plan3FullPowerPointAdditiveResolution
    | Plan3FullPowerPointAdditiveContract,
    runtime: Plan3FullPowerPointAdditiveRuntime,
) -> Plan3FullPowerPointAdditiveExecution:
    """Execute only a proven contract; unresolved branches are identity ops."""

    if not isinstance(runtime, Plan3FullPowerPointAdditiveRuntime):
        raise TypeError("runtime must be a typed additive runtime")
    if isinstance(resolution, Plan3FullPowerPointAdditiveContract):
        resolution = _resolve_bounded_contract(resolution)
    if isinstance(resolution, Plan3FullPowerPointAdditiveUnresolved):
        multiple = runtime.multiple
        return Plan3FullPowerPointAdditiveExecution(
            resolution=resolution,
            before=runtime,
            after=runtime,
            before_multiple=multiple,
            after_multiple=multiple,
        )
    if not isinstance(resolution, Plan3FullPowerPointAdditiveExecutable):
        raise TypeError("invalid FullPowerPointAdditive resolution")
    before_multiple = runtime.multiple
    try:
        after = runtime.install(resolution.contract)
    except (Plan3FullPowerPointAdditiveContractError, TypeError) as error:
        unresolved = _unresolved(
            resolution.contract.effect_id,
            "full-power-point-additive-runtime-shape-unsupported",
            str(error) or type(error).__name__,
            contract=resolution.contract,
            card_id=resolution.card_id,
            upgrade=resolution.upgrade,
        )
        return Plan3FullPowerPointAdditiveExecution(
            resolution=unresolved,
            before=runtime,
            after=runtime,
            before_multiple=before_multiple,
            after_multiple=before_multiple,
        )
    return Plan3FullPowerPointAdditiveExecution(
        resolution=resolution,
        before=runtime,
        after=after,
        before_multiple=before_multiple,
        after_multiple=after.multiple,
    )


@dataclass(frozen=True, slots=True)
class Plan3FullPowerPointGain:
    """One native direct FullPowerPoint gain after the additive multiplier."""

    base_value: int
    fixed_value: int
    multiple: float
    gained: int
    upper_cap: int | None = None

    def __post_init__(self) -> None:
        _int32(self.base_value, "base_value")
        _int32(self.fixed_value, "fixed_value")
        _int32(self.gained, "gained")
        if not math.isfinite(self.multiple):
            raise ValueError("multiple must be finite")
        if self.upper_cap is not None:
            raise Plan3FullPowerPointAdditiveContractError(
                "FullPowerPointAdditive has no proven upper cap"
            )

    @property
    def rounded_formula(self) -> str:
        return "ceil(float32((base_value + fixed_value) * multiple))"

    def to_dict(self) -> dict[str, object]:
        return {
            "base_value": self.base_value,
            "fixed_value": self.fixed_value,
            "multiple": self.multiple,
            "gained": self.gained,
            "upper_cap": self.upper_cap,
            "rounded_formula": self.rounded_formula,
        }

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)


def calculate_plan3_full_power_point_gain(
    runtime: Plan3FullPowerPointAdditiveRuntime,
    base_value: int,
    *,
    fixed_value: int = 0,
) -> Plan3FullPowerPointGain:
    """Calculate the native direct point delta without mutating any state."""

    if not isinstance(runtime, Plan3FullPowerPointAdditiveRuntime):
        raise TypeError("runtime must be a typed additive runtime")
    _int32(base_value, "base_value")
    _int32(fixed_value, "fixed_value")
    combined = _signed_int32(base_value + fixed_value)
    multiple = runtime.multiple
    product = _f32(multiple * _f32(combined))
    if not math.isfinite(product):
        raise Plan3FullPowerPointAdditiveContractError(
            "native point product is not finite"
        )
    gained = math.ceil(product)
    if not _INT32_MIN <= gained <= _INT32_MAX:
        raise Plan3FullPowerPointAdditiveContractError(
            "native point gain is outside Int32"
        )
    return Plan3FullPowerPointGain(
        base_value=base_value,
        fixed_value=fixed_value,
        multiple=multiple,
        gained=gained,
        upper_cap=None,
    )


@dataclass(frozen=True, slots=True)
class Plan3FullPowerPointGainExecution:
    """Minimal immutable bridge result for the existing Plan3 scalar state."""

    before: Plan3State
    after: Plan3State
    runtime: Plan3FullPowerPointAdditiveRuntime
    gain: Plan3FullPowerPointGain | None
    supported: bool
    unsupported_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.before, Plan3State) or not isinstance(
            self.after, Plan3State
        ):
            raise TypeError("before and after must be Plan3State")
        if not isinstance(self.runtime, Plan3FullPowerPointAdditiveRuntime):
            raise TypeError("runtime must be additive runtime")
        if self.supported and self.gain is None:
            raise ValueError("supported gain execution needs a gain")
        if not self.supported and self.after is not self.before:
            raise Plan3FullPowerPointAdditiveContractError(
                "unsupported core hook must preserve state identity"
            )
        if self.unsupported_reason is not None and not isinstance(
            self.unsupported_reason, str
        ):
            raise TypeError("unsupported_reason must be text or None")

    @property
    def applied(self) -> bool:
        return self.supported

    def to_dict(self) -> dict[str, object]:
        return {
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "runtime": self.runtime.to_dict(),
            "gain": None if self.gain is None else self.gain.to_dict(),
            "supported": self.supported,
            "unsupported_reason": self.unsupported_reason,
        }

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)


def apply_plan3_full_power_point_gain(
    state: Plan3State,
    runtime: Plan3FullPowerPointAdditiveRuntime,
    base_value: int,
    *,
    fixed_value: int = 0,
) -> Plan3FullPowerPointGainExecution:
    """Minimal core hook for an ordered direct FullPowerPoint effect.

    Call this at the existing ``FullPowerPoint`` effect slot, before the
    caller advances to the next ordered slot.  It updates the current and
    cumulative Plan3 gauge fields by the proven native delta.  No upper cap
    is introduced here; the native direct point method does not apply one.
    """

    if not isinstance(state, Plan3State):
        raise TypeError("state must be Plan3State")
    if not isinstance(runtime, Plan3FullPowerPointAdditiveRuntime):
        raise TypeError("runtime must be a typed additive runtime")
    try:
        state.validate(allow_completed=True)
        if base_value < 0 or fixed_value < 0:
            raise Plan3FullPowerPointAdditiveContractError(
                "bounded FullPowerPoint gain hook accepts non-negative inputs"
            )
        gain = calculate_plan3_full_power_point_gain(
            runtime,
            base_value,
            fixed_value=fixed_value,
        )
    except (
        Plan3FullPowerPointAdditiveContractError,
        TypeError,
        ValueError,
    ) as error:
        return Plan3FullPowerPointGainExecution(
            before=state,
            after=state,
            runtime=runtime,
            gain=None,
            supported=False,
            unsupported_reason=str(error) or type(error).__name__,
        )
    after = replace(
        state,
        full_power_points=_native_nonnegative_add(
            state.full_power_points, gain.gained
        ),
        full_power_points_total=_native_nonnegative_add(
            state.full_power_points_total, gain.gained
        ),
    )
    return Plan3FullPowerPointGainExecution(
        before=state,
        after=after,
        runtime=runtime,
        gain=gain,
        supported=True,
    )


def load_plan3_full_power_point_additive_rows(
    database: Path = DEFAULT_DATABASE,
) -> tuple[Plan3FullPowerPointAdditiveResolution, ...]:
    """Resolve exactly the four current-coverage card versions."""

    return tuple(
        resolve_plan3_full_power_point_additive_card(
            TARGET_CARD_ID,
            upgrade,
            database,
        )
        for upgrade in range(4)
    )


# Short aliases keep the bounded slice convenient without changing the
# explicit Plan3-prefixed public names.
FullPowerPointAdditiveContract = Plan3FullPowerPointAdditiveContract
FullPowerPointAdditiveLayer = Plan3FullPowerPointAdditiveLayer
FullPowerPointAdditiveRuntime = Plan3FullPowerPointAdditiveRuntime
FullPowerPointAdditiveExecutable = Plan3FullPowerPointAdditiveExecutable
FullPowerPointAdditiveUnresolved = Plan3FullPowerPointAdditiveUnresolved
FullPowerPointAdditiveResolution = Plan3FullPowerPointAdditiveResolution
FullPowerPointAdditiveExecution = Plan3FullPowerPointAdditiveExecution
resolve_full_power_point_additive = resolve_plan3_full_power_point_additive
resolve_full_power_point_additive_card = (
    resolve_plan3_full_power_point_additive_card
)
execute_full_power_point_additive = execute_plan3_full_power_point_additive
calculate_full_power_point_gain = calculate_plan3_full_power_point_gain
apply_full_power_point_gain = apply_plan3_full_power_point_gain
