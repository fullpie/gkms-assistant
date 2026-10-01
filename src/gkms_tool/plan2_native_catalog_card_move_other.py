"""Bounded standalone Plan2 catalog adapter for the four remaining CardMoves.

The native program catalog currently reports four CardMove versions with the
same ``exact-adapter-shape-unsupported`` detail.  They are the four upgrades
of ``p_card-02-ido-3_198``.  This module owns only that target effect slot.  It
does not import or call the central program catalog, horizon, core runtime,
formal coverage, GUI, or controller.

The target is an exact DeckAll search followed by a DeckFirst move.  The
extended ordered-source and GUID rematch primitive lives in
``plan2_card_move_remaining``; this module compiles the target-family Master
rows and exposes typed catalog/handoff boundaries around that primitive.
Adjacent aggressive/stamina effects are not re-owned.  The nested Encore and
ForcePlay effects remain explicit co-blockers, so no whole-card execution is
claimed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Literal

from .card_search import ProduceCardSearchRule, load_produce_card_search
from .logic_engine import PLAN_LOGIC
from .master_db import DEFAULT_DATABASE
from .plan2_card_move import (
    CardMoveResolutionError,
    Plan2CardMoveContract,
    resolve_plan2_card_move_contract,
)
from .plan2_card_move_remaining import (
    RemainingCardMoveContract,
    RemainingCardMoveError,
    RemainingCardMoveExecutionContext,
    RemainingCardMoveHandoff,
    RemainingCardMoveResult,
    RemainingCardMoveState,
    simulate_remaining_card_move,
)


PLAN2_NATIVE_CATALOG_CARD_MOVE_OTHER_SCHEMA_VERSION = 1
ADAPTER_ID = "plan2.native.catalog.card_move_other"

CARD_ID = "p_card-02-ido-3_198"
TARGET_CARD_ID = "p_card-02-ido-3_211"
TARGET_UPGRADES = (0, 1, 2, 3)
TARGET_REFS = tuple((CARD_ID, upgrade) for upgrade in TARGET_UPGRADES)

TARGET_EFFECT_ID = (
    "e_effect-exam_card_move-p_card_search-deck_all-"
    "p_card-02-ido-3_211-deck_first-all-0_0"
)
TARGET_SEARCH_ID = "p_card_search-deck_all-p_card-02-ido-3_211"
TARGET_SLOT_INDEX = 1
TARGET_POSITION = "ProduceCardPositionType_DeckAll"
TARGET_DESTINATION = "ProduceCardMovePositionType_DeckFirst"
TARGET_PICK = "ProducePickRangeType_All"
TARGET_COUNT = (0, 0)
PLAN2 = "ProducePlanType_Plan2"
MENTAL_SKILL = "ProduceCardCategory_MentalSkill"
CARD_MOVE_EFFECT_TYPE = "ProduceExamEffectType_ExamCardMove"
UNKNOWN_EFFECT = "ProduceExamEffectType_Unknown"
UNKNOWN_PICK = "ProducePickRangeType_Unknown"
UNKNOWN_PICK_COUNT = "ProducePickCountType_Unknown"
FORCE_PLAY_EFFECT_TYPE = "ProduceExamEffectType_ExamForcePlayCardSearch"
FORCE_PLAY_EFFECT_ID = (
    "e_effect-exam_force_play_card_search-p_card_search-target_is_self-"
    "exam_status_enchant_encore-all-1_1"
)
ENCORE_EFFECT_TYPE = "ProduceExamEffectType_ExamStatusEnchantEncore"
ENCORE_EFFECT_ID = (
    "e_effect-exam_status_enchant_encore-0001-02-inf-"
    "enchant-p_card-02-ido-3_198-enc01"
)
ENCORE_STATUS_ID = "enchant-p_card-02-ido-3_198-enc01"
ENCORE_TRIGGER_ID = (
    "e_trigger-exam_card_play_after-p_card_search-target-"
    "p_card-02-ido-3_211-0_1"
)
STAMINA_EFFECT_ID = "e_effect-exam_stamina_consumption_add-01"
AGGRESSIVE_EFFECT_IDS = (
    "e_effect-exam_card_play_aggressive-0004",
    "e_effect-exam_card_play_aggressive-0005",
)
PLAY_ORIGINS = ("normal", "forced", "extra")
SOURCE_ORDER = (
    "hand",
    "deck",
    "grave",
    "lost",
    "hold",
    "playing",
    "future",
    "past",
)
CO_FAMILY_FORCE_PLAY = "force_play_search"
CO_FAMILY_ENCORE = "status_enchant_encore"


class Plan2NativeCatalogCardMoveOtherError(ValueError):
    """Fail-closed catalog, handoff, or execution error."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise Plan2NativeCatalogCardMoveOtherError("invalid-text", label)
    return value


def _nonnegative(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Plan2NativeCatalogCardMoveOtherError("invalid-nonnegative-int", label)
    return value


def _json_value(value: object, label: str) -> object:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise Plan2NativeCatalogCardMoveOtherError("invalid-master-json", label) from error
    return parsed


def _json_object(value: object, label: str) -> Mapping[str, object]:
    parsed = _json_value(value, label)
    if not isinstance(parsed, Mapping):
        raise Plan2NativeCatalogCardMoveOtherError("invalid-master-object", label)
    return parsed


def _json_list(value: object, label: str) -> tuple[object, ...]:
    parsed = _json_value(value, label)
    if not isinstance(parsed, list):
        raise Plan2NativeCatalogCardMoveOtherError("invalid-master-list", label)
    return tuple(parsed)


def _row_dict(row: Mapping[str, object] | sqlite3.Row) -> dict[str, object]:
    if isinstance(row, Mapping):
        return dict(row)
    keys = getattr(row, "keys", None)
    if callable(keys):
        return {str(key): row[key] for key in keys()}
    raise Plan2NativeCatalogCardMoveOtherError("master-row-not-mapping")


def _search_dict(search: ProduceCardSearchRule) -> dict[str, object]:
    return {
        name: getattr(search, name)
        for name in (
            "id",
            "card_rarities",
            "produce_card_ids",
            "upgrade_counts",
            "plan_type",
            "card_categories",
            "card_status_type",
            "order_type",
            "card_position_type",
            "card_search_tag",
            "produce_card_random_pool_id",
            "limit_count",
            "stamina_min_max_type",
            "stamina_min",
            "stamina_max",
            "exam_effect_type",
            "effect_group_ids",
            "is_self",
            "produce_card_pool_id",
            "cost_type",
            "is_customized",
        )
    }


def _require_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise Plan2NativeCatalogCardMoveOtherError(
            "master-contract-drift", f"{label}: expected {expected!r}, got {actual!r}"
        )


def _effect_row(
    connection: sqlite3.Connection, effect_id: str
) -> dict[str, object]:
    row = connection.execute("SELECT * FROM effect WHERE id = ?", (effect_id,)).fetchone()
    if row is None:
        raise Plan2NativeCatalogCardMoveOtherError("missing-master-effect", effect_id)
    return _row_dict(row)


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeCatalogCardMoveOtherBlocker:
    """One hard per-version compile blocker or explicit co blocker."""

    card_id: str
    upgrade: int
    slot_index: int
    code: str
    family: str
    detail: str
    effect_id: str = ""
    companion: bool = True

    def __post_init__(self) -> None:
        _text(self.card_id, "blocker.card_id")
        _nonnegative(self.upgrade, "blocker.upgrade")
        if isinstance(self.slot_index, bool) or not isinstance(self.slot_index, int):
            raise Plan2NativeCatalogCardMoveOtherError("invalid-slot-index")
        if self.slot_index < -1:
            raise Plan2NativeCatalogCardMoveOtherError("invalid-slot-index")
        _text(self.code, "blocker.code")
        _text(self.family, "blocker.family")
        _text(self.detail, "blocker.detail")
        _text(self.effect_id, "blocker.effect_id", empty=True)
        if type(self.companion) is not bool:
            raise TypeError("blocker.companion must be bool")

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def version_ref(self) -> str:
        return f"{self.card_id}#{self.upgrade}"

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "version_ref": self.version_ref,
            "slot_index": self.slot_index,
            "code": self.code,
            "family": self.family,
            "detail": self.detail,
            "effect_id": self.effect_id,
            "companion": self.companion,
        }


