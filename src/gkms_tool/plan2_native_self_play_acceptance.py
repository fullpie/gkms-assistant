"""Pure offline in-memory acceptance loop for the Plan2 native runtime.

This harness loads one Master initial deck, compiles the native program
catalog, bootstraps a deterministic in-memory root, and repeatedly applies
bounded expectimax plus ``simulate_plan2_native_action`` until terminal.  It
does not fabricate LocalSave evidence and has no controller, screen, MAA,
GUI, live-process, clock, or agent boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Final

from .master_db import DEFAULT_DATABASE
from .plan2_native_expectimax import (
    Plan2NativeEvaluationWeights,
    Plan2NativeExpectimaxError,
    Plan2NativeExpectimaxLimits,
    Plan2NativeExpectimaxResult,
    plan_plan2_native_expectimax,
)
from .plan2_native_horizon import (
    Plan2MasterInitialDeck,
    Plan2NativeAction,
    Plan2NativeBlocker,
    Plan2NativeBootstrap,
    Plan2NativeHorizonState,
    Plan2NativeProgramCatalog,
    Plan2NativeTransition,
    bootstrap_plan2_native_master_deck,
    load_master_plan2_initial_deck,
    simulate_plan2_native_action,
)
from .plan2_native_program_catalog import (
    PLAN2_MASTER_VERSION_TARGET,
    Plan2NativeProgramCatalogCompilation,
    compile_plan2_native_program_catalog,
)


PLAN2_NATIVE_SELF_PLAY_ACCEPTANCE_SCHEMA_VERSION: Final = 1
DEFAULT_MASTER_DIR: Final = Path(__file__).resolve().parents[2] / "_research" / "gakumasu-diff"

STATUS_COMPLETED: Final = "completed"
STATUS_STOPPED: Final = "stopped"

STOP_TERMINAL: Final = "terminal"
STOP_MANIFEST_FAILED: Final = "manifest-load-failed"
STOP_CATALOG_FAILED: Final = "catalog-compile-failed"
STOP_CURRENT_PROGRAM_MISSING: Final = "current-deck-program-missing"
STOP_BOOTSTRAP_BLOCKED: Final = "bootstrap-blocked"
STOP_EXPECTIMAX_BLOCKED: Final = "expectimax-blocked"
STOP_TRANSITION_BLOCKED: Final = "transition-blocked"
STOP_MAX_ACTIONS: Final = "max-actions-reached"

ManifestLoader = Callable[[str, str, int, Path], Plan2MasterInitialDeck]
CatalogCompiler = Callable[[Path], Plan2NativeProgramCatalogCompilation]
Bootstrapper = Callable[
    [Plan2MasterInitialDeck, int, int, int, int, int, int, int],
    Plan2NativeBootstrap,
]
Planner = Callable[
    [
        Plan2NativeHorizonState,
        Plan2NativeProgramCatalog,
        Plan2NativeEvaluationWeights,
        Plan2NativeExpectimaxLimits,
    ],
    Plan2NativeExpectimaxResult,
]
Transitioner = Callable[
    [Plan2NativeHorizonState, Plan2NativeAction, Plan2NativeProgramCatalog],
    Plan2NativeTransition,
]


def _default_manifest_loader(
    idol_card_id: str,
    produce_id: str,
    idol_card_upgrade: int,
    master_dir: Path,
) -> Plan2MasterInitialDeck:
    return load_master_plan2_initial_deck(
        idol_card_id,
        produce_id=produce_id,
        idol_card_upgrade=idol_card_upgrade,
        master_dir=master_dir,
    )


def _default_catalog_compiler(path: Path) -> Plan2NativeProgramCatalogCompilation:
    return compile_plan2_native_program_catalog(database=path)


def _default_bootstrapper(
    manifest: Plan2MasterInitialDeck,
    random_state: int,
    limit_turn: int,
    stamina: int,
    max_stamina: int,
    draw_count: int,
    hand_limit: int,
    extra_turn: int,
) -> Plan2NativeBootstrap:
    return bootstrap_plan2_native_master_deck(
        manifest,
        random_state=random_state,
        limit_turn=limit_turn,
        stamina=stamina,
        max_stamina=max_stamina,
        draw_count=draw_count,
        hand_limit=hand_limit,
        extra_turn=extra_turn,
    )


def _default_planner(
    state: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
    weights: Plan2NativeEvaluationWeights,
    limits: Plan2NativeExpectimaxLimits,
) -> Plan2NativeExpectimaxResult:
    return plan_plan2_native_expectimax(
        state,
        catalog,
        weights=weights,
        limits=limits,
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeSelfPlayDependencies:
    manifest_loader: ManifestLoader = _default_manifest_loader
    catalog_compiler: CatalogCompiler = _default_catalog_compiler
    bootstrapper: Bootstrapper = _default_bootstrapper
    planner: Planner = _default_planner
    transitioner: Transitioner = simulate_plan2_native_action

    def __post_init__(self) -> None:
        for name in (
            "manifest_loader",
            "catalog_compiler",
            "bootstrapper",
            "planner",
            "transitioner",
        ):
            if not callable(getattr(self, name)):
                raise TypeError(f"{name} must be callable")


def _action_to_dict(action: Plan2NativeAction) -> dict[str, object]:
    return {
        "kind": action.kind,
        "card_guid": action.card_guid,
        "action_id": action.action_id,
    }


def _queued_action_to_dict(value: object) -> dict[str, object]:
    return {
        "kind": getattr(value, "kind", ""),
        "value": getattr(value, "value", 0),
        "card_guid": getattr(value, "card_guid", ""),
        "pay_cost": getattr(value, "pay_cost", False),
        "spend_play": getattr(value, "spend_play", False),
    }


def _state_to_dict(state: Plan2NativeHorizonState | None) -> dict[str, object] | None:
    if state is None:
        return None
    scalar = state.scalar
    active_status_uids = sorted(
        {
            *(value.status_uid for value in scalar.review_multiple_layers),
            *(value.status_uid for value in scalar.end_turn_listeners),
            *(value.status_uid for value in scalar.card_play_listeners),
            *(
                (scalar.stamina_consumption_add_status.status_uid,)
                if scalar.stamina_consumption_add_status is not None
                else ()
            ),
            *(value.status_uid for value in state.stamina_modifiers.down_fix_layers),
            *(
                (state.stamina_modifiers.down.status_uid,)
                if state.stamina_modifiers.down is not None
                else ()
            ),
            *(
                (state.stamina_modifiers.add.layer.status_uid,)
                if state.stamina_modifiers.add.layer is not None
                else ()
            ),
            *(value.status_uid for value in state.status_enchant.listeners),
            *(value.status_uid for value in state.review_dynamic.layers),
            *(value.uid for value in state.debuff_registry.statuses),
            *(value.status_uid for value in state.effect_chains.queue),
            *(value.native_uid for value in state.generated_runtime.statuses),
            *(
                (value.status_uid for value in state.encore_runtime.listeners)
                if state.encore_runtime is not None
                else ()
            ),
            *state.item_runtime.active_status_uids,
        }
    )
    return {
        "schema_version": state.schema_version,
        "source_kind": state.source_kind,
        "phase": state.phase,
        "terminal": state.terminal,
        "remaining_turns": state.remaining_turns,
        "limit_turn": state.limit_turn,
        "extra_turn": state.extra_turn,
        "draw_count": state.draw_count,
        "hand_limit": state.hand_limit,
        "plays_remaining": state.plays_remaining,
        "scalar": {
            "current_turn": scalar.current_turn,
            "review": scalar.review,
            "score": scalar.score,
            "review_count_add": scalar.review_count_add,
            "block": scalar.block,
            "card_play_aggressive": scalar.card_play_aggressive,
            "stamina": scalar.stamina,
            "max_stamina": scalar.max_stamina,
            "exam_card_play_count": scalar.exam_card_play_count,
            "turn_card_play_count": scalar.turn_card_play_count,
            "next_status_uid": scalar.next_status_uid,
        },
        "zones": state.zones.to_dict(),
        "runtime": {
            "active_status_uids": active_status_uids,
            "review_status_present": state.review_dynamic.review_status_present,
            "review_passing_turn_start": state.review_dynamic.review_passing_turn_start,
            "review_dynamic_layer_count": len(state.review_dynamic.layers),
            "stamina_modifier_count": (
                len(state.stamina_modifiers.down_fix_layers)
                + int(state.stamina_modifiers.down is not None)
                + int(state.stamina_modifiers.add.layer is not None)
            ),
            "status_enchant_listener_count": len(state.status_enchant.listeners),
            "debuff_status_count": len(state.debuff_registry.statuses),
            "effect_chain_count": len(state.effect_chains.queue),
            "generated_status_count": len(state.generated_runtime.statuses),
            "encore_listener_count": (
                0 if state.encore_runtime is None else len(state.encore_runtime.listeners)
            ),
            "item_listener_count": len(state.item_runtime.listeners),
        },
        "items": {
            "source_ids": [value.item_id for value in state.item_runtime.sources],
            "usage_counts": [list(value) for value in state.item_runtime.usage_counts],
            "active_status_uids": list(state.item_runtime.active_status_uids),
        },
        "total_effect_draw_card_count": state.total_effect_draw_card_count,
        "review_consumption_sum": state.review_consumption_sum,
        "block_consumption_sum_count": state.block_consumption_sum_count,
        "judge_parameter": state.judge_parameter,
        "clear_border": state.clear_border,
        "command_queue": [
            _queued_action_to_dict(value) for value in state.command_queue
        ],
        "opaque_status_queue": list(state.opaque_status_queue),
    }


@dataclass(frozen=True, slots=True)
class Plan2NativeSelfPlayCoverage:
    target_version_count: int
    universe_version_count: int
    compiled_version_count: int
    failed_version_count: int
    globally_complete: bool
    current_refs: tuple[tuple[str, int], ...]
    current_missing_refs: tuple[tuple[str, int], ...]

    @property
    def current_deck_ready(self) -> bool:
        return not self.current_missing_refs

    def to_dict(self) -> dict[str, object]:
        return {
            "target_version_count": self.target_version_count,
            "universe_version_count": self.universe_version_count,
            "compiled_version_count": self.compiled_version_count,
            "failed_version_count": self.failed_version_count,
            "globally_complete": self.globally_complete,
            "current_deck_ready": self.current_deck_ready,
            "current_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.current_refs
            ],
            "current_missing_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.current_missing_refs
            ],
        }


@dataclass(frozen=True, slots=True)
class Plan2NativeSelfPlayStep:
    schema_version: int
    step_index: int
    action: Plan2NativeAction
    predicted_value: float
    principal_path: tuple[str, ...]
    predicted_before: Plan2NativeHorizonState
    predicted_after: Plan2NativeHorizonState
    transition_trace: tuple[str, ...]
    search: Plan2NativeExpectimaxResult
    stop_reason_after_step: str | None

    def __post_init__(self) -> None:
        if self.schema_version != PLAN2_NATIVE_SELF_PLAY_ACCEPTANCE_SCHEMA_VERSION:
            raise ValueError("unsupported Plan2 self-play step schema")
        if type(self.step_index) is not int or self.step_index < 1:
            raise ValueError("step_index must be a positive integer")
        if not isinstance(self.action, Plan2NativeAction):
            raise TypeError("action must be Plan2NativeAction")

    def to_dict(self) -> dict[str, object]:
        before = _state_to_dict(self.predicted_before)
        after = _state_to_dict(self.predicted_after)
        return {
            "schema_version": self.schema_version,
            "step_index": self.step_index,
            "action": _action_to_dict(self.action),
            "predicted_value": self.predicted_value,
            "principal_path": list(self.principal_path),
            "predicted_before": before,
            "predicted_after": after,
            "actual_next": after,
            "prediction_matches_actual": True,
            "differences": [],
            "predicted_transition": {
                "supported": True,
                "before": before,
                "after": after,
                "trace": list(self.transition_trace),
                "blockers": [],
            },
            "search": self.search.to_dict(),
            "stop_reason_after_step": self.stop_reason_after_step,
        }


@dataclass(frozen=True, slots=True)
class Plan2NativeSelfPlayResult:
    schema_version: int
    status: str
    idol_card_id: str
    random_state: int
    limit_turn: int
    max_actions: int
    coverage: Plan2NativeSelfPlayCoverage | None
    initial_state: Plan2NativeHorizonState | None
    final_state: Plan2NativeHorizonState | None
    terminal_reached: bool
    stop_reason: str
    blockers: tuple[Plan2NativeBlocker, ...]
    steps: tuple[Plan2NativeSelfPlayStep, ...]

    @property
    def acceptance_passed(self) -> bool:
        return self.terminal_reached and not self.blockers and self.stop_reason == STOP_TERMINAL

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "acceptance_passed": self.acceptance_passed,
            "idol_card_id": self.idol_card_id,
            "random_state": self.random_state,
            "limit_turn": self.limit_turn,
            "max_actions": self.max_actions,
            "coverage": None if self.coverage is None else self.coverage.to_dict(),
            "initial_state": _state_to_dict(self.initial_state),
            "final_state": _state_to_dict(self.final_state),
            "terminal_reached": self.terminal_reached,
            "stop_reason": self.stop_reason,
            "blockers": [value.to_dict() for value in self.blockers],
            "steps": [value.to_dict() for value in self.steps],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, indent=indent, allow_nan=False
        )


def _failed_result(
    *,
    idol_card_id: str,
    random_state: int,
    limit_turn: int,
    max_actions: int,
    stop_reason: str,
    blockers: tuple[Plan2NativeBlocker, ...],
    coverage: Plan2NativeSelfPlayCoverage | None = None,
    initial_state: Plan2NativeHorizonState | None = None,
    final_state: Plan2NativeHorizonState | None = None,
    steps: tuple[Plan2NativeSelfPlayStep, ...] = (),
) -> Plan2NativeSelfPlayResult:
    return Plan2NativeSelfPlayResult(
        PLAN2_NATIVE_SELF_PLAY_ACCEPTANCE_SCHEMA_VERSION,
        STATUS_STOPPED,
        idol_card_id,
        random_state,
        limit_turn,
        max_actions,
        coverage,
        initial_state,
        final_state,
        False,
        stop_reason,
        blockers,
        steps,
    )


def run_plan2_native_self_play_acceptance(
    idol_card_id: str,
    *,
    random_state: int,
    limit_turn: int,
    stamina: int,
    max_stamina: int,
    draw_count: int = 3,
    hand_limit: int = 5,
    extra_turn: int = 0,
    produce_id: str = "produce-001",
    idol_card_upgrade: int = 0,
    max_actions: int = 100,
    limits: Plan2NativeExpectimaxLimits = Plan2NativeExpectimaxLimits(),
    weights: Plan2NativeEvaluationWeights = Plan2NativeEvaluationWeights(),
    database: str | Path = DEFAULT_DATABASE,
    master_dir: str | Path = DEFAULT_MASTER_DIR,
    dependencies: Plan2NativeSelfPlayDependencies = Plan2NativeSelfPlayDependencies(),
) -> Plan2NativeSelfPlayResult:
    """Run deterministic Master-backed Plan2 decisions until a hard stop."""

    if type(idol_card_id) is not str or not idol_card_id:
        raise ValueError("idol_card_id must be non-empty text")
    if type(max_actions) is not int or max_actions < 1:
        raise ValueError("max_actions must be a positive integer")
    if not isinstance(limits, Plan2NativeExpectimaxLimits):
        raise TypeError("limits must be Plan2NativeExpectimaxLimits")
    if not isinstance(weights, Plan2NativeEvaluationWeights):
        raise TypeError("weights must be Plan2NativeEvaluationWeights")
    if not isinstance(dependencies, Plan2NativeSelfPlayDependencies):
        raise TypeError("dependencies must be Plan2NativeSelfPlayDependencies")

    try:
        manifest = dependencies.manifest_loader(
            idol_card_id,
            produce_id,
            idol_card_upgrade,
            Path(master_dir),
        )
    except (OSError, KeyError, TypeError, ValueError) as error:
        return _failed_result(
            idol_card_id=idol_card_id,
            random_state=random_state,
            limit_turn=limit_turn,
            max_actions=max_actions,
            stop_reason=STOP_MANIFEST_FAILED,
            blockers=(
                Plan2NativeBlocker(
                    "self-play-manifest-load-failed",
                    f"{type(error).__name__}:{error}",
                ),
            ),
        )
    if not isinstance(manifest, Plan2MasterInitialDeck):
        return _failed_result(
            idol_card_id=idol_card_id,
            random_state=random_state,
            limit_turn=limit_turn,
            max_actions=max_actions,
            stop_reason=STOP_MANIFEST_FAILED,
            blockers=(Plan2NativeBlocker("self-play-manifest-loader-contract"),),
        )

    try:
        compilation = dependencies.catalog_compiler(Path(database))
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        return _failed_result(
            idol_card_id=idol_card_id,
            random_state=random_state,
            limit_turn=limit_turn,
            max_actions=max_actions,
            stop_reason=STOP_CATALOG_FAILED,
            blockers=(
                Plan2NativeBlocker(
                    "self-play-catalog-compile-failed",
                    f"{type(error).__name__}:{error}",
                ),
            ),
        )
    if not isinstance(compilation, Plan2NativeProgramCatalogCompilation):
        return _failed_result(
            idol_card_id=idol_card_id,
            random_state=random_state,
            limit_turn=limit_turn,
            max_actions=max_actions,
            stop_reason=STOP_CATALOG_FAILED,
            blockers=(Plan2NativeBlocker("self-play-catalog-compiler-contract"),),
        )

    current_refs = tuple(sorted(set(manifest.card_refs)))
    compiled_refs = set(compilation.compiled_refs)
    missing_refs = tuple(ref for ref in current_refs if ref not in compiled_refs)
    coverage = Plan2NativeSelfPlayCoverage(
        PLAN2_MASTER_VERSION_TARGET,
        compilation.universe_version_count,
        compilation.compiled_version_count,
        compilation.failed_version_count,
        compilation.fully_compiled,
        current_refs,
        missing_refs,
    )
    if missing_refs:
        return _failed_result(
            idol_card_id=idol_card_id,
            random_state=random_state,
            limit_turn=limit_turn,
            max_actions=max_actions,
            stop_reason=STOP_CURRENT_PROGRAM_MISSING,
            coverage=coverage,
            blockers=tuple(
                Plan2NativeBlocker(
                    "self-play-current-program-missing",
                    f"{card_id}@{upgrade}",
                )
                for card_id, upgrade in missing_refs
            ),
        )

    try:
        bootstrap = dependencies.bootstrapper(
            manifest,
            random_state,
            limit_turn,
            stamina,
            max_stamina,
            draw_count,
            hand_limit,
            extra_turn,
        )
    except (TypeError, ValueError) as error:
        return _failed_result(
            idol_card_id=idol_card_id,
            random_state=random_state,
            limit_turn=limit_turn,
            max_actions=max_actions,
            stop_reason=STOP_BOOTSTRAP_BLOCKED,
            coverage=coverage,
            blockers=(
                Plan2NativeBlocker(
                    "self-play-bootstrap-failed",
                    f"{type(error).__name__}:{error}",
                ),
            ),
        )
    if not isinstance(bootstrap, Plan2NativeBootstrap):
        return _failed_result(
            idol_card_id=idol_card_id,
            random_state=random_state,
            limit_turn=limit_turn,
            max_actions=max_actions,
            stop_reason=STOP_BOOTSTRAP_BLOCKED,
            coverage=coverage,
            blockers=(Plan2NativeBlocker("self-play-bootstrap-contract"),),
        )
    if not bootstrap.simulation_ready or bootstrap.state is None:
        blockers = bootstrap.blockers or (
            Plan2NativeBlocker("self-play-bootstrap-state-missing"),
        )
        return _failed_result(
            idol_card_id=idol_card_id,
            random_state=random_state,
            limit_turn=limit_turn,
            max_actions=max_actions,
            stop_reason=STOP_BOOTSTRAP_BLOCKED,
            coverage=coverage,
            blockers=blockers,
        )

    initial = bootstrap.state
    state = initial
    steps: list[Plan2NativeSelfPlayStep] = []
    if state.terminal:
        return Plan2NativeSelfPlayResult(
            PLAN2_NATIVE_SELF_PLAY_ACCEPTANCE_SCHEMA_VERSION,
            STATUS_COMPLETED,
            idol_card_id,
            random_state,
            limit_turn,
            max_actions,
            coverage,
            initial,
            state,
            True,
            STOP_TERMINAL,
            (),
            (),
        )

    for step_index in range(1, max_actions + 1):
        try:
            search = dependencies.planner(
                state,
                compilation.catalog,
                weights,
                limits,
            )
        except Plan2NativeExpectimaxError as error:
            return _failed_result(
                idol_card_id=idol_card_id,
                random_state=random_state,
                limit_turn=limit_turn,
                max_actions=max_actions,
                stop_reason=STOP_EXPECTIMAX_BLOCKED,
                coverage=coverage,
                initial_state=initial,
                final_state=state,
                blockers=(error.blocker,),
                steps=tuple(steps),
            )
        if not isinstance(search, Plan2NativeExpectimaxResult):
            return _failed_result(
                idol_card_id=idol_card_id,
                random_state=random_state,
                limit_turn=limit_turn,
                max_actions=max_actions,
                stop_reason=STOP_EXPECTIMAX_BLOCKED,
                coverage=coverage,
                initial_state=initial,
                final_state=state,
                blockers=(Plan2NativeBlocker("self-play-expectimax-contract"),),
                steps=tuple(steps),
            )
        if search.best_action is None:
            blockers = search.blockers or (
                Plan2NativeBlocker("self-play-expectimax-no-action"),
            )
            return _failed_result(
                idol_card_id=idol_card_id,
                random_state=random_state,
                limit_turn=limit_turn,
                max_actions=max_actions,
                stop_reason=STOP_EXPECTIMAX_BLOCKED,
                coverage=coverage,
                initial_state=initial,
                final_state=state,
                blockers=blockers,
                steps=tuple(steps),
            )

        transition = dependencies.transitioner(
            state,
            search.best_action,
            compilation.catalog,
        )
        if not isinstance(transition, Plan2NativeTransition):
            blockers = (Plan2NativeBlocker("self-play-transition-contract"),)
            return _failed_result(
                idol_card_id=idol_card_id,
                random_state=random_state,
                limit_turn=limit_turn,
                max_actions=max_actions,
                stop_reason=STOP_TRANSITION_BLOCKED,
                coverage=coverage,
                initial_state=initial,
                final_state=state,
                blockers=blockers,
                steps=tuple(steps),
            )
        if not transition.supported or transition.after is None:
            blockers = transition.blockers or (
                Plan2NativeBlocker("self-play-transition-unsupported"),
            )
            return _failed_result(
                idol_card_id=idol_card_id,
                random_state=random_state,
                limit_turn=limit_turn,
                max_actions=max_actions,
                stop_reason=STOP_TRANSITION_BLOCKED,
                coverage=coverage,
                initial_state=initial,
                final_state=state,
                blockers=blockers,
                steps=tuple(steps),
            )

        after = transition.after
        terminal = after.terminal
        step_stop_reason = STOP_TERMINAL if terminal else None
        steps.append(
            Plan2NativeSelfPlayStep(
                PLAN2_NATIVE_SELF_PLAY_ACCEPTANCE_SCHEMA_VERSION,
                step_index,
                search.best_action,
                search.value,
                search.action_path,
                state,
                after,
                transition.trace,
                search,
                step_stop_reason,
            )
        )
        state = after
        if terminal:
            return Plan2NativeSelfPlayResult(
                PLAN2_NATIVE_SELF_PLAY_ACCEPTANCE_SCHEMA_VERSION,
                STATUS_COMPLETED,
                idol_card_id,
                random_state,
                limit_turn,
                max_actions,
                coverage,
                initial,
                state,
                True,
                STOP_TERMINAL,
                (),
                tuple(steps),
            )

    blocker = Plan2NativeBlocker("self-play-max-actions-reached", str(max_actions))
    return _failed_result(
        idol_card_id=idol_card_id,
        random_state=random_state,
        limit_turn=limit_turn,
        max_actions=max_actions,
        stop_reason=STOP_MAX_ACTIONS,
        coverage=coverage,
        initial_state=initial,
        final_state=state,
        blockers=(blocker,),
        steps=tuple(steps),
    )


__all__ = [
    "PLAN2_NATIVE_SELF_PLAY_ACCEPTANCE_SCHEMA_VERSION",
    "STATUS_COMPLETED",
    "STATUS_STOPPED",
    "STOP_BOOTSTRAP_BLOCKED",
    "STOP_CATALOG_FAILED",
    "STOP_CURRENT_PROGRAM_MISSING",
    "STOP_EXPECTIMAX_BLOCKED",
    "STOP_MANIFEST_FAILED",
    "STOP_MAX_ACTIONS",
    "STOP_TERMINAL",
    "STOP_TRANSITION_BLOCKED",
    "Plan2NativeSelfPlayCoverage",
    "Plan2NativeSelfPlayDependencies",
    "Plan2NativeSelfPlayResult",
    "Plan2NativeSelfPlayStep",
    "run_plan2_native_self_play_acceptance",
]
