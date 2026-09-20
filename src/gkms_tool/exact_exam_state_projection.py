"""Shared native-state projection for Exact BC and Offline RL inputs.

The runtime DLL already emits the flat native ExamSave shape used during
training.  Maa transition rows instead carry the equivalent typed LocalSave
state.  This module is the single conversion boundary between those two
representations; it never consults a simulator or a state-after value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from .canonical_training_labels import (
    EFFECT_BY_NATIVE_VALUE,
    PLAN_BY_NATIVE_VALUE,
    STAGE_BY_NATIVE_VALUE,
)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _typed_card_to_native(value: Mapping[str, Any]) -> dict[str, object]:
    runtime = _mapping(value.get("runtime_state"))
    return {
        "_guid": value.get("guid"),
        "_playCount": runtime.get("play_count", 0),
        "_baseUpgradeCount": value.get("base_upgrade", 0),
        "_tmpUpgradeCount": value.get("temporary_upgrade", 0),
        "_supportUpgradeIdList": deepcopy(value.get("support_upgrade_ids", [])),
        "_fixedDeckOrder": value.get("fixed_deck_order", 0),
        "_statusEffect": deepcopy(runtime.get("status_effect", {})),
        "_affectGrowEffectIdList": deepcopy(
            runtime.get("affect_grow_effect_id_list", [])
        ),
        "_growEffectExamStartAfterList": deepcopy(
            runtime.get("grow_effect_exam_start_after_list", [])
        ),
        "_isMoveProduceExamEffectUseInTurn": runtime.get(
            "is_move_produce_exam_effect_use_in_turn", False
        ),
        "_staminaConsumptionSpecifyEffectList": deepcopy(
            runtime.get("stamina_consumption_specify_effect_list", [])
        ),
        "_cardData": {
            "_id": value.get("card_id"),
            "_upgradeCount": value.get(
                "effective_upgrade", value.get("base_upgrade", 0)
            ),
            "_customizeCountList": deepcopy(
                runtime.get("customize_count_list", [])
            ),
            "_produceCardSkinId": runtime.get("produce_card_skin_id", ""),
            "_produceCardSkinAssetId": runtime.get(
                "produce_card_skin_asset_id", ""
            ),
        },
    }


def _typed_card_group_to_native(value: object, *, label: str) -> dict[str, object]:
    if not isinstance(value, list):
        raise ValueError(f"typed LocalSave {label} group is invalid")
    return {"list": [_typed_card_to_native(_mapping(card)) for card in value]}


def project_exact_exam_native_state(
    state_before: Mapping[str, Any],
) -> dict[str, object]:
    """Return the flat native input shape consumed by Exact BC/IQL models."""

    if not isinstance(state_before, Mapping):
        raise TypeError("Exact Exam state must be a mapping")
    if isinstance(state_before.get("handList"), list) and isinstance(
        state_before.get("drinkList"), list
    ):
        return deepcopy(dict(state_before))

    root = _mapping(state_before.get("root_runtime"))
    opaque = _mapping(root.get("opaque_fields"))
    zones = _mapping(state_before.get("zones"))
    if not opaque or not zones:
        raise ValueError("transition has no native or typed LocalSave state")

    result: dict[str, object] = deepcopy(dict(opaque))
    scalar_fields = {
        "characterId": "character_id",
        "settingId": "setting_id",
        "examType": "exam_type",
        "stepType": "step_type_value",
        "phase": "phase",
        "currentTurn": "current_turn",
        "limitTurn": "limit_turn",
        "remainTurn": "remain_turn",
        "extraTurn": "extra_turn",
        "parameter": "score",
        "stamina": "stamina",
        "maxStamina": "max_stamina",
        "block": "block",
        "vocalBonusPermil": "vocal_bonus_permille",
        "danceBonusPermil": "dance_bonus_permille",
        "visualBonusPermil": "visual_bonus_permille",
        "turnStatusParameterTypeList": "turn_parameter_types",
        "turnCardPlayCount": "turn_card_play_count",
        "examCardPlayCount": "exam_card_play_count",
        "isTurnCardPlayEnd": "is_turn_card_play_end",
        "random": "random_state",
        "turnUseSupportCardIdList": "turn_use_support_ids",
    }
    for target, source in scalar_fields.items():
        if source in state_before:
            result[target] = deepcopy(state_before[source])

    zone_fields = {
        "handList": "hand",
        "deckList": "deck",
        "graveList": "grave",
        "lostList": "lost",
        "holdList": "hold",
    }
    for target, source in zone_fields.items():
        rows = zones.get(source, [])
        if not isinstance(rows, list):
            raise ValueError(f"typed LocalSave zone {source} is invalid")
        result[target] = [
            _typed_card_to_native(_mapping(value)) for value in rows
        ]

    playing = state_before.get("playing_card")
    if playing is not None and not isinstance(playing, Mapping):
        raise ValueError("typed LocalSave playing_card is invalid")
    result["playingCard"] = (
        None if playing is None else _typed_card_to_native(playing)
    )

    removed = state_before.get("removed_cards", [])
    if not isinstance(removed, list):
        raise ValueError("typed LocalSave removed_cards is invalid")
    result["removedCardList"] = [
        _typed_card_to_native(_mapping(value)) for value in removed
    ]

    future = state_before.get("future_deck", [])
    if not isinstance(future, list):
        raise ValueError("typed LocalSave future_deck is invalid")
    result["futureDeckList"] = [
        _typed_card_group_to_native(value, label="future_deck")
        for value in future
    ]

    past = state_before.get("past_deck", [])
    if past is None:
        raise ValueError("typed LocalSave past_deck is unknown")
    if not isinstance(past, list):
        raise ValueError("typed LocalSave past_deck is invalid")
    result["pastDeckList"] = [
        _typed_card_group_to_native(value, label="past_deck")
        for value in past
    ]

    result["commandList"] = deepcopy(root.get("command_list", []))
    result["drawCardGuidList"] = deepcopy(root.get("draw_card_guid_list", []))
    result["isTurnCardGrave"] = root.get("is_turn_card_grave")
    result["isTurnCardLost"] = root.get("is_turn_card_lost")
    result["isExamEndComplete"] = root.get("is_exam_end_complete")
    return result


def project_plan2_horizon_native_state(
    state: object,
    *,
    flow: str,
    stage: str,
) -> dict[str, object]:
    """Project one retained Plan2 horizon into the Exact model input shape.

    The horizon is already the replay-proven logical state after the physical
    action.  This projection uses only its scalar/runtime/zones and the flow
    and stage previously bound by a complete physical evidence boundary.
    """

    from .plan2_native_horizon import Plan2NativeHorizonState

    if not isinstance(state, Plan2NativeHorizonState):
        raise TypeError("retained Exact state must be Plan2NativeHorizonState")
    parts = flow.split("|") if isinstance(flow, str) else []
    if len(parts) != 3 or any(not value for value in parts):
        raise ValueError("retained Exact flow must contain produce/plan/effect")
    if stage not in STAGE_BY_NATIVE_VALUE.values():
        raise ValueError("retained Exact stage is unsupported")
    plan_values = {value: key for key, value in PLAN_BY_NATIVE_VALUE.items()}
    effect_values = {value: key for key, value in EFFECT_BY_NATIVE_VALUE.items()}
    stage_values = {value: key for key, value in STAGE_BY_NATIVE_VALUE.items()}
    if parts[1] not in plan_values or parts[2] not in effect_values:
        raise ValueError("retained Exact flow enum identity is unsupported")

    def cards(values: object) -> list[dict[str, object]]:
        if not isinstance(values, Sequence) or isinstance(
            values, (str, bytes, bytearray)
        ):
            raise ValueError("retained Exact zone is invalid")
        return [
            _typed_card_to_native(_mapping(value.to_dict()))
            for value in values
        ]

    scalar = state.scalar
    result: dict[str, object] = {
        "produceId": parts[0],
        "planType": plan_values[parts[1]],
        "mainEffectType": effect_values[parts[2]],
        "displayMainEffectType": effect_values[parts[2]],
        "stepType": stage_values[stage],
        "phase": 6,
        "currentTurn": scalar.current_turn,
        "limitTurn": state.limit_turn,
        "remainTurn": state.remaining_turns,
        "extraTurn": state.extra_turn,
        "parameter": scalar.score,
        "score": scalar.score,
        "stamina": scalar.stamina,
        "maxStamina": scalar.max_stamina,
        "block": scalar.block,
        "review": scalar.review,
        "cardPlayAggressive": scalar.card_play_aggressive,
        "examCardPlayCount": scalar.exam_card_play_count,
        "turnCardPlayCount": scalar.turn_card_play_count,
        "playsRemaining": state.plays_remaining,
        "isTurnCardPlayEnd": state.plays_remaining <= 0,
        "isExamEndComplete": state.terminal,
        "random": state.zones.random_state,
        "totalDrawCardCount": state.total_effect_draw_card_count,
        "reviewConsumptionSumCount": state.review_consumption_sum,
        "blockConsumptionSumCount": state.block_consumption_sum_count,
        "handList": cards(state.zones.hand),
        "deckList": cards(state.zones.deck),
        "graveList": cards(state.zones.grave),
        "lostList": cards(state.zones.lost),
        # Plan2's retained horizon has already returned Hold at TurnStart and
        # owns only the four actionable zones.
        "holdList": [],
        "drinkList": [
            {
                "_id": value.drink_id,
                "_instanceId": value.instance_id,
            }
            for value in state.drink_runtime.inventory
        ],
    }
    return result


def normalize_exact_exam_legal_candidates(
    state_before: Mapping[str, Any],
    candidates: Sequence[object],
) -> tuple[dict[str, object], ...]:
    """Bind a complete unified native set to the Exact policy input schema."""

    hand = state_before.get("handList")
    if not isinstance(hand, list):
        raise ValueError("Exact policy state has no handList")
    hand_by_guid: dict[str, tuple[int, object, object]] = {}
    for index, value in enumerate(hand):
        row = _mapping(value)
        guid = str(row.get("_guid", row.get("guid")))
        card_data = _mapping(row.get("_cardData", row.get("cardData")))
        hand_by_guid[guid] = (
            index,
            card_data.get("_id", card_data.get("id", row.get("card_id"))),
            card_data.get(
                "_upgradeCount",
                card_data.get("upgradeCount", row.get("effective_upgrade")),
            ),
        )
    aliases = {
        "play": "use-hand",
        "card": "use-hand",
        "use_hand": "use-hand",
        "drink": "use-drink",
        "use_drink": "use-drink",
        "end_turn": "turn-end",
        "turn_end": "turn-end",
    }
    result: list[dict[str, object]] = []
    for raw in candidates:
        candidate = dict(_mapping(raw))
        if not candidate:
            raise ValueError("legal candidate is not an object")
        raw_kind = str(candidate.get("kind", candidate.get("action_type", "")))
        kind = aliases.get(raw_kind, raw_kind)
        candidate["kind"] = kind
        candidate["action_type"] = kind
        candidate["legal"] = True
        if kind == "use-hand":
            guid = candidate.get("card_guid", candidate.get("guid"))
            if not isinstance(guid, str) or guid not in hand_by_guid:
                raise ValueError("card candidate is outside the native hand")
            hand_slot, card_id, upgrade = hand_by_guid[guid]
            candidate["card_guid"] = guid
            candidate["guid"] = guid
            candidate["slot_index"] = hand_slot
            candidate.setdefault("card_id", card_id)
            if upgrade is not None:
                candidate.setdefault("upgrade", upgrade)
        elif kind == "use-drink":
            slot = candidate.get("slot_index", candidate.get("slot"))
            if isinstance(slot, bool) or not isinstance(slot, int):
                raise ValueError("drink candidate has no native slot")
            candidate["slot_index"] = slot
        elif kind == "turn-end":
            candidate["slot_index"] = int(candidate.get("slot_index", 0))
            candidate["action_id"] = "END_TURN"
        else:
            raise ValueError(f"unsupported exact action family: {kind}")
        result.append(candidate)
    return tuple(result)


__all__ = [
    "normalize_exact_exam_legal_candidates",
    "project_exact_exam_native_state",
    "project_plan2_horizon_native_state",
]
