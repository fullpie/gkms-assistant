"""Bounded Plan2 Hand move-effect runtime for one exact card family.

The target card owns ``e_effect-exam_card_play_aggressive-0003`` as a
``moveEffectTriggerType=Hand`` effect.  Android observes a completed zone
commit through the HandAdd difference callback, queues the Master move effect,
sets the per-card move-effect-used flag, and only then runs the Aggressive
executor.  This leaf models that callback boundary and nothing from the
ordinary card-play effect list.

Only ``p_card-02-ido-3_214`` upgrades 0..3 are admitted.  The shared immutable
GUID state remains the source of zone identity and card instances; no second
zone or card-move algorithm is introduced here.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Final

from .logic_engine import load_master_card
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import INT32_MAX, INT32_MIN
from .plan3_native_state import Plan3NativeState


CARD_ID: Final = "p_card-02-ido-3_214"
CARD_NAME: Final = "理想に手が届く日まで"
CARD_UPGRADES: Final = (0, 1, 2, 3)
AFFECTED_CARD_VERSIONS: Final = tuple((CARD_ID, value) for value in CARD_UPGRADES)
AFFECTED_CARD_VERSION_COUNT: Final = 4
DIRECT_CARD_VERSION_COUNT: Final = 4
CO_BLOCKED_CARD_VERSION_COUNT: Final = 0

PLAN2: Final = "ProducePlanType_Plan2"
MENTAL_SKILL: Final = "ProduceCardCategory_MentalSkill"
MOVE_TRIGGER_HAND: Final = "ProduceCardMoveEffectTriggerType_Hand"
MOVE_EFFECT_ID: Final = "e_effect-exam_card_play_aggressive-0003"
MOVE_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamCardPlayAggressive"
MOVE_HAND: Final = "ProduceCardMovePositionType_Hand"
MOVE_GRAVE: Final = "ProduceCardMovePositionType_Grave"
MOVE_LOST: Final = "ProduceCardMovePositionType_Lost"
MOVE_HOLD: Final = "ProduceCardMovePositionType_Hold"
CHILD_VALUE: Final = 3
CHILD_EFFECT_GROUP_ID: Final = (
    "effect_group-visible-exam_card_play_aggressive-000"
)

ZONE_NAMES: Final = frozenset(("hand", "deck", "grave", "lost", "hold"))
# These are the source groups represented by the existing exact GUID move
# primitive for a HandAdd.  A PlayingCard or a newly-created card has no
# authoritative Plan3NativeState source in this bounded leaf.
HAND_ADD_SOURCES: Final = frozenset(("deck", "grave", "hold"))
CARD_MOVE_SOURCES: Final = frozenset(("hand", "deck", "grave", "hold"))

ORDINARY_EFFECT_IDS_BY_UPGRADE: Final = (
    ("e_effect-exam_card_play_aggressive-0001", "e_effect-exam_playable_value_add-01"),
    ("e_effect-exam_card_play_aggressive-0003", "e_effect-exam_playable_value_add-01"),
    ("e_effect-exam_card_play_aggressive-0004", "e_effect-exam_playable_value_add-01"),
    ("e_effect-exam_card_play_aggressive-0005", "e_effect-exam_playable_value_add-01"),
)
ORDINARY_AGGRESSIVE_VALUES_BY_UPGRADE: Final = (1, 3, 4, 5)
CARD_STAMINA_BY_UPGRADE: Final = (4, 3, 3, 3)
CARD_NAMES_BY_UPGRADE: Final = (
    "理想に手が届く日まで",
    "理想に手が届く日まで+",
    "理想に手が届く日まで++",
    "理想に手が届く日まで+++",
)

NATIVE_HAND_ADD_ORDER: Final = (
    "authoritative-zone-commit",
    "HandAdd-difference-callback",
    "Master-move-effect-queue",
    "child-Aggressive-executor",
)
NATIVE_CARD_PLAY_NO_HAND_TRIGGER_ORDER: Final = (
    "authoritative-zone-commit",
    "no-HandAdd-difference-for-Hand->Grave",
    "card-play-count-update-after-final-zone-move",
    "global-card-play-count-update-after-card-play-count",
)


class Plan2MoveCardPlayAggressiveError(ValueError):
    """A target Master row or runtime callback is outside the leaf contract."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class MoveReason(str, Enum):
    """Move origins admitted by the callback boundary."""

    ORDINARY_PLAY = "ordinary-play"
    FORCED_PLAY = "forced-play"
    END_TURN_DISCARD = "end-turn-discard"
    DRAW = "draw"
    REPLACE = "replace"
    CARD_MOVE = "card-move"
    HAND_ADD = "hand-add"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class HandMoveCardVersion:
    card_id: str
    upgrade: int
    name: str
    plan_type: str
    category: str
    stamina: int
    cost_type: str
    cost_value: int
    play_effect_ids: tuple[str, ...]
    move_position_type: str
    move_trigger_type: str
    move_effect_ids: tuple[str, ...]
    move_trigger_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "cardId": self.card_id,
            "upgrade": self.upgrade,
            "name": self.name,
            "planType": self.plan_type,
            "category": self.category,
            "stamina": self.stamina,
            "costType": self.cost_type,
            "costValue": self.cost_value,
            "playEffectIds": list(self.play_effect_ids),
            "movePositionType": self.move_position_type,
            "moveEffectTriggerType": self.move_trigger_type,
            "moveEffectIds": list(self.move_effect_ids),
            "moveTriggerIds": list(self.move_trigger_ids),
        }


