"""Generic, typed dispatch contracts for Plan 2 mandatory P-item triggers.

The Master row for a mandatory idol item is an event listener, not an
item-specific special case.  This module turns the row into a small immutable
dispatch contract and evaluates it against one immutable event context.  It
intentionally stops at the predicate boundary: effect execution, listener
installation, usage counts, and state mutation remain owned by the existing
Plan 2 runtimes.

The contract covers the fourteen event phases used by the currently missing
Plan 2 mandatory-item families.  The parser is deliberately stricter than a
``dict.get`` based dispatcher: phase/field/search vectors remain positional,
Master order is retained, ``bool`` never masquerades as an integer, and an
unknown phase/field/operator/search observation yields a fail-closed result.
No idol-card or item ID is consulted when parsing or dispatching.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Final, Literal

from .card_search import (
    ProduceCardSearchRule,
    match_exact_playing_card_search,
    match_exact_target_card_context_search,
    match_exact_target_card_search,
)
from .plan2_pitem_composite_trigger import (
    CARD_MOVE_UNKNOWN,
    CHECK_DEFAULT,
    CHECK_NOT,
    CompositeFieldPredicate,
    CompositeTriggerError,
    CompositeTriggerSpec,
    LESSON_UNKNOWN,
    parse_composite_trigger,
)


class PItemEventDispatchError(ValueError):
    """A Master/event shape outside the generic P-item dispatch contract."""

    def __init__(self, code: str, detail: str = "") -> None:
        if type(code) is not str or not code:
            raise ValueError("dispatch error code must be non-empty text")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}:{detail}" if detail else code)


class PItemEventPhase(str, Enum):
    """Canonical native phases used by the 50 missing P-item families."""

    START_EXAM = "ProduceExamPhaseType_ExamStartExam"
    START_PLAY = "ProduceExamPhaseType_StartPlay"
    START_TURN = "ProduceExamPhaseType_ExamStartTurn"
    END_TURN = "ProduceExamPhaseType_ExamEndTurn"
    TURN_INTERVAL = "ProduceExamPhaseType_ExamTurnInterval"
    END_TURN_INTERVAL = "ProduceExamPhaseType_ExamEndTurnInterval"
    CARD_PLAY = "ProduceExamPhaseType_ExamCardPlay"
    CARD_PLAY_AFTER = "ProduceExamPhaseType_ExamCardPlayAfter"
    PLAY_COUNT_INTERVAL = "ProduceExamPhaseType_ExamPlayCountInterval"
    PLAY_TURN_COUNT_INTERVAL = "ProduceExamPhaseType_ExamPlayTurnCountInterval"
    STAMINA_REDUCE = "ProduceExamPhaseType_ExamStaminaReduce"
    BUFF_CONSUME = "ProduceExamPhaseType_ExamBuffConsume"
    AGGRESSIVE_UP_INTERVAL = "ProduceExamPhaseType_ExamAggressiveUpInterval"
    STATUS_CHANGE = "ProduceExamPhaseType_ExamStatusChange"


# Keep a tuple for deterministic catalog/report output and a set for parsing.
SUPPORTED_PITEM_EVENT_PHASES: Final[tuple[str, ...]] = tuple(
    phase.value for phase in PItemEventPhase
)
_SUPPORTED_PHASES: Final = frozenset(SUPPORTED_PITEM_EVENT_PHASES)

# These are the field-status members present in the imported Master schema.
# An allowlist is important here: an unrecognised field must not become a
# positive predicate merely because a caller happens to provide a same-named
# value in its context.
KNOWN_PITEM_FIELD_TYPES: Final = frozenset(
    {
        "ProduceExamFieldStatusType_BlockUp",
        "ProduceExamFieldStatusType_CardPlayAggressiveUp",
        "ProduceExamFieldStatusType_CardSearchCountUp",
        "ProduceExamFieldStatusType_ConcentrationChangeCountUp",
        "ProduceExamFieldStatusType_ConcentrationUp",
        "ProduceExamFieldStatusType_ConditionThresholdMultiple",
        "ProduceExamFieldStatusType_ConditionThresholdMultipleDown",
        "ProduceExamFieldStatusType_FullPowerChangeCountUp",
        "ProduceExamFieldStatusType_FullPowerPointGetSumUp",
        "ProduceExamFieldStatusType_FullPowerPointUp",
        "ProduceExamFieldStatusType_FullPowerUp",
        "ProduceExamFieldStatusType_LessonBuffUp",
        "ProduceExamFieldStatusType_NoBlock",
        "ProduceExamFieldStatusType_NoStance",
        "ProduceExamFieldStatusType_ParameterBuff",
        "ProduceExamFieldStatusType_ParameterBuffMultiplePerTurnUp",
        "ProduceExamFieldStatusType_ParameterBuffUp",
        "ProduceExamFieldStatusType_PlayCardLesson",
        "ProduceExamFieldStatusType_PlayCardSkill",
        "ProduceExamFieldStatusType_PreservationChangeCountUp",
        "ProduceExamFieldStatusType_PreservationUp",
        "ProduceExamFieldStatusType_RemainingTurn",
        "ProduceExamFieldStatusType_ReviewUp",
        "ProduceExamFieldStatusType_StaminaConsumptionDown",
        "ProduceExamFieldStatusType_StaminaLessMultiple",
        "ProduceExamFieldStatusType_StaminaUpMultiple",
        "ProduceExamFieldStatusType_StanceChangeCountUp",
        "ProduceExamFieldStatusType_TurnProgressUp",
    }
)

# Trigger effect filters are also Master enums.  This list is intentionally
# about *trigger filters*, not every possible child effect type.
KNOWN_PITEM_EFFECT_TYPES: Final = frozenset(
    {
        "ProduceExamEffectType_ExamBlock",
        "ProduceExamEffectType_ExamCardPlayAggressive",
        "ProduceExamEffectType_ExamConcentration",
        "ProduceExamEffectType_ExamFullPowerPoint",
        "ProduceExamEffectType_ExamLessonBuff",
        "ProduceExamEffectType_ExamLessonDependBlock",
        "ProduceExamEffectType_ExamParameterBuff",
        "ProduceExamEffectType_ExamReview",
    }
)

SearchReferenceKind = Literal["field", "produce"]


def _strict_text(value: object, label: str, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value):
        raise PItemEventDispatchError("invalid-text", label)
    return value


def _strict_int(value: object, label: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise PItemEventDispatchError("invalid-integer", label)
    if minimum is not None and value < minimum:
        raise PItemEventDispatchError("integer-below-minimum", label)
    return value


def _coerce_phase(value: object, label: str = "phase") -> str:
    if isinstance(value, PItemEventPhase):
        value = value.value
    phase = _strict_text(value, label)
    if phase not in _SUPPORTED_PHASES:
        raise PItemEventDispatchError("unsupported-phase", phase)
    return phase


@dataclass(frozen=True, slots=True)
class PItemCardSearchReference:
    """A typed reference to one Master card-search row.

    ``field_index`` identifies the positional field-status row for a field
    reference.  It is deliberately not inferred from the search ID because
    the same search row can be referenced by more than one trigger field.
    ``rule`` is optional: a native listener may provide a proved boolean
    result without loading the full search row, while a direct-card search can
    alternatively be evaluated from ``card_id``/``card_upgrade``.
    """

    search_id: str
    kind: SearchReferenceKind
    field_index: int | None = None
    upper_count: int = 0
    lower_count: int = 0
    rule: ProduceCardSearchRule | None = None

    def __post_init__(self) -> None:
        _strict_text(self.search_id, "search_id")
        if self.kind not in ("field", "produce"):
            raise PItemEventDispatchError("unknown-search-reference-kind", str(self.kind))
        if self.kind == "field":
            if type(self.field_index) is not int or self.field_index < 0:
                raise PItemEventDispatchError("field-search-index-required", self.search_id)
        elif self.field_index is not None:
            raise PItemEventDispatchError("produce-search-field-index", self.search_id)
        _strict_int(self.upper_count, "search.upper_count", minimum=0)
        _strict_int(self.lower_count, "search.lower_count", minimum=0)
        if self.rule is not None and not isinstance(self.rule, ProduceCardSearchRule):
            raise TypeError("rule must be ProduceCardSearchRule or None")
        if self.rule is not None and self.rule.id != self.search_id:
            raise PItemEventDispatchError("search-rule-id-mismatch", self.search_id)

    @property
    def is_field(self) -> bool:
        return self.kind == "field"

    @property
    def is_produce(self) -> bool:
        return self.kind == "produce"

    def to_dict(self) -> dict[str, object]:
        return {
            "searchId": self.search_id,
            "kind": self.kind,
            "fieldIndex": self.field_index,
            "upperCount": self.upper_count,
            "lowerCount": self.lower_count,
        }


# Friendly name used by callers that refer to observations as references.
TypedCardSearchReference = PItemCardSearchReference


@dataclass(frozen=True, slots=True)
class PItemCardSearchEvidence:
    """One typed native result for a card-search reference."""

    search_id: str
    matched: bool
    result_count: int | None = None
    card_id: str = ""
    card_upgrade: int | None = None

    def __post_init__(self) -> None:
        _strict_text(self.search_id, "evidence.search_id")
        if type(self.matched) is not bool:
            raise PItemEventDispatchError("search-match-not-bool", self.search_id)
        if self.result_count is not None:
            _strict_int(self.result_count, "evidence.result_count", minimum=0)
        _strict_text(self.card_id, "evidence.card_id", empty=True)
        if self.card_upgrade is not None:
            _strict_int(self.card_upgrade, "evidence.card_upgrade", minimum=0)
        if self.card_id and self.card_upgrade is None:
            raise PItemEventDispatchError(
                "evidence-card-upgrade-missing", self.search_id
            )
        if not self.card_id and self.card_upgrade is not None:
            raise PItemEventDispatchError(
                "evidence-card-id-missing", self.search_id
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "searchId": self.search_id,
            "matched": self.matched,
            "resultCount": self.result_count,
            "cardId": self.card_id,
            "cardUpgrade": self.card_upgrade,
        }


CardSearchObservation = PItemCardSearchEvidence


@dataclass(frozen=True, slots=True)
class PItemPredicateContext:
    """Immutable typed evidence at one native phase boundary.

    Field values intentionally retain ``int | bool`` until evaluation.  This
    lets a malformed bool-for-threshold observation be reported as a
    fail-closed evaluation instead of being silently coerced or accepted.
    """

    phase: str | PItemEventPhase
    phase_values: tuple[int, ...] = ()
    field_values: Mapping[str, object] = field(default_factory=dict)
    field_search_values: Mapping[tuple[str, str], object] = field(default_factory=dict)
    card_searches: Mapping[str, object] = field(default_factory=dict)
    # Compatibility spelling for callers already using the composite layer.
    card_search_matches: Mapping[str, object] = field(default_factory=dict)
    card_id: str = ""
    card_upgrade: int | None = None
    card_category: str = ""
    card_effect_group_ids: tuple[str, ...] = ()
    effect_types: tuple[str, ...] = ()
    card_move_position_type: str = CARD_MOVE_UNKNOWN
    lesson_type: str = LESSON_UNKNOWN

    def __post_init__(self) -> None:
        object.__setattr__(self, "phase", _coerce_phase(self.phase, "context.phase"))
        values = tuple(self.phase_values)
        for index, value in enumerate(values):
            _strict_int(value, f"context.phase_values[{index}]", minimum=0)
        object.__setattr__(self, "phase_values", values)

        for name in ("field_values", "field_search_values", "card_searches", "card_search_matches"):
            raw = getattr(self, name)
            if not isinstance(raw, Mapping):
                raise TypeError(f"{name} must be a mapping")
            object.__setattr__(self, name, dict(raw))

        for key in self.field_values:
            _strict_text(key, "context.field_values key")
        for key in self.field_search_values:
            if not isinstance(key, tuple) or len(key) != 2:
                raise PItemEventDispatchError("invalid-field-search-key", repr(key))
            _strict_text(key[0], "context.field_search_values field")
            _strict_text(key[1], "context.field_search_values search")
        for key in tuple(self.card_searches) + tuple(self.card_search_matches):
            _strict_text(key, "context.card_search key")

        _strict_text(self.card_id, "context.card_id", empty=True)
        if self.card_upgrade is not None:
            _strict_int(self.card_upgrade, "context.card_upgrade", minimum=0)
        _strict_text(self.card_category, "context.card_category", empty=True)
        card_effect_group_ids = tuple(self.card_effect_group_ids)
        if any(
            type(value) is not str or not value
            for value in card_effect_group_ids
        ):
            raise PItemEventDispatchError(
                "invalid-card-effect-groups",
                "context.card_effect_group_ids",
            )
        object.__setattr__(self, "card_effect_group_ids", card_effect_group_ids)
        effects = tuple(self.effect_types)
        if any(type(value) is not str or not value for value in effects):
            raise PItemEventDispatchError("invalid-effect-types", "context.effect_types")
        object.__setattr__(self, "effect_types", effects)
        _strict_text(self.card_move_position_type, "context.card_move_position_type")
        _strict_text(self.lesson_type, "context.lesson_type")

    @property
    def merged_card_searches(self) -> dict[str, object]:
        """Merge both accepted search-map spellings without overwriting proof."""

        merged = dict(self.card_searches)
        for search_id, value in self.card_search_matches.items():
            if search_id in merged and merged[search_id] != value:
                # Preserve an explicit conflict marker; evaluation will fail
                # closed rather than choosing one source by accident.
                merged[search_id] = _ConflictingEvidence()
            else:
                merged[search_id] = value
        return merged


# Private marker that cannot be mistaken for a native bool.
@dataclass(frozen=True, slots=True)
class _ConflictingEvidence:
    pass


Plan2PItemEventContext = PItemPredicateContext


@dataclass(frozen=True, slots=True)
class PItemEventDispatch:
    """One item-ID-independent, ordered Master event dispatch contract."""

    trigger: CompositeTriggerSpec
    phase: PItemEventPhase
    search_references: tuple[PItemCardSearchReference, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.trigger, CompositeTriggerSpec):
            raise TypeError("trigger must be CompositeTriggerSpec")
        if not isinstance(self.phase, PItemEventPhase):
            raise TypeError("phase must be PItemEventPhase")
        if self.trigger.phase != self.phase.value:
            raise PItemEventDispatchError("phase-trigger-mismatch", self.trigger.trigger_id)
        refs = tuple(self.search_references)
        if any(not isinstance(ref, PItemCardSearchReference) for ref in refs):
            raise TypeError("search_references must contain typed references")
        expected: list[tuple[str, int | None]] = []
        for index, predicate in enumerate(self.trigger.fields):
            if predicate.card_search_id:
                expected.append((predicate.card_search_id, index))
        if self.trigger.produce_card_search_id:
            expected.append((self.trigger.produce_card_search_id, None))
        actual = [(ref.search_id, ref.field_index) for ref in refs]
        if actual != expected:
            raise PItemEventDispatchError("search-reference-order", self.trigger.trigger_id)
        object.__setattr__(self, "search_references", refs)

    @property
    def trigger_id(self) -> str:
        return self.trigger.trigger_id

    @property
    def event_phase(self) -> PItemEventPhase:
        return self.phase

    @property
    def phase_name(self) -> str:
        return self.phase.name

    @property
    def event_family(self) -> str:
        return self.phase.name

    @property
    def phase_values(self) -> tuple[int, ...]:
        return self.trigger.phase_values

    @property
    def fields(self) -> tuple[CompositeFieldPredicate, ...]:
        return self.trigger.fields

    @property
    def field_predicates(self) -> tuple[CompositeFieldPredicate, ...]:
        return self.trigger.fields

    @property
    def effect_types(self) -> tuple[str, ...]:
        return self.trigger.effect_types

    @property
    def master_order(self) -> tuple[str, ...]:
        """Order of all positional predicate/search slots in the Master row."""

        order = [f"field[{index}]" for index in range(len(self.trigger.fields))]
        if self.trigger.produce_card_search_id:
            order.append("produce-card-search")
        return tuple(order)

    def to_dict(self) -> dict[str, object]:
        result = self.trigger.to_dict()
        result.update(
            {
                "phase": self.phase.value,
                "eventFamily": self.phase.name,
                "searchReferences": [ref.to_dict() for ref in self.search_references],
                "masterOrder": list(self.master_order),
            }
        )
        return result


Plan2PItemEventDispatch = PItemEventDispatch


@dataclass(frozen=True, slots=True)
class PItemDispatchResolution:
    supported: bool
    contract: PItemEventDispatch | None
    reasons: tuple[str, ...] = ()

    @property
    def dispatch(self) -> PItemEventDispatch | None:
        return self.contract

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.contract is None


@dataclass(frozen=True, slots=True)
class PItemDispatchEvaluation:
    trigger_id: str
    supported: bool
    fires: bool | None
    reasons: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.fires is None


Plan2PItemDispatchResolution = PItemDispatchResolution
Plan2PItemDispatchEvaluation = PItemDispatchEvaluation


def _validate_dispatch_shape(trigger: CompositeTriggerSpec) -> None:
    if trigger.phase not in _SUPPORTED_PHASES:
        raise PItemEventDispatchError("unsupported-phase", trigger.phase)
    for index, predicate in enumerate(trigger.fields):
        if predicate.field_type not in KNOWN_PITEM_FIELD_TYPES:
            raise PItemEventDispatchError(
                "unknown-field-type", f"{trigger.trigger_id}:{index}:{predicate.field_type}"
            )
        if predicate.check_type not in (CHECK_DEFAULT, CHECK_NOT):
            raise PItemEventDispatchError(
                "unknown-check-type", f"{trigger.trigger_id}:{predicate.check_type}"
            )
        if predicate.value is not None and type(predicate.value) is not int:
            raise PItemEventDispatchError("invalid-field-threshold", trigger.trigger_id)
        if predicate.value is not None and predicate.value < 0:
            raise PItemEventDispatchError("invalid-field-threshold", trigger.trigger_id)
    for effect_type in trigger.effect_types:
        if effect_type not in KNOWN_PITEM_EFFECT_TYPES:
            raise PItemEventDispatchError(
                "unknown-trigger-effect-type", f"{trigger.trigger_id}:{effect_type}"
            )
    if trigger.produce_card_search_id:
        if trigger.upper_search_count < 0 or trigger.lower_search_count < 0:
            raise PItemEventDispatchError("invalid-search-count", trigger.trigger_id)
    elif trigger.upper_search_count or trigger.lower_search_count:
        raise PItemEventDispatchError("search-count-without-reference", trigger.trigger_id)


def parse_plan2_pitem_event_dispatch(
    raw: object,
    *,
    card_search_rule: ProduceCardSearchRule | None = None,
    field_card_search_rules: Mapping[str, ProduceCardSearchRule] | None = None,
) -> PItemEventDispatch:
    """Parse one Master trigger into a typed generic event dispatch.

    ``field_card_search_rules`` is optional because native phase evidence can
    supply a proved counter/search result directly.  When supplied, every
    referenced row is checked for an exact ID match; unknown mappings are
    rejected instead of being ignored.
    """

    try:
        trigger = parse_composite_trigger(raw, card_search_rule=card_search_rule)
    except (CompositeTriggerError, TypeError, ValueError) as error:
        raise PItemEventDispatchError(
            getattr(error, "code", "parse-error"), str(error)
        ) from error
    _validate_dispatch_shape(trigger)

    rules = field_card_search_rules
    if rules is not None and not isinstance(rules, Mapping):
        raise TypeError("field_card_search_rules must be a mapping or None")
    if rules is not None:
        expected_rule_ids = {
            predicate.card_search_id
            for predicate in trigger.fields
            if predicate.card_search_id
        }
        extra_rule_ids = set(rules).difference(expected_rule_ids)
        if extra_rule_ids:
            raise PItemEventDispatchError(
                "field-search-rule-extra", ",".join(sorted(extra_rule_ids))
            )
    refs: list[PItemCardSearchReference] = []
    for index, predicate in enumerate(trigger.fields):
        if not predicate.card_search_id:
            continue
        rule = None if rules is None else rules.get(predicate.card_search_id)
        if rules is not None:
            if rule is None:
                raise PItemEventDispatchError("field-search-rule-missing", predicate.card_search_id)
            if not isinstance(rule, ProduceCardSearchRule):
                raise TypeError("field_card_search_rules values must be ProduceCardSearchRule")
        refs.append(
            PItemCardSearchReference(
                search_id=predicate.card_search_id,
                kind="field",
                field_index=index,
                rule=rule,
            )
        )
    if trigger.produce_card_search_id:
        refs.append(
            PItemCardSearchReference(
                search_id=trigger.produce_card_search_id,
                kind="produce",
                upper_count=trigger.upper_search_count,
                lower_count=trigger.lower_search_count,
                rule=trigger.card_search_rule,
            )
        )
    return PItemEventDispatch(
        trigger=trigger,
        phase=PItemEventPhase(trigger.phase),
        search_references=tuple(refs),
    )


def resolve_plan2_pitem_event_dispatch(
    raw: object,
    *,
    card_search_rule: ProduceCardSearchRule | None = None,
    field_card_search_rules: Mapping[str, ProduceCardSearchRule] | None = None,
) -> PItemDispatchResolution:
    """Return a non-throwing fail-closed parse result."""

    try:
        contract = parse_plan2_pitem_event_dispatch(
            raw,
            card_search_rule=card_search_rule,
            field_card_search_rules=field_card_search_rules,
        )
    except (PItemEventDispatchError, CompositeTriggerError, TypeError, ValueError) as error:
        code = getattr(error, "code", "parse-error")
        return PItemDispatchResolution(False, None, (f"{code}:{error}",))
    return PItemDispatchResolution(True, contract)


def _typed_search_matches(
    context: PItemPredicateContext,
) -> tuple[dict[str, object], tuple[str, ...]]:
    matches: dict[str, object] = {}
    reasons: list[str] = []
    for search_id, evidence in context.merged_card_searches.items():
        if isinstance(evidence, PItemCardSearchEvidence):
            if evidence.search_id != search_id:
                reasons.append(f"card-search-reference-mismatch:{search_id}")
                continue
            matches[search_id] = evidence.matched
        elif type(evidence) is bool:
            matches[search_id] = evidence
        else:
            reasons.append(f"card-search-evidence-invalid:{search_id}")
    return matches, tuple(reasons)


def _validate_typed_search_bounds(
    dispatch: PItemEventDispatch,
    context: PItemPredicateContext,
) -> tuple[str, ...]:
    """Validate optional result-count/card-key evidence without coercion."""

    reasons: list[str] = []
    evidence = context.merged_card_searches
    for reference in dispatch.search_references:
        value = evidence.get(reference.search_id)
        if not isinstance(value, PItemCardSearchEvidence):
            continue
        if reference.rule is not None and value.card_id:
            matched, issue = match_exact_target_card_search(
                reference.rule,
                value.card_id,
                value.card_upgrade if value.card_upgrade is not None else -1,
            )
            if issue is not None:
                reasons.append(f"card-search-unsupported:{issue}")
            elif matched is not value.matched:
                reasons.append(f"card-search-match-conflict:{reference.search_id}")
        if value.result_count is None:
            continue
        if reference.lower_count and value.result_count < reference.lower_count:
            reasons.append(f"card-search-result-count-below-lower:{reference.search_id}")
        if reference.upper_count and value.result_count > reference.upper_count:
            reasons.append(f"card-search-result-count-above-upper:{reference.search_id}")
    return tuple(dict.fromkeys(reasons))


def _lookup_field_value(
    context: PItemPredicateContext, predicate: CompositeFieldPredicate
) -> tuple[object, bool]:
    if predicate.card_search_id:
        key = (predicate.field_type, predicate.card_search_id)
        if key in context.field_search_values:
            return context.field_search_values[key], True
        return None, False
    if predicate.field_type in context.field_values:
        return context.field_values[predicate.field_type], True
    return None, False


def _evaluate_pitem_field(
    predicate: CompositeFieldPredicate, actual: object
) -> tuple[bool | None, str | None]:
    """Evaluate one native field status with strict scalar typing.

    Most Master field-status thresholds are positive ``>=`` comparisons.
    ``RemainingTurn`` is the native terminal-window exception: its trigger
    shape is ``RemainTurn <= N`` (and ``Not`` therefore means ``> N``).
    """

    if predicate.value is None:
        if type(actual) is not bool:
            return None, "field-value-type-bool-required"
        result = actual
    else:
        if type(actual) is not int:
            return None, "field-value-type-int-required"
        if predicate.field_type == "ProduceExamFieldStatusType_RemainingTurn":
            result = actual <= predicate.value
        else:
            result = actual >= predicate.value
    return (not result if predicate.check_not else result), None


def _evaluate_dispatch_predicates(
    dispatch: PItemEventDispatch,
    context: PItemPredicateContext,
    matches: Mapping[str, object],
) -> tuple[bool | None, tuple[str, ...]]:
    """Evaluate the ordered trigger conjunction without coercion."""

    trigger = dispatch.trigger
    reasons: list[str] = []
    if context.phase != trigger.phase:
        return False, ()
    if context.phase_values != trigger.phase_values:
        reasons.append("phase-values-mismatch")

    for index, predicate in enumerate(trigger.fields):
        actual, present = _lookup_field_value(context, predicate)
        if not present:
            reasons.append(f"field-missing:{index}:{predicate.field_type}")
            continue
        matched, issue = _evaluate_pitem_field(predicate, actual)
        if issue is not None:
            reasons.append(f"field-invalid:{index}:{predicate.field_type}:{issue}")
        elif matched is False:
            return False, tuple(dict.fromkeys(reasons))

    if trigger.produce_card_search_id:
        matched: bool | None = None
        search_id = trigger.produce_card_search_id
        if search_id in matches:
            value = matches[search_id]
            if type(value) is not bool:
                reasons.append("card-search-match-not-bool")
            else:
                matched = value
        elif trigger.card_search_rule is not None and context.card_id:
            if context.card_upgrade is None:
                reasons.append("card-search-upgrade-missing")
            else:
                search_rule = trigger.card_search_rule
                if (
                    search_rule.card_position_type
                    == "ProduceCardPositionType_Playing"
                ):
                    matched, issue = match_exact_playing_card_search(
                        search_rule,
                        card_category=context.card_category,
                        card_effect_group_ids=context.card_effect_group_ids,
                        # The search row supplies its own allowed category and
                        # effect-group filters.  The shared matcher validates
                        # all remaining Playing-search fields generically.
                        expected_categories=search_rule.card_categories,
                        expected_effect_group_ids=search_rule.effect_group_ids,
                    )
                elif context.card_category or context.card_effect_group_ids:
                    matched, issue = match_exact_target_card_context_search(
                        search_rule,
                        card_id=context.card_id,
                        upgrade_count=context.card_upgrade,
                        card_category=context.card_category,
                        card_effect_group_ids=context.card_effect_group_ids,
                    )
                else:
                    matched, issue = match_exact_target_card_search(
                        search_rule,
                        context.card_id,
                        context.card_upgrade,
                    )
                if issue is not None:
                    reasons.append(f"card-search-unsupported:{issue}")
        else:
            reasons.append("card-search-evidence-missing")
        if matched is False:
            return False, tuple(dict.fromkeys(reasons))

    if trigger.effect_types:
        if not context.effect_types:
            reasons.append("effect-types-evidence-missing")
        elif context.effect_types != trigger.effect_types:
            return False, tuple(dict.fromkeys(reasons))
    if trigger.card_move_position_type != CARD_MOVE_UNKNOWN:
        if context.card_move_position_type == CARD_MOVE_UNKNOWN:
            reasons.append("card-move-evidence-missing")
        elif context.card_move_position_type != trigger.card_move_position_type:
            return False, tuple(dict.fromkeys(reasons))
    if trigger.lesson_type != LESSON_UNKNOWN:
        if context.lesson_type == LESSON_UNKNOWN:
            reasons.append("lesson-evidence-missing")
        elif context.lesson_type != trigger.lesson_type:
            return False, tuple(dict.fromkeys(reasons))
    if reasons:
        return None, tuple(dict.fromkeys(reasons))
    return True, ()


def evaluate_plan2_pitem_event_dispatch(
    dispatch: PItemEventDispatch,
    context: PItemPredicateContext,
) -> PItemDispatchEvaluation:
    """Evaluate one dispatch against typed event evidence.

    ``fires=None`` is the only result for incomplete/invalid evidence.  A
    missing field, phase value, or search observation is never interpreted as
    zero/false.  A proved predicate mismatch returns ``fires=False``.
    """

    if not isinstance(dispatch, PItemEventDispatch):
        raise TypeError("dispatch must be PItemEventDispatch")
    if not isinstance(context, PItemPredicateContext):
        raise TypeError("context must be PItemPredicateContext")
    matches, evidence_reasons = _typed_search_matches(context)
    evidence_reasons = tuple(
        dict.fromkeys(
            evidence_reasons
            + _validate_typed_search_bounds(dispatch, context)
        )
    )
    evaluated, evaluated_reasons = _evaluate_dispatch_predicates(
        dispatch, context, matches
    )
    if evaluated is False:
        return PItemDispatchEvaluation(
            dispatch.trigger_id,
            True,
            False,
            tuple(dict.fromkeys(evaluated_reasons + evidence_reasons)),
        )
    if evaluated is None or evidence_reasons:
        return PItemDispatchEvaluation(
            dispatch.trigger_id,
            True,
            None,
            tuple(dict.fromkeys(evaluated_reasons + evidence_reasons)),
        )
    return PItemDispatchEvaluation(dispatch.trigger_id, True, True)


# Short aliases make this layer convenient to use beside the existing
# ``resolve_composite_trigger`` naming without hiding the explicit Plan2 API.
parse_pitem_event_dispatch = parse_plan2_pitem_event_dispatch
resolve_pitem_event_dispatch = resolve_plan2_pitem_event_dispatch
evaluate_pitem_event_dispatch = evaluate_plan2_pitem_event_dispatch
dispatch_plan2_pitem_event = evaluate_plan2_pitem_event_dispatch
parse_event_dispatch = parse_plan2_pitem_event_dispatch
resolve_event_dispatch = resolve_plan2_pitem_event_dispatch
evaluate_event_dispatch = evaluate_plan2_pitem_event_dispatch
PItemEventContext = PItemPredicateContext
Plan2PItemTriggerContext = PItemPredicateContext
PItemTriggerDispatch = PItemEventDispatch


__all__ = [
    "CardSearchObservation",
    "KNOWN_PITEM_EFFECT_TYPES",
    "KNOWN_PITEM_FIELD_TYPES",
    "PItemCardSearchEvidence",
    "PItemCardSearchReference",
    "PItemDispatchEvaluation",
    "PItemDispatchResolution",
    "PItemEventDispatch",
    "PItemEventContext",
    "PItemEventDispatchError",
    "PItemEventPhase",
    "PItemPredicateContext",
    "PItemTriggerDispatch",
    "Plan2PItemDispatchEvaluation",
    "Plan2PItemDispatchResolution",
    "Plan2PItemEventContext",
    "Plan2PItemEventDispatch",
    "Plan2PItemTriggerContext",
    "SUPPORTED_PITEM_EVENT_PHASES",
    "TypedCardSearchReference",
    "dispatch_plan2_pitem_event",
    "evaluate_pitem_event_dispatch",
    "evaluate_event_dispatch",
    "evaluate_plan2_pitem_event_dispatch",
    "parse_pitem_event_dispatch",
    "parse_event_dispatch",
    "parse_plan2_pitem_event_dispatch",
    "resolve_pitem_event_dispatch",
    "resolve_event_dispatch",
    "resolve_plan2_pitem_event_dispatch",
]
