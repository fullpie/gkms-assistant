"""Standalone exact Plan2 catalog and lifecycle for the two additive families.

The module is deliberately target-scoped.  It reads the Master rows through
the existing exact interval5 loader, owns no central registry, and exposes
only frozen value objects and pure transitions.  Card names, descriptions,
screen text, and placeholder values are never used to resolve gameplay behavior.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Iterable, Literal, Mapping, TypeVar

from .native_exam_formula import (
    INT32_MAX,
    INT32_MIN,
    NativeFormulaDomainError,
    ceil_f32_to_i32,
    f32,
    permille_to_f32,
)
from .plan2_aggressive_additive_interval5 import (
    ADDITIVE_EFFECT_ID as FIX_EFFECT_ID,
    ADDITIVE_EFFECT_TYPE as FIX_EFFECT_TYPE,
    AGGRESSIVE_EFFECT_ID as P201_AGGRESSIVE_EFFECT_ID,
    AGGRESSIVE_EFFECT_TYPE,
    CARD_ID as P201_CARD_ID,
    CARD_MOVE_POSITION as P201_CARD_MOVE_POSITION,
    CARD_STAMINA_BY_UPGRADE as P201_STAMINA_BY_UPGRADE,
    ExactEffect,
    AggressiveAdditiveFixLayer,
    Interval5Listener,
    Plan2AggressiveAdditiveIntervalError,
    Plan2AggressiveAdditiveIntervalOverflow,
    QueuedIntervalEffect,
    _CARD_RAW_KEYS,
    _EFFECT_RAW_KEYS,
    _effect_expected,
    _json_array,
    _json_object,
    _load_effect,
    _strict_payload,
    load_plan2_aggressive_additive_interval5_catalog,
    native_add_and_clamp_zero,
    native_checked_sum,
    DRAW_EFFECT_ID,
    DRAW_EFFECT_TYPE,
    ENCORE_EFFECT_ID,
    ENCORE_EFFECT_TYPE,
    FORCE_PLAY_EFFECT_ID,
    FORCE_PLAY_EFFECT_TYPE,
    STATUS_ENCHANT_ID,
    TIMER_EFFECT_ID,
    TIMER_EFFECT_TYPE,
)
from .plan2_turn_progress_trigger import (
    TARGET_TRIGGER_ID as P182_TRIGGER_ID,
    load_plan2_turn_progress_trigger_catalog,
)
from .master_db import DEFAULT_DATABASE


SCHEMA_VERSION = 1
ADAPTER_ID = "plan2.native.catalog.aggressive-additive"
PLAN2_NATIVE_CATALOG_AGGRESSIVE_ADDITIVE_SCHEMA_VERSION = SCHEMA_VERSION
TARGET_VERSION_COUNT = 8

P182_CARD_ID = "p_card-02-sup-3_182"
P182_ADDITIVE_EFFECT_ID = "e_effect-exam_aggressive_additive-0500-03"
P182_ADDITIVE_EFFECT_TYPE = "ProduceExamEffectType_ExamAggressiveAdditive"
# The equipped-item status-change listener uses the same native additive
# status family as P182, but its source effect is a distinct exact Master row.
# Keeping that provenance on the layer lets retained LocalSave recovery bind
# the native status UID/order without pretending the item effect came from a
# card program.
PITEM_STATUS_CHANGE_ADDITIVE_EFFECT_ID = (
    "e_effect-exam_aggressive_additive-0500-02"
)
P182_STAMINA_EFFECT_ID = "e_effect-exam_stamina_consumption_down_fix-0001-inf"
P182_STAMINA_EFFECT_TYPE = (
    "ProduceExamEffectType_ExamStaminaConsumptionDownFix"
)
P182_AGGRESSIVE_EFFECT_IDS = (
    "e_effect-exam_card_play_aggressive-0003",
    "e_effect-exam_card_play_aggressive-0004",
    "e_effect-exam_card_play_aggressive-0005",
)
P182_AGGRESSIVE_EFFECT_ID_BY_UPGRADE = (
    "e_effect-exam_card_play_aggressive-0003",
    "e_effect-exam_card_play_aggressive-0003",
    "e_effect-exam_card_play_aggressive-0004",
    "e_effect-exam_card_play_aggressive-0005",
)
P182_AGGRESSIVE_VALUES = (3, 4, 5)
P182_STAMINA_EFFECT_VALUE = 1
P182_STAMINA_EFFECT_TURN = -1
P182_STAMINA_BY_UPGRADE = (4, 1, 1, 1)
P182_CARD_MOVE_POSITION = "ProduceCardMovePositionType_Lost"
P182_COST_TYPE = "ExamCostType_Unknown"
P182_COST_VALUE = 0
P182_EFFECT_GROUP = "effect_group-visible-exam_card_play_aggressive-000"
P182_STAMINA_EFFECT_GROUP = (
    "effect_group-visible-exam_stamina_consumption_down_fix-000"
)

P201_FIX_EFFECT_ID = FIX_EFFECT_ID
P201_FIX_EFFECT_TYPE = FIX_EFFECT_TYPE
P201_AGGRESSIVE_EFFECT_TYPE = AGGRESSIVE_EFFECT_TYPE
P201_STAMINA_BY_UPGRADE = P201_STAMINA_BY_UPGRADE
P201_CARD_MOVE_POSITION = P201_CARD_MOVE_POSITION

SUPPORTED_PLAY_ORIGINS = ("ordinary", "forced", "extra")
PLAY_ORIGINS = SUPPORTED_PLAY_ORIGINS
TARGET_FAMILIES = ("additive", "fix")
AGGRESSIVE_EFFECT_IDS = tuple(
    dict.fromkeys((*P182_AGGRESSIVE_EFFECT_IDS, P201_AGGRESSIVE_EFFECT_ID))
)


class Plan2NativeAggressiveAdditiveError(ValueError):
    """A strict catalog or immutable transition cannot be resolved."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        message = code if not detail else f"{code}: {detail}"
        super().__init__(message)


class Plan2NativeAggressiveAdditiveOverflow(
    Plan2NativeAggressiveAdditiveError
):
    """A proven signed-int32 aggregate would overflow."""


Plan2NativeCatalogAggressiveAdditiveContractError = (
    Plan2NativeAggressiveAdditiveError
)


class Plan2AggressiveAdditivePlayOrigin(str, Enum):
    ORDINARY = "ordinary"
    FORCED = "forced"
    EXTRA = "extra"


PlayOrigin = Plan2AggressiveAdditivePlayOrigin


_MapValue = TypeVar("_MapValue")


class _FrozenMap(Mapping[str, _MapValue]):
    """Small read-only mapping with no public mutation path."""

    __slots__ = ("_items", "_lookup")

    def __init__(self, values: Mapping[str, _MapValue]) -> None:
        self._items = tuple(sorted(values.items(), key=lambda item: item[0]))
        self._lookup = dict(self._items)

    def __getitem__(self, key: str) -> _MapValue:
        return self._lookup[key]

    def __iter__(self):
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)


def _i32(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Plan2NativeAggressiveAdditiveError(
            "value-not-signed-int32", label
        )
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2NativeAggressiveAdditiveError(
            "value-outside-signed-int32", f"{label}={value}"
        )
    return value


def _positive(value: object, label: str) -> int:
    value = _i32(value, label)
    if value <= 0:
        raise Plan2NativeAggressiveAdditiveError(
            "value-must-be-positive", f"{label}={value}"
        )
    return value


def _nonnegative(value: object, label: str) -> int:
    value = _i32(value, label)
    if value < 0:
        raise Plan2NativeAggressiveAdditiveError(
            "value-must-be-nonnegative", f"{label}={value}"
        )
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise Plan2NativeAggressiveAdditiveError(
            "value-must-be-nonempty-text", label
        )
    return value


def _bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise Plan2NativeAggressiveAdditiveError(
            "value-must-be-bool", label
        )
    return value


def _tuple_text(values: Iterable[object], label: str) -> tuple[str, ...]:
    result = tuple(values)
    if any(not isinstance(value, str) for value in result):
        raise Plan2NativeAggressiveAdditiveError(
            "value-must-be-text-sequence", label
        )
    return result


def _normalize_origin(
    value: Plan2AggressiveAdditivePlayOrigin | str,
) -> Plan2AggressiveAdditivePlayOrigin:
    if isinstance(value, Plan2AggressiveAdditivePlayOrigin):
        return value
    if value == "normal":
        return Plan2AggressiveAdditivePlayOrigin.ORDINARY
    try:
        return Plan2AggressiveAdditivePlayOrigin(value)
    except (TypeError, ValueError) as error:
        raise Plan2NativeAggressiveAdditiveError(
            "unsupported-play-origin", repr(value)
        ) from error


def _i32_wrap(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


@dataclass(frozen=True, slots=True)
class Plan2NativeAggressiveAdditiveEffect:
    effect_id: str
    effect_type: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    effect_group_ids: tuple[str, ...] = ()
    chain_effect_id: str = ""
    status_enchant_id: str = ""
    search_id: str = ""

    def __post_init__(self) -> None:
        _text(self.effect_id, "effect_id")
        _text(self.effect_type, "effect_type")
        _i32(self.value1, f"{self.effect_id}.value1")
        _i32(self.value2, f"{self.effect_id}.value2")
        _i32(self.effect_count, f"{self.effect_id}.count")
        _i32(self.effect_turn, f"{self.effect_id}.turn")
        if any(not isinstance(value, str) for value in self.effect_group_ids):
            raise Plan2NativeAggressiveAdditiveError(
                "effect-group-shape", self.effect_id
            )

    @property
    def lifetime(self) -> str:
        return "unlimited" if self.effect_turn < 0 else "finite"

    @property
    def value1_permille(self) -> int:
        return self.value1

    @property
    def permil(self) -> int:
        return self.value1

    @property
    def turn(self) -> int:
        return self.effect_turn

    @property
    def count(self) -> int:
        return self.effect_count

    @property
    def is_permanent(self) -> bool:
        return self.effect_turn < 0

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.effect_id,
            "effectType": self.effect_type,
            "value1": self.value1,
            "value2": self.value2,
            "count": self.effect_count,
            "turn": self.effect_turn,
            "lifetime": self.lifetime,
            "effectGroupIds": list(self.effect_group_ids),
            "chainEffectId": self.chain_effect_id,
            "statusEnchantId": self.status_enchant_id,
            "searchId": self.search_id,
        }


@dataclass(frozen=True, slots=True)
class Plan2NativeAggressiveAdditiveSlot:
    slot_index: int
    effect: Plan2NativeAggressiveAdditiveEffect
    trigger_id: str = ""
    hide_icon: bool = False
    is_once: bool = False

    def __post_init__(self) -> None:
        _nonnegative(self.slot_index, "slot_index")
        if not isinstance(self.effect, Plan2NativeAggressiveAdditiveEffect):
            raise TypeError("effect must be a typed native effect")
        if not isinstance(self.trigger_id, str):
            raise Plan2NativeAggressiveAdditiveError(
                "slot-trigger-shape", str(self.slot_index)
            )
        _bool(self.hide_icon, "slot.hide_icon")
        _bool(self.is_once, "slot.is_once")

    @property
    def effect_id(self) -> str:
        return self.effect.effect_id

    @property
    def effect_type(self) -> str:
        return self.effect.effect_type


@dataclass(frozen=True, slots=True)
class Plan2AggressiveAdditiveCompanionBlocker:
    code: str
    version_ref: str
    family: str
    detail: str
    effect_id: str = ""

    def __post_init__(self) -> None:
        _text(self.code, "blocker.code")
        _text(self.version_ref, "blocker.version_ref")
        card_id, separator, upgrade = self.version_ref.rpartition("#")
        if not separator or not card_id or not upgrade.isdigit():
            raise Plan2NativeAggressiveAdditiveError(
                "blocker-version-ref-shape", repr(self.version_ref)
            )
        _text(self.family, "blocker.family")
        _text(self.detail, "blocker.detail")
        if not isinstance(self.effect_id, str):
            raise Plan2NativeAggressiveAdditiveError(
                "blocker-effect-id-shape", self.family
            )

    @property
    def ref(self) -> tuple[str, int]:
        card_id, _, upgrade = self.version_ref.rpartition("#")
        return card_id, int(upgrade)

    @property
    def version_ref_text(self) -> str:
        return self.version_ref

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "versionRef": self.version_ref,
            "cardId": self.ref[0],
            "upgrade": self.ref[1],
            "family": self.family,
            "detail": self.detail,
            "effectId": self.effect_id,
        }


