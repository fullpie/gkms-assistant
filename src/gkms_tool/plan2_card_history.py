"""Exact replay of a completed Plan2 card retained in ``commandList``.

Plan2 can leave ``playingCard`` and the original ``9, 5..., 13, 6, 9``
restore queue in ExamSaveData after the client has already finished the card
and returned to an actionable Hand.  At that boundary the persisted scalar
fields are post-cost but pre-effect.  This module proves that boundary against
the previous settled evidence, the compiled Plan2 Master program, and one
native horizon transition.  It never treats the transitional scalar snapshot
as the logical after-state.

The accepted command-effect subset is deliberately small and explicit.  An
unknown numeric effect identity, command shape, cost family, item runtime, or
zone mutation returns a typed issue instead of manufacturing a replay.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
from enum import StrEnum
import json
from pathlib import Path
import sqlite3
from typing import Mapping, Sequence, TypeAlias

from .audition_local_save_state import (
    AuditionLocalSaveStateEvidence,
    CanonicalJsonValue,
    LocalSaveExamCard,
    LocalSaveExamState,
)
from .audition_native_ordered_zones import NativeOrderedCardInstance
from .card_play_count_receipt import CARD_PLAY_COUNT_INCREMENT_OPERATION
from .logic_engine import COST_STAMINA
from .master_db import DEFAULT_DATABASE
from .plan2_exam_save_battle_scoring import (
    plan2_battle_scoring_context_from_exam_save,
)
from .plan2_native_catalog_stamina import (
    Plan2CostResources,
    Plan2NativeCardCost,
    Plan2NativeCostRequest,
    PlayOrigin,
    pay_plan2_native_cost,
)
from .plan2_native_catalog_aggressive_additive import (
    AggressiveAdditiveLayer,
    PITEM_STATUS_CHANGE_ADDITIVE_EFFECT_ID,
    Plan2AggressiveAdditiveRuntime,
)
from .plan2_native_catalog_review_dynamic import (
    LAYER_REVIEW_ADDITIVE,
    Plan2NativeReviewDynamicLayer,
)
from .plan2_native_horizon import (
    PHASE_MAIN,
    Plan2NativeAction,
    Plan2NativeCardProgram,
    Plan2NativeDrinkAction,
    Plan2NativeHorizonState,
    Plan2NativeProgramCatalog,
    Plan2NativeTransition,
    materialize_plan2_native_card_program,
    simulate_plan2_native_action,
    simulate_plan2_native_action_lifecycle,
)
from .plan2_native_item_runtime import (
    EFFECT_AGGRESSIVE_ADDITIVE as ITEM_EFFECT_AGGRESSIVE_ADDITIVE,
    EFFECT_LESSON_DEPEND_REVIEW as ITEM_EFFECT_LESSON_DEPEND_REVIEW,
    EFFECT_REVIEW_ADDITIVE as ITEM_EFFECT_REVIEW_ADDITIVE,
    EFFECT_STAMINA_RECOVER_FIX as ITEM_EFFECT_STAMINA_RECOVER_FIX,
    ITEM_PHASE_CARD_PLAY_AFTER,
    ITEM_PHASE_STATUS_CHANGE,
    Plan2NativeItemDispatch,
    Plan2NativeItemEvent,
    dispatch_plan2_native_item_event,
    restore_plan2_native_item_runtime,
)
from .plan2_native_evidence_semantics import (
    Plan2NativeDrinkCommitProof,
    prove_plan2_native_drink_commit,
    validate_plan2_native_drink_commit,
)
from .plan2_native_program_catalog import (
    Plan2MasterPlayingExecutor,
    Plan2MasterScalarOperation,
    Plan2MasterTriggeredOperation,
)
from .plan2_symbolic_generated_inputs import is_plan2_symbolic_guid
# Observation provenance for the corrected HUD turn contract.  The large
# remaining-turn number already includes every ExtraTurn materialized in
# LocalSave ``remainTurn``.  Only an ExtraTurn still pending in the retained
# native command queue is added by the reader.
PLAN2_HUD_TURNS_RAW_REMAIN_V2 = "hud-turns-raw-remain-v2"


_PITEM_STATUS_CHANGE_REVIEW_EFFECT_ID = (
    "e_effect-exam_review_additive-0500-02"
)
_PITEM_STATUS_CHANGE_EFFECT_STATUS_SHAPES = (
    (
        _PITEM_STATUS_CHANGE_REVIEW_EFFECT_ID,
        177,
        "ReviewAdditiveStatusEffect",
    ),
    (
        PITEM_STATUS_CHANGE_ADDITIVE_EFFECT_ID,
        176,
        "AggressiveAdditiveStatusEffect",
    ),
)
_ADDITIVE_STATUS_TYPE = {
    "asm": "Assembly-CSharp",
    "ns": "Campus.InGame.Exam",
}


# These enum values are already independently used by the reviewed Plan2
# native effect/item adapters.  Only simple direct command shapes are admitted
# below; the native horizon remains the authority for their actual semantics.
_COMMAND_EFFECT_TYPE_BY_MASTER = {
    "ProduceExamEffectType_ExamLesson": 1,
    "ProduceExamEffectType_ExamBlock": 3,
    "ProduceExamEffectType_ExamCardDraw": 4,
    "ProduceExamEffectType_ExamStaminaConsumptionDown": 5,
    "ProduceExamEffectType_ExamCardCreateId": 6,
    "ProduceExamEffectType_ExamStaminaReduceFix": 7,
    "ProduceExamEffectType_ExamCardMove": 9,
    "ProduceExamEffectType_ExamCardUpgrade": 11,
    "ProduceExamEffectType_ExamPlayableValueAdd": 14,
    "ProduceExamEffectType_ExamBlockRestriction": 18,
    "ProduceExamEffectType_ExamLessonDependBlock": 19,
    "ProduceExamEffectType_ExamCardCreateSearch": 21,
    "ProduceExamEffectType_ExamStatusEnchant": 22,
    "ProduceExamEffectType_ExamForcePlayCardSearch": 24,
    "ProduceExamEffectType_ExamStaminaRecoverFix": 28,
    "ProduceExamEffectType_ExamReview": 31,
    "ProduceExamEffectType_ExamReviewValueMultiple": 36,
    "ProduceExamEffectType_ExamCardSearchEffectPlayCountBuff": 38,
    "ProduceExamEffectType_ExamLessonValueMultiple": 39,
    "ProduceExamEffectType_ExamCardPlayAggressive": 42,
    "ProduceExamEffectType_ExamSearchPlayCardStaminaConsumptionChange": 59,
    # Observed in the game-owned retained queue for
    # p_card-02-men-3_002@0 and independently compiled by the exact Plan2
    # ExtraTurn catalog.  This is the Android ProduceExamEffectType enum
    # value, not a guessed UI/card mapping.
    "ProduceExamEffectType_ExamExtraTurn": 63,
    # Observed in the game-owned retained queue for
    # p_card-00-men-3_003@0.  The exact AntiDebuff operation is already
    # compiled/executed by the Plan2 debuff catalog; this enum binds the
    # serialized command row to that same Master effect.
    "ProduceExamEffectType_ExamAntiDebuff": 66,
    "ProduceExamEffectType_ExamStaminaConsumptionAdd": 69,
    "ProduceExamEffectType_ExamStaminaConsumptionDownFix": 93,
    # Native retained queues serialize HandGraveCountCardDraw as effect type
    # 98.  The exact ordered-zone semantics are already executed by the Plan2
    # horizon; this table only proves that the persisted command belongs to
    # the compiled Master effect instead of rejecting the completed card.
    "ProduceExamEffectType_ExamHandGraveCountCardDraw": 98,
    "ProduceExamEffectType_ExamEffectTimer": 103,
    "ProduceExamEffectType_ExamStaminaRecoverMultiple": 117,
    "ProduceExamEffectType_ExamBlockFix": 119,
    "ProduceExamEffectType_ExamLessonDependExamReview": 124,
    "ProduceExamEffectType_ExamLessonDependExamCardPlayAggressive": 125,
    "ProduceExamEffectType_ExamBlockPerUseCardCount": 133,
    "ProduceExamEffectType_ExamBlockAddMultipleAggressive": 143,
    "ProduceExamEffectType_ExamDebuffRecover": 148,
    "ProduceExamEffectType_ExamAggressiveValueMultiple": 149,
    "ProduceExamEffectType_ExamAddGrowEffect": 156,
    "ProduceExamEffectType_ExamReviewPerSearchCount": 159,
    "ProduceExamEffectType_ExamAggressiveAdditive": 176,
    "ProduceExamEffectType_ExamReviewAdditive": 177,
    "ProduceExamEffectType_ExamLessonValueMultipleDependReviewOrAggressive": 181,
    "ProduceExamEffectType_ExamReviewMultiple": 182,
    "ProduceExamEffectType_ExamLessonDependBlockConsumptionSum": 186,
    "ProduceExamEffectType_ExamAggressiveAdditiveFix": 212,
    "ProduceExamEffectType_ExamStatusEnchantEncore": 219,
}

_COMMAND_MOVE_POSITION_BY_MASTER = {
    "ProduceCardMovePositionType_Unknown": 0,
    "ProduceCardMovePositionType_Hand": 1,
    "ProduceCardMovePositionType_DeckFirst": 2,
    "ProduceCardMovePositionType_DeckLast": 3,
    "ProduceCardMovePositionType_DeckRandom": 4,
    "ProduceCardMovePositionType_Grave": 5,
    "ProduceCardMovePositionType_Lost": 6,
    "ProduceCardMovePositionType_Hold": 7,
}

_COMMAND_PICK_RANGE_BY_MASTER = {
    "ProducePickRangeType_Unknown": 0,
    "ProducePickRangeType_Select": 1,
    "ProducePickRangeType_Random": 2,
    "ProducePickRangeType_All": 3,
}

_COMMAND_PICK_COUNT_BY_MASTER = {
    "ProducePickCountType_Unknown": 0,
}

_RAW_CARD_FIELDS = {
    "_affectGrowEffectIdList",
    "_baseUpgradeCount",
    "_cardData",
    "_fixedDeckOrder",
    "_growEffectExamStartAfterList",
    "_guid",
    "_isMoveProduceExamEffectUseInTurn",
    "_playCount",
    "_staminaConsumptionSpecifyEffectList",
    "_statusEffect",
    "_supportUpgradeIdList",
    "_tmpUpgradeCount",
}

_COMMAND_FIELDS = {
    "_assetId",
    "_cardGuids",
    "_cardSelectSearchId",
    "_cardSelectSearchId2",
    "_effectTriggerId",
    "_enchantEffectUid",
    "_isCardSelect",
    "_isCardSelect2",
    "_isConsumeCost",
    "_isManual",
    "_isPlayingGimmick",
    "_isPlayingMoveCardEffect",
    "_isSeparateStart",
    "_isSkipForCalculateForecast",
    "_isUsePlayableCardCount",
    "_originEffectIndex",
    "_phaseType",
    "_playCardPositionType",
    "_playEffect",
    "_playIndex",
    "_playType",
    "_playingCard",
    "_playingDrink",
    "_playingEnchantIsTriggerActive",
    "_playingGimmick",
    "_playingItem",
    "_remainCanPlayCardCount",
    "_selectIndex",
    "_startEnchantOriginId",
    "_startEnchantOriginLevel",
    "_startEnchantOriginType",
    "_startEnchantOwnerId",
    "_supportCardIds",
    "_triggeredGrowEffectAffectLists",
    "_useEffectUidList",
}

_PLAY_EFFECT_FIELDS = {
    "_cardGrowEffectIdList",
    "_cardSearchId",
    "_cardSearchId2",
    "_chainEffectId",
    "_chainEffectIdList",
    "_effectCount",
    "_effectGroupIdList",
    "_effectTurn",
    "_effectType",
    "_effectValue1",
    "_effectValue2",
    "_id",
    "_judgeTargetIndex",
    "_movePositionType",
    "_pickCount",
    "_pickCountMax",
    "_pickCountMax2",
    "_pickCountMin",
    "_pickCountMin2",
    "_pickCountReferenceProduceCardSearchId",
    "_pickCountReferenceProduceCardSearchId2",
    "_pickCountType",
    "_pickCountType2",
    "_pickRangeType",
    "_pickRangeType2",
    "_statusEnchantId",
    "_targetExamEffectType",
    "_targetProduceCardId",
    "_targetUpgradeCount",
}

_EMPTY_PLAY_EFFECT = {
    "_cardGrowEffectIdList": [],
    "_cardSearchId": "",
    "_cardSearchId2": "",
    "_chainEffectId": "",
    "_chainEffectIdList": [],
    "_effectCount": 0,
    "_effectGroupIdList": [],
    "_effectTurn": 0,
    "_effectType": 0,
    "_effectValue1": 0,
    "_effectValue2": 0,
    "_id": "",
    "_judgeTargetIndex": 0,
    "_movePositionType": 0,
    "_pickCount": 0,
    "_pickCountMax": 0,
    "_pickCountMax2": 0,
    "_pickCountMin": 0,
    "_pickCountMin2": 0,
    "_pickCountReferenceProduceCardSearchId": "",
    "_pickCountReferenceProduceCardSearchId2": "",
    "_pickCountType": 0,
    "_pickCountType2": 0,
    "_pickRangeType": 0,
    "_pickRangeType2": 0,
    "_statusEnchantId": "",
    "_targetExamEffectType": 0,
    "_targetProduceCardId": "",
    "_targetUpgradeCount": 0,
}

_EMPTY_PLAYING_DRINK = {"_id": ""}
_EMPTY_PLAYING_ITEM = {
    "_fireCount": 0,
    "_id": "",
    "_itemType": 0,
    "_parentCustomItemIds": [],
    "_reactionCount": 0,
}
_EMPTY_PLAYING_GIMMICK = {
    "_gimmickEffect": {"_effectId": ""},
    "_gimmickTrigger": {
        "_id": "",
        "_phaseTypeList": [],
        "_phaseValueList": [],
    },
    "_id": "",
    "_isPositive": False,
    "_priority": 0,
    "_remainingTurn": 0,
    "_remainingTurnPermil": 0,
    "_startTurn": 0,
}


@dataclass(frozen=True, slots=True)
class Plan2CardHistoryIssue:
    """One fail-closed reason for rejecting a persisted card queue."""

    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("issue code must be non-empty text")
        if not isinstance(self.detail, str):
            raise TypeError("issue detail must be text")


class Plan2CardHistoryObservationAuthority(StrEnum):
    """Whether an observation may participate in logical replay."""

    DIAGNOSTIC = "diagnostic"
    LOGICAL_REPLAY = "logical-replay"


@dataclass(frozen=True, slots=True)
class Plan2CardHistoryObservation:
    """One post-animation observation with explicit state authority.

    Maa/HUD/OCR observations are diagnostic by default and must never modify
    the logical horizon.  ``LOGICAL_REPLAY`` is reserved for a snapshot built
    from an already-proven native horizon, such as restart-journal replay.
    """

    turns_remaining: int
    score: int
    stamina: int
    block: int
    review: int | None = None
    aggressive: int | None = None
    plays_remaining: int | None = None
    source: str = "maa-exam-hud"
    score_authoritative: bool = True
    authority: Plan2CardHistoryObservationAuthority = (
        Plan2CardHistoryObservationAuthority.DIAGNOSTIC
    )

    def __post_init__(self) -> None:
        for name in ("turns_remaining", "score", "stamina", "block"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"observation {name} must be non-negative")
        for name in ("review", "aggressive", "plays_remaining"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"observation {name} must be non-negative or None")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("observation source must be non-empty text")
        if not isinstance(self.score_authoritative, bool):
            raise TypeError("observation score_authoritative must be boolean")
        if not isinstance(
            self.authority, Plan2CardHistoryObservationAuthority
        ):
            raise TypeError("observation authority must be typed")


@dataclass(frozen=True, slots=True)
class Plan2CompletedCardReplay:
    """A proven transitional ExamSave queue and its logical Plan2 after-state."""

    action: Plan2NativeAction
    card_guid: str
    card_id: str
    command_effect_ids: tuple[str, ...]
    program_effect_ids: tuple[str, ...]
    fired_item_enchantment_ids: tuple[str, ...]
    item_effect_ids: tuple[str, ...]
    persisted_item_source_fire_counts: tuple[tuple[str, int], ...]
    remaining_plays: int
    before: Plan2NativeHorizonState
    after: Plan2NativeHorizonState
    transition: Plan2NativeTransition
    persisted_before: AuditionLocalSaveStateEvidence
    persisted_transition: AuditionLocalSaveStateEvidence
    trace: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.action.kind != "play" or self.action.card_guid != self.card_guid:
            raise ValueError("replay action and card GUID do not agree")
        if not self.card_id or not self.command_effect_ids:
            raise ValueError("replay card/effect identity must be non-empty")
        if self.remaining_plays < 0 or self.after.plays_remaining != self.remaining_plays:
            raise ValueError("completed replay remaining plays are invalid")
        if not self.transition.supported or self.transition.after != self.after:
            raise ValueError("replay transition is not the logical after-state")
        if self.transition.before != self.before:
            raise ValueError("replay transition does not begin at before")

    @property
    def logical_after(self) -> Plan2NativeHorizonState:
        return self.after

    @property
    def rng_after(self) -> int:
        return self.after.zones.random_state


@dataclass(frozen=True, slots=True)
class Plan2CompletedDrinkReplay:
    """One exact retained drink queue and its logical post-effect horizon.

    ``materialized_from_local_save`` is used when the game has already removed
    the drink from its durable inventory and written the resulting scalar
    state, while retaining presentation commands.  In that case no formula is
    replayed a second time: the cleaned LocalSave is the post-effect authority.
    """

    action: Plan2NativeDrinkAction
    before: Plan2NativeHorizonState
    after: Plan2NativeHorizonState
    transition: Plan2NativeTransition
    persisted_transition: AuditionLocalSaveStateEvidence
    command_effect_ids: tuple[str, ...]
    trace: tuple[str, ...]
    persisted_before: AuditionLocalSaveStateEvidence | None = None
    materialized_from_local_save: bool = False
    provisional_from_submitted_receipt: bool = False
    commit_proof: Plan2NativeDrinkCommitProof | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, Plan2NativeDrinkAction):
            raise TypeError("drink replay action must be typed")
        if not self.command_effect_ids:
            raise ValueError("drink replay must retain command effect identity")
        if not isinstance(self.materialized_from_local_save, bool):
            raise TypeError("materialized_from_local_save must be boolean")
        if not isinstance(self.provisional_from_submitted_receipt, bool):
            raise TypeError(
                "provisional_from_submitted_receipt must be boolean"
            )
        if self.materialized_from_local_save and self.provisional_from_submitted_receipt:
            raise ValueError("drink replay cannot be materialized and provisional")
        if self.provisional_from_submitted_receipt and self.commit_proof is not None:
            raise ValueError("provisional drink replay cannot carry commit proof")
        if self.persisted_before is None:
            object.__setattr__(self, "persisted_before", self.persisted_transition)
        elif not isinstance(self.persisted_before, AuditionLocalSaveStateEvidence):
            raise TypeError("drink replay before evidence must be typed")
        if not self.transition.supported or self.transition.after != self.after:
            raise ValueError("drink replay transition is not the logical after-state")
        if self.transition.before != self.before or self.transition.action != self.action:
            raise ValueError("drink replay transition does not bind before/action")
        if not isinstance(
            self.persisted_transition, AuditionLocalSaveStateEvidence
        ):
            raise TypeError("drink replay must retain typed evidence")
        if self.materialized_from_local_save and (
            self.before != self.after
            or "drink-history:materialized-local-save" not in self.trace
        ):
            raise ValueError("materialized drink replay must be a marked evidence rebase")
        if self.provisional_from_submitted_receipt and (
            self.persisted_before != self.persisted_transition
            or "drink-history:provisional-submitted-receipt" not in self.trace
        ):
            raise ValueError(
                "provisional drink replay must retain its unchanged save boundary"
            )
        if self.commit_proof is not None:
            validate_plan2_native_drink_commit(
                self.commit_proof,
                self.persisted_before,
                self.persisted_transition,
                self.action,
            )
            if self.commit_proof.command_effect_ids != self.command_effect_ids:
                raise ValueError(
                    "drink replay command effects differ from commit proof"
                )

    @property
    def logical_after(self) -> Plan2NativeHorizonState:
        return self.after


Plan2CompletedLogicalReplay: TypeAlias = (
    Plan2CompletedCardReplay | Plan2CompletedDrinkReplay
)


@dataclass(frozen=True, slots=True)
class Plan2CardHistoryReplayResult:
    replay: Plan2CompletedCardReplay | None
    issues: tuple[Plan2CardHistoryIssue, ...] = ()

    def __post_init__(self) -> None:
        issues = tuple(self.issues)
        if any(not isinstance(value, Plan2CardHistoryIssue) for value in issues):
            raise TypeError("issues must contain Plan2CardHistoryIssue values")
        object.__setattr__(self, "issues", issues)
        if (self.replay is None) == (not issues):
            raise ValueError("result must contain exactly one of replay or issues")

    @property
    def supported(self) -> bool:
        return self.replay is not None and not self.issues


@dataclass(frozen=True, slots=True)
class Plan2DrinkHistoryReplayResult:
    replay: Plan2CompletedDrinkReplay | None
    issues: tuple[Plan2CardHistoryIssue, ...] = ()

    def __post_init__(self) -> None:
        issues = tuple(self.issues)
        if any(not isinstance(value, Plan2CardHistoryIssue) for value in issues):
            raise TypeError("issues must contain Plan2CardHistoryIssue values")
        object.__setattr__(self, "issues", issues)
        if (self.replay is None) == (not issues):
            raise ValueError("drink replay result must contain replay xor issues")

    @property
    def supported(self) -> bool:
        return self.replay is not None and not self.issues


@dataclass(frozen=True, slots=True)
class _MasterEffectSpec:
    effect_id: str
    raw: Mapping[str, object]
    required: bool


class _ReplayRejected(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.issue = Plan2CardHistoryIssue(code, detail)
        super().__init__(code if not detail else f"{code}:{detail}")


def _reject(code: str, detail: str = "") -> None:
    raise _ReplayRejected(code, detail)


def _native_cards(
    cards: Sequence[LocalSaveExamCard],
) -> tuple[NativeOrderedCardInstance, ...]:
    try:
        return tuple(NativeOrderedCardInstance.from_local_save(card) for card in cards)
    except (TypeError, ValueError) as error:
        _reject("plan2-card-history-card-runtime-invalid", str(error))


def _cross_authority_card_equal(
    local_save_card: NativeOrderedCardInstance,
    logical_card: NativeOrderedCardInstance,
) -> bool:
    """Compare one persisted card with one solver card without conflating counts.

    Android's serialized card object can expose a lifecycle ``_playCount`` one
    higher than the solver's semantic accepted-play count after a completed
    move.  Those are already modelled as separate authorities by the T1/T2
    acceptance report.  Card history therefore keeps GUID, identity, upgrade,
    support lineage, fixed order, and every other runtime field exact while it
    deliberately does not require the two authorities' play-count integers to
    be equal.
    """

    if local_save_card == logical_card:
        return True
    if (
        local_save_card.guid != logical_card.guid
        or local_save_card.card_id != logical_card.card_id
        or local_save_card.base_upgrade != logical_card.base_upgrade
        or local_save_card.temporary_upgrade != logical_card.temporary_upgrade
        or local_save_card.effective_upgrade != logical_card.effective_upgrade
        or local_save_card.support_upgrade_ids != logical_card.support_upgrade_ids
        or local_save_card.fixed_deck_order != logical_card.fixed_deck_order
    ):
        return False
    return replace(
        local_save_card.runtime_state,
        play_count=logical_card.runtime_state.play_count,
    ) == logical_card.runtime_state


def _cross_authority_zone_equal(
    local_save_cards: Sequence[LocalSaveExamCard],
    logical_cards: Sequence[NativeOrderedCardInstance],
) -> bool:
    persisted = _native_cards(local_save_cards)
    return len(persisted) == len(logical_cards) and all(
        _cross_authority_card_equal(actual, expected)
        for actual, expected in zip(persisted, logical_cards)
    )


def _materialize_symbolic_generated_guids(
    horizon: Plan2NativeHorizonState,
    evidence: AuditionLocalSaveStateEvidence,
    *,
    omitted_logical_guids: tuple[str, ...] = (),
) -> tuple[Plan2NativeHorizonState, tuple[tuple[str, str], ...]]:
    """Bind search-only generated GUIDs to one exact later LocalSave layout.

    A retained pre-effect queue does not yet contain the native GUID allocated
    by ``CardCreateId``.  Plan2 therefore uses a deterministic symbolic GUID
    while replaying that queue.  The next LocalSave transaction publishes the
    game GUID.  Rebind only when every ordered zone matches exactly and the
    sole difference at a symbolic position is that GUID; card ID, upgrade,
    support lineage, fixed order and full runtime must all match.

    ``omitted_logical_guids`` is the next submitted PLAY, which is already
    absent from that retained transition's ordinary zones.  It is never used
    to omit or identify a symbolic card.
    """

    if not isinstance(horizon, Plan2NativeHorizonState):
        raise TypeError("horizon must be a typed Plan2 state")
    if not isinstance(evidence, AuditionLocalSaveStateEvidence):
        raise TypeError("evidence must be typed")
    omitted = set(omitted_logical_guids)
    if any(is_plan2_symbolic_guid(value) for value in omitted):
        _reject("plan2-card-history-symbolic-action-guid-forbidden")
    symbolic_guids = {
        card.guid
        for card in horizon.zones.card_universe
        if is_plan2_symbolic_guid(card.guid)
    }
    if not symbolic_guids:
        return horizon, ()
    native_horizon_guids = {
        card.guid
        for card in horizon.zones.card_universe
        if not is_plan2_symbolic_guid(card.guid)
    }

    native_by_symbolic: dict[str, NativeOrderedCardInstance] = {}
    observed_guids: set[str] = set()
    for zone_name in ("hand", "deck", "grave", "lost"):
        logical_cards = tuple(
            card
            for card in getattr(horizon.zones, zone_name)
            if card.guid not in omitted
        )
        observed_cards = _native_cards(getattr(evidence.state.zones, zone_name))
        if len(logical_cards) != len(observed_cards):
            # Leave the ordinary exact zone proof to report its established
            # typed mismatch.  A partial layout is not enough to bind GUIDs.
            return horizon, ()
        for logical_card, observed_card in zip(logical_cards, observed_cards):
            if not is_plan2_symbolic_guid(logical_card.guid):
                if not _cross_authority_card_equal(observed_card, logical_card):
                    return horizon, ()
                continue
            if (
                is_plan2_symbolic_guid(observed_card.guid)
                or observed_card.guid in observed_guids
                or observed_card.guid in native_horizon_guids
                or replace(logical_card, guid=observed_card.guid) != observed_card
            ):
                _reject(
                    "plan2-card-history-generated-guid-materialization-mismatch",
                    f"{logical_card.guid}:{zone_name}",
                )
            previous = native_by_symbolic.get(logical_card.guid)
            if previous is not None and previous != observed_card:
                _reject(
                    "plan2-card-history-generated-guid-materialization-ambiguous",
                    logical_card.guid,
                )
            native_by_symbolic[logical_card.guid] = observed_card
            observed_guids.add(observed_card.guid)

    if set(native_by_symbolic) != symbolic_guids:
        return horizon, ()

    def mapped_guid(value: str) -> str:
        card = native_by_symbolic.get(value)
        return value if card is None else card.guid

    def mapped_card(card: NativeOrderedCardInstance) -> NativeOrderedCardInstance:
        return native_by_symbolic.get(card.guid, card)

    zones = replace(
        horizon.zones,
        card_universe=tuple(
            sorted(
                (mapped_card(card) for card in horizon.zones.card_universe),
                key=lambda card: card.guid,
            )
        ),
        hand=tuple(mapped_card(card) for card in horizon.zones.hand),
        deck=tuple(mapped_card(card) for card in horizon.zones.deck),
        grave=tuple(mapped_card(card) for card in horizon.zones.grave),
        lost=tuple(mapped_card(card) for card in horizon.zones.lost),
        pending_played=(
            None
            if horizon.zones.pending_played is None
            else mapped_card(horizon.zones.pending_played)
        ),
    )
    generated_inputs = tuple(
        replace(
            value,
            source_guid=mapped_guid(value.source_guid),
            guid_tokens=(
                None
                if value.guid_tokens is None
                else tuple(mapped_guid(guid) for guid in value.guid_tokens)
            ),
            force_selected_guid=(
                None
                if value.force_selected_guid is None
                else mapped_guid(value.force_selected_guid)
            ),
        )
        for value in horizon.generated_inputs
    )
    status_child_inputs = tuple(
        replace(
            value,
            guid_tokens=(
                None
                if value.guid_tokens is None
                else tuple(mapped_guid(guid) for guid in value.guid_tokens)
            ),
            playing_guid=(
                None
                if value.playing_guid is None
                else mapped_guid(value.playing_guid)
            ),
        )
        for value in horizon.status_child_inputs
    )
    command_queue = tuple(
        command
        if not command.card_guid
        else replace(command, card_guid=mapped_guid(command.card_guid))
        for command in horizon.command_queue
    )
    materialized = replace(
        horizon,
        zones=zones,
        generated_inputs=generated_inputs,
        status_child_inputs=status_child_inputs,
        command_queue=command_queue,
        source_kind=f"{horizon.source_kind}+generated-guid-materialized",
    )
    mapping = tuple(
        (symbolic, native_by_symbolic[symbolic].guid)
        for symbolic in sorted(native_by_symbolic)
    )
    return materialized, mapping


def _apply_proven_generated_guid_mapping(
    horizon: Plan2NativeHorizonState,
    mapping: tuple[tuple[str, str], ...],
) -> Plan2NativeHorizonState:
    """Carry an already-proven generated GUID back across one logical replay.

    ``_materialize_symbolic_generated_guids`` proves the mapping against the
    first LocalSave that publishes the native GUID.  A retained queue can keep
    the generated card symbolic across one intervening drink transaction.  In
    that case the drink's logical before-state uses the same symbolic identity
    as its after-state, so only that identity may be rebased; card content,
    ordering, runtime, scalar state, and RNG remain untouched.
    """

    if not isinstance(horizon, Plan2NativeHorizonState):
        raise TypeError("horizon must be a typed Plan2 state")
    native_by_symbolic = dict(mapping)
    if len(native_by_symbolic) != len(mapping) or any(
        not is_plan2_symbolic_guid(symbolic)
        or not isinstance(native, str)
        or not native
        or is_plan2_symbolic_guid(native)
        for symbolic, native in mapping
    ):
        _reject("plan2-card-history-generated-guid-proven-mapping-invalid")
    symbolic_guids = {
        card.guid
        for card in horizon.zones.card_universe
        if is_plan2_symbolic_guid(card.guid)
    }
    if not set(native_by_symbolic).issubset(symbolic_guids):
        _reject("plan2-card-history-generated-guid-prior-before-missing")
    existing_native_guids = {
        card.guid
        for card in horizon.zones.card_universe
        if not is_plan2_symbolic_guid(card.guid)
    }
    if existing_native_guids & set(native_by_symbolic.values()):
        _reject("plan2-card-history-generated-guid-prior-before-collision")

    def mapped_guid(value: str) -> str:
        return native_by_symbolic.get(value, value)

    def mapped_card(card: NativeOrderedCardInstance) -> NativeOrderedCardInstance:
        mapped = mapped_guid(card.guid)
        return card if mapped == card.guid else replace(card, guid=mapped)

    zones = replace(
        horizon.zones,
        card_universe=tuple(
            sorted(
                (mapped_card(card) for card in horizon.zones.card_universe),
                key=lambda card: card.guid,
            )
        ),
        hand=tuple(mapped_card(card) for card in horizon.zones.hand),
        deck=tuple(mapped_card(card) for card in horizon.zones.deck),
        grave=tuple(mapped_card(card) for card in horizon.zones.grave),
        lost=tuple(mapped_card(card) for card in horizon.zones.lost),
        pending_played=(
            None
            if horizon.zones.pending_played is None
            else mapped_card(horizon.zones.pending_played)
        ),
    )
    generated_inputs = tuple(
        replace(
            value,
            source_guid=mapped_guid(value.source_guid),
            guid_tokens=(
                None
                if value.guid_tokens is None
                else tuple(mapped_guid(guid) for guid in value.guid_tokens)
            ),
            force_selected_guid=(
                None
                if value.force_selected_guid is None
                else mapped_guid(value.force_selected_guid)
            ),
        )
        for value in horizon.generated_inputs
    )
    status_child_inputs = tuple(
        replace(
            value,
            guid_tokens=(
                None
                if value.guid_tokens is None
                else tuple(mapped_guid(guid) for guid in value.guid_tokens)
            ),
            playing_guid=(
                None
                if value.playing_guid is None
                else mapped_guid(value.playing_guid)
            ),
        )
        for value in horizon.status_child_inputs
    )
    command_queue = tuple(
        command
        if not command.card_guid
        else replace(command, card_guid=mapped_guid(command.card_guid))
        for command in horizon.command_queue
    )
    return replace(
        horizon,
        zones=zones,
        generated_inputs=generated_inputs,
        status_child_inputs=status_child_inputs,
        command_queue=command_queue,
        source_kind=f"{horizon.source_kind}+generated-guid-materialized",
    )


def _replace_replay_logical_after(
    replay: Plan2CompletedLogicalReplay,
    after: Plan2NativeHorizonState,
    *,
    audit: tuple[str, ...],
) -> Plan2CompletedLogicalReplay:
    """Carry a metadata/identity-only logical rebase through replay authority."""

    if (
        isinstance(replay, Plan2CompletedDrinkReplay)
        and replay.materialized_from_local_save
    ):
        transition = replace(replay.transition, before=after, after=after)
        return replace(
            replay,
            before=after,
            after=after,
            transition=transition,
            trace=(*replay.trace, *audit),
        )
    transition = replace(replay.transition, after=after)
    return replace(
        replay,
        after=after,
        transition=transition,
        trace=(*replay.trace, *audit),
    )


def _chained_hand_add_identity_equal(
    persisted: NativeOrderedCardInstance,
    logical: NativeOrderedCardInstance,
) -> bool:
    """Accept only support upgrades materialized after a retained snapshot.

    A chained retained queue keeps the preceding card's pre-effect zones.
    Its logical replay can draw another card and append HandAdd support IDs
    before that next card is selected.  GUID lookup and the later exact
    Playing-card check remain authoritative; here the only permitted delta is
    an ordered extension of support IDs.  Resetting support must make every
    other field, including play_count and opaque card runtime, exactly equal.
    """

    persisted_supports = persisted.support_upgrade_ids
    logical_supports = logical.support_upgrade_ids
    return bool(
        len(logical_supports) > len(persisted_supports)
        and logical_supports[: len(persisted_supports)] == persisted_supports
        and persisted.reset_support_upgrade()
        == logical.reset_support_upgrade()
    )


def _raw_card(card: LocalSaveExamCard) -> dict[str, object]:
    runtime = card.runtime_state
    if runtime is None:
        _reject("plan2-card-history-card-runtime-missing", card.guid)
    return {
        "_affectGrowEffectIdList": runtime.affect_grow_effect_id_list.to_value(),
        "_baseUpgradeCount": card.base_upgrade,
        "_cardData": {
            "_customizeCountList": runtime.customize_count_list.to_value(),
            "_id": card.card_id,
            "_produceCardSkinAssetId": runtime.produce_card_skin_asset_id,
            "_produceCardSkinId": runtime.produce_card_skin_id,
            "_upgradeCount": card.effective_upgrade,
        },
        "_fixedDeckOrder": card.fixed_deck_order,
        "_growEffectExamStartAfterList": (
            runtime.grow_effect_exam_start_after_list.to_value()
        ),
        "_guid": card.guid,
        "_isMoveProduceExamEffectUseInTurn": (
            runtime.is_move_produce_exam_effect_use_in_turn
        ),
        "_playCount": runtime.play_count,
        "_staminaConsumptionSpecifyEffectList": (
            runtime.stamina_consumption_specify_effect_list.to_value()
        ),
        "_statusEffect": runtime.status_effect.to_value(),
        "_supportUpgradeIdList": list(card.support_upgrade_ids),
        "_tmpUpgradeCount": card.temporary_upgrade,
    }


def _same_stage_turn_schedule(
    old: LocalSaveExamState,
    new: LocalSaveExamState,
    *,
    completed_card_count: int,
) -> bool:
    if completed_card_count <= 0:
        return bool(
            old.remain_turn == new.remain_turn
            and old.extra_turn == new.extra_turn
            and old.turn_parameter_types == new.turn_parameter_types
        )
    added_turns = new.extra_turn - old.extra_turn
    if old.exam_type == 0 and not old.turn_parameter_types:
        schedule_matches = not new.turn_parameter_types
    else:
        schedule_matches = bool(
            len(new.turn_parameter_types)
            == len(old.turn_parameter_types) + added_turns
            and new.turn_parameter_types[: len(old.turn_parameter_types)]
            == old.turn_parameter_types
        )
    return bool(
        added_turns >= 0
        and new.remain_turn == old.remain_turn + added_turns
        and schedule_matches
    )


def _same_stage_boundary(
    before: AuditionLocalSaveStateEvidence,
    after: AuditionLocalSaveStateEvidence,
    *,
    completed_card_count: int = 0,
) -> bool:
    old = before.state
    new = after.state
    return bool(
        before.run_id == after.run_id
        and before.step_context_id == after.step_context_id
        and before.source_path == after.source_path
        and before.source_type == after.source_type
        and old.character_id == new.character_id
        and old.setting_id == new.setting_id
        and old.exam_type == new.exam_type
        and old.step_type_value == new.step_type_value
        and old.phase == new.phase
        and old.current_turn == new.current_turn
        and old.limit_turn == new.limit_turn
        # For a chained queue, the new snapshot may include the prior card's
        # ExtraTurn effect.  Native appends exactly one parameter type for each
        # added turn while preserving the existing schedule prefix.
        and _same_stage_turn_schedule(
            old,
            new,
            completed_card_count=completed_card_count,
        )
        and old.max_stamina == new.max_stamina
        and old.vocal_bonus_permille == new.vocal_bonus_permille
        and old.dance_bonus_permille == new.dance_bonus_permille
        and old.visual_bonus_permille == new.visual_bonus_permille
        and old.turn_card_play_count + completed_card_count
        == new.turn_card_play_count
        and old.exam_card_play_count + completed_card_count
        == new.exam_card_play_count
        and old.future_deck == new.future_deck
        and old.past_deck == new.past_deck
    )


def _native_extra_turn_schedule_materialization_matches(
    old: LocalSaveExamState,
    new: LocalSaveExamState,
    logical_scoring: object,
    native_scoring: object,
    previous_after: Plan2NativeHorizonState,
) -> bool:
    """Prove one retained prior ExtraTurn's native schedule append."""

    previous_scoring = previous_after.scalar.battle_scoring
    if previous_scoring is None:
        return False
    added_turns = new.extra_turn - old.extra_turn
    return bool(
        added_turns > 0
        and _same_stage_turn_schedule(old, new, completed_card_count=1)
        and getattr(logical_scoring, "current_turn", None)
        == getattr(native_scoring, "current_turn", None)
        and getattr(logical_scoring, "limit_turn", None)
        == getattr(native_scoring, "limit_turn", None)
        and getattr(logical_scoring, "extra_turn", None)
        == getattr(native_scoring, "extra_turn", None)
        == previous_after.extra_turn
        == previous_scoring.extra_turn
        and getattr(logical_scoring, "exam_mode", None)
        == getattr(native_scoring, "exam_mode", None)
        == previous_scoring.exam_mode
        and tuple(getattr(logical_scoring, "turn_parameter_types", ()))
        == tuple(old.turn_parameter_types)
        == tuple(previous_scoring.turn_parameter_types)
        and tuple(getattr(native_scoring, "turn_parameter_types", ()))
        == tuple(new.turn_parameter_types)
    )


