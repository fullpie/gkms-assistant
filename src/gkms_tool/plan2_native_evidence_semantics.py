"""Typed LocalSave semantic proofs shared by Plan2 replay and input gates.

This module deliberately contains no screen/OCR or Master-effect policy.  The
card-history compiler first proves the retained command queue against Master,
then asks this module to bind that proven queue to one exact submitted drink
action and its ordered LocalSave inventory transition.  Downstream executors
consume the resulting immutable proof instead of independently guessing
progress from a partial collection of scalar fields.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Final, Mapping, Sequence

from .audition_local_save_state import AuditionLocalSaveStateEvidence
from .plan2_native_horizon import Plan2NativeDrinkAction


PLAN2_NATIVE_DRINK_COMMIT_PROOF_SCHEMA_VERSION: Final = 1
DRINK_COMMIT_RETAINED_PRE_EFFECT: Final = "retained-pre-effect"
DRINK_COMMIT_POST_EFFECT_REMOVE_AT: Final = "post-effect-remove-at"
_DRINK_COMMIT_MODES: Final = {
    DRINK_COMMIT_RETAINED_PRE_EFFECT,
    DRINK_COMMIT_POST_EFFECT_REMOVE_AT,
}


def _ordered_drink_ids(
    evidence: AuditionLocalSaveStateEvidence,
) -> tuple[str, ...]:
    runtime = evidence.state.root_runtime
    opaque = None if runtime is None else runtime.opaque_fields.to_value()
    rows = opaque.get("drinkList") if isinstance(opaque, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError("Plan2 drink proof has no ordered drinkList")
    result: list[str] = []
    for index, row in enumerate(rows):
        drink_id = row.get("_id") if isinstance(row, Mapping) else None
        if not isinstance(drink_id, str) or not drink_id:
            raise ValueError(
                f"Plan2 drink proof drinkList[{index}] has no exact identity"
            )
        result.append(drink_id)
    return tuple(result)


def _retained_command_semantics(
    evidence: AuditionLocalSaveStateEvidence,
    *,
    drink_id: str,
) -> tuple[str, tuple[str, ...]]:
    runtime = evidence.state.root_runtime
    rows = None if runtime is None else runtime.command_list.to_value()
    if not isinstance(rows, list) or len(rows) < 4:
        raise ValueError("Plan2 drink proof has no retained command queue")
    if any(not isinstance(row, Mapping) for row in rows):
        raise ValueError("Plan2 drink proof command queue is malformed")
    nonempty_playing_ids = tuple(
        playing_id
        for row in rows
        for playing in (row.get("_playingDrink"),)
        for playing_id in (
            playing.get("_id") if isinstance(playing, Mapping) else None,
        )
        if isinstance(playing_id, str) and playing_id
    )
    if not nonempty_playing_ids or set(nonempty_playing_ids) != {drink_id}:
        raise ValueError("Plan2 drink proof command queue has a different drink")
    effect_ids = tuple(
        effect_id
        for row in rows
        if row.get("_playType") == 5
        for effect in (row.get("_playEffect"),)
        for effect_id in (
            effect.get("_id") if isinstance(effect, Mapping) else None,
        )
        if isinstance(effect_id, str) and effect_id
    )
    if not effect_ids:
        raise ValueError("Plan2 drink proof command queue has no effects")
    canonical = json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), effect_ids


def _same_exam_action_boundary(
    before: AuditionLocalSaveStateEvidence,
    transition: AuditionLocalSaveStateEvidence,
) -> bool:
    old = before.state
    new = transition.state
    return bool(
        before.run_id == transition.run_id
        and before.step_context_id == transition.step_context_id
        and before.source_path == transition.source_path
        and before.source_type == transition.source_type
        and old.character_id == new.character_id
        and old.setting_id == new.setting_id
        and old.exam_type == new.exam_type
        and old.step_type_value == new.step_type_value
        and old.phase == new.phase
        and old.current_turn == new.current_turn
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeDrinkCommitProof:
    """Exact evidence proof for one non-provisional DRINK transaction."""

    schema_version: int
    mode: str
    action: Plan2NativeDrinkAction
    persisted_before_digest: str
    persisted_transition_digest: str
    before_inventory: tuple[str, ...]
    transition_inventory: tuple[str, ...]
    retained_command_digest: str
    command_effect_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != PLAN2_NATIVE_DRINK_COMMIT_PROOF_SCHEMA_VERSION:
            raise ValueError("unsupported Plan2 drink commit proof schema")
        if self.mode not in _DRINK_COMMIT_MODES:
            raise ValueError("unsupported Plan2 drink commit proof mode")
        if not isinstance(self.action, Plan2NativeDrinkAction):
            raise TypeError("Plan2 drink commit proof action must be typed")
        for name in (
            "persisted_before_digest",
            "persisted_transition_digest",
            "retained_command_digest",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"Plan2 drink commit proof {name} is invalid")
        before = tuple(self.before_inventory)
        transition = tuple(self.transition_inventory)
        effects = tuple(self.command_effect_ids)
        if any(not isinstance(value, str) or not value for value in (*before, *transition)):
            raise ValueError("Plan2 drink commit proof inventory is invalid")
        if not effects or any(
            not isinstance(value, str) or not value for value in effects
        ):
            raise ValueError("Plan2 drink commit proof effects are invalid")
        object.__setattr__(self, "before_inventory", before)
        object.__setattr__(self, "transition_inventory", transition)
        object.__setattr__(self, "command_effect_ids", effects)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "action_id": self.action.action_id,
            "persisted_before_digest": self.persisted_before_digest,
            "persisted_transition_digest": self.persisted_transition_digest,
            "before_inventory": list(self.before_inventory),
            "transition_inventory": list(self.transition_inventory),
            "retained_command_digest": self.retained_command_digest,
            "command_effect_ids": list(self.command_effect_ids),
        }


def prove_plan2_native_drink_commit(
    persisted_before: AuditionLocalSaveStateEvidence,
    persisted_transition: AuditionLocalSaveStateEvidence,
    action: Plan2NativeDrinkAction,
    *,
    command_effect_ids: Sequence[str],
    exact_master_command_queue_proven: bool,
) -> Plan2NativeDrinkCommitProof:
    """Build the sole completed-DRINK proof after exact Master queue review."""

    if not isinstance(persisted_before, AuditionLocalSaveStateEvidence):
        raise TypeError("persisted_before must be typed evidence")
    if not isinstance(persisted_transition, AuditionLocalSaveStateEvidence):
        raise TypeError("persisted_transition must be typed evidence")
    if not isinstance(action, Plan2NativeDrinkAction):
        raise TypeError("action must be a typed drink action")
    if exact_master_command_queue_proven is not True:
        raise ValueError("Plan2 drink commit requires exact Master command proof")
    if not _same_exam_action_boundary(persisted_before, persisted_transition):
        raise ValueError("Plan2 drink commit evidence identity changed")
    before_inventory = _ordered_drink_ids(persisted_before)
    transition_inventory = _ordered_drink_ids(persisted_transition)
    if (
        not 0 <= action.slot_index < len(before_inventory)
        or before_inventory[action.slot_index] != action.drink_id
    ):
        raise ValueError("Plan2 drink commit action does not bind before slot")
    expected_removed = (
        before_inventory[: action.slot_index]
        + before_inventory[action.slot_index + 1 :]
    )
    if transition_inventory == before_inventory:
        mode = DRINK_COMMIT_RETAINED_PRE_EFFECT
    elif transition_inventory == expected_removed:
        mode = DRINK_COMMIT_POST_EFFECT_REMOVE_AT
    else:
        raise ValueError("Plan2 drink commit inventory is neither retained nor RemoveAt")
    command_digest, observed_effect_ids = _retained_command_semantics(
        persisted_transition,
        drink_id=action.drink_id,
    )
    expected_effect_ids = tuple(command_effect_ids)
    if observed_effect_ids != expected_effect_ids:
        raise ValueError("Plan2 drink commit command effects changed")
    return Plan2NativeDrinkCommitProof(
        PLAN2_NATIVE_DRINK_COMMIT_PROOF_SCHEMA_VERSION,
        mode,
        action,
        persisted_before.digest(),
        persisted_transition.digest(),
        before_inventory,
        transition_inventory,
        command_digest,
        expected_effect_ids,
    )


def validate_plan2_native_drink_commit(
    proof: Plan2NativeDrinkCommitProof,
    persisted_before: AuditionLocalSaveStateEvidence,
    persisted_transition: AuditionLocalSaveStateEvidence,
    action: Plan2NativeDrinkAction,
) -> Plan2NativeDrinkCommitProof:
    """Revalidate a typed proof at an executor/restart consumption boundary."""

    if not isinstance(proof, Plan2NativeDrinkCommitProof):
        raise TypeError("completed DRINK has no typed semantic proof")
    if action != proof.action:
        raise ValueError("completed DRINK proof action mismatch")
    if not _same_exam_action_boundary(persisted_before, persisted_transition):
        raise ValueError("completed DRINK proof evidence identity changed")
    if persisted_before.digest() != proof.persisted_before_digest:
        raise ValueError("completed DRINK proof before evidence mismatch")
    if persisted_transition.digest() != proof.persisted_transition_digest:
        raise ValueError("completed DRINK proof transition evidence mismatch")
    before_inventory = _ordered_drink_ids(persisted_before)
    transition_inventory = _ordered_drink_ids(persisted_transition)
    if before_inventory != proof.before_inventory:
        raise ValueError("completed DRINK proof before inventory mismatch")
    if transition_inventory != proof.transition_inventory:
        raise ValueError("completed DRINK proof transition inventory mismatch")
    if (
        not 0 <= action.slot_index < len(before_inventory)
        or before_inventory[action.slot_index] != action.drink_id
    ):
        raise ValueError("completed DRINK proof slot/ID mismatch")
    expected_removed = (
        before_inventory[: action.slot_index]
        + before_inventory[action.slot_index + 1 :]
    )
    expected_transition = (
        before_inventory
        if proof.mode == DRINK_COMMIT_RETAINED_PRE_EFFECT
        else expected_removed
    )
    if transition_inventory != expected_transition:
        raise ValueError("completed DRINK proof mode/inventory mismatch")
    command_digest, effect_ids = _retained_command_semantics(
        persisted_transition,
        drink_id=action.drink_id,
    )
    if command_digest != proof.retained_command_digest:
        raise ValueError("completed DRINK proof command queue mismatch")
    if effect_ids != proof.command_effect_ids:
        raise ValueError("completed DRINK proof command effect mismatch")
    return proof


__all__ = [
    "DRINK_COMMIT_POST_EFFECT_REMOVE_AT",
    "DRINK_COMMIT_RETAINED_PRE_EFFECT",
    "PLAN2_NATIVE_DRINK_COMMIT_PROOF_SCHEMA_VERSION",
    "Plan2NativeDrinkCommitProof",
    "prove_plan2_native_drink_commit",
    "validate_plan2_native_drink_commit",
]
