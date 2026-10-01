"""Server-resolved Initial Regular reward-card scenario frontiers.

Android 3.2.3 does not expose a standalone reward ``offer id``.  Rolled card
choices live in ``UserProduceProgressPresent.Rewards``: ``ResourceId`` is the
card id and ``ResourceLevel`` is its upgrade count.  Selection later sends
``ProduceStepPresentReceive(PositionNumber, RewardIndexes)``.  Exclusion sends
``ProduceExcludeProduceCardRequest.PickIndex``; its response has no replacement
field and instead updates ``UserProduceProgressPresent`` plus
``UserProduceProgress.ProduceCardRemainExcludeCount`` through CommonResponse
UserData.  The client then refreshes that same reward-list slot.

Accordingly, this module derives the outer-kernel ``RewardOffer.offer_id``
only from the exact wire slot ``(PositionNumber, RewardIndex)``.  It never
claims that value is a server protobuf field.  Candidate membership and every
frontier probability remain caller-supplied server scenario facts.  Master is
used solely to verify that each exact ``(card id, upgrade count)`` row exists;
no card pool or random-pool table is expanded here.  PC Produce LocalSave
cannot fill this boundary because it records accepted inventory events, not
the offered reward list.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from pathlib import Path
import re
import sqlite3
from typing import TypeAlias

from .initial_regular_event_scenario import UserProduceProgressPresent
from .initial_regular_offline_search import (
    InitialRegularOfflineBranchExpansion,
    InitialRegularWeightedRewardBranch,
)
from .initial_regular_rollout import INITIAL_REGULAR_PRODUCE_ID
from .initial_regular_weekly_adapter import InitialRegularWeeklyNode
from .master_db import DEFAULT_DATABASE
from .produce_rollout import (
    ChanceBranch,
    ExternalKind,
    ExternalRequest,
    ProduceRolloutState,
    RewardOffer,
    RewardOffersOutcome,
    RewardReplacementOutcome,
    RolloutPhase,
)
from .produce_rollout_expectimax import OuterSearchIssue


PRODUCE_CARD_RESOURCE_TYPE = "ProduceResourceType_ProduceCard"
PRODUCE_DISPLAY_TYPE_CHOICE = "ProduceDisplayType_Choice"
FKTN_CHARACTER_ID = "fktn"

_SCHEMA = "gkms_tool.initial_regular_reward_scenario"
_SCHEMA_VERSION = 1
_OFFER_ID_PATTERN = re.compile(r"^present:(\d+):reward:(\d+)$")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if not _is_int(value) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be boolean")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise ValueError(f"{label} must be an object with text keys")
    return value


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _exact(value: Mapping[str, object], fields: set[str], label: str) -> None:
    actual = set(value)
    if actual != fields:
        raise ValueError(
            f"{label} fields differ: "
            f"missing={sorted(fields - actual)}, extra={sorted(actual - fields)}"
        )


def _issue(code: str, field: str, detail: str = "") -> OuterSearchIssue:
    return OuterSearchIssue(code, field, detail)


class InitialRegularRewardOfferSource(StrEnum):
    """Response that exposed the server-owned Present rows to the client."""

    LESSON_END = "produce_step_lesson_end"
    AUDITION_END = "produce_step_audition_end"


def initial_regular_reward_offer_id(
    position_number: int,
    reward_index: int,
) -> str:
    """Return the stable outer identity for one exact Present reward slot."""

    position = _integer(position_number, "position_number")
    index = _integer(reward_index, "reward_index")
    return f"present:{position}:reward:{index}"


def parse_initial_regular_reward_offer_id(offer_id: str) -> tuple[int, int]:
    """Decode an adapter slot id; this is not a claimed protobuf id."""

    value = _text(offer_id, "offer_id")
    match = _OFFER_ID_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError("offer_id is not an Initial Regular Present slot id")
    return int(match.group(1)), int(match.group(2))


@dataclass(frozen=True, slots=True)
class InitialRegularRewardProgressScenario:
    """Exact reward-relevant UserProduceProgress state at one response.

    ``selection_required`` is deliberately explicit because the outer kernel
    has no skip/full-deck selection branch.  It is scenario evidence, not a
    value inferred from Master.  Likewise, an available reroll or a hidden
    but positive exclusion count is rejected by the adapter rather than
    silently removed from the legal action set.
    """

    source: InitialRegularRewardOfferSource
    present: UserProduceProgressPresent
    exclude_produce_card_ids: tuple[str, ...] = ()
    produce_card_remain_exclude_count: int = 0
    hidden_produce_card_exclude: bool = False
    produce_card_remain_select_reroll_count: int = 0
    hidden_produce_card_reroll: bool = False
    selection_required: bool = True

    def __post_init__(self) -> None:
        source = self.source
        if isinstance(source, str) and not isinstance(
            source, InitialRegularRewardOfferSource
        ):
            try:
                source = InitialRegularRewardOfferSource(source)
            except ValueError as error:
                raise ValueError("invalid reward offer source") from error
            object.__setattr__(self, "source", source)
        if not isinstance(source, InitialRegularRewardOfferSource):
            raise TypeError("source must be InitialRegularRewardOfferSource")
        if not isinstance(self.present, UserProduceProgressPresent):
            raise TypeError("present must be UserProduceProgressPresent")
        excluded = tuple(self.exclude_produce_card_ids)
        if any(not isinstance(value, str) or not value for value in excluded):
            raise ValueError("exclude_produce_card_ids must contain text ids")
        if len(excluded) != len(set(excluded)):
            raise ValueError("exclude_produce_card_ids must be unique")
        object.__setattr__(self, "exclude_produce_card_ids", excluded)
        _integer(
            self.produce_card_remain_exclude_count,
            "produce_card_remain_exclude_count",
        )
        _integer(
            self.produce_card_remain_select_reroll_count,
            "produce_card_remain_select_reroll_count",
        )
        _boolean(
            self.hidden_produce_card_exclude,
            "hidden_produce_card_exclude",
        )
        _boolean(
            self.hidden_produce_card_reroll,
            "hidden_produce_card_reroll",
        )
        _boolean(self.selection_required, "selection_required")

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source.value,
            "present": self.present.to_dict(),
            "exclude_produce_card_ids": list(self.exclude_produce_card_ids),
            "produce_card_remain_exclude_count": (
                self.produce_card_remain_exclude_count
            ),
            "hidden_produce_card_exclude": self.hidden_produce_card_exclude,
            "produce_card_remain_select_reroll_count": (
                self.produce_card_remain_select_reroll_count
            ),
            "hidden_produce_card_reroll": self.hidden_produce_card_reroll,
            "selection_required": self.selection_required,
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
    ) -> "InitialRegularRewardProgressScenario":
        value = _mapping(payload, "reward progress scenario")
        fields = {
            "source",
            "present",
            "exclude_produce_card_ids",
            "produce_card_remain_exclude_count",
            "hidden_produce_card_exclude",
            "produce_card_remain_select_reroll_count",
            "hidden_produce_card_reroll",
            "selection_required",
        }
        _exact(value, fields, "reward progress scenario")
        return cls(
            source=InitialRegularRewardOfferSource(
                _text(value["source"], "reward source")
            ),
            present=UserProduceProgressPresent.from_dict(
                _mapping(value["present"], "reward present")
            ),
            exclude_produce_card_ids=tuple(
                _text(item, f"exclude_produce_card_ids[{index}]")
                for index, item in enumerate(
                    _list(
                        value["exclude_produce_card_ids"],
                        "exclude_produce_card_ids",
                    )
                )
            ),
            produce_card_remain_exclude_count=_integer(
                value["produce_card_remain_exclude_count"],
                "produce_card_remain_exclude_count",
            ),
            hidden_produce_card_exclude=_boolean(
                value["hidden_produce_card_exclude"],
                "hidden_produce_card_exclude",
            ),
            produce_card_remain_select_reroll_count=_integer(
                value["produce_card_remain_select_reroll_count"],
                "produce_card_remain_select_reroll_count",
            ),
            hidden_produce_card_reroll=_boolean(
                value["hidden_produce_card_reroll"],
                "hidden_produce_card_reroll",
            ),
            selection_required=_boolean(
                value["selection_required"], "selection_required"
            ),
        )


@dataclass(frozen=True, slots=True)
class InitialRegularRewardOffersScenario:
    """Initial server-rolled card choices for a pending reward request."""

    progress: InitialRegularRewardProgressScenario

    def __post_init__(self) -> None:
        if not isinstance(self.progress, InitialRegularRewardProgressScenario):
            raise TypeError("progress must be InitialRegularRewardProgressScenario")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "kind": "offers",
            "progress": self.progress.to_dict(),
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
    ) -> "InitialRegularRewardOffersScenario":
        value = _mapping(payload, "reward offers scenario")
        _exact(
            value,
            {"schema", "schema_version", "kind", "progress"},
            "reward offers scenario",
        )
        if value["schema"] != _SCHEMA or value["schema_version"] != _SCHEMA_VERSION:
            raise ValueError("unsupported reward scenario schema")
        if value["kind"] != "offers":
            raise ValueError("reward scenario kind is not offers")
        return cls(
            InitialRegularRewardProgressScenario.from_dict(
                _mapping(value["progress"], "reward progress")
            )
        )


@dataclass(frozen=True, slots=True)
class InitialRegularRewardReplacementScenario:
    """Post-exclude UserData snapshot and exact request ``PickIndex``."""

    pick_index: int
    progress: InitialRegularRewardProgressScenario

    def __post_init__(self) -> None:
        _integer(self.pick_index, "pick_index")
        if not isinstance(self.progress, InitialRegularRewardProgressScenario):
            raise TypeError("progress must be InitialRegularRewardProgressScenario")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "kind": "replacement",
            "pick_index": self.pick_index,
            "progress": self.progress.to_dict(),
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
    ) -> "InitialRegularRewardReplacementScenario":
        value = _mapping(payload, "reward replacement scenario")
        _exact(
            value,
            {
                "schema",
                "schema_version",
                "kind",
                "pick_index",
                "progress",
            },
            "reward replacement scenario",
        )
        if value["schema"] != _SCHEMA or value["schema_version"] != _SCHEMA_VERSION:
            raise ValueError("unsupported reward scenario schema")
        if value["kind"] != "replacement":
            raise ValueError("reward scenario kind is not replacement")
        return cls(
            pick_index=_integer(value["pick_index"], "pick_index"),
            progress=InitialRegularRewardProgressScenario.from_dict(
                _mapping(value["progress"], "reward progress")
            ),
        )


InitialRegularRewardScenario: TypeAlias = (
    InitialRegularRewardOffersScenario
    | InitialRegularRewardReplacementScenario
)


def initial_regular_reward_scenario_from_dict(
    payload: Mapping[str, object],
) -> InitialRegularRewardScenario:
    value = _mapping(payload, "reward scenario")
    kind = value.get("kind")
    if kind == "offers":
        return InitialRegularRewardOffersScenario.from_dict(value)
    if kind == "replacement":
        return InitialRegularRewardReplacementScenario.from_dict(value)
    raise ValueError("unknown reward scenario kind")


@dataclass(frozen=True, slots=True)
class InitialRegularWeightedRewardScenario:
    probability: Fraction
    branch_id: str
    scenario: InitialRegularRewardScenario
    rng_token: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.probability, Fraction):
            raise TypeError("reward scenario probability must be Fraction")
        if self.probability <= 0 or self.probability > 1:
            raise ValueError("reward scenario probability must be in (0, 1]")
        _text(self.branch_id, "reward scenario branch_id")
        if not isinstance(
            self.scenario,
            (
                InitialRegularRewardOffersScenario,
                InitialRegularRewardReplacementScenario,
            ),
        ):
            raise TypeError("scenario must be a typed reward scenario")
        if self.rng_token is not None:
            _text(self.rng_token, "reward scenario rng_token")


@dataclass(frozen=True, slots=True)
class InitialRegularRewardScenarioFrontier:
    """A caller-declared complete distribution of server-rolled responses."""

    branches: tuple[InitialRegularWeightedRewardScenario, ...]
    complete: bool

    def __post_init__(self) -> None:
        branches = tuple(self.branches)
        if not all(
            isinstance(value, InitialRegularWeightedRewardScenario)
            for value in branches
        ):
            raise TypeError("reward frontier branches must be typed")
        if not isinstance(self.complete, bool):
            raise TypeError("reward frontier complete must be boolean")
        object.__setattr__(self, "branches", branches)


def _preflight_issues(
    state: ProduceRolloutState,
    request: ExternalRequest,
) -> list[OuterSearchIssue]:
    issues: list[OuterSearchIssue] = []
    if state.mode_id != INITIAL_REGULAR_PRODUCE_ID:
        issues.append(_issue("reward-state-mode-mismatch", "state.mode_id", state.mode_id))
    if state.character_id != FKTN_CHARACTER_ID:
        issues.append(
            _issue(
                "reward-state-character-mismatch",
                "state.character_id",
                state.character_id,
            )
        )
    if state.phase is not RolloutPhase.WAITING_EXTERNAL or state.pending != request:
        issues.append(
            _issue(
                "reward-request-not-pending",
                "state.pending",
                request.request_id,
            )
        )
    if request.kind not in {
        ExternalKind.REWARD_OFFERS,
        ExternalKind.REWARD_REPLACEMENT,
    }:
        issues.append(
            _issue(
                "reward-request-kind-unsupported",
                "request.kind",
                request.kind.value,
            )
        )
    if request.week != state.week:
        issues.append(
            _issue(
                "reward-request-week-mismatch",
                "request.week",
                f"request={request.week}:state={state.week}",
            )
        )
    if state.reward_context is None:
        issues.append(
            _issue("reward-context-missing", "state.reward_context")
        )
    return issues


def _progress_issues(
    progress: InitialRegularRewardProgressScenario,
) -> list[OuterSearchIssue]:
    present = progress.present
    issues: list[OuterSearchIssue] = []
    if present.received:
        issues.append(_issue("reward-present-already-received", "progress.present.received"))
    if present.display_type != PRODUCE_DISPLAY_TYPE_CHOICE:
        issues.append(
            _issue(
                "reward-present-display-type-unsupported",
                "progress.present.display_type",
                present.display_type,
            )
        )
    if present.pick_count != 1:
        issues.append(
            _issue(
                "reward-present-pick-count-unrepresentable",
                "progress.present.pick_count",
                str(present.pick_count),
            )
        )
    if present.reward_indexes:
        issues.append(
            _issue(
                "reward-present-selection-already-bound",
                "progress.present.reward_indexes",
                repr(present.reward_indexes),
            )
        )
    if not progress.selection_required:
        issues.append(
            _issue(
                "reward-selection-skip-unrepresented",
                "progress.selection_required",
                "outer kernel has no skip/full-deck selection action",
            )
        )
    if (
        progress.produce_card_remain_select_reroll_count > 0
        and not progress.hidden_produce_card_reroll
    ):
        issues.append(
            _issue(
                "reward-reroll-unrepresented",
                "progress.produce_card_remain_select_reroll_count",
                str(progress.produce_card_remain_select_reroll_count),
            )
        )
    if (
        progress.produce_card_remain_exclude_count > 0
        and progress.hidden_produce_card_exclude
    ):
        issues.append(
            _issue(
                "reward-hidden-exclude-unrepresented",
                "progress.hidden_produce_card_exclude",
                "positive raw exclude count is hidden by server state",
            )
        )
    return issues


def _present_offers(
    progress: InitialRegularRewardProgressScenario,
) -> tuple[tuple[RewardOffer, ...], list[OuterSearchIssue]]:
    offers: list[RewardOffer] = []
    issues = _progress_issues(progress)
    present = progress.present
    if not present.rewards:
        issues.append(_issue("reward-present-empty", "progress.present.rewards"))
    for index, reward in enumerate(present.rewards):
        field = f"progress.present.rewards[{index}]"
        if reward.resource_type != PRODUCE_CARD_RESOURCE_TYPE:
            issues.append(
                _issue(
                    "reward-resource-type-unrepresentable",
                    f"{field}.resource_type",
                    reward.resource_type,
                )
            )
            continue
        if not reward.resource_id:
            issues.append(_issue("reward-card-id-missing", f"{field}.resource_id"))
            continue
        if reward.quantity != 1:
            issues.append(
                _issue(
                    "reward-card-quantity-unrepresentable",
                    f"{field}.quantity",
                    str(reward.quantity),
                )
            )
            continue
        offers.append(
            RewardOffer(
                offer_id=initial_regular_reward_offer_id(
                    present.position_number,
                    index,
                ),
                card_id=reward.resource_id,
                upgrade=reward.resource_level,
            )
        )
    return tuple(offers), issues


def _master_issues(
    offers: tuple[RewardOffer, ...],
    database: Path,
    *,
    field: str,
) -> list[OuterSearchIssue]:
    if not database.is_file():
        return [
            _issue(
                "reward-master-database-missing",
                "database",
                str(database),
            )
        ]
    issues: list[OuterSearchIssue] = []
    try:
        with closing(sqlite3.connect(database)) as connection:
            for index, offer in enumerate(offers):
                row = connection.execute(
                    "SELECT 1 FROM card WHERE id = ? AND upgrade_count = ?",
                    (offer.card_id, offer.upgrade),
                ).fetchone()
                if row is None:
                    issues.append(
                        _issue(
                            "reward-card-master-identity-missing",
                            f"{field}[{index}]",
                            f"{offer.card_id}@{offer.upgrade}",
                        )
                    )
    except sqlite3.Error as error:
        issues.append(
            _issue(
                "reward-master-query-failed",
                "database.card",
                f"{type(error).__name__}:{error}",
            )
        )
    return issues


def _adapt_offers(
    state: ProduceRolloutState,
    request: ExternalRequest,
    weighted: InitialRegularWeightedRewardScenario,
    database: Path,
) -> tuple[RewardOffersOutcome | None, list[OuterSearchIssue]]:
    scenario = weighted.scenario
    if not isinstance(scenario, InitialRegularRewardOffersScenario):
        return None, [
            _issue(
                "reward-scenario-kind-mismatch",
                "scenario",
                request.kind.value,
            )
        ]
    progress = scenario.progress
    offers, issues = _present_offers(progress)
    if state.reward_offers:
        issues.append(
            _issue(
                "reward-offers-state-not-empty",
                "state.reward_offers",
                repr(tuple(value.offer_id for value in state.reward_offers)),
            )
        )
    if progress.exclude_produce_card_ids != state.excluded_reward_card_ids:
        issues.append(
            _issue(
                "reward-excluded-card-state-mismatch",
                "progress.exclude_produce_card_ids",
                (
                    f"scenario={progress.exclude_produce_card_ids}:"
                    f"state={state.excluded_reward_card_ids}"
                ),
            )
        )
    excluded = set(state.excluded_reward_card_ids)
    for index, offer in enumerate(offers):
        if offer.card_id in excluded:
            issues.append(
                _issue(
                    "reward-offer-already-excluded",
                    f"progress.present.rewards[{index}]",
                    offer.card_id,
                )
            )
    issues.extend(_master_issues(offers, database, field="scenario.offers"))
    if issues:
        return None, issues
    branch = ChanceBranch(
        branch_id=weighted.branch_id,
        probability=float(weighted.probability),
        rng_token=weighted.rng_token,
        event_id=scenario.progress.source.value,
    )
    return (
        RewardOffersOutcome(
            request_id=request.request_id,
            branch=branch,
            offers=offers,
            remaining_exclude_count=(
                progress.produce_card_remain_exclude_count
            ),
        ),
        [],
    )


def _adapt_replacement(
    state: ProduceRolloutState,
    request: ExternalRequest,
    weighted: InitialRegularWeightedRewardScenario,
    database: Path,
) -> tuple[RewardReplacementOutcome | None, list[OuterSearchIssue]]:
    scenario = weighted.scenario
    if not isinstance(scenario, InitialRegularRewardReplacementScenario):
        return None, [
            _issue(
                "reward-scenario-kind-mismatch",
                "scenario",
                request.kind.value,
            )
        ]
    progress = scenario.progress
    refreshed, issues = _present_offers(progress)
    old_offers = state.reward_offers
    if not old_offers:
        issues.append(_issue("reward-replacement-old-offers-missing", "state.reward_offers"))
    expected_count = state.remaining_reward_excludes - 1
    if state.remaining_reward_excludes < 1:
        issues.append(
            _issue(
                "reward-replacement-exclude-count-empty",
                "state.remaining_reward_excludes",
                str(state.remaining_reward_excludes),
            )
        )
    if progress.produce_card_remain_exclude_count != expected_count:
        issues.append(
            _issue(
                "reward-replacement-exclude-count-mismatch",
                "progress.produce_card_remain_exclude_count",
                f"expected={expected_count}:actual={progress.produce_card_remain_exclude_count}",
            )
        )
    if len(refreshed) != len(old_offers):
        issues.append(
            _issue(
                "reward-replacement-offer-count-mismatch",
                "progress.present.rewards",
                f"old={len(old_offers)}:refreshed={len(refreshed)}",
            )
        )

    target_index: int | None = None
    for index, old in enumerate(old_offers):
        expected_id = initial_regular_reward_offer_id(
            progress.present.position_number,
            index,
        )
        if old.offer_id != expected_id:
            issues.append(
                _issue(
                    "reward-replacement-offer-id-mismatch",
                    f"state.reward_offers[{index}].offer_id",
                    f"expected={expected_id}:actual={old.offer_id}",
                )
            )
        if old.offer_id == request.action_id:
            target_index = index
    if target_index is None:
        issues.append(
            _issue(
                "reward-replacement-request-offer-missing",
                "request.action_id",
                request.action_id,
            )
        )
    elif scenario.pick_index != target_index:
        issues.append(
            _issue(
                "reward-replacement-pick-index-mismatch",
                "scenario.pick_index",
                f"request_slot={target_index}:scenario={scenario.pick_index}",
            )
        )

    replacement: RewardOffer | None = None
    old_card_id: str | None = None
    if target_index is not None and target_index < len(refreshed):
        replacement = refreshed[target_index]
        old_card_id = old_offers[target_index].card_id
        if replacement.card_id == old_card_id:
            issues.append(
                _issue(
                    "reward-replacement-card-unchanged",
                    f"progress.present.rewards[{target_index}]",
                    old_card_id,
                )
            )
        for index, (old, new) in enumerate(zip(old_offers, refreshed)):
            if index == target_index:
                continue
            if (old.card_id, old.upgrade) != (new.card_id, new.upgrade):
                issues.append(
                    _issue(
                        "reward-replacement-nontarget-mutated",
                        f"progress.present.rewards[{index}]",
                        (
                            f"old={old.card_id}@{old.upgrade}:"
                            f"new={new.card_id}@{new.upgrade}"
                        ),
                    )
                )

    if old_card_id is not None:
        expected_excluded = (*state.excluded_reward_card_ids, old_card_id)
        if progress.exclude_produce_card_ids != expected_excluded:
            issues.append(
                _issue(
                    "reward-replacement-excluded-card-state-mismatch",
                    "progress.exclude_produce_card_ids",
                    (
                        f"expected={expected_excluded}:"
                        f"actual={progress.exclude_produce_card_ids}"
                    ),
                )
            )
    issues.extend(
        _master_issues(refreshed, database, field="scenario.refreshed_offers")
    )
    if issues or replacement is None:
        return None, issues
    branch = ChanceBranch(
        branch_id=weighted.branch_id,
        probability=float(weighted.probability),
        rng_token=weighted.rng_token,
        event_id="produce_exclude_produce_card",
    )
    return (
        RewardReplacementOutcome(
            request_id=request.request_id,
            branch=branch,
            replacement=replacement,
            remaining_exclude_count_after=(
                progress.produce_card_remain_exclude_count
            ),
        ),
        [],
    )


def build_initial_regular_reward_scenario_frontier(
    state: ProduceRolloutState,
    request: ExternalRequest,
    frontier: InitialRegularRewardScenarioFrontier,
    *,
    database: Path = DEFAULT_DATABASE,
) -> InitialRegularOfflineBranchExpansion:
    """Build an exact reward frontier, or return fail-closed typed issues."""

    if not isinstance(state, ProduceRolloutState):
        raise TypeError("state must be ProduceRolloutState")
    if not isinstance(request, ExternalRequest):
        raise TypeError("request must be ExternalRequest")
    if not isinstance(frontier, InitialRegularRewardScenarioFrontier):
        raise TypeError("frontier must be InitialRegularRewardScenarioFrontier")
    issues = _preflight_issues(state, request)
    if not frontier.complete:
        issues.append(
            _issue(
                "reward-frontier-incomplete",
                "frontier.complete",
                "server reward distribution was not declared complete",
            )
        )
    if not frontier.branches:
        issues.append(_issue("reward-frontier-empty", "frontier.branches"))
    branch_ids = tuple(value.branch_id for value in frontier.branches)
    if len(branch_ids) != len(set(branch_ids)):
        issues.append(
            _issue(
                "reward-frontier-branch-id-duplicate",
                "frontier.branches.branch_id",
            )
        )
    total = sum(
        (value.probability for value in frontier.branches),
        Fraction(),
    )
    if frontier.branches and total != Fraction(1):
        issues.append(
            _issue(
                "reward-frontier-probability-incomplete",
                "frontier.branches.probability",
                f"actual={total}",
            )
        )
    if issues:
        return InitialRegularOfflineBranchExpansion.paused(*issues)

    database = Path(database)
    resolved: list[InitialRegularWeightedRewardBranch] = []
    for index, weighted in enumerate(frontier.branches):
        if request.kind is ExternalKind.REWARD_OFFERS:
            outcome, branch_issues = _adapt_offers(
                state,
                request,
                weighted,
                database,
            )
        else:
            outcome, branch_issues = _adapt_replacement(
                state,
                request,
                weighted,
                database,
            )
        if branch_issues:
            issues.extend(
                _issue(
                    issue.code,
                    f"frontier.branches[{index}].{issue.field}",
                    issue.detail,
                )
                for issue in branch_issues
            )
            continue
        assert outcome is not None
        resolved.append(
            InitialRegularWeightedRewardBranch(weighted.probability, outcome)
        )
    if issues:
        return InitialRegularOfflineBranchExpansion.paused(*issues)
    return InitialRegularOfflineBranchExpansion.resolved(*resolved)


InitialRegularRewardFrontierSource: TypeAlias = Callable[
    [ProduceRolloutState, ExternalRequest, InitialRegularWeeklyNode | None],
    InitialRegularRewardScenarioFrontier | None,
]


class InitialRegularRewardScenarioProvider:
    """Registry-compatible provider backed by a server-scenario source."""

    def __init__(
        self,
        source: InitialRegularRewardFrontierSource,
        *,
        database: Path = DEFAULT_DATABASE,
    ) -> None:
        if not callable(source):
            raise TypeError("reward frontier source must be callable")
        self.source = source
        self.database = Path(database)

    def __call__(
        self,
        state: ProduceRolloutState,
        request: ExternalRequest,
        weekly_node: InitialRegularWeeklyNode | None,
    ) -> InitialRegularOfflineBranchExpansion:
        try:
            frontier = self.source(state, request, weekly_node)
        except Exception as error:  # scenario acquisition is an external seam
            return InitialRegularOfflineBranchExpansion.paused(
                _issue(
                    "reward-frontier-source-failed",
                    "reward_provider.source",
                    f"{type(error).__name__}:{error}",
                )
            )
        if frontier is None:
            return InitialRegularOfflineBranchExpansion.paused(
                _issue(
                    "reward-frontier-source-missing",
                    "reward_provider.source",
                    request.request_id,
                )
            )
        if not isinstance(frontier, InitialRegularRewardScenarioFrontier):
            return InitialRegularOfflineBranchExpansion.paused(
                _issue(
                    "reward-frontier-source-invalid",
                    "reward_provider.source",
                    type(frontier).__name__,
                )
            )
        return build_initial_regular_reward_scenario_frontier(
            state,
            request,
            frontier,
            database=self.database,
        )


__all__ = [
    "FKTN_CHARACTER_ID",
    "InitialRegularRewardFrontierSource",
    "InitialRegularRewardOfferSource",
    "InitialRegularRewardOffersScenario",
    "InitialRegularRewardProgressScenario",
    "InitialRegularRewardReplacementScenario",
    "InitialRegularRewardScenario",
    "InitialRegularRewardScenarioFrontier",
    "InitialRegularRewardScenarioProvider",
    "InitialRegularWeightedRewardScenario",
    "PRODUCE_CARD_RESOURCE_TYPE",
    "PRODUCE_DISPLAY_TYPE_CHOICE",
    "build_initial_regular_reward_scenario_frontier",
    "initial_regular_reward_offer_id",
    "initial_regular_reward_scenario_from_dict",
    "parse_initial_regular_reward_offer_id",
]
