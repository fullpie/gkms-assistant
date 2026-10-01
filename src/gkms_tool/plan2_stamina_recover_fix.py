"""Standalone Android v3.2.3 contract for ``ExamStaminaRecoverFix``.

This module is deliberately outside :mod:`plan2_state`.  The native effect
executor receives stamina through ``ExamEffectCalculateContext`` and mutates
the native ``ExamParameterModel``; keeping those two values as explicit
runtime inputs makes this a small, pure evidence model rather than an
additional Plan2 state layer.

The body is version-locked to the local Android v3.2.3 analysis pair:

* ``StaminaRecoverFixEffectExecutor`` constructor ``0x7E8D6B0`` reads only
  ``IExamEffect.get_EffectValue1`` (interface slot 2);
* ``ExecuteEffect`` ``0x7E8D768`` calls
  ``ExamEffectUtility.CalculateStaminaRecover`` and then
  ``ExamEffectUtility.AddStaminaFix``;
* ``CalculateStaminaRecover`` is ``0x7E5EC2C`` and
  ``AddStaminaFix`` is ``0x7E5EDE0``.

Unknown catalog shapes are unresolved instead of being converted into a
guessed action.  The evaluator is pure: it returns immutable before/after
values and an ordered native-side-effect trace, and never mutates a caller's
runtime object.
"""

from __future__ import annotations

import math
import sqlite3
import struct
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping


EFFECT_TYPE = "ProduceExamEffectType_ExamStaminaRecoverFix"
EXECUTOR_TYPE = "Campus.InGame.Exam.StaminaRecoverFixEffectExecutor"
MULTIPLE_EFFECT_TYPE = "ProduceExamEffectType_ExamStaminaRecoverMultiple"

INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1
_F32_THOUSAND = 1000.0
_F32_NEGATIVE_EPSILON = struct.unpack(
    "<f", bytes.fromhex("17b7d1b8")
)[0]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE = PROJECT_ROOT / "var" / "master.sqlite3"

ANDROID_STAMINA_RECOVER_FIX_EVIDENCE = {
    "executor": {
        "symbol": "StaminaRecoverFixEffectExecutor.ExecuteEffect",
        "constructor_va": "0x7E8D6B0",
        "execute_va": "0x7E8D768",
        "fact": "constructor reads effectValue1; execute calls calculate then fixed add",
    },
    "formula": {
        "calculate_va": "0x7E5EC2C",
        "add_fix_va": "0x7E5EDE0",
        "fact": "restriction/additive/cap precede difference, setter, and callback",
    },
}


class StaminaRecoverFixDomainError(ValueError):
    """A typed input is outside the finite Int32/native domain."""


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise StaminaRecoverFixDomainError(f"{label} must be non-empty text")
    return value


def _optional_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise StaminaRecoverFixDomainError(f"{label} must be text")
    return value


