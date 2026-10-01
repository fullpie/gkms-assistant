"""NIA phase-23 adapter for the shared Plan 3 accepted-play transaction.

Android v3.2.3 builds the current card's direct commands from the playing
card before queued ``ExamPlayCountInterval`` children execute.  The shared
kernel therefore passes an already-compiled ``Plan3Card`` here and this
adapter mutates only listener counters and native DeckAll card state.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .master_db import DEFAULT_DATABASE
from .nia_add_grow_effect import (
    DEFAULT_MASTER_DIR,
    NiaAddGrowMutation,
    NiaDeckAllState,
    execute_plan3_add_grow_program,
    load_plan3_add_grow_program,
)
from .nia_native_search import (
    NIA_EXAM_MAIN_PHASE,
    NIA_TURN_START_BOUNDARY,
    NiaSearchProfileIdentity,
    NiaSearchRuntimeIdentity,
    NiaSearchTurnStartProfile,
    NiaSearchTurnStartRuntime,
)
from .nia_play_count_interval import (
    NiaPlayCountIntervalContractError,
    NiaPlayCountIntervalFire,
)
from .nia_status_enchant import (
    NiaStatusEnchantProgram,
    PHASE_PLAY_COUNT_INTERVAL,
    load_nia_status_enchant_program,
)
from .plan3_engine import (
    EFFECT_ADD_GROW,
    EFFECT_LESSON,
    EFFECT_STATUS_ENCHANT,
    ActivePlan3StatusEnchant,
    Plan3Card,
    Plan3ExamSettings,
    Plan3State,
    _apply_plan3_lesson_hit,
    load_plan3_card,
    load_plan3_effect,
)
from .plan3_native_search import (
    Plan3NativeAcceptedPlayExtensionResult,
    Plan3NativeAcceptedPlaySource,
)
from .plan3_native_state import (
    Plan3NativeCard,
    Plan3NativeState,
    Plan3NativeStateError,
)
from .play_count_interval_dispatcher import (
    OrderedPlayCountIntervalState,
    PlayCountIntervalDispatchError,
    PlayCountIntervalEffectApplication,
    PlayCountIntervalEffectHandler,
    execute_ordered_play_count_intervals,
)


ANDROID_NIA_ACCEPTED_PLAY_ORDER = (
    "set-playing-card",
    "increment-phase-23-counter-and-capture-listener-commands",
    "compiled-card-from-pre-listener-playing-snapshot",
    "payment-settled",
    "execute-exam-card-play-listener-children",
    "execute-captured-exam-play-count-interval-children",
    "execute-ordered-direct-card-effects",
    "final-zone-move",
)

# The shared engine calls this adapter from its generic pre-direct transform.
# The current Plan3Card remains the pre-listener compilation, while CardPlay
# native mutations settle before the captured interval child and both precede
# the card's ordered direct effects and final move.
NIA_ACCEPTED_PLAY_BOUNDARY = (
    "compile-current-card-before-extension",
    "delegate-captured-phase-23-listener-instances",
    "settle-scalar-legality-payment-and-card-play",
    "settle-exam-card-play-native-effects",
    "execute-captured-phase-23-native-children",
    "execute-native-direct-card-effects",
    "settle-final-zone-move",
)

_STEP_TYPE_VALUE = {
    "ProduceStepType_AuditionMid1": 16,
    "ProduceStepType_AuditionMid2": 17,
    "ProduceStepType_AuditionFinal": 18,
}

_NATIVE_ZONES = ("hand", "deck", "grave", "lost", "hold")


@dataclass(frozen=True, slots=True)
class _AcceptedIntervalPayload:
    deck_all: NiaDeckAllState
    scalar_state: Plan3State
    settings: Plan3ExamSettings
    mutations: tuple[NiaAddGrowMutation, ...] = ()
    parameter_pre_fix_values: tuple[int, ...] = ()
    parameter_actual_values: tuple[int, ...] = ()
    parameter_clear_changed: bool = False
    parameter_perfect_changed: bool = False


@dataclass(frozen=True, slots=True)
class NiaAcceptedPlayTrace:
    profile_identity: NiaSearchProfileIdentity
    runtime_identity: NiaSearchRuntimeIdentity
    source: Plan3NativeAcceptedPlaySource
    playing_guid: str
    playing_card_id: str
    handled_listener_instance_ids: tuple[str, ...]
    fires: tuple[NiaPlayCountIntervalFire, ...]
    mutation_guids: tuple[str, ...]
    order: tuple[str, ...] = ANDROID_NIA_ACCEPTED_PLAY_ORDER
    adapter_order: tuple[str, ...] = NIA_ACCEPTED_PLAY_BOUNDARY


def _identity_blockers(
    profile: NiaSearchTurnStartProfile,
    runtime: NiaSearchRuntimeIdentity,
) -> tuple[str, ...]:
    expected = profile.identity
    blockers: list[str] = []
    if runtime.phase != NIA_EXAM_MAIN_PHASE:
        blockers.append("nia-phase-mismatch")
    if runtime.boundary != NIA_TURN_START_BOUNDARY:
        blockers.append("nia-turn-start-boundary-mismatch")
    if runtime.step_type_value != _STEP_TYPE_VALUE[expected.step_type]:
        blockers.append("nia-step-value-mismatch")
    for field in ("produce_id", "step_type", "audition_number", "group_id"):
        if getattr(runtime, field) != getattr(expected, field):
            blockers.append(f"nia-profile-{field.replace('_', '-')}-mismatch")
    return tuple(blockers)


def _split_playing_card(
    native: Plan3NativeState,
    playing_guid: str,
) -> tuple[Plan3NativeState, Plan3NativeCard, str, int]:
    matches: list[tuple[str, int, Plan3NativeCard]] = []
    for zone_name in _NATIVE_ZONES:
        for index, card in enumerate(getattr(native, zone_name)):
            if card.guid == playing_guid:
                matches.append((zone_name, index, card))
    if len(matches) != 1:
        raise Plan3NativeStateError(
            "accepted-play-guid-not-unique", playing_guid
        )
    zone_name, index, playing = matches[0]
    zone = getattr(native, zone_name)
    ordinary = replace(
        native,
        **{zone_name: (*zone[:index], *zone[index + 1 :])},
    )
    return ordinary, playing, zone_name, index


def _restore_playing_card(
    ordinary: Plan3NativeState,
    playing: Plan3NativeCard | None,
    zone_name: str,
    index: int,
) -> Plan3NativeState:
    if playing is None:
        raise Plan3NativeStateError("accepted-play-playing-card-missing")
    if any(card.guid == playing.guid for card in ordinary.all_cards):
        raise Plan3NativeStateError(
            "accepted-play-playing-card-duplicated", playing.guid
        )
    zone = list(getattr(ordinary, zone_name))
    if not 0 <= index <= len(zone):
        raise Plan3NativeStateError(
            "accepted-play-source-index-drift", f"{zone_name}:{index}"
        )
    zone.insert(index, playing)
    return replace(ordinary, **{zone_name: tuple(zone)})


def _replace_phase_count(
    enchant: ActivePlan3StatusEnchant,
    count: int,
) -> ActivePlan3StatusEnchant:
    counts = tuple(
        (phase, value)
        for phase, value in enchant.phase_counts
        if phase != PHASE_PLAY_COUNT_INTERVAL
    )
    return replace(
        enchant,
        phase_counts=(*counts, (PHASE_PLAY_COUNT_INTERVAL, count)),
    )


def _ordered_interval_handlers(
    *,
    database: Path,
    master_dir: Path,
) -> tuple[PlayCountIntervalEffectHandler[_AcceptedIntervalPayload], ...]:
    def validate_add_grow(effect):
        try:
            load_plan3_add_grow_program(
                effect.id,
                database=database,
                master_dir=master_dir,
            )
        except (KeyError, OSError, TypeError, ValueError) as error:
            return (
                f"add-grow:{effect.id}:{type(error).__name__}:{error}",
            )
        return ()

    def execute_add_grow(payload, effect, _context):
        from .plan3_native_search import _plan3_add_grow_card_profile

        program = load_plan3_add_grow_program(
            effect.id,
            database=database,
            master_dir=master_dir,
        )
        result = execute_plan3_add_grow_program(
            payload.deck_all,
            program,
            card_profile_by_card=(
                lambda card: _plan3_add_grow_card_profile(
                    load_plan3_card(
                        card.card_id,
                        card.effective_upgrade,
                        database,
                    )
                )
            ),
        )
        return PlayCountIntervalEffectApplication(
            _AcceptedIntervalPayload(
                result.state,
                payload.scalar_state,
                payload.settings,
                (*payload.mutations, *result.mutations),
                payload.parameter_pre_fix_values,
                payload.parameter_actual_values,
                payload.parameter_clear_changed,
                payload.parameter_perfect_changed,
            ),
            result.mutations,
        )

    def validate_lesson(effect):
        if (
            effect.value1 < 0
            or effect.value2 != 0
            or effect.effect_count <= 0
            or effect.effect_turn != 0
            or effect.status_enchant_id
            or effect.chain_effect_id
            or effect.chain_effect_ids
        ):
            return (f"lesson-shape:{effect.id}",)
        return ()

    def execute_lesson(payload, effect, _context):
        working = payload.scalar_state
        applications: list[object] = []
        pre_fix = list(payload.parameter_pre_fix_values)
        actual = list(payload.parameter_actual_values)
        clear_changed = payload.parameter_clear_changed
        perfect_changed = payload.parameter_perfect_changed
        for _ in range(effect.effect_count):
            working, application = _apply_plan3_lesson_hit(
                working,
                effect.value1,
                payload.settings,
            )
            applications.append(application)
            pre_fix.append(application.pre_fix_parameter)
            actual.append(application.actual_parameter)
            clear_changed = clear_changed or application.clear_changed
            perfect_changed = (
                perfect_changed or application.perfect_changed
            )
        return PlayCountIntervalEffectApplication(
            _AcceptedIntervalPayload(
                payload.deck_all,
                working,
                payload.settings,
                payload.mutations,
                tuple(pre_fix),
                tuple(actual),
                clear_changed,
                perfect_changed,
            ),
            tuple(applications),
        )

    return (
        PlayCountIntervalEffectHandler(
            EFFECT_ADD_GROW,
            validate_add_grow,
            execute_add_grow,
        ),
        PlayCountIntervalEffectHandler(
            EFFECT_LESSON,
            validate_lesson,
            execute_lesson,
        ),
    )


@dataclass(frozen=True, slots=True)
class NiaAcceptedPlaySearchExtension:
    """Exact NIA interval programs bound to one static audition profile."""

    profile: NiaSearchTurnStartProfile
    runtime_identity: NiaSearchRuntimeIdentity
    programs: tuple[NiaStatusEnchantProgram, ...]
    profile_blockers: tuple[str, ...] = ()
    database: Path = DEFAULT_DATABASE
    master_dir: Path = DEFAULT_MASTER_DIR

    def __post_init__(self) -> None:
        if not isinstance(self.profile, NiaSearchTurnStartProfile):
            raise TypeError("profile must be NiaSearchTurnStartProfile")
        if not isinstance(self.runtime_identity, NiaSearchRuntimeIdentity):
            raise TypeError("runtime_identity must be NiaSearchRuntimeIdentity")
        programs = tuple(self.programs)
        if not all(isinstance(item, NiaStatusEnchantProgram) for item in programs):
            raise TypeError("programs must contain NiaStatusEnchantProgram values")
        source_ids = [item.effect.id for item in programs]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("interval program effect IDs must be unique")
        status_ids = [item.effect.status_enchant_id for item in programs]
        if (
            any(not value for value in status_ids)
            or len(status_ids) != len(set(status_ids))
        ):
            raise ValueError("interval program status IDs must be unique")
        if any(
            item.effect.status_enchant is None
            or item.effect.status_enchant.trigger.phase_types
            != (PHASE_PLAY_COUNT_INTERVAL,)
            for item in programs
        ):
            raise ValueError("programs must be exact play-count interval listeners")
        blockers = tuple(self.profile_blockers)
        if any(not isinstance(item, str) or not item for item in blockers):
            raise TypeError("profile_blockers must contain non-empty text")
        object.__setattr__(self, "programs", programs)
        object.__setattr__(self, "profile_blockers", blockers)
        object.__setattr__(self, "database", Path(self.database))
        object.__setattr__(self, "master_dir", Path(self.master_dir))

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

    def managed_play_count_interval_instance_ids(
        self,
        scalar_state: Plan3State,
        extension_state: object | None,
    ) -> tuple[str, ...]:
        if not isinstance(scalar_state, Plan3State):
            raise TypeError("scalar_state must be Plan3State")
        # One phase-23 dispatcher must own every active listener in native
        # collection order.  Partitioning by NIA profile/card/item origin
        # loses cross-source ordering and made a valid card listener block a
        # simultaneously active gimmick listener.
        return tuple(
            enchant.instance_id
            for enchant in scalar_state.active_status_enchants
            if enchant.rule.trigger.phase_types == (
                PHASE_PLAY_COUNT_INTERVAL,
            )
        )

    def __call__(
        self,
        scalar_before: Plan3State,
        scalar_after: Plan3State,
        native_state: Plan3NativeState,
        playing_guid: str,
        compiled_card: Plan3Card,
        extension_state: object | None,
        _settings: Plan3ExamSettings,
        source: Plan3NativeAcceptedPlaySource,
    ) -> Plan3NativeAcceptedPlayExtensionResult:
        if not isinstance(scalar_before, Plan3State):
            raise TypeError("scalar_before must be Plan3State")
        if not isinstance(scalar_after, Plan3State):
            raise TypeError("scalar_after must be Plan3State")
        if not isinstance(native_state, Plan3NativeState):
            raise TypeError("native_state must be Plan3NativeState")
        if not isinstance(playing_guid, str) or not playing_guid:
            raise TypeError("playing_guid must be non-empty text")
        if not isinstance(compiled_card, Plan3Card):
            raise TypeError("compiled_card must be Plan3Card")
        if not isinstance(source, Plan3NativeAcceptedPlaySource):
            raise TypeError("source must be Plan3NativeAcceptedPlaySource")

        managed_ids = self.managed_play_count_interval_instance_ids(
            scalar_before, extension_state
        )
        blockers = list(self.gate_blockers)
        runtime = (
            extension_state
            if isinstance(extension_state, NiaSearchTurnStartRuntime)
            else None
        )
        if runtime is None:
            blockers.append("nia-search-runtime-state-missing")
        after_by_id = {
            enchant.instance_id: enchant
            for enchant in scalar_after.active_status_enchants
        }
        before_by_id = {
            enchant.instance_id: enchant
            for enchant in scalar_before.active_status_enchants
        }
        managed_after: list[ActivePlan3StatusEnchant] = []
        for instance_id in managed_ids:
            enchant = after_by_id.get(instance_id)
            if enchant is None:
                blockers.append(
                    f"nia-accepted-listener-missing-after:{instance_id}"
                )
            else:
                managed_after.append(enchant)
                if enchant != before_by_id[instance_id]:
                    blockers.append(
                        "nia-accepted-listener-changed-after-capture:"
                        f"{instance_id}"
                    )
        if blockers:
            return Plan3NativeAcceptedPlayExtensionResult(
                scalar_after.active_status_enchants,
                native_state,
                extension_state,
                managed_ids,
                unsupported_rules=tuple(dict.fromkeys(blockers)),
            )
        if not managed_ids:
            return Plan3NativeAcceptedPlayExtensionResult(
                scalar_after.active_status_enchants,
                native_state,
                runtime,
                scalar_state=scalar_after,
            )

        assert runtime is not None
        try:
            ordinary, playing, zone_name, zone_index = _split_playing_card(
                native_state, playing_guid
            )
            if (
                playing.card_id != compiled_card.id
                or playing.effective_upgrade != compiled_card.upgrade
            ):
                raise Plan3NativeStateError(
                    "accepted-play-compiled-card-identity-drift",
                    playing_guid,
                )
            interval_state = OrderedPlayCountIntervalState(
                _AcceptedIntervalPayload(
                    NiaDeckAllState(
                        ordinary=ordinary,
                        playing=playing,
                        future_decks=runtime.future_decks,
                        past_decks=runtime.past_decks,
                    ),
                    scalar_after,
                    _settings,
                ),
                tuple(managed_after),
            )
            execution = execute_ordered_play_count_intervals(
                interval_state,
                _ordered_interval_handlers(
                    database=self.database,
                    master_dir=self.master_dir,
                ),
            )
            deck_after = execution.after.payload.deck_all
            native_after = _restore_playing_card(
                deck_after.ordinary,
                deck_after.playing,
                zone_name,
                zone_index,
            )
        except (
            NiaPlayCountIntervalContractError,
            PlayCountIntervalDispatchError,
            Plan3NativeStateError,
        ) as error:
            code = getattr(error, "code", type(error).__name__)
            detail = getattr(error, "detail", str(error))
            return Plan3NativeAcceptedPlayExtensionResult(
                scalar_after.active_status_enchants,
                native_state,
                runtime,
                managed_ids,
                unsupported_rules=(
                    f"nia-accepted-play:{code}:{detail}".rstrip(":"),
                ),
            )

        active_by_id = {
            enchant.instance_id: enchant
            for enchant in execution.after.active
        }
        active_after = tuple(
            active_by_id.get(enchant.instance_id, enchant)
            for enchant in scalar_after.active_status_enchants
        )
        mutations = execution.after.payload.mutations
        profile_program_by_status = {
            program.effect.status_enchant_id: program
            for program in self.programs
        }
        legacy_fires: list[NiaPlayCountIntervalFire] = []
        for fire in execution.fires:
            listener = execution.after.active[fire.listener_sequence]
            program = profile_program_by_status.get(listener.rule.id)
            source_effect_id = (
                listener.rule.id if program is None else program.effect.id
            )
            nested_effect_ids = tuple(
                effect.id for effect in listener.rule.effects
            )
            # ``NiaPlayCountIntervalFire`` is the compatibility trace for
            # AddGrow and its offsets index ``add_grow_mutations``.  Other
            # typed evidence (for example a lesson ParameterApplication)
            # must not shift those offsets.
            mutation_start = sum(
                sum(
                    isinstance(value, NiaAddGrowMutation)
                    for value in receipt.evidence
                )
                for receipt in execution.effect_receipts[
                    : fire.effect_receipt_start
                ]
            )
            mutation_count = sum(
                sum(
                    isinstance(value, NiaAddGrowMutation)
                    for value in receipt.evidence
                )
                for receipt in execution.effect_receipts[
                    fire.effect_receipt_start:
                    fire.effect_receipt_start + fire.effect_receipt_count
                ]
            )
            legacy_fires.append(
                NiaPlayCountIntervalFire(
                    listener_sequence=fire.listener_sequence,
                    instance_id=fire.instance_id,
                    source_effect_id=source_effect_id,
                    status_enchant_id=listener.rule.id,
                    interval=fire.interval,
                    count_before=fire.count_before,
                    count_after=fire.count_after,
                    nested_effect_ids=nested_effect_ids,
                    mutation_start=mutation_start,
                    mutation_count=mutation_count,
                )
            )
        runtime_after = NiaSearchTurnStartRuntime(
            multiplier_state=runtime.multiplier_state,
            future_decks=deck_after.future_decks,
            past_decks=deck_after.past_decks,
            applied_turns=runtime.applied_turns,
        )
        trace = NiaAcceptedPlayTrace(
            self.profile.identity,
            self.runtime_identity,
            source,
            playing_guid,
            compiled_card.id,
            managed_ids,
            tuple(legacy_fires),
            tuple(item.guid for item in mutations),
        )
        return Plan3NativeAcceptedPlayExtensionResult(
            active_after,
            native_after,
            runtime_after,
            managed_ids,
            fired_status_enchant_ids=tuple(
                fire.status_enchant_id for fire in legacy_fires
            ),
            fired_effect_ids=tuple(
                effect_id
                for fire in legacy_fires
                for effect_id in fire.nested_effect_ids
            ),
            add_grow_mutations=mutations,
            trace=trace,
            scalar_state=execution.after.payload.scalar_state,
            parameter_pre_fix_values=(
                execution.after.payload.parameter_pre_fix_values
            ),
            parameter_actual_values=(
                execution.after.payload.parameter_actual_values
            ),
            parameter_clear_changed=(
                execution.after.payload.parameter_clear_changed
            ),
            parameter_perfect_changed=(
                execution.after.payload.parameter_perfect_changed
            ),
        )


def build_nia_accepted_play_search_extension(
    profile: NiaSearchTurnStartProfile,
    runtime_identity: NiaSearchRuntimeIdentity,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaAcceptedPlaySearchExtension:
    """Load the profile's exact interval programs without guessing listeners."""

    if not isinstance(profile, NiaSearchTurnStartProfile):
        raise TypeError("profile must be NiaSearchTurnStartProfile")
    if not isinstance(runtime_identity, NiaSearchRuntimeIdentity):
        raise TypeError("runtime_identity must be NiaSearchRuntimeIdentity")
    programs: list[NiaStatusEnchantProgram] = []
    blockers: list[str] = []
    seen: set[str] = set()
    for step in profile.steps:
        if step.effect_id in seen:
            continue
        seen.add(step.effect_id)
        try:
            effect = load_plan3_effect(step.effect_id, database=Path(database))
            if effect.effect_type != EFFECT_STATUS_ENCHANT:
                continue
            program = load_nia_status_enchant_program(
                step.effect_id,
                database=Path(database),
                master_dir=Path(master_dir),
            )
        except (KeyError, OSError, TypeError, ValueError) as error:
            blockers.append(
                f"nia-accepted-program:{step.effect_id}:"
                f"{type(error).__name__}:{error}"
            )
            continue
        if program.effect.status_enchant is not None and (
            program.effect.status_enchant.trigger.phase_types
            == (PHASE_PLAY_COUNT_INTERVAL,)
        ):
            programs.append(program)
    return NiaAcceptedPlaySearchExtension(
        profile,
        runtime_identity,
        tuple(programs),
        tuple(blockers),
        database=Path(database),
        master_dir=Path(master_dir),
    )


__all__ = [
    "ANDROID_NIA_ACCEPTED_PLAY_ORDER",
    "NIA_ACCEPTED_PLAY_BOUNDARY",
    "NiaAcceptedPlaySearchExtension",
    "NiaAcceptedPlayTrace",
    "build_nia_accepted_play_search_extension",
]
