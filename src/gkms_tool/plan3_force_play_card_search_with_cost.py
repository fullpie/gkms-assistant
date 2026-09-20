"""Exact Android v3.2.3 planner for ``ExamForcePlayCardSearchWithCost``.

The only current Master use is the one-shot StartTurn/FullPower listener on
``p_card-03-ido-100_038``.  Native first builds the exact ``NotLost``
collection (Hand, Deck, Grave, Hold), excludes cards whose *current play
effect list* contains ordinary ``ExamForcePlayCardSearch`` (enum 24), asks for
one explicit target, then queues a ``UsePool`` command with
``isConsumeCost=true``.  The executor appends a matching ``CardForcePlay``
difference after the command; it does not synchronously play the target.

This module owns that executor boundary.  The later arbitrary-card
transaction remains an explicit core hook because it must evaluate the live
cost, IsPlayable, card listeners, Playing state, and final destination.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any

from .card_search import ProduceCardSearchRule, load_produce_card_search
from .master_db import DEFAULT_DATABASE
from .plan3_force_play_card_search import (
    CORE_HOOK_REQUIREMENTS,
    ForcePlayDifference,
    ForcePlayResolvedBranch,
    ForcePlayTargetCardMaster,
    ForcePlayUnresolvedBranch,
    load_force_play_target_card_master,
)
from .plan3_native_state import (
    Plan3NativeCard,
    Plan3NativeCardMoveTarget,
    Plan3NativeState,
)


EFFECT_TYPE = "ProduceExamEffectType_ExamForcePlayCardSearchWithCost"
EFFECT_TYPE_VALUE = 187
EFFECT_ID = (
    "e_effect-exam_force_play_card_search_with_cost-"
    "p_card_search-not_lost-select-1_1"
)
SEARCH_ID = "p_card_search-not_lost"
SEARCH_POSITION = "ProduceCardPositionType_NotLost"
PICK_RANGE = "ProducePickRangeType_Select"
PICK_COUNT_TYPE = "ProducePickCountType_Unknown"
PICK_RANGE_UNKNOWN = "ProducePickRangeType_Unknown"
MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"

CARD_ID = "p_card-03-ido-100_038"
CARD_UPGRADE = 0
STATUS_INSTALL_EFFECT_ID = (
    "e_effect-exam_status_enchant-01-inf-"
    "enchant-p_card-03-ido-100_038-enc01"
)
STATUS_ENCHANT_ID = "enchant-p_card-03-ido-100_038-enc01"
TRIGGER_ID = "e_trigger-exam_start_turn-full_power_up"
TRIGGER_PHASE = "ProduceExamPhaseType_ExamStartTurn"
TRIGGER_FIELD = "ProduceExamFieldStatusType_FullPowerUp"
CARD_DIRECT_EFFECT_IDS = (
    "e_effect-exam_full_power_point-0005",
    "e_effect-exam_playable_value_add-01",
    STATUS_INSTALL_EFFECT_ID,
)

ANDROID_VERSION = "Android v3.2.3"
ANDROID_EXECUTOR_CTOR = "0x7E79660"
ANDROID_EXECUTOR_EXECUTE = "0x7E7990C"
ANDROID_RECURSION_CARD_PREDICATE = "0x7E79FAC"
ANDROID_RECURSION_EFFECT_PREDICATE = "0x7E7A0C4"
ANDROID_GET_SEARCH_CARD_LIST = "0x7E57B94"
ANDROID_CREATE_USE_POOL = "0x7EC3458"
ANDROID_SET_FORCE_DIFFERENCE = "0x7E57420"
PC_METADATA_TYPE = (
    "Campus.InGame.Exam.ForcePlayCardSearchWithCostEffectExecutor"
)
PC_METADATA_FIELDS = (
    "_search",
    "_pickRangeType",
    "_pickCountMin",
    "_pickCountMax",
    "_pickCountType",
    "_search2",
)
PC_METADATA_METHODS = ((".ctor", 1, 6278), ("ExecuteEffect", 1, 198))


class ForcePlayWithCostError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class ForcePlayWithCostResolutionError(ForcePlayWithCostError):
    """Static Master or current target-card data is not exact."""


class ForcePlayWithCostInputError(ForcePlayWithCostError):
    """Runtime input is malformed or cannot select an exact branch."""


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ForcePlayWithCostInputError("invalid-text", label)
    return value


def _text_tuple(values: Sequence[object], label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise ForcePlayWithCostInputError("invalid-text-sequence", label)
    result = tuple(values)
    if any(not isinstance(value, str) or not value for value in result):
        raise ForcePlayWithCostInputError("invalid-text-sequence", label)
    return result


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ForcePlayWithCostInputError("invalid-nonnegative-int", label)
    return value


@dataclass(frozen=True, slots=True)
class ForcePlayWithCostEffectRow:
    effect_id: str = EFFECT_ID
    search_id: str = SEARCH_ID
    pick_range_type: str = PICK_RANGE
    pick_count_min: int = 1
    pick_count_max: int = 1
    effect_value1: int = 0
    effect_value2: int = 0
    effect_count: int = 0
    effect_turn: int = 0


EXACT_EFFECT_ROW = ForcePlayWithCostEffectRow()


def _search_shape_errors(search: ProduceCardSearchRule) -> tuple[str, ...]:
    expected: tuple[tuple[str, object], ...] = (
        ("id", SEARCH_ID),
        ("card_rarities", ()),
        ("produce_card_ids", ()),
        ("upgrade_counts", ()),
        ("plan_type", "ProducePlanType_Unknown"),
        ("card_categories", ()),
        ("card_status_type", "ProduceCardSearchStatusType_Unknown"),
        ("order_type", "ProduceCardOrderType_Unknown"),
        ("card_position_type", SEARCH_POSITION),
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
    return tuple(
        f"search-shape:{field_name}"
        for field_name, expected_value in expected
        if getattr(search, field_name) != expected_value
    )


@dataclass(frozen=True, slots=True)
class ForcePlayWithCostContract:
    row: ForcePlayWithCostEffectRow
    search: ProduceCardSearchRule

    def __post_init__(self) -> None:
        if not isinstance(self.row, ForcePlayWithCostEffectRow):
            raise ForcePlayWithCostInputError("invalid-effect-row")
        if not isinstance(self.search, ProduceCardSearchRule):
            raise ForcePlayWithCostInputError("invalid-search-rule")

    @property
    def unresolved_reasons(self) -> tuple[str, ...]:
        return _search_shape_errors(self.search)

    @property
    def executable(self) -> bool:
        return not self.unresolved_reasons


_EXPECTED_EFFECT_PAYLOAD: Mapping[str, object] = {
    "id": EFFECT_ID,
    "effectType": EFFECT_TYPE,
    "effectValue1": 0,
    "effectValue2": 0,
    "effectCount": 0,
    "effectTurn": 0,
    "targetProduceCardId": "",
    "targetUpgradeCount": 0,
    "targetExamEffectType": "ProduceExamEffectType_Unknown",
    "produceCardSearchId": SEARCH_ID,
    "movePositionType": MOVE_UNKNOWN,
    "pickRangeType": PICK_RANGE,
    "pickCountReferenceProduceCardSearchId": "",
    "pickCountType": PICK_COUNT_TYPE,
    "pickCountMin": 1,
    "pickCountMax": 1,
    "produceCardSearchId2": "",
    "pickRangeType2": PICK_RANGE_UNKNOWN,
    "pickCountReferenceProduceCardSearchId2": "",
    "pickCountType2": PICK_COUNT_TYPE,
    "pickCountMin2": 0,
    "pickCountMax2": 0,
    "chainProduceExamEffectId": "",
    "chainProduceExamEffectIds": [],
    "produceExamStatusEnchantId": "",
    "produceCardStatusEnchantId": "",
    "produceCardGrowEffectIds": [],
    "effectGroupIds": [],
}


def resolve_force_play_with_cost_contract(
    effect_row: Mapping[str, object] | sqlite3.Row,
    *,
    database: Path = DEFAULT_DATABASE,
) -> ForcePlayWithCostContract:
    try:
        values = dict(effect_row)
    except (TypeError, ValueError) as error:
        raise ForcePlayWithCostResolutionError("invalid-master-row") from error
    if values.get("id") != EFFECT_ID or values.get("effect_type") != EFFECT_TYPE:
        raise ForcePlayWithCostResolutionError("unexpected-effect-row")
    raw = values.get("raw_json")
    if not isinstance(raw, str):
        raise ForcePlayWithCostResolutionError("missing-master-raw-json")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ForcePlayWithCostResolutionError("invalid-master-raw-json") from error
    if not isinstance(payload, Mapping):
        raise ForcePlayWithCostResolutionError("invalid-master-raw-json")
    for field_name, expected_value in _EXPECTED_EFFECT_PAYLOAD.items():
        if payload.get(field_name) != expected_value:
            raise ForcePlayWithCostResolutionError(
                "master-structural-mismatch", field_name
            )
    try:
        search = load_produce_card_search(SEARCH_ID, Path(database))
    except (KeyError, ValueError, sqlite3.Error) as error:
        raise ForcePlayWithCostResolutionError(
            "search-load-failed", SEARCH_ID
        ) from error
    contract = ForcePlayWithCostContract(EXACT_EFFECT_ROW, search)
    if not contract.executable:
        raise ForcePlayWithCostResolutionError(
            "search-structural-mismatch", contract.unresolved_reasons[0]
        )
    return contract


def load_force_play_with_cost_contract(
    database: Path = DEFAULT_DATABASE,
) -> ForcePlayWithCostContract:
    with sqlite3.connect(Path(database)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (EFFECT_ID,)
        ).fetchone()
    if row is None:
        raise ForcePlayWithCostResolutionError("effect-master-missing", EFFECT_ID)
    return resolve_force_play_with_cost_contract(row, database=database)


def exact_card_bundle_errors(
    database: Path = DEFAULT_DATABASE,
) -> tuple[str, ...]:
    """Validate the sole card/listener chain that reaches enum 187."""

    errors: list[str] = []
    with sqlite3.connect(Path(database)) as connection:
        connection.row_factory = sqlite3.Row
        card = connection.execute(
            "SELECT * FROM card WHERE id = ? AND upgrade_count = ?",
            (CARD_ID, CARD_UPGRADE),
        ).fetchone()
        install = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (STATUS_INSTALL_EFFECT_ID,)
        ).fetchone()
        status = connection.execute(
            "SELECT * FROM produce_exam_status_enchant WHERE id = ?",
            (STATUS_ENCHANT_ID,),
        ).fetchone()
        trigger = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?", (TRIGGER_ID,)
        ).fetchone()
    if card is None:
        return ("card-master-missing",)
    try:
        card_effects = tuple(
            item["produceExamEffectId"]
            for item in json.loads(card["play_effects_json"])
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        return ("card-effects-invalid",)
    expected_card = {
        "plan_type": "ProducePlanType_Plan3",
        "category": "ProduceCardCategory_MentalSkill",
        "stamina": 0,
        "cost_type": "ExamCostType_Unknown",
        "cost_value": 0,
        "play_trigger_id": "",
        "move_position_type": "ProduceCardMovePositionType_Lost",
    }
    for field_name, expected_value in expected_card.items():
        if card[field_name] != expected_value:
            errors.append(f"card-shape:{field_name}")
    if card_effects != CARD_DIRECT_EFFECT_IDS:
        errors.append("card-effect-order")
    if install is None:
        errors.append("status-install-effect-missing")
    else:
        expected_install = {
            "effect_type": "ProduceExamEffectType_ExamStatusEnchant",
            "value1": 0,
            "value2": 0,
            "effect_count": 1,
            "effect_turn": -1,
            "status_enchant_id": STATUS_ENCHANT_ID,
            "chain_effect_id": "",
        }
        for field_name, expected_value in expected_install.items():
            if install[field_name] != expected_value:
                errors.append(f"status-install-shape:{field_name}")
    if status is None:
        errors.append("status-enchant-missing")
    else:
        try:
            payload = json.loads(status["raw_json"])
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, Mapping):
            errors.append("status-enchant-json")
        elif (
            payload.get("produceExamTriggerId") != TRIGGER_ID
            or payload.get("produceExamEffectIds") != [EFFECT_ID]
        ):
            errors.append("status-enchant-shape")
    if trigger is None:
        errors.append("trigger-missing")
    else:
        try:
            payload = json.loads(trigger["raw_json"])
        except json.JSONDecodeError:
            payload = None
        exact_trigger = isinstance(payload, Mapping) and (
            payload.get("phaseTypes") == [TRIGGER_PHASE]
            and payload.get("phaseValues") == []
            and payload.get("fieldStatusCheckTypes") == []
            and payload.get("fieldStatusTypes") == [TRIGGER_FIELD]
            and payload.get("fieldStatusValues") == []
            and payload.get("fieldStatusProduceCardSearchIds") == []
            and payload.get("produceCardSearchId") == ""
            and payload.get("upperSearchCount") == 0
            and payload.get("lowerSearchCount") == 0
            and payload.get("cardMovePositionType") == MOVE_UNKNOWN
            and payload.get("effectTypes") == []
            and payload.get("lessonType") == "ProduceStepLessonType_Unknown"
        )
        if not exact_trigger:
            errors.append("trigger-shape")
    return tuple(dict.fromkeys(errors))


@dataclass(frozen=True, slots=True)
class ForcePlayWithCostExecutionInput:
    selected_guids: tuple[str, ...] = ()
    enchant_effect_uid: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "selected_guids", _text_tuple(self.selected_guids, "selected_guids")
        )
        _nonnegative_int(self.enchant_effect_uid, "enchant_effect_uid")


WITH_COST_USE_POOL_TRANSACTION_ORDER: tuple[str, ...] = (
    "remove-current-use-pool-command",
    "resolve-current-source-position-by-command-card-guid",
    "evaluate-live-cost-and-is-playable",
    "consume-cost-because-is-consume-cost-true",
    "do-not-consume-playable-count",
    "remove-source-card",
    "check-insert-existing-difference-commands",
    "build-card-batch",
    "set-playing-card-same-guid-object",
    "execute-normal-card-listeners-and-direct-effects",
    "move-play-card-and-increment-global-play-count",
    "settle-live-card-destination",
    "difference-callback",
)


@dataclass(frozen=True, slots=True)
class ForcePlayWithCostQueuedCommand:
    guid: str
    card: Plan3NativeCard
    original_source_zone: str
    original_source_position_type: str
    original_source_position_value: int
    original_source_index: int
    base_move_position_type: str
    enchant_effect_uid: int
    command_type: int = 4
    is_consume_cost: bool = True
    is_manual: bool = False
    is_use_playable_count: bool = False
    resolves_source_by_guid_at_execution: bool = True
    core_hook_requirements: tuple[str, ...] = CORE_HOOK_REQUIREMENTS
    transaction_order: tuple[str, ...] = WITH_COST_USE_POOL_TRANSACTION_ORDER

    def __post_init__(self) -> None:
        _text(self.guid, "guid")
        if self.card.guid != self.guid:
            raise ForcePlayWithCostInputError("command-guid-card-mismatch")
        _nonnegative_int(self.original_source_index, "original_source_index")
        _nonnegative_int(self.original_source_position_value, "position_value")
        _nonnegative_int(self.enchant_effect_uid, "enchant_effect_uid")

    @property
    def cost_delta(self) -> None:
        """The live target/card/status state decides the payment later."""

        return None

    @property
    def plays_remaining_delta(self) -> int:
        return 0


@dataclass(frozen=True, slots=True)
class ForcePlayWithCostTrace:
    effect_id: str
    random_state_before: int
    random_state_after: int
    rng_consumed: bool
    operations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ForcePlayWithCostPlanResult:
    before: Plan3NativeState
    after: Plan3NativeState
    contract: ForcePlayWithCostContract
    execution_input: ForcePlayWithCostExecutionInput
    branch: ForcePlayResolvedBranch | ForcePlayUnresolvedBranch
    commands: tuple[ForcePlayWithCostQueuedCommand, ...]
    differences: tuple[ForcePlayDifference, ...]
    trace: ForcePlayWithCostTrace

    @property
    def resolved(self) -> bool:
        return isinstance(self.branch, ForcePlayResolvedBranch)

    @property
    def core_hook_required(self) -> bool:
        return bool(self.commands)

    def __post_init__(self) -> None:
        if self.after is not self.before:
            raise ForcePlayWithCostInputError("effect-planner-mutated-state")
        if not self.resolved and (self.commands or self.differences):
            raise ForcePlayWithCostInputError("unresolved-result-has-output")
        if len(self.commands) != len(self.differences):
            raise ForcePlayWithCostInputError("command-difference-count-mismatch")
        for ordinal, (command, difference) in enumerate(
            zip(self.commands, self.differences, strict=True)
        ):
            if difference.ordinal != ordinal or difference.guid != command.guid:
                raise ForcePlayWithCostInputError("command-difference-order-mismatch")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_SOURCE_LAYOUT: tuple[tuple[str, str, int], ...] = (
    ("hand", "ProduceCardPositionType_Hand", 2),
    ("deck", "ProduceCardPositionType_Deck", 3),
    ("grave", "ProduceCardPositionType_Grave", 4),
    ("hold", "ProduceCardPositionType_Hold", 13),
)


def _not_lost_candidates(
    state: Plan3NativeState,
) -> tuple[Plan3NativeCardMoveTarget, ...]:
    candidates: list[Plan3NativeCardMoveTarget] = []
    for state_field, _position_type, _position_value in _SOURCE_LAYOUT:
        source_zone = "draw" if state_field == "deck" else (
            "discard" if state_field == "grave" else state_field
        )
        for source_index, card in enumerate(getattr(state, state_field)):
            candidates.append(
                Plan3NativeCardMoveTarget(
                    card.guid, card, source_zone, source_index
                )
            )
    return tuple(candidates)


def _source_position(source_zone: str) -> tuple[str, int] | None:
    state_field = {"draw": "deck", "discard": "grave"}.get(
        source_zone, source_zone
    )
    return next(
        (
            (position_type, position_value)
            for field_name, position_type, position_value in _SOURCE_LAYOUT
            if field_name == state_field
        ),
        None,
    )


def _unresolved(
    state: Plan3NativeState,
    contract: ForcePlayWithCostContract,
    supplied: ForcePlayWithCostExecutionInput,
    reason: str,
    detail: str = "",
) -> ForcePlayWithCostPlanResult:
    return ForcePlayWithCostPlanResult(
        state,
        state,
        contract,
        supplied,
        ForcePlayUnresolvedBranch(reason, detail),
        (),
        (),
        ForcePlayWithCostTrace(
            EFFECT_ID,
            state.random_state,
            state.random_state,
            False,
            ("fail-closed-before-queue",),
        ),
    )


def plan_force_play_card_search_with_cost(
    state: Plan3NativeState,
    contract: ForcePlayWithCostContract,
    *,
    execution_input: ForcePlayWithCostExecutionInput | None = None,
    database: Path = DEFAULT_DATABASE,
) -> ForcePlayWithCostPlanResult:
    """Queue the exact selected ``UsePool`` command without playing it."""

    if not isinstance(state, Plan3NativeState):
        raise ForcePlayWithCostInputError("state-must-be-plan3-native-state")
    if not isinstance(contract, ForcePlayWithCostContract):
        raise ForcePlayWithCostInputError("invalid-contract")
    supplied = execution_input or ForcePlayWithCostExecutionInput()
    if not isinstance(supplied, ForcePlayWithCostExecutionInput):
        raise ForcePlayWithCostInputError("invalid-execution-input")
    if not contract.executable:
        return _unresolved(
            state,
            contract,
            supplied,
            "with-cost-contract-unresolved",
            contract.unresolved_reasons[0],
        )
    candidates = _not_lost_candidates(state)
    masters: dict[str, ForcePlayTargetCardMaster] = {}
    try:
        for candidate in candidates:
            masters[candidate.guid] = load_force_play_target_card_master(
                candidate.card, Path(database)
            )
    except (ValueError, sqlite3.Error) as error:
        code = getattr(error, "code", "target-card-master-unresolved")
        detail = getattr(error, "detail", str(error))
        return _unresolved(state, contract, supplied, code, detail)
    eligible = tuple(
        candidate
        for candidate in candidates
        if not masters[candidate.guid].has_recursive_force_play
    )
    excluded = tuple(
        candidate.guid
        for candidate in candidates
        if masters[candidate.guid].has_recursive_force_play
    )
    if not eligible and not supplied.selected_guids:
        selected: tuple[Plan3NativeCardMoveTarget, ...] = ()
    elif len(supplied.selected_guids) != 1:
        return _unresolved(
            state,
            contract,
            supplied,
            "select-count-mismatch",
            f"expected=1:actual={len(supplied.selected_guids)}",
        )
    else:
        selected = tuple(
            candidate
            for candidate in eligible
            if candidate.guid == supplied.selected_guids[0]
        )
        if len(selected) != 1:
            return _unresolved(
                state,
                contract,
                supplied,
                "selected-guid-not-eligible",
                supplied.selected_guids[0],
            )
    commands: list[ForcePlayWithCostQueuedCommand] = []
    differences: list[ForcePlayDifference] = []
    operations: list[str] = [
        "build-not-lost-candidates:hand-deck-grave-hold",
        "filter-card-when-any-current-play-effect-has-type-24",
        "explicit-select-one",
    ]
    for ordinal, target in enumerate(selected):
        source = _source_position(target.source_zone)
        if source is None:
            return _unresolved(
                state,
                contract,
                supplied,
                "unsupported-force-play-source-zone",
                target.source_zone,
            )
        position_type, position_value = source
        commands.append(
            ForcePlayWithCostQueuedCommand(
                target.guid,
                target.card,
                target.source_zone,
                position_type,
                position_value,
                target.source_index,
                masters[target.guid].base_move_position_type,
                supplied.enchant_effect_uid,
            )
        )
        differences.append(ForcePlayDifference(ordinal, target.guid))
        operations.extend(
            (
                f"queue-use-pool-consume-cost:{target.guid}",
                f"append-card-force-play-difference:{target.guid}",
            )
        )
    branch = ForcePlayResolvedBranch(
        tuple(candidate.guid for candidate in candidates),
        tuple(candidate.guid for candidate in eligible),
        excluded,
        tuple(candidate.guid for candidate in selected),
        "explicit-target-index-membership",
    )
    return ForcePlayWithCostPlanResult(
        state,
        state,
        contract,
        supplied,
        branch,
        tuple(commands),
        tuple(differences),
        ForcePlayWithCostTrace(
            EFFECT_ID,
            state.random_state,
            state.random_state,
            False,
            tuple(operations),
        ),
    )


execute_force_play_card_search_with_cost = plan_force_play_card_search_with_cost


def native_audit_to_dict() -> dict[str, object]:
    return {
        "android_version": ANDROID_VERSION,
        "effect_type": EFFECT_TYPE,
        "effect_type_value": EFFECT_TYPE_VALUE,
        "effect_id": EFFECT_ID,
        "affected_card_version": {"card_id": CARD_ID, "upgrade": CARD_UPGRADE},
        "native_addresses": {
            "constructor": ANDROID_EXECUTOR_CTOR,
            "execute_effect": ANDROID_EXECUTOR_EXECUTE,
            "card_recursion_predicate": ANDROID_RECURSION_CARD_PREDICATE,
            "effect_recursion_predicate": ANDROID_RECURSION_EFFECT_PREDICATE,
            "get_search_card_list": ANDROID_GET_SEARCH_CARD_LIST,
            "create_use_pool_command": ANDROID_CREATE_USE_POOL,
            "set_card_force_play_difference": ANDROID_SET_FORCE_DIFFERENCE,
        },
        "pc_metadata": {
            "type": PC_METADATA_TYPE,
            "type_index": 3556,
            "fields": list(PC_METADATA_FIELDS),
            "methods": [list(value) for value in PC_METADATA_METHODS],
            "constructor_token": "0x060045CF",
            "execute_effect_token": "0x060045D0",
        },
        "proved_order": [
            "GetSearchCardList(NotLost, enum24-recursion-predicate)",
            "Pick(Select, 1..1)",
            "CreateUsePoolCommand(isConsumeCost=true,isManual=false,uid=context)",
            "append command",
            "append CardForcePlay difference",
        ],
        "not_lost_zone_order": ["hand", "deck", "grave", "hold"],
        "target_allowed_native_positions": [2, 3, 4, 13],
        "rng_consumed": False,
        "core_hook": list(CORE_HOOK_REQUIREMENTS),
    }


__all__ = [
    "ANDROID_EXECUTOR_CTOR",
    "ANDROID_EXECUTOR_EXECUTE",
    "ANDROID_VERSION",
    "CARD_DIRECT_EFFECT_IDS",
    "CARD_ID",
    "CARD_UPGRADE",
    "EFFECT_ID",
    "EFFECT_TYPE",
    "EFFECT_TYPE_VALUE",
    "EXACT_EFFECT_ROW",
    "ForcePlayWithCostContract",
    "ForcePlayWithCostEffectRow",
    "ForcePlayWithCostError",
    "ForcePlayWithCostExecutionInput",
    "ForcePlayWithCostInputError",
    "ForcePlayWithCostPlanResult",
    "ForcePlayWithCostQueuedCommand",
    "ForcePlayWithCostResolutionError",
    "PC_METADATA_FIELDS",
    "PC_METADATA_METHODS",
    "PC_METADATA_TYPE",
    "SEARCH_ID",
    "STATUS_ENCHANT_ID",
    "STATUS_INSTALL_EFFECT_ID",
    "TRIGGER_ID",
    "WITH_COST_USE_POOL_TRANSACTION_ORDER",
    "exact_card_bundle_errors",
    "execute_force_play_card_search_with_cost",
    "load_force_play_with_cost_contract",
    "native_audit_to_dict",
    "plan_force_play_card_search_with_cost",
    "resolve_force_play_with_cost_contract",
]