@dataclass(frozen=True, slots=True)
class Plan2NativeAggressiveAdditiveProgram:
    card_id: str
    upgrade: int
    plan_type: str
    category: str
    stamina: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    move_position: str
    ordered_slots: tuple[Plan2NativeAggressiveAdditiveSlot, ...]
    target_slot_index: int
    target_family: Literal["additive", "fix"]
    companion_blockers: tuple[Plan2AggressiveAdditiveCompanionBlocker, ...]
    owned_companion_families: tuple[str, ...] = ()
    effect_group_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.card_id, "card_id")
        _nonnegative(self.upgrade, "upgrade")
        _text(self.plan_type, "plan_type")
        _text(self.category, "category")
        _i32(self.stamina, "stamina")
        _text(self.cost_type, "cost_type")
        _i32(self.cost_value, "cost_value")
        if not isinstance(self.play_trigger_id, str):
            raise Plan2NativeAggressiveAdditiveError(
                "card-trigger-shape", self.card_id
            )
        _text(self.move_position, "move_position")
        if not self.ordered_slots:
            raise Plan2NativeAggressiveAdditiveError(
                "card-has-no-effect-slots", self.card_id
            )
        if not 0 <= self.target_slot_index < len(self.ordered_slots):
            raise Plan2NativeAggressiveAdditiveError(
                "target-slot-outside-card", self.version_ref
            )
        if self.target_family not in TARGET_FAMILIES:
            raise Plan2NativeAggressiveAdditiveError(
                "unknown-target-family", self.target_family
            )
        if self.target_effect.effect_turn != -1 and self.target_effect.effect_turn < 1:
            raise Plan2NativeAggressiveAdditiveError(
                "target-turn-must-be-positive-or-unlimited", self.version_ref
            )
        indexes = tuple(slot.slot_index for slot in self.ordered_slots)
        if indexes != tuple(range(len(indexes))):
            raise Plan2NativeAggressiveAdditiveError(
                "card-slot-order-is-not-contiguous", self.version_ref
            )
        if any(
            blocker.ref != self.version_ref
            for blocker in self.companion_blockers
        ):
            raise Plan2NativeAggressiveAdditiveError(
                "blocker-ref-does-not-match-program", self.version_ref
            )

    @property
    def version_ref(self) -> tuple[str, int]:
        return (self.card_id, self.upgrade)

    @property
    def ref(self) -> tuple[str, int]:
        return self.version_ref

    @property
    def card(self) -> "Plan2NativeAggressiveAdditiveProgram":
        return self

    @property
    def version_ref_text(self) -> str:
        return f"{self.card_id}#{self.upgrade}"

    @property
    def target_slot(self) -> Plan2NativeAggressiveAdditiveSlot:
        return self.ordered_slots[self.target_slot_index]

    @property
    def target_effect(self) -> Plan2NativeAggressiveAdditiveEffect:
        return self.target_slot.effect

    @property
    def target_effect_id(self) -> str:
        return self.target_effect.effect_id

    @property
    def effect(self) -> Plan2NativeAggressiveAdditiveEffect:
        return self.target_effect

    @property
    def effect_id(self) -> str:
        return self.target_effect_id

    @property
    def slot_index(self) -> int:
        return self.target_slot_index

    @property
    def target_effect_type(self) -> str:
        return self.target_effect.effect_type

    @property
    def value1(self) -> int:
        return self.target_effect.value1

    @property
    def value2(self) -> int:
        return self.target_effect.value2

    @property
    def effect_count(self) -> int:
        return self.target_effect.effect_count

    @property
    def count(self) -> int:
        return self.effect_count

    @property
    def permil(self) -> int:
        return self.value1

    @property
    def turn(self) -> int:
        return self.effect_turn

    @property
    def is_permanent(self) -> bool:
        return self.effect_turn < 0

    @property
    def effect_turn(self) -> int:
        return self.target_effect.effect_turn

    @property
    def lifetime(self) -> str:
        return self.target_effect.lifetime

    @property
    def prior_effect_ids(self) -> tuple[str, ...]:
        return tuple(
            slot.effect_id for slot in self.ordered_slots[: self.target_slot_index]
        )

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return tuple(slot.effect_id for slot in self.ordered_slots)

    @property
    def supported_play_origins(self) -> tuple[str, ...]:
        return SUPPORTED_PLAY_ORIGINS

    @property
    def play_origins(self) -> tuple[str, ...]:
        return self.supported_play_origins

    @property
    def target_effect_executable(self) -> bool:
        return True

    @property
    def whole_card_executable(self) -> bool:
        return not self.companion_blockers

    @property
    def target_executable(self) -> bool:
        return self.target_effect_executable

    def to_dict(self) -> dict[str, object]:
        return {
            "versionRef": self.version_ref_text,
            "cardId": self.card_id,
            "upgrade": self.upgrade,
            "targetFamily": self.target_family,
            "targetEffectId": self.target_effect_id,
            "targetEffectType": self.target_effect_type,
            "targetSlotIndex": self.target_slot_index,
            "value1": self.value1,
            "value1Permille": self.value1,
            "value2": self.value2,
            "count": self.effect_count,
            "turn": self.effect_turn,
            "lifetime": self.lifetime,
            "orderedEffectIds": list(self.ordered_effect_ids),
            "priorEffectIds": list(self.prior_effect_ids),
            "supportedPlayOrigins": list(self.supported_play_origins),
            "statusEnchantIds": [],
            "targetEffectExecutable": self.target_effect_executable,
            "wholeCardExecutable": self.whole_card_executable,
            "companionBlockers": [
                blocker.to_dict() for blocker in self.companion_blockers
            ],
            "companionBlockerFamilies": sorted(
                {blocker.family for blocker in self.companion_blockers}
            ),
            "ownedCompanionFamilies": list(self.owned_companion_families),
        }


@dataclass(frozen=True, slots=True)
class Plan2NativeAggressiveAdditiveCatalog:
    programs: tuple[Plan2NativeAggressiveAdditiveProgram, ...]
    effects: Mapping[str, Plan2NativeAggressiveAdditiveEffect]
    database: str = ""
    exact_trigger_ids: tuple[str, ...] = (P182_TRIGGER_ID,)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise Plan2NativeAggressiveAdditiveError(
                "unsupported-catalog-schema", str(self.schema_version)
            )
        refs = tuple(program.version_ref for program in self.programs)
        if refs != tuple(sorted(refs)) or len(refs) != len(set(refs)):
            raise Plan2NativeAggressiveAdditiveError(
                "catalog-program-order-or-uniqueness"
            )
        if any(
            not isinstance(effect_id, str)
            or not isinstance(effect, Plan2NativeAggressiveAdditiveEffect)
            for effect_id, effect in self.effects.items()
        ):
            raise Plan2NativeAggressiveAdditiveError(
                "catalog-effect-map-shape"
            )
        if any(
            program.target_effect_id not in self.effects
            for program in self.programs
        ):
            raise Plan2NativeAggressiveAdditiveError(
                "catalog-target-effect-not-indexed"
            )

    @property
    def affected_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(program.version_ref for program in self.programs)

    @property
    def affected_version_count(self) -> int:
        return len(self.programs)

    @property
    def executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.target_effect_executable_refs

    @property
    def executable_version_count(self) -> int:
        return len(self.executable_refs)

    @property
    def target_effect_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.affected_refs

    @property
    def whole_card_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            program.version_ref
            for program in self.programs
            if program.whole_card_executable
        )

    @property
    def companion_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            program.version_ref
            for program in self.programs
            if program.companion_blockers
        )

    @property
    def target_effect_executable(self) -> bool:
        return bool(self.programs)

    @property
    def whole_card_executable(self) -> bool:
        return bool(self.programs) and len(self.whole_card_executable_refs) == len(
            self.programs
        )

    def resolve(self, card_id: str, upgrade: int) -> Plan2NativeAggressiveAdditiveProgram:
        key = (card_id, upgrade)
        for program in self.programs:
            if program.version_ref == key:
                return program
        raise Plan2NativeAggressiveAdditiveError(
            "version-not-in-exact-catalog", f"{card_id}+{upgrade}"
        )

    def get(
        self, card_id: str, upgrade: int
    ) -> Plan2NativeAggressiveAdditiveProgram | None:
        try:
            return self.resolve(card_id, upgrade)
        except Plan2NativeAggressiveAdditiveError:
            return None

    def effect(self, effect_id: str) -> Plan2NativeAggressiveAdditiveEffect:
        try:
            return self.effects[effect_id]
        except KeyError as error:
            raise Plan2NativeAggressiveAdditiveError(
                "effect-not-in-exact-catalog", effect_id
            ) from error

    def to_dict(self) -> dict[str, object]:
        return {
            "adapterId": ADAPTER_ID,
            "schemaVersion": self.schema_version,
            "database": self.database,
            "affectedRefs": [
                f"{card_id}+{upgrade}"
                for card_id, upgrade in self.affected_refs
            ],
            "programs": [program.to_dict() for program in self.programs],
            "effects": {
                effect_id: effect.to_dict()
                for effect_id, effect in sorted(self.effects.items())
            },
            "exactTriggerIds": list(self.exact_trigger_ids),
            "targetEffectExecutableRefs": [
                f"{card_id}+{upgrade}"
                for card_id, upgrade in self.target_effect_executable_refs
            ],
            "wholeCardExecutableRefs": [
                f"{card_id}+{upgrade}"
                for card_id, upgrade in self.whole_card_executable_refs
            ],
        }


