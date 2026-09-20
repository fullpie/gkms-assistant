"""Typed parser/evaluator for native ``ProduceExamTrigger`` conjunctions.

The Master trigger row is not a single scalar condition.  It is an ordered
tuple of a phase, optional phase arguments, zero or more field predicates,
optional card-search bounds, an effect filter, and optional movement/lesson
qualifiers.  Several idol-bound P items use the conjunction of a field
threshold and a card search (and a few use a phase argument and a field
threshold).  The older item runtime compiled those columns into mutually
exclusive special cases, which made adding one more P item require another
item-id branch.

This module owns only the data contract.  It does not install a listener or
mutate game state.  It deliberately keeps the Master array order, rejects
shape drift, and returns ``fires=None`` when the caller has not supplied every
piece of runtime evidence needed to evaluate a predicate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from typing import Final

from .card_search import ProduceCardSearchRule, match_exact_target_card_search


CHECK_DEFAULT: Final = ""
CHECK_NOT: Final = "ProduceExamTriggerCheckType_Not"
_SUPPORTED_CHECKS: Final = frozenset({CHECK_DEFAULT, CHECK_NOT})

CARD_MOVE_UNKNOWN: Final = "ProduceCardMovePositionType_Unknown"
LESSON_UNKNOWN: Final = "ProduceStepLessonType_Unknown"
NO_BLOCK_FIELD: Final = "ProduceExamFieldStatusType_NoBlock"

# These are the native phase/lesson tokens present in the imported Android
# Master schema.  Keeping the allowlist here means a newly introduced enum is
# a visible blocker instead of becoming an accidental exact-match wildcard.
KNOWN_PHASES: Final = frozenset(
    {
        "ProduceExamPhaseType_ExamAggressiveUpInterval",
        "ProduceExamPhaseType_ExamBuffConsume",
        "ProduceExamPhaseType_ExamCardMoveGrave",
        "ProduceExamPhaseType_ExamCardMoveHand",
        "ProduceExamPhaseType_ExamCardMoveLost",
        "ProduceExamPhaseType_ExamCardPlay",
        "ProduceExamPhaseType_ExamCardPlayAfter",
        "ProduceExamPhaseType_ExamEndTurn",
        "ProduceExamPhaseType_ExamEndTurnInterval",
        "ProduceExamPhaseType_ExamPlayCountInterval",
        "ProduceExamPhaseType_ExamPlayCountIntervalAfter",
        "ProduceExamPhaseType_ExamPlayTurnCountInterval",
        "ProduceExamPhaseType_ExamSearchCardPlay",
        "ProduceExamPhaseType_ExamStaminaReduce",
        "ProduceExamPhaseType_ExamStaminaReduceCard",
        "ProduceExamPhaseType_ExamStanceChangeConcentration",
        "ProduceExamPhaseType_ExamStanceChangeCountInterval",
        "ProduceExamPhaseType_ExamStanceChangeFromConcentration",
        "ProduceExamPhaseType_ExamStanceChangeFromFullPower",
        "ProduceExamPhaseType_ExamStanceChangeFullPower",
        "ProduceExamPhaseType_ExamStanceChangePreservation",
        "ProduceExamPhaseType_ExamStartExam",
        "ProduceExamPhaseType_ExamStartTurn",
        "ProduceExamPhaseType_ExamStatusChange",
        "ProduceExamPhaseType_ExamTurnInterval",
        "ProduceExamPhaseType_ExamTurnSkip",
        "ProduceExamPhaseType_ExamTurnTimer",
        "ProduceExamPhaseType_None",
        "ProduceExamPhaseType_StartExamPlay",
        "ProduceExamPhaseType_StartPlay",
    }
)
KNOWN_LESSON_TYPES: Final = frozenset(
    {
        LESSON_UNKNOWN,
        "ProduceStepLessonType_LessonDance",
        "ProduceStepLessonType_LessonSp",
        "ProduceStepLessonType_LessonVisual",
        "ProduceStepLessonType_LessonVocal",
    }
)


class CompositeTriggerError(ValueError):
    """A fail-closed parse or shape error with a stable diagnostic code."""

    def __init__(self, code: str, detail: str = "") -> None:
        if not isinstance(code, str) or not code:
            raise ValueError("CompositeTriggerError code must be non-empty text")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}:{detail}" if detail else code)


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise CompositeTriggerError("invalid-text", label)
    return value


def _int(value: object, label: str, *, minimum: int | None = None) -> int:
    # ``bool`` is an ``int`` subclass.  Master's JSON integer fields must not
    # silently accept True/False as 1/0, especially for trigger thresholds.
    if isinstance(value, bool) or not isinstance(value, int):
        raise CompositeTriggerError("invalid-integer", label)
    if minimum is not None and value < minimum:
        raise CompositeTriggerError("integer-below-minimum", label)
    return value


def _tuple_text(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise CompositeTriggerError("invalid-string-array", label)
    result = tuple(_text(item, f"{label}[{index}]") for index, item in enumerate(value))
    return result


def _tuple_int(value: object, label: str) -> tuple[int, ...]:
    if not isinstance(value, (tuple, list)):
        raise CompositeTriggerError("invalid-integer-array", label)
    return tuple(
        _int(item, f"{label}[{index}]") for index, item in enumerate(value)
    )


def _sequence_value(value: object, label: str) -> object:
    """Decode one raw DB JSON array, preserving list order exactly."""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise CompositeTriggerError("invalid-json", label) from error
    return value


def _mapping_value(
    raw: Mapping[str, object],
    *names: str,
    required: bool = True,
    default: object = None,
) -> object:
    for name in names:
        if name in raw:
            return raw[name]
    if required:
        raise CompositeTriggerError("missing-field", names[0])
    return default


@dataclass(frozen=True, slots=True)
class CompositeFieldPredicate:
    """One ordered ``fieldStatus`` tuple row.

    ``check_type == ""`` means Master omitted the parallel check array and
    therefore uses the native positive comparison.  ``value is None`` is the
    exact representation of a field whose Master value array is empty (for
    example ``NoBlock``).  No field rows are sorted or deduplicated.
    """

    field_type: str
    check_type: str = CHECK_DEFAULT
    value: int | None = None
    card_search_id: str = ""

    def __post_init__(self) -> None:
        _text(self.field_type, "field_type")
        _text(self.check_type, "check_type", empty=True)
        if self.check_type not in _SUPPORTED_CHECKS:
            raise CompositeTriggerError("unknown-check-type", self.check_type)
        if self.value is not None:
            _int(self.value, "field_value", minimum=0)
        _text(self.card_search_id, "field_card_search_id", empty=True)

    @property
    def check_not(self) -> bool:
        return self.check_type == CHECK_NOT

    def to_dict(self) -> dict[str, object]:
        return {
            "checkType": self.check_type,
            "fieldType": self.field_type,
            "value": self.value,
            "fieldCardSearchId": self.card_search_id,
        }


@dataclass(frozen=True, slots=True)
class CompositeTriggerSpec:
    """Canonical ordered Master trigger shape.

    The current Android Master has one phase per trigger.  A trigger with
    multiple phase entries is not treated as an implicit OR; it is rejected
    because this evaluator receives one concrete native phase event.
    """

    trigger_id: str
    phase: str
    phase_values: tuple[int, ...]
    fields: tuple[CompositeFieldPredicate, ...]
    produce_card_search_id: str = ""
    upper_search_count: int = 0
    lower_search_count: int = 0
    card_move_position_type: str = CARD_MOVE_UNKNOWN
    effect_types: tuple[str, ...] = ()
    lesson_type: str = LESSON_UNKNOWN
    card_search_rule: ProduceCardSearchRule | None = None

    def __post_init__(self) -> None:
        _text(self.trigger_id, "trigger_id")
        _text(self.phase, "phase")
        if self.phase not in KNOWN_PHASES:
            raise CompositeTriggerError("unknown-phase", self.phase)
        phase_values = tuple(self.phase_values)
        if len(phase_values) > 1:
            raise CompositeTriggerError("phase-value-shape", self.trigger_id)
        for index, value in enumerate(phase_values):
            _int(value, f"phase_values[{index}]", minimum=0)
        object.__setattr__(self, "phase_values", phase_values)

        fields = tuple(self.fields)
        if any(not isinstance(item, CompositeFieldPredicate) for item in fields):
            raise TypeError("fields must contain CompositeFieldPredicate values")
        object.__setattr__(self, "fields", fields)

        _text(self.produce_card_search_id, "produce_card_search_id", empty=True)
        _int(self.upper_search_count, "upper_search_count", minimum=0)
        _int(self.lower_search_count, "lower_search_count", minimum=0)
        _text(self.card_move_position_type, "card_move_position_type")
        effect_types = tuple(self.effect_types)
        if any(not isinstance(value, str) or not value for value in effect_types):
            raise CompositeTriggerError("invalid-string-array", "effect_types")
        object.__setattr__(self, "effect_types", effect_types)
        _text(self.lesson_type, "lesson_type")
        if self.lesson_type not in KNOWN_LESSON_TYPES:
            raise CompositeTriggerError("unknown-lesson-type", self.lesson_type)
        if self.card_search_rule is not None and not isinstance(
            self.card_search_rule, ProduceCardSearchRule
        ):
            raise TypeError("card_search_rule must be ProduceCardSearchRule or None")

    @property
    def field_types(self) -> tuple[str, ...]:
        return tuple(item.field_type for item in self.fields)

    @property
    def field_check_types(self) -> tuple[str, ...]:
        # The empty Master check array is represented as empty in this view;
        # ``fields`` retains the effective default operator per row.
        if all(item.check_type == CHECK_DEFAULT for item in self.fields):
            return ()
        return tuple(item.check_type for item in self.fields)

    @property
    def field_values(self) -> tuple[int, ...]:
        if all(item.value is None for item in self.fields):
            return ()
        return tuple(
            item.value for item in self.fields if item.value is not None
        )

    @property
    def field_card_search_ids(self) -> tuple[str, ...]:
        if all(not item.card_search_id for item in self.fields):
            return ()
        return tuple(item.card_search_id for item in self.fields)

    @property
    def is_composite(self) -> bool:
        """Whether this row combines at least two independent constraints."""

        return bool(
            self.fields
            and (
                self.produce_card_search_id
                or self.phase_values
                or self.effect_types
                or self.card_move_position_type != CARD_MOVE_UNKNOWN
                or self.lesson_type != LESSON_UNKNOWN
            )
        ) or len(self.fields) > 1

    def shape_fingerprint(self) -> tuple[object, ...]:
        """Return order-sensitive evidence suitable for a binding audit."""

        return (
            self.phase,
            self.phase_values,
            tuple(
                (
                    item.check_type,
                    item.field_type,
                    item.value,
                    item.card_search_id,
                )
                for item in self.fields
            ),
            self.produce_card_search_id,
            self.upper_search_count,
            self.lower_search_count,
            self.card_move_position_type,
            self.effect_types,
            self.lesson_type,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.trigger_id,
            "phaseTypes": [self.phase],
            "phaseValues": list(self.phase_values),
            "fieldStatusCheckTypes": list(self.field_check_types),
            "fieldStatusTypes": list(self.field_types),
            "fieldStatusValues": list(self.field_values),
            "fieldStatusProduceCardSearchIds": list(self.field_card_search_ids),
            "produceCardSearchId": self.produce_card_search_id,
            "upperSearchCount": self.upper_search_count,
            "lowerSearchCount": self.lower_search_count,
            "cardMovePositionType": self.card_move_position_type,
            "effectTypes": list(self.effect_types),
            "lessonType": self.lesson_type,
        }


def _extract_trigger_values(raw: object) -> dict[str, object]:
    """Read ``ExamTriggerRule``, ``ExamStatusTrigger``, or a Master row.

    Attribute access is structural on purpose: importing either runtime model
    here would introduce circular dependencies.  Mapping aliases cover both
    normalized snake_case objects and the SQLite ``*_json`` columns.
    """

    if isinstance(raw, Mapping):
        source = raw
        return {
            "id": _mapping_value(source, "id", "trigger_id"),
            "phase_types": _sequence_value(
                _mapping_value(source, "phase_types", "phase_types_json", "phaseTypes"),
                "phase_types",
            ),
            "phase_values": _sequence_value(
                _mapping_value(source, "phase_values", "phase_values_json", "phaseValues"),
                "phase_values",
            ),
            "checks": _sequence_value(
                _mapping_value(
                    source,
                    "field_check_types",
                    "field_status_check_types",
                    "field_status_check_types_json",
                    "fieldStatusCheckTypes",
                ),
                "field_check_types",
            ),
            "fields": _sequence_value(
                _mapping_value(
                    source,
                    "field_types",
                    "field_status_types",
                    "field_status_types_json",
                    "fieldStatusTypes",
                ),
                "field_types",
            ),
            "values": _sequence_value(
                _mapping_value(
                    source,
                    "field_values",
                    "field_status_values",
                    "field_status_values_json",
                    "fieldStatusValues",
                ),
                "field_values",
            ),
            "field_searches": _sequence_value(
                _mapping_value(
                    source,
                    "field_card_search_ids",
                    "field_status_card_search_ids",
                    "field_status_produce_card_search_ids_json",
                    "fieldStatusProduceCardSearchIds",
                ),
                "field_card_search_ids",
            ),
            "produce_card_search_id": _mapping_value(
                source,
                "produce_card_search_id",
                "produceCardSearchId",
            ),
            "upper_search_count": _mapping_value(
                source, "upper_search_count", "upperSearchCount"
            ),
            "lower_search_count": _mapping_value(
                source, "lower_search_count", "lowerSearchCount"
            ),
            "card_move_position_type": _mapping_value(
                source, "card_move_position_type", "cardMovePositionType"
            ),
            "effect_types": _sequence_value(
                _mapping_value(source, "effect_types", "effect_types_json", "effectTypes"),
                "effect_types",
            ),
            "lesson_type": _mapping_value(source, "lesson_type", "lessonType"),
            "card_search_rule": source.get("card_search_rule"),
        }

    names = {
        "id": ("id",),
        "phase_types": ("phase_types",),
        "phase_values": ("phase_values",),
        "checks": ("field_check_types", "field_status_check_types"),
        "fields": ("field_types", "field_status_types"),
        "values": ("field_values", "field_status_values"),
        "field_searches": (
            "field_card_search_ids",
            "field_status_card_search_ids",
        ),
        "produce_card_search_id": ("produce_card_search_id",),
        "upper_search_count": ("upper_search_count",),
        "lower_search_count": ("lower_search_count",),
        "card_move_position_type": ("card_move_position_type",),
        "effect_types": ("effect_types",),
        "lesson_type": ("lesson_type",),
    }
    result: dict[str, object] = {}
    for key, attributes in names.items():
        value = None
        found = False
        for attribute in attributes:
            if hasattr(raw, attribute):
                value = getattr(raw, attribute)
                found = True
                break
        if not found:
            raise CompositeTriggerError("missing-field", attributes[0])
        result[key] = value
    result["card_search_rule"] = getattr(raw, "card_search_rule", None)
    return result


def parse_composite_trigger(
    raw: object,
    *,
    card_search_rule: ProduceCardSearchRule | None = None,
) -> CompositeTriggerSpec:
    """Parse one exact Master/native trigger shape.

    Empty parallel arrays are the native default.  If a vector is present,
    its entries are zipped by index; no sorting, set conversion, or
    positional repair is performed.  Mismatched lengths and unknown
    operators fail closed with :class:`CompositeTriggerError`.
    """

    values = _extract_trigger_values(raw)
    trigger_id = _text(values["id"], "trigger_id")
    phase_types = _tuple_text(values["phase_types"], f"{trigger_id}.phase_types")
    if len(phase_types) != 1:
        raise CompositeTriggerError("phase-shape", trigger_id)
    phase_values = _tuple_int(values["phase_values"], f"{trigger_id}.phase_values")

    checks = _tuple_text(values["checks"], f"{trigger_id}.field_checks")
    fields = _tuple_text(values["fields"], f"{trigger_id}.field_types")
    field_values = _tuple_int(values["values"], f"{trigger_id}.field_values")
    # A present field-search vector is positional; an empty entry means that
    # this field row has no card-search qualifier.  Keep that empty token
    # instead of treating it as a malformed string or dropping its position.
    raw_field_searches = values["field_searches"]
    if not isinstance(raw_field_searches, (tuple, list)):
        raise CompositeTriggerError(
            "invalid-string-array", f"{trigger_id}.field_search_ids"
        )
    field_searches = tuple(
        _text(value, f"{trigger_id}.field_search_ids[{index}]", empty=True)
        for index, value in enumerate(raw_field_searches)
    )
    field_count = len(fields)
    for name, vector in (
        ("field_checks", checks),
        ("field_values", field_values),
        ("field_search_ids", field_searches),
    ):
        if len(vector) not in (0, field_count):
            raise CompositeTriggerError("field-vector-shape", f"{trigger_id}:{name}")
    if field_count == 0 and (checks or field_values or field_searches):
        raise CompositeTriggerError("field-vector-shape", trigger_id)

    effective_checks = checks or (CHECK_DEFAULT,) * field_count
    effective_values: tuple[int | None, ...] = (
        field_values or (None,) * field_count
    )
    effective_searches = field_searches or ("",) * field_count
    predicates = tuple(
        CompositeFieldPredicate(
            field_type=field_type,
            check_type=check,
            value=value,
            card_search_id=search_id,
        )
        for field_type, check, value, search_id in zip(
            fields,
            effective_checks,
            effective_values,
            effective_searches,
            strict=True,
        )
    )

    for check in checks:
        if check not in _SUPPORTED_CHECKS:
            raise CompositeTriggerError("unknown-check-type", f"{trigger_id}:{check}")

    produce_card_search_id = _text(
        values["produce_card_search_id"],
        f"{trigger_id}.produce_card_search_id",
        empty=True,
    )
    upper = _int(values["upper_search_count"], f"{trigger_id}.upper", minimum=0)
    lower = _int(values["lower_search_count"], f"{trigger_id}.lower", minimum=0)
    move = _text(values["card_move_position_type"], f"{trigger_id}.card_move")
    effect_types = _tuple_text(values["effect_types"], f"{trigger_id}.effect_types")
    lesson = _text(values["lesson_type"], f"{trigger_id}.lesson_type")
    supplied_rule = values.get("card_search_rule")
    if card_search_rule is not None and supplied_rule is not None and supplied_rule != card_search_rule:
        raise CompositeTriggerError("card-search-rule-drift", trigger_id)
    selected_rule = card_search_rule if card_search_rule is not None else supplied_rule
    if selected_rule is not None and not isinstance(selected_rule, ProduceCardSearchRule):
        raise TypeError("card_search_rule must be ProduceCardSearchRule or None")

    return CompositeTriggerSpec(
        trigger_id=trigger_id,
        phase=phase_types[0],
        phase_values=phase_values,
        fields=predicates,
        produce_card_search_id=produce_card_search_id,
        upper_search_count=upper,
        lower_search_count=lower,
        card_move_position_type=move,
        effect_types=effect_types,
        lesson_type=lesson,
        card_search_rule=selected_rule,
    )


@dataclass(frozen=True, slots=True)
class CompositeTriggerResolution:
    supported: bool
    trigger: CompositeTriggerSpec | None
    reasons: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.trigger is None


def resolve_composite_trigger(
    raw: object,
    *,
    card_search_rule: ProduceCardSearchRule | None = None,
) -> CompositeTriggerResolution:
    """Return a non-throwing, fail-closed parse result for binding code."""

    try:
        trigger = parse_composite_trigger(raw, card_search_rule=card_search_rule)
    except (CompositeTriggerError, TypeError, ValueError) as error:
        code = getattr(error, "code", "parse-error")
        return CompositeTriggerResolution(False, None, (f"{code}:{error}",))
    return CompositeTriggerResolution(True, trigger)


@dataclass(frozen=True, slots=True)
class CompositeTriggerContext:
    """Immutable runtime evidence for one native trigger dispatch."""

    phase: str
    phase_values: tuple[int, ...] = ()
    field_values: Mapping[str, int | bool] = None  # type: ignore[assignment]
    field_search_values: Mapping[tuple[str, str], int | bool] = None  # type: ignore[assignment]
    card_search_matches: Mapping[str, bool] = None  # type: ignore[assignment]
    card_id: str = ""
    card_upgrade: int | None = None
    effect_types: tuple[str, ...] = ()
    card_move_position_type: str = CARD_MOVE_UNKNOWN
    lesson_type: str = LESSON_UNKNOWN

    def __post_init__(self) -> None:
        _text(self.phase, "context.phase")
        phase_values = tuple(self.phase_values)
        for index, value in enumerate(phase_values):
            _int(value, f"context.phase_values[{index}]", minimum=0)
        object.__setattr__(self, "phase_values", phase_values)
        for name in ("field_values", "field_search_values", "card_search_matches"):
            value = getattr(self, name)
            if value is None:
                value = {}
            if not isinstance(value, Mapping):
                raise TypeError(f"{name} must be a mapping")
            object.__setattr__(self, name, dict(value))
        _text(self.card_id, "context.card_id", empty=True)
        if self.card_upgrade is not None:
            _int(self.card_upgrade, "context.card_upgrade", minimum=0)
        effect_types = tuple(self.effect_types)
        if any(not isinstance(value, str) or not value for value in effect_types):
            raise CompositeTriggerError("invalid-string-array", "context.effect_types")
        object.__setattr__(self, "effect_types", effect_types)
        _text(self.card_move_position_type, "context.card_move_position_type")
        _text(self.lesson_type, "context.lesson_type")


@dataclass(frozen=True, slots=True)
class CompositeTriggerEvaluation:
    trigger_id: str
    supported: bool
    fires: bool | None
    reasons: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.fires is None


def _lookup_field(
    context: CompositeTriggerContext,
    predicate: CompositeFieldPredicate,
) -> tuple[object, bool]:
    if predicate.card_search_id:
        key = (predicate.field_type, predicate.card_search_id)
        if key in context.field_search_values:
            return context.field_search_values[key], True
        return None, False
    if predicate.field_type in context.field_values:
        return context.field_values[predicate.field_type], True
    return None, False


def _evaluate_field(
    predicate: CompositeFieldPredicate,
    actual: object,
) -> tuple[bool | None, str | None]:
    if predicate.value is None:
        if type(actual) is not bool:
            return None, "field-value-type-bool-required"
        result = actual
    else:
        if isinstance(actual, bool) or not isinstance(actual, int):
            return None, "field-value-type-int-required"
        result = actual >= predicate.value
    return (not result if predicate.check_not else result), None


def evaluate_composite_trigger(
    trigger: CompositeTriggerSpec,
    context: CompositeTriggerContext,
) -> CompositeTriggerEvaluation:
    """Evaluate every ordered trigger conjunct against supplied evidence.

    A missing field/search/effect/phase-value input is not interpreted as
    zero, false, or an empty card.  It yields ``fires=None`` and a reason so a
    caller cannot accidentally fire an unproven P-item listener.
    """

    if not isinstance(trigger, CompositeTriggerSpec):
        raise TypeError("trigger must be CompositeTriggerSpec")
    if not isinstance(context, CompositeTriggerContext):
        raise TypeError("context must be CompositeTriggerContext")
    reasons: list[str] = []
    if context.phase != trigger.phase:
        return CompositeTriggerEvaluation(trigger.trigger_id, True, False)
    if context.phase_values != trigger.phase_values:
        reasons.append("phase-values-mismatch")

    for index, predicate in enumerate(trigger.fields):
        actual, present = _lookup_field(context, predicate)
        if not present:
            reasons.append(f"field-missing:{index}:{predicate.field_type}")
            continue
        matched, issue = _evaluate_field(predicate, actual)
        if issue is not None:
            reasons.append(f"field-invalid:{index}:{predicate.field_type}:{issue}")
        elif matched is False:
            return CompositeTriggerEvaluation(trigger.trigger_id, True, False)

    if trigger.produce_card_search_id:
        matched: bool | None = None
        if trigger.produce_card_search_id in context.card_search_matches:
            value = context.card_search_matches[trigger.produce_card_search_id]
            if type(value) is not bool:
                reasons.append("card-search-match-not-bool")
            else:
                matched = value
        elif trigger.card_search_rule is not None and context.card_id:
            if context.card_upgrade is None:
                reasons.append("card-search-upgrade-missing")
            else:
                matched, issue = match_exact_target_card_search(
                    trigger.card_search_rule,
                    context.card_id,
                    context.card_upgrade,
                )
                if issue is not None:
                    reasons.append(f"card-search-unsupported:{issue}")
        else:
            reasons.append("card-search-evidence-missing")
        if matched is False:
            return CompositeTriggerEvaluation(trigger.trigger_id, True, False)

    if trigger.effect_types:
        if not context.effect_types:
            reasons.append("effect-types-evidence-missing")
        elif context.effect_types != trigger.effect_types:
            return CompositeTriggerEvaluation(trigger.trigger_id, True, False)
    if trigger.card_move_position_type != CARD_MOVE_UNKNOWN:
        if context.card_move_position_type == CARD_MOVE_UNKNOWN:
            reasons.append("card-move-evidence-missing")
        elif context.card_move_position_type != trigger.card_move_position_type:
            return CompositeTriggerEvaluation(trigger.trigger_id, True, False)
    if trigger.lesson_type != LESSON_UNKNOWN:
        if context.lesson_type == LESSON_UNKNOWN:
            reasons.append("lesson-evidence-missing")
        elif context.lesson_type != trigger.lesson_type:
            return CompositeTriggerEvaluation(trigger.trigger_id, True, False)
    if reasons:
        return CompositeTriggerEvaluation(
            trigger.trigger_id, True, None, tuple(dict.fromkeys(reasons))
        )
    return CompositeTriggerEvaluation(trigger.trigger_id, True, True)


__all__ = [
    "CARD_MOVE_UNKNOWN",
    "CHECK_DEFAULT",
    "CHECK_NOT",
    "CompositeFieldPredicate",
    "CompositeTriggerContext",
    "CompositeTriggerError",
    "CompositeTriggerEvaluation",
    "CompositeTriggerResolution",
    "CompositeTriggerSpec",
    "LESSON_UNKNOWN",
    "KNOWN_LESSON_TYPES",
    "KNOWN_PHASES",
    "NO_BLOCK_FIELD",
    "evaluate_composite_trigger",
    "parse_composite_trigger",
    "resolve_composite_trigger",
]