def _i32_input(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StaminaRecoverFixDomainError(f"{label} must be an Int32 integer")
    if not INT32_MIN <= value <= INT32_MAX:
        raise StaminaRecoverFixDomainError(f"{label} is outside Int32")
    return value


def _i32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _iadd(left: int, right: int) -> int:
    return _i32(left + right)


def _isub(left: int, right: int) -> int:
    return _i32(left - right)


def _f32(value: int | float) -> float:
    try:
        result = struct.unpack("<f", struct.pack("<f", float(value)))[0]
    except (OverflowError, struct.error) as exc:
        raise StaminaRecoverFixDomainError(
            f"value is outside finite float32: {value!r}"
        ) from exc
    if not math.isfinite(result):
        raise StaminaRecoverFixDomainError(
            f"value is outside finite float32: {value!r}"
        )
    return result


def _fadd(left: int | float, right: int | float) -> float:
    return _f32(_f32(left) + _f32(right))


def _fmul(left: int | float, right: int | float) -> float:
    return _f32(_f32(left) * _f32(right))


def _fdiv(left: int | float, right: int | float) -> float:
    denominator = _f32(right)
    if denominator == 0.0:
        raise StaminaRecoverFixDomainError("float32 division by zero")
    return _f32(_f32(left) / denominator)


def _ceil_f32_to_i32(value: float) -> int:
    rounded = math.ceil(_f32(value))
    if not INT32_MIN <= rounded <= INT32_MAX:
        raise StaminaRecoverFixDomainError(
            "float32 ceiling cannot be represented as Int32"
        )
    return rounded


def _get_ratio_effect_int_value(value: int, ratio: int) -> int:
    """The ceil branch used by native ``CalculateStaminaRecover``.

    The native helper is only reached for an active ``StaminaRecoverAdd``
    status.  Its normal finite path is binary32 ratio/product arithmetic,
    subtraction of the exact ``-0.0001f`` constant, then ``FCVTPS`` (ceil).
    """

    if value < 1:
        return 0
    rate = _fdiv(_f32(_i32(ratio)), _F32_THOUSAND)
    product = _fmul(rate, _f32(value))
    return _ceil_f32_to_i32(_fadd(product, _F32_NEGATIVE_EPSILON))


@dataclass(frozen=True, slots=True)
class StaminaRecoverFixContract:
    """One normalized, proven ``ExamStaminaRecoverFix`` effect row.

    ``effect_value1`` intentionally permits zero and negative synthetic
    probes.  The native executor accepts the Int32 getter and the shared
    recovery helper returns zero for values below one.  The other fields are
    shape gates: the local Master rows have zero/empty values there, and an
    unproven non-zero shape is not executable by this standalone contract.
    """

    effect_id: str
    effect_value1: int
    effect_value2: int = 0
    effect_count: int = 0
    effect_turn: int = 0
    effect_type: str = EFFECT_TYPE
    status_enchant_id: str = ""
    chain_effect_id: str = ""

    def __post_init__(self) -> None:
        _require_text(self.effect_id, "effect_id")
        if self.effect_type != EFFECT_TYPE:
            raise StaminaRecoverFixDomainError(
                f"unsupported effect_type: {self.effect_type!r}"
            )
        _i32_input(self.effect_value1, "effect_value1")
        _i32_input(self.effect_value2, "effect_value2")
        _i32_input(self.effect_count, "effect_count")
        _i32_input(self.effect_turn, "effect_turn")
        _optional_text(self.status_enchant_id, "status_enchant_id")
        _optional_text(self.chain_effect_id, "chain_effect_id")
        if self.effect_value2 != 0:
            raise StaminaRecoverFixDomainError(
                "effect_value2 is not the proven Fix shape"
            )
        if self.effect_count != 0:
            raise StaminaRecoverFixDomainError(
                "effect_count is not the proven Fix shape"
            )
        if self.effect_turn != 0:
            raise StaminaRecoverFixDomainError(
                "effect_turn is not the proven Fix shape"
            )
        if self.status_enchant_id or self.chain_effect_id:
            raise StaminaRecoverFixDomainError(
                "nested status/chain data is not the proven Fix shape"
            )

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, object]
    ) -> "StaminaRecoverFixContract":
        """Parse a Master-like mapping, returning no guessed shape.

        Both normalized SQLite names and the original Master JSON names are
        accepted.  Display-only fields may remain in a Master payload; any
        non-empty nested execution link is rejected.
        """

        if not isinstance(raw, Mapping):
            raise StaminaRecoverFixDomainError("effect payload must be a mapping")

        def pick(*names: str, required: bool = True) -> object:
            for name in names:
                if name in raw:
                    return raw[name]
            if required:
                raise StaminaRecoverFixDomainError(
                    f"missing effect field: {names[0]}"
                )
            return ""

        nested_text_fields = (
            ("produceExamStatusEnchantId", "status_enchant_id"),
            ("chainProduceExamEffectId", "chain_effect_id"),
            ("produceCardStatusEnchantId", None),
            ("produceExamTriggerId", None),
            ("produceCardSearchId", None),
            ("targetProduceCardId", None),
        )
        for original_name, normalized_name in nested_text_fields:
            names = (original_name, normalized_name) if normalized_name else (original_name,)
            value = pick(*names, required=False)
            if value not in ("", None):
                raise StaminaRecoverFixDomainError(
                    f"unsupported nested Fix field: {original_name}"
                )

        for name in ("chainProduceExamEffectIds", "chain_effect_ids"):
            value = pick(name, required=False)
            if value not in (None, "", [], ()):
                raise StaminaRecoverFixDomainError(
                    "unsupported nested chain effect list"
                )

        effect_id = pick("id", "effect_id")
        effect_type = pick("effectType", "effect_type")
        effect_value1 = pick("effectValue1", "value1", "effect_value1")
        effect_value2 = pick("effectValue2", "value2", "effect_value2")
        effect_count = pick("effectCount", "effect_count")
        effect_turn = pick("effectTurn", "effect_turn")
        status_enchant_id = pick(
            "produceExamStatusEnchantId", "status_enchant_id", required=False
        )
        chain_effect_id = pick(
            "chainProduceExamEffectId", "chain_effect_id", required=False
        )
        return cls(
            effect_id=_require_text(effect_id, "effect_id"),
            effect_value1=_i32_input(effect_value1, "effect_value1"),
            effect_value2=_i32_input(effect_value2, "effect_value2"),
            effect_count=_i32_input(effect_count, "effect_count"),
            effect_turn=_i32_input(effect_turn, "effect_turn"),
            effect_type=_require_text(effect_type, "effect_type"),
            status_enchant_id=_optional_text(status_enchant_id, "status_enchant_id"),
            chain_effect_id=_optional_text(chain_effect_id, "chain_effect_id"),
        )


