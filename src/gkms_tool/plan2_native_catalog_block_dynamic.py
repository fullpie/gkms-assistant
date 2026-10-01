"""Standalone Plan2 catalog owner for two direct dynamic-Block executors.

The exact Common/Plan2 direct-card slice is 27 versions/occurrences:

* 15 ``ExamBlockAddMultipleAggressive`` effects; and
* 12 ``ExamBlockPerUseCardCount`` effects.

Compilation is independent of the central native catalog.  It validates the
complete runtime-relevant Master shape and reports per-version companion
blockers so a later central owner can distinguish a locally executable effect
from a card version that still has unrelated gaps.

Execution follows the Android v3.2.3 native order.  Dynamic Aggressive is read
at effect execution time.  Per-use-card-count reads the global exam count
before the surrounding play increments the exam and turn counters; an
explicit per-card count is retained only to prove that it is not the source.
Both feed the shared binary32/ceil ``CalculateAddBlock`` implementation, then
apply native signed-Int32 ``AddBlockFix``/``SetBlock`` semantics.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3
from typing import Literal, Mapping

from .logic_engine import PLAN_COMMON, PLAN_LOGIC, load_master_effect
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    INT32_MAX,
    INT32_MIN,
    AddBlockSettings,
    AddBlockStatus,
    calculate_add_block,
    f32,
    permille_to_f32,
)
from .plan2_block_per_use_card_count import (
    BLOCK_PER_USE_CARD_COUNT_EFFECT_TYPE,
    Plan2BlockPerUseCardCountEffect,
    load_plan2_block_per_use_card_count_effect,
)


PLAN2_NATIVE_DYNAMIC_BLOCK_SCHEMA_VERSION = 1
EXPECTED_VERSION_COUNT = 27
EXPECTED_OCCURRENCE_COUNT = 27
EXPECTED_AGGRESSIVE_OCCURRENCE_COUNT = 15
EXPECTED_PER_USE_OCCURRENCE_COUNT = 12
EXPECTED_UNIQUE_EFFECT_COUNT = 15
EXPECTED_COMPANION_BLOCKER_COUNT = 21
EXPECTED_DIRECT_UNLOCK_VERSION_COUNT = 12

BLOCK_ADD_MULTIPLE_AGGRESSIVE_EFFECT_TYPE = (
    "ProduceExamEffectType_ExamBlockAddMultipleAggressive"
)
TARGET_EFFECT_TYPES = frozenset(
    {
        BLOCK_ADD_MULTIPLE_AGGRESSIVE_EFFECT_TYPE,
        BLOCK_PER_USE_CARD_COUNT_EFFECT_TYPE,
    }
)
BLOCK_EFFECT_GROUP = "effect_group-visible-exam_block-000"

COST_UNKNOWN = "ExamCostType_Unknown"
MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"
EFFECT_UNKNOWN = "ProduceExamEffectType_Unknown"
PICK_RANGE_UNKNOWN = "ProducePickRangeType_Unknown"
PICK_COUNT_UNKNOWN = "ProducePickCountType_Unknown"
PLAY_ORIGINS = frozenset({"normal", "forced", "extra"})

COMPANION_LESSON_AGGRESSIVE = (
    "ProduceExamEffectType_ExamLessonDependExamCardPlayAggressive"
)
COMPANION_STAMINA_DOWN_FIX = (
    "ProduceExamEffectType_ExamStaminaConsumptionDownFix"
)
COMPANION_UNBOUND_EFFECT_TYPES = frozenset(
    {COMPANION_LESSON_AGGRESSIVE, COMPANION_STAMINA_DOWN_FIX}
)
KNOWN_SIBLING_EFFECT_TYPES = frozenset(
    {
        "ProduceExamEffectType_ExamLessonDependBlock",
        "ProduceExamEffectType_ExamReview",
        *COMPANION_UNBOUND_EFFECT_TYPES,
    }
)
KNOWN_COMPANION_COST = "ExamCostType_ExamReview"
KNOWN_COMPANION_CARD_TRIGGER = "e_trigger-none-card_play_aggressive_up-3"

COUNT_SOURCE_NONE = "none"
COUNT_SOURCE_EXAM = "exam-card-play-count"
CountSource = Literal["none", "exam-card-play-count"]
PlayOrigin = Literal["normal", "forced", "extra"]

ANDROID_V323_DYNAMIC_BLOCK_EVIDENCE = {
    "aggressive": {
        "executor": "Campus.InGame.Exam.BlockAddMultipleAggressiveEffectExecutor",
        "constructor_va": "0x7E71B38",
        "execute_va": "0x7E71CB0",
        "fact": (
            "constructor copies value1/value2/count; Execute converts value2 "
            "from permille, adds binary32 1.0, and calls CalculateAddBlock "
            "once per effectCount"
        ),
    },
    "per_use": {
        "executor": "Campus.InGame.Exam.BlockPerUserCardCountEffectExecutor",
        "execute_va": "0x7E73198",
        "count_getter_va": "0x7EB82FC",
        "fact": (
            "unchecked-int32 value1 + value2 * ExamCardPlayCount, read "
            "before PlayCardCountIncrement"
        ),
    },
    "block": {
        "calculate_add_block_va": "0x7E5E6CC",
        "add_block_fix_va": "0x7E5EA94",
        "set_block_va": "0x7EBB524",
        "fact": (
            "Block restriction is evaluated in CalculateAddBlock; AddBlockFix "
            "uses max(-currentBlock, calculated), then unchecked Int32 add. "
            "SetBlock applies no independent upper cap."
        ),
    },
}


class Plan2NativeDynamicBlockError(ValueError):
    """Stable fail-closed compiler/runtime diagnostic."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}:{detail}" if detail else code)


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise Plan2NativeDynamicBlockError("invalid-text", label)
    return value


