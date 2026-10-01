"""Exact standalone simulator for the three remaining Plan2 CardMove shapes.

The ordinary CardMove adapter deliberately covers only the shared five-zone
subset.  This module is narrower by card/version, but wider by native shape:
NotLost, category-filtered DeckGrave, DeckAll (including Playing and timeline
decks), DeckFirst, and native Hand overflow are represented explicitly.

Search/selection captures ordered GUIDs.  The mutation phase rematches those
GUIDs in the current state, so cards created after capture are never pulled
into the move and a relocated GUID is moved only if it still satisfies the
same search at execution time.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3
from typing import Literal, TypeAlias

from .audition_native_support import NativeHandAddCard, evaluate_native_hand_add_support
from .audition_support_runtime import SupportUpgradeRuntimeInput
from .card_search import ProduceCardSearchRule, load_produce_card_search
from .exam_native_rng import INT32_MAX, UINT32_MASK, next_int32, next_range
from .master_db import DEFAULT_DATABASE
from .plan3_native_state import Plan3NativeCard


CARD_041 = "p_card-02-ido-100_041"
CARD_046 = "p_card-02-ido-100_046"
CARD_198 = "p_card-02-ido-3_198"

EFFECT_NOT_LOST_HAND = (
    "e_effect-exam_card_move-p_card_search-not_lost-"
    "p_card-02-ido-3_146-hand-all-0_0"
)
EFFECT_MENTAL_DECK_GRAVE = (
    "e_effect-exam_card_move-p_card_search-mental_skill-"
    "deck_grave-deck_first-random-1_1"
)
EFFECT_DECK_ALL_211 = (
    "e_effect-exam_card_move-p_card_search-deck_all-"
    "p_card-02-ido-3_211-deck_first-all-0_0"
)
TIMER_041 = (
    "e_effect-exam_effect_timer-0001-01-"
    + EFFECT_NOT_LOST_HAND
)
STATUS_EFFECT_046 = "e_effect-exam_status_enchant-05-enchant-p_card-02-ido-100_046-enc01"
STATUS_046 = "enchant-p_card-02-ido-100_046-enc01"
TRIGGER_046 = "e_trigger-exam_status_change-exam_card_play_aggressive"

SEARCH_NOT_LOST_146 = "p_card_search-not_lost-p_card-02-ido-3_146"
SEARCH_MENTAL_DECK_GRAVE = "p_card_search-mental_skill-deck_grave"
SEARCH_DECK_ALL_211 = "p_card_search-deck_all-p_card-02-ido-3_211"

SEARCH_NOT_LOST = "ProduceCardPositionType_NotLost"
SEARCH_DECK_GRAVE = "ProduceCardPositionType_DeckGrave"
SEARCH_DECK_ALL = "ProduceCardPositionType_DeckAll"
MOVE_HAND = "ProduceCardMovePositionType_Hand"
MOVE_DECK_FIRST = "ProduceCardMovePositionType_DeckFirst"
PICK_ALL = "ProducePickRangeType_All"
PICK_RANDOM = "ProducePickRangeType_Random"
MENTAL_SKILL = "ProduceCardCategory_MentalSkill"

FORMAL_AFFECTED_VERSION_REFS = (
    f"{CARD_041}#0",
    f"{CARD_046}#0",
    *(f"{CARD_198}#{upgrade}" for upgrade in range(4)),
)
FORMAL_DIRECT_VERSION_REFS = (f"{CARD_046}#0",)
FORMAL_CO_BLOCKED_VERSION_REFS = tuple(
    ref for ref in FORMAL_AFFECTED_VERSION_REFS if ref not in FORMAL_DIRECT_VERSION_REFS
)

CardMoveSource: TypeAlias = Literal[
    "hand", "deck", "grave", "lost", "hold", "playing", "future", "past"
]
HandoffKind: TypeAlias = Literal["timer-child", "status-trigger-child", "direct-effect"]
PlayOrigin: TypeAlias = Literal["normal", "forced", "extra", "callback"]
SupportInputs: TypeAlias = Mapping[str, SupportUpgradeRuntimeInput] | Iterable[SupportUpgradeRuntimeInput]


class RemainingCardMoveError(ValueError):
    """Fail-closed error with a stable machine-readable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


