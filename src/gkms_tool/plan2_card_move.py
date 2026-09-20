"""Plan2/Common adapter for the proven ``ExamCardMove`` subset.

The Android executor payload is plan-neutral.  This module only validates the
Master payload and delegates candidate construction, random selection, and
the immutable zone mutation to :mod:`plan3_native_state`.  It intentionally
does not provide a second state model or a second CardMove algorithm.

Only the native subset already proved by the shared Plan3 primitive is
accepted at execution time.  Rich searches, deck-placement destinations,
playing-card settlement, and chained/timer/status execution fail closed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any, TypeAlias

from .audition_support_runtime import SupportUpgradeRuntimeInput
from .card_search import (
    ProduceCardSearchRule,
    load_produce_card_search,
    validate_plan3_card_move_search,
)
from .master_db import DEFAULT_DATABASE
from .plan3_native_state import (
    Plan3NativeCard,
    Plan3NativeCardMoveTarget,
    Plan3NativeState,
    Plan3NativeStateError,
)


CARD_MOVE_EFFECT_TYPE = "ProduceExamEffectType_ExamCardMove"
PLAN_COMMON = "ProducePlanType_Common"
PLAN2 = "ProducePlanType_Plan2"
PLAN2_COMMON_PLAN_TYPES = frozenset((PLAN_COMMON, PLAN2))

MOVE_HAND = "ProduceCardMovePositionType_Hand"
MOVE_GRAVE = "ProduceCardMovePositionType_Grave"
MOVE_LOST = "ProduceCardMovePositionType_Lost"
MOVE_HOLD = "ProduceCardMovePositionType_Hold"
SEARCH_DECK_GRAVE = "ProduceCardPositionType_DeckGrave"

PICK_ALL = "ProducePickRangeType_All"
PICK_SELECT = "ProducePickRangeType_Select"
PICK_RANDOM = "ProducePickRangeType_Random"
PICK_RANGE_TYPES = frozenset((PICK_ALL, PICK_SELECT, PICK_RANDOM))

UNKNOWN_PICK_COUNT = "ProducePickCountType_Unknown"
UNKNOWN_EFFECT = "ProduceExamEffectType_Unknown"
UNKNOWN_PICK_RANGE = "ProducePickRangeType_Unknown"

PLAN2_CARD_MOVE_LOST_RANDOM_EFFECT_ID = (
    "e_effect-exam_card_move-"
    "p_card_search-deck_grave-p_card-00-acc-0_002-lost-random-1_1"
)
PLAN2_CARD_MOVE_LOST_RANDOM_SEARCH_ID = (
    "p_card_search-deck_grave-p_card-00-acc-0_002"
)

Plan2CardMoveState: TypeAlias = Plan3NativeState
Plan2CardMoveCard: TypeAlias = Plan3NativeCard
Plan2CardMoveTarget: TypeAlias = Plan3NativeCardMoveTarget

_MISSING = object()
_NO_DEFAULT = object()


class CardMoveError(ValueError):
    """Base error with a stable fail-closed code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class CardMoveResolutionError(CardMoveError):
    """The Master row or its shape cannot be proven by this adapter."""


class CardMoveInputError(CardMoveError):
    """An explicitly supplied execution input is malformed."""


