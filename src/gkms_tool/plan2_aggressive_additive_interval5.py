"""Exact standalone Plan2 runtime for p201 additive-fix and interval five.

This leaf owns the two remaining p201 families only:

* ``ProduceExamEffectType_ExamAggressiveAdditiveFix``; and
* ``e_trigger-exam_aggressive_up_interval-5-exam_card_play_aggressive``.

Master fixes the four card versions and their direct slot order.  Android
v3.2.3 fixes the status merge, signed Int32, duration, Aggressive application,
phase-53 counting, and stable listener ordering rules.  ForcePlay, Encore, and
Timer/Draw execution remain external handoff boundaries; this module records
their exact queued commands without calling a proxy or a central runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Final, Literal, Mapping

from .master_db import DEFAULT_DATABASE
from .plan3_native_state import Plan3NativeCard, Plan3NativeState


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COVERAGE_ARTIFACT = (
    PROJECT_ROOT / "var" / "coverage" / "plan2_card_executable_coverage.json"
)

CARD_ID: Final = "p_card-02-ido-3_201"
CARD_UPGRADES: Final = (0, 1, 2, 3)
CARD_STAMINA_BY_UPGRADE: Final = (5, 2, 1, 0)
CARD_COST_TYPE: Final = "ExamCostType_Unknown"
CARD_COST_VALUE: Final = 0
CARD_MOVE_POSITION: Final = "ProduceCardMovePositionType_Lost"
CARD_PLAN_TYPE: Final = "ProducePlanType_Plan2"
CARD_CATEGORY: Final = "ProduceCardCategory_MentalSkill"

AGGRESSIVE_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamCardPlayAggressive"
ADDITIVE_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamAggressiveAdditiveFix"
)
TIMER_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamEffectTimer"
DRAW_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamCardDraw"
ENCORE_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamStatusEnchantEncore"
FORCE_PLAY_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamForcePlayCardSearch"

AGGRESSIVE_EFFECT_ID: Final = "e_effect-exam_card_play_aggressive-0003"
ADDITIVE_EFFECT_ID: Final = "e_effect-exam_aggressive_additive_fix-0001-02"
TIMER_EFFECT_ID: Final = (
    "e_effect-exam_effect_timer-0001-01-e_effect-exam_card_draw-0001"
)
DRAW_EFFECT_ID: Final = "e_effect-exam_card_draw-0001"
ENCORE_EFFECT_ID: Final = (
    "e_effect-exam_status_enchant_encore-0001-02-inf-"
    "enchant-p_card-02-ido-3_201-enc01"
)
FORCE_PLAY_EFFECT_ID: Final = (
    "e_effect-exam_force_play_card_search-p_card_search-target_is_self-"
    "exam_status_enchant_encore-all-1_1"
)
STATUS_ENCHANT_ID: Final = "enchant-p_card-02-ido-3_201-enc01"
TRIGGER_ID: Final = (
    "e_trigger-exam_aggressive_up_interval-5-exam_card_play_aggressive"
)
PHASE_TYPE: Final = "ProduceExamPhaseType_ExamAggressiveUpInterval"
PHASE_VALUE: Final = 53
INTERVAL: Final = 5

AGGRESSIVE_GROUP = ("effect_group-visible-exam_card_play_aggressive-000",)
TIMER_GROUP = (
    "effect_group-visible-exam_effect_timer-000",
    "effect_group-visible-exam_card_draw-000",
)
ENCORE_GROUP = ("effect_group-visible-exam_status_enchant_encore-000",)
CARD_EFFECT_GROUPS = (
    "effect_group-visible-exam_effect_timer-000",
    "effect_group-visible-exam_card_draw-000",
    "effect_group-visible-exam_status_enchant_encore-000",
    "effect_group-visible-exam_card_play_aggressive-000",
)

INT32_MIN: Final = -(2**31)
INT32_MAX: Final = 2**31 - 1
UINT32_MASK: Final = 2**32 - 1

PlayOrigin = Literal["normal", "forced", "extra"]
CommandKind = Literal["force-play", "aggressive-child"]


class Plan2AggressiveAdditiveIntervalError(ValueError):
    """Stable fail-closed error for this exact standalone leaf."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class Plan2AggressiveAdditiveIntervalOverflow(
    Plan2AggressiveAdditiveIntervalError
):
    """Native checked ``Enumerable.Sum<int>`` would overflow."""


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise Plan2AggressiveAdditiveIntervalError("invalid-text", label)
    return value


def _int32(value: object, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not INT32_MIN <= value <= INT32_MAX
    ):
        raise Plan2AggressiveAdditiveIntervalError("invalid-int32", label)
    return value


def _nonnegative(value: object, label: str) -> int:
    parsed = _int32(value, label)
    if parsed < 0:
        raise Plan2AggressiveAdditiveIntervalError("negative-value", label)
    return parsed


def _positive(value: object, label: str) -> int:
    parsed = _int32(value, label)
    if parsed < 1:
        raise Plan2AggressiveAdditiveIntervalError("nonpositive-value", label)
    return parsed


def _i32_wrap(value: int) -> int:
    unsigned = value & UINT32_MASK
    return unsigned if unsigned <= INT32_MAX else unsigned - (UINT32_MASK + 1)


def native_add_and_clamp_zero(current: int, delta: int) -> int:
    """ARM ``add w`` followed by ``bic w,w,w,asr#31``."""

    current = _int32(current, "current")
    delta = _int32(delta, "delta")
    wrapped = _i32_wrap(current + delta)
    return 0 if wrapped < 0 else wrapped


def native_checked_sum(values: tuple[int, ...]) -> int:
    """Android's checked ``Enumerable.Sum<int>`` boundary."""

    total = 0
    for index, value in enumerate(values):
        value = _int32(value, f"sum[{index}]")
        candidate = total + value
        if not INT32_MIN <= candidate <= INT32_MAX:
            raise Plan2AggressiveAdditiveIntervalOverflow(
                "checked-additive-sum-overflow", f"index={index}"
            )
        total = candidate
    return total


@dataclass(frozen=True, slots=True)
class ExactEffect:
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str = ""
    chain_effect_id: str = ""
    search_id: str = ""
    pick_range_type: str = "ProducePickRangeType_Unknown"
    pick_count_min: int = 0
    pick_count_max: int = 0
    effect_group_ids: tuple[str, ...] = ()


EFFECTS: Mapping[str, ExactEffect] = MappingProxyType(
    {
        row.effect_id: row
        for row in (
            ExactEffect(
                AGGRESSIVE_EFFECT_ID,
                AGGRESSIVE_EFFECT_TYPE,
                3,
                0,
                0,
                0,
                effect_group_ids=AGGRESSIVE_GROUP,
            ),
            ExactEffect(
                ADDITIVE_EFFECT_ID,
                ADDITIVE_EFFECT_TYPE,
                1,
                0,
                0,
                2,
                effect_group_ids=AGGRESSIVE_GROUP,
            ),
            ExactEffect(
                TIMER_EFFECT_ID,
                TIMER_EFFECT_TYPE,
                1,
                0,
                1,
                0,
                chain_effect_id=DRAW_EFFECT_ID,
                effect_group_ids=TIMER_GROUP,
            ),
            ExactEffect(
                DRAW_EFFECT_ID,
                DRAW_EFFECT_TYPE,
                1,
                0,
                0,
                0,
                effect_group_ids=("effect_group-visible-exam_card_draw-000",),
            ),
            ExactEffect(
                ENCORE_EFFECT_ID,
                ENCORE_EFFECT_TYPE,
                1,
                0,
                2,
                -1,
                status_enchant_id=STATUS_ENCHANT_ID,
                effect_group_ids=ENCORE_GROUP,
            ),
            ExactEffect(
                FORCE_PLAY_EFFECT_ID,
                FORCE_PLAY_EFFECT_TYPE,
                0,
                0,
                0,
                0,
                search_id="p_card_search-target_is_self-exam_status_enchant_encore",
                pick_range_type="ProducePickRangeType_All",
                pick_count_min=1,
                pick_count_max=1,
            ),
        )
    }
)


