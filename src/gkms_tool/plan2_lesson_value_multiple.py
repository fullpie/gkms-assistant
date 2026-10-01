"""Plan2/Common boundary for the exact LessonValueMultiple native primitive.

The Android v3.2.3 status, getter, score-hit, and turn-lifecycle model lives
in :mod:`gkms_tool.nia_lesson_value_multiple`.  This module is deliberately a
small boundary around that model:

* only the two current Plan2 target rows are admitted;
* the normalized and raw Master payloads are checked before admission; and
* the standalone card-slot evaluator delegates status and score work to the
  NIA primitive instead of maintaining a second multiplier implementation.

It does not connect this effect to a larger Plan2 state machine.  Callers can
use the immutable status projection and the typed lesson-hit adapter directly.
Unknown LessonValueMultiple rows are rejected even when their payload happens
to look similar: their Plan2 semantics are not certified by this boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TypeAlias

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    AddingParameterSettings,
    AddingParameterStatus,
    ParameterApplicationStatus,
)
from . import nia_lesson_value_multiple as _nia


EFFECT_TYPE = _nia.EFFECT_TYPE
STATUS_TYPE = _nia.STATUS_TYPE
PLAN_COMMON = "ProducePlanType_Common"
PLAN2 = "ProducePlanType_Plan2"
PLAN2_COMMON_PLAN_TYPES = frozenset((PLAN_COMMON, PLAN2))

TARGET_CARD_ID = "p_card-02-men-3_040"
TARGET_UPGRADES = (0, 1, 2, 3)
TARGET_EFFECT_0100 = "e_effect-exam_lesson_value_multiple-0100-inf"
TARGET_EFFECT_0200 = "e_effect-exam_lesson_value_multiple-0200-inf"
TARGET_EFFECT_IDS = (TARGET_EFFECT_0100, TARGET_EFFECT_0200)
TARGET_EFFECT_VALUES = {
    TARGET_EFFECT_0100: 100,
    TARGET_EFFECT_0200: 200,
}
TARGET_EFFECT_BY_UPGRADE = {
    0: TARGET_EFFECT_0100,
    1: TARGET_EFFECT_0200,
    2: TARGET_EFFECT_0200,
    3: TARGET_EFFECT_0200,
}

EFFECT_LESSON = "ProduceExamEffectType_ExamLesson"
EFFECT_BLOCK = "ProduceExamEffectType_ExamBlock"


class Plan2LessonValueMultipleContractError(ValueError):
    """A Plan2/Common Master or evaluator input is not proven exact."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        message = code if not detail else f"{code}:{detail}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Plan2LessonValueMultiplePause:
    """Typed fail-closed result for an unresolved effect reference."""

    code: str
    detail: str = ""
    effect_id: str = ""


@dataclass(frozen=True, slots=True)
class Plan2LessonValueMultipleEffect:
    """Only the executable fields of one whitelisted exact Master row."""

    effect_id: str
    permil: int
    turn: int
    effect_type: str = EFFECT_TYPE
    effect_value2: int = 0
    effect_count: int = 0
    status_enchant_id: str = ""
    chain_effect_id: str = ""

    def __post_init__(self) -> None:
        expected = TARGET_EFFECT_VALUES.get(self.effect_id)
        if expected is None:
            raise Plan2LessonValueMultipleContractError(
                "unproven-effect-row", self.effect_id
            )
        if self.effect_type != EFFECT_TYPE:
            raise Plan2LessonValueMultipleContractError(
                "unexpected-effect-type", str(self.effect_type)
            )
        if type(self.permil) is not int or self.permil != expected:
            raise Plan2LessonValueMultipleContractError(
                "master-value-mismatch",
                f"{self.effect_id}: expected {expected}, got {self.permil!r}",
            )
        if type(self.turn) is not int or self.turn != -1:
            raise Plan2LessonValueMultipleContractError(
                "master-turn-mismatch", f"{self.effect_id}: {self.turn!r}"
            )
        if self.effect_value2 != 0 or self.effect_count != 0:
            raise Plan2LessonValueMultipleContractError(
                "unsupported-value-shape", self.effect_id
            )
        if self.status_enchant_id or self.chain_effect_id:
            raise Plan2LessonValueMultipleContractError(
                "unsupported-linked-shape", self.effect_id
            )

    @property
    def effect_value1(self) -> int:
        return self.permil

    @property
    def effect_turn(self) -> int:
        return self.turn

    @property
    def native_effect(self) -> _nia.LessonValueMultipleMasterEffect:
        """Return the shared NIA contract without copying its arithmetic."""

        return _nia.LessonValueMultipleMasterEffect(
            effect_id=self.effect_id,
            permil=self.permil,
            turn=self.turn,
        )

    @classmethod
    def from_mapping(
        cls, row: Mapping[str, object] | sqlite3.Row
    ) -> "Plan2LessonValueMultipleEffect":
        """Parse through the same strict Master-row boundary as the loader."""

        return parse_plan2_lesson_value_multiple_master_row(row)


