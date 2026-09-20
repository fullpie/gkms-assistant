"""Bind an audition ``ExamSaveData`` snapshot to the native Plan 3 search.

The Plan 3 card kernel already delegates parameter gains to the native battle
formula.  This module does not duplicate that arithmetic.  It only supplies
the audition-only state that has a different meaning from an ordinary lesson:

* ``clearBorder`` is a *rank* threshold, not a parameter threshold;
* ``limitBorder`` is the force-end score cap;
* the current and future Vocal/Dance/Visual order is serialized in
  ``turnStatusParameterTypeList``; and
* NPC turn scores are already serialized in ``npcDataList``.

The complete remaining attribute schedule is passed to the native GUID/RNG
search, and the existing objective callback is bound to the serialized NPC
rank rule.  Callers can either request the original one-card advisory search
or a complete remaining-turn horizon with skip, force-end recovery, and
ordered drink actions (including selected GUID moves).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

import yaml

from .audition_horizon import (
    AuditionBoundaryModel,
    AuditionBoundaryOutcome,
)
from .local_save_decoder import LocalSaveDecodeError
from .master_db import DEFAULT_DATABASE
from .plan3_drink import (
    Plan3DrinkInventory,
    load_plan3_drink_inventory,
)
from .plan3_drink_history import (
    Plan3CompletedDrinkReplay,
    replay_completed_plan3_drink_history,
)
from .plan3_engine import (
    DEFAULT_MASTER_DIR,
    LESSON_DANCE,
    LESSON_VISUAL,
    LESSON_VOCAL,
    STANCE_CONCENTRATION,
    STANCE_FULL_POWER,
    STANCE_PRESERVATION,
    Plan3ExamSettings,
    Plan3State,
    load_plan3_exam_settings,
)
from .plan3_local_save_bridge import (
    DecodedPlan3LocalSave,
    decode_plan3_local_save_bytes,
    decode_plan3_local_save_file,
)
from .plan3_native_search import (
    Plan3NativeAcceptedPlayExtension,
    Plan3NativeSearchResult,
    Plan3NativeTurnStartExtension,
    search_plan3_native,
)
from .plan3_native_search_bridge import (
    DEFAULT_SUPPORT_CARD_MASTER,
    Plan3NativeSearchBridgeIssue,
    Plan3NativeSearchBridgeResult,
    search_plan3_native_decoded_local_save,
)
from .plan3_search import Objective, evaluate_plan3_search_state


_AUDITION_EXAM_TYPE = 1
_MAIN_PHASE = 6
_PARAMETER_TO_LESSON = {
    1: LESSON_VOCAL,
    2: LESSON_DANCE,
    3: LESSON_VISUAL,
}
_BONUS_FIELD_BY_PARAMETER = {
    1: "vocalBonusPermil",
    2: "danceBonusPermil",
    3: "visualBonusPermil",
}


def _integer(
    value: object,
    label: str,
    *,
    minimum: int | None = None,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


@dataclass(frozen=True, slots=True)
class Plan3AuditionTurnFrame:
    """One exact turn target and its already-resolved battle multiplier."""

    round_number: int
    parameter_type: int
    lesson_type: str
    score_multiplier_permille: int

    def __post_init__(self) -> None:
        _integer(self.round_number, "round_number", minimum=1)
        if self.parameter_type not in _PARAMETER_TO_LESSON:
            raise ValueError("parameter_type must be Vocal/Dance/Visual")
        if self.lesson_type != _PARAMETER_TO_LESSON[self.parameter_type]:
            raise ValueError("lesson_type does not match parameter_type")
        _integer(
            self.score_multiplier_permille,
            "score_multiplier_permille",
            minimum=1,
        )


@dataclass(frozen=True, slots=True)
class Plan3AuditionNpcTrack:
    """The native per-turn NPC score track serialized by ``ExamSaveData``."""

    npc_id: str
    number: int
    turn_scores: tuple[int, ...]
    current_score: int
    vocal_score: int
    dance_score: int
    visual_score: int

    def __post_init__(self) -> None:
        if not isinstance(self.npc_id, str) or not self.npc_id:
            raise ValueError("npc_id must be non-empty text")
        _integer(self.number, "npc number", minimum=1)
        if not self.turn_scores:
            raise ValueError("NPC turn_scores must not be empty")
        for index, value in enumerate(self.turn_scores):
            _integer(value, f"NPC turn_scores[{index}]", minimum=0)
        for label in (
            "current_score",
            "vocal_score",
            "dance_score",
            "visual_score",
        ):
            _integer(getattr(self, label), f"NPC {label}", minimum=0)

    def score_after_round(
        self,
        *,
        current_round: int,
        round_number: int,
    ) -> int:
        """Return NPC score after ``round_number`` from a settled live turn.

        At the start of ``current_round``, ``current_score`` already contains
        every entry before that round.  The remaining serialized entries can
        therefore be added without regenerating or guessing NPC RNG.
        """

        _integer(current_round, "current_round", minimum=1)
        _integer(round_number, "round_number", minimum=0)
        completed_round = current_round - 1
        if round_number < completed_round or round_number > len(self.turn_scores):
            raise ValueError("requested NPC round is outside the available horizon")
        return self.current_score + sum(
            self.turn_scores[completed_round:round_number]
        )


@dataclass(frozen=True, slots=True)
class Plan3AuditionHorizonGap:
    code: str
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("gap code must be non-empty text")
        if not isinstance(self.detail, str) or not self.detail:
            raise ValueError("gap detail must be non-empty text")


@dataclass(frozen=True, slots=True)
class Plan3AuditionBoundary:
    """One native turn-close outcome exposed by the Plan 3 bridge."""

    outcome: AuditionBoundaryOutcome
    exam_end_complete: bool
    post_boundary_recovery_units: int
    next_round: int | None

    def __post_init__(self) -> None:
        try:
            outcome = AuditionBoundaryOutcome(self.outcome)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"unsupported Plan3 boundary: {self.outcome!r}"
            ) from error
        object.__setattr__(self, "outcome", outcome)
        if type(self.exam_end_complete) is not bool:
            raise TypeError("exam_end_complete must be a boolean")
        if (
            not isinstance(self.post_boundary_recovery_units, int)
            or isinstance(self.post_boundary_recovery_units, bool)
            or self.post_boundary_recovery_units < 0
        ):
            raise ValueError(
                "post_boundary_recovery_units must be a non-negative integer"
            )
        if self.next_round is not None and (
            not isinstance(self.next_round, int)
            or isinstance(self.next_round, bool)
            or self.next_round < 1
        ):
            raise ValueError("next_round must be a positive integer or None")
        if outcome is AuditionBoundaryOutcome.EXAM_END:
            if not self.exam_end_complete:
                raise ValueError("EXAM_END requires exam_end_complete")
        elif self.exam_end_complete:
            raise ValueError("NEXT_TURN cannot be exam_end_complete")

    @property
    def terminal(self) -> bool:
        return self.outcome is AuditionBoundaryOutcome.EXAM_END

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "exam_end_complete": self.exam_end_complete,
            "post_boundary_recovery_units": self.post_boundary_recovery_units,
            "next_round": self.next_round,
        }


@dataclass(frozen=True, slots=True)
class Plan3AuditionContext:
    """Audition-only schedule, rank, NPC, and terminal state."""

    setting_id: str
    step_type_value: int
    current_round: int
    limit_round: int
    clear_rank: int
    force_end_score: int
    frames: tuple[Plan3AuditionTurnFrame, ...]
    npcs: tuple[Plan3AuditionNpcTrack, ...]
    npc_score_multiple_permille: int
    is_static_npc_score: bool
    turn_end_stamina_recovery: int | None
    drink_ids: tuple[str, ...] = ()
    extra_turn: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.setting_id, str) or not self.setting_id:
            raise ValueError("setting_id must be non-empty text")
        _integer(self.step_type_value, "step_type_value")
        _integer(self.current_round, "current_round", minimum=1)
        _integer(self.limit_round, "limit_round", minimum=1)
        if self.current_round > self.limit_round:
            raise ValueError("current_round exceeds limit_round")
        _integer(self.clear_rank, "clear_rank", minimum=1)
        # Native saves use both -1 and 0 as disabled sentinels, depending on
        # the audition construction path. Only positive values enable the
        # score-based force end.
        _integer(self.force_end_score, "force_end_score", minimum=-1)
        _integer(
            self.npc_score_multiple_permille,
            "npc_score_multiple_permille",
            minimum=0,
        )
        if not isinstance(self.is_static_npc_score, bool):
            raise ValueError("is_static_npc_score must be bool")
        if self.turn_end_stamina_recovery is not None:
            _integer(
                self.turn_end_stamina_recovery,
                "turn_end_stamina_recovery",
                minimum=0,
            )
        if not isinstance(self.drink_ids, tuple) or any(
            not isinstance(value, str) or not value
            for value in self.drink_ids
        ):
            raise TypeError("drink_ids must contain non-empty text")
        _integer(self.extra_turn, "extra_turn", minimum=0)
        if len(self.frames) != self.limit_round:
            raise ValueError("turn frames must cover the complete audition")
        if tuple(frame.round_number for frame in self.frames) != tuple(
            range(1, self.limit_round + 1)
        ):
            raise ValueError("turn frames must be contiguous from round 1")
        if not self.npcs:
            raise ValueError("audition NPC list must not be empty")
        if self.clear_rank > len(self.npcs) + 1:
            raise ValueError("clear_rank exceeds the player-plus-NPC field")
        if len({npc.number for npc in self.npcs}) != len(self.npcs):
            raise ValueError("NPC numbers must be unique")
        if any(len(npc.turn_scores) != self.limit_round for npc in self.npcs):
            raise ValueError("every NPC score list must cover every turn")

    @property
    def current_frame(self) -> Plan3AuditionTurnFrame:
        return self.frames[self.current_round - 1]

    @property
    def remaining_frames(self) -> tuple[Plan3AuditionTurnFrame, ...]:
        return self.frames[self.current_round - 1 :]

    def npc_scores_after_round(self, round_number: int) -> tuple[int, ...]:
        if self.is_static_npc_score:
            _integer(round_number, "round_number", minimum=0)
            if not self.current_round - 1 <= round_number <= self.limit_round:
                raise ValueError("requested NPC round is outside the available horizon")
            # ExamSequence initializes static NPCs by adding the complete
            # serialized score list before the first decision. Do not add it
            # again as though these were incremental NIA NPC tracks.
            return tuple(npc.current_score for npc in self.npcs)
        return tuple(
            npc.score_after_round(
                current_round=self.current_round,
                round_number=round_number,
            )
            for npc in self.npcs
        )

    def player_rank(self, player_score: int, *, after_round: int) -> int:
        """Match Android ``GetCurrentAuditionRank`` including tie semantics.

        The native predicate is ``npc.CurrentScore > player.Parameter``;
        equal scores therefore do not outrank the player.
        """

        _integer(player_score, "player_score", minimum=0)
        return 1 + sum(
            score > player_score
            for score in self.npc_scores_after_round(after_round)
        )

    def minimum_score_for_clear_rank(self, *, after_round: int) -> int:
        scores = sorted(
            self.npc_scores_after_round(after_round),
            reverse=True,
        )
        if self.clear_rank > len(scores):
            return 0
        # Ties favour the player, so matching the Nth NPC score is sufficient.
        return scores[self.clear_rank - 1]

    @property
    def projected_final_npc_scores(self) -> tuple[int, ...]:
        return self.npc_scores_after_round(self.limit_round)

    @property
    def projected_final_clear_score(self) -> int:
        return self.minimum_score_for_clear_rank(after_round=self.limit_round)

    @property
    def drink_count(self) -> int:
        return len(self.drink_ids)

    @property
    def boundary_model(self) -> AuditionBoundaryModel:
        """The explicit native boundary classifier for this save."""

        return AuditionBoundaryModel(
            current_turn=self.current_round,
            limit_turn=self.limit_round,
            extra_turn=self.extra_turn,
        )

    @property
    def full_horizon_gaps(self) -> tuple[Plan3AuditionHorizonGap, ...]:
        gaps: list[Plan3AuditionHorizonGap] = []
        if self.turn_end_stamina_recovery is None:
            gaps.append(
                Plan3AuditionHorizonGap(
                    "plan3-audition-force-end-recovery-unresolved",
                    "ExamSetting.examTurnEndRecoveryStamina is unavailable",
                )
            )
        if self.npc_score_multiple_permille:
            gaps.append(
                Plan3AuditionHorizonGap(
                    "plan3-audition-npc-multiplier-transition-missing",
                    "non-zero npcScoreMultiplePermil needs its native mutation timing",
                )
            )
        return tuple(gaps)


@dataclass(frozen=True, slots=True)
class Plan3AuditionCurrentActionResult:
    """One-card battle search plus an explicit wider-horizon boundary."""

    context: Plan3AuditionContext
    prepared: Plan3NativeSearchBridgeResult
    battle_state: Plan3State | None
    search: Plan3NativeSearchResult | None
    issues: tuple[Plan3NativeSearchBridgeIssue, ...] = ()
    replayed_command_effect_ids: tuple[str, ...] = ()
    search_mode: str = "current_action"
    drinks_included: bool = False
    boundary: Plan3AuditionBoundary | None = None

    @property
    def current_action_ready(self) -> bool:
        return self.search is not None and not self.issues

    @property
    def current_action_choice_complete(self) -> bool:
        if not self.current_action_ready or self.search is None:
            return False
        if any(
            diagnostic.stage != "battle-horizon-scope"
            for diagnostic in self.search.diagnostics
        ):
            return False
        # Android IsIdolStatusIsStepMax makes Concentration/Preservation step
        # 2 idempotent.  Keep the result advisory if an older engine build
        # incorrectly increments such a candidate; this guard becomes inert
        # as soon as the shared stance kernel is corrected.
        return not any(
            step.card_transition is not None
            and step.before.stance == step.after.stance
            and step.before.stance in {
                STANCE_CONCENTRATION,
                STANCE_PRESERVATION,
            }
            and step.before.stance_level >= 2
            and step.after.stance_level > step.before.stance_level
            for candidate in self.search.candidates
            for step in candidate.steps
        )

    @property
    def full_horizon_ready(self) -> bool:
        return (
            self.search_mode == "full_horizon"
            and self.current_action_choice_complete
            and not self.context.full_horizon_gaps
            and (self.drinks_included or not self.context.drink_ids)
            and self.search is not None
            and self.search.depth is None
            and self.search.best is not None
            and self.search.best.complete
        )

    @property
    def best(self):
        return None if self.search is None else self.search.best

    @property
    def boundary_outcome(self) -> AuditionBoundaryOutcome | None:
        return None if self.boundary is None else self.boundary.outcome

    @property
    def exam_end_complete(self) -> bool:
        return bool(self.boundary is not None and self.boundary.exam_end_complete)

    @property
    def next_user_action(self) -> str | None:
        if self.boundary is not None and self.boundary.terminal:
            return None
        return None if self.search is None else "search"


def _raw_exam_save(decoded: DecodedPlan3LocalSave) -> Mapping[str, object]:
    try:
        value = json.loads(decoded.envelope.plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LocalSaveDecodeError(
            "decrypted ExamSaveData body is not valid UTF-8 JSON"
        ) from error
    if not isinstance(value, Mapping):
        raise LocalSaveDecodeError("decrypted ExamSaveData root must be an object")
    return value


def _turn_end_stamina_recovery(
    setting_id: str,
    *,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> int:
    path = Path(master_dir) / "ExamSetting.yaml"
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    payload = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    if not isinstance(payload, list):
        raise ValueError("ExamSetting.yaml must contain a list")
    rows = [
        row
        for row in payload
        if isinstance(row, Mapping) and row.get("id") == setting_id
    ]
    if len(rows) != 1:
        raise KeyError(f"ExamSetting must resolve exactly once: {setting_id}")
    return _integer(
        rows[0].get("examTurnEndRecoveryStamina"),
        "ExamSetting.examTurnEndRecoveryStamina",
        minimum=0,
    )


def parse_plan3_audition_context(
    raw_exam_save: Mapping[str, object],
    *,
    turn_end_stamina_recovery: int | None = None,
) -> Plan3AuditionContext:
    """Parse schedule/NPC/rank facts without invoking any card formula."""

    if not isinstance(raw_exam_save, Mapping):
        raise TypeError("raw_exam_save must be a mapping")
    if _integer(raw_exam_save.get("examType"), "examType") != _AUDITION_EXAM_TYPE:
        raise ValueError("ExamSaveData is not an audition")
    if _integer(raw_exam_save.get("phase"), "phase") != _MAIN_PHASE:
        raise ValueError("ExamSaveData is not in the Main phase")
    current_round = _integer(
        raw_exam_save.get("currentTurn"), "currentTurn", minimum=1
    )
    limit_round = _integer(
        raw_exam_save.get("limitTurn"), "limitTurn", minimum=1
    )
    extra_turn = _integer(raw_exam_save.get("extraTurn"), "extraTurn", minimum=0)
    if extra_turn != 0:
        raise ValueError("audition extra-turn scheduling is not implemented")
    raw_types = raw_exam_save.get("turnStatusParameterTypeList")
    if not isinstance(raw_types, list) or len(raw_types) != limit_round:
        raise ValueError(
            "turnStatusParameterTypeList must contain one entry per turn"
        )
    parameter_types = tuple(
        _integer(value, f"turnStatusParameterTypeList[{index}]")
        for index, value in enumerate(raw_types)
    )
    if any(value not in _PARAMETER_TO_LESSON for value in parameter_types):
        raise ValueError("turn schedule contains an unknown parameter type")
    bonuses = {
        parameter_type: _integer(
            raw_exam_save.get(field),
            field,
            minimum=1,
        )
        for parameter_type, field in _BONUS_FIELD_BY_PARAMETER.items()
    }
    frames = tuple(
        Plan3AuditionTurnFrame(
            round_number=index,
            parameter_type=parameter_type,
            lesson_type=_PARAMETER_TO_LESSON[parameter_type],
            score_multiplier_permille=bonuses[parameter_type],
        )
        for index, parameter_type in enumerate(parameter_types, start=1)
    )
    raw_npcs = raw_exam_save.get("npcDataList")
    if not isinstance(raw_npcs, list):
        raise ValueError("npcDataList must be a list")
    npcs: list[Plan3AuditionNpcTrack] = []
    for index, value in enumerate(raw_npcs):
        row = _mapping(value, f"npcDataList[{index}]")
        raw_scores = row.get("_scoreList")
        if not isinstance(raw_scores, list):
            raise ValueError(f"npcDataList[{index}]._scoreList must be a list")
        npcs.append(
            Plan3AuditionNpcTrack(
                npc_id=(
                    row.get("_id")
                    if isinstance(row.get("_id"), str)
                    else ""
                ),
                number=_integer(
                    row.get("_number"),
                    f"npcDataList[{index}]._number",
                    minimum=1,
                ),
                turn_scores=tuple(
                    _integer(
                        score,
                        f"npcDataList[{index}]._scoreList[{turn}]",
                        minimum=0,
                    )
                    for turn, score in enumerate(raw_scores)
                ),
                current_score=_integer(
                    row.get("_currentScore"),
                    f"npcDataList[{index}]._currentScore",
                    minimum=0,
                ),
                vocal_score=_integer(
                    row.get("_vocalScore"),
                    f"npcDataList[{index}]._vocalScore",
                    minimum=0,
                ),
                dance_score=_integer(
                    row.get("_danceScore"),
                    f"npcDataList[{index}]._danceScore",
                    minimum=0,
                ),
                visual_score=_integer(
                    row.get("_visualScore"),
                    f"npcDataList[{index}]._visualScore",
                    minimum=0,
                ),
            )
        )
    npc_multiplier = _integer(
        raw_exam_save.get("npcScoreMultiplePermil"),
        "npcScoreMultiplePermil",
        minimum=0,
    )
    is_static = raw_exam_save.get("isStaticNpcScore")
    if not isinstance(is_static, bool):
        raise ValueError("isStaticNpcScore must be bool")
    if npc_multiplier == 0:
        completed_rounds = current_round - 1
        for npc in npcs:
            expected = sum(npc.turn_scores if is_static else npc.turn_scores[:completed_rounds])
            if npc.current_score != expected:
                raise ValueError(
                    "NPC current score does not match the serialized "
                    + ("static total: " if is_static else "completed turns: ")
                    + f"npc={npc.number}; expected={expected}; actual={npc.current_score}"
                )
    raw_drinks = raw_exam_save.get("drinkList")
    if not isinstance(raw_drinks, list):
        raise ValueError("drinkList must be a list")
    drink_id_values: list[str] = []
    for index, value in enumerate(raw_drinks):
        drink_id = _mapping(value, f"drinkList[{index}]").get("_id")
        if not isinstance(drink_id, str) or not drink_id:
            raise ValueError("drinkList entries must contain non-empty _id")
        drink_id_values.append(drink_id)
    drink_ids = tuple(drink_id_values)
    setting_id = raw_exam_save.get("settingId")
    if not isinstance(setting_id, str) or not setting_id:
        raise ValueError("settingId must be non-empty text")
    return Plan3AuditionContext(
        setting_id=setting_id,
        step_type_value=_integer(raw_exam_save.get("stepType"), "stepType"),
        current_round=current_round,
        limit_round=limit_round,
        clear_rank=_integer(
            raw_exam_save.get("clearBorder"), "clearBorder", minimum=1
        ),
        force_end_score=_integer(
            raw_exam_save.get("limitBorder"), "limitBorder", minimum=-1
        ),
        frames=frames,
        npcs=tuple(npcs),
        npc_score_multiple_permille=npc_multiplier,
        is_static_npc_score=is_static,
        turn_end_stamina_recovery=turn_end_stamina_recovery,
        drink_ids=drink_ids,
        extra_turn=extra_turn,
    )


def bind_plan3_audition_state(
    state: Plan3State,
    context: Plan3AuditionContext,
) -> Plan3State:
    """Apply audition semantics without changing the Plan 3 card kernel."""

    if not isinstance(state, Plan3State):
        raise TypeError("state must be Plan3State")
    if not isinstance(context, Plan3AuditionContext):
        raise TypeError("context must be Plan3AuditionContext")
    if state.round_number != context.current_round:
        raise ValueError("Plan3 state round does not match audition context")
    expected_remaining = context.limit_round - context.current_round + 1
    if state.turns_remaining != expected_remaining:
        raise ValueError("Plan3 turns_remaining does not match audition context")
    frame = context.current_frame
    bonus = {
        candidate.parameter_type: candidate.score_multiplier_permille
        for candidate in context.frames
    }
    return replace(
        state,
        # Native audition clearBorder is ranking, so it must never feed the
        # lesson-only score/is_clear gate in Plan3State.
        clear_border=-1,
        limit_border=(
            context.force_end_score
            if context.force_end_score > 0
            else -1
        ),
        is_battle=True,
        current_parameter_type=frame.parameter_type,
        lesson_type=frame.lesson_type,
        step_type_value=0,
        battle_bonus_permille_vocal=bonus[1],
        battle_bonus_permille_dance=bonus[2],
        battle_bonus_permille_visual=bonus[3],
    )


def evaluate_plan3_audition_objective(
    context: Plan3AuditionContext,
    state: Plan3State,
    *,
    boundary: Plan3AuditionBoundary | None = None,
) -> tuple[int, int, int, int, int, int, int]:
    """Evaluate rank/clear directly from an explicit native boundary."""

    if not isinstance(context, Plan3AuditionContext):
        raise TypeError("context must be Plan3AuditionContext")
    if not isinstance(state, Plan3State):
        raise TypeError("state must be Plan3State")
    if boundary is not None and not isinstance(boundary, Plan3AuditionBoundary):
        raise TypeError("boundary must be Plan3AuditionBoundary or None")
    force_end = (
        context.force_end_score > 0
        and state.score >= context.force_end_score
    )
    terminal = (
        state.turns_remaining == 0
        or force_end
        or (boundary is not None and boundary.exam_end_complete)
    )
    rank = (
        context.player_rank(
            state.score,
            after_round=context.limit_round,
        )
        if terminal
        else len(context.npcs) + 1
    )
    return (
        int(terminal),
        int(force_end),
        int(terminal and rank <= context.clear_rank),
        -rank if terminal else 0,
        state.score,
        state.stamina,
        state.block,
    )


def make_plan3_audition_objective(
    context: Plan3AuditionContext,
) -> Objective:
    """Return the existing search callback bound to native NPC rank rules."""

    if not isinstance(context, Plan3AuditionContext):
        raise TypeError("context must be Plan3AuditionContext")

    def objective(state: Plan3State):
        return evaluate_plan3_audition_objective(context, state)

    return objective


def _terminalize_plan3_boundary_path(
    path,
    context: Plan3AuditionContext,
    objective: Objective | None,
):
    """Stop a native path at the explicit last-boundary completion point."""

    for index, step in enumerate(path.steps):
        if step.kind != "turn_boundary" or step.ended_state is None:
            continue
        ended = step.ended_state
        resolved = context.boundary_model.resolve(
            next_turn=ended.round_number,
            post_boundary_recovery_units=ended.turns_remaining,
        )
        if not resolved.terminal:
            continue
        recovery = context.turn_end_stamina_recovery
        if recovery is None:
            raise ValueError(
                "ExamSetting recovery is required for an EXAM_END boundary"
            )
        if ended.max_stamina is None:
            raise ValueError(
                "max_stamina is required for an EXAM_END boundary"
            )
        recovered = min(
            ended.max_stamina,
            ended.stamina + resolved.recovery_units * recovery,
        ) - ended.stamina
        terminal_state = replace(
            ended,
            turns_remaining=0,
            stamina=ended.stamina + recovered,
            plays_remaining=0,
            awaiting_turn_start=False,
        )
        native_terminal = step.native_ended_state
        if native_terminal is None:
            raise ValueError(
                "EXAM_END boundary is missing its native post-boundary state"
            )
        projection = native_terminal.projection()
        terminal_state = replace(
            terminal_state,
            hand=projection.hand,
            draw_pile=projection.deck,
            discard_pile=projection.grave,
            lost_pile=projection.lost,
            hold_pile=projection.hold,
        )
        terminal_state.validate(allow_completed=True)
        native_terminal.assert_plan3_projection(terminal_state)
        terminal_step = replace(
            step,
            kind="audition_boundary",
            after=terminal_state,
            native_after=native_terminal,
            turn_start=None,
            native_draw=None,
            runtime_draws=(),
            add_grow_mutations=(),
            forced_end_stamina_recovered=recovered,
        )
        terminal_path = replace(
            path,
            state=terminal_state,
            native_state=native_terminal,
            steps=(*path.steps[:index], terminal_step),
            evaluation=evaluate_plan3_search_state(terminal_state, objective),
        )
        return terminal_path, Plan3AuditionBoundary(
            outcome=resolved.outcome,
            exam_end_complete=resolved.exam_end_complete,
            post_boundary_recovery_units=resolved.recovery_units,
            next_round=ended.round_number,
        )
    return path, None


def _apply_plan3_audition_boundaries(
    search: Plan3NativeSearchResult,
    context: Plan3AuditionContext,
    objective: Objective | None,
) -> tuple[Plan3NativeSearchResult, Plan3AuditionBoundary | None]:
    """Apply EXAM_END to every candidate before a next frame is exposed."""

    transformed: list[tuple[object, Plan3AuditionBoundary | None]] = []
    for path in search.candidates:
        transformed.append(
            _terminalize_plan3_boundary_path(path, context, objective)
        )
    if not transformed:
        return search, None

    def path_key(value) -> tuple[object, ...]:
        return (
            value[0].evaluation.objective,
            -len(value[0].decision_steps),
            tuple(
                (
                    step.action.card_ref.card_id,
                    step.action.card_ref.upgrade,
                    step.action.guid,
                )
                for step in value[0].decision_steps
                if step.action is not None
            ),
        )

    ranked = tuple(sorted(transformed, key=path_key, reverse=True))
    best_path, best_boundary = ranked[0]
    return replace(
        search,
        best=best_path,
        candidates=tuple(path for path, _boundary in ranked),
    ), best_boundary


def _search_plan3_audition_decoded(
    decoded: DecodedPlan3LocalSave,
    *,
    depth: int | None,
    include_skip: bool,
    include_drinks: bool,
    enable_force_end: bool,
    search_mode: str,
    raw_exam_save: Mapping[str, object] | None = None,
    beam_width: int = 64,
    objective: Objective | None = None,
    database: Path = DEFAULT_DATABASE,
    support_card_master: Path = DEFAULT_SUPPORT_CARD_MASTER,
    master_dir: Path = DEFAULT_MASTER_DIR,
    turn_start_extension: Plan3NativeTurnStartExtension | None = None,
    accepted_play_extension: Plan3NativeAcceptedPlayExtension | None = None,
    initial_turn_start_extension_state: object | None = None,
) -> Plan3AuditionCurrentActionResult:
    """Prepare one audition snapshot and invoke the shared native search."""

    if not isinstance(decoded, DecodedPlan3LocalSave):
        raise TypeError("decoded must be DecodedPlan3LocalSave")
    raw = _raw_exam_save(decoded) if raw_exam_save is None else raw_exam_save
    recovery: int | None
    try:
        recovery = _turn_end_stamina_recovery(
            decoded.exam_state.setting_id,
            master_dir=Path(master_dir),
        )
    except (KeyError, TypeError, ValueError, OSError):
        # Recovery is not needed before the current card is played.  Keep it
        # as an explicit full-horizon gap rather than blocking useful work.
        recovery = None
    context = parse_plan3_audition_context(
        raw,
        turn_end_stamina_recovery=recovery,
    )
    prepared = search_plan3_native_decoded_local_save(
        decoded,
        raw_exam_save=raw,
        beam_width=1,
        depth=0,
        database=Path(database),
        support_card_master=Path(support_card_master),
        turn_start_extension=turn_start_extension,
        accepted_play_extension=accepted_play_extension,
        initial_turn_start_extension_state=initial_turn_start_extension_state,
    )
    issues = list(prepared.issues)
    battle_state: Plan3State | None = None
    search: Plan3NativeSearchResult | None = None
    boundary: Plan3AuditionBoundary | None = None
    replayed_command_effect_ids: tuple[str, ...] = ()
    command_only_issue = bool(issues) and all(
        issue.code == "projection:not-actionable-settled"
        and issue.detail.startswith("ExamSaveData:")
        for issue in issues
    )
    if prepared.native_state is not None and (not issues or command_only_issue):
        try:
            battle_state = bind_plan3_audition_state(
                prepared.projection.state,
                context,
            )
            settings = load_plan3_exam_settings(
                decoded.exam_state.setting_id,
                master_dir=Path(master_dir),
            )
            raw_commands = raw.get("commandList")
            if isinstance(raw_commands, list) and raw_commands:
                replay = replay_completed_plan3_drink_history(
                    battle_state,
                    raw,
                    settings=settings,
                    master_dir=Path(master_dir),
                    database=Path(database),
                )
                if replay is None:
                    raise ValueError(
                        "non-empty commandList is not a supported terminal "
                        "drink history"
                    )
                battle_state = replay.after
                replayed_command_effect_ids = replay.effect_ids
                if command_only_issue:
                    issues.clear()
            force_end_enabled = (
                enable_force_end and context.force_end_score > 0
            )
            if force_end_enabled and recovery is None:
                raise ValueError(
                    "force-end recovery is unavailable for full horizon"
                )
            inventory: Plan3DrinkInventory | None = None
            if include_drinks:
                inventory = load_plan3_drink_inventory(
                    context.drink_ids,
                    master_dir=Path(master_dir),
                    database=Path(database),
                )
            search = search_plan3_native(
                battle_state,
                prepared.native_state,
                beam_width=beam_width,
                depth=depth,
                settings=settings,
                # NIA's turn-start adapter owns the complete gimmick profile.
                # Ordinary Plan3 keeps the existing generic profile path.
                gimmick_profile=(
                    None
                    if turn_start_extension is not None
                    else prepared.gimmick_profile
                ),
                objective=(
                    make_plan3_audition_objective(context)
                    if objective is None
                    else objective
                ),
                database=Path(database),
                support_upgrades=prepared.support_upgrades,
                support_card_searches=dict(prepared.support_card_searches),
                battle_parameter_schedule=tuple(
                    frame.parameter_type
                    for frame in context.remaining_frames
                ),
                battle_ranking_resolved=True,
                include_skip=include_skip,
                force_end_score=(
                    context.force_end_score if force_end_enabled else 0
                ),
                force_end_stamina_recovery=(
                    recovery
                    if force_end_enabled and recovery is not None
                    else 0
                ),
                drink_inventory=inventory,
                turn_start_extension=turn_start_extension,
                accepted_play_extension=accepted_play_extension,
                initial_turn_start_extension_state=(
                    initial_turn_start_extension_state
                ),
            )
            if search_mode == "full_horizon":
                search, boundary = _apply_plan3_audition_boundaries(
                    search,
                    context,
                    make_plan3_audition_objective(context)
                    if objective is None
                    else objective,
                )
        except (KeyError, TypeError, ValueError, OSError) as error:
            issues.append(
                Plan3NativeSearchBridgeIssue(
                    "audition-search-unavailable",
                    f"{type(error).__name__}:{error}",
                )
            )
    return Plan3AuditionCurrentActionResult(
        context=context,
        prepared=prepared,
        battle_state=battle_state,
        search=search,
        issues=tuple(issues),
        replayed_command_effect_ids=replayed_command_effect_ids,
        search_mode=search_mode,
        drinks_included=include_drinks,
        boundary=boundary,
    )


def search_plan3_audition_current_action_decoded(
    decoded: DecodedPlan3LocalSave,
    *,
    raw_exam_save: Mapping[str, object] | None = None,
    beam_width: int = 64,
    objective: Objective | None = None,
    database: Path = DEFAULT_DATABASE,
    support_card_master: Path = DEFAULT_SUPPORT_CARD_MASTER,
    master_dir: Path = DEFAULT_MASTER_DIR,
    turn_start_extension: Plan3NativeTurnStartExtension | None = None,
    accepted_play_extension: Plan3NativeAcceptedPlayExtension | None = None,
    initial_turn_start_extension_state: object | None = None,
) -> Plan3AuditionCurrentActionResult:
    """Search exactly one visible-card action in a settled Plan 3 audition."""

    return _search_plan3_audition_decoded(
        decoded,
        depth=1,
        include_skip=False,
        include_drinks=False,
        enable_force_end=False,
        search_mode="current_action",
        raw_exam_save=raw_exam_save,
        beam_width=beam_width,
        objective=objective,
        database=database,
        support_card_master=support_card_master,
        master_dir=master_dir,
        turn_start_extension=turn_start_extension,
        accepted_play_extension=accepted_play_extension,
        initial_turn_start_extension_state=initial_turn_start_extension_state,
    )


def search_plan3_audition_horizon_decoded(
    decoded: DecodedPlan3LocalSave,
    *,
    include_drinks: bool = True,
    raw_exam_save: Mapping[str, object] | None = None,
    beam_width: int = 64,
    objective: Objective | None = None,
    database: Path = DEFAULT_DATABASE,
    support_card_master: Path = DEFAULT_SUPPORT_CARD_MASTER,
    master_dir: Path = DEFAULT_MASTER_DIR,
    turn_start_extension: Plan3NativeTurnStartExtension | None = None,
    accepted_play_extension: Plan3NativeAcceptedPlayExtension | None = None,
    initial_turn_start_extension_state: object | None = None,
) -> Plan3AuditionCurrentActionResult:
    """Search every remaining turn, including skip, force end, and drinks."""

    if not isinstance(include_drinks, bool):
        raise TypeError("include_drinks must be bool")
    return _search_plan3_audition_decoded(
        decoded,
        depth=None,
        include_skip=True,
        include_drinks=include_drinks,
        enable_force_end=True,
        search_mode="full_horizon",
        raw_exam_save=raw_exam_save,
        beam_width=beam_width,
        objective=objective,
        database=database,
        support_card_master=support_card_master,
        master_dir=master_dir,
        turn_start_extension=turn_start_extension,
        accepted_play_extension=accepted_play_extension,
        initial_turn_start_extension_state=initial_turn_start_extension_state,
    )


def search_plan3_audition_current_action_bytes(
    data: bytes,
    **kwargs,
) -> Plan3AuditionCurrentActionResult:
    return search_plan3_audition_current_action_decoded(
        decode_plan3_local_save_bytes(data),
        **kwargs,
    )


def search_plan3_audition_current_action_file(
    path: str | Path,
    **kwargs,
) -> Plan3AuditionCurrentActionResult:
    return search_plan3_audition_current_action_decoded(
        decode_plan3_local_save_file(path),
        **kwargs,
    )


def search_plan3_audition_horizon_bytes(
    data: bytes,
    **kwargs,
) -> Plan3AuditionCurrentActionResult:
    return search_plan3_audition_horizon_decoded(
        decode_plan3_local_save_bytes(data),
        **kwargs,
    )


def search_plan3_audition_horizon_file(
    path: str | Path,
    **kwargs,
) -> Plan3AuditionCurrentActionResult:
    return search_plan3_audition_horizon_decoded(
        decode_plan3_local_save_file(path),
        **kwargs,
    )


__all__ = [
    "Plan3AuditionBoundary",
    "Plan3AuditionContext",
    "Plan3AuditionCurrentActionResult",
    "Plan3AuditionHorizonGap",
    "Plan3AuditionNpcTrack",
    "Plan3AuditionTurnFrame",
    "Plan3CompletedDrinkReplay",
    "bind_plan3_audition_state",
    "evaluate_plan3_audition_objective",
    "make_plan3_audition_objective",
    "parse_plan3_audition_context",
    "replay_completed_plan3_drink_history",
    "search_plan3_audition_current_action_bytes",
    "search_plan3_audition_current_action_decoded",
    "search_plan3_audition_current_action_file",
    "search_plan3_audition_horizon_bytes",
    "search_plan3_audition_horizon_decoded",
    "search_plan3_audition_horizon_file",
]