@dataclass(frozen=True, slots=True)
class ExactCardSlot:
    slot: int
    effect_id: str
    trigger_id: str = ""
    hide_icon: bool = False
    is_once: bool = False


CARD_SLOTS = (
    ExactCardSlot(0, AGGRESSIVE_EFFECT_ID),
    ExactCardSlot(1, ADDITIVE_EFFECT_ID),
    ExactCardSlot(2, TIMER_EFFECT_ID),
    ExactCardSlot(3, ENCORE_EFFECT_ID, is_once=True),
)


@dataclass(frozen=True, slots=True)
class ExactCardVersion:
    card_id: str
    upgrade: int
    stamina: int
    cost_type: str
    cost_value: int
    move_position: str
    effect_group_ids: tuple[str, ...]
    slots: tuple[ExactCardSlot, ...]

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return tuple(slot.effect_id for slot in self.slots)


VERSIONS = tuple(
    ExactCardVersion(
        CARD_ID,
        upgrade,
        stamina,
        CARD_COST_TYPE,
        CARD_COST_VALUE,
        CARD_MOVE_POSITION,
        CARD_EFFECT_GROUPS,
        CARD_SLOTS,
    )
    for upgrade, stamina in enumerate(CARD_STAMINA_BY_UPGRADE)
)


@dataclass(frozen=True, slots=True)
class ExactIntervalTrigger:
    trigger_id: str = TRIGGER_ID
    phase_types: tuple[str, ...] = (PHASE_TYPE,)
    phase_values: tuple[int, ...] = (INTERVAL,)
    effect_types: tuple[str, ...] = (AGGRESSIVE_EFFECT_TYPE,)
    card_search_id: str = ""
    upper_search_count: int = 0
    lower_search_count: int = 0
    move_position: str = "ProduceCardMovePositionType_Unknown"
    lesson_type: str = "ProduceStepLessonType_Unknown"


@dataclass(frozen=True, slots=True)
class Plan2AggressiveAdditiveIntervalCatalog:
    versions: tuple[ExactCardVersion, ...]
    effects: Mapping[str, ExactEffect]
    trigger: ExactIntervalTrigger
    status_enchant_id: str
    status_child_effect_ids: tuple[str, ...]

    def version(self, upgrade: int) -> ExactCardVersion:
        try:
            return next(row for row in self.versions if row.upgrade == upgrade)
        except StopIteration as error:
            raise Plan2AggressiveAdditiveIntervalError(
                "card-version-outside-exact-bundle", str(upgrade)
            ) from error


_EFFECT_RAW_KEYS = frozenset(
    {
        "id",
        "effectType",
        "effectValue1",
        "effectValue2",
        "effectCount",
        "effectTurn",
        "targetProduceCardId",
        "targetUpgradeCount",
        "targetExamEffectType",
        "produceCardSearchId",
        "movePositionType",
        "pickRangeType",
        "pickCountReferenceProduceCardSearchId",
        "pickCountType",
        "pickCountMin",
        "pickCountMax",
        "produceCardSearchId2",
        "pickRangeType2",
        "pickCountReferenceProduceCardSearchId2",
        "pickCountType2",
        "pickCountMin2",
        "pickCountMax2",
        "chainProduceExamEffectId",
        "chainProduceExamEffectIds",
        "produceExamStatusEnchantId",
        "produceCardStatusEnchantId",
        "produceCardGrowEffectIds",
        "effectGroupIds",
        "produceDescriptions",
        "customizeProduceDescriptions",
    }
)

_EFFECT_NEUTRAL = {
    "targetProduceCardId": "",
    "targetUpgradeCount": 0,
    "targetExamEffectType": "ProduceExamEffectType_Unknown",
    "produceCardSearchId": "",
    "movePositionType": "ProduceCardMovePositionType_Unknown",
    "pickRangeType": "ProducePickRangeType_Unknown",
    "pickCountReferenceProduceCardSearchId": "",
    "pickCountType": "ProducePickCountType_Unknown",
    "pickCountMin": 0,
    "pickCountMax": 0,
    "produceCardSearchId2": "",
    "pickRangeType2": "ProducePickRangeType_Unknown",
    "pickCountReferenceProduceCardSearchId2": "",
    "pickCountType2": "ProducePickCountType_Unknown",
    "pickCountMin2": 0,
    "pickCountMax2": 0,
    "chainProduceExamEffectId": "",
    "chainProduceExamEffectIds": [],
    "produceExamStatusEnchantId": "",
    "produceCardStatusEnchantId": "",
    "produceCardGrowEffectIds": [],
}

_TRIGGER_RAW_KEYS = frozenset(
    {
        "id",
        "phaseTypes",
        "phaseValues",
        "fieldStatusCheckTypes",
        "fieldStatusTypes",
        "fieldStatusValues",
        "fieldStatusProduceCardSearchIds",
        "produceCardSearchId",
        "upperSearchCount",
        "lowerSearchCount",
        "cardMovePositionType",
        "effectTypes",
        "lessonType",
        "produceDescriptions",
        "playProduceDescriptions",
        "playEffectProduceDescriptions",
    }
)

_CARD_RAW_KEYS = frozenset(
    {
        "id", "upgradeCount", "name", "assetId", "isCharacterAsset",
        "voiceAssetId", "rarity", "planType", "category", "stamina",
        "forceStamina", "costType", "costValue", "playProduceExamTriggerId",
        "playEffects", "playMovePositionType", "moveEffectTriggerType",
        "moveProduceExamEffectIds", "isEndTurnLost", "isInitial", "isRestrict",
        "produceCardStatusEnchantId", "searchTag", "libraryHidden",
        "noDeckDuplication", "isReward", "produceDescriptions", "effectGroupIds",
        "evaluation", "isConversion", "isInitialDeckProduceCard", "isLimited",
        "maxCustomizeCount", "moveProduceExamTriggerIds", "order",
        "originCharacterId", "originIdolCardId", "originPrimaStellaIdolCardId",
        "originSupportCardId", "produceCardCustomizeIds", "rentalUnlockProducerLevel",
        "unlockProducerLevel", "viewStartTime",
    }
)