def _ordered_drink_ids_from_evidence(
    evidence: AuditionLocalSaveStateEvidence,
) -> tuple[str, ...]:
    runtime = evidence.state.root_runtime
    opaque = None if runtime is None else runtime.opaque_fields.to_value()
    rows = opaque.get("drinkList") if isinstance(opaque, Mapping) else None
    if not isinstance(rows, list):
        _reject("plan2-drink-history-inventory-shape-invalid")
    result: list[str] = []
    for index, row in enumerate(rows):
        drink_id = row.get("_id") if isinstance(row, Mapping) else None
        if not isinstance(drink_id, str) or not drink_id:
            _reject(
                "plan2-drink-history-inventory-shape-invalid",
                str(index),
            )
        result.append(drink_id)
    return tuple(result)


def _prove_before_horizon(
    evidence: AuditionLocalSaveStateEvidence,
    horizon: Plan2NativeHorizonState,
) -> None:
    state = evidence.state
    if not state.is_native_actionable_settled:
        _reject("plan2-card-history-before-not-actionable")
    if horizon.phase != PHASE_MAIN or horizon.terminal or horizon.command_queue:
        _reject("plan2-card-history-before-horizon-not-actionable")
    if horizon.zones.pending_played is not None or state.zones.hold:
        _reject("plan2-card-history-before-zone-boundary-unsupported", "pending-or-hold")
    if horizon.zones.binding.local_save_evidence_digest != evidence.digest():
        _reject("plan2-card-history-before-binding-mismatch")
    scalar = horizon.scalar
    if (
        scalar.current_turn != state.current_turn
        or scalar.score != state.score
        or scalar.stamina != state.stamina
        or scalar.max_stamina != state.max_stamina
        or scalar.block != state.block
        or scalar.turn_card_play_count != state.turn_card_play_count
        or scalar.exam_card_play_count != state.exam_card_play_count
        or horizon.limit_turn != state.limit_turn
        or horizon.extra_turn != state.extra_turn
        or horizon.remaining_turns != state.remain_turn
        or horizon.zones.random_state != state.random_state
    ):
        _reject("plan2-card-history-before-scalar-mismatch")
    for zone_name in ("hand", "deck", "grave", "lost"):
        if _native_cards(getattr(state.zones, zone_name)) != getattr(
            horizon.zones, zone_name
        ):
            _reject("plan2-card-history-before-zone-mismatch", zone_name)


def _prove_chained_before_horizon(
    evidence: AuditionLocalSaveStateEvidence,
    horizon: Plan2NativeHorizonState,
    prior_replay: Plan2CompletedLogicalReplay,
) -> str:
    """Bind a retained prior queue to its already-settled logical horizon.

    The retained ExamSave scalar/status snapshot is deliberately *not* used as
    the next planning root.  It proves the prior queue and stage identity;
    ``prior_replay.after`` proves the settled scalar, RNG, zones and remaining
    play state.  Effects such as CardDraw legitimately change RNG/zones after
    that retained pre-effect snapshot.  The next transition is compared to
    the logical horizon GUID-by-GUID below, so repeating the pre-effect zone
    comparison here would reject valid chained cards.
    """

    state = evidence.state
    runtime = state.root_runtime
    if isinstance(prior_replay, Plan2CompletedDrinkReplay):
        if (
            evidence != prior_replay.persisted_transition
            or horizon != prior_replay.logical_after
            or runtime is None
            or state.phase != 6
            or state.playing_card is not None
            or runtime.command_list_is_empty
            or state.zones.hold
        ):
            _reject("plan2-card-history-chain-prior-drink-mismatch")
        if horizon.phase != PHASE_MAIN or horizon.terminal or horizon.command_queue:
            _reject("plan2-card-history-chain-horizon-not-actionable")
        if horizon.zones.pending_played is not None:
            _reject("plan2-card-history-chain-horizon-pending")
        if (
            horizon.scalar.current_turn != state.current_turn
            or horizon.limit_turn != state.limit_turn
            or horizon.remaining_turns != state.remain_turn
        ):
            _reject("plan2-card-history-chain-stage-mismatch")
        return "drink"

    if (
        evidence != prior_replay.persisted_transition
        or horizon != prior_replay.logical_after
        or runtime is None
        or state.phase != 6
        or state.playing_card is None
        or state.playing_card.guid != prior_replay.card_guid
        or runtime.command_list_is_empty
        or state.zones.hold
    ):
        _reject("plan2-card-history-chain-prior-replay-mismatch")
    if horizon.phase != PHASE_MAIN or horizon.terminal or horizon.command_queue:
        _reject("plan2-card-history-chain-horizon-not-actionable")
    if horizon.zones.pending_played is not None:
        _reject("plan2-card-history-chain-horizon-pending")
    if (
        horizon.scalar.current_turn != state.current_turn
        or horizon.limit_turn != state.limit_turn
    ):
        _reject("plan2-card-history-chain-stage-mismatch")

    # ``evidence`` is the retained pre-effect command queue of the previous
    # card, while ``horizon`` is that card's fully replayed logical result.
    # Effects are allowed to change RemainingTurn/ExtraTurn before the next
    # card is accepted (for example, an extra-turn card at RemainingTurn=1).
    # Those post-effect values are bound against the next persisted queue
    # below; comparing them back to this pre-effect save rejects a valid
    # same-turn chain.  CurrentTurn and LimitTurn remain the stage boundary.

    # HandAdd support upgrade is temporary Hand/Playing identity.  Native
    # MovePlayCard clears it before appending the card to Grave/Lost; compare
    # the retained playingCard with that exact post-move identity.
    prior = NativeOrderedCardInstance.from_local_save(
        state.playing_card
    ).reset_support_upgrade()
    prior_selected = tuple(
        card
        for card in prior_replay.before.zones.hand
        if card.guid == prior.guid
    )
    if len(prior_selected) != 1:
        _reject("plan2-card-history-chain-prior-source-mismatch")
    settled_prior = _settled_card_from_play_count_receipts(
        prior_selected[0],
        prior_replay.transition.operation_receipts,
    )
    destinations = tuple(
        name
        for name in ("grave", "lost")
        if tuple(
            card for card in getattr(horizon.zones, name) if card.guid == prior.guid
        )
        == (settled_prior,)
    )
    if len(destinations) != 1:
        _reject("plan2-card-history-chain-prior-destination-mismatch")
    prior_destination = destinations[0]
    return prior_destination


def _same_turn_used_support_ids(
    persisted: tuple[str, ...], logical: tuple[str, ...]
) -> bool:
    """Compare the native per-turn support-use ledger as an ID set.

    LocalSave preserves the order in which its runtime serialized the IDs,
    while the HandAdd evaluator returns the same used IDs in support-loadout
    traversal order.  Native eligibility asks only whether an ID has already
    been used this turn.  Keep duplicates invalid, but do not reject an exact
    membership match solely because these two authorities order it
    differently.
    """

    return (
        len(persisted) == len(set(persisted))
        and len(logical) == len(set(logical))
        and set(persisted) == set(logical)
    )


def _load_master_effects(
    program: Plan2NativeCardProgram,
    *,
    database: Path,
) -> tuple[tuple[str, ...], tuple[_MasterEffectSpec, ...]]:
    executor = program.native_playing_executor
    if not isinstance(executor, Plan2MasterPlayingExecutor):
        _reject(
            "plan2-card-history-master-playing-executor-required",
            program.native_playing_executor_id,
        )
    program_effect_ids = executor.operation_effect_ids
    if not program_effect_ids:
        _reject("plan2-card-history-program-effect-identity-missing")
    try:
        with closing(
            sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)
        ) as connection:
            connection.row_factory = sqlite3.Row
            cards = tuple(
                connection.execute(
                    """
                    SELECT play_effects_json
                      FROM card
                     WHERE id = ? AND upgrade_count = ?
                    """,
                    program.ref,
                )
            )
            if len(cards) != 1:
                _reject(
                    "plan2-card-history-master-card-not-unique",
                    f"{program.card_id}@{program.upgrade}:{len(cards)}",
                )
            links = json.loads(str(cards[0]["play_effects_json"]))
            if not isinstance(links, list) or not links:
                _reject("plan2-card-history-master-effect-list-invalid")
            ordered: list[str] = []
            required_slots: list[bool] = []
            for index, link in enumerate(links):
                if not isinstance(link, Mapping):
                    _reject(
                        "plan2-card-history-master-effect-link-invalid", str(index)
                    )
                effect_id = link.get("produceExamEffectId")
                trigger_id = link.get("produceExamTriggerId")
                if (
                    not isinstance(effect_id, str)
                    or not effect_id
                    or not isinstance(trigger_id, str)
                    or not isinstance(link.get("hideIcon"), bool)
                    or not isinstance(link.get("isOncePlayEffect"), bool)
                ):
                    _reject(
                        "plan2-card-history-master-effect-link-invalid", str(index)
                    )
                ordered.append(effect_id)
                required_slots.append(not trigger_id)
            if tuple(ordered) != program_effect_ids:
                _reject(
                    "plan2-card-history-program-effect-order-mismatch",
                    f"master={tuple(ordered)!r};program={program_effect_ids!r}",
                )
            if not any(required_slots):
                _reject("plan2-card-history-direct-effect-list-empty")
            specs: list[_MasterEffectSpec] = []
            for effect_id, required in zip(
                ordered, required_slots, strict=True
            ):
                rows = tuple(
                    connection.execute(
                        "SELECT raw_json FROM effect WHERE id = ?", (effect_id,)
                    )
                )
                if len(rows) != 1:
                    _reject(
                        "plan2-card-history-master-effect-not-unique",
                        f"{effect_id}:{len(rows)}",
                    )
                raw = json.loads(str(rows[0]["raw_json"]))
                if not isinstance(raw, Mapping):
                    _reject("plan2-card-history-master-effect-invalid", effect_id)
                # Requiredness belongs to the ordered play-effect slot, not
                # the effect ID.  Master legitimately reuses one effect ID in
                # an unconditional slot and a later triggered slot; reducing
                # this to an ID set incorrectly makes the triggered duplicate
                # mandatory in every retained command queue.
                specs.append(_MasterEffectSpec(effect_id, raw, required))
    except _ReplayRejected:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as error:
        _reject(
            "plan2-card-history-master-unavailable",
            f"{type(error).__name__}:{error}",
        )
    return program_effect_ids, tuple(specs)


