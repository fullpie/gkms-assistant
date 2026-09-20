"""Shadow bridge from one native ExamSave state to the Plan 1 reducer.

The Plan 1 core predates the runtime recorder and expects a typed
``Plan1NativeStageState``.  This module is the deliberately small adapter at
that boundary: it projects one *settled* captured ``state_before`` and sends
one normalized replay action through the existing native step executor and
stage reducer.

It is an observation/diagnostic path, not an exact-transition producer.  In
particular, it never accepts or reads ``state_after`` and it never invents a
CardCreateId GUID, a drink effect, or a legal-action set.  Missing runtime
facts are returned as typed blockers while a scalar/card result is exposed
when the existing reducer can evaluate it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3
from typing import Any

from .audition_local_save_state import (
    AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION,
    EXAM_SAVE_DATA_SOURCE_TYPE,
    AuditionLocalSaveStateEvidence,
    LocalSaveExamState,
    parse_local_save_exam_state,
)
from .audition_native_ordered_zones import (
    NATIVE_ORDERED_ZONE_SCHEMA_VERSION,
    NativeEndTurnDisposition,
    NativeOrderedZoneEvidenceBinding,
    NativeOrderedZoneError,
    NativeOrderedZoneState,
    build_native_ordered_zone_state,
    canonical_runtime_digest_aggregate,
    NativeOrderedCardInstance,
)
from .card_search import (
    exact_playing_card_search_mismatches,
    match_exact_target_card_context_search,
)
from .leaderboard_replay import LeaderboardReplayAction
from .logic_engine import load_master_card
from .master_db import DEFAULT_DATABASE
from .nia_static_adapter import NiaGimmickStep, load_nia_gimmick_steps
from .runtime_canonical_json import canonical_sha256
from .plan1_native_core import (
    EFFECT_BLOCK,
    EFFECT_LESSON_BUFF,
    EFFECT_LESSON_BUFF_ADDITIVE,
    EFFECT_LESSON,
    EFFECT_PARAMETER_BUFF,
    EFFECT_PLAYABLE_VALUE_ADD,
    EFFECT_STAMINA_CONSUMPTION_DOWN,
    EFFECT_STAMINA_RECOVER_FIX,
    EFFECT_STATUS_ENCHANT,
    EFFECT_TIMER,
    PHASE_CARD_PLAY,
    Plan1CardTransition,
    Plan1CompiledEffect,
    Plan1CompiledCard,
    Plan1EffectExecution,
    Plan1NativeSettings,
    Plan1PlayCountIntervalListenerStatus,
    Plan1ScalarState,
    compile_plan1_effect,
    execute_plan1_effects,
    load_plan1_native_settings,
)
from .plan1_native_stage import (
    Plan1EffectTimerInstance,
    Plan1NativeStageState,
    Plan1StageAction,
    Plan1StageStep,
    Plan1StageTurnBoundary,
    Plan1TurnStartPhaseFacts,
    apply_plan1_stage_action,
    advance_plan1_stage_turn,
    preview_plan1_stage_action,
)
from .plan1_runtime_zone_effects import Plan1RuntimeZoneEffects, compile_runtime_drink_zone_kernel
from .plan1_native_step_executor import (
    Plan1NativeStepAction,
    Plan1NativeStepExecutor,
    Plan1NativeStepExecutorError,
    Plan1NativeStepResult,
    Plan1NativeTurnBoundary,
)
from .audition_native_support import evaluate_native_hand_add_support
from .drink_catalog import DrinkCatalogDrink, load_drink_catalog
from .exam_status_runtime import (
    PHASE_STATUS_CHANGE,
    load_exam_status_trigger,
    load_runtime_status_enchant,
)
from .exam_hand_add_support_runtime import (
    project_exam_hand_add_support_runtime,
)
from .plan1_generated_card_adapter import (
    adapt_generated_card_identity_to_plan1_binding,
)
from .plan1_runtime_customization import compile_plan1_card_instance
from .plan1_runtime_scoring import (
    Plan1RuntimeScoringError,
    project_plan1_runtime_scoring,
)
from .plan1_pitem_runtime import (
    Plan1PItemEventDispatch,
    Plan1PItemEventContext,
    dispatch_plan1_pitem_event,
    evaluate_plan1_pitem_event_dispatch,
    parse_plan1_local_save_item_graph,
    restore_plan1_local_save_item_runtime,
    SERIALIZED_PHASE_ENUM,
)
from .plan3_engine import (
    ActivePlan3StatusEnchant,
    Plan3Effect,
    load_plan3_exam_settings,
    load_plan3_status_enchant,
)
from .play_count_interval_dispatcher import (
    OrderedPlayCountIntervalState,
    PlayCountIntervalDispatchError,
    PlayCountIntervalEffectApplication,
    PlayCountIntervalEffectHandler,
    execute_ordered_play_count_intervals,
)
from .nia_status_enchant import PHASE_PLAY_COUNT_INTERVAL
from .nia_plan1_native_sidecar import Plan1NativeDrinkSlot


@dataclass(frozen=True, slots=True)
class Plan1RuntimeBridgeBlocker:
    """One explicit limitation at the captured-runtime boundary."""

    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("blocker code must be non-empty text")
        if not isinstance(self.detail, str):
            raise TypeError("blocker detail must be text")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class Plan1RuntimeDrinkApplication:
    """One Master-ordered drink transaction projected onto Plan 1.

    The drink definition is loaded from the typed Master catalog.  ``before``
    and ``after`` are Plan 1 stage checkpoints; the drink inventory is kept as
    IDs because ``Plan1NativeStageState`` intentionally models only cards and
    scalar exam state.  This object is shadow evidence and never claims that
    the generated post-state is exact.
    """

    drink: DrinkCatalogDrink
    slot_index: int
    inventory_before: tuple[str, ...]
    inventory_after: tuple[str, ...]
    before: Plan1NativeStageState
    after: Plan1NativeStageState
    compiled_effects: tuple[Plan1CompiledEffect, ...]
    execution: Plan1EffectExecution
    kernel_trace: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.drink, DrinkCatalogDrink):
            raise TypeError("drink must be DrinkCatalogDrink")
        if isinstance(self.slot_index, bool) or not isinstance(self.slot_index, int):
            raise TypeError("slot_index must be an integer")
        if self.slot_index < 0:
            raise ValueError("slot_index must be non-negative")
        for name in ("inventory_before", "inventory_after"):
            values = tuple(getattr(self, name))
            if any(not isinstance(value, str) or not value for value in values):
                raise ValueError(f"{name} must contain non-empty drink IDs")
            object.__setattr__(self, name, values)
        if self.slot_index >= len(self.inventory_before):
            raise ValueError("slot_index is outside inventory_before")
        expected_after = (
            self.inventory_before[: self.slot_index]
            + self.inventory_before[self.slot_index + 1 :]
        )
        if self.inventory_after != expected_after:
            raise ValueError("inventory_after must remove exactly one drink slot")
        if not isinstance(self.before, Plan1NativeStageState) or not isinstance(
            self.after, Plan1NativeStageState
        ):
            raise TypeError("drink checkpoints must be Plan1NativeStageState")
        effects = tuple(self.compiled_effects)
        if any(not isinstance(value, Plan1CompiledEffect) for value in effects):
            raise TypeError("compiled_effects must contain Plan1CompiledEffect values")
        object.__setattr__(self, "compiled_effects", effects)
        if not isinstance(self.execution, Plan1EffectExecution):
            raise TypeError("execution must be Plan1EffectExecution")
        if self.execution.before != self.before.scalar:
            raise ValueError("drink execution must start at before scalar")
        if self.execution.after != self.after.scalar:
            raise ValueError("drink execution must end at after scalar")

    @property
    def scalar_before(self) -> Plan1ScalarState:
        return self.before.scalar

    @property
    def scalar_after(self) -> Plan1ScalarState:
        return self.after.scalar

    @property
    def trace(self):
        return self.execution.trace

    @property
    def exact(self) -> bool:
        return False

    @property
    def legal_actions_complete(self) -> bool:
        return False

    @property
    def promotion_allowed(self) -> bool:
        return False

    @property
    def applied(self) -> bool:
        return not self.execution.blockers

    @property
    def state_before(self) -> Plan1NativeStageState:
        return self.before

    @property
    def state_after(self) -> Plan1NativeStageState:
        return self.after


@dataclass(frozen=True, slots=True)
class Plan1RuntimeTurnEnd:
    """One explicit EndTurn/ExamSetting recovery boundary."""

    before: Plan1NativeStageState
    after: Plan1NativeStageState
    before_zones: NativeOrderedZoneState
    after_zones: NativeOrderedZoneState
    recovery_requested: int
    recovery_actual: int
    terminal: bool
    boundary: Plan1StageTurnBoundary | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.before, Plan1NativeStageState) or not isinstance(
            self.after, Plan1NativeStageState
        ):
            raise TypeError("turn-end checkpoints must be Plan1NativeStageState")
        if not isinstance(self.before_zones, NativeOrderedZoneState) or not isinstance(
            self.after_zones, NativeOrderedZoneState
        ):
            raise TypeError("turn-end zones must be NativeOrderedZoneState")
        if self.before.zones != self.before_zones:
            raise ValueError("before_zones must match before checkpoint")
        if self.after.zones != self.after_zones:
            raise ValueError("after_zones must match after checkpoint")
        for name in ("recovery_requested", "recovery_actual"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.recovery_actual > self.recovery_requested:
            raise ValueError("recovery_actual must not exceed recovery_requested")
        if not isinstance(self.terminal, bool):
            raise TypeError("terminal must be bool")
        if self.boundary is not None and not isinstance(
            self.boundary, Plan1StageTurnBoundary
        ):
            raise TypeError("boundary must be Plan1StageTurnBoundary or None")

    @property
    def scalar_before(self) -> Plan1ScalarState:
        return self.before.scalar

    @property
    def scalar_after(self) -> Plan1ScalarState:
        return self.after.scalar

    @property
    def recovered_stamina(self) -> int:
        return self.recovery_actual

    @property
    def exact(self) -> bool:
        return False

    @property
    def legal_actions_complete(self) -> bool:
        return False

    @property
    def promotion_allowed(self) -> bool:
        return False

    @property
    def state_before(self) -> Plan1NativeStageState:
        return self.before

    @property
    def state_after(self) -> Plan1NativeStageState:
        return self.after


@dataclass(frozen=True, slots=True)
class Plan1RuntimeStateProjection:
    """Typed Plan 1 checkpoint projected from native ``state_before``."""

    parsed: LocalSaveExamState | None
    state: Plan1NativeStageState | None
    native_zones: NativeOrderedZoneState | None
    blockers: tuple[Plan1RuntimeBridgeBlocker, ...]
    captured_digest: str
    raw_payload: Mapping[str, Any] | None = None
    notes: tuple[str, ...] = ()
    # Pure branch-local installs. Never inserted into captured raw references
    # or projected back into an actor/native training row.
    local_status_installs: tuple[tuple[Plan1CompiledEffect, int], ...] = ()
    local_item_runtimes: tuple[tuple[str, str, object], ...] = ()

    @property
    def usable(self) -> bool:
        return self.state is not None

    # The bridge is intentionally shadow-only.  A successful scalar reducer
    # call must not be mistaken for a promotion-ready exact transition.
    @property
    def exact(self) -> bool:
        return False

    @property
    def legal_actions_complete(self) -> bool:
        return False

    @property
    def promotion_allowed(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class Plan1RuntimeTransitionProjection:
    """Result of applying one replay action to a projected Plan 1 state."""

    projection: Plan1RuntimeStateProjection
    action: LeaderboardReplayAction
    slot_index: int | None
    card: NativeOrderedCardInstance | None
    program: Plan1CompiledCard | None
    stage_action: Plan1StageAction | None
    transition: Plan1CardTransition | None
    next_state: Plan1NativeStageState | None
    step: Plan1StageStep | None
    blockers: tuple[Plan1RuntimeBridgeBlocker, ...]
    # Non-card actions use the same projection envelope so callers can walk a
    # replay stream without a second dispatcher.  These fields are optional
    # for source compatibility with the original card-only bridge.
    drink: DrinkCatalogDrink | None = None
    drink_application: Plan1RuntimeDrinkApplication | None = None
    turn_end: Plan1RuntimeTurnEnd | None = None
    # The selected use-hand action is executed through the native step
    # adapter.  Keep the typed result on the bridge envelope so callers can
    # inspect the automatic TurnStart continuation without having to infer it
    # from ``next_state``.
    native_step: Plan1NativeStepResult | None = None

    @property
    def applied(self) -> bool:
        card_applied = bool(
            self.step is not None
            and self.stage_action is not None
            and self.stage_action.legal
            and (
                self.native_step is None
                or not self.native_step.blockers
            )
        )
        return bool(
            (
                card_applied
            )
            or self.drink_application is not None
            or self.turn_end is not None
        )

    @property
    def supported(self) -> bool:
        card_supported = bool(
            self.native_step is not None
            and not self.native_step.blockers
            and self.transition is not None
            and self.transition.applied
            and self.stage_action is not None
            and self.stage_action.legal
        )
        legacy_card_supported = bool(
            self.native_step is None
            and self.transition is not None
            and self.transition.applied
            and self.stage_action is not None
            and self.stage_action.legal
            and self.step is not None
        )
        return bool(
            (
                card_supported or legacy_card_supported
            )
            or self.drink_application is not None
            or self.turn_end is not None
        )

    @property
    def legal(self) -> bool:
        card_legal = bool(
            self.stage_action is not None
            and self.stage_action.legal
            and (
                self.native_step is None
                or not self.native_step.blockers
            )
        )
        return bool(
            card_legal
            or self.drink_application is not None
            or self.turn_end is not None
        )

    @property
    def exact(self) -> bool:
        return False

    @property
    def legal_actions_complete(self) -> bool:
        return False

    @property
    def promotion_allowed(self) -> bool:
        return False

    @property
    def final_state(self) -> Plan1NativeStageState | None:
        return self.next_state

    @property
    def turn_boundary(self) -> Plan1RuntimeTurnEnd | None:
        """Alias used by consumers that call the action a boundary."""

        return self.turn_end

    @property
    def drink_result(self) -> Plan1RuntimeDrinkApplication | None:
        return self.drink_application

    @property
    def turn_end_result(self) -> Plan1RuntimeTurnEnd | None:
        return self.turn_end


def _dedupe(
    blockers: tuple[Plan1RuntimeBridgeBlocker, ...]
    | list[Plan1RuntimeBridgeBlocker],
) -> tuple[Plan1RuntimeBridgeBlocker, ...]:
    result: list[Plan1RuntimeBridgeBlocker] = []
    seen: set[tuple[str, str]] = set()
    for blocker in blockers:
        key = (blocker.code, blocker.detail)
        if key in seen:
            continue
        seen.add(key)
        result.append(blocker)
    return tuple(result)


def _as_int(value: object, default: int = 0) -> int:
    return value if type(value) is int else default


def _status_snapshot(
    parsed: LocalSaveExamState,
    *,
    require_graph: bool = False,
) -> tuple[dict[str, int | bool], tuple[Plan1RuntimeBridgeBlocker, ...]]:
    """Read scalar status values from the captured native reference graph.

    ``LocalSaveExamState`` intentionally keeps unknown root fields opaque.
    The small set below is the scalar state already understood by
    ``Plan1ScalarState``.  Unknown status classes are retained as an explicit
    non-exact boundary, rather than guessed from an identifier.
    """

    defaults: dict[str, int | bool] = {
        "parameter_buff_turns": 0,
        "parameter_buff_fresh": False,
        "lesson_buff": 0,
        "parameter_buff_multiple_per_turn_turns": 0,
        "parameter_buff_multiple_per_turn_fresh": False,
        "stamina_consumption_down_turns": 0,
        "stamina_consumption_down_fresh": False,
        "stamina_consumption_add_turns": 0,
        "stamina_consumption_add_fresh": False,
        "stamina_consumption_down_fixed": 0,
        "anti_debuff_count": 0,
        "playable_value_add": 0,
        "buff_consumption_down": False,
        "buff_consumption_add": False,
    }
    blockers: list[Plan1RuntimeBridgeBlocker] = []
    root = (
        {}
        if parsed.root_runtime is None
        else parsed.root_runtime.opaque_fields.to_value()
    )
    if not isinstance(root, Mapping):
        return defaults, (
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-status-graph-unavailable",
                "root_runtime.opaque_fields is not an object",
            ),
        )
    status = root.get("status")
    references = root.get("references")
    if not isinstance(status, Mapping) or not isinstance(references, Mapping):
        if not require_graph:
            return defaults, ()
        return defaults, (
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-status-graph-unavailable",
                "status/references is not an object",
            ),
        )
    ref_rows = references.get("RefIds")
    effect_rows = status.get("_effectList")
    if not isinstance(ref_rows, list) or not isinstance(effect_rows, list):
        if not require_graph:
            return defaults, ()
        return defaults, (
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-status-graph-unavailable",
                "status._effectList/references.RefIds is not an array",
            ),
        )
    by_rid: dict[int, Mapping[str, Any]] = {}
    for row in ref_rows:
        if not isinstance(row, Mapping) or type(row.get("rid")) is not int:
            continue
        if int(row["rid"]) in by_rid:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-status-reference-duplicate",
                    str(row["rid"]),
                )
            )
            continue
        by_rid[int(row["rid"])] = row
    def read_int(data: Mapping[str, Any], keys: tuple[str, ...], label: str) -> int | None:
        for key in keys:
            if key not in data:
                continue
            value = data[key]
            if type(value) is int:
                return value
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-status-value-invalid", f"{label}:{key}"
                )
            )
            return None
        return None

    def read_lifecycle(data: Mapping[str, Any], label: str) -> bool:
        """Validate common native status lifetime fields before projection."""

        turn = data.get("_turn")
        passing = data.get("_isPassingTurnStart")
        limited = data.get("_isTurnLimited")
        uid = data.get("_uid")
        valid = (
            type(turn) is int
            and turn >= -1
            and type(passing) is bool
            and type(limited) is bool
            and limited is (turn != -1)
            and type(uid) is int
            and uid >= 1
        )
        if not valid:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-status-lifecycle-invalid", label
                )
            )
            return False
        return True

    def read_fresh(data: Mapping[str, Any], label: str) -> bool:
        value = data.get("_isPassingTurnStart")
        if type(value) is bool:
            # Native false means the freshly-created layer has not yet
            # crossed its first TurnStart spend boundary.
            return not value
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-status-value-invalid",
                f"{label}:_isPassingTurnStart",
            )
        )
        return False

    def nonnegative(value: int, label: str) -> int | None:
        if value < 0:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-status-value-invalid", label
                )
            )
            return None
        return value

    for effect_ref in effect_rows:
        if (
            not isinstance(effect_ref, Mapping)
            or type(effect_ref.get("rid")) is not int
            or int(effect_ref.get("rid")) < 1
        ):
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-status-reference-invalid",
                    repr(effect_ref),
                )
            )
            continue
        row = by_rid.get(int(effect_ref["rid"]))
        if row is None:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-status-reference-missing",
                    str(effect_ref["rid"]),
                )
            )
            continue
        raw_type = row.get("type")
        raw_data = row.get("data")
        if not isinstance(raw_type, Mapping) or not isinstance(raw_data, Mapping):
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-status-reference-invalid",
                    str(effect_ref["rid"]),
                )
            )
            continue
        kind = raw_type.get("class")
        data = raw_data
        if kind == "ParameterBuffStatusEffect":
            if not read_lifecycle(data, str(kind)):
                continue
            value = read_int(data, ("_turn",), str(kind))
            if value is not None:
                defaults["parameter_buff_turns"] = max(
                    int(defaults["parameter_buff_turns"]), max(value, 0)
                )
                defaults["parameter_buff_fresh"] = read_fresh(
                    data, str(kind)
                )
        elif kind == "LessonBuffStatusEffect":
            if not read_lifecycle(data, str(kind)):
                continue
            value = read_int(data, ("_value",), str(kind))
            value = None if value is None else nonnegative(value, str(kind))
            if value is not None:
                defaults["lesson_buff"] = max(
                    int(defaults["lesson_buff"]), max(value, 0)
                )
        elif kind == "ParameterBuffMultiplePerTurnStatusEffect":
            if not read_lifecycle(data, str(kind)):
                continue
            value = read_int(data, ("_turn",), str(kind))
            if value is not None:
                defaults["parameter_buff_multiple_per_turn_turns"] = max(
                    int(defaults["parameter_buff_multiple_per_turn_turns"]),
                    max(value, 0),
                )
                defaults["parameter_buff_multiple_per_turn_fresh"] = read_fresh(
                    data, str(kind)
                )
        elif kind == "PlayableValueAddStatusEffect":
            if not read_lifecycle(data, str(kind)):
                continue
            value = read_int(data, ("_value",), str(kind))
            value = None if value is None else nonnegative(value, str(kind))
            if value is not None:
                defaults["playable_value_add"] = int(defaults["playable_value_add"]) + value
        elif kind == "StaminaConsumptionDownStatusEffect":
            if not read_lifecycle(data, str(kind)):
                continue
            value = read_int(data, ("_turn",), str(kind))
            if value is not None:
                current = int(defaults["stamina_consumption_down_turns"])
                # Down is an additive status.  A permanent layer dominates
                # any finite duration; finite layers are accumulated rather
                # than collapsed with ``max``.
                if value == -1 or current == -1:
                    defaults["stamina_consumption_down_turns"] = -1
                else:
                    defaults["stamina_consumption_down_turns"] = current + max(value, 0)
                defaults["stamina_consumption_down_fresh"] = read_fresh(
                    data, str(kind)
                )
        elif kind == "StaminaConsumptionAddStatusEffect":
            if not read_lifecycle(data, str(kind)):
                continue
            value = read_int(data, ("_turn",), str(kind))
            if value is not None:
                current = int(defaults["stamina_consumption_add_turns"])
                if value == -1 or current == -1:
                    defaults["stamina_consumption_add_turns"] = -1
                else:
                    defaults["stamina_consumption_add_turns"] = current + max(value, 0)
                defaults["stamina_consumption_add_fresh"] = read_fresh(
                    data, str(kind)
                )
        elif kind == "StaminaConsumptionDownFixStatusEffect":
            if not read_lifecycle(data, str(kind)):
                continue
            value = read_int(data, ("_value", "_count"), str(kind))
            value = None if value is None else nonnegative(value, str(kind))
            if value is not None:
                defaults["stamina_consumption_down_fixed"] = max(
                    int(defaults["stamina_consumption_down_fixed"]), value
                )
        elif kind == "AntiDebuffStatusEffect":
            if not read_lifecycle(data, str(kind)):
                continue
            value = read_int(data, ("_count", "_value"), str(kind))
            value = None if value is None else nonnegative(value, str(kind))
            if value is not None:
                defaults["anti_debuff_count"] = max(
                    int(defaults["anti_debuff_count"]), value
                )
    return defaults, _dedupe(blockers)


def _synthetic_evidence(
    parsed: LocalSaveExamState,
    captured_digest: str,
    *,
    stage_label: str,
) -> AuditionLocalSaveStateEvidence:
    """Attach deterministic provenance solely to the supplied checkpoint.

    This is not a second source of game state.  The values only satisfy the
    ordered-zone module's provenance type when a caller gives a bare parsed
    ``LocalSaveExamState`` instead of a persisted evidence envelope.
    """

    token = captured_digest[:16]
    return AuditionLocalSaveStateEvidence(
        schema_version=AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION,
        run_id=f"plan1-runtime-bridge:{stage_label}:{token}",
        step_context_id=f"plan1-state-before:{token}",
        step_context_digest=captured_digest,
        session_transition_id=f"plan1-transition-before:{token}",
        zone_checkpoint_digest=captured_digest,
        source_path="captured://plan1-runtime/state_before",
        source_sha256=captured_digest,
        source_size=max(
            1,
            len(
                json.dumps(
                    parsed.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ),
        ),
        source_type=EXAM_SAVE_DATA_SOURCE_TYPE,
        envelope_version=0,
        state=parsed,
    )


def _project_native_zones(
    parsed: LocalSaveExamState,
    captured_digest: str,
    *,
    stage_label: str,
) -> NativeOrderedZoneState:
    evidence = _synthetic_evidence(
        parsed, captured_digest, stage_label=stage_label
    )
    instances = tuple(
        NativeOrderedCardInstance.from_local_save(card)
        for zone in (
            parsed.zones.hand,
            parsed.zones.deck,
            parsed.zones.grave,
            parsed.zones.lost,
        )
        for card in zone
    )
    runtime_digest = canonical_runtime_digest_aggregate(instances)
    # The initial native draw leaves ``drawCardGuidList`` as historical
    # evidence even though Hand/Deck are already settled and an ExamAction is
    # actionable.  The shared boundary accepts that history; this Plan1
    # projection still records its narrower, not-yet-positioned mapping as a
    # blocker instead of mutating the captured state to hide it.
    root = parsed.root_runtime
    initial_draw_evidence = bool(root is not None and root.draw_card_guid_list)
    if not initial_draw_evidence and parsed.is_native_actionable_settled:
        return build_native_ordered_zone_state(
            evidence,
            runtime_evidence_digest=runtime_digest,
        )
    if root is None:
        raise NativeOrderedZoneError(
            "unknown-root-runtime-state",
            "Plan1 runtime bridge requires schema-v5 root runtime",
        )
    unsupported: list[str] = []
    if parsed.exam_type == 0:
        from .runtime_lesson_context import lesson_step_rule
        try:
            lesson_step_rule(parsed.step_type_value)
        except (TypeError, ValueError):
            unsupported.append("lesson-step-type")
    elif parsed.exam_type != 1:
        unsupported.append("examType")
    if parsed.phase != 6:
        unsupported.append("phase")
    if parsed.extra_turn != 0 and parsed.exam_type != 0:
        unsupported.append("extraTurn")
    if parsed.is_turn_card_play_end and parsed.exam_type != 0:
        unsupported.append("isTurnCardPlayEnd")
    if parsed.zones.hold:
        unsupported.append("holdList")
    if parsed.playing_card is not None:
        unsupported.append("playingCard")
    if parsed.future_deck or parsed.past_deck:
        unsupported.append("future/pastDeckList")
    if parsed.removed_cards and not parsed.removed_cards_are_positioned_tombstones:
        unsupported.append("removedCardList")
    if not root.command_list_is_empty or root.is_exam_end_complete:
        unsupported.append("pending-command-or-terminal")
    if unsupported:
        raise NativeOrderedZoneError(
            "not-native-actionable-settled",
            f"unsupported native boundary fields: {', '.join(unsupported)}",
        )
    binding = NativeOrderedZoneEvidenceBinding(
        schema_version=NATIVE_ORDERED_ZONE_SCHEMA_VERSION,
        local_save_schema_version=AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION,
        run_id=evidence.run_id,
        step_context_id=evidence.step_context_id,
        step_context_digest=evidence.step_context_digest,
        session_transition_id=evidence.session_transition_id,
        zone_checkpoint_digest=evidence.zone_checkpoint_digest,
        local_save_source_sha256=evidence.source_sha256,
        local_save_evidence_digest=evidence.digest(),
        runtime_evidence_digest=runtime_digest,
    )
    return NativeOrderedZoneState(
        schema_version=NATIVE_ORDERED_ZONE_SCHEMA_VERSION,
        binding=binding,
        random_state=parsed.random_state,
        card_universe=tuple(sorted(instances, key=lambda card: card.guid)),
        hand=tuple(
            NativeOrderedCardInstance.from_local_save(card)
            for card in parsed.zones.hand
        ),
        deck=tuple(
            NativeOrderedCardInstance.from_local_save(card)
            for card in parsed.zones.deck
        ),
        grave=tuple(
            NativeOrderedCardInstance.from_local_save(card)
            for card in parsed.zones.grave
        ),
        lost=tuple(
            NativeOrderedCardInstance.from_local_save(card)
            for card in parsed.zones.lost
        ),
    )


def _native_base_playable_count(parsed: LocalSaveExamState) -> int:
    """Return the generic native Plan 1 per-turn playable allowance.

    Plan 1 starts with one ordinary playable card.  Additional cards are
    represented by active ``PlayableValueAddStatusEffect`` layers and are
    added by the state-before status scan; no card identity is special-cased.
    The argument is retained as a typed checkpoint dependency so future mode
    variants can bind a different generic allowance without changing callers.
    """

    return 1


def _native_next_turn_playable_count(
    parsed: LocalSaveExamState,
    *,
    status: Mapping[str, int | bool] | None = None,
) -> int:
    """Derive next TurnStart's playable count from state-before evidence.

    ``PlayableValueAddStatusEffect`` is represented in ``status`` by the
    active-reference scan.  TurnStart resets the ordinary current-play count;
    therefore the current per-turn allowance is added back rather than
    subtracting ``turn_card_play_count`` a second time.
    """

    if status is None:
        status, _ = _status_snapshot(parsed)
    return _native_base_playable_count(parsed) + max(
        int(status["playable_value_add"]),
        0,
    )


def _scalar_from_local_save(
    parsed: LocalSaveExamState,
    *,
    raw_payload: Mapping[str, Any] | None,
) -> tuple[Plan1ScalarState, tuple[Plan1RuntimeBridgeBlocker, ...]]:
    status, blockers = _status_snapshot(
        parsed,
        require_graph=raw_payload is not None,
    )
    plays_remaining = 0
    if not parsed.is_turn_card_play_end:
        # A settled Main-phase ``state_before`` with an open turn is still
        # actionable even when the persisted turn-card counter has already
        # consumed the ordinary allowance.  The native current-play context
        # (and, where present, active PlayableValueAdd status) is the boundary
        # authority; fail closed to the minimum one visible play rather than
        # inventing a mode-wide two-play budget.
        derived_plays = max(
            _native_base_playable_count(parsed) - parsed.turn_card_play_count,
            0,
        ) + int(status["playable_value_add"])
        plays_remaining = max(1, derived_plays)
    raw_limit_border = (
        raw_payload.get("limitBorder", -1)
        if raw_payload is not None
        else -1
    )
    limit_border = raw_limit_border if type(raw_limit_border) is int else -1
    root = (
        {}
        if parsed.root_runtime is None
        else parsed.root_runtime.opaque_fields.to_value()
    )
    total_effect_draw = _as_int(
        root.get("totalDrawCardCount") if isinstance(root, Mapping) else None
    )
    scalar = Plan1ScalarState(
        score=parsed.score,
        stamina=parsed.stamina,
        max_stamina=parsed.max_stamina,
        block=parsed.block,
        parameter_buff_turns=int(status["parameter_buff_turns"]),
        lesson_buff=int(status["lesson_buff"]),
        turn=parsed.current_turn,
        plays_remaining=plays_remaining,
        play_count=parsed.exam_card_play_count,
        limit_border=limit_border,
        parameter_buff_multiple_per_turn_turns=int(
            status["parameter_buff_multiple_per_turn_turns"]
        ),
        parameter_buff_multiple_per_turn_fresh=bool(
            status["parameter_buff_multiple_per_turn_fresh"]
        ),
        parameter_buff_fresh=bool(status["parameter_buff_fresh"]),
        stamina_consumption_down_turns=int(
            status["stamina_consumption_down_turns"]
        ),
        stamina_consumption_down_fresh=bool(
            status["stamina_consumption_down_fresh"]
        ),
        stamina_consumption_add_turns=int(
            status["stamina_consumption_add_turns"]
        ),
        stamina_consumption_add_fresh=bool(
            status["stamina_consumption_add_fresh"]
        ),
        stamina_consumption_down_fixed=int(
            status["stamina_consumption_down_fixed"]
        ),
        anti_debuff_count=int(status["anti_debuff_count"]),
        total_effect_draw_card_count=total_effect_draw,
        buff_consumption_down=bool(status["buff_consumption_down"]),
        buff_consumption_add=bool(status["buff_consumption_add"]),
    )
    # ``LocalSaveExamState`` is a valid state-before input in its own right.
    # Its root opaque graph retains the same ``planType``/score fields as the
    # raw recorder mapping, so do not silently drop the battle projection when
    # callers use the already-parsed form.
    scoring_plan_type = (
        raw_payload.get("planType")
        if raw_payload is not None
        else root.get("planType")
        if isinstance(root, Mapping)
        else None
    )
    if scoring_plan_type in (2, "ProducePlanType_Plan1"):
        try:
            scoring = project_plan1_runtime_scoring(
                parsed if raw_payload is None else raw_payload
            )
            projected_scalar = scoring.apply_to_scalar(scalar)
            scalar = replace(
                projected_scalar,
                parameter_buff_fresh=scalar.parameter_buff_fresh,
                parameter_buff_multiple_per_turn_fresh=(
                    scalar.parameter_buff_multiple_per_turn_fresh
                ),
                stamina_consumption_down_fresh=(
                    scalar.stamina_consumption_down_fresh
                ),
                stamina_consumption_add_fresh=(
                    scalar.stamina_consumption_add_fresh
                ),
            )
        except (Plan1RuntimeScoringError, KeyError, OSError, TypeError, ValueError) as error:
            blockers = (
                *blockers,
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-scoring-projection-failed",
                    f"{getattr(error, 'code', type(error).__name__)}:{error}",
                ),
            )
    try:
        from .plan2_search_play_card_stamina_consumption_change import (
            SearchPlayCardStaminaRuntime, restore_search_play_card_stamina_status,
        )
        by_rid = {row.get("rid"): row for row in root.get("references", {}).get("RefIds", ()) if isinstance(row, Mapping)}
        restored = []
        for link in root.get("status", {}).get("_effectList", ()):
            row = by_rid.get(link.get("rid"), {}) if isinstance(link, Mapping) else {}
            if row.get("type", {}).get("class") == "SearchPlayCardStaminaConsumptionChangeStatusEffect":
                restored.append(restore_search_play_card_stamina_status(row["data"]))
        if restored:
            scalar = replace(scalar, search_play_card_stamina_runtime=SearchPlayCardStaminaRuntime(
                tuple(restored), scalar.next_status_uid))
    except (ValueError, TypeError, KeyError, OSError) as error:
        blockers = (*blockers, Plan1RuntimeBridgeBlocker("plan1-search-stamina-status-restore-failed", str(error)))
    return scalar, blockers


def project_plan1_runtime_state(
    payload: Mapping[str, Any] | LocalSaveExamState,
    *,
    stage_label: str = "stage",
) -> Plan1RuntimeStateProjection:
    """Project one captured native ``state_before`` without using S'."""

    raw_payload: Mapping[str, Any] | None = None
    if isinstance(payload, LocalSaveExamState):
        parsed = payload
        captured_digest = canonical_sha256(parsed.to_dict())
    elif isinstance(payload, Mapping):
        raw_payload = payload
        captured_digest = canonical_sha256(payload)
        try:
            parsed = parse_local_save_exam_state(payload)
        except (TypeError, ValueError) as error:
            return Plan1RuntimeStateProjection(
                parsed=None,
                state=None,
                native_zones=None,
                blockers=(
                    Plan1RuntimeBridgeBlocker(
                        "plan1-runtime-state-parse-failed",
                        f"{type(error).__name__}:{error}",
                    ),
                ),
                captured_digest=captured_digest,
                raw_payload=raw_payload,
            )
    else:
        raise TypeError("payload must be a mapping or LocalSaveExamState")

    blockers: list[Plan1RuntimeBridgeBlocker] = []
    notes: list[str] = []
    try:
        zones = _project_native_zones(
            parsed, captured_digest, stage_label=stage_label
        )
        if (
            parsed.root_runtime is not None
            and parsed.root_runtime.draw_card_guid_list
        ):
            notes.append("native drawCardGuidList is retained history; current ordered zones remain authoritative")
    except (NativeOrderedZoneError, TypeError, ValueError, IndexError) as error:
        code = getattr(error, "code", "native-zone-projection-failed")
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                f"plan1-runtime-{code}", str(error)
            )
        )
        return Plan1RuntimeStateProjection(
            parsed=parsed,
            state=None,
            native_zones=None,
            blockers=_dedupe(blockers),
            captured_digest=captured_digest,
            raw_payload=raw_payload,
        )

    try:
        scalar, scalar_blockers = _scalar_from_local_save(
            parsed, raw_payload=raw_payload
        )
        blockers.extend(scalar_blockers)
        stage = Plan1NativeStageState(scalar=scalar, zones=zones)
    except (TypeError, ValueError, OverflowError) as error:
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-scalar-projection-failed",
                f"{type(error).__name__}:{error}",
            )
        )
        return Plan1RuntimeStateProjection(
            parsed=parsed,
            state=None,
            native_zones=zones,
            blockers=_dedupe(blockers),
            captured_digest=captured_digest,
            raw_payload=raw_payload,
        )
    return Plan1RuntimeStateProjection(
        parsed=parsed,
        state=stage,
        native_zones=zones,
        blockers=_dedupe(blockers),
        captured_digest=captured_digest,
        raw_payload=raw_payload,
        notes=tuple(notes),
    )


