"""One deterministic NIA player-side inner terminal acceptance.

The audition profile and card programs come from the pinned PC Master.  The
ordered card instances, GUIDs, two RNG seeds, and initial scalar runtime do
not: they are explicit caller-authored fixture inputs.  In particular, this
module does not claim that the fixture was decoded from an authentic NIA
LocalSave, and it deliberately leaves NPC ranking outside the acceptance.

The runner is a thin composition boundary.  It replays the native audition
turn-parameter recipe, then enables both NIA extensions around the shared
Plan 3 native search until the player-side turn clock reaches zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from .master_db import DEFAULT_DATABASE, get_idol_profile
from .native_exam_formula import ProduceParameterType
from .nia_accepted_play_extension import (
    NiaAcceptedPlaySearchExtension,
    NiaAcceptedPlayTrace,
    build_nia_accepted_play_search_extension,
)
from .nia_lesson_value_multiple import (
    EFFECT_TYPE as LESSON_VALUE_MULTIPLE_EFFECT_TYPE,
)
from .nia_native_search import (
    NIA_EXAM_MAIN_PHASE,
    NiaSearchRuntimeIdentity,
    NiaSearchTurnStartProfile,
    NiaSearchTurnStartRuntime,
    NiaSearchTurnStartTrace,
    build_nia_turn_start_search_extension,
)
from .nia_static_adapter import (
    DEFAULT_MASTER_DIR,
    NiaAuditionDefinition,
    NiaReplayedTurnParameterSchedule,
    load_nia_static_bundle,
    replay_nia_turn_parameter_schedule,
)
from .plan3_engine import (
    LESSON_DANCE,
    LESSON_VISUAL,
    LESSON_VOCAL,
    Plan3CardRef,
    Plan3State,
    load_plan3_card,
    load_plan3_exam_settings,
    load_plan3_initial_deck,
)
from .plan3_native_search import (
    Plan3NativeSearchPath,
    Plan3NativeSearchResult,
    search_plan3_native,
)
from .plan3_native_state import Plan3NativeCard, Plan3NativeState
from .produce_rollout import DeckEntry


NIA_TERMINAL_IDOL_CARD_ID = "i_card-fktn-3-011"
NIA_TERMINAL_PRODUCE_ID = "produce-004"
NIA_TERMINAL_STEP_TYPE = "ProduceStepType_AuditionMid1"
NIA_TERMINAL_AUDITION_NUMBER = 1
NIA_TERMINAL_CARD_ID = "p_card-00-act-0_001"
NIA_TERMINAL_CARD_COUNT = 27
NIA_TERMINAL_GUID_PREFIX = "terminal-guid"
NIA_TERMINAL_SCHEDULE_RANDOM_STATE = 0x12345678
NIA_TERMINAL_EXAM_RANDOM_STATE = 0x2468ACE0
NIA_FKTN_STARTER_GUID_PREFIX = "nia-fktn-starter-guid"
NIA_FKTN_UNIQUE_CARD_UPGRADE = 1

_EXPECTED_NPC_GAP = "battle-npc-ranking-state-unmodelled"
_PARAMETER_RUNTIME = {
    "ProduceParameterType_Vocal": (
        int(ProduceParameterType.VOCAL),
        LESSON_VOCAL,
    ),
    "ProduceParameterType_Dance": (
        int(ProduceParameterType.DANCE),
        LESSON_DANCE,
    ),
    "ProduceParameterType_Visual": (
        int(ProduceParameterType.VISUAL),
        LESSON_VISUAL,
    ),
}


class NiaInnerInputAuthority(StrEnum):
    """Machine-readable authority labels for this bounded acceptance."""

    PC_MASTER = "pc-master"
    CALLER_AUTHORED = "caller-authored-synthetic-fixture"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class NiaInnerMasterProfileSelector:
    idol_card_id: str = NIA_TERMINAL_IDOL_CARD_ID
    produce_id: str = NIA_TERMINAL_PRODUCE_ID
    step_type: str = NIA_TERMINAL_STEP_TYPE
    audition_number: int = NIA_TERMINAL_AUDITION_NUMBER

    def __post_init__(self) -> None:
        for name in ("idol_card_id", "produce_id", "step_type"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise TypeError(f"{name} must be non-empty text")
        if (
            not isinstance(self.audition_number, int)
            or isinstance(self.audition_number, bool)
            or self.audition_number < 1
        ):
            raise TypeError("audition_number must be a positive integer")


@dataclass(frozen=True, slots=True)
class NiaCallerOwnedOrderedDeck:
    """Caller-authored order and identities; not a reconstructed shuffle."""

    cards: tuple[Plan3NativeCard, ...]

    def __post_init__(self) -> None:
        cards = tuple(self.cards)
        if not cards or not all(isinstance(card, Plan3NativeCard) for card in cards):
            raise TypeError("cards must contain Plan3NativeCard values")
        guids = tuple(card.guid for card in cards)
        if len(guids) != len(set(guids)):
            raise ValueError("caller-owned ordered deck GUIDs must be unique")
        orders = tuple(card.fixed_deck_order for card in cards)
        if orders != tuple(range(1, len(cards) + 1)):
            raise ValueError(
                "caller-owned ordered deck must use consecutive fixed orders"
            )
        object.__setattr__(self, "cards", cards)

    @property
    def guids(self) -> tuple[str, ...]:
        return tuple(card.guid for card in self.cards)

    @property
    def card_count(self) -> int:
        return len(self.cards)


@dataclass(frozen=True, slots=True)
class NiaCallerOwnedInnerRuntime:
    """Synthetic scalar/RNG values supplied by the acceptance caller."""

    schedule_random_state: int = NIA_TERMINAL_SCHEDULE_RANDOM_STATE
    exam_random_state: int = NIA_TERMINAL_EXAM_RANDOM_STATE
    score: int = 0
    stamina: int = 99
    max_stamina: int = 99
    block: int = 0
    concentration_change_count: int = 2
    battle_bonus_permille_vocal: int = 1000
    battle_bonus_permille_dance: int = 1000
    battle_bonus_permille_visual: int = 1000
    judge_parameter_vocal: int = 0
    judge_parameter_dance: int = 0
    judge_parameter_visual: int = 0
    round_number: int = 1
    plays_remaining: int = 0
    awaiting_turn_start: bool = True

    def __post_init__(self) -> None:
        for name in ("schedule_random_state", "exam_random_state"):
            value = getattr(self, name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= 0xFFFFFFFF
            ):
                raise ValueError(f"{name} must be a uint32")
        for name in (
            "score",
            "stamina",
            "max_stamina",
            "block",
            "concentration_change_count",
            "battle_bonus_permille_vocal",
            "battle_bonus_permille_dance",
            "battle_bonus_permille_visual",
            "judge_parameter_vocal",
            "judge_parameter_dance",
            "judge_parameter_visual",
            "plays_remaining",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.stamina > self.max_stamina:
            raise ValueError("stamina cannot exceed max_stamina")
        if (
            not isinstance(self.round_number, int)
            or isinstance(self.round_number, bool)
            or self.round_number < 1
        ):
            raise ValueError("round_number must be a positive integer")
        if not isinstance(self.awaiting_turn_start, bool):
            raise TypeError("awaiting_turn_start must be bool")


@dataclass(frozen=True, slots=True)
class NiaDeterministicInnerFixture:
    """All inputs needed to replay the bounded player-side scenario."""

    selector: NiaInnerMasterProfileSelector
    ordered_deck: NiaCallerOwnedOrderedDeck
    runtime: NiaCallerOwnedInnerRuntime
    beam_width: int = 1
    profile_authority: NiaInnerInputAuthority = field(
        default=NiaInnerInputAuthority.PC_MASTER,
        init=False,
    )
    deck_guid_authority: NiaInnerInputAuthority = field(
        default=NiaInnerInputAuthority.CALLER_AUTHORED,
        init=False,
    )
    rng_authority: NiaInnerInputAuthority = field(
        default=NiaInnerInputAuthority.CALLER_AUTHORED,
        init=False,
    )
    scalar_runtime_authority: NiaInnerInputAuthority = field(
        default=NiaInnerInputAuthority.CALLER_AUTHORED,
        init=False,
    )
    npc_authority: NiaInnerInputAuthority = field(
        default=NiaInnerInputAuthority.UNRESOLVED,
        init=False,
    )
    authentic_nia_local_save: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.selector, NiaInnerMasterProfileSelector):
            raise TypeError("selector must be NiaInnerMasterProfileSelector")
        if not isinstance(self.ordered_deck, NiaCallerOwnedOrderedDeck):
            raise TypeError("ordered_deck must be NiaCallerOwnedOrderedDeck")
        if not isinstance(self.runtime, NiaCallerOwnedInnerRuntime):
            raise TypeError("runtime must be NiaCallerOwnedInnerRuntime")
        if (
            not isinstance(self.beam_width, int)
            or isinstance(self.beam_width, bool)
            or self.beam_width < 1
        ):
            raise ValueError("beam_width must be a positive integer")


@dataclass(frozen=True, slots=True, order=True)
class NiaInnerTerminalAcceptanceIssue:
    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("issue code must be non-empty text")
        if not isinstance(self.detail, str):
            raise TypeError("issue detail must be text")


@dataclass(frozen=True, slots=True)
class NiaInnerTerminalAcceptance:
    """True Master profile plus one synthetic, deterministic inner replay."""

    fixture: NiaDeterministicInnerFixture
    audition: NiaAuditionDefinition
    profile: NiaSearchTurnStartProfile
    parameter_schedule: NiaReplayedTurnParameterSchedule
    battle_parameter_schedule: tuple[int, ...]
    initial_state: Plan3State
    initial_native_state: Plan3NativeState
    search: Plan3NativeSearchResult
    unresolved_outer_gaps: tuple[str, ...]
    issues: tuple[NiaInnerTerminalAcceptanceIssue, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.fixture, NiaDeterministicInnerFixture):
            raise TypeError("fixture must be NiaDeterministicInnerFixture")
        if not isinstance(self.audition, NiaAuditionDefinition):
            raise TypeError("audition must be NiaAuditionDefinition")
        if not isinstance(self.profile, NiaSearchTurnStartProfile):
            raise TypeError("profile must be NiaSearchTurnStartProfile")
        if not isinstance(
            self.parameter_schedule, NiaReplayedTurnParameterSchedule
        ):
            raise TypeError(
                "parameter_schedule must be NiaReplayedTurnParameterSchedule"
            )
        if not isinstance(self.initial_state, Plan3State):
            raise TypeError("initial_state must be Plan3State")
        if not isinstance(self.initial_native_state, Plan3NativeState):
            raise TypeError("initial_native_state must be Plan3NativeState")
        if not isinstance(self.search, Plan3NativeSearchResult):
            raise TypeError("search must be Plan3NativeSearchResult")
        if any(not isinstance(value, str) or not value for value in self.unresolved_outer_gaps):
            raise TypeError("unresolved_outer_gaps must contain non-empty text")
        if any(
            not isinstance(value, NiaInnerTerminalAcceptanceIssue)
            for value in self.issues
        ):
            raise TypeError("issues must contain typed acceptance issues")

    @property
    def best(self) -> Plan3NativeSearchPath | None:
        return self.search.best

    @property
    def player_inner_terminal(self) -> bool:
        return (
            not self.issues
            and self.best is not None
            and self.best.complete
            and self.best.stopped_reason == ""
        )

    @property
    def deterministic_player_inner_executable(self) -> bool:
        return self.player_inner_terminal

    @property
    def npc_resolved(self) -> bool:
        return False

    @property
    def full_audition_resolved(self) -> bool:
        return self.player_inner_terminal and self.npc_resolved

    @property
    def authentic_nia_local_save(self) -> bool:
        return self.fixture.authentic_nia_local_save


def _issue(code: str, detail: str = "") -> NiaInnerTerminalAcceptanceIssue:
    return NiaInnerTerminalAcceptanceIssue(code, detail)


def _select_audition(
    fixture: NiaDeterministicInnerFixture,
    *,
    master_dir: Path,
) -> NiaAuditionDefinition:
    selector = fixture.selector
    bundle = load_nia_static_bundle(
        selector.idol_card_id,
        produce_id=selector.produce_id,
        master_dir=master_dir,
    )
    matches = tuple(
        audition
        for audition in bundle.auditions
        if audition.rules.step_type == selector.step_type
        and audition.rules.number == selector.audition_number
    )
    if len(matches) != 1:
        raise ValueError(
            "NIA terminal Master profile must resolve exactly once: "
            f"{selector.produce_id}/{selector.step_type}/"
            f"{selector.audition_number}:found={len(matches)}"
        )
    return matches[0]


def build_nia_produce004_mid1_terminal_fixture(
    *,
    database: Path = DEFAULT_DATABASE,
) -> NiaDeterministicInnerFixture:
    """Build the pinned 27-card caller-authored terminal fixture.

    Only the card rule is loaded from Master.  The repeated instances, GUIDs,
    fixed ordering, RNG seeds, and scalar values are deliberately authored by
    this caller fixture.
    """

    card = load_plan3_card(NIA_TERMINAL_CARD_ID, database=Path(database))
    cards = tuple(
        Plan3NativeCard(
            guid=f"{NIA_TERMINAL_GUID_PREFIX}-{index:02d}",
            card_id=card.id,
            base_upgrade=card.upgrade,
            temporary_upgrade=0,
            effective_upgrade=card.upgrade,
            fixed_deck_order=index + 1,
        )
        for index in range(NIA_TERMINAL_CARD_COUNT)
    )
    return NiaDeterministicInnerFixture(
        selector=NiaInnerMasterProfileSelector(),
        ordered_deck=NiaCallerOwnedOrderedDeck(cards),
        runtime=NiaCallerOwnedInnerRuntime(),
    )


def build_nia_produce004_fktn_starter_terminal_fixture(
    *,
    database: Path = DEFAULT_DATABASE,
    beam_width: int = 8,
    idol_card_upgrade: int = NIA_FKTN_UNIQUE_CARD_UPGRADE,
) -> NiaDeterministicInnerFixture:
    """Build the real Master starter identities plus explicit runtime order.

    ``ProduceExamInitialDeck`` owns the eight Plan3 mode cards and
    ``IdolCard.produceCardId`` owns FKTN's mandatory ninth card.  The unique
    card upgrade, GUIDs, fixed order, RNG, and scalar runtime remain explicit
    caller fixture inputs; this is not presented as a LocalSave decode.
    """

    database = Path(database)
    if (
        not isinstance(idol_card_upgrade, int)
        or isinstance(idol_card_upgrade, bool)
        or idol_card_upgrade < 0
    ):
        raise ValueError("idol_card_upgrade must be a non-negative integer")
    mode_deck = load_plan3_initial_deck(
        produce_id=NIA_TERMINAL_PRODUCE_ID,
        database=database,
    )
    profile = get_idol_profile(NIA_TERMINAL_IDOL_CARD_ID, database)
    if profile is None or not profile.produce_card_id:
        raise ValueError("NIA FKTN IdolCard must provide a mandatory produceCardId")
    if profile.plan_type != "ProducePlanType_Plan3":
        raise ValueError("NIA FKTN IdolCard must use Plan3")
    refs = (
        *mode_deck.cards,
        Plan3CardRef(profile.produce_card_id, idol_card_upgrade),
    )
    cards: list[Plan3NativeCard] = []
    for index, ref in enumerate(refs, start=1):
        # Resolve every selected version through the executable card catalog;
        # this validates identity/version without inferring runtime state.
        loaded = load_plan3_card(ref.card_id, ref.upgrade, database=database)
        cards.append(
            Plan3NativeCard(
                guid=f"{NIA_FKTN_STARTER_GUID_PREFIX}-{index:02d}",
                card_id=loaded.id,
                base_upgrade=loaded.upgrade,
                temporary_upgrade=0,
                effective_upgrade=loaded.upgrade,
                fixed_deck_order=index,
            )
        )
    return NiaDeterministicInnerFixture(
        selector=NiaInnerMasterProfileSelector(),
        ordered_deck=NiaCallerOwnedOrderedDeck(tuple(cards)),
        runtime=NiaCallerOwnedInnerRuntime(),
        beam_width=beam_width,
    )


def build_nia_terminal_fixture_from_outer_deck(
    outer_deck: tuple[DeckEntry, ...],
    *,
    selector: NiaInnerMasterProfileSelector | None = None,
    runtime: NiaCallerOwnedInnerRuntime | None = None,
    beam_width: int = 8,
    database: Path = DEFAULT_DATABASE,
) -> NiaDeterministicInnerFixture:
    """Bind an identity-complete outer deck to one deterministic inner stage.

    Outer rows must carry one response/live instance ID per card.  The caller
    still owns the stage-local fixed order and RNG; missing GUIDs are never
    synthesized here.  This is the bridge used by offline whole-route fixed
    scenarios and, later, by a LocalSave-backed route.
    """

    if not isinstance(outer_deck, tuple) or not outer_deck:
        raise TypeError("outer_deck must be a non-empty tuple")
    if any(not isinstance(entry, DeckEntry) for entry in outer_deck):
        raise TypeError("outer_deck must contain DeckEntry values")
    database = Path(database)
    cards: list[Plan3NativeCard] = []
    for entry in outer_deck:
        if len(entry.instance_ids) != entry.count:
            raise ValueError(
                "NIA inner bridge requires one instance ID per outer card: "
                f"{entry.card_id}@{entry.upgrade}"
            )
        loaded = load_plan3_card(
            entry.card_id,
            entry.upgrade,
            database=database,
        )
        for guid in entry.instance_ids:
            cards.append(
                Plan3NativeCard(
                    guid=guid,
                    card_id=loaded.id,
                    base_upgrade=loaded.upgrade,
                    temporary_upgrade=0,
                    effective_upgrade=loaded.upgrade,
                    fixed_deck_order=len(cards) + 1,
                )
            )
    return NiaDeterministicInnerFixture(
        selector=(NiaInnerMasterProfileSelector() if selector is None else selector),
        ordered_deck=NiaCallerOwnedOrderedDeck(tuple(cards)),
        runtime=(NiaCallerOwnedInnerRuntime() if runtime is None else runtime),
        beam_width=beam_width,
    )


def project_nia_fixture_outer_deck(
    fixture: NiaDeterministicInnerFixture,
) -> tuple[DeckEntry, ...]:
    """Group one identity-complete fixture without dropping card GUIDs."""

    if not isinstance(fixture, NiaDeterministicInnerFixture):
        raise TypeError("fixture must be NiaDeterministicInnerFixture")
    grouped: list[DeckEntry] = []
    for card in fixture.ordered_deck.cards:
        key = (card.card_id, card.effective_upgrade)
        index = next(
            (
                value
                for value, entry in enumerate(grouped)
                if (entry.card_id, entry.upgrade) == key
            ),
            None,
        )
        if index is None:
            grouped.append(
                DeckEntry(
                    card.card_id,
                    card.effective_upgrade,
                    instance_ids=(card.guid,),
                )
            )
        else:
            entry = grouped[index]
            grouped[index] = DeckEntry(
                entry.card_id,
                entry.upgrade,
                entry.count + 1,
                (*entry.instance_ids, card.guid),
            )
    return tuple(grouped)


def _profile_identity(
    fixture: NiaDeterministicInnerFixture,
    profile: NiaSearchTurnStartProfile,
) -> NiaSearchRuntimeIdentity:
    identity = profile.identity
    return NiaSearchRuntimeIdentity(
        phase=NIA_EXAM_MAIN_PHASE,
        step_type_value=identity.step_type_value,
        produce_id=fixture.selector.produce_id,
        step_type=identity.step_type,
        audition_number=identity.audition_number,
        group_id=identity.group_id,
    )


def _search_issues(
    result: Plan3NativeSearchResult,
    *,
    expected_turns: int,
    accepted_play_extension: NiaAcceptedPlaySearchExtension,
) -> tuple[tuple[str, ...], tuple[NiaInnerTerminalAcceptanceIssue, ...]]:
    outer_gaps: list[str] = []
    issues: list[NiaInnerTerminalAcceptanceIssue] = []
    for diagnostic in result.diagnostics:
        for gap in diagnostic.semantic_gaps:
            if (
                diagnostic.stage == "battle-horizon-scope"
                and gap == _EXPECTED_NPC_GAP
            ):
                outer_gaps.append(gap)
            else:
                issues.append(
                    _issue(
                        "nia-inner-search-semantic-gap",
                        f"{diagnostic.stage}:{gap}",
                    )
                )
    if tuple(outer_gaps) != (_EXPECTED_NPC_GAP,):
        issues.append(
            _issue(
                "nia-inner-expected-npc-boundary-missing",
                ",".join(outer_gaps),
            )
        )

    best = result.best
    if best is None:
        issues.append(_issue("nia-inner-search-no-path"))
        return tuple(outer_gaps), tuple(issues)
    if not best.complete:
        issues.append(
            _issue(
                "nia-inner-player-terminal-not-reached",
                f"turns_remaining={best.state.turns_remaining}",
            )
        )
    if best.stopped_reason:
        issues.append(_issue("nia-inner-search-stopped", best.stopped_reason))

    turn_start_traces = tuple(
        step.turn_start_extension_trace
        for step in best.steps
        if isinstance(step.turn_start_extension_trace, NiaSearchTurnStartTrace)
    )
    accepted_traces = tuple(
        step.accepted_play_extension_trace
        for step in best.steps
        if isinstance(step.accepted_play_extension_trace, NiaAcceptedPlayTrace)
    )
    if len(turn_start_traces) != expected_turns:
        issues.append(
            _issue(
                "nia-inner-turn-start-extension-incomplete",
                f"expected={expected_turns}:actual={len(turn_start_traces)}",
            )
        )
    expects_interval_listener = bool(accepted_play_extension.programs)
    if expects_interval_listener and len(accepted_traces) != len(best.actions):
        issues.append(
            _issue(
                "nia-inner-accepted-play-extension-incomplete",
                f"actions={len(best.actions)}:traces={len(accepted_traces)}",
            )
        )
    if expects_interval_listener and not any(
        trace.fires for trace in accepted_traces
    ):
        issues.append(_issue("nia-inner-play-count-interval-never-fired"))
    installed_multiplier = any(
        decision.installed
        and decision.effect_type == LESSON_VALUE_MULTIPLE_EFFECT_TYPE
        for trace in turn_start_traces
        for decision in trace.decisions
    )
    if installed_multiplier and not any(
        trace.lesson_multiplier > 1.0 for trace in turn_start_traces
    ):
        issues.append(_issue("nia-inner-lesson-multiple-never-applied"))
    return tuple(outer_gaps), tuple(issues)


def run_nia_terminal_acceptance(
    fixture: NiaDeterministicInnerFixture | None = None,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaInnerTerminalAcceptance:
    """Run one selected true Master profile with explicit synthetic runtime."""

    source = (
        build_nia_produce004_mid1_terminal_fixture(database=database)
        if fixture is None
        else fixture
    )
    if not isinstance(source, NiaDeterministicInnerFixture):
        raise TypeError("fixture must be NiaDeterministicInnerFixture or None")
    database = Path(database)
    master_dir = Path(master_dir)
    audition = _select_audition(source, master_dir=master_dir)
    profile = NiaSearchTurnStartProfile.from_audition(
        source.selector.produce_id,
        audition,
    )
    runtime_identity = _profile_identity(source, profile)
    replay = replay_nia_turn_parameter_schedule(
        audition.turn_parameter_schedule,
        source.runtime.schedule_random_state,
    )
    try:
        parameter_runtime = tuple(
            _PARAMETER_RUNTIME[value] for value in replay.parameter_types
        )
    except KeyError as error:
        raise ValueError(
            f"unsupported NIA parameter type in replay: {error.args[0]}"
        ) from error
    battle_schedule = tuple(value[0] for value in parameter_runtime)
    cards = source.ordered_deck.cards
    initial_state = Plan3State(
        round_number=source.runtime.round_number,
        turns_remaining=profile.identity.turns,
        score=source.runtime.score,
        stamina=source.runtime.stamina,
        max_stamina=source.runtime.max_stamina,
        block=source.runtime.block,
        plays_remaining=source.runtime.plays_remaining,
        awaiting_turn_start=source.runtime.awaiting_turn_start,
        concentration_change_count=(
            source.runtime.concentration_change_count
        ),
        is_battle=True,
        current_parameter_type=battle_schedule[0],
        battle_bonus_permille_vocal=(
            source.runtime.battle_bonus_permille_vocal
        ),
        battle_bonus_permille_dance=(
            source.runtime.battle_bonus_permille_dance
        ),
        battle_bonus_permille_visual=(
            source.runtime.battle_bonus_permille_visual
        ),
        judge_parameter_vocal=source.runtime.judge_parameter_vocal,
        judge_parameter_dance=source.runtime.judge_parameter_dance,
        judge_parameter_visual=source.runtime.judge_parameter_visual,
        lesson_type=parameter_runtime[0][1],
        draw_pile=tuple(card.ref for card in cards),
    )
    initial_native_state = Plan3NativeState(
        deck=cards,
        random_state=source.runtime.exam_random_state,
    )
    settings = load_plan3_exam_settings(master_dir=master_dir)
    accepted_play_extension = build_nia_accepted_play_search_extension(
        profile,
        runtime_identity,
        database=database,
        master_dir=master_dir,
    )
    search = search_plan3_native(
        initial_state,
        initial_native_state,
        beam_width=source.beam_width,
        depth=None,
        settings=settings,
        database=database,
        battle_parameter_schedule=battle_schedule,
        battle_ranking_resolved=False,
        include_skip=False,
        turn_start_extension=build_nia_turn_start_search_extension(
            profile,
            runtime_identity,
            database=database,
            master_dir=master_dir,
        ),
        accepted_play_extension=accepted_play_extension,
        initial_turn_start_extension_state=NiaSearchTurnStartRuntime(),
    )
    outer_gaps, issues = _search_issues(
        search,
        expected_turns=profile.identity.turns,
        accepted_play_extension=accepted_play_extension,
    )
    return NiaInnerTerminalAcceptance(
        fixture=source,
        audition=audition,
        profile=profile,
        parameter_schedule=replay,
        battle_parameter_schedule=battle_schedule,
        initial_state=initial_state,
        initial_native_state=initial_native_state,
        search=search,
        unresolved_outer_gaps=outer_gaps,
        issues=issues,
    )


def run_nia_produce004_mid1_terminal_acceptance(
    fixture: NiaDeterministicInnerFixture | None = None,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaInnerTerminalAcceptance:
    """Backward-compatible entry for the original produce-004 Mid1 fixture."""

    return run_nia_terminal_acceptance(
        fixture,
        database=database,
        master_dir=master_dir,
    )


__all__ = [
    "NIA_TERMINAL_AUDITION_NUMBER",
    "NIA_TERMINAL_CARD_COUNT",
    "NIA_TERMINAL_CARD_ID",
    "NIA_TERMINAL_EXAM_RANDOM_STATE",
    "NIA_FKTN_STARTER_GUID_PREFIX",
    "NIA_FKTN_UNIQUE_CARD_UPGRADE",
    "NIA_TERMINAL_GUID_PREFIX",
    "NIA_TERMINAL_IDOL_CARD_ID",
    "NIA_TERMINAL_PRODUCE_ID",
    "NIA_TERMINAL_SCHEDULE_RANDOM_STATE",
    "NIA_TERMINAL_STEP_TYPE",
    "NiaCallerOwnedInnerRuntime",
    "NiaCallerOwnedOrderedDeck",
    "NiaDeterministicInnerFixture",
    "NiaInnerInputAuthority",
    "NiaInnerMasterProfileSelector",
    "NiaInnerTerminalAcceptance",
    "NiaInnerTerminalAcceptanceIssue",
    "build_nia_produce004_mid1_terminal_fixture",
    "build_nia_produce004_fktn_starter_terminal_fixture",
    "build_nia_terminal_fixture_from_outer_deck",
    "project_nia_fixture_outer_deck",
    "run_nia_produce004_mid1_terminal_acceptance",
    "run_nia_terminal_acceptance",
]