def try_parse_stamina_recover_fix(
    raw: Mapping[str, object],
) -> StaminaRecoverFixContract | None:
    """Return a contract only for a known shape; otherwise return ``None``."""

    try:
        return StaminaRecoverFixContract.from_mapping(raw)
    except (KeyError, TypeError, ValueError, StaminaRecoverFixDomainError):
        return None


MASTER_STAMINA_RECOVER_FIX_ROWS: tuple[StaminaRecoverFixContract, ...] = tuple(
    StaminaRecoverFixContract(
        effect_id=f"e_effect-exam_stamina_recover_fix-{value:04d}",
        effect_value1=value,
    )
    for value in range(1, 26)
)


def load_master_stamina_recover_fix_rows(
    database: Path = DEFAULT_DATABASE,
) -> tuple[StaminaRecoverFixContract, ...]:
    """Read the normalized local Master table without changing any artifact."""

    path = Path(database)
    if not path.is_file():
        return ()
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        rows = connection.execute(
            """
            SELECT id, effect_type, value1, value2, effect_count,
                   effect_turn, status_enchant_id, chain_effect_id
              FROM effect
             WHERE effect_type = ?
             ORDER BY value1, id
            """,
            (EFFECT_TYPE,),
        ).fetchall()
    return tuple(
        StaminaRecoverFixContract(
            effect_id=str(row[0]),
            effect_value1=_i32_input(row[2], "value1"),
            effect_value2=_i32_input(row[3], "value2"),
            effect_count=_i32_input(row[4], "effect_count"),
            effect_turn=_i32_input(row[5], "effect_turn"),
            effect_type=str(row[1]),
            status_enchant_id=str(row[6]),
            chain_effect_id=str(row[7]),
        )
        for row in rows
    )


