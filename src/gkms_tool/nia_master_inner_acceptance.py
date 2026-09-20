"""Bounded offline acceptance for all N.I.A. Master player-side auditions.

The produce-005 outer weekly schedule is server/runtime state and is not
present as a fixed Master route.  This module therefore proves only the three
real player-side audition profiles against a GUID-complete FKTN starter deck;
it deliberately does not invent an outer 26-week calendar or claim LocalSave
coverage.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .audition_rules import FINAL, MID1, MID2
from .master_db import DEFAULT_DATABASE
from .nia_inner_terminal_acceptance import (
    NiaCallerOwnedInnerRuntime,
    NiaInnerMasterProfileSelector,
    NiaInnerTerminalAcceptance,
    build_nia_produce004_fktn_starter_terminal_fixture,
    build_nia_terminal_fixture_from_outer_deck,
    project_nia_fixture_outer_deck,
    run_nia_terminal_acceptance,
)
from .nia_static_adapter import DEFAULT_MASTER_DIR
from .nia_static_inventory import (
    NiaStaticAuditionStage,
    NiaStaticInventory,
    build_nia_static_inventory,
)
from .produce_rollout import DeckEntry


NIA_MASTER_PRODUCE_ID: Final = "produce-005"
NIA_MASTER_FKTN_IDOL_CARD_ID: Final = "i_card-fktn-3-011"
NIA_MASTER_STAGE_WEEKS: Final = ((9, MID1), (17, MID2), (26, FINAL))


@dataclass(frozen=True, slots=True)
class NiaMasterInnerStageAcceptance:
    week: int
    catalog: NiaStaticAuditionStage
    acceptance: NiaInnerTerminalAcceptance
    caller_battle_bonus_permille: tuple[int, int, int]

    def __post_init__(self) -> None:
        expected = dict(NIA_MASTER_STAGE_WEEKS).get(self.week)
        if expected != self.catalog.step_type:
            raise ValueError("NIA Master stage/week identity is inconsistent")
        if not self.acceptance.deterministic_player_inner_executable:
            raise ValueError("NIA Master player-side acceptance is incomplete")
        if self.acceptance.best is None or not self.acceptance.best.complete:
            raise ValueError("NIA Master player-side search did not terminate")

    @property
    def score(self) -> int:
        assert self.acceptance.best is not None
        return self.acceptance.best.state.score

    @property
    def stamina(self) -> int:
        assert self.acceptance.best is not None
        return self.acceptance.best.state.stamina


@dataclass(frozen=True, slots=True)
class NiaMasterInnerAcceptanceResult:
    inventory: NiaStaticInventory
    starter_deck: tuple[DeckEntry, ...]
    stages: tuple[NiaMasterInnerStageAcceptance, ...]
    authority_description: str
    authentic_nia_local_save: bool = False
    outer_schedule_executable: bool = False

    def __post_init__(self) -> None:
        if self.inventory.produce_id != NIA_MASTER_PRODUCE_ID:
            raise ValueError("NIA Master result has the wrong produce ID")
        if tuple((value.week, value.catalog.step_type) for value in self.stages) != (
            NIA_MASTER_STAGE_WEEKS
        ):
            raise ValueError("NIA Master result must contain all three stages")
        if any(len(entry.instance_ids) != entry.count for entry in self.starter_deck):
            raise ValueError("NIA Master starter deck lost card GUID identity")
        if self.authentic_nia_local_save or self.outer_schedule_executable:
            raise ValueError("bounded NIA Master result cannot claim wider coverage")

    @property
    def player_inner_complete(self) -> bool:
        return all(
            value.acceptance.deterministic_player_inner_executable
            for value in self.stages
        )

    @property
    def scores(self) -> tuple[int, ...]:
        return tuple(value.score for value in self.stages)


def _catalog(
    inventory: NiaStaticInventory,
    step_type: str,
) -> NiaStaticAuditionStage:
    matches = tuple(
        value
        for value in inventory.auditions
        if value.step_type == step_type and value.number == 1
    )
    if len(matches) != 1:
        raise RuntimeError(f"NIA Master stage catalog is ambiguous: {step_type}")
    return matches[0]


def run_nia_master_fktn_inner_acceptance(
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaMasterInnerAcceptanceResult:
    """Execute Mid1, Mid2, and Final without inventing the outer schedule."""

    database = Path(database)
    master_dir = Path(master_dir)
    inventory = build_nia_static_inventory(
        NIA_MASTER_FKTN_IDOL_CARD_ID,
        produce_id=NIA_MASTER_PRODUCE_ID,
        database=database,
        master_dir=master_dir,
    )
    starter = build_nia_produce004_fktn_starter_terminal_fixture(
        database=database,
    )
    outer_deck = project_nia_fixture_outer_deck(starter)
    stages = []
    for index, (week, step_type) in enumerate(NIA_MASTER_STAGE_WEEKS, start=1):
        bonus = (10000, 10000, 10000)
        fixture = build_nia_terminal_fixture_from_outer_deck(
            outer_deck,
            selector=NiaInnerMasterProfileSelector(
                idol_card_id=NIA_MASTER_FKTN_IDOL_CARD_ID,
                produce_id=NIA_MASTER_PRODUCE_ID,
                step_type=step_type,
                audition_number=1,
            ),
            runtime=NiaCallerOwnedInnerRuntime(
                schedule_random_state=0x31000000 + index,
                exam_random_state=0x41000000 + index,
                stamina=27,
                max_stamina=27,
                concentration_change_count=2,
                battle_bonus_permille_vocal=bonus[0],
                battle_bonus_permille_dance=bonus[1],
                battle_bonus_permille_visual=bonus[2],
            ),
            beam_width=8,
            database=database,
        )
        acceptance = run_nia_terminal_acceptance(
            fixture,
            database=database,
            master_dir=master_dir,
        )
        stages.append(
            NiaMasterInnerStageAcceptance(
                week,
                _catalog(inventory, step_type),
                acceptance,
                bonus,
            )
        )
    return NiaMasterInnerAcceptanceResult(
        inventory=inventory,
        starter_deck=outer_deck,
        stages=tuple(stages),
        authority_description=(
            "PC Master/metadata + Android native player-side semantics; "
            "GUID-complete FKTN starter deck and caller-owned RNG/bonus roots; "
            "produce-005 weekly schedule, rewards, NPC terminal tracks, and "
            "authentic LocalSave remain unresolved"
        ),
    )


__all__ = [
    "NIA_MASTER_FKTN_IDOL_CARD_ID",
    "NIA_MASTER_PRODUCE_ID",
    "NIA_MASTER_STAGE_WEEKS",
    "NiaMasterInnerAcceptanceResult",
    "NiaMasterInnerStageAcceptance",
    "run_nia_master_fktn_inner_acceptance",
]