Plan2NativeCatalogCardMoveOtherCompanionBlocker = (
    Plan2NativeCatalogCardMoveOtherBlocker
)


@dataclass(frozen=True, slots=True)
class Plan2NativeCatalogCardMoveOtherEffectSlot:
    """One exact card.play_effects_json link in Master order."""

    slot_index: int
    effect_id: str
    effect_type: str
    trigger_id: str
    hide_icon: bool
    is_once_play_effect: bool

    def __post_init__(self) -> None:
        _nonnegative(self.slot_index, "slot_index")
        _text(self.effect_id, "effect_id")
        _text(self.effect_type, "effect_type")
        _text(self.trigger_id, "trigger_id", empty=True)
        if type(self.hide_icon) is not bool or type(self.is_once_play_effect) is not bool:
            raise TypeError("Master effect link flags must be bool")

    def to_dict(self) -> dict[str, object]:
        return {
            "slot_index": self.slot_index,
            "effect_id": self.effect_id,
            "effect_type": self.effect_type,
            "trigger_id": self.trigger_id,
            "hide_icon": self.hide_icon,
            "is_once_play_effect": self.is_once_play_effect,
        }


@dataclass(frozen=True, slots=True)
class Plan2NativeCatalogCardMoveOtherVersion:
    """Compiled target leaf for one of the four Master versions."""

    card_id: str
    upgrade: int
    plan_type: str
    category: str
    move_position_type: str
    effect_slots: tuple[Plan2NativeCatalogCardMoveOtherEffectSlot, ...]
    target_slot_index: int
    target_contract: Plan2CardMoveContract
    remaining_contract: RemainingCardMoveContract
    companion_blockers: tuple[Plan2NativeCatalogCardMoveOtherBlocker, ...]

    def __post_init__(self) -> None:
        _text(self.card_id, "version.card_id")
        _nonnegative(self.upgrade, "version.upgrade")
        _require_equal(self.plan_type, PLAN2, "version.plan_type")
        _require_equal(self.category, MENTAL_SKILL, "version.category")
        _require_equal(
            self.move_position_type,
            "ProduceCardMovePositionType_Lost",
            "version.move_position_type",
        )
        slots = tuple(self.effect_slots)
        if tuple(slot.slot_index for slot in slots) != tuple(range(len(slots))):
            raise Plan2NativeCatalogCardMoveOtherError("card-effect-order", self.version_ref)
        if len(slots) != 4:
            raise Plan2NativeCatalogCardMoveOtherError("card-effect-count", self.version_ref)
        object.__setattr__(self, "effect_slots", slots)
        _nonnegative(self.target_slot_index, "version.target_slot_index")
        if self.target_slot_index >= len(slots):
            raise Plan2NativeCatalogCardMoveOtherError("target-slot-range", self.version_ref)
        if slots[self.target_slot_index].effect_id != TARGET_EFFECT_ID:
            raise Plan2NativeCatalogCardMoveOtherError("target-slot-id", self.version_ref)
        if not isinstance(self.target_contract, Plan2CardMoveContract):
            raise TypeError("target_contract must be Plan2CardMoveContract")
        if not isinstance(self.remaining_contract, RemainingCardMoveContract):
            raise TypeError("remaining_contract must be RemainingCardMoveContract")
        if self.target_contract.effect_id != TARGET_EFFECT_ID:
            raise Plan2NativeCatalogCardMoveOtherError("target-contract-id", self.version_ref)
        if self.remaining_contract.version_ref != self.version_ref:
            raise Plan2NativeCatalogCardMoveOtherError(
                "remaining-contract-version", self.version_ref
            )
        if self.remaining_contract.effect_id != TARGET_EFFECT_ID:
            raise Plan2NativeCatalogCardMoveOtherError(
                "remaining-contract-effect", self.version_ref
            )
        blockers = tuple(self.companion_blockers)
        if any(
            not isinstance(item, Plan2NativeCatalogCardMoveOtherBlocker)
            or item.ref != self.ref
            or not item.companion
            for item in blockers
        ):
            raise TypeError("companion_blockers must contain same-version typed blockers")
        object.__setattr__(self, "companion_blockers", blockers)

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def version_ref(self) -> str:
        return f"{self.card_id}#{self.upgrade}"

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return tuple(slot.effect_id for slot in self.effect_slots)

    @property
    def target_effect_id(self) -> str:
        return self.target_contract.effect_id

    @property
    def target_effect_type(self) -> str:
        return self.target_contract.effect_type

    @property
    def search_id(self) -> str:
        return self.target_contract.search_id

    @property
    def source_position(self) -> str:
        return self.remaining_contract.search.card_position_type

    @property
    def destination(self) -> str:
        return self.target_contract.destination

    @property
    def pick_range_type(self) -> str:
        return self.target_contract.pick_range_type

    @property
    def count_range(self) -> tuple[int, int]:
        return self.target_contract.count_range

    @property
    def supported_play_origins(self) -> tuple[str, ...]:
        return PLAY_ORIGINS

    @property
    def target_effect_executable(self) -> bool:
        return True

    @property
    def direct_effect_executable(self) -> bool:
        return self.target_effect_executable

    @property
    def executable(self) -> bool:
        return self.direct_effect_executable

    @property
    def whole_card_executable(self) -> bool:
        return self.target_effect_executable and not self.companion_blockers

    @property
    def co_blocked(self) -> bool:
        return bool(self.companion_blockers)

    @property
    def companion_blocker_families(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.family for item in self.companion_blockers))

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "version_ref": self.version_ref,
            "plan_type": self.plan_type,
            "category": self.category,
            "move_position_type": self.move_position_type,
            "target_slot_index": self.target_slot_index,
            "target_effect_id": self.target_effect_id,
            "target_effect_type": self.target_effect_type,
            "search_id": self.search_id,
            "source_position": self.source_position,
            "destination": self.destination,
            "pick_range_type": self.pick_range_type,
            "count_range": list(self.count_range),
            "source_order": list(SOURCE_ORDER),
            "ordered_effect_ids": list(self.ordered_effect_ids),
            "effect_slots": [slot.to_dict() for slot in self.effect_slots],
            "target_contract": self.target_contract.to_dict(),
            "search": _search_dict(self.remaining_contract.search),
            "supported_play_origins": list(self.supported_play_origins),
            "target_effect_executable": self.target_effect_executable,
            "direct_effect_executable": self.direct_effect_executable,
            "whole_card_executable": self.whole_card_executable,
            "companion_blockers": [item.to_dict() for item in self.companion_blockers],
        }


