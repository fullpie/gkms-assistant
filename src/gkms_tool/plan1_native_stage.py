"""Minimal deterministic stage runner for the native Plan 1 card core.

The card core deliberately stops at one ``MovePlayCard`` transaction.  This
module supplies only the small amount of orchestration that can be proven by
the shared native ordered-zone model: enumerate the visible Hand, stage the
played card in the native pending slot, dispatch the exact FKTN SSR mandatory
beforeProduce listener when its target is accepted, interleave direct ``CardDraw`` and
fixed ``CardCreateId`` slots with scalar effects, settle the played card, then
(when the caller supplies the observed turn inputs) close the Hand, draw, and
advance scalar counters.

Turn-start playable values, draw counts, and end-turn Lost classification are
runtime inputs.  CardCreateId GUIDs are likewise explicit caller authority;
the stage only lifts the exact native constructor defaults and placement
result.  Missing inputs are represented by typed :class:`Plan1Blocker` values.
The runner does not synthesize GUIDs, shuffle an invented deck, or perform I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Iterable, Mapping, Sequence

from .audition_local_save_state import (
    LocalSaveExamCard,
    empty_local_save_exam_card_runtime_state,
)
from .audition_native_ordered_zones import (
    NativeEndTurnDisposition,
    NativeOrderedCardInstance,
    NativeOrderedDrawTransition,
    NativeOrderedZoneError,
    NativeOrderedZoneState,
)
from .audition_native_support import (
    NativeHandAddCard,
    NativeHandAddCardResult,
    NativeHandAddSupportError,
    NativeHandAddSupportResult,
)
from .plan1_native_core import (
    EFFECT_CARD_UPGRADE,
    EFFECT_CARD_CREATE_ID,
    EFFECT_CARD_DRAW,
    EFFECT_CARD_MOVE,
    EFFECT_HAND_GRAVE_COUNT_CARD_DRAW,
    EFFECT_TIMER,
    FKTN_SSR_PLAN1_BEFORE_ITEM_ID,
    PLAN1_TIMER_CHILD_EFFECT_TYPES,
    Plan1Blocker,
    Plan1CardCreateResolution,
    Plan1CardDrawResolution,
    Plan1CardMoveResolution,
    Plan1CardUpgradeResolution,
    Plan1CardTransition,
    Plan1CompiledCard,
    Plan1CompiledEffect,
    Plan1DeckCompilation,
    Plan1EffectTimerResolution,
    Plan1EquippedItemRuntime,
    Plan1HandGraveDrawResolution,
    Plan1NativeSettings,
    Plan1ScalarState,
    Plan1TraceEntry,
    Plan1TransitionStatus,
    advance_plan1_turn,
    execute_plan1_card,
    execute_plan1_effects,
    load_plan1_native_settings,
    validate_fktn_ssr_plan1_equipped_item_runtime,
)
from .plan2_core_runtime import apply_plan2_card_move_lost_random_direct
from .plan3_card_create_id import (
    CARD_CREATE_ID_PATTERN,
    CardCreateContract,
    CardCreateError,
    CardCreateMutationTrace,
    apply_card_create_id,
)
from .plan3_card_upgrade import execute_plan3_card_upgrade
from .plan3_native_state import Plan3NativeCard, Plan3NativeState


def _blocker(code: str, source_id: str, detail: str = "") -> Plan1Blocker:
    return Plan1Blocker(code, source_id, detail)


@dataclass(frozen=True, slots=True)
class Plan1TurnStartPhaseFacts:
    """Caller-observed commands between ordinary draw and Timer dispatch.

    The minimum Timer slice can prove only the empty ``ExamStartTurn`` and
    ``StartPlay`` command lists.  Requiring this explicit object whenever a
    timer is active prevents an absent runtime observation from being treated
    as an empty phase.
    """

    exam_start_turn_effect_ids: tuple[str, ...] = ()
    start_play_effect_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("exam_start_turn_effect_ids", "start_play_effect_ids"):
            values = tuple(getattr(self, name))
            if any(not isinstance(value, str) or not value for value in values):
                raise ValueError(f"{name} must contain non-empty effect ids")
            object.__setattr__(self, name, values)

    @property
    def empty(self) -> bool:
        return not self.exam_start_turn_effect_ids and not self.start_play_effect_ids


@dataclass(frozen=True, slots=True)
class Plan1StageTurnBoundary:
    """Observed inputs used while entering the next turn.

    ``plays_remaining`` is intentionally optional at the type level so a
    caller can represent an unresolved native turn-start reset.  The stage
    runner fails closed when a subsequent action would need that value.
    ``dispositions`` classifies every card left in Hand using the exact native
    ``ResetHand`` primitive.  The mapping is copied on construction to prevent
    a mutable caller-owned dictionary from changing a stage after the fact.
    """

    draw_count: int = 0
    plays_remaining: int | None = None
    dispositions: tuple[tuple[str, NativeEndTurnDisposition], ...] = ()
    phase_facts: Plan1TurnStartPhaseFacts | None = None
    exam_start_turn_effects: tuple[Plan1CompiledEffect, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.draw_count, bool) or not isinstance(self.draw_count, int):
            raise TypeError("draw_count must be an integer")
        if self.draw_count < 0:
            raise ValueError("draw_count must be non-negative")
        if self.plays_remaining is not None:
            if isinstance(self.plays_remaining, bool) or not isinstance(
                self.plays_remaining, int
            ):
                raise TypeError("plays_remaining must be an integer or None")
            if self.plays_remaining < 0:
                raise ValueError("plays_remaining must be non-negative")
        values = tuple(self.dispositions)
        seen: set[str] = set()
        canonical: list[tuple[str, NativeEndTurnDisposition]] = []
        for guid, disposition in values:
            if not isinstance(guid, str) or not guid:
                raise TypeError("disposition GUIDs must be non-empty text")
            if guid in seen:
                raise ValueError(f"duplicate disposition GUID: {guid!r}")
            if not isinstance(disposition, NativeEndTurnDisposition):
                raise TypeError(
                    "dispositions must contain NativeEndTurnDisposition values"
                )
            seen.add(guid)
            canonical.append((guid, disposition))
        object.__setattr__(self, "dispositions", tuple(canonical))
        if self.phase_facts is not None and not isinstance(
            self.phase_facts, Plan1TurnStartPhaseFacts
        ):
            raise TypeError("phase_facts must be Plan1TurnStartPhaseFacts or None")
        effects = tuple(self.exam_start_turn_effects)
        if any(not isinstance(value, Plan1CompiledEffect) for value in effects):
            raise TypeError(
                "exam_start_turn_effects must contain Plan1CompiledEffect values"
            )
        if any(value.blockers for value in effects):
            raise ValueError("exam_start_turn_effects must be executable")
        object.__setattr__(self, "exam_start_turn_effects", effects)

    @classmethod
    def from_mapping(
        cls,
        *,
        draw_count: int = 0,
        plays_remaining: int | None = None,
        dispositions: Mapping[str, NativeEndTurnDisposition] | None = None,
        phase_facts: Plan1TurnStartPhaseFacts | None = None,
        exam_start_turn_effects: Sequence[Plan1CompiledEffect] = (),
    ) -> "Plan1StageTurnBoundary":
        values = () if dispositions is None else tuple(dispositions.items())
        return cls(
            draw_count,
            plays_remaining,
            values,
            phase_facts,
            tuple(exam_start_turn_effects),
        )

    def disposition_mapping(self) -> dict[str, NativeEndTurnDisposition]:
        return dict(self.dispositions)


@dataclass(frozen=True, slots=True)
class Plan1StageCardDraw:
    """One CardDraw slot resolved against exact ordered zones."""

    effect_id: str
    effect_index: int
    requested_count: int
    actual_count: int
    before_zones: NativeOrderedZoneState
    native_transition: NativeOrderedDrawTransition

    def __post_init__(self) -> None:
        if not isinstance(self.effect_id, str) or not self.effect_id:
            raise ValueError("effect_id must be non-empty")
        for name in ("effect_index", "requested_count", "actual_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.actual_count > self.requested_count:
            raise ValueError("actual_count must not exceed requested_count")
        if not isinstance(self.before_zones, NativeOrderedZoneState):
            raise TypeError("before_zones must be NativeOrderedZoneState")
        if not isinstance(self.native_transition, NativeOrderedDrawTransition):
            raise TypeError("native_transition must be NativeOrderedDrawTransition")
        if self.native_transition.random_state_before != self.before_zones.random_state:
            raise ValueError("CardDraw RNG state is not chained from before_zones")
        if len(self.native_transition.drawn_guids) != self.actual_count:
            raise ValueError("actual_count must equal the ordered drawn GUID count")
        if self.before_zones.card_universe != self.after_zones.card_universe:
            raise ValueError("CardDraw must preserve every card runtime identity")
        if self.before_zones.pending_played != self.after_zones.pending_played:
            raise ValueError("CardDraw must not settle or replace the playing card")

    @property
    def after_zones(self) -> NativeOrderedZoneState:
        return self.native_transition.state

    @property
    def drawn_guids(self) -> tuple[str, ...]:
        return self.native_transition.drawn_guids


@dataclass(frozen=True, slots=True)
class Plan1HandAddSupportRequest:
    """Exact post-draw callback input; runtime/catalog stay closure-bound."""

    effect_id: str
    effect_index: int
    drawn_cards: tuple[NativeHandAddCard, ...]
    random_state: int
    used_support_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.effect_id, str) or not self.effect_id:
            raise ValueError("effect_id must be non-empty")
        if (
            isinstance(self.effect_index, bool)
            or not isinstance(self.effect_index, int)
            or self.effect_index < 0
        ):
            raise ValueError("effect_index must be a non-negative integer")
        cards = tuple(self.drawn_cards)
        if any(not isinstance(value, NativeHandAddCard) for value in cards):
            raise TypeError("drawn_cards must contain NativeHandAddCard values")
        if len({value.guid for value in cards}) != len(cards):
            raise ValueError("drawn card GUIDs must be unique")
        if (
            isinstance(self.random_state, bool)
            or not isinstance(self.random_state, int)
            or not 0 <= self.random_state <= 0xFFFFFFFF
        ):
            raise ValueError("random_state must be uint32")
        used = tuple(self.used_support_ids)
        if any(not isinstance(value, str) or not value for value in used):
            raise ValueError("used_support_ids must contain non-empty text")
        if len(set(used)) != len(used):
            raise ValueError("used_support_ids must be unique")
        object.__setattr__(self, "drawn_cards", cards)
        object.__setattr__(self, "used_support_ids", used)


Plan1HandAddSupportResolver = Callable[
    [Plan1HandAddSupportRequest], NativeHandAddSupportResult
]


@dataclass(frozen=True, slots=True)
class Plan1StageHandAddSupport:
    """Validated HandAdd support callback and its committed zone mutation."""

    request: Plan1HandAddSupportRequest
    result: NativeHandAddSupportResult
    before_zones: NativeOrderedZoneState
    after_zones: NativeOrderedZoneState

    def __post_init__(self) -> None:
        if not isinstance(self.request, Plan1HandAddSupportRequest):
            raise TypeError("request must be Plan1HandAddSupportRequest")
        if not isinstance(self.result, NativeHandAddSupportResult):
            raise TypeError("result must be NativeHandAddSupportResult")
        if not isinstance(self.before_zones, NativeOrderedZoneState) or not isinstance(
            self.after_zones, NativeOrderedZoneState
        ):
            raise TypeError("HandAdd zones must be NativeOrderedZoneState")
        if self.before_zones.random_state != self.request.random_state:
            raise ValueError("HandAdd request RNG does not match before_zones")
        if self.result.initial_random_state != self.request.random_state:
            raise ValueError("HandAdd result RNG does not start at the request")
        if self.after_zones.random_state != self.result.final_random_state:
            raise ValueError("HandAdd result RNG does not match after_zones")
        if not set(self.request.used_support_ids) <= set(
            self.result.used_support_ids
        ):
            raise ValueError("HandAdd result dropped an already-used support id")
        requested = tuple(
            (value.guid, value.card_id, value.base_upgrade, value.effective_upgrade)
            for value in self.request.drawn_cards
        )
        returned = tuple(
            (
                value.guid,
                value.card_id,
                value.base_upgrade,
                value.initial_effective_upgrade,
            )
            for value in self.result.cards
        )
        if returned != requested:
            raise ValueError("HandAdd result card identity/order changed")
        for name in ("hand", "deck", "grave", "lost"):
            if tuple(card.guid for card in getattr(self.before_zones, name)) != tuple(
                card.guid for card in getattr(self.after_zones, name)
            ):
                raise ValueError("HandAdd callback changed ordered zone membership")
        before_pending = self.before_zones.pending_played
        after_pending = self.after_zones.pending_played
        if (
            None if before_pending is None else before_pending.guid
        ) != (None if after_pending is None else after_pending.guid):
            raise ValueError("HandAdd callback changed pending played identity")
        before_identity = tuple(
            card.persistent_identity for card in self.before_zones.card_universe
        )
        after_identity = tuple(
            card.persistent_identity for card in self.after_zones.card_universe
        )
        if before_identity != after_identity:
            raise ValueError("HandAdd callback changed persistent card identity")


@dataclass(frozen=True, slots=True)
class Plan1StageHandGraveDraw:
    """Intermediate trace for Hand snapshot -> Grave -> Draw -> HandAdd."""

    effect_id: str
    effect_index: int
    before_zones: NativeOrderedZoneState
    hand_snapshot_guids: tuple[str, ...]
    after_hand_to_grave: NativeOrderedZoneState
    native_draw: NativeOrderedDrawTransition
    hand_add: Plan1StageHandAddSupport

    def __post_init__(self) -> None:
        if not isinstance(self.effect_id, str) or not self.effect_id:
            raise ValueError("effect_id must be non-empty")
        if (
            isinstance(self.effect_index, bool)
            or not isinstance(self.effect_index, int)
            or self.effect_index < 0
        ):
            raise ValueError("effect_index must be a non-negative integer")
        for value in (
            self.before_zones,
            self.after_hand_to_grave,
        ):
            if not isinstance(value, NativeOrderedZoneState):
                raise TypeError("HandGraveDraw states must be ordered zones")
        if not isinstance(self.native_draw, NativeOrderedDrawTransition):
            raise TypeError("native_draw must be NativeOrderedDrawTransition")
        if not isinstance(self.hand_add, Plan1StageHandAddSupport):
            raise TypeError("hand_add must be Plan1StageHandAddSupport")
        snapshot = tuple(self.hand_snapshot_guids)
        expected_snapshot = tuple(card.guid for card in self.before_zones.hand)
        if snapshot != expected_snapshot:
            raise ValueError("HandGraveDraw snapshot must preserve Hand order")
        expected_grave = (
            *self.before_zones.grave,
            *(card.reset_support_upgrade() for card in self.before_zones.hand),
        )
        if (
            self.after_hand_to_grave.hand
            or self.after_hand_to_grave.grave != expected_grave
            or self.after_hand_to_grave.deck != self.before_zones.deck
            or self.after_hand_to_grave.lost != self.before_zones.lost
            or self.after_hand_to_grave.pending_played
            != self.before_zones.pending_played
            or self.after_hand_to_grave.random_state
            != self.before_zones.random_state
        ):
            raise ValueError("HandGraveDraw move intermediate changed non-Hand zones")
        if self.native_draw.random_state_before != self.after_hand_to_grave.random_state:
            raise ValueError("HandGraveDraw draw RNG is not chained from the move")
        if len(self.native_draw.drawn_guids) != len(snapshot):
            raise ValueError("HandGraveDraw actual count changed from the Hand snapshot")
        if self.hand_add.before_zones != self.native_draw.state:
            raise ValueError("HandAdd must follow the native draw immediately")
        if tuple(value.guid for value in self.hand_add.request.drawn_cards) != (
            self.native_draw.drawn_guids
        ):
            raise ValueError("HandAdd order must equal native draw order")
        object.__setattr__(self, "hand_snapshot_guids", snapshot)

    @property
    def requested_count(self) -> int:
        return len(self.hand_snapshot_guids)

    @property
    def actual_count(self) -> int:
        return len(self.native_draw.drawn_guids)

    @property
    def after_zones(self) -> NativeOrderedZoneState:
        return self.hand_add.after_zones


@dataclass(frozen=True, slots=True)
class Plan1EffectTimerInstance:
    """One separately installed native one-shot ``ExamTurnTimer``."""

    timer_effect: Plan1CompiledEffect
    source_guid: str
    source_play_count: int
    effect_index: int
    install_ordinal: int
    elapsed_turns: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.timer_effect, Plan1CompiledEffect):
            raise TypeError("timer_effect must be Plan1CompiledEffect")
        child = self.timer_effect.timer_child
        if (
            self.timer_effect.effect_type != EFFECT_TIMER
            or not self.timer_effect.executable
            or child is None
            or child.effect_type not in PLAN1_TIMER_CHILD_EFFECT_TYPES
            or child.effect_type == EFFECT_TIMER
            or not child.executable
        ):
            raise ValueError("timer_effect is outside the exact Plan1 Timer slice")
        if not isinstance(self.source_guid, str) or not self.source_guid:
            raise ValueError("source_guid must be non-empty")
        for name in (
            "source_play_count",
            "effect_index",
            "install_ordinal",
            "elapsed_turns",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.elapsed_turns >= self.delay_turns:
            raise ValueError("an active timer must not already be due")

    @property
    def timer_effect_id(self) -> str:
        return self.timer_effect.effect_id

    @property
    def child_effect(self) -> Plan1CompiledEffect:
        child = self.timer_effect.timer_child
        assert child is not None
        return child

    @property
    def delay_turns(self) -> int:
        return self.timer_effect.value1

    @property
    def identity(self) -> tuple[str, int, int, int]:
        return (
            self.source_guid,
            self.source_play_count,
            self.effect_index,
            self.install_ordinal,
        )


@dataclass(frozen=True, slots=True)
class Plan1StageTimerFire:
    """Committed trace for one due timer child, in installation order."""

    timer: Plan1EffectTimerInstance
    before_scalar: Plan1ScalarState
    after_scalar: Plan1ScalarState
    before_zones: NativeOrderedZoneState
    after_zones: NativeOrderedZoneState
    effect_trace: tuple[Plan1TraceEntry, ...]
    card_draws: tuple[Plan1StageCardDraw, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.timer, Plan1EffectTimerInstance):
            raise TypeError("timer must be Plan1EffectTimerInstance")
        if self.timer.elapsed_turns + 1 != self.timer.delay_turns:
            raise ValueError("Timer fire must occur at its exact relative boundary")
        for value, label, expected in (
            (self.before_scalar, "before_scalar", Plan1ScalarState),
            (self.after_scalar, "after_scalar", Plan1ScalarState),
            (self.before_zones, "before_zones", NativeOrderedZoneState),
            (self.after_zones, "after_zones", NativeOrderedZoneState),
        ):
            if not isinstance(value, expected):
                raise TypeError(f"{label} has the wrong type")
        traces = tuple(self.effect_trace)
        draws = tuple(self.card_draws)
        if any(not isinstance(value, Plan1TraceEntry) for value in traces):
            raise TypeError("effect_trace must contain Plan1TraceEntry values")
        if any(not isinstance(value, Plan1StageCardDraw) for value in draws):
            raise TypeError("card_draws must contain Plan1StageCardDraw values")
        if not traces:
            raise ValueError("a fired Timer child must retain its effect trace")
        if traces[0].before != self.before_scalar or traces[-1].after != self.after_scalar:
            raise ValueError("Timer scalar trace endpoints do not match the fire")
        if any(
            left.after != right.before
            for left, right in zip(traces, traces[1:])
        ):
            raise ValueError("Timer scalar trace is not contiguous")
        if any(value.source_id != self.child_effect_id for value in traces):
            raise ValueError("Timer trace child identity changed")
        if self.timer.child_effect.effect_type == EFFECT_CARD_DRAW:
            if len(draws) != 1:
                raise ValueError("Timer CardDraw must retain one ordered-zone command")
            if draws[0].before_zones != self.before_zones:
                raise ValueError("Timer CardDraw zone endpoints do not match the fire")
            for name in ("hand", "deck", "grave", "lost"):
                if tuple(
                    card.guid for card in getattr(draws[0].after_zones, name)
                ) != tuple(
                    card.guid for card in getattr(self.after_zones, name)
                ):
                    raise ValueError(
                        "Timer CardDraw post-HandAdd changed zone membership/order"
                    )
        elif self.timer.child_effect.effect_type == EFFECT_CARD_UPGRADE:
            if draws:
                raise ValueError("Timer CardUpgrade must not retain CardDraw commands")
            for name in ("hand", "deck", "grave", "lost"):
                if tuple(
                    card.guid for card in getattr(self.before_zones, name)
                ) != tuple(
                    card.guid for card in getattr(self.after_zones, name)
                ):
                    raise ValueError("Timer CardUpgrade changed zone membership/order")
            if (
                self.before_zones.random_state != self.after_zones.random_state
                or self.before_zones.pending_played
                != self.after_zones.pending_played
            ):
                raise ValueError("Timer CardUpgrade changed RNG or pending card")
        elif draws or self.before_zones != self.after_zones:
            raise ValueError("scalar Timer children must not mutate ordered zones")
        object.__setattr__(self, "effect_trace", traces)
        object.__setattr__(self, "card_draws", draws)

    @property
    def child_effect_id(self) -> str:
        return self.timer.child_effect.effect_id


@dataclass(frozen=True, slots=True)
class Plan1CardCreateGuidBinding:
    """Caller-authoritative GUIDs for one exact CardCreateId invocation.

    Native GUID creation is lazy and independent of the exam RNG.  The
    playing GUID plus its pre-play runtime count identifies an invocation
    without a mutable allocator or hidden cache.
    """

    source_guid: str
    source_play_count: int
    effect_id: str
    effect_index: int
    guid_tokens: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("source_guid", "effect_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty text")
        for name in ("source_play_count", "effect_index"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        tokens = tuple(self.guid_tokens)
        if any(not isinstance(value, str) or not value.strip() for value in tokens):
            raise ValueError("guid_tokens must contain non-empty text")
        if len(set(tokens)) != len(tokens):
            raise ValueError("guid_tokens must not contain duplicates")
        object.__setattr__(self, "guid_tokens", tokens)

    @property
    def invocation_key(self) -> tuple[str, int, str, int]:
        return (
            self.source_guid,
            self.source_play_count,
            self.effect_id,
            self.effect_index,
        )


@dataclass(frozen=True, slots=True)
class Plan1StageCardCreate:
    """One CardCreateId slot resolved by the shared native executor."""

    effect_id: str
    effect_index: int
    source_guid: str
    source_play_count: int
    before_zones: NativeOrderedZoneState
    after_zones: NativeOrderedZoneState
    native_trace: CardCreateMutationTrace

    def __post_init__(self) -> None:
        for name in ("effect_id", "source_guid"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty text")
        for name in ("effect_index", "source_play_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.before_zones, NativeOrderedZoneState) or not isinstance(
            self.after_zones, NativeOrderedZoneState
        ):
            raise TypeError("CardCreateId zones must be NativeOrderedZoneState")
        if not isinstance(self.native_trace, CardCreateMutationTrace):
            raise TypeError("native_trace must be CardCreateMutationTrace")
        if self.native_trace.effect_id != self.effect_id:
            raise ValueError("CardCreateId trace effect identity changed")
        if self.native_trace.random_state_before != self.before_zones.random_state:
            raise ValueError("CardCreateId RNG is not chained from before_zones")
        if self.native_trace.random_state_after != self.after_zones.random_state:
            raise ValueError("CardCreateId RNG does not match after_zones")
        if self.before_zones.pending_played != self.after_zones.pending_played:
            raise ValueError("CardCreateId must not settle or replace the playing card")

    @property
    def allocated_guids(self) -> tuple[str, ...]:
        return self.native_trace.allocated_guids

    @property
    def insertion_indices(self) -> tuple[int, ...]:
        return tuple(value.insertion_index for value in self.native_trace.mutations)


@dataclass(frozen=True, slots=True)
class Plan1StageEquippedItemFire:
    """Committed preview of the exact one-use ExamCardPlay item listener."""

    runtime_before: Plan1EquippedItemRuntime
    runtime_after: Plan1EquippedItemRuntime
    target_guid: str
    scalar_before: Plan1ScalarState
    scalar_after: Plan1ScalarState
    trace: tuple[Plan1TraceEntry, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_before, Plan1EquippedItemRuntime) or not isinstance(
            self.runtime_after, Plan1EquippedItemRuntime
        ):
            raise TypeError("item fire runtimes must be Plan1EquippedItemRuntime")
        if not isinstance(self.target_guid, str) or not self.target_guid:
            raise ValueError("target_guid must be non-empty text")
        if not isinstance(self.scalar_before, Plan1ScalarState) or not isinstance(
            self.scalar_after, Plan1ScalarState
        ):
            raise TypeError("item fire scalars must be Plan1ScalarState")
        trace = tuple(self.trace)
        if any(not isinstance(value, Plan1TraceEntry) for value in trace):
            raise TypeError("trace must contain Plan1TraceEntry values")
        if self.runtime_before.contract_identity != self.runtime_after.contract_identity:
            raise ValueError("item contract identity changed while consuming a use")
        if self.runtime_before.remaining_uses != self.runtime_after.remaining_uses + 1:
            raise ValueError("item fire must consume exactly one remaining use")
        if tuple(value.source_id for value in trace) != tuple(
            effect.effect_id for effect in self.runtime_before.effects
        ):
            raise ValueError("item fire trace must preserve Master effect order")
        if not trace or trace[0].before != self.scalar_before:
            raise ValueError("item fire trace does not start at scalar_before")
        for before_entry, after_entry in zip(trace, trace[1:]):
            if before_entry.after != after_entry.before:
                raise ValueError("item fire trace scalar chain is discontinuous")
        if trace[-1].after != self.scalar_after:
            raise ValueError("item fire trace does not end at scalar_after")
        object.__setattr__(self, "trace", trace)


@dataclass(frozen=True, slots=True)
class Plan1StageAction:
    """One visible Hand candidate and its pure card-core preview."""

    hand_index: int
    card: NativeOrderedCardInstance
    program: Plan1CompiledCard | None
    transition: Plan1CardTransition | None
    blockers: tuple[Plan1Blocker, ...] = ()
    zones_before: NativeOrderedZoneState | None = None
    zones_after: NativeOrderedZoneState | None = None
    settlement: str = ""
    card_draws: tuple[Plan1StageCardDraw, ...] = ()
    card_creates: tuple[Plan1StageCardCreate, ...] = ()
    timer_installs: tuple[Plan1EffectTimerInstance, ...] = ()
    timers_before: tuple[Plan1EffectTimerInstance, ...] = ()
    timer_install_ordinal_before: int = 0
    hand_grave_draws: tuple[Plan1StageHandGraveDraw, ...] = ()
    hand_add_supports: tuple[Plan1StageHandAddSupport, ...] = ()
    hand_add_used_support_ids_before: tuple[str, ...] = ()
    hand_add_used_support_ids_after: tuple[str, ...] = ()
    equipped_item_runtime_before: Plan1EquippedItemRuntime | None = None
    equipped_item_runtime_after: Plan1EquippedItemRuntime | None = None
    equipped_item_fire: Plan1StageEquippedItemFire | None = None

    def __post_init__(self) -> None:
        if isinstance(self.hand_index, bool) or not isinstance(self.hand_index, int):
            raise TypeError("hand_index must be an integer")
        if self.hand_index < 0:
            raise ValueError("hand_index must be non-negative")
        if not isinstance(self.card, NativeOrderedCardInstance):
            raise TypeError("card must be NativeOrderedCardInstance")
        if self.program is not None and not isinstance(
            self.program, Plan1CompiledCard
        ):
            raise TypeError("program must be Plan1CompiledCard or None")
        if self.transition is not None and not isinstance(
            self.transition, Plan1CardTransition
        ):
            raise TypeError("transition must be Plan1CardTransition or None")
        object.__setattr__(self, "blockers", tuple(self.blockers))
        object.__setattr__(self, "card_draws", tuple(self.card_draws))
        object.__setattr__(self, "card_creates", tuple(self.card_creates))
        object.__setattr__(self, "timer_installs", tuple(self.timer_installs))
        object.__setattr__(self, "timers_before", tuple(self.timers_before))
        object.__setattr__(self, "hand_grave_draws", tuple(self.hand_grave_draws))
        object.__setattr__(self, "hand_add_supports", tuple(self.hand_add_supports))
        if any(not isinstance(value, Plan1StageCardDraw) for value in self.card_draws):
            raise TypeError("card_draws must contain Plan1StageCardDraw values")
        if any(
            not isinstance(value, Plan1StageCardCreate)
            for value in self.card_creates
        ):
            raise TypeError("card_creates must contain Plan1StageCardCreate values")
        for values, label in (
            (self.timer_installs, "timer_installs"),
            (self.timers_before, "timers_before"),
        ):
            if any(not isinstance(value, Plan1EffectTimerInstance) for value in values):
                raise TypeError(
                    f"{label} must contain Plan1EffectTimerInstance values"
                )
        if any(
            not isinstance(value, Plan1StageHandGraveDraw)
            for value in self.hand_grave_draws
        ):
            raise TypeError(
                "hand_grave_draws must contain Plan1StageHandGraveDraw values"
            )
        if any(
            not isinstance(value, Plan1StageHandAddSupport)
            for value in self.hand_add_supports
        ):
            raise TypeError(
                "hand_add_supports must contain Plan1StageHandAddSupport values"
            )
        for name in (
            "hand_add_used_support_ids_before",
            "hand_add_used_support_ids_after",
        ):
            values = tuple(getattr(self, name))
            if any(not isinstance(value, str) or not value for value in values):
                raise ValueError(f"{name} must contain non-empty support ids")
            if len(set(values)) != len(values):
                raise ValueError(f"{name} must contain unique support ids")
            object.__setattr__(self, name, values)
        if not set(self.hand_add_used_support_ids_before) <= set(
            self.hand_add_used_support_ids_after
        ):
            raise ValueError("HandAdd support ids cannot be removed by an action")
        if (
            isinstance(self.timer_install_ordinal_before, bool)
            or not isinstance(self.timer_install_ordinal_before, int)
            or self.timer_install_ordinal_before < 0
        ):
            raise ValueError(
                "timer_install_ordinal_before must be a non-negative integer"
            )
        expected_ordinals = tuple(
            range(
                self.timer_install_ordinal_before,
                self.timer_install_ordinal_before + len(self.timer_installs),
            )
        )
        if tuple(value.install_ordinal for value in self.timer_installs) != expected_ordinals:
            raise ValueError("timer installs must have contiguous installation order")
        for value, label in (
            (self.zones_before, "zones_before"),
            (self.zones_after, "zones_after"),
        ):
            if value is not None and not isinstance(value, NativeOrderedZoneState):
                raise TypeError(f"{label} must be NativeOrderedZoneState or None")
        if self.zones_after is not None and self.zones_before is None:
            raise ValueError("zones_after requires zones_before")
        if self.settlement not in ("", "grave", "lost"):
            raise ValueError("settlement must be empty, grave, or lost")
        for value, label in (
            (self.equipped_item_runtime_before, "equipped_item_runtime_before"),
            (self.equipped_item_runtime_after, "equipped_item_runtime_after"),
        ):
            if value is not None and not isinstance(value, Plan1EquippedItemRuntime):
                raise TypeError(f"{label} must be Plan1EquippedItemRuntime or None")
        if (self.equipped_item_runtime_before is None) != (
            self.equipped_item_runtime_after is None
        ):
            raise ValueError("equipped item before/after runtime must appear together")
        if self.equipped_item_fire is not None:
            if not isinstance(self.equipped_item_fire, Plan1StageEquippedItemFire):
                raise TypeError(
                    "equipped_item_fire must be Plan1StageEquippedItemFire or None"
                )
            if (
                self.equipped_item_fire.runtime_before
                != self.equipped_item_runtime_before
                or self.equipped_item_fire.runtime_after
                != self.equipped_item_runtime_after
            ):
                raise ValueError("item fire runtimes do not match action runtimes")
        elif self.equipped_item_runtime_before != self.equipped_item_runtime_after:
            raise ValueError("item runtime changed without an item fire")

    @property
    def guid(self) -> str:
        return self.card.guid

    @property
    def legal(self) -> bool:
        return (
            not self.blockers
            and self.transition is not None
            and self.transition.status is Plan1TransitionStatus.APPLIED
            and self.zones_before is not None
            and self.zones_after is not None
            and bool(self.settlement)
        )

    @property
    def score_gain(self) -> int:
        if self.transition is None:
            return 0
        return self.transition.after.score - self.transition.before.score


@dataclass(frozen=True, slots=True)
class Plan1StageStep:
    """One selected card transition including native zone settlement."""

    action: Plan1StageAction
    transition: Plan1CardTransition
    before_zones: NativeOrderedZoneState
    after_zones: NativeOrderedZoneState
    settlement: str
    card_draws: tuple[Plan1StageCardDraw, ...] = ()
    card_creates: tuple[Plan1StageCardCreate, ...] = ()
    timer_installs: tuple[Plan1EffectTimerInstance, ...] = ()
    hand_grave_draws: tuple[Plan1StageHandGraveDraw, ...] = ()
    hand_add_supports: tuple[Plan1StageHandAddSupport, ...] = ()
    equipped_item_fire: Plan1StageEquippedItemFire | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "card_draws", tuple(self.card_draws))
        object.__setattr__(self, "card_creates", tuple(self.card_creates))
        object.__setattr__(self, "timer_installs", tuple(self.timer_installs))
        object.__setattr__(self, "hand_grave_draws", tuple(self.hand_grave_draws))
        object.__setattr__(self, "hand_add_supports", tuple(self.hand_add_supports))
        if any(not isinstance(value, Plan1StageCardDraw) for value in self.card_draws):
            raise TypeError("card_draws must contain Plan1StageCardDraw values")
        if any(
            not isinstance(value, Plan1StageCardCreate)
            for value in self.card_creates
        ):
            raise TypeError("card_creates must contain Plan1StageCardCreate values")
        if any(
            not isinstance(value, Plan1EffectTimerInstance)
            for value in self.timer_installs
        ):
            raise TypeError(
                "timer_installs must contain Plan1EffectTimerInstance values"
            )
        if any(
            not isinstance(value, Plan1StageHandGraveDraw)
            for value in self.hand_grave_draws
        ):
            raise TypeError(
                "hand_grave_draws must contain Plan1StageHandGraveDraw values"
            )
        if any(
            not isinstance(value, Plan1StageHandAddSupport)
            for value in self.hand_add_supports
        ):
            raise TypeError(
                "hand_add_supports must contain Plan1StageHandAddSupport values"
            )
        if self.equipped_item_fire is not None and not isinstance(
            self.equipped_item_fire, Plan1StageEquippedItemFire
        ):
            raise TypeError(
                "equipped_item_fire must be Plan1StageEquippedItemFire or None"
            )

    @property
    def applied(self) -> bool:
        return self.transition.applied and not self.action.blockers


@dataclass(frozen=True, slots=True)
class Plan1NativeStageState:
    """Immutable scalar + ordered-zone checkpoint for one Plan 1 turn."""

    scalar: Plan1ScalarState
    zones: NativeOrderedZoneState
    steps: tuple[Plan1StageStep, ...] = ()
    blockers: tuple[Plan1Blocker, ...] = ()
    card_create_bindings: tuple[Plan1CardCreateGuidBinding, ...] = ()
    effect_timers: tuple[Plan1EffectTimerInstance, ...] = ()
    timer_fires: tuple[Plan1StageTimerFire, ...] = ()
    next_timer_install_ordinal: int = 0
    hand_add_used_support_ids: tuple[str, ...] = ()
    equipped_item_runtime: Plan1EquippedItemRuntime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scalar, Plan1ScalarState):
            raise TypeError("scalar must be Plan1ScalarState")
        if not isinstance(self.zones, NativeOrderedZoneState):
            raise TypeError("zones must be NativeOrderedZoneState")
        object.__setattr__(self, "steps", tuple(self.steps))
        object.__setattr__(self, "blockers", tuple(self.blockers))
        bindings = tuple(self.card_create_bindings)
        if any(not isinstance(value, Plan1CardCreateGuidBinding) for value in bindings):
            raise TypeError(
                "card_create_bindings must contain Plan1CardCreateGuidBinding values"
            )
        object.__setattr__(self, "card_create_bindings", bindings)
        timers = tuple(self.effect_timers)
        fires = tuple(self.timer_fires)
        if any(not isinstance(value, Plan1EffectTimerInstance) for value in timers):
            raise TypeError(
                "effect_timers must contain Plan1EffectTimerInstance values"
            )
        if any(not isinstance(value, Plan1StageTimerFire) for value in fires):
            raise TypeError("timer_fires must contain Plan1StageTimerFire values")
        if len({value.identity for value in timers}) != len(timers):
            raise ValueError("active Timer identities must be unique")
        if tuple(value.install_ordinal for value in timers) != tuple(
            sorted(value.install_ordinal for value in timers)
        ):
            raise ValueError("active Timers must preserve installation order")
        if (
            isinstance(self.next_timer_install_ordinal, bool)
            or not isinstance(self.next_timer_install_ordinal, int)
            or self.next_timer_install_ordinal < 0
        ):
            raise ValueError(
                "next_timer_install_ordinal must be a non-negative integer"
            )
        observed_ordinals = tuple(
            value.install_ordinal for value in (*timers, *(fire.timer for fire in fires))
        )
        if observed_ordinals and self.next_timer_install_ordinal <= max(observed_ordinals):
            raise ValueError("next Timer ordinal must follow every observed install")
        object.__setattr__(self, "effect_timers", timers)
        object.__setattr__(self, "timer_fires", fires)
        used_support_ids = tuple(self.hand_add_used_support_ids)
        if any(
            not isinstance(value, str) or not value
            for value in used_support_ids
        ):
            raise ValueError(
                "hand_add_used_support_ids must contain non-empty support ids"
            )
        if len(set(used_support_ids)) != len(used_support_ids):
            raise ValueError("hand_add_used_support_ids must contain unique ids")
        object.__setattr__(
            self, "hand_add_used_support_ids", used_support_ids
        )
        if self.equipped_item_runtime is not None and not isinstance(
            self.equipped_item_runtime, Plan1EquippedItemRuntime
        ):
            raise TypeError(
                "equipped_item_runtime must be Plan1EquippedItemRuntime or None"
            )

    @property
    def current_turn(self) -> int:
        return self.scalar.turn

    @property
    def terminal(self) -> bool:
        return bool(self.blockers)


# Shorter names are useful for callers that do not need the native qualifier.
Plan1StageState = Plan1NativeStageState


@dataclass(frozen=True, slots=True)
class Plan1NativeStageRun:
    """Result of executing a finite sequence of deterministic turn plans."""

    initial: Plan1NativeStageState
    final: Plan1NativeStageState
    boundaries: tuple[Plan1StageTurnBoundary, ...]
    completed: bool

    @property
    def steps(self) -> tuple[Plan1StageStep, ...]:
        return self.final.steps

    @property
    def blockers(self) -> tuple[Plan1Blocker, ...]:
        return self.final.blockers


def _program_index(
    compilation: Plan1DeckCompilation | Sequence[Plan1CompiledCard],
) -> dict[tuple[str, int, str | None], Plan1CompiledCard]:
    programs = compilation.programs if isinstance(compilation, Plan1DeckCompilation) else compilation
    result: dict[tuple[str, int, str | None], Plan1CompiledCard] = {}
    for program in programs:
        key = (program.card_id, program.upgrade, program.instance_guid)
        if key in result and result[key] != program:
            raise ValueError("incompatible Plan1 card programs require distinct native GUID bindings")
        result[key] = program
    return result


def _equipped_item_stage_blockers(
    state: Plan1NativeStageState,
    compilation: Plan1DeckCompilation | Sequence[Plan1CompiledCard],
) -> tuple[Plan1Blocker, ...]:
    runtime = state.equipped_item_runtime
    blockers: list[Plan1Blocker] = []
    required: Plan1EquippedItemRuntime | None = None
    if isinstance(compilation, Plan1DeckCompilation) and (
        compilation.manifest.before_produce_item.item_id
        == FKTN_SSR_PLAN1_BEFORE_ITEM_ID
    ):
        required = compilation.equipped_item_runtime
        if required is None:
            blockers.append(
                _blocker(
                    "plan1-equipped-item-runtime-missing",
                    FKTN_SSR_PLAN1_BEFORE_ITEM_ID,
                    "compilation",
                )
            )
        else:
            blockers.extend(
                validate_fktn_ssr_plan1_equipped_item_runtime(required)
            )
        if runtime is None:
            blockers.append(
                _blocker(
                    "plan1-equipped-item-runtime-missing",
                    FKTN_SSR_PLAN1_BEFORE_ITEM_ID,
                    "stage",
                )
            )
    if runtime is not None:
        blockers.extend(validate_fktn_ssr_plan1_equipped_item_runtime(runtime))
        if required is not None and (
            runtime.contract_identity != required.contract_identity
        ):
            blockers.append(
                _blocker(
                    "plan1-equipped-item-runtime-binding-mismatch",
                    FKTN_SSR_PLAN1_BEFORE_ITEM_ID,
                )
            )
    return tuple(dict.fromkeys(blockers))


def _plan3_placement_card(value: NativeOrderedCardInstance) -> Plan3NativeCard:
    """Project only identity/runtime facts consumed by CardCreate placement."""

    return Plan3NativeCard(
        guid=value.guid,
        card_id=value.card_id,
        base_upgrade=value.base_upgrade,
        temporary_upgrade=value.temporary_upgrade,
        effective_upgrade=value.effective_upgrade,
        support_upgrade_ids=value.support_upgrade_ids,
        fixed_deck_order=value.fixed_deck_order,
        play_count=value.runtime_state.play_count,
    )


def _plan3_placement_state(value: NativeOrderedZoneState) -> Plan3NativeState:
    """Build the shared executor's ordinary-zone projection.

    ``pending_played`` is intentionally absent from Plan3NativeState, matching
    the native CardCreateId command's ordinary-zone inputs.  GUID collisions
    are checked against the full Plan1 universe before the executor is called.
    """

    hand = tuple(_plan3_placement_card(card) for card in value.hand)
    return Plan3NativeState(
        hand=hand,
        deck=tuple(_plan3_placement_card(card) for card in value.deck),
        grave=tuple(_plan3_placement_card(card) for card in value.grave),
        lost=tuple(_plan3_placement_card(card) for card in value.lost),
        random_state=value.random_state,
        turn_used_support_ids=tuple(
            dict.fromkeys(
                support_id
                for card in hand
                for support_id in card.support_upgrade_ids
            )
        ),
    )


def _plan3_card_upgrade_state(
    value: NativeOrderedZoneState,
    *,
    database,
) -> Plan3NativeState:
    """Project exact Hand runtime; other zones are identity-only non-targets."""

    hand = tuple(
        Plan3NativeCard.from_native_ordered(card, database=database)
        for card in value.hand
    )
    return Plan3NativeState(
        hand=hand,
        deck=tuple(_plan3_placement_card(card) for card in value.deck),
        grave=tuple(_plan3_placement_card(card) for card in value.grave),
        lost=tuple(_plan3_placement_card(card) for card in value.lost),
        random_state=value.random_state,
        turn_used_support_ids=tuple(
            dict.fromkeys(
                support_id
                for card in hand
                for support_id in card.support_upgrade_ids
            )
        ),
    )


def _plan3_card_move_state(value: NativeOrderedZoneState) -> Plan3NativeState:
    """Project a pending Plan1 play into the shared CardMove executor.

    The shared native primitive represents the currently played card inside
    Hand solely so it can exclude that GUID from searches.  Plan1 keeps the
    same instance in ``pending_played`` until final settlement, so the lift
    below removes it from the returned Hand again.
    """

    pending = value.pending_played
    if pending is None:
        raise NativeOrderedZoneError(
            "no-pending-played-card",
            "CardMove requires an active played card",
        )
    hand = (
        *tuple(_plan3_placement_card(card) for card in value.hand),
        _plan3_placement_card(pending),
    )
    return Plan3NativeState(
        hand=hand,
        deck=tuple(_plan3_placement_card(card) for card in value.deck),
        grave=tuple(_plan3_placement_card(card) for card in value.grave),
        lost=tuple(_plan3_placement_card(card) for card in value.lost),
        random_state=value.random_state,
        turn_used_support_ids=tuple(
            dict.fromkeys(
                support_id
                for card in hand
                for support_id in card.support_upgrade_ids
            )
        ),
    )


def _lift_card_move_result(
    before: NativeOrderedZoneState,
    after: Plan3NativeState,
) -> NativeOrderedZoneState:
    """Lift a scalar-neutral shared CardMove result back into Plan1 zones."""

    pending = before.pending_played
    if pending is None:
        raise NativeOrderedZoneError(
            "no-pending-played-card",
            "CardMove result has no pending played card",
        )
    if after.hold:
        raise NativeOrderedZoneError(
            "card-move-hold-unexpected",
            "the integrated Lost destination cannot populate Hold",
        )
    existing = {card.guid: card for card in before.all_positioned_cards}
    after_guids = tuple(card.guid for card in after.all_cards)
    if set(after_guids) != set(existing) or len(after_guids) != len(existing):
        raise NativeOrderedZoneError(
            "card-move-conservation-failed",
            "shared CardMove changed the Plan1 GUID universe",
        )
    if tuple(card.guid for card in after.hand).count(pending.guid) != 1:
        raise NativeOrderedZoneError(
            "pending-played-card-mismatch",
            pending.guid,
        )

    def zone(
        values: Sequence[Plan3NativeCard],
        *,
        omit_pending: bool = False,
    ) -> tuple[NativeOrderedCardInstance, ...]:
        return tuple(
            existing[card.guid]
            for card in values
            if not omit_pending or card.guid != pending.guid
        )

    return replace(
        before,
        random_state=after.random_state,
        hand=zone(after.hand, omit_pending=True),
        deck=zone(after.deck),
        grave=zone(after.grave),
        lost=zone(after.lost),
    )


def _new_ordered_card(value: Plan3NativeCard) -> NativeOrderedCardInstance:
    """Lift the shared executor's exact new-card defaults into ordered zones."""

    return NativeOrderedCardInstance.from_local_save(
        LocalSaveExamCard(
            zone_order=0,
            guid=value.guid,
            card_id=value.card_id,
            base_upgrade=value.base_upgrade,
            temporary_upgrade=value.temporary_upgrade,
            effective_upgrade=value.effective_upgrade,
            support_upgrade_ids=value.support_upgrade_ids,
            fixed_deck_order=value.fixed_deck_order,
            runtime_state=empty_local_save_exam_card_runtime_state(),
        )
    )


