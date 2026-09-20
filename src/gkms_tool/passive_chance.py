"""Exact chance nodes for probabilistic memory/support passive rules.

The PC Master stores one activation rate beside every ProduceSkill rule slot.
Android v3.2.3 exposes those slots in numeric order, but the client does not
contain the server-side RNG draw/consumption implementation.  Consequently
this module represents one marginal activation as an exact two-way chance
node and deliberately refuses to compose multiple matching sources into an
independent joint distribution.

``activationRatePermil == 0`` is the Master sentinel for deterministic
activation and remains owned by :mod:`passive_runtime`.  This module accepts
the observed probabilistic domain ``1..999`` and preserves the unreduced
``rate / 1000`` fraction for replay and serialization.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from .passive_catalog import (
    SOURCE_MEMORY,
    SOURCE_SUPPORT,
    PassiveRule,
    PassiveSource,
)


EFFECT_EXAM_STATUS_ENCHANT = "ProduceEffectType_ExamStatusEnchant"
EFFECT_STAMINA_RECOVER_FIX = "ProduceEffectType_StaminaRecoverFix"

PHASE_END_LESSON = "ProducePhaseType_EndLesson"
PHASE_START_LESSON = "ProducePhaseType_StartLesson"
PHASE_START_AUDITION_MID1 = "ProducePhaseType_StartAuditionMid1"
PHASE_START_AUDITION_MID2 = "ProducePhaseType_StartAuditionMid2"
PHASE_START_AUDITION_FINAL = "ProducePhaseType_StartAuditionFinal"

_LESSON_STATUS_TRIGGER = re.compile(
    r"^p_trigger-start_lesson-lesson_(?:vocal|dance|visual)(?:_sp)?$"
)
_END_LESSON_STAMINA_TRIGGER = re.compile(
    r"^p_trigger-end_lesson-lesson_(?:vocal|dance|visual)$"
)
_AUDITION_STATUS_TRIGGER_BY_PHASE: dict[str, frozenset[str]] = {
    PHASE_START_AUDITION_MID1: frozenset({"p_trigger-start_audition_mid1"}),
    PHASE_START_AUDITION_MID2: frozenset({"p_trigger-start_audition_mid2"}),
    PHASE_START_AUDITION_FINAL: frozenset({"p_trigger-start_audition_final"}),
}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _usage_key(source: PassiveSource, source_index: int) -> str:
    """Mirror passive_runtime's stable key without creating an import cycle."""

    parent = source.parent_id or "-"
    order = "-" if source.slot_order is None else str(source.slot_order)
    level = "-" if source.skill_level is None else str(source.skill_level)
    return (
        f"passive[{source_index}]|{source.source_kind}|{source.source_id}|"
        f"{parent}|{order}|{source.skill_id}|{level}"
    )


@dataclass(frozen=True, slots=True)
class ExactProbability:
    """A JSON-safe exact rational retaining the original permil scale."""

    numerator: int
    denominator: int = 1000

    def __post_init__(self) -> None:
        if not _is_int(self.numerator) or not _is_int(self.denominator):
            raise ValueError("probability terms must be integers")
        if self.denominator <= 0:
            raise ValueError("probability denominator must be positive")
        if self.numerator < 0 or self.numerator > self.denominator:
            raise ValueError("probability must be between zero and one")

    def complement(self) -> "ExactProbability":
        return ExactProbability(self.denominator - self.numerator, self.denominator)

    def to_dict(self) -> dict[str, int]:
        return {
            "numerator": self.numerator,
            "denominator": self.denominator,
        }


@dataclass(frozen=True, slots=True)
class PassiveChanceEffect:
    """Fixed Master effect carried to the event scheduler on a hit branch."""

    effect_id: str
    effect_type: str
    effect_value: int
    produce_resource_type: str | None
    status_enchant_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "effect_id": self.effect_id,
            "effect_type": self.effect_type,
            "effect_value": self.effect_value,
            "produce_resource_type": self.produce_resource_type,
            "status_enchant_id": self.status_enchant_id,
        }