def _text(value: object, field: str, *, error: type[CardMoveError]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error("invalid-text", field)
    return value


def _nonnegative_int(
    value: object, field: str, *, error: type[CardMoveError]
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise error("invalid-nonnegative-int", field)
    return value


def _string_tuple(
    value: object, field: str, *, error: type[CardMoveError]
) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise error("invalid-string-list", field)
    result = tuple(value)
    if any(not isinstance(item, str) or not item.strip() for item in result):
        raise error("invalid-string-list", field)
    return result


@dataclass(frozen=True, slots=True)
class Plan2CardMoveContract:
    """Structural fields retained from one Master CardMove payload."""

    effect_id: str
    search_id: str
    destination: str
    pick_range_type: str
    count_min: int
    count_max: int
    effect_type: str = CARD_MOVE_EFFECT_TYPE
    target_card_id: str = ""
    target_upgrade: int = 0
    target_effect_type: str = UNKNOWN_EFFECT
    pick_count_type: str = UNKNOWN_PICK_COUNT
    pick_count_reference_search_id: str = ""
    search2_id: str = ""
    second_pick_range_type: str = UNKNOWN_PICK_RANGE
    second_count_min: int = 0
    second_count_max: int = 0
    second_pick_count_type: str = UNKNOWN_PICK_COUNT
    second_pick_count_reference_search_id: str = ""
    card_status_enchant_id: str = ""
    card_grow_effect_ids: tuple[str, ...] = ()
    effect_group_ids: tuple[str, ...] = ()
    chain_effect_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect_id", error=CardMoveResolutionError)
        _text(self.search_id, "search_id", error=CardMoveResolutionError)
        _text(self.destination, "destination", error=CardMoveResolutionError)
        _text(
            self.pick_range_type,
            "pick_range_type",
            error=CardMoveResolutionError,
        )
        _nonnegative_int(
            self.count_min, "count_min", error=CardMoveResolutionError
        )
        _nonnegative_int(
            self.count_max, "count_max", error=CardMoveResolutionError
        )
        if self.count_min > self.count_max:
            raise CardMoveResolutionError("invalid-count-range")
        if self.effect_type != CARD_MOVE_EFFECT_TYPE:
            raise CardMoveResolutionError(
                "unexpected-effect-type", self.effect_type
            )
        _text(
            self.pick_count_type,
            "pick_count_type",
            error=CardMoveResolutionError,
        )
        _nonnegative_int(
            self.target_upgrade,
            "target_upgrade",
            error=CardMoveResolutionError,
        )
        _nonnegative_int(
            self.second_count_min,
            "second_count_min",
            error=CardMoveResolutionError,
        )
        _nonnegative_int(
            self.second_count_max,
            "second_count_max",
            error=CardMoveResolutionError,
        )
        if self.second_count_min > self.second_count_max:
            raise CardMoveResolutionError("invalid-second-count-range")
        for field, value in (
            ("target_card_id", self.target_card_id),
            ("target_effect_type", self.target_effect_type),
            ("pick_count_reference_search_id", self.pick_count_reference_search_id),
            ("search2_id", self.search2_id),
            ("second_pick_range_type", self.second_pick_range_type),
            ("second_pick_count_type", self.second_pick_count_type),
            (
                "second_pick_count_reference_search_id",
                self.second_pick_count_reference_search_id,
            ),
            ("card_status_enchant_id", self.card_status_enchant_id),
        ):
            if not isinstance(value, str):
                raise CardMoveResolutionError("invalid-text-field", field)
        for field, value in (
            ("card_grow_effect_ids", self.card_grow_effect_ids),
            ("effect_group_ids", self.effect_group_ids),
            ("chain_effect_ids", self.chain_effect_ids),
        ):
            _string_tuple(value, field, error=CardMoveResolutionError)

    @property
    def count_range(self) -> tuple[int, int]:
        return self.count_min, self.count_max

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)


def assert_plan2_card_move_lost_random_direct_contract(
    contract: Plan2CardMoveContract,
) -> None:
    """Require the shared DeckGrave filtered Random(1)->Lost subset."""

    if not isinstance(contract, Plan2CardMoveContract):
        raise TypeError("contract must be Plan2CardMoveContract")
    if not (
        contract.destination == MOVE_LOST
        and contract.pick_range_type == PICK_RANDOM
        and contract.count_range == (1, 1)
        and not contract.target_card_id
        and contract.target_upgrade == 0
        and contract.target_effect_type == UNKNOWN_EFFECT
        and contract.pick_count_type == UNKNOWN_PICK_COUNT
        and not contract.pick_count_reference_search_id
        and not contract.search2_id
        and contract.second_pick_range_type == UNKNOWN_PICK_RANGE
        and contract.second_count_min == 0
        and contract.second_count_max == 0
        and contract.second_pick_count_type == UNKNOWN_PICK_COUNT
        and not contract.second_pick_count_reference_search_id
        and not contract.card_status_enchant_id
        and not contract.card_grow_effect_ids
        and not contract.effect_group_ids
        and not contract.chain_effect_ids
    ):
        raise CardMoveResolutionError(
            "unintegrated-card-move-shape", contract.effect_id
        )


def assert_plan2_card_move_lost_random_direct_search(
    contract: Plan2CardMoveContract,
    search: ProduceCardSearchRule,
) -> None:
    """Require one ID-filtered DeckGrave search without binding its IDs."""

    assert_plan2_card_move_lost_random_direct_contract(contract)
    if not isinstance(search, ProduceCardSearchRule):
        raise TypeError("search must be ProduceCardSearchRule")
    if search.id != contract.search_id:
        raise CardMoveResolutionError(
            "card-move-search-id-contract-mismatch",
            f"contract={contract.search_id};search={search.id}",
        )
    invalid = validate_plan3_card_move_search(search)
    if invalid is not None:
        raise CardMoveResolutionError(invalid, search.id)
    if not (
        search.card_position_type == SEARCH_DECK_GRAVE
        and len(search.produce_card_ids) == 1
        and not search.upgrade_counts
        and search.limit_count == 0
    ):
        raise CardMoveResolutionError(
            "unintegrated-card-move-search-shape",
            search.id,
        )

@dataclass(frozen=True, slots=True)
class CardMoveUnresolvedInput:
    """Typed result for a row that cannot be resolved."""

    code: str
    detail: str

    @property
    def reason(self) -> str:
        return self.detail


@dataclass(frozen=True, slots=True)
class Plan2CardMoveResult:
    """Immutable differential/audit result for one delegated effect slot."""

    before: Plan3NativeState
    after: Plan3NativeState
    candidates: tuple[Plan3NativeCardMoveTarget, ...]
    selected: tuple[Plan3NativeCardMoveTarget, ...]
    random_state_before: int
    random_state_after: int

    def __post_init__(self) -> None:
        if not isinstance(self.before, Plan3NativeState) or not isinstance(
            self.after, Plan3NativeState
        ):
            raise CardMoveInputError("state-must-be-plan3-native-state")
        if not all(
            isinstance(value, Plan3NativeCardMoveTarget)
            for value in (*self.candidates, *self.selected)
        ):
            raise CardMoveInputError("invalid-card-move-target")
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "selected", tuple(self.selected))

    @property
    def state(self) -> Plan3NativeState:
        return self.after

    @property
    def new_state(self) -> Plan3NativeState:
        return self.after

    @property
    def selected_guids(self) -> tuple[str, ...]:
        return tuple(value.guid for value in self.selected)

    @property
    def resolved(self) -> bool:
        return True

    @property
    def rng_consumed(self) -> bool:
        return self.random_state_before != self.random_state_after

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _row_dict(
    effect_row: Mapping[str, object] | sqlite3.Row,
) -> dict[str, object]:
    if isinstance(effect_row, Mapping):
        return dict(effect_row)
    keys = getattr(effect_row, "keys", None)
    if callable(keys):
        try:
            return {key: effect_row[key] for key in keys()}
        except (KeyError, IndexError, TypeError) as error:
            raise CardMoveResolutionError("invalid-master-row") from error
    raise CardMoveResolutionError("master-row-must-be-mapping")