Plan2LessonValueMultipleContract: TypeAlias = Plan2LessonValueMultipleEffect
Plan2LessonValueMultipleMasterEffect: TypeAlias = Plan2LessonValueMultipleEffect
Plan2LessonParameterMultipleState: TypeAlias = _nia.LessonParameterMultipleState
Plan2LessonParameterMultipleStatus: TypeAlias = _nia.LessonParameterMultipleStatus
Plan2LessonValueMultipleExecution: TypeAlias = _nia.LessonValueMultipleExecution
Plan2LessonHitMutation: TypeAlias = _nia.LessonHitMutation


@dataclass(frozen=True, slots=True)
class Plan2LessonValueMultipleResolution:
    contract: Plan2LessonValueMultipleEffect | None = None
    pause: Plan2LessonValueMultiplePause | None = None

    def __post_init__(self) -> None:
        if (self.contract is None) == (self.pause is None):
            raise ValueError("resolution must contain exactly one outcome")

    @property
    def resolved(self) -> bool:
        return self.contract is not None


def _row_value(
    row: Mapping[str, object] | sqlite3.Row, *names: str
) -> object:
    if isinstance(row, Mapping):
        for name in names:
            if name in row:
                return row[name]
        return None
    keys = getattr(row, "keys", None)
    if callable(keys):
        available = set(keys())
        for name in names:
            if name in available:
                return row[name]
    return None


_MISSING = object()


def _row_value_or_missing(
    row: Mapping[str, object] | sqlite3.Row, *names: str
) -> object:
    if isinstance(row, Mapping):
        for name in names:
            if name in row:
                return row[name]
        return _MISSING
    keys = getattr(row, "keys", None)
    if callable(keys):
        available = set(keys())
        for name in names:
            if name in available:
                return row[name]
    return _MISSING


def _require_plan2_common(
    row: Mapping[str, object] | sqlite3.Row | None = None,
    explicit_plan_type: str | None = None,
) -> None:
    observed = explicit_plan_type
    if observed is None and row is not None:
        candidate = _row_value(
            row,
            "plan_type",
            "planType",
            "producePlanType",
            "card_plan_type",
        )
        if candidate is not None:
            observed = str(candidate)
    if observed is not None and observed not in PLAN2_COMMON_PLAN_TYPES:
        raise Plan2LessonValueMultipleContractError(
            "unsupported-plan", str(observed)
        )


def _required_text(value: object, label: str, effect_id: str = "") -> str:
    if not isinstance(value, str) or not value:
        raise Plan2LessonValueMultipleContractError(
            "invalid-master-row", f"{effect_id}:{label}"
        )
    return value


def _required_int(value: object, label: str, effect_id: str = "") -> int:
    if type(value) is not int:
        raise Plan2LessonValueMultipleContractError(
            "invalid-master-row", f"{effect_id}:{label}"
        )
    return value


def _require_equal(
    actual: object, expected: object, field: str, effect_id: str
) -> None:
    if actual != expected:
        raise Plan2LessonValueMultipleContractError(
            "master-shape-mismatch",
            f"{effect_id}:{field}: expected {expected!r}, got {actual!r}",
        )


