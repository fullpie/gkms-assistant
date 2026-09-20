"""Plan-neutral LocalSave progress for native ``TriggerEffectStatusEffect``.

Master data owns a listener's immutable identity (finite/permanent lifetime,
the configured per-turn limit, trigger and effects).  The serializer owns the
current progress.  Keeping those two concerns separate is important when a
save is resumed after one or more turns or trigger activations.

This module deliberately parses only the common lifecycle/counter block.  It
does not infer a card, item, or gimmick owner and it does not weaken the
caller's exact Master identity checks.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum


INT32_MIN = -(2**31)
INT32_MAX = 2**31 - 1

_IDENTITY_EMPTY_ITEM_FIELDS = frozenset(
    {
        "_fireCount",
        "_id",
        "_itemType",
        "_parentCustomItemIds",
        "_reactionCount",
    }
)


class TriggerEffectSerializerProgressError(ValueError):
    """A serialized progress field violates the bounded native contract."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class TriggerEffectSerializerLifecyclePolicy(Enum):
    """How a retained serialized listener may represent its total lifetime.

    Ordinary card/gimmick listeners are restored only while active, so a
    finite total counter must remain positive.  Android keeps equipped-item
    listeners linked after their final activation; those exact item statuses
    are still live serializer objects with ``_limitCount == 0`` and must keep
    reserving their status UID.  Callers must opt into that item-only policy
    explicitly instead of weakening every listener family.
    """

    ACTIVE = "active"
    ITEM_ACTIVE_OR_EXHAUSTED = "item-active-or-exhausted"


