"""Bounded Plan2/Common card gate for ``StaminaUpMultiple(500)``.

The only executable surface in this module is the direct ``play_trigger_id``
on ``p_card-00-sup-2_027`` (upgrades 0--3).  The native arithmetic remains
owned by :mod:`plan3_stamina_multiple_trigger`; this file only validates the
complete Master trigger/card shape, adapts a settled StartTurn snapshot, and
evaluates the card gate before the card transaction.

The Android v3.2.3 body reads ``ExamParameterModel.get_Stamina`` and
``get_MaxStamina``.  It converts the signed getter results and the signed
trigger value to binary32, divides by binary32 ``1000.0f``, then uses ordered
``FCMP``/``CSET GE``.  The shared evaluator deliberately fails closed for a
zero/non-positive maximum, negative current stamina, or over-cap projected
state because the surrounding native state setters prove the normal domain,
not because this module invents a clamp or a divide-by-zero result.

No direct effect, timer, draw, card upgrade, status listener, Plan2 state,
Plan3 engine, central coverage output, or GUI surface is executed here.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final

from .master_db import DEFAULT_DATABASE
from .plan3_stamina_multiple_trigger import (
    FIELD_STAMINA_UP_MULTIPLE,
    NATIVE_BRANCHES,
    NATIVE_FORMULA_EVIDENCE,
    PHASE_EXAM_START_TURN,
    StaminaField,
    StaminaMultipleEvaluation,
    StaminaMultipleTrigger,
    StaminaSnapshot,
    STAMINA_MULTIPLE_TRIGGER_BY_ID,
    evaluate_stamina_multiple,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_AUDIT_PATH = (
    PROJECT_ROOT
    / "var"
    / "coverage"
    / "plan2_start_turn_stamina_up_multiple_native_audit.json"
)

TRIGGER_ID: Final = "e_trigger-exam_start_turn-stamina_up_multiple-500"
START_TURN_TRIGGER_ID: Final = TRIGGER_ID
START_TURN_PHASE_TYPE: Final = PHASE_EXAM_START_TURN
TRIGGER_PHASE_TYPE: Final = START_TURN_PHASE_TYPE
TRIGGER_PHASE: Final = START_TURN_PHASE_TYPE
TRIGGER_FIELD_TYPE: Final = FIELD_STAMINA_UP_MULTIPLE
TRIGGER_THRESHOLD: Final = 500

TARGET_CARD_ID: Final = "p_card-00-sup-2_027"
CARD_ID: Final = TARGET_CARD_ID
TARGET_CARD_NAME: Final = "ご指導ご鞭撻"
TARGET_UPGRADES: Final = (0, 1, 2, 3)
UPGRADES: Final = TARGET_UPGRADES

COMMON_PLAN_TYPE: Final = "ProducePlanType_Common"
ACTIVE_SKILL_CATEGORY: Final = "ProduceCardCategory_ActiveSkill"
CARD_MOVE_LOST: Final = "ProduceCardMovePositionType_Lost"
MOVE_UNKNOWN: Final = "ProduceCardMovePositionType_Unknown"
LESSON_UNKNOWN: Final = "ProduceStepLessonType_Unknown"
CHECK_NOT: Final = "ProduceExamTriggerCheckType_Not"

TARGET_EFFECT_IDS: Final = (
    "e_effect-exam_lesson-0004-01",
    "e_effect-exam_block-0005",
    "e_effect-exam_effect_timer-0001-01-e_effect-exam_card_upgrade-"
    "p_card_search-hand-all-0_0",
)
TARGET_EFFECT_TYPES: Final = (
    "ProduceExamEffectType_ExamLesson",
    "ProduceExamEffectType_ExamBlock",
    "ProduceExamEffectType_ExamEffectTimer",
)
TARGET_EFFECT_CHAIN_ID: Final = "e_effect-exam_card_upgrade-p_card_search-hand-all-0_0"

INT32_MIN: Final = -(2**31)
INT32_MAX: Final = 2**31 - 1


class StartTurnSnapshotBoundary(str, Enum):
    """Only the pre-card-payment settled boundary is executable."""

    SETTLED_PRE_CARD_GATE = "settled-start-turn-pre-card-gate"
    POST_PAYMENT = "post-payment"
    POST_DIRECT_EFFECTS = "post-direct-effects"
    POST_CARD_MOVE = "post-card-move"


class CardExecutionKind(str, Enum):
    """Execution paths separated because only ordinary card play is proven."""

    ORDINARY = "ordinary-card-play"
    FORCED = "forced-card-play"
    EXTRA = "extra-card-play"


class StartTurnCatalogError(ValueError):
    """The local Master database cannot be read as the bounded target shape."""


@dataclass(frozen=True, slots=True)
class Plan2StartTurnSnapshot:
    """Immutable values visible to the card gate at the settled boundary.

    The four settlement flags are provenance gates, not extra predicate
    inputs.  They make it impossible for a caller to silently pass a snapshot
    taken before start-turn recovery/effects or during a card transaction.
    """

    current_stamina: int | None
    max_stamina: int | None
    boundary: StartTurnSnapshotBoundary = (
        StartTurnSnapshotBoundary.SETTLED_PRE_CARD_GATE
    )
    phase: str = PHASE_EXAM_START_TURN
    recovery_settled: bool = True
    consumption_settled: bool = True
    status_turn_spend_settled: bool = True
    ordered_start_turn_effects_settled: bool = True

    def __post_init__(self) -> None:
        for name in ("current_stamina", "max_stamina"):
            value = getattr(self, name)
            if value is not None and type(value) is not int:
                raise TypeError(f"{name} must be a plain Int32 integer or None")
            if value is not None and not INT32_MIN <= value <= INT32_MAX:
                raise ValueError(f"{name} is outside signed Int32")
        object.__setattr__(self, "boundary", StartTurnSnapshotBoundary(self.boundary))
        if type(self.phase) is not str or not self.phase:
            raise TypeError("phase must be a non-empty string")
        for name in (
            "recovery_settled",
            "consumption_settled",
            "status_turn_spend_settled",
            "ordered_start_turn_effects_settled",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be bool")

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_stamina": self.current_stamina,
            "max_stamina": self.max_stamina,
            "boundary": self.boundary.value,
            "phase": self.phase,
            "recovery_settled": self.recovery_settled,
            "consumption_settled": self.consumption_settled,
            "status_turn_spend_settled": self.status_turn_spend_settled,
            "ordered_start_turn_effects_settled": (
                self.ordered_start_turn_effects_settled
            ),
        }


# Convenient names for callers that use the vocabulary of the neighboring
# Plan2 card-play adapter.
Plan2StartTurnStaminaSnapshot = Plan2StartTurnSnapshot
Plan2StartTurnCandidate = Plan2StartTurnSnapshot
CandidateBoundary = StartTurnSnapshotBoundary


@dataclass(frozen=True, slots=True)
class Plan2StartTurnCardEffectSlot:
    """One preserved Master play-effect slot; it is not executed here."""

    slot_index: int
    effect_id: str
    trigger_id: str
    hide_icon: bool
    is_once_play_effect: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_index": self.slot_index,
            "effect_id": self.effect_id,
            "trigger_id": self.trigger_id,
            "hide_icon": self.hide_icon,
            "is_once_play_effect": self.is_once_play_effect,
        }


@dataclass(frozen=True, slots=True)
class Plan2StartTurnCardVersion:
    """The exact four Common card versions affected by the gate."""

    card_id: str
    upgrade_count: int
    name: str
    plan_type: str
    category: str
    stamina: int
    cost_type: str
    cost_value: int
    play_trigger_id: str
    move_position_type: str
    move_effect_trigger_type: str
    move_effect_ids: tuple[str, ...]
    effect_slots: tuple[Plan2StartTurnCardEffectSlot, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "move_effect_ids", tuple(self.move_effect_ids))
        object.__setattr__(self, "effect_slots", tuple(self.effect_slots))

    @property
    def is_common_direct(self) -> bool:
        return (
            self.card_id == TARGET_CARD_ID
            and self.plan_type == COMMON_PLAN_TYPE
            and self.play_trigger_id == TRIGGER_ID
        )

    @property
    def ordered_effect_ids(self) -> tuple[str, ...]:
        return tuple(slot.effect_id for slot in self.effect_slots)

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "upgrade_count": self.upgrade_count,
            "name": self.name,
            "plan_type": self.plan_type,
            "category": self.category,
            "stamina": self.stamina,
            "cost_type": self.cost_type,
            "cost_value": self.cost_value,
            "play_trigger_id": self.play_trigger_id,
            "move_position_type": self.move_position_type,
            "move_effect_trigger_type": self.move_effect_trigger_type,
            "move_effect_ids": list(self.move_effect_ids),
            "effect_slots": [slot.to_dict() for slot in self.effect_slots],
            "ordered_effect_ids": list(self.ordered_effect_ids),
            "direct": self.is_common_direct,
        }


@dataclass(frozen=True, slots=True)
class Plan2StartTurnCatalog:
    """Read-only target catalog and shape reconciliation."""

    database: str
    trigger: StaminaMultipleTrigger
    actual_trigger_contract: Mapping[str, Any] | None
    card_versions: tuple[Plan2StartTurnCardVersion, ...]
    out_of_scope_card_versions: tuple[Plan2StartTurnCardVersion, ...] = ()
    # The trigger family has one status-enchant row for a different source;
    # it is retained as catalog-only and is not part of the four direct card
    # versions in this bounded task.
    target_status_enchant_ids: tuple[str, ...] = ()
    effect_types_by_id: Mapping[str, str] = ()
    shape_issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "card_versions", tuple(self.card_versions))
        object.__setattr__(
            self, "out_of_scope_card_versions", tuple(self.out_of_scope_card_versions)
        )
        object.__setattr__(
            self, "target_status_enchant_ids", tuple(self.target_status_enchant_ids)
        )
        object.__setattr__(self, "shape_issues", tuple(self.shape_issues))
        object.__setattr__(self, "effect_types_by_id", dict(self.effect_types_by_id))

    @property
    def affected_card_versions(self) -> tuple[Plan2StartTurnCardVersion, ...]:
        return self.card_versions

    @property
    def direct_card_versions(self) -> tuple[Plan2StartTurnCardVersion, ...]:
        return tuple(row for row in self.card_versions if row.is_common_direct)

    @property
    def co_blocked_card_versions(self) -> tuple[Plan2StartTurnCardVersion, ...]:
        # The fixed four-version scope has no second blocker.  Effects after
        # the gate are delegated to existing Plan2 runtime and are not
        # reclassified as co-blockers by this standalone.
        return ()

    @property
    def exact_shape_supported(self) -> bool:
        return not self.shape_issues and len(self.direct_card_versions) == 4

    @property
    def affected_card_version_count(self) -> int:
        return len(self.affected_card_versions)

    @property
    def direct_card_version_count(self) -> int:
        return len(self.direct_card_versions)

    @property
    def co_blocked_card_version_count(self) -> int:
        return len(self.co_blocked_card_versions)

    def summary(self) -> dict[str, Any]:
        refs = [
            f"{row.card_id}#{row.upgrade_count}"
            for row in self.affected_card_versions
        ]
        return {
            "affected_card_versions": self.affected_card_version_count,
            "direct_card_versions": self.direct_card_version_count,
            "co_blocked_card_versions": self.co_blocked_card_version_count,
            "directly_unlocked_if_fixed_alone": (
                self.direct_card_version_count
                if not self.shape_issues
                else 0
            ),
            "affected_card_refs": refs,
            "direct_card_refs": [
                f"{row.card_id}#{row.upgrade_count}"
                for row in self.direct_card_versions
            ],
            "co_blocked_card_refs": [],
            "out_of_scope_card_versions": len(self.out_of_scope_card_versions),
            "exact_shape_supported": self.exact_shape_supported,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "trigger": self.trigger.to_dict(),
            "actual_trigger_contract": (
                None
                if self.actual_trigger_contract is None
                else dict(self.actual_trigger_contract)
            ),
            "card_versions": [row.to_dict() for row in self.card_versions],
            "out_of_scope_card_versions": [
                row.to_dict() for row in self.out_of_scope_card_versions
            ],
            "target_status_enchant_ids": list(self.target_status_enchant_ids),
            "same_trigger_status_enchant_count": len(self.target_status_enchant_ids),
            "effect_types_by_id": dict(self.effect_types_by_id),
            "summary": self.summary(),
            "shape_issues": list(self.shape_issues),
            "exact_shape_supported": self.exact_shape_supported,
        }


def _json_list(value: object, label: str, issues: list[str]) -> tuple[Any, ...]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        issues.append(f"invalid-json:{label}")
        return ()
    if not isinstance(parsed, list):
        issues.append(f"json-array-required:{label}")
        return ()
    return tuple(parsed)


def _strict_string_tuple(
    value: object, label: str, issues: list[str]
) -> tuple[str, ...]:
    parsed = _json_list(value, label, issues)
    if any(type(item) is not str for item in parsed):
        issues.append(f"string-array-required:{label}")
        return ()
    return tuple(parsed)


def _strict_int_tuple(
    value: object, label: str, issues: list[str]
) -> tuple[int, ...]:
    parsed = _json_list(value, label, issues)
    if any(type(item) is not int for item in parsed):
        issues.append(f"int-array-required:{label}")
        return ()
    return tuple(parsed)


def _text(value: object, label: str, issues: list[str]) -> str:
    if type(value) is not str:
        issues.append(f"string-required:{label}")
        return ""
    return value


def _plain_int(value: object, label: str, issues: list[str]) -> int:
    if type(value) is not int:
        issues.append(f"int-required:{label}")
        return 0
    return value


def _master_trigger_contract(
    row: sqlite3.Row | None, issues: list[str]
) -> dict[str, Any] | None:
    if row is None:
        issues.append("missing-trigger-row")
        return None
    trigger_id = _text(row["id"], "trigger.id", issues)
    contract: dict[str, Any] = {
        "id": trigger_id,
        "phase_types": list(
            _strict_string_tuple(row["phase_types_json"], f"{trigger_id}.phase_types", issues)
        ),
        "phase_values": list(
            _strict_int_tuple(row["phase_values_json"], f"{trigger_id}.phase_values", issues)
        ),
        "field_check_types": list(
            _strict_string_tuple(
                row["field_status_check_types_json"],
                f"{trigger_id}.field_check_types",
                issues,
            )
        ),
        "field_types": list(
            _strict_string_tuple(row["field_status_types_json"], f"{trigger_id}.field_types", issues)
        ),
        "field_values": list(
            _strict_int_tuple(row["field_status_values_json"], f"{trigger_id}.field_values", issues)
        ),
        "field_card_search_ids": list(
            _strict_string_tuple(
                row["field_status_produce_card_search_ids_json"],
                f"{trigger_id}.field_card_search_ids",
                issues,
            )
        ),
        "produce_card_search_id": _text(
            row["produce_card_search_id"], f"{trigger_id}.produce_card_search_id", issues
        ),
        "upper_search_count": _plain_int(
            row["upper_search_count"], f"{trigger_id}.upper_search_count", issues
        ),
        "lower_search_count": _plain_int(
            row["lower_search_count"], f"{trigger_id}.lower_search_count", issues
        ),
        "card_move_position_type": _text(
            row["card_move_position_type"], f"{trigger_id}.card_move_position_type", issues
        ),
        "effect_types": list(
            _strict_string_tuple(row["effect_types_json"], f"{trigger_id}.effect_types", issues)
        ),
        "lesson_type": _text(row["lesson_type"], f"{trigger_id}.lesson_type", issues),
    }
    return contract


def _shape_differences(
    actual: Mapping[str, Any] | None,
    expected: Mapping[str, Any],
) -> list[str]:
    if actual is None:
        return []
    return [
        f"trigger-shape-mismatch:{key}"
        for key, value in expected.items()
        if actual.get(key) != value
    ]


def _effect_slots(
    card_id: str,
    upgrade: int,
    raw_value: object,
    issues: list[str],
) -> tuple[Plan2StartTurnCardEffectSlot, ...]:
    parsed = _json_list(raw_value, f"card:{card_id}#{upgrade}.play_effects", issues)
    if len(parsed) != len(TARGET_EFFECT_IDS):
        issues.append(f"card-effect-slot-count:{card_id}#{upgrade}")
    slots: list[Plan2StartTurnCardEffectSlot] = []
    for index, item in enumerate(parsed):
        if not isinstance(item, Mapping):
            issues.append(f"card-effect-slot-not-object:{card_id}#{upgrade}:{index}")
            continue
        effect_id = item.get("produceExamEffectId")
        trigger_id = item.get("produceExamTriggerId")
        hide_icon = item.get("hideIcon")
        once = item.get("isOncePlayEffect")
        if index < len(TARGET_EFFECT_IDS) and effect_id != TARGET_EFFECT_IDS[index]:
            issues.append(f"card-effect-id-mismatch:{card_id}#{upgrade}:{index}")
        if type(effect_id) is not str or not effect_id:
            issues.append(f"card-effect-id-invalid:{card_id}#{upgrade}:{index}")
            effect_id = ""
        if trigger_id != "":
            issues.append(f"card-effect-trigger-not-empty:{card_id}#{upgrade}:{index}")
        if type(trigger_id) is not str:
            issues.append(f"card-effect-trigger-invalid:{card_id}#{upgrade}:{index}")
            trigger_id = ""
        if hide_icon is not False:
            issues.append(f"card-effect-hide-icon-shape:{card_id}#{upgrade}:{index}")
        if once is not False:
            issues.append(f"card-effect-once-shape:{card_id}#{upgrade}:{index}")
        slots.append(
            Plan2StartTurnCardEffectSlot(
                slot_index=index,
                effect_id=effect_id,
                trigger_id=trigger_id,
                hide_icon=hide_icon if type(hide_icon) is bool else False,
                is_once_play_effect=once if type(once) is bool else False,
            )
        )
    return tuple(slots)


def _card_from_row(
    row: sqlite3.Row, issues: list[str]
) -> Plan2StartTurnCardVersion:
    card_id = _text(row["id"], "card.id", issues)
    upgrade = _plain_int(row["upgrade_count"], f"card:{card_id}.upgrade", issues)
    raw_json = row["raw_json"]
    try:
        raw = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    except (TypeError, ValueError, json.JSONDecodeError):
        raw = {}
        issues.append(f"invalid-json:card:{card_id}#{upgrade}.raw_json")
    if not isinstance(raw, Mapping):
        raw = {}
        issues.append(f"json-object-required:card:{card_id}#{upgrade}.raw_json")
    move_trigger = raw.get("moveEffectTriggerType", "")
    move_effect_ids = raw.get("moveProduceExamEffectIds", ())
    if move_trigger != "ProduceCardMoveEffectTriggerType_Unknown":
        issues.append(f"move-trigger-shape:{card_id}#{upgrade}")
    if move_effect_ids != []:
        issues.append(f"move-effect-shape:{card_id}#{upgrade}")
    if type(move_trigger) is not str:
        move_trigger = ""
    if not isinstance(move_effect_ids, list) or any(
        type(value) is not str for value in move_effect_ids
    ):
        move_effect_ids = []
        issues.append(f"move-effect-ids-shape:{card_id}#{upgrade}")
    return Plan2StartTurnCardVersion(
        card_id=card_id,
        upgrade_count=upgrade,
        name=_text(row["name"], f"card:{card_id}.name", issues),
        plan_type=_text(row["plan_type"], f"card:{card_id}.plan_type", issues),
        category=_text(row["category"], f"card:{card_id}.category", issues),
        stamina=_plain_int(row["stamina"], f"card:{card_id}.stamina", issues),
        cost_type=_text(row["cost_type"], f"card:{card_id}.cost_type", issues),
        cost_value=_plain_int(row["cost_value"], f"card:{card_id}.cost_value", issues),
        play_trigger_id=_text(
            row["play_trigger_id"], f"card:{card_id}.play_trigger_id", issues
        ),
        move_position_type=_text(
            row["move_position_type"], f"card:{card_id}.move_position_type", issues
        ),
        move_effect_trigger_type=move_trigger,
        move_effect_ids=tuple(move_effect_ids),
        effect_slots=_effect_slots(
            card_id,
            upgrade,
            row["play_effects_json"],
            issues,
        ),
    )


def _validate_card_shape(
    row: Plan2StartTurnCardVersion,
    issues: list[str],
) -> None:
    label = f"card:{row.card_id}#{row.upgrade_count}"
    if row.card_id != TARGET_CARD_ID:
        issues.append(f"card-id-mismatch:{label}")
    if row.upgrade_count not in TARGET_UPGRADES:
        issues.append(f"card-upgrade-out-of-scope:{label}")
    if row.plan_type != COMMON_PLAN_TYPE:
        issues.append(f"card-plan-not-common:{label}")
    if row.category != ACTIVE_SKILL_CATEGORY:
        issues.append(f"card-category-mismatch:{label}")
    if row.play_trigger_id != TRIGGER_ID:
        issues.append(f"card-play-trigger-mismatch:{label}")
    if row.move_position_type != CARD_MOVE_LOST:
        issues.append(f"card-move-position-mismatch:{label}")
    if row.ordered_effect_ids != TARGET_EFFECT_IDS:
        issues.append(f"card-ordered-effects-mismatch:{label}")


def _validate_effect_rows(
    connection: sqlite3.Connection,
    issues: list[str],
) -> dict[str, str]:
    placeholders = ",".join("?" for _ in TARGET_EFFECT_IDS)
    rows = connection.execute(
        f"SELECT id, effect_type, chain_effect_id FROM effect WHERE id IN ({placeholders})",
        TARGET_EFFECT_IDS,
    ).fetchall()
    by_id = {str(row["id"]): row for row in rows}
    for effect_id in TARGET_EFFECT_IDS:
        row = by_id.get(effect_id)
        if row is None:
            issues.append(f"missing-target-effect:{effect_id}")
            continue
        expected_type = TARGET_EFFECT_TYPES[TARGET_EFFECT_IDS.index(effect_id)]
        if row["effect_type"] != expected_type:
            issues.append(f"target-effect-type-mismatch:{effect_id}")
        if effect_id == TARGET_EFFECT_IDS[2] and row["chain_effect_id"] != TARGET_EFFECT_CHAIN_ID:
            issues.append(f"target-effect-chain-mismatch:{effect_id}")
    return {
        effect_id: str(by_id[effect_id]["effect_type"])
        for effect_id in TARGET_EFFECT_IDS
        if effect_id in by_id
    }


def load_plan2_start_turn_stamina_up_multiple_catalog(
    database: Path | str = DEFAULT_DATABASE,
) -> Plan2StartTurnCatalog:
    """Load only the target trigger/card rows from Master in read-only mode."""

    path = Path(database)
    if not path.is_file():
        raise StartTurnCatalogError(f"Master database not found: {path}")
    issues: list[str] = []
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        raise StartTurnCatalogError(f"cannot open Master read-only: {path}") from error
    connection.row_factory = sqlite3.Row
    with closing(connection):
        trigger_row = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?", (TRIGGER_ID,)
        ).fetchone()
        actual_contract = _master_trigger_contract(trigger_row, issues)
        issues.extend(
            _shape_differences(actual_contract, TARGET_TRIGGER_ROW.master_contract())
        )

        status_rows = connection.execute(
            "SELECT id FROM produce_exam_status_enchant "
            "WHERE produce_exam_trigger_id = ? ORDER BY id",
            (TRIGGER_ID,),
        ).fetchall()
        status_ids = tuple(str(row["id"]) for row in status_rows)
        raw_cards = connection.execute(
            "SELECT id, upgrade_count, name, plan_type, category, stamina, "
            "cost_type, cost_value, play_trigger_id, move_position_type, "
            "play_effects_json, raw_json FROM card "
            "WHERE play_trigger_id = ? ORDER BY id, upgrade_count",
            (TRIGGER_ID,),
        ).fetchall()
        parsed_cards: list[Plan2StartTurnCardVersion] = []
        for raw_card in raw_cards:
            before = len(issues)
            card = _card_from_row(raw_card, issues)
            parsed_cards.append(card)
            _validate_card_shape(card, issues)
            # Keep a stable per-row marker in the audit without turning a
            # card's unrelated display field into an executable claim.
            if len(issues) == before and card.upgrade_count not in TARGET_UPGRADES:
                issues.append(f"card-upgrade-out-of-scope:{card.card_id}#{card.upgrade_count}")

        target_cards = tuple(
            card
            for card in parsed_cards
            if card.card_id == TARGET_CARD_ID and card.upgrade_count in TARGET_UPGRADES
        )
        out_of_scope = tuple(card for card in parsed_cards if card not in target_cards)
        if len(target_cards) != len(TARGET_UPGRADES):
            issues.append(f"unexpected-target-card-version-count:{len(target_cards)}")
        if tuple(card.upgrade_count for card in target_cards) != TARGET_UPGRADES:
            issues.append("target-upgrades-not-exact-0-1-2-3")
        effect_types = _validate_effect_rows(connection, issues)

    return Plan2StartTurnCatalog(
        database=str(path),
        trigger=TARGET_TRIGGER_ROW,
        actual_trigger_contract=actual_contract,
        card_versions=target_cards,
        out_of_scope_card_versions=out_of_scope,
        target_status_enchant_ids=status_ids,
        effect_types_by_id=effect_types,
        shape_issues=tuple(dict.fromkeys(issues)),
    )


# The common native catalog is a static exact row.  Reusing it is intentional:
# this adapter does not make a second formula or a second Master translation.
TARGET_TRIGGER_ROW: Final[StaminaMultipleTrigger] = (
    STAMINA_MULTIPLE_TRIGGER_BY_ID[TRIGGER_ID]
)
TARGET_TRIGGER_SHAPE: Final[StaminaMultipleTrigger] = TARGET_TRIGGER_ROW


_MISSING: Final = object()


def _mapping_value(
    source: Mapping[object, object], *names: str
) -> tuple[bool, object]:
    for name in names:
        if name in source:
            return True, source[name]
    return False, None


def _source_value(source: object, *names: str) -> tuple[bool, object]:
    if isinstance(source, Mapping):
        return _mapping_value(source, *names)
    for name in names:
        try:
            return True, getattr(source, name)
        except AttributeError:
            continue
        except Exception:
            return True, None
    return False, None


def _tuple_value(
    source: Mapping[object, object], *names: str
) -> tuple[bool, tuple[object, ...] | None]:
    found, value = _mapping_value(source, *names)
    if not found or not isinstance(value, (list, tuple)):
        return found, None
    return True, tuple(value)


def _trigger_contract_from_source(source: object) -> dict[str, Any] | None:
    names = {
        "phase_types": ("phase_types", "phaseTypes"),
        "phase_values": ("phase_values", "phaseValues"),
        "field_check_types": ("field_check_types", "fieldStatusCheckTypes"),
        "field_types": ("field_types", "fieldStatusTypes"),
        "field_values": ("field_values", "fieldStatusValues"),
        "field_card_search_ids": (
            "field_card_search_ids",
            "fieldStatusProduceCardSearchIds",
        ),
        "effect_types": ("effect_types", "effectTypes"),
    }
    scalar_names = {
        "id": ("id",),
        "produce_card_search_id": ("produce_card_search_id", "produceCardSearchId"),
        "upper_search_count": ("upper_search_count", "upperSearchCount"),
        "lower_search_count": ("lower_search_count", "lowerSearchCount"),
        "card_move_position_type": (
            "card_move_position_type",
            "cardMovePositionType",
        ),
        "lesson_type": ("lesson_type", "lessonType"),
    }
    if isinstance(source, Mapping):
        get = _mapping_value
        get_tuple = _tuple_value
    else:
        def get(item: Mapping[object, object], *field_names: str) -> tuple[bool, object]:
            del item
            return _source_value(source, *field_names)

        def get_tuple(
            item: Mapping[object, object], *field_names: str
        ) -> tuple[bool, tuple[object, ...] | None]:
            del item
            found, value = _source_value(source, *field_names)
            if not found or not isinstance(value, (list, tuple)):
                return found, None
            return True, tuple(value)

    result: dict[str, Any] = {}
    for field_name, field_names in names.items():
        found, value = get_tuple(source, *field_names)
        if not found or value is None:
            return None
        result[field_name] = list(value)
    for field_name, field_names in scalar_names.items():
        found, value = get(source, *field_names)
        if not found:
            return None
        result[field_name] = value
    if type(result["id"]) is not str:
        return None
    if type(result["produce_card_search_id"]) is not str:
        return None
    if type(result["upper_search_count"]) is not int:
        return None
    if type(result["lower_search_count"]) is not int:
        return None
    if type(result["card_move_position_type"]) is not str:
        return None
    if type(result["lesson_type"]) is not str:
        return None
    for field_name in (
        "phase_types",
        "field_check_types",
        "field_types",
        "field_card_search_ids",
        "effect_types",
    ):
        if any(type(value) is not str for value in result[field_name]):
            return None
    for field_name in ("phase_values", "field_values"):
        if any(type(value) is not int for value in result[field_name]):
            return None
    return result


def _trigger_shape_reasons(source: object) -> tuple[str, ...]:
    if isinstance(source, str):
        if source == TRIGGER_ID:
            return ()
        return ("unknown-trigger-id",)
    if isinstance(source, StaminaMultipleTrigger):
        actual = source.master_contract()
    else:
        actual = _trigger_contract_from_source(source)
    if actual is None:
        return ("trigger-shape-unavailable",)
    reasons: list[str] = []
    if actual.get("id") != TRIGGER_ID:
        reasons.append("unknown-or-non-target-trigger-id")
    reasons.extend(_shape_differences(actual, TARGET_TRIGGER_ROW.master_contract()))
    return tuple(dict.fromkeys(reasons))


def _trigger_label(source: object) -> str:
    if isinstance(source, str):
        return source
    if isinstance(source, StaminaMultipleTrigger):
        return source.id
    return str(getattr(source, "id", "<unknown-trigger-shape>"))


def _snapshot_boundary(source: object, default: StartTurnSnapshotBoundary) -> StartTurnSnapshotBoundary | None:
    found, value = _source_value(source, "boundary", "capture_boundary")
    if not found:
        value = default
    try:
        return StartTurnSnapshotBoundary(value)
    except (TypeError, ValueError):
        return None


def _snapshot_values(source: object) -> tuple[bool, object, bool, object]:
    current_found, current = _source_value(
        source, "current_stamina", "currentStamina", "stamina"
    )
    max_found, maximum = _source_value(
        source, "max_stamina", "maxStamina", "maximum_stamina"
    )
    return current_found, current, max_found, maximum


def adapt_plan2_start_turn_snapshot(
    source: object,
    *,
    boundary: StartTurnSnapshotBoundary | str = (
        StartTurnSnapshotBoundary.SETTLED_PRE_CARD_GATE
    ),
    phase: str = PHASE_EXAM_START_TURN,
) -> Plan2StartTurnSnapshot | None:
    """Adapt a state-like source without importing a runtime state class."""

    if isinstance(source, Plan2StartTurnSnapshot):
        return source
    if isinstance(source, StaminaSnapshot):
        try:
            return Plan2StartTurnSnapshot(
                source.current_stamina,
                source.max_stamina,
                StartTurnSnapshotBoundary(boundary),
                phase,
            )
        except (TypeError, ValueError):
            return None
    if source is None:
        return None
    current_found, current, max_found, maximum = _snapshot_values(source)
    if not current_found and not max_found:
        return None
    current = current if current_found else None
    maximum = maximum if max_found else None
    source_boundary = _snapshot_boundary(source, StartTurnSnapshotBoundary(boundary))
    if source_boundary is None:
        return None
    flags: dict[str, bool] = {}
    for name in (
        "recovery_settled",
        "consumption_settled",
        "status_turn_spend_settled",
        "ordered_start_turn_effects_settled",
    ):
        found, value = _source_value(source, name)
        if found:
            if type(value) is not bool:
                return None
            flags[name] = value
    source_phase_found, source_phase = _source_value(source, "phase", "event_phase")
    if source_phase_found:
        if type(source_phase) is not str:
            return None
        phase = source_phase
    try:
        return Plan2StartTurnSnapshot(
            current,
            maximum,
            source_boundary,
            phase,
            **flags,
        )
    except (TypeError, ValueError):
        return None


adapt_plan2_start_turn_candidate = adapt_plan2_start_turn_snapshot


@dataclass(frozen=True, slots=True)
class Plan2StartTurnStaminaEvaluation:
    """Pure direct card-gate result; ``fires=None`` means fail closed."""

    trigger_id: str
    event_phase: str | None
    snapshot: Plan2StartTurnSnapshot | None
    formula: StaminaMultipleEvaluation | None
    fires: bool | None
    execution_kind: CardExecutionKind
    reasons: tuple[str, ...] = ()
    state_unchanged: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "execution_kind", CardExecutionKind(self.execution_kind))
        object.__setattr__(self, "reasons", tuple(self.reasons))

    @property
    def resolved(self) -> bool:
        return self.fires is not None

    @property
    def supported(self) -> bool:
        return not self.reasons and self.fires is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "event_phase": self.event_phase,
            "snapshot": None if self.snapshot is None else self.snapshot.to_dict(),
            "formula": None if self.formula is None else self.formula.to_dict(),
            "fires": self.fires,
            "execution_kind": self.execution_kind.value,
            "reasons": list(self.reasons),
            "state_unchanged": self.state_unchanged,
        }


def evaluate_plan2_start_turn_stamina_up_multiple(
    trigger_or_id: object = TRIGGER_ID,
    snapshot: object | None = None,
    *,
    event_phase: str | None = PHASE_EXAM_START_TURN,
    current_stamina: object = _MISSING,
    max_stamina: object = _MISSING,
    boundary: StartTurnSnapshotBoundary | str = (
        StartTurnSnapshotBoundary.SETTLED_PRE_CARD_GATE
    ),
    execution_kind: CardExecutionKind | str = CardExecutionKind.ORDINARY,
) -> Plan2StartTurnStaminaEvaluation:
    """Evaluate the exact ordinary card gate at the settled StartTurn state.

    The scalar formula is delegated unchanged to the common native evaluator.
    A post-payment/post-effect snapshot, a forced/extra execution request, an
    event-phase mismatch, or any Master shape deviation returns ``fires=None``
    and never mutates the supplied source.
    """

    trigger_id = _trigger_label(trigger_or_id)
    reasons = list(_trigger_shape_reasons(trigger_or_id))
    try:
        kind = CardExecutionKind(execution_kind)
    except (TypeError, ValueError):
        kind = CardExecutionKind.ORDINARY
        reasons.append("execution-kind-unproven")
    if event_phase != PHASE_EXAM_START_TURN:
        reasons.append("event-phase-mismatch-or-unproven")
    if kind is not CardExecutionKind.ORDINARY:
        reasons.append("forced-or-extra-execution-unproven")

    source = snapshot
    if source is None and (
        current_stamina is not _MISSING or max_stamina is not _MISSING
    ):
        source = {
            "current_stamina": (
                None if current_stamina is _MISSING else current_stamina
            ),
            "max_stamina": None if max_stamina is _MISSING else max_stamina,
            "boundary": boundary,
            "phase": event_phase,
        }
    adapted = (
        adapt_plan2_start_turn_snapshot(
            source,
            boundary=boundary,
            phase=event_phase or PHASE_EXAM_START_TURN,
        )
        if source is not None
        else None
    )
    formula: StaminaMultipleEvaluation | None = None
    if adapted is None:
        reasons.append("start-turn-settled-snapshot-unavailable")
    else:
        if adapted.phase != PHASE_EXAM_START_TURN:
            reasons.append("snapshot-phase-is-not-exam-start-turn")
        if adapted.boundary is not StartTurnSnapshotBoundary.SETTLED_PRE_CARD_GATE:
            reasons.append("snapshot-is-after-card-gate-or-payment")
        if not adapted.recovery_settled:
            reasons.append("start-turn-recovery-not-settled")
        if not adapted.consumption_settled:
            reasons.append("stamina-consumption-not-settled")
        if not adapted.status_turn_spend_settled:
            reasons.append("status-turn-spend-not-settled")
        if not adapted.ordered_start_turn_effects_settled:
            reasons.append("ordered-start-turn-effects-not-settled")
        if adapted.current_stamina is None:
            reasons.append("current-stamina-missing")
        elif adapted.max_stamina is None:
            reasons.append("max-stamina-missing")
        else:
            formula = evaluate_stamina_multiple(
                StaminaField.STAMINA_UP_MULTIPLE,
                TRIGGER_THRESHOLD,
                StaminaSnapshot(adapted.current_stamina, adapted.max_stamina),
            )
            reasons.extend(formula.reasons)

    unique_reasons = tuple(dict.fromkeys(reasons))
    fires = (
        formula.fires
        if formula is not None and not unique_reasons
        else None
    )
    return Plan2StartTurnStaminaEvaluation(
        trigger_id=trigger_id,
        event_phase=event_phase,
        snapshot=adapted,
        formula=formula,
        fires=fires,
        execution_kind=kind,
        reasons=unique_reasons,
    )


evaluate_plan2_start_turn_card_trigger = evaluate_plan2_start_turn_stamina_up_multiple
evaluate_plan2_start_turn_trigger = evaluate_plan2_start_turn_stamina_up_multiple
evaluate_plan2_start_turn_stamina_up_multiple_trigger = (
    evaluate_plan2_start_turn_stamina_up_multiple
)
evaluate = evaluate_plan2_start_turn_stamina_up_multiple
load_catalog = load_plan2_start_turn_stamina_up_multiple_catalog


__all__ = [
    "ACTIVE_SKILL_CATEGORY",
    "CARD_MOVE_LOST",
    "CARD_ID",
    "CandidateBoundary",
    "CardExecutionKind",
    "CHECK_NOT",
    "COMMON_PLAN_TYPE",
    "DEFAULT_AUDIT_PATH",
    "FIELD_STAMINA_UP_MULTIPLE",
    "INT32_MAX",
    "INT32_MIN",
    "LESSON_UNKNOWN",
    "MOVE_UNKNOWN",
    "NATIVE_BRANCHES",
    "NATIVE_FORMULA_EVIDENCE",
    "PHASE_EXAM_START_TURN",
    "Plan2StartTurnCandidate",
    "Plan2StartTurnCardEffectSlot",
    "Plan2StartTurnCardVersion",
    "Plan2StartTurnCatalog",
    "Plan2StartTurnSnapshot",
    "Plan2StartTurnStaminaEvaluation",
    "Plan2StartTurnStaminaSnapshot",
    "START_TURN_PHASE_TYPE",
    "START_TURN_TRIGGER_ID",
    "StartTurnCatalogError",
    "StartTurnSnapshotBoundary",
    "TARGET_CARD_ID",
    "TARGET_CARD_NAME",
    "TARGET_EFFECT_IDS",
    "TARGET_EFFECT_TYPES",
    "TARGET_TRIGGER_ROW",
    "TARGET_TRIGGER_SHAPE",
    "TARGET_UPGRADES",
    "TRIGGER_FIELD_TYPE",
    "TRIGGER_ID",
    "TRIGGER_PHASE",
    "TRIGGER_PHASE_TYPE",
    "TRIGGER_THRESHOLD",
    "UPGRADES",
    "adapt_plan2_start_turn_candidate",
    "adapt_plan2_start_turn_snapshot",
    "evaluate",
    "evaluate_plan2_start_turn_card_trigger",
    "evaluate_plan2_start_turn_stamina_up_multiple",
    "evaluate_plan2_start_turn_stamina_up_multiple_trigger",
    "evaluate_plan2_start_turn_trigger",
    "evaluate_stamina_multiple",
    "load_plan2_start_turn_stamina_up_multiple_catalog",
    "load_catalog",
]
