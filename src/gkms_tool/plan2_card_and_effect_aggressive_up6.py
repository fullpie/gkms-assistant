"""Exact standalone Plan2 card/effect gates for AggressiveUp6.

The Master row ``e_trigger-none-card_play_aggressive_up-6`` is used in two
different places.  A card ``playTriggerId`` is a play-usability gate captured
while the card is Playing and before payment/direct effects.  A
``playEffectsJson[].produceExamTriggerId`` is a direct-effect gate captured
while ``ExecuteCardCommandImpl`` is building the direct-effect sequence.

Both uses call the same native field predicate: signed current Aggressive is
read and compared inclusively with 6.  This leaf deliberately does not read
card-play counters or an Aggressive event delta.  In the direct-effect build,
all trigger slots use the sequence-build snapshot; earlier queued effects have
not executed yet and therefore cannot change a later slot's predicate input.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final, Literal, Mapping, Sequence

from .master_db import DEFAULT_DATABASE
from .native_exam_formula import INT32_MAX, INT32_MIN
from .plan2_aggressive_card_trigger import (
    AggressiveCardVersion,
    AggressiveEffectRow,
    AggressiveTriggerRow,
    CardEffectSlot,
    CHECK_NOT,
    DIRECT_DISPATCH_PHASE,
    FIELD_CARD_PLAY_AGGRESSIVE_UP,
    LESSON_UNKNOWN,
    MOVE_UNKNOWN,
    PHASE_NONE,
    PLAN2,
    PLAN_COMMON,
    resolve_aggressive_trigger,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_COVERAGE_ARTIFACT = (
    PROJECT_ROOT / "var" / "coverage" / "plan2_card_executable_coverage.json"
)

TARGET_TRIGGER_ID: Final = "e_trigger-none-card_play_aggressive_up-6"
TARGET_THRESHOLD: Final = 6
TARGET_EFFECT_ID: Final = "e_effect-exam_card_draw-0002"
TARGET_EFFECT_TYPE: Final = "ProduceExamEffectType_ExamCardDraw"
TARGET_EFFECT_SLOT_INDEX: Final = 3

TARGET_CARD_LEVEL_REFS: Final = (
    ("p_card-02-act-100_009", 0),
    ("p_card-02-sup-3_181", 0),
    ("p_card-02-sup-3_181", 1),
    ("p_card-02-sup-3_181", 2),
    ("p_card-02-sup-3_181", 3),
)
TARGET_EFFECT_LEVEL_REFS: Final = (
    ("p_card-02-sup-3_178", 0),
    ("p_card-02-sup-3_178", 1),
    ("p_card-02-sup-3_178", 2),
    ("p_card-02-sup-3_178", 3),
)
TARGET_AFFECTED_REFS: Final = TARGET_CARD_LEVEL_REFS + TARGET_EFFECT_LEVEL_REFS
SUPPORTED_PLAN_TYPES: Final = (PLAN_COMMON, PLAN2)
CARD_ORIGINS: Final = ("normal", "forced", "extra")


class AggressiveUp6CatalogError(ValueError):
    """The named Master/artifact rows are missing or outside this contract."""


class AggressiveGateKind(str, Enum):
    CARD_PLAY = "card-play-gate"
    DIRECT_EFFECT = "direct-effect-gate"


class AggressiveGateBoundary(str, Enum):
    """The two native capture points owned by this leaf."""

    CARD_PLAY_BEFORE_PAYMENT = "card-play-playing-before-payment"
    DIRECT_EFFECT_SEQUENCE_BUILD = "direct-effect-sequence-build"


@dataclass(frozen=True, slots=True)
class ExactAggressiveUp6Contract:
    """The one supported target row, with Not/search/phase shapes excluded."""

    row: AggressiveTriggerRow
    threshold: int = TARGET_THRESHOLD
    comparison: str = "signed_greater_equal"
    value_source: str = "signed current ExamStatusEffectCollection.GetAggressive(true)"

    @property
    def result_expression(self) -> str:
        return f"signed_aggressive_value >= {self.threshold}"

    def to_dict(self) -> dict[str, object]:
        return {
            "triggerId": self.row.id,
            "phase": self.row.phase_types[0],
            "threshold": self.threshold,
            "comparison": self.comparison,
            "resultExpression": self.result_expression,
            "valueSource": self.value_source,
            "checkNot": False,
        }


@dataclass(frozen=True, slots=True)
class ExactContractResolution:
    supported: bool
    contract: ExactAggressiveUp6Contract | None
    reasons: tuple[str, ...] = ()


EXACT_TARGET_TRIGGER = AggressiveTriggerRow(
    id=TARGET_TRIGGER_ID,
    phase_types=(PHASE_NONE,),
    phase_values=(),
    field_status_check_types=(),
    field_status_types=(FIELD_CARD_PLAY_AGGRESSIVE_UP,),
    field_status_values=(TARGET_THRESHOLD,),
    field_status_card_search_ids=(),
    produce_card_search_id="",
    upper_search_count=0,
    lower_search_count=0,
    card_move_position_type=MOVE_UNKNOWN,
    effect_types=(),
    lesson_type=LESSON_UNKNOWN,
)


def _strict_int(value: object, label: str, *, signed: bool = True) -> int:
    if type(value) is not int:
        raise AggressiveUp6CatalogError(f"{label} must be a plain integer")
    if signed and not INT32_MIN <= value <= INT32_MAX:
        raise AggressiveUp6CatalogError(f"{label} is outside signed Int32")
    if not signed and value < 0:
        raise AggressiveUp6CatalogError(f"{label} must be non-negative")
    return value


def _json_object(value: object, label: str) -> dict[str, object]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise AggressiveUp6CatalogError(f"{label}: invalid JSON object") from error
    if not isinstance(parsed, dict):
        raise AggressiveUp6CatalogError(f"{label}: expected JSON object")
    return parsed


def _json_array(value: object, label: str) -> list[object]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise AggressiveUp6CatalogError(f"{label}: invalid JSON array") from error
    if not isinstance(parsed, list):
        raise AggressiveUp6CatalogError(f"{label}: expected JSON array")
    return parsed


def _string_array(value: object, label: str) -> tuple[str, ...]:
    parsed = _json_array(value, label)
    if any(type(item) is not str for item in parsed):
        raise AggressiveUp6CatalogError(f"{label}: expected string entries")
    return tuple(parsed)  # type: ignore[arg-type]


def _int_array(value: object, label: str) -> tuple[int, ...]:
    parsed = _json_array(value, label)
    if any(type(item) is not int for item in parsed):
        raise AggressiveUp6CatalogError(f"{label}: expected integer entries")
    return tuple(parsed)  # type: ignore[arg-type]


def _strict_raw_contract(raw: Mapping[str, object], expected: Mapping[str, object], label: str) -> None:
    for key, wanted in expected.items():
        if key not in raw:
            raise AggressiveUp6CatalogError(f"{label}: missing raw field {key}")
        actual = raw[key]
        if type(actual) is not type(wanted) or actual != wanted:
            raise AggressiveUp6CatalogError(
                f"{label}: raw field {key} is {actual!r}, expected {wanted!r}"
            )


def _trigger_from_db_row(row: sqlite3.Row) -> AggressiveTriggerRow:
    trigger_id = str(row["id"])
    typed = AggressiveTriggerRow(
        id=trigger_id,
        phase_types=_string_array(row["phase_types_json"], f"{trigger_id}.phaseTypes"),
        phase_values=_int_array(row["phase_values_json"], f"{trigger_id}.phaseValues"),
        field_status_check_types=_string_array(
            row["field_status_check_types_json"],
            f"{trigger_id}.fieldStatusCheckTypes",
        ),
        field_status_types=_string_array(
            row["field_status_types_json"], f"{trigger_id}.fieldStatusTypes"
        ),
        field_status_values=_int_array(
            row["field_status_values_json"], f"{trigger_id}.fieldStatusValues"
        ),
        field_status_card_search_ids=_string_array(
            row["field_status_produce_card_search_ids_json"],
            f"{trigger_id}.fieldStatusProduceCardSearchIds",
        ),
        produce_card_search_id=str(row["produce_card_search_id"]),
        upper_search_count=_strict_int(
            row["upper_search_count"], f"{trigger_id}.upperSearchCount", signed=False
        ),
        lower_search_count=_strict_int(
            row["lower_search_count"], f"{trigger_id}.lowerSearchCount", signed=False
        ),
        card_move_position_type=str(row["card_move_position_type"]),
        effect_types=_string_array(row["effect_types_json"], f"{trigger_id}.effectTypes"),
        lesson_type=str(row["lesson_type"]),
    )
    raw = _json_object(row["raw_json"], trigger_id)
    _strict_raw_contract(
        raw,
        {
            "id": TARGET_TRIGGER_ID,
            "phaseTypes": [PHASE_NONE],
            "phaseValues": [],
            "fieldStatusCheckTypes": [],
            "fieldStatusTypes": [FIELD_CARD_PLAY_AGGRESSIVE_UP],
            "fieldStatusValues": [TARGET_THRESHOLD],
            "fieldStatusProduceCardSearchIds": [],
            "produceCardSearchId": "",
            "upperSearchCount": 0,
            "lowerSearchCount": 0,
            "cardMovePositionType": MOVE_UNKNOWN,
            "effectTypes": [],
            "lessonType": LESSON_UNKNOWN,
        },
        trigger_id,
    )
    return typed


def resolve_exact_aggressive_up6_trigger(
    row: AggressiveTriggerRow,
) -> ExactContractResolution:
    """Resolve only the exact target; every unsupported shape fails closed."""

    if not isinstance(row, AggressiveTriggerRow):
        return ExactContractResolution(False, None, ("trigger-row-type-is-unsupported",))

    # Reuse the family resolver for its typed shape diagnostics, then apply
    # this leaf's stricter target-only rules.  The family Not rows are useful
    # elsewhere but are deliberately not executable here.
    generic = resolve_aggressive_trigger(row)
    reasons: list[str] = []
    if not generic.supported:
        reasons.extend(generic.reasons)
    if row.id != TARGET_TRIGGER_ID:
        reasons.append("trigger-id-is-not-exact-target")
    if row.phase_types != (PHASE_NONE,):
        reasons.append("trigger-phase-is-not-none")
    if row.phase_values:
        reasons.append("trigger-phase-values-must-be-empty")
    if row.field_status_check_types:
        if CHECK_NOT in row.field_status_check_types:
            reasons.append("trigger-not-check-unsupported")
        else:
            reasons.append("trigger-field-check-shape-unsupported")
    if row.field_status_types != (FIELD_CARD_PLAY_AGGRESSIVE_UP,):
        reasons.append("trigger-field-is-not-card-play-aggressive-up")
    if row.field_status_values != (TARGET_THRESHOLD,):
        reasons.append("trigger-value-shape-is-not-exact-six")
    if row.field_status_card_search_ids:
        reasons.append("trigger-field-search-is-unsupported")
    if row.produce_card_search_id:
        reasons.append("trigger-card-search-is-unsupported")
    if row.upper_search_count != 0 or row.lower_search_count != 0:
        reasons.append("trigger-search-count-shape-is-unsupported")
    if row.card_move_position_type != MOVE_UNKNOWN:
        reasons.append("trigger-card-move-shape-is-unsupported")
    if row.effect_types:
        reasons.append("trigger-effect-type-shape-is-unsupported")
    if row.lesson_type != LESSON_UNKNOWN:
        reasons.append("trigger-lesson-shape-is-unsupported")
    unique = tuple(dict.fromkeys(reasons))
    if unique:
        return ExactContractResolution(False, None, unique)
    return ExactContractResolution(True, ExactAggressiveUp6Contract(row), ())


@dataclass(frozen=True, slots=True)
class AggressiveGateInput:
    """Read-only native inputs for either exact gate.

    ``global_card_play_count``, ``turn_card_play_count``, and
    ``observed_aggressive_delta`` are retained only to make their inertness
    explicit.  ``card_origin`` and ``is_use_playable_count`` share the same
    predicate for normal, forced, and extra plays.
    """

    aggressive_status_value: int
    phase: str = PHASE_NONE
    dispatch_phase: str = DIRECT_DISPATCH_PHASE
    boundary: AggressiveGateBoundary = AggressiveGateBoundary.CARD_PLAY_BEFORE_PAYMENT
    card_origin: Literal["normal", "forced", "extra"] = "normal"
    is_use_playable_count: bool = True
    global_card_play_count: int = 0
    turn_card_play_count: int = 0
    observed_aggressive_delta: int | None = None

    def __post_init__(self) -> None:
        _strict_int(self.aggressive_status_value, "aggressive_status_value")
        for name in ("global_card_play_count", "turn_card_play_count"):
            _strict_int(getattr(self, name), name, signed=False)
        if self.observed_aggressive_delta is not None:
            _strict_int(self.observed_aggressive_delta, "observed_aggressive_delta")
        if type(self.is_use_playable_count) is not bool:
            raise TypeError("is_use_playable_count must be bool")
        if type(self.phase) is not str or not self.phase:
            raise TypeError("phase must be a non-empty string")
        if type(self.dispatch_phase) is not str or not self.dispatch_phase:
            raise TypeError("dispatch_phase must be a non-empty string")
        try:
            object.__setattr__(self, "boundary", AggressiveGateBoundary(self.boundary))
        except (TypeError, ValueError) as error:
            raise TypeError("boundary must be an AggressiveGateBoundary") from error
        if self.card_origin not in CARD_ORIGINS:
            raise ValueError("card_origin must be normal, forced, or extra")


@dataclass(frozen=True, slots=True)
class AggressiveGateEvaluation:
    trigger_id: str
    gate_kind: AggressiveGateKind
    supported: bool
    fires: bool | None
    aggressive_status_value: int | None
    threshold: int | None
    comparison: str | None
    check_not: bool | None
    phase: str
    dispatch_phase: str
    boundary: AggressiveGateBoundary
    card_origin: str
    reasons: tuple[str, ...] = ()

    @property
    def fail_closed(self) -> bool:
        return not self.supported or self.fires is None

    @property
    def value_source(self) -> str:
        return "signed current Aggressive status value"

    def to_dict(self) -> dict[str, object]:
        return {
            "triggerId": self.trigger_id,
            "gateKind": self.gate_kind.value,
            "supported": self.supported,
            "fires": self.fires,
            "aggressiveStatusValue": self.aggressive_status_value,
            "threshold": self.threshold,
            "comparison": self.comparison,
            "checkNot": self.check_not,
            "phase": self.phase,
            "dispatchPhase": self.dispatch_phase,
            "boundary": self.boundary.value,
            "cardOrigin": self.card_origin,
            "valueSource": self.value_source,
            "reasons": list(self.reasons),
        }


def _trigger_id(source: object) -> str:
    return source.id if isinstance(source, AggressiveTriggerRow) else "<unknown-trigger>"


def _unsupported_gate(
    trigger: object,
    kind: AggressiveGateKind,
    inputs: AggressiveGateInput,
    reasons: Sequence[str],
    *,
    threshold: int | None = None,
    comparison: str | None = None,
    check_not: bool | None = None,
) -> AggressiveGateEvaluation:
    return AggressiveGateEvaluation(
        trigger_id=_trigger_id(trigger),
        gate_kind=kind,
        supported=False,
        fires=None,
        aggressive_status_value=None,
        threshold=threshold,
        comparison=comparison,
        check_not=check_not,
        phase=inputs.phase,
        dispatch_phase=inputs.dispatch_phase,
        boundary=inputs.boundary,
        card_origin=inputs.card_origin,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def _evaluate_gate(
    kind: AggressiveGateKind,
    inputs: AggressiveGateInput,
    trigger: AggressiveTriggerRow,
) -> AggressiveGateEvaluation:
    resolution = resolve_exact_aggressive_up6_trigger(trigger)
    if not resolution.supported:
        return _unsupported_gate(trigger, kind, inputs, resolution.reasons)
    contract = resolution.contract
    assert contract is not None
    expected_boundary = (
        AggressiveGateBoundary.CARD_PLAY_BEFORE_PAYMENT
        if kind is AggressiveGateKind.CARD_PLAY
        else AggressiveGateBoundary.DIRECT_EFFECT_SEQUENCE_BUILD
    )
    reasons: list[str] = []
    if inputs.phase != PHASE_NONE:
        reasons.append("runtime-phase-is-not-none")
    if inputs.dispatch_phase != DIRECT_DISPATCH_PHASE:
        reasons.append("dispatch-phase-is-not-play-effect")
    if inputs.boundary is not expected_boundary:
        reasons.append(
            "runtime-boundary-is-not-"
            + expected_boundary.value
        )
    if reasons:
        return _unsupported_gate(
            trigger,
            kind,
            inputs,
            reasons,
            threshold=contract.threshold,
            comparison=contract.comparison,
            check_not=False,
        )
    return AggressiveGateEvaluation(
        trigger_id=trigger.id,
        gate_kind=kind,
        supported=True,
        fires=inputs.aggressive_status_value >= contract.threshold,
        aggressive_status_value=inputs.aggressive_status_value,
        threshold=contract.threshold,
        comparison=contract.comparison,
        check_not=False,
        phase=inputs.phase,
        dispatch_phase=inputs.dispatch_phase,
        boundary=inputs.boundary,
        card_origin=inputs.card_origin,
    )


def evaluate_card_play_aggressive_up6_trigger(
    inputs: AggressiveGateInput,
    trigger: AggressiveTriggerRow = EXACT_TARGET_TRIGGER,
) -> AggressiveGateEvaluation:
    """Evaluate the card-level play-usability gate before payment/effects."""

    if not isinstance(inputs, AggressiveGateInput):
        raise TypeError("inputs must be AggressiveGateInput")
    return _evaluate_gate(AggressiveGateKind.CARD_PLAY, inputs, trigger)


def evaluate_direct_effect_aggressive_up6_trigger(
    inputs: AggressiveGateInput,
    trigger: AggressiveTriggerRow = EXACT_TARGET_TRIGGER,
) -> AggressiveGateEvaluation:
    """Evaluate a direct-effect trigger at the sequence-build boundary."""

    if not isinstance(inputs, AggressiveGateInput):
        raise TypeError("inputs must be AggressiveGateInput")
    return _evaluate_gate(AggressiveGateKind.DIRECT_EFFECT, inputs, trigger)


# Short names make the two boundaries easy to use without hiding their exact
# semantics.  The longer names remain the canonical public API.
evaluate_card_play_gate = evaluate_card_play_aggressive_up6_trigger
evaluate_direct_effect_gate = evaluate_direct_effect_aggressive_up6_trigger
evaluate_card_trigger = evaluate_card_play_aggressive_up6_trigger
evaluate_effect_trigger = evaluate_direct_effect_aggressive_up6_trigger


@dataclass(frozen=True, slots=True)
class DirectEffectSequenceEvaluation:
    card: AggressiveCardVersion
    snapshot_aggressive_status_value: int
    queue_effect_ids: tuple[str, ...]
    target_slot_indexes: tuple[int, ...]
    evaluations: tuple[AggressiveGateEvaluation, ...]

    @property
    def supported(self) -> bool:
        return bool(self.evaluations) and all(item.supported for item in self.evaluations)

    @property
    def fires(self) -> bool | None:
        if not self.evaluations or not self.supported:
            return None
        return all(item.fires is True for item in self.evaluations)

    @property
    def snapshot_rule(self) -> str:
        return (
            "one current Aggressive read during direct-effect sequence build; "
            "earlier direct effects execute later and do not alter this snapshot"
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "cardId": self.card.card_id,
            "upgrade": self.card.upgrade_count,
            "snapshotAggressiveStatusValue": self.snapshot_aggressive_status_value,
            "queueEffectIds": list(self.queue_effect_ids),
            "targetSlotIndexes": list(self.target_slot_indexes),
            "evaluations": [item.to_dict() for item in self.evaluations],
            "fires": self.fires,
            "snapshotRule": self.snapshot_rule,
        }


def simulate_direct_effect_sequence(
    card: AggressiveCardVersion,
    inputs: AggressiveGateInput,
    trigger: AggressiveTriggerRow = EXACT_TARGET_TRIGGER,
) -> DirectEffectSequenceEvaluation:
    """Probe target slots in Master order using one direct-sequence snapshot."""

    if not isinstance(card, AggressiveCardVersion):
        raise TypeError("card must be AggressiveCardVersion")
    if not isinstance(inputs, AggressiveGateInput):
        raise TypeError("inputs must be AggressiveGateInput")
    target_slots = tuple(
        slot for slot in card.effect_slots if slot.trigger_id == TARGET_TRIGGER_ID
    )
    evaluations = tuple(
        evaluate_direct_effect_aggressive_up6_trigger(inputs, trigger)
        for _slot in target_slots
    )
    return DirectEffectSequenceEvaluation(
        card=card,
        snapshot_aggressive_status_value=inputs.aggressive_status_value,
        queue_effect_ids=tuple(slot.effect_id for slot in card.effect_slots),
        target_slot_indexes=tuple(slot.slot_index for slot in target_slots),
        evaluations=evaluations,
    )


@dataclass(frozen=True, slots=True)
class CoverageReadback:
    source: str
    card_trigger_classification: str
    card_trigger_instances: int
    effect_trigger_classification: str
    effect_trigger_instances: int
    card_gap_refs: tuple[tuple[str, int], ...]
    effect_gap_refs: tuple[tuple[str, int], ...]
    next_gap_card_direct: int
    next_gap_effect_direct: int

    @property
    def affected_count(self) -> int:
        return len(set(self.card_gap_refs) | set(self.effect_gap_refs))

    @property
    def direct_count(self) -> int:
        return self.affected_count

    @property
    def co_blocked_count(self) -> int:
        return len(set(self.card_gap_refs) & set(self.effect_gap_refs))

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "slotInventory": {
                "cardTrigger": {
                    "classification": self.card_trigger_classification,
                    "instances": self.card_trigger_instances,
                },
                "effectTrigger": {
                    "classification": self.effect_trigger_classification,
                    "instances": self.effect_trigger_instances,
                },
            },
            "cardGapRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.card_gap_refs
            ],
            "effectGapRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.effect_gap_refs
            ],
            "affected": self.affected_count,
            "direct": self.direct_count,
            "coBlocked": self.co_blocked_count,
            "nextGapDirect": {
                "cardTrigger": self.next_gap_card_direct,
                "effectTrigger": self.next_gap_effect_direct,
            },
        }


def _gap_ref(raw: object, label: str) -> tuple[str, str] | None:
    if not isinstance(raw, str):
        raise AggressiveUp6CatalogError(f"{label}: gap must be string")
    prefix_card = "C:card-trigger:"
    prefix_effect = "C:effect-trigger:"
    if raw.startswith(prefix_card + TARGET_TRIGGER_ID):
        return "card", raw
    if raw.startswith(prefix_effect + TARGET_TRIGGER_ID):
        return "effect", raw
    return None


def load_coverage_readback(
    artifact: Path | str = DEFAULT_COVERAGE_ARTIFACT,
) -> CoverageReadback:
    """Read only the named target rows/gaps from the central artifact."""

    path = Path(artifact)
    if not path.is_file():
        raise AggressiveUp6CatalogError(f"coverage artifact not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AggressiveUp6CatalogError(f"cannot read coverage artifact: {path}") from error
    if not isinstance(data, dict):
        raise AggressiveUp6CatalogError("coverage artifact must be a JSON object")

    inventory = data.get("slot_inventory")
    if not isinstance(inventory, list):
        raise AggressiveUp6CatalogError("coverage slot_inventory is missing")
    card_inventory = [
        row
        for row in inventory
        if isinstance(row, dict)
        and row.get("kind") == "card-trigger"
        and row.get("id") == TARGET_TRIGGER_ID
    ]
    effect_inventory = [
        row
        for row in inventory
        if isinstance(row, dict)
        and row.get("kind") == "effect-trigger"
        and row.get("id") == TARGET_TRIGGER_ID
    ]
    if len(card_inventory) != 1 or len(effect_inventory) != 1:
        raise AggressiveUp6CatalogError("target slot_inventory rows are not unique")
    card_row, effect_row = card_inventory[0], effect_inventory[0]
    if card_row.get("instances") != len(TARGET_CARD_LEVEL_REFS):
        raise AggressiveUp6CatalogError("card-trigger artifact instance count changed")
    if effect_row.get("instances") != len(TARGET_EFFECT_LEVEL_REFS):
        raise AggressiveUp6CatalogError("effect-trigger artifact instance count changed")

    card_classification = card_row.get("classification")
    effect_classification = effect_row.get("classification")
    if card_classification == "B" and effect_classification == "B":
        overlay = data.get("plan2_card_and_effect_aggressive_up6_executable_overlay")
        if not isinstance(overlay, dict):
            raise AggressiveUp6CatalogError("integrated executable overlay is missing")

        def overlay_refs(key: str) -> tuple[tuple[str, int], ...]:
            raw_refs = overlay.get(key)
            if not isinstance(raw_refs, list):
                raise AggressiveUp6CatalogError(f"integrated {key} is missing")
            refs: list[tuple[str, int]] = []
            for index, raw_ref in enumerate(raw_refs):
                if not isinstance(raw_ref, dict):
                    raise AggressiveUp6CatalogError(
                        f"integrated {key}[{index}] is not an object"
                    )
                card_id = raw_ref.get("card_id")
                upgrade = raw_ref.get("upgrade")
                if type(card_id) is not str or type(upgrade) is not int:
                    raise AggressiveUp6CatalogError(
                        f"integrated {key}[{index}] is malformed"
                    )
                refs.append((card_id, upgrade))
            return tuple(refs)

        card_refs = overlay_refs("card_level_version_refs")
        effect_refs = overlay_refs("effect_level_version_refs")
        if card_refs != TARGET_CARD_LEVEL_REFS:
            raise AggressiveUp6CatalogError("integrated card-trigger refs changed")
        if effect_refs != TARGET_EFFECT_LEVEL_REFS:
            raise AggressiveUp6CatalogError("integrated effect-trigger refs changed")
        if overlay.get("affected_versions") != len(TARGET_AFFECTED_REFS):
            raise AggressiveUp6CatalogError("integrated affected count changed")
        if overlay.get("direct_versions") != len(TARGET_AFFECTED_REFS):
            raise AggressiveUp6CatalogError("integrated direct count changed")
        if overlay.get("co_blocked_versions") != 0:
            raise AggressiveUp6CatalogError("integrated co-blocked count changed")
        return CoverageReadback(
            source=str(path),
            card_trigger_classification="B",
            card_trigger_instances=int(card_row["instances"]),
            effect_trigger_classification="B",
            effect_trigger_instances=int(effect_row["instances"]),
            card_gap_refs=card_refs,
            effect_gap_refs=effect_refs,
            next_gap_card_direct=0,
            next_gap_effect_direct=0,
        )
    if card_classification != "C" or effect_classification != "C":
        raise AggressiveUp6CatalogError(
            "target coverage rows are neither paired C gaps nor paired B executors"
        )

    artifact_refs: dict[str, list[tuple[str, int]]] = {"card": [], "effect": []}
    cards = data.get("cards")
    if not isinstance(cards, list):
        raise AggressiveUp6CatalogError("coverage cards section is missing")
    for index, row in enumerate(cards):
        if not isinstance(row, dict):
            continue
        card_id = row.get("card_id")
        upgrade = row.get("upgrade")
        if type(card_id) is not str or type(upgrade) is not int:
            continue
        gaps = row.get("gaps", [])
        if not isinstance(gaps, list):
            raise AggressiveUp6CatalogError(f"coverage cards[{index}].gaps is not a list")
        for gap_index, gap in enumerate(gaps):
            parsed = _gap_ref(gap, f"coverage cards[{index}].gaps[{gap_index}]")
            if parsed is not None:
                artifact_refs[parsed[0]].append((card_id, upgrade))
    card_refs = tuple(dict.fromkeys(artifact_refs["card"]))
    effect_refs = tuple(dict.fromkeys(artifact_refs["effect"]))
    if card_refs != TARGET_CARD_LEVEL_REFS:
        raise AggressiveUp6CatalogError("coverage card-trigger refs changed")
    if effect_refs != TARGET_EFFECT_LEVEL_REFS:
        raise AggressiveUp6CatalogError("coverage effect-trigger refs changed")

    next_gaps = data.get("next_gaps")
    if not isinstance(next_gaps, list):
        raise AggressiveUp6CatalogError("coverage next_gaps section is missing")
    next_by_gap = {
        row.get("gap"): row
        for row in next_gaps
        if isinstance(row, dict) and isinstance(row.get("gap"), str)
    }
    card_next = next_by_gap.get("C:card-trigger:" + TARGET_TRIGGER_ID)
    effect_next = next_by_gap.get("C:effect-trigger:" + TARGET_TRIGGER_ID)
    if not isinstance(card_next, dict) or not isinstance(effect_next, dict):
        raise AggressiveUp6CatalogError("target next_gaps rows are missing")
    for label, row, expected in (
        ("card", card_next, len(TARGET_CARD_LEVEL_REFS)),
        ("effect", effect_next, len(TARGET_EFFECT_LEVEL_REFS)),
    ):
        if row.get("directly_unlocked_if_fixed_alone") != expected:
            raise AggressiveUp6CatalogError(f"{label} next_gaps direct count changed")
        if row.get("affected_card_versions") != expected:
            raise AggressiveUp6CatalogError(f"{label} next_gaps affected count changed")
    return CoverageReadback(
        source=str(path),
        card_trigger_classification="C",
        card_trigger_instances=int(card_row["instances"]),
        effect_trigger_classification="C",
        effect_trigger_instances=int(effect_row["instances"]),
        card_gap_refs=card_refs,
        effect_gap_refs=effect_refs,
        next_gap_card_direct=int(card_next["directly_unlocked_if_fixed_alone"]),
        next_gap_effect_direct=int(effect_next["directly_unlocked_if_fixed_alone"]),
    )


def _effect_from_db_row(row: sqlite3.Row) -> AggressiveEffectRow:
    effect_id = str(row["id"])
    raw = _json_object(row["raw_json"], effect_id)
    groups = raw.get("effectGroupIds", [])
    if type(groups) is not list or any(type(item) is not str or not item for item in groups):
        raise AggressiveUp6CatalogError(f"{effect_id}: invalid effectGroupIds")
    return AggressiveEffectRow(
        id=effect_id,
        effect_type=str(row["effect_type"]),
        value1=_strict_int(row["value1"], f"{effect_id}.value1"),
        value2=_strict_int(row["value2"], f"{effect_id}.value2"),
        effect_count=_strict_int(row["effect_count"], f"{effect_id}.effectCount"),
        effect_turn=_strict_int(row["effect_turn"], f"{effect_id}.effectTurn"),
        status_enchant_id=str(row["status_enchant_id"]),
        chain_effect_id=str(row["chain_effect_id"]),
        effect_group_ids=tuple(groups),  # type: ignore[arg-type]
    )


def _card_effects(
    raw_json: object,
    card_id: str,
    upgrade: int,
) -> tuple[dict[str, object], ...]:
    raw_effects = _json_array(raw_json, f"{card_id}#{upgrade}.playEffects")
    result: list[dict[str, object]] = []
    for index, raw in enumerate(raw_effects):
        if not isinstance(raw, dict):
            raise AggressiveUp6CatalogError(f"{card_id}#{upgrade}: slot {index} is not object")
        for key in ("produceExamTriggerId", "produceExamEffectId", "hideIcon", "isOncePlayEffect"):
            if key not in raw:
                raise AggressiveUp6CatalogError(f"{card_id}#{upgrade}: slot {index} missing {key}")
        if type(raw["produceExamTriggerId"]) is not str:
            raise AggressiveUp6CatalogError(f"{card_id}#{upgrade}: trigger id is not string")
        if type(raw["produceExamEffectId"]) is not str:
            raise AggressiveUp6CatalogError(f"{card_id}#{upgrade}: effect id is not string")
        if type(raw["hideIcon"]) is not bool or type(raw["isOncePlayEffect"]) is not bool:
            raise AggressiveUp6CatalogError(f"{card_id}#{upgrade}: slot flags are not bool")
        result.append(raw)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class Plan2CardAndEffectAggressiveUp6Catalog:
    """The exact nine-version union of the two target usage positions."""

    database: str
    trigger: AggressiveTriggerRow
    card_level_versions: tuple[AggressiveCardVersion, ...]
    effect_level_versions: tuple[AggressiveCardVersion, ...]
    affected_versions: tuple[AggressiveCardVersion, ...]
    direct_versions: tuple[AggressiveCardVersion, ...]
    co_blocked_versions: tuple[AggressiveCardVersion, ...]
    effect_rows: tuple[AggressiveEffectRow, ...]
    coverage: CoverageReadback

    @property
    def affected_count(self) -> int:
        return len(self.affected_versions)

    @property
    def direct_count(self) -> int:
        return len(self.direct_versions)

    @property
    def co_blocked_count(self) -> int:
        return len(self.co_blocked_versions)

    @property
    def target_effect(self) -> AggressiveEffectRow:
        for row in self.effect_rows:
            if row.id == TARGET_EFFECT_ID:
                return row
        raise KeyError(TARGET_EFFECT_ID)

    def version_refs(self, kind: AggressiveGateKind) -> tuple[tuple[str, int], ...]:
        versions = (
            self.card_level_versions
            if kind is AggressiveGateKind.CARD_PLAY
            else self.effect_level_versions
        )
        return tuple((card.card_id, card.upgrade_count) for card in versions)

    def summary(self) -> dict[str, object]:
        return {
            "triggerId": TARGET_TRIGGER_ID,
            "cardLevelPlayGateVersions": len(self.card_level_versions),
            "effectLevelDirectEffectGateVersions": len(self.effect_level_versions),
            "affected": self.affected_count,
            "direct": self.direct_count,
            "coBlocked": self.co_blocked_count,
            "cardLevelRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.version_refs(AggressiveGateKind.CARD_PLAY)
            ],
            "effectLevelRefs": [
                {"cardId": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.version_refs(AggressiveGateKind.DIRECT_EFFECT)
            ],
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "database": self.database,
            "trigger": self.trigger.to_dict(),
            "summary": self.summary(),
            "cardLevelVersions": [card.to_dict() for card in self.card_level_versions],
            "effectLevelVersions": [card.to_dict() for card in self.effect_level_versions],
            "effectRows": [row.to_dict() for row in self.effect_rows],
            "coverage": self.coverage.to_dict(),
        }


def load_plan2_card_and_effect_aggressive_up6_catalog(
    database: Path | str = DEFAULT_DATABASE,
    coverage_artifact: Path | str = DEFAULT_COVERAGE_ARTIFACT,
) -> Plan2CardAndEffectAggressiveUp6Catalog:
    """Read named target rows from Master and the current coverage artifact."""

    coverage = load_coverage_readback(coverage_artifact)
    path = Path(database)
    if not path.is_file():
        raise AggressiveUp6CatalogError(f"Master database not found: {path}")
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        raise AggressiveUp6CatalogError(f"cannot open Master read-only: {path}") from error
    connection.row_factory = sqlite3.Row
    with closing(connection):
        trigger_row = connection.execute(
            "SELECT * FROM produce_exam_trigger WHERE id = ?",
            (TARGET_TRIGGER_ID,),
        ).fetchone()
        if trigger_row is None:
            raise AggressiveUp6CatalogError(f"missing target trigger: {TARGET_TRIGGER_ID}")
        trigger = _trigger_from_db_row(trigger_row)
        resolution = resolve_exact_aggressive_up6_trigger(trigger)
        if not resolution.supported:
            raise AggressiveUp6CatalogError(
                f"target trigger is outside exact shape: {resolution.reasons}"
            )

        card_rows = connection.execute(
            """
            SELECT id, upgrade_count, plan_type, category,
                   play_trigger_id, play_effects_json
              FROM card
             WHERE play_trigger_id = ? OR instr(play_effects_json, ?) > 0
             ORDER BY id, upgrade_count
            """,
            (TARGET_TRIGGER_ID, TARGET_TRIGGER_ID),
        ).fetchall()
        if not card_rows:
            raise AggressiveUp6CatalogError("target trigger has no card references")

        parsed_cards: list[tuple[sqlite3.Row, tuple[dict[str, object], ...]]] = []
        effect_ids: set[str] = set()
        for row in card_rows:
            card_id = str(row["id"])
            upgrade = _strict_int(row["upgrade_count"], f"{card_id}.upgradeCount", signed=False)
            if type(row["plan_type"]) is not str or row["plan_type"] not in SUPPORTED_PLAN_TYPES:
                raise AggressiveUp6CatalogError(f"{card_id}#{upgrade}: unsupported plan type")
            effects = _card_effects(row["play_effects_json"], card_id, upgrade)
            parsed_cards.append((row, effects))
            effect_ids.update(
                str(raw["produceExamEffectId"])
                for raw in effects
                if raw["produceExamEffectId"]
            )

        placeholders = ",".join("?" for _ in sorted(effect_ids))
        effect_rows_raw = connection.execute(
            f"SELECT * FROM effect WHERE id IN ({placeholders})",
            tuple(sorted(effect_ids)),
        ).fetchall()
        effects_by_id = {
            str(row["id"]): _effect_from_db_row(row) for row in effect_rows_raw
        }
        missing_effects = sorted(effect_ids - set(effects_by_id))
        if missing_effects:
            raise AggressiveUp6CatalogError(f"missing target card effects: {missing_effects}")
        target_effect = effects_by_id.get(TARGET_EFFECT_ID)
        if target_effect is None:
            raise AggressiveUp6CatalogError(f"missing target effect: {TARGET_EFFECT_ID}")
        if (
            target_effect.effect_type != TARGET_EFFECT_TYPE
            or target_effect.value1 != 2
            or target_effect.value2 != 0
            or target_effect.effect_count != 0
            or target_effect.effect_turn != 0
            or target_effect.status_enchant_id != ""
            or target_effect.chain_effect_id != ""
        ):
            raise AggressiveUp6CatalogError("target direct effect scalar shape changed")
        effect_groups = {
            effect_id: row.effect_group_ids for effect_id, row in effects_by_id.items()
        }
        cards: list[AggressiveCardVersion] = []
        for row, effects in parsed_cards:
            card_id = str(row["id"])
            upgrade = int(row["upgrade_count"])
            slots = tuple(
                CardEffectSlot(
                    card_id=card_id,
                    upgrade_count=upgrade,
                    slot_index=index,
                    effect_id=str(raw["produceExamEffectId"]),
                    trigger_id=str(raw["produceExamTriggerId"]),
                    is_once_play_effect=bool(raw["isOncePlayEffect"]),
                    effect_group_ids=effect_groups[str(raw["produceExamEffectId"])],
                )
                for index, raw in enumerate(effects)
            )
            cards.append(
                AggressiveCardVersion(
                    card_id=card_id,
                    upgrade_count=upgrade,
                    plan_type=str(row["plan_type"]),
                    category=str(row["category"]),
                    play_trigger_id=str(row["play_trigger_id"]),
                    effect_slots=slots,
                )
            )

    card_level = tuple(card for card in cards if card.play_trigger_id == TARGET_TRIGGER_ID)
    effect_level = tuple(
        card
        for card in cards
        if any(slot.trigger_id == TARGET_TRIGGER_ID for slot in card.effect_slots)
    )
    actual_card_refs = tuple((card.card_id, card.upgrade_count) for card in card_level)
    actual_effect_refs = tuple((card.card_id, card.upgrade_count) for card in effect_level)
    if actual_card_refs != TARGET_CARD_LEVEL_REFS:
        raise AggressiveUp6CatalogError(
            f"card-level target refs changed: {actual_card_refs!r}"
        )
    if actual_effect_refs != TARGET_EFFECT_LEVEL_REFS:
        raise AggressiveUp6CatalogError(
            f"effect-level target refs changed: {actual_effect_refs!r}"
        )
    overlap = set(actual_card_refs) & set(actual_effect_refs)
    if overlap:
        raise AggressiveUp6CatalogError(f"target refs are co-blocked/overlapping: {sorted(overlap)}")
    for card in effect_level:
        matching = tuple(
            slot for slot in card.effect_slots if slot.trigger_id == TARGET_TRIGGER_ID
        )
        if len(matching) != 1:
            raise AggressiveUp6CatalogError(
                f"{card.card_id}#{card.upgrade_count}: target effect slot count changed"
            )
        slot = matching[0]
        if (
            slot.slot_index != TARGET_EFFECT_SLOT_INDEX
            or slot.effect_id != TARGET_EFFECT_ID
            or slot.is_once_play_effect
        ):
            raise AggressiveUp6CatalogError(
                f"{card.card_id}#{card.upgrade_count}: target effect slot shape changed"
            )
    if tuple((card.card_id, card.upgrade_count) for card in cards) != (
        TARGET_CARD_LEVEL_REFS[:1]
        + TARGET_EFFECT_LEVEL_REFS
        + TARGET_CARD_LEVEL_REFS[1:]
    ):
        # The query is ordered by id, not semantic position.  Keep the
        # catalog deterministic without requiring a specific SQL collation.
        cards = sorted(
            cards,
            key=lambda card: (
                0 if card.play_trigger_id == TARGET_TRIGGER_ID else 1,
                card.card_id,
                card.upgrade_count,
            ),
        )
    by_ref = {(card.card_id, card.upgrade_count): card for card in cards}
    ordered_refs = TARGET_CARD_LEVEL_REFS + TARGET_EFFECT_LEVEL_REFS
    affected = tuple(by_ref[ref] for ref in ordered_refs)
    card_level = tuple(by_ref[ref] for ref in TARGET_CARD_LEVEL_REFS)
    effect_level = tuple(by_ref[ref] for ref in TARGET_EFFECT_LEVEL_REFS)
    direct = affected
    effects = tuple(effects_by_id[key] for key in sorted(effects_by_id))
    return Plan2CardAndEffectAggressiveUp6Catalog(
        database=str(Path(database)),
        trigger=trigger,
        card_level_versions=card_level,
        effect_level_versions=effect_level,
        affected_versions=affected,
        direct_versions=direct,
        co_blocked_versions=(),
        effect_rows=effects,
        coverage=coverage,
    )


load_catalog = load_plan2_card_and_effect_aggressive_up6_catalog


ANDROID_V323_PC_METADATA_EVIDENCE: Final = {
    "android_v3_2_3": {
        "field_predicate": {
            "source": "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/ExamExtensions.txt",
            "symbol": "ExamExtensions.IsEffectTriggerFieldValid",
            "rva": "0x68072F4",
            "getter": "ExamStatusEffectCollection.GetAggressive(true)",
            "getter_rva": "0x7E99350",
            "fact": "reads signed current Aggressive; comparison is inclusive >= threshold",
        },
        "card_play_build": {
            "source": "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/Exam/ExamSequence.txt",
            "symbol": "ExamSequence.ExecuteCardCommandImpl",
            "set_playing": "ExamCardMoveController.SetPlayingCard",
            "play_trigger_list": "ExamCardData.get_PlayEffectTriggerList",
            "predicate": "ExamExtensions.IsEffectTriggerFieldValid",
            "factory": "ExamPlayCommand.CreatePlayEffectCommand(effect)",
            "fact": "direct-effect gates are selected while the direct sequence is built",
        },
        "transaction_order": [
            "SetPlayingCard / capture card-play context",
            "card-level play usability gate using current Aggressive",
            "payment and cost-difference commands",
            "direct-effect sequence build in Master slot order",
            "execute queued direct effects",
            "UserCardAfterCheck / MovePlayCard / play-count updates",
        ],
        "snapshot_rule": "earlier direct effects have not executed when later PlayEffectTriggerList entries are gated; use one direct-sequence build snapshot",
        "forced_extra": "normal, forced, and extra paths share the same Playing/direct-effect predicate; source/cost/play-count policy is outside this leaf",
    },
    "pc_named_queries": {
        "trigger_source": "_research/gakumasu-diff/ProduceExamTrigger.yaml:e_trigger-none-card_play_aggressive_up-6",
        "card_source": "_research/gakumasu-diff/ProduceCard.yaml:p_card-02-act-100_009,p_card-02-sup-3_181,p_card-02-sup-3_178",
        "effect_source": "_research/gakumasu-diff/ProduceExamEffect.yaml:e_effect-exam_card_draw-0002",
        "structural_source": "var/coverage/android_pc_exam_metadata_compatibility.json",
        "fact": "PC metadata is a named structural cross-check; Android v3.2.3 owns native timing/semantics",
    },
}


def build_plan2_card_and_effect_aggressive_up6_audit(
    database: Path | str = DEFAULT_DATABASE,
    coverage_artifact: Path | str = DEFAULT_COVERAGE_ARTIFACT,
    *,
    focused_test_count: int | None = None,
) -> dict[str, object]:
    """Build the leaf audit payload without hashes, clocks, or central writes."""

    catalog = load_plan2_card_and_effect_aggressive_up6_catalog(
        database, coverage_artifact
    )
    payload: dict[str, object] = {
        "schema_version": 1,
        "scope": (
            "Plan2/Common exact standalone shared trigger: "
            f"{TARGET_TRIGGER_ID}; card-level and direct-effect-level uses"
        ),
        "validation": {
            "internet": "not_used",
            "hash_verification": "not_performed",
            "security_verification": "not_performed",
            "clock_verification": "not_performed",
            "master_access": "read_only",
            "central_coverage": "read_only_reference; not rebuilt or modified",
        },
        "target": {
            "trigger_id": TARGET_TRIGGER_ID,
            "threshold": TARGET_THRESHOLD,
            "card_level_play_gate": {
                "version_count": len(catalog.card_level_versions),
                "version_refs": catalog.summary()["cardLevelRefs"],
                "gap": f"C:card-trigger:{TARGET_TRIGGER_ID}",
            },
            "effect_level_direct_effect_gate": {
                "version_count": len(catalog.effect_level_versions),
                "version_refs": catalog.summary()["effectLevelRefs"],
                "target_slot_index": TARGET_EFFECT_SLOT_INDEX,
                "target_effect_id": TARGET_EFFECT_ID,
                "gap": f"C:effect-trigger:{TARGET_TRIGGER_ID}",
            },
            "affected_direct_co_blocked": {
                "affected": catalog.affected_count,
                "direct": catalog.direct_count,
                "co_blocked": catalog.co_blocked_count,
            },
        },
        "master_snapshot": {
            "database": str(database),
            "trigger": catalog.trigger.to_dict(),
            "target_effect": catalog.target_effect.to_dict(),
            "card_level_slots": [
                {
                    "card_id": card.card_id,
                    "upgrade": card.upgrade_count,
                    "play_trigger_id": card.play_trigger_id,
                    "ordered_effect_ids": [slot.effect_id for slot in card.effect_slots],
                }
                for card in catalog.card_level_versions
            ],
            "effect_level_slots": [
                {
                    "card_id": card.card_id,
                    "upgrade": card.upgrade_count,
                    "target_slots": [slot.to_dict() for slot in card.effect_slots if slot.trigger_id == TARGET_TRIGGER_ID],
                    "ordered_effect_ids": [slot.effect_id for slot in card.effect_slots],
                }
                for card in catalog.effect_level_versions
            ],
        },
        "coverage_readback": catalog.coverage.to_dict(),
        "native_sources": ANDROID_V323_PC_METADATA_EVIDENCE,
        "exact_semantics": {
            "card_level": "play-usability gate at Playing/before payment and before direct effects",
            "effect_level": "direct-effect sequence gate while PlayEffect commands are built",
            "field_status": "signed current Aggressive >= 6, inclusive equality",
            "not": "unsupported and fail-closed",
            "phase_search_value_shape": "unsupported and fail-closed outside exact None/no-search/[6] shape",
            "counters_and_delta": "global/per-turn play counts and observed Aggressive delta are inert",
            "forced_extra": "normal, forced, and extra use the same gate; no bypass is inferred",
            "same_card_snapshot": "all direct-effect trigger slots use the sequence-build snapshot before queued effects execute",
        },
        "scope_guard": {
            "new_runtime": "src/gkms_tool/plan2_card_and_effect_aggressive_up6.py",
            "new_tests": ["tests/test_plan2_card_and_effect_aggressive_up6.py"],
            "central_coverage_read_only": "var/coverage/plan2_card_executable_coverage.json",
            "central_coverage_rebuilt": False,
            "central_coverage_modified": False,
            "forbidden_layers_modified": [],
            "plan2_state_modified": False,
            "plan2_core_runtime_modified": False,
            "plan3_modified": False,
            "gui_or_outer_modified": False,
        },
        "focused_verification": {
            "command": ".venv\\Scripts\\python.exe -m pytest -q tests/test_plan2_card_and_effect_aggressive_up6.py",
            "result": (
                f"{focused_test_count} passed"
                if focused_test_count is not None
                else "not_recorded"
            ),
            "scope": "new file only; no full-suite or broad pytest run",
        },
    }
    return payload


__all__ = [
    "ANDROID_V323_PC_METADATA_EVIDENCE",
    "AggressiveGateBoundary",
    "AggressiveGateEvaluation",
    "AggressiveGateInput",
    "AggressiveGateKind",
    "AggressiveUp6CatalogError",
    "CoverageReadback",
    "DEFAULT_COVERAGE_ARTIFACT",
    "DirectEffectSequenceEvaluation",
    "EXACT_TARGET_TRIGGER",
    "ExactAggressiveUp6Contract",
    "ExactContractResolution",
    "Plan2CardAndEffectAggressiveUp6Catalog",
    "TARGET_AFFECTED_REFS",
    "TARGET_CARD_LEVEL_REFS",
    "TARGET_EFFECT_ID",
    "TARGET_EFFECT_LEVEL_REFS",
    "TARGET_EFFECT_SLOT_INDEX",
    "TARGET_EFFECT_TYPE",
    "TARGET_THRESHOLD",
    "TARGET_TRIGGER_ID",
    "build_plan2_card_and_effect_aggressive_up6_audit",
    "evaluate_card_play_aggressive_up6_trigger",
    "evaluate_card_play_gate",
    "evaluate_card_trigger",
    "evaluate_direct_effect_aggressive_up6_trigger",
    "evaluate_direct_effect_gate",
    "evaluate_effect_trigger",
    "load_catalog",
    "load_coverage_readback",
    "load_plan2_card_and_effect_aggressive_up6_catalog",
    "resolve_exact_aggressive_up6_trigger",
    "simulate_direct_effect_sequence",
]
