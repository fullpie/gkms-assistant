"""Stable read-only advisor for one settled Plan 3 lesson.

This is the lesson counterpart of :mod:`gkms_tool.plan3_audition_advisor`.
It consumes the authoritative encrypted ``ExamSaveData`` state, runs the
shared GUID/RNG-aware Plan 3 simulator for the complete remaining lesson, and
returns one execution-neutral first action.  It never captures or clicks the
game.  Decode, projection, runtime-card, drink, or search gaps always produce
an unavailable report with no action.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .master_db import DEFAULT_DATABASE
from .plan3_audition_advisor import serialize_plan3_audition_decision
from .plan3_card_history import (
    Plan3CompletedCardReplay,
    replay_completed_plan3_card_history,
)
from .plan3_drink import load_plan3_drink_inventory
from .plan3_drink_history import (
    Plan3CompletedDrinkReplay,
    replay_completed_plan3_drink_history,
)
from .plan3_engine import (
    DEFAULT_MASTER_DIR,
    LESSON_DANCE,
    LESSON_VISUAL,
    LESSON_VOCAL,
    Plan3State,
    load_plan3_exam_settings,
)
from .plan3_local_save_bridge import (
    DecodedPlan3LocalSave,
    decode_plan3_local_save_file,
)
from .plan3_native_search import Plan3NativeSearchResult, search_plan3_native
from .plan3_native_search_bridge import (
    DEFAULT_SUPPORT_CARD_MASTER,
    Plan3NativeSearchBridgeResult,
    search_plan3_native_decoded_local_save,
)
from .plan3_native_state import Plan3NativeState


SCHEMA_NAME = "gkms_tool.plan3_lesson_advisor"
SCHEMA_VERSION = 1
STATUS_READY = "ready"
STATUS_UNAVAILABLE = "unavailable"
_LESSON_EXAM_TYPE = 0
_LESSON_TYPES = {LESSON_VOCAL, LESSON_DANCE, LESSON_VISUAL}


@dataclass(frozen=True, slots=True)
class Plan3LessonAdvisorIssue:
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
class Plan3LessonAdvisorReport:
    status: str
    issues: tuple[Plan3LessonAdvisorIssue, ...]
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
            if self.issues or self.diagnostics:
                raise ValueError("ready advisor report cannot contain gaps")
            if (
                self.current_state is None
                or self.first_action is None
                or self.terminal is None
                or not self.best_decision_steps
            ):
                raise ValueError("ready advisor report is incomplete")
            if self.first_action != self.best_decision_steps[0]:
                raise ValueError("first action must be decision step zero")
        elif (
            self.first_action is not None
            or self.best_decision_steps
            or self.terminal is not None
        ):
            raise ValueError("unavailable report cannot expose a decision")

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
            self.to_dict(), ensure_ascii=False, indent=indent, allow_nan=False
        )


def _empty_search_payload(
    *, beam_width: int, include_drinks: bool
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


def _unavailable(
    *issues: Plan3LessonAdvisorIssue,
    source: str = "",
    current_state: Mapping[str, object] | None = None,
    search: Mapping[str, object] | None = None,
    diagnostics: tuple[Mapping[str, object], ...] = (),
) -> Plan3LessonAdvisorReport:
    normalized = issues or (
        Plan3LessonAdvisorIssue("advisor-unavailable", "no reason supplied"),
    )
    return Plan3LessonAdvisorReport(
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


def _raw_exam_save(decoded: DecodedPlan3LocalSave) -> Mapping[str, object]:
    value = json.loads(decoded.envelope.plaintext.decode("utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("decrypted ExamSaveData root must be an object")
    return value


def _drink_ids(raw: Mapping[str, object]) -> tuple[str, ...]:
    values = raw.get("drinkList")
    if not isinstance(values, list):
        raise ValueError("ExamSaveData.drinkList must be a list")
    result: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            raise ValueError(f"drinkList[{index}] must be an object")
        drink_id = value.get("_id")
        if not isinstance(drink_id, str) or not drink_id:
            raise ValueError(f"drinkList[{index}]._id is invalid")
        result.append(drink_id)
    return tuple(result)


def _current_state_payload(
    prepared: Plan3NativeSearchBridgeResult,
    drink_ids: tuple[str, ...],
    *,
    state: Plan3State | None = None,
    native_state: Plan3NativeState | None = None,
    completed_drink_replay: Plan3CompletedDrinkReplay | None = None,
    completed_card_replay: Plan3CompletedCardReplay | None = None,
) -> dict[str, object] | None:
    native = prepared.native_state if native_state is None else native_state
    scalar = prepared.projection.state if state is None else state
    if native is None:
        return None
    if not isinstance(scalar, Plan3State):
        raise TypeError("state must be Plan3State")
    payload: dict[str, object] = {
        "setting_id": prepared.decoded.exam_state.setting_id,
        "exam_type": prepared.decoded.exam_state.exam_type,
        "round": scalar.round_number,
        "turns_remaining": scalar.turns_remaining,
        "plays_remaining": scalar.plays_remaining,
        "score": scalar.score,
        "stamina": scalar.stamina,
        "max_stamina": scalar.max_stamina,
        "block": scalar.block,
        "stance": scalar.stance,
        "stance_level": scalar.stance_level,
        "full_power_points": scalar.full_power_points,
        "enthusiasm": scalar.enthusiasm,
        "enthusiasm_additive": scalar.enthusiasm_additive,
        "lesson_type": scalar.lesson_type,
        "step_type_value": scalar.step_type_value,
        "clear_border": scalar.clear_border,
        "perfect_border": scalar.limit_border,
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
            for index, drink_id in enumerate(drink_ids)
        ],
        "zone_counts": {
            "hand": len(native.hand),
            "deck": len(native.deck),
            "grave": len(native.grave),
            "lost": len(native.lost),
            "hold": len(native.hold),
        },
    }
    if completed_drink_replay is not None:
        payload["completed_drink_replay"] = {
            "drink_id": completed_drink_replay.drink_id,
            "effect_ids": list(completed_drink_replay.effect_ids),
        }
    if completed_card_replay is not None:
        replay_payload: dict[str, object] = {
            "card_guid": completed_card_replay.card_guid,
            "card_id": completed_card_replay.card_id,
            "effect_ids": list(completed_card_replay.effect_ids),
            "remaining_plays": completed_card_replay.remaining_plays,
        }
        if completed_card_replay.chain_depth > 1:
            replay_payload.update(
                {
                    "provenance": completed_card_replay.provenance,
                    "chain_depth": completed_card_replay.chain_depth,
                    "chain_card_guids": list(
                        completed_card_replay.chain_card_guids
                    ),
                }
            )
        payload["completed_card_replay"] = replay_payload
    return payload


def _search_payload(
    search: Plan3NativeSearchResult | None,
    *,
    beam_width: int,
    include_drinks: bool,
    drinks_considered: bool | None = None,
    drink_policy: str = "",
) -> dict[str, object]:
    if search is None:
        return _empty_search_payload(
            beam_width=beam_width, include_drinks=include_drinks
        )
    payload: dict[str, object] = {
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
    if drinks_considered is not None:
        payload["drinks_considered"] = drinks_considered
    if drink_policy:
        payload["drink_policy"] = drink_policy
    return payload


def _diagnostics(
    search: Plan3NativeSearchResult | None,
) -> tuple[dict[str, object], ...]:
    if search is None:
        return ()
    return tuple(
        {
            "stage": item.stage,
            "semantic_gaps": list(item.semantic_gaps),
            "card_guid": item.card_guid,
        }
        for item in search.diagnostics
    )


def advise_plan3_lesson_decoded(
    decoded: DecodedPlan3LocalSave,
    *,
    source: str = "",
    beam_width: int = 64,
    include_drinks: bool = True,
    database: Path = DEFAULT_DATABASE,
    support_card_master: Path = DEFAULT_SUPPORT_CARD_MASTER,
    master_dir: Path = DEFAULT_MASTER_DIR,
    transition_before: DecodedPlan3LocalSave | None = None,
    transition_card_guid: str | None = None,
    transition_replay: Plan3CompletedCardReplay | None = None,
    native_preparation: Plan3NativeSearchBridgeResult | None = None,
) -> Plan3LessonAdvisorReport:
    """Search a complete settled lesson without sending controller input."""

    if not isinstance(decoded, DecodedPlan3LocalSave):
        raise TypeError("decoded must be DecodedPlan3LocalSave")
    if (transition_before is None) != (transition_card_guid is None):
        raise ValueError(
            "transition_before and transition_card_guid must be supplied together"
        )
    if transition_replay is not None and transition_before is not None:
        raise ValueError(
            "transition_replay cannot be combined with transition_before"
        )
    if native_preparation is not None:
        if transition_replay is not None or transition_before is not None:
            raise ValueError("native_preparation cannot be combined with legacy transition replay")
        if not isinstance(native_preparation, Plan3NativeSearchBridgeResult):
            raise TypeError("native_preparation must be the shared native search preparation")
        if (native_preparation.decoded.envelope.plaintext != decoded.envelope.plaintext
                or native_preparation.decoded.exam_state != decoded.exam_state):
            raise ValueError("native_preparation does not belong to the supplied lesson state")
    if transition_replay is not None and not isinstance(
        transition_replay, Plan3CompletedCardReplay
    ):
        raise TypeError("transition_replay must be Plan3CompletedCardReplay")
    empty_search = _empty_search_payload(
        beam_width=beam_width, include_drinks=include_drinks
    )
    if decoded.exam_state.exam_type != _LESSON_EXAM_TYPE:
        return _unavailable(
            Plan3LessonAdvisorIssue(
                "not-lesson", f"exam_type={decoded.exam_state.exam_type}"
            ),
            source=source,
            search=empty_search,
        )

    completed_card_replay = transition_replay
    try:
        raw = _raw_exam_save(decoded)
        drink_ids = _drink_ids(raw)
        if completed_card_replay is not None:
            persisted = completed_card_replay.persisted_after.envelope
            current = decoded.envelope
            if not (
                persisted.save_data_version == current.save_data_version
                and persisted.encrypted_body_size == current.encrypted_body_size
                and persisted.plaintext == current.plaintext
            ):
                raise ValueError(
                    "transition replay does not end at the supplied LocalSave"
                )
            prepared = completed_card_replay.prepared_before
        elif native_preparation is not None:
            prepared = native_preparation
        elif transition_before is None:
            prepared = search_plan3_native_decoded_local_save(
                decoded,
                raw_exam_save=raw,
                beam_width=1,
                depth=0,
                database=Path(database),
                support_card_master=Path(support_card_master),
            )
        else:
            completed_card_replay = replay_completed_plan3_card_history(
                transition_before,
                decoded,
                transition_card_guid,
                database=Path(database),
                support_card_master=Path(support_card_master),
            )
            if completed_card_replay is None:
                raise ValueError(
                    "card command history did not match the before-state replay"
                )
            prepared = completed_card_replay.prepared_before
    except Exception as error:
        return _unavailable(
            Plan3LessonAdvisorIssue(
                (
                    "lesson-card-replay-unavailable"
                    if transition_before is not None
                    or transition_replay is not None
                    else "lesson-state-unavailable"
                ),
                f"{type(error).__name__}:{error}",
            ),
            source=source,
            search=empty_search,
        )

    issues = (
        []
        if completed_card_replay is not None
        else [
            Plan3LessonAdvisorIssue(issue.code, issue.detail)
            for issue in prepared.issues
        ]
    )
    state = (
        prepared.projection.state
        if completed_card_replay is None
        else completed_card_replay.after
    )
    native_state = (
        prepared.native_state
        if completed_card_replay is None
        else completed_card_replay.native_after
    )
    settings = None
    completed_drink_replay: Plan3CompletedDrinkReplay | None = None
    raw_commands = raw.get("commandList")
    command_only_issue = bool(issues) and all(
        issue.code == "projection:not-actionable-settled"
        and issue.detail.startswith("ExamSaveData:")
        for issue in issues
    )
    if (
        completed_card_replay is None
        and isinstance(raw_commands, list)
        and raw_commands
        and command_only_issue
    ):
        try:
            settings = load_plan3_exam_settings(
                decoded.exam_state.setting_id,
                master_dir=Path(master_dir),
            )
            completed_drink_replay = replay_completed_plan3_drink_history(
                state,
                raw,
                settings=settings,
                master_dir=Path(master_dir),
                database=Path(database),
            )
            if completed_drink_replay is None:
                raise ValueError(
                    "non-empty commandList is not a supported terminal "
                    "drink history"
                )
            state = completed_drink_replay.after
            issues.clear()
        except (KeyError, TypeError, ValueError, OSError) as error:
            issues.append(
                Plan3LessonAdvisorIssue(
                    "lesson-drink-replay-unavailable",
                    f"{type(error).__name__}:{error}",
                )
            )
    current = _current_state_payload(
        prepared,
        drink_ids,
        state=state,
        native_state=native_state,
        completed_drink_replay=completed_drink_replay,
        completed_card_replay=completed_card_replay,
    )
    if state.is_battle or state.lesson_type not in _LESSON_TYPES:
        issues.append(
            Plan3LessonAdvisorIssue(
                "not-plan3-lesson",
                f"is_battle={state.is_battle};lesson_type={state.lesson_type}",
            )
        )
    if state.turns_remaining <= 0:
        issues.append(
            Plan3LessonAdvisorIssue(
                "lesson-complete", "there is no remaining lesson action"
            )
        )
    if native_state is None:
        issues.append(
            Plan3LessonAdvisorIssue(
                "native-state-unavailable", "GUID/RNG state was not projected"
            )
        )
    if issues:
        unique = tuple(
            Plan3LessonAdvisorIssue(code, detail)
            for code, detail in dict.fromkeys(
                (issue.code, issue.detail) for issue in issues
            )
        )
        return _unavailable(
            *unique,
            source=source,
            current_state=current,
            search=empty_search,
        )

    search: Plan3NativeSearchResult | None = None
    searched_with_drinks = False
    drink_policy = "disabled"
    try:
        if settings is None:
            settings = load_plan3_exam_settings(
                decoded.exam_state.setting_id, master_dir=Path(master_dir)
            )

        def run_search(*, with_drinks: bool) -> Plan3NativeSearchResult:
            inventory = (
                load_plan3_drink_inventory(
                    drink_ids,
                    master_dir=Path(master_dir),
                    database=Path(database),
                )
                if with_drinks
                else None
            )
            return search_plan3_native(
                state,
                native_state,
                beam_width=beam_width,
                depth=None,
                settings=settings,
                gimmick_profile=prepared.gimmick_profile,
                database=Path(database),
                support_upgrades=prepared.support_upgrades,
                support_card_searches=dict(prepared.support_card_searches),
                include_skip=True,
                force_end_score=state.limit_border,
                force_end_stamina_recovery=(
                    settings.turn_end_stamina_recovery
                ),
                drink_inventory=inventory,
            )

        # A lesson is only an intermediate resource sink.  Preserve every
        # drink whenever a complete PERFECT proof exists without one; only
        # expand consumable branches when the no-drink horizon cannot prove
        # PERFECT.  This prevents a local block/stamina tie-break from wasting
        # an item that is much more valuable in the final audition.
        search = run_search(with_drinks=False)
        if include_drinks:
            no_drink_perfect = bool(
                not search.diagnostics
                and search.best is not None
                and search.best.complete
                and search.best.evaluation.perfect
            )
            if no_drink_perfect:
                drink_policy = "preserved-perfect-without-drink"
            elif drink_ids:
                search = run_search(with_drinks=True)
                searched_with_drinks = True
                drink_policy = "enabled-no-perfect-without-drink"
            else:
                drink_policy = "no-drinks-available"
    except Exception as error:
        return _unavailable(
            Plan3LessonAdvisorIssue(
                "lesson-search-unavailable", f"{type(error).__name__}:{error}"
            ),
            source=source,
            current_state=current,
            search=empty_search,
        )

    search_payload = _search_payload(
        search,
        beam_width=beam_width,
        include_drinks=searched_with_drinks,
        drinks_considered=include_drinks,
        drink_policy=drink_policy,
    )
    diagnostics = _diagnostics(search)
    if diagnostics:
        diagnostic_issues = tuple(
            Plan3LessonAdvisorIssue(
                f"search-diagnostic:{item['stage']}", str(gap)
            )
            for item in diagnostics
            for gap in item["semantic_gaps"]
        )
        return _unavailable(
            *diagnostic_issues,
            source=source,
            current_state=current,
            search=search_payload,
            diagnostics=diagnostics,
        )
    best = search.best
    if best is None or not best.complete:
        return _unavailable(
            Plan3LessonAdvisorIssue(
                "full-horizon-not-ready",
                "search did not produce an exact terminal lesson path",
            ),
            source=source,
            current_state=current,
            search=search_payload,
        )
    try:
        decisions = tuple(
            serialize_plan3_audition_decision(step, index)
            for index, step in enumerate(best.decision_steps)
        )
    except (TypeError, ValueError) as error:
        return _unavailable(
            Plan3LessonAdvisorIssue(
                "decision-binding-unavailable",
                f"{type(error).__name__}:{error}",
            ),
            source=source,
            current_state=current,
            search=search_payload,
        )
    if not decisions:
        return _unavailable(
            Plan3LessonAdvisorIssue(
                "no-executable-action", "terminal path contains no decision"
            ),
            source=source,
            current_state=current,
            search=search_payload,
        )

    force_steps = tuple(
        step for step in best.steps if step.kind == "force_end"
    )
    terminal = {
        "complete": best.evaluation.complete,
        "clear": best.evaluation.clear,
        "perfect": best.evaluation.perfect,
        "score": best.evaluation.score,
        "stamina": best.evaluation.stamina,
        "block": best.evaluation.block,
        "force_end": bool(force_steps),
        "force_end_stamina_recovered": sum(
            step.forced_end_stamina_recovered for step in force_steps
        ),
        "drinks_used": (
            len(search.initial_drink_ids) - len(best.drink_inventory.drinks)
            if searched_with_drinks
            else 0
        ),
        "drinks_remaining": (
            len(best.drink_inventory.drinks)
            if searched_with_drinks
            else len(drink_ids)
        ),
    }
    return Plan3LessonAdvisorReport(
        status=STATUS_READY,
        issues=(),
        current_state=current,
        best_decision_steps=decisions,
        first_action=decisions[0],
        terminal=terminal,
        search=search_payload,
        diagnostics=(),
        source=source,
    )


def advise_plan3_lesson_file(
    path: str | Path,
    *,
    beam_width: int = 64,
    include_drinks: bool = True,
    database: Path = DEFAULT_DATABASE,
    support_card_master: Path = DEFAULT_SUPPORT_CARD_MASTER,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> Plan3LessonAdvisorReport:
    source_path = Path(path)
    source = str(source_path.resolve())
    try:
        decoded = decode_plan3_local_save_file(source_path)
    except Exception as error:
        return _unavailable(
            Plan3LessonAdvisorIssue(
                "local-save-unavailable", f"{type(error).__name__}:{error}"
            ),
            source=source,
            search=_empty_search_payload(
                beam_width=beam_width, include_drinks=include_drinks
            ),
        )
    return advise_plan3_lesson_decoded(
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
        description="Read one encrypted Plan 3 lesson LocalSave and emit JSON advice."
    )
    parser.add_argument("local_save", type=Path)
    parser.add_argument("--beam-width", type=int, default=64)
    parser.add_argument("--no-drinks", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    report = advise_plan3_lesson_file(
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
    "Plan3LessonAdvisorIssue",
    "Plan3LessonAdvisorReport",
    "advise_plan3_lesson_decoded",
    "advise_plan3_lesson_file",
    "main",
]