def _i32_value(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Plan2NativeDynamicBlockError("invalid-int32", label)
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2NativeDynamicBlockError("int32-out-of-range", label)
    return value


def _nonnegative(value: object, label: str) -> int:
    result = _i32_value(value, label)
    if result < 0:
        raise Plan2NativeDynamicBlockError("negative-value", label)
    return result


def _i32_wrap(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _json_object(value: object, label: str) -> Mapping[str, object]:
    try:
        result = json.loads(str(value))
    except (TypeError, ValueError) as error:
        raise Plan2NativeDynamicBlockError("invalid-json", label) from error
    if not isinstance(result, Mapping):
        raise Plan2NativeDynamicBlockError("invalid-json-object", label)
    return result


def _json_array(value: object, label: str) -> tuple[object, ...]:
    try:
        result = json.loads(str(value))
    except (TypeError, ValueError) as error:
        raise Plan2NativeDynamicBlockError("invalid-json", label) from error
    if not isinstance(result, list):
        raise Plan2NativeDynamicBlockError("invalid-json-array", label)
    return tuple(result)


def _require_raw(
    raw: Mapping[str, object], expected: Mapping[str, object], label: str
) -> None:
    for key, value in expected.items():
        if key not in raw or raw[key] != value:
            raise Plan2NativeDynamicBlockError(
                "master-raw-drift", f"{label}.{key}"
            )


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeDynamicBlockBlocker:
    code: str
    card_id: str
    upgrade: int
    detail: str
    slot_index: int | None = None

    def __post_init__(self) -> None:
        _text(self.code, "blocker.code")
        _text(self.card_id, "blocker.card_id")
        _nonnegative(self.upgrade, "blocker.upgrade")
        _text(self.detail, "blocker.detail", empty=True)
        if self.slot_index is not None:
            _nonnegative(self.slot_index, "blocker.slot_index")

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade


@dataclass(frozen=True, slots=True)
class Plan2NativeDynamicBlockProgram:
    card_id: str
    upgrade: int
    slot_index: int
    ordered_effect_ids: tuple[str, ...]
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    count_source: CountSource
    companion_blockers: tuple[Plan2NativeDynamicBlockBlocker, ...] = ()

    def __post_init__(self) -> None:
        _text(self.card_id, "program.card_id")
        _nonnegative(self.upgrade, "program.upgrade")
        _nonnegative(self.slot_index, "program.slot_index")
        _text(self.effect_id, "program.effect_id")
        if self.effect_type not in TARGET_EFFECT_TYPES:
            raise Plan2NativeDynamicBlockError(
                "effect-type-unbound", self.effect_type
            )
        for name in ("value1", "value2", "effect_count", "effect_turn"):
            _i32_value(getattr(self, name), f"program.{name}")
        ordered = tuple(self.ordered_effect_ids)
        if (
            self.slot_index >= len(ordered)
            or ordered[self.slot_index] != self.effect_id
        ):
            raise Plan2NativeDynamicBlockError(
                "card-slot-order-drift", self.effect_id
            )
        if self.effect_type == BLOCK_ADD_MULTIPLE_AGGRESSIVE_EFFECT_TYPE:
            if (
                self.value1 < 0
                or self.value2 < 0
                or self.effect_count < 1
                or self.effect_turn != 0
                or self.count_source != COUNT_SOURCE_NONE
            ):
                raise Plan2NativeDynamicBlockError(
                    "aggressive-effect-shape", self.effect_id
                )
        else:
            if (
                self.effect_count != 0
                or self.effect_turn != 0
                or self.count_source != COUNT_SOURCE_EXAM
            ):
                raise Plan2NativeDynamicBlockError(
                    "per-use-effect-shape", self.effect_id
                )
        companions = tuple(self.companion_blockers)
        if any(
            not isinstance(item, Plan2NativeDynamicBlockBlocker)
            or item.ref != self.ref
            for item in companions
        ):
            raise Plan2NativeDynamicBlockError(
                "companion-blocker-shape", self.effect_id
            )
        object.__setattr__(self, "ordered_effect_ids", ordered)
        object.__setattr__(self, "companion_blockers", companions)

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def prior_effect_ids(self) -> tuple[str, ...]:
        return self.ordered_effect_ids[: self.slot_index]

    @property
    def locally_executable(self) -> bool:
        return True

    @property
    def central_direct_unlock(self) -> bool:
        return not self.companion_blockers

    @property
    def repetitions(self) -> int:
        if self.effect_type == BLOCK_ADD_MULTIPLE_AGGRESSIVE_EFFECT_TYPE:
            return self.effect_count
        return 1

    @property
    def aggressive_rate(self) -> float:
        if self.effect_type != BLOCK_ADD_MULTIPLE_AGGRESSIVE_EFFECT_TYPE:
            return f32(1.0)
        return f32(permille_to_f32(self.value2) + f32(1.0))


@dataclass(frozen=True, slots=True)
class Plan2NativeDynamicBlockCatalog:
    programs: tuple[Plan2NativeDynamicBlockProgram, ...]

    def __post_init__(self) -> None:
        programs = tuple(self.programs)
        refs = tuple(item.ref for item in programs)
        if refs != tuple(sorted(refs)) or len(refs) != len(set(refs)):
            raise Plan2NativeDynamicBlockError("catalog-order-or-duplicate")
        object.__setattr__(self, "programs", programs)

    def version(
        self, card_id: str, upgrade: int
    ) -> Plan2NativeDynamicBlockProgram:
        for program in self.programs:
            if program.ref == (card_id, upgrade):
                return program
        raise KeyError(f"dynamic Block version not found: {card_id}#{upgrade}")


@dataclass(frozen=True, slots=True)
class Plan2NativeDynamicBlockCompilation:
    schema_version: int
    database: str
    affected_refs: tuple[tuple[str, int], ...]
    occurrence_count: int
    aggressive_occurrence_count: int
    per_use_occurrence_count: int
    catalog: Plan2NativeDynamicBlockCatalog
    blockers: tuple[Plan2NativeDynamicBlockBlocker, ...]

    def __post_init__(self) -> None:
        if self.schema_version != PLAN2_NATIVE_DYNAMIC_BLOCK_SCHEMA_VERSION:
            raise Plan2NativeDynamicBlockError("schema-version")
        _text(self.database, "compilation.database")
        refs = tuple(self.affected_refs)
        if refs != tuple(sorted(refs)) or len(refs) != len(set(refs)):
            raise Plan2NativeDynamicBlockError("affected-ref-order")
        for name in (
            "occurrence_count",
            "aggressive_occurrence_count",
            "per_use_occurrence_count",
        ):
            _nonnegative(getattr(self, name), f"compilation.{name}")
        blockers = tuple(self.blockers)
        if any(
            not isinstance(item, Plan2NativeDynamicBlockBlocker)
            for item in blockers
        ):
            raise TypeError("blockers contain the wrong type")
        object.__setattr__(self, "affected_refs", refs)
        object.__setattr__(self, "blockers", blockers)

    @property
    def compiled_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(item.ref for item in self.catalog.programs)

    @property
    def failed_refs(self) -> tuple[tuple[str, int], ...]:
        compiled = set(self.compiled_refs)
        return tuple(ref for ref in self.affected_refs if ref not in compiled)

    @property
    def executable_version_count(self) -> int:
        return len(self.compiled_refs)

    @property
    def direct_unlock_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            item.ref
            for item in self.catalog.programs
            if item.central_direct_unlock
        )

    @property
    def co_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            item.ref
            for item in self.catalog.programs
            if item.companion_blockers
        )

    @property
    def companion_blockers(self) -> tuple[Plan2NativeDynamicBlockBlocker, ...]:
        return tuple(
            blocker
            for item in self.catalog.programs
            for blocker in item.companion_blockers
        )

    @property
    def companion_blocker_code_counts(self) -> dict[str, int]:
        return dict(
            sorted(Counter(item.code for item in self.companion_blockers).items())
        )

    @property
    def blocker_code_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.code for item in self.blockers).items()))

    @property
    def fully_compiled(self) -> bool:
        return not self.blockers and not self.failed_refs

    def blockers_for(
        self, ref: tuple[str, int]
    ) -> tuple[Plan2NativeDynamicBlockBlocker, ...]:
        return tuple(item for item in self.blockers if item.ref == ref)


