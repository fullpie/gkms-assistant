"""Exact standalone Plan2 ``ExamReviewPerSearchCount`` evaluator.

This module is deliberately a leaf.  It resolves one version-pinned Master
row, projects the native Deck+Grave search over the shared
``Plan3NativeState`` zones, and returns an immutable Review transition.  It
does not modify ``Plan2State``, ``plan2_core_runtime``, Plan3, coverage, GUI,
or any outer command flow.

The Android v3.2.3 executor does the following for the exact row covered
here::

    candidates = GetSearchCardList(search, context, null, null)
    per_search = ceil_f32(FloatFromPermil(value2) * f32(candidates.Count))
    TryAddReviewStatus(value1 + per_search, context, isFix=false)
    if TryAddReviewStatus returned true:
        before/after Review difference type 31 is appended

``GetSearchCardList`` returns a non-null ordered collection for the proved
DeckGrave shape.  Its source order is Deck followed by Grave; Hand, Lost,
Playing, and Hold are not part of this exact search.  Card GUIDs are retained
in the candidate records for diagnostics, but they do not enter the count or
the arithmetic and no RNG call is made.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .card_search import (
    ProduceCardSearchRule,
    load_produce_card_search,
    match_plan3_card_move_search,
    validate_plan3_card_move_search,
)
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    INT32_MAX,
    INT32_MIN,
    NativeFormulaDomainError,
    ceil_f32_to_i32,
    f32,
    permille_to_f32,
)
from .plan3_native_state import (
    Plan3NativeCardMoveTarget,
    Plan3NativeState,
)


EFFECT_TYPE = "ProduceExamEffectType_ExamReviewPerSearchCount"
REVIEW_EFFECT_TYPE = "ProduceExamEffectType_ExamReview"
REVIEW_DIFFERENCE_TYPE_VALUE = 31
EFFECT_ID = (
    "e_effect-exam_review_per_search_count-2000-"
    "p_card_search-deck_grave-all-0_0"
)
SEARCH_ID = "p_card_search-deck_grave"
TARGET_CARD_ID = "p_card-02-ido-3_146"
TARGET_UPGRADES = (0, 1, 2, 3)
TARGET_EFFECT_SLOT = 2
TARGET_PLAN_TYPE = "ProducePlanType_Plan2"
DECK_GRAVE_POSITION = "ProduceCardPositionType_DeckGrave"
PICK_RANGE_ALL = "ProducePickRangeType_All"
UNKNOWN_PICK_RANGE = "ProducePickRangeType_Unknown"
UNKNOWN_PICK_COUNT = "ProducePickCountType_Unknown"
UNKNOWN_MOVE_POSITION = "ProduceCardMovePositionType_Unknown"
UNKNOWN_EXAM_EFFECT = "ProduceExamEffectType_Unknown"
REVIEW_VALUE2_PERMILLE = 2000


class Plan2ReviewPerSearchCountContractError(ValueError):
    """A Master/runtime shape outside the exact native boundary."""


def _plain_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Plan2ReviewPerSearchCountContractError(
            f"{label} must be an integer"
        )
    return value


def _i32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _require_i32(value: object, label: str) -> int:
    result = _plain_int(value, label)
    if not INT32_MIN <= result <= INT32_MAX:
        raise Plan2ReviewPerSearchCountContractError(
            f"{label} is outside Int32: {result}"
        )
    return result


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise Plan2ReviewPerSearchCountContractError(
            f"{label} must be non-empty text"
        )
    return value


def _require_raw_equal(
    raw: dict[str, object], key: str, expected: object, effect_id: str
) -> None:
    if raw.get(key) != expected:
        raise Plan2ReviewPerSearchCountContractError(
            f"{effect_id}: {key} must be {expected!r}, got {raw.get(key)!r}"
        )


@dataclass(frozen=True, slots=True)
class Plan2ReviewPerSearchCountEffect:
    """Native executable fields from the one exact Master effect row."""

    effect_id: str
    effect_type: str
    value1: int
    value2: int
    search_id: str
    pick_range_type: str
    effect_count: int = 0
    effect_turn: int = 0

    def __post_init__(self) -> None:
        if self.effect_id != EFFECT_ID:
            raise Plan2ReviewPerSearchCountContractError(
                f"unsupported effect id: {self.effect_id!r}"
            )
        if self.effect_type != EFFECT_TYPE:
            raise Plan2ReviewPerSearchCountContractError(
                f"{self.effect_id}: unexpected effect type"
            )
        if self.value1 != 0 or self.value2 != REVIEW_VALUE2_PERMILLE:
            raise Plan2ReviewPerSearchCountContractError(
                f"{self.effect_id}: unsupported value1/value2 shape"
            )
        for label, value in (
            ("value1", self.value1),
            ("value2", self.value2),
            ("effect_count", self.effect_count),
            ("effect_turn", self.effect_turn),
        ):
            _require_i32(value, label)
        if self.effect_count != 0 or self.effect_turn != 0:
            raise Plan2ReviewPerSearchCountContractError(
                f"{self.effect_id}: count/turn must be zero"
            )
        if self.search_id != SEARCH_ID:
            raise Plan2ReviewPerSearchCountContractError(
                f"{self.effect_id}: unexpected search id"
            )
        if self.pick_range_type != PICK_RANGE_ALL:
            raise Plan2ReviewPerSearchCountContractError(
                f"{self.effect_id}: pick range must be All"
            )


@dataclass(frozen=True, slots=True)
class Plan2ReviewPerSearchCountTarget:
    """One target card version and its native play-effect slot."""

    card_id: str
    upgrade_count: int
    plan_type: str
    effect_slot: int
    ordered_effect_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.card_id, "card_id")
        if self.card_id != TARGET_CARD_ID:
            raise Plan2ReviewPerSearchCountContractError(
                f"unexpected target card: {self.card_id}"
            )
        if self.upgrade_count not in TARGET_UPGRADES:
            raise Plan2ReviewPerSearchCountContractError(
                f"unsupported target upgrade: {self.upgrade_count}"
            )
        if self.plan_type != TARGET_PLAN_TYPE:
            raise Plan2ReviewPerSearchCountContractError(
                f"{self.card_id}+{self.upgrade_count}: unexpected plan"
            )
        if self.effect_slot != TARGET_EFFECT_SLOT:
            raise Plan2ReviewPerSearchCountContractError(
                f"{self.card_id}+{self.upgrade_count}: unexpected effect slot"
            )
        effect_ids = tuple(self.ordered_effect_ids)
        if len(effect_ids) != 4 or any(
            not isinstance(effect_id, str) or not effect_id for effect_id in effect_ids
        ):
            raise Plan2ReviewPerSearchCountContractError(
                f"{self.card_id}+{self.upgrade_count}: unsupported effect list"
            )
        if effect_ids.count(EFFECT_ID) != 1 or effect_ids[TARGET_EFFECT_SLOT] != EFFECT_ID:
            raise Plan2ReviewPerSearchCountContractError(
                f"{self.card_id}+{self.upgrade_count}: effect order mismatch"
            )
        object.__setattr__(self, "ordered_effect_ids", effect_ids)


@dataclass(frozen=True, slots=True)
class Plan2ReviewPerSearchCountContract:
    """Resolved exact effect/search/card boundary."""

    effect: Plan2ReviewPerSearchCountEffect
    search: ProduceCardSearchRule
    targets: tuple[Plan2ReviewPerSearchCountTarget, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.effect, Plan2ReviewPerSearchCountEffect):
            raise TypeError("effect must be Plan2ReviewPerSearchCountEffect")
        if not isinstance(self.search, ProduceCardSearchRule):
            raise TypeError("search must be ProduceCardSearchRule")
        invalid = validate_plan3_card_move_search(self.search)
        if invalid is not None:
            raise Plan2ReviewPerSearchCountContractError(invalid)
        if self.search.id != SEARCH_ID:
            raise Plan2ReviewPerSearchCountContractError(
                "unexpected search id"
            )
        if (
            self.search.card_position_type != DECK_GRAVE_POSITION
            or self.search.limit_count != 0
            or self.search.produce_card_ids
            or self.search.upgrade_counts
        ):
            raise Plan2ReviewPerSearchCountContractError(
                "search is not the exact neutral DeckGrave All shape"
            )
        targets = tuple(self.targets)
        if any(
            not isinstance(target, Plan2ReviewPerSearchCountTarget)
            for target in targets
        ):
            raise TypeError("targets must contain exact target values")
        if tuple(target.upgrade_count for target in targets) != TARGET_UPGRADES:
            raise Plan2ReviewPerSearchCountContractError(
                "target versions must be upgrades 0..3 in order"
            )
        object.__setattr__(self, "targets", targets)

    @property
    def affected_version_count(self) -> int:
        return len(self.targets)

    @property
    def direct_version_count(self) -> int:
        """All resolved target versions have the exact direct effect slot."""

        return sum(
            target.effect_slot == TARGET_EFFECT_SLOT for target in self.targets
        )

    @property
    def affected_direct(self) -> tuple[int, int]:
        return self.affected_version_count, self.direct_version_count


@dataclass(frozen=True, slots=True)
class ReviewAdditiveLayer:
    """One active native ReviewAdditive value, in status-list order."""

    permille: int

    def __post_init__(self) -> None:
        _require_i32(self.permille, "review_additive_layer.permille")


@dataclass(frozen=True, slots=True)
class Plan2ReviewPerSearchCountRequest:
    """Pure context values read by the native status-add path.

    ``review_status_present`` is separate from ``review_before`` because the
    native collection can retain an active zero-valued Review status.  The
    default is an existing status so callers can use ``review_before`` for the
    common merge case; pass ``False`` for an absent status.
    """

    state: Plan3NativeState
    review_before: int = 0
    review_status_present: bool = True
    review_additive_fix: int = 0
    review_additive_layers: tuple[ReviewAdditiveLayer, ...] = ()
    status_addition_blocked: bool = False
    context_present: bool = True
    enchant_trigger_active: bool = True
    difference_list_available: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.state, Plan3NativeState):
            raise TypeError("state must be Plan3NativeState")
        _require_i32(self.review_before, "review_before")
        _require_i32(self.review_additive_fix, "review_additive_fix")
        if type(self.review_status_present) is not bool:
            raise TypeError("review_status_present must be a boolean")
        if not self.review_status_present and self.review_before != 0:
            raise Plan2ReviewPerSearchCountContractError(
                "an absent Review status must read as zero"
            )
        layers = tuple(self.review_additive_layers)
        if any(not isinstance(layer, ReviewAdditiveLayer) for layer in layers):
            raise TypeError(
                "review_additive_layers must contain ReviewAdditiveLayer values"
            )
        object.__setattr__(self, "review_additive_layers", layers)
        for label, value in (
            ("status_addition_blocked", self.status_addition_blocked),
            ("context_present", self.context_present),
            ("enchant_trigger_active", self.enchant_trigger_active),
            ("difference_list_available", self.difference_list_available),
        ):
            if type(value) is not bool:
                raise TypeError(f"{label} must be a boolean")


@dataclass(frozen=True, slots=True)
class Plan2ReviewStatusChange:
    """The status collection mutation, before the effect difference."""

    review_before: int
    review_after: int
    status_present_before: bool
    status_present_after: bool
    delta: int


@dataclass(frozen=True, slots=True)
class Plan2ReviewPerSearchCountDifference:
    """Native ``ExamEffectDifferenceData.SetStatusDifference`` payload."""

    before: int
    after: int
    effect_type: str = REVIEW_EFFECT_TYPE
    effect_type_value: int = REVIEW_DIFFERENCE_TYPE_VALUE
    is_consume: bool = False

    def __post_init__(self) -> None:
        _require_i32(self.before, "difference.before")
        _require_i32(self.after, "difference.after")
        if self.effect_type != REVIEW_EFFECT_TYPE:
            raise Plan2ReviewPerSearchCountContractError(
                "difference must target ExamReview"
            )
        if self.effect_type_value != REVIEW_DIFFERENCE_TYPE_VALUE:
            raise Plan2ReviewPerSearchCountContractError(
                "difference type must be 31"
            )
        if self.is_consume:
            raise Plan2ReviewPerSearchCountContractError(
                "Review gain difference cannot be a consume difference"
            )


@dataclass(frozen=True, slots=True)
class Plan2ReviewPerSearchCountTransition:
    """Immutable exact projection of one native executor invocation."""

    before_state: Plan3NativeState
    after_state: Plan3NativeState
    contract: Plan2ReviewPerSearchCountContract
    request: Plan2ReviewPerSearchCountRequest
    candidates: tuple[Plan3NativeCardMoveTarget, ...]
    search_count: int
    value2_rate: float
    search_count_value: int
    value1_plus_search_count_value: int
    additive_fix: int
    additive_multiple: float
    review_delta: int
    review_before: int
    review_after: int
    status_present_before: bool
    status_present_after: bool
    status_mutated: bool
    try_add_returned: bool
    status_change: Plan2ReviewStatusChange | None
    difference: Plan2ReviewPerSearchCountDifference | None
    random_state_before: int
    random_state_after: int

    @property
    def difference_emitted(self) -> bool:
        return self.difference is not None

    @property
    def card_guids(self) -> tuple[str, ...]:
        return tuple(candidate.guid for candidate in self.candidates)


def _read_effect_row(effect_id: str, database: Path) -> Plan2ReviewPerSearchCountEffect:
    database = Path(database).resolve()
    try:
        with closing(
            sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        ) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM effect WHERE id = ?", (effect_id,)
            ).fetchone()
    except sqlite3.Error as error:
        raise Plan2ReviewPerSearchCountContractError(
            f"effect table unavailable: {database}"
        ) from error
    if row is None:
        raise Plan2ReviewPerSearchCountContractError(
            f"unknown Master effect: {effect_id}"
        )
    if effect_id != EFFECT_ID:
        raise Plan2ReviewPerSearchCountContractError(
            f"unsupported effect id: {effect_id!r}"
        )

    values = {
        "effect_type": str(row["effect_type"]),
        "value1": _require_i32(int(row["value1"]), "value1"),
        "value2": _require_i32(int(row["value2"]), "value2"),
        "effect_count": _require_i32(int(row["effect_count"]), "effect_count"),
        "effect_turn": _require_i32(int(row["effect_turn"]), "effect_turn"),
    }
    try:
        raw = json.loads(str(row["raw_json"]))
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise Plan2ReviewPerSearchCountContractError(
            f"{effect_id}: raw_json is invalid"
        ) from error
    if not isinstance(raw, dict):
        raise Plan2ReviewPerSearchCountContractError(
            f"{effect_id}: raw_json must be an object"
        )
    expected_raw = (
        ("id", effect_id),
        ("effectType", EFFECT_TYPE),
        ("effectValue1", 0),
        ("effectValue2", REVIEW_VALUE2_PERMILLE),
        ("effectCount", 0),
        ("effectTurn", 0),
        ("produceCardSearchId", SEARCH_ID),
        ("produceCardSearchId2", ""),
        ("pickRangeType", PICK_RANGE_ALL),
        ("pickRangeType2", UNKNOWN_PICK_RANGE),
        ("pickCountType", UNKNOWN_PICK_COUNT),
        ("pickCountType2", UNKNOWN_PICK_COUNT),
        ("pickCountMin", 0),
        ("pickCountMax", 0),
        ("pickCountMin2", 0),
        ("pickCountMax2", 0),
        ("movePositionType", UNKNOWN_MOVE_POSITION),
        ("targetProduceCardId", ""),
        ("targetUpgradeCount", 0),
        ("targetExamEffectType", UNKNOWN_EXAM_EFFECT),
        ("chainProduceExamEffectId", ""),
        ("chainProduceExamEffectIds", []),
        ("produceCardStatusEnchantId", ""),
        ("effectGroupIds", ["effect_group-visible-exam_review-000"]),
    )
    for key, expected in expected_raw:
        _require_raw_equal(raw, key, expected, effect_id)
    if values["effect_type"] != EFFECT_TYPE:
        raise Plan2ReviewPerSearchCountContractError(
            f"{effect_id}: SQL effect type mismatch"
        )
    if values["value1"] != 0 or values["value2"] != REVIEW_VALUE2_PERMILLE:
        raise Plan2ReviewPerSearchCountContractError(
            f"{effect_id}: SQL value mismatch"
        )
    return Plan2ReviewPerSearchCountEffect(
        effect_id=effect_id,
        effect_type=values["effect_type"],
        value1=values["value1"],
        value2=values["value2"],
        search_id=SEARCH_ID,
        pick_range_type=PICK_RANGE_ALL,
        effect_count=values["effect_count"],
        effect_turn=values["effect_turn"],
    )


def _read_targets(
    database: Path,
) -> tuple[Plan2ReviewPerSearchCountTarget, ...]:
    database = Path(database).resolve()
    try:
        with closing(
            sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        ) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT id, upgrade_count, plan_type, play_effects_json
                  FROM card
                 WHERE id = ? AND upgrade_count IN (0, 1, 2, 3)
                 ORDER BY upgrade_count
                """,
                (TARGET_CARD_ID,),
            ).fetchall()
    except sqlite3.Error as error:
        raise Plan2ReviewPerSearchCountContractError(
            f"card table unavailable: {database}"
        ) from error
    if tuple(int(row["upgrade_count"]) for row in rows) != TARGET_UPGRADES:
        raise Plan2ReviewPerSearchCountContractError(
            f"{TARGET_CARD_ID}: Master target versions are not exactly 0..3"
        )

    result: list[Plan2ReviewPerSearchCountTarget] = []
    for row in rows:
        upgrade = int(row["upgrade_count"])
        try:
            effects = json.loads(str(row["play_effects_json"]))
        except (TypeError, json.JSONDecodeError) as error:
            raise Plan2ReviewPerSearchCountContractError(
                f"{TARGET_CARD_ID}+{upgrade}: play effects are invalid"
            ) from error
        if not isinstance(effects, list):
            raise Plan2ReviewPerSearchCountContractError(
                f"{TARGET_CARD_ID}+{upgrade}: play effects must be a list"
            )
        effect_ids: list[str] = []
        for index, item in enumerate(effects):
            if not isinstance(item, dict):
                raise Plan2ReviewPerSearchCountContractError(
                    f"{TARGET_CARD_ID}+{upgrade}: effect #{index} is not an object"
                )
            effect_id = item.get("produceExamEffectId")
            if not isinstance(effect_id, str) or not effect_id:
                raise Plan2ReviewPerSearchCountContractError(
                    f"{TARGET_CARD_ID}+{upgrade}: effect #{index} has no id"
                )
            effect_ids.append(effect_id)
        result.append(
            Plan2ReviewPerSearchCountTarget(
                card_id=str(row["id"]),
                upgrade_count=upgrade,
                plan_type=str(row["plan_type"]),
                effect_slot=(
                    effect_ids.index(EFFECT_ID)
                    if EFFECT_ID in effect_ids
                    else -1
                ),
                ordered_effect_ids=tuple(effect_ids),
            )
        )
    return tuple(result)