def _opaque_runtime(parsed: LocalSaveExamState | None) -> Mapping[str, Any]:
    if parsed is None or parsed.root_runtime is None:
        return {}
    value = parsed.root_runtime.opaque_fields.to_value()
    return value if isinstance(value, Mapping) else {}


def _active_runtime_references(
    parsed: LocalSaveExamState | None,
) -> tuple[Mapping[str, object], ...]:
    root = _opaque_runtime(parsed)
    status = root.get("status")
    references = root.get("references")
    if not isinstance(status, Mapping) or not isinstance(references, Mapping):
        return ()
    active = status.get("_effectList")
    rows = references.get("RefIds")
    if not isinstance(active, list) or not isinstance(rows, list):
        return ()
    by_rid = {
        row.get("rid"): row
        for row in rows
        if isinstance(row, Mapping) and type(row.get("rid")) is int
    }
    return tuple(
        by_rid[link["rid"]]
        for link in active
        if isinstance(link, Mapping)
        and type(link.get("rid")) is int
        and link["rid"] in by_rid
    )


def _restore_captured_effect_timers(
    projection: Plan1RuntimeStateProjection,
    *,
    database: Path,
    blockers: list[Plan1RuntimeBridgeBlocker],
) -> tuple[Plan1EffectTimerInstance, ...]:
    """Hydrate active origin-type-2 timer listeners in captured order."""

    timers: list[Plan1EffectTimerInstance] = []
    for row in _active_runtime_references(projection.parsed):
        raw_type = row.get("type")
        data = row.get("data")
        if (
            not isinstance(raw_type, Mapping)
            or raw_type.get("class") != "TriggerEffectStatusEffect"
            or not isinstance(data, Mapping)
            or data.get("_originType") != 2
        ):
            continue
        trigger = data.get("_trigger")
        raw_effects = data.get("_effectList")
        source = data.get("_triggerCard")
        if (
            not isinstance(trigger, Mapping)
            or trigger.get("_phaseTypeList") != [SERIALIZED_PHASE_ENUM["ProduceExamPhaseType_ExamTurnTimer"]]
            or not isinstance(trigger.get("_phaseValueList"), list)
            or len(trigger["_phaseValueList"]) != 1
            or not isinstance(raw_effects, list)
            or len(raw_effects) != 1
            or not isinstance(raw_effects[0], Mapping)
            or not isinstance(source, Mapping)
        ):
            continue
        delay = trigger["_phaseValueList"][0]
        child_id = raw_effects[0].get("_id")
        turn_count = data.get("_turnCount")
        source_data = source.get("_cardData")
        if (
            type(delay) is not int
            or delay < 1
            or type(turn_count) is not int
            or not 0 <= turn_count < delay
            or not isinstance(child_id, str)
            or not child_id
            or not isinstance(source_data, Mapping)
        ):
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-effect-timer-shape-invalid",
                    f"rid={row.get('rid')}",
                )
            )
            continue
        with closing(sqlite3.connect(Path(database))) as connection:
            connection.row_factory = sqlite3.Row
            parents = connection.execute(
                "SELECT id FROM effect WHERE effect_type = ? "
                "AND chain_effect_id = ? AND value1 = ? ORDER BY id",
                (EFFECT_TIMER, child_id, delay),
            ).fetchall()
        if len(parents) != 1:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-effect-timer-parent-ambiguous",
                    f"child={child_id};delay={delay};matches={len(parents)}",
                )
            )
            continue
        parent_id = str(parents[0]["id"])
        timer_effect = compile_plan1_effect(parent_id, database=Path(database))
        if timer_effect.blockers:
            blockers.extend(
                Plan1RuntimeBridgeBlocker(value.code, value.detail)
                for value in timer_effect.blockers
            )
            continue
        card_id = source_data.get("_id")
        card_upgrade = source_data.get("_upgradeCount")
        source_guid = source.get("_guid")
        source_play_count = source.get("_playCount")
        if (
            not isinstance(card_id, str)
            or not card_id
            or type(card_upgrade) is not int
            or not isinstance(source_guid, str)
            or not source_guid
            or type(source_play_count) is not int
        ):
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-effect-timer-source-invalid",
                    f"rid={row.get('rid')}",
                )
            )
            continue
        with closing(sqlite3.connect(Path(database))) as connection:
            card_row = connection.execute(
                "SELECT play_effects_json FROM card WHERE id = ? AND upgrade_count = ?",
                (card_id, card_upgrade),
            ).fetchone()
        if card_row is None:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-effect-timer-source-master-missing",
                    f"{card_id}@{card_upgrade}",
                )
            )
            continue
        play_effects = json.loads(str(card_row[0]))
        indexes = tuple(
            index
            for index, value in enumerate(play_effects)
            if isinstance(value, Mapping)
            and value.get("produceExamEffectId") == parent_id
        )
        if len(indexes) != 1:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-effect-timer-source-slot-ambiguous",
                    f"{card_id}@{card_upgrade}:{parent_id}:matches={len(indexes)}",
                )
            )
            continue
        timers.append(
            Plan1EffectTimerInstance(
                timer_effect=timer_effect,
                source_guid=source_guid,
                source_play_count=source_play_count,
                effect_index=indexes[0],
                install_ordinal=len(timers),
                elapsed_turns=turn_count,
            )
        )
    return tuple(timers)