Plan2NativeCatalogCardMoveOtherProgram = Plan2NativeCatalogCardMoveOtherVersion


@dataclass(frozen=True, slots=True)
class Plan2NativeCatalogCardMoveOtherCatalog:
    """Immutable partial catalog; failed versions are never emitted as programs."""

    database: str
    programs: tuple[Plan2NativeCatalogCardMoveOtherVersion, ...]

    def __post_init__(self) -> None:
        _text(self.database, "catalog.database")
        programs = tuple(self.programs)
        if any(
            not isinstance(item, Plan2NativeCatalogCardMoveOtherVersion)
            for item in programs
        ):
            raise TypeError("catalog.programs must contain typed versions")
        refs = tuple(item.ref for item in programs)
        if refs != tuple(sorted(refs)) or len(refs) != len(set(refs)):
            raise Plan2NativeCatalogCardMoveOtherError("catalog-version-order")
        object.__setattr__(self, "programs", programs)

    @property
    def affected_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(item.ref for item in self.programs)

    @property
    def executable_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(item.ref for item in self.programs if item.executable)

    @property
    def target_effect_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(item.ref for item in self.programs if item.target_effect_executable)

    @property
    def direct_effect_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(item.ref for item in self.programs if item.direct_effect_executable)

    @property
    def companion_blockers(self) -> tuple[Plan2NativeCatalogCardMoveOtherBlocker, ...]:
        return tuple(item for program in self.programs for item in program.companion_blockers)

    @property
    def companion_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(item.ref for item in self.programs if item.co_blocked)

    @property
    def co_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return self.companion_blocked_refs

    @property
    def whole_card_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(item.ref for item in self.programs if item.whole_card_executable)

    @property
    def companion_blocker_code_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.code for item in self.companion_blockers).items()))

    @property
    def companion_family_version_counts(self) -> dict[str, int]:
        refs: dict[str, set[tuple[str, int]]] = {}
        for item in self.companion_blockers:
            refs.setdefault(item.family, set()).add(item.ref)
        return {family: len(values) for family, values in sorted(refs.items())}

    def get(self, card_id: str, upgrade: int) -> Plan2NativeCatalogCardMoveOtherVersion | None:
        return next((item for item in self.programs if item.ref == (card_id, upgrade)), None)

    def resolve(self, card_id: str, upgrade: int) -> Plan2NativeCatalogCardMoveOtherVersion:
        program = self.get(card_id, upgrade)
        if program is None:
            raise KeyError(f"uncompiled CardMove-other version: {card_id}#{upgrade}")
        return program

    version = resolve

    def to_dict(self) -> dict[str, object]:
        return {
            "adapter_id": ADAPTER_ID,
            "schema_version": PLAN2_NATIVE_CATALOG_CARD_MOVE_OTHER_SCHEMA_VERSION,
            "database": self.database,
            "compiled_version_count": len(self.programs),
            "target_effect_executable_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.target_effect_executable_refs
            ],
            "direct_effect_executable_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.direct_effect_executable_refs
            ],
            "companion_blocked_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.companion_blocked_refs
            ],
            "whole_card_executable_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.whole_card_executable_refs
            ],
            "companion_blocker_code_counts": self.companion_blocker_code_counts,
            "companion_family_version_counts": self.companion_family_version_counts,
            "programs": [item.to_dict() for item in self.programs],
        }


