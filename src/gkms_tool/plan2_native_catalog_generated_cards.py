"""Standalone Plan2/Common catalog for generated-card and Hand-move effects.

This leaf owns the 25 currently unbound direct occurrences of CardCreateId,
CardCreateSearch, CardSearchEffectPlayCountBuff, and ForcePlayCardSearch in 21
Plan2/Common card versions.  It also inventories and adapts the five Plan2
cards whose move-effect trigger is Hand.

The implementation deliberately delegates mutation semantics to the existing
exact native leaves.  In particular, card GUIDs and PlayCountBuff status UIDs
are caller-owned observations: neither can be reconstructed from the exam RNG
or the current immutable simulation state, so absence fails closed without
manufacturing an identity.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Final, Literal, TypeAlias

from .master_db import DEFAULT_DATABASE
from .plan2_card_create_id import (
    CardCreateInputError,
    CardCreateResult,
    apply_plan2_card_create_id,
    resolve_plan2_card_create_contract,
)
from .plan2_card_create_search import (
    CardCreateSearchChanceInput,
    CardCreateSearchInputError,
    CardCreateSearchResult,
    TARGET_EFFECT_ID as CREATE_SEARCH_EFFECT_ID,
    apply_plan2_card_create_search,
    load_plan2_common_card_versions,
    resolve_plan2_card_create_search_contract,
)
from .plan2_card_search_play_count_buff import (
    EFFECT_ID as PLAY_COUNT_BUFF_EFFECT_ID,
    FORCE_PLAY_EFFECT_ID,
    PlayCountBuffRuntime,
    PlayCountBuffStatus,
    Plan2CardSearchPlayCountBuffHandoff,
    Plan2ForcePlayBuffQueueExecution,
    build_force_play_handoff,
    execute_force_play_buff_handoff,
    install_card_search_effect_play_count_buff,
    load_affected_card_versions as load_play_count_buff_versions,
    load_card_search_effect_play_count_buff_contract,
    load_force_play_handoff_contract,
)
from .plan2_force_play_search import (
    Plan2ForcePlayQueuedCommand,
    Plan2ForcePlayQueueExecution,
    execute_plan2_force_play_queue,
)
from .plan2_lesson_depend_block_consumption_final import (
    FinalCardRuntime,
    HandMoveTransition,
    load_plan2_final_card_contract,
    move_final_card_to_hand,
)
from .plan2_move_card_play_aggressive import (
    HandMoveAggressiveRuntimeResult,
    HandMoveAggressiveRuntimeState,
    HandMoveCommitEvent,
    load_all_plan2_move_card_play_aggressive_contracts,
    execute_plan2_move_card_play_aggressive,
)
from .plan2_state import Plan2State
from .plan3_force_play_card_search import (
    ForcePlayExecutionInput,
    ForcePlayPlanResult,
    plan_force_play_card_search,
    resolve_force_play_card_search_contract,
)
from .plan3_native_state import Plan3NativeCard, Plan3NativeState


SCHEMA_VERSION: Final = 1
PLAN_COMMON: Final = "ProducePlanType_Common"
PLAN2: Final = "ProducePlanType_Plan2"
PLANS: Final = frozenset((PLAN_COMMON, PLAN2))

CARD_CREATE_ID: Final = "ProduceExamEffectType_ExamCardCreateId"
CARD_CREATE_SEARCH: Final = "ProduceExamEffectType_ExamCardCreateSearch"
PLAY_COUNT_BUFF: Final = (
    "ProduceExamEffectType_ExamCardSearchEffectPlayCountBuff"
)
FORCE_PLAY_SEARCH: Final = "ProduceExamEffectType_ExamForcePlayCardSearch"
TARGET_TYPES: Final = frozenset(
    (CARD_CREATE_ID, CARD_CREATE_SEARCH, PLAY_COUNT_BUFF, FORCE_PLAY_SEARCH)
)
EFFECT_TIMER: Final = "ProduceExamEffectType_ExamEffectTimer"
MOVE_TRIGGER_HAND: Final = "ProduceCardMoveEffectTriggerType_Hand"

CREATE_ID_SLEEP_EFFECT: Final = (
    "e_effect-exam_card_create_id-p_card-00-acc-0_002-0-deck_random-1_1"
)
CREATE_ID_IDOL_EFFECT: Final = (
    "e_effect-exam_card_create_id-p_card-02-ido-3_232-0-deck_random-5_5"
)
CREATE_ID_EFFECT_IDS: Final = frozenset(
    (CREATE_ID_SLEEP_EFFECT, CREATE_ID_IDOL_EFFECT)
)
PLAY_COUNT_CARD_ID: Final = "p_card-02-ido-3_192"
MOVE_BLOCK_CARD_ID: Final = "p_card-02-act-100_010"
MOVE_AGGRESSIVE_CARD_ID: Final = "p_card-02-ido-3_214"

EXPECTED_OCCURRENCE_COUNT: Final = 25
EXPECTED_VERSION_COUNT: Final = 21
EXPECTED_EFFECT_ID_COUNT: Final = 5
EXPECTED_TYPE_COUNTS: Final = MappingProxyType({
    CARD_CREATE_ID: 13,
    CARD_CREATE_SEARCH: 4,
    PLAY_COUNT_BUFF: 4,
    FORCE_PLAY_SEARCH: 4,
})
EXPECTED_COMPANION_BLOCKER_COUNT: Final = 5
EXPECTED_WHOLE_CARD_DIRECT_COUNT: Final = 16
EXPECTED_WHOLE_CARD_CO_BLOCKED_COUNT: Final = 5
EXPECTED_HAND_MOVE_VERSION_COUNT: Final = 5
EXPECTED_HAND_MOVE_BLOCKER_COUNT: Final = 10

Operation: TypeAlias = Literal[
    "card-create-id",
    "card-create-search",
    "play-count-buff",
    "force-play-search",
]
MoveExecutor: TypeAlias = Literal["block-five", "aggressive-three"]


class Plan2NativeGeneratedCardError(ValueError):
    """Stable compiler/runtime failure at an unproven boundary."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value):
        raise Plan2NativeGeneratedCardError("invalid-text", label)
    return value