def _drink_inventory(
    parsed: LocalSaveExamState | None,
) -> tuple[Plan1NativeDrinkSlot, ...]:
    """Read the ordered native ``drinkList`` from the supplied checkpoint."""

    raw = _opaque_runtime(parsed).get("drinkList", ())
    if not isinstance(raw, (list, tuple)):
        raise ValueError("ExamSaveData.drinkList must be a list")
    return tuple(
        Plan1NativeDrinkSlot.from_value(value, index)
        for index, value in enumerate(raw)
    )


def _compile_plan1_drink(
    drink_id: str,
    *,
    database: Path,
) -> tuple[
    DrinkCatalogDrink | None,
    tuple[Plan1CompiledEffect, ...],
    tuple[Plan1RuntimeBridgeBlocker, ...],
]:
    """Compile one ordered Master drink into Plan 1's core effect DTOs.

    The shared catalog preserves every Master-ordered effect, including
    Plan1-only buff families that the Plan3 DTO intentionally rejects.  Each
    exam effect is then compiled by the same Plan1 core as a card/P-item
    effect; no drink ID or archetype is special-cased.
    """

    try:
        drink = load_drink_catalog(Path(database)).get_drink(drink_id)
    except (KeyError, OSError, TypeError, ValueError) as error:
        return (
            None,
            (),
            (
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-drink-master-load-failed",
                    f"{drink_id}:{type(error).__name__}:{error}",
                ),
            ),
        )

    compiled: list[Plan1CompiledEffect] = []
    blockers: list[Plan1RuntimeBridgeBlocker] = []
    for reference in drink.effect_refs:
        effect = reference.effect
        if effect.source_kind != "exam":
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-drink-effect-source-unsupported",
                    f"{reference.effect_index}:{effect.gap_family}",
                )
            )
            continue
        try:
            compiled_effect = compile_plan1_effect(
                effect.source_id,
                database=Path(database),
            )
            compiled_effect = compile_runtime_drink_zone_kernel(compiled_effect, Path(database))
        except (KeyError, OSError, TypeError, ValueError) as error:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-drink-effect-compile-failed",
                    f"{effect.source_id}:{type(error).__name__}:{error}",
                )
            )
            continue
        promoted = _promote_transition_local_effect_installers(
            (compiled_effect,)
        )[0]
        if promoted.blockers:
            blockers.extend(
                Plan1RuntimeBridgeBlocker(value.code, value.detail)
                for value in promoted.blockers
            )
            continue
        compiled.append(promoted)
    return drink, tuple(compiled), _dedupe(blockers)


def _turn_end_dispositions(
    state: Plan1NativeStageState,
    *,
    database: Path,
) -> tuple[
    dict[str, NativeEndTurnDisposition] | None,
    tuple[Plan1RuntimeBridgeBlocker, ...],
]:
    """Resolve each remaining Hand card's static Master ``isEndTurnLost``."""

    dispositions: dict[str, NativeEndTurnDisposition] = {}
    blockers: list[Plan1RuntimeBridgeBlocker] = []
    for card in state.zones.hand:
        try:
            master_card = load_master_card(
                card.card_id,
                card.effective_upgrade,
                database=Path(database),
            )
            dispositions[card.guid] = (
                NativeEndTurnDisposition.LOST
                if master_card.is_end_turn_lost
                else NativeEndTurnDisposition.GRAVE
            )
        except (KeyError, OSError, TypeError, ValueError) as error:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-turn-end-master-card-failed",
                    f"{card.guid}:{card.card_id}+{card.effective_upgrade}:"
                    f"{type(error).__name__}:{error}",
                )
            )
    return (None if blockers else dispositions), _dedupe(blockers)


def _exam_setting_turn_values(
    setting_id: str,
) -> tuple[int, int]:
    """Return ``turnStartDistribute`` and EndTurn recovery from Master."""

    settings = load_plan3_exam_settings(setting_id)
    return settings.turn_start_distribute, settings.turn_end_stamina_recovery


_GIMMICK_FIELD_UNKNOWN = "ProduceExamFieldStatusType_Unknown"
_GIMMICK_FIELD_PARAMETER_BUFF = "ProduceExamFieldStatusType_ParameterBuff"
_GIMMICK_FIELD_PARAMETER_BUFF_UP = "ProduceExamFieldStatusType_ParameterBuffUp"
_GIMMICK_FIELD_LESSON_BUFF = "ProduceExamFieldStatusType_LessonBuffUp"
_GIMMICK_CHECK_NORMAL = "ProduceExamTriggerCheckType_Unknown"
_GIMMICK_CHECK_NOT = "ProduceExamTriggerCheckType_Not"


def _plan1_gimmick_condition_fires(
    step: NiaGimmickStep,
    scalar: Plan1ScalarState,
) -> tuple[bool | None, str]:
    """Evaluate the Plan1 scalar subset of a native StartTurn condition.

    Native checks the field after EndTurn status spending.  ``None`` means the
    Master row needs a field/search grammar outside this bounded Plan1 family;
    callers must block instead of treating it as unconditional.
    """

    if step.remaining_turn_permille != 0 or step.remaining_turn != 0:
        return None, "remaining-turn-condition-unsupported"
    if step.field_status_card_search_id:
        return None, "field-card-search-unsupported"
    if (
        step.field_status_type == _GIMMICK_FIELD_UNKNOWN
        and step.field_status_check_type == _GIMMICK_CHECK_NORMAL
        and step.field_status_value == 0
    ):
        return True, "unconditional"
    if step.field_status_type == _GIMMICK_FIELD_PARAMETER_BUFF:
        if step.field_status_value != 0:
            return None, "parameter-buff-presence-value-unsupported"
        if step.field_status_check_type == _GIMMICK_CHECK_NORMAL:
            return scalar.parameter_buff_turns > 0, (
                f"field-status-present:{scalar.parameter_buff_turns}>0"
            )
        if step.field_status_check_type == _GIMMICK_CHECK_NOT:
            return scalar.parameter_buff_turns <= 0, (
                f"field-status-absent:{scalar.parameter_buff_turns}<=0"
            )
        return None, f"field-check-unsupported:{step.field_status_check_type}"
    current = {
        _GIMMICK_FIELD_PARAMETER_BUFF_UP: scalar.parameter_buff_turns,
        _GIMMICK_FIELD_LESSON_BUFF: scalar.lesson_buff,
    }.get(step.field_status_type)
    if current is None:
        return None, f"field-status-unsupported:{step.field_status_type}"
    if step.field_status_check_type == _GIMMICK_CHECK_NORMAL:
        return current >= step.field_status_value, (
            f"field-status:{current}>={step.field_status_value}"
        )
    if step.field_status_check_type == _GIMMICK_CHECK_NOT:
        return current < step.field_status_value, (
            f"field-status:{current}<{step.field_status_value}"
        )
    return None, f"field-check-unsupported:{step.field_status_check_type}"


