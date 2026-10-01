"""Typed Plan 3 ``ExamCardUpgrade`` resolver and immutable executor.

This module is deliberately narrower than the native executor.  It resolves
the real Master effect/search rows, retains the native GUID/zone order, and
only commits the part of ``UpgradeEffectExecutor`` that is evidenced by the
Android 3.2.3 body:

* ``GetSearchCardList`` supplies the ordered search result and native pick
  branch;
* the executor filters with ``ExamCardData.IsUpgradableRaw``;
* ``DeepCopy`` followed by ``SetTemporaryUpgrade`` writes the temporary
  upgrade layer, while the existing runtime-card resolver proves the exact
  effective Master variant.

The four ordinary ``Plan3NativeState`` zones are the complete settled state.
Playing is intentionally represented as a typed pause because the existing
state rejects an unsettled playing transition.  No game operation, GUI
operation, clock, or integrity/security check belongs here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from enum import Enum
import json
from pathlib import Path
import sqlite3
from typing import Any

from .audition_native_eligibility import MAX_EFFECTIVE_UPGRADE
from .card_search import ProduceCardSearchRule, load_produce_card_search
from .exam_native_rng import UINT32_MASK
from .master_db import DEFAULT_DATABASE
from .plan3_engine import (
    CATEGORY_ACTIVE,
    CATEGORY_MENTAL,
    PICK_COUNT_UNKNOWN,
    PICK_RANGE_ALL,
    PICK_RANGE_RANDOM,
    PICK_RANGE_SELECT,
)
from .plan3_native_state import (
    Plan3NativeCard,
    Plan3NativeCardMoveTarget,
    Plan3NativeState,
    Plan3NativeStateError,
)
from .plan3_runtime_card import resolve_plan3_runtime_card


EFFECT_EXAM_CARD_UPGRADE = "ProduceExamEffectType_ExamCardUpgrade"
_EFFECT_GROUP = "effect_group-visible-exam_card_upgrade-000"

_POSITION_HAND = "ProduceCardPositionType_Hand"
_POSITION_DECK = "ProduceCardPositionType_Deck"
_POSITION_DECK_ALL = "ProduceCardPositionType_DeckAll"
_POSITION_GRAVE = "ProduceCardPositionType_Grave"
_POSITION_HOLD = "ProduceCardPositionType_Hold"
_POSITION_PLAYING = "ProduceCardPositionType_Playing"

_ORDER_UNKNOWN = "ProduceCardOrderType_Unknown"
_ORDER_FIRST = "ProduceCardOrderType_First"
_UNKNOWN_PLAN = "ProducePlanType_Unknown"
_UNKNOWN_STATUS = "ProduceCardSearchStatusType_Unknown"
_UNKNOWN_MIN_MAX = "ConditionMinMaxType_Unknown"
_UNKNOWN_EXAM_EFFECT = "ProduceExamEffectType_Unknown"
_UNKNOWN_COST = "ExamCostType_Unknown"
_MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"


class Plan3CardUpgradeBranch(str, Enum):
    """The exact pick branch selected by an ``ExamCardUpgrade`` row."""

    ALL = "all"
    SELECT = "select"
    RANDOM = "random"


class Plan3CardUpgradeResultStatus(str, Enum):
    APPLIED = "applied"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class Plan3NativeUpgradeEvidence:
    """Pinned native facts used by this typed effect family."""

    game_version: str = "Android 3.2.3"
    effect_enum_name: str = "ExamCardUpgrade"
    effect_enum_value: int = 11
    executor_type: str = "Campus.InGame.Exam.UpgradeEffectExecutor"
    branch_target: int = 0x7E5CE70
    constructor_va: int = 0x7E90F6C
    constructor_token: int = 0x06004927
    execute_va: int = 0x7E91218
    search_card_list_va: int = 0x7E57B94
    is_upgradable_raw_va: int = 0x80910F8
    set_temporary_upgrade_va: int = 0x80907A0

    def __post_init__(self) -> None:
        for name in ("game_version", "effect_enum_name", "executor_type"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty text")
        for name in (
            "effect_enum_value",
            "branch_target",
            "constructor_va",
            "constructor_token",
            "execute_va",
            "search_card_list_va",
            "is_upgradable_raw_va",
            "set_temporary_upgrade_va",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

    def to_dict(self) -> dict[str, object]:
        return {
            "game_version": self.game_version,
            "effect_enum_name": self.effect_enum_name,
            "effect_enum_value": self.effect_enum_value,
            "executor_type": self.executor_type,
            "branch_target": self.branch_target,
            "constructor_va": self.constructor_va,
            "constructor_token": self.constructor_token,
            "execute_va": self.execute_va,
            "search_card_list_va": self.search_card_list_va,
            "is_upgradable_raw_va": self.is_upgradable_raw_va,
            "set_temporary_upgrade_va": self.set_temporary_upgrade_va,
        }


PLAN3_NATIVE_UPGRADE_EVIDENCE = Plan3NativeUpgradeEvidence()


def _text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"{label} must be non-empty text")
    return value


def _integer(value: object, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    return value


def _uint32(value: object, label: str) -> int:
    number = _integer(value, label, minimum=0)
    if number > UINT32_MASK:
        raise ValueError(f"{label} must be a uint32")
    return number


def _tuple_text(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise ValueError(f"{label} must be a text sequence")
    result = tuple(value)
    if any(not isinstance(item, str) for item in result):
        raise ValueError(f"{label} must contain text")
    return result


@dataclass(frozen=True, slots=True)
class Plan3CardUpgradePause:
    """Machine-readable pause; the caller must supply evidence or a choice."""

    code: str
    detail: str = ""
    effect_id: str = ""
    guid: str = ""

    def __post_init__(self) -> None:
        _text(self.code, "pause code")
        _text(self.detail, "pause detail", allow_empty=True)
        _text(self.effect_id, "pause effect_id", allow_empty=True)
        _text(self.guid, "pause guid", allow_empty=True)

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "detail": self.detail,
            "effect_id": self.effect_id,
            "guid": self.guid,
        }


@dataclass(frozen=True, slots=True)
class Plan3CardUpgradeContract:
    """Immutable, fully resolved Master contract for one upgrade effect."""

    effect_id: str
    source_kind: str
    effect_type: str
    effect_value1: int
    effect_value2: int
    effect_count: int
    effect_turn: int
    effect_group_ids: tuple[str, ...]
    search_id: str
    search_id2: str
    pick_range_type: str
    pick_range_type2: str
    pick_count_type: str
    pick_count_type2: str
    pick_count_min: int
    pick_count_max: int
    pick_count_min2: int
    pick_count_max2: int
    pick_count_reference_search_id: str
    pick_count_reference_search_id2: str
    target_exam_effect_type: str
    target_card_id: str
    target_upgrade_count: int
    search: ProduceCardSearchRule
    search_support_pause: str = ""
    database: Path = DEFAULT_DATABASE
    native_evidence: Plan3NativeUpgradeEvidence = PLAN3_NATIVE_UPGRADE_EVIDENCE

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect_id")
        if self.source_kind not in {"effect", "drink"}:
            raise ValueError("source_kind must be effect or drink")
        if self.effect_type != EFFECT_EXAM_CARD_UPGRADE:
            raise ValueError("contract effect_type is not ExamCardUpgrade")
        for name in ("effect_value1", "effect_value2", "effect_count", "effect_turn"):
            if _integer(getattr(self, name), name) != 0:
                raise ValueError(f"{name} is not neutral for ExamCardUpgrade")
        if tuple(self.effect_group_ids) != (_EFFECT_GROUP,):
            raise ValueError("unsupported ExamCardUpgrade effect group")
        object.__setattr__(self, "effect_group_ids", tuple(self.effect_group_ids))
        for name in (
            "search_id",
            "search_id2",
            "pick_range_type",
            "pick_range_type2",
            "pick_count_type",
            "pick_count_type2",
            "pick_count_reference_search_id",
            "pick_count_reference_search_id2",
            "target_exam_effect_type",
            "target_card_id",
        ):
            _text(getattr(self, name), name, allow_empty=name.endswith("2") or name in {
                "search_id2",
                "pick_count_reference_search_id",
                "pick_count_reference_search_id2",
                "target_card_id",
            })
        if self.search_id2:
            raise ValueError("ExamCardUpgrade does not use search_id2")
        if self.pick_range_type2 != "ProducePickRangeType_Unknown":
            raise ValueError("ExamCardUpgrade does not use pick_range_type2")
        if self.pick_count_type != PICK_COUNT_UNKNOWN or self.pick_count_type2 != PICK_COUNT_UNKNOWN:
            raise ValueError("ExamCardUpgrade pick count type is not unknown")
        if self.pick_count_min2 != 0 or self.pick_count_max2 != 0:
            raise ValueError("ExamCardUpgrade does not use a second count")
        if self.pick_count_reference_search_id or self.pick_count_reference_search_id2:
            raise ValueError("ExamCardUpgrade does not use a count reference search")
        if self.target_exam_effect_type != "ProduceExamEffectType_Unknown":
            raise ValueError("ExamCardUpgrade has an unsupported target effect")
        if self.target_card_id or self.target_upgrade_count != 0:
            raise ValueError("ExamCardUpgrade has an unsupported direct target")
        minimum = _integer(self.pick_count_min, "pick_count_min", minimum=0)
        maximum = _integer(self.pick_count_max, "pick_count_max", minimum=0)
        if minimum > maximum:
            raise ValueError("pick_count_min cannot exceed pick_count_max")
        if self.pick_range_type == PICK_RANGE_ALL:
            if minimum != 0 or maximum != 0:
                raise ValueError("All ExamCardUpgrade rows must use count 0..0")
        elif self.pick_range_type not in {PICK_RANGE_SELECT, PICK_RANGE_RANDOM}:
            raise ValueError("unsupported ExamCardUpgrade pick range")
        if not isinstance(self.search, ProduceCardSearchRule):
            raise TypeError("search must be ProduceCardSearchRule")
        if self.search.id != self.search_id:
            raise ValueError("search id differs from contract")
        _text(self.search_support_pause, "search_support_pause", allow_empty=True)
        object.__setattr__(self, "database", Path(self.database))
        if not isinstance(self.native_evidence, Plan3NativeUpgradeEvidence):
            raise TypeError("native_evidence must be Plan3NativeUpgradeEvidence")

    @property
    def branch(self) -> Plan3CardUpgradeBranch:
        return {
            PICK_RANGE_ALL: Plan3CardUpgradeBranch.ALL,
            PICK_RANGE_SELECT: Plan3CardUpgradeBranch.SELECT,
            PICK_RANGE_RANDOM: Plan3CardUpgradeBranch.RANDOM,
        }[self.pick_range_type]

    def to_dict(self) -> dict[str, object]:
        return {
            "effect_id": self.effect_id,
            "source_kind": self.source_kind,
            "effect_type": self.effect_type,
            "effect_value1": self.effect_value1,
            "effect_value2": self.effect_value2,
            "effect_count": self.effect_count,
            "effect_turn": self.effect_turn,
            "effect_group_ids": list(self.effect_group_ids),
            "search_id": self.search_id,
            "search_id2": self.search_id2,
            "search_position": self.search.card_position_type,
            "search_order": self.search.order_type,
            "search_categories": list(self.search.card_categories),
            "search_limit_count": self.search.limit_count,
            "search_support_pause": self.search_support_pause,
            "pick_range_type": self.pick_range_type,
            "pick_count_type": self.pick_count_type,
            "pick_count_min": self.pick_count_min,
            "pick_count_max": self.pick_count_max,
            "branch": self.branch.value,
            "database": str(self.database),
            "native_evidence": self.native_evidence.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class Plan3CardUpgradeResolution:
    """Resolver output; unknown Master rows are typed pauses."""

    contract: Plan3CardUpgradeContract | None = None
    pause: Plan3CardUpgradePause | None = None

    def __post_init__(self) -> None:
        if (self.contract is None) == (self.pause is None):
            raise ValueError("resolution must contain exactly a contract or pause")

    @property
    def resolved(self) -> bool:
        return self.contract is not None

    def to_dict(self) -> dict[str, object]:
        if self.contract is not None:
            return {"resolved": True, "contract": self.contract.to_dict()}
        assert self.pause is not None
        return {"resolved": False, "pause": self.pause.to_dict()}


@dataclass(frozen=True, slots=True)
class Plan3CardUpgradeSelectionRequest:
    """Explicit native-search handoff: selected GUIDs and/or RNG cursor."""

    effect_id: str
    branch: Plan3CardUpgradeBranch
    selected_guids: tuple[str, ...] | None = None
    rng_state: int | None = None

    def __post_init__(self) -> None:
        _text(self.effect_id, "selection effect_id")
        if not isinstance(self.branch, Plan3CardUpgradeBranch):
            try:
                object.__setattr__(
                    self, "branch", Plan3CardUpgradeBranch(self.branch)
                )
            except (TypeError, ValueError) as error:
                raise ValueError("invalid selection branch") from error
        if self.selected_guids is not None:
            if isinstance(self.selected_guids, (str, bytes)):
                raise TypeError("selected_guids must be a sequence")
            values = tuple(self.selected_guids)
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError("selected_guids must contain non-empty text")
            object.__setattr__(self, "selected_guids", values)
        if self.rng_state is not None:
            object.__setattr__(
                self, "rng_state", _uint32(self.rng_state, "rng_state")
            )

    @classmethod
    def for_contract(
        cls,
        contract: Plan3CardUpgradeContract,
        *,
        selected_guids: Sequence[str] | None = None,
        rng_state: int | None = None,
    ) -> "Plan3CardUpgradeSelectionRequest":
        if not isinstance(contract, Plan3CardUpgradeContract):
            raise TypeError("contract must be Plan3CardUpgradeContract")
        return cls(contract.effect_id, contract.branch, selected_guids, rng_state)

    def to_dict(self) -> dict[str, object]:
        return {
            "effect_id": self.effect_id,
            "branch": self.branch.value,
            "selected_guids": None
            if self.selected_guids is None
            else list(self.selected_guids),
            "rng_state": self.rng_state,
        }


@dataclass(frozen=True, slots=True)
class Plan3CardUpgradeCandidate:
    """One ordered, GUID-exact result of the Master search predicate."""

    guid: str
    card_id: str
    source_zone: str
    source_index: int
    card: Plan3NativeCard

    def __post_init__(self) -> None:
        _text(self.guid, "candidate guid")
        _text(self.card_id, "candidate card_id")
        if self.guid != self.card.guid or self.card_id != self.card.card_id:
            raise ValueError("candidate identity differs from card")
        if self.source_zone not in {"hand", "deck", "grave", "hold", "playing"}:
            raise ValueError("invalid candidate source zone")
        _integer(self.source_index, "candidate source_index", minimum=0)

    def to_dict(self) -> dict[str, object]:
        return {
            "guid": self.guid,
            "card_id": self.card_id,
            "source_zone": self.source_zone,
            "source_index": self.source_index,
            "effective_upgrade": self.card.effective_upgrade,
        }


@dataclass(frozen=True, slots=True)
class Plan3CardUpgradeCandidateResult:
    candidates: tuple[Plan3CardUpgradeCandidate, ...] = ()
    pause: Plan3CardUpgradePause | None = None

    def __post_init__(self) -> None:
        values = tuple(self.candidates)
        if any(not isinstance(value, Plan3CardUpgradeCandidate) for value in values):
            raise TypeError("candidates must contain Plan3CardUpgradeCandidate values")
        if len({value.guid for value in values}) != len(values):
            raise ValueError("candidate GUIDs must be unique")
        object.__setattr__(self, "candidates", values)
        if self.pause is not None and not isinstance(self.pause, Plan3CardUpgradePause):
            raise TypeError("pause must be Plan3CardUpgradePause or None")


@dataclass(frozen=True, slots=True)
class Plan3CardUpgradeMutationTrace:
    """Per-GUID before/after trace for one selected card."""

    ordinal: int
    guid: str
    card_id: str
    source_zone: str
    source_index: int
    status: str
    base_upgrade_before: int
    temporary_upgrade_before: int
    effective_upgrade_before: int
    base_upgrade_after: int | None
    temporary_upgrade_after: int | None
    effective_upgrade_after: int | None
    support_upgrade_ids_before: tuple[str, ...]
    support_upgrade_ids_after: tuple[str, ...] | None
    runtime_preserved: bool
    reason: str = ""

    def __post_init__(self) -> None:
        _integer(self.ordinal, "trace ordinal", minimum=0)
        _text(self.guid, "trace guid")
        _text(self.card_id, "trace card_id")
        _text(self.status, "trace status")
        _integer(self.source_index, "trace source_index", minimum=0)
        for name in (
            "base_upgrade_before",
            "temporary_upgrade_before",
            "effective_upgrade_before",
        ):
            _integer(getattr(self, name), name, minimum=0)
        if self.base_upgrade_after is not None:
            _integer(self.base_upgrade_after, "base_upgrade_after", minimum=0)
        if self.temporary_upgrade_after is not None:
            _integer(self.temporary_upgrade_after, "temporary_upgrade_after", minimum=0)
        if self.effective_upgrade_after is not None:
            _integer(self.effective_upgrade_after, "effective_upgrade_after", minimum=0)
        before = tuple(self.support_upgrade_ids_before)
        if any(not isinstance(value, str) for value in before):
            raise ValueError("support_upgrade_ids_before must contain text")
        object.__setattr__(self, "support_upgrade_ids_before", before)
        if self.support_upgrade_ids_after is not None:
            after = tuple(self.support_upgrade_ids_after)
            if any(not isinstance(value, str) for value in after):
                raise ValueError("support_upgrade_ids_after must contain text")
            object.__setattr__(self, "support_upgrade_ids_after", after)
        if not isinstance(self.runtime_preserved, bool):
            raise TypeError("runtime_preserved must be bool")
        _text(self.reason, "trace reason", allow_empty=True)

    def to_dict(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "guid": self.guid,
            "card_id": self.card_id,
            "source_zone": self.source_zone,
            "source_index": self.source_index,
            "status": self.status,
            "base_upgrade_before": self.base_upgrade_before,
            "temporary_upgrade_before": self.temporary_upgrade_before,
            "effective_upgrade_before": self.effective_upgrade_before,
            "base_upgrade_after": self.base_upgrade_after,
            "temporary_upgrade_after": self.temporary_upgrade_after,
            "effective_upgrade_after": self.effective_upgrade_after,
            "support_upgrade_ids_before": list(self.support_upgrade_ids_before),
            "support_upgrade_ids_after": None
            if self.support_upgrade_ids_after is None
            else list(self.support_upgrade_ids_after),
            "runtime_preserved": self.runtime_preserved,
            "reason": self.reason,
        }


def _card_to_dict(card: Plan3NativeCard) -> dict[str, object]:
    status = card.runtime_grow_status
    return {
        "guid": card.guid,
        "card_id": card.card_id,
        "base_upgrade": card.base_upgrade,
        "temporary_upgrade": card.temporary_upgrade,
        "effective_upgrade": card.effective_upgrade,
        "support_upgrade_ids": list(card.support_upgrade_ids),
        "fixed_deck_order": card.fixed_deck_order,
        "play_count": card.play_count,
        "runtime_grow_effect_ids": list(card.runtime_grow_effect_ids),
        "runtime_lesson_add": card.runtime_lesson_add,
        "runtime_full_power_point_add": card.runtime_full_power_point_add,
        "runtime_full_power_point_add_aggregate": {
            "value": card.runtime_full_power_point_add_aggregate.value,
            "grow_effect_ids": list(
                card.runtime_full_power_point_add_aggregate.grow_effect_ids
            ),
            "customize_ids": list(
                card.runtime_full_power_point_add_aggregate.customize_ids
            ),
        },
        "runtime_full_power_point_cost_add": card.runtime_full_power_point_cost_add,
        "runtime_customize_ids": list(card.runtime_customize_ids),
        "runtime_customization": {
            "customize_ids": list(card.runtime_customization.customize_ids),
            "effects": [
                {
                    "slot_index": effect.slot_index,
                    "customize_id": effect.customize_id,
                    "customize_count": effect.customize_count,
                    "grow_effect_id": effect.grow_effect_id,
                    "grow_effect_type": effect.grow_effect_type,
                    "value": effect.value,
                    "added_effect_id": (
                        None
                        if effect.added_effect is None
                        else effect.added_effect.id
                    ),
                }
                for effect in card.runtime_customization.effects
            ],
        },
        "runtime_customize_lesson_add": card.runtime_customize_lesson_add,
        "runtime_customize_lesson_depend_review_add": (
            card.runtime_customize_lesson_depend_review_add
        ),
        "runtime_lesson_count_add": card.runtime_lesson_count_add,
        "runtime_grow_status_id": None if status is None else status.id,
        "runtime_block_add": card.runtime_block_add,
        "runtime_cost_reduce": card.runtime_cost_reduce,
        "runtime_cost_add": card.runtime_cost_add,
        "runtime_cost_penetrate_reduce": (
            card.runtime_cost_penetrate_reduce
        ),
        "runtime_cost_penetrate_add": card.runtime_cost_penetrate_add,
    }


def _state_to_dict(state: Plan3NativeState) -> dict[str, object]:
    return {
        "random_state": state.random_state,
        "turn_used_support_ids": list(state.turn_used_support_ids),
        "total_effect_draw_card_count": state.total_effect_draw_card_count,
        "search_stamina_runtime": state.search_stamina_runtime.to_dict(),
        "anti_debuff_runtime": state.anti_debuff_runtime.to_json(),
        "hand": [_card_to_dict(card) for card in state.hand],
        "deck": [_card_to_dict(card) for card in state.deck],
        "grave": [_card_to_dict(card) for card in state.grave],
        "lost": [_card_to_dict(card) for card in state.lost],
        "hold": [_card_to_dict(card) for card in state.hold],
    }


@dataclass(frozen=True, slots=True)
class Plan3CardUpgradeResult:
    status: Plan3CardUpgradeResultStatus
    effect_id: str
    source_kind: str
    branch: Plan3CardUpgradeBranch
    state_before: Plan3NativeState
    state_after: Plan3NativeState
    candidate_guids: tuple[str, ...]
    selected_guids: tuple[str, ...]
    rng_state_before: int
    rng_state_after: int
    mutation_trace: tuple[Plan3CardUpgradeMutationTrace, ...] = ()
    pause: Plan3CardUpgradePause | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, Plan3CardUpgradeResultStatus):
            object.__setattr__(
                self, "status", Plan3CardUpgradeResultStatus(self.status)
            )
        _text(self.effect_id, "result effect_id")
        if self.source_kind not in {"effect", "drink"}:
            raise ValueError("invalid result source_kind")
        if not isinstance(self.branch, Plan3CardUpgradeBranch):
            object.__setattr__(self, "branch", Plan3CardUpgradeBranch(self.branch))
        if not isinstance(self.state_before, Plan3NativeState) or not isinstance(
            self.state_after, Plan3NativeState
        ):
            raise TypeError("result states must be Plan3NativeState")
        object.__setattr__(self, "candidate_guids", tuple(self.candidate_guids))
        object.__setattr__(self, "selected_guids", tuple(self.selected_guids))
        if len(set(self.candidate_guids)) != len(self.candidate_guids):
            raise ValueError("candidate_guids must be unique")
        if (
            self.status is Plan3CardUpgradeResultStatus.APPLIED
            and len(set(self.selected_guids)) != len(self.selected_guids)
        ):
            raise ValueError("selected_guids must be unique")
        _uint32(self.rng_state_before, "rng_state_before")
        _uint32(self.rng_state_after, "rng_state_after")
        traces = tuple(self.mutation_trace)
        if any(not isinstance(value, Plan3CardUpgradeMutationTrace) for value in traces):
            raise TypeError("mutation_trace must contain typed traces")
        object.__setattr__(self, "mutation_trace", traces)
        if self.status is Plan3CardUpgradeResultStatus.APPLIED and self.pause is not None:
            raise ValueError("applied result cannot contain a pause")
        if self.status is Plan3CardUpgradeResultStatus.PAUSED and self.pause is None:
            raise ValueError("paused result must contain a pause")
        if self.pause is not None and not isinstance(self.pause, Plan3CardUpgradePause):
            raise TypeError("pause must be Plan3CardUpgradePause")

    @property
    def applied(self) -> bool:
        return self.status is Plan3CardUpgradeResultStatus.APPLIED

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "effect_id": self.effect_id,
            "source_kind": self.source_kind,
            "branch": self.branch.value,
            "candidate_guids": list(self.candidate_guids),
            "selected_guids": list(self.selected_guids),
            "rng_state_before": self.rng_state_before,
            "rng_state_after": self.rng_state_after,
            "pause": None if self.pause is None else self.pause.to_dict(),
            "mutation_trace": [trace.to_dict() for trace in self.mutation_trace],
            "state_before": _state_to_dict(self.state_before),
            "state_after": _state_to_dict(self.state_after),
        }


def _pause(
    code: str,
    detail: str = "",
    *,
    effect_id: str = "",
    guid: str = "",
) -> Plan3CardUpgradePause:
    return Plan3CardUpgradePause(code, detail, effect_id, guid)


def _json_object(value: object, label: str) -> Mapping[str, object]:
    try:
        parsed = json.loads(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be JSON object") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be JSON object")
    return parsed


def _raw_text(raw: Mapping[str, object], key: str, *, empty: bool = False) -> str:
    if empty and key not in raw:
        return ""
    return _text(raw.get(key), key, allow_empty=empty)


def _raw_int(raw: Mapping[str, object], key: str) -> int:
    return _integer(raw.get(key), key)


def _raw_strings(raw: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = raw.get(key)
    if value is None and key in {
        "chainProduceExamEffectIds",
        "produceCardGrowEffectIds",
    }:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a JSON string array")
    return tuple(value)


def _read_upgrade_shape(
    effect_id: str,
    source_kind: str,
    row: Mapping[str, object],
) -> dict[str, object]:
    raw = _json_object(row.get("raw_json"), f"{effect_id}.raw_json")
    if _raw_text(raw, "id") != effect_id:
        raise ValueError("effect raw id differs from row id")
    effect_type = _raw_text(raw, "effectType")
    if effect_type != EFFECT_EXAM_CARD_UPGRADE:
        raise ValueError("effect type is not ExamCardUpgrade")

    # These values are the exact native constructor/executor fields.  The
    # scalar columns are cross-checked for both effect-table and drink rows.
    column_pairs = (
        ("effectType", "effect_type"),
        ("effectValue1", "value1" if source_kind == "effect" else "effect_value1"),
        ("effectValue2", "value2" if source_kind == "effect" else "effect_value2"),
        ("effectCount", "effect_count"),
        ("effectTurn", "effect_turn"),
    )
    for raw_key, column in column_pairs:
        if column in row and raw.get(raw_key) != row[column]:
            raise ValueError(f"{raw_key} differs from Master column")

    shape: dict[str, object] = {
        "effect_type": effect_type,
        "effect_value1": _raw_int(raw, "effectValue1"),
        "effect_value2": _raw_int(raw, "effectValue2"),
        "effect_count": _raw_int(raw, "effectCount"),
        "effect_turn": _raw_int(raw, "effectTurn"),
        "effect_group_ids": _raw_strings(raw, "effectGroupIds"),
        "search_id": _raw_text(raw, "produceCardSearchId", empty=True),
        "search_id2": _raw_text(raw, "produceCardSearchId2", empty=True),
        "pick_range_type": _raw_text(raw, "pickRangeType", empty=True),
        "pick_range_type2": _raw_text(raw, "pickRangeType2", empty=True),
        "pick_count_type": _raw_text(raw, "pickCountType", empty=True),
        "pick_count_type2": _raw_text(raw, "pickCountType2", empty=True),
        "pick_count_min": _raw_int(raw, "pickCountMin"),
        "pick_count_max": _raw_int(raw, "pickCountMax"),
        "pick_count_min2": _raw_int(raw, "pickCountMin2"),
        "pick_count_max2": _raw_int(raw, "pickCountMax2"),
        "pick_count_reference_search_id": _raw_text(
            raw, "pickCountReferenceProduceCardSearchId", empty=True
        ),
        "pick_count_reference_search_id2": _raw_text(
            raw, "pickCountReferenceProduceCardSearchId2", empty=True
        ),
        "target_exam_effect_type": _raw_text(
            raw, "targetExamEffectType", empty=True
        ),
        "target_card_id": _raw_text(raw, "targetProduceCardId", empty=True),
        "target_upgrade_count": _raw_int(raw, "targetUpgradeCount"),
        "chain_effect_id": _raw_text(raw, "chainProduceExamEffectId", empty=True),
        "chain_effect_ids": _raw_strings(raw, "chainProduceExamEffectIds"),
        "status_enchant_id": _raw_text(
            raw, "produceExamStatusEnchantId", empty=True
        ),
        "trigger_id": _raw_text(raw, "produceExamTriggerId", empty=True),
        "grow_effect_ids": _raw_strings(raw, "produceCardGrowEffectIds"),
        "move_position_type": _raw_text(raw, "movePositionType", empty=True),
    }
    if source_kind == "drink":
        if row.get("source_kind") != "exam" or row.get("source_id") != effect_id:
            raise ValueError("drink catalog source does not match effect")
        if row.get("effect_type") != effect_type:
            raise ValueError("drink catalog effect type differs from raw row")
        if row.get("effect_group_ids_json") is not None:
            groups = json.loads(str(row["effect_group_ids_json"]))
            if tuple(groups) != shape["effect_group_ids"]:
                raise ValueError("drink catalog effect groups differ from raw row")
    else:
        if row.get("id") != effect_id or row.get("effect_type") != effect_type:
            raise ValueError("effect Master row does not match raw row")

    unsupported_nonempty = (
        "chain_effect_id",
        "chain_effect_ids",
        "status_enchant_id",
        "trigger_id",
        "grow_effect_ids",
    )
    if any(shape[name] for name in unsupported_nonempty):
        raise ValueError("ExamCardUpgrade has nested status, trigger, chain, or grow data")
    if shape["move_position_type"] not in {"", _MOVE_UNKNOWN}:
        raise ValueError("ExamCardUpgrade has an unsupported move position")
    return shape


def _search_support_pause(rule: ProduceCardSearchRule) -> str:
    """Classify only the current, evidenced collection-search grammar."""

    if rule.card_search_tag:
        return "card-search-tag-unproven"
    if rule.card_position_type not in {
        _POSITION_HAND,
        _POSITION_DECK,
        _POSITION_DECK_ALL,
        _POSITION_GRAVE,
        _POSITION_HOLD,
        _POSITION_PLAYING,
    }:
        return "card-search-position-unproven"
    if rule.card_position_type in {_POSITION_GRAVE, _POSITION_HOLD, _POSITION_PLAYING}:
        return "card-search-position-unproven"
    if rule.card_rarities or rule.produce_card_ids or rule.upgrade_counts:
        return "card-search-key-filter-unproven"
    neutral = (
        rule.plan_type == _UNKNOWN_PLAN
        and rule.card_status_type == _UNKNOWN_STATUS
        and not rule.produce_card_random_pool_id
        and rule.stamina_min_max_type == _UNKNOWN_MIN_MAX
        and rule.stamina_min == 0
        and rule.stamina_max == 0
        and rule.exam_effect_type == _UNKNOWN_EXAM_EFFECT
        and not rule.effect_group_ids
        and not rule.is_self
        and not rule.produce_card_pool_id
        and rule.cost_type == _UNKNOWN_COST
        and not rule.is_customized
    )
    if not neutral:
        return "card-search-non-neutral-unproven"
    if len(rule.card_categories) > 1 or any(
        category not in {CATEGORY_ACTIVE, CATEGORY_MENTAL}
        for category in rule.card_categories
    ):
        return "card-search-category-unproven"
    if rule.limit_count < 0:
        return "card-search-limit-invalid"
    if rule.card_position_type in {_POSITION_HAND, _POSITION_DECK_ALL}:
        if rule.order_type != _ORDER_UNKNOWN or rule.limit_count != 0:
            return "card-search-order-limit-unproven"
    elif rule.card_position_type == _POSITION_DECK:
        if rule.order_type == _ORDER_FIRST and rule.limit_count in {1, 3}:
            return ""
        if rule.order_type == _ORDER_UNKNOWN and rule.limit_count == 1:
            return ""
        return "card-search-order-limit-unproven"
    return ""


class Plan3CardUpgradeResolver:
    """Resolve exact card-effect or drink-catalog Master rows."""

    def __init__(
        self,
        database: Path = DEFAULT_DATABASE,
        *,
        native_evidence: Plan3NativeUpgradeEvidence = PLAN3_NATIVE_UPGRADE_EVIDENCE,
    ) -> None:
        self.database = Path(database)
        if not isinstance(native_evidence, Plan3NativeUpgradeEvidence):
            raise TypeError("native_evidence must be Plan3NativeUpgradeEvidence")
        self.native_evidence = native_evidence

    def resolve(
        self,
        effect_id: str,
        *,
        source_kind: str = "effect",
    ) -> Plan3CardUpgradeResolution:
        try:
            effect_id = _text(effect_id, "effect_id")
        except ValueError as error:
            return Plan3CardUpgradeResolution(
                pause=_pause("invalid-effect-id", str(error))
            )
        if source_kind not in {"effect", "drink"}:
            raise ValueError("source_kind must be effect or drink")
        try:
            row = self._load_row(effect_id, source_kind)
        except sqlite3.Error as error:
            return Plan3CardUpgradeResolution(
                pause=_pause("master-db-error", str(error), effect_id=effect_id)
            )
        if row is None:
            return Plan3CardUpgradeResolution(
                pause=_pause("effect-not-found", source_kind, effect_id=effect_id)
            )
        try:
            shape = _read_upgrade_shape(effect_id, source_kind, row)
            search_id = shape["search_id"]
            if not isinstance(search_id, str) or not search_id:
                raise ValueError("ExamCardUpgrade search id is empty")
            search = load_produce_card_search(search_id, self.database)
            support_pause = _search_support_pause(search)
            contract = Plan3CardUpgradeContract(
                effect_id=effect_id,
                source_kind=source_kind,
                effect_type=shape["effect_type"],
                effect_value1=shape["effect_value1"],
                effect_value2=shape["effect_value2"],
                effect_count=shape["effect_count"],
                effect_turn=shape["effect_turn"],
                effect_group_ids=shape["effect_group_ids"],
                search_id=shape["search_id"],
                search_id2=shape["search_id2"],
                pick_range_type=shape["pick_range_type"],
                pick_range_type2=shape["pick_range_type2"],
                pick_count_type=shape["pick_count_type"],
                pick_count_type2=shape["pick_count_type2"],
                pick_count_min=shape["pick_count_min"],
                pick_count_max=shape["pick_count_max"],
                pick_count_min2=shape["pick_count_min2"],
                pick_count_max2=shape["pick_count_max2"],
                pick_count_reference_search_id=shape[
                    "pick_count_reference_search_id"
                ],
                pick_count_reference_search_id2=shape[
                    "pick_count_reference_search_id2"
                ],
                target_exam_effect_type=shape["target_exam_effect_type"],
                target_card_id=shape["target_card_id"],
                target_upgrade_count=shape["target_upgrade_count"],
                search=search,
                search_support_pause=support_pause,
                database=self.database,
                native_evidence=self.native_evidence,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            return Plan3CardUpgradeResolution(
                pause=_pause("effect-shape-unsupported", str(error), effect_id=effect_id)
            )
        return Plan3CardUpgradeResolution(contract=contract)

    def _load_row(
        self, effect_id: str, source_kind: str
    ) -> Mapping[str, object] | None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            if source_kind == "effect":
                row = connection.execute(
                    """
                    SELECT id, effect_type, value1, value2, effect_count,
                           effect_turn, raw_json
                      FROM effect
                     WHERE id = ?
                    """,
                    (effect_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT source_kind, source_id, effect_type,
                           effect_value1, effect_value2, effect_count,
                           effect_turn, effect_group_ids_json, raw_json
                      FROM produce_drink_effect_catalog
                     WHERE source_kind = 'exam' AND source_id = ?
                    """,
                    (effect_id,),
                ).fetchone()
        return None if row is None else dict(row)

    def candidates(
        self,
        state: Plan3NativeState,
        contract: Plan3CardUpgradeContract,
    ) -> Plan3CardUpgradeCandidateResult:
        """Resolve the ordered, already-``IsUpgradableRaw`` candidate list."""

        if not isinstance(state, Plan3NativeState):
            raise TypeError("state must be Plan3NativeState")
        if not isinstance(contract, Plan3CardUpgradeContract):
            raise TypeError("contract must be Plan3CardUpgradeContract")
        if contract.search_support_pause:
            return Plan3CardUpgradeCandidateResult(
                pause=_pause(
                    contract.search_support_pause,
                    contract.search_id,
                    effect_id=contract.effect_id,
                )
            )
        entries = self._search_entries(state, contract.search.card_position_type)
        if isinstance(entries, Plan3CardUpgradePause):
            return Plan3CardUpgradeCandidateResult(pause=entries)
        candidates: list[Plan3CardUpgradeCandidate] = []
        for source_zone, source_index, card in entries:
            category = self._master_category(card.card_id, card.effective_upgrade)
            if category is None:
                return Plan3CardUpgradeCandidateResult(
                    pause=_pause(
                        "card-master-missing",
                        f"{card.card_id}+{card.effective_upgrade}",
                        effect_id=contract.effect_id,
                        guid=card.guid,
                    )
                )
            if contract.search.card_categories and category not in contract.search.card_categories:
                continue
            if contract.search.produce_card_ids and card.card_id not in contract.search.produce_card_ids:
                continue
            if contract.search.upgrade_counts and card.effective_upgrade not in contract.search.upgrade_counts:
                continue
            # This is the native b__7_0 IsUpgradableRaw predicate: raw base
            # plus temporary must be zero, and the +1 Master card must exist.
            if card.base_upgrade + card.temporary_upgrade != 0:
                continue
            if not self._master_exists(card.card_id, 1):
                continue
            candidates.append(
                Plan3CardUpgradeCandidate(
                    guid=card.guid,
                    card_id=card.card_id,
                    source_zone=source_zone,
                    source_index=source_index,
                    card=card,
                )
            )
        if contract.search.limit_count > 0:
            candidates = candidates[: contract.search.limit_count]
        return Plan3CardUpgradeCandidateResult(candidates=tuple(candidates))

    def _search_entries(
        self, state: Plan3NativeState, position: str
    ) -> tuple[tuple[str, int, Plan3NativeCard], ...] | Plan3CardUpgradePause:
        if position == _POSITION_HAND:
            return tuple(("hand", index, card) for index, card in enumerate(state.hand))
        if position in {_POSITION_DECK, _POSITION_DECK_ALL}:
            return tuple(("deck", index, card) for index, card in enumerate(state.deck))
        if position == _POSITION_PLAYING:
            return _pause("playing-state-unsettled", position)
        if position in {_POSITION_GRAVE, _POSITION_HOLD}:
            return _pause("card-search-position-unproven", position)
        return _pause("card-search-position-unproven", position)

    def _master_exists(self, card_id: str, upgrade: int) -> bool:
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT 1 FROM card WHERE id = ? AND upgrade_count = ?",
                (card_id, upgrade),
            ).fetchone()
        return row is not None

    def _master_category(self, card_id: str, upgrade: int) -> str | None:
        with closing(sqlite3.connect(self.database)) as connection:
            row = connection.execute(
                "SELECT category FROM card WHERE id = ? AND upgrade_count = ?",
                (card_id, upgrade),
            ).fetchone()
        return None if row is None else str(row[0])


