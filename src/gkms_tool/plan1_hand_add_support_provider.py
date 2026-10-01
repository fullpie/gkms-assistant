"""Caller-authoritative Plan1 HandAdd support runtime boundary.

This module is intentionally a small adapter around the already reconstructed
native HandAdd evaluator.  It does not read LocalSave data, run a stage, or
invent support values.  Callers provide an ordered, typed support loadout,
including the runtime upgrade permil and the RNG authority label, and then
pass the typed request objects used by :mod:`plan1_native_stage`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .audition_native_support import (
    NativeHandAddSupportError,
    evaluate_native_hand_add_support,
)
from .audition_support_runtime import (
    SUPPORTED_LESSON_TYPES,
    SupportUpgradeRuntimeInput,
)
from .card_search import ProduceCardSearchRule
from .plan1_native_stage import (
    Plan1HandAddSupportRequest,
    Plan1HandAddSupportResolver,
)


class Plan1HandAddSupportProviderError(ValueError):
    """Typed, fail-closed errors at the caller/runtime boundary."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(f"{self.code}: {self.detail}")


def _provider_error(code: str, detail: str) -> Plan1HandAddSupportProviderError:
    return Plan1HandAddSupportProviderError(code, detail)


def _require_uint32(value: Any, *, code: str, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF:
        raise _provider_error(code, f"{label} must be an unsigned 32-bit integer")
    return int(value)


def _canonical_ids(
    values: Sequence[str],
    supports: tuple[SupportUpgradeRuntimeInput, ...],
) -> tuple[str, ...]:
    """Validate support IDs and return them in caller loadout order."""

    support_ids = tuple(item.support_id for item in supports)
    support_set = set(support_ids)
    seen: set[str] = set()
    for support_id in values:
        if not isinstance(support_id, str) or not support_id:
            raise _provider_error("support-used-id-unknown", "used support IDs must be non-empty text")
        if support_id not in support_set:
            raise _provider_error("support-used-id-unknown", f"unknown support ID: {support_id}")
        if support_id in seen:
            raise _provider_error("support-used-id-duplicate", f"duplicate support ID: {support_id}")
        seen.add(support_id)
    return tuple(support_id for support_id in support_ids if support_id in seen)


@dataclass(frozen=True, slots=True)
class Plan1HandAddSupportLoadout:
    """Caller-owned ordered support inputs for one deterministic Plan1 run.

    ``supports`` and ``support_card_searches`` are typed objects, rather than
    raw JSON, so no unvalidated payload can enter the native evaluator.  The
    ``used_support_ids`` tuple is the caller's current per-round state.  A
    subsequent request may reset that tuple for a new round while retaining
    the chained RNG state.
    """

    supports: tuple[SupportUpgradeRuntimeInput, ...]
    support_card_searches: Mapping[str, ProduceCardSearchRule]
    lesson_type: str
    random_state: int
    rng_authority: str
    used_support_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        supports = tuple(self.supports)
        if not supports:
            raise _provider_error(
                "support-loadout-identity-missing",
                "caller must provide at least one identified support",
            )
        for support in supports:
            if not isinstance(support, SupportUpgradeRuntimeInput):
                raise _provider_error(
                    "support-loadout-identity-missing",
                    "support loadout entries must be SupportUpgradeRuntimeInput",
                )
            if not support.support_id:
                raise _provider_error("support-loadout-identity-missing", "support ID is empty")
            if not isinstance(support.runtime_permil, int):
                raise _provider_error(
                    "support-runtime-permil-missing",
                    f"runtime permil missing for {support.support_id}",
                )
        support_ids = tuple(item.support_id for item in supports)
        if len(set(support_ids)) != len(support_ids):
            raise _provider_error("support-loadout-identity-duplicate", "support IDs must be unique")
        orders = tuple(item.loadout_order for item in supports)
        if len(set(orders)) != len(orders):
            raise _provider_error("support-loadout-order-duplicate", "support loadout orders must be unique")
        if orders != tuple(sorted(orders)):
            raise _provider_error(
                "support-loadout-order-invalid",
                "supports must be supplied in caller-authoritative loadout order",
            )
        if not isinstance(self.lesson_type, str) or self.lesson_type not in SUPPORTED_LESSON_TYPES:
            raise _provider_error("support-lesson-type-invalid", f"unsupported lesson type: {self.lesson_type!r}")
        _require_uint32(self.random_state, code="support-rng-state-invalid", label="random_state")
        if not isinstance(self.rng_authority, str) or not self.rng_authority.strip():
            raise _provider_error(
                "support-rng-authority-missing",
                "caller must identify the authoritative RNG source",
            )

        searches = self.support_card_searches
        if not isinstance(searches, Mapping):
            raise _provider_error("support-card-search-missing", "support card searches must be a typed mapping")
        copied_searches: dict[str, ProduceCardSearchRule] = {}
        for search_id, rule in searches.items():
            if not isinstance(search_id, str) or not search_id:
                raise _provider_error("support-card-search-missing", "search ID is empty")
            if not isinstance(rule, ProduceCardSearchRule):
                raise _provider_error(
                    "support-card-search-missing",
                    f"search rule {search_id!r} is not ProduceCardSearchRule",
                )
            if rule.id != search_id:
                raise _provider_error(
                    "support-card-search-mismatch",
                    f"search key {search_id!r} does not match rule ID {rule.id!r}",
                )
            copied_searches[search_id] = rule
        missing_searches = [
            support.card_search_id
            for support in supports
            if support.card_search_id not in copied_searches
        ]
        if missing_searches:
            raise _provider_error(
                "support-card-search-missing",
                f"missing search rules: {', '.join(missing_searches)}",
            )

        canonical_used = _canonical_ids(tuple(self.used_support_ids), supports)
        object.__setattr__(self, "supports", supports)
        object.__setattr__(self, "support_card_searches", MappingProxyType(copied_searches))
        object.__setattr__(self, "used_support_ids", canonical_used)

    @classmethod
    def from_support_payloads(
        cls,
        *,
        lesson_type: str,
        random_state: int,
        rng_authority: str,
        supports: Mapping[str, Mapping[str, Any]],
        support_card_searches: Mapping[str, ProduceCardSearchRule],
        used_support_ids: Sequence[str] = (),
    ) -> "Plan1HandAddSupportLoadout":
        """Build typed inputs from a caller response, failing closed on gaps.

        This is the only raw-payload entry point.  In particular, a missing
        ``runtime_permil`` is reported explicitly instead of silently using a
        default value.
        """

        if not isinstance(supports, Mapping) or not supports:
            raise _provider_error(
                "support-loadout-identity-missing",
                "caller support payload must be a non-empty ID-keyed mapping",
            )
        typed_supports: list[SupportUpgradeRuntimeInput] = []
        for support_id, payload in supports.items():
            if not isinstance(support_id, str) or not support_id:
                raise _provider_error("support-loadout-identity-missing", "support ID is empty")
            if not isinstance(payload, Mapping):
                raise _provider_error("support-loadout-input-invalid", f"payload for {support_id} is not a mapping")
            if "runtime_permil" not in payload:
                raise _provider_error(
                    "support-runtime-permil-missing",
                    f"runtime permil missing for {support_id}",
                )
            try:
                typed_supports.append(SupportUpgradeRuntimeInput.from_keyed_dict(support_id, payload))
            except (TypeError, ValueError) as exc:
                raise _provider_error(
                    "support-loadout-input-invalid",
                    f"invalid support payload for {support_id}: {exc}",
                ) from exc
        typed_supports.sort(key=lambda support: support.loadout_order)
        return cls(
            supports=tuple(typed_supports),
            support_card_searches=support_card_searches,
            lesson_type=lesson_type,
            random_state=random_state,
            rng_authority=rng_authority,
            used_support_ids=tuple(used_support_ids),
        )

    # Alias kept descriptive for callers that name the source a caller response.
    from_caller_payloads = from_support_payloads


@dataclass(frozen=True, slots=True)
class Plan1HandAddSupportRoundRecord:
    """Auditable per-request/per-round support usage and RNG transition."""

    round_index: int
    effect_id: str
    effect_index: int
    random_state_before: int
    random_state_after: int
    used_support_ids_before: tuple[str, ...]
    used_support_ids_after: tuple[str, ...]
    drawn_guids: tuple[str, ...]
    consumed_support_ids: tuple[str, ...]
    applied_support_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_index": self.round_index,
            "effect_id": self.effect_id,
            "effect_index": self.effect_index,
            "random_state_before": self.random_state_before,
            "random_state_after": self.random_state_after,
            "used_support_ids_before": list(self.used_support_ids_before),
            "used_support_ids_after": list(self.used_support_ids_after),
            "drawn_guids": list(self.drawn_guids),
            "consumed_support_ids": list(self.consumed_support_ids),
            "applied_support_ids": list(self.applied_support_ids),
        }


