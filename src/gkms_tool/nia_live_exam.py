"""NIA Plan3 composition with native authority by default, explicit legacy Maa."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .master_db import DEFAULT_DATABASE
from .nia_audition_advisor import (
    advise_nia_plan3_audition_decoded,
    advise_nia_plan3_audition_replay,
    search_nia_plan3_audition_current_action,
)
from .plan3_audition_search_bridge import (
    bind_plan3_audition_state,
    parse_plan3_audition_context,
)
from .plan3_card_history import (
    Plan3CompletedCardReplay,
    replay_completed_plan3_card_history_from_search,
    replay_next_completed_plan3_card_history,
    settle_completed_plan3_card_replay_native_state,
)
from .plan3_drink import load_plan3_drink_inventory
from .plan3_engine import load_plan3_exam_settings
from .plan3_exam_orchestrator import run_plan3_exam_orchestrator
from .plan3_audition_executor import MaaPlan3ActionDriver
from .plan3_local_save_bridge import DecodedPlan3LocalSave
from .plan3_native_search_bridge import DEFAULT_SUPPORT_CARD_MASTER
from .plan3_native_search import enumerate_plan3_native_legal_candidates
from .produce_outer_local_save import (
    DEFAULT_PC_GAME_ROOT,
    ProduceOuterLocalSaveSnapshot,
    read_current_produce_outer_local_save,
)
from .nia_static_adapter import DEFAULT_MASTER_DIR


def build_nia_plan3_executor_options(
    outer_reader,
    *,
    source: str,
    expected_produce_id: str | None = None,
    expected_idol_card_id: str | None = None,
    database: Path = DEFAULT_DATABASE,
    support_card_master: Path = DEFAULT_SUPPORT_CARD_MASTER,
    master_dir: Path = DEFAULT_MASTER_DIR,
    drink_timing_prior: object | None = None,
    drink_timing_flow_id: str | None = None,
    leaderboard_drink_prior: object | None = None,
) -> Mapping[str, object]:
    """Return executor seams which rebuild NIA authority on every read."""

    database = Path(database)
    support_card_master = Path(support_card_master)
    master_dir = Path(master_dir)

    def checked_report(report):
        current = getattr(report, "current_state", None)
        context = current.get("nia_context") if isinstance(current, Mapping) else None
        if not isinstance(context, Mapping):
            return report
        actual_produce_id = context.get("produce_id")
        actual_idol_card_id = context.get("idol_card_id")
        if (
            expected_produce_id is not None
            and actual_produce_id != expected_produce_id
        ):
            raise ValueError(
                "N.I.A. ExamSave produce_id differs from selected mode: "
                f"{actual_produce_id!r} != {expected_produce_id!r}"
            )
        if (
            expected_idol_card_id is not None
            and actual_idol_card_id != expected_idol_card_id
        ):
            raise ValueError(
                "N.I.A. ExamSave idol card differs from selected idol: "
                f"{actual_idol_card_id!r} != {expected_idol_card_id!r}"
            )
        return report

    def advisor(decoded: DecodedPlan3LocalSave):
        return checked_report(advise_nia_plan3_audition_decoded(
            outer_reader(),
            decoded,
            source=source,
            prefer_exam_save_runtime=True,
            database=database,
            support_card_master=support_card_master,
            master_dir=master_dir,
            drink_timing_prior=drink_timing_prior,
            drink_timing_flow_id=drink_timing_flow_id,
            leaderboard_drink_prior=leaderboard_drink_prior,
        ))

    def replay_advisor(
        decoded: DecodedPlan3LocalSave,
        replay: Plan3CompletedCardReplay,
    ):
        return checked_report(advise_nia_plan3_audition_replay(
            outer_reader(),
            decoded,
            replay,
            source=source,
            prefer_exam_save_runtime=True,
            database=database,
            support_card_master=support_card_master,
            master_dir=master_dir,
            drink_timing_prior=drink_timing_prior,
            drink_timing_flow_id=drink_timing_flow_id,
            leaderboard_drink_prior=leaderboard_drink_prior,
        ))

    def replay_resolver(before, after, plan, prior):
        guid = str(plan.action["card_guid"])
        if prior is not None:
            return replay_next_completed_plan3_card_history(
                prior,
                before,
                after,
                guid,
                database=database,
            )
        current = search_nia_plan3_audition_current_action(
            outer_reader(),
            before,
            prefer_exam_save_runtime=True,
            beam_width=max(1, len(before.exam_state.zones.hand)),
            database=database,
            support_card_master=support_card_master,
            master_dir=master_dir,
        )
        prepared = current.prepared
        if (
            current.battle_state is None
            or prepared.native_state is None
            or current.search is None
            or current.issues
        ):
            return None
        return replay_completed_plan3_card_history_from_search(
            before,
            after,
            guid,
            logical_before=current.battle_state,
            native_before=prepared.native_state,
            prepared_before=prepared,
            search=current.search,
            database=database,
        )

    return {
        "advisor_factory": advisor,
        "card_replay_resolver": replay_resolver,
        "replay_advisor_factory": replay_advisor,
        # NIA accepts the game's removal of the exact bound ExamSave after a
        # successful MAA card/skip dispatch as the terminal boundary.  Result
        # numbers remain owned by the following outer/result page.
        "accept_missing_local_save_as_terminal": True,
    }


def run_live_nia_plan3_exam(
    exam_save_path: str | Path,
    *,
    game_root: str | Path = DEFAULT_PC_GAME_ROOT,
    max_actions: int = 100,
    expected_produce_id: str | None = None,
    expected_idol_card_id: str | None = None,
    stop_requested=lambda: False,
    progress_callback=None,
    expected_run_id: str | None = None,
):
    path = Path(exam_save_path)
    root = Path(game_root)
    from .runtime_command_client import input_backend
    backend = input_backend()
    outer_reader = None if backend == "dll" else lambda: read_current_produce_outer_local_save(root)
    options = {} if backend == "dll" else build_nia_plan3_executor_options(
        outer_reader,
        source=str(path.resolve()),
        expected_produce_id=expected_produce_id,
        expected_idol_card_id=expected_idol_card_id,
    )
    # Both explicit transports retain the same formal RL -> BC policy owner.
    from .nia_plan3_native_candidates import (
        build_nia_plan3_native_candidate_provider,
    )
    from .plan3_learned_policy_runtime import (
        build_nia_plan3_learned_executor_options,
    )
    from .policy_bundle import PolicyBundle
    from .offline_rl_runtime_adapter import build_offline_rl_artifact_selector

    bundle = PolicyBundle.load()
    runtime = bundle.payload.get("runtime")
    exact_component = bundle.component("exact_exam_policy")
    if not (
        isinstance(runtime, Mapping)
        and runtime.get("mode") == "learned-policy"
        and runtime.get("default_enabled") is True
        and runtime.get("fallback") == "stop-on-no-learned-decision"
        and exact_component.get("shadow_ready") is True
        and exact_component.get("live_apply_allowed") is True
    ):
        raise ValueError("active policy bundle is not live RL-to-BC")
    try:
        offline_component = bundle.component("offline_rl_policy")
    except KeyError:
        offline_rl = None
    else:
        try:
            offline_rl = build_offline_rl_artifact_selector(
                offline_component,
                project_root=bundle.project_root,
            )
        except (FileNotFoundError, OSError, TypeError, ValueError):
            # Exact BC remains the only learned fallback.  No advisor or Maa
            # policy is allowed to take ownership when RL abstains.
            offline_rl = None

    if backend == "dll":
        return _run_native_nia_plan3(path, bundle=bundle, offline_rl=offline_rl,
            max_actions=max_actions, expected_produce_id=expected_produce_id,
            expected_idol_card_id=expected_idol_card_id, expected_run_id=expected_run_id,
            stop_requested=stop_requested, progress_callback=progress_callback)

    candidate_provider = build_nia_plan3_native_candidate_provider(
        outer_reader,
        expected_produce_id=expected_produce_id,
        expected_idol_card_id=expected_idol_card_id,
        prefer_exam_save_runtime=True,
    )

    def replay_candidate_provider(decoded, replay):
        raw = json.loads(decoded.envelope.plaintext.decode("utf-8"))
        if not isinstance(raw, Mapping):
            raise ValueError("decrypted Plan3 replay root is invalid")
        settings = load_plan3_exam_settings(decoded.exam_state.setting_id)
        context = parse_plan3_audition_context(
            raw,
            turn_end_stamina_recovery=settings.turn_end_stamina_recovery,
        )
        logical = bind_plan3_audition_state(replay.after, context)
        native = settle_completed_plan3_card_replay_native_state(replay)
        prepared = replay.prepared_before
        inventory = load_plan3_drink_inventory(context.drink_ids)
        force_end_score = (
            context.force_end_score if context.force_end_score > 0 else 0
        )
        enumeration = enumerate_plan3_native_legal_candidates(
            logical,
            native,
            settings=settings,
            support_upgrades=prepared.support_upgrades,
            support_card_searches=dict(prepared.support_card_searches),
            battle_parameter_schedule=tuple(
                frame.parameter_type for frame in context.remaining_frames
            ),
            battle_ranking_resolved=True,
            include_drinks=True,
            include_turn_end=True,
            force_end_score=force_end_score,
            force_end_stamina_recovery=(
                context.turn_end_stamina_recovery if force_end_score else 0
            ),
            drink_inventory=inventory,
            turn_start_extension=prepared.turn_start_extension,
            accepted_play_extension=prepared.accepted_play_extension,
            initial_turn_start_extension_state=replay.extension_state_after,
        )
        return enumeration, logical, native

    options = build_nia_plan3_learned_executor_options(
        options,
        current_candidate_provider=candidate_provider,
        replay_candidate_provider=replay_candidate_provider,
        bundle=bundle,
        offline_rl=offline_rl,
    )
    step_observer = None
    try:
        from .nia_plan3_contextual_observer import (
            build_nia_plan3_contextual_sidecar,
        )
        from .run_identity import load_active_run

        active_run = load_active_run()
        if (
            active_run is not None
            and (
                expected_produce_id is None
                or active_run.produce_id == expected_produce_id
            )
            and (
                expected_idol_card_id is None
                or active_run.idol_card_id == expected_idol_card_id
            )
        ):
            if expected_run_id is None:
                expected_run_id = active_run.run_id
            step_observer = build_nia_plan3_contextual_sidecar(
                outer_reader,
                source_id=active_run.run_id,
                expected_produce_id=expected_produce_id,
                expected_idol_card_id=expected_idol_card_id,
            )
    except (ImportError, OSError, TypeError, ValueError):
        step_observer = None
    from .runtime_command_client import input_backend
    action_driver = None if input_backend() == "dll" else MaaPlan3ActionDriver(ordered_hand_slot_authority=True)
    return run_plan3_exam_orchestrator(
        path,
        dry_run=False,
        max_actions=max_actions,
        executor_options=options,
        driver=action_driver,
        step_observer=step_observer,
        stop_requested=stop_requested,
        progress_callback=progress_callback,
        expected_run_id=expected_run_id,
    )


def _run_native_nia_plan3(path, *, bundle, offline_rl, max_actions, expected_produce_id,
                         expected_idol_card_id, expected_run_id, stop_requested, progress_callback):
    from .runtime_nia_plan3 import RuntimeNiaPlan3CandidateProvider, build_runtime_nia_learned_advisor
    from .nia_plan3_contextual_observer import build_nia_plan3_contextual_sidecar
    from .run_identity import load_active_run
    provider = RuntimeNiaPlan3CandidateProvider(expected_produce_id=expected_produce_id,
                                               expected_idol_card_id=expected_idol_card_id)
    options = {"advisor_factory": build_runtime_nia_learned_advisor(provider, bundle=bundle,
                                                                   offline_rl=offline_rl, source="dll-native-nia")}
    observer = None
    try:
        active_run = load_active_run()
        if (active_run is not None and (expected_produce_id is None or active_run.produce_id == expected_produce_id)
                and (expected_idol_card_id is None or active_run.idol_card_id == expected_idol_card_id)):
            expected_run_id = expected_run_id or active_run.run_id
            observer = build_nia_plan3_contextual_sidecar(None, source_id=active_run.run_id,
                expected_produce_id=expected_produce_id, expected_idol_card_id=expected_idol_card_id,
                native_candidate_provider=provider)
    except (ImportError, OSError, TypeError, ValueError):
        observer = None
    return run_plan3_exam_orchestrator(path, dry_run=False, max_actions=max_actions,
        executor_options=options, driver=None, step_observer=observer,
        stop_requested=stop_requested, progress_callback=progress_callback, expected_run_id=expected_run_id)


__all__ = [
    "build_nia_plan3_executor_options",
    "run_live_nia_plan3_exam",
]
