"""Fail-closed, typed resolution for the two executable Plan 3 CardPlay shapes.

This module deliberately owns only the narrow ``ExamCardPlay`` listener shapes
that are proved by the local Master rows and native field layout.  In
particular, ``ExamCardPlayAfter`` is not accepted here, and
``StaminaUpMultiple`` is retained as an unresolved shape because its threshold
semantics are not proved by the available typed evidence.

The resolver is pure: it returns the same Plan3 and native-state objects as
``before``/``after`` and never consumes a card, changes a counter, or applies an
effect.  The intended core hook is before the CardPlay transaction moves the
playing card out of Hand.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
import json
from typing import TYPE_CHECKING, Any, Literal

from .card_search import (
    exact_playing_card_search_mismatches,
    match_exact_playing_card_search,
)
from .playing_card_identity import resolve_playing_card_identity

if TYPE_CHECKING:
    from .card_search import ProduceCardSearchRule
    from .plan3_engine import Plan3Card, Plan3State, Plan3Trigger
    from .plan3_native_state import Plan3NativeState
else:
    # Keep runtime imports independent from the optional YAML-backed loader;
    # static type checkers still resolve the concrete existing classes above.
    Plan3Card = Any
    Plan3State = Any
    Plan3Trigger = Any
    Plan3NativeState = Any
    ProduceCardSearchRule = Any


# These literals intentionally remain local.  Importing plan3_engine would
# make this narrow resolver depend on the YAML loader even when a caller only
# needs static trigger-shape resolution.
PHASE_CARD_PLAY = "ProduceExamPhaseType_ExamCardPlay"
PHASE_CARD_PLAY_AFTER = "ProduceExamPhaseType_ExamCardPlayAfter"
POSITION_PLAYING = "ProduceCardPositionType_Playing"

CATEGORY_MENTAL_SKILL = "ProduceCardCategory_MentalSkill"
FIELD_CONCENTRATION_UP = "ProduceExamFieldStatusType_ConcentrationUp"
FIELD_STAMINA_UP_MULTIPLE = "ProduceExamFieldStatusType_StaminaUpMultiple"

SEARCH_MENTAL_SKILL_PLAYING = "p_card_search-mental_skill-playing"
SEARCH_PLAYING_EFFECT_GROUP_CONCENTRATION = (
    "p_card_search-playing-effect_group-visible-exam_concentration-000"
)
EFFECT_GROUP_CONCENTRATION = "effect_group-visible-exam_concentration-000"

MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"
LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"
SEARCH_STATUS_UNKNOWN = "ProduceCardSearchStatusType_Unknown"
SEARCH_ORDER_UNKNOWN = "ProduceCardOrderType_Unknown"
SEARCH_MIN_MAX_UNKNOWN = "ConditionMinMaxType_Unknown"
SEARCH_EXAM_EFFECT_UNKNOWN = "ProduceExamEffectType_Unknown"
SEARCH_COST_UNKNOWN = "ExamCostType_Unknown"

STANCE_CONCENTRATION = "concentration"

SHAPE_MENTAL_PLAYING_CONCENTRATION: Literal[
    "mental_skill_playing_concentration"
] = "mental_skill_playing_concentration"
SHAPE_PLAYING_EFFECT_GROUP_CONCENTRATION: Literal[
    "playing_effect_group_concentration"
] = "playing_effect_group_concentration"
SHAPE_STAMINA_UP_MULTIPLE: Literal["stamina_up_multiple"] = "stamina_up_multiple"
SHAPE_UNRESOLVED: Literal["unresolved"] = "unresolved"

EXECUTABLE_SHAPES = frozenset(
    {
        SHAPE_MENTAL_PLAYING_CONCENTRATION,
        SHAPE_PLAYING_EFFECT_GROUP_CONCENTRATION,
    }
)

# Current coverage inventory, kept as explicit static facts rather than used
# to broaden validity.  The 12 affected versions are three unique cards x four
# upgrades; only the two Plan3 shapes represented by the first two constants
# below are sole blockers (8 versions total).
AFFECTED_CARD_VERSION_COUNT = 12
SOLE_UNLOCK_CARD_VERSION_COUNT = 8

_MISSING = object()


@dataclass(frozen=True, slots=True, order=True)
class Plan3CardPlayUnresolved:
    """One explicit reason why a CardPlay decision remains unresolved."""

    field: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class Plan3CardPlayTriggerContract:
    """Immutable trigger/search pair with an exact shape classification.

    ``trigger`` and ``search`` are the existing frozen typed rows.  Keeping the
    rows intact preserves their declared field order and all trigger/search
    identifiers; ``to_dict`` converts them to JSON-friendly ordered mappings.
    ``search`` is ``None`` only for a trigger with no search id.
    """

    trigger: Plan3Trigger
    search: ProduceCardSearchRule | None
    shape: str
    unresolved: tuple[Plan3CardPlayUnresolved, ...] = ()

    @property
    def trigger_id(self) -> str:
        return self.trigger.id

    @property
    def search_id(self) -> str:
        return self.trigger.produce_card_search_id

    @property
    def supported(self) -> bool:
        return self.shape in EXECUTABLE_SHAPES and not self.unresolved

    @property
    def resolved(self) -> bool:
        return self.supported

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "search_id": self.search_id,
            "shape": self.shape,
            "trigger": _plain(self.trigger),
            "search": _plain(self.search),
            "unresolved": [item.to_dict() for item in self.unresolved],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class Plan3CardPlayDecision:
    """A pure decision for one known playing GUID at one explicit phase."""

    contract: Plan3CardPlayTriggerContract
    event_phase: str
    playing_guid: str
    supported: bool
    fires: bool | None
    unresolved: tuple[Plan3CardPlayUnresolved, ...] = ()

    @property
    def trigger_id(self) -> str:
        return self.contract.trigger_id

    @property
    def search_id(self) -> str:
        return self.contract.search_id

    @property
    def shape(self) -> str:
        return self.contract.shape

    @property
    def resolved(self) -> bool:
        return self.supported and not self.unresolved

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "search_id": self.search_id,
            "shape": self.shape,
            "event_phase": self.event_phase,
            "playing_guid": self.playing_guid,
            "supported": self.supported,
            "fires": self.fires,
            "contract": self.contract.to_dict(),
            "unresolved": [item.to_dict() for item in self.unresolved],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class Plan3CardPlayResult:
    """Decision plus identity-preserving before/after state boundaries."""

    before: Plan3State
    after: Plan3State
    native_before: Plan3NativeState
    native_after: Plan3NativeState
    decision: Plan3CardPlayDecision

    @property
    def fires(self) -> bool | None:
        return self.decision.fires

    @property
    def supported(self) -> bool:
        return self.decision.supported

    @property
    def state_unchanged(self) -> bool:
        return self.before == self.after

    @property
    def native_state_unchanged(self) -> bool:
        return self.native_before == self.native_after

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.to_dict(),
            "state_before": _plain(self.before),
            "state_after": _plain(self.after),
            "native_state_before": _plain(self.native_before),
            "native_state_after": _plain(self.native_after),
            "state_unchanged": self.state_unchanged,
            "native_state_unchanged": self.native_state_unchanged,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


def _plain(value: Any) -> Any:
    """Convert frozen dataclasses/tuples into JSON-friendly values."""

    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _plain(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


def _require_fields(value: Any, names: tuple[str, ...], label: str) -> None:
    missing = tuple(name for name in names if not hasattr(value, name))
    if missing:
        joined = ", ".join(missing)
        raise TypeError(f"{label} is missing typed fields: {joined}")


_TRIGGER_FIELDS = (
    "id",
    "phase_types",
    "phase_values",
    "field_check_types",
    "field_types",
    "field_values",
    "field_card_search_ids",
    "produce_card_search_id",
    "upper_search_count",
    "lower_search_count",
    "card_move_position_type",
    "effect_types",
    "lesson_type",
)

_SEARCH_FIELDS = (
    "id",
    "card_rarities",
    "produce_card_ids",
    "upgrade_counts",
    "plan_type",
    "card_categories",
    "card_status_type",
    "order_type",
    "card_position_type",
    "card_search_tag",
    "produce_card_random_pool_id",
    "limit_count",
    "stamina_min_max_type",
    "stamina_min",
    "stamina_max",
    "exam_effect_type",
    "effect_group_ids",
    "is_self",
    "produce_card_pool_id",
    "cost_type",
    "is_customized",
)


def _error(
    errors: list[Plan3CardPlayUnresolved],
    field: str,
    reason: str,
) -> None:
    item = Plan3CardPlayUnresolved(field, reason)
    if item not in errors:
        errors.append(item)


def _trigger_base_is_card_play(trigger: Plan3Trigger) -> bool:
    return (
        trigger.phase_types == (PHASE_CARD_PLAY,)
        and trigger.phase_values == ()
        and trigger.field_check_types == ()
        and trigger.field_card_search_ids == ()
        and trigger.card_move_position_type == MOVE_UNKNOWN
        and trigger.effect_types == ()
        and trigger.lesson_type == LESSON_UNKNOWN
    )


def _search_shape_errors(
    search: ProduceCardSearchRule,
    *,
    expected_categories: tuple[str, ...],
    expected_effect_groups: tuple[str, ...],
) -> tuple[Plan3CardPlayUnresolved, ...]:
    """Validate the complete neutral search grammar, preserving tuple order."""
    errors: list[Plan3CardPlayUnresolved] = []
    for field_name in exact_playing_card_search_mismatches(
        search,
        expected_categories=expected_categories,
        expected_effect_group_ids=expected_effect_groups,
    ):
        _error(
            errors,
            f"search.{field_name}",
            f"search-shape:{search.id}",
        )
    return tuple(errors)


def resolve_plan3_card_play_trigger(
    trigger: Plan3Trigger,
    search: ProduceCardSearchRule | None = None,
) -> Plan3CardPlayTriggerContract:
    """Classify one typed Master trigger/search pair without guessing formulas.

    The only supported shapes are:

    * CardPlay + ``MentalSkill`` playing search + ConcentrationUp value 2;
    * CardPlay + playing search constrained to the exact concentration effect
      group.

    ``StaminaUpMultiple`` value 500 is recognized for inventory reporting but
    remains unresolved because the threshold/multiple semantics are not
    represented by a proved typed field in this contract.
    """

    _require_fields(trigger, _TRIGGER_FIELDS, "Plan3Trigger")
    if search is not None:
        _require_fields(search, _SEARCH_FIELDS, "ProduceCardSearchRule")

    trigger_id = trigger.id
    errors: list[Plan3CardPlayUnresolved] = []

    if not isinstance(trigger_id, str) or not trigger_id:
        _error(errors, "trigger.id", "trigger-id-invalid")

    if trigger.phase_types == (PHASE_CARD_PLAY_AFTER,):
        _error(
            errors,
            "phase_types",
            f"card-play-after-is-separate-phase:{trigger_id}",
        )
    elif trigger.phase_types != (PHASE_CARD_PLAY,):
        _error(errors, "phase_types", f"trigger-phase-shape:{trigger_id}")

    if trigger.phase_values != ():
        _error(errors, "phase_values", f"trigger-phase-values:{trigger_id}")
    if trigger.field_check_types != ():
        _error(errors, "field_check_types", f"trigger-field-check:{trigger_id}")
    if trigger.field_card_search_ids != ():
        _error(errors, "field_card_search_ids", f"trigger-card-search:{trigger_id}")
    if trigger.card_move_position_type != MOVE_UNKNOWN:
        _error(
            errors,
            "card_move_position_type",
            f"trigger-move-position:{trigger_id}",
        )
    if trigger.effect_types != ():
        _error(errors, "effect_types", f"trigger-effect-filter:{trigger_id}")
    if trigger.lesson_type != LESSON_UNKNOWN:
        _error(errors, "lesson_type", f"trigger-lesson-type:{trigger_id}")

    if trigger.field_types not in {
        (),
        (FIELD_CONCENTRATION_UP,),
        (FIELD_STAMINA_UP_MULTIPLE,),
    }:
        _error(errors, "field_types", f"trigger-field:{trigger_id}")
    if trigger.field_types == (FIELD_CONCENTRATION_UP,) and trigger.field_values != (2,):
        _error(errors, "field_values", f"trigger-concentration-threshold:{trigger_id}")
    if (
        trigger.field_types == (FIELD_STAMINA_UP_MULTIPLE,)
        and trigger.field_values != (500,)
    ):
        _error(errors, "field_values", f"trigger-stamina-multiple:{trigger_id}")

    if not isinstance(trigger.upper_search_count, int) or isinstance(
        trigger.upper_search_count, bool
    ):
        _error(errors, "upper_search_count", f"trigger-search-count:{trigger_id}")
    if not isinstance(trigger.lower_search_count, int) or isinstance(
        trigger.lower_search_count, bool
    ):
        _error(errors, "lower_search_count", f"trigger-search-count:{trigger_id}")

    search_id = trigger.produce_card_search_id
    if search_id and search is None:
        _error(errors, "search", f"search-row-unavailable:{search_id}")
    elif not search_id and search is not None:
        _error(errors, "search", f"search-row-unexpected:{trigger_id}")
    elif search_id and search is not None and search.id != search_id:
        _error(errors, "search.id", f"search-id-mismatch:{trigger_id}")

    base = _trigger_base_is_card_play(trigger)
    mental_candidate = (
        base
        and trigger.field_types == (FIELD_CONCENTRATION_UP,)
        and trigger.field_values == (2,)
        and trigger.produce_card_search_id == SEARCH_MENTAL_SKILL_PLAYING
        and trigger.upper_search_count == 0
        and trigger.lower_search_count == 1
    )
    group_candidate = (
        base
        and trigger.field_types == ()
        and trigger.field_values == ()
        and trigger.produce_card_search_id
        == SEARCH_PLAYING_EFFECT_GROUP_CONCENTRATION
        and trigger.upper_search_count == 0
        and trigger.lower_search_count == 1
    )
    stamina_candidate = (
        base
        and trigger.field_types == (FIELD_STAMINA_UP_MULTIPLE,)
        and trigger.field_values == (500,)
        and trigger.produce_card_search_id == ""
        and trigger.upper_search_count == 0
        and trigger.lower_search_count == 0
    )

    if mental_candidate:
        shape = SHAPE_MENTAL_PLAYING_CONCENTRATION
        if search is not None and search.id == search_id:
            errors.extend(
                _search_shape_errors(
                    search,
                    expected_categories=(CATEGORY_MENTAL_SKILL,),
                    expected_effect_groups=(),
                )
            )
    elif group_candidate:
        shape = SHAPE_PLAYING_EFFECT_GROUP_CONCENTRATION
        if search is not None and search.id == search_id:
            errors.extend(
                _search_shape_errors(
                    search,
                    expected_categories=(),
                    expected_effect_groups=(EFFECT_GROUP_CONCENTRATION,),
                )
            )
    elif stamina_candidate:
        shape = SHAPE_STAMINA_UP_MULTIPLE
        _error(
            errors,
            "stamina_multiple_formula",
            f"stamina-multiple-formula-unproven:{trigger_id}",
        )
    else:
        shape = SHAPE_UNRESOLVED

    # Count and interval are part of the exact trigger shape.  A candidate
    # with any other count must never silently fall through to a match.
    if mental_candidate or group_candidate:
        if (trigger.upper_search_count, trigger.lower_search_count) != (0, 1):
            _error(errors, "search_count", f"trigger-search-count:{trigger_id}")
    elif not stamina_candidate and (
        trigger.upper_search_count != 0 or trigger.lower_search_count != 0
    ):
        _error(errors, "search_count", f"trigger-search-count:{trigger_id}")

    # Preserve first-seen evidence order while removing duplicate diagnostics.
    unique_errors = tuple(dict.fromkeys(errors))
    return Plan3CardPlayTriggerContract(
        trigger=trigger,
        search=search,
        shape=shape,
        unresolved=unique_errors,
    )


def _decision(
    contract: Plan3CardPlayTriggerContract,
    *,
    event_phase: str,
    playing_guid: str,
    supported: bool,
    fires_value: bool | None,
    errors: list[Plan3CardPlayUnresolved] | tuple[Plan3CardPlayUnresolved, ...] = (),
) -> Plan3CardPlayDecision:
    combined = tuple(dict.fromkeys((*contract.unresolved, *errors)))
    return Plan3CardPlayDecision(
        contract=contract,
        event_phase=event_phase,
        playing_guid=playing_guid,
        supported=supported,
        fires=fires_value,
        unresolved=combined,
    )


def evaluate_plan3_card_play_trigger(
    trigger_or_contract: Plan3Trigger | Plan3CardPlayTriggerContract,
    state: Plan3State,
    native_state: Plan3NativeState,
    playing_guid: str,
    playing_card: Plan3Card,
    *,
    event_phase: str = PHASE_CARD_PLAY,
    search: ProduceCardSearchRule | None = None,
) -> Plan3CardPlayResult:
    """Evaluate one CardPlay event using only already-known state evidence.

    ``playing_guid`` must identify a native Hand card whose id/effective
    upgrade agrees with ``playing_card``.  A missing or inconsistent native
    identity is unresolved; it is never treated as a non-match.  CardPlayAfter
    is likewise unresolved even when the trigger text looks similar.
    """

    if isinstance(trigger_or_contract, Plan3CardPlayTriggerContract):
        contract = trigger_or_contract
    else:
        contract = resolve_plan3_card_play_trigger(trigger_or_contract, search)

    errors: list[Plan3CardPlayUnresolved] = []
    if not contract.supported:
        decision = _decision(
            contract,
            event_phase=event_phase,
            playing_guid=playing_guid,
            supported=False,
            fires_value=None,
        )
        return Plan3CardPlayResult(state, state, native_state, native_state, decision)

    if event_phase == PHASE_CARD_PLAY_AFTER:
        _error(
            errors,
            "event_phase",
            f"card-play-after-is-separate-phase:{contract.trigger_id}",
        )
    elif event_phase != PHASE_CARD_PLAY:
        _error(errors, "event_phase", f"event-phase-unresolved:{contract.trigger_id}")

    if not isinstance(playing_guid, str) or not playing_guid:
        _error(errors, "playing_guid", f"playing-guid-invalid:{contract.trigger_id}")

    if not errors:
        identity = resolve_playing_card_identity(
            native_state,
            playing_guid,
            playing_card,
            reason_context=contract.trigger_id,
        )
        for issue in identity.issues:
            _error(errors, issue.field, issue.reason)

    if errors:
        decision = _decision(
            contract,
            event_phase=event_phase,
            playing_guid=playing_guid,
            supported=False,
            fires_value=None,
            errors=errors,
        )
        return Plan3CardPlayResult(state, state, native_state, native_state, decision)

    if contract.shape == SHAPE_MENTAL_PLAYING_CONCENTRATION:
        category = getattr(playing_card, "category", _MISSING)
        stance = getattr(state, "stance", _MISSING)
        stance_level = getattr(state, "stance_level", _MISSING)
        if category is _MISSING or stance is _MISSING or stance_level is _MISSING:
            _error(errors, "state-or-playing-card", f"typed-fields-unavailable:{contract.trigger_id}")
            fires_value: bool | None = None
        elif not isinstance(stance_level, int) or isinstance(stance_level, bool):
            _error(errors, "stance_level", f"concentration-threshold-invalid:{contract.trigger_id}")
            fires_value = None
        else:
            search_match, search_reason = match_exact_playing_card_search(
                contract.search,
                card_category=category,
                expected_categories=(CATEGORY_MENTAL_SKILL,),
                expected_effect_group_ids=(),
            )
            if search_reason is not None:
                _error(errors, "search", search_reason)
                fires_value = None
            else:
                fires_value = (
                    search_match
                    and stance == STANCE_CONCENTRATION
                    and stance_level >= 2
                )
    elif contract.shape == SHAPE_PLAYING_EFFECT_GROUP_CONCENTRATION:
        category = getattr(playing_card, "category", _MISSING)
        groups = getattr(playing_card, "effect_group_ids", _MISSING)
        if category is _MISSING or groups is _MISSING:
            _error(errors, "playing_card.effect_group_ids", f"effect-group-unavailable:{contract.trigger_id}")
            fires_value = None
        else:
            try:
                group_tuple = tuple(groups)
            except TypeError:
                group_tuple = ()
                search_reason = f"effect-group-invalid:{contract.trigger_id}"
            else:
                fires_value, search_reason = match_exact_playing_card_search(
                    contract.search,
                    card_category=category,
                    card_effect_group_ids=group_tuple,
                    expected_categories=(),
                    expected_effect_group_ids=(EFFECT_GROUP_CONCENTRATION,),
                )
            if search_reason is not None:
                _error(errors, "playing_card.effect_group_ids", search_reason)
                fires_value = None
    else:
        # The contract currently gates this branch, but retain a fail-closed
        # guard so adding a future shape cannot accidentally become executable.
        _error(errors, "shape", f"trigger-card-play-shape:{contract.trigger_id}")
        fires_value = None

    decision = _decision(
        contract,
        event_phase=event_phase,
        playing_guid=playing_guid,
        supported=not errors,
        fires_value=fires_value,
        errors=errors,
    )
    return Plan3CardPlayResult(state, state, native_state, native_state, decision)


def fires(
    trigger_or_contract: Plan3Trigger | Plan3CardPlayTriggerContract,
    state: Plan3State,
    native_state: Plan3NativeState,
    playing_guid: str,
    playing_card: Plan3Card,
    *,
    event_phase: str = PHASE_CARD_PLAY,
    search: ProduceCardSearchRule | None = None,
) -> bool | None:
    """Return ``True``/``False`` when proved, otherwise ``None``."""

    return evaluate_plan3_card_play_trigger(
        trigger_or_contract,
        state,
        native_state,
        playing_guid,
        playing_card,
        event_phase=event_phase,
        search=search,
    ).fires


__all__ = [
    "AFFECTED_CARD_VERSION_COUNT",
    "CATEGORY_MENTAL_SKILL",
    "EFFECT_GROUP_CONCENTRATION",
    "EXECUTABLE_SHAPES",
    "FIELD_CONCENTRATION_UP",
    "FIELD_STAMINA_UP_MULTIPLE",
    "PHASE_CARD_PLAY",
    "PHASE_CARD_PLAY_AFTER",
    "POSITION_PLAYING",
    "SEARCH_MENTAL_SKILL_PLAYING",
    "SEARCH_PLAYING_EFFECT_GROUP_CONCENTRATION",
    "SHAPE_MENTAL_PLAYING_CONCENTRATION",
    "SHAPE_PLAYING_EFFECT_GROUP_CONCENTRATION",
    "SHAPE_STAMINA_UP_MULTIPLE",
    "SHAPE_UNRESOLVED",
    "SOLE_UNLOCK_CARD_VERSION_COUNT",
    "Plan3CardPlayDecision",
    "Plan3CardPlayResult",
    "Plan3CardPlayTriggerContract",
    "Plan3CardPlayUnresolved",
    "evaluate_plan3_card_play_trigger",
    "fires",
    "resolve_plan3_card_play_trigger",
]
