"""Learn a bounded, exact-scope drink behaviour prior from leaderboard runs.

The leaderboard replay source records the ordered drink inventory at the
beginning of each audition and stores ``use-drink`` actions as indexes into
the still-unconsumed inventory.  This module resolves those indexes offline;
it never infers an item from a score or from a translated display name.

Only complete rank-one three-stage trajectories are eligible.  A malformed
stage, an ambiguous drink action, or an out-of-range index abstains the whole
trajectory.  The resulting prior is advisory: callers still resolve drink
identity and legality from Master and may only add the returned bounded bonus
to an existing static score.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from statistics import fmean
from typing import Any, Mapping, Sequence

from .drink_behavior_observation import (
    DRINK_TIMING_GAP_STATE_INCOMPLETE,
    DRINK_TIMING_GAP_STATE_MISMATCH,
    DRINK_TIMING_GAP_STATE_MISSING,
    DRINK_TIMING_GAP_NO_ACTION,
    DRINK_TIMING_MAX_BONUS,
    DrinkBehaviorObservation,
    DrinkTimingEvaluation,
    DrinkTimingPrior,
    evaluate_drink_timing,
)


SCHEMA = "gkms.plan2-leaderboard-drink-prior.v1"
GENERIC_SCHEMA = "gkms.leaderboard-drink-prior.v1"
PLAN1 = "ProducePlanType_Plan1"
PLAN2 = "ProducePlanType_Plan2"
PLAN3 = "ProducePlanType_Plan3"
LESSON_BUFF = "ProduceExamEffectType_ExamLessonBuff"
PARAMETER_BUFF = "ProduceExamEffectType_ExamParameterBuff"
REVIEW = "ProduceExamEffectType_ExamReview"
AGGRESSIVE = "ProduceExamEffectType_ExamCardPlayAggressive"
CONCENTRATION = "ProduceExamEffectType_ExamConcentration"
SUPPORTED_FLOWS = frozenset(
    {
        (PLAN1, LESSON_BUFF),
        (PLAN1, PARAMETER_BUFF),
        (PLAN2, REVIEW),
        (PLAN2, AGGRESSIVE),
        (PLAN3, CONCENTRATION),
    }
)
SUPPORTED_EXAM_EFFECT_TYPES = frozenset(effect for _plan, effect in SUPPORTED_FLOWS)
_LEGACY_LEADERBOARD_EPISODES = (
    Path(__file__).resolve().parents[2]
    / "var"
    / "leaderboard_dataset"
    / "v330_nia_all_modes_multicard_v1"
    / "episodes.jsonl"
)
_FIVE_ARCHETYPE_LEADERBOARD_EPISODES = (
    Path(__file__).resolve().parents[2]
    / "var"
    / "leaderboard_dataset"
    / "v330_nia_five_archetype_all_modes_v1"
    / "episodes.jsonl"
)
_BACKGROUND_REFRESH_LEADERBOARD_EPISODES = (
    Path(__file__).resolve().parents[2]
    / "var"
    / "leaderboard_dataset"
    / "v330_nia_background_refresh_v1"
    / "episodes.jsonl"
)
# Prefer the latest atomically published background corpus.  Older installed
# copies retain the stable five-archetype/Plan2 fallback.
DEFAULT_LEADERBOARD_EPISODES = (
    _BACKGROUND_REFRESH_LEADERBOARD_EPISODES
    if _BACKGROUND_REFRESH_LEADERBOARD_EPISODES.is_file()
    else (
        _FIVE_ARCHETYPE_LEADERBOARD_EPISODES
        if _FIVE_ARCHETYPE_LEADERBOARD_EPISODES.is_file()
        else _LEGACY_LEADERBOARD_EPISODES
    )
)

_STAGES = (
    "ProduceStepType_AuditionMid1",
    "ProduceStepType_AuditionMid2",
    "ProduceStepType_AuditionFinal",
)
_STAGE_INDEX = {value: index for index, value in enumerate(_STAGES)}
_USE_DRINK_ACTION_TYPES = frozenset(
    {
        # v2 normalized JSON uses this spelling.  The enum spellings are
        # accepted as well so callers cannot accidentally make a safe source
        # abstain merely by skipping the normalizer.
        "use-drink",
        "use_drink",
        "ExamActionTypeUseDrink",
        "ExamActionType_UseDrink",
        "ExamActionType_Use_Drink",
    }
)
MAX_BONUS = 300
MIN_TRAJECTORY_SUPPORT_FOR_BONUS = 2


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _positive_number(value: object, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{label} must be a positive number")
    return value


def _strict_integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} must be an array")
    result = tuple(_text(item, f"{label}[]") for item in value)
    return result


def _raw_episode_rows(path: Path) -> tuple[Mapping[str, Any], ...]:
    rows: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"leaderboard episode line {line_number} is malformed") from error
        if not isinstance(raw, Mapping):
            raise ValueError(f"leaderboard episode line {line_number} is not an object")
        rows.append(raw)
    if not rows:
        raise ValueError("leaderboard episode source is empty")
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class _Replay:
    trajectory_id: str
    terminal_score: int | float
    initial_drink_ids: tuple[str, ...]
    used_drink_ids: tuple[str, ...]
    timing_observations: tuple[DrinkBehaviorObservation, ...] = ()
    timing_gap_codes: tuple[str, ...] = ()


def _action_order(raw: Mapping[str, Any], position: int) -> int:
    # ``order`` is required by the normalized schema, but defaulting a missing
    # value to the source position keeps this small diagnostic reader useful
    # for hand-built fixtures without weakening duplicate-order rejection.
    value = raw.get("order", position)
    return _strict_integer(value, f"actions[{position}].order")


def _timing_state_rows(
    row: Mapping[str, Any],
) -> dict[int, Mapping[str, Any]] | None:
    """Return explicit recorder rows keyed by canonical action order.

    Canonical leaderboard v2 rows intentionally do not include these rows.
    ``None`` therefore means *source did not carry a transition recorder*,
    while an empty mapping means the source explicitly carried no state rows.
    Both cases are reported as a timing gap by the trajectory replay.
    """

    raw = row.get("native_action_states", row.get("action_states"))
    if raw is None:
        return None
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("native_action_states must be an array")
    result: dict[int, Mapping[str, Any]] = {}
    for index, value in enumerate(raw):
        if not isinstance(value, Mapping):
            raise ValueError(f"native_action_states[{index}] must be an object")
        order = value.get("order", value.get("action_order"))
        if order is None:
            raise ValueError(f"native_action_states[{index}].order is missing")
        order = _strict_integer(order, f"native_action_states[{index}].order")
        if order in result:
            raise ValueError("duplicate native action-state order")
        result[order] = value
    return result


def _timing_observation_from_row(
    *,
    row: Mapping[str, Any],
    trajectory_id: str,
    action_order: int,
    drink_index: int,
    candidate_before: tuple[str, ...],
    chosen_drink_id: str,
    candidate_after: tuple[str, ...],
    terminal_score: int,
    state_row: Mapping[str, Any],
) -> DrinkBehaviorObservation:
    """Validate one explicit recorder boundary against replay inventory."""

    before = state_row.get("before", state_row.get("state_before"))
    after = state_row.get("after", state_row.get("state_after"))
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        raise ValueError("native action-state before/after is missing")
    state_complete = state_row.get("state_complete", True)
    if state_complete is not True:
        raise ValueError("native action-state is marked incomplete")
    state_action = state_row.get("action_type")
    if state_action not in {"use-drink", "use_drink", "drink", "UseDrink"}:
        raise ValueError("native action-state action type is not use-drink")
    state_index = state_row.get("play_index", state_row.get("index"))
    if _strict_integer(state_index, "native action-state.play_index") != drink_index:
        raise ValueError("native action-state index differs from replay")
    state_drink = state_row.get("drink_id", state_row.get("chosen_drink_id"))
    if state_drink != chosen_drink_id:
        raise ValueError("native action-state drink ID differs from replay")
    available = state_row.get(
        "available_drink_ids",
        state_row.get(
            "available_drink_ids_before",
            state_row.get("drink_inventory", state_row.get("candidate_drink_ids")),
        ),
    )
    if not isinstance(available, Sequence) or isinstance(available, (str, bytes)):
        raise ValueError("native action-state available_drink_ids is missing")
    available_before = tuple(_text(value, "native action-state available drink") for value in available)
    if available_before != candidate_before:
        raise ValueError("native action-state candidate inventory differs from replay")
    available_after = state_row.get(
        "available_drink_ids_after", state_row.get("remaining_drink_ids")
    )
    if not isinstance(available_after, Sequence) or isinstance(available_after, (str, bytes)):
        raise ValueError("native action-state available_drink_ids_after is missing")
    normalized_after = tuple(
        _text(value, "native action-state available drink after")
        for value in available_after
    )
    if normalized_after != candidate_after:
        raise ValueError("native action-state post inventory differs from replay")
    current_turn = before.get("current_turn")
    if not isinstance(current_turn, int) or isinstance(current_turn, bool) or current_turn < 1:
        raise ValueError("native action-state current_turn is missing")
    manual_sequence = state_row.get(
        "manual_command_sequence",
        state_row.get("manual_sequence", state_row.get("command_sequence", action_order + 1)),
    )
    manual_sequence = _strict_integer(
        manual_sequence,
        "native action-state.manual_command_sequence",
        minimum=1,
    )
    return DrinkBehaviorObservation(
        episode_id=_text(
            row.get(
                "episode_id",
                f"{trajectory_id}:audition:{row.get('audition_index', 0)}",
            ),
            "episode_id",
        ),
        trajectory_id=trajectory_id,
        produce_id=_text(row.get("produce_id"), "produce_id"),
        idol_card_id=_text(row.get("idol_card_id"), "idol_card_id"),
        plan_type=_text(row.get("plan_type"), "plan_type"),
        exam_effect_type=_text(row.get("exam_effect_type"), "exam_effect_type"),
        stage=_text(row.get("step_type"), "step_type"),
        action_order=action_order,
        native_sequence=manual_sequence,
        drink_index=drink_index,
        candidate_drink_ids=candidate_before,
        chosen_drink_id=chosen_drink_id,
        terminal_score=terminal_score,
        round_number=current_turn,
        state_before=dict(before),
        state_after=dict(after),
        candidate_drink_ids_after=normalized_after,
        state_source="leaderboard+live-transition-recorder",
        state_complete=True,
    )


def _replay_trajectory(
    trajectory_id: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    plan_type: str = PLAN2,
) -> _Replay | None:
    """Replay one grouped trajectory, returning ``None`` on any ambiguity."""

    if len(rows) != len(_STAGES):
        return None
    by_stage: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        try:
            stage = _text(row.get("step_type"), "step_type")
        except ValueError:
            return None
        if stage not in _STAGE_INDEX or stage in by_stage:
            return None
        by_stage[stage] = row
        if row.get("plan_type") != plan_type:
            return None
        if row.get("rank") != 1:
            return None
        try:
            _positive_number(row.get("terminal_score"), "terminal_score")
        except ValueError:
            return None
        # When present, the numeric stage index must agree with the canonical
        # stage.  A contradictory index cannot be replayed safely.
        if "stage_index" in row:
            try:
                if _strict_integer(row["stage_index"], "stage_index") != 0:
                    # Leaderboard rows use section_index for the outer section;
                    # stage_index is currently 0 for all three audition rows.
                    # It is not a stage ordinal, so do not interpret it.
                    pass
            except ValueError:
                return None
        if "audition_index" in row:
            try:
                audition_index = _strict_integer(row["audition_index"], "audition_index")
            except ValueError:
                return None
            if audition_index != _STAGE_INDEX[stage]:
                return None
    if set(by_stage) != set(_STAGES):
        return None

    used: list[str] = []
    initial: list[str] = []
    timing_observations: list[DrinkBehaviorObservation] = []
    timing_gaps: list[str] = []
    for stage in _STAGES:
        row = by_stage[stage]
        try:
            inventory = list(_string_tuple(row.get("produce_drink_ids"), "produce_drink_ids"))
        except ValueError:
            return None
        initial.extend(inventory)
        try:
            state_rows = _timing_state_rows(row)
        except ValueError:
            # The baseline replay remains usable, but no timing row from this
            # trajectory may be trusted after a malformed supplement.
            state_rows = {}
            timing_gaps.append(DRINK_TIMING_GAP_STATE_INCOMPLETE)
        raw_actions = row.get("actions")
        if not isinstance(raw_actions, Sequence) or isinstance(raw_actions, (str, bytes)):
            return None
        ordered: list[tuple[int, Mapping[str, Any]]] = []
        seen_orders: set[int] = set()
        for position, raw_action in enumerate(raw_actions):
            if not isinstance(raw_action, Mapping):
                return None
            try:
                order = _action_order(raw_action, position)
            except ValueError:
                return None
            if order in seen_orders:
                return None
            seen_orders.add(order)
            ordered.append((order, raw_action))
        ordered.sort(key=lambda item: item[0])
        stage_has_drink = False
        for _order, raw_action in ordered:
            action_type = raw_action.get("action_type")
            if not isinstance(action_type, str):
                return None
            if action_type not in _USE_DRINK_ACTION_TYPES:
                continue
            stage_has_drink = True
            raw_indexes = raw_action.get("indexes")
            if not isinstance(raw_indexes, Sequence) or isinstance(raw_indexes, (str, bytes)):
                return None
            if len(raw_indexes) != 1:
                return None
            try:
                index = _strict_integer(raw_indexes[0], "use-drink index")
            except ValueError:
                return None
            if index >= len(inventory):
                return None
            candidate_before = tuple(inventory)
            chosen = inventory.pop(index)
            candidate_after = tuple(inventory)
            used.append(chosen)
            if state_rows is None:
                continue
            state_row = state_rows.get(_order)
            if state_row is None:
                timing_gaps.append(DRINK_TIMING_GAP_STATE_MISSING)
                continue
            try:
                timing_observations.append(
                    _timing_observation_from_row(
                        row=row,
                        trajectory_id=trajectory_id,
                        action_order=_order,
                        drink_index=index,
                        candidate_before=candidate_before,
                        chosen_drink_id=chosen,
                        candidate_after=candidate_after,
                        terminal_score=_strict_integer(
                            row.get("terminal_score"),
                            "terminal_score",
                            minimum=1,
                        ),
                        state_row=state_row,
                    )
                )
            except (TypeError, ValueError, KeyError):
                timing_gaps.append(DRINK_TIMING_GAP_STATE_MISMATCH)

        if state_rows is None and stage_has_drink:
            timing_gaps.append(DRINK_TIMING_GAP_STATE_MISSING)

    if not used:
        timing_gaps.append(DRINK_TIMING_GAP_NO_ACTION)
    final_score = by_stage[_STAGES[-1]].get("terminal_score")
    try:
        final_score = _positive_number(final_score, "final terminal_score")
    except ValueError:
        return None
    if timing_gaps:
        # State-conditioned timing is trajectory-atomic just like the action
        # replay: one missing boundary must not leave later uses looking
        # complete.  Keep the machine-readable gaps for the manifest.
        timing_observations = []
    return _Replay(
        trajectory_id=trajectory_id,
        terminal_score=final_score,
        initial_drink_ids=tuple(initial),
        used_drink_ids=tuple(used),
        timing_observations=tuple(timing_observations),
        timing_gap_codes=tuple(dict.fromkeys(timing_gaps)),
    )


@dataclass(frozen=True, slots=True)
class LeaderboardDrinkPriorStat:
    """Evidence for one Master drink ID inside one exact scope."""

    drink_id: str
    trajectory_support: int
    use_count: int
    mean_terminal_score: float
    bonus: int

    def __post_init__(self) -> None:
        if not self.drink_id:
            raise ValueError("drink prior requires a drink ID")
        if self.trajectory_support < 1:
            raise ValueError("drink prior support must be positive")
        if self.use_count < 0:
            raise ValueError("drink prior use count cannot be negative")
        if self.mean_terminal_score <= 0:
            raise ValueError("drink prior terminal score must be positive")
        if not 0 <= self.bonus <= MAX_BONUS:
            raise ValueError(f"drink prior bonus must be bounded to 0..{MAX_BONUS}")

    @property
    def terminal_score(self) -> float:
        """Compatibility alias for consumers that call the mean terminal score."""

        return self.mean_terminal_score

    @property
    def support(self) -> int:
        return self.trajectory_support


@dataclass(frozen=True, slots=True)
class Plan2LeaderboardDrinkPrior:
    source_path: Path
    source_sha256: str
    trajectory_count: int
    final_episode_count: int
    abstained_trajectory_count: int
    master_hashes: tuple[str, ...]
    app_versions: tuple[str, ...]
    produce_ids: tuple[str, ...]
    idol_card_ids: tuple[str, ...]
    character_ids: tuple[str, ...]
    statistics: Mapping[str, LeaderboardDrinkPriorStat]
    exam_effect_type: str
    plan_type: str = PLAN2
    schema: str = SCHEMA
    timing_observations: tuple[DrinkBehaviorObservation, ...] = ()
    timing_prior: DrinkTimingPrior | None = None
    timing_evaluation: DrinkTimingEvaluation | None = None
    timing_gap_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema not in {SCHEMA, GENERIC_SCHEMA}:
            raise ValueError("unsupported leaderboard drink prior identity")
        if (self.plan_type, self.exam_effect_type) not in SUPPORTED_FLOWS:
            raise ValueError("unsupported leaderboard drink prior flow")
        if self.trajectory_count < 3 or self.final_episode_count != self.trajectory_count:
            raise ValueError("leaderboard drink prior requires at least three complete trajectories")
        if self.abstained_trajectory_count < 0:
            raise ValueError("leaderboard drink prior abstention count is invalid")
        if len(self.source_sha256) != 64:
            raise ValueError("leaderboard drink prior source hash is invalid")
        if len(self.produce_ids) != 1 or len(self.idol_card_ids) != 1:
            raise ValueError("leaderboard drink prior must have one exact scope")
        if any(
            not isinstance(value, DrinkBehaviorObservation)
            or not value.is_state_conditioned
            for value in self.timing_observations
        ):
            raise ValueError("timing_observations must be complete state rows")
        if self.timing_prior is not None and not isinstance(
            self.timing_prior, DrinkTimingPrior
        ):
            raise TypeError("timing_prior must be DrinkTimingPrior or None")
        if self.timing_observations and self.timing_prior is None:
            raise ValueError("timing observations require a timing prior")
        if self.timing_evaluation is not None and not isinstance(
            self.timing_evaluation, DrinkTimingEvaluation
        ):
            raise TypeError("timing_evaluation must be DrinkTimingEvaluation or None")
        if any(
            not isinstance(value, str) or not value for value in self.timing_gap_codes
        ):
            raise ValueError("timing_gap_codes must contain non-empty text")

    @property
    def produce_id(self) -> str:
        return self.produce_ids[0]

    @property
    def idol_card_id(self) -> str:
        return self.idol_card_ids[0]

    def score_for(self, drink_id: str) -> int:
        stat = self.statistics.get(drink_id)
        return 0 if stat is None else stat.bonus

    @property
    def timing_observation_count(self) -> int:
        return len(self.timing_observations)

    @property
    def timing_available(self) -> bool:
        return bool(self.timing_observations) and not self.timing_gap_codes

    def timing_score_for(self, observation: DrinkBehaviorObservation) -> int:
        if self.timing_prior is None or self.timing_gap_codes:
            return 0
        if not self.applies_to(
            produce_id=observation.produce_id,
            idol_card_id=observation.idol_card_id,
            exam_effect_type=observation.exam_effect_type,
        ):
            return 0
        return self.timing_prior.score_for(observation)

    def timing_bonus_for(self, observation: DrinkBehaviorObservation) -> int:
        if self.timing_prior is None or self.timing_gap_codes:
            return 0
        if not self.applies_to(
            produce_id=observation.produce_id,
            idol_card_id=observation.idol_card_id,
            exam_effect_type=observation.exam_effect_type,
        ):
            return 0
        return self.timing_prior.bonus_for(observation)

    def score_for_state(self, **kwargs: object) -> int:
        if self.timing_prior is None or self.timing_gap_codes:
            return 0
        flow_id = kwargs.get("flow_id")
        if flow_id is not None and flow_id != self._flow_id_for_scope():
            return 0
        return self.timing_prior.score_for_state(**kwargs)  # type: ignore[arg-type]

    def bonus_for_state(self, **kwargs: object) -> int:
        if self.timing_prior is None or self.timing_gap_codes:
            return 0
        flow_id = kwargs.get("flow_id")
        if flow_id is not None and flow_id != self._flow_id_for_scope():
            return 0
        return self.timing_prior.bonus_for_state(**kwargs)

    def _flow_id_for_scope(self) -> str:
        return "|".join((self.produce_id, self.plan_type, self.exam_effect_type))

    def applies_to(
        self,
        *,
        produce_id: str,
        idol_card_id: str,
        exam_effect_type: str,
    ) -> bool:
        """Require the complete observed mode/card/archetype scope."""

        return (
            self.produce_id == produce_id
            and self.idol_card_id == idol_card_id
            and self.exam_effect_type == exam_effect_type
        )

    def summary(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "source_path": str(self.source_path),
            "source_sha256": self.source_sha256,
            "trajectory_count": self.trajectory_count,
            "final_episode_count": self.final_episode_count,
            "abstained_trajectory_count": self.abstained_trajectory_count,
            "master_hashes": list(self.master_hashes),
            "app_versions": list(self.app_versions),
            "produce_ids": list(self.produce_ids),
            "idol_card_ids": list(self.idol_card_ids),
            "exam_effect_type": self.exam_effect_type,
            "drink_count": len(self.statistics),
            "timing_observation_count": len(self.timing_observations),
            "timing_available": bool(self.timing_observations),
            "timing_gap_codes": list(self.timing_gap_codes),
            "timing_gap": (
                self.timing_gap_codes[0] if self.timing_gap_codes else None
            ),
            "timing_prior_schema": (
                None if self.timing_prior is None else self.timing_prior.schema
            ),
            "timing_evaluation": (
                None
                if self.timing_evaluation is None
                else self.timing_evaluation.to_dict()
            ),
        }


def _build_prior(
    path: Path,
    *,
    produce_id: str,
    idol_card_id: str,
    plan_type: str = PLAN2,
    exam_effect_type: str,
) -> Plan2LeaderboardDrinkPrior:
    scope = (
        _text(produce_id, "produce_id"),
        _text(idol_card_id, "idol_card_id"),
        _text(plan_type, "plan_type"),
        _text(exam_effect_type, "exam_effect_type"),
    )
    if (scope[2], scope[3]) not in SUPPORTED_FLOWS:
        raise ValueError("unsupported leaderboard drink prior flow")
    rows = _raw_episode_rows(path)
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        if (
            row.get("plan_type") == scope[2]
            and row.get("produce_id") == scope[0]
            and row.get("idol_card_id") == scope[1]
            and row.get("exam_effect_type") == scope[3]
        ):
            trajectory_id = row.get("trajectory_id")
            if isinstance(trajectory_id, str) and trajectory_id:
                grouped[trajectory_id].append(row)

    if not grouped:
        raise ValueError("leaderboard drink prior has no matching exact scope")

    replays: list[_Replay] = []
    abstained = 0
    for trajectory_id, members in sorted(grouped.items()):
        replay = _replay_trajectory(
            trajectory_id,
            members,
            plan_type=scope[2],
        )
        if replay is None:
            abstained += 1
        else:
            replays.append(replay)
    if len(replays) < 3:
        raise ValueError("leaderboard drink prior has fewer than three complete trajectories")

    trajectory_count = len(replays)
    maximum_score = max(float(value.terminal_score) for value in replays)
    support: dict[str, int] = defaultdict(int)
    uses: dict[str, int] = defaultdict(int)
    scores: dict[str, list[float]] = defaultdict(list)
    timing_observations: list[DrinkBehaviorObservation] = []
    timing_gaps: set[str] = set()
    for replay in replays:
        present = set(replay.initial_drink_ids)
        for drink_id in present:
            support[drink_id] += 1
            scores[drink_id].append(float(replay.terminal_score))
        for drink_id in replay.used_drink_ids:
            uses[drink_id] += 1
        timing_observations.extend(replay.timing_observations)
        timing_gaps.update(replay.timing_gap_codes)

    statistics: dict[str, LeaderboardDrinkPriorStat] = {}
    for drink_id in sorted(support):
        trajectory_support = support[drink_id]
        use_count = uses[drink_id]
        mean_terminal_score = fmean(scores[drink_id])
        # Keep sparse observations neutral.  The score is deliberately a
        # small additive signal; it cannot turn an otherwise illegal/new
        # Master row into a candidate and is capped independently here.
        if trajectory_support < MIN_TRAJECTORY_SUPPORT_FOR_BONUS:
            bonus = 0
        else:
            support_bonus = round(120 * trajectory_support / trajectory_count)
            use_bonus = round(100 * min(1.0, use_count / max(1, trajectory_support * 2)))
            performance_bonus = round(80 * mean_terminal_score / maximum_score)
            bonus = min(MAX_BONUS, support_bonus + use_bonus + performance_bonus)
        statistics[drink_id] = LeaderboardDrinkPriorStat(
            drink_id=drink_id,
            trajectory_support=trajectory_support,
            use_count=use_count,
            mean_terminal_score=mean_terminal_score,
            bonus=bonus,
        )

    valid_rows = tuple(
        row
        for replay in replays
        for row in grouped[replay.trajectory_id]
    )

    def metadata_values(name: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    value
                    for row in valid_rows
                    for value in (row.get(name),)
                    if isinstance(value, str) and value
                }
            )
        )

    timing_values = tuple(timing_observations)
    timing_prior = (
        DrinkTimingPrior.from_observations(timing_values)
        if timing_values
        else None
    )
    timing_evaluation = evaluate_drink_timing(timing_values)
    return Plan2LeaderboardDrinkPrior(
        source_path=path,
        source_sha256=_sha256(path),
        trajectory_count=trajectory_count,
        final_episode_count=trajectory_count,
        abstained_trajectory_count=abstained,
        master_hashes=metadata_values("master_hash"),
        app_versions=metadata_values("app_version"),
        produce_ids=(scope[0],),
        idol_card_ids=(scope[1],),
        character_ids=metadata_values("character_id"),
        statistics=statistics,
        exam_effect_type=scope[3],
        plan_type=scope[2],
        schema=GENERIC_SCHEMA,
        timing_observations=timing_values,
        timing_prior=timing_prior,
        timing_evaluation=timing_evaluation,
        timing_gap_codes=tuple(sorted(timing_gaps)),
    )


@lru_cache(maxsize=128)
def _load_cached(
    path_text: str,
    source_mtime_ns: int,
    source_size: int,
    produce_id: str,
    idol_card_id: str,
    plan_type: str,
    exam_effect_type: str,
) -> Plan2LeaderboardDrinkPrior:
    # File identity participates in the cache key, so an atomically rebuilt
    # background dataset is visible to the next produce without a worker
    # restart.  The values are otherwise intentionally unused here.
    del source_mtime_ns, source_size
    return _build_prior(
        Path(path_text),
        produce_id=produce_id,
        idol_card_id=idol_card_id,
        plan_type=plan_type,
        exam_effect_type=exam_effect_type,
    )


def load_leaderboard_drink_prior(
    source: str | Path = DEFAULT_LEADERBOARD_EPISODES,
    *,
    produce_id: str = "",
    idol_card_id: str = "",
    plan_type: str = "",
    exam_effect_type: str = "",
) -> Plan2LeaderboardDrinkPrior:
    """Build a bounded drink prior for one exact five-archetype scope."""

    if not all(
        isinstance(value, str) and value.strip()
        for value in (produce_id, idol_card_id, plan_type, exam_effect_type)
    ):
        raise ValueError("leaderboard drink prior requires a complete exact scope")
    path = Path(source).resolve()
    stat = path.stat()
    return _load_cached(
        str(path),
        stat.st_mtime_ns,
        stat.st_size,
        produce_id,
        idol_card_id,
        plan_type,
        exam_effect_type,
    )


def load_plan2_leaderboard_drink_prior(
    source: str | Path = DEFAULT_LEADERBOARD_EPISODES,
    *,
    produce_id: str = "",
    idol_card_id: str = "",
    exam_effect_type: str = "",
) -> Plan2LeaderboardDrinkPrior:
    """Build a prior for one explicit ``(produce, idol, effect)`` scope."""

    if not all(isinstance(value, str) and value.strip() for value in (produce_id, idol_card_id, exam_effect_type)):
        raise ValueError("leaderboard drink prior requires a complete exact scope")
    return load_leaderboard_drink_prior(
        source,
        produce_id=produce_id,
        idol_card_id=idol_card_id,
        plan_type=PLAN2,
        exam_effect_type=exam_effect_type,
    )


def try_load_default_leaderboard_drink_prior(
    *,
    produce_id: str = "",
    idol_card_id: str = "",
    plan_type: str = "",
    exam_effect_type: str = "",
) -> Plan2LeaderboardDrinkPrior | None:
    try:
        return load_leaderboard_drink_prior(
            produce_id=produce_id,
            idol_card_id=idol_card_id,
            plan_type=plan_type,
            exam_effect_type=exam_effect_type,
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


def try_load_default_plan2_leaderboard_drink_prior(
    *,
    produce_id: str = "",
    idol_card_id: str = "",
    exam_effect_type: str = "",
) -> Plan2LeaderboardDrinkPrior | None:
    try:
        return load_plan2_leaderboard_drink_prior(
            produce_id=produce_id,
            idol_card_id=idol_card_id,
            exam_effect_type=exam_effect_type,
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


__all__ = [
    "AGGRESSIVE",
    "CONCENTRATION",
    "DEFAULT_LEADERBOARD_EPISODES",
    "GENERIC_SCHEMA",
    "LESSON_BUFF",
    "LeaderboardDrinkPriorStat",
    "MAX_BONUS",
    "MIN_TRAJECTORY_SUPPORT_FOR_BONUS",
    "PARAMETER_BUFF",
    "PLAN1",
    "PLAN2",
    "PLAN3",
    "Plan2LeaderboardDrinkPrior",
    "DrinkBehaviorObservation",
    "DrinkTimingEvaluation",
    "DrinkTimingPrior",
    "DRINK_TIMING_MAX_BONUS",
    "DRINK_TIMING_GAP_NO_ACTION",
    "DRINK_TIMING_GAP_STATE_INCOMPLETE",
    "DRINK_TIMING_GAP_STATE_MISMATCH",
    "DRINK_TIMING_GAP_STATE_MISSING",
    "REVIEW",
    "SCHEMA",
    "SUPPORTED_EXAM_EFFECT_TYPES",
    "SUPPORTED_FLOWS",
    "load_leaderboard_drink_prior",
    "load_plan2_leaderboard_drink_prior",
    "try_load_default_leaderboard_drink_prior",
    "try_load_default_plan2_leaderboard_drink_prior",
]