@dataclass(frozen=True, slots=True)
class Plan2NativeAggressiveAdditiveCompilation:
    schema_version: int
    database: str
    affected_refs: tuple[tuple[str, int], ...]
    catalog: Plan2NativeAggressiveAdditiveCatalog
    blockers: tuple[Plan2AggressiveAdditiveCompanionBlocker, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise Plan2NativeAggressiveAdditiveError(
                "unsupported-compilation-schema", str(self.schema_version)
            )
        refs = tuple(self.affected_refs)
        if len(refs) != len(set(refs)) or refs != tuple(sorted(refs)):
            raise Plan2NativeAggressiveAdditiveError(
                "affected-ref-order-or-uniqueness"
            )
        if not isinstance(self.catalog, Plan2NativeAggressiveAdditiveCatalog):
            raise TypeError("catalog must be Plan2NativeAggressiveAdditiveCatalog")
        if any(ref not in refs for ref in self.catalog.affected_refs):
            raise Plan2NativeAggressiveAdditiveError(
                "catalog-ref-outside-affected-set"
            )
        if any(
            not isinstance(blocker, Plan2AggressiveAdditiveCompanionBlocker)
            for blocker in self.blockers
        ):
            raise TypeError("blockers must be typed companion blockers")
        object.__setattr__(self, "affected_refs", refs)
        object.__setattr__(self, "blockers", tuple(self.blockers))

    @property
    def complete(self) -> bool:
        return not self.blockers and self.catalog.affected_refs == self.affected_refs

    @property
    def fully_compiled(self) -> bool:
        return self.complete

    @property
    def compiled_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.affected_refs

    @property
    def failed_refs(self) -> tuple[tuple[str, int], ...]:
        compiled = set(self.compiled_refs)
        return tuple(ref for ref in self.affected_refs if ref not in compiled)

    @property
    def compiled_version_count(self) -> int:
        return len(self.compiled_refs)

    @property
    def failed_version_count(self) -> int:
        return len(self.failed_refs)

    @property
    def executable_version_count(self) -> int:
        return len(self.catalog.target_effect_executable_refs)

    @property
    def target_effect_executable(self) -> bool:
        return (
            self.affected_version_count == 8
            and self.compiled_version_count == self.affected_version_count
            and not self.failed_refs
        )

    @property
    def companion_blockers(self) -> tuple[Plan2AggressiveAdditiveCompanionBlocker, ...]:
        return tuple(
            blocker
            for program in self.catalog.programs
            for blocker in program.companion_blockers
        )

    @property
    def companion_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.companion_blocked_refs

    @property
    def whole_card_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.whole_card_executable_refs

    @property
    def companion_blocker_code_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for blocker in self.companion_blockers:
            counts[blocker.code] = counts.get(blocker.code, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def companion_family_version_counts(self) -> dict[str, int]:
        refs_by_family: dict[str, set[tuple[str, int]]] = {}
        for blocker in self.companion_blockers:
            refs_by_family.setdefault(blocker.family, set()).add(blocker.ref)
        return {
            family: len(refs)
            for family, refs in sorted(refs_by_family.items())
        }

    def blockers_for(
        self, ref: tuple[str, int]
    ) -> tuple[Plan2AggressiveAdditiveCompanionBlocker, ...]:
        return tuple(blocker for blocker in self.blockers if blocker.ref == ref)

    @property
    def target_effect_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.target_effect_executable_refs

    @property
    def whole_card_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.catalog.whole_card_executable_refs

    @property
    def affected_version_count(self) -> int:
        return len(self.affected_refs)

    def to_dict(self) -> dict[str, object]:
        return {
            "adapterId": ADAPTER_ID,
            "schemaVersion": self.schema_version,
            "database": self.database,
            "complete": self.complete,
            "affectedRefs": [
                f"{card_id}+{upgrade}"
                for card_id, upgrade in self.affected_refs
            ],
            "catalog": self.catalog.to_dict(),
            "compileBlockers": [blocker.to_dict() for blocker in self.blockers],
        }


def _old_effect_to_effect(
    effect: ExactEffect,
) -> Plan2NativeAggressiveAdditiveEffect:
    return Plan2NativeAggressiveAdditiveEffect(
        effect_id=effect.effect_id,
        effect_type=effect.effect_type,
        value1=effect.value1,
        value2=effect.value2,
        effect_count=effect.effect_count,
        effect_turn=effect.effect_turn,
        effect_group_ids=effect.effect_group_ids,
        chain_effect_id=effect.chain_effect_id,
        status_enchant_id=effect.status_enchant_id,
        search_id=effect.search_id,
    )


def _p182_expected_effects() -> tuple[ExactEffect, ...]:
    return (
        *tuple(
            ExactEffect(
                effect_id,
                AGGRESSIVE_EFFECT_TYPE,
                value,
                0,
                0,
                0,
                effect_group_ids=(P182_EFFECT_GROUP,),
            )
            for effect_id, value in zip(
                P182_AGGRESSIVE_EFFECT_IDS, P182_AGGRESSIVE_VALUES
            )
        ),
        ExactEffect(
            P182_ADDITIVE_EFFECT_ID,
            P182_ADDITIVE_EFFECT_TYPE,
            500,
            0,
            0,
            3,
            effect_group_ids=(P182_EFFECT_GROUP,),
        ),
        ExactEffect(
            P182_STAMINA_EFFECT_ID,
            P182_STAMINA_EFFECT_TYPE,
            P182_STAMINA_EFFECT_VALUE,
            0,
            0,
            P182_STAMINA_EFFECT_TURN,
            effect_group_ids=(P182_STAMINA_EFFECT_GROUP,),
        ),
    )


def _p182_card_raw_expected(
    *,
    card_id: str,
    upgrade: int,
    stamina: int,
    play_effects: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "id": card_id,
        "upgradeCount": upgrade,
        "planType": "ProducePlanType_Plan2",
        "category": "ProduceCardCategory_MentalSkill",
        "stamina": stamina,
        "forceStamina": 0,
        "costType": P182_COST_TYPE,
        "costValue": P182_COST_VALUE,
        "playProduceExamTriggerId": P182_TRIGGER_ID,
        "playEffects": play_effects,
        "playMovePositionType": P182_CARD_MOVE_POSITION,
        "moveEffectTriggerType": "ProduceCardMoveEffectTriggerType_Unknown",
        "moveProduceExamEffectIds": [],
        "isEndTurnLost": False,
        "isInitial": False,
        "isRestrict": False,
        "produceCardStatusEnchantId": "",
        "effectGroupIds": [P182_STAMINA_EFFECT_GROUP, P182_EFFECT_GROUP],
    }


def _p182_link(effect_id: str) -> dict[str, object]:
    return {
        "produceExamEffectId": effect_id,
        "produceExamTriggerId": "",
        "hideIcon": False,
        "isOncePlayEffect": False,
    }


def _load_p182_programs(
    database: Path | str,
) -> tuple[
    tuple[Plan2NativeAggressiveAdditiveProgram, ...],
    dict[str, Plan2NativeAggressiveAdditiveEffect],
]:
    expected_effects = {
        effect.effect_id: effect for effect in _p182_expected_effects()
    }
    programs: list[Plan2NativeAggressiveAdditiveProgram] = []
    effects: dict[str, Plan2NativeAggressiveAdditiveEffect] = {}

    try:
        trigger_catalog = load_plan2_turn_progress_trigger_catalog(database)
        if trigger_catalog.target.id != P182_TRIGGER_ID:
            raise Plan2NativeAggressiveAdditiveError(
                "altered-turn-progress-trigger", P182_TRIGGER_ID
            )
        with sqlite3.connect(Path(database)) as connection:
            connection.row_factory = sqlite3.Row
            for expected in expected_effects.values():
                _load_effect(connection, expected)
                effects[expected.effect_id] = _old_effect_to_effect(expected)

            for upgrade, stamina in enumerate(P182_STAMINA_BY_UPGRADE):
                row = connection.execute(
                    "SELECT * FROM card WHERE id = ? AND upgrade_count = ?",
                    (P182_CARD_ID, upgrade),
                ).fetchone()
                if row is None:
                    raise Plan2NativeAggressiveAdditiveError(
                        "missing-card-row", f"{P182_CARD_ID}+{upgrade}"
                    )
                projection = (
                    row["plan_type"],
                    row["category"],
                    row["stamina"],
                    row["cost_type"],
                    row["cost_value"],
                    row["play_trigger_id"],
                    row["move_position_type"],
                )
                wanted = (
                    "ProducePlanType_Plan2",
                    "ProduceCardCategory_MentalSkill",
                    stamina,
                    P182_COST_TYPE,
                    P182_COST_VALUE,
                    P182_TRIGGER_ID,
                    P182_CARD_MOVE_POSITION,
                )
                if projection != wanted:
                    raise Plan2NativeAggressiveAdditiveError(
                        "altered-card-row", f"{P182_CARD_ID}+{upgrade}"
                    )
                play_effects = _json_array(
                    row["play_effects_json"],
                    f"{P182_CARD_ID}+{upgrade}.playEffects",
                )
                links: list[dict[str, object]] = []
                for slot_index, value in enumerate(play_effects):
                    if not isinstance(value, dict):
                        raise Plan2NativeAggressiveAdditiveError(
                            "altered-card-slot-shape",
                            f"{P182_CARD_ID}+{upgrade}:{slot_index}",
                        )
                    expected_link = _p182_link(
                        (
                            P182_AGGRESSIVE_EFFECT_ID_BY_UPGRADE[upgrade]
                            if slot_index == 0
                            else P182_ADDITIVE_EFFECT_ID
                            if slot_index == 1
                            else P182_STAMINA_EFFECT_ID
                        )
                    )
                    _strict_payload(
                        value,
                        expected_link,
                        frozenset(
                            {
                                "produceExamEffectId",
                                "produceExamTriggerId",
                                "hideIcon",
                                "isOncePlayEffect",
                            }
                        ),
                        f"{P182_CARD_ID}+{upgrade}.playEffects[{slot_index}]",
                    )
                    links.append(value)
                expected_links = [
                    _p182_link(P182_AGGRESSIVE_EFFECT_ID_BY_UPGRADE[upgrade]),
                    _p182_link(P182_ADDITIVE_EFFECT_ID),
                    _p182_link(P182_STAMINA_EFFECT_ID),
                ]
                if links != expected_links:
                    raise Plan2NativeAggressiveAdditiveError(
                        "altered-card-effect-order",
                        f"{P182_CARD_ID}+{upgrade}",
                    )
                raw = _json_object(
                    row["raw_json"], f"{P182_CARD_ID}+{upgrade}"
                )
                _strict_payload(
                    raw,
                    _p182_card_raw_expected(
                        card_id=P182_CARD_ID,
                        upgrade=upgrade,
                        stamina=stamina,
                        play_effects=expected_links,
                    ),
                    _CARD_RAW_KEYS,
                    f"{P182_CARD_ID}+{upgrade}",
                )
                aggressive_effect = effects[
                    P182_AGGRESSIVE_EFFECT_ID_BY_UPGRADE[upgrade]
                ]
                additive_effect = effects[P182_ADDITIVE_EFFECT_ID]
                stamina_effect = effects[P182_STAMINA_EFFECT_ID]
                slots = (
                    Plan2NativeAggressiveAdditiveSlot(
                        0, aggressive_effect
                    ),
                    Plan2NativeAggressiveAdditiveSlot(
                        1, additive_effect
                    ),
                    Plan2NativeAggressiveAdditiveSlot(
                        2, stamina_effect
                    ),
                )
                ref = (P182_CARD_ID, upgrade)
                blockers = (
                    Plan2AggressiveAdditiveCompanionBlocker(
                        "companion-adapter-pending",
                        f"{ref[0]}#{ref[1]}",
                        "plan2_cost",
                        "Unknown/value0 card payment is outside this target adapter",
                    ),
                    Plan2AggressiveAdditiveCompanionBlocker(
                        "companion-adapter-pending",
                        f"{ref[0]}#{ref[1]}",
                        "turn_progress_trigger",
                        "card-owned current-turn > 2 pre-payment gate is not installed here",
                        P182_TRIGGER_ID,
                    ),
                    Plan2AggressiveAdditiveCompanionBlocker(
                        "companion-adapter-pending",
                        f"{ref[0]}#{ref[1]}",
                        "stamina_consumption_down_fix",
                        "slot 2 is a separate stamina-down status family",
                        P182_STAMINA_EFFECT_ID,
                    ),
                )
                programs.append(
                    Plan2NativeAggressiveAdditiveProgram(
                        card_id=P182_CARD_ID,
                        upgrade=upgrade,
                        plan_type="ProducePlanType_Plan2",
                        category="ProduceCardCategory_MentalSkill",
                        stamina=stamina,
                        cost_type=P182_COST_TYPE,
                        cost_value=P182_COST_VALUE,
                        play_trigger_id=P182_TRIGGER_ID,
                        move_position=P182_CARD_MOVE_POSITION,
                        ordered_slots=slots,
                        target_slot_index=1,
                        target_family="additive",
                        companion_blockers=blockers,
                        owned_companion_families=("aggressive_event_hook",),
                        effect_group_ids=(P182_EFFECT_GROUP,),
                    )
                )
    except (
        sqlite3.Error,
        Plan2AggressiveAdditiveIntervalError,
        KeyError,
        IndexError,
        TypeError,
    ) as error:
        raise Plan2NativeAggressiveAdditiveError(
            "p182-master-validation-failed", str(error)
        ) from error
    except NativeFormulaDomainError as error:
        raise Plan2NativeAggressiveAdditiveError(
            "p182-native-domain-failed", str(error)
        ) from error
    return tuple(programs), effects


def _load_p201_programs(
    database: Path | str,
) -> tuple[
    tuple[Plan2NativeAggressiveAdditiveProgram, ...],
    dict[str, Plan2NativeAggressiveAdditiveEffect],
]:
    try:
        source = load_plan2_aggressive_additive_interval5_catalog(database)
    except (sqlite3.Error, Plan2AggressiveAdditiveIntervalError) as error:
        raise Plan2NativeAggressiveAdditiveError(
            "p201-master-validation-failed", str(error)
        ) from error
    effects = {
        effect_id: _old_effect_to_effect(effect)
        for effect_id, effect in source.effects.items()
    }
    programs: list[Plan2NativeAggressiveAdditiveProgram] = []
    for version in source.versions:
        ref = (P201_CARD_ID, version.upgrade)
        slots = tuple(
            Plan2NativeAggressiveAdditiveSlot(
                slot.slot,
                effects[slot.effect_id],
                slot.trigger_id,
                slot.hide_icon,
                slot.is_once,
            )
            for slot in version.slots
        )
        blockers = (
            Plan2AggressiveAdditiveCompanionBlocker(
                "companion-adapter-pending",
                f"{ref[0]}#{ref[1]}",
                "plan2_cost",
                "Unknown/value0 card payment is outside this target adapter",
            ),
            Plan2AggressiveAdditiveCompanionBlocker(
                "companion-adapter-pending",
                f"{ref[0]}#{ref[1]}",
                "timer_draw",
                "timer draw and delayed child dispatch are not installed by this target row",
                TIMER_EFFECT_ID,
            ),
            Plan2AggressiveAdditiveCompanionBlocker(
                "companion-adapter-pending",
                f"{ref[0]}#{ref[1]}",
                "status_enchant_encore",
                "Encore status installation is a companion slot",
                ENCORE_EFFECT_ID,
            ),
            Plan2AggressiveAdditiveCompanionBlocker(
                "companion-adapter-pending",
                f"{ref[0]}#{ref[1]}",
                "force_play_search",
                "Encore's ForcePlay search child is a companion executor",
                FORCE_PLAY_EFFECT_ID,
            ),
        )
        programs.append(
            Plan2NativeAggressiveAdditiveProgram(
                card_id=P201_CARD_ID,
                upgrade=version.upgrade,
                plan_type="ProducePlanType_Plan2",
                category="ProduceCardCategory_MentalSkill",
                stamina=version.stamina,
                cost_type=version.cost_type,
                cost_value=version.cost_value,
                play_trigger_id="",
                move_position=version.move_position,
                ordered_slots=slots,
                target_slot_index=1,
                target_family="fix",
                companion_blockers=blockers,
                owned_companion_families=(
                    "aggressive_event_hook",
                    "aggressive_interval5",
                ),
                effect_group_ids=version.effect_group_ids,
            )
        )
    return tuple(programs), effects


def _all_affected_refs() -> tuple[tuple[str, int], ...]:
    refs = [
        *((P182_CARD_ID, upgrade) for upgrade in range(4)),
        *((P201_CARD_ID, upgrade) for upgrade in range(4)),
    ]
    return tuple(sorted(refs))


def compile_plan2_native_catalog_aggressive_additive(
    database: Path | str = DEFAULT_DATABASE,
) -> Plan2NativeAggressiveAdditiveCompilation:
    """Compile only the eight exact target versions into a frozen catalog."""

    affected_refs = _all_affected_refs()
    programs: list[Plan2NativeAggressiveAdditiveProgram] = []
    effects: dict[str, Plan2NativeAggressiveAdditiveEffect] = {}
    blockers: list[Plan2AggressiveAdditiveCompanionBlocker] = []
    database_text = str(Path(database))

    try:
        p182_programs, p182_effects = _load_p182_programs(database)
        programs.extend(p182_programs)
        effects.update(p182_effects)
    except Plan2NativeAggressiveAdditiveError as error:
        for upgrade in range(4):
            blockers.append(
                Plan2AggressiveAdditiveCompanionBlocker(
                    error.code,
                    f"{P182_CARD_ID}#{upgrade}",
                    "catalog_compile",
                    error.detail or "P182 exact shape did not validate",
                )
            )

    try:
        p201_programs, p201_effects = _load_p201_programs(database)
        programs.extend(p201_programs)
        effects.update(p201_effects)
    except Plan2NativeAggressiveAdditiveError as error:
        for upgrade in range(4):
            blockers.append(
                Plan2AggressiveAdditiveCompanionBlocker(
                    error.code,
                    f"{P201_CARD_ID}#{upgrade}",
                    "catalog_compile",
                    error.detail or "P201 exact shape did not validate",
                )
            )

    programs.sort(key=lambda program: program.version_ref)
    catalog = Plan2NativeAggressiveAdditiveCatalog(
        tuple(programs),
        _FrozenMap(dict(sorted(effects.items()))),
        database=database_text,
    )
    return Plan2NativeAggressiveAdditiveCompilation(
        schema_version=SCHEMA_VERSION,
        database=database_text,
        affected_refs=affected_refs,
        catalog=catalog,
        blockers=tuple(blockers),
    )


def load_plan2_native_catalog_aggressive_additive(
    database: Path | str = DEFAULT_DATABASE,
) -> Plan2NativeAggressiveAdditiveCatalog:
    compilation = compile_plan2_native_catalog_aggressive_additive(database)
    if not compilation.complete:
        first = compilation.blockers[0] if compilation.blockers else None
        detail = first.to_dict() if first else "incomplete target set"
        raise Plan2NativeAggressiveAdditiveError(
            "catalog-not-complete", repr(detail)
        )
    return compilation.catalog


@dataclass(frozen=True, slots=True)
class AggressiveAdditiveLayer:
    uid: int
    permille: int
    turn: int
    install_sequence: int
    source_effect_id: str = P182_ADDITIVE_EFFECT_ID
    # Explicitly constructed existing layers retain their historic Main-start
    # default. Native restore supplies the actual serializer flag; a fresh
    # installation below starts false, as the game constructor does.
    passing_turn_start: bool = True
    native_rid: int | None = None
    native_source_sha256: str = ""

    def __post_init__(self) -> None:
        _positive(self.uid, "multiple.uid")
        _i32(self.permille, "multiple.permille")
        _i32(self.turn, "multiple.turn")
        _positive(self.install_sequence, "multiple.install_sequence")
        _bool(self.passing_turn_start, "multiple.passing_turn_start")
        if self.native_rid is not None:
            _positive(self.native_rid, "multiple.native_rid")
            if (self.source_effect_id or len(self.native_source_sha256) != 64
                    or any(c not in "0123456789abcdef" for c in self.native_source_sha256)):
                raise Plan2NativeAggressiveAdditiveError("native-layer-provenance-invalid")
        elif self.native_source_sha256:
            raise Plan2NativeAggressiveAdditiveError("native-layer-rid-missing")
        elif self.source_effect_id not in {
            P182_ADDITIVE_EFFECT_ID,
            PITEM_STATUS_CHANGE_ADDITIVE_EFFECT_ID,
        }:
            raise Plan2NativeAggressiveAdditiveError(
                "unknown-multiple-source-effect", self.source_effect_id
            )

    @property
    def is_turn_limited(self) -> bool:
        return self.turn >= 0


@dataclass(frozen=True, slots=True)
class Plan2AggressiveAdditiveListener:
    uid: int
    source_id: str
    captured_guid: str
    child_effect_ids: tuple[str, ...]
    install_sequence: int
    phase_count: int = 0
    max_uses: int | None = None
    max_uses_per_turn: int | None = None
    uses: int = 0
    uses_this_turn: int = 0
    trigger_id: str = "e_trigger-exam_aggressive_up_interval-5-exam_card_play_aggressive"
    phase_type: str = "ProduceExamPhaseType_ExamAggressiveUpInterval"
    interval: int = 5
    effect_types: tuple[str, ...] = (AGGRESSIVE_EFFECT_TYPE,)

    def __post_init__(self) -> None:
        _positive(self.uid, "listener.uid")
        _text(self.source_id, "listener.source_id")
        _text(self.captured_guid, "listener.captured_guid")
        _positive(self.install_sequence, "listener.install_sequence")
        _i32(self.phase_count, "listener.phase_count")
        if (
            self.trigger_id
            != "e_trigger-exam_aggressive_up_interval-5-exam_card_play_aggressive"
            or self.phase_type
            != "ProduceExamPhaseType_ExamAggressiveUpInterval"
            or self.interval != 5
            or self.effect_types != (AGGRESSIVE_EFFECT_TYPE,)
        ):
            raise Plan2NativeAggressiveAdditiveError(
                "altered-interval-listener-shape", str(self.uid)
            )
        if not self.child_effect_ids or any(
            effect_id not in {FORCE_PLAY_EFFECT_ID, P201_AGGRESSIVE_EFFECT_ID}
            for effect_id in self.child_effect_ids
        ):
            raise Plan2NativeAggressiveAdditiveError(
                "unsupported-interval-child-shape", str(self.uid)
            )
        for value, label in (
            (self.max_uses, "listener.max_uses"),
            (self.max_uses_per_turn, "listener.max_uses_per_turn"),
        ):
            if value is not None:
                _positive(value, label)
        _nonnegative(self.uses, "listener.uses")
        _nonnegative(self.uses_this_turn, "listener.uses_this_turn")
        if self.max_uses is not None and self.uses > self.max_uses:
            raise Plan2NativeAggressiveAdditiveError(
                "listener-use-overflow", str(self.uid)
            )
        if (
            self.max_uses_per_turn is not None
            and self.uses_this_turn > self.max_uses_per_turn
        ):
            raise Plan2NativeAggressiveAdditiveError(
                "listener-turn-use-overflow", str(self.uid)
            )

    @property
    def can_fire(self) -> bool:
        return (
            (self.max_uses is None or self.uses < self.max_uses)
            and (
                self.max_uses_per_turn is None
                or self.uses_this_turn < self.max_uses_per_turn
            )
        )


@dataclass(frozen=True, slots=True)
class Plan2AggressiveAdditiveQueuedChild:
    command_id: str
    event_sequence: int
    listener_uid: int
    listener_install_sequence: int
    child_index: int
    effect_id: str
    effect_type: str
    cause_origin: Plan2AggressiveAdditivePlayOrigin
    cause_transaction_id: str
    captured_guid: str

    def __post_init__(self) -> None:
        _text(self.command_id, "child.command_id")
        _positive(self.event_sequence, "child.event_sequence")
        _positive(self.listener_uid, "child.listener_uid")
        _positive(
            self.listener_install_sequence, "child.listener_install_sequence"
        )
        _nonnegative(self.child_index, "child.child_index")
        if self.effect_id == FORCE_PLAY_EFFECT_ID:
            expected = FORCE_PLAY_EFFECT_TYPE
        elif self.effect_id in AGGRESSIVE_EFFECT_IDS:
            expected = AGGRESSIVE_EFFECT_TYPE
        else:
            raise Plan2NativeAggressiveAdditiveError(
                "unknown-queued-child-effect", self.effect_id
            )
        if self.effect_type != expected:
            raise Plan2NativeAggressiveAdditiveError(
                "queued-child-type-mismatch", self.effect_id
            )
        _text(self.cause_transaction_id, "child.cause_transaction_id")
        _text(self.captured_guid, "child.captured_guid")

    @property
    def kind(self) -> Literal["force-play", "aggressive-child"]:
        return (
            "force-play"
            if self.effect_type == FORCE_PLAY_EFFECT_TYPE
            else "aggressive-child"
        )


@dataclass(frozen=True, slots=True)
class Plan2AggressiveAdditiveRuntime:
    catalog: Plan2NativeAggressiveAdditiveCatalog
    aggressive: int = 0
    additive_layers: tuple[AggressiveAdditiveLayer, ...] = ()
    additive_fix_layers: tuple[AggressiveAdditiveFixLayer, ...] = ()
    listeners: tuple[Plan2AggressiveAdditiveListener, ...] = ()
    pending_children: tuple[Plan2AggressiveAdditiveQueuedChild, ...] = ()
    next_status_uid: int = 1
    next_install_sequence: int = 1
    event_sequence: int = 0
    turn_index: int = 0
    history: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        _nonnegative(self.aggressive, "runtime.aggressive")
        _positive(self.next_status_uid, "runtime.next_status_uid")
        _positive(self.next_install_sequence, "runtime.next_install_sequence")
        _nonnegative(self.event_sequence, "runtime.event_sequence")
        _nonnegative(self.turn_index, "runtime.turn_index")
        all_layers = (*self.additive_layers, *self.additive_fix_layers)
        uids = tuple(layer.uid for layer in all_layers)
        sequences = tuple(
            (*[layer.install_sequence for layer in all_layers],)
            + tuple(listener.install_sequence for listener in self.listeners)
        )
        if len(uids) != len(set(uids)):
            raise Plan2NativeAggressiveAdditiveError(
                "duplicate-status-uid"
            )
        if len(sequences) != len(set(sequences)):
            raise Plan2NativeAggressiveAdditiveError(
                "duplicate-install-sequence"
            )
        if any(layer.turn == 0 for layer in all_layers):
            raise Plan2NativeAggressiveAdditiveError(
                "zero-turn-status-is-not-supported"
            )
        if any(
            self.next_status_uid <= uid for uid in uids
        ):
            raise Plan2NativeAggressiveAdditiveError(
                "next-status-uid-is-not-fresh"
            )
        if any(
            self.next_install_sequence <= sequence
            for sequence in sequences
        ):
            raise Plan2NativeAggressiveAdditiveError(
                "next-install-sequence-is-not-fresh"
            )
        if tuple(sorted(self.additive_layers, key=lambda layer: layer.install_sequence)) != self.additive_layers:
            raise Plan2NativeAggressiveAdditiveError(
                "multiple-layer-order-is-not-stable"
            )
        if tuple(sorted(self.additive_fix_layers, key=lambda layer: layer.install_sequence)) != self.additive_fix_layers:
            raise Plan2NativeAggressiveAdditiveError(
                "fix-layer-order-is-not-stable"
            )
        if tuple(sorted(self.listeners, key=lambda listener: listener.install_sequence)) != self.listeners:
            raise Plan2NativeAggressiveAdditiveError(
                "listener-order-is-not-stable"
            )

    @property
    def additive_fix(self) -> int:
        try:
            return native_checked_sum(
                tuple(layer.value for layer in self.additive_fix_layers)
            )
        except Plan2AggressiveAdditiveIntervalOverflow as error:
            raise Plan2NativeAggressiveAdditiveOverflow(
                "checked-additive-sum-overflow", str(error)
            ) from error

    @property
    def additive_multiple(self) -> float:
        multiple = f32(1.0)
        try:
            for layer in self.additive_layers:
                multiple = f32(
                    multiple
                    + f32(permille_to_f32(layer.permille))
                )
            return multiple
        except NativeFormulaDomainError as error:
            raise Plan2NativeAggressiveAdditiveError(
                "native-multiple-domain-failed", str(error)
            ) from error

    @property
    def status_uids(self) -> tuple[int, ...]:
        return tuple(
            layer.uid
            for layer in (*self.additive_layers, *self.additive_fix_layers)
        )

    @classmethod
    def empty(
        cls,
        catalog: Plan2NativeAggressiveAdditiveCatalog,
        *,
        aggressive: int = 0,
    ) -> "Plan2AggressiveAdditiveRuntime":
        return cls(catalog=catalog, aggressive=aggressive)


@dataclass(frozen=True, slots=True)
class Plan2AggressiveAdditiveInstallTransition:
    before: Plan2AggressiveAdditiveRuntime
    after: Plan2AggressiveAdditiveRuntime
    program: Plan2NativeAggressiveAdditiveProgram
    play_origin: Plan2AggressiveAdditivePlayOrigin
    resolved: bool
    committed: bool
    blocked: bool
    merged: bool
    status_uid: int | None
    value_before: int | None
    value_after: int | None
    reason: str = ""
    operations: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return self.resolved and self.committed

    @property
    def created_status_uid(self) -> int | None:
        return self.status_uid if self.committed and not self.merged else None

    @property
    def merged_status_uid(self) -> int | None:
        return self.status_uid if self.committed and self.merged else None

    @property
    def installed_status_uid(self) -> int:
        if self.status_uid is None:
            raise Plan2NativeAggressiveAdditiveError(
                "install-did-not-create-or-merge-status"
            )
        return self.status_uid

    @property
    def stack_order_after(self) -> tuple[int, ...]:
        return self.after.status_uids

    @property
    def multiplier_after(self) -> float:
        return self.after.additive_multiple


@dataclass(frozen=True, slots=True)
class Plan2AggressiveAdditiveEvent:
    base_value: int
    play_origin: Plan2AggressiveAdditivePlayOrigin | str = (
        Plan2AggressiveAdditivePlayOrigin.ORDINARY
    )
    transaction_id: str = "event"
    effect_id: str = P182_AGGRESSIVE_EFFECT_IDS[0]
    effect_type: str = AGGRESSIVE_EFFECT_TYPE
    blocked: bool = False
    enchant_trigger_active: bool = False
    is_effect_value_fixed: bool = False

    def __post_init__(self) -> None:
        _i32(self.base_value, "event.base_value")
        _normalize_origin(self.play_origin)
        _text(self.transaction_id, "event.transaction_id")
        _text(self.effect_id, "event.effect_id")
        if self.effect_type != AGGRESSIVE_EFFECT_TYPE:
            raise Plan2NativeAggressiveAdditiveError(
                "event-effect-type-mismatch", self.effect_type
            )
        _bool(self.blocked, "event.blocked")
        _bool(self.enchant_trigger_active, "event.enchant_trigger_active")
        _bool(self.is_effect_value_fixed, "event.is_effect_value_fixed")


@dataclass(frozen=True, slots=True)
class Plan2AggressiveAdditiveEventTransition:
    before: Plan2AggressiveAdditiveRuntime
    after: Plan2AggressiveAdditiveRuntime
    event: Plan2AggressiveAdditiveEvent
    resolved: bool
    succeeded: bool
    aggressive_before: int
    aggressive_after: int
    additive_fix: int
    additive_multiple: float
    applied_value: int
    phase_incremented: bool
    queued_children: tuple[Plan2AggressiveAdditiveQueuedChild, ...] = ()
    reason: str = ""
    operations: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return self.resolved and self.succeeded


@dataclass(frozen=True, slots=True)
class Plan2AggressiveAdditiveTurnTransition:
    before: Plan2AggressiveAdditiveRuntime
    after: Plan2AggressiveAdditiveRuntime
    turn_index: int
    decremented_status_uids: tuple[int, ...]
    expired_status_uids: tuple[int, ...]
    unlimited_status_uids: tuple[int, ...]
    reset_listener_uids: tuple[int, ...]
    operations: tuple[str, ...] = ()

    @property
    def fresh_status_uids(self) -> tuple[int, ...]:
        return ()

    @property
    def spent_status_uids(self) -> tuple[int, ...]:
        return self.decremented_status_uids

    @property
    def permanent_status_uids(self) -> tuple[int, ...]:
        return self.unlimited_status_uids

    @property
    def stack_order_after(self) -> tuple[int, ...]:
        return self.after.status_uids


@dataclass(frozen=True, slots=True)
class Plan2AggressiveAdditiveChildTransition:
    before: Plan2AggressiveAdditiveRuntime
    after: Plan2AggressiveAdditiveRuntime
    command: Plan2AggressiveAdditiveQueuedChild
    resolved: bool
    succeeded: bool
    event_transition: Plan2AggressiveAdditiveEventTransition | None = None
    reason: str = ""


def _new_uid(
    runtime: Plan2AggressiveAdditiveRuntime,
    requested_uid: int | None,
) -> int:
    if requested_uid is None:
        return runtime.next_status_uid
    uid = _positive(requested_uid, "requested_uid")
    if uid in runtime.status_uids:
        raise Plan2NativeAggressiveAdditiveError(
            "status-uid-already-installed", str(uid)
        )
    return uid


def _next_counter(value: int, label: str) -> int:
    if value >= INT32_MAX:
        raise Plan2NativeAggressiveAdditiveError(
            "counter-would-leave-signed-int32", label
        )
    return value + 1


def _next_status_counter(
    runtime: Plan2AggressiveAdditiveRuntime,
    status_uid: int,
    requested_uid: int | None,
) -> int:
    if requested_uid is None:
        return _next_counter(status_uid, "next_status_uid")
    if status_uid >= INT32_MAX:
        raise Plan2NativeAggressiveAdditiveError(
            "counter-would-leave-signed-int32", "next_status_uid"
        )
    return max(runtime.next_status_uid, status_uid + 1)


def install_plan2_native_aggressive_additive(
    runtime: Plan2AggressiveAdditiveRuntime,
    program: Plan2NativeAggressiveAdditiveProgram,
    *,
    play_origin: Plan2AggressiveAdditivePlayOrigin | str = (
        Plan2AggressiveAdditivePlayOrigin.ORDINARY
    ),
    completed_effect_ids: tuple[str, ...] = (),
    uid: int | None = None,
    blocked: bool = False,
) -> Plan2AggressiveAdditiveInstallTransition:
    """Install one exact target slot after its exact preceding slots."""

    if not isinstance(runtime, Plan2AggressiveAdditiveRuntime):
        raise TypeError("runtime must be Plan2AggressiveAdditiveRuntime")
    if not isinstance(program, Plan2NativeAggressiveAdditiveProgram):
        raise TypeError("program must be a typed catalog program")
    origin = _normalize_origin(play_origin)
    _bool(blocked, "blocked")
    if tuple(completed_effect_ids) != program.prior_effect_ids:
        return Plan2AggressiveAdditiveInstallTransition(
            runtime,
            runtime,
            program,
            origin,
            resolved=False,
            committed=False,
            blocked=blocked,
            merged=False,
            status_uid=None,
            value_before=None,
            value_after=None,
            reason="prior-card-slots-not-exactly-complete",
            operations=("fail-closed-before-status-read",),
        )
    if program.target_effect_type not in {
        P182_ADDITIVE_EFFECT_TYPE,
        FIX_EFFECT_TYPE,
    }:
        return Plan2AggressiveAdditiveInstallTransition(
            runtime,
            runtime,
            program,
            origin,
            resolved=False,
            committed=False,
            blocked=blocked,
            merged=False,
            status_uid=None,
            value_before=None,
            value_after=None,
            reason="unsupported-target-effect-type",
            operations=("fail-closed-before-status-read",),
        )
    if blocked:
        return Plan2AggressiveAdditiveInstallTransition(
            runtime,
            runtime,
            program,
            origin,
            resolved=True,
            committed=False,
            blocked=True,
            merged=False,
            status_uid=None,
            value_before=None,
            value_after=None,
            reason="effect-blocked-before-mutation",
            operations=("resolve-exact-slot", "block-gate",),
        )

    try:
        if program.target_family == "additive":
            old_layers = runtime.additive_layers
            same_indexes = [
                index
                for index, layer in enumerate(old_layers)
                if layer.turn == program.effect_turn
            ]
            if same_indexes:
                index = same_indexes[-1]
                old = old_layers[index]
                new_value = native_add_and_clamp_zero(
                    old.permille, program.value1
                )
                new_layer = replace(old, permille=new_value)
                new_layers = old_layers[:index] + (new_layer,) + old_layers[index + 1 :]
                next_uid = runtime.next_status_uid
                next_sequence = runtime.next_install_sequence
                status_uid = old.uid
                merged = True
                value_before = old.permille
                operations = (
                    "resolve-exact-slot",
                    "read-additive-multiple",
                    "same-turn-last-layer-merge",
                    "native-add-and-clamp-zero",
                )
            else:
                status_uid = _new_uid(runtime, uid)
                sequence = runtime.next_install_sequence
                new_layer = AggressiveAdditiveLayer(
                    status_uid,
                    program.value1,
                    program.effect_turn,
                    sequence,
                    program.effect_id,
                    passing_turn_start=False,
                )
                new_layers = old_layers + (new_layer,)
                next_uid = _next_status_counter(runtime, status_uid, uid)
                next_sequence = _next_counter(sequence, "next_install_sequence")
                merged = False
                value_before = None
                operations = (
                    "resolve-exact-slot",
                    "read-additive-multiple",
                    "append-fresh-layer",
                )
            after = replace(
                runtime,
                additive_layers=new_layers,
                next_status_uid=next_uid,
                next_install_sequence=next_sequence,
            )
        else:
            old_layers = runtime.additive_fix_layers
            same_indexes = [
                index
                for index, layer in enumerate(old_layers)
                if layer.turn == program.effect_turn
            ]
            if same_indexes:
                index = same_indexes[-1]
                old = old_layers[index]
                new_value = native_add_and_clamp_zero(
                    old.value, program.value1
                )
                new_layer = replace(old, value=new_value)
                new_layers = old_layers[:index] + (new_layer,) + old_layers[index + 1 :]
                next_uid = runtime.next_status_uid
                next_sequence = runtime.next_install_sequence
                status_uid = old.uid
                merged = True
                value_before = old.value
                operations = (
                    "resolve-exact-slot",
                    "read-additive-fix-sum",
                    "same-turn-last-layer-merge",
                    "native-add-and-clamp-zero",
                )
            else:
                status_uid = _new_uid(runtime, uid)
                sequence = runtime.next_install_sequence
                new_layer = AggressiveAdditiveFixLayer(
                    status_uid,
                    program.value1,
                    program.effect_turn,
                    sequence,
                    passing_turn_start=False,
                )
                new_layers = old_layers + (new_layer,)
                next_uid = _next_status_counter(runtime, status_uid, uid)
                next_sequence = _next_counter(sequence, "next_install_sequence")
                merged = False
                value_before = None
                operations = (
                    "resolve-exact-slot",
                    "read-additive-fix-sum",
                    "append-fresh-layer",
                )
            after = replace(
                runtime,
                additive_fix_layers=new_layers,
                next_status_uid=next_uid,
                next_install_sequence=next_sequence,
            )
        if program.target_family == "additive":
            _ = after.additive_multiple
            value_after = next(
                layer.permille
                for layer in after.additive_layers
                if layer.uid == status_uid
            )
        else:
            value_after = after.additive_fix
    except (
        Plan2NativeAggressiveAdditiveError,
        Plan2AggressiveAdditiveIntervalError,
    ) as error:
        return Plan2AggressiveAdditiveInstallTransition(
            runtime,
            runtime,
            program,
            origin,
            resolved=False,
            committed=False,
            blocked=False,
            merged=False,
            status_uid=None,
            value_before=None,
            value_after=None,
            reason=str(error),
            operations=("fail-closed-atomic-install",),
        )
    return Plan2AggressiveAdditiveInstallTransition(
        runtime,
        after,
        program,
        origin,
        resolved=True,
        committed=True,
        blocked=False,
        merged=merged,
        status_uid=status_uid,
        value_before=value_before,
        value_after=int(value_after),
        operations=operations,
    )


def install_plan2_native_aggressive_additive_listener(
    runtime: Plan2AggressiveAdditiveRuntime,
    *,
    source_id: str,
    captured_guid: str,
    child_effect_ids: tuple[str, ...] = (FORCE_PLAY_EFFECT_ID,),
    uid: int | None = None,
    max_uses: int | None = None,
    max_uses_per_turn: int | None = None,
) -> Plan2AggressiveAdditiveRuntime:
    """Install the exact interval5 listener used by the P201 companion."""

    status_uid = _new_uid(runtime, uid)
    sequence = runtime.next_install_sequence
    listener = Plan2AggressiveAdditiveListener(
        uid=status_uid,
        source_id=source_id,
        captured_guid=captured_guid,
        child_effect_ids=tuple(child_effect_ids),
        install_sequence=sequence,
        max_uses=max_uses,
        max_uses_per_turn=max_uses_per_turn,
    )
    return replace(
        runtime,
        listeners=runtime.listeners + (listener,),
        next_status_uid=_next_status_counter(runtime, status_uid, uid),
        next_install_sequence=_next_counter(
            sequence, "next_install_sequence"
        ),
    )


def _resolve_aggressive_effect(
    runtime: Plan2AggressiveAdditiveRuntime,
    effect_id: str,
) -> Plan2NativeAggressiveAdditiveEffect:
    if effect_id not in AGGRESSIVE_EFFECT_IDS:
        raise Plan2NativeAggressiveAdditiveError(
            "unknown-aggressive-effect-id", effect_id
        )
    effect = runtime.catalog.effect(effect_id)
    if effect.effect_type != AGGRESSIVE_EFFECT_TYPE:
        raise Plan2NativeAggressiveAdditiveError(
            "aggressive-effect-type-mismatch", effect_id
        )
    return effect


def _apply_plan2_native_aggressive_additive_event(
    runtime: Plan2AggressiveAdditiveRuntime,
    event: Plan2AggressiveAdditiveEvent,
    *,
    master_effect_bound: bool,
) -> Plan2AggressiveAdditiveEventTransition:
    """Apply the current Aggressive hook with native getter/order semantics."""

    if not isinstance(runtime, Plan2AggressiveAdditiveRuntime):
        raise TypeError("runtime must be Plan2AggressiveAdditiveRuntime")
    if not isinstance(event, Plan2AggressiveAdditiveEvent):
        raise TypeError("event must be Plan2AggressiveAdditiveEvent")
    origin = _normalize_origin(event.play_origin)
    aggressive_before = runtime.aggressive
    try:
        if not master_effect_bound:
            _resolve_aggressive_effect(runtime, event.effect_id)
        additive_fix = 0 if event.is_effect_value_fixed else runtime.additive_fix
        additive_multiple = (
            f32(1.0)
            if event.is_effect_value_fixed
            else runtime.additive_multiple
        )
        if event.blocked:
            return Plan2AggressiveAdditiveEventTransition(
                runtime,
                runtime,
                event,
                resolved=True,
                succeeded=False,
                aggressive_before=aggressive_before,
                aggressive_after=aggressive_before,
                additive_fix=additive_fix,
                additive_multiple=additive_multiple,
                applied_value=0,
                phase_incremented=False,
                reason="effect-blocked-before-mutation",
                operations=("resolve-aggressive", "block-gate"),
            )
        base_plus_fix = _i32_wrap(event.base_value + additive_fix)
        multiplied = f32(f32(base_plus_fix) * f32(additive_multiple))
        applied_value = ceil_f32_to_i32(multiplied)
        aggressive_after = native_add_and_clamp_zero(
            aggressive_before, applied_value
        )
    except (
        Plan2NativeAggressiveAdditiveError,
        Plan2NativeAggressiveAdditiveOverflow,
        NativeFormulaDomainError,
        Plan2AggressiveAdditiveIntervalError,
    ) as error:
        return Plan2AggressiveAdditiveEventTransition(
            runtime,
            runtime,
            event,
            resolved=False,
            succeeded=False,
            aggressive_before=aggressive_before,
            aggressive_after=aggressive_before,
            additive_fix=0,
            additive_multiple=f32(1.0),
            applied_value=0,
            phase_incremented=False,
            reason=str(error),
            operations=("fail-closed-atomic-event",),
        )

    event_sequence = _next_counter(
        runtime.event_sequence, "runtime.event_sequence"
    )
    listeners = list(runtime.listeners)
    queued: list[Plan2AggressiveAdditiveQueuedChild] = []
    phase_incremented = False
    operations = [
        "resolve-aggressive",
        "read-additive-fix-once",
        "read-additive-multiple-once",
        "int32-base-plus-fix",
        "float32-multiply",
        "ceil-to-int32",
        "native-add-and-clamp-zero",
    ]
    if event.enchant_trigger_active:
        phase_incremented = bool(listeners)
        operations.append("read-enchant-trigger-context")
        operations.append("increment-phase-count-once")
        for index, listener in enumerate(runtime.listeners):
            new_phase_count = _i32_wrap(listener.phase_count + 1)
            updated = replace(
                listener,
                phase_count=new_phase_count,
            )
            listeners[index] = updated
            if (
                new_phase_count > 0
                and new_phase_count % listener.interval == 0
                and listener.can_fire
            ):
                updated = replace(
                    updated,
                    uses=updated.uses + 1,
                    uses_this_turn=updated.uses_this_turn + 1,
                )
                listeners[index] = updated
                for child_index, child_id in enumerate(
                    listener.child_effect_ids
                ):
                    child_type = (
                        FORCE_PLAY_EFFECT_TYPE
                        if child_id == FORCE_PLAY_EFFECT_ID
                        else AGGRESSIVE_EFFECT_TYPE
                    )
                    command = Plan2AggressiveAdditiveQueuedChild(
                        command_id=(
                            f"phase53:{event_sequence}:"
                            f"listener:{listener.uid}:child:{child_index}"
                        ),
                        event_sequence=event_sequence,
                        listener_uid=listener.uid,
                        listener_install_sequence=listener.install_sequence,
                        child_index=child_index,
                        effect_id=child_id,
                        effect_type=child_type,
                        cause_origin=origin,
                        cause_transaction_id=event.transaction_id,
                        captured_guid=listener.captured_guid,
                    )
                    queued.append(command)
        total_exhausted = {
            listener.uid
            for listener in listeners
            if listener.max_uses is not None
            and listener.uses >= listener.max_uses
        }
        if total_exhausted:
            listeners = [
                listener
                for listener in listeners
                if listener.uid not in total_exhausted
            ]
            operations.append("remove-total-exhausted-after-queue-batch")
        if queued:
            operations.append("queue-children-stable-list-order")
    after = replace(
        runtime,
        aggressive=aggressive_after,
        listeners=tuple(listeners),
        pending_children=runtime.pending_children + tuple(queued),
        event_sequence=event_sequence,
    )
    return Plan2AggressiveAdditiveEventTransition(
        runtime,
        after,
        event,
        resolved=True,
        succeeded=True,
        aggressive_before=aggressive_before,
        aggressive_after=aggressive_after,
        additive_fix=additive_fix,
        additive_multiple=additive_multiple,
        applied_value=applied_value,
        phase_incremented=phase_incremented,
        queued_children=tuple(queued),
        operations=tuple(operations),
    )


def apply_plan2_native_aggressive_additive_event(
    runtime: Plan2AggressiveAdditiveRuntime,
    event: Plan2AggressiveAdditiveEvent,
) -> Plan2AggressiveAdditiveEventTransition:
    """Apply an event whose effect identity belongs to this slice's catalog."""

    return _apply_plan2_native_aggressive_additive_event(
        runtime,
        event,
        master_effect_bound=False,
    )


def apply_plan2_native_bound_aggressive_additive_event(
    runtime: Plan2AggressiveAdditiveRuntime,
    event: Plan2AggressiveAdditiveEvent,
) -> Plan2AggressiveAdditiveEventTransition:
    """Apply an already Master-compiled Aggressive scalar operation.

    The full Plan2 program compiler owns the effect row and ordered card slot,
    so the smaller aggressive-additive catalog need not duplicate every
    possible Aggressive effect ID merely to apply active additive layers.
    Event type/value/lifecycle arithmetic remains identical to the catalog
    entrypoint.
    """

    return _apply_plan2_native_aggressive_additive_event(
        runtime,
        event,
        master_effect_bound=True,
    )


def apply_plan2_native_bound_aggressive_gain(runtime, *, aggressive, value, count=1,
                                            effect_id, play_origin="normal", enchant_trigger_active=False):
    """Common scalar gain using the existing additive/fix/float32 owner.

    This seam deliberately has no phase-53 command executor. A caller needing
    those listeners must use their complete existing owner, not discard a
    queued forced play or counter update to obtain a scalar estimate.
    """
    if runtime.listeners or runtime.pending_children:
        raise Plan2NativeAggressiveAdditiveError("aggressive-gain-listener-continuation-unbound", effect_id)
    _positive(count, "gain.count")
    working = replace(runtime, aggressive=_nonnegative(aggressive, "gain.aggressive"))
    for index in range(count):
        applied = apply_plan2_native_bound_aggressive_additive_event(working, Plan2AggressiveAdditiveEvent(
            base_value=value, play_origin=play_origin, effect_id=effect_id,
            transaction_id=f"bound-gain:{effect_id}:{index}", enchant_trigger_active=enchant_trigger_active))
        if not applied.executable:
            raise Plan2NativeAggressiveAdditiveError("aggressive-gain-failed-closed", applied.reason or effect_id)
        working = applied.after
    return working


def execute_plan2_native_aggressive_additive_child(
    runtime: Plan2AggressiveAdditiveRuntime,
    command_id: str,
) -> Plan2AggressiveAdditiveChildTransition:
    _text(command_id, "command_id")
    try:
        command = next(
            command
            for command in runtime.pending_children
            if command.command_id == command_id
        )
    except StopIteration as error:
        raise Plan2NativeAggressiveAdditiveError(
            "queued-child-not-found", command_id
        ) from error
    remaining = tuple(
        command_row
        for command_row in runtime.pending_children
        if command_row.command_id != command_id
    )
    handed = replace(runtime, pending_children=remaining)
    if command.kind == "force-play":
        return Plan2AggressiveAdditiveChildTransition(
            runtime,
            handed,
            command,
            resolved=True,
            succeeded=False,
            reason="force-play-child-handoff-required",
        )
    effect = _resolve_aggressive_effect(handed, command.effect_id)
    event = Plan2AggressiveAdditiveEvent(
        base_value=effect.value1,
        play_origin=command.cause_origin,
        transaction_id=f"{command.cause_transaction_id}:child:{command.child_index}",
        effect_id=command.effect_id,
        effect_type=effect.effect_type,
        enchant_trigger_active=False,
    )
    event_transition = apply_plan2_native_aggressive_additive_event(
        handed, event
    )
    return Plan2AggressiveAdditiveChildTransition(
        runtime,
        event_transition.after,
        command,
        resolved=True,
        succeeded=event_transition.succeeded,
        event_transition=event_transition,
        reason=event_transition.reason,
    )


def advance_plan2_native_aggressive_additive_turn(
    runtime: Plan2AggressiveAdditiveRuntime,
) -> Plan2AggressiveAdditiveTurnTransition:
    """Spend finite statuses at the immutable turn boundary."""

    next_turn = _next_counter(runtime.turn_index, "runtime.turn_index")
    decremented: list[int] = []
    expired: list[int] = []
    unlimited: list[int] = []
    multiple_layers: list[AggressiveAdditiveLayer] = []
    for layer in runtime.additive_layers:
        if layer.turn < 0:
            unlimited.append(layer.uid)
            multiple_layers.append(replace(layer, passing_turn_start=True))
        elif not layer.passing_turn_start:
            multiple_layers.append(replace(layer, passing_turn_start=True))
        else:
            decremented.append(layer.uid)
            remaining = layer.turn - 1
            if remaining <= 0:
                expired.append(layer.uid)
            else:
                multiple_layers.append(replace(layer, turn=remaining))
    fix_layers: list[AggressiveAdditiveFixLayer] = []
    for layer in runtime.additive_fix_layers:
        if layer.turn < 0:
            unlimited.append(layer.uid)
            fix_layers.append(replace(layer, passing_turn_start=True))
        elif not layer.passing_turn_start:
            fix_layers.append(replace(layer, passing_turn_start=True))
        else:
            decremented.append(layer.uid)
            remaining = layer.turn - 1
            if remaining <= 0:
                expired.append(layer.uid)
            else:
                fix_layers.append(replace(layer, turn=remaining))
    listeners = tuple(
        replace(listener, uses_this_turn=0)
        for listener in runtime.listeners
    )
    after = replace(
        runtime,
        additive_layers=tuple(multiple_layers),
        additive_fix_layers=tuple(fix_layers),
        listeners=listeners,
        turn_index=next_turn,
    )
    return Plan2AggressiveAdditiveTurnTransition(
        runtime,
        after,
        next_turn,
        tuple(decremented),
        tuple(expired),
        tuple(unlimited),
        tuple(listener.uid for listener in runtime.listeners),
        (
            "turn-boundary",
            "spend-only-finite-statuses-that-passed-MainStart",
            "mark-survivors-passing-at-next-MainStart",
            "preserve-unlimited-negative-turns",
            "expire-zero-turn-statuses",
            "reset-listener-per-turn-uses",
        ),
    )


def finish_plan2_native_aggressive_additive_turn(
    runtime: Plan2AggressiveAdditiveRuntime,
) -> Plan2AggressiveAdditiveTurnTransition:
    """End-turn projection; additive statuses spend only at TurnStart."""

    if not isinstance(runtime, Plan2AggressiveAdditiveRuntime):
        raise TypeError("runtime must be Plan2AggressiveAdditiveRuntime")
    return Plan2AggressiveAdditiveTurnTransition(
        before=runtime,
        after=runtime,
        turn_index=runtime.turn_index,
        decremented_status_uids=(),
        expired_status_uids=(),
        unlimited_status_uids=tuple(
            layer.uid
            for layer in (*runtime.additive_layers, *runtime.additive_fix_layers)
            if layer.turn < 0
        ),
        reset_listener_uids=(),
        operations=("turn-end-noop-for-additive-statuses",),
    )


@dataclass(frozen=True, slots=True)
class Plan2AggressiveAdditiveHandoffRow:
    program: Plan2NativeAggressiveAdditiveProgram

    @property
    def version_ref(self) -> tuple[str, int]:
        return self.program.version_ref

    @property
    def card_id(self) -> str:
        return self.program.card_id

    @property
    def upgrade(self) -> int:
        return self.program.upgrade

    @property
    def target_effect_id(self) -> str:
        return self.program.target_effect_id

    @property
    def target_slot_index(self) -> int:
        return self.program.target_slot_index

    @property
    def value1_permille(self) -> int:
        return self.program.value1

    @property
    def value2(self) -> int:
        return self.program.value2

    @property
    def count(self) -> int:
        return self.program.effect_count

    @property
    def turn(self) -> int:
        return self.program.effect_turn

    @property
    def lifetime(self) -> str:
        return self.program.lifetime

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return self.program.ordered_effect_ids

    @property
    def supported_play_origins(self) -> tuple[str, ...]:
        return self.program.supported_play_origins

    @property
    def companion_blockers(self) -> tuple[Plan2AggressiveAdditiveCompanionBlocker, ...]:
        return self.program.companion_blockers

    @property
    def target_effect_executable(self) -> bool:
        return self.program.target_effect_executable

    @property
    def whole_card_executable(self) -> bool:
        return self.program.whole_card_executable

    def to_dict(self) -> dict[str, object]:
        return self.program.to_dict()


@dataclass(frozen=True, slots=True)
class Plan2AggressiveAdditiveHandoff:
    rows: tuple[Plan2AggressiveAdditiveHandoffRow, ...]
    affected_refs: tuple[tuple[str, int], ...]
    target_effect_executable_refs: tuple[tuple[str, int], ...]
    whole_card_executable_refs: tuple[tuple[str, int], ...]
    companion_blockers: tuple[Plan2AggressiveAdditiveCompanionBlocker, ...]
    owned_companion_families: tuple[str, ...]
    accounting: Mapping[str, int]

    @property
    def affected_version_count(self) -> int:
        return len(self.affected_refs)

    @property
    def executable_refs(self) -> tuple[tuple[str, int], ...]:
        return self.target_effect_executable_refs

    @property
    def executable_version_count(self) -> int:
        return len(self.executable_refs)

    @property
    def companion_blocked_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            row.version_ref
            for row in self.rows
            if row.companion_blockers
        )

    @property
    def companion_blocker_code_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for blocker in self.companion_blockers:
            counts[blocker.code] = counts.get(blocker.code, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def companion_family_version_counts(self) -> dict[str, int]:
        refs_by_family: dict[str, set[tuple[str, int]]] = {}
        for blocker in self.companion_blockers:
            refs_by_family.setdefault(blocker.family, set()).add(blocker.ref)
        return {
            family: len(refs)
            for family, refs in sorted(refs_by_family.items())
        }

    @property
    def target_effect_executable(self) -> bool:
        return len(self.target_effect_executable_refs) == self.affected_version_count

    @property
    def whole_card_executable(self) -> bool:
        return len(self.whole_card_executable_refs) == self.affected_version_count

    def to_dict(self) -> dict[str, object]:
        return {
            "adapterId": ADAPTER_ID,
            "schemaVersion": SCHEMA_VERSION,
            "affectedVersionCount": self.affected_version_count,
            "affectedRefs": [
                f"{card_id}+{upgrade}"
                for card_id, upgrade in self.affected_refs
            ],
            "targetEffectExecutableRefs": [
                f"{card_id}+{upgrade}"
                for card_id, upgrade in self.target_effect_executable_refs
            ],
            "wholeCardExecutableRefs": [
                f"{card_id}+{upgrade}"
                for card_id, upgrade in self.whole_card_executable_refs
            ],
            "targetEffectExecutable": self.target_effect_executable,
            "wholeCardExecutable": self.whole_card_executable,
            "companionBlockers": [
                blocker.to_dict() for blocker in self.companion_blockers
            ],
            "ownedCompanionFamilies": list(self.owned_companion_families),
            "accounting": dict(self.accounting),
            "rows": [row.to_dict() for row in self.rows],
        }


def build_plan2_native_aggressive_additive_handoff(
    source: Plan2NativeAggressiveAdditiveCompilation
    | Plan2NativeAggressiveAdditiveCatalog,
) -> Plan2AggressiveAdditiveHandoff:
    if isinstance(source, Plan2NativeAggressiveAdditiveCompilation):
        if not source.complete:
            raise Plan2NativeAggressiveAdditiveError(
                "handoff-requires-complete-catalog",
                repr([blocker.to_dict() for blocker in source.blockers]),
            )
        catalog = source.catalog
        affected_refs = source.affected_refs
    elif isinstance(source, Plan2NativeAggressiveAdditiveCatalog):
        catalog = source
        affected_refs = catalog.affected_refs
    else:
        raise TypeError("source must be a typed compilation or catalog")
    if len(affected_refs) != 8:
        raise Plan2NativeAggressiveAdditiveError(
            "handoff-affected-set-is-not-eight", str(len(affected_refs))
        )
    if catalog.affected_refs != affected_refs:
        raise Plan2NativeAggressiveAdditiveError(
            "handoff-affected-ref-order-mismatch"
        )
    rows = tuple(
        Plan2AggressiveAdditiveHandoffRow(program)
        for program in catalog.programs
    )
    blockers = tuple(
        blocker
        for program in catalog.programs
        for blocker in program.companion_blockers
    )
    owned = tuple(
        sorted(
            {
                family
                for program in catalog.programs
                for family in program.owned_companion_families
            }
        )
    )
    family_version_refs: dict[str, set[tuple[str, int]]] = {}
    for program in catalog.programs:
        for blocker in program.companion_blockers:
            family_version_refs.setdefault(blocker.family, set()).add(
                program.version_ref
            )
    accounting: dict[str, int] = {
        "affected8": len(affected_refs),
        "targetEffectExecutable": len(catalog.target_effect_executable_refs),
        "companionBlocked": len(catalog.companion_blocked_refs),
        "wholeCardExecutable": len(catalog.whole_card_executable_refs),
        "companionBlockerOccurrences": len(blockers),
    }
    for family, refs in sorted(family_version_refs.items()):
        accounting[f"blockerVersions:{family}"] = len(refs)
    return Plan2AggressiveAdditiveHandoff(
        rows=rows,
        affected_refs=affected_refs,
        target_effect_executable_refs=catalog.target_effect_executable_refs,
        whole_card_executable_refs=catalog.whole_card_executable_refs,
        companion_blockers=blockers,
        owned_companion_families=owned,
        accounting=_FrozenMap(accounting),
    )


def exact_plan2_native_aggressive_additive_rows(
    source: Plan2NativeAggressiveAdditiveCompilation
    | Plan2NativeAggressiveAdditiveCatalog,
) -> tuple[dict[str, object], ...]:
    return tuple(
        row.to_dict()
        for row in build_plan2_native_aggressive_additive_handoff(source).rows
    )


def plan2_native_aggressive_additive_catalog_summary(
    source: Plan2NativeAggressiveAdditiveCompilation
    | Plan2NativeAggressiveAdditiveCatalog,
) -> dict[str, object]:
    handoff = build_plan2_native_aggressive_additive_handoff(source)
    return handoff.to_dict()


# Compact aliases keep the lifecycle vocabulary discoverable without adding a
# second implementation surface.
compile_aggressive_additive_catalog = (
    compile_plan2_native_catalog_aggressive_additive
)
load_aggressive_additive_catalog = load_plan2_native_catalog_aggressive_additive
install_aggressive_additive = install_plan2_native_aggressive_additive
apply_aggressive_additive_event = (
    apply_plan2_native_aggressive_additive_event
)
advance_aggressive_additive_turn = (
    advance_plan2_native_aggressive_additive_turn
)
advance_aggressive_additive_turn_end = finish_plan2_native_aggressive_additive_turn
build_aggressive_additive_handoff = (
    build_plan2_native_aggressive_additive_handoff
)
build_plan2_aggressive_additive_central_handoff = (
    build_plan2_native_aggressive_additive_handoff
)
build_plan2_native_aggressive_additive_central_handoff = (
    build_plan2_native_aggressive_additive_handoff
)
build_aggressive_additive_central_handoff = (
    build_plan2_native_aggressive_additive_handoff
)
resolve_plan2_native_aggressive_additive_event = (
    apply_plan2_native_aggressive_additive_event
)
resolve_aggressive_additive_event = apply_plan2_native_aggressive_additive_event
simulate_plan2_native_aggressive_additive_turn_start = (
    advance_plan2_native_aggressive_additive_turn
)
simulate_plan2_native_aggressive_additive_turn_end = (
    finish_plan2_native_aggressive_additive_turn
)
handoff_plan2_native_aggressive_additive = (
    build_plan2_native_aggressive_additive_handoff
)
catalog_to_dict = plan2_native_aggressive_additive_catalog_summary


__all__ = [
    "ADAPTER_ID",
    "AGGRESSIVE_EFFECT_IDS",
    "PLAN2_NATIVE_CATALOG_AGGRESSIVE_ADDITIVE_SCHEMA_VERSION",
    "PLAY_ORIGINS",
    "TARGET_VERSION_COUNT",
    "AggressiveAdditiveLayer",
    "Plan2AggressiveAdditiveCompanionBlocker",
    "Plan2AggressiveAdditiveEvent",
    "Plan2AggressiveAdditiveEventTransition",
    "Plan2AggressiveAdditiveHandoff",
    "Plan2AggressiveAdditiveHandoffRow",
    "Plan2AggressiveAdditiveInstallTransition",
    "Plan2AggressiveAdditiveListener",
    "Plan2AggressiveAdditivePlayOrigin",
    "Plan2AggressiveAdditiveQueuedChild",
    "Plan2AggressiveAdditiveRuntime",
    "Plan2AggressiveAdditiveTurnTransition",
    "Plan2NativeAggressiveAdditiveCatalog",
    "Plan2NativeAggressiveAdditiveCompilation",
    "Plan2NativeAggressiveAdditiveEffect",
    "Plan2NativeAggressiveAdditiveError",
    "Plan2NativeAggressiveAdditiveOverflow",
    "Plan2NativeCatalogAggressiveAdditiveContractError",
    "Plan2NativeAggressiveAdditiveProgram",
    "Plan2NativeAggressiveAdditiveSlot",
    "P182_ADDITIVE_EFFECT_ID",
    "P182_ADDITIVE_EFFECT_TYPE",
    "P182_AGGRESSIVE_EFFECT_ID_BY_UPGRADE",
    "P182_CARD_ID",
    "PITEM_STATUS_CHANGE_ADDITIVE_EFFECT_ID",
    "P201_CARD_ID",
    "P201_FIX_EFFECT_ID",
    "P201_FIX_EFFECT_TYPE",
    "FORCE_PLAY_EFFECT_ID",
    "FORCE_PLAY_EFFECT_TYPE",
    "P201_AGGRESSIVE_EFFECT_ID",
    "SUPPORTED_PLAY_ORIGINS",
    "TARGET_FAMILIES",
    "advance_aggressive_additive_turn",
    "advance_aggressive_additive_turn_end",
    "advance_plan2_native_aggressive_additive_turn",
    "apply_aggressive_additive_event",
    "apply_plan2_native_bound_aggressive_additive_event",
    "apply_plan2_native_aggressive_additive_event",
    "build_aggressive_additive_handoff",
    "build_aggressive_additive_central_handoff",
    "build_plan2_aggressive_additive_central_handoff",
    "build_plan2_native_aggressive_additive_central_handoff",
    "build_plan2_native_aggressive_additive_handoff",
    "compile_aggressive_additive_catalog",
    "compile_plan2_native_catalog_aggressive_additive",
    "exact_plan2_native_aggressive_additive_rows",
    "execute_plan2_native_aggressive_additive_child",
    "finish_plan2_native_aggressive_additive_turn",
    "install_aggressive_additive",
    "install_plan2_native_aggressive_additive",
    "install_plan2_native_aggressive_additive_listener",
    "handoff_plan2_native_aggressive_additive",
    "load_aggressive_additive_catalog",
    "load_plan2_native_catalog_aggressive_additive",
    "plan2_native_aggressive_additive_catalog_summary",
    "resolve_aggressive_additive_event",
    "resolve_plan2_native_aggressive_additive_event",
    "simulate_plan2_native_aggressive_additive_turn_end",
    "simulate_plan2_native_aggressive_additive_turn_start",
    "catalog_to_dict",
]