@dataclass(frozen=True, slots=True)
class Plan2NativeCatalogCardMoveOtherCompilation:
    """Per-version compile result with hard blockers and target/co accounting."""

    schema_version: int
    database: str
    affected_refs: tuple[tuple[str, int], ...]
    occurrence_count: int
    catalog: Plan2NativeCatalogCardMoveOtherCatalog
    blockers: tuple[Plan2NativeCatalogCardMoveOtherBlocker, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != PLAN2_NATIVE_CATALOG_CARD_MOVE_OTHER_SCHEMA_VERSION:
            raise Plan2NativeCatalogCardMoveOtherError("unsupported-schema-version")
        _text(self.database, "compilation.database")
        refs = tuple(self.affected_refs)
        if refs != tuple(sorted(refs)) or len(refs) != len(set(refs)):
            raise Plan2NativeCatalogCardMoveOtherError("affected-ref-order")
        _nonnegative(self.occurrence_count, "compilation.occurrence_count")
        if self.occurrence_count != len(refs):
            raise Plan2NativeCatalogCardMoveOtherError("occurrence-ref-mismatch")
        if not isinstance(self.catalog, Plan2NativeCatalogCardMoveOtherCatalog):
            raise TypeError("compilation.catalog must be typed")
        if any(item.ref not in refs for item in self.catalog.programs):
            raise Plan2NativeCatalogCardMoveOtherError("compiled-ref-outside-target")
        blockers = tuple(self.blockers)
        if any(
            not isinstance(item, Plan2NativeCatalogCardMoveOtherBlocker)
            for item in blockers
        ):
            raise TypeError("compilation.blockers must be typed")
        if any(item.companion for item in blockers):
            raise Plan2NativeCatalogCardMoveOtherError("hard-blocker-marked-companion")
        object.__setattr__(self, "affected_refs", refs)
        object.__setattr__(self, "blockers", blockers)

    @property
    def compiled_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.affected_refs

    @property
    def compiled_version_count(self) -> int:
        return len(self.compiled_refs)

    @property
    def failed_refs(self) -> tuple[tuple[str, int], ...]:
        compiled = set(self.compiled_refs)
        return tuple(ref for ref in self.affected_refs if ref not in compiled)

    @property
    def failed_version_count(self) -> int:
        return len(self.failed_refs)

    @property
    def failed_occurrence_count(self) -> int:
        return self.occurrence_count - self.compiled_version_count

    @property
    def fully_compiled(self) -> bool:
        return not self.blockers and not self.failed_refs

    @property
    def target_effect_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.target_effect_executable_refs

    @property
    def direct_effect_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.direct_effect_executable_refs

    @property
    def executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.executable_refs

    @property
    def target_effect_executable(self) -> bool:
        return self.fully_compiled and self.target_effect_executable_refs == self.affected_refs

    @property
    def direct_effect_executable(self) -> bool:
        return self.fully_compiled and self.direct_effect_executable_refs == self.affected_refs

    @property
    def companion_blockers(self) -> tuple[Plan2NativeCatalogCardMoveOtherBlocker, ...]:
        return self.catalog.companion_blockers

    @property
    def companion_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.companion_blocked_refs

    @property
    def co_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return self.companion_blocked_refs

    @property
    def whole_card_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.whole_card_executable_refs

    @property
    def blocker_code_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.code for item in self.blockers).items()))

    @property
    def companion_blocker_code_counts(self) -> dict[str, int]:
        return self.catalog.companion_blocker_code_counts

    @property
    def companion_family_version_counts(self) -> dict[str, int]:
        return self.catalog.companion_family_version_counts

    @property
    def target_direct_co_accounting(self) -> dict[str, object]:
        return {
            "affected_versions": len(self.affected_refs),
            "target_effect_executable_versions": len(self.target_effect_executable_refs),
            "direct_effect_executable_versions": len(self.direct_effect_executable_refs),
            "co_blocked_versions": len(self.co_blocked_refs),
            "whole_card_executable_versions": len(self.whole_card_executable_refs),
            "hard_failed_versions": self.failed_version_count,
            "companion_blocker_occurrences": len(self.companion_blockers),
        }

    def blockers_for(
        self, ref: tuple[str, int]
    ) -> tuple[Plan2NativeCatalogCardMoveOtherBlocker, ...]:
        return tuple(item for item in self.blockers if item.ref == ref)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "adapter_id": ADAPTER_ID,
            "database": self.database,
            "affected_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.affected_refs
            ],
            "occurrence_count": self.occurrence_count,
            "compiled_version_count": self.compiled_version_count,
            "failed_version_count": self.failed_version_count,
            "failed_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.failed_refs
            ],
            "fully_compiled": self.fully_compiled,
            "target_effect_executable": self.target_effect_executable,
            "direct_effect_executable": self.direct_effect_executable,
            "target_direct_co_accounting": self.target_direct_co_accounting,
            "blocker_code_counts": self.blocker_code_counts,
            "blockers": [item.to_dict() for item in self.blockers],
            "companion_blockers": [item.to_dict() for item in self.companion_blockers],
            "catalog": self.catalog.to_dict(),
        }


Plan2NativeCatalogCardMoveOtherCatalogCompilation = Plan2NativeCatalogCardMoveOtherCompilation


@dataclass(frozen=True, slots=True)
class Plan2NativeCatalogCardMoveOtherHandoffRow:
    """Immutable target-only handoff row."""

    version_ref: str
    card_id: str
    upgrade: int
    target_effect_id: str
    target_effect_type: str
    target_slot_index: int
    search_id: str
    source_position: str
    destination: str
    pick_range_type: str
    count_min: int
    count_max: int
    source_order: tuple[str, ...]
    ordered_effect_ids: tuple[str, ...]
    supported_play_origins: tuple[str, ...]
    target_effect_executable: bool
    direct_effect_executable: bool
    whole_card_executable: bool
    companion_blockers: tuple[Plan2NativeCatalogCardMoveOtherBlocker, ...]

    def __post_init__(self) -> None:
        _text(self.version_ref, "handoff.version_ref")
        _text(self.card_id, "handoff.card_id")
        _nonnegative(self.upgrade, "handoff.upgrade")
        _require_equal(self.version_ref, f"{self.card_id}#{self.upgrade}", "handoff.version_ref")
        _require_equal(self.target_effect_id, TARGET_EFFECT_ID, "handoff.target_effect_id")
        _require_equal(self.target_effect_type, CARD_MOVE_EFFECT_TYPE, "handoff.target_effect_type")
        _require_equal(self.target_slot_index, TARGET_SLOT_INDEX, "handoff.target_slot_index")
        _require_equal(self.search_id, TARGET_SEARCH_ID, "handoff.search_id")
        _require_equal(self.source_position, TARGET_POSITION, "handoff.source_position")
        _require_equal(self.destination, TARGET_DESTINATION, "handoff.destination")
        _require_equal(self.pick_range_type, TARGET_PICK, "handoff.pick_range_type")
        _require_equal((self.count_min, self.count_max), TARGET_COUNT, "handoff.count")
        if tuple(self.source_order) != SOURCE_ORDER:
            raise Plan2NativeCatalogCardMoveOtherError("handoff-source-order")
        ordered = tuple(self.ordered_effect_ids)
        if len(ordered) != 4 or ordered[TARGET_SLOT_INDEX] != TARGET_EFFECT_ID:
            raise Plan2NativeCatalogCardMoveOtherError("handoff-target-slot")
        object.__setattr__(self, "ordered_effect_ids", ordered)
        origins = tuple(self.supported_play_origins)
        if origins != PLAY_ORIGINS:
            raise Plan2NativeCatalogCardMoveOtherError("handoff-play-origins")
        object.__setattr__(self, "supported_play_origins", origins)
        if type(self.target_effect_executable) is not bool or type(self.direct_effect_executable) is not bool:
            raise TypeError("handoff executable flags must be bool")
        if type(self.whole_card_executable) is not bool:
            raise TypeError("handoff.whole_card_executable must be bool")
        blockers = tuple(self.companion_blockers)
        if any(
            not isinstance(item, Plan2NativeCatalogCardMoveOtherBlocker)
            or item.ref != self.ref
            or not item.companion
            for item in blockers
        ):
            raise TypeError("handoff.companion_blockers must contain same-version typed blockers")
        if self.whole_card_executable and blockers:
            raise Plan2NativeCatalogCardMoveOtherError("handoff-whole-card-blocked")
        object.__setattr__(self, "companion_blockers", blockers)

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def co_blocked(self) -> bool:
        return bool(self.companion_blockers)

    def to_dict(self) -> dict[str, object]:
        return {
            "version_ref": self.version_ref,
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "target_effect_id": self.target_effect_id,
            "target_effect_type": self.target_effect_type,
            "target_slot_index": self.target_slot_index,
            "search_id": self.search_id,
            "source_position": self.source_position,
            "destination": self.destination,
            "pick_range_type": self.pick_range_type,
            "count": [self.count_min, self.count_max],
            "source_order": list(self.source_order),
            "ordered_effect_ids": list(self.ordered_effect_ids),
            "supported_play_origins": list(self.supported_play_origins),
            "target_effect_executable": self.target_effect_executable,
            "direct_effect_executable": self.direct_effect_executable,
            "whole_card_executable": self.whole_card_executable,
            "companion_blockers": [item.to_dict() for item in self.companion_blockers],
        }


