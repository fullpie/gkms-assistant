"""Exact Plan2 catalog owner for five dynamic Review/Lesson effect types.

This is a bounded leaf over the 20 Common/Plan2 card versions whose target
effect link has no trigger.  It compiles directly from Master and deliberately
does not import the program catalog, horizon, core runtime, or formal coverage.

The runtime is immutable.  Installers preserve native stack identity and
freshness, Review evaluators consume execution-time snapshots, and Lesson
handoff delegates the audited binary32/permille/ceil/max calculation order to
the existing exact helpers.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
import sqlite3
from typing import Final, Literal, TypeAlias

from . import nia_lesson_value_multiple as _nia
from .logic_engine import PLAN_COMMON, PLAN_LOGIC, load_master_effect
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    AddingParameterAdditionalData,
    AddingParameterSettings,
    AddingParameterStatus,
    INT32_MAX,
    INT32_MIN,
    NativeFormulaDomainError,
    ParameterApplicationStatus,
    ceil_f32_to_i32,
    f32,
    permille_to_f32,
)
from .plan2_lesson_multiple_depend_review_aggressive import (
    LessonParameterMultipleDependReviewOrAggressiveState,
    LessonParameterMultipleDependReviewOrAggressiveStatus,
    Plan2ReviewAggressiveSnapshot,
    apply_plan2_lesson_multiple_depend_review_or_aggressive_hits,
    install_plan2_lesson_multiple_depend_review_or_aggressive,
    load_plan2_lesson_multiple_depend_review_aggressive_card_versions,
    load_plan2_lesson_multiple_depend_review_aggressive_effect,
)
from .plan2_review_per_search_count import (
    EFFECT_ID as REVIEW_PER_SEARCH_EFFECT_ID,
    SEARCH_ID as REVIEW_PER_SEARCH_SEARCH_ID,
    resolve_plan2_review_per_search_count,
)


PLAN2_NATIVE_REVIEW_DYNAMIC_SCHEMA_VERSION: Final = 1
EXPECTED_VERSION_COUNT: Final = 20
EXPECTED_OCCURRENCE_COUNT: Final = 20
EXPECTED_UNIQUE_EFFECT_COUNT: Final = 6
EXPECTED_DIRECT_UNLOCK_VERSION_COUNT: Final = 12
EXPECTED_COMPANION_BLOCKER_COUNT: Final = 8

REVIEW_ADDITIVE_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamReviewAdditive"
)
REVIEW_VALUE_MULTIPLE_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamReviewValueMultiple"
)
REVIEW_PER_SEARCH_COUNT_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamReviewPerSearchCount"
)
LESSON_VALUE_MULTIPLE_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamLessonValueMultiple"
)
LESSON_DEPEND_REVIEW_AGGRESSIVE_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamLessonValueMultipleDependReviewOrAggressive"
)
TARGET_EFFECT_TYPES = frozenset(
    {
        REVIEW_ADDITIVE_EFFECT_TYPE,
        REVIEW_VALUE_MULTIPLE_EFFECT_TYPE,
        REVIEW_PER_SEARCH_COUNT_EFFECT_TYPE,
        LESSON_VALUE_MULTIPLE_EFFECT_TYPE,
        LESSON_DEPEND_REVIEW_AGGRESSIVE_EFFECT_TYPE,
    }
)
EXPECTED_TYPE_COUNTS = {effect_type: 4 for effect_type in TARGET_EFFECT_TYPES}

OP_INSTALL_REVIEW_ADDITIVE: Final = "install-review-additive"
OP_REVIEW_VALUE_MULTIPLE: Final = "review-value-multiple"
OP_REVIEW_PER_SEARCH_COUNT: Final = "review-per-search-count"
OP_INSTALL_LESSON_MULTIPLE: Final = "install-lesson-multiple"
OP_INSTALL_LESSON_DEPENDENT: Final = "install-lesson-dependent"
OperationKind: TypeAlias = Literal[
    "install-review-additive",
    "review-value-multiple",
    "review-per-search-count",
    "install-lesson-multiple",
    "install-lesson-dependent",
]
OPERATION_BY_TYPE: Mapping[str, OperationKind] = {
    REVIEW_ADDITIVE_EFFECT_TYPE: OP_INSTALL_REVIEW_ADDITIVE,
    REVIEW_VALUE_MULTIPLE_EFFECT_TYPE: OP_REVIEW_VALUE_MULTIPLE,
    REVIEW_PER_SEARCH_COUNT_EFFECT_TYPE: OP_REVIEW_PER_SEARCH_COUNT,
    LESSON_VALUE_MULTIPLE_EFFECT_TYPE: OP_INSTALL_LESSON_MULTIPLE,
    LESSON_DEPEND_REVIEW_AGGRESSIVE_EFFECT_TYPE: OP_INSTALL_LESSON_DEPENDENT,
}

LAYER_REVIEW_ADDITIVE: Final = "review-additive"
LAYER_LESSON_MULTIPLE: Final = "lesson-multiple"
LAYER_LESSON_MULTIPLE_DOWN: Final = "lesson-multiple-down"
LAYER_LESSON_DEPENDENT: Final = "lesson-dependent"
LayerKind: TypeAlias = Literal[
    "review-additive",
    "lesson-multiple",
    "lesson-multiple-down",
    "lesson-dependent",
]
PLAY_ORIGINS = frozenset({"normal", "forced", "extra"})

_CARD_TRIGGER_DEPENDENT = "e_trigger-none-card_play_aggressive_up-9"
_CARD_TRIGGER_PER_SEARCH = "e_trigger-none-review_up-10"
_EFFECT_TRIGGER_LESSON_MULTIPLE = "e_trigger-none-review_up-15"
_STATUS_ENCHANT_TYPE = "ProduceExamEffectType_ExamStatusEnchant"

_UNKNOWN_EXAM_EFFECT = "ProduceExamEffectType_Unknown"
_UNKNOWN_MOVE = "ProduceCardMovePositionType_Unknown"
_UNKNOWN_PICK_RANGE = "ProducePickRangeType_Unknown"
_UNKNOWN_PICK_COUNT = "ProducePickCountType_Unknown"
_PICK_ALL = "ProducePickRangeType_All"


class Plan2NativeReviewDynamicError(ValueError):
    """Stable fail-closed compiler/runtime diagnostic."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