def _json_object(value: object, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2AggressiveAdditiveIntervalError(
            "invalid-json-object", label
        ) from error
    if not isinstance(parsed, dict):
        raise Plan2AggressiveAdditiveIntervalError("invalid-json-object", label)
    return parsed


def _json_array(value: object, label: str) -> list[object]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2AggressiveAdditiveIntervalError(
            "invalid-json-array", label
        ) from error
    if not isinstance(parsed, list):
        raise Plan2AggressiveAdditiveIntervalError("invalid-json-array", label)
    return parsed


def _strict_payload(
    payload: Mapping[str, object],
    expected: Mapping[str, object],
    allowed_keys: frozenset[str],
    label: str,
) -> None:
    unknown = set(payload) - allowed_keys
    missing = set(expected) - set(payload)
    if unknown:
        raise Plan2AggressiveAdditiveIntervalError(
            "unknown-master-shape", f"{label}:{sorted(unknown)}"
        )
    if missing:
        raise Plan2AggressiveAdditiveIntervalError(
            "missing-master-shape", f"{label}:{sorted(missing)}"
        )
    for key, wanted in expected.items():
        actual = payload[key]
        if type(actual) is not type(wanted) or actual != wanted:
            raise Plan2AggressiveAdditiveIntervalError(
                "altered-master-shape",
                f"{label}.{key}={actual!r}:expected={wanted!r}",
            )


def _effect_expected(effect: ExactEffect) -> dict[str, object]:
    expected = dict(_EFFECT_NEUTRAL)
    expected.update(
        {
            "id": effect.effect_id,
            "effectType": effect.effect_type,
            "effectValue1": effect.value1,
            "effectValue2": effect.value2,
            "effectCount": effect.effect_count,
            "effectTurn": effect.effect_turn,
            "effectGroupIds": list(effect.effect_group_ids),
        }
    )
    if effect.status_enchant_id:
        expected["produceExamStatusEnchantId"] = effect.status_enchant_id
    if effect.chain_effect_id:
        expected["chainProduceExamEffectId"] = effect.chain_effect_id
    if effect.search_id:
        expected["produceCardSearchId"] = effect.search_id
        expected["pickRangeType"] = effect.pick_range_type
        expected["pickCountMin"] = effect.pick_count_min
        expected["pickCountMax"] = effect.pick_count_max
    return expected


def _load_effect(
    connection: sqlite3.Connection, expected: ExactEffect
) -> None:
    row = connection.execute(
        "SELECT * FROM effect WHERE id = ?", (expected.effect_id,)
    ).fetchone()
    if row is None:
        raise Plan2AggressiveAdditiveIntervalError(
            "missing-effect-row", expected.effect_id
        )
    projection = (
        row["effect_type"], row["value1"], row["value2"],
        row["effect_count"], row["effect_turn"], row["status_enchant_id"],
        row["chain_effect_id"],
    )
    wanted = (
        expected.effect_type, expected.value1, expected.value2,
        expected.effect_count, expected.effect_turn, expected.status_enchant_id,
        expected.chain_effect_id,
    )
    if projection != wanted:
        raise Plan2AggressiveAdditiveIntervalError(
            "altered-effect-row", expected.effect_id
        )
    payload = _json_object(row["raw_json"], expected.effect_id)
    _strict_payload(
        payload, _effect_expected(expected), _EFFECT_RAW_KEYS, expected.effect_id
    )


def load_plan2_aggressive_additive_interval5_catalog(
    database: Path | str = DEFAULT_DATABASE,
) -> Plan2AggressiveAdditiveIntervalCatalog:
    """Load and validate all four p201 versions and both exact families."""

    try:
        with sqlite3.connect(Path(database)) as connection:
            connection.row_factory = sqlite3.Row
            for effect in EFFECTS.values():
                _load_effect(connection, effect)

            trigger = connection.execute(
                "SELECT * FROM produce_exam_trigger WHERE id = ?", (TRIGGER_ID,)
            ).fetchone()
            if trigger is None:
                raise Plan2AggressiveAdditiveIntervalError(
                    "missing-trigger-row", TRIGGER_ID
                )
            trigger_expected = {
                "id": TRIGGER_ID,
                "phaseTypes": [PHASE_TYPE],
                "phaseValues": [INTERVAL],
                "fieldStatusCheckTypes": [],
                "fieldStatusTypes": [],
                "fieldStatusValues": [],
                "fieldStatusProduceCardSearchIds": [],
                "produceCardSearchId": "",
                "upperSearchCount": 0,
                "lowerSearchCount": 0,
                "cardMovePositionType": "ProduceCardMovePositionType_Unknown",
                "effectTypes": [AGGRESSIVE_EFFECT_TYPE],
                "lessonType": "ProduceStepLessonType_Unknown",
            }
            _strict_payload(
                _json_object(trigger["raw_json"], TRIGGER_ID),
                trigger_expected,
                _TRIGGER_RAW_KEYS,
                TRIGGER_ID,
            )

            status = connection.execute(
                "SELECT * FROM produce_exam_status_enchant WHERE id = ?",
                (STATUS_ENCHANT_ID,),
            ).fetchone()
            if status is None:
                raise Plan2AggressiveAdditiveIntervalError(
                    "missing-status-enchant-row", STATUS_ENCHANT_ID
                )
            children = tuple(
                _text(value, "status child")
                for value in _json_array(
                    status["produce_exam_effect_ids_json"], STATUS_ENCHANT_ID
                )
            )
            if (
                status["produce_exam_trigger_id"] != TRIGGER_ID
                or children != (FORCE_PLAY_EFFECT_ID,)
            ):
                raise Plan2AggressiveAdditiveIntervalError(
                    "altered-status-enchant-row", STATUS_ENCHANT_ID
                )
            _strict_payload(
                _json_object(status["raw_json"], STATUS_ENCHANT_ID),
                {
                    "id": STATUS_ENCHANT_ID,
                    "assetId": "",
                    "produceExamTriggerId": TRIGGER_ID,
                    "produceExamEffectIds": [FORCE_PLAY_EFFECT_ID],
                },
                frozenset(
                    {
                        "id", "assetId", "produceExamTriggerId",
                        "produceExamEffectIds", "produceDescriptions",
                    }
                ),
                STATUS_ENCHANT_ID,
            )

            for expected in VERSIONS:
                card = connection.execute(
                    "SELECT * FROM card WHERE id = ? AND upgrade_count = ?",
                    (CARD_ID, expected.upgrade),
                ).fetchone()
                if card is None:
                    raise Plan2AggressiveAdditiveIntervalError(
                        "missing-card-version", f"{CARD_ID}+{expected.upgrade}"
                    )
                slots = _json_array(card["play_effects_json"], "card.playEffects")
                expected_slots = [
                    {
                        "produceExamTriggerId": slot.trigger_id,
                        "produceExamEffectId": slot.effect_id,
                        "hideIcon": slot.hide_icon,
                        "isOncePlayEffect": slot.is_once,
                    }
                    for slot in CARD_SLOTS
                ]
                projection = (
                    card["plan_type"], card["category"], card["stamina"],
                    card["cost_type"], card["cost_value"],
                    card["play_trigger_id"], card["move_position_type"], slots,
                )
                wanted = (
                    CARD_PLAN_TYPE, CARD_CATEGORY, expected.stamina,
                    CARD_COST_TYPE, CARD_COST_VALUE, "", CARD_MOVE_POSITION,
                    expected_slots,
                )
                if projection != wanted:
                    raise Plan2AggressiveAdditiveIntervalError(
                        "altered-card-version", f"{CARD_ID}+{expected.upgrade}"
                    )
                raw = _json_object(card["raw_json"], f"{CARD_ID}+{expected.upgrade}")
                _strict_payload(
                    raw,
                    {
                        "id": CARD_ID,
                        "upgradeCount": expected.upgrade,
                        "planType": CARD_PLAN_TYPE,
                        "category": CARD_CATEGORY,
                        "stamina": expected.stamina,
                        "forceStamina": 0,
                        "costType": CARD_COST_TYPE,
                        "costValue": CARD_COST_VALUE,
                        "playProduceExamTriggerId": "",
                        "playEffects": expected_slots,
                        "playMovePositionType": CARD_MOVE_POSITION,
                        "moveEffectTriggerType": "ProduceCardMoveEffectTriggerType_Unknown",
                        "moveProduceExamEffectIds": [],
                        "isEndTurnLost": False,
                        "isInitial": False,
                        "isRestrict": False,
                        "produceCardStatusEnchantId": "",
                        "effectGroupIds": list(CARD_EFFECT_GROUPS),
                    },
                    _CARD_RAW_KEYS,
                    f"{CARD_ID}+{expected.upgrade}",
                )
    except sqlite3.Error as error:
        raise Plan2AggressiveAdditiveIntervalError(
            "master-database-error", str(error)
        ) from error
    return Plan2AggressiveAdditiveIntervalCatalog(
        VERSIONS, EFFECTS, ExactIntervalTrigger(), STATUS_ENCHANT_ID,
        (FORCE_PLAY_EFFECT_ID,),
    )


@dataclass(frozen=True, slots=True)
class AggressiveAdditiveFixLayer:
    uid: int
    value: int
    turn: int
    install_sequence: int
    source_effect_id: str = ADDITIVE_EFFECT_ID
    passing_turn_start: bool = True

    def __post_init__(self) -> None:
        _positive(self.uid, "additive.uid")
        _int32(self.value, "additive.value")
        _int32(self.turn, "additive.turn")
        _positive(self.install_sequence, "additive.install_sequence")
        if type(self.passing_turn_start) is not bool:
            raise Plan2AggressiveAdditiveIntervalError("invalid-passing-turn-start")
        if self.source_effect_id != ADDITIVE_EFFECT_ID:
            raise Plan2AggressiveAdditiveIntervalError(
                "unknown-additive-source-effect", self.source_effect_id
            )

    @property
    def is_turn_limited(self) -> bool:
        return self.turn >= 0


@dataclass(frozen=True, slots=True)
class Interval5Listener:
    uid: int
    source_id: str
    captured_guid: str
    child_effect_ids: tuple[str, ...]
    install_sequence: int
    phase_count: int = 0
    max_uses: int | None = None
    max_uses_per_turn: int | None = None
    uses: int = 0
    uses_this_turn: int = 0
    is_encore: bool = False
    trigger_id: str = TRIGGER_ID
    phase_type: str = PHASE_TYPE
    interval: int = INTERVAL
    effect_types: tuple[str, ...] = (AGGRESSIVE_EFFECT_TYPE,)

    def __post_init__(self) -> None:
        _positive(self.uid, "listener.uid")
        _text(self.source_id, "listener.source_id")
        _text(self.captured_guid, "listener.captured_guid")
        _positive(self.install_sequence, "listener.install_sequence")
        _int32(self.phase_count, "listener.phase_count")
        if (
            self.trigger_id != TRIGGER_ID
            or self.phase_type != PHASE_TYPE
            or self.interval != INTERVAL
            or self.effect_types != (AGGRESSIVE_EFFECT_TYPE,)
        ):
            raise Plan2AggressiveAdditiveIntervalError(
                "altered-interval-listener-shape", str(self.uid)
            )
        if not self.child_effect_ids or any(
            effect_id not in {FORCE_PLAY_EFFECT_ID, AGGRESSIVE_EFFECT_ID}
            for effect_id in self.child_effect_ids
        ):
            raise Plan2AggressiveAdditiveIntervalError(
                "unsupported-interval-child-shape", str(self.uid)
            )
        for value, label in (
            (self.max_uses, "max_uses"),
            (self.max_uses_per_turn, "max_uses_per_turn"),
        ):
            if value is not None:
                _positive(value, label)
        _nonnegative(self.uses, "uses")
        _nonnegative(self.uses_this_turn, "uses_this_turn")
        if self.max_uses is not None and self.uses > self.max_uses:
            raise Plan2AggressiveAdditiveIntervalError("listener-use-overflow")
        if (
            self.max_uses_per_turn is not None
            and self.uses_this_turn > self.max_uses_per_turn
        ):
            raise Plan2AggressiveAdditiveIntervalError("listener-turn-use-overflow")

    @property
    def can_fire(self) -> bool:
        return (
            (self.max_uses is None or self.uses < self.max_uses)
            and (
                self.max_uses_per_turn is None
                or self.uses_this_turn < self.max_uses_per_turn
            )
        )


@dataclass(frozen=True, slots=True)
class QueuedIntervalEffect:
    command_id: str
    event_sequence: int
    listener_uid: int
    listener_install_sequence: int
    child_index: int
    effect_id: str
    effect_type: str
    cause_origin: PlayOrigin
    cause_transaction_id: str
    captured_guid: str

    @property
    def kind(self) -> CommandKind:
        return (
            "force-play"
            if self.effect_type == FORCE_PLAY_EFFECT_TYPE
            else "aggressive-child"
        )


@dataclass(frozen=True, slots=True)
class TimerDrawCommand:
    command_id: str
    source_transaction_id: str
    source_guid: str
    remaining_turns: int = 1
    remaining_count: int = 1
    child_effect_id: str = DRAW_EFFECT_ID
    draw_count: int = 1


@dataclass(frozen=True, slots=True)
class P201PlayHistory:
    transaction_id: str
    guid: str
    upgrade: int
    play_origin: PlayOrigin
    source_zone: str
    destination_zone: str
    stamina_before: int
    stamina_after: int
    cost_paid: int
    card_count_before: int
    card_count_after: int
    global_count_before: int
    global_count_after: int
    turn_count_before: int
    turn_count_after: int
    aggressive_before: int
    aggressive_after_slot0: int
    additive_before_slot0: int
    additive_after_slot1: int
    once_outcome: Literal["installed", "skipped-once"]
    random_state_before: int
    random_state_after: int
    effect_order: tuple[str, ...] = (
        AGGRESSIVE_EFFECT_ID, ADDITIVE_EFFECT_ID, TIMER_EFFECT_ID, ENCORE_EFFECT_ID
    )
    history_event_count: int = 1
    final_move_count: int = 1


@dataclass(frozen=True, slots=True)
class Plan2AggressiveAdditiveIntervalRuntime:
    catalog: Plan2AggressiveAdditiveIntervalCatalog
    native_state: Plan3NativeState
    stamina: int
    aggressive: int = 0
    additive_layers: tuple[AggressiveAdditiveFixLayer, ...] = ()
    listeners: tuple[Interval5Listener, ...] = ()
    pending_interval_effects: tuple[QueuedIntervalEffect, ...] = ()
    pending_timers: tuple[TimerDrawCommand, ...] = ()
    ready_timer_effects: tuple[TimerDrawCommand, ...] = ()
    once_consumed: tuple[tuple[str, str], ...] = ()
    committed_transactions: tuple[str, ...] = ()
    global_play_count: int = 0
    turn_play_count: int = 0
    event_sequence: int = 0
    history: tuple[P201PlayHistory, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.catalog, Plan2AggressiveAdditiveIntervalCatalog):
            raise Plan2AggressiveAdditiveIntervalError("invalid-catalog")
        if not isinstance(self.native_state, Plan3NativeState):
            raise Plan2AggressiveAdditiveIntervalError("invalid-native-state")
        _nonnegative(self.stamina, "stamina")
        _nonnegative(self.aggressive, "aggressive")
        for values, label in (
            (tuple(layer.uid for layer in self.additive_layers), "additive-uids"),
            (tuple(listener.uid for listener in self.listeners), "listener-uids"),
            (
                tuple(command.command_id for command in self.pending_interval_effects),
                "pending-command-ids",
            ),
            (tuple(timer.command_id for timer in self.pending_timers), "timer-ids"),
            (self.committed_transactions, "transactions"),
            (self.once_consumed, "once-keys"),
        ):
            if len(values) != len(set(values)):
                raise Plan2AggressiveAdditiveIntervalError("duplicate-runtime-key", label)
        layer_sequences = tuple(layer.install_sequence for layer in self.additive_layers)
        listener_sequences = tuple(listener.install_sequence for listener in self.listeners)
        if (
            len(set(layer_sequences)) != len(layer_sequences)
            or layer_sequences != tuple(sorted(layer_sequences))
        ):
            raise Plan2AggressiveAdditiveIntervalError("additive-order-invalid")
        if (
            len(set(listener_sequences)) != len(listener_sequences)
            or listener_sequences != tuple(sorted(listener_sequences))
        ):
            raise Plan2AggressiveAdditiveIntervalError("listener-order-invalid")
        _nonnegative(self.global_play_count, "global_play_count")
        _nonnegative(self.turn_play_count, "turn_play_count")
        _nonnegative(self.event_sequence, "event_sequence")

    @property
    def additive_fix(self) -> int:
        return native_checked_sum(tuple(layer.value for layer in self.additive_layers))


def new_plan2_aggressive_additive_interval5_runtime(
    native_state: Plan3NativeState,
    *,
    stamina: int,
    aggressive: int = 0,
    catalog: Plan2AggressiveAdditiveIntervalCatalog | None = None,
) -> Plan2AggressiveAdditiveIntervalRuntime:
    return Plan2AggressiveAdditiveIntervalRuntime(
        catalog or load_plan2_aggressive_additive_interval5_catalog(),
        native_state,
        stamina,
        aggressive,
    )


@dataclass(frozen=True, slots=True)
class AdditiveInstallResult:
    before: Plan2AggressiveAdditiveIntervalRuntime
    after: Plan2AggressiveAdditiveIntervalRuntime
    resolved: bool
    committed: bool
    merged: bool
    blocked: bool
    reason: str
    layer: AggressiveAdditiveFixLayer | None
    total_before: int | None
    total_after: int | None


def install_aggressive_additive_fix(
    runtime: Plan2AggressiveAdditiveIntervalRuntime,
    *,
    value: int = 1,
    turn: int = 2,
    uid: int | None = None,
    blocked: bool = False,
    effect_id: str = ADDITIVE_EFFECT_ID,
) -> AdditiveInstallResult:
    """Native TryAdd: merge the last same-turn layer, otherwise append."""

    if not isinstance(runtime, Plan2AggressiveAdditiveIntervalRuntime):
        raise Plan2AggressiveAdditiveIntervalError("invalid-runtime")
    try:
        value = _int32(value, "additive.value")
        turn = _int32(turn, "additive.turn")
        if effect_id != ADDITIVE_EFFECT_ID:
            raise Plan2AggressiveAdditiveIntervalError(
                "unknown-additive-effect", effect_id
            )
        total_before = runtime.additive_fix
    except Plan2AggressiveAdditiveIntervalError as error:
        return AdditiveInstallResult(
            runtime, runtime, False, False, False, False, error.code,
            None, None, None,
        )
    if blocked:
        return AdditiveInstallResult(
            runtime, runtime, True, False, False, True,
            "effect-type-blocked", None, total_before, total_before,
        )
    matching = [
        index for index, layer in enumerate(runtime.additive_layers)
        if layer.turn == turn
    ]
    if matching:
        index = matching[-1]
        old = runtime.additive_layers[index]
        merged = replace(old, value=native_add_and_clamp_zero(old.value, value))
        layers = list(runtime.additive_layers)
        layers[index] = merged
        after = replace(runtime, additive_layers=tuple(layers))
        try:
            total_after = after.additive_fix
        except Plan2AggressiveAdditiveIntervalError as error:
            return AdditiveInstallResult(
                runtime, runtime, False, False, False, False, error.code,
                None, total_before, None,
            )
        return AdditiveInstallResult(
            runtime, after, True, True, True, False, "", merged,
            total_before, total_after,
        )
    next_uid = uid if uid is not None else 1 + max(
        (layer.uid for layer in runtime.additive_layers), default=0
    )
    try:
        next_uid = _positive(next_uid, "additive.uid")
    except Plan2AggressiveAdditiveIntervalError as error:
        return AdditiveInstallResult(
            runtime, runtime, False, False, False, False, error.code,
            None, total_before, None,
        )
    if next_uid in {layer.uid for layer in runtime.additive_layers}:
        return AdditiveInstallResult(
            runtime, runtime, False, False, False, False,
            "additive-uid-collision", None, total_before, None,
        )
    sequence = 1 + max(
        (layer.install_sequence for layer in runtime.additive_layers), default=0
    )
    layer = AggressiveAdditiveFixLayer(next_uid, value, turn, sequence, passing_turn_start=False)
    after = replace(runtime, additive_layers=(*runtime.additive_layers, layer))
    try:
        total_after = after.additive_fix
    except Plan2AggressiveAdditiveIntervalError as error:
        return AdditiveInstallResult(
            runtime, runtime, False, False, False, False, error.code,
            None, total_before, None,
        )
    return AdditiveInstallResult(
        runtime, after, True, True, False, False, "", layer,
        total_before, total_after,
    )


def install_interval5_listener(
    runtime: Plan2AggressiveAdditiveIntervalRuntime,
    *,
    uid: int,
    source_id: str,
    captured_guid: str,
    child_effect_ids: tuple[str, ...],
    max_uses: int | None = None,
    max_uses_per_turn: int | None = None,
    is_encore: bool = False,
) -> Plan2AggressiveAdditiveIntervalRuntime:
    uid = _positive(uid, "listener.uid")
    if uid in {listener.uid for listener in runtime.listeners}:
        raise Plan2AggressiveAdditiveIntervalError("listener-uid-collision")
    sequence = 1 + max(
        (listener.install_sequence for listener in runtime.listeners), default=0
    )
    listener = Interval5Listener(
        uid, source_id, captured_guid, tuple(child_effect_ids), sequence,
        max_uses=max_uses, max_uses_per_turn=max_uses_per_turn,
        is_encore=is_encore,
    )
    return replace(runtime, listeners=(*runtime.listeners, listener))


@dataclass(frozen=True, slots=True)
class AggressiveEvent:
    base_value: int
    play_origin: PlayOrigin
    transaction_id: str
    effect_id: str = AGGRESSIVE_EFFECT_ID
    effect_type: str = AGGRESSIVE_EFFECT_TYPE
    blocked: bool = False
    enchant_trigger_active: bool = True
    is_effect_value_fixed: bool = False


@dataclass(frozen=True, slots=True)
class IntervalFire:
    listener_uid: int
    install_sequence: int
    phase_count: int
    queued_command_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AggressiveEventResult:
    before: Plan2AggressiveAdditiveIntervalRuntime
    after: Plan2AggressiveAdditiveIntervalRuntime
    event: AggressiveEvent
    resolved: bool
    succeeded: bool
    reason: str
    additive_value: int | None
    applied_value: int | None
    aggressive_before: int
    aggressive_after: int
    phase_incremented: bool
    fires: tuple[IntervalFire, ...]
    queued: tuple[QueuedIntervalEffect, ...]
    operations: tuple[str, ...]


def apply_aggressive_event(
    runtime: Plan2AggressiveAdditiveIntervalRuntime,
    event: AggressiveEvent,
) -> AggressiveEventResult:
    """Apply one successful Aggressive occurrence and count phase 53 once."""

    if not isinstance(runtime, Plan2AggressiveAdditiveIntervalRuntime):
        raise Plan2AggressiveAdditiveIntervalError("invalid-runtime")
    if not isinstance(event, AggressiveEvent):
        raise Plan2AggressiveAdditiveIntervalError("invalid-aggressive-event")
    before_value = runtime.aggressive
    try:
        base = _int32(event.base_value, "aggressive.base_value")
        _text(event.transaction_id, "aggressive.transaction_id")
        if event.play_origin not in ("normal", "forced", "extra"):
            raise Plan2AggressiveAdditiveIntervalError("unknown-play-origin")
        if event.effect_id != AGGRESSIVE_EFFECT_ID or event.effect_type != AGGRESSIVE_EFFECT_TYPE:
            raise Plan2AggressiveAdditiveIntervalError("altered-aggressive-effect-shape")
        additive = 0 if event.is_effect_value_fixed else runtime.additive_fix
    except Plan2AggressiveAdditiveIntervalError as error:
        return AggressiveEventResult(
            runtime, runtime, event, False, False, error.code, None, None,
            before_value, before_value, False, (), (), (),
        )
    if event.blocked:
        return AggressiveEventResult(
            runtime, runtime, event, True, False, "effect-type-blocked",
            additive, None, before_value, before_value, False, (), (),
            ("check-effect-type-block",),
        )

    # Native performs a 32-bit add before float32 multiplier/ceil.  The exact
    # p201 family has multiplier 1, so the signed wrapped sum is the amount.
    applied = _i32_wrap(base + additive)
    aggressive_after = native_add_and_clamp_zero(before_value, applied)
    sequence = runtime.event_sequence + 1
    listeners = runtime.listeners
    phase_incremented = event.enchant_trigger_active
    if phase_incremented:
        listeners = tuple(
            replace(listener, phase_count=_i32_wrap(listener.phase_count + 1))
            for listener in listeners
        )

    queued: list[QueuedIntervalEffect] = []
    fires: list[IntervalFire] = []
    spent: list[Interval5Listener] = []
    for listener in listeners:
        matching = (
            phase_incremented
            and listener.phase_count >= 1
            and listener.phase_count % INTERVAL == 0
            and listener.can_fire
        )
        if not matching:
            spent.append(listener)
            continue
        updated = replace(
            listener,
            uses=listener.uses + 1,
            uses_this_turn=listener.uses_this_turn + 1,
        )
        spent.append(updated)
        ids: list[str] = []
        for child_index, effect_id in enumerate(listener.child_effect_ids):
            effect_type = EFFECTS[effect_id].effect_type
            command_id = (
                f"phase53:{sequence}:listener:{listener.uid}:child:{child_index}"
            )
            queued.append(
                QueuedIntervalEffect(
                    command_id, sequence, listener.uid,
                    listener.install_sequence, child_index, effect_id,
                    effect_type, event.play_origin, event.transaction_id,
                    listener.captured_guid,
                )
            )
            ids.append(command_id)
        fires.append(
            IntervalFire(
                listener.uid, listener.install_sequence,
                listener.phase_count, tuple(ids),
            )
        )

    # Native spends while inserting commands and removes total-exhausted
    # listeners only after the entire stable-order trigger batch is built.
    remaining = tuple(
        listener for listener in spent
        if listener.max_uses is None or listener.uses < listener.max_uses
    )
    after = replace(
        runtime,
        aggressive=aggressive_after,
        listeners=remaining,
        pending_interval_effects=(
            *runtime.pending_interval_effects, *queued
        ),
        event_sequence=sequence,
    )
    return AggressiveEventResult(
        runtime, after, event, True, True, "", additive, applied,
        before_value, aggressive_after, phase_incremented,
        tuple(fires), tuple(queued),
        (
            "read-active-additive-fix-once",
            "apply-signed-aggressive-amount-once",
            "increment-phase53-once" if phase_incremented else "skip-phase53-context-filter",
            "collect-interval5-listeners-in-install-order",
            "spend-before-queue-child",
            "remove-total-exhausted-after-batch",
        ),
    )


def execute_queued_aggressive_child(
    runtime: Plan2AggressiveAdditiveIntervalRuntime,
    command_id: str,
) -> AggressiveEventResult:
    """Execute one queued exact Aggressive child; never count its additive twice."""

    matches = tuple(
        command for command in runtime.pending_interval_effects
        if command.command_id == command_id
    )
    if len(matches) != 1:
        raise Plan2AggressiveAdditiveIntervalError(
            "queued-command-not-unique", command_id
        )
    command = matches[0]
    if command.kind != "aggressive-child" or command.effect_id != AGGRESSIVE_EFFECT_ID:
        raise Plan2AggressiveAdditiveIntervalError(
            "queued-command-is-not-aggressive", command_id
        )
    without = replace(
        runtime,
        pending_interval_effects=tuple(
            item for item in runtime.pending_interval_effects
            if item.command_id != command_id
        ),
    )
    return apply_aggressive_event(
        without,
        AggressiveEvent(
            EFFECTS[AGGRESSIVE_EFFECT_ID].value1,
            command.cause_origin,
            command.command_id,
            enchant_trigger_active=True,
        ),
    )


def _zone_for_guid(state: Plan3NativeState, guid: str) -> tuple[str, int, Plan3NativeCard]:
    matches: list[tuple[str, int, Plan3NativeCard]] = []
    for zone in ("hand", "deck", "grave", "lost", "hold"):
        for index, card in enumerate(getattr(state, zone)):
            if card.guid == guid:
                matches.append((zone, index, card))
    if len(matches) != 1:
        raise Plan2AggressiveAdditiveIntervalError("card-guid-not-unique", guid)
    return matches[0]


def _settle_card_to_lost(
    state: Plan3NativeState, guid: str
) -> tuple[Plan3NativeState, str, Plan3NativeCard, Plan3NativeCard]:
    zone, index, card = _zone_for_guid(state, guid)
    if card.play_count >= INT32_MAX:
        raise Plan2AggressiveAdditiveIntervalError("card-play-count-overflow", guid)
    updated = replace(card, play_count=card.play_count + 1)
    zones = {
        name: list(getattr(state, name))
        for name in ("hand", "deck", "grave", "lost", "hold")
    }
    del zones[zone][index]
    zones["lost"].append(updated)
    settled = replace(
        state,
        hand=tuple(zones["hand"]), deck=tuple(zones["deck"]),
        grave=tuple(zones["grave"]), lost=tuple(zones["lost"]),
        hold=tuple(zones["hold"]),
    )
    return settled, zone, card, updated


@dataclass(frozen=True, slots=True)
class P201PlayRequest:
    transaction_id: str
    guid: str
    play_origin: PlayOrigin = "normal"
    additive_uid: int | None = None
    listener_uid: int | None = None
    aggressive_blocked: bool = False
    additive_blocked: bool = False
    encore_blocked: bool = False


@dataclass(frozen=True, slots=True)
class P201PlayResult:
    before: Plan2AggressiveAdditiveIntervalRuntime
    after: Plan2AggressiveAdditiveIntervalRuntime
    request: P201PlayRequest
    resolved: bool
    committed: bool
    reason: str
    history: P201PlayHistory | None
    aggressive_result: AggressiveEventResult | None
    additive_result: AdditiveInstallResult | None
    installed_listener_uid: int | None
    timer_command_id: str | None
    operations: tuple[str, ...]


def play_p201_card(
    runtime: Plan2AggressiveAdditiveIntervalRuntime,
    request: P201PlayRequest,
) -> P201PlayResult:
    """Execute all four exact p201 slots and settle one card transaction."""

    if not isinstance(runtime, Plan2AggressiveAdditiveIntervalRuntime):
        raise Plan2AggressiveAdditiveIntervalError("invalid-runtime")
    if not isinstance(request, P201PlayRequest):
        raise Plan2AggressiveAdditiveIntervalError("invalid-play-request")
    try:
        _text(request.transaction_id, "transaction_id")
        _text(request.guid, "guid")
        if request.transaction_id in runtime.committed_transactions:
            raise Plan2AggressiveAdditiveIntervalError(
                "transaction-already-committed", request.transaction_id
            )
        if request.play_origin not in ("normal", "forced", "extra"):
            raise Plan2AggressiveAdditiveIntervalError("unknown-play-origin")
        source_zone, _index, card = _zone_for_guid(runtime.native_state, request.guid)
        if card.card_id != CARD_ID:
            raise Plan2AggressiveAdditiveIntervalError("card-id-outside-exact-bundle")
        version = runtime.catalog.version(card.effective_upgrade)
        if request.play_origin == "normal" and source_zone != "hand":
            raise Plan2AggressiveAdditiveIntervalError("normal-play-source-is-not-hand")
        cost_paid = version.stamina if request.play_origin == "normal" else 0
        if runtime.stamina < cost_paid:
            raise Plan2AggressiveAdditiveIntervalError("insufficient-stamina")
        if runtime.global_play_count >= INT32_MAX or runtime.turn_play_count >= INT32_MAX:
            raise Plan2AggressiveAdditiveIntervalError("play-count-overflow")
        additive_before = runtime.additive_fix
    except Plan2AggressiveAdditiveIntervalError as error:
        return P201PlayResult(
            runtime, runtime, request, False, False, error.code,
            None, None, None, None, None, (),
        )

    working = replace(runtime, stamina=runtime.stamina - cost_paid)
    aggressive = apply_aggressive_event(
        working,
        AggressiveEvent(
            EFFECTS[AGGRESSIVE_EFFECT_ID].value1,
            request.play_origin,
            request.transaction_id,
            blocked=request.aggressive_blocked,
            enchant_trigger_active=True,
        ),
    )
    if not aggressive.resolved or not aggressive.succeeded:
        return P201PlayResult(
            runtime, runtime, request, aggressive.resolved, False,
            aggressive.reason, None, aggressive, None, None, None, (),
        )
    working = aggressive.after
    additive = install_aggressive_additive_fix(
        working,
        value=EFFECTS[ADDITIVE_EFFECT_ID].value1,
        turn=EFFECTS[ADDITIVE_EFFECT_ID].effect_turn,
        uid=request.additive_uid,
        blocked=request.additive_blocked,
    )
    if not additive.resolved or not additive.committed:
        return P201PlayResult(
            runtime, runtime, request, additive.resolved, False,
            additive.reason, None, aggressive, additive, None, None, (),
        )
    working = additive.after

    timer_id = f"timer:{request.transaction_id}:slot:2"
    if timer_id in {
        timer.command_id for timer in (*working.pending_timers, *working.ready_timer_effects)
    }:
        return P201PlayResult(
            runtime, runtime, request, False, False, "timer-id-collision",
            None, aggressive, additive, None, None, (),
        )
    timer = TimerDrawCommand(timer_id, request.transaction_id, request.guid)
    working = replace(working, pending_timers=(*working.pending_timers, timer))

    once_key = (request.guid, ENCORE_EFFECT_ID)
    installed_listener_uid: int | None = None
    if once_key in working.once_consumed:
        once_outcome: Literal["installed", "skipped-once"] = "skipped-once"
    else:
        if request.encore_blocked:
            return P201PlayResult(
                runtime, runtime, request, False, False,
                "status-enchant-add-blocked", None, aggressive, additive,
                None, None, (),
            )
        next_uid = request.listener_uid
        if next_uid is None:
            next_uid = 1 + max(
                (listener.uid for listener in working.listeners), default=0
            )
        try:
            working = install_interval5_listener(
                working,
                uid=next_uid,
                source_id=CARD_ID,
                captured_guid=request.guid,
                child_effect_ids=(FORCE_PLAY_EFFECT_ID,),
                max_uses=2,
                max_uses_per_turn=1,
                is_encore=True,
            )
        except Plan2AggressiveAdditiveIntervalError as error:
            return P201PlayResult(
                runtime, runtime, request, False, False, error.code,
                None, aggressive, additive, None, None, (),
            )
        installed_listener_uid = next_uid
        once_outcome = "installed"
        working = replace(
            working, once_consumed=(*working.once_consumed, once_key)
        )

    try:
        settled, source_zone, before_card, _after_card = _settle_card_to_lost(
            working.native_state, request.guid
        )
    except Plan2AggressiveAdditiveIntervalError as error:
        return P201PlayResult(
            runtime, runtime, request, False, False, error.code,
            None, aggressive, additive, installed_listener_uid, timer_id, (),
        )
    additive_after = working.additive_fix
    history = P201PlayHistory(
        request.transaction_id, request.guid, version.upgrade,
        request.play_origin, source_zone, "lost", runtime.stamina,
        working.stamina, cost_paid, before_card.play_count,
        before_card.play_count + 1, runtime.global_play_count,
        runtime.global_play_count + 1, runtime.turn_play_count,
        runtime.turn_play_count + 1, runtime.aggressive,
        aggressive.aggressive_after, additive_before, additive_after,
        once_outcome, runtime.native_state.random_state,
        settled.random_state,
    )
    after = replace(
        working,
        native_state=settled,
        committed_transactions=(
            *working.committed_transactions, request.transaction_id
        ),
        global_play_count=working.global_play_count + 1,
        turn_play_count=working.turn_play_count + 1,
        history=(*working.history, history),
    )
    return P201PlayResult(
        runtime, after, request, True, True, "", history,
        aggressive, additive, installed_listener_uid, timer_id,
        (
            "bind-playing-guid-and-effective-upgrade",
            "pay-cost-only-for-normal-play",
            "slot0-aggressive-reads-pre-slot1-additive",
            "slot1-additive-install-or-same-turn-merge",
            "slot2-queue-timer-draw1",
            "slot3-install-once-interval5-encore-or-skip",
            "move-to-lost-once",
            "increment-card-global-turn-counts-once",
            "append-history-once",
        ),
    )


def start_plan2_aggressive_additive_interval5_turn(
    runtime: Plan2AggressiveAdditiveIntervalRuntime,
) -> Plan2AggressiveAdditiveIntervalRuntime:
    """Native SpendTurn then MainStart; preserve fresh status and phase counts."""

    layers: list[AggressiveAdditiveFixLayer] = []
    for layer in runtime.additive_layers:
        if not layer.is_turn_limited or not layer.passing_turn_start:
            layers.append(replace(layer, passing_turn_start=True))
            continue
        remaining = layer.turn - 1
        if remaining > 0:
            layers.append(replace(layer, turn=remaining))
    listeners = tuple(
        replace(listener, uses_this_turn=0) for listener in runtime.listeners
    )
    pending: list[TimerDrawCommand] = []
    ready = list(runtime.ready_timer_effects)
    for timer in runtime.pending_timers:
        remaining = timer.remaining_turns - 1
        updated = replace(timer, remaining_turns=remaining)
        if remaining <= 0:
            ready.append(updated)
        else:
            pending.append(updated)
    return replace(
        runtime,
        additive_layers=tuple(layers),
        listeners=listeners,
        pending_timers=tuple(pending),
        ready_timer_effects=tuple(ready),
        turn_play_count=0,
    )


def remove_interval5_listener(
    runtime: Plan2AggressiveAdditiveIntervalRuntime, uid: int
) -> Plan2AggressiveAdditiveIntervalRuntime:
    uid = _positive(uid, "listener.uid")
    if sum(listener.uid == uid for listener in runtime.listeners) != 1:
        raise Plan2AggressiveAdditiveIntervalError("listener-not-unique", str(uid))
    return replace(
        runtime,
        listeners=tuple(listener for listener in runtime.listeners if listener.uid != uid),
    )


@dataclass(frozen=True, slots=True)
class BaselineChainingProbe:
    baseline_gaps_by_upgrade: tuple[tuple[int, tuple[str, ...]], ...]
    after_forceplay_encore_gaps_by_upgrade: tuple[tuple[int, tuple[str, ...]], ...]
    additive_family_affected: int
    additive_family_direct: int
    additive_family_co_blocked: int
    interval5_family_affected: int
    interval5_family_direct: int
    interval5_family_co_blocked: int
    combined_affected: int
    combined_direct: int
    combined_co_blocked: int


def probe_p201_baseline_chaining(
    artifact: Path | str = DEFAULT_COVERAGE_ARTIFACT,
) -> BaselineChainingProbe:
    """Read the baseline only; never mutate central coverage artifacts."""

    try:
        payload = json.loads(Path(artifact).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise Plan2AggressiveAdditiveIntervalError(
            "coverage-artifact-unreadable", str(error)
        ) from error
    cards = payload.get("cards")
    if not isinstance(cards, list):
        raise Plan2AggressiveAdditiveIntervalError("coverage-cards-shape")
    rows: dict[int, tuple[str, ...]] = {}
    for row in cards:
        if not isinstance(row, dict) or row.get("card_id") != CARD_ID:
            continue
        upgrade = row.get("upgrade")
        gaps = row.get("gaps")
        if type(upgrade) is not int or not isinstance(gaps, list) or any(
            not isinstance(gap, str) for gap in gaps
        ):
            raise Plan2AggressiveAdditiveIntervalError("coverage-card-row-shape")
        rows[upgrade] = tuple(gaps)
    if set(rows) != set(CARD_UPGRADES):
        raise Plan2AggressiveAdditiveIntervalError("coverage-p201-versions-missing")
    force_encore = {
        "C:effect:ProduceExamEffectType_ExamForcePlayCardSearch",
        "C:effect:ProduceExamEffectType_ExamStatusEnchantEncore",
    }
    additive_gap = f"C:effect:{ADDITIVE_EFFECT_TYPE}"
    interval_gap = f"C:status-trigger:{TRIGGER_ID}"
    chained = {
        upgrade: tuple(gap for gap in gaps if gap not in force_encore)
        for upgrade, gaps in rows.items()
    }
    current_fully_covered = all(not gaps for gaps in rows.values())
    legacy_chained_shape = all(
        set(gaps) == {additive_gap, interval_gap}
        for gaps in chained.values()
    )
    if not current_fully_covered and not legacy_chained_shape:
        raise Plan2AggressiveAdditiveIntervalError(
            "unexpected-post-forceplay-encore-baseline-shape"
        )
    affected = len(CARD_UPGRADES)
    family_direct = affected if current_fully_covered else 0
    family_co_blocked = 0 if current_fully_covered else affected
    return BaselineChainingProbe(
        tuple(sorted(rows.items())), tuple(sorted(chained.items())),
        affected, family_direct, family_co_blocked,
        affected, family_direct, family_co_blocked,
        affected, affected, 0,
    )


def catalog_to_dict(
    catalog: Plan2AggressiveAdditiveIntervalCatalog,
) -> dict[str, object]:
    probe = probe_p201_baseline_chaining()
    return {
        "card_id": CARD_ID,
        "versions": [
            {
                "upgrade": version.upgrade,
                "stamina": version.stamina,
                "cost_type": version.cost_type,
                "cost_value": version.cost_value,
                "move_position": version.move_position,
                "ordered_effect_ids": list(version.ordered_effect_ids),
            }
            for version in catalog.versions
        ],
        "additive": {
            "effect_id": ADDITIVE_EFFECT_ID,
            "signed_value": 1,
            "signed_turn": 2,
            "same_turn_rule": "merge-last-and-native-add-clamp-zero",
            "different_turn_rule": "append-independent-layer",
            "sum_rule": "checked-int32-sum-of-active-layers",
        },
        "interval5": {
            "trigger_id": TRIGGER_ID,
            "phase_type": PHASE_TYPE,
            "phase_value": PHASE_VALUE,
            "interval": INTERVAL,
            "effect_types": [AGGRESSIVE_EFFECT_TYPE],
            "counter_rule": "one-per-successful-enchant-active-aggressive-occurrence",
        },
        "accounting": {
            "additive_family": {
                "affected": probe.additive_family_affected,
                "direct": probe.additive_family_direct,
                "co_blocked": probe.additive_family_co_blocked,
            },
            "interval5_family": {
                "affected": probe.interval5_family_affected,
                "direct": probe.interval5_family_direct,
                "co_blocked": probe.interval5_family_co_blocked,
            },
            "combined_after_forceplay_encore": {
                "affected": probe.combined_affected,
                "direct": probe.combined_direct,
                "co_blocked": probe.combined_co_blocked,
            },
        },
    }
