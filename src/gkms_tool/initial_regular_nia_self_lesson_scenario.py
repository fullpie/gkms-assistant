"""Typed N.I.A. ``SelfLesson`` response boundary for offline outer rollouts.

``ProduceStepSelfLesson`` is not an exam-shaped lesson.  Android v3.2.3
exposes a Start request containing only ``DeviceProduceUuid`` and an End
request containing only ``DeviceProduceUuid``.  The corresponding responses
carry Start ``BeforeStamina``/``AfterStamina``/ordered ``EffectResults`` and
End ordered ``RewardResults``/``EffectResults``.  In contrast,
``ProduceStepLessonEndRequest`` carries an ``ExamEndResult`` and
``TurnEndLogs`` (the utility builds those from stamina, parameters, cards,
items, drinks and turn logs).

Consequently this module deliberately exposes only a weekly-action adapter.
It preserves response buckets independently and applies a caller-authoritative
complete outer post-state.  It never manufactures a card deck, RNG stream or
turn log and never flattens the Start/End response buckets into an inferred
execution order.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Mapping, TypeAlias

from .initial_regular_event_scenario import (
    ProduceEffectResultTrace,
    ProduceRewardResultSnapshot,
    validate_effect_result_chain,
)
from .initial_regular_inner_protocol import (
    InitialRegularInnerStageIssue,
    InitialRegularInnerStageRequest,
)
from .nia_static_inventory import NiaStaticInventory, NiaStaticLessonStage
from .produce_rollout import (
    AttributeValues,
    ChanceBranch,
    DeckEntry,
    ExternalKind,
    ExternalRequest,
    ProduceRolloutState,
    ProduceStatePatch,
    RolloutPhase,
    WeeklyActionOutcome,
)


NIA_SELF_LESSON_ACTION_ID: Final = "self_lesson"
NIA_SELF_LESSON_NO_INNER_EXAM: Final = "nia-self-lesson-has-no-inner-exam"

# Exact top-level protobuf fields in the Android v3.2.3 generated messages.
ANDROID_SELF_LESSON_START_REQUEST_FIELDS: Final = ("device_produce_uuid",)
ANDROID_SELF_LESSON_END_REQUEST_FIELDS: Final = ("device_produce_uuid",)
ANDROID_SELF_LESSON_START_RESPONSE_FIELDS: Final = (
    "before_stamina",
    "after_stamina",
    "effect_results",
)
ANDROID_SELF_LESSON_END_RESPONSE_FIELDS: Final = (
    "reward_results",
    "effect_results",
)
ANDROID_LESSON_END_REQUEST_FIELDS: Final = (
    "device_produce_uuid",
    "exam_end_result",
    "turn_end_logs",
)
ANDROID_LESSON_END_UTILITY_INPUTS: Final = (
    "stamina",
    "produce_point",
    "parameter_value",
    "clear_threshold",
    "cards",
    "drink_ids",
    "items",
    "trigger_mission_achieve_list",
    "turn_logs",
)

# ``Master/ProduceStepSelfLesson.yaml`` rows reachable in produce-004.  The
# tuple shape is (id, progressLevel, stamina, parameter), in Master order.
NIA_PRO_SELF_LESSON_MASTER_ROWS: Final = (
    ("self_lesson-produce_004-01-normal", 1, 6, 80),
    ("self_lesson-produce_004-01-sp", 1, 8, 100),
    ("self_lesson-produce_004-02-normal", 1, 6, 100),
    ("self_lesson-produce_004-02-sp", 1, 8, 120),
    ("self_lesson-produce_004-03-normal", 1, 6, 120),
    ("self_lesson-produce_004-03-sp", 1, 8, 150),
)


def _plain_int(value: object, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be an object")
    return value


def _exact(value: Mapping[str, object], fields: set[str], field: str) -> None:
    actual = set(value)
    if actual != fields:
        missing = sorted(fields - actual)
        extra = sorted(actual - fields)
        detail: list[str] = []
        if missing:
            detail.append(f"missing={','.join(missing)}")
        if extra:
            detail.append(f"extra={','.join(extra)}")
        raise ValueError(f"{field} keys mismatch ({';'.join(detail)})")


def _list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{field} must be a list")
    return value


def _text(value: object, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be text")
    if not empty and not value:
        raise ValueError(f"{field} must be non-empty")
    return value


def _strict_branch(value: object, field: str) -> ChanceBranch:
    payload = _mapping(value, field)
    _exact(
        payload,
        {"branch_id", "probability", "rng_token", "event_id", "was_sp"},
        field,
    )
    return ChanceBranch.from_dict(payload)


def _strict_attributes(value: object, field: str) -> AttributeValues:
    payload = _mapping(value, field)
    _exact(payload, {"vocal", "dance", "visual"}, field)
    return AttributeValues.from_dict(payload)


def _strict_deck_entry(value: object, field: str) -> DeckEntry:
    payload = _mapping(value, field)
    _exact(payload, {"card_id", "upgrade", "count", "instance_ids"}, field)
    return DeckEntry.from_dict(payload)


class NiaSelfLessonScenarioAuthority(StrEnum):
    """Authority of the runtime-only Start/End response values."""

    CALLER_RESOLVED_SERVER_RESPONSE = "caller-resolved-server-response"
    CALLER_AUTHORED_SYNTHETIC_MASTER_ONLY = (
        "caller-authored-synthetic-master-only"
    )


@dataclass(frozen=True, slots=True)
class NiaSelfLessonStartScenario:
    """Exact fields from one SelfLesson Start response.

    ``effect_results`` retains response order.  Only adjacent entries inside
    this bucket are chain-validated; no relationship to an End bucket is
    inferred.
    """

    before_stamina: int
    after_stamina: int
    effect_results: tuple[ProduceEffectResultTrace, ...] = ()

    def __post_init__(self) -> None:
        _plain_int(self.before_stamina, "SelfLesson Start before_stamina")
        _plain_int(self.after_stamina, "SelfLesson Start after_stamina")
        values = tuple(self.effect_results)
        if not all(isinstance(value, ProduceEffectResultTrace) for value in values):
            raise TypeError("SelfLesson Start effects must be typed result traces")
        validate_effect_result_chain(values)
        object.__setattr__(self, "effect_results", values)

    def to_dict(self) -> dict[str, object]:
        return {
            "before_stamina": self.before_stamina,
            "after_stamina": self.after_stamina,
            "effect_results": [value.to_dict() for value in self.effect_results],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaSelfLessonStartScenario":
        value = _mapping(payload, "SelfLesson Start")
        _exact(value, {"before_stamina", "after_stamina", "effect_results"}, "SelfLesson Start")
        return cls(
            before_stamina=_plain_int(value["before_stamina"], "SelfLesson Start before_stamina"),
            after_stamina=_plain_int(value["after_stamina"], "SelfLesson Start after_stamina"),
            effect_results=tuple(
                ProduceEffectResultTrace.from_dict(_mapping(item, "SelfLesson Start effect"))
                for item in _list(value["effect_results"], "SelfLesson Start effect_results")
            ),
        )


@dataclass(frozen=True, slots=True)
class NiaSelfLessonEndScenario:
    """Exact ordered fields from one SelfLesson End response."""

    reward_results: tuple[ProduceRewardResultSnapshot, ...] = ()
    effect_results: tuple[ProduceEffectResultTrace, ...] = ()

    def __post_init__(self) -> None:
        rewards = tuple(self.reward_results)
        effects = tuple(self.effect_results)
        if not all(isinstance(value, ProduceRewardResultSnapshot) for value in rewards):
            raise TypeError("SelfLesson End rewards must be typed reward snapshots")
        if not all(isinstance(value, ProduceEffectResultTrace) for value in effects):
            raise TypeError("SelfLesson End effects must be typed result traces")
        validate_effect_result_chain(effects)
        object.__setattr__(self, "reward_results", rewards)
        object.__setattr__(self, "effect_results", effects)

    def to_dict(self) -> dict[str, object]:
        return {
            "reward_results": [value.to_dict() for value in self.reward_results],
            "effect_results": [value.to_dict() for value in self.effect_results],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaSelfLessonEndScenario":
        value = _mapping(payload, "SelfLesson End")
        _exact(value, {"reward_results", "effect_results"}, "SelfLesson End")
        return cls(
            reward_results=tuple(
                ProduceRewardResultSnapshot.from_dict(_mapping(item, "SelfLesson End reward"))
                for item in _list(value["reward_results"], "SelfLesson End reward_results")
            ),
            effect_results=tuple(
                ProduceEffectResultTrace.from_dict(_mapping(item, "SelfLesson End effect"))
                for item in _list(value["effect_results"], "SelfLesson End effect_results")
            ),
        )


@dataclass(frozen=True, slots=True)
class NiaSelfLessonPostState:
    """Complete caller-authoritative outer state after SelfLesson End.

    The End reward/effect records are evidence, not reducer instructions in
    this bounded slice.  Supplying all persistent outer fields prevents a
    partial response from silently losing an inventory or card mutation.
    """

    stamina: int
    max_stamina: int
    produce_points: int
    attributes: AttributeValues
    deck: tuple[DeckEntry, ...]
    item_session_refs: tuple[str, ...] = ()
    drink_session_refs: tuple[str, ...] = ()
    passive_session_refs: tuple[str, ...] = ()
    excluded_reward_card_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _plain_int(self.stamina, "SelfLesson post stamina")
        _plain_int(self.max_stamina, "SelfLesson post max_stamina", minimum=1)
        _plain_int(self.produce_points, "SelfLesson post produce_points")
        if not isinstance(self.attributes, AttributeValues):
            raise TypeError("SelfLesson post attributes must be AttributeValues")
        cards = tuple(self.deck)
        if not all(isinstance(value, DeckEntry) for value in cards):
            raise TypeError("SelfLesson post deck must contain DeckEntry values")
        object.__setattr__(self, "deck", cards)
        for field in (
            "item_session_refs",
            "drink_session_refs",
            "passive_session_refs",
            "excluded_reward_card_ids",
        ):
            object.__setattr__(self, field, tuple(getattr(self, field)))
        # Reuse the common patch validator, then enforce the complete-state
        # stamina relationship which a partial patch cannot check by itself.
        self.to_patch()
        if self.stamina > self.max_stamina:
            raise ValueError("SelfLesson post stamina exceeds max_stamina")

    @classmethod
    def from_outer_state(
        cls,
        state: ProduceRolloutState,
        *,
        stamina: int | None = None,
        attributes: AttributeValues | None = None,
    ) -> "NiaSelfLessonPostState":
        if not isinstance(state, ProduceRolloutState):
            raise TypeError("state must be ProduceRolloutState")
        return cls(
            stamina=state.stamina if stamina is None else stamina,
            max_stamina=state.max_stamina,
            produce_points=state.produce_points,
            attributes=state.attributes if attributes is None else attributes,
            deck=state.deck,
            item_session_refs=state.item_session_refs,
            drink_session_refs=state.drink_session_refs,
            passive_session_refs=state.passive_session_refs,
            excluded_reward_card_ids=state.excluded_reward_card_ids,
        )

    def to_patch(self) -> ProduceStatePatch:
        return ProduceStatePatch(
            stamina=self.stamina,
            max_stamina=self.max_stamina,
            produce_points=self.produce_points,
            attributes=self.attributes,
            deck=self.deck,
            item_session_refs=self.item_session_refs,
            drink_session_refs=self.drink_session_refs,
            passive_session_refs=self.passive_session_refs,
            excluded_reward_card_ids=self.excluded_reward_card_ids,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "stamina": self.stamina,
            "max_stamina": self.max_stamina,
            "produce_points": self.produce_points,
            "attributes": self.attributes.to_dict(),
            "deck": [value.to_dict() for value in self.deck],
            "item_session_refs": list(self.item_session_refs),
            "drink_session_refs": list(self.drink_session_refs),
            "passive_session_refs": list(self.passive_session_refs),
            "excluded_reward_card_ids": list(self.excluded_reward_card_ids),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NiaSelfLessonPostState":
        value = _mapping(payload, "SelfLesson post state")
        fields = {
            "stamina",
            "max_stamina",
            "produce_points",
            "attributes",
            "deck",
            "item_session_refs",
            "drink_session_refs",
            "passive_session_refs",
            "excluded_reward_card_ids",
        }
        _exact(value, fields, "SelfLesson post state")

        def _refs(name: str) -> tuple[str, ...]:
            return tuple(
                _text(item, f"SelfLesson post {name} item", empty=True)
                for item in _list(value[name], f"SelfLesson post {name}")
            )

        return cls(
            stamina=_plain_int(value["stamina"], "SelfLesson post stamina"),
            max_stamina=_plain_int(value["max_stamina"], "SelfLesson post max_stamina", minimum=1),
            produce_points=_plain_int(value["produce_points"], "SelfLesson post produce_points"),
            attributes=_strict_attributes(value["attributes"], "SelfLesson post attributes"),
            deck=tuple(
                _strict_deck_entry(item, "SelfLesson post deck entry")
                for item in _list(value["deck"], "SelfLesson post deck")
            ),
            item_session_refs=_refs("item_session_refs"),
            drink_session_refs=_refs("drink_session_refs"),
            passive_session_refs=_refs("passive_session_refs"),
            excluded_reward_card_ids=_refs("excluded_reward_card_ids"),
        )


@dataclass(frozen=True, slots=True)
class InitialRegularNiaSelfLessonScenario:
    """Caller-owned binding for one exact produce-004 SelfLesson lifecycle."""

    week: int
    master_row_id: str
    selected_attribute: str
    branch: ChanceBranch
    start: NiaSelfLessonStartScenario
    end: NiaSelfLessonEndScenario
    post_state: NiaSelfLessonPostState
    authority: NiaSelfLessonScenarioAuthority
    runtime_ref: str

    def __post_init__(self) -> None:
        _plain_int(self.week, "SelfLesson scenario week", minimum=1)
        if not isinstance(self.master_row_id, str) or not self.master_row_id:
            raise ValueError("SelfLesson scenario master_row_id is required")
        if self.selected_attribute not in {"vocal", "dance", "visual"}:
            raise ValueError("SelfLesson selected_attribute is invalid")
        if not isinstance(self.branch, ChanceBranch):
            raise TypeError("SelfLesson branch must be ChanceBranch")
        if not isinstance(self.start, NiaSelfLessonStartScenario):
            raise TypeError("SelfLesson start must be typed")
        if not isinstance(self.end, NiaSelfLessonEndScenario):
            raise TypeError("SelfLesson end must be typed")
        if not isinstance(self.post_state, NiaSelfLessonPostState):
            raise TypeError("SelfLesson post_state must be typed")
        authority = self.authority
        if isinstance(authority, str) and not isinstance(
            authority,
            NiaSelfLessonScenarioAuthority,
        ):
            try:
                authority = NiaSelfLessonScenarioAuthority(authority)
            except ValueError as error:
                raise ValueError("SelfLesson scenario authority is invalid") from error
            object.__setattr__(self, "authority", authority)
        if not isinstance(authority, NiaSelfLessonScenarioAuthority):
            raise TypeError("SelfLesson scenario authority must be typed")
        if not isinstance(self.runtime_ref, str) or not self.runtime_ref:
            raise ValueError("SelfLesson caller runtime_ref is required")
        if not (
            self.master_row_id.endswith("-normal")
            or self.master_row_id.endswith("-sp")
        ):
            raise ValueError("SelfLesson master row must identify normal or SP")

    @property
    def is_sp(self) -> bool:
        return self.master_row_id.endswith("-sp")

    @property
    def authentic_nia_local_save(self) -> bool:
        """This boundary never claims that a caller scenario is a LocalSave."""

        return False

    @property
    def has_inner_exam(self) -> bool:
        return False

    @property
    def inner_deck_authority(self) -> str:
        return "not-applicable-self-lesson-has-no-inner-exam"

    @property
    def inner_rng_authority(self) -> str:
        return "not-applicable-self-lesson-has-no-inner-exam"

    def to_dict(self) -> dict[str, object]:
        return {
            "week": self.week,
            "master_row_id": self.master_row_id,
            "selected_attribute": self.selected_attribute,
            "branch": self.branch.to_dict(),
            "start": self.start.to_dict(),
            "end": self.end.to_dict(),
            "post_state": self.post_state.to_dict(),
            "authority": self.authority.value,
            "runtime_ref": self.runtime_ref,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "InitialRegularNiaSelfLessonScenario":
        value = _mapping(payload, "SelfLesson scenario")
        _exact(
            value,
            {
                "week",
                "master_row_id",
                "selected_attribute",
                "branch",
                "start",
                "end",
                "post_state",
                "authority",
                "runtime_ref",
            },
            "SelfLesson scenario",
        )
        authority = _text(value["authority"], "SelfLesson scenario authority")
        try:
            authority_value = NiaSelfLessonScenarioAuthority(authority)
        except ValueError as error:
            raise ValueError("SelfLesson scenario authority is invalid") from error
        return cls(
            week=_plain_int(value["week"], "SelfLesson scenario week", minimum=1),
            master_row_id=_text(value["master_row_id"], "SelfLesson scenario master_row_id"),
            selected_attribute=_text(value["selected_attribute"], "SelfLesson selected_attribute"),
            branch=_strict_branch(value["branch"], "SelfLesson scenario branch"),
            start=NiaSelfLessonStartScenario.from_dict(_mapping(value["start"], "SelfLesson scenario start")),
            end=NiaSelfLessonEndScenario.from_dict(_mapping(value["end"], "SelfLesson scenario end")),
            post_state=NiaSelfLessonPostState.from_dict(_mapping(value["post_state"], "SelfLesson scenario post_state")),
            authority=authority_value,
            runtime_ref=_text(value["runtime_ref"], "SelfLesson scenario runtime_ref"),
        )


def _issue(
    code: str,
    field: str,
    detail: str = "",
) -> InitialRegularInnerStageIssue:
    return InitialRegularInnerStageIssue(code, field, detail)


def _dedupe(
    values: list[InitialRegularInnerStageIssue],
) -> tuple[InitialRegularInnerStageIssue, ...]:
    return tuple(dict.fromkeys(values))


def _master_shape(
    inventory: NiaStaticInventory,
) -> tuple[tuple[str, int, int, int], ...]:
    return tuple(
        (row.id, row.progress_level, row.stamina, row.parameter)
        for row in inventory.lessons
    )


def _selected_row(
    scenario: InitialRegularNiaSelfLessonScenario,
    inventory: NiaStaticInventory,
) -> NiaStaticLessonStage | None:
    matches = tuple(
        row for row in inventory.lessons if row.id == scenario.master_row_id
    )
    return matches[0] if len(matches) == 1 else None


def _attributes_with_gain(
    values: AttributeValues,
    attribute: str,
    gain: int,
    cap: int,
) -> AttributeValues:
    fields = {
        "vocal": values.vocal,
        "dance": values.dance,
        "visual": values.visual,
    }
    fields[attribute] = min(cap, fields[attribute] + gain)
    return AttributeValues(**fields)


def _synthetic_master_only_issues(
    state: ProduceRolloutState,
    scenario: InitialRegularNiaSelfLessonScenario,
    row: NiaStaticLessonStage,
    inventory: NiaStaticInventory,
) -> tuple[InitialRegularInnerStageIssue, ...]:
    issues: list[InitialRegularInnerStageIssue] = []
    if scenario.start.effect_results or scenario.end.effect_results:
        issues.append(
            _issue(
                "nia-self-lesson-synthetic-effects-not-supported",
                "scenario.start/end.effect_results",
                "synthetic-master-only accepts no unexecuted response effects",
            )
        )
    if scenario.end.reward_results:
        issues.append(
            _issue(
                "nia-self-lesson-synthetic-rewards-not-supported",
                "scenario.end.reward_results",
                "synthetic-master-only accepts no server reward mutation",
            )
        )
    if state.stamina < row.stamina:
        issues.append(
            _issue(
                "nia-self-lesson-synthetic-stamina-insufficient",
                "outer_state.stamina",
                f"required={row.stamina}:actual={state.stamina}",
            )
        )
        return _dedupe(issues)
    expected_stamina = state.stamina - row.stamina
    if scenario.start.after_stamina != expected_stamina:
        issues.append(
            _issue(
                "nia-self-lesson-synthetic-start-stamina-mismatch",
                "scenario.start.after_stamina",
                f"expected={expected_stamina}:actual={scenario.start.after_stamina}",
            )
        )
    expected_attributes = _attributes_with_gain(
        state.attributes,
        scenario.selected_attribute,
        row.parameter,
        inventory.parameter_growth_limit,
    )
    expected_post = NiaSelfLessonPostState.from_outer_state(
        state,
        stamina=expected_stamina,
        attributes=expected_attributes,
    )
    if scenario.post_state != expected_post:
        issues.append(
            _issue(
                "nia-self-lesson-synthetic-post-state-mismatch",
                "scenario.post_state",
                "synthetic-master-only post-state must be the exact Master mutation",
            )
        )
    return _dedupe(issues)


def _scenario_issues(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularNiaSelfLessonScenario,
    inventory: NiaStaticInventory,
) -> tuple[InitialRegularInnerStageIssue, ...]:
    issues: list[InitialRegularInnerStageIssue] = []
    if state.phase is not RolloutPhase.WAITING_EXTERNAL:
        issues.append(
            _issue(
                "nia-self-lesson-request-not-pending",
                "outer_state.phase",
                state.phase.value,
            )
        )
    if state.pending != request:
        issues.append(
            _issue(
                "nia-self-lesson-pending-request-mismatch",
                "outer_state.pending",
            )
        )
    if request.kind is not ExternalKind.WEEKLY_ACTION_OUTCOME:
        issues.append(
            _issue(
                "nia-self-lesson-request-kind-mismatch",
                "request.kind",
                request.kind.value,
            )
        )
    if request.action_id != NIA_SELF_LESSON_ACTION_ID:
        issues.append(
            _issue(
                "nia-self-lesson-action-mismatch",
                "request.action_id",
                request.action_id,
            )
        )
    if request.week != state.week or scenario.week != state.week:
        issues.append(
            _issue(
                "nia-self-lesson-week-mismatch",
                "request/scenario.week",
                f"state={state.week}:request={request.week}:scenario={scenario.week}",
            )
        )
    if request.stage_type is not None:
        issues.append(
            _issue(
                NIA_SELF_LESSON_NO_INNER_EXAM,
                "request.stage_type",
                request.stage_type,
            )
        )
    if state.mode_id != "produce-004" or inventory.produce_id != "produce-004":
        issues.append(
            _issue(
                "nia-self-lesson-produce-id-mismatch",
                "outer_state/inventory.produce_id",
                f"state={state.mode_id}:inventory={inventory.produce_id}",
            )
        )
    actual_master = _master_shape(inventory)
    if actual_master != NIA_PRO_SELF_LESSON_MASTER_ROWS:
        issues.append(
            _issue(
                "nia-self-lesson-master-catalog-mismatch",
                "inventory.lessons",
                f"expected=6:actual={len(actual_master)}",
            )
        )
    row = _selected_row(scenario, inventory)
    if row is None:
        issues.append(
            _issue(
                "nia-self-lesson-master-row-unresolved",
                "scenario.master_row_id",
                scenario.master_row_id,
            )
        )
    if scenario.start.before_stamina != state.stamina:
        issues.append(
            _issue(
                "nia-self-lesson-start-before-stamina-mismatch",
                "scenario.start.before_stamina",
                f"expected={state.stamina}:actual={scenario.start.before_stamina}",
            )
        )
    if scenario.branch.probability is None or scenario.branch.probability <= 0.0:
        issues.append(
            _issue(
                "nia-self-lesson-branch-probability-unresolved",
                "scenario.branch.probability",
            )
        )
    if scenario.branch.was_sp is None or scenario.branch.was_sp != scenario.is_sp:
        issues.append(
            _issue(
                "nia-self-lesson-sp-branch-mismatch",
                "scenario.branch.was_sp",
                f"row_is_sp={scenario.is_sp}:branch={scenario.branch.was_sp}",
            )
        )
    if (
        row is not None
        and scenario.authority
        is NiaSelfLessonScenarioAuthority.CALLER_AUTHORED_SYNTHETIC_MASTER_ONLY
    ):
        issues.extend(
            _synthetic_master_only_issues(state, scenario, row, inventory)
        )
    return _dedupe(issues)


def adapt_initial_regular_nia_self_lesson_scenario(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularNiaSelfLessonScenario,
    inventory: NiaStaticInventory,
) -> WeeklyActionOutcome | tuple[InitialRegularInnerStageIssue, ...]:
    """Project one exact SelfLesson Start/End lifecycle to the outer kernel.

    Captured caller-resolved responses use ``post_state`` as the authority;
    response effect/reward rows are retained but not re-executed.  A synthetic
    Master-only scenario is executable only when both response effect buckets
    and the reward bucket are empty, and is checked against the selected one
    of the six produce-004 Master rows.
    """

    if not isinstance(state, ProduceRolloutState):
        raise TypeError("state must be ProduceRolloutState")
    if not isinstance(request, ExternalRequest):
        raise TypeError("request must be ExternalRequest")
    if not isinstance(scenario, InitialRegularNiaSelfLessonScenario):
        raise TypeError("scenario must be InitialRegularNiaSelfLessonScenario")
    if not isinstance(inventory, NiaStaticInventory):
        raise TypeError("inventory must be NiaStaticInventory")
    issues = _scenario_issues(state, request, scenario, inventory)
    if issues:
        return issues
    return WeeklyActionOutcome(
        request_id=request.request_id,
        branch=scenario.branch,
        patch=scenario.post_state.to_patch(),
        # End RewardResults are already reflected by the authoritative post
        # state; they are not a server offer frontier awaiting another call.
        reward_requested=False,
    )


def reject_nia_self_lesson_inner_request(
    request: InitialRegularInnerStageRequest,
) -> tuple[InitialRegularInnerStageIssue, ...]:
    """Fail closed if a SelfLesson is routed through the card inner protocol."""

    if not isinstance(request, InitialRegularInnerStageRequest):
        raise TypeError("request must be InitialRegularInnerStageRequest")
    return (
        _issue(
            NIA_SELF_LESSON_NO_INNER_EXAM,
            "request.action_id",
            "Android SelfLesson Start/End requests contain only "
            "DeviceProduceUuid; unlike ProduceStepLessonEnd they carry no "
            "ExamEndResult, cards, RNG or TurnEndLogs",
        ),
    )


@dataclass(frozen=True, slots=True)
class InitialRegularNiaSelfLessonNoInnerAdapter:
    """Callable common-protocol guard for accidental SelfLesson delegation."""

    def __call__(
        self,
        request: InitialRegularInnerStageRequest,
    ) -> tuple[InitialRegularInnerStageIssue, ...]:
        return reject_nia_self_lesson_inner_request(request)


InitialRegularNiaSelfLessonAdapterResult: TypeAlias = (
    WeeklyActionOutcome | tuple[InitialRegularInnerStageIssue, ...]
)


__all__ = [
    "ANDROID_LESSON_END_REQUEST_FIELDS",
    "ANDROID_LESSON_END_UTILITY_INPUTS",
    "ANDROID_SELF_LESSON_END_REQUEST_FIELDS",
    "ANDROID_SELF_LESSON_END_RESPONSE_FIELDS",
    "ANDROID_SELF_LESSON_START_REQUEST_FIELDS",
    "ANDROID_SELF_LESSON_START_RESPONSE_FIELDS",
    "InitialRegularNiaSelfLessonAdapterResult",
    "InitialRegularNiaSelfLessonNoInnerAdapter",
    "InitialRegularNiaSelfLessonScenario",
    "NIA_PRO_SELF_LESSON_MASTER_ROWS",
    "NIA_SELF_LESSON_ACTION_ID",
    "NIA_SELF_LESSON_NO_INNER_EXAM",
    "NiaSelfLessonEndScenario",
    "NiaSelfLessonPostState",
    "NiaSelfLessonScenarioAuthority",
    "NiaSelfLessonStartScenario",
    "adapt_initial_regular_nia_self_lesson_scenario",
    "reject_nia_self_lesson_inner_request",
]