def _lift_card_create_result(
    before: NativeOrderedZoneState,
    after: Plan3NativeState,
    allocated_guids: Sequence[str],
) -> NativeOrderedZoneState:
    """Merge CardCreate output while preserving every existing runtime."""

    existing = {card.guid: card for card in before.all_positioned_cards}
    created_by_guid: dict[str, NativeOrderedCardInstance] = {}
    plan3_by_guid = {
        card.guid: card
        for card in (*after.hand, *after.deck, *after.grave, *after.lost)
    }
    for guid in allocated_guids:
        created_by_guid[guid] = _new_ordered_card(plan3_by_guid[guid])
    projected = {**existing, **created_by_guid}

    def zone(values: Sequence[Plan3NativeCard]) -> tuple[NativeOrderedCardInstance, ...]:
        return tuple(projected[card.guid] for card in values)

    universe = tuple(
        sorted(
            (*before.card_universe, *created_by_guid.values()),
            key=lambda card: card.guid,
        )
    )
    return replace(
        before,
        random_state=after.random_state,
        card_universe=universe,
        hand=zone(after.hand),
        deck=zone(after.deck),
        grave=zone(after.grave),
        lost=zone(after.lost),
    )


def _lift_card_upgrade_result(
    before: NativeOrderedZoneState,
    after: Plan3NativeState,
) -> NativeOrderedZoneState:
    """Lift only temporary/effective upgrade deltas by stable GUID."""

    current = {card.guid: card for card in before.card_universe}
    after_cards = tuple(after.all_cards)
    after_guids = {card.guid for card in after_cards}
    positioned_before = {
        card.guid
        for card in (*before.hand, *before.deck, *before.grave, *before.lost)
    }
    if after_guids != positioned_before:
        raise NativeOrderedZoneError(
            "card-upgrade-conservation-failed",
            "CardUpgrade changed the GUID universe",
        )
    projected = dict(current)
    for card in after_cards:
        updated = current[card.guid].set_temporary_upgrade(
            card.temporary_upgrade
        )
        if updated.effective_upgrade != card.effective_upgrade:
            raise NativeOrderedZoneError(
                "card-upgrade-lineage-mismatch",
                card.guid,
            )
        projected[card.guid] = updated

    def zone(values: Sequence[Plan3NativeCard]):
        return tuple(projected[card.guid] for card in values)

    return replace(
        before,
        card_universe=tuple(projected[card.guid] for card in before.card_universe),
        hand=zone(after.hand),
        deck=zone(after.deck),
        grave=zone(after.grave),
        lost=zone(after.lost),
    )
