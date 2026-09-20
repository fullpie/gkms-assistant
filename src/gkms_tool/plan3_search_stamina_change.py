"""Exact standalone resolver for search-based card stamina overrides.

This module intentionally does not plug the effect into the Plan 3 core.  It
captures only the Android 3.2.3 behavior proven for the three scoped Master
rows: install (or merge) a search status, select that status before asking the
card for its own stamina cost, and spend one status count on a real payment.
Unknown rows and richer searches remain typed unresolved results.
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

if TYPE_CHECKING:
    from .plan3_native_state import Plan3NativeCard, Plan3NativeState


EFFECT_TYPE = (
    "ProduceExamEffectType_ExamSearchPlayCardStaminaConsumptionChange"
)
EFFECT_ID_ALL_ONE = (
    "e_effect-exam_search_play_card_stamina_consumption_change-01-inf-"
    "p_card_search-deck_all-all-0_0"
)
EFFECT_ID_ALL_TWO = (
    "e_effect-exam_search_play_card_stamina_consumption_change-02-inf-"
    "p_card_search-deck_all-all-0_0"
)
EFFECT_ID_CARD_FIVE = (
    "e_effect-exam_search_play_card_stamina_consumption_change-05-inf-"
    "p_card_search-deck_all-p_card-03-ido-3_135-all-0_0"
)
SUPPORTED_EFFECT_IDS = (
    EFFECT_ID_ALL_ONE,
    EFFECT_ID_ALL_TWO,
    EFFECT_ID_CARD_FIVE,
)

SEARCH_DECK_ALL = "p_card_search-deck_all"
SEARCH_DECK_ALL_CARD_03_IDO_3_135 = (
    "p_card_search-deck_all-p_card-03-ido-3_135"
)
TARGET_CARD_ID = "p_card-03-ido-3_135"

APPLICATION_TIMING = "effect-execute"
USE_TIMING = "card-stamina-cost-resolution"
PAYMENT_ORDER = (
    "search-override-before-card-get-stamina-cost-before-damage-stamina"
)
DURATION_INFINITE = -1

NATIVE_EXECUTOR_TYPE = (
    "Campus.InGame.Exam.SearchPlayCardStaminaConsumptionChangeEffectExecutor"
)
NATIVE_CONSTRUCTOR_VA = 0x7E8C0C4
NATIVE_EXECUTE_VA = 0x7E8C30C
NATIVE_TRY_ADD_STATUS_VA = 0x7E9BA40
NATIVE_USE_STATUS_VA = 0x7E9BD40

RESOLUTION_READY = "ready"
RESOLUTION_UNRESOLVED = "unresolved"

SCOPED_AFFECTED_CARD_VERSIONS = (
    *(("p_card-00-sup-3_158", upgrade) for upgrade in range(4)),
    *(("p_card-03-act-3_065", upgrade) for upgrade in range(4)),
    ("p_card-03-ido-100_042", 0),
)
EXISTING_COVERAGE_SOLE_VERSIONS = SCOPED_AFFECTED_CARD_VERSIONS[:8]

_SUPPORTED_SHAPES = {
    EFFECT_ID_ALL_ONE: (1, SEARCH_DECK_ALL, ()),
    EFFECT_ID_ALL_TWO: (2, SEARCH_DECK_ALL, ()),
    EFFECT_ID_CARD_FIVE: (
        5,
        SEARCH_DECK_ALL_CARD_03_IDO_3_135,
        (TARGET_CARD_ID,),
    ),
}

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
        raise ValueError(f"{label} must be text")
    return value


def _integer(value: object, label: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
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
class Plan3SearchStaminaGap:
    """One stable reason why an effect was not admitted."""

    code: str
    effect_id: str
    detail: str = ""

    def __post_init__(self) -> None:
        _text(self.code, "gap.code")
        _text(self.effect_id, "gap.effect_id")
        _text(self.detail, "gap.detail", allow_empty=True)

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "effect_id": self.effect_id,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "Plan3SearchStaminaGap":
        return cls(
            _text(payload.get("code"), "gap.code"),
            _text(payload.get("effect_id"), "gap.effect_id"),
            _text(payload.get("detail"), "gap.detail", allow_empty=True),
        )


@dataclass(frozen=True, slots=True)
class Plan3SearchStaminaTarget:
    """The proven direct-card predicate retained by the native status."""

    search_id: str
    card_position_type: str
    card_ids: tuple[str, ...] = ()
    upgrade_counts: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        _text(self.search_id, "target.search_id")
        if self.card_position_type != "ProduceCardPositionType_DeckAll":
            raise ValueError("only the proven DeckAll search position is supported")
        card_ids = _string_tuple(self.card_ids, "target.card_ids")
        upgrades = _integer_tuple(self.upgrade_counts, "target.upgrade_counts")
        object.__setattr__(self, "card_ids", card_ids)
        object.__setattr__(self, "upgrade_counts", upgrades)
        expected = {
            SEARCH_DECK_ALL: (),
            SEARCH_DECK_ALL_CARD_03_IDO_3_135: (TARGET_CARD_ID,),
        }.get(self.search_id)
        if expected is None or card_ids != expected or upgrades:
            raise ValueError("target search is outside the proven Master subset")

    @property
    def mode(self) -> str:
        return "all" if not self.card_ids else "card-id"

    def matches(self, card: Plan3NativeCard) -> bool:
        from .plan3_native_state import Plan3NativeCard

        if not isinstance(card, Plan3NativeCard):
            raise TypeError("card must be Plan3NativeCard")
        if self.card_ids and card.card_id not in self.card_ids:
            return False
        if self.upgrade_counts and card.effective_upgrade not in self.upgrade_counts:
            return False
        return True

    def to_dict(self) -> dict[str, object]:
        return {
            "search_id": self.search_id,
            "card_position_type": self.card_position_type,
            "mode": self.mode,
            "card_ids": list(self.card_ids),
            "upgrade_counts": list(self.upgrade_counts),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "Plan3SearchStaminaTarget":
        target = cls(
            search_id=_text(payload.get("search_id"), "target.search_id"),
            card_position_type=_text(
                payload.get("card_position_type"), "target.card_position_type"
            ),
            card_ids=_string_tuple(payload.get("card_ids"), "target.card_ids"),
            upgrade_counts=_integer_tuple(
                payload.get("upgrade_counts"), "target.upgrade_counts"
            ),
        )
        if payload.get("mode") != target.mode:
            raise ValueError("serialized target mode is inconsistent")
        return target


@dataclass(frozen=True, slots=True)
class Plan3SearchStaminaContract:
    """Executable projection of one exact supported effect/search pair."""

    effect_id: str
    stamina_cost: int
    activation_count: int
    duration_turns: int
    target: Plan3SearchStaminaTarget
    effect_type: str = EFFECT_TYPE
    application_timing: str = APPLICATION_TIMING
    use_timing: str = USE_TIMING
    payment_order: str = PAYMENT_ORDER
    executor_type: str = NATIVE_EXECUTOR_TYPE

    def __post_init__(self) -> None:
        _integer(self.stamina_cost, "contract.stamina_cost", minimum=0)
        _integer(self.activation_count, "contract.activation_count", minimum=1)
        _integer(self.duration_turns, "contract.duration_turns")
        expected = _SUPPORTED_SHAPES.get(self.effect_id)
        if expected is None:
            raise ValueError("effect_id is outside the proven Master subset")
        count, search_id, card_ids = expected
        if (
            self.effect_type != EFFECT_TYPE
            or self.stamina_cost != 0
            or self.activation_count != count
            or self.duration_turns != DURATION_INFINITE
            or not isinstance(self.target, Plan3SearchStaminaTarget)
            or self.target.search_id != search_id
            or self.target.card_ids != card_ids
            or self.application_timing != APPLICATION_TIMING
            or self.use_timing != USE_TIMING
            or self.payment_order != PAYMENT_ORDER
            or self.executor_type != NATIVE_EXECUTOR_TYPE
        ):
            raise ValueError("contract is outside the proven native/Master shape")

    @property
    def infinite(self) -> bool:
        return self.duration_turns == DURATION_INFINITE

    def to_dict(self) -> dict[str, object]:
        return {
            "effect_id": self.effect_id,
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

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "Plan3SearchStaminaContract":
        raw_target = _mapping(payload.get("target"), "contract.target")
        contract = cls(
            effect_id=_text(payload.get("effect_id"), "contract.effect_id"),
            effect_type=_text(payload.get("effect_type"), "contract.effect_type"),
            stamina_cost=_integer(payload.get("stamina_cost"), "contract.stamina_cost"),
            activation_count=_integer(
                payload.get("activation_count"), "contract.activation_count", minimum=1
            ),
            duration_turns=_integer(
                payload.get("duration_turns"), "contract.duration_turns"
            ),
            target=Plan3SearchStaminaTarget.from_dict(raw_target),
            application_timing=_text(
                payload.get("application_timing"), "contract.application_timing"
            ),
            use_timing=_text(payload.get("use_timing"), "contract.use_timing"),
            payment_order=_text(
                payload.get("payment_order"), "contract.payment_order"
            ),
            executor_type=_text(
                payload.get("executor_type"), "contract.executor_type"
            ),
        )
        if payload.get("infinite") is not True:
            raise ValueError("serialized contract duration marker is inconsistent")
        return contract


@dataclass(frozen=True, slots=True)
class Plan3SearchStaminaResolution:
    effect_id: str
    contract: Plan3SearchStaminaContract | None = None
    gaps: tuple[Plan3SearchStaminaGap, ...] = ()

    def __post_init__(self) -> None:
        _text(self.effect_id, "resolution.effect_id")
        gaps = tuple(self.gaps)
        if not all(isinstance(gap, Plan3SearchStaminaGap) for gap in gaps):
            raise TypeError("gaps must contain Plan3SearchStaminaGap")
        object.__setattr__(self, "gaps", gaps)
        if (self.contract is None) == (not gaps):
            raise ValueError("resolution must contain exactly one of contract or gaps")
        if self.contract is not None and self.contract.effect_id != self.effect_id:
            raise ValueError("resolution effect_id does not match contract")
        if any(gap.effect_id != self.effect_id for gap in gaps):
            raise ValueError("resolution effect_id does not match gap")

    @property
    def state(self) -> str:
        return RESOLUTION_READY if self.contract is not None else RESOLUTION_UNRESOLVED

    @property
    def executable(self) -> bool:
        return self.contract is not None

    def require_ready(self) -> Plan3SearchStaminaContract:
        if self.contract is None:
            raise Plan3SearchStaminaUnresolvedError(self)
        return self.contract

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "effect_id": self.effect_id,
            "contract": self.contract.to_dict() if self.contract else None,
            "gaps": [gap.to_dict() for gap in self.gaps],
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "Plan3SearchStaminaResolution":
        raw_contract = payload.get("contract")
        raw_gaps = payload.get("gaps")
        if raw_contract is not None and not isinstance(raw_contract, Mapping):
            raise ValueError("resolution.contract must be a mapping or null")
        if not isinstance(raw_gaps, list) or not all(
            isinstance(gap, Mapping) for gap in raw_gaps
        ):
            raise ValueError("resolution.gaps must be an object array")
        result = cls(
            effect_id=_text(payload.get("effect_id"), "resolution.effect_id"),
            contract=(
                Plan3SearchStaminaContract.from_dict(raw_contract)
                if raw_contract is not None
                else None
            ),
            gaps=tuple(Plan3SearchStaminaGap.from_dict(gap) for gap in raw_gaps),
        )
        if payload.get("state") != result.state:
            raise ValueError("serialized resolution state is inconsistent")
        return result


class Plan3SearchStaminaUnresolvedError(RuntimeError):
    def __init__(self, resolution: Plan3SearchStaminaResolution) -> None:
        self.resolution = resolution
        codes = ",".join(gap.code for gap in resolution.gaps)
        super().__init__(f"search stamina effect is unresolved: {codes}")


def _unresolved(effect_id: str, code: str, detail: str = "") -> Plan3SearchStaminaResolution:
    return Plan3SearchStaminaResolution(
        effect_id=effect_id,
        gaps=(Plan3SearchStaminaGap(code, effect_id, detail),),
    )


def _validate_search(
    rule: ProduceCardSearchRule,
    expected_id: str,
    expected_card_ids: tuple[str, ...],
) -> str | None:
    if not isinstance(rule, ProduceCardSearchRule):
        return "search row did not parse to ProduceCardSearchRule"
    actual = (
        rule.id,
        rule.card_rarities,
        rule.produce_card_ids,
        rule.upgrade_counts,
        rule.plan_type,
        rule.card_categories,
        rule.card_status_type,
        rule.order_type,
        rule.card_position_type,
        rule.card_search_tag,
        rule.produce_card_random_pool_id,
        rule.limit_count,
        rule.stamina_min_max_type,
        rule.stamina_min,
        rule.stamina_max,
        rule.exam_effect_type,
        rule.effect_group_ids,
        rule.is_self,
        rule.produce_card_pool_id,
        rule.cost_type,
        rule.is_customized,
    )
    expected = (
        expected_id,
        (),
        expected_card_ids,
        (),
        "ProducePlanType_Unknown",
        (),
        "ProduceCardSearchStatusType_Unknown",
        "ProduceCardOrderType_Unknown",
        "ProduceCardPositionType_DeckAll",
        "",
        "",
        0,
        "ConditionMinMaxType_Unknown",
        0,
        0,
        "ProduceExamEffectType_Unknown",
        (),
        False,
        "",
        "ExamCostType_Unknown",
        False,
    )
    return None if actual == expected else f"unexpected search shape: {rule.id}"


def resolve_plan3_search_stamina_change_from_rows(
    raw_effect: Mapping[str, object],
    search: ProduceCardSearchRule,
) -> Plan3SearchStaminaResolution:
    """Resolve already-loaded Master rows without broadening their shape."""

    if not isinstance(raw_effect, Mapping):
        raise TypeError("raw_effect must be a mapping")
    raw_id = raw_effect.get("id")
    effect_id = raw_id if isinstance(raw_id, str) and raw_id else "<invalid-effect-id>"
    shape = _SUPPORTED_SHAPES.get(effect_id)
    if shape is None:
        return _unresolved(effect_id, "unproven-effect-shape")
    count, search_id, card_ids = shape
    expected_fields = {
        "id": effect_id,
        "effectType": EFFECT_TYPE,
        "effectCount": count,
        "produceCardSearchId": search_id,
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
        key for key, expected in expected_fields.items() if raw_effect.get(key) != expected
    )
    mismatches = tuple(dict.fromkeys((*type_mismatches, *value_mismatches)))
    if mismatches:
        return _unresolved(
            effect_id,
            "master-shape-mismatch",
            ",".join(mismatches),
        )
    search_gap = _validate_search(search, search_id, card_ids)
    if search_gap is not None:
        return _unresolved(effect_id, "card-search-shape-unresolved", search_gap)
    target = Plan3SearchStaminaTarget(
        search_id=search_id,
        card_position_type=search.card_position_type,
        card_ids=search.produce_card_ids,
        upgrade_counts=search.upgrade_counts,
    )
    return Plan3SearchStaminaResolution(
        effect_id,
        Plan3SearchStaminaContract(
            effect_id=effect_id,
            stamina_cost=int(raw_effect["effectValue1"]),
            activation_count=int(raw_effect["effectCount"]),
            duration_turns=int(raw_effect["effectTurn"]),
            target=target,
        ),
    )


def resolve_plan3_search_stamina_change(
    effect_id: str,
    database: Path = DEFAULT_DATABASE,
) -> Plan3SearchStaminaResolution:
    """Load and resolve one exact imported Master effect, fail closed."""

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
        if effect_id not in _SUPPORTED_SHAPES:
            return _unresolved(effect_id, "unproven-effect-shape")
        raw = json.loads(str(row[7]))
        if not isinstance(raw, Mapping):
            return _unresolved(effect_id, "master-row-invalid", "raw_json is not an object")
        column_mismatch = (
            row[0] != raw.get("effectType")
            or row[1] != raw.get("effectValue1")
            or row[2] != raw.get("effectValue2")
            or row[3] != raw.get("effectCount")
            or row[4] != raw.get("effectTurn")
            or row[5] != raw.get("produceExamStatusEnchantId")
            or row[6] != raw.get("chainProduceExamEffectId")
        )
        if column_mismatch:
            return _unresolved(effect_id, "master-column-raw-mismatch")
        search_id = raw.get("produceCardSearchId")
        if not isinstance(search_id, str) or not search_id:
            return _unresolved(effect_id, "card-search-id-invalid")
        try:
            search = load_produce_card_search(search_id, database)
        except (KeyError, ValueError, sqlite3.Error) as error:
            return _unresolved(effect_id, "card-search-load-failed", str(error))
        return resolve_plan3_search_stamina_change_from_rows(raw, search)
    except (json.JSONDecodeError, sqlite3.Error, OSError) as error:
        return _unresolved(effect_id, "master-read-failed", str(error))


@dataclass(frozen=True, slots=True)
class Plan3SearchStaminaStatus:
    status_uid: int
    stamina_cost: int
    remaining_uses: int
    duration_turns: int
    target: Plan3SearchStaminaTarget
    from_effect_id: str

    def __post_init__(self) -> None:
        _integer(self.status_uid, "status_uid", minimum=1)
        _integer(self.stamina_cost, "stamina_cost", minimum=0)
        if self.stamina_cost != 0:
            raise ValueError("only the proven zero-cost status is supported")
        _integer(self.remaining_uses, "remaining_uses", minimum=1)
        if self.duration_turns != DURATION_INFINITE:
            raise ValueError("finite search-stamina duration is unresolved")
        if not isinstance(self.target, Plan3SearchStaminaTarget):
            raise TypeError("target must be Plan3SearchStaminaTarget")
        if self.from_effect_id not in SUPPORTED_EFFECT_IDS:
            raise ValueError("status source effect is outside the proven subset")

    @property
    def merge_key(self) -> tuple[int, int, str]:
        return self.duration_turns, self.stamina_cost, self.target.search_id

    def to_dict(self) -> dict[str, object]:
        return {
            "status_uid": self.status_uid,
            "stamina_cost": self.stamina_cost,
            "remaining_uses": self.remaining_uses,
            "duration_turns": self.duration_turns,
            "target": self.target.to_dict(),
            "from_effect_id": self.from_effect_id,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "Plan3SearchStaminaStatus":
        return cls(
            status_uid=_integer(payload.get("status_uid"), "status_uid", minimum=1),
            stamina_cost=_integer(payload.get("stamina_cost"), "stamina_cost"),
            remaining_uses=_integer(
                payload.get("remaining_uses"), "remaining_uses", minimum=1
            ),
            duration_turns=_integer(payload.get("duration_turns"), "duration_turns"),
            target=Plan3SearchStaminaTarget.from_dict(
                _mapping(payload.get("target"), "status.target")
            ),
            from_effect_id=_text(payload.get("from_effect_id"), "from_effect_id"),
        )


@dataclass(frozen=True, slots=True)
class Plan3SearchStaminaRuntime:
    statuses: tuple[Plan3SearchStaminaStatus, ...] = ()
    next_status_uid: int = 1

    def __post_init__(self) -> None:
        statuses = tuple(self.statuses)
        if not all(isinstance(status, Plan3SearchStaminaStatus) for status in statuses):
            raise TypeError("statuses must contain Plan3SearchStaminaStatus")
        uids = tuple(status.status_uid for status in statuses)
        if len(set(uids)) != len(uids):
            raise ValueError("status UIDs must be unique")
        _integer(self.next_status_uid, "next_status_uid", minimum=1)
        if uids and self.next_status_uid <= max(uids):
            raise ValueError("next_status_uid must exceed retained status UIDs")
        object.__setattr__(self, "statuses", statuses)

    def to_dict(self) -> dict[str, object]:
        return {
            "statuses": [status.to_dict() for status in self.statuses],
            "next_status_uid": self.next_status_uid,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "Plan3SearchStaminaRuntime":
        raw_statuses = payload.get("statuses")
        if not isinstance(raw_statuses, list) or not all(
            isinstance(status, Mapping) for status in raw_statuses
        ):
            raise ValueError("runtime.statuses must be an object array")
        return cls(
            statuses=tuple(
                Plan3SearchStaminaStatus.from_dict(status) for status in raw_statuses
            ),
            next_status_uid=_integer(
                payload.get("next_status_uid"), "next_status_uid", minimum=1
            ),
        )


@dataclass(frozen=True, slots=True)
class Plan3SearchStaminaCardIdentity:
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

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "Plan3SearchStaminaCardIdentity":
        return cls(
            guid=_text(payload.get("guid"), "identity.guid"),
            card_id=_text(payload.get("card_id"), "identity.card_id"),
            effective_upgrade=_integer(
                payload.get("effective_upgrade"), "identity.effective_upgrade", minimum=0
            ),
            zone=_text(payload.get("zone"), "identity.zone"),
            zone_index=_integer(payload.get("zone_index"), "identity.zone_index", minimum=0),
        )


@dataclass(frozen=True, slots=True)
class Plan3SearchStaminaGuidMutation:
    card: Plan3SearchStaminaCardIdentity
    status_uid: int
    operation: str
    prior_remaining_uses: int
    next_remaining_uses: int

    def __post_init__(self) -> None:
        if not isinstance(self.card, Plan3SearchStaminaCardIdentity):
            raise TypeError("card must be Plan3SearchStaminaCardIdentity")
        _integer(self.status_uid, "mutation.status_uid", minimum=1)
        if self.operation not in {"installed", "stacked"}:
            raise ValueError("mutation.operation is invalid")
        _integer(self.prior_remaining_uses, "mutation.prior_remaining_uses", minimum=0)
        _integer(self.next_remaining_uses, "mutation.next_remaining_uses", minimum=1)

    def to_dict(self) -> dict[str, object]:
        return {
            "card": self.card.to_dict(),
            "status_uid": self.status_uid,
            "operation": self.operation,
            "prior_remaining_uses": self.prior_remaining_uses,
            "next_remaining_uses": self.next_remaining_uses,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "Plan3SearchStaminaGuidMutation":
        return cls(
            card=Plan3SearchStaminaCardIdentity.from_dict(
                _mapping(payload.get("card"), "mutation.card")
            ),
            status_uid=_integer(payload.get("status_uid"), "mutation.status_uid", minimum=1),
            operation=_text(payload.get("operation"), "mutation.operation"),
            prior_remaining_uses=_integer(
                payload.get("prior_remaining_uses"),
                "mutation.prior_remaining_uses",
                minimum=0,
            ),
            next_remaining_uses=_integer(
                payload.get("next_remaining_uses"),
                "mutation.next_remaining_uses",
                minimum=1,
            ),
        )


@dataclass(frozen=True, slots=True)
class Plan3SearchStaminaApplication:
    effect_id: str
    runtime_before: Plan3SearchStaminaRuntime
    runtime_after: Plan3SearchStaminaRuntime
    status_uid: int
    operation: str
    target_mutations: tuple[Plan3SearchStaminaGuidMutation, ...]
    application_timing: str = APPLICATION_TIMING

    def __post_init__(self) -> None:
        if self.effect_id not in SUPPORTED_EFFECT_IDS:
            raise ValueError("application effect is outside the proven subset")
        if not isinstance(self.runtime_before, Plan3SearchStaminaRuntime) or not isinstance(
            self.runtime_after, Plan3SearchStaminaRuntime
        ):
            raise TypeError("application runtimes must be Plan3SearchStaminaRuntime")
        _integer(self.status_uid, "application.status_uid", minimum=1)
        if self.operation not in {"installed", "stacked"}:
            raise ValueError("application.operation is invalid")
        mutations = tuple(self.target_mutations)
        if not all(isinstance(item, Plan3SearchStaminaGuidMutation) for item in mutations):
            raise TypeError("target_mutations contains an invalid value")
        if len({item.card.guid for item in mutations}) != len(mutations):
            raise ValueError("target mutations must be unique per GUID")
        if any(
            item.status_uid != self.status_uid or item.operation != self.operation
            for item in mutations
        ):
            raise ValueError("target mutation does not match application")
        if self.application_timing != APPLICATION_TIMING:
            raise ValueError("application timing is outside the proven order")
        object.__setattr__(self, "target_mutations", mutations)

    @property
    def target_guids(self) -> tuple[str, ...]:
        return tuple(item.card.guid for item in self.target_mutations)

    def to_dict(self) -> dict[str, object]:
        return {
            "effect_id": self.effect_id,
            "runtime_before": self.runtime_before.to_dict(),
            "runtime_after": self.runtime_after.to_dict(),
            "status_uid": self.status_uid,
            "operation": self.operation,
            "target_mutations": [item.to_dict() for item in self.target_mutations],
            "target_guids": list(self.target_guids),
            "application_timing": self.application_timing,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "Plan3SearchStaminaApplication":
        raw_mutations = payload.get("target_mutations")
        if not isinstance(raw_mutations, list) or not all(
            isinstance(item, Mapping) for item in raw_mutations
        ):
            raise ValueError("application.target_mutations must be an object array")
        result = cls(
            effect_id=_text(payload.get("effect_id"), "application.effect_id"),
            runtime_before=Plan3SearchStaminaRuntime.from_dict(
                _mapping(payload.get("runtime_before"), "application.runtime_before")
            ),
            runtime_after=Plan3SearchStaminaRuntime.from_dict(
                _mapping(payload.get("runtime_after"), "application.runtime_after")
            ),
            status_uid=_integer(
                payload.get("status_uid"), "application.status_uid", minimum=1
            ),
            operation=_text(payload.get("operation"), "application.operation"),
            target_mutations=tuple(
                Plan3SearchStaminaGuidMutation.from_dict(item) for item in raw_mutations
            ),
            application_timing=_text(
                payload.get("application_timing"), "application.application_timing"
            ),
        )
        if payload.get("target_guids") != list(result.target_guids):
            raise ValueError("serialized target GUIDs are inconsistent")
        return result


def _card_identities(
    state: Plan3NativeState,
    playing_card: Plan3NativeCard | None,
) -> tuple[tuple[Plan3NativeCard, Plan3SearchStaminaCardIdentity], ...]:
    from .plan3_native_state import Plan3NativeCard, Plan3NativeState

    if not isinstance(state, Plan3NativeState):
        raise TypeError("state must be Plan3NativeState")
    values: list[tuple[Plan3NativeCard, Plan3SearchStaminaCardIdentity]] = []
    for zone in ("hand", "deck", "grave", "lost", "hold"):
        for index, card in enumerate(getattr(state, zone)):
            values.append(
                (
                    card,
                    Plan3SearchStaminaCardIdentity(
                        card.guid, card.card_id, card.effective_upgrade, zone, index
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
                Plan3SearchStaminaCardIdentity(
                    playing_card.guid,
                    playing_card.card_id,
                    playing_card.effective_upgrade,
                    "playing",
                    0,
                ),
            )
        )
    return tuple(values)


def apply_plan3_search_stamina_change(
    runtime: Plan3SearchStaminaRuntime,
    contract: Plan3SearchStaminaContract,
    state: Plan3NativeState,
    *,
    playing_card: Plan3NativeCard | None = None,
) -> Plan3SearchStaminaApplication:
    """Install/merge the native search status and snapshot matching GUIDs.

    The card state is observed only for the audit mutations; it is returned
    unchanged by construction.  The status retains the search predicate, so a
    matching card that appears later is still eligible, as in Android.
    """

    from .plan3_native_state import Plan3NativeState

    if not isinstance(runtime, Plan3SearchStaminaRuntime):
        raise TypeError("runtime must be Plan3SearchStaminaRuntime")
    if not isinstance(contract, Plan3SearchStaminaContract):
        raise TypeError("contract must be Plan3SearchStaminaContract")
    identities = tuple(
        identity
        for card, identity in _card_identities(state, playing_card)
        if contract.target.matches(card)
    )
    merge_key = (contract.duration_turns, contract.stamina_cost, contract.target.search_id)
    matching_index = next(
        (
            index
            for index, status in enumerate(runtime.statuses)
            if status.merge_key == merge_key
        ),
        None,
    )
    if matching_index is None:
        operation = "installed"
        prior = 0
        status = Plan3SearchStaminaStatus(
            status_uid=runtime.next_status_uid,
            stamina_cost=contract.stamina_cost,
            remaining_uses=contract.activation_count,
            duration_turns=contract.duration_turns,
            target=contract.target,
            from_effect_id=contract.effect_id,
        )
        after = Plan3SearchStaminaRuntime(
            statuses=runtime.statuses + (status,),
            next_status_uid=runtime.next_status_uid + 1,
        )
    else:
        operation = "stacked"
        existing = runtime.statuses[matching_index]
        prior = existing.remaining_uses
        status = replace(
            existing,
            remaining_uses=existing.remaining_uses + contract.activation_count,
        )
        statuses = list(runtime.statuses)
        statuses[matching_index] = status
        after = Plan3SearchStaminaRuntime(tuple(statuses), runtime.next_status_uid)
    mutations = tuple(
        Plan3SearchStaminaGuidMutation(
            card=identity,
            status_uid=status.status_uid,
            operation=operation,
            prior_remaining_uses=prior,
            next_remaining_uses=status.remaining_uses,
        )
        for identity in identities
    )
    return Plan3SearchStaminaApplication(
        effect_id=contract.effect_id,
        runtime_before=runtime,
        runtime_after=after,
        status_uid=status.status_uid,
        operation=operation,
        target_mutations=mutations,
    )


@dataclass(frozen=True, slots=True)
class Plan3SearchStaminaPayment:
    runtime_before: Plan3SearchStaminaRuntime
    runtime_after: Plan3SearchStaminaRuntime
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
    use_timing: str = USE_TIMING
    payment_order: str = PAYMENT_ORDER
    downstream_stage: str = "DamageStamina"

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_before, Plan3SearchStaminaRuntime) or not isinstance(
            self.runtime_after, Plan3SearchStaminaRuntime
        ):
            raise TypeError("payment runtimes must be Plan3SearchStaminaRuntime")
        _text(self.card_guid, "payment.card_guid")
        _text(self.card_id, "payment.card_id")
        _integer(self.effective_upgrade, "payment.effective_upgrade", minimum=0)
        _integer(self.selected_stamina_cost, "payment.selected_stamina_cost", minimum=0)
        if self.cost_source not in {"search-override", "card-get-stamina-cost"}:
            raise ValueError("payment.cost_source is invalid")
        if not isinstance(self.card_cost_evaluated, bool) or not isinstance(
            self.simulated, bool
        ) or not isinstance(self.consumed_use, bool):
            raise TypeError("payment flags must be booleans")
        if self.use_timing != USE_TIMING or self.payment_order != PAYMENT_ORDER:
            raise ValueError("payment timing/order is outside the proven path")
        if self.downstream_stage != "DamageStamina":
            raise ValueError("payment downstream stage is invalid")
        matched_values = (
            self.matched_status_uid,
            self.matched_from_effect_id,
            self.matched_search_id,
        )
        if self.cost_source == "search-override":
            if any(value is None for value in matched_values):
                raise ValueError("override payment must retain matched status identity")
            if self.card_cost_evaluated or self.card_stamina_cost is not None:
                raise ValueError("card cost must not be evaluated after a search override")
            if self.consumed_use == self.simulated:
                raise ValueError("only non-simulated override use consumes a count")
        else:
            if any(value is not None for value in matched_values) or self.consumed_use:
                raise ValueError("fallback payment cannot retain or consume a status")
            if not self.card_cost_evaluated or self.card_stamina_cost is None:
                raise ValueError("fallback payment must evaluate the card stamina cost")
            if self.selected_stamina_cost != self.card_stamina_cost:
                raise ValueError("fallback selected cost is inconsistent")

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
            "use_timing": self.use_timing,
            "payment_order": self.payment_order,
            "downstream_stage": self.downstream_stage,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "Plan3SearchStaminaPayment":
        def optional_integer(value: object, label: str) -> int | None:
            return None if value is None else _integer(value, label, minimum=0)

        def optional_text(value: object, label: str) -> str | None:
            return None if value is None else _text(value, label)

        for name in ("card_cost_evaluated", "simulated", "consumed_use"):
            if not isinstance(payload.get(name), bool):
                raise ValueError(f"payment.{name} must be a boolean")
        return cls(
            runtime_before=Plan3SearchStaminaRuntime.from_dict(
                _mapping(payload.get("runtime_before"), "payment.runtime_before")
            ),
            runtime_after=Plan3SearchStaminaRuntime.from_dict(
                _mapping(payload.get("runtime_after"), "payment.runtime_after")
            ),
            card_guid=_text(payload.get("card_guid"), "payment.card_guid"),
            card_id=_text(payload.get("card_id"), "payment.card_id"),
            effective_upgrade=_integer(
                payload.get("effective_upgrade"), "payment.effective_upgrade", minimum=0
            ),
            selected_stamina_cost=_integer(
                payload.get("selected_stamina_cost"),
                "payment.selected_stamina_cost",
                minimum=0,
            ),
            cost_source=_text(payload.get("cost_source"), "payment.cost_source"),
            matched_status_uid=optional_integer(
                payload.get("matched_status_uid"), "payment.matched_status_uid"
            ),
            matched_from_effect_id=optional_text(
                payload.get("matched_from_effect_id"), "payment.matched_from_effect_id"
            ),
            matched_search_id=optional_text(
                payload.get("matched_search_id"), "payment.matched_search_id"
            ),
            card_cost_evaluated=payload["card_cost_evaluated"],
            card_stamina_cost=optional_integer(
                payload.get("card_stamina_cost"), "payment.card_stamina_cost"
            ),
            simulated=payload["simulated"],
            consumed_use=payload["consumed_use"],
            use_timing=_text(payload.get("use_timing"), "payment.use_timing"),
            payment_order=_text(payload.get("payment_order"), "payment.payment_order"),
            downstream_stage=_text(
                payload.get("downstream_stage"), "payment.downstream_stage"
            ),
        )


def resolve_plan3_search_stamina_payment(
    runtime: Plan3SearchStaminaRuntime,
    card: Plan3NativeCard,
    card_stamina_cost: int | Callable[[], int],
    *,
    simulate: bool = False,
) -> Plan3SearchStaminaPayment:
    """Select and optionally spend an override before card cost evaluation."""

    from .plan3_native_state import Plan3NativeCard

    if not isinstance(runtime, Plan3SearchStaminaRuntime):
        raise TypeError("runtime must be Plan3SearchStaminaRuntime")
    if not isinstance(card, Plan3NativeCard):
        raise TypeError("card must be Plan3NativeCard")
    if not isinstance(simulate, bool):
        raise TypeError("simulate must be a boolean")
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
        elif status.remaining_uses == 1:
            after = Plan3SearchStaminaRuntime(
                runtime.statuses[:matched_index] + runtime.statuses[matched_index + 1 :],
                runtime.next_status_uid,
            )
        else:
            statuses = list(runtime.statuses)
            statuses[matched_index] = replace(
                status, remaining_uses=status.remaining_uses - 1
            )
            after = Plan3SearchStaminaRuntime(tuple(statuses), runtime.next_status_uid)
        return Plan3SearchStaminaPayment(
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
        )
    fallback = card_stamina_cost() if callable(card_stamina_cost) else card_stamina_cost
    fallback = _integer(fallback, "card_stamina_cost", minimum=0)
    return Plan3SearchStaminaPayment(
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
    )


@dataclass(frozen=True, slots=True)
class Plan3SearchStaminaAffectedVersion:
    card_id: str
    upgrade_count: int
    effect_ids: tuple[str, ...]
    sole_unlock_in_existing_coverage: bool

    def __post_init__(self) -> None:
        _text(self.card_id, "affected.card_id")
        _integer(self.upgrade_count, "affected.upgrade_count", minimum=0)
        effects = _string_tuple(self.effect_ids, "affected.effect_ids")
        if not effects or any(effect not in SUPPORTED_EFFECT_IDS for effect in effects):
            raise ValueError("affected version must reference a supported effect")
        if not isinstance(self.sole_unlock_in_existing_coverage, bool):
            raise TypeError("sole_unlock_in_existing_coverage must be boolean")
        object.__setattr__(self, "effect_ids", effects)

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "upgrade_count": self.upgrade_count,
            "effect_ids": list(self.effect_ids),
            "sole_unlock_in_existing_coverage": self.sole_unlock_in_existing_coverage,
        }


def load_plan3_search_stamina_affected_versions(
    database: Path = DEFAULT_DATABASE,
) -> tuple[Plan3SearchStaminaAffectedVersion, ...]:
    """Revalidate the scoped 9-version Master inventory (8 sole blockers)."""

    database = Path(database)
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    results: list[Plan3SearchStaminaAffectedVersion] = []
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        for card_id, upgrade in SCOPED_AFFECTED_CARD_VERSIONS:
            row = connection.execute(
                "SELECT play_effects_json FROM card WHERE id = ? AND upgrade_count = ?",
                (card_id, upgrade),
            ).fetchone()
            if row is None:
                raise ValueError(f"scoped affected card version is missing: {card_id}+{upgrade}")
            raw_effects = json.loads(str(row[0]))
            if not isinstance(raw_effects, list) or not all(
                isinstance(effect, Mapping)
                and isinstance(effect.get("produceExamEffectId"), str)
                for effect in raw_effects
            ):
                raise ValueError(f"invalid card effect list: {card_id}+{upgrade}")
            effects = tuple(
                str(effect["produceExamEffectId"])
                for effect in raw_effects
                if effect["produceExamEffectId"] in SUPPORTED_EFFECT_IDS
            )
            if len(effects) != 1:
                raise ValueError(
                    "scoped affected card version has unexpected family linkage: "
                    f"{card_id}+{upgrade}"
                )
            results.append(
                Plan3SearchStaminaAffectedVersion(
                    card_id,
                    upgrade,
                    effects,
                    (card_id, upgrade) in EXISTING_COVERAGE_SOLE_VERSIONS,
                )
            )
    return tuple(results)


__all__ = [
    "APPLICATION_TIMING",
    "DURATION_INFINITE",
    "EFFECT_ID_ALL_ONE",
    "EFFECT_ID_ALL_TWO",
    "EFFECT_ID_CARD_FIVE",
    "EFFECT_TYPE",
    "EXISTING_COVERAGE_SOLE_VERSIONS",
    "NATIVE_CONSTRUCTOR_VA",
    "NATIVE_EXECUTE_VA",
    "NATIVE_EXECUTOR_TYPE",
    "NATIVE_TRY_ADD_STATUS_VA",
    "NATIVE_USE_STATUS_VA",
    "PAYMENT_ORDER",
    "RESOLUTION_READY",
    "RESOLUTION_UNRESOLVED",
    "SCOPED_AFFECTED_CARD_VERSIONS",
    "SEARCH_DECK_ALL",
    "SEARCH_DECK_ALL_CARD_03_IDO_3_135",
    "SUPPORTED_EFFECT_IDS",
    "TARGET_CARD_ID",
    "USE_TIMING",
    "Plan3SearchStaminaAffectedVersion",
    "Plan3SearchStaminaApplication",
    "Plan3SearchStaminaCardIdentity",
    "Plan3SearchStaminaContract",
    "Plan3SearchStaminaGap",
    "Plan3SearchStaminaGuidMutation",
    "Plan3SearchStaminaPayment",
    "Plan3SearchStaminaResolution",
    "Plan3SearchStaminaRuntime",
    "Plan3SearchStaminaStatus",
    "Plan3SearchStaminaTarget",
    "Plan3SearchStaminaUnresolvedError",
    "apply_plan3_search_stamina_change",
    "load_plan3_search_stamina_affected_versions",
    "resolve_plan3_search_stamina_change",
    "resolve_plan3_search_stamina_change_from_rows",
    "resolve_plan3_search_stamina_payment",
]