def _compile_runtime_gimmick_effect(
    effect_id: str,
    *,
    database: Path,
    blockers: list[Plan1RuntimeBridgeBlocker],
) -> Plan1CompiledEffect | None:
    """Compile one scheduled effect through its existing Plan1 route.

    StatusEnchant is an installer rather than a scalar mutation.  The
    transition-local bridge restores its listener from the following native
    state, so installation itself remains a scalar-neutral operation; before
    admitting it, validate the complete Master root/trigger/child graph that
    the later status dispatcher will consume.
    """

    try:
        effect = compile_plan1_effect(effect_id, database=Path(database))
    except (KeyError, OSError, TypeError, ValueError) as error:
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-gimmick-effect-compile-failed",
                f"{effect_id}:{type(error).__name__}:{error}",
            )
        )
        return None
    if effect.effect_type != EFFECT_STATUS_ENCHANT:
        if effect.blockers:
            blockers.extend(
                Plan1RuntimeBridgeBlocker(value.code, value.detail)
                for value in effect.blockers
            )
            return None
        return effect

    promoted = _promote_transition_local_effect_installers((effect,))[0]
    if promoted.blockers or effect.effect_turn != -1:
        blockers.extend(
            Plan1RuntimeBridgeBlocker(value.code, value.detail)
            for value in promoted.blockers
        )
        if not promoted.blockers:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-gimmick-status-lifecycle-unsupported",
                    effect_id,
                )
            )
        return None
    try:
        enchant = load_runtime_status_enchant(
            promoted.status_enchant_id,
            Path(database),
        )
    except (KeyError, OSError, TypeError, ValueError) as error:
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-gimmick-status-load-failed",
                f"{effect_id}:{type(error).__name__}:{error}",
            )
        )
        return None
    trigger = enchant.trigger
    common_neutral = bool(
        not trigger.field_status_check_types
        and not trigger.field_status_card_search_ids
        and trigger.upper_search_count == 0
        and trigger.lower_search_count == 0
        and trigger.card_move_position_type
        == "ProduceCardMovePositionType_Unknown"
        and trigger.lesson_type == "ProduceStepLessonType_Unknown"
    )
    status_change_shape = bool(
        common_neutral
        and trigger.phase_types == (PHASE_STATUS_CHANGE,)
        and not trigger.phase_values
        and not trigger.produce_card_search_id
        and len(trigger.effect_types) == 1
        and trigger.effect_types[0] in {EFFECT_PARAMETER_BUFF, EFFECT_LESSON_BUFF}
        and len(trigger.field_status_types) == len(trigger.field_status_values)
        and len(trigger.field_status_types) <= 1
        and all(
            value in {
                _GIMMICK_FIELD_PARAMETER_BUFF_UP,
                _GIMMICK_FIELD_LESSON_BUFF,
            }
            for value in trigger.field_status_types
        )
    )
    interval_shape = bool(
        common_neutral
        and trigger.phase_types == (PHASE_PLAY_COUNT_INTERVAL,)
        and len(trigger.phase_values) == 1
        and type(trigger.phase_values[0]) is int
        and trigger.phase_values[0] > 0
        and not trigger.field_status_types
        and not trigger.field_status_values
        and not trigger.effect_types
        and (
            not trigger.produce_card_search_id
            or trigger.card_search_rule is not None
        )
    )
    if not (status_change_shape or interval_shape):
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-gimmick-status-trigger-unsupported",
                f"{effect_id}:{trigger.id}",
            )
        )
        return None
    for child in enchant.effects:
        compiled_child = compile_plan1_effect(child.id, database=Path(database))
        if (
            compiled_child.blockers
            or compiled_child.effect_type
            not in {EFFECT_PARAMETER_BUFF, EFFECT_LESSON_BUFF}
        ):
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-gimmick-status-child-unsupported",
                    f"{effect_id}:{child.id}",
                )
            )
            return None
    return promoted


def _apply_runtime_turn_start_gimmicks(
    projection: Plan1RuntimeStateProjection,
    state: Plan1NativeStageState,
    *,
    settings: Plan1NativeSettings,
    database: Path,
    blockers: list[Plan1RuntimeBridgeBlocker],
) -> Plan1NativeStageState | None:
    """Apply Master effects scheduled for the next captured turn.

    The ordered ``gimmickList`` is part of ``state_before`` and identifies
    both its target turn and exact effect ID.  It is therefore a stronger
    boundary than inferring a N.I.A. gimmick from card identity or score.
    """

    parsed = projection.parsed
    if parsed is None:
        return state
    raw = _opaque_runtime(parsed).get("gimmickList", ())
    if not isinstance(raw, list):
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-gimmick-list-invalid",
                type(raw).__name__,
            )
        )
        return None
    target_turn = parsed.current_turn + 1
    effects: list[Plan1CompiledEffect] = []
    for index, row in enumerate(raw):
        if not isinstance(row, Mapping):
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-gimmick-row-invalid", str(index)
                )
            )
            return None
        turn = row.get("turn")
        group_id = row.get("gimmickGroupId")
        effect_id = row.get("gimmickEffectId")
        if (
            type(turn) is not int
            or not isinstance(group_id, str)
            or not group_id
            or not isinstance(effect_id, str)
            or not effect_id
        ):
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-gimmick-row-invalid", str(index)
                )
            )
            return None
        if turn != target_turn:
            continue
        try:
            matching_steps = tuple(
                step
                for step in load_nia_gimmick_steps(group_id)
                if step.start_turn == turn and step.effect_id == effect_id
            )
        except (KeyError, OSError, TypeError, ValueError) as error:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-gimmick-master-load-failed",
                    f"{group_id}:{type(error).__name__}:{error}",
                )
            )
            return None
        if len(matching_steps) != 1:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-gimmick-schedule-binding-failed",
                    f"{group_id}:turn={turn}:effect={effect_id}:"
                    f"matches={len(matching_steps)}",
                )
            )
            return None
        fires, condition_detail = _plan1_gimmick_condition_fires(
            matching_steps[0],
            state.scalar,
        )
        if fires is None:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-gimmick-condition-unsupported",
                    f"{group_id}:turn={turn}:{condition_detail}",
                )
            )
            return None
        if not fires:
            continue
        effect = _compile_runtime_gimmick_effect(
            effect_id,
            database=Path(database),
            blockers=blockers,
        )
        if effect is None:
            return None
        effects.append(effect)
    if not effects:
        return state
    execution = execute_plan1_effects(
        state.scalar,
        tuple(effects),
        settings=settings,
    )
    if execution.blockers:
        blockers.extend(
            Plan1RuntimeBridgeBlocker(value.code, value.detail)
            for value in execution.blockers
        )
        return None
    after_scalar = execution.after
    if (
        any(
            entry.effect_type == EFFECT_PARAMETER_BUFF
            and entry.after.parameter_buff_turns
            > entry.before.parameter_buff_turns
            for entry in execution.trace
        )
        and state.scalar.parameter_buff_turns == 0
    ):
        after_scalar = replace(after_scalar, parameter_buff_fresh=True)
    return replace(state, scalar=after_scalar)


def _apply_runtime_end_turn_listeners(
    projection: Plan1RuntimeStateProjection,
    trigger_state: Plan1NativeStageState,
    result_state: Plan1NativeStageState,
    *,
    settings: Plan1NativeSettings,
    database: Path,
    blockers: list[Plan1RuntimeBridgeBlocker],
) -> Plan1NativeStageState | None:
    """Apply active, captured EndTurn listener children in native order."""

    effects = _active_status_listener_effects(
        projection,
        phase="ProduceExamPhaseType_ExamEndTurn",
        scalar=trigger_state.scalar,
        database=Path(database),
        blockers=blockers,
    )
    if not effects:
        return result_state
    execution = execute_plan1_effects(
        result_state.scalar,
        effects,
        settings=settings,
    )
    if execution.blockers:
        blockers.extend(
            Plan1RuntimeBridgeBlocker(value.code, value.detail)
            for value in execution.blockers
        )
        return None
    return replace(result_state, scalar=execution.after)


def _local_status_listener_effects(projection, phase, scalar, database, blockers):
    effects = []
    for installer, installed_turn in projection.local_status_installs:
        # A fresh install is exempt at its first end boundary. The bounded
        # branch may cross that boundary; no permanent duration is invented.
        if installer.effect_turn > 0 and scalar.turn > installed_turn + installer.effect_turn:
            continue
        try:
            enchant = load_runtime_status_enchant(installer.status_enchant_id, Path(database))
            trigger = enchant.trigger
            if trigger.phase_types != (phase,):
                continue
            if (phase not in {"ProduceExamPhaseType_ExamEndTurn", "ProduceExamPhaseType_StartPlay"}
                    or trigger.phase_values or trigger.field_status_check_types or trigger.field_status_types
                    or trigger.field_status_values or trigger.field_status_card_search_ids or trigger.effect_types
                    or trigger.produce_card_search_id or trigger.upper_search_count or trigger.lower_search_count
                    or trigger.card_move_position_type != "ProduceCardMovePositionType_Unknown"
                    or trigger.lesson_type != "ProduceStepLessonType_Unknown"):
                raise ValueError("local installed trigger has no complete phase contract")
            for child in enchant.effects:
                compiled = compile_plan1_effect(child.id, database=Path(database))
                if compiled.blockers:
                    raise ValueError("local installed child has no core implementation")
                effects.append(compiled)
        except (ValueError, TypeError, KeyError, OSError) as error:
            blockers.append(Plan1RuntimeBridgeBlocker("plan1-local-installed-listener-unavailable", str(error)))
    return tuple(effects)


def _apply_runtime_start_play_listeners(projection, state, *, settings, database, blockers):
    effects = _active_status_listener_effects(projection, phase="ProduceExamPhaseType_StartPlay",
        scalar=state.scalar, database=Path(database), blockers=blockers)
    if blockers:
        return None
    if not effects:
        return state
    resolver, observed_used = _runtime_hand_add_support_resolver(projection, state, database=Path(database), blockers=blockers)
    if blockers:
        return None
    used = tuple(dict.fromkeys((*state.hand_add_used_support_ids, *observed_used)))
    executor = Plan1RuntimeZoneEffects(state, settings=settings, hand_add_support_resolver=resolver, used_support_ids=used)
    execution = executor.apply(state.scalar, effects)
    if execution.blockers:
        blockers.extend(Plan1RuntimeBridgeBlocker(value.code, value.detail) for value in execution.blockers)
        return None
    return executor.commit(state, execution.after)


def _apply_plan1_runtime_drink(
    projection: Plan1RuntimeStateProjection,
    action: LeaderboardReplayAction,
    *,
    database: Path,
    settings: Plan1NativeSettings | None,
    blockers: list[Plan1RuntimeBridgeBlocker],
    selected_card_guids: tuple[str, ...] | None = None,
) -> tuple[
    int | None,
    DrinkCatalogDrink | None,
    Plan1RuntimeDrinkApplication | None,
    Plan1NativeStageState | None,
]:
    """Apply a drink using only state-before inventory and Master effects."""

    state = projection.state
    parsed = projection.parsed
    slot_index: int | None = None
    drink: DrinkCatalogDrink | None = None
    application: Plan1RuntimeDrinkApplication | None = None
    if len(action.indexes) != 1:
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-action-index-shape-unsupported",
                f"indexes={list(action.indexes)!r}",
            )
        )
        return slot_index, drink, application, None
    slot_index = action.indexes[0]
    if state is None:
        blockers.append(Plan1RuntimeBridgeBlocker("plan1-runtime-stage-state-unavailable"))
        return slot_index, drink, application, None
    try:
        inventory = _drink_inventory(parsed)
    except (TypeError, ValueError) as error:
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-drink-inventory-invalid",
                f"{type(error).__name__}:{error}",
            )
        )
        return slot_index, drink, application, None
    if slot_index < 0 or slot_index >= len(inventory):
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-drink-slot-out-of-range",
                f"slot={slot_index};inventory={len(inventory)}",
            )
        )
        return slot_index, drink, application, None
    selected = inventory[slot_index]
    drink, compiled, compile_blockers = _compile_plan1_drink(
        selected.drink_id,
        database=Path(database),
    )
    blockers.extend(compile_blockers)
    if drink is None or compile_blockers:
        return slot_index, drink, application, None
    effective_settings = (
        load_plan1_native_settings(
            parsed.setting_id if parsed is not None else "p_exam_setting-1"
        )
        if settings is None
        else settings
    )
    # Android UseDrink enqueues one PlayEffectCommand per Master effect.
    # AffectEffect commits that effect before CheckInsertEffectResultTrigger-
    # Command inserts its StatusChange children ahead of the next drink
    # effect. Ordinary enchant children carry a nonzero PlayingEnchantUid
    # with trigger-active=false; they do not recursively emit another layer.
    # Static evidence: apk_drink_status_change_order.json (0x7f0a938,
    # 0x7f0a948, 0x7ed0b7c, 0x7e551a0). Keep finite SpendCount/ref persistence
    # explicit until the typed drink prefix owns those counters.
    ordered_effects: list[Plan1CompiledEffect] = []
    ordered_trace = []
    working = state.scalar
    working_used_support_ids = state.hand_add_used_support_ids
    hand_add_support_resolver = None
    if any(effect.effect_type in {"ProduceExamEffectType_ExamCardDraw", "ProduceExamEffectType_ExamHandGraveCountCardDraw", "ProduceExamEffectType_ExamCardMove", "ProduceExamEffectType_ExamCardCreateSearch"}
           for effect in compiled):
        hand_add_support_resolver, observed_used = _runtime_hand_add_support_resolver(
            projection, state, database=Path(database), blockers=blockers)
        if blockers:
            return slot_index, drink, application, None
        working_used_support_ids = tuple(dict.fromkeys((*working_used_support_ids, *observed_used)))

    zone_executor = Plan1RuntimeZoneEffects(state, settings=effective_settings,
        hand_add_support_resolver=hand_add_support_resolver, used_support_ids=working_used_support_ids,
        database=Path(database), selected_card_guids=selected_card_guids,
        plan_ignore_card_ids=(tuple(_opaque_runtime(parsed)["planIgnoreProduceCardWhiteList"])
            if isinstance(_opaque_runtime(parsed).get("planIgnoreProduceCardWhiteList"), list) else None),
        simulation_guid_prefix=f"simulated-drink:{projection.captured_digest}:{slot_index}")

    for effect in compiled:
        direct = zone_executor.apply(working, (effect,))
        if direct.blockers:
            blockers.extend(Plan1RuntimeBridgeBlocker(value.code, value.detail) for value in direct.blockers)
            return slot_index, drink, application, None
        offset = len(ordered_effects)
        ordered_effects.append(effect)
        ordered_trace.extend(replace(entry, effect_index=None if entry.effect_index is None else offset + entry.effect_index)
                             for entry in direct.trace)
        # These are the two native SetStatusDifference paths verified for
        # common Plan1 drinks. Derive events from state changes rather than
        # labeling a source effect as the status it might have changed.
        changed_types = tuple((effect_type, getattr(direct.after, field) - getattr(working, field)) for field, effect_type in (
            ("lesson_buff", EFFECT_LESSON_BUFF),
            ("parameter_buff_turns", EFFECT_PARAMETER_BUFF),
        ) if getattr(direct.after, field) > getattr(working, field))
        working = direct.after
        for changed_type, changed_amount in changed_types:
            children = _active_status_listener_effects(
                projection,
                phase=PHASE_STATUS_CHANGE,
                scalar=working,
                changed_effect_type=changed_type,
                changed_amount=changed_amount,
                database=Path(database),
                blockers=blockers,
                require_unlimited_persistent=True,
            )
            if blockers:
                return slot_index, drink, application, None
            if children:
                child_execution = zone_executor.apply(working, children)
                if child_execution.blockers:
                    blockers.extend(Plan1RuntimeBridgeBlocker(value.code, value.detail) for value in child_execution.blockers)
                    return slot_index, drink, application, None
                offset = len(ordered_effects)
                ordered_effects.extend(children)
                ordered_trace.extend(replace(entry, effect_index=None if entry.effect_index is None else offset + entry.effect_index)
                                     for entry in child_execution.trace)
                working = child_execution.after
    compiled = tuple(ordered_effects)
    execution = Plan1EffectExecution(state.scalar, working,
        tuple(replace(entry, ordinal=index) for index, entry in enumerate(ordered_trace)))
    next_state = zone_executor.commit(state, execution.after)
    before_ids = tuple(value.drink_id for value in inventory)
    after_ids = before_ids[:slot_index] + before_ids[slot_index + 1 :]
    try:
        application = Plan1RuntimeDrinkApplication(
            drink=drink,
            slot_index=slot_index,
            inventory_before=before_ids,
            inventory_after=after_ids,
            before=state,
            after=next_state,
            compiled_effects=compiled,
            execution=execution,
            kernel_trace=tuple(zone_executor.kernel_trace),
        )
    except (TypeError, ValueError) as error:
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-drink-application-invalid",
                f"{type(error).__name__}:{error}",
            )
        )
        return slot_index, drink, None, None
    return slot_index, drink, application, next_state


