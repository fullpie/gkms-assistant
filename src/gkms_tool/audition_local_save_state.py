"""Read-only, run-bound evidence for the current ``ExamSaveData`` state.

This module is deliberately separate from the mutable shadow checkpoint.  It
decodes the game's local save as an independent observation, preserves every
card's persistent/base identity, and reports exact differences without
silently repairing either source.  A caller may use a separately reviewed
reconciliation artifact to accept a difference; this evidence object alone
never mutates the checkpoint or the game.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from typing import Any, Iterable, Mapping

from .audition_rules import AuditionRules
from .audition_zone_checkpoint import AuditionZoneCheckpoint, ZoneCardRef
from .exam_session import ExamSession, SESSION_SCHEMA_VERSION
from .local_save_decoder import decode_local_save_bytes


AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION = 5
_LEGACY_AUDITION_LOCAL_SAVE_STATE_SCHEMA_V4 = 4
_LEGACY_AUDITION_LOCAL_SAVE_STATE_SCHEMA_V3 = 3
_LEGACY_AUDITION_LOCAL_SAVE_STATE_SCHEMA_V2 = 2
EXAM_SAVE_DATA_SOURCE_TYPE = "Campus.InGame.Exam.ExamSaveData"
LOCAL_SAVE_STATE_FILENAME = "audition_local_save_state.json"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ZONE_FIELDS = ("handList", "deckList", "graveList", "lostList", "holdList")
# PC v3.3.0 added these serializer fields without changing the established
# v3.2.3 exam-state fields used by the native replay.  Keep them explicit and
# lossless instead of weakening the exact root allowlist.
EXAM_SAVE_DATA_V330_ADDITIONAL_ROOT_FIELDS = (
    "examExtraTurn",
    "idolCardId",
    "idolCardSkinId",
    "isReplay",
    "lessonLimitUpScore",
    "produceDrinkPossessLimit",
    "produceId",
)
EXAM_SAVE_DATA_ROOT_FIELDS = (
    "examType",
    "settingId",
    "characterId",
    "stamina",
    "maxStamina",
    "producePoint",
    "random",
    "stepType",
    "mainEffectType",
    "displayMainEffectType",
    "planType",
    "clearBorder",
    "originClearBorder",
    "limitBorder",
    "phase",
    "currentTurn",
    "limitTurn",
    "remainTurn",
    "extraTurn",
    "parameter",
    "block",
    "status",
    "handList",
    "deckList",
    "graveList",
    "lostList",
    "holdList",
    "futureDeckList",
    "pastDeckList",
    "playingCard",
    "removedCardList",
    "planIgnoreProduceCardWhiteList",
    "playLogList",
    "userPlayLogList",
    "isTurnCardPlayEnd",
    "turnCardPlayCount",
    "examCardPlayCount",
    "isTurnCardGrave",
    "isTurnCardLost",
    "drinkList",
    "itemList",
    "supportCardList",
    "turnUseSupportCardIdList",
    "commandList",
    "gimmickList",
    "turnLogList",
    "currentTurnHandCardList",
    "currentTurnHoldCardList",
    "currentTurnStartStatusList",
    "currentTurnStartStatusEffectDataList",
    "currentTurnTriggeredStatusEnchantIdList",
    "currentTurnTotalConsumeStamina",
    "currentTurnTotalAddParameter",
    "currentTurnTotalBlock",
    "totalConsumeStamina",
    "isExamEndComplete",
    "fullPowerPointGetSumCount",
    "blockConsumptionSumCount",
    "reviewConsumptionSumCount",
    "stanceChangeCount",
    "stanceFullPowerChangeCount",
    "stanceConcentrationChangeCount",
    "stancePreservationChangeCount",
    "drawCardGuidList",
    "totalDrawCardCount",
    "produceTargetParameter",
    "vocalBonusPermil",
    "danceBonusPermil",
    "visualBonusPermil",
    "vocalConfigParameter",
    "danceConfigParameter",
    "visualConfigParameter",
    "turnStatusParameterTypeList",
    "npcDataList",
    "isStaticNpcScore",
    "parameterVocal",
    "parameterDance",
    "parameterVisual",
    "npcScoreMultiplePermil",
    "competitionIdolDataList",
    "competitionSectionIndex",
    "references",
    *EXAM_SAVE_DATA_V330_ADDITIONAL_ROOT_FIELDS,
)
_EXAM_SAVE_DATA_ROOT_FIELD_SET = frozenset(EXAM_SAVE_DATA_ROOT_FIELDS)
_V323_EXAM_SAVE_DATA_ROOT_FIELD_SET = (
    _EXAM_SAVE_DATA_ROOT_FIELD_SET
    - frozenset(EXAM_SAVE_DATA_V330_ADDITIONAL_ROOT_FIELDS)
)
_STRUCTURED_ROOT_FIELDS = frozenset(
    {
        "examType",
        "settingId",
        "characterId",
        "stamina",
        "maxStamina",
        "random",
        "stepType",
        "phase",
        "currentTurn",
        "limitTurn",
        "remainTurn",
        "extraTurn",
        "parameter",
        "block",
        "handList",
        "deckList",
        "graveList",
        "lostList",
        "holdList",
        "futureDeckList",
        "pastDeckList",
        "playingCard",
        "removedCardList",
        "isTurnCardPlayEnd",
        "turnCardPlayCount",
        "examCardPlayCount",
        "turnUseSupportCardIdList",
        "vocalBonusPermil",
        "danceBonusPermil",
        "visualBonusPermil",
        "turnStatusParameterTypeList",
    }
)
_SETTLED_ROOT_RUNTIME_FIELDS = frozenset(
    {
        "commandList",
        "drawCardGuidList",
        "isTurnCardGrave",
        "isTurnCardLost",
        "isExamEndComplete",
    }
)
_ROOT_RUNTIME_FIELDS = _EXAM_SAVE_DATA_ROOT_FIELD_SET - _STRUCTURED_ROOT_FIELDS
_OPAQUE_ROOT_RUNTIME_FIELDS = _ROOT_RUNTIME_FIELDS - _SETTLED_ROOT_RUNTIME_FIELDS
_V323_OPAQUE_ROOT_RUNTIME_FIELDS = (
    _V323_EXAM_SAVE_DATA_ROOT_FIELD_SET
    - _STRUCTURED_ROOT_FIELDS
    - _SETTLED_ROOT_RUNTIME_FIELDS
)
if (
    len(EXAM_SAVE_DATA_ROOT_FIELDS) != 89
    or len(_EXAM_SAVE_DATA_ROOT_FIELD_SET) != 89
    or len(_STRUCTURED_ROOT_FIELDS) != 31
    or len(_ROOT_RUNTIME_FIELDS) != 58
    or len(_OPAQUE_ROOT_RUNTIME_FIELDS) != 53
):
    raise RuntimeError("ExamSaveData PC v3.3.0 root field inventory is inconsistent")
_RAW_CARD_FIELDS = {
    "_cardData",
    "_guid",
    "_fixedDeckOrder",
    "_baseUpgradeCount",
    "_tmpUpgradeCount",
    "_supportUpgradeIdList",
    "_statusEffect",
    "_growEffectExamStartAfterList",
    "_affectGrowEffectIdList",
    "_playCount",
    "_isMoveProduceExamEffectUseInTurn",
    "_staminaConsumptionSpecifyEffectList",
}
_RAW_CARD_DATA_FIELDS = {
    "_id",
    "_upgradeCount",
    "_customizeCountList",
    "_produceCardSkinId",
    "_produceCardSkinAssetId",
}
_EMPTY_STATUS_EFFECT = {
    "_id": "",
    "_produceExamTriggerId": "",
    "_produceCardGrowEffectIdList": [],
    "_effectGroupIdList": [],
    "_triggerCount": 0,
    "_phaseCountDictionary": {"_list": []},
    "_spendTurn": 0,
    "_spendCount": 0,
}
_LESSON_TYPE_BY_PARAMETER_VALUE = {
    1: "ProduceStepLessonType_LessonVocal",
    2: "ProduceStepLessonType_LessonDance",
    3: "ProduceStepLessonType_LessonVisual",
}
_STEP_TYPE_VALUE_BY_NAME = {
    "ProduceStepType_AuditionMid1": 16,
    "ProduceStepType_AuditionMid2": 17,
    "ProduceStepType_AuditionFinal": 18,
}
_AUDITION_EXAM_TYPE = 1
_LESSON_EXAM_TYPE = 0
_MAIN_PHASE = 6
_PLAN2_NATIVE_LESSON_STEP_TYPES = frozenset(range(1, 10))
_PLAN2_NATIVE_AUDITION_STEP_TYPES = frozenset(range(16, 19))


def audition_step_type_value(step_type: str) -> int:
    """Return the native ``ProduceStepType`` value for an audition stage."""

    value = _STEP_TYPE_VALUE_BY_NAME.get(_text(step_type, "step_type"))
    if value is None:
        raise ValueError(f"unsupported audition step_type: {step_type}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _plain_text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    return value


def _integer(
    value: object,
    label: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} must be an integer <= {maximum}")
    return value


def _signed_int32(value: object, label: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < -(1 << 31)
        or value > (1 << 31) - 1
    ):
        raise ValueError(f"{label} must be a signed 32-bit integer")
    return value


def _optional_signed_int32(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _signed_int32(value, label)


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _sha256(value: object, label: str) -> str:
    text = _text(value, label)
    if _SHA256_RE.fullmatch(text) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _exact(payload: Mapping[str, object], fields: set[str], label: str) -> None:
    actual = set(payload)
    if actual != fields:
        raise ValueError(
            f"{label} fields are invalid: "
            f"missing={sorted(fields - actual)} unknown={sorted(actual - fields)}"
        )


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _neutral_v330_root_extensions() -> dict[str, object]:
    """Return deterministic values for deserializing pre-v3.3 evidence only."""

    return {
        "examExtraTurn": 0,
        "idolCardId": "",
        "idolCardSkinId": "",
        "isReplay": False,
        "lessonLimitUpScore": 0,
        # The earlier client had the same fixed three-drink inventory limit;
        # it simply did not serialize the value at the ExamSave root.
        "produceDrinkPossessLimit": 3,
        "produceId": "",
    }


def _validate_json_value(value: object, label: str) -> None:
    """Reject values that cannot be represented losslessly in strict JSON."""

    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError(f"{label} must not contain NaN or infinity")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{label}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} object keys must be text")
            _validate_json_value(item, f"{label}.{key}")
        return
    raise ValueError(f"{label} must contain only JSON-compatible values")


@dataclass(frozen=True, order=True, slots=True)
class CanonicalJsonValue:
    """Hashable, exact JSON-compatible value used for undecoded native fields."""

    canonical_json: str

    def __post_init__(self) -> None:
        if not isinstance(self.canonical_json, str):
            raise TypeError("canonical_json must be text")
        try:
            value = json.loads(self.canonical_json)
        except json.JSONDecodeError as error:
            raise ValueError("canonical_json must contain valid JSON") from error
        _validate_json_value(value, "canonical_json")
        if _canonical_json(value) != self.canonical_json:
            raise ValueError("canonical_json must use the canonical representation")

    @classmethod
    def from_value(cls, value: object, label: str) -> "CanonicalJsonValue":
        _validate_json_value(value, label)
        return cls(_canonical_json(value))

    def to_value(self) -> object:
        return json.loads(self.canonical_json)


@dataclass(frozen=True, order=True, slots=True)
class LocalSaveExamRootRuntimeState:
    """The 58 root fields not represented by the structured exam projection."""

    command_list: CanonicalJsonValue
    draw_card_guid_list: tuple[str, ...]
    is_turn_card_grave: bool
    is_turn_card_lost: bool
    is_exam_end_complete: bool
    opaque_fields: CanonicalJsonValue

    def __post_init__(self) -> None:
        if not isinstance(self.command_list, CanonicalJsonValue):
            raise TypeError("command_list must be CanonicalJsonValue")
        if not isinstance(self.command_list.to_value(), list):
            raise ValueError("command_list must contain a JSON list")
        draw_guids = tuple(self.draw_card_guid_list)
        if any(not isinstance(value, str) or not value.strip() for value in draw_guids):
            raise ValueError("draw_card_guid_list must contain non-empty text")
        object.__setattr__(self, "draw_card_guid_list", draw_guids)
        _boolean(self.is_turn_card_grave, "is_turn_card_grave")
        _boolean(self.is_turn_card_lost, "is_turn_card_lost")
        _boolean(self.is_exam_end_complete, "is_exam_end_complete")
        if not isinstance(self.opaque_fields, CanonicalJsonValue):
            raise TypeError("opaque_fields must be CanonicalJsonValue")
        raw_opaque = self.opaque_fields.to_value()
        if not isinstance(raw_opaque, Mapping):
            raise ValueError("opaque_fields must contain a JSON object")
        _exact(
            raw_opaque,
            set(_OPAQUE_ROOT_RUNTIME_FIELDS),
            "root_runtime.opaque_fields",
        )

    @property
    def command_list_is_empty(self) -> bool:
        return self.command_list.to_value() == []

    def to_dict(self) -> dict[str, object]:
        return {
            "command_list": self.command_list.to_value(),
            "draw_card_guid_list": list(self.draw_card_guid_list),
            "is_turn_card_grave": self.is_turn_card_grave,
            "is_turn_card_lost": self.is_turn_card_lost,
            "is_exam_end_complete": self.is_exam_end_complete,
            "opaque_fields": self.opaque_fields.to_value(),
        }

    @classmethod
    def from_raw_root(
        cls, payload: Mapping[str, object]
    ) -> "LocalSaveExamRootRuntimeState":
        # v3.3.0 extension fields remain opaque to the v3.2.3 native replay,
        # but their serializer shapes and identities are still exact evidence.
        _integer(payload["examExtraTurn"], "ExamSaveData.examExtraTurn")
        _text(payload["idolCardId"], "ExamSaveData.idolCardId")
        _plain_text(payload["idolCardSkinId"], "ExamSaveData.idolCardSkinId")
        _boolean(payload["isReplay"], "ExamSaveData.isReplay")
        _integer(payload["lessonLimitUpScore"], "ExamSaveData.lessonLimitUpScore")
        _integer(
            payload["produceDrinkPossessLimit"],
            "ExamSaveData.produceDrinkPossessLimit",
        )
        _text(payload["produceId"], "ExamSaveData.produceId")
        raw_command_list = payload["commandList"]
        if not isinstance(raw_command_list, list):
            raise ValueError("ExamSaveData.commandList must be a list")
        raw_draw_guids = payload["drawCardGuidList"]
        if not isinstance(raw_draw_guids, list):
            raise ValueError("ExamSaveData.drawCardGuidList must be a list")
        draw_guids = tuple(
            _text(value, f"ExamSaveData.drawCardGuidList[{index}]")
            for index, value in enumerate(raw_draw_guids)
        )
        opaque = {
            field_name: payload[field_name]
            for field_name in _OPAQUE_ROOT_RUNTIME_FIELDS
        }
        return cls(
            command_list=CanonicalJsonValue.from_value(
                raw_command_list, "ExamSaveData.commandList"
            ),
            draw_card_guid_list=draw_guids,
            is_turn_card_grave=_boolean(
                payload["isTurnCardGrave"], "ExamSaveData.isTurnCardGrave"
            ),
            is_turn_card_lost=_boolean(
                payload["isTurnCardLost"], "ExamSaveData.isTurnCardLost"
            ),
            is_exam_end_complete=_boolean(
                payload["isExamEndComplete"], "ExamSaveData.isExamEndComplete"
            ),
            opaque_fields=CanonicalJsonValue.from_value(
                opaque, "ExamSaveData root runtime fields"
            ),
        )
    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "LocalSaveExamRootRuntimeState":
        if not isinstance(payload, Mapping):
            raise ValueError("root_runtime must be an object")
        fields = {
            "command_list",
            "draw_card_guid_list",
            "is_turn_card_grave",
            "is_turn_card_lost",
            "is_exam_end_complete",
            "opaque_fields",
        }
        _exact(payload, fields, "root_runtime")
        raw_command_list = payload["command_list"]
        if not isinstance(raw_command_list, list):
            raise ValueError("root_runtime.command_list must be a list")
        raw_draw_guids = payload["draw_card_guid_list"]
        if not isinstance(raw_draw_guids, list):
            raise ValueError("root_runtime.draw_card_guid_list must be a list")
        raw_opaque = payload["opaque_fields"]
        if not isinstance(raw_opaque, Mapping):
            raise ValueError("root_runtime.opaque_fields must be an object")
        if set(raw_opaque) == set(_V323_OPAQUE_ROOT_RUNTIME_FIELDS):
            # Existing schema-v5 journals and path-free fixtures predate the
            # PC v3.3.0 serializer extension.  Upgrade only that exact legacy
            # keyset; partial or mixed shapes remain rejected by __post_init__.
            raw_opaque = {
                **dict(raw_opaque),
                **_neutral_v330_root_extensions(),
            }
        return cls(
            command_list=CanonicalJsonValue.from_value(
                raw_command_list, "root_runtime.command_list"
            ),
            draw_card_guid_list=tuple(
                _text(value, f"root_runtime.draw_card_guid_list[{index}]")
                for index, value in enumerate(raw_draw_guids)
            ),
            is_turn_card_grave=_boolean(
                payload["is_turn_card_grave"], "is_turn_card_grave"
            ),
            is_turn_card_lost=_boolean(
                payload["is_turn_card_lost"], "is_turn_card_lost"
            ),
            is_exam_end_complete=_boolean(
                payload["is_exam_end_complete"], "is_exam_end_complete"
            ),
            opaque_fields=CanonicalJsonValue.from_value(
                raw_opaque, "root_runtime.opaque_fields"
            ),
        )


def empty_local_save_exam_root_runtime_state() -> LocalSaveExamRootRuntimeState:
    """Return an actual-shaped neutral root runtime for synthetic unit evidence."""

    list_fields = {
        "competitionIdolDataList",
        "currentTurnHandCardList",
        "currentTurnHoldCardList",
        "currentTurnStartStatusEffectDataList",
        "currentTurnStartStatusList",
        "currentTurnTriggeredStatusEnchantIdList",
        "drinkList",
        "gimmickList",
        "itemList",
        "npcDataList",
        "planIgnoreProduceCardWhiteList",
        "playLogList",
        "supportCardList",
        "turnLogList",
        "userPlayLogList",
    }
    mapping_fields = {"status", "references"}
    boolean_fields = {"isStaticNpcScore"}
    text_fields = {"idolCardId", "idolCardSkinId", "produceId"}
    opaque: dict[str, object] = {}
    for field_name in _OPAQUE_ROOT_RUNTIME_FIELDS:
        if field_name in list_fields:
            opaque[field_name] = []
        elif field_name in mapping_fields:
            opaque[field_name] = {}
        elif field_name in boolean_fields:
            opaque[field_name] = False
        elif field_name in text_fields:
            opaque[field_name] = ""
        else:
            opaque[field_name] = 0
    opaque.update(_neutral_v330_root_extensions())
    return LocalSaveExamRootRuntimeState(
        command_list=CanonicalJsonValue.from_value([], "command_list"),
        draw_card_guid_list=(),
        is_turn_card_grave=False,
        is_turn_card_lost=False,
        is_exam_end_complete=False,
        opaque_fields=CanonicalJsonValue.from_value(opaque, "opaque_fields"),
    )


@dataclass(frozen=True, order=True, slots=True)
class LocalSaveExamCardRuntimeState:
    """Effect-relevant mutable card state retained without semantic guesses."""

    play_count: int
    status_effect: CanonicalJsonValue
    affect_grow_effect_id_list: CanonicalJsonValue
    grow_effect_exam_start_after_list: CanonicalJsonValue
    is_move_produce_exam_effect_use_in_turn: bool
    stamina_consumption_specify_effect_list: CanonicalJsonValue
    customize_count_list: CanonicalJsonValue
    produce_card_skin_id: str
    produce_card_skin_asset_id: str

    def __post_init__(self) -> None:
        _integer(self.play_count, "play_count")
        for name in (
            "status_effect",
            "affect_grow_effect_id_list",
            "grow_effect_exam_start_after_list",
            "stamina_consumption_specify_effect_list",
            "customize_count_list",
        ):
            if not isinstance(getattr(self, name), CanonicalJsonValue):
                raise TypeError(f"{name} must be CanonicalJsonValue")
        _boolean(
            self.is_move_produce_exam_effect_use_in_turn,
            "is_move_produce_exam_effect_use_in_turn",
        )
        _plain_text(self.produce_card_skin_id, "produce_card_skin_id")
        _plain_text(self.produce_card_skin_asset_id, "produce_card_skin_asset_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "play_count": self.play_count,
            "status_effect": self.status_effect.to_value(),
            "affect_grow_effect_id_list": self.affect_grow_effect_id_list.to_value(),
            "grow_effect_exam_start_after_list": (
                self.grow_effect_exam_start_after_list.to_value()
            ),
            "is_move_produce_exam_effect_use_in_turn": (
                self.is_move_produce_exam_effect_use_in_turn
            ),
            "stamina_consumption_specify_effect_list": (
                self.stamina_consumption_specify_effect_list.to_value()
            ),
            "customize_count_list": self.customize_count_list.to_value(),
            "produce_card_skin_id": self.produce_card_skin_id,
            "produce_card_skin_asset_id": self.produce_card_skin_asset_id,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "LocalSaveExamCardRuntimeState":
        if not isinstance(payload, Mapping):
            raise ValueError("card runtime_state must be an object")
        fields = {
            "play_count",
            "status_effect",
            "affect_grow_effect_id_list",
            "grow_effect_exam_start_after_list",
            "is_move_produce_exam_effect_use_in_turn",
            "stamina_consumption_specify_effect_list",
            "customize_count_list",
            "produce_card_skin_id",
            "produce_card_skin_asset_id",
        }
        _exact(payload, fields, "card runtime_state")
        return cls(
            play_count=_integer(payload["play_count"], "play_count"),
            status_effect=CanonicalJsonValue.from_value(
                payload["status_effect"], "status_effect"
            ),
            affect_grow_effect_id_list=CanonicalJsonValue.from_value(
                payload["affect_grow_effect_id_list"],
                "affect_grow_effect_id_list",
            ),
            grow_effect_exam_start_after_list=CanonicalJsonValue.from_value(
                payload["grow_effect_exam_start_after_list"],
                "grow_effect_exam_start_after_list",
            ),
            is_move_produce_exam_effect_use_in_turn=_boolean(
                payload["is_move_produce_exam_effect_use_in_turn"],
                "is_move_produce_exam_effect_use_in_turn",
            ),
            stamina_consumption_specify_effect_list=CanonicalJsonValue.from_value(
                payload["stamina_consumption_specify_effect_list"],
                "stamina_consumption_specify_effect_list",
            ),
            customize_count_list=CanonicalJsonValue.from_value(
                payload["customize_count_list"], "customize_count_list"
            ),
            produce_card_skin_id=_plain_text(
                payload["produce_card_skin_id"], "produce_card_skin_id"
            ),
            produce_card_skin_asset_id=_plain_text(
                payload["produce_card_skin_asset_id"],
                "produce_card_skin_asset_id",
            ),
        )


def empty_local_save_exam_card_runtime_state() -> LocalSaveExamCardRuntimeState:
    """Return the exact idle v3.2.3 runtime shape for synthetic evidence tests."""

    return LocalSaveExamCardRuntimeState(
        play_count=0,
        status_effect=CanonicalJsonValue.from_value(
            _EMPTY_STATUS_EFFECT, "status_effect"
        ),
        affect_grow_effect_id_list=CanonicalJsonValue.from_value(
            [], "affect_grow_effect_id_list"
        ),
        grow_effect_exam_start_after_list=CanonicalJsonValue.from_value(
            [], "grow_effect_exam_start_after_list"
        ),
        is_move_produce_exam_effect_use_in_turn=False,
        stamina_consumption_specify_effect_list=CanonicalJsonValue.from_value(
            [], "stamina_consumption_specify_effect_list"
        ),
        customize_count_list=CanonicalJsonValue.from_value(
            [], "customize_count_list"
        ),
        produce_card_skin_id="",
        produce_card_skin_asset_id="",
    )


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{label} must be a list of non-empty text")
    result = tuple(value)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} must not contain duplicates")
    return result


def _integer_list(
    value: object,
    label: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return tuple(
        _integer(
            item,
            f"{label}[{index}]",
            minimum=minimum,
            maximum=maximum,
        )
        for index, item in enumerate(value)
    )


@dataclass(frozen=True, order=True, slots=True)
class LocalSaveExamCard:
    zone_order: int
    guid: str
    card_id: str
    base_upgrade: int
    temporary_upgrade: int
    effective_upgrade: int
    support_upgrade_ids: tuple[str, ...] = ()
    fixed_deck_order: int | None = None
    # ``None`` is reserved for migrated schema-v2/v3 artifacts.  Native
    # deterministic consumers must reject it rather than assume defaults.
    runtime_state: LocalSaveExamCardRuntimeState | None = None

    def __post_init__(self) -> None:
        _integer(self.zone_order, "zone_order")
        _text(self.guid, "guid")
        _text(self.card_id, "card_id")
        _integer(self.base_upgrade, "base_upgrade", maximum=3)
        _integer(self.temporary_upgrade, "temporary_upgrade", maximum=3)
        _integer(self.effective_upgrade, "effective_upgrade", maximum=3)
        support_ids = tuple(self.support_upgrade_ids)
        if any(not isinstance(value, str) or not value.strip() for value in support_ids):
            raise ValueError("support_upgrade_ids must contain non-empty text")
        if len(set(support_ids)) != len(support_ids):
            raise ValueError("support_upgrade_ids must not contain duplicates")
        object.__setattr__(self, "support_upgrade_ids", support_ids)
        _optional_signed_int32(self.fixed_deck_order, "fixed_deck_order")
        if self.runtime_state is not None and not isinstance(
            self.runtime_state, LocalSaveExamCardRuntimeState
        ):
            raise TypeError(
                "runtime_state must be LocalSaveExamCardRuntimeState or None"
            )
        expected = self.base_upgrade + self.temporary_upgrade + len(support_ids)
        if self.effective_upgrade != expected:
            raise ValueError(
                "effective upgrade is not explained by base, temporary, and support upgrades"
            )

    @property
    def base_ref(self) -> ZoneCardRef:
        return ZoneCardRef(self.card_id, self.base_upgrade)

    @property
    def effective_ref(self) -> ZoneCardRef:
        return ZoneCardRef(self.card_id, self.effective_upgrade)

    def runtime_payload(self) -> dict[str, object] | None:
        """Return canonical effect state, excluding identity and zone position.

        GUID/card_id and zone_order are deliberately outside this payload.
        Upgrade layers, support lineage, fixed-order metadata, customization,
        and all still-semantically-opaque native runtime fields are included.
        """

        if self.runtime_state is None:
            return None
        return {
            "base_upgrade": self.base_upgrade,
            "temporary_upgrade": self.temporary_upgrade,
            "effective_upgrade": self.effective_upgrade,
            "support_upgrade_ids": list(self.support_upgrade_ids),
            "fixed_deck_order": self.fixed_deck_order,
            "runtime_state": self.runtime_state.to_dict(),
        }

    @property
    def runtime_state_digest(self) -> str | None:
        payload = self.runtime_payload()
        if payload is None:
            return None
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "zone_order": self.zone_order,
            "guid": self.guid,
            "card_id": self.card_id,
            "base_upgrade": self.base_upgrade,
            "temporary_upgrade": self.temporary_upgrade,
            "effective_upgrade": self.effective_upgrade,
            "support_upgrade_ids": list(self.support_upgrade_ids),
            # ``null`` is materially different from zero: null means the
            # native source field was absent, while zero is an observed value.
            "fixed_deck_order": self.fixed_deck_order,
            "runtime_state": (
                None if self.runtime_state is None else self.runtime_state.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "LocalSaveExamCard":
        return cls._from_dict(
            payload, source_schema_version=AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION
        )

    @classmethod
    def _from_dict(
        cls,
        payload: Mapping[str, object],
        *,
        source_schema_version: int,
    ) -> "LocalSaveExamCard":
        if not isinstance(payload, Mapping):
            raise ValueError("local-save exam card must be an object")
        fields = {
            "zone_order",
            "guid",
            "card_id",
            "base_upgrade",
            "temporary_upgrade",
            "effective_upgrade",
            "support_upgrade_ids",
        }
        if source_schema_version >= 3:
            fields.add("fixed_deck_order")
        if source_schema_version >= 4:
            fields.add("runtime_state")
        _exact(payload, fields, "local-save exam card")
        raw_supports = payload["support_upgrade_ids"]
        if not isinstance(raw_supports, list):
            raise ValueError("support_upgrade_ids must be a list")
        return cls(
            zone_order=_integer(payload["zone_order"], "zone_order"),
            guid=_text(payload["guid"], "guid"),
            card_id=_text(payload["card_id"], "card_id"),
            base_upgrade=_integer(
                payload["base_upgrade"], "base_upgrade", maximum=3
            ),
            temporary_upgrade=_integer(
                payload["temporary_upgrade"], "temporary_upgrade", maximum=3
            ),
            effective_upgrade=_integer(
                payload["effective_upgrade"], "effective_upgrade", maximum=3
            ),
            support_upgrade_ids=tuple(raw_supports),  # type: ignore[arg-type]
            fixed_deck_order=(
                None
                if source_schema_version < 3
                else _optional_signed_int32(
                    payload["fixed_deck_order"], "fixed_deck_order"
                )
            ),
            runtime_state=(
                None
                if source_schema_version < 4 or payload["runtime_state"] is None
                else LocalSaveExamCardRuntimeState.from_dict(
                    payload["runtime_state"]  # type: ignore[arg-type]
                )
            ),
        )


def _parse_card(raw: object, *, zone: str, order: int) -> LocalSaveExamCard:
    if not isinstance(raw, Mapping):
        raise ValueError(f"ExamSaveData.{zone}[{order}] must be an object")
    _exact(raw, _RAW_CARD_FIELDS, f"ExamSaveData.{zone}[{order}]")
    card_data = raw.get("_cardData")
    if not isinstance(card_data, Mapping):
        raise ValueError(
            f"ExamSaveData.{zone}[{order}]._cardData must be an object"
        )
    _exact(
        card_data,
        _RAW_CARD_DATA_FIELDS,
        f"ExamSaveData.{zone}[{order}]._cardData",
    )
    label = f"ExamSaveData.{zone}[{order}]"
    status_effect = raw["_statusEffect"]
    if not isinstance(status_effect, Mapping):
        raise ValueError(f"{label}._statusEffect must be an object")
    for field in (
        "_growEffectExamStartAfterList",
        "_affectGrowEffectIdList",
        "_staminaConsumptionSpecifyEffectList",
    ):
        if not isinstance(raw[field], list):
            raise ValueError(f"{label}.{field} must be a list")
    if not isinstance(card_data["_customizeCountList"], list):
        raise ValueError(f"{label}._cardData._customizeCountList must be a list")
    support_ids = _string_list(
        raw.get("_supportUpgradeIdList"),
        f"ExamSaveData.{zone}[{order}]._supportUpgradeIdList",
    )
    fixed_deck_order = (
        _signed_int32(
            raw["_fixedDeckOrder"],
            f"ExamSaveData.{zone}[{order}]._fixedDeckOrder",
        )
    )
    return LocalSaveExamCard(
        zone_order=order,
        guid=_text(raw.get("_guid"), f"ExamSaveData.{zone}[{order}]._guid"),
        card_id=_text(
            card_data.get("_id"), f"ExamSaveData.{zone}[{order}]._cardData._id"
        ),
        base_upgrade=_integer(
            raw.get("_baseUpgradeCount"),
            f"ExamSaveData.{zone}[{order}]._baseUpgradeCount",
            maximum=3,
        ),
        temporary_upgrade=_integer(
            raw.get("_tmpUpgradeCount"),
            f"ExamSaveData.{zone}[{order}]._tmpUpgradeCount",
            maximum=3,
        ),
        effective_upgrade=_integer(
            card_data.get("_upgradeCount"),
            f"ExamSaveData.{zone}[{order}]._cardData._upgradeCount",
            maximum=3,
        ),
        support_upgrade_ids=support_ids,
        fixed_deck_order=fixed_deck_order,
        runtime_state=LocalSaveExamCardRuntimeState(
            play_count=_integer(raw["_playCount"], f"{label}._playCount"),
            status_effect=CanonicalJsonValue.from_value(
                status_effect, f"{label}._statusEffect"
            ),
            affect_grow_effect_id_list=CanonicalJsonValue.from_value(
                raw["_affectGrowEffectIdList"],
                f"{label}._affectGrowEffectIdList",
            ),
            grow_effect_exam_start_after_list=CanonicalJsonValue.from_value(
                raw["_growEffectExamStartAfterList"],
                f"{label}._growEffectExamStartAfterList",
            ),
            is_move_produce_exam_effect_use_in_turn=_boolean(
                raw["_isMoveProduceExamEffectUseInTurn"],
                f"{label}._isMoveProduceExamEffectUseInTurn",
            ),
            stamina_consumption_specify_effect_list=CanonicalJsonValue.from_value(
                raw["_staminaConsumptionSpecifyEffectList"],
                f"{label}._staminaConsumptionSpecifyEffectList",
            ),
            customize_count_list=CanonicalJsonValue.from_value(
                card_data["_customizeCountList"],
                f"{label}._cardData._customizeCountList",
            ),
            produce_card_skin_id=_plain_text(
                card_data["_produceCardSkinId"],
                f"{label}._cardData._produceCardSkinId",
            ),
            produce_card_skin_asset_id=_plain_text(
                card_data["_produceCardSkinAssetId"],
                f"{label}._cardData._produceCardSkinAssetId",
            ),
        ),
    )


def parse_raw_local_save_exam_card(
    raw: object,
    *,
    source_label: str,
    order: int = 0,
) -> LocalSaveExamCard:
    """Strictly parse one native card serializer row for typed consumers."""

    if not isinstance(source_label, str) or not source_label:
        raise ValueError("source_label must be non-empty text")
    if isinstance(order, bool) or not isinstance(order, int) or order < 0:
        raise ValueError("order must be a non-negative integer")
    return _parse_card(raw, zone=source_label, order=order)


def _parse_optional_playing_card(raw: object) -> LocalSaveExamCard | None:
    """Parse ``playingCard`` while accepting only the exact empty sentinel.

    ExamSaveData serializes an empty ExamCardModel-shaped object while the
    game is waiting for input.  Treating any arbitrary malformed object as
    that sentinel could hide a resolving card, so every identity/upgrade
    field used here must be empty/zero before returning ``None``.
    """

    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("ExamSaveData.playingCard must be an object or null")
    _exact(raw, _RAW_CARD_FIELDS, "ExamSaveData.playingCard")
    card_data = raw.get("_cardData")
    if not isinstance(card_data, Mapping):
        raise ValueError("ExamSaveData.playingCard._cardData must be an object")
    _exact(card_data, _RAW_CARD_DATA_FIELDS, "ExamSaveData.playingCard._cardData")
    support_ids = raw.get("_supportUpgradeIdList")
    if not isinstance(support_ids, list):
        raise ValueError(
            "ExamSaveData.playingCard._supportUpgradeIdList must be a list"
        )
    _plain_text(raw["_guid"], "ExamSaveData.playingCard._guid")
    _plain_text(card_data["_id"], "ExamSaveData.playingCard._cardData._id")
    _integer(
        raw["_baseUpgradeCount"],
        "ExamSaveData.playingCard._baseUpgradeCount",
        maximum=3,
    )
    _integer(
        raw["_tmpUpgradeCount"],
        "ExamSaveData.playingCard._tmpUpgradeCount",
        maximum=3,
    )
    _integer(
        card_data["_upgradeCount"],
        "ExamSaveData.playingCard._cardData._upgradeCount",
        maximum=3,
    )
    _signed_int32(
        raw["_fixedDeckOrder"], "ExamSaveData.playingCard._fixedDeckOrder"
    )
    _integer(raw["_playCount"], "ExamSaveData.playingCard._playCount")
    _boolean(
        raw["_isMoveProduceExamEffectUseInTurn"],
        "ExamSaveData.playingCard._isMoveProduceExamEffectUseInTurn",
    )
    if not isinstance(raw["_statusEffect"], Mapping):
        raise ValueError("ExamSaveData.playingCard._statusEffect must be an object")
    for field in (
        "_affectGrowEffectIdList",
        "_growEffectExamStartAfterList",
        "_staminaConsumptionSpecifyEffectList",
    ):
        if not isinstance(raw[field], list):
            raise ValueError(f"ExamSaveData.playingCard.{field} must be a list")
        _validate_json_value(raw[field], f"ExamSaveData.playingCard.{field}")
    if not isinstance(card_data["_customizeCountList"], list):
        raise ValueError(
            "ExamSaveData.playingCard._cardData._customizeCountList must be a list"
        )
    _validate_json_value(
        raw["_statusEffect"], "ExamSaveData.playingCard._statusEffect"
    )
    _validate_json_value(
        card_data["_customizeCountList"],
        "ExamSaveData.playingCard._cardData._customizeCountList",
    )
    _plain_text(
        card_data["_produceCardSkinId"],
        "ExamSaveData.playingCard._cardData._produceCardSkinId",
    )
    _plain_text(
        card_data["_produceCardSkinAssetId"],
        "ExamSaveData.playingCard._cardData._produceCardSkinAssetId",
    )
    sentinel = (
        raw.get("_guid") == ""
        and card_data.get("_id") == ""
        and raw.get("_baseUpgradeCount") == 0
        and raw.get("_tmpUpgradeCount") == 0
        and card_data.get("_upgradeCount") == 0
        and support_ids == []
        and raw.get("_fixedDeckOrder") == 0
        and raw.get("_playCount") == 0
        and raw.get("_statusEffect") == _EMPTY_STATUS_EFFECT
        and raw.get("_affectGrowEffectIdList") == []
        and raw.get("_growEffectExamStartAfterList") == []
        and raw.get("_isMoveProduceExamEffectUseInTurn") is False
        and raw.get("_staminaConsumptionSpecifyEffectList") == []
        and card_data.get("_customizeCountList") == []
        and card_data.get("_produceCardSkinId") == ""
        and card_data.get("_produceCardSkinAssetId") == ""
    )
    if sentinel:
        return None
    return _parse_card(raw, zone="playingCard", order=0)


def _parse_auxiliary_zone(
    payload: Mapping[str, object], source_name: str
) -> tuple[LocalSaveExamCard, ...]:
    raw_zone = payload.get(source_name)
    if not isinstance(raw_zone, list):
        raise ValueError(f"ExamSaveData.{source_name} must be a list")
    return tuple(
        _parse_card(raw, zone=source_name, order=order)
        for order, raw in enumerate(raw_zone)
    )


def _parse_ordered_card_groups(
    payload: Mapping[str, object], source_name: str
) -> tuple[tuple[LocalSaveExamCard, ...], ...]:
    """Parse native ``List<ExamSaveCardListData>`` without flattening it."""

    raw_groups = payload.get(source_name)
    if not isinstance(raw_groups, list):
        raise ValueError(f"ExamSaveData.{source_name} must be a list")
    groups: list[tuple[LocalSaveExamCard, ...]] = []
    for group_order, raw_group in enumerate(raw_groups):
        if not isinstance(raw_group, Mapping):
            raise ValueError(
                f"ExamSaveData.{source_name}[{group_order}] must be an object"
            )
        raw_cards = raw_group.get("list")
        if not isinstance(raw_cards, list):
            raise ValueError(
                f"ExamSaveData.{source_name}[{group_order}].list must be a list"
            )
        zone = f"{source_name}[{group_order}].list"
        groups.append(
            tuple(
                _parse_card(raw, zone=zone, order=card_order)
                for card_order, raw in enumerate(raw_cards)
            )
        )
    return tuple(groups)


def _ordered_card_sequence(
    values: object, label: str
) -> tuple[LocalSaveExamCard, ...]:
    try:
        result = tuple(values)  # type: ignore[arg-type]
    except TypeError as error:
        raise TypeError(f"{label} must contain LocalSaveExamCard values") from error
    if not all(isinstance(value, LocalSaveExamCard) for value in result):
        raise TypeError(f"{label} must contain LocalSaveExamCard values")
    if tuple(value.zone_order for value in result) != tuple(range(len(result))):
        raise ValueError(f"{label} zone_order must be contiguous")
    return result


def _ordered_card_groups(
    values: object, label: str
) -> tuple[tuple[LocalSaveExamCard, ...], ...]:
    try:
        raw_groups = tuple(values)  # type: ignore[arg-type]
    except TypeError as error:
        raise TypeError(f"{label} must be an ordered list of card lists") from error
    groups: list[tuple[LocalSaveExamCard, ...]] = []
    for group_order, raw_group in enumerate(raw_groups):
        if isinstance(raw_group, LocalSaveExamCard):
            raise TypeError(f"{label} must be an ordered list of card lists")
        groups.append(
            _ordered_card_sequence(raw_group, f"{label}[{group_order}]")
        )
    return tuple(groups)


@dataclass(frozen=True, slots=True)
class LocalSaveExamZones:
    hand: tuple[LocalSaveExamCard, ...]
    deck: tuple[LocalSaveExamCard, ...]
    grave: tuple[LocalSaveExamCard, ...]
    lost: tuple[LocalSaveExamCard, ...]
    hold: tuple[LocalSaveExamCard, ...]

    def __post_init__(self) -> None:
        all_cards: list[LocalSaveExamCard] = []
        for name in ("hand", "deck", "grave", "lost", "hold"):
            values = tuple(getattr(self, name))
            if not all(isinstance(value, LocalSaveExamCard) for value in values):
                raise TypeError(f"{name} must contain LocalSaveExamCard values")
            if tuple(value.zone_order for value in values) != tuple(range(len(values))):
                raise ValueError(f"{name} zone_order must be contiguous")
            object.__setattr__(self, name, values)
            all_cards.extend(values)
        guids = [value.guid for value in all_cards]
        if len(set(guids)) != len(guids):
            raise ValueError("ExamSaveData card GUIDs must be unique across zones")
        for name in ("deck", "grave", "lost"):
            for card in getattr(self, name):
                # SetTemporaryUpgrade writes a persistent field on the card
                # instance; native code does not prove that moving the card
                # out of Hand clears it.  Support HandAdd lineage, however,
                # is turn-local and is only accepted in the visible/held Hand.
                if card.support_upgrade_ids:
                    raise ValueError(
                        f"{name} contains a support-upgraded card"
                    )

    @property
    def all_cards(self) -> tuple[LocalSaveExamCard, ...]:
        return (*self.hand, *self.deck, *self.grave, *self.lost, *self.hold)

    @property
    def base_counter(self) -> Counter[ZoneCardRef]:
        return Counter(card.base_ref for card in self.all_cards)

    def to_dict(self) -> dict[str, object]:
        return {
            name: [value.to_dict() for value in getattr(self, name)]
            for name in ("hand", "deck", "grave", "lost", "hold")
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "LocalSaveExamZones":
        return cls._from_dict(
            payload, source_schema_version=AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION
        )

    @classmethod
    def _from_dict(
        cls,
        payload: Mapping[str, object],
        *,
        source_schema_version: int,
    ) -> "LocalSaveExamZones":
        if not isinstance(payload, Mapping):
            raise ValueError("local-save exam zones must be an object")
        fields = {"hand", "deck", "grave", "lost", "hold"}
        _exact(payload, fields, "local-save exam zones")
        values: dict[str, tuple[LocalSaveExamCard, ...]] = {}
        for name in fields:
            raw = payload[name]
            if not isinstance(raw, list) or not all(
                isinstance(item, Mapping) for item in raw
            ):
                raise ValueError(f"local-save zone {name} must be a list of objects")
            values[name] = tuple(
                LocalSaveExamCard._from_dict(
                    item, source_schema_version=source_schema_version
                )
                for item in raw
            )
        return cls(**values)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class LocalSaveExamState:
    character_id: str
    setting_id: str
    exam_type: int
    step_type_value: int
    phase: int
    current_turn: int
    limit_turn: int
    remain_turn: int
    extra_turn: int
    score: int
    stamina: int
    max_stamina: int
    block: int
    vocal_bonus_permille: int
    dance_bonus_permille: int
    visual_bonus_permille: int
    turn_parameter_types: tuple[int, ...]
    turn_card_play_count: int
    exam_card_play_count: int
    is_turn_card_play_end: bool
    random_state: int
    turn_use_support_ids: tuple[str, ...]
    zones: LocalSaveExamZones
    playing_card: LocalSaveExamCard | None = None
    removed_cards: tuple[LocalSaveExamCard, ...] = ()
    future_deck: tuple[tuple[LocalSaveExamCard, ...], ...] = ()
    # ``None`` is reserved for migrated schema-v2 artifacts, which never
    # retained pastDeckList and therefore cannot safely claim it was empty.
    past_deck: tuple[tuple[LocalSaveExamCard, ...], ...] | None = ()
    # ``None`` marks migrated schema-v2/v3/v4 evidence, whose reducer dropped
    # 51 native root fields and cannot prove an actionable settled boundary.
    root_runtime: LocalSaveExamRootRuntimeState | None = None

    def __post_init__(self) -> None:
        _text(self.character_id, "character_id")
        _text(self.setting_id, "setting_id")
        _integer(self.exam_type, "exam_type")
        _integer(self.step_type_value, "step_type_value")
        _integer(self.phase, "phase")
        _integer(self.current_turn, "current_turn", minimum=1)
        _integer(self.limit_turn, "limit_turn", minimum=1)
        _integer(self.remain_turn, "remain_turn")
        _integer(self.extra_turn, "extra_turn")
        _integer(self.score, "score")
        _integer(self.stamina, "stamina")
        _integer(self.max_stamina, "max_stamina", minimum=1)
        _integer(self.block, "block")
        _integer(self.vocal_bonus_permille, "vocal_bonus_permille", minimum=0)
        _integer(self.dance_bonus_permille, "dance_bonus_permille", minimum=0)
        _integer(self.visual_bonus_permille, "visual_bonus_permille", minimum=0)
        turn_types = tuple(self.turn_parameter_types)
        if self.exam_type == _LESSON_EXAM_TYPE:
            if turn_types:
                raise ValueError(
                    "lesson turn_parameter_types must use the native empty list"
                )
        elif len(turn_types) not in {
            self.limit_turn,
            self.limit_turn + self.extra_turn,
        } or any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value not in {1, 2, 3}
            for value in turn_types
        ):
            raise ValueError(
                "turn_parameter_types must contain one Vocal/Dance/Visual value "
                "per base turn, optionally including serialized added turns"
            )
        object.__setattr__(self, "turn_parameter_types", turn_types)
        _integer(self.turn_card_play_count, "turn_card_play_count")
        _integer(self.exam_card_play_count, "exam_card_play_count")
        _boolean(self.is_turn_card_play_end, "is_turn_card_play_end")
        _integer(self.random_state, "random_state", maximum=0xFFFFFFFF)
        used = tuple(self.turn_use_support_ids)
        if any(not isinstance(value, str) or not value.strip() for value in used):
            raise ValueError("turn_use_support_ids must contain non-empty text")
        if len(set(used)) != len(used):
            raise ValueError("turn_use_support_ids must not contain duplicates")
        object.__setattr__(self, "turn_use_support_ids", used)
        if not isinstance(self.zones, LocalSaveExamZones):
            raise TypeError("zones must be LocalSaveExamZones")
        if self.playing_card is not None and not isinstance(
            self.playing_card, LocalSaveExamCard
        ):
            raise TypeError("playing_card must be LocalSaveExamCard or None")
        removed = _ordered_card_sequence(self.removed_cards, "removed_cards")
        future = _ordered_card_groups(self.future_deck, "future_deck")
        past = (
            None
            if self.past_deck is None
            else _ordered_card_groups(self.past_deck, "past_deck")
        )
        object.__setattr__(self, "removed_cards", removed)
        object.__setattr__(self, "future_deck", future)
        object.__setattr__(self, "past_deck", past)
        if self.root_runtime is not None and not isinstance(
            self.root_runtime, LocalSaveExamRootRuntimeState
        ):
            raise TypeError(
                "root_runtime must be LocalSaveExamRootRuntimeState or None"
            )
        if self.stamina > self.max_stamina:
            raise ValueError("stamina exceeds max_stamina")
        expected_remain = self.limit_turn - self.current_turn + 1 + self.extra_turn
        terminal_complete = bool(
            self.root_runtime is not None
            and self.root_runtime.is_exam_end_complete
        )
        if self.remain_turn != expected_remain and not (
            terminal_complete and self.remain_turn == 0
        ):
            raise ValueError(
                "remain_turn does not match limit_turn/current_turn/extra_turn"
            )
        installed = {
            support_id
            for card in (*self.zones.hand, *self.zones.hold)
            for support_id in card.support_upgrade_ids
        }
        if installed - set(used):
            raise ValueError(
                "Hand/Hold support upgrades are absent from turn_use_support_ids"
            )

    @property
    def all_card_instances(self) -> tuple[LocalSaveExamCard, ...]:
        grouped = (*self.future_deck, *(self.past_deck or ()))
        return (
            *self.zones.all_cards,
            *((self.playing_card,) if self.playing_card is not None else ()),
            *self.removed_cards,
            *(card for group in grouped for card in group),
        )

    @property
    def has_complete_card_runtime_state(self) -> bool:
        return all(card.runtime_state is not None for card in self.all_card_instances)

    @property
    def has_complete_root_runtime_state(self) -> bool:
        return self.root_runtime is not None

    @property
    def removed_cards_are_positioned_tombstones(self) -> bool:
        """Whether removedCardList only mirrors cards already in Grave/Lost.

        PC keeps a pre-play card snapshot in ``removedCardList`` after the
        resolved card has reached Grave or Lost.  The snapshot can persist
        after a later reshuffle draws that same GUID back into Hand or Deck;
        it is presentation/history, not a sixth live zone.  GUID plus card ID
        bind the stale snapshot wherever the live instance is currently
        positioned; temporary upgrade and play-count fields may legitimately
        differ from the resolved card instance.
        """

        if not self.removed_cards:
            return True
        removed_guids = tuple(card.guid for card in self.removed_cards)
        if len(removed_guids) != len(set(removed_guids)):
            return False
        positioned = (
            *self.zones.hand,
            *self.zones.deck,
            *self.zones.grave,
            *self.zones.lost,
            *self.zones.hold,
        )
        for removed in self.removed_cards:
            matches = tuple(
                card for card in positioned if card.guid == removed.guid
            )
            if len(matches) != 1 or matches[0].card_id != removed.card_id:
                return False
        return True

    @property
    def is_native_actionable_settled(self) -> bool:
        """Whether root and transition fields prove an actionable Main state."""

        runtime = self.root_runtime
        supported_stage = (
            self.exam_type == _LESSON_EXAM_TYPE
            and self.step_type_value in _PLAN2_NATIVE_LESSON_STEP_TYPES
        ) or (
            self.exam_type == _AUDITION_EXAM_TYPE
            and self.step_type_value in _PLAN2_NATIVE_AUDITION_STEP_TYPES
        )
        return bool(
            runtime is not None
            and supported_stage
            and self.phase == _MAIN_PHASE
            and not self.is_turn_card_play_end
            and self.playing_card is None
            and self.removed_cards_are_positioned_tombstones
            and runtime.command_list_is_empty
            and not runtime.is_exam_end_complete
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "character_id": self.character_id,
            "setting_id": self.setting_id,
            "exam_type": self.exam_type,
            "step_type_value": self.step_type_value,
            "phase": self.phase,
            "current_turn": self.current_turn,
            "limit_turn": self.limit_turn,
            "remain_turn": self.remain_turn,
            "extra_turn": self.extra_turn,
            "score": self.score,
            "stamina": self.stamina,
            "max_stamina": self.max_stamina,
            "block": self.block,
            "vocal_bonus_permille": self.vocal_bonus_permille,
            "dance_bonus_permille": self.dance_bonus_permille,
            "visual_bonus_permille": self.visual_bonus_permille,
            "turn_parameter_types": list(self.turn_parameter_types),
            "turn_card_play_count": self.turn_card_play_count,
            "exam_card_play_count": self.exam_card_play_count,
            "is_turn_card_play_end": self.is_turn_card_play_end,
            "random_state": self.random_state,
            "turn_use_support_ids": list(self.turn_use_support_ids),
            "zones": self.zones.to_dict(),
            "playing_card": (
                None if self.playing_card is None else self.playing_card.to_dict()
            ),
            "removed_cards": [card.to_dict() for card in self.removed_cards],
            "future_deck": [
                [card.to_dict() for card in group] for group in self.future_deck
            ],
            "past_deck": (
                None
                if self.past_deck is None
                else [
                    [card.to_dict() for card in group]
                    for group in self.past_deck
                ]
            ),
            "root_runtime": (
                None if self.root_runtime is None else self.root_runtime.to_dict()
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "LocalSaveExamState":
        return cls._from_dict(
            payload, source_schema_version=AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION
        )

    @classmethod
    def _from_dict(
        cls,
        payload: Mapping[str, object],
        *,
        source_schema_version: int,
    ) -> "LocalSaveExamState":
        if not isinstance(payload, Mapping):
            raise ValueError("local-save exam state must be an object")
        fields = {
            "character_id",
            "setting_id",
            "exam_type",
            "step_type_value",
            "phase",
            "current_turn",
            "limit_turn",
            "remain_turn",
            "extra_turn",
            "score",
            "stamina",
            "max_stamina",
            "block",
            "vocal_bonus_permille",
            "dance_bonus_permille",
            "visual_bonus_permille",
            "turn_parameter_types",
            "turn_card_play_count",
            "exam_card_play_count",
            "is_turn_card_play_end",
            "random_state",
            "turn_use_support_ids",
            "zones",
            "playing_card",
            "removed_cards",
            "future_deck",
        }
        if source_schema_version >= 3:
            fields.add("past_deck")
        if source_schema_version >= 5:
            fields.add("root_runtime")
        _exact(payload, fields, "local-save exam state")
        raw_used = payload["turn_use_support_ids"]
        if not isinstance(raw_used, list):
            raise ValueError("turn_use_support_ids must be a list")
        raw_turn_types = payload["turn_parameter_types"]
        if not isinstance(raw_turn_types, list):
            raise ValueError("turn_parameter_types must be a list")
        raw_playing = payload["playing_card"]
        if raw_playing is not None and not isinstance(raw_playing, Mapping):
            raise ValueError("playing_card must be an object or null")
        raw_removed = payload["removed_cards"]
        raw_future = payload["future_deck"]
        if not isinstance(raw_removed, list) or not all(
            isinstance(item, Mapping) for item in raw_removed
        ):
            raise ValueError("removed_cards must be a list of objects")
        if not isinstance(raw_future, list):
            raise ValueError("future_deck must be a list")
        if source_schema_version < 3:
            if raw_future:
                raise ValueError(
                    "cannot migrate non-empty schema-v2 future_deck because "
                    "native group boundaries were not preserved"
                )
            raw_past: list[object] | None = None
        else:
            raw_past_value = payload["past_deck"]
            if raw_past_value is not None and not isinstance(
                raw_past_value, list
            ):
                raise ValueError("past_deck must be a list or null")
            raw_past = raw_past_value
        raw_root_runtime = (
            payload["root_runtime"] if source_schema_version >= 5 else None
        )
        if raw_root_runtime is not None and not isinstance(
            raw_root_runtime, Mapping
        ):
            raise ValueError("root_runtime must be an object or null")

        def parse_groups(
            raw_groups: list[object], label: str
        ) -> tuple[tuple[LocalSaveExamCard, ...], ...]:
            groups: list[tuple[LocalSaveExamCard, ...]] = []
            for group_order, raw_group in enumerate(raw_groups):
                if not isinstance(raw_group, list) or not all(
                    isinstance(item, Mapping) for item in raw_group
                ):
                    raise ValueError(f"{label}[{group_order}] must be a list of objects")
                groups.append(
                    tuple(
                        LocalSaveExamCard._from_dict(
                            item,
                            source_schema_version=source_schema_version,
                        )
                        for item in raw_group
                    )
                )
            return tuple(groups)

        return cls(
            character_id=_text(payload["character_id"], "character_id"),
            setting_id=_text(payload["setting_id"], "setting_id"),
            exam_type=_integer(payload["exam_type"], "exam_type"),
            step_type_value=_integer(payload["step_type_value"], "step_type_value"),
            phase=_integer(payload["phase"], "phase"),
            current_turn=_integer(payload["current_turn"], "current_turn", minimum=1),
            limit_turn=_integer(payload["limit_turn"], "limit_turn", minimum=1),
            remain_turn=_integer(payload["remain_turn"], "remain_turn"),
            extra_turn=_integer(payload["extra_turn"], "extra_turn"),
            score=_integer(payload["score"], "score"),
            stamina=_integer(payload["stamina"], "stamina"),
            max_stamina=_integer(payload["max_stamina"], "max_stamina", minimum=1),
            block=_integer(payload["block"], "block"),
            vocal_bonus_permille=_integer(
                payload["vocal_bonus_permille"],
                "vocal_bonus_permille",
                minimum=0,
            ),
            dance_bonus_permille=_integer(
                payload["dance_bonus_permille"],
                "dance_bonus_permille",
                minimum=0,
            ),
            visual_bonus_permille=_integer(
                payload["visual_bonus_permille"],
                "visual_bonus_permille",
                minimum=0,
            ),
            turn_parameter_types=tuple(raw_turn_types),  # type: ignore[arg-type]
            turn_card_play_count=_integer(
                payload["turn_card_play_count"], "turn_card_play_count"
            ),
            exam_card_play_count=_integer(
                payload["exam_card_play_count"], "exam_card_play_count"
            ),
            is_turn_card_play_end=_boolean(
                payload["is_turn_card_play_end"], "is_turn_card_play_end"
            ),
            random_state=_integer(
                payload["random_state"], "random_state", maximum=0xFFFFFFFF
            ),
            turn_use_support_ids=tuple(raw_used),  # type: ignore[arg-type]
            zones=LocalSaveExamZones._from_dict(  # type: ignore[arg-type]
                payload["zones"],
                source_schema_version=source_schema_version,
            ),
            playing_card=(
                None
                if raw_playing is None
                else LocalSaveExamCard._from_dict(
                    raw_playing,
                    source_schema_version=source_schema_version,
                )
            ),
            removed_cards=tuple(
                LocalSaveExamCard._from_dict(
                    item, source_schema_version=source_schema_version
                )
                for item in raw_removed
            ),
            future_deck=parse_groups(raw_future, "future_deck"),
            past_deck=(
                None
                if raw_past is None
                else parse_groups(raw_past, "past_deck")
            ),
            root_runtime=(
                None
                if raw_root_runtime is None
                else LocalSaveExamRootRuntimeState.from_dict(raw_root_runtime)
            ),
        )


def parse_local_save_exam_state(payload: Mapping[str, object]) -> LocalSaveExamState:
    """Strictly extract the current audition state from decrypted JSON."""

    if not isinstance(payload, Mapping):
        raise ValueError("ExamSaveData JSON root must be an object")
    _exact(payload, set(_EXAM_SAVE_DATA_ROOT_FIELD_SET), "ExamSaveData root")
    parsed_zones: dict[str, tuple[LocalSaveExamCard, ...]] = {}
    for source_name in _ZONE_FIELDS:
        raw_zone = payload.get(source_name)
        if not isinstance(raw_zone, list):
            raise ValueError(f"ExamSaveData.{source_name} must be a list")
        parsed_zones[source_name.removesuffix("List")] = tuple(
            _parse_card(raw, zone=source_name, order=order)
            for order, raw in enumerate(raw_zone)
        )
    return LocalSaveExamState(
        character_id=_text(payload.get("characterId"), "ExamSaveData.characterId"),
        setting_id=_text(payload.get("settingId"), "ExamSaveData.settingId"),
        exam_type=_integer(payload.get("examType"), "ExamSaveData.examType"),
        step_type_value=_integer(
            payload.get("stepType"), "ExamSaveData.stepType"
        ),
        phase=_integer(payload.get("phase"), "ExamSaveData.phase"),
        current_turn=_integer(
            payload.get("currentTurn"), "ExamSaveData.currentTurn", minimum=1
        ),
        limit_turn=_integer(
            payload.get("limitTurn"), "ExamSaveData.limitTurn", minimum=1
        ),
        remain_turn=_integer(
            payload.get("remainTurn"), "ExamSaveData.remainTurn"
        ),
        extra_turn=_integer(payload.get("extraTurn"), "ExamSaveData.extraTurn"),
        score=_integer(payload.get("parameter"), "ExamSaveData.parameter"),
        stamina=_integer(payload.get("stamina"), "ExamSaveData.stamina"),
        max_stamina=_integer(
            payload.get("maxStamina"), "ExamSaveData.maxStamina", minimum=1
        ),
        block=_integer(payload.get("block"), "ExamSaveData.block"),
        vocal_bonus_permille=_integer(
            payload.get("vocalBonusPermil"),
            "ExamSaveData.vocalBonusPermil",
            minimum=0,
        ),
        dance_bonus_permille=_integer(
            payload.get("danceBonusPermil"),
            "ExamSaveData.danceBonusPermil",
            minimum=0,
        ),
        visual_bonus_permille=_integer(
            payload.get("visualBonusPermil"),
            "ExamSaveData.visualBonusPermil",
            minimum=0,
        ),
        turn_parameter_types=_integer_list(
            payload.get("turnStatusParameterTypeList"),
            "ExamSaveData.turnStatusParameterTypeList",
            minimum=1,
            maximum=3,
        ),
        turn_card_play_count=_integer(
            payload.get("turnCardPlayCount"), "ExamSaveData.turnCardPlayCount"
        ),
        exam_card_play_count=_integer(
            payload.get("examCardPlayCount"), "ExamSaveData.examCardPlayCount"
        ),
        is_turn_card_play_end=_boolean(
            payload.get("isTurnCardPlayEnd"), "ExamSaveData.isTurnCardPlayEnd"
        ),
        random_state=_integer(
            payload.get("random"), "ExamSaveData.random", maximum=0xFFFFFFFF
        ),
        turn_use_support_ids=_string_list(
            payload.get("turnUseSupportCardIdList"),
            "ExamSaveData.turnUseSupportCardIdList",
        ),
        zones=LocalSaveExamZones(
            hand=parsed_zones["hand"],
            deck=parsed_zones["deck"],
            grave=parsed_zones["grave"],
            lost=parsed_zones["lost"],
            hold=parsed_zones["hold"],
        ),
        playing_card=_parse_optional_playing_card(payload.get("playingCard")),
        removed_cards=_parse_auxiliary_zone(payload, "removedCardList"),
        future_deck=_parse_ordered_card_groups(payload, "futureDeckList"),
        past_deck=_parse_ordered_card_groups(payload, "pastDeckList"),
        root_runtime=LocalSaveExamRootRuntimeState.from_raw_root(payload),
    )


@dataclass(frozen=True, slots=True)
class AuditionLocalSaveStateEvidence:
    schema_version: int
    run_id: str
    step_context_id: str
    step_context_digest: str
    session_transition_id: str
    zone_checkpoint_digest: str
    source_path: str
    source_sha256: str
    source_size: int
    source_type: str
    envelope_version: int
    state: LocalSaveExamState

    def __post_init__(self) -> None:
        if self.schema_version != AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION:
            raise ValueError("unsupported audition local-save state schema")
        for label in ("run_id", "step_context_id", "session_transition_id"):
            _text(getattr(self, label), label)
        _sha256(self.step_context_digest, "step_context_digest")
        _sha256(self.zone_checkpoint_digest, "zone_checkpoint_digest")
        _text(self.source_path, "source_path")
        _sha256(self.source_sha256, "source_sha256")
        _integer(self.source_size, "source_size", minimum=1)
        if self.source_type != EXAM_SAVE_DATA_SOURCE_TYPE:
            raise ValueError(f"unsupported source_type: {self.source_type}")
        _integer(self.envelope_version, "envelope_version")
        if not isinstance(self.state, LocalSaveExamState):
            raise TypeError("state must be LocalSaveExamState")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "step_context_id": self.step_context_id,
            "step_context_digest": self.step_context_digest,
            "session_transition_id": self.session_transition_id,
            "zone_checkpoint_digest": self.zone_checkpoint_digest,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "source_size": self.source_size,
            "source_type": self.source_type,
            "envelope_version": self.envelope_version,
            "state": self.state.to_dict(),
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "AuditionLocalSaveStateEvidence":
        if not isinstance(payload, Mapping):
            raise ValueError("audition local-save state evidence must be an object")
        fields = {
            "schema_version",
            "run_id",
            "step_context_id",
            "step_context_digest",
            "session_transition_id",
            "zone_checkpoint_digest",
            "source_path",
            "source_sha256",
            "source_size",
            "source_type",
            "envelope_version",
            "state",
        }
        _exact(payload, fields, "audition local-save state evidence")
        source_schema_version = _integer(
            payload["schema_version"], "schema_version"
        )
        if source_schema_version not in {
            AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION,
            _LEGACY_AUDITION_LOCAL_SAVE_STATE_SCHEMA_V4,
            _LEGACY_AUDITION_LOCAL_SAVE_STATE_SCHEMA_V3,
            _LEGACY_AUDITION_LOCAL_SAVE_STATE_SCHEMA_V2,
        }:
            raise ValueError("unsupported audition local-save state schema")
        return cls(
            schema_version=AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION,
            run_id=_text(payload["run_id"], "run_id"),
            step_context_id=_text(payload["step_context_id"], "step_context_id"),
            step_context_digest=_sha256(
                payload["step_context_digest"], "step_context_digest"
            ),
            session_transition_id=_text(
                payload["session_transition_id"], "session_transition_id"
            ),
            zone_checkpoint_digest=_sha256(
                payload["zone_checkpoint_digest"], "zone_checkpoint_digest"
            ),
            source_path=_text(payload["source_path"], "source_path"),
            source_sha256=_sha256(payload["source_sha256"], "source_sha256"),
            source_size=_integer(payload["source_size"], "source_size", minimum=1),
            source_type=_text(payload["source_type"], "source_type"),
            envelope_version=_integer(
                payload["envelope_version"], "envelope_version"
            ),
            state=LocalSaveExamState._from_dict(  # type: ignore[arg-type]
                payload["state"],
                source_schema_version=source_schema_version,
            ),
        )


def build_audition_local_save_state_evidence(
    *,
    run_id: str,
    step_context_id: str,
    step_context_digest: str,
    session_transition_id: str,
    checkpoint: AuditionZoneCheckpoint,
    source_path: Path,
    source_type: str = EXAM_SAVE_DATA_SOURCE_TYPE,
) -> AuditionLocalSaveStateEvidence:
    """Read and decode one exact source file without writing to it."""

    if not isinstance(checkpoint, AuditionZoneCheckpoint):
        raise TypeError("checkpoint must be AuditionZoneCheckpoint")
    target = Path(source_path).resolve()
    source = target.read_bytes()
    envelope = decode_local_save_bytes(source, source_type)
    try:
        payload = json.loads(envelope.plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("decrypted ExamSaveData is not valid UTF-8 JSON") from error
    if not isinstance(payload, Mapping):
        raise ValueError("decrypted ExamSaveData root must be an object")
    return AuditionLocalSaveStateEvidence(
        schema_version=AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION,
        run_id=_text(run_id, "run_id"),
        step_context_id=_text(step_context_id, "step_context_id"),
        step_context_digest=_sha256(step_context_digest, "step_context_digest"),
        session_transition_id=_text(
            session_transition_id, "session_transition_id"
        ),
        zone_checkpoint_digest=checkpoint.digest(),
        source_path=str(target),
        source_sha256=hashlib.sha256(source).hexdigest(),
        source_size=len(source),
        source_type=source_type,
        envelope_version=envelope.save_data_version,
        state=parse_local_save_exam_state(payload),
    )


def _refs(cards: Iterable[LocalSaveExamCard], *, effective: bool) -> tuple[ZoneCardRef, ...]:
    return tuple(card.effective_ref if effective else card.base_ref for card in cards)


@dataclass(frozen=True, order=True, slots=True)
class LocalSaveStateMismatch:
    field: str
    expected: str
    actual: str

    def to_dict(self) -> dict[str, str]:
        return {
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
        }


def compare_local_save_state(
    evidence: AuditionLocalSaveStateEvidence,
    *,
    session: ExamSession,
    checkpoint: AuditionZoneCheckpoint,
    rules: AuditionRules,
) -> tuple[LocalSaveStateMismatch, ...]:
    """Return every exact live/session/checkpoint difference; never repair it."""

    if not isinstance(evidence, AuditionLocalSaveStateEvidence):
        raise TypeError("evidence must be AuditionLocalSaveStateEvidence")
    if not isinstance(session, ExamSession) or session.schema_version != SESSION_SCHEMA_VERSION:
        raise ValueError("a schema-v3 ExamSession is required")
    if not isinstance(checkpoint, AuditionZoneCheckpoint):
        raise TypeError("checkpoint must be AuditionZoneCheckpoint")
    if not isinstance(rules, AuditionRules):
        raise TypeError("rules must be AuditionRules")
    state = evidence.state
    mismatches: list[LocalSaveStateMismatch] = []

    def check(field: str, expected: object, actual: object) -> None:
        if expected != actual:
            mismatches.append(
                LocalSaveStateMismatch(field, repr(expected), repr(actual))
            )

    binding = session.preflight_binding
    assert binding is not None
    check("run_id", session.run_id, evidence.run_id)
    check("step_context_id", binding.step_context_id, evidence.step_context_id)
    check(
        "step_context_digest",
        binding.step_context_digest,
        evidence.step_context_digest,
    )
    check("session_transition_id", session.transition_id, evidence.session_transition_id)
    check("zone_checkpoint_digest", checkpoint.digest(), evidence.zone_checkpoint_digest)
    check("character_id", session.character_id, state.character_id)
    check("setting_id", rules.exam_setting_id, state.setting_id)
    check("exam_type", _AUDITION_EXAM_TYPE, state.exam_type)
    check(
        "step_type_value",
        _STEP_TYPE_VALUE_BY_NAME.get(session.step_type),
        state.step_type_value,
    )
    check("phase", _MAIN_PHASE, state.phase)
    check("limit_turn", rules.turns, state.limit_turn)
    check("round_number", session.logic_state.round_number, state.current_turn)
    check("turns_remaining", session.logic_state.turns_remaining, state.remain_turn)
    check("score", session.logic_state.score, state.score)
    check("stamina", session.logic_state.stamina, state.stamina)
    check("max_stamina", session.logic_state.max_stamina, state.max_stamina)
    check("block", session.logic_state.block, state.block)
    check("extra_turn", 0, state.extra_turn)
    check("turn_card_play_count", 0, state.turn_card_play_count)
    check("exam_card_play_count", checkpoint.lineage_depth, state.exam_card_play_count)
    check("is_turn_card_play_end", False, state.is_turn_card_play_end)
    check("playing_card.empty", None, state.playing_card)
    check("removed_cards.empty", (), state.removed_cards)
    check("future_deck.empty", (), state.future_deck)
    check("past_deck.empty", (), state.past_deck)
    multiplier_by_parameter = {
        1: state.vocal_bonus_permille,
        2: state.dance_bonus_permille,
        3: state.visual_bonus_permille,
    }
    current_parameter = state.turn_parameter_types[state.current_turn - 1]
    check(
        "score_multiplier_permille",
        session.logic_state.score_multiplier_permille,
        multiplier_by_parameter[current_parameter],
    )
    check("hand.effective", checkpoint.zones.hand, _refs(state.zones.hand, effective=True))
    check("hand.base", checkpoint.hand_base_cards, _refs(state.zones.hand, effective=False))
    check("deck.base", Counter(checkpoint.zones.deck), Counter(_refs(state.zones.deck, effective=False)))
    check("grave.base", Counter(checkpoint.zones.grave), Counter(_refs(state.zones.grave, effective=False)))
    check("lost.base", Counter(checkpoint.zones.lost), Counter(_refs(state.zones.lost, effective=False)))
    check("hold.empty", (), _refs(state.zones.hold, effective=False))
    return tuple(mismatches)


def verified_turn_schedule_from_local_save(
    evidence: AuditionLocalSaveStateEvidence,
):
    """Build the exact remaining Plan2 schedule bound by ``ExamSaveData``.

    Auditions serialize one parameter type per turn.  Lessons instead bind one
    parameter type through their step identity (1..3 Vocal, 4..6 Dance,
    7..9 Visual) and therefore carry an empty ``turn_parameter_types`` list.
    The caller should still compare this file-backed projection with two fresh
    Maa captures before taking an action.
    """

    if not isinstance(evidence, AuditionLocalSaveStateEvidence):
        raise TypeError("evidence must be AuditionLocalSaveStateEvidence")
    from .audition_horizon import VerifiedTurnFrame, VerifiedTurnSchedule

    state = evidence.state
    common_invalid = (
        state.phase != _MAIN_PHASE
        or state.is_turn_card_play_end
        or state.playing_card is not None
        or not state.removed_cards_are_positioned_tombstones
        or state.future_deck
        or state.past_deck is None
        or state.past_deck
    )
    audition = (
        state.exam_type == _AUDITION_EXAM_TYPE
        and state.step_type_value in _PLAN2_NATIVE_AUDITION_STEP_TYPES
    )
    lesson = (
        state.exam_type == _LESSON_EXAM_TYPE
        and state.step_type_value in _PLAN2_NATIVE_LESSON_STEP_TYPES
    )
    if common_invalid or not (audition or lesson):
        raise ValueError(
            "ExamSaveData is not a settled supported Plan2 Main phase"
        )
    if lesson:
        if state.turn_parameter_types:
            raise ValueError("Plan2 lesson ExamSaveData must carry an empty schedule")
        parameter_value = ((state.step_type_value - 1) // 3) + 1
        lesson_type = _LESSON_TYPE_BY_PARAMETER_VALUE[parameter_value]
        frames = tuple(
            VerifiedTurnFrame(
                round_number=round_number,
                lesson_type=lesson_type,
                score_multiplier_permille=1000,
            )
            for round_number in range(
                state.current_turn,
                state.limit_turn + state.extra_turn + 1,
            )
        )
    else:
        multiplier_by_parameter = {
            1: state.vocal_bonus_permille,
            2: state.dance_bonus_permille,
            3: state.visual_bonus_permille,
        }
        frames = tuple(
            VerifiedTurnFrame(
                round_number=round_number,
                lesson_type=_LESSON_TYPE_BY_PARAMETER_VALUE[
                    state.turn_parameter_types[round_number - 1]
                ],
                score_multiplier_permille=multiplier_by_parameter[
                    state.turn_parameter_types[round_number - 1]
                ],
            )
            for round_number in range(
                state.current_turn,
                state.limit_turn + state.extra_turn + 1,
            )
        )
    return VerifiedTurnSchedule(
        source=(
            "exam-save-data-local-save-v1:"
            f"{evidence.run_id}:{evidence.source_sha256}"
        ),
        evidence_sha256=evidence.digest(),
        frames=frames,
    )


def load_audition_local_save_state(
    path: Path,
) -> AuditionLocalSaveStateEvidence | None:
    target = Path(path)
    if not target.exists():
        return None
    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("audition local-save state root must be an object")
    return AuditionLocalSaveStateEvidence.from_dict(payload)


def save_audition_local_save_state(
    evidence: AuditionLocalSaveStateEvidence, path: Path
) -> Path:
    if not isinstance(evidence, AuditionLocalSaveStateEvidence):
        raise TypeError("evidence must be AuditionLocalSaveStateEvidence")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(evidence.canonical_json() + "\n", encoding="utf-8")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


__all__ = [
    "AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION",
    "AuditionLocalSaveStateEvidence",
    "CanonicalJsonValue",
    "EXAM_SAVE_DATA_ROOT_FIELDS",
    "EXAM_SAVE_DATA_V330_ADDITIONAL_ROOT_FIELDS",
    "EXAM_SAVE_DATA_SOURCE_TYPE",
    "LOCAL_SAVE_STATE_FILENAME",
    "LocalSaveExamCard",
    "LocalSaveExamCardRuntimeState",
    "LocalSaveExamRootRuntimeState",
    "LocalSaveExamState",
    "LocalSaveExamZones",
    "LocalSaveStateMismatch",
    "build_audition_local_save_state_evidence",
    "compare_local_save_state",
    "empty_local_save_exam_card_runtime_state",
    "empty_local_save_exam_root_runtime_state",
    "load_audition_local_save_state",
    "parse_local_save_exam_state",
    "parse_raw_local_save_exam_card",
    "save_audition_local_save_state",
    "verified_turn_schedule_from_local_save",
    "audition_step_type_value",
]
