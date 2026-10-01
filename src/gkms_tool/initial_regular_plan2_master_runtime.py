"""Master-backed Plan2 runtime provider for Initial Regular inner stages.

Outer item session references are treated as exact Master ProduceItem IDs.
Every item is loaded and compiled through the existing typed item runtime;
the complete ordered rule set is then compiled once more so listener UIDs are
allocated by the single native owner.  Drinks and passives have no equivalent
fresh-stage runtime compiler yet and therefore remain explicit blockers.

This module is deliberately only a scenario provider.  It does not discover
live state, synthesize provenance, or infer consumed persistent loadout state.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, TypeAlias

from .initial_regular_inner_protocol import InitialRegularInnerStageRequest
from .item_rules import EquippedItemRule, load_item_rule
from .plan2_native_horizon import Plan2NativeBlocker
from .plan2_native_item_runtime import (
    Plan2NativeItemCompilation,
    Plan2NativeItemRuntime,
    compile_plan2_native_item_rules,
)
from .plan2_native_stage_bootstrap import Plan2NativeStageRuntime


PLAN2_MASTER_ITEM_RUNTIME_ADAPTER_ID: Final = (
    "initial-regular.plan2.master-item-rules.v1"
)

Plan2MasterItemRuleLoader: TypeAlias = Callable[[str], EquippedItemRule]
Plan2MasterItemRuleCompiler: TypeAlias = Callable[
    [Sequence[EquippedItemRule]], Plan2NativeItemCompilation
]


def _block(
    blockers: list[Plan2NativeBlocker],
    code: str,
    detail: str = "",
) -> None:
    blocker = Plan2NativeBlocker(code, detail)
    if blocker not in blockers:
        blockers.append(blocker)


class InitialRegularPlan2MasterRuntimeBlocked(RuntimeError):
    """Adapter-call boundary error retaining every typed provision blocker."""

    def __init__(self, blockers: Sequence[Plan2NativeBlocker]) -> None:
        resolved = tuple(blockers)
        if not resolved or any(
            not isinstance(value, Plan2NativeBlocker) for value in resolved
        ):
            raise TypeError("blockers must contain typed Plan2NativeBlocker values")
        self.blockers = resolved
        rendered = ";".join(
            value.code if not value.detail else f"{value.code}:{value.detail}"
            for value in resolved
        )
        super().__init__(rendered)


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2MasterRuntimeProvision:
    """Atomic result: an exact runtime or typed blockers, never both."""

    runtime: Plan2NativeStageRuntime | None = None
    blockers: tuple[Plan2NativeBlocker, ...] = ()

    def __post_init__(self) -> None:
        blockers = tuple(self.blockers)
        if any(not isinstance(value, Plan2NativeBlocker) for value in blockers):
            raise TypeError("blockers must contain typed Plan2NativeBlocker values")
        if self.runtime is not None and not isinstance(
            self.runtime, Plan2NativeStageRuntime
        ):
            raise TypeError("runtime must be Plan2NativeStageRuntime or None")
        if (self.runtime is None) == (not blockers):
            raise ValueError("provision must contain exactly one of runtime or blockers")
        object.__setattr__(self, "blockers", blockers)

    @property
    def supported(self) -> bool:
        return self.runtime is not None and not self.blockers

    def require_runtime(self) -> Plan2NativeStageRuntime:
        """Return the injectable runtime or raise with the original blockers."""

        if not self.supported:
            raise InitialRegularPlan2MasterRuntimeBlocked(self.blockers)
        assert self.runtime is not None
        return self.runtime


def _compilation_blockers(
    compilation: Plan2NativeItemCompilation,
    *,
    item_id: str,
) -> tuple[Plan2NativeBlocker, ...]:
    if compilation.supported:
        return ()
    if not compilation.blockers:
        return (
            Plan2NativeBlocker(
                "master-runtime-item-compile-result-invalid",
                item_id,
            ),
        )
    return tuple(
        Plan2NativeBlocker(
            "master-runtime-item-unsupported",
            f"{item_id}:{detail}",
        )
        for detail in compilation.blockers
    )


def _validate_composed_runtime(
    runtime: Plan2NativeItemRuntime,
    item_refs: tuple[str, ...],
) -> tuple[Plan2NativeBlocker, ...]:
    """Prove the combined compiler retained sources and allocated one UID space."""

    blockers: list[Plan2NativeBlocker] = []
    source_ids = tuple(value.item_id for value in runtime.sources)
    if source_ids != item_refs:
        _block(
            blockers,
            "master-runtime-item-source-order-mismatch",
            f"expected={item_refs!r};actual={source_ids!r}",
        )
    listener_uids = runtime.active_status_uids
    expected_uids = tuple(range(1, len(runtime.listeners) + 1))
    if listener_uids != expected_uids:
        _block(
            blockers,
            "master-runtime-item-uid-allocation-unsafe",
            f"expected={expected_uids!r};actual={listener_uids!r}",
        )
    expected_next_uid = len(runtime.listeners) + 1
    if runtime.next_status_uid != expected_next_uid:
        _block(
            blockers,
            "master-runtime-item-next-uid-unsafe",
            f"expected={expected_next_uid};actual={runtime.next_status_uid}",
        )
    return tuple(blockers)


def provision_initial_regular_plan2_master_runtime(
    request: InitialRegularInnerStageRequest,
    *,
    item_rule_loader: Plan2MasterItemRuleLoader = load_item_rule,
    item_rule_compiler: Plan2MasterItemRuleCompiler = (
        compile_plan2_native_item_rules
    ),
) -> InitialRegularPlan2MasterRuntimeProvision:
    """Compile exact outer loadout references into one fresh Plan2 runtime.

    The returned envelope is useful at registry/provisioning boundaries where
    blockers must be inspected without throwing.  Use
    :class:`InitialRegularPlan2MasterRuntimeProvider` directly as the existing
    inner adapter's callable ``runtime_provider``.
    """

    if not isinstance(request, InitialRegularInnerStageRequest):
        raise TypeError("request must be InitialRegularInnerStageRequest")
    if not callable(item_rule_loader):
        raise TypeError("item_rule_loader must be callable")
    if not callable(item_rule_compiler):
        raise TypeError("item_rule_compiler must be callable")

    refs = request.runtime_refs
    item_refs = tuple(refs.item_session_refs)
    drink_refs = tuple(refs.drink_session_refs)
    passive_refs = tuple(refs.passive_session_refs)
    blockers: list[Plan2NativeBlocker] = []

    if drink_refs:
        _block(
            blockers,
            "master-runtime-drink-unresolved",
            repr(drink_refs),
        )
    if passive_refs:
        _block(
            blockers,
            "master-runtime-passive-unresolved",
            repr(passive_refs),
        )

    rules: list[EquippedItemRule] = []
    for item_ref in item_refs:
        try:
            rule = item_rule_loader(item_ref)
        except Exception as error:
            _block(
                blockers,
                "master-runtime-item-unavailable",
                f"{item_ref}:{type(error).__name__}",
            )
            continue
        if not isinstance(rule, EquippedItemRule):
            _block(
                blockers,
                "master-runtime-item-loader-result-invalid",
                f"{item_ref}:{type(rule).__name__}",
            )
            continue
        if rule.id != item_ref:
            _block(
                blockers,
                "master-runtime-item-id-mismatch",
                f"expected={item_ref};actual={rule.id}",
            )
            continue
        rules.append(rule)
        try:
            individual = item_rule_compiler((rule,))
        except Exception as error:
            _block(
                blockers,
                "master-runtime-item-compile-failed",
                f"{item_ref}:{type(error).__name__}:{error}",
            )
            continue
        if not isinstance(individual, Plan2NativeItemCompilation):
            _block(
                blockers,
                "master-runtime-item-compile-result-invalid",
                f"{item_ref}:{type(individual).__name__}",
            )
            continue
        for blocker in _compilation_blockers(individual, item_id=item_ref):
            if blocker not in blockers:
                blockers.append(blocker)

    if len(rules) != len(item_refs) or blockers:
        return InitialRegularPlan2MasterRuntimeProvision(
            blockers=tuple(blockers)
        )

    if not rules:
        return InitialRegularPlan2MasterRuntimeProvision(
            runtime=Plan2NativeStageRuntime()
        )

    try:
        combined = item_rule_compiler(tuple(rules))
    except Exception as error:
        return InitialRegularPlan2MasterRuntimeProvision(
            blockers=(
                Plan2NativeBlocker(
                    "master-runtime-item-composition-failed",
                    f"{type(error).__name__}:{error}",
                ),
            )
        )
    if not isinstance(combined, Plan2NativeItemCompilation):
        return InitialRegularPlan2MasterRuntimeProvision(
            blockers=(
                Plan2NativeBlocker(
                    "master-runtime-item-composition-result-invalid",
                    type(combined).__name__,
                ),
            )
        )
    if not combined.supported or combined.runtime is None:
        combined_blockers = _compilation_blockers(
            combined,
            item_id="combined",
        )
        return InitialRegularPlan2MasterRuntimeProvision(
            blockers=combined_blockers
        )

    composition_blockers = _validate_composed_runtime(
        combined.runtime,
        item_refs,
    )
    if composition_blockers:
        return InitialRegularPlan2MasterRuntimeProvision(
            blockers=composition_blockers
        )
    return InitialRegularPlan2MasterRuntimeProvision(
        runtime=Plan2NativeStageRuntime(
            item_runtime=combined.runtime,
            resolved_item_session_refs=item_refs,
            runtime_adapter_ids=(PLAN2_MASTER_ITEM_RUNTIME_ADAPTER_ID,),
        )
    )


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2MasterRuntimeProvider:
    """Callable success-path provider accepted by ``InitialRegularPlan2InnerAdapter``."""

    item_rule_loader: Plan2MasterItemRuleLoader = load_item_rule
    item_rule_compiler: Plan2MasterItemRuleCompiler = (
        compile_plan2_native_item_rules
    )

    def __post_init__(self) -> None:
        if not callable(self.item_rule_loader):
            raise TypeError("item_rule_loader must be callable")
        if not callable(self.item_rule_compiler):
            raise TypeError("item_rule_compiler must be callable")

    def provision(
        self,
        request: InitialRegularInnerStageRequest,
    ) -> InitialRegularPlan2MasterRuntimeProvision:
        return provision_initial_regular_plan2_master_runtime(
            request,
            item_rule_loader=self.item_rule_loader,
            item_rule_compiler=self.item_rule_compiler,
        )

    def __call__(
        self,
        request: InitialRegularInnerStageRequest,
    ) -> Plan2NativeStageRuntime:
        return self.provision(request).require_runtime()


def build_initial_regular_plan2_master_runtime_provider(
    *,
    item_rule_loader: Plan2MasterItemRuleLoader = load_item_rule,
    item_rule_compiler: Plan2MasterItemRuleCompiler = (
        compile_plan2_native_item_rules
    ),
) -> InitialRegularPlan2MasterRuntimeProvider:
    return InitialRegularPlan2MasterRuntimeProvider(
        item_rule_loader=item_rule_loader,
        item_rule_compiler=item_rule_compiler,
    )


__all__ = [
    "PLAN2_MASTER_ITEM_RUNTIME_ADAPTER_ID",
    "InitialRegularPlan2MasterRuntimeBlocked",
    "InitialRegularPlan2MasterRuntimeProvision",
    "InitialRegularPlan2MasterRuntimeProvider",
    "Plan2MasterItemRuleCompiler",
    "Plan2MasterItemRuleLoader",
    "build_initial_regular_plan2_master_runtime_provider",
    "provision_initial_regular_plan2_master_runtime",
]