@dataclass(frozen=True, slots=True)
class HandMoveAggressiveContract:
    card: HandMoveCardVersion
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str
    chain_effect_id: str
    effect_group_ids: tuple[str, ...]

    @property
    def child_value(self) -> int:
        return self.value1

    def to_dict(self) -> dict[str, object]:
        return {
            "card": self.card.to_dict(),
            "child": {
                "id": self.effect_id,
                "effectType": self.effect_type,
                "effectValue1": self.value1,
                "effectValue2": self.value2,
                "effectCount": self.effect_count,
                "effectTurn": self.effect_turn,
                "produceExamStatusEnchantId": self.status_enchant_id,
                "chainProduceExamEffectId": self.chain_effect_id,
                "effectGroupIds": list(self.effect_group_ids),
            },
        }


def _strict_equal(actual: object, expected: object, label: str) -> None:
    if type(actual) is not type(expected):
        raise Plan2MoveCardPlayAggressiveError(
            "master-shape", f"{label}={actual!r}; expected={expected!r}"
        )
    if isinstance(expected, list):
        if len(actual) != len(expected):  # type: ignore[arg-type]
            raise Plan2MoveCardPlayAggressiveError("master-shape", label)
        for index, (item, wanted) in enumerate(zip(actual, expected)):
            _strict_equal(item, wanted, f"{label}[{index}]")
        return
    if actual != expected:
        raise Plan2MoveCardPlayAggressiveError(
            "master-shape", f"{label}={actual!r}; expected={expected!r}"
        )


def _json_object(value: object, label: str) -> dict[str, object]:
    try:
        result = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise Plan2MoveCardPlayAggressiveError("master-json", label) from error
    if not isinstance(result, dict):
        raise Plan2MoveCardPlayAggressiveError("master-json", label)
    return result


def _read_effect_row(effect_id: str, database: Path) -> sqlite3.Row:
    path = Path(database).resolve()
    try:
        with closing(
            sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        ) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM effect WHERE id = ?", (effect_id,)
            ).fetchone()
    except sqlite3.Error as error:
        raise Plan2MoveCardPlayAggressiveError("master-read", effect_id) from error
    if row is None:
        raise Plan2MoveCardPlayAggressiveError("missing-child", effect_id)
    return row