def _i32(value: object, label: str) -> int:
    if type(value) is not int:
        raise Plan2NativeReviewDynamicError("invalid-int32", label)
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2NativeReviewDynamicError("int32-out-of-range", label)
    return value


def _nonnegative(value: object, label: str) -> int:
    result = _i32(value, label)
    if result < 0:
        raise Plan2NativeReviewDynamicError("negative-value", label)
    return result


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value):
        raise Plan2NativeReviewDynamicError("invalid-text", label)
    return value


def _i32_wrap(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _ceil(value: float, label: str) -> int:
    try:
        value = f32(value)
        if not math.isfinite(value):
            raise NativeFormulaDomainError(label)
        return ceil_f32_to_i32(value)
    except (NativeFormulaDomainError, OverflowError, ValueError) as error:
        raise Plan2NativeReviewDynamicError("native-domain", label) from error


def _json_array(value: object, label: str) -> tuple[object, ...]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2NativeReviewDynamicError("invalid-json", label) from error
    if not isinstance(parsed, list):
        raise Plan2NativeReviewDynamicError("invalid-json-array", label)
    return tuple(parsed)


def _json_object(value: object, label: str) -> Mapping[str, object]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2NativeReviewDynamicError("invalid-json", label) from error
    if not isinstance(parsed, Mapping):
        raise Plan2NativeReviewDynamicError("invalid-json-object", label)
    return parsed


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeReviewDynamicBlocker:
    code: str
    card_id: str
    upgrade: int
    detail: str = ""
    slot_index: int = -1

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade


@dataclass(frozen=True, slots=True)
class Plan2NativeReviewDynamicProgram:
    card_id: str
    upgrade: int
    slot_index: int
    ordered_effect_ids: tuple[str, ...]
    effect_id: str
    effect_type: str
    operation_kind: OperationKind
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    search_id: str = ""
    pick_range_type: str = ""
    companion_blockers: tuple[Plan2NativeReviewDynamicBlocker, ...] = ()

    def __post_init__(self) -> None:
        _text(self.card_id, "program.card_id")
        _nonnegative(self.upgrade, "program.upgrade")
        _nonnegative(self.slot_index, "program.slot_index")
        if type(self.ordered_effect_ids) is not tuple or any(
            type(item) is not str or not item for item in self.ordered_effect_ids
        ):
            raise Plan2NativeReviewDynamicError("effect-order-shape")
        if self.slot_index >= len(self.ordered_effect_ids):
            raise Plan2NativeReviewDynamicError("effect-slot-out-of-range")
        _text(self.effect_id, "program.effect_id")
        if self.effect_type not in TARGET_EFFECT_TYPES:
            raise Plan2NativeReviewDynamicError(
                "effect-type-unbound", self.effect_type
            )
        if self.operation_kind != OPERATION_BY_TYPE[self.effect_type]:
            raise Plan2NativeReviewDynamicError(
                "operation-kind-drift", self.effect_id
            )
        for name in ("value1", "value2", "effect_count", "effect_turn"):
            _i32(getattr(self, name), f"program.{name}")
        if self.ordered_effect_ids[self.slot_index] != self.effect_id:
            raise Plan2NativeReviewDynamicError("effect-order-drift", self.effect_id)

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def prior_effect_ids(self) -> tuple[str, ...]:
        return self.ordered_effect_ids[: self.slot_index]


@dataclass(frozen=True, slots=True)
class Plan2NativeReviewDynamicCatalog:
    programs: tuple[Plan2NativeReviewDynamicProgram, ...]

    def version(self, card_id: str, upgrade: int) -> Plan2NativeReviewDynamicProgram:
        matches = tuple(item for item in self.programs if item.ref == (card_id, upgrade))
        if len(matches) != 1:
            raise KeyError(f"review-dynamic version resolves {len(matches)} times")
        return matches[0]


@dataclass(frozen=True, slots=True)
class Plan2NativeReviewDynamicCompilation:
    schema_version: int
    database: str
    affected_refs: tuple[tuple[str, int], ...]
    occurrence_count: int
    type_counts: tuple[tuple[str, int], ...]
    catalog: Plan2NativeReviewDynamicCatalog
    blockers: tuple[Plan2NativeReviewDynamicBlocker, ...]

    @property
    def fully_compiled(self) -> bool:
        return not self.blockers

    @property
    def executable_version_count(self) -> int:
        return len(self.catalog.programs)

    @property
    def companion_blockers(self) -> tuple[Plan2NativeReviewDynamicBlocker, ...]:
        return tuple(
            blocker
            for program in self.catalog.programs
            for blocker in program.companion_blockers
        )

    @property
    def direct_unlock_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            item.ref for item in self.catalog.programs if not item.companion_blockers
        )

    @property
    def co_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            item.ref for item in self.catalog.programs if item.companion_blockers
        )

    @property
    def companion_blocker_code_counts(self) -> Mapping[str, int]:
        return dict(Counter(item.code for item in self.companion_blockers))


def _validate_link(link: Mapping[str, object], label: str) -> None:
    expected_keys = {
        "produceExamEffectId",
        "produceExamTriggerId",
        "hideIcon",
        "isOncePlayEffect",
    }
    if set(link) != expected_keys:
        raise Plan2NativeReviewDynamicError("card-link-shape", label)
    if link["produceExamTriggerId"] != "":
        raise Plan2NativeReviewDynamicError("target-link-trigger-shape", label)
    if link["hideIcon"] is not False or link["isOncePlayEffect"] is not False:
        raise Plan2NativeReviewDynamicError("target-link-flag-shape", label)