def _value(
    row: Mapping[str, object],
    *names: str,
    default: object = _NO_DEFAULT,
) -> object:
    for name in names:
        if name in row:
            return row[name]
    if default is not _NO_DEFAULT:
        return default
    raise CardMoveResolutionError("missing-structural-field", names[0])


def _raw_master_payload(outer: Mapping[str, object]) -> dict[str, object]:
    candidate = _value(outer, "raw_json", "rawJson", "raw", default=_MISSING)
    if candidate is _MISSING or candidate is None or candidate == "":
        structural_keys = {
            "effectType",
            "produceCardSearchId",
            "movePositionType",
            "pickRangeType",
            "pickCountMin",
            "pickCountMax",
        }
        if structural_keys.intersection(outer):
            return dict(outer)
        raise CardMoveResolutionError("missing-master-raw-json")
    if isinstance(candidate, (bytes, bytearray)):
        candidate = candidate.decode("utf-8")
    if isinstance(candidate, str):
        try:
            candidate = json.loads(candidate)
        except json.JSONDecodeError as error:
            raise CardMoveResolutionError("invalid-master-raw-json") from error
    if not isinstance(candidate, Mapping):
        raise CardMoveResolutionError("master-raw-json-must-be-object")
    return dict(candidate)


def _optional_agrees(
    outer: Mapping[str, object],
    raw: Mapping[str, object],
    outer_name: str,
    raw_name: str,
) -> None:
    outer_value = _value(outer, outer_name, default=_MISSING)
    raw_value = _value(raw, raw_name, default=_MISSING)
    if (
        outer_value is not _MISSING
        and raw_value is not _MISSING
        and outer_value != raw_value
    ):
        raise CardMoveResolutionError(
            "master-structural-field-mismatch", raw_name
        )