def _move_target(candidate: Plan3CardUpgradeCandidate) -> Plan3NativeCardMoveTarget:
    source_zone = {
        "hand": "hand",
        "deck": "draw",
        "grave": "discard",
        "hold": "hold",
        "playing": "playing",
    }[candidate.source_zone]
    return Plan3NativeCardMoveTarget(
        guid=candidate.guid,
        card=candidate.card,
        source_zone=source_zone,
        source_index=candidate.source_index,
    )


def _trace_before(
    candidate: Plan3CardUpgradeCandidate,
    ordinal: int,
    *,
    status: str,
    reason: str = "",
    after: Plan3NativeCard | None = None,
) -> Plan3CardUpgradeMutationTrace:
    card = candidate.card
    return Plan3CardUpgradeMutationTrace(
        ordinal=ordinal,
        guid=card.guid,
        card_id=card.card_id,
        source_zone=candidate.source_zone,
        source_index=candidate.source_index,
        status=status,
        base_upgrade_before=card.base_upgrade,
        temporary_upgrade_before=card.temporary_upgrade,
        effective_upgrade_before=card.effective_upgrade,
        base_upgrade_after=None if after is None else after.base_upgrade,
        temporary_upgrade_after=None if after is None else after.temporary_upgrade,
        effective_upgrade_after=None if after is None else after.effective_upgrade,
        support_upgrade_ids_before=card.support_upgrade_ids,
        support_upgrade_ids_after=None
        if after is None
        else after.support_upgrade_ids,
        runtime_preserved=after is not None and all(
            getattr(card, field) == getattr(after, field)
            for field in (
                "runtime_grow_effect_ids",
                "runtime_lesson_add",
                "runtime_full_power_point_add",
                "runtime_full_power_point_add_aggregate",
                "runtime_full_power_point_cost_add",
                "runtime_customize_ids",
                "runtime_customization",
                "runtime_customize_lesson_add",
                "runtime_customize_lesson_depend_review_add",
                "runtime_lesson_count_add",
                "runtime_grow_status",
                "runtime_block_add",
                "runtime_cost_reduce",
                "runtime_cost_add",
                "runtime_cost_penetrate_reduce",
                "runtime_cost_penetrate_add",
            )
        ),
        reason=reason,
    )


