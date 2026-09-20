"""Deterministic Plan 3 search with GUID zones and native RNG in every node."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from itertools import combinations, product
from pathlib import Path
from typing import Callable, Protocol, TypeAlias, runtime_checkable

from .audition_native_support import NativeHandAddSupportError
from .audition_support_runtime import SupportUpgradeRuntimeInput
from .card_play_count_receipt import (
    CARD_PLAY_COUNT_INCREMENT_OPERATION,
    CARD_PLAY_COUNT_RECEIPT_FAMILY,
    PLAN3_ORDINARY_CARD_PLAY_COUNT_BUILD_OWNER,
    PLAN3_ORDINARY_CARD_PLAY_COUNT_MOVE_OWNER,
    PLAN3_ORDINARY_CARD_PLAY_COUNT_RECEIPT_OWNERS,
    CardPlayCountOperationReceipt,
)
from .card_search import ProduceCardSearchRule, load_produce_card_search
from .enthusiastic_runtime import EnthusiasticReceipt
from .playable_value_add_runtime import (
    PlayableValueAddOperation,
    PlayableValueAddReceipt,
    add_playable_value_add,
    use_playable_value_add,
)
from .logic_engine import MasterCard, load_master_card
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    StaminaGrowEffect,
    StaminaGrowEffectType,
    get_stamina_cost,
)
from .nia_add_grow_effect import (
    NiaAddGrowContractError,
    NiaAddGrowMutation,
    NiaDeckAllState,
    Plan3AddGrowCardProfile,
    execute_plan3_add_grow_program,
    load_plan3_add_grow_program,
)
from .plan3_drink import (
    Plan3DrinkInventory,
    Plan3DrinkSelectedCardMove,
)
from .plan3_drink_adapter import (
    Plan3DrinkApplication,
    Plan3DrinkApplicationError,
    apply_plan3_drink,
)
from .plan3_engine import (
    CATEGORY_TROUBLE,
    COST_STAMINA,
    EFFECT_ADD_GROW,
    EFFECT_ANTI_DEBUFF,
    EFFECT_BLOCK,
    EFFECT_CARD_CREATE_ID,
    EFFECT_CARD_CREATE_SEARCH,
    EFFECT_CARD_UPGRADE,
    EFFECT_CARD_DRAW,
    EFFECT_CARD_MOVE,
    EFFECT_CARD_SEARCH_PLAY_COUNT_BUFF,
    EFFECT_FORCE_PLAY_CARD_SEARCH,
    EFFECT_FORCE_PLAY_CARD_SEARCH_WITH_COST,
    EFFECT_FULL_POWER_POINT,
    EFFECT_HAND_GRAVE_COUNT_CARD_DRAW,
    EFFECT_SEARCH_PLAY_CARD_STAMINA_CHANGE,
    EFFECT_STATUS_ENCHANT,
    EFFECT_STATUS_ENCHANT_ENCORE,
    EFFECT_TIMER,
    EXACT_ENCORE_CARD_ID,
    EXACT_ENCORE_STATUS_ID,
    EFFECT_LESSON,
    EFFECT_LESSON_FULL_POWER_POINT,
    EFFECT_PLAYABLE_VALUE_ADD,
    EFFECT_STAMINA_CONSUMPTION_DOWN,
    PHASE_STATUS_CHANGE,
    LESSON_DANCE,
    LESSON_VISUAL,
    LESSON_VOCAL,
    MOVE_GRAVE,
    MOVE_HOLD,
    MOVE_LOST,
    PICK_RANGE_ALL,
    PICK_RANGE_RANDOM,
    PICK_RANGE_SELECT,
    PLAN3,
    SEARCH_PLAYING_SELF,
    SEARCH_TROUBLE_NOT_LOST,
    STANCE_FULL_POWER,
    ActivePlan3StatusEnchant,
    Plan3Card,
    Plan3CardRef,
    Plan3Effect,
    Plan3ExamSettings,
    Plan3ExternalTurnStartGimmickResolution,
    Plan3GimmickProfile,
    Plan3PreDirectTransformResult,
    Plan3RuntimeEffectEvent,
    Plan3State,
    Plan3Transition,
    Plan3TurnStart,
    allocate_plan3_status_uid,
    apply_plan3_card,
    end_plan3_turn,
    evaluate_plan3_trigger,
    load_plan3_card,
    load_plan3_exam_settings,
    start_plan3_turn,
)
from .plan3_native_state import (
    Plan3NativeCard,
    Plan3NativeCardGrowStatus,
    Plan3NativeCardMoveTarget,
    Plan3NativeDrawTransition,
    Plan3NativeState,
    Plan3NativeStateError,
)
from .plan3_card_search_effect_play_count_buff import (
    CardPlayCountBuffHookInput,
    execute_card_search_effect_play_count_buff,
)
from .plan3_force_play_card_search import (
    DECK_GRAVE_SELECT_EFFECT_ID,
    HOLD_ALL_EFFECT_ID,
    ForcePlayExecutionInput,
    contract_for_effect as force_play_contract_for_effect,
    plan_force_play_card_search,
)
from .plan3_force_play_card_search_with_cost import (
    ForcePlayWithCostExecutionInput,
    load_force_play_with_cost_contract,
    plan_force_play_card_search_with_cost,
)
from .plan3_use_pool import (
    Plan3UsePoolCommand,
    Plan3UsePoolStage,
    stage_plan3_use_pool_guid,
)
from .plan3_hand_grave_draw import (
    HandGraveDrawTransition,
    execute_hand_grave_draw,
    load_hand_grave_draw,
)
from .plan3_card_upgrade import (
    Plan3CardUpgradeBranch,
    Plan3CardUpgradeResult,
    execute_plan3_card_upgrade,
    resolve_plan3_card_upgrade,
    resolve_plan3_card_upgrade_candidates,
)
from .plan3_card_create_id import (
    CardCreateContract,
    CardCreateError,
    CardCreateInputError,
    CardCreateResolutionError,
    CardCreateResult,
    apply_card_create_id,
    load_master_card_create_id_rows,
    resolve_card_create_contract,
)
from .plan3_card_create_search import (
    CardCreateSearchChanceInput,
    CardCreateSearchEffectContract,
    CardCreateSearchError,
    CardCreateSearchInputError,
    CardCreateSearchResult,
    apply_card_create_search,
    load_master_card_create_search_rows,
    resolve_card_create_search_contract,
)
from .plan3_card_play_trigger import (
    EXECUTABLE_SHAPES as EXECUTABLE_CARD_PLAY_TRIGGER_SHAPES,
    PHASE_CARD_PLAY as NATIVE_PHASE_CARD_PLAY,
    evaluate_plan3_card_play_trigger,
    resolve_plan3_card_play_trigger,
)
from .plan3_card_play_after_trigger import (
    CARD_ID_IDO_3_110,
    CARD_UPGRADES_IDO_3_110,
    PHASE_CARD_PLAY_AFTER as NATIVE_PHASE_CARD_PLAY_AFTER,
    TRIGGER_ID_CARD_PLAY_AFTER_EFFECT_GROUP,
    evaluate_plan3_card_play_after_trigger,
    resolve_plan3_card_play_after_trigger,
)
from .plan3_search import (
    Objective,
    Plan3SearchEvaluation,
    evaluate_plan3_search_state,
)
from .plan3_search_stamina_change import (
    Plan3SearchStaminaApplication,
    Plan3SearchStaminaPayment,
    apply_plan3_search_stamina_change,
    resolve_plan3_search_stamina_change,
    resolve_plan3_search_stamina_payment,
)
from .plan3_anti_debuff import (
    ANTI_DEBUFF_CATALOG,
    AntiDebuffExecution,
    ExecutionStatus as AntiDebuffExecutionStatus,
    execute_anti_debuff,
)
from .plan3_trigger_none_field import (
    DISPATCH_PHASE_CARD_PLAY_EFFECT,
    DISPATCH_SOURCE_CARD_DIRECT_EFFECT,
    TRIGGER_ID as TRIGGER_NONE_NOT_PRESERVATION_UP,
    evaluate_plan3_trigger_none_field,
)


SupportInputs: TypeAlias = (
    Mapping[str, SupportUpgradeRuntimeInput]
    | Iterable[SupportUpgradeRuntimeInput]
)

Plan3StatusOperationReceipt: TypeAlias = (
    AntiDebuffExecution | PlayableValueAddReceipt | EnthusiasticReceipt
)


@dataclass(frozen=True, slots=True)
class Plan3NativeGuidRequest:
    """Path-stable identity request for one lazily-created native card."""

    path_token: tuple[str, ...]
    source_guid: str
    source_play_count: int
    effect_id: str
    effect_sequence_index: int
    created_ordinal: int

    def __post_init__(self) -> None:
        token = tuple(self.path_token)
        if any(not isinstance(value, str) or not value for value in token):
            raise ValueError("path_token must contain non-empty text")
        object.__setattr__(self, "path_token", token)
        for name in ("source_guid", "effect_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be non-empty text")
        for name in (
            "source_play_count",
            "effect_sequence_index",
            "created_ordinal",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@runtime_checkable
class Plan3NativeGuidProvider(Protocol):
    """Caller-owned adapter for native lazy ``Guid.NewGuid`` identity."""

    def allocate(self, *, request: Plan3NativeGuidRequest) -> str:
        ...


@dataclass(frozen=True, slots=True)
class Plan3OpaqueSymbolicGuidProvider:
    """Deterministic offline tokens; never interprets UUID bits as RNG."""

    namespace: str

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, str) or not self.namespace:
            raise ValueError("symbolic GUID namespace must be non-empty text")

    def allocate(self, *, request: Plan3NativeGuidRequest) -> str:
        if not isinstance(request, Plan3NativeGuidRequest):
            raise TypeError("request must be Plan3NativeGuidRequest")

        def opaque(value: object) -> str:
            text = str(value)
            return f"{len(text)}:{text}"

        fields: tuple[object, ...] = (
            self.namespace,
            *request.path_token,
            request.source_guid,
            request.source_play_count,
            request.effect_id,
            request.effect_sequence_index,
            request.created_ordinal,
        )
        return "plan3-symbolic-guid|" + "|".join(opaque(value) for value in fields)


NativeGuidProviderInput: TypeAlias = (
    Plan3NativeGuidProvider | Callable[[Plan3NativeGuidRequest], str]
)


class Plan3NativeAcceptedPlaySource(str, Enum):
    """Native entry point that accepted the card before phase-23 dispatch."""

    ORDINARY = "ordinary"
    FORCED = "forced"
    EXTRA = "extra"


@dataclass(frozen=True, slots=True)
class Plan3NativeAcceptedPlayExtensionResult:
    """Mode-specific phase-23 result at the pre-direct native boundary.

    Scalar card effects are already compiled from the pre-listener playing
    card.  ``native_state`` therefore carries listener-side DeckAll changes
    without asking the shared kernel to compile the current card again.
    """

    active_status_enchants: tuple[ActivePlan3StatusEnchant, ...]
    native_state: Plan3NativeState
    extension_state: object | None
    handled_play_count_interval_instance_ids: tuple[str, ...] = ()
    fired_status_enchant_ids: tuple[str, ...] = ()
    fired_effect_ids: tuple[str, ...] = ()
    add_grow_mutations: tuple[NiaAddGrowMutation, ...] = ()
    unsupported_rules: tuple[str, ...] = ()
    trace: object | None = None
    scalar_state: Plan3State | None = None
    parameter_pre_fix_values: tuple[int, ...] = ()
    parameter_actual_values: tuple[int, ...] = ()
    parameter_clear_changed: bool = False
    parameter_perfect_changed: bool = False

    def __post_init__(self) -> None:
        active = tuple(self.active_status_enchants)
        if not all(isinstance(item, ActivePlan3StatusEnchant) for item in active):
            raise TypeError(
                "active_status_enchants must contain ActivePlan3StatusEnchant values"
            )
        object.__setattr__(self, "active_status_enchants", active)
        if not isinstance(self.native_state, Plan3NativeState):
            raise TypeError("native_state must be Plan3NativeState")
        if self.scalar_state is not None and not isinstance(
            self.scalar_state, Plan3State
        ):
            raise TypeError("scalar_state must be Plan3State or None")
        for values, label in (
            (self.parameter_pre_fix_values, "parameter_pre_fix_values"),
            (self.parameter_actual_values, "parameter_actual_values"),
        ):
            normalized = tuple(values)
            if any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in normalized
            ):
                raise TypeError(f"{label} must contain integers")
            object.__setattr__(self, label, normalized)
        if type(self.parameter_clear_changed) is not bool:
            raise TypeError("parameter_clear_changed must be boolean")
        if type(self.parameter_perfect_changed) is not bool:
            raise TypeError("parameter_perfect_changed must be boolean")
        handled = tuple(self.handled_play_count_interval_instance_ids)
        if (
            any(not isinstance(value, str) or not value for value in handled)
            or len(handled) != len(set(handled))
        ):
            raise TypeError(
                "handled play-count interval IDs must be unique non-empty text"
            )
        object.__setattr__(
            self, "handled_play_count_interval_instance_ids", handled
        )
        for values, label in (
            (self.fired_status_enchant_ids, "fired_status_enchant_ids"),
            (self.fired_effect_ids, "fired_effect_ids"),
            (self.unsupported_rules, "unsupported_rules"),
        ):
            if any(not isinstance(value, str) or not value for value in values):
                raise TypeError(f"{label} must contain non-empty text")
        if not all(
            isinstance(value, NiaAddGrowMutation)
            for value in self.add_grow_mutations
        ):
            raise TypeError(
                "add_grow_mutations must contain NiaAddGrowMutation values"
            )
        if self.extension_state is not None:
            try:
                hash(self.extension_state)
            except TypeError as error:
                raise TypeError("extension_state must be hashable") from error


@runtime_checkable
class Plan3NativeAcceptedPlayExtension(Protocol):
    """Pure mode adapter for one legal native card transaction.

    ``managed_play_count_interval_instance_ids`` is queried before scalar
    execution so the shared phase-23 dispatcher can delegate those exact
    listener instances.  The callable runs through the engine's generic
    pre-direct transform after payment and ordinary ExamCardPlay, before the
    card's ordered direct effects execute.
    """

    def managed_play_count_interval_instance_ids(
        self,
        scalar_state: Plan3State,
        extension_state: object | None,
    ) -> tuple[str, ...]:
        ...

    def __call__(
        self,
        scalar_before: Plan3State,
        scalar_after: Plan3State,
        native_state: Plan3NativeState,
        playing_guid: str,
        compiled_card: Plan3Card,
        extension_state: object | None,
        settings: Plan3ExamSettings,
        source: Plan3NativeAcceptedPlaySource,
    ) -> Plan3NativeAcceptedPlayExtensionResult:
        ...


@dataclass(frozen=True, slots=True)
class _Plan3AcceptedPreDirectCapture:
    execution: Plan3NativeAcceptedPlayExtensionResult
    native_state: Plan3NativeState
    runtime_add_grow_mutations: tuple[NiaAddGrowMutation, ...] = ()


def _accepted_pre_direct_transform(
    *,
    scalar_before: Plan3State,
    native_state: Plan3NativeState,
    playing_guid: str,
    compiled_card: Plan3Card,
    extension: Plan3NativeAcceptedPlayExtension,
    extension_state: object | None,
    settings: Plan3ExamSettings,
    source: Plan3NativeAcceptedPlaySource,
    managed_interval_ids: tuple[str, ...],
    database: Path,
    card_profile_by_card: Callable[
        [Plan3NativeCard], Plan3AddGrowCardProfile
    ],
) -> tuple[
    Callable[
        [Plan3State, tuple[Plan3RuntimeEffectEvent, ...]],
        Plan3PreDirectTransformResult,
    ],
    list[_Plan3AcceptedPreDirectCapture],
]:
    """Bind the native accepted-play extension to the engine boundary."""

    captures: list[_Plan3AcceptedPreDirectCapture] = []

    def transform(
        scalar_boundary: Plan3State,
        runtime_events: tuple[Plan3RuntimeEffectEvent, ...],
    ) -> Plan3PreDirectTransformResult:
        if captures:
            return Plan3PreDirectTransformResult(
                scalar_boundary,
                ("accepted-play-pre-direct-transform-reentered",),
            )
        native_working = native_state
        runtime_mutations: tuple[NiaAddGrowMutation, ...] = ()
        for event in runtime_events:
            if (
                event.effect_type != EFFECT_ADD_GROW
                or event.phase != NATIVE_PHASE_CARD_PLAY
            ):
                continue
            native_working, mutations = apply_plan3_native_add_grow_effects(
                native_working,
                (event.effect,),
                database=database,
                playing_guid=playing_guid,
                card_profile_by_card=card_profile_by_card,
            )
            runtime_mutations = (*runtime_mutations, *mutations)

        execution = extension(
            scalar_before,
            scalar_boundary,
            native_working,
            playing_guid,
            compiled_card,
            extension_state,
            settings,
            source,
        )
        if not isinstance(execution, Plan3NativeAcceptedPlayExtensionResult):
            raise TypeError("accepted-play-extension-result-type")
        if execution.unsupported_rules:
            return Plan3PreDirectTransformResult(
                scalar_boundary,
                tuple(execution.unsupported_rules),
            )
        if (
            execution.handled_play_count_interval_instance_ids
            != managed_interval_ids
        ):
            return Plan3PreDirectTransformResult(
                scalar_boundary,
                ("accepted-play-extension-managed-listener-drift",),
            )

        before_active = scalar_boundary.active_status_enchants
        after_active = execution.active_status_enchants
        if tuple(item.instance_id for item in before_active) != tuple(
            item.instance_id for item in after_active
        ):
            return Plan3PreDirectTransformResult(
                scalar_boundary,
                ("accepted-play-extension-active-order-drift",),
            )
        managed_set = set(managed_interval_ids)
        for before_enchant, after_enchant in zip(
            before_active,
            after_active,
            strict=True,
        ):
            expected = (
                before_enchant
                if before_enchant.instance_id not in managed_set
                else replace(
                    after_enchant,
                    phase_counts=before_enchant.phase_counts,
                )
            )
            if expected != before_enchant:
                return Plan3PreDirectTransformResult(
                    scalar_boundary,
                    (
                        "accepted-play-extension-active-mutation-drift:"
                        f"{before_enchant.instance_id}",
                    ),
                )

        before_zone_guids = tuple(
            tuple(card.guid for card in zone)
            for zone in (
                native_working.hand,
                native_working.deck,
                native_working.grave,
                native_working.lost,
                native_working.hold,
            )
        )
        after_native = execution.native_state
        after_zone_guids = tuple(
            tuple(card.guid for card in zone)
            for zone in (
                after_native.hand,
                after_native.deck,
                after_native.grave,
                after_native.lost,
                after_native.hold,
            )
        )
        if after_zone_guids != before_zone_guids:
            return Plan3PreDirectTransformResult(
                scalar_boundary,
                ("accepted-play-extension-native-zone-drift",),
            )
        non_card_projection = replace(
            after_native,
            hand=native_working.hand,
            deck=native_working.deck,
            grave=native_working.grave,
            lost=native_working.lost,
            hold=native_working.hold,
        )
        if non_card_projection != native_working:
            return Plan3PreDirectTransformResult(
                scalar_boundary,
                ("accepted-play-extension-native-runtime-drift",),
            )
        if execution.scalar_state is None and (
            execution.parameter_pre_fix_values
            or execution.parameter_actual_values
            or execution.parameter_clear_changed
            or execution.parameter_perfect_changed
        ):
            return Plan3PreDirectTransformResult(
                scalar_boundary,
                ("accepted-play-extension-scalar-state-missing",),
            )
        scalar_after = replace(
            execution.scalar_state or scalar_boundary,
            active_status_enchants=after_active,
        )
        captures.append(
            _Plan3AcceptedPreDirectCapture(
                execution,
                after_native,
                runtime_mutations,
            )
        )
        return Plan3PreDirectTransformResult(
            scalar_after,
            parameter_pre_fix_values=(
                execution.parameter_pre_fix_values
            ),
            parameter_actual_values=execution.parameter_actual_values,
            parameter_clear_changed=execution.parameter_clear_changed,
            parameter_perfect_changed=(
                execution.parameter_perfect_changed
            ),
            fired_status_enchant_ids=(
                execution.fired_status_enchant_ids
            ),
            fired_effect_ids=execution.fired_effect_ids,
        )

    return transform, captures

_NATIVE_DESTINATION_BY_SCALAR_ZONE = {
    "discard": MOVE_GRAVE,
    "lost": MOVE_LOST,
    "hold": MOVE_HOLD,
}


@dataclass(frozen=True, order=True, slots=True)
class Plan3NativeSearchAction:
    guid: str
    card_ref: Plan3CardRef
    selected_card_guids: tuple[str, ...] = ()


# A path-level advisory is intentionally evaluated after the exact native
# objective in the search key.  Callers may use it for imitation ordering,
# but it cannot change legal expansion or a path's native evaluation.
Plan3NativePathTieBreaker: TypeAlias = Callable[["Plan3NativeSearchPath"], float]
PLAN3_MAX_PATH_TIE_BREAK = 1000.0


@dataclass(frozen=True, order=True, slots=True)
class Plan3NativeDrinkAction:
    slot_index: int
    drink_id: str
    selected_card_guid: str | None = None


@dataclass(frozen=True, slots=True)
class Plan3UsePoolTransaction:
    """One settled GUID command using the normal native card pipeline."""

    command: Plan3UsePoolCommand
    source_effect_id: str
    source_zone: str
    before: Plan3State
    after: Plan3State
    native_before: Plan3NativeState
    native_after: Plan3NativeState
    card_transition: Plan3Transition
    global_card_play_count_delta: int = 1
    history_event_count: int = 1
    final_move_count: int = 1
    playable_count_consumed: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.command, Plan3UsePoolCommand):
            raise TypeError("command must be Plan3UsePoolCommand")
        if not self.source_effect_id or not self.source_zone:
            raise ValueError("UsePool transaction source metadata is required")
        if not self.card_transition.supported or not self.card_transition.legal:
            raise ValueError("UsePool transaction must contain one legal card play")
        if (
            self.global_card_play_count_delta,
            self.history_event_count,
            self.final_move_count,
            self.playable_count_consumed,
        ) != (1, 1, 1, 0):
            raise ValueError("UsePool settles count/history/move exactly once")


@dataclass(frozen=True, slots=True)
class Plan3NativeSearchStep:
    kind: str
    before: Plan3State
    after: Plan3State
    native_before: Plan3NativeState
    native_after: Plan3NativeState
    action: Plan3NativeSearchAction | None = None
    card_transition: Plan3Transition | None = None
    turn_start: Plan3TurnStart | None = None
    native_draw: Plan3NativeDrawTransition | None = None
    runtime_draws: tuple["Plan3NativeRuntimeDrawTransition", ...] = ()
    add_grow_mutations: tuple[NiaAddGrowMutation, ...] = ()
    search_stamina_applications: tuple[
        Plan3SearchStaminaApplication, ...
    ] = ()
    search_stamina_payment: Plan3SearchStaminaPayment | None = None
    hand_grave_draws: tuple[HandGraveDrawTransition, ...] = ()
    card_upgrade_results: tuple[Plan3CardUpgradeResult, ...] = ()
    card_create_results: tuple[CardCreateResult, ...] = ()
    card_create_search_results: tuple[CardCreateSearchResult, ...] = ()
    anti_debuff_executions: tuple[AntiDebuffExecution, ...] = ()
    status_operation_receipts: tuple[Plan3StatusOperationReceipt, ...] = ()
    runtime_card_upgrades: tuple[
        "Plan3NativeRuntimeCardUpgradeTransition", ...
    ] = ()
    ended_state: Plan3State | None = None
    native_ended_state: Plan3NativeState | None = None
    drink_action: Plan3NativeDrinkAction | None = None
    drink_application: Plan3DrinkApplication | None = None
    forced_end_stamina_recovered: int = 0
    turn_start_extension_trace: object | None = None
    accepted_play_extension_trace: object | None = None
    encore_replays: tuple["Plan3EncoreForcedReplay", ...] = ()
    queued_use_pool_commands: tuple[Plan3UsePoolCommand, ...] = ()
    queued_use_pool_effect_ids: tuple[str, ...] = ()
    use_pool_transactions: tuple[Plan3UsePoolTransaction, ...] = ()
    settled_use_pool_effect_ids: tuple[str, ...] = ()
    move_effect_traces: tuple[object, ...] = ()
    global_card_play_count_delta: int = 0
    history_event_count: int = 0
    final_move_count: int = 0
    playable_count_consumed: int = 0
    operation_receipts: tuple[CardPlayCountOperationReceipt, ...] = ()

    def __post_init__(self) -> None:
        status_receipts = tuple(self.status_operation_receipts)
        if any(
            not isinstance(
                value,
                (
                    AntiDebuffExecution,
                    PlayableValueAddReceipt,
                    EnthusiasticReceipt,
                ),
            )
            for value in status_receipts
        ):
            raise TypeError(
                "status_operation_receipts must contain typed Plan3 "
                "status operations"
            )
        if self.kind not in {"card", "use_pool"} and status_receipts:
            raise ValueError(
                "status_operation_receipts belong only to card transactions"
            )
        anti_receipts = tuple(
            value
            for value in status_receipts
            if isinstance(value, AntiDebuffExecution)
        )
        if status_receipts and anti_receipts != tuple(
            self.anti_debuff_executions
        ):
            raise ValueError(
                "AntiDebuff status receipts must match anti_debuff_executions"
            )
        if (
            not status_receipts
            and self.after.status_uid_cursor_exact
            and self.anti_debuff_executions
        ):
            raise ValueError(
                "exact AntiDebuff step requires status operation receipts"
            )
        status_cursor = self.before.next_status_uid
        anti_runtime = self.before.anti_debuff_runtime
        playable_runtime = self.before.playable_value_add_runtime
        enthusiastic_runtime = self.before.enthusiastic_runtime
        before_listener_uids = {
            enchant.native_uid
            for enchant in self.before.active_status_enchants
            if enchant.native_uid > 0
        }
        intervening_listener_uids = list(
            sorted(
                enchant.native_uid
                for enchant in self.after.active_status_enchants
                if enchant.native_uid > 0
                and enchant.native_uid not in before_listener_uids
            )
        )
        for receipt in status_receipts:
            if isinstance(receipt, AntiDebuffExecution):
                if (
                    receipt.status
                    is not AntiDebuffExecutionStatus.EXECUTED
                    or receipt.state_before != anti_runtime
                ):
                    raise ValueError(
                        "AntiDebuff status receipt chain is discontinuous"
                    )
                if receipt.operation == "installed":
                    if (
                        receipt.state_before.status_present
                        or receipt.state_after.uid != status_cursor
                    ):
                        raise ValueError(
                            "AntiDebuff install does not own the next status UID"
                        )
                    status_cursor += 1
                elif receipt.operation == "stacked":
                    if (
                        not receipt.state_before.status_present
                        or receipt.state_after.uid
                        != receipt.state_before.uid
                    ):
                        raise ValueError(
                            "AntiDebuff stack changed status identity"
                        )
                else:
                    raise ValueError(
                        "unsupported AntiDebuff status receipt operation"
                    )
                anti_runtime = receipt.state_after
                continue
            if isinstance(receipt, EnthusiasticReceipt):
                if (
                    receipt.before != enthusiastic_runtime
                    or receipt.cursor_before != status_cursor
                ):
                    raise ValueError(
                        "Enthusiastic status receipt chain is discontinuous"
                    )
                enthusiastic_runtime = receipt.after
                status_cursor = receipt.cursor_after
                continue
            if (
                receipt.before != playable_runtime
            ):
                raise ValueError(
                    "PlayableValueAdd status receipt chain is discontinuous"
                )
            while status_cursor < receipt.cursor_before:
                if (
                    not intervening_listener_uids
                    or intervening_listener_uids[0] != status_cursor
                ):
                    raise ValueError(
                        "PlayableValueAdd cursor gap has no exact status owner"
                    )
                intervening_listener_uids.pop(0)
                status_cursor += 1
            if receipt.cursor_before != status_cursor:
                raise ValueError(
                    "PlayableValueAdd status cursor moved backwards"
                )
            playable_runtime = receipt.after
            status_cursor = receipt.cursor_after
        while intervening_listener_uids and intervening_listener_uids[0] == status_cursor:
            intervening_listener_uids.pop(0)
            status_cursor += 1
        if status_receipts and (
            intervening_listener_uids
            or
            self.after.anti_debuff_runtime != anti_runtime
            or self.after.playable_value_add_runtime != playable_runtime
            or self.after.enthusiastic_runtime != enthusiastic_runtime
            or self.after.next_status_uid != status_cursor
        ):
            raise ValueError(
                "status operation receipts do not match step boundary"
            )
        object.__setattr__(
            self,
            "status_operation_receipts",
            status_receipts,
        )
        receipts = tuple(self.operation_receipts)
        if any(
            not isinstance(value, CardPlayCountOperationReceipt)
            for value in receipts
        ):
            raise TypeError(
                "operation_receipts must contain "
                "CardPlayCountOperationReceipt"
            )
        if any(
            value.owner not in PLAN3_ORDINARY_CARD_PLAY_COUNT_RECEIPT_OWNERS
            for value in receipts
        ):
            raise ValueError(
                "operation_receipts must use Plan3 ordinary card "
                "play-count owners"
            )
        if self.kind != "card" and receipts:
            raise ValueError(
                "operation_receipts belong only to an ordinary card step"
            )
        if self.kind == "card":
            owners = tuple(value.owner for value in receipts)
            expected = (
                PLAN3_ORDINARY_CARD_PLAY_COUNT_BUILD_OWNER,
                PLAN3_ORDINARY_CARD_PLAY_COUNT_MOVE_OWNER,
            )
            if self.playable_count_consumed == 1:
                if (
                    owners != expected
                    or len({value.subject_id for value in receipts}) != 1
                    or receipts[0].after_value != receipts[1].before_value
                ):
                    raise ValueError(
                        "ordinary card step requires one build/move receipt chain"
                    )
            elif receipts:
                raise ValueError(
                    "non-ordinary card step must not carry ordinary receipts"
                )
        object.__setattr__(self, "operation_receipts", receipts)

    @property
    def terminal_boundary(self) -> bool:
        """Whether this step is the final EndTurn without a next TurnStart.

        The normal card-at-last-play path has two possible boundaries.  When
        turns remain, :func:`start_native_turn` appends a ``turn_boundary``
        step and carries the authoritative draw/TurnStart result.  On the
        final turn, ``end_plan3_turn`` instead produces an ``end_turn`` step:
        the hand has already been committed to Grave/Lost and there is no
        legal next draw.  Keeping that distinction explicit prevents callers
        from treating the terminal boundary as a missing TurnStart or from
        inventing one.
        """

        return (
            self.kind == "end_turn"
            and self.turn_start is None
            and self.native_draw is None
            and self.after.turns_remaining == 0
            and not self.after.awaiting_turn_start
            and self.after.plays_remaining == 0
        )

    @property
    def settled_boundary(self) -> bool:
        """Whether this step closes the selected card's current turn."""

        return (
            self.kind == "turn_boundary"
            and self.turn_start is not None
        ) or self.terminal_boundary