def _load_master_effect_spec(
    effect_id: str,
    *,
    required: bool,
    database: Path,
) -> _MasterEffectSpec:
    """Load one exact Master effect added by runtime customization."""

    try:
        with closing(
            sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)
        ) as connection:
            rows = tuple(
                connection.execute(
                    "SELECT raw_json FROM effect WHERE id = ?", (effect_id,)
                )
            )
        if len(rows) != 1:
            _reject(
                "plan2-card-history-master-effect-not-unique",
                f"{effect_id}:{len(rows)}",
            )
        raw = json.loads(str(rows[0][0]))
        if not isinstance(raw, Mapping):
            _reject("plan2-card-history-master-effect-invalid", effect_id)
    except _ReplayRejected:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError) as error:
        _reject(
            "plan2-card-history-master-unavailable",
            f"{type(error).__name__}:{error}",
        )
    return _MasterEffectSpec(effect_id, raw, required)


def _materialize_master_effects(
    program: Plan2NativeCardProgram,
    selected: NativeOrderedCardInstance,
    *,
    database: Path,
) -> tuple[tuple[str, ...], tuple[_MasterEffectSpec, ...]]:
    """Project the shared Plan2 runtime customization contract to commands.

    Native retained history serializes the same effective Playing operations
    that the horizon executes.  Build its exact command specs from that shared
    operation materializer instead of independently interpreting grow types.
    """

    executor = program.native_playing_executor
    if not isinstance(executor, Plan2MasterPlayingExecutor):
        _reject(
            "plan2-card-history-master-playing-executor-required",
            program.native_playing_executor_id,
        )
    customization = executor.runtime_customization_contract
    if customization is None:
        _reject("plan2-card-history-runtime-customization-not-materialized")
    operations = executor.operations

    added_effect_ids = tuple(
        entry.added_effect.id
        for entry in customization.effects
        if entry.added_effect is not None
    )
    base_operation_count = len(executor.operations) - len(added_effect_ids)
    if base_operation_count < 1:
        _reject("plan2-card-history-runtime-customization-operation-mismatch")
    static_executor_view = replace(
        executor,
        operations=executor.operations[:base_operation_count],
        operation_effect_ids=executor.operation_effect_ids[:base_operation_count],
        operation_once_flags=executor.operation_once_flags[:base_operation_count],
        runtime_customization_contract=None,
        runtime_customization_trace=(),
    )
    base_effect_ids, base_specs = _load_master_effects(
        replace(program, native_playing_executor=static_executor_view),
        database=database,
    )
    effect_ids = (*base_effect_ids, *added_effect_ids)
    if len(effect_ids) != len(operations):
        _reject(
            "plan2-card-history-runtime-customization-operation-mismatch",
            f"effects={len(effect_ids)};operations={len(operations)}",
        )

    specs: list[_MasterEffectSpec] = []
    for index, (effect_id, operation) in enumerate(
        zip(effect_ids, operations, strict=True)
    ):
        spec = (
            base_specs[index]
            if index < len(base_specs)
            else _load_master_effect_spec(
                effect_id,
                required=True,
                database=database,
            )
        )
        unwrapped = (
            operation.operation
            if isinstance(operation, Plan2MasterTriggeredOperation)
            else operation
        )
        if isinstance(unwrapped, Plan2MasterScalarOperation):
            if unwrapped.effect_id != effect_id:
                _reject(
                    "plan2-card-history-runtime-customization-effect-mismatch",
                    f"{effect_id}:{unwrapped.effect_id}",
                )
            raw = dict(spec.raw)
            raw.update(
                {
                    "effectValue1": unwrapped.value1,
                    "effectValue2": unwrapped.value2,
                    "effectCount": unwrapped.count,
                    "effectTurn": unwrapped.turn,
                }
            )
            spec = _MasterEffectSpec(spec.effect_id, raw, spec.required)
        specs.append(spec)
    return tuple(effect_ids), tuple(specs)


def _serialized_effect_matches(
    serialized: object,
    spec: _MasterEffectSpec,
) -> bool:
    if not isinstance(serialized, Mapping) or set(serialized) != _PLAY_EFFECT_FIELDS:
        return False
    raw = spec.raw
    effect_type = raw.get("effectType")
    command_type = _COMMAND_EFFECT_TYPE_BY_MASTER.get(effect_type)
    effect_groups = raw.get("effectGroupIds")
    move_position = _COMMAND_MOVE_POSITION_BY_MASTER.get(
        raw.get("movePositionType")
    )
    pick_range = _COMMAND_PICK_RANGE_BY_MASTER.get(raw.get("pickRangeType"))
    pick_range2 = _COMMAND_PICK_RANGE_BY_MASTER.get(raw.get("pickRangeType2"))
    pick_count_type = _COMMAND_PICK_COUNT_BY_MASTER.get(raw.get("pickCountType"))
    pick_count_type2 = _COMMAND_PICK_COUNT_BY_MASTER.get(raw.get("pickCountType2"))
    target_effect_type = (
        0
        if raw.get("targetExamEffectType") == "ProduceExamEffectType_Unknown"
        else _COMMAND_EFFECT_TYPE_BY_MASTER.get(raw.get("targetExamEffectType"))
    )
    if (
        command_type is None
        or raw.get("id") != spec.effect_id
        or move_position is None
        or pick_range is None
        or pick_range2 is None
        or pick_count_type is None
        or pick_count_type2 is None
        or target_effect_type is None
        or raw.get("produceCardStatusEnchantId") != ""
        or not isinstance(effect_groups, list)
        or any(not isinstance(value, str) or not value for value in effect_groups)
    ):
        return False
    return bool(
        serialized.get("_id") == spec.effect_id
        and serialized.get("_effectType") == command_type
        and serialized.get("_effectValue1") == raw.get("effectValue1")
        and serialized.get("_effectValue2") == raw.get("effectValue2")
        and serialized.get("_effectCount") == raw.get("effectCount")
        and serialized.get("_effectTurn") == raw.get("effectTurn")
        and serialized.get("_effectGroupIdList") == effect_groups
        and serialized.get("_targetProduceCardId") == raw.get("targetProduceCardId")
        and serialized.get("_targetUpgradeCount") == raw.get("targetUpgradeCount")
        and serialized.get("_targetExamEffectType") == target_effect_type
        and serialized.get("_cardSearchId") == raw.get("produceCardSearchId")
        and serialized.get("_cardSearchId2") == raw.get("produceCardSearchId2")
        and serialized.get("_movePositionType") == move_position
        and serialized.get("_pickRangeType") == pick_range
        and serialized.get("_pickRangeType2") == pick_range2
        and serialized.get("_pickCountReferenceProduceCardSearchId")
        == raw.get("pickCountReferenceProduceCardSearchId")
        and serialized.get("_pickCountReferenceProduceCardSearchId2")
        == raw.get("pickCountReferenceProduceCardSearchId2")
        and serialized.get("_pickCountType") == pick_count_type
        and serialized.get("_pickCountType2") == pick_count_type2
        and serialized.get("_pickCount") == raw.get("pickCount", 0)
        and serialized.get("_pickCountMin") == raw.get("pickCountMin")
        and serialized.get("_pickCountMax") == raw.get("pickCountMax")
        and serialized.get("_pickCountMin2") == raw.get("pickCountMin2")
        and serialized.get("_pickCountMax2") == raw.get("pickCountMax2")
        and serialized.get("_judgeTargetIndex") == raw.get("judgeTargetIndex", 0)
        and serialized.get("_chainEffectId")
        == raw.get("chainProduceExamEffectId")
        and serialized.get("_chainEffectIdList")
        == raw.get("chainProduceExamEffectIds")
        and serialized.get("_statusEnchantId")
        == raw.get("produceExamStatusEnchantId")
        and serialized.get("_cardGrowEffectIdList")
        == raw.get("produceCardGrowEffectIds")
    )


def _command_has_neutral_context(command: Mapping[str, object]) -> bool:
    enchant_uid = command.get("_enchantEffectUid")
    playing_item = command.get("_playingItem")
    play_effect = command.get("_playEffect")
    origin_effect_index = command.get("_originEffectIndex")
    listener_presentation_item = bool(
        isinstance(enchant_uid, int)
        and not isinstance(enchant_uid, bool)
        and enchant_uid > 0
        and isinstance(playing_item, Mapping)
        and set(playing_item) == set(_EMPTY_PLAYING_ITEM)
        and isinstance(playing_item.get("_id"), str)
        and isinstance(playing_item.get("_itemType"), int)
        and not isinstance(playing_item.get("_itemType"), bool)
        and playing_item.get("_itemType", -1) >= 0
        and isinstance(playing_item.get("_parentCustomItemIds"), list)
        and all(
            isinstance(value, str) and value
            for value in playing_item.get("_parentCustomItemIds", [])
        )
        and isinstance(playing_item.get("_reactionCount"), int)
        and not isinstance(playing_item.get("_reactionCount"), bool)
        and playing_item.get("_reactionCount", -1) >= 0
        and isinstance(playing_item.get("_fireCount"), int)
        and not isinstance(playing_item.get("_fireCount"), bool)
        and playing_item.get("_fireCount", -1) >= 0
    )
    return bool(
        set(command) == _COMMAND_FIELDS
        and isinstance(play_effect, Mapping)
        and command.get("_assetId") == ""
        and command.get("_cardGuids") == []
        and command.get("_cardSelectSearchId")
        == play_effect.get("_cardSearchId")
        and command.get("_cardSelectSearchId2")
        == play_effect.get("_cardSearchId2")
        and command.get("_effectTriggerId") == ""
        # TriggerEffectStatusEffect wraps an already accepted card with
        # playType=10 commands and identifies the active listener here.  The
        # UID is validated against the LocalSave status graph below; requiring
        # zero globally incorrectly rejects ordinary listener execution.
        and isinstance(enchant_uid, int)
        and not isinstance(enchant_uid, bool)
        and command.get("_isCardSelect") is False
        and command.get("_isCardSelect2") is False
        and command.get("_isConsumeCost") is False
        and command.get("_isManual") is False
        and command.get("_isPlayingGimmick") is False
        and command.get("_isPlayingMoveCardEffect") is False
        # This is a presentation/forecast hint, not an execution context.
        # Native CardDraw commands serialize it as true while the surrounding
        # card/effect/GUID/cost grammar remains identical.
        and isinstance(command.get("_isSkipForCalculateForecast"), bool)
        # Listener children retain their Master effect-slot index.  Direct
        # card commands remain index zero; the active listener/status graph
        # below binds a non-zero index to the exact serialized child.
        and isinstance(origin_effect_index, int)
        and not isinstance(origin_effect_index, bool)
        and origin_effect_index >= 0
        and (origin_effect_index == 0 or listener_presentation_item)
        and command.get("_phaseType") == 0
        and command.get("_playIndex") == 0
        and command.get("_playingDrink") == _EMPTY_PLAYING_DRINK
        and command.get("_playingEnchantIsTriggerActive") is False
        and command.get("_playingGimmick") == _EMPTY_PLAYING_GIMMICK
        and (
            command.get("_playingItem") == _EMPTY_PLAYING_ITEM
            or listener_presentation_item
        )
        and command.get("_selectIndex") == []
        and command.get("_startEnchantOriginId") == ""
        and command.get("_startEnchantOriginLevel") == 0
        and command.get("_startEnchantOriginType") == 0
        and command.get("_startEnchantOwnerId") == ""
        and command.get("_supportCardIds") == []
        and command.get("_triggeredGrowEffectAffectLists") == []
        and command.get("_useEffectUidList") == []
    )


def _active_trigger_effects_by_uid(
    state: LocalSaveExamState,
) -> dict[
    int,
    tuple[
        tuple[Mapping[str, object], ...],
        Mapping[str, object] | None,
        str,
        bool,
    ],
]:
    runtime = state.root_runtime
    if runtime is None:
        _reject("plan2-card-history-trigger-root-missing")
    opaque = runtime.opaque_fields.to_value()
    if not isinstance(opaque, Mapping):
        _reject("plan2-card-history-trigger-root-invalid")
    status = opaque.get("status")
    references = opaque.get("references")
    if not isinstance(status, Mapping) or not isinstance(references, Mapping):
        _reject("plan2-card-history-trigger-status-graph-invalid")
    active = status.get("_effectList")
    entries = references.get("RefIds")
    if not isinstance(active, list) or not isinstance(entries, list):
        _reject("plan2-card-history-trigger-status-graph-invalid")
    active_rids = {
        link.get("rid")
        for link in active
        if isinstance(link, Mapping)
        and set(link) == {"rid"}
        and isinstance(link.get("rid"), int)
        and not isinstance(link.get("rid"), bool)
    }
    result: dict[
        int,
        tuple[
            tuple[Mapping[str, object], ...],
            Mapping[str, object] | None,
            str,
            bool,
        ],
    ] = {}
    for entry in entries:
        if not isinstance(entry, Mapping) or entry.get("rid") not in active_rids:
            continue
        type_value = entry.get("type")
        data = entry.get("data")
        if (
            not isinstance(type_value, Mapping)
            or type_value.get("class") != "TriggerEffectStatusEffect"
            or not isinstance(data, Mapping)
        ):
            continue
        uid = data.get("_uid")
        effects = data.get("_effectList")
        trigger_item = data.get("_triggerItem")
        enchantment_id = data.get("_statusEnchantId")
        is_item_direct_enchant = data.get("_isItemDirectEnchant")
        if (
            isinstance(uid, bool)
            or not isinstance(uid, int)
            or uid <= 0
            or not isinstance(effects, list)
            or not all(isinstance(value, Mapping) for value in effects)
            or not isinstance(enchantment_id, str)
            or type(is_item_direct_enchant) is not bool
            or uid in result
        ):
            _reject("plan2-card-history-trigger-status-invalid")
        if trigger_item is not None and (
            not isinstance(trigger_item, Mapping)
            or set(trigger_item) != set(_EMPTY_PLAYING_ITEM)
        ):
            _reject("plan2-card-history-trigger-status-invalid")
        result[uid] = (
            tuple(effects),
            None if trigger_item is None else trigger_item,
            enchantment_id,
            is_item_direct_enchant,
        )
    return result


def _listener_presentation_item_matches(
    serialized: object,
    trigger_item: Mapping[str, object] | None,
) -> bool:
    if not isinstance(serialized, Mapping):
        return False
    neutral_identity = bool(
        set(serialized) == set(_EMPTY_PLAYING_ITEM)
        and serialized.get("_id") == ""
        and serialized.get("_itemType") == 0
        and serialized.get("_parentCustomItemIds") == []
        and isinstance(serialized.get("_reactionCount"), int)
        and not isinstance(serialized.get("_reactionCount"), bool)
        and serialized.get("_reactionCount", -1) >= 0
        and isinstance(serialized.get("_fireCount"), int)
        and not isinstance(serialized.get("_fireCount"), bool)
        and serialized.get("_fireCount", -1) >= 0
    )
    return neutral_identity or (
        trigger_item is not None and dict(serialized) == dict(trigger_item)
    )


def _serialized_listener_effect_matches(
    serialized: object,
    listener_effect: Mapping[str, object],
) -> bool:
    if not isinstance(serialized, Mapping) or set(serialized) != _PLAY_EFFECT_FIELDS:
        return False
    # The LocalSave status graph and command queue are the two native
    # serializations of the same ProduceEffect.  Identity and all executable
    # scalar/cardinality fields are sufficient here; selector fields remain
    # protected by the command's active listener UID and the settled card GUID.
    defaults: Mapping[str, object] = {
        "_effectValue1": 0,
        "_effectValue2": 0,
        "_effectCount": 0,
        "_effectTurn": 0,
        "_statusEnchantId": "",
        "_chainEffectId": "",
    }
    return bool(
        isinstance(listener_effect.get("_id"), str)
        and listener_effect.get("_id")
        and serialized.get("_id") == listener_effect.get("_id")
        and serialized.get("_effectType") == listener_effect.get("_effectType")
        and all(
            serialized.get(key) == listener_effect.get(key, default)
            for key, default in defaults.items()
        )
    )


def _prove_command_queue(
    state: LocalSaveExamState,
    *,
    specs: tuple[_MasterEffectSpec, ...],
    expected_playing: Mapping[str, object],
    before_plays: int,
    destination: str,
) -> tuple[
    tuple[str, ...],
    int,
    tuple[tuple[int, str, str, tuple[tuple[str, int], ...], bool], ...],
]:
    runtime = state.root_runtime
    if runtime is None:
        _reject("plan2-card-history-transition-root-missing")
    raw_commands = runtime.command_list.to_value()
    if not isinstance(raw_commands, list) or not all(
        isinstance(value, Mapping) for value in raw_commands
    ):
        _reject("plan2-card-history-command-list-invalid")
    commands = tuple(raw_commands)
    if len(commands) < 5:
        _reject(
            "plan2-card-history-command-count-mismatch",
            f"minimum=5;actual={len(commands)}",
        )
    if any(not _command_has_neutral_context(value) for value in commands):
        _reject("plan2-card-history-command-context-unsupported")
    if any(set(value.get("_playingCard", {})) != _RAW_CARD_FIELDS for value in commands):
        _reject("plan2-card-history-command-playing-card-mismatch")

    start = commands[0]
    middle_commands = commands[1:-3]
    after_effect, move, terminal = commands[-3:]
    if destination not in {"grave", "lost"}:
        _reject("plan2-card-history-destination-unsupported", destination)
    if (
        start.get("_enchantEffectUid") != 0
        or after_effect.get("_enchantEffectUid") != 0
        or move.get("_enchantEffectUid") != 0
        or terminal.get("_enchantEffectUid") != 0
        or start.get("_playType") != 9
        or start.get("_isSeparateStart") is not True
        or start.get("_isUsePlayableCardCount") is not False
        or start.get("_playCardPositionType") != 0
        or start.get("_playEffect") != _EMPTY_PLAY_EFFECT
        or after_effect.get("_playType") != 13
        or after_effect.get("_isSeparateStart") is not False
        or after_effect.get("_isUsePlayableCardCount") is not False
        or after_effect.get("_playCardPositionType") != 0
        or after_effect.get("_playEffect") != _EMPTY_PLAY_EFFECT
        or move.get("_playType") != 6
        or move.get("_isSeparateStart") is not False
        or move.get("_isUsePlayableCardCount") is not True
        # MovePlayCard serializes the card's current/origin position.  A card
        # accepted from Hand therefore remains enum 2 whether Master sends it
        # to Grave or Lost.  The actual destination is independently proven
        # by the compiled program and the logical ordered-zone transition.
        or move.get("_playCardPositionType") != 2
        or move.get("_playEffect") != _EMPTY_PLAY_EFFECT
        or terminal.get("_playType") != 9
        or terminal.get("_isSeparateStart") is not False
        or terminal.get("_isUsePlayableCardCount") is not False
        or terminal.get("_playCardPositionType") != 0
        or terminal.get("_playEffect") != _EMPTY_PLAY_EFFECT
    ):
        _reject("plan2-card-history-command-grammar-mismatch")

    active_listener_effects = _active_trigger_effects_by_uid(state)
    effect_commands: list[Mapping[str, object]] = []
    active_listener_uid: int | None = None
    active_listener_trigger_item: Mapping[str, object] | None = None
    active_listener_enchantment_id = ""
    active_listener_is_direct_item = False
    active_listener_children: list[tuple[str, int]] = []
    native_item_groups: list[
        tuple[int, str, str, tuple[tuple[str, int], ...], bool]
    ] = []
    listener_effect_cursor = 0
    listener_effect_count = 0
    for command in middle_commands:
        play_type = command.get("_playType")
        enchant_uid = command.get("_enchantEffectUid")
        if play_type == 10:
            raw_card = command.get("_playingCard")
            if not isinstance(raw_card, Mapping) or raw_card.get("_guid") != "":
                _reject("plan2-card-history-listener-playing-card-invalid")
            if command.get("_playEffect") != _EMPTY_PLAY_EFFECT:
                _reject("plan2-card-history-listener-command-invalid")
            if command.get("_isSeparateStart") is True:
                if (
                    active_listener_uid is not None
                    or not isinstance(enchant_uid, int)
                    or isinstance(enchant_uid, bool)
                    or enchant_uid not in active_listener_effects
                ):
                    _reject("plan2-card-history-listener-command-invalid")
                (
                    listener_effects,
                    listener_trigger_item,
                    listener_enchantment_id,
                    listener_is_direct_item,
                ) = (
                    active_listener_effects[enchant_uid]
                )
                if not listener_effects or not _listener_presentation_item_matches(
                    command.get("_playingItem"),
                    listener_trigger_item,
                ):
                    _reject("plan2-card-history-listener-command-invalid")
                active_listener_uid = enchant_uid
                active_listener_trigger_item = listener_trigger_item
                active_listener_enchantment_id = listener_enchantment_id
                active_listener_is_direct_item = listener_is_direct_item
                active_listener_children = []
                listener_effect_cursor = 0
                listener_effect_count = 0
            else:
                if active_listener_uid is None or enchant_uid != 0:
                    _reject("plan2-card-history-listener-command-invalid")
                if listener_effect_count == 0:
                    _reject("plan2-card-history-listener-effect-missing")
                listener_effects = active_listener_effects[active_listener_uid][0]
                expected_children = tuple(
                    (str(effect.get("_id", "")), index)
                    for index, effect in enumerate(listener_effects)
                )
                item_id = (
                    ""
                    if active_listener_trigger_item is None
                    else str(active_listener_trigger_item.get("_id", ""))
                )
                if active_listener_is_direct_item:
                    native_item_groups.append(
                        (
                            active_listener_uid,
                            item_id,
                            active_listener_enchantment_id,
                            tuple(active_listener_children),
                            bool(
                                item_id
                                and active_listener_enchantment_id
                                and tuple(active_listener_children)
                                == expected_children
                            ),
                        )
                    )
                active_listener_uid = None
                active_listener_trigger_item = None
                active_listener_enchantment_id = ""
                active_listener_is_direct_item = False
                active_listener_children = []
            continue
        if play_type != 5 or command.get("_isSeparateStart") is not False:
            _reject("plan2-card-history-command-grammar-mismatch")
        if active_listener_uid is None:
            if enchant_uid != 0:
                _reject("plan2-card-history-listener-command-invalid")
            if command.get("_playingCard") != expected_playing:
                _reject("plan2-card-history-command-playing-card-mismatch")
            effect_commands.append(command)
            continue
        raw_card = command.get("_playingCard")
        if not isinstance(raw_card, Mapping) or raw_card.get("_guid") != "":
            _reject("plan2-card-history-listener-playing-card-invalid")
        if enchant_uid != active_listener_uid:
            _reject("plan2-card-history-listener-command-invalid")
        if not _listener_presentation_item_matches(
            command.get("_playingItem"),
            active_listener_trigger_item,
        ):
            _reject("plan2-card-history-listener-command-invalid")
        listener_effects = active_listener_effects[active_listener_uid][0]
        match = next(
            (
                index
                for index in range(listener_effect_cursor, len(listener_effects))
                if _serialized_listener_effect_matches(
                    command.get("_playEffect"), listener_effects[index]
                )
            ),
            None,
        )
        if match is None:
            _reject("plan2-card-history-listener-effect-mismatch")
        if command.get("_originEffectIndex") != match:
            _reject("plan2-card-history-listener-effect-mismatch")
        effect_id = command.get("_playEffect", {}).get("_id")
        if not isinstance(effect_id, str) or not effect_id:
            _reject("plan2-card-history-listener-effect-mismatch")
        active_listener_children.append((effect_id, match))
        listener_effect_cursor = match + 1
        listener_effect_count += 1
    if active_listener_uid is not None:
        _reject("plan2-card-history-listener-command-unclosed")
    if any(
        command.get("_playingCard") != expected_playing
        for command in (start, after_effect, move, terminal)
    ):
        _reject("plan2-card-history-command-playing-card-mismatch")
    # The client serializes only effect slots whose trigger fired.  Match the
    # observed commands to an ordered subsequence of the complete Master card
    # effect list, while still requiring every unconditional slot.  This keeps
    # exact effect identity/order without demanding a fixed command count.
    matched_indices: list[int] = []
    cursor = 0
    for command in effect_commands:
        if (
            command.get("_playType") != 5
            or command.get("_isSeparateStart") is not False
            or command.get("_isUsePlayableCardCount") is not False
            or command.get("_playCardPositionType") != 0
        ):
            _reject("plan2-card-history-command-grammar-mismatch")
        match = next(
            (
                index
                for index in range(cursor, len(specs))
                if _serialized_effect_matches(
                    command.get("_playEffect"),
                    specs[index],
                )
            ),
            None,
        )
        if match is None:
            _reject("plan2-card-history-command-grammar-mismatch")
        matched_indices.append(match)
        cursor = match + 1
    if any(
        spec.required and index not in matched_indices
        for index, spec in enumerate(specs)
    ):
        _reject("plan2-card-history-command-grammar-mismatch")
    remaining_values = tuple(
        command.get("_remainCanPlayCardCount") for command in commands
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        for value in remaining_values
    ):
        _reject(
            "plan2-card-history-command-remaining-play-mismatch",
            f"serialized={remaining_values!r};before={before_plays}",
        )
    # This is a command-construction snapshot, not the settled play counter.
    # A drink/item/status may add playability between the logical root and a
    # later direct-effect row, so a value can legitimately exceed
    # ``before_plays``.  The completed replay below derives the next count from
    # the typed horizon and stable same-turn evidence; these serialized values
    # only need to remain non-negative integers.
    return (
        tuple(specs[index].effect_id for index in matched_indices),
        remaining_values[-1],
        tuple(native_item_groups),
    )


