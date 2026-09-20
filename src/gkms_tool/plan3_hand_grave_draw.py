"""Exact standalone HandGraveCountCardDraw executor for Android v3.2.3.

The recovered native body snapshots either the selected target cards or the
whole Hand, moves that snapshot to Grave, draws the snapshot count, and adds
the returned (actual) draw count to ``TotalEffectDrawCardCount``.  The sole PC
Master row has ``value1 == 0``, so only the whole-Hand branch is executable
here.  Richer/unrecognized Master rows remain typed unresolved.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3
from typing import NoReturn, TypeAlias

from .audition_support_runtime import SupportUpgradeRuntimeInput
from .card_search import ProduceCardSearchRule
from .master_db import DEFAULT_DATABASE
from .plan3_native_state import (
    Plan3NativeDrawTransition,
    Plan3NativeState,
    Plan3NativeStateError,
)


MASTER_EFFECT_ID = "e_effect-exam_hand_grave_count_card_draw"
MASTER_EFFECT_TYPE = "ProduceExamEffectType_ExamHandGraveCountCardDraw"
MASTER_ENUM_NAME = "ExamHandGraveCountCardDraw"
MASTER_ENUM_VALUE = 98

NATIVE_EXECUTOR_TYPE = "Campus.InGame.Exam.HandGraveCountCardDrawEffectExecutor"
NATIVE_TYPE_INDEX = 3697
NATIVE_TYPE_GLOBAL_VA = 0xE7570D0
NATIVE_USAGE_CELL_VA = 0xEB2BA88
NATIVE_CONSTRUCTOR_VA = 0x7E7E36C
NATIVE_CONSTRUCTOR_TOKEN = 0x0600485F
NATIVE_EXECUTE_VA = 0x7E7E424
NATIVE_EXECUTE_TOKEN = 0x06004860
NATIVE_EXECUTE_RANGE = (0x7E7E424, 0x7E7E848)
NATIVE_SELECT_WITH_INDEX_VA = 0x7E7E8C0
NATIVE_SELECT_CARD_VA = 0x7E7E930
NATIVE_SELECTED_INDEX_PREDICATE_VA = 0x7E7E938
NATIVE_CREATE_CARD_POSITION_LIST_VA = 0x8095FC8
NATIVE_MOVE_CARD_VA = 0x8097808
NATIVE_DRAW_CARD_VA = 0x809DE54
NATIVE_ADD_TOTAL_EFFECT_DRAW_COUNT_VA = 0x7EBC148

HAND_POSITION_ENUM_VALUE = 2
GRAVE_MOVE_ENUM_VALUE = 5
NATIVE_OPERATION_ORDER = (
    "snapshot-hand-position-data",
    "snapshot-list-count",
    "move-snapshot-hand-to-grave",
    "draw-snapshot-count",
    "add-returned-count-to-total-effect-draw-card-count",
)
DRAW_COUNT_FORMULA = (
    "requested = Hand.Count at ExecuteEffect; actual = "
    "min(requested, Deck.Count + Grave.Count after Hand-to-Grave, "
    "HandLimit - Hand.Count after Hand-to-Grave)"
)
SIMULATION_BEHAVIOR = (
    "no executor-local simulation branch; execute the same body against the "
    "caller-provided context/state copy"
)

MASTER_EFFECT_GROUP_ID = (
    "effect_group-visible-exam_hand_grave_count_card_draw-000"
)

# These are the explicitly scoped card references for this contract.  They
# are evidence of Master linkage, not inputs to the unresolved native formula.
AFFECTED_CARD_IDS = (
    "p_card-00-men-3_005",
    "p_card-03-sup-3_194",
)
AFFECTED_CARD_VERSIONS = tuple(
    (card_id, upgrade)
    for card_id in AFFECTED_CARD_IDS
    for upgrade in range(4)
)

# No native-body evidence is missing for the exact value1-zero Master row.
# These compatibility names now describe the smallest inputs required by the
# standalone state transition, rather than missing reverse-engineering work.
REQUIRED_NATIVE_INPUTS = (
    "ordered_guid_zones",
    "hand_limit",
    "shared_random_state",
    "lesson_type_and_hand_add_support_runtime",
)
REQUIRED_NATIVE_FORMULA = DRAW_COUNT_FORMULA
UNRESOLVED_REASON = (
    "only the exact value1-zero PC Master row is proven; a different Master "
    "shape requires separate native and target-selection evidence"
)

SupportInputs: TypeAlias = (
    Mapping[str, SupportUpgradeRuntimeInput]
    | Iterable[SupportUpgradeRuntimeInput]
)

_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1

_MASTER_ROW_KEYS = frozenset(
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


class HandGraveDrawContractError(ValueError):
    """A Master/native binding is outside the proven contract."""


class HandGraveDrawUnresolvedError(RuntimeError):
    """Execution was refused because the native formula is unresolved."""


def _text(value: object, label: str, *, non_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    if non_empty and not value:
        raise HandGraveDrawContractError(f"{label} must be non-empty")
    return value


def _int32(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if not _INT32_MIN <= value <= _INT32_MAX:
        raise HandGraveDrawContractError(f"{label} is outside Int32: {value}")
    return value


def _text_tuple(value: object, label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise TypeError(f"{label} must be a list or tuple of text")
    result = tuple(_text(item, f"{label} item", non_empty=True) for item in value)
    return result


def _description_types(
    value: object,
    label: str,
    effect_id: str,
) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise TypeError(f"{label} must be a list of mappings")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise TypeError(f"{label}[{index}] must be a mapping")
        if item.get("originProduceExamEffectId") != effect_id:
            raise HandGraveDrawContractError(
                f"{label}[{index}] has the wrong effect lineage"
            )
        result.append(
            _text(item.get("produceDescriptionType"), f"{label}[{index}].type", non_empty=True)
        )
    return tuple(result)


@dataclass(frozen=True, slots=True)
class HandGraveDrawCardReference:
    """One explicitly scoped Master card/version reference."""

    card_id: str
    upgrade_count: int
    play_effect_index: int

    def __post_init__(self) -> None:
        _text(self.card_id, "card_id", non_empty=True)
        upgrade = _int32(self.upgrade_count, "upgrade_count")
        index = _int32(self.play_effect_index, "play_effect_index")
        if upgrade < 0 or index < 0:
            raise HandGraveDrawContractError("card reference indexes must be non-negative")


@dataclass(frozen=True, slots=True)
class HandGraveDrawContract:
    """Execution-relevant projection of the exact Master effect row.

    The zero/default payload fields are retained because they are part of the
    proven row shape.  They are not interpreted as a draw count.
    """

    effect_id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    target_card_id: str
    target_upgrade_count: int
    target_effect_type: str
    search_id: str
    move_position_type: str
    pick_range_type: str
    pick_reference_search_id: str
    pick_count_type: str
    pick_count_min: int
    pick_count_max: int
    second_search_id: str
    second_pick_range_type: str
    second_pick_reference_search_id: str
    second_pick_count_type: str
    second_pick_count_min: int
    second_pick_count_max: int
    chain_effect_id: str
    chain_effect_ids: tuple[str, ...]
    exam_status_enchant_id: str
    card_status_enchant_id: str
    grow_effect_ids: tuple[str, ...]
    effect_group_ids: tuple[str, ...]
    executor_type: str = NATIVE_EXECUTOR_TYPE
    enum_name: str = MASTER_ENUM_NAME
    enum_value: int = MASTER_ENUM_VALUE
    type_index: int = NATIVE_TYPE_INDEX
    constructor_va: int = NATIVE_CONSTRUCTOR_VA
    constructor_token: int = NATIVE_CONSTRUCTOR_TOKEN
    execute_va: int = NATIVE_EXECUTE_VA
    execute_token: int = NATIVE_EXECUTE_TOKEN

    def __post_init__(self) -> None:
        for label in (
            "effect_id",
            "effect_type",
            "target_card_id",
            "target_effect_type",
            "search_id",
            "move_position_type",
            "pick_range_type",
            "pick_reference_search_id",
            "pick_count_type",
            "second_search_id",
            "second_pick_range_type",
            "second_pick_reference_search_id",
            "second_pick_count_type",
            "chain_effect_id",
            "exam_status_enchant_id",
            "card_status_enchant_id",
            "executor_type",
            "enum_name",
        ):
            _text(getattr(self, label), label)
        _text(self.effect_id, "effect_id", non_empty=True)
        _text(self.effect_type, "effect_type", non_empty=True)
        _text(self.executor_type, "executor_type", non_empty=True)
        _text(self.enum_name, "enum_name", non_empty=True)

        for label in (
            "value1",
            "value2",
            "effect_count",
            "effect_turn",
            "target_upgrade_count",
            "pick_count_min",
            "pick_count_max",
            "second_pick_count_min",
            "second_pick_count_max",
            "enum_value",
            "type_index",
            "constructor_va",
            "constructor_token",
            "execute_va",
            "execute_token",
        ):
            _int32(getattr(self, label), label)

        for label in ("chain_effect_ids", "grow_effect_ids", "effect_group_ids"):
            normalized = _text_tuple(getattr(self, label), label)
            object.__setattr__(self, label, normalized)

    @classmethod
    def from_master_row(cls, raw: Mapping[str, object]) -> "HandGraveDrawContract":
        """Parse and validate one raw ``effect`` Master row."""

        if not isinstance(raw, Mapping):
            raise TypeError("raw must be a mapping")
        keys = frozenset(raw)
        missing = sorted(_MASTER_ROW_KEYS - keys)
        extra = sorted(keys - _MASTER_ROW_KEYS)
        if missing or extra:
            details = []
            if missing:
                details.append(f"missing={missing}")
            if extra:
                details.append(f"extra={extra}")
            raise HandGraveDrawContractError(
                "Master row keys are not the exact hand/grave draw shape: "
                + ", ".join(details)
            )

        effect_id = raw.get("id")
        if not isinstance(effect_id, str):
            raise TypeError("id must be text")
        _description_types(raw.get("produceDescriptions"), "produceDescriptions", effect_id)
        _description_types(
            raw.get("customizeProduceDescriptions"),
            "customizeProduceDescriptions",
            effect_id,
        )

        contract = cls(
            effect_id=raw.get("id"),
            effect_type=raw.get("effectType"),
            value1=raw.get("effectValue1"),
            value2=raw.get("effectValue2"),
            effect_count=raw.get("effectCount"),
            effect_turn=raw.get("effectTurn"),
            target_card_id=raw.get("targetProduceCardId"),
            target_upgrade_count=raw.get("targetUpgradeCount"),
            target_effect_type=raw.get("targetExamEffectType"),
            search_id=raw.get("produceCardSearchId"),
            move_position_type=raw.get("movePositionType"),
            pick_range_type=raw.get("pickRangeType"),
            pick_reference_search_id=raw.get("pickCountReferenceProduceCardSearchId"),
            pick_count_type=raw.get("pickCountType"),
            pick_count_min=raw.get("pickCountMin"),
            pick_count_max=raw.get("pickCountMax"),
            second_search_id=raw.get("produceCardSearchId2"),
            second_pick_range_type=raw.get("pickRangeType2"),
            second_pick_reference_search_id=raw.get(
                "pickCountReferenceProduceCardSearchId2"
            ),
            second_pick_count_type=raw.get("pickCountType2"),
            second_pick_count_min=raw.get("pickCountMin2"),
            second_pick_count_max=raw.get("pickCountMax2"),
            chain_effect_id=raw.get("chainProduceExamEffectId"),
            chain_effect_ids=raw.get("chainProduceExamEffectIds"),
            exam_status_enchant_id=raw.get("produceExamStatusEnchantId"),
            card_status_enchant_id=raw.get("produceCardStatusEnchantId"),
            grow_effect_ids=raw.get("produceCardGrowEffectIds"),
            effect_group_ids=raw.get("effectGroupIds"),
        )
        contract.assert_exact_master_shape()
        return contract

    def assert_exact_master_shape(self) -> None:
        """Reject any row shape whose semantics are not proven here."""

        actual = (
            self.effect_id,
            self.effect_type,
            self.value1,
            self.value2,
            self.effect_count,
            self.effect_turn,
            self.target_card_id,
            self.target_upgrade_count,
            self.target_effect_type,
            self.search_id,
            self.move_position_type,
            self.pick_range_type,
            self.pick_reference_search_id,
            self.pick_count_type,
            self.pick_count_min,
            self.pick_count_max,
            self.second_search_id,
            self.second_pick_range_type,
            self.second_pick_reference_search_id,
            self.second_pick_count_type,
            self.second_pick_count_min,
            self.second_pick_count_max,
            self.chain_effect_id,
            self.chain_effect_ids,
            self.exam_status_enchant_id,
            self.card_status_enchant_id,
            self.grow_effect_ids,
            self.effect_group_ids,
        )
        expected = (
            MASTER_EFFECT_ID,
            MASTER_EFFECT_TYPE,
            0,
            0,
            0,
            0,
            "",
            0,
            "ProduceExamEffectType_Unknown",
            "",
            "ProduceCardMovePositionType_Unknown",
            "ProducePickRangeType_Unknown",
            "",
            "ProducePickCountType_Unknown",
            0,
            0,
            "",
            "ProducePickRangeType_Unknown",
            "",
            "ProducePickCountType_Unknown",
            0,
            0,
            "",
            (),
            "",
            "",
            (),
            (MASTER_EFFECT_GROUP_ID,),
        )
        if actual != expected:
            raise HandGraveDrawContractError(
                f"unsupported hand/grave draw Master shape: {self.effect_id}"
            )
        if (
            self.executor_type,
            self.enum_name,
            self.enum_value,
            self.type_index,
            self.constructor_va,
            self.constructor_token,
            self.execute_va,
            self.execute_token,
        ) != (
            NATIVE_EXECUTOR_TYPE,
            MASTER_ENUM_NAME,
            MASTER_ENUM_VALUE,
            NATIVE_TYPE_INDEX,
            NATIVE_CONSTRUCTOR_VA,
            NATIVE_CONSTRUCTOR_TOKEN,
            NATIVE_EXECUTE_VA,
            NATIVE_EXECUTE_TOKEN,
        ):
            raise HandGraveDrawContractError("native binding is not the pinned executor")


@dataclass(frozen=True, slots=True)
class HandGraveDrawExecutable:
    """A native-proven executable resolution of the exact PC Master row."""

    contract: HandGraveDrawContract
    required_inputs: tuple[str, ...] = REQUIRED_NATIVE_INPUTS
    draw_count_formula: str = DRAW_COUNT_FORMULA
    operation_order: tuple[str, ...] = NATIVE_OPERATION_ORDER
    simulation_behavior: str = SIMULATION_BEHAVIOR
    target_position: str = "ProduceCardPositionType_Hand"
    move_destination: str = "ProduceCardMovePositionType_Grave"
    draw_is_add_log: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.contract, HandGraveDrawContract):
            raise TypeError("contract must be HandGraveDrawContract")
        self.contract.assert_exact_master_shape()
        inputs = _text_tuple(self.required_inputs, "required_inputs")
        order = _text_tuple(self.operation_order, "operation_order")
        if inputs != REQUIRED_NATIVE_INPUTS:
            raise HandGraveDrawContractError("required runtime input list changed")
        if order != NATIVE_OPERATION_ORDER:
            raise HandGraveDrawContractError("native operation order changed")
        if self.draw_count_formula != DRAW_COUNT_FORMULA:
            raise HandGraveDrawContractError("native draw formula changed")
        if self.simulation_behavior != SIMULATION_BEHAVIOR:
            raise HandGraveDrawContractError("simulation behavior changed")
        if (
            self.target_position != "ProduceCardPositionType_Hand"
            or self.move_destination != "ProduceCardMovePositionType_Grave"
            or self.draw_is_add_log is not True
        ):
            raise HandGraveDrawContractError("native move/draw arguments changed")
        object.__setattr__(self, "required_inputs", inputs)
        object.__setattr__(self, "operation_order", order)

    @property
    def executable(self) -> bool:
        return True

    def execute(
        self,
        state: Plan3NativeState,
        *,
        hand_limit: int,
        lesson_type: str,
        support_upgrades: SupportInputs = (),
        support_card_searches: Mapping[str, ProduceCardSearchRule] | None = None,
        simulate: bool = False,
    ) -> "HandGraveDrawTransition":
        return execute_hand_grave_draw(
            self,
            state,
            hand_limit=hand_limit,
            lesson_type=lesson_type,
            support_upgrades=support_upgrades,
            support_card_searches=support_card_searches,
            simulate=simulate,
        )


@dataclass(frozen=True, slots=True)
class HandGraveDrawUnresolved:
    """Typed fail-closed resolution for any non-proven Master shape."""

    effect_id: str
    blocker_code: str
    detail: str
    reason: str = UNRESOLVED_REASON

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect_id", non_empty=True)
        _text(self.blocker_code, "blocker_code", non_empty=True)
        _text(self.detail, "detail", non_empty=True)
        _text(self.reason, "reason", non_empty=True)

    @property
    def executable(self) -> bool:
        return False

    @property
    def required_native_inputs(self) -> tuple[str, ...]:
        return ("exact non-value1-zero target-selection body and Master linkage",)

    @property
    def required_native_formula(self) -> str:
        return "unresolved for this non-proven shape"

    def evaluate(self, *_args: object, **_kwargs: object) -> NoReturn:
        raise HandGraveDrawUnresolvedError(
            f"hand/grave draw execution is unresolved: {self.blocker_code}"
        )

    def execute(self, *_args: object, **_kwargs: object) -> NoReturn:
        self.evaluate(*_args, **_kwargs)


HandGraveDrawResolution: TypeAlias = (
    HandGraveDrawExecutable | HandGraveDrawUnresolved
)


@dataclass(frozen=True, slots=True)
class HandGraveDrawTransition:
    """One immutable execution trace over GUID-preserving native zones."""

    contract: HandGraveDrawContract
    before: Plan3NativeState
    after_hand_to_grave: Plan3NativeState
    native_draw: Plan3NativeDrawTransition
    discarded_guids: tuple[str, ...]
    simulated: bool
    operation_order: tuple[str, ...] = NATIVE_OPERATION_ORDER

    def __post_init__(self) -> None:
        if not isinstance(self.contract, HandGraveDrawContract):
            raise TypeError("contract must be HandGraveDrawContract")
        self.contract.assert_exact_master_shape()
        if not isinstance(self.before, Plan3NativeState) or not isinstance(
            self.after_hand_to_grave, Plan3NativeState
        ):
            raise TypeError("transition states must be Plan3NativeState")
        if not isinstance(self.native_draw, Plan3NativeDrawTransition):
            raise TypeError("native_draw must be Plan3NativeDrawTransition")
        if not isinstance(self.simulated, bool):
            raise TypeError("simulated must be bool")
        guids = _text_tuple(self.discarded_guids, "discarded_guids")
        if len(set(guids)) != len(guids):
            raise HandGraveDrawContractError("discarded GUIDs must be unique")
        order = _text_tuple(self.operation_order, "operation_order")
        if order != NATIVE_OPERATION_ORDER:
            raise HandGraveDrawContractError("transition operation order changed")
        expected_guids = tuple(card.guid for card in self.before.hand)
        if guids != expected_guids:
            raise HandGraveDrawContractError(
                "discarded GUIDs must be the ExecuteEffect Hand snapshot"
            )
        expected_grave = (
            *self.before.grave,
            *(card.reset_support_upgrade() for card in self.before.hand),
        )
        if (
            self.after_hand_to_grave.hand
            or self.after_hand_to_grave.grave != expected_grave
            or self.after_hand_to_grave.deck != self.before.deck
            or self.after_hand_to_grave.lost != self.before.lost
            or self.after_hand_to_grave.hold != self.before.hold
            or self.after_hand_to_grave.random_state != self.before.random_state
            or self.native_draw.before != self.after_hand_to_grave
        ):
            raise HandGraveDrawContractError(
                "transition does not preserve native Hand-to-Grave order"
            )
        requested = len(expected_guids)
        if (
            self.native_draw.requested_count != requested
            or self.native_draw.actual_count != requested
            or not self.native_draw.effect_draw
        ):
            raise HandGraveDrawContractError(
                "native draw count does not match the proven Hand snapshot"
            )
        expected_counter = self.before.total_effect_draw_card_count + requested
        if self.after.total_effect_draw_card_count != expected_counter:
            raise HandGraveDrawContractError(
                "actual draw count was not added to total effect draw count"
            )
        object.__setattr__(self, "discarded_guids", guids)
        object.__setattr__(self, "operation_order", order)

    @property
    def after(self) -> Plan3NativeState:
        return self.native_draw.after

    @property
    def requested_count(self) -> int:
        return self.native_draw.requested_count

    @property
    def actual_count(self) -> int:
        return self.native_draw.actual_count

    @property
    def drawn_guids(self) -> tuple[str, ...]:
        return self.native_draw.drawn_guids


def resolve_hand_grave_draw(raw: Mapping[str, object]) -> HandGraveDrawResolution:
    """Resolve only the exact value1-zero row; return a typed gap otherwise."""

    if not isinstance(raw, Mapping):
        raise TypeError("raw must be a mapping")
    raw_id = raw.get("id")
    effect_id = raw_id if isinstance(raw_id, str) and raw_id else "<invalid-effect-id>"
    try:
        return HandGraveDrawExecutable(HandGraveDrawContract.from_master_row(raw))
    except (HandGraveDrawContractError, TypeError) as error:
        return HandGraveDrawUnresolved(
            effect_id=effect_id,
            blocker_code="unsupported-hand-grave-draw-master-shape",
            detail=str(error) or type(error).__name__,
        )


def load_hand_grave_draw(
    database: Path = DEFAULT_DATABASE,
) -> HandGraveDrawResolution:
    """Load and resolve the sole exact PC Master effect row."""

    try:
        with closing(sqlite3.connect(Path(database))) as connection:
            row = connection.execute(
                "SELECT raw_json FROM effect WHERE id = ?",
                (MASTER_EFFECT_ID,),
            ).fetchone()
        if row is None:
            return HandGraveDrawUnresolved(
                MASTER_EFFECT_ID,
                "hand-grave-draw-master-missing",
                MASTER_EFFECT_ID,
            )
        raw = json.loads(str(row[0]))
        if not isinstance(raw, Mapping):
            raise ValueError("raw_json is not an object")
        return resolve_hand_grave_draw(raw)
    except (json.JSONDecodeError, OSError, sqlite3.Error, ValueError) as error:
        return HandGraveDrawUnresolved(
            MASTER_EFFECT_ID,
            "hand-grave-draw-master-load-failed",
            f"{type(error).__name__}:{error}",
        )


def parse_hand_grave_draw_master_row(
    raw: Mapping[str, object],
) -> HandGraveDrawContract:
    """Named parser for callers that only need the immutable contract."""

    return HandGraveDrawContract.from_master_row(raw)


def execute_hand_grave_draw(
    resolution: HandGraveDrawResolution | HandGraveDrawContract,
    state: Plan3NativeState,
    *,
    hand_limit: int,
    lesson_type: str,
    support_upgrades: SupportInputs = (),
    support_card_searches: Mapping[str, ProduceCardSearchRule] | None = None,
    simulate: bool = False,
) -> HandGraveDrawTransition:
    """Execute the proven snapshot -> Grave -> draw -> counter sequence.

    ``simulate`` does not change the effect formula or RNG consumption.  The
    native executor has no simulation branch; native simulation supplies a
    copied context.  This pure function likewise leaves ``state`` untouched
    and returns the candidate post-state for the caller to commit or discard.
    """

    if isinstance(resolution, HandGraveDrawUnresolved):
        resolution.execute()
    if isinstance(resolution, HandGraveDrawExecutable):
        contract = resolution.contract
    elif isinstance(resolution, HandGraveDrawContract):
        contract = resolution
        contract.assert_exact_master_shape()
    else:
        raise TypeError(
            "resolution must be HandGraveDrawExecutable, "
            "HandGraveDrawUnresolved, or HandGraveDrawContract"
        )
    if not isinstance(state, Plan3NativeState):
        raise TypeError("state must be Plan3NativeState")
    hand_limit = _int32(hand_limit, "hand_limit")
    if hand_limit < 0:
        raise Plan3NativeStateError("invalid-hand-limit", repr(hand_limit))
    if len(state.hand) > hand_limit:
        raise Plan3NativeStateError(
            "hand-limit-invariant",
            f"hand={len(state.hand)}:limit={hand_limit}",
        )
    _text(lesson_type, "lesson_type", non_empty=True)
    if support_card_searches is None:
        support_card_searches = {}
    if not isinstance(support_card_searches, Mapping):
        raise TypeError("support_card_searches must be a mapping")
    if not isinstance(simulate, bool):
        raise TypeError("simulate must be bool")

    hand_snapshot = state.hand
    discarded = tuple(card.reset_support_upgrade() for card in hand_snapshot)
    after_hand_to_grave = replace(
        state,
        hand=(),
        grave=(*state.grave, *discarded),
    )
    native_draw = after_hand_to_grave.draw_to_hand(
        len(hand_snapshot),
        hand_limit=hand_limit,
        lesson_type=lesson_type,
        support_upgrades=support_upgrades,
        support_card_searches=support_card_searches,
        effect_draw=True,
    )
    return HandGraveDrawTransition(
        contract=contract,
        before=state,
        after_hand_to_grave=after_hand_to_grave,
        native_draw=native_draw,
        discarded_guids=tuple(card.guid for card in hand_snapshot),
        simulated=simulate,
    )


__all__ = [
    "AFFECTED_CARD_VERSIONS",
    "AFFECTED_CARD_IDS",
    "DRAW_COUNT_FORMULA",
    "GRAVE_MOVE_ENUM_VALUE",
    "HAND_POSITION_ENUM_VALUE",
    "HandGraveDrawCardReference",
    "HandGraveDrawContract",
    "HandGraveDrawContractError",
    "HandGraveDrawExecutable",
    "HandGraveDrawResolution",
    "HandGraveDrawTransition",
    "HandGraveDrawUnresolved",
    "HandGraveDrawUnresolvedError",
    "MASTER_EFFECT_GROUP_ID",
    "MASTER_EFFECT_ID",
    "MASTER_EFFECT_TYPE",
    "MASTER_ENUM_NAME",
    "MASTER_ENUM_VALUE",
    "NATIVE_CONSTRUCTOR_TOKEN",
    "NATIVE_CONSTRUCTOR_VA",
    "NATIVE_CREATE_CARD_POSITION_LIST_VA",
    "NATIVE_DRAW_CARD_VA",
    "NATIVE_EXECUTE_RANGE",
    "NATIVE_EXECUTE_TOKEN",
    "NATIVE_EXECUTE_VA",
    "NATIVE_EXECUTOR_TYPE",
    "NATIVE_MOVE_CARD_VA",
    "NATIVE_OPERATION_ORDER",
    "NATIVE_ADD_TOTAL_EFFECT_DRAW_COUNT_VA",
    "NATIVE_SELECTED_INDEX_PREDICATE_VA",
    "NATIVE_SELECT_CARD_VA",
    "NATIVE_SELECT_WITH_INDEX_VA",
    "NATIVE_TYPE_GLOBAL_VA",
    "NATIVE_TYPE_INDEX",
    "NATIVE_USAGE_CELL_VA",
    "REQUIRED_NATIVE_FORMULA",
    "REQUIRED_NATIVE_INPUTS",
    "SIMULATION_BEHAVIOR",
    "UNRESOLVED_REASON",
    "execute_hand_grave_draw",
    "load_hand_grave_draw",
    "parse_hand_grave_draw_master_row",
    "resolve_hand_grave_draw",
]
