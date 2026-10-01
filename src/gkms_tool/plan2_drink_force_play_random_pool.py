"""Exact detached RandomPool selection for the Plan2 rainbow drink.

Android resolves this ForcePlay shape through the same RandomPool search
primitive as ``ExamCardCreateSearch``.  The selected card is only a temporary
UsePool object, though: its GUID is caller-owned, its exam RNG state commits,
and none of the synthetic Hand/Deck placement survives the selection step.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3

from .master_db import DEFAULT_DATABASE
from .plan3_card_create_search import (
    DEFAULT_RESEARCH_DIR,
    CardCreateSearchChanceInput,
    CardCreateSearchCondition,
    CardCreateSearchEffectContract,
    CardCreateSearchError,
    _make_effect,
    _make_pool,
    _make_search,
    _parse_card_plan_types,
    _parse_pool_source,
    apply_card_create_search,
)
from .plan3_force_play_card_search import (
    ForcePlayCardSearchError,
    load_force_play_target_card_master,
    resolve_force_play_card_search_contract,
)
from .plan3_native_state import Plan3NativeCard, Plan3NativeState


RUSH_IDOL_UNIQUE_FORCE_PLAY_EFFECT_ID = (
    "e_effect-exam_force_play_card_search-"
    "p_card_search-random-random_pool-"
    "p_random_pool-rush_idol_unique_set-ssr-upgrade_1-1-"
    "hand-random-1_1"
)
RUSH_IDOL_UNIQUE_SEARCH_ID = (
    "p_card_search-random-random_pool-"
    "p_random_pool-rush_idol_unique_set-ssr-upgrade_1-1"
)
RUSH_IDOL_UNIQUE_POOL_ID = (
    "p_random_pool-rush_idol_unique_set-ssr-upgrade_1"
)

_EXPECTED_AUTHORITATIVE_CANDIDATES = 26
_EXPECTED_AUTHORITATIVE_TICKETS = 104
_EXPECTED_PLAN2_CANDIDATES = 15
_EXPECTED_PLAN2_TICKETS = 60
_EXPECTED_EMPTY_WHITELIST_RNG_CALLS = 62


class Plan2DrinkForcePlayRandomPoolError(ValueError):
    """Stable fail-closed error for this one exact drink effect."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


@dataclass(frozen=True, slots=True)
class Plan2DrinkForcePlayRandomPoolProgram:
    """Validated Master graph plus a selector-only RandomPool contract."""

    effect_id: str
    search_id: str
    pool_id: str
    selector: CardCreateSearchEffectContract
    authoritative_candidate_count: int
    authoritative_ticket_count: int
    plan2_candidate_count: int
    plan2_ticket_count: int
    empty_whitelist_rng_call_count: int

    def __post_init__(self) -> None:
        if self.effect_id != RUSH_IDOL_UNIQUE_FORCE_PLAY_EFFECT_ID:
            raise Plan2DrinkForcePlayRandomPoolError("effect-id-drift")
        if self.search_id != RUSH_IDOL_UNIQUE_SEARCH_ID:
            raise Plan2DrinkForcePlayRandomPoolError("search-id-drift")
        if self.pool_id != RUSH_IDOL_UNIQUE_POOL_ID:
            raise Plan2DrinkForcePlayRandomPoolError("pool-id-drift")
        if not isinstance(self.selector, CardCreateSearchEffectContract):
            raise TypeError("selector must be a CardCreateSearchEffectContract")
        if (
            self.selector.effect_id != self.effect_id
            or self.selector.search_id != self.search_id
            or self.selector.pool.pool_id != self.pool_id
        ):
            raise Plan2DrinkForcePlayRandomPoolError("selector-link-drift")
        observed = (
            self.authoritative_candidate_count,
            self.authoritative_ticket_count,
            self.plan2_candidate_count,
            self.plan2_ticket_count,
            self.empty_whitelist_rng_call_count,
        )
        expected = (
            _EXPECTED_AUTHORITATIVE_CANDIDATES,
            _EXPECTED_AUTHORITATIVE_TICKETS,
            _EXPECTED_PLAN2_CANDIDATES,
            _EXPECTED_PLAN2_TICKETS,
            _EXPECTED_EMPTY_WHITELIST_RNG_CALLS,
        )
        if observed != expected:
            raise Plan2DrinkForcePlayRandomPoolError(
                "pool-cardinality-drift",
                f"observed={observed!r}:expected={expected!r}",
            )