def _validate_raw_shape(
    raw_json: object,
    *,
    effect_id: str,
    permil: int,
    turn: int,
) -> None:
    try:
        raw = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
    except (TypeError, json.JSONDecodeError) as error:
        raise Plan2LessonValueMultipleContractError(
            "invalid-master-raw-json", effect_id
        ) from error
    if not isinstance(raw, Mapping):
        raise Plan2LessonValueMultipleContractError(
            "invalid-master-raw-json", f"{effect_id}:object-required"
        )

    exact_fields: dict[str, object] = {
        "id": effect_id,
        "effectType": EFFECT_TYPE,
        "effectValue1": permil,
        "effectValue2": 0,
        "effectCount": 0,
        "effectTurn": turn,
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
        "effectGroupIds": ["effect_group-visible-exam_lesson_value_multiple-000"],
    }
    for field, expected in exact_fields.items():
        if field in raw:
            _require_equal(raw[field], expected, field, effect_id)
        else:
            raise Plan2LessonValueMultipleContractError(
                "master-shape-mismatch", f"{effect_id}:{field}:missing"
            )


def _effect_from_row(
    row: Mapping[str, object] | sqlite3.Row,
    *,
    plan_type: str | None = None,
) -> Plan2LessonValueMultipleEffect:
    _require_plan2_common(row, plan_type)
    raw_id = _row_value(row, "id", "effect_id")
    effect_id = _required_text(raw_id, "id")
    if effect_id not in TARGET_EFFECT_VALUES:
        raise Plan2LessonValueMultipleContractError(
            "unproven-effect-row", effect_id
        )

    raw_type = _row_value(row, "effect_type", "effectType")
    effect_type = _required_text(raw_type, "effect_type", effect_id)
    _require_equal(effect_type, EFFECT_TYPE, "effect_type", effect_id)

    value1 = _required_int(
        _row_value(row, "value1", "effectValue1"), "effectValue1", effect_id
    )
    turn = _required_int(
        _row_value(row, "effect_turn", "effectTurn"), "effectTurn", effect_id
    )

    optional_fields: tuple[tuple[str, tuple[str, ...], object], ...] = (
        ("effectValue2", ("value2", "effectValue2"), 0),
        ("effectCount", ("effect_count", "effectCount"), 0),
        ("produceExamStatusEnchantId", ("status_enchant_id", "produceExamStatusEnchantId"), ""),
        ("chainProduceExamEffectId", ("chain_effect_id", "chainProduceExamEffectId"), ""),
    )
    normalized: dict[str, object] = {}
    for label, names, expected in optional_fields:
        value = _row_value_or_missing(row, *names)
        if value is _MISSING:
            value = expected
        _require_equal(value, expected, label, effect_id)
        normalized[label] = value

    raw_json = _row_value_or_missing(row, "raw_json")
    if raw_json is not _MISSING:
        _validate_raw_shape(
            raw_json,
            effect_id=effect_id,
            permil=value1,
            turn=turn,
        )

    return Plan2LessonValueMultipleEffect(
        effect_id=effect_id,
        permil=value1,
        turn=turn,
        effect_type=effect_type,
        effect_value2=int(normalized["effectValue2"]),
        effect_count=int(normalized["effectCount"]),
        status_enchant_id=str(normalized["produceExamStatusEnchantId"]),
        chain_effect_id=str(normalized["chainProduceExamEffectId"]),
    )


def _effect_id_from_reference(
    effect: str | Mapping[str, object] | sqlite3.Row,
    *,
    plan_type: str | None,
) -> str:
    if isinstance(effect, str):
        _require_plan2_common(explicit_plan_type=plan_type)
        if not effect:
            raise Plan2LessonValueMultipleContractError(
                "invalid-effect-id", "empty"
            )
        return effect
    if not isinstance(effect, (Mapping, sqlite3.Row)):
        raise Plan2LessonValueMultipleContractError(
            "invalid-effect-reference", type(effect).__name__
        )
    _require_plan2_common(effect, plan_type)
    value = _row_value(effect, "id", "effect_id")
    return _required_text(value, "id")


