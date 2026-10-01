"""Plan-neutral ordered dispatcher for native PlayCountInterval listeners.

Every active ``TriggerEffectStatusEffect`` owns its own phase counter.  This
kernel advances all exact interval listeners in native active/UID order and
routes fired child effects through typed, caller-supplied handlers.  Listener
origin (gimmick, card, item, or memory) is deliberately irrelevant.

All listener/effect/handler shapes are preflighted before the first handler is
called.  Unknown effects therefore fail atomically instead of partially
advancing an earlier counter or applying an earlier listener.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Generic, TypeVar

from .nia_status_enchant import PHASE_PLAY_COUNT_INTERVAL
from .plan3_engine import ActivePlan3StatusEnchant, Plan3Effect


INT32_MAX = (1 << 31) - 1
StateT = TypeVar("StateT")


class PlayCountIntervalDispatchError(ValueError):
    """An active listener or effect has no exact ordered executor."""


@dataclass(frozen=True, slots=True)
class PlayCountIntervalEffectContext:
    listener_sequence: int
    native_uid: int
    instance_id: str
    rule_id: str
    interval: int
    count_before: int
    count_after: int
    effect_index: int


@dataclass(frozen=True, slots=True)
class PlayCountIntervalEffectApplication(Generic[StateT]):
    state: StateT
    evidence: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))


EffectValidator = Callable[[Plan3Effect], tuple[str, ...]]
EffectExecutor = Callable[
    [StateT, Plan3Effect, PlayCountIntervalEffectContext],
    PlayCountIntervalEffectApplication[StateT],
]


@dataclass(frozen=True, slots=True)
class PlayCountIntervalEffectHandler(Generic[StateT]):
    effect_type: str
    validate: EffectValidator
    execute: EffectExecutor[StateT]

    def __post_init__(self) -> None:
        if not isinstance(self.effect_type, str) or not self.effect_type:
            raise ValueError("interval handler effect_type must be non-empty")
        if not callable(self.validate) or not callable(self.execute):
            raise TypeError("interval handler callbacks must be callable")


@dataclass(frozen=True, slots=True)
class OrderedPlayCountIntervalState(Generic[StateT]):
    payload: StateT
    active: tuple[ActivePlan3StatusEnchant, ...]

    def __post_init__(self) -> None:
        active = tuple(self.active)
        if any(not isinstance(value, ActivePlan3StatusEnchant) for value in active):
            raise TypeError(
                "ordered interval active values must be ActivePlan3StatusEnchant"
            )
        instance_ids = tuple(value.instance_id for value in active)
        if any(not value for value in instance_ids) or len(instance_ids) != len(
            set(instance_ids)
        ):
            raise PlayCountIntervalDispatchError(
                "active listener instance IDs must be non-empty and unique"
            )
        object.__setattr__(self, "active", active)


@dataclass(frozen=True, slots=True)
class PlayCountIntervalEffectReceipt:
    context: PlayCountIntervalEffectContext
    effect_id: str
    effect_type: str
    evidence: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.context, PlayCountIntervalEffectContext):
            raise TypeError("effect receipt context must be typed")
        if not isinstance(self.effect_id, str) or not self.effect_id:
            raise ValueError("effect receipt id must be non-empty")
        if not isinstance(self.effect_type, str) or not self.effect_type:
            raise ValueError("effect receipt type must be non-empty")
        object.__setattr__(self, "evidence", tuple(self.evidence))


@dataclass(frozen=True, slots=True)
class PlayCountIntervalFire:
    listener_sequence: int
    native_uid: int
    instance_id: str
    rule_id: str
    interval: int
    count_before: int
    count_after: int
    effect_receipt_start: int
    effect_receipt_count: int


@dataclass(frozen=True, slots=True)
class OrderedPlayCountIntervalExecution(Generic[StateT]):
    before: OrderedPlayCountIntervalState[StateT]
    after: OrderedPlayCountIntervalState[StateT]
    fires: tuple[PlayCountIntervalFire, ...]
    effect_receipts: tuple[PlayCountIntervalEffectReceipt, ...]


def _handler_index(
    handlers: Iterable[PlayCountIntervalEffectHandler[StateT]],
) -> dict[str, PlayCountIntervalEffectHandler[StateT]]:
    if isinstance(handlers, (str, bytes)):
        raise TypeError("handlers must be typed interval handlers")
    result: dict[str, PlayCountIntervalEffectHandler[StateT]] = {}
    for handler in tuple(handlers):
        if not isinstance(handler, PlayCountIntervalEffectHandler):
            raise TypeError("handlers must contain PlayCountIntervalEffectHandler")
        if handler.effect_type in result:
            raise PlayCountIntervalDispatchError(
                f"duplicate interval effect handler: {handler.effect_type}"
            )
        result[handler.effect_type] = handler
    return result


def _replace_phase_count(
    listener: ActivePlan3StatusEnchant,
    count: int,
) -> ActivePlan3StatusEnchant:
    replaced = False
    values: list[tuple[str, int]] = []
    for phase, current in listener.phase_counts:
        if phase == PHASE_PLAY_COUNT_INTERVAL:
            if replaced:
                raise PlayCountIntervalDispatchError(
                    f"duplicate interval phase counter: {listener.instance_id}"
                )
            values.append((phase, count))
            replaced = True
        else:
            values.append((phase, current))
    if not replaced:
        values.append((PHASE_PLAY_COUNT_INTERVAL, count))
    return replace(listener, phase_counts=tuple(values))


def _preflight(
    state: OrderedPlayCountIntervalState[StateT],
    handlers: dict[str, PlayCountIntervalEffectHandler[StateT]],
) -> tuple[tuple[int, ActivePlan3StatusEnchant, int, int], ...]:
    interval: list[tuple[int, ActivePlan3StatusEnchant, int, int]] = []
    prior_uid = 0
    for sequence, listener in enumerate(state.active):
        phases = listener.rule.trigger.phase_types
        if PHASE_PLAY_COUNT_INTERVAL not in phases:
            continue
        if phases != (PHASE_PLAY_COUNT_INTERVAL,):
            raise PlayCountIntervalDispatchError(
                f"unsupported interval phase shape: {listener.instance_id}"
            )
        values = listener.rule.trigger.phase_values
        if (
            len(values) != 1
            or isinstance(values[0], bool)
            or not isinstance(values[0], int)
            or not 1 <= values[0] <= INT32_MAX
        ):
            raise PlayCountIntervalDispatchError(
                f"unsupported interval value: {listener.instance_id}"
            )
        uid = listener.native_uid
        if isinstance(uid, bool) or not isinstance(uid, int) or not 0 <= uid <= INT32_MAX:
            raise PlayCountIntervalDispatchError(
                f"interval listeners are not in native UID order: "
                f"{listener.instance_id}:{uid}"
            )
        # Zero is the compatibility sentinel used by older synthetic/item
        # fixtures.  Exact LocalSave paths carry positive UIDs and must be
        # strictly increasing; zero-ID listeners retain active tuple order.
        if uid > 0:
            if uid <= prior_uid:
                raise PlayCountIntervalDispatchError(
                    f"interval listeners are not in native UID order: "
                    f"{listener.instance_id}:{uid}"
                )
            prior_uid = uid
        elif prior_uid > 0:
            raise PlayCountIntervalDispatchError(
                f"zero-UID interval follows exact native UID: "
                f"{listener.instance_id}"
            )
        if (
            isinstance(listener.remaining_turns, bool)
            or not isinstance(listener.remaining_turns, int)
            or listener.remaining_turns == 0
            or listener.remaining_turns < -1
            or listener.remaining_turns > INT32_MAX
        ):
            raise PlayCountIntervalDispatchError(
                f"inactive interval listener lifecycle: {listener.instance_id}"
            )
        for name in ("max_uses", "uses", "max_uses_per_turn", "uses_this_turn"):
            raw = getattr(listener, name)
            if (
                isinstance(raw, bool)
                or not isinstance(raw, int)
                or not 0 <= raw <= INT32_MAX
            ):
                raise PlayCountIntervalDispatchError(
                    f"invalid interval listener {name}: {listener.instance_id}"
                )
        if (
            (listener.max_uses and listener.uses > listener.max_uses)
            or (
                listener.max_uses_per_turn
                and listener.uses_this_turn > listener.max_uses_per_turn
            )
        ):
            raise PlayCountIntervalDispatchError(
                f"interval listener use count is out of range: {listener.instance_id}"
            )
        phase_names = tuple(phase for phase, _count in listener.phase_counts)
        if len(phase_names) != len(set(phase_names)):
            raise PlayCountIntervalDispatchError(
                f"duplicate listener phase counter: {listener.instance_id}"
            )
        before = listener.phase_count(PHASE_PLAY_COUNT_INTERVAL)
        if before >= INT32_MAX:
            raise PlayCountIntervalDispatchError(
                f"interval phase count overflow: {listener.instance_id}"
            )
        if not listener.rule.effects:
            raise PlayCountIntervalDispatchError(
                f"interval listener has no effects: {listener.instance_id}"
            )
        for effect in listener.rule.effects:
            handler = handlers.get(effect.effect_type)
            if handler is None:
                raise PlayCountIntervalDispatchError(
                    f"unknown interval effect: {listener.instance_id}:"
                    f"{effect.effect_type}:{effect.id}"
                )
            errors = tuple(handler.validate(effect))
            if any(not isinstance(value, str) or not value for value in errors):
                raise TypeError("interval handler validation errors must be text")
            if errors:
                raise PlayCountIntervalDispatchError(
                    f"unsupported interval effect: {listener.instance_id}:"
                    + ",".join(errors)
                )
        will_fire = (before + 1) % values[0] == 0 and not (
            (listener.max_uses > 0 and listener.uses >= listener.max_uses)
            or (
                listener.max_uses_per_turn > 0
                and listener.uses_this_turn >= listener.max_uses_per_turn
            )
        )
        if will_fire and (
            listener.uses >= INT32_MAX
            or listener.uses_this_turn >= INT32_MAX
        ):
            raise PlayCountIntervalDispatchError(
                f"interval listener use count overflow: {listener.instance_id}"
            )
        interval.append((sequence, listener, values[0], before))
    return tuple(interval)


def execute_ordered_play_count_intervals(
    state: OrderedPlayCountIntervalState[StateT],
    handlers: Iterable[PlayCountIntervalEffectHandler[StateT]],
) -> OrderedPlayCountIntervalExecution[StateT]:
    """Advance every active interval listener exactly once and atomically."""

    if not isinstance(state, OrderedPlayCountIntervalState):
        raise TypeError("state must be OrderedPlayCountIntervalState")
    by_effect = _handler_index(handlers)
    prepared = _preflight(state, by_effect)

    payload = state.payload
    active = list(state.active)
    fires: list[PlayCountIntervalFire] = []
    receipts: list[PlayCountIntervalEffectReceipt] = []
    for sequence, listener, interval, before in prepared:
        after_count = before + 1
        updated = _replace_phase_count(listener, after_count)
        active[sequence] = updated
        exhausted = (
            (updated.max_uses > 0 and updated.uses >= updated.max_uses)
            or (
                updated.max_uses_per_turn > 0
                and updated.uses_this_turn >= updated.max_uses_per_turn
            )
        )
        if after_count % interval or exhausted:
            continue
        receipt_start = len(receipts)
        for effect_index, effect in enumerate(updated.rule.effects):
            context = PlayCountIntervalEffectContext(
                listener_sequence=sequence,
                native_uid=updated.native_uid,
                instance_id=updated.instance_id,
                rule_id=updated.rule.id,
                interval=interval,
                count_before=before,
                count_after=after_count,
                effect_index=effect_index,
            )
            try:
                application = by_effect[effect.effect_type].execute(
                    payload, effect, context
                )
            except Exception as error:
                raise PlayCountIntervalDispatchError(
                    f"interval effect execution failed: {updated.instance_id}:"
                    f"{effect.id}:{type(error).__name__}:{error}"
                ) from error
            if not isinstance(application, PlayCountIntervalEffectApplication):
                raise TypeError(
                    "interval effect handler must return "
                    "PlayCountIntervalEffectApplication"
                )
            payload = application.state
            receipts.append(
                PlayCountIntervalEffectReceipt(
                    context,
                    effect.id,
                    effect.effect_type,
                    application.evidence,
                )
            )
        active[sequence] = replace(
            updated,
            uses=(updated.uses + 1 if updated.max_uses > 0 else updated.uses),
            uses_this_turn=(
                updated.uses_this_turn + 1
                if updated.max_uses_per_turn > 0
                else updated.uses_this_turn
            ),
        )
        fires.append(
            PlayCountIntervalFire(
                listener_sequence=sequence,
                native_uid=updated.native_uid,
                instance_id=updated.instance_id,
                rule_id=updated.rule.id,
                interval=interval,
                count_before=before,
                count_after=after_count,
                effect_receipt_start=receipt_start,
                effect_receipt_count=len(receipts) - receipt_start,
            )
        )
    after = OrderedPlayCountIntervalState(payload, tuple(active))
    return OrderedPlayCountIntervalExecution(
        state,
        after,
        tuple(fires),
        tuple(receipts),
    )


__all__ = [
    "INT32_MAX",
    "OrderedPlayCountIntervalExecution",
    "OrderedPlayCountIntervalState",
    "PlayCountIntervalDispatchError",
    "PlayCountIntervalEffectApplication",
    "PlayCountIntervalEffectContext",
    "PlayCountIntervalEffectHandler",
    "PlayCountIntervalEffectReceipt",
    "PlayCountIntervalFire",
    "execute_ordered_play_count_intervals",
]