def _expected_post_cost(
    before: Plan2NativeHorizonState,
    program: Plan2NativeCardProgram,
) -> Plan2CostResources:
    native_cost = program.native_cost
    if native_cost is None:
        if program.cost_type == "none":
            return Plan2CostResources(
                stamina=before.scalar.stamina,
                max_stamina=before.scalar.max_stamina,
                block=before.scalar.block,
                review=before.scalar.review,
                motivation=before.scalar.card_play_aggressive,
                review_exists=before.scalar.review > 0,
                review_consumption_sum=before.review_consumption_sum,
            )
        native_cost = Plan2NativeCardCost(
            card_id=program.card_id,
            upgrade=program.upgrade,
            cost_type=COST_STAMINA,
            base_cost=program.cost_value,
            stamina=program.cost_value,
        )
    resources = Plan2CostResources(
        stamina=before.scalar.stamina,
        max_stamina=before.scalar.max_stamina,
        block=before.scalar.block,
        review=before.scalar.review,
        motivation=before.scalar.card_play_aggressive,
        review_exists=before.scalar.review > 0,
        review_consumption_sum=before.review_consumption_sum,
    )
    payment = pay_plan2_native_cost(
        Plan2NativeCostRequest(
            cost=native_cost,
            resources=resources,
            modifiers=before.stamina_modifiers,
            origin=PlayOrigin.NORMAL,
        )
    )
    if not payment.committed:
        _reject(
            "plan2-card-history-cost-payment-rejected",
            payment.predicate.reason or native_cost.cost_type,
        )
    return payment.resources_after


def _retained_prior_drink_materialized_scalar(
    old: LocalSaveExamState,
    new: LocalSaveExamState,
    *,
    expected_drink_id: str,
    current_card_id: str,
) -> tuple[int, int, int] | None:
    """Prove one retained DRINK log materialized before the current PLAY.

    Native can retain an empty drink user-log row, then fill that exact row and
    append the current card's still-empty row in the same save that publishes
    the card's post-cost command queue.  Only explicit score/stamina/block log
    lines are folded here; any other detail shape remains fail-closed.
    """

    def rows(state: LocalSaveExamState) -> list[object] | None:
        runtime = state.root_runtime
        if runtime is None:
            return None
        opaque = runtime.opaque_fields.to_value()
        value = opaque.get("userPlayLogList") if isinstance(opaque, Mapping) else None
        return value if isinstance(value, list) else None

    def flattened_lines(row: Mapping[str, object]) -> list[object] | None:
        details = row.get("_detailList")
        if not isinstance(details, list):
            return None
        result: list[object] = []
        for detail in details:
            if not isinstance(detail, Mapping):
                return None
            lines = detail.get("_detailLineList")
            if not isinstance(lines, list):
                return None
            result.extend(lines)
        return result

    old_rows = rows(old)
    new_rows = rows(new)
    if (
        old_rows is None
        or new_rows is None
        or not old_rows
        or len(new_rows) != len(old_rows) + 1
        or old_rows[:-1] != new_rows[:-2]
        or not isinstance(old_rows[-1], Mapping)
        or not isinstance(new_rows[-2], Mapping)
        or not isinstance(new_rows[-1], Mapping)
    ):
        return None
    pending = old_rows[-1]
    materialized = new_rows[-2]
    current = new_rows[-1]
    pending_without_details = {
        key: value for key, value in pending.items() if key != "_detailList"
    }
    materialized_without_details = {
        key: value for key, value in materialized.items() if key != "_detailList"
    }
    pending_lines = flattened_lines(pending)
    materialized_lines = flattened_lines(materialized)
    current_lines = flattened_lines(current)
    if (
        pending.get("_cellType") != 4
        or pending.get("_triggerId") != expected_drink_id
        or pending_without_details != materialized_without_details
        or pending_lines != []
        or not materialized_lines
        or current.get("_cellType") != 3
        or current.get("_triggerId") != current_card_id
        or current_lines != []
    ):
        return None

    values = {1: old.stamina, 2: old.block, 3: old.score}
    observed_line = False
    for raw_line in materialized_lines:
        if not isinstance(raw_line, Mapping):
            return None
        line_type = raw_line.get("_detailLineType")
        effect_type = raw_line.get("_effectType")
        before_value = raw_line.get("_before")
        after_value = raw_line.get("_after")
        if (
            isinstance(line_type, bool)
            or not isinstance(line_type, int)
            or line_type not in {1, 2, 3}
            or effect_type != 0
            or isinstance(before_value, bool)
            or not isinstance(before_value, int)
            or isinstance(after_value, bool)
            or not isinstance(after_value, int)
            or before_value < 0
            or after_value < 0
            or values[line_type] != before_value
        ):
            return None
        values[line_type] = after_value
        observed_line = True
    if not observed_line:
        return None
    return values[3], values[1], values[2]


def _active_status_graph(
    state: LocalSaveExamState,
) -> tuple[
    object,
    tuple[
        tuple[int, Mapping[str, object], Mapping[str, object]],
        ...,
    ],
]:
    runtime = state.root_runtime
    if runtime is None:
        _reject("plan2-card-history-item-root-missing")
    opaque = runtime.opaque_fields.to_value()
    if not isinstance(opaque, Mapping):
        _reject("plan2-card-history-item-root-invalid")
    status = opaque.get("status")
    references = opaque.get("references")
    if not isinstance(status, Mapping) or not isinstance(references, Mapping):
        _reject("plan2-card-history-item-status-graph-invalid")
    active = status.get("_effectList")
    entries = references.get("RefIds")
    if not isinstance(active, list) or not isinstance(entries, list):
        _reject("plan2-card-history-item-status-graph-invalid")
    by_rid: dict[
        int,
        tuple[Mapping[str, object], Mapping[str, object]],
    ] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            _reject("plan2-card-history-item-reference-invalid")
        rid = entry.get("rid")
        type_value = entry.get("type")
        data = entry.get("data")
        if (
            isinstance(rid, bool)
            or not isinstance(rid, int)
            or not isinstance(type_value, Mapping)
            or not isinstance(data, Mapping)
        ):
            _reject("plan2-card-history-item-reference-invalid")
        class_name = type_value.get("class")
        if not isinstance(class_name, str):
            _reject("plan2-card-history-item-reference-invalid")
        if rid in by_rid:
            _reject("plan2-card-history-item-reference-duplicate", str(rid))
        by_rid[rid] = (type_value, data)
    statuses: list[
        tuple[int, Mapping[str, object], Mapping[str, object]]
    ] = []
    for link in active:
        if not isinstance(link, Mapping) or set(link) != {"rid"}:
            _reject("plan2-card-history-item-active-link-invalid")
        rid = link.get("rid")
        if isinstance(rid, bool) or not isinstance(rid, int) or rid not in by_rid:
            _reject("plan2-card-history-item-active-reference-missing", str(rid))
        type_value, data = by_rid[rid]
        statuses.append((rid, type_value, data))
    return opaque.get("itemList"), tuple(statuses)


def _active_trigger_statuses(
    state: LocalSaveExamState,
) -> tuple[object, tuple[tuple[int, Mapping[str, object]], ...]]:
    raw_items, active = _active_status_graph(state)
    statuses: list[tuple[int, Mapping[str, object]]] = []
    for rid, type_value, data in active:
        class_name = type_value.get("class")
        if (
            class_name == "TriggerEffectStatusEffect"
            and data.get("_isItemDirectEnchant") is True
        ):
            statuses.append((rid, data))
    return raw_items, tuple(statuses)


def _parse_native_pitem_context(
    raw: object,
    *,
    label: str,
) -> dict[str, object]:
    """Decode one item-dispatch evidence record without coercing values.

    ``dispatch_plan2_native_item_event`` writes this payload next to the
    native event line.  Journal replay must bind the same positional fields
    and search observations; scraping a localized card description or
    defaulting a missing counter to zero would make an unsupported listener
    appear to have fired.
    """

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            _reject(
                "plan2-card-history-item-context-invalid",
                f"{label}:json:{error}",
            )
    if not isinstance(raw, Mapping):
        _reject("plan2-card-history-item-context-invalid", f"{label}:object")
    allowed = {
        "phase_values",
        "field_values",
        "field_search_values",
        "card_search_matches",
        "card_search_counts",
        "effect_types",
        "card_move_position_type",
        "lesson_type",
        "remaining_turn",
        "stamina_multiple",
        "aggressive",
        "current_stamina",
        "max_stamina",
        "card_category",
        "card_effect_group_ids",
    }
    if any(not isinstance(key, str) or key not in allowed for key in raw):
        _reject("plan2-card-history-item-context-invalid", f"{label}:keys")

    def strict_int(value: object, path: str, *, minimum: int | None = None) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            _reject("plan2-card-history-item-context-invalid", f"{label}:{path}:int")
        if minimum is not None and value < minimum:
            _reject("plan2-card-history-item-context-invalid", f"{label}:{path}:range")
        return value

    def strict_scalar(value: object, path: str) -> int | bool:
        if type(value) is bool:
            return value
        return strict_int(value, path)

    phase_values_raw = raw.get("phase_values", [])
    if not isinstance(phase_values_raw, list):
        _reject("plan2-card-history-item-context-invalid", f"{label}:phase_values")
    phase_values = tuple(
        strict_int(value, f"phase_values[{index}]", minimum=0)
        for index, value in enumerate(phase_values_raw)
    )

    field_values_raw = raw.get("field_values", {})
    if not isinstance(field_values_raw, Mapping):
        _reject("plan2-card-history-item-context-invalid", f"{label}:field_values")
    field_values: dict[str, int | bool] = {}
    for key, value in field_values_raw.items():
        if not isinstance(key, str) or not key:
            _reject("plan2-card-history-item-context-invalid", f"{label}:field-key")
        field_values[key] = strict_scalar(value, f"field_values.{key}")

    def parse_pair_map(name: str) -> dict[tuple[str, str], int | bool]:
        entries = raw.get(name, [])
        if not isinstance(entries, list):
            _reject("plan2-card-history-item-context-invalid", f"{label}:{name}")
        result: dict[tuple[str, str], int | bool] = {}
        for index, entry in enumerate(entries):
            if (
                not isinstance(entry, list)
                or len(entry) != 2
                or not isinstance(entry[0], list)
                or len(entry[0]) != 2
                or not all(isinstance(value, str) and value for value in entry[0])
            ):
                _reject(
                    "plan2-card-history-item-context-invalid",
                    f"{label}:{name}[{index}]:key",
                )
            key = (entry[0][0], entry[0][1])
            if key in result:
                _reject(
                    "plan2-card-history-item-context-invalid",
                    f"{label}:{name}[{index}]:duplicate",
                )
            result[key] = strict_scalar(entry[1], f"{name}[{index}].value")
        return result

    matches_raw = raw.get("card_search_matches", {})
    if not isinstance(matches_raw, Mapping):
        _reject("plan2-card-history-item-context-invalid", f"{label}:card_search_matches")
    matches: dict[str, bool] = {}
    for key, value in matches_raw.items():
        if not isinstance(key, str) or not key or type(value) is not bool:
            _reject("plan2-card-history-item-context-invalid", f"{label}:card-search-match")
        matches[key] = value

    effect_types_raw = raw.get("effect_types", [])
    if not isinstance(effect_types_raw, list) or any(
        not isinstance(value, str) or not value for value in effect_types_raw
    ):
        _reject("plan2-card-history-item-context-invalid", f"{label}:effect_types")
    move = raw.get("card_move_position_type", "ProduceCardMovePositionType_Unknown")
    if not isinstance(move, str) or not move:
        _reject("plan2-card-history-item-context-invalid", f"{label}:card_move_position_type")
    lesson = raw.get("lesson_type", "ProduceStepLessonType_Unknown")
    if not isinstance(lesson, str) or not lesson:
        _reject("plan2-card-history-item-context-invalid", f"{label}:lesson_type")
    card_category = raw.get("card_category", "")
    if not isinstance(card_category, str):
        _reject("plan2-card-history-item-context-invalid", f"{label}:card_category")
    card_effect_group_ids_raw = raw.get("card_effect_group_ids", [])
    if not isinstance(card_effect_group_ids_raw, list) or any(
        not isinstance(value, str) or not value
        for value in card_effect_group_ids_raw
    ):
        _reject(
            "plan2-card-history-item-context-invalid",
            f"{label}:card_effect_group_ids",
        )

    optional: dict[str, int | None] = {}
    for name in (
        "remaining_turn",
        "stamina_multiple",
        "aggressive",
        "current_stamina",
        "max_stamina",
    ):
        value = raw.get(name)
        if value is not None:
            value = strict_int(value, name, minimum=0)
        optional[name] = value
    return {
        "phase_values": phase_values,
        "field_values": field_values,
        "field_search_values": parse_pair_map("field_search_values"),
        "card_search_matches": matches,
        "card_search_counts": parse_pair_map("card_search_counts"),
        "effect_types": tuple(effect_types_raw),
        "card_move_position_type": move,
        "lesson_type": lesson,
        "card_category": card_category,
        "card_effect_group_ids": tuple(card_effect_group_ids_raw),
        **optional,
    }


def _parse_native_pitem_event_records(
    trace: Sequence[str],
    *,
    card: NativeOrderedCardInstance,
    round_number: int,
) -> tuple[Plan2NativeItemEvent, ...]:
    """Reconstruct the exact ordered item events written to a transition."""

    events: list[dict[str, object]] = []
    context_seen = False
    status_values = tuple(
        value.removeprefix("native-item-status-change:")
        for value in trace
        if value.startswith("native-item-status-change:")
    )
    for value in trace:
        if value.startswith("native-item-event:"):
            payload = value.removeprefix("native-item-event:")
            try:
                phase, card_part = payload.split(":", 1)
                card_text, review_text = card_part.rsplit(":review=", 1)
                event_card_id, upgrade_text = card_text.rsplit("@", 1)
                event_upgrade = int(upgrade_text)
                event_review = int(review_text)
            except (TypeError, ValueError) as error:
                _reject(
                    "plan2-card-history-item-event-trace-invalid",
                    f"{value!r}:{error}",
                )
            if event_upgrade < 0 or event_review < 0:
                _reject("plan2-card-history-item-event-trace-invalid", value)
            events.append(
                {
                    "phase": phase,
                    "round_number": round_number,
                    "card_id": event_card_id,
                    "card_upgrade": event_upgrade,
                    "review": event_review,
                    "context": None,
                }
            )
            context_seen = False
            continue
        if value.startswith("native-pitem-context:"):
            if not events or context_seen:
                _reject("plan2-card-history-item-context-invalid", value)
            events[-1]["context"] = _parse_native_pitem_context(
                value.removeprefix("native-pitem-context:"),
                label=f"trace[{len(events) - 1}]",
            ) if isinstance(value, str) else None
            context_seen = True

    if not events:
        _reject("plan2-card-history-item-event-review-mismatch", "events=()")
    status_indices = tuple(
        index
        for index, event in enumerate(events)
        if event["phase"] == ITEM_PHASE_STATUS_CHANGE
    )
    # One native StatusChange phase is emitted for each committed scalar
    # operation.  A card may therefore have more than one status event in a
    # single retained queue (for example CardPlayAggressive followed by
    # Review).  The old parser treated the whole trace as if it could contain
    # only one marker and then copied the first marker onto every event.  That
    # both rejected valid production queues and, worse, could bind the wrong
    # changed-effect identity to a later listener.  Keep the positional
    # one-marker-per-status-event seam exact: counts must agree and markers are
    # consumed in native trace order.
    if len(status_values) != len(status_indices):
        _reject(
            "plan2-card-history-item-status-change-event-mismatch",
            f"events={status_values!r};status_indices={status_indices!r}",
        )
    status_payloads: tuple[dict[str, object], ...] = ()
    if status_values:
        parsed_status_payloads: list[dict[str, object]] = []
        for status_value in status_values:
            try:
                changed_effect_type, tail = status_value.split(":difference=", 1)
                difference_text, tail = tail.split(":block=", 1)
                block_text, lesson_type = tail.split(":lesson=", 1)
                difference = int(difference_text)
                block = int(block_text)
            except (TypeError, ValueError) as error:
                _reject(
                    "plan2-card-history-item-status-change-event-mismatch",
                    f"{status_value!r}:{error}",
                )
            if difference < 0 or block < 0 or not changed_effect_type or not lesson_type:
                _reject(
                    "plan2-card-history-item-status-change-event-mismatch",
                    status_value,
                )
            parsed_status_payloads.append(
                {
                    "changed_effect_type": changed_effect_type,
                    "status_difference": difference,
                    "status_change_committed": True,
                    "lesson_type": lesson_type,
                    "block": block,
                }
            )
        status_payloads = tuple(parsed_status_payloads)

    card_events = tuple(
        event
        for event in events
        if event["phase"] == ITEM_PHASE_CARD_PLAY_AFTER
    )
    if card_events and tuple(
        (event["card_id"], event["card_upgrade"]) for event in card_events
    ) != ((card.card_id, card.effective_upgrade),):
        _reject(
            "plan2-card-history-item-event-review-mismatch",
            f"matches={card_events!r}",
        )
    if not card_events and not any(
        event["phase"] != ITEM_PHASE_STATUS_CHANGE for event in events
    ):
        _reject("plan2-card-history-item-event-review-mismatch", "card-event-missing")

    status_payload_by_index = dict(zip(status_indices, status_payloads, strict=True))
    result: list[Plan2NativeItemEvent] = []
    for event in events:
        kwargs = dict(event)
        context = kwargs.pop("context")
        kwargs.pop("phase", None)
        if event["phase"] == ITEM_PHASE_STATUS_CHANGE:
            status_payload = status_payload_by_index.get(len(result))
            if status_payload is None:
                _reject(
                    "plan2-card-history-item-status-change-event-mismatch",
                    "status-payload-missing",
                )
            kwargs.update(status_payload)
        if context is not None:
            kwargs.update(context)
        try:
            result.append(Plan2NativeItemEvent(event["phase"], **kwargs))
        except (TypeError, ValueError) as error:
            _reject(
                "plan2-card-history-item-event-trace-invalid",
                f"{event!r}:{error}",
            )
    return tuple(result)


def _settled_card_from_play_count_receipts(
    selected: NativeOrderedCardInstance,
    receipts: Sequence[object],
) -> NativeOrderedCardInstance:
    """Apply the exact GUID-local build/move increments emitted by transition."""

    expected = selected
    selected_receipts = tuple(
        value
        for value in receipts
        if getattr(value, "subject_id", None) == selected.guid
        and getattr(value, "operation", None)
        == CARD_PLAY_COUNT_INCREMENT_OPERATION
    )
    if not selected_receipts:
        _reject("plan2-card-history-play-count-receipt-missing")
    for receipt in selected_receipts:
        current_count = expected.runtime_state.play_count
        if (
            getattr(receipt, "before_value", None) != current_count
            or getattr(receipt, "after_value", None) != current_count + 1
        ):
            _reject("plan2-card-history-play-count-receipt-mismatch")
        expected = expected.increment_play_count()
    return expected.reset_support_upgrade()


def _prove_item_runtime(
    persisted: LocalSaveExamState,
    before: Plan2NativeHorizonState,
    after: Plan2NativeHorizonState,
    card: NativeOrderedCardInstance,
    transition: Plan2NativeTransition,
    *,
    native_item_groups: tuple[
        tuple[int, str, str, tuple[tuple[str, int], ...], bool], ...
    ] = (),
    prior_replay: Plan2CompletedLogicalReplay | None = None,
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[tuple[str, int], ...],
]:
    raw_items, active_statuses = _active_trigger_statuses(persisted)
    restored = restore_plan2_native_item_runtime(
        raw_items,
        active_statuses,
        lesson_step_type_value=(
            persisted.step_type_value if persisted.exam_type == 0 else None
        ),
    )
    if not restored.supported or restored.runtime is None:
        _reject(
            "plan2-card-history-item-runtime-unresolved",
            ",".join(restored.blockers),
        )
    # ``itemList._fireCount`` is not the listener's remaining-use counter.  A
    # completed client animation may advance it while the persisted trigger
    # status is still the pre-effect snapshot.  Keep the observed value in the
    # replay audit, but bind identity/reaction fields and the complete listener
    # grammar/counters instead of assigning an unproved use formula to it.
    def same_pre_effect_runtime(
        left,
        right,
        *,
        include_next_uid: bool = True,
    ) -> bool:
        return bool(
            tuple(
                (value.item_id, value.reaction_count)
                for value in left.sources
            )
            == tuple(
                (value.item_id, value.reaction_count)
                for value in right.sources
            )
            and left.listeners == right.listeners
            and left.proven_exhausted == right.proven_exhausted
            and (
                not include_next_uid
                or left.next_status_uid == right.next_status_uid
            )
        )

    # Native commits a matching item's trigger-progress counters while it
    # enqueues that item's exact command group.  A retained ExamSave may
    # therefore expose any exact prefix of the current action's item events
    # before the corresponding scalar/status effects have physically settled.
    # Derive those prefixes only from the parsed native command trace; a bare
    # counter decrement without a matching event remains invalid.
    dispatches = []
    item_runtime = before.item_runtime
    item_runtime_prefixes = [item_runtime]
    item_runtime_prefix_fires: list[tuple[str, ...]] = [()]
    cumulative_fires: list[str] = []
    status_dispatches: list[Plan2NativeItemDispatch] = []
    try:
        events = _parse_native_pitem_event_records(
            transition.trace,
            card=card,
            round_number=before.scalar.current_turn,
        )
        for event in events:
            current = dispatch_plan2_native_item_event(item_runtime, event)
            dispatches.append(current)
            item_runtime = current.after
            item_runtime_prefixes.append(item_runtime)
            cumulative_fires.extend(current.fired_enchantment_ids)
            item_runtime_prefix_fires.append(tuple(cumulative_fires))
            if event.phase == ITEM_PHASE_STATUS_CHANGE:
                status_dispatches.append(current)
        if not dispatches:
            _reject("plan2-card-history-item-event-review-mismatch", "events=()")
        dispatch = dispatches[-1]
    except _ReplayRejected:
        raise
    except (TypeError, ValueError) as error:
        _reject(
            "plan2-card-history-item-dispatch-failed",
            f"{type(error).__name__}:{error}",
        )

    native_item_fires_tuple = tuple(
        enchantment_id
        for _uid, _item_id, enchantment_id, _children, _complete
        in native_item_groups
    )
    native_item_groups_complete = bool(native_item_groups) and all(
        complete for _uid, _item_id, _enchantment_id, _children, complete
        in native_item_groups
    )
    runtime_matches_current_action_prefix = any(
        same_pre_effect_runtime(restored.runtime, prefix)
        and (
            index == 0
            or (
                bool(prefix_fires)
                and native_item_groups_complete
                and native_item_fires_tuple == prefix_fires
            )
        )
        for index, (prefix, prefix_fires) in enumerate(zip(
            item_runtime_prefixes,
            item_runtime_prefix_fires,
            strict=True,
        ))
    )
    retained_prior_item_lag = bool(
        not runtime_matches_current_action_prefix
        and isinstance(prior_replay, Plan2CompletedCardReplay)
        and prior_replay.fired_item_enchantment_ids
        and before.item_runtime == prior_replay.logical_after.item_runtime
        and same_pre_effect_runtime(
            restored.runtime,
            prior_replay.before.item_runtime,
        )
    )
    retained_prior_uid_lag = bool(
        not runtime_matches_current_action_prefix
        and isinstance(prior_replay, Plan2CompletedCardReplay)
        and prior_replay.fired_item_enchantment_ids
        and before.item_runtime == prior_replay.logical_after.item_runtime
        and same_pre_effect_runtime(
            restored.runtime,
            before.item_runtime,
            include_next_uid=False,
        )
        and restored.runtime.next_status_uid
        == prior_replay.before.item_runtime.next_status_uid
        and before.item_runtime.next_status_uid == before.scalar.next_status_uid
    )
    if (
        not runtime_matches_current_action_prefix
        and not retained_prior_item_lag
        and not retained_prior_uid_lag
    ):
        _reject("plan2-card-history-item-pre-effect-runtime-mismatch")
    expected_item_after = dispatch.after
    if any(value.effects for value in status_dispatches):
        if after.item_runtime.next_status_uid != after.scalar.next_status_uid:
            _reject("plan2-card-history-item-status-uid-floor-mismatch")
        expected_item_after = replace(
            expected_item_after,
            next_status_uid=after.scalar.next_status_uid,
        )
    if after.item_runtime != expected_item_after:
        _reject("plan2-card-history-item-logical-after-mismatch")
    expected_fire_trace = tuple(
        value
        for item_dispatch in dispatches
        for value in item_dispatch.trace
        if value.startswith("item-fire:")
    )
    observed_fire_trace = tuple(
        value for value in transition.trace if value.startswith("item-fire:")
    )
    if observed_fire_trace != expected_fire_trace:
        _reject("plan2-card-history-item-fire-trace-mismatch")

    for item_dispatch in status_dispatches:
        status_positions: list[int] = []
        for effect in item_dispatch.effects:
            if effect.effect_type == ITEM_EFFECT_REVIEW_ADDITIVE:
                marker = f"item-review-additive:{effect.effect_id}:installed"
            elif effect.effect_type == ITEM_EFFECT_AGGRESSIVE_ADDITIVE:
                marker = (
                    f"item-aggressive-additive:{effect.effect_id}:installed"
                )
            elif effect.effect_type == ITEM_EFFECT_LESSON_DEPEND_REVIEW:
                marker = f"item-lesson-depend-review:{effect.effect_id}:"
            elif effect.effect_type == ITEM_EFFECT_STAMINA_RECOVER_FIX:
                marker = f"item-stamina-recover-fix:{effect.effect_id}"
            else:
                _reject(
                    "plan2-card-history-item-status-change-effect-mismatch",
                    effect.effect_type,
                )
            matches = tuple(
                index
                for index, value in enumerate(transition.trace)
                if (
                    value.startswith(marker)
                    if marker.endswith(":")
                    else value == marker
                )
            )
            if len(matches) != 1:
                _reject(
                    "plan2-card-history-item-status-change-effect-trace-mismatch",
                    effect.effect_id,
                )
            status_positions.append(matches[0])
        if status_positions != sorted(status_positions):
            _reject("plan2-card-history-item-status-change-effect-order-mismatch")
    trace_positions: list[int] = []
    for item_dispatch in dispatches:
        if item_dispatch in status_dispatches:
            # StatusChange children use their typed owner-specific markers
            # above (additive, lesson-dependent Review, stamina recovery).
            # They are not emitted through the generic scalar item-effect
            # branch and therefore must not also require an impossible
            # duplicate ``item-effect:`` marker.
            continue
        for index, effect in enumerate(item_dispatch.effects):
            prefix = f"item-effect:{index}:{effect.effect_id}:{effect.effect_type}:"
            matches = tuple(
                offset
                for offset, value in enumerate(transition.trace)
                if value.startswith(prefix)
            )
            if len(matches) != 1:
                _reject("plan2-card-history-item-effect-trace-mismatch", effect.effect_id)
            trace_positions.append(matches[0])
    if trace_positions != sorted(trace_positions):
        _reject("plan2-card-history-item-effect-order-mismatch")
    return (
        tuple(
            enchantment_id
            for item_dispatch in dispatches
            for enchantment_id in item_dispatch.fired_enchantment_ids
        ),
        tuple(
            effect.effect_id
            for item_dispatch in dispatches
            for effect in item_dispatch.effects
        ),
        tuple(
            (value.item_id, value.fire_count) for value in restored.runtime.sources
        ),
    )