def load_plan2_lesson_value_multiple_effect(
    effect_id: str,
    *,
    database: Path = DEFAULT_DATABASE,
) -> Plan2LessonValueMultipleEffect:
    """Load and validate one exact target row from the local Master DB."""

    if not isinstance(effect_id, str) or not effect_id:
        raise Plan2LessonValueMultipleContractError("invalid-effect-id", "empty")
    database = Path(database).resolve()
    try:
        with sqlite3.connect(
            f"file:{database.as_posix()}?mode=ro", uri=True
        ) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM effect WHERE id = ?", (effect_id,)
            ).fetchone()
    except sqlite3.Error as error:
        raise Plan2LessonValueMultipleContractError(
            "master-effect-read-failed", str(error)
        ) from error
    if row is None:
        raise Plan2LessonValueMultipleContractError(
            "effect-not-found", effect_id
        )
    return _effect_from_row(row)


def parse_plan2_lesson_value_multiple_master_row(
    effect: Mapping[str, object] | sqlite3.Row,
    *,
    plan_type: str | None = None,
) -> Plan2LessonValueMultipleEffect:
    """Validate an already loaded normalized/raw Master row."""

    if not isinstance(effect, (Mapping, sqlite3.Row)):
        raise Plan2LessonValueMultipleContractError(
            "invalid-effect-reference", type(effect).__name__
        )
    return _effect_from_row(effect, plan_type=plan_type)


def resolve_plan2_lesson_value_multiple(
    effect: str | Mapping[str, object] | sqlite3.Row,
    *,
    database: Path = DEFAULT_DATABASE,
    plan_type: str | None = None,
) -> Plan2LessonValueMultipleResolution:
    """Resolve a string or local Master row into the exact typed contract."""

    if isinstance(effect, str):
        contract = load_plan2_lesson_value_multiple_effect(
            effect, database=database
        )
    else:
        contract = parse_plan2_lesson_value_multiple_master_row(
            effect, plan_type=plan_type
        )
    return Plan2LessonValueMultipleResolution(contract=contract)


def try_resolve_plan2_lesson_value_multiple(
    effect: str | Mapping[str, object] | sqlite3.Row,
    *,
    database: Path = DEFAULT_DATABASE,
    plan_type: str | None = None,
) -> Plan2LessonValueMultipleResolution:
    """Resolve with a typed pause instead of raising a boundary error."""

    try:
        return resolve_plan2_lesson_value_multiple(
            effect, database=database, plan_type=plan_type
        )
    except Plan2LessonValueMultipleContractError as error:
        effect_id = effect if isinstance(effect, str) else str(
            _row_value(effect, "id", "effect_id")
            if isinstance(effect, (Mapping, sqlite3.Row))
            else ""
        )
        return Plan2LessonValueMultipleResolution(
            pause=Plan2LessonValueMultiplePause(
                error.code, error.detail, effect_id
            )
        )


def load_plan2_lesson_value_multiple_effects(
    *, database: Path = DEFAULT_DATABASE
) -> tuple[Plan2LessonValueMultipleEffect, ...]:
    """Load only the two target rows; all other family rows stay unresolved."""

    return tuple(
        load_plan2_lesson_value_multiple_effect(effect_id, database=database)
        for effect_id in TARGET_EFFECT_IDS
    )


@dataclass(frozen=True, slots=True)
class Plan2LessonValueMultipleDifference:
    """The status difference emitted after a successful native installation."""

    status_type: int
    effect_id: str
    multiplier_before: float
    multiplier_after: float


@dataclass(frozen=True, slots=True)
class Plan2LessonValueMultipleTransition:
    """Pure installation projection with native call-order evidence."""

    before: Plan2LessonParameterMultipleState
    after: Plan2LessonParameterMultipleState
    effect: Plan2LessonValueMultipleEffect
    installed: bool
    merged_index: int | None
    multiplier_before: float
    multiplier_after: float
    difference: Plan2LessonValueMultipleDifference | None
    trace: tuple[str, ...]

    @property
    def state_after(self) -> Plan2LessonParameterMultipleState:
        return self.after