def _validate_neutral_effect_raw(
    row: Mapping[str, object], effect_id: str, effect_type: str
) -> Mapping[str, object]:
    raw = _json_object(row["raw_json"], effect_id)
    _require_raw(
        raw,
        {
            "id": effect_id,
            "effectType": effect_type,
            "effectValue1": int(row["value1"]),
            "effectValue2": int(row["value2"]),
            "effectCount": int(row["effect_count"]),
            "effectTurn": int(row["effect_turn"]),
            "targetProduceCardId": "",
            "targetUpgradeCount": 0,
            "targetExamEffectType": EFFECT_UNKNOWN,
            "produceCardSearchId": "",
            "movePositionType": MOVE_UNKNOWN,
            "pickRangeType": PICK_RANGE_UNKNOWN,
            "pickCountReferenceProduceCardSearchId": "",
            "pickCountType": PICK_COUNT_UNKNOWN,
            "pickCountMin": 0,
            "pickCountMax": 0,
            "produceCardSearchId2": "",
            "pickRangeType2": PICK_RANGE_UNKNOWN,
            "pickCountReferenceProduceCardSearchId2": "",
            "pickCountType2": PICK_COUNT_UNKNOWN,
            "pickCountMin2": 0,
            "pickCountMax2": 0,
            "chainProduceExamEffectId": "",
            "chainProduceExamEffectIds": [],
            "produceExamStatusEnchantId": "",
            "produceCardStatusEnchantId": "",
            "produceCardGrowEffectIds": [],
            "effectGroupIds": [BLOCK_EFFECT_GROUP],
        },
        effect_id,
    )
    if str(row["status_enchant_id"]) or str(row["chain_effect_id"]):
        raise Plan2NativeDynamicBlockError(
            "effect-reference-shape", effect_id
        )
    return raw