def _require_plan2_common(
    row: Mapping[str, object] | sqlite3.Row,
    explicit_plan_type: str | None,
) -> None:
    outer = _row_dict(row)
    observed = explicit_plan_type
    if observed is None:
        candidate = _value(
            outer,
            "plan_type",
            "planType",
            "producePlanType",
            "card_plan_type",
            default=_MISSING,
        )
        if candidate is not _MISSING:
            observed = candidate if isinstance(candidate, str) else str(candidate)
    if observed is not None and observed not in PLAN2_COMMON_PLAN_TYPES:
        raise CardMoveResolutionError("unsupported-plan", str(observed))


def resolve_plan2_card_move_contract(
    effect_row: Mapping[str, object] | sqlite3.Row,
    *,
    plan_type: str | None = None,
) -> Plan2CardMoveContract:
    """Parse one exact Master payload; execution applies the proven-shape gate."""

    _require_plan2_common(effect_row, plan_type)
    outer = _row_dict(effect_row)
    raw = _raw_master_payload(outer)

    outer_type = _value(outer, "effect_type", "effectType", default=_MISSING)
    raw_type = _value(raw, "effectType", "effect_type", default=_MISSING)
    for value in (outer_type, raw_type):
        if value is not _MISSING and value != CARD_MOVE_EFFECT_TYPE:
            raise CardMoveResolutionError("unexpected-effect-type", str(value))
    if outer_type is _MISSING and raw_type is _MISSING:
        raise CardMoveResolutionError("missing-effect-type")

    outer_id = _value(outer, "id", "effect_id", default=_MISSING)
    raw_id = _value(raw, "id", "effect_id", default=_MISSING)
    effect_id_value = raw_id if raw_id is not _MISSING else outer_id
    effect_id = _text(
        effect_id_value, "effect_id", error=CardMoveResolutionError
    )
    if outer_id is not _MISSING and raw_id is not _MISSING and outer_id != raw_id:
        raise CardMoveResolutionError("master-id-mismatch")

    for outer_name, raw_name in (
        ("value1", "effectValue1"),
        ("value2", "effectValue2"),
        ("effect_count", "effectCount"),
        ("effect_turn", "effectTurn"),
        ("status_enchant_id", "produceExamStatusEnchantId"),
        ("chain_effect_id", "chainProduceExamEffectId"),
    ):
        _optional_agrees(outer, raw, outer_name, raw_name)

    search_id = _text(
        _value(raw, "produceCardSearchId", "search_id"),
        "produceCardSearchId",
        error=CardMoveResolutionError,
    )
    destination = _text(
        _value(raw, "movePositionType", "destination"),
        "movePositionType",
        error=CardMoveResolutionError,
    )
    pick_range_type = _text(
        _value(raw, "pickRangeType", "pick_range_type"),
        "pickRangeType",
        error=CardMoveResolutionError,
    )
    count_min = _nonnegative_int(
        _value(raw, "pickCountMin", "count_min"),
        "pickCountMin",
        error=CardMoveResolutionError,
    )
    count_max = _nonnegative_int(
        _value(raw, "pickCountMax", "count_max"),
        "pickCountMax",
        error=CardMoveResolutionError,
    )
    target_upgrade = _nonnegative_int(
        _value(raw, "targetUpgradeCount", "target_upgrade", default=0),
        "targetUpgradeCount",
        error=CardMoveResolutionError,
    )

    return Plan2CardMoveContract(
        effect_id=effect_id,
        search_id=search_id,
        destination=destination,
        pick_range_type=pick_range_type,
        count_min=count_min,
        count_max=count_max,
        effect_type=str(raw_type if raw_type is not _MISSING else outer_type),
        target_card_id=str(
            _value(raw, "targetProduceCardId", "target_card_id", default="")
        ),
        target_upgrade=target_upgrade,
        target_effect_type=str(
            _value(
                raw,
                "targetExamEffectType",
                "target_effect_type",
                default=UNKNOWN_EFFECT,
            )
        ),
        pick_count_type=str(
            _value(raw, "pickCountType", "pick_count_type", default=UNKNOWN_PICK_COUNT)
        ),
        pick_count_reference_search_id=str(
            _value(
                raw,
                "pickCountReferenceProduceCardSearchId",
                "pick_count_reference_search_id",
                default="",
            )
        ),
        search2_id=str(
            _value(raw, "produceCardSearchId2", "search2_id", default="")
        ),
        second_pick_range_type=str(
            _value(
                raw,
                "pickRangeType2",
                "second_pick_range_type",
                default=UNKNOWN_PICK_RANGE,
            )
        ),
        second_count_min=_nonnegative_int(
            _value(raw, "pickCountMin2", "second_count_min", default=0),
            "pickCountMin2",
            error=CardMoveResolutionError,
        ),
        second_count_max=_nonnegative_int(
            _value(raw, "pickCountMax2", "second_count_max", default=0),
            "pickCountMax2",
            error=CardMoveResolutionError,
        ),
        second_pick_count_type=str(
            _value(
                raw,
                "pickCountType2",
                "second_pick_count_type",
                default=UNKNOWN_PICK_COUNT,
            )
        ),
        second_pick_count_reference_search_id=str(
            _value(
                raw,
                "pickCountReferenceProduceCardSearchId2",
                "second_pick_count_reference_search_id",
                default="",
            )
        ),
        card_status_enchant_id=str(
            _value(
                raw,
                "produceCardStatusEnchantId",
                "card_status_enchant_id",
                default="",
            )
        ),
        card_grow_effect_ids=_string_tuple(
            _value(raw, "produceCardGrowEffectIds", "card_grow_effect_ids", default=[]),
            "produceCardGrowEffectIds",
            error=CardMoveResolutionError,
        ),
        effect_group_ids=_string_tuple(
            _value(raw, "effectGroupIds", "effect_group_ids", default=[]),
            "effectGroupIds",
            error=CardMoveResolutionError,
        ),
        chain_effect_ids=_string_tuple(
            _value(raw, "chainProduceExamEffectIds", "chain_effect_ids", default=[]),
            "chainProduceExamEffectIds",
            error=CardMoveResolutionError,
        ),
    )