def execute_plan2_lesson_value_multiple(
    state: Plan2LessonParameterMultipleState,
    effect: Plan2LessonValueMultipleEffect,
    *,
    block_add_status: bool = False,
) -> Plan2LessonValueMultipleTransition:
    """Install one exact row through the shared NIA native projection."""

    if not isinstance(state, _nia.LessonParameterMultipleState):
        raise TypeError("state must be LessonParameterMultipleState")
    if not isinstance(effect, Plan2LessonValueMultipleEffect):
        raise TypeError("effect must be Plan2LessonValueMultipleEffect")
    before_multiple = state.multiplier()
    native = _nia.execute_master_effect(
        state,
        effect.native_effect,
        block_add_status=block_add_status,
    )
    after_multiple = native.state.multiplier()
    difference = (
        Plan2LessonValueMultipleDifference(
            status_type=STATUS_TYPE,
            effect_id=effect.effect_id,
            multiplier_before=before_multiple,
            multiplier_after=after_multiple,
        )
        if native.installed
        else None
    )
    trace = (
        (
            "read-multiplier-before-install",
            "try-add-status-type-39",
            "read-multiplier-after-install",
            "create-status-difference",
        )
        if native.installed
        else ("read-multiplier-before-install", "try-add-status-type-39")
    )
    return Plan2LessonValueMultipleTransition(
        before=state,
        after=native.state,
        effect=effect,
        installed=native.installed,
        merged_index=native.merged_index,
        multiplier_before=before_multiple,
        multiplier_after=after_multiple,
        difference=difference,
        trace=trace,
    )


install_plan2_lesson_value_multiple = execute_plan2_lesson_value_multiple


def bind_plan2_effect_status(
    status: AddingParameterStatus,
    state: Plan2LessonParameterMultipleState,
    *,
    effect_type: str,
) -> AddingParameterStatus:
    """Bind type-39 only to Lesson calculation; leave Block/other paths alone."""

    if not isinstance(status, AddingParameterStatus):
        raise TypeError("status must be AddingParameterStatus")
    if not isinstance(state, _nia.LessonParameterMultipleState):
        raise TypeError("state must be LessonParameterMultipleState")
    if effect_type == EFFECT_LESSON:
        return _nia.bind_adding_parameter_status(status, state)
    if effect_type in {EFFECT_BLOCK, "other"}:
        return status
    raise Plan2LessonValueMultipleContractError(
        "unsupported-effect-scope", effect_type
    )


def apply_plan2_lesson_hit(
    value: int,
    *,
    multiplier_state: Plan2LessonParameterMultipleState,
    adding_status: AddingParameterStatus,
    settings: AddingParameterSettings,
    application_status: ParameterApplicationStatus,
    is_buff_active: bool = True,
) -> Plan2LessonHitMutation:
    """Delegate one Lesson Calculate→AddParameter hit to the NIA primitive."""

    return _nia.apply_lesson_hit(
        value,
        multiplier_state=multiplier_state,
        adding_status=adding_status,
        settings=settings,
        application_status=application_status,
        is_buff_active=is_buff_active,
    )


def apply_plan2_lesson_hits(
    value: int,
    count: int,
    *,
    multiplier_state: Plan2LessonParameterMultipleState,
    adding_status: AddingParameterStatus,
    settings: AddingParameterSettings,
    application_status: ParameterApplicationStatus,
    is_buff_active: bool = True,
) -> tuple[Plan2LessonHitMutation, ...]:
    """Delegate repeated per-hit calculation without adding a second model."""

    return _nia.apply_lesson_hits(
        value,
        count,
        multiplier_state=multiplier_state,
        adding_status=adding_status,
        settings=settings,
        application_status=application_status,
        is_buff_active=is_buff_active,
    )