def _compile_companion_blockers(
    *,
    card: Mapping[str, object],
    links: tuple[Mapping[str, object], ...],
    effects: Mapping[str, Mapping[str, object]],
    target_slot: int,
) -> tuple[Plan2NativeDynamicBlockBlocker, ...]:
    card_id = str(card["id"])
    upgrade = int(card["upgrade_count"])
    result: list[Plan2NativeDynamicBlockBlocker] = []
    cost_type = str(card["cost_type"])
    cost_value = int(card["cost_value"])
    if cost_type == KNOWN_COMPANION_COST and cost_value == 3:
        result.append(
            Plan2NativeDynamicBlockBlocker(
                "cost-runtime-unbound", card_id, upgrade, cost_type
            )
        )
    elif cost_type != COST_UNKNOWN or cost_value != 0:
        raise Plan2NativeDynamicBlockError(
            "companion-cost-shape", f"{cost_type}:{cost_value}"
        )
    card_trigger = str(card["play_trigger_id"])
    if card_trigger == KNOWN_COMPANION_CARD_TRIGGER:
        result.append(
            Plan2NativeDynamicBlockBlocker(
                "card-trigger-runtime-unbound",
                card_id,
                upgrade,
                card_trigger,
            )
        )
    elif card_trigger:
        raise Plan2NativeDynamicBlockError(
            "companion-card-trigger-shape", card_trigger
        )
    for slot_index, link in enumerate(links):
        if slot_index == target_slot:
            continue
        effect_id = str(link.get("produceExamEffectId", ""))
        link_trigger = str(link.get("produceExamTriggerId", ""))
        effect = effects.get(effect_id)
        if effect is None:
            raise Plan2NativeDynamicBlockError(
                "companion-effect-missing", effect_id
            )
        effect_type = str(effect["effect_type"])
        if link_trigger:
            raise Plan2NativeDynamicBlockError(
                "companion-link-trigger-shape", f"{effect_id}:{link_trigger}"
            )
        if effect_type in COMPANION_UNBOUND_EFFECT_TYPES:
            result.append(
                Plan2NativeDynamicBlockBlocker(
                    "effect-runtime-unbound",
                    card_id,
                    upgrade,
                    f"{effect_type}:{effect_id}",
                    slot_index,
                )
            )
        elif effect_type not in KNOWN_SIBLING_EFFECT_TYPES:
            raise Plan2NativeDynamicBlockError(
                "companion-effect-shape", f"{effect_type}:{effect_id}"
            )
    return tuple(result)


def _compile_program(
    *,
    card: Mapping[str, object],
    links: tuple[Mapping[str, object], ...],
    target_slot: int,
    row: Mapping[str, object],
    effects: Mapping[str, Mapping[str, object]],
    database: Path,
) -> Plan2NativeDynamicBlockProgram:
    card_id = str(card["id"])
    upgrade = int(card["upgrade_count"])
    effect_id = str(row["id"])
    effect_type = str(row["effect_type"])
    _validate_neutral_effect_raw(row, effect_id, effect_type)
    value1 = _i32_value(row["value1"], f"{effect_id}.value1")
    value2 = _i32_value(row["value2"], f"{effect_id}.value2")
    effect_count = _i32_value(
        row["effect_count"], f"{effect_id}.effect_count"
    )
    effect_turn = _i32_value(
        row["effect_turn"], f"{effect_id}.effect_turn"
    )
    if effect_type == BLOCK_PER_USE_CARD_COUNT_EFFECT_TYPE:
        exact = load_plan2_block_per_use_card_count_effect(effect_id, database)
        if exact != Plan2BlockPerUseCardCountEffect(
            effect_id, value1, value2, effect_count, effect_turn
        ):
            raise Plan2NativeDynamicBlockError(
                "per-use-helper-row-drift", effect_id
            )
        count_source: CountSource = COUNT_SOURCE_EXAM
    else:
        exact = load_master_effect(effect_id, database)
        normalized = (
            exact.id,
            exact.effect_type,
            exact.value1,
            exact.value2,
            exact.effect_count,
            exact.effect_turn,
            exact.status_enchant_id,
            exact.chain_effect_id,
        )
        row_normalized = (
            effect_id,
            effect_type,
            value1,
            value2,
            effect_count,
            effect_turn,
            str(row["status_enchant_id"]),
            str(row["chain_effect_id"]),
        )
        if normalized != row_normalized:
            raise Plan2NativeDynamicBlockError(
                "logic-engine-row-drift", effect_id
            )
        count_source = COUNT_SOURCE_NONE
    companions = _compile_companion_blockers(
        card=card,
        links=links,
        effects=effects,
        target_slot=target_slot,
    )
    return Plan2NativeDynamicBlockProgram(
        card_id=card_id,
        upgrade=upgrade,
        slot_index=target_slot,
        ordered_effect_ids=tuple(
            str(link["produceExamEffectId"]) for link in links
        ),
        effect_id=effect_id,
        effect_type=effect_type,
        value1=value1,
        value2=value2,
        effect_count=effect_count,
        effect_turn=effect_turn,
        count_source=count_source,
        companion_blockers=companions,
    )