def _apply_plan1_runtime_turn_end(
    projection: Plan1RuntimeStateProjection,
    action: LeaderboardReplayAction,
    *,
    database: Path,
    settings: Plan1NativeSettings | None,
    blockers: list[Plan1RuntimeBridgeBlocker],
) -> tuple[Plan1RuntimeTurnEnd | None, Plan1NativeStageState | None]:
    """Apply only an explicit replay EndTurn action.

    Non-terminal boundaries use the existing stage ``close_turn`` + draw path.
    The final boundary closes Hand without drawing the next turn, then applies
    ``remaining_turns * ExamSetting.examTurnEndRecoveryStamina`` exactly once.
    """

    state = projection.state
    parsed = projection.parsed
    if action.indexes:
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-turn-end-indexes-unsupported",
                f"indexes={list(action.indexes)!r}",
            )
        )
        return None, None
    if state is None or parsed is None:
        blockers.append(Plan1RuntimeBridgeBlocker("plan1-runtime-stage-state-unavailable"))
        return None, None
    dispositions, disposition_blockers = _turn_end_dispositions(
        state,
        database=Path(database),
    )
    blockers.extend(disposition_blockers)
    if dispositions is None:
        return None, None
    hand_add_support_resolver, used_support_ids = (
        _runtime_hand_add_support_resolver(
            projection,
            state,
            database=Path(database),
            blockers=blockers,
        )
    )
    if used_support_ids != state.hand_add_used_support_ids:
        state = replace(
            state,
            hand_add_used_support_ids=used_support_ids,
        )
    try:
        draw_count, recovery_per_turn = _exam_setting_turn_values(parsed.setting_id)
    except (KeyError, OSError, TypeError, ValueError) as error:
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-turn-end-setting-failed",
                f"{parsed.setting_id}:{type(error).__name__}:{error}",
            )
        )
        return None, None
    try:
        effective_settings = (
            load_plan1_native_settings(parsed.setting_id)
            if settings is None
            else settings
        )
        remain_turn = parsed.remain_turn
        if remain_turn < 1:
            raise ValueError(f"remainTurn must be positive before EndTurn: {remain_turn}")
        if remain_turn > 1:
            # Explicit EndTurn follows the same state-before status boundary
            # as the automatic last-play continuation.  In particular,
            # PlayableValueAdd layers are already live at this boundary and
            # must contribute to the next turn's allowance.
            status, status_blockers = _status_snapshot(parsed)
            blockers.extend(status_blockers)
            plays_remaining = _native_next_turn_playable_count(
                parsed,
                status=status,
            )
            requested = recovery_per_turn
            actual = min(
                requested,
                max(0, state.scalar.max_stamina - state.scalar.stamina),
            )
            recovered = replace(
                state,
                scalar=replace(
                    state.scalar,
                    stamina=state.scalar.stamina + actual,
                ),
            )
            boundary = Plan1StageTurnBoundary.from_mapping(
                draw_count=draw_count,
                plays_remaining=plays_remaining,
                dispositions=dispositions,
            )
            advanced = advance_plan1_stage_turn(
                recovered,
                boundary,
                settings=effective_settings,
                hand_add_support_resolver=hand_add_support_resolver,
            )
            if advanced.blockers:
                blockers.extend(
                    Plan1RuntimeBridgeBlocker(value.code, value.detail)
                    for value in advanced.blockers[len(recovered.blockers) :]
                )
                return None, None
            advanced_with_end_turn = _apply_runtime_end_turn_listeners(
                projection,
                recovered,
                advanced,
                settings=effective_settings,
                database=Path(database),
                blockers=blockers,
            )
            if advanced_with_end_turn is None:
                return None, None
            advanced = advanced_with_end_turn
            advanced_with_gimmick = _apply_runtime_turn_start_gimmicks(
                projection,
                advanced,
                settings=effective_settings,
                database=Path(database),
                blockers=blockers,
            )
            if advanced_with_gimmick is None:
                return None, None
            advanced = advanced_with_gimmick
            advanced = _apply_runtime_start_play_listeners(projection, advanced,
                settings=effective_settings, database=Path(database), blockers=blockers)
            if advanced is None:
                return None, None
            after = replace(
                advanced,
                # ``advanced`` already carries the recovery that happened
                # before the EndTurn/TurnStart lifecycle.
                scalar=advanced.scalar,
            )
            boundary_value: Plan1StageTurnBoundary | None = boundary
        else:
            closed = state.zones.close_turn(dispositions)
            requested = recovery_per_turn * remain_turn
            actual = min(
                requested,
                max(0, state.scalar.max_stamina - state.scalar.stamina),
            )
            after = replace(
                state,
                scalar=replace(
                    state.scalar,
                    stamina=state.scalar.stamina + actual,
                    plays_remaining=0,
                ),
                zones=closed,
            )
            boundary_value = None
            terminal = True
            return (
                Plan1RuntimeTurnEnd(
                    before=state,
                    after=after,
                    before_zones=state.zones,
                    after_zones=closed,
                    recovery_requested=requested,
                    recovery_actual=actual,
                    terminal=terminal,
                    boundary=boundary_value,
                ),
                after,
            )
    except (NativeOrderedZoneError, TypeError, ValueError, OverflowError) as error:
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-turn-end-apply-failed",
                f"{type(error).__name__}:{error}",
            )
        )
        return None, None
    turn_end = Plan1RuntimeTurnEnd(
        before=state,
        after=after,
        before_zones=state.zones,
        after_zones=after.zones,
        recovery_requested=requested,
        recovery_actual=actual,
        terminal=False,
        boundary=boundary_value,
    )
    return turn_end, after


def _runtime_boundary_for_step(
    projection: Plan1RuntimeStateProjection,
    *,
    settings: Plan1NativeSettings,
    database: Path,
    blockers: list[Plan1RuntimeBridgeBlocker],
) -> Plan1NativeTurnBoundary | None:
    """Build native step boundary facts from state-before and generic Master.

    The step executor needs the next TurnStart draw and playable count only
    when the selected card consumes the last current play.  Draw/recovery
    values come from the selected ``ExamSetting`` row; playable-value layers
    come from the active status graph in the captured ``state_before``.  No
    post-state is consulted, and an unavailable Master row remains a typed
    bridge blocker.
    """

    parsed = projection.parsed
    setting_id = (
        parsed.setting_id
        if parsed is not None
        else settings.exam_setting_id
    )
    try:
        draw_count, _recovery = _exam_setting_turn_values(setting_id)
        status = None
        if parsed is not None:
            status, status_blockers = _status_snapshot(parsed)
            blockers.extend(status_blockers)
        plays_remaining = (
            _native_next_turn_playable_count(parsed, status=status)
            if parsed is not None
            else 2
        )
        remain_turn = None if parsed is None else parsed.remain_turn
        start_effects = _runtime_pitem_start_turn_effects(
            projection,
            database=Path(database),
            blockers=blockers,
        )
        return Plan1NativeTurnBoundary(
            draw_count=draw_count,
            plays_remaining=plays_remaining,
            # The executor resolves the remaining Hand cards' static
            # isEndTurnLost classification from generic Master after the
            # selected card has settled.  Supplying ``None`` here is
            # intentional: it avoids classifying the selected source card
            # before its native move.
            dispositions=None,
            phase_facts=Plan1TurnStartPhaseFacts(
                exam_start_turn_effect_ids=tuple(
                    effect.effect_id for effect in start_effects
                ),
            ),
            exam_start_turn_effects=start_effects,
            remain_turn=remain_turn,
        )
    except (KeyError, OSError, TypeError, ValueError, OverflowError) as error:
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-step-turn-boundary-failed",
                f"{setting_id}:{type(error).__name__}:{error}",
            )
        )
        return None


def _runtime_parameter_buff_listener(
    projection: Plan1RuntimeStateProjection,
    *,
    scalar: Plan1ScalarState,
    database: Path,
    blockers: list[Plan1RuntimeBridgeBlocker],
) -> tuple[Plan1CompiledEffect, ...]:
    """Route every captured non-item ParameterBuff listener through Master."""

    return _active_status_listener_effects(
        projection,
        phase=PHASE_STATUS_CHANGE,
        scalar=scalar,
        changed_effect_type=EFFECT_PARAMETER_BUFF,
        database=Path(database),
        blockers=blockers,
    )


def _promote_transition_local_effect_installers(
    source_effects: Sequence[Plan1CompiledEffect],
) -> tuple[Plan1CompiledEffect, ...]:
    """Admit scalar-neutral installers for captured transition-local replay."""

    effects: list[Plan1CompiledEffect] = []
    for effect in source_effects:
        expected = {
            ("effect-type-unsupported", effect.effect_id, EFFECT_STATUS_ENCHANT)
        }
        observed = {
            (value.code, value.source_id, value.detail)
            for value in effect.blockers
        }
        shape_ok = (
            effect.effect_type == EFFECT_STATUS_ENCHANT
            and bool(effect.status_enchant_id)
            and effect.value1 == 0
            and effect.value2 == 0
            and effect.effect_count == 0
            and (effect.effect_turn == -1 or effect.effect_turn > 0)
            and observed == expected
        )
        effects.append(replace(effect, blockers=()) if shape_ok else effect)
    return tuple(effects)


def _promote_transition_local_status_installers(
    program: Plan1CompiledCard,
) -> Plan1CompiledCard:
    """Admit scalar-neutral status installers only for local replay steps.

    The global Plan1 catalog deliberately keeps these cards blocked because
    chain/live search does not yet own listener persistence.  Transition-local
    comparison reprojects the installed listener from the next captured
    state_before, so the installer itself can be treated as a proven
    scalar/zone-neutral effect without widening the live catalog.
    """

    promoted_blockers = set()
    effects = _promote_transition_local_effect_installers(program.effects)
    for before, after in zip(program.effects, effects, strict=True):
        if not after.blockers:
            promoted_blockers.update(before.blockers)
    if not promoted_blockers:
        return program
    return replace(
        program,
        effects=tuple(effects),
        blockers=tuple(
            value for value in program.blockers
            if value not in promoted_blockers
        ),
    )


def _active_status_listener_effects(
    projection: Plan1RuntimeStateProjection,
    *,
    phase: str,
    scalar: Plan1ScalarState,
    changed_effect_type: str | None = None,
    changed_amount: int | None = None,
    playing_card: NativeOrderedCardInstance | None = None,
    playing_program: Plan1CompiledCard | None = None,
    database: Path,
    blockers: list[Plan1RuntimeBridgeBlocker],
    require_unlimited_persistent: bool = False,
) -> tuple[Plan1CompiledEffect, ...]:
    """Compile proven non-item listener children for one native phase."""

    parsed = projection.parsed
    if parsed is None:
        return ()
    root = _opaque_runtime(parsed)
    status = root.get("status")
    references = root.get("references")
    if not isinstance(status, Mapping) or not isinstance(references, Mapping):
        return ()
    links = status.get("_effectList")
    rows = references.get("RefIds")
    if not isinstance(links, list) or not isinstance(rows, list):
        return ()
    by_rid = {
        row["rid"]: row
        for row in rows
        if isinstance(row, Mapping) and type(row.get("rid")) is int
    }
    compiled: list[Plan1CompiledEffect] = []
    for link in links:
        if not isinstance(link, Mapping) or type(link.get("rid")) is not int:
            continue
        row = by_rid.get(link["rid"])
        if not isinstance(row, Mapping):
            continue
        raw_type = row.get("type")
        data = row.get("data")
        if (
            not isinstance(raw_type, Mapping)
            or raw_type.get("class") != "TriggerEffectStatusEffect"
            or not isinstance(data, Mapping)
            or data.get("_isItemDirectEnchant") is True
        ):
            continue
        status_id = data.get("_statusEnchantId")
        raw_trigger = data.get("_trigger")
        trigger_id = (
            raw_trigger.get("_id")
            if isinstance(raw_trigger, Mapping)
            else None
        )
        if not isinstance(status_id, str) or not status_id or not isinstance(trigger_id, str):
            continue
        try:
            enchant = load_runtime_status_enchant(status_id, Path(database))
            trigger = load_exam_status_trigger(trigger_id, Path(database))
        except (KeyError, OSError, TypeError, ValueError) as error:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-status-listener-load-failed",
                    f"{status_id}:{type(error).__name__}:{error}",
                )
            )
            continue
        if enchant.trigger.id != trigger_id:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-status-listener-trigger-mismatch",
                    status_id,
                )
            )
            continue
        duration = data.get("_turn")
        if type(duration) is int and duration >= 0:
            elapsed = max(0, scalar.turn - projection.parsed.current_turn)
            passing = int(data.get("_isPassingTurnStart") is True)
            if duration - max(0, elapsed - passing) <= 0:
                continue
        fires = False
        if phase == PHASE_STATUS_CHANGE:
            # IsChangeEffectTrigger compares PhaseValueList[0] with the
            # positive DifferencePair amount, independently of IsValidTrigger
            # field conditions against the current scalar (APK 0x7e6ae14).
            # Missing delta is not permission to substitute a cumulative buff.
            if changed_amount is not None and changed_amount <= 0:
                continue
            if trigger.phase_values and changed_amount is None and changed_effect_type in trigger.effect_types:
                blockers.append(Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-status-change-delta-unavailable", status_id))
                continue
            neutral = (
                trigger.phase_types == (PHASE_STATUS_CHANGE,)
                and not trigger.field_status_types
                and not trigger.field_status_values
                and not trigger.field_status_card_search_ids
                and not trigger.produce_card_search_id
                and trigger.upper_search_count == 0
                and trigger.lower_search_count == 0
                and trigger.card_move_position_type
                == "ProduceCardMovePositionType_Unknown"
                and trigger.lesson_type == "ProduceStepLessonType_Unknown"
            )
            fires = bool(
                neutral
                and changed_effect_type is not None
                and changed_effect_type in trigger.effect_types
                and (
                    not trigger.phase_values
                    or (
                        len(trigger.phase_values) == 1
                        and changed_amount is not None
                        and changed_amount >= trigger.phase_values[0]
                    )
                )
            )
            # N.I.A. Final variants use the same status-change event with one
            # current-field lower bound.  This remains a structural route:
            # the trigger's effect family selects the event, while the field
            # tuple is evaluated against the post-change scalar supplied by
            # the caller.
            if (
                trigger.phase_types == (PHASE_STATUS_CHANGE,)
                and not trigger.phase_values
                and not trigger.field_status_check_types
                and not trigger.field_status_card_search_ids
                and not trigger.produce_card_search_id
                and trigger.upper_search_count == 0
                and trigger.lower_search_count == 0
                and trigger.card_move_position_type
                == "ProduceCardMovePositionType_Unknown"
                and trigger.lesson_type == "ProduceStepLessonType_Unknown"
                and changed_effect_type is not None
                and changed_effect_type in trigger.effect_types
                and len(trigger.field_status_types) == 1
                and len(trigger.field_status_values) == 1
            ):
                field_type = trigger.field_status_types[0]
                threshold = trigger.field_status_values[0]
                actual = {
                    _GIMMICK_FIELD_LESSON_BUFF: scalar.lesson_buff,
                    _GIMMICK_FIELD_PARAMETER_BUFF_UP: (
                        scalar.parameter_buff_turns
                    ),
                }.get(field_type)
                fires = bool(actual is not None and actual >= threshold)
        elif phase == PHASE_PLAY_COUNT_INTERVAL:
            neutral = (
                trigger.phase_types == (PHASE_PLAY_COUNT_INTERVAL,)
                and len(trigger.phase_values) == 1
                and type(trigger.phase_values[0]) is int
                and trigger.phase_values[0] > 0
                and not trigger.field_status_check_types
                and not trigger.field_status_types
                and not trigger.field_status_values
                and not trigger.field_status_card_search_ids
                and not trigger.effect_types
                and trigger.upper_search_count == 0
                and trigger.lower_search_count == 0
                and trigger.card_move_position_type
                == "ProduceCardMovePositionType_Unknown"
                and trigger.lesson_type == "ProduceStepLessonType_Unknown"
                and playing_card is not None
                and playing_program is not None
            )
            search_matches = False
            if neutral:
                if not trigger.produce_card_search_id:
                    search_matches = True
                elif trigger.card_search_rule is not None:
                    search_matches, search_issue = (
                        match_exact_target_card_context_search(
                            trigger.card_search_rule,
                            card_id=playing_card.card_id,
                            upgrade_count=playing_card.effective_upgrade,
                            card_category=playing_program.category,
                            card_effect_group_ids=playing_program.effect_group_ids,
                        )
                    )
                    if search_issue is not None:
                        search_matches = False
            raw_counts = data.get("_phaseCountDictionary")
            count_rows = (
                raw_counts.get("_list")
                if isinstance(raw_counts, Mapping)
                else None
            )
            phase_count = 0
            count_valid = isinstance(count_rows, list)
            seen_phase_count = False
            if count_valid:
                for raw_count in count_rows:
                    raw_value = (
                        raw_count.get("value")
                        if isinstance(raw_count, Mapping)
                        else None
                    )
                    if (
                        not isinstance(raw_count, Mapping)
                        or type(raw_count.get("key")) is not int
                        or not isinstance(raw_value, Mapping)
                        or type(raw_value.get("current")) is not int
                        or raw_value["current"] < 0
                    ):
                        count_valid = False
                        break
                    if (
                        raw_count["key"]
                        == SERIALIZED_PHASE_ENUM[PHASE_PLAY_COUNT_INTERVAL]
                    ):
                        if seen_phase_count:
                            count_valid = False
                            break
                        seen_phase_count = True
                        phase_count = raw_value["current"]
            fires = bool(
                neutral
                and search_matches
                and count_valid
                and (phase_count + 1) % trigger.phase_values[0] == 0
            )
        elif phase == PHASE_CARD_PLAY:
            search = trigger.card_search_rule
            neutral = (
                trigger.phase_types == (PHASE_CARD_PLAY,)
                and not trigger.phase_values
                and not trigger.field_status_check_types
                and not trigger.field_status_types
                and not trigger.field_status_values
                and not trigger.field_status_card_search_ids
                and not trigger.effect_types
                and bool(trigger.produce_card_search_id)
                and search is not None
                and not exact_playing_card_search_mismatches(
                    search,
                    expected_categories=("ProduceCardCategory_ActiveSkill",),
                    expected_effect_group_ids=(),
                )
                and trigger.upper_search_count == 0
                and trigger.lower_search_count == 1
                and trigger.card_move_position_type
                == "ProduceCardMovePositionType_Unknown"
                and trigger.lesson_type == "ProduceStepLessonType_Unknown"
            )
            fires = bool(
                neutral
                and playing_card is not None
                and playing_program is not None
                and playing_program.category
                == "ProduceCardCategory_ActiveSkill"
            )
        elif phase in {"ProduceExamPhaseType_ExamEndTurn", "ProduceExamPhaseType_StartPlay"}:
            neutral = (
                trigger.phase_types == (phase,)
                and not trigger.phase_values
                and not trigger.field_status_check_types
                and not trigger.field_status_card_search_ids
                and not trigger.effect_types
                and not trigger.produce_card_search_id
                and trigger.upper_search_count == 0
                and trigger.lower_search_count == 0
                and trigger.card_move_position_type
                == "ProduceCardMovePositionType_Unknown"
                and trigger.lesson_type == "ProduceStepLessonType_Unknown"
                and len(trigger.field_status_types)
                == len(trigger.field_status_values)
            )
            if neutral:
                fires = True
                for field_type, threshold in zip(
                    trigger.field_status_types,
                    trigger.field_status_values,
                    strict=True,
                ):
                    if field_type == "ProduceExamFieldStatusType_LessonBuffUp":
                        actual = scalar.lesson_buff
                    elif field_type == "ProduceExamFieldStatusType_ParameterBuffUp":
                        actual = scalar.parameter_buff_turns
                    else:
                        fires = False
                        break
                    if actual < threshold:
                        fires = False
                        break
        if not fires:
            continue
        if require_unlimited_persistent and (data.get("_limitCount") != -1 or data.get("_turn") != -1):
            blockers.append(Plan1RuntimeBridgeBlocker(
                "plan1-runtime-drink-listener-counter-continuation-unbound", status_id))
            continue
        raw_effects = data.get("_effectList")
        observed_ids = tuple(
            effect.get("_id")
            for effect in raw_effects
            if isinstance(effect, Mapping) and isinstance(effect.get("_id"), str)
        ) if isinstance(raw_effects, list) else ()
        master_ids = tuple(effect.id for effect in enchant.effects)
        if observed_ids != master_ids:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-status-listener-effect-mismatch",
                    status_id,
                )
            )
            continue
        for effect_id in master_ids:
            effect = compile_plan1_effect(effect_id, database=Path(database))
            if effect.blockers:
                blockers.extend(
                    Plan1RuntimeBridgeBlocker(value.code, value.detail)
                    for value in effect.blockers
                )
            else:
                compiled.append(effect)
    compiled.extend(_local_status_listener_effects(projection, phase, scalar, database, blockers))
    return tuple(compiled)


