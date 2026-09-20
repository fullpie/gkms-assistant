"""N.I.A. audition difficulty advice from typed run evidence.

The game/static unlock boundary owns which rows may be entered.  Formal play
selects the highest unlocked row; the prior-stage score forecast remains
diagnostic and must never turn missing calibration into an automatic easy-row
fallback.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import floor
from typing import Mapping

from .audition_rules import FINAL, MID1, MID2
from .nia_static_inventory import NiaStaticAuditionStage, build_nia_static_inventory
from .produce_outer_local_save import ProduceOuterLocalSaveSnapshot


_STEP_TYPE_VALUE = {MID1: 16, MID2: 17, FINAL: 18}
_PREVIOUS_STAGE = {MID2: MID1, FINAL: MID2}


@dataclass(frozen=True, slots=True)
class NiaAuditionDifficultyAdvice:
    stage: str
    selected_threshold: int
    selected_number: int
    selected_audition_type: str
    selected_base_score: int
    selected_npc_score_max: int
    forecast_score: int | None
    prior_stage: str | None
    prior_score: int | None
    prior_number: int | None
    prior_attributes: tuple[int, int, int] | None
    current_attributes: tuple[int, int, int] | None
    current_deck_count: int | None
    prior_deck_count: int | None
    attribute_factor: float | None
    turn_factor: float | None
    deck_factor: float | None
    policy: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def infer_nia_audition_stage(
    snapshot: ProduceOuterLocalSaveSnapshot,
    screen_stage: str | None = None,
) -> str:
    """Infer the next boundary from completed LocalSave audition steps."""

    completed = {
        int(step.step_type)
        for step in snapshot.completed_steps
        if int(getattr(step, "step_type", -1)) in (16, 17, 18)
    }
    if 17 in completed:
        return FINAL
    if 16 in completed:
        return MID2
    if screen_stage in (MID1, MID2, FINAL):
        return screen_stage
    return MID1


def _line_position(line: object) -> tuple[int, int, int]:
    return (
        int(getattr(line, "log_index", -1)),
        int(getattr(line, "detail_index", -1)),
        int(getattr(line, "line_index", -1)),
    )


def _score_from_step(step: object) -> int | None:
    for line in reversed(tuple(getattr(step, "lines", ()))):
        if int(getattr(line, "line_type", -1)) != 4:
            continue
        raw = getattr(line, "target_id", "")
        if isinstance(raw, str) and raw.isdigit():
            return int(raw)
    return None


def _cleared(step: object) -> bool:
    types = {int(getattr(line, "line_type", -1)) for line in getattr(step, "lines", ())}
    return 2 in types and 3 not in types


def _previous_observation(
    snapshot: ProduceOuterLocalSaveSnapshot,
    previous_stage: str,
) -> tuple[int, int, tuple[int, int, int]] | None:
    step_type = _STEP_TYPE_VALUE[previous_stage]
    steps = [step for step in snapshot.completed_steps if step.step_type == step_type]
    if not steps:
        return None
    step = max(steps, key=lambda value: value.log_index)
    score = _score_from_step(step)
    if score is None or not _cleared(step):
        return None

    selections = [
        line
        for line in snapshot.audition_select_events
        if line.log_index <= step.log_index
    ]
    if not selections:
        return None
    selection = max(selections, key=_line_position)
    number = int(selection.target_subscription_number)
    if number < 1:
        return None

    before = _line_position(selection)
    latest: dict[int, object] = {}
    for completed in snapshot.completed_steps:
        for line in completed.lines:
            line_type = int(getattr(line, "line_type", -1))
            if line_type in (6, 7, 8) and _line_position(line) < before:
                previous = latest.get(line_type)
                if previous is None or _line_position(previous) < _line_position(line):
                    latest[line_type] = line
    if set(latest) != {6, 7, 8}:
        return None
    attributes = tuple(int(latest[line_type].after) for line_type in (6, 7, 8))
    if min(attributes) < 0 or sum(attributes) < 1:
        return None
    return score, number, attributes


def _npc_score_max(stage: NiaStaticAuditionStage) -> int:
    return max((npc.score_max for npc in stage.npc_scores), default=stage.base_score)


def advise_nia_audition_difficulty(
    snapshot: ProduceOuterLocalSaveSnapshot,
    *,
    produce_id: str,
    idol_card_id: str,
    screen_stage: str | None = None,
    current_deck_count: int | None = None,
    prior_deck_counts: Mapping[int, int] | None = None,
) -> NiaAuditionDifficultyAdvice:
    """Select the highest row allowed by the authoritative unlock boundary."""

    stage = infer_nia_audition_stage(snapshot, screen_stage)
    inventory = build_nia_static_inventory(idol_card_id, produce_id=produce_id)
    candidates = sorted(
        (value for value in inventory.auditions if value.step_type == stage),
        key=lambda value: (value.vote_count, value.number),
    )
    if not candidates:
        raise ValueError(f"N.I.A. audition candidates are unavailable: {stage}")
    vote_count = max(0, snapshot.vote_count or 0)
    unlocked = [value for value in candidates if value.vote_count <= vote_count]
    if not unlocked:
        unlocked = [candidates[0]]

    current_attributes = None
    if None not in (snapshot.vocal, snapshot.dance, snapshot.visual):
        current_attributes = (
            int(snapshot.vocal),
            int(snapshot.dance),
            int(snapshot.visual),
        )

    prior_stage = _PREVIOUS_STAGE.get(stage)
    observation = (
        None if prior_stage is None else _previous_observation(snapshot, prior_stage)
    )
    forecast = None
    prior_score = prior_number = None
    prior_attributes = None
    attribute_factor = turn_factor = deck_factor = None
    prior_deck_count = None
    if observation is not None and current_attributes is not None:
        prior_score, prior_number, prior_attributes = observation
        prior_static = next(
            (
                value
                for value in inventory.auditions
                if value.step_type == prior_stage and value.number == prior_number
            ),
            None,
        )
        if prior_static is not None:
            attribute_factor = min(
                3.0,
                max(0.5, sum(current_attributes) / sum(prior_attributes)),
            )
            turn_factor = min(2.0, max(0.5, unlocked[0].turns / prior_static.turns))
            deck_factor = 1.0
            if prior_deck_counts is not None:
                raw_prior_count = prior_deck_counts.get(_STEP_TYPE_VALUE[prior_stage])
                if isinstance(raw_prior_count, int) and raw_prior_count > 0:
                    prior_deck_count = raw_prior_count
            if (
                isinstance(current_deck_count, int)
                and current_deck_count > 0
                and prior_deck_count is not None
            ):
                deck_factor = min(1.5, max(0.75, current_deck_count / prior_deck_count))
            # The 10% haircut is applied after all observed growth factors.
            forecast = floor(
                prior_score * attribute_factor * turn_factor * deck_factor * 0.90
            )

    # Entry availability is the hard boundary.  Score forecasting is useful
    # telemetry, but it is neither complete enough nor authorized to demote an
    # already-unlocked row.  Keeping selection independent from ``forecast``
    # also prevents Mid1 (which has no prior stage by construction) from
    # always choosing the easiest audition.
    selected = unlocked[-1]
    return NiaAuditionDifficultyAdvice(
        stage=stage,
        selected_threshold=selected.vote_count,
        selected_number=selected.number,
        selected_audition_type=selected.audition_type,
        selected_base_score=selected.base_score,
        selected_npc_score_max=_npc_score_max(selected),
        forecast_score=forecast,
        prior_stage=prior_stage,
        prior_score=prior_score,
        prior_number=prior_number,
        prior_attributes=prior_attributes,
        current_attributes=current_attributes,
        current_deck_count=current_deck_count,
        prior_deck_count=prior_deck_count,
        attribute_factor=attribute_factor,
        turn_factor=turn_factor,
        deck_factor=deck_factor,
        policy="highest-authoritatively-unlocked",
    )


__all__ = [
    "NiaAuditionDifficultyAdvice",
    "advise_nia_audition_difficulty",
    "infer_nia_audition_stage",
]
