"""Exact standalone Plan2 ``ExamAggressiveValueMultiple`` handoff.

This leaf owns only slot 0 of ``p_card-02-ido-3_127`` upgrades 0..3.  It
validates the complete three-slot Master shape, applies the native current
Aggressive mutation, and stops immediately before the DebuffRecover slot.
The adjacent DebuffRecover and Timer->Block work deliberately remain
handoffs; no central coverage, Plan3, controller, or GUI surface imports this
module.

Android v3.2.3 proves that the effect is neither a future-gain multiplier nor
a score factor.  It reads the current Aggressive integer, adds
``ceil(float32(current) * FloatFromPermil(value))`` through the fix path of
``TryAddAggressiveStatus``, and reports a CardPlayAggressive difference.  The
singleton status owns the result; this executor installs no duration, stack,
replacement, expiry, or reset policy of its own.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Final, Literal, TypeAlias

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    INT32_MAX,
    INT32_MIN,
    NativeFormulaDomainError,
    ceil_f32_to_i32,
    f32,
    permille_to_f32,
)
from .plan3_engine import (
    CATEGORY_MENTAL,
    COST_STAMINA,
    MOVE_LOST,
    Plan3Card,
    Plan3Effect,
    load_plan3_card,
)


PLAN2: Final = "ProducePlanType_Plan2"
CARD_ID: Final = "p_card-02-ido-3_127"
TARGET_UPGRADES: Final = (0, 1, 2, 3)
TARGET_VERSION_REFS: Final = tuple(
    f"{CARD_ID}+{upgrade}" for upgrade in TARGET_UPGRADES
)

EFFECT_AGGRESSIVE_VALUE_MULTIPLE: Final = (
    "ProduceExamEffectType_ExamAggressiveValueMultiple"
)
EFFECT_CARD_PLAY_AGGRESSIVE: Final = (
    "ProduceExamEffectType_ExamCardPlayAggressive"
)
EFFECT_DEBUFF_RECOVER: Final = "ProduceExamEffectType_ExamDebuffRecover"
EFFECT_TIMER: Final = "ProduceExamEffectType_ExamEffectTimer"
EFFECT_BLOCK: Final = "ProduceExamEffectType_ExamBlock"
EFFECT_UNKNOWN: Final = "ProduceExamEffectType_Unknown"

MULTIPLE_EFFECT_ID: Final = "e_effect-exam_aggressive_value_multiple-0500"
AGGRESSIVE_GROUP: Final = (
    "effect_group-visible-exam_card_play_aggressive-000"
)
BLOCK_GROUP: Final = "effect_group-visible-exam_block-000"
TIMER_GROUP: Final = "effect_group-visible-exam_effect_timer-000"
CARD_GROUPS: Final = (BLOCK_GROUP, TIMER_GROUP, AGGRESSIVE_GROUP)

GAP_AGGRESSIVE_VALUE_MULTIPLE: Final = (
    "C:effect:ProduceExamEffectType_ExamAggressiveValueMultiple"
)
GAP_DEBUFF_RECOVER: Final = (
    "C:effect:ProduceExamEffectType_ExamDebuffRecover"
)
GAP_TIMER: Final = "C:effect:ProduceExamEffectType_ExamEffectTimer"

PlayOrigin: TypeAlias = Literal["normal", "forced", "extra"]
PLAY_ORIGINS: Final = ("normal", "forced", "extra")

# ``ExecuteCardCommandImpl`` builds candidates from the pre-payment snapshot.
# Ordinary payment (or the applicable forced/extra policy) resolves before the
# direct commands.  Difference-trigger commands from one direct effect are
# inserted ahead of the next source-card slot.  Move/count order is the exact
# Android MovePlayCard order; the last history item is adapter-owned and only
# occurs after the native transaction has committed.
NATIVE_CARD_TRANSACTION_ORDER: Final = (
    "bind-playing-card",
    "build-card-commands-from-pre-payment-snapshot",
    "read-build-side-card-play-count",
    "resolve-origin-cost-policy",
    "insert-cost-and-direct-cost-difference-reactives",
    "slot0-aggressive-value-multiple",
    "insert-slot0-difference-reactives-before-slot1",
    "slot1-debuff-recover-handoff",
    "insert-slot1-difference-reactives-before-slot2",
    "slot2-timer-install-handoff",
    "insert-slot2-difference-reactives-before-user-after",
    "user-card-after-check",
    "increment-resolved-card-play-count",
    "increment-global-play-count",
    "resolve-playing-card",
    "physical-move-to-lost",
    "card-play-count-add",
    "status-update-play-card-count",
    "effect-difference-callback",
    "append-adapter-history-after-commit",
)


class Plan2AggressiveValueMultipleContractError(ValueError):
    """Stable fail-closed error for this four-version standalone leaf."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