def compile_plan2_native_catalog_block_dynamic(
    *, database: str | Path = DEFAULT_DATABASE
) -> Plan2NativeDynamicBlockCompilation:
    """Compile the exact 15+12 direct dynamic-Block Master slice."""

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
                "SELECT id, upgrade_count, cost_type, cost_value, "
                "play_trigger_id, play_effects_json FROM card "
                "WHERE plan_type IN (?, ?) ORDER BY id, upgrade_count",
                (PLAN_COMMON, PLAN_LOGIC),
            )
        )
        effects = {
            str(row["id"]): dict(row)
            for row in connection.execute("SELECT * FROM effect")
        }

    candidates: list[
        tuple[
            Mapping[str, object],
            tuple[Mapping[str, object], ...],
            int,
            Mapping[str, object],
        ]
    ] = []
    type_counts: Counter[str] = Counter()
    for card in cards:
        raw_links = _json_array(
            card["play_effects_json"],
            f"{card['id']}#{card['upgrade_count']}.play_effects",
        )
        links: list[Mapping[str, object]] = []
        for slot_index, raw_link in enumerate(raw_links):
            if not isinstance(raw_link, Mapping):
                raise Plan2NativeDynamicBlockError(
                    "card-link-shape",
                    f"{card['id']}#{card['upgrade_count']}@{slot_index}",
                )
            if not isinstance(raw_link.get("produceExamEffectId"), str) or not isinstance(
                raw_link.get("produceExamTriggerId"), str
            ):
                raise Plan2NativeDynamicBlockError(
                    "card-link-reference-shape",
                    f"{card['id']}#{card['upgrade_count']}@{slot_index}",
                )
            links.append(raw_link)
        normalized_links = tuple(links)
        for slot_index, link in enumerate(normalized_links):
            effect_id = str(link.get("produceExamEffectId", ""))
            effect = effects.get(effect_id)
            if effect is None or str(effect["effect_type"]) not in TARGET_EFFECT_TYPES:
                continue
            _require_raw(
                link,
                {
                    "produceExamEffectId": effect_id,
                    "produceExamTriggerId": "",
                    "hideIcon": False,
                    "isOncePlayEffect": False,
                },
                f"{card['id']}#{card['upgrade_count']}@{slot_index}",
            )
            if str(link.get("produceExamTriggerId", "")):
                raise Plan2NativeDynamicBlockError(
                    "target-link-trigger-shape", effect_id
                )
            candidates.append((card, normalized_links, slot_index, effect))
            type_counts[str(effect["effect_type"])] += 1

    affected_refs = tuple(
        sorted((str(card["id"]), int(card["upgrade_count"])) for card, _, _, _ in candidates)
    )
    if (
        len(candidates) != EXPECTED_OCCURRENCE_COUNT
        or len(affected_refs) != EXPECTED_VERSION_COUNT
        or len(set(affected_refs)) != EXPECTED_VERSION_COUNT
        or type_counts[BLOCK_ADD_MULTIPLE_AGGRESSIVE_EFFECT_TYPE]
        != EXPECTED_AGGRESSIVE_OCCURRENCE_COUNT
        or type_counts[BLOCK_PER_USE_CARD_COUNT_EFFECT_TYPE]
        != EXPECTED_PER_USE_OCCURRENCE_COUNT
    ):
        raise Plan2NativeDynamicBlockError(
            "target-slice-cardinality-drift",
            f"versions={len(set(affected_refs))},occurrences={len(candidates)},"
            f"aggressive={type_counts[BLOCK_ADD_MULTIPLE_AGGRESSIVE_EFFECT_TYPE]},"
            f"per-use={type_counts[BLOCK_PER_USE_CARD_COUNT_EFFECT_TYPE]}",
        )

    programs: list[Plan2NativeDynamicBlockProgram] = []
    blockers: list[Plan2NativeDynamicBlockBlocker] = []
    for card, links, slot_index, effect in candidates:
        try:
            programs.append(
                _compile_program(
                    card=card,
                    links=links,
                    target_slot=slot_index,
                    row=effect,
                    effects=effects,
                    database=database_path,
                )
            )
        except (KeyError, sqlite3.Error, TypeError, ValueError) as error:
            blockers.append(
                Plan2NativeDynamicBlockBlocker(
                    getattr(error, "code", "dynamic-block-compile-failed"),
                    str(card["id"]),
                    int(card["upgrade_count"]),
                    getattr(error, "detail", str(error)) or str(error),
                    slot_index,
                )
            )
    programs.sort(key=lambda item: item.ref)
    compilation = Plan2NativeDynamicBlockCompilation(
        PLAN2_NATIVE_DYNAMIC_BLOCK_SCHEMA_VERSION,
        str(database_path),
        affected_refs,
        len(candidates),
        type_counts[BLOCK_ADD_MULTIPLE_AGGRESSIVE_EFFECT_TYPE],
        type_counts[BLOCK_PER_USE_CARD_COUNT_EFFECT_TYPE],
        Plan2NativeDynamicBlockCatalog(tuple(programs)),
        tuple(blockers),
    )
    if compilation.fully_compiled:
        if len({item.effect_id for item in programs}) != EXPECTED_UNIQUE_EFFECT_COUNT:
            raise Plan2NativeDynamicBlockError("unique-effect-cardinality-drift")
        if len(compilation.companion_blockers) != EXPECTED_COMPANION_BLOCKER_COUNT:
            raise Plan2NativeDynamicBlockError("companion-cardinality-drift")
        if len(compilation.direct_unlock_refs) != EXPECTED_DIRECT_UNLOCK_VERSION_COUNT:
            raise Plan2NativeDynamicBlockError("direct-unlock-cardinality-drift")
    return compilation


