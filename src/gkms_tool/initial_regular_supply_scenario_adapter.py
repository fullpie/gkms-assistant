"""Fail-closed Initial Regular Supply/Present scenario adapter.

The Present API is a server-owned boundary.  In Android 3.2.3 it is split
into ``ProduceStepPresentStart``, one or more
``ProduceStepPresentReceive(positionNumber, rewardIndexes)`` calls, and
``ProduceStepPresentEnd``.  The offered members therefore come only from the
captured ``UserProduceProgressPresent`` rows.  This module never expands a
Master reward set or random pool.

The adapter is deliberately a replay wire, not a reward roller.  A successful
scenario carries the exact post-state that the response/LocalSave established.
Item and drink rewards additionally bind their resource result to concrete
outer session refs; this is necessary because ``ProduceStatePatch`` stores
runtime refs rather than Master resource IDs.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
import json
from typing import Mapping, Sequence, TypeAlias

from .initial_regular_event_scenario import (
    ProduceEffectResultTrace,
    ProduceRewardResultSnapshot,
    ScenarioBlocker,
    UserProduceProgressPresent,
    validate_present_position_order,
)
from .initial_regular_rollout import INITIAL_REGULAR_PRODUCE_ID
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
from .route_calendar import SUPPLY


SUPPLY_SCENARIO_ADAPTER_REF = (
    "gkms_tool.initial_regular_supply_scenario_adapter"
)

PRODUCE_CARD = "ProduceResourceType_ProduceCard"
PRODUCE_ITEM = "ProduceResourceType_ProduceItem"
PRODUCE_DRINK = "ProduceResourceType_ProduceDrink"
PRODUCE_POINT = "ProduceResourceType_ProducePoint"
STAMINA = "ProduceResourceType_Stamina"
PARAMETER_VOCAL = "ProduceResourceType_ParameterVocal"
PARAMETER_DANCE = "ProduceResourceType_ParameterDance"
PARAMETER_VISUAL = "ProduceResourceType_ParameterVisual"

_SUPPORTED_RESOURCE_TYPES = frozenset(
    {
        PRODUCE_CARD,
        PRODUCE_ITEM,
        PRODUCE_DRINK,
        PRODUCE_POINT,
        STAMINA,
        PARAMETER_VOCAL,
        PARAMETER_DANCE,
        PARAMETER_VISUAL,
    }
)
_INVENTORY_RESOURCE_TYPES = frozenset({PRODUCE_ITEM, PRODUCE_DRINK})
_DISPLAY_TYPES = frozenset(
    {"ProduceDisplayType_Itself", "ProduceDisplayType_Choice"}
)
_FKTN_CHARACTER_ID = "fktn"
_SCHEMA_NAME = "gkms_tool.initial_regular_supply_scenario"
_SCHEMA_VERSION = 1


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if not _is_int(value) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty text")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} keys must be text")
    return value


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _exact(value: Mapping[str, object], fields: set[str], label: str) -> None:
    actual = set(value)
    if actual != fields:
        missing = sorted(fields - actual)
        extra = sorted(actual - fields)
        raise ValueError(f"{label} fields differ: missing={missing}, extra={extra}")


def _text_tuple(value: object, label: str) -> tuple[str, ...]:
    return tuple(
        _text(item, f"{label}[{index}]")
        for index, item in enumerate(_list(value, label))
    )


class InitialRegularSupplyInventoryResolution(StrEnum):
    """Whether an item/drink capacity decision is response-resolved."""

    NOT_APPLICABLE = "not_applicable"
    SERVER_CONFIRMED = "server_confirmed"
    UNRESOLVED_FULL_INVENTORY = "unresolved_full_inventory"


@dataclass(frozen=True, slots=True)
class InitialRegularSupplyRewardResolution:
    """One response-ordered reward and its exact inventory mutation.

    ``reward_index`` is the index sent to PresentReceive.  It is ``None`` for
    an unindexed ``ProduceStepPresentEndResponse.RewardResults`` member.
    ``added_session_refs`` and ``removed_session_refs`` are meaningful only
    for ProduceItem/ProduceDrink.  They are scenario evidence, never derived
    from ``resource_id``.
    """

    reward: ProduceRewardResultSnapshot
    reward_index: int | None = None
    inventory_resolution: InitialRegularSupplyInventoryResolution = (
        InitialRegularSupplyInventoryResolution.NOT_APPLICABLE
    )
    added_session_refs: tuple[str, ...] = ()
    removed_session_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.reward, ProduceRewardResultSnapshot):
            raise TypeError("reward must be ProduceRewardResultSnapshot")
        if self.reward_index is not None:
            _integer(self.reward_index, "reward_index")
        resolution = self.inventory_resolution
        if isinstance(resolution, str) and not isinstance(
            resolution, InitialRegularSupplyInventoryResolution
        ):
            try:
                resolution = InitialRegularSupplyInventoryResolution(resolution)
            except ValueError as error:
                raise ValueError("invalid inventory resolution") from error
            object.__setattr__(self, "inventory_resolution", resolution)
        if not isinstance(resolution, InitialRegularSupplyInventoryResolution):
            raise TypeError("inventory_resolution has the wrong type")
        for name in ("added_session_refs", "removed_session_refs"):
            values = tuple(getattr(self, name))
            if any(not isinstance(value, str) or not value for value in values):
                raise ValueError(f"{name} must contain non-empty text")
            if len(values) != len(set(values)):
                raise ValueError(f"{name} must contain unique refs")
            object.__setattr__(self, name, values)

    def to_dict(self) -> dict[str, object]:
        return {
            "reward": self.reward.to_dict(),
            "reward_index": self.reward_index,
            "inventory_resolution": self.inventory_resolution.value,
            "added_session_refs": list(self.added_session_refs),
            "removed_session_refs": list(self.removed_session_refs),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "InitialRegularSupplyRewardResolution":
        value = _mapping(payload, "supply reward resolution")
        _exact(
            value,
            {
                "reward",
                "reward_index",
                "inventory_resolution",
                "added_session_refs",
                "removed_session_refs",
            },
            "supply reward resolution",
        )
        raw_index = value["reward_index"]
        return cls(
            reward=ProduceRewardResultSnapshot.from_dict(
                _mapping(value["reward"], "supply reward")
            ),
            reward_index=(
                None
                if raw_index is None
                else _integer(raw_index, "supply reward index")
            ),
            inventory_resolution=InitialRegularSupplyInventoryResolution(
                _text(value["inventory_resolution"], "inventory resolution")
            ),
            added_session_refs=_text_tuple(
                value["added_session_refs"], "added_session_refs"
            ),
            removed_session_refs=_text_tuple(
                value["removed_session_refs"], "removed_session_refs"
            ),
        )


@dataclass(frozen=True, slots=True)
class InitialRegularSupplyReceiveResult:
    """One exact PresentReceive response, keyed by PositionNumber."""

    position_number: int
    provided_rewards: tuple[InitialRegularSupplyRewardResolution, ...]
    effect_results: tuple[ProduceEffectResultTrace, ...] = ()

    def __post_init__(self) -> None:
        _integer(self.position_number, "receive position_number")
        rewards = tuple(self.provided_rewards)
        effects = tuple(self.effect_results)
        if not all(
            isinstance(value, InitialRegularSupplyRewardResolution)
            for value in rewards
        ):
            raise TypeError("provided_rewards must contain typed resolutions")
        if not all(isinstance(value, ProduceEffectResultTrace) for value in effects):
            raise TypeError("receive effect_results must contain typed traces")
        object.__setattr__(self, "provided_rewards", rewards)
        object.__setattr__(self, "effect_results", effects)

    @property
    def reward_indexes(self) -> tuple[int, ...]:
        return tuple(
            value.reward_index
            for value in self.provided_rewards
            if value.reward_index is not None
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "position_number": self.position_number,
            "provided_rewards": [value.to_dict() for value in self.provided_rewards],
            "effect_results": [value.to_dict() for value in self.effect_results],
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "InitialRegularSupplyReceiveResult":
        value = _mapping(payload, "supply receive result")
        _exact(
            value,
            {"position_number", "provided_rewards", "effect_results"},
            "supply receive result",
        )
        return cls(
            position_number=_integer(value["position_number"], "receive position"),
            provided_rewards=tuple(
                InitialRegularSupplyRewardResolution.from_dict(
                    _mapping(item, "provided reward")
                )
                for item in _list(value["provided_rewards"], "provided_rewards")
            ),
            effect_results=tuple(
                ProduceEffectResultTrace.from_dict(
                    _mapping(item, "receive effect result")
                )
                for item in _list(value["effect_results"], "receive effect_results")
            ),
        )


@dataclass(frozen=True, slots=True)
class InitialRegularSupplyEndResult:
    """Exact PresentEnd response boundary."""

    reward_results: tuple[InitialRegularSupplyRewardResolution, ...] = ()
    effect_results: tuple[ProduceEffectResultTrace, ...] = ()

    def __post_init__(self) -> None:
        rewards = tuple(self.reward_results)
        effects = tuple(self.effect_results)
        if not all(
            isinstance(value, InitialRegularSupplyRewardResolution)
            for value in rewards
        ):
            raise TypeError("end reward_results must contain typed resolutions")
        if not all(isinstance(value, ProduceEffectResultTrace) for value in effects):
            raise TypeError("end effect_results must contain typed traces")
        object.__setattr__(self, "reward_results", rewards)
        object.__setattr__(self, "effect_results", effects)

    def to_dict(self) -> dict[str, object]:
        return {
            "reward_results": [value.to_dict() for value in self.reward_results],
            "effect_results": [value.to_dict() for value in self.effect_results],
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "InitialRegularSupplyEndResult":
        value = _mapping(payload, "supply end result")
        _exact(
            value,
            {"reward_results", "effect_results"},
            "supply end result",
        )
        return cls(
            reward_results=tuple(
                InitialRegularSupplyRewardResolution.from_dict(
                    _mapping(item, "end reward result")
                )
                for item in _list(value["reward_results"], "end reward_results")
            ),
            effect_results=tuple(
                ProduceEffectResultTrace.from_dict(
                    _mapping(item, "end effect result")
                )
                for item in _list(value["effect_results"], "end effect_results")
            ),
        )


@dataclass(frozen=True, slots=True)
class InitialRegularSupplyPostState:
    """Complete outer-representable post-state from response/LocalSave."""

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
        _integer(self.stamina, "post stamina")
        _integer(self.max_stamina, "post max_stamina", minimum=1)
        _integer(self.produce_points, "post produce_points")
        if self.stamina > self.max_stamina:
            raise ValueError("post stamina exceeds max_stamina")
        if not isinstance(self.attributes, AttributeValues):
            raise TypeError("post attributes must be AttributeValues")
        deck = tuple(self.deck)
        if not all(isinstance(value, DeckEntry) for value in deck):
            raise TypeError("post deck must contain DeckEntry values")
        object.__setattr__(self, "deck", deck)
        instance_ids = tuple(
            instance_id for entry in deck for instance_id in entry.instance_ids
        )
        if len(instance_ids) != len(set(instance_ids)):
            raise ValueError("post deck instance IDs must be globally unique")
        for name in (
            "item_session_refs",
            "drink_session_refs",
            "passive_session_refs",
            "excluded_reward_card_ids",
        ):
            values = tuple(getattr(self, name))
            if any(not isinstance(value, str) or not value for value in values):
                raise ValueError(f"post {name} must contain non-empty text")
            if name != "drink_session_refs" and len(values) != len(set(values)):
                raise ValueError(f"post {name} must contain unique values")
            object.__setattr__(self, name, values)

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
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "InitialRegularSupplyPostState":
        value = _mapping(payload, "supply post state")
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
        _exact(value, fields, "supply post state")
        return cls(
            stamina=_integer(value["stamina"], "post stamina"),
            max_stamina=_integer(
                value["max_stamina"], "post max_stamina", minimum=1
            ),
            produce_points=_integer(
                value["produce_points"], "post produce_points"
            ),
            attributes=AttributeValues.from_dict(
                _mapping(value["attributes"], "post attributes")
            ),
            deck=tuple(
                DeckEntry.from_dict(_mapping(item, "post deck entry"))
                for item in _list(value["deck"], "post deck")
            ),
            item_session_refs=_text_tuple(
                value["item_session_refs"], "post item_session_refs"
            ),
            drink_session_refs=_text_tuple(
                value["drink_session_refs"], "post drink_session_refs"
            ),
            passive_session_refs=_text_tuple(
                value["passive_session_refs"], "post passive_session_refs"
            ),
            excluded_reward_card_ids=_text_tuple(
                value["excluded_reward_card_ids"],
                "post excluded_reward_card_ids",
            ),
        )


@dataclass(frozen=True, slots=True)
class InitialRegularSupplyScenario:
    """Server-resolved Present lifecycle and optional exact settled state."""

    presents: tuple[UserProduceProgressPresent, ...]
    receive_results: tuple[InitialRegularSupplyReceiveResult, ...] = ()
    end_result: InitialRegularSupplyEndResult | None = None
    post_state: InitialRegularSupplyPostState | None = None
    start_effect_results: tuple[ProduceEffectResultTrace, ...] = ()
    blockers: tuple[ScenarioBlocker, ...] = ()

    def __post_init__(self) -> None:
        presents = tuple(self.presents)
        receives = tuple(self.receive_results)
        effects = tuple(self.start_effect_results)
        blockers = tuple(self.blockers)
        if not all(isinstance(value, UserProduceProgressPresent) for value in presents):
            raise TypeError("presents must contain typed Present rows")
        validate_present_position_order(presents)
        if not all(
            isinstance(value, InitialRegularSupplyReceiveResult)
            for value in receives
        ):
            raise TypeError("receive_results must contain typed results")
        if self.end_result is not None and not isinstance(
            self.end_result, InitialRegularSupplyEndResult
        ):
            raise TypeError("end_result must be typed or None")
        if self.post_state is not None and not isinstance(
            self.post_state, InitialRegularSupplyPostState
        ):
            raise TypeError("post_state must be typed or None")
        if not all(isinstance(value, ProduceEffectResultTrace) for value in effects):
            raise TypeError("start_effect_results must contain typed traces")
        if not all(isinstance(value, ScenarioBlocker) for value in blockers):
            raise TypeError("blockers must contain ScenarioBlocker values")
        object.__setattr__(self, "presents", presents)
        object.__setattr__(self, "receive_results", receives)
        object.__setattr__(self, "start_effect_results", effects)
        object.__setattr__(self, "blockers", blockers)

    @property
    def end_confirmed(self) -> bool:
        return self.end_result is not None

    @property
    def remaining_positions(self) -> tuple[int, ...]:
        return tuple(
            value.position_number for value in self.presents if not value.received
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA_NAME,
            "schema_version": _SCHEMA_VERSION,
            "presents": [value.to_dict() for value in self.presents],
            "receive_results": [value.to_dict() for value in self.receive_results],
            "end_result": (
                None if self.end_result is None else self.end_result.to_dict()
            ),
            "post_state": (
                None if self.post_state is None else self.post_state.to_dict()
            ),
            "start_effect_results": [
                value.to_dict() for value in self.start_effect_results
            ],
            "blockers": [value.to_dict() for value in self.blockers],
        }

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, allow_nan=False, indent=indent
        )

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "InitialRegularSupplyScenario":
        value = _mapping(payload, "initial regular supply scenario")
        fields = {
            "schema",
            "schema_version",
            "presents",
            "receive_results",
            "end_result",
            "post_state",
            "start_effect_results",
            "blockers",
        }
        _exact(value, fields, "initial regular supply scenario")
        if value["schema"] != _SCHEMA_NAME or value["schema_version"] != _SCHEMA_VERSION:
            raise ValueError("unsupported initial regular supply scenario schema")
        raw_end = value["end_result"]
        raw_post = value["post_state"]
        return cls(
            presents=tuple(
                UserProduceProgressPresent.from_dict(
                    _mapping(item, "supply present")
                )
                for item in _list(value["presents"], "supply presents")
            ),
            receive_results=tuple(
                InitialRegularSupplyReceiveResult.from_dict(
                    _mapping(item, "supply receive result")
                )
                for item in _list(
                    value["receive_results"], "supply receive_results"
                )
            ),
            end_result=(
                None
                if raw_end is None
                else InitialRegularSupplyEndResult.from_dict(
                    _mapping(raw_end, "supply end result")
                )
            ),
            post_state=(
                None
                if raw_post is None
                else InitialRegularSupplyPostState.from_dict(
                    _mapping(raw_post, "supply post state")
                )
            ),
            start_effect_results=tuple(
                ProduceEffectResultTrace.from_dict(
                    _mapping(item, "supply start effect result")
                )
                for item in _list(
                    value["start_effect_results"], "supply start_effect_results"
                )
            ),
            blockers=tuple(
                ScenarioBlocker.from_dict(_mapping(item, "supply blocker"))
                for item in _list(value["blockers"], "supply blockers")
            ),
        )

    @classmethod
    def from_json(cls, raw: str) -> "InitialRegularSupplyScenario":
        value = json.loads(raw)
        return cls.from_dict(_mapping(value, "initial regular supply scenario"))


@dataclass(frozen=True, slots=True, order=True)
class InitialRegularSupplyPendingSelection:
    position_number: int
    pick_count: int
    selected_indexes: tuple[int, ...]
    remaining_pick_count: int


@dataclass(frozen=True, slots=True, order=True)
class InitialRegularSupplyScenarioIssue:
    code: str
    field: str
    detail: str = ""
    position_number: int | None = None
    reward_index: int | None = None

    def __post_init__(self) -> None:
        _text(self.code, "issue code")
        _text(self.field, "issue field")
        if self.position_number is not None:
            _integer(self.position_number, "issue position_number")
        if self.reward_index is not None:
            _integer(self.reward_index, "issue reward_index")


@dataclass(frozen=True, slots=True)
class InitialRegularSupplyScenarioPause:
    outer_state: ProduceRolloutState
    request: ExternalRequest
    scenario: InitialRegularSupplyScenario
    branch: ChanceBranch
    issues: tuple[InitialRegularSupplyScenarioIssue, ...]
    remaining_selections: tuple[InitialRegularSupplyPendingSelection, ...] = ()
    outcome: None = field(default=None, init=False)

    def __post_init__(self) -> None:
        issues = tuple(self.issues)
        remaining = tuple(self.remaining_selections)
        if not issues or not all(
            isinstance(value, InitialRegularSupplyScenarioIssue) for value in issues
        ):
            raise ValueError("supply pause requires typed issues")
        if not all(
            isinstance(value, InitialRegularSupplyPendingSelection)
            for value in remaining
        ):
            raise TypeError("remaining_selections must be typed")
        object.__setattr__(self, "issues", issues)
        object.__setattr__(self, "remaining_selections", remaining)

    @property
    def available(self) -> bool:
        return False


InitialRegularSupplyScenarioResult: TypeAlias = (
    WeeklyActionOutcome | InitialRegularSupplyScenarioPause
)


def _issue(
    code: str,
    field_name: str,
    detail: str = "",
    *,
    position: int | None = None,
    reward_index: int | None = None,
) -> InitialRegularSupplyScenarioIssue:
    return InitialRegularSupplyScenarioIssue(
        code=code,
        field=field_name,
        detail=detail,
        position_number=position,
        reward_index=reward_index,
    )


def _remaining(
    scenario: InitialRegularSupplyScenario,
) -> tuple[InitialRegularSupplyPendingSelection, ...]:
    return tuple(
        InitialRegularSupplyPendingSelection(
            position_number=present.position_number,
            pick_count=present.pick_count,
            selected_indexes=present.reward_indexes,
            remaining_pick_count=present.pick_count - len(present.reward_indexes),
        )
        for present in scenario.presents
        if not present.received
    )


def _pause(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularSupplyScenario,
    branch: ChanceBranch,
    issues: Sequence[InitialRegularSupplyScenarioIssue],
) -> InitialRegularSupplyScenarioPause:
    return InitialRegularSupplyScenarioPause(
        outer_state=state,
        request=request,
        scenario=scenario,
        branch=branch,
        issues=tuple(issues),
        remaining_selections=_remaining(scenario),
    )


def _preflight_issues(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularSupplyScenario,
) -> tuple[InitialRegularSupplyScenarioIssue, ...]:
    issues: list[InitialRegularSupplyScenarioIssue] = []
    if state.mode_id != INITIAL_REGULAR_PRODUCE_ID:
        issues.append(_issue("state-mode-mismatch", "state.mode_id", state.mode_id))
    if state.character_id != _FKTN_CHARACTER_ID:
        issues.append(
            _issue(
                "state-character-mismatch",
                "state.character_id",
                state.character_id,
            )
        )
    if request.kind != ExternalKind.WEEKLY_ACTION_OUTCOME:
        issues.append(_issue("request-kind-mismatch", "request.kind"))
    if state.phase != RolloutPhase.WAITING_EXTERNAL or state.pending != request:
        issues.append(_issue("outer-request-not-pending", "state.pending"))
    if request.week != state.week:
        issues.append(
            _issue(
                "request-week-mismatch",
                "request.week",
                f"state={state.week}:request={request.week}",
            )
        )
    if request.action_id != SUPPLY:
        issues.append(
            _issue("request-action-mismatch", "request.action_id", request.action_id)
        )
    if request.adapter_ref not in {None, SUPPLY_SCENARIO_ADAPTER_REF}:
        issues.append(
            _issue(
                "request-adapter-mismatch",
                "request.adapter_ref",
                request.adapter_ref,
            )
        )
    issues.extend(
        _issue(f"scenario:{value.code}", "scenario.blockers", value.message)
        for value in scenario.blockers
    )
    return tuple(issues)


def _reward_identity(value: object) -> tuple[str, str, int, int]:
    return (
        getattr(value, "resource_type"),
        getattr(value, "resource_id"),
        getattr(value, "resource_level"),
        getattr(value, "quantity"),
    )


def _join_receive_results(
    scenario: InitialRegularSupplyScenario,
) -> tuple[
    tuple[InitialRegularSupplyRewardResolution, ...],
    tuple[InitialRegularSupplyScenarioIssue, ...],
]:
    issues: list[InitialRegularSupplyScenarioIssue] = []
    by_position: dict[int, list[InitialRegularSupplyReceiveResult]] = {}
    for result in scenario.receive_results:
        by_position.setdefault(result.position_number, []).append(result)
    present_by_position = {
        value.position_number: value for value in scenario.presents
    }
    for position, values in by_position.items():
        if position not in present_by_position:
            issues.append(
                _issue(
                    "receive-position-extra",
                    "scenario.receive_results.position_number",
                    "no Present row has this position",
                    position=position,
                )
            )
        if len(values) > 1:
            issues.append(
                _issue(
                    "receive-position-duplicate",
                    "scenario.receive_results.position_number",
                    f"{len(values)} responses share one position",
                    position=position,
                )
            )

    resolved: list[InitialRegularSupplyRewardResolution] = []
    for present in scenario.presents:
        responses = by_position.get(present.position_number, ())
        if not present.received:
            issues.append(
                _issue(
                    "present-selection-pending",
                    "scenario.presents.received",
                    f"remaining={present.pick_count - len(present.reward_indexes)}",
                    position=present.position_number,
                )
            )
            if responses:
                issues.append(
                    _issue(
                        "receive-for-unreceived-present",
                        "scenario.receive_results",
                        position=present.position_number,
                    )
                )
            continue
        if len(responses) != 1:
            if not responses:
                issues.append(
                    _issue(
                        "receive-result-missing",
                        "scenario.receive_results",
                        position=present.position_number,
                    )
                )
            continue
        response = responses[0]
        indexes = tuple(value.reward_index for value in response.provided_rewards)
        if any(value is None for value in indexes):
            issues.append(
                _issue(
                    "receive-reward-index-missing",
                    "scenario.receive_results.provided_rewards.reward_index",
                    position=present.position_number,
                )
            )
            continue
        typed_indexes = tuple(value for value in indexes if value is not None)
        if typed_indexes != present.reward_indexes:
            issues.append(
                _issue(
                    "receive-index-order-mismatch",
                    "scenario.receive_results.provided_rewards.reward_index",
                    f"present={present.reward_indexes}:response={typed_indexes}",
                    position=present.position_number,
                )
            )
            continue
        for resolution in response.provided_rewards:
            assert resolution.reward_index is not None
            reward_index = resolution.reward_index
            offered = present.rewards[reward_index]
            if _reward_identity(offered) != _reward_identity(resolution.reward):
                issues.append(
                    _issue(
                        "receive-reward-identity-mismatch",
                        "scenario.receive_results.provided_rewards.reward",
                        f"offer={_reward_identity(offered)!r}:"
                        f"response={_reward_identity(resolution.reward)!r}",
                        position=present.position_number,
                        reward_index=reward_index,
                    )
                )
                continue
            resolved.append(resolution)

    return tuple(resolved), tuple(issues)


def _resolution_issues(
    resolutions: Sequence[InitialRegularSupplyRewardResolution],
    *,
    end_members: bool = False,
) -> tuple[InitialRegularSupplyScenarioIssue, ...]:
    issues: list[InitialRegularSupplyScenarioIssue] = []
    for index, resolution in enumerate(resolutions):
        reward = resolution.reward
        if end_members and resolution.reward_index is not None:
            issues.append(
                _issue(
                    "end-reward-index-unexpected",
                    "scenario.end_result.reward_results.reward_index",
                    reward_index=resolution.reward_index,
                )
            )
        if reward.quantity < 1:
            issues.append(
                _issue(
                    "reward-quantity-invalid",
                    "reward.quantity",
                    f"response-index={index}",
                    reward_index=resolution.reward_index,
                )
            )
        if reward.resource_type not in _SUPPORTED_RESOURCE_TYPES:
            issues.append(
                _issue(
                    "reward-resource-unrepresentable",
                    "reward.resource_type",
                    reward.resource_type,
                    reward_index=resolution.reward_index,
                )
            )
            continue
        if reward.resource_type in {PRODUCE_CARD, PRODUCE_ITEM, PRODUCE_DRINK} and not reward.resource_id:
            issues.append(
                _issue(
                    "reward-resource-id-missing",
                    "reward.resource_id",
                    reward.resource_type,
                    reward_index=resolution.reward_index,
                )
            )
        inventory = reward.resource_type in _INVENTORY_RESOURCE_TYPES
        if inventory:
            if (
                resolution.inventory_resolution
                == InitialRegularSupplyInventoryResolution.UNRESOLVED_FULL_INVENTORY
            ):
                issues.append(
                    _issue(
                        "full-inventory-resolution-unresolved",
                        "reward.inventory_resolution",
                        reward.resource_type,
                        reward_index=resolution.reward_index,
                    )
                )
            elif (
                resolution.inventory_resolution
                != InitialRegularSupplyInventoryResolution.SERVER_CONFIRMED
            ):
                issues.append(
                    _issue(
                        "inventory-resolution-unconfirmed",
                        "reward.inventory_resolution",
                        reward.resource_type,
                        reward_index=resolution.reward_index,
                    )
                )
            if len(resolution.added_session_refs) != reward.quantity:
                issues.append(
                    _issue(
                        "inventory-added-ref-count-mismatch",
                        "reward.added_session_refs",
                        f"quantity={reward.quantity}:"
                        f"refs={len(resolution.added_session_refs)}",
                        reward_index=resolution.reward_index,
                    )
                )
        elif (
            resolution.inventory_resolution
            != InitialRegularSupplyInventoryResolution.NOT_APPLICABLE
            or resolution.added_session_refs
            or resolution.removed_session_refs
        ):
            issues.append(
                _issue(
                    "non-inventory-reward-has-inventory-mutation",
                    "reward.inventory_resolution",
                    reward.resource_type,
                    reward_index=resolution.reward_index,
                )
            )
    return tuple(issues)


def _deck_counter(values: Sequence[DeckEntry]) -> Counter[tuple[str, int]]:
    result: Counter[tuple[str, int]] = Counter()
    for value in values:
        result[(value.card_id, value.upgrade)] += value.count
    return result


def _card_post_issues(
    state: ProduceRolloutState,
    post: InitialRegularSupplyPostState,
    resolutions: Sequence[InitialRegularSupplyRewardResolution],
) -> tuple[InitialRegularSupplyScenarioIssue, ...]:
    expected = _deck_counter(state.deck)
    for resolution in resolutions:
        reward = resolution.reward
        if reward.resource_type == PRODUCE_CARD and reward.quantity > 0:
            expected[(reward.resource_id, reward.resource_level)] += reward.quantity
    actual = _deck_counter(post.deck)
    if actual != expected:
        return (
            _issue(
                "card-post-state-mismatch",
                "scenario.post_state.deck",
                f"expected={dict(expected)!r}:actual={dict(actual)!r}",
            ),
        )
    return ()


def _inventory_post_issues(
    state: ProduceRolloutState,
    post: InitialRegularSupplyPostState,
    resolutions: Sequence[InitialRegularSupplyRewardResolution],
) -> tuple[InitialRegularSupplyScenarioIssue, ...]:
    issues: list[InitialRegularSupplyScenarioIssue] = []
    for resource_type, state_refs, post_refs, field_name in (
        (
            PRODUCE_ITEM,
            state.item_session_refs,
            post.item_session_refs,
            "item_session_refs",
        ),
        (
            PRODUCE_DRINK,
            state.drink_session_refs,
            post.drink_session_refs,
            "drink_session_refs",
        ),
    ):
        relevant = [
            value
            for value in resolutions
            if value.reward.resource_type == resource_type
        ]
        added = [ref for value in relevant for ref in value.added_session_refs]
        removed = [ref for value in relevant for ref in value.removed_session_refs]
        if len(added) != len(set(added)):
            issues.append(
                _issue(
                    "inventory-added-ref-duplicate",
                    f"reward.{field_name}",
                    resource_type,
                )
            )
        if len(removed) != len(set(removed)):
            issues.append(
                _issue(
                    "inventory-removed-ref-duplicate",
                    f"reward.{field_name}",
                    resource_type,
                )
            )
        missing = sorted(set(removed) - set(state_refs))
        already_present = sorted(set(added) & set(state_refs) - set(removed))
        if missing:
            issues.append(
                _issue(
                    "inventory-removed-ref-missing",
                    f"reward.{field_name}",
                    repr(missing),
                )
            )
        if already_present:
            issues.append(
                _issue(
                    "inventory-added-ref-already-present",
                    f"reward.{field_name}",
                    repr(already_present),
                )
            )
        expected = (set(state_refs) - set(removed)) | set(added)
        if set(post_refs) != expected:
            issues.append(
                _issue(
                    "inventory-post-state-mismatch",
                    f"scenario.post_state.{field_name}",
                    f"expected={sorted(expected)!r}:actual={sorted(post_refs)!r}",
                )
            )
    return tuple(issues)


def _plain_post_issues(
    state: ProduceRolloutState,
    post: InitialRegularSupplyPostState,
    scenario: InitialRegularSupplyScenario,
    resolutions: Sequence[InitialRegularSupplyRewardResolution],
) -> tuple[InitialRegularSupplyScenarioIssue, ...]:
    """Check reward-only scalar dimensions when no response effect exists.

    Parameter caps are intentionally not reconstructed here.  The exact post
    value may gain anywhere from zero through the selected quantity.  Stamina
    uses the already-known max-stamina boundary.  If any response EffectResult
    is present, the complete post-state remains authority and no cross-effect
    ordering or arithmetic is invented.
    """

    has_effects = bool(
        scenario.start_effect_results
        or any(value.effect_results for value in scenario.receive_results)
        or (
            scenario.end_result is not None
            and scenario.end_result.effect_results
        )
    )
    if has_effects:
        return ()
    quantities: Counter[str] = Counter()
    for value in resolutions:
        quantities[value.reward.resource_type] += value.reward.quantity
    issues: list[InitialRegularSupplyScenarioIssue] = []
    if post.max_stamina != state.max_stamina:
        issues.append(
            _issue(
                "unexplained-max-stamina-change",
                "scenario.post_state.max_stamina",
            )
        )
    expected_stamina = min(
        state.max_stamina, state.stamina + quantities[STAMINA]
    )
    if post.stamina != expected_stamina:
        issues.append(
            _issue(
                "stamina-reward-post-state-mismatch",
                "scenario.post_state.stamina",
                f"expected={expected_stamina}:actual={post.stamina}",
            )
        )
    expected_points = state.produce_points + quantities[PRODUCE_POINT]
    if post.produce_points != expected_points:
        issues.append(
            _issue(
                "produce-point-reward-post-state-mismatch",
                "scenario.post_state.produce_points",
                f"expected={expected_points}:actual={post.produce_points}",
            )
        )
    for resource_type, attribute in (
        (PARAMETER_VOCAL, "vocal"),
        (PARAMETER_DANCE, "dance"),
        (PARAMETER_VISUAL, "visual"),
    ):
        before = getattr(state.attributes, attribute)
        after = getattr(post.attributes, attribute)
        quantity = quantities[resource_type]
        if not before <= after <= before + quantity:
            issues.append(
                _issue(
                    "parameter-reward-post-state-mismatch",
                    f"scenario.post_state.attributes.{attribute}",
                    f"before={before}:quantity={quantity}:after={after}",
                )
            )
    if post.passive_session_refs != state.passive_session_refs:
        issues.append(
            _issue(
                "unexplained-passive-inventory-change",
                "scenario.post_state.passive_session_refs",
            )
        )
    if post.excluded_reward_card_ids != state.excluded_reward_card_ids:
        issues.append(
            _issue(
                "unexplained-reward-exclusion-change",
                "scenario.post_state.excluded_reward_card_ids",
            )
        )
    return tuple(issues)


def adapt_initial_regular_supply_scenario(
    state: ProduceRolloutState,
    request: ExternalRequest,
    scenario: InitialRegularSupplyScenario,
    *,
    branch: ChanceBranch,
) -> InitialRegularSupplyScenarioResult:
    """Return one exact weekly outcome, or a typed fail-closed pause."""

    if not isinstance(state, ProduceRolloutState):
        raise TypeError("state must be ProduceRolloutState")
    if not isinstance(request, ExternalRequest):
        raise TypeError("request must be ExternalRequest")
    if not isinstance(scenario, InitialRegularSupplyScenario):
        raise TypeError("scenario must be InitialRegularSupplyScenario")
    if not isinstance(branch, ChanceBranch):
        raise TypeError("branch must be ChanceBranch")

    issues = list(_preflight_issues(state, request, scenario))
    for present in scenario.presents:
        if present.display_type not in _DISPLAY_TYPES:
            issues.append(
                _issue(
                    "present-display-type-unsupported",
                    "scenario.presents.display_type",
                    present.display_type,
                    position=present.position_number,
                )
            )

    receive_rewards, receive_issues = _join_receive_results(scenario)
    issues.extend(receive_issues)
    issues.extend(_resolution_issues(receive_rewards))

    end_rewards: tuple[InitialRegularSupplyRewardResolution, ...] = ()
    if scenario.end_result is None:
        issues.append(
            _issue(
                "present-end-response-unconfirmed",
                "scenario.end_result",
                "ProduceStepPresentEndResponse is required",
            )
        )
    else:
        end_rewards = scenario.end_result.reward_results
        issues.extend(_resolution_issues(end_rewards, end_members=True))
        if scenario.remaining_positions:
            issues.append(
                _issue(
                    "present-end-with-pending-selection",
                    "scenario.end_result",
                    repr(scenario.remaining_positions),
                )
            )

    if scenario.post_state is None:
        issues.append(
            _issue(
                "supply-post-state-unresolved",
                "scenario.post_state",
                "exact response/LocalSave outer state is required",
            )
        )
    else:
        all_rewards = receive_rewards + end_rewards
        issues.extend(_card_post_issues(state, scenario.post_state, all_rewards))
        issues.extend(
            _inventory_post_issues(state, scenario.post_state, all_rewards)
        )
        issues.extend(
            _plain_post_issues(state, scenario.post_state, scenario, all_rewards)
        )

    if issues:
        return _pause(state, request, scenario, branch, issues)
    assert scenario.post_state is not None
    return WeeklyActionOutcome(
        request_id=request.request_id,
        branch=branch,
        patch=scenario.post_state.to_patch(),
        reward_requested=False,
        continuation=None,
    )


adapt_initial_regular_supply_scenario_to_weekly_outcome = (
    adapt_initial_regular_supply_scenario
)
resolve_initial_regular_supply_scenario = adapt_initial_regular_supply_scenario


__all__ = [
    "InitialRegularSupplyEndResult",
    "InitialRegularSupplyInventoryResolution",
    "InitialRegularSupplyPendingSelection",
    "InitialRegularSupplyPostState",
    "InitialRegularSupplyReceiveResult",
    "InitialRegularSupplyRewardResolution",
    "InitialRegularSupplyScenario",
    "InitialRegularSupplyScenarioIssue",
    "InitialRegularSupplyScenarioPause",
    "InitialRegularSupplyScenarioResult",
    "PARAMETER_DANCE",
    "PARAMETER_VISUAL",
    "PARAMETER_VOCAL",
    "PRODUCE_CARD",
    "PRODUCE_DRINK",
    "PRODUCE_ITEM",
    "PRODUCE_POINT",
    "STAMINA",
    "SUPPLY_SCENARIO_ADAPTER_REF",
    "adapt_initial_regular_supply_scenario",
    "adapt_initial_regular_supply_scenario_to_weekly_outcome",
    "resolve_initial_regular_supply_scenario",
]
