"""Exact bounded N.I.A. weekly-action projections for offline rollouts.

The functions in this module mutate only values owned by pinned Master rows.
Server-rolled business details and reward cards are explicit scenario inputs;
the static reward-set metadata is used to validate their route, never to
invent candidate membership.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3

from .master_db import DEFAULT_DATABASE
from .initial_regular_supply_scenario_adapter import (
    InitialRegularSupplyScenario,
    InitialRegularSupplyScenarioPause,
    adapt_initial_regular_supply_scenario,
)
from .initial_regular_nia_self_lesson_scenario import (
    InitialRegularNiaSelfLessonScenario,
    NiaSelfLessonEndScenario,
    NiaSelfLessonPostState,
    NiaSelfLessonScenarioAuthority,
    NiaSelfLessonStartScenario,
    adapt_initial_regular_nia_self_lesson_scenario,
)
from .initial_regular_nia_interval_scenario import (
    InitialRegularNiaIntervalScenario,
    NIA_INTERVAL_ACTION_ID,
    adapt_initial_regular_nia_interval_scenario,
)
from .initial_regular_nia_event_business_scenario import (
    InitialRegularNiaEventBusinessScenario,
    adapt_initial_regular_nia_event_business_scenario,
    load_nia_event_business_master_choice,
)
from .initial_regular_nia_business_scenario import (
    InitialRegularNiaBusinessScenario,
    adapt_initial_regular_nia_business_scenario,
    load_nia_business_master_selection,
)
from .initial_regular_nia_customize_scenario import (
    InitialRegularNiaCustomizeScenario,
    adapt_initial_regular_nia_customize_scenario,
)
from .initial_regular_nia_refresh_scenario import (
    InitialRegularNiaRefreshScenario,
    NIA_REFRESH_ACTION_ID,
    adapt_initial_regular_nia_refresh_scenario,
)
from .initial_regular_rollout import INITIAL_REGULAR_PRODUCE_ID
from .nia_reward_catalog import resolve_business_reward
from .nia_static_inventory import NiaStaticInventory
from .plan3_engine import load_plan3_card
from .produce_rollout import (
    AttributeValues,
    ChanceBranch,
    ExternalKind,
    ExternalRequest,
    ProduceRolloutState,
    ProduceStatePatch,
    RewardOffer,
    WeeklyActionOutcome,
)
from .route_calendar import EVENT_BUSINESS


SELF_LESSON = "self_lesson"
BUSINESS = "business"
OUTING = "outing"
INTERVAL = NIA_INTERVAL_ACTION_ID
REFRESH = NIA_REFRESH_ACTION_ID
SPECIAL_GUIDANCE = "special_guidance"
CARE_PACKAGE = "care_package"
SUPPLY = "supply"

_ATTRIBUTE_EFFECTS = {
    "ProduceEffectType_VocalAddition": "vocal",
    "ProduceEffectType_DanceAddition": "dance",
    "ProduceEffectType_VisualAddition": "visual",
}
_STAMINA_RECOVER = "ProduceEffectType_StaminaRecoverFix"
_PRODUCE_POINT_ADD = "ProduceEffectType_ProducePointAddition"
_VOTE_COUNT_ADD = "ProduceEffectType_VoteCountAddition"
_REWARD_SET = "ProduceEffectType_ProduceRewardSet"


class NiaOuterActionRuntimeError(ValueError):
    """Raised when an input crosses the proven weekly-action boundary."""


@dataclass(frozen=True, slots=True)
class NiaSelfLessonScenario:
    phase: int
    attribute: str
    is_sp: bool
    branch_id: str

    def __post_init__(self) -> None:
        if self.phase not in (1, 2, 3):
            raise ValueError("self-lesson phase must be 1, 2, or 3")
        if self.attribute not in {"vocal", "dance", "visual"}:
            raise ValueError("self-lesson attribute must be vocal/dance/visual")
        if not isinstance(self.is_sp, bool) or not self.branch_id:
            raise ValueError("self-lesson SP flag/branch identity is invalid")


@dataclass(frozen=True, slots=True)
class NiaBusinessScenario:
    detail_id: str
    suggestion_id: str
    reward_offer: RewardOffer
    branch_id: str

    def __post_init__(self) -> None:
        if not self.detail_id or not self.suggestion_id or not self.branch_id:
            raise ValueError("business scenario identity is incomplete")
        if not isinstance(self.reward_offer, RewardOffer):
            raise TypeError("business reward_offer must be typed")


@dataclass(frozen=True, slots=True)
class NiaCarePackageScenario:
    present_scenario: InitialRegularSupplyScenario
    branch_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.present_scenario, InitialRegularSupplyScenario):
            raise TypeError("present_scenario must be InitialRegularSupplyScenario")
        if not self.branch_id:
            raise ValueError("care package branch_id is required")


@dataclass(frozen=True, slots=True)
class NiaResolvedWeeklyAction:
    outcome: WeeklyActionOutcome
    action_id: str
    master_row_ids: tuple[str, ...]
    vote_count_delta: int = 0
    reward_offer: RewardOffer | None = None
    nominal_attribute_gain: int = 0
    stamina_delta: int = 0
    produce_point_delta: int = 0
    server_resolved_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, WeeklyActionOutcome):
            raise TypeError("outcome must be WeeklyActionOutcome")
        if not self.action_id or any(not value for value in self.master_row_ids):
            raise ValueError("weekly action identity/evidence is invalid")
        if self.vote_count_delta < 0 or self.nominal_attribute_gain < 0:
            raise ValueError("NIA weekly gains cannot be negative")
        if any(not value for value in self.server_resolved_fields):
            raise ValueError("server-resolved field labels cannot be empty")


def _check_request(
    state: ProduceRolloutState,
    request: ExternalRequest,
    action_id: str,
) -> None:
    if not isinstance(state, ProduceRolloutState):
        raise TypeError("state must be ProduceRolloutState")
    if not isinstance(request, ExternalRequest):
        raise TypeError("request must be ExternalRequest")
    if (
        request.kind is not ExternalKind.WEEKLY_ACTION_OUTCOME
        or request.week != state.week
        or request.action_id != action_id
        or state.pending != request
    ):
        raise NiaOuterActionRuntimeError(
            f"weekly request does not match {action_id} at week {state.week}"
        )


def _branch(branch_id: str, *, was_sp: bool | None = None) -> ChanceBranch:
    return ChanceBranch(branch_id, probability=1.0, was_sp=was_sp)


def _attributes_with_gain(
    attributes: AttributeValues,
    attribute: str,
    gain: int,
    *,
    cap: int,
) -> AttributeValues:
    values = {
        "vocal": attributes.vocal,
        "dance": attributes.dance,
        "visual": attributes.visual,
    }
    values[attribute] = min(cap, values[attribute] + gain)
    return AttributeValues(**values)


def _self_lesson_row(
    state: ProduceRolloutState,
    scenario: NiaSelfLessonScenario,
    inventory: NiaStaticInventory,
):
    suffix = "sp" if scenario.is_sp else "normal"
    master_produce_id = state.mode_id.replace("-", "_")
    row_id = f"self_lesson-{master_produce_id}-{scenario.phase:02d}-{suffix}"
    matches = tuple(row for row in inventory.lessons if row.id == row_id)
    if len(matches) != 1:
        raise NiaOuterActionRuntimeError(
            f"self-lesson Master row must resolve exactly once: {row_id}"
        )
    return matches[0]


def build_nia_self_lesson_response_scenario(
    state: ProduceRolloutState,
    scenario: NiaSelfLessonScenario,
    inventory: NiaStaticInventory,
) -> InitialRegularNiaSelfLessonScenario:
    """Bridge the legacy v1 route row to the typed Start/End boundary.

    This builder is intentionally restricted to the built-in fixed route's
    Master-only semantics.  Captured callers should construct an
    :class:`InitialRegularNiaSelfLessonScenario` directly and pass it as
    ``response_scenario`` to :func:`resolve_nia_self_lesson`.
    """

    if not isinstance(state, ProduceRolloutState):
        raise TypeError("state must be ProduceRolloutState")
    if not isinstance(scenario, NiaSelfLessonScenario):
        raise TypeError("scenario must be NiaSelfLessonScenario")
    if not isinstance(inventory, NiaStaticInventory):
        raise TypeError("inventory must be NiaStaticInventory")
    row = _self_lesson_row(state, scenario, inventory)
    if state.stamina < row.stamina:
        raise NiaOuterActionRuntimeError(
            f"self-lesson stamina is insufficient: {state.stamina}<{row.stamina}"
        )
    stamina_after = state.stamina - row.stamina
    attributes_after = _attributes_with_gain(
        state.attributes,
        scenario.attribute,
        row.parameter,
        cap=inventory.parameter_growth_limit,
    )
    return InitialRegularNiaSelfLessonScenario(
        week=state.week,
        master_row_id=row.id,
        selected_attribute=scenario.attribute,
        branch=_branch(scenario.branch_id, was_sp=scenario.is_sp),
        start=NiaSelfLessonStartScenario(
            before_stamina=state.stamina,
            after_stamina=stamina_after,
        ),
        end=NiaSelfLessonEndScenario(),
        post_state=NiaSelfLessonPostState.from_outer_state(
            state,
            stamina=stamina_after,
            attributes=attributes_after,
        ),
        authority=(
            NiaSelfLessonScenarioAuthority.CALLER_AUTHORED_SYNTHETIC_MASTER_ONLY
        ),
        runtime_ref=(
            f"{scenario.branch_id}:outer-state-plus-produce-step-self-lesson-master"
        ),
    )


def resolve_nia_self_lesson(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: NiaSelfLessonScenario,
    inventory: NiaStaticInventory,
    *,
    response_scenario: InitialRegularNiaSelfLessonScenario | None = None,
) -> NiaResolvedWeeklyAction:
    """Resolve one SelfLesson exclusively through its typed Start/End wire.

    The legacy ``scenario`` remains the version-1 route/JSON selector.  When
    ``response_scenario`` is absent it is bridged to an explicit synthetic
    Master-only response.  A captured caller can replace that response while
    retaining the stable route schema.
    """

    _check_request(state, request, SELF_LESSON)
    if not isinstance(scenario, NiaSelfLessonScenario):
        raise TypeError("scenario must be NiaSelfLessonScenario")
    if not isinstance(inventory, NiaStaticInventory):
        raise TypeError("inventory must be NiaStaticInventory")
    row = _self_lesson_row(state, scenario, inventory)
    typed = response_scenario or build_nia_self_lesson_response_scenario(
        state,
        scenario,
        inventory,
    )
    if not isinstance(typed, InitialRegularNiaSelfLessonScenario):
        raise TypeError(
            "response_scenario must be InitialRegularNiaSelfLessonScenario or None"
        )
    mismatches = []
    if typed.master_row_id != row.id:
        mismatches.append(
            f"master_row_id expected={row.id} actual={typed.master_row_id}"
        )
    if typed.selected_attribute != scenario.attribute:
        mismatches.append(
            "selected_attribute "
            f"expected={scenario.attribute} actual={typed.selected_attribute}"
        )
    if typed.is_sp != scenario.is_sp:
        mismatches.append(
            f"is_sp expected={scenario.is_sp} actual={typed.is_sp}"
        )
    if typed.branch.branch_id != scenario.branch_id:
        mismatches.append(
            f"branch_id expected={scenario.branch_id} actual={typed.branch.branch_id}"
        )
    if mismatches:
        raise NiaOuterActionRuntimeError(
            "self-lesson typed response disagrees with v1 route selector: "
            + "; ".join(mismatches)
        )
    adapted = adapt_initial_regular_nia_self_lesson_scenario(
        state,
        request,
        typed,
        inventory,
    )
    if not isinstance(adapted, WeeklyActionOutcome):
        detail = ",".join(
            f"{value.code}:{value.field}:{value.detail}".rstrip(":")
            for value in adapted
        )
        raise NiaOuterActionRuntimeError(
            f"self-lesson typed response blocked: {detail}"
        )
    exact_stamina = adapted.patch.stamina
    if exact_stamina is None:
        raise AssertionError("typed SelfLesson post-state must include stamina")
    return NiaResolvedWeeklyAction(
        adapted,
        SELF_LESSON,
        (row.id,),
        nominal_attribute_gain=row.parameter,
        stamina_delta=exact_stamina - state.stamina,
        server_resolved_fields=(
            "selected_attribute",
            "sp_roll",
            "typed_self_lesson_start_response",
            "typed_self_lesson_end_response",
            "exact_post_state",
        ),
    )


def resolve_nia_outing(
    state: ProduceRolloutState,
    request: ExternalRequest,
    inventory: NiaStaticInventory,
    *,
    branch_id: str,
) -> NiaResolvedWeeklyAction:
    """Apply the N.I.A. ProduceSetting refresh recovery."""

    _check_request(state, request, OUTING)
    recovery = (
        state.max_stamina
        * inventory.setting.refresh_stamina_recovery_permille
        // 1000
    )
    stamina_after = min(state.max_stamina, state.stamina + recovery)
    outcome = WeeklyActionOutcome(
        request.request_id,
        _branch(branch_id),
        ProduceStatePatch(stamina=stamina_after),
    )
    return NiaResolvedWeeklyAction(
        outcome,
        OUTING,
        (inventory.produce_setting_id,),
        stamina_delta=stamina_after - state.stamina,
    )


def resolve_nia_interval(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularNiaIntervalScenario,
) -> NiaResolvedWeeklyAction:
    """Resolve an exact server-owned Interval lifecycle.

    Unlike the fixed scenario's Master-only ``outing`` projection, Interval
    retains the server product rows and ordered Start/Buy/Reroll/End response
    buckets.  Its complete post-state is applied by the typed adapter; the
    similarly named schedule RefreshStamina display value is never consumed.
    """

    _check_request(state, request, INTERVAL)
    if not isinstance(scenario, InitialRegularNiaIntervalScenario):
        raise TypeError("scenario must be InitialRegularNiaIntervalScenario")
    adapted = adapt_initial_regular_nia_interval_scenario(
        state,
        request,
        scenario,
    )
    if not isinstance(adapted, WeeklyActionOutcome):
        detail = ",".join(
            f"{value.code}:{value.field}:{value.detail}".rstrip(":")
            for value in adapted
        )
        raise NiaOuterActionRuntimeError(
            f"Interval typed response blocked: {detail}"
        )
    return NiaResolvedWeeklyAction(
        adapted,
        INTERVAL,
        (
            "android:ProduceStepIntervalStart",
            "android:ProduceStepIntervalBuy/Reroll",
            "android:ProduceStepIntervalEnd",
        ),
        stamina_delta=scenario.post_state.stamina - state.stamina,
        produce_point_delta=(
            scenario.post_state.produce_points - state.produce_points
        ),
        server_resolved_fields=(
            "UserProduceProgressInterval products",
            "ordered Interval Start/Buy/Reroll/End responses",
            "exact post-state",
        ),
    )


def resolve_nia_event_business(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularNiaEventBusinessScenario,
    *,
    database: Path = DEFAULT_DATABASE,
) -> NiaResolvedWeeklyAction:
    """Resolve one exact generic event response reached from EventBusiness.

    Detail selection and response success remain caller/server facts.  The
    typed adapter applies only the complete response post-state, so the
    Master detail/base/outcome buckets are never flattened into an invented
    client execution order and no second reward request is opened.
    """

    _check_request(state, request, EVENT_BUSINESS)
    if not isinstance(scenario, InitialRegularNiaEventBusinessScenario):
        raise TypeError(
            "scenario must be InitialRegularNiaEventBusinessScenario"
        )
    database = Path(database)
    adapted = adapt_initial_regular_nia_event_business_scenario(
        state,
        request,
        scenario,
        database=database,
    )
    if not isinstance(adapted, WeeklyActionOutcome):
        detail = ",".join(
            f"{value.code}:{value.field}:{value.detail}".rstrip(":")
            for value in adapted
        )
        raise NiaOuterActionRuntimeError(
            f"EventBusiness typed response blocked: {detail}"
        )
    master = load_nia_event_business_master_choice(
        scenario.detail_id,
        scenario.suggestion_id,
        actual_success=scenario.response.success,
        database=database,
    )
    return NiaResolvedWeeklyAction(
        adapted,
        EVENT_BUSINESS,
        (master.detail_id, master.suggestion_id),
        vote_count_delta=(
            scenario.post_state.vote_count - scenario.vote_count_before
        ),
        stamina_delta=scenario.post_state.stamina - state.stamina,
        produce_point_delta=(
            scenario.post_state.produce_points - state.produce_points
        ),
        server_resolved_fields=(
            "UserProduceProgressEvent detail/suggestion identity",
            "ProduceStepEventResponse.Success",
            "ordered ProduceStepEventResponse.MemoryRewardResults",
            "ordered ProduceStepEventResponse.EffectResults",
            "exact CommonResponse post-state",
        ),
    )


def resolve_nia_refresh(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularNiaRefreshScenario,
) -> NiaResolvedWeeklyAction:
    """Resolve the exact Refresh(14) response, distinct from fixed outing."""

    _check_request(state, request, REFRESH)
    if not isinstance(scenario, InitialRegularNiaRefreshScenario):
        raise TypeError("scenario must be InitialRegularNiaRefreshScenario")
    adapted = adapt_initial_regular_nia_refresh_scenario(
        state,
        request,
        scenario,
    )
    if not isinstance(adapted, WeeklyActionOutcome):
        detail = ",".join(
            f"{value.code}:{value.field}:{value.detail}".rstrip(":")
            for value in adapted
        )
        raise NiaOuterActionRuntimeError(
            f"Refresh typed response blocked: {detail}"
        )
    return NiaResolvedWeeklyAction(
        adapted,
        REFRESH,
        (
            "android:ProduceStepRefresh",
            "pc-master:ProduceSetting.refreshStaminaRecoveryPermil",
        ),
        stamina_delta=scenario.response_stamina_delta,
        produce_point_delta=(
            scenario.post_state.produce_points - state.produce_points
        ),
        server_resolved_fields=(
            "ProduceStepRefreshResponse.BeforeStamina/AfterStamina",
            "ordered ProduceStepRefreshResponse.EffectResults",
            "exact CommonResponse post-state",
            "ProduceSchedule.RefreshStamina delta observation",
        ),
    )


def resolve_nia_special_guidance_skip(
    state: ProduceRolloutState,
    request: ExternalRequest,
    inventory: NiaStaticInventory,
    *,
    branch_id: str | None = None,
    response_scenario: InitialRegularNiaCustomizeScenario | None = None,
) -> NiaResolvedWeeklyAction:
    """End Customize without selecting a card.

    A typed response applies its exact End CommonResponse state.  Omitting it
    preserves the legacy fixed-route synthetic recovery calculation.
    """

    _check_request(state, request, SPECIAL_GUIDANCE)
    if response_scenario is not None:
        adapted = adapt_initial_regular_nia_customize_scenario(
            state,
            request,
            response_scenario,
            inventory,
        )
        if not isinstance(adapted, WeeklyActionOutcome):
            detail = ",".join(
                f"{value.code}:{value.field}:{value.detail}".rstrip(":")
                for value in adapted
            )
            raise NiaOuterActionRuntimeError(
                f"Customize typed response blocked: {detail}"
            )
        return NiaResolvedWeeklyAction(
            adapted,
            SPECIAL_GUIDANCE,
            (
                response_scenario.master_setting.produce_setting_id,
                "android:ProduceStepCustomizeStart",
                "android:ProduceStepCustomizeEnd",
            ),
            stamina_delta=response_scenario.post_state.stamina - state.stamina,
            produce_point_delta=(
                response_scenario.post_state.produce_points - state.produce_points
            ),
            server_resolved_fields=(
                "ordered ProduceStepCustomizeStartResponse.EffectResults",
                "Customize Start CommonResponse post-state",
                "explicit zero Select requests",
                "ordered ProduceStepCustomizeEndResponse.EffectResults",
                "exact Customize End CommonResponse post-state",
            ),
        )
    if not branch_id:
        raise TypeError("legacy Customize skip requires branch_id")
    recovery = (
        state.max_stamina
        * inventory.setting.step_skip_stamina_recovery_permille
        // 1000
    )
    stamina_after = min(state.max_stamina, state.stamina + recovery)
    outcome = WeeklyActionOutcome(
        request.request_id,
        _branch(branch_id),
        ProduceStatePatch(stamina=stamina_after),
    )
    return NiaResolvedWeeklyAction(
        outcome,
        SPECIAL_GUIDANCE,
        (inventory.produce_setting_id, "android:ProduceStepCustomizeEndRequest"),
        stamina_delta=stamina_after - state.stamina,
        server_resolved_fields=(
            "legacy synthetic v1 customize_end_without_select recovery",
        ),
    )


def resolve_nia_care_package(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: NiaCarePackageScenario,
) -> NiaResolvedWeeklyAction:
    """Resolve the native Present lifecycle behind one N.I.A. care package."""

    _check_request(state, request, CARE_PACKAGE)
    if not isinstance(scenario, NiaCarePackageScenario):
        raise TypeError("scenario must be NiaCarePackageScenario")
    # The shared adapter names Initial Regular's UI action ``supply``.  Both
    # actions enter ProduceStepType_Present and use the same Start/Receive/End
    # API.  Rebind only that domain label; request ID, state, Present rows and
    # exact post-state all remain the caller's captured evidence.
    supply_request = replace(request, action_id=SUPPLY, adapter_ref=None)
    supply_state = replace(
        state,
        mode_id=INITIAL_REGULAR_PRODUCE_ID,
        pending=supply_request,
    )
    adapted = adapt_initial_regular_supply_scenario(
        supply_state,
        supply_request,
        scenario.present_scenario,
        branch=_branch(scenario.branch_id),
    )
    if isinstance(adapted, InitialRegularSupplyScenarioPause):
        detail = ",".join(
            f"{value.code}:{value.field}:{value.detail}".rstrip(":")
            for value in adapted.issues
        )
        raise NiaOuterActionRuntimeError(
            f"care package Present scenario is unresolved: {detail}"
        )
    deck = state.deck if adapted.patch.deck is None else adapted.patch.deck
    if any(len(entry.instance_ids) != entry.count for entry in deck):
        raise NiaOuterActionRuntimeError(
            "care package post deck must preserve every response card GUID"
        )
    stamina = state.stamina if adapted.patch.stamina is None else adapted.patch.stamina
    points = (
        state.produce_points
        if adapted.patch.produce_points is None
        else adapted.patch.produce_points
    )
    return NiaResolvedWeeklyAction(
        adapted,
        CARE_PACKAGE,
        (
            "android:ProduceStepPresentStart",
            "android:ProduceStepPresentReceive",
            "android:ProduceStepPresentEnd",
        ),
        stamina_delta=stamina - state.stamina,
        produce_point_delta=points - state.produce_points,
        server_resolved_fields=(
            "UserProduceProgressPresent",
            "PresentReceive",
            "PresentEnd",
            "exact_post_state",
        ),
    )


def _json_ids(raw: object, label: str) -> tuple[str, ...]:
    try:
        values = json.loads(str(raw))
    except json.JSONDecodeError as error:
        raise NiaOuterActionRuntimeError(f"{label} is invalid JSON") from error
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value for value in values
    ):
        raise NiaOuterActionRuntimeError(f"{label} must contain IDs")
    return tuple(values)


def _exact_effect(
    connection: sqlite3.Connection,
    effect_id: str,
) -> tuple[str, int]:
    row = connection.execute(
        "SELECT effect_type, effect_value_min, effect_value_max "
        "FROM produce_effect WHERE id = ?",
        (effect_id,),
    ).fetchone()
    if row is None:
        raise NiaOuterActionRuntimeError(f"unknown ProduceEffect: {effect_id}")
    minimum, maximum = int(row[1]), int(row[2])
    if minimum != maximum:
        raise NiaOuterActionRuntimeError(
            f"ProduceEffect is server-ranged rather than exact: {effect_id}"
        )
    return str(row[0]), minimum


def resolve_nia_business(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: NiaBusinessScenario | InitialRegularNiaBusinessScenario,
    inventory: NiaStaticInventory | None = None,
    *,
    database: Path = DEFAULT_DATABASE,
) -> NiaResolvedWeeklyAction:
    """Resolve exact Business responses or the legacy synthetic v1 row.

    ``InitialRegularNiaBusinessScenario`` follows Android's dedicated
    Start/Select lifecycle and applies only its complete response post-state.
    ``NiaBusinessScenario`` remains the checked-in v1 fallback and retains
    its older Master-effect/reward-offer synthesis when no response overlay
    was supplied.
    """

    _check_request(state, request, BUSINESS)
    if isinstance(scenario, InitialRegularNiaBusinessScenario):
        database = Path(database)
        adapted = adapt_initial_regular_nia_business_scenario(
            state,
            request,
            scenario,
            database=database,
        )
        if not isinstance(adapted, WeeklyActionOutcome):
            detail = ",".join(
                f"{value.code}:{value.field}:{value.detail}".rstrip(":")
                for value in adapted
            )
            raise NiaOuterActionRuntimeError(
                f"Business typed response blocked: {detail}"
            )
        master = load_nia_business_master_selection(
            scenario.progress,
            scenario.suggestion_id,
            was_excellent=scenario.was_excellent,
            produce_id=scenario.produce_id,
            database=database,
        )
        attribute_gain = sum(
            max(0, getattr(scenario.post_state.attributes, name) - getattr(state.attributes, name))
            for name in ("vocal", "dance", "visual")
        )
        return NiaResolvedWeeklyAction(
            adapted,
            BUSINESS,
            (master.selected_detail_id, master.suggestion_id),
            vote_count_delta=(
                scenario.post_state.vote_count - scenario.vote_count_before
            ),
            nominal_attribute_gain=attribute_gain,
            stamina_delta=scenario.post_state.stamina - state.stamina,
            produce_point_delta=(
                scenario.post_state.produce_points - state.produce_points
            ),
            server_resolved_fields=(
                "UserProduceProgressBusiness normal/excellent detail identity",
                "ProduceStepBusinessStartResponse.EffectResults/CommonResponse",
                "server-selected normal/excellent detail",
                "ProduceStepBusinessSelectResponse.ConsumptionResults",
                "ProduceStepBusinessSelectResponse.EffectResults/ProvidedRewards",
                "exact Select CommonResponse post-state",
            ),
        )
    if not isinstance(scenario, NiaBusinessScenario):
        raise TypeError(
            "scenario must be NiaBusinessScenario or "
            "InitialRegularNiaBusinessScenario"
        )
    if not isinstance(inventory, NiaStaticInventory):
        raise TypeError("legacy Business scenario requires NiaStaticInventory")
    association = resolve_business_reward(
        scenario.detail_id,
        scenario.suggestion_id,
        database=Path(database),
    )
    if not association.ready or association.value is None:
        blockers = ",".join(
            f"{value.code}:{value.detail}" for value in association.blockers
        )
        raise NiaOuterActionRuntimeError(
            f"business detail/suggestion is not exact: {blockers}"
        )
    if association.value.source_produce_id != state.mode_id:
        raise NiaOuterActionRuntimeError("business suggestion belongs to another mode")
    # A concrete rolled card is caller/server evidence.  Validate only that
    # the selected card version is executable; candidate membership remains
    # unresolved in Master and is intentionally not inferred.
    load_plan3_card(
        scenario.reward_offer.card_id,
        scenario.reward_offer.upgrade,
        database=Path(database),
    )
    if scenario.reward_offer.received_card_instance_id is None:
        raise NiaOuterActionRuntimeError(
            "business reward must preserve its response-bound card instance ID"
        )

    connection = sqlite3.connect(Path(database))
    try:
        connection.row_factory = sqlite3.Row
        detail = connection.execute(
            "SELECT produce_effect_ids_json FROM step_event_detail WHERE id = ?",
            (scenario.detail_id,),
        ).fetchone()
        suggestion = connection.execute(
            "SELECT stamina, produce_point, produce_card_id, "
            "produce_effect_ids_json FROM step_event_suggestion WHERE id = ?",
            (scenario.suggestion_id,),
        ).fetchone()
        if detail is None or suggestion is None:
            raise NiaOuterActionRuntimeError("business Master row disappeared")
        if int(suggestion["stamina"]) != 0 or int(suggestion["produce_point"]) != 0:
            raise NiaOuterActionRuntimeError(
                "business direct suggestion fields need an ordered executor"
            )
        if str(suggestion["produce_card_id"]):
            raise NiaOuterActionRuntimeError(
                "business direct produce_card_id is outside reward-set flow"
            )
        detail_effects = _json_ids(
            detail["produce_effect_ids_json"],
            f"{scenario.detail_id}.effects",
        )
        suggestion_effects = _json_ids(
            suggestion["produce_effect_ids_json"],
            f"{scenario.suggestion_id}.effects",
        )
        attributes = state.attributes
        stamina = state.stamina
        points = state.produce_points
        vote_delta = 0
        nominal_attribute_gain = 0
        reward_sets = 0
        for effect_id in (*detail_effects, *suggestion_effects):
            effect_type, value = _exact_effect(connection, effect_id)
            attribute = _ATTRIBUTE_EFFECTS.get(effect_type)
            if attribute is not None:
                before = getattr(attributes, attribute)
                attributes = _attributes_with_gain(
                    attributes,
                    attribute,
                    value,
                    cap=inventory.parameter_growth_limit,
                )
                nominal_attribute_gain += value
                del before
            elif effect_type == _STAMINA_RECOVER:
                stamina = min(state.max_stamina, stamina + value)
            elif effect_type == _PRODUCE_POINT_ADD:
                points += value
            elif effect_type == _VOTE_COUNT_ADD:
                vote_delta += value
            elif effect_type == _REWARD_SET:
                reward_sets += 1
            else:
                raise NiaOuterActionRuntimeError(
                    f"unsupported business ProduceEffect: {effect_id}:{effect_type}"
                )
    finally:
        connection.close()
    if reward_sets != 1 or association.value.effect_id not in suggestion_effects:
        raise NiaOuterActionRuntimeError(
            "business scenario must contain exactly one validated card reward set"
        )
    outcome = WeeklyActionOutcome(
        request.request_id,
        _branch(scenario.branch_id),
        ProduceStatePatch(
            stamina=stamina,
            produce_points=points,
            attributes=attributes,
        ),
        reward_requested=True,
    )
    return NiaResolvedWeeklyAction(
        outcome,
        BUSINESS,
        (
            scenario.detail_id,
            scenario.suggestion_id,
            *detail_effects,
            *suggestion_effects,
        ),
        vote_count_delta=vote_delta,
        reward_offer=scenario.reward_offer,
        nominal_attribute_gain=nominal_attribute_gain,
        stamina_delta=stamina - state.stamina,
        produce_point_delta=points - state.produce_points,
        server_resolved_fields=(
            "business_detail",
            "business_suggestion",
            "reward_offer",
        ),
    )


__all__ = [
    "BUSINESS",
    "CARE_PACKAGE",
    "EVENT_BUSINESS",
    "INTERVAL",
    "NiaBusinessScenario",
    "NiaCarePackageScenario",
    "NiaOuterActionRuntimeError",
    "NiaResolvedWeeklyAction",
    "NiaSelfLessonScenario",
    "OUTING",
    "REFRESH",
    "SELF_LESSON",
    "SPECIAL_GUIDANCE",
    "build_nia_self_lesson_response_scenario",
    "resolve_nia_business",
    "resolve_nia_care_package",
    "resolve_nia_event_business",
    "resolve_nia_interval",
    "resolve_nia_outing",
    "resolve_nia_refresh",
    "resolve_nia_self_lesson",
    "resolve_nia_special_guidance_skip",
]
