"""Strict replay of a persisted, completed Plan 3 card command queue.

The game can expose the next same-turn Hand while its LocalSave still keeps
the just-played card in ``playingCard`` and the complete restore command
queue in ``commandList``.  At that boundary the root scalar fields are the
post-cost, pre-effect snapshot; treating them as the new decision state is
wrong, but waiting for an empty command list can also wait forever.

This module accepts that boundary only when a one-card simulation from the
previous settled LocalSave matches the serialized Master effects, selected
GUID, paid scalar snapshot, untouched native zones, destination, and native
remaining-play count.  Anything else returns ``None``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from .master_db import DEFAULT_DATABASE
from .plan3_engine import (
    COST_STAMINA,
    EFFECT_BLOCK,
    EFFECT_BLOCK_FIX,
    EFFECT_CONCENTRATION,
    EFFECT_ENTHUSIASTIC_ADDITIVE,
    EFFECT_ENTHUSIASTIC_MULTIPLE,
    EFFECT_MULTIPLE_ENTHUSIASTIC_LESSON,
    EFFECT_STATUS_ENCHANT_ENCORE,
    EXACT_ENCORE_CARD_ID,
    EXACT_ENCORE_STATUS_ID,
    EFFECT_FULL_POWER_POINT,
    EFFECT_FULL_POWER_POINT_REDUCE,
    EFFECT_LESSON,
    EFFECT_LESSON_FULL_POWER_POINT,
    EFFECT_PLAYABLE_VALUE_ADD,
    EFFECT_PRESERVATION,
    EFFECT_STAMINA_CONSUMPTION_DOWN,
    EFFECT_STAMINA_RECOVER_FIX,
    EFFECT_STAMINA_REDUCE_FIX,
    Plan3Effect,
    Plan3State,
    Plan3Transition,
    load_plan3_exam_settings,
)
from .plan3_local_save_bridge import (
    DecodedPlan3LocalSave,
    project_plan3_local_save,
)
from .plan3_native_search import (
    Plan3NativeAcceptedPlayExtension,
    Plan3NativeSearchPath,
    Plan3NativeSearchResult,
    Plan3NativeSearchStep,
    Plan3NativeTurnStartExtension,
    search_plan3_native,
)
from .plan3_native_search_bridge import (
    DEFAULT_SUPPORT_CARD_MASTER,
    Plan3NativeSearchBridgeResult,
    search_plan3_native_decoded_local_save,
)
from .plan3_native_state import Plan3NativeCard, Plan3NativeState


_COMMAND_EFFECT_TYPE_BY_MASTER = {
    EFFECT_LESSON: 1,
    EFFECT_BLOCK: 3,
    EFFECT_STAMINA_CONSUMPTION_DOWN: 5,
    EFFECT_STAMINA_REDUCE_FIX: 7,
    EFFECT_PLAYABLE_VALUE_ADD: 14,
    EFFECT_STAMINA_RECOVER_FIX: 28,
    EFFECT_CONCENTRATION: 45,
    EFFECT_PRESERVATION: 46,
    EFFECT_FULL_POWER_POINT: 49,
    EFFECT_FULL_POWER_POINT_REDUCE: 51,
    EFFECT_LESSON_FULL_POWER_POINT: 56,
    EFFECT_BLOCK_FIX: 119,
    EFFECT_ENTHUSIASTIC_ADDITIVE: 170,
    EFFECT_ENTHUSIASTIC_MULTIPLE: 171,
    EFFECT_MULTIPLE_ENTHUSIASTIC_LESSON: 183,
    EFFECT_STATUS_ENCHANT_ENCORE: 219,
}


@dataclass(frozen=True, slots=True)
class Plan3CompletedCardReplay:
    """A statically proven same-turn card queue and its logical after-state."""

    card_guid: str
    card_id: str
    effect_ids: tuple[str, ...]
    remaining_plays: int
    after: Plan3State
    native_after: Plan3NativeState
    transition: Plan3Transition
    step: Plan3NativeSearchStep
    prepared_before: Plan3NativeSearchBridgeResult
    persisted_before: DecodedPlan3LocalSave
    persisted_after: DecodedPlan3LocalSave
    extension_state_after: object | None = None
    prior_replay: Plan3CompletedCardReplay | None = None

    def __post_init__(self) -> None:
        if not self.card_guid or not self.card_id:
            raise ValueError("card replay identity must be non-empty")
        if not self.effect_ids:
            raise ValueError("card replay must contain at least one effect")
        if self.remaining_plays < 1:
            raise ValueError("completed card replay must remain actionable")
        if self.after.plays_remaining != self.remaining_plays:
            raise ValueError("replay remaining-play count is inconsistent")
        if self.prior_replay is not None and not _same_decoded_snapshot(
            self.persisted_before,
            self.prior_replay.persisted_after,
        ):
            raise ValueError("replay chain does not join at the persisted snapshot")
        self.native_after.assert_plan3_projection(self.after)

    @property
    def chain_depth(self) -> int:
        return 1 if self.prior_replay is None else self.prior_replay.chain_depth + 1

    @property
    def chain_card_guids(self) -> tuple[str, ...]:
        prefix = (
            ()
            if self.prior_replay is None
            else self.prior_replay.chain_card_guids
        )
        return (*prefix, self.card_guid)

    @property
    def provenance(self) -> str:
        return (
            "completed-card-replay"
            if self.prior_replay is None
            else "completed-card-replay-chain"
        )

    @property
    def exam_card_play_count_delta(self) -> int:
        return (
            self.persisted_after.exam_state.exam_card_play_count
            - self.persisted_before.exam_state.exam_card_play_count
        )


@dataclass(frozen=True, slots=True)
class Plan3TerminalCardReplay:
    """A strict chained card prediction that immediately force-ends a lesson.

    Unlike :class:`Plan3CompletedCardReplay`, this boundary has no persisted
    ``after`` LocalSave: the client can remove ``ExamSaveData`` while changing
    directly to the pursuit-result screen.  The object therefore contains only
    the exact static/native prediction.  Screen evidence is attached by the
    executor and is never manufactured here.
    """

    card_guid: str
    card_id: str
    effect_ids: tuple[str, ...]
    remaining_plays: int
    card_after: Plan3State
    after: Plan3State
    native_after: Plan3NativeState
    transition: Plan3Transition
    step: Plan3NativeSearchStep
    path: Plan3NativeSearchPath
    prepared_before: Plan3NativeSearchBridgeResult
    persisted_before: DecodedPlan3LocalSave
    prior_replay: Plan3CompletedCardReplay

    def __post_init__(self) -> None:
        if not self.card_guid or not self.card_id:
            raise ValueError("terminal replay identity must be non-empty")
        if not self.effect_ids:
            raise ValueError("terminal replay must contain at least one effect")
        if self.remaining_plays != 0 or self.card_after.plays_remaining != 0:
            raise ValueError("terminal replay must consume the final play")
        if self.after.turns_remaining != 0 or not self.path.evaluation.complete:
            raise ValueError("terminal replay must end the lesson")
        if not _same_decoded_snapshot(
            self.persisted_before,
            self.prior_replay.persisted_after,
        ):
            raise ValueError("terminal replay does not join the prior snapshot")
        self.native_after.assert_plan3_projection(self.after)

    @property
    def chain_depth(self) -> int:
        return self.prior_replay.chain_depth + 1

    @property
    def chain_card_guids(self) -> tuple[str, ...]:
        return (*self.prior_replay.chain_card_guids, self.card_guid)

    @property
    def provenance(self) -> str:
        return "terminal-card-replay-chain"


def _raw_exam_save(decoded: DecodedPlan3LocalSave) -> Mapping[str, object]:
    value = json.loads(decoded.envelope.plaintext.decode("utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("decrypted ExamSaveData root must be an object")
    return value


def _serialized_effect_matches(
    serialized: object,
    effect: Plan3Effect,
) -> bool:
    if not isinstance(serialized, Mapping):
        return False
    effect_type = _COMMAND_EFFECT_TYPE_BY_MASTER.get(effect.effect_type)
    if effect.effect_type == EFFECT_STATUS_ENCHANT_ENCORE:
        return bool(
            effect.id
            == "e_effect-exam_status_enchant_encore-0001-03-inf-"
            "enchant-p_card-03-ido-3_197-enc01"
            and effect.status_enchant_id == EXACT_ENCORE_STATUS_ID
            and effect.status_enchant is not None
            and serialized.get("_id") == effect.id
            and serialized.get("_effectType") == effect_type
            and serialized.get("_effectValue1") == effect.value1
            and serialized.get("_effectValue2") == effect.value2
            and serialized.get("_effectCount") == effect.effect_count
            and serialized.get("_effectTurn") == effect.effect_turn
            and serialized.get("_chainEffectId", "") == ""
            and serialized.get("_chainEffectIdList", []) == []
            and serialized.get("_statusEnchantId") == EXACT_ENCORE_STATUS_ID
            and serialized.get("_cardGrowEffectIdList", []) == []
        )
    # Complex nested/conditional effects need their own command-shape proof.
    # The simple numeric effects below have no information omitted by the
    # scalar kernel and can therefore be compared field-for-field.
    if (
        effect_type is None
        or effect.trigger is not None
        or effect.status_enchant_id
        or effect.status_enchant is not None
        or effect.chain_effect_id
        or effect.card_move_rule is not None
    ):
        return False
    return bool(
        serialized.get("_id") == effect.id
        and serialized.get("_effectType") == effect_type
        and serialized.get("_effectValue1") == effect.value1
        and serialized.get("_effectValue2") == effect.value2
        and serialized.get("_effectCount") == effect.effect_count
        and serialized.get("_effectTurn") == effect.effect_turn
        and serialized.get("_chainEffectId", "") == ""
        and serialized.get("_chainEffectIdList", []) == []
        and serialized.get("_statusEnchantId", "") == ""
        and serialized.get("_cardGrowEffectIdList", []) == []
    )


def _native_cards(values: object) -> tuple[Plan3NativeCard, ...] | None:
    if not isinstance(values, tuple):
        return None
    try:
        return tuple(Plan3NativeCard.from_local_save(card) for card in values)
    except (TypeError, ValueError, OSError):
        return None


def _same_native_card_identity_and_play_count(
    left: Plan3NativeCard,
    right: Plan3NativeCard,
) -> bool:
    """Compare persisted identity fields that a prior queued effect cannot grow."""

    return bool(
        left.guid == right.guid
        and left.card_id == right.card_id
        and left.base_upgrade == right.base_upgrade
        and left.temporary_upgrade == right.temporary_upgrade
        and left.effective_upgrade == right.effective_upgrade
        and left.support_upgrade_ids == right.support_upgrade_ids
        and left.fixed_deck_order == right.fixed_deck_order
        and left.play_count == right.play_count
    )


def _settle_prior_playing_card_lifecycle(
    prior_replay: Plan3CompletedCardReplay,
) -> Plan3NativeState:
    """Apply the destination-side play-count increment seen on queue settle."""

    zone_name = {
        "discard": "grave",
        "lost": "lost",
    }.get(prior_replay.transition.moved_to)
    if zone_name is None:
        raise ValueError("prior replay has no supported settled destination")
    native = prior_replay.native_after
    zone = getattr(native, zone_name)
    matches = tuple(
        index
        for index, card in enumerate(zone)
        if card.guid == prior_replay.card_guid
    )
    if len(matches) != 1:
        raise ValueError("prior replay card is not unique in its destination")
    index = matches[0]
    settled_zone = tuple(
        card.increment_play_count() if offset == index else card
        for offset, card in enumerate(zone)
    )
    settled = replace(native, **{zone_name: settled_zone})
    settled.assert_plan3_projection(prior_replay.after)
    return settled


def settle_completed_plan3_card_replay_native_state(
    replay: Plan3CompletedCardReplay,
) -> Plan3NativeState:
    """Return the exact next-decision native zones for a proven replay."""

    if not isinstance(replay, Plan3CompletedCardReplay):
        raise TypeError("replay must be Plan3CompletedCardReplay")
    return _settle_prior_playing_card_lifecycle(replay)


def _same_decoded_snapshot(
    left: DecodedPlan3LocalSave,
    right: DecodedPlan3LocalSave,
) -> bool:
    """Require the exact serialized snapshot, not a similar projection."""

    return bool(
        left.envelope.save_data_version == right.envelope.save_data_version
        and left.envelope.encrypted_body_size
        == right.envelope.encrypted_body_size
        and left.envelope.plaintext == right.envelope.plaintext
    )


def _same_exam_boundary(
    before: DecodedPlan3LocalSave,
    after: DecodedPlan3LocalSave,
    *,
    completed_prior_card_count: int = 0,
) -> bool:
    if (
        not isinstance(completed_prior_card_count, int)
        or isinstance(completed_prior_card_count, bool)
        or completed_prior_card_count < 0
    ):
        raise ValueError("completed_prior_card_count must be non-negative")
    old = before.exam_state
    new = after.exam_state
    return bool(
        old.character_id == new.character_id
        and old.setting_id == new.setting_id
        and old.exam_type == new.exam_type
        and old.step_type_value == new.step_type_value
        and old.current_turn == new.current_turn
        and old.limit_turn == new.limit_turn
        and old.remain_turn == new.remain_turn
        and old.extra_turn == new.extra_turn
        and old.max_stamina == new.max_stamina
        and old.random_state == new.random_state
        and old.turn_use_support_ids == new.turn_use_support_ids
        and old.turn_card_play_count + completed_prior_card_count
        == new.turn_card_play_count
        and old.exam_card_play_count + completed_prior_card_count
        == new.exam_card_play_count
    )


def _matching_card_step(
    search: Plan3NativeSearchResult,
    card_guid: str,
) -> tuple[Plan3NativeSearchStep, Plan3NativeSearchPath] | None:
    matches: list[tuple[Plan3NativeSearchStep, Plan3NativeSearchPath]] = []
    for candidate in search.candidates:
        card_steps = tuple(step for step in candidate.steps if step.kind == "card")
        if (
            len(card_steps) == 1
            and card_steps[0].action is not None
            and card_steps[0].action.guid == card_guid
            and len(candidate.decision_steps) == 1
        ):
            matches.append((card_steps[0], candidate))
    return matches[0] if len(matches) == 1 else None


def _same_transitional_native_card(
    observed: Plan3NativeCard,
    expected: Plan3NativeCard,
    *,
    allow_deferred_phase_counts: bool,
) -> bool:
    """Compare one queued-save card without weakening its stable identity.

    A same-turn chained command queue can serialize a card listener before
    its ``CardPlayAfter`` phase-count dictionary is flushed back to the card
    object.  The logical replay already contains that increment.  Archived
    captures prove that every other runtime field is current at this boundary.
    Accept only that one empty-to-nonempty dictionary lag, and only for the two
    listener kinds which own card-play phase counters.
    """

    if observed == expected:
        return True
    if not allow_deferred_phase_counts:
        return False
    observed_status = observed.runtime_grow_status
    expected_status = expected.runtime_grow_status
    if (
        observed_status is None
        or expected_status is None
        or observed_status.trigger_kind
        not in {
            "card_play_after_self_full_power",
            "card_play_after_effect_group",
        }
        or observed_status.phase_counts
        or not expected_status.phase_counts
        or replace(
            observed_status,
            phase_counts=expected_status.phase_counts,
        )
        != expected_status
    ):
        return False
    return replace(observed, runtime_grow_status=expected_status) == expected


def _same_transitional_native_zone(
    observed: tuple[Plan3NativeCard, ...],
    expected: tuple[Plan3NativeCard, ...],
    *,
    allow_deferred_phase_counts: bool,
) -> bool:
    return len(observed) == len(expected) and all(
        _same_transitional_native_card(
            observed_card,
            expected_card,
            allow_deferred_phase_counts=allow_deferred_phase_counts,
        )
        for observed_card, expected_card in zip(observed, expected, strict=True)
    )


def _prove_completed_card_transition(
    persisted_before: DecodedPlan3LocalSave,
    persisted_after: DecodedPlan3LocalSave,
    card_guid: str,
    *,
    logical_before: Plan3State,
    native_before: Plan3NativeState,
    prepared_before: Plan3NativeSearchBridgeResult,
    search: Plan3NativeSearchResult,
    completed_prior_card_count: int,
    prior_replay: Plan3CompletedCardReplay | None,
    database: Path,
) -> Plan3CompletedCardReplay | None:
    """Validate one serialized command queue against one logical state."""

    old = persisted_before.exam_state
    new = persisted_after.exam_state
    runtime = new.root_runtime
    prior_destination = (
        None if prior_replay is None else prior_replay.transition.moved_to
    )
    expected_turn_grave = prior_destination == "discard"
    expected_turn_lost = prior_destination == "lost"
    if (
        prior_destination not in {None, "discard", "lost"}
        or not _same_exam_boundary(
            persisted_before,
            persisted_after,
            completed_prior_card_count=completed_prior_card_count,
        )
        or old.exam_type not in {0, 1}
        or new.phase != 6
        or new.is_turn_card_play_end
        or new.playing_card is None
        or new.playing_card.guid != card_guid
        or new.removed_cards
        or runtime is None
        or runtime.command_list_is_empty
        or runtime.draw_card_guid_list
        or runtime.is_turn_card_grave is not expected_turn_grave
        or runtime.is_turn_card_lost is not expected_turn_lost
        or runtime.is_exam_end_complete
    ):
        return None

    native_matches = tuple(
        card for card in native_before.hand if card.guid == card_guid
    )
    persisted_matches = tuple(
        card for card in old.zones.hand if card.guid == card_guid
    )
    if len(native_matches) != 1 or len(persisted_matches) != 1:
        return None
    selected_native_before = native_matches[0]
    selected_persisted_before = Plan3NativeCard.from_local_save(
        persisted_matches[0]
    )
    selected_after = new.playing_card
    if (
        not _same_native_card_identity_and_play_count(
            selected_persisted_before,
            selected_native_before,
        )
        or selected_after.card_id != selected_native_before.card_id
        or selected_after.effective_upgrade
        != selected_native_before.effective_upgrade
        or any(card.guid == card_guid for card in new.zones.hand)
    ):
        return None

    raw = _raw_exam_save(persisted_after)
    raw_commands = raw.get("commandList")
    raw_playing = raw.get("playingCard")
    if (
        not isinstance(raw_commands, list)
        or not all(isinstance(command, Mapping) for command in raw_commands)
        or not isinstance(raw_playing, Mapping)
    ):
        return None
    commands = tuple(raw_commands)

    match = _matching_card_step(search, card_guid)
    if match is None:
        return None
    step, candidate = match
    transition = step.card_transition
    if (
        step.before != logical_before
        or step.native_before != native_before
        or transition is None
        or not transition.supported
        or not transition.legal
        or transition.unverified_rules
        or not (
            transition.card.cost_type == COST_STAMINA
            or transition.card.id == EXACT_ENCORE_CARD_ID
        )
        or transition.moved_to not in {"discard", "lost"}
        or candidate.state.plays_remaining < 1
        or not candidate.native_state.hand
    ):
        return None

    effects = transition.card.effects
    if not effects or len(commands) != len(effects) + 4:
        return None
    start = commands[0]
    effect_commands = commands[1 : 1 + len(effects)]
    after_effect = commands[-3]
    move = commands[-2]
    terminal = commands[-1]
    if (
        start.get("_playType") != 9
        or start.get("_isSeparateStart") is not True
        or after_effect.get("_playType") != 13
        or move.get("_playType") != 6
        or move.get("_playCardPositionType") != 2
        or move.get("_isUsePlayableCardCount") is not True
        or terminal.get("_playType") != 9
        or terminal.get("_isSeparateStart") is not False
        or any(command.get("_playingCard") != raw_playing for command in commands)
        or any(
            command.get("_isCardSelect") is not False
            or command.get("_isCardSelect2") is not False
            or command.get("_isPlayingMoveCardEffect") is not False
            for command in commands
        )
        or any(command.get("_playType") != 5 for command in effect_commands)
        or not all(
            _serialized_effect_matches(command.get("_playEffect"), effect)
            for command, effect in zip(effect_commands, effects, strict=True)
        )
    ):
        return None

    remaining_values = tuple(
        command.get("_remainCanPlayCardCount") for command in commands
    )
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in remaining_values
    ):
        return None
    # These values are captured before the move consumes the current play.
    if max(remaining_values) != logical_before.plays_remaining:
        return None

    observed_playing = Plan3NativeCard.from_local_save(selected_after)
    if observed_playing != selected_native_before.increment_play_count():
        return None

    # The prior logical replay has already finalized the previous card's
    # destination.  The next persisted snapshot must now expose precisely that
    # zone state, with only the newly selected card removed into playingCard.
    for zone_name in ("hand", "deck", "grave", "lost", "hold"):
        observed = _native_cards(getattr(new.zones, zone_name))
        expected = tuple(
            card
            for card in getattr(native_before, zone_name)
            if card.guid != card_guid
        )
        if observed is None or not _same_transitional_native_zone(
            observed,
            expected,
            # Only a second (or later) queue can lag a phase counter from the
            # replay immediately before it.  The first queue remains exact.
            allow_deferred_phase_counts=prior_replay is not None,
        ):
            return None

    # Root scalars are post-cost and pre-effect.  They must join the logical
    # prior exactly; the queued Master effects produce candidate.state later.
    transitional = project_plan3_local_save(
        new,
        database=database,
        native_card_play_counts=True,
    ).state
    if (
        new.score != logical_before.score
        or new.stamina != logical_before.stamina - transition.stamina_paid
        or new.block != logical_before.block - transition.block_paid
        or transitional.stance != logical_before.stance
        or transitional.stance_level != logical_before.stance_level
        or transitional.enthusiasm != logical_before.enthusiasm
        or transitional.enthusiasm_additive
        != logical_before.enthusiasm_additive
        or transitional.stance_change_count != logical_before.stance_change_count
        or transitional.concentration_change_count
        != logical_before.concentration_change_count
        or transitional.preservation_change_count
        != logical_before.preservation_change_count
        or transitional.full_power_change_count
        != logical_before.full_power_change_count
        or transitional.full_power_points != logical_before.full_power_points
    ):
        return None

    return Plan3CompletedCardReplay(
        card_guid=card_guid,
        card_id=selected_native_before.card_id,
        effect_ids=tuple(effect.id for effect in effects),
        remaining_plays=candidate.state.plays_remaining,
        after=candidate.state,
        native_after=candidate.native_state,
        transition=transition,
        step=step,
        prepared_before=prepared_before,
        persisted_before=persisted_before,
        persisted_after=persisted_after,
        extension_state_after=candidate.turn_start_extension_state,
        prior_replay=prior_replay,
    )


def replay_completed_plan3_card_history(
    before: DecodedPlan3LocalSave,
    after: DecodedPlan3LocalSave,
    card_guid: str,
    *,
    database: Path = DEFAULT_DATABASE,
    support_card_master: Path = DEFAULT_SUPPORT_CARD_MASTER,
    turn_start_extension: Plan3NativeTurnStartExtension | None = None,
    accepted_play_extension: Plan3NativeAcceptedPlayExtension | None = None,
    initial_turn_start_extension_state: object | None = None,
) -> Plan3CompletedCardReplay | None:
    """Prove one complete ``9, 5..., 13, 6, 9`` card queue."""

    if not isinstance(before, DecodedPlan3LocalSave):
        raise TypeError("before must be DecodedPlan3LocalSave")
    if not isinstance(after, DecodedPlan3LocalSave):
        raise TypeError("after must be DecodedPlan3LocalSave")
    if not isinstance(card_guid, str) or not card_guid:
        raise TypeError("card_guid must be non-empty text")
    prepared = search_plan3_native_decoded_local_save(
        before,
        beam_width=max(1, len(before.exam_state.zones.hand)),
        depth=1,
        database=Path(database),
        support_card_master=Path(support_card_master),
        turn_start_extension=turn_start_extension,
        accepted_play_extension=accepted_play_extension,
        initial_turn_start_extension_state=initial_turn_start_extension_state,
    )
    if prepared.issues or prepared.search is None or prepared.native_state is None:
        return None
    return _prove_completed_card_transition(
        before,
        after,
        card_guid,
        logical_before=prepared.projection.state,
        native_before=prepared.native_state,
        prepared_before=prepared,
        search=prepared.search,
        completed_prior_card_count=0,
        prior_replay=None,
        database=Path(database),
    )


def replay_completed_plan3_card_history_from_search(
    before: DecodedPlan3LocalSave,
    after: DecodedPlan3LocalSave,
    card_guid: str,
    *,
    logical_before: Plan3State,
    native_before: Plan3NativeState,
    prepared_before: Plan3NativeSearchBridgeResult,
    search: Plan3NativeSearchResult,
    database: Path = DEFAULT_DATABASE,
) -> Plan3CompletedCardReplay | None:
    """Prove a queue from an already mode-bound native search.

    NIA auditions bind battle scheduling and path-local gimmick extensions
    before selecting a card.  Re-projecting the same LocalSave through the
    generic bridge would discard that mode authority, so callers provide the
    exact search inputs/results which produced the action.
    """

    if not isinstance(before, DecodedPlan3LocalSave):
        raise TypeError("before must be DecodedPlan3LocalSave")
    if not isinstance(after, DecodedPlan3LocalSave):
        raise TypeError("after must be DecodedPlan3LocalSave")
    if not isinstance(card_guid, str) or not card_guid:
        raise TypeError("card_guid must be non-empty text")
    if not isinstance(logical_before, Plan3State):
        raise TypeError("logical_before must be Plan3State")
    if not isinstance(native_before, Plan3NativeState):
        raise TypeError("native_before must be Plan3NativeState")
    if not isinstance(prepared_before, Plan3NativeSearchBridgeResult):
        raise TypeError("prepared_before must be Plan3NativeSearchBridgeResult")
    if not isinstance(search, Plan3NativeSearchResult):
        raise TypeError("search must be Plan3NativeSearchResult")
    return _prove_completed_card_transition(
        before,
        after,
        card_guid,
        logical_before=logical_before,
        native_before=native_before,
        prepared_before=prepared_before,
        search=search,
        completed_prior_card_count=0,
        prior_replay=None,
        database=Path(database),
    )


def replay_next_completed_plan3_card_history(
    prior_replay: Plan3CompletedCardReplay,
    before: DecodedPlan3LocalSave,
    after: DecodedPlan3LocalSave,
    card_guid: str,
    *,
    database: Path = DEFAULT_DATABASE,
) -> Plan3CompletedCardReplay | None:
    """Replay the next same-turn queue from a proven logical prior.

    ``before`` must be byte-for-byte the transition snapshot already bound to
    ``prior_replay``.  No projection of that transitional save becomes
    authority: search starts from ``prior_replay.after/native_after`` and
    reuses the original static support, search, gimmick, and setting inputs.
    """

    if not isinstance(prior_replay, Plan3CompletedCardReplay):
        raise TypeError("prior_replay must be Plan3CompletedCardReplay")
    if not isinstance(before, DecodedPlan3LocalSave):
        raise TypeError("before must be DecodedPlan3LocalSave")
    if not isinstance(after, DecodedPlan3LocalSave):
        raise TypeError("after must be DecodedPlan3LocalSave")
    if not isinstance(card_guid, str) or not card_guid:
        raise TypeError("card_guid must be non-empty text")
    if not _same_decoded_snapshot(before, prior_replay.persisted_after):
        return None
    prepared = prior_replay.prepared_before
    settings = load_plan3_exam_settings(
        prepared.decoded.exam_state.setting_id,
    )
    native_before = _settle_prior_playing_card_lifecycle(prior_replay)
    search = search_plan3_native(
        prior_replay.after,
        native_before,
        beam_width=max(1, len(native_before.hand)),
        depth=1,
        settings=settings,
        gimmick_profile=(
            None
            if prepared.turn_start_extension is not None
            else prepared.gimmick_profile
        ),
        database=Path(database),
        support_upgrades=prepared.support_upgrades,
        support_card_searches=dict(prepared.support_card_searches),
        turn_start_extension=prepared.turn_start_extension,
        accepted_play_extension=prepared.accepted_play_extension,
        initial_turn_start_extension_state=prior_replay.extension_state_after,
    )
    if search.diagnostics:
        return None
    return _prove_completed_card_transition(
        before,
        after,
        card_guid,
        logical_before=prior_replay.after,
        native_before=native_before,
        prepared_before=prepared,
        search=search,
        completed_prior_card_count=1,
        prior_replay=prior_replay,
        database=Path(database),
    )


def predict_terminal_next_plan3_card_history(
    prior_replay: Plan3CompletedCardReplay,
    before: DecodedPlan3LocalSave,
    card_guid: str,
    *,
    database: Path = DEFAULT_DATABASE,
) -> Plan3TerminalCardReplay | None:
    """Prove that one exact next card consumes the last play and force-ends.

    This is intentionally narrower than ordinary replay.  It is available only
    for a chained lesson snapshot, requires an exact snapshot join, expands
    every current Hand card at depth one, and accepts only a diagnostic-free
    path containing the selected card followed by the native lesson
    ``force_end`` transition.  A caller must still provide independent result
    screen evidence before treating the missing LocalSave as committed.
    """

    if not isinstance(prior_replay, Plan3CompletedCardReplay):
        raise TypeError("prior_replay must be Plan3CompletedCardReplay")
    if not isinstance(before, DecodedPlan3LocalSave):
        raise TypeError("before must be DecodedPlan3LocalSave")
    if not isinstance(card_guid, str) or not card_guid:
        raise TypeError("card_guid must be non-empty text")
    if (
        before.exam_state.exam_type != 0
        or prior_replay.after.plays_remaining != 1
        or prior_replay.after.limit_border <= 0
        or not _same_decoded_snapshot(before, prior_replay.persisted_after)
    ):
        return None

    prepared = prior_replay.prepared_before
    settings = load_plan3_exam_settings(
        prepared.decoded.exam_state.setting_id,
    )
    native_before = _settle_prior_playing_card_lifecycle(prior_replay)
    search = search_plan3_native(
        prior_replay.after,
        native_before,
        beam_width=max(1, len(native_before.hand)),
        depth=1,
        settings=settings,
        gimmick_profile=(
            None
            if prepared.turn_start_extension is not None
            else prepared.gimmick_profile
        ),
        database=Path(database),
        support_upgrades=prepared.support_upgrades,
        support_card_searches=dict(prepared.support_card_searches),
        turn_start_extension=prepared.turn_start_extension,
        accepted_play_extension=prepared.accepted_play_extension,
        initial_turn_start_extension_state=prior_replay.extension_state_after,
        force_end_score=prior_replay.after.limit_border,
        force_end_stamina_recovery=settings.turn_end_stamina_recovery,
    )
    if search.diagnostics:
        return None
    match = _matching_card_step(search, card_guid)
    if match is None:
        return None
    step, candidate = match
    transition = step.card_transition
    force_steps = tuple(
        value for value in candidate.steps if value.kind == "force_end"
    )
    native_matches = tuple(
        card for card in native_before.hand if card.guid == card_guid
    )
    if (
        len(native_matches) != 1
        or step.action is None
        or step.action.card_ref.card_id != native_matches[0].card_id
        or step.action.card_ref.upgrade != native_matches[0].effective_upgrade
        or step.before != prior_replay.after
        or step.native_before != native_before
        or transition is None
        or not transition.supported
        or not transition.legal
        or transition.unverified_rules
        or transition.card.cost_type != COST_STAMINA
        or transition.card.id != native_matches[0].card_id
        or transition.card.upgrade != native_matches[0].effective_upgrade
        or transition.moved_to not in {"discard", "lost"}
        or step.after.plays_remaining != 0
        or candidate.state.plays_remaining != 0
        or candidate.state.turns_remaining != 0
        or not candidate.evaluation.complete
        or len(force_steps) != 1
        or force_steps[0] != candidate.steps[-1]
        or force_steps[0].forced_end_stamina_recovered < 0
    ):
        return None
    effects = transition.card.effects
    if not effects:
        return None
    return Plan3TerminalCardReplay(
        card_guid=card_guid,
        card_id=native_matches[0].card_id,
        effect_ids=tuple(effect.id for effect in effects),
        remaining_plays=0,
        card_after=step.after,
        after=candidate.state,
        native_after=candidate.native_state,
        transition=transition,
        step=step,
        path=candidate,
        prepared_before=prepared,
        persisted_before=before,
        prior_replay=prior_replay,
    )


__all__ = [
    "Plan3CompletedCardReplay",
    "Plan3TerminalCardReplay",
    "predict_terminal_next_plan3_card_history",
    "replay_completed_plan3_card_history",
    "replay_completed_plan3_card_history_from_search",
    "replay_next_completed_plan3_card_history",
    "settle_completed_plan3_card_replay_native_state",
]