def _best_effort_item_source_fire_counts(
    persisted: LocalSaveExamState,
) -> tuple[tuple[str, int], ...]:
    """Keep the serialized item usage ledger without compiling its trigger."""

    try:
        raw_items, _ = _active_trigger_statuses(persisted)
    except _ReplayRejected:
        return ()
    if not isinstance(raw_items, list):
        return ()
    values: list[tuple[str, int]] = []
    for row in raw_items:
        if not isinstance(row, Mapping):
            continue
        item_id = row.get("_id")
        fire_count = row.get("_fireCount")
        if (
            isinstance(item_id, str)
            and item_id
            and not isinstance(fire_count, bool)
            and isinstance(fire_count, int)
            and fire_count >= 0
        ):
            values.append((item_id, fire_count))
    return tuple(values)


def _rebase_completed_transition_from_observation(
    transition: Plan2NativeTransition,
    observation: Plan2CardHistoryObservation,
) -> Plan2NativeTransition:
    """Overlay rendered scalars while preserving the proven native structure."""

    after = transition.after
    if after is None:
        _reject("plan2-card-history-observation-transition-missing")
    if (
        observation.authority
        is Plan2CardHistoryObservationAuthority.DIAGNOSTIC
    ):
        return Plan2NativeTransition(
            transition.before,
            transition.action,
            after,
            (
                *transition.trace,
                f"observation:{observation.source}:diagnostic-only",
            ),
            (),
            operation_receipts=transition.operation_receipts,
        )
    if observation.turns_remaining != after.remaining_turns:
        _reject(
            "plan2-card-history-observation-turn-mismatch",
            f"logical={after.remaining_turns};screen={observation.turns_remaining}",
        )
    if observation.stamina > after.scalar.max_stamina:
        _reject(
            "plan2-card-history-observation-stamina-out-of-range",
            f"screen={observation.stamina};max={after.scalar.max_stamina}",
        )

    # Journal entries commonly persist the already-proven logical horizon as
    # their observation.  Re-applying that identical observation must not
    # mutate semantic provenance (or accumulate ``+observed-hud`` suffixes)
    # every time a process reconstructs the replay chain.  The journal row and
    # transition trace still retain the observation authority.
    observation_matches = (
        (not observation.score_authoritative or observation.score == after.scalar.score)
        and observation.stamina == after.scalar.stamina
        and observation.block == after.scalar.block
        and (observation.review is None or observation.review == after.scalar.review)
        and (
            observation.aggressive is None
            or observation.aggressive == after.scalar.card_play_aggressive
        )
        and (
            observation.plays_remaining is None
            or observation.plays_remaining == after.plays_remaining
        )
    )
    if observation_matches:
        return Plan2NativeTransition(
            transition.before,
            transition.action,
            after,
            (*transition.trace, f"observation:{observation.source}:matched"),
            (),
            operation_receipts=transition.operation_receipts,
        )

    review = after.scalar.review if observation.review is None else observation.review
    aggressive = (
        after.scalar.card_play_aggressive
        if observation.aggressive is None
        else observation.aggressive
    )
    observed_score = (
        observation.score
        if observation.score_authoritative
        else after.scalar.score
    )
    scoring = after.scalar.battle_scoring
    if scoring is not None:
        score_delta = observed_score - after.scalar.score
        parameter_type = scoring.current_parameter_type.name
        scoring_fields: dict[str, int] = {
            "judge_parameter": observed_score,
        }
        if score_delta:
            total = scoring.current_turn_total_add_parameter + score_delta
            if total < 0:
                _reject("plan2-card-history-observation-score-underflow")
            scoring_fields["current_turn_total_add_parameter"] = total
            # Lesson score is one stage-local scalar.  The per-attribute
            # fields belong to audition battle tracks and stay unchanged for
            # a lesson observation.
            field = None if after.exam_mode.is_lesson else {
                "VOCAL": "judge_parameter_vocal",
                "DANCE": "judge_parameter_dance",
                "VISUAL": "judge_parameter_visual",
            }.get(parameter_type)
            if field is not None:
                value = getattr(scoring, field) + score_delta
                if value < 0:
                    _reject("plan2-card-history-observation-attribute-underflow", field)
                scoring_fields[field] = value
        scoring = replace(scoring, **scoring_fields)

    scalar = replace(
        after.scalar,
        score=observed_score,
        stamina=observation.stamina,
        block=observation.block,
        review=review,
        card_play_aggressive=aggressive,
        battle_scoring=scoring,
    )
    review_dynamic = after.review_dynamic
    if review == 0:
        review_dynamic = replace(
            review_dynamic,
            review=0,
            review_status_present=False,
            review_passing_turn_start=False,
        )
    else:
        review_dynamic = replace(
            review_dynamic,
            review=review,
            review_status_present=True,
            review_passing_turn_start=(
                review_dynamic.review_passing_turn_start
                if review_dynamic.review_status_present
                else False
            ),
        )
    triggered_review = replace(
        after.triggered_review_status_runtime,
        review_runtime=review_dynamic,
    )
    aggressive_runtime = after.aggressive_additive_runtime
    if aggressive_runtime is not None:
        aggressive_runtime = replace(aggressive_runtime, aggressive=aggressive)
    status_child_review = after.status_child_review_state
    if status_child_review is not None:
        status_child_review = replace(status_child_review, plan2_state=scalar)

    rebased = replace(
        after,
        scalar=scalar,
        review_dynamic=review_dynamic,
        triggered_review_status_runtime=triggered_review,
        aggressive_additive_runtime=aggressive_runtime,
        status_child_review_state=status_child_review,
        judge_parameter=observed_score,
        plays_remaining=(
            after.plays_remaining
            if observation.plays_remaining is None
            else observation.plays_remaining
        ),
        source_kind=f"{after.source_kind}+observed-hud",
    )
    return Plan2NativeTransition(
        transition.before,
        transition.action,
        rebased,
        (*transition.trace, f"observation:{observation.source}"),
        (),
        operation_receipts=transition.operation_receipts,
    )


def _removed_card_tombstones_are_positioned_for_replay(
    state: LocalSaveExamState,
) -> bool:
    """Bind each tombstone to one live or currently-playing card identity."""

    if not state.removed_cards:
        return True
    removed_guids = tuple(card.guid for card in state.removed_cards)
    if len(removed_guids) != len(set(removed_guids)):
        return False
    positioned = (
        *state.zones.hand,
        *state.zones.deck,
        *state.zones.grave,
        *state.zones.lost,
        *state.zones.hold,
        *((state.playing_card,) if state.playing_card is not None else ()),
    )
    for removed in state.removed_cards:
        matches = tuple(card for card in positioned if card.guid == removed.guid)
        if len(matches) != 1 or matches[0].card_id != removed.card_id:
            return False
    return True


def _trace_hand_all_upgrade_guids(
    trace: Sequence[str],
) -> tuple[str, ...] | None:
    """Return one exact ordered native HandAllUpgrade GUID batch.

    A retained queue may materialize every pre-upgrade Hand instance into
    ``removedCardList`` at once.  Membership alone is insufficient authority:
    it would accept a reordered, truncated or extended tombstone suffix.  The
    replay therefore owns exactly one non-empty comma-delimited trace row with
    no duplicate or empty GUID.
    """

    prefix = "card-upgrade:hand-all:"
    rows = tuple(value[len(prefix) :] for value in trace if value.startswith(prefix))
    if len(rows) != 1:
        return None
    guids = tuple(rows[0].split(","))
    if not guids or any(not guid for guid in guids) or len(guids) != len(set(guids)):
        return None
    return guids


def _materialize_chained_external_item_runtime(
    persisted_before: LocalSaveExamState,
    persisted_transition: LocalSaveExamState,
    before_horizon: Plan2NativeHorizonState,
    prior_replay: Plan2CompletedLogicalReplay | None,
    observation: Plan2CardHistoryObservation | None,
    catalog: Plan2NativeProgramCatalog,
) -> tuple[Plan2NativeHorizonState, bool]:
    """Adopt a newly-restorable item graph after an observed prior action.

    Live bootstrap deliberately omits an unsupported item listener graph and
    lets the next ExamSave/HUD own its scalar result.  While chaining retained
    cards, that prior native transaction can exhaust the unsupported listener,
    making the next pre-effect graph fully Master-restorable.  Bind exactly
    that graph before simulating the next card, but only when source/status
    identity is stable and all serializer progress is monotonic.
    """

    if (
        observation is None
        or not isinstance(prior_replay, Plan2CompletedCardReplay)
        or "card-history:item-runtime:screen-observed" not in prior_replay.trace
        or before_horizon.item_runtime.sources
        or before_horizon.item_runtime.listeners
        or before_horizon.item_runtime.proven_exhausted
    ):
        return before_horizon, False
    old_items, old_active = _active_status_graph(persisted_before)
    new_items, new_active = _active_status_graph(persisted_transition)
    old_statuses = tuple(
        (rid, data)
        for rid, type_value, data in old_active
        if type_value.get("class") == "TriggerEffectStatusEffect"
        and data.get("_isItemDirectEnchant") is True
    )
    new_statuses = tuple(
        (rid, data)
        for rid, type_value, data in new_active
        if type_value.get("class") == "TriggerEffectStatusEffect"
        and data.get("_isItemDirectEnchant") is True
    )
    # A retained transition may publish an unrelated native status (for
    # example a playable-count or stamina status) while the unsupported item
    # listener is unchanged.  Do not let that unrelated active-list growth
    # force an item graph restoration attempt: only a direct item status whose
    # finite counter crossed 1 -> 0 can materialize the exact additive child
    # graph below.  This is identity/progress based and intentionally has no
    # item-ID allowlist.
    item_exhaustion_candidate = len(old_statuses) != len(new_statuses)
    if not item_exhaustion_candidate:
        for (_old_rid, old_data), (_new_rid, new_data) in zip(
            old_statuses,
            new_statuses,
            strict=True,
        ):
            old_id = old_data.get("_statusEnchantId")
            new_id = new_data.get("_statusEnchantId")
            old_limit = old_data.get("_limitCount")
            new_limit = new_data.get("_limitCount")
            if (
                old_id != new_id
                or not isinstance(old_id, str)
                or not old_id
                or isinstance(old_limit, bool)
                or not isinstance(old_limit, int)
                or isinstance(new_limit, bool)
                or not isinstance(new_limit, int)
            ):
                item_exhaustion_candidate = True
                break
            if old_limit != new_limit:
                item_exhaustion_candidate = True
                break
    if not item_exhaustion_candidate:
        return before_horizon, False
    restored = restore_plan2_native_item_runtime(
        new_items,
        new_statuses,
        lesson_step_type_value=(
            persisted_transition.step_type_value
            if persisted_transition.exam_type == 0
            else None
        ),
    )
    if not restored.supported or restored.runtime is None:
        if len(new_active) != len(old_active):
            _reject(
                "plan2-card-history-external-item-materialization-unresolved",
                ",".join(restored.blockers),
            )
        return before_horizon, False
    if (
        not isinstance(old_items, list)
        or not isinstance(new_items, list)
        or len(old_items) != len(new_items)
    ):
        _reject("plan2-card-history-external-item-source-shape-mismatch")

    old_fire_by_id: dict[str, int] = {}
    new_fire_by_id: dict[str, int] = {}
    for old_row, new_row in zip(old_items, new_items, strict=True):
        if not isinstance(old_row, Mapping) or not isinstance(new_row, Mapping):
            _reject("plan2-card-history-external-item-source-shape-mismatch")
        old_id = old_row.get("_id")
        new_id = new_row.get("_id")
        old_fire = old_row.get("_fireCount")
        new_fire = new_row.get("_fireCount")
        old_identity = dict(old_row)
        new_identity = dict(new_row)
        old_identity.pop("_fireCount", None)
        new_identity.pop("_fireCount", None)
        if (
            not isinstance(old_id, str)
            or not old_id
            or new_id != old_id
            or isinstance(old_fire, bool)
            or not isinstance(old_fire, int)
            or isinstance(new_fire, bool)
            or not isinstance(new_fire, int)
            or old_fire < 0
            or new_fire < old_fire
            or old_identity != new_identity
            or old_id in old_fire_by_id
        ):
            _reject("plan2-card-history-external-item-source-progress-mismatch")
        old_fire_by_id[old_id] = old_fire
        new_fire_by_id[old_id] = new_fire

    if tuple(rid for rid, _data in old_statuses) != tuple(
        rid for rid, _data in new_statuses
    ):
        _reject("plan2-card-history-external-item-status-identity-mismatch")
    exhausted = {
        (value.source_item_id, value.enchantment_id, value.status_uid): value
        for value in restored.runtime.proven_exhausted
    }
    exhausted.update(
        {
            (value.source_item_id, value.enchantment_id, value.status_uid): value
            for value in restored.runtime.listeners
            if value.max_uses > 0 and value.remaining_uses == 0
        }
    )
    newly_exhausted: list[
        tuple[
            str,
            str,
            int,
            Mapping[str, object],
            Mapping[str, object],
        ]
    ] = []
    for (_old_rid, old_data), (_new_rid, new_data) in zip(
        old_statuses,
        new_statuses,
        strict=True,
    ):
        old_trigger = old_data.get("_triggerItem")
        new_trigger = new_data.get("_triggerItem")
        old_limit = old_data.get("_limitCount")
        new_limit = new_data.get("_limitCount")
        if (
            not isinstance(old_trigger, Mapping)
            or not isinstance(new_trigger, Mapping)
            or isinstance(old_limit, bool)
            or not isinstance(old_limit, int)
            or isinstance(new_limit, bool)
            or not isinstance(new_limit, int)
        ):
            _reject("plan2-card-history-external-item-status-shape-mismatch")
        item_id = old_trigger.get("_id")
        old_fire = old_trigger.get("_fireCount")
        new_fire = new_trigger.get("_fireCount")
        if (
            not isinstance(item_id, str)
            or not item_id
            or new_trigger.get("_id") != item_id
            or old_fire_by_id.get(item_id) != old_fire
            or new_fire_by_id.get(item_id) != new_fire
            or (old_limit == -1 and new_limit != -1)
            or (
                old_limit >= 0
                and not (0 <= new_limit <= old_limit <= new_limit + 1)
            )
        ):
            _reject("plan2-card-history-external-item-status-progress-mismatch")
        old_identity = dict(old_data)
        new_identity = dict(new_data)
        old_identity["_limitCount"] = 0
        new_identity["_limitCount"] = 0
        old_trigger_identity = dict(old_trigger)
        new_trigger_identity = dict(new_trigger)
        old_trigger_identity["_fireCount"] = 0
        new_trigger_identity["_fireCount"] = 0
        old_identity["_triggerItem"] = old_trigger_identity
        new_identity["_triggerItem"] = new_trigger_identity
        if old_identity != new_identity:
            _reject("plan2-card-history-external-item-status-identity-mismatch")

        enchantment_id = new_data.get("_statusEnchantId")
        uid = new_data.get("_uid")
        if (
            old_limit == 1
            and new_limit == 0
            and isinstance(enchantment_id, str)
            and enchantment_id
            and not isinstance(uid, bool)
            and isinstance(uid, int)
            and (item_id, enchantment_id, uid) in exhausted
        ):
            newly_exhausted.append(
                (
                    item_id,
                    enchantment_id,
                    uid,
                    old_data,
                    new_data,
                )
            )
        elif old_limit != new_limit:
            _reject(
                "plan2-card-history-external-item-unowned-limit-progress",
                f"{item_id}:{old_limit}->{new_limit}",
            )

    if len(newly_exhausted) != 1:
        _reject(
            "plan2-card-history-external-item-exhaustion-not-unique",
            f"count={len(newly_exhausted)}",
        )
    (
        source_item_id,
        _enchantment_id,
        _listener_uid,
        old_exhausted_status,
        new_exhausted_status,
    ) = newly_exhausted[0]
    raw_effects = new_exhausted_status.get("_effectList")
    if (
        not isinstance(raw_effects, list)
        or len(raw_effects) != len(_PITEM_STATUS_CHANGE_EFFECT_STATUS_SHAPES)
        or any(not isinstance(value, Mapping) for value in raw_effects)
    ):
        _reject("plan2-card-history-external-item-additive-effect-shape")
    for raw_effect, (effect_id, effect_type, _class_name) in zip(
        raw_effects,
        _PITEM_STATUS_CHANGE_EFFECT_STATUS_SHAPES,
        strict=True,
    ):
        assert isinstance(raw_effect, Mapping)
        if (
            raw_effect.get("_id") != effect_id
            or raw_effect.get("_effectType") != effect_type
            or raw_effect.get("_effectValue1") != 500
            or raw_effect.get("_effectValue2") != 0
            or raw_effect.get("_effectCount") != 0
            or raw_effect.get("_effectTurn") != 2
        ):
            _reject(
                "plan2-card-history-external-item-additive-effect-mismatch",
                effect_id,
            )

    # ``_fireCount`` is not a remaining-use counter.  For this exact
    # two-child native listener the source/trigger ledger advances once per
    # child while the finite listener spends exactly one use.  Other source
    # rows may mirror the same serializer progress, but no row may move by an
    # unrelated amount.
    child_count = len(raw_effects)
    if (
        new_fire_by_id[source_item_id] - old_fire_by_id[source_item_id]
        != child_count
        or new_exhausted_status["_triggerItem"].get("_fireCount")
        - old_exhausted_status["_triggerItem"].get("_fireCount")
        != child_count
        or any(
            new_fire_by_id[item_id] - old_fire_by_id[item_id]
            not in {0, child_count}
            for item_id in old_fire_by_id
        )
    ):
        _reject("plan2-card-history-external-item-source-progress-mismatch")

    # The native active-status list is the transaction authority here.  The
    # retained commandList contains only the selected card's own commands;
    # item-created status children are published as two ordered active status
    # rows after the prior active prefix.  Requiring that exact prefix/suffix
    # prevents a different status mutation from being mistaken for this item.
    if len(new_active) != len(old_active) + len(raw_effects):
        _reject("plan2-card-history-external-item-active-status-count-mismatch")
    direct_item_rids = {rid for rid, _data in old_statuses}
    for old_row, new_row in zip(
        old_active,
        new_active[: len(old_active)],
        strict=True,
    ):
        old_rid, old_type, old_data = old_row
        new_rid, new_type, new_data = new_row
        if old_rid != new_rid or old_type != new_type:
            _reject("plan2-card-history-external-item-active-prefix-mismatch")
        if old_rid not in direct_item_rids and old_data != new_data:
            # The second retained save is already post-cost for the next
            # selected card.  Native scalar status rows may therefore carry
            # a new value while their complete status identity remains
            # stable; that value is independently checked by the ordinary
            # post-cost/HUD scalar proof below.  No other active status row is
            # permitted to drift across this materialization boundary.
            class_name = old_type.get("class")
            old_scalar_status = dict(old_data)
            new_scalar_status = dict(new_data)
            old_value = old_scalar_status.pop("_value", None)
            new_value = new_scalar_status.pop("_value", None)
            if (
                class_name
                not in {"ReviewStatusEffect", "AggressiveStatusEffect"}
                or isinstance(old_value, bool)
                or not isinstance(old_value, int)
                or isinstance(new_value, bool)
                or not isinstance(new_value, int)
                or old_scalar_status != new_scalar_status
            ):
                _reject(
                    "plan2-card-history-external-item-active-prefix-mismatch"
                )

    additive_rows = new_active[len(old_active) :]
    additive_uids: list[int] = []
    additive_rids: list[int] = []
    for (
        rid,
        type_value,
        raw_status,
    ), raw_effect, (_effect_id, _effect_type, class_name) in zip(
        additive_rows,
        raw_effects,
        _PITEM_STATUS_CHANGE_EFFECT_STATUS_SHAPES,
        strict=True,
    ):
        assert isinstance(raw_effect, Mapping)
        expected_type = {**_ADDITIVE_STATUS_TYPE, "class": class_name}
        expected_status = {
            "_isPassingTurnStart": False,
            "_isTurnLimited": True,
            "_turn": raw_effect["_effectTurn"],
            "_uid": raw_status.get("_uid"),
            "_value": raw_effect["_effectValue1"],
        }
        uid = raw_status.get("_uid")
        if (
            type_value != expected_type
            or raw_status != expected_status
            or isinstance(uid, bool)
            or not isinstance(uid, int)
            or uid < 1
            or rid in {value[0] for value in old_active}
        ):
            _reject("plan2-card-history-external-item-additive-status-mismatch")
        additive_uids.append(uid)
        additive_rids.append(rid)
    if (
        len(set(additive_rids)) != len(additive_rids)
        or additive_uids != list(range(additive_uids[0], additive_uids[0] + 2))
    ):
        _reject("plan2-card-history-external-item-additive-order-mismatch")

    review_uid, aggressive_uid = additive_uids
    existing_next_uids = (
        before_horizon.scalar.next_status_uid,
        before_horizon.status_enchant.next_status_uid,
        before_horizon.review_dynamic.next_status_uid,
        before_horizon.triggered_review_status_runtime.next_status_uid,
    )
    if (
        any(value != review_uid for value in existing_next_uids)
        or before_horizon.review_dynamic.layers
        or before_horizon.aggressive_additive_runtime is not None
    ):
        _reject("plan2-card-history-external-item-additive-uid-mismatch")

    aggressive_catalogs = tuple(
        executor.aggressive_additive_catalog
        for program in catalog.programs
        for executor in (program.native_playing_executor,)
        if isinstance(executor, Plan2MasterPlayingExecutor)
        and executor.aggressive_additive_catalog is not None
    )
    if (
        not aggressive_catalogs
        or len({id(value) for value in aggressive_catalogs}) != 1
    ):
        _reject("plan2-card-history-external-item-additive-catalog-mismatch")
    aggressive_catalog = aggressive_catalogs[0]
    next_uid = aggressive_uid + 1
    source_identity = f"item:{source_item_id}"
    review_layer = Plan2NativeReviewDynamicLayer(
        review_uid,
        LAYER_REVIEW_ADDITIVE,
        source_identity,
        source_item_id,
        0,
        _PITEM_STATUS_CHANGE_REVIEW_EFFECT_ID,
        500,
        2,
        False,
        True,
    )
    review_runtime = replace(
        before_horizon.review_dynamic,
        layers=(review_layer,),
        next_status_uid=next_uid,
    )
    aggressive_runtime = Plan2AggressiveAdditiveRuntime(
        catalog=aggressive_catalog,
        aggressive=before_horizon.scalar.card_play_aggressive,
        additive_layers=(
            AggressiveAdditiveLayer(
                aggressive_uid,
                500,
                2,
                1,
                PITEM_STATUS_CHANGE_ADDITIVE_EFFECT_ID,
            ),
        ),
        next_status_uid=next_uid,
        next_install_sequence=2,
    )
    status_child_review = before_horizon.status_child_review_state
    if status_child_review is not None:
        status_child_review = replace(
            status_child_review,
            plan2_state=replace(
                status_child_review.plan2_state,
                next_status_uid=next_uid,
            ),
        )

    return (
        replace(
            before_horizon,
            scalar=replace(
                before_horizon.scalar,
                next_status_uid=next_uid,
            ),
            item_runtime=restored.runtime,
            status_enchant=replace(
                before_horizon.status_enchant,
                next_status_uid=next_uid,
            ),
            review_dynamic=review_runtime,
            aggressive_additive_runtime=aggressive_runtime,
            triggered_review_status_runtime=replace(
                before_horizon.triggered_review_status_runtime,
                review_runtime=review_runtime,
                next_status_uid=next_uid,
            ),
            status_child_review_state=status_child_review,
            source_kind=(
                f"{before_horizon.source_kind}"
                "+materialized-external-item-status-queue"
            ),
        ),
        True,
    )