def try_resolve_plan2_card_move_contract(
    effect_row: Mapping[str, object] | sqlite3.Row,
    *,
    plan_type: str | None = None,
) -> Plan2CardMoveContract | CardMoveUnresolvedInput:
    try:
        return resolve_plan2_card_move_contract(effect_row, plan_type=plan_type)
    except CardMoveResolutionError as error:
        return CardMoveUnresolvedInput(error.code, error.detail or error.code)


def load_master_plan2_card_move_rows(
    database: Path = DEFAULT_DATABASE,
) -> tuple[dict[str, object], ...]:
    """Read the complete local Master CardMove catalog without changing coverage."""

    try:
        with sqlite3.connect(Path(database)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM effect WHERE effect_type = ? ORDER BY id",
                (CARD_MOVE_EFFECT_TYPE,),
            ).fetchall()
    except sqlite3.Error as error:
        raise CardMoveResolutionError("master-effect-read-failed") from error
    return tuple(dict(row) for row in rows)


def _assert_proven_contract(contract: Plan2CardMoveContract) -> None:
    if contract.destination not in {MOVE_HAND, MOVE_GRAVE, MOVE_LOST, MOVE_HOLD}:
        raise CardMoveResolutionError(
            "unsupported-card-move-destination", contract.destination
        )
    if contract.pick_range_type not in PICK_RANGE_TYPES:
        raise CardMoveResolutionError(
            "unsupported-card-move-pick-range", contract.pick_range_type
        )
    if contract.target_card_id:
        raise CardMoveResolutionError("unsupported-card-move-target-card")
    if contract.target_upgrade != 0:
        raise CardMoveResolutionError("unsupported-card-move-target-upgrade")
    if contract.target_effect_type != UNKNOWN_EFFECT:
        raise CardMoveResolutionError("unsupported-card-move-target-effect")
    if contract.pick_count_type != UNKNOWN_PICK_COUNT:
        raise CardMoveResolutionError("unsupported-card-move-pick-count-type")
    if contract.pick_count_reference_search_id:
        raise CardMoveResolutionError("unsupported-card-move-pick-count-reference")
    if contract.search2_id:
        raise CardMoveResolutionError("unsupported-card-move-second-search")
    if contract.second_pick_range_type != UNKNOWN_PICK_RANGE:
        raise CardMoveResolutionError("unsupported-card-move-second-pick-range")
    if contract.second_count_min != 0 or contract.second_count_max != 0:
        raise CardMoveResolutionError("unsupported-card-move-second-count")
    if contract.second_pick_count_type != UNKNOWN_PICK_COUNT:
        raise CardMoveResolutionError("unsupported-card-move-second-pick-count-type")
    if contract.second_pick_count_reference_search_id:
        raise CardMoveResolutionError("unsupported-card-move-second-pick-reference")
    if contract.card_status_enchant_id:
        raise CardMoveResolutionError("unsupported-card-move-status-enchant")
    if contract.card_grow_effect_ids:
        raise CardMoveResolutionError("unsupported-card-move-grow-effects")
    if contract.effect_group_ids:
        raise CardMoveResolutionError("unsupported-card-move-effect-groups")
    if contract.chain_effect_ids:
        raise CardMoveResolutionError("unsupported-card-move-chain")
    if contract.pick_range_type == PICK_ALL and contract.count_range != (0, 0):
        raise CardMoveResolutionError("unsupported-card-move-all-count")


