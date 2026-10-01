"""GUID-preserving native zone/RNG boundary for Plan 3 simulation.

This module owns only facts that the scalar :class:`Plan3State` cannot retain:
per-instance GUIDs, layered upgrades, native Deck order, the shared uint32 RNG
state, and current-turn HandAdd support usage.  It intentionally does not
execute card effects.  Callers project the resulting effective card refs into
``Plan3State`` and use the existing strict Plan 3 kernel for arithmetic.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from functools import lru_cache
import json
from pathlib import Path
import sqlite3
from typing import TypeAlias

import yaml

from .audition_local_save_state import (
    LocalSaveExamCard,
    LocalSaveExamCardRuntimeState,
    LocalSaveExamState,
)
from .audition_native_ordered_zones import NativeOrderedCardInstance
from .audition_native_support import (
    NativeHandAddCard,
    NativeHandAddSupportResult,
    evaluate_native_hand_add_support,
)
from .audition_support_runtime import SupportUpgradeRuntimeInput
from .card_search import (
    ProduceCardSearchRule,
    load_produce_card_search,
    match_plan3_card_move_search,
    validate_plan3_card_move_search,
)
from .exam_native_rng import (
    INT32_MAX,
    INT32_MIN,
    UINT32_MASK,
    FixedDeckOrderError,
    next_int32,
    next_range,
    order_native_pool,
    shuffle_unfixed_pool,
)
from .master_db import DEFAULT_DATABASE
from .plan3_engine import (
    DEFAULT_MASTER_DIR,
    EFFECT_ADD_GROW,
    EFFECT_UNKNOWN,
    load_plan3_effect,
    MOVE_GRAVE,
    MOVE_HAND,
    MOVE_LOST,
    MOVE_UNKNOWN,
    PICK_COUNT_UNKNOWN,
    PICK_RANGE_ALL,
    PICK_RANGE_UNKNOWN,
    SEARCH_DECK_ALL,
    Plan3Effect,
    Plan3CardRef,
    Plan3State,
)
from .plan3_search_stamina_change import Plan3SearchStaminaRuntime
from .plan3_anti_debuff import (
    AntiDebuffBlockResult,
    AntiDebuffRuntime,
    ExamStatusEffectTargetType,
    try_block_status_addition,
)
from .enthusiastic_runtime import (
    EnthusiasticReceipt,
    EnthusiasticRuntime,
    spend_enthusiastic_turn,
)


MOVE_HOLD = "ProduceCardMovePositionType_Hold"
SUPPORTED_NATIVE_DESTINATIONS = frozenset({MOVE_GRAVE, MOVE_LOST, MOVE_HOLD})

SupportInputs: TypeAlias = (
    Mapping[str, SupportUpgradeRuntimeInput]
    | Iterable[SupportUpgradeRuntimeInput]
)


_RUNTIME_GROW_EFFECT_FIELDS = frozenset(
    {
        "_id",
        "_effectType",
        "_value",
        "_playProduceExamTriggerId",
        "_playEffectProduceExamTriggerId",
        "_targetPlayEffectProduceExamTriggerIdList",
        "_playProduceExamEffectId",
        "_targetPlayProduceExamEffectIdList",
        "_produceCardStatusEnchantId",
        "_playMovePositionType",
    }
)
_GROW_EFFECT_LESSON_ADD = 1
_GROW_EFFECT_LESSON_COUNT_ADD = 3
_GROW_EFFECT_BLOCK_ADD = 5
_GROW_EFFECT_COST_REDUCE = 12
_GROW_EFFECT_COST_ADD = 13
_GROW_EFFECT_COST_PENETRATE_REDUCE = 14
_GROW_EFFECT_COST_PENETRATE_ADD = 15
_GROW_EFFECT_FULL_POWER_POINT_ADD = 7
_GROW_EFFECT_COST_FULL_POWER_POINT_ADD = 49
_GROW_TYPE_LESSON_ADD = "ProduceCardGrowEffectType_LessonAdd"
_GROW_TYPE_LESSON_COUNT_ADD = "ProduceCardGrowEffectType_LessonCountAdd"
_GROW_TYPE_REVIEW_ADD = "ProduceCardGrowEffectType_ReviewAdd"
_GROW_TYPE_LESSON_DEPEND_REVIEW_ADD = (
    "ProduceCardGrowEffectType_LessonDependExamReviewAdd"
)
_GROW_TYPE_EFFECT_ADD = "ProduceCardGrowEffectType_EffectAdd"
_GROW_TYPE_INITIAL_ADD = "ProduceCardGrowEffectType_InitialAdd"
_GROW_TYPE_BLOCK_ADD = "ProduceCardGrowEffectType_BlockAdd"
_GROW_TYPE_AGGRESSIVE_ADD = "ProduceCardGrowEffectType_AggressiveAdd"
_GROW_TYPE_COST_REDUCE = "ProduceCardGrowEffectType_CostReduce"
_GROW_TYPE_COST_ADD = "ProduceCardGrowEffectType_CostAdd"
_GROW_TYPE_COST_PENETRATE_REDUCE = (
    "ProduceCardGrowEffectType_CostPenetrateReduce"
)
_GROW_TYPE_COST_PENETRATE_ADD = (
    "ProduceCardGrowEffectType_CostPenetrateAdd"
)
_GROW_TYPE_STAMINA_DOWN_TURN_ADD = "ProduceCardGrowEffectType_StaminaConsumptionDownTurnAdd"
_GROW_TYPE_FULL_POWER_POINT_ADD = (
    "ProduceCardGrowEffectType_FullPowerPointAdd"
)
_GROW_TYPE_COST_FULL_POWER_POINT_ADD = (
    "ProduceCardGrowEffectType_CostFullPowerPointAdd"
)
_PHASE_CARD_PLAY_AFTER = "ProduceExamPhaseType_ExamCardPlayAfter"
_FIELD_FULL_POWER_UP = "ProduceExamFieldStatusType_FullPowerUp"
_SEARCH_TARGET_IS_SELF = "p_card_search-target_is_self"
_SEARCH_TARGET_CONCENTRATION_GROUP = (
    "p_card_search-target-effect_group-visible-exam_concentration-000"
)
_CONCENTRATION_EFFECT_GROUP = (
    "effect_group-visible-exam_concentration-000"
)
_PHASE_STANCE_CHANGE_COUNT_INTERVAL = (
    "ProduceExamPhaseType_ExamStanceChangeCountInterval"
)
_PHASE_STANCE_CHANGE_FULL_POWER = (
    "ProduceExamPhaseType_ExamStanceChangeFullPower"
)
_PHASE_STANCE_CHANGE_CONCENTRATION = (
    "ProduceExamPhaseType_ExamStanceChangeConcentration"
)
_PHASE_STANCE_CHANGE_PRESERVATION = (
    "ProduceExamPhaseType_ExamStanceChangePreservation"
)
_PHASE_VALUE_STANCE_CHANGE_COUNT_INTERVAL = 35
_PHASE_VALUE_STANCE_CHANGE_COUNT = 36
_PHASE_VALUE_STANCE_CHANGE_CONCENTRATION = 37
_PHASE_VALUE_STANCE_CHANGE_PRESERVATION = 38
_PHASE_VALUE_STANCE_CHANGE_FULL_POWER = 39
_PHASE_VALUE_CARD_PLAY_AFTER = 3
_EMPTY_STATUS_EFFECT = {
    "_id": "",
    "_produceExamTriggerId": "",
    "_produceCardGrowEffectIdList": [],
    "_effectGroupIdList": [],
    "_triggerCount": 0,
    "_phaseCountDictionary": {"_list": []},
    "_spendTurn": 0,
    "_spendCount": 0,
}
_STATUS_EFFECT_FIELDS = frozenset(_EMPTY_STATUS_EFFECT)
_NEUTRAL_GROW_MASTER_FIELDS = {
    "costType": "ExamCostType_Unknown",
    "playProduceExamTriggerId": "",
    "playEffectProduceExamTriggerId": "",
    "targetPlayEffectProduceExamTriggerIds": [],
    "playProduceExamEffectId": "",
    "targetPlayProduceExamEffectIds": [],
    "produceCardStatusEnchantId": "",
    "playMovePositionType": "ProduceCardMovePositionType_Unknown",
    "effectGroupIds": [],
}


class Plan3NativeStateError(ValueError):
    """A stable fail-closed native-zone boundary error."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


@dataclass(frozen=True, order=True, slots=True)
class Plan3NativeGrowEffect:
    """One statically resolved card-grow contribution."""

    id: str
    effect_type: str
    value: int


@dataclass(frozen=True, order=True, slots=True)
class Plan3NativeFullPowerPointAdd:
    """GUID-owned ``ProduceCardGrowEffectType_FullPowerPointAdd`` total.

    This is deliberately separate from ``CostFullPowerPointAdd``.  The
    former changes the value of a card's ``ExamFullPowerPoint`` effect while
    the latter changes the card's Full Power cost.  Keeping the source grow
    IDs and the originating Customize IDs here makes the projection survive
    moves, draws, and upgrades without flattening the two native operations
    into one cost scalar.
    """

    value: int = 0
    grow_effect_ids: tuple[str, ...] = ()
    customize_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.value, int)
            or isinstance(self.value, bool)
            or self.value < 0
        ):
            raise Plan3NativeStateError(
                "invalid-runtime-full-power-point-add", repr(self.value)
            )
        grow_ids = tuple(self.grow_effect_ids)
        if any(not isinstance(value, str) or not value for value in grow_ids):
            raise Plan3NativeStateError(
                "invalid-runtime-full-power-point-add-effect-id"
            )
        customize_ids = tuple(self.customize_ids)
        if any(
            not isinstance(value, str) or not value for value in customize_ids
        ):
            raise Plan3NativeStateError(
                "invalid-runtime-full-power-point-add-customize-id"
            )
        if len(set(customize_ids)) != len(customize_ids):
            raise Plan3NativeStateError(
                "duplicate-runtime-full-power-point-add-customize-id"
            )
        object.__setattr__(self, "grow_effect_ids", grow_ids)
        object.__setattr__(self, "customize_ids", customize_ids)

    @property
    def amount(self) -> int:
        """Compatibility spelling for callers that use aggregate amounts."""

        return self.value

    @property
    def effect_ids(self) -> tuple[str, ...]:
        """Compatibility spelling for source grow lineage."""

        return self.grow_effect_ids


# The longer name is useful at serialization/API boundaries and keeps the
# concise class name available to callers that mirror the Master enum.
Plan3NativeFullPowerPointAddAggregate = Plan3NativeFullPowerPointAdd


@dataclass(frozen=True, slots=True)
class Plan3NativeRuntimeCustomizationEffect:
    """One ordered, active ``CardCustomize`` grow contribution.

    ``produceCardGrowEffectIds`` is not always a scalar grow.  In
    particular, the ``EffectAdd`` family points at a second Master effect,
    while ``overwriteProduceCardGrowEffectType`` on the Customize row
    describes the user-facing grow family.  Keeping both the source grow
    identity and the resolved effect here prevents callers from flattening
    an added effect into a scalar delta or losing its slot order.
    """

    slot_index: int
    customize_id: str
    customize_count: int
    grow_effect_id: str
    grow_effect_type: str
    value: int
    added_effect: Plan3Effect | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.slot_index, int)
            or isinstance(self.slot_index, bool)
            or self.slot_index < 0
        ):
            raise Plan3NativeStateError(
                "invalid-runtime-customization-slot", self.customize_id
            )
        for value, label in (
            (self.customize_id, "customize_id"),
            (self.grow_effect_id, "grow_effect_id"),
            (self.grow_effect_type, "grow_effect_type"),
        ):
            if not isinstance(value, str) or not value:
                raise Plan3NativeStateError(
                    "invalid-runtime-customization-text", label
                )
        if (
            not isinstance(self.customize_count, int)
            or isinstance(self.customize_count, bool)
            or self.customize_count <= 0
        ):
            raise Plan3NativeStateError(
                "invalid-runtime-customization-count", self.customize_id
            )
        if (
            not isinstance(self.value, int)
            or isinstance(self.value, bool)
            or self.value < 0
        ):
            raise Plan3NativeStateError(
                "invalid-runtime-customization-value", self.grow_effect_id
            )
        if self.added_effect is not None and not isinstance(
            self.added_effect, Plan3Effect
        ):
            raise TypeError("added_effect must be Plan3Effect or None")


@dataclass(frozen=True, slots=True)
class Plan3NativeRuntimeCustomization:
    """Typed ordered projection of one card instance's active customizations."""

    customize_ids: tuple[str, ...]
    effects: tuple[Plan3NativeRuntimeCustomizationEffect, ...]

    def __post_init__(self) -> None:
        ids = tuple(self.customize_ids)
        if any(not isinstance(value, str) or not value for value in ids):
            raise Plan3NativeStateError("invalid-runtime-customize-id")
        if len(set(ids)) != len(ids):
            raise Plan3NativeStateError("duplicate-runtime-customize-id")
        effects = tuple(self.effects)
        if any(
            not isinstance(value, Plan3NativeRuntimeCustomizationEffect)
            for value in effects
        ):
            raise TypeError(
                "effects must contain Plan3NativeRuntimeCustomizationEffect values"
            )
        previous_slot = -1
        for effect in effects:
            if (
                effect.customize_id not in ids
                or effect.slot_index < previous_slot
            ):
                raise Plan3NativeStateError("runtime-customization-order-invalid")
            previous_slot = effect.slot_index
        object.__setattr__(self, "customize_ids", ids)
        object.__setattr__(self, "effects", effects)

    @property
    def review_add(self) -> int:
        return sum(
            value.value
            for value in self.effects
            if value.grow_effect_type == _GROW_TYPE_REVIEW_ADD
        )

    @property
    def lesson_add(self) -> int:
        return sum(
            value.value
            for value in self.effects
            if value.grow_effect_type == _GROW_TYPE_LESSON_ADD
        )

    @property
    def lesson_count_add(self) -> int:
        return sum(
            value.value
            for value in self.effects
            if value.grow_effect_type == _GROW_TYPE_LESSON_COUNT_ADD
        )

    @property
    def lesson_depend_review_add(self) -> int:
        return sum(
            value.value
            for value in self.effects
            if value.grow_effect_type == _GROW_TYPE_LESSON_DEPEND_REVIEW_ADD
        )

    @property
    def full_power_point_add(self) -> int:
        return sum(
            value.value
            for value in self.effects
            if value.grow_effect_type == _GROW_TYPE_FULL_POWER_POINT_ADD
        )

    @property
    def full_power_point_add_effect_ids(self) -> tuple[str, ...]:
        return tuple(
            value.grow_effect_id
            for value in self.effects
            if value.grow_effect_type == _GROW_TYPE_FULL_POWER_POINT_ADD
        )

    @property
    def full_power_point_add_customize_ids(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                value.customize_id
                for value in self.effects
                if value.grow_effect_type == _GROW_TYPE_FULL_POWER_POINT_ADD
            )
        )

    @property
    def block_add(self) -> int:
        return sum(
            value.value
            for value in self.effects
            if value.grow_effect_type == _GROW_TYPE_BLOCK_ADD
        )

    @property
    def cost_reduce(self) -> int:
        return sum(
            value.value
            for value in self.effects
            if value.grow_effect_type == _GROW_TYPE_COST_REDUCE
        )

    @property
    def cost_penetrate_reduce_effects(self) -> tuple[Plan3NativeRuntimeCustomizationEffect, ...]:
        return tuple(value for value in self.effects
                     if value.grow_effect_type == _GROW_TYPE_COST_PENETRATE_REDUCE)

    @property
    def stamina_consumption_down_turn_add(self) -> int:
        return sum(value.value for value in self.effects if value.grow_effect_type == _GROW_TYPE_STAMINA_DOWN_TURN_ADD)

    @property
    def cost_reduce_effects(
        self,
    ) -> tuple[Plan3NativeRuntimeCustomizationEffect, ...]:
        """Return CostReduce rows in native Customize slot/list order."""

        return tuple(
            value
            for value in self.effects
            if value.grow_effect_type == _GROW_TYPE_COST_REDUCE
        )