def _validate_child(effect_id: str, database: Path) -> tuple[int, int, int, int, str, str, tuple[str, ...]]:
    row = _read_effect_row(effect_id, database)
    raw = _json_object(row["raw_json"], effect_id)
    expected = {
        "id": MOVE_EFFECT_ID,
        "effectType": MOVE_EFFECT_TYPE,
        "effectValue1": CHILD_VALUE,
        "effectValue2": 0,
        "effectCount": 0,
        "effectTurn": 0,
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
        "effectGroupIds": [CHILD_EFFECT_GROUP_ID],
    }
    for key, value in expected.items():
        if key not in raw:
            raise Plan2MoveCardPlayAggressiveError("master-shape", f"{effect_id}:{key}")
        _strict_equal(raw[key], value, f"{effect_id}.{key}")
    columns = (
        ("id", effect_id, MOVE_EFFECT_ID),
        ("effect_type", str(row["effect_type"]), MOVE_EFFECT_TYPE),
        ("value1", int(row["value1"]), CHILD_VALUE),
        ("value2", int(row["value2"]), 0),
        ("effect_count", int(row["effect_count"]), 0),
        ("effect_turn", int(row["effect_turn"]), 0),
        ("status_enchant_id", str(row["status_enchant_id"]), ""),
        ("chain_effect_id", str(row["chain_effect_id"]), ""),
    )
    for label, actual, wanted in columns:
        if actual != wanted:
            raise Plan2MoveCardPlayAggressiveError(
                "master-shape", f"{effect_id}.{label}={actual!r}"
            )
    return (
        CHILD_VALUE,
        0,
        0,
        0,
        "",
        "",
        (CHILD_EFFECT_GROUP_ID,),
    )


def _load_contract_uncached(upgrade: int, database: Path) -> HandMoveAggressiveContract:
    if type(upgrade) is not int or upgrade not in CARD_UPGRADES:
        raise Plan2MoveCardPlayAggressiveError("unsupported-upgrade", str(upgrade))
    try:
        master = load_master_card(CARD_ID, upgrade, Path(database))
    except (KeyError, OSError, ValueError) as error:
        raise Plan2MoveCardPlayAggressiveError("missing-card", f"{CARD_ID}+{upgrade}") from error

    expected_play_ids = ORDINARY_EFFECT_IDS_BY_UPGRADE[upgrade]
    if (
        master.id != CARD_ID
        or master.upgrade != upgrade
        or master.name != CARD_NAMES_BY_UPGRADE[upgrade]
        or master.plan_type != PLAN2
        or master.category != MENTAL_SKILL
        or master.stamina_cost != CARD_STAMINA_BY_UPGRADE[upgrade]
        or master.cost_type != "ExamCostType_Unknown"
        or master.cost_value != 0
        or master.play_trigger_id != ""
        or master.move_position_type != MOVE_GRAVE
        or tuple(effect.id for effect in master.effects) != expected_play_ids
        or master.move_effect_trigger_type != MOVE_TRIGGER_HAND
        or master.move_effect_ids != (MOVE_EFFECT_ID,)
        or master.move_trigger_ids != ()
        or master.produce_card_status_enchant_id != ""
    ):
        raise Plan2MoveCardPlayAggressiveError("master-card-shape", f"{CARD_ID}+{upgrade}")

    ordinary = master.effects[0]
    if (
        ordinary.effect_type != MOVE_EFFECT_TYPE
        or ordinary.value1 != ORDINARY_AGGRESSIVE_VALUES_BY_UPGRADE[upgrade]
        or ordinary.value2 != 0
        or ordinary.effect_count != 0
        or ordinary.effect_turn != 0
        or ordinary.status_enchant_id
        or ordinary.chain_effect_id
    ):
        raise Plan2MoveCardPlayAggressiveError("ordinary-effect-shape", f"{CARD_ID}+{upgrade}")
    playable = master.effects[1]
    if (
        playable.effect_type != "ProduceExamEffectType_ExamPlayableValueAdd"
        or playable.value1 != 0
        or playable.value2 != 0
        or playable.effect_count != 1
        or playable.effect_turn != 0
        or playable.status_enchant_id
        or playable.chain_effect_id
    ):
        raise Plan2MoveCardPlayAggressiveError("ordinary-effect-shape", f"{CARD_ID}+{upgrade}")

    raw_child = _validate_child(MOVE_EFFECT_ID, Path(database))
    card = HandMoveCardVersion(
        CARD_ID,
        upgrade,
        master.name,
        master.plan_type,
        master.category,
        master.stamina_cost,
        master.cost_type,
        master.cost_value,
        expected_play_ids,
        master.move_position_type,
        master.move_effect_trigger_type,
        master.move_effect_ids,
        master.move_trigger_ids,
    )
    return HandMoveAggressiveContract(
        card,
        MOVE_EFFECT_ID,
        MOVE_EFFECT_TYPE,
        *raw_child,
    )


def load_plan2_move_card_play_aggressive_contract(
    upgrade: int, database: Path = DEFAULT_DATABASE
) -> HandMoveAggressiveContract:
    """Load one exact target card version and its one move child."""

    return _load_contract_uncached(upgrade, Path(database))


