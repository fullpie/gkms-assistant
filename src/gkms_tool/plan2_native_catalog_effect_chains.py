"""Bounded native owner for direct Plan2/Common ``ExamEffectTimer`` chains.

The current Master slice contains 104 direct Timer occurrences across 96 card
versions and 19 Timer IDs.  Every parent owns one child.  This leaf compiles
those rows without importing the native program catalog, horizon, core, or
formal coverage, then exposes an immutable relative-StartTurn scheduler.

Child commands remain in installed queue order.  CardDraw and Hand-All
CardUpgrade execute against the exact ordered-zone model, scalar score/Block
children use audited native arithmetic, and the one CardMove child has a
typed handoff to its existing exact GUID executor.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3
from typing import Final, Literal, TypeAlias

from .audition_native_ordered_zones import (
    NativeOrderedCardInstance,
    NativeOrderedZoneError,
    NativeOrderedZoneState,
    _runtime_state_digest,
)
from .logic_engine import PLAN_COMMON, PLAN_LOGIC, load_master_effect
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    AddingParameterAdditionalData,
    AddingParameterSettings,
    AddingParameterStatus,
    AddBlockSettings,
    AddBlockStatus,
    INT32_MAX,
    INT32_MIN,
    ParameterApplication,
    ParameterApplicationStatus,
    apply_parameter_add,
    calculate_add_block,
    calculate_adding_parameter,
    get_ratio_effect_int_value,
)
from .plan2_card_move_remaining import (
    RemainingCardMoveExecutionContext,
    RemainingCardMoveHandoff,
    RemainingCardMoveResult,
    RemainingCardMoveState,
    simulate_remaining_card_move,
)
from .plan2_card_upgrade import (
    Plan2CardUpgradeContract,
    parse_plan2_card_upgrade_master_row,
)
from .plan2_end_turn_trigger import _lesson_depend_block_raw_score
from .plan2_exam_effect_timer import (
    Plan2ExamEffectTimerContract,
    load_plan2_exam_effect_timer_catalog,
)
from .plan2_timer_block_chains import (
    Plan2TimerBlockCatalog,
    load_plan2_timer_block_catalog,
)
from .plan2_timer_card_move import (
    CHILD_EFFECT_ID as TIMER_CARD_MOVE_CHILD_ID,
    TIMER_EFFECT_ID as TIMER_CARD_MOVE_PARENT_ID,
    load_plan2_timer_card_move_contract,
)
from .plan2_timer_lesson_depend_review import (
    Plan2TimerReviewCatalog,
    load_plan2_timer_review_catalog,
)


PLAN2_NATIVE_EFFECT_CHAIN_SCHEMA_VERSION: Final = 1
EXPECTED_OCCURRENCE_COUNT: Final = 104
EXPECTED_VERSION_COUNT: Final = 96
EXPECTED_TIMER_EFFECT_COUNT: Final = 19
EXPECTED_CHILD_EFFECT_COUNT: Final = 17
EXPECTED_COMPANION_BLOCKER_COUNT: Final = 69
EXPECTED_WHOLE_CARD_DIRECT_COUNT: Final = 43
EXPECTED_WHOLE_CARD_CO_BLOCKED_COUNT: Final = 53

EFFECT_TIMER: Final = "ProduceExamEffectType_ExamEffectTimer"
CHILD_CARD_DRAW: Final = "ProduceExamEffectType_ExamCardDraw"
CHILD_CARD_UPGRADE: Final = "ProduceExamEffectType_ExamCardUpgrade"
CHILD_BLOCK: Final = "ProduceExamEffectType_ExamBlock"
CHILD_LESSON_BLOCK: Final = "ProduceExamEffectType_ExamLessonDependBlock"
CHILD_LESSON_REVIEW: Final = (
    "ProduceExamEffectType_ExamLessonDependExamReview"
)
CHILD_LESSON_MULTIPLE: Final = (
    "ProduceExamEffectType_ExamLessonValueMultiple"
)
CHILD_CARD_MOVE: Final = "ProduceExamEffectType_ExamCardMove"
SUPPORTED_CHILD_TYPES = frozenset(
    {
        CHILD_CARD_DRAW,
        CHILD_CARD_UPGRADE,
        CHILD_BLOCK,
        CHILD_LESSON_BLOCK,
        CHILD_LESSON_REVIEW,
        CHILD_LESSON_MULTIPLE,
        CHILD_CARD_MOVE,
    }
)
EXPECTED_CHILD_TYPE_COUNTS = {
    CHILD_CARD_DRAW: 56,
    CHILD_CARD_UPGRADE: 27,
    CHILD_BLOCK: 12,
    CHILD_LESSON_BLOCK: 4,
    CHILD_LESSON_REVIEW: 4,
    CHILD_CARD_MOVE: 1,
}

EXECUTOR_DRAW: Final = "ordered-zone-draw"
EXECUTOR_UPGRADE: Final = "ordered-zone-hand-all-upgrade"
EXECUTOR_BLOCK: Final = "native-block"
EXECUTOR_LESSON_BLOCK: Final = "native-lesson-depend-block"
EXECUTOR_LESSON_REVIEW: Final = "native-lesson-depend-review"
EXECUTOR_LESSON_MULTIPLE: Final = "native-lesson-multiple-install"
EXECUTOR_CARD_MOVE: Final = "timer-card-move-exact-handoff"
ChildExecutor: TypeAlias = Literal[
    "ordered-zone-draw",
    "ordered-zone-hand-all-upgrade",
    "native-block",
    "native-lesson-depend-block",
    "native-lesson-depend-review",
    "native-lesson-multiple-install",
    "timer-card-move-exact-handoff",
]
EXECUTOR_BY_TYPE: Mapping[str, ChildExecutor] = {
    CHILD_CARD_DRAW: EXECUTOR_DRAW,
    CHILD_CARD_UPGRADE: EXECUTOR_UPGRADE,
    CHILD_BLOCK: EXECUTOR_BLOCK,
    CHILD_LESSON_BLOCK: EXECUTOR_LESSON_BLOCK,
    CHILD_LESSON_REVIEW: EXECUTOR_LESSON_REVIEW,
    CHILD_LESSON_MULTIPLE: EXECUTOR_LESSON_MULTIPLE,
    CHILD_CARD_MOVE: EXECUTOR_CARD_MOVE,
}

PLAY_ORIGINS = frozenset({"normal", "forced", "extra"})
INSTALL_PHASE: Final = "Playing/direct-effect-slot"
START_TURN_PHASES: Final = (
    "ExamStartTurn",
    "StartPlay",
    "ExamTurnTimer",
    "RemoveTurnTimerTriggeredStatus",
)

_UNSUPPORTED_SIBLING_TYPES = frozenset(
    {
        "ProduceExamEffectType_ExamAntiDebuff",
        "ProduceExamEffectType_ExamCardCreateSearch",
        "ProduceExamEffectType_ExamCardCreateId",
        "ProduceExamEffectType_ExamAggressiveValueMultiple",
        "ProduceExamEffectType_ExamDebuffRecover",
        "ProduceExamEffectType_ExamAggressiveAdditiveFix",
        "ProduceExamEffectType_ExamStatusEnchantEncore",
    }
)


class Plan2NativeEffectChainError(ValueError):
    """Stable compiler/runtime error for an unproven chain boundary."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


