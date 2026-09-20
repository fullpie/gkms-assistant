"""Offline Plan2 ExamSaveData decision orchestration.

This module composes three existing pure boundaries: current-Master program
catalog compilation, exact LocalSave bootstrap, and bounded native
expectimax.  Global catalog incompleteness is reported but does not prevent a
decision when every card version in the current save has a compiled program.
No screen, controller, agent, live process, or runtime proxy is used.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import math
from pathlib import Path
import sqlite3
from typing import Final

from .audition_local_save_state import (
    AuditionLocalSaveStateEvidence,
)
from .master_db import DEFAULT_DATABASE
from .plan2_native_expectimax import (
    Plan2NativeEvaluationWeights,
    Plan2NativeExpectimaxError,
    Plan2NativeExpectimaxLimits,
    Plan2NativeExpectimaxResult,
    plan_plan2_native_expectimax,
)
from .plan2_native_horizon import (
    Plan2NativeAction,
    Plan2NativeBlocker,
    Plan2NativeDrinkAction,
    Plan2NativeHorizonState,
    Plan2NativeOfflineAction,
    Plan2NativeProgramCatalog,
    Plan2NativeTransition,
    simulate_plan2_native_action_lifecycle,
)
from .plan2_native_local_save_bootstrap import (
    Plan2NativeExamSettingAuthorityError,
    Plan2NativeLocalSaveBootstrapAudit,
    bootstrap_plan2_native_horizon_from_evidence,
    load_plan2_native_exam_setting_authority,
)
from .plan2_native_program_catalog import (
    PLAN2_MASTER_VERSION_TARGET,
    Plan2NativeProgramCatalogCompilation,
    compile_plan2_native_program_catalog,
)
from .plan2_symbolic_generated_inputs import (
    is_plan2_symbolic_guid,
    prepare_plan2_symbolic_generated_inputs,
)


PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION: Final = 1

EvidenceLoader = Callable[[Path], AuditionLocalSaveStateEvidence | None]
CatalogCompiler = Callable[[Path], Plan2NativeProgramCatalogCompilation]
Bootstrapper = Callable[
    [
        AuditionLocalSaveStateEvidence,
        Plan2NativeProgramCatalog,
        int | None,
        int | None,
    ],
    Plan2NativeLocalSaveBootstrapAudit,
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
    [Plan2NativeHorizonState, Plan2NativeOfflineAction, Plan2NativeProgramCatalog],
    Plan2NativeTransition,
]


def _default_evidence_loader(path: Path) -> AuditionLocalSaveStateEvidence:
    # Preserve the checked-in/self-contained JSON evidence path while making
    # the production default understand the encrypted game ExamSave file.
    if path.read_bytes().lstrip().startswith(b"{"):
        from .audition_local_save_state import load_audition_local_save_state

        evidence = load_audition_local_save_state(path)
        if evidence is None:
            raise FileNotFoundError(path)
        return evidence

    # Import lazily to keep the pure orchestrator free of the outer loop's
    # optional UI dependencies until a raw production ExamSave path is used.
    from .initial_regular_autopilot import (
        load_initial_regular_plan2_exam_evidence,
    )

    return load_initial_regular_plan2_exam_evidence(path)


def _default_catalog_compiler(path: Path) -> Plan2NativeProgramCatalogCompilation:
    return compile_plan2_native_program_catalog(database=path)


def _default_bootstrapper(
    evidence: AuditionLocalSaveStateEvidence,
    catalog: Plan2NativeProgramCatalog,
    draw_count: int | None,
    hand_limit: int | None,
) -> Plan2NativeLocalSaveBootstrapAudit:
    if draw_count is None or hand_limit is None:
        authority = load_plan2_native_exam_setting_authority(
            evidence.state.setting_id
        )
        if draw_count is None:
            draw_count = authority.draw_count
        if hand_limit is None:
            hand_limit = authority.hand_limit
    gimmick_hooks = None
    opaque = evidence.state.root_runtime.opaque_fields.to_value()
    if isinstance(opaque, dict):
        if evidence.state.exam_type == 0:
            from .initial_regular_plan2_lesson_gimmick_runtime import (
                build_initial_regular_plan2_lesson_gimmick_hooks,
            )

            gimmick_hooks = build_initial_regular_plan2_lesson_gimmick_hooks(
                opaque.get("gimmickList"),
            )
        elif evidence.state.exam_type == 1:
            from .initial_regular_plan2_gimmick_runtime import (
                build_plan2_audition_gimmick_hooks,
            )

            # Native schedule IDs and Master shape decide support. Mid1 and
            # Mid2 use the same StartTurn lifecycle as Final; dropping their
            # hooks used to omit future Review gains from one-step estimates.
            gimmick_hooks = build_plan2_audition_gimmick_hooks(
                opaque.get("gimmickList"),
            )
    return bootstrap_plan2_native_horizon_from_evidence(
        evidence,
        catalog=catalog,
        draw_count=draw_count,
        hand_limit=hand_limit,
        gimmick_hooks=gimmick_hooks,
        # Production live play is observation-driven: the game owns passive
        # listener execution and the next ExamSave owns the resulting scalar
        # state.  Unknown passive listeners must not prevent selecting a card
        # from the exact current Hand.
        observe_external_effects=True,
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
class Plan2NativeExamSaveDependencies:
    """Injectable pure dependencies for focused orchestration tests."""

    evidence_loader: EvidenceLoader = _default_evidence_loader
    catalog_compiler: CatalogCompiler = _default_catalog_compiler
    bootstrapper: Bootstrapper = _default_bootstrapper
    planner: Planner = _default_planner
    transitioner: Transitioner = simulate_plan2_native_action_lifecycle

    def __post_init__(self) -> None:
        for name in (
            "evidence_loader",
            "catalog_compiler",
            "bootstrapper",
            "planner",
            "transitioner",
        ):
            if not callable(getattr(self, name)):
                raise TypeError(f"{name} must be callable")


@dataclass(frozen=True, slots=True)
class Plan2NativeCurrentCardProgram:
    guid: str
    card_id: str
    effective_upgrade: int
    available: bool

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.effective_upgrade

    def to_dict(self) -> dict[str, object]:
        return {
            "guid": self.guid,
            "card_id": self.card_id,
            "effective_upgrade": self.effective_upgrade,
            "available": self.available,
        }


@dataclass(frozen=True, slots=True)
class Plan2NativeCatalogCoverageReport:
    target_version_count: int
    universe_version_count: int
    compiled_version_count: int
    failed_version_count: int
    globally_complete: bool
    global_blocker_code_counts: tuple[tuple[str, int], ...]
    current_cards: tuple[Plan2NativeCurrentCardProgram, ...]

    def __post_init__(self) -> None:
        for name in (
            "target_version_count",
            "universe_version_count",
            "compiled_version_count",
            "failed_version_count",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if type(self.globally_complete) is not bool:
            raise TypeError("globally_complete must be bool")
        if any(
            not isinstance(value, Plan2NativeCurrentCardProgram)
            for value in self.current_cards
        ):
            raise TypeError("current_cards must contain typed card status")

    @property
    def current_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted({value.ref for value in self.current_cards}))

    @property
    def current_missing_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            sorted({value.ref for value in self.current_cards if not value.available})
        )

    @property
    def current_cards_ready(self) -> bool:
        return not self.current_missing_refs

    def to_dict(self) -> dict[str, object]:
        return {
            "target_version_count": self.target_version_count,
            "universe_version_count": self.universe_version_count,
            "compiled_version_count": self.compiled_version_count,
            "failed_version_count": self.failed_version_count,
            "globally_complete": self.globally_complete,
            "global_blocker_code_counts": dict(self.global_blocker_code_counts),
            "current_cards_ready": self.current_cards_ready,
            "current_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.current_refs
            ],
            "current_missing_refs": [
                {"card_id": card_id, "upgrade": upgrade}
                for card_id, upgrade in self.current_missing_refs
            ],
            "current_cards": [value.to_dict() for value in self.current_cards],
        }


def _queued_action_to_dict(value: object) -> dict[str, object]:
    return {
        "kind": getattr(value, "kind", ""),
        "value": getattr(value, "value", 0),
        "card_guid": getattr(value, "card_guid", ""),
        "pay_cost": getattr(value, "pay_cost", False),
        "spend_play": getattr(value, "spend_play", False),
    }


def _horizon_state_to_dict(state: Plan2NativeHorizonState) -> dict[str, object]:
    """Return a JSON-compatible decision snapshot while retaining ``state`` itself."""

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
            "review_passing_turn_start": (
                state.review_dynamic.review_passing_turn_start
            ),
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
                0
                if state.encore_runtime is None
                else len(state.encore_runtime.listeners)
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
class Plan2NativeExamSaveDecision:
    schema_version: int
    source_kind: str
    source_path: str
    coverage: Plan2NativeCatalogCoverageReport | None
    bootstrap: Plan2NativeLocalSaveBootstrapAudit | None
    search: Plan2NativeExpectimaxResult | None
    predicted_after_state: Plan2NativeHorizonState | None
    blockers: tuple[Plan2NativeBlocker, ...]
    logical_root: Plan2NativeHorizonState | None = None
    # ``None`` keeps the historical native expectimax choice.  A non-empty
    # source makes ``policy_action`` authoritative, including an explicit
    # no-decision (action=None) when every learned policy abstains.
    policy_action: Plan2NativeOfflineAction | None = None
    policy_source: str | None = None
    policy_probability: float | None = None
    policy_legal_action_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION:
            raise ValueError("unsupported Plan2 ExamSave decision schema")
        if self.source_kind not in {"evidence", "path", "horizon"}:
            raise ValueError("unsupported source_kind")
        if not isinstance(self.source_path, str):
            raise TypeError("source_path must be text")
        if self.coverage is not None and not isinstance(
            self.coverage, Plan2NativeCatalogCoverageReport
        ):
            raise TypeError("coverage must be typed or None")
        if self.bootstrap is not None and not isinstance(
            self.bootstrap, Plan2NativeLocalSaveBootstrapAudit
        ):
            raise TypeError("bootstrap must be typed or None")
        if self.search is not None and not isinstance(
            self.search, Plan2NativeExpectimaxResult
        ):
            raise TypeError("search must be typed or None")
        if self.predicted_after_state is not None and not isinstance(
            self.predicted_after_state, Plan2NativeHorizonState
        ):
            raise TypeError("predicted_after_state must be typed or None")
        if self.logical_root is not None and not isinstance(
            self.logical_root, Plan2NativeHorizonState
        ):
            raise TypeError("logical_root must be typed or None")
        if self.policy_action is not None and not isinstance(
            self.policy_action, (Plan2NativeAction, Plan2NativeDrinkAction)
        ):
            raise TypeError("policy_action must be a typed offline action or None")
        if self.policy_source is not None and (
            not isinstance(self.policy_source, str) or not self.policy_source
        ):
            raise ValueError("policy_source must be non-empty text or None")
        if self.policy_source is None and self.policy_action is not None:
            raise ValueError("policy_action requires an explicit policy_source")
        if self.policy_probability is not None:
            if self.policy_source is None or self.policy_action is None:
                raise ValueError(
                    "policy_probability requires an explicit policy action"
                )
            if (
                isinstance(self.policy_probability, bool)
                or not isinstance(self.policy_probability, (int, float))
                or not math.isfinite(float(self.policy_probability))
                or not 0.0 <= float(self.policy_probability) <= 1.0
            ):
                raise ValueError("policy_probability must be within 0..1")
        legal_action_ids = tuple(self.policy_legal_action_ids)
        if any(not isinstance(value, str) or not value for value in legal_action_ids):
            raise ValueError("policy legal action IDs must be non-empty text")
        if len(legal_action_ids) != len(set(legal_action_ids)):
            raise ValueError("policy legal action IDs must be unique")
        if self.policy_source is None and legal_action_ids:
            raise ValueError("policy legal actions require an explicit policy source")
        if (
            self.policy_action is not None
            and legal_action_ids
            and self.policy_action.action_id not in legal_action_ids
        ):
            raise ValueError("policy action is absent from the recorded legal set")
        object.__setattr__(self, "policy_legal_action_ids", legal_action_ids)
        if self.source_kind == "horizon" and self.logical_root is None:
            raise ValueError("horizon decision requires logical_root")
        if any(not isinstance(value, Plan2NativeBlocker) for value in self.blockers):
            raise TypeError("blockers must contain Plan2NativeBlocker")

    @property
    def best_action(self) -> Plan2NativeOfflineAction | None:
        if self.policy_source is not None:
            return self.policy_action
        return None if self.search is None else self.search.best_action

    @property
    def principal_path(self) -> tuple[str, ...]:
        if self.policy_source is not None:
            return () if self.policy_action is None else (self.policy_action.action_id,)
        return () if self.search is None else self.search.action_path

    @property
    def predicted_value(self) -> float | None:
        if self.policy_source is not None:
            return None
        return None if self.search is None else self.search.value

    @property
    def root_state(self) -> Plan2NativeHorizonState | None:
        if self.logical_root is not None:
            return self.logical_root
        return None if self.bootstrap is None else self.bootstrap.state

    @property
    def terminal(self) -> bool:
        return bool(self.root_state is not None and self.root_state.terminal)

    @property
    def decision_ready(self) -> bool:
        if self.search is None and self.policy_source is None:
            return False
        if self.source_kind != "horizon" and (
            self.coverage is None or not self.coverage.current_cards_ready
        ):
            return False
        if self.terminal:
            if self.policy_source is not None:
                return (
                    self.policy_action is None
                    and self.predicted_after_state == self.root_state
                )
            return (
                self.search.best_action is None
                and self.search.terminal_reached
                and self.predicted_after_state == self.search.root
            )
        if self.policy_source is not None:
            return (
                self.policy_action is not None
                and self.predicted_after_state is not None
            )
        return (
            self.search.best_action is not None
            and not self.search.fail_closed
            and self.predicted_after_state is not None
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_kind": self.source_kind,
            "source_path": self.source_path,
            "decision_ready": self.decision_ready,
            "terminal": self.terminal,
            "best_action": (
                None if self.best_action is None else self.best_action.action_id
            ),
            "policy_source": self.policy_source,
            "policy_probability": self.policy_probability,
            "legal_actions": list(self.policy_legal_action_ids),
            "planner_best_action": (
                None
                if self.search is None or self.search.best_action is None
                else self.search.best_action.action_id
            ),
            "principal_path": list(self.principal_path),
            "predicted_value": self.predicted_value,
            "predicted_after_state": (
                None
                if self.predicted_after_state is None
                else _horizon_state_to_dict(self.predicted_after_state)
            ),
            "logical_root": (
                None
                if self.logical_root is None
                else _horizon_state_to_dict(self.logical_root)
            ),
            "coverage": None if self.coverage is None else self.coverage.to_dict(),
            "bootstrap": (
                None if self.bootstrap is None else self.bootstrap.to_dict()
            ),
            "search": None if self.search is None else self.search.to_dict(),
            "blockers": [value.to_dict() for value in self.blockers],
        }


def with_plan2_policy_action(
    decision: Plan2NativeExamSaveDecision,
    *,
    source: str,
    action: Plan2NativeOfflineAction | None,
    predicted_after_state: Plan2NativeHorizonState | None,
    probability: float | None = None,
    legal_action_ids: tuple[str, ...] = (),
    blockers: tuple[Plan2NativeBlocker, ...] = (),
) -> Plan2NativeExamSaveDecision:
    """Apply an explicit learned-policy choice without forging a search path."""

    if not isinstance(decision, Plan2NativeExamSaveDecision):
        raise TypeError("decision must be Plan2NativeExamSaveDecision")
    if not isinstance(source, str) or not source:
        raise ValueError("policy source must be non-empty text")
    if any(not isinstance(value, Plan2NativeBlocker) for value in blockers):
        raise TypeError("policy blockers must contain Plan2NativeBlocker")
    return replace(
        decision,
        policy_action=action,
        policy_source=source,
        policy_probability=probability,
        policy_legal_action_ids=legal_action_ids,
        predicted_after_state=predicted_after_state,
        blockers=tuple(dict.fromkeys((*decision.blockers, *blockers))),
    )


def _current_cards(
    evidence: AuditionLocalSaveStateEvidence,
    compilation: Plan2NativeProgramCatalogCompilation,
) -> tuple[Plan2NativeCurrentCardProgram, ...]:
    compiled = set(compilation.compiled_refs)
    return tuple(
        Plan2NativeCurrentCardProgram(
            card.guid,
            card.card_id,
            card.effective_upgrade,
            (card.card_id, card.effective_upgrade) in compiled,
        )
        for card in evidence.state.all_card_instances
    )


def _coverage_report(
    evidence: AuditionLocalSaveStateEvidence,
    compilation: Plan2NativeProgramCatalogCompilation,
) -> Plan2NativeCatalogCoverageReport:
    return Plan2NativeCatalogCoverageReport(
        target_version_count=PLAN2_MASTER_VERSION_TARGET,
        universe_version_count=compilation.universe_version_count,
        compiled_version_count=compilation.compiled_version_count,
        failed_version_count=compilation.failed_version_count,
        globally_complete=compilation.fully_compiled,
        global_blocker_code_counts=tuple(
            compilation.blocker_code_counts.items()
        ),
        current_cards=_current_cards(evidence, compilation),
    )


def _load_source(
    source: str | Path | AuditionLocalSaveStateEvidence,
    dependencies: Plan2NativeExamSaveDependencies,
) -> tuple[AuditionLocalSaveStateEvidence | None, str, str, Plan2NativeBlocker | None]:
    if isinstance(source, AuditionLocalSaveStateEvidence):
        return source, "evidence", source.source_path, None
    if not isinstance(source, (str, Path)):
        raise TypeError("source must be a LocalSave evidence path or typed evidence")
    path = Path(source)
    try:
        evidence = dependencies.evidence_loader(path)
    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
        return (
            None,
            "path",
            str(path),
            Plan2NativeBlocker(
                "exam-save-evidence-load-failed",
                f"{type(error).__name__}:{error}",
            ),
        )
    if evidence is None:
        return (
            None,
            "path",
            str(path),
            Plan2NativeBlocker("exam-save-evidence-not-found", str(path)),
        )
    if not isinstance(evidence, AuditionLocalSaveStateEvidence):
        return (
            None,
            "path",
            str(path),
            Plan2NativeBlocker("exam-save-evidence-loader-contract"),
        )
    return evidence, "path", str(path), None


def decide_plan2_native_exam_save(
    source: str | Path | AuditionLocalSaveStateEvidence,
    *,
    draw_count: int | None = None,
    hand_limit: int | None = None,
    limits: Plan2NativeExpectimaxLimits = Plan2NativeExpectimaxLimits(),
    weights: Plan2NativeEvaluationWeights = Plan2NativeEvaluationWeights(),
    database: str | Path = DEFAULT_DATABASE,
    dependencies: Plan2NativeExamSaveDependencies = Plan2NativeExamSaveDependencies(),
) -> Plan2NativeExamSaveDecision:
    """Load, compile, bootstrap, gate current GUIDs, search, and predict once."""

    if not isinstance(limits, Plan2NativeExpectimaxLimits):
        raise TypeError("limits must be Plan2NativeExpectimaxLimits")
    if not isinstance(weights, Plan2NativeEvaluationWeights):
        raise TypeError("weights must be Plan2NativeEvaluationWeights")
    if not isinstance(dependencies, Plan2NativeExamSaveDependencies):
        raise TypeError("dependencies must be Plan2NativeExamSaveDependencies")
    evidence, source_kind, source_path, load_blocker = _load_source(
        source, dependencies
    )
    if load_blocker is not None or evidence is None:
        return Plan2NativeExamSaveDecision(
            PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION,
            source_kind,
            source_path,
            None,
            None,
            None,
            None,
            (load_blocker,) if load_blocker is not None else (),
        )

    try:
        compilation = dependencies.catalog_compiler(Path(database))
    except (OSError, TypeError, ValueError) as error:
        return Plan2NativeExamSaveDecision(
            PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION,
            source_kind,
            source_path,
            None,
            None,
            None,
            None,
            (
                Plan2NativeBlocker(
                    "plan2-program-catalog-compile-failed",
                    f"{type(error).__name__}:{error}",
                ),
            ),
        )
    if not isinstance(compilation, Plan2NativeProgramCatalogCompilation):
        return Plan2NativeExamSaveDecision(
            PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION,
            source_kind,
            source_path,
            None,
            None,
            None,
            None,
            (Plan2NativeBlocker("plan2-program-catalog-compiler-contract"),),
        )

    coverage = _coverage_report(evidence, compilation)
    current_blockers = tuple(
        Plan2NativeBlocker(
            "exam-save-current-card-program-missing",
            f"{value.guid}:{value.card_id}@{value.effective_upgrade}",
        )
        for value in coverage.current_cards
        if not value.available
    )
    try:
        bootstrap = dependencies.bootstrapper(
            evidence,
            compilation.catalog,
            draw_count,
            hand_limit,
        )
    except Plan2NativeExamSettingAuthorityError as error:
        blockers = (
            *current_blockers,
            Plan2NativeBlocker(error.code, error.detail),
        )
        return Plan2NativeExamSaveDecision(
            PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION,
            source_kind,
            source_path,
            coverage,
            None,
            None,
            None,
            tuple(dict.fromkeys(blockers)),
        )
    except (TypeError, ValueError) as error:
        blockers = (
            *current_blockers,
            Plan2NativeBlocker(
                "exam-save-bootstrap-failed",
                f"{type(error).__name__}:{error}",
            ),
        )
        return Plan2NativeExamSaveDecision(
            PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION,
            source_kind,
            source_path,
            coverage,
            None,
            None,
            None,
            tuple(dict.fromkeys(blockers)),
        )
    if not isinstance(bootstrap, Plan2NativeLocalSaveBootstrapAudit):
        return Plan2NativeExamSaveDecision(
            PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION,
            source_kind,
            source_path,
            coverage,
            None,
            None,
            None,
            (
                *current_blockers,
                Plan2NativeBlocker("exam-save-bootstrap-contract"),
            ),
        )
    blockers = tuple(
        dict.fromkeys((*current_blockers, *bootstrap.blockers))
    )
    if bootstrap.state is None or blockers:
        return Plan2NativeExamSaveDecision(
            PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION,
            source_kind,
            source_path,
            coverage,
            bootstrap,
            None,
            None,
            blockers,
        )

    root = bootstrap.state
    symbolic = (
        None
        if root.terminal
        else prepare_plan2_symbolic_generated_inputs(
            root,
            compilation.catalog,
            database=compilation.master_database,
            expansion_depth=limits.max_depth,
        )
    )
    predictive_root = root if symbolic is None else symbolic.state
    symbolic_blockers = () if symbolic is None else symbolic.blockers
    try:
        search = dependencies.planner(
            predictive_root,
            compilation.catalog,
            weights,
            limits,
        )
    except Plan2NativeExpectimaxError as error:
        return Plan2NativeExamSaveDecision(
            PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION,
            source_kind,
            source_path,
            coverage,
            bootstrap,
            None,
            None,
            tuple(dict.fromkeys((*symbolic_blockers, error.blocker))),
        )
    except (TypeError, ValueError) as error:
        return Plan2NativeExamSaveDecision(
            PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION,
            source_kind,
            source_path,
            coverage,
            bootstrap,
            None,
            None,
            tuple(dict.fromkeys((
                *symbolic_blockers,
                Plan2NativeBlocker(
                    "plan2-expectimax-failed",
                    f"{type(error).__name__}:{error}",
                ),
            ))),
        )
    if not isinstance(search, Plan2NativeExpectimaxResult):
        return Plan2NativeExamSaveDecision(
            PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION,
            source_kind,
            source_path,
            coverage,
            bootstrap,
            None,
            None,
            tuple(
                dict.fromkeys(
                    (
                        *symbolic_blockers,
                        Plan2NativeBlocker("plan2-expectimax-contract"),
                    )
                )
            ),
        )

    search_blockers = [*symbolic_blockers, *search.blockers]
    after: Plan2NativeHorizonState | None = None
    if predictive_root.terminal:
        if search.best_action is not None or not search.terminal_reached:
            search_blockers.append(
                Plan2NativeBlocker("plan2-terminal-search-contract")
            )
        else:
            after = predictive_root
    elif search.best_action is None:
        search_blockers.append(Plan2NativeBlocker("plan2-expectimax-failed-closed"))
    elif is_plan2_symbolic_guid(getattr(search.best_action, "card_guid", None)):
        # Symbolic GUIDs may appear deeper in a hypothetical principal path,
        # but a root action is an execution boundary and must come from the
        # current LocalSave.  The next observed ExamSave owns any created GUID.
        search_blockers.append(
            Plan2NativeBlocker(
                "plan2-symbolic-guid-execution-forbidden",
                str(getattr(search.best_action, "card_guid", "")),
            )
        )
    else:
        try:
            transition = dependencies.transitioner(
                predictive_root, search.best_action, compilation.catalog
            )
        except (TypeError, ValueError) as error:
            search_blockers.append(
                Plan2NativeBlocker(
                    "plan2-best-action-after-state-failed",
                    f"{type(error).__name__}:{error}",
                )
            )
        else:
            if not isinstance(transition, Plan2NativeTransition):
                search_blockers.append(
                    Plan2NativeBlocker("plan2-after-transition-contract")
                )
            elif not transition.supported or transition.after is None:
                search_blockers.extend(transition.blockers)
                search_blockers.append(
                    Plan2NativeBlocker("plan2-best-action-after-state-unavailable")
                )
            else:
                after = transition.after

    return Plan2NativeExamSaveDecision(
        PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION,
        source_kind,
        source_path,
        coverage,
        bootstrap,
        search,
        after,
        tuple(dict.fromkeys(search_blockers)),
    )


plan_plan2_native_exam_save = decide_plan2_native_exam_save


def decide_plan2_native_horizon(
    root: Plan2NativeHorizonState,
    catalog: Plan2NativeProgramCatalog,
    *,
    limits: Plan2NativeExpectimaxLimits = Plan2NativeExpectimaxLimits(),
    weights: Plan2NativeEvaluationWeights = Plan2NativeEvaluationWeights(),
    planner: Planner = _default_planner,
    transitioner: Transitioner = simulate_plan2_native_action_lifecycle,
    database: str | Path = DEFAULT_DATABASE,
) -> Plan2NativeExamSaveDecision:
    """Plan directly from a replay-proven logical horizon.

    This entry point deliberately performs no LocalSave bootstrap.  The caller
    supplies the already-bound ordered-zone horizon and the exact compiled
    catalog used by card-history replay.
    """

    if not isinstance(root, Plan2NativeHorizonState):
        raise TypeError("root must be Plan2NativeHorizonState")
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be Plan2NativeProgramCatalog")
    if not isinstance(limits, Plan2NativeExpectimaxLimits):
        raise TypeError("limits must be Plan2NativeExpectimaxLimits")
    if not isinstance(weights, Plan2NativeEvaluationWeights):
        raise TypeError("weights must be Plan2NativeEvaluationWeights")
    if not callable(planner) or not callable(transitioner):
        raise TypeError("planner and transitioner must be callable")

    symbolic = (
        None
        if root.terminal
        else prepare_plan2_symbolic_generated_inputs(
            root,
            catalog,
            database=database,
            expansion_depth=limits.max_depth,
        )
    )
    predictive_root = root if symbolic is None else symbolic.state
    symbolic_blockers = () if symbolic is None else symbolic.blockers

    try:
        search = planner(predictive_root, catalog, weights, limits)
    except Plan2NativeExpectimaxError as error:
        return Plan2NativeExamSaveDecision(
            PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION,
            "horizon",
            "",
            None,
            None,
            None,
            None,
            tuple(dict.fromkeys((*symbolic_blockers, error.blocker))),
            root,
        )
    except (TypeError, ValueError) as error:
        return Plan2NativeExamSaveDecision(
            PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION,
            "horizon",
            "",
            None,
            None,
            None,
            None,
            tuple(dict.fromkeys((
                *symbolic_blockers,
                Plan2NativeBlocker(
                    "plan2-expectimax-failed",
                    f"{type(error).__name__}:{error}",
                ),
            ))),
            root,
        )
    if not isinstance(search, Plan2NativeExpectimaxResult):
        return Plan2NativeExamSaveDecision(
            PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION,
            "horizon",
            "",
            None,
            None,
            None,
            None,
            tuple(
                dict.fromkeys(
                    (
                        *symbolic_blockers,
                        Plan2NativeBlocker("plan2-expectimax-contract"),
                    )
                )
            ),
            root,
        )

    blockers = [*symbolic_blockers, *search.blockers]
    after: Plan2NativeHorizonState | None = None
    if predictive_root.terminal:
        if search.best_action is not None or not search.terminal_reached:
            blockers.append(Plan2NativeBlocker("plan2-terminal-search-contract"))
        else:
            after = predictive_root
    elif search.best_action is None:
        blockers.append(Plan2NativeBlocker("plan2-expectimax-failed-closed"))
    elif is_plan2_symbolic_guid(getattr(search.best_action, "card_guid", None)):
        blockers.append(
            Plan2NativeBlocker(
                "plan2-symbolic-guid-execution-forbidden",
                str(getattr(search.best_action, "card_guid", "")),
            )
        )
    else:
        try:
            transition = transitioner(predictive_root, search.best_action, catalog)
        except (TypeError, ValueError) as error:
            blockers.append(
                Plan2NativeBlocker(
                    "plan2-best-action-after-state-failed",
                    f"{type(error).__name__}:{error}",
                )
            )
        else:
            if not isinstance(transition, Plan2NativeTransition):
                blockers.append(Plan2NativeBlocker("plan2-after-transition-contract"))
            elif not transition.supported or transition.after is None:
                blockers.extend(transition.blockers)
                blockers.append(
                    Plan2NativeBlocker("plan2-best-action-after-state-unavailable")
                )
            else:
                after = transition.after
    return Plan2NativeExamSaveDecision(
        PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION,
        "horizon",
        "",
        None,
        None,
        search,
        after,
        tuple(dict.fromkeys(blockers)),
        root,
    )


__all__ = [
    "PLAN2_NATIVE_EXAM_SAVE_ORCHESTRATOR_SCHEMA_VERSION",
    "Plan2NativeCatalogCoverageReport",
    "Plan2NativeCurrentCardProgram",
    "Plan2NativeExamSaveDecision",
    "Plan2NativeExamSaveDependencies",
    "decide_plan2_native_exam_save",
    "decide_plan2_native_horizon",
    "plan_plan2_native_exam_save",
    "with_plan2_policy_action",
]
