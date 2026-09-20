"""Plan2/Common adapter for the exact direct-card BlockRestriction row.

The native BlockRestriction status is plan-neutral.  The Plan3/Common module
already owns its immutable status lifecycle and the AntiDebuff
``IsBlockAddStatus`` projection, so this module only narrows that primitive to
the one proven Plan2/Common direct-card row and validates the local Master
boundary.  It deliberately does not define a second status runtime or map
anything onto the Plan2 state model.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import TypeAlias

from .master_db import DEFAULT_DATABASE
from .plan3_anti_debuff import AntiDebuffRuntime
from .plan3_block_restriction import (
    AFFECTED_CARD_VERSIONS as COMMON_AFFECTED_CARD_VERSIONS,
    BlockRestrictionDifference,
    BlockRestrictionRuntime,
    CardVersion,
    DIRECT_EFFECT as COMMON_DIRECT_EFFECT,
    DIRECT_EFFECT_ID,
    EFFECT_GROUP_ID,
    EFFECT_TYPE,
    EFFECT_TYPE_VALUE,
    EffectContext,
    EffectRow,
    Execution,
    execute_effect as _shared_execute_effect,
    matches_effect_contract as _shared_matches_effect_contract,
)


PLAN_COMMON = "ProducePlanType_Common"
PLAN2 = "ProducePlanType_Plan2"
PLAN2_COMMON_PLAN_TYPES = frozenset((PLAN_COMMON, PLAN2))

DIRECT_CARD_ID = "p_card-00-men-2_014"
DIRECT_CARD_UPGRADES = (0, 1, 2, 3)
DIRECT_CARD_CONTEXT = EffectContext.DIRECT_CARD

# The aliases are intentional: Plan2/Common must not acquire a second
# BlockRestriction runtime, result type, or effect row catalog.
Plan2BlockRestrictionRuntime: TypeAlias = BlockRestrictionRuntime
Plan2BlockRestrictionDifference: TypeAlias = BlockRestrictionDifference
Plan2BlockRestrictionExecution: TypeAlias = Execution
Plan2BlockRestrictionEffectRow: TypeAlias = EffectRow

DIRECT_EFFECT = COMMON_DIRECT_EFFECT
AFFECTED_CARD_VERSIONS = COMMON_AFFECTED_CARD_VERSIONS
DIRECT_CARD_VERSIONS = AFFECTED_CARD_VERSIONS


class Plan2BlockRestrictionResolutionError(ValueError):
    """A Master row or caller context is outside the proven adapter shape."""


def validate_plan2_common_plan(plan_type: object) -> str:
    """Accept only plan labels whose native surface is in scope."""

    if not isinstance(plan_type, str) or plan_type not in PLAN2_COMMON_PLAN_TYPES:
        raise Plan2BlockRestrictionResolutionError(
            f"unsupported plan type: {plan_type!r}"
        )
    return plan_type


def _row_mapping(value: object) -> dict[str, object] | None:
    if isinstance(value, Mapping):
        return dict(value)
    keys = getattr(value, "keys", None)
    if callable(keys):
        try:
            return {key: value[key] for key in keys()}
        except (KeyError, IndexError, TypeError):
            return None
    return None


def _strict_equal(actual: object, expected: object) -> bool:
    """Compare Master JSON scalars without bool/int coercion."""

    return type(actual) is type(expected) and actual == expected


_DIRECT_MASTER_FIELDS: dict[str, object] = {
    "id": DIRECT_EFFECT_ID,
    "effectType": EFFECT_TYPE,
    "effectValue1": 0,
    "effectValue2": 0,
    "effectCount": 0,
    "effectTurn": 2,
    "targetProduceCardId": "",
    "targetUpgradeCount": 0,
    "targetExamEffectType": "ProduceExamEffectType_Unknown",
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
    "chainProduceExamEffectId": "",
    "chainProduceExamEffectIds": [],
    "produceExamStatusEnchantId": "",
    "produceCardStatusEnchantId": "",
    "produceCardGrowEffectIds": [],
    "effectGroupIds": [EFFECT_GROUP_ID],
}


def _raw_payload(outer: Mapping[str, object]) -> dict[str, object] | None:
    raw = outer.get("raw_json", outer.get("rawJson"))
    if raw is None:
        return None
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    return dict(raw) if isinstance(raw, Mapping) else None


def _matches_master_mapping(effect: object) -> bool:
    """Validate a normalized SQLite row/raw Master object exactly."""

    outer = _row_mapping(effect)
    if outer is None:
        return False
    raw = _raw_payload(outer)
    if raw is None:
        # A raw Master object is also a useful resolver input.
        raw = outer if "effectType" in outer else None
    if raw is None:
        return False

    for key, expected in _DIRECT_MASTER_FIELDS.items():
        if key not in raw or not _strict_equal(raw[key], expected):
            return False

    # SQLite's normalized columns must agree with the raw payload when they
    # are present; otherwise a lookalike row must not be normalized silently.
    normalized = (
        ("id", "id"),
        ("effect_type", "effectType"),
        ("value1", "effectValue1"),
        ("value2", "effectValue2"),
        ("effect_count", "effectCount"),
        ("effect_turn", "effectTurn"),
        ("status_enchant_id", "produceExamStatusEnchantId"),
        ("chain_effect_id", "chainProduceExamEffectId"),
    )
    for outer_key, raw_key in normalized:
        if outer_key in outer and not _strict_equal(
            outer[outer_key], raw[raw_key]
        ):
            return False
    return True


def _object_has_exact_group(effect: object) -> bool:
    group_ids = getattr(effect, "effect_group_ids", None)
    if group_ids is None:
        return True
    try:
        return tuple(group_ids) == (EFFECT_GROUP_ID,)
    except TypeError:
        return False


def matches_plan2_effect_contract(
    effect: object,
    *,
    context: object = DIRECT_CARD_CONTEXT,
    plan_type: object | None = None,
) -> bool:
    """Match only the direct-card Plan2/Common contract."""

    if context is not DIRECT_CARD_CONTEXT:
        return False
    if plan_type is not None:
        try:
            validate_plan2_common_plan(plan_type)
        except Plan2BlockRestrictionResolutionError:
            return False
    mapping = _row_mapping(effect)
    if mapping is not None:
        return _matches_master_mapping(mapping)
    return _object_has_exact_group(effect) and _shared_matches_effect_contract(
        effect, DIRECT_CARD_CONTEXT
    )


def resolve_plan2_block_restriction(
    effect: object,
    *,
    context: object = DIRECT_CARD_CONTEXT,
    plan_type: object | None = None,
) -> object | None:
    """Resolve one exact direct row, or return ``None`` without guessing."""

    if not matches_plan2_effect_contract(
        effect, context=context, plan_type=plan_type
    ):
        return None
    # Mapping inputs are normalized to the shared immutable row; object inputs
    # (including a Plan3Effect loaded from Master) retain their scalar payload.
    return DIRECT_EFFECT if _row_mapping(effect) is not None else effect


try_resolve_plan2_block_restriction = resolve_plan2_block_restriction


def load_plan2_block_restriction_effect(
    effect_id: str = DIRECT_EFFECT_ID,
    database: Path = DEFAULT_DATABASE,
) -> EffectRow:
    """Read and validate the one exact direct effect from local Master."""

    if effect_id != DIRECT_EFFECT_ID:
        raise Plan2BlockRestrictionResolutionError(
            f"unsupported direct effect: {effect_id!r}"
        )
    database = Path(database).resolve()
    with closing(
        sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM effect WHERE id = ?", (effect_id,)
        ).fetchone()
    if row is None:
        raise KeyError(f"unknown Master effect: {effect_id}")
    if resolve_plan2_block_restriction(row) is None:
        raise Plan2BlockRestrictionResolutionError(
            f"unresolved Master shape: {effect_id}"
        )
    return DIRECT_EFFECT


load_master_plan2_block_restriction_effect = load_plan2_block_restriction_effect


def load_plan2_direct_card_versions(
    database: Path = DEFAULT_DATABASE,
) -> tuple[CardVersion, ...]:
    """Validate the four Common card slots that directly own the row."""

    expected = {
        card.upgrade: card
        for card in AFFECTED_CARD_VERSIONS
        if card.card_id == DIRECT_CARD_ID
    }
    if tuple(sorted(expected)) != DIRECT_CARD_UPGRADES:
        raise Plan2BlockRestrictionResolutionError(
            "shared affected-card catalog is not the four direct upgrades"
        )

    database = Path(database).resolve()
    with closing(
        sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM card WHERE id = ? ORDER BY upgrade_count",
            (DIRECT_CARD_ID,),
        ).fetchall()

    if len(rows) != len(DIRECT_CARD_UPGRADES):
        raise Plan2BlockRestrictionResolutionError(
            "direct card must have exactly four Master upgrades"
        )
    for row in rows:
        upgrade = row["upgrade_count"]
        card = expected.get(upgrade)
        if row["plan_type"] != PLAN_COMMON or card is None:
            raise Plan2BlockRestrictionResolutionError(
                f"unexpected direct card version: {upgrade!r}"
            )
        try:
            effect_ids = tuple(
                item["produceExamEffectId"]
                for item in json.loads(row["play_effects_json"])
            )
        except (TypeError, KeyError, json.JSONDecodeError) as error:
            raise Plan2BlockRestrictionResolutionError(
                f"invalid play effect slots: {DIRECT_CARD_ID}+{upgrade}"
            ) from error
        if effect_ids != card.ordered_effect_ids:
            raise Plan2BlockRestrictionResolutionError(
                f"unexpected play effect order: {DIRECT_CARD_ID}+{upgrade}"
            )
        if len(effect_ids) < 2 or effect_ids[1] != DIRECT_EFFECT_ID:
            raise Plan2BlockRestrictionResolutionError(
                f"direct restriction is not slot 1: {DIRECT_CARD_ID}+{upgrade}"
            )
        if not effect_ids[0].startswith("e_effect-exam_block-"):
            raise Plan2BlockRestrictionResolutionError(
                f"slot 0 is not Block: {DIRECT_CARD_ID}+{upgrade}"
            )
    return tuple(expected[upgrade] for upgrade in DIRECT_CARD_UPGRADES)


resolve_plan2_direct_card_versions = load_plan2_direct_card_versions


def _unresolved(
    effect: object,
    state: object,
    context: object,
    reason: str,
) -> Execution:
    return Execution(
        False,
        str(getattr(effect, "id", "<unknown>")),
        context,  # type: ignore[arg-type]
        state,  # type: ignore[arg-type]
        state,  # type: ignore[arg-type]
        None,
        "unresolved",
        None,
        False,
        False,
        (),
        (reason,),
    )


def execute_plan2_block_restriction(
    effect: object,
    state: object,
    anti_debuff: object,
    *,
    context: object = DIRECT_CARD_CONTEXT,
    plan_type: object | None = None,
) -> Execution:
    """Execute the exact direct row through the shared native projection."""

    if context is not DIRECT_CARD_CONTEXT:
        return _unresolved(effect, state, context, "direct-card-context-required")
    if plan_type is not None:
        try:
            validate_plan2_common_plan(plan_type)
        except Plan2BlockRestrictionResolutionError:
            return _unresolved(effect, state, context, "unsupported-plan")
    if not isinstance(state, BlockRestrictionRuntime):
        return _unresolved(effect, state, context, "unsupported-state-shape")
    if not isinstance(anti_debuff, AntiDebuffRuntime):
        return _unresolved(effect, state, context, "unsupported-anti-debuff-shape")
    resolved = resolve_plan2_block_restriction(
        effect, context=DIRECT_CARD_CONTEXT, plan_type=plan_type
    )
    if resolved is None:
        return _unresolved(effect, state, context, "exact-effect-context-contract")
    return _shared_execute_effect(
        resolved,
        state,
        anti_debuff,
        context=DIRECT_CARD_CONTEXT,
    )


execute_effect = execute_plan2_block_restriction
try_execute_plan2_block_restriction = execute_plan2_block_restriction

# Export the actual shared callable for identity/differential tests and audit.
shared_execute_effect = _shared_execute_effect
shared_matches_effect_contract = _shared_matches_effect_contract


__all__ = [
    "AFFECTED_CARD_VERSIONS",
    "BlockRestrictionDifference",
    "BlockRestrictionRuntime",
    "CardVersion",
    "DIRECT_CARD_CONTEXT",
    "DIRECT_CARD_ID",
    "DIRECT_CARD_UPGRADES",
    "DIRECT_CARD_VERSIONS",
    "DIRECT_EFFECT",
    "DIRECT_EFFECT_ID",
    "EFFECT_GROUP_ID",
    "EFFECT_TYPE",
    "EFFECT_TYPE_VALUE",
    "EffectContext",
    "EffectRow",
    "Execution",
    "PLAN2",
    "PLAN2_COMMON_PLAN_TYPES",
    "PLAN_COMMON",
    "Plan2BlockRestrictionDifference",
    "Plan2BlockRestrictionEffectRow",
    "Plan2BlockRestrictionExecution",
    "Plan2BlockRestrictionResolutionError",
    "Plan2BlockRestrictionRuntime",
    "load_master_plan2_block_restriction_effect",
    "load_plan2_block_restriction_effect",
    "load_plan2_direct_card_versions",
    "execute_effect",
    "execute_plan2_block_restriction",
    "matches_plan2_effect_contract",
    "resolve_plan2_block_restriction",
    "resolve_plan2_direct_card_versions",
    "shared_execute_effect",
    "shared_matches_effect_contract",
    "try_execute_plan2_block_restriction",
    "try_resolve_plan2_block_restriction",
    "validate_plan2_common_plan",
]
