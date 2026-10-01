"""Bounded, fail-closed resolver for the Plan 3 CardPlayAfter target row.

This module owns exactly one current-coverage family:

``e_trigger-exam_card_play_after-p_card_search-target-effect_group-visible-``
``exam_concentration-000-0_1``

The trigger is deliberately separate from :mod:`plan3_card_play_trigger`.
That module resolves ``ExamCardPlay`` and its ``Playing`` searches; this one
resolves ``ExamCardPlayAfter`` and the exact ``Target`` search.  The two
phases have different native queue boundaries and must never be coerced into
one another.

The pure evaluator uses two explicit GUIDs:

* ``event_guid`` is the card instance carrying the per-card
  ``CardPlayAfter`` listener (the current affected card family is
  ``p_card-03-ido-3_110`` + upgrades 0..3).
* ``target_guid`` is the card instance supplied to the trigger's ``Target``
  search.  Its typed Master card must expose the exact concentration effect
  group.

``UserCardAfterCheck`` builds the post-play command list before
``MovePlayCard`` but appends those commands after the already queued move.
The evaluator therefore requires the caller to identify the settled
CardPlayAfter boundary with ``move_settled=True``.  It only returns a
decision; neither supplied state is mutated.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
import json
from typing import TYPE_CHECKING, Any, Literal, Mapping

if TYPE_CHECKING:
    from .card_search import ProduceCardSearchRule
    from .plan3_engine import Plan3Card, Plan3State, Plan3Trigger
    from .plan3_native_state import Plan3NativeState
else:
    # Keep this boundary independent from the YAML-backed engine loader.
    Plan3Card = Any
    Plan3State = Any
    Plan3Trigger = Any
    Plan3NativeState = Any
    ProduceCardSearchRule = Any


PHASE_CARD_PLAY = "ProduceExamPhaseType_ExamCardPlay"
PHASE_CARD_PLAY_AFTER = "ProduceExamPhaseType_ExamCardPlayAfter"

POSITION_TARGET = "ProduceCardPositionType_Target"
POSITION_PLAYING = "ProduceCardPositionType_Playing"
MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"
LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"
SEARCH_STATUS_UNKNOWN = "ProduceCardSearchStatusType_Unknown"
SEARCH_ORDER_UNKNOWN = "ProduceCardOrderType_Unknown"
SEARCH_MIN_MAX_UNKNOWN = "ConditionMinMaxType_Unknown"
SEARCH_EXAM_EFFECT_UNKNOWN = "ProduceExamEffectType_Unknown"
SEARCH_COST_UNKNOWN = "ExamCostType_Unknown"
PLAN_UNKNOWN = "ProducePlanType_Unknown"

CARD_ID_IDO_3_110 = "p_card-03-ido-3_110"
CARD_UPGRADES_IDO_3_110 = (0, 1, 2, 3)
AFFECTED_CARD_VERSIONS = tuple(
    (CARD_ID_IDO_3_110, upgrade) for upgrade in CARD_UPGRADES_IDO_3_110
)
AFFECTED_CARD_VERSION_COUNT = 4
SOLE_UNLOCK_CARD_VERSION_COUNT = 4

TRIGGER_ID_CARD_PLAY_AFTER_EFFECT_GROUP = (
    "e_trigger-exam_card_play_after-p_card_search-target-"
    "effect_group-visible-exam_concentration-000-0_1"
)
SEARCH_TARGET_EFFECT_GROUP_CONCENTRATION = (
    "p_card_search-target-effect_group-visible-exam_concentration-000"
)
# This is the same effect group with the intentionally different Playing
# search used by the separate ExamCardPlay family.
SEARCH_PLAYING_EFFECT_GROUP_CONCENTRATION = (
    "p_card_search-playing-effect_group-visible-exam_concentration-000"
)
EFFECT_GROUP_CONCENTRATION = "effect_group-visible-exam_concentration-000"

SHAPE_TARGET_EFFECT_GROUP_CONCENTRATION: Literal[
    "target_effect_group_concentration"
] = "target_effect_group_concentration"
SHAPE_UNRESOLVED: Literal["unresolved"] = "unresolved"

CARD_PLAY_AFTER_ORDER = (
    "card-effects",
    "user-card-after-check-build",
    "final-zone-move",
    "card-play-after-post-play-commands",
)

_MISSING = object()


@dataclass(frozen=True, slots=True, order=True)
class Plan3CardPlayAfterUnresolved:
    """One explicit reason why this bounded decision cannot be resolved."""

    field: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class Plan3CardPlayAfterEvidence:
    """A local evidence locator for the narrow resolver contract."""

    source: str
    locator: str
    claim: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "locator": self.locator,
            "claim": self.claim,
        }


STATIC_EVIDENCE = (
    Plan3CardPlayAfterEvidence(
        "coverage",
        "var/coverage/plan3_card_executable_coverage.json:1008-1038",
        "The sole current trigger-card-play-after-shape gap affects four Plan3 versions of p_card-03-ido-3_110.",
    ),
    Plan3CardPlayAfterEvidence(
        "master-diff",
        "_research/gakumasu-diff/ProduceExamTrigger.yaml:10882-10893",
        "The trigger is ExamCardPlayAfter with Target search and upperSearchCount=0/lowerSearchCount=1.",
    ),
    Plan3CardPlayAfterEvidence(
        "master-diff",
        "_research/gakumasu-diff/ProduceCardSearch.yaml:6103-6120,9013-9030",
        "Playing and Target are separate search rows; both carry the same exact concentration effect group.",
    ),
    Plan3CardPlayAfterEvidence(
        "master-sqlite",
        "var/master.sqlite3:produce_card_search and produce_exam_trigger target rows",
        "The imported typed rows retain Target, exact effectGroup, and the 0/1 bounds without prose inference.",
    ),
    Plan3CardPlayAfterEvidence(
        "android-order",
        "docs/android-v323-card-transaction-order-report.md:5-26,376-395",
        "Android command order builds UserCardAfterCheck before MovePlayCard but executes its appended CardPlayAfter commands after the final-zone move.",
    ),
    Plan3CardPlayAfterEvidence(
        "android-plan3-order",
        "docs/android-v323-plan3-static-trigger-order.md:59-92",
        "The ordinary Hand transaction resolves the final zone before ExamCardPlayAfter commands.",
    ),
    Plan3CardPlayAfterEvidence(
        "pc-compatibility",
        "docs/android-pc-exam-metadata-compatibility.md:1-20",
        "Current PC and Android retain equal ordered ExamSequence metadata shape; this is structural cross-version evidence, not live execution.",
    ),
)


@dataclass(frozen=True, slots=True)
class Plan3CardPlayAfterTriggerContract:
    """Immutable exact trigger/search projection for this family."""

    trigger: Plan3Trigger
    search: ProduceCardSearchRule | None
    shape: str
    unresolved: tuple[Plan3CardPlayAfterUnresolved, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "unresolved", tuple(self.unresolved))

    @property
    def trigger_id(self) -> str:
        return str(getattr(self.trigger, "id", ""))

    @property
    def search_id(self) -> str:
        return str(getattr(self.trigger, "produce_card_search_id", ""))

    @property
    def supported(self) -> bool:
        return self.shape == SHAPE_TARGET_EFFECT_GROUP_CONCENTRATION and not self.unresolved

    @property
    def resolved(self) -> bool:
        return self.supported

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "search_id": self.search_id,
            "shape": self.shape,
            "supported": self.supported,
            "trigger": _jsonable(self.trigger),
            "search": _jsonable(self.search),
            "unresolved": [item.to_dict() for item in self.unresolved],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class Plan3CardPlayAfterDecision:
    """Pure fires decision for one explicit event/target GUID pair."""

    contract: Plan3CardPlayAfterTriggerContract
    event_phase: str
    move_settled: bool
    event_guid: str
    target_guid: str
    event_card_id: str
    event_card_upgrade: int | None
    target_card_id: str
    target_card_upgrade: int | None
    supported: bool
    fires: bool | None
    unresolved: tuple[Plan3CardPlayAfterUnresolved, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "unresolved", tuple(self.unresolved))

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
            "move_settled": self.move_settled,
            "event_guid": self.event_guid,
            "target_guid": self.target_guid,
            "event_card_id": self.event_card_id,
            "event_card_upgrade": self.event_card_upgrade,
            "target_card_id": self.target_card_id,
            "target_card_upgrade": self.target_card_upgrade,
            "supported": self.supported,
            "fires": self.fires,
            "contract": self.contract.to_dict(),
            "unresolved": [item.to_dict() for item in self.unresolved],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class Plan3CardPlayAfterResult:
    """Decision plus identity-preserving state boundaries."""

    before: Plan3State
    after: Plan3State
    native_before: Plan3NativeState
    native_after: Plan3NativeState
    decision: Plan3CardPlayAfterDecision

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
            "state_before": _jsonable(self.before),
            "state_after": _jsonable(self.after),
            "native_state_before": _jsonable(self.native_before),
            "native_state_after": _jsonable(self.native_after),
            "state_unchanged": self.state_unchanged,
            "native_state_unchanged": self.native_state_unchanged,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _jsonable(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        converted = [_jsonable(item) for item in value]
        return sorted(
            converted,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _add_error(
    errors: list[Plan3CardPlayAfterUnresolved], field: str, reason: str
) -> None:
    item = Plan3CardPlayAfterUnresolved(field, reason)
    if item not in errors:
        errors.append(item)


def _row_fields(
    value: Any,
    names: tuple[str, ...],
    label: str,
    errors: list[Plan3CardPlayAfterUnresolved],
) -> bool:
    missing = tuple(name for name in names if not hasattr(value, name))
    if missing:
        _add_error(
            errors,
            label,
            f"typed-fields-missing:{','.join(missing)}",
        )
        return False
    return True


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


def _search_shape_errors(
    search: ProduceCardSearchRule,
    errors: list[Plan3CardPlayAfterUnresolved],
) -> None:
    expected: tuple[tuple[str, Any], ...] = (
        ("id", SEARCH_TARGET_EFFECT_GROUP_CONCENTRATION),
        ("card_rarities", ()),
        ("produce_card_ids", ()),
        ("upgrade_counts", ()),
        ("plan_type", PLAN_UNKNOWN),
        ("card_categories", ()),
        ("card_status_type", SEARCH_STATUS_UNKNOWN),
        ("order_type", SEARCH_ORDER_UNKNOWN),
        ("card_position_type", POSITION_TARGET),
        ("card_search_tag", ""),
        ("produce_card_random_pool_id", ""),
        ("limit_count", 0),
        ("stamina_min_max_type", SEARCH_MIN_MAX_UNKNOWN),
        ("stamina_min", 0),
        ("stamina_max", 0),
        ("exam_effect_type", SEARCH_EXAM_EFFECT_UNKNOWN),
        ("effect_group_ids", (EFFECT_GROUP_CONCENTRATION,)),
        ("is_self", False),
        ("produce_card_pool_id", ""),
        ("cost_type", SEARCH_COST_UNKNOWN),
        ("is_customized", False),
    )
    for field_name, expected_value in expected:
        actual = getattr(search, field_name, _MISSING)
        if actual != expected_value:
            if field_name == "card_position_type" and actual == POSITION_PLAYING:
                reason = "playing-search-is-not-target-search"
            elif field_name == "effect_group_ids":
                reason = "search-effect-group-not-exact"
            else:
                reason = f"search-shape:{SEARCH_TARGET_EFFECT_GROUP_CONCENTRATION}"
            _add_error(errors, f"search.{field_name}", reason)


def resolve_plan3_card_play_after_trigger(
    trigger: Plan3Trigger,
    search: ProduceCardSearchRule | None = None,
) -> Plan3CardPlayAfterTriggerContract:
    """Admit only the exact current Target/effect-group trigger row."""

    errors: list[Plan3CardPlayAfterUnresolved] = []
    trigger_complete = _row_fields(trigger, _TRIGGER_FIELDS, "trigger", errors)
    if not trigger_complete:
        return Plan3CardPlayAfterTriggerContract(
            trigger=trigger,
            search=search,
            shape=SHAPE_UNRESOLVED,
            unresolved=tuple(errors),
        )

    trigger_id = getattr(trigger, "id")
    if trigger_id != TRIGGER_ID_CARD_PLAY_AFTER_EFFECT_GROUP:
        _add_error(errors, "trigger.id", "trigger-id-outside-bounded-family")

    phase_types = getattr(trigger, "phase_types")
    if phase_types == (PHASE_CARD_PLAY,):
        _add_error(errors, "phase_types", "card-play-is-not-card-play-after")
    elif phase_types != (PHASE_CARD_PLAY_AFTER,):
        _add_error(errors, "phase_types", "trigger-phase-is-not-card-play-after")

    expected_trigger: tuple[tuple[str, Any], ...] = (
        ("phase_values", ()),
        ("field_check_types", ()),
        ("field_types", ()),
        ("field_values", ()),
        ("field_card_search_ids", ()),
        ("produce_card_search_id", SEARCH_TARGET_EFFECT_GROUP_CONCENTRATION),
        ("upper_search_count", 0),
        ("lower_search_count", 1),
        ("card_move_position_type", MOVE_UNKNOWN),
        ("effect_types", ()),
        ("lesson_type", LESSON_UNKNOWN),
    )
    for field_name, expected_value in expected_trigger:
        actual = getattr(trigger, field_name, _MISSING)
        if actual != expected_value:
            if field_name in {"upper_search_count", "lower_search_count"}:
                reason = "trigger-search-bounds-not-0-1"
            elif field_name == "produce_card_search_id":
                reason = "trigger-search-id-not-target-effect-group"
            else:
                reason = f"trigger-shape:{TRIGGER_ID_CARD_PLAY_AFTER_EFFECT_GROUP}"
            _add_error(errors, field_name, reason)

    search_id = getattr(trigger, "produce_card_search_id")
    if search_id and search is None:
        _add_error(errors, "search", "target-search-row-unavailable")
    elif not search_id and search is not None:
        _add_error(errors, "search", "unexpected-search-row")
    elif search is not None:
        if not _row_fields(search, _SEARCH_FIELDS, "search", errors):
            pass
        elif search.id != search_id:
            _add_error(errors, "search.id", "search-id-mismatch")
        else:
            _search_shape_errors(search, errors)

    unique_errors = tuple(dict.fromkeys(errors))
    shape = (
        SHAPE_TARGET_EFFECT_GROUP_CONCENTRATION
        if not unique_errors
        else SHAPE_UNRESOLVED
    )
    return Plan3CardPlayAfterTriggerContract(
        trigger=trigger,
        search=search,
        shape=shape,
        unresolved=unique_errors,
    )


def _typed_card_identity(
    card: Any,
    label: str,
    errors: list[Plan3CardPlayAfterUnresolved],
) -> tuple[str, int | None]:
    card_id = getattr(card, "id", _MISSING)
    upgrade = getattr(card, "upgrade", _MISSING)
    if not isinstance(card_id, str) or not card_id:
        _add_error(errors, f"{label}.id", "card-id-unavailable")
        card_id = ""
    if not isinstance(upgrade, int) or isinstance(upgrade, bool) or upgrade < 0:
        _add_error(errors, f"{label}.upgrade", "card-upgrade-unavailable")
        upgrade_value: int | None = None
    else:
        upgrade_value = upgrade
    return card_id, upgrade_value


def _native_lookup(
    native_state: Any,
    guid: str,
    label: str,
    errors: list[Plan3CardPlayAfterUnresolved],
) -> Any:
    lookup = getattr(native_state, "card_by_guid", None)
    if not callable(lookup):
        _add_error(errors, "native_state", "guid-lookup-unavailable")
        return _MISSING
    try:
        return lookup(guid)
    except Exception:
        _add_error(errors, label, "guid-not-found")
        return _MISSING


def _state_has_ref(
    state: Any,
    card_id: str,
    upgrade: int | None,
    label: str,
    errors: list[Plan3CardPlayAfterUnresolved],
) -> None:
    zone_names = ("hand", "draw_pile", "discard_pile", "lost_pile", "hold_pile")
    if any(not hasattr(state, name) for name in zone_names):
        _add_error(errors, "state", "plan3-zone-identity-unavailable")
        return
    try:
        refs = tuple(
            ref
            for name in zone_names
            for ref in tuple(getattr(state, name))
        )
    except (TypeError, ValueError):
        _add_error(errors, "state", "plan3-zone-identity-invalid")
        return
    if not any(
        getattr(ref, "card_id", _MISSING) == card_id
        and getattr(ref, "upgrade", _MISSING) == upgrade
        for ref in refs
    ):
        _add_error(errors, label, "card-ref-not-in-plan3-state")


def _status_is_exact(
    native_card: Any,
    errors: list[Plan3CardPlayAfterUnresolved],
) -> Any:
    status = getattr(native_card, "runtime_grow_status", _MISSING)
    if status is _MISSING or status is None:
        _add_error(errors, "event_native_card.runtime_grow_status", "listener-status-unavailable")
        return _MISSING
    expected = (
        ("trigger_id", TRIGGER_ID_CARD_PLAY_AFTER_EFFECT_GROUP),
        ("trigger_kind", "card_play_after_effect_group"),
        ("search_id", SEARCH_TARGET_EFFECT_GROUP_CONCENTRATION),
        ("target_effect_group_ids", (EFFECT_GROUP_CONCENTRATION,)),
    )
    for field_name, expected_value in expected:
        actual = getattr(status, field_name, _MISSING)
        if actual != expected_value:
            _add_error(
                errors,
                f"event_native_card.runtime_grow_status.{field_name}",
                "listener-status-not-exact",
            )
    for field_name in ("trigger_count", "spend_count"):
        actual = getattr(status, field_name, _MISSING)
        if (
            not isinstance(actual, int)
            or isinstance(actual, bool)
            or actual < 0
        ):
            _add_error(
                errors,
                f"event_native_card.runtime_grow_status.{field_name}",
                "listener-count-unavailable",
            )
    return status


def _decision(
    contract: Plan3CardPlayAfterTriggerContract,
    *,
    event_phase: str,
    move_settled: bool,
    event_guid: str,
    target_guid: str,
    event_card_id: str,
    event_card_upgrade: int | None,
    target_card_id: str,
    target_card_upgrade: int | None,
    fires_value: bool | None,
    errors: list[Plan3CardPlayAfterUnresolved] | tuple[Plan3CardPlayAfterUnresolved, ...] = (),
) -> Plan3CardPlayAfterDecision:
    combined = tuple(dict.fromkeys((*contract.unresolved, *errors)))
    return Plan3CardPlayAfterDecision(
        contract=contract,
        event_phase=event_phase,
        move_settled=move_settled,
        event_guid=event_guid,
        target_guid=target_guid,
        event_card_id=event_card_id,
        event_card_upgrade=event_card_upgrade,
        target_card_id=target_card_id,
        target_card_upgrade=target_card_upgrade,
        supported=contract.supported and not errors,
        fires=fires_value,
        unresolved=combined,
    )


def evaluate_plan3_card_play_after_trigger(
    trigger_or_contract: Plan3Trigger | Plan3CardPlayAfterTriggerContract,
    state: Plan3State,
    native_state: Plan3NativeState,
    event_guid: str,
    target_guid: str,
    event_card: Plan3Card,
    target_card: Plan3Card,
    *,
    event_phase: str = PHASE_CARD_PLAY_AFTER,
    move_settled: bool = True,
    search: ProduceCardSearchRule | None = None,
) -> Plan3CardPlayAfterResult:
    """Return a pure fires decision for one exact event/target card pair.

    ``event_card`` is the typed Master card owning ``event_guid``'s runtime
    listener; ``target_card`` is the typed Master card owning ``target_guid``.
    Missing or inconsistent identity is unresolved, never a false match.
    """

    if isinstance(trigger_or_contract, Plan3CardPlayAfterTriggerContract):
        contract = trigger_or_contract
    else:
        contract = resolve_plan3_card_play_after_trigger(trigger_or_contract, search)

    errors: list[Plan3CardPlayAfterUnresolved] = []
    event_card_id, event_card_upgrade = _typed_card_identity(
        event_card, "event_card", errors
    )
    target_card_id, target_card_upgrade = _typed_card_identity(
        target_card, "target_card", errors
    )

    if not contract.supported:
        decision = _decision(
            contract,
            event_phase=event_phase,
            move_settled=move_settled,
            event_guid=event_guid,
            target_guid=target_guid,
            event_card_id=event_card_id,
            event_card_upgrade=event_card_upgrade,
            target_card_id=target_card_id,
            target_card_upgrade=target_card_upgrade,
            fires_value=None,
            errors=errors,
        )
        return Plan3CardPlayAfterResult(state, state, native_state, native_state, decision)

    if event_phase == PHASE_CARD_PLAY:
        _add_error(errors, "event_phase", "card-play-is-not-card-play-after")
    elif event_phase != PHASE_CARD_PLAY_AFTER:
        _add_error(errors, "event_phase", "event-phase-not-card-play-after")

    if not isinstance(move_settled, bool):
        _add_error(errors, "move_settled", "move-boundary-invalid")
    elif not move_settled:
        _add_error(errors, "move_settled", "card-play-after-before-final-zone-move")

    for guid, label in ((event_guid, "event_guid"), (target_guid, "target_guid")):
        if not isinstance(guid, str) or not guid:
            _add_error(errors, label, "guid-invalid")

    if event_card_id != CARD_ID_IDO_3_110 or event_card_upgrade not in CARD_UPGRADES_IDO_3_110:
        _add_error(errors, "event_card", "event-card-outside-current-coverage")
    if event_card_upgrade is not None and event_card_upgrade not in CARD_UPGRADES_IDO_3_110:
        _add_error(errors, "event_card.upgrade", "event-card-upgrade-outside-current-coverage")

    # The scalar state is the settled Plan3 boundary; require its exact card
    # references but do not infer a GUID from a zone or from card order.
    if state is None:
        _add_error(errors, "state", "plan3-state-unavailable")
    else:
        _state_has_ref(state, event_card_id, event_card_upgrade, "event_card", errors)
        _state_has_ref(state, target_card_id, target_card_upgrade, "target_card", errors)

    event_native = _MISSING
    target_native = _MISSING
    if not errors:
        event_native = _native_lookup(native_state, event_guid, "event_guid", errors)
        if target_guid == event_guid:
            target_native = event_native
        else:
            target_native = _native_lookup(native_state, target_guid, "target_guid", errors)

    if not errors:
        for native, card_id, upgrade, label in (
            (event_native, event_card_id, event_card_upgrade, "event_card"),
            (target_native, target_card_id, target_card_upgrade, "target_card"),
        ):
            if (
                getattr(native, "card_id", _MISSING) != card_id
                or getattr(native, "effective_upgrade", _MISSING) != upgrade
            ):
                _add_error(errors, label, "native-card-identity-mismatch")

    status = _MISSING
    if not errors:
        status = _status_is_exact(event_native, errors)

    groups = _MISSING
    if not errors:
        groups = getattr(target_card, "effect_group_ids", _MISSING)
        if isinstance(groups, (str, bytes)) or not isinstance(groups, (tuple, list)):
            _add_error(errors, "target_card.effect_group_ids", "effect-group-unavailable")
        else:
            groups = tuple(groups)
            if any(not isinstance(value, str) or not value for value in groups):
                _add_error(errors, "target_card.effect_group_ids", "effect-group-invalid")

    fires_value: bool | None
    if errors:
        fires_value = None
    else:
        trigger_count = status.trigger_count
        spend_count = status.spend_count
        if trigger_count and spend_count >= trigger_count:
            fires_value = False
        else:
            fires_value = EFFECT_GROUP_CONCENTRATION in groups

    decision = _decision(
        contract,
        event_phase=event_phase,
        move_settled=move_settled,
        event_guid=event_guid,
        target_guid=target_guid,
        event_card_id=event_card_id,
        event_card_upgrade=event_card_upgrade,
        target_card_id=target_card_id,
        target_card_upgrade=target_card_upgrade,
        fires_value=fires_value,
        errors=errors,
    )
    return Plan3CardPlayAfterResult(state, state, native_state, native_state, decision)


def fires(
    trigger_or_contract: Plan3Trigger | Plan3CardPlayAfterTriggerContract,
    state: Plan3State,
    native_state: Plan3NativeState,
    event_guid: str,
    target_guid: str,
    event_card: Plan3Card,
    target_card: Plan3Card,
    *,
    event_phase: str = PHASE_CARD_PLAY_AFTER,
    move_settled: bool = True,
    search: ProduceCardSearchRule | None = None,
) -> bool | None:
    """Return True/False only for the proved exact boundary; else None."""

    return evaluate_plan3_card_play_after_trigger(
        trigger_or_contract,
        state,
        native_state,
        event_guid,
        target_guid,
        event_card,
        target_card,
        event_phase=event_phase,
        move_settled=move_settled,
        search=search,
    ).fires


# Short aliases make the boundary convenient without introducing a second
# implementation or broadening the accepted shape.
resolve = resolve_plan3_card_play_after_trigger
evaluate = evaluate_plan3_card_play_after_trigger


__all__ = [
    "AFFECTED_CARD_VERSION_COUNT",
    "AFFECTED_CARD_VERSIONS",
    "CARD_ID_IDO_3_110",
    "CARD_PLAY_AFTER_ORDER",
    "CARD_UPGRADES_IDO_3_110",
    "EFFECT_GROUP_CONCENTRATION",
    "PHASE_CARD_PLAY",
    "PHASE_CARD_PLAY_AFTER",
    "POSITION_PLAYING",
    "POSITION_TARGET",
    "SEARCH_PLAYING_EFFECT_GROUP_CONCENTRATION",
    "SEARCH_TARGET_EFFECT_GROUP_CONCENTRATION",
    "SHAPE_TARGET_EFFECT_GROUP_CONCENTRATION",
    "SHAPE_UNRESOLVED",
    "SOLE_UNLOCK_CARD_VERSION_COUNT",
    "STATIC_EVIDENCE",
    "TRIGGER_ID_CARD_PLAY_AFTER_EFFECT_GROUP",
    "Plan3CardPlayAfterDecision",
    "Plan3CardPlayAfterEvidence",
    "Plan3CardPlayAfterResult",
    "Plan3CardPlayAfterTriggerContract",
    "Plan3CardPlayAfterUnresolved",
    "evaluate",
    "evaluate_plan3_card_play_after_trigger",
    "fires",
    "resolve",
    "resolve_plan3_card_play_after_trigger",
]