@dataclass(frozen=True, slots=True)
class Plan3NativeRuntimeDrawTransition:
    """One CardDraw runtime command and its independent native transition."""

    event: Plan3RuntimeEffectEvent
    transition: Plan3NativeDrawTransition

    def __post_init__(self) -> None:
        if not isinstance(self.event, Plan3RuntimeEffectEvent):
            raise TypeError("event must be Plan3RuntimeEffectEvent")
        if self.event.effect_type != EFFECT_CARD_DRAW:
            raise ValueError("runtime draw event must be ExamCardDraw")
        if not isinstance(self.transition, Plan3NativeDrawTransition):
            raise TypeError("transition must be Plan3NativeDrawTransition")
        if self.event.requested_count != self.transition.requested_count:
            raise ValueError("runtime draw request does not match event")
        if not self.transition.effect_draw:
            raise ValueError("runtime draw transition must update effect counter")

    @property
    def requested_count(self) -> int:
        assert self.event.requested_count is not None
        return self.event.requested_count

    @property
    def actual_count(self) -> int:
        return self.transition.actual_count

    @property
    def drawn_guids(self) -> tuple[str, ...]:
        return self.transition.drawn_guids


@dataclass(frozen=True, slots=True)
class Plan3NativeRuntimeCardUpgradeTransition:
    """One delayed CardUpgrade command tied to its scalar event slot."""

    event: Plan3RuntimeEffectEvent
    result: Plan3CardUpgradeResult

    def __post_init__(self) -> None:
        if not isinstance(self.event, Plan3RuntimeEffectEvent):
            raise TypeError("event must be Plan3RuntimeEffectEvent")
        if self.event.effect_type != EFFECT_CARD_UPGRADE:
            raise ValueError("runtime upgrade event must be ExamCardUpgrade")
        if not isinstance(self.result, Plan3CardUpgradeResult):
            raise TypeError("result must be Plan3CardUpgradeResult")
        if not self.result.applied:
            raise ValueError("runtime upgrade result must be applied")
        if self.event.effect_id != self.result.effect_id:
            raise ValueError("runtime upgrade result does not match event")


@dataclass(frozen=True, slots=True)
class Plan3NativeRuntimeEffectReplay:
    """Complete native replay output while the compatibility API stays stable."""

    state: Plan3NativeState
    draws: tuple[Plan3NativeRuntimeDrawTransition, ...] = ()
    card_upgrades: tuple[Plan3NativeRuntimeCardUpgradeTransition, ...] = ()
    card_moves: tuple[object, ...] = ()


@dataclass(frozen=True, slots=True)
class _Plan3NativeEffectVariant:
    state: Plan3NativeState
    scalar_state: Plan3State
    status_cursor: int = 0
    direct_draw: Plan3NativeDrawTransition | None = None
    add_grow_mutations: tuple[NiaAddGrowMutation, ...] = ()
    search_stamina_applications: tuple[
        Plan3SearchStaminaApplication, ...
    ] = ()
    hand_grave_draws: tuple[HandGraveDrawTransition, ...] = ()
    card_upgrade_results: tuple[Plan3CardUpgradeResult, ...] = ()
    card_create_results: tuple[CardCreateResult, ...] = ()
    card_create_search_results: tuple[CardCreateSearchResult, ...] = ()
    anti_debuff_executions: tuple[AntiDebuffExecution, ...] = ()
    status_operation_receipts: tuple[Plan3StatusOperationReceipt, ...] = ()
    reconciled_listener_instance_ids: tuple[str, ...] = ()
    queued_use_pool_commands: tuple[Plan3UsePoolCommand, ...] = ()
    queued_use_pool_effect_ids: tuple[str, ...] = ()
    move_effect_traces: tuple[object, ...] = ()


@dataclass(frozen=True, slots=True)
class Plan3MoveDispatchVariant:
    state: Plan3NativeState
    traces: tuple[object, ...] = ()
    add_grow_mutations: tuple[NiaAddGrowMutation, ...] = ()


def dispatch_plan3_guid_move_commits(
    before: Plan3NativeState,
    after: Plan3NativeState,
    moved_guids: Iterable[str],
    *,
    hand_limit: int,
    hold_limit: int,
    lesson_type: str,
    is_full_power: bool,
    support_upgrades: SupportInputs,
    support_card_searches: Mapping[str, ProduceCardSearchRule],
    database: Path,
) -> tuple[Plan3MoveDispatchVariant, ...]:
    """Run the central difference callback after each committed GUID move."""

    from .plan3_legend_move_effect import (
        LegendMoveEffectError,
        MoveCommitEvent,
        MoveEffectRuntimeConfig,
        dispatch_legend_move_callback_branches,
    )

    config = MoveEffectRuntimeConfig(
        hand_limit=hand_limit,
        hold_limit=hold_limit,
        lesson_type=lesson_type,
        is_full_power=is_full_power,
        support_upgrades=support_upgrades,
        support_card_searches=support_card_searches,
    )
    variants = (Plan3MoveDispatchVariant(after),)
    for guid in tuple(moved_guids):
        expanded: list[Plan3MoveDispatchVariant] = []
        for variant in variants:
            moved_card = variant.state.card_by_guid(guid)
            try:
                move_master = load_master_card(
                    moved_card.card_id,
                    moved_card.effective_upgrade,
                    Path(database),
                )
            except (KeyError, OSError, ValueError) as error:
                raise Plan3NativeStateError(
                    "move-effect-master-unresolved",
                    f"{moved_card.card_id}+{moved_card.effective_upgrade}:{error}",
                ) from error
            if not move_master.move_effect_ids:
                expanded.append(variant)
                continue
            event = MoveCommitEvent(before, variant.state, guid)
            try:
                results = dispatch_legend_move_callback_branches(
                    event, config, database=Path(database)
                )
            except LegendMoveEffectError as error:
                raise Plan3NativeStateError(
                    f"move-effect-callback-{error.code}", error.detail
                ) from error
            for result in results:
                expanded.append(
                    Plan3MoveDispatchVariant(
                        result.after,
                        (*variant.traces, *result.traces),
                        (
                            *variant.add_grow_mutations,
                            *result.add_grow_mutations,
                        ),
                    )
                )
        variants = tuple(expanded)
    return variants


def _validate_native_card_play_triggers(
    state: Plan3State,
    native_state: Plan3NativeState,
    playing_guid: str,
    playing_card: Plan3Card,
    *,
    database: Path,
) -> None:
    """Validate the two bounded CardPlay shapes on the exact playing GUID."""

    for enchant in state.active_status_enchants:
        trigger = enchant.rule.trigger
        if trigger.phase_types != (NATIVE_PHASE_CARD_PLAY,):
            continue
        try:
            search = (
                load_produce_card_search(
                    trigger.produce_card_search_id, database
                )
                if trigger.produce_card_search_id
                else None
            )
        except (KeyError, OSError, TypeError, ValueError) as error:
            raise Plan3NativeStateError(
                "card-play-trigger-search-unavailable",
                f"{trigger.id}:{type(error).__name__}:{error}",
            ) from error
        contract = resolve_plan3_card_play_trigger(trigger, search)
        if contract.shape not in EXECUTABLE_CARD_PLAY_TRIGGER_SHAPES:
            continue
        result = evaluate_plan3_card_play_trigger(
            contract,
            state,
            native_state,
            playing_guid,
            playing_card,
            event_phase=NATIVE_PHASE_CARD_PLAY,
        )
        if not result.decision.supported or result.fires is None:
            reasons = ",".join(
                item.reason for item in result.decision.unresolved
            )
            raise Plan3NativeStateError(
                "card-play-trigger-native-unresolved",
                f"{trigger.id}:{reasons}",
            )
        scalar = evaluate_plan3_trigger(
            trigger,
            state,
            event_phase=NATIVE_PHASE_CARD_PLAY,
            event_card=playing_card,
        )
        if not scalar.supported or scalar.fires != result.fires:
            raise Plan3NativeStateError(
                "card-play-trigger-native-scalar-mismatch", trigger.id
            )


def _validate_native_none_field_trigger(
    state: Plan3State,
    native_state: Plan3NativeState,
    playing_guid: str,
    playing_card: Plan3Card,
) -> None:
    """Bind the one exact None+Not+Preservation direct trigger to its GUID."""

    for effect in playing_card.effects:
        trigger = effect.trigger
        if trigger is None or trigger.id != TRIGGER_NONE_NOT_PRESERVATION_UP:
            continue
        result = evaluate_plan3_trigger_none_field(
            trigger,
            state,
            native_state,
            playing_guid,
            playing_card,
            dispatch_phase=DISPATCH_PHASE_CARD_PLAY_EFFECT,
            dispatch_source=DISPATCH_SOURCE_CARD_DIRECT_EFFECT,
        )
        scalar = evaluate_plan3_trigger(trigger, state)
        if (
            not result.supported
            or result.fires is None
            or not scalar.supported
            or scalar.fires != result.fires
        ):
            reasons = ",".join(
                item.reason for item in result.decision.unresolved
            )
            raise Plan3NativeStateError(
                "none-field-trigger-native-unresolved",
                f"{trigger.id}:{reasons}",
            )


def _apply_native_anti_debuff_effect(
    state: Plan3NativeState,
    scalar_state: Plan3State,
    effect: Plan3Effect,
    *,
    operation_cursor: int | None = None,
) -> tuple[Plan3NativeState, Plan3State, AntiDebuffExecution]:
    if state.anti_debuff_runtime != scalar_state.anti_debuff_runtime:
        raise Plan3NativeStateError(
            "plan3-anti-debuff-projection-mismatch"
        )
    try:
        row = ANTI_DEBUFF_CATALOG.row(effect.id)
    except KeyError as error:
        raise Plan3NativeStateError(
            "anti-debuff-effect-row-unresolved", effect.id
        ) from error
    if (
        effect.effect_type != row.effect_type
        or effect.value1 != 0
        or effect.value2 != 0
        or effect.effect_count != row.effect_count
        or effect.effect_turn != row.effect_turn
        or effect.trigger is not None
    ):
        raise Plan3NativeStateError(
            "anti-debuff-effect-shape-mismatch", effect.id
        )
    working_scalar = scalar_state
    install_uid: int | None = None
    if not state.anti_debuff_runtime.status_present:
        if operation_cursor is None:
            working_scalar, install_uid = allocate_plan3_status_uid(
                scalar_state
            )
        else:
            if (
                isinstance(operation_cursor, bool)
                or not isinstance(operation_cursor, int)
                or not 1 <= operation_cursor < 2**31 - 1
                or not scalar_state.status_uid_cursor_exact
            ):
                raise Plan3NativeStateError(
                    "anti-debuff-operation-cursor-unprojected",
                    str(operation_cursor),
                )
            install_uid = operation_cursor
            working_scalar = replace(
                scalar_state,
                next_status_uid=max(
                    scalar_state.next_status_uid,
                    operation_cursor + 1,
                ),
            )
    execution = execute_anti_debuff(
        row,
        state.anti_debuff_runtime,
        install_uid=install_uid,
    )
    if (
        not isinstance(execution, AntiDebuffExecution)
        or execution.status is not AntiDebuffExecutionStatus.EXECUTED
    ):
        reasons = ",".join(
            reason.value for reason in getattr(execution, "reasons", ())
        )
        raise Plan3NativeStateError(
            "anti-debuff-execution-unresolved",
            f"{effect.id}:{reasons}",
        )
    scalar_after = replace(
        working_scalar,
        anti_debuff_runtime=execution.state_after,
    )
    native_after = replace(
        state,
        anti_debuff_runtime=execution.state_after,
    )
    return native_after, scalar_after, execution


def _direct_listener_identity(
    effect: Plan3Effect,
    *,
    source_card_id: str,
) -> tuple[str, str]:
    if effect.effect_type == EFFECT_TIMER:
        return effect.id, f"effect-timer:{effect.id}"
    if effect.effect_type in {
        EFFECT_STATUS_ENCHANT,
        EFFECT_STATUS_ENCHANT_ENCORE,
    }:
        if effect.status_enchant is None:
            raise Plan3NativeStateError(
                "direct-listener-rule-missing", effect.id
            )
        return source_card_id, effect.status_enchant.id
    raise Plan3NativeStateError(
        "direct-listener-effect-type-unprojected", effect.effect_type
    )


def _reconcile_preinstalled_direct_listener_uids(
    scalar_state: Plan3State,
    effects: tuple[Plan3Effect, ...],
    *,
    source_card_id: str,
    operation_cursor: int,
    trigger_state: Plan3State,
    card_search_counts: Mapping[str, int],
    prior_instance_ids: frozenset[str],
    new_listeners: tuple[ActivePlan3StatusEnchant, ...],
) -> tuple[Plan3State, int]:
    """Atomically map compatibility listeners onto native source-order UIDs.

    ``apply_plan3_card`` installs scalar listener objects while native-owned
    AntiDebuff/Playable statuses are intentionally deferred to this spine.
    Compute the complete direct-effect allocation order first, then commit
    every listener UID in one immutable replacement.  This avoids transient
    duplicate UIDs when a deferred status must be inserted before two or more
    compatibility-installed listeners.
    """

    if (
        isinstance(operation_cursor, bool)
        or not isinstance(operation_cursor, int)
        or not 1 <= operation_cursor < 2**31 - 1
        or not scalar_state.status_uid_cursor_exact
    ):
        raise Plan3NativeStateError(
            "direct-listener-operation-cursor-unprojected",
            str(operation_cursor),
        )
    available = list(new_listeners)
    if any(
        listener.instance_id in prior_instance_ids
        for listener in available
    ):
        raise Plan3NativeStateError(
            "direct-listener-instance-not-new", source_card_id
        )
    assigned_uids: dict[str, int] = {}
    cursor = operation_cursor
    # The compatibility reducer has already executed non-deferred direct
    # effects.  In particular, an earlier stance change can release a fresh
    # Playable status before a later direct Playable slot, and a scalar-owned
    # status gate can likewise materialize AntiDebuff.  Start from that
    # post-reducer runtime rather than the pre-card trigger snapshot so a
    # following native-owned slot correctly merges without consuming a UID.
    anti_present = scalar_state.anti_debuff_runtime.status_present
    playable_present = (
        scalar_state.playable_value_add_runtime.status_present
    )
    for effect in effects:
        if effect.trigger is not None:
            decision = evaluate_plan3_trigger(
                effect.trigger,
                trigger_state,
                card_search_counts=card_search_counts,
            )
            if not decision.supported:
                raise Plan3NativeStateError(
                    "direct-listener-trigger-unprojected",
                    ",".join(decision.unsupported_rules),
                )
            if not decision.fires:
                continue
        if effect.effect_type == EFFECT_ANTI_DEBUFF:
            if not anti_present:
                cursor += 1
                anti_present = True
            continue
        if effect.effect_type == EFFECT_PLAYABLE_VALUE_ADD:
            if not playable_present:
                cursor += 1
                playable_present = True
            continue
        if effect.effect_type not in {
            EFFECT_STATUS_ENCHANT,
            EFFECT_STATUS_ENCHANT_ENCORE,
            EFFECT_TIMER,
        }:
            continue
        expected_source_id, expected_rule_id = _direct_listener_identity(
            effect,
            source_card_id=source_card_id,
        )
        matching_index = next(
            (
                index
                for index, listener in enumerate(available)
                if listener.source_id == expected_source_id
                and listener.rule.id == expected_rule_id
            ),
            None,
        )
        if matching_index is None:
            raise Plan3NativeStateError(
                "direct-listener-instance-count-unprojected",
                f"{effect.id}:0",
            )
        listener = available.pop(matching_index)
        if listener.is_encore_enchant and listener.native_uid != cursor:
            # Encore identity embeds the native UID.  No current card mixes
            # Encore with a deferred direct status; keep such a future shape
            # closed until its captured identity can be atomically renamed.
            raise Plan3NativeStateError(
                "direct-listener-encore-identity-rebind-unprojected",
                listener.instance_id,
            )
        assigned_uids[listener.instance_id] = cursor
        cursor += 1
    if available or set(assigned_uids) != {
        listener.instance_id for listener in new_listeners
    }:
        raise Plan3NativeStateError(
            "direct-listener-reconciliation-incomplete",
            source_card_id,
        )
    active = tuple(
        replace(listener, native_uid=assigned_uids[listener.instance_id])
        if listener.instance_id in assigned_uids
        else listener
        for listener in scalar_state.active_status_enchants
    )
    active_uids = tuple(
        listener.native_uid for listener in active if listener.native_uid > 0
    )
    if len(active_uids) != len(set(active_uids)):
        raise Plan3NativeStateError(
            "direct-listener-native-uid-collision", source_card_id
        )
    return replace(
        scalar_state,
        active_status_enchants=active,
        next_status_uid=cursor,
    ), cursor


def _confirm_preinstalled_direct_listener_uid(
    scalar_state: Plan3State,
    effect: Plan3Effect,
    *,
    source_card_id: str,
    operation_cursor: int,
    prior_instance_ids: frozenset[str],
    reconciled_instance_ids: tuple[str, ...],
) -> str:
    expected_source_id, expected_rule_id = _direct_listener_identity(
        effect,
        source_card_id=source_card_id,
    )
    reconciled = frozenset(reconciled_instance_ids)
    matches = tuple(
        listener
        for listener in scalar_state.active_status_enchants
        if listener.instance_id not in prior_instance_ids
        and listener.instance_id not in reconciled
        and listener.source_id == expected_source_id
        and listener.rule.id == expected_rule_id
        and listener.native_uid == operation_cursor
    )
    if len(matches) != 1:
        raise Plan3NativeStateError(
            "direct-listener-source-order-mismatch", effect.id
        )
    return matches[0].instance_id


def _apply_native_playable_value_add_effect(
    scalar_state: Plan3State,
    effect: Plan3Effect,
    *,
    operation_cursor: int | None = None,
) -> tuple[Plan3State, PlayableValueAddReceipt]:
    """Install the exact phase-2B direct-card Playable status sidecar.

    ``apply_plan3_card`` already owns the scalar ``plays_remaining`` delta.
    This native-search slice owns only the status identity/cursor lifecycle,
    so applying the kernel here must not add the scalar play a second time.
    Existing statuses merge through the same native kernel and preserve their
    UID. ``operation_cursor`` is the cursor at this exact direct-effect slot;
    the scalar state may already contain a later, independently owned status
    allocation materialized by the compatibility reducer.
    """

    if effect.effect_type != EFFECT_PLAYABLE_VALUE_ADD:
        raise Plan3NativeStateError(
            "playable-value-add-effect-type", effect.effect_type
        )
    if (
        isinstance(effect.effect_count, bool)
        or effect.effect_count <= 0
        or effect.value1 != 0
        or effect.value2 != 0
        or effect.effect_turn != 0
        or effect.status_enchant_id
        or effect.status_enchant is not None
        or effect.chain_effect_id
        or effect.chain_effect_ids
        or effect.chain_effect is not None
        or effect.card_move_rule is not None
    ):
        raise Plan3NativeStateError(
            "playable-value-add-direct-shape-unprojected",
            effect.id,
        )
    if not scalar_state.status_uid_cursor_exact:
        raise Plan3NativeStateError(
            "playable-value-add-status-uid-cursor-unprojected"
        )
    cursor = (
        scalar_state.next_status_uid
        if operation_cursor is None
        else operation_cursor
    )
    try:
        receipt = add_playable_value_add(
            scalar_state.playable_value_add_runtime,
            effect.effect_count,
            next_status_uid=cursor,
            source=f"direct-card:{effect.id}",
        )
    except (OverflowError, TypeError, ValueError) as error:
        raise Plan3NativeStateError(
            "playable-value-add-direct-kernel",
            f"{type(error).__name__}:{error}",
        ) from error
    if receipt.operation not in {
        PlayableValueAddOperation.ADD_CREATE,
        PlayableValueAddOperation.ADD_MERGE,
    }:
        raise Plan3NativeStateError(
            "playable-value-add-direct-operation-unprojected",
            receipt.operation.value,
        )
    return (
        replace(
            scalar_state,
            playable_value_add_runtime=receipt.after,
            next_status_uid=max(
                scalar_state.next_status_uid,
                receipt.cursor_after,
            ),
        ),
        receipt,
    )


def _replace_native_card_by_guid(
    state: Plan3NativeState,
    guid: str,
    replacement: Plan3NativeCard,
) -> Plan3NativeState:
    if replacement.guid != guid:
        raise Plan3NativeStateError("native-card-replacement-guid-mismatch", guid)
    found = 0
    zones: dict[str, tuple[Plan3NativeCard, ...]] = {}
    for zone_name in ("hand", "deck", "grave", "lost", "hold"):
        zone: list[Plan3NativeCard] = []
        for card in getattr(state, zone_name):
            if card.guid == guid:
                found += 1
                zone.append(replacement)
            else:
                zone.append(card)
        zones[zone_name] = tuple(zone)
    if found != 1:
        raise Plan3NativeStateError("native-card-replacement-guid-count", guid)
    return replace(state, **zones)


def _install_exact_card_play_after_listener(
    state: Plan3NativeState,
    effect: Plan3Effect,
    *,
    playing_guid: str,
    database: Path,
) -> Plan3NativeState:
    """Attach the bounded 110 listener to its exact native owner GUID."""

    rule = effect.status_enchant
    if (
        effect.effect_type != EFFECT_STATUS_ENCHANT
        or rule is None
        or rule.trigger.id != TRIGGER_ID_CARD_PLAY_AFTER_EFFECT_GROUP
    ):
        return state
    owner = state.card_by_guid(playing_guid)
    if (
        owner.card_id != CARD_ID_IDO_3_110
        or owner.effective_upgrade not in CARD_UPGRADES_IDO_3_110
    ):
        raise Plan3NativeStateError(
            "card-play-after-listener-owner-outside-bound", owner.card_id
        )
    if owner.runtime_grow_status is not None:
        raise Plan3NativeStateError(
            "card-play-after-listener-owner-status-conflict", playing_guid
        )
    search = load_produce_card_search(
        rule.trigger.produce_card_search_id, database
    )
    contract = resolve_plan3_card_play_after_trigger(rule.trigger, search)
    if not contract.supported:
        reasons = ",".join(item.reason for item in contract.unresolved)
        raise Plan3NativeStateError(
            "card-play-after-listener-contract-unresolved", reasons
        )
    if len(rule.effects) != 1 or rule.effects[0].effect_type != EFFECT_ADD_GROW:
        raise Plan3NativeStateError(
            "card-play-after-listener-effect-shape", rule.id
        )
    program = load_plan3_add_grow_program(
        rule.effects[0].id, database=database
    )
    status = Plan3NativeCardGrowStatus(
        id=rule.id,
        trigger_id=rule.trigger.id,
        trigger_kind="card_play_after_effect_group",
        interval=0,
        grow_effects=program.grow_effects,
        trigger_count=0,
        spend_count=0,
        spend_turn=0,
        search_id=rule.trigger.produce_card_search_id,
        target_effect_group_ids=search.effect_group_ids,
    )
    return _replace_native_card_by_guid(
        state,
        playing_guid,
        replace(owner, runtime_grow_status=status),
    )


