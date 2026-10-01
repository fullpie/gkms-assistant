"""Exact standalone Plan2/Common runtime for search-based card stamina cost.

The plain zero-cost, permanent DeckAll shape is admitted structurally, with
the Master-supplied positive use count and original effect identity. The card and zone boundary
is :class:`Plan3NativeCard`/:class:`Plan3NativeState`; search rows are the
shared :mod:`card_search` primitive and the final resource split is the shared
``native_exam_formula.split_stamina_payment`` primitive.  This module does
not import Plan2State or the Plan2 core runtime.

The native sequence is deliberately represented in two separate operations:

* the effect executor installs (or merges) a count-limited status; and
* the card cost hook asks that status for an override before evaluating the
  card's own stamina getter.

Unknown effect/search shapes are typed unresolved results rather than broad
matches.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING

from .card_search import ProduceCardSearchRule, load_produce_card_search
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import StaminaPayment, split_stamina_payment

if TYPE_CHECKING:
    from .plan3_native_state import Plan3NativeCard, Plan3NativeState


EFFECT_TYPE = "ProduceExamEffectType_ExamSearchPlayCardStaminaConsumptionChange"
EFFECT_ID = (
    "e_effect-exam_search_play_card_stamina_consumption_change-01-inf-"
    "p_card_search-deck_all-all-0_0"
)
TWO_USE_EFFECT_ID = (
    "e_effect-exam_search_play_card_stamina_consumption_change-02-inf-"
    "p_card_search-deck_all-all-0_0"
)
# Known current Master examples, not the structural resolver's admission gate.
SUPPORTED_EFFECT_IDS = (EFFECT_ID, TWO_USE_EFFECT_ID)
SEARCH_ID = "p_card_search-deck_all"
CARD_POSITION_DECK_ALL = "ProduceCardPositionType_DeckAll"
DIRECT_CARD_ID = "p_card-00-sup-3_158"
DIRECT_UPGRADES = (0, 1, 2, 3)
DIRECT_CARD_VERSIONS = tuple((DIRECT_CARD_ID, upgrade) for upgrade in DIRECT_UPGRADES)

APPLICATION_TIMING = "effect-execute"
USE_TIMING = "card-stamina-cost-resolution"
PAYMENT_ORDER = (
    "search-override-before-card-get-stamina-cost-before-damage-stamina"
)
DURATION_INFINITE = -1
STATUS_EFFECT_TYPE_VALUE = 59

NATIVE_EXECUTOR_TYPE = (
    "Campus.InGame.Exam.SearchPlayCardStaminaConsumptionChangeEffectExecutor"
)
NATIVE_CONSTRUCTOR_VA = 0x7E8C0C4
NATIVE_EXECUTE_VA = 0x7E8C30C
NATIVE_TRY_ADD_STATUS_VA = 0x7E9BA40
NATIVE_USE_STATUS_VA = 0x7E9BD40
NATIVE_CONSUME_CARD_COST_VA = 0x7ED040C
NATIVE_GET_STAMINA_COST_VA = 0x808F588
NATIVE_DAMAGE_STAMINA_VA = 0x7E5F288

RESOLUTION_READY = "ready"
RESOLUTION_UNRESOLVED = "unresolved"

_NEUTRAL_EFFECT_FIELDS: Mapping[str, object] = {
    "effectValue1": 0,
    "effectValue2": 0,
    "effectTurn": DURATION_INFINITE,
    "targetProduceCardId": "",
    "targetUpgradeCount": 0,
    "targetExamEffectType": "ProduceExamEffectType_Unknown",
    "movePositionType": "ProduceCardMovePositionType_Unknown",
    "pickRangeType": "ProducePickRangeType_All",
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
    "effectGroupIds": [],
}


def _text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{label} must be non-empty text")
    return value


def _integer(value: object, label: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a string array")
    result = tuple(_text(item, f"{label} item") for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} must not contain duplicates")
    return result


def _integer_tuple(value: object, label: str) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be an integer array")
    result = tuple(_integer(item, f"{label} item", minimum=0) for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} must not contain duplicates")
    return result


@dataclass(frozen=True, slots=True)
class SearchPlayCardStaminaGap:
    code: str
    effect_id: str
    detail: str = ""

    def __post_init__(self) -> None:
        _text(self.code, "gap.code")
        _text(self.effect_id, "gap.effect_id")
        _text(self.detail, "gap.detail", allow_empty=True)

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "effect_id": self.effect_id, "detail": self.detail}


class SearchPlayCardStaminaUnresolvedError(RuntimeError):
    def __init__(self, resolution: "SearchPlayCardStaminaResolution") -> None:
        self.resolution = resolution
        codes = ",".join(gap.code for gap in resolution.gaps)
        super().__init__(f"search-play-card stamina effect is unresolved: {codes}")


def _unresolved(
    effect_id: str, code: str, detail: str = ""
) -> "SearchPlayCardStaminaResolution":
    return SearchPlayCardStaminaResolution(
        effect_id=effect_id,
        gaps=(SearchPlayCardStaminaGap(code, effect_id, detail),),
    )


def _search_shape_mismatch(
    rule: ProduceCardSearchRule,
) -> str | None:
    expected: tuple[tuple[str, object], ...] = (
        ("card_rarities", ()),
        ("produce_card_ids", ()),
        ("upgrade_counts", ()),
        ("plan_type", "ProducePlanType_Unknown"),
        ("card_categories", ()),
        ("card_status_type", "ProduceCardSearchStatusType_Unknown"),
        ("order_type", "ProduceCardOrderType_Unknown"),
        ("card_position_type", CARD_POSITION_DECK_ALL),
        ("card_search_tag", ""),
        ("produce_card_random_pool_id", ""),
        ("limit_count", 0),
        ("stamina_min_max_type", "ConditionMinMaxType_Unknown"),
        ("stamina_min", 0),
        ("stamina_max", 0),
        ("exam_effect_type", "ProduceExamEffectType_Unknown"),
        ("effect_group_ids", ()),
        ("is_self", False),
        ("produce_card_pool_id", ""),
        ("cost_type", "ExamCostType_Unknown"),
        ("is_customized", False),
    )
    for field_name, expected_value in expected:
        if getattr(rule, field_name) != expected_value:
            return f"{field_name}: expected {expected_value!r}, got {getattr(rule, field_name)!r}"
    return None


@dataclass(frozen=True, slots=True)
class SearchPlayCardStaminaTarget:
    """The shared ``ProduceCardSearchRule`` projected as a direct predicate."""

    search: ProduceCardSearchRule

    def __post_init__(self) -> None:
        if not isinstance(self.search, ProduceCardSearchRule):
            raise TypeError("target.search must be ProduceCardSearchRule")
        mismatch = _search_shape_mismatch(self.search)
        if mismatch is not None:
            raise ValueError(f"target search is outside the exact DeckAll shape: {mismatch}")

    @property
    def search_id(self) -> str:
        return self.search.id

    @property
    def mode(self) -> str:
        return "all"

    def matches(self, card: "Plan3NativeCard") -> bool:
        from .plan3_native_state import Plan3NativeCard

        if not isinstance(card, Plan3NativeCard):
            raise TypeError("card must be Plan3NativeCard")
        # DeckAll is the native search predicate used with the already-bound
        # playing ExamCardData.  It is not a restriction to the current Deck
        # collection and the exact row has no ID/upgrade filters.
        return not self.search.produce_card_ids and not self.search.upgrade_counts

    def to_dict(self) -> dict[str, object]:
        return {
            "search_id": self.search.id,
            "card_position_type": self.search.card_position_type,
            "mode": self.mode,
            "card_ids": list(self.search.produce_card_ids),
            "upgrade_counts": list(self.search.upgrade_counts),
        }


@dataclass(frozen=True, slots=True)
class SearchPlayCardStaminaContract:
    effect_id: str
    stamina_cost: int
    activation_count: int
    duration_turns: int
    target: SearchPlayCardStaminaTarget
    from_effect_id: str = EFFECT_ID
    effect_type: str = EFFECT_TYPE
    application_timing: str = APPLICATION_TIMING
    use_timing: str = USE_TIMING
    payment_order: str = PAYMENT_ORDER
    executor_type: str = NATIVE_EXECUTOR_TYPE

    def __post_init__(self) -> None:
        _text(self.effect_id, "contract.effect_id")
        _integer(self.stamina_cost, "contract.stamina_cost", minimum=0)
        _integer(self.activation_count, "contract.activation_count", minimum=1)
        _integer(self.duration_turns, "contract.duration_turns")
        if not isinstance(self.target, SearchPlayCardStaminaTarget):
            raise TypeError("contract.target must be SearchPlayCardStaminaTarget")
        if (
            self.from_effect_id != self.effect_id
            or self.effect_type != EFFECT_TYPE
            or self.stamina_cost != 0
            or self.duration_turns != DURATION_INFINITE
            or self.application_timing != APPLICATION_TIMING
            or self.use_timing != USE_TIMING
            or self.payment_order != PAYMENT_ORDER
            or self.executor_type != NATIVE_EXECUTOR_TYPE
        ):
            raise ValueError("contract is outside the exact native/Master shape")

    @property
    def infinite(self) -> bool:
        return self.duration_turns == DURATION_INFINITE

    def to_dict(self) -> dict[str, object]:
        return {
            "effect_id": self.effect_id,
            "from_effect_id": self.from_effect_id,
            "effect_type": self.effect_type,
            "stamina_cost": self.stamina_cost,
            "activation_count": self.activation_count,
            "duration_turns": self.duration_turns,
            "infinite": self.infinite,
            "target": self.target.to_dict(),
            "application_timing": self.application_timing,
            "use_timing": self.use_timing,
            "payment_order": self.payment_order,
            "executor_type": self.executor_type,
        }


@dataclass(frozen=True, slots=True)
class SearchPlayCardStaminaResolution:
    effect_id: str
    contract: SearchPlayCardStaminaContract | None = None
    gaps: tuple[SearchPlayCardStaminaGap, ...] = ()

    def __post_init__(self) -> None:
        _text(self.effect_id, "resolution.effect_id")
        gaps = tuple(self.gaps)
        if not all(isinstance(gap, SearchPlayCardStaminaGap) for gap in gaps):
            raise TypeError("resolution.gaps must contain SearchPlayCardStaminaGap")
        if (self.contract is None) == (not gaps):
            raise ValueError("resolution must contain exactly one of contract or gaps")
        if self.contract is not None and self.contract.effect_id != self.effect_id:
            raise ValueError("resolution effect_id does not match contract")
        if any(gap.effect_id != self.effect_id for gap in gaps):
            raise ValueError("resolution effect_id does not match gap")
        object.__setattr__(self, "gaps", gaps)

    @property
    def state(self) -> str:
        return RESOLUTION_READY if self.contract is not None else RESOLUTION_UNRESOLVED

    @property
    def executable(self) -> bool:
        return self.contract is not None

    def require_ready(self) -> SearchPlayCardStaminaContract:
        if self.contract is None:
            raise SearchPlayCardStaminaUnresolvedError(self)
        return self.contract


def resolve_plan2_search_play_card_stamina_consumption_change_from_rows(
    raw_effect: Mapping[str, object],
    search: ProduceCardSearchRule,
) -> SearchPlayCardStaminaResolution:
    """Resolve one already-loaded raw Master effect and exact search row."""

    if not isinstance(raw_effect, Mapping):
        raise TypeError("raw_effect must be a mapping")
    raw_id = raw_effect.get("id")
    effect_id = raw_id if isinstance(raw_id, str) and raw_id else "<invalid-effect-id>"
    if effect_id == "<invalid-effect-id>":
        return _unresolved(effect_id, "invalid-effect-identity")
    if type(raw_effect.get("effectCount")) is not int or not 1 <= raw_effect["effectCount"] <= 2147483647:
        return _unresolved(effect_id, "invalid-effect-use-count")

    expected_fields = {
        "id": effect_id,
        "effectType": EFFECT_TYPE,
        "effectCount": raw_effect["effectCount"],
        "produceCardSearchId": getattr(search, "id", None),
        **_NEUTRAL_EFFECT_FIELDS,
    }
    integer_fields = (
        "effectValue1",
        "effectValue2",
        "effectCount",
        "effectTurn",
        "targetUpgradeCount",
        "pickCountMin",
        "pickCountMax",
        "pickCountMin2",
        "pickCountMax2",
    )
    string_fields = (
        "id",
        "effectType",
        "targetProduceCardId",
        "targetExamEffectType",
        "produceCardSearchId",
        "movePositionType",
        "pickRangeType",
        "pickCountReferenceProduceCardSearchId",
        "pickCountType",
        "produceCardSearchId2",
        "pickRangeType2",
        "pickCountReferenceProduceCardSearchId2",
        "pickCountType2",
        "chainProduceExamEffectId",
        "produceExamStatusEnchantId",
        "produceCardStatusEnchantId",
    )
    list_fields = (
        "chainProduceExamEffectIds",
        "produceCardGrowEffectIds",
        "effectGroupIds",
    )
    type_mismatches = tuple(
        key
        for key in integer_fields
        if not isinstance(raw_effect.get(key), int)
        or isinstance(raw_effect.get(key), bool)
    ) + tuple(
        key for key in string_fields if not isinstance(raw_effect.get(key), str)
    ) + tuple(
        key for key in list_fields if not isinstance(raw_effect.get(key), list)
    )
    value_mismatches = tuple(
        key
        for key, expected in expected_fields.items()
        if raw_effect.get(key) != expected
    )
    mismatches = tuple(dict.fromkeys((*type_mismatches, *value_mismatches)))
    if mismatches:
        return _unresolved(effect_id, "master-shape-mismatch", ",".join(mismatches))
    if not isinstance(search, ProduceCardSearchRule):
        return _unresolved(effect_id, "card-search-shape-unresolved", "not a search row")
    search_mismatch = _search_shape_mismatch(search)
    if search_mismatch is not None:
        return _unresolved(
            effect_id,
            "card-search-shape-unresolved",
            search_mismatch,
        )
    target = SearchPlayCardStaminaTarget(search)
    return SearchPlayCardStaminaResolution(
        effect_id=effect_id,
        contract=SearchPlayCardStaminaContract(
            effect_id=effect_id,
            stamina_cost=int(raw_effect["effectValue1"]),
            activation_count=int(raw_effect["effectCount"]),
            duration_turns=int(raw_effect["effectTurn"]),
            target=target,
            from_effect_id=effect_id,
        ),
    )


def resolve_plan2_search_play_card_stamina_consumption_change(
    effect_id: str,
    database: Path = DEFAULT_DATABASE,
) -> SearchPlayCardStaminaResolution:
    """Read one Master row read-only and resolve only the exact supported row."""

    _text(effect_id, "effect_id")
    database = Path(database)
    if not database.is_file():
        return _unresolved(effect_id, "master-database-missing", str(database))
    try:
        uri = f"file:{database.resolve().as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            row = connection.execute(
                "SELECT effect_type, value1, value2, effect_count, effect_turn, "
                "status_enchant_id, chain_effect_id, raw_json FROM effect WHERE id = ?",
                (effect_id,),
            ).fetchone()
        if row is None:
            return _unresolved(effect_id, "effect-not-found")
        raw = json.loads(str(row[7]))
        if not isinstance(raw, Mapping):
            return _unresolved(effect_id, "master-row-invalid", "raw_json is not an object")
        if (
            row[0] != raw.get("effectType")
            or row[1] != raw.get("effectValue1")
            or row[2] != raw.get("effectValue2")
            or row[3] != raw.get("effectCount")
            or row[4] != raw.get("effectTurn")
            or row[5] != raw.get("produceExamStatusEnchantId")
            or row[6] != raw.get("chainProduceExamEffectId")
        ):
            return _unresolved(effect_id, "master-column-raw-mismatch")
        search_id = raw.get("produceCardSearchId")
        if not isinstance(search_id, str) or not search_id:
            return _unresolved(effect_id, "card-search-id-invalid")
        try:
            search = load_produce_card_search(search_id, database)
        except (KeyError, ValueError, sqlite3.Error) as error:
            return _unresolved(effect_id, "card-search-load-failed", str(error))
        return resolve_plan2_search_play_card_stamina_consumption_change_from_rows(
            raw, search
        )
    except (json.JSONDecodeError, sqlite3.Error, OSError) as error:
        return _unresolved(effect_id, "master-read-failed", str(error))


@dataclass(frozen=True, slots=True)
class SearchPlayCardStaminaStatus:
    status_uid: int
    stamina_cost: int
    remaining_uses: int
    duration_turns: int
    target: SearchPlayCardStaminaTarget
    from_effect_id: str

    def __post_init__(self) -> None:
        _integer(self.status_uid, "status_uid", minimum=1)
        _integer(self.stamina_cost, "stamina_cost", minimum=0)
        _integer(self.remaining_uses, "remaining_uses", minimum=1)
        _integer(self.duration_turns, "duration_turns")
        if self.stamina_cost != 0:
            raise ValueError("only the exact zero-cost status is supported")
        if self.duration_turns != DURATION_INFINITE:
            raise ValueError("finite search-stamina duration is unresolved")
        if not isinstance(self.target, SearchPlayCardStaminaTarget):
            raise TypeError("status.target must be SearchPlayCardStaminaTarget")
        _text(self.from_effect_id, "status.from_effect_id")

    @property
    def merge_key(self) -> tuple[int, int, str]:
        # Native TryAdd's closure keys the existing status by turn, value, and
        # the resolved search.  fromEffectId is lineage, not a merge key.
        return self.duration_turns, self.stamina_cost, self.target.search_id

    @property
    def is_count_limited(self) -> bool:
        return True

    @property
    def is_permanent(self) -> bool:
        return self.duration_turns == DURATION_INFINITE

    def to_dict(self) -> dict[str, object]:
        return {
            "status_uid": self.status_uid,
            "stamina_cost": self.stamina_cost,
            "remaining_uses": self.remaining_uses,
            "duration_turns": self.duration_turns,
            "target": self.target.to_dict(),
            "from_effect_id": self.from_effect_id,
            "is_count_limited": self.is_count_limited,
            "is_permanent": self.is_permanent,
        }


def restore_search_play_card_stamina_status(data: Mapping[str, object], *, database: Path = DEFAULT_DATABASE) -> SearchPlayCardStaminaStatus:
    """One shared parser for the actual native zero-cost status payload."""
    fields = {"_count", "_fromExamEffectId", "_isPassingTurnStart", "_isTurnLimited", "_searchId", "_turn", "_uid", "_value"}
    if set(data) != fields or data.get("_isPassingTurnStart") is not False or data.get("_isTurnLimited") is not False:
        raise ValueError("search-stamina status field/lifetime contract differs")
    effect_id = data.get("_fromExamEffectId")
    if not isinstance(effect_id, str) or not effect_id:
        raise ValueError("search-stamina source effect is absent")
    resolution = resolve_plan2_search_play_card_stamina_consumption_change(effect_id, database=database)
    if resolution.contract is None:
        raise ValueError("search-stamina source effect is unsupported")
    contract = resolution.contract
    if (type(data.get("_turn")) is not int or data["_turn"] != contract.duration_turns
            or type(data.get("_value")) is not int or data["_value"] != contract.stamina_cost
            or data.get("_searchId") != contract.target.search_id):
        raise ValueError("search-stamina source/search/cost does not match its Master contract")
    return SearchPlayCardStaminaStatus(status_uid=_integer(data["_uid"], "search-stamina UID", minimum=1),
        stamina_cost=data["_value"], remaining_uses=_integer(data["_count"], "search-stamina count", minimum=1),
        duration_turns=data["_turn"], target=contract.target, from_effect_id=effect_id)


@dataclass(frozen=True, slots=True)
class SearchPlayCardStaminaRuntime:
    statuses: tuple[SearchPlayCardStaminaStatus, ...] = ()
    next_status_uid: int = 1

    def __post_init__(self) -> None:
        statuses = tuple(self.statuses)
        if not all(isinstance(status, SearchPlayCardStaminaStatus) for status in statuses):
            raise TypeError("statuses must contain SearchPlayCardStaminaStatus")
        uids = tuple(status.status_uid for status in statuses)
        if len(set(uids)) != len(uids):
            raise ValueError("status UIDs must be unique")
        merge_keys = tuple(status.merge_key for status in statuses)
        if len(set(merge_keys)) != len(merge_keys):
            raise ValueError("native status collection cannot retain duplicate merge keys")
        _integer(self.next_status_uid, "next_status_uid", minimum=1)
        if uids and self.next_status_uid <= max(uids):
            raise ValueError("next_status_uid must exceed retained status UIDs")
        object.__setattr__(self, "statuses", statuses)

    def to_dict(self) -> dict[str, object]:
        return {
            "statuses": [status.to_dict() for status in self.statuses],
            "next_status_uid": self.next_status_uid,
        }


@dataclass(frozen=True, slots=True)
class SearchPlayCardStaminaCardIdentity:
    """GUID/location audit projection; the card itself remains Plan3NativeCard."""

    guid: str
    card_id: str
    effective_upgrade: int
    zone: str
    zone_index: int

    def __post_init__(self) -> None:
        _text(self.guid, "identity.guid")
        _text(self.card_id, "identity.card_id")
        _integer(self.effective_upgrade, "identity.effective_upgrade", minimum=0)
        if self.zone not in {"hand", "deck", "grave", "lost", "hold", "playing"}:
            raise ValueError("identity.zone is invalid")
        _integer(self.zone_index, "identity.zone_index", minimum=0)
        if self.zone == "playing" and self.zone_index != 0:
            raise ValueError("playing card index must be zero")

    def to_dict(self) -> dict[str, object]:
        return {
            "guid": self.guid,
            "card_id": self.card_id,
            "effective_upgrade": self.effective_upgrade,
            "zone": self.zone,
            "zone_index": self.zone_index,
        }


def _card_identities(
    state: "Plan3NativeState",
    playing_card: "Plan3NativeCard | None",
) -> tuple[tuple["Plan3NativeCard", SearchPlayCardStaminaCardIdentity], ...]:
    from .plan3_native_state import Plan3NativeCard, Plan3NativeState

    if not isinstance(state, Plan3NativeState):
        raise TypeError("state must be Plan3NativeState")
    values: list[tuple[Plan3NativeCard, SearchPlayCardStaminaCardIdentity]] = []
    for zone in ("hand", "deck", "grave", "lost", "hold"):
        for index, card in enumerate(getattr(state, zone)):
            values.append(
                (
                    card,
                    SearchPlayCardStaminaCardIdentity(
                        card.guid,
                        card.card_id,
                        card.effective_upgrade,
                        zone,
                        index,
                    ),
                )
            )
    if playing_card is not None:
        if not isinstance(playing_card, Plan3NativeCard):
            raise TypeError("playing_card must be Plan3NativeCard or None")
        if playing_card.guid in {card.guid for card in state.all_cards}:
            raise ValueError("playing card GUID duplicates an ordinary zone")
        values.append(
            (
                playing_card,
                SearchPlayCardStaminaCardIdentity(
                    playing_card.guid,
                    playing_card.card_id,
                    playing_card.effective_upgrade,
                    "playing",
                    0,
                ),
            )
        )
    return tuple(values)


@dataclass(frozen=True, slots=True)
class SearchPlayCardStaminaApplication:
    effect_id: str
    runtime_before: SearchPlayCardStaminaRuntime
    runtime_after: SearchPlayCardStaminaRuntime
    operation: str
    status_uid: int | None
    prior_remaining_uses: int
    next_remaining_uses: int
    target_identities: tuple[SearchPlayCardStaminaCardIdentity, ...] = ()
    status_add_blocked: bool = False
    effect_difference_appended: bool = False
    difference_status_effect_type: int | None = None
    difference_is_consume: bool = False
    value_changed_callback_invoked: bool = False
    application_timing: str = APPLICATION_TIMING

    def __post_init__(self) -> None:
        _text(self.effect_id, "application.effect_id")
        if not isinstance(self.runtime_before, SearchPlayCardStaminaRuntime) or not isinstance(
            self.runtime_after, SearchPlayCardStaminaRuntime
        ):
            raise TypeError("application runtimes must be SearchPlayCardStaminaRuntime")
        if self.operation not in {"installed", "stacked", "rejected"}:
            raise ValueError("application.operation is invalid")
        if not isinstance(self.status_add_blocked, bool):
            raise TypeError("status_add_blocked must be boolean")
        identities = tuple(self.target_identities)
        if not all(isinstance(item, SearchPlayCardStaminaCardIdentity) for item in identities):
            raise TypeError("target_identities contains an invalid value")
        if len({item.guid for item in identities}) != len(identities):
            raise ValueError("target identities must be unique per GUID")
        object.__setattr__(self, "target_identities", identities)
        if self.operation == "rejected":
            if not self.status_add_blocked or self.runtime_after is not self.runtime_before:
                raise ValueError("rejected application must preserve the runtime")
            if self.status_uid is not None or identities:
                raise ValueError("rejected application cannot install a status")
            if self.effect_difference_appended or self.value_changed_callback_invoked:
                raise ValueError("blocked status addition cannot emit difference/callback")
        else:
            if self.status_add_blocked or self.status_uid is None:
                raise ValueError("successful application must retain its status UID")
            if self.difference_status_effect_type != STATUS_EFFECT_TYPE_VALUE:
                raise ValueError("successful application must record status type 59")
            if not self.effect_difference_appended or self.difference_is_consume:
                raise ValueError("successful application must append a non-consume difference")
        if self.application_timing != APPLICATION_TIMING:
            raise ValueError("application timing is outside the native path")

    @property
    def target_guids(self) -> tuple[str, ...]:
        return tuple(item.guid for item in self.target_identities)

    def to_dict(self) -> dict[str, object]:
        return {
            "effect_id": self.effect_id,
            "runtime_before": self.runtime_before.to_dict(),
            "runtime_after": self.runtime_after.to_dict(),
            "operation": self.operation,
            "status_uid": self.status_uid,
            "prior_remaining_uses": self.prior_remaining_uses,
            "next_remaining_uses": self.next_remaining_uses,
            "target_identities": [item.to_dict() for item in self.target_identities],
            "target_guids": list(self.target_guids),
            "status_add_blocked": self.status_add_blocked,
            "effect_difference_appended": self.effect_difference_appended,
            "difference_status_effect_type": self.difference_status_effect_type,
            "difference_is_consume": self.difference_is_consume,
            "value_changed_callback_invoked": self.value_changed_callback_invoked,
            "application_timing": self.application_timing,
        }


def install_search_play_card_stamina_status(
    runtime: SearchPlayCardStaminaRuntime,
    contract: SearchPlayCardStaminaContract,
) -> tuple[SearchPlayCardStaminaRuntime, SearchPlayCardStaminaStatus, int, bool]:
    """Pure native merge/allocation owner; no card inventory is fabricated.

    Returns the next runtime, selected status, previous use count and whether
    a new status was allocated. Inventory/difference auditing remains with
    the caller that actually has ordered zones.
    """
    if not isinstance(runtime, SearchPlayCardStaminaRuntime) or not isinstance(contract, SearchPlayCardStaminaContract):
        raise TypeError("typed search-stamina runtime and contract required")
    key = (contract.duration_turns, contract.stamina_cost, contract.target.search_id)
    index = next((i for i, status in enumerate(runtime.statuses) if status.merge_key == key), None)
    if index is None:
        status = SearchPlayCardStaminaStatus(runtime.next_status_uid, contract.stamina_cost,
            contract.activation_count, contract.duration_turns, contract.target, contract.from_effect_id)
        return SearchPlayCardStaminaRuntime((*runtime.statuses, status), runtime.next_status_uid + 1), status, 0, True
    previous = runtime.statuses[index]
    statuses = list(runtime.statuses)
    statuses[index] = replace(previous, remaining_uses=previous.remaining_uses + contract.activation_count)
    return SearchPlayCardStaminaRuntime(tuple(statuses), runtime.next_status_uid), statuses[index], previous.remaining_uses, False


def apply_plan2_search_play_card_stamina_change(
    runtime: SearchPlayCardStaminaRuntime,
    contract: SearchPlayCardStaminaContract,
    state: "Plan3NativeState",
    *,
    playing_card: "Plan3NativeCard | None" = None,
    status_add_blocked: bool = False,
) -> SearchPlayCardStaminaApplication:
    """Model native TryAdd + executor difference, without mutating card state."""

    if not isinstance(runtime, SearchPlayCardStaminaRuntime):
        raise TypeError("runtime must be SearchPlayCardStaminaRuntime")
    if not isinstance(contract, SearchPlayCardStaminaContract):
        raise TypeError("contract must be SearchPlayCardStaminaContract")
    if not isinstance(status_add_blocked, bool):
        raise TypeError("status_add_blocked must be boolean")
    if status_add_blocked:
        return SearchPlayCardStaminaApplication(
            effect_id=contract.effect_id,
            runtime_before=runtime,
            runtime_after=runtime,
            operation="rejected",
            status_uid=None,
            prior_remaining_uses=0,
            next_remaining_uses=0,
            target_identities=(),
            status_add_blocked=True,
        )
    identities = tuple(
        identity
        for card, identity in _card_identities(state, playing_card)
        if contract.target.matches(card)
    )

    after, status, prior, callback = install_search_play_card_stamina_status(runtime, contract)
    operation = "installed" if callback else "stacked"
    return SearchPlayCardStaminaApplication(
        effect_id=contract.effect_id,
        runtime_before=runtime,
        runtime_after=after,
        operation=operation,
        status_uid=status.status_uid,
        prior_remaining_uses=prior,
        next_remaining_uses=status.remaining_uses,
        target_identities=identities,
        effect_difference_appended=True,
        difference_status_effect_type=STATUS_EFFECT_TYPE_VALUE,
        difference_is_consume=False,
        value_changed_callback_invoked=callback,
    )


@dataclass(frozen=True, slots=True)
class SearchPlayCardStaminaTurnAdvance:
    runtime_before: SearchPlayCardStaminaRuntime
    runtime_after: SearchPlayCardStaminaRuntime
    expired_status_uids: tuple[int, ...] = ()
    value_changed_callback_invoked: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_before, SearchPlayCardStaminaRuntime) or not isinstance(
            self.runtime_after, SearchPlayCardStaminaRuntime
        ):
            raise TypeError("turn runtimes must be SearchPlayCardStaminaRuntime")
        if self.runtime_after is not self.runtime_before:
            raise ValueError("permanent status turn advance must preserve runtime")
        if tuple(self.expired_status_uids):
            raise ValueError("permanent status cannot expire by turn")


def advance_plan2_search_play_card_stamina_turn(
    runtime: SearchPlayCardStaminaRuntime,
    turns: int = 1,
) -> SearchPlayCardStaminaTurnAdvance:
    """Advance the timer boundary; the exact ``-1`` status never expires."""

    if not isinstance(runtime, SearchPlayCardStaminaRuntime):
        raise TypeError("runtime must be SearchPlayCardStaminaRuntime")
    _integer(turns, "turns", minimum=0)
    return SearchPlayCardStaminaTurnAdvance(runtime, runtime)


@dataclass(frozen=True, slots=True)
class SearchPlayCardStaminaPayment:
    runtime_before: SearchPlayCardStaminaRuntime
    runtime_after: SearchPlayCardStaminaRuntime
    card_guid: str
    card_id: str
    effective_upgrade: int
    selected_stamina_cost: int
    cost_source: str
    matched_status_uid: int | None
    matched_from_effect_id: str | None
    matched_search_id: str | None
    card_cost_evaluated: bool
    card_stamina_cost: int | None
    simulated: bool
    consumed_use: bool
    recently_used_marked: bool
    status_removed: bool
    value_changed_callback_invoked: bool
    use_timing: str = USE_TIMING
    payment_order: str = PAYMENT_ORDER
    downstream_stage: str = "DamageStamina"

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_before, SearchPlayCardStaminaRuntime) or not isinstance(
            self.runtime_after, SearchPlayCardStaminaRuntime
        ):
            raise TypeError("payment runtimes must be SearchPlayCardStaminaRuntime")
        _text(self.card_guid, "payment.card_guid")
        _text(self.card_id, "payment.card_id")
        _integer(self.effective_upgrade, "payment.effective_upgrade", minimum=0)
        _integer(self.selected_stamina_cost, "payment.selected_stamina_cost", minimum=0)
        if self.cost_source not in {"search-override", "card-get-stamina-cost"}:
            raise ValueError("payment.cost_source is invalid")
        if not all(
            isinstance(value, bool)
            for value in (
                self.card_cost_evaluated,
                self.simulated,
                self.consumed_use,
                self.recently_used_marked,
                self.status_removed,
                self.value_changed_callback_invoked,
            )
        ):
            raise TypeError("payment flags must be booleans")
        if self.use_timing != USE_TIMING or self.payment_order != PAYMENT_ORDER:
            raise ValueError("payment timing/order is outside the native path")
        if self.downstream_stage != "DamageStamina":
            raise ValueError("payment downstream stage is invalid")
        matched = (
            self.matched_status_uid,
            self.matched_from_effect_id,
            self.matched_search_id,
        )
        if self.cost_source == "search-override":
            if any(value is None for value in matched):
                raise ValueError("override payment must retain matched status identity")
            if self.card_cost_evaluated or self.card_stamina_cost is not None:
                raise ValueError("card getter must be bypassed after an override")
            if self.consumed_use == self.simulated:
                raise ValueError("only real override use consumes one count")
            if not self.value_changed_callback_invoked:
                raise ValueError("successful native Use must invoke value callback")
            if self.recently_used_marked != (not self.simulated):
                raise ValueError("recently-used list follows isSimulate")
        else:
            if any(value is not None for value in matched):
                raise ValueError("fallback payment cannot retain a status identity")
            if self.consumed_use or self.recently_used_marked or self.status_removed:
                raise ValueError("fallback payment cannot consume a status")
            if not self.card_cost_evaluated or self.card_stamina_cost is None:
                raise ValueError("fallback payment must evaluate the card getter")
            if self.selected_stamina_cost != self.card_stamina_cost:
                raise ValueError("fallback selected cost is inconsistent")
            if self.value_changed_callback_invoked:
                raise ValueError("fallback path has no status callback")

    def to_dict(self) -> dict[str, object]:
        return {
            "runtime_before": self.runtime_before.to_dict(),
            "runtime_after": self.runtime_after.to_dict(),
            "card_guid": self.card_guid,
            "card_id": self.card_id,
            "effective_upgrade": self.effective_upgrade,
            "selected_stamina_cost": self.selected_stamina_cost,
            "cost_source": self.cost_source,
            "matched_status_uid": self.matched_status_uid,
            "matched_from_effect_id": self.matched_from_effect_id,
            "matched_search_id": self.matched_search_id,
            "card_cost_evaluated": self.card_cost_evaluated,
            "card_stamina_cost": self.card_stamina_cost,
            "simulated": self.simulated,
            "consumed_use": self.consumed_use,
            "recently_used_marked": self.recently_used_marked,
            "status_removed": self.status_removed,
            "value_changed_callback_invoked": self.value_changed_callback_invoked,
            "use_timing": self.use_timing,
            "payment_order": self.payment_order,
            "downstream_stage": self.downstream_stage,
        }


def _native_card(card: object) -> "Plan3NativeCard":
    from .plan3_native_state import Plan3NativeCard

    if not isinstance(card, Plan3NativeCard):
        raise TypeError("card must be Plan3NativeCard")
    return card


def _clamp_native_cost(value: object, label: str) -> int:
    # The card getter's native boundary has a max(0, result) floor.  Keep the
    # same floor for an explicit adapter input while retaining strict typing.
    value = _integer(value, label)
    return max(0, value)


def resolve_plan2_search_play_card_stamina_payment(
    runtime: SearchPlayCardStaminaRuntime,
    card: "Plan3NativeCard",
    card_stamina_cost: int | Callable[[], int],
    *,
    simulate: bool = False,
) -> SearchPlayCardStaminaPayment:
    """Run the native status lookup before the card stamina getter."""

    if not isinstance(runtime, SearchPlayCardStaminaRuntime):
        raise TypeError("runtime must be SearchPlayCardStaminaRuntime")
    card = _native_card(card)
    if not isinstance(simulate, bool):
        raise TypeError("simulate must be boolean")
    matched_index = next(
        (
            index
            for index, status in enumerate(runtime.statuses)
            if status.target.matches(card)
        ),
        None,
    )
    if matched_index is not None:
        status = runtime.statuses[matched_index]
        if simulate:
            after = runtime
            removed = False
        elif status.remaining_uses == 1:
            after = SearchPlayCardStaminaRuntime(
                runtime.statuses[:matched_index] + runtime.statuses[matched_index + 1 :],
                runtime.next_status_uid,
            )
            removed = True
        else:
            statuses = list(runtime.statuses)
            statuses[matched_index] = replace(
                status,
                remaining_uses=status.remaining_uses - 1,
            )
            after = SearchPlayCardStaminaRuntime(tuple(statuses), runtime.next_status_uid)
            removed = False
        return SearchPlayCardStaminaPayment(
            runtime_before=runtime,
            runtime_after=after,
            card_guid=card.guid,
            card_id=card.card_id,
            effective_upgrade=card.effective_upgrade,
            selected_stamina_cost=max(0, status.stamina_cost),
            cost_source="search-override",
            matched_status_uid=status.status_uid,
            matched_from_effect_id=status.from_effect_id,
            matched_search_id=status.target.search_id,
            card_cost_evaluated=False,
            card_stamina_cost=None,
            simulated=simulate,
            consumed_use=not simulate,
            recently_used_marked=not simulate,
            status_removed=removed,
            value_changed_callback_invoked=True,
        )

    raw_cost = card_stamina_cost() if callable(card_stamina_cost) else card_stamina_cost
    fallback = _clamp_native_cost(raw_cost, "card_stamina_cost")
    return SearchPlayCardStaminaPayment(
        runtime_before=runtime,
        runtime_after=runtime,
        card_guid=card.guid,
        card_id=card.card_id,
        effective_upgrade=card.effective_upgrade,
        selected_stamina_cost=fallback,
        cost_source="card-get-stamina-cost",
        matched_status_uid=None,
        matched_from_effect_id=None,
        matched_search_id=None,
        card_cost_evaluated=True,
        card_stamina_cost=fallback,
        simulated=simulate,
        consumed_use=False,
        recently_used_marked=False,
        status_removed=False,
        value_changed_callback_invoked=False,
    )


def split_plan2_search_play_card_stamina_payment(
    payment: SearchPlayCardStaminaPayment,
    *,
    penetrate: bool,
    current_stamina: int,
    max_stamina: int,
    current_block: int,
) -> StaminaPayment:
    """Delegate the downstream DamageStamina resource split to the shared kernel."""

    if not isinstance(payment, SearchPlayCardStaminaPayment):
        raise TypeError("payment must be SearchPlayCardStaminaPayment")
    return split_stamina_payment(
        payment.selected_stamina_cost,
        penetrate=penetrate,
        current_stamina=current_stamina,
        max_stamina=max_stamina,
        current_block=current_block,
    )


@dataclass(frozen=True, slots=True)
class SearchPlayCardStaminaAffectedVersion:
    card_id: str
    upgrade_count: int
    effect_ids: tuple[str, ...]
    direct: bool

    def __post_init__(self) -> None:
        _text(self.card_id, "affected.card_id")
        _integer(self.upgrade_count, "affected.upgrade_count", minimum=0)
        effects = _string_tuple(self.effect_ids, "affected.effect_ids")
        if effects != (EFFECT_ID,):
            raise ValueError("affected version must reference exactly the target effect")
        if not isinstance(self.direct, bool):
            raise TypeError("affected.direct must be boolean")
        object.__setattr__(self, "effect_ids", effects)

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "upgrade_count": self.upgrade_count,
            "effect_ids": list(self.effect_ids),
            "direct": self.direct,
        }


def load_plan2_search_play_card_stamina_affected_versions(
    database: Path = DEFAULT_DATABASE,
) -> tuple[SearchPlayCardStaminaAffectedVersion, ...]:
    """Revalidate the four direct target card versions from local Master."""

    database = Path(database)
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    results: list[SearchPlayCardStaminaAffectedVersion] = []
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        for card_id, upgrade in DIRECT_CARD_VERSIONS:
            row = connection.execute(
                "SELECT plan_type, play_effects_json FROM card "
                "WHERE id = ? AND upgrade_count = ?",
                (card_id, upgrade),
            ).fetchone()
            if row is None:
                raise ValueError(f"direct card version is missing: {card_id}+{upgrade}")
            if row[0] != "ProducePlanType_Common":
                raise ValueError(f"direct card is not Common: {card_id}+{upgrade}")
            raw_effects = json.loads(str(row[1]))
            if not isinstance(raw_effects, list) or not all(
                isinstance(effect, Mapping)
                and isinstance(effect.get("produceExamEffectId"), str)
                for effect in raw_effects
            ):
                raise ValueError(f"invalid card effect list: {card_id}+{upgrade}")
            effects = tuple(
                str(effect["produceExamEffectId"])
                for effect in raw_effects
                if effect["produceExamEffectId"] == EFFECT_ID
            )
            if effects != (EFFECT_ID,):
                raise ValueError(f"unexpected target linkage: {card_id}+{upgrade}")
            results.append(SearchPlayCardStaminaAffectedVersion(card_id, upgrade, effects, True))
    return tuple(results)


__all__ = [
    "APPLICATION_TIMING",
    "CARD_POSITION_DECK_ALL",
    "DIRECT_CARD_ID",
    "DIRECT_CARD_VERSIONS",
    "DIRECT_UPGRADES",
    "DURATION_INFINITE",
    "EFFECT_ID",
    "EFFECT_TYPE",
    "NATIVE_CONSUME_CARD_COST_VA",
    "NATIVE_DAMAGE_STAMINA_VA",
    "NATIVE_EXECUTE_VA",
    "NATIVE_EXECUTOR_TYPE",
    "NATIVE_GET_STAMINA_COST_VA",
    "NATIVE_TRY_ADD_STATUS_VA",
    "NATIVE_USE_STATUS_VA",
    "PAYMENT_ORDER",
    "RESOLUTION_READY",
    "RESOLUTION_UNRESOLVED",
    "SEARCH_ID",
    "STATUS_EFFECT_TYPE_VALUE",
    "SUPPORTED_EFFECT_IDS",
    "install_search_play_card_stamina_status",
    "USE_TIMING",
    "SearchPlayCardStaminaAffectedVersion",
    "SearchPlayCardStaminaApplication",
    "SearchPlayCardStaminaCardIdentity",
    "SearchPlayCardStaminaContract",
    "SearchPlayCardStaminaGap",
    "SearchPlayCardStaminaPayment",
    "SearchPlayCardStaminaResolution",
    "SearchPlayCardStaminaRuntime",
    "SearchPlayCardStaminaStatus",
    "SearchPlayCardStaminaTarget",
    "SearchPlayCardStaminaTurnAdvance",
    "SearchPlayCardStaminaUnresolvedError",
    "advance_plan2_search_play_card_stamina_turn",
    "apply_plan2_search_play_card_stamina_change",
    "load_plan2_search_play_card_stamina_affected_versions",
    "resolve_plan2_search_play_card_stamina_consumption_change",
    "resolve_plan2_search_play_card_stamina_consumption_change_from_rows",
    "resolve_plan2_search_play_card_stamina_payment",
    "split_plan2_search_play_card_stamina_payment",
]
