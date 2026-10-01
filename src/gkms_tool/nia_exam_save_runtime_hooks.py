"""Build exact NIA search hooks from one settled ExamSave snapshot.

This adapter needs no outer Produce save.  The current ExamSave supplies the
mode/card identity, one native gimmick group, step/turn identity, multiplier
runtime and future/past deck groups; Master supplies the unique audition.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .audition_local_save_state import LocalSaveExamState
from .master_db import DEFAULT_DATABASE
from .nia_accepted_play_extension import (
    NiaAcceptedPlaySearchExtension,
    build_nia_accepted_play_search_extension,
)
from .nia_native_search import (
    NIA_EXAM_MAIN_PHASE,
    NiaSearchRuntimeIdentity,
    NiaSearchTurnStartProfile,
    NiaSearchTurnStartRuntime,
    NiaTurnStartSearchExtension,
    build_nia_turn_start_search_extension,
)
from .nia_static_adapter import DEFAULT_MASTER_DIR, load_nia_static_bundle
from .plan3_engine import Plan3State
from .plan3_native_state import Plan3NativeCard


NIA_PRODUCE_ID = "produce-004"


class NiaExamSaveRuntimeHooksError(ValueError):
    """The ExamSave cannot select one exact NIA runtime profile."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


@dataclass(frozen=True, slots=True)
class NiaExamSaveRuntimeHooks:
    profile: NiaSearchTurnStartProfile
    identity: NiaSearchRuntimeIdentity
    turn_start_extension: NiaTurnStartSearchExtension
    accepted_play_extension: NiaAcceptedPlaySearchExtension
    runtime: NiaSearchTurnStartRuntime


def _text(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise NiaExamSaveRuntimeHooksError("identity-field-shape", field)
    return raw


def _int(raw: object, field: str, *, minimum: int = 0) -> int:
    if (
        isinstance(raw, bool)
        or not isinstance(raw, int)
        or raw < minimum
    ):
        raise NiaExamSaveRuntimeHooksError("identity-field-shape", field)
    return raw


def _gimmick_group_id(raw_exam_save: Mapping[str, object]) -> str:
    rows = raw_exam_save.get("gimmickList")
    if not isinstance(rows, list):
        raise NiaExamSaveRuntimeHooksError(
            "gimmick-list-shape", "gimmickList"
        )
    groups: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise NiaExamSaveRuntimeHooksError(
                "gimmick-list-shape", f"gimmickList[{index}]"
            )
        group_id = _text(
            row.get("gimmickGroupId"),
            f"gimmickList[{index}].gimmickGroupId",
        )
        if group_id not in groups:
            groups.append(group_id)
    if len(groups) != 1:
        raise NiaExamSaveRuntimeHooksError(
            "gimmick-group-arity", repr(groups)
        )
    return groups[0]


def _native_groups(
    groups,
    *,
    database: Path,
    master_dir: Path,
) -> tuple[tuple[Plan3NativeCard, ...], ...]:
    return tuple(
        tuple(
            Plan3NativeCard.from_local_save(
                card,
                database=database,
                master_dir=master_dir,
            )
            for card in group
        )
        for group in groups
    )


def build_nia_exam_save_runtime_hooks(
    raw_exam_save: Mapping[str, object],
    exam_state: LocalSaveExamState,
    scalar_state: Plan3State,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaExamSaveRuntimeHooks:
    """Bind the exact current NIA audition and its two native search hooks."""

    if not isinstance(raw_exam_save, Mapping):
        raise TypeError("raw_exam_save must be a mapping")
    if not isinstance(exam_state, LocalSaveExamState):
        raise TypeError("exam_state must be LocalSaveExamState")
    if not isinstance(scalar_state, Plan3State):
        raise TypeError("scalar_state must be Plan3State")
    database = Path(database)
    master_dir = Path(master_dir)

    produce_id = _text(raw_exam_save.get("produceId"), "produceId")
    if produce_id != NIA_PRODUCE_ID:
        raise NiaExamSaveRuntimeHooksError(
            "produce-id-mismatch", produce_id
        )
    idol_card_id = _text(raw_exam_save.get("idolCardId"), "idolCardId")
    phase = _int(raw_exam_save.get("phase"), "phase")
    step_type_value = _int(raw_exam_save.get("stepType"), "stepType")
    limit_turn = _int(
        raw_exam_save.get("limitTurn"), "limitTurn", minimum=1
    )
    if phase != NIA_EXAM_MAIN_PHASE:
        raise NiaExamSaveRuntimeHooksError("phase-mismatch", str(phase))
    group_id = _gimmick_group_id(raw_exam_save)

    bundle = load_nia_static_bundle(
        idol_card_id,
        produce_id=produce_id,
        master_dir=master_dir,
    )
    auditions = tuple(
        audition
        for audition in bundle.auditions
        if audition.rules.gimmick_group_id == group_id
    )
    if len(auditions) != 1:
        raise NiaExamSaveRuntimeHooksError(
            "audition-profile-arity",
            f"group={group_id};matches={len(auditions)}",
        )
    profile = NiaSearchTurnStartProfile.from_audition(
        produce_id,
        auditions[0],
    )
    if (
        profile.identity.step_type_value != step_type_value
        or profile.identity.turns != limit_turn
    ):
        raise NiaExamSaveRuntimeHooksError(
            "audition-profile-identity-mismatch",
            (
                f"step={step_type_value}/{profile.identity.step_type_value};"
                f"turns={limit_turn}/{profile.identity.turns}"
            ),
        )
    identity = NiaSearchRuntimeIdentity(
        phase=phase,
        step_type_value=step_type_value,
        produce_id=produce_id,
        step_type=profile.identity.step_type,
        audition_number=profile.identity.audition_number,
        group_id=group_id,
    )
    turn_start = build_nia_turn_start_search_extension(
        profile,
        identity,
        database=database,
        master_dir=master_dir,
    )
    accepted_play = build_nia_accepted_play_search_extension(
        profile,
        identity,
        database=database,
        master_dir=master_dir,
    )
    blockers = (*turn_start.gate_blockers, *accepted_play.gate_blockers)
    if blockers:
        raise NiaExamSaveRuntimeHooksError(
            "extension-gate", ",".join(dict.fromkeys(blockers))
        )
    if exam_state.past_deck is None:
        raise NiaExamSaveRuntimeHooksError(
            "past-deck-runtime-missing"
        )
    runtime = NiaSearchTurnStartRuntime(
        multiplier_state=scalar_state.lesson_parameter_multiple_state,
        future_decks=_native_groups(
            exam_state.future_deck,
            database=database,
            master_dir=master_dir,
        ),
        past_decks=_native_groups(
            exam_state.past_deck,
            database=database,
            master_dir=master_dir,
        ),
        # A settled Main snapshot has already run TurnStart for this turn.
        applied_turns=tuple(range(1, exam_state.current_turn + 1)),
    )
    return NiaExamSaveRuntimeHooks(
        profile,
        identity,
        turn_start,
        accepted_play,
        runtime,
    )


__all__ = [
    "NIA_PRODUCE_ID",
    "NiaExamSaveRuntimeHooks",
    "NiaExamSaveRuntimeHooksError",
    "build_nia_exam_save_runtime_hooks",
]