@dataclass(frozen=True, slots=True)
class RemainingCardMoveState:
    """All native search pools required by the three admitted shapes."""

    hand: tuple[Plan3NativeCard, ...] = ()
    deck: tuple[Plan3NativeCard, ...] = ()
    grave: tuple[Plan3NativeCard, ...] = ()
    lost: tuple[Plan3NativeCard, ...] = ()
    hold: tuple[Plan3NativeCard, ...] = ()
    playing: Plan3NativeCard | None = None
    future_decks: tuple[tuple[Plan3NativeCard, ...], ...] = ()
    past_decks: tuple[tuple[Plan3NativeCard, ...], ...] = ()
    random_state: int = 0
    turn_used_support_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        cards: list[Plan3NativeCard] = []
        for name in ("hand", "deck", "grave", "lost", "hold"):
            values = tuple(getattr(self, name))
            if not all(isinstance(card, Plan3NativeCard) for card in values):
                raise RemainingCardMoveError("invalid-zone-card", name)
            object.__setattr__(self, name, values)
            cards.extend(values)
        for name in ("future_decks", "past_decks"):
            decks = tuple(tuple(deck) for deck in getattr(self, name))
            if any(not all(isinstance(card, Plan3NativeCard) for card in deck) for deck in decks):
                raise RemainingCardMoveError("invalid-timeline-card", name)
            object.__setattr__(self, name, decks)
            for deck in decks:
                cards.extend(deck)
        if self.playing is not None:
            if not isinstance(self.playing, Plan3NativeCard):
                raise RemainingCardMoveError("invalid-playing-card")
            cards.append(self.playing)
        guids = tuple(card.guid for card in cards)
        if len(set(guids)) != len(guids):
            raise RemainingCardMoveError("duplicate-guid")
        if isinstance(self.random_state, bool) or not isinstance(self.random_state, int) or not 0 <= self.random_state <= UINT32_MASK:
            raise RemainingCardMoveError("invalid-random-state")
        used = tuple(self.turn_used_support_ids)
        if any(not isinstance(value, str) or not value for value in used) or len(set(used)) != len(used):
            raise RemainingCardMoveError("invalid-used-support-ids")
        object.__setattr__(self, "turn_used_support_ids", used)
        for card in (*self.deck, *self.grave, *self.lost):
            if card.support_upgrade_ids:
                raise RemainingCardMoveError("support-upgrade-outside-hand-hold", card.guid)
        for decks in (self.future_decks, self.past_decks):
            for deck in decks:
                for card in deck:
                    if card.support_upgrade_ids:
                        raise RemainingCardMoveError("support-upgrade-in-timeline", card.guid)

    @property
    def all_cards(self) -> tuple[Plan3NativeCard, ...]:
        playing = () if self.playing is None else (self.playing,)
        return (
            *self.hand, *self.deck, *self.grave, *self.lost, *self.hold,
            *playing,
            *(card for deck in self.future_decks for card in deck),
            *(card for deck in self.past_decks for card in deck),
        )


@dataclass(frozen=True, slots=True)
class RemainingCardMoveContract:
    card_id: str
    upgrade: int
    effect_id: str
    search: ProduceCardSearchRule
    destination: str
    pick: str
    count_min: int
    count_max: int
    handoff_kind: HandoffKind
    parent_id: str
    effect_index: int
    co_blockers: tuple[str, ...] = ()

    @property
    def version_ref(self) -> str:
        return f"{self.card_id}#{self.upgrade}"


@dataclass(frozen=True, slots=True)
class RemainingCardMoveHandoff:
    kind: HandoffKind
    parent_id: str
    phase: str
    effect_index: int
    play_origin: PlayOrigin
    queue_dispatch_count: int = 1