load_target_contract = load_plan2_move_card_play_aggressive_contract


def load_all_plan2_move_card_play_aggressive_contracts(
    database: Path = DEFAULT_DATABASE,
) -> tuple[HandMoveAggressiveContract, ...]:
    return tuple(
        load_plan2_move_card_play_aggressive_contract(upgrade, database)
        for upgrade in CARD_UPGRADES
    )


@dataclass(frozen=True, slots=True)
class HandMoveAggressiveRuntimeState:
    """Leaf-local scalar plus the shared immutable GUID zone state."""

    native_state: Plan3NativeState
    aggressive_value: int = 0
    global_card_play_count: int = 0
    turn_card_play_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.native_state, Plan3NativeState):
            raise TypeError("native_state must be Plan3NativeState")
        for name in ("aggressive_value", "global_card_play_count", "turn_card_play_count"):
            value = getattr(self, name)
            if type(value) is not int or not INT32_MIN <= value <= INT32_MAX:
                raise Plan2MoveCardPlayAggressiveError("invalid-int32", name)
        for name in ("global_card_play_count", "turn_card_play_count"):
            if getattr(self, name) < 0:
                raise Plan2MoveCardPlayAggressiveError("invalid-play-count", name)

    @property
    def card_play_aggressive(self) -> int:
        return self.aggressive_value


@dataclass(frozen=True, slots=True)
class HandMoveCommitEvent:
    """One already-committed card move and its native reason label."""

    before: Plan3NativeState
    after_commit: Plan3NativeState
    moved_guid: str
    source_zone: str
    destination_zone: str
    move_reason: MoveReason | str

    def __post_init__(self) -> None:
        if not isinstance(self.before, Plan3NativeState) or not isinstance(
            self.after_commit, Plan3NativeState
        ):
            raise TypeError("before and after_commit must be Plan3NativeState")
        if type(self.moved_guid) is not str or not self.moved_guid:
            raise TypeError("moved_guid must be non-empty text")
        for name in ("source_zone", "destination_zone"):
            if type(getattr(self, name)) is not str or not getattr(self, name):
                raise TypeError(f"{name} must be non-empty text")
        if not isinstance(self.move_reason, (MoveReason, str)):
            raise TypeError("move_reason must be MoveReason or text")

    @property
    def after(self) -> Plan3NativeState:
        return self.after_commit


@dataclass(frozen=True, slots=True)
class AggressiveChildResult:
    before: int
    after: int
    effect_id: str
    value1: int
    arithmetic: str = "signed-int32-add; singleton-status-merge; Int32-cap-fail-closed"


@dataclass(frozen=True, slots=True)
class HandMoveAggressiveRuntimeResult:
    before: HandMoveAggressiveRuntimeState
    after: HandMoveAggressiveRuntimeState
    event: HandMoveCommitEvent
    contract: HandMoveAggressiveContract | None
    triggered: bool
    executed: bool
    used_move_effect_guids: frozenset[str]
    queued_move_effect_ids: tuple[str, ...]
    child_effect_ids: tuple[str, ...]
    operations: tuple[str, ...]
    resolved: bool
    reason: str = ""
    detail: str = ""

    @property
    def aggressive_before(self) -> int:
        return self.before.aggressive_value

    @property
    def aggressive_after(self) -> int:
        return self.after.aggressive_value

    @property
    def card_play_count_changed(self) -> bool:
        return self.before.global_card_play_count != self.after.global_card_play_count


def _zone_of(state: Plan3NativeState, guid: str) -> str:
    found = tuple(
        zone
        for zone in ("hand", "deck", "grave", "lost", "hold")
        if any(card.guid == guid for card in getattr(state, zone))
    )
    if len(found) != 1:
        raise Plan2MoveCardPlayAggressiveError("guid-zone-count", f"{guid}:{len(found)}")
    return found[0]


def _card_by_guid(state: Plan3NativeState, guid: str):
    try:
        return state.card_by_guid(guid)
    except ValueError as error:
        raise Plan2MoveCardPlayAggressiveError("guid-not-found", guid) from error


def _normalize_reason(value: MoveReason | str) -> str:
    return value.value if isinstance(value, MoveReason) else value


