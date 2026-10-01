"""Screen-side proof used to publish a live zone reconciliation.

The LocalSave projection proves exact hidden card zones, while two fresh Maa
PrintWindow frames prove that the user-visible game is settled at that same
state.  This module canonicalizes the visible proof and requires exact
agreement before its digest may be placed in a reconciliation artifact.
It never captures, clicks, or writes files itself.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Mapping

from .audition_horizon import VerifiedTurnSchedule
from .audition_local_save_state import (
    AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION,
    AuditionLocalSaveStateEvidence,
    verified_turn_schedule_from_local_save,
)
from .live_actions import CardPlayFrameAnalysis


RECONCILIATION_SCREEN_SEMANTIC_SCHEMA_VERSION = 1
MINIMUM_SETTLED_CONFIDENCE = 0.80
MINIMUM_SUPPORT_MARKER_SCORE = 0.60
MINIMUM_SUPPORT_IDENTITY_SCORE = 0.60
MINIMUM_SUPPORT_IDENTITY_MARGIN = 0.20


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _confidence(value: object, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{label} must be a finite numeric confidence")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"{label} must be between zero and one")
    return result


@dataclass(frozen=True, slots=True)
class ReconciliationScreenHandCard:
    slot: int
    card_id: str
    effective_upgrade: int
    displayed_cost: int
    detection_label: str
    support_marker_id: str | None

    def __post_init__(self) -> None:
        _integer(self.slot, "slot")
        _text(self.card_id, "card_id")
        _integer(self.effective_upgrade, "effective_upgrade")
        _integer(self.displayed_cost, "displayed_cost")
        _text(self.detection_label, "detection_label")
        if self.support_marker_id is not None:
            _text(self.support_marker_id, "support_marker_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "slot": self.slot,
            "card_id": self.card_id,
            "effective_upgrade": self.effective_upgrade,
            "displayed_cost": self.displayed_cost,
            "detection_label": self.detection_label,
            "support_marker_id": self.support_marker_id,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationScreenSemantic:
    schema_version: int
    turns_remaining: int
    score: int
    stamina: int
    block: int
    score_multiplier_permille: int
    hand: tuple[ReconciliationScreenHandCard, ...]
    remaining_schedule: tuple[tuple[int, str, int], ...]

    def __post_init__(self) -> None:
        schema_version = _integer(
            self.schema_version,
            "schema_version",
            minimum=1,
        )
        if schema_version != RECONCILIATION_SCREEN_SEMANTIC_SCHEMA_VERSION:
            raise ValueError("unsupported reconciliation screen semantic schema")
        for label in (
            "turns_remaining",
            "score",
            "stamina",
            "block",
            "score_multiplier_permille",
        ):
            _integer(getattr(self, label), label)
        hand = tuple(self.hand)
        if not hand or not all(
            isinstance(value, ReconciliationScreenHandCard) for value in hand
        ):
            raise ValueError("hand must contain screen card values")
        if tuple(value.slot for value in hand) != tuple(range(len(hand))):
            raise ValueError("screen Hand slots must be contiguous and ordered")
        object.__setattr__(self, "hand", hand)
        schedule = tuple(tuple(value) for value in self.remaining_schedule)
        if not schedule:
            raise ValueError("remaining_schedule must not be empty")
        prior_round = 0
        for index, raw in enumerate(schedule):
            if len(raw) != 3:
                raise ValueError(f"remaining_schedule[{index}] is malformed")
            round_number, lesson_type, multiplier = raw
            round_number = _integer(
                round_number, f"remaining_schedule[{index}].round_number", minimum=1
            )
            _text(lesson_type, f"remaining_schedule[{index}].lesson_type")
            _integer(
                multiplier,
                f"remaining_schedule[{index}].score_multiplier_permille",
                minimum=1,
            )
            if prior_round and round_number != prior_round + 1:
                raise ValueError("remaining_schedule rounds must be contiguous")
            prior_round = round_number
        object.__setattr__(self, "remaining_schedule", schedule)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "turns_remaining": self.turns_remaining,
            "score": self.score,
            "stamina": self.stamina,
            "block": self.block,
            "score_multiplier_permille": self.score_multiplier_permille,
            "hand": [value.to_dict() for value in self.hand],
            "remaining_schedule": [
                {
                    "round_number": round_number,
                    "lesson_type": lesson_type,
                    "score_multiplier_permille": multiplier,
                }
                for round_number, lesson_type, multiplier in self.remaining_schedule
            ],
        }

    def canonical_json(self) -> str:
        return _canonical_json(self.to_dict())

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def _marker_bindings(
    analysis: CardPlayFrameAnalysis,
) -> dict[int, str]:
    observation = analysis.observed_state.get("_observation")
    if not isinstance(observation, Mapping):
        raise ValueError("settled frame has no observation metadata")
    evidence = observation.get("evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("settled frame has no evidence metadata")
    raw_markers = evidence.get("support_upgrade_markers")
    if not isinstance(raw_markers, list):
        raise ValueError("settled frame support marker evidence is malformed")
    result: dict[int, str] = {}
    for raw in raw_markers:
        if not isinstance(raw, Mapping) or raw.get("detected") is not True:
            raise ValueError("settled frame contains a non-positive support marker")
        slot = _integer(raw.get("hand_index"), "support marker hand_index")
        support_id = _text(raw.get("support_card_id"), "support marker ID")
        if slot in result:
            raise ValueError("settled frame repeats a support marker slot")
        score = _confidence(raw.get("score"), "support marker score")
        # Retain strict diagnostic serialization without using either value
        # as an authorization threshold.  Ordered LocalSave owns identity.
        _confidence(raw.get("identity_score"), "support marker identity score")
        _confidence(raw.get("identity_margin"), "support marker identity margin")
        if score < MINIMUM_SUPPORT_MARKER_SCORE:
            raise ValueError("support marker score is below the safe threshold")
        # The exact support identity is owned by the ordered LocalSave Hand.
        # Screen evidence proves only the fixed-slot ``+`` topology; artwork
        # similarity and colour are intentionally not part of the gate.
        result[slot] = support_id
    return result


def screen_semantic_from_analysis(
    analysis: CardPlayFrameAnalysis,
    schedule: VerifiedTurnSchedule,
) -> ReconciliationScreenSemantic:
    """Canonicalize one independently recognized, settled Plan2 Maa frame."""

    if not isinstance(analysis, CardPlayFrameAnalysis):
        raise TypeError("analysis must be CardPlayFrameAnalysis")
    if not isinstance(schedule, VerifiedTurnSchedule):
        raise TypeError("schedule must be VerifiedTurnSchedule")
    schedule.validate()
    if analysis.phase != "settled" or analysis.expected_matches is not True:
        raise ValueError("reconciliation requires an expected settled frame")
    analysis_confidence = _confidence(
        analysis.confidence,
        "settled frame confidence",
    )
    if analysis_confidence < MINIMUM_SETTLED_CONFIDENCE:
        raise ValueError("settled frame confidence is below the safe threshold")
    semantic = analysis.semantic_key
    if not isinstance(semantic, tuple) or not semantic:
        raise ValueError("settled frame semantic key is not a Plan2 Hand")
    if semantic[0] == "exam" and len(semantic) == 7:
        turns_remaining = semantic[1]
        score = semantic[2]
        stamina = semantic[3]
        block = semantic[4]
        score_multiplier_permille = semantic[5]
        raw_hand = semantic[6]
    elif semantic[0] == "lesson" and len(semantic) == 8:
        turns_remaining = semantic[1]
        score = analysis.observed_state.get("score")
        stamina = semantic[3]
        block = semantic[4]
        score_multiplier_permille = 1000
        raw_hand = semantic[7]
    else:
        raise ValueError("settled frame semantic key is not a supported Plan2 Hand")
    if not isinstance(raw_hand, tuple):
        raise ValueError("settled frame semantic key has no ordered Hand")
    markers = _marker_bindings(analysis)
    if any(slot >= len(raw_hand) for slot in markers):
        raise ValueError("support marker hand_index is outside the settled Hand")
    hand: list[ReconciliationScreenHandCard] = []
    for expected_slot, raw in enumerate(raw_hand):
        if not isinstance(raw, tuple) or len(raw) != 7:
            raise ValueError("settled Hand entry lacks support-marker identity")
        slot, card_id, upgrade, cost, label, marker_detected, marker_id = raw
        slot = _integer(slot, f"hand[{expected_slot}].slot")
        if slot != expected_slot:
            raise ValueError("settled Hand slots are not contiguous")
        detailed_id = markers.pop(expected_slot, None)
        if marker_detected is True:
            if marker_id != detailed_id:
                raise ValueError("semantic and detailed support marker IDs disagree")
        elif marker_detected is False:
            if marker_id is not None or detailed_id is not None:
                raise ValueError("unmarked Hand slot has support marker evidence")
        else:
            raise ValueError("support marker flag must be boolean")
        hand.append(
            ReconciliationScreenHandCard(
                slot=expected_slot,
                card_id=_text(card_id, f"hand[{expected_slot}].card_id"),
                effective_upgrade=_integer(
                    upgrade, f"hand[{expected_slot}].effective_upgrade"
                ),
                displayed_cost=_integer(cost, f"hand[{expected_slot}].displayed_cost"),
                detection_label=_text(label, f"hand[{expected_slot}].label"),
                support_marker_id=detailed_id,
            )
        )
    if markers:
        raise ValueError("settled frame contains unconsumed support marker evidence")
    return ReconciliationScreenSemantic(
        schema_version=RECONCILIATION_SCREEN_SEMANTIC_SCHEMA_VERSION,
        turns_remaining=_integer(turns_remaining, "turns_remaining"),
        score=_integer(score, "score"),
        stamina=_integer(stamina, "stamina"),
        block=_integer(block, "block"),
        score_multiplier_permille=_integer(
            score_multiplier_permille,
            "score_multiplier_permille",
            minimum=1,
        ),
        hand=tuple(hand),
        remaining_schedule=tuple(
            (
                frame.round_number,
                frame.lesson_type,
                frame.score_multiplier_permille,
            )
            for frame in schedule.frames
        ),
    )


def validate_screen_semantic_against_local_save(
    semantic: ReconciliationScreenSemantic,
    evidence: AuditionLocalSaveStateEvidence,
) -> None:
    """Require every visible decision field to equal ExamSaveData exactly."""

    if not isinstance(semantic, ReconciliationScreenSemantic):
        raise TypeError("semantic must be ReconciliationScreenSemantic")
    if not isinstance(evidence, AuditionLocalSaveStateEvidence):
        raise TypeError("evidence must be AuditionLocalSaveStateEvidence")
    state = evidence.state
    if (
        _integer(evidence.schema_version, "ExamSaveData schema_version", minimum=1)
        != AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION
    ):
        raise ValueError("ExamSaveData evidence must use exact LocalSave schema v5")
    if not state.has_complete_root_runtime_state:
        raise ValueError(
            "ExamSaveData root runtime state is unknown; schema-v5 evidence is required"
        )
    if not state.is_native_actionable_settled:
        raise ValueError("ExamSaveData is not a settled native actionable state")
    if not state.has_complete_card_runtime_state:
        raise ValueError(
            "ExamSaveData card runtime state is unknown; schema-v4 evidence is required"
        )
    audition = state.exam_type == 1 and state.step_type_value in {16, 17, 18}
    lesson = state.exam_type == 0 and state.step_type_value in set(range(1, 10))
    if (
        not (audition or lesson)
        or state.phase != 6
        or state.extra_turn != 0
        or state.is_turn_card_play_end
        or state.playing_card is not None
        or state.removed_cards
        or state.future_deck
        or state.past_deck is None
        or state.past_deck
        or state.zones.hold
    ):
        raise ValueError("ExamSaveData is not a settled supported Main phase")
    if lesson:
        if state.turn_parameter_types:
            raise ValueError("Plan2 lesson ExamSaveData must carry an empty schedule")
        current_multiplier = 1000
    else:
        multiplier_by_parameter = {
            1: state.vocal_bonus_permille,
            2: state.dance_bonus_permille,
            3: state.visual_bonus_permille,
        }
        current_parameter = state.turn_parameter_types[state.current_turn - 1]
        current_multiplier = multiplier_by_parameter[current_parameter]
    expected_hud = (
        state.remain_turn,
        state.score,
        state.stamina,
        state.block,
        current_multiplier,
    )
    actual_hud = (
        semantic.turns_remaining,
        semantic.score,
        semantic.stamina,
        semantic.block,
        semantic.score_multiplier_permille,
    )
    if actual_hud != expected_hud:
        raise ValueError(
            f"Maa HUD differs from ExamSaveData: {actual_hud!r} != {expected_hud!r}"
        )
    expected_hand: list[tuple[str, int, str | None]] = []
    for card in state.zones.hand:
        if len(card.support_upgrade_ids) > 1:
            raise ValueError(
                "multiple support upgrades on one Hand card need a richer screen proof"
            )
        expected_hand.append(
            (
                card.card_id,
                card.effective_upgrade,
                card.support_upgrade_ids[0] if card.support_upgrade_ids else None,
            )
        )
    actual_hand = [
        (card.card_id, card.effective_upgrade, card.support_marker_id)
        for card in semantic.hand
    ]
    if actual_hand != expected_hand:
        raise ValueError(
            f"Maa Hand differs from ExamSaveData: {actual_hand!r} != {expected_hand!r}"
        )
    local_schedule = verified_turn_schedule_from_local_save(evidence)
    expected_schedule = tuple(
        (
            frame.round_number,
            frame.lesson_type,
            frame.score_multiplier_permille,
        )
        for frame in local_schedule.frames
    )
    if semantic.remaining_schedule != expected_schedule:
        raise ValueError("Maa ring schedule differs from ExamSaveData")


def validate_stable_screen_pair_against_local_save(
    first: CardPlayFrameAnalysis,
    second: CardPlayFrameAnalysis,
    *,
    schedule: VerifiedTurnSchedule,
    evidence: AuditionLocalSaveStateEvidence,
) -> ReconciliationScreenSemantic:
    """Return one canonical proof only when both fresh frames agree exactly."""

    if first.semantic_key != second.semantic_key:
        raise ValueError("fresh Maa frames do not have identical semantics")
    if (
        isinstance(first.semantic_key, tuple)
        and first.semantic_key
        and first.semantic_key[0] == "lesson"
    ):
        if len(first.semantic_key) != 8:
            raise ValueError("settled lesson semantic key is malformed")
        runtime = evidence.state.root_runtime
        opaque = None if runtime is None else runtime.opaque_fields.to_value()
        base_parameter = (
            None if not isinstance(opaque, Mapping) else opaque.get("produceTargetParameter")
        )
        expected_parameter = (
            None
            if isinstance(base_parameter, bool) or not isinstance(base_parameter, int)
            else base_parameter + evidence.state.score
        )
        if (
            expected_parameter is None
            or first.semantic_key[5] != expected_parameter
        ):
            raise ValueError("Maa lesson parameter differs from ExamSaveData")
    left = screen_semantic_from_analysis(first, schedule)
    right = screen_semantic_from_analysis(second, schedule)
    if left != right or left.digest() != right.digest():
        raise ValueError("fresh Maa frames do not have identical semantics")
    validate_screen_semantic_against_local_save(left, evidence)
    return left


__all__ = [
    "MINIMUM_SETTLED_CONFIDENCE",
    "RECONCILIATION_SCREEN_SEMANTIC_SCHEMA_VERSION",
    "ReconciliationScreenHandCard",
    "ReconciliationScreenSemantic",
    "screen_semantic_from_analysis",
    "validate_screen_semantic_against_local_save",
    "validate_stable_screen_pair_against_local_save",
]