def _apply_exact_card_play_after_listeners(
    scalar_state: Plan3State,
    native_state: Plan3NativeState,
    target_guid: str,
    target_card: Plan3Card,
    *,
    database: Path,
) -> tuple[
    Plan3NativeState,
    tuple[NiaAddGrowMutation, ...],
    tuple[str, ...],
]:
    """Evaluate bounded Target/effectGroup listeners post-final-zone."""

    working = native_state
    mutations: tuple[NiaAddGrowMutation, ...] = ()
    skipped: list[str] = []
    listeners = tuple(
        enchant
        for enchant in scalar_state.active_status_enchants
        if enchant.source_id == CARD_ID_IDO_3_110
        and enchant.rule.trigger.id
        == TRIGGER_ID_CARD_PLAY_AFTER_EFFECT_GROUP
    )
    for enchant in listeners:
        owners = tuple(
            card
            for card in working.all_cards
            if card.card_id == CARD_ID_IDO_3_110
            and card.runtime_grow_status is not None
            and card.runtime_grow_status.trigger_id
            == TRIGGER_ID_CARD_PLAY_AFTER_EFFECT_GROUP
        )
        if len(owners) != 1:
            raise Plan3NativeStateError(
                "card-play-after-listener-owner-guid-ambiguous",
                f"{enchant.instance_id}:{len(owners)}",
            )
        owner = owners[0]
        skipped.append(owner.guid)
        event_card = load_plan3_card(
            owner.card_id, owner.effective_upgrade, database
        )
        search = load_produce_card_search(
            enchant.rule.trigger.produce_card_search_id, database
        )
        contract = resolve_plan3_card_play_after_trigger(
            enchant.rule.trigger, search
        )
        result = evaluate_plan3_card_play_after_trigger(
            contract,
            scalar_state,
            working,
            owner.guid,
            target_guid,
            event_card,
            target_card,
            event_phase=NATIVE_PHASE_CARD_PLAY_AFTER,
            move_settled=True,
        )
        if not result.decision.supported or result.fires is None:
            reasons = ",".join(
                item.reason for item in result.decision.unresolved
            )
            raise Plan3NativeStateError(
                "card-play-after-listener-native-unresolved",
                f"{enchant.instance_id}:{reasons}",
            )
        status = owner.runtime_grow_status
        assert status is not None
        status = status.with_phase_increment(3, 1)
        if result.fires:
            status = replace(status, spend_count=status.spend_count + 1)
        working = _replace_native_card_by_guid(
            working,
            owner.guid,
            replace(owner, runtime_grow_status=status),
        )
        if result.fires:
            working, added = apply_plan3_native_add_grow_effects(
                working,
                enchant.rule.effects,
                database=database,
            )
            mutations = (*mutations, *added)
    return working, mutations, tuple(skipped)


@dataclass(frozen=True, slots=True)
class Plan3NativeTurnStartExtensionResult:
    """Path-local state returned by a mode-specific turn-start adapter."""

    scalar_state: Plan3State
    native_state: Plan3NativeState
    extension_state: object | None
    fired_effect_ids: tuple[str, ...] = ()
    unsupported_rules: tuple[str, ...] = ()
    trace: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scalar_state, Plan3State):
            raise TypeError("scalar_state must be Plan3State")
        if not isinstance(self.native_state, Plan3NativeState):
            raise TypeError("native_state must be Plan3NativeState")
        if any(
            not isinstance(value, str) or not value
            for value in (*self.fired_effect_ids, *self.unsupported_rules)
        ):
            raise TypeError("effect IDs and unsupported rules must be text")


Plan3NativeTurnStartExtension: TypeAlias = Callable[
    [Plan3State, Plan3NativeState, object | None, Plan3ExamSettings],
    Plan3NativeTurnStartExtensionResult,
]


@dataclass(frozen=True, slots=True)
class Plan3NativeSearchDiagnostic:
    stage: str
    semantic_gaps: tuple[str, ...]
    state: Plan3State
    native_state: Plan3NativeState
    actions: tuple[Plan3NativeSearchAction, ...]
    card_guid: str | None = None


@dataclass(frozen=True, slots=True)
class Plan3NativeSearchPath:
    state: Plan3State
    native_state: Plan3NativeState
    steps: tuple[Plan3NativeSearchStep, ...]
    evaluation: Plan3SearchEvaluation
    drink_inventory: Plan3DrinkInventory = field(
        default_factory=Plan3DrinkInventory
    )
    stopped_reason: str = ""
    turn_start_extension_state: object | None = None

    @property
    def actions(self) -> tuple[Plan3NativeSearchAction, ...]:
        return tuple(step.action for step in self.steps if step.action is not None)

    @property
    def card_refs(self) -> tuple[Plan3CardRef, ...]:
        return tuple(action.card_ref for action in self.actions)

    @property
    def card_guids(self) -> tuple[str, ...]:
        return tuple(action.guid for action in self.actions)

    @property
    def transitions(self) -> tuple[Plan3Transition, ...]:
        return tuple(
            step.card_transition
            for step in self.steps
            if step.card_transition is not None
        )

    @property
    def drink_actions(self) -> tuple[Plan3NativeDrinkAction, ...]:
        return tuple(
            step.drink_action
            for step in self.steps
            if step.drink_action is not None
        )

    @property
    def decision_steps(self) -> tuple[Plan3NativeSearchStep, ...]:
        return tuple(
            step
            for step in self.steps
            if step.kind in {"card", "drink", "skip"}
        )

    @property
    def complete(self) -> bool:
        return self.state.turns_remaining == 0


@dataclass(frozen=True, slots=True)
class Plan3NativeSearchResult:
    best: Plan3NativeSearchPath | None
    candidates: tuple[Plan3NativeSearchPath, ...]
    diagnostics: tuple[Plan3NativeSearchDiagnostic, ...]
    expanded_nodes: int
    deduplicated_nodes: int
    beam_width: int
    depth: int | None
    battle_parameter_schedule: tuple[int, ...] | None = None
    battle_ranking_resolved: bool = False
    include_skip: bool = False
    force_end_score: int = 0
    force_end_stamina_recovery: int = 0
    initial_drink_ids: tuple[str, ...] = ()


PLAN3_NATIVE_ENUMERATOR_AUTHORITY = "plan3-native-root-enumerator-v1"


@dataclass(frozen=True, slots=True)
class Plan3NativeLegalCandidate:
    """One canonical user-visible action at a settled Plan 3 root.

    The search kernel uses ``card``/``skip`` as internal step kinds and keeps
    drink identity as an ordered inventory slot.  This adapter deliberately
    exposes the smaller action vocabulary consumed by a legal-candidate
    sidecar: ``play``, ``drink`` and ``turn-end``.  Native provenance remains
    attached, but does not become a second action identity.
    """

    kind: str
    action_id: str
    native_kind: str
    hand_index: int | None = None
    card_guid: str | None = None
    card_id: str | None = None
    card_upgrade: int | None = None
    selected_card_guids: tuple[str, ...] = ()
    drink_slot_index: int | None = None
    drink_id: str | None = None
    selected_card_guid: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"play", "drink", "turn-end"}:
            raise ValueError(f"unsupported canonical Plan 3 action kind: {self.kind}")
        if not isinstance(self.action_id, str) or not self.action_id:
            raise ValueError("Plan 3 legal candidate action_id must be non-empty")
        if self.native_kind not in {"card", "drink", "skip"}:
            raise ValueError(f"unsupported native Plan 3 action kind: {self.native_kind}")
        selected = tuple(self.selected_card_guids)
        if any(not isinstance(value, str) or not value for value in selected):
            raise ValueError("selected_card_guids must contain non-empty text")
        if len(selected) != len(set(selected)):
            raise ValueError("selected_card_guids must be unique")
        object.__setattr__(self, "selected_card_guids", selected)

        if self.kind == "play":
            if self.native_kind != "card":
                raise ValueError("play candidate must have native_kind=card")
            if not isinstance(self.hand_index, int) or isinstance(self.hand_index, bool):
                raise ValueError("play candidate hand_index must be an integer")
            if self.hand_index < 0:
                raise ValueError("play candidate hand_index must be non-negative")
            for name in ("card_guid", "card_id"):
                value = getattr(self, name)
                if not isinstance(value, str) or not value:
                    raise ValueError(f"play candidate {name} must be non-empty")
            if (
                not isinstance(self.card_upgrade, int)
                or isinstance(self.card_upgrade, bool)
                or self.card_upgrade < 0
            ):
                raise ValueError("play candidate card_upgrade must be non-negative")
            if self.action_id != f"PLAY:{self.card_guid}":
                raise ValueError("play candidate action_id does not match card_guid")
            if any(
                value is not None
                for value in (
                    self.drink_slot_index,
                    self.drink_id,
                    self.selected_card_guid,
                )
            ):
                raise ValueError("play candidate cannot carry drink identity")
            return

        if self.kind == "drink":
            if self.native_kind != "drink":
                raise ValueError("drink candidate must have native_kind=drink")
            if (
                not isinstance(self.drink_slot_index, int)
                or isinstance(self.drink_slot_index, bool)
                or self.drink_slot_index < 0
            ):
                raise ValueError("drink candidate slot must be non-negative")
            if not isinstance(self.drink_id, str) or not self.drink_id:
                raise ValueError("drink candidate drink_id must be non-empty")
            if self.selected_card_guid is not None and (
                not isinstance(self.selected_card_guid, str)
                or not self.selected_card_guid
            ):
                raise ValueError("drink candidate selected_card_guid is invalid")
            selected = self.selected_card_guid or "-"
            expected = f"DRINK:{self.drink_slot_index}:{self.drink_id}:{selected}"
            if self.action_id != expected:
                raise ValueError("drink candidate action_id does not match identity")
            if any(
                value is not None
                for value in (
                    self.hand_index,
                    self.card_guid,
                    self.card_id,
                    self.card_upgrade,
                )
            ):
                raise ValueError("drink candidate cannot carry card identity")
            return

        if self.native_kind != "skip" or self.action_id != "END_TURN":
            raise ValueError("turn-end candidate must be the native skip END_TURN")
        if any(
            value is not None
            for value in (
                self.hand_index,
                self.card_guid,
                self.card_id,
                self.card_upgrade,
                self.drink_slot_index,
                self.drink_id,
                self.selected_card_guid,
            )
        ):
            raise ValueError("turn-end candidate cannot carry card or drink identity")

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "action_id": self.action_id,
            "native_kind": self.native_kind,
        }
        if self.kind == "play":
            payload.update(
                {
                    "hand_index": self.hand_index,
                    "card_guid": self.card_guid,
                    "card_id": self.card_id,
                    "upgrade": self.card_upgrade,
                    "selected_card_guids": list(self.selected_card_guids),
                }
            )
        elif self.kind == "drink":
            payload.update(
                {
                    "slot_index": self.drink_slot_index,
                    "drink_id": self.drink_id,
                    "selected_card_guid": self.selected_card_guid,
                }
            )
        return payload


@dataclass(frozen=True, slots=True)
class Plan3NativeLegalCandidateEnumeration:
    """Exact root candidate publication with an explicit completeness claim."""

    candidates: tuple[Plan3NativeLegalCandidate, ...]
    authority: str = PLAN3_NATIVE_ENUMERATOR_AUTHORITY
    complete: bool = True
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        candidates = tuple(self.candidates)
        if any(not isinstance(value, Plan3NativeLegalCandidate) for value in candidates):
            raise TypeError("candidates must contain Plan3NativeLegalCandidate values")
        action_ids = tuple(value.action_id for value in candidates)
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("root legal candidates must have unique action IDs")
        if not isinstance(self.authority, str) or not self.authority.strip():
            raise ValueError("candidate authority must be non-empty text")
        if type(self.complete) is not bool:
            raise TypeError("candidate completeness must be boolean")
        blockers = tuple(self.blockers)
        if any(not isinstance(value, str) or not value for value in blockers):
            raise ValueError("candidate blockers must contain non-empty text")
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "blockers", tuple(dict.fromkeys(blockers)))
        if self.complete and (not candidates or self.blockers):
            raise ValueError("complete root enumeration requires candidates and no blockers")

    @property
    def canonical_candidates(self) -> tuple[dict[str, object], ...]:
        return tuple(value.to_dict() for value in self.candidates)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "gkms.plan3-native-legal-candidate-enumeration.v1",
            "authority": self.authority,
            "complete": self.complete,
            "blockers": list(self.blockers),
            "candidates": list(self.canonical_candidates),
        }


def _synchronize_zones(
    state: Plan3State, native: Plan3NativeState
) -> Plan3State:
    zones = native.projection()
    return replace(
        state,
        hand=zones.hand,
        draw_pile=zones.deck,
        discard_pile=zones.grave,
        lost_pile=zones.lost,
        hold_pile=zones.hold,
    )


def _plan3_add_grow_card_profile(card: Plan3Card) -> Plan3AddGrowCardProfile:
    """Project the static card facts queried by native AddGrow validation."""

    return Plan3AddGrowCardProfile(
        category=card.category,
        effect_group_ids=card.effect_group_ids,
        cost_type=card.cost_type,
        is_stamina_cost_penetrate=card.force_stamina_cost > 0,
        effect_types=tuple(effect.effect_type for effect in card.effects),
    )


def _apply_native_search_stamina_effect(
    state: Plan3NativeState,
    effect: Plan3Effect,
    *,
    playing_guid: str,
    database: Path,
) -> tuple[Plan3NativeState, Plan3SearchStaminaApplication]:
    """Install one exact search-stamina command with Playing separated."""

    matches = tuple(
        (index, card)
        for index, card in enumerate(state.hand)
        if card.guid == playing_guid
    )
    if len(matches) != 1:
        raise Plan3NativeStateError("played-guid-not-in-hand", playing_guid)
    playing_index, playing = matches[0]
    ordinary = replace(
        state,
        hand=tuple(card for card in state.hand if card.guid != playing_guid),
    )
    resolution = resolve_plan3_search_stamina_change(
        effect.id, database=database
    )
    if not resolution.executable:
        gap = resolution.gaps[0]
        raise Plan3NativeStateError(
            "search-stamina-effect-unresolved",
            f"{gap.code}:{gap.detail}".rstrip(":"),
        )
    application = apply_plan3_search_stamina_change(
        ordinary.search_stamina_runtime,
        resolution.require_ready(),
        ordinary,
        playing_card=playing,
    )
    after = replace(
        ordinary,
        search_stamina_runtime=application.runtime_after,
    )
    hand = list(after.hand)
    hand.insert(playing_index, playing)
    return replace(after, hand=tuple(hand)), application


def _apply_native_hand_grave_draw_effect(
    state: Plan3NativeState,
    *,
    playing_guid: str,
    hand_limit: int,
    lesson_type: str,
    support_upgrades: SupportInputs,
    support_card_searches: Mapping[str, ProduceCardSearchRule],
    database: Path,
) -> tuple[Plan3NativeState, HandGraveDrawTransition]:
    """Execute Hand/Grave draw with the active card in Playing, not Hand."""

    matches = tuple(
        (index, card)
        for index, card in enumerate(state.hand)
        if card.guid == playing_guid
    )
    if len(matches) != 1:
        raise Plan3NativeStateError("played-guid-not-in-hand", playing_guid)
    playing_index, playing = matches[0]
    ordinary = replace(
        state,
        hand=tuple(card for card in state.hand if card.guid != playing_guid),
    )
    resolution = load_hand_grave_draw(database)
    if not resolution.executable:
        raise Plan3NativeStateError(
            "hand-grave-draw-effect-unresolved",
            f"{resolution.blocker_code}:{resolution.detail}",
        )
    execution = execute_hand_grave_draw(
        resolution,
        ordinary,
        hand_limit=hand_limit,
        lesson_type=lesson_type,
        support_upgrades=support_upgrades,
        support_card_searches=support_card_searches,
    )
    hand = list(execution.after.hand)
    hand.insert(min(playing_index, len(hand)), playing)
    return replace(execution.after, hand=tuple(hand)), execution


def _apply_native_card_upgrade_effect(
    state: Plan3NativeState,
    effect: Plan3Effect,
    *,
    playing_guid: str,
    database: Path,
) -> tuple[tuple[Plan3NativeState, Plan3CardUpgradeResult], ...]:
    """Execute exact All/Select/Random upgrade branches without guessing."""

    matches = tuple(
        (index, card)
        for index, card in enumerate(state.hand)
        if card.guid == playing_guid
    )
    if len(matches) != 1:
        raise Plan3NativeStateError("played-guid-not-in-hand", playing_guid)
    playing_index, playing = matches[0]
    ordinary = replace(
        state,
        hand=tuple(card for card in state.hand if card.guid != playing_guid),
    )
    resolution = resolve_plan3_card_upgrade(
        effect.id,
        database=database,
    )
    if not resolution.resolved or resolution.contract is None:
        assert resolution.pause is not None
        raise Plan3NativeStateError(
            "card-upgrade-effect-unresolved",
            f"{resolution.pause.code}:{resolution.pause.detail}".rstrip(":"),
        )
    contract = resolution.contract
    requests: tuple[tuple[str, ...] | None, ...]
    if contract.branch is Plan3CardUpgradeBranch.ALL:
        requests = (None,)
    elif contract.branch is Plan3CardUpgradeBranch.RANDOM:
        requests = (None,)
    else:
        candidate_result = resolve_plan3_card_upgrade_candidates(
            ordinary,
            contract,
            database=database,
        )
        if candidate_result.pause is not None:
            raise Plan3NativeStateError(
                "card-upgrade-effect-unresolved",
                (
                    f"{candidate_result.pause.code}:"
                    f"{candidate_result.pause.detail}"
                ).rstrip(":"),
            )
        candidates = candidate_result.candidates
        if len(candidates) < contract.pick_count_min:
            raise Plan3NativeStateError(
                "card-upgrade-select-candidate-shortfall",
                f"{effect.id}:{len(candidates)}<{contract.pick_count_min}",
            )
        upper = min(contract.pick_count_max, len(candidates))
        requests = tuple(
            tuple(candidate.guid for candidate in selected)
            for count in range(contract.pick_count_min, upper + 1)
            for selected in combinations(candidates, count)
        )
    outputs: list[tuple[Plan3NativeState, Plan3CardUpgradeResult]] = []
    pauses: list[str] = []
    for selected in requests:
        if contract.branch is Plan3CardUpgradeBranch.ALL:
            result = execute_plan3_card_upgrade(
                ordinary,
                contract,
                database=database,
            )
        elif contract.branch is Plan3CardUpgradeBranch.RANDOM:
            result = execute_plan3_card_upgrade(
                ordinary,
                contract,
                rng_state=ordinary.random_state,
                database=database,
            )
        else:
            assert selected is not None
            result = execute_plan3_card_upgrade(
                ordinary,
                contract,
                selected_guids=selected,
                database=database,
            )
        if not result.applied:
            assert result.pause is not None
            pauses.append(
                f"{result.pause.code}:{result.pause.detail}".rstrip(":")
            )
            continue
        hand = list(result.state_after.hand)
        hand.insert(min(playing_index, len(hand)), playing)
        outputs.append(
            (replace(result.state_after, hand=tuple(hand)), result)
        )
    if not outputs:
        raise Plan3NativeStateError(
            "card-upgrade-effect-paused",
            pauses[0] if pauses else effect.id,
        )
    return tuple(outputs)


def _apply_native_card_create_id_effect(
    state: Plan3NativeState,
    effect: Plan3Effect,
    contract: CardCreateContract,
    *,
    playing_guid: str,
    hand_limit: int,
    path_token: tuple[str, ...],
    source_play_count: int,
    effect_sequence_index: int,
    guid_provider: NativeGuidProviderInput | None,
) -> tuple[Plan3NativeState, CardCreateResult]:
    """Execute one create slot with caller-owned, path-stable GUID tokens."""

    matches = tuple(
        (index, card)
        for index, card in enumerate(state.hand)
        if card.guid == playing_guid
    )
    if len(matches) != 1:
        raise Plan3NativeStateError("played-guid-not-in-hand", playing_guid)
    if guid_provider is None:
        raise Plan3NativeStateError(
            "card-create-guid-provider-missing", effect.id
        )
    if contract.count_min != contract.count_max:
        raise Plan3NativeStateError(
            "card-create-count-branch-unmodelled",
            f"{effect.id}:{contract.count_min}..{contract.count_max}",
        )
    playing_index, playing = matches[0]
    requests = tuple(
        Plan3NativeGuidRequest(
            path_token=path_token,
            source_guid=playing_guid,
            source_play_count=source_play_count,
            effect_id=effect.id,
            effect_sequence_index=effect_sequence_index,
            created_ordinal=ordinal,
        )
        for ordinal in range(contract.count_min)
    )
    tokens: list[str] = []
    try:
        for request in requests:
            if hasattr(guid_provider, "allocate"):
                token = guid_provider.allocate(request=request)  # type: ignore[union-attr]
            else:
                token = guid_provider(request)  # type: ignore[operator]
            if not isinstance(token, str) or not token.strip():
                raise CardCreateInputError(
                    "invalid-allocated-guid", str(request.created_ordinal)
                )
            tokens.append(token)
    except CardCreateInputError:
        raise
    except (TypeError, ValueError) as error:
        raise CardCreateInputError(
            "guid-provider-failed", f"{type(error).__name__}:{error}"
        ) from error
    if len(set(tokens)) != len(tokens):
        raise CardCreateInputError("duplicate-allocated-guid")
    existing = {card.guid for card in state.all_cards}
    collision = next((token for token in tokens if token in existing), None)
    if collision is not None:
        raise CardCreateInputError("guid-already-in-state", collision)

    ordinary = replace(
        state,
        hand=tuple(card for card in state.hand if card.guid != playing_guid),
    )
    result = apply_card_create_id(
        ordinary,
        contract,
        guid_tokens=tuple(tokens),
        hand_limit=hand_limit,
    )
    if not result.resolved:
        required = ",".join(result.branch.required_fields)
        raise Plan3NativeStateError(
            "card-create-effect-unresolved",
            f"{effect.id}:{result.branch.reason}:{required}",
        )
    hand = list(result.after.hand)
    hand.insert(min(playing_index, len(hand)), playing)
    return replace(result.after, hand=tuple(hand)), result


def _apply_native_card_create_search_effect(
    state: Plan3NativeState,
    effect: Plan3Effect,
    contract: CardCreateSearchEffectContract,
    *,
    playing_guid: str,
    plan_type: str,
    plan_ignore_card_ids: tuple[str, ...],
    hand_limit: int,
    path_token: tuple[str, ...],
    source_play_count: int,
    effect_sequence_index: int,
    guid_provider: NativeGuidProviderInput | None,
) -> tuple[Plan3NativeState, CardCreateSearchResult]:
    """Replay every native RNG call, allocating GUIDs only after count resolves."""

    matches = tuple(
        (index, card)
        for index, card in enumerate(state.hand)
        if card.guid == playing_guid
    )
    if len(matches) != 1:
        raise Plan3NativeStateError("played-guid-not-in-hand", playing_guid)
    playing_index, playing = matches[0]
    ordinary = replace(
        state,
        hand=tuple(card for card in state.hand if card.guid != playing_guid),
    )
    context = CardCreateSearchChanceInput(
        plan_type=plan_type,
        plan_ignore_card_ids=plan_ignore_card_ids,
        hand_limit=hand_limit,
    )
    probe = apply_card_create_search(
        ordinary,
        contract,
        execution_input=context,
    )
    required_count = 0
    if probe.unresolved:
        fields = probe.branch.required_fields
        if len(fields) != 1 or not fields[0].startswith("guid_tokens["):
            raise Plan3NativeStateError(
                "card-create-search-effect-unresolved",
                f"{effect.id}:{','.join(fields)}",
            )
        try:
            bounds = fields[0].removeprefix("guid_tokens[").removesuffix("]")
            start_text, end_text = bounds.split(":", 1)
            start, required_count = int(start_text), int(end_text)
        except (TypeError, ValueError) as error:
            raise Plan3NativeStateError(
                "card-create-search-guid-request-invalid", fields[0]
            ) from error
        if start != 0 or required_count <= 0:
            raise Plan3NativeStateError(
                "card-create-search-guid-request-invalid", fields[0]
            )
    if required_count and guid_provider is None:
        raise Plan3NativeStateError(
            "card-create-search-guid-provider-missing", effect.id
        )

    tokens: list[str] = []
    for ordinal in range(required_count):
        request = Plan3NativeGuidRequest(
            path_token=path_token,
            source_guid=playing_guid,
            source_play_count=source_play_count,
            effect_id=effect.id,
            effect_sequence_index=effect_sequence_index,
            created_ordinal=ordinal,
        )
        try:
            if hasattr(guid_provider, "allocate"):
                token = guid_provider.allocate(request=request)  # type: ignore[union-attr]
            else:
                token = guid_provider(request)  # type: ignore[operator]
        except (TypeError, ValueError) as error:
            raise CardCreateSearchInputError(
                "guid-provider-failed", f"{type(error).__name__}:{error}"
            ) from error
        if not isinstance(token, str) or not token.strip():
            raise CardCreateSearchInputError(
                "invalid-allocated-guid", str(ordinal)
            )
        tokens.append(token)
    if len(set(tokens)) != len(tokens):
        raise CardCreateSearchInputError("duplicate-allocated-guid")
    existing = {card.guid for card in state.all_cards}
    collision = next((token for token in tokens if token in existing), None)
    if collision is not None:
        raise CardCreateSearchInputError("guid-already-in-state", collision)

    result = (
        probe
        if not probe.unresolved
        else apply_card_create_search(
            ordinary,
            contract,
            execution_input=replace(context, guid_tokens=tuple(tokens)),
        )
    )
    if not result.resolved:
        raise Plan3NativeStateError(
            "card-create-search-effect-unresolved",
            f"{effect.id}:{','.join(result.branch.required_fields)}",
        )
    hand = list(result.after.hand)
    hand.insert(min(playing_index, len(hand)), playing)
    return replace(result.after, hand=tuple(hand)), result


def _rematch_card_move_targets(
    state: Plan3NativeState,
    targets: tuple[Plan3NativeCardMoveTarget, ...],
) -> tuple[Plan3NativeCardMoveTarget, ...]:
    """Refresh GUID-identical target objects after a preceding grow command."""

    zones = {
        "hand": state.hand,
        "draw": state.deck,
        "discard": state.grave,
        "hold": state.hold,
    }
    rematched: list[Plan3NativeCardMoveTarget] = []
    for target in targets:
        zone = zones.get(target.source_zone)
        if (
            zone is None
            or target.source_index >= len(zone)
            or zone[target.source_index].guid != target.guid
        ):
            raise Plan3NativeStateError(
                "card-move-source-position-stale", target.guid
            )
        rematched.append(replace(target, card=zone[target.source_index]))
    return tuple(rematched)