def _resolve_plan1_stage_card_draw(
    scalar_before: Plan1ScalarState,
    zones_before: NativeOrderedZoneState,
    effect: Plan1CompiledEffect,
    effect_index: int,
    settings: Plan1NativeSettings,
) -> tuple[Plan1CardDrawResolution, Plan1StageCardDraw | None]:
    """Resolve one native CardDraw command against immutable ordered zones."""

    if effect.effect_type != EFFECT_CARD_DRAW:
        blocker = _blocker(
            "plan1-card-draw-effect-mismatch",
            effect.effect_id,
            effect.effect_type,
        )
        return (
            Plan1CardDrawResolution(
                scalar_before, scalar_before, max(effect.value1, 0), 0, (blocker,)
            ),
            None,
        )
    if len(zones_before.hand) > settings.hand_limit:
        blocker = _blocker(
            "plan1-card-draw-hand-limit-invariant",
            effect.effect_id,
            f"hand={len(zones_before.hand)};limit={settings.hand_limit}",
        )
        return (
            Plan1CardDrawResolution(
                scalar_before, scalar_before, effect.value1, 0, (blocker,)
            ),
            None,
        )

    actual_count = min(
        effect.value1,
        len(zones_before.deck) + len(zones_before.grave),
        settings.hand_limit - len(zones_before.hand),
    )
    try:
        native_transition = zones_before.draw_to_hand(actual_count)
        scalar_after = replace(
            scalar_before,
            total_effect_draw_card_count=(
                scalar_before.total_effect_draw_card_count + actual_count
            ),
        )
        resolution = Plan1CardDrawResolution(
            scalar_before,
            scalar_after,
            effect.value1,
            actual_count,
        )
        draw = Plan1StageCardDraw(
            effect_id=effect.effect_id,
            effect_index=effect_index,
            requested_count=effect.value1,
            actual_count=actual_count,
            before_zones=zones_before,
            native_transition=native_transition,
        )
    except (NativeOrderedZoneError, TypeError, ValueError, OverflowError) as error:
        blocker = _blocker(
            "plan1-card-draw-failed",
            effect.effect_id,
            str(error),
        )
        return (
            Plan1CardDrawResolution(
                scalar_before, scalar_before, effect.value1, 0, (blocker,)
            ),
            None,
        )
    return resolution, draw