def _nonnegative(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise Plan2NativeGeneratedCardError("invalid-nonnegative-int", label)
    return value


def _json_object(value: object, label: str) -> Mapping[str, object]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2NativeGeneratedCardError("invalid-json", label) from error
    if not isinstance(parsed, Mapping):
        raise Plan2NativeGeneratedCardError("invalid-json-object", label)
    return parsed


def _effect_ids(raw: Mapping[str, object], label: str) -> tuple[str, ...]:
    effects = raw.get("playEffects")
    if not isinstance(effects, list):
        raise Plan2NativeGeneratedCardError("invalid-play-effects", label)
    result: list[str] = []
    for index, entry in enumerate(effects):
        if not isinstance(entry, Mapping):
            raise Plan2NativeGeneratedCardError(
                "invalid-play-effect-entry", f"{label}:{index}"
            )
        effect_id = entry.get("produceExamEffectId")
        _text(effect_id, f"{label}:{index}:effect-id")
        result.append(effect_id)
    return tuple(result)


@dataclass(frozen=True, slots=True, order=True)
class GeneratedCardBlocker:
    code: str
    card_id: str
    upgrade: int
    slot_index: int = -1
    detail: str = ""

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade


@dataclass(frozen=True, slots=True)
class GeneratedCardProgram:
    card_id: str
    upgrade: int
    plan_type: str
    slot_index: int
    ordered_effect_ids: tuple[str, ...]
    effect_id: str
    effect_type: str
    operation: Operation
    target_card_id: str = ""
    target_upgrade: int = 0
    destination: str = ""
    count_min: int = 0
    count_max: int = 0

    def __post_init__(self) -> None:
        _text(self.card_id, "program.card_id")
        _nonnegative(self.upgrade, "program.upgrade")
        _nonnegative(self.slot_index, "program.slot_index")
        if self.plan_type not in PLANS:
            raise Plan2NativeGeneratedCardError("unsupported-plan", self.plan_type)
        if self.effect_type not in TARGET_TYPES:
            raise Plan2NativeGeneratedCardError("unsupported-effect-type")
        if self.slot_index >= len(self.ordered_effect_ids) or self.ordered_effect_ids[
            self.slot_index
        ] != self.effect_id:
            raise Plan2NativeGeneratedCardError("effect-order-drift", self.effect_id)

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def prior_effect_ids(self) -> tuple[str, ...]:
        return self.ordered_effect_ids[: self.slot_index]

    @property
    def following_effect_ids(self) -> tuple[str, ...]:
        return self.ordered_effect_ids[self.slot_index + 1 :]


@dataclass(frozen=True, slots=True)
class GeneratedCardVersion:
    card_id: str
    upgrade: int
    plan_type: str
    ordered_effect_ids: tuple[str, ...]
    programs: tuple[GeneratedCardProgram, ...]

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade


@dataclass(frozen=True, slots=True)
class GeneratedHandMoveProgram:
    card_id: str
    upgrade: int
    ordered_effect_ids: tuple[str, ...]
    move_effect_ids: tuple[str, ...]
    executor: MoveExecutor
    play_trigger_id: str = ""
    move_trigger_type: str = MOVE_TRIGGER_HAND

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade


@dataclass(frozen=True, slots=True)
class GeneratedCardCatalog:
    programs: tuple[GeneratedCardProgram, ...]
    versions: tuple[GeneratedCardVersion, ...]
    hand_move_programs: tuple[GeneratedHandMoveProgram, ...]

    def program(self, card_id: str, upgrade: int, effect_id: str) -> GeneratedCardProgram:
        found = tuple(
            item
            for item in self.programs
            if item.card_id == card_id
            and item.upgrade == upgrade
            and item.effect_id == effect_id
        )
        if len(found) != 1:
            raise KeyError((card_id, upgrade, effect_id))
        return found[0]

    def version(self, card_id: str, upgrade: int) -> GeneratedCardVersion:
        return next(
            item
            for item in self.versions
            if item.card_id == card_id and item.upgrade == upgrade
        )

    def hand_move(self, card_id: str, upgrade: int) -> GeneratedHandMoveProgram:
        return next(
            item
            for item in self.hand_move_programs
            if item.card_id == card_id and item.upgrade == upgrade
        )


@dataclass(frozen=True, slots=True)
class GeneratedCardCompilation:
    catalog: GeneratedCardCatalog
    blockers: tuple[GeneratedCardBlocker, ...]
    companion_blockers: tuple[GeneratedCardBlocker, ...]
    whole_card_direct_refs: tuple[tuple[str, int], ...]
    whole_card_co_blocked_refs: tuple[tuple[str, int], ...]
    central_move_blockers: tuple[GeneratedCardBlocker, ...]

    @property
    def fully_compiled(self) -> bool:
        return not self.blockers

    @property
    def occurrence_count(self) -> int:
        return len(self.catalog.programs)

    @property
    def effect_id_count(self) -> int:
        return len({item.effect_id for item in self.catalog.programs})

    @property
    def type_counts(self) -> Mapping[str, int]:
        return dict(Counter(item.effect_type for item in self.catalog.programs))

    @property
    def companion_code_counts(self) -> Mapping[str, int]:
        return dict(Counter(item.code for item in self.companion_blockers))

    @property
    def whole_card_direct_when_exact_companions_ready_refs(
        self,
    ) -> tuple[tuple[str, int], ...]:
        """All target versions once the exact Timer-chain leaf is composed."""

        return tuple(sorted(item.ref for item in self.catalog.versions))


_OPERATION_BY_TYPE: Mapping[str, Operation] = {
    CARD_CREATE_ID: "card-create-id",
    CARD_CREATE_SEARCH: "card-create-search",
    PLAY_COUNT_BUFF: "play-count-buff",
    FORCE_PLAY_SEARCH: "force-play-search",
}


def _validate_target_contract(
    row: Mapping[str, object],
    *,
    plan_type: str,
    card_id: str,
    database: Path,
    master_card_refs: frozenset[tuple[str, int]],
) -> tuple[str, int, str, int, int]:
    effect_type = str(row["effect_type"])
    effect_id = str(row["id"])
    if effect_type == CARD_CREATE_ID:
        contract = resolve_plan2_card_create_contract(row, plan_type=plan_type)
        if effect_id not in CREATE_ID_EFFECT_IDS:
            raise Plan2NativeGeneratedCardError("out-of-scope-create-id", effect_id)
        if (contract.card_id, contract.upgrade) not in master_card_refs:
            raise Plan2NativeGeneratedCardError(
                "created-card-master-missing",
                f"{contract.card_id}@{contract.upgrade}",
            )
        return (
            contract.card_id,
            contract.upgrade,
            contract.destination,
            contract.count_min,
            contract.count_max,
        )
    if effect_type == CARD_CREATE_SEARCH:
        contract = resolve_plan2_card_create_search_contract(row, plan_type=plan_type)
        return "", 0, contract.destination, contract.pick_count_min, contract.pick_count_max
    if effect_type == PLAY_COUNT_BUFF:
        if card_id != PLAY_COUNT_CARD_ID or effect_id != PLAY_COUNT_BUFF_EFFECT_ID:
            raise Plan2NativeGeneratedCardError("out-of-scope-play-count-buff", effect_id)
        contract = load_card_search_effect_play_count_buff_contract(database)
        if not contract.executable:
            raise Plan2NativeGeneratedCardError("play-count-buff-contract")
        return "", 0, "playing", contract.row.effect_count, contract.row.effect_count
    if effect_type == FORCE_PLAY_SEARCH:
        if card_id != PLAY_COUNT_CARD_ID or effect_id != FORCE_PLAY_EFFECT_ID:
            raise Plan2NativeGeneratedCardError("out-of-scope-force-play", effect_id)
        handoff = load_force_play_handoff_contract(database)
        contract = resolve_force_play_card_search_contract(row, database=database)
        if not handoff.executable or not contract.executable:
            raise Plan2NativeGeneratedCardError("force-play-contract")
        return "", 0, "deck_grave", 1, 1
    raise Plan2NativeGeneratedCardError("unsupported-effect-type", effect_type)


def compile_plan2_native_catalog_generated_cards(
    database: Path = DEFAULT_DATABASE,
) -> GeneratedCardCompilation:
    """Compile only the exact Plan2/Common generated-card and Hand slice."""

    path = Path(database)
    programs: list[GeneratedCardProgram] = []
    versions: list[GeneratedCardVersion] = []
    blockers: list[GeneratedCardBlocker] = []
    companions: list[GeneratedCardBlocker] = []
    move_programs: list[GeneratedHandMoveProgram] = []
    move_blockers: list[GeneratedCardBlocker] = []
    try:
        with closing(sqlite3.connect(path)) as connection:
            connection.row_factory = sqlite3.Row
            effect_rows = {
                str(row["id"]): dict(row)
                for row in connection.execute("SELECT * FROM effect")
            }
            card_rows = tuple(
                connection.execute(
                    "SELECT id, upgrade_count, plan_type, raw_json FROM card "
                    "WHERE plan_type IN (?, ?) ORDER BY id, upgrade_count",
                    (PLAN_COMMON, PLAN2),
                )
            )
    except sqlite3.Error as error:
        raise Plan2NativeGeneratedCardError("master-read-failed", str(error)) from error

    master_card_refs = frozenset(
        (str(row["id"]), int(row["upgrade_count"])) for row in card_rows
    )
    for card_row in card_rows:
        card_id = str(card_row["id"])
        upgrade = int(card_row["upgrade_count"])
        plan_type = str(card_row["plan_type"])
        try:
            raw = _json_object(card_row["raw_json"], f"{card_id}#{upgrade}")
            ordered = _effect_ids(raw, f"{card_id}#{upgrade}")
        except Plan2NativeGeneratedCardError as error:
            blockers.append(GeneratedCardBlocker(error.code, card_id, upgrade, detail=error.detail))
            continue

        target_slots = tuple(
            (slot, effect_rows.get(effect_id))
            for slot, effect_id in enumerate(ordered)
            if effect_rows.get(effect_id, {}).get("effect_type") in TARGET_TYPES
        )
        current: list[GeneratedCardProgram] = []
        for slot, effect_row in target_slots:
            assert effect_row is not None
            effect_id = ordered[slot]
            effect_type = str(effect_row["effect_type"])
            try:
                play_effects = raw["playEffects"]
                assert isinstance(play_effects, list)
                entry = play_effects[slot]
                assert isinstance(entry, Mapping)
                if entry.get("produceExamTriggerId", "") not in (None, ""):
                    raise Plan2NativeGeneratedCardError(
                        "target-effect-is-triggered", f"{card_id}#{upgrade}:{slot}"
                    )
                if entry.get("isOncePlayEffect") is not False:
                    raise Plan2NativeGeneratedCardError(
                        "target-effect-once-flag-drift",
                        f"{card_id}#{upgrade}:{slot}",
                    )
                target_id, target_upgrade, destination, count_min, count_max = (
                    _validate_target_contract(
                        effect_row,
                        plan_type=plan_type,
                        card_id=card_id,
                        database=path,
                        master_card_refs=master_card_refs,
                    )
                )
                program = GeneratedCardProgram(
                    card_id,
                    upgrade,
                    plan_type,
                    slot,
                    ordered,
                    effect_id,
                    effect_type,
                    _OPERATION_BY_TYPE[effect_type],
                    target_id,
                    target_upgrade,
                    destination,
                    count_min,
                    count_max,
                )
                programs.append(program)
                current.append(program)
            except Exception as error:
                blockers.append(
                    GeneratedCardBlocker(
                        getattr(error, "code", "target-contract-failed"),
                        card_id,
                        upgrade,
                        slot,
                        getattr(error, "detail", str(error)),
                    )
                )
        if current:
            if raw.get("playProduceExamTriggerId", "") not in (None, ""):
                blockers.append(
                    GeneratedCardBlocker(
                        "target-card-play-triggered",
                        card_id,
                        upgrade,
                        detail=str(raw.get("playProduceExamTriggerId")),
                    )
                )
            versions.append(
                GeneratedCardVersion(card_id, upgrade, plan_type, ordered, tuple(current))
            )
            for slot, sibling_id in enumerate(ordered):
                if any(item.slot_index == slot for item in current):
                    continue
                sibling = effect_rows.get(sibling_id)
                if sibling and sibling.get("effect_type") == EFFECT_TIMER:
                    companions.append(
                        GeneratedCardBlocker(
                            "effect-chain-runtime-unbound",
                            card_id,
                            upgrade,
                            slot,
                            sibling_id,
                        )
                    )

        if plan_type == PLAN2 and raw.get("moveEffectTriggerType") == MOVE_TRIGGER_HAND:
            move_ids_raw = raw.get("moveProduceExamEffectIds")
            trigger_ids_raw = raw.get("moveProduceExamTriggerIds")
            if not isinstance(move_ids_raw, list) or not isinstance(trigger_ids_raw, list):
                blockers.append(
                    GeneratedCardBlocker("hand-move-list-shape", card_id, upgrade)
                )
                continue
            move_ids = tuple(str(value) for value in move_ids_raw)
            trigger_ids = tuple(str(value) for value in trigger_ids_raw)
            try:
                if trigger_ids:
                    raise Plan2NativeGeneratedCardError("hand-move-trigger-ids-not-empty")
                if card_id == MOVE_BLOCK_CARD_ID and upgrade == 0:
                    contract = load_plan2_final_card_contract(path)
                    if move_ids != (contract.move_effect.effect_id,):
                        raise Plan2NativeGeneratedCardError("hand-move-child-drift")
                    executor: MoveExecutor = "block-five"
                elif card_id == MOVE_AGGRESSIVE_CARD_ID and upgrade in (0, 1, 2, 3):
                    contracts = load_all_plan2_move_card_play_aggressive_contracts(path)
                    contract = contracts[upgrade]
                    if move_ids != (contract.effect_id,):
                        raise Plan2NativeGeneratedCardError("hand-move-child-drift")
                    executor = "aggressive-three"
                else:
                    raise Plan2NativeGeneratedCardError("out-of-scope-hand-move")
                move_programs.append(
                    GeneratedHandMoveProgram(
                        card_id,
                        upgrade,
                        ordered,
                        move_ids,
                        executor,
                        str(raw.get("playProduceExamTriggerId", "")),
                    )
                )
                move_blockers.extend(
                    (
                        GeneratedCardBlocker("move-trigger-type-unbound", card_id, upgrade),
                        GeneratedCardBlocker(
                            "move-effect-runtime-unbound",
                            card_id,
                            upgrade,
                            detail=move_ids[0],
                        ),
                    )
                )
            except Exception as error:
                blockers.append(
                    GeneratedCardBlocker(
                        getattr(error, "code", "hand-move-contract-failed"),
                        card_id,
                        upgrade,
                        detail=getattr(error, "detail", str(error)),
                    )
                )

    # Cross-check the exact helper-owned card version sets, independently of
    # the discovery loop above.
    try:
        if len(load_plan2_common_card_versions(path)) != 4:
            raise Plan2NativeGeneratedCardError("create-search-version-count")
        if len(load_play_count_buff_versions(path)) != 4:
            raise Plan2NativeGeneratedCardError("buff-version-count")
    except Exception as error:
        blockers.append(
            GeneratedCardBlocker(
                getattr(error, "code", "helper-cross-check-failed"),
                "<catalog>",
                0,
                detail=getattr(error, "detail", str(error)),
            )
        )

    companion_refs = {item.ref for item in companions}
    refs = {item.ref for item in versions}
    direct = tuple(sorted(refs - companion_refs))
    co = tuple(sorted(refs & companion_refs))
    catalog = GeneratedCardCatalog(
        tuple(programs), tuple(versions), tuple(move_programs)
    )
    compilation = GeneratedCardCompilation(
        catalog,
        tuple(blockers),
        tuple(companions),
        direct,
        co,
        tuple(move_blockers),
    )
    if compilation.fully_compiled:
        checks = (
            (compilation.occurrence_count, EXPECTED_OCCURRENCE_COUNT, "occurrence-accounting-drift"),
            (len(catalog.versions), EXPECTED_VERSION_COUNT, "version-accounting-drift"),
            (compilation.effect_id_count, EXPECTED_EFFECT_ID_COUNT, "effect-id-accounting-drift"),
            (len(companions), EXPECTED_COMPANION_BLOCKER_COUNT, "companion-accounting-drift"),
            (len(direct), EXPECTED_WHOLE_CARD_DIRECT_COUNT, "direct-accounting-drift"),
            (len(co), EXPECTED_WHOLE_CARD_CO_BLOCKED_COUNT, "co-accounting-drift"),
            (len(move_programs), EXPECTED_HAND_MOVE_VERSION_COUNT, "move-version-accounting-drift"),
            (len(move_blockers), EXPECTED_HAND_MOVE_BLOCKER_COUNT, "move-blocker-accounting-drift"),
        )
        for actual, expected, code in checks:
            if actual != expected:
                raise Plan2NativeGeneratedCardError(code, f"{actual}!={expected}")
        if compilation.type_counts != EXPECTED_TYPE_COUNTS:
            raise Plan2NativeGeneratedCardError("type-accounting-drift")
    return compilation


load_plan2_native_catalog_generated_cards = compile_plan2_native_catalog_generated_cards


@dataclass(frozen=True, slots=True)
class GeneratedCardCreateInput:
    """Caller-owned context absent from :class:`Plan3NativeState`."""

    completed_effect_ids: tuple[str, ...] | None = None
    guid_tokens: tuple[str, ...] | None = None
    plan_ignore_card_ids: tuple[str, ...] | None = None
    hand_limit: int | None = None

    def __post_init__(self) -> None:
        for name in ("completed_effect_ids", "guid_tokens", "plan_ignore_card_ids"):
            value = getattr(self, name)
            if value is None:
                continue
            normalized = tuple(value)
            if any(type(item) is not str or not item for item in normalized):
                raise Plan2NativeGeneratedCardError("invalid-text-sequence", name)
            object.__setattr__(self, name, normalized)
        if self.hand_limit is not None:
            _nonnegative(self.hand_limit, "hand_limit")


@dataclass(frozen=True, slots=True)
class GeneratedCardCreateExecution:
    before: Plan3NativeState
    after: Plan3NativeState
    program: GeneratedCardProgram
    exact: CardCreateResult | CardCreateSearchResult | None
    created_cards: tuple[Plan3NativeCard, ...] = ()
    unresolved: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return not self.unresolved and self.exact is not None

    @property
    def state_unchanged(self) -> bool:
        return self.before is self.after


def _created_cards(state: Plan3NativeState, guids: Sequence[str]) -> tuple[Plan3NativeCard, ...]:
    return tuple(state.card_by_guid(guid) for guid in guids)


def execute_plan2_generated_card_create(
    state: Plan3NativeState,
    program: GeneratedCardProgram,
    supplied: GeneratedCardCreateInput,
    *,
    database: Path = DEFAULT_DATABASE,
) -> GeneratedCardCreateExecution:
    """Execute one create slot in Master order, or return an unchanged no-op."""

    if not isinstance(state, Plan3NativeState):
        raise Plan2NativeGeneratedCardError("invalid-native-state")
    if not isinstance(program, GeneratedCardProgram) or program.operation not in {
        "card-create-id",
        "card-create-search",
    }:
        raise Plan2NativeGeneratedCardError("not-a-create-program")
    if not isinstance(supplied, GeneratedCardCreateInput):
        raise Plan2NativeGeneratedCardError("invalid-create-input")
    if supplied.completed_effect_ids is None:
        return GeneratedCardCreateExecution(
            state, state, program, None, unresolved=("completed_effect_ids",)
        )
    if supplied.completed_effect_ids != program.prior_effect_ids:
        return GeneratedCardCreateExecution(
            state, state, program, None, unresolved=("effect-order-mismatch",)
        )
    if supplied.guid_tokens is None:
        return GeneratedCardCreateExecution(
            state, state, program, None, unresolved=("guid_tokens",)
        )
    path = Path(database)
    try:
        with closing(sqlite3.connect(path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM effect WHERE id = ?", (program.effect_id,)
            ).fetchone()
        if row is None:
            raise Plan2NativeGeneratedCardError("effect-row-missing", program.effect_id)
        if program.operation == "card-create-id":
            contract = resolve_plan2_card_create_contract(row, plan_type=program.plan_type)
            if (
                contract.card_id,
                contract.upgrade,
                contract.destination,
                contract.count_min,
                contract.count_max,
            ) != (
                program.target_card_id,
                program.target_upgrade,
                program.destination,
                program.count_min,
                program.count_max,
            ):
                raise CardCreateInputError("compiled-target-contract-drift")
            exact = apply_plan2_card_create_id(
                state,
                contract,
                guid_tokens=supplied.guid_tokens,
                hand_limit=supplied.hand_limit,
            )
            guids = exact.trace.allocated_guids
        else:
            contract = resolve_plan2_card_create_search_contract(
                row, plan_type=program.plan_type
            )
            chance = CardCreateSearchChanceInput(
                plan_type=program.plan_type,
                plan_ignore_card_ids=supplied.plan_ignore_card_ids,
                hand_limit=supplied.hand_limit,
                guid_tokens=supplied.guid_tokens,
            )
            exact = apply_plan2_card_create_search(
                state, contract, chance_input=chance
            )
            guids = exact.trace.guid_tokens
    except (sqlite3.Error, CardCreateInputError, CardCreateSearchInputError) as error:
        return GeneratedCardCreateExecution(
            state,
            state,
            program,
            None,
            unresolved=(getattr(error, "code", "create-input-error"),),
        )
    if exact.unresolved:
        required = tuple(item.field for item in exact.trace.unresolved_inputs)
        return GeneratedCardCreateExecution(
            state,
            state,
            program,
            exact,
            unresolved=required or (getattr(exact.branch, "reason", "unresolved"),),
        )
    cards = _created_cards(exact.after, guids)
    if any(
        card.temporary_upgrade != 0
        or card.base_upgrade != card.effective_upgrade
        for card in cards
    ):
        raise Plan2NativeGeneratedCardError("created-upgrade-invariant")
    return GeneratedCardCreateExecution(state, exact.after, program, exact, cards)


@dataclass(frozen=True, slots=True)
class GeneratedStatusUidInput:
    """Observed native identity; zero/absence is never synthesized here."""

    native_uid: int | None

    def __post_init__(self) -> None:
        if self.native_uid is not None and (
            type(self.native_uid) is not int or self.native_uid <= 0
        ):
            raise Plan2NativeGeneratedCardError("invalid-native-status-uid")


@dataclass(frozen=True, slots=True)
class GeneratedBuffInstall:
    before: PlayCountBuffRuntime
    after: PlayCountBuffRuntime
    status: PlayCountBuffStatus | None
    accepted: bool
    unresolved: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return not self.unresolved


def install_plan2_generated_play_count_buff(
    runtime: PlayCountBuffRuntime,
    program: GeneratedCardProgram,
    uid_input: GeneratedStatusUidInput,
    *,
    native_status_add_blocked: bool,
    database: Path = DEFAULT_DATABASE,
) -> GeneratedBuffInstall:
    """Append one exact status layer with a caller-observed native UID."""

    if program.operation != "play-count-buff":
        raise Plan2NativeGeneratedCardError("not-a-play-count-buff-program")
    if native_status_add_blocked:
        contract = load_card_search_effect_play_count_buff_contract(Path(database))
        exact = install_card_search_effect_play_count_buff(
            runtime, contract, native_status_add_blocked=True
        )
        return GeneratedBuffInstall(runtime, runtime, exact.status, False)
    if uid_input.native_uid is None:
        return GeneratedBuffInstall(
            runtime, runtime, None, False, ("native_status_uid",)
        )
    if any(status.native_uid == uid_input.native_uid for status in runtime.statuses):
        return GeneratedBuffInstall(
            runtime, runtime, None, False, ("native_status_uid_collision",)
        )
    contract = load_card_search_effect_play_count_buff_contract(Path(database))
    exact = install_card_search_effect_play_count_buff(
        runtime, contract, native_status_add_blocked=False
    )
    status = replace(exact.status, native_uid=uid_input.native_uid)
    after = PlayCountBuffRuntime((*runtime.statuses, status))
    return GeneratedBuffInstall(runtime, after, status, True)


@dataclass(frozen=True, slots=True)
class GeneratedBuffForceInput:
    parent_guid: str
    selected_guid: str | None
    status_uid: GeneratedStatusUidInput
    native_status_add_blocked: bool = False
    enchant_effect_uid: int = 0

    def __post_init__(self) -> None:
        _text(self.parent_guid, "parent_guid")
        if self.selected_guid is not None:
            _text(self.selected_guid, "selected_guid")
        if not isinstance(self.status_uid, GeneratedStatusUidInput):
            raise Plan2NativeGeneratedCardError("invalid-status-uid-input")
        if type(self.native_status_add_blocked) is not bool:
            raise Plan2NativeGeneratedCardError("invalid-status-block-gate")
        _nonnegative(self.enchant_effect_uid, "enchant_effect_uid")


@dataclass(frozen=True, slots=True)
class GeneratedBuffForceExecution:
    before_plan2: Plan2State
    after_plan2: Plan2State
    before_native: Plan3NativeState
    after_native: Plan3NativeState
    before_runtime: PlayCountBuffRuntime
    after_runtime: PlayCountBuffRuntime
    install: GeneratedBuffInstall | None
    force_plan: ForcePlayPlanResult | None
    handoff: Plan2CardSearchPlayCountBuffHandoff | None
    pre_settlement: Plan2ForcePlayBuffQueueExecution | None
    settlement: Plan2ForcePlayQueueExecution | None
    unresolved: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return not self.unresolved and self.settlement is not None

    @property
    def child_history_event_count(self) -> int:
        return 0 if self.settlement is None else self.settlement.history_event_count

    @property
    def child_final_move_count(self) -> int:
        return 0 if self.settlement is None else self.settlement.final_move_count

    @property
    def child_global_play_count_delta(self) -> int:
        return (
            0
            if self.settlement is None
            else self.settlement.global_card_play_count_delta
        )


def _empty_buff_force(
    plan2: Plan2State,
    native: Plan3NativeState,
    runtime: PlayCountBuffRuntime,
    *,
    reason: str,
    install: GeneratedBuffInstall | None = None,
    force_plan: ForcePlayPlanResult | None = None,
    handoff: Plan2CardSearchPlayCountBuffHandoff | None = None,
    pre_settlement: Plan2ForcePlayBuffQueueExecution | None = None,
) -> GeneratedBuffForceExecution:
    return GeneratedBuffForceExecution(
        plan2,
        plan2,
        native,
        native,
        runtime,
        runtime,
        install,
        force_plan,
        handoff,
        pre_settlement,
        None,
        (reason,),
    )


def execute_plan2_generated_buff_force_chain(
    plan2_state: Plan2State,
    native_state: Plan3NativeState,
    runtime: PlayCountBuffRuntime,
    buff_program: GeneratedCardProgram,
    force_program: GeneratedCardProgram,
    supplied: GeneratedBuffForceInput,
    *,
    database: Path = DEFAULT_DATABASE,
) -> GeneratedBuffForceExecution:
    """Install parent status, queue Select force play, consume, then settle.

    Queue construction is state-pure and does not consume the listener.  The
    selected Deck/Grave GUID is rematched when its UsePool command executes;
    only then is the newest matching status consumed.  The shared exact force
    settlement owns the child's card/global/turn counts, history, and move.
    Parent-card settlement remains outside this direct-slot leaf.
    """

    if buff_program.ref != force_program.ref or buff_program.ref != (
        PLAY_COUNT_CARD_ID,
        buff_program.upgrade,
    ):
        raise Plan2NativeGeneratedCardError("buff-force-card-mismatch")
    if buff_program.slot_index + 1 != force_program.slot_index:
        raise Plan2NativeGeneratedCardError("buff-force-order-drift")
    if supplied.selected_guid is None:
        return _empty_buff_force(
            plan2_state, native_state, runtime, reason="selected_guid"
        )
    path = Path(database)
    install = install_plan2_generated_play_count_buff(
        runtime,
        buff_program,
        supplied.status_uid,
        native_status_add_blocked=supplied.native_status_add_blocked,
        database=path,
    )
    if install.unresolved:
        return _empty_buff_force(
            plan2_state,
            native_state,
            runtime,
            reason=install.unresolved[0],
            install=install,
        )
    try:
        with closing(sqlite3.connect(path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM effect WHERE id = ?", (force_program.effect_id,)
            ).fetchone()
        if row is None:
            raise Plan2NativeGeneratedCardError("effect-row-missing", force_program.effect_id)
        contract = resolve_force_play_card_search_contract(row, database=path)
        force_plan = plan_force_play_card_search(
            native_state,
            contract,
            playing_guid=supplied.parent_guid,
            execution_input=ForcePlayExecutionInput(
                (supplied.selected_guid,), supplied.enchant_effect_uid
            ),
            database=path,
        )
    except Exception as error:
        return _empty_buff_force(
            plan2_state,
            native_state,
            runtime,
            reason=getattr(error, "code", "force-plan-failed"),
            install=install,
        )
    if force_plan.unresolved:
        return _empty_buff_force(
            plan2_state,
            native_state,
            runtime,
            reason=getattr(force_plan.branch, "reason", "force-plan-unresolved"),
            install=install,
            force_plan=force_plan,
        )
    handoff = build_force_play_handoff(
        install.after,
        force_plan.commands,
        differences=force_plan.differences,
        parent_guid=supplied.parent_guid,
        database=path,
    )
    if not handoff.executable:
        return _empty_buff_force(
            plan2_state,
            native_state,
            runtime,
            reason=handoff.unresolved[0],
            install=install,
            force_plan=force_plan,
            handoff=handoff,
        )
    pre = execute_force_play_buff_handoff(
        plan2_state, native_state, install.after, handoff, database=path
    )
    if not pre.executable:
        return _empty_buff_force(
            plan2_state,
            native_state,
            runtime,
            reason=pre.unresolved[0],
            install=install,
            force_plan=force_plan,
            handoff=handoff,
            pre_settlement=pre,
        )
    # The Plan2 settlement adapter uses its own nominal command class, while
    # the shared planner deliberately returns the common native shape.  Copy
    # only the already-resolved immutable fields; PlayHand is the Plan2
    # adapter's asserted UsePool source-position sentinel, and live source
    # zone/index are still rematched by GUID at execution time.
    settlement_commands = tuple(
        Plan2ForcePlayQueuedCommand(
            command.ordinal,
            command.guid,
            command.card,
            command.original_source_zone,
            "ProduceCardPositionType_PlayHand",
            command.original_source_index,
            command.base_move_position_type,
            command.enchant_effect_uid,
        )
        for command in force_plan.commands
    )
    settlement = execute_plan2_force_play_queue(
        plan2_state, native_state, settlement_commands, database=path
    )
    return GeneratedBuffForceExecution(
        plan2_state,
        settlement.after_plan2,
        native_state,
        settlement.after_native,
        runtime,
        pre.after_runtime,
        install,
        force_plan,
        handoff,
        pre,
        settlement,
    )


@dataclass(frozen=True, slots=True)
class GeneratedHandMoveSettlement:
    exam_turn: int
    guid: str
    source_zone: str
    destination_zone: str
    reason: str
    effect_id: str
    triggered: bool
    executed: bool
    card_play_count_delta: int = 0
    global_play_count_delta: int = 0


@dataclass(frozen=True, slots=True)
class GeneratedHandMoveTurnState:
    """Typed per-turn state required where the shared state has no owner."""

    exam_turn: int
    used_guids: frozenset[str] = frozenset()
    settlements: tuple[GeneratedHandMoveSettlement, ...] = ()

    def __post_init__(self) -> None:
        _nonnegative(self.exam_turn, "exam_turn")
        if not isinstance(self.used_guids, frozenset) or any(
            type(value) is not str or not value for value in self.used_guids
        ):
            raise Plan2NativeGeneratedCardError("invalid-used-guid-state")
        if any(
            not isinstance(value, GeneratedHandMoveSettlement)
            for value in self.settlements
        ):
            raise Plan2NativeGeneratedCardError("invalid-move-settlement-history")


def start_plan2_generated_hand_move_turn(
    state: GeneratedHandMoveTurnState, next_exam_turn: int
) -> GeneratedHandMoveTurnState:
    _nonnegative(next_exam_turn, "next_exam_turn")
    if next_exam_turn <= state.exam_turn:
        raise Plan2NativeGeneratedCardError("non-forward-turn-boundary")
    return GeneratedHandMoveTurnState(next_exam_turn, frozenset(), state.settlements)


@dataclass(frozen=True, slots=True)
class GeneratedHandMoveExecution:
    program: GeneratedHandMoveProgram
    before_turn: GeneratedHandMoveTurnState
    after_turn: GeneratedHandMoveTurnState
    exact: HandMoveTransition | HandMoveAggressiveRuntimeResult | None
    unresolved: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return not self.unresolved and self.exact is not None

    @property
    def settlement_count(self) -> int:
        return len(self.after_turn.settlements) - len(self.before_turn.settlements)


def _append_move_settlement(
    state: GeneratedHandMoveTurnState,
    program: GeneratedHandMoveProgram,
    *,
    guid: str,
    source: str,
    destination: str,
    reason: str,
    triggered: bool,
    executed: bool,
) -> GeneratedHandMoveTurnState:
    used = state.used_guids | ({guid} if executed else set())
    settlement = GeneratedHandMoveSettlement(
        state.exam_turn,
        guid,
        source,
        destination,
        reason,
        program.move_effect_ids[0],
        triggered,
        executed,
    )
    return GeneratedHandMoveTurnState(
        state.exam_turn, frozenset(used), (*state.settlements, settlement)
    )


def execute_plan2_generated_hand_move_block(
    program: GeneratedHandMoveProgram,
    runtime: FinalCardRuntime,
    turn_state: GeneratedHandMoveTurnState,
    *,
    guid: str,
    source_zone: Literal["deck", "grave", "hold"],
    reason: Literal["draw", "replace", "hand-add", "card-move"],
    database: Path = DEFAULT_DATABASE,
) -> GeneratedHandMoveExecution:
    if program.executor != "block-five":
        raise Plan2NativeGeneratedCardError("wrong-hand-move-executor")
    try:
        card = runtime.native_state.card_by_guid(guid)
    except ValueError:
        return GeneratedHandMoveExecution(
            program, turn_state, turn_state, None, ("guid-not-found",)
        )
    if card.move_effect_used_in_turn != (guid in turn_state.used_guids):
        return GeneratedHandMoveExecution(
            program,
            turn_state,
            turn_state,
            None,
            ("per-guid-turn-state-drift",),
        )
    try:
        exact = move_final_card_to_hand(
            runtime,
            guid,
            source_zone=source_zone,
            reason=reason,
            database=Path(database),
        )
    except Exception as error:
        return GeneratedHandMoveExecution(
            program,
            turn_state,
            turn_state,
            None,
            (getattr(error, "code", "hand-move-failed"),),
        )
    after_turn = _append_move_settlement(
        turn_state,
        program,
        guid=guid,
        source=source_zone,
        destination="hand",
        reason=reason,
        triggered=exact.move_effect_triggered,
        executed=exact.move_effect_executed,
    )
    return GeneratedHandMoveExecution(program, turn_state, after_turn, exact)


def execute_plan2_generated_hand_move_aggressive(
    program: GeneratedHandMoveProgram,
    event: HandMoveCommitEvent,
    runtime: HandMoveAggressiveRuntimeState,
    turn_state: GeneratedHandMoveTurnState,
    *,
    database: Path = DEFAULT_DATABASE,
) -> GeneratedHandMoveExecution:
    if program.executor != "aggressive-three":
        raise Plan2NativeGeneratedCardError("wrong-hand-move-executor")
    exact = execute_plan2_move_card_play_aggressive(
        event,
        runtime,
        used_move_effect_guids=turn_state.used_guids,
        database=Path(database),
    )
    if not exact.resolved:
        return GeneratedHandMoveExecution(
            program,
            turn_state,
            turn_state,
            exact,
            (exact.reason or "hand-move-unresolved",),
        )
    after_turn = _append_move_settlement(
        turn_state,
        program,
        guid=event.moved_guid,
        source=event.source_zone,
        destination=event.destination_zone,
        reason=str(getattr(event.move_reason, "value", event.move_reason)),
        triggered=exact.triggered,
        executed=exact.executed,
    )
    if after_turn.used_guids != exact.used_move_effect_guids:
        raise Plan2NativeGeneratedCardError("per-guid-turn-state-drift")
    return GeneratedHandMoveExecution(program, turn_state, after_turn, exact)


compile_generated_cards = compile_plan2_native_catalog_generated_cards


__all__ = [
    "CARD_CREATE_ID",
    "CARD_CREATE_SEARCH",
    "CREATE_ID_EFFECT_IDS",
    "CREATE_ID_IDOL_EFFECT",
    "CREATE_ID_SLEEP_EFFECT",
    "CREATE_SEARCH_EFFECT_ID",
    "EXPECTED_COMPANION_BLOCKER_COUNT",
    "EXPECTED_EFFECT_ID_COUNT",
    "EXPECTED_HAND_MOVE_BLOCKER_COUNT",
    "EXPECTED_HAND_MOVE_VERSION_COUNT",
    "EXPECTED_OCCURRENCE_COUNT",
    "EXPECTED_TYPE_COUNTS",
    "EXPECTED_VERSION_COUNT",
    "EXPECTED_WHOLE_CARD_CO_BLOCKED_COUNT",
    "EXPECTED_WHOLE_CARD_DIRECT_COUNT",
    "FORCE_PLAY_EFFECT_ID",
    "FORCE_PLAY_SEARCH",
    "GeneratedBuffForceExecution",
    "GeneratedBuffForceInput",
    "GeneratedBuffInstall",
    "GeneratedCardCatalog",
    "GeneratedCardCompilation",
    "GeneratedCardCreateExecution",
    "GeneratedCardCreateInput",
    "GeneratedCardProgram",
    "GeneratedCardVersion",
    "GeneratedHandMoveExecution",
    "GeneratedHandMoveProgram",
    "GeneratedHandMoveSettlement",
    "GeneratedHandMoveTurnState",
    "GeneratedStatusUidInput",
    "PLAY_COUNT_BUFF",
    "PLAY_COUNT_BUFF_EFFECT_ID",
    "Plan2NativeGeneratedCardError",
    "compile_generated_cards",
    "compile_plan2_native_catalog_generated_cards",
    "execute_plan2_generated_buff_force_chain",
    "execute_plan2_generated_card_create",
    "execute_plan2_generated_hand_move_aggressive",
    "execute_plan2_generated_hand_move_block",
    "install_plan2_generated_play_count_buff",
    "load_plan2_native_catalog_generated_cards",
    "start_plan2_generated_hand_move_turn",
]