def _reason_shape(reason: str, source: str, destination: str) -> bool:
    if reason in {MoveReason.DRAW.value, MoveReason.HAND_ADD.value}:
        return source in HAND_ADD_SOURCES and destination == "hand"
    if reason == MoveReason.REPLACE.value:
        return (source in HAND_ADD_SOURCES and destination == "hand") or (
            source == "hand" and destination in {"grave", "hold"}
        )
    if reason == MoveReason.ORDINARY_PLAY.value:
        return source == "hand" and destination == "grave"
    if reason == MoveReason.FORCED_PLAY.value:
        return source in {"hand", "lost"} and destination == "grave"
    if reason == MoveReason.END_TURN_DISCARD.value:
        return source == "hand" and destination == "grave"
    if reason == MoveReason.HOLD.value:
        return source == "hand" and destination == "hold"
    if reason == MoveReason.CARD_MOVE.value:
        return source in CARD_MOVE_SOURCES and destination in {
            "hand",
            "grave",
            "lost",
            "hold",
        }
    return False


def _same_other_cards(before: Plan3NativeState, after: Plan3NativeState, guid: str) -> bool:
    for zone in ZONE_NAMES:
        left = tuple(card for card in getattr(before, zone) if card.guid != guid)
        right = tuple(card for card in getattr(after, zone) if card.guid != guid)
        if left != right:
            return False
    return True


def _failed(
    runtime: HandMoveAggressiveRuntimeState,
    event: HandMoveCommitEvent,
    used: frozenset[str],
    operations: tuple[str, ...],
    code: str,
    detail: str = "",
    *,
    contract: HandMoveAggressiveContract | None = None,
    committed: bool = False,
) -> HandMoveAggressiveRuntimeResult:
    after = replace(runtime, native_state=event.after_commit) if committed else runtime
    return HandMoveAggressiveRuntimeResult(
        runtime,
        after,
        event,
        contract,
        False,
        False,
        used,
        (),
        (),
        operations,
        False,
        code,
        detail,
    )


def _execute_child_aggressive(
    value: int, contract: HandMoveAggressiveContract
) -> AggressiveChildResult:
    if contract.effect_id != MOVE_EFFECT_ID or contract.effect_type != MOVE_EFFECT_TYPE:
        raise Plan2MoveCardPlayAggressiveError("unknown-child", contract.effect_id)
    if contract.value1 != CHILD_VALUE or contract.value2 or contract.effect_count or contract.effect_turn:
        raise Plan2MoveCardPlayAggressiveError("child-shape", contract.effect_id)
    if type(value) is not int or not INT32_MIN <= value <= INT32_MAX:
        raise Plan2MoveCardPlayAggressiveError("invalid-aggressive-value", str(value))
    merged = value + contract.value1
    if not INT32_MIN <= merged <= INT32_MAX:
        raise Plan2MoveCardPlayAggressiveError(
            "aggressive-int32-cap", f"{value}+{contract.value1}"
        )
    return AggressiveChildResult(value, merged, contract.effect_id, contract.value1)