class Plan1HandAddSupportProvider:
    """Stateful callback adapter for ``Plan1HandAddSupportRequest``.

    The provider enforces one authoritative RNG chain.  Used support IDs are
    validated against the caller's loadout and are intentionally accepted as
    per-round caller state, so a new round can start with an empty tuple.
    """

    def __init__(self, loadout: Plan1HandAddSupportLoadout) -> None:
        if not isinstance(loadout, Plan1HandAddSupportLoadout):
            raise _provider_error("support-loadout-input-invalid", "loadout must be Plan1HandAddSupportLoadout")
        self.loadout = loadout
        self._next_random_state = loadout.random_state
        self._last_used_support_ids = loadout.used_support_ids
        self._round_records: list[Plan1HandAddSupportRoundRecord] = []

    @property
    def callback(self) -> Plan1HandAddSupportResolver:
        return self

    @property
    def current_random_state(self) -> int:
        return self._next_random_state

    @property
    def used_support_ids(self) -> tuple[str, ...]:
        return self._last_used_support_ids

    @property
    def round_records(self) -> tuple[Plan1HandAddSupportRoundRecord, ...]:
        return tuple(self._round_records)

    # Descriptive alias for consumers that call the data a usage ledger.
    @property
    def usage_records(self) -> tuple[Plan1HandAddSupportRoundRecord, ...]:
        return self.round_records

    def __call__(self, request: Plan1HandAddSupportRequest):
        if not isinstance(request, Plan1HandAddSupportRequest):
            raise _provider_error("support-request-type-invalid", "request must be Plan1HandAddSupportRequest")
        if request.random_state != self._next_random_state:
            raise _provider_error(
                "support-rng-not-chained",
                f"expected RNG state {self._next_random_state}, got {request.random_state}",
            )
        request_used = _canonical_ids(request.used_support_ids, self.loadout.supports)
        if request_used != tuple(request.used_support_ids):
            raise _provider_error(
                "support-used-ids-order-invalid",
                "used support IDs must follow caller loadout order",
            )
        try:
            result = evaluate_native_hand_add_support(
                request.drawn_cards,
                lesson_type=self.loadout.lesson_type,
                random_state=request.random_state,
                support_upgrades=self.loadout.supports,
                support_card_searches=self.loadout.support_card_searches,
                used_support_ids=request_used,
            )
        except NativeHandAddSupportError as exc:
            raise _provider_error("support-native-blocker", f"{exc.code}: {exc.detail}") from exc

        expected_cards = tuple(
            (card.guid, card.card_id, card.base_upgrade, card.effective_upgrade)
            for card in request.drawn_cards
        )
        actual_cards = tuple(
            (card.guid, card.card_id, card.base_upgrade, card.initial_effective_upgrade)
            for card in result.cards
        )
        if expected_cards != actual_cards:
            raise _provider_error(
                "support-card-identity-mismatch",
                "native HandAdd result changed caller card identity or order",
            )
        if result.initial_random_state != request.random_state:
            raise _provider_error("support-rng-state-mismatch", "native result initial RNG differs from request")

        result_used = _canonical_ids(result.used_support_ids, self.loadout.supports)
        consumed = tuple(support_id for support_id in result_used if support_id not in request_used)
        applied = tuple(
            support_id
            for card in result.cards
            for support_id in card.added_support_ids
        )
        record = Plan1HandAddSupportRoundRecord(
            round_index=len(self._round_records),
            effect_id=request.effect_id,
            effect_index=request.effect_index,
            random_state_before=request.random_state,
            random_state_after=result.final_random_state,
            used_support_ids_before=request_used,
            used_support_ids_after=result_used,
            drawn_guids=tuple(card.guid for card in request.drawn_cards),
            consumed_support_ids=consumed,
            applied_support_ids=applied,
        )
        self._round_records.append(record)
        self._next_random_state = result.final_random_state
        self._last_used_support_ids = result_used
        return result


def build_plan1_hand_add_support_provider(
    loadout: Plan1HandAddSupportLoadout,
) -> Plan1HandAddSupportProvider:
    """Build a deterministic callback provider from typed caller inputs."""

    return Plan1HandAddSupportProvider(loadout)


def project_plan1_hand_add_support_resolver(
    loadout: Plan1HandAddSupportLoadout,
) -> Plan1HandAddSupportResolver:
    """Project the provider directly to the existing stage callback shape."""

    return build_plan1_hand_add_support_provider(loadout)


__all__ = [
    "Plan1HandAddSupportProviderError",
    "Plan1HandAddSupportLoadout",
    "Plan1HandAddSupportRoundRecord",
    "Plan1HandAddSupportProvider",
    "build_plan1_hand_add_support_provider",
    "project_plan1_hand_add_support_resolver",
]
