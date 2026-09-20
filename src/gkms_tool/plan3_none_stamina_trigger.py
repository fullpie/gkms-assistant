"""Exact ``None + StaminaUpMultiple(500)`` trigger catalog.

``ProduceExamPhaseType_None`` is a concrete direct-effect predicate phase in
Master.  It is not a wildcard for arbitrary runtime events.  The one row in
this module reuses the Android-v3.2.3 stamina formula while retaining every
Master trigger column and the four card versions that reference it.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .plan3_stamina_multiple_trigger import (
    FIELD_STAMINA_UP_MULTIPLE,
    StaminaField,
    StaminaMultipleEvaluation,
    StaminaSnapshot,
    evaluate_stamina_multiple,
)


PHASE_NONE = "ProduceExamPhaseType_None"
LESSON_UNKNOWN = "ProduceStepLessonType_Unknown"
MOVE_UNKNOWN = "ProduceCardMovePositionType_Unknown"
TRIGGER_NONE_STAMINA_UP_MULTIPLE_500 = (
    "e_trigger-none-stamina_up_multiple-500"
)


@dataclass(frozen=True, slots=True)
class NoneStaminaTrigger:
    """One exact Master row; no id-derived fields are accepted."""

    id: str
    phase_types: tuple[str, ...]
    phase_values: tuple[int, ...]
    field_check_types: tuple[str, ...]
    field_types: tuple[str, ...]
    field_values: tuple[int, ...]
    field_card_search_ids: tuple[str, ...]
    produce_card_search_id: str
    upper_search_count: int
    lower_search_count: int
    card_move_position_type: str
    effect_types: tuple[str, ...]
    lesson_type: str

    def master_contract(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "phase_types": list(self.phase_types),
            "phase_values": list(self.phase_values),
            "field_check_types": list(self.field_check_types),
            "field_types": list(self.field_types),
            "field_values": list(self.field_values),
            "field_card_search_ids": list(self.field_card_search_ids),
            "produce_card_search_id": self.produce_card_search_id,
            "upper_search_count": self.upper_search_count,
            "lower_search_count": self.lower_search_count,
            "card_move_position_type": self.card_move_position_type,
            "effect_types": list(self.effect_types),
            "lesson_type": self.lesson_type,
        }


NONE_STAMINA_UP_MULTIPLE_500 = NoneStaminaTrigger(
    id=TRIGGER_NONE_STAMINA_UP_MULTIPLE_500,
    phase_types=(PHASE_NONE,),
    phase_values=(),
    field_check_types=(),
    field_types=(FIELD_STAMINA_UP_MULTIPLE,),
    field_values=(500,),
    field_card_search_ids=(),
    produce_card_search_id="",
    upper_search_count=0,
    lower_search_count=0,
    card_move_position_type=MOVE_UNKNOWN,
    effect_types=(),
    lesson_type=LESSON_UNKNOWN,
)

NONE_STAMINA_TRIGGER_BY_ID: Mapping[str, NoneStaminaTrigger] = (
    MappingProxyType(
        {NONE_STAMINA_UP_MULTIPLE_500.id: NONE_STAMINA_UP_MULTIPLE_500}
    )
)

AFFECTED_CARD_VERSIONS: tuple[tuple[str, int], ...] = tuple(
    ("p_card-00-sup-3_161", upgrade) for upgrade in range(4)
)


def matches_trigger_contract(trigger: Any) -> bool:
    """Return true only for the complete stored Master contract."""

    row = NONE_STAMINA_TRIGGER_BY_ID.get(str(getattr(trigger, "id", "")))
    if row is None:
        return False
    for name in (
        "phase_types",
        "phase_values",
        "field_check_types",
        "field_types",
        "field_values",
        "field_card_search_ids",
        "effect_types",
    ):
        try:
            actual = tuple(getattr(trigger, name))
        except (AttributeError, TypeError):
            return False
        if actual != getattr(row, name):
            return False
    for name in (
        "produce_card_search_id",
        "upper_search_count",
        "lower_search_count",
        "card_move_position_type",
        "lesson_type",
    ):
        if getattr(trigger, name, object()) != getattr(row, name):
            return False
    return True


def evaluate(
    trigger: Any,
    snapshot: StaminaSnapshot,
    *,
    event_phase: str,
) -> StaminaMultipleEvaluation | None:
    """Evaluate the exact row, or return ``None`` for an unknown contract."""

    if not matches_trigger_contract(trigger):
        return None
    if event_phase != PHASE_NONE:
        return StaminaMultipleEvaluation(
            field_type=StaminaField.STAMINA_UP_MULTIPLE,
            threshold=500,
            fires=False,
            reasons=("concrete-none-phase-did-not-match",),
        )
    return evaluate_stamina_multiple(
        StaminaField.STAMINA_UP_MULTIPLE,
        500,
        snapshot,
    )


__all__ = [
    "AFFECTED_CARD_VERSIONS",
    "LESSON_UNKNOWN",
    "MOVE_UNKNOWN",
    "NONE_STAMINA_TRIGGER_BY_ID",
    "NONE_STAMINA_UP_MULTIPLE_500",
    "NoneStaminaTrigger",
    "PHASE_NONE",
    "TRIGGER_NONE_STAMINA_UP_MULTIPLE_500",
    "evaluate",
    "matches_trigger_contract",
]