def _resolve_plan1_stage_card_upgrade(
    scalar_before: Plan1ScalarState,
    zones_before: NativeOrderedZoneState,
    effect: Plan1CompiledEffect,
) -> tuple[Plan1CardUpgradeResolution, NativeOrderedZoneState | None]:
    contract = effect.card_upgrade_contract
    if effect.effect_type != EFFECT_CARD_UPGRADE or contract is None:
        blocker = _blocker(
            "plan1-card-upgrade-contract-mismatch",
            effect.effect_id,
        )
        return (
            Plan1CardUpgradeResolution(
                scalar_before, scalar_before, 0, 0, (blocker,)
            ),
            None,
        )
    try:
        native = execute_plan3_card_upgrade(
            _plan3_card_upgrade_state(
                zones_before,
                database=contract.database,
            ),
            contract,
        )
        if not native.applied:
            assert native.pause is not None
            raise ValueError(
                f"{native.pause.code}:{native.pause.detail}".rstrip(":")
            )
        zones_after = _lift_card_upgrade_result(
            zones_before,
            native.state_after,
        )
        return (
            Plan1CardUpgradeResolution(
                scalar_before,
                scalar_before,
                len(native.candidate_guids),
                len(native.selected_guids),
            ),
            zones_after,
        )
    except (KeyError, NativeOrderedZoneError, TypeError, ValueError) as error:
        blocker = _blocker(
            "plan1-card-upgrade-failed",
            effect.effect_id,
            str(error),
        )
        return (
            Plan1CardUpgradeResolution(
                scalar_before, scalar_before, 0, 0, (blocker,)
            ),
            None,
        )