def _pause_result(
    state: Plan3NativeState,
    contract: Plan3CardUpgradeContract,
    *,
    candidate_guids: Sequence[str] = (),
    selected_guids: Sequence[str] = (),
    pause: Plan3CardUpgradePause,
    mutation_trace: Sequence[Plan3CardUpgradeMutationTrace] = (),
) -> Plan3CardUpgradeResult:
    return Plan3CardUpgradeResult(
        status=Plan3CardUpgradeResultStatus.PAUSED,
        effect_id=contract.effect_id,
        source_kind=contract.source_kind,
        branch=contract.branch,
        state_before=state,
        state_after=state,
        candidate_guids=tuple(candidate_guids),
        selected_guids=tuple(selected_guids),
        rng_state_before=state.random_state,
        rng_state_after=state.random_state,
        mutation_trace=tuple(mutation_trace),
        pause=pause,
    )


def execute_plan3_card_upgrade(
    state: Plan3NativeState,
    contract: Plan3CardUpgradeContract,
    selection: Plan3CardUpgradeSelectionRequest | None = None,
    *,
    selected_guids: Sequence[str] | None = None,
    rng_state: int | None = None,
    database: Path | None = None,
) -> Plan3CardUpgradeResult:
    """Apply one exact contract or return an unchanged typed-pause result.

    The native-search bridge supplies ``selected_guids`` for Select (and may
    echo Random's selected GUIDs) plus the explicit RNG cursor for Random.
    The function never invents a user choice and never changes card order.
    """

    if not isinstance(state, Plan3NativeState):
        raise TypeError("state must be Plan3NativeState")
    if not isinstance(contract, Plan3CardUpgradeContract):
        raise TypeError("contract must be Plan3CardUpgradeContract")
    if selection is not None and (selected_guids is not None or rng_state is not None):
        raise TypeError("selection cannot be combined with explicit selection arguments")
    if selection is None:
        selection = Plan3CardUpgradeSelectionRequest.for_contract(
            contract, selected_guids=selected_guids, rng_state=rng_state
        )
    if not isinstance(selection, Plan3CardUpgradeSelectionRequest):
        raise TypeError("selection must be Plan3CardUpgradeSelectionRequest")
    if selection.effect_id != contract.effect_id:
        return _pause_result(
            state,
            contract,
            pause=_pause(
                "selection-effect-mismatch",
                selection.effect_id,
                effect_id=contract.effect_id,
            ),
        )
    if selection.branch is not contract.branch:
        return _pause_result(
            state,
            contract,
            pause=_pause(
                "selection-branch-mismatch",
                f"request={selection.branch.value};contract={contract.branch.value}",
                effect_id=contract.effect_id,
            ),
        )
    if selection.rng_state is not None and selection.rng_state != state.random_state:
        return _pause_result(
            state,
            contract,
            selected_guids=selection.selected_guids or (),
            pause=_pause(
                "rng-state-mismatch",
                f"request={selection.rng_state};state={state.random_state}",
                effect_id=contract.effect_id,
            ),
        )

    resolver = Plan3CardUpgradeResolver(
        database=contract.database if database is None else database,
        native_evidence=contract.native_evidence,
    )
    candidate_result = resolver.candidates(state, contract)
    if candidate_result.pause is not None:
        return _pause_result(
            state,
            contract,
            pause=candidate_result.pause,
        )
    candidates = candidate_result.candidates
    candidate_guids = tuple(candidate.guid for candidate in candidates)
    by_guid = {candidate.guid: candidate for candidate in candidates}
    random_after = state.random_state

    if contract.branch is Plan3CardUpgradeBranch.ALL:
        if selection.selected_guids not in (None, ()):
            return _pause_result(
                state,
                contract,
                candidate_guids=candidate_guids,
                selected_guids=selection.selected_guids,
                pause=_pause(
                    "all-does-not-accept-selection",
                    effect_id=contract.effect_id,
                ),
            )
        selected = candidates
    elif contract.branch is Plan3CardUpgradeBranch.SELECT:
        if selection.selected_guids is None:
            return _pause_result(
                state,
                contract,
                candidate_guids=candidate_guids,
                pause=_pause(
                    "selection-required",
                    contract.search_id,
                    effect_id=contract.effect_id,
                ),
            )
        requested = selection.selected_guids
        if len(set(requested)) != len(requested):
            return _pause_result(
                state,
                contract,
                candidate_guids=candidate_guids,
                selected_guids=requested,
                pause=_pause("duplicate-selection-guid", effect_id=contract.effect_id),
            )
        unknown = next((guid for guid in requested if guid not in by_guid), None)
        if unknown is not None:
            return _pause_result(
                state,
                contract,
                candidate_guids=candidate_guids,
                selected_guids=requested,
                pause=_pause(
                    "selection-guid-not-candidate", unknown, effect_id=contract.effect_id
                ),
            )
        if not contract.pick_count_min <= len(requested) <= contract.pick_count_max:
            return _pause_result(
                state,
                contract,
                candidate_guids=candidate_guids,
                selected_guids=requested,
                pause=_pause(
                    "selection-count-mismatch",
                    f"count={len(requested)};expected={contract.pick_count_min}..{contract.pick_count_max}",
                    effect_id=contract.effect_id,
                ),
            )
        selected = tuple(by_guid[guid] for guid in requested)
    else:
        if selection.rng_state is None:
            return _pause_result(
                state,
                contract,
                candidate_guids=candidate_guids,
                selected_guids=selection.selected_guids or (),
                pause=_pause("random-rng-required", effect_id=contract.effect_id),
            )
        try:
            random_state_input = replace(state, random_state=selection.rng_state)
            random_state_after, random_targets = random_state_input.resolve_random_card_move_targets(
                (_move_target(candidate) for candidate in candidates),
                count_min=contract.pick_count_min,
                count_max=contract.pick_count_max,
            )
        except (Plan3NativeStateError, TypeError, ValueError) as error:
            return _pause_result(
                state,
                contract,
                candidate_guids=candidate_guids,
                selected_guids=selection.selected_guids or (),
                pause=_pause(
                    "random-native-rng-unresolved",
                    str(error),
                    effect_id=contract.effect_id,
                ),
            )
        random_after = random_state_after.random_state
        selected = tuple(by_guid[target.guid] for target in random_targets)
        if selection.selected_guids is not None and tuple(selection.selected_guids) != tuple(
            candidate.guid for candidate in selected
        ):
            return _pause_result(
                state,
                contract,
                candidate_guids=candidate_guids,
                selected_guids=selection.selected_guids,
                pause=_pause(
                    "random-selection-mismatch",
                    f"native={tuple(candidate.guid for candidate in selected)!r}",
                    effect_id=contract.effect_id,
                ),
            )

    updates: dict[str, Plan3NativeCard] = {}
    traces: list[Plan3CardUpgradeMutationTrace] = []
    for ordinal, candidate in enumerate(selected):
        card = candidate.card
        target_effective = card.effective_upgrade + 1
        if target_effective > MAX_EFFECTIVE_UPGRADE:
            trace = _trace_before(
                candidate,
                ordinal,
                status="paused",
                reason="native-upgrade-cap-unresolved",
            )
            return _pause_result(
                state,
                contract,
                candidate_guids=candidate_guids,
                selected_guids=tuple(value.guid for value in selected),
                pause=_pause(
                    "native-upgrade-cap-unresolved",
                    f"effective={card.effective_upgrade};max={MAX_EFFECTIVE_UPGRADE}",
                    effect_id=contract.effect_id,
                    guid=card.guid,
                ),
                mutation_trace=(trace,),
            )
        try:
            # Reload the exact upgraded Master before committing the same
            # GUID.  Runtime grow/customization layers are instance-owned and
            # remain attached; later legality, cost, effects, triggers and
            # searches therefore compose that exact Master with this stack.
            upgraded_master = resolve_plan3_runtime_card(
                card.card_id,
                base_upgrade=card.base_upgrade,
                temporary_upgrade=1,
                effective_upgrade=target_effective,
                support_upgrade_ids=card.support_upgrade_ids,
                database=resolver.database,
            )
            if (
                upgraded_master.id != card.card_id
                or upgraded_master.upgrade != target_effective
            ):
                raise ValueError("upgraded Master identity mismatch")
            updated = replace(
                card,
                temporary_upgrade=1,
                effective_upgrade=target_effective,
            )
        except (KeyError, Plan3NativeStateError, TypeError, ValueError) as error:
            trace = _trace_before(
                candidate,
                ordinal,
                status="paused",
                reason="target-master-or-lineage-unresolved",
            )
            return _pause_result(
                state,
                contract,
                candidate_guids=candidate_guids,
                selected_guids=tuple(value.guid for value in selected),
                pause=_pause(
                    "target-master-or-lineage-unresolved",
                    str(error),
                    effect_id=contract.effect_id,
                    guid=card.guid,
                ),
                mutation_trace=(trace,),
            )
        updates[card.guid] = updated
        traces.append(
            _trace_before(candidate, ordinal, status="upgraded", after=updated)
        )

    def update_zone(zone: tuple[Plan3NativeCard, ...]) -> tuple[Plan3NativeCard, ...]:
        return tuple(updates.get(card.guid, card) for card in zone)

    after_state = replace(
        state,
        hand=update_zone(state.hand),
        deck=update_zone(state.deck),
        grave=update_zone(state.grave),
        lost=update_zone(state.lost),
        hold=update_zone(state.hold),
        random_state=random_after,
    )
    return Plan3CardUpgradeResult(
        status=Plan3CardUpgradeResultStatus.APPLIED,
        effect_id=contract.effect_id,
        source_kind=contract.source_kind,
        branch=contract.branch,
        state_before=state,
        state_after=after_state,
        candidate_guids=candidate_guids,
        selected_guids=tuple(value.guid for value in selected),
        rng_state_before=state.random_state,
        rng_state_after=random_after,
        mutation_trace=tuple(traces),
    )


