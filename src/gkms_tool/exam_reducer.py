"""Small canonical action boundary for the Plan2 native exam simulator.

The horizon implementation predates the callers that need to drive it.  It
therefore exposes a few low-level action classes and two simulation entry
points.  This module gives callers one deliberately small vocabulary and
routes every executable action through the *lifecycle* entry point.  Card
effects and turn rules remain owned by :mod:`plan2_native_horizon`.

``EffectSelect`` is part of the vocabulary because a real exam can expose a
selection prompt.  The current Plan2 lifecycle does not yet have a native
selection continuation, so the reducer reports it as unsupported instead of
pretending that an index can be applied safely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from .plan2_native_horizon import (
    Plan2NativeBlocker,
    Plan2NativeDrinkAction,
    Plan2NativeHorizonError,
    Plan2NativeHorizonState,
    Plan2NativeOfflineAction,
    Plan2NativeOperationReceipt,
    Plan2NativeProgramCatalog,
    _require_plan2_operation_receipt_scope,
    simulate_plan2_native_action_lifecycle,
)


def _nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class PlayCard:
    """Play the settled Hand card identified by its runtime GUID."""

    guid: str

    def __post_init__(self) -> None:
        _nonempty_text(self.guid, "card guid")

    @property
    def kind(self) -> str:
        return "play"

    @property
    def action_id(self) -> str:
        return f"PLAY:{self.guid}"


@dataclass(frozen=True, slots=True)
class UseDrink:
    """Use one ordered inventory slot, bound to its stable runtime instance.

    ``selection`` is the optional selected card GUID used by drinks whose
    native program asks the caller to choose a card.  It is intentionally kept
    as data here; the native horizon remains responsible for validating and
    applying it.
    """

    slot: int
    instance: str
    selection: str | None = None

    def __post_init__(self) -> None:
        _nonnegative_int(self.slot, "drink slot")
        _nonempty_text(self.instance, "drink instance")
        if self.selection is not None:
            _nonempty_text(self.selection, "drink selection")

    @property
    def kind(self) -> str:
        return "drink"

    @property
    def slot_index(self) -> int:
        """Compatibility spelling for the native action field."""

        return self.slot

    @property
    def instance_id(self) -> str:
        """Compatibility spelling for the native action field."""

        return self.instance

    @property
    def selected_card_guid(self) -> str:
        """Native actions use an empty string for an absent selection."""

        return "" if self.selection is None else self.selection

    @property
    def action_id(self) -> str:
        suffix = "" if self.selection is None else f":SELECT:{self.selection}"
        return f"DRINK:{self.slot}:{self.instance}{suffix}"


@dataclass(frozen=True, slots=True)
class EndTurn:
    """Explicitly close the current turn through the native lifecycle."""

    @property
    def kind(self) -> str:
        return "end_turn"

    @property
    def action_id(self) -> str:
        return "END_TURN"


@dataclass(frozen=True, slots=True)
class EffectSelect:
    """Selection indices for a future native effect continuation."""

    indexes: tuple[int, ...]

    def __post_init__(self) -> None:
        indexes = tuple(self.indexes)
        if any(
            isinstance(index, bool) or not isinstance(index, int) or index < 0
            for index in indexes
        ):
            raise ValueError("effect selection indexes must be non-negative integers")
        object.__setattr__(self, "indexes", indexes)

    @property
    def kind(self) -> str:
        return "effect_select"

    @property
    def action_id(self) -> str:
        return "EFFECT_SELECT:" + ",".join(str(index) for index in self.indexes)


CanonicalAction: TypeAlias = PlayCard | UseDrink | EndTurn | EffectSelect


@dataclass(frozen=True, slots=True)
class Transition:
    """One canonical action result.

    ``before`` is always the exact settled input object.  ``after`` is present
    only when the lifecycle completed; unsupported actions leave it as
    ``None`` and explain the reason in ``unsupported``.  ``terminal`` mirrors
    the resulting horizon's terminal flag, while ``trace`` is the native
    lifecycle trace with no extra interpretation layered on top.
    """

    before: Plan2NativeHorizonState
    action: CanonicalAction
    after: Plan2NativeHorizonState | None
    trace: tuple[str, ...] = ()
    unsupported: tuple[Plan2NativeBlocker, ...] = ()
    terminal: bool = False
    operation_receipts: tuple[Plan2NativeOperationReceipt, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.before, Plan2NativeHorizonState):
            raise TypeError("transition before must be Plan2NativeHorizonState")
        if not isinstance(self.action, (PlayCard, UseDrink, EndTurn, EffectSelect)):
            raise TypeError("transition action must be canonical")
        if self.after is not None and not isinstance(
            self.after, Plan2NativeHorizonState
        ):
            raise TypeError("transition after must be Plan2NativeHorizonState or None")
        trace = tuple(self.trace)
        if any(not isinstance(value, str) or not value for value in trace):
            raise ValueError("transition trace must contain non-empty text")
        object.__setattr__(self, "trace", trace)
        unsupported = tuple(self.unsupported)
        if any(not isinstance(value, Plan2NativeBlocker) for value in unsupported):
            raise TypeError("transition unsupported must contain Plan2NativeBlocker")
        object.__setattr__(self, "unsupported", unsupported)
        if not isinstance(self.terminal, bool):
            raise TypeError("transition terminal must be boolean")
        operation_receipts = tuple(self.operation_receipts)
        if any(
            not isinstance(value, Plan2NativeOperationReceipt)
            for value in operation_receipts
        ):
            raise TypeError(
                "transition operation_receipts must contain "
                "Plan2NativeOperationReceipt"
            )
        _require_plan2_operation_receipt_scope(
            operation_receipts,
            label="transition operation_receipts",
        )
        object.__setattr__(self, "operation_receipts", operation_receipts)
        expected_terminal = self.after is not None and self.after.terminal
        if self.terminal != expected_terminal:
            raise ValueError("transition terminal disagrees with after state")

    @property
    def supported(self) -> bool:
        return self.after is not None and not self.unsupported

    @property
    def blockers(self) -> tuple[Plan2NativeBlocker, ...]:
        """Alias for callers migrating from ``Plan2NativeTransition``."""

        return self.unsupported


class Plan2NativeReducer:
    """Canonical adapter around ``simulate_plan2_native_action_lifecycle``.

    The class does not contain card or drink effect logic.  Its sole job is to
    translate canonical actions into the already-tested native action shape,
    invoke the lifecycle boundary, and translate the result back.  In
    particular, callers should use :meth:`reduce` instead of calling either
    low-level horizon simulator directly.
    """

    def __init__(self, catalog: Plan2NativeProgramCatalog) -> None:
        if not isinstance(catalog, Plan2NativeProgramCatalog):
            raise TypeError("catalog must be Plan2NativeProgramCatalog")
        self.catalog = catalog

    def reduce(
        self,
        state: Plan2NativeHorizonState,
        action: CanonicalAction,
    ) -> Transition:
        if not isinstance(state, Plan2NativeHorizonState):
            raise TypeError("state must be Plan2NativeHorizonState")
        if not isinstance(action, (PlayCard, UseDrink, EndTurn, EffectSelect)):
            return self._unsupported(
                state,
                action,
                "canonical-action-unsupported",
                type(action).__name__,
            )

        if isinstance(action, EffectSelect):
            return self._unsupported(
                state,
                action,
                "effect-selection-unsupported",
                action.action_id,
            )

        try:
            native_action = self._native_action(state, action)
        except (Plan2NativeHorizonError, IndexError, TypeError, ValueError) as error:
            blocker = (
                error.blocker
                if isinstance(error, Plan2NativeHorizonError)
                else Plan2NativeBlocker(
                    "canonical-action-unsupported",
                    f"{type(error).__name__}:{error}",
                )
            )
            return Transition(
                before=state,
                action=action,
                after=None,
                unsupported=(blocker,),
            )

        native = simulate_plan2_native_action_lifecycle(
            state,
            native_action,
            self.catalog,
        )
        return Transition(
            before=state,
            action=action,
            after=native.after,
            trace=native.trace,
            unsupported=native.blockers,
            terminal=native.after is not None and native.after.terminal,
            operation_receipts=native.operation_receipts,
        )

    def transition(
        self,
        state: Plan2NativeHorizonState,
        action: CanonicalAction,
    ) -> Transition:
        """Readable alias for :meth:`reduce`."""

        return self.reduce(state, action)

    def apply(
        self,
        state: Plan2NativeHorizonState,
        action: CanonicalAction,
    ) -> Transition:
        """Alias used by state-machine callers that name the step ``apply``."""

        return self.reduce(state, action)

    def __call__(
        self,
        state: Plan2NativeHorizonState,
        action: CanonicalAction,
    ) -> Transition:
        return self.reduce(state, action)

    def _native_action(
        self,
        state: Plan2NativeHorizonState,
        action: PlayCard | UseDrink | EndTurn,
    ) -> Plan2NativeOfflineAction:
        if isinstance(action, PlayCard):
            from .plan2_native_horizon import Plan2NativeAction

            return Plan2NativeAction("play", action.guid)
        if isinstance(action, EndTurn):
            from .plan2_native_horizon import Plan2NativeAction

            return Plan2NativeAction("end_turn")

        # Canonical UseDrink intentionally omits drink_id.  Resolve it from
        # the same ordered inventory that the lifecycle will consume, keeping
        # slot and instance bound together without duplicating drink effects.
        if action.slot >= len(state.drink_runtime.inventory):
            raise Plan2NativeHorizonError(
                "drink-slot-out-of-range",
                str(action.slot),
            )
        selected = state.drink_runtime.resolve(
            action.slot,
            instance_id=action.instance,
            drink_id=state.drink_runtime.inventory[action.slot].drink_id,
        )
        return Plan2NativeDrinkAction(
            slot_index=action.slot,
            instance_id=action.instance,
            drink_id=selected.drink_id,
            selected_card_guid=action.selected_card_guid,
        )

    @staticmethod
    def _unsupported(
        state: Plan2NativeHorizonState,
        action: object,
        code: str,
        detail: str,
    ) -> Transition:
        if not isinstance(action, (PlayCard, UseDrink, EndTurn, EffectSelect)):
            # Keep the result type useful even for a foreign object supplied by
            # an adapter under development.  It cannot be replayed as a
            # canonical action, so report the type error as a stable blocker.
            raise TypeError(
                "unsupported transition requires a canonical action; "
                f"got {type(action).__name__}"
            )
        return Transition(
            before=state,
            action=action,
            after=None,
            unsupported=(Plan2NativeBlocker(code, detail),),
        )


__all__ = [
    "CanonicalAction",
    "EffectSelect",
    "EndTurn",
    "PlayCard",
    "Plan2NativeReducer",
    "Transition",
    "UseDrink",
]