def resolve_plan2_review_per_search_count(
    effect_id: str = EFFECT_ID,
    database: Path = DEFAULT_DATABASE,
) -> Plan2ReviewPerSearchCountContract:
    """Resolve and strictly validate the exact current-Master contract."""

    if effect_id != EFFECT_ID:
        raise Plan2ReviewPerSearchCountContractError(
            f"unsupported effect id: {effect_id!r}"
        )
    effect = _read_effect_row(effect_id, database)
    try:
        search = load_produce_card_search(SEARCH_ID, Path(database))
    except (KeyError, OSError, TypeError, ValueError, IndexError, sqlite3.Error) as error:
        raise Plan2ReviewPerSearchCountContractError(
            f"cannot load exact search contract from {database}"
        ) from error
    invalid = validate_plan3_card_move_search(search)
    if invalid is not None:
        raise Plan2ReviewPerSearchCountContractError(invalid)
    if (
        search.id != SEARCH_ID
        or search.card_position_type != DECK_GRAVE_POSITION
        or search.limit_count != 0
        or search.produce_card_ids
        or search.upgrade_counts
    ):
        raise Plan2ReviewPerSearchCountContractError(
            "search is not the exact neutral DeckGrave All shape"
        )
    targets = _read_targets(database)
    return Plan2ReviewPerSearchCountContract(effect, search, targets)