@dataclass(frozen=True, slots=True)
class RemainingCardMoveTarget:
    guid: str
    card: Plan3NativeCard
    source: CardMoveSource
    source_group: int
    source_index: int


@dataclass(frozen=True, slots=True)
class RemainingCardMoveSnapshot:
    contract: RemainingCardMoveContract
    candidates: tuple[RemainingCardMoveTarget, ...]
    selected: tuple[RemainingCardMoveTarget, ...]
    random_state_before: int
    random_state_after: int
    handoff: RemainingCardMoveHandoff

    @property
    def selected_guids(self) -> tuple[str, ...]:
        return tuple(target.guid for target in self.selected)


@dataclass(frozen=True, slots=True)
class RemainingCardMoveMutation:
    kind: Literal["remove", "insert", "skip"]
    guid: str
    zone: str
    index: int
    detail: str = ""


@dataclass(frozen=True, slots=True)
class RemainingCardMoveResult:
    before: RemainingCardMoveState
    after: RemainingCardMoveState
    snapshot: RemainingCardMoveSnapshot
    rematched: tuple[RemainingCardMoveTarget, ...]
    skipped_guids: tuple[str, ...]
    mutations: tuple[RemainingCardMoveMutation, ...]
    callbacks: tuple[str, ...]
    phases: tuple[str, ...]

    @property
    def moved_guids(self) -> tuple[str, ...]:
        return tuple(target.guid for target in self.rematched)


@dataclass(frozen=True, slots=True)
class RemainingCardMoveExecutionContext:
    hand_limit: int
    lesson_type: str = "ProduceStepLessonType_LessonVocal"
    support_upgrades: SupportInputs = ()
    support_card_searches: Mapping[str, ProduceCardSearchRule] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.hand_limit, bool) or not isinstance(self.hand_limit, int) or self.hand_limit < 0:
            raise RemainingCardMoveError("invalid-hand-limit")
        if not isinstance(self.lesson_type, str) or not self.lesson_type:
            raise RemainingCardMoveError("invalid-lesson-type")


def _json_list(value: object, label: str) -> list[object]:
    try:
        result = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise RemainingCardMoveError("invalid-master-json", label) from error
    if not isinstance(result, list):
        raise RemainingCardMoveError("invalid-master-list", label)
    return result


def _play_effects(row: sqlite3.Row) -> tuple[tuple[str, str, bool, bool], ...]:
    result: list[tuple[str, str, bool, bool]] = []
    for raw in _json_list(row["play_effects_json"], f"{row['id']}#{row['upgrade_count']}"):
        if not isinstance(raw, dict) or set(raw) != {
            "produceExamTriggerId", "produceExamEffectId", "hideIcon", "isOncePlayEffect"
        }:
            raise RemainingCardMoveError("unsupported-play-effect-shape", str(row["id"]))
        result.append((raw["produceExamTriggerId"], raw["produceExamEffectId"], raw["hideIcon"], raw["isOncePlayEffect"]))
    return tuple(result)


def _require_effect_shape(connection: sqlite3.Connection, effect_id: str, *, search_id: str, destination: str, pick: str, count_min: int, count_max: int) -> None:
    row = connection.execute("SELECT raw_json FROM effect WHERE id = ?", (effect_id,)).fetchone()
    if row is None:
        raise RemainingCardMoveError("missing-effect", effect_id)
    raw = json.loads(row[0])
    expected = {
        "effectType": "ProduceExamEffectType_ExamCardMove",
        "effectValue1": 0,
        "effectValue2": 0,
        "effectCount": 0,
        "effectTurn": 0,
        "targetProduceCardId": "",
        "targetUpgradeCount": 0,
        "targetExamEffectType": "ProduceExamEffectType_Unknown",
        "produceCardSearchId": search_id,
        "movePositionType": destination,
        "pickRangeType": pick,
        "pickCountReferenceProduceCardSearchId": "",
        "pickCountType": "ProducePickCountType_Unknown",
        "pickCountMin": count_min,
        "pickCountMax": count_max,
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
        "effectGroupIds": [],
    }
    for key, value in expected.items():
        if raw.get(key) != value:
            raise RemainingCardMoveError("effect-contract-drift", f"{effect_id}:{key}")


