"""Standalone Plan2 debuff registry and native catalog handoffs.

The leaf owns two exact executor boundaries only:

* ``ExamAntiDebuff`` installs/stacks one permanent preventive count status and
  gates later status additions by native target classification;
* ``ExamDebuffRecover`` snapshots active debuff status objects by newest UID
  and later removes the captured objects as whole objects.

Card cost, companion effects, whole-card settlement, the central native
catalog, and the search horizon stay outside this module.  All public state is
immutable so a caller may persist it between horizon nodes without retaining
an engine object.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, replace
from enum import Enum
import json
from pathlib import Path
import sqlite3
from typing import TypeAlias

from .exam_status_runtime import RuntimeExamEffect
from .master_db import DEFAULT_DATABASE
from .plan2_anti_debuff import (
    ANTI_DEBUFF_CATALOG,
    ANTI_DEBUFF_EFFECT_TYPE_VALUE,
    ANTI_DEBUFF_PERMANENT_TURN,
    AntiDebuffDifference,
    AntiDebuffEffectRow,
    AntiDebuffExecution,
    AntiDebuffRuntime,
    ExamStatusEffectTargetType as AntiDebuffTargetType,
    execute_plan2_anti_debuff,
    shared_try_block_status_addition,
)
from .plan2_debuff_recover import (
    NATIVE_BUFF_EFFECT_TYPES,
    NATIVE_BUFF_EFFECT_TYPE_VALUES,
    NATIVE_DEBUFF_EFFECT_TYPES,
    NATIVE_DEBUFF_EFFECT_TYPE_VALUES,
    NATIVE_EFFECT_TARGET_TYPE,
    NATIVE_NONE_EFFECT_TYPES,
    DebuffRecoverDifference,
    DebuffRecoverEffectRow,
    DebuffRecoverExecution,
    DebuffRecoverPlan,
    DebuffRecoverState,
    ExamStatusEffectTargetType,
    Plan2DebuffRecoverContractError,
    StatusEffect,
    commit_debuff_recover,
    load_plan2_debuff_recover_catalog,
    resolve_status_target_type,
    snapshot_debuff_recover,
)


EFFECT_ANTI_DEBUFF = "ProduceExamEffectType_ExamAntiDebuff"
EFFECT_DEBUFF_RECOVER = "ProduceExamEffectType_ExamDebuffRecover"
TARGET_EFFECT_TYPES = frozenset({EFFECT_ANTI_DEBUFF, EFFECT_DEBUFF_RECOVER})
PLAN_TYPES = frozenset({"ProducePlanType_Common", "ProducePlanType_Plan2"})

ANTI_DEBUFF_STATUS_NATIVE_TYPE = "Campus.InGame.Exam.AntiDebuffStatusEffect"
ANTI_DEBUFF_STATUS_INSTANCE_PREFIX = "native:anti-debuff"

CURRENT_MASTER_FAMILY_VERSION_COUNTS = {
    EFFECT_ANTI_DEBUFF: 8,
    EFFECT_DEBUFF_RECOVER: 4,
}


class Plan2NativeDebuffCatalogError(ValueError):
    """A catalog or registry shape is outside the proven native boundary."""


class PlayOrigin(str, Enum):
    NORMAL = "normal"
    FORCED = "forced"
    EXTRA = "extra"


def _i32(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if not -(2**31) <= value <= 2**31 - 1:
        raise Plan2NativeDebuffCatalogError(f"{label} is outside Int32")
    return value


def _nonnegative_i32(value: object, label: str) -> int:
    result = _i32(value, label)
    if result < 0:
        raise Plan2NativeDebuffCatalogError(f"{label} must be non-negative")
    return result


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        qualifier = "text" if empty else "non-empty text"
        raise TypeError(f"{label} must be {qualifier}")
    return value


def _json_object(value: object, label: str) -> Mapping[str, object]:
    try:
        parsed = json.loads(str(value)) if isinstance(value, str) else value
    except json.JSONDecodeError as error:
        raise Plan2NativeDebuffCatalogError(f"{label} is invalid JSON") from error
    if not isinstance(parsed, Mapping):
        raise Plan2NativeDebuffCatalogError(f"{label} must be a JSON object")
    return parsed


def _json_array(value: object, label: str) -> tuple[object, ...]:
    try:
        parsed = json.loads(str(value)) if isinstance(value, str) else value
    except json.JSONDecodeError as error:
        raise Plan2NativeDebuffCatalogError(f"{label} is invalid JSON") from error
    if not isinstance(parsed, list):
        raise Plan2NativeDebuffCatalogError(f"{label} must be a JSON array")
    return tuple(parsed)


def _json_value(value: object) -> object:
    return list(value) if isinstance(value, tuple) else value


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeDebuffEffectHandoff:
    card_id: str
    upgrade: int
    slot_index: int
    effect_id: str
    effect_type: str
    companion_effect_ids: tuple[str, ...]
    companion_effect_types: tuple[str, ...]
    card_trigger_id: str = ""

    def __post_init__(self) -> None:
        _text(self.card_id, "card_id")
        _nonnegative_i32(self.upgrade, "upgrade")
        _nonnegative_i32(self.slot_index, "slot_index")
        _text(self.effect_id, "effect_id")
        if self.effect_type not in TARGET_EFFECT_TYPES:
            raise Plan2NativeDebuffCatalogError("unknown debuff handoff family")
        if len(self.companion_effect_ids) != len(self.companion_effect_types):
            raise Plan2NativeDebuffCatalogError("companion accounting mismatch")
        if any(not isinstance(value, str) or not value for value in self.companion_effect_ids):
            raise TypeError("companion_effect_ids must contain non-empty text")
        if any(not isinstance(value, str) or not value for value in self.companion_effect_types):
            raise TypeError("companion_effect_types must contain non-empty text")
        _text(self.card_trigger_id, "card_trigger_id", empty=True)

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def target_executable(self) -> bool:
        return True

    @property
    def companion_required(self) -> bool:
        return bool(self.companion_effect_ids or self.card_trigger_id)

    @property
    def whole_card_executable_claimed(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class Plan2NativeDebuffFamilyAccounting:
    effect_type: str
    affected_versions: int
    target_executable_versions: int
    companion_required_versions: int
    whole_card_executable_claims: int = 0

    def __post_init__(self) -> None:
        if self.effect_type not in TARGET_EFFECT_TYPES:
            raise Plan2NativeDebuffCatalogError("unknown accounting family")
        for name in (
            "affected_versions",
            "target_executable_versions",
            "companion_required_versions",
            "whole_card_executable_claims",
        ):
            _nonnegative_i32(getattr(self, name), name)
        if self.target_executable_versions != self.affected_versions:
            raise Plan2NativeDebuffCatalogError("target executor accounting drift")
        if self.companion_required_versions > self.affected_versions:
            raise Plan2NativeDebuffCatalogError("companion accounting drift")
        if self.whole_card_executable_claims:
            raise Plan2NativeDebuffCatalogError(
                "this leaf must not claim whole-card execution"
            )


@dataclass(frozen=True, slots=True)
class Plan2NativeCatalogDebuff:
    master_database: str
    anti_debuff_effects: tuple[AntiDebuffEffectRow, ...]
    debuff_recover_effects: tuple[DebuffRecoverEffectRow, ...]
    handoffs: tuple[Plan2NativeDebuffEffectHandoff, ...]
    accounting: tuple[Plan2NativeDebuffFamilyAccounting, ...]

    def __post_init__(self) -> None:
        _text(self.master_database, "master_database")
        keys = [(row.ref, row.slot_index) for row in self.handoffs]
        if len(keys) != len(set(keys)):
            raise Plan2NativeDebuffCatalogError("duplicate target handoff slot")

    @property
    def version_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(row.effect_type for row in self.handoffs).items()))

    @property
    def target_executable_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted(row.ref for row in self.handoffs))

    @property
    def companion_required_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted(row.ref for row in self.handoffs if row.companion_required))

    @property
    def anti_debuff_effect_by_id(self) -> dict[str, AntiDebuffEffectRow]:
        return {row.source_effect_id: row for row in self.anti_debuff_effects}

    @property
    def debuff_recover_effect_by_id(self) -> dict[str, DebuffRecoverEffectRow]:
        return {row.id: row for row in self.debuff_recover_effects}

    def handoff_for(
        self, ref: tuple[str, int], *, effect_type: str | None = None
    ) -> Plan2NativeDebuffEffectHandoff | None:
        for row in self.handoffs:
            if row.ref == ref and (effect_type is None or row.effect_type == effect_type):
                return row
        return None


def _validate_anti_effect_rows(
    connection: sqlite3.Connection,
) -> tuple[AntiDebuffEffectRow, ...]:
    rows = ANTI_DEBUFF_CATALOG.master_effect_rows
    for expected in rows:
        master = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (expected.source_effect_id,)
        ).fetchone()
        if master is None:
            raise Plan2NativeDebuffCatalogError(
                f"missing AntiDebuff effect: {expected.source_effect_id}"
            )
        normalized = dict(master)
        raw = _json_object(
            normalized.get("raw_json"), f"{expected.source_effect_id}.raw_json"
        )
        column_checks = {
            "effect_type": expected.effect_type,
            "value1": expected.field_value("effectValue1"),
            "value2": expected.field_value("effectValue2"),
            "effect_count": expected.field_value("effectCount"),
            "effect_turn": expected.field_value("effectTurn"),
            "status_enchant_id": expected.field_value("produceExamStatusEnchantId"),
            "chain_effect_id": expected.field_value("chainProduceExamEffectId"),
        }
        for key, value in column_checks.items():
            if normalized.get(key) != value:
                raise Plan2NativeDebuffCatalogError(
                    f"{expected.source_effect_id}: normalized {key} drift"
                )
        for field in expected.fields:
            if raw.get(field.name) != _json_value(field.value):
                raise Plan2NativeDebuffCatalogError(
                    f"{expected.source_effect_id}: raw {field.name} drift"
                )
    return tuple(rows)


def _compile_anti_handoffs(
    connection: sqlite3.Connection,
) -> tuple[Plan2NativeDebuffEffectHandoff, ...]:
    handoffs: list[Plan2NativeDebuffEffectHandoff] = []
    for expected in ANTI_DEBUFF_CATALOG.affected_card_versions:
        master = connection.execute(
            """
            SELECT id, upgrade_count, plan_type, category, play_trigger_id,
                   play_effects_json, raw_json
              FROM card
             WHERE id = ? AND upgrade_count = ?
            """,
            (expected.card_id, expected.upgrade_count),
        ).fetchone()
        if master is None:
            raise Plan2NativeDebuffCatalogError(
                f"missing AntiDebuff card: {expected.card_id}@{expected.upgrade_count}"
            )
        row = dict(master)
        raw = _json_object(
            row["raw_json"], f"{expected.card_id}@{expected.upgrade_count}.raw_json"
        )
        links = _json_array(
            row["play_effects_json"],
            f"{expected.card_id}@{expected.upgrade_count}.play_effects_json",
        )
        if (
            row["id"] != expected.card_id
            or row["upgrade_count"] != expected.upgrade_count
            or row["plan_type"] != expected.plan_type
            or row["category"] != expected.category
            or row["play_trigger_id"] != ""
            or raw.get("id") != expected.card_id
            or raw.get("upgradeCount") != expected.upgrade_count
            or raw.get("planType") != expected.plan_type
            or raw.get("category") != expected.category
            or raw.get("playProduceExamTriggerId") != ""
            or raw.get("playEffects") != list(links)
            or len(links) != len(expected.effect_slots)
        ):
            raise Plan2NativeDebuffCatalogError(
                f"AntiDebuff card shape drift: {expected.card_id}@{expected.upgrade_count}"
            )
        slot_types: list[str] = []
        target_index: int | None = None
        for index, (link, slot) in enumerate(zip(links, expected.effect_slots, strict=True)):
            if not isinstance(link, Mapping):
                raise Plan2NativeDebuffCatalogError("AntiDebuff slot must be an object")
            if (
                link.get("produceExamEffectId") != slot.source_effect_id
                or link.get("produceExamTriggerId") != slot.slot_trigger_id
                or link.get("hideIcon") != slot.hide_icon
                or link.get("isOncePlayEffect") != slot.once_only
                or slot.effect_order != index
            ):
                raise Plan2NativeDebuffCatalogError(
                    f"AntiDebuff slot drift: {expected.card_id}@{expected.upgrade_count}:{index}"
                )
            effect_type_row = connection.execute(
                "SELECT effect_type FROM effect WHERE id = ?", (slot.source_effect_id,)
            ).fetchone()
            if effect_type_row is None or effect_type_row[0] != slot.effect_type:
                raise Plan2NativeDebuffCatalogError(
                    f"AntiDebuff slot effect drift: {slot.source_effect_id}"
                )
            slot_types.append(slot.effect_type)
            if slot.effect_type == EFFECT_ANTI_DEBUFF:
                if target_index is not None:
                    raise Plan2NativeDebuffCatalogError("multiple AntiDebuff target slots")
                target_index = index
        if target_index is None:
            raise Plan2NativeDebuffCatalogError("missing AntiDebuff target slot")
        target = expected.effect_slots[target_index]
        handoffs.append(
            Plan2NativeDebuffEffectHandoff(
                card_id=expected.card_id,
                upgrade=expected.upgrade_count,
                slot_index=target_index,
                effect_id=target.source_effect_id,
                effect_type=EFFECT_ANTI_DEBUFF,
                companion_effect_ids=tuple(
                    slot.source_effect_id
                    for index, slot in enumerate(expected.effect_slots)
                    if index != target_index
                ),
                companion_effect_types=tuple(
                    effect_type
                    for index, effect_type in enumerate(slot_types)
                    if index != target_index
                ),
            )
        )
    return tuple(handoffs)


def compile_plan2_native_catalog_debuff(
    *,
    database: str | Path = DEFAULT_DATABASE,
    enforce_current_master_counts: bool = True,
) -> Plan2NativeCatalogDebuff:
    """Compile the 8 AntiDebuff and 4 DebuffRecover target-version handoffs."""

    database_path = Path(database).resolve()
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    with closing(
        sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        anti_effects = _validate_anti_effect_rows(connection)
        anti_handoffs = _compile_anti_handoffs(connection)

    try:
        recover_catalog = load_plan2_debuff_recover_catalog(database=database_path)
    except Plan2DebuffRecoverContractError as error:
        raise Plan2NativeDebuffCatalogError(
            f"DebuffRecover catalog drift: {error}"
        ) from error
    recover_handoffs = tuple(
        Plan2NativeDebuffEffectHandoff(
            card_id=version.card_id,
            upgrade=version.upgrade_count,
            slot_index=version.target_slot.slot_index,
            effect_id=version.effect.id,
            effect_type=EFFECT_DEBUFF_RECOVER,
            companion_effect_ids=tuple(
                slot.effect_id
                for slot in version.ordered_slots
                if slot.slot_index != version.target_slot.slot_index
            ),
            companion_effect_types=tuple(
                (
                    "ProduceExamEffectType_ExamAggressiveValueMultiple"
                    if slot.slot_index == 0
                    else "ProduceExamEffectType_ExamEffectTimer"
                )
                for slot in version.ordered_slots
                if slot.slot_index != version.target_slot.slot_index
            ),
            card_trigger_id=version.play_trigger_id,
        )
        for version in recover_catalog.versions
    )
    recover_effects_by_id = {version.effect.id: version.effect for version in recover_catalog.versions}
    handoffs = tuple(sorted((*anti_handoffs, *recover_handoffs)))
    accounting = tuple(
        Plan2NativeDebuffFamilyAccounting(
            effect_type=effect_type,
            affected_versions=sum(row.effect_type == effect_type for row in handoffs),
            target_executable_versions=sum(
                row.effect_type == effect_type and row.target_executable for row in handoffs
            ),
            companion_required_versions=sum(
                row.effect_type == effect_type and row.companion_required for row in handoffs
            ),
        )
        for effect_type in sorted(TARGET_EFFECT_TYPES)
    )
    catalog = Plan2NativeCatalogDebuff(
        master_database=str(database_path),
        anti_debuff_effects=anti_effects,
        debuff_recover_effects=tuple(
            recover_effects_by_id[key] for key in sorted(recover_effects_by_id)
        ),
        handoffs=handoffs,
        accounting=accounting,
    )
    if enforce_current_master_counts and (
        catalog.version_counts != CURRENT_MASTER_FAMILY_VERSION_COUNTS
    ):
        raise Plan2NativeDebuffCatalogError(
            f"current Master family counts drifted: {catalog.version_counts!r}"
        )
    return catalog


load_plan2_native_catalog_debuff = compile_plan2_native_catalog_debuff


@dataclass(frozen=True, slots=True)
class Plan2NativeStatusLifetime:
    instance_id: str
    turn: int = ANTI_DEBUFF_PERMANENT_TURN
    is_passing_turn_start: bool = False

    def __post_init__(self) -> None:
        _text(self.instance_id, "lifetime.instance_id")
        _i32(self.turn, "lifetime.turn")
        if self.turn != -1 and self.turn < 1:
            raise Plan2NativeDebuffCatalogError(
                "status turn must be -1 (permanent) or positive"
            )
        if not isinstance(self.is_passing_turn_start, bool):
            raise TypeError("is_passing_turn_start must be bool")

    @property
    def permanent(self) -> bool:
        return self.turn == -1


@dataclass(frozen=True, slots=True)
class Plan2NativeDebuffRegistry:
    """Horizon-saveable active-list order, lifetime, UID, and removal state."""

    statuses: tuple[StatusEffect, ...] = ()
    lifetimes: tuple[Plan2NativeStatusLifetime, ...] = ()
    next_uid: int = 1
    recently_used_uids: tuple[int, ...] = ()
    removed_statuses: tuple[StatusEffect, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.statuses, tuple) or any(
            not isinstance(value, StatusEffect) for value in self.statuses
        ):
            raise TypeError("statuses must be a tuple of StatusEffect")
        if not isinstance(self.lifetimes, tuple) or any(
            not isinstance(value, Plan2NativeStatusLifetime) for value in self.lifetimes
        ):
            raise TypeError("lifetimes must be a tuple of Plan2NativeStatusLifetime")
        if not self.lifetimes and self.statuses:
            object.__setattr__(
                self,
                "lifetimes",
                tuple(Plan2NativeStatusLifetime(value.instance_id) for value in self.statuses),
            )
        status_ids = [value.instance_id for value in self.statuses]
        lifetime_ids = [value.instance_id for value in self.lifetimes]
        if len(status_ids) != len(set(status_ids)):
            raise Plan2NativeDebuffCatalogError("duplicate active status instance_id")
        if lifetime_ids != status_ids:
            raise Plan2NativeDebuffCatalogError(
                "lifetime order/identity must match active status order"
            )
        if not isinstance(self.removed_statuses, tuple) or any(
            not isinstance(value, StatusEffect) for value in self.removed_statuses
        ):
            raise TypeError("removed_statuses must be a tuple of StatusEffect")
        for status in (*self.statuses, *self.removed_statuses):
            _text(status.instance_id, "status.instance_id")
            _nonnegative_i32(status.uid, f"status:{status.instance_id}.uid")
            _text(status.effect_type, f"status:{status.instance_id}.effect_type")
            _i32(status.icon_value, f"status:{status.instance_id}.icon_value")
            if _i32(status.layers, f"status:{status.instance_id}.layers") < 1:
                raise Plan2NativeDebuffCatalogError("status layers must be positive")
            if not isinstance(status.active, bool):
                raise TypeError("status.active must be bool")
        _nonnegative_i32(self.next_uid, "next_uid")
        if self.next_uid < 1:
            raise Plan2NativeDebuffCatalogError("next_uid must be positive")
        known_uids = [value.uid for value in (*self.statuses, *self.removed_statuses)]
        if known_uids and self.next_uid <= max(known_uids):
            raise Plan2NativeDebuffCatalogError(
                "next_uid must exceed active and removed status UIDs"
            )
        if not isinstance(self.recently_used_uids, tuple):
            raise TypeError("recently_used_uids must be a tuple")
        for uid in self.recently_used_uids:
            _nonnegative_i32(uid, "recently_used_uid")

    @property
    def ordered_instance_ids(self) -> tuple[str, ...]:
        return tuple(value.instance_id for value in self.statuses)

    @property
    def anti_debuff_statuses(self) -> tuple[StatusEffect, ...]:
        return tuple(
            value
            for value in self.statuses
            if value.active and value.effect_type == EFFECT_ANTI_DEBUFF
        )

    @property
    def anti_debuff_status(self) -> StatusEffect | None:
        values = self.anti_debuff_statuses
        if len(values) > 1:
            raise Plan2NativeDebuffCatalogError("multiple active AntiDebuff statuses")
        return values[0] if values else None

    @property
    def anti_debuff_count(self) -> int:
        status = self.anti_debuff_status
        if status is None:
            return 0
        if (
            status.layers != 1
            or status.target_type != int(ExamStatusEffectTargetType.BUFF)
            or status.native_status_type != ANTI_DEBUFF_STATUS_NATIVE_TYPE
            or not self.lifetime_for(status.instance_id).permanent
        ):
            raise Plan2NativeDebuffCatalogError(
                "active AntiDebuff status has an unsupported native shape"
            )
        count = _nonnegative_i32(status.icon_value, "AntiDebuff count")
        if count < 1:
            raise Plan2NativeDebuffCatalogError("active AntiDebuff must have a count")
        return count

    def lifetime_for(self, instance_id: str) -> Plan2NativeStatusLifetime:
        for lifetime in self.lifetimes:
            if lifetime.instance_id == instance_id:
                return lifetime
        raise KeyError(instance_id)


@dataclass(frozen=True, slots=True)
class Plan2NativeDebuffTurnStart:
    before: Plan2NativeDebuffRegistry
    after: Plan2NativeDebuffRegistry
    permanent_status_uids: tuple[int, ...]


def advance_plan2_native_debuff_turn_start(
    registry: Plan2NativeDebuffRegistry,
) -> Plan2NativeDebuffTurnStart:
    """Expose that the AntiDebuff count status is permanent and is not spent."""

    if not isinstance(registry, Plan2NativeDebuffRegistry):
        raise TypeError("registry must be Plan2NativeDebuffRegistry")
    status = registry.anti_debuff_status
    if status is None:
        return Plan2NativeDebuffTurnStart(registry, registry, ())
    _ = registry.anti_debuff_count
    lifetime = registry.lifetime_for(status.instance_id)
    if not lifetime.permanent:
        raise Plan2NativeDebuffCatalogError("AntiDebuff lifetime must be permanent")
    return Plan2NativeDebuffTurnStart(registry, registry, (status.uid,))


StatusDifference: TypeAlias = AntiDebuffDifference | DebuffRecoverDifference


@dataclass(frozen=True, slots=True)
class Plan2NativeDebuffQueueHandoff:
    differences: tuple[StatusDifference, ...] = ()
    callback_events: tuple[str, ...] = ()
    trace: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.differences, tuple) or any(
            not isinstance(value, (AntiDebuffDifference, DebuffRecoverDifference))
            for value in self.differences
        ):
            raise TypeError("differences contains an unsupported payload")
        if not isinstance(self.callback_events, tuple) or any(
            not isinstance(value, str) or not value for value in self.callback_events
        ):
            raise TypeError("callback_events must contain non-empty text")
        if not isinstance(self.trace, tuple) or any(
            not isinstance(value, str) for value in self.trace
        ):
            raise TypeError("trace must contain text")


@dataclass(frozen=True, slots=True)
class Plan2NativeAntiDebuffExecution:
    handoff: Plan2NativeDebuffEffectHandoff
    origin: PlayOrigin
    registry_before: Plan2NativeDebuffRegistry
    registry_after: Plan2NativeDebuffRegistry
    executable: bool
    exact: AntiDebuffExecution | None
    queue_handoff: Plan2NativeDebuffQueueHandoff
    reason: str | None = None


def execute_plan2_native_anti_debuff_handoff(
    catalog: Plan2NativeCatalogDebuff,
    handoff: Plan2NativeDebuffEffectHandoff,
    registry: Plan2NativeDebuffRegistry,
    *,
    origin: PlayOrigin = PlayOrigin.NORMAL,
) -> Plan2NativeAntiDebuffExecution:
    """Install/stack one exact AntiDebuff row while preserving registry order."""

    if not isinstance(catalog, Plan2NativeCatalogDebuff):
        raise TypeError("catalog must be Plan2NativeCatalogDebuff")
    if not isinstance(handoff, Plan2NativeDebuffEffectHandoff):
        raise TypeError("handoff must be Plan2NativeDebuffEffectHandoff")
    if not isinstance(registry, Plan2NativeDebuffRegistry):
        raise TypeError("registry must be Plan2NativeDebuffRegistry")
    if not isinstance(origin, PlayOrigin):
        raise TypeError("origin must be PlayOrigin")
    if handoff not in catalog.handoffs:
        return Plan2NativeAntiDebuffExecution(
            handoff,
            origin,
            registry,
            registry,
            False,
            None,
            Plan2NativeDebuffQueueHandoff(),
            "unknown-catalog-handoff",
        )
    if handoff.effect_type != EFFECT_ANTI_DEBUFF:
        return Plan2NativeAntiDebuffExecution(
            handoff,
            origin,
            registry,
            registry,
            False,
            None,
            Plan2NativeDebuffQueueHandoff(),
            "handoff-family-mismatch",
        )
    effect = catalog.anti_debuff_effect_by_id.get(handoff.effect_id)
    if effect is None:
        return Plan2NativeAntiDebuffExecution(
            handoff,
            origin,
            registry,
            registry,
            False,
            None,
            Plan2NativeDebuffQueueHandoff(),
            "unknown-anti-debuff-effect",
        )
    try:
        before_count = registry.anti_debuff_count
    except Plan2NativeDebuffCatalogError as error:
        return Plan2NativeAntiDebuffExecution(
            handoff,
            origin,
            registry,
            registry,
            False,
            None,
            Plan2NativeDebuffQueueHandoff(),
            str(error),
        )
    exact = execute_plan2_anti_debuff(effect, AntiDebuffRuntime(before_count))
    if not isinstance(exact, AntiDebuffExecution):
        return Plan2NativeAntiDebuffExecution(
            handoff,
            origin,
            registry,
            registry,
            False,
            None,
            Plan2NativeDebuffQueueHandoff(),
            "exact-anti-debuff-unresolved",
        )
    current = registry.anti_debuff_status
    if current is None:
        uid = registry.next_uid
        status = StatusEffect(
            instance_id=f"{ANTI_DEBUFF_STATUS_INSTANCE_PREFIX}:{uid}",
            uid=uid,
            effect_type=EFFECT_ANTI_DEBUFF,
            icon_value=exact.state_after.count,
            layers=1,
            active=True,
            target_type=int(ExamStatusEffectTargetType.BUFF),
            native_status_type=ANTI_DEBUFF_STATUS_NATIVE_TYPE,
        )
        after = replace(
            registry,
            statuses=(*registry.statuses, status),
            lifetimes=(
                *registry.lifetimes,
                Plan2NativeStatusLifetime(status.instance_id, ANTI_DEBUFF_PERMANENT_TURN),
            ),
            next_uid=uid + 1,
        )
    else:
        status = replace(current, icon_value=exact.state_after.count)
        after = replace(
            registry,
            statuses=tuple(status if value is current else value for value in registry.statuses),
        )
    callbacks = (
        ("AntiDebuffStatusEffect:on-value-changed",) if exact.callback_fired else ()
    )
    queue = Plan2NativeDebuffQueueHandoff(
        differences=(exact.difference,),
        callback_events=callbacks,
        trace=(f"{origin.value}:direct:AntiDebuff", *exact.trace),
    )
    return Plan2NativeAntiDebuffExecution(
        handoff, origin, registry, after, True, exact, queue
    )


_EFFECT_TYPE_VALUE_BY_NAME = {
    **dict(zip(NATIVE_DEBUFF_EFFECT_TYPES, NATIVE_DEBUFF_EFFECT_TYPE_VALUES, strict=True)),
    **dict(zip(NATIVE_BUFF_EFFECT_TYPES, NATIVE_BUFF_EFFECT_TYPE_VALUES, strict=True)),
}


@dataclass(frozen=True, slots=True)
class Plan2StatusTargetClassification:
    effect_type: str
    resolved: bool
    target_type: ExamStatusEffectTargetType | None
    effect_type_value: int | None
    reason: str | None = None


def classify_plan2_status_target(
    incoming: str | RuntimeExamEffect | StatusEffect,
) -> Plan2StatusTargetClassification:
    """Classify one exact runtime/native status type without spelling guesses."""

    if isinstance(incoming, str):
        effect_type = incoming
    elif isinstance(incoming, (RuntimeExamEffect, StatusEffect)):
        effect_type = incoming.effect_type
    else:
        return Plan2StatusTargetClassification(
            "", False, None, None, "unsupported-incoming-status-shape"
        )
    if not effect_type:
        return Plan2StatusTargetClassification(
            effect_type, False, None, None, "empty-incoming-effect-type"
        )
    try:
        target_value = resolve_status_target_type(effect_type)
    except Plan2DebuffRecoverContractError:
        return Plan2StatusTargetClassification(
            effect_type, False, None, None, "unknown-status-effect-type"
        )
    target = ExamStatusEffectTargetType(target_value)
    effect_type_value = _EFFECT_TYPE_VALUE_BY_NAME.get(effect_type)
    if effect_type_value is None and effect_type in NATIVE_NONE_EFFECT_TYPES:
        # NONE entries do not consume AntiDebuff; their concrete enum value is
        # immaterial to this gate, but zero keeps the typed handoff explicit.
        effect_type_value = 0
    if effect_type_value is None:
        return Plan2StatusTargetClassification(
            effect_type, False, None, None, "missing-native-effect-type-value"
        )
    return Plan2StatusTargetClassification(
        effect_type, True, target, effect_type_value
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeAntiDebuffGate:
    registry_before: Plan2NativeDebuffRegistry
    registry_after: Plan2NativeDebuffRegistry
    classification: Plan2StatusTargetClassification
    resolved: bool
    blocked: bool
    consumed_count: int
    removed_status: StatusEffect | None
    queue_handoff: Plan2NativeDebuffQueueHandoff
    caller_may_add_incoming: bool
    reason: str | None = None


def gate_plan2_native_status_addition(
    registry: Plan2NativeDebuffRegistry,
    incoming: str | RuntimeExamEffect | StatusEffect,
) -> Plan2NativeAntiDebuffGate:
    """Run native AntiDebuff before an incoming status add, fail closed."""

    if not isinstance(registry, Plan2NativeDebuffRegistry):
        raise TypeError("registry must be Plan2NativeDebuffRegistry")
    classification = classify_plan2_status_target(incoming)
    if not classification.resolved:
        return Plan2NativeAntiDebuffGate(
            registry,
            registry,
            classification,
            False,
            False,
            0,
            None,
            Plan2NativeDebuffQueueHandoff(),
            False,
            classification.reason,
        )
    try:
        status = registry.anti_debuff_status
        count = registry.anti_debuff_count
    except Plan2NativeDebuffCatalogError as error:
        return Plan2NativeAntiDebuffGate(
            registry,
            registry,
            classification,
            False,
            False,
            0,
            None,
            Plan2NativeDebuffQueueHandoff(),
            False,
            str(error),
        )
    assert classification.target_type is not None
    assert classification.effect_type_value is not None
    exact = shared_try_block_status_addition(
        AntiDebuffRuntime(count),
        incoming_effect_type_value=classification.effect_type_value,
        incoming_target_type=AntiDebuffTargetType[classification.target_type.name],
    )
    if not exact.blocked:
        return Plan2NativeAntiDebuffGate(
            registry,
            registry,
            classification,
            True,
            False,
            0,
            None,
            Plan2NativeDebuffQueueHandoff(trace=exact.trace),
            True,
        )
    assert status is not None and exact.difference is not None
    if exact.removed_status:
        after = replace(
            registry,
            statuses=tuple(value for value in registry.statuses if value is not status),
            lifetimes=tuple(
                value for value in registry.lifetimes if value.instance_id != status.instance_id
            ),
            recently_used_uids=(*registry.recently_used_uids, status.uid),
            removed_statuses=(*registry.removed_statuses, status),
        )
        removed = status
    else:
        updated = replace(status, icon_value=exact.state_after.count)
        after = replace(
            registry,
            statuses=tuple(updated if value is status else value for value in registry.statuses),
            recently_used_uids=(*registry.recently_used_uids, status.uid),
        )
        removed = None
    return Plan2NativeAntiDebuffGate(
        registry,
        after,
        classification,
        True,
        True,
        1,
        removed,
        Plan2NativeDebuffQueueHandoff(
            differences=(exact.difference,), trace=exact.trace
        ),
        False,
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeDebuffRecoverSnapshot:
    handoff: Plan2NativeDebuffEffectHandoff
    origin: PlayOrigin
    registry_before: Plan2NativeDebuffRegistry
    resolved: bool
    exact_plan: DebuffRecoverPlan | None
    reason: str | None = None

    @property
    def selected_instance_ids(self) -> tuple[str, ...]:
        return (
            self.exact_plan.selected_instance_ids if self.exact_plan is not None else ()
        )


def snapshot_plan2_native_debuff_recover(
    catalog: Plan2NativeCatalogDebuff,
    handoff: Plan2NativeDebuffEffectHandoff,
    registry: Plan2NativeDebuffRegistry,
    *,
    origin: PlayOrigin = PlayOrigin.NORMAL,
) -> Plan2NativeDebuffRecoverSnapshot:
    """Capture exact active/debuff/newest/Take candidates without mutation."""

    if not isinstance(catalog, Plan2NativeCatalogDebuff):
        raise TypeError("catalog must be Plan2NativeCatalogDebuff")
    if not isinstance(handoff, Plan2NativeDebuffEffectHandoff):
        raise TypeError("handoff must be Plan2NativeDebuffEffectHandoff")
    if not isinstance(registry, Plan2NativeDebuffRegistry):
        raise TypeError("registry must be Plan2NativeDebuffRegistry")
    if not isinstance(origin, PlayOrigin):
        raise TypeError("origin must be PlayOrigin")
    if handoff not in catalog.handoffs:
        return Plan2NativeDebuffRecoverSnapshot(
            handoff, origin, registry, False, None, "unknown-catalog-handoff"
        )
    if handoff.effect_type != EFFECT_DEBUFF_RECOVER:
        return Plan2NativeDebuffRecoverSnapshot(
            handoff, origin, registry, False, None, "handoff-family-mismatch"
        )
    effect = catalog.debuff_recover_effect_by_id.get(handoff.effect_id)
    if effect is None:
        return Plan2NativeDebuffRecoverSnapshot(
            handoff, origin, registry, False, None, "unknown-debuff-recover-effect"
        )
    exact_origin = "ordinary" if origin is PlayOrigin.NORMAL else origin.value
    try:
        plan = snapshot_debuff_recover(
            effect,
            DebuffRecoverState(registry.statuses),
            play_origin=exact_origin,
        )
    except (Plan2DebuffRecoverContractError, TypeError, ValueError) as error:
        return Plan2NativeDebuffRecoverSnapshot(
            handoff,
            origin,
            registry,
            False,
            None,
            f"registry-or-effect-unresolved:{error}",
        )
    return Plan2NativeDebuffRecoverSnapshot(
        handoff, origin, registry, True, plan
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeDebuffRecoverCommit:
    snapshot: Plan2NativeDebuffRecoverSnapshot
    registry_at_commit: Plan2NativeDebuffRegistry
    registry_after: Plan2NativeDebuffRegistry
    executable: bool
    exact: DebuffRecoverExecution | None
    queue_handoff: Plan2NativeDebuffQueueHandoff
    reason: str | None = None


def commit_plan2_native_debuff_recover(
    snapshot: Plan2NativeDebuffRecoverSnapshot,
    registry_at_commit: Plan2NativeDebuffRegistry | None = None,
) -> Plan2NativeDebuffRecoverCommit:
    """Commit captured object identities; no race-time reclassification occurs."""

    if not isinstance(snapshot, Plan2NativeDebuffRecoverSnapshot):
        raise TypeError("snapshot must be Plan2NativeDebuffRecoverSnapshot")
    current = snapshot.registry_before if registry_at_commit is None else registry_at_commit
    if not isinstance(current, Plan2NativeDebuffRegistry):
        raise TypeError("registry_at_commit must be Plan2NativeDebuffRegistry")
    if not snapshot.resolved or snapshot.exact_plan is None:
        return Plan2NativeDebuffRecoverCommit(
            snapshot,
            current,
            current,
            False,
            None,
            Plan2NativeDebuffQueueHandoff(),
            snapshot.reason or "snapshot-unresolved",
        )
    try:
        exact = commit_debuff_recover(
            snapshot.exact_plan, DebuffRecoverState(current.statuses)
        )
    except (Plan2DebuffRecoverContractError, TypeError, ValueError) as error:
        return Plan2NativeDebuffRecoverCommit(
            snapshot,
            current,
            current,
            False,
            None,
            Plan2NativeDebuffQueueHandoff(),
            f"commit-unresolved:{error}",
        )
    removed_ids = {id(value) for value in exact.removed}
    after_lifetimes = tuple(
        lifetime
        for status, lifetime in zip(current.statuses, current.lifetimes, strict=True)
        if id(status) not in removed_ids
    )
    after = replace(
        current,
        statuses=exact.after.statuses,
        lifetimes=after_lifetimes,
        removed_statuses=(*current.removed_statuses, *exact.removed),
    )
    queue = Plan2NativeDebuffQueueHandoff(
        differences=exact.difference_queue,
        callback_events=exact.direct_callback_events,
        trace=(f"{snapshot.origin.value}:direct:DebuffRecover", *exact.event_trace),
    )
    return Plan2NativeDebuffRecoverCommit(
        snapshot, current, after, True, exact, queue
    )


def execute_plan2_native_debuff_recover_handoff(
    catalog: Plan2NativeCatalogDebuff,
    handoff: Plan2NativeDebuffEffectHandoff,
    registry: Plan2NativeDebuffRegistry,
    *,
    origin: PlayOrigin = PlayOrigin.NORMAL,
) -> Plan2NativeDebuffRecoverCommit:
    """Convenience snapshot+commit for the no-race executor path."""

    return commit_plan2_native_debuff_recover(
        snapshot_plan2_native_debuff_recover(
            catalog, handoff, registry, origin=origin
        )
    )


__all__ = [
    "ANTI_DEBUFF_STATUS_INSTANCE_PREFIX",
    "ANTI_DEBUFF_STATUS_NATIVE_TYPE",
    "CURRENT_MASTER_FAMILY_VERSION_COUNTS",
    "EFFECT_ANTI_DEBUFF",
    "EFFECT_DEBUFF_RECOVER",
    "PLAN_TYPES",
    "Plan2NativeAntiDebuffExecution",
    "Plan2NativeAntiDebuffGate",
    "Plan2NativeCatalogDebuff",
    "Plan2NativeDebuffCatalogError",
    "Plan2NativeDebuffEffectHandoff",
    "Plan2NativeDebuffFamilyAccounting",
    "Plan2NativeDebuffQueueHandoff",
    "Plan2NativeDebuffRecoverCommit",
    "Plan2NativeDebuffRecoverSnapshot",
    "Plan2NativeDebuffRegistry",
    "Plan2NativeDebuffTurnStart",
    "Plan2NativeStatusLifetime",
    "Plan2StatusTargetClassification",
    "PlayOrigin",
    "StatusDifference",
    "TARGET_EFFECT_TYPES",
    "advance_plan2_native_debuff_turn_start",
    "classify_plan2_status_target",
    "commit_plan2_native_debuff_recover",
    "compile_plan2_native_catalog_debuff",
    "execute_plan2_native_anti_debuff_handoff",
    "execute_plan2_native_debuff_recover_handoff",
    "gate_plan2_native_status_addition",
    "load_plan2_native_catalog_debuff",
    "snapshot_plan2_native_debuff_recover",
]