@dataclass(frozen=True, slots=True)
class PassiveChanceContract:
    """One ordered ProduceSkill slot with an exact activation gate."""

    contract_id: str
    source_index: int
    source_kind: str
    source_id: str
    skill_id: str
    rule_slot: int
    trigger_id: str
    trigger_phase_type: str
    activation_rate_permil: int
    usage_key: str
    activation_limit: int
    use_count: int
    effect: PassiveChanceEffect

    @property
    def activation_probability(self) -> ExactProbability:
        return ExactProbability(self.activation_rate_permil, 1000)

    @property
    def unlimited(self) -> bool:
        return self.activation_limit == 0

    @property
    def remaining_uses(self) -> int | None:
        if self.unlimited:
            return None
        return max(0, self.activation_limit - self.use_count)

    @property
    def can_activate(self) -> bool:
        remaining = self.remaining_uses
        return remaining is None or remaining > 0

    @property
    def order_key(self) -> tuple[int, int]:
        return self.source_index, self.rule_slot

    def with_use_count(self, use_count: int) -> "PassiveChanceContract":
        if not _is_int(use_count) or use_count < 0:
            raise ValueError("use_count must be a non-negative integer")
        return replace(self, use_count=use_count)

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_id": self.contract_id,
            "source_index": self.source_index,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "skill_id": self.skill_id,
            "rule_slot": self.rule_slot,
            "trigger_id": self.trigger_id,
            "trigger_phase_type": self.trigger_phase_type,
            "activation_rate_permil": self.activation_rate_permil,
            "activation_probability": self.activation_probability.to_dict(),
            "usage_key": self.usage_key,
            "activation_limit": self.activation_limit,
            "use_count": self.use_count,
            "effect": self.effect.to_dict(),
            # The exact server RNG draw index/consumption is not present in
            # Android client code.  A scheduler must never infer a joint draw
            # order from this local slot order.
            "rng_contract": "single-marginal-only",
        }


@dataclass(frozen=True, slots=True)
class PassiveChanceBranch:
    outcome: str
    probability: ExactProbability
    activated: bool
    usage_key: str
    prior_use_count: int
    next_use_count: int
    effect: PassiveChanceEffect | None

    def to_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "probability": self.probability.to_dict(),
            "activated": self.activated,
            "usage_key": self.usage_key,
            "prior_use_count": self.prior_use_count,
            "next_use_count": self.next_use_count,
            "effect": self.effect.to_dict() if self.effect is not None else None,
        }


@dataclass(frozen=True, slots=True)
class PassiveChanceExpansion:
    contract_id: str
    branches: tuple[PassiveChanceBranch, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_id": self.contract_id,
            "branches": [branch.to_dict() for branch in self.branches],
        }


@dataclass(frozen=True, slots=True)
class PassiveChanceBlock:
    code: str
    reason: str
    source_index: int
    source_kind: str
    source_id: str
    skill_id: str
    rule_slot: int | None
    trigger_id: str
    effect_id: str
    activation_rate_permil: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "reason": self.reason,
            "source_index": self.source_index,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "skill_id": self.skill_id,
            "rule_slot": self.rule_slot,
            "trigger_id": self.trigger_id,
            "effect_id": self.effect_id,
            "activation_rate_permil": self.activation_rate_permil,
        }


class UnsupportedPassiveChanceError(RuntimeError):
    def __init__(self, blockers: Sequence[PassiveChanceBlock] | Sequence[str]) -> None:
        self.blockers = tuple(blockers)
        labels = [
            block.code if isinstance(block, PassiveChanceBlock) else str(block)
            for block in self.blockers
        ]
        super().__init__("passive chance is blocked: " + ", ".join(labels))


