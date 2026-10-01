"""Plan3 adapters for the plan-neutral N.I.A. learning contract."""

from __future__ import annotations

from collections.abc import Mapping

from .nia_native_action_contract import (
    NiaNativeAction,
    NiaNativeCandidateSet,
    NiaNativeStepRecord,
)
from .plan3_native_search import (
    Plan3NativeLegalCandidate,
    Plan3NativeLegalCandidateEnumeration,
)


def plan3_candidate_action(
    candidate: Plan3NativeLegalCandidate,
) -> NiaNativeAction:
    if not isinstance(candidate, Plan3NativeLegalCandidate):
        raise TypeError("candidate must be Plan3NativeLegalCandidate")
    if candidate.kind == "play":
        assert candidate.card_guid is not None
        return NiaNativeAction.play(
            candidate.card_guid,
            card_id=candidate.card_id or "",
        )
    if candidate.kind == "drink":
        assert candidate.drink_slot_index is not None
        assert candidate.drink_id is not None
        return NiaNativeAction.drink(
            candidate.drink_slot_index,
            candidate.drink_id,
            selected_card_guid=candidate.selected_card_guid or "",
        )
    return NiaNativeAction.end_turn()


def adapt_plan3_candidate_enumeration(
    enumeration: Plan3NativeLegalCandidateEnumeration,
) -> NiaNativeCandidateSet:
    if not isinstance(enumeration, Plan3NativeLegalCandidateEnumeration):
        raise TypeError(
            "enumeration must be Plan3NativeLegalCandidateEnumeration"
        )
    return NiaNativeCandidateSet(
        tuple(plan3_candidate_action(value) for value in enumeration.candidates),
        authority=enumeration.authority,
        complete=enumeration.complete,
        blockers=enumeration.blockers,
    )


def build_plan3_native_step_record(
    *,
    source_id: str,
    step: int,
    state_before: Mapping[str, object],
    enumeration: Plan3NativeLegalCandidateEnumeration,
    chosen: Plan3NativeLegalCandidate,
    submission_proof: Mapping[str, object],
    state_after: Mapping[str, object] | None = None,
    reward: int | float | None = None,
    terminal: bool = False,
    metadata: Mapping[str, object] | None = None,
) -> NiaNativeStepRecord:
    return NiaNativeStepRecord(
        source_id=source_id,
        step=step,
        state_before=state_before,
        legal=adapt_plan3_candidate_enumeration(enumeration),
        action=plan3_candidate_action(chosen),
        submission_proof=submission_proof,
        state_after=state_after,
        reward=reward,
        terminal=terminal,
        metadata={} if metadata is None else metadata,
    )


__all__ = [
    "adapt_plan3_candidate_enumeration",
    "build_plan3_native_step_record",
    "plan3_candidate_action",
]