@dataclass(frozen=True, order=True, slots=True)
class Plan3NativeCardGrowStatus:
    """Persisted per-card listener, validated against StatusEnchant Master."""

    id: str
    trigger_id: str
    trigger_kind: str
    interval: int
    grow_effects: tuple[Plan3NativeGrowEffect, ...]
    trigger_count: int
    spend_count: int
    spend_turn: int
    phase_counts: tuple[tuple[int, int], ...] = ()
    search_id: str = ""
    target_effect_group_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.id, "status_id")
        _text(self.trigger_id, "status_trigger_id")
        if self.trigger_kind not in {
            "stance_change_interval",
            "stance_change_concentration",
            "stance_change_preservation",
            "full_power",
            "card_play_after_self_full_power",
            "card_play_after_effect_group",
        }:
            raise Plan3NativeStateError(
                "unsupported-card-grow-trigger", self.trigger_kind
            )
        if (
            self.trigger_kind == "stance_change_interval"
            and self.interval not in {1, 2, 3}
        ):
            raise Plan3NativeStateError(
                "unsupported-card-grow-trigger-interval", str(self.interval)
            )
        if self.trigger_kind in {
            "stance_change_concentration",
            "stance_change_preservation",
            "full_power",
            "card_play_after_self_full_power",
            "card_play_after_effect_group",
        } and self.interval != 0:
            raise Plan3NativeStateError("invalid-card-grow-trigger-interval")
        if not self.grow_effects:
            raise Plan3NativeStateError("card-grow-status-effects-empty", self.id)
        if any(
            effect.effect_type
            not in {
                _GROW_TYPE_LESSON_ADD,
                _GROW_TYPE_LESSON_COUNT_ADD,
                _GROW_TYPE_BLOCK_ADD,
                _GROW_TYPE_FULL_POWER_POINT_ADD,
                _GROW_TYPE_COST_ADD,
                _GROW_TYPE_COST_PENETRATE_REDUCE,
                _GROW_TYPE_COST_FULL_POWER_POINT_ADD,
            }
            for effect in self.grow_effects
        ):
            raise Plan3NativeStateError(
                "unsupported-card-grow-status-effect", self.id
            )
        for value, label in (
            (self.trigger_count, "trigger_count"),
            (self.spend_count, "spend_count"),
            (self.spend_turn, "spend_turn"),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= INT32_MAX
            ):
                raise Plan3NativeStateError("invalid-card-grow-status-count", label)
        if self.trigger_count > 0 and self.spend_count > self.trigger_count:
            raise Plan3NativeStateError("card-grow-status-spend-mismatch", self.id)
        groups = tuple(self.target_effect_group_ids)
        if any(not isinstance(value, str) or not value for value in groups):
            raise Plan3NativeStateError(
                "invalid-card-grow-target-effect-groups", self.id
            )
        object.__setattr__(self, "target_effect_group_ids", groups)
        if self.trigger_kind == "card_play_after_effect_group":
            if (
                self.search_id != _SEARCH_TARGET_CONCENTRATION_GROUP
                or groups != (_CONCENTRATION_EFFECT_GROUP,)
            ):
                raise Plan3NativeStateError(
                    "unsupported-card-grow-trigger-search", self.search_id
                )
        elif self.trigger_kind == "card_play_after_self_full_power":
            if self.search_id not in {"", _SEARCH_TARGET_IS_SELF} or groups:
                raise Plan3NativeStateError(
                    "unsupported-card-grow-trigger-search", self.search_id
                )
        elif self.search_id or groups:
            raise Plan3NativeStateError(
                "unexpected-card-grow-target-effect-groups", self.id
            )
        keys = tuple(key for key, _value in self.phase_counts)
        if len(set(keys)) != len(keys) or any(
            not isinstance(key, int)
            or isinstance(key, bool)
            or not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= INT32_MAX
            for key, value in self.phase_counts
        ):
            raise Plan3NativeStateError("invalid-card-grow-phase-counts", self.id)

    def phase_count(self, phase: int) -> int:
        return dict(self.phase_counts).get(phase, 0)

    def with_phase_increment(self, phase: int, count: int) -> "Plan3NativeCardGrowStatus":
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
        ):
            raise Plan3NativeStateError(
                "invalid-card-grow-phase-increment", self.id
            )
        if count == 0:
            return self
        values = dict(self.phase_counts)
        current = values.get(phase, 0)
        if current > INT32_MAX - count:
            raise Plan3NativeStateError(
                "card-grow-phase-count-overflow", self.id
            )
        values[phase] = current + count
        return replace(self, phase_counts=tuple(sorted(values.items())))

    def advance_stance_change_callbacks(
        self,
        count: int,
    ) -> tuple["Plan3NativeCardGrowStatus", int]:
        """Advance native generic and interval stance callbacks once.

        A successful stance change first increments the generic stance count
        callback (phase 36, then phase 35).  The later interval-command
        callback increments phase 35 once more.  Trigger crossings are owned
        by the generic count, so the duplicated phase-35 bookkeeping must not
        fire the listener twice.
        """

        before = self.phase_count(_PHASE_VALUE_STANCE_CHANGE_COUNT)
        advanced = self.with_phase_increment(
            _PHASE_VALUE_STANCE_CHANGE_COUNT,
            count,
        )
        advanced = advanced.with_phase_increment(
            _PHASE_VALUE_STANCE_CHANGE_COUNT_INTERVAL,
            count,
        )
        advanced = advanced.with_phase_increment(
            _PHASE_VALUE_STANCE_CHANGE_COUNT_INTERVAL,
            count,
        )
        requested = (
            advanced.phase_count(_PHASE_VALUE_STANCE_CHANGE_COUNT)
            // self.interval
            - before // self.interval
        )
        return advanced, requested

    def capped_fire_count(self, requested: int) -> int:
        if (
            not isinstance(requested, int)
            or isinstance(requested, bool)
            or requested < 0
        ):
            raise Plan3NativeStateError(
                "invalid-card-grow-fire-count", self.id
            )
        fires = (
            requested
            if self.trigger_count == 0
            else min(requested, self.trigger_count - self.spend_count)
        )
        if self.spend_count > INT32_MAX - fires:
            raise Plan3NativeStateError("card-grow-spend-overflow", self.id)
        return fires


@dataclass(frozen=True, slots=True)
class _Plan3CardRuntimeMaster:
    status_id: str
    customize_ids: tuple[str, ...]


def _load_yaml_rows(path: Path) -> tuple[Mapping[str, object], ...]:
    if not path.is_file():
        raise Plan3NativeStateError("card-runtime-master-missing", str(path))
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    payload = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    if not isinstance(payload, list) or not all(
        isinstance(row, Mapping) for row in payload
    ):
        raise Plan3NativeStateError("card-runtime-master-invalid", str(path))
    return tuple(payload)


@lru_cache(maxsize=8)
def _runtime_master_indexes(
    master_dir: Path,
) -> tuple[
    Mapping[str, Mapping[str, object]],
    Mapping[str, Mapping[str, object]],
    Mapping[str, Mapping[str, object]],
    Mapping[str, Mapping[str, object]],
]:
    master_dir = Path(master_dir)

    def index(
        name: str, *, customize: bool = False
    ) -> Mapping[str, Mapping[str, object]]:
        result: dict[str, Mapping[str, object]] = {}
        for row in _load_yaml_rows(master_dir / name):
            row_id = row.get("id")
            key = (
                f"{row_id}\0{row.get('customizeCount')}"
                if customize
                else row_id
            )
            if not isinstance(row_id, str) or not row_id or key in result:
                raise Plan3NativeStateError(
                    "card-runtime-master-id-invalid", f"{name}:{row_id!r}"
                )
            result[key] = row
        return result

    return (
        index("ProduceCardGrowEffect.yaml"),
        index("ProduceCardStatusEnchant.yaml"),
        index("ProduceExamTrigger.yaml"),
        index("ProduceCardCustomize.yaml", customize=True),
    )


@lru_cache(maxsize=2048)
def _card_runtime_master(
    card_id: str,
    upgrade: int,
    database: Path,
) -> _Plan3CardRuntimeMaster:
    try:
        with sqlite3.connect(Path(database)) as connection:
            row = connection.execute(
                "SELECT raw_json FROM card WHERE id = ? AND upgrade_count = ?",
                (card_id, upgrade),
            ).fetchone()
    except sqlite3.Error as error:
        raise Plan3NativeStateError(
            "card-runtime-card-master-unavailable", f"{card_id}+{upgrade}:{error}"
        ) from error
    if row is None:
        raise Plan3NativeStateError(
            "card-runtime-card-master-missing", f"{card_id}+{upgrade}"
        )
    try:
        raw = json.loads(row[0])
    except (TypeError, ValueError) as error:
        raise Plan3NativeStateError(
            "card-runtime-card-master-invalid", f"{card_id}+{upgrade}"
        ) from error
    if not isinstance(raw, Mapping):
        raise Plan3NativeStateError(
            "card-runtime-card-master-invalid", f"{card_id}+{upgrade}"
        )
    status_id = raw.get("produceCardStatusEnchantId", "")
    customize_ids = raw.get("produceCardCustomizeIds", [])
    if not isinstance(status_id, str) or not isinstance(customize_ids, list) or any(
        not isinstance(value, str) or not value for value in customize_ids
    ):
        raise Plan3NativeStateError(
            "card-runtime-card-master-invalid", f"{card_id}+{upgrade}"
        )
    return _Plan3CardRuntimeMaster(status_id, tuple(customize_ids))


def _grow_effect_from_master(
    effect_id: str,
    *,
    master_dir: Path,
    allowed_types: frozenset[str],
) -> Plan3NativeGrowEffect:
    grow_index, _status_index, _trigger_index, _customize_index = (
        _runtime_master_indexes(Path(master_dir))
    )
    row = grow_index.get(effect_id)
    if row is None:
        raise Plan3NativeStateError("runtime-grow-effect-master-missing", effect_id)
    effect_type = row.get("effectType")
    value = row.get("value")
    if effect_type not in allowed_types:
        raise Plan3NativeStateError(
            "unsupported-runtime-grow-effect-type", f"{effect_id}:{effect_type!r}"
        )
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise Plan3NativeStateError("invalid-runtime-grow-effect-value", effect_id)
    if any(row.get(key) != expected for key, expected in _NEUTRAL_GROW_MASTER_FIELDS.items()):
        raise Plan3NativeStateError("conditional-runtime-grow-effect-unsupported", effect_id)
    return Plan3NativeGrowEffect(effect_id, effect_type, value)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Plan3NativeStateError("invalid-text", label)
    return value


def _upgrade(value: object, label: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= 3
    ):
        raise Plan3NativeStateError("invalid-upgrade", label)
    return value


def _uint32(value: object, label: str = "random_state") -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= UINT32_MASK
    ):
        raise Plan3NativeStateError("invalid-random-state", label)
    return value