@dataclass(frozen=True, slots=True)
class PassiveChanceRuntimeResult:
    contracts: tuple[PassiveChanceContract, ...]
    blocking_rules: tuple[PassiveChanceBlock, ...]

    @property
    def fully_supported(self) -> bool:
        return not self.blocking_rules

    @property
    def blocking_codes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(block.code for block in self.blocking_rules))

    def require_supported(self) -> None:
        if self.blocking_rules:
            raise UnsupportedPassiveChanceError(self.blocking_rules)

    def to_dict(self) -> dict[str, object]:
        return {
            "contracts": [contract.to_dict() for contract in self.contracts],
            "blocking_rules": [block.to_dict() for block in self.blocking_rules],
        }


def _block(
    code: str,
    reason: str,
    *,
    source_index: int,
    source: PassiveSource,
    rule: PassiveRule | None,
) -> PassiveChanceBlock:
    return PassiveChanceBlock(
        code=code,
        reason=reason,
        source_index=source_index,
        source_kind=source.source_kind,
        source_id=source.source_id,
        skill_id=source.skill_id,
        rule_slot=rule.slot if rule is not None else None,
        trigger_id=rule.trigger_id if rule is not None else "",
        effect_id=rule.effect_id if rule is not None else "",
        activation_rate_permil=(
            rule.activation_rate_permil if rule is not None else None
        ),
    )


def _allowed_catalog_reason(reason: str, rule: PassiveRule) -> bool:
    return reason in {
        f"probabilistic-activation-rate:{rule.activation_rate_permil}",
        f"rule-not-whitelisted:{rule.trigger_id}:{rule.effect_type or 'unknown'}",
    }


def _status_trigger_supported(rule: PassiveRule) -> bool:
    if rule.trigger_phase_type == PHASE_START_LESSON:
        return _LESSON_STATUS_TRIGGER.fullmatch(rule.trigger_id) is not None
    return rule.trigger_id in _AUDITION_STATUS_TRIGGER_BY_PHASE.get(
        rule.trigger_phase_type or "", frozenset()
    )


def _effect_from_rule(rule: PassiveRule) -> PassiveChanceEffect | str:
    if not rule.effect_id or not rule.effect_type:
        return "missing effect identity/type"
    if (
        not _is_int(rule.effect_value_min)
        or not _is_int(rule.effect_value_max)
        or rule.effect_value_min != rule.effect_value_max
    ):
        return (
            f"effectRange={rule.effect_value_min!r}.."
            f"{rule.effect_value_max!r}"
        )

    if rule.effect_type == EFFECT_EXAM_STATUS_ENCHANT:
        if not _status_trigger_supported(rule):
            return (
                f"unsupported status trigger={rule.trigger_id}:"
                f"{rule.trigger_phase_type or 'unknown'}"
            )
        if rule.effect_value_min != 0:
            return (
                f"status enchant effectRange={rule.effect_value_min}.."
                f"{rule.effect_value_max}; expected 0..0"
            )
        effect_raw = rule.raw.get("effect")
        status_id = (
            effect_raw.get("produceExamStatusEnchantId")
            if isinstance(effect_raw, Mapping)
            else None
        )
        if not isinstance(status_id, str) or not status_id:
            return "ProduceEffect.produceExamStatusEnchantId is empty"
        return PassiveChanceEffect(
            effect_id=rule.effect_id,
            effect_type=rule.effect_type,
            effect_value=0,
            produce_resource_type=rule.produce_resource_type,
            status_enchant_id=status_id,
        )

    if rule.effect_type == EFFECT_STAMINA_RECOVER_FIX:
        if (
            rule.trigger_phase_type != PHASE_END_LESSON
            or _END_LESSON_STAMINA_TRIGGER.fullmatch(rule.trigger_id) is None
        ):
            return (
                f"unsupported stamina trigger={rule.trigger_id}:"
                f"{rule.trigger_phase_type or 'unknown'}"
            )
        return PassiveChanceEffect(
            effect_id=rule.effect_id,
            effect_type=rule.effect_type,
            effect_value=int(rule.effect_value_min),
            produce_resource_type=rule.produce_resource_type,
        )

    return f"unsupported probabilistic effect type={rule.effect_type}"


