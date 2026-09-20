"""Bounded evaluator for Plan 3 ``None`` counter-field triggers.

This module is intentionally separate from :mod:`plan3_engine`.  It models
only the four Android v3.2.3 field cases audited in the local Master database
and native dump.  The predicate reads an explicit immutable counter snapshot;
it never derives a counter from an event delta and never mutates state.

The Master rows use an empty ``field_status_check_types`` list for the native
Unknown check.  A ``Not`` tuple is accepted by the evaluator so its inversion
can be tested, but no current row in this catalog uses it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from .master_db import DEFAULT_DATABASE
from .plan3_engine import Plan3CardRef, Plan3State, Plan3Trigger


PHASE_NONE = "ProduceExamPhaseType_None"
CHECK_UNKNOWN = "ProduceExamTriggerCheckType_Unknown"
CHECK_NOT = "ProduceExamTriggerCheckType_Not"
PHASE_UNKNOWN_MOVE = "ProduceCardMovePositionType_Unknown"
LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"

FIELD_CONCENTRATION_CHANGE_COUNT_UP = (
    "ProduceExamFieldStatusType_ConcentrationChangeCountUp"
)
FIELD_PRESERVATION_CHANGE_COUNT_UP = (
    "ProduceExamFieldStatusType_PreservationChangeCountUp"
)
FIELD_FULL_POWER_CHANGE_COUNT_UP = (
    "ProduceExamFieldStatusType_FullPowerChangeCountUp"
)
FIELD_FULL_POWER_POINT_GET_SUM_UP = (
    "ProduceExamFieldStatusType_FullPowerPointGetSumUp"
)

SUPPORTED_COUNTER_FIELDS = (
    FIELD_CONCENTRATION_CHANGE_COUNT_UP,
    FIELD_PRESERVATION_CHANGE_COUNT_UP,
    FIELD_FULL_POWER_CHANGE_COUNT_UP,
    FIELD_FULL_POWER_POINT_GET_SUM_UP,
)

_FIELD_TO_COUNTER = {
    FIELD_CONCENTRATION_CHANGE_COUNT_UP: "concentration_change_count",
    FIELD_PRESERVATION_CHANGE_COUNT_UP: "preservation_change_count",
    FIELD_FULL_POWER_CHANGE_COUNT_UP: "full_power_change_count",
    FIELD_FULL_POWER_POINT_GET_SUM_UP: "full_power_point_get_sum",
}

_INT32_MIN = -(2**31)
_INT32_MAX = 2**31 - 1


class CounterTriggerResolution(str, Enum):
    """Whether a trigger has the exact audited row shape."""

    INDEPENDENT_RESOLVED = "independent-resolved"
    UNRESOLVED = "unresolved"


def _nonnegative_int32(value: object, label: str) -> int:
    if type(value) is not int or not 0 <= value <= _INT32_MAX:
        raise ValueError(f"{label} must be a non-negative Int32")
    return value


@dataclass(frozen=True, slots=True)
class Plan3CounterState:
    """The four counters read by the audited native field cases.

    ``full_power_point_get_sum`` is deliberately separate from current
    ``full_power_points``.  The Plan3State bridge maps it to
    ``full_power_points_total``, which is the persistent cumulative field.
    """

    concentration_change_count: int = 0
    preservation_change_count: int = 0
    full_power_change_count: int = 0
    full_power_point_get_sum: int = 0

    def __post_init__(self) -> None:
        for name in (
            "concentration_change_count",
            "preservation_change_count",
            "full_power_change_count",
            "full_power_point_get_sum",
        ):
            _nonnegative_int32(getattr(self, name), name)

    @classmethod
    def from_plan3_state(cls, state: Plan3State) -> "Plan3CounterState":
        """Build an explicit snapshot from the existing typed Plan3State.

        The evaluator itself still requires a ``Plan3CounterState`` argument;
        this bridge is only a visible, testable mapping at the boundary.
        """

        if not isinstance(state, Plan3State):
            raise TypeError("state must be Plan3State")
        return cls(
            concentration_change_count=state.concentration_change_count,
            preservation_change_count=state.preservation_change_count,
            full_power_change_count=state.full_power_change_count,
            full_power_point_get_sum=state.full_power_points_total,
        )

    def value_for(self, field_status_type: str) -> int:
        counter_name = _FIELD_TO_COUNTER.get(field_status_type)
        if counter_name is None:
            raise KeyError(field_status_type)
        return int(getattr(self, counter_name))


@dataclass(frozen=True, slots=True, order=True)
class CounterTriggerReference:
    """One exact card-version edge to a catalogued trigger row."""

    card_id: str
    upgrade: int
    plan_type: str
    source_kind: str
    source_index: int | None = None
    effect_id: str = ""
    is_once_play_effect: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.card_id, str) or not self.card_id:
            raise ValueError("card_id must be non-empty text")
        if type(self.upgrade) is not int or self.upgrade < 0:
            raise ValueError("upgrade must be a non-negative integer")
        if not isinstance(self.plan_type, str) or not self.plan_type:
            raise ValueError("plan_type must be non-empty text")
        if self.source_kind not in {
            "card-play-trigger",
            "direct-effect-trigger",
        }:
            raise ValueError(f"unknown trigger source kind: {self.source_kind}")
        if self.source_kind == "direct-effect-trigger":
            if type(self.source_index) is not int or self.source_index < 0:
                raise ValueError("direct effect source_index must be non-negative")
            if not self.effect_id:
                raise ValueError("direct effect reference needs effect_id")
            if type(self.is_once_play_effect) is not bool:
                raise ValueError("direct effect reference needs once boolean")
        elif self.source_index is not None:
            raise ValueError("card-play reference cannot have source_index")

    @property
    def card_version(self) -> Plan3CardRef:
        return Plan3CardRef(self.card_id, self.upgrade)

    @property
    def version(self) -> str:
        return f"{self.card_id}+{self.upgrade}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "plan_type": self.plan_type,
            "source_kind": self.source_kind,
            "source_index": self.source_index,
            "effect_id": self.effect_id,
            "is_once_play_effect": self.is_once_play_effect,
        }


@dataclass(frozen=True, slots=True)
class CounterTriggerCatalogRow:
    """A typed projection of one exact Master trigger row and its card edges."""

    trigger: Plan3Trigger
    counter_name: str
    references: tuple[CounterTriggerReference, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.trigger, Plan3Trigger):
            raise TypeError("trigger must be Plan3Trigger")
        trigger_field = (
            self.trigger.field_types[0] if self.trigger.field_types else ""
        )
        if _FIELD_TO_COUNTER.get(trigger_field) != self.counter_name:
            raise ValueError("counter_name does not match trigger field")
        object.__setattr__(self, "references", tuple(self.references))

    @property
    def id(self) -> str:
        return self.trigger.id

    @property
    def field_status_type(self) -> str:
        return self.trigger.field_types[0]

    @property
    def threshold(self) -> int:
        return self.trigger.field_values[0]

    @property
    def affected_card_versions(self) -> tuple[Plan3CardRef, ...]:
        return tuple(
            sorted(
                {item.card_version for item in self.references},
                key=lambda item: (item.card_id, item.upgrade),
            )
        )

    @property
    def affected_card_version_count(self) -> int:
        return len(self.affected_card_versions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "phase_types": list(self.trigger.phase_types),
            "phase_values": list(self.trigger.phase_values),
            "field_check_types": list(self.trigger.field_check_types),
            "field_types": list(self.trigger.field_types),
            "field_values": list(self.trigger.field_values),
            "field_status_type": self.field_status_type,
            "counter_name": self.counter_name,
            "threshold": self.threshold,
            "affected_card_version_count": self.affected_card_version_count,
            "affected_card_versions": [
                {"card_id": item.card_id, "upgrade": item.upgrade}
                for item in self.affected_card_versions
            ],
            "references": [item.to_dict() for item in self.references],
        }


@dataclass(frozen=True, slots=True)
class CounterTriggerCatalog:
    """The complete bounded inventory for the four requested fields."""

    rows: tuple[CounterTriggerCatalogRow, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(self.rows))
        ids = [item.id for item in self.rows]
        if len(ids) != len(set(ids)):
            raise ValueError("counter trigger catalog IDs must be unique")

    @property
    def by_id(self) -> dict[str, CounterTriggerCatalogRow]:
        return {item.id: item for item in self.rows}

    @property
    def referenced_rows(self) -> tuple[CounterTriggerCatalogRow, ...]:
        return tuple(item for item in self.rows if item.references)

    @property
    def affected_card_versions(self) -> tuple[Plan3CardRef, ...]:
        return tuple(
            sorted(
                {
                    version
                    for row in self.rows
                    for version in row.affected_card_versions
                },
                key=lambda item: (item.card_id, item.upgrade),
            )
        )

    @property
    def affected_card_version_count(self) -> int:
        return len(self.affected_card_versions)

    @property
    def reference_count(self) -> int:
        return sum(len(row.references) for row in self.rows)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger_row_count": len(self.rows),
            "referenced_trigger_row_count": len(self.referenced_rows),
            "reference_count": self.reference_count,
            "affected_card_version_count": self.affected_card_version_count,
            "rows": [item.to_dict() for item in self.rows],
        }


def _master_trigger(trigger_id: str, field: str, threshold: int) -> Plan3Trigger:
    return Plan3Trigger(
        id=trigger_id,
        phase_types=(PHASE_NONE,),
        field_types=(field,),
        field_values=(threshold,),
        card_move_position_type=PHASE_UNKNOWN_MOVE,
        lesson_type=LESSON_UNKNOWN,
    )


def _card_play_references(
    card_id: str, upgrades: Iterable[int], plan_type: str = "ProducePlanType_Plan3"
) -> tuple[CounterTriggerReference, ...]:
    return tuple(
        CounterTriggerReference(
            card_id=card_id,
            upgrade=upgrade,
            plan_type=plan_type,
            source_kind="card-play-trigger",
        )
        for upgrade in upgrades
    )


def _direct_reference(
    card_id: str,
    upgrade: int,
    source_index: int,
    *,
    plan_type: str = "ProducePlanType_Plan3",
) -> CounterTriggerReference:
    return CounterTriggerReference(
        card_id=card_id,
        upgrade=upgrade,
        plan_type=plan_type,
        source_kind="direct-effect-trigger",
        source_index=source_index,
        effect_id="e_effect-exam_extra_turn",
        is_once_play_effect=False,
    )


_STATIC_ROWS = (
    CounterTriggerCatalogRow(
        _master_trigger(
            "e_trigger-none-concentration_change_count_up-2",
            FIELD_CONCENTRATION_CHANGE_COUNT_UP,
            2,
        ),
        "concentration_change_count",
        (_direct_reference("p_card-03-men-100_016", 0, 3),),
    ),
    CounterTriggerCatalogRow(
        _master_trigger(
            "e_trigger-none-concentration_change_count_up-3",
            FIELD_CONCENTRATION_CHANGE_COUNT_UP,
            3,
        ),
        "concentration_change_count",
    ),
    CounterTriggerCatalogRow(
        _master_trigger(
            "e_trigger-none-full_power_change_count_up-1",
            FIELD_FULL_POWER_CHANGE_COUNT_UP,
            1,
        ),
        "full_power_change_count",
        _card_play_references("p_card-03-ido-3_106", range(4)),
    ),
    CounterTriggerCatalogRow(
        _master_trigger(
            "e_trigger-none-full_power_change_count_up-2",
            FIELD_FULL_POWER_CHANGE_COUNT_UP,
            2,
        ),
        "full_power_change_count",
        (_direct_reference("p_card-03-men-100_016", 0, 4),),
    ),
    CounterTriggerCatalogRow(
        _master_trigger(
            "e_trigger-none-full_power_point_get_sum_up-10",
            FIELD_FULL_POWER_POINT_GET_SUM_UP,
            10,
        ),
        "full_power_point_get_sum",
        _card_play_references("p_card-03-sup-3_250", range(4)),
    ),
    CounterTriggerCatalogRow(
        _master_trigger(
            "e_trigger-none-full_power_point_get_sum_up-5",
            FIELD_FULL_POWER_POINT_GET_SUM_UP,
            5,
        ),
        "full_power_point_get_sum",
        _card_play_references("p_card-03-act-3_189", range(4)),
    ),
    CounterTriggerCatalogRow(
        _master_trigger(
            "e_trigger-none-preservation_change_count_up-1",
            FIELD_PRESERVATION_CHANGE_COUNT_UP,
            1,
        ),
        "preservation_change_count",
    ),
    CounterTriggerCatalogRow(
        _master_trigger(
            "e_trigger-none-preservation_change_count_up-2",
            FIELD_PRESERVATION_CHANGE_COUNT_UP,
            2,
        ),
        "preservation_change_count",
        (
            *_card_play_references("p_card-03-ido-3_197", range(4)),
            _direct_reference("p_card-03-men-100_016", 0, 2),
            *_card_play_references("p_card-03-men-2_074", range(4)),
        ),
    ),
)

COUNTER_TRIGGER_CATALOG = CounterTriggerCatalog(_STATIC_ROWS)
COUNTER_TRIGGER_ROWS = tuple(item.trigger for item in COUNTER_TRIGGER_CATALOG.rows)
COUNTER_TRIGGER_BY_ID = COUNTER_TRIGGER_CATALOG.by_id
AFFECTED_CARD_VERSIONS = COUNTER_TRIGGER_CATALOG.affected_card_versions
AFFECTED_CARD_VERSION_COUNT = COUNTER_TRIGGER_CATALOG.affected_card_version_count


@dataclass(frozen=True, slots=True)
class Plan3CounterTriggerContract:
    """Resolved shape and native counter mapping for one trigger."""

    trigger: Plan3Trigger
    resolution: CounterTriggerResolution
    counter_name: str | None = None
    unresolved: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "resolution", CounterTriggerResolution(self.resolution))
        object.__setattr__(self, "unresolved", tuple(self.unresolved))

    @property
    def supported(self) -> bool:
        return (
            self.resolution is CounterTriggerResolution.INDEPENDENT_RESOLVED
            and not self.unresolved
        )

    @property
    def field_status_type(self) -> str | None:
        if not self.trigger.field_types:
            return None
        return self.trigger.field_types[0]

    @property
    def threshold(self) -> int | None:
        if not self.trigger.field_values:
            return None
        return self.trigger.field_values[0]


def _shape_errors(trigger: Plan3Trigger) -> tuple[str, ...]:
    expected = COUNTER_TRIGGER_BY_ID.get(trigger.id)
    if expected is None:
        return (f"unknown-counter-trigger-row:{trigger.id}",)
    baseline = expected.trigger
    errors: list[str] = []
    if trigger.phase_types != baseline.phase_types:
        errors.append(f"phase-types:{trigger.id}")
    if trigger.phase_values != baseline.phase_values:
        errors.append(f"phase-values:{trigger.id}")
    if trigger.field_types != baseline.field_types:
        errors.append(f"field-types:{trigger.id}")
    if trigger.field_values != baseline.field_values:
        errors.append(f"field-values:{trigger.id}")
    if trigger.field_card_search_ids != baseline.field_card_search_ids:
        errors.append(f"field-card-search:{trigger.id}")
    if trigger.produce_card_search_id != baseline.produce_card_search_id:
        errors.append(f"card-search:{trigger.id}")
    if trigger.upper_search_count != baseline.upper_search_count:
        errors.append(f"upper-search-count:{trigger.id}")
    if trigger.lower_search_count != baseline.lower_search_count:
        errors.append(f"lower-search-count:{trigger.id}")
    if trigger.card_move_position_type != baseline.card_move_position_type:
        errors.append(f"card-move-position:{trigger.id}")
    if trigger.effect_types != baseline.effect_types:
        errors.append(f"effect-types:{trigger.id}")
    if trigger.lesson_type != baseline.lesson_type:
        errors.append(f"lesson-type:{trigger.id}")
    if trigger.field_check_types not in ((), (CHECK_NOT,)):
        errors.append(f"field-check-types:{trigger.id}")
    return tuple(errors)


def resolve_plan3_counter_trigger(
    trigger: Plan3Trigger,
) -> Plan3CounterTriggerContract:
    """Resolve only the eight exact local rows; unknown shapes stay unresolved."""

    if not isinstance(trigger, Plan3Trigger):
        return Plan3CounterTriggerContract(
            trigger=Plan3Trigger(id="<invalid>", phase_types=()),
            resolution=CounterTriggerResolution.UNRESOLVED,
            unresolved=("typed-plan3-trigger-unavailable",),
        )
    errors = _shape_errors(trigger)
    expected = COUNTER_TRIGGER_BY_ID.get(trigger.id)
    return Plan3CounterTriggerContract(
        trigger=trigger,
        resolution=(
            CounterTriggerResolution.INDEPENDENT_RESOLVED
            if not errors
            else CounterTriggerResolution.UNRESOLVED
        ),
        counter_name=expected.counter_name if expected is not None else None,
        unresolved=errors,
    )


@dataclass(frozen=True, slots=True)
class CounterTriggerDecision:
    """Pure result; ``fires=None`` means the input was not proven."""

    contract: Plan3CounterTriggerContract
    event_phase: object
    supported: bool
    fires: bool | None
    counter_value: int | None = None
    threshold: int | None = None
    inverted: bool = False
    unresolved: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "unresolved", tuple(self.unresolved))


def evaluate_plan3_counter_trigger(
    trigger: Plan3Trigger,
    counters: Plan3CounterState,
    *,
    event_phase: str,
) -> CounterTriggerDecision:
    """Evaluate one ``None`` counter trigger against explicit state.

    The native predicate is ``current >= field_status_value``.  The empty
    check list keeps that result; the observed ``Not`` check inverts it to
    ``current < field_status_value``.  ``event_phase`` is intentionally
    explicit: the ``None`` token is a concrete dispatch phase, not a wildcard.
    """

    contract = resolve_plan3_counter_trigger(trigger)
    errors = list(contract.unresolved)
    if event_phase != PHASE_NONE:
        errors.append(f"event-phase-not-none:{event_phase!r}")
    if not isinstance(counters, Plan3CounterState):
        errors.append("typed-counter-state-unavailable")
    if errors:
        return CounterTriggerDecision(
            contract=contract,
            event_phase=event_phase,
            supported=False,
            fires=None,
            inverted=trigger.field_check_types == (CHECK_NOT,)
            if isinstance(trigger, Plan3Trigger)
            else False,
            unresolved=tuple(dict.fromkeys(errors)),
        )

    assert contract.counter_name is not None
    assert contract.field_status_type is not None
    assert contract.threshold is not None
    current = counters.value_for(contract.field_status_type)
    base_result = current >= contract.threshold
    inverted = trigger.field_check_types == (CHECK_NOT,)
    return CounterTriggerDecision(
        contract=contract,
        event_phase=event_phase,
        supported=True,
        fires=(not base_result) if inverted else base_result,
        counter_value=current,
        threshold=contract.threshold,
        inverted=inverted,
    )


def fires(
    trigger: Plan3Trigger,
    counters: Plan3CounterState,
    *,
    event_phase: str,
) -> bool | None:
    """Small pure convenience wrapper around the typed decision."""

    return evaluate_plan3_counter_trigger(
        trigger, counters, event_phase=event_phase
    ).fires


def _json_tuple(raw: object, label: str) -> tuple[Any, ...]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid Master JSON: {label}") from exc
    if not isinstance(value, list):
        raise ValueError(f"Master JSON must be a list: {label}")
    return tuple(value)


def load_master_counter_trigger_rows(
    database: Path = DEFAULT_DATABASE,
) -> tuple[Plan3Trigger, ...]:
    """Load every exact local ``None`` row in the four-field scope."""

    rows: list[Plan3Trigger] = []
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        for row in connection.execute(
            """
            SELECT id, phase_types_json, phase_values_json,
                   field_status_check_types_json, field_status_types_json,
                   field_status_values_json,
                   field_status_produce_card_search_ids_json,
                   produce_card_search_id, upper_search_count,
                   lower_search_count, card_move_position_type,
                   effect_types_json, lesson_type
              FROM produce_exam_trigger
            """
        ):
            phase_types = _json_tuple(row["phase_types_json"], f"{row['id']}:phase")
            field_types = _json_tuple(row["field_status_types_json"], f"{row['id']}:field")
            if phase_types != (PHASE_NONE,) or len(field_types) != 1:
                continue
            if field_types[0] not in SUPPORTED_COUNTER_FIELDS:
                continue
            field_values = _json_tuple(
                row["field_status_values_json"], f"{row['id']}:field-value"
            )
            rows.append(
                Plan3Trigger(
                    id=str(row["id"]),
                    phase_types=phase_types,
                    phase_values=tuple(
                        int(value)
                        for value in _json_tuple(
                            row["phase_values_json"], f"{row['id']}:phase-value"
                        )
                    ),
                    field_check_types=tuple(
                        str(value)
                        for value in _json_tuple(
                            row["field_status_check_types_json"],
                            f"{row['id']}:check",
                        )
                    ),
                    field_types=tuple(str(value) for value in field_types),
                    field_values=tuple(int(value) for value in field_values),
                    field_card_search_ids=tuple(
                        str(value)
                        for value in _json_tuple(
                            row["field_status_produce_card_search_ids_json"],
                            f"{row['id']}:field-search",
                        )
                    ),
                    produce_card_search_id=str(row["produce_card_search_id"]),
                    upper_search_count=int(row["upper_search_count"]),
                    lower_search_count=int(row["lower_search_count"]),
                    card_move_position_type=str(row["card_move_position_type"]),
                    effect_types=tuple(
                        str(value)
                        for value in _json_tuple(
                            row["effect_types_json"], f"{row['id']}:effect-types"
                        )
                    ),
                    lesson_type=str(row["lesson_type"]),
                )
            )
    return tuple(sorted(rows, key=lambda item: item.id))


def load_master_counter_trigger_references(
    database: Path = DEFAULT_DATABASE,
) -> tuple[tuple[str, CounterTriggerReference], ...]:
    """Load card-play and direct-effect edges to the exact target rows."""

    target_ids = set(COUNTER_TRIGGER_BY_ID)
    references: list[tuple[str, CounterTriggerReference]] = []
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        for row in connection.execute(
            """
            SELECT id, upgrade_count, plan_type, play_trigger_id,
                   play_effects_json
              FROM card
            """
        ):
            card_id = str(row["id"])
            upgrade = int(row["upgrade_count"])
            plan_type = str(row["plan_type"])
            play_trigger_id = str(row["play_trigger_id"])
            if play_trigger_id in target_ids:
                reference = CounterTriggerReference(
                    card_id=card_id,
                    upgrade=upgrade,
                    plan_type=plan_type,
                    source_kind="card-play-trigger",
                )
                references.append((play_trigger_id, reference))
            effects = _json_tuple(row["play_effects_json"], f"{card_id}+{upgrade}:effects")
            for index, raw_effect in enumerate(effects):
                if not isinstance(raw_effect, dict):
                    raise ValueError(f"card effect must be an object: {card_id}+{upgrade}")
                trigger_id = raw_effect.get("produceExamTriggerId", "")
                if trigger_id not in target_ids:
                    continue
                once = raw_effect.get("isOncePlayEffect", False)
                if type(once) is not bool:
                    raise ValueError(f"card effect once flag is invalid: {card_id}+{upgrade}")
                effect_id = raw_effect.get("produceExamEffectId", "")
                if not isinstance(effect_id, str) or not effect_id:
                    raise ValueError(f"card effect ID is invalid: {card_id}+{upgrade}")
                effect_row = connection.execute(
                    "SELECT id FROM effect WHERE id = ?", (effect_id,)
                ).fetchone()
                if effect_row is None:
                    raise KeyError(f"Master effect not found: {effect_id}")
                reference = CounterTriggerReference(
                    card_id=card_id,
                    upgrade=upgrade,
                    plan_type=plan_type,
                    source_kind="direct-effect-trigger",
                    source_index=index,
                    effect_id=effect_id,
                    is_once_play_effect=once,
                )
                references.append((str(trigger_id), reference))
    return tuple(
        sorted(
            references,
            key=lambda item: (
                item[0],
                item[1].card_id,
                item[1].upgrade,
                item[1].source_kind,
                item[1].source_index if item[1].source_index is not None else -1,
            ),
        )
    )


def load_counter_trigger_catalog(
    database: Path = DEFAULT_DATABASE,
) -> CounterTriggerCatalog:
    """Join the local Master rows and their actual card-version references."""

    rows = load_master_counter_trigger_rows(database)
    references: dict[str, list[CounterTriggerReference]] = {}
    for trigger_id, reference in load_master_counter_trigger_references(database):
        references.setdefault(trigger_id, []).append(reference)
    catalog_rows: list[CounterTriggerCatalogRow] = []
    for trigger in rows:
        counter_name = _FIELD_TO_COUNTER.get(
            trigger.field_types[0] if trigger.field_types else ""
        )
        if counter_name is None:
            raise ValueError(f"unsupported counter field in Master: {trigger.id}")
        catalog_rows.append(
            CounterTriggerCatalogRow(
                trigger=trigger,
                counter_name=counter_name,
                references=tuple(references.get(trigger.id, ())),
            )
        )
    return CounterTriggerCatalog(tuple(catalog_rows))


__all__ = [
    "AFFECTED_CARD_VERSION_COUNT",
    "AFFECTED_CARD_VERSIONS",
    "CHECK_NOT",
    "CHECK_UNKNOWN",
    "COUNTER_TRIGGER_BY_ID",
    "COUNTER_TRIGGER_CATALOG",
    "COUNTER_TRIGGER_ROWS",
    "FIELD_CONCENTRATION_CHANGE_COUNT_UP",
    "FIELD_FULL_POWER_CHANGE_COUNT_UP",
    "FIELD_FULL_POWER_POINT_GET_SUM_UP",
    "FIELD_PRESERVATION_CHANGE_COUNT_UP",
    "PHASE_NONE",
    "Plan3CounterState",
    "Plan3CounterTriggerContract",
    "CounterTriggerCatalog",
    "CounterTriggerCatalogRow",
    "CounterTriggerDecision",
    "CounterTriggerReference",
    "CounterTriggerResolution",
    "evaluate_plan3_counter_trigger",
    "fires",
    "load_counter_trigger_catalog",
    "load_master_counter_trigger_references",
    "load_master_counter_trigger_rows",
    "resolve_plan3_counter_trigger",
]