def _move_plan1_hand_snapshot_to_grave(
    zones: NativeOrderedZoneState,
) -> NativeOrderedZoneState:
    """Apply native Hand snapshot settlement without touching pending/Lost."""

    settled = tuple(card.reset_support_upgrade() for card in zones.hand)
    replacements = {card.guid: card for card in settled}
    universe = tuple(
        replacements.get(card.guid, card) for card in zones.card_universe
    )
    return replace(
        zones,
        card_universe=universe,
        hand=(),
        grave=(*zones.grave, *settled),
    )


def _native_hand_add_card(
    card: NativeOrderedCardInstance,
) -> NativeHandAddCard:
    return NativeHandAddCard(
        guid=card.guid,
        card_id=card.card_id,
        base_upgrade=card.base_upgrade,
        effective_upgrade=card.effective_upgrade,
    )


def _resolve_plan1_hand_add_support(
    zones_before: NativeOrderedZoneState,
    *,
    effect_id: str,
    effect_index: int,
    drawn_guids: Sequence[str],
    used_support_ids: tuple[str, ...],
    resolver: Plan1HandAddSupportResolver | None,
) -> tuple[
    NativeOrderedZoneState | None,
    tuple[str, ...],
    Plan1StageHandAddSupport | None,
    Plan1Blocker | None,
]:
    """Run and validate the native HandAdd callback after one exact draw."""

    if resolver is None:
        return (
            None,
            used_support_ids,
            None,
            _blocker(
                "plan1-hand-add-support-runtime-required",
                effect_id,
                "supply lesson/support runtime for native HandAdd",
            ),
        )
    by_guid = {card.guid: card for card in zones_before.hand}
    try:
        drawn_cards = tuple(by_guid[guid] for guid in drawn_guids)
    except KeyError as error:
        return (
            None,
            used_support_ids,
            None,
            _blocker(
                "plan1-hand-add-support-drawn-card-missing",
                effect_id,
                str(error.args[0]),
            ),
        )
    request = Plan1HandAddSupportRequest(
        effect_id=effect_id,
        effect_index=effect_index,
        drawn_cards=tuple(_native_hand_add_card(card) for card in drawn_cards),
        random_state=zones_before.random_state,
        used_support_ids=used_support_ids,
    )
    try:
        result = resolver(request)
        if not isinstance(result, NativeHandAddSupportResult):
            raise TypeError(
                "HandAdd resolver must return NativeHandAddSupportResult"
            )
        result_cards = tuple(result.cards)
        if any(
            not isinstance(value, NativeHandAddCardResult)
            for value in result_cards
        ):
            raise TypeError(
                "HandAdd result cards must contain NativeHandAddCardResult values"
            )
        result_used = tuple(result.used_support_ids)
        if any(not isinstance(value, str) or not value for value in result_used):
            raise ValueError("HandAdd used support ids must be non-empty text")
        if len(set(result_used)) != len(result_used):
            raise ValueError("HandAdd used support ids must be unique")
        if not set(used_support_ids) <= set(result_used):
            raise ValueError("HandAdd result dropped an already-used support id")
        expected_identity = tuple(
            (card.guid, card.card_id, card.base_upgrade, card.effective_upgrade)
            for card in drawn_cards
        )
        returned_identity = tuple(
            (
                card.guid,
                card.card_id,
                card.base_upgrade,
                card.initial_effective_upgrade,
            )
            for card in result_cards
        )
        if returned_identity != expected_identity:
            raise ValueError("HandAdd result changed drawn card identity/order")
        if result.initial_random_state != request.random_state:
            raise ValueError("HandAdd result starts at the wrong RNG state")

        working = zones_before
        for previous, card_result in zip(drawn_cards, result_cards, strict=True):
            added_ids = tuple(card_result.added_support_ids)
            if any(not isinstance(value, str) or not value for value in added_ids):
                raise ValueError("HandAdd added support ids must be non-empty text")
            if len(set(added_ids)) != len(added_ids):
                raise ValueError("HandAdd added support ids must be unique")
            if not set(added_ids) <= set(result_used):
                raise ValueError("HandAdd installed an unconsumed support id")
            updated = previous.install_support_upgrades(added_ids)
            if updated.effective_upgrade != card_result.final_effective_upgrade:
                raise ValueError("HandAdd final effective upgrade is inconsistent")
            working = working.replace_hand_runtime(previous, updated)
        working = replace(working, random_state=result.final_random_state)
        trace = Plan1StageHandAddSupport(
            request=request,
            result=result,
            before_zones=zones_before,
            after_zones=working,
        )
    except NativeHandAddSupportError as error:
        return (
            None,
            used_support_ids,
            None,
            _blocker(
                "plan1-hand-add-support-runtime-failed",
                effect_id,
                f"{error.code}:{error.detail}",
            ),
        )
    except (NativeOrderedZoneError, TypeError, ValueError, OverflowError) as error:
        return (
            None,
            used_support_ids,
            None,
            _blocker(
                "plan1-hand-add-support-result-invalid",
                effect_id,
                str(error),
            ),
        )
    return working, result_used, trace, None


def _resolve_plan1_stage_hand_grave_draw(scalar_before, zones_before, effect, effect_index, settings, hand_add_support_resolver, used_support_ids):
    """Shared Hand snapshot -> Grave -> draw -> HandAdd owner for cards and drinks."""
    contract = effect.hand_grave_draw_contract
    if effect.effect_type != EFFECT_HAND_GRAVE_COUNT_CARD_DRAW or contract is None:
        blocker = _blocker('plan1-hand-grave-draw-contract-mismatch', effect.effect_id)
        return (Plan1HandGraveDrawResolution(scalar_before, scalar_before, 0, 0, (blocker,)), None)
    try:
        contract.assert_exact_master_shape()
    except (TypeError, ValueError) as error:
        blocker = _blocker('plan1-hand-grave-draw-contract-mismatch', effect.effect_id, str(error))
        return (Plan1HandGraveDrawResolution(scalar_before, scalar_before, 0, 0, (blocker,)), None)
    hand_snapshot = tuple((card.guid for card in zones_before.hand))
    requested_count = len(hand_snapshot)
    if requested_count > settings.hand_limit:
        blocker = _blocker('plan1-hand-grave-draw-hand-limit-invariant', effect.effect_id, f'hand={requested_count};limit={settings.hand_limit}')
        return (Plan1HandGraveDrawResolution(scalar_before, scalar_before, requested_count, 0, (blocker,)), None)
    try:
        after_hand_to_grave = _move_plan1_hand_snapshot_to_grave(zones_before)
        actual_count = min(requested_count, len(after_hand_to_grave.deck) + len(after_hand_to_grave.grave), settings.hand_limit - len(after_hand_to_grave.hand))
        native_draw = after_hand_to_grave.draw_to_hand(actual_count)
    except (NativeOrderedZoneError, TypeError, ValueError, OverflowError) as error:
        blocker = _blocker('plan1-hand-grave-draw-failed', effect.effect_id, str(error))
        return (Plan1HandGraveDrawResolution(scalar_before, scalar_before, requested_count, 0, (blocker,)), None)
    if actual_count != requested_count:
        blocker = _blocker('plan1-hand-grave-draw-count-mismatch', effect.effect_id, f'requested={requested_count};actual={actual_count}')
        return (Plan1HandGraveDrawResolution(scalar_before, scalar_before, requested_count, actual_count, (blocker,)), None)
    supported_zones, next_used_support_ids, support_trace, support_blocker = _resolve_plan1_hand_add_support(native_draw.state, effect_id=effect.effect_id, effect_index=effect_index, drawn_guids=native_draw.drawn_guids, used_support_ids=used_support_ids, resolver=hand_add_support_resolver)
    if support_blocker is not None:
        return (Plan1HandGraveDrawResolution(scalar_before, scalar_before, requested_count, actual_count, (support_blocker,)), None)
    assert supported_zones is not None
    assert support_trace is not None
    try:
        scalar_after = replace(scalar_before, total_effect_draw_card_count=scalar_before.total_effect_draw_card_count + actual_count)
        resolution = Plan1HandGraveDrawResolution(scalar_before, scalar_after, requested_count, actual_count)
        trace = Plan1StageHandGraveDraw(effect_id=effect.effect_id, effect_index=effect_index, before_zones=zones_before, hand_snapshot_guids=hand_snapshot, after_hand_to_grave=after_hand_to_grave, native_draw=native_draw, hand_add=support_trace)
    except (NativeOrderedZoneError, TypeError, ValueError, OverflowError) as error:
        blocker = _blocker('plan1-hand-grave-draw-result-invalid', effect.effect_id, str(error))
        return (Plan1HandGraveDrawResolution(scalar_before, scalar_before, requested_count, actual_count, (blocker,)), None)
    return (resolution, trace)