@dataclass(frozen=True, slots=True)
class Plan2NativeCatalogCardMoveOtherHandoff:
    """Immutable aggregate handoff; it never calls a central runtime."""

    adapter_id: str
    schema_version: int
    affected_refs: tuple[tuple[str, int], ...]
    executable_refs: tuple[tuple[str, int], ...]
    rows: tuple[Plan2NativeCatalogCardMoveOtherHandoffRow, ...]

    def __post_init__(self) -> None:
        _require_equal(self.adapter_id, ADAPTER_ID, "handoff.adapter_id")
        _require_equal(
            self.schema_version,
            PLAN2_NATIVE_CATALOG_CARD_MOVE_OTHER_SCHEMA_VERSION,
            "handoff.schema_version",
        )
        affected = tuple(self.affected_refs)
        executable = tuple(self.executable_refs)
        rows = tuple(self.rows)
        if any(not isinstance(row, Plan2NativeCatalogCardMoveOtherHandoffRow) for row in rows):
            raise TypeError("handoff.rows must be typed")
        if affected != tuple(sorted(affected)) or len(affected) != len(set(affected)):
            raise Plan2NativeCatalogCardMoveOtherError("handoff-affected-order")
        if executable != tuple(sorted(executable)) or set(executable) - set(affected):
            raise Plan2NativeCatalogCardMoveOtherError("handoff-executable-order")
        if tuple(row.ref for row in rows) != executable:
            raise Plan2NativeCatalogCardMoveOtherError("handoff-row-order")
        object.__setattr__(self, "affected_refs", affected)
        object.__setattr__(self, "executable_refs", executable)
        object.__setattr__(self, "rows", rows)

    @property
    def affected_version_count(self) -> int:
        return len(self.affected_refs)

    @property
    def executable_version_count(self) -> int:
        return len(self.executable_refs)

    @property
    def target_effect_executable(self) -> bool:
        return self.executable_refs == self.affected_refs == TARGET_REFS

    @property
    def direct_effect_executable(self) -> bool:
        return self.target_effect_executable

    @property
    def companion_blockers(self) -> tuple[Plan2NativeCatalogCardMoveOtherBlocker, ...]:
        return tuple(item for row in self.rows for item in row.companion_blockers)

    @property
    def companion_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(row.ref for row in self.rows if row.co_blocked)

    @property
    def co_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return self.companion_blocked_refs

    @property
    def whole_card_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(row.ref for row in self.rows if row.whole_card_executable)

    @property
    def companion_blocker_code_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.code for item in self.companion_blockers).items()))

    def to_dict(self) -> dict[str, object]:
        return {
            "adapter_id": self.adapter_id,
            "schema_version": self.schema_version,
            "affected_version_count": self.affected_version_count,
            "executable_version_count": self.executable_version_count,
            "target_effect_executable": self.target_effect_executable,
            "direct_effect_executable": self.direct_effect_executable,
            "affected_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.affected_refs
            ],
            "executable_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.executable_refs
            ],
            "companion_blocked_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.companion_blocked_refs
            ],
            "whole_card_executable_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.whole_card_executable_refs
            ],
            "companion_blocker_code_counts": self.companion_blocker_code_counts,
            "rows": [row.to_dict() for row in self.rows],
        }


@dataclass(frozen=True, slots=True)
class Plan2NativeCatalogCardMoveOtherExecution:
    """Immutable wrapper around the exact two-phase CardMove result."""

    program: Plan2NativeCatalogCardMoveOtherVersion
    handoff: RemainingCardMoveHandoff
    native: RemainingCardMoveResult

    def __post_init__(self) -> None:
        if not isinstance(self.program, Plan2NativeCatalogCardMoveOtherVersion):
            raise TypeError("execution.program must be typed")
        if not isinstance(self.handoff, RemainingCardMoveHandoff):
            raise TypeError("execution.handoff must be typed")
        if not isinstance(self.native, RemainingCardMoveResult):
            raise TypeError("execution.native must be typed")

    @property
    def before(self) -> RemainingCardMoveState:
        return self.native.before

    @property
    def after(self) -> RemainingCardMoveState:
        return self.native.after

    @property
    def candidates(self):
        return self.native.snapshot.candidates

    @property
    def selected(self):
        return self.native.snapshot.selected

    @property
    def selected_guids(self) -> tuple[str, ...]:
        return self.native.snapshot.selected_guids

    @property
    def rematched_guids(self) -> tuple[str, ...]:
        return self.native.moved_guids

    @property
    def skipped_guids(self) -> tuple[str, ...]:
        return self.native.skipped_guids

    @property
    def random_state_before(self) -> int:
        return self.native.snapshot.random_state_before

    @property
    def random_state_after(self) -> int:
        return self.native.snapshot.random_state_after

    @property
    def callbacks(self) -> tuple[str, ...]:
        return self.native.callbacks

    @property
    def phases(self) -> tuple[str, ...]:
        return self.native.phases

    def to_dict(self) -> dict[str, object]:
        return {
            "version_ref": self.program.version_ref,
            "handoff": {
                "kind": self.handoff.kind,
                "parent_id": self.handoff.parent_id,
                "phase": self.handoff.phase,
                "effect_index": self.handoff.effect_index,
                "play_origin": self.handoff.play_origin,
                "queue_dispatch_count": self.handoff.queue_dispatch_count,
            },
            "candidates": [item.guid for item in self.candidates],
            "selected_guids": list(self.selected_guids),
            "rematched_guids": list(self.rematched_guids),
            "skipped_guids": list(self.skipped_guids),
            "random_state_before": self.random_state_before,
            "random_state_after": self.random_state_after,
            "callbacks": list(self.callbacks),
            "phases": list(self.phases),
        }


def _validate_target_contract(
    contract: Plan2CardMoveContract, search: ProduceCardSearchRule
) -> None:
    expected = {
        "effect_id": TARGET_EFFECT_ID,
        "search_id": TARGET_SEARCH_ID,
        "destination": TARGET_DESTINATION,
        "pick_range_type": TARGET_PICK,
        "count_min": 0,
        "count_max": 0,
        "target_card_id": "",
        "target_upgrade": 0,
        "target_effect_type": UNKNOWN_EFFECT,
        "pick_count_type": UNKNOWN_PICK_COUNT,
        "pick_count_reference_search_id": "",
        "search2_id": "",
        "second_pick_range_type": UNKNOWN_PICK,
        "second_count_min": 0,
        "second_count_max": 0,
        "second_pick_count_type": UNKNOWN_PICK_COUNT,
        "second_pick_count_reference_search_id": "",
        "card_status_enchant_id": "",
        "card_grow_effect_ids": (),
        "effect_group_ids": (),
        "chain_effect_ids": (),
    }
    for name, value in expected.items():
        _require_equal(getattr(contract, name), value, f"target.{name}")
    _require_equal(search.id, TARGET_SEARCH_ID, "target.search.id")
    expected_search = {
        "card_rarities": (),
        "produce_card_ids": (TARGET_CARD_ID,),
        "upgrade_counts": (),
        "plan_type": "ProducePlanType_Unknown",
        "card_categories": (),
        "card_status_type": "ProduceCardSearchStatusType_Unknown",
        "order_type": "ProduceCardOrderType_Unknown",
        "card_position_type": TARGET_POSITION,
        "card_search_tag": "",
        "produce_card_random_pool_id": "",
        "limit_count": 0,
        "stamina_min_max_type": "ConditionMinMaxType_Unknown",
        "stamina_min": 0,
        "stamina_max": 0,
        "exam_effect_type": UNKNOWN_EFFECT,
        "effect_group_ids": (),
        "is_self": False,
        "produce_card_pool_id": "",
        "cost_type": "ExamCostType_Unknown",
        "is_customized": False,
    }
    for name, value in expected_search.items():
        _require_equal(getattr(search, name), value, f"target.search.{name}")


