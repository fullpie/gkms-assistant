"""Independent resolver for the bounded ``None`` field-trigger family.

The local Master row is deliberately kept separate from the runtime dispatch
context.  ``ProduceExamPhaseType_None`` is the row's phase token; it is not a
wildcard runtime event.  Native evidence places this card-owned effect at the
``PlayEffect`` dispatch between the card-play listener work and the later
post-play/move work.  The source must therefore be supplied explicitly.

This module is a pure, fail-closed boundary.  It does not consume a card or
apply the playable-value effect.  A future core hook can call ``fires`` at the
direct card-effect slot and apply the already-loaded effect only when the
returned value is ``True``.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
import json
from typing import Any

from .plan3_engine import Plan3Card, Plan3State, Plan3Trigger
from .plan3_native_state import Plan3NativeState


# Exact local Master tokens for this one row/effect pair.
TRIGGER_ID = "e_trigger-none-not-preservation_up"
PHASE_NONE = "ProduceExamPhaseType_None"
CHECK_NOT = "ProduceExamTriggerCheckType_Not"
FIELD_PRESERVATION_UP = "ProduceExamFieldStatusType_PreservationUp"
PHASE_UNKNOWN_MOVE = "ProduceCardMovePositionType_Unknown"
LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"
PLAN3 = "ProducePlanType_Plan3"
CARD_ID = "p_card-03-act-2_067"

EFFECT_ID = "e_effect-exam_playable_value_add-01"
EFFECT_TYPE = "ProduceExamEffectType_ExamPlayableValueAdd"
EFFECT_COUNT = 1
EFFECT_ONCE = False

# ``None`` is not accepted as a runtime phase.  The Android command order
# evidence names the actual direct card-effect command ``PlayEffect``; the
# source token keeps it distinct from ExamCardPlay status listeners and from
# ExamCardPlayAfter/post-move work.
DISPATCH_PHASE_CARD_PLAY_EFFECT = "PlayEffect"
DISPATCH_SOURCE_CARD_DIRECT_EFFECT = "card-direct-effect"

# Readable aliases for callers that prefer the shorter names.
DISPATCH_PHASE = DISPATCH_PHASE_CARD_PLAY_EFFECT
DISPATCH_SOURCE = DISPATCH_SOURCE_CARD_DIRECT_EFFECT
SOURCE_CARD_DIRECT_EFFECT = DISPATCH_SOURCE_CARD_DIRECT_EFFECT

STANCE_PRESERVATION = "preservation"


class TriggerResolution(str, Enum):
    """Classification of the typed contract."""

    INDEPENDENT_RESOLVED = "independent-resolved"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True, order=True)
class AffectedCardVersion:
    """One current coverage version in the bounded family."""

    card_id: str
    upgrade: int

    def __post_init__(self) -> None:
        if not isinstance(self.card_id, str) or not self.card_id:
            raise ValueError("card_id must be a non-empty string")
        if type(self.upgrade) is not int or self.upgrade < 0:
            raise ValueError("upgrade must be a non-negative plain integer")

    @property
    def version(self) -> str:
        return f"{self.card_id}+{self.upgrade}"

    def to_dict(self) -> dict[str, Any]:
        return {"card_id": self.card_id, "upgrade": self.upgrade}


AFFECTED_CARD_VERSIONS: tuple[AffectedCardVersion, ...] = tuple(
    AffectedCardVersion(CARD_ID, upgrade) for upgrade in range(4)
)
SOLE_UNLOCK_CARD_VERSIONS = AFFECTED_CARD_VERSIONS
AFFECTED_CARD_VERSION_COUNT = len(AFFECTED_CARD_VERSIONS)
SOLE_UNLOCK_CARD_VERSION_COUNT = len(SOLE_UNLOCK_CARD_VERSIONS)


@dataclass(frozen=True, slots=True, order=True)
class StaticEvidence:
    """A local evidence locator and the narrow fact it supports."""

    source: str
    locator: str
    claim: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "locator": self.locator,
            "claim": self.claim,
        }


STATIC_EVIDENCE: tuple[StaticEvidence, ...] = (
    StaticEvidence(
        "var/coverage/plan3_blocker_priority.json",
        "family=trigger-none-field-check",
        "The current affected and sole-unlock inventory is card upgrade 0..3: four versions.",
    ),
    StaticEvidence(
        "var/master.sqlite3:produce_exam_trigger",
        TRIGGER_ID,
        "The row is None phase, Not check, PreservationUp, absent field values, and zero upper/lower search counts.",
    ),
    StaticEvidence(
        "var/master.sqlite3:card.play_effects_json",
        f"{CARD_ID}+upgrade=0..3",
        "All four card versions reference the playable-value effect through this trigger and set isOncePlayEffect=false.",
    ),
    StaticEvidence(
        "docs/android-v323-card-transaction-order-report.md",
        "UseHand -> PlayEffect -> UserCardAfterCheck -> MovePlayCard",
        "The card's own effect trigger is selected from the pre-payment snapshot and its PlayEffect command runs in ordered card-effect time.",
    ),
    StaticEvidence(
        "docs/android-v323-plan3-status-enchant-executor.md",
        "event-ordering: ExamCardPlay candidates/direct effects/ExamCardPlayAfter",
        "The direct card effect is not a status-enchant listener and is not a post-move ExamCardPlayAfter dispatch.",
    ),
    StaticEvidence(
        "docs/android-v323-plan3-static-trigger-order.md",
        "IsFieldStatusTriggerStatusEffect RVA 0x68082D4 / body 0x68089AC",
        "PreservationUp with a present value reads current stance and current stance step; absent values supply no level threshold.",
    ),
    StaticEvidence(
        "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/ExamExtensions.txt",
        "IsEffectTriggerFieldStatusValid:872-1998; IsFieldStatusTriggerStatusEffect:14735-16200",
        "The typed field-value accumulator is initialized to zero before the field helper call; the PreservationUp branch reads current status type/step, so the empty Master value vector is the zero/no-threshold form.",
    ),
    StaticEvidence(
        "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/Card/ProduceExamTrigger.txt",
        "PhaseTypeList/FieldStatusTypeList/FieldStatusValueList/FieldStatusCheckTypeList getters",
        "The phase, field, value, and Not-check pieces are separate typed native fields.",
    ),
    StaticEvidence(
        "src/gkms_tool/plan3_engine.py",
        "_trigger_field_matches: FIELD_PRESERVATION_UP then CHECK_NOT",
        "The existing typed field branch first tests current Preservation stance and then applies the Not check as logical inversion; this module repeats only that exact proven shape without changing core.",
    ),
)


@dataclass(frozen=True, slots=True, order=True)
class Plan3TriggerNoneFieldUnresolved:
    """One fail-closed reason attached to a contract or dispatch decision."""

    field: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class Plan3TriggerNoneFieldContract:
    """Immutable exact row/effect contract for the bounded family."""

    trigger: Plan3Trigger
    resolution: TriggerResolution
    source_kind: str = DISPATCH_SOURCE_CARD_DIRECT_EFFECT
    effect_id: str = EFFECT_ID
    effect_type: str = EFFECT_TYPE
    effect_count: int = EFFECT_COUNT
    effect_once: bool = EFFECT_ONCE
    affected_card_versions: tuple[AffectedCardVersion, ...] = (
        *AFFECTED_CARD_VERSIONS,
    )
    evidence: tuple[StaticEvidence, ...] = STATIC_EVIDENCE
    unresolved: tuple[Plan3TriggerNoneFieldUnresolved, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "resolution", TriggerResolution(self.resolution))
        object.__setattr__(
            self, "affected_card_versions", tuple(self.affected_card_versions)
        )
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "unresolved", tuple(self.unresolved))

    @property
    def trigger_id(self) -> str:
        return str(getattr(self.trigger, "id", "<unknown>"))

    @property
    def phase(self) -> str:
        return PHASE_NONE

    @property
    def field_check_type(self) -> str:
        return CHECK_NOT

    @property
    def field_type(self) -> str:
        return FIELD_PRESERVATION_UP

    @property
    def field_value_is_absent(self) -> bool:
        return getattr(self.trigger, "field_values", ()) == ()

    @property
    def supported(self) -> bool:
        return self.resolution is TriggerResolution.INDEPENDENT_RESOLVED and not self.unresolved

    @property
    def resolved(self) -> bool:
        return self.supported

    @property
    def repeatable(self) -> bool:
        """The target effect has no per-card-instance once gate."""

        return self.effect_once is False

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "resolution": self.resolution.value,
            "source_kind": self.source_kind,
            "dispatch_phase": DISPATCH_PHASE_CARD_PLAY_EFFECT,
            "dispatch_source": DISPATCH_SOURCE_CARD_DIRECT_EFFECT,
            "trigger": _jsonable(self.trigger),
            "field_check_type": self.field_check_type,
            "field_type": self.field_type,
            "field_value_is_absent": self.field_value_is_absent,
            "effect_id": self.effect_id,
            "effect_type": self.effect_type,
            "effect_count": self.effect_count,
            "effect_once": self.effect_once,
            "repeatable": self.repeatable,
            "affected_card_versions": [
                version.to_dict() for version in self.affected_card_versions
            ],
            "evidence": [item.to_dict() for item in self.evidence],
            "unresolved": [item.to_dict() for item in self.unresolved],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class Plan3TriggerNoneFieldDecision:
    """Pure dispatch decision; no state/effect mutation is represented."""

    contract: Plan3TriggerNoneFieldContract
    dispatch_phase: str
    dispatch_source: str
    playing_guid: str
    supported: bool
    fires: bool | None
    unresolved: tuple[Plan3TriggerNoneFieldUnresolved, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "unresolved", tuple(self.unresolved))

    @property
    def trigger_id(self) -> str:
        return self.contract.trigger_id

    @property
    def resolution(self) -> TriggerResolution:
        return (
            TriggerResolution.INDEPENDENT_RESOLVED
            if self.supported and not self.unresolved
            else TriggerResolution.UNRESOLVED
        )

    @property
    def resolved(self) -> bool:
        return self.supported and not self.unresolved

    def to_dict(self) -> dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "dispatch_phase": self.dispatch_phase,
            "dispatch_source": self.dispatch_source,
            "playing_guid": self.playing_guid,
            "supported": self.supported,
            "fires": self.fires,
            "resolution": self.resolution.value,
            "contract": self.contract.to_dict(),
            "unresolved": [item.to_dict() for item in self.unresolved],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class Plan3TriggerNoneFieldResult:
    """Decision plus identity-preserving state boundaries."""

    before: Plan3State
    after: Plan3State
    native_before: Plan3NativeState
    native_after: Plan3NativeState
    decision: Plan3TriggerNoneFieldDecision

    @property
    def fires(self) -> bool | None:
        return self.decision.fires

    @property
    def supported(self) -> bool:
        return self.decision.supported

    @property
    def state_unchanged(self) -> bool:
        return self.before is self.after

    @property
    def native_state_unchanged(self) -> bool:
        return self.native_before is self.native_after

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
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _unique_errors(
    errors: list[Plan3TriggerNoneFieldUnresolved],
) -> tuple[Plan3TriggerNoneFieldUnresolved, ...]:
    return tuple(dict.fromkeys(errors))


def _error(
    errors: list[Plan3TriggerNoneFieldUnresolved], field: str, reason: str
) -> None:
    errors.append(Plan3TriggerNoneFieldUnresolved(field, reason))


def _trigger_shape_errors(trigger: Any) -> list[Plan3TriggerNoneFieldUnresolved]:
    errors: list[Plan3TriggerNoneFieldUnresolved] = []
    if not isinstance(trigger, Plan3Trigger):
        _error(errors, "trigger", "typed-plan3-trigger-unavailable")
        return errors

    exact_values: tuple[tuple[str, Any], ...] = (
        ("id", TRIGGER_ID),
        ("phase_types", (PHASE_NONE,)),
        ("phase_values", ()),
        ("field_check_types", (CHECK_NOT,)),
        ("field_types", (FIELD_PRESERVATION_UP,)),
        ("field_values", ()),
        ("field_card_search_ids", ()),
        ("produce_card_search_id", ""),
        ("upper_search_count", 0),
        ("lower_search_count", 0),
        ("card_move_position_type", PHASE_UNKNOWN_MOVE),
        ("effect_types", ()),
        ("lesson_type", LESSON_UNKNOWN),
    )
    for field_name, expected in exact_values:
        if getattr(trigger, field_name, object()) != expected:
            _error(
                errors,
                f"trigger.{field_name}",
                f"exact-shape-mismatch:{TRIGGER_ID}",
            )
    return errors


def _source_effect(card: Any, trigger: Plan3Trigger) -> Any | None:
    try:
        effects = tuple(card.effects)
    except (AttributeError, TypeError):
        return None
    matches = tuple(
        effect
        for effect in effects
        if getattr(getattr(effect, "trigger", None), "id", None) == trigger.id
    )
    return matches[0] if len(matches) == 1 else None


def _source_card_errors(
    card: Any, trigger: Plan3Trigger
) -> list[Plan3TriggerNoneFieldUnresolved]:
    errors: list[Plan3TriggerNoneFieldUnresolved] = []
    if not isinstance(card, Plan3Card):
        _error(errors, "playing_card", "typed-plan3-card-unavailable")
        return errors
    if (card.id, card.upgrade) not in {
        (version.card_id, version.upgrade)
        for version in AFFECTED_CARD_VERSIONS
    }:
        _error(errors, "playing_card", f"card-version-outside-current-family:{TRIGGER_ID}")
    if card.plan_type != PLAN3:
        _error(errors, "playing_card.plan_type", f"plan-type-mismatch:{TRIGGER_ID}")

    effect = _source_effect(card, trigger)
    if effect is None:
        _error(errors, "playing_card.effects", f"direct-effect-source-missing:{TRIGGER_ID}")
        return errors
    if effect.id != EFFECT_ID:
        _error(errors, "effect.id", f"effect-id-mismatch:{TRIGGER_ID}")
    if effect.effect_type != EFFECT_TYPE:
        _error(errors, "effect.effect_type", f"effect-type-mismatch:{TRIGGER_ID}")
    if effect.effect_count != EFFECT_COUNT:
        _error(errors, "effect.effect_count", f"effect-count-mismatch:{TRIGGER_ID}")
    if effect.once is not EFFECT_ONCE:
        _error(errors, "effect.once", f"once-flag-mismatch:{TRIGGER_ID}")
    if getattr(effect, "status_enchant", None) is not None:
        _error(errors, "effect.status_enchant", f"status-listener-source-unproven:{TRIGGER_ID}")
    if getattr(effect, "trigger", None) != trigger:
        _error(errors, "effect.trigger", f"effect-trigger-mismatch:{TRIGGER_ID}")
    return errors


def resolve_plan3_trigger_none_field(
    trigger: Plan3Trigger,
    *,
    source_card: Plan3Card | None = None,
) -> Plan3TriggerNoneFieldContract:
    """Resolve only the exact target row and, when supplied, its card source."""

    errors = _trigger_shape_errors(trigger)
    if source_card is not None:
        errors.extend(_source_card_errors(source_card, trigger))
    unresolved = _unique_errors(errors)
    return Plan3TriggerNoneFieldContract(
        trigger=trigger,
        resolution=(
            TriggerResolution.INDEPENDENT_RESOLVED
            if not unresolved
            else TriggerResolution.UNRESOLVED
        ),
        unresolved=unresolved,
    )


def _decision(
    contract: Plan3TriggerNoneFieldContract,
    *,
    dispatch_phase: Any,
    dispatch_source: Any,
    playing_guid: Any,
    supported: bool,
    fires_value: bool | None,
    errors: list[Plan3TriggerNoneFieldUnresolved],
) -> Plan3TriggerNoneFieldDecision:
    return Plan3TriggerNoneFieldDecision(
        contract=contract,
        dispatch_phase=str(dispatch_phase),
        dispatch_source=str(dispatch_source),
        playing_guid=str(playing_guid),
        supported=supported,
        fires=fires_value,
        unresolved=_unique_errors([*contract.unresolved, *errors]),
    )


def evaluate_plan3_trigger_none_field(
    trigger_or_contract: Plan3Trigger | Plan3TriggerNoneFieldContract,
    state: Plan3State,
    native_state: Plan3NativeState,
    playing_guid: str,
    playing_card: Plan3Card,
    *,
    dispatch_phase: str,
    dispatch_source: str,
) -> Plan3TriggerNoneFieldResult:
    """Evaluate the exact field predicate at one explicit direct-effect dispatch.

    The supplied state/native objects are returned by identity.  A missing or
    inconsistent card GUID/source, an incorrect dispatch boundary, or an
    unproved trigger shape returns ``fires=None`` rather than treating it as a
    non-match.
    """

    if isinstance(trigger_or_contract, Plan3TriggerNoneFieldContract):
        contract = trigger_or_contract
    else:
        contract = resolve_plan3_trigger_none_field(
            trigger_or_contract, source_card=playing_card
        )

    errors: list[Plan3TriggerNoneFieldUnresolved] = []
    if not contract.supported:
        decision = _decision(
            contract,
            dispatch_phase=dispatch_phase,
            dispatch_source=dispatch_source,
            playing_guid=playing_guid,
            supported=False,
            fires_value=None,
            errors=errors,
        )
        return Plan3TriggerNoneFieldResult(state, state, native_state, native_state, decision)

    if dispatch_phase != DISPATCH_PHASE_CARD_PLAY_EFFECT:
        _error(errors, "dispatch_phase", f"dispatch-phase-unresolved:{TRIGGER_ID}")
    if dispatch_source != DISPATCH_SOURCE_CARD_DIRECT_EFFECT:
        _error(errors, "dispatch_source", f"dispatch-source-unresolved:{TRIGGER_ID}")
    if not isinstance(state, Plan3State):
        _error(errors, "state", "typed-plan3-state-unavailable")
    if not isinstance(native_state, Plan3NativeState):
        _error(errors, "native_state", "typed-plan3-native-state-unavailable")
    if not isinstance(playing_guid, str) or not playing_guid:
        _error(errors, "playing_guid", f"playing-guid-invalid:{TRIGGER_ID}")
    if not isinstance(playing_card, Plan3Card):
        _error(errors, "playing_card", "typed-plan3-card-unavailable")

    if not errors:
        try:
            native_state.assert_plan3_projection(state)
        except Exception:
            _error(errors, "native_state", f"plan3-zone-projection-unproven:{TRIGGER_ID}")

    native_card: Any = None
    if not errors:
        try:
            native_card = native_state.card_by_guid(playing_guid)
        except Exception:
            _error(errors, "playing_guid", f"playing-guid-not-found:{playing_guid}")
    if not errors and not any(
        getattr(card, "guid", None) == playing_guid for card in native_state.hand
    ):
        _error(errors, "playing_guid", f"playing-guid-not-in-hand:{playing_guid}")
    if not errors and playing_card is not None:
        errors.extend(_source_card_errors(playing_card, contract.trigger))
    if not errors:
        if native_card.card_id != playing_card.id:
            _error(errors, "playing_card.id", f"native-card-id-mismatch:{playing_guid}")
        elif native_card.effective_upgrade != playing_card.upgrade:
            _error(errors, "playing_card.upgrade", f"native-card-upgrade-mismatch:{playing_guid}")
        elif playing_card.ref not in state.hand:
            _error(errors, "state.hand", f"playing-card-not-in-plan3-hand:{playing_guid}")

    if errors:
        decision = _decision(
            contract,
            dispatch_phase=dispatch_phase,
            dispatch_source=dispatch_source,
            playing_guid=playing_guid,
            supported=False,
            fires_value=None,
            errors=errors,
        )
        return Plan3TriggerNoneFieldResult(state, state, native_state, native_state, decision)

    # FieldStatusValueList is empty in Master: PreservationUp means the
    # current stance type only.  CHECK_NOT then inverts that predicate.  No
    # stance-level threshold is invented from the absent value list.
    fires_value = state.stance != STANCE_PRESERVATION
    decision = _decision(
        contract,
        dispatch_phase=dispatch_phase,
        dispatch_source=dispatch_source,
        playing_guid=playing_guid,
        supported=True,
        fires_value=fires_value,
        errors=errors,
    )
    return Plan3TriggerNoneFieldResult(state, state, native_state, native_state, decision)


def fires(
    trigger_or_contract: Plan3Trigger | Plan3TriggerNoneFieldContract,
    state: Plan3State,
    native_state: Plan3NativeState,
    playing_guid: str,
    playing_card: Plan3Card,
    *,
    dispatch_phase: str,
    dispatch_source: str,
) -> bool | None:
    """Return the exact decision value, or ``None`` when typed evidence is incomplete."""

    return evaluate_plan3_trigger_none_field(
        trigger_or_contract,
        state,
        native_state,
        playing_guid,
        playing_card,
        dispatch_phase=dispatch_phase,
        dispatch_source=dispatch_source,
    ).fires


__all__ = [
    "AFFECTED_CARD_VERSION_COUNT",
    "AFFECTED_CARD_VERSIONS",
    "AffectedCardVersion",
    "CARD_ID",
    "CHECK_NOT",
    "DISPATCH_PHASE",
    "DISPATCH_PHASE_CARD_PLAY_EFFECT",
    "DISPATCH_SOURCE",
    "DISPATCH_SOURCE_CARD_DIRECT_EFFECT",
    "EFFECT_COUNT",
    "EFFECT_ID",
    "EFFECT_ONCE",
    "EFFECT_TYPE",
    "FIELD_PRESERVATION_UP",
    "PHASE_NONE",
    "PLAN3",
    "Plan3TriggerNoneFieldContract",
    "Plan3TriggerNoneFieldDecision",
    "Plan3TriggerNoneFieldResult",
    "Plan3TriggerNoneFieldUnresolved",
    "SOLE_UNLOCK_CARD_VERSION_COUNT",
    "SOLE_UNLOCK_CARD_VERSIONS",
    "SOURCE_CARD_DIRECT_EFFECT",
    "STATIC_EVIDENCE",
    "STANCE_PRESERVATION",
    "TRIGGER_ID",
    "TriggerResolution",
    "evaluate_plan3_trigger_none_field",
    "fires",
    "resolve_plan3_trigger_none_field",
]