def load_plan2_review_per_search_count(
    effect_id: str = EFFECT_ID,
    database: Path = DEFAULT_DATABASE,
) -> Plan2ReviewPerSearchCountContract:
    """Alias emphasizing the read-only Master loader boundary."""

    return resolve_plan2_review_per_search_count(effect_id, database)


def _resolve_deck_grave_candidates(
    state: Plan3NativeState,
    search: ProduceCardSearchRule,
) -> tuple[Plan3NativeCardMoveTarget, ...]:
    """Project shared ordered zones through the shared search matcher."""

    invalid = validate_plan3_card_move_search(search)
    if invalid is not None:
        raise Plan2ReviewPerSearchCountContractError(invalid)
    if search.card_position_type != DECK_GRAVE_POSITION or search.limit_count != 0:
        raise Plan2ReviewPerSearchCountContractError(
            "runtime search is not exact neutral DeckGrave All"
        )

    # This is the same native source order used by Plan3NativeState's shared
    # CardMove primitive.  The playing card is a separate context field in
    # native ExamEffectCalculateContext and is intentionally not synthesized
    # into this settled-zone snapshot.
    result: list[Plan3NativeCardMoveTarget] = []
    for source_zone, cards in (("draw", state.deck), ("discard", state.grave)):
        for source_index, card in enumerate(cards):
            matches, reason = match_plan3_card_move_search(
                search, card.card_id, card.effective_upgrade
            )
            if reason is not None:
                raise Plan2ReviewPerSearchCountContractError(reason)
            if matches:
                result.append(
                    Plan3NativeCardMoveTarget(
                        guid=card.guid,
                        card=card,
                        source_zone=source_zone,
                        source_index=source_index,
                    )
                )
    return tuple(result)