@dataclass(frozen=True, slots=True)
class StaminaRecoverFixRuntime:
    """Explicit native inputs; this is not a Plan2State extension."""

    stamina: int
    max_stamina: int
    stamina_recover_restricted: bool = False
    stamina_recover_add_permil: int | None = None
    block: int = 0

    def __post_init__(self) -> None:
        _i32_input(self.stamina, "stamina")
        _i32_input(self.max_stamina, "max_stamina")
        _i32_input(self.block, "block")
        if not isinstance(self.stamina_recover_restricted, bool):
            raise StaminaRecoverFixDomainError(
                "stamina_recover_restricted must be bool"
            )
        if self.stamina_recover_add_permil is not None:
            _i32_input(self.stamina_recover_add_permil, "stamina_recover_add_permil")


@dataclass(frozen=True, slots=True)
class StaminaDifference:
    """The stamina difference payload recorded before the native setter."""

    before_stamina: int
    after_stamina: int
    before_max_stamina: int
    after_max_stamina: int
    current_block: int
    difference_type: str = "Stamina"

    def __post_init__(self) -> None:
        for name in (
            "before_stamina",
            "after_stamina",
            "before_max_stamina",
            "after_max_stamina",
            "current_block",
        ):
            _i32_input(getattr(self, name), f"difference.{name}")
        if self.difference_type != "Stamina":
            raise StaminaRecoverFixDomainError(
                "Fix difference type must be Stamina"
            )


@dataclass(frozen=True, slots=True)
class StaminaRecoverFixEvaluation:
    """Pure result, including unresolved/fail-closed outcomes."""

    contract: StaminaRecoverFixContract | None
    runtime_before: StaminaRecoverFixRuntime
    runtime_after: StaminaRecoverFixRuntime
    executable: bool
    simulated: bool
    reason: str | None
    requested_value: int | None
    adjusted_recovery_value: int | None
    calculated_recovery: int | None
    predicted_stamina: int | None
    difference: StaminaDifference | None
    trace: tuple[str, ...]

    @property
    def actual_stamina(self) -> int:
        """The state visible after this pure operation (or before in simulate)."""

        return self.runtime_after.stamina


def _runtime_reason(runtime: StaminaRecoverFixRuntime) -> str | None:
    if runtime.max_stamina < 0:
        return "native AddStaminaFix rejects negative max_stamina"
    if runtime.stamina < 0:
        return "negative stamina is outside the supported runtime shape"
    if runtime.stamina > runtime.max_stamina:
        return "stamina exceeds max_stamina"
    return None


def _calculate_stamina_recover(
    value: int, runtime: StaminaRecoverFixRuntime
) -> tuple[int, int]:
    """Return ``(calculated_recovery, adjusted_value)`` in native order."""

    if value < 1:
        return 0, 0
    if runtime.stamina_recover_restricted:
        return 0, 0

    adjusted = value
    permil = runtime.stamina_recover_add_permil
    if permil is not None and permil >= 1:
        ratio = _i32(permil - 1000)
        adjusted = _iadd(value, _get_ratio_effect_int_value(value, ratio))

    summed = _iadd(runtime.stamina, adjusted)
    capped = min(runtime.max_stamina, summed)
    return _isub(capped, runtime.stamina), adjusted


def _add_stamina_fix(
    value: int, runtime: StaminaRecoverFixRuntime
) -> int:
    """The final ``max(0, min(current + value, max))`` native cap."""

    summed = _iadd(runtime.stamina, value)
    bounded = summed if summed < runtime.max_stamina else runtime.max_stamina
    return 0 if summed < 0 else bounded


def _unresolved(
    runtime: StaminaRecoverFixRuntime,
    *,
    contract: StaminaRecoverFixContract | None,
    simulated: bool,
    reason: str,
    trace: tuple[str, ...],
) -> StaminaRecoverFixEvaluation:
    return StaminaRecoverFixEvaluation(
        contract=contract,
        runtime_before=runtime,
        runtime_after=runtime,
        executable=False,
        simulated=simulated,
        reason=reason,
        requested_value=None,
        adjusted_recovery_value=None,
        calculated_recovery=None,
        predicted_stamina=None,
        difference=None,
        trace=trace,
    )