def _validate_raw_effect(row: Mapping[str, object]) -> Mapping[str, object]:
    effect_id = str(row["id"])
    effect_type = str(row["effect_type"])
    raw = _json_object(row["raw_json"], effect_id)
    search_id = (
        REVIEW_PER_SEARCH_SEARCH_ID
        if effect_type == REVIEW_PER_SEARCH_COUNT_EFFECT_TYPE
        else ""
    )
    pick_range = _PICK_ALL if search_id else _UNKNOWN_PICK_RANGE
    groups: object
    if effect_type in {
        REVIEW_ADDITIVE_EFFECT_TYPE,
        REVIEW_VALUE_MULTIPLE_EFFECT_TYPE,
        REVIEW_PER_SEARCH_COUNT_EFFECT_TYPE,
    }:
        groups = ["effect_group-visible-exam_review-000"]
    elif effect_type == LESSON_VALUE_MULTIPLE_EFFECT_TYPE:
        groups = ["effect_group-visible-exam_lesson_value_multiple-000"]
    else:
        groups = []
    expected = {
        "id": effect_id,
        "effectType": effect_type,
        "effectValue1": row["value1"],
        "effectValue2": row["value2"],
        "effectCount": row["effect_count"],
        "effectTurn": row["effect_turn"],
        "targetProduceCardId": "",
        "targetUpgradeCount": 0,
        "targetExamEffectType": _UNKNOWN_EXAM_EFFECT,
        "produceCardSearchId": search_id,
        "movePositionType": _UNKNOWN_MOVE,
        "pickRangeType": pick_range,
        "pickCountReferenceProduceCardSearchId": "",
        "pickCountType": _UNKNOWN_PICK_COUNT,
        "pickCountMin": 0,
        "pickCountMax": 0,
        "produceCardSearchId2": "",
        "pickRangeType2": _UNKNOWN_PICK_RANGE,
        "pickCountReferenceProduceCardSearchId2": "",
        "pickCountType2": _UNKNOWN_PICK_COUNT,
        "pickCountMin2": 0,
        "pickCountMax2": 0,
        "chainProduceExamEffectId": "",
        "chainProduceExamEffectIds": [],
        "produceExamStatusEnchantId": "",
        "produceCardStatusEnchantId": "",
        "produceCardGrowEffectIds": [],
        "effectGroupIds": groups,
    }
    for key, wanted in expected.items():
        if key not in raw or raw[key] != wanted:
            raise Plan2NativeReviewDynamicError(
                "master-raw-drift", f"{effect_id}.{key}"
            )
    if row["status_enchant_id"] or row["chain_effect_id"]:
        raise Plan2NativeReviewDynamicError("master-link-drift", effect_id)
    return raw


def _companion_blockers(
    card: Mapping[str, object],
    links: tuple[Mapping[str, object], ...],
    effects: Mapping[str, Mapping[str, object]],
    target_slot: int,
) -> tuple[Plan2NativeReviewDynamicBlocker, ...]:
    card_id = str(card["id"])
    upgrade = int(card["upgrade_count"])
    target_type = str(effects[str(links[target_slot]["produceExamEffectId"])]["effect_type"])
    result: list[Plan2NativeReviewDynamicBlocker] = []
    card_trigger = str(card["play_trigger_id"])
    if card_trigger:
        if (
            target_type == LESSON_DEPEND_REVIEW_AGGRESSIVE_EFFECT_TYPE
            and card_trigger == _CARD_TRIGGER_DEPENDENT
        ):
            result.append(
                Plan2NativeReviewDynamicBlocker(
                    "card-trigger-runtime-unbound",
                    card_id,
                    upgrade,
                    card_trigger,
                    -1,
                )
            )
        elif not (
            target_type == REVIEW_PER_SEARCH_COUNT_EFFECT_TYPE
            and card_trigger == _CARD_TRIGGER_PER_SEARCH
        ):
            raise Plan2NativeReviewDynamicError(
                "companion-card-trigger-shape", f"{card_id}:{card_trigger}"
            )
    for slot_index, link in enumerate(links):
        if slot_index == target_slot or not link["produceExamTriggerId"]:
            continue
        sibling_id = str(link["produceExamEffectId"])
        sibling = effects.get(sibling_id)
        if (
            target_type == LESSON_VALUE_MULTIPLE_EFFECT_TYPE
            and slot_index == 3
            and link["produceExamTriggerId"] == _EFFECT_TRIGGER_LESSON_MULTIPLE
            and sibling is not None
            and sibling["effect_type"] == _STATUS_ENCHANT_TYPE
        ):
            result.append(
                Plan2NativeReviewDynamicBlocker(
                    "effect-trigger-runtime-unbound",
                    card_id,
                    upgrade,
                    f"{sibling_id}:{_EFFECT_TRIGGER_LESSON_MULTIPLE}",
                    slot_index,
                )
            )
        else:
            raise Plan2NativeReviewDynamicError(
                "companion-effect-trigger-shape",
                f"{card_id}:{upgrade}:{slot_index}",
            )
    return tuple(result)


def _validate_exact_helper(
    row: Mapping[str, object], database: Path
) -> tuple[str, str]:
    effect_id = str(row["id"])
    effect_type = str(row["effect_type"])
    if effect_type in {REVIEW_ADDITIVE_EFFECT_TYPE, REVIEW_VALUE_MULTIPLE_EFFECT_TYPE}:
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
        stored = (
            effect_id,
            effect_type,
            row["value1"],
            row["value2"],
            row["effect_count"],
            row["effect_turn"],
            row["status_enchant_id"],
            row["chain_effect_id"],
        )
        if normalized != stored:
            raise Plan2NativeReviewDynamicError("logic-engine-row-drift", effect_id)
    elif effect_type == LESSON_VALUE_MULTIPLE_EFFECT_TYPE:
        exact = _nia.load_master_effect(effect_id, database=database)
        if (exact.effect_id, exact.permil, exact.turn) != (
            effect_id,
            row["value1"],
            row["effect_turn"],
        ):
            raise Plan2NativeReviewDynamicError("lesson-helper-row-drift", effect_id)
    elif effect_type == REVIEW_PER_SEARCH_COUNT_EFFECT_TYPE:
        exact = resolve_plan2_review_per_search_count(effect_id, database)
        if (
            exact.effect.value1,
            exact.effect.value2,
            exact.effect.effect_count,
            exact.effect.effect_turn,
        ) != (
            row["value1"],
            row["value2"],
            row["effect_count"],
            row["effect_turn"],
        ):
            raise Plan2NativeReviewDynamicError("search-helper-row-drift", effect_id)
        return exact.effect.search_id, exact.effect.pick_range_type
    elif effect_type == LESSON_DEPEND_REVIEW_AGGRESSIVE_EFFECT_TYPE:
        exact = load_plan2_lesson_multiple_depend_review_aggressive_effect(
            effect_id, database=database
        )
        if (exact.effect_id, exact.value1, exact.value2, exact.count, exact.turn) != (
            effect_id,
            row["value1"],
            row["value2"],
            row["effect_count"],
            row["effect_turn"],
        ):
            raise Plan2NativeReviewDynamicError("dependent-helper-row-drift", effect_id)
    return "", ""