def _runtime_pitem_card_play_effects(
    projection: Plan1RuntimeStateProjection,
    card: NativeOrderedCardInstance,
    program: Plan1CompiledCard,
    *,
    database: Path,
    blockers: list[Plan1RuntimeBridgeBlocker],
    phase: str = "ProduceExamPhaseType_ExamCardPlay",
    progress: dict | None = None,
) -> tuple[Plan1CompiledEffect, ...]:
    """Resolve active item-owned ExamCardPlay listeners for one card.

    ``itemList`` may contain common/passive items that do not own an active
    Plan 1 card-play listener.  Restoring the whole list atomically would let
    those unrelated rows block a proven active listener, so this adapter
    groups each captured item-direct status with its own source row.  Every
    behavioural decision still comes from the LocalSave reference graph and
    Master trigger/search rows; item and card IDs are never switches here.
    """

    parsed = projection.parsed
    if parsed is None:
        return ()
    opaque = _opaque_runtime(parsed)
    raw_items = opaque.get("itemList")
    if not isinstance(raw_items, list) or not raw_items:
        return ()
    try:
        graph = parse_plan1_local_save_item_graph(parsed)
    except (KeyError, OSError, TypeError, ValueError) as error:
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-pitem-graph-failed",
                f"{type(error).__name__}:{error}",
            )
        )
        return ()
    if graph.blockers:
        blockers.extend(
            Plan1RuntimeBridgeBlocker(
                f"plan1-runtime-pitem-{value.code}", value.detail
            )
            for value in graph.blockers
        )
        return ()

    sources = {value.item_id: value for value in graph.item_sources}
    compiled: list[Plan1CompiledEffect] = []
    seen_item_statuses: set[tuple[str, str]] = set()
    for reference in graph.active_references:
        data = reference.data
        if reference.class_name != "TriggerEffectStatusEffect":
            if data.get("_isItemDirectEnchant") is True:
                blockers.append(
                    Plan1RuntimeBridgeBlocker(
                        "plan1-runtime-pitem-local-save-item-status-class-unsupported",
                        f"rid={reference.rid}:{reference.class_name}",
                    )
                )
            continue
        if data.get("_isItemDirectEnchant") is not True:
            continue
        raw_item = data.get("_triggerItem")
        item_id = raw_item.get("_id") if isinstance(raw_item, Mapping) else None
        source = sources.get(item_id) if isinstance(item_id, str) else None
        if source is None:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-pitem-source-missing",
                    f"rid={reference.rid};item={item_id!r}",
                )
            )
            continue
        status_id = data.get("_statusEnchantId")
        if not isinstance(status_id, str) or not status_id:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-pitem-status-id-invalid",
                    f"rid={reference.rid}",
                )
            )
            continue
        status_key = (source.item_id, status_id)
        if status_key in seen_item_statuses:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-pitem-status-duplicate",
                    f"rid={reference.rid};item={source.item_id};status={status_id}",
                )
            )
            continue
        seen_item_statuses.add(status_key)
        item_row = {
            "_fireCount": source.fire_count,
            "_id": source.item_id,
            "_itemType": source.item_type,
            "_parentCustomItemIds": list(source.parent_custom_item_ids),
            "_reactionCount": source.reaction_count,
        }
        restoration = restore_plan1_local_save_item_runtime(
            item_list=(item_row,),
            active_trigger_statuses=((reference.rid, data),),
            database=Path(database),
        )
        if not restoration.supported or restoration.runtime is None:
            blockers.extend(
                Plan1RuntimeBridgeBlocker(
                    f"plan1-runtime-pitem-{value.code}", value.detail
                )
                for value in restoration.blockers
            )
            continue
        restored_runtime = next((runtime for item, status, runtime in projection.local_item_runtimes
                                 if (item, status) == status_key), restoration.runtime)
        event = Plan1PItemEventContext(
            phase=phase,
            field_values={
                "ProduceExamFieldStatusType_LessonBuffUp": (
                    projection.state.scalar.lesson_buff
                    if projection.state is not None
                    else 0
                ),
                "ProduceExamFieldStatusType_ParameterBuffUp": (
                    projection.state.scalar.parameter_buff_turns
                    if projection.state is not None
                    else 0
                ),
            },
            card_id=card.card_id,
            card_upgrade=card.effective_upgrade,
            card_category=program.category,
        )
        dispatched = dispatch_plan1_pitem_event(restored_runtime, event)
        if not dispatched.applied:
            blockers.extend(
                Plan1RuntimeBridgeBlocker(
                    f"plan1-runtime-pitem-{value.code}", value.detail
                )
                for value in dispatched.blockers
            )
            continue
        if progress is not None:
            progress[status_key] = dispatched.after
        for effect in dispatched.effects:
            try:
                compiled_effect = compile_plan1_effect(
                    effect.id,
                    database=Path(database),
                )
            except (KeyError, OSError, TypeError, ValueError) as error:
                blockers.append(
                    Plan1RuntimeBridgeBlocker(
                        "plan1-runtime-pitem-effect-compile-failed",
                        f"{effect.id}:{type(error).__name__}:{error}",
                    )
                )
                continue
            if compiled_effect.blockers:
                blockers.extend(
                    Plan1RuntimeBridgeBlocker(value.code, value.detail)
                    for value in compiled_effect.blockers
                )
                continue
            compiled.append(compiled_effect)
    return tuple(compiled)


def _runtime_pitem_start_turn_effects(
    projection: Plan1RuntimeStateProjection,
    *,
    database: Path,
    blockers: list[Plan1RuntimeBridgeBlocker],
) -> tuple[Plan1CompiledEffect, ...]:
    """Resolve active item-owned StartTurn listeners in captured status order."""

    parsed = projection.parsed
    if parsed is None:
        return ()
    raw_items = _opaque_runtime(parsed).get("itemList")
    if not isinstance(raw_items, list) or not raw_items:
        return ()
    try:
        graph = parse_plan1_local_save_item_graph(parsed)
    except (KeyError, OSError, TypeError, ValueError) as error:
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-pitem-graph-failed",
                f"{type(error).__name__}:{error}",
            )
        )
        return ()
    if graph.blockers:
        blockers.extend(
            Plan1RuntimeBridgeBlocker(
                f"plan1-runtime-pitem-{value.code}", value.detail
            )
            for value in graph.blockers
        )
        return ()
    next_parameter = (
        parsed.turn_parameter_types[parsed.current_turn]
        if parsed.current_turn < len(parsed.turn_parameter_types)
        else None
    )
    lesson_type = {
        1: "ProduceStepLessonType_LessonVocal",
        2: "ProduceStepLessonType_LessonDance",
        3: "ProduceStepLessonType_LessonVisual",
    }.get(next_parameter, "ProduceStepLessonType_Unknown")
    sources = {value.item_id: value for value in graph.item_sources}
    compiled: list[Plan1CompiledEffect] = []
    for reference in graph.active_references:
        data = reference.data
        if (
            reference.class_name != "TriggerEffectStatusEffect"
            or data.get("_isItemDirectEnchant") is not True
        ):
            continue
        raw_item = data.get("_triggerItem")
        item_id = raw_item.get("_id") if isinstance(raw_item, Mapping) else None
        source = sources.get(item_id) if isinstance(item_id, str) else None
        if source is None:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-pitem-source-missing",
                    f"rid={reference.rid};item={item_id!r}",
                )
            )
            continue
        item_row = {
            "_fireCount": source.fire_count,
            "_id": source.item_id,
            "_itemType": source.item_type,
            "_parentCustomItemIds": list(source.parent_custom_item_ids),
            "_reactionCount": source.reaction_count,
        }
        restoration = restore_plan1_local_save_item_runtime(
            item_list=(item_row,),
            active_trigger_statuses=((reference.rid, data),),
            database=Path(database),
        )
        if not restoration.supported or restoration.runtime is None:
            blockers.extend(
                Plan1RuntimeBridgeBlocker(
                    f"plan1-runtime-pitem-{value.code}", value.detail
                )
                for value in restoration.blockers
            )
            continue
        dispatched = dispatch_plan1_pitem_event(
            restoration.runtime,
            Plan1PItemEventContext(
                phase="ProduceExamPhaseType_ExamStartTurn",
                field_values={
                    "ProduceExamFieldStatusType_LessonBuffUp": (
                        projection.state.scalar.lesson_buff
                        if projection.state is not None
                        else 0
                    ),
                    "ProduceExamFieldStatusType_ParameterBuffUp": (
                        projection.state.scalar.parameter_buff_turns
                        if projection.state is not None
                        else 0
                    ),
                },
                lesson_type=lesson_type,
            ),
        )
        if not dispatched.applied:
            blockers.extend(
                Plan1RuntimeBridgeBlocker(
                    f"plan1-runtime-pitem-{value.code}", value.detail
                )
                for value in dispatched.blockers
            )
            continue
        for effect in dispatched.effects:
            compiled_effect = compile_plan1_effect(
                effect.id,
                database=Path(database),
            )
            if compiled_effect.blockers:
                blockers.extend(
                    Plan1RuntimeBridgeBlocker(value.code, value.detail)
                    for value in compiled_effect.blockers
                )
            else:
                compiled.append(compiled_effect)
    return tuple(compiled)


def _runtime_interval_effect_group_evidence(
    program: Plan1CompiledCard,
    dispatch: Plan1PItemEventDispatch,
) -> tuple[tuple[str, ...] | None, Plan1RuntimeBridgeBlocker | None]:
    """Return compiled card groups or a typed missing-provenance blocker."""

    if not isinstance(program, Plan1CompiledCard):
        raise TypeError("program must be Plan1CompiledCard")
    if not isinstance(dispatch, Plan1PItemEventDispatch):
        raise TypeError("dispatch must be Plan1PItemEventDispatch")
    required = any(
        reference.rule is not None and bool(reference.rule.effect_group_ids)
        for reference in dispatch.search_references
    )
    if required and not program.effect_group_ids_known:
        return None, Plan1RuntimeBridgeBlocker(
            "plan1-runtime-pitem-interval-effect-groups-unavailable",
            program.card_id,
        )
    return program.effect_group_ids, None