class Plan2LessonValueMultipleSlotKind(str, Enum):
    INSTALL = "lesson_value_multiple"
    LESSON = "lesson"
    BLOCK = "block"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Plan2LessonValueMultipleSlot:
    """Minimal ordered slot input for the standalone boundary evaluator."""

    index: int
    kind: Plan2LessonValueMultipleSlotKind
    effect_id: str = ""
    lesson_value: int | None = None
    lesson_count: int = 1
    effect: Plan2LessonValueMultipleEffect | None = None

    def __post_init__(self) -> None:
        if type(self.index) is not int or self.index < 0:
            raise ValueError("slot index must be a non-negative integer")
        if not isinstance(self.kind, Plan2LessonValueMultipleSlotKind):
            raise TypeError("slot kind must be Plan2LessonValueMultipleSlotKind")
        if type(self.lesson_count) is not int or self.lesson_count < 0:
            raise ValueError("lesson_count must be a non-negative integer")
        if self.kind is Plan2LessonValueMultipleSlotKind.INSTALL:
            if self.effect is None:
                raise Plan2LessonValueMultipleContractError(
                    "install-slot-effect-required", self.effect_id
                )
            if self.effect_id and self.effect_id != self.effect.effect_id:
                raise Plan2LessonValueMultipleContractError(
                    "install-slot-id-mismatch", self.effect_id
                )
        elif self.effect is not None:
            raise Plan2LessonValueMultipleContractError(
                "non-install-slot-has-effect", str(self.index)
            )


@dataclass(frozen=True, slots=True)
class Plan2LessonValueMultipleSlotResult:
    slot: Plan2LessonValueMultipleSlot
    state_before: Plan2LessonParameterMultipleState
    state_after: Plan2LessonParameterMultipleState
    multiplier_before: float
    multiplier_after: float
    transition: Plan2LessonValueMultipleTransition | None
    hits: tuple[Plan2LessonHitMutation, ...]


@dataclass(frozen=True, slots=True)
class Plan2LessonValueMultipleSlotsResult:
    state_after: Plan2LessonParameterMultipleState
    application_status_after: ParameterApplicationStatus
    slots: tuple[Plan2LessonValueMultipleSlotResult, ...]


def evaluate_plan2_lesson_value_multiple_slots(
    slots: Sequence[Plan2LessonValueMultipleSlot],
    *,
    state: Plan2LessonParameterMultipleState | None = None,
    adding_status: AddingParameterStatus | None = None,
    settings: AddingParameterSettings | None = None,
    application_status: ParameterApplicationStatus | None = None,
) -> Plan2LessonValueMultipleSlotsResult:
    """Evaluate ordered slots while applying type 39 only to Lesson hits.

    The evaluator is intentionally not a general card executor.  ``BLOCK``
    and ``OTHER`` are scope probes: they preserve the status and produce no
    Lesson hit.  A caller that needs their full runtime must use their own
    separately proven effect adapter.  A Lesson slot after an INSTALL slot
    reads the newly installed state immediately.
    """

    if not isinstance(slots, Sequence):
        raise TypeError("slots must be a sequence")
    working = (
        _nia.LessonParameterMultipleState() if state is None else state
    )
    if not isinstance(working, _nia.LessonParameterMultipleState):
        raise TypeError("state must be LessonParameterMultipleState")
    adding = AddingParameterStatus() if adding_status is None else adding_status
    config = AddingParameterSettings() if settings is None else settings
    current_application = (
        ParameterApplicationStatus(judge_parameter=0)
        if application_status is None
        else application_status
    )
    if not isinstance(adding, AddingParameterStatus):
        raise TypeError("adding_status must be AddingParameterStatus")
    if not isinstance(config, AddingParameterSettings):
        raise TypeError("settings must be AddingParameterSettings")
    if not isinstance(current_application, ParameterApplicationStatus):
        raise TypeError("application_status must be ParameterApplicationStatus")

    results: list[Plan2LessonValueMultipleSlotResult] = []
    for slot in slots:
        if not isinstance(slot, Plan2LessonValueMultipleSlot):
            raise TypeError("slots must contain Plan2LessonValueMultipleSlot")
        before = working
        before_multiple = before.multiplier()
        transition: Plan2LessonValueMultipleTransition | None = None
        hits: tuple[Plan2LessonHitMutation, ...] = ()
        if slot.kind is Plan2LessonValueMultipleSlotKind.INSTALL:
            assert slot.effect is not None
            transition = execute_plan2_lesson_value_multiple(working, slot.effect)
            working = transition.after
        elif slot.kind is Plan2LessonValueMultipleSlotKind.LESSON:
            if slot.lesson_value is None:
                raise Plan2LessonValueMultipleContractError(
                    "lesson-slot-value-required", str(slot.index)
                )
            hits = apply_plan2_lesson_hits(
                slot.lesson_value,
                slot.lesson_count,
                multiplier_state=working,
                adding_status=adding,
                settings=config,
                application_status=current_application,
            )
            if hits:
                current_application = hits[-1].next_application_status
        elif slot.kind in {
            Plan2LessonValueMultipleSlotKind.BLOCK,
            Plan2LessonValueMultipleSlotKind.OTHER,
        }:
            # The status getter is not an input to these effect families.
            # Their complete mutations remain outside this standalone scope.
            pass
        else:  # pragma: no cover - Enum exhaustiveness guard
            raise AssertionError(slot.kind)
        results.append(
            Plan2LessonValueMultipleSlotResult(
                slot=slot,
                state_before=before,
                state_after=working,
                multiplier_before=before_multiple,
                multiplier_after=working.multiplier(),
                transition=transition,
                hits=hits,
            )
        )
    return Plan2LessonValueMultipleSlotsResult(
        state_after=working,
        application_status_after=current_application,
        slots=tuple(results),
    )