load_plan2_native_catalog_block_dynamic = (
    compile_plan2_native_catalog_block_dynamic
)


@dataclass(frozen=True, slots=True)
class Plan2NativeDynamicBlockRuntime:
    """Smallest immutable execution-time state read by both executors."""

    block: int = 0
    aggressive: int = 0
    source_card_use_count: int = 0
    exam_card_play_count: int = 0
    turn_card_play_count: int = 0
    block_consumption_sum_count: int = 0
    block_restriction: bool = False
    block_add_down: bool = False
    block_add_down_restriction_for_ratio: bool = False
    block_add_down_restriction_for_fix: bool = False
    block_add_down_permille: int = 1000
    block_add_down_fix_peek: int = 0
    block_add_down_fix_consume: int = 0
    block_upper_cap: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "block",
            "aggressive",
            "block_consumption_sum_count",
            "block_add_down_permille",
            "block_add_down_fix_peek",
            "block_add_down_fix_consume",
        ):
            _i32_value(getattr(self, name), f"runtime.{name}")
        for name in (
            "source_card_use_count",
            "exam_card_play_count",
            "turn_card_play_count",
        ):
            _nonnegative(getattr(self, name), f"runtime.{name}")
        for name in (
            "block_restriction",
            "block_add_down",
            "block_add_down_restriction_for_ratio",
            "block_add_down_restriction_for_fix",
        ):
            if type(getattr(self, name)) is not bool:
                raise Plan2NativeDynamicBlockError(
                    "runtime-boolean-shape", name
                )
        if self.block_upper_cap is not None:
            _nonnegative(self.block_upper_cap, "runtime.block_upper_cap")

    @property
    def add_block_status(self) -> AddBlockStatus:
        return AddBlockStatus(
            block_restriction=self.block_restriction,
            aggressive=self.aggressive,
            block_add_down=self.block_add_down,
            block_add_down_restriction_for_ratio=(
                self.block_add_down_restriction_for_ratio
            ),
            block_add_down_restriction_for_fix=(
                self.block_add_down_restriction_for_fix
            ),
            block_add_down_fix_peek=self.block_add_down_fix_peek,
            block_add_down_fix_consume=self.block_add_down_fix_consume,
        )

    @property
    def add_block_settings(self) -> AddBlockSettings:
        return AddBlockSettings(
            block_add_down_permille=self.block_add_down_permille
        )


@dataclass(frozen=True, slots=True)
class Plan2NativeDynamicBlockExecutionInput:
    source_guid: str
    play_origin: PlayOrigin = "normal"
    accepted: bool = True
    completed_effect_ids: tuple[str, ...] = ()
    boundary: str = "direct-effect-before-play-count-increment"
    execution_snapshot_known: bool = True
    current_aggressive_known: bool = True
    count_snapshots_known: bool = True
    block_cap_snapshot_known: bool = True

    def __post_init__(self) -> None:
        _text(self.source_guid, "input.source_guid")
        if self.play_origin not in PLAY_ORIGINS:
            raise Plan2NativeDynamicBlockError(
                "play-origin-unbound", self.play_origin
            )
        completed = tuple(self.completed_effect_ids)
        if any(not isinstance(item, str) or not item for item in completed):
            raise Plan2NativeDynamicBlockError(
                "completed-effect-ids-shape"
            )
        object.__setattr__(self, "completed_effect_ids", completed)
        _text(self.boundary, "input.boundary")
        for name in (
            "accepted",
            "execution_snapshot_known",
            "current_aggressive_known",
            "count_snapshots_known",
            "block_cap_snapshot_known",
        ):
            if type(getattr(self, name)) is not bool:
                raise Plan2NativeDynamicBlockError(
                    "input-boolean-shape", name
                )


@dataclass(frozen=True, slots=True)
class Plan2NativeDynamicBlockDifference:
    kind: Literal["block", "effect"]
    preview: int
    current: int
    block_consumption_sum_count: int | None
    status_effect_type: int | None
    is_consumption: bool = False

    def __post_init__(self) -> None:
        if self.kind not in {"block", "effect"}:
            raise Plan2NativeDynamicBlockError("difference-kind-shape")
        _i32_value(self.preview, "difference.preview")
        _i32_value(self.current, "difference.current")
        if self.kind == "block":
            if self.status_effect_type is not None:
                raise Plan2NativeDynamicBlockError(
                    "block-difference-status-shape"
                )
            if self.block_consumption_sum_count is None:
                raise Plan2NativeDynamicBlockError(
                    "block-difference-sum-missing"
                )
            _i32_value(
                self.block_consumption_sum_count,
                "difference.block_consumption_sum_count",
            )
        elif (
            self.status_effect_type != 3
            or self.block_consumption_sum_count is not None
        ):
            raise Plan2NativeDynamicBlockError(
                "effect-difference-shape"
            )
        if type(self.is_consumption) is not bool or self.is_consumption:
            raise Plan2NativeDynamicBlockError(
                "dynamic-block-consumption-shape"
            )