def _require_search_shape(search: ProduceCardSearchRule, *, position: str, card_ids: tuple[str, ...] = (), categories: tuple[str, ...] = ()) -> None:
    expected = {
        "card_rarities": (), "produce_card_ids": card_ids, "upgrade_counts": (),
        "plan_type": "ProducePlanType_Unknown", "card_categories": categories,
        "card_status_type": "ProduceCardSearchStatusType_Unknown",
        "order_type": "ProduceCardOrderType_Unknown", "card_position_type": position,
        "card_search_tag": "", "produce_card_random_pool_id": "", "limit_count": 0,
        "stamina_min_max_type": "ConditionMinMaxType_Unknown", "stamina_min": 0,
        "stamina_max": 0, "exam_effect_type": "ProduceExamEffectType_Unknown",
        "effect_group_ids": (), "is_self": False, "produce_card_pool_id": "",
        "cost_type": "ExamCostType_Unknown", "is_customized": False,
    }
    for key, value in expected.items():
        if getattr(search, key) != value:
            raise RemainingCardMoveError("search-contract-drift", f"{search.id}:{key}")


def _row(connection: sqlite3.Connection, card_id: str, upgrade: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM card WHERE id = ? AND upgrade_count = ?", (card_id, upgrade)
    ).fetchone()
    if row is None:
        raise RemainingCardMoveError("missing-card-version", f"{card_id}#{upgrade}")
    if (row["plan_type"], row["category"], row["move_position_type"]) != (
        "ProducePlanType_Plan2", MENTAL_SKILL, "ProduceCardMovePositionType_Lost"
    ):
        raise RemainingCardMoveError("card-contract-drift", f"{card_id}#{upgrade}")
    return row