def _coerce_selected_guids(
    selected_guids: Sequence[str] | None,
) -> tuple[str, ...]:
    if selected_guids is None:
        return ()
    if isinstance(selected_guids, (str, bytes)):
        raise CardMoveInputError("selected-guids-must-be-sequence")
    try:
        values = tuple(selected_guids)
    except TypeError as error:
        raise CardMoveInputError("selected-guids-must-be-sequence") from error
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise CardMoveInputError("invalid-selected-guid")
    if len(set(values)) != len(values):
        raise CardMoveInputError("duplicate-selected-guid")
    return values


def _select_targets(
    candidates: tuple[Plan3NativeCardMoveTarget, ...],
    contract: Plan2CardMoveContract,
    selected_guids: Sequence[str] | None,
) -> tuple[Plan3NativeCardMoveTarget, ...]:
    requested = _coerce_selected_guids(selected_guids)
    if contract.pick_range_type == PICK_ALL:
        if requested:
            raise CardMoveInputError("all-pick-does-not-accept-selection")
        return candidates
    if contract.pick_range_type == PICK_RANDOM:
        if requested:
            raise CardMoveInputError("random-pick-does-not-accept-selection")
        raise AssertionError("random selection is delegated separately")
    by_guid = {value.guid: value for value in candidates}
    missing = tuple(guid for guid in requested if guid not in by_guid)
    if missing:
        raise CardMoveInputError("selected-guid-not-candidate", missing[0])
    if not contract.count_min <= len(requested) <= contract.count_max:
        raise CardMoveInputError(
            "selected-count-out-of-range",
            f"count={len(requested)}:range={contract.count_range}",
        )
    requested_set = set(requested)
    # Android retains the search result order; click/input order is not a
    # second ordering channel.
    return tuple(value for value in candidates if value.guid in requested_set)