@dataclass(frozen=True, slots=True)
class Plan2NativeDynamicBlockHit:
    repetition: int
    aggressive_at_execution: int
    requested_value: int
    additional_aggressive_rate: float
    calculated_value: int
    block_fix_value: int
    block_before: int
    block_after: int
    cap_applied: bool
    restriction_active: bool
    differences: tuple[Plan2NativeDynamicBlockDifference, ...]

    @property
    def actual_delta(self) -> int:
        return self.block_after - self.block_before

    @property
    def actual_delta_i32(self) -> int:
        return _i32_wrap(self.actual_delta)


@dataclass(frozen=True, slots=True)
class Plan2NativeDynamicBlockTransition:
    before: Plan2NativeDynamicBlockRuntime
    after: Plan2NativeDynamicBlockRuntime
    program: Plan2NativeDynamicBlockProgram
    execution_input: Plan2NativeDynamicBlockExecutionInput
    count_source: CountSource
    source_card_use_count_at_execution: int
    exam_card_play_count_at_execution: int
    turn_card_play_count_at_execution: int
    hits: tuple[Plan2NativeDynamicBlockHit, ...] = ()
    counters_incremented: bool = False
    unresolved: tuple[str, ...] = ()
    trace: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return not self.unresolved

    @property
    def applied_block(self) -> int:
        return self.after.block - self.before.block

    @property
    def applied_block_i32(self) -> int:
        return _i32_wrap(self.applied_block)

    @property
    def cap_applied(self) -> bool:
        return any(item.cap_applied for item in self.hits)


def _unresolved_transition(
    runtime: Plan2NativeDynamicBlockRuntime,
    program: Plan2NativeDynamicBlockProgram,
    execution_input: Plan2NativeDynamicBlockExecutionInput,
    *reasons: str,
) -> Plan2NativeDynamicBlockTransition:
    return Plan2NativeDynamicBlockTransition(
        runtime,
        runtime,
        program,
        execution_input,
        program.count_source,
        runtime.source_card_use_count,
        runtime.exam_card_play_count,
        runtime.turn_card_play_count,
        unresolved=tuple(dict.fromkeys(reasons)),
        trace=("fail-closed-before-dynamic-block-commit",),
    )


def execute_plan2_native_dynamic_block(
    runtime: Plan2NativeDynamicBlockRuntime,
    program: Plan2NativeDynamicBlockProgram,
    execution_input: Plan2NativeDynamicBlockExecutionInput,
) -> Plan2NativeDynamicBlockTransition:
    """Execute the one direct effect without advancing card-play counters."""

    if not isinstance(runtime, Plan2NativeDynamicBlockRuntime):
        raise TypeError("runtime must be Plan2NativeDynamicBlockRuntime")
    if not isinstance(program, Plan2NativeDynamicBlockProgram):
        raise TypeError("program must be Plan2NativeDynamicBlockProgram")
    if not isinstance(execution_input, Plan2NativeDynamicBlockExecutionInput):
        raise TypeError("execution_input has the wrong type")
    if not execution_input.accepted:
        return Plan2NativeDynamicBlockTransition(
            runtime,
            runtime,
            program,
            execution_input,
            program.count_source,
            runtime.source_card_use_count,
            runtime.exam_card_play_count,
            runtime.turn_card_play_count,
            trace=("rejected-before-direct-effects",),
        )
    errors: list[str] = []
    if not execution_input.execution_snapshot_known:
        errors.append("execution-snapshot-unknown")
    if execution_input.boundary != "direct-effect-before-play-count-increment":
        errors.append("execution-boundary-unbound")
    if execution_input.completed_effect_ids != program.prior_effect_ids:
        errors.append("prior-card-effect-order-unproven")
    if not execution_input.block_cap_snapshot_known:
        errors.append("block-cap-snapshot-unknown")
    if runtime.block_upper_cap is not None:
        errors.append("native-block-upper-cap-unbound")
    # Both executors call CalculateAddBlock and therefore both read current
    # Aggressive.  The aggressive-specific executor only changes the rate.
    if not execution_input.current_aggressive_known:
        errors.append("current-aggressive-snapshot-unknown")
    if (
        program.effect_type == BLOCK_PER_USE_CARD_COUNT_EFFECT_TYPE
        and not execution_input.count_snapshots_known
    ):
        errors.append("card-play-count-snapshots-unknown")
    if errors:
        return _unresolved_transition(
            runtime, program, execution_input, *errors
        )

    if program.effect_type == BLOCK_PER_USE_CARD_COUNT_EFFECT_TYPE:
        requested = _i32_wrap(
            program.value1
            + program.value2 * runtime.exam_card_play_count
        )
        rate = f32(1.0)
        trace = [
            "executor:read:ExamCardPlayCount",
            "executor:ignore:source-card-use-count",
            "executor:ignore:TurnCardPlayCount",
            "executor:i32-madd:value1+value2*ExamCardPlayCount",
        ]
    else:
        requested = program.value1
        rate = program.aggressive_rate
        trace = [
            "executor:FloatFromPermil(value2)",
            "executor:f32-add:additionalAggressiveRate+1.0f",
        ]

    current = runtime
    hits: list[Plan2NativeDynamicBlockHit] = []
    try:
        aggressive_at_execution = runtime.aggressive
        calculated_at_execution = calculate_add_block(
            requested,
            is_buff_active=True,
            status=runtime.add_block_status,
            settings=runtime.add_block_settings,
            additional_multiple_aggressive_rate=rate,
        )
        trace.extend(
            (
                f"executor:read-current-Aggressive:{aggressive_at_execution}",
                f"executor:CalculateAddBlock:{calculated_at_execution}",
            )
        )
        for repetition in range(program.repetitions):
            # Native AddBlockFix uses signed 32-bit negation/addition.  It
            # floors ordinary negative additions at zero, but SetBlock has no
            # independent upper cap and therefore preserves Int32 wrap.
            block_fix = max(
                _i32_wrap(-current.block), calculated_at_execution
            )
            block_after = _i32_wrap(current.block + block_fix)
            differences = (
                Plan2NativeDynamicBlockDifference(
                    "block",
                    current.block,
                    block_after,
                    current.block_consumption_sum_count,
                    None,
                ),
                Plan2NativeDynamicBlockDifference(
                    "effect", current.block, block_after, None, 3
                ),
            )
            hits.append(
                Plan2NativeDynamicBlockHit(
                    repetition,
                    aggressive_at_execution,
                    requested,
                    rate,
                    calculated_at_execution,
                    block_fix,
                    current.block,
                    block_after,
                    False,
                    current.block_restriction,
                    differences,
                )
            )
            trace.extend(
                (
                    f"hit:{repetition}:AddBlockFix:{current.block}->{block_after}",
                    f"hit:{repetition}:actual-delta:{block_after - current.block}",
                    f"hit:{repetition}:block-upper-cap:none",
                )
            )
            current = replace(current, block=block_after)
    except (OverflowError, TypeError, ValueError) as error:
        return _unresolved_transition(
            runtime,
            program,
            execution_input,
            f"dynamic-block-arithmetic-unresolved:{error}",
        )

    trace.extend(
        (
            "AddBlockFix:SetBlock(isConsumption=false)",
            "block-consumption-sum:unchanged",
            "card-play-count-increment:pending-after-direct-effects",
        )
    )
    return Plan2NativeDynamicBlockTransition(
        runtime,
        current,
        program,
        execution_input,
        program.count_source,
        runtime.source_card_use_count,
        runtime.exam_card_play_count,
        runtime.turn_card_play_count,
        tuple(hits),
        trace=tuple(trace),
    )