def load_remaining_card_move_contracts(database: Path = DEFAULT_DATABASE) -> tuple[RemainingCardMoveContract, ...]:
    """Load and strictly validate the six admitted current-Master versions."""

    if not Path(database).is_file():
        raise RemainingCardMoveError("missing-master-database", str(database))
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        searches = {
            SEARCH_NOT_LOST_146: load_produce_card_search(SEARCH_NOT_LOST_146, database),
            SEARCH_MENTAL_DECK_GRAVE: load_produce_card_search(SEARCH_MENTAL_DECK_GRAVE, database),
            SEARCH_DECK_ALL_211: load_produce_card_search(SEARCH_DECK_ALL_211, database),
        }
        if any(value is None for value in searches.values()):
            raise RemainingCardMoveError("missing-search")
        s1 = searches[SEARCH_NOT_LOST_146]
        s2 = searches[SEARCH_MENTAL_DECK_GRAVE]
        s3 = searches[SEARCH_DECK_ALL_211]
        assert s1 is not None and s2 is not None and s3 is not None
        _require_search_shape(s1, position=SEARCH_NOT_LOST, card_ids=("p_card-02-ido-3_146",))
        _require_search_shape(s2, position=SEARCH_DECK_GRAVE, categories=(MENTAL_SKILL,))
        _require_search_shape(s3, position=SEARCH_DECK_ALL, card_ids=("p_card-02-ido-3_211",))
        _require_effect_shape(connection, EFFECT_NOT_LOST_HAND, search_id=SEARCH_NOT_LOST_146, destination=MOVE_HAND, pick=PICK_ALL, count_min=0, count_max=0)
        _require_effect_shape(connection, EFFECT_MENTAL_DECK_GRAVE, search_id=SEARCH_MENTAL_DECK_GRAVE, destination=MOVE_DECK_FIRST, pick=PICK_RANDOM, count_min=1, count_max=1)
        _require_effect_shape(connection, EFFECT_DECK_ALL_211, search_id=SEARCH_DECK_ALL_211, destination=MOVE_DECK_FIRST, pick=PICK_ALL, count_min=0, count_max=0)

        row041 = _row(connection, CARD_041, 0)
        expected041 = (
            ("", "e_effect-exam_review-0010", False, False),
            ("", "e_effect-exam_playable_value_add-01", False, False),
            ("", "e_effect-exam_card_create_id-p_card-02-ido-3_232-0-deck_random-5_5", False, False),
            ("", TIMER_041, False, False),
        )
        if _play_effects(row041) != expected041:
            raise RemainingCardMoveError("card-effect-order-drift", f"{CARD_041}#0")
        timer = connection.execute("SELECT raw_json FROM effect WHERE id = ?", (TIMER_041,)).fetchone()
        if timer is None:
            raise RemainingCardMoveError("missing-timer-parent", TIMER_041)
        timer_raw = json.loads(timer[0])
        for key, value in {
            "effectType": "ProduceExamEffectType_ExamEffectTimer",
            "effectValue1": 1, "effectCount": 1,
            "chainProduceExamEffectId": EFFECT_NOT_LOST_HAND,
        }.items():
            if timer_raw.get(key) != value:
                raise RemainingCardMoveError("timer-contract-drift", key)

        row046 = _row(connection, CARD_046, 0)
        if _play_effects(row046) != (
            ("", "e_effect-exam_playable_value_add-01", False, False),
            ("", STATUS_EFFECT_046, False, False),
        ):
            raise RemainingCardMoveError("card-effect-order-drift", f"{CARD_046}#0")
        status = connection.execute(
            "SELECT produce_exam_trigger_id, produce_exam_effect_ids_json FROM produce_exam_status_enchant WHERE id = ?",
            (STATUS_046,),
        ).fetchone()
        if status is None or status[0] != TRIGGER_046 or _json_list(status[1], STATUS_046) != [EFFECT_MENTAL_DECK_GRAVE]:
            raise RemainingCardMoveError("status-child-contract-drift", STATUS_046)
        trigger = connection.execute(
            "SELECT phase_types_json, effect_types_json FROM produce_exam_trigger WHERE id = ?", (TRIGGER_046,)
        ).fetchone()
        if trigger is None or _json_list(trigger[0], TRIGGER_046) != ["ProduceExamPhaseType_ExamStatusChange"] or _json_list(trigger[1], TRIGGER_046) != ["ProduceExamEffectType_ExamCardPlayAggressive"]:
            raise RemainingCardMoveError("status-trigger-contract-drift", TRIGGER_046)

        result = [
            RemainingCardMoveContract(CARD_041, 0, EFFECT_NOT_LOST_HAND, s1, MOVE_HAND, PICK_ALL, 0, 0, "timer-child", TIMER_041, 0, ("ProduceExamEffectType_ExamEffectTimer",)),
            RemainingCardMoveContract(CARD_046, 0, EFFECT_MENTAL_DECK_GRAVE, s2, MOVE_DECK_FIRST, PICK_RANDOM, 1, 1, "status-trigger-child", STATUS_046, 0),
        ]
        for upgrade in range(4):
            card = _row(connection, CARD_198, upgrade)
            aggressive = "e_effect-exam_card_play_aggressive-0004" if upgrade == 0 else "e_effect-exam_card_play_aggressive-0005"
            if _play_effects(card) != (
                ("", aggressive, False, False),
                ("", EFFECT_DECK_ALL_211, False, False),
                ("", "e_effect-exam_stamina_consumption_add-01", False, False),
                ("", "e_effect-exam_status_enchant_encore-0001-02-inf-enchant-p_card-02-ido-3_198-enc01", False, True),
            ):
                raise RemainingCardMoveError("card-effect-order-drift", f"{CARD_198}#{upgrade}")
            result.append(RemainingCardMoveContract(
                CARD_198, upgrade, EFFECT_DECK_ALL_211, s3, MOVE_DECK_FIRST,
                PICK_ALL, 0, 0, "direct-effect", CARD_198, 1,
                ("ProduceExamEffectType_ExamForcePlayCardSearch", "ProduceExamEffectType_ExamStatusEnchantEncore"),
            ))
    return tuple(result)