def _preview_plan1_stage_action(
    state: Plan1NativeStageState,
    hand_index: int,
    program: Plan1CompiledCard,
    settings: Plan1NativeSettings,
    hand_add_support_resolver: Plan1HandAddSupportResolver | None,
    parameter_buff_status_change_effects: Sequence[Plan1CompiledEffect] = (),
) -> tuple[
    Plan1CardTransition | None,
    NativeOrderedZoneState | None,
    str,
    tuple[Plan1StageCardDraw, ...],
    tuple[Plan1StageCardCreate, ...],
    tuple[Plan1EffectTimerInstance, ...],
    tuple[Plan1StageHandGraveDraw, ...],
    tuple[Plan1StageHandAddSupport, ...],
    tuple[str, ...],
    Plan1EquippedItemRuntime | None,
    Plan1StageEquippedItemFire | None,
    tuple[Plan1Blocker, ...],
]:
    """Stage cost/effects/zones/Timer installs without committing state."""

    try:
        staged_zones, selected = state.zones.remove_hand(hand_index)
    except (NativeOrderedZoneError, ValueError, IndexError) as error:
        blocker = _blocker(
            "plan1-zone-play-start-failed",
            program.card_id,
            str(error),
        )
        return (
            None,
            None,
            "",
            (),
            (),
            (),
            (),
            (),
            state.hand_add_used_support_ids,
            state.equipped_item_runtime,
            None,
            (blocker,),
        )

    card_draws: list[Plan1StageCardDraw] = []
    card_creates: list[Plan1StageCardCreate] = []
    timer_installs: list[Plan1EffectTimerInstance] = []
    hand_grave_draws: list[Plan1StageHandGraveDraw] = []
    hand_add_supports: list[Plan1StageHandAddSupport] = []
    staged_used_support_ids = state.hand_add_used_support_ids
    item_runtime_before = state.equipped_item_runtime
    item_runtime_after = item_runtime_before
    item_effects: tuple[Plan1CompiledEffect, ...] = ()
    if (
        item_runtime_before is not None
        and item_runtime_before.remaining_uses > 0
        and selected.card_id == item_runtime_before.target_card_id
    ):
        item_effects = item_runtime_before.effects
        item_runtime_after = item_runtime_before.consume()
    # A supplied runtime provider is authoritative for every effect draw,
    # including ordinary CardDraw rows.  The historical no-provider path
    # remains unchanged: only Hand/Grave-count draws demand the old explicit
    # runtime blocker.
    requires_hand_add_support = hand_add_support_resolver is not None or any(
        effect.effect_type == EFFECT_HAND_GRAVE_COUNT_CARD_DRAW
        for effect in program.effects
    )

    def resolve_card_draw(
        scalar_before: Plan1ScalarState,
        effect: Plan1CompiledEffect,
        effect_index: int,
    ) -> Plan1CardDrawResolution:
        nonlocal staged_zones, staged_used_support_ids
        resolution, draw = _resolve_plan1_stage_card_draw(
            scalar_before,
            staged_zones,
            effect,
            effect_index,
            settings,
        )
        if draw is not None:
            next_zones = draw.after_zones
            if requires_hand_add_support:
                (
                    supported_zones,
                    next_used_support_ids,
                    support_trace,
                    support_blocker,
                ) = _resolve_plan1_hand_add_support(
                    next_zones,
                    effect_id=effect.effect_id,
                    effect_index=effect_index,
                    drawn_guids=draw.drawn_guids,
                    used_support_ids=staged_used_support_ids,
                    resolver=hand_add_support_resolver,
                )
                if support_blocker is not None:
                    return Plan1CardDrawResolution(
                        scalar_before,
                        scalar_before,
                        effect.value1,
                        draw.actual_count,
                        (support_blocker,),
                    )
                assert supported_zones is not None
                assert support_trace is not None
                next_zones = supported_zones
                staged_used_support_ids = next_used_support_ids
                hand_add_supports.append(support_trace)
            staged_zones = next_zones
            card_draws.append(draw)
        return resolution

    def resolve_card_upgrade(
        scalar_before: Plan1ScalarState,
        effect: Plan1CompiledEffect,
        effect_index: int,
    ) -> Plan1CardUpgradeResolution:
        nonlocal staged_zones
        resolution, zones_after = _resolve_plan1_stage_card_upgrade(
            scalar_before,
            staged_zones,
            effect,
        )
        if zones_after is not None:
            staged_zones = zones_after
        return resolution

    def resolve_hand_grave_draw(scalar_before, effect, effect_index):
        nonlocal staged_zones, staged_used_support_ids
        resolution, trace = _resolve_plan1_stage_hand_grave_draw(
            scalar_before, staged_zones, effect, effect_index, settings,
            hand_add_support_resolver, staged_used_support_ids)
        if trace is not None:
            staged_zones = trace.hand_add.after_zones
            staged_used_support_ids = trace.hand_add.result.used_support_ids
            hand_grave_draws.append(trace)
            hand_add_supports.append(trace.hand_add)
        return resolution

    def resolve_card_move(
        scalar_before: Plan1ScalarState,
        effect: Plan1CompiledEffect,
        effect_index: int,
    ) -> Plan1CardMoveResolution:
        nonlocal staged_zones
        contract = effect.card_move_contract
        search = effect.card_move_search
        if (
            effect.effect_type != EFFECT_CARD_MOVE
            or contract is None
            or search is None
        ):
            blocker = _blocker(
                "plan1-card-move-contract-mismatch",
                effect.effect_id,
            )
            return Plan1CardMoveResolution(
                scalar_before,
                scalar_before,
                0,
                0,
                (blocker,),
            )

        zones_before = staged_zones
        try:
            native = apply_plan2_card_move_lost_random_direct(
                _plan3_card_move_state(zones_before),
                contract,
                playing_guid=selected.guid,
                search=search,
            )
            candidate_count = len(native.standalone.candidates)
            moved_count = len(native.standalone.selected)
            expected_moved_count = min(candidate_count, 1)
            if moved_count != expected_moved_count:
                raise NativeOrderedZoneError(
                    "card-move-random-selection-count-mismatch",
                    f"candidates={candidate_count};selected={moved_count};"
                    f"expected={expected_moved_count}",
                )
            zones_after = _lift_card_move_result(
                zones_before,
                native.after,
            )
        except (TypeError, ValueError) as error:
            blocker = _blocker(
                "plan1-card-move-failed",
                effect.effect_id,
                str(error),
            )
            return Plan1CardMoveResolution(
                scalar_before,
                scalar_before,
                0,
                0,
                (blocker,),
            )

        staged_zones = zones_after
        return Plan1CardMoveResolution(
            scalar_before,
            scalar_before,
            candidate_count,
            moved_count,
        )

    def resolve_card_create(
        scalar_before: Plan1ScalarState,
        effect,
        effect_index: int,
    ) -> Plan1CardCreateResolution:
        nonlocal staged_zones
        if effect.effect_type != EFFECT_CARD_CREATE_ID:
            blocker = _blocker(
                "plan1-card-create-effect-mismatch",
                effect.effect_id,
                effect.effect_type,
            )
            return Plan1CardCreateResolution(
                scalar_before,
                scalar_before,
                effect.pick_count_min,
                0,
                (blocker,),
            )
        match = CARD_CREATE_ID_PATTERN.fullmatch(effect.effect_id)
        observed = (
            effect.target_card_id,
            effect.target_upgrade,
            "deck_random",
            effect.pick_count_min,
            effect.pick_count_max,
        )
        expected = (
            None
            if match is None
            else (
                match.group(1),
                int(match.group(2)),
                match.group(3),
                int(match.group(4)),
                int(match.group(5)),
            )
        )
        if (
            match is None
            or observed != expected
            or effect.move_position_type
            != "ProduceCardMovePositionType_DeckRandom"
            or effect.pick_count_min <= 0
            or effect.pick_count_min != effect.pick_count_max
        ):
            blocker = _blocker(
                "plan1-card-create-contract-mismatch",
                effect.effect_id,
            )
            return Plan1CardCreateResolution(
                scalar_before,
                scalar_before,
                max(effect.pick_count_min, 0),
                0,
                (blocker,),
            )

        invocation_key = (
            selected.guid,
            selected.runtime_state.play_count,
            effect.effect_id,
            effect_index,
        )
        matches = tuple(
            value
            for value in state.card_create_bindings
            if value.invocation_key == invocation_key
        )
        if len(matches) != 1:
            blocker = _blocker(
                (
                    "plan1-card-create-guid-binding-missing"
                    if not matches
                    else "plan1-card-create-guid-binding-ambiguous"
                ),
                effect.effect_id,
                f"source={selected.guid};play_count={selected.runtime_state.play_count};"
                f"effect_index={effect_index}",
            )
            return Plan1CardCreateResolution(
                scalar_before,
                scalar_before,
                effect.pick_count_min,
                0,
                (blocker,),
            )
        binding = matches[0]
        collisions = sorted(
            set(binding.guid_tokens)
            & {card.guid for card in staged_zones.card_universe}
        )
        if collisions:
            blocker = _blocker(
                "plan1-card-create-guid-collision",
                effect.effect_id,
                collisions[0],
            )
            return Plan1CardCreateResolution(
                scalar_before,
                scalar_before,
                effect.pick_count_min,
                0,
                (blocker,),
            )

        zones_before = staged_zones
        try:
            contract = CardCreateContract(
                effect_id=effect.effect_id,
                card_id=effect.target_card_id,
                upgrade=effect.target_upgrade,
                destination="deck_random",
                count_min=effect.pick_count_min,
                count_max=effect.pick_count_max,
                raw_move_position_type=effect.move_position_type,
            )
            native_result = apply_card_create_id(
                _plan3_placement_state(zones_before),
                contract,
                guid_tokens=binding.guid_tokens,
            )
            if native_result.unresolved:
                required = ",".join(native_result.branch.required_fields)
                blocker = _blocker(
                    "plan1-card-create-native-unresolved",
                    effect.effect_id,
                    required,
                )
                return Plan1CardCreateResolution(
                    scalar_before,
                    scalar_before,
                    effect.pick_count_min,
                    0,
                    (blocker,),
                )
            zones_after = _lift_card_create_result(
                zones_before,
                native_result.after,
                native_result.trace.allocated_guids,
            )
            created = Plan1StageCardCreate(
                effect_id=effect.effect_id,
                effect_index=effect_index,
                source_guid=selected.guid,
                source_play_count=selected.runtime_state.play_count,
                before_zones=zones_before,
                after_zones=zones_after,
                native_trace=native_result.trace,
            )
        except (CardCreateError, NativeOrderedZoneError, TypeError, ValueError) as error:
            blocker = _blocker(
                "plan1-card-create-failed",
                effect.effect_id,
                str(error),
            )
            return Plan1CardCreateResolution(
                scalar_before,
                scalar_before,
                effect.pick_count_min,
                0,
                (blocker,),
            )

        staged_zones = zones_after
        card_creates.append(created)
        return Plan1CardCreateResolution(
            scalar_before,
            scalar_before,
            effect.pick_count_min,
            len(native_result.trace.allocated_guids),
        )

    def resolve_effect_timer(
        scalar_before: Plan1ScalarState,
        effect: Plan1CompiledEffect,
        effect_index: int,
    ) -> Plan1EffectTimerResolution:
        child = effect.timer_child
        if (
            effect.effect_type != EFFECT_TIMER
            or child is None
            or child.effect_type not in PLAN1_TIMER_CHILD_EFFECT_TYPES
            or child.effect_type == EFFECT_TIMER
            or not child.executable
        ):
            blocker = _blocker(
                "plan1-effect-timer-contract-mismatch",
                effect.effect_id,
            )
            return Plan1EffectTimerResolution(
                scalar_before,
                scalar_before,
                max(effect.value1, 1),
                (blocker,),
            )
        timer_installs.append(
            Plan1EffectTimerInstance(
                timer_effect=effect,
                source_guid=selected.guid,
                source_play_count=selected.runtime_state.play_count,
                effect_index=effect_index,
                install_ordinal=(
                    state.next_timer_install_ordinal + len(timer_installs)
                ),
            )
        )
        return Plan1EffectTimerResolution(
            scalar_before,
            scalar_before,
            effect.value1,
        )

    transition = execute_plan1_card(
        state.scalar,
        program,
        settings=settings,
        card_draw_resolver=resolve_card_draw,
        card_upgrade_resolver=resolve_card_upgrade,
        hand_grave_draw_resolver=resolve_hand_grave_draw,
        card_move_resolver=resolve_card_move,
        card_create_resolver=resolve_card_create,
        effect_timer_resolver=resolve_effect_timer,
        before_produce_item_effects=item_effects,
        parameter_buff_status_change_effects=(
            parameter_buff_status_change_effects
        ),
    )
    if not transition.applied:
        return (
            transition,
            None,
            "",
            (),
            (),
            (),
            (),
            (),
            state.hand_add_used_support_ids,
            item_runtime_before,
            None,
            transition.blockers,
        )

    item_fire: Plan1StageEquippedItemFire | None = None
    if item_effects:
        item_trace = transition.trace[1 : 1 + len(item_effects)]
        assert item_runtime_before is not None
        assert item_runtime_after is not None
        item_fire = Plan1StageEquippedItemFire(
            runtime_before=item_runtime_before,
            runtime_after=item_runtime_after,
            target_guid=selected.guid,
            scalar_before=transition.trace[0].after,
            scalar_after=item_trace[-1].after,
            trace=item_trace,
        )

    try:
        pending = staged_zones.pending_played
        if pending is None or pending.guid != selected.guid:
            raise NativeOrderedZoneError(
                "pending-played-card-mismatch",
                selected.guid,
            )
        # Native ``MovePlayCard`` updates the per-card runtime counter twice
        # for one manually issued play (the two writes are visible in the
        # v4 LocalSave snapshot), while the scalar exam play count is advanced
        # once by ``execute_plan1_card``.  Keep that distinction here so a
        # later replayed copy of the same card starts from the native count.
        incremented = pending.increment_play_count().increment_play_count()
        staged_zones = staged_zones.replace_pending_runtime(pending, incremented)
        if program.move_position_type == "ProduceCardMovePositionType_Grave":
            staged_zones = staged_zones.append_played_to_grave(incremented)
            settlement = "grave"
        elif program.move_position_type == "ProduceCardMovePositionType_Lost":
            staged_zones = staged_zones.append_played_to_lost(incremented)
            settlement = "lost"
        else:
            raise NativeOrderedZoneError(
                "plan1-card-move-position-unsupported",
                program.move_position_type,
            )
    except (NativeOrderedZoneError, ValueError, IndexError) as error:
        blocker = _blocker(
            "plan1-zone-settlement-failed",
            selected.guid,
            str(error),
        )
        return (
            transition,
            None,
            "",
            (),
            (),
            (),
            (),
            (),
            state.hand_add_used_support_ids,
            item_runtime_before,
            None,
            (blocker,),
        )

    return (
        transition,
        staged_zones,
        settlement,
        tuple(card_draws),
        tuple(card_creates),
        tuple(timer_installs),
        tuple(hand_grave_draws),
        tuple(hand_add_supports),
        staged_used_support_ids,
        item_runtime_after,
        item_fire,
        (),
    )


