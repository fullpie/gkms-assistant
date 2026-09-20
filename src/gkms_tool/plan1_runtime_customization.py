"""Fail-closed Plan 1 card-instance customization compilation.

Plan 1's static card compiler intentionally reads only the card Master row.
The live card object carries one more piece of identity: its ordered
``customizeCountList``.  This module joins that instance list to the card's
ordered ``produceCardCustomizeIds`` and the official
``ProduceCardCustomize``/``ProduceCardGrowEffect`` rows.

The initial executable slice is deliberately small and generic.  It accepts
the ordinary stamina and two buff-cost reduction grow families:

* ``ProduceCardGrowEffectType_CostReduce``
* ``ProduceCardGrowEffectType_CostParameterBuffReduce``
* ``ProduceCardGrowEffectType_CostLessonBuffReduce``
* ``ProduceCardGrowEffectType_LessonBuffAdd``

Every other active customization, malformed list, missing Master row, or
conditional grow shape produces a typed Plan 1 blocker.  In particular, the
compiler never identifies a card by a special-case card ID.  An uncustomized
instance remains the static :class:`~gkms_tool.plan1_native_core.Plan1CompiledCard`
program with an empty modifier tuple.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import json
from functools import lru_cache
from pathlib import Path
import sqlite3
from typing import TypeAlias

import yaml

from .audition_local_save_state import (
    LocalSaveExamCard,
    LocalSaveExamCardRuntimeState,
)
from .audition_native_ordered_zones import NativeOrderedCardInstance
from .master_db import DEFAULT_DATABASE
from .plan1_native_core import (
    COST_LESSON_BUFF,
    COST_PARAMETER_BUFF,
    COST_STAMINA,
    DEFAULT_MASTER_DIR,
    EFFECT_LESSON,
    EFFECT_LESSON_BUFF,
    Plan1Blocker,
    Plan1CompiledCard,
    Plan1EffectModifier,
    Plan1PaymentModifier,
    compile_plan1_card,
)


GROW_TYPE_COST_PARAMETER_BUFF_REDUCE = (
    "ProduceCardGrowEffectType_CostParameterBuffReduce"
)
GROW_TYPE_COST_LESSON_BUFF_REDUCE = (
    "ProduceCardGrowEffectType_CostLessonBuffReduce"
)
GROW_TYPE_COST_REDUCE = "ProduceCardGrowEffectType_CostReduce"
GROW_TYPE_LESSON_BUFF_ADD = "ProduceCardGrowEffectType_LessonBuffAdd"
GROW_TYPE_LESSON_ADD = "ProduceCardGrowEffectType_LessonAdd"
GROW_TYPE_LESSON_COUNT_ADD = "ProduceCardGrowEffectType_LessonCountAdd"
# Verbose aliases are convenient at API boundaries that mirror the Master
# enum names and keep callers from depending on this module's shorter prefix.
PLAN1_CUSTOMIZATION_COST_PARAMETER_BUFF_REDUCE = (
    GROW_TYPE_COST_PARAMETER_BUFF_REDUCE
)
PLAN1_CUSTOMIZATION_COST_LESSON_BUFF_REDUCE = (
    GROW_TYPE_COST_LESSON_BUFF_REDUCE
)

SUPPORTED_PLAN1_CUSTOMIZATION_GROW_TYPES = frozenset(
    {
        GROW_TYPE_COST_PARAMETER_BUFF_REDUCE,
        GROW_TYPE_COST_LESSON_BUFF_REDUCE,
        GROW_TYPE_COST_REDUCE,
        GROW_TYPE_LESSON_BUFF_ADD,
        GROW_TYPE_LESSON_ADD,
        GROW_TYPE_LESSON_COUNT_ADD,
    }
)

_UNKNOWN = "ProduceCardGrowEffectType_Unknown"
_UNKNOWN_COST = "ExamCostType_Unknown"
_UNKNOWN_MOVE = "ProduceCardMovePositionType_Unknown"


class Plan1CustomizationError(ValueError):
    """A typed error for strict runtime-customization parsing."""

    def __init__(self, code: str, detail: str = "") -> None:
        if not isinstance(code, str) or not code:
            raise ValueError("customization error code must be non-empty text")
        if not isinstance(detail, str):
            raise TypeError("customization error detail must be text")
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


@dataclass(frozen=True, slots=True)
class Plan1RuntimeCustomization:
    """Ordered instance customization and its typed payment/effect output."""

    card_id: str
    upgrade: int
    customize_ids: tuple[str, ...]
    customize_count_list: tuple[int, ...]
    payment_modifiers: tuple[Plan1PaymentModifier, ...] = ()
    effect_modifiers: tuple[Plan1EffectModifier, ...] = ()
    blockers: tuple[Plan1Blocker, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.card_id, str) or not self.card_id:
            raise ValueError("card_id must be non-empty text")
        if (
            isinstance(self.upgrade, bool)
            or not isinstance(self.upgrade, int)
            or self.upgrade < 0
        ):
            raise ValueError("upgrade must be a non-negative integer")
        ids = tuple(self.customize_ids)
        if any(not isinstance(value, str) or not value for value in ids):
            raise ValueError("customize_ids must contain non-empty text")
        if len(set(ids)) != len(ids):
            raise ValueError("customize_ids must not contain duplicates")
        counts = tuple(self.customize_count_list)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts
        ):
            raise ValueError("customize_count_list must contain non-negative integers")
        if len(counts) != len(ids):
            raise ValueError("customize_count_list must align with customize_ids")
        payment = tuple(self.payment_modifiers)
        if any(not isinstance(value, Plan1PaymentModifier) for value in payment):
            raise TypeError(
                "payment_modifiers must contain Plan1PaymentModifier values"
            )
        effects = tuple(self.effect_modifiers)
        if any(not isinstance(value, Plan1EffectModifier) for value in effects):
            raise TypeError(
                "effect_modifiers must contain Plan1EffectModifier values"
            )
        blockers = tuple(self.blockers)
        if any(not isinstance(value, Plan1Blocker) for value in blockers):
            raise TypeError("blockers must contain Plan1Blocker values")
        object.__setattr__(self, "customize_ids", ids)
        object.__setattr__(self, "customize_count_list", counts)
        object.__setattr__(self, "payment_modifiers", payment)
        object.__setattr__(self, "effect_modifiers", effects)
        object.__setattr__(self, "blockers", blockers)

    @property
    def customize_counts(self) -> tuple[int, ...]:
        return self.customize_count_list

    @property
    def active_customize_ids(self) -> tuple[str, ...]:
        return tuple(
            customize_id
            for customize_id, count in zip(
                self.customize_ids, self.customize_count_list, strict=True
            )
            if count > 0
        )

    @property
    def payment(self) -> tuple[Plan1PaymentModifier, ...]:
        """Short alias for bridge code projecting payment modifiers."""

        return self.payment_modifiers

    @property
    def effects(self) -> tuple[Plan1EffectModifier, ...]:
        return self.effect_modifiers

    @property
    def payment_cost_reduction(self) -> int:
        return sum(value.reduction for value in self.payment_modifiers)

    @property
    def executable(self) -> bool:
        return not self.blockers


# Keep a descriptive spelling available for callers that treat the returned
# value as a compilation artifact rather than a mutable runtime object.
Plan1CardCustomization = Plan1RuntimeCustomization
Plan1CompiledCustomization = Plan1RuntimeCustomization
Plan1CardRuntimeCustomization = Plan1RuntimeCustomization


CardInstanceInput: TypeAlias = NativeOrderedCardInstance | LocalSaveExamCard


def _blocker(code: str, source_id: str, detail: str = "") -> Plan1Blocker:
    return Plan1Blocker(code, source_id, detail)


def _as_int(value: object, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise Plan1CustomizationError(
            "plan1-customization-master-shape-unsupported",
            f"{label}={value!r}",
        )
    return value


def _mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise Plan1CustomizationError(
            "plan1-customization-master-shape-unsupported", label
        )
    return value


def _yaml_rows(path: Path, *, source: str) -> tuple[Mapping[str, object], ...]:
    if not path.is_file():
        raise Plan1CustomizationError("plan1-customization-master-missing", source)
    try:
        loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        payload = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise Plan1CustomizationError(
            "plan1-customization-master-invalid", f"{source}:{error}"
        ) from error
    if not isinstance(payload, list) or not all(
        isinstance(row, Mapping) for row in payload
    ):
        raise Plan1CustomizationError("plan1-customization-master-invalid", source)
    return tuple(payload)


@lru_cache(maxsize=16)
def _customization_master_indexes(
    master_dir: Path,
) -> tuple[dict[tuple[str, int], Mapping[str, object]], dict[str, Mapping[str, object]]]:
    """Load official Customize and GrowEffect rows once per Master directory."""

    master_dir = Path(master_dir)
    custom_index: dict[tuple[str, int], Mapping[str, object]] = {}
    for row in _yaml_rows(
        master_dir / "ProduceCardCustomize.yaml",
        source="ProduceCardCustomize.yaml",
    ):
        row_id = row.get("id")
        count = row.get("customizeCount")
        if not isinstance(row_id, str) or not row_id:
            raise Plan1CustomizationError(
                "plan1-customization-master-shape-unsupported",
                "ProduceCardCustomize.id",
            )
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise Plan1CustomizationError(
                "plan1-customization-master-shape-unsupported",
                f"{row_id}:customizeCount",
            )
        key = (row_id, count)
        if key in custom_index:
            raise Plan1CustomizationError(
                "plan1-customization-master-duplicate",
                f"{row_id}:{count}",
            )
        custom_index[key] = row

    grow_index: dict[str, Mapping[str, object]] = {}
    for row in _yaml_rows(
        master_dir / "ProduceCardGrowEffect.yaml",
        source="ProduceCardGrowEffect.yaml",
    ):
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id:
            raise Plan1CustomizationError(
                "plan1-customization-master-shape-unsupported",
                "ProduceCardGrowEffect.id",
            )
        if row_id in grow_index:
            raise Plan1CustomizationError(
                "plan1-customization-master-duplicate", row_id
            )
        grow_index[row_id] = row
    return custom_index, grow_index


def _card_master_customization_ids(
    card_id: str,
    upgrade: int,
    *,
    database: Path,
) -> tuple[str, ...]:
    try:
        with sqlite3.connect(Path(database)) as connection:
            row = connection.execute(
                "SELECT raw_json FROM card WHERE id = ? AND upgrade_count = ?",
                (card_id, upgrade),
            ).fetchone()
    except sqlite3.Error as error:
        raise Plan1CustomizationError(
            "plan1-customization-card-master-unavailable",
            f"{card_id}@{upgrade}:{error}",
        ) from error
    if row is None:
        raise Plan1CustomizationError(
            "plan1-customization-card-master-missing", f"{card_id}@{upgrade}"
        )
    try:
        raw = json.loads(row[0])
    except (TypeError, ValueError) as error:
        raise Plan1CustomizationError(
            "plan1-customization-card-master-invalid", f"{card_id}@{upgrade}"
        ) from error
    raw = _mapping(raw, label=f"{card_id}@{upgrade}:raw_json")
    value = raw.get("produceCardCustomizeIds")
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise Plan1CustomizationError(
            "plan1-customization-card-master-shape-unsupported",
            f"{card_id}@{upgrade}:produceCardCustomizeIds",
        )
    ids = tuple(value)
    if len(set(ids)) != len(ids):
        raise Plan1CustomizationError(
            "plan1-customization-card-master-shape-unsupported",
            f"{card_id}@{upgrade}:duplicate-customize-id",
        )
    return ids


def _runtime_counts(
    runtime: LocalSaveExamCardRuntimeState,
    *,
    card_id: str,
) -> tuple[int, ...]:
    if not isinstance(runtime, LocalSaveExamCardRuntimeState):
        raise TypeError("runtime must be LocalSaveExamCardRuntimeState")
    value = runtime.customize_count_list.to_value()
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0
        for item in value
    ):
        raise Plan1CustomizationError(
            "plan1-customization-count-list-invalid", card_id
        )
    return tuple(value)


def _require_exact_neutral(
    row: Mapping[str, object],
    *,
    grow_effect_id: str,
) -> None:
    expected = {
        "costType": _UNKNOWN_COST,
        "playProduceExamTriggerId": "",
        "playEffectProduceExamTriggerId": "",
        "targetPlayEffectProduceExamTriggerIds": [],
        "playProduceExamEffectId": "",
        "targetPlayProduceExamEffectIds": [],
        "produceCardStatusEnchantId": "",
        "playMovePositionType": _UNKNOWN_MOVE,
        "effectGroupIds": [],
    }
    for field, expected_value in expected.items():
        value = row.get(field)
        if isinstance(expected_value, list):
            valid = isinstance(value, list) and value == expected_value
        else:
            valid = value == expected_value
        if not valid:
            raise Plan1CustomizationError(
                "plan1-customization-grow-shape-unsupported",
                f"{grow_effect_id}:{field}",
            )


def _compile_grow_payment_modifier(
    customize_id: str,
    grow_effect_id: str,
    row: Mapping[str, object],
    *,
    card_id: str,
    card_cost_type: str,
    card_stamina_cost: int,
    card_force_stamina_cost: int,
    master_dir: Path,
) -> Plan1PaymentModifier:
    effect_type = row.get("effectType")
    if effect_type not in SUPPORTED_PLAN1_CUSTOMIZATION_GROW_TYPES:
        raise Plan1CustomizationError(
            "plan1-customization-grow-type-unsupported",
            f"{grow_effect_id}:{effect_type!r}",
        )
    value = _as_int(row.get("value"), label=f"{grow_effect_id}:value", minimum=1)
    _require_exact_neutral(row, grow_effect_id=grow_effect_id)
    expected_cost_type = {
        GROW_TYPE_COST_REDUCE: COST_STAMINA,
        GROW_TYPE_COST_PARAMETER_BUFF_REDUCE: COST_PARAMETER_BUFF,
        GROW_TYPE_COST_LESSON_BUFF_REDUCE: COST_LESSON_BUFF,
    }[effect_type]
    if card_cost_type != expected_cost_type:
        raise Plan1CustomizationError(
            "plan1-customization-cost-type-mismatch",
            f"{card_id}:{card_cost_type}:{expected_cost_type}",
        )
    if effect_type == GROW_TYPE_COST_REDUCE and not (
        card_stamina_cost > 0 and card_force_stamina_cost == 0
    ):
        raise Plan1CustomizationError(
            "plan1-customization-cost-target-count-invalid",
            f"{card_id}:ordinary={card_stamina_cost}:"
            f"force={card_force_stamina_cost}",
        )
    # ``master_dir`` is part of the function boundary to keep the compiler's
    # provenance explicit even though all row validation is performed above.
    _ = master_dir
    return Plan1PaymentModifier(
        customize_id=customize_id,
        grow_effect_id=grow_effect_id,
        cost_type=expected_cost_type,
        reduction=value,
    )


def _compile_customization(
    card_id: str,
    upgrade: int,
    runtime: LocalSaveExamCardRuntimeState,
    *,
    card_cost_type: str,
    card_stamina_cost: int,
    card_force_stamina_cost: int,
    database: Path,
    master_dir: Path,
) -> Plan1RuntimeCustomization:
    counts = _runtime_counts(runtime, card_id=card_id)
    ids = _card_master_customization_ids(card_id, upgrade, database=database)
    if not counts:
        # An explicitly captured empty list has no applied customization.
        # Temporary Hand-All Upgrade can expose new Master customization
        # choices without installing any of them into this card instance.
        # Do not invent counters for those choices or discard active counts.
        return Plan1RuntimeCustomization(card_id, upgrade, (), ())
    if len(counts) != len(ids):
        raise Plan1CustomizationError(
            "plan1-customization-lineage-mismatch",
            f"{card_id}@{upgrade}:counts={len(counts)}:ids={len(ids)}",
        )
    custom_index, grow_index = _customization_master_indexes(Path(master_dir))
    payment_modifiers: list[Plan1PaymentModifier] = []
    effect_modifiers: list[Plan1EffectModifier] = []
    for customize_id, count in zip(ids, counts, strict=True):
        if count == 0:
            continue
        customize_row = custom_index.get((customize_id, count))
        if customize_row is None:
            raise Plan1CustomizationError(
                "plan1-customization-row-missing",
                f"{customize_id}:count={count}",
            )
        if customize_row.get("id") != customize_id:
            raise Plan1CustomizationError(
                "plan1-customization-row-shape-unsupported", customize_id
            )
        if customize_row.get("customizeCount") != count:
            raise Plan1CustomizationError(
                "plan1-customization-count-mismatch",
                f"{customize_id}:runtime={count}:master={customize_row.get('customizeCount')!r}",
            )
        overwrite = customize_row.get("overwriteProduceCardGrowEffectType")
        if not isinstance(overwrite, str) or not overwrite:
            raise Plan1CustomizationError(
                "plan1-customization-row-shape-unsupported",
                f"{customize_id}:overwriteProduceCardGrowEffectType",
            )
        if overwrite != _UNKNOWN:
            raise Plan1CustomizationError(
                "plan1-customization-row-shape-unsupported",
                f"{customize_id}:overwrite={overwrite}",
            )
        description = customize_row.get("description")
        if not isinstance(description, str):
            raise Plan1CustomizationError(
                "plan1-customization-row-shape-unsupported",
                f"{customize_id}:description",
            )
        _as_int(
            customize_row.get("producePoint"),
            label=f"{customize_id}:producePoint",
            minimum=0,
        )
        grow_ids = customize_row.get("produceCardGrowEffectIds")
        if not isinstance(grow_ids, list) or not grow_ids or any(
            not isinstance(value, str) or not value for value in grow_ids
        ):
            raise Plan1CustomizationError(
                "plan1-customization-grow-list-invalid", customize_id
            )
        # The two cost-reducer rows are one-grow rows.  A selected multi-grow
        # row is not partially consumed because doing so would change native
        # ordered semantics and hide the unsupported effect.
        for grow_effect_id in grow_ids:
            grow_row = grow_index.get(grow_effect_id)
            if grow_row is None:
                raise Plan1CustomizationError(
                    "plan1-customization-grow-row-missing", grow_effect_id
                )
            grow_type = grow_row.get("effectType")
            if grow_type in {
                GROW_TYPE_COST_REDUCE,
                GROW_TYPE_COST_PARAMETER_BUFF_REDUCE,
                GROW_TYPE_COST_LESSON_BUFF_REDUCE,
            }:
                payment_modifiers.append(
                    _compile_grow_payment_modifier(
                        customize_id,
                        grow_effect_id,
                        grow_row,
                        card_id=card_id,
                        card_cost_type=card_cost_type,
                        card_stamina_cost=card_stamina_cost,
                        card_force_stamina_cost=card_force_stamina_cost,
                        master_dir=Path(master_dir),
                    )
                )
            elif grow_type in {
                GROW_TYPE_LESSON_BUFF_ADD,
                GROW_TYPE_LESSON_ADD,
                GROW_TYPE_LESSON_COUNT_ADD,
            }:
                value = _as_int(
                    grow_row.get("value"),
                    label=f"{grow_effect_id}:value",
                    minimum=1,
                )
                _require_exact_neutral(
                    grow_row,
                    grow_effect_id=grow_effect_id,
                )
                effect_modifiers.append(
                    Plan1EffectModifier(
                        customize_id=customize_id,
                        grow_effect_id=grow_effect_id,
                        effect_type=grow_type,
                        value=value,
                    )
                )
            else:
                raise Plan1CustomizationError(
                    "plan1-customization-grow-type-unsupported",
                    f"{grow_effect_id}:{grow_type!r}",
                )
    return Plan1RuntimeCustomization(
        card_id,
        upgrade,
        ids,
        counts,
        tuple(payment_modifiers),
        tuple(effect_modifiers),
    )


def _blocked_customization(
    card_id: str,
    upgrade: int,
    counts: tuple[int, ...],
    error: Plan1CustomizationError,
    *,
    database: Path,
) -> Plan1RuntimeCustomization:
    # Keep IDs when the card row is available; this makes a blocked program
    # inspectable by the bridge while preserving an atomic no-op result.
    try:
        ids = _card_master_customization_ids(card_id, upgrade, database=database)
    except (Plan1CustomizationError, sqlite3.Error):
        ids = ()
    if len(counts) != len(ids):
        ids = ()
        counts = ()
    return Plan1RuntimeCustomization(
        card_id,
        upgrade,
        ids,
        counts,
        blockers=(_blocker(error.code, card_id, error.detail),),
    )


def compile_plan1_card_customization(
    card_id: str,
    upgrade: int,
    runtime: LocalSaveExamCardRuntimeState,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> Plan1RuntimeCustomization:
    """Compile one instance's ordered customization into typed modifiers.

    This public convenience function is fail-closed: malformed/unknown
    customization returns a ``Plan1RuntimeCustomization`` carrying a stable
    ``Plan1Blocker`` rather than raising.  Use
    :func:`parse_plan1_runtime_customization_ordered` when exception-style
    strict parsing is preferred.
    """

    if not isinstance(card_id, str) or not card_id:
        raise ValueError("card_id must be non-empty text")
    if isinstance(upgrade, bool) or not isinstance(upgrade, int) or upgrade < 0:
        raise ValueError("upgrade must be a non-negative integer")
    if not isinstance(runtime, LocalSaveExamCardRuntimeState):
        raise TypeError("runtime must be LocalSaveExamCardRuntimeState")
    database = Path(database)
    master_dir = Path(master_dir)
    try:
        # Read the static card once so card cost-family alignment is based on
        # the normalized Master row rather than a card-id convention.
        card = compile_plan1_card(card_id, upgrade, database=database)
        counts = _runtime_counts(runtime, card_id=card_id)
        return _compile_customization(
            card_id,
            upgrade,
            runtime,
            card_cost_type=card.cost_type,
            card_stamina_cost=card.stamina_cost,
            card_force_stamina_cost=card.force_stamina_cost,
            database=database,
            master_dir=master_dir,
        )
    except Plan1CustomizationError as error:
        try:
            counts = _runtime_counts(runtime, card_id=card_id)
        except (Plan1CustomizationError, TypeError):
            counts = ()
        return _blocked_customization(
            card_id,
            upgrade,
            counts,
            error,
            database=database,
        )


def parse_plan1_runtime_customization_ordered(
    card_id: str,
    upgrade: int,
    runtime: LocalSaveExamCardRuntimeState,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> Plan1RuntimeCustomization:
    """Strict ordered parser; unsupported shapes raise a typed error."""

    result = compile_plan1_card_customization(
        card_id,
        upgrade,
        runtime,
        database=database,
        master_dir=master_dir,
    )
    if result.blockers:
        blocker = result.blockers[0]
        raise Plan1CustomizationError(blocker.code, blocker.detail)
    return result


def compile_plan1_card_instance(
    instance: CardInstanceInput | str,
    upgrade: int | None = None,
    runtime: LocalSaveExamCardRuntimeState | None = None,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> Plan1CompiledCard:
    """Compile a static Plan 1 card together with captured instance state.

    ``instance`` may be a ``NativeOrderedCardInstance`` or a
    ``LocalSaveExamCard``.  For bridge callers that already have separate
    identity fields, passing ``card_id`` as the first argument together with
    ``upgrade`` and ``runtime`` is also supported.  The returned card remains
    static-Master normalized and carries typed instance modifiers for the
    existing Plan 1 executor.
    """

    if isinstance(instance, NativeOrderedCardInstance):
        if upgrade is not None or runtime is not None:
            raise TypeError("upgrade/runtime must be omitted for card instances")
        card_id = instance.card_id
        card_upgrade = instance.effective_upgrade
        card_runtime = instance.runtime_state
    elif isinstance(instance, LocalSaveExamCard):
        if upgrade is not None or runtime is not None:
            raise TypeError("upgrade/runtime must be omitted for card instances")
        if instance.runtime_state is None:
            raise Plan1CustomizationError(
                "plan1-customization-runtime-missing", instance.card_id
            )
        card_id = instance.card_id
        card_upgrade = instance.effective_upgrade
        card_runtime = instance.runtime_state
    elif isinstance(instance, str):
        if upgrade is None or runtime is None:
            raise TypeError("card_id input requires upgrade and runtime")
        card_id = instance
        card_upgrade = upgrade
        card_runtime = runtime
    else:
        raise TypeError(
            "instance must be NativeOrderedCardInstance, LocalSaveExamCard, or card_id"
        )

    card = compile_plan1_card(card_id, card_upgrade, database=Path(database))
    customization = compile_plan1_card_customization(
        card_id,
        card_upgrade,
        card_runtime,
        database=Path(database),
        master_dir=Path(master_dir),
    )
    blockers = tuple(dict.fromkeys((*card.blockers, *customization.blockers)))
    if card.category == "ProduceCardCategory_Trouble" and not card.blockers:
        try:
            from .plan3_native_state import Plan3NativeCard
            from .plan3_sleepy_trouble import assert_exact_sleepy_instance
            if isinstance(instance, NativeOrderedCardInstance):
                observed = LocalSaveExamCard(instance.guid, instance.card_id, instance.base_upgrade,
                    instance.temporary_upgrade, instance.effective_upgrade, instance.support_upgrade_ids,
                    instance.fixed_deck_order, instance.runtime_state)
            elif isinstance(instance, LocalSaveExamCard):
                observed = instance
            else:
                raise ValueError("Trouble runtime validation requires a complete observed card instance")
            assert_exact_sleepy_instance(Plan3NativeCard.from_local_save(observed, database=Path(database), master_dir=Path(master_dir)))
        except (KeyError, OSError, TypeError, ValueError) as error:
            blockers = (*blockers, _blocker("plan1-trouble-runtime-unproved", card_id, str(error)))
    effects = card.effects
    for modifier in customization.effect_modifiers:
        target_effect_type = {
            GROW_TYPE_LESSON_BUFF_ADD: EFFECT_LESSON_BUFF,
            GROW_TYPE_LESSON_ADD: EFFECT_LESSON,
            GROW_TYPE_LESSON_COUNT_ADD: EFFECT_LESSON,
        }.get(modifier.effect_type)
        if target_effect_type is None:
            blockers = tuple(
                dict.fromkeys(
                    (
                        *blockers,
                        _blocker(
                            "plan1-customization-effect-runtime-unsupported",
                            modifier.grow_effect_id,
                            modifier.effect_type,
                        ),
                    )
                )
            )
            continue
        matches = tuple(
            index
            for index, effect in enumerate(effects)
            if effect.effect_type == target_effect_type
        )
        if len(matches) != 1:
            blockers = tuple(
                dict.fromkeys(
                    (
                        *blockers,
                        _blocker(
                            "plan1-customization-effect-target-missing",
                            modifier.grow_effect_id,
                            f"{target_effect_type}:matches={len(matches)}",
                        ),
                    )
                )
            )
            continue
        index = matches[0]
        updated = list(effects)
        if modifier.effect_type in {
            GROW_TYPE_LESSON_BUFF_ADD,
            GROW_TYPE_LESSON_ADD,
        }:
            updated[index] = replace(
                updated[index], value1=updated[index].value1 + modifier.value
            )
        else:
            updated[index] = replace(
                updated[index],
                effect_count=updated[index].effect_count + modifier.value,
            )
        effects = tuple(updated)
    return replace(
        card,
        effects=effects,
        blockers=blockers,
        customize_ids=customization.customize_ids,
        customize_count_list=customization.customize_count_list,
        payment_modifiers=customization.payment_modifiers,
        effect_modifiers=customization.effect_modifiers,
    )


def compile_plan1_card_for_instance(
    instance: CardInstanceInput | str,
    upgrade: int | None = None,
    runtime: LocalSaveExamCardRuntimeState | None = None,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> Plan1CompiledCard:
    """Readable alias for :func:`compile_plan1_card_instance`."""

    return compile_plan1_card_instance(
        instance,
        upgrade,
        runtime,
        database=database,
        master_dir=master_dir,
    )


def compile_plan1_card_from_local_save(
    card: LocalSaveExamCard,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> Plan1CompiledCard:
    """Compile one captured LocalSave card instance."""

    return compile_plan1_card_instance(
        card,
        database=database,
        master_dir=master_dir,
    )


__all__ = [
    "GROW_TYPE_COST_LESSON_BUFF_REDUCE",
    "GROW_TYPE_COST_PARAMETER_BUFF_REDUCE",
    "GROW_TYPE_COST_REDUCE",
    "GROW_TYPE_LESSON_BUFF_ADD",
    "PLAN1_CUSTOMIZATION_COST_LESSON_BUFF_REDUCE",
    "PLAN1_CUSTOMIZATION_COST_PARAMETER_BUFF_REDUCE",
    "SUPPORTED_PLAN1_CUSTOMIZATION_GROW_TYPES",
    "Plan1CustomizationError",
    "Plan1CardCustomization",
    "Plan1CardRuntimeCustomization",
    "Plan1CompiledCustomization",
    "Plan1RuntimeCustomization",
    "compile_plan1_card_customization",
    "compile_plan1_card_for_instance",
    "compile_plan1_card_from_local_save",
    "compile_plan1_card_instance",
    "parse_plan1_runtime_customization_ordered",
]