def apply_plan3_native_add_grow_effects(
    state: Plan3NativeState,
    effects: Iterable["Plan3Effect"],
    *,
    database: Path = DEFAULT_DATABASE,
    playing_guid: str | None = None,
    card_profile_by_card: Callable[
        [Plan3NativeCard], Plan3AddGrowCardProfile
    ] | None = None,
) -> tuple[Plan3NativeState, tuple[NiaAddGrowMutation, ...]]:
    """Apply shared AddGrow programs with native Playing/Hand semantics."""

    if not isinstance(state, Plan3NativeState):
        raise TypeError("state must be Plan3NativeState")
    if isinstance(effects, (str, bytes)):
        raise TypeError("effects must be an iterable of Plan3Effect values")
    ordered_effects = tuple(effects)
    if any(not isinstance(effect, Plan3Effect) for effect in ordered_effects):
        raise TypeError("effects must contain Plan3Effect values")
    if card_profile_by_card is not None and not callable(card_profile_by_card):
        raise TypeError("card_profile_by_card must be callable or None")

    playing: Plan3NativeCard | None = None
    playing_index: int | None = None
    ordinary = state
    if playing_guid is not None:
        matches = tuple(
            (index, card)
            for index, card in enumerate(state.hand)
            if card.guid == playing_guid
        )
        if len(matches) != 1:
            raise Plan3NativeStateError("played-guid-not-in-hand", playing_guid)
        playing_index, playing = matches[0]
        ordinary = replace(
            state,
            hand=tuple(
                card for card in state.hand if card.guid != playing_guid
            ),
        )
    deck_all = NiaDeckAllState(ordinary=ordinary, playing=playing)

    if card_profile_by_card is None:
        profile_cache: dict[
            tuple[str, int], Plan3AddGrowCardProfile
        ] = {}

        def resolve_profile(card: Plan3NativeCard) -> Plan3AddGrowCardProfile:
            key = (card.card_id, card.effective_upgrade)
            profile = profile_cache.get(key)
            if profile is None:
                profile = _plan3_add_grow_card_profile(
                    load_plan3_card(
                        card.card_id, card.effective_upgrade, database
                    )
                )
                profile_cache[key] = profile
            return profile

        card_profile_by_card = resolve_profile

    mutations: list[NiaAddGrowMutation] = []
    try:
        for effect in ordered_effects:
            if effect.effect_type != EFFECT_ADD_GROW or effect.card_move_rule is None:
                raise Plan3NativeStateError(
                    "unsupported-exam-add-grow-effect", effect.id
                )
            program = load_plan3_add_grow_program(
                effect.id, database=Path(database)
            )
            rule = effect.card_move_rule
            if (
                program.effect.search_id != rule.search_id
                or program.effect.grow_effect_ids
                != rule.card_grow_effect_ids
            ):
                raise Plan3NativeStateError(
                    "exam-add-grow-master-lineage-mismatch", effect.id
                )
            execution = execute_plan3_add_grow_program(
                deck_all,
                program,
                card_profile_by_card=card_profile_by_card,
            )
            deck_all = execution.state
            mutations.extend(execution.mutations)
    except NiaAddGrowContractError as error:
        raise Plan3NativeStateError(
            "unsupported-exam-add-grow-shape", str(error)
        ) from error
    except Plan3NativeStateError:
        raise
    except (KeyError, OSError, ValueError) as error:
        raise Plan3NativeStateError(
            "exam-add-grow-master-load", f"{type(error).__name__}:{error}"
        ) from error

    after = deck_all.ordinary
    if playing_index is not None:
        assert deck_all.playing is not None
        hand = list(after.hand)
        hand.insert(playing_index, deck_all.playing)
        after = replace(after, hand=tuple(hand))
    return after, tuple(mutations)


def execute_plan3_runtime_effect_events(
    scalar_before: Plan3State,
    turn_start: Plan3TurnStart,
    native_after_ordinary_draw: Plan3NativeState,
    *,
    hand_limit: int,
    lesson_type: str,
    hold_limit: int | None = None,
    support_upgrades: SupportInputs = (),
    support_card_searches: Mapping[str, ProduceCardSearchRule] | None = None,
    database: Path = DEFAULT_DATABASE,
) -> Plan3NativeRuntimeEffectReplay:
    """Replay native runtime mutations in the scalar event plan's exact order."""

    if not isinstance(scalar_before, Plan3State):
        raise TypeError("scalar_before must be Plan3State")
    if not isinstance(turn_start, Plan3TurnStart):
        raise TypeError("turn_start must be Plan3TurnStart")
    if not turn_start.supported:
        raise ValueError("turn_start must be supported before native replay")
    if turn_start.before != scalar_before:
        raise ValueError("turn_start.before does not match scalar_before")
    if not isinstance(native_after_ordinary_draw, Plan3NativeState):
        raise TypeError("native_after_ordinary_draw must be Plan3NativeState")
    searches = (
        {}
        if support_card_searches is None
        else dict(support_card_searches)
    )
    supports: SupportInputs
    if isinstance(support_upgrades, Mapping):
        supports = dict(support_upgrades)
    else:
        supports = tuple(support_upgrades)

    events = tuple(turn_start.runtime_effect_events)
    if tuple(event.sequence_index for event in events) != tuple(range(len(events))):
        raise Plan3NativeStateError("runtime-effect-sequence-invalid")
    working = native_after_ordinary_draw
    # Ordinary draw cannot alter Enthusiastic. Replay only the actual typed
    # stance-owner receipts, including its allocated UID/cursor, rather than
    # copying the scalar's final runtime to conceal an inconsistent source.
    if working.enthusiastic_runtime != scalar_before.enthusiastic_runtime:
        raise Plan3NativeStateError("turn-start-enthusiastic-source-mismatch")
    from .enthusiastic_runtime import replay_enthusiastic_add_receipts
    try:
        replayed = replay_enthusiastic_add_receipts(working.enthusiastic_runtime, turn_start.enthusiastic_receipts,
            cursor_before=scalar_before.next_status_uid, cursor_after=turn_start.after.next_status_uid)
    except (ValueError, TypeError) as error:
        raise Plan3NativeStateError("turn-start-enthusiastic-receipt-mismatch", str(error)) from error
    working = replace(working, enthusiastic_runtime=replayed)
    scalar_cursor = scalar_before
    runtime_draws: list[Plan3NativeRuntimeDrawTransition] = []
    runtime_card_upgrades: list[
        Plan3NativeRuntimeCardUpgradeTransition
    ] = []
    runtime_card_moves = []
    for event in events:
        working = working.apply_card_grow_events(scalar_cursor, event.before)
        if event.effect_type == EFFECT_ADD_GROW:
            working, _mutations = apply_plan3_native_add_grow_effects(
                working,
                (event.effect,),
                database=database,
            )
        elif event.effect_type == EFFECT_CARD_DRAW:
            assert event.requested_count is not None
            transition = working.draw_to_hand(
                event.requested_count,
                hand_limit=hand_limit,
                lesson_type=lesson_type,
                support_upgrades=supports,
                support_card_searches=searches,
                effect_draw=True,
            )
            runtime_draws.append(
                Plan3NativeRuntimeDrawTransition(event, transition)
            )
            working = transition.after
        elif event.effect_type == EFFECT_CARD_MOVE:
            from .plan3_startplay_idol_return import return_idol_to_hand
            if hold_limit is None:
                raise Plan3NativeStateError("runtime-card-move-hold-limit-unavailable", event.effect_id)
            if event.phase != "ProduceExamPhaseType_StartPlay":
                raise Plan3NativeStateError("runtime-card-move-phase-unresolved", event.effect_id)
            moved, trace = return_idol_to_hand(working, event.effect, hand_limit=hand_limit, hold_limit=hold_limit,
                is_full_power=event.before.stance == STANCE_FULL_POWER, lesson_type=lesson_type,
                support_upgrades=supports, support_card_searches=searches, database=database)
            dispatched = dispatch_plan3_guid_move_commits(working, moved, trace.target_guids,
                hand_limit=hand_limit, hold_limit=hold_limit, lesson_type=lesson_type,
                is_full_power=event.before.stance == STANCE_FULL_POWER, support_upgrades=supports,
                support_card_searches=searches, database=Path(database))
            if len(dispatched) != 1:
                raise Plan3NativeStateError("runtime-card-move-callback-branching-unresolved", event.effect_id)
            working = dispatched[0].state
            runtime_card_moves.append({"sequence_index": event.sequence_index, "move": trace,
                                       "callbacks": dispatched[0].traces})
        elif event.effect_type == EFFECT_CARD_UPGRADE:
            resolution = resolve_plan3_card_upgrade(
                event.effect_id,
                database=database,
            )
            if not resolution.resolved or resolution.contract is None:
                assert resolution.pause is not None
                raise Plan3NativeStateError(
                    "runtime-card-upgrade-unresolved",
                    (
                        f"{resolution.pause.code}:"
                        f"{resolution.pause.detail}"
                    ).rstrip(":"),
                )
            contract = resolution.contract
            if contract.branch is Plan3CardUpgradeBranch.SELECT:
                raise Plan3NativeStateError(
                    "runtime-card-upgrade-select-unmodelled",
                    event.effect_id,
                )
            result = execute_plan3_card_upgrade(
                working,
                contract,
                rng_state=(
                    working.random_state
                    if contract.branch is Plan3CardUpgradeBranch.RANDOM
                    else None
                ),
                database=database,
            )
            if not result.applied:
                assert result.pause is not None
                raise Plan3NativeStateError(
                    "runtime-card-upgrade-paused",
                    (
                        f"{result.pause.code}:{result.pause.detail}"
                    ).rstrip(":"),
                )
            runtime_card_upgrades.append(
                Plan3NativeRuntimeCardUpgradeTransition(event, result)
            )
            working = result.state_after
        working = working.apply_card_grow_events(event.before, event.after)
        scalar_cursor = event.after
    working = working.apply_card_grow_events(scalar_cursor, turn_start.after)
    if working.enthusiastic_runtime != turn_start.after.enthusiastic_runtime:
        raise Plan3NativeStateError("turn-start-enthusiastic-receipt-chain-incomplete")
    return Plan3NativeRuntimeEffectReplay(
        state=working,
        draws=tuple(runtime_draws),
        card_upgrades=tuple(runtime_card_upgrades),
        card_moves=tuple(runtime_card_moves),
    )


def replay_plan3_runtime_effect_events(
    scalar_before: Plan3State,
    turn_start: Plan3TurnStart,
    native_after_ordinary_draw: Plan3NativeState,
    *,
    hand_limit: int,
    lesson_type: str,
    hold_limit: int | None = None,
    support_upgrades: SupportInputs = (),
    support_card_searches: Mapping[str, ProduceCardSearchRule] | None = None,
    database: Path = DEFAULT_DATABASE,
) -> tuple[
    Plan3NativeState,
    tuple[Plan3NativeRuntimeDrawTransition, ...],
]:
    """Compatibility projection of the complete typed runtime replay."""

    replay = execute_plan3_runtime_effect_events(
        scalar_before,
        turn_start,
        native_after_ordinary_draw,
        hand_limit=hand_limit,
        lesson_type=lesson_type,
        hold_limit=hold_limit,
        support_upgrades=support_upgrades,
        support_card_searches=support_card_searches,
        database=database,
    )
    return replay.state, replay.draws


def _apply_native_runtime_growth(
    card: Plan3Card, native_card: "Plan3NativeCard"
) -> Plan3Card:
    """Apply GUID-bound materialized and customization grow layers."""

    from .plan3_ordered_customization import append_customization_effects
    from .card_effect_predicates import is_block_icon_value_effect_type
    # Native pre-affect pass creates EffectAdd children before numeric grows;
    # an added Lesson therefore receives the same later LessonAdd layer.
    card=append_customization_effects(card,native_card.runtime_customization)

    lesson_add = (
        native_card.runtime_lesson_add
        + native_card.runtime_customize_lesson_add
    )
    lesson_count_add = native_card.runtime_lesson_count_add
    customization = native_card.runtime_customization
    customize_block_add = customization.block_add
    customize_cost_reduce = customization.cost_reduce
    customize_penetrate_reduce = customization.cost_penetrate_reduce_effects
    customize_stamina_down_turn_add = customization.stamina_consumption_down_turn_add
    runtime_block_add = native_card.runtime_block_add
    full_power_point_add = native_card.runtime_full_power_point_add
    full_power_cost_add = native_card.runtime_full_power_point_cost_add
    stamina_cost_add = native_card.runtime_cost_add
    stamina_cost_reduce = native_card.runtime_cost_reduce
    penetrate_cost_reduce = native_card.runtime_cost_penetrate_reduce
    penetrate_cost_add = native_card.runtime_cost_penetrate_add
    if not any(
        (
            lesson_add,
            lesson_count_add,
            customize_block_add,
            customize_cost_reduce,
            customize_penetrate_reduce,
            customize_stamina_down_turn_add,
            runtime_block_add,
            full_power_point_add,
            full_power_cost_add,
            stamina_cost_add,
            stamina_cost_reduce,
            penetrate_cost_reduce,
            penetrate_cost_add,
        )
    ):
        return card
    lesson_matches = tuple(
        index
        for index, effect in enumerate(card.effects)
        if effect.effect_type in (EFFECT_LESSON, EFFECT_LESSON_FULL_POWER_POINT)
    )
    # Android v3.2.3 routes ProduceCardGrowEffectType_LessonAdd through
    # GrowEffectAffectProduceCardEffectList's
    # Where(IsLessonIconValueEffectType).ForEach(+value) branch.  An empty
    # match set is therefore a native no-op (the GUID-bound grow lineage is
    # still retained), and every matching lesson-value effect is updated.
    # LessonCountAdd uses a different native branch whose multi/no-target
    # behaviour is not covered by that proof, so keep its strict boundary.
    if lesson_count_add and len(lesson_matches) != 1:
        raise Plan3NativeStateError(
            "runtime-lesson-add-target-unsupported",
            f"{native_card.guid}:{card.id}:lesson_effects={len(lesson_matches)}",
        )
    block_matches = tuple(
        index
        for index, effect in enumerate(card.effects)
        if is_block_icon_value_effect_type(effect.effect_type)
    )
    full_power_point_matches = tuple(
        index
        for index, effect in enumerate(card.effects)
        if effect.effect_type == EFFECT_FULL_POWER_POINT
    )
    effects = list(card.effects)
    if customize_stamina_down_turn_add:
        matches = [index for index, effect in enumerate(effects) if effect.effect_type == EFFECT_STAMINA_CONSUMPTION_DOWN]
        if not matches:
            raise Plan3NativeStateError("runtime-customization-stamina-down-turn-target-unsupported", native_card.guid)
        for index in matches:
            if effects[index].effect_turn <= 0:
                raise Plan3NativeStateError("runtime-customization-stamina-down-duration-unsupported", native_card.guid)
            effects[index] = replace(effects[index], effect_turn=effects[index].effect_turn + customize_stamina_down_turn_add)
    if lesson_add:
        for index in lesson_matches:
            effects[index] = replace(
                effects[index], value1=effects[index].value1 + lesson_add
            )
    if lesson_count_add:
        index = lesson_matches[0]
        effects[index] = replace(
            effects[index],
            effect_count=effects[index].effect_count + lesson_count_add,
        )
    for block_add in (customize_block_add, runtime_block_add):
        if not block_add:
            continue
        # Native BlockAdd/Reduce visits Where(IsBlockIconValueEffectType)
        # with ForEach, so an empty match set is a no-op and all matches
        # are updated. The grow remains on the GUID; a later EffectAdd or
        # upgrade may introduce a matching effect on the next getter call.
        for index in block_matches:
            effects[index] = replace(
                effects[index], value1=max(1, effects[index].value1 + block_add)
            )
    # FullPowerPointAdd targets the card's ExamFullPowerPoint effect value;
    # it is unrelated to CostFullPowerPointAdd below.  Native's typed grow
    # branch is a no-op when the card has no matching effect, while the
    # GUID-owned lineage remains on the native card.
    if full_power_point_add:
        for index in full_power_point_matches:
            effects[index] = replace(
                effects[index],
                value1=effects[index].value1 + full_power_point_add,
            )
    stamina_cost = card.stamina_cost
    force_stamina_cost = card.force_stamina_cost
    customize_stamina_grow = tuple(
        StaminaGrowEffect(
            StaminaGrowEffectType.COST_REDUCE,
            entry.value,
        )
        for entry in customization.cost_reduce_effects
    )
    if customize_stamina_grow and (
        card.cost_type != COST_STAMINA or force_stamina_cost > 0
    ):
        raise Plan3NativeStateError(
            "runtime-customization-cost-reduce-target-unsupported",
            f"{native_card.guid}:{card.id}:{card.cost_type}",
        )
    customize_penetrate_grow = tuple(
        StaminaGrowEffect(StaminaGrowEffectType.COST_PENETRATE_REDUCE, entry.value)
        for entry in customize_penetrate_reduce
    )
    if customize_penetrate_grow and (card.cost_type != COST_STAMINA or force_stamina_cost <= 0):
        raise Plan3NativeStateError(
            "runtime-customization-cost-penetrate-reduce-target-unsupported",
            f"{native_card.guid}:{card.id}:{card.cost_type}",
        )
    if card.cost_type == COST_STAMINA:
        if force_stamina_cost > 0:
            force_stamina_cost = get_stamina_cost(
                force_stamina_cost,
                penetrate=True,
                master_grow=customize_penetrate_grow,
                runtime_grow=(
                    StaminaGrowEffect(
                        StaminaGrowEffectType.COST_PENETRATE_REDUCE,
                        penetrate_cost_reduce,
                    ),
                    StaminaGrowEffect(
                        StaminaGrowEffectType.COST_PENETRATE_ADD,
                        penetrate_cost_add,
                    ),
                ),
            )
        else:
            stamina_cost = get_stamina_cost(
                stamina_cost,
                penetrate=False,
                master_grow=customize_stamina_grow,
                runtime_grow=(
                    StaminaGrowEffect(
                        StaminaGrowEffectType.COST_REDUCE,
                        stamina_cost_reduce,
                    ),
                    StaminaGrowEffect(
                        StaminaGrowEffectType.COST_ADD,
                        stamina_cost_add,
                    ),
                ),
            )
    return replace(
        card,
        stamina_cost=stamina_cost,
        force_stamina_cost=force_stamina_cost,
        cost_value=card.cost_value + full_power_cost_add,
        effects=tuple(effects),
    )


# Replay/runtime bridges must use the same GUID-bound grow application as the
# native search path; this public alias avoids copying that effect logic.
apply_plan3_native_runtime_growth = _apply_native_runtime_growth


@dataclass(frozen=True, slots=True)
class Plan3EncoreForcedReplay:
    """One exact target-self UsePool transaction emitted by Encore."""

    listener_uid: int
    captured_guid: str
    before: Plan3State
    after: Plan3State
    native_before: Plan3NativeState
    native_after: Plan3NativeState
    transition: Plan3Transition
    executed_effect_ids: tuple[str, ...]
    listener_count_spent_before_queue: bool = True
    source_zone: str = "lost"
    destination_zone: str = "lost"
    used_playing_stage: bool = True
    consumes_cost: bool = False
    consumes_playable_count: bool = False
    is_manual_play: bool = False
    global_card_play_count_delta: int = 0
    card_play_count_before: int = 0
    card_play_count_after: int = 0
    accepted_play_extension_trace: object | None = None
    accepted_play_add_grow_mutations: tuple[NiaAddGrowMutation, ...] = ()

    @property
    def legal(self) -> bool:
        return self.transition.legal

    @property
    def once_installer_skipped(self) -> bool:
        return all(
            effect.id not in self.executed_effect_ids
            for effect in self.transition.card.effects
            if effect.once
        )


@dataclass(frozen=True, slots=True)
class Plan3ExactEncoreEndTurnResult:
    """Atomic result of scheduling/executing exact Encore listeners."""

    before: Plan3State
    after: Plan3State
    native_before: Plan3NativeState
    native_after: Plan3NativeState
    supported: bool
    unsupported_rules: tuple[str, ...] = ()
    replays: tuple[Plan3EncoreForcedReplay, ...] = ()
    extension_state: object | None = None


def execute_plan3_exact_encore_end_turn(
    state: Plan3State,
    native_state: Plan3NativeState,
    *,
    settings: Plan3ExamSettings | None = None,
    database: Path = DEFAULT_DATABASE,
    accepted_play_extension: Plan3NativeAcceptedPlayExtension | None = None,
    initial_extension_state: object | None = None,
) -> Plan3ExactEncoreEndTurnResult:
    """Execute only ``p_card-03-ido-3_197`` Encore UsePool commands.

    Listener candidates are snapshotted, each count is spent before its
    nested enum-24 command, and any unproved Master/native shape rolls the
    entire batch back to the supplied scalar/native pair.
    """

    from .plan3_encore_enthusiastic_lesson import (
        CARD_VERSION_BY_UPGRADE,
        EncoreEndTurnInput,
        EncoreListener,
        load_encore_contract,
        plan_encore_end_turn_replay,
    )

    if not isinstance(state, Plan3State):
        raise TypeError("state must be Plan3State")
    if not isinstance(native_state, Plan3NativeState):
        raise TypeError("native_state must be Plan3NativeState")
    if accepted_play_extension is not None and not isinstance(
        accepted_play_extension, Plan3NativeAcceptedPlayExtension
    ):
        raise TypeError(
            "accepted_play_extension must implement "
            "Plan3NativeAcceptedPlayExtension or be None"
        )
    if initial_extension_state is not None:
        try:
            hash(initial_extension_state)
        except TypeError as error:
            raise TypeError("initial_extension_state must be hashable") from error
    state.validate(allow_completed=True)
    native_state.assert_plan3_projection(state)
    if settings is None:
        settings = load_plan3_exam_settings()

    def unresolved(*rules: str) -> Plan3ExactEncoreEndTurnResult:
        return Plan3ExactEncoreEndTurnResult(
            state,
            state,
            native_state,
            native_state,
            False,
            tuple(dict.fromkeys(rules)),
            extension_state=initial_extension_state,
        )

    candidates = tuple(
        enchant
        for enchant in state.active_status_enchants
        if enchant.is_encore_enchant
        or enchant.rule.id == EXACT_ENCORE_STATUS_ID
    )
    if not candidates:
        return Plan3ExactEncoreEndTurnResult(
            state,
            state,
            native_state,
            native_state,
            True,
            extension_state=initial_extension_state,
        )
    try:
        contract = load_encore_contract(Path(database))
    except (OSError, ValueError) as error:
        return unresolved(f"exact-encore-contract:{error}")
    if not contract.executable:
        return unresolved(
            *(f"exact-encore-contract:{reason}" for reason in contract.unresolved_reasons)
        )

    working = state
    native = native_state
    extension_state = initial_extension_state
    replays: list[Plan3EncoreForcedReplay] = []
    for snapshot in candidates:
        current = next(
            (
                enchant
                for enchant in working.active_status_enchants
                if enchant.instance_id == snapshot.instance_id
            ),
            None,
        )
        if current is None:
            continue
        if (
            not current.is_encore_enchant
            or current.rule != contract.rule
            or current.source_id != EXACT_ENCORE_CARD_ID
            or current.native_uid <= 0
            or not current.captured_card_guid
        ):
            return unresolved(
                f"exact-encore-listener-shape:{current.instance_id}"
            )
        matches = tuple(
            card
            for card in native.lost
            if card.guid == current.captured_card_guid
        )
        if len(matches) != 1:
            return unresolved(
                f"exact-encore-captured-guid-not-in-lost:{current.captured_card_guid}"
            )
        target = matches[0]
        planned = plan_encore_end_turn_replay(
            native,
            EncoreListener(
                current.native_uid,
                target,
                current,
                is_encore_enchant=True,
            ),
            EncoreEndTurnInput(
                remaining_turns=working.turns_remaining,
                once_effect_consumed=(target.play_count > 0),
            ),
            database=Path(database),
        )
        if not planned.resolved:
            return unresolved(f"exact-encore-plan:{planned.reason}")
        if not planned.triggered:
            continue
        if len(planned.commands) != 1:
            return unresolved("exact-encore-command-count")

        spent = planned.after_listener.active
        active = tuple(
            enchant
            for enchant in working.active_status_enchants
            if enchant.instance_id != current.instance_id
        )
        if not planned.remove_after_trigger_batch:
            active = tuple(
                spent if enchant.instance_id == current.instance_id else enchant
                for enchant in working.active_status_enchants
            )
        spent_state = replace(working, active_status_enchants=active)

        expected = CARD_VERSION_BY_UPGRADE.get(target.effective_upgrade)
        if expected is None or target.card_id != EXACT_ENCORE_CARD_ID:
            return unresolved("exact-encore-target-outside-family")
        try:
            card = load_plan3_card(
                target.card_id, target.effective_upgrade, Path(database)
            )
            card = _apply_native_runtime_growth(card, target)
        except (KeyError, OSError, ValueError, Plan3NativeStateError) as error:
            code = getattr(error, "code", type(error).__name__)
            detail = getattr(error, "detail", str(error))
            return unresolved(
                f"exact-encore-target-runtime:{code}:{detail}".rstrip(":")
            )
        if tuple(effect.id for effect in card.effects) != expected.ordered_effect_ids:
            return unresolved("exact-encore-target-effect-order-drift")
        managed_interval_ids: tuple[str, ...] = ()
        if accepted_play_extension is not None:
            try:
                managed_interval_ids = tuple(
                    accepted_play_extension.managed_play_count_interval_instance_ids(
                        spent_state,
                        extension_state,
                    )
                )
                if (
                    any(
                        not isinstance(value, str) or not value
                        for value in managed_interval_ids
                    )
                    or len(managed_interval_ids)
                    != len(set(managed_interval_ids))
                ):
                    return unresolved(
                        "exact-encore-accepted-extension-managed-listeners"
                    )
            except (TypeError, ValueError) as error:
                return unresolved(
                    "exact-encore-accepted-extension-managed-listeners:"
                    f"{type(error).__name__}:{error}"
                )
        pre_direct_transform = None
        pre_direct_captures: list[_Plan3AcceptedPreDirectCapture] = []
        if accepted_play_extension is not None:
            (
                pre_direct_transform,
                pre_direct_captures,
            ) = _accepted_pre_direct_transform(
                scalar_before=spent_state,
                native_state=native,
                playing_guid=target.guid,
                compiled_card=card,
                extension=accepted_play_extension,
                extension_state=extension_state,
                settings=settings,
                source=Plan3NativeAcceptedPlaySource.EXTRA,
                managed_interval_ids=managed_interval_ids,
                database=Path(database),
                card_profile_by_card=(
                    lambda native_card: _plan3_add_grow_card_profile(
                        load_plan3_card(
                            native_card.card_id,
                            native_card.effective_upgrade,
                            Path(database),
                        )
                    )
                ),
            )
        transition = apply_plan3_card(
            spent_state,
            card,
            settings=settings,
            prior_play_count=target.play_count,
            playing_guid=target.guid,
            forced_replay=True,
            externally_handled_play_count_interval_instance_ids=(
                managed_interval_ids
            ),
            pre_direct_transform=pre_direct_transform,
        )
        if not transition.supported or transition.unverified_rules:
            return unresolved(
                *(
                    transition.unsupported_rules
                    or tuple(
                        f"unverified:{rule}"
                        for rule in transition.unverified_rules
                    )
                    or ("exact-encore-engine-unsupported",)
                )
            )
        executed_effect_ids = tuple(
            effect.id for effect in card.effects if not effect.once
        )
        if not transition.legal:
            replay = Plan3EncoreForcedReplay(
                current.native_uid,
                target.guid,
                working,
                spent_state,
                native,
                native,
                transition,
                (),
                global_card_play_count_delta=0,
                card_play_count_before=target.play_count,
                card_play_count_after=target.play_count,
            )
            working = spent_state
            replays.append(replay)
            continue
        if transition.native_add_grow_effects or transition.native_deferred_effects:
            return unresolved("exact-encore-direct-native-effect-unexpected")
        unsupported_runtime = tuple(
            event.effect.id
            for event in transition.runtime_effect_events
            if event.effect_type in {EFFECT_CARD_DRAW, EFFECT_CARD_UPGRADE}
        )
        if unsupported_runtime:
            return unresolved(
                *(f"exact-encore-runtime-native-effect:{value}" for value in unsupported_runtime)
            )
        try:
            native_after = native
            accepted_execution: (
                Plan3NativeAcceptedPlayExtensionResult | None
            ) = None
            if pre_direct_transform is not None:
                if len(pre_direct_captures) != 1:
                    raise Plan3NativeStateError(
                        "exact-encore-pre-direct-capture-arity",
                        str(len(pre_direct_captures)),
                    )
                capture = pre_direct_captures[0]
                accepted_execution = capture.execution
                native_after = capture.native_state
                extension_state = accepted_execution.extension_state
            for event in transition.runtime_effect_events:
                if (
                    event.effect_type != EFFECT_ADD_GROW
                    or (
                        pre_direct_transform is not None
                        and event.phase == NATIVE_PHASE_CARD_PLAY
                    )
                ):
                    continue
                native_after, _mutations = apply_plan3_native_add_grow_effects(
                    native_after,
                    (event.effect,),
                    database=Path(database),
                    playing_guid=target.guid,
                    card_profile_by_card=(
                        lambda native_card: _plan3_add_grow_card_profile(
                            load_plan3_card(
                                native_card.card_id,
                                native_card.effective_upgrade,
                                Path(database),
                            )
                        )
                    ),
                )
            native_after = native_after.apply_card_grow_events(
                spent_state, transition.after
            )
            native_after = native_after.play_lost_by_guid(
                target.guid, card.move_position_type
            )
            synchronized = _synchronize_zones(transition.after, native_after)
            (
                native_after,
                _card_play_after_mutations,
                skipped_listener_guids,
            ) = _apply_exact_card_play_after_listeners(
                synchronized,
                native_after,
                target.guid,
                card,
                database=Path(database),
            )
            native_after = native_after.apply_card_play_after_grow_events(
                target.guid,
                is_full_power=(transition.after.stance == STANCE_FULL_POWER),
                played_effect_group_ids=card.effect_group_ids,
                skip_listener_guids=skipped_listener_guids,
            )
            synchronized = _synchronize_zones(transition.after, native_after)
            native_after.assert_plan3_projection(synchronized)
        except (KeyError, OSError, ValueError, Plan3NativeStateError) as error:
            code = getattr(error, "code", type(error).__name__)
            detail = getattr(error, "detail", str(error))
            return unresolved(
                f"exact-encore-native-transaction:{code}:{detail}".rstrip(":")
            )
        settled_target = next(
            card for card in native_after.all_cards if card.guid == target.guid
        )
        variant_transition = replace(transition, after=synchronized)
        replays.append(
            Plan3EncoreForcedReplay(
                current.native_uid,
                target.guid,
                working,
                synchronized,
                native,
                native_after,
                variant_transition,
                executed_effect_ids,
                global_card_play_count_delta=1,
                card_play_count_before=target.play_count,
                card_play_count_after=settled_target.play_count,
                accepted_play_extension_trace=(
                    None
                    if accepted_execution is None
                    else accepted_execution.trace
                ),
                accepted_play_add_grow_mutations=(
                    ()
                    if accepted_execution is None
                    else accepted_execution.add_grow_mutations
                ),
            )
        )
        working = synchronized
        native = native_after

    return Plan3ExactEncoreEndTurnResult(
        state,
        working,
        native_state,
        native,
        True,
        replays=tuple(replays),
        extension_state=extension_state,
    )