def _i32(value: object, label: str) -> int:
    if type(value) is not int:
        raise Plan2NativeEffectChainError("invalid-int32", label)
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2NativeEffectChainError("int32-out-of-range", label)
    return value


def _nonnegative(value: object, label: str) -> int:
    result = _i32(value, label)
    if result < 0:
        raise Plan2NativeEffectChainError("negative-value", label)
    return result


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value):
        raise Plan2NativeEffectChainError("invalid-text", label)
    return value


def _json_object(value: object, label: str) -> Mapping[str, object]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2NativeEffectChainError("invalid-json", label) from error
    if not isinstance(parsed, Mapping):
        raise Plan2NativeEffectChainError("invalid-json-object", label)
    return parsed


def _json_array(value: object, label: str) -> tuple[object, ...]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2NativeEffectChainError("invalid-json", label) from error
    if not isinstance(parsed, list):
        raise Plan2NativeEffectChainError("invalid-json-array", label)
    return tuple(parsed)


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeEffectChainBlocker:
    code: str
    card_id: str
    upgrade: int
    detail: str = ""
    slot_index: int = -1

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade


@dataclass(frozen=True, slots=True)
class Plan2NativeEffectChainProgram:
    card_id: str
    upgrade: int
    slot_index: int
    ordered_effect_ids: tuple[str, ...]
    timer_effect_id: str
    delay: int
    effect_count: int
    effect_turn: int
    child_effect_id: str
    child_effect_type: str
    child_value1: int
    child_value2: int
    child_count: int
    child_turn: int
    child_executor: ChildExecutor | None
    child_blocker: str = ""

    def __post_init__(self) -> None:
        _text(self.card_id, "program.card_id")
        _nonnegative(self.upgrade, "program.upgrade")
        _nonnegative(self.slot_index, "program.slot_index")
        if type(self.ordered_effect_ids) is not tuple or self.slot_index >= len(
            self.ordered_effect_ids
        ):
            raise Plan2NativeEffectChainError("effect-order-shape")
        if self.ordered_effect_ids[self.slot_index] != self.timer_effect_id:
            raise Plan2NativeEffectChainError("effect-order-drift", self.timer_effect_id)
        for name in (
            "delay",
            "effect_count",
            "effect_turn",
            "child_value1",
            "child_value2",
            "child_count",
            "child_turn",
        ):
            _i32(getattr(self, name), f"program.{name}")
        if self.child_effect_type not in SUPPORTED_CHILD_TYPES:
            if self.child_executor is not None or not self.child_blocker:
                raise Plan2NativeEffectChainError("child-executor-accounting-drift")
        elif self.child_executor != EXECUTOR_BY_TYPE[self.child_effect_type]:
            raise Plan2NativeEffectChainError("child-executor-drift", self.child_effect_id)

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def prior_effect_ids(self) -> tuple[str, ...]:
        return self.ordered_effect_ids[: self.slot_index]

    @property
    def executable(self) -> bool:
        return self.child_executor is not None and not self.child_blocker


@dataclass(frozen=True, slots=True)
class Plan2NativeEffectChainVersion:
    card_id: str
    upgrade: int
    ordered_effect_ids: tuple[str, ...]
    timers: tuple[Plan2NativeEffectChainProgram, ...]
    companion_blockers: tuple[Plan2NativeEffectChainBlocker, ...]

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def whole_card_executable(self) -> bool:
        return all(item.executable for item in self.timers) and not self.companion_blockers


@dataclass(frozen=True, slots=True)
class Plan2NativeEffectChainCatalog:
    versions: tuple[Plan2NativeEffectChainVersion, ...]

    @property
    def programs(self) -> tuple[Plan2NativeEffectChainProgram, ...]:
        return tuple(program for version in self.versions for program in version.timers)

    def version(self, card_id: str, upgrade: int) -> Plan2NativeEffectChainVersion:
        matches = tuple(item for item in self.versions if item.ref == (card_id, upgrade))
        if len(matches) != 1:
            raise KeyError(f"effect-chain version resolves {len(matches)} times")
        return matches[0]

    def timer(
        self, card_id: str, upgrade: int, timer_effect_id: str | None = None
    ) -> Plan2NativeEffectChainProgram:
        version = self.version(card_id, upgrade)
        matches = version.timers
        if timer_effect_id is not None:
            matches = tuple(x for x in matches if x.timer_effect_id == timer_effect_id)
        if len(matches) != 1:
            raise KeyError(f"effect-chain timer resolves {len(matches)} times")
        return matches[0]