def _runtime_pitem_play_count_interval_effects(
    projection: Plan1RuntimeStateProjection,
    card: NativeOrderedCardInstance,
    program: Plan1CompiledCard,
    *,
    database: Path,
    blockers: list[Plan1RuntimeBridgeBlocker],
) -> tuple[
    tuple[Plan1CompiledEffect, ...],
    tuple[Plan1PlayCountIntervalListenerStatus, ...],
]:
    """Resolve matching item phase-23 listeners before direct card effects."""

    parsed = projection.parsed
    existing_progress = (
        ()
        if projection.state is None
        else projection.state.scalar.play_count_interval_listeners
    )
    if parsed is None:
        return (), existing_progress
    opaque = _opaque_runtime(parsed)
    raw_items = opaque.get("itemList")
    if not isinstance(raw_items, list) or not raw_items:
        return (), existing_progress
    try:
        graph = parse_plan1_local_save_item_graph(parsed)
    except (KeyError, OSError, TypeError, ValueError) as error:
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-pitem-interval-graph-failed",
                f"{type(error).__name__}:{error}",
            )
        )
        return (), existing_progress
    if graph.blockers:
        blockers.extend(
            Plan1RuntimeBridgeBlocker(
                f"plan1-runtime-pitem-{value.code}", value.detail
            )
            for value in graph.blockers
        )
        return (), existing_progress

    sources = {value.item_id: value for value in graph.item_sources}
    active: list[ActivePlan3StatusEnchant] = []
    listener_progress: list[Plan1PlayCountIntervalListenerStatus] = []
    inverse_phases = {value: key for key, value in SERIALIZED_PHASE_ENUM.items()}
    for reference in graph.active_references:
        data = reference.data
        if (
            reference.class_name != "TriggerEffectStatusEffect"
            or data.get("_isItemDirectEnchant") is not True
        ):
            continue
        raw_trigger = data.get("_trigger")
        raw_phases = (
            raw_trigger.get("_phaseTypeList")
            if isinstance(raw_trigger, Mapping)
            else None
        )
        if raw_phases != [SERIALIZED_PHASE_ENUM[PHASE_PLAY_COUNT_INTERVAL]]:
            continue
        raw_item = data.get("_triggerItem")
        item_id = raw_item.get("_id") if isinstance(raw_item, Mapping) else None
        source = sources.get(item_id) if isinstance(item_id, str) else None
        if source is None:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-pitem-interval-source-missing",
                    f"rid={reference.rid};item={item_id!r}",
                )
            )
            return (), existing_progress
        status_id = data.get("_statusEnchantId")
        if not isinstance(status_id, str) or not status_id:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-pitem-interval-status-id-invalid",
                    f"rid={reference.rid}",
                )
            )
            return (), existing_progress
        item_row = {
            "_fireCount": source.fire_count,
            "_id": source.item_id,
            "_itemType": source.item_type,
            "_parentCustomItemIds": list(source.parent_custom_item_ids),
            "_reactionCount": source.reaction_count,
        }
        restoration = restore_plan1_local_save_item_runtime(
            item_list=(item_row,),
            active_trigger_statuses=((reference.rid, data),),
            database=Path(database),
        )
        if not restoration.supported or restoration.runtime is None:
            blockers.extend(
                Plan1RuntimeBridgeBlocker(
                    f"plan1-runtime-pitem-{value.code}", value.detail
                )
                for value in restoration.blockers
            )
            return (), existing_progress
        if len(restoration.runtime.listeners) != 1:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-pitem-interval-listener-count-invalid",
                    status_id,
                )
            )
            return (), existing_progress
        listener = restoration.runtime.listeners[0]
        trigger = listener.dispatch.trigger
        effect_groups, group_blocker = (
            _runtime_interval_effect_group_evidence(
                program,
                listener.dispatch,
            )
        )
        if group_blocker is not None or effect_groups is None:
            assert group_blocker is not None
            blockers.append(group_blocker)
            return (), existing_progress
        evaluation = evaluate_plan1_pitem_event_dispatch(
            listener.dispatch,
            Plan1PItemEventContext(
                phase=PHASE_PLAY_COUNT_INTERVAL,
                phase_values=trigger.phase_values,
                card_id=card.card_id,
                card_upgrade=card.effective_upgrade,
                card_category=program.category,
                card_effect_group_ids=effect_groups,
            ),
        )
        if evaluation.fires is None:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-pitem-interval-evidence-incomplete",
                    f"{status_id}:{','.join(evaluation.reasons)}",
                )
            )
            return (), existing_progress
        try:
            rule = load_plan3_status_enchant(status_id, Path(database))
        except (KeyError, OSError, TypeError, ValueError) as error:
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-pitem-interval-rule-load-failed",
                    f"{status_id}:{type(error).__name__}:{error}",
                )
            )
            return (), existing_progress
        if (
            rule.trigger.id != trigger.trigger_id
            or tuple(effect.id for effect in rule.effects)
            != tuple(effect.id for effect in listener.effects)
        ):
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-pitem-interval-rule-mismatch",
                    status_id,
                )
            )
            return (), existing_progress

        raw_counts = data.get("_phaseCountDictionary")
        rows = (
            raw_counts.get("_list")
            if isinstance(raw_counts, Mapping)
            else None
        )
        if not isinstance(rows, list):
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-pitem-interval-counter-invalid",
                    status_id,
                )
            )
            return (), existing_progress
        phase_counts: list[tuple[str, int]] = []
        for raw_count in rows:
            if not isinstance(raw_count, Mapping):
                blockers.append(
                    Plan1RuntimeBridgeBlocker(
                        "plan1-runtime-pitem-interval-counter-invalid",
                        status_id,
                    )
                )
                return (), existing_progress
            phase = inverse_phases.get(raw_count.get("key"))
            raw_value = raw_count.get("value")
            count = (
                raw_value.get("current")
                if isinstance(raw_value, Mapping)
                else None
            )
            if (
                phase is None
                or type(count) is not int
                or count < 0
            ):
                blockers.append(
                    Plan1RuntimeBridgeBlocker(
                        "plan1-runtime-pitem-interval-counter-invalid",
                        status_id,
                    )
                )
                return (), existing_progress
            phase_counts.append((phase, count))
        interval_count = next(
            (
                count
                for phase, count in phase_counts
                if phase == PHASE_PLAY_COUNT_INTERVAL
            ),
            0,
        )
        progress = Plan1PlayCountIntervalListenerStatus(
            uid=listener.status_uid,
            source_id=source.item_id,
            rule_id=status_id,
            phase_count=interval_count,
            remaining_uses=listener.remaining_uses,
            turn_count=listener.turn_count,
        )
        listener_progress.append(progress)
        if listener.remaining_uses == 0 or not evaluation.fires:
            continue
        active.append(
            ActivePlan3StatusEnchant(
                instance_id=f"localsave:{reference.rid}:{status_id}",
                source_id=source.item_id,
                rule=rule,
                max_uses=(
                    0 if listener.remaining_uses == -1
                    else listener.remaining_uses
                ),
                uses=0,
                max_uses_per_turn=0,
                uses_this_turn=0,
                remaining_turns=int(data.get("_turn", -1)),
                passing_turn_start=bool(data.get("_isPassingTurnStart")),
                is_item_direct=True,
                turn_count=listener.turn_count,
                phase_counts=tuple(phase_counts),
                native_uid=listener.status_uid,
            )
        )
    if not active:
        return (), tuple(listener_progress)

    def validate(effect: Plan3Effect) -> tuple[str, ...]:
        if (
            effect.effect_type == EFFECT_LESSON_BUFF_ADDITIVE
            and effect.value1 > 0
            and effect.value2 == 0
            and effect.effect_count == 0
            and effect.effect_turn > 0
            and not effect.status_enchant_id
            and effect.status_enchant is None
            and not effect.chain_effect_id
            and not effect.chain_effect_ids
            and effect.chain_effect is None
            and effect.trigger is None
            and not effect.once
            and effect.card_move_rule is None
        ):
            return ()
        return ("lesson-buff-additive-shape",)

    def execute(
        payload: tuple[Plan3Effect, ...],
        effect: Plan3Effect,
        _context,
    ) -> PlayCountIntervalEffectApplication[tuple[Plan3Effect, ...]]:
        return PlayCountIntervalEffectApplication((*payload, effect))

    try:
        execution = execute_ordered_play_count_intervals(
            OrderedPlayCountIntervalState((), tuple(active)),
            (
                PlayCountIntervalEffectHandler(
                    EFFECT_LESSON_BUFF_ADDITIVE,
                    validate,
                    execute,
                ),
            ),
        )
    except (PlayCountIntervalDispatchError, TypeError, ValueError) as error:
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-pitem-interval-dispatch-failed",
                f"{type(error).__name__}:{error}",
            )
        )
        return (), existing_progress
    updated_by_uid = {
        value.native_uid: value for value in execution.after.active
    }
    updated_progress = tuple(
        replace(
            value,
            phase_count=updated_by_uid[value.uid].phase_count(
                PHASE_PLAY_COUNT_INTERVAL
            ),
            remaining_uses=(
                -1
                if updated_by_uid[value.uid].max_uses == 0
                else max(
                    updated_by_uid[value.uid].max_uses
                    - updated_by_uid[value.uid].uses,
                    0,
                )
            ),
            turn_count=updated_by_uid[value.uid].turn_count,
        )
        if value.uid in updated_by_uid
        else value
        for value in listener_progress
    )
    return tuple(
        Plan1CompiledEffect(
            effect_id=effect.id,
            effect_type=effect.effect_type,
            value1=effect.value1,
            value2=effect.value2,
            effect_count=effect.effect_count,
            effect_turn=effect.effect_turn,
        )
        for effect in execution.after.payload
    ), updated_progress


def _runtime_hand_add_support_resolver(
    projection: Plan1RuntimeStateProjection,
    state: Plan1NativeStageState,
    *,
    database: Path,
    blockers: list[Plan1RuntimeBridgeBlocker],
) -> tuple[object | None, tuple[str, ...]]:
    """Project LocalSave support rows into the existing Plan1 callback."""

    parsed = projection.parsed
    if parsed is None or parsed.root_runtime is None:
        return None, ()
    root = parsed.root_runtime.opaque_fields.to_value()
    if not isinstance(root, Mapping):
        return None, tuple(parsed.turn_use_support_ids)
    raw_support = root.get("supportCardList")
    if raw_support is None:
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-hand-add-support-unavailable",
                "supportCardList:not-a-list:None",
            )
        )
        return None, tuple(parsed.turn_use_support_ids)
    if not isinstance(raw_support, list):
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-hand-add-support-unavailable",
                f"supportCardList:not-a-list:{type(raw_support).__name__}",
            )
        )
        return None, tuple(parsed.turn_use_support_ids)
    if not raw_support:
        return None, tuple(parsed.turn_use_support_ids)
    try:
        runtime = project_exam_hand_add_support_runtime(
            parsed,
            database=Path(database),
        )
        next_turn_lesson_type: str | None = None
        if parsed.current_turn < len(parsed.turn_parameter_types):
            next_parameter = parsed.turn_parameter_types[parsed.current_turn]
            next_turn_lesson_type = {
                1: "ProduceStepLessonType_LessonVocal",
                2: "ProduceStepLessonType_LessonDance",
                3: "ProduceStepLessonType_LessonVisual",
            }.get(next_parameter)
            if next_turn_lesson_type is None:
                raise ValueError(
                    f"unsupported next-turn parameter type: {next_parameter}"
                )
        support_searches = {
            value.id: value for value in runtime.support_card_searches
        }

        # Keep this callback pure.  The ordered-zone draw/shuffle path may
        # consume native RNG between two HandAdd requests; a stateful
        # provider would reject that valid intervening consumer as a broken
        # chain.  The request itself carries the authoritative RNG cursor and
        # used-support tuple for each boundary.  A TurnStart draw belongs to
        # the *next* turn's parameter type; effect draws still belong to the
        # current turn.  Keeping that choice at the phase boundary prevents
        # two adjacent equal parameter types from hiding a stale-turn bug.
        def resolve(request):
            lesson_type = runtime.request.lesson_type
            if (
                (request.effect_id == "plan1-turn-start-draw" or state.scalar.turn > parsed.current_turn)
                and next_turn_lesson_type is not None
            ):
                lesson_type = next_turn_lesson_type
            return evaluate_native_hand_add_support(
                request.drawn_cards,
                lesson_type=lesson_type,
                random_state=request.random_state,
                support_upgrades=runtime.support_upgrades,
                support_card_searches=support_searches,
                used_support_ids=request.used_support_ids,
            )

        return resolve, runtime.used_support_ids
    except (KeyError, OSError, TypeError, ValueError) as error:
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-hand-add-support-unavailable",
                f"{type(error).__name__}:{error}",
            )
        )
        return None, ()


def _card_after_step(
    result: Plan1NativeStepResult,
) -> Plan1NativeStageState:
    """Reify the executor's settled card checkpoint before TurnStart."""

    action = result.card_action
    step = result.card_step
    scalar = step.transition.after
    # The one-step executor keeps the native status lifecycle bit outside the
    # legacy card transition.  Reapply the same bit here so the automatic
    # boundary evidence starts at the exact executor card checkpoint.
    parameter_buff_applied = any(
        entry.effect_type == EFFECT_PARAMETER_BUFF
        and entry.stage.value == "effect"
        and entry.after.parameter_buff_turns > entry.before.parameter_buff_turns
        for entry in step.transition.trace
    )
    if (
        parameter_buff_applied
        and result.before.scalar.parameter_buff_turns == 0
    ):
        scalar = replace(scalar, parameter_buff_fresh=True)
    return replace(
        result.before,
        scalar=scalar,
        zones=step.after_zones,
        steps=(*result.before.steps, step),
        effect_timers=(*result.before.effect_timers, *step.timer_installs),
        next_timer_install_ordinal=(
            result.before.next_timer_install_ordinal + len(step.timer_installs)
        ),
        hand_add_used_support_ids=action.hand_add_used_support_ids_after,
        equipped_item_runtime=action.equipped_item_runtime_after,
    )


def _automatic_turn_end_from_step(
    result: Plan1NativeStepResult,
    projection: Plan1RuntimeStateProjection,
    *,
    database: Path,
    blockers: list[Plan1RuntimeBridgeBlocker],
) -> tuple[Plan1RuntimeTurnEnd | None, Plan1NativeStageState | None]:
    """Attach generic ExamSetting recovery to an executor TurnStart result."""

    if result.boundary is None or result.stage_boundary is None:
        return None, result.after
    card_after = _card_after_step(result)
    parsed = projection.parsed
    setting_id = None if parsed is None else parsed.setting_id
    if parsed is None:
        # A bare typed projection has no remaining-turn authority.  The
        # executor result is still useful, but no recovery can be claimed.
        return (
            Plan1RuntimeTurnEnd(
                before=card_after,
                after=result.after,
                before_zones=card_after.zones,
                after_zones=result.after.zones,
                recovery_requested=0,
                recovery_actual=0,
                terminal=False,
                boundary=result.stage_boundary,
            ),
            result.after,
        )
    try:
        assert setting_id is not None
        _draw_count, _recovery_per_turn = _exam_setting_turn_values(setting_id)
        effective_settings = load_plan1_native_settings(setting_id)
        ended = _apply_runtime_end_turn_listeners(
            projection,
            card_after,
            result.after,
            settings=effective_settings,
            database=Path(database),
            blockers=blockers,
        )
        if ended is None:
            return None, None
        gimmicked = _apply_runtime_turn_start_gimmicks(
            projection,
            ended,
            settings=effective_settings,
            database=Path(database),
            blockers=blockers,
        )
        if gimmicked is None:
            return None, None
        gimmicked = _apply_runtime_start_play_listeners(projection, gimmicked,
            settings=effective_settings, database=Path(database), blockers=blockers)
        if gimmicked is None:
            return None, None
        # Native distinguishes exhausting the last playable card from an
        # explicit replay EndTurn action.  Only the explicit action applies
        # ExamSetting.examTurnEndRecoveryStamina; the automatic continuation
        # closes/draws/starts the next turn without recovery.
        requested = 0
        actual = 0
        after = gimmicked
        return (
            Plan1RuntimeTurnEnd(
                before=card_after,
                after=after,
                before_zones=card_after.zones,
                after_zones=after.zones,
                recovery_requested=requested,
                recovery_actual=actual,
                terminal=False,
                boundary=result.stage_boundary,
            ),
            after,
        )
    except (KeyError, OSError, TypeError, ValueError, OverflowError) as error:
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-step-recovery-failed",
                f"{setting_id}:{type(error).__name__}:{error}",
            )
        )
        return None, None