def _parse_plan3_runtime_grow(
    runtime: LocalSaveExamCardRuntimeState,
    *,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> tuple[
    tuple[str, ...],
    int,
    int,
    int,
    int,
    int,
    int,
    int,
    int,
    int,
    tuple[str, ...],
]:
    """Parse exact persisted typed grow lineage and materialized rows.

    Support and memory effects materialize card growth directly on each
    ``ExamCardModel``. Native grow type 1 adds to the lesson effect and type
    3 adds to its lesson hit count, 5 adds to block, 7 adds to the value of
    an ``ExamFullPowerPoint`` effect, 12/13 adjust ordinary stamina cost,
    14/15 adjust penetrating stamina cost, and type 49 adds to the Full
    Power point cost. They remain bound to the GUID. Every other shape stays
    fail-closed until implemented.
    """

    if not isinstance(runtime, LocalSaveExamCardRuntimeState):
        raise TypeError("runtime must be LocalSaveExamCardRuntimeState")
    raw_ids = runtime.affect_grow_effect_id_list.to_value()
    raw_effects = runtime.grow_effect_exam_start_after_list.to_value()
    if not isinstance(raw_ids, list) or not all(
        isinstance(value, str) and value for value in raw_ids
    ):
        raise Plan3NativeStateError("invalid-runtime-grow-effect-ids")
    if not isinstance(raw_effects, list):
        raise Plan3NativeStateError("invalid-runtime-grow-effect-list")

    resolved = tuple(
        _grow_effect_from_master(
            effect_id,
            master_dir=Path(master_dir),
            allowed_types=frozenset(
                {
                    _GROW_TYPE_LESSON_ADD,
                    _GROW_TYPE_LESSON_COUNT_ADD,
                    _GROW_TYPE_BLOCK_ADD,
                    _GROW_TYPE_FULL_POWER_POINT_ADD,
                    _GROW_TYPE_COST_REDUCE,
                    _GROW_TYPE_COST_ADD,
                    _GROW_TYPE_COST_PENETRATE_REDUCE,
                    _GROW_TYPE_COST_PENETRATE_ADD,
                    _GROW_TYPE_COST_FULL_POWER_POINT_ADD,
                }
            ),
        )
        for effect_id in raw_ids
    )
    lesson_add = sum(
        effect.value
        for effect in resolved
        if effect.effect_type == _GROW_TYPE_LESSON_ADD
    )
    full_power_point_cost_add = sum(
        effect.value
        for effect in resolved
        if effect.effect_type == _GROW_TYPE_COST_FULL_POWER_POINT_ADD
    )
    lesson_count_add = sum(
        effect.value
        for effect in resolved
        if effect.effect_type == _GROW_TYPE_LESSON_COUNT_ADD
    )
    block_add = sum(
        effect.value
        for effect in resolved
        if effect.effect_type == _GROW_TYPE_BLOCK_ADD
    )
    full_power_point_add_ids = tuple(
        effect.id
        for effect in resolved
        if effect.effect_type == _GROW_TYPE_FULL_POWER_POINT_ADD
    )
    full_power_point_add = sum(
        effect.value
        for effect in resolved
        if effect.effect_type == _GROW_TYPE_FULL_POWER_POINT_ADD
    )
    cost_reduce = sum(
        effect.value
        for effect in resolved
        if effect.effect_type == _GROW_TYPE_COST_REDUCE
    )
    cost_add = sum(
        effect.value
        for effect in resolved
        if effect.effect_type == _GROW_TYPE_COST_ADD
    )
    cost_penetrate_reduce = sum(
        effect.value
        for effect in resolved
        if effect.effect_type == _GROW_TYPE_COST_PENETRATE_REDUCE
    )
    cost_penetrate_add = sum(
        effect.value
        for effect in resolved
        if effect.effect_type == _GROW_TYPE_COST_PENETRATE_ADD
    )
    materialized: list[tuple[str, str, int]] = []
    materialized_type_names = {
        _GROW_EFFECT_LESSON_ADD: _GROW_TYPE_LESSON_ADD,
        _GROW_EFFECT_LESSON_COUNT_ADD: _GROW_TYPE_LESSON_COUNT_ADD,
        _GROW_EFFECT_BLOCK_ADD: _GROW_TYPE_BLOCK_ADD,
        _GROW_EFFECT_FULL_POWER_POINT_ADD: _GROW_TYPE_FULL_POWER_POINT_ADD,
        _GROW_EFFECT_COST_REDUCE: _GROW_TYPE_COST_REDUCE,
        _GROW_EFFECT_COST_ADD: _GROW_TYPE_COST_ADD,
        _GROW_EFFECT_COST_PENETRATE_REDUCE: (
            _GROW_TYPE_COST_PENETRATE_REDUCE
        ),
        _GROW_EFFECT_COST_PENETRATE_ADD: _GROW_TYPE_COST_PENETRATE_ADD,
        _GROW_EFFECT_COST_FULL_POWER_POINT_ADD: (
            _GROW_TYPE_COST_FULL_POWER_POINT_ADD
        ),
    }
    for index, raw in enumerate(raw_effects):
        if not isinstance(raw, Mapping) or set(raw) != _RUNTIME_GROW_EFFECT_FIELDS:
            raise Plan3NativeStateError(
                "unsupported-runtime-grow-effect-shape", str(index)
            )
        effect_id = raw.get("_id")
        effect_type = raw.get("_effectType")
        value = raw.get("_value")
        if not isinstance(effect_id, str) or not effect_id:
            raise Plan3NativeStateError("invalid-runtime-grow-effect-id", str(index))
        effect_type_name = materialized_type_names.get(effect_type)
        if effect_type_name is None:
            raise Plan3NativeStateError(
                "unsupported-runtime-grow-effect-type", f"{effect_id}:{effect_type!r}"
            )
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise Plan3NativeStateError(
                "invalid-runtime-grow-effect-value", effect_id
            )
        if (
            raw.get("_playProduceExamTriggerId") != ""
            or raw.get("_playEffectProduceExamTriggerId") != ""
            or raw.get("_targetPlayEffectProduceExamTriggerIdList") != []
            or raw.get("_playProduceExamEffectId") != ""
            or raw.get("_targetPlayProduceExamEffectIdList") != []
            or raw.get("_produceCardStatusEnchantId") != ""
            or raw.get("_playMovePositionType") != 0
        ):
            raise Plan3NativeStateError(
                "conditional-runtime-grow-effect-unsupported", effect_id
            )
        materialized.append((effect_id, effect_type_name, value))

    # Native AddGrowEffect groups contributions by grow type.  Its retained
    # row keeps the first materialized ID while `_affectGrowEffectIdList`
    # remains the authoritative contribution lineage (including repeats).
    # Consequently +4 then +15 is one LessonAdd row, repeated LessonCountAdd
    # contributions are one typed row with their sum, while a simultaneous
    # CostFullPowerPointAdd contribution is retained as a second typed row.
    if raw_ids:
        resolved_types = tuple(dict.fromkeys(effect.effect_type for effect in resolved))
        if len(materialized) != len(resolved_types):
            raise Plan3NativeStateError(
                "runtime-grow-effect-materialization-mismatch",
                f"lineage={tuple(raw_ids)!r}:rows={tuple(materialized)!r}",
            )
        for effect_type_name in resolved_types:
            lineage = tuple(
                effect for effect in resolved if effect.effect_type == effect_type_name
            )
            rows = tuple(
                row for row in materialized if row[1] == effect_type_name
            )
            if len(rows) != 1:
                raise Plan3NativeStateError(
                    "runtime-grow-effect-materialization-mismatch",
                    f"type={effect_type_name}:rows={rows!r}",
                )
            materialized_id, _row_type, materialized_value = rows[0]
            if (
                materialized_id not in {effect.id for effect in lineage}
                or materialized_value != sum(effect.value for effect in lineage)
            ):
                raise Plan3NativeStateError(
                    "runtime-grow-effect-lineage-mismatch",
                    f"affect={tuple(raw_ids)!r}:materialized={tuple(materialized)!r}",
                )
    elif materialized:
        raise Plan3NativeStateError(
            "runtime-grow-effect-lineage-mismatch",
            f"affect=():materialized={tuple(materialized)!r}",
        )
    return (
        tuple(raw_ids),
        lesson_add,
        full_power_point_cost_add,
        lesson_count_add,
        block_add,
        cost_reduce,
        cost_add,
        cost_penetrate_reduce,
        cost_penetrate_add,
        full_power_point_add,
        full_power_point_add_ids,
    )


def parse_plan3_runtime_lesson_add(
    runtime: LocalSaveExamCardRuntimeState,
    *,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> tuple[tuple[str, ...], int, int]:
    """Parse persisted LessonAdd and Full-Power-cost growth.

    The historical three-value contract is retained for callers that only
    consume those two projections. LessonCountAdd is validated as part of the
    same typed LocalSave lineage and is projected by ``Plan3NativeCard``.
    """

    (
        grow_ids,
        lesson_add,
        full_power_point_cost_add,
        _lesson_count_add,
        _block_add,
        _cost_reduce,
        _cost_add,
        _cost_penetrate_reduce,
        _cost_penetrate_add,
        _full_power_point_add,
        _full_power_point_add_ids,
    ) = _parse_plan3_runtime_grow(runtime, master_dir=master_dir)
    return grow_ids, lesson_add, full_power_point_cost_add


def _parse_plan3_runtime_customization_effects(
    card_id: str,
    upgrade: int,
    runtime: LocalSaveExamCardRuntimeState,
    *,
    allowed_types: frozenset[str],
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> tuple[tuple[str, ...], tuple[Plan3NativeGrowEffect, ...]]:
    """Resolve an exact customization lineage into allowed grow effects."""

    raw_counts = runtime.customize_count_list.to_value()
    if not isinstance(raw_counts, list) or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in raw_counts
    ):
        raise Plan3NativeStateError("invalid-card-customize-count-list", card_id)
    if not any(raw_counts):
        return (), ()
    master = _card_runtime_master(card_id, upgrade, Path(database))
    if len(raw_counts) != len(master.customize_ids):
        # Cards with no customization slots serialize an empty list.  A
        # different length cannot be aligned to the ordered Master IDs.
        raise Plan3NativeStateError(
            "card-customize-lineage-mismatch",
            f"{card_id}+{upgrade}:counts={len(raw_counts)}:ids={len(master.customize_ids)}",
        )
    _grow_index, _status_index, _trigger_index, customize_index = (
        _runtime_master_indexes(Path(master_dir))
    )
    applied_ids: list[str] = []
    effects: list[Plan3NativeGrowEffect] = []
    for customize_id, count in zip(master.customize_ids, raw_counts, strict=True):
        if count == 0:
            continue
        row = customize_index.get(f"{customize_id}\0{count}")
        if row is None:
            raise Plan3NativeStateError(
                "card-customize-master-missing", customize_id
            )
        expected_count = row.get("customizeCount")
        if count != expected_count:
            raise Plan3NativeStateError(
                "card-customize-count-unsupported",
                f"{customize_id}:runtime={count}:master={expected_count!r}",
            )
        if row.get("overwriteProduceCardGrowEffectType") not in (
            "ProduceCardGrowEffectType_Unknown",
            "",
        ):
            raise Plan3NativeStateError(
                "card-customize-overwrite-unsupported", customize_id
            )
        grow_ids = row.get("produceCardGrowEffectIds")
        if not isinstance(grow_ids, list) or not grow_ids or any(
            not isinstance(value, str) or not value for value in grow_ids
        ):
            raise Plan3NativeStateError(
                "card-customize-grow-list-invalid", customize_id
            )
        applied_ids.append(customize_id)
        for effect_id in grow_ids:
            effects.append(
                _grow_effect_from_master(
                    effect_id,
                    master_dir=Path(master_dir),
                    allowed_types=allowed_types,
                )
            )
    return tuple(applied_ids), tuple(effects)


_RUNTIME_CUSTOMIZATION_GROW_TYPES = frozenset(
    {
        _GROW_TYPE_LESSON_ADD,
        _GROW_TYPE_LESSON_COUNT_ADD,
        _GROW_TYPE_FULL_POWER_POINT_ADD,
        _GROW_TYPE_REVIEW_ADD,
        _GROW_TYPE_LESSON_DEPEND_REVIEW_ADD,
        _GROW_TYPE_EFFECT_ADD,
        # InitialAdd is a card-definition marker.  It has no Playing-time
        # operation, but it is still a known grow row and must not be
        # mistaken for an unknown/corrupt runtime lineage (JSNA carries it).
        _GROW_TYPE_INITIAL_ADD,
        _GROW_TYPE_AGGRESSIVE_ADD,
    }
)

# Plan 2's ordered Playing customization compiler intentionally keeps its
# historical surface above.  Plan 3 additionally owns these two neutral
# scalar families end-to-end: native-state hydration retains their ordered
# Customize provenance, and the Plan 3 card materializer applies them in the
# customization layer before persisted runtime grows.
_PLAN3_NATIVE_CUSTOMIZATION_GROW_TYPES = frozenset(
    {
        *_RUNTIME_CUSTOMIZATION_GROW_TYPES,
        _GROW_TYPE_BLOCK_ADD,
        _GROW_TYPE_COST_REDUCE,
        _GROW_TYPE_COST_PENETRATE_REDUCE,
        _GROW_TYPE_STAMINA_DOWN_TURN_ADD,
    }
)

# Plan 2 shares the ordered runtime customization contract.  Unlike the
# historical default parser, its native payment boundary now owns CostReduce
# as well as its existing Playing-time scalar/effect families.
PLAN2_NATIVE_CUSTOMIZATION_GROW_TYPES = frozenset(
    {
        *_RUNTIME_CUSTOMIZATION_GROW_TYPES,
        _GROW_TYPE_BLOCK_ADD,
        _GROW_TYPE_COST_REDUCE,
    }
)


def _runtime_customization_list(
    row: Mapping[str, object],
    key: str,
    *,
    default: object = (),
    effect_id: str,
) -> tuple[object, ...]:
    value = row.get(key, default)
    if not isinstance(value, list):
        raise Plan3NativeStateError(
            "invalid-runtime-customization-master-field",
            f"{effect_id}:{key}",
        )
    return tuple(value)


def _runtime_customization_added_effect(
    grow_effect_id: str,
    row: Mapping[str, object],
    *,
    database: Path,
) -> Plan3Effect:
    """Resolve one ``EffectAdd`` grow row without flattening its child."""

    # The current Master uses ``playProduceExamEffectId`` for a direct child
    # appended to the card's Playing effect sequence.  Trigger-targeted,
    # status-enchant, move, and effect-group rewrites are separate native
    # paths and stay fail-closed until they have a typed executor.
    if row.get("costType", "ExamCostType_Unknown") != "ExamCostType_Unknown":
        raise Plan3NativeStateError(
            "conditional-runtime-customization-effect-unsupported",
            grow_effect_id,
        )
    for key in (
        "playProduceExamTriggerId",
        "playEffectProduceExamTriggerId",
        "produceCardStatusEnchantId",
        "playMovePositionType",
    ):
        expected = (
            "ProduceCardMovePositionType_Unknown"
            if key == "playMovePositionType"
            else ""
        )
        if row.get(key, expected) != expected:
            raise Plan3NativeStateError(
                "conditional-runtime-customization-effect-unsupported",
                f"{grow_effect_id}:{key}",
            )
    for key in (
        "targetPlayEffectProduceExamTriggerIdList",
        "targetPlayProduceExamEffectIds",
    ):
        if _runtime_customization_list(
            row,
            key,
            default=[],
            effect_id=grow_effect_id,
        ):
            raise Plan3NativeStateError(
                "conditional-runtime-customization-effect-unsupported",
                f"{grow_effect_id}:{key}",
            )
    if row.get("value", 0) != 0:
        raise Plan3NativeStateError(
            "invalid-runtime-customization-effect-value", grow_effect_id
        )
    effect_id = row.get("playProduceExamEffectId")
    if not isinstance(effect_id, str) or not effect_id:
        raise Plan3NativeStateError(
            "runtime-customization-added-effect-missing", grow_effect_id
        )
    try:
        effect = load_plan3_effect(effect_id, Path(database))
    except (KeyError, sqlite3.Error, TypeError, ValueError) as error:
        raise Plan3NativeStateError(
            "runtime-customization-added-effect-unavailable",
            f"{grow_effect_id}:{effect_id}:{type(error).__name__}",
        ) from error
    if effect.id != effect_id:
        raise Plan3NativeStateError(
            "runtime-customization-added-effect-lineage-mismatch",
            f"{grow_effect_id}:{effect_id}",
        )
    return effect


def _runtime_customization_entry(
    slot_index: int,
    customize_id: str,
    customize_count: int,
    grow_effect_id: str,
    *,
    database: Path,
    master_dir: Path,
    allowed_types: frozenset[str],
) -> Plan3NativeRuntimeCustomizationEffect:
    grow_index, _status_index, _trigger_index, _customize_index = (
        _runtime_master_indexes(Path(master_dir))
    )
    grow_row = grow_index.get(grow_effect_id)
    if grow_row is None:
        raise Plan3NativeStateError(
            "runtime-grow-effect-master-missing", grow_effect_id
        )
    grow_type = grow_row.get("effectType")
    if not isinstance(grow_type, str) or not grow_type:
        raise Plan3NativeStateError(
            "invalid-runtime-grow-effect-type", grow_effect_id
        )
    value = grow_row.get("value")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise Plan3NativeStateError(
            "invalid-runtime-grow-effect-value", grow_effect_id
        )
    if grow_type not in allowed_types:
        raise Plan3NativeStateError(
            "unsupported-runtime-customization-grow-type",
            f"{grow_effect_id}:{grow_type!r}",
        )
    added_effect = None
    if grow_type == _GROW_TYPE_EFFECT_ADD:
        added_effect = _runtime_customization_added_effect(
            grow_effect_id,
            grow_row,
            database=Path(database),
        )
    else:
        # Reuse the existing strict neutral-field parser for scalar grows;
        # this keeps conditional grow rows fail-closed consistently with the
        # Plan3 runtime projection.
        _grow_effect_from_master(
            grow_effect_id,
            master_dir=Path(master_dir),
            allowed_types=frozenset({grow_type}),
        )
    return Plan3NativeRuntimeCustomizationEffect(
        slot_index=slot_index,
        customize_id=customize_id,
        customize_count=customize_count,
        grow_effect_id=grow_effect_id,
        grow_effect_type=grow_type,
        value=value,
        added_effect=added_effect,
    )


def parse_plan3_runtime_customization_ordered(
    card_id: str,
    upgrade: int,
    runtime: LocalSaveExamCardRuntimeState,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
    allowed_types: frozenset[str] = _RUNTIME_CUSTOMIZATION_GROW_TYPES,
) -> Plan3NativeRuntimeCustomization:
    """Compile active CardCustomize rows into an ordered typed contract.

    The legacy tuple parsers intentionally expose only aggregate Lesson
    values.  That shape cannot represent ``EffectAdd`` (for example the
    target card's added LessonDependExamReview effect), so Playing uses this
    richer contract.  The contract retains the Customize slot and source
    grow IDs, resolves direct added effects from the exact effect Master row,
    and rejects every unsupported/conditional grow instead of silently
    dropping it.
    """

    if not isinstance(runtime, LocalSaveExamCardRuntimeState):
        raise TypeError("runtime must be LocalSaveExamCardRuntimeState")
    raw_counts = runtime.customize_count_list.to_value()
    if not isinstance(raw_counts, list) or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in raw_counts
    ):
        raise Plan3NativeStateError("invalid-card-customize-count-list", card_id)
    if not any(raw_counts):
        return Plan3NativeRuntimeCustomization((), ())
    master = _card_runtime_master(card_id, upgrade, Path(database))
    if len(raw_counts) != len(master.customize_ids):
        raise Plan3NativeStateError(
            "card-customize-lineage-mismatch",
            f"{card_id}+{upgrade}:counts={len(raw_counts)}:ids={len(master.customize_ids)}",
        )
    _grow_index, _status_index, _trigger_index, customize_index = (
        _runtime_master_indexes(Path(master_dir))
    )
    applied_ids: list[str] = []
    entries: list[Plan3NativeRuntimeCustomizationEffect] = []
    for slot_index, (customize_id, count) in enumerate(
        zip(master.customize_ids, raw_counts, strict=True)
    ):
        if count == 0:
            continue
        row = customize_index.get(f"{customize_id}\0{count}")
        if row is None:
            raise Plan3NativeStateError(
                "card-customize-master-missing", customize_id
            )
        expected_count = row.get("customizeCount")
        if count != expected_count:
            raise Plan3NativeStateError(
                "card-customize-count-unsupported",
                f"{customize_id}:runtime={count}:master={expected_count!r}",
            )
        overwrite = row.get(
            "overwriteProduceCardGrowEffectType",
            "ProduceCardGrowEffectType_Unknown",
        )
        if not isinstance(overwrite, str) or not overwrite:
            raise Plan3NativeStateError(
                "card-customize-overwrite-invalid", customize_id
            )
        grow_ids = row.get("produceCardGrowEffectIds")
        if not isinstance(grow_ids, list) or not grow_ids or any(
            not isinstance(value, str) or not value for value in grow_ids
        ):
            raise Plan3NativeStateError(
                "card-customize-grow-list-invalid", customize_id
            )
        applied_ids.append(customize_id)
        for grow_effect_id in grow_ids:
            entries.append(
                _runtime_customization_entry(
                    slot_index,
                    customize_id,
                    count,
                    grow_effect_id,
                    database=Path(database),
                    master_dir=Path(master_dir),
                    allowed_types=allowed_types,
                )
            )
    return Plan3NativeRuntimeCustomization(
        tuple(applied_ids),
        tuple(entries),
    )


def parse_plan3_native_runtime_customization(
    card_id: str,
    upgrade: int,
    runtime: LocalSaveExamCardRuntimeState,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> Plan3NativeRuntimeCustomization:
    """Compile the exact Plan 3 customization layer for one card instance.

    CostReduce, CostPenetrateReduce and BlockAdd are admitted only here, where native
    applicability and execution semantics already have typed owners.  The
    shared ordered parser's default remains unchanged for Plan 2 callers.
    """

    return parse_plan3_runtime_customization_ordered(
        card_id,
        upgrade,
        runtime,
        database=database,
        master_dir=master_dir,
        allowed_types=_PLAN3_NATIVE_CUSTOMIZATION_GROW_TYPES,
    )


def parse_plan2_native_runtime_customization(
    card_id: str,
    upgrade: int,
    runtime: LocalSaveExamCardRuntimeState,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> Plan3NativeRuntimeCustomization:
    """Compile the customization families owned by Plan 2 end to end."""

    return parse_plan3_runtime_customization_ordered(
        card_id,
        upgrade,
        runtime,
        database=database,
        master_dir=master_dir,
        allowed_types=PLAN2_NATIVE_CUSTOMIZATION_GROW_TYPES,
    )


def parse_plan3_runtime_customization(
    card_id: str,
    upgrade: int,
    runtime: LocalSaveExamCardRuntimeState,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> tuple[tuple[str, ...], int, int]:
    """Resolve LessonAdd/CountAdd customization through exact Master rows."""

    applied_ids, effects = _parse_plan3_runtime_customization_effects(
        card_id,
        upgrade,
        runtime,
        allowed_types=frozenset(
            {_GROW_TYPE_LESSON_ADD, _GROW_TYPE_LESSON_COUNT_ADD}
        ),
        database=database,
        master_dir=master_dir,
    )
    return (
        applied_ids,
        sum(
            effect.value
            for effect in effects
            if effect.effect_type == _GROW_TYPE_LESSON_ADD
        ),
        sum(
            effect.value
            for effect in effects
            if effect.effect_type == _GROW_TYPE_LESSON_COUNT_ADD
        ),
    )


def parse_plan3_runtime_customization_with_full_power(
    card_id: str,
    upgrade: int,
    runtime: LocalSaveExamCardRuntimeState,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> tuple[tuple[str, ...], int, int, Plan3NativeFullPowerPointAdd]:
    """Resolve scalar CardCustomize grows including FullPowerPointAdd.

    The historical :func:`parse_plan3_runtime_customization` contract is
    intentionally unchanged because Plan2 callers use its fail-closed
    Lesson-only boundary.  Plan3 card hydration needs the additional native
    ``FullPowerPointAdd`` family, so this sibling returns its typed lineage
    without weakening that older boundary.
    """

    ordered = parse_plan3_native_runtime_customization(
        card_id,
        upgrade,
        runtime,
        database=database,
        master_dir=master_dir,
    )
    # Do not send a typed definition rewrite through the historical scalar
    # tuple parser: it cannot represent EffectAdd/InitialAdd. The ordered
    # parser resolves the exact selected Customize-count row and its children.
    # Plan2's legacy aggregate API above remains unchanged.
    supported={_GROW_TYPE_LESSON_ADD,_GROW_TYPE_LESSON_COUNT_ADD,_GROW_TYPE_FULL_POWER_POINT_ADD,
        _GROW_TYPE_BLOCK_ADD,_GROW_TYPE_COST_REDUCE,_GROW_TYPE_COST_PENETRATE_REDUCE,
        _GROW_TYPE_STAMINA_DOWN_TURN_ADD,_GROW_TYPE_EFFECT_ADD,_GROW_TYPE_INITIAL_ADD}
    for entry in ordered.effects:
        if entry.grow_effect_type not in supported:
            raise Plan3NativeStateError('unsupported-plan3-customization-grow-type',entry.grow_effect_type)
    full_power_entries = tuple(
        entry
        for entry in ordered.effects
        if entry.grow_effect_type == _GROW_TYPE_FULL_POWER_POINT_ADD
    )
    full_power = Plan3NativeFullPowerPointAdd(
        value=sum(entry.value for entry in full_power_entries),
        grow_effect_ids=tuple(
            entry.grow_effect_id for entry in full_power_entries
        ),
        customize_ids=tuple(
            dict.fromkeys(entry.customize_id for entry in full_power_entries)
        ),
    )
    return (
        ordered.customize_ids,
        ordered.lesson_add,
        ordered.lesson_count_add,
        full_power,
    )


def parse_plan3_runtime_lesson_depend_review_customization(
    card_id: str,
    upgrade: int,
    runtime: LocalSaveExamCardRuntimeState,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> tuple[tuple[str, ...], int]:
    """Resolve the native LessonDependExamReview customization delta.

    LessonAdd and LessonCountAdd are accepted as already-supported sibling
    customization branches so a card with more than one active slot can be
    inspected without discarding its ordered Master lineage.  Only the exact
    LessonDependExamReviewAdd contribution is returned to the Plan2 playing
    executor; every other grow type remains fail-closed.
    """

    applied_ids, effects = _parse_plan3_runtime_customization_effects(
        card_id,
        upgrade,
        runtime,
        allowed_types=frozenset(
            {
                _GROW_TYPE_LESSON_ADD,
                _GROW_TYPE_LESSON_COUNT_ADD,
                _GROW_TYPE_LESSON_DEPEND_REVIEW_ADD,
            }
        ),
        database=database,
        master_dir=master_dir,
    )
    return (
        applied_ids,
        sum(
            effect.value
            for effect in effects
            if effect.effect_type == _GROW_TYPE_LESSON_DEPEND_REVIEW_ADD
        ),
    )


def _is_neutral_card_grow_trigger(
    row: Mapping[str, object], *, search_id: str = "", lower_count: int = 0
) -> bool:
    return (
        row.get("fieldStatusCheckTypes") == []
        and row.get("fieldStatusTypes") == []
        and row.get("fieldStatusValues") == []
        and row.get("fieldStatusProduceCardSearchIds") == []
        and row.get("produceCardSearchId") == search_id
        and row.get("upperSearchCount") == 0
        and row.get("lowerSearchCount") == lower_count
        and row.get("cardMovePositionType")
        == "ProduceCardMovePositionType_Unknown"
        and row.get("effectTypes") == []
        and row.get("lessonType") == "ProduceStepLessonType_Unknown"
    )


def _assert_target_concentration_search(
    *, database: Path
) -> tuple[str, ...]:
    try:
        rule = load_produce_card_search(
            _SEARCH_TARGET_CONCENTRATION_GROUP, Path(database)
        )
    except (KeyError, OSError, ValueError) as error:
        raise Plan3NativeStateError(
            "card-grow-trigger-search-master-unavailable",
            _SEARCH_TARGET_CONCENTRATION_GROUP,
        ) from error
    actual = (
        rule.card_rarities,
        rule.produce_card_ids,
        rule.upgrade_counts,
        rule.plan_type,
        rule.card_categories,
        rule.card_status_type,
        rule.order_type,
        rule.card_position_type,
        rule.card_search_tag,
        rule.produce_card_random_pool_id,
        rule.limit_count,
        rule.stamina_min_max_type,
        rule.stamina_min,
        rule.stamina_max,
        rule.exam_effect_type,
        rule.effect_group_ids,
        rule.is_self,
        rule.produce_card_pool_id,
        rule.cost_type,
        rule.is_customized,
    )
    expected = (
        (),
        (),
        (),
        "ProducePlanType_Unknown",
        (),
        "ProduceCardSearchStatusType_Unknown",
        "ProduceCardOrderType_Unknown",
        "ProduceCardPositionType_Target",
        "",
        "",
        0,
        "ConditionMinMaxType_Unknown",
        0,
        0,
        "ProduceExamEffectType_Unknown",
        (_CONCENTRATION_EFFECT_GROUP,),
        False,
        "",
        "ExamCostType_Unknown",
        False,
    )
    if actual != expected:
        raise Plan3NativeStateError(
            "unsupported-card-grow-trigger-search",
            _SEARCH_TARGET_CONCENTRATION_GROUP,
        )
    return rule.effect_group_ids


def parse_plan3_runtime_card_grow_status(
    card_id: str,
    upgrade: int,
    runtime: LocalSaveExamCardRuntimeState,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> Plan3NativeCardGrowStatus | None:
    """Validate the live card listener against Card/Status/Trigger Master."""

    raw = runtime.status_effect.to_value()
    if not isinstance(raw, Mapping) or set(raw) != _STATUS_EFFECT_FIELDS:
        raise Plan3NativeStateError("invalid-card-grow-status-shape", card_id)
    if raw == _EMPTY_STATUS_EFFECT:
        return None
    card_master = _card_runtime_master(card_id, upgrade, Path(database))
    status_id = raw.get("_id")
    if not isinstance(status_id, str) or status_id != card_master.status_id:
        raise Plan3NativeStateError(
            "card-grow-status-lineage-mismatch",
            f"{card_id}+{upgrade}:runtime={status_id!r}:master={card_master.status_id!r}",
        )
    _grow_index, status_index, trigger_index, _customize_index = (
        _runtime_master_indexes(Path(master_dir))
    )
    status_row = status_index.get(status_id)
    if status_row is None:
        raise Plan3NativeStateError("card-grow-status-master-missing", status_id)
    trigger_id = raw.get("_produceExamTriggerId")
    grow_ids = raw.get("_produceCardGrowEffectIdList")
    trigger_count = raw.get("_triggerCount")
    if (
        trigger_id != status_row.get("produceExamTriggerId")
        or grow_ids != status_row.get("produceCardGrowEffectIds")
        or trigger_count != status_row.get("triggerCount")
        or raw.get("_effectGroupIdList") != status_row.get("effectGroupIds", [])
    ):
        raise Plan3NativeStateError("card-grow-status-master-mismatch", status_id)
    if not isinstance(trigger_id, str) or not isinstance(grow_ids, list):
        raise Plan3NativeStateError("card-grow-status-master-invalid", status_id)
    trigger_row = trigger_index.get(trigger_id)
    if trigger_row is None:
        raise Plan3NativeStateError("card-grow-trigger-master-missing", trigger_id)
    phase_types = trigger_row.get("phaseTypes")
    phase_values = trigger_row.get("phaseValues")
    target_effect_group_ids: tuple[str, ...] = ()
    search_id = ""
    if (
        trigger_id == "e_trigger-exam_stance_change_full_power"
        and phase_types == [_PHASE_STANCE_CHANGE_FULL_POWER]
        and phase_values == []
        and _is_neutral_card_grow_trigger(trigger_row)
    ):
        trigger_kind = "full_power"
        interval = 0
    elif (
        trigger_id
        in {
            "e_trigger-exam_stance_change_count_interval-1",
            "e_trigger-exam_stance_change_count_interval-2",
            "e_trigger-exam_stance_change_count_interval-3",
        }
        and phase_types == [_PHASE_STANCE_CHANGE_COUNT_INTERVAL]
        and phase_values == [int(trigger_id.rsplit("-", 1)[1])]
        and _is_neutral_card_grow_trigger(trigger_row)
    ):
        trigger_kind = "stance_change_interval"
        interval = phase_values[0]
    elif (
        trigger_id == "e_trigger-exam_stance_change_concentration"
        and phase_types == [_PHASE_STANCE_CHANGE_CONCENTRATION]
        and phase_values == []
        and _is_neutral_card_grow_trigger(trigger_row)
    ):
        trigger_kind = "stance_change_concentration"
        interval = 0
    elif (
        trigger_id == "e_trigger-exam_stance_change_preservation"
        and phase_types == [_PHASE_STANCE_CHANGE_PRESERVATION]
        and phase_values == []
        and _is_neutral_card_grow_trigger(trigger_row)
    ):
        trigger_kind = "stance_change_preservation"
        interval = 0
    elif (
        trigger_id
        == (
            "e_trigger-exam_card_play_after-full_power_up-"
            "p_card_search-target_is_self-0_1"
        )
        and phase_types == [_PHASE_CARD_PLAY_AFTER]
        and phase_values == []
        and trigger_row.get("fieldStatusCheckTypes") == []
        and trigger_row.get("fieldStatusTypes") == [_FIELD_FULL_POWER_UP]
        and trigger_row.get("fieldStatusValues") == []
        and trigger_row.get("fieldStatusProduceCardSearchIds") == []
        and trigger_row.get("produceCardSearchId") == _SEARCH_TARGET_IS_SELF
        and trigger_row.get("upperSearchCount") == 0
        and trigger_row.get("lowerSearchCount") == 1
        and trigger_row.get("cardMovePositionType")
        == "ProduceCardMovePositionType_Unknown"
        and trigger_row.get("effectTypes") == []
        and trigger_row.get("lessonType") == "ProduceStepLessonType_Unknown"
    ):
        trigger_kind = "card_play_after_self_full_power"
        interval = 0
        search_id = _SEARCH_TARGET_IS_SELF
    elif (
        trigger_id
        == (
            "e_trigger-exam_card_play_after-p_card_search-target-"
            "effect_group-visible-exam_concentration-000-0_1"
        )
        and phase_types == [_PHASE_CARD_PLAY_AFTER]
        and phase_values == []
        and _is_neutral_card_grow_trigger(
            trigger_row,
            search_id=_SEARCH_TARGET_CONCENTRATION_GROUP,
            lower_count=1,
        )
    ):
        trigger_kind = "card_play_after_effect_group"
        interval = 0
        search_id = _SEARCH_TARGET_CONCENTRATION_GROUP
        target_effect_group_ids = _assert_target_concentration_search(
            database=Path(database)
        )
    else:
        raise Plan3NativeStateError(
            "unsupported-card-grow-trigger", trigger_id
        )
    phase_dictionary = raw.get("_phaseCountDictionary")
    if not isinstance(phase_dictionary, Mapping) or set(phase_dictionary) != {"_list"}:
        raise Plan3NativeStateError("invalid-card-grow-phase-counts", status_id)
    phase_list = phase_dictionary.get("_list")
    if not isinstance(phase_list, list):
        raise Plan3NativeStateError("invalid-card-grow-phase-counts", status_id)
    phase_counts: list[tuple[int, int]] = []
    for entry in phase_list:
        if (
            not isinstance(entry, Mapping)
            or set(entry) != {"key", "value"}
            or not isinstance(entry.get("key"), int)
            or isinstance(entry.get("key"), bool)
            or not isinstance(entry.get("value"), Mapping)
            or set(entry["value"]) != {"current"}
            or not isinstance(entry["value"].get("current"), int)
            or isinstance(entry["value"].get("current"), bool)
            or entry["value"]["current"] < 0
        ):
            raise Plan3NativeStateError("invalid-card-grow-phase-counts", status_id)
        phase_counts.append((entry["key"], entry["value"]["current"]))
    grow_effects = tuple(
        _grow_effect_from_master(
            effect_id,
            master_dir=Path(master_dir),
            allowed_types=frozenset(
                {
                    _GROW_TYPE_LESSON_ADD,
                    _GROW_TYPE_LESSON_COUNT_ADD,
                    _GROW_TYPE_BLOCK_ADD,
                    _GROW_TYPE_COST_ADD,
                    _GROW_TYPE_COST_PENETRATE_REDUCE,
                    _GROW_TYPE_COST_FULL_POWER_POINT_ADD,
                }
            ),
        )
        for effect_id in grow_ids
    )
    return Plan3NativeCardGrowStatus(
        id=status_id,
        trigger_id=trigger_id,
        trigger_kind=trigger_kind,
        interval=interval,
        grow_effects=grow_effects,
        trigger_count=trigger_count,
        spend_count=raw.get("_spendCount"),
        spend_turn=raw.get("_spendTurn"),
        phase_counts=tuple(sorted(phase_counts)),
        search_id=search_id,
        target_effect_group_ids=target_effect_group_ids,
    )


@dataclass(frozen=True, order=True, slots=True)
class Plan3NativeCard:
    """One exact card instance across Plan 3's five ordinary zones."""

    guid: str
    card_id: str
    base_upgrade: int
    temporary_upgrade: int
    effective_upgrade: int
    support_upgrade_ids: tuple[str, ...] = ()
    fixed_deck_order: int = 0
    play_count: int = 0
    runtime_grow_effect_ids: tuple[str, ...] = ()
    runtime_lesson_add: int = 0
    # ``FullPowerPointAdd`` changes an ExamFullPowerPoint effect value.  It
    # is intentionally independent from ``runtime_full_power_point_cost_add``
    # (which changes the card's Full Power cost).
    runtime_full_power_point_add: int = 0
    runtime_full_power_point_add_aggregate: Plan3NativeFullPowerPointAdd | None = None
    runtime_full_power_point_cost_add: int = 0
    runtime_customize_ids: tuple[str, ...] = ()
    runtime_customization: Plan3NativeRuntimeCustomization = (
        Plan3NativeRuntimeCustomization((), ())
    )
    # Plan 2's Encore/created-card projection needs the complete ordered
    # customization identity even for grow families that Plan 3 itself does
    # not execute (ReviewAdd, EffectAdd, AggressiveAdd).  Keep that provenance
    # separate from the Plan 3 materialization field above.
    plan2_runtime_customization: Plan3NativeRuntimeCustomization = (
        Plan3NativeRuntimeCustomization((), ())
    )
    runtime_customize_lesson_add: int = 0
    # Plan2's LessonDependExamReview customization is GUID-local runtime
    # identity too.  It is deliberately kept separate from LessonAdd: the
    # former changes the permille operand of one card effect, while the
    # latter is a flat lesson-value grow layer.
    runtime_customize_lesson_depend_review_add: int = 0
    runtime_lesson_count_add: int = 0
    runtime_grow_status: Plan3NativeCardGrowStatus | None = None
    runtime_block_add: int = 0
    runtime_cost_reduce: int = 0
    runtime_cost_add: int = 0
    runtime_cost_penetrate_reduce: int = 0
    runtime_cost_penetrate_add: int = 0
    move_effect_used_in_turn: bool = False

    def __post_init__(self) -> None:
        _text(self.guid, "guid")
        _text(self.card_id, "card_id")
        _upgrade(self.base_upgrade, "base_upgrade")
        _upgrade(self.temporary_upgrade, "temporary_upgrade")
        _upgrade(self.effective_upgrade, "effective_upgrade")
        supports = tuple(self.support_upgrade_ids)
        if any(not isinstance(value, str) or not value.strip() for value in supports):
            raise Plan3NativeStateError(
                "invalid-support-upgrade-id", self.guid
            )
        if len(set(supports)) != len(supports):
            raise Plan3NativeStateError(
                "duplicate-support-upgrade-id", self.guid
            )
        object.__setattr__(self, "support_upgrade_ids", supports)
        expected = self.base_upgrade + self.temporary_upgrade + len(supports)
        if self.effective_upgrade != expected:
            raise Plan3NativeStateError(
                "upgrade-lineage-mismatch",
                f"{self.guid}:effective={self.effective_upgrade}:expected={expected}",
            )
        if (
            not isinstance(self.fixed_deck_order, int)
            or isinstance(self.fixed_deck_order, bool)
            or not INT32_MIN <= self.fixed_deck_order <= INT32_MAX
        ):
            raise Plan3NativeStateError("invalid-fixed-deck-order", self.guid)
        if (
            not isinstance(self.play_count, int)
            or isinstance(self.play_count, bool)
            or not 0 <= self.play_count <= INT32_MAX
        ):
            raise Plan3NativeStateError("invalid-play-count", self.guid)
        grow_ids = tuple(self.runtime_grow_effect_ids)
        if any(not isinstance(value, str) or not value for value in grow_ids):
            raise Plan3NativeStateError("invalid-runtime-grow-effect-id", self.guid)
        object.__setattr__(self, "runtime_grow_effect_ids", grow_ids)
        if (
            not isinstance(self.runtime_lesson_add, int)
            or isinstance(self.runtime_lesson_add, bool)
            or self.runtime_lesson_add < 0
        ):
            raise Plan3NativeStateError("invalid-runtime-lesson-add", self.guid)
        if (
            not isinstance(self.runtime_full_power_point_add, int)
            or isinstance(self.runtime_full_power_point_add, bool)
            or self.runtime_full_power_point_add < 0
        ):
            raise Plan3NativeStateError(
                "invalid-runtime-full-power-point-add", self.guid
            )
        aggregate = self.runtime_full_power_point_add_aggregate
        if aggregate is None:
            aggregate = Plan3NativeFullPowerPointAdd(
                value=self.runtime_full_power_point_add
            )
        elif not isinstance(aggregate, Plan3NativeFullPowerPointAdd):
            raise TypeError(
                "runtime_full_power_point_add_aggregate must be "
                "Plan3NativeFullPowerPointAdd or None"
            )
        elif self.runtime_full_power_point_add not in {
            0,
            aggregate.value,
        }:
            raise Plan3NativeStateError(
                "runtime-full-power-point-add-aggregate-mismatch", self.guid
            )
        if self.runtime_full_power_point_add == 0:
            object.__setattr__(
                self, "runtime_full_power_point_add", aggregate.value
            )
        object.__setattr__(self, "runtime_full_power_point_add_aggregate", aggregate)
        if (
            not isinstance(self.runtime_full_power_point_cost_add, int)
            or isinstance(self.runtime_full_power_point_cost_add, bool)
            or self.runtime_full_power_point_cost_add < 0
        ):
            raise Plan3NativeStateError(
                "invalid-runtime-full-power-point-cost-add", self.guid
            )
        if grow_ids and not (
            self.runtime_lesson_add
            or self.runtime_full_power_point_add
            or self.runtime_full_power_point_cost_add
            or self.runtime_lesson_count_add
            or self.runtime_block_add
            or self.runtime_cost_reduce
            or self.runtime_cost_add
            or self.runtime_cost_penetrate_reduce
            or self.runtime_cost_penetrate_add
        ):
            raise Plan3NativeStateError(
                "runtime-grow-effect-value-mismatch", self.guid
            )
        if any(
            (
                self.runtime_lesson_add,
                self.runtime_full_power_point_add,
                self.runtime_full_power_point_cost_add,
                self.runtime_lesson_count_add,
                self.runtime_block_add,
                self.runtime_cost_reduce,
                self.runtime_cost_add,
                self.runtime_cost_penetrate_reduce,
                self.runtime_cost_penetrate_add,
            )
        ) and not grow_ids and not (
            (
                self.runtime_lesson_count_add
                or self.runtime_full_power_point_add
            )
            and self.runtime_customize_ids
        ):
            raise Plan3NativeStateError(
                "runtime-grow-effect-value-mismatch", self.guid
            )
        for value, label in (
            (self.runtime_block_add, "runtime_block_add"),
            (self.runtime_cost_reduce, "runtime_cost_reduce"),
            (self.runtime_cost_add, "runtime_cost_add"),
            (
                self.runtime_cost_penetrate_reduce,
                "runtime_cost_penetrate_reduce",
            ),
            (self.runtime_cost_penetrate_add, "runtime_cost_penetrate_add"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise Plan3NativeStateError(
                    "invalid-runtime-grow-effect-value", label
                )
        customize_ids = tuple(self.runtime_customize_ids)
        if any(not isinstance(value, str) or not value for value in customize_ids):
            raise Plan3NativeStateError("invalid-runtime-customize-id", self.guid)
        if len(set(customize_ids)) != len(customize_ids):
            raise Plan3NativeStateError("duplicate-runtime-customize-id", self.guid)
        customization = self.runtime_customization
        if not isinstance(customization, Plan3NativeRuntimeCustomization):
            raise TypeError(
                "runtime_customization must be Plan3NativeRuntimeCustomization"
            )
        if customization.customize_ids:
            if customize_ids and customize_ids != customization.customize_ids:
                raise Plan3NativeStateError(
                    "runtime-customization-id-mismatch", self.guid
                )
            if not customize_ids:
                customize_ids = customization.customize_ids
        if any(
            effect.grow_effect_type
            not in {
                _GROW_TYPE_LESSON_ADD,
                _GROW_TYPE_LESSON_COUNT_ADD,
                _GROW_TYPE_FULL_POWER_POINT_ADD,
                _GROW_TYPE_BLOCK_ADD,
                _GROW_TYPE_COST_REDUCE,
                _GROW_TYPE_COST_PENETRATE_REDUCE,
                _GROW_TYPE_STAMINA_DOWN_TURN_ADD,
                _GROW_TYPE_EFFECT_ADD,
                _GROW_TYPE_INITIAL_ADD,
            }
            for effect in customization.effects
        ):
            raise Plan3NativeStateError(
                "runtime-customization-effect-unbound", self.guid
            )
        plan2_customization = self.plan2_runtime_customization
        if not isinstance(plan2_customization, Plan3NativeRuntimeCustomization):
            raise TypeError(
                "plan2_runtime_customization must be Plan3NativeRuntimeCustomization"
            )
        if plan2_customization.customize_ids:
            if customize_ids and customize_ids != plan2_customization.customize_ids:
                raise Plan3NativeStateError(
                    "plan2-runtime-customization-id-mismatch", self.guid
                )
            if not customize_ids:
                customize_ids = plan2_customization.customize_ids
        object.__setattr__(self, "runtime_customize_ids", customize_ids)
        object.__setattr__(self, "runtime_customization", customization)
        object.__setattr__(self, "plan2_runtime_customization", plan2_customization)
        aggregate_customize_ids = set(
            self.runtime_full_power_point_add_aggregate.customize_ids
        )
        if not aggregate_customize_ids.issubset(customize_ids):
            raise Plan3NativeStateError(
                "runtime-full-power-point-add-customize-lineage-mismatch",
                self.guid,
            )
        for value, label in (
            (self.runtime_customize_lesson_add, "runtime_customize_lesson_add"),
            (
                self.runtime_customize_lesson_depend_review_add,
                "runtime_customize_lesson_depend_review_add",
            ),
            (self.runtime_lesson_count_add, "runtime_lesson_count_add"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise Plan3NativeStateError("invalid-runtime-customize-value", label)
        if customize_ids and not (
            self.runtime_customize_lesson_add
            or self.runtime_customize_lesson_depend_review_add
            or self.runtime_lesson_count_add
            or self.runtime_full_power_point_add
            or customization.effects
            or plan2_customization.effects
        ):
            raise Plan3NativeStateError(
                "runtime-customize-value-mismatch", self.guid
            )
        if (
            self.runtime_customize_lesson_add
            or self.runtime_customize_lesson_depend_review_add
        ) and not customize_ids:
            raise Plan3NativeStateError(
                "runtime-customize-value-mismatch", self.guid
            )
        if customization.effects and (
            customization.lesson_add != self.runtime_customize_lesson_add
            or customization.lesson_count_add > self.runtime_lesson_count_add
            or customization.full_power_point_add
            > self.runtime_full_power_point_add
        ):
            raise Plan3NativeStateError(
                "runtime-customization-value-mismatch", self.guid
            )
        if self.runtime_grow_status is not None and not isinstance(
            self.runtime_grow_status, Plan3NativeCardGrowStatus
        ):
            raise TypeError(
                "runtime_grow_status must be Plan3NativeCardGrowStatus or None"
            )
        if not isinstance(self.move_effect_used_in_turn, bool):
            raise TypeError("move_effect_used_in_turn must be bool")

    @classmethod
    def from_local_save(
        cls,
        card: LocalSaveExamCard,
        *,
        database: Path = DEFAULT_DATABASE,
        master_dir: Path = DEFAULT_MASTER_DIR,
    ) -> "Plan3NativeCard":
        if not isinstance(card, LocalSaveExamCard):
            raise TypeError("card must be LocalSaveExamCard")
        if card.fixed_deck_order is None:
            raise Plan3NativeStateError("unknown-fixed-deck-order", card.guid)
        if card.runtime_state is None:
            raise Plan3NativeStateError("unknown-card-runtime-state", card.guid)
        (
            grow_ids,
            lesson_add,
            full_power_point_cost_add,
            grow_lesson_count_add,
            block_add,
            cost_reduce,
            cost_add,
            cost_penetrate_reduce,
            cost_penetrate_add,
            full_power_point_add,
            full_power_point_add_ids,
        ) = _parse_plan3_runtime_grow(
            card.runtime_state, master_dir=master_dir
        )
        (
            customize_ids,
            customize_lesson_add,
            lesson_count_add,
            customize_full_power_point_add,
        ) = parse_plan3_runtime_customization_with_full_power(
            card.card_id,
            card.effective_upgrade,
            card.runtime_state,
            database=database,
            master_dir=master_dir,
        )
        runtime_customization = parse_plan3_native_runtime_customization(
            card.card_id,
            card.effective_upgrade,
            card.runtime_state,
            database=database,
            master_dir=master_dir,
        )
        grow_status = parse_plan3_runtime_card_grow_status(
            card.card_id,
            card.effective_upgrade,
            card.runtime_state,
            database=database,
            master_dir=master_dir,
        )
        return cls(
            guid=card.guid,
            card_id=card.card_id,
            base_upgrade=card.base_upgrade,
            temporary_upgrade=card.temporary_upgrade,
            effective_upgrade=card.effective_upgrade,
            support_upgrade_ids=card.support_upgrade_ids,
            fixed_deck_order=card.fixed_deck_order,
            play_count=card.runtime_state.play_count,
            runtime_grow_effect_ids=grow_ids,
            runtime_lesson_add=lesson_add,
            runtime_full_power_point_add=(
                full_power_point_add
                + customize_full_power_point_add.value
            ),
            runtime_full_power_point_add_aggregate=(
                Plan3NativeFullPowerPointAdd(
                    value=(
                        full_power_point_add
                        + customize_full_power_point_add.value
                    ),
                    grow_effect_ids=(
                        *full_power_point_add_ids,
                        *customize_full_power_point_add.grow_effect_ids,
                    ),
                    customize_ids=customize_full_power_point_add.customize_ids,
                )
            ),
            runtime_full_power_point_cost_add=full_power_point_cost_add,
            runtime_customize_ids=customize_ids,
            runtime_customization=runtime_customization,
            runtime_customize_lesson_add=customize_lesson_add,
            runtime_lesson_count_add=(
                grow_lesson_count_add + lesson_count_add
            ),
            runtime_grow_status=grow_status,
            runtime_block_add=block_add,
            runtime_cost_reduce=cost_reduce,
            runtime_cost_add=cost_add,
            runtime_cost_penetrate_reduce=cost_penetrate_reduce,
            runtime_cost_penetrate_add=cost_penetrate_add,
            move_effect_used_in_turn=(
                card.runtime_state.is_move_produce_exam_effect_use_in_turn
            ),
        )

    @classmethod
    def from_native_ordered(
        cls,
        card: NativeOrderedCardInstance,
        *,
        database: Path = DEFAULT_DATABASE,
        master_dir: Path = DEFAULT_MASTER_DIR,
    ) -> "Plan3NativeCard":
        if not isinstance(card, NativeOrderedCardInstance):
            raise TypeError("card must be NativeOrderedCardInstance")
        (
            grow_ids,
            lesson_add,
            full_power_point_cost_add,
            grow_lesson_count_add,
            block_add,
            cost_reduce,
            cost_add,
            cost_penetrate_reduce,
            cost_penetrate_add,
            full_power_point_add,
            full_power_point_add_ids,
        ) = _parse_plan3_runtime_grow(
            card.runtime_state, master_dir=master_dir
        )
        (
            customize_ids,
            customize_lesson_add,
            lesson_count_add,
            customize_full_power_point_add,
        ) = parse_plan3_runtime_customization_with_full_power(
            card.card_id,
            card.effective_upgrade,
            card.runtime_state,
            database=database,
            master_dir=master_dir,
        )
        runtime_customization = parse_plan3_native_runtime_customization(
            card.card_id,
            card.effective_upgrade,
            card.runtime_state,
            database=database,
            master_dir=master_dir,
        )
        grow_status = parse_plan3_runtime_card_grow_status(
            card.card_id,
            card.effective_upgrade,
            card.runtime_state,
            database=database,
            master_dir=master_dir,
        )
        return cls(
            guid=card.guid,
            card_id=card.card_id,
            base_upgrade=card.base_upgrade,
            temporary_upgrade=card.temporary_upgrade,
            effective_upgrade=card.effective_upgrade,
            support_upgrade_ids=card.support_upgrade_ids,
            fixed_deck_order=card.fixed_deck_order,
            play_count=card.runtime_state.play_count,
            runtime_grow_effect_ids=grow_ids,
            runtime_lesson_add=lesson_add,
            runtime_full_power_point_add=(
                full_power_point_add
                + customize_full_power_point_add.value
            ),
            runtime_full_power_point_add_aggregate=(
                Plan3NativeFullPowerPointAdd(
                    value=(
                        full_power_point_add
                        + customize_full_power_point_add.value
                    ),
                    grow_effect_ids=(
                        *full_power_point_add_ids,
                        *customize_full_power_point_add.grow_effect_ids,
                    ),
                    customize_ids=customize_full_power_point_add.customize_ids,
                )
            ),
            runtime_full_power_point_cost_add=full_power_point_cost_add,
            runtime_customize_ids=customize_ids,
            runtime_customization=runtime_customization,
            runtime_customize_lesson_add=customize_lesson_add,
            runtime_lesson_count_add=(
                grow_lesson_count_add + lesson_count_add
            ),
            runtime_grow_status=grow_status,
            runtime_block_add=block_add,
            runtime_cost_reduce=cost_reduce,
            runtime_cost_add=cost_add,
            runtime_cost_penetrate_reduce=cost_penetrate_reduce,
            runtime_cost_penetrate_add=cost_penetrate_add,
            move_effect_used_in_turn=(
                card.runtime_state.is_move_produce_exam_effect_use_in_turn
            ),
        )

    @property
    def ref(self) -> Plan3CardRef:
        return Plan3CardRef(self.card_id, self.effective_upgrade)

    @property
    def full_power_point_add(self) -> Plan3NativeFullPowerPointAdd:
        """Return the typed FullPowerPointAdd lineage for this GUID."""

        return self.runtime_full_power_point_add_aggregate

    @property
    def runtime_full_power_point_add_lineage(
        self,
    ) -> Plan3NativeFullPowerPointAdd:
        """Alias used by identity/audit consumers."""

        return self.runtime_full_power_point_add_aggregate

    def reset_support_upgrade(self) -> "Plan3NativeCard":
        if not self.support_upgrade_ids:
            return self
        return replace(
            self,
            effective_upgrade=self.base_upgrade + self.temporary_upgrade,
            support_upgrade_ids=(),
        )

    def refresh_runtime_grow_status(self) -> "Plan3NativeCard":
        """Refresh local listener phase counters without reviving its budget."""

        status = self.runtime_grow_status
        if status is None or not status.phase_counts:
            return self
        return replace(
            self,
            runtime_grow_status=replace(status, phase_counts=()),
        )

    def increment_play_count(self) -> "Plan3NativeCard":
        """Persist one completed play on this exact GUID instance."""

        if self.play_count >= INT32_MAX:
            raise Plan3NativeStateError(
                "play-count-overflow",
                f"{self.guid}:native signed play_count cannot be incremented exactly",
            )
        return replace(self, play_count=self.play_count + 1)

    def apply_grow_status_events(
        self,
        *,
        stance_changes: int,
        concentration_changes: int = 0,
        preservation_changes: int = 0,
        full_power_changes: int,
    ) -> "Plan3NativeCard":
        """Apply proven stance events to this exact card instance."""

        status = self.runtime_grow_status
        if status is None:
            return self
        if status.trigger_kind in {
            "card_play_after_self_full_power",
            "card_play_after_effect_group",
        }:
            return self
        if status.trigger_kind == "full_power":
            observed = full_power_changes
            phase = _PHASE_VALUE_STANCE_CHANGE_FULL_POWER
        elif status.trigger_kind == "stance_change_concentration":
            observed = concentration_changes
            phase = _PHASE_VALUE_STANCE_CHANGE_CONCENTRATION
        elif status.trigger_kind == "stance_change_preservation":
            observed = preservation_changes
            phase = _PHASE_VALUE_STANCE_CHANGE_PRESERVATION
        else:
            observed = stance_changes
            phase = _PHASE_VALUE_STANCE_CHANGE_COUNT_INTERVAL
        if observed < 0:
            raise Plan3NativeStateError("card-grow-event-count-negative", self.guid)
        if status.trigger_kind == "stance_change_interval":
            status, requested = status.advance_stance_change_callbacks(observed)
        else:
            status = status.with_phase_increment(phase, observed)
            requested = observed
        fires = status.capped_fire_count(requested)
        if fires == 0:
            return replace(self, runtime_grow_status=status)
        working = self
        for _ in range(fires):
            working = working.apply_runtime_grow_effects(status.grow_effects)
        return replace(
            working,
            runtime_grow_status=replace(
                status, spend_count=status.spend_count + fires
            ),
        )

    def apply_card_play_after_grow_event(
        self,
        *,
        is_full_power: bool,
        is_playing: bool = True,
        played_effect_group_ids: Iterable[str] = (),
    ) -> "Plan3NativeCard":
        """Apply proven CardPlayAfter search predicates to this GUID."""

        if not isinstance(is_full_power, bool):
            raise TypeError("is_full_power must be bool")
        if not isinstance(is_playing, bool):
            raise TypeError("is_playing must be bool")
        groups = tuple(played_effect_group_ids)
        if any(not isinstance(value, str) or not value for value in groups):
            raise TypeError(
                "played_effect_group_ids must contain non-empty text"
            )
        status = self.runtime_grow_status
        if status is None or status.trigger_kind not in {
            "card_play_after_self_full_power",
            "card_play_after_effect_group",
        }:
            return self
        status = status.with_phase_increment(_PHASE_VALUE_CARD_PLAY_AFTER, 1)
        if status.trigger_kind == "card_play_after_self_full_power":
            matches = is_playing and is_full_power
        else:
            matches = set(status.target_effect_group_ids).issubset(groups)
        fires = status.capped_fire_count(int(matches))
        if fires == 0:
            return replace(self, runtime_grow_status=status)
        working = self.apply_runtime_grow_effects(status.grow_effects)
        return replace(
            working,
            runtime_grow_status=replace(
                status, spend_count=status.spend_count + fires
            ),
        )

    def apply_runtime_grow_effects(
        self, effects: Iterable[Plan3NativeGrowEffect]
    ) -> "Plan3NativeCard":
        """Materialize statically resolved grow effects on this GUID."""

        additions = tuple(effects)
        if not additions:
            return self
        if any(
            effect.effect_type
            not in {
                _GROW_TYPE_LESSON_ADD,
                _GROW_TYPE_LESSON_COUNT_ADD,
                _GROW_TYPE_BLOCK_ADD,
                _GROW_TYPE_FULL_POWER_POINT_ADD,
                _GROW_TYPE_COST_REDUCE,
                _GROW_TYPE_COST_ADD,
                _GROW_TYPE_COST_PENETRATE_REDUCE,
                _GROW_TYPE_COST_PENETRATE_ADD,
                _GROW_TYPE_COST_FULL_POWER_POINT_ADD,
            }
            for effect in additions
        ):
            raise Plan3NativeStateError(
                "unsupported-runtime-grow-effect-type", self.guid
            )
        return replace(
            self,
            runtime_grow_effect_ids=tuple(
                (*self.runtime_grow_effect_ids, *(effect.id for effect in additions))
            ),
            runtime_lesson_add=(
                self.runtime_lesson_add
                + sum(
                    effect.value
                    for effect in additions
                    if effect.effect_type == _GROW_TYPE_LESSON_ADD
                )
            ),
            runtime_lesson_count_add=(
                self.runtime_lesson_count_add
                + sum(
                    effect.value
                    for effect in additions
                    if effect.effect_type == _GROW_TYPE_LESSON_COUNT_ADD
                )
            ),
            runtime_block_add=(
                self.runtime_block_add
                + sum(
                    effect.value
                    for effect in additions
                    if effect.effect_type == _GROW_TYPE_BLOCK_ADD
                )
            ),
            runtime_full_power_point_add=(
                self.runtime_full_power_point_add
                + sum(
                    effect.value
                    for effect in additions
                    if effect.effect_type == _GROW_TYPE_FULL_POWER_POINT_ADD
                )
            ),
            runtime_full_power_point_add_aggregate=(
                replace(
                    self.runtime_full_power_point_add_aggregate,
                    value=(
                        self.runtime_full_power_point_add
                        + sum(
                            effect.value
                            for effect in additions
                            if effect.effect_type
                            == _GROW_TYPE_FULL_POWER_POINT_ADD
                        )
                    ),
                    grow_effect_ids=(
                        self.runtime_full_power_point_add_aggregate.grow_effect_ids
                        + tuple(
                            effect.id
                            for effect in additions
                            if effect.effect_type
                            == _GROW_TYPE_FULL_POWER_POINT_ADD
                        )
                    ),
                )
            ),
            runtime_cost_reduce=(
                self.runtime_cost_reduce
                + sum(
                    effect.value
                    for effect in additions
                    if effect.effect_type == _GROW_TYPE_COST_REDUCE
                )
            ),
            runtime_cost_add=(
                self.runtime_cost_add
                + sum(
                    effect.value
                    for effect in additions
                    if effect.effect_type == _GROW_TYPE_COST_ADD
                )
            ),
            runtime_cost_penetrate_reduce=(
                self.runtime_cost_penetrate_reduce
                + sum(
                    effect.value
                    for effect in additions
                    if effect.effect_type
                    == _GROW_TYPE_COST_PENETRATE_REDUCE
                )
            ),
            runtime_cost_penetrate_add=(
                self.runtime_cost_penetrate_add
                + sum(
                    effect.value
                    for effect in additions
                    if effect.effect_type == _GROW_TYPE_COST_PENETRATE_ADD
                )
            ),
            runtime_full_power_point_cost_add=(
                self.runtime_full_power_point_cost_add
                + sum(
                    effect.value
                    for effect in additions
                    if effect.effect_type
                    == _GROW_TYPE_COST_FULL_POWER_POINT_ADD
                )
            ),
        )

    def install_support_upgrades(
        self, support_ids: Iterable[str]
    ) -> "Plan3NativeCard":
        if isinstance(support_ids, (str, bytes)):
            raise TypeError("support_ids must be an iterable of IDs")
        additions = tuple(support_ids)
        existing = set(self.support_upgrade_ids)
        seen: set[str] = set()
        for support_id in additions:
            _text(support_id, "support_id")
            if support_id in existing or support_id in seen:
                raise Plan3NativeStateError(
                    "duplicate-support-upgrade-id", support_id
                )
            if self.effective_upgrade + len(seen) >= 3:
                raise Plan3NativeStateError(
                    "support-upgrade-overflow", self.guid
                )
            seen.add(support_id)
        if not additions:
            return self
        return replace(
            self,
            effective_upgrade=self.effective_upgrade + len(additions),
            support_upgrade_ids=(*self.support_upgrade_ids, *additions),
        )


@dataclass(frozen=True, slots=True)
class Plan3NativeCardMoveTarget:
    guid: str
    card: Plan3NativeCard
    source_zone: str
    source_index: int

    def __post_init__(self) -> None:
        _text(self.guid, "card_move_guid")
        if self.guid != self.card.guid:
            raise Plan3NativeStateError("card-move-guid-card-mismatch", self.guid)
        if self.source_zone not in {
            "hand",
            "draw",
            "discard",
            "hold",
            "playing",
        }:
            raise Plan3NativeStateError(
                "invalid-card-move-source-zone", self.source_zone
            )
        if (
            not isinstance(self.source_index, int)
            or isinstance(self.source_index, bool)
            or self.source_index < 0
        ):
            raise Plan3NativeStateError("invalid-card-move-source-index")


@dataclass(frozen=True, slots=True)
class Plan3NativeProjection:
    hand: tuple[Plan3CardRef, ...]
    deck: tuple[Plan3CardRef, ...]
    grave: tuple[Plan3CardRef, ...]
    lost: tuple[Plan3CardRef, ...]
    hold: tuple[Plan3CardRef, ...]


@dataclass(frozen=True, slots=True)
class Plan3NativeState:
    """Immutable GUID-preserving state for Hand/Deck/Grave/Lost/Hold."""

    hand: tuple[Plan3NativeCard, ...] = ()
    deck: tuple[Plan3NativeCard, ...] = ()
    grave: tuple[Plan3NativeCard, ...] = ()
    lost: tuple[Plan3NativeCard, ...] = ()
    hold: tuple[Plan3NativeCard, ...] = ()
    random_state: int = 0
    turn_used_support_ids: tuple[str, ...] = ()
    total_effect_draw_card_count: int = 0
    search_stamina_runtime: Plan3SearchStaminaRuntime = (
        Plan3SearchStaminaRuntime()
    )
    anti_debuff_runtime: AntiDebuffRuntime = AntiDebuffRuntime()
    enthusiastic_runtime: EnthusiasticRuntime = EnthusiasticRuntime()

    def __post_init__(self) -> None:
        all_cards: list[Plan3NativeCard] = []
        for zone_name in ("hand", "deck", "grave", "lost", "hold"):
            try:
                zone = tuple(getattr(self, zone_name))
            except TypeError as error:
                raise TypeError(f"{zone_name} must be an iterable of cards") from error
            if not all(isinstance(card, Plan3NativeCard) for card in zone):
                raise TypeError(f"{zone_name} must contain Plan3NativeCard values")
            object.__setattr__(self, zone_name, zone)
            all_cards.extend(zone)
        guids = tuple(card.guid for card in all_cards)
        if len(set(guids)) != len(guids):
            raise Plan3NativeStateError("duplicate-guid")
        _uint32(self.random_state)
        if not isinstance(
            self.search_stamina_runtime, Plan3SearchStaminaRuntime
        ):
            raise TypeError(
                "search_stamina_runtime must be Plan3SearchStaminaRuntime"
            )
        if not isinstance(self.anti_debuff_runtime, AntiDebuffRuntime):
            raise TypeError(
                "anti_debuff_runtime must be AntiDebuffRuntime"
            )
        if not isinstance(self.enthusiastic_runtime, EnthusiasticRuntime):
            raise TypeError(
                "enthusiastic_runtime must be EnthusiasticRuntime"
            )
        if (
            not isinstance(self.total_effect_draw_card_count, int)
            or isinstance(self.total_effect_draw_card_count, bool)
            or self.total_effect_draw_card_count < 0
        ):
            raise Plan3NativeStateError("invalid-total-effect-draw-card-count")

        used = tuple(self.turn_used_support_ids)
        if any(not isinstance(value, str) or not value.strip() for value in used):
            raise Plan3NativeStateError("invalid-turn-used-support-id")
        if len(set(used)) != len(used):
            raise Plan3NativeStateError("duplicate-turn-used-support-id")
        object.__setattr__(self, "turn_used_support_ids", used)
        used_set = set(used)
        active_supports = {
            support_id
            for card in (*self.hand, *self.hold)
            for support_id in card.support_upgrade_ids
        }
        missing_used = active_supports - used_set
        if missing_used:
            raise Plan3NativeStateError(
                "active-support-not-marked-used", sorted(missing_used)[0]
            )
        for zone_name in ("deck", "grave", "lost"):
            card = next(
                (
                    value
                    for value in getattr(self, zone_name)
                    if value.support_upgrade_ids
                ),
                None,
            )
            if card is not None:
                raise Plan3NativeStateError(
                    "support-upgrade-outside-hand-hold",
                    f"{zone_name}:{card.guid}",
                )

    def try_block_status_addition(
        self,
        *,
        incoming_effect_type_value: int,
        incoming_target_type: ExamStatusEffectTargetType,
        simulate: bool = False,
    ) -> tuple["Plan3NativeState", AntiDebuffBlockResult]:
        """Run the native pre-add gate from an explicit target classification."""

        result = try_block_status_addition(
            self.anti_debuff_runtime,
            incoming_effect_type_value=incoming_effect_type_value,
            incoming_target_type=incoming_target_type,
            simulate=simulate,
        )
        after = (
            self
            if result.state_after is self.anti_debuff_runtime
            else replace(self, anti_debuff_runtime=result.state_after)
        )
        return after, result

    def spend_enthusiastic_turn(
        self,
        *,
        next_status_uid: int,
        source: str,
    ) -> tuple["Plan3NativeState", EnthusiasticReceipt]:
        """Remove the passing turn-one native release status."""

        receipt = spend_enthusiastic_turn(
            self.enthusiastic_runtime,
            next_status_uid=next_status_uid,
            source=source,
        )
        return replace(self, enthusiastic_runtime=receipt.after), receipt

    @classmethod
    def from_local_save(
        cls,
        state: LocalSaveExamState,
        *,
        database: Path = DEFAULT_DATABASE,
        master_dir: Path = DEFAULT_MASTER_DIR,
        projected_state: Plan3State | None = None,
    ) -> "Plan3NativeState":
        if not isinstance(state, LocalSaveExamState):
            raise TypeError("state must be LocalSaveExamState")
        if projected_state is not None and not isinstance(
            projected_state, Plan3State
        ):
            raise TypeError("projected_state must be Plan3State or None")
        if state.playing_card is not None:
            raise Plan3NativeStateError("playing-card-transition-unsettled")
        if state.removed_cards:
            raise Plan3NativeStateError("removed-card-zone-unmodelled")

        def cards(zone: Iterable[LocalSaveExamCard]) -> tuple[Plan3NativeCard, ...]:
            return tuple(
                Plan3NativeCard.from_local_save(
                    card, database=database, master_dir=master_dir
                )
                for card in zone
            )

        total_effect_draw_card_count = 0
        if state.root_runtime is not None:
            opaque = state.root_runtime.opaque_fields.to_value()
            if not isinstance(opaque, Mapping):
                raise Plan3NativeStateError("invalid-root-runtime-opaque-fields")
            raw_status = opaque.get("status")
            if projected_state is None:
                if not isinstance(raw_status, Mapping):
                    raise Plan3NativeStateError(
                        "active-status-projection-required",
                        "status-graph-unavailable",
                    )
                raw_active = raw_status.get("_effectList", ())
                if not isinstance(raw_active, list):
                    raise Plan3NativeStateError(
                        "active-status-projection-required",
                        "active-list-shape",
                    )
                if raw_active:
                    raise Plan3NativeStateError(
                        "active-status-projection-required",
                        f"active-count={len(raw_active)}",
                    )
            raw_total = opaque.get("totalDrawCardCount")
            if (
                not isinstance(raw_total, int)
                or isinstance(raw_total, bool)
                or raw_total < 0
            ):
                raise Plan3NativeStateError(
                    "invalid-total-effect-draw-card-count"
                )
            total_effect_draw_card_count = raw_total

        result = cls(
            hand=cards(state.zones.hand),
            deck=cards(state.zones.deck),
            grave=cards(state.zones.grave),
            lost=cards(state.zones.lost),
            hold=cards(state.zones.hold),
            random_state=state.random_state,
            turn_used_support_ids=state.turn_use_support_ids,
            total_effect_draw_card_count=total_effect_draw_card_count,
            anti_debuff_runtime=(
                AntiDebuffRuntime()
                if projected_state is None
                else projected_state.anti_debuff_runtime
            ),
            enthusiastic_runtime=(
                EnthusiasticRuntime()
                if projected_state is None
                else projected_state.enthusiastic_runtime
            ),
        )
        if projected_state is not None:
            result.assert_plan3_projection(projected_state)
        return result

    @property
    def all_cards(self) -> tuple[Plan3NativeCard, ...]:
        return (*self.hand, *self.deck, *self.grave, *self.lost, *self.hold)

    def projection(self) -> Plan3NativeProjection:
        return Plan3NativeProjection(
            hand=tuple(card.ref for card in self.hand),
            deck=tuple(card.ref for card in self.deck),
            grave=tuple(card.ref for card in self.grave),
            lost=tuple(card.ref for card in self.lost),
            hold=tuple(card.ref for card in self.hold),
        )

    def card_by_guid(self, guid: str) -> Plan3NativeCard:
        matches = tuple(card for card in self.all_cards if card.guid == guid)
        if len(matches) != 1:
            raise Plan3NativeStateError("guid-not-found", guid)
        return matches[0]

    def card_move_candidates(
        self,
        search: ProduceCardSearchRule,
        *,
        playing_guid: str,
    ) -> tuple[Plan3NativeCardMoveTarget, ...]:
        """Build Android's ordered CardMove collection for one search row."""

        _text(playing_guid, "playing_guid")
        invalid = validate_plan3_card_move_search(search)
        if invalid is not None:
            raise Plan3NativeStateError(invalid)
        playing_matches = tuple(
            (index, card)
            for index, card in enumerate(self.hand)
            if card.guid == playing_guid
        )
        if len(playing_matches) != 1:
            raise Plan3NativeStateError("played-guid-not-in-hand", playing_guid)
        playing_index, playing_card = playing_matches[0]
        groups = {
            "ProduceCardPositionType_Hand": (
                (
                    "hand",
                    tuple(
                        (index, card)
                        for index, card in enumerate(self.hand)
                        if card.guid != playing_guid
                    ),
                ),
            ),
            "ProduceCardPositionType_Deck": (
                ("draw", tuple(enumerate(self.deck))),
            ),
            "ProduceCardPositionType_Grave": (
                ("discard", tuple(enumerate(self.grave))),
            ),
            "ProduceCardPositionType_DeckGrave": (
                ("draw", tuple(enumerate(self.deck))),
                ("discard", tuple(enumerate(self.grave))),
            ),
            "ProduceCardPositionType_Hold": (
                ("hold", tuple(enumerate(self.hold))),
            ),
            "ProduceCardPositionType_Playing": (
                ("playing", ((playing_index, playing_card),)),
            ),
        }.get(search.card_position_type)
        if groups is None:
            raise Plan3NativeStateError(
                "card-move-search-position", search.id
            )
        result: list[Plan3NativeCardMoveTarget] = []
        for source_zone, entries in groups:
            for source_index, card in entries:
                matches, reason = match_plan3_card_move_search(
                    search, card.card_id, card.effective_upgrade
                )
                if reason is not None:
                    raise Plan3NativeStateError(reason)
                if matches:
                    result.append(
                        Plan3NativeCardMoveTarget(
                            card.guid, card, source_zone, source_index
                        )
                    )
        if search.limit_count > 0:
            result = result[: search.limit_count]
        return tuple(result)

    def resolve_random_card_move_targets(
        self,
        candidates: Iterable[Plan3NativeCardMoveTarget],
        *,
        count_min: int,
        count_max: int,
    ) -> tuple["Plan3NativeState", tuple[Plan3NativeCardMoveTarget, ...]]:
        """Replay Random pick count, per-candidate keys, and canonical order."""

        ordered = tuple(candidates)
        if not all(isinstance(value, Plan3NativeCardMoveTarget) for value in ordered):
            raise TypeError("candidates must contain Plan3NativeCardMoveTarget")
        if (
            not isinstance(count_min, int)
            or isinstance(count_min, bool)
            or not isinstance(count_max, int)
            or isinstance(count_max, bool)
            or count_min < 0
            or count_min > count_max
            or count_max >= INT32_MAX
        ):
            raise Plan3NativeStateError("invalid-card-move-random-count")
        count, random_state = next_range(
            self.random_state, count_min, count_max + 1
        )
        keyed: list[tuple[int, int, Plan3NativeCardMoveTarget]] = []
        for index, candidate in enumerate(ordered):
            key, random_state = next_int32(random_state)
            keyed.append((key, index, candidate))
        picked = sorted(keyed, key=lambda value: (value[0], value[1]))[
            : min(count, len(keyed))
        ]
        selected_indices = {index for _key, index, _candidate in picked}
        selected = tuple(
            candidate
            for index, candidate in enumerate(ordered)
            if index in selected_indices
        )
        return replace(self, random_state=random_state), selected

    def move_card_targets(
        self,
        targets: Iterable[Plan3NativeCardMoveTarget],
        move_destination: str,
        *,
        hold_limit: int,
        hand_limit: int,
        is_full_power: bool,
        lesson_type: str,
        support_upgrades: SupportInputs,
        support_card_searches: Mapping[str, ProduceCardSearchRule],
    ) -> "Plan3NativeState":
        """Move exact GUID targets in selector order at one effect slot."""

        selected = tuple(targets)
        if not all(isinstance(value, Plan3NativeCardMoveTarget) for value in selected):
            raise TypeError("targets must contain Plan3NativeCardMoveTarget")
        if move_destination not in {MOVE_HAND, MOVE_GRAVE, MOVE_LOST, MOVE_HOLD}:
            raise Plan3NativeStateError(
                "unsupported-card-move-destination", move_destination
            )
        if len({value.guid for value in selected}) != len(selected):
            raise Plan3NativeStateError("duplicate-move-guid")
        for value, label in ((hold_limit, "hold"), (hand_limit, "hand")):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise Plan3NativeStateError(f"invalid-{label}-limit")
        if not isinstance(is_full_power, bool):
            raise TypeError("is_full_power must be bool")
        if move_destination == MOVE_HOLD and is_full_power:
            if selected:
                raise Plan3NativeStateError("hold-add-during-full-power")
            return self
        if not selected:
            return self

        zone_values = {
            "hand": self.hand,
            "draw": self.deck,
            "discard": self.grave,
            "hold": self.hold,
        }
        for target in selected:
            if target.source_zone == "playing":
                raise Plan3NativeStateError(
                    "card-move-playing-settlement-deferred", target.guid
                )
            zone = zone_values[target.source_zone]
            if (
                target.source_index >= len(zone)
                or zone[target.source_index] != target.card
            ):
                raise Plan3NativeStateError(
                    "card-move-source-position-stale", target.guid
                )
        selected_guids = {target.guid for target in selected}
        hand = tuple(card for card in self.hand if card.guid not in selected_guids)
        deck = tuple(card for card in self.deck if card.guid not in selected_guids)
        grave = tuple(card for card in self.grave if card.guid not in selected_guids)
        hold = tuple(card for card in self.hold if card.guid not in selected_guids)
        moved = tuple(
            target.card.reset_support_upgrade()
            if target.source_zone in {"hand", "playing"}
            else target.card
            for target in selected
        )
        if move_destination in {MOVE_GRAVE, MOVE_LOST} and any(
            card.support_upgrade_ids for card in moved
        ):
            raise Plan3NativeStateError(
                "card-move-support-upgrade-outside-hand-hold", moved[0].guid
            )

        changes: dict[str, object] = {
            "hand": hand,
            "deck": deck,
            "grave": grave,
            "hold": hold,
        }
        if move_destination == MOVE_HAND:
            if len(hand) + len(moved) > hand_limit:
                raise Plan3NativeStateError(
                    "card-move-hand-limit",
                    f"hand={len(hand)}:add={len(moved)}:limit={hand_limit}",
                )
            if any(card.support_upgrade_ids for card in moved):
                raise Plan3NativeStateError(
                    "card-move-hand-existing-support-unproven", moved[0].guid
                )
            hand_add = evaluate_native_hand_add_support(
                (
                    NativeHandAddCard(
                        guid=card.guid,
                        card_id=card.card_id,
                        base_upgrade=card.base_upgrade,
                        effective_upgrade=card.effective_upgrade,
                    )
                    for card in moved
                ),
                lesson_type=lesson_type,
                random_state=self.random_state,
                support_upgrades=support_upgrades,
                support_card_searches=support_card_searches,
                used_support_ids=self.turn_used_support_ids,
            )
            result_by_guid = {value.guid: value for value in hand_add.cards}
            resolved = tuple(
                card.install_support_upgrades(
                    result_by_guid[card.guid].added_support_ids
                )
                for card in moved
            )
            changes.update(
                hand=(*hand, *resolved),
                random_state=hand_add.final_random_state,
                turn_used_support_ids=hand_add.used_support_ids,
            )
        elif move_destination == MOVE_HOLD:
            combined = (*hold, *moved)
            overflow_count = max(0, len(combined) - hold_limit)
            overflow = combined[:overflow_count]
            if any(card.support_upgrade_ids for card in overflow):
                raise Plan3NativeStateError(
                    "hold-overflow-support-upgrade-unproven", overflow[0].guid
                )
            changes.update(
                hold=combined[overflow_count:],
                grave=(*grave, *overflow),
            )
        elif move_destination == MOVE_GRAVE:
            changes["grave"] = (*grave, *moved)
        else:
            changes["lost"] = (*self.lost, *moved)
        return replace(self, **changes)

    def apply_card_grow_events(
        self,
        before: Plan3State,
        after: Plan3State,
    ) -> "Plan3NativeState":
        """Write proven scalar stance transitions back to GUID card state."""

        if not isinstance(before, Plan3State) or not isinstance(after, Plan3State):
            raise TypeError("before and after must be Plan3State values")
        stance_changes = after.stance_change_count - before.stance_change_count
        concentration_changes = (
            after.concentration_change_count
            - before.concentration_change_count
        )
        preservation_changes = (
            after.preservation_change_count
            - before.preservation_change_count
        )
        full_power_changes = (
            after.full_power_change_count - before.full_power_change_count
        )
        if any(
            count < 0
            for count in (
                stance_changes,
                concentration_changes,
                preservation_changes,
                full_power_changes,
            )
        ):
            raise Plan3NativeStateError("card-grow-stance-counter-regressed")
        if (
            concentration_changes
            + preservation_changes
            + full_power_changes
            > stance_changes
        ):
            raise Plan3NativeStateError("card-grow-stance-counter-mismatch")
        if stance_changes == 0:
            return self
        replacements: dict[str, tuple[Plan3NativeCard, ...]] = {}
        changed = False
        for zone_name in ("hand", "deck", "grave", "lost", "hold"):
            zone = tuple(
                card.apply_grow_status_events(
                    stance_changes=stance_changes,
                    concentration_changes=concentration_changes,
                    preservation_changes=preservation_changes,
                    full_power_changes=full_power_changes,
                )
                for card in getattr(self, zone_name)
            )
            replacements[zone_name] = zone
            changed = changed or zone != getattr(self, zone_name)
        return replace(self, **replacements) if changed else self

    def apply_card_play_after_grow_events(
        self,
        guid: str,
        *,
        is_full_power: bool,
        played_effect_group_ids: Iterable[str] = (),
        skip_listener_guids: Iterable[str] = (),
        detached_played_card: Plan3NativeCard | None = None,
    ) -> "Plan3NativeState":
        """Apply per-card CardPlayAfter listeners after the final zone move."""

        _text(guid, "guid")
        if not isinstance(is_full_power, bool):
            raise TypeError("is_full_power must be bool")
        groups = tuple(played_effect_group_ids)
        if any(not isinstance(value, str) or not value for value in groups):
            raise TypeError(
                "played_effect_group_ids must contain non-empty text"
            )
        skipped = tuple(skip_listener_guids)
        if any(not isinstance(value, str) or not value for value in skipped):
            raise TypeError(
                "skip_listener_guids must contain non-empty text"
            )
        if len(set(skipped)) != len(skipped):
            raise Plan3NativeStateError("duplicate-skipped-listener-guid")
        matches = tuple(card for card in self.all_cards if card.guid == guid)
        if detached_played_card is not None:
            if (not isinstance(detached_played_card, Plan3NativeCard)
                    or detached_played_card.guid != guid or matches):
                raise Plan3NativeStateError("detached-played-card-identity-mismatch", guid)
        elif len(matches) != 1:
            raise Plan3NativeStateError("played-guid-not-settled", guid)
        if any(card.guid == guid for card in self.hand):
            raise Plan3NativeStateError("played-guid-still-in-hand", guid)
        replacements: dict[str, tuple[Plan3NativeCard, ...]] = {}
        changed = False
        for zone_name in ("hand", "deck", "grave", "lost", "hold"):
            zone = tuple(
                (
                    card
                    if card.guid in skipped
                    else card.apply_card_play_after_grow_event(
                        is_full_power=is_full_power,
                        is_playing=card.guid == guid,
                        played_effect_group_ids=groups,
                    )
                )
                for card in getattr(self, zone_name)
            )
            replacements[zone_name] = zone
            changed = changed or zone != getattr(self, zone_name)
        return replace(self, **replacements) if changed else self

    def apply_card_play_after_grow_event(
        self,
        guid: str,
        *,
        is_full_power: bool,
    ) -> "Plan3NativeState":
        """Compatibility wrapper for the proven self-target listener."""

        return self.apply_card_play_after_grow_events(
            guid,
            is_full_power=is_full_power,
        )

    def apply_exam_add_grow_effects(
        self,
        effects: Iterable[Plan3Effect],
        *,
        master_dir: Path = DEFAULT_MASTER_DIR,
    ) -> "Plan3NativeState":
        """Apply the proven ``deck_all`` AddGrow effect to every card GUID."""

        if isinstance(effects, (str, bytes)):
            raise TypeError("effects must be an iterable of Plan3Effect values")
        resolved: list[Plan3NativeGrowEffect] = []
        for effect in tuple(effects):
            if not isinstance(effect, Plan3Effect):
                raise TypeError("effects must contain Plan3Effect values")
            rule = effect.card_move_rule
            if effect.effect_type != EFFECT_ADD_GROW or rule is None:
                raise Plan3NativeStateError(
                    "unsupported-exam-add-grow-effect", effect.id
                )
            if not (
                effect.value1 == 0
                and effect.value2 == 0
                and effect.effect_count == 0
                and effect.effect_turn == 0
                and not effect.status_enchant_id
                and effect.status_enchant is None
                and not effect.chain_effect_id
                and effect.trigger is None
                and not rule.target_card_id
                and rule.target_upgrade == 0
                and rule.target_effect_type == EFFECT_UNKNOWN
                and rule.search_id == SEARCH_DECK_ALL
                and rule.destination == MOVE_UNKNOWN
                and rule.pick_range_type == PICK_RANGE_ALL
                and not rule.pick_reference_search_id
                and rule.pick_count_type == PICK_COUNT_UNKNOWN
                and rule.pick_count_min == 0
                and rule.pick_count_max == 0
                and not rule.second_search_id
                and rule.second_pick_range_type == PICK_RANGE_UNKNOWN
                and not rule.second_pick_reference_search_id
                and rule.second_pick_count_type == PICK_COUNT_UNKNOWN
                and rule.second_pick_count_min == 0
                and rule.second_pick_count_max == 0
                and not rule.chain_effect_ids
                and not rule.card_status_enchant_id
                and bool(rule.card_grow_effect_ids)
            ):
                raise Plan3NativeStateError(
                    "unsupported-exam-add-grow-shape", effect.id
                )
            resolved.extend(
                _grow_effect_from_master(
                    grow_id,
                    master_dir=Path(master_dir),
                    allowed_types=frozenset({_GROW_TYPE_LESSON_ADD}),
                )
                for grow_id in rule.card_grow_effect_ids
            )
        if not resolved:
            return self
        replacements = {
            zone_name: tuple(
                card.apply_runtime_grow_effects(resolved)
                for card in getattr(self, zone_name)
            )
            for zone_name in ("hand", "deck", "grave", "lost", "hold")
        }
        return replace(self, **replacements)

    def assert_plan3_projection(self, state: Plan3State) -> None:
        """Require exact shared runtime and ordered five-zone equality."""

        if not isinstance(state, Plan3State):
            raise TypeError("state must be Plan3State")
        if self.anti_debuff_runtime != state.anti_debuff_runtime:
            raise Plan3NativeStateError(
                "plan3-anti-debuff-projection-mismatch"
            )
        if self.enthusiastic_runtime != state.enthusiastic_runtime:
            raise Plan3NativeStateError(
                "plan3-enthusiastic-projection-mismatch"
            )
        projected = self.projection()
        expected = {
            "hand": projected.hand,
            "draw_pile": projected.deck,
            "discard_pile": projected.grave,
            "lost_pile": projected.lost,
            "hold_pile": projected.hold,
        }
        mismatches = tuple(
            name for name, refs in expected.items() if getattr(state, name) != refs
        )
        if mismatches:
            raise Plan3NativeStateError(
                "plan3-zone-projection-mismatch", ",".join(mismatches)
            )

    def with_move_effect_used_guids(
        self, used_guids: Iterable[str]
    ) -> "Plan3NativeState":
        """Persist native ``IsMoveEffectUseInTurn`` on exact GUIDs."""

        used = frozenset(used_guids)
        if any(not isinstance(value, str) or not value for value in used):
            raise TypeError("used_guids must contain non-empty text")
        known = {card.guid for card in self.all_cards}
        missing = used - known
        if missing:
            raise Plan3NativeStateError(
                "move-effect-used-guid-missing", sorted(missing)[0]
            )
        replacements = {
            zone_name: tuple(
                replace(
                    card,
                    move_effect_used_in_turn=(card.guid in used),
                )
                if card.move_effect_used_in_turn != (card.guid in used)
                else card
                for card in getattr(self, zone_name)
            )
            for zone_name in ("hand", "deck", "grave", "lost", "hold")
        }
        return (
            replace(self, **replacements)
            if any(
                replacements[name] != getattr(self, name)
                for name in replacements
            )
            else self
        )

    def reset_turn_used_supports(
        self,
        *,
        mark_turn_start: bool = True,
    ) -> "Plan3NativeState":
        """Open a turn and advance/reset GUID-local card runtime.

        Native ``ExamCardData.SetPassingTurnStart`` increments the persisted
        card-status ``spendTurn`` counter and clears the once-per-turn move
        marker.  A terminal EndTurn has no following TurnStart, so its caller
        passes ``mark_turn_start=False`` while retaining the historical
        support/move reset behavior.
        """

        if type(mark_turn_start) is not bool:
            raise TypeError("mark_turn_start must be boolean")

        active = next(
            (
                card.guid
                for card in (*self.hand, *self.hold)
                if card.support_upgrade_ids
            ),
            None,
        )
        if active is not None:
            raise Plan3NativeStateError(
                "turn-reset-with-active-support-upgrade", active
            )
        def reset_card(card: Plan3NativeCard) -> Plan3NativeCard:
            status = card.runtime_grow_status
            if mark_turn_start and status is not None:
                if status.spend_turn >= INT32_MAX:
                    raise Plan3NativeStateError(
                        "card-grow-status-spend-turn-overflow",
                        card.guid,
                    )
                status = replace(status, spend_turn=status.spend_turn + 1)
            if (
                status is card.runtime_grow_status
                and not card.move_effect_used_in_turn
            ):
                return card
            return replace(
                card,
                runtime_grow_status=status,
                move_effect_used_in_turn=False,
            )

        replacements = {
            zone_name: tuple(
                reset_card(card) for card in getattr(self, zone_name)
            )
            for zone_name in ("hand", "deck", "grave", "lost", "hold")
        }
        if not self.turn_used_support_ids and all(
            replacements[zone_name] == getattr(self, zone_name)
            for zone_name in replacements
        ):
            return self
        return replace(self, turn_used_support_ids=(), **replacements)

    def move_hand_by_guid(
        self, guid: str, move_destination: str
    ) -> "Plan3NativeState":
        """Route an unplayed Hand instance without changing its play count."""

        return self._move_hand_by_guid(
            guid,
            move_destination,
            increment_play_count=False,
        )

    def play_hand_by_guid(
        self, guid: str, move_destination: str
    ) -> "Plan3NativeState":
        """Route one actually played Hand instance and increment it exactly once."""

        return self._move_hand_by_guid(
            guid,
            move_destination,
            increment_play_count=True,
        )

    def play_lost_by_guid(
        self, guid: str, move_destination: str
    ) -> "Plan3NativeState":
        """Force-play one existing Lost GUID through the native play lifecycle.

        This is the same per-instance play-count and destination settlement as
        :meth:`play_hand_by_guid`; it only changes the authoritative source
        zone used by the exact target-self UsePool command.
        """

        _text(guid, "guid")
        if move_destination not in SUPPORTED_NATIVE_DESTINATIONS:
            raise Plan3NativeStateError(
                "unsupported-master-move-destination", str(move_destination)
            )
        matches = tuple(
            (index, card)
            for index, card in enumerate(self.lost)
            if card.guid == guid
        )
        if len(matches) != 1:
            raise Plan3NativeStateError("lost-guid-not-found", guid)
        index, card = matches[0]
        settled = card.increment_play_count().reset_support_upgrade()
        zone_name = {
            MOVE_HOLD: "hold",
            MOVE_LOST: "lost",
            MOVE_GRAVE: "grave",
        }[move_destination]
        source = (*self.lost[:index], *self.lost[index + 1 :])
        if zone_name == "lost":
            return replace(self, lost=(*source, settled))
        destination = getattr(self, zone_name)
        return replace(
            self,
            lost=source,
            **{zone_name: (*destination, settled)},
        )

    def _move_hand_by_guid(
        self,
        guid: str,
        move_destination: str,
        *,
        increment_play_count: bool,
    ) -> "Plan3NativeState":
        """Route one Hand instance using an exact Master move destination."""

        _text(guid, "guid")
        if move_destination not in SUPPORTED_NATIVE_DESTINATIONS:
            raise Plan3NativeStateError(
                "unsupported-master-move-destination", str(move_destination)
            )
        matches = tuple(
            (index, card)
            for index, card in enumerate(self.hand)
            if card.guid == guid
        )
        if len(matches) != 1:
            raise Plan3NativeStateError("hand-guid-not-found", guid)
        index, card = matches[0]
        if increment_play_count:
            card = card.increment_play_count()
            # Android v3.2.3 MovePlayCard resets the per-card support lineage
            # before reading PlayMovePositionType and adding it to any zone.
            card = card.reset_support_upgrade()
        if move_destination == MOVE_HOLD:
            # MoveCard resets support upgrades for both Hand (2) and Playing
            # (8) source groups before any destination dispatch, including
            # an unplayed Hand -> Hold selection.
            settled = card.reset_support_upgrade()
            zone_name = "hold"
        elif move_destination == MOVE_LOST:
            settled = card.reset_support_upgrade()
            zone_name = "lost"
        else:
            settled = card.reset_support_upgrade()
            zone_name = "grave"
        destination = getattr(self, zone_name)
        return replace(
            self,
            hand=(*self.hand[:index], *self.hand[index + 1 :]),
            **{zone_name: (*destination, settled)},
        )

    def move_hand_guids_to_hold(
        self,
        guids: Iterable[str],
        *,
        hold_limit: int,
        is_full_power: bool = False,
    ) -> "Plan3NativeState":
        """Apply one ordered native Hand -> Hold CardMove selection.

        MoveCard preserves the selector's order.  It appends the selected
        cards after existing Hold, moves the oldest overflow prefix to Grave,
        and retains the newest ``hold_limit`` cards.  Selected Hand cards keep
        play counts but reset any support-upgrade lineage.
        """

        if isinstance(guids, (str, bytes)):
            raise TypeError("guids must be an iterable of GUID strings")
        ordered_guids = tuple(guids)
        for guid in ordered_guids:
            _text(guid, "guid")
        if len(set(ordered_guids)) != len(ordered_guids):
            raise Plan3NativeStateError("duplicate-move-guid")
        if (
            not isinstance(hold_limit, int)
            or isinstance(hold_limit, bool)
            or hold_limit < 0
        ):
            raise Plan3NativeStateError("invalid-hold-limit", repr(hold_limit))
        if not isinstance(is_full_power, bool):
            raise TypeError("is_full_power must be bool")
        if len(self.hold) > hold_limit:
            raise Plan3NativeStateError(
                "hold-limit-invariant",
                f"hold={len(self.hold)}:limit={hold_limit}",
            )
        if not ordered_guids:
            return self
        if is_full_power:
            raise Plan3NativeStateError("hold-add-during-full-power")

        by_guid = {card.guid: card for card in self.hand}
        missing = next(
            (guid for guid in ordered_guids if guid not in by_guid),
            None,
        )
        if missing is not None:
            raise Plan3NativeStateError("hand-guid-not-found", missing)
        selected_set = set(ordered_guids)
        selected = tuple(
            by_guid[guid].reset_support_upgrade() for guid in ordered_guids
        )
        combined_hold = (*self.hold, *selected)
        overflow_count = max(0, len(combined_hold) - hold_limit)
        overflow = combined_hold[:overflow_count]
        if any(card.support_upgrade_ids for card in overflow):
            # Native only resets Hand/Playing sources.  A support-upgraded
            # pre-existing Hold overflow is not reachable in the proven turn
            # lifecycle, so do not invent a Grave representation for it.
            raise Plan3NativeStateError(
                "hold-overflow-support-upgrade-unproven",
                overflow[0].guid,
            )
        return replace(
            self,
            hand=tuple(
                card for card in self.hand if card.guid not in selected_set
            ),
            grave=(*self.grave, *overflow),
            hold=combined_hold[overflow_count:],
        )

    def draw_to_hand(
        self,
        requested_count: int,
        *,
        hand_limit: int,
        lesson_type: str,
        support_upgrades: SupportInputs,
        support_card_searches: Mapping[str, ProduceCardSearchRule],
        effect_draw: bool = False,
    ) -> "Plan3NativeDrawTransition":
        """Draw fixed Deck prefix, shuffle Grave, then run HandAdd supports."""

        if (
            not isinstance(requested_count, int)
            or isinstance(requested_count, bool)
            or requested_count < 0
        ):
            raise Plan3NativeStateError(
                "invalid-draw-count", repr(requested_count)
            )
        if (
            not isinstance(hand_limit, int)
            or isinstance(hand_limit, bool)
            or hand_limit < 0
        ):
            raise Plan3NativeStateError("invalid-hand-limit", repr(hand_limit))
        if len(self.hand) > hand_limit:
            raise Plan3NativeStateError(
                "hand-limit-invariant",
                f"hand={len(self.hand)}:limit={hand_limit}",
            )
        if not isinstance(effect_draw, bool):
            raise TypeError("effect_draw must be bool")
        actual_count = min(
            requested_count,
            len(self.deck) + len(self.grave),
            hand_limit - len(self.hand),
        )
        random_after_shuffle = self.random_state
        recycled_grave_count = 0
        if actual_count <= len(self.deck):
            drawn = self.deck[:actual_count]
            deck_after = self.deck[actual_count:]
            grave_after = self.grave
        else:
            prefix = self.deck
            recycled_grave_count = len(self.grave)
            fixed_orders = tuple(card.fixed_deck_order for card in self.grave)
            try:
                shuffled, random_after_shuffle = order_native_pool(
                    self.grave,
                    self.random_state,
                    fixed_deck_orders=fixed_orders,
                )
            except FixedDeckOrderError as error:
                raise Plan3NativeStateError(
                    "fixed-order-grave-sort-unproven"
                ) from error
            remaining = actual_count - len(prefix)
            drawn = (*prefix, *shuffled[:remaining])
            deck_after = tuple(shuffled[remaining:])
            grave_after = ()

        hand_add = evaluate_native_hand_add_support(
            (
                NativeHandAddCard(
                    guid=card.guid,
                    card_id=card.card_id,
                    base_upgrade=card.base_upgrade,
                    effective_upgrade=card.effective_upgrade,
                )
                for card in drawn
            ),
            lesson_type=lesson_type,
            random_state=random_after_shuffle,
            support_upgrades=support_upgrades,
            support_card_searches=support_card_searches,
            used_support_ids=self.turn_used_support_ids,
        )
        result_by_guid = {result.guid: result for result in hand_add.cards}
        resolved_drawn: list[Plan3NativeCard] = []
        for card in drawn:
            result = result_by_guid[card.guid]
            resolved = card.install_support_upgrades(result.added_support_ids)
            if resolved.effective_upgrade != result.final_effective_upgrade:
                raise AssertionError("support evaluator and card lineage diverged")
            resolved_drawn.append(resolved)
        after = replace(
            self,
            hand=(*self.hand, *resolved_drawn),
            deck=deck_after,
            grave=grave_after,
            random_state=hand_add.final_random_state,
            turn_used_support_ids=hand_add.used_support_ids,
            total_effect_draw_card_count=(
                self.total_effect_draw_card_count
                + (actual_count if effect_draw else 0)
            ),
        )
        return Plan3NativeDrawTransition(
            before=self,
            after=after,
            requested_count=requested_count,
            actual_count=actual_count,
            hand_limit=hand_limit,
            effect_draw=effect_draw,
            drawn_guids=tuple(card.guid for card in resolved_drawn),
            recycled_grave_count=recycled_grave_count,
            random_state_after_shuffle=random_after_shuffle,
            support_result=hand_add,
        )


@dataclass(frozen=True, slots=True)
class Plan3NativeDrawTransition:
    before: Plan3NativeState
    after: Plan3NativeState
    requested_count: int
    actual_count: int
    hand_limit: int
    effect_draw: bool
    drawn_guids: tuple[str, ...]
    recycled_grave_count: int
    random_state_after_shuffle: int
    support_result: NativeHandAddSupportResult

    def __post_init__(self) -> None:
        if not isinstance(self.before, Plan3NativeState) or not isinstance(
            self.after, Plan3NativeState
        ):
            raise TypeError("before and after must be Plan3NativeState values")
        for name, value in (
            ("requested_count", self.requested_count),
            ("actual_count", self.actual_count),
            ("hand_limit", self.hand_limit),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise Plan3NativeStateError(f"invalid-{name.replace('_', '-')}")
        if self.actual_count > self.requested_count:
            raise Plan3NativeStateError("actual-draw-exceeds-requested")
        if not isinstance(self.effect_draw, bool):
            raise TypeError("effect_draw must be bool")
        drawn = tuple(_text(value, "drawn_guid") for value in self.drawn_guids)
        if len(set(drawn)) != len(drawn):
            raise Plan3NativeStateError("duplicate-drawn-guid")
        object.__setattr__(self, "drawn_guids", drawn)
        if self.actual_count != len(drawn):
            raise Plan3NativeStateError("actual-draw-count-mismatch")
        expected_counter = self.before.total_effect_draw_card_count + (
            self.actual_count if self.effect_draw else 0
        )
        if self.after.total_effect_draw_card_count != expected_counter:
            raise Plan3NativeStateError("effect-draw-counter-mismatch")
        if (
            not isinstance(self.recycled_grave_count, int)
            or isinstance(self.recycled_grave_count, bool)
            or self.recycled_grave_count < 0
        ):
            raise Plan3NativeStateError("invalid-recycled-grave-count")
        _uint32(self.random_state_after_shuffle, "random_state_after_shuffle")
        if not isinstance(self.support_result, NativeHandAddSupportResult):
            raise TypeError("support_result must be NativeHandAddSupportResult")
        if self.support_result.initial_random_state != self.random_state_after_shuffle:
            raise Plan3NativeStateError("support-rng-does-not-follow-shuffle")
        if self.support_result.final_random_state != self.after.random_state:
            raise Plan3NativeStateError("support-rng-final-state-mismatch")
        support_guids = tuple(card.guid for card in self.support_result.cards)
        if support_guids != drawn:
            raise Plan3NativeStateError("support-card-order-does-not-match-draw")
        before_identity = {
            (
                card.guid,
                card.card_id,
                card.base_upgrade,
                card.temporary_upgrade,
                card.fixed_deck_order,
                card.play_count,
                card.runtime_grow_effect_ids,
                card.runtime_lesson_add,
                card.runtime_full_power_point_add,
                card.runtime_full_power_point_add_aggregate,
                card.runtime_full_power_point_cost_add,
                card.runtime_customize_ids,
                card.runtime_customization,
                card.plan2_runtime_customization,
                card.runtime_customize_lesson_add,
                card.runtime_customize_lesson_depend_review_add,
                card.runtime_lesson_count_add,
                card.runtime_grow_status,
                card.runtime_block_add,
                card.runtime_cost_reduce,
                card.runtime_cost_add,
                card.runtime_cost_penetrate_reduce,
                card.runtime_cost_penetrate_add,
            )
            for card in self.before.all_cards
        }
        after_identity = {
            (
                card.guid,
                card.card_id,
                card.base_upgrade,
                card.temporary_upgrade,
                card.fixed_deck_order,
                card.play_count,
                card.runtime_grow_effect_ids,
                card.runtime_lesson_add,
                card.runtime_full_power_point_add,
                card.runtime_full_power_point_add_aggregate,
                card.runtime_full_power_point_cost_add,
                card.runtime_customize_ids,
                card.runtime_customization,
                card.plan2_runtime_customization,
                card.runtime_customize_lesson_add,
                card.runtime_customize_lesson_depend_review_add,
                card.runtime_lesson_count_add,
                card.runtime_grow_status,
                card.runtime_block_add,
                card.runtime_cost_reduce,
                card.runtime_cost_add,
                card.runtime_cost_penetrate_reduce,
                card.runtime_cost_penetrate_add,
            )
            for card in self.after.all_cards
        }
        if before_identity != after_identity:
            raise Plan3NativeStateError("draw-changed-persistent-card-identity")
        after_by_guid = {card.guid: card for card in self.after.hand}
        if any(guid not in after_by_guid for guid in drawn):
            raise Plan3NativeStateError("drawn-guid-not-in-result-hand")

    @property
    def drawn_cards(self) -> tuple[Plan3NativeCard, ...]:
        by_guid = {card.guid: card for card in self.after.hand}
        return tuple(by_guid[guid] for guid in self.drawn_guids)


__all__ = [
    "MOVE_HOLD",
    "Plan3NativeCard",
    "Plan3NativeCardMoveTarget",
    "Plan3NativeCardGrowStatus",
    "Plan3NativeDrawTransition",
    "Plan3NativeFullPowerPointAdd",
    "Plan3NativeFullPowerPointAddAggregate",
    "Plan3NativeGrowEffect",
    "Plan3NativeRuntimeCustomization",
    "Plan3NativeRuntimeCustomizationEffect",
    "Plan3NativeProjection",
    "Plan3NativeState",
    "Plan3NativeStateError",
    "PLAN2_NATIVE_CUSTOMIZATION_GROW_TYPES",
    "SUPPORTED_NATIVE_DESTINATIONS",
    "parse_plan3_runtime_card_grow_status",
    "parse_plan2_native_runtime_customization",
    "parse_plan3_runtime_customization",
    "parse_plan3_native_runtime_customization",
    "parse_plan3_runtime_customization_ordered",
    "parse_plan3_runtime_customization_with_full_power",
    "parse_plan3_runtime_lesson_depend_review_customization",
    "parse_plan3_runtime_lesson_add",
]
