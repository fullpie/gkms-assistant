"""Central GUID transaction boundary for Plan 3 native ``UsePool`` plays.

The effect planners intentionally stop after queueing a GUID command.  This
module owns the shared command shape and the execution-time relocation into
the compatibility Playing slot.  It does not execute card effects itself;
``plan3_native_search`` supplies the one normal card transaction so ordinary,
with-cost, and future HoldAll callers share the same settlement path.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .plan3_engine import (
    CATEGORY_MENTAL,
    COST_STAMINA,
    EFFECT_FORCE_PLAY_CARD_SEARCH,
    EFFECT_PLAYABLE_VALUE_ADD,
    EFFECT_STATUS_ENCHANT,
    MOVE_LOST,
    PLAN3,
    Plan3Card,
    Plan3Effect,
    Plan3State,
)
from .plan3_native_state import (
    Plan3NativeCard,
    Plan3NativeState,
    Plan3NativeStateError,
)


EFFECT_CARD_SEARCH_PLAY_COUNT_BUFF = (
    "ProduceExamEffectType_ExamCardSearchEffectPlayCountBuff"
)
EFFECT_FORCE_PLAY_WITH_COST = (
    "ProduceExamEffectType_ExamForcePlayCardSearchWithCost"
)

NEO_CARD_ID = "p_card-03-ido-3_193"
NEO_UPGRADES = (0, 1, 2, 3)
NEO_EFFECT_IDS = (
    "e_effect-exam_playable_value_add-01",
    "e_effect-exam_card_search_effect_play_count_buff-0001-01-inf-"
    "p_card_search-n-r-sr-ssr-playing-all-0_0",
    "e_effect-exam_force_play_card_search-"
    "p_card_search-deck_grave-select-1_1",
)
NEO_EFFECT_GROUP_IDS = (
    "effect_group-visible-exam_playable_value_add-000",
    "effect_group-visible-exam_card_search_effect_play_count_buff-000",
)

WITH_COST_CARD_ID = "p_card-03-ido-100_038"
WITH_COST_CARD_UPGRADE = 0
WITH_COST_INSTALLER_ID = (
    "e_effect-exam_status_enchant-01-inf-"
    "enchant-p_card-03-ido-100_038-enc01"
)
WITH_COST_STATUS_ID = "enchant-p_card-03-ido-100_038-enc01"
WITH_COST_EFFECT_ID = (
    "e_effect-exam_force_play_card_search_with_cost-"
    "p_card_search-not_lost-select-1_1"
)
WITH_COST_TRIGGER_ID = "e_trigger-exam_start_turn-full_power_up"
WITH_COST_EFFECT_IDS = (
    "e_effect-exam_full_power_point-0005",
    "e_effect-exam_playable_value_add-01",
    WITH_COST_INSTALLER_ID,
)
WITH_COST_EFFECT_GROUP_IDS = (
    "effect_group-visible-exam_status_enchant-000",
    "effect_group-visible-exam_playable_value_add-000",
    "effect_group-visible-exam_full_power-000",
)

ORDINARY_FORCE_EFFECT_ID = NEO_EFFECT_IDS[2]
PLAY_COUNT_BUFF_EFFECT_ID = NEO_EFFECT_IDS[1]

USE_POOL_TRANSACTION_ORDER = (
    "resolve-current-source-position-by-command-guid",
    "stage-same-guid-as-playing",
    "evaluate-live-card-and-is-playable",
    "pay-live-cost-once-only-when-command-requests",
    "do-not-consume-playable-count",
    "snapshot-normal-card-listeners",
    "consume-newest-matching-play-count-buff-once",
    "execute-n-plus-one-direct-effect-passes",
    "run-once-effect-only-on-pass-zero",
    "increment-guid-play-count-at-build-and-move",
    "append-history-once",
    "settle-master-move-and-difference-once",
)


def _plain(effect: Plan3Effect) -> bool:
    return bool(
        not effect.status_enchant_id
        and effect.status_enchant is None
        and not effect.chain_effect_id
        and not effect.chain_effect_ids
        and effect.chain_effect is None
        and effect.trigger is None
        and not effect.once
        and effect.card_move_rule is None
    )


def matches_ordinary_force_effect(effect: object) -> bool:
    return bool(
        isinstance(effect, Plan3Effect)
        and effect.id == ORDINARY_FORCE_EFFECT_ID
        and effect.effect_type == EFFECT_FORCE_PLAY_CARD_SEARCH
        and effect.value1 == effect.value2 == effect.effect_count == effect.effect_turn == 0
        and _plain(effect)
    )


def matches_play_count_buff_effect(effect: object) -> bool:
    return bool(
        isinstance(effect, Plan3Effect)
        and effect.id == PLAY_COUNT_BUFF_EFFECT_ID
        and effect.effect_type == EFFECT_CARD_SEARCH_PLAY_COUNT_BUFF
        and effect.value1 == 1
        and effect.value2 == 0
        and effect.effect_count == 1
        and effect.effect_turn == -1
        and _plain(effect)
    )


def matches_with_cost_effect(effect: object) -> bool:
    return bool(
        isinstance(effect, Plan3Effect)
        and effect.id == WITH_COST_EFFECT_ID
        and effect.effect_type == EFFECT_FORCE_PLAY_WITH_COST
        and effect.value1 == effect.value2 == effect.effect_count == effect.effect_turn == 0
        and _plain(effect)
    )


def matches_neo_card(card: object) -> bool:
    return bool(
        isinstance(card, Plan3Card)
        and card.id == NEO_CARD_ID
        and card.upgrade in NEO_UPGRADES
        and card.plan_type == PLAN3
        and card.category == CATEGORY_MENTAL
        and card.stamina_cost == 0
        and card.force_stamina_cost == 0
        and card.cost_type == COST_STAMINA
        and card.cost_value == 0
        and card.play_trigger is None
        and card.move_position_type == MOVE_LOST
        and tuple(effect.id for effect in card.effects) == NEO_EFFECT_IDS
        and tuple(effect.once for effect in card.effects) == (False, False, False)
        and card.effect_group_ids == NEO_EFFECT_GROUP_IDS
        and card.effects[0].effect_type == EFFECT_PLAYABLE_VALUE_ADD
        and card.effects[0].effect_count == 1
        and matches_play_count_buff_effect(card.effects[1])
        and matches_ordinary_force_effect(card.effects[2])
    )


def matches_with_cost_installer(effect: object) -> bool:
    if not (
        isinstance(effect, Plan3Effect)
        and effect.id == WITH_COST_INSTALLER_ID
        and effect.effect_type == EFFECT_STATUS_ENCHANT
        and effect.value1 == effect.value2 == 0
        and effect.effect_count == 1
        and effect.effect_turn == -1
        and effect.status_enchant_id == WITH_COST_STATUS_ID
        and effect.status_enchant is not None
        and effect.trigger is None
        and not effect.once
        and effect.card_move_rule is None
        and not effect.chain_effect_id
        and not effect.chain_effect_ids
        and effect.chain_effect is None
    ):
        return False
    rule = effect.status_enchant
    trigger = rule.trigger
    return bool(
        rule.id == WITH_COST_STATUS_ID
        and len(rule.effects) == 1
        and matches_with_cost_effect(rule.effects[0])
        and trigger.id == WITH_COST_TRIGGER_ID
        and trigger.phase_types == ("ProduceExamPhaseType_ExamStartTurn",)
        and not trigger.phase_values
        and not trigger.field_check_types
        and trigger.field_types == ("ProduceExamFieldStatusType_FullPowerUp",)
        and not trigger.field_values
        and not trigger.field_card_search_ids
        and not trigger.produce_card_search_id
        and trigger.upper_search_count == trigger.lower_search_count == 0
        and trigger.card_move_position_type
        == "ProduceCardMovePositionType_Unknown"
        and not trigger.effect_types
        and trigger.lesson_type == "ProduceStepLessonType_Unknown"
    )


def matches_with_cost_card(card: object) -> bool:
    return bool(
        isinstance(card, Plan3Card)
        and (card.id, card.upgrade)
        == (WITH_COST_CARD_ID, WITH_COST_CARD_UPGRADE)
        and card.plan_type == PLAN3
        and card.category == CATEGORY_MENTAL
        and card.stamina_cost == card.force_stamina_cost == 0
        and card.cost_type == COST_STAMINA
        and card.cost_value == 0
        and card.play_trigger is None
        and card.move_position_type == MOVE_LOST
        and tuple(effect.id for effect in card.effects) == WITH_COST_EFFECT_IDS
        and tuple(effect.once for effect in card.effects) == (False, False, False)
        and card.effect_group_ids == WITH_COST_EFFECT_GROUP_IDS
        and matches_with_cost_installer(card.effects[2])
    )


@dataclass(frozen=True, slots=True)
class Plan3UsePoolCommand:
    guid: str
    is_consume_cost: bool
    is_use_playable_count: bool = False
    is_manual: bool = False
    enchant_effect_uid: int = 0
    detached_card: Plan3NativeCard | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.guid, str) or not self.guid:
            raise ValueError("UsePool guid must be non-empty text")
        for name in (
            "is_consume_cost",
            "is_use_playable_count",
            "is_manual",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be bool")
        if self.is_use_playable_count:
            raise ValueError("the bounded forced UsePool slice never consumes playable")
        if self.is_manual:
            raise ValueError("the bounded forced UsePool slice is never manual")
        if self.detached_card is not None and (
            not isinstance(self.detached_card, Plan3NativeCard)
            or self.detached_card.guid != self.guid
            or self.is_consume_cost
        ):
            raise ValueError("detached RandomPool command requires its exact cost-free card")
        if (
            isinstance(self.enchant_effect_uid, bool)
            or not isinstance(self.enchant_effect_uid, int)
            or self.enchant_effect_uid < 0
        ):
            raise ValueError("enchant_effect_uid must be non-negative")

    @classmethod
    def from_queued(cls, queued: object) -> "Plan3UsePoolCommand":
        try:
            return cls(
                guid=getattr(queued, "guid"),
                is_consume_cost=getattr(queued, "is_consume_cost"),
                is_use_playable_count=getattr(
                    queued, "is_use_playable_count"
                ),
                is_manual=getattr(queued, "is_manual"),
                enchant_effect_uid=getattr(queued, "enchant_effect_uid"),
            )
        except AttributeError as error:
            raise TypeError("queued command does not expose UsePool fields") from error


@dataclass(frozen=True, slots=True)
class Plan3UsePoolStage:
    command: Plan3UsePoolCommand
    scalar_before: Plan3State
    native_before: Plan3NativeState
    scalar_playing: Plan3State
    native_playing: Plan3NativeState
    card: Plan3NativeCard
    source_zone: str
    source_index: int
    order: tuple[str, ...] = USE_POOL_TRANSACTION_ORDER


_NATIVE_ZONES = (
    ("hand", "hand", "hand"),
    ("deck", "deck", "draw_pile"),
    ("grave", "grave", "discard_pile"),
    ("hold", "hold", "hold_pile"),
    ("lost", "lost", "lost_pile"),
)


def stage_plan3_use_pool_guid(
    scalar: Plan3State,
    native: Plan3NativeState,
    command: Plan3UsePoolCommand,
) -> Plan3UsePoolStage:
    """Resolve the command GUID against current zones and stage Playing.

    Stored source indices on the effect command are intentionally ignored.
    The current immutable state is searched at execution time, matching the
    native runner after earlier queued commands may have moved the GUID.
    """

    if not isinstance(scalar, Plan3State):
        raise TypeError("scalar must be Plan3State")
    if not isinstance(native, Plan3NativeState):
        raise TypeError("native must be Plan3NativeState")
    if not isinstance(command, Plan3UsePoolCommand):
        raise TypeError("command must be Plan3UsePoolCommand")
    native.assert_plan3_projection(scalar)
    if command.detached_card is not None:
        card = command.detached_card
        if any(value.guid == card.guid for value in native.all_cards):
            raise Plan3NativeStateError("detached-use-pool-guid-already-positioned", card.guid)
        native_playing = replace(native, hand=(*native.hand, card))
        scalar_playing = replace(scalar, hand=(*scalar.hand, card.ref))
        return Plan3UsePoolStage(command, scalar, native, scalar_playing, native_playing,
                                card, "random_pool", -1)
    matches: list[tuple[str, str, str, int, Plan3NativeCard]] = []
    for public_zone, native_field, scalar_field in _NATIVE_ZONES:
        for index, card in enumerate(getattr(native, native_field)):
            if card.guid == command.guid:
                matches.append(
                    (public_zone, native_field, scalar_field, index, card)
                )
    if len(matches) != 1:
        raise Plan3NativeStateError("use-pool-guid-not-found", command.guid)
    source_zone, native_field, scalar_field, index, card = matches[0]
    if source_zone == "hand":
        return Plan3UsePoolStage(
            command,
            scalar,
            native,
            scalar,
            native,
            card,
            source_zone,
            index,
        )

    native_source = getattr(native, native_field)
    scalar_source = getattr(scalar, scalar_field)
    if index >= len(scalar_source) or scalar_source[index] != card.ref:
        raise Plan3NativeStateError(
            "use-pool-scalar-source-mismatch", command.guid
        )
    native_playing = replace(
        native,
        **{
            native_field: (*native_source[:index], *native_source[index + 1 :]),
            "hand": (*native.hand, card),
        },
    )
    scalar_playing = replace(
        scalar,
        **{
            scalar_field: (*scalar_source[:index], *scalar_source[index + 1 :]),
            "hand": (*scalar.hand, card.ref),
        },
    )
    native_playing.assert_plan3_projection(scalar_playing)
    return Plan3UsePoolStage(
        command,
        scalar,
        native,
        scalar_playing,
        native_playing,
        card,
        source_zone,
        index,
    )


__all__ = [
    "EFFECT_CARD_SEARCH_PLAY_COUNT_BUFF",
    "EFFECT_FORCE_PLAY_WITH_COST",
    "NEO_CARD_ID",
    "NEO_UPGRADES",
    "ORDINARY_FORCE_EFFECT_ID",
    "PLAY_COUNT_BUFF_EFFECT_ID",
    "Plan3UsePoolCommand",
    "Plan3UsePoolStage",
    "USE_POOL_TRANSACTION_ORDER",
    "WITH_COST_CARD_ID",
    "WITH_COST_CARD_UPGRADE",
    "WITH_COST_EFFECT_ID",
    "WITH_COST_STATUS_ID",
    "matches_neo_card",
    "matches_ordinary_force_effect",
    "matches_play_count_buff_effect",
    "matches_with_cost_card",
    "matches_with_cost_effect",
    "matches_with_cost_installer",
    "stage_plan3_use_pool_guid",
]
