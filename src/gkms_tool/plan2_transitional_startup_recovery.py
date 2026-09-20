"""Recover the currently accepted Plan2 PLAY after a process restart.

The game can keep a completed card's post-cost/pre-effect ``commandList`` in
ExamSaveData after MAA has already submitted it.  The normal executor owns the
exact settled before-state in memory, but a restarted process does not.  This
module rebuilds that before-state only when two game-owned journals close the
gap:

* the current-turn ``ExamSavePlayLogData`` binds the played GUID and Hand slot;
* the previous ``ExamSaveTurnLogData`` binds a later turn's first-card
  stamina/block root, while turn 1 is rooted by its zero-based cumulative
  block ledger;
* an exact reversible buff-cost status binds a later retained card's root.

No card ID, turn, slot, coordinate, or support-card identity is hard-coded.
No card ID, turn, slot, coordinate, artwork, or support-marker colour is
hard-coded.  Later stamina-cost cards remain fail-closed when the retained
save does not uniquely prove how the payment split between stamina and block.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from typing import Mapping

from .audition_local_save_state import (
    AuditionLocalSaveStateEvidence,
    CanonicalJsonValue,
    LocalSaveExamZones,
)
from .local_save_decoder import decode_local_save_json, obfuscated_name
from .master_db import DEFAULT_DATABASE
from .plan2_card_history import (
    Plan2CardHistoryObservation,
    Plan2CompletedCardReplay,
    replay_completed_plan2_card_history,
)
from .plan2_native_catalog_stamina import (
    COST_GOOD_IMPRESSION,
    COST_MOTIVATION,
    COST_STAMINA,
)
from .plan2_native_horizon import Plan2NativeAction
from .plan2_native_local_save_bootstrap import (
    bootstrap_plan2_native_horizon_from_evidence,
    load_plan2_native_exam_setting_authority,
)
from .plan2_native_program_catalog import compile_plan2_native_program_catalog


PLAY_LOG_TYPE = "Campus.InGame.Exam.ExamSavePlayLogData"
TURN_LOG_TYPE = "Campus.InGame.Exam.ExamSaveTurnLogData"


class Plan2TransitionalStartupRecoveryError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


def _error(code: str, detail: str = "") -> None:
    raise Plan2TransitionalStartupRecoveryError(code, detail)


def _int(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _error("startup-recovery-journal-field-invalid", label)
    return value


def _load_play_index(directory: Path, evidence: AuditionLocalSaveStateEvidence) -> int:
    state = evidence.state
    playing = state.playing_card
    assert playing is not None
    path = directory / obfuscated_name(PLAY_LOG_TYPE, str(state.current_turn))
    payload = decode_local_save_json(path, PLAY_LOG_TYPE)
    rows = payload.get("playLogList") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        _error("startup-recovery-play-log-invalid")
    matches: list[int] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        command = row.get("_command")
        card = row.get("_playCard")
        card_data = card.get("_cardData") if isinstance(card, Mapping) else None
        if not isinstance(command, Mapping) or not isinstance(card_data, Mapping):
            continue
        if (
            row.get("_currentTurn") == state.current_turn
            and row.get("_phaseType") == 6
            and row.get("_isCostFailed") is False
            and command.get("_playType") == 2
            and command.get("_playCardPositionType") == 2
            and card.get("_guid") == playing.guid
            and card_data.get("_id") == playing.card_id
            and card_data.get("_upgradeCount") == playing.effective_upgrade
        ):
            matches.append(_int(command.get("_playIndex"), "_playIndex"))
    if len(matches) != 1:
        _error(
            "startup-recovery-play-log-action-not-unique",
            f"guid={playing.guid};matches={len(matches)}",
        )
    index = matches[0]
    if index > len(state.zones.hand):
        _error("startup-recovery-play-index-out-of-range", str(index))
    return index


def _load_previous_resources(directory: Path, current_turn: int) -> tuple[int, int]:
    if current_turn <= 1:
        _error("startup-recovery-previous-turn-anchor-required")
    path = directory / obfuscated_name(TURN_LOG_TYPE, str(current_turn - 1))
    payload = decode_local_save_json(path, TURN_LOG_TYPE)
    row = payload.get("turnLog") if isinstance(payload, Mapping) else None
    if not isinstance(row, Mapping) or row.get("turn") != current_turn - 1:
        _error("startup-recovery-turn-log-invalid")
    return (
        _int(row.get("stamina"), "turnLog.stamina"),
        _int(row.get("block"), "turnLog.block"),
    )


def _bootstrap(evidence: AuditionLocalSaveStateEvidence, catalog):
    state = evidence.state
    authority = load_plan2_native_exam_setting_authority(state.setting_id)
    opaque = state.root_runtime.opaque_fields.to_value()
    hooks = None
    if isinstance(opaque, Mapping):
        if state.exam_type == 0:
            from .initial_regular_plan2_lesson_gimmick_runtime import (
                build_initial_regular_plan2_lesson_gimmick_hooks,
            )

            hooks = build_initial_regular_plan2_lesson_gimmick_hooks(
                opaque.get("gimmickList")
            )
        elif state.exam_type == 1 and state.step_type_value == 18:
            from .initial_regular_plan2_gimmick_runtime import (
                build_initial_regular_plan2_final_gimmick_hooks,
            )

            hooks = build_initial_regular_plan2_final_gimmick_hooks(
                opaque.get("gimmickList")
            )
    return bootstrap_plan2_native_horizon_from_evidence(
        evidence,
        catalog=catalog,
        draw_count=authority.draw_count,
        hand_limit=authority.hand_limit,
        gimmick_hooks=hooks,
        # A restarted live run observes memory/item/gimmick outcomes from the
        # rendered HUD after the retained queue drains.  Unsupported external
        # listener grammars must not prevent reconstruction of the proven
        # card GUID/cost/zone transaction.
        observe_external_effects=True,
    )


def _active_status_references(
    opaque: Mapping[str, object],
) -> tuple[set[int], list[dict[str, object]]]:
    status = opaque.get("status")
    references = opaque.get("references")
    if not isinstance(status, Mapping) or not isinstance(references, Mapping):
        _error("startup-recovery-status-graph-invalid")
    links = status.get("_effectList")
    rows = references.get("RefIds")
    if not isinstance(links, list) or not isinstance(rows, list):
        _error("startup-recovery-status-graph-invalid")
    active: set[int] = set()
    for link in links:
        rid = link.get("rid") if isinstance(link, Mapping) else None
        if isinstance(rid, bool) or not isinstance(rid, int) or rid <= 0:
            _error("startup-recovery-status-graph-invalid")
        active.add(rid)
    typed_rows = tuple(row for row in rows if isinstance(row, dict))
    if len(typed_rows) != len(rows):
        _error("startup-recovery-status-graph-invalid")
    return active, list(typed_rows)


def _rewind_buff_cost(
    opaque: dict[str, object],
    *,
    cost_type: str,
    amount: int,
) -> None:
    """Reverse only the accepted card's already-persisted buff payment.

    The subsequent native replay recomputes the payment and compares the
    resulting post-cost state with the immutable transition.  Thus a future
    modifier that changes ``amount`` is rejected instead of silently guessed.
    """

    if amount < 0:
        _error("startup-recovery-cost-invalid", str(amount))
    if amount == 0:
        return
    active, rows = _active_status_references(opaque)
    target_class = (
        "ReviewStatusEffect"
        if cost_type == COST_GOOD_IMPRESSION
        else "AggressiveStatusEffect"
    )
    matches: list[dict[str, object]] = []
    for row in rows:
        raw_type = row.get("type")
        if (
            row.get("rid") in active
            and isinstance(raw_type, Mapping)
            and raw_type.get("class") == target_class
            and isinstance(row.get("data"), dict)
        ):
            matches.append(row["data"])
    if len(matches) != 1:
        _error(
            "startup-recovery-cost-status-not-unique",
            f"{target_class}:count={len(matches)}",
        )
    data = matches[0]
    field = "_turn" if cost_type == COST_GOOD_IMPRESSION else "_value"
    current = _int(data.get(field), f"{target_class}.{field}", minimum=1)
    data[field] = current + amount
    if cost_type == COST_GOOD_IMPRESSION:
        counter = "reviewConsumptionSumCount"
        consumed = _int(opaque.get(counter), counter)
        if consumed < amount:
            _error("startup-recovery-counter-underflow", counter)
        opaque[counter] = consumed - amount


def _rewind_current_cost(
    opaque: dict[str, object],
    *,
    cost_type: str | None,
    base_cost: int,
    first_card: bool,
    state_stamina: int,
    state_block: int,
    previous_resources: tuple[int, int] | None,
) -> tuple[int, int]:
    if cost_type in (None, ""):
        return state_stamina, state_block
    if cost_type == COST_STAMINA:
        if not first_card:
            _error(
                "startup-recovery-later-stamina-cost-ambiguous",
                "retained save does not prove the stamina/block split",
            )

        # The previous TurnLog is an end-of-turn anchor, not necessarily the
        # resource snapshot immediately before the first card of this turn.
        # Native StartTurn listeners may recover stamina or add block before
        # the card is accepted.  ExamSave owns exact same-turn accumulators for
        # both sides of that boundary, so reconstruct the pre-card resources
        # from those fields and let the native replay below re-prove the full
        # payment (including modifiers) against the persisted post-cost state.
        stamina_loss = _int(
            opaque.get("currentTurnTotalConsumeStamina"),
            "currentTurnTotalConsumeStamina",
        )
        current_turn_block_add = _int(
            opaque.get("currentTurnTotalBlock"),
            "currentTurnTotalBlock",
        )
        before_stamina = state_stamina + stamina_loss
        if previous_resources is None:
            # Turn 1 has no previous TurnLog by definition.  Its cumulative
            # block-consumption ledger contains only this turn, and this is
            # the first accepted card, so it is the exact payment already
            # persisted by the retained command queue.  StartTurn block is
            # independently preserved in currentTurnTotalBlock.
            block_loss = _int(
                opaque.get("blockConsumptionSumCount"),
                "blockConsumptionSumCount",
            )
            before_block = state_block + block_loss
            if before_block != current_turn_block_add:
                _error(
                    "startup-recovery-resource-anchor-incompatible",
                    f"turn1_block={before_block};"
                    f"current_turn_block_add={current_turn_block_add}",
                )
        else:
            _previous_stamina, previous_block = previous_resources
            before_block = previous_block + current_turn_block_add
        block_loss = before_block - state_block
        if block_loss < 0:
            _error(
                "startup-recovery-resource-anchor-incompatible",
                f"before_block={before_block};"
                f"state_block={state_block}",
            )
        for key, delta in (
            ("currentTurnTotalConsumeStamina", stamina_loss),
            ("totalConsumeStamina", stamina_loss),
            ("blockConsumptionSumCount", block_loss),
        ):
            current = _int(opaque.get(key), key)
            if current < delta:
                _error("startup-recovery-counter-underflow", key)
            opaque[key] = current - delta
        return before_stamina, before_block
    if cost_type in {COST_GOOD_IMPRESSION, COST_MOTIVATION}:
        _rewind_buff_cost(opaque, cost_type=cost_type, amount=base_cost)
        return state_stamina, state_block
    _error("startup-recovery-cost-family-unsupported", cost_type)


def recover_plan2_transitional_startup(
    transition: AuditionLocalSaveStateEvidence,
    *,
    database: str | Path = DEFAULT_DATABASE,
    observation: Plan2CardHistoryObservation | None = None,
) -> Plan2CompletedCardReplay:
    """Reprove the retained current card from game-owned LocalSave logs."""

    if not isinstance(transition, AuditionLocalSaveStateEvidence):
        raise TypeError("transition must be typed ExamSaveData evidence")
    if observation is not None and not isinstance(
        observation, Plan2CardHistoryObservation
    ):
        raise TypeError("observation must be Plan2CardHistoryObservation or None")
    state = transition.state
    runtime = state.root_runtime
    playing = state.playing_card
    if (
        state.phase != 6
        or playing is None
        or playing.runtime_state is None
        or playing.runtime_state.play_count < 1
        or runtime is None
        or runtime.command_list_is_empty
        or runtime.draw_card_guid_list
        or state.removed_cards
        or state.zones.hold
    ):
        _error("startup-recovery-transition-boundary-invalid")
    directory = Path(transition.source_path).parent
    play_index = _load_play_index(directory, transition)
    compilation = compile_plan2_native_program_catalog(database=database)
    program = compilation.catalog.get(playing)
    if program is None:
        _error("startup-recovery-card-program-missing", playing.card_id)
    native_cost = program.native_cost
    cost_type = None
    base_cost = 0
    if native_cost is not None:
        cost_type = native_cost.cost_type
        base_cost = native_cost.base_cost
    elif program.cost_type != "none":
        cost_type = COST_STAMINA
        base_cost = program.cost_value

    opaque = deepcopy(runtime.opaque_fields.to_value())
    if not isinstance(opaque, dict):
        _error("startup-recovery-root-runtime-invalid")
    first_card = state.turn_card_play_count == 0
    previous_resources = None
    if first_card and cost_type == COST_STAMINA and state.current_turn > 1:
        previous_resources = _load_previous_resources(directory, state.current_turn)
    before_stamina, before_block = _rewind_current_cost(
        opaque,
        cost_type=cost_type,
        base_cost=base_cost,
        first_card=first_card,
        state_stamina=state.stamina,
        state_block=state.block,
        previous_resources=previous_resources,
    )
    before_runtime = replace(
        runtime,
        command_list=CanonicalJsonValue.from_value([], "startup command_list"),
        opaque_fields=CanonicalJsonValue.from_value(opaque, "startup root runtime"),
    )
    selected = replace(
        playing,
        zone_order=play_index,
        runtime_state=replace(
            playing.runtime_state,
            play_count=playing.runtime_state.play_count - 1,
        ),
    )
    hand = list(state.zones.hand)
    hand.insert(play_index, selected)
    hand = [replace(card, zone_order=index) for index, card in enumerate(hand)]
    before_state = replace(
        state,
        stamina=before_stamina,
        block=before_block,
        zones=LocalSaveExamZones(
            tuple(hand),
            state.zones.deck,
            state.zones.grave,
            state.zones.lost,
            state.zones.hold,
        ),
        playing_card=None,
        root_runtime=before_runtime,
    )
    canonical = json.dumps(
        before_state.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    before = replace(
        transition,
        state=before_state,
        session_transition_id=(
            f"recovered-before:{state.current_turn}:{playing.guid}"
        ),
        source_sha256=hashlib.sha256(b"plan2-startup-v2:" + canonical).hexdigest(),
        source_size=len(canonical),
    )

    bootstrap = _bootstrap(before, compilation.catalog)
    if not bootstrap.simulation_ready or bootstrap.state is None:
        detail = ",".join(
            blocker.code + (":" + blocker.detail if blocker.detail else "")
            for blocker in bootstrap.blockers
        )
        _error("startup-recovery-before-bootstrap-blocked", detail)
    native = next(
        (card for card in bootstrap.state.zones.hand if card.guid == playing.guid),
        None,
    )
    recovered_program = None if native is None else compilation.catalog.get(native)
    if native is None or recovered_program is None:
        _error("startup-recovery-card-program-missing", playing.card_id)
    action = Plan2NativeAction("play", playing.guid)
    result = replay_completed_plan2_card_history(
        before,
        transition,
        action,
        before_horizon=bootstrap.state,
        catalog=compilation.catalog,
        database=database,
        observation=observation,
    )
    if not result.supported or result.replay is None:
        detail = ",".join(
            issue.code + (":" + issue.detail if issue.detail else "")
            for issue in result.issues
        )
        _error("startup-recovery-card-replay-rejected", detail)
    return result.replay


__all__ = [
    "Plan2TransitionalStartupRecoveryError",
    "recover_plan2_transitional_startup",
]