def search_plan3_native(
    initial_state: Plan3State,
    initial_native_state: Plan3NativeState,
    *,
    beam_width: int = 64,
    depth: int | None = None,
    settings: Plan3ExamSettings | None = None,
    gimmick_profile: Plan3GimmickProfile | None = None,
    objective: Objective | None = None,
    database: Path = DEFAULT_DATABASE,
    support_upgrades: SupportInputs = (),
    support_card_searches: Mapping[str, ProduceCardSearchRule] | None = None,
    battle_parameter_schedule: tuple[int, ...] | None = None,
    battle_ranking_resolved: bool = False,
    include_skip: bool = False,
    force_end_score: int = 0,
    force_end_stamina_recovery: int = 0,
    drink_inventory: Plan3DrinkInventory | None = None,
    turn_start_extension: Plan3NativeTurnStartExtension | None = None,
    accepted_play_extension: Plan3NativeAcceptedPlayExtension | None = None,
    initial_turn_start_extension_state: object | None = None,
    guid_provider: NativeGuidProviderInput | None = None,
    plan_type: str = PLAN3,
    plan_ignore_card_ids: tuple[str, ...] = (),
    hand_limit: int | None = None,
    _use_pool_command: Plan3UsePoolCommand | None = None,
    _use_pool_recursion_depth: int = 0,
    _root_only: bool = False,
    imitation_tie_breaker: Plan3NativePathTieBreaker | None = None,
    _single_action: Plan3NativeSearchAction | None = None,
    _single_turn_end: bool = False,
) -> Plan3NativeSearchResult:
    """Search exact Plan 3 states while preserving GUID/RNG/support lineage.

    Unlike the scalar search, no future-upgrade callback or authority flag is
    accepted.  Native HandAdd evaluation resolves the effective drawn cards
    directly from the RNG and supplied static support inputs.  A battle
    parameter schedule starts at the current turn: element zero must match
    ``initial_state.current_parameter_type`` and each following element is
    installed before that future turn's StartTurn processing.  Optional skip,
    audition force-end, and immutable drink inventory inputs add non-card
    decisions without replacing the existing card kernel.  ``depth`` keeps
    its original meaning and counts card plays only; use ``None`` for a full
    horizon containing skip and drink decisions.  A mode-specific turn-start
    extension is path-local and runs after Full Power/Hold resolution but
    before the authoritative native draw.  A mode-specific accepted-play
    extension delegates exact phase-23 listener instances after payment and
    ordinary CardPlay native effects, while the current card keeps the rule
    compiled from its pre-listener PlayingCard snapshot.

    ``_root_only`` is an internal adapter mode used by the legal-candidate
    publisher.  It returns every first decision branch before the normal
    beam/dedup/principal-path pruning and never recurses into a future
    decision.  It is intentionally private so ordinary horizon callers keep
    the historical search contract.  ``imitation_tie_breaker`` is appended
    after the exact native objective in path ordering; it cannot alter native
    legality, transitions, RNG or terminal values.

    ``_single_action`` is an internal execution mode for a caller that already
    owns one settled native card action (for example a replay cursor).  It
    expands exactly that GUID/ref and does not enumerate other cards, drinks,
    or a second user decision.  The ordinary boundary scheduler remains in
    charge, so a card that consumes the final play crosses EndTurn and the
    following TurnStart with the same draw/support/RNG path as full search.
    The switch accepts no post-action state; the result is derived solely from
    the supplied state and action.

    ``_single_turn_end`` is the equivalent narrow mode for one explicit
    manual END_TURN.  It expands only the existing skip branch, lets the
    ordinary Encore/EndTurn/optional TurnStart scheduler settle once, and
    publishes that boundary without enumerating a fresh decision.
    """

    if (
        not isinstance(beam_width, int)
        or isinstance(beam_width, bool)
        or beam_width < 1
    ):
        raise ValueError("beam_width must be a positive integer")
    if depth is not None and (
        not isinstance(depth, int) or isinstance(depth, bool) or depth < 0
    ):
        raise ValueError("depth must be a non-negative integer or None")
    if not isinstance(initial_native_state, Plan3NativeState):
        raise TypeError("initial_native_state must be Plan3NativeState")
    if _use_pool_command is not None and not isinstance(
        _use_pool_command, Plan3UsePoolCommand
    ):
        raise TypeError("_use_pool_command must be Plan3UsePoolCommand or None")
    if (
        not isinstance(_use_pool_recursion_depth, int)
        or isinstance(_use_pool_recursion_depth, bool)
        or not 0 <= _use_pool_recursion_depth <= 8
    ):
        raise ValueError("_use_pool_recursion_depth must be between 0 and 8")
    if _use_pool_command is not None and _use_pool_recursion_depth == 0:
        raise ValueError("UsePool command mode requires positive recursion depth")
    if type(_root_only) is not bool:
        raise TypeError("_root_only must be bool")
    if imitation_tie_breaker is not None and not callable(imitation_tie_breaker):
        raise TypeError("imitation_tie_breaker must be callable or None")
    if _single_action is not None and not isinstance(
        _single_action, Plan3NativeSearchAction
    ):
        raise TypeError("_single_action must be Plan3NativeSearchAction or None")
    if type(_single_turn_end) is not bool:
        raise TypeError("_single_turn_end must be bool")
    if _single_action is not None and _root_only:
        raise ValueError("_single_action cannot be combined with _root_only")
    if _single_action is not None and _use_pool_command is not None:
        raise ValueError("_single_action cannot be combined with UsePool")
    if _single_turn_end and _single_action is not None:
        raise ValueError("_single_turn_end cannot be combined with _single_action")
    if _single_turn_end and _root_only:
        raise ValueError("_single_turn_end cannot be combined with _root_only")
    if _single_turn_end and _use_pool_command is not None:
        raise ValueError("_single_turn_end cannot be combined with UsePool")
    if _single_turn_end and not include_skip:
        raise ValueError("_single_turn_end requires include_skip")
    if _root_only and _use_pool_command is not None:
        raise ValueError("root-only enumeration cannot run a forced UsePool command")
    if not isinstance(include_skip, bool):
        raise TypeError("include_skip must be bool")
    if not isinstance(battle_ranking_resolved, bool):
        raise TypeError("battle_ranking_resolved must be bool")
    for value, label in (
        (force_end_score, "force_end_score"),
        (force_end_stamina_recovery, "force_end_stamina_recovery"),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{label} must be a non-negative integer")
    if force_end_stamina_recovery and not force_end_score:
        raise ValueError(
            "force_end_stamina_recovery requires force_end_score"
        )
    if (
        force_end_stamina_recovery
        and initial_state.max_stamina is None
    ):
        raise ValueError(
            "force-end stamina recovery requires authoritative max_stamina"
        )
    if drink_inventory is not None and not isinstance(
        drink_inventory, Plan3DrinkInventory
    ):
        raise TypeError("drink_inventory must be Plan3DrinkInventory or None")
    if (
        turn_start_extension is None
        and accepted_play_extension is None
        and initial_turn_start_extension_state is not None
    ):
        raise ValueError(
            "initial extension state requires a turn-start or accepted-play extension"
        )
    if turn_start_extension is not None and not callable(turn_start_extension):
        raise TypeError("turn_start_extension must be callable or None")
    if accepted_play_extension is not None and not isinstance(
        accepted_play_extension, Plan3NativeAcceptedPlayExtension
    ):
        raise TypeError(
            "accepted_play_extension must implement "
            "Plan3NativeAcceptedPlayExtension or be None"
        )
    if turn_start_extension is not None and gimmick_profile is not None:
        raise ValueError(
            "turn_start_extension and gimmick_profile are mutually exclusive"
        )
    if guid_provider is not None and not (
        isinstance(guid_provider, Plan3NativeGuidProvider)
        or callable(guid_provider)
    ):
        raise TypeError("guid_provider must be callable or expose allocate()")
    if not isinstance(plan_type, str) or not plan_type:
        raise TypeError("plan_type must be non-empty text")
    if not isinstance(plan_ignore_card_ids, tuple) or any(
        not isinstance(card_id, str) or not card_id
        for card_id in plan_ignore_card_ids
    ):
        raise TypeError(
            "plan_ignore_card_ids must be a tuple of non-empty card IDs"
        )
    if hand_limit is not None and (
        not isinstance(hand_limit, int)
        or isinstance(hand_limit, bool)
        or hand_limit < 0
    ):
        raise ValueError("hand_limit must be a non-negative integer or None")
    if initial_turn_start_extension_state is not None:
        try:
            hash(initial_turn_start_extension_state)
        except TypeError as error:
            raise TypeError(
                "initial_turn_start_extension_state must be hashable"
            ) from error
    initial_state.validate(allow_completed=True)
    initial_native_state.assert_plan3_projection(initial_state)
    use_pool_stage: Plan3UsePoolStage | None = None
    if _use_pool_command is not None:
        use_pool_stage = stage_plan3_use_pool_guid(
            initial_state, initial_native_state, _use_pool_command
        )
        initial_state = use_pool_stage.scalar_playing
        initial_native_state = use_pool_stage.native_playing
        depth = 1
        include_skip = False
        drink_inventory = Plan3DrinkInventory()
    initial_drinks = (
        Plan3DrinkInventory()
        if drink_inventory is None
        else drink_inventory
    )
    if battle_parameter_schedule is None:
        battle_schedule: tuple[int, ...] | None = None
    else:
        if not isinstance(battle_parameter_schedule, tuple):
            raise TypeError("battle_parameter_schedule must be a tuple or None")
        if not initial_state.is_battle:
            raise ValueError(
                "battle_parameter_schedule requires an initial battle state"
            )
        if not battle_parameter_schedule:
            raise ValueError("battle_parameter_schedule must not be empty")
        if any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value not in {1, 2, 3}
            for value in battle_parameter_schedule
        ):
            raise ValueError(
                "battle_parameter_schedule entries must be Vocal/Dance/Visual (1/2/3)"
            )
        if battle_parameter_schedule[0] != initial_state.current_parameter_type:
            raise ValueError(
                "battle_parameter_schedule[0] must match the current parameter type"
            )
        battle_schedule = battle_parameter_schedule
    if settings is None:
        settings = load_plan3_exam_settings()
    if hand_limit is None:
        card_create_hand_limit = settings.hand_limit
    elif hand_limit != settings.hand_limit:
        raise ValueError("hand_limit must match settings.hand_limit")
    else:
        card_create_hand_limit = hand_limit
    searches = {} if support_card_searches is None else dict(support_card_searches)
    if isinstance(support_upgrades, Mapping):
        supports: SupportInputs = dict(support_upgrades)
    else:
        supports = tuple(support_upgrades)

    plan3_card_cache: dict[tuple[str, int], Plan3Card] = {}
    master_card_cache: dict[tuple[str, int], MasterCard] = {}
    card_move_search_cache: dict[str, ProduceCardSearchRule] = {}
    card_create_contract_cache: dict[str, CardCreateContract] = {}
    card_create_search_contract_cache: dict[
        str, CardCreateSearchEffectContract
    ] = {}
    diagnostics: list[Plan3NativeSearchDiagnostic] = []
    finished: list[Plan3NativeSearchPath] = []
    expanded_nodes = 0
    deduplicated_nodes = 0

    def load_master(ref: Plan3CardRef) -> MasterCard:
        key = (ref.card_id, ref.upgrade)
        master = master_card_cache.get(key)
        if master is None:
            master = load_master_card(ref.card_id, ref.upgrade, database)
            master_card_cache[key] = master
        return master

    def load_cards(ref: Plan3CardRef) -> tuple[Plan3Card, MasterCard]:
        key = (ref.card_id, ref.upgrade)
        plan3 = plan3_card_cache.get(key)
        if plan3 is None:
            plan3 = load_plan3_card(ref.card_id, ref.upgrade, database)
            plan3_card_cache[key] = plan3
        master = load_master(ref)
        return plan3, master

    def load_card_move_search(search_id: str) -> ProduceCardSearchRule:
        search = card_move_search_cache.get(search_id)
        if search is None:
            search = load_produce_card_search(search_id, database)
            card_move_search_cache[search_id] = search
        return search

    def load_card_create_contract(effect_id: str) -> CardCreateContract:
        contract = card_create_contract_cache.get(effect_id)
        if contract is None:
            row = next(
                (
                    value
                    for value in load_master_card_create_id_rows(database)
                    if value["id"] == effect_id
                ),
                None,
            )
            if row is None:
                raise CardCreateResolutionError(
                    "master-effect-missing", effect_id
                )
            contract = resolve_card_create_contract(row)
            card_create_contract_cache[effect_id] = contract
        return contract

    def load_card_create_search_contract(
        effect_id: str,
    ) -> CardCreateSearchEffectContract:
        contract = card_create_search_contract_cache.get(effect_id)
        if contract is None:
            row = next(
                (
                    value
                    for value in load_master_card_create_search_rows(database)
                    if value["id"] == effect_id
                ),
                None,
            )
            if row is None:
                raise Plan3NativeStateError(
                    "card-create-search-master-effect-missing", effect_id
                )
            contract = resolve_card_create_search_contract(row)
            card_create_search_contract_cache[effect_id] = contract
        return contract

    def card_move_options(
        state: Plan3State,
        native: Plan3NativeState,
        card: Plan3Card,
        native_card: Plan3NativeCard,
        counts: Mapping[str, int],
    ) -> tuple[
        tuple[
            tuple[
                tuple[Plan3NativeCardMoveTarget, ...],
                Plan3NativeState,
            ],
            ...,
        ],
        Mapping[str, ProduceCardSearchRule],
    ]:
        move_effects = tuple(
            effect
            for effect in card.effects
            if effect.effect_type == EFFECT_CARD_MOVE
            and not (native_card.play_count > 0 and effect.once)
        )
        if not move_effects:
            return ((((), native)),), {}
        if len(move_effects) != 1 or move_effects[0].card_move_rule is None:
            raise Plan3NativeStateError("card-move-effect-count-unmodelled")
        effect = move_effects[0]
        if effect.trigger is not None:
            decision = evaluate_plan3_trigger(
                effect.trigger, state, card_search_counts=counts
            )
            if not decision.supported:
                raise Plan3NativeStateError(
                    "card-move-trigger-unsupported",
                    ",".join(decision.unsupported_rules),
                )
            if not decision.fires:
                return ((((), native)),), {}
        rule = effect.card_move_rule
        if (
            rule.search_id == SEARCH_PLAYING_SELF
            and rule.pick_range_type == PICK_RANGE_ALL
            and rule.destination == MOVE_HOLD
        ):
            return ((((), native)),), {}
        search = load_card_move_search(rule.search_id)
        search_map = {rule.search_id: search}
        candidates = native.card_move_candidates(
            search, playing_guid=native_card.guid
        )
        if state.stance == STANCE_FULL_POWER and rule.destination == MOVE_HOLD:
            return ((((), native)),), search_map
        if rule.pick_range_type == PICK_RANGE_ALL:
            return (((candidates, native)),), search_map
        minimum = min(rule.pick_count_min, len(candidates))
        maximum = min(rule.pick_count_max, len(candidates))
        if rule.pick_range_type == PICK_RANGE_SELECT:
            options = tuple(
                (tuple(option), native)
                for count in range(minimum, maximum + 1)
                for option in combinations(candidates, count)
            )
            return options, search_map
        if rule.pick_range_type == PICK_RANGE_RANDOM:
            random_after, selected = native.resolve_random_card_move_targets(
                candidates,
                count_min=rule.pick_count_min,
                count_max=rule.pick_count_max,
            )
            return (((selected, random_after)),), search_map
        raise Plan3NativeStateError(
            "unsupported-card-move-pick-range", rule.pick_range_type
        )

    def card_search_counts(native: Plan3NativeState) -> dict[str, int]:
        # Master NotLost covers every ordinary zone except Lost.  Resolve the
        # category from static Master for each exact GUID rather than carrying
        # a guessed scalar counter through shuffles and support upgrades.
        not_lost = (*native.hand, *native.deck, *native.grave, *native.hold)
        trouble_count = sum(
            load_master(card.ref).category == CATEGORY_TROUBLE
            for card in not_lost
        )
        return {SEARCH_TROUBLE_NOT_LOST: trouble_count}

    def ordinary_force_command_choices(
        native: Plan3NativeState,
        *,
        playing_guid: str,
        occurrence_count: int,
    ) -> tuple[tuple[Plan3UsePoolCommand, ...], ...]:
        """Enumerate every local Select1 command tuple, without RNG."""

        if occurrence_count <= 0:
            return ((),)
        contract = force_play_contract_for_effect(
            DECK_GRAVE_SELECT_EFFECT_ID, Path(database)
        )
        one_occurrence: list[Plan3UsePoolCommand] = []
        for candidate in (*native.deck, *native.grave):
            planned = plan_force_play_card_search(
                native,
                contract,
                playing_guid=playing_guid,
                execution_input=ForcePlayExecutionInput(
                    selected_guids=(candidate.guid,)
                ),
                database=Path(database),
            )
            if planned.resolved and len(planned.commands) == 1:
                one_occurrence.append(
                    Plan3UsePoolCommand.from_queued(planned.commands[0])
                )
        if not one_occurrence:
            empty = plan_force_play_card_search(
                native,
                contract,
                playing_guid=playing_guid,
                execution_input=ForcePlayExecutionInput(),
                database=Path(database),
            )
            if empty.resolved and not empty.commands:
                return ((),)
            raise Plan3NativeStateError(
                "force-play-select1-no-replayable-branch",
                playing_guid,
            )
        return tuple(
            tuple(commands)
            for commands in product(one_occurrence, repeat=occurrence_count)
        )

    def with_cost_command_choices(
        native: Plan3NativeState,
        *,
        enchant_effect_uid: int,
    ) -> tuple[Plan3UsePoolCommand, ...]:
        contract = load_force_play_with_cost_contract(Path(database))
        choices: list[Plan3UsePoolCommand] = []
        for candidate in (
            *native.hand,
            *native.deck,
            *native.grave,
            *native.hold,
        ):
            planned = plan_force_play_card_search_with_cost(
                native,
                contract,
                execution_input=ForcePlayWithCostExecutionInput(
                    selected_guids=(candidate.guid,),
                    enchant_effect_uid=enchant_effect_uid,
                ),
                database=Path(database),
            )
            if planned.resolved and len(planned.commands) == 1:
                choices.append(
                    Plan3UsePoolCommand.from_queued(planned.commands[0])
                )
        if choices:
            return tuple(choices)
        empty = plan_force_play_card_search_with_cost(
            native,
            contract,
            execution_input=ForcePlayWithCostExecutionInput(
                enchant_effect_uid=enchant_effect_uid
            ),
            database=Path(database),
        )
        if empty.resolved and not empty.commands:
            return ()
        raise Plan3NativeStateError(
            "force-play-with-cost-select1-no-replayable-branch"
        )

    def make_path(
        state: Plan3State,
        native: Plan3NativeState,
        steps: tuple[Plan3NativeSearchStep, ...],
        *,
        extension_state: object | None,
        drinks: Plan3DrinkInventory | None = None,
        stopped_reason: str = "",
    ) -> Plan3NativeSearchPath:
        native.assert_plan3_projection(state)
        return Plan3NativeSearchPath(
            state=state,
            native_state=native,
            drink_inventory=(initial_drinks if drinks is None else drinks),
            steps=steps,
            evaluation=evaluate_plan3_search_state(state, objective),
            stopped_reason=stopped_reason,
            turn_start_extension_state=extension_state,
        )

    def diagnose(
        path: Plan3NativeSearchPath,
        stage: str,
        *gaps: str,
        card_guid: str | None = None,
    ) -> None:
        diagnostics.append(
            Plan3NativeSearchDiagnostic(
                stage=stage,
                semantic_gaps=tuple(dict.fromkeys(gaps)),
                state=path.state,
                native_state=path.native_state,
                actions=path.actions,
                card_guid=card_guid,
            )
        )

    def stop(
        path: Plan3NativeSearchPath, reason: str
    ) -> Plan3NativeSearchPath:
        return replace(path, stopped_reason=reason)

    def execute_use_pool_command(
        path: Plan3NativeSearchPath,
        command: Plan3UsePoolCommand,
        *,
        source_effect_id: str,
    ) -> tuple[Plan3NativeSearchPath, ...]:
        """Run one queued GUID through this same native card executor."""

        if _use_pool_recursion_depth >= 8:
            raise Plan3NativeStateError(
                "use-pool-recursion-depth", command.guid
            )
        stage = stage_plan3_use_pool_guid(
            path.state, path.native_state, command
        )
        nested = execute_plan3_guid_use_pool_transaction(
            path.state,
            path.native_state,
            command,
            beam_width=beam_width,
            settings=settings,
            objective=objective,
            database=Path(database),
            support_upgrades=supports,
            support_card_searches=searches,
            battle_ranking_resolved=True,
            include_skip=False,
            guid_provider=guid_provider,
            plan_type=plan_type,
            plan_ignore_card_ids=plan_ignore_card_ids,
            hand_limit=card_create_hand_limit,
            turn_start_extension=turn_start_extension,
            accepted_play_extension=accepted_play_extension,
            initial_turn_start_extension_state=(
                path.turn_start_extension_state
            ),
            recursion_depth=_use_pool_recursion_depth + 1,
        )
        branches: list[Plan3NativeSearchPath] = []
        for candidate in nested.candidates:
            nested_card_steps = tuple(
                step
                for step in candidate.steps
                if step.action is not None
                and step.action.guid == command.guid
            )
            if len(nested_card_steps) != 1:
                continue
            nested_step = nested_card_steps[0]
            if (
                nested_step.card_transition is None
                or nested_step.queued_use_pool_commands
            ):
                continue
            transaction = Plan3UsePoolTransaction(
                command=command,
                source_effect_id=source_effect_id,
                source_zone=stage.source_zone,
                before=path.state,
                after=candidate.state,
                native_before=path.native_state,
                native_after=candidate.native_state,
                card_transition=nested_step.card_transition,
            )
            step = replace(
                nested_step,
                kind="use_pool",
                before=path.state,
                after=candidate.state,
                native_before=path.native_state,
                native_after=candidate.native_state,
                action=None,
                queued_use_pool_commands=(),
                queued_use_pool_effect_ids=(),
                use_pool_transactions=(transaction,),
            )
            branches.append(
                make_path(
                    candidate.state,
                    candidate.native_state,
                    (*path.steps, step),
                    extension_state=(
                        candidate.turn_start_extension_state
                    ),
                    drinks=path.drink_inventory,
                )
            )
        if not branches:
            details = tuple(
                gap
                for item in nested.diagnostics
                for gap in item.semantic_gaps
            )
            raise Plan3NativeStateError(
                "use-pool-command-failed",
                ",".join(details) or command.guid,
            )
        return tuple(branches)

    def pending_use_pool_branches(
        path: Plan3NativeSearchPath,
    ) -> tuple[Plan3NativeSearchPath, ...] | None:
        """Execute at most one pending command; a later beam node continues."""

        base_index = next(
            (
                index
                for index in range(len(path.steps) - 1, -1, -1)
                if path.steps[index].kind != "use_pool"
            ),
            None,
        )
        if base_index is None:
            return None
        base = path.steps[base_index]
        following_transactions = tuple(
            transaction
            for step in path.steps[base_index + 1 :]
            for transaction in step.use_pool_transactions
        )
        queued_effect_ids = (
            base.queued_use_pool_effect_ids
            if base.queued_use_pool_effect_ids
            else tuple(
                DECK_GRAVE_SELECT_EFFECT_ID
                for _ in base.queued_use_pool_commands
            )
        )
        if len(queued_effect_ids) != len(base.queued_use_pool_commands):
            raise Plan3NativeStateError(
                "use-pool-command-source-count-mismatch"
            )
        queued_sources = set(queued_effect_ids)
        ordinary_done = sum(
            transaction.source_effect_id in queued_sources
            for transaction in following_transactions
        )
        if ordinary_done < len(base.queued_use_pool_commands):
            command = base.queued_use_pool_commands[ordinary_done]
            return execute_use_pool_command(
                path,
                command,
                source_effect_id=queued_effect_ids[ordinary_done],
            )

        if base.turn_start is None:
            return None
        events = tuple(
            event
            for event in base.turn_start.runtime_effect_events
            if event.effect_type == EFFECT_FORCE_PLAY_CARD_SEARCH_WITH_COST
        )
        with_cost_done = sum(
            transaction.source_effect_id
            == EFFECT_FORCE_PLAY_CARD_SEARCH_WITH_COST
            for transaction in following_transactions
        ) + sum(
            effect_id == EFFECT_FORCE_PLAY_CARD_SEARCH_WITH_COST
            for step in path.steps[base_index + 1 :]
            for effect_id in step.settled_use_pool_effect_ids
        )
        if with_cost_done >= len(events):
            return None
        event = events[with_cost_done]
        enchant = next(
            (
                item
                for item in event.before.active_status_enchants
                if item.instance_id == event.source_enchant_instance_id
            ),
            None,
        )
        if enchant is None:
            # A one-shot listener is removed when its trigger command is
            # queued, before the ordered runtime effect snapshot is emitted.
            # The TurnStart input still owns the exact source instance/UID;
            # recover that same identity rather than silently creating a
            # context-free UsePool command with UID zero.
            enchant = next(
                (
                    item
                    for item in base.turn_start.before.active_status_enchants
                    if item.instance_id == event.source_enchant_instance_id
                ),
                None,
            )
        if enchant is None or enchant.native_uid <= 0:
            raise Plan3NativeStateError(
                "force-play-with-cost-source-enchant-uid-unprojected",
                event.source_enchant_instance_id,
            )
        commands = with_cost_command_choices(
            path.native_state,
            enchant_effect_uid=enchant.native_uid,
        )
        if not commands:
            # Native Select1 with no eligible target queues no command and is
            # fully settled; record the empty event so it cannot be revisited.
            step = Plan3NativeSearchStep(
                kind="use_pool",
                before=path.state,
                after=path.state,
                native_before=path.native_state,
                native_after=path.native_state,
                settled_use_pool_effect_ids=(
                    EFFECT_FORCE_PLAY_CARD_SEARCH_WITH_COST,
                ),
            )
            return (
                make_path(
                    path.state,
                    path.native_state,
                    (*path.steps, step),
                    extension_state=path.turn_start_extension_state,
                    drinks=path.drink_inventory,
                ),
            )
        return tuple(
            branch
            for command in commands
            for branch in execute_use_pool_command(
                path,
                command,
                source_effect_id=EFFECT_FORCE_PLAY_CARD_SEARCH_WITH_COST,
            )
        )

    def pending_use_pool_key(
        path: Plan3NativeSearchPath,
    ) -> tuple[Plan3UsePoolCommand, ...]:
        """Keep deferred Select1 commands distinct until they settle."""

        base_index = next(
            (
                index
                for index in range(len(path.steps) - 1, -1, -1)
                if path.steps[index].kind != "use_pool"
            ),
            None,
        )
        if base_index is None:
            return ()
        base = path.steps[base_index]
        queued_sources = set(
            base.queued_use_pool_effect_ids
            or (DECK_GRAVE_SELECT_EFFECT_ID,)
        )
        completed = sum(
            transaction.source_effect_id in queued_sources
            for step in path.steps[base_index + 1 :]
            for transaction in step.use_pool_transactions
        )
        return base.queued_use_pool_commands[completed:]

    def path_key(path: Plan3NativeSearchPath) -> tuple[object, ...]:
        decisions = tuple(
            (
                "card",
                step.action.card_ref.card_id,
                step.action.card_ref.upgrade,
                step.action.guid,
                step.action.selected_card_guids,
            )
            if step.action is not None
            else (
                "drink",
                step.drink_action.drink_id,
                step.drink_action.slot_index,
                step.drink_action.selected_card_guid or "",
            )
            if step.drink_action is not None
            else ("skip",)
            for step in path.decision_steps
        )
        advisory = 0.0
        if imitation_tie_breaker is not None:
            try:
                advisory = float(imitation_tie_breaker(path))
            except (AttributeError, TypeError, ValueError):
                advisory = 0.0
            if not (
                advisory == advisory
                and abs(advisory) <= PLAN3_MAX_PATH_TIE_BREAK
            ):
                advisory = 0.0
        return (
            path.evaluation.objective,
            advisory,
            -len(path.decision_steps),
            decisions,
        )

    def native_guid_path_token(path: Plan3NativeSearchPath) -> tuple[str, ...]:
        """Canonical replay identity before the currently expanded action."""

        token = [
            f"round={path.state.round_number}",
            f"turns={path.state.turns_remaining}",
            f"plays={path.state.plays_remaining}",
        ]
        for index, step in enumerate(path.decision_steps):
            if step.action is not None:
                token.append(
                    "card="
                    f"{index}:{step.action.guid}:"
                    f"{step.action.card_ref.card_id}+"
                    f"{step.action.card_ref.upgrade}:"
                    f"{','.join(step.action.selected_card_guids)}"
                )
            elif step.drink_action is not None:
                token.append(
                    "drink="
                    f"{index}:{step.drink_action.slot_index}:"
                    f"{step.drink_action.drink_id}:"
                    f"{step.drink_action.selected_card_guid or ''}"
                )
            else:
                token.append(f"skip={index}")
        return tuple(token)

    def complete_forced(
        path: Plan3NativeSearchPath,
        *,
        recovery_turns: int | None = None,
    ) -> Plan3NativeSearchPath:
        if (
            force_end_score <= 0
            or path.state.score < force_end_score
            or (
                path.state.turns_remaining == 0
                and recovery_turns is None
            )
        ):
            return path
        turns = (
            path.state.turns_remaining
            if recovery_turns is None
            else recovery_turns
        )
        if recovery_turns is None and path.steps:
            last_step = path.steps[-1]
            if (
                last_step.kind == "turn_boundary"
                and last_step.ended_state is not None
                and last_step.ended_state.score < force_end_score
                and path.state.score >= force_end_score
            ):
                # A TurnStart listener can reach PERFECT while resolving the
                # SKIP/TurnEnd command.  The native lesson result recovers HP
                # from the turn count displayed when that command began, not
                # from the already-decremented state installed for the next
                # turn.  Preserve that pre-boundary count for this one exact
                # command-chain case.
                turns = last_step.before.turns_remaining
        target_recovery = turns * force_end_stamina_recovery
        # A manual END_TURN already applied one fixed Setting recovery before
        # its EndTurn listeners.  Force-end recovery is a total amount based
        # on the displayed pre-boundary turn count, so subtract that committed
        # unit instead of paying it twice.
        already_recovered = 0
        if (
            path.decision_steps
            and path.decision_steps[-1].kind == "skip"
        ):
            boundary = next(
                (
                    step
                    for step in reversed(path.steps)
                    if step.kind in {"turn_boundary", "end_turn"}
                ),
                None,
            )
            before_recovery = (
                None if boundary is None else boundary.before
            )
            if (
                before_recovery is not None
                and before_recovery.max_stamina is not None
            ):
                already_recovered = min(
                    settings.turn_end_stamina_recovery,
                    max(
                        0,
                        before_recovery.max_stamina
                        - before_recovery.stamina,
                    ),
                )
        recovery = max(0, target_recovery - already_recovered)
        if path.state.max_stamina is not None:
            recovery = min(
                recovery,
                max(0, path.state.max_stamina - path.state.stamina),
            )
        total_recovered = already_recovered + recovery
        completed = replace(
            path.state,
            turns_remaining=0,
            stamina=path.state.stamina + recovery,
            score=force_end_score,
        )
        completed.validate(allow_completed=True)
        step = Plan3NativeSearchStep(
            kind="force_end",
            before=path.state,
            after=completed,
            native_before=path.native_state,
            native_after=path.native_state,
            forced_end_stamina_recovered=total_recovered,
        )
        return make_path(
            completed,
            path.native_state,
            (*path.steps, step),
            extension_state=path.turn_start_extension_state,
            drinks=path.drink_inventory,
        )

    def end_native_hand(
        path: Plan3NativeSearchPath,
    ) -> tuple[
        Plan3State,
        Plan3NativeState,
        tuple[Plan3EncoreForcedReplay, ...],
        object | None,
    ] | None:
        encore = execute_plan3_exact_encore_end_turn(
            path.state,
            path.native_state,
            settings=settings,
            database=Path(database),
            accepted_play_extension=accepted_play_extension,
            initial_extension_state=path.turn_start_extension_state,
        )
        if not encore.supported:
            diagnose(path, "end-turn-encore", *encore.unsupported_rules)
            return None
        native = encore.native_after
        for card in tuple(native.hand):
            try:
                _plan3, master = load_cards(card.ref)
            except (KeyError, TypeError, ValueError, OSError) as error:
                diagnose(
                    path,
                    "end-turn-master",
                    f"master-card-load:{type(error).__name__}:{error}",
                    card_guid=card.guid,
                )
                return None
            destination = MOVE_LOST if master.is_end_turn_lost else MOVE_GRAVE
            try:
                native = native.move_hand_by_guid(card.guid, destination)
            except Plan3NativeStateError as error:
                diagnose(
                    path,
                    "end-turn-native-zone",
                    f"{error.code}:{error.detail}".rstrip(":"),
                    card_guid=card.guid,
                )
                return None
        manual_turn_end = bool(
            path.decision_steps
            and path.decision_steps[-1].kind == "skip"
            and encore.after.max_stamina is not None
        )
        scalar = _synchronize_zones(
            end_plan3_turn(
                encore.after,
                settings=settings,
                apply_turn_end_recovery=manual_turn_end,
            ),
            native,
        )
        if native.enthusiastic_runtime.status_present:
            try:
                native, _enthusiastic_spend = native.spend_enthusiastic_turn(
                    next_status_uid=encore.after.next_status_uid,
                    source="turn-end-spend",
                )
            except (OverflowError, TypeError, ValueError) as error:
                diagnose(
                    path,
                    "turn-enthusiastic-spend",
                    f"{type(error).__name__}:{error}",
                )
                return None
        native.assert_plan3_projection(scalar)
        try:
            native = native.reset_turn_used_supports(
                mark_turn_start=scalar.turns_remaining > 0,
            )
        except Plan3NativeStateError as error:
            diagnose(
                path,
                "turn-support-reset",
                f"{error.code}:{error.detail}".rstrip(":"),
            )
            return None
        return scalar, native, encore.replays, encore.extension_state

    def start_native_turn(
        path: Plan3NativeSearchPath,
        state: Plan3State,
        native: Plan3NativeState,
        *,
        ended_state: Plan3State | None,
        native_ended_state: Plan3NativeState | None,
        encore_replays: tuple[Plan3EncoreForcedReplay, ...] = (),
    ) -> Plan3NativeSearchPath | None:
        if not state.awaiting_turn_start:
            diagnose(path, "turn-start", "phase:not-awaiting-turn-start")
            return None
        if native.hand:
            diagnose(path, "turn-start", "native-hand-not-empty-before-start")
            return None

        # Full Power returns Hold before the ordinary distribution.  Any
        # support-upgraded Hold card was already rejected by turn-use reset.
        native_before_draw = native
        if state.full_power_points >= 10 and native.hold:
            native_before_draw = replace(
                native,
                hand=native.hold,
                hold=(),
            )
        source_by_guid = {card.guid: card for card in native_before_draw.all_cards}
        draw_lesson_type = state.lesson_type
        if state.is_battle:
            draw_lesson_type = {
                1: LESSON_VOCAL,
                2: LESSON_DANCE,
                3: LESSON_VISUAL,
            }.get(state.current_parameter_type, "")
            if not draw_lesson_type:
                diagnose(
                    path,
                    "native-draw",
                    "battle-current-parameter-type-unknown",
                )
                return None
        try:
            drawn = native_before_draw.draw_to_hand(
                settings.turn_start_distribute,
                hand_limit=settings.hand_limit,
                lesson_type=draw_lesson_type,
                support_upgrades=supports,
                support_card_searches=searches,
            )
        except (Plan3NativeStateError, NativeHandAddSupportError) as error:
            code = getattr(error, "code", type(error).__name__)
            detail = getattr(error, "detail", str(error))
            diagnose(
                path,
                "native-draw",
                f"{code}:{detail}".rstrip(":"),
            )
            return None
        base_draw_order = tuple(
            source_by_guid[guid].ref for guid in drawn.drawn_guids
        )
        extension_execution: Plan3NativeTurnStartExtensionResult | None = None

        def resolve_external_gimmick(
            working: Plan3State,
            current_settings: Plan3ExamSettings,
        ) -> Plan3ExternalTurnStartGimmickResolution:
            nonlocal extension_execution
            assert turn_start_extension is not None
            extension_execution = turn_start_extension(
                working,
                native_before_draw,
                path.turn_start_extension_state,
                current_settings,
            )
            if not isinstance(
                extension_execution, Plan3NativeTurnStartExtensionResult
            ):
                raise TypeError(
                    "turn_start_extension must return "
                    "Plan3NativeTurnStartExtensionResult"
                )
            if extension_execution.extension_state is not None:
                try:
                    hash(extension_execution.extension_state)
                except TypeError:
                    return Plan3ExternalTurnStartGimmickResolution(
                        state=working,
                        unsupported_rules=(
                            "turn-start-extension-state-not-hashable",
                        ),
                    )
            return Plan3ExternalTurnStartGimmickResolution(
                state=extension_execution.scalar_state,
                fired_effect_ids=extension_execution.fired_effect_ids,
                unsupported_rules=extension_execution.unsupported_rules,
            )

        started = start_plan3_turn(
            state,
            authoritative_draw_order=base_draw_order,
            settings=settings,
            gimmick_profile=(
                gimmick_profile if turn_start_extension is None else None
            ),
            external_gimmick_resolver=(
                None
                if turn_start_extension is None
                else resolve_external_gimmick
            ),
        )
        if not started.supported:
            diagnose(path, "turn-start", *started.unsupported_rules)
            return None
        if extension_execution is not None:
            try:
                redrawn = extension_execution.native_state.draw_to_hand(
                    settings.turn_start_distribute,
                    hand_limit=settings.hand_limit,
                    lesson_type=draw_lesson_type,
                    support_upgrades=supports,
                    support_card_searches=searches,
                )
            except (Plan3NativeStateError, NativeHandAddSupportError) as error:
                code = getattr(error, "code", type(error).__name__)
                detail = getattr(error, "detail", str(error))
                diagnose(
                    path,
                    "turn-start-extension-native-draw",
                    f"{code}:{detail}".rstrip(":"),
                )
                return None
            if redrawn.drawn_guids != drawn.drawn_guids:
                diagnose(
                    path,
                    "turn-start-extension-native-draw",
                    "draw-order-changed-by-turn-start-extension",
                )
                return None
            drawn = redrawn
        try:
            runtime_replay = execute_plan3_runtime_effect_events(
                state,
                started,
                drawn.after,
                hand_limit=settings.hand_limit,
                hold_limit=settings.hold_limit,
                lesson_type=draw_lesson_type,
                support_upgrades=supports,
                support_card_searches=searches,
                database=database,
            )
        except (Plan3NativeStateError, NativeHandAddSupportError) as error:
            code = getattr(error, "code", type(error).__name__)
            detail_value = getattr(error, "detail", str(error))
            detail = f"{code}:{detail_value}".rstrip(":")
            diagnose(
                path,
                "turn-start-native-runtime",
                detail,
            )
            return None
        native_after_start = replace(
            runtime_replay.state,
            anti_debuff_runtime=(
                runtime_replay.state.anti_debuff_runtime
                .mark_passing_turn_start()
            ),
        )
        if (
            native_after_start.anti_debuff_runtime
            != started.after.anti_debuff_runtime
        ):
            diagnose(
                path,
                "turn-start-native-runtime",
                "plan3-anti-debuff-projection-mismatch",
            )
            return None
        runtime_draws = runtime_replay.draws
        synchronized = _synchronize_zones(started.after, native_after_start)
        started = replace(
            started,
            after=synchronized,
            drawn_cards=tuple(card.ref for card in drawn.drawn_cards),
        )
        native_after_start.assert_plan3_projection(synchronized)
        kind = "turn_start" if ended_state is None else "turn_boundary"
        step = Plan3NativeSearchStep(
            kind=kind,
            before=path.state,
            after=synchronized,
            native_before=path.native_state,
            native_after=native_after_start,
            turn_start=started,
            native_draw=drawn,
            runtime_draws=runtime_draws,
            runtime_card_upgrades=runtime_replay.card_upgrades,
            move_effect_traces=runtime_replay.card_moves,
            ended_state=ended_state,
            native_ended_state=native_ended_state,
            turn_start_extension_trace=(
                None
                if extension_execution is None
                else extension_execution.trace
            ),
            encore_replays=encore_replays,
        )
        return make_path(
            synchronized,
            native_after_start,
            (*path.steps, step),
            extension_state=(
                path.turn_start_extension_state
                if extension_execution is None
                else extension_execution.extension_state
            ),
            drinks=path.drink_inventory,
        )

    def settle_boundary(
        path: Plan3NativeSearchPath,
    ) -> Plan3NativeSearchPath | None:
        if path.state.turns_remaining == 0:
            return path
        if path.state.awaiting_turn_start:
            try:
                native = path.native_state.reset_turn_used_supports()
            except Plan3NativeStateError as error:
                diagnose(
                    path,
                    "turn-support-reset",
                    f"{error.code}:{error.detail}".rstrip(":"),
                )
                finished.append(stop(path, "unsupported-turn-boundary"))
                return None
            started = start_native_turn(
                path,
                path.state,
                native,
                ended_state=None,
                native_ended_state=None,
            )
            if started is None:
                finished.append(stop(path, "unsupported-turn-start"))
            return started
        if path.state.plays_remaining > 0:
            return path
        next_battle_parameter_type: int | None = None
        if path.state.is_battle and path.state.turns_remaining > 1:
            next_schedule_index = (
                path.state.round_number - initial_state.round_number + 1
            )
            if (
                battle_schedule is None
                or next_schedule_index < 0
                or next_schedule_index >= len(battle_schedule)
            ):
                diagnose(
                    path,
                    "turn-boundary",
                    "battle-future-turn-parameter-schedule-unmodelled",
                )
                finished.append(
                    stop(path, "unsupported-battle-turn-boundary")
                )
                return None
            next_battle_parameter_type = battle_schedule[next_schedule_index]
        ended_pair = end_native_hand(path)
        if ended_pair is None:
            finished.append(stop(path, "unsupported-turn-end"))
            return None
        (
            ended,
            native_ended,
            encore_replays,
            ended_extension_state,
        ) = ended_pair
        if force_end_score > 0 and ended.score >= force_end_score:
            step = Plan3NativeSearchStep(
                kind="end_turn",
                before=path.state,
                after=ended,
                native_before=path.native_state,
                native_after=native_ended,
                ended_state=ended,
                native_ended_state=native_ended,
                encore_replays=encore_replays,
            )
            ended_path = make_path(
                ended,
                native_ended,
                (*path.steps, step),
                extension_state=ended_extension_state,
                drinks=path.drink_inventory,
            )
            return complete_forced(
                ended_path,
                recovery_turns=path.state.turns_remaining,
            )
        if next_battle_parameter_type is not None:
            ended = replace(
                ended,
                current_parameter_type=next_battle_parameter_type,
            )
        if ended.turns_remaining == 0:
            # Native terminal ExamSaveData retains the just-completed turn
            # number; there is no following TurnStart whose ordinal should be
            # published.  ``end_plan3_turn`` increments for the ordinary
            # scheduler, so normalize only this proven terminal boundary.
            ended = replace(
                ended,
                round_number=path.state.round_number,
            )
            step = Plan3NativeSearchStep(
                kind="end_turn",
                before=path.state,
                after=ended,
                native_before=path.native_state,
                native_after=native_ended,
                ended_state=ended,
                native_ended_state=native_ended,
                encore_replays=encore_replays,
            )
            return make_path(
                ended,
                native_ended,
                (*path.steps, step),
                extension_state=ended_extension_state,
                drinks=path.drink_inventory,
            )
        started = start_native_turn(
            replace(
                path,
                turn_start_extension_state=ended_extension_state,
            ),
            ended,
            native_ended,
            ended_state=ended,
            native_ended_state=native_ended,
            encore_replays=encore_replays,
        )
        if started is None:
            finished.append(stop(path, "unsupported-turn-start"))
        return started

    root_path = make_path(
        initial_state,
        initial_native_state,
        (),
        extension_state=initial_turn_start_extension_state,
    )
    if initial_state.is_battle:
        # The player-side card arithmetic below is battle-aware, including
        # per-attribute bonus scaling and subtotal mutation.  The state does
        # not yet carry opponent score/ranking data.  The caller may supply
        # the complete future turn-attribute schedule; otherwise expose that
        # wider-horizon boundary even though current-turn search remains
        # available.
        gaps: list[str] = []
        if not battle_ranking_resolved:
            gaps.append("battle-npc-ranking-state-unmodelled")
        if (
            battle_schedule is None
            or len(battle_schedule) < initial_state.turns_remaining
        ):
            gaps.append("battle-future-turn-parameter-schedule-unmodelled")
        if gaps:
            diagnose(root_path, "battle-horizon-scope", *gaps)
    frontier = (root_path,)
    while frontier:
        children: list[Plan3NativeSearchPath] = []
        for raw_path in frontier:
            if raw_path.stopped_reason:
                finished.append(raw_path)
                continue
            raw_path = complete_forced(raw_path)
            if raw_path.complete:
                finished.append(raw_path)
                continue
            if _use_pool_command is None:
                try:
                    pending = pending_use_pool_branches(raw_path)
                except (Plan3NativeStateError, ValueError, OSError) as error:
                    code = getattr(error, "code", type(error).__name__)
                    detail = getattr(error, "detail", str(error))
                    diagnose(
                        raw_path,
                        "use-pool",
                        f"{code}:{detail}".rstrip(":"),
                    )
                    finished.append(stop(raw_path, "unsupported-use-pool"))
                    continue
                if pending is not None:
                    children.extend(pending)
                    continue
            # A card-depth limit is an exact post-action cut.  Do not cross a
            # turn boundary first: that could consume shuffle/support RNG for
            # a horizon the caller did not authorize or fully configure.
            if depth is not None and len(raw_path.actions) >= depth:
                finished.append(stop(raw_path, "depth-limit"))
                continue
            path = (
                raw_path
                if _use_pool_command is not None
                else settle_boundary(raw_path)
            )
            if path is None:
                continue
            path = complete_forced(path)
            if path.complete:
                finished.append(path)
                continue
            if (_single_action is not None or _single_turn_end) and any(
                step.settled_boundary for step in path.steps
            ):
                # The caller requested one card only.  Once the ordinary
                # EndTurn -> TurnStart boundary, or the final terminal
                # EndTurn, has settled, publish that path without expanding
                # a fresh hand or adding a skip.
                finished.append(
                    stop(
                        path,
                        (
                            "single-turn-end-complete"
                            if _single_turn_end
                            else "single-action-complete"
                        ),
                    )
                )
                continue
            if _use_pool_command is None:
                try:
                    pending = pending_use_pool_branches(path)
                except (Plan3NativeStateError, ValueError, OSError) as error:
                    code = getattr(error, "code", type(error).__name__)
                    detail = getattr(error, "detail", str(error))
                    diagnose(
                        path,
                        "use-pool",
                        f"{code}:{detail}".rstrip(":"),
                    )
                    finished.append(stop(path, "unsupported-use-pool"))
                    continue
                if pending is not None:
                    children.extend(pending)
                    continue

            expanded_nodes += 1
            exact_child = False

            # A single-action caller owns the card identity and must not make
            # the search enumerate unrelated drink decisions.  Keeping the
            # loop in place (with an empty iterable) preserves the normal
            # branch implementation for the ordinary horizon search.
            for slot_index, drink in enumerate(
                ()
                if _single_action is not None or _single_turn_end
                else path.drink_inventory.drinks
            ):
                selected_moves = tuple(
                    effect
                    for effect in drink.effects
                    if isinstance(effect, Plan3DrinkSelectedCardMove)
                )
                if len(selected_moves) > 1:
                    diagnose(
                        path,
                        "drink-effect",
                        f"multiple-selected-moves:{drink.id}",
                    )
                    continue
                selected_guids: tuple[str | None, ...]
                if selected_moves:
                    selected_guids = tuple(
                        card.guid
                        for card in (
                            *path.native_state.deck,
                            *path.native_state.grave,
                        )
                    )
                    if not selected_guids:
                        continue
                else:
                    selected_guids = (None,)
                for selected_guid in selected_guids:
                    try:
                        application = apply_plan3_drink(
                            path.state,
                            path.native_state,
                            path.drink_inventory,
                            slot_index,
                            selected_card_guid=selected_guid,
                            settings=settings,
                        )
                    except (
                        Plan3DrinkApplicationError,
                        Plan3NativeStateError,
                        IndexError,
                        TypeError,
                        ValueError,
                    ) as error:
                        code = getattr(error, "code", type(error).__name__)
                        detail = getattr(error, "detail", str(error))
                        diagnose(
                            path,
                            "drink-effect",
                            f"{code}:{detail}".rstrip(":"),
                            card_guid=selected_guid,
                        )
                        continue
                    drink_action = Plan3NativeDrinkAction(
                        slot_index=slot_index,
                        drink_id=drink.id,
                        selected_card_guid=selected_guid,
                    )
                    step = Plan3NativeSearchStep(
                        kind="drink",
                        before=path.state,
                        after=application.scalar_after,
                        native_before=path.native_state,
                        native_after=application.native_after,
                        drink_action=drink_action,
                        drink_application=application,
                    )
                    children.append(
                        make_path(
                            application.scalar_after,
                            application.native_after,
                            (*path.steps, step),
                            extension_state=path.turn_start_extension_state,
                            drinks=application.inventory_after,
                        )
                    )
                    exact_child = True

            if _single_turn_end:
                native_cards = ()
            elif _single_action is not None and path.decision_steps:
                native_cards = ()
            elif _single_action is None:
                native_cards = path.native_state.hand
            else:
                native_cards = tuple(
                    card
                    for card in path.native_state.hand
                    if card.guid == _single_action.guid
                    and card.ref == _single_action.card_ref
                )
            for native_card in native_cards:
                if (
                    _use_pool_command is not None
                    and native_card.guid != _use_pool_command.guid
                ):
                    continue
                ref = native_card.ref
                try:
                    base_card_rule, _master = load_cards(ref)
                    current_card_search_counts = card_search_counts(
                        path.native_state
                    )
                except (KeyError, TypeError, ValueError, OSError) as error:
                    diagnose(
                        path,
                        "card-load",
                        f"master-card-load:{type(error).__name__}:{error}",
                        card_guid=native_card.guid,
                    )
                    continue
                try:
                    if base_card_rule.category == CATEGORY_TROUBLE:
                        from .plan3_sleepy_trouble import (
                            SleepyTroubleError,
                            assert_exact_sleepy_instance,
                        )

                        try:
                            assert_exact_sleepy_instance(native_card)
                        except SleepyTroubleError as error:
                            raise Plan3NativeStateError(
                                error.code, error.detail
                            ) from error
                    card_rule = _apply_native_runtime_growth(
                        base_card_rule, native_card
                    )
                    _validate_native_card_play_triggers(
                        path.state,
                        path.native_state,
                        native_card.guid,
                        card_rule,
                        database=Path(database),
                    )
                    _validate_native_none_field_trigger(
                        path.state,
                        path.native_state,
                        native_card.guid,
                        card_rule,
                    )
                except Plan3NativeStateError as error:
                    diagnose(
                        path,
                        "card-runtime-growth",
                        f"{error.code}:{error.detail}".rstrip(":"),
                        card_guid=native_card.guid,
                    )
                    continue
                native_card_stamina_cost = card_rule.stamina_cost
                search_stamina_preview: Plan3SearchStaminaPayment | None = None
                if card_rule.cost_type == COST_STAMINA and (
                    _use_pool_command is None
                    or _use_pool_command.is_consume_cost
                ):
                    search_stamina_preview = (
                        resolve_plan3_search_stamina_payment(
                            path.native_state.search_stamina_runtime,
                            native_card,
                            native_card_stamina_cost,
                            simulate=True,
                        )
                    )
                    if (
                        search_stamina_preview.cost_source
                        == "search-override"
                    ):
                        card_rule = replace(
                            card_rule,
                            stamina_cost=(
                                search_stamina_preview.selected_stamina_cost
                            ),
                        )
                try:
                    selection_options, card_move_searches = card_move_options(
                        path.state,
                        path.native_state,
                        card_rule,
                        native_card,
                        current_card_search_counts,
                    )
                except (KeyError, ValueError, OSError) as error:
                    code = getattr(error, "code", type(error).__name__)
                    detail = getattr(error, "detail", str(error))
                    diagnose(
                        path,
                        "card-effect",
                        f"{code}:{detail}".rstrip(":"),
                        card_guid=native_card.guid,
                    )
                    continue
                direct_effect_repeat_count = 0
                play_state = path.state
                if path.state.play_count_buff_runtime.statuses:
                    buff_hook = execute_card_search_effect_play_count_buff(
                        CardPlayCountBuffHookInput(
                            state=path.native_state,
                            runtime=path.state.play_count_buff_runtime,
                            playing_guid=native_card.guid,
                        ),
                        database=Path(database),
                    )
                    if not buff_hook.executable:
                        diagnose(
                            path,
                            "play-count-buff",
                            *buff_hook.unresolved,
                            card_guid=native_card.guid,
                        )
                        continue
                    direct_effect_repeat_count = buff_hook.repeat_count
                    play_state = replace(
                        path.state,
                        play_count_buff_runtime=buff_hook.after_runtime,
                    )
                managed_interval_ids: tuple[str, ...] = ()
                if accepted_play_extension is not None:
                    try:
                        managed_interval_ids = tuple(
                            accepted_play_extension.managed_play_count_interval_instance_ids(
                                play_state,
                                path.turn_start_extension_state,
                            )
                        )
                        if (
                            any(
                                not isinstance(value, str) or not value
                                for value in managed_interval_ids
                            )
                            or len(managed_interval_ids)
                            != len(set(managed_interval_ids))
                        ):
                            raise ValueError(
                                "managed interval IDs must be unique text"
                            )
                    except (TypeError, ValueError) as error:
                        diagnose(
                            path,
                            "accepted-play-extension",
                            f"managed-listeners:{type(error).__name__}:{error}",
                            card_guid=native_card.guid,
                        )
                        continue
                force_occurrence_count = sum(
                    effect.effect_type == EFFECT_FORCE_PLAY_CARD_SEARCH
                    and effect.id == DECK_GRAVE_SELECT_EFFECT_ID
                    for pass_index in range(direct_effect_repeat_count + 1)
                    for effect in card_rule.effects
                    if not (
                        (native_card.play_count > 0 or pass_index > 0)
                        and effect.once
                    )
                )
                try:
                    force_command_options = ordinary_force_command_choices(
                        path.native_state,
                        playing_guid=native_card.guid,
                        occurrence_count=force_occurrence_count,
                    )
                except (ValueError, OSError, Plan3NativeStateError) as error:
                    code = getattr(error, "code", type(error).__name__)
                    detail = getattr(error, "detail", str(error))
                    diagnose(
                        path,
                        "force-play-plan",
                        f"{code}:{detail}".rstrip(":"),
                        card_guid=native_card.guid,
                    )
                    continue
                combined_options = tuple(
                    (selected_targets, selection_native, commands)
                    for selected_targets, selection_native in selection_options
                    for commands in force_command_options
                )
                for (
                    selected_targets,
                    selection_native,
                    queued_use_pool_commands,
                ) in combined_options:
                    # The ordinary UseHand transaction performs its first
                    # per-GUID PlayCardCount mutation after build-time
                    # trigger/once selection but before cost and runtime
                    # effects.  Keep this immutable state branch-local:
                    # another Select/UsePool option must always start from
                    # the same unplayed input card.
                    branch_native = selection_native
                    build_receipts: tuple[
                        CardPlayCountOperationReceipt, ...
                    ] = ()
                    if _use_pool_command is None:
                        build_card = branch_native.card_by_guid(
                            native_card.guid
                        )
                        if not any(
                            value.guid == build_card.guid
                            for value in branch_native.hand
                        ):
                            raise Plan3NativeStateError(
                                "ordinary-build-guid-not-in-hand",
                                build_card.guid,
                            )
                        built_card = build_card.increment_play_count()
                        branch_native = _replace_native_card_by_guid(
                            branch_native,
                            build_card.guid,
                            built_card,
                        )
                        build_receipts = (
                            CardPlayCountOperationReceipt(
                                family=CARD_PLAY_COUNT_RECEIPT_FAMILY,
                                operation=(
                                    CARD_PLAY_COUNT_INCREMENT_OPERATION
                                ),
                                subject_id=build_card.guid,
                                owner=(
                                    PLAN3_ORDINARY_CARD_PLAY_COUNT_BUILD_OWNER
                                ),
                                before_value=build_card.play_count,
                                after_value=built_card.play_count,
                            ),
                        )
                    else:
                        # APK ExecuteCardCommandImpl (0x7ecf06c) increments
                        # the same GUID for UsePool as well. MovePlayCard's
                        # second increment precedes its playable-budget gate.
                        # Keep ordinary-only receipt owner names unchanged.
                        build_card = branch_native.card_by_guid(native_card.guid)
                        branch_native = _replace_native_card_by_guid(
                            branch_native, build_card.guid, build_card.increment_play_count()
                        )
                    selected_guids = (
                        *(candidate.guid for candidate in selected_targets),
                        *(command.guid for command in queued_use_pool_commands),
                    )
                    search_stamina_payment: (
                        Plan3SearchStaminaPayment | None
                    ) = None
                    if search_stamina_preview is not None:
                        search_stamina_payment = (
                            resolve_plan3_search_stamina_payment(
                                branch_native.search_stamina_runtime,
                                branch_native.card_by_guid(
                                    native_card.guid
                                ),
                                native_card_stamina_cost,
                                simulate=False,
                            )
                        )
                        branch_native = replace(
                            branch_native,
                            search_stamina_runtime=(
                                search_stamina_payment.runtime_after
                            ),
                        )
                    pre_direct_transform = None
                    pre_direct_captures: list[
                        _Plan3AcceptedPreDirectCapture
                    ] = []
                    if accepted_play_extension is not None:
                        play_source = (
                            Plan3NativeAcceptedPlaySource.ORDINARY
                            if _use_pool_command is None
                            else Plan3NativeAcceptedPlaySource.FORCED
                        )
                        (
                            pre_direct_transform,
                            pre_direct_captures,
                        ) = _accepted_pre_direct_transform(
                            scalar_before=play_state,
                            native_state=branch_native,
                            playing_guid=native_card.guid,
                            compiled_card=card_rule,
                            extension=accepted_play_extension,
                            extension_state=(
                                path.turn_start_extension_state
                            ),
                            settings=settings,
                            source=play_source,
                            managed_interval_ids=managed_interval_ids,
                            database=Path(database),
                            card_profile_by_card=(
                                lambda card: _plan3_add_grow_card_profile(
                                    load_cards(card.ref)[0]
                                )
                            ),
                        )
                    try:
                        transition = apply_plan3_card(
                            play_state,
                            card_rule,
                            settings=settings,
                            prior_play_count=native_card.play_count,
                            card_search_counts=current_card_search_counts,
                            card_move_selection=tuple(
                                candidate.card.ref
                                for candidate in selected_targets
                            ),
                            card_move_selection_sources=tuple(
                                candidate.source_zone
                                for candidate in selected_targets
                            ),
                            card_move_searches=card_move_searches,
                            playing_guid=native_card.guid,
                            forced_use_pool=_use_pool_command is not None,
                            detached_use_pool=(
                                _use_pool_command is not None
                                and _use_pool_command.detached_card is not None
                            ),
                            use_pool_consume_cost=(
                                False
                                if _use_pool_command is None
                                else _use_pool_command.is_consume_cost
                            ),
                            direct_effect_repeat_count=(
                                direct_effect_repeat_count
                            ),
                            externally_handled_play_count_interval_instance_ids=(
                                managed_interval_ids
                            ),
                            pre_direct_transform=pre_direct_transform,
                        )
                    except ValueError as error:
                        diagnose(
                            path,
                            "card-effect",
                            f"{type(error).__name__}:{error}",
                            card_guid=native_card.guid,
                        )
                        continue
                    if not transition.supported:
                        diagnose(
                            path,
                            "card-effect",
                            *(
                                transition.unsupported_rules
                                or ("engine-unsupported",)
                            ),
                            card_guid=native_card.guid,
                        )
                        continue
                    if not transition.legal:
                        continue
                    if transition.unverified_rules:
                        diagnose(
                            path,
                            "card-effect",
                            *(
                                f"unverified:{rule}"
                                for rule in transition.unverified_rules
                            ),
                            card_guid=native_card.guid,
                        )
                        continue
                    transition = replace(transition, before=path.state)
                    try:
                        runtime_add_grow_mutations: tuple[
                            NiaAddGrowMutation, ...
                        ] = ()
                        accepted_play_execution: (
                            Plan3NativeAcceptedPlayExtensionResult | None
                        ) = None
                        accepted_extension_state = (
                            path.turn_start_extension_state
                        )
                        if pre_direct_transform is not None:
                            if len(pre_direct_captures) != 1:
                                raise Plan3NativeStateError(
                                    "accepted-play-pre-direct-capture-arity",
                                    str(len(pre_direct_captures)),
                                )
                            capture = pre_direct_captures[0]
                            accepted_play_execution = capture.execution
                            branch_native = capture.native_state
                            runtime_add_grow_mutations = (
                                capture.runtime_add_grow_mutations
                            )
                            accepted_extension_state = (
                                accepted_play_execution.extension_state
                            )
                        else:
                            for event in transition.runtime_effect_events:
                                if (
                                    event.effect_type != EFFECT_ADD_GROW
                                    or event.phase != NATIVE_PHASE_CARD_PLAY
                                ):
                                    continue
                                branch_native, mutations = (
                                    apply_plan3_native_add_grow_effects(
                                        branch_native,
                                        (event.effect,),
                                        database=database,
                                        playing_guid=native_card.guid,
                                        card_profile_by_card=(
                                            lambda card: (
                                                _plan3_add_grow_card_profile(
                                                    load_cards(card.ref)[0]
                                                )
                                            )
                                        ),
                                    )
                                )
                                runtime_add_grow_mutations = (
                                    *runtime_add_grow_mutations,
                                    *mutations,
                                )
                        draw_lesson_type = path.state.lesson_type
                        if path.state.is_battle:
                            draw_lesson_type = {
                                1: LESSON_VOCAL,
                                2: LESSON_DANCE,
                                3: LESSON_VISUAL,
                            }.get(path.state.current_parameter_type, "")
                            if not draw_lesson_type:
                                raise Plan3NativeStateError(
                                    "battle-current-parameter-type-unknown"
                                )
                        native_add_grow_ids = {
                            effect.id
                            for effect in transition.native_add_grow_effects
                        }
                        if (
                            branch_native.anti_debuff_runtime
                            != path.state.anti_debuff_runtime
                        ):
                            raise Plan3NativeStateError(
                                "plan3-anti-debuff-projection-mismatch"
                            )
                        # Scalar status-add gates (currently BlockRestriction)
                        # execute before GUID-native deferred effects.  Mirror
                        # that one computed runtime into the native carrier;
                        # this is not a post-state observation or a second
                        # AntiDebuff executor.
                        branch_native = replace(
                            branch_native,
                            anti_debuff_runtime=(
                                transition.after.anti_debuff_runtime
                            ),
                            enthusiastic_runtime=(
                                transition.after.enthusiastic_runtime
                            ),
                        )
                        release_status_receipts = tuple(
                            transition.release_status_receipts
                        )
                        release_cursor = path.state.next_status_uid
                        for release_receipt in release_status_receipts:
                            if isinstance(release_receipt, AntiDebuffExecution):
                                if release_receipt.operation == "installed":
                                    release_cursor += 1
                            else:
                                if release_receipt.cursor_before != release_cursor:
                                    raise Plan3NativeStateError(
                                        "release-status-cursor-order-unprojected",
                                        str(release_cursor),
                                    )
                                release_cursor = release_receipt.cursor_after
                        prior_listener_instance_ids = frozenset(
                            listener.instance_id
                            for listener in path.state.active_status_enchants
                        )
                        newly_installed_listeners = tuple(
                            listener
                            for listener in transition.after.active_status_enchants
                            if listener.instance_id
                            not in prior_listener_instance_ids
                        )
                        compatibility_listener_uids = tuple(
                            sorted(
                                listener.native_uid
                                for listener in newly_installed_listeners
                            )
                        )
                        ordered_status_dispatcher_exact = (
                            path.state.status_uid_cursor_exact
                            and transition.after.status_uid_cursor_exact
                            and compatibility_listener_uids
                            == tuple(
                                range(
                                    release_cursor,
                                    release_cursor
                                    + len(newly_installed_listeners),
                                )
                            )
                            and transition.after.next_status_uid
                            == release_cursor + len(newly_installed_listeners)
                        )
                        variants = (
                            _Plan3NativeEffectVariant(
                                branch_native,
                                transition.after,
                                status_cursor=release_cursor,
                                add_grow_mutations=(
                                    *runtime_add_grow_mutations,
                                    *(
                                        ()
                                        if accepted_play_execution is None
                                        else accepted_play_execution.add_grow_mutations
                                    ),
                                ),
                                queued_use_pool_commands=(
                                    queued_use_pool_commands
                                ),
                                queued_use_pool_effect_ids=tuple(
                                    DECK_GRAVE_SELECT_EFFECT_ID
                                    for _ in queued_use_pool_commands
                                ),
                                status_operation_receipts=(
                                    release_status_receipts
                                ),
                            ),
                        )
                        native_direct_effects = tuple(
                            effect
                            for pass_index in range(
                                direct_effect_repeat_count + 1
                            )
                            for effect in card_rule.effects
                            if not (
                                (
                                    native_card.play_count > 0
                                    or pass_index > 0
                                )
                                and effect.once
                            )
                        )
                        has_direct_ordered_status = any(
                            effect.effect_type
                            in {EFFECT_ANTI_DEBUFF, EFFECT_PLAYABLE_VALUE_ADD}
                            for effect in native_direct_effects
                        )
                        has_direct_playable = any(
                            effect.effect_type == EFFECT_PLAYABLE_VALUE_ADD
                            for effect in native_direct_effects
                        )
                        if (
                            ordered_status_dispatcher_exact
                            and has_direct_ordered_status
                        ):
                            (
                                reconciled_scalar,
                                _reconciled_status_cursor,
                            ) = _reconcile_preinstalled_direct_listener_uids(
                                transition.after,
                                native_direct_effects,
                                source_card_id=card_rule.id,
                                operation_cursor=release_cursor,
                                trigger_state=path.state,
                                card_search_counts=(
                                    current_card_search_counts
                                ),
                                prior_instance_ids=(
                                    prior_listener_instance_ids
                                ),
                                new_listeners=newly_installed_listeners,
                            )
                            variants = tuple(
                                replace(
                                    variant,
                                    scalar_state=reconciled_scalar,
                                )
                                for variant in variants
                            )
                        for effect_sequence_index, effect in enumerate(
                            native_direct_effects
                        ):
                            if effect.trigger is not None:
                                decision = evaluate_plan3_trigger(
                                    effect.trigger,
                                    path.state,
                                    card_search_counts=(
                                        current_card_search_counts
                                    ),
                                )
                                if not decision.supported:
                                    raise Plan3NativeStateError(
                                        "direct-native-trigger-unsupported",
                                        ",".join(decision.unsupported_rules),
                                    )
                                if not decision.fires:
                                    continue
                            next_variants: list[
                                _Plan3NativeEffectVariant
                            ] = []
                            for variant in variants:
                                current = variant.state
                                if effect.effect_type == EFFECT_CARD_MOVE:
                                    if (
                                        selected_targets
                                        and not (
                                            path.state.stance
                                            == STANCE_FULL_POWER
                                            and effect.card_move_rule.destination
                                            == MOVE_HOLD
                                        )
                                    ):
                                        before_move = current
                                        current_targets = (
                                            _rematch_card_move_targets(
                                                current,
                                                selected_targets,
                                            )
                                        )
                                        current = current.move_card_targets(
                                            current_targets,
                                            effect.card_move_rule.destination,
                                            hold_limit=settings.hold_limit,
                                            hand_limit=settings.hand_limit,
                                            is_full_power=(
                                                path.state.stance
                                                == STANCE_FULL_POWER
                                            ),
                                            lesson_type=draw_lesson_type,
                                            support_upgrades=supports,
                                            support_card_searches=searches,
                                        )
                                        for dispatched in dispatch_plan3_guid_move_commits(
                                            before_move,
                                            current,
                                            (target.guid for target in current_targets),
                                            hand_limit=settings.hand_limit,
                                            hold_limit=settings.hold_limit,
                                            lesson_type=draw_lesson_type,
                                            is_full_power=False,
                                            support_upgrades=supports,
                                            support_card_searches=searches,
                                            database=Path(database),
                                        ):
                                            next_variants.append(
                                                replace(
                                                    variant,
                                                    state=dispatched.state,
                                                    add_grow_mutations=(
                                                        *variant.add_grow_mutations,
                                                        *dispatched.add_grow_mutations,
                                                    ),
                                                    move_effect_traces=(
                                                        *variant.move_effect_traces,
                                                        *dispatched.traces,
                                                    ),
                                                )
                                            )
                                    else:
                                        next_variants.append(variant)
                                elif effect.effect_type == EFFECT_CARD_DRAW:
                                    if variant.direct_draw is not None:
                                        raise Plan3NativeStateError(
                                            "multiple-direct-card-draw-order-unmodelled"
                                        )
                                    direct_draw = current.draw_to_hand(
                                        effect.value1,
                                        hand_limit=settings.hand_limit,
                                        lesson_type=draw_lesson_type,
                                        support_upgrades=supports,
                                        support_card_searches=searches,
                                        effect_draw=True,
                                    )
                                    for dispatched in dispatch_plan3_guid_move_commits(
                                        direct_draw.before,
                                        direct_draw.after,
                                        direct_draw.drawn_guids,
                                        hand_limit=settings.hand_limit,
                                        hold_limit=settings.hold_limit,
                                        lesson_type=draw_lesson_type,
                                        is_full_power=(
                                            path.state.stance
                                            == STANCE_FULL_POWER
                                        ),
                                        support_upgrades=supports,
                                        support_card_searches=searches,
                                        database=Path(database),
                                    ):
                                        next_variants.append(
                                            replace(
                                                variant,
                                                state=dispatched.state,
                                                direct_draw=direct_draw,
                                                add_grow_mutations=(
                                                    *variant.add_grow_mutations,
                                                    *dispatched.add_grow_mutations,
                                                ),
                                                move_effect_traces=(
                                                    *variant.move_effect_traces,
                                                    *dispatched.traces,
                                                ),
                                            )
                                        )
                                elif (
                                    effect.effect_type == EFFECT_ADD_GROW
                                    and effect.id in native_add_grow_ids
                                ):
                                    current, mutations = (
                                        apply_plan3_native_add_grow_effects(
                                            current,
                                            (effect,),
                                            database=database,
                                            playing_guid=native_card.guid,
                                            card_profile_by_card=(
                                                lambda card: (
                                                    _plan3_add_grow_card_profile(
                                                        load_cards(card.ref)[0]
                                                    )
                                                )
                                            ),
                                        )
                                    )
                                    next_variants.append(
                                        replace(
                                            variant,
                                            state=current,
                                            add_grow_mutations=(
                                                *variant.add_grow_mutations,
                                                *mutations,
                                            ),
                                        )
                                    )
                                elif (
                                    effect.effect_type
                                    == EFFECT_SEARCH_PLAY_CARD_STAMINA_CHANGE
                                ):
                                    current, application = (
                                        _apply_native_search_stamina_effect(
                                            current,
                                            effect,
                                            playing_guid=native_card.guid,
                                            database=Path(database),
                                        )
                                    )
                                    next_variants.append(
                                        replace(
                                            variant,
                                            state=current,
                                            search_stamina_applications=(
                                                *variant.search_stamina_applications,
                                                application,
                                            ),
                                        )
                                    )
                                elif (
                                    effect.effect_type
                                    == EFFECT_HAND_GRAVE_COUNT_CARD_DRAW
                                ):
                                    current, execution = (
                                        _apply_native_hand_grave_draw_effect(
                                            current,
                                            playing_guid=native_card.guid,
                                            hand_limit=settings.hand_limit,
                                            lesson_type=draw_lesson_type,
                                            support_upgrades=supports,
                                            support_card_searches=searches,
                                            database=Path(database),
                                        )
                                    )
                                    next_variants.append(
                                        replace(
                                            variant,
                                            state=current,
                                            hand_grave_draws=(
                                                *variant.hand_grave_draws,
                                                execution,
                                            ),
                                        )
                                    )
                                elif (
                                    effect.effect_type
                                    == EFFECT_FORCE_PLAY_CARD_SEARCH
                                    and effect.id == HOLD_ALL_EFFECT_ID
                                ):
                                    planned = plan_force_play_card_search(
                                        current,
                                        force_play_contract_for_effect(
                                            HOLD_ALL_EFFECT_ID,
                                            Path(database),
                                        ),
                                        playing_guid=native_card.guid,
                                        execution_input=ForcePlayExecutionInput(),
                                        database=Path(database),
                                    )
                                    if not planned.resolved:
                                        raise Plan3NativeStateError(
                                            "hold-all-force-play-unresolved",
                                            (
                                                f"{planned.branch.reason}:"
                                                f"{planned.branch.detail}"
                                            ).rstrip(":"),
                                        )
                                    next_variants.append(
                                        replace(
                                            variant,
                                            queued_use_pool_commands=(
                                                *variant.queued_use_pool_commands,
                                                *(
                                                    Plan3UsePoolCommand.from_queued(command)
                                                    for command in planned.commands
                                                ),
                                            ),
                                            queued_use_pool_effect_ids=(
                                                *variant.queued_use_pool_effect_ids,
                                                *(
                                                    HOLD_ALL_EFFECT_ID
                                                    for _ in planned.commands
                                                ),
                                            ),
                                        )
                                    )
                                elif effect.effect_type == EFFECT_CARD_UPGRADE:
                                    for current, result in (
                                        _apply_native_card_upgrade_effect(
                                            current,
                                            effect,
                                            playing_guid=native_card.guid,
                                            database=Path(database),
                                        )
                                    ):
                                        next_variants.append(
                                            replace(
                                                variant,
                                                state=current,
                                                card_upgrade_results=(
                                                    *variant.card_upgrade_results,
                                                    result,
                                                ),
                                            )
                                        )
                                elif effect.effect_type == EFFECT_CARD_CREATE_ID:
                                    contract = load_card_create_contract(
                                        effect.id
                                    )
                                    current, result = (
                                        _apply_native_card_create_id_effect(
                                            current,
                                            effect,
                                            contract,
                                            playing_guid=native_card.guid,
                                            hand_limit=settings.hand_limit,
                                            path_token=native_guid_path_token(path),
                                            source_play_count=(
                                                native_card.play_count
                                            ),
                                            effect_sequence_index=(
                                                effect_sequence_index
                                            ),
                                            guid_provider=guid_provider,
                                        )
                                    )
                                    next_variants.append(
                                        replace(
                                            variant,
                                            state=current,
                                            card_create_results=(
                                                *variant.card_create_results,
                                                result,
                                            ),
                                        )
                                    )
                                elif (
                                    effect.effect_type
                                    == EFFECT_CARD_CREATE_SEARCH
                                ):
                                    contract = load_card_create_search_contract(
                                        effect.id
                                    )
                                    current, result = (
                                        _apply_native_card_create_search_effect(
                                            current,
                                            effect,
                                            contract,
                                            playing_guid=native_card.guid,
                                            plan_type=plan_type,
                                            plan_ignore_card_ids=(
                                                plan_ignore_card_ids
                                            ),
                                            hand_limit=(
                                                card_create_hand_limit
                                            ),
                                            path_token=(
                                                native_guid_path_token(path)
                                            ),
                                            source_play_count=(
                                                native_card.play_count
                                            ),
                                            effect_sequence_index=(
                                                effect_sequence_index
                                            ),
                                            guid_provider=guid_provider,
                                        )
                                    )
                                    next_variants.append(
                                        replace(
                                            variant,
                                            state=current,
                                            card_create_search_results=(
                                                *variant.card_create_search_results,
                                                result,
                                            ),
                                        )
                                    )
                                elif effect.effect_type == EFFECT_ANTI_DEBUFF:
                                    current, scalar_after, execution = (
                                        _apply_native_anti_debuff_effect(
                                            current,
                                            variant.scalar_state,
                                            effect,
                                            operation_cursor=(
                                                variant.status_cursor
                                            ),
                                        )
                                    )
                                    next_variants.append(
                                        replace(
                                            variant,
                                            state=current,
                                            scalar_state=scalar_after,
                                            status_cursor=(
                                                variant.status_cursor + 1
                                                if execution.operation
                                                == "installed"
                                                else variant.status_cursor
                                            ),
                                            anti_debuff_executions=(
                                                *variant.anti_debuff_executions,
                                                execution,
                                            ),
                                            status_operation_receipts=(
                                                *variant.status_operation_receipts,
                                                *(
                                                    (execution,)
                                                    if ordered_status_dispatcher_exact
                                                    else ()
                                                ),
                                            ),
                                        )
                                    )
                                elif effect.effect_type == EFFECT_PLAYABLE_VALUE_ADD:
                                    if (
                                        not ordered_status_dispatcher_exact
                                        and not variant.scalar_state
                                        .playable_value_add_runtime.status_present
                                    ):
                                        next_variants.append(variant)
                                        continue
                                    scalar_after, receipt = (
                                        _apply_native_playable_value_add_effect(
                                            variant.scalar_state,
                                            effect,
                                            operation_cursor=(
                                                variant.status_cursor
                                            ),
                                        )
                                    )
                                    next_variants.append(
                                        replace(
                                            variant,
                                            scalar_state=scalar_after,
                                            status_cursor=(
                                                receipt.cursor_after
                                            ),
                                            status_operation_receipts=(
                                                *variant.status_operation_receipts,
                                                receipt,
                                            ),
                                        )
                                    )
                                elif effect.effect_type == EFFECT_STATUS_ENCHANT:
                                    current = _install_exact_card_play_after_listener(
                                        current,
                                        effect,
                                        playing_guid=native_card.guid,
                                        database=Path(database),
                                    )
                                    status_cursor = variant.status_cursor
                                    scalar_after = variant.scalar_state
                                    reconciled_instance_ids = (
                                        variant.reconciled_listener_instance_ids
                                    )
                                    if ordered_status_dispatcher_exact:
                                        reconciled_instance_id = (
                                            _confirm_preinstalled_direct_listener_uid(
                                                scalar_after,
                                                effect,
                                                source_card_id=card_rule.id,
                                                operation_cursor=status_cursor,
                                                prior_instance_ids=(
                                                    prior_listener_instance_ids
                                                ),
                                                reconciled_instance_ids=(
                                                    reconciled_instance_ids
                                                ),
                                            )
                                        )
                                        status_cursor += 1
                                        reconciled_instance_ids = (
                                            *reconciled_instance_ids,
                                            reconciled_instance_id,
                                        )
                                    next_variants.append(
                                        replace(
                                            variant,
                                            state=current,
                                            scalar_state=scalar_after,
                                            status_cursor=status_cursor,
                                            reconciled_listener_instance_ids=(
                                                reconciled_instance_ids
                                            ),
                                        )
                                    )
                                elif effect.effect_type in {
                                    EFFECT_STATUS_ENCHANT_ENCORE,
                                    EFFECT_TIMER,
                                }:
                                    if not ordered_status_dispatcher_exact:
                                        next_variants.append(variant)
                                        continue
                                    scalar_after = variant.scalar_state
                                    reconciled_instance_id = (
                                        _confirm_preinstalled_direct_listener_uid(
                                            variant.scalar_state,
                                            effect,
                                            source_card_id=card_rule.id,
                                            operation_cursor=(
                                                variant.status_cursor
                                            ),
                                            prior_instance_ids=(
                                                prior_listener_instance_ids
                                            ),
                                            reconciled_instance_ids=(
                                                variant.reconciled_listener_instance_ids
                                            ),
                                        )
                                    )
                                    next_variants.append(
                                        replace(
                                            variant,
                                            scalar_state=scalar_after,
                                            status_cursor=(
                                                variant.status_cursor + 1
                                            ),
                                            reconciled_listener_instance_ids=(
                                                *variant.reconciled_listener_instance_ids,
                                                reconciled_instance_id,
                                            ),
                                        )
                                    )
                                else:
                                    next_variants.append(variant)
                            # Phase-13 children are queued after the direct
                            # effect that committed the gauge difference,
                            # before a later direct move/draw/create command.
                            # Do not postpone deck_all growth until final move
                            # or replay it again as CardPlayAfter growth.
                            direct_runtime_grows = tuple(
                                event for event in transition.runtime_effect_events
                                if event.phase == PHASE_STATUS_CHANGE
                                and event.source_direct_effect_index == effect_sequence_index
                                and event.effect_type == EFFECT_ADD_GROW
                            )
                            if direct_runtime_grows:
                                grown_variants = []
                                for next_variant in next_variants:
                                    grown, mutations = apply_plan3_native_add_grow_effects(
                                        next_variant.state,
                                        tuple(event.effect for event in direct_runtime_grows),
                                        database=database, playing_guid=native_card.guid,
                                        card_profile_by_card=lambda card: _plan3_add_grow_card_profile(load_cards(card.ref)[0]),
                                    )
                                    grown_variants.append(replace(next_variant, state=grown,
                                        add_grow_mutations=(*next_variant.add_grow_mutations, *mutations)))
                                next_variants = grown_variants
                            variants = tuple(next_variants)
                    except (
                        Plan3NativeStateError,
                        CardCreateError,
                        CardCreateSearchError,
                    ) as error:
                        diagnose(
                            path,
                            "card-native-zone",
                            f"{error.code}:{error.detail}".rstrip(":"),
                            card_guid=native_card.guid,
                        )
                        continue
                    for variant in variants:
                        try:
                            if (
                                ordered_status_dispatcher_exact
                                and has_direct_ordered_status
                                and frozenset(
                                    variant.reconciled_listener_instance_ids
                                )
                                != frozenset(
                                    listener.instance_id
                                    for listener in newly_installed_listeners
                                )
                            ):
                                raise Plan3NativeStateError(
                                    "direct-listener-reconciliation-incomplete",
                                    card_rule.id,
                                )
                            direct_playable_add = sum(
                                receipt.value_after - receipt.value_before
                                for receipt in variant.status_operation_receipts
                                if isinstance(
                                    receipt,
                                    PlayableValueAddReceipt,
                                )
                                and receipt.operation
                                in {
                                    PlayableValueAddOperation.ADD_CREATE,
                                    PlayableValueAddOperation.ADD_MERGE,
                                }
                            )
                            if (
                                ordered_status_dispatcher_exact
                                or not has_direct_playable
                            ) and transition.extra_plays != direct_playable_add:
                                raise Plan3NativeStateError(
                                    "playable-value-add-release-unprojected",
                                    (
                                        f"transition={transition.extra_plays};"
                                        f"direct={direct_playable_add}"
                                    ),
                                )
                            grown_native = variant.state.apply_card_grow_events(
                                path.state, variant.scalar_state
                            )
                            native_destination = (
                                _NATIVE_DESTINATION_BY_SCALAR_ZONE.get(
                                    transition.moved_to,
                                    card_rule.move_position_type,
                                )
                            )
                            move_card_before = grown_native.card_by_guid(
                                native_card.guid
                            )
                            committed_native = grown_native.play_hand_by_guid(
                                native_card.guid,
                                native_destination,
                            )
                            settled_scalar = variant.scalar_state
                            status_operation_receipts = (
                                variant.status_operation_receipts
                            )
                            if (
                                _use_pool_command is None
                                and settled_scalar.playable_value_add_runtime
                                .status_present
                            ):
                                try:
                                    playable_use = use_playable_value_add(
                                        settled_scalar.playable_value_add_runtime,
                                        next_status_uid=(
                                            variant.status_cursor
                                        ),
                                        source="ordinary-move-play-card",
                                    )
                                except (
                                    OverflowError,
                                    TypeError,
                                    ValueError,
                                ) as error:
                                    raise Plan3NativeStateError(
                                        "playable-value-add-use-kernel",
                                        f"{type(error).__name__}:{error}",
                                    ) from error
                                if (
                                    playable_use.operation
                                    not in {
                                        PlayableValueAddOperation.USE_RETAIN,
                                        PlayableValueAddOperation.USE_REMOVE,
                                    }
                                ):
                                    raise Plan3NativeStateError(
                                        "playable-value-add-use-shape-unprojected",
                                        playable_use.operation.value,
                                    )
                                settled_scalar = replace(
                                    settled_scalar,
                                    playable_value_add_runtime=(
                                        playable_use.after
                                    ),
                                    # Compatibility scalar execution may
                                    # already contain an intervening exact
                                    # status allocation.  It must agree with
                                    # the ordered operation cursor here.
                                    next_status_uid=settled_scalar.next_status_uid,
                                )
                                if (
                                    settled_scalar.next_status_uid
                                    != playable_use.cursor_after
                                ):
                                    raise Plan3NativeStateError(
                                        "playable-value-add-use-cursor-drift",
                                        (
                                            f"scalar={settled_scalar.next_status_uid};"
                                            f"operation={playable_use.cursor_after}"
                                        ),
                                    )
                                status_operation_receipts = (
                                    *status_operation_receipts,
                                    playable_use,
                                )
                            operation_receipts = build_receipts
                            if _use_pool_command is None:
                                move_card_after = (
                                    committed_native.card_by_guid(
                                        native_card.guid
                                    )
                                )
                                operation_receipts = (
                                    *operation_receipts,
                                    CardPlayCountOperationReceipt(
                                        family=(
                                            CARD_PLAY_COUNT_RECEIPT_FAMILY
                                        ),
                                        operation=(
                                            CARD_PLAY_COUNT_INCREMENT_OPERATION
                                        ),
                                        subject_id=native_card.guid,
                                        owner=(
                                            PLAN3_ORDINARY_CARD_PLAY_COUNT_MOVE_OWNER
                                        ),
                                        before_value=(
                                            move_card_before.play_count
                                        ),
                                        after_value=(
                                            move_card_after.play_count
                                        ),
                                    ),
                                )
                            detached_played_card = (
                                None if _use_pool_command is None
                                else _use_pool_command.detached_card
                            )
                            if detached_played_card is not None:
                                committed_native = replace(committed_native, **{
                                    name: tuple(card for card in getattr(committed_native, name)
                                                if card.guid != native_card.guid)
                                    for name in ("hand", "deck", "grave", "lost", "hold")
                                })
                                settlement_dispatch = (Plan3MoveDispatchVariant(committed_native),)
                            else:
                                settlement_dispatch = dispatch_plan3_guid_move_commits(
                                    grown_native,
                                    committed_native,
                                    (native_card.guid,),
                                    hand_limit=settings.hand_limit,
                                    hold_limit=settings.hold_limit,
                                    lesson_type=draw_lesson_type,
                                    is_full_power=(
                                        variant.scalar_state.stance
                                        == STANCE_FULL_POWER
                                    ),
                                    support_upgrades=supports,
                                    support_card_searches=searches,
                                    database=Path(database),
                                )
                            if len(settlement_dispatch) != 1:
                                raise Plan3NativeStateError(
                                    "final-move-callback-branching-unmodelled",
                                    native_card.guid,
                                )
                            dispatched_settlement = settlement_dispatch[0]
                            native_after = dispatched_settlement.state
                            runtime_after_mutations: tuple[NiaAddGrowMutation, ...] = ()
                            # Item/global CardPlayAfter listeners are already
                            # selected and spent by the scalar event queue.
                            # Their AddGrow commands apply after the final
                            # card move, to the actual GUID Hold/other pools.
                            # CardPlay-phase grows were applied before the
                            # direct card effects and must not be replayed.
                            for event in transition.runtime_effect_events:
                                if (event.phase != NATIVE_PHASE_CARD_PLAY_AFTER
                                        or event.effect_type != EFFECT_ADD_GROW):
                                    continue
                                native_after, mutations = apply_plan3_native_add_grow_effects(
                                    native_after, (event.effect,), database=Path(database),
                                    card_profile_by_card=lambda card: _plan3_add_grow_card_profile(load_cards(card.ref)[0]),
                                )
                                runtime_after_mutations = (*runtime_after_mutations, *mutations)
                            synchronized = _synchronize_zones(
                                settled_scalar, native_after
                            )
                            (
                                native_after,
                                card_play_after_mutations,
                                skipped_listener_guids,
                            ) = _apply_exact_card_play_after_listeners(
                                synchronized,
                                native_after,
                                native_card.guid,
                                card_rule,
                                database=Path(database),
                            )
                            card_play_after_mutations = (*runtime_after_mutations, *card_play_after_mutations)
                            native_after = (
                                native_after.apply_card_play_after_grow_events(
                                    native_card.guid,
                                    is_full_power=(
                                        variant.scalar_state.stance
                                        == STANCE_FULL_POWER
                                    ),
                                    played_effect_group_ids=(
                                        card_rule.effect_group_ids
                                    ),
                                    skip_listener_guids=(
                                        skipped_listener_guids
                                    ),
                                    detached_played_card=detached_played_card,
                                )
                            )
                            synchronized = _synchronize_zones(
                                settled_scalar, native_after
                            )
                            owned_direct_status_count = sum(
                                isinstance(receipt, AntiDebuffExecution)
                                or (
                                    isinstance(
                                        receipt,
                                        PlayableValueAddReceipt,
                                    )
                                    and receipt.operation
                                    in {
                                        PlayableValueAddOperation.ADD_CREATE,
                                        PlayableValueAddOperation.ADD_MERGE,
                                    }
                                )
                                for receipt in status_operation_receipts
                            )
                            declared_direct_status_count = sum(
                                effect.effect_type
                                in {
                                    EFFECT_ANTI_DEBUFF,
                                    EFFECT_PLAYABLE_VALUE_ADD,
                                }
                                for effect in native_direct_effects
                            )
                            if (
                                variant.search_stamina_applications
                                or (
                                    not ordered_status_dispatcher_exact
                                    and has_direct_ordered_status
                                    and owned_direct_status_count
                                    != declared_direct_status_count
                                )
                            ):
                                synchronized = replace(
                                    synchronized,
                                    status_uid_cursor_exact=False,
                                )
                            variant_transition = replace(
                                transition, after=synchronized
                            )
                            native_after.assert_plan3_projection(synchronized)
                        except Plan3NativeStateError as error:
                            diagnose(
                                path,
                                "card-native-zone",
                                f"{error.code}:{error.detail}".rstrip(":"),
                                card_guid=native_card.guid,
                            )
                            continue
                        action = Plan3NativeSearchAction(
                            native_card.guid,
                            ref,
                            (
                                *(candidate.guid for candidate in selected_targets),
                                *(command.guid for command in variant.queued_use_pool_commands),
                            ),
                        )
                        if _single_action is not None and action != _single_action:
                            # Selection identity belongs to the caller.  Do
                            # not return a different Select branch merely
                            # because it has the same source card.
                            continue
                        step = Plan3NativeSearchStep(
                            kind="card",
                            before=path.state,
                            after=synchronized,
                            native_before=path.native_state,
                            native_after=native_after,
                            action=action,
                            card_transition=variant_transition,
                            native_draw=variant.direct_draw,
                            add_grow_mutations=(
                                *variant.add_grow_mutations,
                                *dispatched_settlement.add_grow_mutations,
                                *card_play_after_mutations,
                            ),
                            search_stamina_applications=(
                                variant.search_stamina_applications
                            ),
                            search_stamina_payment=search_stamina_payment,
                            hand_grave_draws=variant.hand_grave_draws,
                            card_upgrade_results=(
                                variant.card_upgrade_results
                            ),
                            card_create_results=(
                                variant.card_create_results
                            ),
                            card_create_search_results=(
                                variant.card_create_search_results
                            ),
                            anti_debuff_executions=(
                                variant.anti_debuff_executions
                            ),
                            status_operation_receipts=(
                                status_operation_receipts
                            ),
                            queued_use_pool_commands=(
                                variant.queued_use_pool_commands
                            ),
                            queued_use_pool_effect_ids=(
                                variant.queued_use_pool_effect_ids
                            ),
                            move_effect_traces=(
                                *variant.move_effect_traces,
                                *dispatched_settlement.traces,
                            ),
                            accepted_play_extension_trace=(
                                None
                                if accepted_play_execution is None
                                else accepted_play_execution.trace
                            ),
                            global_card_play_count_delta=1,
                            history_event_count=1,
                            final_move_count=1,
                            playable_count_consumed=(
                                0 if _use_pool_command is not None else 1
                            ),
                            operation_receipts=operation_receipts,
                        )
                        uid_cursor_gap = (
                            not synchronized.status_uid_cursor_exact
                        )
                        if uid_cursor_gap:
                            diagnose(
                                path,
                                "card-native-zone",
                                (
                                    "playable-value-add-native-status-uid-"
                                    f"unprojected:{card_rule.id}"
                                ),
                                card_guid=native_card.guid,
                            )
                        children.append(
                            make_path(
                                synchronized,
                                native_after,
                                (*path.steps, step),
                                extension_state=(
                                    accepted_extension_state
                                ),
                                drinks=path.drink_inventory,
                                stopped_reason=(
                                    "status-uid-cursor-unprojected"
                                    if uid_cursor_gap
                                    else ""
                                ),
                            )
                        )
                        exact_child = True

            if include_skip:
                skipped = replace(path.state, plays_remaining=0)
                step = Plan3NativeSearchStep(
                    kind="skip",
                    before=path.state,
                    after=skipped,
                    native_before=path.native_state,
                    native_after=path.native_state,
                )
                children.append(
                    make_path(
                        skipped,
                        path.native_state,
                        (*path.steps, step),
                        extension_state=path.turn_start_extension_state,
                        drinks=path.drink_inventory,
                    )
                )
                exact_child = True
            if not exact_child:
                finished.append(stop(path, "no-supported-legal-card"))

        if not children:
            break
        if _root_only:
            # The initial frontier contains exactly one root path.  Returning
            # here preserves every first decision branch and deliberately
            # bypasses the normal state deduplication and beam/principal
            # ranking below.  The public adapter performs semantic action
            # deduplication (GUID/slot identity) after this exact expansion.
            return Plan3NativeSearchResult(
                best=None,
                candidates=tuple(children),
                diagnostics=tuple(diagnostics),
                expanded_nodes=expanded_nodes,
                deduplicated_nodes=deduplicated_nodes,
                beam_width=beam_width,
                depth=depth,
                battle_parameter_schedule=battle_schedule,
                battle_ranking_resolved=battle_ranking_resolved,
                include_skip=include_skip,
                force_end_score=force_end_score,
                force_end_stamina_recovery=force_end_stamina_recovery,
                initial_drink_ids=initial_drinks.ids,
            )
        by_state: dict[
            tuple[
                Plan3State,
                Plan3NativeState,
                Plan3DrinkInventory,
                object | None,
                tuple[Plan3UsePoolCommand, ...],
            ],
            Plan3NativeSearchPath,
        ] = {}
        for child in children:
            key = (
                child.state,
                child.native_state,
                child.drink_inventory,
                child.turn_start_extension_state,
                pending_use_pool_key(child),
            )
            previous = by_state.get(key)
            if previous is None or path_key(child) > path_key(previous):
                if previous is not None:
                    deduplicated_nodes += 1
                by_state[key] = child
            else:
                deduplicated_nodes += 1
        ranked = sorted(by_state.values(), key=path_key, reverse=True)
        frontier = tuple(ranked[:beam_width])

    ranked_finished = tuple(sorted(finished, key=path_key, reverse=True))
    return Plan3NativeSearchResult(
        best=(ranked_finished[0] if ranked_finished else None),
        candidates=ranked_finished,
        diagnostics=tuple(diagnostics),
        expanded_nodes=expanded_nodes,
        deduplicated_nodes=deduplicated_nodes,
        beam_width=beam_width,
        depth=depth,
        battle_parameter_schedule=battle_schedule,
        battle_ranking_resolved=battle_ranking_resolved,
        include_skip=include_skip,
        force_end_score=force_end_score,
        force_end_stamina_recovery=force_end_stamina_recovery,
        initial_drink_ids=initial_drinks.ids,
    )