def execute_plan2_move_card_play_aggressive(
    event: HandMoveCommitEvent,
    runtime: HandMoveAggressiveRuntimeState,
    *,
    used_move_effect_guids: frozenset[str] = frozenset(),
    database: Path = DEFAULT_DATABASE,
) -> HandMoveAggressiveRuntimeResult:
    """Execute only the target's post-commit Hand move-effect callback."""

    if not isinstance(event, HandMoveCommitEvent):
        raise TypeError("event must be HandMoveCommitEvent")
    if not isinstance(runtime, HandMoveAggressiveRuntimeState):
        raise TypeError("runtime must be HandMoveAggressiveRuntimeState")
    if not isinstance(used_move_effect_guids, frozenset) or any(
        type(value) is not str or not value for value in used_move_effect_guids
    ):
        raise TypeError("used_move_effect_guids must be a frozenset of text")

    operations: list[str] = ["authoritative-zone-commit"]
    if event.before != runtime.native_state:
        return _failed(
            runtime,
            event,
            used_move_effect_guids,
            tuple((*operations, "reject-callback-state-order")),
            "callback-state-order-mismatch",
            event.moved_guid,
        )
    if event.source_zone not in ZONE_NAMES:
        return _failed(runtime, event, used_move_effect_guids, tuple(operations), "unknown-source", event.source_zone)
    if event.destination_zone not in ZONE_NAMES:
        return _failed(runtime, event, used_move_effect_guids, tuple(operations), "unknown-destination", event.destination_zone)
    try:
        inferred_source = _zone_of(event.before, event.moved_guid)
        inferred_destination = _zone_of(event.after_commit, event.moved_guid)
        before_card = _card_by_guid(event.before, event.moved_guid)
        after_card = _card_by_guid(event.after_commit, event.moved_guid)
    except Plan2MoveCardPlayAggressiveError as error:
        return _failed(
            runtime,
            event,
            used_move_effect_guids,
            tuple((*operations, "reject-invalid-guid-zone")),
            error.code,
            error.detail,
        )
    if (event.source_zone, event.destination_zone) != (
        inferred_source,
        inferred_destination,
    ):
        return _failed(
            runtime,
            event,
            used_move_effect_guids,
            tuple((*operations, "reject-declared-zone-mismatch")),
            "declared-zone-mismatch",
            f"{event.source_zone}->{event.destination_zone}; inferred {inferred_source}->{inferred_destination}",
        )
    if inferred_source == inferred_destination:
        return _failed(
            runtime,
            event,
            used_move_effect_guids,
            tuple((*operations, "reject-no-zone-difference")),
            "move-did-not-change-zone",
            event.moved_guid,
        )
    if not _same_other_cards(event.before, event.after_commit, event.moved_guid):
        return _failed(
            runtime,
            event,
            used_move_effect_guids,
            tuple((*operations, "reject-unrelated-zone-difference")),
            "unrelated-zone-difference",
            event.moved_guid,
        )
    if before_card != after_card:
        return _failed(
            runtime,
            event,
            used_move_effect_guids,
            tuple((*operations, "reject-card-state-changed-before-callback")),
            "card-state-changed-before-callback",
            event.moved_guid,
        )
    if (
        before_card.card_id != CARD_ID
        or after_card.card_id != CARD_ID
        or before_card.base_upgrade != before_card.effective_upgrade
        or after_card.base_upgrade != after_card.effective_upgrade
        or before_card.temporary_upgrade != 0
        or after_card.temporary_upgrade != 0
        or before_card.support_upgrade_ids
        or after_card.support_upgrade_ids
        or before_card.effective_upgrade not in CARD_UPGRADES
        or after_card.effective_upgrade != before_card.effective_upgrade
    ):
        return _failed(
            runtime,
            event,
            used_move_effect_guids,
            tuple((*operations, "reject-card-guid-or-upgrade-shape")),
            "unsupported-card-shape",
            f"{before_card.card_id}+{before_card.effective_upgrade}:{event.moved_guid}",
            committed=True,
        )

    reason = _normalize_reason(event.move_reason)
    if not _reason_shape(reason, inferred_source, inferred_destination):
        operations.extend(
            (
                "difference-callback:unclassified",
                f"difference:{inferred_source}->{inferred_destination}",
                f"move-reason:{reason}",
            )
        )
        return _failed(
            runtime,
            event,
            used_move_effect_guids,
            tuple((*operations, "reject-unknown-or-incompatible-move-reason")),
            "unknown-move-reason" if reason not in {item.value for item in MoveReason} else "move-reason-shape",
            reason,
            committed=True,
        )
    if inferred_destination == "hand":
        operations.extend(
            (
                "HandAdd-difference-callback",
                f"difference:{inferred_source}->{inferred_destination}",
                f"move-reason:{reason}",
            )
        )
    else:
        operations.extend(
            (
                "difference-callback:not-HandAdd",
                f"difference:{inferred_source}->{inferred_destination}",
                f"move-reason:{reason}",
            )
        )

    try:
        contract = load_plan2_move_card_play_aggressive_contract(
            before_card.effective_upgrade, Path(database)
        )
    except Plan2MoveCardPlayAggressiveError as error:
        return _failed(
            runtime,
            event,
            used_move_effect_guids,
            tuple((*operations, "reject-master-card-or-child-shape")),
            error.code,
            error.detail,
            committed=True,
        )
    after_committed = replace(runtime, native_state=event.after_commit)

    # Hand is a destination trigger.  A Hand source moving to Grave/Hold is a
    # known non-fire path; it is not treated as a wildcard Hand move.
    if inferred_destination != "hand":
        operations.extend(
            (
                "move-trigger:not-fired-destination-is-not-Hand",
                "card-play-count:update-deferred-to-MovePlayCard",
                "global-card-play-count:update-deferred-to-MovePlayCard",
            )
        )
        return HandMoveAggressiveRuntimeResult(
            runtime,
            after_committed,
            event,
            contract,
            False,
            False,
            used_move_effect_guids,
            (),
            (),
            tuple(operations),
            True,
        )

    if inferred_source not in HAND_ADD_SOURCES:
        return _failed(
            runtime,
            event,
            used_move_effect_guids,
            tuple((*operations, "reject-unproven-HandAdd-source")),
            "unknown-hand-add-source",
            inferred_source,
            contract=contract,
            committed=True,
        )
    if event.moved_guid in used_move_effect_guids:
        operations.append("skip-is-move-effect-use-in-turn")
        return HandMoveAggressiveRuntimeResult(
            runtime,
            after_committed,
            event,
            contract,
            True,
            False,
            used_move_effect_guids,
            (),
            (),
            tuple(operations),
            True,
        )

    operations.append(f"queue-master-move-effect:{contract.effect_id}")
    used = frozenset((*used_move_effect_guids, event.moved_guid))
    operations.append("set-is-move-effect-use-in-turn")
    operations.append(f"execute-child-aggressive:{contract.effect_id}:value1={contract.value1}")
    try:
        child = _execute_child_aggressive(runtime.aggressive_value, contract)
    except Plan2MoveCardPlayAggressiveError as error:
        return HandMoveAggressiveRuntimeResult(
            runtime,
            after_committed,
            event,
            contract,
            True,
            False,
            used,
            (contract.effect_id,),
            (contract.effect_id,),
            tuple((*operations, "reject-child-aggressive-shape-or-int32-cap")),
            False,
            error.code,
            error.detail,
        )
    after = replace(after_committed, aggressive_value=child.after)
    operations.extend(
        (
            "merge-aggressive-into-singleton-status",
            f"aggressive:signed-int32:{child.before}+{child.value1}={child.after}",
            "card-play-count:unchanged-for-HandAdd",
            "global-card-play-count:unchanged-for-HandAdd",
        )
    )
    return HandMoveAggressiveRuntimeResult(
        runtime,
        after,
        event,
        contract,
        True,
        True,
        used,
        (contract.effect_id,),
        (contract.effect_id,),
        tuple(operations),
        True,
    )


