"""Path-local NIA turn-start integration for the shared native beam search.

The hook runs at the exact post-Full-Power/pre-draw boundary exposed by
``search_plan3_native``.  It carries NIA-only persistent state per beam path
and never performs game or process I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

from .master_db import DEFAULT_DATABASE
from .nia_add_grow_effect import DEFAULT_MASTER_DIR, NiaDeckAllState
from .nia_lesson_value_multiple import LessonParameterMultipleState
from .nia_static_adapter import NiaAuditionDefinition, NiaGimmickStep
from .nia_turn_start_gimmick import (
    NiaTurnStartDecision,
    NiaTurnStartCounters,
    inspect_nia_turn_start_profile,
    execute_nia_turn_start_from_master,
)
from .plan3_engine import Plan3ExamSettings, Plan3State
from .plan3_native_search import Plan3NativeTurnStartExtensionResult
from .plan3_native_state import Plan3NativeCard, Plan3NativeState


NIA_EXAM_MAIN_PHASE = 6
NIA_TURN_START_BOUNDARY = "post-full-power-pre-draw"

_STEP_TYPE_VALUE = {
    "ProduceStepType_AuditionMid1": 16,
    "ProduceStepType_AuditionMid2": 17,
    "ProduceStepType_AuditionFinal": 18,
}


@dataclass(frozen=True, slots=True)
class NiaSearchProfileIdentity:
    produce_id: str
    step_type: str
    audition_number: int
    group_id: str
    turns: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.produce_id, "produce_id"),
            (self.step_type, "step_type"),
        ):
            if not isinstance(value, str) or not value:
                raise TypeError(f"{label} must be non-empty text")
        # Ordinary Initial midterms legitimately have no scheduled gimmick.
        # Empty is an observed Master identity, not an invented group.
        if not isinstance(self.group_id, str):
            raise TypeError("group_id must be text (empty means no scheduled gimmick)")
        for value, label in (
            (self.audition_number, "audition_number"),
            (self.turns, "turns"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise TypeError(f"{label} must be a positive integer")
        if self.step_type not in _STEP_TYPE_VALUE:
            raise ValueError(f"unsupported NIA audition step: {self.step_type}")

    @property
    def step_type_value(self) -> int:
        return _STEP_TYPE_VALUE[self.step_type]


@dataclass(frozen=True, slots=True)
class NiaSearchRuntimeIdentity:
    phase: int
    step_type_value: int
    produce_id: str
    step_type: str
    audition_number: int
    group_id: str
    boundary: str = NIA_TURN_START_BOUNDARY


@dataclass(frozen=True, slots=True)
class NiaSearchTurnStartProfile:
    identity: NiaSearchProfileIdentity
    steps: tuple[NiaGimmickStep, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.identity, NiaSearchProfileIdentity):
            raise TypeError("identity must be NiaSearchProfileIdentity")
        steps = tuple(self.steps)
        if not all(isinstance(step, NiaGimmickStep) for step in steps):
            raise TypeError("steps must contain NiaGimmickStep values")
        object.__setattr__(self, "steps", steps)

    @classmethod
    def from_audition(
        cls,
        produce_id: str,
        audition: NiaAuditionDefinition,
    ) -> "NiaSearchTurnStartProfile":
        if not isinstance(audition, NiaAuditionDefinition):
            raise TypeError("audition must be NiaAuditionDefinition")
        return cls(
            NiaSearchProfileIdentity(
                produce_id=produce_id,
                step_type=audition.rules.step_type,
                audition_number=audition.rules.number,
                group_id=audition.rules.gimmick_group_id,
                turns=audition.rules.turns,
            ),
            audition.gimmicks,
        )


@dataclass(frozen=True, slots=True)
class NiaSearchTurnStartRuntime:
    multiplier_state: LessonParameterMultipleState = field(
        default_factory=LessonParameterMultipleState
    )
    future_decks: tuple[tuple[Plan3NativeCard, ...], ...] = ()
    past_decks: tuple[tuple[Plan3NativeCard, ...], ...] = ()
    applied_turns: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.multiplier_state, LessonParameterMultipleState):
            raise TypeError(
                "multiplier_state must be LessonParameterMultipleState"
            )
        if len(self.applied_turns) != len(set(self.applied_turns)):
            raise ValueError("applied_turns must be unique")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in self.applied_turns
        ):
            raise TypeError("applied_turns must contain positive integers")


@dataclass(frozen=True, slots=True)
class NiaSearchTurnStartTrace:
    profile_identity: NiaSearchProfileIdentity
    runtime_identity: NiaSearchRuntimeIdentity
    turn: int
    decisions: tuple[NiaTurnStartDecision, ...]
    listener_instance_ids: tuple[str, ...]
    add_grow_mutation_guids: tuple[str, ...]
    lesson_multiplier: float


def _identity_blockers(
    profile: NiaSearchTurnStartProfile,
    runtime: NiaSearchRuntimeIdentity,
) -> tuple[str, ...]:
    if not isinstance(runtime, NiaSearchRuntimeIdentity):
        return ("nia-runtime-identity-missing",)
    expected = profile.identity
    blockers: list[str] = []
    if runtime.phase != NIA_EXAM_MAIN_PHASE:
        blockers.append("nia-phase-mismatch")
    if runtime.boundary != NIA_TURN_START_BOUNDARY:
        blockers.append("nia-turn-start-boundary-mismatch")
    if runtime.step_type_value != _STEP_TYPE_VALUE[expected.step_type]:
        blockers.append("nia-step-value-mismatch")
    for field in (
        "produce_id",
        "step_type",
        "audition_number",
        "group_id",
    ):
        if getattr(runtime, field) != getattr(expected, field):
            blockers.append(f"nia-profile-{field.replace('_', '-')}-mismatch")
    return tuple(blockers)


@dataclass(frozen=True, slots=True)
class NiaTurnStartSearchExtension:
    profile: NiaSearchTurnStartProfile
    runtime_identity: NiaSearchRuntimeIdentity
    profile_blockers: tuple[str, ...] = ()
    database: Path = DEFAULT_DATABASE
    master_dir: Path = DEFAULT_MASTER_DIR

    @property
    def gate_blockers(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                (
                    *self.profile_blockers,
                    *_identity_blockers(self.profile, self.runtime_identity),
                )
            )
        )

    def __call__(
        self,
        scalar_state: Plan3State,
        native_state: Plan3NativeState,
        extension_state: object | None,
        _settings: Plan3ExamSettings,
    ) -> Plan3NativeTurnStartExtensionResult:
        runtime = (
            extension_state
            if isinstance(extension_state, NiaSearchTurnStartRuntime)
            else None
        )
        blockers = list(self.gate_blockers)
        if runtime is None:
            blockers.append("nia-search-runtime-state-missing")
        turn = scalar_state.round_number
        total_turns = self.profile.identity.turns + scalar_state.extra_turn
        if turn < 1 or turn > total_turns:
            blockers.append("nia-profile-turn-out-of-range")
        elif scalar_state.turns_remaining != (
            total_turns - turn + 1
        ):
            blockers.append("nia-profile-turn-clock-mismatch")
        if runtime is not None and turn in runtime.applied_turns:
            blockers.append("nia-turn-start-already-applied")
        if blockers:
            return Plan3NativeTurnStartExtensionResult(
                scalar_state,
                native_state,
                extension_state,
                unsupported_rules=tuple(dict.fromkeys(blockers)),
            )

        assert runtime is not None
        deck_all = NiaDeckAllState(
            ordinary=native_state,
            future_decks=runtime.future_decks,
            past_decks=runtime.past_decks,
        )
        scalar_working = [scalar_state]

        def apply_shared_scalar(step, effect, statuses, multiplier, next_uid):
            from .plan3_engine import (Plan3GimmickProfile, Plan3GimmickStep,
                _resolve_plan3_turn_start_gimmick, load_plan3_effect)
            current = replace(scalar_working[0], active_status_enchants=tuple(statuses),
                              lesson_parameter_multiple_state=multiplier,
                              next_status_uid=scalar_working[0].next_status_uid if next_uid is None else next_uid)
            definition = load_plan3_effect(effect.effect_id, self.database)
            profile = Plan3GimmickProfile(self.profile.identity.group_id, (Plan3GimmickStep(
                step.priority, step.start_turn, step.remaining_turn_permille,
                step.field_status_type, step.field_status_value, step.field_status_check_type, definition),))
            applied = _resolve_plan3_turn_start_gimmick(profile, current, _settings)
            if applied.unsupported_rules:
                raise ValueError("shared scalar gimmick unsupported: " + ",".join(applied.unsupported_rules))
            scalar_working[0] = applied.state

        # Native ExamGimmickModel.Setup builds the schedule by calling Update
        # for 1..limitTurn. HIF's startTurn=99 row is retained Master data but
        # never queued in a 9/10/12-turn audition, including later extra turns.
        queued_steps = tuple(step for step in self.profile.steps if step.start_turn <= self.profile.identity.turns)
        execution = execute_nia_turn_start_from_master(
            queued_steps,
            current_turn=turn,
            counters=NiaTurnStartCounters(
                full_power_point_get_sum=(
                    scalar_state.full_power_points_total
                ),
                concentration_change_count=(
                    scalar_state.concentration_change_count
                ),
                preservation_change_count=(
                    scalar_state.preservation_change_count
                ),
            ),
            multiplier_state=(
                runtime.multiplier_state.spend_turn_start()
            ),
            deck_all_state=deck_all,
            active_status_enchants=scalar_state.active_status_enchants,
            next_status_uid=(
                scalar_state.next_status_uid
                if scalar_state.status_uid_cursor_exact
                else None
            ),
            database=self.database,
            master_dir=self.master_dir,
            scalar_effect_handler=apply_shared_scalar,
        )
        unsupported = execution.unsupported
        if unsupported or execution.deck_all_state is None:
            rules = [
                f"nia-gimmick:{item.step.priority}:{item.reason}"
                for item in unsupported
            ]
            if execution.deck_all_state is None:
                rules.append("nia-deck-all-state-missing")
            return Plan3NativeTurnStartExtensionResult(
                scalar_state,
                native_state,
                runtime,
                unsupported_rules=tuple(dict.fromkeys(rules)),
            )

        multiplier = execution.multiplier_state.mark_passing_turn_start()
        deck_after = execution.deck_all_state
        scalar_after = replace(
            scalar_working[0],
            active_status_enchants=execution.status_enchants,
            # The extension runtime keeps NIA's path-local lifecycle, while
            # every shared Plan 3 score-hit path reads the same typed status
            # from the scalar state.  Project the settled post-TurnStart
            # snapshot into both owners; leaving it only in extension_state
            # would make subsequent Lesson hits silently use 1.0x.
            lesson_parameter_multiple_state=multiplier,
            next_status_uid=(
                scalar_state.next_status_uid
                if execution.next_status_uid is None
                else execution.next_status_uid
            ),
        )
        runtime_after = NiaSearchTurnStartRuntime(
            multiplier_state=multiplier,
            future_decks=deck_after.future_decks,
            past_decks=deck_after.past_decks,
            applied_turns=(*runtime.applied_turns, turn),
        )
        trace = NiaSearchTurnStartTrace(
            profile_identity=self.profile.identity,
            runtime_identity=self.runtime_identity,
            turn=turn,
            decisions=execution.decisions,
            listener_instance_ids=tuple(
                item.instance_id for item in execution.status_enchants
            ),
            add_grow_mutation_guids=tuple(
                item.guid for item in execution.add_grow_mutations
            ),
            lesson_multiplier=multiplier.multiplier(),
        )
        return Plan3NativeTurnStartExtensionResult(
            scalar_after,
            deck_after.ordinary,
            runtime_after,
            fired_effect_ids=tuple(
                item.step.effect_id for item in execution.applied
            ),
            trace=trace,
        )


def build_nia_turn_start_search_extension(
    profile: NiaSearchTurnStartProfile,
    runtime_identity: NiaSearchRuntimeIdentity,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaTurnStartSearchExtension:
    """Build a fail-closed search hook from one exact static profile."""

    if not isinstance(profile, NiaSearchTurnStartProfile):
        raise TypeError("profile must be NiaSearchTurnStartProfile")
    capability = inspect_nia_turn_start_profile(
        profile.steps,
        database=database,
        master_dir=master_dir,
    )
    blockers = tuple(
        f"nia-profile-effect-layer:{code}"
        for code in capability.blocker_codes
    )
    if capability.executable_steps != capability.total_steps:
        blockers = (
            *blockers,
            "nia-profile-effect-layer-incomplete",
        )
    # Out-of-horizon absolute turns are legal dormant Master rows. Effects
    # still need a complete handler; they are never moved to an earlier turn.
    return NiaTurnStartSearchExtension(
        profile=profile,
        runtime_identity=runtime_identity,
        profile_blockers=tuple(dict.fromkeys(blockers)),
        database=Path(database),
        master_dir=Path(master_dir),
    )


__all__ = [
    "NIA_EXAM_MAIN_PHASE",
    "NIA_TURN_START_BOUNDARY",
    "NiaSearchProfileIdentity",
    "NiaSearchRuntimeIdentity",
    "NiaSearchTurnStartProfile",
    "NiaSearchTurnStartRuntime",
    "NiaSearchTurnStartTrace",
    "NiaTurnStartSearchExtension",
    "build_nia_turn_start_search_extension",
]