def resolve_passive_chance_runtime(
    sources: Sequence[PassiveSource],
    *,
    usage_counts: Mapping[str, int] | None = None,
) -> PassiveChanceRuntimeResult:
    """Resolve nonzero-rate rules without inventing a joint RNG model."""

    if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence):
        raise TypeError("sources must be a sequence of PassiveSource")
    normalized_usage: dict[str, int] = {}
    for key, value in (usage_counts or {}).items():
        if not isinstance(key, str) or not key:
            raise ValueError("usage count keys must be non-empty strings")
        if not _is_int(value) or value < 0:
            raise ValueError(f"usage count must be non-negative: {key}")
        normalized_usage[key] = value

    contracts: list[PassiveChanceContract] = []
    blocks: list[PassiveChanceBlock] = []
    for source_index, source in enumerate(sources):
        if not isinstance(source, PassiveSource):
            raise TypeError("sources must contain PassiveSource")
        if not all(isinstance(rule, PassiveRule) for rule in source.rules):
            raise TypeError("PassiveSource.rules must contain PassiveRule")
        probabilistic = tuple(
            rule for rule in source.rules if rule.activation_rate_permil != 0
        )
        if not probabilistic:
            continue
        usage_key = _usage_key(source, source_index)
        use_count = normalized_usage.get(usage_key, 0)

        if source.unsupported_rules:
            blocks.append(
                _block(
                    "unsupported-source",
                    ";".join(source.unsupported_rules),
                    source_index=source_index,
                    source=source,
                    rule=None,
                )
            )
            continue
        if source.source_kind not in {SOURCE_MEMORY, SOURCE_SUPPORT}:
            blocks.append(
                _block(
                    "unsupported-source-kind",
                    source.source_kind,
                    source_index=source_index,
                    source=source,
                    rule=None,
                )
            )
            continue
        if not _is_int(source.activation_count) or source.activation_count < 0:
            blocks.append(
                _block(
                    "invalid-activation-count",
                    f"activationCount={source.activation_count!r}",
                    source_index=source_index,
                    source=source,
                    rule=None,
                )
            )
            continue
        if source.activation_count > 0 and use_count > source.activation_count:
            blocks.append(
                _block(
                    "usage-exceeds-activation-limit",
                    f"useCount={use_count}>{source.activation_count}",
                    source_index=source_index,
                    source=source,
                    rule=None,
                )
            )
            continue

        trigger_counts: dict[tuple[str, str | None], int] = {}
        for rule in probabilistic:
            key = (rule.trigger_id, rule.trigger_phase_type)
            trigger_counts[key] = trigger_counts.get(key, 0) + 1

        for rule in sorted(probabilistic, key=lambda item: item.slot):
            rate = rule.activation_rate_permil
            if not _is_int(rate) or rate < 1 or rate > 999:
                blocks.append(
                    _block(
                        "invalid-probabilistic-rate",
                        f"activationRatePermil={rate!r}; expected 1..999",
                        source_index=source_index,
                        source=source,
                        rule=rule,
                    )
                )
                continue
            if trigger_counts[(rule.trigger_id, rule.trigger_phase_type)] > 1:
                blocks.append(
                    _block(
                        "unknown-shared-source-rng-order",
                        (
                            "multiple probabilistic slots in one source share "
                            f"trigger {rule.trigger_id}"
                        ),
                        source_index=source_index,
                        source=source,
                        rule=rule,
                    )
                )
                continue
            unknown_reasons = tuple(
                reason
                for reason in rule.unsupported_rules
                if not _allowed_catalog_reason(reason, rule)
            )
            if unknown_reasons:
                blocks.append(
                    _block(
                        "catalog-rule-block",
                        ";".join(unknown_reasons),
                        source_index=source_index,
                        source=source,
                        rule=rule,
                    )
                )
                continue
            effect = _effect_from_rule(rule)
            if isinstance(effect, str):
                code = (
                    "unknown-effect-range"
                    if effect.startswith("effectRange=")
                    else "unsupported-chance-shape"
                )
                blocks.append(
                    _block(
                        code,
                        effect,
                        source_index=source_index,
                        source=source,
                        rule=rule,
                    )
                )
                continue
            contract_id = f"{usage_key}|rule[{rule.slot}]"
            contracts.append(
                PassiveChanceContract(
                    contract_id=contract_id,
                    source_index=source_index,
                    source_kind=source.source_kind,
                    source_id=source.source_id,
                    skill_id=source.skill_id,
                    rule_slot=rule.slot,
                    trigger_id=rule.trigger_id,
                    trigger_phase_type=rule.trigger_phase_type or "",
                    activation_rate_permil=rate,
                    usage_key=usage_key,
                    activation_limit=int(source.activation_count),
                    use_count=use_count,
                    effect=effect,
                )
            )

    contracts.sort(key=lambda item: item.order_key)
    return PassiveChanceRuntimeResult(tuple(contracts), tuple(blocks))