def _root_decision_step(
    path: Plan3NativeSearchPath,
) -> tuple[Plan3NativeSearchStep | None, tuple[str, ...]]:
    decisions = path.decision_steps
    if len(decisions) != 1:
        return None, ("root-decision-count-not-one",)
    return decisions[0], ()


def _nested_driver_blockers(step: Plan3NativeSearchStep) -> tuple[str, ...]:
    """Return card branches the current Plan3 driver cannot express.

    Automatic ``ALL``/``RANDOM`` card moves are native effects and remain
    provenance on the card candidate.  ``SELECT`` card moves and deferred
    UsePool commands require another user-visible selection, while the
    current ``MaaPlan3ActionDriver`` only submits the Hand card slot.
    """

    if step.kind != "card":
        return ()
    blockers: list[str] = []
    if step.queued_use_pool_commands:
        blockers.append("nested-use-pool-selection-unrepresentable")
    transition = step.card_transition
    if transition is None:
        return tuple(blockers)
    for effect in transition.card.effects:
        if effect.effect_type != EFFECT_CARD_MOVE or effect.card_move_rule is None:
            continue
        rule = effect.card_move_rule
        if rule.pick_range_type == PICK_RANGE_SELECT or (
            rule.second_pick_range_type == PICK_RANGE_SELECT
        ):
            blockers.append(f"nested-card-selection-unrepresentable:{effect.id}")
    return tuple(dict.fromkeys(blockers))