execute_hand_move_effect = execute_plan2_move_card_play_aggressive
apply_plan2_move_card_play_aggressive = execute_plan2_move_card_play_aggressive
simulate_plan2_move_card_play_aggressive = execute_plan2_move_card_play_aggressive


__all__ = [
    "CARD_ID",
    "CARD_NAME",
    "CARD_UPGRADES",
    "AFFECTED_CARD_VERSIONS",
    "AFFECTED_CARD_VERSION_COUNT",
    "DIRECT_CARD_VERSION_COUNT",
    "CO_BLOCKED_CARD_VERSION_COUNT",
    "PLAN2",
    "MENTAL_SKILL",
    "MOVE_TRIGGER_HAND",
    "MOVE_EFFECT_ID",
    "MOVE_EFFECT_TYPE",
    "MOVE_HAND",
    "MOVE_GRAVE",
    "MOVE_LOST",
    "MOVE_HOLD",
    "CHILD_VALUE",
    "NATIVE_HAND_ADD_ORDER",
    "NATIVE_CARD_PLAY_NO_HAND_TRIGGER_ORDER",
    "Plan2MoveCardPlayAggressiveError",
    "MoveReason",
    "HandMoveCardVersion",
    "HandMoveAggressiveContract",
    "load_plan2_move_card_play_aggressive_contract",
    "load_target_contract",
    "load_all_plan2_move_card_play_aggressive_contracts",
    "HandMoveAggressiveRuntimeState",
    "HandMoveCommitEvent",
    "AggressiveChildResult",
    "HandMoveAggressiveRuntimeResult",
    "execute_plan2_move_card_play_aggressive",
    "execute_hand_move_effect",
    "apply_plan2_move_card_play_aggressive",
    "simulate_plan2_move_card_play_aggressive",
]