def _replay(
    persisted_before: AuditionLocalSaveStateEvidence,
    persisted_transition: AuditionLocalSaveStateEvidence,
    action: Plan2NativeAction,
    *,
    before_horizon: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
    database: Path,
    prior_replay: Plan2CompletedLogicalReplay | None = None,
    observation: Plan2CardHistoryObservation | None = None,
) -> Plan2CompletedCardReplay:
    logical_observation = (
        observation
        if observation is not None
        and observation.authority
        is Plan2CardHistoryObservationAuthority.LOGICAL_REPLAY
        else None
    )
    prior_destination = (
        None
        if prior_replay is None
        else _prove_chained_before_horizon(
            persisted_before, before_horizon, prior_replay
        )
    )
    if prior_replay is None:
        _prove_before_horizon(persisted_before, before_horizon)
    if action.kind != "play":
        _reject("plan2-card-history-action-not-play", action.action_id)
    if not _same_stage_boundary(
        persisted_before,
        persisted_transition,
        completed_card_count=(
            1 if isinstance(prior_replay, Plan2CompletedCardReplay) else 0
        ),
    ):
        _reject("plan2-card-history-stage-boundary-mismatch")
    old = persisted_before.state
    new = persisted_transition.state
    if (
        isinstance(prior_replay, Plan2CompletedDrinkReplay)
        and prior_replay.provisional_from_submitted_receipt
    ):
        prior_action = prior_replay.action
        before_drinks = _ordered_drink_ids_from_evidence(persisted_before)
        after_drinks = _ordered_drink_ids_from_evidence(persisted_transition)
        if (
            not 0 <= prior_action.slot_index < len(before_drinks)
            or before_drinks[prior_action.slot_index] != prior_action.drink_id
        ):
            _reject(
                "plan2-card-history-provisional-drink-before-mismatch",
                prior_action.action_id,
            )
        expected_after_drinks = (
            before_drinks[: prior_action.slot_index]
            + before_drinks[prior_action.slot_index + 1 :]
        )
        if after_drinks != expected_after_drinks:
            _reject(
                "plan2-card-history-provisional-drink-not-materialized",
                f"expected={expected_after_drinks!r};actual={after_drinks!r}",
            )
    old_runtime = old.root_runtime
    runtime = new.root_runtime
    # ``removedCardList`` is a positioned history/tombstone collection, not a
    # live card zone.  A tombstone already present in the settled before-save
    # may persist byte-for-byte while the next card command is retained.  The
    # typed card equality below covers every serialized identity, upgrade and
    # runtime field; tuple equality also binds order/zone_order.  Keep both
    # original evidence objects on the replay and exclude only this proven
    # stable collection from the semantic card transition.
    stable_removed_tombstones = bool(
        old.removed_cards == new.removed_cards
        and _removed_card_tombstones_are_positioned_for_replay(old)
        and _removed_card_tombstones_are_positioned_for_replay(new)
    )
    deferred_prior_drink_selected_tombstone = False
    if (
        not stable_removed_tombstones
        and isinstance(prior_replay, Plan2CompletedDrinkReplay)
        and not prior_replay.provisional_from_submitted_receipt
        and prior_replay.persisted_before is not None
        and len(new.removed_cards) == len(old.removed_cards) + 1
        and new.removed_cards[:-1] == old.removed_cards
        and _removed_card_tombstones_are_positioned_for_replay(old)
        and _removed_card_tombstones_are_positioned_for_replay(new)
    ):
        # Hand-wide upgrade drinks can remain as a pre-effect queue until the
        # upgraded card is played.  Native then publishes the selected card's
        # old Hand value as one appended tombstone at the same transaction
        # boundary as the upgraded Playing value.  Accept only that exact
        # three-way witness: old Hand -> appended raw tombstone, prior logical
        # Hand -> incremented Playing.  Existing tombstones must remain an
        # ordered byte-for-byte prefix owned by the prior drink replay.
        try:
            normalized_old = _normalize_prior_drink_card_tombstones(
                old,
                persisted_before=prior_replay.persisted_before,
                before_horizon=prior_replay.before,
            )
            old_selected_matches = tuple(
                card for card in old.zones.hand if card.guid == action.card_guid
            )
            logical_selected_matches = tuple(
                card
                for card in before_horizon.zones.hand
                if card.guid == action.card_guid
            )
            appended = new.removed_cards[-1]
            old_selected = old_selected_matches[0]
            logical_selected = logical_selected_matches[0]
            old_native = NativeOrderedCardInstance.from_local_save(old_selected)
            observed_playing = NativeOrderedCardInstance.from_local_save(
                new.playing_card
            )
        except (IndexError, TypeError, ValueError, _ReplayRejected):
            pass
        else:
            deferred_prior_drink_selected_tombstone = bool(
                not normalized_old.removed_cards
                and len(old_selected_matches) == 1
                and len(logical_selected_matches) == 1
                and appended.zone_order == len(old.removed_cards)
                and replace(appended, zone_order=old_selected.zone_order)
                == old_selected
                and old_native != logical_selected
                and observed_playing == logical_selected.increment_play_count()
            )
            if deferred_prior_drink_selected_tombstone:
                stable_removed_tombstones = True
    deferred_prior_card_upgrade_tombstone = False
    deferred_prior_card_selected_upgrade_tombstone = False
    if (
        not stable_removed_tombstones
        and isinstance(prior_replay, Plan2CompletedCardReplay)
        and len(new.removed_cards) > len(old.removed_cards)
        and new.removed_cards[: len(old.removed_cards)] == old.removed_cards
        and _removed_card_tombstones_are_positioned_for_replay(old)
        and _removed_card_tombstones_are_positioned_for_replay(new)
    ):
        # HandAllUpgrade can publish every pre-upgrade Hand instance as one
        # ordered tombstone suffix when the following PLAY is submitted before
        # the retained queue disappears.  Bind the complete suffix—not merely
        # per-GUID membership—to the prior replay's single comma-delimited
        # upgrade trace.  Each raw instance must progress to the exact logical
        # upgrade and then either remain in its active zone or, for the unique
        # selected GUID, become Playing with one accepted-play increment.
        appended_suffix = new.removed_cards[len(old.removed_cards) :]
        traced_guids = _trace_hand_all_upgrade_guids(prior_replay.trace)
        old_positioned = (
            *old.zones.hand,
            *old.zones.deck,
            *old.zones.grave,
            *old.zones.lost,
            *old.zones.hold,
            *((old.playing_card,) if old.playing_card is not None else ()),
        )
        new_positioned = (
            *new.zones.hand,
            *new.zones.deck,
            *new.zones.grave,
            *new.zones.lost,
            *new.zones.hold,
            *((new.playing_card,) if new.playing_card is not None else ()),
        )
        logical_positioned = (
            *before_horizon.zones.hand,
            *before_horizon.zones.deck,
            *before_horizon.zones.grave,
            *before_horizon.zones.lost,
        )
        logical_selected = tuple(
            card
            for card in before_horizon.zones.hand
            if card.guid == action.card_guid
        )
        try:
            observed_playing = NativeOrderedCardInstance.from_local_save(
                new.playing_card
            )
            batch_valid = bool(
                traced_guids
                == tuple(card.guid for card in appended_suffix)
                and len(logical_selected) == 1
            )
            selected_witnesses = 0
            for index, appended in enumerate(appended_suffix):
                old_matches = tuple(
                    card for card in old_positioned if card.guid == appended.guid
                )
                new_matches = tuple(
                    card for card in new_positioned if card.guid == appended.guid
                )
                logical_matches = tuple(
                    card
                    for card in logical_positioned
                    if card.guid == appended.guid
                )
                if not (
                    len(old_matches) == 1
                    and len(new_matches) == 1
                    and len(logical_matches) == 1
                    and appended.zone_order == len(old.removed_cards) + index
                    and replace(
                        appended,
                        zone_order=old_matches[0].zone_order,
                    )
                    == old_matches[0]
                ):
                    batch_valid = False
                    continue
                old_card = NativeOrderedCardInstance.from_local_save(
                    old_matches[0]
                )
                new_card = NativeOrderedCardInstance.from_local_save(
                    new_matches[0]
                )
                logical_card = logical_matches[0]
                if old_card == logical_card:
                    batch_valid = False
                if appended.guid == action.card_guid:
                    selected_witnesses += 1
                    if not (
                        new_matches[0] == new.playing_card
                        and logical_card == logical_selected[0]
                        and new_card
                        == logical_selected[0].increment_play_count()
                    ):
                        batch_valid = False
                elif new_card != logical_card:
                    batch_valid = False
        except (IndexError, TypeError, ValueError):
            pass
        else:
            unaffected_selected = False
            if batch_valid and selected_witnesses == 0:
                # HandAllUpgrade only tombstones cards whose persisted raw
                # instance actually changed.  The following selected card may
                # already have the logical upgrade, so it is absent from the
                # non-empty suffix.  In that shape, prove the selected card
                # independently across old -> logical -> fresh Playing rather
                # than weakening any suffix witness.
                suffix_guids = tuple(card.guid for card in appended_suffix)
                old_selected_matches = tuple(
                    card
                    for card in old_positioned
                    if card.guid == action.card_guid
                )
                if (
                    appended_suffix
                    and action.card_guid not in suffix_guids
                    and len(old_selected_matches) == 1
                    and len(logical_selected) == 1
                ):
                    old_selected_card = NativeOrderedCardInstance.from_local_save(
                        old_selected_matches[0]
                    )
                    unaffected_selected = bool(
                        old_selected_card == logical_selected[0]
                        and observed_playing
                        == logical_selected[0].increment_play_count()
                    )
            deferred_prior_card_upgrade_tombstone = bool(
                batch_valid
                and (selected_witnesses == 1 or unaffected_selected)
                and observed_playing
                == logical_selected[0].increment_play_count()
            )
            if deferred_prior_card_upgrade_tombstone:
                stable_removed_tombstones = True
                deferred_prior_card_selected_upgrade_tombstone = (
                    selected_witnesses == 1
                )
    prior_drink_tombstones_carried = deferred_prior_drink_selected_tombstone
    if (
        new.removed_cards
        and isinstance(prior_replay, Plan2CompletedDrinkReplay)
        and not prior_replay.provisional_from_submitted_receipt
        and old.removed_cards == new.removed_cards
        and prior_replay.persisted_before is not None
    ):
        # A completed drink can leave the preceding HandAllUpgrade snapshot in
        # removedCardList while its exact upgraded GUID moves from Hand to the
        # next card's playingCard.  Re-prove that the tombstone was already
        # owned by the drink transition, then require byte-for-byte carryover;
        # the requested Playing GUID/runtime is still checked below.
        try:
            normalized_old = _normalize_prior_drink_card_tombstones(
                old,
                persisted_before=prior_replay.persisted_before,
                before_horizon=prior_replay.before,
            )
        except _ReplayRejected:
            pass
        else:
            prior_drink_tombstones_carried = not normalized_old.removed_cards
    if (
        old_runtime is None
        or runtime is None
        or new.phase != 6
        or new.playing_card is None
        or new.playing_card.guid != action.card_guid
        or not stable_removed_tombstones
        or runtime.command_list_is_empty
        or runtime.draw_card_guid_list
        or runtime.is_exam_end_complete
        or old.zones.hold
        or new.zones.hold
    ):
        _reject("plan2-card-history-transition-boundary-invalid")

    # ``isTurnCardGrave`` / ``isTurnCardLost`` are presentation/lifecycle
    # flags, not the ordered-zone authority.  In a retained multi-card queue
    # the game can already serialize the preceding GUID in Grave/Lost while
    # leaving both flags false until the outer command transaction drains.
    # Requiring those redundant flags caused a successfully played second
    # card to be rejected on restart.  The exact GUID comparison for Hand,
    # Deck, Grave and Lost below remains the action/destination authority.

    native_matches = tuple(
        card for card in before_horizon.zones.hand if card.guid == action.card_guid
    )
    if prior_replay is None:
        # The first action is bound directly to the settled LocalSave Hand.
        # Requiring Hand membership here remains the screen/GUID authority.
        persisted_candidates = old.zones.hand
    else:
        # A retained command queue is a pre-effect snapshot.  The preceding
        # logical card *or drink* replay may already have drawn the next
        # visible card from Deck or Grave while the raw ExamSave still
        # serializes that GUID in its old zone (or as the previous Playing
        # card).  The logical Hand proves playability; the complete persisted
        # positioned universe proves that the same runtime identity exists
        # exactly once.  Do not require the stale raw Hand to have
        # materialized the draw before accepting the chained action.
        persisted_candidates = (
            *old.zones.hand,
            *old.zones.deck,
            *old.zones.grave,
            *old.zones.lost,
            *((old.playing_card,) if old.playing_card is not None else ()),
        )
    persisted_matches = tuple(
        card for card in persisted_candidates if card.guid == action.card_guid
    )
    if len(native_matches) != 1 or len(persisted_matches) != 1:
        _reject("plan2-card-history-action-guid-not-unique", action.card_guid)
    selected = native_matches[0]
    persisted_selected = NativeOrderedCardInstance.from_local_save(
        persisted_matches[0]
    )
    selected_before_matches = persisted_selected == selected
    if (
        not selected_before_matches
        and isinstance(prior_replay, Plan2CompletedCardReplay)
    ):
        selected_before_matches = _chained_hand_add_identity_equal(
            persisted_selected,
            selected,
        )
    if not selected_before_matches and deferred_prior_drink_selected_tombstone:
        selected_before_matches = True
    if (
        not selected_before_matches
        and deferred_prior_card_selected_upgrade_tombstone
    ):
        selected_before_matches = True
    if not selected_before_matches:
        _reject("plan2-card-history-selected-card-before-mismatch")
    observed_playing = NativeOrderedCardInstance.from_local_save(new.playing_card)
    if observed_playing != selected.increment_play_count():
        _reject("plan2-card-history-playing-card-runtime-mismatch")

    for zone_name in ("hand", "deck", "grave", "lost"):
        expected = tuple(
            card
            for card in getattr(before_horizon.zones, zone_name)
            if card.guid != action.card_guid
        )
        if not _cross_authority_zone_equal(
            getattr(new.zones, zone_name), expected
        ):
            _reject("plan2-card-history-transition-zone-mismatch", zone_name)
    logical_hand_add_runtime = bool(
        new.random_state == before_horizon.zones.random_state
        and _same_turn_used_support_ids(
            new.turn_use_support_ids,
            before_horizon.turn_used_support_ids,
        )
    )
    retained_hand_add_runtime = bool(
        new.random_state == old.random_state
        and _same_turn_used_support_ids(
            new.turn_use_support_ids,
            old.turn_use_support_ids,
        )
    )
    if prior_replay is not None and not (
        logical_hand_add_runtime or retained_hand_add_runtime
    ):
        _reject(
            "plan2-card-history-transition-hand-add-runtime-mismatch",
            f"rng={new.random_state}/"
            f"logical={before_horizon.zones.random_state}/"
            f"retained={old.random_state};"
            f"used={new.turn_use_support_ids!r}/"
            f"logical={before_horizon.turn_used_support_ids!r}/"
            f"retained={old.turn_use_support_ids!r}",
        )

    if catalog.get(selected) is None:
        _reject(
            "plan2-card-history-card-program-missing",
            f"{selected.card_id}@{selected.effective_upgrade}",
        )
    try:
        program = materialize_plan2_native_card_program(catalog, selected)
    except (TypeError, ValueError) as error:
        _reject(
            "plan2-card-history-runtime-customization-unresolved",
            f"{selected.card_id}@{selected.effective_upgrade}:"
            f"{type(error).__name__}:{error}",
        )
    program_effect_ids, specs = _materialize_master_effects(
        program,
        selected,
        database=database,
    )
    playing_executor = program.native_playing_executor
    assert isinstance(playing_executor, Plan2MasterPlayingExecutor)
    runtime_customization_trace = playing_executor.runtime_customization_trace
    expected_playing = _raw_card(new.playing_card)
    (
        command_effect_ids,
        retained_final_remaining_plays,
        native_item_groups,
    ) = _prove_command_queue(
        new,
        specs=specs,
        expected_playing=expected_playing,
        before_plays=before_horizon.plays_remaining,
        destination=program.move_destination,
    )

    before_horizon, external_item_runtime_materialized = (
        _materialize_chained_external_item_runtime(
            old,
            new,
            before_horizon,
            prior_replay,
            logical_observation,
            catalog,
        )
    )

    paid = _expected_post_cost(before_horizon, program)
    retained_prior_drink_scalar_materialization = False
    if isinstance(prior_replay, Plan2CompletedDrinkReplay):
        materialized_scalar = _retained_prior_drink_materialized_scalar(
            old,
            new,
            expected_drink_id=prior_replay.action.drink_id,
            current_card_id=selected.card_id,
        )
        if materialized_scalar is not None:
            materialized_score, materialized_stamina, materialized_block = (
                materialized_scalar
            )
            original_scalar = before_horizon.scalar
            materialized_transition = (
                _rebase_completed_transition_from_observation(
                    Plan2NativeTransition(
                        before_horizon,
                        action,
                        before_horizon,
                        ("retained-drink-localsave-scalar-rebase",),
                        (),
                    ),
                    Plan2CardHistoryObservation(
                        turns_remaining=before_horizon.remaining_turns,
                        score=materialized_score,
                        stamina=materialized_stamina,
                        block=materialized_block,
                        review=original_scalar.review,
                        aggressive=original_scalar.card_play_aggressive,
                        plays_remaining=before_horizon.plays_remaining,
                        source="retained-drink-localsave-scalar",
                        score_authoritative=True,
                        authority=(
                            Plan2CardHistoryObservationAuthority.LOGICAL_REPLAY
                        ),
                    ),
                )
            )
            assert materialized_transition.after is not None
            materialized_before = replace(
                materialized_transition.after,
                source_kind=(
                    f"{before_horizon.source_kind}+"
                    "retained-drink-localsave-scalar"
                ),
            )
            materialized_paid = _expected_post_cost(materialized_before, program)
            if (
                new.score == materialized_score
                and new.stamina == materialized_paid.stamina
                and new.block == materialized_paid.block
                and new.max_stamina == materialized_paid.max_stamina
            ):
                if logical_observation is not None:
                    observed_score = materialized_score + (
                        logical_observation.score - original_scalar.score
                    )
                    observed_stamina = materialized_stamina + (
                        logical_observation.stamina - original_scalar.stamina
                    )
                    observed_block = materialized_block + (
                        logical_observation.block - original_scalar.block
                    )
                    if min(observed_score, observed_stamina, observed_block) < 0:
                        _reject(
                            "plan2-card-history-retained-drink-observation-underflow"
                        )
                    logical_observation = replace(
                        logical_observation,
                        score=observed_score,
                        stamina=observed_stamina,
                        block=observed_block,
                        source=(
                            f"{logical_observation.source}:"
                            "retained-drink-localsave-scalar"
                        ),
                    )
                before_horizon = materialized_before
                paid = materialized_paid
                retained_prior_drink_scalar_materialization = True
    retained_prior_materialization = False
    retained_prior_materialized_effect_ids: tuple[str, ...] = ()
    # When a second PLAY is submitted while the previous card's retained
    # command queue is still present, native may drain that older queue and
    # publish its direct scalar effects in the same ExamSave transaction as
    # the new card's post-cost queue.  The new queue is then pre-effect for
    # the requested card, but a raw scalar such as Block can be one or more
    # Master effects ahead of ``paid``.  Do not infer this from HUD values or
    # accept an arbitrary scalar delta: rebuild the immediately preceding
    # card from its exact logical root and use the compiled Master transition
    # as the only additional witness.
    retained_prior_materialized_paid: Plan2CostResources | None = None
    retained_prior_materialized_score: int | None = None
    retained_prior_previous_after: Plan2NativeHorizonState | None = None
    retained_prior_observed_score = False
    retained_prior_schedule_materialized = False
    if isinstance(prior_replay, Plan2CompletedCardReplay):
        previous = simulate_plan2_native_action(
            prior_replay.before,
            prior_replay.action,
            catalog,
        )
        if previous.supported and previous.after is not None:
            retained_prior_previous_after = previous.after
            retained_prior_materialized_paid = _expected_post_cost(
                previous.after,
                program,
            )
            retained_prior_materialized_score = previous.after.scalar.score
            retained_prior_materialized_effect_ids = prior_replay.command_effect_ids
            retained_prior_materialization = bool(
                retained_prior_materialized_effect_ids
                and retained_prior_materialized_score is not None
                and new.score == retained_prior_materialized_score
                and new.stamina == retained_prior_materialized_paid.stamina
                and new.max_stamina == retained_prior_materialized_paid.max_stamina
                and new.block == retained_prior_materialized_paid.block
                and any(
                    (
                        new.score != before_horizon.scalar.score,
                        new.stamina != paid.stamina,
                        new.max_stamina != paid.max_stamina,
                        new.block != paid.block,
                    )
                )
            )
    if (
        isinstance(prior_replay, Plan2CompletedCardReplay)
        and not retained_prior_materialization
        # The following retained PLAY is an exact native boundary: its raw
        # score is the settled score after the prior card, while its
        # stamina/block already include the current card's proven cost.  A
        # prior HUD/projection can overestimate that settled score, so do not
        # require the native value to exceed the logical projection.  It must
        # still have advanced beyond the prior physical pre-effect save; an
        # unchanged value remains the separate retained-pre-effect case.
        and new.score > old.score
        and new.score != before_horizon.scalar.score
        and new.stamina == paid.stamina
        and new.block == paid.block
        and new.max_stamina == paid.max_stamina
    ):
        # The prior retained command can finish additional passive/status
        # score work in the same save that records the next PLAY's proven
        # post-cost boundary.  Action/GUID/queue identity has already been proven above;
        # adopt only the monotonic LocalSave score, then run the current card
        # from that authoritative scalar.  Current stamina/block costs remain
        # independently bound by ``paid`` and the persisted post-cost values.
        # ``new`` owns the full native battle-scoring bookkeeping at this
        # post-cost/pre-effect boundary.  Replacing only the headline score
        # would leave current-turn/per-attribute counters descended from the
        # over-projected HUD value and can underflow when the correction is
        # downward.  Card payment cannot mutate any of these score fields, so
        # the exact ExamSave scoring context is also the current card's
        # authoritative pre-effect context.
        try:
            native_scoring = plan2_battle_scoring_context_from_exam_save(new)
        except (TypeError, ValueError) as error:
            _reject(
                "plan2-card-history-native-score-context-unresolved",
                f"{type(error).__name__}:{error}",
            )
        logical_scoring = before_horizon.scalar.battle_scoring
        schedule_matches = bool(
            logical_scoring is not None
            and native_scoring.turn_parameter_types
            == logical_scoring.turn_parameter_types
        )
        if (
            not schedule_matches
            and logical_scoring is not None
            and retained_prior_previous_after is not None
            and _native_extra_turn_schedule_materialization_matches(
                old,
                new,
                logical_scoring,
                native_scoring,
                retained_prior_previous_after,
            )
        ):
            schedule_matches = True
            retained_prior_schedule_materialized = True
        if logical_scoring is None or any(
            (
                native_scoring.current_turn != logical_scoring.current_turn,
                native_scoring.limit_turn != logical_scoring.limit_turn,
                native_scoring.extra_turn != logical_scoring.extra_turn,
                not schedule_matches,
                native_scoring.exam_mode != logical_scoring.exam_mode,
            )
        ):
            _reject(
                "plan2-card-history-native-score-context-identity-mismatch",
                f"native=({native_scoring.current_turn},"
                f"{native_scoring.limit_turn},{native_scoring.extra_turn},"
                f"{len(native_scoring.turn_parameter_types)});"
                "logical=("
                f"{getattr(logical_scoring, 'current_turn', None)},"
                f"{getattr(logical_scoring, 'limit_turn', None)},"
                f"{getattr(logical_scoring, 'extra_turn', None)},"
                f"{len(getattr(logical_scoring, 'turn_parameter_types', ()))})",
            )
        scalar = replace(
            before_horizon.scalar,
            score=new.score,
            battle_scoring=native_scoring,
        )
        status_child_review = before_horizon.status_child_review_state
        if status_child_review is not None:
            status_child_review = replace(
                status_child_review,
                plan2_state=scalar,
            )
        before_horizon = replace(
            before_horizon,
            scalar=scalar,
            status_child_review_state=status_child_review,
            judge_parameter=new.score,
            source_kind=(
                f"{before_horizon.source_kind}+chained-localsave-score"
            ),
        )
        paid = _expected_post_cost(before_horizon, program)
        retained_prior_observed_score = True
    retained_pre_effect_score = bool(
        prior_replay is not None
        and new.score == old.score
        and new.score != before_horizon.scalar.score
    )
    external_cost_modifier = bool(
        logical_observation is not None
        and (new.stamina != paid.stamina or new.block != paid.block)
        and logical_observation.stamina == new.stamina
        and logical_observation.block == new.block
    )
    if (
        (
            new.score != before_horizon.scalar.score
            and not retained_pre_effect_score
            and not retained_prior_materialization
        )
        or (
            new.max_stamina != paid.max_stamina
            and not retained_prior_materialization
        )
        or (
            not external_cost_modifier
            and not retained_prior_materialization
            and (new.stamina != paid.stamina or new.block != paid.block)
        )
    ):
        _reject(
            "plan2-card-history-post-cost-scalar-mismatch",
            f"expected=({before_horizon.scalar.score},{paid.stamina},"
            f"{paid.block},{paid.max_stamina});actual=({new.score},"
            f"{new.stamina},{new.block},{new.max_stamina})",
        )

    card_transition = simulate_plan2_native_action(
        before_horizon, action, catalog
    )
    if not card_transition.supported or card_transition.after is None:
        detail = ",".join(
            value.code + ("=" + value.detail if value.detail else "")
            for value in card_transition.blockers
        )
        _reject("plan2-card-history-horizon-transition-blocked", detail)
    card_after = card_transition.after
    if external_cost_modifier:
        card_after = replace(
            card_after,
            scalar=replace(
                card_after.scalar,
                stamina=new.stamina,
                block=new.block,
            ),
            source_kind=f"{card_after.source_kind}+observed-cost",
        )
        card_transition = Plan2NativeTransition(
            card_transition.before,
            card_transition.action,
            card_after,
            (*card_transition.trace, "external-cost:exam-save+hud"),
            (),
            operation_receipts=card_transition.operation_receipts,
        )
    transition = simulate_plan2_native_action_lifecycle(
        before_horizon, action, catalog
    )
    if not transition.supported or transition.after is None:
        detail = ",".join(
            value.code + ("=" + value.detail if value.detail else "")
            for value in transition.blockers
        )
        _reject("plan2-card-history-lifecycle-transition-blocked", detail)
    after = transition.after
    if external_cost_modifier:
        after = replace(
            after,
            scalar=replace(after.scalar, stamina=new.stamina, block=new.block),
            source_kind=f"{after.source_kind}+observed-cost",
        )
        transition = Plan2NativeTransition(
            transition.before,
            transition.action,
            after,
            (*transition.trace, "external-cost:exam-save+hud"),
            (),
            operation_receipts=transition.operation_receipts,
        )
    if (
        card_after.command_queue
        or card_after.zones.pending_played is not None
        or after.command_queue
        or after.zones.pending_played is not None
    ):
        _reject("plan2-card-history-logical-after-unsettled")
    crossed_automatic_turn = (
        "automatic-lifecycle:zero-play" in transition.trace
    )
    observation_is_same_raw_turn = bool(
        logical_observation is not None
        and crossed_automatic_turn
        and logical_observation.turns_remaining == card_after.remaining_turns
        and logical_observation.turns_remaining != after.remaining_turns
    )
    observation_proves_same_turn_open = bool(
        retained_final_remaining_plays > 0
        and
        observation_is_same_raw_turn
        and not new.is_turn_card_play_end
    )
    observation_explicitly_proves_another_play = bool(
        retained_final_remaining_plays > 0
        and
        logical_observation is not None
        and observation_is_same_raw_turn
        and logical_observation.plays_remaining is not None
        and logical_observation.plays_remaining > 0
    )
    if (
        observation_proves_same_turn_open
        or observation_explicitly_proves_another_play
    ):
        # An external item/status may add playability after the direct card
        # effects.  An explicit positive Maa HUD count is ideal.  When that
        # small number is unreadable, a stable same-turn Maa frame plus the
        # native ``isTurnCardPlayEnd == false`` flag proves at least one more
        # action without guessing a larger count.  Re-read after that action.
        remaining = (
            logical_observation.plays_remaining
            if logical_observation is not None
            and logical_observation.plays_remaining is not None
            and logical_observation.plays_remaining > 0
            else 1
        )
        after = replace(
            card_after,
            plays_remaining=remaining,
            source_kind=f"{card_after.source_kind}+observed-same-turn-open",
        )
        transition = Plan2NativeTransition(
            card_transition.before,
            card_transition.action,
            after,
            (
                *card_transition.trace,
                f"observation:same-turn-open:plays={remaining}",
            ),
            (),
            operation_receipts=card_transition.operation_receipts,
        )
        crossed_automatic_turn = False
    # ``isTurnCardPlayEnd`` is a phase-local flag in this retained, non-empty
    # command queue.  It may still be false before UseCardAfterCheck/TurnCheck
    # even though the completed logical transaction has zero plays and will
    # automatically advance.  Do not compare that intermediate flag with the
    # completed horizon.  Explicit Maa ``plays_remaining`` above is the only
    # same-frame authority that can keep the turn open.
    destination = (
        card_after.zones.grave
        if program.move_destination == "grave"
        else card_after.zones.lost
    )
    settled_matches = tuple(card for card in destination if card.guid == action.card_guid)
    # Native ``MovePlayCard`` resets the Hand-only support upgrade before the
    # played instance is appended to Grave/Lost.  The ordered-zone executor
    # already models that exact lifecycle in ``append_played_to_*``; compare
    # against the same reset instance instead of demanding that the temporary
    # Hand support marker survives outside Hand.
    expected_settled = _settled_card_from_play_count_receipts(
        selected,
        card_transition.operation_receipts,
    )
    if len(settled_matches) != 1 or settled_matches[0] != expected_settled:
        _reject("plan2-card-history-logical-destination-mismatch")
    external_item_runtime = False
    try:
        fired, item_effect_ids, item_source_fire_counts = _prove_item_runtime(
            new,
            before_horizon,
            card_after,
            selected,
            card_transition,
            native_item_groups=native_item_groups,
            prior_replay=prior_replay,
        )
    except _ReplayRejected as rejected:
        if (
            logical_observation is None
            or rejected.issue.code
            != "plan2-card-history-item-runtime-unresolved"
        ):
            raise
        # Only an already-proven logical replay snapshot may bridge an
        # external runtime.  Maa/HUD diagnostics fail closed here and the
        # completion policy can run the whole exam without manufacturing an
        # after-state.
        fired = ()
        item_effect_ids = ()
        item_source_fire_counts = _best_effort_item_source_fire_counts(new)
        external_item_runtime = True
    if logical_observation is not None:
        observation_is_pre_turn_check = bool(
            retained_final_remaining_plays == 0
            and crossed_automatic_turn
            and logical_observation.turns_remaining
            == card_after.remaining_turns
            and logical_observation.turns_remaining != after.remaining_turns
        )
        if observation_is_pre_turn_check:
            # Maa can capture the rendered card transaction while the native
            # queue is still before TurnCheck.  Its scalar values own that
            # intermediate post-card boundary (including external item/cost
            # effects), while the exact final native command count of zero
            # owns the automatic EndTurn.  Rebase the same-turn card result,
            # force only that proven zero play count, then run the ordinary
            # typed EndTurn/TurnStart lifecycle.  Stale HUD ``plays=1`` must
            # never reopen the turn during an animation.
            observed_card_transition = (
                _rebase_completed_transition_from_observation(
                    card_transition,
                    replace(logical_observation, plays_remaining=0),
                )
            )
            assert observed_card_transition.after is not None
            automatic = simulate_plan2_native_action(
                observed_card_transition.after,
                Plan2NativeAction("end_turn"),
                catalog,
            )
            if not automatic.supported or automatic.after is None:
                detail = ",".join(
                    value.code + ("=" + value.detail if value.detail else "")
                    for value in automatic.blockers
                )
                _reject(
                    "plan2-card-history-observed-turn-lifecycle-blocked",
                    detail,
                )
            after = automatic.after
            transition = Plan2NativeTransition(
                card_transition.before,
                card_transition.action,
                after,
                (
                    *observed_card_transition.trace,
                    "automatic-lifecycle:retained-command-zero",
                    *automatic.trace,
                ),
                (),
                operation_receipts=card_transition.operation_receipts,
            )
        else:
            transition = _rebase_completed_transition_from_observation(
                transition,
                logical_observation,
            )
            assert transition.after is not None
            after = transition.after
    if observation is not None and logical_observation is None:
        transition = _rebase_completed_transition_from_observation(
            transition,
            observation,
        )
        assert transition.after is not None
        after = transition.after
    return Plan2CompletedCardReplay(
        action=action,
        card_guid=selected.guid,
        card_id=selected.card_id,
        command_effect_ids=command_effect_ids,
        program_effect_ids=program_effect_ids,
        fired_item_enchantment_ids=fired,
        item_effect_ids=item_effect_ids,
        persisted_item_source_fire_counts=item_source_fire_counts,
        remaining_plays=after.plays_remaining,
        before=before_horizon,
        after=after,
        transition=transition,
        persisted_before=persisted_before,
        persisted_transition=persisted_transition,
        trace=(
            f"card-history:action:{action.action_id}",
            "card-history:command-grammar:9,master-effect-subsequence,13,6,9",
            f"card-history:post-cost:stamina={new.stamina}:block={new.block}",
            *(
                ("card-history:external-cost:exam-save+hud",)
                if external_cost_modifier
                else ()
            ),
            *(
                ("card-history:retained-pre-effect-score",)
                if retained_pre_effect_score
                else ()
            ),
            *(
                (
                    "card-history:retained-prior-command-materialized:"
                    + ",".join(retained_prior_materialized_effect_ids),
                )
                if retained_prior_materialization
                else ()
            ),
            *(
                ("card-history:retained-prior-local-save-score-rebased",)
                if retained_prior_observed_score
                else ()
            ),
            *(
                (
                    "card-history:retained-prior-extra-turn-schedule-"
                    "materialized",
                )
                if retained_prior_schedule_materialized
                else ()
            ),
            *(
                (
                    "card-history:retained-prior-drink-local-save-scalar-"
                    "rebased",
                )
                if retained_prior_drink_scalar_materialization
                else ()
            ),
            *(
                f"card-history:{value}"
                for value in runtime_customization_trace
            ),
            *(
                ("card-history:prior-drink-tombstones-carried",)
                if prior_drink_tombstones_carried
                else ()
            ),
            *(
                ("card-history:prior-card-upgrade-tombstone-carried",)
                if deferred_prior_card_upgrade_tombstone
                else ()
            ),
            *(
                (
                    "card-history:stable-positioned-tombstones:"
                    f"{len(new.removed_cards)}",
                )
                if new.removed_cards
                else ()
            ),
            "card-history:item-source-fire-counts:"
            + ",".join(
                f"{item_id}={fire_count}"
                for item_id, fire_count in item_source_fire_counts
            ),
            *(
                ("card-history:item-runtime:screen-observed",)
                if external_item_runtime
                else ()
            ),
            *(
                (
                    "card-history:item-runtime:materialized-after-observed-prior",
                    "card-history:item-runtime:native-status-queue-exact",
                )
                if external_item_runtime_materialized
                else ()
            ),
            f"card-history:logical-after:plays={after.plays_remaining}:"
            f"rng={after.zones.random_state}",
            *transition.trace,
        ),
    )


