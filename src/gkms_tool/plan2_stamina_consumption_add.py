"""Standalone Android v3.2.3 contract for ``ExamStaminaConsumptionAdd``.

This file intentionally does not import :mod:`plan2_state`, ``plan3_engine``,
or either native-search implementation.  The native effect is a small
status-duration operation, while the actual percentage is supplied by the
exam setting at payment time.  The public runtime below therefore keeps only
the one status type that this effect can install and accepts the other payment
inputs as an explicit, typed boundary.

The important distinction is that ``ExamStaminaConsumptionAdd`` is not a
fixed ``+1`` cost and is not a card-local grow effect.  Its Master value
fields are zero; ``effectTurn`` installs/extends the status, and
``ExamSetting.examStaminaConsumptionAddPermil`` supplies the percentage when a
later stamina cost is paid.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    INT32_MAX,
    INT32_MIN,
    IdolStatusType,
    StaminaPayment,
    StaminaPaymentSettings,
    StaminaPaymentStatus,
    calculate_effective_stamina_cost,
    split_stamina_payment,
)


EFFECT_TYPE = "ProduceExamEffectType_ExamStaminaConsumptionAdd"
STATUS_TYPE = 8
PERMANENT_TURN = -1
PLAN_TYPES = (
    "ProducePlanType_Common",
    "ProducePlanType_Plan2",
)

# Values are the local PC ``ExamSetting.yaml`` row used by the current Master
# import.  They are defaults for the standalone payment adapter, not hidden
# fields of the Add effect itself.
DEFAULT_EXAM_SETTING = {
    "consumption_down_permille": 500,
    "consumption_down_add_permille": 600,
    "consumption_add_permille": 1000,
    "consumption_add_down_permille": 1250,
    "reduce_change_value": 1,
    "concentration_permille": (2000, 2000),
    "preservation_permille": (500, 250),
    "over_preservation_permille": 0,
}

# The addresses are deliberately kept with the primitive.  The companion
# JSON audit contains the source hashes and the full catalog reconciliation.
ANDROID_STAMINA_CONSUMPTION_ADD_EVIDENCE = {
    "executor": {
        "enum_value": 69,
        "factory_branch_va": "0x7E5D3C8",
        "constructor_va": "0x7E8C678",
        "execute_va": "0x7E8C730",
        "executor_type": (
            "Campus.InGame.Exam.StaminaConsumptionAddEffectExecutor"
        ),
        "fact": "executor stores only _turn and calls the add-status try/get path",
    },
    "status": {
        "try_add_va": "0x7E97484",
        "is_add_va": "0x7E975E0",
        "get_turn_va": "0x7E97660",
        "constructor_va": "0x7E97598",
        "base_add_turn_va": "0x7E92BA0",
        "base_spend_turn_va": "0x7E92C48",
        "fact": (
            "one active same-type status is extended with AddTurn; a missing "
            "status is constructed"
        ),
    },
    "payment": {
        "consume_card_cost_va": "0x7ED040C",
        "damage_stamina_va": "0x7E5F288",
        "formula": (
            "float32 base -> stance -> down -> add(+permille) -> ceil -> "
            "add-fix -> down-fix floor -> reduce-change replacement"
        ),
        "block_split": "penetrate ? no block : block absorbs before stamina",
    },
}


class StaminaConsumptionAddContractError(ValueError):
    """A Master/runtime shape is outside the proven Add boundary."""


_EFFECT_MAPPING_KEYS = frozenset(
    {
        "id",
        "effectType",
        "effect_type",
        "effectValue1",
        "value1",
        "effectValue2",
        "value2",
        "effectCount",
        "effect_count",
        "effectTurn",
        "effect_turn",
        "produceExamStatusEnchantId",
        "status_enchant_id",
        "chainProduceExamEffectId",
        "chain_effect_id",
        "raw_json",
    }
)


def _plain_i32(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    if not INT32_MIN <= value <= INT32_MAX:
        raise StaminaConsumptionAddContractError(
            f"{label} is outside Int32: {value}"
        )
    return value


def _wrapped_i32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value & 0x80000000 else value


def _turn(value: object, label: str) -> int:
    result = _plain_i32(value, label)
    if result != PERMANENT_TURN and result < 1:
        raise StaminaConsumptionAddContractError(
            f"{label} must be -1 (permanent) or positive"
        )
    return result


def _mapping_value(
    row: Mapping[str, object],
    camel: str,
    snake: str | None = None,
    *,
    required: bool = True,
    default: object = None,
) -> object:
    if camel in row:
        if snake is not None and snake in row and row[camel] != row[snake]:
            raise StaminaConsumptionAddContractError(
                f"conflicting Master fields: {camel}/{snake}"
            )
        return row[camel]
    if snake is not None and snake in row:
        return row[snake]
    if required:
        raise StaminaConsumptionAddContractError(f"missing Master field: {camel}")
    return default


def _optional_text(value: object, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    return value


def _validate_raw_json(raw_value: object, effect: "StaminaConsumptionAddEffect") -> None:
    if raw_value is None:
        return
    if isinstance(raw_value, str):
        try:
            raw = json.loads(raw_value)
        except json.JSONDecodeError as error:
            raise StaminaConsumptionAddContractError(
                f"{effect.effect_id}: raw_json is invalid"
            ) from error
    else:
        raw = raw_value
    if not isinstance(raw, Mapping):
        raise StaminaConsumptionAddContractError(
            f"{effect.effect_id}: raw_json must be an object"
        )
    expected = {
        "id": effect.effect_id,
        "effectType": EFFECT_TYPE,
        "effectValue1": effect.value1,
        "effectValue2": effect.value2,
        "effectCount": effect.effect_count,
        "effectTurn": effect.effect_turn,
        "targetProduceCardId": "",
        "targetUpgradeCount": 0,
        "targetExamEffectType": "ProduceExamEffectType_Unknown",
        "produceCardSearchId": "",
        "movePositionType": "ProduceCardMovePositionType_Unknown",
        "produceCardSearchId2": "",
        "chainProduceExamEffectId": "",
        "chainProduceExamEffectIds": [],
        "produceExamStatusEnchantId": "",
        "produceCardStatusEnchantId": "",
        "produceCardGrowEffectIds": [],
    }
    missing = sorted(set(expected).difference(raw))
    if missing:
        raise StaminaConsumptionAddContractError(
            f"{effect.effect_id}: raw_json is missing fields: {', '.join(missing)}"
        )
    for key, expected_value in expected.items():
        if raw[key] != expected_value:
            raise StaminaConsumptionAddContractError(
                f"{effect.effect_id}: raw {key} has unsupported value "
                f"{raw[key]!r}"
            )


@dataclass(frozen=True, slots=True)
class StaminaConsumptionAddEffect:
    """The exact executable fields of one Add Master row."""

    effect_id: str
    value1: int
    value2: int
    effect_count: int
    effect_turn: int
    status_enchant_id: str = ""
    chain_effect_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.effect_id, str) or not self.effect_id:
            raise StaminaConsumptionAddContractError(
                "effect_id must be non-empty text"
            )
        _plain_i32(self.value1, "value1")
        _plain_i32(self.value2, "value2")
        _plain_i32(self.effect_count, "effect_count")
        if self.value1 != 0:
            raise StaminaConsumptionAddContractError(
                "ExamStaminaConsumptionAdd value1 must be zero; it is not a fixed value"
            )
        if self.value2 != 0 or self.effect_count != 0:
            raise StaminaConsumptionAddContractError(
                "value2 and effectCount must be zero for Add"
            )
        _turn(self.effect_turn, "effect_turn")
        if not isinstance(self.status_enchant_id, str):
            raise TypeError("status_enchant_id must be text")
        if not isinstance(self.chain_effect_id, str):
            raise TypeError("chain_effect_id must be text")
        if self.status_enchant_id or self.chain_effect_id:
            raise StaminaConsumptionAddContractError(
                "nested status/chain references are unsupported for Add"
            )

    @property
    def turn(self) -> int:
        return self.effect_turn

    @property
    def is_permanent(self) -> bool:
        return self.effect_turn == PERMANENT_TURN

    @classmethod
    def from_mapping(cls, row: Mapping[str, object]) -> "StaminaConsumptionAddEffect":
        if not isinstance(row, Mapping):
            raise TypeError("effect row must be a mapping")
        unknown = set(row).difference(_EFFECT_MAPPING_KEYS)
        if unknown:
            raise StaminaConsumptionAddContractError(
                "unsupported effect-row fields: " + ", ".join(sorted(unknown))
            )
        effect_type = _mapping_value(row, "effectType", "effect_type")
        if effect_type != EFFECT_TYPE:
            raise StaminaConsumptionAddContractError(
                f"expected {EFFECT_TYPE}, found {effect_type!r}"
            )
        effect_id = _mapping_value(row, "id")
        if not isinstance(effect_id, str) or not effect_id:
            raise TypeError("id must be non-empty text")
        effect = cls(
            effect_id=effect_id,
            value1=_plain_i32(
                _mapping_value(row, "effectValue1", "value1"), "value1"
            ),
            value2=_plain_i32(
                _mapping_value(row, "effectValue2", "value2"), "value2"
            ),
            effect_count=_plain_i32(
                _mapping_value(
                    row, "effectCount", "effect_count"
                ),
                "effect_count",
            ),
            effect_turn=_plain_i32(
                _mapping_value(row, "effectTurn", "effect_turn"),
                "effect_turn",
            ),
            status_enchant_id=_optional_text(
                _mapping_value(
                    row,
                    "produceExamStatusEnchantId",
                    "status_enchant_id",
                    required=False,
                    default="",
                ),
                "status_enchant_id",
            ),
            chain_effect_id=_optional_text(
                _mapping_value(
                    row,
                    "chainProduceExamEffectId",
                    "chain_effect_id",
                    required=False,
                    default="",
                ),
                "chain_effect_id",
            ),
        )
        _validate_raw_json(row.get("raw_json"), effect)
        return effect


def load_master_effect(
    effect_id: str,
    *,
    database: Path = DEFAULT_DATABASE,
) -> StaminaConsumptionAddEffect:
    """Load and strictly validate one local Master effect row."""

    if not isinstance(effect_id, str) or not effect_id:
        raise TypeError("effect_id must be non-empty text")
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
    return StaminaConsumptionAddEffect.from_mapping(dict(row))


def load_all_master_effects(
    *, database: Path = DEFAULT_DATABASE
) -> tuple[StaminaConsumptionAddEffect, ...]:
    """Return every current-Master row of the exact Add effect type."""

    database = Path(database).resolve()
    with closing(
        sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM effect WHERE effect_type = ? ORDER BY id",
            (EFFECT_TYPE,),
        ).fetchall()
    return tuple(StaminaConsumptionAddEffect.from_mapping(dict(row)) for row in rows)


@dataclass(frozen=True, slots=True)
class StaminaConsumptionAddCardVersion:
    """One unique card-version reference to an Add effect."""

    card_id: str
    upgrade_count: int
    plan_type: str
    effect_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.card_id, str)
            or not isinstance(self.effect_id, str)
            or not self.card_id
            or not self.effect_id
        ):
            raise ValueError("card_id and effect_id must be non-empty")
        if not isinstance(self.plan_type, str) or self.plan_type not in PLAN_TYPES:
            raise StaminaConsumptionAddContractError(
                f"unsupported plan type: {self.plan_type}"
            )
        if (
            isinstance(self.upgrade_count, bool)
            or not isinstance(self.upgrade_count, int)
            or self.upgrade_count < 0
        ):
            raise ValueError("upgrade_count must be non-negative")


def _json_array(value: object, label: str) -> list[object]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise StaminaConsumptionAddContractError(
                f"{label} is invalid JSON"
            ) from error
    if not isinstance(value, list):
        raise StaminaConsumptionAddContractError(f"{label} must be an array")
    return value


def catalog_affected_card_versions(
    *, database: Path = DEFAULT_DATABASE
) -> tuple[StaminaConsumptionAddCardVersion, ...]:
    """Catalog reachable Common/Plan2 versions without touching coverage code.

    The traversal follows the same Master-owned play, move, status-enchant,
    and chain references used by the read-only coverage report.  An Add row
    is recorded once per unique card version/effect pair.
    """

    database = Path(database).resolve()
    with closing(
        sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        cards = connection.execute(
            "SELECT * FROM card WHERE plan_type IN (?, ?) "
            "ORDER BY id, upgrade_count",
            PLAN_TYPES,
        ).fetchall()
        effects = {
            str(row["id"]): dict(row)
            for row in connection.execute("SELECT * FROM effect")
        }
        statuses = {
            str(row["id"]): dict(row)
            for row in connection.execute(
                "SELECT * FROM produce_exam_status_enchant"
            )
        }

    add_ids = {
        effect.effect_id for effect in load_all_master_effects(database=database)
    }
    found: set[tuple[str, int, str, str]] = set()
    for card in cards:
        card_id_value = card["id"]
        if not isinstance(card_id_value, str) or not card_id_value:
            raise StaminaConsumptionAddContractError(
                "card id must be non-empty text"
            )
        card_id = card_id_value
        upgrade_value = card["upgrade_count"]
        if (
            isinstance(upgrade_value, bool)
            or not isinstance(upgrade_value, int)
            or upgrade_value < 0
        ):
            raise StaminaConsumptionAddContractError(
                f"card {card_id} upgrade_count must be non-negative Int32"
            )
        _plain_i32(upgrade_value, f"card {card_id} upgrade_count")
        upgrade = upgrade_value
        plan_type = card["plan_type"]
        if not isinstance(plan_type, str):
            raise StaminaConsumptionAddContractError(
                f"card {card_id} plan_type must be text"
            )
        raw_card_value = card["raw_json"]
        if not isinstance(raw_card_value, str):
            raise StaminaConsumptionAddContractError(
                f"card {card_id} raw_json must be text"
            )
        raw_card = json.loads(raw_card_value)
        if not isinstance(raw_card, Mapping):
            raise StaminaConsumptionAddContractError(
                f"card {card_id} raw_json must be an object"
            )
        visiting: set[str] = set()

        def visit(effect_id: str) -> None:
            if not effect_id or effect_id in visiting:
                return
            row = effects.get(effect_id)
            if row is None:
                return
            visiting.add(effect_id)
            if effect_id in add_ids:
                # Parsing the exact row here makes a malformed reachable Add
                # row fail closed instead of merely being counted.
                StaminaConsumptionAddEffect.from_mapping(row)
                found.add((card_id, upgrade, plan_type, effect_id))

            status_id = _optional_text(
                row.get("status_enchant_id"),
                f"{effect_id} status_enchant_id",
            )
            status = statuses.get(status_id)
            if status is not None:
                for child in _json_array(
                    status["produce_exam_effect_ids_json"], status_id
                ):
                    if not isinstance(child, str):
                        raise StaminaConsumptionAddContractError(
                            f"{status_id} child effect id must be text"
                        )
                    visit(child)

            raw_effect_value = row["raw_json"]
            if not isinstance(raw_effect_value, str):
                raise StaminaConsumptionAddContractError(
                    f"effect {effect_id} raw_json must be text"
                )
            raw_effect = json.loads(raw_effect_value)
            if not isinstance(raw_effect, Mapping):
                raise StaminaConsumptionAddContractError(
                    f"effect {effect_id} raw_json must be an object"
                )
            chain_ids: list[str] = []
            chain_id = _optional_text(
                row.get("chain_effect_id"),
                f"{effect_id} chain_effect_id",
            )
            if chain_id:
                chain_ids.append(chain_id)
            for child in _json_array(
                raw_effect.get("chainProduceExamEffectIds", []), effect_id
            ):
                if not isinstance(child, str):
                    raise StaminaConsumptionAddContractError(
                        f"{effect_id} chain id must be text"
                    )
                chain_ids.append(child)
            for child in dict.fromkeys(chain_ids):
                visit(child)
            visiting.remove(effect_id)

        for play in _json_array(card["play_effects_json"], card_id):
            if not isinstance(play, Mapping):
                raise StaminaConsumptionAddContractError(
                    f"{card_id} contains an invalid play slot"
                )
            play_effect_id = play.get("produceExamEffectId", "")
            if play_effect_id is None:
                play_effect_id = ""
            if not isinstance(play_effect_id, str):
                raise StaminaConsumptionAddContractError(
                    f"{card_id} play effect id must be text"
                )
            visit(play_effect_id)
        for effect_id in _json_array(
            raw_card.get("moveProduceExamEffectIds", []), card_id
        ):
            if not isinstance(effect_id, str):
                raise StaminaConsumptionAddContractError(
                    f"{card_id} move effect id must be text"
                )
            visit(effect_id)

    return tuple(
        StaminaConsumptionAddCardVersion(*row)
        for row in sorted(found, key=lambda item: (item[0], item[1], item[3]))
    )


# A descriptive alias makes the catalog call site read naturally in audits.
catalog_plan2_common_effect_cards = catalog_affected_card_versions


@dataclass(frozen=True, slots=True)
class StaminaConsumptionAddLayer:
    """One native type-8 status instance.

    Native ``TryAddStaminaConsumptionAddStatus`` keeps at most one active
    instance of this status type.  Repeated finite additions call ``AddTurn``
    on that instance, so this class is still a layer for lifecycle purposes,
    but it is not a per-duration stack.
    """

    turn: int
    is_passing_turn_start: bool = False
    status_uid: int = 1
    is_turn_limited: bool | None = None

    def __post_init__(self) -> None:
        # Zero can occur transiently after native Int32 AddTurn wrapping.  A
        # Master effect itself never accepts zero; see _turn above.
        _plain_i32(self.turn, "status turn")
        if not isinstance(self.is_passing_turn_start, bool):
            raise TypeError("is_passing_turn_start must be bool")
        if isinstance(self.status_uid, bool) or not isinstance(self.status_uid, int):
            raise TypeError("status_uid must be an integer")
        _plain_i32(self.status_uid, "status_uid")
        if self.status_uid < 1:
            raise ValueError("status_uid must be positive")
        if self.is_turn_limited is None:
            if self.turn < 0 and self.turn != PERMANENT_TURN:
                raise StaminaConsumptionAddContractError(
                    "negative finite status turn requires explicit is_turn_limited=True"
                )
            object.__setattr__(
                self, "is_turn_limited", self.turn != PERMANENT_TURN
            )
        elif not isinstance(self.is_turn_limited, bool):
            raise TypeError("is_turn_limited must be bool or None")
        elif not self.is_turn_limited and self.turn != PERMANENT_TURN:
            raise StaminaConsumptionAddContractError(
                "only turn -1 may be marked permanent"
            )

    @property
    def is_permanent(self) -> bool:
        return not self.is_turn_limited


@dataclass(frozen=True, slots=True)
class StaminaConsumptionAddRuntime:
    """Minimal immutable runtime value for the native Add status."""

    layers: tuple[StaminaConsumptionAddLayer, ...] = ()
    next_status_uid: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.layers, tuple):
            raise TypeError("layers must be a tuple")
        if len(self.layers) > 1:
            raise StaminaConsumptionAddContractError(
                "native Add runtime has one same-type status, not per-turn layers"
            )
        if any(
            not isinstance(layer, StaminaConsumptionAddLayer)
            for layer in self.layers
        ):
            raise TypeError("layers must contain StaminaConsumptionAddLayer")
        _plain_i32(self.next_status_uid, "next_status_uid")
        if self.next_status_uid < 1:
            raise ValueError("next_status_uid must be positive")
        if self.layers and self.next_status_uid <= self.layers[0].status_uid:
            raise ValueError("next_status_uid must exceed the active status UID")

    @property
    def layer(self) -> StaminaConsumptionAddLayer | None:
        return self.layers[0] if self.layers else None

    @property
    def is_active(self) -> bool:
        return self.layer is not None

    @property
    def consumption_add_down(self) -> bool:
        """This primitive only installs ordinary Add, never AddDown."""

        return False


@dataclass(frozen=True, slots=True)
class StaminaConsumptionAddExecution:
    before: StaminaConsumptionAddRuntime
    after: StaminaConsumptionAddRuntime
    effect: StaminaConsumptionAddEffect
    installed: bool
    created_status_uid: int | None = None
    merged_status_uid: int | None = None


def install_stamina_consumption_add(
    state: StaminaConsumptionAddRuntime,
    effect: StaminaConsumptionAddEffect,
    *,
    block_add_status: bool = False,
) -> StaminaConsumptionAddExecution:
    """Project native ``TryAddStaminaConsumptionAddStatus``.

    The block gate is explicit because the full status collection/context is
    intentionally outside this standalone module.  If the gate is blocked,
    the native operation makes no mutation.  Otherwise an empty collection
    creates one status; an existing finite status receives native Int32
    ``AddTurn`` accumulation and keeps its freshness flag.  An existing
    permanent status cannot be extended and is left unchanged.
    """

    if not isinstance(state, StaminaConsumptionAddRuntime):
        raise TypeError("state must be StaminaConsumptionAddRuntime")
    if not isinstance(effect, StaminaConsumptionAddEffect):
        raise TypeError("effect must be StaminaConsumptionAddEffect")
    if not isinstance(block_add_status, bool):
        raise TypeError("block_add_status must be bool")
    if block_add_status:
        return StaminaConsumptionAddExecution(
            before=state,
            after=state,
            effect=effect,
            installed=False,
        )

    current = state.layer
    if current is None:
        uid = state.next_status_uid
        layer = StaminaConsumptionAddLayer(
            turn=effect.turn,
            status_uid=uid,
            is_passing_turn_start=False,
        )
        after = StaminaConsumptionAddRuntime(
            layers=(layer,), next_status_uid=uid + 1
        )
        return StaminaConsumptionAddExecution(
            before=state,
            after=after,
            effect=effect,
            installed=True,
            created_status_uid=uid,
        )

    if current.is_permanent:
        return StaminaConsumptionAddExecution(
            before=state,
            after=state,
            effect=effect,
            installed=False,
        )

    merged = replace(
        current,
        turn=_wrapped_i32(current.turn + effect.turn),
    )
    after = StaminaConsumptionAddRuntime(
        layers=(merged,), next_status_uid=state.next_status_uid
    )
    return StaminaConsumptionAddExecution(
        before=state,
        after=after,
        effect=effect,
        installed=True,
        merged_status_uid=current.status_uid,
    )


def execute_master_effect(
    state: StaminaConsumptionAddRuntime,
    effect: StaminaConsumptionAddEffect,
    *,
    block_add_status: bool = False,
) -> StaminaConsumptionAddExecution:
    """Typed alias for applying one validated Master row."""

    return install_stamina_consumption_add(
        state, effect, block_add_status=block_add_status
    )


@dataclass(frozen=True, slots=True)
class StaminaConsumptionAddTurnStartTransition:
    before: StaminaConsumptionAddRuntime
    after: StaminaConsumptionAddRuntime
    spent_status_uids: tuple[int, ...] = ()
    expired_status_uids: tuple[int, ...] = ()
    fresh_status_uids: tuple[int, ...] = ()
    permanent_status_uids: tuple[int, ...] = ()


def simulate_turn_start(
    state: StaminaConsumptionAddRuntime,
) -> StaminaConsumptionAddTurnStartTransition:
    """Apply the native fresh/passing finite-status boundary."""

    if not isinstance(state, StaminaConsumptionAddRuntime):
        raise TypeError("state must be StaminaConsumptionAddRuntime")
    current = state.layer
    if current is None:
        return StaminaConsumptionAddTurnStartTransition(state, state)

    if current.is_permanent:
        after = StaminaConsumptionAddRuntime(
            layers=(replace(current, is_passing_turn_start=True),),
            next_status_uid=state.next_status_uid,
        )
        return StaminaConsumptionAddTurnStartTransition(
            before=state,
            after=after,
            permanent_status_uids=(current.status_uid,),
        )

    if not current.is_passing_turn_start:
        if current.turn <= 0:
            after = StaminaConsumptionAddRuntime(
                layers=(), next_status_uid=state.next_status_uid
            )
            return StaminaConsumptionAddTurnStartTransition(
                before=state,
                after=after,
                expired_status_uids=(current.status_uid,),
            )
        after = StaminaConsumptionAddRuntime(
            layers=(replace(current, is_passing_turn_start=True),),
            next_status_uid=state.next_status_uid,
        )
        return StaminaConsumptionAddTurnStartTransition(
            before=state,
            after=after,
            fresh_status_uids=(current.status_uid,),
        )

    next_turn = _wrapped_i32(current.turn - 1)
    if next_turn <= 0:
        after = StaminaConsumptionAddRuntime(
            layers=(), next_status_uid=state.next_status_uid
        )
        return StaminaConsumptionAddTurnStartTransition(
            before=state,
            after=after,
            spent_status_uids=(current.status_uid,),
            expired_status_uids=(current.status_uid,),
        )

    after = StaminaConsumptionAddRuntime(
        layers=(replace(current, turn=next_turn),),
        next_status_uid=state.next_status_uid,
    )
    return StaminaConsumptionAddTurnStartTransition(
        before=state,
        after=after,
        spent_status_uids=(current.status_uid,),
    )


def spend_turn_start(state: StaminaConsumptionAddRuntime) -> StaminaConsumptionAddRuntime:
    """Convenience projection returning only the post-boundary runtime."""

    return simulate_turn_start(state).after


@dataclass(frozen=True, slots=True)
class StaminaConsumptionAddSettings:
    """Typed local ``ExamSetting`` inputs consumed by the payment evaluator."""

    concentration_permille: tuple[int, int] = (2000, 2000)
    preservation_permille: tuple[int, int] = (500, 250)
    over_preservation_permille: int = 0
    consumption_down_permille: int = 500
    consumption_down_add_permille: int = 600
    consumption_add_permille: int = 1000
    consumption_add_down_permille: int = 1250
    reduce_change_value: int = 1

    def __post_init__(self) -> None:
        for label, values in (
            ("concentration_permille", self.concentration_permille),
            ("preservation_permille", self.preservation_permille),
        ):
            if (
                not isinstance(values, tuple)
                or len(values) != 2
                or any(isinstance(value, bool) or not isinstance(value, int) for value in values)
            ):
                raise TypeError(f"{label} must be a pair of integers")
            for value in values:
                _plain_i32(value, label)
        for label in (
            "over_preservation_permille",
            "consumption_down_permille",
            "consumption_down_add_permille",
            "consumption_add_permille",
            "consumption_add_down_permille",
            "reduce_change_value",
        ):
            _plain_i32(getattr(self, label), label)

    def native(self) -> StaminaPaymentSettings:
        return StaminaPaymentSettings(
            concentration_permille=self.concentration_permille,
            preservation_permille=self.preservation_permille,
            over_preservation_permille=self.over_preservation_permille,
            consumption_down_permille=self.consumption_down_permille,
            consumption_down_add_permille=self.consumption_down_add_permille,
            consumption_add_permille=self.consumption_add_permille,
            consumption_add_down_permille=self.consumption_add_down_permille,
            reduce_change_value=self.reduce_change_value,
        )


@dataclass(frozen=True, slots=True)
class StaminaConsumptionAddPaymentContext:
    """Inputs at the native ``DamageStamina`` payment boundary.

    ``base_cost`` must already be the result of the card getter or a selected
    search-card override.  This keeps card-local grow effects outside the
    standalone Add contract while retaining the exact payment ordering.
    """

    base_cost: int
    current_stamina: int
    max_stamina: int
    current_block: int
    penetrate: bool = False
    idol_status_type: IdolStatusType = IdolStatusType.UNKNOWN
    idol_status_step: int = 1
    consumption_down: bool = False
    consumption_down_add: bool = False
    consumption_add_down: bool = False
    consumption_add_fix: int = 0
    consumption_down_fix: int = 0
    reduce_change_threshold: int = -1
    reduce_change_active: bool = False

    def __post_init__(self) -> None:
        for label, value in (
            ("base_cost", self.base_cost),
            ("current_stamina", self.current_stamina),
            ("max_stamina", self.max_stamina),
            ("current_block", self.current_block),
            ("idol_status_step", self.idol_status_step),
            ("consumption_add_fix", self.consumption_add_fix),
            ("consumption_down_fix", self.consumption_down_fix),
            ("reduce_change_threshold", self.reduce_change_threshold),
        ):
            _plain_i32(value, label)
        if self.base_cost < 0:
            raise StaminaConsumptionAddContractError(
                "base_cost must be non-negative at DamageStamina boundary"
            )
        if (
            self.current_stamina < 0
            or self.max_stamina < 0
            or self.current_block < 0
        ):
            raise StaminaConsumptionAddContractError(
                "payment resources must be non-negative"
            )
        if self.current_stamina > self.max_stamina:
            raise StaminaConsumptionAddContractError(
                "current_stamina cannot exceed max_stamina"
            )
        if not isinstance(self.penetrate, bool):
            raise TypeError("penetrate must be bool")
        if not isinstance(self.idol_status_type, IdolStatusType):
            raise TypeError("idol_status_type must be IdolStatusType")
        for label in (
            "consumption_down",
            "consumption_down_add",
            "consumption_add_down",
            "reduce_change_active",
        ):
            if not isinstance(getattr(self, label), bool):
                raise TypeError(f"{label} must be bool")


@dataclass(frozen=True, slots=True)
class StaminaConsumptionAddPaymentResult:
    context: StaminaConsumptionAddPaymentContext
    after_runtime: StaminaConsumptionAddRuntime
    add_active: bool
    add_permille: int
    payment: StaminaPayment

    @property
    def effective_cost(self) -> int:
        return self.payment.effective_cost

    @property
    def stamina_loss(self) -> int:
        return self.payment.stamina_loss

    @property
    def block_loss(self) -> int:
        return self.payment.block_loss

    @property
    def new_stamina(self) -> int:
        return self.payment.new_stamina

    @property
    def new_block(self) -> int:
        return self.payment.new_block


def evaluate_stamina_payment(
    state: StaminaConsumptionAddRuntime,
    context: StaminaConsumptionAddPaymentContext,
    *,
    settings: StaminaConsumptionAddSettings | None = None,
) -> StaminaConsumptionAddPaymentResult:
    """Evaluate one ordinary stamina payment after an Add status is installed.

    The status is read by the native ``isUse=true`` getter for this cost; it
    is not removed by payment.  Turn lifetime is changed only by the explicit
    TurnStart projection above.  The returned ``after_runtime`` is therefore
    the same immutable runtime value.
    """

    if not isinstance(state, StaminaConsumptionAddRuntime):
        raise TypeError("state must be StaminaConsumptionAddRuntime")
    if not isinstance(context, StaminaConsumptionAddPaymentContext):
        raise TypeError("context must be StaminaConsumptionAddPaymentContext")
    if settings is None:
        settings = StaminaConsumptionAddSettings()
    if not isinstance(settings, StaminaConsumptionAddSettings):
        raise TypeError("settings must be StaminaConsumptionAddSettings")

    layer = state.layer
    add_active = layer is not None
    selected_add_permille = (
        settings.consumption_add_down_permille
        if context.consumption_add_down
        else settings.consumption_add_permille
    )
    status = StaminaPaymentStatus(
        idol_status_type=context.idol_status_type,
        idol_status_step=context.idol_status_step,
        consumption_down=context.consumption_down,
        consumption_down_add=context.consumption_down_add,
        consumption_add=add_active,
        consumption_add_down=context.consumption_add_down,
        consumption_add_fix=context.consumption_add_fix,
        consumption_down_fix=context.consumption_down_fix,
        reduce_change_threshold=context.reduce_change_threshold,
        reduce_change_active=context.reduce_change_active,
    )
    effective = calculate_effective_stamina_cost(
        context.base_cost,
        status=status,
        settings=settings.native(),
    )
    payment = split_stamina_payment(
        effective,
        penetrate=context.penetrate,
        current_stamina=context.current_stamina,
        max_stamina=context.max_stamina,
        current_block=context.current_block,
    )
    return StaminaConsumptionAddPaymentResult(
        context=context,
        after_runtime=state,
        add_active=add_active,
        add_permille=selected_add_permille if add_active else 0,
        payment=payment,
    )


def calculate_stamina_consumption_add_cost(
    state: StaminaConsumptionAddRuntime,
    context: StaminaConsumptionAddPaymentContext,
    *,
    settings: StaminaConsumptionAddSettings | None = None,
) -> int:
    """Return only the effective cost from ``evaluate_stamina_payment``."""

    return evaluate_stamina_payment(state, context, settings=settings).effective_cost


__all__ = [
    "ANDROID_STAMINA_CONSUMPTION_ADD_EVIDENCE",
    "DEFAULT_EXAM_SETTING",
    "EFFECT_TYPE",
    "PERMANENT_TURN",
    "PLAN_TYPES",
    "STATUS_TYPE",
    "StaminaConsumptionAddCardVersion",
    "StaminaConsumptionAddContractError",
    "StaminaConsumptionAddEffect",
    "StaminaConsumptionAddExecution",
    "StaminaConsumptionAddLayer",
    "StaminaConsumptionAddPaymentContext",
    "StaminaConsumptionAddPaymentResult",
    "StaminaConsumptionAddRuntime",
    "StaminaConsumptionAddSettings",
    "StaminaConsumptionAddTurnStartTransition",
    "calculate_stamina_consumption_add_cost",
    "catalog_affected_card_versions",
    "catalog_plan2_common_effect_cards",
    "execute_master_effect",
    "evaluate_stamina_payment",
    "install_stamina_consumption_add",
    "load_all_master_effects",
    "load_master_effect",
    "simulate_turn_start",
    "spend_turn_start",
]