def evaluate_stamina_recover_fix(
    effect: StaminaRecoverFixContract | Mapping[str, object],
    runtime: StaminaRecoverFixRuntime,
    *,
    simulate: bool = False,
) -> StaminaRecoverFixEvaluation:
    """Evaluate one exact Fix row without mutating ``runtime``.

    A simulated evaluation still computes the native predicted value and
    difference payload, but its ``runtime_after`` is the identical input
    object and no setter/callback is emitted.  This gives callers a useful
    preview while making simulation identity explicit.
    """

    if not isinstance(runtime, StaminaRecoverFixRuntime):
        raise TypeError("runtime must be StaminaRecoverFixRuntime")
    if not isinstance(simulate, bool):
        raise TypeError("simulate must be bool")

    contract: StaminaRecoverFixContract | None
    if isinstance(effect, StaminaRecoverFixContract):
        contract = effect
    elif isinstance(effect, Mapping):
        contract = try_parse_stamina_recover_fix(effect)
    else:
        contract = None

    if contract is None:
        return _unresolved(
            runtime,
            contract=None,
            simulated=simulate,
            reason="unknown or unsupported ExamStaminaRecoverFix shape",
            trace=("contract:unresolved",),
        )

    runtime_reason = _runtime_reason(runtime)
    if runtime_reason is not None:
        return _unresolved(
            runtime,
            contract=contract,
            simulated=simulate,
            reason=runtime_reason,
            trace=("runtime:unresolved",),
        )

    requested = contract.effect_value1
    calculated, adjusted = _calculate_stamina_recover(requested, runtime)
    predicted = _add_stamina_fix(calculated, runtime)
    difference = StaminaDifference(
        before_stamina=runtime.stamina,
        after_stamina=predicted,
        before_max_stamina=runtime.max_stamina,
        after_max_stamina=runtime.max_stamina,
        current_block=runtime.block,
    )
    trace = [
        "executor:calculate_stamina_recover",
        "executor:add_stamina_fix",
        "difference:create",
        "difference:set_stamina",
        "difference:append_to_context",
    ]
    if simulate:
        trace.extend(("simulate:identity", "parameter:set_stamina:skipped", "callback:skipped"))
        after = runtime
    else:
        trace.extend(("parameter:set_stamina", "callback:effect_difference_executed"))
        after = replace(runtime, stamina=predicted)
    return StaminaRecoverFixEvaluation(
        contract=contract,
        runtime_before=runtime,
        runtime_after=after,
        executable=True,
        simulated=simulate,
        reason=None,
        requested_value=requested,
        adjusted_recovery_value=adjusted,
        calculated_recovery=calculated,
        predicted_stamina=predicted,
        difference=difference,
        trace=tuple(trace),
    )


def catalog_row_dict(row: StaminaRecoverFixContract) -> dict[str, object]:
    """Serialize the normalized columns used by the narrow audit."""

    return {
        "id": row.effect_id,
        "effect_type": row.effect_type,
        "effect_value1": row.effect_value1,
        "effect_value2": row.effect_value2,
        "effect_count": row.effect_count,
        "effect_turn": row.effect_turn,
        "status_enchant_id": row.status_enchant_id,
        "chain_effect_id": row.chain_effect_id,
    }


__all__ = [
    "ANDROID_STAMINA_RECOVER_FIX_EVIDENCE",
    "DEFAULT_DATABASE",
    "EFFECT_TYPE",
    "EXECUTOR_TYPE",
    "INT32_MAX",
    "INT32_MIN",
    "MASTER_STAMINA_RECOVER_FIX_ROWS",
    "MULTIPLE_EFFECT_TYPE",
    "StaminaDifference",
    "StaminaRecoverFixContract",
    "StaminaRecoverFixDomainError",
    "StaminaRecoverFixEvaluation",
    "StaminaRecoverFixRuntime",
    "catalog_row_dict",
    "evaluate_stamina_recover_fix",
    "load_master_stamina_recover_fix_rows",
    "try_parse_stamina_recover_fix",
]