def replay_completed_plan2_card_history(
    persisted_before: AuditionLocalSaveStateEvidence,
    persisted_transition: AuditionLocalSaveStateEvidence,
    action: Plan2NativeAction,
    *,
    before_horizon: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
    database: str | Path = DEFAULT_DATABASE,
    observation: Plan2CardHistoryObservation | None = None,
) -> Plan2CardHistoryReplayResult:
    """Prove one retained Plan2 card queue and return its logical after horizon.

    ``persisted_transition`` must be the stable post-cost/pre-effect snapshot,
    not an OCR or screen-derived value.  ``before_horizon`` must retain the
    exact LocalSave evidence binding produced by the Plan2 bootstrap.
    """

    if not isinstance(persisted_before, AuditionLocalSaveStateEvidence):
        raise TypeError("persisted_before must be AuditionLocalSaveStateEvidence")
    if not isinstance(persisted_transition, AuditionLocalSaveStateEvidence):
        raise TypeError(
            "persisted_transition must be AuditionLocalSaveStateEvidence"
        )
    if not isinstance(action, Plan2NativeAction):
        raise TypeError("action must be Plan2NativeAction")
    if not isinstance(before_horizon, Plan2NativeHorizonState):
        raise TypeError("before_horizon must be Plan2NativeHorizonState")
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be Plan2NativeProgramCatalog")
    if observation is not None and not isinstance(
        observation, Plan2CardHistoryObservation
    ):
        raise TypeError("observation must be Plan2CardHistoryObservation or None")
    database_path = Path(database)
    try:
        replay = _replay(
            persisted_before,
            persisted_transition,
            action,
            before_horizon=before_horizon,
            catalog=catalog,
            database=database_path,
            observation=observation,
        )
    except _ReplayRejected as rejected:
        return Plan2CardHistoryReplayResult(None, (rejected.issue,))
    except (OSError, OverflowError, TypeError, ValueError) as error:
        return Plan2CardHistoryReplayResult(
            None,
            (
                Plan2CardHistoryIssue(
                    "plan2-card-history-replay-failed-closed",
                    f"{type(error).__name__}:{error}",
                ),
            ),
        )
    return Plan2CardHistoryReplayResult(replay)


def _empty_drink_playing_card(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != _RAW_CARD_FIELDS:
        return False
    card_data = value.get("_cardData")
    status = value.get("_statusEffect")
    return bool(
        value.get("_affectGrowEffectIdList") == []
        and value.get("_baseUpgradeCount") == 0
        and isinstance(card_data, Mapping)
        and card_data
        == {
            "_customizeCountList": [],
            "_id": "",
            "_produceCardSkinAssetId": "",
            "_produceCardSkinId": "",
            "_upgradeCount": 0,
        }
        and value.get("_fixedDeckOrder") == 0
        and value.get("_growEffectExamStartAfterList") == []
        and value.get("_guid") == ""
        and value.get("_isMoveProduceExamEffectUseInTurn") is False
        and value.get("_playCount") == 0
        and value.get("_staminaConsumptionSpecifyEffectList") == []
        and isinstance(status, Mapping)
        and status
        == {
            "_effectGroupIdList": [],
            "_id": "",
            "_phaseCountDictionary": {"_list": []},
            "_produceCardGrowEffectIdList": [],
            "_produceExamTriggerId": "",
            "_spendCount": 0,
            "_spendTurn": 0,
            "_triggerCount": 0,
        }
        and value.get("_supportUpgradeIdList") == []
        and value.get("_tmpUpgradeCount") == 0
    )


def _drink_command_has_exact_neutral_context(
    command: object,
    *,
    effect: Mapping[str, object],
) -> bool:
    if not isinstance(command, Mapping) or set(command) != _COMMAND_FIELDS:
        return False
    return bool(
        set(effect) == _PLAY_EFFECT_FIELDS
        and command.get("_assetId") == ""
        and command.get("_cardGuids") == []
        and command.get("_cardSelectSearchId") == effect.get("_cardSearchId")
        and command.get("_cardSelectSearchId2") == effect.get("_cardSearchId2")
        and command.get("_effectTriggerId") == ""
        and command.get("_enchantEffectUid") == 0
        and command.get("_isCardSelect") is False
        and command.get("_isCardSelect2") is False
        and command.get("_isConsumeCost") is False
        and command.get("_isManual") is False
        and command.get("_isPlayingGimmick") is False
        and command.get("_isPlayingMoveCardEffect") is False
        and command.get("_isSkipForCalculateForecast") is False
        and command.get("_isUsePlayableCardCount") is False
        and command.get("_phaseType") == 0
        and command.get("_playCardPositionType") == 0
        and command.get("_playIndex") == 0
        and command.get("_playEffect") == effect
        and _empty_drink_playing_card(command.get("_playingCard"))
        and command.get("_playingEnchantIsTriggerActive") is False
        and command.get("_playingGimmick") == _EMPTY_PLAYING_GIMMICK
        and command.get("_playingItem") == _EMPTY_PLAYING_ITEM
        and command.get("_remainCanPlayCardCount") == 0
        and command.get("_selectIndex") == []
        and command.get("_startEnchantOriginId") == ""
        and command.get("_startEnchantOriginLevel") == 0
        and command.get("_startEnchantOwnerId") == ""
        and command.get("_supportCardIds") == []
        and command.get("_triggeredGrowEffectAffectLists") == []
        and command.get("_useEffectUidList") == []
    )


def _exact_retained_drink_commit_queue(
    rows: Sequence[object],
    *,
    drink_id: str,
    master_effect_refs: Sequence[object],
    allow_consumed_start_suffix: bool = False,
) -> bool:
    """Prove the native DrinkStart/effects/DrinkEnd/queue-end transaction."""

    has_start = len(rows) == len(master_effect_refs) + 3
    consumed_start_suffix = bool(
        allow_consumed_start_suffix
        and len(rows) == len(master_effect_refs) + 2
    )
    if not has_start and not consumed_start_suffix:
        return False
    start = rows[0] if has_start else None
    effect_rows = rows[1:-2] if has_start else rows[:-2]
    end, final = rows[-2:]
    if not all(isinstance(value, Mapping) for value in rows):
        return False
    assert isinstance(end, Mapping)
    assert isinstance(final, Mapping)
    if not all(
        _drink_command_has_exact_neutral_context(value, effect=_EMPTY_PLAY_EFFECT)
        for value in ((start, end, final) if has_start else (end, final))
    ):
        return False
    if not (
        end.get("_playType") == 10
        and end.get("_isSeparateStart") is False
        and end.get("_originEffectIndex") == 0
        and end.get("_startEnchantOriginType") == 7
        and end.get("_playingDrink") == {"_id": drink_id}
        and final.get("_playType") == 9
        and final.get("_isSeparateStart") is False
        and final.get("_originEffectIndex") == 0
        and final.get("_startEnchantOriginType") == 0
        and final.get("_playingDrink") == _EMPTY_PLAYING_DRINK
    ):
        return False
    if has_start:
        assert isinstance(start, Mapping)
        if not (
            start.get("_playType") == 10
            and start.get("_isSeparateStart") is True
            and start.get("_originEffectIndex") == 0
            and start.get("_startEnchantOriginType") == 7
            and start.get("_playingDrink") == {"_id": drink_id}
        ):
            return False
    for index, (command, reference) in enumerate(
        zip(effect_rows, master_effect_refs, strict=True)
    ):
        if not isinstance(command, Mapping):
            return False
        effect = command.get("_playEffect")
        master_effect = getattr(reference, "effect", None)
        raw_json = getattr(master_effect, "raw_json", None)
        effect_id = getattr(master_effect, "source_id", None)
        try:
            raw = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError):
            return False
        if (
            not isinstance(effect, Mapping)
            or not isinstance(raw, Mapping)
            or not isinstance(effect_id, str)
            or not _drink_command_has_exact_neutral_context(command, effect=effect)
            or command.get("_playType") != 5
            or command.get("_isSeparateStart") is not False
            or command.get("_originEffectIndex") != index
            or command.get("_startEnchantOriginType") != 0
            or command.get("_playingDrink") != {"_id": drink_id}
            or not _serialized_effect_matches(
                effect,
                _MasterEffectSpec(effect_id, raw, True),
            )
        ):
            return False
    return True


def _exact_relative_drink_user_play_log_append(
    before: AuditionLocalSaveStateEvidence,
    current: AuditionLocalSaveStateEvidence,
    *,
    drink_id: str,
) -> bool:
    """Prove the one native user-log row emitted by a completed DRINK."""

    def rows_from(value: AuditionLocalSaveStateEvidence) -> object:
        runtime = value.state.root_runtime
        opaque = None if runtime is None else runtime.opaque_fields.to_value()
        return opaque.get("userPlayLogList") if isinstance(opaque, Mapping) else None

    def projected_actions(rows: object) -> tuple[str, ...] | None:
        if not isinstance(rows, list):
            return None
        result: list[str] = []
        for row in rows:
            if not isinstance(row, Mapping) or row.get("_cellType") != 4:
                continue
            trigger_id = row.get("_triggerId")
            expected = {
                "_bonusPermil": 0,
                "_cellType": 4,
                "_currentTurn": 0,
                "_detailList": [],
                "_drawCardCustomizeList": [],
                "_drawCardList": [],
                "_drawCardSkinIdList": [],
                "_drawCardUpgradeList": [],
                "_extraTurn": 0,
                "_hasTriggerProduceCardSkin": False,
                "_limitTurn": 0,
                "_playCardCustomizeList": [],
                "_playCardList": [],
                "_playCardSkinIdList": [],
                "_playCardUpgradeList": [],
                "_triggerEffect": {"rid": -2},
                "_triggerId": trigger_id,
                "_triggerOwnerId": "",
                "_triggerProduceCardSkinId": "",
                "_triggerSubscriptionBoolean": False,
                "_triggerSubscriptionNumber": 0,
                "_triggerSubscriptionNumberList": [],
                "_triggerUid": 0,
                "_turnTypeList": [],
            }
            if (
                not isinstance(trigger_id, str)
                or not trigger_id
                or row != expected
            ):
                return None
            result.append(trigger_id)
        return tuple(result)

    old_actions = projected_actions(rows_from(before))
    new_actions = projected_actions(rows_from(current))
    return bool(
        old_actions is not None
        and new_actions is not None
        and new_actions == (*old_actions, drink_id)
    )


def _normalize_prior_drink_card_tombstones(
    state: LocalSaveExamState,
    *,
    persisted_before: AuditionLocalSaveStateEvidence | None,
    before_horizon: Plan2NativeHorizonState | None,
) -> LocalSaveExamState:
    """Remove only tombstones exactly explained by the preceding drink replay."""

    def local_positions(
        source: LocalSaveExamState,
    ) -> tuple[tuple[str, int, LocalSaveExamCard], ...]:
        return tuple(
            (zone, index, card)
            for zone in ("hand", "deck", "grave", "lost")
            for index, card in enumerate(getattr(source.zones, zone))
        )

    def native_positions() -> tuple[tuple[str, int, NativeOrderedCardInstance], ...]:
        zones = before_horizon.zones
        return tuple(
            (zone, index, card)
            for zone in ("hand", "deck", "grave", "lost")
            for index, card in enumerate(getattr(zones, zone))
        )

    old_removed = (
        () if persisted_before is None else persisted_before.state.removed_cards
    )
    if old_removed:
        # A prior card/drink can leave a positioned tombstone that survives
        # unchanged across this drink.  It is durable history, not evidence
        # that this drink upgraded the card again.  Bind the complete ordered
        # tombstone collection byte-for-byte, then independently bind its one
        # live instance across old/current/logical authority before excluding
        # the history-only collection from the drink transition.
        if before_horizon is None:
            _reject("plan2-drink-history-transition-boundary-invalid")
        old_state = persisted_before.state
        if (
            state.removed_cards != old_removed
            or not _removed_card_tombstones_are_positioned_for_replay(old_state)
            or not _removed_card_tombstones_are_positioned_for_replay(state)
        ):
            _reject(
                "plan2-drink-history-prior-tombstone-mismatch",
                "stable-history-drift",
            )
        persisted_positions = local_positions(old_state)
        current_positions = local_positions(state)
        logical_positions = native_positions()
        for removed in state.removed_cards:
            old_matches = tuple(
                value
                for value in persisted_positions
                if value[2].guid == removed.guid
            )
            current_matches = tuple(
                value
                for value in current_positions
                if value[2].guid == removed.guid
            )
            logical_matches = tuple(
                value
                for value in logical_positions
                if value[2].guid == removed.guid
            )
            if not (
                len(old_matches) == len(current_matches) == len(logical_matches) == 1
            ):
                _reject(
                    "plan2-drink-history-prior-tombstone-mismatch",
                    f"{removed.guid}:stable-live-not-unique",
                )
            old_zone, old_index, old_card = old_matches[0]
            current_zone, current_index, current_card = current_matches[0]
            logical_zone, logical_index, logical_card = logical_matches[0]
            try:
                old_native = NativeOrderedCardInstance.from_local_save(old_card)
                current_native = NativeOrderedCardInstance.from_local_save(
                    current_card
                )
            except (TypeError, ValueError) as error:
                _reject(
                    "plan2-drink-history-prior-tombstone-mismatch",
                    f"{removed.guid}:{type(error).__name__}:{error}",
                )
            if (
                not (
                    (old_zone, old_index)
                    == (current_zone, current_index)
                    == (logical_zone, logical_index)
                )
                or old_native != current_native
                or current_native != logical_card
            ):
                _reject(
                    "plan2-drink-history-prior-tombstone-mismatch",
                    f"{removed.guid}:stable-old={old_zone}[{old_index}]:"
                    f"current={current_zone}[{current_index}]:"
                    f"logical={logical_zone}[{logical_index}]",
                )
        return replace(state, removed_cards=())

    if not state.removed_cards:
        return state
    if persisted_before is None or before_horizon is None:
        _reject("plan2-drink-history-transition-boundary-invalid")
    removed_guids = tuple(card.guid for card in state.removed_cards)
    if len(removed_guids) != len(set(removed_guids)):
        _reject("plan2-drink-history-prior-tombstone-mismatch", "duplicate-guid")

    persisted_positions = local_positions(persisted_before.state)
    current_positions = local_positions(state)
    logical_positions = native_positions()
    for removed in state.removed_cards:
        old_matches = tuple(
            value for value in persisted_positions if value[2].guid == removed.guid
        )
        current_matches = tuple(
            value for value in current_positions if value[2].guid == removed.guid
        )
        logical_matches = tuple(
            value for value in logical_positions if value[2].guid == removed.guid
        )
        if not (
            len(old_matches) == len(current_matches) == len(logical_matches) == 1
        ):
            _reject(
                "plan2-drink-history-prior-tombstone-mismatch",
                removed.guid,
            )
        old_zone, old_index, old_card = old_matches[0]
        current_zone, current_index, current_card = current_matches[0]
        logical_zone, logical_index, logical_card = logical_matches[0]
        try:
            removed_native = NativeOrderedCardInstance.from_local_save(removed)
            old_native = NativeOrderedCardInstance.from_local_save(old_card)
            current_native = NativeOrderedCardInstance.from_local_save(current_card)
        except (TypeError, ValueError) as error:
            _reject(
                "plan2-drink-history-prior-tombstone-mismatch",
                f"{removed.guid}:{type(error).__name__}:{error}",
            )
        if (
            removed_native != old_native
            or (current_zone, current_index) != (logical_zone, logical_index)
            or current_native != logical_card
        ):
            _reject(
                "plan2-drink-history-prior-tombstone-mismatch",
                f"{removed.guid}:old={old_zone}[{old_index}]:"
                f"current={current_zone}[{current_index}]:"
                f"logical={logical_zone}[{logical_index}]",
            )
    return replace(state, removed_cards=())


