"""Stable read-only Plan 3 audition advisor API and JSON CLI.

The adapter accepts one encrypted ``ExamSaveData`` LocalSave, delegates every
simulation to the existing Plan 3 audition horizon, and converts the best path
to an execution-neutral contract for the GUI or MAA.  It never clicks the
game.  Any decode, projection, or semantic gap returns ``status=unavailable``
with no executable first action.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

from .master_db import DEFAULT_DATABASE
from .drink_behavior_observation import DRINK_TIMING_MAX_BONUS
from .plan3_audition_search_bridge import (
    Plan3AuditionCurrentActionResult,
    search_plan3_audition_horizon_decoded,
)
from .plan3_engine import DEFAULT_MASTER_DIR
from .plan3_local_save_bridge import (
    DecodedPlan3LocalSave,
    decode_plan3_local_save_file,
)
from .plan3_native_search import Plan3NativeSearchStep
from .plan3_native_search import (
    Plan3NativeAcceptedPlayExtension,
    Plan3NativeSearchResult,
    Plan3NativeTurnStartExtension,
)
from .plan3_native_search_bridge import DEFAULT_SUPPORT_CARD_MASTER


SCHEMA_NAME = "gkms_tool.plan3_audition_advisor"
SCHEMA_VERSION = 1
STATUS_READY = "ready"
STATUS_UNAVAILABLE = "unavailable"
_AUDITION_EXAM_TYPE = 1
_AUDITION_STAGE_BY_VALUE = {
    16: "ProduceStepType_AuditionMid1",
    17: "ProduceStepType_AuditionMid2",
    18: "ProduceStepType_AuditionFinal",
}


@dataclass(frozen=True, slots=True)
class Plan3AuditionAdvisorIssue:
    code: str
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("advisor issue code must be non-empty text")
        if not isinstance(self.detail, str):
            raise TypeError("advisor issue detail must be text")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Plan3AuditionAdvisorReport:
    status: str
    issues: tuple[Plan3AuditionAdvisorIssue, ...]
    current_state: Mapping[str, object] | None
    best_decision_steps: tuple[Mapping[str, object], ...]
    first_action: Mapping[str, object] | None
    terminal: Mapping[str, object] | None
    search: Mapping[str, object]
    diagnostics: tuple[Mapping[str, object], ...]
    source: str = ""

    def __post_init__(self) -> None:
        if self.status not in {STATUS_READY, STATUS_UNAVAILABLE}:
            raise ValueError("advisor status must be ready or unavailable")
        if self.status == STATUS_READY:
            if self.issues:
                raise ValueError("ready advisor report cannot contain issues")
            if (
                self.current_state is None
                or self.first_action is None
                or self.terminal is None
            ):
                raise ValueError(
                    "ready advisor report requires state, action, and terminal"
                )
            if not self.best_decision_steps:
                raise ValueError("ready advisor report requires decision steps")
            if self.first_action != self.best_decision_steps[0]:
                raise ValueError("first action must be decision step zero")
            if self.diagnostics:
                raise ValueError("ready advisor report cannot contain diagnostics")
        elif (
            self.first_action is not None
            or self.best_decision_steps
            or self.terminal is not None
        ):
            raise ValueError(
                "unavailable advisor report cannot expose decisions or terminal"
            )

    @property
    def available(self) -> bool:
        return self.status == STATUS_READY

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": SCHEMA_NAME,
            "schema_version": SCHEMA_VERSION,
            "index_base": 0,
            "status": self.status,
            "available": self.available,
            "source": self.source,
            "issues": [issue.to_dict() for issue in self.issues],
            "current_state": (
                None if self.current_state is None else dict(self.current_state)
            ),
            "best_decision_steps": [
                dict(step) for step in self.best_decision_steps
            ],
            "first_action": (
                None if self.first_action is None else dict(self.first_action)
            ),
            "terminal": None if self.terminal is None else dict(self.terminal),
            "search": dict(self.search),
            "diagnostics": [dict(value) for value in self.diagnostics],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            indent=indent,
            allow_nan=False,
        )


def _unavailable(
    *issues: Plan3AuditionAdvisorIssue,
    source: str = "",
    current_state: Mapping[str, object] | None = None,
    search: Mapping[str, object] | None = None,
    diagnostics: tuple[Mapping[str, object], ...] = (),
) -> Plan3AuditionAdvisorReport:
    normalized = issues or (
        Plan3AuditionAdvisorIssue("advisor-unavailable", "no reason supplied"),
    )
    return Plan3AuditionAdvisorReport(
        status=STATUS_UNAVAILABLE,
        issues=tuple(normalized),
        current_state=current_state,
        best_decision_steps=(),
        first_action=None,
        terminal=None,
        search={} if search is None else search,
        diagnostics=diagnostics,
        source=source,
    )


def _empty_search_payload(
    *,
    beam_width: int,
    include_drinks: bool,
) -> dict[str, object]:
    return {
        "algorithm": "deterministic_beam",
        "optimality_guaranteed": False,
        "beam_width": beam_width,
        "depth": None,
        "include_skip": True,
        "include_drinks": include_drinks,
        "expanded_nodes": 0,
        "deduplicated_nodes": 0,
        "candidate_count": 0,
    }


def _current_state_payload(
    result: Plan3AuditionCurrentActionResult,
) -> dict[str, object] | None:
    state = result.battle_state
    native = result.prepared.native_state
    if state is None or native is None:
        return None
    frame = result.context.current_frame
    current_npc_round = result.context.current_round - 1
    return {
        "setting_id": result.context.setting_id,
        "round": state.round_number,
        "limit_round": result.context.limit_round,
        "turns_remaining": state.turns_remaining,
        "plays_remaining": state.plays_remaining,
        "score": state.score,
        "stamina": state.stamina,
        "max_stamina": state.max_stamina,
        "block": state.block,
        "stance": state.stance,
        "stance_level": state.stance_level,
        "full_power_points": state.full_power_points,
        "enthusiasm": state.enthusiasm,
        "enthusiasm_additive": state.enthusiasm_additive,
        "parameter": {
            "type": frame.parameter_type,
            "lesson_type": frame.lesson_type,
            "score_multiplier_permille": frame.score_multiplier_permille,
        },
        "current_rank": result.context.player_rank(
            state.score,
            after_round=current_npc_round,
        ),
        "clear_rank": result.context.clear_rank,
        "projected_final_clear_score": (
            result.context.projected_final_clear_score
        ),
        "remaining_parameter_schedule": [
            frame.parameter_type for frame in result.context.remaining_frames
        ],
        "force_end_score": result.context.force_end_score,
        "replayed_command_effect_ids": list(
            result.replayed_command_effect_ids
        ),
        "hand": [
            {
                "hand_index": index,
                "guid": card.guid,
                "card_id": card.card_id,
                "upgrade": card.effective_upgrade,
            }
            for index, card in enumerate(native.hand)
        ],
        "drinks": [
            {"drink_slot_index": index, "drink_id": drink_id}
            for index, drink_id in enumerate(result.context.drink_ids)
        ],
        "zone_counts": {
            "hand": len(native.hand),
            "deck": len(native.deck),
            "grave": len(native.grave),
            "lost": len(native.lost),
            "hold": len(native.hold),
        },
    }


def serialize_plan3_audition_decision(
    step: Plan3NativeSearchStep,
    sequence_index: int,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "sequence_index": sequence_index,
        "kind": step.kind,
        "round": step.before.round_number,
        "plays_remaining_before": step.before.plays_remaining,
        # All indexes in this schema are explicitly zero-based.
        "hand_index": None,
        "card_guid": None,
        "card_id": None,
        "card_name": None,
        "card_upgrade": None,
        "drink_slot_index": None,
        "drink_id": None,
        "drink_name": None,
        "selected_card_guid": None,
    }
    if step.kind == "card":
        if step.action is None:
            raise ValueError("card decision is missing its bound action")
        hand_indexes = tuple(
            index
            for index, card in enumerate(step.native_before.hand)
            if card.guid == step.action.guid
        )
        if len(hand_indexes) != 1:
            raise ValueError(
                f"card decision GUID is not unique in Hand: {step.action.guid}"
            )
        payload.update(
            {
                "hand_index": hand_indexes[0],
                "card_guid": step.action.guid,
                "card_id": step.action.card_ref.card_id,
                "card_name": (
                    None
                    if step.card_transition is None
                    else step.card_transition.card.name
                ),
                "card_upgrade": step.action.card_ref.upgrade,
            }
        )
    elif step.kind == "drink":
        if step.drink_action is None or step.drink_application is None:
            raise ValueError("drink decision is missing its bound application")
        action = step.drink_action
        inventory = step.drink_application.inventory_before
        if (
            action.slot_index < 0
            or action.slot_index >= len(inventory.drinks)
            or inventory.drinks[action.slot_index].id != action.drink_id
        ):
            raise ValueError("drink decision slot no longer matches inventory")
        payload.update(
            {
                "drink_slot_index": action.slot_index,
                "drink_id": action.drink_id,
                "drink_name": step.drink_application.use.drink.name,
                "selected_card_guid": action.selected_card_guid,
            }
        )
    elif step.kind != "skip":
        raise ValueError(f"unknown decision kind: {step.kind}")
    return payload


def _diagnostic_payloads(
    result: Plan3AuditionCurrentActionResult,
) -> tuple[dict[str, object], ...]:
    if result.search is None:
        return ()
    return tuple(
        {
            "stage": diagnostic.stage,
            "semantic_gaps": list(diagnostic.semantic_gaps),
            "card_guid": diagnostic.card_guid,
        }
        for diagnostic in result.search.diagnostics
    )


def _search_payload(
    result: Plan3AuditionCurrentActionResult,
    *,
    beam_width: int,
    include_drinks: bool,
) -> dict[str, object]:
    search = result.search
    if search is None:
        return _empty_search_payload(
            beam_width=beam_width,
            include_drinks=include_drinks,
        )
    return {
        "algorithm": "deterministic_beam",
        "optimality_guaranteed": False,
        "beam_width": search.beam_width,
        "depth": search.depth,
        "include_skip": search.include_skip,
        "include_drinks": include_drinks,
        "expanded_nodes": search.expanded_nodes,
        "deduplicated_nodes": search.deduplicated_nodes,
        "candidate_count": len(search.candidates),
    }


def _plan3_timing_state(step: Plan3NativeSearchStep) -> dict[str, object]:
    """Project the same compact native fields used by the timing prior."""

    state = step.before
    return {
        "current_turn": state.round_number,
        "remain_turn": state.turns_remaining,
        "score": state.score,
        "stamina": state.stamina,
        "block": state.block,
        "parameter": state.current_parameter_type,
    }


def _apply_drink_timing_advisory(
    search: Plan3NativeSearchResult,
    *,
    timing_prior: object | None,
    timing_flow_id: str | None,
    stage: str,
) -> tuple[Plan3NativeSearchResult, dict[str, object]]:
    """Tie-break equal native paths with a bounded timing bonus.

    The exact Plan3 search remains the source of legal candidates and the
    primary objective.  This helper only reorders already-returned paths when
    their native objective is equal; it cannot authorize an illegal/new
    drink or change the search expansion/deduplication contract.
    """

    base = {
        "enabled": False,
        "scope": timing_flow_id,
        "bonus_cap": DRINK_TIMING_MAX_BONUS,
        "applied_bonus": 0,
        "scored_drink_count": 0,
        "reason": "native-action-state-missing",
    }
    if timing_prior is None:
        return search, base
    summary = getattr(timing_prior, "summary", None)
    summary_value = summary() if callable(summary) else {}
    if not isinstance(summary_value, Mapping):
        summary_value = {}
    observation_count = summary_value.get(
        "observation_count", summary_value.get("timing_observation_count", 0)
    )
    if observation_count == 0:
        base.update(
            {
                "source": summary_value.get("schema"),
                "reason": "no-state-conditioned-evidence",
            }
        )
        return search, base
    bonus_for_state = getattr(timing_prior, "bonus_for_state", None)
    if not callable(bonus_for_state):
        base.update(
            {
                "source": summary_value.get("schema"),
                "reason": "timing-prior-interface-unavailable",
            }
        )
        return search, base

    scored_paths: list[
        tuple[object, int, int, tuple[str, ...], int, object]
    ] = []
    scored_count = 0
    for path in search.candidates:
        path_bonus = 0
        path_scored = 0
        for step in path.decision_steps:
            if (
                step.kind != "drink"
                or step.drink_action is None
                or step.drink_application is None
            ):
                continue
            inventory = step.drink_application.inventory_before
            candidate_ids = tuple(value.id for value in inventory.drinks)
            try:
                bonus = bonus_for_state(
                    flow_id=timing_flow_id,
                    stage=stage,
                    round_number=step.before.round_number,
                    candidate_drink_ids=candidate_ids,
                    state_before=_plan3_timing_state(step),
                    drink_id=step.drink_action.drink_id,
                )
            except (TypeError, ValueError, KeyError):
                bonus = 0
            if isinstance(bonus, int) and not isinstance(bonus, bool) and bonus > 0:
                path_bonus += bonus
                path_scored += 1
        path_bonus = min(DRINK_TIMING_MAX_BONUS, path_bonus)
        scored_count += path_scored
        tie_ids = tuple(
            step.drink_action.drink_id
            for step in path.decision_steps
            if step.kind == "drink" and step.drink_action is not None
        )
        scored_paths.append(
            (
                path.evaluation.objective,
                path_bonus,
                -len(path.decision_steps),
                tie_ids,
                path_scored,
                path,
            )
        )
    if not scored_paths:
        base.update(
            {
                "source": summary_value.get("schema"),
                "reason": "no-search-candidates",
            }
        )
        return search, base
    # ``search.best`` is the formal advisor's decision.  Timing may only
    # choose another path whose *exact native objective tuple* is equal to
    # that decision.  In particular, do not re-rank every candidate here:
    # doing so could change a non-equal objective if a caller supplies an
    # unsorted/partial candidate list, even though timing is advisory only.
    baseline = search.best
    if baseline is None:
        base.update(
            {
                "source": summary_value.get("schema"),
                "reason": "no-search-best",
            }
        )
        return search, base
    baseline_objective = baseline.evaluation.objective
    tied_paths = [
        item for item in scored_paths if item[0] == baseline_objective
    ]
    tied_scored = [item for item in tied_paths if item[1] > 0]
    tied_scored_count = sum(item[4] for item in tied_paths)
    if not tied_scored:
        # Positive timing evidence on a path with a different formal
        # objective is intentionally visible only as a diagnostic.  The
        # returned search/result remains byte-for-byte decision-invariant.
        reason = (
            "non-equal-objective-ignored"
            if scored_count > 0
            else "no-matching-timing-context"
        )
        base.update(
            {
                "source": summary_value.get("schema"),
                "reason": reason,
                "scored_drink_count": scored_count,
            }
        )
        return search, base
    # Sort only the equal-objective tie group.  Keep all non-tie candidates at
    # their original positions so this advisory cannot change formal search
    # ordering outside the tie it is explicitly allowed to break.
    ranked_ties = sorted(
        tied_paths,
        key=lambda item: (item[1], item[2], tuple(item[3])),
        reverse=True,
    )
    ranked_candidates = list(search.candidates)
    tie_positions = [
        index
        for index, path in enumerate(search.candidates)
        if path.evaluation.objective == baseline_objective
    ]
    for index, item in zip(tie_positions, ranked_ties, strict=False):
        ranked_candidates[index] = item[5]
    selected = ranked_ties[0][5]
    base.update(
        {
            "enabled": True,
            "source": summary_value.get("schema"),
            "reason": "bounded-equal-objective-tiebreak",
            "applied_bonus": ranked_ties[0][1],
            "scored_drink_count": tied_scored_count,
        }
    )
    return replace(search, best=selected, candidates=tuple(ranked_candidates)), base


def build_plan3_audition_advisor_report(
    result: Plan3AuditionCurrentActionResult,
    *,
    source: str = "",
    beam_width: int = 64,
    include_drinks: bool = True,
    drink_timing_prior: object | None = None,
    drink_timing_flow_id: str | None = None,
    leaderboard_drink_prior: object | None = None,
) -> Plan3AuditionAdvisorReport:
    """Convert a completed horizon result to the stable advisor contract."""

    if not isinstance(result, Plan3AuditionCurrentActionResult):
        raise TypeError("result must be Plan3AuditionCurrentActionResult")
    current = _current_state_payload(result)
    diagnostics = _diagnostic_payloads(result)
    if drink_timing_prior is not None and leaderboard_drink_prior is not None:
        if drink_timing_prior is not leaderboard_drink_prior:
            raise ValueError(
                "drink_timing_prior and leaderboard_drink_prior disagree"
            )
    if drink_timing_prior is None:
        drink_timing_prior = leaderboard_drink_prior
    search_payload = _search_payload(
        result,
        beam_width=beam_width,
        include_drinks=include_drinks,
    )
    search = result.search
    if search is not None and drink_timing_prior is not None:
        search, timing_advisory = _apply_drink_timing_advisory(
            search,
            timing_prior=drink_timing_prior,
            timing_flow_id=drink_timing_flow_id,
            stage=_AUDITION_STAGE_BY_VALUE.get(
                result.context.step_type_value,
                str(result.context.step_type_value),
            ),
        )
    else:
        timing_advisory = {
            "enabled": False,
            "scope": drink_timing_flow_id,
            "bonus_cap": DRINK_TIMING_MAX_BONUS,
            "applied_bonus": 0,
            "scored_drink_count": 0,
            "reason": "native-action-state-missing",
        }
    if current is not None:
        current["drink_timing_advisory"] = dict(timing_advisory)
    search_payload["drink_timing_advisory"] = timing_advisory
    issues: list[Plan3AuditionAdvisorIssue] = [
        Plan3AuditionAdvisorIssue(issue.code, issue.detail)
        for issue in result.issues
    ]
    issues.extend(
        Plan3AuditionAdvisorIssue(gap.code, gap.detail)
        for gap in result.context.full_horizon_gaps
    )
    for diagnostic in diagnostics:
        stage = str(diagnostic["stage"])
        gaps = diagnostic["semantic_gaps"]
        assert isinstance(gaps, list)
        issues.extend(
            Plan3AuditionAdvisorIssue(
                f"search-diagnostic:{stage}",
                str(gap),
            )
            for gap in gaps
        )
    issue_keys = tuple(
        dict.fromkeys((issue.code, issue.detail) for issue in issues)
    )
    issues = [
        Plan3AuditionAdvisorIssue(code, detail)
        for code, detail in issue_keys
    ]
    if not result.full_horizon_ready and not issues:
        issues.append(
            Plan3AuditionAdvisorIssue(
                "full-horizon-not-ready",
                "search did not produce an exact executable terminal path",
            )
        )
    if issues:
        return _unavailable(
            *issues,
            source=source,
            current_state=current,
            search=search_payload,
            diagnostics=diagnostics,
        )

    # ``search`` may have been tie-broken by the bounded timing advisory.
    assert search is not None and search.best is not None
    best = search.best
    try:
        decisions = tuple(
            serialize_plan3_audition_decision(step, index)
            for index, step in enumerate(best.decision_steps)
        )
    except (TypeError, ValueError) as error:
        return _unavailable(
            Plan3AuditionAdvisorIssue(
                "decision-binding-unavailable",
                f"{type(error).__name__}:{error}",
            ),
            source=source,
            current_state=current,
            search=search_payload,
            diagnostics=diagnostics,
        )
    if not decisions:
        return _unavailable(
            Plan3AuditionAdvisorIssue(
                "no-executable-action",
                "terminal path contains no drink, card, or skip decision",
            ),
            source=source,
            current_state=current,
            search=search_payload,
            diagnostics=diagnostics,
        )

    force_steps = tuple(
        step for step in best.steps if step.kind == "force_end"
    )
    terminal = {
        "complete": best.complete,
        "score": best.state.score,
        "stamina": best.state.stamina,
        "max_stamina": best.state.max_stamina,
        "rank": result.context.player_rank(
            best.state.score,
            after_round=result.context.limit_round,
        ),
        "force_end": bool(force_steps),
        "force_end_stamina_recovered": sum(
            step.forced_end_stamina_recovered for step in force_steps
        ),
    }
    return Plan3AuditionAdvisorReport(
        status=STATUS_READY,
        issues=(),
        current_state=current,
        best_decision_steps=decisions,
        first_action=decisions[0],
        terminal=terminal,
        search=search_payload,
        diagnostics=diagnostics,
        source=source,
    )


# Public descriptive alias for callers that want to inspect the bounded
# tie-break without constructing a report themselves.
apply_drink_timing_advisory = _apply_drink_timing_advisory


def advise_plan3_audition_decoded(
    decoded: DecodedPlan3LocalSave,
    *,
    source: str = "",
    beam_width: int = 64,
    include_drinks: bool = True,
    database: Path = DEFAULT_DATABASE,
    support_card_master: Path = DEFAULT_SUPPORT_CARD_MASTER,
    master_dir: Path = DEFAULT_MASTER_DIR,
    turn_start_extension: Plan3NativeTurnStartExtension | None = None,
    accepted_play_extension: Plan3NativeAcceptedPlayExtension | None = None,
    initial_turn_start_extension_state: object | None = None,
    drink_timing_prior: object | None = None,
    drink_timing_flow_id: str | None = None,
    leaderboard_drink_prior: object | None = None,
) -> Plan3AuditionAdvisorReport:
    """Advise one already-decrypted LocalSave without executing any action."""

    if not isinstance(decoded, DecodedPlan3LocalSave):
        raise TypeError("decoded must be DecodedPlan3LocalSave")
    if decoded.exam_state.exam_type != _AUDITION_EXAM_TYPE:
        return _unavailable(
            Plan3AuditionAdvisorIssue(
                "not-audition",
                f"exam_type={decoded.exam_state.exam_type}",
            ),
            source=source,
            search=_empty_search_payload(
                beam_width=beam_width,
                include_drinks=include_drinks,
            ),
        )
    try:
        result = search_plan3_audition_horizon_decoded(
            decoded,
            include_drinks=include_drinks,
            beam_width=beam_width,
            database=Path(database),
            support_card_master=Path(support_card_master),
            master_dir=Path(master_dir),
            turn_start_extension=turn_start_extension,
            accepted_play_extension=accepted_play_extension,
            initial_turn_start_extension_state=(
                initial_turn_start_extension_state
            ),
        )
    except Exception as error:
        return _unavailable(
            Plan3AuditionAdvisorIssue(
                "audition-search-error",
                f"{type(error).__name__}:{error}",
            ),
            source=source,
            search=_empty_search_payload(
                beam_width=beam_width,
                include_drinks=include_drinks,
            ),
        )
    return build_plan3_audition_advisor_report(
        result,
        source=source,
        beam_width=beam_width,
        include_drinks=include_drinks,
        drink_timing_prior=drink_timing_prior,
        drink_timing_flow_id=drink_timing_flow_id,
        leaderboard_drink_prior=leaderboard_drink_prior,
    )


def advise_plan3_audition_file(
    path: str | Path,
    *,
    beam_width: int = 64,
    include_drinks: bool = True,
    database: Path = DEFAULT_DATABASE,
    support_card_master: Path = DEFAULT_SUPPORT_CARD_MASTER,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> Plan3AuditionAdvisorReport:
    """Decode one encrypted LocalSave and return a fail-closed advisor report."""

    source_path = Path(path)
    source = str(source_path.resolve())
    try:
        decoded = decode_plan3_local_save_file(source_path)
    except Exception as error:
        return _unavailable(
            Plan3AuditionAdvisorIssue(
                "local-save-unavailable",
                f"{type(error).__name__}:{error}",
            ),
            source=source,
            search=_empty_search_payload(
                beam_width=beam_width,
                include_drinks=include_drinks,
            ),
        )
    return advise_plan3_audition_decoded(
        decoded,
        source=source,
        beam_width=beam_width,
        include_drinks=include_drinks,
        database=Path(database),
        support_card_master=Path(support_card_master),
        master_dir=Path(master_dir),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read one encrypted Plan 3 audition LocalSave and emit JSON advice."
    )
    parser.add_argument("local_save", type=Path)
    parser.add_argument("--beam-width", type=int, default=64)
    parser.add_argument(
        "--no-drinks",
        action="store_true",
        help="diagnostic mode; omitting available drink branches can make advice unavailable",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)

    report = advise_plan3_audition_file(
        args.local_save,
        beam_width=args.beam_width,
        include_drinks=not args.no_drinks,
    )
    encoded = report.to_json(indent=None if args.compact else 2) + "\n"
    if args.output is not None:
        target = args.output.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(encoded, encoding="utf-8")
    else:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(encoded, end="")
    return 0 if report.available else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "STATUS_READY",
    "STATUS_UNAVAILABLE",
    "Plan3AuditionAdvisorIssue",
    "Plan3AuditionAdvisorReport",
    "advise_plan3_audition_decoded",
    "advise_plan3_audition_file",
    "apply_drink_timing_advisory",
    "build_plan3_audition_advisor_report",
    "main",
    "serialize_plan3_audition_decision",
]