def _canonical_root_candidate(
    path: Plan3NativeSearchPath,
) -> tuple[Plan3NativeLegalCandidate | None, tuple[str, ...]]:
    step, blockers = _root_decision_step(path)
    if step is None:
        return None, blockers
    if step.kind == "card":
        if step.action is None or step.card_transition is None:
            return None, ("card-root-action-incomplete",)
        hand_matches = tuple(
            index
            for index, card in enumerate(step.native_before.hand)
            if card.guid == step.action.guid
        )
        if len(hand_matches) != 1:
            return None, ("card-root-guid-not-unique-in-hand",)
        candidate = Plan3NativeLegalCandidate(
            kind="play",
            action_id=f"PLAY:{step.action.guid}",
            native_kind="card",
            hand_index=hand_matches[0],
            card_guid=step.action.guid,
            card_id=step.action.card_ref.card_id,
            card_upgrade=step.action.card_ref.upgrade,
            selected_card_guids=step.action.selected_card_guids,
        )
        return candidate, (*blockers, *_nested_driver_blockers(step))
    if step.kind == "drink":
        if step.drink_action is None:
            return None, ("drink-root-action-incomplete",)
        action = step.drink_action
        selected = action.selected_card_guid or "-"
        candidate = Plan3NativeLegalCandidate(
            kind="drink",
            action_id=f"DRINK:{action.slot_index}:{action.drink_id}:{selected}",
            native_kind="drink",
            drink_slot_index=action.slot_index,
            drink_id=action.drink_id,
            selected_card_guid=action.selected_card_guid,
        )
        return candidate, blockers
    if step.kind == "skip":
        if step.before.plays_remaining < 1:
            return None, ("turn-end-root-not-playable",)
        return (
            Plan3NativeLegalCandidate(
                kind="turn-end",
                action_id="END_TURN",
                native_kind="skip",
            ),
            blockers,
        )
    return None, (*blockers, f"unsupported-root-decision-kind:{step.kind}")


