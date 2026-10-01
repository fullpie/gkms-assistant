"""Fail-closed N.I.A. Plan 3 audition advisor for GUI and MAA callers.

The public mapping entry point accepts decrypted outer Produce PlayLog data,
an optional Produce lifecycle mapping, and a decrypted ``ExamSaveData``.  It
derives the exact N.I.A. identity through ``nia_outer_exam_save_adapter``,
validates the saved schedule/NPC/static context, and only then delegates to
the existing Plan 3 full-horizon advisor.  No input action is performed.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
import json
from pathlib import Path
import sys
from typing import Any

import yaml

from .audition_local_save_state import parse_local_save_exam_state
from .local_save_decoder import LocalSaveEnvelope
from .master_db import DEFAULT_DATABASE
from .nia_outer_exam_save_adapter import (
    NiaOuterExamSaveAdapterError,
    NiaOuterExamSaveComposition,
    compose_nia_outer_exam_save,
)
from .nia_native_search import (
    NiaSearchRuntimeIdentity,
    NiaSearchTurnStartProfile,
    NiaSearchTurnStartRuntime,
    build_nia_turn_start_search_extension,
)
from .nia_accepted_play_extension import (
    build_nia_accepted_play_search_extension,
)
from .nia_static_adapter import DEFAULT_MASTER_DIR
from .nia_turn_start_gimmick import inspect_nia_turn_start_profile
from .plan3_audition_advisor import (
    STATUS_UNAVAILABLE,
    Plan3AuditionAdvisorIssue,
    Plan3AuditionAdvisorReport,
    advise_plan3_audition_decoded,
    build_plan3_audition_advisor_report,
)
from .plan3_audition_search_bridge import (
    bind_plan3_audition_state,
    make_plan3_audition_objective,
    parse_plan3_audition_context,
    Plan3AuditionCurrentActionResult,
    search_plan3_audition_current_action_decoded,
)
from .plan3_card_history import (
    Plan3CompletedCardReplay,
    settle_completed_plan3_card_replay_native_state,
)
from .plan3_drink import load_plan3_drink_inventory
from .plan3_engine import load_plan3_exam_settings
from .plan3_native_search import search_plan3_native
from .plan3_local_save_bridge import (
    DecodedPlan3LocalSave,
    decode_plan3_local_save_file,
    project_plan3_local_save,
)
from .plan3_native_state import Plan3NativeCard
from .plan3_native_search_bridge import (
    DEFAULT_SUPPORT_CARD_MASTER,
    search_plan3_native_decoded_local_save,
)
from .produce_outer_local_save import (
    ProduceLifecycleState,
    ProduceOuterLocalSaveSnapshot,
    decode_produce_lifecycle_file,
    decode_produce_play_log_file,
)


_PLAN3 = "ProducePlanType_Plan3"


def _empty_search(beam_width: int, include_drinks: bool) -> dict[str, object]:
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
    code: str,
    detail: str,
    *,
    source: str,
    beam_width: int,
    include_drinks: bool,
    context: Mapping[str, object] | None = None,
) -> Plan3AuditionAdvisorReport:
    current = None if context is None else {"nia_context": dict(context)}
    return Plan3AuditionAdvisorReport(
        status=STATUS_UNAVAILABLE,
        issues=(Plan3AuditionAdvisorIssue(code, detail),),
        current_state=current,
        best_decision_steps=(),
        first_action=None,
        terminal=None,
        search=_empty_search(beam_width, include_drinks),
        diagnostics=(),
        source=source,
    )


def _raw_mapping(decoded: DecodedPlan3LocalSave) -> Mapping[str, object]:
    value = json.loads(decoded.envelope.plaintext.decode("utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("ExamSaveData root must be an object")
    return value


def _decoded_mapping(exam_save: Mapping[str, object]) -> DecodedPlan3LocalSave:
    plaintext = json.dumps(
        exam_save,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return DecodedPlan3LocalSave(
        envelope=LocalSaveEnvelope(
            save_data_version=0,
            encrypted_body_size=len(plaintext),
            plaintext=plaintext,
        ),
        exam_state=parse_local_save_exam_state(exam_save),
    )


def _load_idol_card_row(
    idol_card_id: str, master_dir: Path
) -> Mapping[str, Any]:
    path = master_dir / "IdolCard.yaml"
    payload = yaml.load(
        path.read_text(encoding="utf-8"),
        Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader),
    )
    if not isinstance(payload, list):
        raise ValueError("IdolCard.yaml must contain a list")
    rows = [
        row
        for row in payload
        if isinstance(row, Mapping) and row.get("id") == idol_card_id
    ]
    if len(rows) != 1:
        raise KeyError(
            f"IdolCard must resolve exactly once: {idol_card_id}"
        )
    return rows[0]


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _validate_static_runtime_context(
    composition: NiaOuterExamSaveComposition,
    exam_save: Mapping[str, object],
    *,
    prefer_exam_save_runtime: bool = False,
    database: Path,
    master_dir: Path,
) -> Mapping[str, object]:
    projection = composition.projection
    blocking = tuple(
        issue
        for issue in projection.issues
        if issue.code != "pre-schedule-random-state-missing"
    )
    if blocking or projection.profile is None:
        details = ";".join(
            f"{issue.code}:{issue.field}:{issue.detail}" for issue in blocking
        )
        raise ValueError(details or "static N.I.A. profile is unavailable")
    profile = projection.profile

    idol = _load_idol_card_row(composition.idol_card_id, master_dir)
    plan_type = idol.get("planType")
    if plan_type != _PLAN3:
        raise NiaOuterExamSaveAdapterError(
            f"unsupported idol plan: {plan_type!r}"
        )

    if not prefer_exam_save_runtime:
        checks = (
            ("settingId", profile.rules.exam_setting_id),
            ("clearBorder", profile.clear_rank),
            ("limitBorder", profile.rules.force_end_score),
        )
        for field, expected in checks:
            actual = exam_save.get(field)
            if actual != expected:
                raise ValueError(
                    f"ExamSaveData.{field} {actual!r} != Master {expected!r}"
                )

    recipe = profile.turn_parameter_schedule
    actual_schedule = composition.serialized_turn_parameter_types
    prefix_length = recipe.random_turns
    if not prefer_exam_save_runtime:
        if Counter(actual_schedule[:prefix_length]) != Counter(recipe.random_pool):
            raise ValueError("saved N.I.A. random schedule prefix has wrong weights")
        if actual_schedule[prefix_length:] != recipe.fixed_tail:
            raise ValueError("saved N.I.A. schedule has the wrong fixed tail")

    raw_npcs = exam_save.get("npcDataList")
    if not isinstance(raw_npcs, list):
        raise ValueError("ExamSaveData.npcDataList must be a list")
    static_npcs = {npc.number: npc for npc in profile.npc_scores}
    if not prefer_exam_save_runtime and len(raw_npcs) != len(static_npcs):
        raise ValueError("ExamSaveData NPC count differs from N.I.A. Master")
    multiplier = _integer(
        exam_save.get("npcScoreMultiplePermil"),
        "ExamSaveData.npcScoreMultiplePermil",
    )
    for index, raw_npc in enumerate(raw_npcs):
        if not isinstance(raw_npc, Mapping):
            raise ValueError(f"npcDataList[{index}] must be an object")
        number = _integer(raw_npc.get("_number"), f"npc[{index}].number", minimum=1)
        static = static_npcs.get(number)
        if static is None:
            raise ValueError(f"unknown N.I.A. NPC number: {number}")
        if (
            not prefer_exam_save_runtime
            and raw_npc.get("_id") != profile.rules.npc_group_id
        ):
            raise ValueError(f"NPC {number} group does not match N.I.A. Master")
        scores = raw_npc.get("_scoreList")
        if not isinstance(scores, list) or len(scores) != profile.rules.turns:
            raise ValueError(f"NPC {number} score list has wrong turn count")
        total = sum(
            _integer(value, f"npc[{number}].score[{turn}]")
            for turn, value in enumerate(scores)
        )
        if (
            not prefer_exam_save_runtime
            and multiplier == 0
            and not static.score_min <= total <= static.score_max
        ):
            raise ValueError(
                f"NPC {number} score total {total} is outside static range "
                f"{static.score_min}..{static.score_max}"
            )

    gimmick_capability = inspect_nia_turn_start_profile(
        profile.gimmicks,
        database=database,
        master_dir=master_dir,
    )
    search_profile = NiaSearchTurnStartProfile.from_audition(
        composition.difficulty_key.produce_id,
        profile,
    )
    search_extension = build_nia_turn_start_search_extension(
        search_profile,
        NiaSearchRuntimeIdentity(
            phase=_integer(exam_save.get("phase"), "ExamSaveData.phase"),
            step_type_value=_integer(
                exam_save.get("stepType"), "ExamSaveData.stepType"
            ),
            produce_id=composition.difficulty_key.produce_id,
            step_type=composition.difficulty_key.step_type,
            audition_number=composition.difficulty_key.number,
            group_id=profile.rules.gimmick_group_id,
        ),
        database=database,
        master_dir=master_dir,
    )
    accepted_play_extension = build_nia_accepted_play_search_extension(
        search_profile,
        search_extension.runtime_identity,
        database=database,
        master_dir=master_dir,
    )
    turn_start_search_blockers = search_extension.gate_blockers
    horizon_blockers = list(turn_start_search_blockers)
    horizon_blockers.extend(accepted_play_extension.gate_blockers)
    profile_execution_gate = {
        "gimmick_effect_layer_executable": (
            gimmick_capability.effect_layer_executable
        ),
        "gimmick_steps": gimmick_capability.total_steps,
        "executable_gimmick_steps": gimmick_capability.executable_steps,
        "turn_start_gimmicks_search_integrated": (
            not turn_start_search_blockers
        ),
        "shared_horizon_gimmicks_executable": not horizon_blockers,
        "whole_audition_executable": not horizon_blockers,
        "blocker_codes": list(dict.fromkeys(horizon_blockers)),
        "dormant_master_gimmick_steps": [step.start_turn for step in profile.gimmicks if step.start_turn > profile.rules.turns],
    }

    from .runtime_mode_profile import load_runtime_mode_profile
    mode_profile = load_runtime_mode_profile(composition.difficulty_key.produce_id, master_dir=master_dir)
    mode_name = {"ProduceType_FirstStar": "Initial", "ProduceType_NextIdolAudition": "NIA",
                 "ProduceType_HatsuboshiIdolFestival": "HIF"}[mode_profile.produce_type]
    return {
        "mode": mode_name,
        "produce_type": mode_profile.produce_type,
        "target_resources": mode_profile.target_resources,
        "plan_type": _PLAN3,
        "idol_card_id": composition.idol_card_id,
        "produce_id": composition.difficulty_key.produce_id,
        "difficulty": (
            "Pro"
            if composition.difficulty_key.produce_id == "produce-004"
            else "Master" if composition.difficulty_key.produce_id == "produce-005"
            else mode_profile.difficulty_name
        ),
        "step_type": composition.difficulty_key.step_type,
        "audition_number": composition.difficulty_key.number,
        "difficulty_row_id": composition.difficulty_key.row_id,
        "battle_config_id": profile.rules.battle_config_id,
        "turn_schedule": list(actual_schedule),
        "pre_schedule_rng_available": (
            projection.pre_schedule_random_state is not None
        ),
        "runtime_authority": (
            "exam-save-live"
            if prefer_exam_save_runtime
            else "master-cross-checked"
        ),
        "scoring_authority": "native serialized per-attribute bonuses and NPC score rows" if prefer_exam_save_runtime else "mode-specific Master",
        "profile_execution_gate": profile_execution_gate,
    }


def _nia_search_extensions(
    composition: NiaOuterExamSaveComposition,
    decoded: DecodedPlan3LocalSave,
    exam_save: Mapping[str, object],
    *,
    database: Path,
    master_dir: Path,
):
    """Build path-local NIA hooks from the same two LocalSave authorities."""

    profile = composition.projection.profile
    if profile is None:
        raise ValueError("NIA static audition profile is unavailable")
    search_profile = NiaSearchTurnStartProfile.from_audition(
        composition.difficulty_key.produce_id,
        profile,
    )
    identity = NiaSearchRuntimeIdentity(
        phase=_integer(exam_save.get("phase"), "ExamSaveData.phase"),
        step_type_value=_integer(
            exam_save.get("stepType"), "ExamSaveData.stepType"
        ),
        produce_id=composition.difficulty_key.produce_id,
        step_type=composition.difficulty_key.step_type,
        audition_number=composition.difficulty_key.number,
        group_id=profile.rules.gimmick_group_id,
    )
    scalar = project_plan3_local_save(
        decoded.exam_state,
        database=database,
        native_card_play_counts=True,
    )
    if scalar.issues:
        raise ValueError(
            "NIA runtime state projection failed: "
            + ",".join(issue.code for issue in scalar.issues)
        )
    if decoded.exam_state.past_deck is None:
        raise ValueError("NIA pastDeckList runtime is unavailable")

    def groups(values):
        return tuple(
            tuple(Plan3NativeCard.from_local_save(card) for card in group)
            for group in values
        )

    runtime = NiaSearchTurnStartRuntime(
        multiplier_state=scalar.state.lesson_parameter_multiple_state,
        future_decks=groups(decoded.exam_state.future_deck),
        past_decks=groups(decoded.exam_state.past_deck),
        # A settled Main-phase save has already run TurnStart for its current
        # turn.  Future search paths start applying at currentTurn + 1.
        applied_turns=tuple(range(1, decoded.exam_state.current_turn + 1)),
    )
    turn_start = build_nia_turn_start_search_extension(
        search_profile,
        identity,
        database=database,
        master_dir=master_dir,
    )
    accepted_play = build_nia_accepted_play_search_extension(
        search_profile,
        identity,
        database=database,
        master_dir=master_dir,
    )
    blockers = (*turn_start.gate_blockers, *accepted_play.gate_blockers)
    if blockers:
        raise ValueError("NIA search extension gate: " + ",".join(blockers))
    return turn_start, accepted_play, runtime


def _advise_composed(
    outer: ProduceOuterLocalSaveSnapshot | Mapping[str, object],
    exam_save: Mapping[str, object],
    *,
    decoded: DecodedPlan3LocalSave | None,
    lifecycle: ProduceLifecycleState | Mapping[str, object] | None,
    source: str,
    beam_width: int,
    include_drinks: bool,
    prefer_exam_save_runtime: bool,
    database: Path,
    support_card_master: Path,
    master_dir: Path,
    drink_timing_prior: object | None,
    drink_timing_flow_id: str | None,
    leaderboard_drink_prior: object | None,
) -> Plan3AuditionAdvisorReport:
    context: Mapping[str, object] | None = None
    try:
        composition = compose_nia_outer_exam_save(
            outer,
            exam_save,
            lifecycle=lifecycle,
            prefer_exam_save_runtime=prefer_exam_save_runtime,
            master_dir=master_dir,
        )
        context = _validate_static_runtime_context(
            composition,
            exam_save,
            prefer_exam_save_runtime=prefer_exam_save_runtime,
            database=database,
            master_dir=master_dir,
        )
    except NiaOuterExamSaveAdapterError as error:
        code = (
            "nia-plan-unsupported"
            if str(error).startswith("unsupported idol plan:")
            else "nia-context-unavailable"
        )
        return _unavailable(
            code,
            f"{type(error).__name__}:{error}",
            source=source,
            beam_width=beam_width,
            include_drinks=include_drinks,
            context=context,
        )
    except (KeyError, TypeError, ValueError, OSError) as error:
        return _unavailable(
            "nia-context-mismatch",
            f"{type(error).__name__}:{error}",
            source=source,
            beam_width=beam_width,
            include_drinks=include_drinks,
            context=context,
        )

    gate = context.get("profile_execution_gate")
    if not isinstance(gate, Mapping) or not gate.get("whole_audition_executable"):
        blocker_codes = (
            []
            if not isinstance(gate, Mapping)
            else gate.get("blocker_codes", [])
        )
        return _unavailable(
            "nia-profile-execution-gate-closed",
            "future N.I.A. horizon is not executable: "
            + ",".join(str(value) for value in blocker_codes),
            source=source,
            beam_width=beam_width,
            include_drinks=include_drinks,
            context=context,
        )

    try:
        decoded_value = _decoded_mapping(exam_save) if decoded is None else decoded
        turn_start, accepted_play, extension_runtime = _nia_search_extensions(
            composition,
            decoded_value,
            exam_save,
            database=database,
            master_dir=master_dir,
        )
    except (TypeError, ValueError) as error:
        return _unavailable(
            "nia-exam-save-unavailable",
            f"{type(error).__name__}:{error}",
            source=source,
            beam_width=beam_width,
            include_drinks=include_drinks,
            context=context,
        )
    report = advise_plan3_audition_decoded(
        decoded_value,
        source=source,
        beam_width=beam_width,
        include_drinks=include_drinks,
        database=database,
        support_card_master=support_card_master,
        master_dir=master_dir,
        turn_start_extension=turn_start,
        accepted_play_extension=accepted_play,
        initial_turn_start_extension_state=extension_runtime,
        drink_timing_prior=drink_timing_prior,
        drink_timing_flow_id=drink_timing_flow_id,
        leaderboard_drink_prior=leaderboard_drink_prior,
    )
    current = dict(report.current_state or {})
    current["nia_context"] = dict(context)
    return replace(report, current_state=current, source=source)


def advise_nia_plan3_audition_replay(
    outer: ProduceOuterLocalSaveSnapshot | Mapping[str, object],
    decoded: DecodedPlan3LocalSave,
    replay: Plan3CompletedCardReplay,
    *,
    source: str = "",
    beam_width: int = 64,
    include_drinks: bool = True,
    prefer_exam_save_runtime: bool = False,
    database: Path = DEFAULT_DATABASE,
    support_card_master: Path = DEFAULT_SUPPORT_CARD_MASTER,
    master_dir: Path = DEFAULT_MASTER_DIR,
    drink_timing_prior: object | None = None,
    drink_timing_flow_id: str | None = None,
    leaderboard_drink_prior: object | None = None,
) -> Plan3AuditionAdvisorReport:
    """Continue one NIA command queue from its proven logical after-state."""

    if not isinstance(decoded, DecodedPlan3LocalSave):
        raise TypeError("decoded must be DecodedPlan3LocalSave")
    if not isinstance(replay, Plan3CompletedCardReplay):
        raise TypeError("replay must be Plan3CompletedCardReplay")
    raw = _raw_mapping(decoded)
    directory = Path(master_dir).resolve()
    try:
        composition = compose_nia_outer_exam_save(
            outer,
            raw,
            prefer_exam_save_runtime=prefer_exam_save_runtime,
            master_dir=directory,
        )
        context_payload = _validate_static_runtime_context(
            composition,
            raw,
            prefer_exam_save_runtime=prefer_exam_save_runtime,
            database=Path(database),
            master_dir=directory,
        )
        context = parse_plan3_audition_context(
            raw,
            turn_end_stamina_recovery=load_plan3_exam_settings(
                decoded.exam_state.setting_id,
                master_dir=directory,
            ).turn_end_stamina_recovery,
        )
        logical = bind_plan3_audition_state(replay.after, context)
        prepared = replay.prepared_before
        native = settle_completed_plan3_card_replay_native_state(replay)
        settings = load_plan3_exam_settings(
            decoded.exam_state.setting_id,
            master_dir=directory,
        )
        inventory = (
            load_plan3_drink_inventory(
                context.drink_ids,
                master_dir=directory,
                database=Path(database),
            )
            if include_drinks
            else None
        )
        search = search_plan3_native(
            logical,
            native,
            beam_width=beam_width,
            depth=None,
            settings=settings,
            objective=make_plan3_audition_objective(context),
            database=Path(database),
            support_upgrades=prepared.support_upgrades,
            support_card_searches=dict(prepared.support_card_searches),
            battle_parameter_schedule=tuple(
                frame.parameter_type for frame in context.remaining_frames
            ),
            battle_ranking_resolved=True,
            include_skip=True,
            force_end_score=context.force_end_score,
            force_end_stamina_recovery=(
                settings.turn_end_stamina_recovery
                if context.force_end_score
                else 0
            ),
            drink_inventory=inventory,
            turn_start_extension=prepared.turn_start_extension,
            accepted_play_extension=prepared.accepted_play_extension,
            initial_turn_start_extension_state=replay.extension_state_after,
        )
        result = Plan3AuditionCurrentActionResult(
            context=context,
            prepared=prepared,
            battle_state=logical,
            search=search,
            search_mode="full_horizon",
            drinks_included=include_drinks,
        )
        report = build_plan3_audition_advisor_report(
            result,
            source=source,
            beam_width=beam_width,
            include_drinks=include_drinks,
            drink_timing_prior=drink_timing_prior,
            drink_timing_flow_id=drink_timing_flow_id,
            leaderboard_drink_prior=leaderboard_drink_prior,
        )
    except (KeyError, OSError, TypeError, ValueError) as error:
        return _unavailable(
            "nia-replay-unavailable",
            f"{type(error).__name__}:{error}",
            source=source,
            beam_width=beam_width,
            include_drinks=include_drinks,
        )
    current = dict(report.current_state or {})
    current["nia_context"] = dict(context_payload)
    return replace(report, current_state=current, source=source)


def prepare_nia_plan3_audition_runtime(
    outer: ProduceOuterLocalSaveSnapshot | Mapping[str, object],
    decoded: DecodedPlan3LocalSave,
    *,
    prefer_exam_save_runtime: bool = False,
    database: Path = DEFAULT_DATABASE,
    support_card_master: Path = DEFAULT_SUPPORT_CARD_MASTER,
    master_dir: Path = DEFAULT_MASTER_DIR,
):
    """Return the exact current NIA advisor inputs for executor replay proof."""

    raw = _raw_mapping(decoded)
    directory = Path(master_dir).resolve()
    composition = compose_nia_outer_exam_save(
        outer,
        raw,
        prefer_exam_save_runtime=prefer_exam_save_runtime,
        master_dir=directory,
    )
    _validate_static_runtime_context(
        composition,
        raw,
        prefer_exam_save_runtime=prefer_exam_save_runtime,
        database=Path(database),
        master_dir=directory,
    )
    turn_start, accepted_play, runtime = _nia_search_extensions(
        composition,
        decoded,
        raw,
        database=Path(database),
        master_dir=directory,
    )
    prepared = search_plan3_native_decoded_local_save(
        decoded,
        raw_exam_save=raw,
        beam_width=1,
        depth=0,
        database=Path(database),
        support_card_master=Path(support_card_master),
        turn_start_extension=turn_start,
        accepted_play_extension=accepted_play,
        initial_turn_start_extension_state=runtime,
    )
    context = parse_plan3_audition_context(
        raw,
        turn_end_stamina_recovery=load_plan3_exam_settings(
            decoded.exam_state.setting_id,
            master_dir=directory,
        ).turn_end_stamina_recovery,
    )
    return composition, context, prepared


def advise_nia_plan3_audition_decoded(
    outer: ProduceOuterLocalSaveSnapshot | Mapping[str, object],
    decoded: DecodedPlan3LocalSave,
    *,
    source: str = "",
    beam_width: int = 64,
    include_drinks: bool = True,
    prefer_exam_save_runtime: bool = False,
    database: Path = DEFAULT_DATABASE,
    support_card_master: Path = DEFAULT_SUPPORT_CARD_MASTER,
    master_dir: Path = DEFAULT_MASTER_DIR,
    drink_timing_prior: object | None = None,
    drink_timing_flow_id: str | None = None,
    leaderboard_drink_prior: object | None = None,
) -> Plan3AuditionAdvisorReport:
    """Advise a typed ExamSave without converting it back through a file."""

    if not isinstance(decoded, DecodedPlan3LocalSave):
        raise TypeError("decoded must be DecodedPlan3LocalSave")
    return _advise_composed(
        outer,
        _raw_mapping(decoded),
        decoded=decoded,
        lifecycle=None,
        source=source,
        beam_width=beam_width,
        include_drinks=include_drinks,
        prefer_exam_save_runtime=prefer_exam_save_runtime,
        database=Path(database),
        support_card_master=Path(support_card_master),
        master_dir=Path(master_dir).resolve(),
        drink_timing_prior=drink_timing_prior,
        drink_timing_flow_id=drink_timing_flow_id,
        leaderboard_drink_prior=leaderboard_drink_prior,
    )


def search_nia_plan3_audition_current_action(
    outer: ProduceOuterLocalSaveSnapshot | Mapping[str, object],
    decoded: DecodedPlan3LocalSave,
    *,
    beam_width: int = 64,
    prefer_exam_save_runtime: bool = False,
    database: Path = DEFAULT_DATABASE,
    support_card_master: Path = DEFAULT_SUPPORT_CARD_MASTER,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> Plan3AuditionCurrentActionResult:
    """Build a one-card NIA search used only to prove a persisted queue."""

    composition, _context, prepared = prepare_nia_plan3_audition_runtime(
        outer,
        decoded,
        prefer_exam_save_runtime=prefer_exam_save_runtime,
        database=Path(database),
        support_card_master=Path(support_card_master),
        master_dir=Path(master_dir),
    )
    return search_plan3_audition_current_action_decoded(
        decoded,
        beam_width=beam_width,
        database=Path(database),
        support_card_master=Path(support_card_master),
        master_dir=Path(master_dir),
        turn_start_extension=prepared.turn_start_extension,
        accepted_play_extension=prepared.accepted_play_extension,
        initial_turn_start_extension_state=(
            prepared.initial_turn_start_extension_state
        ),
    )


def advise_nia_plan3_audition_mappings(
    outer_play_log: ProduceOuterLocalSaveSnapshot | Mapping[str, object],
    exam_save: Mapping[str, object],
    *,
    lifecycle: ProduceLifecycleState | Mapping[str, object] | None = None,
    source: str = "",
    beam_width: int = 64,
    include_drinks: bool = True,
    prefer_exam_save_runtime: bool = False,
    database: Path = DEFAULT_DATABASE,
    support_card_master: Path = DEFAULT_SUPPORT_CARD_MASTER,
    master_dir: Path = DEFAULT_MASTER_DIR,
    drink_timing_prior: object | None = None,
    drink_timing_flow_id: str | None = None,
    leaderboard_drink_prior: object | None = None,
) -> Plan3AuditionAdvisorReport:
    """Advise one decrypted N.I.A. snapshot without executing an action."""

    if not isinstance(exam_save, Mapping):
        raise TypeError("exam_save must be a mapping")
    return _advise_composed(
        outer_play_log,
        exam_save,
        decoded=None,
        lifecycle=lifecycle,
        source=source,
        beam_width=beam_width,
        include_drinks=include_drinks,
        prefer_exam_save_runtime=prefer_exam_save_runtime,
        database=Path(database),
        support_card_master=Path(support_card_master),
        master_dir=Path(master_dir).resolve(),
        drink_timing_prior=drink_timing_prior,
        drink_timing_flow_id=drink_timing_flow_id,
        leaderboard_drink_prior=leaderboard_drink_prior,
    )


def advise_nia_plan3_audition_files(
    outer_play_log_path: str | Path,
    exam_save_path: str | Path,
    *,
    lifecycle_path: str | Path | None = None,
    beam_width: int = 64,
    include_drinks: bool = True,
    prefer_exam_save_runtime: bool = False,
    database: Path = DEFAULT_DATABASE,
    support_card_master: Path = DEFAULT_SUPPORT_CARD_MASTER,
    master_dir: Path = DEFAULT_MASTER_DIR,
    drink_timing_prior: object | None = None,
    drink_timing_flow_id: str | None = None,
    leaderboard_drink_prior: object | None = None,
) -> Plan3AuditionAdvisorReport:
    """Read encrypted local-save files and return the same stable contract."""

    outer_path = Path(outer_play_log_path)
    exam_path = Path(exam_save_path)
    source = f"outer={outer_path.resolve()};exam={exam_path.resolve()}"
    try:
        outer = decode_produce_play_log_file(outer_path)
        lifecycle = (
            None
            if lifecycle_path is None
            else decode_produce_lifecycle_file(lifecycle_path)
        )
        if lifecycle is not None:
            outer = replace(outer, lifecycle=lifecycle)
        decoded = decode_plan3_local_save_file(exam_path)
        raw = _raw_mapping(decoded)
    except (OSError, TypeError, ValueError) as error:
        return _unavailable(
            "nia-source-unavailable",
            f"{type(error).__name__}:{error}",
            source=source,
            beam_width=beam_width,
            include_drinks=include_drinks,
        )
    return _advise_composed(
        outer,
        raw,
        decoded=decoded,
        lifecycle=None,
        source=source,
        beam_width=beam_width,
        include_drinks=include_drinks,
        prefer_exam_save_runtime=prefer_exam_save_runtime,
        database=Path(database),
        support_card_master=Path(support_card_master),
        master_dir=Path(master_dir).resolve(),
        drink_timing_prior=drink_timing_prior,
        drink_timing_flow_id=drink_timing_flow_id,
        leaderboard_drink_prior=leaderboard_drink_prior,
    )


def _json_mapping(path: Path, label: str) -> Mapping[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} JSON root must be an object")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read N.I.A. outer/ExamSaveData and emit fail-closed Plan 3 advice."
    )
    parser.add_argument("outer_play_log", type=Path)
    parser.add_argument("exam_save", type=Path)
    parser.add_argument("--lifecycle", type=Path)
    parser.add_argument("--decrypted-json", action="store_true")
    parser.add_argument("--beam-width", type=int, default=64)
    parser.add_argument("--no-drinks", action="store_true")
    parser.add_argument(
        "--drink-timing-observations",
        type=Path,
        help="optional state-conditioned drink observation JSONL (offline only)",
    )
    parser.add_argument(
        "--drink-timing-prior",
        dest="drink_timing_observations",
        type=Path,
        help="alias for --drink-timing-observations",
    )
    parser.add_argument("--drink-timing-flow-id")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)

    timing_prior = None
    timing_error: Exception | None = None
    if args.drink_timing_observations is not None:
        try:
            from .drink_behavior_observation import load_drink_timing_prior

            timing_prior = load_drink_timing_prior(
                args.drink_timing_observations
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            timing_error = error

    if timing_error is not None:
        report = _unavailable(
            "drink-timing-prior-unavailable",
            f"{type(timing_error).__name__}:{timing_error}",
            source=(
                f"outer={args.outer_play_log.resolve()};"
                f"exam={args.exam_save.resolve()}"
            ),
            beam_width=args.beam_width,
            include_drinks=not args.no_drinks,
        )
    elif args.decrypted_json:
        try:
            outer = _json_mapping(args.outer_play_log, "outer PlayLog")
            exam = _json_mapping(args.exam_save, "ExamSaveData")
            lifecycle = (
                None
                if args.lifecycle is None
                else _json_mapping(args.lifecycle, "ProduceLocalSaveData")
            )
        except (OSError, TypeError, ValueError) as error:
            report = _unavailable(
                "nia-source-unavailable",
                f"{type(error).__name__}:{error}",
                source=(
                    f"outer={args.outer_play_log.resolve()};"
                    f"exam={args.exam_save.resolve()}"
                ),
                beam_width=args.beam_width,
                include_drinks=not args.no_drinks,
            )
        else:
            report = advise_nia_plan3_audition_mappings(
                outer,
                exam,
                lifecycle=lifecycle,
                source=(
                    f"outer={args.outer_play_log.resolve()};"
                    f"exam={args.exam_save.resolve()}"
                ),
                beam_width=args.beam_width,
                include_drinks=not args.no_drinks,
                drink_timing_prior=timing_prior,
                drink_timing_flow_id=args.drink_timing_flow_id,
            )
    else:
        report = advise_nia_plan3_audition_files(
            args.outer_play_log,
            args.exam_save,
            lifecycle_path=args.lifecycle,
            beam_width=args.beam_width,
            include_drinks=not args.no_drinks,
            drink_timing_prior=timing_prior,
            drink_timing_flow_id=args.drink_timing_flow_id,
        )

    encoded = report.to_json(indent=None if args.compact else 2) + "\n"
    if args.output is None:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(encoded, end="")
    else:
        target = args.output.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(encoded, encoding="utf-8")
    return 0 if report.available else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "advise_nia_plan3_audition_files",
    "advise_nia_plan3_audition_mappings",
    "advise_nia_plan3_audition_decoded",
    "advise_nia_plan3_audition_replay",
    "prepare_nia_plan3_audition_runtime",
    "search_nia_plan3_audition_current_action",
    "main",
]
