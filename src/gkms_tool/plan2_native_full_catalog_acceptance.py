"""Bounded runtime acceptance for the supplied Master's Plan2 card inventory.

The harness is deliberately downstream of compilation.  It does not compile
new effects or alter formulas: it builds small legal horizon snapshots from
program metadata, proves action enumeration per ref, and executes one
representative for each normalized operation signature.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Final, Mapping

from .plan2_native_catalog_status_enchant_encore import (
    Plan2NativeStatusEnchantEncoreRuntime,
)
from .plan2_native_expectimax import enumerate_plan2_native_actions
from .plan2_native_horizon import (
    Plan2MasterInitialDeck,
    Plan2NativeAction,
    Plan2NativeCardProgram,
    Plan2NativeGeneratedAllocatorInput,
    Plan2NativeHorizonState,
    bootstrap_plan2_native_master_deck,
    simulate_plan2_native_action,
)
from .plan2_native_program_catalog import (
    Plan2NativeProgramCatalogCompilation,
    compile_plan2_native_program_catalog,
)
from .plan2_state import Plan2State


SCHEMA_VERSION: Final = 2
OUTCOME_EXECUTED: Final = "executed"
OUTCOME_CONDITIONAL_REJECT: Final = "conditional-reject"
OUTCOME_RUNTIME_FAILURE: Final = "runtime-failure"
_HELPER_PREFERRED_REFS: Final = (
    ("p_card-00-act-0_001", 0),
    ("p_card-00-men-0_001", 0),
    ("p_card-00-sup-0_001", 0),
    ("p_card-02-ido-3_211", 0),
    ("p_card-00-acc-0_002", 0),
)


class Plan2FullCatalogAcceptanceError(RuntimeError):
    def __init__(self, ref: tuple[str, int], detail: str) -> None:
        self.ref = ref
        self.detail = detail
        super().__init__(f"{ref[0]}@{ref[1]}:{detail}")


@dataclass(frozen=True, slots=True)
class Plan2CatalogAcceptanceOutcome:
    card_id: str
    upgrade: int
    signature_id: str
    outcome: str
    program_loaded: bool
    action_enumerated: bool
    scenario: str
    execution_reused: bool = False
    representative_ref: tuple[str, int] | None = None
    invariant_checks: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "signature_id": self.signature_id,
            "outcome": self.outcome,
            "program_loaded": self.program_loaded,
            "action_enumerated": self.action_enumerated,
            "scenario": self.scenario,
            "execution_reused": self.execution_reused,
            "representative_ref": (
                None
                if self.representative_ref is None
                else {
                    "card_id": self.representative_ref[0],
                    "upgrade": self.representative_ref[1],
                }
            ),
            "invariant_checks": list(self.invariant_checks),
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True, slots=True)
class Plan2FullCatalogAcceptanceMatrix:
    schema_version: int
    master_database: str
    outcomes: tuple[Plan2CatalogAcceptanceOutcome, ...]
    signature_count: int
    representative_execution_count: int
    universe_refs: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported full-catalog acceptance schema")
        refs = tuple(value.ref for value in self.outcomes)
        if refs != tuple(sorted(refs)) or len(refs) != len(set(refs)):
            raise ValueError("acceptance outcomes must be unique and sorted")
        if (
            not self.universe_refs
            or self.universe_refs != tuple(sorted(set(self.universe_refs)))
        ):
            raise ValueError("Master inventory must be non-empty, unique and sorted")

    @property
    def version_count(self) -> int:
        return len(self.outcomes)

    @property
    def outcome_counts(self) -> Mapping[str, int]:
        return dict(sorted(Counter(value.outcome for value in self.outcomes).items()))

    @property
    def runtime_failures(self) -> tuple[Plan2CatalogAcceptanceOutcome, ...]:
        return tuple(
            value for value in self.outcomes
            if value.outcome == OUTCOME_RUNTIME_FAILURE
        )

    @property
    def condition_rejections(self) -> tuple[Plan2CatalogAcceptanceOutcome, ...]:
        return tuple(
            value for value in self.outcomes
            if value.outcome == OUTCOME_CONDITIONAL_REJECT
        )

    @property
    def accepted(self) -> bool:
        return (
            tuple(value.ref for value in self.outcomes) == self.universe_refs
            and all(value.program_loaded for value in self.outcomes)
            and all(value.action_enumerated for value in self.outcomes)
            and not self.runtime_failures
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "scope": (
                f"offline full {len(self.universe_refs)} Plan2 native catalog runtime acceptance"
            ),
            "master_database": self.master_database,
            "universe_refs": [list(ref) for ref in self.universe_refs],
            "accounting": {
                "target_versions": len(self.universe_refs),
                "outcome_versions": self.version_count,
                "signature_count": self.signature_count,
                "representative_execution_count": (
                    self.representative_execution_count
                ),
                "outcome_counts": self.outcome_counts,
                "accepted": self.accepted,
            },
            "contracts": {
                "program_loaded_per_ref": True,
                "legal_action_enumeration_per_ref": True,
                "execution_deduplicated_by_operation_signature": True,
                "preexisting_guid_identity_preserved": True,
                "settled_zone_required": True,
                "active_status_uid_uniqueness_required": True,
                "conditional_rejection_is_not_runtime_failure": True,
            },
            "outcomes": [value.to_dict() for value in self.outcomes],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"


@dataclass(frozen=True, slots=True)
class _PlayableScenario:
    label: str
    state: Plan2NativeHorizonState
    card_guid: str


def _unwrap(operation: object) -> object:
    return getattr(operation, "operation", operation)


def _operation_signature(program: Plan2NativeCardProgram) -> tuple[object, ...]:
    executor = program.native_playing_executor
    operations = tuple(getattr(executor, "operations", ()))
    operation_rows = tuple(
        (
            type(operation).__name__,
            type(_unwrap(operation)).__name__,
            str(getattr(operation, "effect_id", getattr(_unwrap(operation), "effect_id", ""))),
            str(
                getattr(
                    getattr(operation, "handoff", None),
                    "trigger_id",
                    getattr(
                        getattr(getattr(operation, "handoff", None), "trigger", None),
                        "trigger_id",
                        "",
                    ),
                )
            ),
        )
        for operation in operations
    )
    cost = program.native_cost
    return (
        program.category,
        program.move_destination,
        program.is_end_turn_lost,
        program.ends_turn,
        program.native_play_predicate_id,
        program.native_horizon_predicate_id,
        None
        if cost is None
        else (
            cost.cost_type,
            cost.base_cost,
            cost.penetrate,
        ),
        operation_rows,
    )


def _candidate_scalars() -> tuple[tuple[str, Plan2State], ...]:
    def scalar(
        *,
        current_turn: int = 1,
        review: int = 100,
        block: int = 100,
        aggressive: int = 100,
        stamina: int = 1000,
    ) -> Plan2State:
        return Plan2State(
            current_turn=current_turn,
            review=review,
            block=block,
            card_play_aggressive=aggressive,
            stamina=stamina,
            max_stamina=1000,
            exam_card_play_count=3,
            turn_card_play_count=3,
        )

    return (
        ("high-fields", scalar()),
        ("zero-fields", scalar(review=0, block=0, aggressive=0)),
        ("no-block", scalar(block=0)),
        ("not-aggressive-nine", scalar(aggressive=0)),
        ("aggressive-three", scalar(aggressive=3)),
        ("aggressive-six", scalar(aggressive=6)),
        ("aggressive-eight", scalar(aggressive=8)),
        ("review-one", scalar(review=1)),
        ("review-ten", scalar(review=10)),
        ("review-fifteen", scalar(review=15)),
        ("stamina-half", scalar(stamina=500)),
        ("stamina-low", scalar(stamina=10)),
        ("final-turn", scalar(current_turn=3)),
        (
            "final-turn-no-block",
            scalar(current_turn=3, block=0, aggressive=8),
        ),
    )


def _helper_refs(
    compilation: Plan2NativeProgramCatalogCompilation,
    target: tuple[str, int],
) -> tuple[tuple[str, int], ...]:
    compiled = set(compilation.compiled_refs)
    selected = [ref for ref in _HELPER_PREFERRED_REFS if ref in compiled and ref != target]
    categories: set[str] = set()
    for program in compilation.catalog.programs:
        if program.ref == target or program.ref in selected:
            continue
        if program.category in categories:
            continue
        categories.add(program.category)
        selected.append(program.ref)
    return tuple(dict.fromkeys(selected))


def _input_requirements(program: Plan2NativeCardProgram) -> tuple[int, bool, bool]:
    create_count = 0
    force = False
    encore = False
    operations = tuple(getattr(program.native_playing_executor, "operations", ()))
    for raw in operations:
        operation = _unwrap(raw)
        name = type(operation).__name__
        if name == "Plan2MasterGeneratedCreateOperation":
            create_count = max(create_count, int(operation.program.count_max))
        elif name == "Plan2MasterGeneratedBuffForceOperation":
            force = True
        elif name == "Plan2MasterStatusEnchantEncoreOperation":
            encore = True
    return create_count, force, encore


def _base_state(
    compilation: Plan2NativeProgramCatalogCompilation,
    program: Plan2NativeCardProgram,
) -> tuple[Plan2NativeHorizonState, str]:
    target = program.ref
    refs = (target, *_helper_refs(compilation, target))
    manifest = Plan2MasterInitialDeck(
        idol_card_id="i_card-full-catalog-acceptance",
        produce_id="produce-full-catalog-acceptance",
        plan_type="ProducePlanType_Plan2",
        exam_effect_type="ProduceExamEffectType_Review",
        produce_default_deck_id="full-catalog-acceptance-default",
        character_deck_id="full-catalog-acceptance-character",
        card_refs=refs,
    )
    boot = bootstrap_plan2_native_master_deck(
        manifest,
        random_state=0x12345678,
        limit_turn=3,
        stamina=1000,
        max_stamina=1000,
        draw_count=1,
        hand_limit=max(len(refs) + 8, 16),
    )
    if boot.state is None or boot.blockers:
        raise Plan2FullCatalogAcceptanceError(target, "scenario-bootstrap-failed")
    state = boot.state
    target_card = next(
        value
        for value in state.zones.card_universe
        if (value.card_id, value.effective_upgrade) == target
    )
    helpers = tuple(
        value for value in state.zones.card_universe if value.guid != target_card.guid
    )
    deck = helpers[: max(1, len(helpers) - 2)]
    grave = helpers[len(deck) : len(deck) + 1]
    lost = helpers[len(deck) + 1 :]
    zones = replace(
        state.zones,
        hand=(target_card,),
        deck=deck,
        grave=grave,
        lost=lost,
    )
    create_count, force, encore = _input_requirements(program)
    selected = next(iter((*deck, *grave, *lost)), None)
    generated_inputs = ()
    if create_count or force:
        generated_inputs = (
            Plan2NativeGeneratedAllocatorInput(
                target_card.guid,
                guid_tokens=tuple(
                    f"accept-created-{index}" for index in range(create_count)
                ),
                plan_ignore_card_ids=(),
                force_selected_guid=(
                    None if not force or selected is None else selected.guid
                ),
                native_status_uid=10_000 if force else None,
            ),
        )
    return (
        replace(
            state,
            zones=zones,
            generated_inputs=generated_inputs,
            encore_runtime=(
                Plan2NativeStatusEnchantEncoreRuntime() if encore else None
            ),
            judge_parameter=100,
            clear_border=200,
            review_consumption_sum=10,
            block_consumption_sum_count=10,
            plays_remaining=3,
        ),
        target_card.guid,
    )


def _find_playable_scenario(
    compilation: Plan2NativeProgramCatalogCompilation,
    program: Plan2NativeCardProgram,
) -> tuple[_PlayableScenario | None, tuple[str, ...], bool]:
    base, guid = _base_state(compilation, program)
    observed: list[str] = []
    enumerated = False
    for label, scalar in _candidate_scalars():
        candidate = replace(base, scalar=scalar)
        enumeration = enumerate_plan2_native_actions(candidate, compilation.catalog)
        enumerated = enumerated or bool(enumeration.actions)
        action = next(
            (
                value
                for value in enumeration.actions
                if value.kind == "play" and value.card_guid == guid
            ),
            None,
        )
        if action is not None:
            return (
                _PlayableScenario(label, candidate, guid),
                tuple(dict.fromkeys(observed)),
                enumerated,
            )
        observed.extend(
            f"{value.code}:{value.detail}" for value in enumeration.blockers
        )
    return None, tuple(dict.fromkeys(observed)), enumerated


def _active_status_uids(state: Plan2NativeHorizonState) -> tuple[int, ...]:
    return (
        *(value.status_uid for value in state.scalar.review_multiple_layers),
        *(value.status_uid for value in state.scalar.end_turn_listeners),
        *(value.status_uid for value in state.scalar.card_play_listeners),
        *(
            (state.scalar.stamina_consumption_add_status.status_uid,)
            if state.scalar.stamina_consumption_add_status is not None
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
            (value.status_uid for value in state.status_child_review_state.active)
            if state.status_child_review_state is not None
            else ()
        ),
        *(
            (
                value.status_uid
                for value in state.status_child_review_state.review_count_add_layers
            )
            if state.status_child_review_state is not None
            else ()
        ),
        *(
            (value.status_uid for value in state.encore_runtime.listeners)
            if state.encore_runtime is not None
            else ()
        ),
        *(
            state.aggressive_additive_runtime.status_uids
            if state.aggressive_additive_runtime is not None
            else ()
        ),
        *(
            state.block_fix_restriction_runtime.active_status_uids
            if state.block_fix_restriction_runtime is not None
            else ()
        ),
        *(value.status_uid for value in state.search_play_card_stamina_runtime.statuses),
        *(value.status_uid for value in state.start_play_listeners),
        *(value.status_uid for value in state.triggered_review_status_runtime.listeners),
        *state.item_runtime.active_status_uids,
    )


def _verify_transition_invariants(
    scenario: _PlayableScenario,
    after: Plan2NativeHorizonState,
) -> tuple[str, ...]:
    if after.zones.pending_played is not None:
        raise ValueError("transition-left-pending-playing-card")
    before_by_guid = {
        value.guid: value for value in scenario.state.zones.card_universe
    }
    after_by_guid = {value.guid: value for value in after.zones.card_universe}
    if not set(before_by_guid) <= set(after_by_guid):
        raise ValueError("preexisting-guid-disappeared")
    if any(
        before.persistent_identity != after_by_guid[guid].persistent_identity
        for guid, before in before_by_guid.items()
    ):
        raise ValueError("preexisting-persistent-identity-changed")
    if len(after_by_guid) != len(after.zones.card_universe):
        raise ValueError("duplicate-guid-after-transition")
    uids = _active_status_uids(after)
    if len(uids) != len(set(uids)):
        raise ValueError("duplicate-active-status-uid")
    source = after_by_guid.get(scenario.card_guid)
    if source is None or source.runtime_state.play_count < 1:
        raise ValueError("played-source-runtime-not-committed")
    return (
        "settled-zone",
        "preexisting-guid-conservation",
        "persistent-identity",
        "active-status-uid-unique",
        "played-source-runtime-committed",
    )


def run_plan2_full_catalog_acceptance(
    *,
    compilation: Plan2NativeProgramCatalogCompilation | None = None,
    strict_runtime_failures: bool = True,
) -> Plan2FullCatalogAcceptanceMatrix:
    compilation = compilation or compile_plan2_native_program_catalog()
    if not compilation.fully_compiled:
        raise ValueError("full-catalog acceptance requires the complete Master inventory")

    programs = tuple(sorted(compilation.catalog.programs, key=lambda value: value.ref))
    signature_keys = tuple(dict.fromkeys(_operation_signature(value) for value in programs))
    signature_ids = {
        value: f"signature-{index:04d}"
        for index, value in enumerate(signature_keys, start=1)
    }
    scenarios: dict[tuple[str, int], _PlayableScenario] = {}
    rejected: dict[tuple[str, int], tuple[tuple[str, ...], bool]] = {}
    for program in programs:
        scenario, blockers, enumerated = _find_playable_scenario(compilation, program)
        if scenario is None:
            rejected[program.ref] = blockers, enumerated
        else:
            scenarios[program.ref] = scenario

    outcomes: dict[tuple[str, int], Plan2CatalogAcceptanceOutcome] = {}
    executions = 0
    grouped: dict[tuple[object, ...], list[Plan2NativeCardProgram]] = {}
    for program in programs:
        grouped.setdefault(_operation_signature(program), []).append(program)
    for signature, members in grouped.items():
        playable = tuple(value for value in members if value.ref in scenarios)
        for member in members:
            if member.ref in scenarios:
                continue
            blockers, enumerated = rejected[member.ref]
            outcomes[member.ref] = Plan2CatalogAcceptanceOutcome(
                member.card_id,
                member.upgrade,
                signature_ids[signature],
                OUTCOME_CONDITIONAL_REJECT,
                True,
                enumerated,
                "all-bounded-candidates-rejected",
                blockers=blockers,
            )
        if not playable:
            continue
        representative = playable[0]
        scenario = scenarios[representative.ref]
        transition = simulate_plan2_native_action(
            scenario.state,
            Plan2NativeAction("play", scenario.card_guid),
            compilation.catalog,
        )
        executions += 1
        if not transition.supported or transition.after is None:
            detail = tuple(
                f"{value.code}:{value.detail}" for value in transition.blockers
            ) or ("unsupported-transition-without-blocker",)
            for member in playable:
                outcomes[member.ref] = Plan2CatalogAcceptanceOutcome(
                    member.card_id,
                    member.upgrade,
                    signature_ids[signature],
                    OUTCOME_RUNTIME_FAILURE,
                    True,
                    True,
                    scenarios[member.ref].label,
                    execution_reused=member is not representative,
                    representative_ref=representative.ref,
                    blockers=detail,
                )
            if strict_runtime_failures:
                raise Plan2FullCatalogAcceptanceError(representative.ref, detail[0])
            continue
        try:
            checks = _verify_transition_invariants(scenario, transition.after)
        except (TypeError, ValueError) as error:
            if strict_runtime_failures:
                raise Plan2FullCatalogAcceptanceError(
                    representative.ref,
                    f"invariant:{type(error).__name__}:{error}",
                ) from error
            checks = ()
            failure = f"invariant:{type(error).__name__}:{error}"
            for member in playable:
                outcomes[member.ref] = Plan2CatalogAcceptanceOutcome(
                    member.card_id,
                    member.upgrade,
                    signature_ids[signature],
                    OUTCOME_RUNTIME_FAILURE,
                    True,
                    True,
                    scenarios[member.ref].label,
                    execution_reused=member is not representative,
                    representative_ref=representative.ref,
                    blockers=(failure,),
                )
            continue
        for member in playable:
            outcomes[member.ref] = Plan2CatalogAcceptanceOutcome(
                member.card_id,
                member.upgrade,
                signature_ids[signature],
                OUTCOME_EXECUTED,
                True,
                True,
                scenarios[member.ref].label,
                execution_reused=member is not representative,
                representative_ref=representative.ref,
                invariant_checks=checks,
            )

    matrix = Plan2FullCatalogAcceptanceMatrix(
        SCHEMA_VERSION,
        compilation.master_database,
        tuple(outcomes[ref] for ref in sorted(outcomes)),
        len(signature_ids),
        executions,
        compilation.universe_refs,
    )
    if tuple(value.ref for value in matrix.outcomes) != compilation.universe_refs:
        raise ValueError("acceptance outcomes do not match the Master inventory")
    return matrix


def write_plan2_full_catalog_acceptance_audit(
    path: str | Path,
    *,
    matrix: Plan2FullCatalogAcceptanceMatrix | None = None,
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        (matrix or run_plan2_full_catalog_acceptance()).to_json(),
        encoding="utf-8",
    )
    return target


__all__ = [
    "OUTCOME_CONDITIONAL_REJECT",
    "OUTCOME_EXECUTED",
    "OUTCOME_RUNTIME_FAILURE",
    "Plan2CatalogAcceptanceOutcome",
    "Plan2FullCatalogAcceptanceError",
    "Plan2FullCatalogAcceptanceMatrix",
    "run_plan2_full_catalog_acceptance",
    "write_plan2_full_catalog_acceptance_audit",
]