def resolve_plan3_card_upgrade(
    effect_id: str,
    *,
    database: Path = DEFAULT_DATABASE,
    source_kind: str = "effect",
) -> Plan3CardUpgradeResolution:
    """Small hook for native search callers that already have an effect ID."""

    return Plan3CardUpgradeResolver(database).resolve(
        effect_id, source_kind=source_kind
    )


def resolve_plan3_card_upgrade_candidates(
    state: Plan3NativeState,
    contract: Plan3CardUpgradeContract,
    *,
    database: Path | None = None,
) -> Plan3CardUpgradeCandidateResult:
    """Small hook for feeding native-search candidates into a selector."""

    resolver = Plan3CardUpgradeResolver(
        contract.database if database is None else database,
        native_evidence=contract.native_evidence,
    )
    return resolver.candidates(state, contract)


__all__ = [
    "EFFECT_EXAM_CARD_UPGRADE",
    "PLAN3_NATIVE_UPGRADE_EVIDENCE",
    "Plan3CardUpgradeBranch",
    "Plan3CardUpgradeCandidate",
    "Plan3CardUpgradeCandidateResult",
    "Plan3CardUpgradeContract",
    "Plan3CardUpgradeMutationTrace",
    "Plan3CardUpgradePause",
    "Plan3CardUpgradeResolution",
    "Plan3CardUpgradeResolver",
    "Plan3CardUpgradeResult",
    "Plan3CardUpgradeResultStatus",
    "Plan3CardUpgradeSelectionRequest",
    "Plan3NativeUpgradeEvidence",
    "execute_plan3_card_upgrade",
    "resolve_plan3_card_upgrade",
    "resolve_plan3_card_upgrade_candidates",
]
