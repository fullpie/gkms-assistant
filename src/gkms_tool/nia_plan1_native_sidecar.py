"""Plan1 root candidate adapter for the plan-neutral N.I.A. contract.

This module deliberately stops at the existing ``Plan1NativeStageState``
boundary.  It does not decode ExamSave runtime facts, calculate drinks, or
copy a simulator.  Callers must supply a fully prepared Plan1 stage and must
explicitly say whether the current drink inventory and END_TURN availability
are known.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeAlias

from .nia_native_action_contract import (
    NiaNativeAction,
    NiaNativeCandidateSet,
    NiaNativeStepRecord,
)
from .plan1_native_core import (
    Plan1CompiledCard,
    Plan1DeckCompilation,
    Plan1NativeSettings,
)
from .plan1_native_stage import (
    Plan1HandAddSupportResolver,
    Plan1NativeStageState,
    Plan1StageAction,
    enumerate_plan1_legal_actions,
)


PLAN1_NATIVE_ENUMERATOR_AUTHORITY = "plan1-native-root-enumerator-v1"
PLAN1_NATIVE_ENUMERATION_SCHEMA = "gkms.plan1-native-legal-candidate-enumeration.v1"


def _text(value: object, label: str, *, optional: bool = False) -> str:
    if optional and value in {None, ""}:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _nonnegative(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class Plan1NativeDrinkSlot:
    """One explicit ordered drink slot; no effect semantics are inferred."""

    slot_index: int
    drink_id: str
    instance_id: str = ""

    def __post_init__(self) -> None:
        _nonnegative(self.slot_index, "drink slot_index")
        _text(self.drink_id, "drink_id")
        instance = self.instance_id or (
            f"localsave-drink:{self.slot_index}:{self.drink_id}"
        )
        _text(instance, "instance_id")
        object.__setattr__(self, "instance_id", instance)

    @classmethod
    def from_value(cls, value: object, slot_index: int) -> "Plan1NativeDrinkSlot":
        if isinstance(value, cls):
            if value.slot_index != slot_index:
                raise ValueError("drink inventory slot order is not contiguous")
            return value
        if isinstance(value, str):
            return cls(slot_index, value)
        if not isinstance(value, Mapping):
            raise TypeError("drink inventory rows must be IDs or objects")
        drink_id = value.get("drink_id", value.get("_id", value.get("id")))
        instance_id = value.get("instance_id", value.get("instance", ""))
        return cls(
            slot_index,
            _text(drink_id, f"drink_inventory[{slot_index}].drink_id"),
            _text(
                instance_id,
                f"drink_inventory[{slot_index}].instance_id",
                optional=True,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "slot_index": self.slot_index,
            "drink_id": self.drink_id,
            "instance_id": self.instance_id,
        }


@dataclass(frozen=True, slots=True)
class Plan1NativeLegalCandidate:
    """Canonical Plan1 action retaining both GUID and visible Hand slot."""

    kind: str
    action_id: str
    native_kind: str
    hand_slot: int | None = None
    card_guid: str | None = None
    card_id: str | None = None
    card_upgrade: int | None = None
    drink_slot_index: int | None = None
    drink_id: str | None = None
    instance_id: str | None = None
    selected_card_guid: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"play", "drink", "turn-end"}:
            raise ValueError(f"unsupported Plan1 candidate kind: {self.kind}")
        _text(self.action_id, "candidate action_id")
        if self.native_kind not in {"card", "drink", "skip"}:
            raise ValueError(f"unsupported Plan1 native kind: {self.native_kind}")

        if self.kind == "play":
            if self.native_kind != "card":
                raise ValueError("PLAY candidate must have native_kind=card")
            _nonnegative(self.hand_slot, "PLAY hand_slot")
            _text(self.card_guid, "PLAY card_guid")
            _text(self.card_id, "PLAY card_id")
            _nonnegative(self.card_upgrade, "PLAY card_upgrade")
            if self.action_id != f"PLAY:{self.card_guid}":
                raise ValueError("PLAY action_id does not match card_guid")
            if any(
                value is not None
                for value in (
                    self.drink_slot_index,
                    self.drink_id,
                    self.instance_id,
                    self.selected_card_guid,
                )
            ):
                raise ValueError("PLAY candidate cannot carry drink fields")
            return

        if self.kind == "drink":
            if self.native_kind != "drink":
                raise ValueError("DRINK candidate must have native_kind=drink")
            _nonnegative(self.drink_slot_index, "DRINK slot_index")
            _text(self.drink_id, "DRINK drink_id")
            _text(self.instance_id, "DRINK instance_id")
            _text(self.selected_card_guid, "DRINK selected_card_guid", optional=True)
            expected = NiaNativeAction.drink(
                self.drink_slot_index,
                self.drink_id,
                instance_id=self.instance_id,
                selected_card_guid=self.selected_card_guid or "",
            ).action_id
            if self.action_id != expected:
                raise ValueError("DRINK action_id does not match identity")
            if any(
                value is not None
                for value in (self.hand_slot, self.card_guid, self.card_id, self.card_upgrade)
            ):
                raise ValueError("DRINK candidate cannot carry PLAY fields")
            return

        if self.native_kind != "skip" or self.action_id != "END_TURN":
            raise ValueError("END_TURN candidate identity is invalid")
        if any(
            value is not None
            for value in (
                self.hand_slot,
                self.card_guid,
                self.card_id,
                self.card_upgrade,
                self.drink_slot_index,
                self.drink_id,
                self.instance_id,
                self.selected_card_guid,
            )
        ):
            raise ValueError("END_TURN candidate cannot carry card or drink fields")

    @classmethod
    def play(cls, action: Plan1StageAction) -> "Plan1NativeLegalCandidate":
        if not isinstance(action, Plan1StageAction) or not action.legal:
            raise ValueError("Plan1 PLAY candidate requires a legal stage action")
        return cls(
            kind="play",
            action_id=f"PLAY:{action.guid}",
            native_kind="card",
            hand_slot=action.hand_index,
            card_guid=action.guid,
            card_id=action.card.card_id,
            card_upgrade=action.card.effective_upgrade,
        )

    @classmethod
    def from_value(cls, value: object) -> "Plan1NativeLegalCandidate":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("Plan1 candidate must be typed or a mapping")
        raw_kind = value.get("kind")
        kind = {
            "card": "play",
            "use-hand": "play",
            "use_hand": "play",
            "use-drink": "drink",
            "use_drink": "drink",
            "turn_end": "turn-end",
            "end_turn": "turn-end",
            "skip": "turn-end",
        }.get(raw_kind, raw_kind)
        if kind == "play":
            guid = _text(value.get("card_guid", value.get("guid")), "card_guid")
            card_id = _text(value.get("card_id"), "card_id")
            hand_slot = _nonnegative(
                value.get("hand_slot", value.get("hand_index")),
                "hand_slot",
            )
            upgrade = _nonnegative(
                value.get("card_upgrade", value.get("upgrade", 0)),
                "card_upgrade",
            )
            return cls(
                kind="play",
                action_id=_text(value.get("action_id", f"PLAY:{guid}"), "action_id"),
                native_kind=str(value.get("native_kind", "card")),
                hand_slot=hand_slot,
                card_guid=guid,
                card_id=card_id,
                card_upgrade=upgrade,
            )
        if kind == "drink":
            slot = _nonnegative(
                value.get("drink_slot_index", value.get("slot_index")),
                "drink_slot_index",
            )
            drink_id = _text(value.get("drink_id"), "drink_id")
            instance = _text(
                value.get("instance_id", value.get("instance", "")),
                "instance_id",
                optional=True,
            ) or f"localsave-drink:{slot}:{drink_id}"
            selected = _text(
                value.get("selected_card_guid", ""),
                "selected_card_guid",
                optional=True,
            )
            return cls(
                kind="drink",
                action_id=_text(
                    value.get("action_id"),
                    "action_id",
                ),
                native_kind=str(value.get("native_kind", "drink")),
                drink_slot_index=slot,
                drink_id=drink_id,
                instance_id=instance,
                selected_card_guid=selected,
            )
        if kind == "turn-end":
            return cls.end_turn()
        raise ValueError("mapping has no supported Plan1 candidate kind")

    @classmethod
    def drink(cls, slot: Plan1NativeDrinkSlot) -> "Plan1NativeLegalCandidate":
        if not isinstance(slot, Plan1NativeDrinkSlot):
            raise TypeError("slot must be Plan1NativeDrinkSlot")
        action = NiaNativeAction.drink(
            slot.slot_index,
            slot.drink_id,
            instance_id=slot.instance_id,
        )
        return cls(
            kind="drink",
            action_id=action.action_id,
            native_kind="drink",
            drink_slot_index=slot.slot_index,
            drink_id=slot.drink_id,
            instance_id=slot.instance_id,
        )

    @classmethod
    def end_turn(cls) -> "Plan1NativeLegalCandidate":
        return cls(kind="turn-end", action_id="END_TURN", native_kind="skip")

    def to_native_action(self) -> NiaNativeAction:
        if self.kind == "play":
            assert self.card_guid is not None and self.card_id is not None
            return NiaNativeAction.play(self.card_guid, card_id=self.card_id)
        if self.kind == "drink":
            assert self.drink_slot_index is not None and self.drink_id is not None
            return NiaNativeAction.drink(
                self.drink_slot_index,
                self.drink_id,
                instance_id=self.instance_id or "",
                selected_card_guid=self.selected_card_guid or "",
            )
        return NiaNativeAction.end_turn()

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "action_id": self.action_id,
            "native_kind": self.native_kind,
        }
        if self.kind == "play":
            payload.update(
                {
                    "hand_slot": self.hand_slot,
                    "card_guid": self.card_guid,
                    "card_id": self.card_id,
                    "upgrade": self.card_upgrade,
                }
            )
        elif self.kind == "drink":
            payload.update(
                {
                    "slot_index": self.drink_slot_index,
                    "instance_id": self.instance_id,
                    "drink_id": self.drink_id,
                    "selected_card_guid": self.selected_card_guid or "",
                }
            )
        return payload


DrinkCandidateProvider: TypeAlias = Callable[
    [Plan1NativeStageState, tuple[Plan1NativeDrinkSlot, ...]],
    Sequence[Plan1NativeLegalCandidate],
]


@dataclass(frozen=True, slots=True)
class Plan1NativeLegalCandidateEnumeration:
    """Typed root enumeration with explicit authority and completeness."""

    candidates: tuple[Plan1NativeLegalCandidate, ...]
    authority: str = PLAN1_NATIVE_ENUMERATOR_AUTHORITY
    complete: bool = True
    blockers: tuple[str, ...] = ()
    schema: str = PLAN1_NATIVE_ENUMERATION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PLAN1_NATIVE_ENUMERATION_SCHEMA:
            raise ValueError("unsupported Plan1 candidate enumeration schema")
        candidates = tuple(self.candidates)
        if any(
            not isinstance(value, Plan1NativeLegalCandidate) for value in candidates
        ):
            raise TypeError("candidates must contain Plan1NativeLegalCandidate values")
        action_ids = tuple(value.action_id for value in candidates)
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("Plan1 root candidates must have unique action IDs")
        _text(self.authority, "candidate authority")
        if type(self.complete) is not bool:
            raise TypeError("candidate complete must be boolean")
        blockers = tuple(self.blockers)
        if any(not isinstance(value, str) or not value for value in blockers):
            raise ValueError("candidate blockers must contain non-empty text")
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "blockers", tuple(dict.fromkeys(blockers)))
        if self.complete and (not candidates or blockers):
            raise ValueError("complete Plan1 enumeration requires candidates and no blockers")

    @property
    def canonical_candidates(self) -> tuple[dict[str, object], ...]:
        return tuple(value.to_dict() for value in self.candidates)

    @property
    def native_candidate_set(self) -> NiaNativeCandidateSet:
        return NiaNativeCandidateSet(
            tuple(value.to_native_action() for value in self.candidates),
            authority=self.authority,
            complete=self.complete,
            blockers=self.blockers,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "authority": self.authority,
            "complete": self.complete,
            "blockers": list(self.blockers),
            "candidates": list(self.canonical_candidates),
            "native_candidate_set": self.native_candidate_set.to_dict(),
        }


def _normalise_drink_inventory(
    value: Sequence[object],
) -> tuple[Plan1NativeDrinkSlot, ...]:
    if isinstance(value, (str, bytes, bytearray)):
        raise TypeError("drink_inventory must be an array")
    return tuple(
        Plan1NativeDrinkSlot.from_value(item, index)
        for index, item in enumerate(value)
    )


def enumerate_plan1_native_legal_candidates(
    state: Plan1NativeStageState,
    compilation: Plan1DeckCompilation | Sequence[Plan1CompiledCard],
    *,
    settings: Plan1NativeSettings | None = None,
    hand_add_support_resolver: Plan1HandAddSupportResolver | None = None,
    drink_inventory: Sequence[object] | None,
    drink_candidate_provider: DrinkCandidateProvider | None = None,
    include_end_turn: bool = False,
    end_turn_available: bool | None = None,
    authority: str = PLAN1_NATIVE_ENUMERATOR_AUTHORITY,
) -> Plan1NativeLegalCandidateEnumeration:
    """Adapt the existing Plan1 card enumerator into a root candidate set.

    ``drink_inventory=None`` means the inventory is unknown, not empty.  A
    non-empty inventory without an injected legality provider is incomplete;
    no card-only subset is advertised as complete.  END_TURN is added only
    when the caller explicitly requests it and supplies availability evidence.
    """

    if not isinstance(state, Plan1NativeStageState):
        raise TypeError("state must be Plan1NativeStageState")
    if drink_candidate_provider is not None and not callable(
        drink_candidate_provider
    ):
        raise TypeError("drink_candidate_provider must be callable or None")
    if type(include_end_turn) is not bool:
        raise TypeError("include_end_turn must be boolean")
    if end_turn_available is not None and type(end_turn_available) is not bool:
        raise TypeError("end_turn_available must be boolean or None")

    blockers: list[str] = []
    candidates: list[Plan1NativeLegalCandidate] = []
    try:
        actions = enumerate_plan1_legal_actions(
            state,
            compilation,
            settings=settings,
            hand_add_support_resolver=hand_add_support_resolver,
        )
    except (TypeError, ValueError, OverflowError) as error:
        return Plan1NativeLegalCandidateEnumeration(
            candidates=(),
            authority=authority,
            complete=False,
            blockers=(f"plan1-enumeration-failed:{type(error).__name__}:{error}",),
        )

    for action in actions:
        if action.legal:
            try:
                candidates.append(Plan1NativeLegalCandidate.play(action))
            except (TypeError, ValueError) as error:
                blockers.append(
                    f"hand[{action.hand_index}]-candidate-invalid:{type(error).__name__}"
                )
        else:
            for blocker in action.blockers:
                blockers.append(
                    f"hand[{action.hand_index}]:{blocker.code}"
                    + (f":{blocker.detail}" if blocker.detail else "")
                )
            if action.transition is None and not action.blockers:
                blockers.append(f"hand[{action.hand_index}]:action-not-evaluated")

    if drink_inventory is None:
        blockers.append("drink-inventory-unavailable")
        drinks: tuple[Plan1NativeDrinkSlot, ...] = ()
    else:
        try:
            drinks = _normalise_drink_inventory(drink_inventory)
        except (TypeError, ValueError) as error:
            blockers.append(f"drink-inventory-invalid:{type(error).__name__}:{error}")
            drinks = ()

    if drinks:
        if drink_candidate_provider is None:
            blockers.append("drink-candidate-provider-missing")
        else:
            try:
                raw_drink_candidates = tuple(
                    drink_candidate_provider(state, drinks)
                )
            except (TypeError, ValueError, KeyError, RuntimeError) as error:
                blockers.append(
                    f"drink-candidate-provider-failed:{type(error).__name__}:{error}"
                )
                raw_drink_candidates = ()
            expected = {
                (slot.slot_index, slot.instance_id, slot.drink_id): slot
                for slot in drinks
            }
            for index, candidate in enumerate(raw_drink_candidates):
                try:
                    value = (
                        candidate
                        if isinstance(candidate, Plan1NativeLegalCandidate)
                        else Plan1NativeLegalCandidate.from_value(candidate)
                    )
                    if value.kind != "drink":
                        raise ValueError("provider returned a non-drink candidate")
                    key = (
                        value.drink_slot_index,
                        value.instance_id,
                        value.drink_id,
                    )
                    if key not in expected:
                        raise ValueError("drink candidate is outside inventory")
                    candidates.append(value)
                except (TypeError, ValueError) as error:
                    blockers.append(
                        f"drink-candidate[{index}]-invalid:{type(error).__name__}:{error}"
                    )
            if not raw_drink_candidates:
                blockers.append("drink-candidate-set-empty")

    if include_end_turn:
        if end_turn_available is None:
            blockers.append("end-turn-availability-unproven")
        elif end_turn_available:
            candidates.append(Plan1NativeLegalCandidate.end_turn())

    # Canonical root candidates must not contain duplicate action identities.
    action_ids = tuple(value.action_id for value in candidates)
    if len(action_ids) != len(set(action_ids)):
        blockers.append("duplicate-root-action-id")

    unique_blockers = tuple(dict.fromkeys(blockers))
    unique_candidates: dict[str, Plan1NativeLegalCandidate] = {}
    for candidate in candidates:
        unique_candidates.setdefault(candidate.action_id, candidate)
    ordered_candidates = tuple(unique_candidates[key] for key in sorted(unique_candidates))
    complete = bool(ordered_candidates) and not unique_blockers
    return Plan1NativeLegalCandidateEnumeration(
        candidates=ordered_candidates,
        authority=authority,
        complete=complete,
        blockers=unique_blockers,
    )


def adapt_plan1_candidate_enumeration(
    enumeration: Plan1NativeLegalCandidateEnumeration,
) -> NiaNativeCandidateSet:
    if not isinstance(enumeration, Plan1NativeLegalCandidateEnumeration):
        raise TypeError("enumeration must be Plan1NativeLegalCandidateEnumeration")
    return enumeration.native_candidate_set


def build_plan1_native_step_record(
    *,
    source_id: str,
    step: int,
    state_before: Mapping[str, object],
    enumeration: Plan1NativeLegalCandidateEnumeration,
    chosen: Plan1NativeLegalCandidate,
    submission_proof: Mapping[str, object],
    state_after: Mapping[str, object] | None = None,
    reward: int | float | None = None,
    terminal: bool = False,
    metadata: Mapping[str, object] | None = None,
) -> NiaNativeStepRecord:
    """Build one contextual or observed Plan1 sidecar step.

    This function does not project runtime state or predict an after-state.
    ``NiaNativeStepRecord`` remains the authority for JSON shape, complete
    candidate containment, and the contextual-versus-full-transition gate.
    """

    if not isinstance(enumeration, Plan1NativeLegalCandidateEnumeration):
        raise TypeError("enumeration must be Plan1NativeLegalCandidateEnumeration")
    if not enumeration.complete:
        raise ValueError("incomplete Plan1 enumeration cannot produce a step record")
    if not isinstance(chosen, Plan1NativeLegalCandidate):
        raise TypeError("chosen must be Plan1NativeLegalCandidate")
    if not any(value.action_id == chosen.action_id for value in enumeration.candidates):
        raise ValueError("chosen Plan1 candidate is outside the complete enumeration")
    return NiaNativeStepRecord(
        source_id=source_id,
        step=step,
        state_before=state_before,
        legal=adapt_plan1_candidate_enumeration(enumeration),
        action=chosen.to_native_action(),
        submission_proof=submission_proof,
        state_after=state_after,
        reward=reward,
        terminal=terminal,
        metadata={} if metadata is None else metadata,
    )


__all__ = [
    "PLAN1_NATIVE_ENUMERATOR_AUTHORITY",
    "PLAN1_NATIVE_ENUMERATION_SCHEMA",
    "DrinkCandidateProvider",
    "Plan1NativeDrinkSlot",
    "Plan1NativeLegalCandidate",
    "Plan1NativeLegalCandidateEnumeration",
    "adapt_plan1_candidate_enumeration",
    "build_plan1_native_step_record",
    "enumerate_plan1_native_legal_candidates",
]
