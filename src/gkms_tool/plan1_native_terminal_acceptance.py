"""Deterministic Plan 1 terminal acceptance fixtures.

The fixture is intentionally explicit: it uses the eleven card programs from
the pinned Master manifest, assigns stable GUIDs, and supplies an ordered Deck
instead of claiming an unobserved initial shuffle.  The runner then reuses the
native ordered-zone/card-core primitives and the deterministic advisor until
all eleven cards have settled into Grave/Lost.  This is a bounded acceptance
smoke, not an authentic LocalSave replay or an optimal strategy proof.

The smaller CardCreateId acceptance uses two true Master card programs.  Its
ordered start, RNG seed, generated GUID, and generated-card end-turn Lost
disposition are explicitly labelled deterministic caller scenario inputs.
It exercises search twice and never invents a GUID or server-owned branch.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Sequence

from .audition_local_save_state import (
    AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION,
    LocalSaveExamCard,
    empty_local_save_exam_card_runtime_state,
)
from .audition_native_ordered_zones import (
    NATIVE_ORDERED_ZONE_SCHEMA_VERSION,
    NativeEndTurnDisposition,
    NativeOrderedCardInstance,
    NativeOrderedZoneEvidenceBinding,
    NativeOrderedZoneState,
    canonical_runtime_digest_aggregate,
)
from .plan1_native_core import (
    Plan1Blocker,
    Plan1CompiledCard,
    Plan1DeckCompilation,
    Plan1ScalarState,
    compile_plan1_card,
    compile_fktn_ssr_plan1_initial_deck,
    load_plan1_native_settings,
    validate_fktn_ssr_plan1_equipped_item_runtime,
)
from .initial_regular_plan1_stage_bootstrap import (
    Plan1StageBootstrap,
    build_synthetic_plan1_terminal_stage_bootstrap,
    validate_plan1_stage_bootstrap_fixture,
)
from .plan1_native_search import (
    Plan1SearchLimits,
    Plan1SearchResult,
    choose_plan1_action,
    search_plan1_stage,
)
from .plan1_native_stage import (
    Plan1CardCreateGuidBinding,
    Plan1HandAddSupportResolver,
    Plan1NativeStageState,
    Plan1NativeStageRun,
    Plan1StageTurnBoundary,
    advance_plan1_stage_turn,
    run_plan1_stage,
)
from .produce_rollout import DeckEntry


_FIXTURE_PREFIX = "fktn-plan1"
_CARD_CREATE_EFFECT_ID = (
    "e_effect-exam_card_create_id-"
    "p_card-00-acc-0_002-0-deck_random-1_1"
)
_CARD_CREATE_SOURCE_ID = "p_card-01-men-2_110"
_CARD_CREATE_TARGET_ID = "p_card-00-acc-0_002"
_CARD_CREATE_FOLLOWUP_ID = "p_card-00-act-0_001"
_CARD_CREATE_SOURCE_GUID = "plan1-card-create-source"
_CARD_CREATE_FOLLOWUP_GUID = "plan1-card-create-followup"
_CARD_CREATE_DEFAULT_GUID = "plan1-card-create-generated"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Plan1TerminalAcceptanceLimits:
    """Transition and terminal budgets for the eleven-card smoke."""

    max_steps: int = 11
    max_turns: int = 11

    def __post_init__(self) -> None:
        for name in ("max_steps", "max_turns"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 1:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class Plan1TerminalFixture:
    """Explicit ordered eleven-card stage start and its source compilation."""

    compilation: Plan1DeckCompilation
    stage: Plan1NativeStageState
    ordered_guids: tuple[str, ...]
    ordered_card_ids: tuple[str, ...]
    blockers: tuple[Plan1Blocker, ...] = ()
    bootstrap: Plan1StageBootstrap | None = None

    @property
    def card_count(self) -> int:
        return len(self.ordered_guids)


@dataclass(frozen=True, slots=True)
class Plan1TerminalAcceptance:
    """Final bounded-run facts retained for acceptance tests and callers."""

    fixture: Plan1TerminalFixture
    run: Plan1NativeStageRun | None
    completed: bool
    terminal: bool
    blockers: tuple[Plan1Blocker, ...]
    initial_card_count: int
    final_hand_count: int
    final_deck_count: int
    final_grave_count: int
    final_lost_count: int
    initial_scalar: Plan1ScalarState
    final_scalar: Plan1ScalarState

    @property
    def score(self) -> int:
        return self.final_scalar.score

    @property
    def stamina(self) -> int:
        return self.final_scalar.stamina

    @property
    def parameter_buff_turns(self) -> int:
        return self.final_scalar.parameter_buff_turns

    @property
    def parameter_buff_multiple_per_turn_turns(self) -> int:
        return self.final_scalar.parameter_buff_multiple_per_turn_turns

    def to_dict(self) -> dict[str, object]:
        return {
            "completed": self.completed,
            "terminal": self.terminal,
            "blockers": [
                {
                    "code": blocker.code,
                    "source_id": blocker.source_id,
                    "detail": blocker.detail,
                }
                for blocker in self.blockers
            ],
            "initial_card_count": self.initial_card_count,
            "final_zone_counts": {
                "hand": self.final_hand_count,
                "deck": self.final_deck_count,
                "grave": self.final_grave_count,
                "lost": self.final_lost_count,
            },
            "initial_scalar": {
                "score": self.initial_scalar.score,
                "stamina": self.initial_scalar.stamina,
                "parameter_buff_turns": self.initial_scalar.parameter_buff_turns,
                "lesson_buff": self.initial_scalar.lesson_buff,
                "parameter_buff_multiple_per_turn_turns": (
                    self.initial_scalar.parameter_buff_multiple_per_turn_turns
                ),
                "turn": self.initial_scalar.turn,
                "play_count": self.initial_scalar.play_count,
            },
            "final_scalar": {
                "score": self.final_scalar.score,
                "stamina": self.final_scalar.stamina,
                "parameter_buff_turns": self.final_scalar.parameter_buff_turns,
                "lesson_buff": self.final_scalar.lesson_buff,
                "parameter_buff_multiple_per_turn_turns": (
                    self.final_scalar.parameter_buff_multiple_per_turn_turns
                ),
                "turn": self.final_scalar.turn,
                "play_count": self.final_scalar.play_count,
            },
        }


@dataclass(frozen=True, slots=True)
class Plan1CardCreateSearchFixture:
    """Two real Master programs plus one caller-owned generated GUID.

    The ordered start is deterministic fixture authority.  ``created_guid``
    is not derived from the exam RNG; when it is ``None`` the stage contains
    no GUID binding and search must surface the typed fail-closed blocker.
    """

    programs: tuple[Plan1CompiledCard, ...]
    stage: Plan1NativeStageState
    created_guid: str | None
    initial_guids: tuple[str, ...]
    blockers: tuple[Plan1Blocker, ...] = ()

    def __post_init__(self) -> None:
        programs = tuple(self.programs)
        if not programs or any(
            not isinstance(value, Plan1CompiledCard) for value in programs
        ):
            raise TypeError("programs must contain Plan1CompiledCard values")
        object.__setattr__(self, "programs", programs)
        if not isinstance(self.stage, Plan1NativeStageState):
            raise TypeError("stage must be Plan1NativeStageState")
        if self.created_guid is not None and (
            not isinstance(self.created_guid, str) or not self.created_guid.strip()
        ):
            raise ValueError("created_guid must be non-empty text or None")
        guids = tuple(self.initial_guids)
        if len(guids) != len(set(guids)) or any(
            not isinstance(value, str) or not value.strip() for value in guids
        ):
            raise ValueError("initial_guids must be unique non-empty text")
        object.__setattr__(self, "initial_guids", guids)
        blockers = tuple(self.blockers)
        if any(not isinstance(value, Plan1Blocker) for value in blockers):
            raise TypeError("blockers must contain Plan1Blocker values")
        object.__setattr__(self, "blockers", blockers)


@dataclass(frozen=True, slots=True)
class Plan1CardCreateSearchAcceptance:
    """Search-driven CardCreateId integration through a settled terminal."""

    fixture: Plan1CardCreateSearchFixture
    searches: tuple[Plan1SearchResult, ...]
    final_state: Plan1NativeStageState
    completed: bool
    terminal: bool
    created_drawn: bool
    blockers: tuple[Plan1Blocker, ...] = ()

    def __post_init__(self) -> None:
        searches = tuple(self.searches)
        if any(not isinstance(value, Plan1SearchResult) for value in searches):
            raise TypeError("searches must contain Plan1SearchResult values")
        object.__setattr__(self, "searches", searches)
        if not isinstance(self.final_state, Plan1NativeStageState):
            raise TypeError("final_state must be Plan1NativeStageState")
        if not isinstance(self.completed, bool) or not isinstance(self.terminal, bool):
            raise TypeError("completed and terminal must be bool")
        if not isinstance(self.created_drawn, bool):
            raise TypeError("created_drawn must be bool")
        blockers = tuple(self.blockers)
        if any(not isinstance(value, Plan1Blocker) for value in blockers):
            raise TypeError("blockers must contain Plan1Blocker values")
        object.__setattr__(self, "blockers", blockers)

    @property
    def final_guids(self) -> tuple[str, ...]:
        return tuple(card.guid for card in self.final_state.zones.card_universe)


def _fixture_cards(compilation: Plan1DeckCompilation) -> tuple[NativeOrderedCardInstance, ...]:
    """Materialize deterministic v4 card instances without a LocalSave read."""

    cards: list[NativeOrderedCardInstance] = []
    for index, program in enumerate(compilation.programs):
        guid = f"{_FIXTURE_PREFIX}-{index:02d}"
        local = LocalSaveExamCard(
            zone_order=index,
            guid=guid,
            card_id=program.card_id,
            base_upgrade=program.upgrade,
            temporary_upgrade=0,
            effective_upgrade=program.upgrade,
            support_upgrade_ids=(),
            fixed_deck_order=0,
            runtime_state=empty_local_save_exam_card_runtime_state(),
        )
        cards.append(NativeOrderedCardInstance.from_local_save(local))
    return tuple(cards)


def _ordered_indices(compilation: Plan1DeckCompilation) -> tuple[int, ...]:
    """Place the proven buff cards first, then retain manifest order."""

    by_id = {
        (program.card_id, program.upgrade): index
        for index, program in enumerate(compilation.programs)
    }
    # The manifest has duplicate common/character entries.  Resolve the first
    # card of each key for the two gate-enabling cards and retain all remaining
    # instance order; no random shuffle is synthesized.
    preferred_ids = (
        "p_card-01-ido-3_066",  # ExamParameterBuff + per-turn status
        "p_card-01-act-0_022",  # >=2-turn gate compound
        "p_card-01-act-0_005",  # ordinary parameter-buff gate
    )
    preferred: list[int] = []
    used: set[int] = set()
    for card_id in preferred_ids:
        index = by_id.get((card_id, 0))
        if index is not None and index not in used:
            preferred.append(index)
            used.add(index)
    preferred.extend(index for index in range(len(compilation.programs)) if index not in used)
    return tuple(preferred)


def build_fktn_ssr_plan1_terminal_fixture(
    *,
    compilation: Plan1DeckCompilation | None = None,
    bootstrap: Plan1StageBootstrap | None = None,
) -> Plan1TerminalFixture:
    """Build an explicit 11-card FKTN stage start from the pinned Master.

    ``compilation`` may be supplied by a caller that already loaded Master;
    when omitted this function performs the same existing Master read as the
    Plan 1 card compiler.  The ordered GUID deck is fixture data, not an RNG
    claim.
    """

    source = (
        compile_fktn_ssr_plan1_initial_deck()
        if compilation is None
        else compilation
    )
    blockers: list[Plan1Blocker] = []
    equipped_item_runtime = source.equipped_item_runtime
    if equipped_item_runtime is None:
        blockers.append(
            Plan1Blocker(
                "plan1-equipped-item-runtime-missing",
                source.manifest.before_produce_item.item_id,
                "terminal fixture",
            )
        )
    else:
        blockers.extend(
            validate_fktn_ssr_plan1_equipped_item_runtime(
                equipped_item_runtime
            )
        )
    if len(source.programs) != 11:
        blockers.append(
            Plan1Blocker(
                "plan1-terminal-card-count-mismatch",
                "FKTN-SSR-Plan1",
                f"expected=11;actual={len(source.programs)}",
            )
        )
    cards = _fixture_cards(source)
    order = _ordered_indices(source)
    if len(order) != len(cards):
        blockers.append(
            Plan1Blocker(
                "plan1-terminal-order-mismatch",
                "FKTN-SSR-Plan1",
                f"order={len(order)};cards={len(cards)}",
            )
        )
        order = tuple(range(len(cards)))
    ordered_cards = tuple(cards[index] for index in order)
    ordered_guids = tuple(card.guid for card in ordered_cards)
    ordered_card_ids = tuple(card.card_id for card in ordered_cards)
    universe = tuple(sorted(ordered_cards, key=lambda card: card.guid))
    if bootstrap is None:
        bootstrap = build_synthetic_plan1_terminal_stage_bootstrap(
            outer_deck=tuple(
                DeckEntry(
                    card_id=card.card_id,
                    upgrade=card.effective_upgrade,
                    instance_ids=(card.guid,),
                )
                for card in ordered_cards
            )
        )
    elif not isinstance(bootstrap, Plan1StageBootstrap):
        raise TypeError("bootstrap must be Plan1StageBootstrap or None")
    runtime_digest = canonical_runtime_digest_aggregate(universe)
    binding = NativeOrderedZoneEvidenceBinding(
        schema_version=NATIVE_ORDERED_ZONE_SCHEMA_VERSION,
        local_save_schema_version=AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION,
        run_id="plan1-terminal-fixture",
        step_context_id="fktn-ssr-plan1-ordered-deck",
        step_context_digest=_digest("fktn-ssr-plan1-ordered-deck"),
        session_transition_id="deterministic-fixture",
        zone_checkpoint_digest=_digest("plan1-terminal-checkpoint"),
        local_save_source_sha256=_digest("no-local-save-fixture-source"),
        local_save_evidence_digest=_digest("deterministic-fixture-evidence"),
        runtime_evidence_digest=_digest(runtime_digest),
    )
    zones = NativeOrderedZoneState(
        schema_version=NATIVE_ORDERED_ZONE_SCHEMA_VERSION,
        binding=binding,
        random_state=bootstrap.rng_before,
        card_universe=universe,
        hand=ordered_cards[:1],
        deck=ordered_cards[1:],
        grave=(),
        lost=(),
    )
    return Plan1TerminalFixture(
        compilation=source,
        stage=Plan1NativeStageState(
            scalar=bootstrap.initial_state,
            zones=zones,
            equipped_item_runtime=equipped_item_runtime,
        ),
        ordered_guids=ordered_guids,
        ordered_card_ids=ordered_card_ids,
        blockers=tuple(blockers),
        bootstrap=bootstrap,
    )


def _card_create_fixture_card(
    guid: str,
    program: Plan1CompiledCard,
    zone_order: int,
) -> NativeOrderedCardInstance:
    return NativeOrderedCardInstance.from_local_save(
        LocalSaveExamCard(
            zone_order=zone_order,
            guid=guid,
            card_id=program.card_id,
            base_upgrade=program.upgrade,
            temporary_upgrade=0,
            effective_upgrade=program.upgrade,
            support_upgrade_ids=(),
            fixed_deck_order=0,
            runtime_state=empty_local_save_exam_card_runtime_state(),
        )
    )


def build_plan1_card_create_search_fixture(
    *,
    created_guid: str | None = _CARD_CREATE_DEFAULT_GUID,
) -> Plan1CardCreateSearchFixture:
    """Build the minimal true-Master CardCreateId search fixture.

    The two initial GUIDs, ordered zones, RNG seed, and optional generated GUID
    are deterministic caller scenario facts.  Passing ``created_guid=None``
    intentionally omits GUID authority so the search boundary can demonstrate
    its typed fail-closed result.
    """

    source = compile_plan1_card(_CARD_CREATE_SOURCE_ID, 0)
    followup = compile_plan1_card(_CARD_CREATE_FOLLOWUP_ID, 0)
    programs = (source, followup)
    blockers = tuple(
        dict.fromkeys(blocker for program in programs for blocker in program.blockers)
    )
    source_card = _card_create_fixture_card(_CARD_CREATE_SOURCE_GUID, source, 0)
    followup_card = _card_create_fixture_card(
        _CARD_CREATE_FOLLOWUP_GUID, followup, 1
    )
    universe = tuple(sorted((source_card, followup_card), key=lambda card: card.guid))
    runtime_digest = canonical_runtime_digest_aggregate(universe)
    binding = NativeOrderedZoneEvidenceBinding(
        schema_version=NATIVE_ORDERED_ZONE_SCHEMA_VERSION,
        local_save_schema_version=AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION,
        run_id="plan1-card-create-search-fixture",
        step_context_id="plan1-card-create-search-ordered-zones",
        step_context_digest=_digest("plan1-card-create-search-ordered-zones"),
        session_transition_id="deterministic-caller-scenario",
        zone_checkpoint_digest=_digest("plan1-card-create-search-checkpoint"),
        local_save_source_sha256=_digest("no-local-save-card-create-source"),
        local_save_evidence_digest=_digest("card-create-search-fixture-evidence"),
        runtime_evidence_digest=_digest(runtime_digest),
    )
    zones = NativeOrderedZoneState(
        schema_version=NATIVE_ORDERED_ZONE_SCHEMA_VERSION,
        binding=binding,
        random_state=0x12345678,
        card_universe=universe,
        hand=(source_card,),
        deck=(followup_card,),
        grave=(),
        lost=(),
    )
    scalar = Plan1ScalarState(
        score=0,
        stamina=100,
        max_stamina=100,
        block=0,
        parameter_buff_turns=0,
        lesson_buff=0,
        turn=1,
        plays_remaining=1,
        play_count=0,
    )
    guid_bindings = (
        ()
        if created_guid is None
        else (
            Plan1CardCreateGuidBinding(
                source_guid=_CARD_CREATE_SOURCE_GUID,
                source_play_count=0,
                effect_id=_CARD_CREATE_EFFECT_ID,
                effect_index=3,
                guid_tokens=(created_guid,),
            ),
        )
    )
    stage = Plan1NativeStageState(
        scalar=scalar,
        zones=zones,
        card_create_bindings=guid_bindings,
    )
    return Plan1CardCreateSearchFixture(
        programs=programs,
        stage=stage,
        created_guid=created_guid,
        initial_guids=(_CARD_CREATE_SOURCE_GUID, _CARD_CREATE_FOLLOWUP_GUID),
        blockers=blockers,
    )


def _card_create_acceptance_result(
    fixture: Plan1CardCreateSearchFixture,
    searches: Sequence[Plan1SearchResult],
    final_state: Plan1NativeStageState,
    blockers: Sequence[Plan1Blocker],
    *,
    created_drawn: bool,
) -> Plan1CardCreateSearchAcceptance:
    values = tuple(dict.fromkeys(blockers))
    terminal = (
        not values
        and not final_state.zones.hand
        and not final_state.zones.deck
        and final_state.zones.pending_played is None
    )
    return Plan1CardCreateSearchAcceptance(
        fixture=fixture,
        searches=tuple(searches),
        final_state=final_state,
        completed=terminal,
        terminal=terminal,
        created_drawn=created_drawn,
        blockers=values,
    )


def run_plan1_card_create_search_acceptance(
    *,
    fixture: Plan1CardCreateSearchFixture | None = None,
    search_limits: Plan1SearchLimits = Plan1SearchLimits(
        max_depth=1,
        max_nodes=8,
    ),
) -> Plan1CardCreateSearchAcceptance:
    """Search, create, draw/dispose, resume search, and settle exactly once.

    The generated trouble card's ``LOST`` end-turn disposition is part of the
    fixture's deterministic caller scenario; it is supplied to the existing
    exact boundary reducer rather than inferred from card type.
    """

    if fixture is None:
        fixture = build_plan1_card_create_search_fixture()
    elif not isinstance(fixture, Plan1CardCreateSearchFixture):
        raise TypeError("fixture must be Plan1CardCreateSearchFixture or None")
    if not isinstance(search_limits, Plan1SearchLimits):
        raise TypeError("search_limits must be Plan1SearchLimits")
    if fixture.blockers:
        return _card_create_acceptance_result(
            fixture,
            (),
            fixture.stage,
            fixture.blockers,
            created_drawn=False,
        )

    settings = load_plan1_native_settings()
    first = search_plan1_stage(
        fixture.stage,
        fixture.programs,
        settings=settings,
        limits=search_limits,
    )
    searches: list[Plan1SearchResult] = [first]
    if first.blockers or len(first.actions) != 1:
        blockers = first.blockers or (
            Plan1Blocker(
                "plan1-card-create-search-no-source-action",
                _CARD_CREATE_SOURCE_ID,
            ),
        )
        return _card_create_acceptance_result(
            fixture,
            searches,
            first.final_state,
            blockers,
            created_drawn=False,
        )

    created_guid = fixture.created_guid
    if created_guid is None:  # Defensive: missing authority must block above.
        return _card_create_acceptance_result(
            fixture,
            searches,
            first.final_state,
            (
                Plan1Blocker(
                    "plan1-card-create-guid-binding-missing",
                    _CARD_CREATE_EFFECT_ID,
                ),
            ),
            created_drawn=False,
        )
    after_create = first.final_state
    created_in_deck = tuple(card.guid for card in after_create.zones.deck)
    if not created_in_deck or created_in_deck[0] != created_guid:
        return _card_create_acceptance_result(
            fixture,
            searches,
            after_create,
            (
                Plan1Blocker(
                    "plan1-card-create-search-placement-mismatch",
                    _CARD_CREATE_EFFECT_ID,
                    str(created_in_deck),
                ),
            ),
            created_drawn=False,
        )

    after_draw = advance_plan1_stage_turn(
        after_create,
        Plan1StageTurnBoundary(draw_count=1, plays_remaining=1),
    )
    if after_draw.blockers:
        return _card_create_acceptance_result(
            fixture,
            searches,
            after_draw,
            after_draw.blockers,
            created_drawn=False,
        )
    created_drawn = tuple(card.guid for card in after_draw.zones.hand) == (
        created_guid,
    )
    if not created_drawn:
        return _card_create_acceptance_result(
            fixture,
            searches,
            after_draw,
            (
                Plan1Blocker(
                    "plan1-card-create-search-draw-mismatch",
                    created_guid,
                ),
            ),
            created_drawn=False,
        )

    after_disposition = advance_plan1_stage_turn(
        after_draw,
        Plan1StageTurnBoundary.from_mapping(
            draw_count=1,
            plays_remaining=1,
            dispositions={created_guid: NativeEndTurnDisposition.LOST},
        ),
    )
    if after_disposition.blockers:
        return _card_create_acceptance_result(
            fixture,
            searches,
            after_disposition,
            after_disposition.blockers,
            created_drawn=True,
        )

    second = search_plan1_stage(
        after_disposition,
        fixture.programs,
        settings=settings,
        limits=search_limits,
    )
    searches.append(second)
    blockers: list[Plan1Blocker] = list(second.blockers)
    final = second.final_state
    if len(second.actions) != 1:
        blockers.append(
            Plan1Blocker(
                "plan1-card-create-search-no-followup-action",
                _CARD_CREATE_FOLLOWUP_ID,
            )
        )

    expected_guids = {*fixture.initial_guids, created_guid}
    final_universe = {card.guid for card in final.zones.card_universe}
    final_positioned = {card.guid for card in final.zones.all_positioned_cards}
    if final_universe != expected_guids or final_positioned != expected_guids:
        blockers.append(
            Plan1Blocker(
                "plan1-card-create-search-identity-conservation-failed",
                created_guid,
                f"universe={sorted(final_universe)};positioned={sorted(final_positioned)}",
            )
        )
    if tuple(card.guid for card in final.zones.lost) != (created_guid,):
        blockers.append(
            Plan1Blocker(
                "plan1-card-create-search-created-settlement-mismatch",
                created_guid,
            )
        )
    if final.scalar.play_count != 2:
        blockers.append(
            Plan1Blocker(
                "plan1-card-create-search-play-count-mismatch",
                _CARD_CREATE_SOURCE_ID,
                f"expected=2;actual={final.scalar.play_count}",
            )
        )
    return _card_create_acceptance_result(
        fixture,
        searches,
        final,
        blockers,
        created_drawn=True,
    )


def _acceptance_blocker(code: str, detail: str) -> Plan1Blocker:
    return Plan1Blocker(code, "FKTN-SSR-Plan1-terminal", detail)


def run_fktn_ssr_plan1_terminal_acceptance(
    *,
    compilation: Plan1DeckCompilation | None = None,
    fixture: Plan1TerminalFixture | None = None,
    limits: Plan1TerminalAcceptanceLimits = Plan1TerminalAcceptanceLimits(),
    hand_add_support_resolver: Plan1HandAddSupportResolver | None = None,
) -> Plan1TerminalAcceptance:
    """Run a deterministic 11-card fixture to a single terminal checkpoint.

    ``fixture`` is caller-authoritative when supplied; this is the injection
    seam used by the Initial Regular inner adapter for ordered deck/runtime
    scenarios.  ``hand_add_support_resolver`` is likewise optional and is
    threaded into stage/search execution without changing the default fixed
    smoke.  The default path still builds the explicit Master manifest fixture
    and never infers an initial shuffle.
    """

    if not isinstance(limits, Plan1TerminalAcceptanceLimits):
        raise TypeError("limits must be Plan1TerminalAcceptanceLimits")
    if fixture is None:
        fixture = build_fktn_ssr_plan1_terminal_fixture(compilation=compilation)
    elif not isinstance(fixture, Plan1TerminalFixture):
        raise TypeError("fixture must be Plan1TerminalFixture or None")
    initial_scalar = fixture.stage.scalar
    bootstrap_blockers = tuple(
        Plan1Blocker(issue.code, issue.field, issue.detail)
        for issue in validate_plan1_stage_bootstrap_fixture(
            fixture.stage, fixture.bootstrap
        )
    )
    preflight_blockers = (*fixture.blockers, *bootstrap_blockers)
    if preflight_blockers:
        return Plan1TerminalAcceptance(
            fixture,
            None,
            False,
            False,
            preflight_blockers,
            fixture.card_count,
            len(fixture.stage.zones.hand),
            len(fixture.stage.zones.deck),
            len(fixture.stage.zones.grave),
            len(fixture.stage.zones.lost),
            initial_scalar,
            initial_scalar,
        )
    if limits.max_steps != fixture.card_count:
        blocker = _acceptance_blocker(
            "plan1-terminal-transition-limit",
            f"max_steps={limits.max_steps};card_count={fixture.card_count}",
        )
        return Plan1TerminalAcceptance(
            fixture,
            None,
            False,
            False,
            (blocker,),
            fixture.card_count,
            len(fixture.stage.zones.hand),
            len(fixture.stage.zones.deck),
            0,
            0,
            initial_scalar,
            initial_scalar,
        )
    if limits.max_turns < fixture.card_count:
        blocker = _acceptance_blocker(
            "plan1-terminal-turn-limit",
            f"max_turns={limits.max_turns};card_count={fixture.card_count}",
        )
        return Plan1TerminalAcceptance(
            fixture,
            None,
            False,
            False,
            (blocker,),
            fixture.card_count,
            len(fixture.stage.zones.hand),
            len(fixture.stage.zones.deck),
            0,
            0,
            initial_scalar,
            initial_scalar,
        )
    boundaries = tuple(
        Plan1StageTurnBoundary(draw_count=1, plays_remaining=1)
        for _ in range(fixture.card_count - 1)
    )
    try:
        settings = load_plan1_native_settings()
        run = run_plan1_stage(
            fixture.stage,
            fixture.compilation,
            boundaries,
            settings=settings,
            choose_action=choose_plan1_action,
            hand_add_support_resolver=hand_add_support_resolver,
        )
    except (TypeError, ValueError, OverflowError) as error:
        blocker = _acceptance_blocker("plan1-terminal-run-failed", str(error))
        return Plan1TerminalAcceptance(
            fixture,
            None,
            False,
            False,
            (blocker,),
            fixture.card_count,
            len(fixture.stage.zones.hand),
            len(fixture.stage.zones.deck),
            0,
            0,
            initial_scalar,
            initial_scalar,
        )
    final = run.final
    blockers = list(run.blockers)
    if len(final.steps) > limits.max_steps:
        blockers.append(
            _acceptance_blocker(
                "plan1-terminal-transition-limit-exceeded",
                f"steps={len(final.steps)};limit={limits.max_steps}",
            )
        )
    zone_counts = {
        "hand": len(final.zones.hand),
        "deck": len(final.zones.deck),
        "grave": len(final.zones.grave),
        "lost": len(final.zones.lost),
    }
    if zone_counts["hand"] or zone_counts["deck"] or final.zones.pending_played is not None:
        blockers.append(
            _acceptance_blocker(
                "plan1-terminal-zones-not-settled",
                str(zone_counts),
            )
        )
    if sum(zone_counts.values()) != fixture.card_count:
        blockers.append(
            _acceptance_blocker(
                "plan1-terminal-card-conservation-failed",
                f"initial={fixture.card_count};final={sum(zone_counts.values())}",
            )
        )
    if final.scalar.play_count != fixture.card_count:
        blockers.append(
            _acceptance_blocker(
                "plan1-terminal-play-count-mismatch",
                f"expected={fixture.card_count};actual={final.scalar.play_count}",
            )
        )
    if (
        final.equipped_item_runtime is None
        or final.equipped_item_runtime.remaining_uses != 0
    ):
        blockers.append(
            _acceptance_blocker(
                "plan1-terminal-equipped-item-not-consumed",
                "missing"
                if final.equipped_item_runtime is None
                else f"remaining={final.equipped_item_runtime.remaining_uses}",
            )
        )
    terminal = not blockers and not final.zones.hand and not final.zones.deck
    return Plan1TerminalAcceptance(
        fixture,
        run,
        completed=not blockers and run.completed,
        terminal=terminal,
        blockers=tuple(blockers),
        initial_card_count=fixture.card_count,
        final_hand_count=zone_counts["hand"],
        final_deck_count=zone_counts["deck"],
        final_grave_count=zone_counts["grave"],
        final_lost_count=zone_counts["lost"],
        initial_scalar=initial_scalar,
        final_scalar=final.scalar,
    )


# Concise aliases for callers that use “acceptance” as the operation name.
build_plan1_terminal_fixture = build_fktn_ssr_plan1_terminal_fixture
accept_fktn_ssr_plan1_terminal = run_fktn_ssr_plan1_terminal_acceptance


__all__ = [
    "Plan1CardCreateSearchAcceptance",
    "Plan1CardCreateSearchFixture",
    "Plan1TerminalAcceptance",
    "Plan1TerminalAcceptanceLimits",
    "Plan1TerminalFixture",
    "accept_fktn_ssr_plan1_terminal",
    "build_fktn_ssr_plan1_terminal_fixture",
    "build_plan1_card_create_search_fixture",
    "build_plan1_terminal_fixture",
    "run_fktn_ssr_plan1_terminal_acceptance",
    "run_plan1_card_create_search_acceptance",
]