def simulate_plan2_native_dynamic_block_card_play(
    runtime: Plan2NativeDynamicBlockRuntime,
    program: Plan2NativeDynamicBlockProgram,
    execution_input: Plan2NativeDynamicBlockExecutionInput,
) -> Plan2NativeDynamicBlockTransition:
    """Execute the effect, then increment native exam/turn play counters."""

    if not execution_input.accepted:
        return execute_plan2_native_dynamic_block(
            runtime, program, execution_input
        )
    if any(
        value == INT32_MAX
        for value in (
            runtime.exam_card_play_count,
            runtime.turn_card_play_count,
        )
    ):
        return _unresolved_transition(
            runtime,
            program,
            execution_input,
            "card-play-count-overflow",
        )
    transition = execute_plan2_native_dynamic_block(
        runtime, program, execution_input
    )
    if not transition.executable or not execution_input.accepted:
        return transition
    after = replace(
        transition.after,
        exam_card_play_count=transition.after.exam_card_play_count + 1,
        turn_card_play_count=transition.after.turn_card_play_count + 1,
    )
    return replace(
        transition,
        after=after,
        counters_incremented=True,
        trace=transition.trace
        + (
            "source-card-use-count:not-source-and-not-mutated-here",
            "card-play-count-increment:exam",
            "card-play-count-increment:turn",
        ),
    )


__all__ = [
    "ANDROID_V323_DYNAMIC_BLOCK_EVIDENCE",
    "BLOCK_ADD_MULTIPLE_AGGRESSIVE_EFFECT_TYPE",
    "BLOCK_PER_USE_CARD_COUNT_EFFECT_TYPE",
    "COUNT_SOURCE_EXAM",
    "COUNT_SOURCE_NONE",
    "EXPECTED_AGGRESSIVE_OCCURRENCE_COUNT",
    "EXPECTED_COMPANION_BLOCKER_COUNT",
    "EXPECTED_DIRECT_UNLOCK_VERSION_COUNT",
    "EXPECTED_OCCURRENCE_COUNT",
    "EXPECTED_PER_USE_OCCURRENCE_COUNT",
    "EXPECTED_UNIQUE_EFFECT_COUNT",
    "EXPECTED_VERSION_COUNT",
    "Plan2NativeDynamicBlockBlocker",
    "Plan2NativeDynamicBlockCatalog",
    "Plan2NativeDynamicBlockCompilation",
    "Plan2NativeDynamicBlockDifference",
    "Plan2NativeDynamicBlockError",
    "Plan2NativeDynamicBlockExecutionInput",
    "Plan2NativeDynamicBlockHit",
    "Plan2NativeDynamicBlockProgram",
    "Plan2NativeDynamicBlockRuntime",
    "Plan2NativeDynamicBlockTransition",
    "compile_plan2_native_catalog_block_dynamic",
    "execute_plan2_native_dynamic_block",
    "load_plan2_native_catalog_block_dynamic",
    "simulate_plan2_native_dynamic_block_card_play",
]