def enumerate_plan1_legal_actions(
    state: Plan1NativeStageState,
    compilation: Plan1DeckCompilation | Sequence[Plan1CompiledCard],
    *,
    settings: Plan1NativeSettings | None = None,
    hand_add_support_resolver: Plan1HandAddSupportResolver | None = None,
) -> tuple[Plan1StageAction, ...]:
    """Preview every current-Hand card against the native card core.

    The returned tuple preserves native Hand order.  Entries with an unknown
    card program or a core blocker remain visible as typed non-legal actions;
    deterministic advisors can then filter by :attr:`Plan1StageAction.legal`.
    """

    if not isinstance(state, Plan1NativeStageState):
        raise TypeError("state must be Plan1NativeStageState")
    if settings is None:
        settings = load_plan1_native_settings()
    programs = _program_index(compilation)
    item_stage_blockers = _equipped_item_stage_blockers(state, compilation)
    actions: list[Plan1StageAction] = []
    for index, instance in enumerate(state.zones.hand):
        program = programs.get((instance.card_id, instance.effective_upgrade, instance.guid))
        if program is None:
            program = programs.get((instance.card_id, instance.effective_upgrade, None))
        if item_stage_blockers:
            actions.append(
                Plan1StageAction(
                    index,
                    instance,
                    program,
                    None,
                    item_stage_blockers,
                )
            )
            continue
        if program is None:
            actions.append(
                Plan1StageAction(
                    index,
                    instance,
                    None,
                    None,
                    (
                        _blocker(
                            "plan1-card-program-missing",
                            instance.card_id,
                            f"upgrade={instance.effective_upgrade}",
                        ),
                    ),
                )
            )
            continue
        (
            transition,
            zones_after,
            settlement,
            card_draws,
            card_creates,
            timer_installs,
            hand_grave_draws,
            hand_add_supports,
            hand_add_used_support_ids_after,
            equipped_item_runtime_after,
            equipped_item_fire,
            blockers,
        ) = _preview_plan1_stage_action(
            state,
            index,
            program,
            settings,
            hand_add_support_resolver,
        )
        actions.append(
            Plan1StageAction(
                index,
                instance,
                program,
                transition,
                blockers,
                zones_before=state.zones,
                zones_after=zones_after,
                settlement=settlement,
                card_draws=card_draws,
                card_creates=card_creates,
                timer_installs=timer_installs,
                timers_before=state.effect_timers,
                timer_install_ordinal_before=state.next_timer_install_ordinal,
                hand_grave_draws=hand_grave_draws,
                hand_add_supports=hand_add_supports,
                hand_add_used_support_ids_before=(
                    state.hand_add_used_support_ids
                ),
                hand_add_used_support_ids_after=(
                    hand_add_used_support_ids_after
                ),
                equipped_item_runtime_before=state.equipped_item_runtime,
                equipped_item_runtime_after=equipped_item_runtime_after,
                equipped_item_fire=equipped_item_fire,
            )
        )
    return tuple(actions)


def preview_plan1_stage_action(
    state: Plan1NativeStageState,
    hand_index: int,
    program: Plan1CompiledCard | None,
    *,
    settings: Plan1NativeSettings | None = None,
    hand_add_support_resolver: Plan1HandAddSupportResolver | None = None,
    parameter_buff_status_change_effects: Sequence[Plan1CompiledEffect] = (),
) -> Plan1StageAction:
    """Preview exactly one selected Hand card.

    ``enumerate_plan1_legal_actions`` is intentionally convenient for policy
    search, but a replay already supplies the selected slot.  This public
    single-card entry point keeps the same preview implementation and avoids
    evaluating unrelated Hand candidates.  It is still only a preview;
    callers must pass the returned action to :func:`apply_plan1_stage_action`.
    """

    if not isinstance(state, Plan1NativeStageState):
        raise TypeError("state must be Plan1NativeStageState")
    if isinstance(hand_index, bool) or not isinstance(hand_index, int):
        raise TypeError("hand_index must be an integer")
    if hand_index < 0 or hand_index >= len(state.zones.hand):
        raise IndexError("Hand index is out of range")
    if settings is None:
        settings = load_plan1_native_settings()
    card = state.zones.hand[hand_index]
    item_stage_blockers = _equipped_item_stage_blockers(state, ())
    if item_stage_blockers:
        return Plan1StageAction(
            hand_index,
            card,
            program,
            None,
            item_stage_blockers,
            timers_before=state.effect_timers,
            timer_install_ordinal_before=state.next_timer_install_ordinal,
            hand_add_used_support_ids_before=state.hand_add_used_support_ids,
            hand_add_used_support_ids_after=state.hand_add_used_support_ids,
            equipped_item_runtime_before=state.equipped_item_runtime,
            equipped_item_runtime_after=state.equipped_item_runtime,
        )
    if program is None:
        return Plan1StageAction(
            hand_index,
            card,
            None,
            None,
            (
                _blocker(
                    "plan1-card-program-missing",
                    card.card_id,
                    f"upgrade={card.effective_upgrade}",
                ),
            ),
            timers_before=state.effect_timers,
            timer_install_ordinal_before=state.next_timer_install_ordinal,
            hand_add_used_support_ids_before=state.hand_add_used_support_ids,
            hand_add_used_support_ids_after=state.hand_add_used_support_ids,
            equipped_item_runtime_before=state.equipped_item_runtime,
            equipped_item_runtime_after=state.equipped_item_runtime,
        )
    if not isinstance(program, Plan1CompiledCard):
        raise TypeError("program must be Plan1CompiledCard or None")
    (
        transition,
        zones_after,
        settlement,
        card_draws,
        card_creates,
        timer_installs,
        hand_grave_draws,
        hand_add_supports,
        hand_add_used_support_ids_after,
        equipped_item_runtime_after,
        equipped_item_fire,
        blockers,
    ) = _preview_plan1_stage_action(
        state,
        hand_index,
        program,
        settings,
        hand_add_support_resolver,
        parameter_buff_status_change_effects,
    )
    return Plan1StageAction(
        hand_index,
        card,
        program,
        transition,
        blockers,
        zones_before=state.zones,
        zones_after=zones_after,
        settlement=settlement,
        card_draws=card_draws,
        card_creates=card_creates,
        timer_installs=timer_installs,
        timers_before=state.effect_timers,
        timer_install_ordinal_before=state.next_timer_install_ordinal,
        hand_grave_draws=hand_grave_draws,
        hand_add_supports=hand_add_supports,
        hand_add_used_support_ids_before=state.hand_add_used_support_ids,
        hand_add_used_support_ids_after=hand_add_used_support_ids_after,
        equipped_item_runtime_before=state.equipped_item_runtime,
        equipped_item_runtime_after=equipped_item_runtime_after,
        equipped_item_fire=equipped_item_fire,
    )


def _append_blocker(
    state: Plan1NativeStageState, blocker: Plan1Blocker
) -> Plan1NativeStageState:
    return replace(state, blockers=(*state.blockers, blocker))


def apply_plan1_stage_action(
    state: Plan1NativeStageState,
    action: Plan1StageAction,
) -> tuple[Plan1NativeStageState, Plan1StageStep | None]:
    """Apply a previously enumerated action and settle it via native zones."""

    if not isinstance(state, Plan1NativeStageState):
        raise TypeError("state must be Plan1NativeStageState")
    if not isinstance(action, Plan1StageAction):
        raise TypeError("action must be Plan1StageAction")
    if action.hand_index >= len(state.zones.hand):
        return (
            _append_blocker(
                state,
                _blocker("plan1-hand-index-invalid", action.guid),
            ),
            None,
        )
    current = state.zones.hand[action.hand_index]
    if current.guid != action.guid:
        return (
            _append_blocker(
                state,
                _blocker(
                    "plan1-hand-action-stale",
                    action.guid,
                    f"current={current.guid}",
                ),
            ),
            None,
        )
    if not action.legal or action.program is None or action.transition is None:
        blockers = action.blockers or (
            _blocker("plan1-action-illegal", action.guid),
        )
        return replace(state, blockers=(*state.blockers, *blockers)), None

    transition = action.transition
    assert action.zones_before is not None
    assert action.zones_after is not None
    if (
        action.zones_before != state.zones
        or transition.before != state.scalar
        or action.timers_before != state.effect_timers
        or action.timer_install_ordinal_before
        != state.next_timer_install_ordinal
        or action.hand_add_used_support_ids_before
        != state.hand_add_used_support_ids
        or action.equipped_item_runtime_before
        != state.equipped_item_runtime
    ):
        return (
            _append_blocker(
                state,
                _blocker(
                    "plan1-stage-action-stale",
                    action.guid,
                    "scalar, ordered zones, or equipped-item runtime changed after preview",
                ),
            ),
            None,
        )

    before_zones = action.zones_before
    after_zones = action.zones_after
    step = Plan1StageStep(
        action=action,
        transition=transition,
        before_zones=before_zones,
        after_zones=after_zones,
        settlement=action.settlement,
        card_draws=action.card_draws,
        card_creates=action.card_creates,
        timer_installs=action.timer_installs,
        hand_grave_draws=action.hand_grave_draws,
        hand_add_supports=action.hand_add_supports,
        equipped_item_fire=action.equipped_item_fire,
    )
    next_state = replace(
        state,
        scalar=transition.after,
        zones=after_zones,
        steps=(*state.steps, step),
        effect_timers=(*state.effect_timers, *action.timer_installs),
        next_timer_install_ordinal=(
            state.next_timer_install_ordinal + len(action.timer_installs)
        ),
        hand_add_used_support_ids=(
            action.hand_add_used_support_ids_after
        ),
        equipped_item_runtime=action.equipped_item_runtime_after,
    )
    return next_state, step