def enumerate_plan3_native_legal_candidates(
    initial_state: Plan3State,
    initial_native_state: Plan3NativeState,
    *,
    include_drinks: bool = True,
    include_turn_end: bool = True,
    root_result_observer: Callable[[Plan3NativeSearchResult], None] | None = None,
    **search_options: object,
) -> Plan3NativeLegalCandidateEnumeration:
    """Publish every exact first-action candidate without search pruning.

    The existing native search owns all card/drink/effect semantics.  This
    wrapper asks it for one unpruned root expansion, then collapses internal
    paths by the action identity the current driver can actually submit.
    ``search_options`` accepts the normal native search dependencies (settings,
    NIA extensions, support inputs, drink inventory, and database); depth and
    ``_root_only`` are owned by this adapter.
    """

    if type(include_drinks) is not bool:
        raise TypeError("include_drinks must be bool")
    if type(include_turn_end) is not bool:
        raise TypeError("include_turn_end must be bool")
    if root_result_observer is not None and not callable(root_result_observer):
        raise TypeError("root_result_observer must be callable")
    options = dict(search_options)
    supplied_include_drinks = options.pop("include_drinks", include_drinks)
    if type(supplied_include_drinks) is not bool:
        raise TypeError("search include_drinks must be bool")
    if supplied_include_drinks != include_drinks:
        raise ValueError("include_drinks was supplied twice with different values")
    supplied_include_skip = options.pop("include_skip", include_turn_end)
    if type(supplied_include_skip) is not bool:
        raise TypeError("search include_skip must be bool")
    if supplied_include_skip != include_turn_end:
        raise ValueError("include_turn_end conflicts with include_skip")
    if options.pop("_root_only", False) is not False:
        raise ValueError("_root_only is owned by the legal-candidate adapter")
    if "_use_pool_command" in options and options["_use_pool_command"] is not None:
        raise ValueError("root legal enumeration cannot start inside UsePool")

    # Root mode returns immediately after the first expansion.  A depth limit
    # would therefore add no authority and is intentionally discarded.
    options.pop("depth", None)
    options["depth"] = None
    options["include_skip"] = include_turn_end
    options["_root_only"] = True
    if not include_drinks:
        options["drink_inventory"] = Plan3DrinkInventory()

    try:
        result = search_plan3_native(
            initial_state,
            initial_native_state,
            **options,
        )
    except (KeyError, OSError, Plan3NativeStateError, TypeError, ValueError) as error:
        return Plan3NativeLegalCandidateEnumeration(
            candidates=(),
            authority=PLAN3_NATIVE_ENUMERATOR_AUTHORITY,
            complete=False,
            blockers=(f"root-search-failed:{type(error).__name__}:{error}",),
        )

    blockers: list[str] = []
    for diagnostic in result.diagnostics:
        # A missing future battle schedule is a wider-horizon advisory gap;
        # it does not invalidate the current root action set.  Every other
        # diagnostic means at least one root branch was not proven.
        if diagnostic.stage == "battle-horizon-scope":
            continue
        gaps = diagnostic.semantic_gaps or ("diagnostic-without-detail",)
        blockers.extend(f"{diagnostic.stage}:{gap}" for gap in gaps)

    if root_result_observer is not None:
        root_result_observer(result)
    by_action_id: dict[str, Plan3NativeLegalCandidate] = {}
    for path in result.candidates:
        candidate, path_blockers = _canonical_root_candidate(path)
        blockers.extend(path_blockers)
        if candidate is not None:
            by_action_id.setdefault(candidate.action_id, candidate)
    if not by_action_id:
        blockers.append("no-supported-root-actions")
    unique_blockers = tuple(dict.fromkeys(blockers))
    candidates = tuple(by_action_id[key] for key in sorted(by_action_id))
    return Plan3NativeLegalCandidateEnumeration(
        candidates=candidates,
        authority=PLAN3_NATIVE_ENUMERATOR_AUTHORITY,
        complete=bool(candidates) and not unique_blockers,
        blockers=unique_blockers,
    )


def execute_plan3_guid_use_pool_transaction(
    state: Plan3State,
    native_state: Plan3NativeState,
    command: Plan3UsePoolCommand,
    *,
    recursion_depth: int = 1,
    **search_options: object,
) -> Plan3NativeSearchResult:
    """Execute one forced GUID with the ordinary local card transaction.

    Ordinary DeckGrave Select1, WithCost NotLost Select1, and a future HoldAll
    planner can all pass the same immutable command here.  The GUID is located
    only when this function begins, and the bounded internal search exposes
    every card-local Select branch without an agent or RNG choice.
    """

    if not isinstance(command, Plan3UsePoolCommand):
        raise TypeError("command must be Plan3UsePoolCommand")
    options = dict(search_options)
    options["depth"] = 1
    options["include_skip"] = False
    options["_use_pool_command"] = command
    options["_use_pool_recursion_depth"] = recursion_depth
    return search_plan3_native(state, native_state, **options)


__all__ = [
    "Plan3EncoreForcedReplay",
    "Plan3ExactEncoreEndTurnResult",
    "Plan3NativeDrinkAction",
    "Plan3NativeLegalCandidate",
    "Plan3NativeLegalCandidateEnumeration",
    "Plan3NativeAcceptedPlayExtension",
    "Plan3NativeAcceptedPlayExtensionResult",
    "Plan3NativeAcceptedPlaySource",
    "Plan3NativeGuidProvider",
    "Plan3NativeGuidRequest",
    "Plan3NativeRuntimeCardUpgradeTransition",
    "Plan3NativeRuntimeDrawTransition",
    "Plan3NativeRuntimeEffectReplay",
    "Plan3MoveDispatchVariant",
    "Plan3NativeSearchAction",
    "Plan3NativeSearchDiagnostic",
    "Plan3NativeSearchPath",
    "Plan3NativePathTieBreaker",
    "Plan3NativeSearchResult",
    "Plan3NativeSearchStep",
    "Plan3StatusOperationReceipt",
    "Plan3NativeTurnStartExtension",
    "Plan3NativeTurnStartExtensionResult",
    "Plan3OpaqueSymbolicGuidProvider",
    "PLAN3_MAX_PATH_TIE_BREAK",
    "Plan3UsePoolTransaction",
    "PLAN3_NATIVE_ENUMERATOR_AUTHORITY",
    "apply_plan3_native_add_grow_effects",
    "apply_plan3_native_runtime_growth",
    "execute_plan3_runtime_effect_events",
    "dispatch_plan3_guid_move_commits",
    "execute_plan3_guid_use_pool_transaction",
    "execute_plan3_exact_encore_end_turn",
    "enumerate_plan3_native_legal_candidates",
    "replay_plan3_runtime_effect_events",
    "search_plan3_native",
]