def _zones(state: RemainingCardMoveState, position: str) -> tuple[tuple[CardMoveSource, int, tuple[Plan3NativeCard, ...]], ...]:
    if position == SEARCH_NOT_LOST:
        return (("hand", 0, state.hand), ("deck", 1, state.deck), ("grave", 2, state.grave), ("hold", 3, state.hold))
    if position == SEARCH_DECK_GRAVE:
        return (("deck", 0, state.deck), ("grave", 1, state.grave))
    if position == SEARCH_DECK_ALL:
        result: list[tuple[CardMoveSource, int, tuple[Plan3NativeCard, ...]]] = [
            ("hand", 0, state.hand), ("deck", 1, state.deck), ("grave", 2, state.grave),
            ("lost", 3, state.lost), ("hold", 4, state.hold),
        ]
        if state.playing is not None:
            result.append(("playing", 5, (state.playing,)))
        result.extend(("future", 6 + index, deck) for index, deck in enumerate(state.future_decks))
        result.extend(("past", 6 + len(state.future_decks) + index, deck) for index, deck in enumerate(state.past_decks))
        return tuple(result)
    raise RemainingCardMoveError("unsupported-search-position", position)


def _category(card: Plan3NativeCard, database: Path) -> str:
    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute(
            "SELECT category FROM card WHERE id = ? AND upgrade_count = ?",
            (card.card_id, card.effective_upgrade),
        ).fetchall()
    if len(rows) != 1 or not isinstance(rows[0][0], str) or not rows[0][0]:
        raise RemainingCardMoveError("card-category-unresolved", f"{card.card_id}#{card.effective_upgrade}")
    return rows[0][0]


def _matches(search: ProduceCardSearchRule, card: Plan3NativeCard, database: Path) -> bool:
    if search.produce_card_ids and card.card_id not in search.produce_card_ids:
        return False
    if search.card_categories and _category(card, database) not in search.card_categories:
        return False
    return True


def _validate_handoff(contract: RemainingCardMoveContract, handoff: RemainingCardMoveHandoff) -> None:
    if (handoff.kind, handoff.parent_id, handoff.effect_index) != (
        contract.handoff_kind, contract.parent_id, contract.effect_index
    ):
        raise RemainingCardMoveError("invalid-effect-handoff", contract.version_ref)
    expected_phase = {
        "timer-child": "timer-expiry-child",
        "status-trigger-child": "status-change-trigger-child",
        "direct-effect": "ordered-card-play-effect",
    }[contract.handoff_kind]
    if handoff.phase != expected_phase or handoff.queue_dispatch_count != 1:
        raise RemainingCardMoveError("invalid-dispatch-phase", contract.version_ref)
    if contract.handoff_kind == "direct-effect":
        if handoff.play_origin not in {"normal", "forced", "extra"}:
            raise RemainingCardMoveError("invalid-direct-play-origin", handoff.play_origin)
    elif handoff.play_origin != "callback":
        raise RemainingCardMoveError("callback-origin-required", handoff.play_origin)