def _replay_plan1_effect_timers(
    state: Plan1NativeStageState,
    scalar_after_start_play: Plan1ScalarState,
    zones_after_start_play: NativeOrderedZoneState,
    settings: Plan1NativeSettings,
    hand_add_support_resolver: Plan1HandAddSupportResolver | None = None,
    used_support_ids: tuple[str, ...] = (),
) -> tuple[
    Plan1ScalarState,
    NativeOrderedZoneState,
    tuple[Plan1EffectTimerInstance, ...],
    tuple[Plan1StageTimerFire, ...],
    tuple[str, ...],
    tuple[Plan1Blocker, ...],
]:
    """Replay due children sequentially; caller commits only full success."""

    working_scalar = scalar_after_start_play
    working_zones = zones_after_start_play
    working_used_support_ids = tuple(used_support_ids)
    remaining: list[Plan1EffectTimerInstance] = []
    fires: list[Plan1StageTimerFire] = []

    for timer in state.effect_timers:
        next_elapsed = timer.elapsed_turns + 1
        if next_elapsed < timer.delay_turns:
            remaining.append(replace(timer, elapsed_turns=next_elapsed))
            continue

        child = timer.child_effect
        if (
            child.effect_type not in PLAN1_TIMER_CHILD_EFFECT_TYPES
            or child.effect_type == EFFECT_TIMER
            or child.activation_trigger_id
            or not child.executable
        ):
            return (
                scalar_after_start_play,
                zones_after_start_play,
                state.effect_timers,
                (),
                used_support_ids,
                (
                    _blocker(
                        "plan1-effect-timer-child-unsupported",
                        timer.timer_effect_id,
                        child.effect_id,
                    ),
                ),
            )

        before_scalar = working_scalar
        before_zones = working_zones
        child_draws: list[Plan1StageCardDraw] = []

        def resolve_timer_card_draw(
            scalar_before: Plan1ScalarState,
            effect: Plan1CompiledEffect,
            child_effect_index: int,
        ) -> Plan1CardDrawResolution:
            nonlocal working_zones, working_used_support_ids
            resolution, draw = _resolve_plan1_stage_card_draw(
                scalar_before,
                working_zones,
                effect,
                timer.effect_index,
                settings,
            )
            if draw is not None:
                working_zones = draw.after_zones
                if hand_add_support_resolver is not None:
                    (
                        supported_zones,
                        next_used_support_ids,
                        _support_trace,
                        support_blocker,
                    ) = _resolve_plan1_hand_add_support(
                        working_zones,
                        effect_id=effect.effect_id,
                        effect_index=child_effect_index,
                        drawn_guids=draw.drawn_guids,
                        used_support_ids=working_used_support_ids,
                        resolver=hand_add_support_resolver,
                    )
                    if support_blocker is not None:
                        return replace(resolution, blockers=(support_blocker,))
                    assert supported_zones is not None
                    working_zones = supported_zones
                    working_used_support_ids = next_used_support_ids
                child_draws.append(draw)
            return resolution

        def resolve_timer_card_upgrade(
            scalar_before: Plan1ScalarState,
            effect: Plan1CompiledEffect,
            child_effect_index: int,
        ) -> Plan1CardUpgradeResolution:
            nonlocal working_zones
            resolution, zones_after = _resolve_plan1_stage_card_upgrade(
                scalar_before,
                working_zones,
                effect,
            )
            if zones_after is not None:
                working_zones = zones_after
            return resolution

        execution = execute_plan1_effects(
            working_scalar,
            (child,),
            settings=settings,
            card_draw_resolver=resolve_timer_card_draw,
            card_upgrade_resolver=resolve_timer_card_upgrade,
        )
        if execution.blockers:
            return (
                scalar_after_start_play,
                zones_after_start_play,
                state.effect_timers,
                (),
                used_support_ids,
                tuple(dict.fromkeys(execution.blockers)),
            )
        working_scalar = execution.after
        fires.append(
            Plan1StageTimerFire(
                timer=timer,
                before_scalar=before_scalar,
                after_scalar=working_scalar,
                before_zones=before_zones,
                after_zones=working_zones,
                effect_trace=execution.trace,
                card_draws=tuple(child_draws),
            )
        )

    return (
        working_scalar,
        working_zones,
        tuple(remaining),
        tuple(fires),
        working_used_support_ids,
        (),
    )


def advance_plan1_stage_turn(
    state: Plan1NativeStageState,
    boundary: Plan1StageTurnBoundary,
    *,
    settings: Plan1NativeSettings | None = None,
    hand_add_support_resolver: Plan1HandAddSupportResolver | None = None,
) -> Plan1NativeStageState:
    """Close the current Hand, draw, and advance exactly one native turn.

    The boundary object is the explicit runtime evidence for values that this
    slice cannot derive (turn-start playable reset and end-turn Lost flags).
    When supplied, ``hand_add_support_resolver`` is called for the ordinary
    turn-start draw and shares the same RNG/used-support ledger as effect
    draws.
    """

    if not isinstance(state, Plan1NativeStageState):
        raise TypeError("state must be Plan1NativeStageState")
    if not isinstance(boundary, Plan1StageTurnBoundary):
        raise TypeError("boundary must be Plan1StageTurnBoundary")
    if state.zones.pending_played is not None:
        return _append_blocker(
            state,
            _blocker("plan1-pending-card-before-turn-boundary", state.zones.pending_played.guid),
        )
    dispositions = boundary.disposition_mapping()
    if state.zones.hand and not dispositions:
        return _append_blocker(
            state,
            _blocker(
                "plan1-end-turn-dispositions-required",
                "Hand",
                "native ResetHand classification was not supplied",
            ),
        )
    try:
        closed = state.zones.close_turn(dispositions)
    except (NativeOrderedZoneError, TypeError, ValueError) as error:
        return _append_blocker(
            state,
            _blocker("plan1-close-turn-failed", "Hand", str(error)),
        )
    try:
        advanced = advance_plan1_turn(state.scalar)
    except (TypeError, ValueError, OverflowError) as error:
        return _append_blocker(
            state,
            _blocker("plan1-turn-advance-failed", "turn", str(error)),
        )
    if boundary.plays_remaining is None:
        return _append_blocker(
            state,
            _blocker(
                "plan1-turn-start-playable-unresolved",
                "turn-start",
                "supply plays_remaining from native turn-start evidence",
            ),
        )
    next_scalar = replace(advanced, plays_remaining=boundary.plays_remaining)
    try:
        drawn = closed.draw_to_hand(boundary.draw_count)
    except (NativeOrderedZoneError, TypeError, ValueError) as error:
        return _append_blocker(
            state,
            _blocker("plan1-draw-failed", "Deck", str(error)),
        )
    # A provider supplied by the caller is shared with effect draws and uses
    # the same ordered-zone RNG chain.  Turn boundaries reset the native
    # per-round used-support tuple before the ordinary draw.
    next_zones = drawn.state
    # The native turn boundary starts a fresh per-turn support-use ledger
    # regardless of whether this caller supplied the optional HandAdd
    # resolver.  Carrying the old tuple when the resolver is absent makes a
    # later provider observe stale support consumption from the previous
    # turn.
    next_used_support_ids: tuple[str, ...] = ()
    if hand_add_support_resolver is not None and drawn.drawn_guids:
        (
            supported_zones,
            next_used_support_ids,
            _support_trace,
            support_blocker,
        ) = _resolve_plan1_hand_add_support(
            next_zones,
            effect_id="plan1-turn-start-draw",
            effect_index=0,
            drawn_guids=drawn.drawn_guids,
            used_support_ids=(),
            resolver=hand_add_support_resolver,
        )
        if support_blocker is not None:
            return replace(
                state,
                blockers=(*state.blockers, support_blocker),
            )
        assert supported_zones is not None
        next_zones = supported_zones
    # Android ordinary lifecycle order is fixed here: Draw has now completed;
    # the caller-observed ExamStartTurn and StartPlay lists must both be empty
    # before the later Timer command list is queried and executed.
    phase_facts = boundary.phase_facts
    start_effects = boundary.exam_start_turn_effects
    if start_effects:
        if phase_facts is None or tuple(
            effect.effect_id for effect in start_effects
        ) != phase_facts.exam_start_turn_effect_ids:
            return _append_blocker(
                state,
                _blocker(
                    "plan1-effect-timer-phase-facts-mismatch",
                    "turn-start",
                    "captured ExamStartTurn ids differ from compiled effects",
                ),
            )
        execution = execute_plan1_effects(
            next_scalar,
            start_effects,
            settings=settings,
        )
        if execution.blockers:
            return replace(
                state,
                blockers=(*state.blockers, *execution.blockers),
            )
        next_scalar = execution.after
    if phase_facts is not None and phase_facts.start_play_effect_ids:
        return _append_blocker(
            state,
            _blocker(
                "plan1-effect-timer-phase-effects-unsupported",
                "start-play",
                ",".join(phase_facts.start_play_effect_ids),
            ),
        )
    if not state.effect_timers:
        return replace(
            state,
            scalar=next_scalar,
            zones=next_zones,
            hand_add_used_support_ids=next_used_support_ids,
        )
    if phase_facts is None:
        return _append_blocker(
            state,
            _blocker(
                "plan1-effect-timer-phase-facts-required",
                "turn-start",
                "supply observed ExamStartTurn and StartPlay command ids",
            ),
        )
    if phase_facts.exam_start_turn_effect_ids != tuple(
        effect.effect_id for effect in start_effects
    ):
        detail = ",".join(phase_facts.exam_start_turn_effect_ids)
        return _append_blocker(
            state,
            _blocker(
                "plan1-effect-timer-phase-effects-unsupported",
                "turn-start",
                detail,
            ),
        )
    if settings is None:
        try:
            settings = load_plan1_native_settings()
        except (TypeError, ValueError, OSError) as error:
            return _append_blocker(
                state,
                _blocker(
                    "plan1-effect-timer-settings-unresolved",
                    "turn-start",
                    str(error),
                ),
            )
    (
        timer_scalar,
        timer_zones,
        remaining_timers,
        timer_fires,
        timer_used_support_ids,
        timer_blockers,
    ) = _replay_plan1_effect_timers(
        state,
        next_scalar,
        next_zones,
        settings,
        hand_add_support_resolver,
        next_used_support_ids,
    )
    if timer_blockers:
        return replace(
            state,
            blockers=(*state.blockers, *timer_blockers),
        )
    return replace(
        state,
        scalar=timer_scalar,
        zones=timer_zones,
        effect_timers=remaining_timers,
        timer_fires=(*state.timer_fires, *timer_fires),
        hand_add_used_support_ids=timer_used_support_ids,
    )


ActionChooser = Callable[
    [Plan1NativeStageState, tuple[Plan1StageAction, ...]], Plan1StageAction | None
]


def run_plan1_stage(
    initial: Plan1NativeStageState,
    compilation: Plan1DeckCompilation | Sequence[Plan1CompiledCard],
    boundaries: Sequence[Plan1StageTurnBoundary] = (),
    *,
    settings: Plan1NativeSettings | None = None,
    choose_action: ActionChooser | None = None,
    hand_add_support_resolver: Plan1HandAddSupportResolver | None = None,
) -> Plan1NativeStageRun:
    """Run one action per turn, plus each explicitly supplied boundary.

    Thus ``boundaries=(b0,)`` executes two consecutive turns.  The default
    chooser is stable Hand order; callers can inject the deterministic advisor
    from :mod:`gkms_tool.plan1_native_search` without changing mechanics.
    A supplied HandAdd resolver is threaded through both turn-start and
    effect draws; omitting it preserves the legacy no-runtime behavior.
    """

    if not isinstance(initial, Plan1NativeStageState):
        raise TypeError("initial must be Plan1NativeStageState")
    boundary_values = tuple(boundaries)
    if any(not isinstance(item, Plan1StageTurnBoundary) for item in boundary_values):
        raise TypeError("boundaries must contain Plan1StageTurnBoundary values")
    current = initial
    chooser = choose_action
    for boundary_index in range(len(boundary_values) + 1):
        actions = enumerate_plan1_legal_actions(
            current,
            compilation,
            settings=settings,
            hand_add_support_resolver=hand_add_support_resolver,
        )
        legal = tuple(action for action in actions if action.legal)
        selected = (
            chooser(current, legal) if chooser is not None else (legal[0] if legal else None)
        )
        if selected is None:
            current = _append_blocker(
                current,
                _blocker("plan1-no-legal-action", f"turn-{current.scalar.turn}"),
            )
            break
        if selected not in legal:
            current = _append_blocker(
                current,
                _blocker("plan1-advisor-action-not-legal", selected.guid),
            )
            break
        current, _step = apply_plan1_stage_action(current, selected)
        if current.blockers:
            break
        if boundary_index < len(boundary_values):
            current = advance_plan1_stage_turn(
                current,
                boundary_values[boundary_index],
                settings=settings,
                hand_add_support_resolver=hand_add_support_resolver,
            )
            if current.blockers:
                break
    return Plan1NativeStageRun(
        initial=initial,
        final=current,
        boundaries=boundary_values,
        completed=not current.blockers and len(current.steps) == len(boundary_values) + 1,
    )


__all__ = [
    "ActionChooser",
    "Plan1NativeStageRun",
    "Plan1NativeStageState",
    "Plan1CardCreateGuidBinding",
    "Plan1EffectTimerInstance",
    "Plan1HandAddSupportRequest",
    "Plan1HandAddSupportResolver",
    "Plan1StageAction",
    "Plan1StageCardCreate",
    "Plan1StageCardDraw",
    "Plan1StageEquippedItemFire",
    "Plan1StageHandAddSupport",
    "Plan1StageHandGraveDraw",
    "Plan1StageState",
    "Plan1StageStep",
    "Plan1StageTimerFire",
    "Plan1StageTurnBoundary",
    "Plan1TurnStartPhaseFacts",
    "advance_plan1_stage_turn",
    "apply_plan1_stage_action",
    "enumerate_plan1_legal_actions",
    "preview_plan1_stage_action",
    "run_plan1_stage",
]
