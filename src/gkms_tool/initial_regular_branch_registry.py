"""Exact request routing for Initial Regular offline branch providers.

The GUI and fixed-run driver both need one callable branch provider, while
lessons, events, supplies, rewards, and auditions are resolved by different
typed adapters.  This registry only dispatches an already pending request.  It
does not choose an action, normalize probabilities, or synthesize a missing
scenario.  A route may be week-specific or explicitly reusable for every week.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .initial_regular_offline_search import (
    InitialRegularOfflineBranchExpansion,
    InitialRegularOfflineBranchProvider,
)
from .initial_regular_weekly_adapter import InitialRegularWeeklyNode
from .produce_rollout import ExternalKind, ExternalRequest, ProduceRolloutState
from .produce_rollout_expectimax import OuterSearchIssue


@dataclass(frozen=True, slots=True, order=True)
class InitialRegularBranchRouteKey:
    kind: ExternalKind
    action_id: str
    stage_type: str | None
    adapter_ref: str | None
    week: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ExternalKind):
            raise TypeError("branch route kind must be ExternalKind")
        if not isinstance(self.action_id, str) or not self.action_id.strip():
            raise ValueError("branch route action_id must be non-empty text")
        for name in ("stage_type", "adapter_ref"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(f"branch route {name} cannot be empty")
        if self.week is not None and (
            isinstance(self.week, bool)
            or not isinstance(self.week, int)
            or self.week < 1
        ):
            raise ValueError("branch route week must be positive or None")

    @classmethod
    def from_request(
        cls,
        request: ExternalRequest,
        *,
        reusable: bool = False,
    ) -> "InitialRegularBranchRouteKey":
        if not isinstance(request, ExternalRequest):
            raise TypeError("request must be ExternalRequest")
        if not isinstance(reusable, bool):
            raise TypeError("reusable must be boolean")
        return cls(
            kind=request.kind,
            action_id=request.action_id,
            stage_type=request.stage_type,
            adapter_ref=request.adapter_ref,
            week=None if reusable else request.week,
        )


@dataclass(frozen=True, slots=True)
class InitialRegularBranchRoute:
    key: InitialRegularBranchRouteKey
    provider: InitialRegularOfflineBranchProvider

    def __post_init__(self) -> None:
        if not isinstance(self.key, InitialRegularBranchRouteKey):
            raise TypeError("branch route key must be typed")
        if not callable(self.provider):
            raise TypeError("branch route provider must be callable")


class InitialRegularOfflineBranchRegistry:
    """One exact dispatcher usable directly as an offline branch provider."""

    def __init__(self, routes: Iterable[InitialRegularBranchRoute]) -> None:
        values = tuple(routes)
        if not all(isinstance(value, InitialRegularBranchRoute) for value in values):
            raise TypeError("branch registry routes must be typed")
        by_key: dict[
            InitialRegularBranchRouteKey, InitialRegularOfflineBranchProvider
        ] = {}
        for route in values:
            if route.key in by_key:
                raise ValueError(f"duplicate branch route: {route.key}")
            by_key[route.key] = route.provider
        self._routes = by_key

    @property
    def keys(self) -> tuple[InitialRegularBranchRouteKey, ...]:
        return tuple(
            sorted(
                self._routes,
                key=lambda value: (
                    value.kind.value,
                    value.action_id,
                    "" if value.stage_type is None else value.stage_type,
                    "" if value.adapter_ref is None else value.adapter_ref,
                    -1 if value.week is None else value.week,
                ),
            )
        )

    def __call__(
        self,
        state: ProduceRolloutState,
        request: ExternalRequest,
        weekly_node: InitialRegularWeeklyNode | None,
    ) -> InitialRegularOfflineBranchExpansion:
        if not isinstance(state, ProduceRolloutState):
            raise TypeError("state must be ProduceRolloutState")
        if not isinstance(request, ExternalRequest):
            raise TypeError("request must be ExternalRequest")
        exact = InitialRegularBranchRouteKey.from_request(request)
        reusable = InitialRegularBranchRouteKey.from_request(
            request, reusable=True
        )
        provider = self._routes.get(exact)
        if provider is None:
            provider = self._routes.get(reusable)
        if provider is None:
            return InitialRegularOfflineBranchExpansion.paused(
                OuterSearchIssue(
                    "offline-branch-route-missing",
                    "branch_registry",
                    (
                        f"week={request.week}:kind={request.kind.value}:"
                        f"action={request.action_id}:stage={request.stage_type}:"
                        f"adapter={request.adapter_ref}"
                    ),
                )
            )
        result = provider(state, request, weekly_node)
        if not isinstance(result, InitialRegularOfflineBranchExpansion):
            return InitialRegularOfflineBranchExpansion.paused(
                OuterSearchIssue(
                    "offline-branch-route-result-invalid",
                    "branch_registry.provider",
                    repr(exact),
                )
            )
        return result


__all__ = [
    "InitialRegularBranchRoute",
    "InitialRegularBranchRouteKey",
    "InitialRegularOfflineBranchRegistry",
]