def _native_ceil_f32(value: float, label: str) -> int:
    try:
        value = f32(value)
        if not math.isfinite(value):
            raise NativeFormulaDomainError(f"{label} is non-finite")
        return ceil_f32_to_i32(value)
    except (NativeFormulaDomainError, OverflowError, ValueError) as error:
        raise Plan2ReviewPerSearchCountContractError(
            f"{label} is outside the finite native Int32 domain"
        ) from error


def _native_review_additive_multiple(
    layers: tuple[ReviewAdditiveLayer, ...],
) -> float:
    multiple = f32(1.0)
    for layer in layers:
        term = permille_to_f32(layer.permille)
        multiple = f32(multiple + term)
    return multiple


def evaluate_plan2_review_per_search_count(
    request: Plan2ReviewPerSearchCountRequest,
    contract: Plan2ReviewPerSearchCountContract,
) -> Plan2ReviewPerSearchCountTransition:
    """Evaluate one exact executor call without mutating any outer state."""

    if not isinstance(request, Plan2ReviewPerSearchCountRequest):
        raise TypeError("request must be Plan2ReviewPerSearchCountRequest")
    if not isinstance(contract, Plan2ReviewPerSearchCountContract):
        raise TypeError("contract must be Plan2ReviewPerSearchCountContract")
    candidates = _resolve_deck_grave_candidates(request.state, contract.search)
    search_count = len(candidates)
    try:
        value2_rate = permille_to_f32(contract.effect.value2)
        search_count_value = _native_ceil_f32(
            f32(value2_rate * f32(search_count)),
            "value2-per-search product",
        )
    except (NativeFormulaDomainError, OverflowError, ValueError) as error:
        raise Plan2ReviewPerSearchCountContractError(
            "value2-per-search product is outside the native domain"
        ) from error
    value1_plus_search_count_value = _i32(
        contract.effect.value1 + search_count_value
    )

    review_before = request.review_before
    review_after = review_before
    status_present_after = request.review_status_present
    status_mutated = False
    try_add_returned = False
    status_change: Plan2ReviewStatusChange | None = None
    difference: Plan2ReviewPerSearchCountDifference | None = None
    additive_fix = 0
    additive_multiple = f32(1.0)
    review_delta = 0

    # ExecuteEffect explicitly returns before touching the status collection
    # when the calculate context is null.  A valid context then enters
    # TryAddReviewStatus, whose block gate precedes all status mutation.
    if request.context_present and not request.status_addition_blocked:
        additive_fix = request.review_additive_fix
        try:
            additive_multiple = _native_review_additive_multiple(
                request.review_additive_layers
            )
            sum_value = _i32(value1_plus_search_count_value + additive_fix)
            review_delta = _native_ceil_f32(
                f32(f32(sum_value) * additive_multiple),
                "Review status delta",
            )
        except (NativeFormulaDomainError, OverflowError, ValueError) as error:
            raise Plan2ReviewPerSearchCountContractError(
                "Review status delta is outside the native domain"
            ) from error
        review_after = _i32(review_before + review_delta)
        status_present_after = True
        status_mutated = True
        status_change = Plan2ReviewStatusChange(
            review_before=review_before,
            review_after=review_after,
            status_present_before=request.review_status_present,
            status_present_after=True,
            delta=review_delta,
        )
        # IsEnchantTriggerActive controls an optional enchant-phase bookkeeping
        # path inside TryAddReviewStatus.  Both its false fast path and its
        # true bookkeeping path return the same ``!IsBlockAddStatus`` result;
        # it does not suppress this Review difference or roll back the status.
        try_add_returned = True
        if try_add_returned and request.difference_list_available:
            difference = Plan2ReviewPerSearchCountDifference(
                before=review_before,
                after=review_after,
            )

    return Plan2ReviewPerSearchCountTransition(
        before_state=request.state,
        after_state=request.state,
        contract=contract,
        request=request,
        candidates=candidates,
        search_count=search_count,
        value2_rate=value2_rate,
        search_count_value=search_count_value,
        value1_plus_search_count_value=value1_plus_search_count_value,
        additive_fix=additive_fix,
        additive_multiple=additive_multiple,
        review_delta=review_delta,
        review_before=review_before,
        review_after=review_after,
        status_present_before=request.review_status_present,
        status_present_after=status_present_after,
        status_mutated=status_mutated,
        try_add_returned=try_add_returned,
        status_change=status_change,
        difference=difference,
        random_state_before=request.state.random_state,
        random_state_after=request.state.random_state,
    )


