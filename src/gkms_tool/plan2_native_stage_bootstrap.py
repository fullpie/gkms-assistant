"""Pure offline stage-start builder for the Plan2 native horizon.

The builder joins explicit stage facts to an outer ``ProduceRolloutState``.
It does not discover Master rows, manufacture LocalSave provenance, or infer
an active loadout.  Callers provide the compiled card catalog, exact typed
runtime projection, RNG state, and ordered-zone binding.

Fresh card GUIDs are authoritative only when they already occur on every
``DeckEntry``.  A deterministic fallback exists solely for declared fixture
scenarios and is retained in the returned audit as non-authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Final, Mapping, Protocol

from .audition_local_save_state import (
    LocalSaveExamCard,
    empty_local_save_exam_card_runtime_state,
)
from .audition_native_ordered_zones import (
    NATIVE_ORDERED_ZONE_SCHEMA_VERSION,
    NativeOrderedCardInstance,
    NativeOrderedZoneError,
    NativeOrderedZoneEvidenceBinding,
    NativeOrderedZoneState,
)
from .exam_native_rng import FixedDeckOrderError, UINT32_MASK, shuffle_unfixed_pool
from .native_exam_formula import INT32_MAX
from .plan2_add_grow_effect import Plan2AddGrowDeckAllState
from .plan2_card_search_play_count_buff import PlayCountBuffRuntime
from .plan2_exam_mode import (
    PLAN2_AUDITION_EXAM_TYPE,
    PLAN2_LESSON_EXAM_TYPE,
    Plan2ExamMode,
    Plan2ExamModeError,
    Plan2ScheduledGimmick,
    Plan2ScheduledGimmickHook,
    Plan2ScheduledGimmickHookKey,
    compile_plan2_scheduled_gimmicks,
    resolve_plan2_exam_mode,
)
from .plan2_exam_save_battle_scoring import (
    PLAN2_EXAM_SAVE_BATTLE_SCORING_SCHEMA_VERSION,
    Plan2BattleScoringError,
    Plan2ExamSaveBattleScoringContext,
)
from .plan2_native_catalog_aggressive_additive import (
    Plan2AggressiveAdditiveRuntime,
)
from .plan2_native_catalog_block_fix_restriction import (
    Plan2NativeBlockFixRestrictionRuntime,
)
from .plan2_native_catalog_debuff import Plan2NativeDebuffRegistry
from .plan2_native_catalog_effect_chains import Plan2NativeEffectChainRuntime
from .plan2_native_catalog_extra_turn import Plan2NativeExtraTurnRuntime
from .plan2_native_catalog_generated_cards import GeneratedHandMoveTurnState
from .plan2_native_catalog_review_dynamic import Plan2NativeReviewDynamicRuntime
from .plan2_native_catalog_stamina import Plan2StaminaModifierRuntime
from .plan2_native_catalog_status_children import Plan2NativeStatusChildInput
from .plan2_native_catalog_status_enchant import Plan2NativeStatusEnchantRuntime
from .plan2_native_catalog_status_enchant_encore import (
    Plan2NativeStatusEnchantEncoreRuntime,
)
from .plan2_native_catalog_status_enchant_triggered import (
    Plan2TriggeredReviewStatusRuntime,
)
from .plan2_native_horizon import (
    PHASE_MAIN,
    PLAN2_NATIVE_HORIZON_SCHEMA_VERSION,
    Plan2NativeBlocker,
    Plan2NativeDrinkRuntime,
    Plan2NativeGeneratedAllocatorInput,
    Plan2NativeHorizonError,
    Plan2NativeHorizonState,
    Plan2NativeProgramCatalog,
    Plan2NativeQueuedAction,
    Plan2NativeTimer,
)
from .plan2_native_item_runtime import Plan2NativeItemRuntime
from .plan2_review_count_add_interval3 import Plan2ReviewCountAddInterval3State
from .plan2_search_play_card_stamina_consumption_change import (
    SearchPlayCardStaminaRuntime,
)
from .plan2_start_play_trigger import Plan2StartPlayListener
from .plan2_state import Plan2State
from .produce_rollout import DeckEntry, ProduceRolloutState


PLAN2_NATIVE_STAGE_BOOTSTRAP_SCHEMA_VERSION: Final = 1
PLAN2_NATIVE_STAGE_PLAN_TYPES: Final = frozenset(
    {"plan2", "ProducePlanType_Plan2"}
)
PLAN2_NATIVE_STAGE_AUDITION_STEPS: Final = frozenset({16, 18})


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty text")
    return value


def _plain_int(
    value: object,
    label: str,
    *,
    minimum: int = 0,
    maximum: int = INT32_MAX,
) -> int:
    if (
        type(value) is not int
        or value < minimum
        or value > maximum
    ):
        raise ValueError(
            f"{label} must be an integer in {minimum}..{maximum}"
        )
    return value


def _border(value: object, label: str) -> int:
    return _plain_int(value, label, minimum=-1)


def _refs(values: object, label: str) -> tuple[str, ...]:
    resolved = tuple(values)  # type: ignore[arg-type]
    if any(not isinstance(value, str) or not value for value in resolved):
        raise ValueError(f"{label} must contain non-empty text")
    if len(resolved) != len(set(resolved)):
        raise ValueError(f"{label} must be unique")
    return resolved


def _ordered_refs(values: object, label: str) -> tuple[str, ...]:
    resolved = tuple(values)  # type: ignore[arg-type]
    if any(not isinstance(value, str) or not value for value in resolved):
        raise ValueError(f"{label} must contain non-empty text")
    return resolved


def _block(
    blockers: list[Plan2NativeBlocker],
    code: str,
    detail: str = "",
) -> None:
    value = Plan2NativeBlocker(code, detail)
    if value not in blockers:
        blockers.append(value)


class Plan2NativeStageAuthority(Enum):
    """Caller declaration for the supplied binding and card identities."""

    AUTHORITATIVE = "authoritative"
    DETERMINISTIC_FIXTURE = "deterministic_fixture"


@dataclass(frozen=True, slots=True)
class Plan2NativeStageGimmickFact:
    """One known stage-start schedule row, before its hook is bound."""

    turn: int
    gimmick_group_id: str
    gimmick_effect_id: str

    def __post_init__(self) -> None:
        _plain_int(self.turn, "gimmick.turn", minimum=1)
        _text(self.gimmick_group_id, "gimmick_group_id")
        _text(self.gimmick_effect_id, "gimmick_effect_id")

    def to_serialized_shape(self) -> dict[str, object]:
        return {
            "turn": self.turn,
            "gimmickGroupId": self.gimmick_group_id,
            "gimmickEffectId": self.gimmick_effect_id,
        }


@dataclass(frozen=True, slots=True)
class Plan2NativeStageFacts:
    """Explicit stage facts required to create a fresh Plan2 exam root.

    ``limit_turn`` is the base native turn limit.  ``extra_turn`` remains a
    separate runtime value, so an Initial Regular lesson fact maps
    ``base_turns`` here and its runtime-context ``exam_extra_turn`` to
    ``extra_turn``.  Lesson ``turn_parameter_types`` must remain empty.
    """

    stage_id: str
    setting_id: str
    exam_type: int
    step_type_value: int
    limit_turn: int
    clear_border: int
    limit_border: int
    plan_type: str = "plan2"
    extra_turn: int = 0
    turn_parameter_types: tuple[int, ...] = ()
    battle_bonus_permille: tuple[int, int, int] = (0, 0, 0)
    initial_score: int = 0
    initial_parameter_values: tuple[int, int, int] = (0, 0, 0)
    current_turn_total_add_parameter: int = 0
    draw_count: int = 3
    hand_limit: int = 5
    plays_remaining: int = 1
    gimmicks: tuple[Plan2NativeStageGimmickFact, ...] = ()

    def __post_init__(self) -> None:
        _text(self.stage_id, "stage_id")
        _text(self.setting_id, "setting_id")
        _text(self.plan_type, "plan_type")
        _plain_int(self.exam_type, "exam_type")
        _plain_int(self.step_type_value, "step_type_value")
        _plain_int(self.limit_turn, "limit_turn", minimum=1)
        _plain_int(self.extra_turn, "extra_turn")
        _border(self.clear_border, "clear_border")
        _border(self.limit_border, "limit_border")
        schedule = tuple(self.turn_parameter_types)
        for index, value in enumerate(schedule):
            _plain_int(value, f"turn_parameter_types[{index}]")
        object.__setattr__(self, "turn_parameter_types", schedule)
        bonuses = tuple(self.battle_bonus_permille)
        if len(bonuses) != 3:
            raise ValueError("battle_bonus_permille must contain three values")
        for index, value in enumerate(bonuses):
            _plain_int(value, f"battle_bonus_permille[{index}]")
        object.__setattr__(self, "battle_bonus_permille", bonuses)
        _plain_int(self.initial_score, "initial_score")
        parameter_values = tuple(self.initial_parameter_values)
        if len(parameter_values) != 3:
            raise ValueError("initial_parameter_values must contain three values")
        for index, value in enumerate(parameter_values):
            _plain_int(value, f"initial_parameter_values[{index}]")
        object.__setattr__(self, "initial_parameter_values", parameter_values)
        _plain_int(
            self.current_turn_total_add_parameter,
            "current_turn_total_add_parameter",
        )
        _plain_int(self.draw_count, "draw_count")
        _plain_int(self.hand_limit, "hand_limit", minimum=1)
        _plain_int(self.plays_remaining, "plays_remaining")
        gimmicks = tuple(self.gimmicks)
        if any(
            not isinstance(value, Plan2NativeStageGimmickFact)
            for value in gimmicks
        ):
            raise TypeError("gimmicks must contain Plan2NativeStageGimmickFact")
        object.__setattr__(self, "gimmicks", gimmicks)

    def to_dict(self) -> dict[str, object]:
        return {
            "stage_id": self.stage_id,
            "setting_id": self.setting_id,
            "plan_type": self.plan_type,
            "exam_type": self.exam_type,
            "step_type_value": self.step_type_value,
            "limit_turn": self.limit_turn,
            "extra_turn": self.extra_turn,
            "clear_border": self.clear_border,
            "limit_border": self.limit_border,
            "turn_parameter_types": list(self.turn_parameter_types),
            "battle_bonus_permille": list(self.battle_bonus_permille),
            "initial_score": self.initial_score,
            "initial_parameter_values": list(self.initial_parameter_values),
            "current_turn_total_add_parameter": (
                self.current_turn_total_add_parameter
            ),
            "draw_count": self.draw_count,
            "hand_limit": self.hand_limit,
            "plays_remaining": self.plays_remaining,
            "gimmicks": [
                value.to_serialized_shape() for value in self.gimmicks
            ],
        }


class _InitialRegularLessonRuntimeContextLike(Protocol):
    exam_extra_turn: int
    battle_bonus_permille: tuple[int, int, int]


class InitialRegularLessonStageFactLike(Protocol):
    """Structural boundary avoiding an import back to the outer catalog."""

    stage_id: str
    setting_id: str
    plan_type: str
    step_type_value: int
    base_turns: int
    clear_border: int
    limit_border: int | None
    runtime_turns: int | None
    current_parameter_type: int
    is_sp: bool
    is_hard: bool
    runtime_context: _InitialRegularLessonRuntimeContextLike | None


def plan2_stage_facts_from_initial_regular_lesson(
    stage: InitialRegularLessonStageFactLike,
    *,
    draw_count: int = 3,
    hand_limit: int = 5,
    plays_remaining: int = 1,
) -> Plan2NativeStageFacts:
    """Map an existing Initial Regular lesson fact without importing it.

    ``runtime_turns`` in that catalog is the resolved total.  The horizon
    keeps the same fact as ``base_turns + exam_extra_turn`` so its terminal
    boundary is not double counted.
    """

    context = stage.runtime_context
    if (
        context is None
        or stage.runtime_turns is None
        or stage.limit_border is None
    ):
        raise ValueError("initial-regular lesson stage runtime context is incomplete")
    expected_runtime_turns = stage.base_turns + context.exam_extra_turn
    if stage.runtime_turns != expected_runtime_turns:
        raise ValueError(
            "initial-regular lesson runtime_turns disagrees with base + extra"
        )
    mode = resolve_plan2_exam_mode(
        PLAN2_LESSON_EXAM_TYPE,
        stage.step_type_value,
    )
    if int(mode.parameter_type) != stage.current_parameter_type:
        raise ValueError("initial-regular lesson parameter type disagrees with step")
    if mode.is_sp != stage.is_sp or mode.is_hard != stage.is_hard:
        raise ValueError("initial-regular lesson variant disagrees with step")
    return Plan2NativeStageFacts(
        stage_id=stage.stage_id,
        setting_id=stage.setting_id,
        plan_type=stage.plan_type,
        exam_type=PLAN2_LESSON_EXAM_TYPE,
        step_type_value=stage.step_type_value,
        limit_turn=stage.base_turns,
        extra_turn=context.exam_extra_turn,
        clear_border=stage.clear_border,
        limit_border=stage.limit_border,
        turn_parameter_types=(),
        battle_bonus_permille=context.battle_bonus_permille,
        draw_count=draw_count,
        hand_limit=hand_limit,
        plays_remaining=plays_remaining,
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeStageChance:
    """Explicit native RNG root plus a caller-owned zone binding."""

    random_state: int
    binding: NativeOrderedZoneEvidenceBinding | None
    authority: Plan2NativeStageAuthority = Plan2NativeStageAuthority.AUTHORITATIVE
    fixed_deck_orders: tuple[int, ...] = ()
    fixture_guid_namespace: str = "plan2-stage-fixture"

    def __post_init__(self) -> None:
        _plain_int(
            self.random_state,
            "random_state",
            maximum=UINT32_MASK,
        )
        try:
            authority = Plan2NativeStageAuthority(self.authority)
        except (TypeError, ValueError) as error:
            raise ValueError("unsupported stage authority") from error
        object.__setattr__(self, "authority", authority)
        orders = tuple(self.fixed_deck_orders)
        for index, value in enumerate(orders):
            _plain_int(
                value,
                f"fixed_deck_orders[{index}]",
                minimum=-(2**31),
            )
        object.__setattr__(self, "fixed_deck_orders", orders)
        _text(self.fixture_guid_namespace, "fixture_guid_namespace")


@dataclass(frozen=True, slots=True)
class Plan2NativeStageRuntime:
    """Typed, already-compiled loadout projection for one fresh horizon.

    The adapter deliberately does not translate outer item/passive IDs.  A
    registry must compile them into these existing owner runtimes and list
    every resolved session ref.  Any missing, extra, or explicitly unresolved
    ref blocks the bootstrap instead of becoming a neutral effect.
    """

    scalar: Plan2State = Plan2State()
    stamina_modifiers: Plan2StaminaModifierRuntime = Plan2StaminaModifierRuntime()
    status_enchant: Plan2NativeStatusEnchantRuntime = Plan2NativeStatusEnchantRuntime()
    review_dynamic: Plan2NativeReviewDynamicRuntime = Plan2NativeReviewDynamicRuntime()
    debuff_registry: Plan2NativeDebuffRegistry = Plan2NativeDebuffRegistry()
    effect_chains: Plan2NativeEffectChainRuntime = Plan2NativeEffectChainRuntime()
    generated_runtime: PlayCountBuffRuntime = PlayCountBuffRuntime()
    generated_inputs: tuple[Plan2NativeGeneratedAllocatorInput, ...] = ()
    status_child_inputs: tuple[Plan2NativeStatusChildInput, ...] = ()
    status_child_review_state: Plan2ReviewCountAddInterval3State | None = None
    total_effect_draw_card_count: int = 0
    generated_hand_move_turn: GeneratedHandMoveTurnState = field(
        default_factory=lambda: GeneratedHandMoveTurnState(0)
    )
    encore_runtime: Plan2NativeStatusEnchantEncoreRuntime | None = None
    aggressive_additive_runtime: Plan2AggressiveAdditiveRuntime | None = None
    block_fix_restriction_runtime: (
        Plan2NativeBlockFixRestrictionRuntime | None
    ) = None
    extra_turn_runtime: Plan2NativeExtraTurnRuntime | None = None
    stamina_recover_restricted: bool = False
    stamina_recover_add_permil: int | None = None
    search_play_card_stamina_runtime: SearchPlayCardStaminaRuntime = (
        SearchPlayCardStaminaRuntime()
    )
    add_grow_runtime: Plan2AddGrowDeckAllState | None = None
    item_runtime: Plan2NativeItemRuntime = Plan2NativeItemRuntime()
    drink_runtime: Plan2NativeDrinkRuntime = Plan2NativeDrinkRuntime()
    start_play_listeners: tuple[Plan2StartPlayListener, ...] = ()
    triggered_review_status_runtime: Plan2TriggeredReviewStatusRuntime = field(
        default_factory=Plan2TriggeredReviewStatusRuntime
    )
    review_consumption_sum: int = 0
    block_consumption_sum_count: int = 0
    timers: tuple[Plan2NativeTimer, ...] = ()
    command_queue: tuple[Plan2NativeQueuedAction, ...] = ()
    opaque_status_queue: tuple[str, ...] = ()
    resolved_item_session_refs: tuple[str, ...] = ()
    resolved_drink_session_refs: tuple[str, ...] = ()
    resolved_passive_session_refs: tuple[str, ...] = ()
    unresolved_runtime_refs: tuple[str, ...] = ()
    runtime_adapter_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.scalar, Plan2State):
            raise TypeError("scalar must be Plan2State")
        for name in (
            "resolved_item_session_refs",
            "resolved_passive_session_refs",
            "unresolved_runtime_refs",
            "runtime_adapter_ids",
        ):
            object.__setattr__(self, name, _refs(getattr(self, name), name))
        object.__setattr__(
            self,
            "resolved_drink_session_refs",
            _ordered_refs(
                self.resolved_drink_session_refs,
                "resolved_drink_session_refs",
            ),
        )
        object.__setattr__(self, "generated_inputs", tuple(self.generated_inputs))
        object.__setattr__(self, "status_child_inputs", tuple(self.status_child_inputs))
        object.__setattr__(self, "start_play_listeners", tuple(self.start_play_listeners))
        object.__setattr__(self, "timers", tuple(self.timers))
        object.__setattr__(self, "command_queue", tuple(self.command_queue))
        if not isinstance(self.drink_runtime, Plan2NativeDrinkRuntime):
            raise TypeError("drink_runtime must be Plan2NativeDrinkRuntime")
        opaque = tuple(self.opaque_status_queue)
        if any(not isinstance(value, str) or not value for value in opaque):
            raise ValueError("opaque_status_queue must contain non-empty text")
        object.__setattr__(self, "opaque_status_queue", opaque)


@dataclass(frozen=True, slots=True, order=True)
class Plan2NativeStageAuditFlag:
    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        _text(self.code, "audit flag code")
        if not isinstance(self.detail, str):
            raise TypeError("audit flag detail must be text")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Plan2NativeStageDeckAudit:
    entry_index: int
    card_id: str
    upgrade: int
    count: int
    instance_ids: tuple[str, ...]
    resolved_instance_ids: tuple[str, ...]
    guid_source: str

    def to_dict(self) -> dict[str, object]:
        return {
            "entry_index": self.entry_index,
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "count": self.count,
            "instance_ids": list(self.instance_ids),
            "resolved_instance_ids": list(self.resolved_instance_ids),
            "guid_source": self.guid_source,
        }


@dataclass(frozen=True, slots=True)
class Plan2NativeStageBootstrap:
    """Solver-ready state or a typed fail-closed audit."""

    schema_version: int
    state: Plan2NativeHorizonState | None
    blockers: tuple[Plan2NativeBlocker, ...]
    audit_flags: tuple[Plan2NativeStageAuditFlag, ...]
    mode: Plan2ExamMode | None
    deck_entries: tuple[Plan2NativeStageDeckAudit, ...]
    catalog_refs: tuple[tuple[str, int], ...]
    runtime_adapter_ids: tuple[str, ...]
    random_state_before: int
    random_state_after: int | None
    declared_authority: Plan2NativeStageAuthority
    guid_authoritative: bool

    def __post_init__(self) -> None:
        if self.schema_version != PLAN2_NATIVE_STAGE_BOOTSTRAP_SCHEMA_VERSION:
            raise ValueError("unsupported Plan2 stage bootstrap schema")
        if self.blockers and self.state is not None:
            raise ValueError("blocked bootstrap must not expose a horizon state")
        if self.state is not None and not isinstance(
            self.state, Plan2NativeHorizonState
        ):
            raise TypeError("state must be Plan2NativeHorizonState or None")

    @property
    def simulation_ready(self) -> bool:
        return self.state is not None and not self.blockers

    @property
    def non_authoritative_fixture(self) -> bool:
        return (
            self.declared_authority
            is Plan2NativeStageAuthority.DETERMINISTIC_FIXTURE
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "simulation_ready": self.simulation_ready,
            "declared_authority": self.declared_authority.value,
            "guid_authoritative": self.guid_authoritative,
            "mode": None if self.mode is None else {
                "exam_type": self.mode.exam_type,
                "step_type_value": self.mode.step_type_value,
                "is_lesson": self.mode.is_lesson,
                "is_sp": self.mode.is_sp,
                "is_hard": self.mode.is_hard,
                "parameter_type": int(self.mode.parameter_type),
            },
            "deck_entries": [value.to_dict() for value in self.deck_entries],
            "catalog_refs": [list(value) for value in self.catalog_refs],
            "runtime_adapter_ids": list(self.runtime_adapter_ids),
            "random_state_before": self.random_state_before,
            "random_state_after": self.random_state_after,
            "audit_flags": [value.to_dict() for value in self.audit_flags],
            "blockers": [value.to_dict() for value in self.blockers],
        }


def _result(
    *,
    state: Plan2NativeHorizonState | None,
    blockers: list[Plan2NativeBlocker],
    flags: list[Plan2NativeStageAuditFlag],
    mode: Plan2ExamMode | None,
    deck_entries: tuple[Plan2NativeStageDeckAudit, ...],
    catalog_refs: tuple[tuple[str, int], ...],
    runtime: Plan2NativeStageRuntime,
    chance: Plan2NativeStageChance,
    random_state_after: int | None = None,
) -> Plan2NativeStageBootstrap:
    return Plan2NativeStageBootstrap(
        schema_version=PLAN2_NATIVE_STAGE_BOOTSTRAP_SCHEMA_VERSION,
        state=state,
        blockers=tuple(blockers),
        audit_flags=tuple(flags),
        mode=mode,
        deck_entries=deck_entries,
        catalog_refs=catalog_refs,
        runtime_adapter_ids=runtime.runtime_adapter_ids,
        random_state_before=chance.random_state,
        random_state_after=random_state_after,
        declared_authority=chance.authority,
        guid_authoritative=(
            chance.authority is Plan2NativeStageAuthority.AUTHORITATIVE
            and bool(deck_entries)
            and all(
                value.guid_source == "outer-state"
                and len(value.resolved_instance_ids) == value.count
                for value in deck_entries
            )
        ),
    )


def _mode_and_scoring(
    facts: Plan2NativeStageFacts,
    blockers: list[Plan2NativeBlocker],
) -> tuple[Plan2ExamMode | None, Plan2ExamSaveBattleScoringContext | None]:
    if facts.plan_type not in PLAN2_NATIVE_STAGE_PLAN_TYPES:
        _block(blockers, "stage-plan-type-unsupported", facts.plan_type)
    try:
        mode = resolve_plan2_exam_mode(
            facts.exam_type,
            facts.step_type_value,
        )
    except Plan2ExamModeError as error:
        _block(blockers, error.code, error.detail)
        return None, None
    if (
        mode.is_battle
        and facts.step_type_value not in PLAN2_NATIVE_STAGE_AUDITION_STEPS
    ):
        _block(
            blockers,
            "stage-audition-step-unsupported",
            str(facts.step_type_value),
        )
    if mode.is_lesson and any(facts.initial_parameter_values):
        _block(
            blockers,
            "lesson-initial-parameter-split-not-empty",
        )
    if mode.is_battle and sum(facts.initial_parameter_values) != facts.initial_score:
        _block(
            blockers,
            "battle-initial-parameter-total-mismatch",
            f"score={facts.initial_score};split={sum(facts.initial_parameter_values)}",
        )
    try:
        scoring = Plan2ExamSaveBattleScoringContext(
            schema_version=PLAN2_EXAM_SAVE_BATTLE_SCORING_SCHEMA_VERSION,
            current_turn=1,
            # The scoring boundary advances on every usable horizon turn.
            # Lessons retain an empty parameter schedule, while auditions
            # must explicitly provide a schedule covering this full total.
            limit_turn=facts.limit_turn + facts.extra_turn,
            turn_parameter_types=facts.turn_parameter_types,
            vocal_bonus_permille=facts.battle_bonus_permille[0],
            dance_bonus_permille=facts.battle_bonus_permille[1],
            visual_bonus_permille=facts.battle_bonus_permille[2],
            judge_parameter=facts.initial_score,
            limit_border=facts.limit_border,
            clear_border=facts.clear_border,
            current_turn_total_add_parameter=(
                facts.current_turn_total_add_parameter
            ),
            judge_parameter_vocal=facts.initial_parameter_values[0],
            judge_parameter_dance=facts.initial_parameter_values[1],
            judge_parameter_visual=facts.initial_parameter_values[2],
            exam_mode=mode,
        )
    except Plan2BattleScoringError as error:
        _block(blockers, error.code, error.detail)
        return mode, None
    return mode, scoring


def _gimmicks(
    facts: Plan2NativeStageFacts,
    hooks: Mapping[Plan2ScheduledGimmickHookKey, Plan2ScheduledGimmickHook]
    | None,
    blockers: list[Plan2NativeBlocker],
) -> tuple[Plan2ScheduledGimmick, ...]:
    try:
        return compile_plan2_scheduled_gimmicks(
            [value.to_serialized_shape() for value in facts.gimmicks],
            hooks=hooks,
            maximum_turn=facts.limit_turn + facts.extra_turn,
            completed_through_turn=0,
        )
    except Plan2ExamModeError as error:
        _block(blockers, error.code, error.detail)
        return ()


def _runtime_blockers(
    rollout: ProduceRolloutState,
    runtime: Plan2NativeStageRuntime,
    blockers: list[Plan2NativeBlocker],
) -> None:
    scalar = runtime.scalar
    conflicts: list[str] = []
    if scalar.current_turn != 1:
        conflicts.append("current_turn")
    if scalar.score != 0:
        conflicts.append("score")
    if scalar.stamina != 0 or scalar.max_stamina != 0:
        conflicts.append("stamina/max_stamina")
    if scalar.exam_card_play_count != 0 or scalar.turn_card_play_count != 0:
        conflicts.append("card_play_counts")
    if scalar.battle_scoring is not None:
        conflicts.append("battle_scoring")
    if conflicts:
        _block(
            blockers,
            "stage-runtime-owned-scalar-conflict",
            ",".join(conflicts),
        )

    for kind, expected, resolved in (
        (
            "item",
            rollout.item_session_refs,
            runtime.resolved_item_session_refs,
        ),
        (
            "passive",
            rollout.passive_session_refs,
            runtime.resolved_passive_session_refs,
        ),
    ):
        missing = tuple(sorted(set(expected) - set(resolved)))
        extra = tuple(sorted(set(resolved) - set(expected)))
        if missing or extra:
            _block(
                blockers,
                "stage-runtime-session-ref-mismatch",
                f"{kind}:missing={missing!r};extra={extra!r}",
            )
    expected_drinks = tuple(rollout.drink_session_refs)
    resolved_drinks = tuple(runtime.resolved_drink_session_refs)
    missing_drinks = tuple(
        sorted(set(expected_drinks) - set(resolved_drinks))
    )
    extra_drinks = tuple(
        sorted(set(resolved_drinks) - set(expected_drinks))
    )
    if missing_drinks or extra_drinks:
        _block(
            blockers,
            "stage-runtime-session-ref-mismatch",
            f"drink:missing={missing_drinks!r};extra={extra_drinks!r}",
        )
    elif expected_drinks != resolved_drinks:
        _block(
            blockers,
            "stage-drink-runtime-order-mismatch",
            f"outer={expected_drinks!r};resolved={resolved_drinks!r}",
        )
    if runtime.drink_runtime.session_refs != resolved_drinks:
        _block(
            blockers,
            "stage-drink-runtime-unavailable",
            f"resolved={resolved_drinks!r};"
            f"inventory={runtime.drink_runtime.session_refs!r}",
        )
    if (
        runtime.resolved_item_session_refs
        or runtime.resolved_drink_session_refs
        or runtime.resolved_passive_session_refs
    ) and not runtime.runtime_adapter_ids:
        _block(blockers, "stage-runtime-adapter-id-missing")
    if runtime.review_dynamic.review != runtime.scalar.review:
        _block(
            blockers,
            "stage-runtime-review-snapshot-mismatch",
            f"scalar={runtime.scalar.review};"
            f"runtime={runtime.review_dynamic.review}",
        )
    for value in runtime.unresolved_runtime_refs:
        _block(blockers, "stage-runtime-unresolved", value)
    if runtime.opaque_status_queue:
        _block(
            blockers,
            "stage-runtime-opaque-status-unresolved",
            ",".join(runtime.opaque_status_queue),
        )


def _expand_deck(
    facts: Plan2NativeStageFacts,
    deck: tuple[DeckEntry, ...],
    chance: Plan2NativeStageChance,
    blockers: list[Plan2NativeBlocker],
    flags: list[Plan2NativeStageAuditFlag],
) -> tuple[
    tuple[tuple[str, str, int], ...],
    tuple[Plan2NativeStageDeckAudit, ...],
]:
    if not deck:
        _block(blockers, "stage-card-universe-empty")
    if chance.authority is Plan2NativeStageAuthority.DETERMINISTIC_FIXTURE:
        flags.append(
            Plan2NativeStageAuditFlag(
                "non-authoritative-fixture-stage",
                facts.stage_id,
            )
        )
    flat: list[tuple[str, str, int]] = []
    audits: list[Plan2NativeStageDeckAudit] = []
    offset = 0
    generated = 0
    for entry_index, entry in enumerate(deck):
        if entry.upgrade > 3:
            _block(
                blockers,
                "stage-card-upgrade-unsupported",
                f"{entry.card_id}@{entry.upgrade}",
            )
        if entry.instance_ids:
            resolved_ids = tuple(entry.instance_ids)
            source = "outer-state"
        elif chance.authority is Plan2NativeStageAuthority.AUTHORITATIVE:
            resolved_ids = ()
            source = "missing"
            _block(
                blockers,
                "stage-card-instance-ids-missing",
                f"entry={entry_index};card={entry.card_id};count={entry.count}",
            )
        else:
            resolved_ids = tuple(
                f"{chance.fixture_guid_namespace}:{facts.stage_id}:"
                f"{offset + index:04d}:{entry.card_id}"
                for index in range(entry.count)
            )
            source = "deterministic-fixture-policy"
            generated += len(resolved_ids)
        audits.append(
            Plan2NativeStageDeckAudit(
                entry_index=entry_index,
                card_id=entry.card_id,
                upgrade=entry.upgrade,
                count=entry.count,
                instance_ids=tuple(entry.instance_ids),
                resolved_instance_ids=resolved_ids,
                guid_source=source,
            )
        )
        flat.extend(
            (guid, entry.card_id, entry.upgrade) for guid in resolved_ids
        )
        offset += entry.count
    guids = tuple(value[0] for value in flat)
    if len(guids) != len(set(guids)):
        _block(blockers, "stage-card-instance-id-duplicate")
    if generated:
        flags.append(
            Plan2NativeStageAuditFlag(
                "deterministic-fixture-guids-generated",
                str(generated),
            )
        )
    return tuple(flat), tuple(audits)


def bootstrap_plan2_native_stage(
    facts: Plan2NativeStageFacts,
    rollout: ProduceRolloutState,
    *,
    chance: Plan2NativeStageChance,
    catalog: Plan2NativeProgramCatalog,
    runtime: Plan2NativeStageRuntime = Plan2NativeStageRuntime(),
    gimmick_hooks: Mapping[
        Plan2ScheduledGimmickHookKey,
        Plan2ScheduledGimmickHook,
    ]
    | None = None,
) -> Plan2NativeStageBootstrap:
    """Build a deterministic solver root from explicit, already-known facts."""

    if not isinstance(facts, Plan2NativeStageFacts):
        raise TypeError("facts must be Plan2NativeStageFacts")
    if not isinstance(rollout, ProduceRolloutState):
        raise TypeError("rollout must be ProduceRolloutState")
    if not isinstance(chance, Plan2NativeStageChance):
        raise TypeError("chance must be Plan2NativeStageChance")
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be Plan2NativeProgramCatalog")
    if not isinstance(runtime, Plan2NativeStageRuntime):
        raise TypeError("runtime must be Plan2NativeStageRuntime")

    blockers: list[Plan2NativeBlocker] = []
    flags: list[Plan2NativeStageAuditFlag] = []
    mode, scoring = _mode_and_scoring(facts, blockers)
    scheduled_gimmicks = _gimmicks(facts, gimmick_hooks, blockers)
    _runtime_blockers(rollout, runtime, blockers)
    if chance.binding is None:
        _block(blockers, "stage-zone-binding-missing")
    elif not isinstance(chance.binding, NativeOrderedZoneEvidenceBinding):
        _block(blockers, "stage-zone-binding-invalid")
    if facts.draw_count > facts.hand_limit:
        _block(
            blockers,
            "stage-draw-exceeds-hand-limit",
            f"draw={facts.draw_count};hand_limit={facts.hand_limit}",
        )

    flat, deck_audit = _expand_deck(
        facts,
        tuple(rollout.deck),
        chance,
        blockers,
        flags,
    )
    catalog_refs = tuple(
        sorted({(entry.card_id, entry.upgrade) for entry in rollout.deck})
    )
    catalog_program_refs = {value.ref for value in catalog.programs}
    for card_id, upgrade in catalog_refs:
        if (card_id, upgrade) not in catalog_program_refs:
            _block(
                blockers,
                "stage-card-program-missing",
                f"{card_id}@{upgrade}",
            )
    if facts.draw_count > len(flat):
        _block(
            blockers,
            "stage-initial-draw-exceeds-deck",
            f"draw={facts.draw_count};deck={len(flat)}",
        )
    fixed_orders = chance.fixed_deck_orders or (0,) * len(flat)
    if len(fixed_orders) != len(flat):
        _block(
            blockers,
            "stage-fixed-deck-order-count-mismatch",
            f"orders={len(fixed_orders)};deck={len(flat)}",
        )

    if blockers or mode is None or scoring is None or chance.binding is None:
        return _result(
            state=None,
            blockers=blockers,
            flags=flags,
            mode=mode,
            deck_entries=deck_audit,
            catalog_refs=catalog_refs,
            runtime=runtime,
            chance=chance,
        )

    cards = tuple(
        LocalSaveExamCard(
            zone_order=index,
            guid=guid,
            card_id=card_id,
            base_upgrade=upgrade,
            temporary_upgrade=0,
            effective_upgrade=upgrade,
            support_upgrade_ids=(),
            fixed_deck_order=fixed_orders[index],
            runtime_state=empty_local_save_exam_card_runtime_state(),
        )
        for index, (guid, card_id, upgrade) in enumerate(flat)
    )
    try:
        shuffled, random_state_after = shuffle_unfixed_pool(
            cards,
            chance.random_state,
            fixed_deck_orders=fixed_orders,
        )
        shuffled_cards = tuple(
            replace(card, zone_order=index)
            for index, card in enumerate(shuffled)
        )
        shuffled_instances = tuple(
            NativeOrderedCardInstance.from_local_save(card)
            for card in shuffled_cards
        )
        zones = NativeOrderedZoneState(
            schema_version=NATIVE_ORDERED_ZONE_SCHEMA_VERSION,
            binding=chance.binding,
            random_state=random_state_after,
            card_universe=tuple(
                sorted(shuffled_instances, key=lambda value: value.guid)
            ),
            hand=(),
            deck=shuffled_instances,
            grave=(),
            lost=(),
        ).draw_to_hand(facts.draw_count).state
    except FixedDeckOrderError as error:
        _block(blockers, "fixed-order-shuffle-unsupported", str(error))
        return _result(
            state=None,
            blockers=blockers,
            flags=flags,
            mode=mode,
            deck_entries=deck_audit,
            catalog_refs=catalog_refs,
            runtime=runtime,
            chance=chance,
        )
    except (NativeOrderedZoneError, TypeError, ValueError) as error:
        code = (
            error.code
            if isinstance(error, NativeOrderedZoneError)
            else "stage-zone-bootstrap-invalid"
        )
        _block(blockers, code, str(error))
        return _result(
            state=None,
            blockers=blockers,
            flags=flags,
            mode=mode,
            deck_entries=deck_audit,
            catalog_refs=catalog_refs,
            runtime=runtime,
            chance=chance,
        )

    scalar = replace(
        runtime.scalar,
        current_turn=1,
        score=facts.initial_score,
        stamina=rollout.stamina,
        max_stamina=rollout.max_stamina,
        exam_card_play_count=0,
        turn_card_play_count=0,
        battle_scoring=scoring,
    )
    try:
        state = Plan2NativeHorizonState(
            schema_version=PLAN2_NATIVE_HORIZON_SCHEMA_VERSION,
            scalar=scalar,
            zones=zones,
            limit_turn=facts.limit_turn,
            stamina_modifiers=runtime.stamina_modifiers,
            status_enchant=runtime.status_enchant,
            review_dynamic=runtime.review_dynamic,
            debuff_registry=runtime.debuff_registry,
            effect_chains=runtime.effect_chains,
            generated_runtime=runtime.generated_runtime,
            generated_inputs=runtime.generated_inputs,
            status_child_inputs=runtime.status_child_inputs,
            status_child_review_state=runtime.status_child_review_state,
            total_effect_draw_card_count=(
                runtime.total_effect_draw_card_count
            ),
            generated_hand_move_turn=runtime.generated_hand_move_turn,
            encore_runtime=runtime.encore_runtime,
            aggressive_additive_runtime=runtime.aggressive_additive_runtime,
            block_fix_restriction_runtime=(
                runtime.block_fix_restriction_runtime
            ),
            extra_turn_runtime=runtime.extra_turn_runtime,
            stamina_recover_restricted=runtime.stamina_recover_restricted,
            stamina_recover_add_permil=runtime.stamina_recover_add_permil,
            search_play_card_stamina_runtime=(
                runtime.search_play_card_stamina_runtime
            ),
            add_grow_runtime=runtime.add_grow_runtime,
            item_runtime=runtime.item_runtime,
            drink_runtime=runtime.drink_runtime,
            start_play_listeners=runtime.start_play_listeners,
            triggered_review_status_runtime=(
                runtime.triggered_review_status_runtime
            ),
            review_consumption_sum=runtime.review_consumption_sum,
            block_consumption_sum_count=runtime.block_consumption_sum_count,
            judge_parameter=facts.initial_score,
            clear_border=facts.clear_border,
            extra_turn=facts.extra_turn,
            draw_count=facts.draw_count,
            hand_limit=facts.hand_limit,
            plays_remaining=facts.plays_remaining,
            phase=PHASE_MAIN,
            timers=runtime.timers,
            command_queue=runtime.command_queue,
            opaque_status_queue=(),
            source_kind=(
                f"plan2-stage:{facts.stage_id}:"
                f"{chance.authority.value}"
            ),
            exam_mode=mode,
            scheduled_gimmicks=scheduled_gimmicks,
        )
    except Plan2NativeHorizonError as error:
        _block(blockers, error.blocker.code, error.blocker.detail)
        state = None
    except (TypeError, ValueError) as error:
        _block(
            blockers,
            "stage-runtime-projection-invalid",
            f"{type(error).__name__}:{error}",
        )
        state = None

    return _result(
        state=state,
        blockers=blockers,
        flags=flags,
        mode=mode,
        deck_entries=deck_audit,
        catalog_refs=catalog_refs,
        runtime=runtime,
        chance=chance,
        random_state_after=(
            random_state_after if state is not None else None
        ),
    )


# Stable adapter-friendly alias; no existing hardcoded Master-deck bootstrap
# participates in this path.
bootstrap_plan2_native_horizon_from_stage = bootstrap_plan2_native_stage


__all__ = [
    "PLAN2_NATIVE_STAGE_AUDITION_STEPS",
    "PLAN2_NATIVE_STAGE_BOOTSTRAP_SCHEMA_VERSION",
    "PLAN2_NATIVE_STAGE_PLAN_TYPES",
    "Plan2NativeStageAuditFlag",
    "Plan2NativeStageAuthority",
    "Plan2NativeStageBootstrap",
    "Plan2NativeStageChance",
    "Plan2NativeStageDeckAudit",
    "Plan2NativeStageFacts",
    "Plan2NativeStageGimmickFact",
    "Plan2NativeStageRuntime",
    "InitialRegularLessonStageFactLike",
    "bootstrap_plan2_native_horizon_from_stage",
    "bootstrap_plan2_native_stage",
    "plan2_stage_facts_from_initial_regular_lesson",
]