@dataclass(frozen=True, slots=True)
class Plan2NativeEffectChainCompilation:
    schema_version: int
    database: str
    affected_refs: tuple[tuple[str, int], ...]
    occurrence_count: int
    timer_effect_count: int
    child_effect_count: int
    child_type_counts: tuple[tuple[str, int], ...]
    catalog: Plan2NativeEffectChainCatalog
    blockers: tuple[Plan2NativeEffectChainBlocker, ...]

    @property
    def fully_compiled(self) -> bool:
        return not self.blockers

    @property
    def executable_occurrence_count(self) -> int:
        return sum(item.executable for item in self.catalog.programs)

    @property
    def child_executor_blockers(self) -> tuple[Plan2NativeEffectChainProgram, ...]:
        return tuple(item for item in self.catalog.programs if not item.executable)

    @property
    def companion_blockers(self) -> tuple[Plan2NativeEffectChainBlocker, ...]:
        return tuple(
            blocker for version in self.catalog.versions for blocker in version.companion_blockers
        )

    @property
    def companion_blocker_code_counts(self) -> Mapping[str, int]:
        return dict(Counter(item.code for item in self.companion_blockers))

    @property
    def whole_card_direct_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(item.ref for item in self.catalog.versions if item.whole_card_executable)

    @property
    def whole_card_co_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(item.ref for item in self.catalog.versions if not item.whole_card_executable)


def _chain_ids(row: Mapping[str, object]) -> tuple[str, ...]:
    raw = _json_object(row["raw_json"], str(row["id"]))
    listed = raw.get("chainProduceExamEffectIds", [])
    if not isinstance(listed, list) or any(type(item) is not str or not item for item in listed):
        raise Plan2NativeEffectChainError("chain-list-shape", str(row["id"]))
    return tuple(
        item for item in (str(row["chain_effect_id"]), *listed) if item
    )


def _validate_parent_child_raw(
    parent: Mapping[str, object], child: Mapping[str, object]
) -> None:
    parent_id = str(parent["id"])
    child_id = str(child["id"])
    raw = _json_object(parent["raw_json"], parent_id)
    expected = {
        "id": parent_id,
        "effectType": EFFECT_TIMER,
        "effectValue1": parent["value1"],
        "effectValue2": 0,
        "effectCount": 1,
        "effectTurn": 0,
        "chainProduceExamEffectId": child_id,
        "chainProduceExamEffectIds": [],
        "produceExamStatusEnchantId": "",
    }
    for key, value in expected.items():
        if raw.get(key) != value:
            raise Plan2NativeEffectChainError("parent-master-raw-drift", f"{parent_id}.{key}")
    groups = raw.get("effectGroupIds")
    if (
        not isinstance(groups, list)
        or "effect_group-visible-exam_effect_timer-000" not in groups
        or any(type(item) is not str or not item for item in groups)
    ):
        raise Plan2NativeEffectChainError(
            "parent-master-raw-drift", f"{parent_id}.effectGroupIds"
        )
    child_raw = _json_object(child["raw_json"], child_id)
    for key, value in {
        "id": child_id,
        "effectType": child["effect_type"],
        "effectValue1": child["value1"],
        "effectValue2": child["value2"],
        "effectCount": child["effect_count"],
        "effectTurn": child["effect_turn"],
        "chainProduceExamEffectId": "",
        "chainProduceExamEffectIds": [],
        "produceExamStatusEnchantId": "",
    }.items():
        if child_raw.get(key) != value:
            raise Plan2NativeEffectChainError("child-master-raw-drift", f"{child_id}.{key}")


def _companion_blockers(
    card: Mapping[str, object],
    links: tuple[Mapping[str, object], ...],
    target_slots: frozenset[int],
    effects: Mapping[str, Mapping[str, object]],
) -> tuple[Plan2NativeEffectChainBlocker, ...]:
    card_id = str(card["id"])
    upgrade = int(card["upgrade_count"])
    result: list[Plan2NativeEffectChainBlocker] = []
    card_trigger = str(card["play_trigger_id"])
    if card_trigger:
        result.append(
            Plan2NativeEffectChainBlocker(
                "card-trigger-runtime-unbound", card_id, upgrade, card_trigger
            )
        )
    for index, link in enumerate(links):
        if index in target_slots:
            continue
        effect_id = str(link.get("produceExamEffectId", ""))
        trigger_id = str(link.get("produceExamTriggerId", ""))
        if trigger_id:
            result.append(
                Plan2NativeEffectChainBlocker(
                    "effect-trigger-runtime-unbound",
                    card_id,
                    upgrade,
                    f"{effect_id}:{trigger_id}",
                    index,
                )
            )
            continue
        effect = effects.get(effect_id)
        if effect is not None and str(effect["effect_type"]) in _UNSUPPORTED_SIBLING_TYPES:
            result.append(
                Plan2NativeEffectChainBlocker(
                    "effect-runtime-unbound",
                    card_id,
                    upgrade,
                    f"{effect['effect_type']}:{effect_id}",
                    index,
                )
            )
    return tuple(result)


def _exact_helper_indexes(database: Path) -> tuple[
    Mapping[str, Plan2ExamEffectTimerContract],
    frozenset[tuple[str, int, str]],
    frozenset[tuple[str, int, str]],
]:
    generic = {
        item.effect_id: item
        for item in load_plan2_exam_effect_timer_catalog(database=database)
    }
    block_catalog: Plan2TimerBlockCatalog = load_plan2_timer_block_catalog(
        database=database
    )
    block_refs = frozenset(
        (version.card.id, version.card.upgrade, version.timer.effect_id)
        for version in block_catalog.versions
    )
    review_catalog: Plan2TimerReviewCatalog = load_plan2_timer_review_catalog(
        database=database
    )
    review_refs = frozenset(
        (version.card.id, version.card.upgrade, timer.effect_id)
        for version in review_catalog.versions
        for timer in version.timers
    )
    # Strictly load the one CardMove parent/card/search/child contract.
    load_plan2_timer_card_move_contract(database=database)
    return generic, block_refs, review_refs