def compile_plan2_native_catalog_review_dynamic(
    *, database: str | Path = DEFAULT_DATABASE
) -> Plan2NativeReviewDynamicCompilation:
    """Compile and account for exactly 4+4+4+4+4 target versions."""

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
                "SELECT id, upgrade_count, plan_type, play_trigger_id, "
                "play_effects_json FROM card WHERE plan_type IN (?, ?) "
                "ORDER BY id, upgrade_count",
                (PLAN_COMMON, PLAN_LOGIC),
            )
        )
        effects = {
            str(row["id"]): dict(row)
            for row in connection.execute("SELECT * FROM effect")
        }

    candidates: list[
        tuple[Mapping[str, object], tuple[Mapping[str, object], ...], int, Mapping[str, object]]
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
                raise Plan2NativeReviewDynamicError(
                    "card-link-shape", f"{card['id']}@{slot_index}"
                )
            links.append(raw_link)
        normalized_links = tuple(links)
        for slot_index, link in enumerate(normalized_links):
            effect_id = link.get("produceExamEffectId")
            effect = effects.get(str(effect_id))
            if effect is None or effect["effect_type"] not in TARGET_EFFECT_TYPES:
                continue
            # Triggered occurrences remain owned by their trigger runtime; this
            # leaf owns only direct target links.
            if link.get("produceExamTriggerId") != "":
                continue
            _validate_link(link, f"{card['id']}#{card['upgrade_count']}@{slot_index}")
            candidates.append((card, normalized_links, slot_index, effect))
            type_counts[str(effect["effect_type"])] += 1

    refs = tuple(sorted((str(c[0]["id"]), int(c[0]["upgrade_count"])) for c in candidates))
    if (
        len(candidates) != EXPECTED_OCCURRENCE_COUNT
        or len(refs) != EXPECTED_VERSION_COUNT
        or len(set(refs)) != EXPECTED_VERSION_COUNT
        or dict(type_counts) != EXPECTED_TYPE_COUNTS
    ):
        raise Plan2NativeReviewDynamicError(
            "target-slice-cardinality-drift",
            f"versions={len(set(refs))},occurrences={len(candidates)},types={dict(type_counts)}",
        )

    # These exact helpers additionally pin all four dependent and per-search
    # card rows, including native slot order and search shape.
    dependent_versions = load_plan2_lesson_multiple_depend_review_aggressive_card_versions(
        database=database_path
    )
    per_search_contract = resolve_plan2_review_per_search_count(
        REVIEW_PER_SEARCH_EFFECT_ID, database_path
    )
    if len(dependent_versions) != 4 or len(per_search_contract.targets) != 4:
        raise Plan2NativeReviewDynamicError("exact-helper-cardinality-drift")

    programs: list[Plan2NativeReviewDynamicProgram] = []
    blockers: list[Plan2NativeReviewDynamicBlocker] = []
    for card, links, slot_index, row in candidates:
        try:
            _validate_raw_effect(row)
            search_id, pick_range = _validate_exact_helper(row, database_path)
            program = Plan2NativeReviewDynamicProgram(
                card_id=str(card["id"]),
                upgrade=int(card["upgrade_count"]),
                slot_index=slot_index,
                ordered_effect_ids=tuple(str(link["produceExamEffectId"]) for link in links),
                effect_id=str(row["id"]),
                effect_type=str(row["effect_type"]),
                operation_kind=OPERATION_BY_TYPE[str(row["effect_type"])],
                value1=_i32(row["value1"], f"{row['id']}.value1"),
                value2=_i32(row["value2"], f"{row['id']}.value2"),
                effect_count=_i32(row["effect_count"], f"{row['id']}.count"),
                effect_turn=_i32(row["effect_turn"], f"{row['id']}.turn"),
                search_id=search_id,
                pick_range_type=pick_range,
                companion_blockers=_companion_blockers(
                    card, links, effects, slot_index
                ),
            )
            programs.append(program)
        except (KeyError, sqlite3.Error, TypeError, ValueError) as error:
            blockers.append(
                Plan2NativeReviewDynamicBlocker(
                    getattr(error, "code", "review-dynamic-compile-failed"),
                    str(card["id"]),
                    int(card["upgrade_count"]),
                    getattr(error, "detail", str(error)) or str(error),
                    slot_index,
                )
            )
    programs.sort(key=lambda item: item.ref)
    compilation = Plan2NativeReviewDynamicCompilation(
        PLAN2_NATIVE_REVIEW_DYNAMIC_SCHEMA_VERSION,
        str(database_path),
        refs,
        len(candidates),
        tuple(sorted(type_counts.items())),
        Plan2NativeReviewDynamicCatalog(tuple(programs)),
        tuple(blockers),
    )
    if compilation.fully_compiled:
        if len({item.effect_id for item in programs}) != EXPECTED_UNIQUE_EFFECT_COUNT:
            raise Plan2NativeReviewDynamicError("unique-effect-cardinality-drift")
        if len(compilation.companion_blockers) != EXPECTED_COMPANION_BLOCKER_COUNT:
            raise Plan2NativeReviewDynamicError("companion-cardinality-drift")
        if len(compilation.direct_unlock_refs) != EXPECTED_DIRECT_UNLOCK_VERSION_COUNT:
            raise Plan2NativeReviewDynamicError("direct-unlock-cardinality-drift")
    return compilation


load_plan2_native_catalog_review_dynamic = compile_plan2_native_catalog_review_dynamic


@dataclass(frozen=True, slots=True)
class Plan2NativeReviewDynamicLayer:
    status_uid: int
    kind: LayerKind
    source_guid: str
    source_card_id: str
    source_upgrade: int
    source_effect_id: str
    permille: int
    turn: int
    passing_turn_start: bool = False
    is_turn_limited: bool | None = None

    def __post_init__(self) -> None:
        if self.status_uid <= 0:
            raise Plan2NativeReviewDynamicError("invalid-status-uid")
        if self.kind not in {
            LAYER_REVIEW_ADDITIVE,
            LAYER_LESSON_MULTIPLE,
            LAYER_LESSON_MULTIPLE_DOWN,
            LAYER_LESSON_DEPENDENT,
        }:
            raise Plan2NativeReviewDynamicError("layer-kind-unbound", str(self.kind))
        _text(self.source_guid, "layer.source_guid")
        _text(self.source_card_id, "layer.source_card_id")
        _nonnegative(self.source_upgrade, "layer.source_upgrade")
        _text(self.source_effect_id, "layer.source_effect_id")
        _i32(self.permille, "layer.permille")
        _i32(self.turn, "layer.turn")
        if type(self.passing_turn_start) is not bool:
            raise Plan2NativeReviewDynamicError("passing-flag-shape")
        if self.is_turn_limited is None:
            object.__setattr__(self, "is_turn_limited", self.turn >= 0)
        elif type(self.is_turn_limited) is not bool:
            raise Plan2NativeReviewDynamicError("limited-flag-shape")
        if self.kind == LAYER_LESSON_DEPENDENT and self.permille != 0:
            raise Plan2NativeReviewDynamicError("dependent-permille-shape")


@dataclass(frozen=True, slots=True)
class Plan2NativeReviewDynamicRuntime:
    review: int = 0
    review_status_present: bool = False
    review_passing_turn_start: bool = False
    layers: tuple[Plan2NativeReviewDynamicLayer, ...] = ()
    next_status_uid: int = 1

    def __post_init__(self) -> None:
        _nonnegative(self.review, "runtime.review")
        if type(self.next_status_uid) is not int or self.next_status_uid <= 0:
            raise Plan2NativeReviewDynamicError("runtime-next-uid-shape")
        if type(self.review_status_present) is not bool or type(
            self.review_passing_turn_start
        ) is not bool:
            raise Plan2NativeReviewDynamicError("review-lifecycle-shape")
        if not self.review_status_present and (
            self.review != 0 or self.review_passing_turn_start
        ):
            raise Plan2NativeReviewDynamicError("absent-review-shape")
        if type(self.layers) is not tuple or any(
            not isinstance(item, Plan2NativeReviewDynamicLayer) for item in self.layers
        ):
            raise Plan2NativeReviewDynamicError("runtime-layer-shape")
        uids = tuple(item.status_uid for item in self.layers)
        if len(uids) != len(set(uids)) or any(uid >= self.next_status_uid for uid in uids):
            raise Plan2NativeReviewDynamicError("runtime-uid-shape")


def apply_plan2_native_review_dynamic_review_gain(
    runtime: Plan2NativeReviewDynamicRuntime,
    *,
    value: int,
    count: int = 1,
) -> int:
    """Apply direct Review gains through active additive layers.

    ``ReviewAdditiveStatusEffect`` is a scalar runtime status, but its
    multiplier belongs to the same ordered Review dynamic stack used by
    ReviewValueMultiple/PerSearch.  Keep one binary32/ceil implementation for
    direct and listener-owned Review children so each occurrence is rounded in
    native order.
    """

    if not isinstance(runtime, Plan2NativeReviewDynamicRuntime):
        raise TypeError("runtime must be Plan2NativeReviewDynamicRuntime")
    _nonnegative(value, "review_gain")
    _nonnegative(count, "review_gain_count")
    review = runtime.review
    for _ in range(max(1, count)):
        multiple = f32(1.0)
        for layer in runtime.layers:
            if layer.kind == LAYER_REVIEW_ADDITIVE and (
                not layer.is_turn_limited or layer.turn > 0
            ):
                multiple = f32(multiple + permille_to_f32(layer.permille))
        delta = ceil_f32_to_i32(f32(f32(value) * multiple))
        review = _i32_wrap(review + delta)
        if review < 0:
            raise Plan2NativeReviewDynamicError("review-result-domain-unresolved")
    return review


@dataclass(frozen=True, slots=True)
class Plan2NativeReviewDynamicInstallInput:
    source_guid: str
    play_origin: str = "normal"
    completed_effect_ids: tuple[str, ...] = ()
    accepted: bool | None = True
    context_present: bool | None = True
    status_addition_blocked: bool | None = False

    def __post_init__(self) -> None:
        _text(self.source_guid, "install.source_guid")
        if self.play_origin not in PLAY_ORIGINS:
            raise Plan2NativeReviewDynamicError("play-origin-unbound", self.play_origin)
        if type(self.completed_effect_ids) is not tuple or any(
            type(item) is not str or not item for item in self.completed_effect_ids
        ):
            raise Plan2NativeReviewDynamicError("completed-order-shape")
        for name in ("accepted", "context_present", "status_addition_blocked"):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise Plan2NativeReviewDynamicError("snapshot-shape", name)


@dataclass(frozen=True, slots=True)
class Plan2NativeReviewDynamicInstallTransition:
    before: Plan2NativeReviewDynamicRuntime
    after: Plan2NativeReviewDynamicRuntime
    program: Plan2NativeReviewDynamicProgram
    execution_input: Plan2NativeReviewDynamicInstallInput
    executable: bool
    installed: bool
    merged_status_uid: int | None
    created_status_uid: int | None
    unresolved: tuple[str, ...]
    trace: tuple[str, ...]


def _install_unresolved(
    runtime: Plan2NativeReviewDynamicRuntime,
    program: Plan2NativeReviewDynamicProgram,
    request: Plan2NativeReviewDynamicInstallInput,
    *codes: str,
) -> Plan2NativeReviewDynamicInstallTransition:
    return Plan2NativeReviewDynamicInstallTransition(
        runtime, runtime, program, request, False, False, None, None, tuple(codes), ()
    )


def install_plan2_native_review_dynamic(
    runtime: Plan2NativeReviewDynamicRuntime,
    program: Plan2NativeReviewDynamicProgram,
    execution_input: Plan2NativeReviewDynamicInstallInput,
) -> Plan2NativeReviewDynamicInstallTransition:
    """Install one additive/multiplier/dependent layer in native stack order."""

    if program.operation_kind not in {
        OP_INSTALL_REVIEW_ADDITIVE,
        OP_INSTALL_LESSON_MULTIPLE,
        OP_INSTALL_LESSON_DEPENDENT,
    }:
        return _install_unresolved(runtime, program, execution_input, "not-installer")
    if execution_input.completed_effect_ids != program.prior_effect_ids:
        return _install_unresolved(runtime, program, execution_input, "effect-order-unresolved")
    unknown = tuple(
        f"{name}-unknown"
        for name in ("accepted", "context_present", "status_addition_blocked")
        if getattr(execution_input, name) is None
    )
    if unknown:
        return _install_unresolved(runtime, program, execution_input, *unknown)
    if not execution_input.accepted:
        return _install_unresolved(runtime, program, execution_input, "card-not-accepted")
    if not execution_input.context_present:
        return _install_unresolved(runtime, program, execution_input, "context-missing")
    if execution_input.status_addition_blocked:
        return Plan2NativeReviewDynamicInstallTransition(
            runtime,
            runtime,
            program,
            execution_input,
            True,
            False,
            None,
            None,
            (),
            ("try-add-status-blocked",),
        )

    layers = list(runtime.layers)
    merged_uid: int | None = None
    created_uid: int | None = None
    trace: tuple[str, ...]
    if program.operation_kind == OP_INSTALL_REVIEW_ADDITIVE:
        # Native searches the ordered list for the last matching turn.
        match = next(
            (
                index
                for index in range(len(layers) - 1, -1, -1)
                if layers[index].kind == LAYER_REVIEW_ADDITIVE
                and layers[index].turn == program.effect_turn
            ),
            None,
        )
        if match is not None:
            current = layers[match]
            layers[match] = replace(
                current, permille=max(0, _i32_wrap(current.permille + program.value1))
            )
            merged_uid = current.status_uid
            trace = ("read-effect-value-turn", "merge-last-same-turn", "clamp-zero")
        else:
            created_uid = runtime.next_status_uid
            layers.append(
                Plan2NativeReviewDynamicLayer(
                    created_uid,
                    LAYER_REVIEW_ADDITIVE,
                    execution_input.source_guid,
                    program.card_id,
                    program.upgrade,
                    program.effect_id,
                    program.value1,
                    program.effect_turn,
                )
            )
            trace = ("read-effect-value-turn", "append-fresh-status")
    elif program.operation_kind == OP_INSTALL_LESSON_MULTIPLE:
        indices = [
            index for index, layer in enumerate(layers) if layer.kind == LAYER_LESSON_MULTIPLE
        ]
        exact_before = _nia.LessonParameterMultipleState(
            tuple(
                _nia.LessonParameterMultipleStatus(
                    layer.permille,
                    layer.turn,
                    layer.passing_turn_start,
                    layer.is_turn_limited,
                )
                for layer in (layers[index] for index in indices)
            )
        )
        exact = _nia.install_lesson_value_multiple(
            exact_before, permil=program.value1, turn=program.effect_turn
        )
        if exact.merged_index is None:
            created_uid = runtime.next_status_uid
            layers.append(
                Plan2NativeReviewDynamicLayer(
                    created_uid,
                    LAYER_LESSON_MULTIPLE,
                    execution_input.source_guid,
                    program.card_id,
                    program.upgrade,
                    program.effect_id,
                    program.value1,
                    program.effect_turn,
                )
            )
            trace = ("lesson-helper", "append-fresh-status")
        else:
            layer_index = indices[exact.merged_index]
            current = layers[layer_index]
            layers[layer_index] = replace(
                current, permille=exact.state.statuses[exact.merged_index].permil
            )
            merged_uid = current.status_uid
            trace = ("lesson-helper", "merge-last-same-turn", "clamp-zero")
    else:
        exact_before = LessonParameterMultipleDependReviewOrAggressiveState(
            tuple(
                LessonParameterMultipleDependReviewOrAggressiveStatus(
                    layer.turn,
                    layer.passing_turn_start,
                    layer.is_turn_limited,
                )
                for layer in layers
                if layer.kind == LAYER_LESSON_DEPENDENT
            )
        )
        exact = install_plan2_lesson_multiple_depend_review_or_aggressive(
            exact_before, turn=program.effect_turn
        )
        if not exact.installed or exact.created_index is None:
            raise Plan2NativeReviewDynamicError("dependent-helper-install-drift")
        created_uid = runtime.next_status_uid
        layers.append(
            Plan2NativeReviewDynamicLayer(
                created_uid,
                LAYER_LESSON_DEPENDENT,
                execution_input.source_guid,
                program.card_id,
                program.upgrade,
                program.effect_id,
                0,
                program.effect_turn,
            )
        )
        trace = ("dependent-helper", "append-distinct-fresh-status")

    after = replace(
        runtime,
        layers=tuple(layers),
        next_status_uid=(
            runtime.next_status_uid + 1 if created_uid is not None else runtime.next_status_uid
        ),
    )
    return Plan2NativeReviewDynamicInstallTransition(
        runtime,
        after,
        program,
        execution_input,
        True,
        True,
        merged_uid,
        created_uid,
        (),
        trace,
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeReviewDynamicEventInput:
    source_guid: str
    play_origin: str = "normal"
    completed_effect_ids: tuple[str, ...] = ()
    accepted: bool | None = True
    context_present: bool | None = True
    status_addition_blocked: bool | None = False
    difference_list_available: bool | None = True
    deck_search_count: int | None = None
    grave_search_count: int | None = None
    review_additive_fix: int | None = 0

    def __post_init__(self) -> None:
        _text(self.source_guid, "event.source_guid")
        if self.play_origin not in PLAY_ORIGINS:
            raise Plan2NativeReviewDynamicError("play-origin-unbound", self.play_origin)
        if type(self.completed_effect_ids) is not tuple or any(
            type(item) is not str or not item for item in self.completed_effect_ids
        ):
            raise Plan2NativeReviewDynamicError("completed-order-shape")
        for name in ("accepted", "context_present", "status_addition_blocked", "difference_list_available"):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise Plan2NativeReviewDynamicError("snapshot-shape", name)
        for name in ("deck_search_count", "grave_search_count"):
            value = getattr(self, name)
            if value is not None:
                _nonnegative(value, f"event.{name}")
        if self.review_additive_fix is not None:
            _i32(self.review_additive_fix, "event.review_additive_fix")


@dataclass(frozen=True, slots=True)
class Plan2NativeReviewDynamicStatusHandoff:
    review_before: int
    review_after: int
    status_present_before: bool
    status_present_after: bool
    status_passing_before: bool
    status_passing_after: bool
    delta: int
    committed: bool
    difference_emitted: bool
    difference_type_value: int | None


@dataclass(frozen=True, slots=True)
class Plan2NativeReviewDynamicEventTransition:
    before: Plan2NativeReviewDynamicRuntime
    after: Plan2NativeReviewDynamicRuntime
    program: Plan2NativeReviewDynamicProgram
    execution_input: Plan2NativeReviewDynamicEventInput
    executable: bool
    search_count: int
    search_count_value: int
    additive_fix: int
    additive_multiple: float
    review_at_execution: int
    review_delta: int
    handoff: Plan2NativeReviewDynamicStatusHandoff | None
    unresolved: tuple[str, ...]
    trace: tuple[str, ...]


def execute_plan2_native_review_dynamic_event(
    runtime: Plan2NativeReviewDynamicRuntime,
    program: Plan2NativeReviewDynamicProgram,
    execution_input: Plan2NativeReviewDynamicEventInput,
) -> Plan2NativeReviewDynamicEventTransition:
    """Execute ReviewValueMultiple or ReviewPerSearchCount exactly once."""

    def unresolved(*codes: str) -> Plan2NativeReviewDynamicEventTransition:
        return Plan2NativeReviewDynamicEventTransition(
            runtime,
            runtime,
            program,
            execution_input,
            False,
            0,
            0,
            0,
            f32(1.0),
            runtime.review,
            0,
            None,
            tuple(codes),
            (),
        )

    if program.operation_kind not in {OP_REVIEW_VALUE_MULTIPLE, OP_REVIEW_PER_SEARCH_COUNT}:
        return unresolved("not-review-event")
    if execution_input.completed_effect_ids != program.prior_effect_ids:
        return unresolved("effect-order-unresolved")
    common_unknown = tuple(
        f"{name}-unknown"
        for name in ("accepted", "context_present", "status_addition_blocked", "difference_list_available")
        if getattr(execution_input, name) is None
    )
    if common_unknown:
        return unresolved(*common_unknown)
    if not execution_input.accepted:
        return unresolved("card-not-accepted")
    if not execution_input.context_present:
        return unresolved("context-missing")

    search_count = 0
    search_value = 0
    additive_fix = 0
    additive_multiple = f32(1.0)
    trace: list[str] = ["read-review-at-execution"]
    if program.operation_kind == OP_REVIEW_VALUE_MULTIPLE:
        if runtime.review == 0:
            return Plan2NativeReviewDynamicEventTransition(
                runtime,
                runtime,
                program,
                execution_input,
                True,
                0,
                0,
                0,
                additive_multiple,
                0,
                0,
                None,
                (),
                ("read-review-at-execution", "zero-review-fast-return"),
            )
        delta = _ceil(
            f32(f32(runtime.review) * permille_to_f32(program.value1)),
            "ReviewValueMultiple.delta",
        )
        trace.extend(("binary32-permille", "ceil-delta", "try-add-review-fix"))
    else:
        unknown = tuple(
            f"{name}-unknown"
            for name in ("deck_search_count", "grave_search_count", "review_additive_fix")
            if getattr(execution_input, name) is None
        )
        if unknown:
            return unresolved(*unknown)
        assert execution_input.deck_search_count is not None
        assert execution_input.grave_search_count is not None
        assert execution_input.review_additive_fix is not None
        search_count = execution_input.deck_search_count + execution_input.grave_search_count
        if search_count > INT32_MAX:
            return unresolved("search-count-domain-unresolved")
        search_value = _ceil(
            f32(permille_to_f32(program.value2) * f32(search_count)),
            "ReviewPerSearchCount.search-value",
        )
        additive_fix = execution_input.review_additive_fix
        for layer in runtime.layers:
            if layer.kind == LAYER_REVIEW_ADDITIVE and (
                not layer.is_turn_limited or layer.turn > 0
            ):
                additive_multiple = f32(
                    additive_multiple + permille_to_f32(layer.permille)
                )
        summed = _i32_wrap(program.value1 + search_value)
        summed = _i32_wrap(summed + additive_fix)
        delta = _ceil(
            f32(f32(summed) * additive_multiple),
            "ReviewPerSearchCount.delta",
        )
        trace.extend(
            (
                "snapshot-deck-then-grave-count",
                "binary32-permille",
                "ceil-search-value",
                "i32-add-fix",
                "ordered-review-additive-layers",
                "ceil-review-delta",
            )
        )

    if execution_input.status_addition_blocked:
        return Plan2NativeReviewDynamicEventTransition(
            runtime,
            runtime,
            program,
            execution_input,
            True,
            search_count,
            search_value,
            additive_fix,
            additive_multiple,
            runtime.review,
            0,
            None,
            (),
            tuple(trace + ["try-add-status-blocked"]),
        )
    review_after = _i32_wrap(runtime.review + delta)
    if review_after < 0:
        return unresolved("review-result-domain-unresolved")
    status_passing_after = (
        runtime.review_passing_turn_start if runtime.review_status_present else False
    )
    after = replace(
        runtime,
        review=review_after,
        review_status_present=True,
        review_passing_turn_start=status_passing_after,
    )
    emitted = bool(execution_input.difference_list_available)
    handoff = Plan2NativeReviewDynamicStatusHandoff(
        runtime.review,
        review_after,
        runtime.review_status_present,
        True,
        runtime.review_passing_turn_start,
        status_passing_after,
        delta,
        True,
        emitted,
        31 if emitted else None,
    )
    return Plan2NativeReviewDynamicEventTransition(
        runtime,
        after,
        program,
        execution_input,
        True,
        search_count,
        search_value,
        additive_fix,
        additive_multiple,
        runtime.review,
        delta,
        handoff,
        (),
        tuple(
            trace
            + ["commit-review"]
            + (["emit-difference-type-31"] if emitted else ["difference-list-unavailable"])
        ),
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeReviewDynamicTurnTransition:
    before: Plan2NativeReviewDynamicRuntime
    after: Plan2NativeReviewDynamicRuntime
    expired_status_uids: tuple[int, ...]
    review_spent: bool
    review_expired: bool


def start_plan2_native_review_dynamic_turn(
    runtime: Plan2NativeReviewDynamicRuntime,
) -> Plan2NativeReviewDynamicTurnTransition:
    """Spend only passing finite statuses, expire, then mark survivors passing."""

    survivors: list[Plan2NativeReviewDynamicLayer] = []
    expired: list[int] = []
    for layer in runtime.layers:
        current = layer
        if layer.is_turn_limited and layer.passing_turn_start:
            current = replace(layer, turn=_i32_wrap(layer.turn - 1))
        if current.is_turn_limited and current.turn <= 0:
            expired.append(current.status_uid)
            continue
        survivors.append(replace(current, passing_turn_start=True))

    review = runtime.review
    present = runtime.review_status_present
    spent = present and runtime.review_passing_turn_start
    if spent:
        review = max(0, review - 1)
        present = review > 0
    review_expired = runtime.review_status_present and not present
    after = replace(
        runtime,
        review=review,
        review_status_present=present,
        review_passing_turn_start=present,
        layers=tuple(survivors),
    )
    return Plan2NativeReviewDynamicTurnTransition(
        runtime, after, tuple(expired), spent, review_expired
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeReviewDynamicLessonRequest:
    value: int
    count: int
    aggressive_at_execution: int | None
    adding_status: AddingParameterStatus
    settings: AddingParameterSettings
    application_status: ParameterApplicationStatus
    play_origin: str = "normal"
    snapshot_known: bool | None = True
    is_buff_active: bool = True
    additional: AddingParameterAdditionalData | None = None

    def __post_init__(self) -> None:
        _i32(self.value, "lesson.value")
        _nonnegative(self.count, "lesson.count")
        if self.aggressive_at_execution is not None:
            _i32(self.aggressive_at_execution, "lesson.aggressive")
        if self.play_origin not in PLAY_ORIGINS:
            raise Plan2NativeReviewDynamicError("play-origin-unbound", self.play_origin)
        if self.snapshot_known is not None and type(self.snapshot_known) is not bool:
            raise Plan2NativeReviewDynamicError("snapshot-shape", "lesson")
        if type(self.is_buff_active) is not bool:
            raise Plan2NativeReviewDynamicError("buff-flag-shape")


Plan2NativeReviewDynamicLessonHit: TypeAlias = _nia.LessonHitMutation


@dataclass(frozen=True, slots=True)
class Plan2NativeReviewDynamicLessonHandoff:
    runtime: Plan2NativeReviewDynamicRuntime
    request: Plan2NativeReviewDynamicLessonRequest
    executable: bool
    review_at_execution: int
    aggressive_at_execution: int | None
    minimum_at_execution: int | None
    lesson_multiplier: float
    dependent_active: bool
    hits: tuple[Plan2NativeReviewDynamicLessonHit, ...]
    final_application_status: ParameterApplicationStatus
    unresolved: tuple[str, ...]
    trace: tuple[str, ...]


def handoff_plan2_native_review_dynamic_lesson(
    runtime: Plan2NativeReviewDynamicRuntime,
    request: Plan2NativeReviewDynamicLessonRequest,
) -> Plan2NativeReviewDynamicLessonHandoff:
    """Handoff one Lesson loop using execution-time Review/Aggressive getters."""

    multiplier_state = _nia.LessonParameterMultipleState(
        tuple(
            _nia.LessonParameterMultipleStatus(
                layer.permille,
                layer.turn,
                layer.passing_turn_start,
                layer.is_turn_limited,
            )
            for layer in runtime.layers
            if layer.kind == LAYER_LESSON_MULTIPLE
        )
    )
    lesson_multiple_down = f32(
        request.adding_status.lesson_parameter_multiple_down
    )
    for layer in runtime.layers:
        if layer.kind == LAYER_LESSON_MULTIPLE_DOWN and (
            not layer.is_turn_limited or layer.turn > 0
        ):
            lesson_multiple_down = f32(
                lesson_multiple_down + permille_to_f32(layer.permille)
            )
    adding_status = replace(
        request.adding_status,
        lesson_parameter_multiple_down=lesson_multiple_down,
    )
    dependent_state = LessonParameterMultipleDependReviewOrAggressiveState(
        tuple(
            LessonParameterMultipleDependReviewOrAggressiveStatus(
                layer.turn,
                layer.passing_turn_start,
                layer.is_turn_limited,
            )
            for layer in runtime.layers
            if layer.kind == LAYER_LESSON_DEPENDENT
        )
    )
    if request.snapshot_known is not True or request.aggressive_at_execution is None:
        return Plan2NativeReviewDynamicLessonHandoff(
            runtime,
            request,
            False,
            runtime.review,
            request.aggressive_at_execution,
            None,
            multiplier_state.multiplier(),
            dependent_state.is_active,
            (),
            request.application_status,
            ("review-aggressive-snapshot-unknown",),
            (),
        )
    snapshot = Plan2ReviewAggressiveSnapshot(
        runtime.review, request.aggressive_at_execution
    )
    hits = apply_plan2_lesson_multiple_depend_review_or_aggressive_hits(
        request.value,
        request.count,
        multiplier_state=multiplier_state,
        dependent_state=dependent_state,
        review_aggressive=snapshot,
        adding_status=adding_status,
        settings=request.settings,
        application_status=request.application_status,
        is_buff_active=request.is_buff_active,
        additional=request.additional,
    )
    final_status = (
        request.application_status if not hits else hits[-1].next_application_status
    )
    return Plan2NativeReviewDynamicLessonHandoff(
        runtime,
        request,
        True,
        runtime.review,
        request.aggressive_at_execution,
        snapshot.minimum,
        multiplier_state.multiplier(),
        dependent_state.is_active,
        hits,
        final_status,
        (),
        (
            "snapshot-current-review-aggressive",
            "min-signed-review-aggressive",
            "binary32-permille-and-max",
            "per-hit-calculate-then-add",
        ),
    )


execute_plan2_native_review_dynamic = execute_plan2_native_review_dynamic_event
resolve_plan2_native_review_dynamic_lesson = handoff_plan2_native_review_dynamic_lesson