def _plain_i32(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Plan2AggressiveValueMultipleContractError(
            "signed-int32-required", label
        )
    if not INT32_MIN <= value <= INT32_MAX:
        raise Plan2AggressiveValueMultipleContractError(
            "signed-int32-out-of-range", label
        )
    return value


def _i32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _json_object(value: object, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2AggressiveValueMultipleContractError(
            "invalid-master-json", label
        ) from error
    if not isinstance(parsed, dict):
        raise Plan2AggressiveValueMultipleContractError(
            "invalid-master-json-object", label
        )
    return parsed


def _require_exact(
    raw: dict[str, object], key: str, expected: object, label: str
) -> None:
    actual = raw.get(key)
    if type(actual) is not type(expected) or actual != expected:
        raise Plan2AggressiveValueMultipleContractError(
            "master-shape-drift", f"{label}:{key}"
        )


def _effect_raw(
    connection: sqlite3.Connection, effect_id: str
) -> dict[str, object]:
    row = connection.execute(
        "SELECT raw_json FROM effect WHERE id = ?", (effect_id,)
    ).fetchone()
    if row is None:
        raise Plan2AggressiveValueMultipleContractError(
            "master-effect-missing", effect_id
        )
    return _json_object(row[0], effect_id)


_NEUTRAL_EFFECT_FIELDS: Final = {
    "targetProduceCardId": "",
    "targetUpgradeCount": 0,
    "targetExamEffectType": EFFECT_UNKNOWN,
    "produceCardSearchId": "",
    "movePositionType": "ProduceCardMovePositionType_Unknown",
    "pickRangeType": "ProducePickRangeType_Unknown",
    "pickCountReferenceProduceCardSearchId": "",
    "pickCountType": "ProducePickCountType_Unknown",
    "pickCountMin": 0,
    "pickCountMax": 0,
    "produceCardSearchId2": "",
    "pickRangeType2": "ProducePickRangeType_Unknown",
    "pickCountReferenceProduceCardSearchId2": "",
    "pickCountType2": "ProducePickCountType_Unknown",
    "pickCountMin2": 0,
    "pickCountMax2": 0,
    "produceExamStatusEnchantId": "",
    "produceCardStatusEnchantId": "",
    "produceCardGrowEffectIds": [],
}


def _validate_effect_row(
    connection: sqlite3.Connection,
    *,
    effect_id: str,
    effect_type: str,
    value1: int,
    value2: int = 0,
    count: int = 0,
    turn: int = 0,
    chain_effect_id: str = "",
    groups: tuple[str, ...] = (),
) -> None:
    normalized = connection.execute(
        """
        SELECT effect_type, value1, value2, effect_count, effect_turn,
               status_enchant_id, chain_effect_id
          FROM effect
         WHERE id = ?
        """,
        (effect_id,),
    ).fetchone()
    if normalized is None:
        raise Plan2AggressiveValueMultipleContractError(
            "master-effect-missing", effect_id
        )
    normalized_expected = (
        effect_type,
        value1,
        value2,
        count,
        turn,
        "",
        chain_effect_id,
    )
    if tuple(normalized) != normalized_expected:
        raise Plan2AggressiveValueMultipleContractError(
            "master-effect-normalized-drift", effect_id
        )
    raw = _effect_raw(connection, effect_id)
    expected = {
        "id": effect_id,
        "effectType": effect_type,
        "effectValue1": value1,
        "effectValue2": value2,
        "effectCount": count,
        "effectTurn": turn,
        **_NEUTRAL_EFFECT_FIELDS,
        "chainProduceExamEffectId": chain_effect_id,
        "chainProduceExamEffectIds": [],
        "effectGroupIds": list(groups),
    }
    for key, value in expected.items():
        _require_exact(raw, key, value, effect_id)


@dataclass(frozen=True, slots=True)
class _VersionSpec:
    upgrade: int
    stamina: int
    debuff_value: int
    block_value: int

    @property
    def ref(self) -> str:
        return f"{CARD_ID}+{self.upgrade}"

    @property
    def name(self) -> str:
        return f"わたしらしい色{'+' * self.upgrade}"

    @property
    def debuff_effect_id(self) -> str:
        return f"e_effect-exam_debuff_recover-{self.debuff_value:04d}"

    @property
    def block_effect_id(self) -> str:
        return f"e_effect-exam_block-{self.block_value:04d}"

    @property
    def timer_effect_id(self) -> str:
        return (
            "e_effect-exam_effect_timer-0001-01-"
            f"{self.block_effect_id}"
        )

    @property
    def ordered_effect_ids(self) -> tuple[str, str, str]:
        return (
            MULTIPLE_EFFECT_ID,
            self.debuff_effect_id,
            self.timer_effect_id,
        )

    @property
    def remaining_blockers(self) -> tuple[str, str, str]:
        return (
            GAP_DEBUFF_RECOVER,
            GAP_TIMER,
            f"C:effect-chain:{self.block_effect_id}",
        )


_VERSION_SPECS: Final = tuple(
    _VersionSpec(upgrade, stamina, debuff, block)
    for upgrade, (stamina, debuff, block) in enumerate(
        ((2, 1, 4), (2, 2, 8), (2, 2, 10), (1, 2, 10))
    )
)
_SPEC_BY_UPGRADE: Final = {row.upgrade: row for row in _VERSION_SPECS}


@dataclass(frozen=True, slots=True)
class Plan2AggressiveValueMultipleBatchAccounting:
    affected: int = 4
    direct: int = 0
    co_blocked: int = 4
    expected_delta: int = 0

    def __post_init__(self) -> None:
        if self.affected != self.direct + self.co_blocked:
            raise ValueError("batch accounting does not balance")


BATCH_ACCOUNTING: Final = Plan2AggressiveValueMultipleBatchAccounting()


@dataclass(frozen=True, slots=True)
class Plan2AggressiveValueMultipleEffectContract:
    effect: Plan3Effect
    slot_index: int = 0
    permille: int = 500
    effect_group_ids: tuple[str, ...] = (AGGRESSIVE_GROUP,)

    def __post_init__(self) -> None:
        if not isinstance(self.effect, Plan3Effect) or not (
            self.slot_index == 0
            and self.effect.id == MULTIPLE_EFFECT_ID
            and self.effect.effect_type == EFFECT_AGGRESSIVE_VALUE_MULTIPLE
            and self.effect.value1 == self.permille == 500
            and self.effect.value2 == 0
            and self.effect.effect_count == 0
            and self.effect.effect_turn == 0
            and not self.effect.status_enchant_id
            and self.effect.status_enchant is None
            and not self.effect.chain_effect_id
            and not self.effect.chain_effect_ids
            and self.effect.chain_effect is None
            and self.effect.trigger is None
            and not self.effect.once
            and self.effect.card_move_rule is None
            and self.effect_group_ids == (AGGRESSIVE_GROUP,)
        ):
            raise Plan2AggressiveValueMultipleContractError(
                "runtime-effect-contract-shape-drift", MULTIPLE_EFFECT_ID
            )


@dataclass(frozen=True, slots=True)
class Plan2AggressiveValueMultipleHandoffSlot:
    slot_index: Literal[1, 2]
    effect: Plan3Effect
    owner: Literal["DebuffRecover", "TimerToBlock"]
    blocker_ids: tuple[str, ...]

    @property
    def effect_id(self) -> str:
        return self.effect.id


Plan2AggressiveValueMultipleSlot: TypeAlias = (
    Plan2AggressiveValueMultipleEffectContract
    | Plan2AggressiveValueMultipleHandoffSlot
)


@dataclass(frozen=True, slots=True)
class Plan2AggressiveValueMultipleCardVersion:
    card: Plan3Card
    ordered_slots: tuple[Plan2AggressiveValueMultipleSlot, ...]
    target: Plan2AggressiveValueMultipleEffectContract
    remaining_blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        spec = _SPEC_BY_UPGRADE.get(self.card.upgrade)
        slots = tuple(self.ordered_slots)
        if spec is None or not (
            isinstance(self.card, Plan3Card)
            and self.card.id == CARD_ID
            and self.card.name == spec.name
            and self.card.plan_type == PLAN2
            and self.card.category == CATEGORY_MENTAL
            and self.card.stamina_cost == spec.stamina
            and self.card.force_stamina_cost == 0
            and self.card.cost_type == COST_STAMINA
            and self.card.cost_value == 0
            and self.card.play_trigger is None
            and self.card.move_position_type == MOVE_LOST
            and self.card.effect_group_ids == CARD_GROUPS
            and len(slots) == 3
            and tuple(slot.slot_index for slot in slots) == (0, 1, 2)
            and tuple(slot.effect.id for slot in slots)
            == spec.ordered_effect_ids
            and slots[0] is self.target
            and self.remaining_blockers == spec.remaining_blockers
        ):
            raise Plan2AggressiveValueMultipleContractError(
                "runtime-card-contract-shape-drift",
                f"{self.card.id}+{self.card.upgrade}",
            )
        object.__setattr__(self, "ordered_slots", slots)

    @property
    def ref(self) -> str:
        return f"{self.card.id}+{self.card.upgrade}"

    @property
    def coverage(self) -> Literal["co-blocked"]:
        return "co-blocked"

    @property
    def pending_effect_ids(self) -> tuple[str, str]:
        return tuple(slot.effect.id for slot in self.ordered_slots[1:])  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class Plan2AggressiveValueMultipleCatalog:
    versions: tuple[Plan2AggressiveValueMultipleCardVersion, ...]

    def __post_init__(self) -> None:
        versions = tuple(self.versions)
        if tuple(row.ref for row in versions) != TARGET_VERSION_REFS:
            raise Plan2AggressiveValueMultipleContractError(
                "catalog-version-order-drift"
            )
        object.__setattr__(self, "versions", versions)

    def version(
        self, upgrade: int
    ) -> Plan2AggressiveValueMultipleCardVersion:
        _plain_i32(upgrade, "upgrade")
        if upgrade not in TARGET_UPGRADES:
            raise Plan2AggressiveValueMultipleContractError(
                "unknown-card-upgrade", str(upgrade)
            )
        try:
            return self.versions[upgrade]
        except (IndexError, TypeError) as error:
            raise Plan2AggressiveValueMultipleContractError(
                "unknown-card-upgrade", str(upgrade)
            ) from error

    @property
    def direct_refs(self) -> tuple[()]:
        return ()

    @property
    def co_blocked_refs(self) -> tuple[str, ...]:
        return tuple(row.ref for row in self.versions)


def _validate_card_raw(
    connection: sqlite3.Connection, spec: _VersionSpec
) -> None:
    row = connection.execute(
        "SELECT raw_json FROM card WHERE id = ? AND upgrade_count = ?",
        (CARD_ID, spec.upgrade),
    ).fetchone()
    if row is None:
        raise Plan2AggressiveValueMultipleContractError(
            "master-card-missing", spec.ref
        )
    raw = _json_object(row[0], spec.ref)
    expected = {
        "id": CARD_ID,
        "upgradeCount": spec.upgrade,
        "name": spec.name,
        "planType": PLAN2,
        "category": CATEGORY_MENTAL,
        "stamina": spec.stamina,
        "forceStamina": 0,
        "costType": "ExamCostType_Unknown",
        "costValue": 0,
        "playProduceExamTriggerId": "",
        "playEffects": [
            {
                "produceExamTriggerId": "",
                "produceExamEffectId": effect_id,
                "hideIcon": False,
                "isOncePlayEffect": False,
            }
            for effect_id in spec.ordered_effect_ids
        ],
        "playMovePositionType": MOVE_LOST,
        "moveEffectTriggerType": "ProduceCardMoveEffectTriggerType_Unknown",
        "moveProduceExamEffectIds": [],
        "isEndTurnLost": False,
        "isInitial": False,
        "isRestrict": False,
        "produceCardStatusEnchantId": "",
        "noDeckDuplication": True,
        "effectGroupIds": list(CARD_GROUPS),
        "produceCardCustomizeIds": [],
        "maxCustomizeCount": 0,
        "isConversion": False,
        "moveProduceExamTriggerIds": [],
    }
    for key, value in expected.items():
        _require_exact(raw, key, value, spec.ref)


def _validate_version(
    spec: _VersionSpec, database: Path
) -> Plan2AggressiveValueMultipleCardVersion:
    try:
        card = load_plan3_card(CARD_ID, spec.upgrade, database)
    except (KeyError, TypeError, ValueError, sqlite3.Error) as error:
        raise Plan2AggressiveValueMultipleContractError(
            "master-card-load-failed", spec.ref
        ) from error

    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            _validate_card_raw(connection, spec)
            _validate_effect_row(
                connection,
                effect_id=MULTIPLE_EFFECT_ID,
                effect_type=EFFECT_AGGRESSIVE_VALUE_MULTIPLE,
                value1=500,
                groups=(AGGRESSIVE_GROUP,),
            )
            _validate_effect_row(
                connection,
                effect_id=spec.debuff_effect_id,
                effect_type=EFFECT_DEBUFF_RECOVER,
                value1=spec.debuff_value,
            )
            _validate_effect_row(
                connection,
                effect_id=spec.timer_effect_id,
                effect_type=EFFECT_TIMER,
                value1=1,
                count=1,
                chain_effect_id=spec.block_effect_id,
                groups=(BLOCK_GROUP, TIMER_GROUP),
            )
            _validate_effect_row(
                connection,
                effect_id=spec.block_effect_id,
                effect_type=EFFECT_BLOCK,
                value1=spec.block_value,
                groups=(BLOCK_GROUP,),
            )
    except sqlite3.Error as error:
        raise Plan2AggressiveValueMultipleContractError(
            "master-read-failed", spec.ref
        ) from error

    try:
        target = Plan2AggressiveValueMultipleEffectContract(card.effects[0])
        debuff = Plan2AggressiveValueMultipleHandoffSlot(
            1,
            card.effects[1],
            "DebuffRecover",
            (GAP_DEBUFF_RECOVER,),
        )
        timer = Plan2AggressiveValueMultipleHandoffSlot(
            2,
            card.effects[2],
            "TimerToBlock",
            (GAP_TIMER, f"C:effect-chain:{spec.block_effect_id}"),
        )
    except (IndexError, TypeError, ValueError) as error:
        if isinstance(error, Plan2AggressiveValueMultipleContractError):
            raise
        raise Plan2AggressiveValueMultipleContractError(
            "runtime-card-slot-shape-drift", spec.ref
        ) from error
    return Plan2AggressiveValueMultipleCardVersion(
        card,
        (target, debuff, timer),
        target,
        spec.remaining_blockers,
    )


def load_plan2_aggressive_value_multiple_catalog(
    *, database: Path = DEFAULT_DATABASE
) -> Plan2AggressiveValueMultipleCatalog:
    """Load and fail-closed validate the four exact p127 versions."""

    if not isinstance(database, Path) or not database.is_file():
        raise Plan2AggressiveValueMultipleContractError(
            "master-database-missing", str(database)
        )
    catalog = Plan2AggressiveValueMultipleCatalog(
        tuple(_validate_version(spec, database) for spec in _VERSION_SPECS)
    )
    if (
        len(catalog.versions),
        len(catalog.direct_refs),
        len(catalog.co_blocked_refs),
    ) != (
        BATCH_ACCOUNTING.affected,
        BATCH_ACCOUNTING.direct,
        BATCH_ACCOUNTING.co_blocked,
    ):
        raise Plan2AggressiveValueMultipleContractError(
            "coverage-accounting-drift"
        )
    return catalog


@dataclass(frozen=True, slots=True)
class Plan2AggressiveValueMultipleTransition:
    before: int
    permille: int
    delta: int
    after: int
    attempted_status_add: bool
    status_add_blocked: bool
    difference_emitted: bool
    difference_effect_type: str | None
    difference_is_consume: bool
    status_action: Literal["none", "merge-current-singleton"]
    operations: tuple[str, ...]

    @property
    def modifies_current_aggressive(self) -> bool:
        return self.attempted_status_add and not self.status_add_blocked

    @property
    def installs_future_gain_multiplier(self) -> bool:
        return False

    @property
    def installs_score_factor(self) -> bool:
        return False

    @property
    def installs_lifetime(self) -> bool:
        return False


def apply_aggressive_value_multiple(
    current_aggressive: int,
    *,
    permille: int = 500,
    status_add_blocked: bool = False,
) -> Plan2AggressiveValueMultipleTransition:
    """Apply the normal-finite Android executor arithmetic.

    The public primitive admits signed Int32 values so zero, negative, and
    wrapping boundaries can be tested.  The exact p127 adapter below always
    supplies its validated Master value of 500 permille.
    """

    current = _plain_i32(current_aggressive, "current_aggressive")
    ratio = _plain_i32(permille, "permille")
    if not isinstance(status_add_blocked, bool):
        raise Plan2AggressiveValueMultipleContractError(
            "boolean-required", "status_add_blocked"
        )
    if current == 0:
        return Plan2AggressiveValueMultipleTransition(
            0,
            ratio,
            0,
            0,
            False,
            status_add_blocked,
            False,
            None,
            False,
            "none",
            ("get-aggressive-false", "zero-current-early-return"),
        )

    try:
        delta = ceil_f32_to_i32(
            f32(f32(permille_to_f32(ratio)) * f32(current))
        )
    except NativeFormulaDomainError as error:
        raise Plan2AggressiveValueMultipleContractError(
            "native-conversion-domain", f"{current}:{ratio}"
        ) from error

    if status_add_blocked:
        return Plan2AggressiveValueMultipleTransition(
            current,
            ratio,
            delta,
            current,
            True,
            True,
            False,
            None,
            False,
            "none",
            (
                "get-aggressive-false",
                "float-from-permille",
                "float32-multiply",
                "ceil-to-int32",
                "try-add-aggressive-fix-blocked",
            ),
        )

    # Existing-status path of TryAddAggressiveStatus(isFix=true): unchecked
    # W-register addition followed by ``bic w8,w8,w8,asr #31`` (max(0,x)).
    after = max(0, _i32(current + delta))
    return Plan2AggressiveValueMultipleTransition(
        current,
        ratio,
        delta,
        after,
        True,
        False,
        True,
        EFFECT_CARD_PLAY_AGGRESSIVE,
        False,
        "merge-current-singleton",
        (
            "get-aggressive-false",
            "float-from-permille",
            "float32-multiply",
            "ceil-to-int32",
            "try-add-aggressive-is-fix-true",
            "int32-wrap-add",
            "negative-clamp-zero",
            "get-aggressive-false",
            "append-card-play-aggressive-difference-even-if-unchanged",
        ),
    )


@dataclass(frozen=True, slots=True)
class Plan2AggressiveValueMultipleHandoff:
    version: Plan2AggressiveValueMultipleCardVersion
    play_origin: PlayOrigin
    source_guid: str
    transition: Plan2AggressiveValueMultipleTransition
    next_slot_index: Literal[1]
    pending_slots: tuple[Plan2AggressiveValueMultipleHandoffSlot, ...]
    completed_operations: tuple[str, ...]
    required_completion_order: tuple[str, ...]

    @property
    def aggressive(self) -> int:
        return self.transition.after


def execute_p127_aggressive_value_multiple_handoff(
    version: Plan2AggressiveValueMultipleCardVersion,
    *,
    current_aggressive: int,
    play_origin: PlayOrigin = "normal",
    source_guid: str,
    status_add_blocked: bool = False,
) -> Plan2AggressiveValueMultipleHandoff:
    """Execute exact slot 0 after cost and stop before DebuffRecover.

    This handoff intentionally owns no cost mutation, DebuffRecover, timer
    installation, card counters, history, or Lost move.  The immutable result
    carries their proven required order so an owning transaction cannot
    silently reorder them.
    """

    if not isinstance(version, Plan2AggressiveValueMultipleCardVersion):
        raise Plan2AggressiveValueMultipleContractError(
            "invalid-card-version"
        )
    # Re-run the frozen runtime shape check against the private exact spec.
    Plan2AggressiveValueMultipleCardVersion(
        version.card,
        version.ordered_slots,
        version.target,
        version.remaining_blockers,
    )
    if play_origin not in PLAY_ORIGINS:
        raise Plan2AggressiveValueMultipleContractError(
            "unknown-play-origin", str(play_origin)
        )
    if not isinstance(source_guid, str) or not source_guid:
        raise Plan2AggressiveValueMultipleContractError(
            "source-guid-required"
        )
    transition = apply_aggressive_value_multiple(
        current_aggressive,
        permille=version.target.permille,
        status_add_blocked=status_add_blocked,
    )
    pending = version.ordered_slots[1:]
    if not all(
        isinstance(slot, Plan2AggressiveValueMultipleHandoffSlot)
        for slot in pending
    ):
        raise Plan2AggressiveValueMultipleContractError(
            "pending-slot-shape-drift", version.ref
        )
    return Plan2AggressiveValueMultipleHandoff(
        version,
        play_origin,
        source_guid,
        transition,
        1,
        pending,  # type: ignore[arg-type]
        (
            f"accepted-{play_origin}-play-after-cost:{version.ref}:{source_guid}",
            "execute-slot0-aggressive-value-multiple",
            (
                "insert-slot0-difference-reactives-before-slot1"
                if transition.difference_emitted
                else "no-slot0-difference-reactives"
            ),
            "yield-before-slot1-debuff-recover",
        ),
        NATIVE_CARD_TRANSACTION_ORDER,
    )


__all__ = [
    "AGGRESSIVE_GROUP",
    "BATCH_ACCOUNTING",
    "CARD_ID",
    "EFFECT_AGGRESSIVE_VALUE_MULTIPLE",
    "EFFECT_CARD_PLAY_AGGRESSIVE",
    "GAP_AGGRESSIVE_VALUE_MULTIPLE",
    "GAP_DEBUFF_RECOVER",
    "GAP_TIMER",
    "MULTIPLE_EFFECT_ID",
    "NATIVE_CARD_TRANSACTION_ORDER",
    "PLAY_ORIGINS",
    "Plan2AggressiveValueMultipleBatchAccounting",
    "Plan2AggressiveValueMultipleCardVersion",
    "Plan2AggressiveValueMultipleCatalog",
    "Plan2AggressiveValueMultipleContractError",
    "Plan2AggressiveValueMultipleEffectContract",
    "Plan2AggressiveValueMultipleHandoff",
    "Plan2AggressiveValueMultipleHandoffSlot",
    "Plan2AggressiveValueMultipleTransition",
    "TARGET_UPGRADES",
    "TARGET_VERSION_REFS",
    "apply_aggressive_value_multiple",
    "execute_p127_aggressive_value_multiple_handoff",
    "load_plan2_aggressive_value_multiple_catalog",
]