def capture_remaining_card_move(
    state: RemainingCardMoveState,
    contract: RemainingCardMoveContract,
    handoff: RemainingCardMoveHandoff,
    *,
    database: Path = DEFAULT_DATABASE,
) -> RemainingCardMoveSnapshot:
    """Search in native order, then perform All/Random GUID selection."""

    if contract.version_ref not in FORMAL_AFFECTED_VERSION_REFS:
        raise RemainingCardMoveError("unsupported-card-version", contract.version_ref)
    _validate_handoff(contract, handoff)
    candidates = tuple(
        RemainingCardMoveTarget(card.guid, card, source, group, index)
        for source, group, cards in _zones(state, contract.search.card_position_type)
        for index, card in enumerate(cards)
        if _matches(contract.search, card, database)
    )
    before = state.random_state
    cursor = before
    if contract.pick == PICK_ALL:
        selected = candidates
    elif contract.pick == PICK_RANDOM:
        if contract.count_min < 0 or contract.count_min > contract.count_max or contract.count_max >= INT32_MAX:
            raise RemainingCardMoveError("invalid-random-count")
        count, cursor = next_range(cursor, contract.count_min, contract.count_max + 1)
        keyed: list[tuple[int, int, RemainingCardMoveTarget]] = []
        for index, candidate in enumerate(candidates):
            key, cursor = next_int32(cursor)
            keyed.append((key, index, candidate))
        chosen = {index for _key, index, _candidate in sorted(keyed, key=lambda item: (item[0], item[1]))[:min(count, len(keyed))]}
        selected = tuple(candidate for index, candidate in enumerate(candidates) if index in chosen)
    else:
        raise RemainingCardMoveError("unsupported-pick", contract.pick)
    return RemainingCardMoveSnapshot(contract, candidates, selected, before, cursor, handoff)


def _locate(state: RemainingCardMoveState, guid: str) -> RemainingCardMoveTarget | None:
    for position in (SEARCH_DECK_ALL,):
        for source, group, cards in _zones(state, position):
            for index, card in enumerate(cards):
                if card.guid == guid:
                    return RemainingCardMoveTarget(guid, card, source, group, index)
    return None


def _remove_guids(state: RemainingCardMoveState, guids: set[str]) -> dict[str, object]:
    changes: dict[str, object] = {
        name: tuple(card for card in getattr(state, name) if card.guid not in guids)
        for name in ("hand", "deck", "grave", "lost", "hold")
    }
    changes["playing"] = None if state.playing is not None and state.playing.guid in guids else state.playing
    changes["future_decks"] = tuple(tuple(card for card in deck if card.guid not in guids) for deck in state.future_decks)
    changes["past_decks"] = tuple(tuple(card for card in deck if card.guid not in guids) for deck in state.past_decks)
    return changes


def _mutation_zone(state: RemainingCardMoveState, target: RemainingCardMoveTarget) -> str:
    if target.source == "future":
        return f"future[{target.source_group - 6}]"
    if target.source == "past":
        return f"past[{target.source_group - 6 - len(state.future_decks)}]"
    return target.source