def load_plan2_target_card_versions(
    *, database: Path = DEFAULT_DATABASE
) -> tuple["Plan2LessonValueMultipleCardVersion", ...]:
    """Load the four direct target versions without rebuilding coverage."""

    database = Path(database).resolve()
    try:
        with sqlite3.connect(
            f"file:{database.as_posix()}?mode=ro", uri=True
        ) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT id, upgrade_count, plan_type, play_effects_json
                FROM card
                WHERE id = ?
                ORDER BY upgrade_count
                """,
                (TARGET_CARD_ID,),
            ).fetchall()
    except sqlite3.Error as error:
        raise Plan2LessonValueMultipleContractError(
            "master-card-read-failed", str(error)
        ) from error
    versions: list[Plan2LessonValueMultipleCardVersion] = []
    for row in rows:
        upgrade = _required_int(row["upgrade_count"], "upgrade_count")
        if upgrade not in TARGET_UPGRADES:
            continue
        plan_type = _required_text(row["plan_type"], "plan_type", TARGET_CARD_ID)
        _require_plan2_common(explicit_plan_type=plan_type)
        try:
            payload = json.loads(str(row["play_effects_json"]))
        except json.JSONDecodeError as error:
            raise Plan2LessonValueMultipleContractError(
                "invalid-card-slots", f"{TARGET_CARD_ID}:{upgrade}"
            ) from error
        if not isinstance(payload, list) or not payload:
            raise Plan2LessonValueMultipleContractError(
                "invalid-card-slots", f"{TARGET_CARD_ID}:{upgrade}"
            )
        effect_ids: list[str] = []
        trigger_ids: list[str] = []
        for slot in payload:
            if not isinstance(slot, Mapping):
                raise Plan2LessonValueMultipleContractError(
                    "invalid-card-slot", f"{TARGET_CARD_ID}:{upgrade}"
                )
            effect_ids.append(
                _required_text(
                    slot.get("produceExamEffectId"),
                    "produceExamEffectId",
                    TARGET_CARD_ID,
                )
            )
            trigger_ids.append(str(slot.get("produceExamTriggerId", "")))
        target_id = TARGET_EFFECT_BY_UPGRADE[upgrade]
        indices = [i for i, item in enumerate(effect_ids) if item == target_id]
        if indices != [2]:
            raise Plan2LessonValueMultipleContractError(
                "target-slot-shape",
                f"{TARGET_CARD_ID}:{upgrade}:{indices}",
            )
        if any(
            item in TARGET_EFFECT_IDS and item != target_id
            for item in effect_ids
        ):
            raise Plan2LessonValueMultipleContractError(
                "multiple-target-slots", f"{TARGET_CARD_ID}:{upgrade}"
            )
        load_plan2_lesson_value_multiple_effect(target_id, database=database)
        versions.append(
            Plan2LessonValueMultipleCardVersion(
                card_id=TARGET_CARD_ID,
                upgrade=upgrade,
                plan_type=plan_type,
                effect_ids=tuple(effect_ids),
                trigger_ids=tuple(trigger_ids),
                target_effect_id=target_id,
                target_slot_index=indices[0],
            )
        )
    expected_upgrades = set(TARGET_UPGRADES)
    if len(versions) != len(TARGET_UPGRADES) or {
        row.upgrade for row in versions
    } != expected_upgrades:
        raise Plan2LessonValueMultipleContractError(
            "target-card-catalog", f"expected {sorted(expected_upgrades)}"
        )
    return tuple(versions)


@dataclass(frozen=True, slots=True)
class Plan2LessonValueMultipleCardVersion:
    card_id: str
    upgrade: int
    plan_type: str
    effect_ids: tuple[str, ...]
    trigger_ids: tuple[str, ...]
    target_effect_id: str
    target_slot_index: int

    @property
    def subsequent_effect_ids(self) -> tuple[str, ...]:
        return self.effect_ids[self.target_slot_index + 1 :]


# Short names make the thin adapter convenient to use without exposing the
# private module alias.  The identity assertions in the focused tests ensure
# these remain delegations, not independently maintained arithmetic.
LessonParameterMultipleState = _nia.LessonParameterMultipleState
LessonParameterMultipleStatus = _nia.LessonParameterMultipleStatus
LessonValueMultipleMasterEffect = _nia.LessonValueMultipleMasterEffect
LessonValueMultipleExecution = _nia.LessonValueMultipleExecution
apply_lesson_hit = _nia.apply_lesson_hit
apply_lesson_hits = _nia.apply_lesson_hits
install_lesson_value_multiple = _nia.install_lesson_value_multiple


__all__ = [
    "EFFECT_BLOCK",
    "EFFECT_LESSON",
    "EFFECT_TYPE",
    "PLAN2",
    "PLAN2_COMMON_PLAN_TYPES",
    "PLAN_COMMON",
    "STATUS_TYPE",
    "TARGET_CARD_ID",
    "TARGET_EFFECT_0100",
    "TARGET_EFFECT_0200",
    "TARGET_EFFECT_BY_UPGRADE",
    "TARGET_EFFECT_IDS",
    "TARGET_UPGRADES",
    "LessonParameterMultipleState",
    "LessonParameterMultipleStatus",
    "LessonValueMultipleExecution",
    "LessonValueMultipleMasterEffect",
    "Plan2LessonHitMutation",
    "Plan2LessonParameterMultipleState",
    "Plan2LessonParameterMultipleStatus",
    "Plan2LessonValueMultipleCardVersion",
    "Plan2LessonValueMultipleContract",
    "Plan2LessonValueMultipleContractError",
    "Plan2LessonValueMultipleDifference",
    "Plan2LessonValueMultipleEffect",
    "Plan2LessonValueMultipleExecution",
    "Plan2LessonValueMultipleMasterEffect",
    "Plan2LessonValueMultiplePause",
    "Plan2LessonValueMultipleResolution",
    "Plan2LessonValueMultipleSlot",
    "Plan2LessonValueMultipleSlotKind",
    "Plan2LessonValueMultipleSlotResult",
    "Plan2LessonValueMultipleSlotsResult",
    "Plan2LessonValueMultipleTransition",
    "apply_lesson_hit",
    "apply_lesson_hits",
    "apply_plan2_lesson_hit",
    "apply_plan2_lesson_hits",
    "bind_plan2_effect_status",
    "execute_plan2_lesson_value_multiple",
    "install_lesson_value_multiple",
    "install_plan2_lesson_value_multiple",
    "load_plan2_lesson_value_multiple_effect",
    "load_plan2_lesson_value_multiple_effects",
    "load_plan2_target_card_versions",
    "parse_plan2_lesson_value_multiple_master_row",
    "resolve_plan2_lesson_value_multiple",
    "try_resolve_plan2_lesson_value_multiple",
    "evaluate_plan2_lesson_value_multiple_slots",
]
