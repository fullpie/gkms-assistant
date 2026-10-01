"""Fail-closed full-deck horizon search for verified Plan 2 auditions.

This module owns *belief-state* card zones.  The numerical card/status rules
remain in :mod:`gkms_tool.logic_engine`, while equipped-item rules remain in
:mod:`gkms_tool.item_rules`.

The zone lifecycle is backed by the pinned Android v3.2.3 client:

* ``ExamCardMoveController.DrawCard`` (RVA ``0x809DE54`` in the pinned
  ``libil2cpp.so``) draws from Deck and calls ``ReplaceGraveToDeck`` when the
  requested count exceeds the cards left in Deck.
* ``ReplaceGraveToDeck`` (RVA ``0x809D7DC``) moves Grave to Deck and shuffles
  it before the remaining draw.  ``ResetHand`` (RVA ``0x809EEC0``) handles
  the unplayed-Hand boundary.
* ``ExamSetting.turnStartDistribute`` is the ordinary turn-start count.

The exact future audition attribute order is deliberately *not* inferred
from Master.  ``ExamParameterModel.CalcTurnParameterType`` constructs and
shuffles ``TurnTargetStatusParameterTypeList`` from the run RNG seed.  Master
contains the attribute counts/configuration, but not the already advanced RNG
state for a live run.  A caller must therefore provide a digest-bound
``VerifiedTurnSchedule`` (from observed/replayed state or a seed-accurate
reconstruction).  Missing frames block the search instead of reusing the
current multiplier for future turns.

Unknown Deck order is represented as a multiset.  Ordinary draws enumerate
distinct multisets with exact hypergeometric probability.  When HandAdd
support rules make sequence order observable, an exact chance-state DP draws
one card at a time and immediately merges histories with the same remaining
multiset, support-use mask, and added-Hand multiset.  No order is guessed and
no probability is sampled.  Limits are safety limits, not sampling knobs:
exceeding a limit returns a concrete blocker and no decision-ready
recommendation.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Iterable, Mapping

from .audition_rules import AuditionRules
from .audition_support_runtime import (
    SupportUpgradeRuntimeInput,
    support_lesson_type_matches,
)
from .card_search import ProduceCardSearchRule, match_support_hand_add_search
from .deck_snapshot import DeckCardStack, DeckSnapshot
from .exam_context import CurrentTurnBoundary
from .item_rules import (
    EquippedItemRule,
    ResolvedItemEffects,
    resolve_item_end_turn,
    resolve_item_turn_start,
)
from .logic_engine import (
    EFFECT_CARD_CREATE_ID,
    EFFECT_CARD_DRAW,
    EFFECT_CARD_MOVE,
    LogicExamState,
    LogicTransition,
    MasterCard,
    MasterCardEffect,
    apply_logic_card,
    apply_logic_turn_skip,
    complete_audition_if_forced,
    load_master_card,
    resolve_logic_status_turn_start,
)


MOVE_GRAVE = "ProduceCardMovePositionType_Grave"
MOVE_LOST = "ProduceCardMovePositionType_Lost"
PLAN_COMMON = "ProducePlanType_Common"
PLAN_LOGIC = "ProducePlanType_Plan2"
LESSON_VOCAL = "ProduceStepLessonType_LessonVocal"
LESSON_DANCE = "ProduceStepLessonType_LessonDance"
LESSON_VISUAL = "ProduceStepLessonType_LessonVisual"
SUPPORTED_LESSON_TYPES = frozenset(
    {LESSON_VOCAL, LESSON_DANCE, LESSON_VISUAL}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERIFIED_SCHEDULE_SOURCE_PREFIXES = (
    "dual-stable-screen-ring-v1:",
    "native-seed-reconstruction-v1:",
    "exam-save-data-local-save-v1:",
)

_HORIZON_SKIP_ITEM_CARD = MasterCard(
    id="gkms-tool-horizon-turn-skip",
    upgrade=0,
    name="END_TURN",
    plan_type=PLAN_COMMON,
    category="",
    stamina_cost=0,
    cost_type="ExamCostType_Unknown",
    cost_value=0,
    play_trigger_id="",
    move_position_type="ProduceCardMovePositionType_Unknown",
    effects=(),
)


AuditionBoundaryOutcome = CurrentTurnBoundary


@dataclass(frozen=True, slots=True)
class AuditionBoundaryState:
    """The explicit native outcome of one closed-turn boundary.

    ``LogicExamState`` intentionally remains the numerical card kernel state;
    this companion value carries the native loop state that the kernel does
    not own.  In particular, an exam can finish before a next card-selection
    frame is materialized.
    """

    outcome: AuditionBoundaryOutcome
    exam_end_complete: bool = False
    recovery_units: int = 0

    def __post_init__(self) -> None:
        try:
            outcome = AuditionBoundaryOutcome(self.outcome)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"unsupported audition boundary: {self.outcome!r}"
            ) from error
        object.__setattr__(self, "outcome", outcome)
        if type(self.exam_end_complete) is not bool:
            raise TypeError("exam_end_complete must be a boolean")
        if (
            not isinstance(self.recovery_units, int)
            or isinstance(self.recovery_units, bool)
            or self.recovery_units < 0
        ):
            raise ValueError("recovery_units must be a non-negative integer")
        if outcome is AuditionBoundaryOutcome.EXAM_END:
            if not self.exam_end_complete:
                raise ValueError("EXAM_END requires exam_end_complete")
        elif self.exam_end_complete:
            raise ValueError("NEXT_TURN cannot be exam_end_complete")
        elif self.recovery_units:
            raise ValueError("NEXT_TURN cannot carry terminal recovery units")

    @classmethod
    def next_turn(cls) -> "AuditionBoundaryState":
        return cls(AuditionBoundaryOutcome.NEXT_TURN, False, 0)

    @classmethod
    def exam_end(cls, recovery_units: int) -> "AuditionBoundaryState":
        return cls(AuditionBoundaryOutcome.EXAM_END, True, recovery_units)

    @property
    def terminal(self) -> bool:
        return self.outcome is AuditionBoundaryOutcome.EXAM_END

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "exam_end_complete": self.exam_end_complete,
            "recovery_units": self.recovery_units,
        }


@dataclass(frozen=True, slots=True)
class AuditionBoundaryModel:
    """Resolve the native last-boundary branch from explicit turn fields.

    The model deliberately does not inspect ``remainTurn``.  The native loop
    chooses its last-boundary path from the next turn ordinal and the saved
    extra-turn count; the remaining-unit value is used only after that branch
    has been selected for terminal stamina recovery.
    """

    current_turn: int
    limit_turn: int
    extra_turn: int = 0
    exam_end_complete: bool = False

    def __post_init__(self) -> None:
        for name in ("current_turn", "limit_turn", "extra_turn"):
            value = getattr(self, name)
            minimum = 1 if name != "extra_turn" else 0
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < minimum
            ):
                raise ValueError(
                    f"{name} must be a {'positive' if minimum else 'non-negative'} integer"
                )
        if self.current_turn > self.limit_turn:
            raise ValueError("current_turn exceeds limit_turn")
        if type(self.exam_end_complete) is not bool:
            raise TypeError("exam_end_complete must be a boolean")

    def resolve(
        self,
        *,
        next_turn: int,
        post_boundary_recovery_units: int,
    ) -> AuditionBoundaryState:
        if (
            not isinstance(next_turn, int)
            or isinstance(next_turn, bool)
            or next_turn < 1
        ):
            raise ValueError("next_turn must be a positive integer")
        if next_turn < self.current_turn:
            raise ValueError("next_turn cannot precede current_turn")
        if (
            not isinstance(post_boundary_recovery_units, int)
            or isinstance(post_boundary_recovery_units, bool)
            or post_boundary_recovery_units < 0
        ):
            raise ValueError(
                "post_boundary_recovery_units must be a non-negative integer"
            )
        if self.exam_end_complete or (
            next_turn >= self.limit_turn and self.extra_turn == 0
        ):
            return AuditionBoundaryState.exam_end(
                post_boundary_recovery_units
            )
        return AuditionBoundaryState.next_turn()


@dataclass(frozen=True, order=True, slots=True)
class HorizonCardRef:
    card_id: str
    upgrade: int = 0

    def validate(self) -> None:
        if not self.card_id:
            raise ValueError("horizon card id is empty")
        if self.upgrade < 0:
            raise ValueError("horizon card upgrade must be non-negative")

    @property
    def key(self) -> str:
        return f"{self.card_id}@{self.upgrade}"


MAX_SUPPORT_UPGRADE = 3
DEFAULT_MAX_CHANCE_OUTCOMES = 8_192
DEFAULT_MAX_CHANCE_DP_STATES = 50_000
DEFAULT_MAX_TERMINAL_POLICY_STATES = 1_000_000
@dataclass(frozen=True, order=True, slots=True)
class HorizonHandEntry:
    """One ordered Hand slot with separate conserved and playable refs."""

    base_ref: HorizonCardRef
    effective_ref: HorizonCardRef
    support_delta: int = 0

    def validate(self) -> None:
        self.base_ref.validate()
        self.effective_ref.validate()
        if self.base_ref.card_id != self.effective_ref.card_id:
            raise ValueError("Hand base/effective refs must have the same card ID")
        if not isinstance(self.support_delta, int) or isinstance(
            self.support_delta, bool
        ):
            raise ValueError("Hand support_delta must be an integer")
        if self.support_delta < 0 or self.support_delta > MAX_SUPPORT_UPGRADE:
            raise ValueError("Hand support_delta must be in the range 0..3")
        if self.effective_ref.upgrade != self.base_ref.upgrade + self.support_delta:
            raise ValueError(
                "Hand effective upgrade must equal base upgrade plus support_delta"
            )

    @property
    def key(self) -> str:
        return self.effective_ref.key


def _ordered_cards(cards: Iterable[HorizonCardRef]) -> tuple[HorizonCardRef, ...]:
    result = tuple(sorted(cards))
    for card in result:
        card.validate()
    return result


def _visible_cards(cards: Iterable[HorizonCardRef]) -> tuple[HorizonCardRef, ...]:
    """Validate a visible Hand without destroying its screen-slot order."""

    result = tuple(cards)
    for card in result:
        card.validate()
    return result


@dataclass(frozen=True, slots=True)
class AuditionCardZones:
    """Ordered effective Hand plus its conserved base-card ledger."""

    hand: tuple[HorizonCardRef, ...]
    draw_pile: tuple[HorizonCardRef, ...]
    discard_pile: tuple[HorizonCardRef, ...] = ()
    lost_pile: tuple[HorizonCardRef, ...] = ()
    hand_base: tuple[HorizonCardRef, ...] = ()
    hand_support_deltas: tuple[int, ...] = ()
    used_support_ids: tuple[str, ...] = ()
    hand_hold_count: int = 0

    def __post_init__(self) -> None:
        # Hand order is an observed UI fact.  Sorting it made ``hand_index``
        # refer to a canonical card order rather than the clickable screen
        # slot.  Hidden zones remain multisets and are canonicalized for
        # hashing/memoization.
        object.__setattr__(self, "hand", _visible_cards(self.hand))
        object.__setattr__(self, "draw_pile", _ordered_cards(self.draw_pile))
        object.__setattr__(
            self, "discard_pile", _ordered_cards(self.discard_pile)
        )
        object.__setattr__(self, "lost_pile", _ordered_cards(self.lost_pile))
        base = self.hand_base or self.hand
        object.__setattr__(self, "hand_base", _visible_cards(base))
        if len(self.hand_base) != len(self.hand):
            raise ValueError("Hand base refs must align one-to-one with effective Hand")
        deltas = self.hand_support_deltas
        if not deltas:
            deltas = tuple(
                effective.upgrade - source.upgrade
                for source, effective in zip(self.hand_base, self.hand, strict=True)
            )
        if len(deltas) != len(self.hand):
            raise ValueError("Hand support deltas must align one-to-one with Hand")
        object.__setattr__(self, "hand_support_deltas", tuple(deltas))
        for entry in self.hand_entries:
            entry.validate()
        used = tuple(self.used_support_ids)
        if any(not isinstance(value, str) or not value.strip() for value in used):
            raise ValueError("used_support_ids must contain non-empty text")
        if len(set(used)) != len(used):
            raise ValueError("used_support_ids must be unique")
        object.__setattr__(self, "used_support_ids", tuple(sorted(used)))
        if (
            not isinstance(self.hand_hold_count, int)
            or isinstance(self.hand_hold_count, bool)
            or self.hand_hold_count < 0
        ):
            raise ValueError("hand_hold_count must be an integer >= 0")

    @property
    def hand_entries(self) -> tuple[HorizonHandEntry, ...]:
        return tuple(
            HorizonHandEntry(source, effective, delta)
            for source, effective, delta in zip(
                self.hand_base,
                self.hand,
                self.hand_support_deltas,
                strict=True,
            )
        )

    @property
    def all_cards(self) -> tuple[HorizonCardRef, ...]:
        """The conserved persistent/base card multiset."""

        return _ordered_cards(
            (*self.hand_base, *self.draw_pile, *self.discard_pile, *self.lost_pile)
        )

    @property
    def catalog_cards(self) -> tuple[HorizonCardRef, ...]:
        return _ordered_cards((*self.all_cards, *self.hand))

    def remove_from_hand(self, card: HorizonCardRef) -> "AuditionCardZones":
        hand = list(self.hand)
        try:
            index = hand.index(card)
        except ValueError as exc:
            raise ValueError(f"card is absent from visible hand: {card.key}") from exc
        hand.pop(index)
        base = list(self.hand_base)
        deltas = list(self.hand_support_deltas)
        base.pop(index)
        deltas.pop(index)
        return replace(
            self,
            hand=tuple(hand),
            hand_base=tuple(base),
            hand_support_deltas=tuple(deltas),
        )

    def remove_hand_entry(self, entry: HorizonHandEntry) -> "AuditionCardZones":
        entries = list(self.hand_entries)
        try:
            index = entries.index(entry)
        except ValueError as exc:
            raise ValueError(f"Hand entry is absent: {entry.key}") from exc
        remaining = entries[:index] + entries[index + 1 :]
        return replace(
            self,
            hand=tuple(value.effective_ref for value in remaining),
            hand_base=tuple(value.base_ref for value in remaining),
            hand_support_deltas=tuple(value.support_delta for value in remaining),
        )


@dataclass(frozen=True, slots=True)
class VerifiedTurnFrame:
    round_number: int
    lesson_type: str
    score_multiplier_permille: int

    def validate(self) -> None:
        if self.round_number < 1:
            raise ValueError("turn frame round_number must be positive")
        if self.lesson_type not in SUPPORTED_LESSON_TYPES:
            raise ValueError(f"unsupported turn frame lesson_type: {self.lesson_type}")
        if self.score_multiplier_permille <= 0:
            raise ValueError("turn frame multiplier must be positive")


@dataclass(frozen=True, slots=True)
class VerifiedTurnSchedule:
    """Evidence-bound exact future attribute/multiplier sequence."""

    source: str
    evidence_sha256: str
    frames: tuple[VerifiedTurnFrame, ...]

    def validate(self) -> None:
        if not self.source:
            raise ValueError("turn schedule source is empty")
        if not self.source.startswith(_VERIFIED_SCHEDULE_SOURCE_PREFIXES):
            raise ValueError(
                "turn schedule source is not a verified provenance scheme"
            )
        if _SHA256_RE.fullmatch(self.evidence_sha256) is None:
            raise ValueError("turn schedule evidence_sha256 must be lowercase SHA-256")
        rounds: set[int] = set()
        for frame in self.frames:
            frame.validate()
            if frame.round_number in rounds:
                raise ValueError(
                    f"duplicate turn schedule round: {frame.round_number}"
                )
            rounds.add(frame.round_number)

    @property
    def by_round(self) -> Mapping[int, VerifiedTurnFrame]:
        self.validate()
        return {frame.round_number: frame for frame in self.frames}

    def digest(self) -> str:
        self.validate()
        canonical = "\n".join(
            (
                self.source,
                self.evidence_sha256,
                *(
                    f"{frame.round_number}|{frame.lesson_type}|"
                    f"{frame.score_multiplier_permille}"
                    for frame in sorted(self.frames, key=lambda item: item.round_number)
                ),
            )
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class HorizonSearchLimits:
    action_beam_width: int = 5
    max_chance_outcomes: int = DEFAULT_MAX_CHANCE_OUTCOMES
    max_expanded_nodes: int = 250_000
    advisory_chance_beam_width: int = 0
    allow_advisory_beam: bool = False
    max_chance_dp_states: int = DEFAULT_MAX_CHANCE_DP_STATES
    max_terminal_policy_states: int = DEFAULT_MAX_TERMINAL_POLICY_STATES

    def validate(self) -> None:
        if self.action_beam_width < 1:
            raise ValueError("action_beam_width must be positive")
        if self.max_chance_outcomes < 1:
            raise ValueError("max_chance_outcomes must be positive")
        if self.max_expanded_nodes < 1:
            raise ValueError("max_expanded_nodes must be positive")
        if self.max_chance_dp_states < 1:
            raise ValueError("max_chance_dp_states must be positive")
        if self.max_terminal_policy_states < 1:
            raise ValueError("max_terminal_policy_states must be positive")
        if self.advisory_chance_beam_width < 0:
            raise ValueError("advisory_chance_beam_width must be non-negative")
        if self.allow_advisory_beam and self.advisory_chance_beam_width < 1:
            raise ValueError(
                "advisory_chance_beam_width must be positive when advisory beam is enabled"
            )


@dataclass(frozen=True, slots=True)
class HorizonBlocker:
    code: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


def apply_audition_terminal_recovery(
    state: LogicExamState,
    boundary: AuditionBoundaryState,
    *,
    recovery_stamina: int | None = None,
    already_recovered: int = 0,
) -> LogicExamState:
    """Apply the native terminal recovery and mark the numerical state done.

    ``already_recovered`` accounts for the fixed recovery emitted by the
    existing END_TURN skip kernel.  Card-ending boundaries normally pass zero;
    the archived manual END_TURN path therefore becomes exactly
    ``stamina + recovery_units * recovery_stamina`` before clamping.
    """

    if not isinstance(state, LogicExamState):
        raise TypeError("state must be LogicExamState")
    if not isinstance(boundary, AuditionBoundaryState):
        raise TypeError("boundary must be AuditionBoundaryState")
    if not boundary.terminal:
        return state
    if state.max_stamina <= 0:
        raise ValueError("terminal recovery requires max_stamina")
    rate = (
        state.turn_end_stamina_recovery
        if recovery_stamina is None
        else recovery_stamina
    )
    if (
        not isinstance(rate, int)
        or isinstance(rate, bool)
        or rate < 0
    ):
        raise ValueError("recovery_stamina must be a non-negative integer")
    if (
        not isinstance(already_recovered, int)
        or isinstance(already_recovered, bool)
        or already_recovered < 0
    ):
        raise ValueError("already_recovered must be a non-negative integer")
    target_recovery = boundary.recovery_units * rate
    additional_recovery = max(0, target_recovery - already_recovered)
    completed = replace(
        state,
        turns_remaining=0,
        stamina=min(
            state.max_stamina,
            state.stamina + additional_recovery,
        ),
    )
    completed.validate(allow_completed=True)
    return completed


@dataclass(frozen=True, slots=True)
class HorizonExpectedValue:
    completion_probability: Fraction
    expected_score: Fraction
    expected_stamina: Fraction

    @classmethod
    def terminal(
        cls, state: LogicExamState, *, force_end_score: int
    ) -> "HorizonExpectedValue":
        complete = force_end_score <= 0 or state.score >= force_end_score
        return cls(
            completion_probability=Fraction(int(complete), 1),
            expected_score=Fraction(state.score, 1),
            expected_stamina=Fraction(state.stamina, 1),
        )

    @classmethod
    def terminal_boundary(
        cls,
        state: LogicExamState,
        boundary: AuditionBoundaryState,
        *,
        force_end_score: int,
        recovery_stamina: int | None = None,
        already_recovered: int = 0,
    ) -> "HorizonExpectedValue":
        if not boundary.terminal:
            raise ValueError("terminal_boundary requires EXAM_END")
        completed = apply_audition_terminal_recovery(
            state,
            boundary,
            recovery_stamina=recovery_stamina,
            already_recovered=already_recovered,
        )
        return cls.terminal(completed, force_end_score=force_end_score)

    def weighted(self, probability: Fraction) -> "HorizonExpectedValue":
        return HorizonExpectedValue(
            self.completion_probability * probability,
            self.expected_score * probability,
            self.expected_stamina * probability,
        )

    def __add__(self, other: "HorizonExpectedValue") -> "HorizonExpectedValue":
        return HorizonExpectedValue(
            self.completion_probability + other.completion_probability,
            self.expected_score + other.expected_score,
            self.expected_stamina + other.expected_stamina,
        )

    @property
    def sort_key(self) -> tuple[Fraction, Fraction, Fraction]:
        return (
            self.completion_probability,
            self.expected_score,
            self.expected_stamina,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "completion_probability": float(self.completion_probability),
            "expected_score": float(self.expected_score),
            "expected_stamina": float(self.expected_stamina),
            "exact": {
                "completion_probability": str(self.completion_probability),
                "expected_score": str(self.expected_score),
                "expected_stamina": str(self.expected_stamina),
            },
        }


ZERO_EXPECTED_VALUE = HorizonExpectedValue(Fraction(0), Fraction(0), Fraction(0))


@dataclass(frozen=True, slots=True)
class HorizonActionEvaluation:
    action_id: str
    hand_index: int | None
    card: MasterCard | None
    value: HorizonExpectedValue | None
    immediate_transition: LogicTransition | None
    blockers: tuple[HorizonBlocker, ...] = ()

    @property
    def fully_supported(self) -> bool:
        return self.value is not None and not self.blockers

    def to_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "hand_index": self.hand_index,
            "card_id": self.card.id if self.card else None,
            "upgrade": self.card.upgrade if self.card else None,
            "name": self.card.name if self.card else "END_TURN",
            "fully_supported": self.fully_supported,
            "value": self.value.to_dict() if self.value else None,
            "immediate_score_gain": (
                self.immediate_transition.total_score_gain
                if self.immediate_transition
                else 0
            ),
            "blockers": [blocker.to_dict() for blocker in self.blockers],
        }


@dataclass(frozen=True, slots=True)
class HorizonDiagnostics:
    schedule_digest: str
    nodes_expanded: int
    memo_entries: int
    chance_outcomes_evaluated: int
    maximum_chance_fanout: int
    full_terminal_horizon: bool
    chance_dp_states_evaluated: int = 0
    maximum_chance_dp_states: int = 0
    draw_outcome_cache_hits: int = 0
    chance_value_cache_hits: int = 0
    terminal_policy_groups_evaluated: int = 0
    terminal_policy_outcomes_merged: int = 0
    terminal_policy_value_cache_hits: int = 0
    approximation_used: bool = False
    action_branches_pruned: int = 0
    chance_outcomes_pruned: int = 0
    draw_semantics: str = (
        "unknown-order exact Deck draw (canonical chance-state DP when HandAdd "
        "support observes sequence); on shortage draw Deck remainder, move "
        "Grave to Deck, shuffle, draw remainder"
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "schedule_digest": self.schedule_digest,
            "nodes_expanded": self.nodes_expanded,
            "memo_entries": self.memo_entries,
            "chance_outcomes_evaluated": self.chance_outcomes_evaluated,
            "maximum_chance_fanout": self.maximum_chance_fanout,
            "chance_dp_states_evaluated": self.chance_dp_states_evaluated,
            "maximum_chance_dp_states": self.maximum_chance_dp_states,
            "draw_outcome_cache_hits": self.draw_outcome_cache_hits,
            "chance_value_cache_hits": self.chance_value_cache_hits,
            "terminal_policy_groups_evaluated": (
                self.terminal_policy_groups_evaluated
            ),
            "terminal_policy_outcomes_merged": (
                self.terminal_policy_outcomes_merged
            ),
            "terminal_policy_value_cache_hits": (
                self.terminal_policy_value_cache_hits
            ),
            "full_terminal_horizon": self.full_terminal_horizon,
            "approximation_used": self.approximation_used,
            "action_branches_pruned": self.action_branches_pruned,
            "chance_outcomes_pruned": self.chance_outcomes_pruned,
            "draw_semantics": self.draw_semantics,
        }


@dataclass(frozen=True, slots=True)
class HorizonDecision:
    decision_ready: bool
    recommendations: tuple[HorizonActionEvaluation, ...]
    blockers: tuple[HorizonBlocker, ...]
    diagnostics: HorizonDiagnostics

    @property
    def best(self) -> HorizonActionEvaluation | None:
        return self.recommendations[0] if self.decision_ready else None

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_ready": self.decision_ready,
            "best_action_id": self.best.action_id if self.best else None,
            "recommendations": [item.to_dict() for item in self.recommendations],
            "blockers": [blocker.to_dict() for blocker in self.blockers],
            "diagnostics": self.diagnostics.to_dict(),
        }


def card_refs_from_snapshot(snapshot: DeckSnapshot) -> tuple[HorizonCardRef, ...]:
    snapshot.validate()
    return _ordered_cards(
        HorizonCardRef(stack.card_id, stack.upgrade)
        for stack in snapshot.cards
        for _ in range(stack.count)
    )


def build_entry_zones(
    snapshot: DeckSnapshot,
    visible_hand: Iterable[HorizonCardRef],
    *,
    confidence_threshold: float = 0.80,
) -> AuditionCardZones:
    """Condition the initially shuffled deck on one observed opening hand."""

    snapshot.validate()
    if not snapshot.is_authoritative(confidence_threshold=confidence_threshold):
        raise ValueError("deck snapshot is not authoritative")
    # Preserve the detector's left-to-right slot order.  It is used only for
    # root action mapping; the hidden Deck remains an unordered multiset.
    hand = _visible_cards(visible_hand)
    remaining = list(card_refs_from_snapshot(snapshot))
    for card in hand:
        try:
            remaining.remove(card)
        except ValueError as exc:
            raise ValueError(
                f"visible hand card is absent from deck snapshot: {card.key}"
            ) from exc
    return AuditionCardZones(hand=hand, draw_pile=tuple(remaining))


def load_horizon_card_catalog(
    cards: Iterable[HorizonCardRef],
) -> Mapping[HorizonCardRef, MasterCard]:
    """Load every distinct deck reference through the strict Master loader."""

    catalog: dict[HorizonCardRef, MasterCard] = {}
    for ref in _ordered_cards(cards):
        if ref in catalog:
            continue
        card = load_master_card(ref.card_id, ref.upgrade)
        if card.id != ref.card_id or card.upgrade != ref.upgrade:
            raise ValueError(f"Master card identity mismatch: {ref.key}")
        catalog[ref] = card
    return catalog


def support_aware_catalog_refs(
    zones: AuditionCardZones,
    supports: Mapping[str, SupportUpgradeRuntimeInput]
    | Iterable[SupportUpgradeRuntimeInput],
) -> tuple[HorizonCardRef, ...]:
    """Return all base/current/future support-upgrade Master references.

    Card-search predicates are checked during exact transition evaluation.
    Loading every level through ``+++`` is a bounded superset (at most four
    refs per distinct card ID) and avoids discovering a missing Master row
    only after part of the expectimax tree has been expanded.
    """

    values = tuple(supports.values()) if isinstance(supports, Mapping) else tuple(supports)
    refs = set(zones.catalog_cards)
    if values:
        for base_ref in zones.all_cards:
            refs.update(
                HorizonCardRef(base_ref.card_id, upgrade)
                for upgrade in range(base_ref.upgrade, MAX_SUPPORT_UPGRADE + 1)
            )
    return _ordered_cards(refs)


@dataclass(frozen=True, slots=True)
class DrawOutcome:
    drawn: tuple[HorizonCardRef, ...]
    remaining: tuple[HorizonCardRef, ...]
    probability: Fraction


@dataclass(frozen=True, slots=True)
class OrderedDrawOutcome:
    """One exact ordered prefix from an otherwise unknown shuffled multiset."""

    drawn: tuple[HorizonCardRef, ...]
    remaining: tuple[HorizonCardRef, ...]
    probability: Fraction


def enumerate_unknown_draws(
    pool: Iterable[HorizonCardRef], count: int
) -> tuple[DrawOutcome, ...]:
    """Enumerate unordered draws with exact multiset probabilities."""

    if count < 0:
        raise ValueError("draw count must be non-negative")
    cards = _ordered_cards(pool)
    count = min(count, len(cards))
    if count == 0:
        return (DrawOutcome((), cards, Fraction(1)),)
    if count == len(cards):
        return (DrawOutcome(cards, (), Fraction(1)),)

    counts = Counter(cards)
    keys = tuple(sorted(counts))
    denominator = math.comb(len(cards), count)
    selected: list[int] = [0] * len(keys)
    results: list[DrawOutcome] = []

    def visit(index: int, remaining_count: int) -> None:
        if index == len(keys):
            if remaining_count != 0:
                return
            drawn: list[HorizonCardRef] = []
            remaining_cards: list[HorizonCardRef] = []
            numerator = 1
            for key, chosen in zip(keys, selected, strict=True):
                drawn.extend([key] * chosen)
                remaining_cards.extend([key] * (counts[key] - chosen))
                numerator *= math.comb(counts[key], chosen)
            results.append(
                DrawOutcome(
                    drawn=_ordered_cards(drawn),
                    remaining=_ordered_cards(remaining_cards),
                    probability=Fraction(numerator, denominator),
                )
            )
            return
        available = counts[keys[index]]
        minimum = max(0, remaining_count - sum(counts[key] for key in keys[index + 1 :]))
        maximum = min(available, remaining_count)
        for chosen in range(minimum, maximum + 1):
            selected[index] = chosen
            visit(index + 1, remaining_count - chosen)
        selected[index] = 0

    visit(0, count)
    results.sort(key=lambda result: result.drawn)
    if sum((result.probability for result in results), Fraction(0)) != 1:
        raise AssertionError("draw outcome probabilities do not sum to one")
    return tuple(results)


def enumerate_unknown_ordered_draws(
    pool: Iterable[HorizonCardRef], count: int
) -> tuple[OrderedDrawOutcome, ...]:
    """Enumerate native draw order exactly, without inventing a canonical order."""

    if count < 0:
        raise ValueError("draw count must be non-negative")
    cards = _ordered_cards(pool)
    count = min(count, len(cards))
    if count == 0:
        return (OrderedDrawOutcome((), cards, Fraction(1)),)
    counts = Counter(cards)
    results: list[OrderedDrawOutcome] = []

    def visit(
        remaining_counts: Counter[HorizonCardRef],
        remaining_total: int,
        drawn: tuple[HorizonCardRef, ...],
        probability: Fraction,
    ) -> None:
        if len(drawn) == count:
            remaining = _ordered_cards(
                card
                for card, card_count in remaining_counts.items()
                for _ in range(card_count)
            )
            results.append(OrderedDrawOutcome(drawn, remaining, probability))
            return
        for card in sorted(remaining_counts):
            available = remaining_counts[card]
            if available <= 0:
                continue
            updated = remaining_counts.copy()
            updated[card] -= 1
            if updated[card] == 0:
                del updated[card]
            visit(
                updated,
                remaining_total - 1,
                (*drawn, card),
                probability * Fraction(available, remaining_total),
            )

    visit(counts, len(cards), (), Fraction(1))
    results.sort(key=lambda result: result.drawn)
    if sum((result.probability for result in results), Fraction(0)) != 1:
        raise AssertionError("ordered draw outcome probabilities do not sum to one")
    return tuple(results)


@dataclass(frozen=True, slots=True)
class _Node:
    state: LogicExamState
    zones: AuditionCardZones


@dataclass(frozen=True, slots=True)
class _PreparedCardTransition:
    """Zone-independent result shared by ranking and exact evaluation."""

    item_result: ResolvedItemEffects
    transition: LogicTransition


class _Blocked(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.blocker = HorizonBlocker(code, detail)


def _blocker_summary(
    blockers: Iterable[HorizonBlocker], *, limit: int = 8, max_length: int = 1200
) -> str:
    unique = tuple(dict.fromkeys(blockers))
    parts = [
        f"{blocker.code}:{blocker.detail}" if blocker.detail else blocker.code
        for blocker in unique[:limit]
    ]
    if len(unique) > limit:
        parts.append(f"...+{len(unique) - limit}-more")
    result = ";".join(parts)
    if len(result) > max_length:
        result = result[: max_length - 3] + "..."
    return result


@dataclass(slots=True)
class _SearchContext:
    rules: AuditionRules
    schedule: VerifiedTurnSchedule
    catalog: Mapping[HorizonCardRef, MasterCard]
    item: EquippedItemRule
    limits: HorizonSearchLimits
    support_upgrades: tuple[SupportUpgradeRuntimeInput, ...] = ()
    support_card_searches: Mapping[str, ProduceCardSearchRule] = field(
        default_factory=dict
    )
    boundary_model: AuditionBoundaryModel | None = None
    memo: dict[_Node, HorizonExpectedValue] = field(default_factory=dict)
    nodes_expanded: int = 0
    chance_outcomes_evaluated: int = 0
    maximum_chance_fanout: int = 0
    chance_dp_states_evaluated: int = 0
    maximum_chance_dp_states: int = 0
    draw_outcome_cache_hits: int = 0
    chance_value_cache_hits: int = 0
    terminal_policy_groups_evaluated: int = 0
    terminal_policy_outcomes_merged: int = 0
    terminal_policy_value_cache_hits: int = 0
    action_branches_pruned: int = 0
    chance_outcomes_pruned: int = 0
    fatal_blocker: HorizonBlocker | None = None
    turn_frames: dict[int, VerifiedTurnFrame] = field(init=False)
    card_transition_cache: dict[
        tuple[LogicExamState, HorizonCardRef], _PreparedCardTransition
    ] = field(default_factory=dict)
    skip_transition_cache: dict[
        LogicExamState, _PreparedCardTransition
    ] = field(default_factory=dict)
    draw_outcome_cache: dict[
        tuple[tuple[HorizonCardRef, ...], int], tuple[DrawOutcome, ...]
    ] = field(default_factory=dict)
    zone_draw_outcome_cache: dict[
        tuple[AuditionCardZones, int, str | None],
        tuple[tuple[AuditionCardZones, Fraction], ...],
    ] = field(default_factory=dict)
    chance_value_cache: dict[
        tuple[LogicExamState, AuditionCardZones, int, str],
        HorizonExpectedValue,
    ] = field(default_factory=dict)
    terminal_policy_proof_cache: dict[
        tuple[LogicExamState, tuple[HorizonHandEntry, ...]], bool
    ] = field(default_factory=dict)
    terminal_policy_value_cache: dict[
        tuple[LogicExamState, tuple[HorizonHandEntry, ...]],
        HorizonExpectedValue,
    ] = field(default_factory=dict)
    def __post_init__(self) -> None:
        # ``VerifiedTurnSchedule.by_round`` validates and materializes a new
        # mapping.  Repeating that in every card preview dominated hundreds of
        # thousands of otherwise identical lookups in a nine-turn search.
        self.turn_frames = dict(self.schedule.by_round)
        self.support_upgrades = tuple(
            sorted(self.support_upgrades, key=lambda value: value.loadout_order)
        )

    def expand_node(self) -> None:
        if self.fatal_blocker is not None:
            raise _Blocked(
                self.fatal_blocker.code,
                self.fatal_blocker.detail,
            )
        if self.nodes_expanded >= self.limits.max_expanded_nodes:
            self.fatal_blocker = HorizonBlocker(
                "search-node-limit-exceeded",
                f"{self.nodes_expanded}>={self.limits.max_expanded_nodes}",
            )
            raise _Blocked(
                self.fatal_blocker.code,
                self.fatal_blocker.detail,
            )
        self.nodes_expanded += 1


def _prepare_card_transition(
    state: LogicExamState,
    ref: HorizonCardRef,
    context: _SearchContext,
) -> _PreparedCardTransition:
    """Return the exact numerical transition, independent of card zones.

    Candidate ordering and exact action evaluation previously recomputed this
    same item/card transition back-to-back.  The key contains every numerical
    state field plus the exact card reference; the item and verified schedule
    are immutable members of one search context.  Deck/Hand/Grave/Lost remain
    outside this cache and are still evolved independently by
    :func:`_finish_action`.
    """

    key = (state, ref)
    cached = context.card_transition_cache.get(key)
    if cached is not None:
        return cached
    frame = context.turn_frames.get(state.round_number)
    if frame is None:
        raise _Blocked("future-turn-frame-missing", f"round={state.round_number}")
    card = context.catalog[ref]
    item_result = resolve_item_end_turn(context.item, state, card)
    transition = apply_logic_card(
        state,
        card,
        post_card_effects=item_result.post_card_effects,
        end_turn_effects=item_result.end_turn_effects,
        status_change_item_enchantments=item_result.status_change_enchantments,
        fired_item_enchantment_ids=item_result.fired_enchantment_ids,
        lesson_type=frame.lesson_type,
    )
    result = _PreparedCardTransition(item_result, transition)
    context.card_transition_cache[key] = result
    return result


def _prepare_skip_transition(
    state: LogicExamState,
    context: _SearchContext,
) -> _PreparedCardTransition:
    """Return the zone-independent exact END_TURN transition."""

    cached = context.skip_transition_cache.get(state)
    if cached is not None:
        return cached
    frame = context.turn_frames.get(state.round_number)
    if frame is None:
        raise _Blocked("future-turn-frame-missing", f"round={state.round_number}")
    item_result = resolve_item_end_turn(
        context.item, state, _HORIZON_SKIP_ITEM_CARD
    )
    transition = apply_logic_turn_skip(
        state,
        end_turn_effects=item_result.end_turn_effects,
        lesson_type=frame.lesson_type,
    )
    result = _PreparedCardTransition(item_result, transition)
    context.skip_transition_cache[state] = result
    return result


def _schedule_blockers(
    state: LogicExamState,
    schedule: VerifiedTurnSchedule,
    boundary_model: AuditionBoundaryModel | None = None,
) -> tuple[HorizonBlocker, ...]:
    try:
        by_round = schedule.by_round
    except ValueError as exc:
        return (HorizonBlocker("turn-schedule-invalid", str(exc)),)
    blockers: list[HorizonBlocker] = []
    last_round = state.round_number + state.turns_remaining - 1
    for round_number in range(state.round_number, last_round + 1):
        if (
            boundary_model is not None
            and round_number >= boundary_model.limit_turn
            and boundary_model.extra_turn == 0
        ):
            # The native last-boundary path completes before this serialized
            # terminal ordinal becomes a card-selection frame.
            continue
        if round_number not in by_round:
            blockers.append(
                HorizonBlocker(
                    "future-turn-frame-missing",
                    f"round={round_number}",
                )
            )
    current = by_round.get(state.round_number)
    if (
        current is not None
        and current.score_multiplier_permille != state.score_multiplier_permille
    ):
        blockers.append(
            HorizonBlocker(
                "current-turn-multiplier-mismatch",
                f"observed={state.score_multiplier_permille};"
                f"schedule={current.score_multiplier_permille}",
            )
        )
    return tuple(blockers)


def _validate_inputs(
    state: LogicExamState,
    zones: AuditionCardZones,
    rules: AuditionRules,
    schedule: VerifiedTurnSchedule,
    catalog: Mapping[HorizonCardRef, MasterCard],
    item: EquippedItemRule,
    limits: HorizonSearchLimits,
    support_upgrades: Mapping[str, SupportUpgradeRuntimeInput]
    | Iterable[SupportUpgradeRuntimeInput] = (),
    support_card_searches: Mapping[str, ProduceCardSearchRule] = {},
    *,
    boundary_model: AuditionBoundaryModel | None = None,
) -> tuple[HorizonBlocker, ...]:
    blockers: list[HorizonBlocker] = []
    try:
        state.validate()
    except (TypeError, ValueError) as exc:
        blockers.append(HorizonBlocker("logic-state-invalid", str(exc)))
    try:
        limits.validate()
    except ValueError as exc:
        blockers.append(HorizonBlocker("search-limits-invalid", str(exc)))
    if boundary_model is not None and not isinstance(
        boundary_model, AuditionBoundaryModel
    ):
        blockers.append(HorizonBlocker("audition-boundary-model-invalid"))
    else:
        blockers.extend(_schedule_blockers(state, schedule, boundary_model))
    if state.turns_remaining > rules.turns:
        blockers.append(
            HorizonBlocker(
                "remaining-turns-exceed-master",
                f"remaining={state.turns_remaining};master={rules.turns}",
            )
        )
    if state.turn_end_stamina_recovery != rules.turn_end_stamina_recovery:
        blockers.append(
            HorizonBlocker(
                "turn-end-recovery-master-mismatch",
                f"state={state.turn_end_stamina_recovery};"
                f"master={rules.turn_end_stamina_recovery}",
            )
        )
    if rules.gimmick_group_id:
        # The one-step live reader can resolve the observed current TurnStart,
        # but this horizon does not yet carry a future gimmick interpreter.
        # Silently omitting later gimmick activations would overstate policy
        # value, so every such stage is closed at preflight.
        blockers.append(
            HorizonBlocker(
                "future-audition-gimmick-not-modeled",
                rules.gimmick_group_id,
            )
        )
    if len(zones.hand) > rules.hand_limit:
        blockers.append(
            HorizonBlocker(
                "visible-hand-exceeds-master-limit",
                f"hand={len(zones.hand)};limit={rules.hand_limit}",
            )
        )
    if not zones.hand:
        blockers.append(HorizonBlocker("visible-hand-empty"))
    if zones.hand_hold_count:
        blockers.append(
            HorizonBlocker(
                "retain-hand-semantics-unsupported",
                f"hand_hold_count={zones.hand_hold_count}",
            )
        )
    if item.unsupported_rules:
        blockers.extend(
            HorizonBlocker("equipped-item-rule-unsupported", rule)
            for rule in item.unsupported_rules
        )
    for ref in zones.catalog_cards:
        card = catalog.get(ref)
        if card is None:
            blockers.append(HorizonBlocker("deck-card-master-missing", ref.key))
            continue
        if card.id != ref.card_id or card.upgrade != ref.upgrade:
            blockers.append(HorizonBlocker("deck-card-master-mismatch", ref.key))
        if card.plan_type not in {PLAN_COMMON, PLAN_LOGIC}:
            blockers.append(
                HorizonBlocker("deck-card-plan-unsupported", f"{ref.key}:{card.plan_type}")
            )
        if card.move_position_type not in {MOVE_GRAVE, MOVE_LOST}:
            blockers.append(
                HorizonBlocker(
                    "deck-card-move-position-unsupported",
                    f"{ref.key}:{card.move_position_type}",
                )
            )
        if card.is_end_turn_lost:
            blockers.append(
                HorizonBlocker("deck-card-end-turn-lost-unsupported", ref.key)
            )
        if (
            card.move_effect_trigger_type
            not in {"", "ProduceCardMoveEffectTriggerType_Unknown"}
            or card.move_effect_ids
            or card.move_trigger_ids
        ):
            blockers.append(
                HorizonBlocker("deck-card-move-effect-unsupported", ref.key)
            )
        if card.produce_card_status_enchant_id:
            blockers.append(
                HorizonBlocker(
                    "deck-card-native-status-enchant-unsupported", ref.key
                )
            )
        if any(effect.once for effect in card.effects):
            # The legacy multiset node intentionally has no per-GUID
            # ExamCardData.PlayCount.  Replaying a once-only card through a
            # reshuffle would otherwise fire its effect again incorrectly.
            blockers.append(
                HorizonBlocker("deck-card-once-effect-unsupported", ref.key)
            )
    if isinstance(support_upgrades, Mapping):
        supports = tuple(support_upgrades.values())
        for support_id, support in support_upgrades.items():
            if (
                not isinstance(support, SupportUpgradeRuntimeInput)
                or support_id != support.support_id
            ):
                blockers.append(
                    HorizonBlocker(
                        "support-runtime-key-mismatch", str(support_id)
                    )
                )
    else:
        supports = tuple(support_upgrades)
    support_ids: set[str] = set()
    loadout_orders: set[int] = set()
    for support in supports:
        if not isinstance(support, SupportUpgradeRuntimeInput):
            blockers.append(HorizonBlocker("support-runtime-input-invalid"))
            continue
        if support.support_id in support_ids:
            blockers.append(
                HorizonBlocker("support-runtime-id-duplicate", support.support_id)
            )
        support_ids.add(support.support_id)
        if support.loadout_order in loadout_orders:
            blockers.append(
                HorizonBlocker(
                    "support-runtime-loadout-order-duplicate",
                    str(support.loadout_order),
                )
            )
        loadout_orders.add(support.loadout_order)
        search = support_card_searches.get(support.card_search_id)
        if search is None:
            blockers.append(
                HorizonBlocker(
                    "support-card-search-missing", support.card_search_id
                )
            )
    for support_id in zones.used_support_ids:
        if support_id not in support_ids:
            blockers.append(HorizonBlocker("support-used-id-unknown", support_id))
    for entry in zones.hand_entries:
        if entry.effective_ref not in catalog:
            blockers.append(
                HorizonBlocker(
                    "effective-hand-card-master-missing", entry.effective_ref.key
                )
            )
    if (
        state.runtime_status_enchants
        and state.last_resolved_runtime_status_round != state.round_number
    ):
        blockers.append(
            HorizonBlocker(
                "current-turn-runtime-status-not-resolved-before-visible-hand",
                f"round={state.round_number}",
            )
        )
    return tuple(dict.fromkeys(blockers))


def audit_audition_horizon_inputs(
    state: LogicExamState,
    zones: AuditionCardZones,
    rules: AuditionRules,
    schedule: VerifiedTurnSchedule,
    catalog: Mapping[HorizonCardRef, MasterCard],
    item: EquippedItemRule,
    *,
    limits: HorizonSearchLimits = HorizonSearchLimits(),
    support_upgrades: Mapping[str, SupportUpgradeRuntimeInput]
    | Iterable[SupportUpgradeRuntimeInput] = (),
    support_card_searches: Mapping[str, ProduceCardSearchRule] = {},
    boundary_model: AuditionBoundaryModel | None = None,
) -> tuple[HorizonBlocker, ...]:
    """Run the non-search preflight used by :func:`recommend_audition_horizon`."""

    return _validate_inputs(
        state,
        zones,
        rules,
        schedule,
        catalog,
        item,
        limits,
        support_upgrades,
        support_card_searches,
        boundary_model=boundary_model,
    )


def _direct_draw_count(
    card: MasterCard,
    transition: LogicTransition,
    item_effects: Iterable[MasterCardEffect],
) -> int:
    count = transition.runtime_status_draw_count
    for effect in (*card.effects, *tuple(item_effects)):
        if effect.effect_type == EFFECT_CARD_MOVE:
            raise _Blocked("random-card-move-zone-not-modeled", effect.id)
        if effect.effect_type == EFFECT_CARD_CREATE_ID:
            # Numeric shadow records created cards but does not expose their
            # ordered interleaving with Draw.  Creation-only cards are handled
            # below; a mixed create/draw card is blocked here.
            continue
        if effect.effect_type != EFFECT_CARD_DRAW:
            continue
        if effect.trigger_id:
            raise _Blocked("conditional-card-draw-not-proven", effect.id)
        if effect.value1 < 0:
            raise _Blocked("negative-card-draw", effect.id)
        count += effect.value1
    has_create = any(
        effect.effect_type == EFFECT_CARD_CREATE_ID for effect in card.effects
    )
    if has_create and count:
        raise _Blocked("create-draw-effect-order-not-proven", card.id)
    return count


def _apply_created_cards(
    before: LogicExamState,
    after: LogicExamState,
    zones: AuditionCardZones,
    catalog: Mapping[HorizonCardRef, MasterCard],
) -> AuditionCardZones:
    if after.created_cards[: len(before.created_cards)] != before.created_cards:
        raise _Blocked("created-card-history-not-append-only")
    created = after.created_cards[len(before.created_cards) :]
    if not created:
        # The overwhelmingly common path has no CardCreate effect.  Returning
        # the already canonical immutable zone object avoids rebuilding and
        # re-sorting all four zones for a semantic no-op.
        return zones
    draw = list(zones.draw_pile)
    for card_id, upgrade, position, count in created:
        ref = HorizonCardRef(card_id, upgrade)
        if position != "deck_random":
            raise _Blocked(
                "created-card-position-not-modeled",
                f"{ref.key}:{position}",
            )
        if ref not in catalog:
            raise _Blocked("created-card-master-missing", ref.key)
        draw.extend([ref] * count)
    return replace(zones, draw_pile=tuple(draw))


def _support_upgrade_outcomes(
    zones: AuditionCardZones,
    drawn: tuple[HorizonCardRef, ...],
    lesson_type: str,
    context: _SearchContext,
) -> tuple[tuple[AuditionCardZones, Fraction], ...]:
    """Apply native HandAdd support iteration and independent RNG exactly."""

    states: list[
        tuple[tuple[HorizonHandEntry, ...], frozenset[str], Fraction]
    ] = [(zones.hand_entries, frozenset(zones.used_support_ids), Fraction(1))]
    for base_ref in drawn:
        next_states: list[
            tuple[tuple[HorizonHandEntry, ...], frozenset[str], Fraction]
        ] = []
        for entries, used, outer_probability in states:
            # Native skips SetDrawCardSupportUpgrade entirely when the card is
            # already not upgradable on HandAdd.  This differs from multiple
            # supports collected for one base@2 card: those rolls are resolved
            # together, then a later SetSupportUpgrade can fail after the first
            # success reaches @3 while the later support remains consumed.
            if base_ref.upgrade >= MAX_SUPPORT_UPGRADE:
                next_states.append(
                    (
                        (*entries, HorizonHandEntry(base_ref, base_ref, 0)),
                        used,
                        outer_probability,
                    )
                )
                continue
            eligible: list[SupportUpgradeRuntimeInput] = []
            for support in context.support_upgrades:
                if (
                    not support_lesson_type_matches(
                        support.lesson_type, lesson_type
                    )
                    or support.support_id in used
                ):
                    continue
                search = context.support_card_searches.get(support.card_search_id)
                if search is None:
                    raise _Blocked(
                        "support-card-search-missing", support.card_search_id
                    )
                matches, unsupported = match_support_hand_add_search(
                    search, base_ref.card_id, base_ref.upgrade
                )
                if unsupported is not None:
                    raise _Blocked("support-card-search-unsupported", unsupported)
                if matches:
                    eligible.append(support)

            rolls: list[tuple[frozenset[str], int, Fraction]] = [
                (used, 0, Fraction(1))
            ]
            for support in eligible:
                rolled: list[tuple[frozenset[str], int, Fraction]] = []
                success_probability = Fraction(support.runtime_permil, 1000)
                failure_probability = 1 - success_probability
                for rolled_used, successes, probability in rolls:
                    if failure_probability:
                        rolled.append(
                            (rolled_used, successes, probability * failure_probability)
                        )
                    if success_probability:
                        rolled.append(
                            (
                                rolled_used | {support.support_id},
                                successes + 1,
                                probability * success_probability,
                            )
                        )
                rolls = rolled
            for rolled_used, successes, probability in rolls:
                # Native collects successful support IDs before invoking
                # SetSupportUpgrade.  Once level 3 is reached, later calls fail
                # on this card; their supports nevertheless remain used.
                applied = min(successes, MAX_SUPPORT_UPGRADE - base_ref.upgrade)
                effective = HorizonCardRef(
                    base_ref.card_id, base_ref.upgrade + applied
                )
                next_states.append(
                    (
                        (*entries, HorizonHandEntry(base_ref, effective, applied)),
                        rolled_used,
                        outer_probability * probability,
                    )
                )
        states = next_states

    combined: dict[AuditionCardZones, Fraction] = {}
    for entries, used, probability in states:
        after = replace(
            zones,
            hand=tuple(entry.effective_ref for entry in entries),
            hand_base=tuple(entry.base_ref for entry in entries),
            hand_support_deltas=tuple(entry.support_delta for entry in entries),
            used_support_ids=tuple(sorted(used)),
        )
        combined[after] = combined.get(after, Fraction(0)) + probability
    return tuple(sorted(combined.items(), key=lambda item: repr(item[0])))


@dataclass(frozen=True, slots=True)
class _ChanceDrawState:
    """One canonical state in an order-observable support draw.

    ``remaining_counts`` is indexed by ``pool_cards``.  Keeping that pool
    identity in the key prevents a Deck layer and a post-reshuffle Grave layer
    with coincidentally equal count vectors from colliding in distinct-state
    accounting.  ``added_hand_entries`` contains only cards added by this draw
    operation and is a multiset encoded as a sorted tuple.  The visible Hand
    that entered the operation is deliberately kept outside this key so its
    observed UI slot order is retained at the root.
    """

    pool_cards: tuple[HorizonCardRef, ...]
    remaining_counts: tuple[int, ...]
    draws_left: int
    used_support_mask: int
    added_hand_entries: tuple[HorizonHandEntry, ...]


def _support_bit_positions(context: _SearchContext) -> dict[str, int]:
    positions: dict[str, int] = {}
    for index, support in enumerate(context.support_upgrades):
        if support.support_id in positions:
            raise _Blocked("support-runtime-id-duplicate", support.support_id)
        positions[support.support_id] = index
    return positions


def _used_support_mask(
    used_support_ids: Iterable[str], positions: Mapping[str, int]
) -> int:
    mask = 0
    for support_id in used_support_ids:
        index = positions.get(support_id)
        if index is None:
            raise _Blocked("support-used-id-unknown", support_id)
        mask |= 1 << index
    return mask


def _support_ids_from_mask(
    mask: int, context: _SearchContext
) -> tuple[str, ...]:
    return tuple(
        support.support_id
        for index, support in enumerate(context.support_upgrades)
        if mask & (1 << index)
    )


def _support_entry_outcomes(
    base_ref: HorizonCardRef,
    used_support_mask: int,
    lesson_type: str,
    context: _SearchContext,
) -> tuple[tuple[HorizonHandEntry, int, Fraction], ...]:
    """Return one-card HandAdd results without retaining RNG history."""

    if base_ref.upgrade >= MAX_SUPPORT_UPGRADE:
        return (
            (
                HorizonHandEntry(base_ref, base_ref, 0),
                used_support_mask,
                Fraction(1),
            ),
        )

    eligible: list[tuple[int, SupportUpgradeRuntimeInput]] = []
    for index, support in enumerate(context.support_upgrades):
        support_bit = 1 << index
        if (
            not support_lesson_type_matches(support.lesson_type, lesson_type)
            or used_support_mask & support_bit
        ):
            continue
        search = context.support_card_searches.get(support.card_search_id)
        if search is None:
            raise _Blocked("support-card-search-missing", support.card_search_id)
        matches, unsupported = match_support_hand_add_search(
            search, base_ref.card_id, base_ref.upgrade
        )
        if unsupported is not None:
            raise _Blocked("support-card-search-unsupported", unsupported)
        if matches:
            eligible.append((support_bit, support))

    rolls: dict[tuple[int, int], Fraction] = {
        (used_support_mask, 0): Fraction(1)
    }
    for support_bit, support in eligible:
        success_probability = Fraction(support.runtime_permil, 1000)
        failure_probability = 1 - success_probability
        next_rolls: dict[tuple[int, int], Fraction] = {}
        for (mask, successes), probability in rolls.items():
            if failure_probability:
                key = (mask, successes)
                next_rolls[key] = (
                    next_rolls.get(key, Fraction(0))
                    + probability * failure_probability
                )
            if success_probability:
                key = (mask | support_bit, successes + 1)
                next_rolls[key] = (
                    next_rolls.get(key, Fraction(0))
                    + probability * success_probability
                )
        rolls = next_rolls

    outcomes: dict[tuple[HorizonHandEntry, int], Fraction] = {}
    for (mask, successes), probability in rolls.items():
        applied = min(successes, MAX_SUPPORT_UPGRADE - base_ref.upgrade)
        entry = HorizonHandEntry(
            base_ref,
            HorizonCardRef(base_ref.card_id, base_ref.upgrade + applied),
            applied,
        )
        key = (entry, mask)
        outcomes[key] = outcomes.get(key, Fraction(0)) + probability
    return tuple(
        (entry, mask, probability)
        for (entry, mask), probability in sorted(
            outcomes.items(), key=lambda item: item[0]
        )
    )


def _record_chance_dp_layer(
    states: Mapping[_ChanceDrawState, Fraction],
    seen: set[_ChanceDrawState],
    context: _SearchContext,
) -> None:
    unseen = set(states).difference(seen)
    seen.update(unseen)
    context.chance_dp_states_evaluated += len(unseen)
    context.maximum_chance_dp_states = max(
        context.maximum_chance_dp_states, len(seen)
    )
    if len(seen) > context.limits.max_chance_dp_states:
        raise _Blocked(
            "chance-dp-state-limit-exceeded",
            f"states={len(seen)};limit={context.limits.max_chance_dp_states}",
        )


def _advance_chance_draw_pool(
    states: Mapping[_ChanceDrawState, Fraction],
    pool_cards: tuple[HorizonCardRef, ...],
    lesson_type: str,
    context: _SearchContext,
    seen: set[_ChanceDrawState],
) -> dict[_ChanceDrawState, Fraction]:
    """Draw one multiset card per layer and merge exact equivalent states."""

    current = dict(states)
    while current and next(iter(current)).draws_left:
        next_states: dict[_ChanceDrawState, Fraction] = {}
        for state, state_probability in current.items():
            if state.pool_cards != pool_cards:
                raise AssertionError("chance draw state pool identity mismatch")
            remaining_total = sum(state.remaining_counts)
            if state.draws_left < 1 or state.draws_left > remaining_total:
                raise AssertionError("invalid canonical chance draw state")
            for card_index, available in enumerate(state.remaining_counts):
                if available <= 0:
                    continue
                card_probability = Fraction(available, remaining_total)
                remaining = list(state.remaining_counts)
                remaining[card_index] -= 1
                for entry, used_mask, support_probability in (
                    _support_entry_outcomes(
                        pool_cards[card_index],
                        state.used_support_mask,
                        lesson_type,
                        context,
                    )
                ):
                    added = tuple(sorted((*state.added_hand_entries, entry)))
                    next_state = _ChanceDrawState(
                        state.pool_cards,
                        tuple(remaining),
                        state.draws_left - 1,
                        used_mask,
                        added,
                    )
                    next_states[next_state] = (
                        next_states.get(next_state, Fraction(0))
                        + state_probability
                        * card_probability
                        * support_probability
                    )
        _record_chance_dp_layer(next_states, seen, context)
        current = next_states
    return current


def _support_aware_draw_dp(
    zones: AuditionCardZones,
    count: int,
    lesson_type: str,
    context: _SearchContext,
) -> tuple[tuple[AuditionCardZones, Fraction], ...]:
    """Exact ordered-draw/support DP with a native Deck -> Grave boundary."""

    positions = _support_bit_positions(context)
    initial_mask = _used_support_mask(zones.used_support_ids, positions)
    deck_counts = Counter(zones.draw_pile)
    deck_cards = tuple(sorted(deck_counts))
    deck_draw_count = min(count, len(zones.draw_pile))
    initial = _ChanceDrawState(
        deck_cards,
        tuple(deck_counts[card] for card in deck_cards),
        deck_draw_count,
        initial_mask,
        (),
    )
    states: dict[_ChanceDrawState, Fraction] = {initial: Fraction(1)}
    seen: set[_ChanceDrawState] = set()
    _record_chance_dp_layer(states, seen, context)
    states = _advance_chance_draw_pool(
        states, deck_cards, lesson_type, context, seen
    )

    crossed_grave_boundary = count > len(zones.draw_pile)
    if crossed_grave_boundary:
        grave_counts = Counter(zones.discard_pile)
        grave_cards = tuple(sorted(grave_counts))
        grave_draw_count = count - len(zones.draw_pile)
        boundary: dict[_ChanceDrawState, Fraction] = {}
        grave_count_values = tuple(grave_counts[card] for card in grave_cards)
        for state, probability in states.items():
            next_state = _ChanceDrawState(
                grave_cards,
                grave_count_values,
                grave_draw_count,
                state.used_support_mask,
                state.added_hand_entries,
            )
            boundary[next_state] = boundary.get(next_state, Fraction(0)) + probability
        _record_chance_dp_layer(boundary, seen, context)
        states = _advance_chance_draw_pool(
            boundary, grave_cards, lesson_type, context, seen
        )

    root_entries = zones.hand_entries
    combined: dict[AuditionCardZones, Fraction] = {}
    for state, probability in states.items():
        if state.draws_left:
            raise AssertionError("canonical chance DP ended before all draws")
        remaining = tuple(
            card
            for card, available in zip(
                state.pool_cards, state.remaining_counts, strict=True
            )
            for _ in range(available)
        )
        entries = (*root_entries, *state.added_hand_entries)
        after = replace(
            zones,
            hand=tuple(entry.effective_ref for entry in entries),
            hand_base=tuple(entry.base_ref for entry in entries),
            hand_support_deltas=tuple(entry.support_delta for entry in entries),
            used_support_ids=_support_ids_from_mask(
                state.used_support_mask, context
            ),
            draw_pile=remaining,
            discard_pile=() if crossed_grave_boundary else zones.discard_pile,
        )
        combined[after] = combined.get(after, Fraction(0)) + probability
    if sum(combined.values(), Fraction(0)) != 1:
        raise AssertionError("canonical chance outcome probabilities do not sum to one")
    return tuple(sorted(combined.items(), key=lambda item: repr(item[0])))


def _draw_from_zones(
    zones: AuditionCardZones,
    count: int,
    context: _SearchContext,
    *,
    lesson_type: str | None = None,
) -> tuple[tuple[AuditionCardZones, Fraction], ...]:
    """Draw with the native Deck remainder -> Grave shuffle boundary."""

    if count < 0:
        raise _Blocked("negative-resolved-draw-count", str(count))
    count = min(count, len(zones.draw_pile) + len(zones.discard_pile))
    if count == 0:
        return ((zones, Fraction(1)),)

    if lesson_type is None and any(
        support.runtime_permil > 0
        and support.support_id not in zones.used_support_ids
        for support in context.support_upgrades
    ):
        raise _Blocked("support-draw-lesson-type-missing")
    support_order_observable = bool(
        lesson_type is not None
        and any(
            support_lesson_type_matches(support.lesson_type, lesson_type)
            and support.runtime_permil > 0
            and support.support_id not in zones.used_support_ids
            for support in context.support_upgrades
        )
    )
    zone_draw_key = (zones, count, lesson_type)
    cached_zone_outcomes = context.zone_draw_outcome_cache.get(zone_draw_key)
    if cached_zone_outcomes is not None:
        context.draw_outcome_cache_hits += 1
        context.chance_outcomes_evaluated += len(cached_zone_outcomes)
        return cached_zone_outcomes
    if support_order_observable:
        assert lesson_type is not None
        outcomes = _support_aware_draw_dp(
            zones, count, lesson_type, context
        )
    elif count <= len(zones.draw_pile):
        draw_key = (zones.draw_pile, count)
        first_draw = context.draw_outcome_cache.get(draw_key)
        if first_draw is None:
            first_draw = enumerate_unknown_draws(zones.draw_pile, count)
            context.draw_outcome_cache[draw_key] = first_draw
        outcomes = tuple(
            (
                replace(
                    zones,
                    hand=(*zones.hand, *outcome.drawn),
                    hand_base=(*zones.hand_base, *outcome.drawn),
                    hand_support_deltas=(
                        *zones.hand_support_deltas,
                        *(0 for _ in outcome.drawn),
                    ),
                    draw_pile=outcome.remaining,
                ),
                outcome.probability,
            )
            for outcome in first_draw
        )
    else:
        drawn_prefix = zones.draw_pile
        remaining_count = count - len(drawn_prefix)
        draw_key = (zones.discard_pile, remaining_count)
        second_draw = context.draw_outcome_cache.get(draw_key)
        if second_draw is None:
            second_draw = enumerate_unknown_draws(
                zones.discard_pile, remaining_count
            )
            context.draw_outcome_cache[draw_key] = second_draw
        outcomes = tuple(
            (
                replace(
                    zones,
                    hand=(*zones.hand, *drawn_prefix, *outcome.drawn),
                    hand_base=(
                        *zones.hand_base,
                        *drawn_prefix,
                        *outcome.drawn,
                    ),
                    hand_support_deltas=(
                        *zones.hand_support_deltas,
                        *(0 for _ in drawn_prefix),
                        *(0 for _ in outcome.drawn),
                    ),
                    draw_pile=outcome.remaining,
                    discard_pile=(),
                ),
                outcome.probability,
            )
            for outcome in second_draw
        )

    fanout = len(outcomes)
    context.maximum_chance_fanout = max(context.maximum_chance_fanout, fanout)
    if fanout > context.limits.max_chance_outcomes:
        raise _Blocked(
            "chance-outcome-limit-exceeded",
            f"fanout={fanout};limit={context.limits.max_chance_outcomes}",
        )
    if (
        context.limits.allow_advisory_beam
        and fanout > context.limits.advisory_chance_beam_width
    ):
        width = context.limits.advisory_chance_beam_width
        # Deterministic weighted-quantile stratification.  This retains broad
        # support across a non-uniform multiset distribution and gives every
        # stratum equal mass.  It is explicitly advisory and never marked
        # decision-ready.
        cumulative: list[Fraction] = []
        running = Fraction(0)
        for _zone, probability in outcomes:
            running += probability
            cumulative.append(running)
        selected_counts: Counter[int] = Counter()
        cursor = 0
        for stratum in range(width):
            target = Fraction(2 * stratum + 1, 2 * width)
            while cursor < fanout - 1 and cumulative[cursor] < target:
                cursor += 1
            selected_counts[cursor] += 1
        outcomes = tuple(
            (outcomes[index][0], Fraction(count, width))
            for index, count in sorted(selected_counts.items())
        )
        context.chance_outcomes_pruned += fanout - len(outcomes)
    context.zone_draw_outcome_cache[zone_draw_key] = outcomes
    context.chance_outcomes_evaluated += len(outcomes)
    return outcomes


def _prepare_next_turn(
    state: LogicExamState, context: _SearchContext
) -> tuple[LogicExamState, int]:
    frame = context.turn_frames.get(state.round_number)
    if frame is None:
        raise _Blocked("future-turn-frame-missing", f"round={state.round_number}")
    current = replace(
        state, score_multiplier_permille=frame.score_multiplier_permille
    )
    runtime = resolve_logic_status_turn_start(current)
    if runtime.unsupported_rules:
        raise _Blocked(
            "turn-start-runtime-status-unsupported",
            ",".join(runtime.unsupported_rules),
        )
    item_start = resolve_item_turn_start(
        context.item,
        runtime.after,
        lesson_type=frame.lesson_type,
    )
    if item_start.unsupported_rules:
        raise _Blocked(
            "turn-start-item-unsupported",
            ",".join(item_start.unsupported_rules),
        )
    # A run carrying two simultaneously firing start-turn sources needs an
    # audited global phase-order interpreter.  Neither current equipped item
    # has a start-turn enchant, so the real Regular run does not hit this gate.
    if runtime.fired_effect_ids and item_start.fired_enchantment_ids:
        raise _Blocked("turn-start-cross-source-order-unverified")
    draw_count = context.rules.turn_start_distribute + runtime.draw_count
    if draw_count > context.rules.hand_limit:
        raise _Blocked(
            "turn-start-draw-exceeds-hand-limit",
            f"draw={draw_count};limit={context.rules.hand_limit}",
        )
    return item_start.after, draw_count


def _terminal_policy_key(
    node: _Node, context: _SearchContext
) -> tuple[LogicExamState, tuple[HorizonHandEntry, ...]] | None:
    """Prove that hidden zones cannot affect this node's policy value.

    The reduced key is used only when END_TURN and every legal visible-card
    action terminate immediately, before any draw or zone-sensitive created
    card can occur.  Full base/effective/support lineage remains in the Hand
    multiset.  A failed proof falls back to the complete :class:`_Node` key.
    """

    entries = tuple(sorted(node.zones.hand_entries))
    key = (node.state, entries)
    known = context.terminal_policy_proof_cache.get(key)
    if known is not None:
        return key if known else None

    proven = True
    skip = _prepare_skip_transition(node.state, context)
    if (
        skip.item_result.unsupported_rules
        or skip.item_result.post_card_effects
        or skip.item_result.fired_enchantment_ids
        or not skip.transition.legal
        or skip.transition.unsupported_rules
        or skip.transition.after.turns_remaining != 0
        or skip.transition.after.created_cards != node.state.created_cards
    ):
        proven = False

    if proven:
        for entry in tuple(dict.fromkeys(entries)):
            prepared = _prepare_card_transition(
                node.state, entry.effective_ref, context
            )
            transition = prepared.transition
            if not transition.legal:
                continue
            if (
                prepared.item_result.unsupported_rules
                or transition.unsupported_rules
                or transition.after.turns_remaining != 0
                or transition.after.created_cards != node.state.created_cards
            ):
                proven = False
                break
            direct_draw_effects = prepared.item_result.post_card_effects
            if transition.turn_ended:
                direct_draw_effects = (
                    *direct_draw_effects,
                    *prepared.item_result.end_turn_effects,
                )
            try:
                _direct_draw_count(
                    context.catalog[entry.effective_ref],
                    transition,
                    direct_draw_effects,
                )
            except _Blocked:
                proven = False
                break

    context.terminal_policy_proof_cache[key] = proven
    return key if proven else None


def _terminal_policy_value(
    node: _Node, context: _SearchContext
) -> HorizonExpectedValue:
    """Evaluate one proven immediate-terminal policy without recursion."""

    if _terminal_policy_key(node, context) is None:
        raise AssertionError("terminal policy value lacks an immediate-terminal proof")
    values: list[HorizonExpectedValue] = []
    blockers: list[HorizonBlocker] = []
    for _action_id, entry in _action_candidates(node, context):
        try:
            value, _transition = (
                _skip_action(node, context)
                if entry is None
                else _card_action(node, entry, context)
            )
        except _Blocked as exc:
            blockers.append(exc.blocker)
            continue
        values.append(value)
    incomplete = tuple(
        blocker
        for blocker in blockers
        if blocker.code not in {"illegal-action", "turn-skip-illegal"}
    )
    if incomplete:
        raise _Blocked(
            "future-action-set-incomplete", _blocker_summary(incomplete)
        )
    if not values:
        raise _Blocked(
            "no-supported-future-action", _blocker_summary(blockers)
        )
    return max(values, key=lambda value: value.sort_key)


def _chance_value(
    state: LogicExamState,
    zones: AuditionCardZones,
    draw_count: int,
    context: _SearchContext,
) -> HorizonExpectedValue:
    frame = context.turn_frames.get(state.round_number)
    if frame is None:
        raise _Blocked("future-turn-frame-missing", f"round={state.round_number}")
    cache_key = (state, zones, draw_count, frame.lesson_type)
    cached = context.chance_value_cache.get(cache_key)
    if cached is not None:
        context.chance_value_cache_hits += 1
        return cached

    outcomes = _draw_from_zones(
        zones, draw_count, context, lesson_type=frame.lesson_type
    )
    grouped: dict[
        tuple[str, object],
        tuple[
            _Node,
            Fraction,
            tuple[LogicExamState, tuple[HorizonHandEntry, ...]] | None,
        ],
    ] = {}
    for after_draw, probability in outcomes:
        node = _Node(state, after_draw)
        terminal_key = _terminal_policy_key(node, context)
        semantic_key: tuple[str, object]
        if terminal_key is not None:
            semantic_key = ("terminal-policy", terminal_key)
        else:
            semantic_key = ("full-node", node)
        known = grouped.get(semantic_key)
        if known is None:
            grouped[semantic_key] = (node, probability, terminal_key)
        else:
            grouped[semantic_key] = (
                known[0],
                known[1] + probability,
                known[2],
            )

    missing_terminal_keys = {
        terminal_key
        for _node, _probability, terminal_key in grouped.values()
        if terminal_key is not None
        and terminal_key not in context.terminal_policy_value_cache
    }
    distinct_terminal_count = (
        context.terminal_policy_groups_evaluated + len(missing_terminal_keys)
    )
    if distinct_terminal_count > context.limits.max_terminal_policy_states:
        raise _Blocked(
            "terminal-policy-state-limit-exceeded",
            f"states={distinct_terminal_count};"
            f"limit={context.limits.max_terminal_policy_states}",
        )
    context.terminal_policy_groups_evaluated = distinct_terminal_count
    context.terminal_policy_outcomes_merged += len(outcomes) - len(grouped)
    total = ZERO_EXPECTED_VALUE
    for node, probability, terminal_key in grouped.values():
        value = (
            context.terminal_policy_value_cache.get(terminal_key)
            if terminal_key is not None
            else None
        )
        if value is not None:
            context.terminal_policy_value_cache_hits += 1
        else:
            if terminal_key is not None:
                value = _terminal_policy_value(node, context)
                context.terminal_policy_value_cache[terminal_key] = value
            else:
                value = _node_value(node, context)
        total = total + value.weighted(probability)
    context.chance_value_cache[cache_key] = total
    return total


def _resolve_boundary_after_transition(
    state: LogicExamState,
    transition: LogicTransition,
    context: _SearchContext,
) -> AuditionBoundaryState | None:
    model = context.boundary_model
    if model is None or not transition.turn_ended or state.turns_remaining <= 0:
        return None
    return model.resolve(
        next_turn=state.round_number,
        post_boundary_recovery_units=state.turns_remaining,
    )


def _finish_action(
    node: _Node,
    transition: LogicTransition,
    hand_entry: HorizonHandEntry | None,
    direct_draw_count: int,
    context: _SearchContext,
) -> HorizonExpectedValue:
    if not transition.legal:
        raise _Blocked("illegal-action")
    if transition.unsupported_rules:
        raise _Blocked(
            "logic-transition-unsupported",
            ",".join(transition.unsupported_rules),
        )

    zones = node.zones
    played_card: MasterCard | None = None
    if hand_entry is not None:
        zones = zones.remove_hand_entry(hand_entry)
        played_card = context.catalog[hand_entry.effective_ref]
    zones = _apply_created_cards(node.state, transition.after, zones, context.catalog)

    after = transition.after
    if context.rules.force_end_score > 0:
        transition = complete_audition_if_forced(
            transition, context.rules.force_end_score
        )
        after = transition.after
    if after.turns_remaining == 0:
        return HorizonExpectedValue.terminal(
            after, force_end_score=context.rules.force_end_score
        )

    if direct_draw_count:
        # Card/status Draw resolves before the boundary.  If it was the last
        # play, those newly drawn cards join the rest of Hand in Grave below.
        drawn_values: list[tuple[AuditionCardZones, Fraction]] = list(
            _draw_from_zones(
                zones,
                direct_draw_count,
                context,
                lesson_type=context.turn_frames[node.state.round_number].lesson_type,
            )
        )
    else:
        drawn_values = [(zones, Fraction(1))]

    total = ZERO_EXPECTED_VALUE
    for drawn_zones, probability in drawn_values:
        # Native keeps the selected card in PlayingCard while its effects
        # (including Draw) resolve.  Only afterwards does MovePlayCard send it
        # to Grave/Lost.  Putting it in Grave before Draw could incorrectly
        # reshuffle and redraw the card when Deck is exhausted.
        if hand_entry is not None and played_card is not None:
            if played_card.move_position_type == MOVE_LOST:
                drawn_zones = replace(
                    drawn_zones,
                    lost_pile=(*drawn_zones.lost_pile, hand_entry.base_ref),
                )
            elif played_card.move_position_type == MOVE_GRAVE:
                drawn_zones = replace(
                    drawn_zones,
                    discard_pile=(
                        *drawn_zones.discard_pile,
                        hand_entry.base_ref,
                    ),
                )
            else:
                raise _Blocked(
                    "played-card-move-position-unsupported", hand_entry.key
                )
        if not transition.turn_ended:
            value = _node_value(_Node(after, drawn_zones), context)
        else:
            if drawn_zones.hand_hold_count:
                raise _Blocked(
                    "retain-hand-semantics-unsupported",
                    f"hand_hold_count={drawn_zones.hand_hold_count}",
                )
            closed = replace(
                drawn_zones,
                hand=(),
                hand_base=(),
                hand_support_deltas=(),
                discard_pile=_ordered_cards(
                    (*drawn_zones.discard_pile, *drawn_zones.hand_base)
                ),
                # ExamParameterModel.ClearTurnState runs before the next
                # HandAdd sequence.  Any unplayed effective refs expire here.
                used_support_ids=(),
            )
            boundary = _resolve_boundary_after_transition(
                after, transition, context
            )
            if boundary is not None and boundary.terminal:
                return HorizonExpectedValue.terminal_boundary(
                    after,
                    boundary,
                    force_end_score=context.rules.force_end_score,
                    recovery_stamina=context.rules.turn_end_stamina_recovery,
                    already_recovered=transition.end_turn_stamina_recovered,
                )
            next_state, draw_count = _prepare_next_turn(after, context)
            value = _chance_value(next_state, closed, draw_count, context)
        total = total + value.weighted(probability)
    return total


def _card_action(
    node: _Node,
    entry: HorizonHandEntry,
    context: _SearchContext,
) -> tuple[HorizonExpectedValue, LogicTransition]:
    ref = entry.effective_ref
    card = context.catalog[ref]
    prepared = _prepare_card_transition(node.state, ref, context)
    item_result = prepared.item_result
    if item_result.unsupported_rules:
        raise _Blocked(
            "equipped-item-transition-unsupported",
            ",".join(item_result.unsupported_rules),
        )
    transition = prepared.transition
    if not transition.legal:
        raise _Blocked("illegal-action", ref.key)
    direct_draw_effects = item_result.post_card_effects
    if transition.turn_ended:
        direct_draw_effects = (
            *direct_draw_effects,
            *item_result.end_turn_effects,
        )
    direct_draw = _direct_draw_count(card, transition, direct_draw_effects)
    value = _finish_action(node, transition, entry, direct_draw, context)
    return value, transition


def _skip_action(
    node: _Node, context: _SearchContext
) -> tuple[HorizonExpectedValue, LogicTransition]:
    prepared = _prepare_skip_transition(node.state, context)
    item_result = prepared.item_result
    if item_result.unsupported_rules:
        raise _Blocked(
            "turn-skip-item-unsupported",
            ",".join(item_result.unsupported_rules),
        )
    if (
        item_result.post_card_effects
        or item_result.fired_enchantment_ids
    ):
        raise _Blocked("turn-skip-item-use-accounting-unverified")
    transition = prepared.transition
    if not transition.legal:
        raise _Blocked(
            "turn-skip-illegal",
            ",".join(transition.unsupported_rules),
        )
    value = _finish_action(node, transition, None, 0, context)
    return value, transition


def _action_candidates(
    node: _Node, context: _SearchContext
) -> tuple[tuple[str, HorizonHandEntry | None], ...]:
    unique_entries = tuple(dict.fromkeys(node.zones.hand_entries))
    ranked: list[
        tuple[tuple[int, int, int, str], str, HorizonHandEntry | None]
    ] = []
    for entry in unique_entries:
        ref = entry.effective_ref
        card = context.catalog[ref]
        preview = _prepare_card_transition(node.state, ref, context).transition
        if not preview.legal:
            continue
        # This is only a beam ordering hint.  Every retained action still uses
        # the exact transition and expectimax value below.
        ranked.append(
            (
                (
                    card.evaluation,
                    preview.total_score_gain,
                    -card.stamina_cost,
                    ref.key,
                ),
                ref.key,
                entry,
            )
        )
    ranked.sort(reverse=True)
    card_candidates = ranked[: context.limits.action_beam_width]
    include_skip = True
    if len(ranked) > context.limits.action_beam_width:
        if not context.limits.allow_advisory_beam:
            raise _Blocked(
                "action-beam-width-exceeded",
                f"actions={len(ranked)};limit={context.limits.action_beam_width}",
            )
        context.action_branches_pruned += (
            len(ranked) - context.limits.action_beam_width
        )
        # In advisory mode, the beam is a policy-width budget.  END_TURN is
        # deliberately outside that budget unless there was room for every
        # playable card.  Root END_TURN is still evaluated independently.
        include_skip = False
        context.action_branches_pruned += 1
    result = tuple(
        (action_id, entry) for _hint, action_id, entry in card_candidates
    )
    if include_skip:
        result += (("END_TURN", None),)
    return result


def _node_value(node: _Node, context: _SearchContext) -> HorizonExpectedValue:
    if node.state.turns_remaining == 0:
        return HorizonExpectedValue.terminal(
            node.state, force_end_score=context.rules.force_end_score
        )
    cached = context.memo.get(node)
    if cached is not None:
        return cached
    context.expand_node()
    values: list[HorizonExpectedValue] = []
    blockers: list[HorizonBlocker] = []
    for _action_id, entry in _action_candidates(node, context):
        try:
            value, _transition = (
                _skip_action(node, context)
                if entry is None
                else _card_action(node, entry, context)
            )
        except _Blocked as exc:
            # A global exact-search budget failure invalidates the entire
            # policy tree.  Propagate it directly instead of nesting one
            # ``future-action-set-incomplete`` wrapper per recursion level and
            # continuing to spend work on sibling/root actions.
            if exc.blocker.code == "search-node-limit-exceeded":
                raise
            blockers.append(exc.blocker)
            continue
        values.append(value)
    incomplete = tuple(
        blocker
        for blocker in blockers
        if blocker.code not in {"illegal-action", "turn-skip-illegal"}
    )
    if incomplete:
        raise _Blocked(
            "future-action-set-incomplete", _blocker_summary(incomplete)
        )
    if not values:
        raise _Blocked(
            "no-supported-future-action", _blocker_summary(blockers)
        )
    best = max(values, key=lambda value: value.sort_key)
    context.memo[node] = best
    return best


def recommend_audition_horizon(
    state: LogicExamState,
    zones: AuditionCardZones,
    rules: AuditionRules,
    schedule: VerifiedTurnSchedule,
    catalog: Mapping[HorizonCardRef, MasterCard],
    item: EquippedItemRule,
    *,
    limits: HorizonSearchLimits = HorizonSearchLimits(),
    support_upgrades: Mapping[str, SupportUpgradeRuntimeInput]
    | Iterable[SupportUpgradeRuntimeInput] = (),
    support_card_searches: Mapping[str, ProduceCardSearchRule] = {},
    boundary_model: AuditionBoundaryModel | None = None,
) -> HorizonDecision:
    """Evaluate every visible root action through the complete remaining exam.

    A result is decision-ready only when the entire future policy for at least
    one root action reaches terminal states without unsupported rules or
    safety-limit truncation.  Other root actions may be reported as blocked,
    but a blocked action never outranks a fully evaluated one.
    """

    supports = (
        tuple(support_upgrades.values())
        if isinstance(support_upgrades, Mapping)
        else tuple(support_upgrades)
    )
    blockers = list(
        _validate_inputs(
            state,
            zones,
            rules,
            schedule,
            catalog,
            item,
            limits,
            support_upgrades,
            support_card_searches,
            boundary_model=boundary_model,
        )
    )
    try:
        schedule_digest = schedule.digest()
    except ValueError:
        schedule_digest = ""
    if blockers:
        return HorizonDecision(
            decision_ready=False,
            recommendations=(),
            blockers=tuple(blockers),
            diagnostics=HorizonDiagnostics(
                schedule_digest=schedule_digest,
                nodes_expanded=0,
                memo_entries=0,
                chance_outcomes_evaluated=0,
                maximum_chance_fanout=0,
                full_terminal_horizon=False,
            ),
        )

    context = _SearchContext(
        rules,
        schedule,
        catalog,
        item,
        limits,
        supports,
        support_card_searches,
        boundary_model,
    )
    root = _Node(state, zones)
    evaluations: list[HorizonActionEvaluation] = []
    seen: set[HorizonHandEntry] = set()
    for hand_index, entry in enumerate(zones.hand_entries):
        if entry in seen:
            continue
        seen.add(entry)
        ref = entry.effective_ref
        card = catalog[ref]
        try:
            value, transition = _card_action(root, entry, context)
            evaluations.append(
                HorizonActionEvaluation(
                    action_id=ref.key,
                    hand_index=hand_index,
                    card=card,
                    value=value,
                    immediate_transition=transition,
                )
            )
        except _Blocked as exc:
            evaluations.append(
                HorizonActionEvaluation(
                    action_id=ref.key,
                    hand_index=hand_index,
                    card=card,
                    value=None,
                    immediate_transition=None,
                    blockers=(exc.blocker,),
                )
            )

    try:
        value, transition = _skip_action(root, context)
        evaluations.append(
            HorizonActionEvaluation(
                action_id="END_TURN",
                hand_index=None,
                card=None,
                value=value,
                immediate_transition=transition,
            )
        )
    except _Blocked as exc:
        evaluations.append(
            HorizonActionEvaluation(
                action_id="END_TURN",
                hand_index=None,
                card=None,
                value=None,
                immediate_transition=None,
                blockers=(exc.blocker,),
            )
        )

    evaluations.sort(
        key=lambda item: (
            item.fully_supported,
            item.value.sort_key if item.value is not None else (Fraction(-1),) * 3,
            item.action_id,
        ),
        reverse=True,
    )
    supported = [item for item in evaluations if item.fully_supported]
    incomplete_root = [
        blocker
        for item in evaluations
        for blocker in item.blockers
        if blocker.code not in {"illegal-action", "turn-skip-illegal"}
    ]
    if incomplete_root:
        blockers.append(
            HorizonBlocker(
                "root-action-set-incomplete",
                _blocker_summary(incomplete_root),
            )
        )
    if not supported:
        blockers.append(HorizonBlocker("no-supported-root-action"))
        blockers.extend(
            blocker for item in evaluations for blocker in item.blockers
        )
    approximation_used = bool(
        context.action_branches_pruned or context.chance_outcomes_pruned
    )
    if approximation_used:
        blockers.append(
            HorizonBlocker(
                "advisory-beam-approximation-used",
                f"action_pruned={context.action_branches_pruned};"
                f"chance_pruned={context.chance_outcomes_pruned}",
            )
        )
    full_terminal_horizon = (
        bool(supported) and not incomplete_root and not approximation_used
    )
    return HorizonDecision(
        decision_ready=(
            bool(supported) and not incomplete_root and not approximation_used
        ),
        recommendations=tuple(evaluations),
        blockers=tuple(dict.fromkeys(blockers)),
        diagnostics=HorizonDiagnostics(
            schedule_digest=schedule_digest,
            nodes_expanded=context.nodes_expanded,
            memo_entries=len(context.memo),
            chance_outcomes_evaluated=context.chance_outcomes_evaluated,
            maximum_chance_fanout=context.maximum_chance_fanout,
            chance_dp_states_evaluated=context.chance_dp_states_evaluated,
            maximum_chance_dp_states=context.maximum_chance_dp_states,
            draw_outcome_cache_hits=context.draw_outcome_cache_hits,
            chance_value_cache_hits=context.chance_value_cache_hits,
            terminal_policy_groups_evaluated=(
                context.terminal_policy_groups_evaluated
            ),
            terminal_policy_outcomes_merged=(
                context.terminal_policy_outcomes_merged
            ),
            terminal_policy_value_cache_hits=(
                context.terminal_policy_value_cache_hits
            ),
            full_terminal_horizon=full_terminal_horizon,
            approximation_used=approximation_used,
            action_branches_pruned=context.action_branches_pruned,
            chance_outcomes_pruned=context.chance_outcomes_pruned,
        ),
    )


def stacks_from_refs(
    cards: Iterable[HorizonCardRef], *, confidence: float = 1.0
) -> tuple[DeckCardStack, ...]:
    """Small deterministic adapter useful to audits and test fixtures."""

    counts = Counter(_ordered_cards(cards))
    return tuple(
        DeckCardStack(ref.card_id, ref.upgrade, count, confidence)
        for ref, count in sorted(counts.items())
    )