def expand_passive_chance(
    contract: PassiveChanceContract,
    *,
    usage_counts: Mapping[str, int] | None = None,
) -> PassiveChanceExpansion:
    """Expand one chance contract into exact hit/miss branches.

    This function is intentionally local to one contract.  It consumes no RNG
    seed and does not claim independence from any other passive.
    """

    if not isinstance(contract, PassiveChanceContract):
        raise TypeError("contract must be PassiveChanceContract")
    current = contract.use_count
    if usage_counts is not None and contract.usage_key in usage_counts:
        candidate = usage_counts[contract.usage_key]
        if not _is_int(candidate) or candidate < 0:
            raise ValueError("usage count must be a non-negative integer")
        current = int(candidate)
    active_contract = contract.with_use_count(current)
    if not active_contract.can_activate:
        return PassiveChanceExpansion(
            contract_id=contract.contract_id,
            branches=(
                PassiveChanceBranch(
                    outcome="activation-limit-exhausted",
                    probability=ExactProbability(1000, 1000),
                    activated=False,
                    usage_key=contract.usage_key,
                    prior_use_count=current,
                    next_use_count=current,
                    effect=None,
                ),
            ),
        )

    hit = active_contract.activation_probability
    return PassiveChanceExpansion(
        contract_id=contract.contract_id,
        branches=(
            PassiveChanceBranch(
                outcome="activated",
                probability=hit,
                activated=True,
                usage_key=contract.usage_key,
                prior_use_count=current,
                next_use_count=current + 1,
                effect=contract.effect,
            ),
            PassiveChanceBranch(
                outcome="not-activated",
                probability=hit.complement(),
                activated=False,
                usage_key=contract.usage_key,
                prior_use_count=current,
                next_use_count=current,
                effect=None,
            ),
        ),
    )


def expand_single_passive_chance_event(
    runtime: PassiveChanceRuntimeResult,
    *,
    trigger_id: str,
    trigger_phase_type: str,
    usage_counts: Mapping[str, int] | None = None,
) -> PassiveChanceExpansion | None:
    """Expand an event only when exactly one source matches.

    Cross-source RNG order/independence is server-owned and unrecovered.  A
    Plan3 scheduler should eventually invoke ``expand_passive_chance`` for an
    already ordered source occurrence; this convenience function refuses an
    ambiguous multi-source event rather than multiplying marginals.
    """

    if not isinstance(runtime, PassiveChanceRuntimeResult):
        raise TypeError("runtime must be PassiveChanceRuntimeResult")
    runtime.require_supported()
    matches = tuple(
        contract
        for contract in runtime.contracts
        if contract.trigger_id == trigger_id
        and contract.trigger_phase_type == trigger_phase_type
    )
    if not matches:
        return None
    if len(matches) != 1:
        raise UnsupportedPassiveChanceError(
            (
                "unknown-cross-source-rng-order:"
                f"{trigger_phase_type}:{trigger_id}:{len(matches)}",
            )
        )
    return expand_passive_chance(matches[0], usage_counts=usage_counts)