ReviewStatusCallback = Callable[[Plan2ReviewStatusChange], None]
ReviewDifferenceCallback = Callable[[Plan2ReviewPerSearchCountDifference], None]


def apply_plan2_review_per_search_count(
    request: Plan2ReviewPerSearchCountRequest,
    contract: Plan2ReviewPerSearchCountContract,
    *,
    on_status_changed: Callable[[Plan2ReviewStatusChange], None] | None = None,
    on_difference: Callable[[Plan2ReviewPerSearchCountDifference], None]
    | None = None,
) -> Plan2ReviewPerSearchCountTransition:
    """Thin callback adapter around the pure evaluator.

    Hook order follows the native boundary: status mutation first, then the
    Review difference append.  The adapter does not execute reactive effects,
    alter card zones, allocate RNG, or call the outer
    ``EffectDifferenceExecutedAsync`` hook; the caller owns that boundary.
    """

    transition = evaluate_plan2_review_per_search_count(request, contract)
    if transition.status_change is not None and on_status_changed is not None:
        on_status_changed(transition.status_change)
    if transition.difference is not None and on_difference is not None:
        on_difference(transition.difference)
    return transition


# Descriptive aliases keep the public surface easy to discover without
# introducing a second implementation or an outer-layer registration point.
resolve_review_per_search_count = resolve_plan2_review_per_search_count
evaluate_review_per_search_count = evaluate_plan2_review_per_search_count
apply_review_per_search_count = apply_plan2_review_per_search_count


__all__ = [
    "DECK_GRAVE_POSITION",
    "EFFECT_ID",
    "EFFECT_TYPE",
    "PICK_RANGE_ALL",
    "REVIEW_DIFFERENCE_TYPE_VALUE",
    "REVIEW_EFFECT_TYPE",
    "REVIEW_VALUE2_PERMILLE",
    "SEARCH_ID",
    "TARGET_CARD_ID",
    "TARGET_EFFECT_SLOT",
    "TARGET_UPGRADES",
    "Plan2ReviewPerSearchCountContract",
    "Plan2ReviewPerSearchCountContractError",
    "Plan2ReviewPerSearchCountDifference",
    "Plan2ReviewPerSearchCountEffect",
    "Plan2ReviewPerSearchCountRequest",
    "Plan2ReviewPerSearchCountTarget",
    "Plan2ReviewPerSearchCountTransition",
    "Plan2ReviewStatusChange",
    "ReviewAdditiveLayer",
    "apply_plan2_review_per_search_count",
    "apply_review_per_search_count",
    "evaluate_plan2_review_per_search_count",
    "evaluate_review_per_search_count",
    "load_plan2_review_per_search_count",
    "resolve_plan2_review_per_search_count",
    "resolve_review_per_search_count",
]