@dataclass(frozen=True, slots=True)
class Plan2DrinkForcePlayRandomPoolSelection:
    """One detached card identity plus the committed exam-RNG suffix."""

    card: Plan3NativeCard
    random_state_before: int
    random_state_after: int
    rng_call_count: int
    selected_ticket_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.card, Plan3NativeCard):
            raise TypeError("card must be Plan3NativeCard")
        for name in (
            "random_state_before",
            "random_state_after",
            "rng_call_count",
            "selected_ticket_index",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TypeError(f"{name} must be a non-negative integer")


def _validate_search(
    observed: object,
    expected: CardCreateSearchCondition,
) -> None:
    """Cross-check the normalized search row against the exact selector."""

    for expected_name in expected.__dataclass_fields__:
        if expected_name == "source_links":
            continue
        observed_name = "id" if expected_name == "search_id" else expected_name
        if getattr(observed, observed_name) != getattr(expected, expected_name):
            raise Plan2DrinkForcePlayRandomPoolError(
                "search-shape-drift",
                expected_name,
            )


def load_plan2_drink_force_play_random_pool_program(
    database: Path = DEFAULT_DATABASE,
    *,
    research_dir: Path = DEFAULT_RESEARCH_DIR,
) -> Plan2DrinkForcePlayRandomPoolProgram:
    """Load and prove the complete current-Master 013 selection graph."""

    database = Path(database)
    research_dir = Path(research_dir)
    try:
        with sqlite3.connect(database) as connection:
            connection.row_factory = sqlite3.Row
            effect_row = connection.execute(
                "SELECT * FROM effect WHERE id = ?",
                (RUSH_IDOL_UNIQUE_FORCE_PLAY_EFFECT_ID,),
            ).fetchone()
    except sqlite3.Error as error:
        raise Plan2DrinkForcePlayRandomPoolError(
            "effect-read-failed",
            str(error),
        ) from error
    if effect_row is None:
        raise Plan2DrinkForcePlayRandomPoolError("effect-row-missing")

    try:
        force_contract = resolve_force_play_card_search_contract(
            effect_row,
            database=database,
        )
    except ForcePlayCardSearchError as error:
        raise Plan2DrinkForcePlayRandomPoolError(
            "force-play-contract-drift",
            str(error),
        ) from error
    row = force_contract.row
    if (
        row.effect_id != RUSH_IDOL_UNIQUE_FORCE_PLAY_EFFECT_ID
        or row.search_id != RUSH_IDOL_UNIQUE_SEARCH_ID
        or row.move_position_type != "ProduceCardMovePositionType_Hand"
        or row.pick_range_type != "ProducePickRangeType_Random"
        or row.pick_count_type != "ProducePickCountType_Unknown"
        or row.pick_count_reference_search_id
        or (row.pick_count_min, row.pick_count_max) != (1, 1)
    ):
        raise Plan2DrinkForcePlayRandomPoolError("force-play-shape-drift")

    expected_search = _make_search(
        RUSH_IDOL_UNIQUE_SEARCH_ID,
        RUSH_IDOL_UNIQUE_POOL_ID,
        1,
        row.source_slot,
    )
    _validate_search(force_contract.search, expected_search)

    try:
        produce_rows = _parse_pool_source(
            research_dir / "ProduceCardPool.yaml",
            (RUSH_IDOL_UNIQUE_POOL_ID,),
            random_rows=False,
        )[RUSH_IDOL_UNIQUE_POOL_ID]
        legacy_rows = _parse_pool_source(
            research_dir / "ProduceCardRandomPool.yaml",
            (RUSH_IDOL_UNIQUE_POOL_ID,),
            random_rows=True,
        )[RUSH_IDOL_UNIQUE_POOL_ID]
        card_ids = tuple(
            dict.fromkeys(
                card_id
                for rows in (produce_rows, legacy_rows)
                for card_id, _upgrade, _ratio in rows
            )
        )
        plan_types = _parse_card_plan_types(
            research_dir / "ProduceCard.yaml",
            card_ids,
        )
        pool = _make_pool(
            RUSH_IDOL_UNIQUE_POOL_ID,
            produce_rows,
            legacy_rows,
            plan_types,
        )
    except CardCreateSearchError as error:
        raise Plan2DrinkForcePlayRandomPoolError(
            "pool-graph-drift",
            str(error),
        ) from error

    authoritative = pool.authoritative_candidates
    plan2 = tuple(
        candidate
        for candidate in authoritative
        if candidate.plan_type == "ProducePlanType_Plan2"
    )
    candidate_count = len(authoritative)
    ticket_count = sum(candidate.ratio for candidate in authoritative)
    plan2_candidate_count = len(plan2)
    plan2_ticket_count = sum(candidate.ratio for candidate in plan2)
    if (
        candidate_count,
        ticket_count,
        plan2_candidate_count,
        plan2_ticket_count,
    ) != (
        _EXPECTED_AUTHORITATIVE_CANDIDATES,
        _EXPECTED_AUTHORITATIVE_TICKETS,
        _EXPECTED_PLAN2_CANDIDATES,
        _EXPECTED_PLAN2_TICKETS,
    ):
        raise Plan2DrinkForcePlayRandomPoolError("pool-cardinality-drift")

    # Native excludes recursive ForcePlay targets.  All current authoritative
    # candidates are proven non-recursive, so the shared RandomPool selector
    # can preserve the exact ticket/RNG order without adding another filter.
    try:
        for index, candidate in enumerate(authoritative):
            master = load_force_play_target_card_master(
                Plan3NativeCard(
                    guid=f"force-play-pool-proof:{index}",
                    card_id=candidate.card_id,
                    base_upgrade=candidate.upgrade_count,
                    temporary_upgrade=0,
                    effective_upgrade=candidate.upgrade_count,
                ),
                database,
            )
            if master.has_recursive_force_play:
                raise Plan2DrinkForcePlayRandomPoolError(
                    "recursive-pool-candidate",
                    f"{candidate.card_id}+{candidate.upgrade_count}",
                )
    except ForcePlayCardSearchError as error:
        raise Plan2DrinkForcePlayRandomPoolError(
            "pool-recursion-proof-failed",
            str(error),
        ) from error

    selector = _make_effect(
        (
            row.effect_id,
            row.source_slot,
            row.search_id,
            row.move_position_type,
            row.pick_range_type,
            row.pick_count_reference_search_id,
            row.pick_count_type,
            row.pick_count_min,
            row.pick_count_max,
        ),
        {expected_search.search_id: expected_search},
        {pool.pool_id: pool},
    )
    return Plan2DrinkForcePlayRandomPoolProgram(
        effect_id=row.effect_id,
        search_id=row.search_id,
        pool_id=pool.pool_id,
        selector=selector,
        authoritative_candidate_count=candidate_count,
        authoritative_ticket_count=ticket_count,
        plan2_candidate_count=plan2_candidate_count,
        plan2_ticket_count=plan2_ticket_count,
        empty_whitelist_rng_call_count=_EXPECTED_EMPTY_WHITELIST_RNG_CALLS,
    )


def select_plan2_drink_force_play_random_pool_card(
    state: Plan3NativeState,
    program: Plan2DrinkForcePlayRandomPoolProgram,
    *,
    guid_token: str,
    plan_ignore_card_ids: tuple[str, ...],
    hand_limit: int,
) -> Plan2DrinkForcePlayRandomPoolSelection:
    """Select one card exactly, discarding the selector's placement mutation."""

    if not isinstance(state, Plan3NativeState):
        raise TypeError("state must be Plan3NativeState")
    if not isinstance(program, Plan2DrinkForcePlayRandomPoolProgram):
        raise TypeError("program must be a typed force-play RandomPool program")
    if not isinstance(guid_token, str) or not guid_token:
        raise Plan2DrinkForcePlayRandomPoolError("guid-token-missing")
    if not isinstance(plan_ignore_card_ids, tuple):
        raise TypeError("plan_ignore_card_ids must be an exact tuple")
    try:
        execution = apply_card_create_search(
            state,
            program.selector,
            execution_input=CardCreateSearchChanceInput(
                plan_type="ProducePlanType_Plan2",
                plan_ignore_card_ids=plan_ignore_card_ids,
                hand_limit=hand_limit,
                guid_tokens=(guid_token,),
            ),
        )
    except CardCreateSearchError as error:
        raise Plan2DrinkForcePlayRandomPoolError(
            "random-pool-selection-failed",
            str(error),
        ) from error
    if not execution.resolved:
        detail = ",".join(
            f"{value.field}:{value.reason}"
            for value in execution.trace.unresolved_inputs
        )
        raise Plan2DrinkForcePlayRandomPoolError(
            "random-pool-selection-unresolved",
            detail,
        )
    if (
        len(execution.trace.selected_card_ids) != 1
        or len(execution.trace.selected_ticket_indices) != 1
        or execution.trace.guid_tokens != (guid_token,)
    ):
        raise Plan2DrinkForcePlayRandomPoolError(
            "random-pool-selection-count-drift"
        )
    card = execution.after.card_by_guid(guid_token)
    return Plan2DrinkForcePlayRandomPoolSelection(
        card=card,
        random_state_before=execution.trace.random_state_before,
        random_state_after=execution.trace.random_state_after,
        rng_call_count=execution.trace.rng_call_count,
        selected_ticket_index=execution.trace.selected_ticket_indices[0],
    )


__all__ = [
    "Plan2DrinkForcePlayRandomPoolError",
    "Plan2DrinkForcePlayRandomPoolProgram",
    "Plan2DrinkForcePlayRandomPoolSelection",
    "RUSH_IDOL_UNIQUE_FORCE_PLAY_EFFECT_ID",
    "RUSH_IDOL_UNIQUE_POOL_ID",
    "RUSH_IDOL_UNIQUE_SEARCH_ID",
    "load_plan2_drink_force_play_random_pool_program",
    "select_plan2_drink_force_play_random_pool_card",
]
