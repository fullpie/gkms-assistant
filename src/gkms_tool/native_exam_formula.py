"""Pure Android v3.2.3 exam arithmetic reconstructed from IL2CPP.

This module is intentionally independent from screen recognition and mutable
shadow state.  Callers must first resolve the status getters in native call
order, then pass their observed values here.  Unsupported/non-finite numeric
domains raise instead of silently inventing a result.

Proven native bodies:

* ``ExamEffectUtility.CalculateAddingParameter`` at ``0x7E5DAF8``;
* ``ExamEffectUtility.AddParameter/AddParameterFix`` at ``0x7E5E4C8`` and
  ``0x7E5E518``;
* ``ExamEffectUtility.CalculateAddBlock`` at ``0x7E5E6E4``;
* ``ExamEffectUtility.GetRatioEffectIntValue`` at ``0x7E5E9AC``;
* ``ExamCardData.GetStaminaCost`` at ``0x808F588``;
* ``ExamSequence.ConsumeCardCost`` at ``0x7ED040C`` and its payment helpers.

No process memory is read.  The companion reports under ``docs/`` pin the
Android 3.2.3 libil2cpp and metadata hashes and preserve instruction evidence.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable


INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
F32_POSITIVE_EPSILON = struct.unpack("<f", bytes.fromhex("17b7d138"))[0]
F32_NEGATIVE_EPSILON = struct.unpack("<f", bytes.fromhex("17b7d1b8"))[0]


class NativeFormulaDomainError(ValueError):
    """Input is outside the normal finite domain proven by the native audit."""


class IdolStatusType(IntEnum):
    UNKNOWN = 0
    CONCENTRATION = 1
    PRESERVATION = 2
    FULL_POWER = 3
    OVER_PRESERVATION = 4


class ProduceParameterType(IntEnum):
    UNKNOWN = 0
    VOCAL = 1
    DANCE = 2
    VISUAL = 3


class StaminaGrowEffectType(IntEnum):
    COST_REDUCE = 12
    COST_ADD = 13
    COST_PENETRATE_REDUCE = 14
    COST_PENETRATE_ADD = 15


def f32(value: int | float) -> float:
    """Round one normal Python numeric value to IEEE-754 binary32."""

    try:
        result = struct.unpack("<f", struct.pack("<f", float(value)))[0]
    except (OverflowError, struct.error) as error:
        raise NativeFormulaDomainError(f"value is outside float32: {value!r}") from error
    if not math.isfinite(result):
        raise NativeFormulaDomainError(f"non-finite float32 is unsupported: {value!r}")
    return result


def _fadd(left: int | float, right: int | float) -> float:
    return f32(f32(left) + f32(right))


def _fsub(left: int | float, right: int | float) -> float:
    return f32(f32(left) - f32(right))


def _fmul(left: int | float, right: int | float) -> float:
    return f32(f32(left) * f32(right))


def _fdiv(left: int | float, right: int | float) -> float:
    denominator = f32(right)
    if denominator == 0.0:
        raise NativeFormulaDomainError("float32 division by zero")
    return f32(f32(left) / denominator)


def _checked_i32(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if not INT32_MIN <= value <= INT32_MAX:
        raise NativeFormulaDomainError(f"{label} is outside Int32: {value}")
    return value


def _checked_i64(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if not INT64_MIN <= value <= INT64_MAX:
        raise NativeFormulaDomainError(f"{label} is outside Int64: {value}")
    return value


def _i32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _i64(value: int) -> int:
    value &= 0xFFFFFFFFFFFFFFFF
    return value - 0x10000000000000000 if value & 0x8000000000000000 else value


def _iadd(left: int, right: int) -> int:
    return _i32(left + right)


def _isub(left: int, right: int) -> int:
    return _i32(left - right)


def _imul(left: int, right: int) -> int:
    return _i32(left * right)


def _rounded_f32_to_i32(value: float, *, direction: str) -> int:
    value = f32(value)
    if direction == "ceil":
        result = math.ceil(value)
    elif direction == "floor":
        result = math.floor(value)
    elif direction == "zero":
        result = math.trunc(value)
    else:  # pragma: no cover - private programmer error
        raise AssertionError(direction)
    return _checked_i32(result, f"float32 {direction} result")


def _rounded_f32_to_i64(value: float, *, direction: str) -> int:
    value = f32(value)
    if direction == "ceil":
        result = math.ceil(value)
    elif direction == "floor":
        result = math.floor(value)
    elif direction == "zero":
        result = math.trunc(value)
    else:  # pragma: no cover - private programmer error
        raise AssertionError(direction)
    return _checked_i64(result, f"float32 {direction} Int64 result")


def ceil_f32_to_i32(value: float) -> int:
    return _rounded_f32_to_i32(value, direction="ceil")


def floor_f32_to_i32(value: float) -> int:
    return _rounded_f32_to_i32(value, direction="floor")


def truncate_f32_to_i32(value: float) -> int:
    return _rounded_f32_to_i32(value, direction="zero")


def ceil_f32_to_i64(value: float) -> int:
    return _rounded_f32_to_i64(value, direction="ceil")


def permille_to_f32(value: int) -> float:
    return _fdiv(f32(_checked_i32(value, "permille")), f32(1000.0))


def get_ratio_effect_int_value(value: int, ratio: int, *, is_ceil: bool) -> int:
    """Exact normal-finite body of native ``GetRatioEffectIntValue``."""

    value = _checked_i32(value, "value")
    ratio = _checked_i32(ratio, "ratio")
    if value < 1:
        return 0
    rate = permille_to_f32(ratio)
    product = _fmul(rate, f32(value))
    if is_ceil:
        return ceil_f32_to_i32(_fadd(product, F32_NEGATIVE_EPSILON))
    return floor_f32_to_i32(_fadd(product, F32_POSITIVE_EPSILON))


@dataclass(frozen=True, slots=True)
class AddingParameterAdditionalData:
    multiple_parameter_buff_rate: float = 1.0
    multiple_enthusiastic_rate: float = 1.0
    multiple_concentration_rate: float = 1.0
    multiple_full_power_rate: float = 1.0


@dataclass(frozen=True, slots=True)
class AddingParameterSettings:
    parameter_buff_permille: int = 1000
    parameter_buff_multiple_per_turn_permille: int = 0
    gimmick_parameter_debuff_permille: int = 1000
    lesson_depend_review_aggressive_permille: int = 0
    lesson_depend_review_aggressive_max_permille: int = 0
    concentration_lesson_permille: tuple[int, int] = (1000, 1000)
    preservation_lesson_permille: tuple[int, int] = (1000, 1000)
    full_power_lesson_permille: int = 1000
    over_preservation_lesson_permille: int = 0


@dataclass(frozen=True, slots=True)
class AddingParameterStatus:
    slump: bool = False
    parameter_buff: bool = False
    parameter_buff_multiple_per_turn: bool = False
    parameter_buff_turn: int = 0
    parameter_debuff: bool = False
    lesson_buff: int = 0
    lesson_debuff: int = 0
    enthusiastic_buff: int = 0
    lesson_parameter_multiple: float = 1.0
    lesson_parameter_multiple_down: float = 0.0
    lesson_parameter_multiple_depend_review_or_aggressive: bool = False
    review: int = 0
    aggressive: int = 0
    lesson_buff_multiple: float = 1.0
    lesson_change_more_than: int = -1
    lesson_change_less_than: int = -1
    idol_status_type: IdolStatusType = IdolStatusType.UNKNOWN
    idol_status_step: int = 1
    concentration_lesson_multiple_additive: float = 0.0
    full_power_lesson_multiple_additive: float = 0.0


def _step_permille(values: tuple[int, int], step: int, label: str) -> int:
    _checked_i32(step, f"{label} step")
    # Native compares only with 1; every other value selects the second slot.
    return values[0] if step == 1 else values[1]


def _adjusted_additional_rate(rate: float) -> float:
    # Native deliberately emits (rate - 1) + 1 in two S-register operations.
    return _fadd(_fsub(f32(rate), f32(1.0)), f32(1.0))


def _adding_parameter_stance_factor(
    status: AddingParameterStatus,
    settings: AddingParameterSettings,
    additional: AddingParameterAdditionalData | None,
) -> float:
    stance = int(status.idol_status_type)
    if stance == IdolStatusType.UNKNOWN:
        return f32(1.0)
    if stance == IdolStatusType.OVER_PRESERVATION:
        # This unscaled conversion is surprising but proven at 0x7E5E258.
        return f32(_checked_i32(settings.over_preservation_lesson_permille, "over-preservation lesson permille"))
    if stance == IdolStatusType.PRESERVATION:
        permille = _step_permille(
            settings.preservation_lesson_permille,
            status.idol_status_step,
            "preservation",
        )
        return permille_to_f32(permille)
    if stance == IdolStatusType.CONCENTRATION:
        permille = _step_permille(
            settings.concentration_lesson_permille,
            status.idol_status_step,
            "concentration",
        )
        delta = _fsub(permille_to_f32(permille), f32(1.0))
        delta = _fadd(delta, status.concentration_lesson_multiple_additive)
        rate = (
            f32(1.0)
            if additional is None
            else _adjusted_additional_rate(additional.multiple_concentration_rate)
        )
        return _fadd(f32(1.0), _fmul(delta, rate))
    if stance == IdolStatusType.FULL_POWER:
        delta = _fsub(
            permille_to_f32(settings.full_power_lesson_permille), f32(1.0)
        )
        delta = _fadd(delta, status.full_power_lesson_multiple_additive)
        rate = (
            f32(1.0)
            if additional is None
            else _adjusted_additional_rate(additional.multiple_full_power_rate)
        )
        return _fadd(f32(1.0), _fmul(delta, rate))
    # Native switch default is the same exact 1.0f factor as Unknown.
    return f32(1.0)


def calculate_adding_parameter(
    value: int,
    *,
    is_buff_active: bool,
    status: AddingParameterStatus,
    settings: AddingParameterSettings,
    additional: AddingParameterAdditionalData | None = None,
) -> int:
    """Exact normal-finite arithmetic of Android v3.2.3 score addition."""

    value = _checked_i32(value, "value")
    if not is_buff_active:
        return value
    if status.slump:
        return 0

    parameter_buff_factor = f32(1.0)
    if status.parameter_buff:
        delta = _fsub(
            permille_to_f32(settings.parameter_buff_permille), f32(1.0)
        )
        if status.parameter_buff_multiple_per_turn:
            product = _imul(
                _checked_i32(settings.parameter_buff_multiple_per_turn_permille, "parameter buff multiple-per-turn permille"),
                _checked_i32(status.parameter_buff_turn, "parameter buff turn"),
            )
            delta = _fadd(delta, permille_to_f32(product))
        if additional is not None:
            delta = _fmul(delta, additional.multiple_parameter_buff_rate)
        parameter_buff_factor = _fadd(delta, f32(1.0))

    parameter_debuff_factor = permille_to_f32(
        settings.gimmick_parameter_debuff_permille
        if status.parameter_debuff
        else 1000
    )
    lesson_down_factor = max(
        f32(0.0),
        _fsub(f32(1.0), status.lesson_parameter_multiple_down),
    )

    depend_factor = f32(0.0)
    if status.lesson_parameter_multiple_depend_review_or_aggressive:
        minimum = min(
            _checked_i32(status.review, "review"),
            _checked_i32(status.aggressive, "aggressive"),
        )
        depend_factor = min(
            _fmul(
                permille_to_f32(settings.lesson_depend_review_aggressive_permille),
                f32(minimum),
            ),
            permille_to_f32(settings.lesson_depend_review_aggressive_max_permille),
        )

    enthusiastic_term = _checked_i32(status.enthusiastic_buff, "enthusiastic buff")
    if additional is not None:
        enthusiastic_term = ceil_f32_to_i32(
            _fmul(additional.multiple_enthusiastic_rate, f32(enthusiastic_term))
        )

    clamped_value = _checked_i32(status.lesson_change_more_than, "more-than override")
    if clamped_value < 0:
        clamped_value = value
    less_result = _checked_i32(status.lesson_change_less_than, "less-than override")
    if less_result >= 0:
        clamped_value = less_result

    lesson_buff_term = ceil_f32_to_i64(
        _fmul(
            status.lesson_buff_multiple,
            f32(_checked_i32(status.lesson_buff, "lesson buff")),
        )
    )
    # These additions are emitted in X registers and Math.Max<long> is used.
    base = _i64(clamped_value + lesson_buff_term)
    base = _i64(base - _checked_i32(status.lesson_debuff, "lesson debuff"))
    base = max(0, _i64(base + enthusiastic_term))

    scaled_base = _fmul(parameter_buff_factor, f32(base))
    scaled_base = _fmul(parameter_debuff_factor, scaled_base)
    lesson_multiple = _fadd(status.lesson_parameter_multiple, depend_factor)
    raw = _fmul(lesson_multiple, scaled_base)
    raw = _fmul(lesson_down_factor, raw)
    raw = _fmul(
        _adding_parameter_stance_factor(status, settings, additional), raw
    )
    result = ceil_f32_to_i64(_fadd(raw, F32_NEGATIVE_EPSILON))
    # Native clamps the Int64 conversion before narrowing the method return to W0.
    return _i32(min(INT32_MAX, result))


@dataclass(frozen=True, slots=True)
class ParameterApplicationStatus:
    """Mutable model inputs consumed by native AddParameter/AddParameterFix."""

    judge_parameter: int
    limit_border: int = -1
    clear_border: int = -1
    current_turn_total_add_parameter: int = 0
    slump: bool = False
    is_battle: bool = False
    current_parameter_type: ProduceParameterType = ProduceParameterType.UNKNOWN
    battle_bonus_permille_vocal: int = 1000
    battle_bonus_permille_dance: int = 1000
    battle_bonus_permille_visual: int = 1000
    judge_parameter_vocal: int = 0
    judge_parameter_dance: int = 0
    judge_parameter_visual: int = 0


@dataclass(frozen=True, slots=True)
class ParameterApplication:
    """One native per-hit mutation and its difference payload."""

    before: int
    after: int
    requested: int
    battle_adjusted: int
    pre_fix_parameter: int
    actual_parameter: int
    current_turn_total_add_parameter: int
    judge_parameter_vocal: int
    judge_parameter_dance: int
    judge_parameter_visual: int
    clear_changed: bool
    perfect_changed: bool


def _battle_bonus_permille(status: ParameterApplicationStatus) -> int:
    parameter_type = int(status.current_parameter_type)
    if parameter_type == ProduceParameterType.VOCAL:
        return _checked_i32(
            status.battle_bonus_permille_vocal, "battle vocal bonus permille"
        )
    if parameter_type == ProduceParameterType.DANCE:
        return _checked_i32(
            status.battle_bonus_permille_dance, "battle dance bonus permille"
        )
    # Native's bonus getter treats Unknown and every non Vocal/Dance value as
    # Visual.  The later per-attribute accumulator does not: it updates only
    # the three recognized enum values.
    return _checked_i32(
        status.battle_bonus_permille_visual, "battle visual bonus permille"
    )


def calculate_battle_parameter_bonus(
    value: int, *, status: ParameterApplicationStatus
) -> int:
    """Exact float32 battle transform used by CalcCurrentTurnBattleBonus."""

    value = _checked_i64(value, "battle parameter value")
    product = _fmul(
        permille_to_f32(_battle_bonus_permille(status)),
        f32(value),
    )
    return ceil_f32_to_i64(_fadd(product, F32_NEGATIVE_EPSILON))


def apply_parameter_add(
    adding_parameter: int, *, status: ParameterApplicationStatus
) -> ParameterApplication:
    """Apply one AddParameter call, including per-hit limits and differences.

    ``CalculateAddingParameter`` and this operation intentionally remain two
    separate helpers.  Native LessonEffectExecutor calls them once per hit,
    and AddParameter independently reads Slump again before AddParameterFix.
    """

    adding_parameter = _checked_i32(adding_parameter, "adding parameter")
    before = _checked_i32(status.judge_parameter, "judge parameter")
    limit_border = _checked_i32(status.limit_border, "limit border")
    clear_border = _checked_i32(status.clear_border, "clear border")
    current_total = _checked_i32(
        status.current_turn_total_add_parameter,
        "current turn total add parameter",
    )
    vocal = _checked_i32(status.judge_parameter_vocal, "judge parameter vocal")
    dance = _checked_i32(status.judge_parameter_dance, "judge parameter dance")
    visual = _checked_i32(status.judge_parameter_visual, "judge parameter visual")

    requested = 0 if status.slump else adding_parameter
    adjusted = (
        calculate_battle_parameter_bonus(requested, status=status)
        if status.is_battle
        else requested
    )
    pre_fix64 = min(adjusted, INT32_MAX)
    pre_fix_parameter = _i32(pre_fix64)

    actual = pre_fix64
    if limit_border >= 0 and _i64(before + pre_fix64) > limit_border:
        actual = _i32(limit_border - before)

    candidate = _i64(before + actual)
    if candidate >= INT32_MAX + 1:
        after = INT32_MAX
        overflow = _i32(candidate - INT32_MAX)
        actual = _i64(actual - overflow)
    else:
        after = _i32(candidate)

    actual_i32 = _i32(actual)
    current_total = _iadd(current_total, actual_i32)
    if status.is_battle:
        parameter_type = int(status.current_parameter_type)
        if parameter_type == ProduceParameterType.VOCAL:
            vocal = _iadd(vocal, actual_i32)
        elif parameter_type == ProduceParameterType.DANCE:
            dance = _iadd(dance, actual_i32)
        elif parameter_type == ProduceParameterType.VISUAL:
            visual = _iadd(visual, actual_i32)

    return ParameterApplication(
        before=before,
        after=after,
        requested=requested,
        battle_adjusted=adjusted,
        pre_fix_parameter=pre_fix_parameter,
        actual_parameter=actual_i32,
        current_turn_total_add_parameter=current_total,
        judge_parameter_vocal=vocal,
        judge_parameter_dance=dance,
        judge_parameter_visual=visual,
        clear_changed=before < clear_border <= after,
        perfect_changed=(
            limit_border >= 0 and before < limit_border <= after
        ),
    )


@dataclass(frozen=True, slots=True)
class AddBlockSettings:
    block_add_down_permille: int = 1000


@dataclass(frozen=True, slots=True)
class AddBlockStatus:
    block_restriction: bool = False
    aggressive: int = 0
    block_add_down: bool = False
    block_add_down_restriction_for_ratio: bool = False
    block_add_down_restriction_for_fix: bool = False
    block_add_down_fix_peek: int = 0
    block_add_down_fix_consume: int = 0


def calculate_add_block(
    value: int,
    *,
    is_buff_active: bool,
    status: AddBlockStatus,
    settings: AddBlockSettings,
    additional_multiple_aggressive_rate: float = 1.0,
) -> int:
    """Exact normal-finite value/modifier order of ``CalculateAddBlock``."""

    value = _checked_i32(value, "value")
    if value < 1:
        return 0
    if not is_buff_active:
        return max(0, value)
    if status.block_restriction:
        return 0

    aggressive_delta = ceil_f32_to_i32(
        _fmul(
            f32(_checked_i32(status.aggressive, "aggressive")),
            additional_multiple_aggressive_rate,
        )
    )
    value = _iadd(value, aggressive_delta)
    if value < 1:
        return max(0, value)

    if status.block_add_down and not status.block_add_down_restriction_for_ratio:
        ratio = truncate_f32_to_i32(
            _fsub(
                f32(1000.0),
                f32(_checked_i32(settings.block_add_down_permille, "block add-down permille")),
            )
        )
        value = _isub(
            value,
            get_ratio_effect_int_value(value, ratio, is_ceil=False),
        )
    if value < 1:
        return max(0, value)

    peek = _checked_i32(status.block_add_down_fix_peek, "block add-down fix peek")
    if peek >= 1 and not status.block_add_down_restriction_for_fix:
        value = _isub(
            value,
            _checked_i32(
                status.block_add_down_fix_consume,
                "block add-down fix consume",
            ),
        )
    return max(0, value)


@dataclass(frozen=True, slots=True)
class StaminaGrowEffect:
    effect_type: StaminaGrowEffectType
    value: int


def apply_stamina_grow(
    value: int,
    *,
    penetrate: bool,
    effects: Iterable[StaminaGrowEffect] | None,
) -> int:
    """Apply one grow list; a non-empty list owns its own zero-floor."""

    value = _checked_i32(value, "stamina grow value")
    if effects is None:
        return value
    rows = tuple(effects)
    if not rows:
        return value
    allowed = (
        {
            StaminaGrowEffectType.COST_PENETRATE_REDUCE,
            StaminaGrowEffectType.COST_PENETRATE_ADD,
        }
        if penetrate
        else {
            StaminaGrowEffectType.COST_REDUCE,
            StaminaGrowEffectType.COST_ADD,
        }
    )
    result = value
    for effect in rows:
        if not isinstance(effect, StaminaGrowEffect):
            raise TypeError("effects must contain StaminaGrowEffect")
        kind = StaminaGrowEffectType(effect.effect_type)
        amount = _checked_i32(effect.value, "stamina grow effect value")
        if kind not in allowed:
            continue
        if kind in {
            StaminaGrowEffectType.COST_REDUCE,
            StaminaGrowEffectType.COST_PENETRATE_REDUCE,
        }:
            result = _isub(result, amount)
        else:
            result = _iadd(result, amount)
    return max(0, result)


def get_stamina_cost(
    raw: int,
    *,
    penetrate: bool,
    master_grow: Iterable[StaminaGrowEffect] | None = None,
    runtime_grow: Iterable[StaminaGrowEffect] | None = None,
    temporary_specify: Iterable[int] | None = None,
) -> int:
    """Exact two grow layers followed by the local minimum override."""

    layer0 = apply_stamina_grow(raw, penetrate=penetrate, effects=master_grow)
    layer1 = apply_stamina_grow(layer0, penetrate=penetrate, effects=runtime_grow)
    if temporary_specify is None:
        return layer1
    values = tuple(
        _checked_i32(value, "temporary stamina specify")
        for value in temporary_specify
    )
    return layer1 if not values else min(values)


@dataclass(frozen=True, slots=True)
class StaminaPaymentSettings:
    concentration_permille: tuple[int, int] = (1000, 1000)
    preservation_permille: tuple[int, int] = (1000, 1000)
    over_preservation_permille: int = 1000
    consumption_down_permille: int = 0
    consumption_down_add_permille: int = 0
    consumption_add_permille: int = 0
    consumption_add_down_permille: int = 0
    reduce_change_value: int = 0


@dataclass(frozen=True, slots=True)
class StaminaPaymentStatus:
    idol_status_type: IdolStatusType = IdolStatusType.UNKNOWN
    idol_status_step: int = 1
    consumption_down: bool = False
    consumption_down_add: bool = False
    consumption_add: bool = False
    consumption_add_down: bool = False
    consumption_add_fix: int = 0
    consumption_down_fix: int = 0
    reduce_change_threshold: int = -1
    reduce_change_active: bool = False


def _stamina_stance_factor(
    status: StaminaPaymentStatus, settings: StaminaPaymentSettings
) -> float:
    stance = int(status.idol_status_type)
    if stance == IdolStatusType.CONCENTRATION:
        return permille_to_f32(
            _step_permille(
                settings.concentration_permille,
                status.idol_status_step,
                "concentration stamina",
            )
        )
    if stance == IdolStatusType.PRESERVATION:
        return permille_to_f32(
            _step_permille(
                settings.preservation_permille,
                status.idol_status_step,
                "preservation stamina",
            )
        )
    if stance == IdolStatusType.OVER_PRESERVATION:
        return permille_to_f32(settings.over_preservation_permille)
    return f32(1.0)


def calculate_effective_stamina_cost(
    value: int,
    *,
    status: StaminaPaymentStatus,
    settings: StaminaPaymentSettings,
) -> int:
    """Payment-stage stance/global/fix ordering before block/HP splitting."""

    value = _checked_i32(value, "stamina payment value")
    scaled = _fmul(f32(value), _stamina_stance_factor(status, settings))
    if status.consumption_down:
        amount = (
            settings.consumption_down_add_permille
            if status.consumption_down_add
            else settings.consumption_down_permille
        )
        amount = _checked_i32(amount, "stamina consumption-down permille")
        scaled = _fmul(
            scaled,
            _fdiv(_fsub(f32(1000.0), f32(amount)), f32(1000.0)),
        )
    if status.consumption_add:
        amount = (
            settings.consumption_add_down_permille
            if status.consumption_add_down
            else settings.consumption_add_permille
        )
        amount = _checked_i32(amount, "stamina consumption-add permille")
        scaled = _fmul(
            scaled,
            _fdiv(_fadd(f32(1000.0), f32(amount)), f32(1000.0)),
        )

    result = ceil_f32_to_i32(scaled)
    result = _iadd(
        result,
        max(0, _checked_i32(status.consumption_add_fix, "stamina add fix")),
    )
    down_fix = _checked_i32(status.consumption_down_fix, "stamina down fix")
    if down_fix >= 1:
        result = max(0, _isub(result, down_fix))
    threshold = _checked_i32(
        status.reduce_change_threshold, "stamina reduce-change threshold"
    )
    if result <= threshold and status.reduce_change_active:
        result = _checked_i32(settings.reduce_change_value, "stamina reduce-change value")
    return result


@dataclass(frozen=True, slots=True)
class StaminaPayment:
    effective_cost: int
    stamina_loss: int
    block_loss: int
    new_stamina: int
    new_block: int


def split_stamina_payment(
    effective_cost: int,
    *,
    penetrate: bool,
    current_stamina: int,
    max_stamina: int,
    current_block: int,
) -> StaminaPayment:
    """Apply the native penetrate/block split and resource clamps."""

    effective_cost = _checked_i32(effective_cost, "effective stamina cost")
    current_stamina = _checked_i32(current_stamina, "current stamina")
    max_stamina = _checked_i32(max_stamina, "max stamina")
    current_block = _checked_i32(current_block, "current block")
    if effective_cost < 0 or current_stamina < 0 or max_stamina < 0 or current_block < 0:
        raise NativeFormulaDomainError("payment resources must be non-negative")
    available_block = 0 if penetrate else current_block
    stamina_loss = max(_isub(effective_cost, available_block), 0)
    block_loss = _isub(effective_cost, stamina_loss)
    new_block = max(_isub(current_block, block_loss), 0)
    new_stamina = min(max(_isub(current_stamina, stamina_loss), 0), max_stamina)
    return StaminaPayment(
        effective_cost=effective_cost,
        stamina_loss=stamina_loss,
        block_loss=block_loss,
        new_stamina=new_stamina,
        new_block=new_block,
    )


def calculate_buff_cost(
    value: int,
    *,
    consumption_down: bool,
    consumption_down_permille: int,
    consumption_add: bool,
    consumption_add_permille: int,
) -> int:
    """Native buff-cost global down/add order with one final ceiling."""

    scaled = f32(_checked_i32(value, "buff cost value"))
    if consumption_down:
        consumption_down_permille = _checked_i32(
            consumption_down_permille, "buff consumption-down permille"
        )
        scaled = _fmul(
            scaled,
            _fdiv(
                _fsub(f32(1000.0), f32(consumption_down_permille)),
                f32(1000.0),
            ),
        )
    if consumption_add:
        consumption_add_permille = _checked_i32(
            consumption_add_permille, "buff consumption-add permille"
        )
        scaled = _fmul(
            scaled,
            _fdiv(
                _fadd(f32(1000.0), f32(consumption_add_permille)),
                f32(1000.0),
            ),
        )
    return ceil_f32_to_i32(scaled)