def _i32(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TriggerEffectSerializerProgressError("invalid-int32", label)
    if not INT32_MIN <= value <= INT32_MAX:
        raise TriggerEffectSerializerProgressError("int32-out-of-range", label)
    return value


def _bounded(
    value: object,
    label: str,
    *,
    minimum: int,
    maximum: int = INT32_MAX,
) -> int:
    result = _i32(value, label)
    if not minimum <= result <= maximum:
        raise TriggerEffectSerializerProgressError(
            "counter-out-of-range",
            f"{label}={result}; expected={minimum}..{maximum}",
        )
    return result


@dataclass(frozen=True, slots=True)
class TriggerEffectSerializerProgress:
    """The complete mutable serializer block for one active listener."""

    turn: int
    turn_count: int
    limit_count: int
    limit_count_in_turn_remaining: int
    is_passing_turn_start: bool
    phase_counts: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        turn = _i32(self.turn, "progress.turn")
        if turn < -1:
            raise TriggerEffectSerializerProgressError(
                "counter-out-of-range", f"progress.turn={turn}; expected=-1..{INT32_MAX}"
            )
        _bounded(self.turn_count, "progress.turn_count", minimum=0)
        total = _i32(self.limit_count, "progress.limit_count")
        if total < -1:
            raise TriggerEffectSerializerProgressError(
                "counter-out-of-range",
                f"progress.limit_count={total}; expected=-1..{INT32_MAX}",
            )
        per_turn = _i32(
            self.limit_count_in_turn_remaining,
            "progress.limit_count_in_turn_remaining",
        )
        if per_turn < -1:
            raise TriggerEffectSerializerProgressError(
                "counter-out-of-range",
                (
                    "progress.limit_count_in_turn_remaining="
                    f"{per_turn}; expected=-1..{INT32_MAX}"
                ),
            )
        if type(self.is_passing_turn_start) is not bool:
            raise TriggerEffectSerializerProgressError(
                "passing-turn-start-shape", "progress"
            )
        counts = tuple(self.phase_counts)
        if counts != tuple(sorted(counts)):
            raise TriggerEffectSerializerProgressError(
                "phase-count-order", "progress"
            )
        seen: set[str] = set()
        for index, row in enumerate(counts):
            if (
                not isinstance(row, tuple)
                or len(row) != 2
                or not isinstance(row[0], str)
                or not row[0]
            ):
                raise TriggerEffectSerializerProgressError(
                    "phase-count-row-shape", f"progress[{index}]"
                )
            if row[0] in seen:
                raise TriggerEffectSerializerProgressError(
                    "phase-count-key-duplicate", row[0]
                )
            seen.add(row[0])
            _bounded(row[1], f"progress[{index}].current", minimum=0)
        object.__setattr__(self, "phase_counts", counts)

    @property
    def is_finite_exhausted(self) -> bool:
        """Whether the serializer retains a finite listener at total zero."""

        return self.limit_count == 0


def restore_trigger_effect_serializer_progress(
    data: Mapping[str, object],
    *,
    master_turn: int,
    master_total_limit: int,
    master_per_turn_limit: int,
    phase_type_names: Mapping[int, str],
    label: str,
    lifecycle_policy: TriggerEffectSerializerLifecyclePolicy = (
        TriggerEffectSerializerLifecyclePolicy.ACTIVE
    ),
) -> TriggerEffectSerializerProgress:
    """Restore mutable listener progress while pinning Master identity.

    ``master_turn`` and the two Master limits use the constructor values
    proven for StatusEnchant: ``-1`` is unlimited and every finite value is
    positive.  A retained finite listener must still have a positive turn and
    total count.  Its per-turn remaining count may be zero until the next
    TurnStart reset.  Turn and total can be lower than their constructor
    values and are never replaced with those defaults during restoration.
    Equipped-item callers may explicitly select
    ``ITEM_ACTIVE_OR_EXHAUSTED`` because Android retains a finite item
    listener at total zero after its last activation.
    """

    if not isinstance(data, Mapping):
        raise TriggerEffectSerializerProgressError("progress-shape", label)
    if not isinstance(
        lifecycle_policy, TriggerEffectSerializerLifecyclePolicy
    ):
        raise TriggerEffectSerializerProgressError(
            "lifecycle-policy-shape", label
        )
    master_turn = _i32(master_turn, f"{label}.master_turn")
    master_total_limit = _i32(
        master_total_limit, f"{label}.master_total_limit"
    )
    master_per_turn_limit = _i32(
        master_per_turn_limit, f"{label}.master_per_turn_limit"
    )
    if master_turn == 0 or master_turn < -1:
        raise TriggerEffectSerializerProgressError(
            "master-turn-shape", f"{label}:{master_turn}"
        )
    if master_total_limit == 0 or master_total_limit < -1:
        raise TriggerEffectSerializerProgressError(
            "master-total-limit-shape", f"{label}:{master_total_limit}"
        )
    if master_per_turn_limit == 0 or master_per_turn_limit < -1:
        raise TriggerEffectSerializerProgressError(
            "master-per-turn-limit-shape",
            f"{label}:{master_per_turn_limit}",
        )

    limited = data.get("_isTurnLimited")
    expected_limited = master_turn != -1
    if type(limited) is not bool or limited is not expected_limited:
        raise TriggerEffectSerializerProgressError(
            "turn-limited-identity-mismatch",
            f"{label}:actual={limited!r}; expected={expected_limited!r}",
        )
    immutable_per_turn = _i32(
        data.get("_limitCountInTurn"), f"{label}._limitCountInTurn"
    )
    if immutable_per_turn != master_per_turn_limit:
        raise TriggerEffectSerializerProgressError(
            "per-turn-limit-identity-mismatch",
            (
                f"{label}:actual={immutable_per_turn}; "
                f"expected={master_per_turn_limit}"
            ),
        )

    raw_turn = _i32(data.get("_turn"), f"{label}._turn")
    if expected_limited:
        turn = _bounded(raw_turn, f"{label}._turn", minimum=1)
    elif raw_turn != -1:
        raise TriggerEffectSerializerProgressError(
            "unlimited-turn-progress-mismatch", f"{label}:{raw_turn}"
        )
    else:
        turn = raw_turn

    turn_count = _bounded(
        data.get("_turnCount"), f"{label}._turnCount", minimum=0
    )

    raw_total = _i32(data.get("_limitCount"), f"{label}._limitCount")
    if master_total_limit == -1:
        if raw_total != -1:
            raise TriggerEffectSerializerProgressError(
                "unlimited-total-progress-mismatch", f"{label}:{raw_total}"
            )
        total = raw_total
    else:
        total = _bounded(
            raw_total,
            f"{label}._limitCount",
            minimum=(
                0
                if lifecycle_policy
                is TriggerEffectSerializerLifecyclePolicy.ITEM_ACTIVE_OR_EXHAUSTED
                else 1
            ),
            maximum=master_total_limit,
        )

    raw_per_turn = _i32(
        data.get("_limitCountInTurnRemain"),
        f"{label}._limitCountInTurnRemain",
    )
    if master_per_turn_limit == -1:
        if raw_per_turn != -1:
            raise TriggerEffectSerializerProgressError(
                "unlimited-per-turn-progress-mismatch",
                f"{label}:{raw_per_turn}",
            )
        per_turn_remaining = raw_per_turn
    else:
        per_turn_remaining = _bounded(
            raw_per_turn,
            f"{label}._limitCountInTurnRemain",
            minimum=0,
            maximum=master_per_turn_limit,
        )

    passing = data.get("_isPassingTurnStart")
    if type(passing) is not bool:
        raise TriggerEffectSerializerProgressError(
            "passing-turn-start-shape", label
        )

    phase_dictionary = data.get("_phaseCountDictionary")
    if (
        not isinstance(phase_dictionary, Mapping)
        or set(phase_dictionary) != {"_list"}
        or not isinstance(phase_dictionary.get("_list"), list)
    ):
        raise TriggerEffectSerializerProgressError(
            "phase-count-shape", label
        )
    allowed_phases = dict(phase_type_names)
    if any(
        isinstance(key, bool)
        or not isinstance(key, int)
        or not INT32_MIN <= key <= INT32_MAX
        or not isinstance(name, str)
        or not name
        for key, name in allowed_phases.items()
    ):
        raise TriggerEffectSerializerProgressError(
            "phase-authority-shape", label
        )
    parsed_counts: list[tuple[str, int]] = []
    seen_keys: set[int] = set()
    for index, row in enumerate(phase_dictionary["_list"]):
        row_label = f"{label}._phaseCountDictionary[{index}]"
        if (
            not isinstance(row, Mapping)
            or set(row) != {"key", "value"}
            or not isinstance(row.get("value"), Mapping)
            or set(row["value"]) != {"current"}
        ):
            raise TriggerEffectSerializerProgressError(
                "phase-count-row-shape", row_label
            )
        key = _i32(row.get("key"), f"{row_label}.key")
        if key not in allowed_phases:
            raise TriggerEffectSerializerProgressError(
                "phase-count-key-unmapped", f"{row_label}:{key}"
            )
        if key in seen_keys:
            raise TriggerEffectSerializerProgressError(
                "phase-count-key-duplicate", f"{row_label}:{key}"
            )
        seen_keys.add(key)
        current = _bounded(
            row["value"].get("current"),
            f"{row_label}.current",
            minimum=0,
        )
        parsed_counts.append((allowed_phases[key], current))

    return TriggerEffectSerializerProgress(
        turn=turn,
        turn_count=turn_count,
        limit_count=total,
        limit_count_in_turn_remaining=per_turn_remaining,
        is_passing_turn_start=passing,
        phase_counts=tuple(sorted(parsed_counts)),
    )


def is_identity_empty_trigger_item_scratch(value: object) -> bool:
    """Return true only for the serializer's non-owning empty item scratch.

    Fire/reaction counters are mutable scratch progress and may be non-zero;
    an item ID, item type, or parent identity makes the object owning and must
    never pass through this helper.
    """

    if not isinstance(value, Mapping) or set(value) != _IDENTITY_EMPTY_ITEM_FIELDS:
        return False
    if (
        value.get("_id") != ""
        or type(value.get("_itemType")) is not int
        or value.get("_itemType") != 0
        or value.get("_parentCustomItemIds") != []
    ):
        return False
    try:
        _bounded(value.get("_fireCount"), "triggerItem._fireCount", minimum=0)
        _bounded(
            value.get("_reactionCount"),
            "triggerItem._reactionCount",
            minimum=0,
        )
    except TriggerEffectSerializerProgressError:
        return False
    return True


# Backward-compatible Plan2 spellings.  These are aliases, not wrappers, so
# isinstance checks and exception catches keep the exact historical behavior.
Plan2NativeSerializerProgressError = TriggerEffectSerializerProgressError
Plan2NativeSerializerLifecyclePolicy = TriggerEffectSerializerLifecyclePolicy
Plan2NativeSerializerProgress = TriggerEffectSerializerProgress
restore_plan2_native_serializer_progress = restore_trigger_effect_serializer_progress


__all__ = [
    "INT32_MAX",
    "INT32_MIN",
    "Plan2NativeSerializerLifecyclePolicy",
    "Plan2NativeSerializerProgress",
    "Plan2NativeSerializerProgressError",
    "TriggerEffectSerializerLifecyclePolicy",
    "TriggerEffectSerializerProgress",
    "TriggerEffectSerializerProgressError",
    "is_identity_empty_trigger_item_scratch",
    "restore_plan2_native_serializer_progress",
    "restore_trigger_effect_serializer_progress",
]