def recover_retained_plan2_drink_history(
    evidence: AuditionLocalSaveStateEvidence,
    *,
    observation: Plan2CardHistoryObservation,
    catalog: Plan2NativeProgramCatalog | None = None,
    database: str | Path = DEFAULT_DATABASE,
    expected_action: Plan2NativeDrinkAction | None = None,
    persisted_before: AuditionLocalSaveStateEvidence | None = None,
    before_horizon: Plan2NativeHorizonState | None = None,
) -> Plan2DrinkHistoryReplayResult:
    """Recover one game-owned drink command queue without a settled save.

    The retained ExamSave already contains the fully materialized result of any
    earlier card.  Only its exact ``playingDrink`` command bucket is replayed;
    the command list is never discarded for an unknown shape.
    """

    if not isinstance(evidence, AuditionLocalSaveStateEvidence):
        raise TypeError("evidence must be typed")
    if not isinstance(observation, Plan2CardHistoryObservation):
        raise TypeError("observation must be typed")
    if expected_action is not None and not isinstance(
        expected_action, Plan2NativeDrinkAction
    ):
        raise TypeError("expected_action must be typed or None")
    if persisted_before is not None and not isinstance(
        persisted_before, AuditionLocalSaveStateEvidence
    ):
        raise TypeError("persisted_before must be typed or None")
    if before_horizon is not None and not isinstance(
        before_horizon, Plan2NativeHorizonState
    ):
        raise TypeError("before_horizon must be typed or None")

    def drink_ids_from(value: AuditionLocalSaveStateEvidence) -> tuple[str, ...]:
        runtime = value.state.root_runtime
        opaque = None if runtime is None else runtime.opaque_fields.to_value()
        rows = opaque.get("drinkList") if isinstance(opaque, Mapping) else None
        if not isinstance(rows, list):
            _reject("plan2-drink-history-inventory-shape-invalid")
        result: list[str] = []
        for index, row in enumerate(rows):
            drink_id = row.get("_id") if isinstance(row, Mapping) else None
            if not isinstance(drink_id, str) or not drink_id:
                _reject(
                    "plan2-drink-history-inventory-shape-invalid",
                    str(index),
                )
            result.append(drink_id)
        return tuple(result)

    try:
        from .plan2_native_exam_save_orchestrator import (
            decide_plan2_native_exam_save,
        )
        from .plan2_native_program_catalog import (
            compile_plan2_native_program_catalog,
        )

        state = _normalize_prior_drink_card_tombstones(
            evidence.state,
            persisted_before=persisted_before,
            before_horizon=before_horizon,
        )
        runtime = state.root_runtime
        rows = None if runtime is None else runtime.command_list.to_value()
        if (
            runtime is None
            or state.phase != 6
            or state.playing_card is not None
            or runtime.command_list_is_empty
            or runtime.draw_card_guid_list
            or runtime.is_exam_end_complete
            or state.zones.hold
            or not isinstance(rows, list)
        ):
            _reject("plan2-drink-history-transition-boundary-invalid")

        has_start_row = bool(
            len(rows) >= 4
            and isinstance(rows[0], Mapping)
            and rows[0].get("_playType") == 10
            and isinstance(rows[0].get("_playingDrink"), Mapping)
            and bool(rows[0]["_playingDrink"].get("_id"))
        )
        effect_rows = rows[1:-2] if has_start_row else rows[:-2]
        if not effect_rows or len(rows) != len(effect_rows) + 2:
            if not has_start_row:
                _reject("plan2-drink-history-command-grammar-invalid")
        if has_start_row and len(rows) != len(effect_rows) + 3:
            _reject("plan2-drink-history-command-grammar-invalid")
        drink_ids: list[str] = []
        observed_effect_ids: list[str] = []
        for index, row in enumerate(effect_rows):
            if not isinstance(row, Mapping):
                _reject("plan2-drink-history-command-shape-invalid", str(index))
            playing_drink = row.get("_playingDrink")
            effect = row.get("_playEffect")
            drink_id = (
                playing_drink.get("_id")
                if isinstance(playing_drink, Mapping)
                else None
            )
            effect_id = effect.get("_id") if isinstance(effect, Mapping) else None
            if (
                row.get("_playType") != 5
                or row.get("_isManual") is not False
                or not isinstance(drink_id, str)
                or not drink_id
                or not isinstance(effect_id, str)
                or not effect_id
            ):
                _reject("plan2-drink-history-command-shape-invalid", str(index))
            drink_ids.append(drink_id)
            observed_effect_ids.append(effect_id)
        if len(set(drink_ids)) != 1:
            _reject("plan2-drink-history-command-drink-mismatch")
        drink_id = drink_ids[0]
        if has_start_row:
            start_identity = rows[0].get("_playingDrink")
            if (
                not isinstance(start_identity, Mapping)
                or start_identity.get("_id") != drink_id
            ):
                _reject("plan2-drink-history-command-drink-mismatch")
        end_drink, end_queue = rows[-2], rows[-1]
        if not isinstance(end_drink, Mapping) or not isinstance(end_queue, Mapping):
            _reject("plan2-drink-history-command-grammar-invalid")
        end_drink_identity = end_drink.get("_playingDrink")
        final_drink_identity = end_queue.get("_playingDrink")
        if (
            end_drink.get("_playType") != 10
            or not isinstance(end_drink_identity, Mapping)
            or end_drink_identity.get("_id") != drink_id
            or end_queue.get("_playType") != 9
            or not isinstance(final_drink_identity, Mapping)
            or final_drink_identity.get("_id") not in {"", None}
        ):
            _reject("plan2-drink-history-command-grammar-invalid")

        clean_runtime = replace(
            runtime,
            command_list=CanonicalJsonValue.from_value(
                [], "plan2-drink-history-clean-command-list"
            ),
        )
        clean_evidence = replace(
            evidence,
            state=replace(state, root_runtime=clean_runtime),
        )
        if not clean_evidence.state.is_native_actionable_settled:
            _reject("plan2-drink-history-clean-boundary-invalid")
        if catalog is None:
            catalog = compile_plan2_native_program_catalog(database=database).catalog
        if not isinstance(catalog, Plan2NativeProgramCatalog):
            raise TypeError("catalog must be typed")

        from .initial_regular_plan2_drink_runtime import (
            compile_plan2_native_drink_instance,
        )
        from .drink_catalog import load_drink_catalog

        drink_catalog = load_drink_catalog(database=database)
        master_drink = drink_catalog.get_drink(drink_id)
        compilation = compile_plan2_native_drink_instance(
            master_drink,
            instance_id=f"materialized-drink:{drink_id}",
        )
        if not compilation.supported or compilation.instance is None:
            detail = ",".join(value.code for value in compilation.blockers)
            _reject("plan2-drink-history-master-unavailable", detail or drink_id)
        instance = compilation.instance
        expected_effect_ids = tuple(value.effect_id for value in instance.effects)
        if tuple(observed_effect_ids) != expected_effect_ids:
            _reject(
                "plan2-drink-history-effect-order-mismatch",
                f"expected={expected_effect_ids!r};actual={tuple(observed_effect_ids)!r}",
            )
        exact_commit_queue = _exact_retained_drink_commit_queue(
            rows,
            drink_id=drink_id,
            master_effect_refs=master_drink.effect_refs,
        )
        exact_consumed_start_suffix = bool(
            not has_start_row
            and _exact_retained_drink_commit_queue(
                rows,
                drink_id=drink_id,
                master_effect_refs=master_drink.effect_refs,
                allow_consumed_start_suffix=True,
            )
        )
        commit_from_consumed_start_suffix = False
        current_drink_ids = drink_ids_from(clean_evidence)
        inventory_retained_pre_effect = False
        slot_authority: int
        if expected_action is not None and persisted_before is not None:
            before_state = persisted_before.state
            current_state = evidence.state
            if (
                persisted_before.run_id != evidence.run_id
                or persisted_before.step_context_id != evidence.step_context_id
                or persisted_before.source_path != evidence.source_path
                or persisted_before.source_type != evidence.source_type
                or before_state.character_id != current_state.character_id
                or before_state.setting_id != current_state.setting_id
                or before_state.exam_type != current_state.exam_type
                or before_state.step_type_value != current_state.step_type_value
                or before_state.phase != current_state.phase
                or before_state.current_turn != current_state.current_turn
            ):
                _reject("plan2-drink-history-receipt-identity-mismatch")
            previous_drink_ids = drink_ids_from(persisted_before)
            slot_authority = expected_action.slot_index
            if (
                not 0 <= slot_authority < len(previous_drink_ids)
                or previous_drink_ids[slot_authority] != drink_id
            ):
                _reject(
                    "plan2-drink-history-action-mismatch",
                    f"slot={slot_authority};before={previous_drink_ids!r};"
                    f"drink={drink_id}",
                )
            expected_current = (
                previous_drink_ids[:slot_authority]
                + previous_drink_ids[slot_authority + 1 :]
            )
            if current_drink_ids == previous_drink_ids:
                inventory_retained_pre_effect = True
            elif current_drink_ids != expected_current:
                _reject(
                    "plan2-drink-history-inventory-identity-invalid",
                    f"expected-pre={previous_drink_ids!r};"
                    f"expected-post={expected_current!r};"
                    f"actual={current_drink_ids!r}",
                )
            elif (
                not exact_commit_queue
                and exact_consumed_start_suffix
                and _exact_relative_drink_user_play_log_append(
                    persisted_before,
                    evidence,
                    drink_id=drink_id,
                )
            ):
                # Production can persist after DrinkStart was consumed but
                # before the complete Master effect suffix was animated.  The
                # durable receipt supplies the exact action/slot; native log
                # history supplies the irreversible submission proof.
                exact_commit_queue = True
                commit_from_consumed_start_suffix = True
            if not exact_commit_queue:
                _reject("plan2-drink-history-input-not-committed", drink_id)
        elif expected_action is not None:
            slot_authority = expected_action.slot_index
            if (
                0 <= slot_authority < len(current_drink_ids)
                and current_drink_ids[slot_authority] == drink_id
            ):
                if not exact_commit_queue:
                    _reject("plan2-drink-history-input-not-committed", drink_id)
                inventory_retained_pre_effect = True
            elif drink_id in current_drink_ids:
                _reject(
                    "plan2-drink-history-action-mismatch",
                    f"slot={slot_authority};current={current_drink_ids!r}",
                )
        else:
            matching_slots = tuple(
                index
                for index, value in enumerate(current_drink_ids)
                if value == drink_id
            )
            if len(matching_slots) == 1:
                _reject(
                    "plan2-drink-history-action-receipt-required",
                    f"drink={drink_id};slot={matching_slots[0]}",
                )
            elif len(matching_slots) > 1:
                _reject(
                    "plan2-drink-history-slot-authority-ambiguous",
                    f"drink={drink_id};inventory={current_drink_ids!r}",
                )
            elif not current_drink_ids:
                # A fully removed empty inventory proves the consumed drink
                # was the sole slot, preserving the legacy post-effect path.
                slot_authority = 0
            else:
                _reject(
                    "plan2-drink-history-slot-authority-missing",
                    f"drink={drink_id};inventory={current_drink_ids!r}",
                )

        action = expected_action or Plan2NativeDrinkAction(
            slot_authority,
            instance.instance_id,
            instance.drink_id,
        )
        if action.drink_id != drink_id or action.slot_index != slot_authority:
            _reject(
                "plan2-drink-history-action-mismatch",
                f"expected={action.action_id};actual-slot={slot_authority}:"
                f"{drink_id}",
            )

        before = before_horizon
        if before is None and persisted_before is not None:
            previous = decide_plan2_native_exam_save(
                persisted_before,
                database=database,
            )
            before = previous.logical_root
            if before is None and previous.bootstrap is not None:
                before = previous.bootstrap.state
        if before is None:
            decision = decide_plan2_native_exam_save(
                clean_evidence,
                database=database,
            )
            before = decision.logical_root
            if before is None and decision.bootstrap is not None:
                before = decision.bootstrap.state
            if before is None:
                detail = ",".join(value.code for value in decision.blockers)
                _reject("plan2-drink-history-bootstrap-blocked", detail)
            if inventory_retained_pre_effect:
                if not 0 <= slot_authority < len(before.drink_runtime.inventory):
                    _reject("plan2-drink-history-slot-authority-missing")
                retained_instance = before.drink_runtime.inventory[slot_authority]
                if retained_instance.drink_id != drink_id:
                    _reject("plan2-drink-history-action-mismatch", drink_id)
                if expected_action is not None and (
                    retained_instance.instance_id != expected_action.instance_id
                ):
                    _reject(
                        "plan2-drink-history-action-mismatch",
                        f"expected-instance={expected_action.instance_id};"
                        f"actual={retained_instance.instance_id}",
                    )
                action = Plan2NativeDrinkAction(
                    slot_authority,
                    retained_instance.instance_id,
                    retained_instance.drink_id,
                )
            else:
                # The post-effect snapshot already removed the slot.  Restore
                # exactly the caller/sole-slot authority for one simulation.
                restored = (
                    instance
                    if expected_action is None
                    else replace(instance, instance_id=expected_action.instance_id)
                )
                if not 0 <= slot_authority <= len(before.drink_runtime.inventory):
                    _reject("plan2-drink-history-slot-authority-missing")
                inventory = before.drink_runtime.inventory
                before = replace(
                    before,
                    drink_runtime=replace(
                        before.drink_runtime,
                        inventory=(
                            *inventory[:slot_authority],
                            restored,
                            *inventory[slot_authority:],
                        ),
                    ),
                )
                action = Plan2NativeDrinkAction(
                    slot_authority,
                    restored.instance_id,
                    restored.drink_id,
                )

        if not 0 <= action.slot_index < len(before.drink_runtime.inventory):
            _reject("plan2-drink-history-slot-authority-missing")
        authoritative_instance = before.drink_runtime.inventory[action.slot_index]
        if (
            authoritative_instance.instance_id != action.instance_id
            or authoritative_instance.drink_id != action.drink_id
        ):
            _reject(
                "plan2-drink-history-action-mismatch",
                f"action={action.action_id};runtime="
                f"{authoritative_instance.instance_id}:"
                f"{authoritative_instance.drink_id}",
            )

        transition = simulate_plan2_native_action_lifecycle(
            before, action, catalog
        )
        if not transition.supported or transition.after is None:
            detail = ",".join(
                value.code + (":" + value.detail if value.detail else "")
                for value in transition.blockers
            )
            _reject("plan2-drink-history-horizon-transition-blocked", detail)
        after = transition.after
        commit_proof = (
            None
            if persisted_before is None
            else prove_plan2_native_drink_commit(
                persisted_before,
                evidence,
                action,
                command_effect_ids=tuple(observed_effect_ids),
                exact_master_command_queue_proven=exact_commit_queue,
            )
        )
        replay = Plan2CompletedDrinkReplay(
            action=action,
            before=before,
            after=after,
            transition=transition,
            persisted_transition=evidence,
            command_effect_ids=tuple(observed_effect_ids),
            trace=(
                f"drink-history:action:{action.action_id}",
                f"drink-history:observation:{observation.source}",
                "drink-history:commit-authority:"
                + (
                    "native-user-play-log-tail+exact-master-command-suffix+"
                    "ordered-slot-remove-at"
                    if commit_from_consumed_start_suffix
                    else
                    "exact-retained-command-queue+ordered-slot"
                    if inventory_retained_pre_effect
                    else "post-effect-inventory-removal+ordered-slot"
                ),
                "drink-history:pre-effect-command-replay",
                *transition.trace,
            ),
            persisted_before=persisted_before,
            materialized_from_local_save=False,
            commit_proof=commit_proof,
        )
    except _ReplayRejected as rejected:
        return Plan2DrinkHistoryReplayResult(None, (rejected.issue,))
    except (OSError, OverflowError, TypeError, ValueError) as error:
        return Plan2DrinkHistoryReplayResult(
            None,
            (
                Plan2CardHistoryIssue(
                    "plan2-drink-history-replay-failed-closed",
                    f"{type(error).__name__}:{error}",
                ),
            ),
        )
    return Plan2DrinkHistoryReplayResult(replay)


def replay_next_completed_plan2_card_history(
    prior_persisted_transition: AuditionLocalSaveStateEvidence,
    prior_logical_after: Plan2NativeHorizonState,
    persisted_transition: AuditionLocalSaveStateEvidence,
    action: Plan2NativeAction,
    *,
    prior_replay: Plan2CompletedLogicalReplay,
    catalog: Plan2NativeProgramCatalog,
    database: str | Path = DEFAULT_DATABASE,
    observation: Plan2CardHistoryObservation | None = None,
) -> Plan2CardHistoryReplayResult:
    """Replay the next card while a prior completed queue remains persisted.

    ``prior_persisted_transition`` remains the exact LocalSave authority for
    stage/GUID/visible-zone identity.  ``prior_logical_after`` is the sole
    scalar, status, and remaining-play planning root.  Both values must bind
    ``prior_replay`` exactly, so the previous card can never be charged or
    applied twice.
    """

    if not isinstance(prior_persisted_transition, AuditionLocalSaveStateEvidence):
        raise TypeError("prior_persisted_transition must be typed evidence")
    if not isinstance(prior_logical_after, Plan2NativeHorizonState):
        raise TypeError("prior_logical_after must be a typed horizon")
    if not isinstance(persisted_transition, AuditionLocalSaveStateEvidence):
        raise TypeError("persisted_transition must be typed evidence")
    if not isinstance(action, Plan2NativeAction):
        raise TypeError("action must be Plan2NativeAction")
    if not isinstance(
        prior_replay, (Plan2CompletedCardReplay, Plan2CompletedDrinkReplay)
    ):
        raise TypeError("prior_replay must be a typed completed replay")
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be Plan2NativeProgramCatalog")
    if observation is not None and not isinstance(
        observation, Plan2CardHistoryObservation
    ):
        raise TypeError("observation must be Plan2CardHistoryObservation or None")
    if prior_replay.persisted_transition != prior_persisted_transition:
        return Plan2CardHistoryReplayResult(
            None,
            (Plan2CardHistoryIssue("plan2-card-history-chain-authority-mismatch"),),
        )
    if prior_replay.logical_after != prior_logical_after:
        # The logical orchestrator prepares deterministic generated-card
        # inputs immediately before search.  Permit exactly that metadata-only
        # extension as the replay root; no scalar, zone, runtime or stage field
        # may differ from the prior replay authority.
        if replace(
            prior_logical_after,
            generated_inputs=prior_replay.logical_after.generated_inputs,
        ) != prior_replay.logical_after:
            return Plan2CardHistoryReplayResult(
                None,
                (
                    Plan2CardHistoryIssue(
                        "plan2-card-history-chain-authority-mismatch"
                    ),
                ),
            )
        prior_replay = _replace_replay_logical_after(
            prior_replay,
            prior_logical_after,
            audit=("card-history:predictive-generated-inputs-bound",),
        )
    late_generated_guid_mapping: tuple[tuple[str, str], ...] = ()
    try:
        materialized_before, generated_guid_mapping = (
            _materialize_symbolic_generated_guids(
                prior_logical_after,
                persisted_transition,
                omitted_logical_guids=(action.card_guid,),
            )
        )
        if generated_guid_mapping:
            symbolic_in_prior_before = {
                card.guid
                for card in prior_replay.before.zones.card_universe
                if is_plan2_symbolic_guid(card.guid)
            }
            late_symbols = symbolic_in_prior_before & {
                symbolic for symbolic, _native in generated_guid_mapping
            }
            if late_symbols:
                # A generated card can remain symbolic through one retained
                # drink queue and receive its native GUID only in the next
                # card's LocalSave.  The later ordered-zone proof already
                # established the exact symbolic->native mapping.  Rebase the
                # same identity in the drink's logical before-state so its
                # transition remains continuous; no scalar or card field is
                # inferred from the later save.
                if not isinstance(prior_replay, Plan2CompletedDrinkReplay):
                    _reject(
                        "plan2-card-history-generated-guid-prior-before-unresolved"
                    )
                if prior_replay.action.selected_card_guid in late_symbols:
                    _reject(
                        "plan2-card-history-generated-guid-prior-drink-selection-unresolved"
                    )
                late_mapping = tuple(
                    (symbolic, native)
                    for symbolic, native in generated_guid_mapping
                    if symbolic in late_symbols
                )
                late_generated_guid_mapping = late_mapping
                materialized_prior_before = _apply_proven_generated_guid_mapping(
                    prior_replay.before,
                    late_mapping,
                )
                prior_replay = replace(
                    prior_replay,
                    before=materialized_prior_before,
                    transition=replace(
                        prior_replay.transition,
                        before=materialized_prior_before,
                    ),
                    trace=(
                        *prior_replay.trace,
                        *(
                            "card-history:generated-guid-materialized-in-prior-"
                            f"drink-before:{symbolic}->{native}"
                            for symbolic, native in late_mapping
                        ),
                    ),
                )
            prior_replay = _replace_replay_logical_after(
                prior_replay,
                materialized_before,
                audit=tuple(
                    "card-history:generated-guid-materialized:"
                    f"{symbolic}->{native}"
                    for symbolic, native in generated_guid_mapping
                ),
            )
            prior_logical_after = materialized_before
        replay = _replay(
            prior_persisted_transition,
            persisted_transition,
            action,
            before_horizon=prior_logical_after,
            catalog=catalog,
            database=Path(database),
            prior_replay=prior_replay,
            observation=observation,
        )
        if generated_guid_mapping:
            replay = replace(
                replay,
                trace=(
                    *(
                        "card-history:generated-guid-materialized-in-prior-"
                        f"drink-before:{symbolic}->{native}"
                        for symbolic, native in late_generated_guid_mapping
                    ),
                    *(
                        "card-history:generated-guid-materialized:"
                        f"{symbolic}->{native}"
                        for symbolic, native in generated_guid_mapping
                    ),
                    *replay.trace,
                ),
            )
    except _ReplayRejected as rejected:
        return Plan2CardHistoryReplayResult(None, (rejected.issue,))
    except (OSError, OverflowError, TypeError, ValueError) as error:
        return Plan2CardHistoryReplayResult(
            None,
            (
                Plan2CardHistoryIssue(
                    "plan2-card-history-replay-failed-closed",
                    f"{type(error).__name__}:{error}",
                ),
            ),
        )
    return Plan2CardHistoryReplayResult(replay)


__all__ = [
    "PLAN2_HUD_TURNS_RAW_REMAIN_V2",
    "Plan2CardHistoryIssue",
    "Plan2CardHistoryObservation",
    "Plan2CardHistoryObservationAuthority",
    "Plan2CardHistoryReplayResult",
    "Plan2CompletedCardReplay",
    "Plan2CompletedDrinkReplay",
    "Plan2CompletedLogicalReplay",
    "Plan2DrinkHistoryReplayResult",
    "recover_retained_plan2_drink_history",
    "replay_completed_plan2_card_history",
    "replay_next_completed_plan2_card_history",
]