def execute_remaining_card_move_snapshot(
    state: RemainingCardMoveState,
    snapshot: RemainingCardMoveSnapshot,
    context: RemainingCardMoveExecutionContext,
    *,
    database: Path = DEFAULT_DATABASE,
) -> RemainingCardMoveResult:
    """Rematch captured GUIDs and mutate all sources before destination add."""

    contract = snapshot.contract
    if state.random_state != snapshot.random_state_after:
        raise RemainingCardMoveError("rng-state-changed-after-selection")
    rematched: list[RemainingCardMoveTarget] = []
    skipped: list[str] = []
    for captured in snapshot.selected:
        current = _locate(state, captured.guid)
        allowed = {source for source, _group, _cards in _zones(state, contract.search.card_position_type)}
        if current is None or current.source not in allowed or not _matches(contract.search, current.card, database):
            skipped.append(captured.guid)
        else:
            rematched.append(current)
    selected_guids = {target.guid for target in rematched}
    changes = _remove_guids(state, selected_guids)
    removals = sorted(rematched, key=lambda item: (item.source_group, -item.source_index))
    mutations: list[RemainingCardMoveMutation] = [
        RemainingCardMoveMutation(
            "remove", item.guid, _mutation_zone(state, item), item.source_index
        )
        for item in removals
    ]
    mutations.extend(RemainingCardMoveMutation("skip", guid, "", -1, "missing-or-no-longer-matching") for guid in skipped)
    moved = tuple(
        target.card.reset_support_upgrade() if target.source in {"hand", "playing"} else target.card
        for target in rematched
    )
    callbacks: list[str] = []
    cursor = state.random_state
    used_support_ids = state.turn_used_support_ids
    if contract.destination == MOVE_DECK_FIRST:
        if any(card.support_upgrade_ids for card in moved):
            raise RemainingCardMoveError("support-upgrade-to-deck-unproven", moved[0].guid)
        changes["deck"] = (*moved, *changes["deck"])
        for index, card in enumerate(moved):
            mutations.append(RemainingCardMoveMutation("insert", card.guid, "deck", index, "deck-first"))
        if moved:
            callbacks.append("OnPoolAdd:DeckFirst")
    elif contract.destination == MOVE_HAND:
        hand = changes["hand"]
        assert isinstance(hand, tuple)
        capacity = max(0, context.hand_limit - len(hand))
        accepted = moved[:capacity]
        overflow = moved[capacity:]
        if any(card.support_upgrade_ids for card in moved):
            raise RemainingCardMoveError("existing-support-on-hand-add-unproven", moved[0].guid)
        # Native AddCard routes the overflow tail to DeckFirst before HandAdd.
        changes["deck"] = (*overflow, *changes["deck"])
        for index, card in enumerate(overflow):
            mutations.append(RemainingCardMoveMutation("insert", card.guid, "deck", index, "hand-overflow-deck-first"))
        if overflow:
            callbacks.append("OnPoolAdd:DeckFirst:HandOverflow")
        searches = {} if context.support_card_searches is None else context.support_card_searches
        if accepted:
            hand_add = evaluate_native_hand_add_support(
                (NativeHandAddCard(card.guid, card.card_id, card.base_upgrade, card.effective_upgrade) for card in accepted),
                lesson_type=context.lesson_type,
                random_state=cursor,
                support_upgrades=context.support_upgrades,
                support_card_searches=searches,
                used_support_ids=used_support_ids,
            )
            by_guid = {card.guid: card for card in hand_add.cards}
            resolved = tuple(card.install_support_upgrades(by_guid[card.guid].added_support_ids) for card in accepted)
            cursor = hand_add.final_random_state
            used_support_ids = hand_add.used_support_ids
        else:
            resolved = ()
        changes["hand"] = (*hand, *resolved)
        for index, card in enumerate(resolved, start=len(hand)):
            mutations.append(RemainingCardMoveMutation("insert", card.guid, "hand", index, "hand-append"))
        if resolved:
            callbacks.append("OnHandAdd")
    else:
        raise RemainingCardMoveError("unsupported-destination", contract.destination)
    after = replace(state, **changes, random_state=cursor, turn_used_support_ids=used_support_ids)
    phases = (
        f"handoff:{snapshot.handoff.kind}",
        "CardMoveEffectExecutor.GetSearchCardList",
        "pick-guid-snapshot",
        "ExamCardMoveController.MoveCard.rematch",
        "remove-all-sources",
        "add-destination",
        "callbacks",
        "return-to-caller-queue",
    )
    return RemainingCardMoveResult(
        state, after, snapshot, tuple(rematched), tuple(skipped),
        tuple(mutations), tuple(callbacks), phases,
    )


def simulate_remaining_card_move(
    state: RemainingCardMoveState,
    contract: RemainingCardMoveContract,
    handoff: RemainingCardMoveHandoff,
    context: RemainingCardMoveExecutionContext,
    *,
    database: Path = DEFAULT_DATABASE,
) -> RemainingCardMoveResult:
    """Synchronous native leaf: capture/select, rematch, remove, add, return."""

    snapshot = capture_remaining_card_move(state, contract, handoff, database=database)
    selected_state = replace(state, random_state=snapshot.random_state_after)
    return execute_remaining_card_move_snapshot(selected_state, snapshot, context, database=database)