def _validate_encore_companions(
    connection: sqlite3.Connection,
    encore_effect: Mapping[str, object],
) -> None:
    encore_raw = _json_object(encore_effect["raw_json"], f"effect:{ENCORE_EFFECT_ID}")
    _require_equal(
        encore_raw.get("produceExamStatusEnchantId"),
        ENCORE_STATUS_ID,
        "Encore.produceExamStatusEnchantId",
    )
    status = connection.execute(
        "SELECT * FROM produce_exam_status_enchant WHERE id = ?", (ENCORE_STATUS_ID,)
    ).fetchone()
    if status is None:
        raise Plan2NativeCatalogCardMoveOtherError("missing-encore-status", ENCORE_STATUS_ID)
    _require_equal(status["produce_exam_trigger_id"], ENCORE_TRIGGER_ID, "Encore.status.trigger")
    _require_equal(
        _json_list(status["produce_exam_effect_ids_json"], ENCORE_STATUS_ID),
        (FORCE_PLAY_EFFECT_ID,),
        "Encore.status.children",
    )
    trigger = connection.execute(
        "SELECT * FROM produce_exam_trigger WHERE id = ?", (ENCORE_TRIGGER_ID,)
    ).fetchone()
    if trigger is None:
        raise Plan2NativeCatalogCardMoveOtherError("missing-encore-trigger", ENCORE_TRIGGER_ID)
    expected_trigger = {
        "phase_types_json": ("ProduceExamPhaseType_ExamCardPlayAfter",),
        "phase_values_json": (),
        "field_status_check_types_json": (),
        "field_status_types_json": (),
        "field_status_values_json": (),
        "field_status_produce_card_search_ids_json": (),
        "produce_card_search_id": "p_card_search-target-p_card-02-ido-3_211",
        "upper_search_count": 0,
        "lower_search_count": 1,
        "card_move_position_type": "ProduceCardMovePositionType_Unknown",
        "effect_types_json": (),
        "lesson_type": "ProduceStepLessonType_Unknown",
    }
    for name, value in expected_trigger.items():
        actual = (
            _json_list(trigger[name], f"{ENCORE_TRIGGER_ID}.{name}")
            if name.endswith("_json")
            else trigger[name]
        )
        _require_equal(actual, value, f"Encore.trigger.{name}")
    force_play = _effect_row(connection, FORCE_PLAY_EFFECT_ID)
    _require_equal(force_play["effect_type"], FORCE_PLAY_EFFECT_TYPE, "ForcePlay.effect_type")


def _compile_version(
    connection: sqlite3.Connection,
    card_id: str,
    upgrade: int,
    database: Path,
) -> Plan2NativeCatalogCardMoveOtherVersion:
    row = connection.execute(
        "SELECT * FROM card WHERE id = ? AND upgrade_count = ?", (card_id, upgrade)
    ).fetchone()
    if row is None:
        raise Plan2NativeCatalogCardMoveOtherError("missing-master-card", f"{card_id}#{upgrade}")
    _require_equal(row["plan_type"], PLAN2, "card.plan_type")
    _require_equal(row["category"], MENTAL_SKILL, "card.category")
    _require_equal(row["move_position_type"], "ProduceCardMovePositionType_Lost", "card.move_position_type")
    links = _json_list(row["play_effects_json"], f"{card_id}#{upgrade}.play_effects_json")
    if len(links) != 4:
        raise Plan2NativeCatalogCardMoveOtherError("card-effect-count", f"{card_id}#{upgrade}")
    aggressive_id = AGGRESSIVE_EFFECT_IDS[0 if upgrade == 0 else 1]
    expected_ids = (aggressive_id, TARGET_EFFECT_ID, STAMINA_EFFECT_ID, ENCORE_EFFECT_ID)
    slots: list[Plan2NativeCatalogCardMoveOtherEffectSlot] = []
    effects: list[dict[str, object]] = []
    for index, link in enumerate(links):
        if not isinstance(link, Mapping):
            raise Plan2NativeCatalogCardMoveOtherError("invalid-play-effect-link", str(index))
        if set(link) != {"produceExamTriggerId", "produceExamEffectId", "hideIcon", "isOncePlayEffect"}:
            raise Plan2NativeCatalogCardMoveOtherError("play-effect-link-shape", str(index))
        if type(link["hideIcon"]) is not bool or type(link["isOncePlayEffect"]) is not bool:
            raise Plan2NativeCatalogCardMoveOtherError("play-effect-link-flags", str(index))
        _require_equal(link["produceExamEffectId"], expected_ids[index], f"card.effect[{index}].id")
        _require_equal(link["produceExamTriggerId"], "", f"card.effect[{index}].trigger")
        _require_equal(link["hideIcon"], False, f"card.effect[{index}].hideIcon")
        _require_equal(link["isOncePlayEffect"], index == 3, f"card.effect[{index}].isOncePlayEffect")
        effect = _effect_row(connection, str(link["produceExamEffectId"]))
        effects.append(effect)
        slots.append(
            Plan2NativeCatalogCardMoveOtherEffectSlot(
                index,
                str(link["produceExamEffectId"]),
                str(effect["effect_type"]),
                str(link["produceExamTriggerId"]),
                bool(link["hideIcon"]),
                bool(link["isOncePlayEffect"]),
            )
        )
    _require_equal(slots[0].effect_type, "ProduceExamEffectType_ExamCardPlayAggressive", "card.effect[0].type")
    _require_equal(slots[2].effect_type, "ProduceExamEffectType_ExamStaminaConsumptionAdd", "card.effect[2].type")
    _require_equal(slots[3].effect_type, ENCORE_EFFECT_TYPE, "card.effect[3].type")
    target_effect = effects[TARGET_SLOT_INDEX]
    target_contract = resolve_plan2_card_move_contract(target_effect, plan_type=PLAN_LOGIC)
    search = load_produce_card_search(TARGET_SEARCH_ID, database)
    _validate_target_contract(target_contract, search)
    _validate_encore_companions(connection, effects[3])
    remaining_contract = RemainingCardMoveContract(
        CARD_ID,
        upgrade,
        TARGET_EFFECT_ID,
        search,
        TARGET_DESTINATION,
        TARGET_PICK,
        0,
        0,
        "direct-effect",
        CARD_ID,
        TARGET_SLOT_INDEX,
        (FORCE_PLAY_EFFECT_TYPE, ENCORE_EFFECT_TYPE),
    )
    blockers = (
        Plan2NativeCatalogCardMoveOtherBlocker(
            CARD_ID,
            upgrade,
            -1,
            "co-effect-outside-card-move-leaf",
            CO_FAMILY_FORCE_PLAY,
            "nested Encore ForcePlay remains outside this target-only CardMove handoff",
            FORCE_PLAY_EFFECT_ID,
        ),
        Plan2NativeCatalogCardMoveOtherBlocker(
            CARD_ID,
            upgrade,
            3,
            "co-effect-outside-card-move-leaf",
            CO_FAMILY_ENCORE,
            "the adjacent once-only Encore wrapper is not executed by this CardMove leaf",
            ENCORE_EFFECT_ID,
        ),
    )
    return Plan2NativeCatalogCardMoveOtherVersion(
        CARD_ID,
        upgrade,
        str(row["plan_type"]),
        str(row["category"]),
        str(row["move_position_type"]),
        tuple(slots),
        TARGET_SLOT_INDEX,
        target_contract,
        remaining_contract,
        blockers,
    )