def _automatic_terminal_from_step(
    result: Plan1NativeStepResult,
    projection: Plan1RuntimeStateProjection,
    *,
    database: Path,
    blockers: list[Plan1RuntimeBridgeBlocker],
) -> tuple[Plan1RuntimeTurnEnd | None, Plan1NativeStageState | None]:
    """Close a final exam turn after executor card settlement."""

    card_after = result.after
    parsed = projection.parsed
    if parsed is None:
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-terminal-turn-facts-unavailable",
                "parsed state_before is required for terminal remainTurn",
            )
        )
        return None, None
    dispositions, disposition_blockers = _turn_end_dispositions(
        card_after,
        database=Path(database),
    )
    blockers.extend(disposition_blockers)
    if dispositions is None:
        return None, None
    try:
        _draw_count, _recovery_per_turn = _exam_setting_turn_values(
            parsed.setting_id
        )
        effective_settings = load_plan1_native_settings(parsed.setting_id)
        remain_turn = parsed.remain_turn
        if remain_turn < 1:
            raise ValueError(
                f"remainTurn must be positive before terminal EndTurn: {remain_turn}"
            )
        closed = card_after.zones.close_turn(dispositions)
        requested = 0
        actual = 0
        after = replace(
            card_after,
            scalar=replace(
                card_after.scalar,
                plays_remaining=0,
            ),
            zones=closed,
        )
        ended = _apply_runtime_end_turn_listeners(
            projection,
            card_after,
            after,
            settings=effective_settings,
            database=Path(database),
            blockers=blockers,
        )
        if ended is None:
            return None, None
        after = ended
        return (
            Plan1RuntimeTurnEnd(
                before=card_after,
                after=after,
                before_zones=card_after.zones,
                after_zones=closed,
                recovery_requested=requested,
                recovery_actual=actual,
                terminal=True,
                boundary=None,
            ),
            after,
        )
    except (NativeOrderedZoneError, KeyError, OSError, TypeError, ValueError, OverflowError) as error:
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-terminal-turn-apply-failed",
                f"{type(error).__name__}:{error}",
            )
        )
        return None, None


def apply_plan1_runtime_action(
    projection: Plan1RuntimeStateProjection,
    action: LeaderboardReplayAction,
    *,
    database: Path = DEFAULT_DATABASE,
    settings: Plan1NativeSettings | None = None,
    generated_card_identity: object | None = None,
    generated_identity: object | None = None,
    runtime_action_order: int | None = None,
    drink_selected_card_guids: tuple[str, ...] | None = None,
) -> Plan1RuntimeTransitionProjection:
    """Send one normalized ``use-hand`` action through the Plan 1 stage.

    The function reads only ``projection`` (which came from state_before) and
    ``action``.  It intentionally has no ``state_after`` parameter, making
    accidental post-state backfill impossible at this boundary.
    """

    if not isinstance(projection, Plan1RuntimeStateProjection):
        raise TypeError("projection must be Plan1RuntimeStateProjection")
    if not isinstance(action, LeaderboardReplayAction):
        raise TypeError("action must be LeaderboardReplayAction")
    blockers = list(projection.blockers)
    if generated_card_identity is not None and generated_identity is not None:
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-generated-identity-ambiguous",
                "supply generated_card_identity or generated_identity, not both",
            )
        )
        generated_card_identity = None
    elif generated_card_identity is None:
        generated_card_identity = generated_identity
    slot_index: int | None = None
    card: NativeOrderedCardInstance | None = None
    program: Plan1CompiledCard | None = None
    stage_action: Plan1StageAction | None = None
    transition: Plan1CardTransition | None = None
    next_state: Plan1NativeStageState | None = None
    step: Plan1StageStep | None = None
    native_step: Plan1NativeStepResult | None = None
    drink: DrinkCatalogDrink | None = None
    drink_application: Plan1RuntimeDrinkApplication | None = None
    turn_end: Plan1RuntimeTurnEnd | None = None
    state = projection.state

    if action.action_type == "use-drink":
        (
            slot_index,
            drink,
            drink_application,
            next_state,
        ) = _apply_plan1_runtime_drink(
            projection,
            action,
            database=Path(database),
            settings=settings,
            blockers=blockers,
            selected_card_guids=drink_selected_card_guids,
        )
    elif action.action_type == "turn-end":
        turn_end, next_state = _apply_plan1_runtime_turn_end(
            projection,
            action,
            database=Path(database),
            settings=settings,
            blockers=blockers,
        )
    elif action.action_type != "use-hand":
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-action-type-unsupported", action.action_type
            )
        )
    elif len(action.indexes) != 1:
        blockers.append(
            Plan1RuntimeBridgeBlocker(
                "plan1-runtime-action-index-shape-unsupported",
                f"indexes={list(action.indexes)!r}",
            )
        )
    elif state is None:
        blockers.append(
            Plan1RuntimeBridgeBlocker("plan1-runtime-stage-state-unavailable")
        )
    else:
        slot_index = action.indexes[0]
        if slot_index < 0 or slot_index >= len(state.zones.hand):
            blockers.append(
                Plan1RuntimeBridgeBlocker(
                    "plan1-runtime-hand-slot-out-of-range",
                    f"slot={slot_index};hand={len(state.zones.hand)}",
                )
            )
        else:
            restored_timers = _restore_captured_effect_timers(
                projection,
                database=Path(database),
                blockers=blockers,
            )
            if restored_timers:
                state = replace(
                    state,
                    effect_timers=restored_timers,
                    next_timer_install_ordinal=len(restored_timers),
                )
                projection = replace(projection, state=state)
            card = state.zones.hand[slot_index]
            try:
                # Bind the selected LocalSave instance's ordered
                # ``customizeCountList`` through the generic Master-backed
                # compiler before the native step executor.  This is
                # instance evidence from state-before, not a card-id branch.
                program = compile_plan1_card_instance(
                    card,
                    database=Path(database),
                )
                program = replace(program, instance_guid=card.guid)
                program = _promote_transition_local_status_installers(program)
                card_play_status_effects = _active_status_listener_effects(
                    projection,
                    phase=PHASE_CARD_PLAY,
                    scalar=state.scalar,
                    playing_card=card,
                    playing_program=program,
                    database=Path(database),
                    blockers=blockers,
                )
                item_progress = {(item, status): runtime for item, status, runtime in projection.local_item_runtimes}
                pitem_effects = _runtime_pitem_card_play_effects(
                    projection,
                    card,
                    program,
                    database=Path(database),
                    blockers=blockers,
                    progress=item_progress,
                )
                (
                    interval_effects,
                    interval_listener_progress,
                ) = _runtime_pitem_play_count_interval_effects(
                    projection,
                    card,
                    program,
                    database=Path(database),
                    blockers=blockers,
                )
                gimmick_interval_effects = _active_status_listener_effects(
                    projection,
                    phase=PHASE_PLAY_COUNT_INTERVAL,
                    scalar=state.scalar,
                    playing_card=card,
                    playing_program=program,
                    database=Path(database),
                    blockers=blockers,
                )
                if (
                    interval_listener_progress
                    != state.scalar.play_count_interval_listeners
                ):
                    state = replace(
                        state,
                        scalar=replace(
                            state.scalar,
                            play_count_interval_listeners=(
                                interval_listener_progress
                            ),
                        ),
                    )
                    projection = replace(projection, state=state)
                if (
                    pitem_effects
                    or interval_effects
                    or gimmick_interval_effects
                    or card_play_status_effects
                ):
                    program = replace(
                        program,
                        # Native ExamCardPlay item listeners run after cost
                        # payment, followed by phase-23 interval listeners,
                        # then the selected card's direct effect list.
                        # Preserve that boundary generically; moving a fired
                        # additive after the card would make it invisible to
                        # the same card's LessonBuff arithmetic.
                        effects=(
                            *pitem_effects,
                            *card_play_status_effects,
                            *interval_effects,
                            *gimmick_interval_effects,
                            *program.effects,
                        ),
                    )
                lesson_status_listener_effects: list[Plan1CompiledEffect] = []
                if any(
                    effect.effect_type == EFFECT_LESSON_BUFF
                    for effect in program.effects
                ):
                    lesson_buff_preview = execute_plan1_effects(
                        state.scalar,
                        tuple(
                            effect
                            for effect in program.effects
                            if effect.effect_type == EFFECT_LESSON_BUFF
                        ),
                    )
                    lesson_status_scalar = lesson_buff_preview.after
                    lesson_status_listener_effects.extend(
                        _active_status_listener_effects(
                            projection,
                            phase=PHASE_STATUS_CHANGE,
                            scalar=lesson_status_scalar,
                            changed_effect_type=EFFECT_LESSON_BUFF,
                            changed_amount=lesson_status_scalar.lesson_buff - state.scalar.lesson_buff,
                            database=Path(database),
                            blockers=blockers,
                        )
                    )
                if lesson_status_listener_effects:
                    program = replace(
                        program,
                        effects=(
                            *program.effects,
                            *lesson_status_listener_effects,
                        ),
                    )
                after_effects = _runtime_pitem_card_play_effects(projection, card, program,
                    database=Path(database), blockers=blockers, phase="ProduceExamPhaseType_ExamCardPlayAfter", progress=item_progress)
                if after_effects:
                    # This known scalar callback is after the selected card's
                    # effects and their children, before the step checks the
                    # remaining playable count. Its +1 must prevent EndTurn.
                    program = replace(program, effects=(*program.effects, *after_effects))
                if item_progress:
                    projection = replace(projection, local_item_runtimes=tuple(
                        (item, status, runtime) for (item, status), runtime in item_progress.items()))
                effective_settings = (
                    load_plan1_native_settings(
                        projection.parsed.setting_id
                        if projection.parsed is not None
                        else "p_exam_setting-1"
                    )
                    if settings is None
                    else settings
                )
                # A generated-card identity is accepted only after the
                # strict recorder adapter has proved it at CreateGuidIfNeed.
                # The binding key deliberately comes from this captured
                # source card's runtime play_count and the unique effect
                # index in the compiled program.  No state_after is passed
                # to, or read by, the adapter.
                if generated_card_identity is not None:
                    binding_result = (
                        adapt_generated_card_identity_to_plan1_binding(
                            generated_card_identity,
                            source_play_count=card.runtime_state.play_count,
                            program=program,
                            expected_action_order=(
                                action.order
                                if runtime_action_order is None
                                else runtime_action_order
                            ),
                            expected_source_guid=card.guid,
                        )
                    )
                    blockers.extend(
                        Plan1RuntimeBridgeBlocker(value.code, value.detail)
                        for value in binding_result.blockers
                    )
                    if binding_result.accepted:
                        assert binding_result.binding is not None
                        existing = tuple(
                            value
                            for value in state.card_create_bindings
                            if value.invocation_key
                            != binding_result.binding.invocation_key
                        )
                        conflicting = tuple(
                            value
                            for value in state.card_create_bindings
                            if value.invocation_key
                            == binding_result.binding.invocation_key
                            and value.guid_tokens
                            != binding_result.binding.guid_tokens
                        )
                        if conflicting:
                            blockers.append(
                                Plan1RuntimeBridgeBlocker(
                                    "plan1-runtime-generated-binding-ambiguous",
                                    f"invocation={binding_result.binding.invocation_key!r}",
                                )
                            )
                        else:
                            state = replace(
                                state,
                                card_create_bindings=(
                                    *existing,
                                    binding_result.binding,
                                ),
                            )
                            projection = replace(projection, state=state)
                # A replay already identifies one Hand slot.  Route that
                # selected action through the native one-step executor so a
                # final playable card can carry the automatic EndTurn /
                # TurnStart continuation.  The boundary is built only from
                # this captured state-before and generic Master facts.
                native_action = Plan1NativeStepAction.from_replay_action(
                    action,
                    state,
                )
                parameter_buff_listener = _runtime_parameter_buff_listener(
                    projection,
                    scalar=state.scalar,
                    database=Path(database),
                    blockers=blockers,
                )
                (
                    hand_add_support_resolver,
                    used_support_ids,
                ) = _runtime_hand_add_support_resolver(
                    projection,
                    state,
                    database=Path(database),
                    blockers=blockers,
                )
                if used_support_ids != state.hand_add_used_support_ids:
                    state = replace(
                        state,
                        hand_add_used_support_ids=used_support_ids,
                    )
                    projection = replace(projection, state=state)
                turn_boundary: Plan1NativeTurnBoundary | None = None
                auto_end_turn = False
                terminal_turn = False
                if state.scalar.plays_remaining <= 1:
                    parsed = projection.parsed
                    terminal_turn = parsed is not None and parsed.remain_turn <= 1
                    if not terminal_turn:
                        turn_boundary = _runtime_boundary_for_step(
                            projection,
                            settings=effective_settings,
                            database=Path(database),
                            blockers=blockers,
                        )
                        # Keep this true even when boundary derivation failed:
                        # the executor then emits its typed
                        # ``step-turn-boundary-required`` blocker rather than
                        # publishing an unbounded card-only result.
                        auto_end_turn = True
                native_step = Plan1NativeStepExecutor(
                    database=Path(database)
                ).execute(
                    state,
                    native_action,
                    program=program,
                    settings=effective_settings,
                    turn_boundary=turn_boundary,
                    parameter_buff_listener=parameter_buff_listener,
                    hand_add_support_resolver=hand_add_support_resolver,
                    auto_end_turn=auto_end_turn,
                    database=Path(database),
                )
                stage_action = native_step.card_action
                transition = native_step.transition
                step = native_step.card_step
                next_state = native_step.after
                if native_step.boundary is not None:
                    turn_end, next_state = _automatic_turn_end_from_step(
                        native_step,
                        projection,
                        database=Path(database),
                        blockers=blockers,
                    )
                elif (
                    terminal_turn
                    and native_step.after.scalar.plays_remaining == 0
                ):
                    turn_end, next_state = _automatic_terminal_from_step(
                        native_step,
                        projection,
                        database=Path(database),
                        blockers=blockers,
                    )
            except Plan1NativeStepExecutorError as error:
                # Keep the reducer's typed stage preview visible for callers
                # diagnosing a blocked replay action.  This is a diagnostic
                # read of state-before, never a post-state fallback.
                blockers.append(
                    Plan1RuntimeBridgeBlocker(
                        "plan1-runtime-native-step-blocked",
                        f"{error.code}:{error.detail}".rstrip(":"),
                    )
                )
                if error.blockers:
                    blockers.extend(
                        Plan1RuntimeBridgeBlocker(
                            "plan1-runtime-native-step-blocker",
                            value,
                        )
                        for value in error.blockers
                    )
                try:
                    stage_action = preview_plan1_stage_action(
                        state,
                        slot_index,
                        program,
                        settings=effective_settings,
                    )
                    transition = stage_action.transition
                    blockers.extend(
                        Plan1RuntimeBridgeBlocker(
                            "plan1-runtime-stage-action-blocked",
                            f"{value.code}:{value.source_id}:{value.detail}".rstrip(":"),
                        )
                        for value in stage_action.blockers
                    )
                except (KeyError, OSError, TypeError, ValueError, IndexError) as preview_error:
                    blockers.append(
                        Plan1RuntimeBridgeBlocker(
                            "plan1-runtime-stage-preview-failed",
                            f"{type(preview_error).__name__}:{preview_error}",
                        )
                    )
            except (KeyError, OSError, TypeError, ValueError, IndexError) as error:
                blockers.append(
                    Plan1RuntimeBridgeBlocker(
                        "plan1-runtime-reducer-invocation-failed",
                        f"{type(error).__name__}:{error}",
                    )
                )

    return Plan1RuntimeTransitionProjection(
        projection=projection,
        action=action,
        slot_index=slot_index,
        card=card,
        program=program,
        stage_action=stage_action,
        transition=transition,
        next_state=next_state,
        step=step,
        blockers=_dedupe(blockers),
        native_step=native_step,
        drink=drink,
        drink_application=drink_application,
        turn_end=turn_end,
    )


def project_plan1_runtime_transition(
    payload: Mapping[str, Any] | LocalSaveExamState,
    action: LeaderboardReplayAction,
    *,
    stage_label: str = "stage",
    database: Path = DEFAULT_DATABASE,
    settings: Plan1NativeSettings | None = None,
    generated_card_identity: object | None = None,
    generated_identity: object | None = None,
    runtime_action_order: int | None = None,
) -> Plan1RuntimeTransitionProjection:
    """Convenience call for the first state_before/action vertical slice."""

    projection = project_plan1_runtime_state(payload, stage_label=stage_label)
    return apply_plan1_runtime_action(
        projection,
        action,
        database=database,
        settings=settings,
        generated_card_identity=generated_card_identity,
        generated_identity=generated_identity,
        runtime_action_order=runtime_action_order,
    )


__all__ = [
    "Plan1RuntimeBridgeBlocker",
    "Plan1RuntimeDrinkApplication",
    "Plan1RuntimeStateProjection",
    "Plan1RuntimeTurnEnd",
    "Plan1RuntimeTransitionProjection",
    "apply_plan1_runtime_action",
    "project_plan1_runtime_state",
    "project_plan1_runtime_transition",
]