def apply_plan2_card_move(
    state: Plan3NativeState,
    contract: Plan2CardMoveContract,
    *,
    playing_guid: str,
    selected_guids: Sequence[str] | None = None,
    hold_limit: int = 2,
    hand_limit: int = 5,
    is_full_power: bool = False,
    lesson_type: str = "ProduceStepLessonType_LessonDance",
    support_upgrades: (
        Mapping[str, SupportUpgradeRuntimeInput]
        | Iterable[SupportUpgradeRuntimeInput]
    ) = (),
    support_card_searches: Mapping[str, ProduceCardSearchRule] | None = None,
    search: ProduceCardSearchRule | None = None,
    database: Path = DEFAULT_DATABASE,
    plan_type: str | None = None,
) -> Plan2CardMoveResult:
    """Execute one proven CardMove through the shared native primitive.

    The input state is immutable.  In particular, random-state advancement is
    held in a local delegated state until the final move succeeds; an error
    therefore publishes no partial zone or RNG mutation.
    """

    if not isinstance(state, Plan3NativeState):
        raise CardMoveInputError("state-must-be-plan3-native-state")
    if not isinstance(contract, Plan2CardMoveContract):
        raise CardMoveInputError("contract-must-be-plan2-card-move-contract")
    if plan_type is not None and plan_type not in PLAN2_COMMON_PLAN_TYPES:
        raise CardMoveResolutionError("unsupported-plan", str(plan_type))
    _text(playing_guid, "playing_guid", error=CardMoveInputError)
    _assert_proven_contract(contract)
    if support_card_searches is None:
        support_card_searches = {}
    if search is None:
        search = load_produce_card_search(contract.search_id, database)
    elif not isinstance(search, ProduceCardSearchRule):
        raise CardMoveInputError("search-must-be-produce-card-search-rule")
    if search.id != contract.search_id:
        raise CardMoveInputError("search-id-contract-mismatch")

    # All candidate and selection logic is deliberately delegated verbatim.
    candidates = state.card_move_candidates(search, playing_guid=playing_guid)
    state_for_move = state
    if contract.pick_range_type == PICK_RANDOM:
        if _coerce_selected_guids(selected_guids):
            raise CardMoveInputError("random-pick-does-not-accept-selection")
        if candidates:
            state_for_move, selected = state.resolve_random_card_move_targets(
                candidates,
                count_min=contract.count_min,
                count_max=contract.count_max,
            )
        else:
            # Native has no collection to sample.  A zero-candidate random
            # move is therefore an exact no-op, including the RNG cursor.
            selected = ()
    else:
        selected = _select_targets(candidates, contract, selected_guids)
    after = state_for_move.move_card_targets(
        selected,
        contract.destination,
        hold_limit=hold_limit,
        hand_limit=hand_limit,
        is_full_power=is_full_power,
        lesson_type=lesson_type,
        support_upgrades=support_upgrades,
        support_card_searches=support_card_searches,
    )
    return Plan2CardMoveResult(
        before=state,
        after=after,
        candidates=candidates,
        selected=selected,
        random_state_before=state.random_state,
        random_state_after=after.random_state,
    )


# Public aliases are intentional aliases, not look-alike implementations.
execute_plan2_card_move = apply_plan2_card_move
apply_card_move = apply_plan2_card_move


__all__ = [
    "CARD_MOVE_EFFECT_TYPE",
    "PLAN_COMMON",
    "PLAN2",
    "PLAN2_COMMON_PLAN_TYPES",
    "MOVE_HAND",
    "MOVE_GRAVE",
    "MOVE_LOST",
    "MOVE_HOLD",
    "PICK_ALL",
    "PICK_SELECT",
    "PICK_RANDOM",
    "SEARCH_DECK_GRAVE",
    "PLAN2_CARD_MOVE_LOST_RANDOM_EFFECT_ID",
    "PLAN2_CARD_MOVE_LOST_RANDOM_SEARCH_ID",
    "Plan2CardMoveCard",
    "Plan2CardMoveContract",
    "Plan2CardMoveResult",
    "Plan2CardMoveState",
    "Plan2CardMoveTarget",
    "CardMoveError",
    "CardMoveResolutionError",
    "CardMoveInputError",
    "CardMoveUnresolvedInput",
    "Plan3NativeCard",
    "Plan3NativeCardMoveTarget",
    "Plan3NativeState",
    "Plan3NativeStateError",
    "load_master_plan2_card_move_rows",
    "resolve_plan2_card_move_contract",
    "try_resolve_plan2_card_move_contract",
    "assert_plan2_card_move_lost_random_direct_contract",
    "assert_plan2_card_move_lost_random_direct_search",
    "apply_plan2_card_move",
    "execute_plan2_card_move",
    "apply_card_move",
]