def _hard_blocker(ref: tuple[str, int], error: BaseException) -> Plan2NativeCatalogCardMoveOtherBlocker:
    code = getattr(error, "code", None)
    if not isinstance(code, str) or not code:
        code = "exact-adapter-shape-unsupported"
    if code in {"master-contract-drift", "card-effect-order", "card-effect-count", "target-slot-id", "play-effect-link-shape"}:
        code = "exact-adapter-shape-unsupported"
    detail = f"{type(error).__name__}: {error}"
    return Plan2NativeCatalogCardMoveOtherBlocker(
        ref[0],
        ref[1],
        TARGET_SLOT_INDEX,
        code,
        "card_move_target",
        detail,
        TARGET_EFFECT_ID,
        False,
    )


def compile_plan2_native_catalog_card_move_other(
    database: str | Path = DEFAULT_DATABASE,
) -> Plan2NativeCatalogCardMoveOtherCompilation:
    """Compile the four target rows independently and fail closed per version."""

    database_path = Path(database)
    programs: list[Plan2NativeCatalogCardMoveOtherVersion] = []
    blockers: list[Plan2NativeCatalogCardMoveOtherBlocker] = []
    if not database_path.is_file():
        blockers.extend(
            Plan2NativeCatalogCardMoveOtherBlocker(
                card_id,
                upgrade,
                TARGET_SLOT_INDEX,
                "missing-authoritative-input",
                "card_move_target",
                f"Master database is missing: {database_path}",
                TARGET_EFFECT_ID,
                False,
            )
            for card_id, upgrade in TARGET_REFS
        )
    else:
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                connection.row_factory = sqlite3.Row
                for ref in TARGET_REFS:
                    try:
                        programs.append(_compile_version(connection, *ref, database_path))
                    except (
                        CardMoveResolutionError,
                        Plan2NativeCatalogCardMoveOtherError,
                        KeyError,
                        IndexError,
                        TypeError,
                        ValueError,
                        sqlite3.Error,
                    ) as error:
                        blockers.append(_hard_blocker(ref, error))
        except (OSError, sqlite3.Error) as error:
            blockers.extend(_hard_blocker(ref, error) for ref in TARGET_REFS)
    catalog = Plan2NativeCatalogCardMoveOtherCatalog(str(database_path), tuple(programs))
    return Plan2NativeCatalogCardMoveOtherCompilation(
        PLAN2_NATIVE_CATALOG_CARD_MOVE_OTHER_SCHEMA_VERSION,
        str(database_path),
        TARGET_REFS,
        len(TARGET_REFS),
        catalog,
        tuple(blockers),
    )


compile_plan2_native_card_move_other_catalog = compile_plan2_native_catalog_card_move_other
compile_card_move_other_catalog = compile_plan2_native_catalog_card_move_other


def load_plan2_native_catalog_card_move_other(
    database: str | Path = DEFAULT_DATABASE,
) -> Plan2NativeCatalogCardMoveOtherCatalog:
    """Return a complete exact catalog or raise its first hard blocker."""

    compilation = compile_plan2_native_catalog_card_move_other(database)
    if compilation.blockers:
        first = compilation.blockers[0]
        raise Plan2NativeCatalogCardMoveOtherError(first.code, first.detail)
    return compilation.catalog


def _handoff_row(
    program: Plan2NativeCatalogCardMoveOtherVersion,
) -> Plan2NativeCatalogCardMoveOtherHandoffRow:
    return Plan2NativeCatalogCardMoveOtherHandoffRow(
        program.version_ref,
        program.card_id,
        program.upgrade,
        program.target_effect_id,
        program.target_effect_type,
        program.target_slot_index,
        program.search_id,
        program.source_position,
        program.destination,
        program.pick_range_type,
        program.count_range[0],
        program.count_range[1],
        SOURCE_ORDER,
        program.ordered_effect_ids,
        PLAY_ORIGINS,
        program.target_effect_executable,
        program.direct_effect_executable,
        program.whole_card_executable,
        program.companion_blockers,
    )


def build_plan2_native_catalog_card_move_other_handoff(
    source: Plan2NativeCatalogCardMoveOtherCompilation | Plan2NativeCatalogCardMoveOtherCatalog,
) -> Plan2NativeCatalogCardMoveOtherHandoff:
    """Build a typed handoff for compiled rows without invoking central code."""

    if isinstance(source, Plan2NativeCatalogCardMoveOtherCompilation):
        affected_refs = source.affected_refs
        catalog = source.catalog
    elif isinstance(source, Plan2NativeCatalogCardMoveOtherCatalog):
        affected_refs = source.affected_refs
        catalog = source
    else:
        raise TypeError("source must be CardMove-other compilation or catalog")
    rows = tuple(_handoff_row(program) for program in catalog.programs)
    return Plan2NativeCatalogCardMoveOtherHandoff(
        ADAPTER_ID,
        PLAN2_NATIVE_CATALOG_CARD_MOVE_OTHER_SCHEMA_VERSION,
        affected_refs,
        tuple(program.ref for program in catalog.programs),
        rows,
    )


build_plan2_native_catalog_card_move_other_central_handoff = (
    build_plan2_native_catalog_card_move_other_handoff
)
build_plan2_card_move_other_central_handoff = build_plan2_native_catalog_card_move_other_handoff


