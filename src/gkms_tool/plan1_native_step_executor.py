"""Execute one selected Plan 1 card and, when needed, one native turn boundary.

The Plan 1 stage module already owns the card transaction and ordered-zone
primitives.  A replay action, however, already tells us which Hand slot was
selected; expanding every other Hand candidate is both wasteful and subtly
wrong when the captured runtime has a different candidate set.  This module
is the small replay-facing adapter around the stage's single-card preview and
apply entry points.

The adapter intentionally accepts only state-before facts.  A caller must
provide the native turn-start draw/play-count facts when the selected card
uses the last play.  The resulting object is a shadow simulation result:
``exact``, ``legal_actions_complete``, and ``promotion_allowed`` always remain
false.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
import sqlite3
from typing import Any, Callable

from .audition_native_ordered_zones import (
    NativeEndTurnDisposition,
    NativeOrderedCardInstance,
)
from .audition_native_support import (
    NativeHandAddCardResult,
    NativeHandAddSupportError,
    NativeHandAddSupportResult,
)
from .leaderboard_replay import LeaderboardReplayAction
from .master_db import DEFAULT_DATABASE
from .plan1_native_core import (
    EFFECT_LESSON_BUFF,
    EFFECT_PARAMETER_BUFF,
    Plan1CompiledCard,
    Plan1CompiledEffect,
    Plan1DeckCompilation,
    Plan1NativeSettings,
    Plan1ScalarState,
    compile_plan1_card,
    compile_plan1_effect,
    load_plan1_native_settings,
)
from .plan1_native_stage import (
    Plan1HandAddSupportRequest,
    Plan1HandAddSupportResolver,
    Plan1NativeStageState,
    Plan1StageAction,
    Plan1StageStep,
    Plan1StageTurnBoundary,
    Plan1TurnStartPhaseFacts,
    apply_plan1_stage_action,
    advance_plan1_stage_turn,
    preview_plan1_stage_action,
)


PARAMETER_BUFF_STATUS_CHANGE_TRIGGER_ID = (
    "e_trigger-exam_status_change-exam_parameter_buff"
)
PARAMETER_BUFF_STATUS_CHANGE_EFFECT_ID = "e_effect-exam_lesson_buff-0001"


class Plan1NativeStepExecutorError(ValueError):
    """A selected one-step Plan 1 boundary could not be executed."""

    def __init__(
        self,
        code: str,
        detail: str = "",
        *,
        blockers: Iterable[str] = (),
    ) -> None:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("step executor error code must be non-empty text")
        if not isinstance(detail, str):
            raise TypeError("step executor error detail must be text")
        values = tuple(blockers)
        if any(not isinstance(value, str) or not value for value in values):
            raise TypeError("step executor blockers must contain non-empty text")
        self.code = code
        self.detail = detail
        self.blockers = tuple(dict.fromkeys(values))
        super().__init__(code if not detail else f"{code}:{detail}")


@dataclass(frozen=True, slots=True)
class Plan1NativeStepBlocker:
    """One machine-readable reason a selected boundary was not published."""

    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("blocker code must be non-empty text")
        if not isinstance(self.detail, str):
            raise TypeError("blocker detail must be text")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Plan1NativeStepAction:
    """The one Hand card selected by a replay or caller policy."""

    hand_index: int
    card_guid: str
    card_id: str
    upgrade: int

    def __post_init__(self) -> None:
        if isinstance(self.hand_index, bool) or not isinstance(self.hand_index, int):
            raise TypeError("hand_index must be an integer")
        if self.hand_index < 0:
            raise ValueError("hand_index must be non-negative")
        for name in ("card_guid", "card_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty text")
        if isinstance(self.upgrade, bool) or not isinstance(self.upgrade, int):
            raise TypeError("upgrade must be an integer")
        if self.upgrade < 0:
            raise ValueError("upgrade must be non-negative")

    @property
    def guid(self) -> str:
        """Short alias used by the Plan 3 step executor API."""

        return self.card_guid

    @property
    def card_ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @classmethod
    def from_stage_action(cls, action: Plan1StageAction) -> "Plan1NativeStepAction":
        if not isinstance(action, Plan1StageAction):
            raise TypeError("action must be Plan1StageAction")
        return cls(
            action.hand_index,
            action.guid,
            action.card.card_id,
            action.card.effective_upgrade,
        )

    @classmethod
    def from_replay_action(
        cls,
        action: LeaderboardReplayAction,
        state: Plan1NativeStageState,
    ) -> "Plan1NativeStepAction":
        if not isinstance(action, LeaderboardReplayAction):
            raise TypeError("action must be LeaderboardReplayAction")
        if action.action_type != "use-hand" or len(action.indexes) != 1:
            raise ValueError("a Plan1 step action must be one use-hand index")
        if not isinstance(state, Plan1NativeStageState):
            raise TypeError("state must be Plan1NativeStageState")
        index = action.indexes[0]
        if index < 0 or index >= len(state.zones.hand):
            raise IndexError("replay Hand index is out of range")
        card = state.zones.hand[index]
        return cls(index, card.guid, card.card_id, card.effective_upgrade)

    @classmethod
    def from_value(
        cls,
        value: object,
        state: Plan1NativeStageState,
    ) -> "Plan1NativeStepAction":
        if isinstance(value, cls):
            return value
        if isinstance(value, Plan1StageAction):
            return cls.from_stage_action(value)
        if isinstance(value, LeaderboardReplayAction):
            return cls.from_replay_action(value, state)
        if not isinstance(value, Mapping):
            raise TypeError("Plan1 step action must be typed or a mapping")
        raw_index = value.get("hand_index", value.get("hand_slot"))
        raw_guid = value.get("card_guid", value.get("guid"))
        raw_id = value.get("card_id")
        raw_upgrade = value.get("upgrade", value.get("card_upgrade"))
        if isinstance(raw_index, bool) or not isinstance(raw_index, int):
            raise ValueError("action.hand_index must be an integer")
        if raw_index < 0 or raw_index >= len(state.zones.hand):
            raise IndexError("action Hand index is out of range")
        current = state.zones.hand[raw_index]
        return cls(
            raw_index,
            current.guid if raw_guid is None else str(raw_guid),
            current.card_id if raw_id is None else str(raw_id),
            current.effective_upgrade if raw_upgrade is None else raw_upgrade,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "hand_index": self.hand_index,
            "card_guid": self.card_guid,
            "card_id": self.card_id,
            "upgrade": self.upgrade,
        }


@dataclass(frozen=True, slots=True)
class Plan1NativeTurnBoundary:
    """Caller-observed values for the automatic EndTurn/TurnStart path.

    ``dispositions=None`` means derive ``isEndTurnLost`` from the supplied
    Master database for the Hand remaining after the selected card.  The
    draw count and the next playable count have no reliable value in a bare
    card checkpoint, so they remain explicit inputs; omission is a typed
    blocker rather than an invented N.I.A. default.
    """

    draw_count: int | None = None
    plays_remaining: int | None = None
    dispositions: Mapping[str, NativeEndTurnDisposition] | Sequence[
        tuple[str, NativeEndTurnDisposition]
    ] | None = None
    phase_facts: Plan1TurnStartPhaseFacts | None = None
    exam_start_turn_effects: tuple[Plan1CompiledEffect, ...] = ()
    remain_turn: int | None = None

    def __post_init__(self) -> None:
        for name in ("draw_count", "plays_remaining", "remain_turn"):
            value = getattr(self, name)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(f"{name} must be non-negative or None")
        if self.phase_facts is not None and not isinstance(
            self.phase_facts, Plan1TurnStartPhaseFacts
        ):
            raise TypeError("phase_facts must be Plan1TurnStartPhaseFacts or None")
        effects = tuple(self.exam_start_turn_effects)
        if any(not isinstance(value, Plan1CompiledEffect) for value in effects):
            raise TypeError(
                "exam_start_turn_effects must contain Plan1CompiledEffect values"
            )
        object.__setattr__(self, "exam_start_turn_effects", effects)
        raw = self.dispositions
        if raw is not None:
            values = tuple(raw.items()) if isinstance(raw, Mapping) else tuple(raw)
            seen: set[str] = set()
            canonical: list[tuple[str, NativeEndTurnDisposition]] = []
            for guid, disposition in values:
                if not isinstance(guid, str) or not guid.strip():
                    raise ValueError("disposition GUIDs must be non-empty text")
                if guid in seen:
                    raise ValueError(f"duplicate disposition GUID: {guid!r}")
                if not isinstance(disposition, NativeEndTurnDisposition):
                    raise TypeError(
                        "dispositions must contain NativeEndTurnDisposition values"
                    )
                seen.add(guid)
                canonical.append((guid, disposition))
            object.__setattr__(self, "dispositions", tuple(canonical))

    @classmethod
    def from_stage_boundary(
        cls,
        boundary: Plan1StageTurnBoundary,
        *,
        remain_turn: int | None = None,
    ) -> "Plan1NativeTurnBoundary":
        if not isinstance(boundary, Plan1StageTurnBoundary):
            raise TypeError("boundary must be Plan1StageTurnBoundary")
        return cls(
            draw_count=boundary.draw_count,
            plays_remaining=boundary.plays_remaining,
            dispositions=boundary.dispositions,
            phase_facts=boundary.phase_facts,
            exam_start_turn_effects=boundary.exam_start_turn_effects,
            remain_turn=remain_turn,
        )

    def disposition_mapping(self) -> dict[str, NativeEndTurnDisposition] | None:
        if self.dispositions is None:
            return None
        return dict(self.dispositions)

    def to_stage_boundary(
        self,
        dispositions: Mapping[str, NativeEndTurnDisposition] | None = None,
    ) -> Plan1StageTurnBoundary:
        if self.draw_count is None:
            raise ValueError("draw_count is unresolved")
        if self.plays_remaining is None:
            raise ValueError("plays_remaining is unresolved")
        selected = self.disposition_mapping() if dispositions is None else dict(dispositions)
        return Plan1StageTurnBoundary.from_mapping(
            draw_count=self.draw_count,
            plays_remaining=self.plays_remaining,
            dispositions=selected,
            phase_facts=self.phase_facts,
            exam_start_turn_effects=self.exam_start_turn_effects,
        )


# Descriptive aliases make the boundary easy to discover without duplicating
# the immutable value type.
Plan1NativeStepBoundary = Plan1NativeTurnBoundary
Plan1StepTurnBoundary = Plan1NativeTurnBoundary


@dataclass(frozen=True, slots=True)
class Plan1ParameterBuffStatusChangeListener:
    """Master-projected children fired when a ParameterBuff status changes."""

    trigger_id: str
    effects: tuple[Plan1CompiledEffect, ...]
    source_id: str = PARAMETER_BUFF_STATUS_CHANGE_TRIGGER_ID

    def __post_init__(self) -> None:
        if self.trigger_id != PARAMETER_BUFF_STATUS_CHANGE_TRIGGER_ID:
            raise ValueError("unsupported Plan1 ParameterBuff status-change trigger")
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise ValueError("source_id must be non-empty text")
        effects = tuple(self.effects)
        if not effects:
            raise ValueError("status-change listener requires at least one effect")
        if any(not isinstance(value, Plan1CompiledEffect) for value in effects):
            raise TypeError("listener effects must contain Plan1CompiledEffect values")
        if any(value.blockers for value in effects):
            raise ValueError("status-change listener effects must be executable")
        if any(value.effect_type != EFFECT_LESSON_BUFF for value in effects):
            raise ValueError("Plan1 ParameterBuff listener only supports LessonBuff children")
        object.__setattr__(self, "effects", effects)

    @classmethod
    def from_effects(
        cls,
        effects: Sequence[Plan1CompiledEffect],
        *,
        source_id: str = PARAMETER_BUFF_STATUS_CHANGE_TRIGGER_ID,
    ) -> "Plan1ParameterBuffStatusChangeListener":
        return cls(PARAMETER_BUFF_STATUS_CHANGE_TRIGGER_ID, tuple(effects), source_id)


def load_plan1_parameter_buff_status_change_listener(
    *,
    database: Path = DEFAULT_DATABASE,
    effect_ids: Sequence[str] = (PARAMETER_BUFF_STATUS_CHANGE_EFFECT_ID,),
) -> Plan1ParameterBuffStatusChangeListener:
    """Load the exact N.I.A. ParameterBuff listener children from Master."""

    values = tuple(effect_ids)
    if not values or any(not isinstance(value, str) or not value for value in values):
        raise ValueError("effect_ids must contain non-empty effect IDs")
    effects = tuple(
        compile_plan1_effect(value, database=Path(database)) for value in values
    )
    return Plan1ParameterBuffStatusChangeListener.from_effects(effects)


def _reassemble_support_result(
    drawn_cards: tuple[object, ...],
    eligible_cards: tuple[object, ...],
    native: NativeHandAddSupportResult,
    *,
    used_support_ids: tuple[str, ...],
) -> NativeHandAddSupportResult:
    """Put filtered support results back into original HandAdd order."""

    native_cards = tuple(native.cards)
    expected_identity = tuple(
        (card.guid, card.card_id, card.base_upgrade, card.effective_upgrade)
        for card in eligible_cards
    )
    returned_identity = tuple(
        (
            getattr(card, "guid", None),
            getattr(card, "card_id", None),
            getattr(card, "base_upgrade", None),
            getattr(card, "initial_effective_upgrade", None),
        )
        for card in native_cards
    )
    if returned_identity != expected_identity:
        raise ValueError("HandAdd resolver changed or omitted eligible card identity/order")
    eligible_by_guid = {value.guid: value for value in native_cards}
    cards: list[NativeHandAddCardResult] = []
    for card in drawn_cards:
        result = eligible_by_guid.get(card.guid)
        if result is None:
            result = NativeHandAddCardResult(
                guid=card.guid,
                card_id=card.card_id,
                base_upgrade=card.base_upgrade,
                initial_effective_upgrade=card.effective_upgrade,
                added_support_ids=(),
                final_effective_upgrade=card.effective_upgrade,
            )
        cards.append(result)
    trace_values: list[object] = []
    for event in native.audit_trace:
        card_order = next(
            (
                index
                for index, value in enumerate(drawn_cards)
                if value.guid == event.card_guid
            ),
            None,
        )
        if card_order is None:
            raise ValueError(
                f"HandAdd audit trace references unknown card GUID {event.card_guid!r}"
            )
        trace_values.append(replace(event, card_order=card_order))
    trace = tuple(trace_values)
    return NativeHandAddSupportResult(
        cards=tuple(cards),
        used_support_ids=native.used_support_ids,
        initial_random_state=native.initial_random_state,
        final_random_state=native.final_random_state,
        audit_trace=trace,
    )


def master_aware_plan1_hand_add_support_resolver(
    resolver: Plan1HandAddSupportResolver,
    *,
    database: Path = DEFAULT_DATABASE,
    master_card_refs: Sequence[tuple[str, int]] | None = None,
) -> Plan1HandAddSupportResolver:
    """Wrap a HandAdd resolver with the native Master ``+1`` gate.

    Native HandAdd first asks ``ExamCardData.IsUpgradable``.  Therefore a
    card is rolled only when its next effective-upgrade row exists in Master;
    a card that exists only at its current upgrade consumes no support RNG.
    The wrapped resolver still owns support probabilities, ordering, and the
    RNG chain.  This function merely filters the cards presented to it and
    restores identity-only results for the skipped cards.
    """

    if not callable(resolver):
        raise TypeError("resolver must be callable")
    known: frozenset[tuple[str, int]] | None = None
    if master_card_refs is not None:
        refs = tuple(master_card_refs)
        if refs != tuple(sorted(set(refs))):
            raise ValueError("master_card_refs must be sorted and unique")
        if any(
            not isinstance(card_id, str)
            or not card_id
            or isinstance(upgrade, bool)
            or not isinstance(upgrade, int)
            or upgrade < 0
            for card_id, upgrade in refs
        ):
            raise ValueError("master_card_refs contain an invalid card ref")
        known = frozenset(refs)
    database = Path(database)
    cache: dict[tuple[str, int], bool] = {}

    def has_master_ref(card_id: str, upgrade: int) -> bool:
        key = (card_id, upgrade)
        if known is not None:
            return key in known
        if key in cache:
            return cache[key]
        if not database.is_file():
            raise NativeHandAddSupportError(
                "hand-add-card-master-unavailable",
                str(database),
            )
        try:
            with sqlite3.connect(database) as connection:
                found = connection.execute(
                    "SELECT 1 FROM card WHERE id = ? AND upgrade_count = ? LIMIT 1",
                    key,
                ).fetchone()
        except sqlite3.Error as error:
            raise NativeHandAddSupportError(
                "hand-add-card-master-unavailable",
                f"{database}:{type(error).__name__}:{error}",
            ) from error
        cache[key] = found is not None
        return cache[key]

    def wrapped(request: Plan1HandAddSupportRequest) -> NativeHandAddSupportResult:
        if not isinstance(request, Plan1HandAddSupportRequest):
            raise NativeHandAddSupportError("support-request-type-invalid")
        drawn = tuple(request.drawn_cards)
        for card in drawn:
            if not has_master_ref(card.card_id, card.effective_upgrade):
                raise NativeHandAddSupportError(
                    "hand-add-card-master-missing",
                    f"{card.card_id}@{card.effective_upgrade}",
                )
        eligible = tuple(
            card
            for card in drawn
            if card.effective_upgrade < 3
            and has_master_ref(card.card_id, card.effective_upgrade + 1)
        )
        if not eligible:
            return NativeHandAddSupportResult(
                cards=tuple(
                    NativeHandAddCardResult(
                        guid=card.guid,
                        card_id=card.card_id,
                        base_upgrade=card.base_upgrade,
                        initial_effective_upgrade=card.effective_upgrade,
                        added_support_ids=(),
                        final_effective_upgrade=card.effective_upgrade,
                    )
                    for card in drawn
                ),
                used_support_ids=request.used_support_ids,
                initial_random_state=request.random_state,
                final_random_state=request.random_state,
            )
        filtered_request = replace(request, drawn_cards=eligible)
        try:
            native = resolver(filtered_request)
        except NativeHandAddSupportError:
            raise
        except (TypeError, ValueError, KeyError, RuntimeError) as error:
            raise NativeHandAddSupportError(
                "hand-add-support-resolver-failed",
                f"{type(error).__name__}:{error}",
            ) from error
        if not isinstance(native, NativeHandAddSupportResult):
            raise NativeHandAddSupportError(
                "support-result-type-invalid",
                type(native).__name__,
            )
        return _reassemble_support_result(
            drawn,
            eligible,
            native,
            used_support_ids=request.used_support_ids,
        )

    return wrapped


def _master_end_turn_dispositions(
    state: Plan1NativeStageState,
    *,
    database: Path,
) -> dict[str, NativeEndTurnDisposition]:
    """Read only ``isEndTurnLost`` for every remaining Hand card."""

    result: dict[str, NativeEndTurnDisposition] = {}
    for card in state.zones.hand:
        if not database.is_file():
            raise Plan1NativeStepExecutorError(
                "step-master-card-unavailable",
                str(database),
            )
        try:
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    "SELECT raw_json FROM card WHERE id = ? AND upgrade_count = ?",
                    (card.card_id, card.effective_upgrade),
                ).fetchone()
        except sqlite3.Error as error:
            raise Plan1NativeStepExecutorError(
                "step-master-card-unavailable",
                f"{database}:{type(error).__name__}:{error}",
            ) from error
        if row is None:
            raise Plan1NativeStepExecutorError(
                "step-master-card-missing",
                f"{card.card_id}@{card.effective_upgrade}",
            )
        import json

        try:
            raw = json.loads(row[0])
        except (TypeError, ValueError) as error:
            raise Plan1NativeStepExecutorError(
                "step-master-card-invalid",
                f"{card.card_id}@{card.effective_upgrade}:{error}",
            ) from error
        if not isinstance(raw, Mapping) or not isinstance(raw.get("isEndTurnLost"), bool):
            raise Plan1NativeStepExecutorError(
                "step-master-card-end-turn-lost-unresolved",
                f"{card.card_id}@{card.effective_upgrade}",
            )
        result[card.guid] = (
            NativeEndTurnDisposition.LOST
            if raw["isEndTurnLost"]
            else NativeEndTurnDisposition.GRAVE
        )
    return result


def _action_program(
    compilation: Plan1DeckCompilation | Sequence[Plan1CompiledCard] | None,
    action: Plan1NativeStepAction,
    *,
    database: Path,
) -> Plan1CompiledCard:
    if compilation is not None:
        programs = (
            compilation.programs
            if isinstance(compilation, Plan1DeckCompilation)
            else tuple(compilation)
        )
        matches = tuple(
            value
            for value in programs
            if isinstance(value, Plan1CompiledCard)
            and (value.card_id, value.upgrade) == (action.card_id, action.upgrade)
        )
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise Plan1NativeStepExecutorError(
                "step-card-program-ambiguous",
                f"{action.card_id}@{action.upgrade}",
            )
        raise Plan1NativeStepExecutorError(
            "step-card-program-missing",
            f"{action.card_id}@{action.upgrade}",
        )
    try:
        return compile_plan1_card(
            action.card_id,
            action.upgrade,
            database=database,
        )
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise Plan1NativeStepExecutorError(
            "step-card-program-compile-failed",
            f"{type(error).__name__}:{error}",
        ) from error


def _listener_effects(
    listener: Plan1ParameterBuffStatusChangeListener | Sequence[Plan1CompiledEffect] | None,
    explicit: Sequence[Plan1CompiledEffect],
) -> tuple[Plan1CompiledEffect, ...]:
    if listener is not None and explicit:
        raise Plan1NativeStepExecutorError(
            "step-parameter-buff-listener-ambiguous"
        )
    if isinstance(listener, Plan1ParameterBuffStatusChangeListener):
        return listener.effects
    if listener is not None:
        values = tuple(listener)
    else:
        values = tuple(explicit)
    if any(not isinstance(value, Plan1CompiledEffect) for value in values):
        raise Plan1NativeStepExecutorError(
            "step-parameter-buff-listener-invalid"
        )
    if any(value.blockers for value in values):
        raise Plan1NativeStepExecutorError(
            "step-parameter-buff-listener-blocked"
        )
    if any(
        value.effect_type not in {EFFECT_LESSON_BUFF, EFFECT_PARAMETER_BUFF}
        for value in values
    ):
        raise Plan1NativeStepExecutorError(
            "step-parameter-buff-listener-effect-unsupported"
        )
    return values


@dataclass(frozen=True, slots=True)
class Plan1NativeStepResult:
    """One selected card plus its optional automatic turn continuation."""

    before: Plan1NativeStageState
    after: Plan1NativeStageState
    action: Plan1NativeStepAction
    card_action: Plan1StageAction
    card_step: Plan1StageStep
    boundary: Plan1NativeTurnBoundary | None = None
    stage_boundary: Plan1StageTurnBoundary | None = None
    remain_turn: int | None = None
    blockers: tuple[Plan1NativeStepBlocker, ...] = ()

    def __post_init__(self) -> None:
        for value, name in (
            (self.before, "before"),
            (self.after, "after"),
        ):
            if not isinstance(value, Plan1NativeStageState):
                raise TypeError(f"{name} must be Plan1NativeStageState")
        if not isinstance(self.action, Plan1NativeStepAction):
            raise TypeError("action must be Plan1NativeStepAction")
        if not isinstance(self.card_action, Plan1StageAction):
            raise TypeError("card_action must be Plan1StageAction")
        if not isinstance(self.card_step, Plan1StageStep):
            raise TypeError("card_step must be Plan1StageStep")
        if self.card_step.action != self.card_action:
            raise ValueError("card_step does not match card_action")
        if self.card_step.before_zones != self.before.zones:
            raise ValueError("card_step does not start at before")
        if (
            self.stage_boundary is None
            and self.after.zones != self.card_step.after_zones
        ):
            raise ValueError("result without a boundary must end at card_step zones")
        if self.stage_boundary is not None and self.boundary is None:
            raise ValueError("stage_boundary requires boundary")
        if self.boundary is not None and self.stage_boundary is None:
            raise ValueError("boundary requires stage_boundary")
        if self.remain_turn is not None and (
            isinstance(self.remain_turn, bool)
            or not isinstance(self.remain_turn, int)
            or self.remain_turn < 0
        ):
            raise ValueError("remain_turn must be non-negative or None")
        blockers = tuple(self.blockers)
        if any(not isinstance(value, Plan1NativeStepBlocker) for value in blockers):
            raise TypeError("blockers must contain Plan1NativeStepBlocker values")
        object.__setattr__(self, "blockers", tuple(dict.fromkeys(blockers)))

    @property
    def transition(self):
        return self.card_step.transition

    @property
    def turn_start(self) -> Plan1NativeTurnBoundary | None:
        return self.boundary

    @property
    def boundary_step(self) -> Plan1StageTurnBoundary | None:
        return self.stage_boundary

    @property
    def boundary_applied(self) -> bool:
        return self.stage_boundary is not None

    @property
    def drawn_guids(self) -> tuple[str, ...]:
        """Return the next Hand in native ordered draw order when available."""

        if self.stage_boundary is None:
            return ()
        return tuple(card.guid for card in self.after.zones.hand)

    @property
    def exact(self) -> bool:
        return False

    @property
    def legal_actions_complete(self) -> bool:
        return False

    @property
    def promotion_allowed(self) -> bool:
        return False

    @property
    def steps(self) -> tuple[Plan1StageStep, ...]:
        return (self.card_step,)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "gkms.plan1-native-step-result.v1",
            "action": self.action.to_dict(),
            "before": {
                "turn": self.before.scalar.turn,
                "plays_remaining": self.before.scalar.plays_remaining,
                "score": self.before.scalar.score,
                "stamina": self.before.scalar.stamina,
                "block": self.before.scalar.block,
                "random_state": self.before.zones.random_state,
            },
            "after": {
                "turn": self.after.scalar.turn,
                "plays_remaining": self.after.scalar.plays_remaining,
                "score": self.after.scalar.score,
                "stamina": self.after.scalar.stamina,
                "block": self.after.scalar.block,
                "random_state": self.after.zones.random_state,
                "hand": [
                    {
                        "guid": card.guid,
                        "card_id": card.card_id,
                        "upgrade": card.effective_upgrade,
                    }
                    for card in self.after.zones.hand
                ],
            },
            "boundary": {
                "applied": self.boundary_applied,
                "draw_count": (
                    None if self.boundary is None else self.boundary.draw_count
                ),
                "plays_remaining": (
                    None
                    if self.boundary is None
                    else self.boundary.plays_remaining
                ),
                "remain_turn": self.remain_turn,
                "drawn_guids": list(self.drawn_guids),
            },
            "blockers": [value.to_dict() for value in self.blockers],
            "exact": False,
            "legal_actions_complete": False,
            "promotion_allowed": False,
        }


class Plan1NativeStepExecutor:
    """Pure executor for one selected Plan 1 card and optional turn boundary."""

    def __init__(
        self,
        *,
        database: Path = DEFAULT_DATABASE,
    ) -> None:
        self.database = Path(database)

    def execute(
        self,
        initial_state: Plan1NativeStageState,
        action: Plan1NativeStepAction | Plan1StageAction | LeaderboardReplayAction | Mapping[str, Any],
        *,
        compilation: Plan1DeckCompilation | Sequence[Plan1CompiledCard] | None = None,
        program: Plan1CompiledCard | None = None,
        settings: Plan1NativeSettings | None = None,
        turn_boundary: Plan1NativeTurnBoundary | Plan1StageTurnBoundary | None = None,
        boundary: Plan1NativeTurnBoundary | Plan1StageTurnBoundary | None = None,
        hand_add_support_resolver: Plan1HandAddSupportResolver | None = None,
        master_card_refs: Sequence[tuple[str, int]] | None = None,
        master_aware_support: bool = True,
        parameter_buff_listener: Plan1ParameterBuffStatusChangeListener
        | Sequence[Plan1CompiledEffect]
        | None = None,
        parameter_buff_status_change_effects: Sequence[Plan1CompiledEffect] = (),
        auto_end_turn: bool = True,
        database: Path | None = None,
    ) -> Plan1NativeStepResult:
        """Execute only ``action`` from ``initial_state``.

        When the card consumes the final local play and ``auto_end_turn`` is
        true, ``turn_boundary`` supplies the native TurnStart draw and reset
        facts.  It is not a post-state argument and is never inferred from a
        captured ``state_after``.
        """

        if not isinstance(initial_state, Plan1NativeStageState):
            raise TypeError("initial_state must be Plan1NativeStageState")
        if initial_state.blockers:
            raise Plan1NativeStepExecutorError(
                "step-initial-state-blocked",
                ",".join(value.code for value in initial_state.blockers),
            )
        if initial_state.zones.pending_played is not None:
            raise Plan1NativeStepExecutorError("step-initial-state-pending-card")
        try:
            selected = Plan1NativeStepAction.from_value(action, initial_state)
        except Plan1NativeStepExecutorError:
            raise
        except (TypeError, ValueError, IndexError) as error:
            raise Plan1NativeStepExecutorError(
                "step-action-invalid",
                f"{type(error).__name__}:{error}",
            ) from error
        current = initial_state.zones.hand[selected.hand_index]
        if (
            current.guid != selected.card_guid
            or current.card_id != selected.card_id
            or current.effective_upgrade != selected.upgrade
        ):
            raise Plan1NativeStepExecutorError(
                "step-action-not-in-hand",
                f"expected={current.guid}:{current.card_id}@{current.effective_upgrade};"
                f"got={selected.card_guid}:{selected.card_id}@{selected.upgrade}",
            )
        if initial_state.scalar.plays_remaining < 1:
            raise Plan1NativeStepExecutorError("step-initial-state-not-playable")

        target_database = self.database if database is None else Path(database)
        selected_program = program or _action_program(
            compilation,
            selected,
            database=target_database,
        )
        if not isinstance(selected_program, Plan1CompiledCard):
            raise Plan1NativeStepExecutorError("step-card-program-invalid")
        if (selected_program.card_id, selected_program.upgrade) != (
            selected.card_id,
            selected.upgrade,
        ):
            raise Plan1NativeStepExecutorError("step-card-program-action-mismatch")

        listener_effects = _listener_effects(
            parameter_buff_listener,
            tuple(parameter_buff_status_change_effects),
        )

        effective_resolver = hand_add_support_resolver
        if effective_resolver is not None and master_aware_support:
            try:
                effective_resolver = master_aware_plan1_hand_add_support_resolver(
                    effective_resolver,
                    database=target_database,
                    master_card_refs=master_card_refs,
                )
            except (TypeError, ValueError) as error:
                raise Plan1NativeStepExecutorError(
                    "step-support-resolver-invalid",
                    f"{type(error).__name__}:{error}",
                ) from error

        if settings is None:
            try:
                settings = load_plan1_native_settings()
            except (OSError, TypeError, ValueError) as error:
                raise Plan1NativeStepExecutorError(
                    "step-settings-load-failed",
                    f"{type(error).__name__}:{error}",
                ) from error

        try:
            stage_action = preview_plan1_stage_action(
                initial_state,
                selected.hand_index,
                selected_program,
                settings=settings,
                hand_add_support_resolver=effective_resolver,
                parameter_buff_status_change_effects=listener_effects,
            )
        except (OSError, TypeError, ValueError, IndexError) as error:
            raise Plan1NativeStepExecutorError(
                "step-card-preview-failed",
                f"{type(error).__name__}:{error}",
            ) from error
        if not stage_action.legal:
            detail = ",".join(
                f"{value.code}:{value.detail}".rstrip(":")
                for value in stage_action.blockers
            )
            raise Plan1NativeStepExecutorError(
                "step-card-not-legal",
                detail,
                blockers=(value.code for value in stage_action.blockers),
            )
        after_card, card_step = apply_plan1_stage_action(initial_state, stage_action)
        if card_step is None or after_card.blockers:
            raise Plan1NativeStepExecutorError(
                "step-card-apply-failed",
                ",".join(value.code for value in after_card.blockers),
                blockers=(value.code for value in after_card.blockers),
            )

        # ``ParameterBuff`` is a status object, not a plain counter.  The
        # native status-change path marks a newly added layer as passing the
        # next TurnStart, so the immediately following EndTurn must not spend
        # it.  Keep this lifecycle bit in the step adapter; the legacy scalar
        # core remains source-compatible for callers that only execute an
        # isolated effect list.
        parameter_buff_applied = any(
            entry.effect_type == EFFECT_PARAMETER_BUFF
            and entry.stage.value == "effect"
            and entry.after.parameter_buff_turns > entry.before.parameter_buff_turns
            for entry in card_step.transition.trace
        )
        if (
            parameter_buff_applied
            and initial_state.scalar.parameter_buff_turns == 0
        ):
            after_card = replace(
                after_card,
                scalar=replace(after_card.scalar, parameter_buff_fresh=True),
            )

        if after_card.scalar.plays_remaining != 0 or not auto_end_turn:
            return Plan1NativeStepResult(
                before=initial_state,
                after=after_card,
                action=selected,
                card_action=stage_action,
                card_step=card_step,
                remain_turn=None,
            )

        if turn_boundary is not None and boundary is not None:
            raise Plan1NativeStepExecutorError(
                "step-turn-boundary-ambiguous"
            )
        supplied_boundary = turn_boundary if turn_boundary is not None else boundary
        if supplied_boundary is None:
            raise Plan1NativeStepExecutorError(
                "step-turn-boundary-required",
                "last play was consumed; supply native draw/play-count facts",
            )
        if isinstance(supplied_boundary, Plan1StageTurnBoundary):
            native_boundary = Plan1NativeTurnBoundary.from_stage_boundary(
                supplied_boundary
            )
        elif isinstance(supplied_boundary, Plan1NativeTurnBoundary):
            native_boundary = supplied_boundary
        else:
            raise TypeError(
                "turn_boundary must be Plan1NativeTurnBoundary or Plan1StageTurnBoundary"
            )
        dispositions = native_boundary.disposition_mapping()
        if dispositions is None:
            dispositions = _master_end_turn_dispositions(
                after_card,
                database=target_database,
            )
        if native_boundary.draw_count is None:
            raise Plan1NativeStepExecutorError(
                "step-turn-draw-count-unresolved"
            )
        if native_boundary.plays_remaining is None:
            raise Plan1NativeStepExecutorError(
                "step-turn-plays-remaining-unresolved"
            )
        try:
            stage_boundary = native_boundary.to_stage_boundary(dispositions)
            after_boundary = advance_plan1_stage_turn(
                after_card,
                stage_boundary,
                settings=settings,
                hand_add_support_resolver=effective_resolver,
            )
        except Plan1NativeStepExecutorError:
            raise
        except (OSError, TypeError, ValueError, IndexError) as error:
            raise Plan1NativeStepExecutorError(
                "step-turn-boundary-failed",
                f"{type(error).__name__}:{error}",
            ) from error
        if after_boundary.blockers:
            raise Plan1NativeStepExecutorError(
                "step-turn-boundary-failed",
                ",".join(value.code for value in after_boundary.blockers),
                blockers=(value.code for value in after_boundary.blockers),
            )
        return Plan1NativeStepResult(
            before=initial_state,
            after=after_boundary,
            action=selected,
            card_action=stage_action,
            card_step=card_step,
            boundary=native_boundary,
            stage_boundary=stage_boundary,
            remain_turn=native_boundary.remain_turn,
        )


def execute_plan1_native_step(
    initial_state: Plan1NativeStageState,
    action: Plan1NativeStepAction | Plan1StageAction | LeaderboardReplayAction | Mapping[str, Any],
    *,
    compilation: Plan1DeckCompilation | Sequence[Plan1CompiledCard] | None = None,
    program: Plan1CompiledCard | None = None,
    settings: Plan1NativeSettings | None = None,
    turn_boundary: Plan1NativeTurnBoundary | Plan1StageTurnBoundary | None = None,
    boundary: Plan1NativeTurnBoundary | Plan1StageTurnBoundary | None = None,
    hand_add_support_resolver: Plan1HandAddSupportResolver | None = None,
    master_card_refs: Sequence[tuple[str, int]] | None = None,
    master_aware_support: bool = True,
    parameter_buff_listener: Plan1ParameterBuffStatusChangeListener
    | Sequence[Plan1CompiledEffect]
    | None = None,
    parameter_buff_status_change_effects: Sequence[Plan1CompiledEffect] = (),
    auto_end_turn: bool = True,
    database: Path = DEFAULT_DATABASE,
) -> Plan1NativeStepResult:
    """Convenience function for one selected Plan 1 card boundary."""

    return Plan1NativeStepExecutor(database=database).execute(
        initial_state,
        action,
        compilation=compilation,
        program=program,
        settings=settings,
        turn_boundary=turn_boundary,
        boundary=boundary,
        hand_add_support_resolver=hand_add_support_resolver,
        master_card_refs=master_card_refs,
        master_aware_support=master_aware_support,
        parameter_buff_listener=parameter_buff_listener,
        parameter_buff_status_change_effects=parameter_buff_status_change_effects,
        auto_end_turn=auto_end_turn,
        database=database,
    )


__all__ = [
    "PARAMETER_BUFF_STATUS_CHANGE_EFFECT_ID",
    "PARAMETER_BUFF_STATUS_CHANGE_TRIGGER_ID",
    "Plan1NativeStepAction",
    "Plan1NativeStepBlocker",
    "Plan1NativeStepBoundary",
    "Plan1NativeStepExecutor",
    "Plan1NativeStepExecutorError",
    "Plan1NativeStepResult",
    "Plan1NativeTurnBoundary",
    "Plan1ParameterBuffStatusChangeListener",
    "Plan1StepTurnBoundary",
    "execute_plan1_native_step",
    "load_plan1_parameter_buff_status_change_listener",
    "master_aware_plan1_hand_add_support_resolver",
]