def compile_plan2_native_catalog_effect_chains(
    *, database: str | Path = DEFAULT_DATABASE
) -> Plan2NativeEffectChainCompilation:
    """Compile all current direct Timer chains and whole-card companions."""

    database_path = Path(database).resolve()
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    with closing(
        sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        cards = tuple(
            dict(row)
            for row in connection.execute(
                "SELECT id, upgrade_count, plan_type, play_trigger_id, play_effects_json "
                "FROM card WHERE plan_type IN (?, ?) ORDER BY id, upgrade_count",
                (PLAN_COMMON, PLAN_LOGIC),
            )
        )
        effects = {
            str(row["id"]): dict(row)
            for row in connection.execute("SELECT * FROM effect")
        }

    candidates: list[
        tuple[Mapping[str, object], tuple[Mapping[str, object], ...], int, Mapping[str, object], Mapping[str, object]]
    ] = []
    by_version: dict[tuple[str, int], list[int]] = {}
    child_type_counts: Counter[str] = Counter()
    for card in cards:
        links_raw = _json_array(
            card["play_effects_json"], f"{card['id']}#{card['upgrade_count']}"
        )
        links: list[Mapping[str, object]] = []
        for index, value in enumerate(links_raw):
            if not isinstance(value, Mapping):
                raise Plan2NativeEffectChainError("card-link-shape", f"{card['id']}@{index}")
            links.append(value)
        normalized = tuple(links)
        for index, link in enumerate(normalized):
            if link.get("produceExamTriggerId") != "":
                continue
            parent = effects.get(str(link.get("produceExamEffectId", "")))
            if parent is None or parent["effect_type"] != EFFECT_TIMER:
                continue
            chain_ids = _chain_ids(parent)
            if not chain_ids:
                continue
            if len(chain_ids) != 1:
                raise Plan2NativeEffectChainError("multi-child-chain-unbound", str(parent["id"]))
            child = effects.get(chain_ids[0])
            if child is None:
                raise Plan2NativeEffectChainError("chain-child-missing", chain_ids[0])
            candidates.append((card, normalized, index, parent, child))
            ref = (str(card["id"]), int(card["upgrade_count"]))
            by_version.setdefault(ref, []).append(index)
            child_type_counts[str(child["effect_type"])] += 1

    refs = tuple(sorted(by_version))
    timer_ids = {str(item[3]["id"]) for item in candidates}
    child_ids = {str(item[4]["id"]) for item in candidates}
    if (
        len(candidates) != EXPECTED_OCCURRENCE_COUNT
        or len(refs) != EXPECTED_VERSION_COUNT
        or len(timer_ids) != EXPECTED_TIMER_EFFECT_COUNT
        or len(child_ids) != EXPECTED_CHILD_EFFECT_COUNT
        or dict(child_type_counts) != EXPECTED_CHILD_TYPE_COUNTS
    ):
        raise Plan2NativeEffectChainError(
            "target-slice-cardinality-drift",
            f"occ={len(candidates)},versions={len(refs)},timers={len(timer_ids)},children={len(child_ids)},types={dict(child_type_counts)}",
        )

    generic_helpers, block_refs, review_refs = _exact_helper_indexes(database_path)
    programs_by_ref: dict[tuple[str, int], list[Plan2NativeEffectChainProgram]] = {
        ref: [] for ref in refs
    }
    compile_blockers: list[Plan2NativeEffectChainBlocker] = []
    for card, links, index, parent, child in candidates:
        card_id = str(card["id"])
        upgrade = int(card["upgrade_count"])
        parent_id = str(parent["id"])
        child_id = str(child["id"])
        child_type = str(child["effect_type"])
        try:
            _validate_parent_child_raw(parent, child)
            helper_pinned = False
            if parent_id in generic_helpers:
                helper = generic_helpers[parent_id]
                helper_pinned = (
                    helper.child_effect_id == child_id
                    and helper.child_effect_type == child_type
                    and helper.delay == parent["value1"]
                )
            elif (card_id, upgrade, parent_id) in block_refs:
                helper_pinned = child_type in {CHILD_BLOCK, CHILD_LESSON_BLOCK}
            elif (card_id, upgrade, parent_id) in review_refs:
                helper_pinned = child_type == CHILD_LESSON_REVIEW
            elif parent_id == TIMER_CARD_MOVE_PARENT_ID:
                helper_pinned = child_id == TIMER_CARD_MOVE_CHILD_ID
            if not helper_pinned:
                raise Plan2NativeEffectChainError("exact-helper-missing", parent_id)
            if child_type == CHILD_CARD_UPGRADE:
                contract: Plan2CardUpgradeContract = parse_plan2_card_upgrade_master_row(
                    child_id, database=database_path, plan_type=PLAN_LOGIC
                )
                if contract.effect_id != child_id:
                    raise Plan2NativeEffectChainError("upgrade-contract-drift", child_id)
            elif child_type not in {CHILD_CARD_MOVE}:
                exact_child = load_master_effect(child_id, database_path)
                if (
                    exact_child.effect_type,
                    exact_child.value1,
                    exact_child.value2,
                    exact_child.effect_count,
                    exact_child.effect_turn,
                ) != (
                    child_type,
                    child["value1"],
                    child["value2"],
                    child["effect_count"],
                    child["effect_turn"],
                ):
                    raise Plan2NativeEffectChainError("child-helper-row-drift", child_id)
            executor = EXECUTOR_BY_TYPE.get(child_type)
            program = Plan2NativeEffectChainProgram(
                card_id,
                upgrade,
                index,
                tuple(str(link["produceExamEffectId"]) for link in links),
                parent_id,
                _i32(parent["value1"], f"{parent_id}.delay"),
                _i32(parent["effect_count"], f"{parent_id}.count"),
                _i32(parent["effect_turn"], f"{parent_id}.turn"),
                child_id,
                child_type,
                _i32(child["value1"], f"{child_id}.value1"),
                _i32(child["value2"], f"{child_id}.value2"),
                _i32(child["effect_count"], f"{child_id}.count"),
                _i32(child["effect_turn"], f"{child_id}.turn"),
                executor,
                "" if executor is not None else "child-executor-runtime-unbound",
            )
            programs_by_ref[(card_id, upgrade)].append(program)
        except (KeyError, sqlite3.Error, TypeError, ValueError) as error:
            compile_blockers.append(
                Plan2NativeEffectChainBlocker(
                    getattr(error, "code", "effect-chain-compile-failed"),
                    card_id,
                    upgrade,
                    getattr(error, "detail", str(error)) or str(error),
                    index,
                )
            )

    card_by_ref = {
        (str(card["id"]), int(card["upgrade_count"])): card for card in cards
    }
    candidate_links = {
        (str(card["id"]), int(card["upgrade_count"])): links
        for card, links, _index, _parent, _child in candidates
    }
    versions: list[Plan2NativeEffectChainVersion] = []
    for ref in refs:
        card = card_by_ref[ref]
        links = candidate_links[ref]
        timers = tuple(sorted(programs_by_ref[ref], key=lambda item: item.slot_index))
        versions.append(
            Plan2NativeEffectChainVersion(
                ref[0],
                ref[1],
                tuple(str(link["produceExamEffectId"]) for link in links),
                timers,
                _companion_blockers(
                    card,
                    links,
                    frozenset(by_version[ref]),
                    effects,
                ),
            )
        )
    compilation = Plan2NativeEffectChainCompilation(
        PLAN2_NATIVE_EFFECT_CHAIN_SCHEMA_VERSION,
        str(database_path),
        refs,
        len(candidates),
        len(timer_ids),
        len(child_ids),
        tuple(sorted(child_type_counts.items())),
        Plan2NativeEffectChainCatalog(tuple(versions)),
        tuple(compile_blockers),
    )
    if compilation.fully_compiled:
        if compilation.executable_occurrence_count != EXPECTED_OCCURRENCE_COUNT:
            raise Plan2NativeEffectChainError("executor-accounting-drift")
        if len(compilation.companion_blockers) != EXPECTED_COMPANION_BLOCKER_COUNT:
            raise Plan2NativeEffectChainError("companion-accounting-drift")
        if len(compilation.whole_card_direct_refs) != EXPECTED_WHOLE_CARD_DIRECT_COUNT:
            raise Plan2NativeEffectChainError("whole-card-direct-accounting-drift")
        if len(compilation.whole_card_co_blocked_refs) != EXPECTED_WHOLE_CARD_CO_BLOCKED_COUNT:
            raise Plan2NativeEffectChainError("whole-card-co-blocked-accounting-drift")
    return compilation


load_plan2_native_catalog_effect_chains = compile_plan2_native_catalog_effect_chains


@dataclass(frozen=True, slots=True)
class Plan2NativeEffectChainQueuedTimer:
    status_uid: int
    source_guid: str
    play_origin: str
    install_sequence: int
    installed_start_turn_boundary: int
    program: Plan2NativeEffectChainProgram

    def __post_init__(self) -> None:
        if type(self.status_uid) is not int or self.status_uid <= 0:
            raise Plan2NativeEffectChainError("queued-status-uid-shape")
        _text(self.source_guid, "queued.source_guid")
        if self.play_origin not in PLAY_ORIGINS:
            raise Plan2NativeEffectChainError("play-origin-unbound", self.play_origin)
        if type(self.install_sequence) is not int or self.install_sequence <= 0:
            raise Plan2NativeEffectChainError("queued-install-sequence-shape")
        _nonnegative(
            self.installed_start_turn_boundary,
            "queued.installed_start_turn_boundary",
        )
        if not isinstance(self.program, Plan2NativeEffectChainProgram):
            raise TypeError("queued program has unsupported shape")

    @property
    def instance_id(self) -> str:
        return f"{self.program.timer_effect_id}|{self.source_guid}|{self.install_sequence}"

    @property
    def due_start_turn_boundary(self) -> int:
        return self.installed_start_turn_boundary + self.program.delay


@dataclass(frozen=True, slots=True)
class Plan2NativeEffectChainRuntime:
    queue: tuple[Plan2NativeEffectChainQueuedTimer, ...] = ()
    completed_start_turn_boundaries: int = 0
    next_status_uid: int = 1
    next_install_sequence: int = 1

    def __post_init__(self) -> None:
        _nonnegative(self.completed_start_turn_boundaries, "runtime.boundaries")
        if (
            type(self.next_status_uid) is not int
            or type(self.next_install_sequence) is not int
            or self.next_status_uid <= 0
            or self.next_install_sequence <= 0
        ):
            raise Plan2NativeEffectChainError("runtime-counter-shape")
        if type(self.queue) is not tuple or any(
            not isinstance(item, Plan2NativeEffectChainQueuedTimer) for item in self.queue
        ):
            raise Plan2NativeEffectChainError("runtime-queue-shape")
        uids = tuple(item.status_uid for item in self.queue)
        sequences = tuple(item.install_sequence for item in self.queue)
        if (
            len(uids) != len(set(uids))
            or len(sequences) != len(set(sequences))
            or sequences != tuple(sorted(sequences))
            or any(uid >= self.next_status_uid for uid in uids)
            or any(seq >= self.next_install_sequence for seq in sequences)
        ):
            raise Plan2NativeEffectChainError("runtime-queue-identity-drift")


@dataclass(frozen=True, slots=True)
class Plan2NativeEffectChainInstallInput:
    source_guid: str
    play_origin: str = "normal"
    completed_effect_ids: tuple[str, ...] = ()
    accepted: bool | None = True
    install_phase_known: bool | None = True

    def __post_init__(self) -> None:
        _text(self.source_guid, "install.source_guid")
        if self.play_origin not in PLAY_ORIGINS:
            raise Plan2NativeEffectChainError("play-origin-unbound", self.play_origin)
        if type(self.completed_effect_ids) is not tuple:
            raise Plan2NativeEffectChainError("completed-order-shape")
        for name in ("accepted", "install_phase_known"):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise Plan2NativeEffectChainError("snapshot-shape", name)


@dataclass(frozen=True, slots=True)
class Plan2NativeEffectChainInstallTransition:
    before: Plan2NativeEffectChainRuntime
    after: Plan2NativeEffectChainRuntime
    program: Plan2NativeEffectChainProgram
    execution_input: Plan2NativeEffectChainInstallInput
    executable: bool
    installed: Plan2NativeEffectChainQueuedTimer | None
    unresolved: tuple[str, ...]
    trace: tuple[str, ...]


def install_plan2_native_effect_chain(
    runtime: Plan2NativeEffectChainRuntime,
    program: Plan2NativeEffectChainProgram,
    execution_input: Plan2NativeEffectChainInstallInput,
) -> Plan2NativeEffectChainInstallTransition:
    """Install one one-shot relative Timer at its ordered Playing slot."""

    unresolved: list[str] = []
    if not program.executable:
        unresolved.append(program.child_blocker or "child-executor-runtime-unbound")
    if execution_input.completed_effect_ids != program.prior_effect_ids:
        unresolved.append("effect-order-unresolved")
    if execution_input.accepted is None:
        unresolved.append("accepted-unknown")
    if execution_input.install_phase_known is None:
        unresolved.append("install-phase-unknown")
    if unresolved:
        return Plan2NativeEffectChainInstallTransition(
            runtime, runtime, program, execution_input, False, None, tuple(unresolved), ()
        )
    if not execution_input.accepted:
        return Plan2NativeEffectChainInstallTransition(
            runtime, runtime, program, execution_input, False, None, ("card-not-accepted",), ()
        )
    if not execution_input.install_phase_known:
        return Plan2NativeEffectChainInstallTransition(
            runtime, runtime, program, execution_input, False, None, ("install-phase-missing",), ()
        )
    queued = Plan2NativeEffectChainQueuedTimer(
        runtime.next_status_uid,
        execution_input.source_guid,
        execution_input.play_origin,
        runtime.next_install_sequence,
        runtime.completed_start_turn_boundaries,
        program,
    )
    after = replace(
        runtime,
        queue=(*runtime.queue, queued),
        next_status_uid=runtime.next_status_uid + 1,
        next_install_sequence=runtime.next_install_sequence + 1,
    )
    return Plan2NativeEffectChainInstallTransition(
        runtime,
        after,
        program,
        execution_input,
        True,
        queued,
        (),
        (
            f"phase:{INSTALL_PHASE}",
            f"slot:{program.slot_index}:install-one-shot-timer",
            f"delay:{program.delay}:child:{program.child_effect_id}",
        ),
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeEffectChainChildCommand:
    queue_instance_id: str
    status_uid: int
    source_guid: str
    source_play_origin: str
    start_turn_boundary: int
    program: Plan2NativeEffectChainProgram


@dataclass(frozen=True, slots=True)
class Plan2NativeEffectChainStartTurnInput:
    phase_snapshot_known: bool | None = True
    terminal_before_start: bool | None = False

    def __post_init__(self) -> None:
        for name in ("phase_snapshot_known", "terminal_before_start"):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise Plan2NativeEffectChainError("snapshot-shape", name)


@dataclass(frozen=True, slots=True)
class Plan2NativeEffectChainStartTurnTransition:
    before: Plan2NativeEffectChainRuntime
    after: Plan2NativeEffectChainRuntime
    turn_input: Plan2NativeEffectChainStartTurnInput
    executable: bool
    due_instance_ids: tuple[str, ...]
    commands: tuple[Plan2NativeEffectChainChildCommand, ...]
    removed_status_uids: tuple[int, ...]
    stranded_instance_ids: tuple[str, ...]
    unresolved: tuple[str, ...]
    trace: tuple[str, ...]


def start_plan2_native_effect_chain_turn(
    runtime: Plan2NativeEffectChainRuntime,
    turn_input: Plan2NativeEffectChainStartTurnInput = Plan2NativeEffectChainStartTurnInput(),
) -> Plan2NativeEffectChainStartTurnTransition:
    """Advance one StartTurn boundary, queue due children, then remove parents."""

    if turn_input.phase_snapshot_known is not True:
        return Plan2NativeEffectChainStartTurnTransition(
            runtime, runtime, turn_input, False, (), (), (), (), ("start-turn-phase-unknown",), ()
        )
    if turn_input.terminal_before_start is None:
        return Plan2NativeEffectChainStartTurnTransition(
            runtime, runtime, turn_input, False, (), (), (), (), ("terminal-snapshot-unknown",), ()
        )
    if turn_input.terminal_before_start:
        return Plan2NativeEffectChainStartTurnTransition(
            runtime,
            runtime,
            turn_input,
            True,
            (),
            (),
            (),
            tuple(item.instance_id for item in runtime.queue),
            (),
            ("terminal:no-StartTurn:no-TurnTimer",),
        )
    if runtime.completed_start_turn_boundaries >= INT32_MAX:
        raise Plan2NativeEffectChainError("start-turn-counter-overflow")
    boundary = runtime.completed_start_turn_boundaries + 1
    due: list[Plan2NativeEffectChainQueuedTimer] = []
    for item in runtime.queue:
        age = boundary - item.installed_start_turn_boundary
        if age < 0:
            raise Plan2NativeEffectChainError("timer-relative-boundary-drift", item.instance_id)
        if age == item.program.delay:
            due.append(item)
        elif age > item.program.delay:
            raise Plan2NativeEffectChainError("stale-timer", item.instance_id)
    due_ids = tuple(item.instance_id for item in due)
    commands = tuple(
        Plan2NativeEffectChainChildCommand(
            item.instance_id,
            item.status_uid,
            item.source_guid,
            item.play_origin,
            boundary,
            item.program,
        )
        for item in due
    )
    due_set = frozenset(due_ids)
    after = replace(
        runtime,
        queue=tuple(item for item in runtime.queue if item.instance_id not in due_set),
        completed_start_turn_boundaries=boundary,
    )
    return Plan2NativeEffectChainStartTurnTransition(
        runtime,
        after,
        turn_input,
        True,
        due_ids,
        commands,
        tuple(item.status_uid for item in due),
        (),
        (),
        (
            "phase:ExamStartTurn",
            "phase:StartPlay",
            "phase:ExamTurnTimer:capture-active-queue-order",
            *(
                f"queue-child:{item.instance_id}:{item.program.child_effect_id}"
                for item in due
            ),
            "phase:RemoveTurnTimerTriggeredStatus:after-child-queue",
        ),
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeEffectChainExecutionState:
    zones: NativeOrderedZoneState | None = None
    hand_limit: int = 5
    master_card_refs: tuple[tuple[str, int], ...] = ()
    block: int = 0
    review: int = 0
    application_status: ParameterApplicationStatus = ParameterApplicationStatus(0)

    def __post_init__(self) -> None:
        if self.zones is not None and not isinstance(self.zones, NativeOrderedZoneState):
            raise TypeError("zones must be NativeOrderedZoneState or None")
        _nonnegative(self.hand_limit, "execution.hand_limit")
        _nonnegative(self.block, "execution.block")
        _i32(self.review, "execution.review")
        if not isinstance(self.application_status, ParameterApplicationStatus):
            raise TypeError("application_status has unsupported shape")
        refs = tuple(self.master_card_refs)
        if refs != tuple(sorted(set(refs))):
            raise Plan2NativeEffectChainError("master-card-refs-shape")


@dataclass(frozen=True, slots=True)
class Plan2NativeEffectChainChildInput:
    snapshot_known: bool | None = True
    adding_status: AddingParameterStatus = AddingParameterStatus()
    adding_settings: AddingParameterSettings = AddingParameterSettings()
    additional: AddingParameterAdditionalData | None = None
    is_buff_active: bool = True
    add_block_status: AddBlockStatus = AddBlockStatus()
    add_block_settings: AddBlockSettings = AddBlockSettings()

    def __post_init__(self) -> None:
        if self.snapshot_known is not None and type(self.snapshot_known) is not bool:
            raise Plan2NativeEffectChainError("snapshot-shape", "child")
        if not isinstance(self.adding_status, AddingParameterStatus):
            raise TypeError("adding_status has unsupported shape")
        if not isinstance(self.adding_settings, AddingParameterSettings):
            raise TypeError("adding_settings has unsupported shape")
        if self.additional is not None and not isinstance(
            self.additional, AddingParameterAdditionalData
        ):
            raise TypeError("additional has unsupported shape")
        if type(self.is_buff_active) is not bool:
            raise Plan2NativeEffectChainError("buff-flag-shape")
        if not isinstance(self.add_block_status, AddBlockStatus):
            raise TypeError("add_block_status has unsupported shape")
        if not isinstance(self.add_block_settings, AddBlockSettings):
            raise TypeError("add_block_settings has unsupported shape")


@dataclass(frozen=True, slots=True)
class Plan2NativeEffectChainChildExecution:
    before: Plan2NativeEffectChainExecutionState
    after: Plan2NativeEffectChainExecutionState
    command: Plan2NativeEffectChainChildCommand
    executable: bool
    drawn_guids: tuple[str, ...] = ()
    upgraded_guids: tuple[str, ...] = ()
    ratio_snapshot: int | None = None
    ratio_value: int | None = None
    calculated_value: int | None = None
    application: ParameterApplication | None = None
    random_state_before: int | None = None
    random_state_after: int | None = None
    unresolved: tuple[str, ...] = ()
    trace: tuple[str, ...] = ()


def _replace_zone_cards(
    state: NativeOrderedZoneState,
    updates: Mapping[str, NativeOrderedCardInstance],
) -> NativeOrderedZoneState:
    universe = tuple(updates.get(card.guid, card) for card in state.card_universe)
    return replace(
        state,
        card_universe=universe,
        hand=tuple(updates.get(card.guid, card) for card in state.hand),
    )


def _temporary_upgrade_one(card: NativeOrderedCardInstance) -> NativeOrderedCardInstance:
    target = card.effective_upgrade + 1
    if target > 3:
        raise NativeOrderedZoneError("native-upgrade-cap-unresolved", card.guid)
    digest = _runtime_state_digest(
        base_upgrade=card.base_upgrade,
        temporary_upgrade=1,
        effective_upgrade=target,
        support_upgrade_ids=card.support_upgrade_ids,
        fixed_deck_order=card.fixed_deck_order,
        runtime_state=card.runtime_state,
    )
    return replace(
        card,
        temporary_upgrade=1,
        effective_upgrade=target,
        runtime_state_digest=digest,
    )


def upgrade_plan2_native_hand_all_zones(
    zones: NativeOrderedZoneState,
    master_card_refs: Sequence[tuple[str, int]],
) -> tuple[NativeOrderedZoneState, tuple[str, ...]]:
    """Apply the shared exact Hand-All temporary-upgrade primitive."""

    if not isinstance(zones, NativeOrderedZoneState):
        raise TypeError("zones must be NativeOrderedZoneState")
    refs = set(master_card_refs)
    updates: dict[str, NativeOrderedCardInstance] = {}
    for card in zones.hand:
        if (card.card_id, card.effective_upgrade) not in refs:
            raise NativeOrderedZoneError(
                "card-master-missing", f"{card.card_id}@{card.effective_upgrade}"
            )
        if card.base_upgrade + card.temporary_upgrade != 0:
            continue
        if (card.card_id, 1) not in refs:
            continue
        updates[card.guid] = _temporary_upgrade_one(card)
    after = zones if not updates else _replace_zone_cards(zones, updates)
    return after, tuple(updates)


def _next_application(
    status: ParameterApplicationStatus, application: ParameterApplication
) -> ParameterApplicationStatus:
    return replace(
        status,
        judge_parameter=application.after,
        current_turn_total_add_parameter=application.current_turn_total_add_parameter,
        judge_parameter_vocal=application.judge_parameter_vocal,
        judge_parameter_dance=application.judge_parameter_dance,
        judge_parameter_visual=application.judge_parameter_visual,
    )


def prepare_plan2_native_effect_chain_start_turn_execution(
    state: Plan2NativeEffectChainExecutionState,
) -> Plan2NativeEffectChainExecutionState:
    """Reset the native per-turn score accumulator before queued children."""

    return replace(
        state,
        application_status=replace(
            state.application_status,
            current_turn_total_add_parameter=0,
        ),
    )


def execute_plan2_native_effect_chain_child(
    state: Plan2NativeEffectChainExecutionState,
    command: Plan2NativeEffectChainChildCommand,
    child_input: Plan2NativeEffectChainChildInput = Plan2NativeEffectChainChildInput(),
) -> Plan2NativeEffectChainChildExecution:
    """Execute one non-CardMove child command without crossing state models."""

    program = command.program
    if child_input.snapshot_known is not True:
        return Plan2NativeEffectChainChildExecution(
            state, state, command, False, unresolved=("execution-snapshot-unknown",)
        )
    if program.child_executor == EXECUTOR_CARD_MOVE:
        return Plan2NativeEffectChainChildExecution(
            state, state, command, False, unresolved=("card-move-exact-handoff-required",)
        )
    if program.child_executor in {EXECUTOR_DRAW, EXECUTOR_UPGRADE} and state.zones is None:
        return Plan2NativeEffectChainChildExecution(
            state, state, command, False, unresolved=("ordered-zone-snapshot-unknown",)
        )
    if program.child_executor == EXECUTOR_DRAW:
        assert state.zones is not None
        capacity = max(state.hand_limit - len(state.zones.hand), 0)
        available = len(state.zones.deck) + len(state.zones.grave)
        actual = min(program.child_value1, capacity, available)
        if actual == 0:
            return Plan2NativeEffectChainChildExecution(
                state,
                state,
                command,
                True,
                random_state_before=state.zones.random_state,
                random_state_after=state.zones.random_state,
                trace=("CardDraw:capacity-clamped:0",),
            )
        draw = state.zones.draw_to_hand(actual)
        after = replace(state, zones=draw.state)
        return Plan2NativeEffectChainChildExecution(
            state,
            after,
            command,
            True,
            drawn_guids=draw.drawn_guids,
            random_state_before=draw.random_state_before,
            random_state_after=draw.state.random_state,
            trace=(
                f"CardDraw:actual={actual}",
                f"CardDraw:Deck-prefix-then-Grave-recycle={draw.recycled_grave_count}",
                "CardDraw:GUID-order-preserved",
            ),
        )
    if program.child_executor == EXECUTOR_UPGRADE:
        assert state.zones is not None
        if not state.master_card_refs:
            return Plan2NativeEffectChainChildExecution(
                state, state, command, False, unresolved=("master-card-lineage-unknown",)
            )
        zones, upgraded_guids = upgrade_plan2_native_hand_all_zones(
            state.zones,
            state.master_card_refs,
        )
        return Plan2NativeEffectChainChildExecution(
            state,
            replace(state, zones=zones),
            command,
            True,
            upgraded_guids=upgraded_guids,
            random_state_before=state.zones.random_state,
            random_state_after=zones.random_state,
            trace=("CardUpgrade:Hand-All:GUID-order", "CardUpgrade:no-RNG"),
        )
    if program.child_executor == EXECUTOR_BLOCK:
        calculated = calculate_add_block(
            program.child_value1,
            is_buff_active=True,
            status=child_input.add_block_status,
            settings=child_input.add_block_settings,
            additional_multiple_aggressive_rate=1.0,
        )
        after_block = state.block + max(-state.block, calculated)
        if not 0 <= after_block <= INT32_MAX:
            raise Plan2NativeEffectChainError("block-result-domain")
        return Plan2NativeEffectChainChildExecution(
            state,
            replace(state, block=after_block),
            command,
            True,
            ratio_snapshot=state.block,
            calculated_value=calculated,
            trace=("Block:snapshot-at-expiry", "CalculateAddBlock", "AddBlockFix"),
        )

    if program.child_executor == EXECUTOR_LESSON_BLOCK:
        snapshot = state.block
        ratio = _lesson_depend_block_raw_score(snapshot, program.child_value1)
        adding_status = child_input.adding_status
    elif program.child_executor == EXECUTOR_LESSON_REVIEW:
        snapshot = state.review
        ratio = get_ratio_effect_int_value(
            snapshot, program.child_value1, is_ceil=True
        )
        adding_status = replace(child_input.adding_status, review=snapshot)
    else:
        return Plan2NativeEffectChainChildExecution(
            state,
            state,
            command,
            False,
            unresolved=(program.child_blocker or "child-executor-runtime-unbound",),
        )
    calculated = calculate_adding_parameter(
        ratio,
        is_buff_active=child_input.is_buff_active,
        status=adding_status,
        settings=child_input.adding_settings,
        additional=child_input.additional,
    )
    application = apply_parameter_add(calculated, status=state.application_status)
    after_status = _next_application(state.application_status, application)
    return Plan2NativeEffectChainChildExecution(
        state,
        replace(state, application_status=after_status),
        command,
        True,
        ratio_snapshot=snapshot,
        ratio_value=ratio,
        calculated_value=calculated,
        application=application,
        trace=(
            "snapshot-current-expiry-value",
            "binary32-permille-ceil",
            "CalculateAddingParameter",
            "AddParameter",
        ),
    )


def execute_plan2_native_effect_chain_card_move(
    command: Plan2NativeEffectChainChildCommand,
    state: RemainingCardMoveState,
    context: RemainingCardMoveExecutionContext,
    *,
    database: str | Path = DEFAULT_DATABASE,
) -> RemainingCardMoveResult:
    """Dispatch the one CardMove command to its exact GUID/rematch executor."""

    program = command.program
    if not (
        program.child_executor == EXECUTOR_CARD_MOVE
        and program.timer_effect_id == TIMER_CARD_MOVE_PARENT_ID
        and program.child_effect_id == TIMER_CARD_MOVE_CHILD_ID
    ):
        raise Plan2NativeEffectChainError("not-card-move-command")
    contract = load_plan2_timer_card_move_contract(database=Path(database))
    handoff = RemainingCardMoveHandoff(
        "timer-child",
        program.timer_effect_id,
        "timer-expiry-child",
        0,
        "callback",
        1,
    )
    return simulate_remaining_card_move(
        state,
        contract.child_contract,
        handoff,
        context,
        database=Path(database),
    )


compile_effect_chains = compile_plan2_native_catalog_effect_chains
install_effect_chain = install_plan2_native_effect_chain
advance_effect_chain_start_turn = start_plan2_native_effect_chain_turn