def exact_plan2_native_catalog_card_move_other_rows(
    source: Plan2NativeCatalogCardMoveOtherCompilation | Plan2NativeCatalogCardMoveOtherCatalog,
) -> tuple[dict[str, object], ...]:
    return tuple(
        row.to_dict()
        for row in build_plan2_native_catalog_card_move_other_handoff(source).rows
    )


def _validate_handoff_row(
    program: Plan2NativeCatalogCardMoveOtherVersion,
    row: Plan2NativeCatalogCardMoveOtherHandoffRow | None,
) -> None:
    if row is None:
        return
    expected = _handoff_row(program)
    if row != expected:
        raise Plan2NativeCatalogCardMoveOtherError("handoff-row-drift", program.version_ref)


def _native_handoff(
    program: Plan2NativeCatalogCardMoveOtherVersion,
    play_origin: str,
) -> RemainingCardMoveHandoff:
    if play_origin not in PLAY_ORIGINS:
        raise Plan2NativeCatalogCardMoveOtherError("invalid-play-origin", play_origin)
    return RemainingCardMoveHandoff(
        "direct-effect",
        program.card_id,
        "ordered-card-play-effect",
        TARGET_SLOT_INDEX,
        play_origin,  # type: ignore[arg-type]
        1,
    )


def execute_plan2_native_catalog_card_move_other(
    state: RemainingCardMoveState,
    program: Plan2NativeCatalogCardMoveOtherVersion,
    *,
    play_origin: Literal["normal", "forced", "extra"] | str = "normal",
    handoff: Plan2NativeCatalogCardMoveOtherHandoffRow | None = None,
    hand_limit: int = 5,
    lesson_type: str = "ProduceStepLessonType_LessonVocal",
    database: str | Path = DEFAULT_DATABASE,
) -> Plan2NativeCatalogCardMoveOtherExecution:
    """Execute only the direct target leaf through the immutable exact primitive."""

    if not isinstance(state, RemainingCardMoveState):
        raise TypeError("state must be RemainingCardMoveState")
    if not isinstance(program, Plan2NativeCatalogCardMoveOtherVersion):
        raise TypeError("program must be CardMove-other version")
    _validate_handoff_row(program, handoff)
    native_handoff = _native_handoff(program, str(play_origin))
    try:
        context = RemainingCardMoveExecutionContext(
            hand_limit=hand_limit,
            lesson_type=lesson_type,
        )
        native = simulate_remaining_card_move(
            state,
            program.remaining_contract,
            native_handoff,
            context,
            database=Path(database),
        )
    except RemainingCardMoveError as error:
        raise Plan2NativeCatalogCardMoveOtherError(error.code, error.detail) from error
    return Plan2NativeCatalogCardMoveOtherExecution(program, native_handoff, native)


execute_plan2_native_card_move_other = execute_plan2_native_catalog_card_move_other
execute_card_move_other = execute_plan2_native_catalog_card_move_other


def _program_for_handoff(
    source: Plan2NativeCatalogCardMoveOtherCompilation | Plan2NativeCatalogCardMoveOtherCatalog,
    row: Plan2NativeCatalogCardMoveOtherHandoffRow,
) -> Plan2NativeCatalogCardMoveOtherVersion:
    if not isinstance(row, Plan2NativeCatalogCardMoveOtherHandoffRow):
        raise TypeError("row must be CardMove-other handoff row")
    if isinstance(source, Plan2NativeCatalogCardMoveOtherCompilation):
        catalog = source.catalog
    elif isinstance(source, Plan2NativeCatalogCardMoveOtherCatalog):
        catalog = source
    else:
        raise TypeError("source must be CardMove-other compilation or catalog")
    program = catalog.get(row.card_id, row.upgrade)
    if program is None:
        raise Plan2NativeCatalogCardMoveOtherError("handoff-version-not-compiled", row.version_ref)
    _validate_handoff_row(program, row)
    return program


def execute_plan2_native_catalog_card_move_other_handoff(
    state: RemainingCardMoveState,
    source: Plan2NativeCatalogCardMoveOtherCompilation | Plan2NativeCatalogCardMoveOtherCatalog,
    row: Plan2NativeCatalogCardMoveOtherHandoffRow,
    *,
    play_origin: Literal["normal", "forced", "extra"] | str = "normal",
    hand_limit: int = 5,
    lesson_type: str = "ProduceStepLessonType_LessonVocal",
    database: str | Path = DEFAULT_DATABASE,
) -> Plan2NativeCatalogCardMoveOtherExecution:
    program = _program_for_handoff(source, row)
    return execute_plan2_native_catalog_card_move_other(
        state,
        program,
        play_origin=play_origin,
        handoff=row,
        hand_limit=hand_limit,
        lesson_type=lesson_type,
        database=database,
    )


execute_plan2_native_card_move_other_handoff = execute_plan2_native_catalog_card_move_other_handoff


__all__ = [
    "ADAPTER_ID",
    "CARD_ID",
    "TARGET_CARD_ID",
    "TARGET_UPGRADES",
    "TARGET_REFS",
    "TARGET_EFFECT_ID",
    "TARGET_SEARCH_ID",
    "TARGET_SLOT_INDEX",
    "TARGET_POSITION",
    "TARGET_DESTINATION",
    "TARGET_PICK",
    "TARGET_COUNT",
    "PLAY_ORIGINS",
    "SOURCE_ORDER",
    "FORCE_PLAY_EFFECT_ID",
    "ENCORE_EFFECT_ID",
    "Plan2NativeCatalogCardMoveOtherError",
    "Plan2NativeCatalogCardMoveOtherBlocker",
    "Plan2NativeCatalogCardMoveOtherCompanionBlocker",
    "Plan2NativeCatalogCardMoveOtherEffectSlot",
    "Plan2NativeCatalogCardMoveOtherVersion",
    "Plan2NativeCatalogCardMoveOtherProgram",
    "Plan2NativeCatalogCardMoveOtherCatalog",
    "Plan2NativeCatalogCardMoveOtherCompilation",
    "Plan2NativeCatalogCardMoveOtherCatalogCompilation",
    "Plan2NativeCatalogCardMoveOtherHandoffRow",
    "Plan2NativeCatalogCardMoveOtherHandoff",
    "Plan2NativeCatalogCardMoveOtherExecution",
    "RemainingCardMoveState",
    "compile_plan2_native_catalog_card_move_other",
    "compile_plan2_native_card_move_other_catalog",
    "load_plan2_native_catalog_card_move_other",
    "build_plan2_native_catalog_card_move_other_handoff",
    "build_plan2_native_catalog_card_move_other_central_handoff",
    "build_plan2_card_move_other_central_handoff",
    "exact_plan2_native_catalog_card_move_other_rows",
    "execute_plan2_native_catalog_card_move_other",
    "execute_plan2_native_card_move_other",
    "execute_card_move_other",
    "execute_plan2_native_catalog_card_move_other_handoff",
    "execute_plan2_native_card_move_other_handoff",
]
