"""Offline runtime contract for selected memory/support passives.

``passive_catalog`` resolves the authoritative Master chains into normalized
``PassiveSource``/``PassiveRule`` objects.  This module performs the next,
deliberately small step:

* aggregate deterministic produce-start modifiers;
* defer lesson/audition status enchants as install contracts; and
* expose supported probabilistic rules as exact chance contracts; and
* turn every ranged, malformed, unknown, or unmodelled chance rule into a hard
  blocking diagnostic.

The resolver may expose supported partial values for diagnostics, but
``apply_run_modifiers`` refuses to apply them while any blocker remains.  This
keeps an unknown passive from being silently interpreted as a zero.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Any

from .passive_catalog import (
    EFFECT_DANCE_ADDITION,
    EFFECT_DANCE_GROWTH_RATE_ADDITION,
    EFFECT_MAX_STAMINA_ADDITION,
    EFFECT_PRODUCE_POINT_ADDITION,
    EFFECT_VISUAL_ADDITION,
    EFFECT_VISUAL_GROWTH_RATE_ADDITION,
    EFFECT_VOCAL_ADDITION,
    EFFECT_VOCAL_GROWTH_RATE_ADDITION,
    TRIGGER_PRODUCE_START_CONFIGURATION,
    TRIGGER_PRODUCE_START_INITIAL,
    PassiveRule,
    PassiveSource,
)
from .passive_chance import (
    PassiveChanceContract,
    resolve_passive_chance_runtime,
)


EFFECT_EXAM_STATUS_ENCHANT = "ProduceEffectType_ExamStatusEnchant"

EFFECT_LESSON_SP_RATE = "ProduceEffectType_LessonSpChangeRatePermilAddition"
EFFECT_LESSON_VOCAL_SP_RATE = (
    "ProduceEffectType_LessonVocalSpChangeRatePermilAddition"
)
EFFECT_LESSON_DANCE_SP_RATE = (
    "ProduceEffectType_LessonDanceSpChangeRatePermilAddition"
)
EFFECT_LESSON_VISUAL_SP_RATE = (
    "ProduceEffectType_LessonVisualSpChangeRatePermilAddition"
)
EFFECT_SUPPORT_EVENT_PARAMETER_UP = (
    "ProduceEffectType_SupportCardEventParameterAdditionValueUp"
)
EFFECT_SUPPORT_EVENT_POINT_UP = (
    "ProduceEffectType_SupportCardEventProducePointAdditionValueUp"
)
EFFECT_SUPPORT_EVENT_STAMINA_UP = (
    "ProduceEffectType_SupportCardEventStaminaRecoverUp"
)
EFFECT_SUPPORT_CARD_UPGRADE_RATE = (
    "ProduceEffectType_SupportCardProduceCardUpgradeProbabilityUp"
)

PHASE_PRODUCE_START = "ProducePhaseType_ProduceStart"
PHASE_START_LESSON = "ProducePhaseType_StartLesson"
PHASE_START_AUDITION = "ProducePhaseType_StartAudition"
PHASE_START_AUDITION_MID1 = "ProducePhaseType_StartAuditionMid1"
PHASE_START_AUDITION_MID2 = "ProducePhaseType_StartAuditionMid2"
PHASE_START_AUDITION_FINAL = "ProducePhaseType_StartAuditionFinal"

_LESSON_STATUS_TRIGGER = re.compile(
    r"^p_trigger-start_lesson-lesson_(?:vocal|dance|visual)(?:_sp)?$"
)
_STATUS_TRIGGER_IDS_BY_PHASE: dict[str, frozenset[str]] = {
    PHASE_START_AUDITION: frozenset(
        {"p_trigger-start_audition-for_hif_memory"}
    ),
    PHASE_START_AUDITION_MID1: frozenset(
        {"p_trigger-start_audition_mid1"}
    ),
    PHASE_START_AUDITION_MID2: frozenset(
        {"p_trigger-start_audition_mid2"}
    ),
    PHASE_START_AUDITION_FINAL: frozenset(
        {"p_trigger-start_audition_final"}
    ),
}

# Field names intentionally retain the units encoded by Master.  In
# particular, GrowthRateAddition is stored as percentage points while the SP
# and support-event values named "Permil" remain permil values.
_RUN_EFFECT_FIELDS: dict[tuple[str, str], str] = {
    (TRIGGER_PRODUCE_START_INITIAL, EFFECT_VOCAL_ADDITION): "vocal_addition",
    (TRIGGER_PRODUCE_START_INITIAL, EFFECT_DANCE_ADDITION): "dance_addition",
    (TRIGGER_PRODUCE_START_INITIAL, EFFECT_VISUAL_ADDITION): "visual_addition",
    (
        TRIGGER_PRODUCE_START_INITIAL,
        EFFECT_PRODUCE_POINT_ADDITION,
    ): "produce_point_addition",
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_MAX_STAMINA_ADDITION,
    ): "max_stamina_addition",
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_VOCAL_GROWTH_RATE_ADDITION,
    ): "vocal_growth_rate_addition",
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_DANCE_GROWTH_RATE_ADDITION,
    ): "dance_growth_rate_addition",
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_VISUAL_GROWTH_RATE_ADDITION,
    ): "visual_growth_rate_addition",
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_LESSON_SP_RATE,
    ): "lesson_sp_change_rate_permil_addition",
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_LESSON_VOCAL_SP_RATE,
    ): "lesson_vocal_sp_change_rate_permil_addition",
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_LESSON_DANCE_SP_RATE,
    ): "lesson_dance_sp_change_rate_permil_addition",
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_LESSON_VISUAL_SP_RATE,
    ): "lesson_visual_sp_change_rate_permil_addition",
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_SUPPORT_EVENT_PARAMETER_UP,
    ): "support_event_parameter_addition_value_up",
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_SUPPORT_EVENT_POINT_UP,
    ): "support_event_produce_point_addition_value_up",
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_SUPPORT_EVENT_STAMINA_UP,
    ): "support_event_stamina_recover_up",
    (
        TRIGGER_PRODUCE_START_CONFIGURATION,
        EFFECT_SUPPORT_CARD_UPGRADE_RATE,
    ): "support_card_upgrade_probability_up",
}

_RUN_STATE_FIELD_BY_MODIFIER = {
    "vocal_addition": "vocal",
    "dance_addition": "dance",
    "visual_addition": "visual",
    "produce_point_addition": "produce_points",
    "max_stamina_addition": "max_stamina",
}


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class RunModifiers:
    """Aggregate deterministic deltas in authoritative Master units."""

    vocal_addition: int = 0
    dance_addition: int = 0
    visual_addition: int = 0
    produce_point_addition: int = 0
    max_stamina_addition: int = 0
    vocal_growth_rate_addition: int = 0
    dance_growth_rate_addition: int = 0
    visual_growth_rate_addition: int = 0
    lesson_sp_change_rate_permil_addition: int = 0
    lesson_vocal_sp_change_rate_permil_addition: int = 0
    lesson_dance_sp_change_rate_permil_addition: int = 0
    lesson_visual_sp_change_rate_permil_addition: int = 0
    support_event_parameter_addition_value_up: int = 0
    support_event_produce_point_addition_value_up: int = 0
    support_event_stamina_recover_up: int = 0
    support_card_upgrade_probability_up: int = 0

    def as_dict(self) -> dict[str, int]:
        return {field.name: int(getattr(self, field.name)) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class AppliedPassiveRule:
    source_index: int
    source_kind: str
    source_id: str
    skill_id: str
    rule_slot: int
    effect_id: str
    trigger_id: str
    outcome: str
    value: int | str


@dataclass(frozen=True, slots=True)
class StatusEnchantInstallContract:
    """Deferred root-enchant installation owned by one passive source.

    ``activation_limit == 0`` is the Master convention for unlimited uses.
    Multiple rules from the same source share ``usage_key`` and ``use_count``.
    """

    source_index: int
    source_kind: str
    source_id: str
    skill_id: str
    rule_slot: int
    effect_id: str
    produce_trigger_id: str
    produce_trigger_phase_type: str
    status_enchant_id: str
    usage_key: str
    activation_limit: int
    use_count: int

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


@dataclass(frozen=True, slots=True)
class PassiveRuntimeBlock:
    """A safety blocker retaining the unresolved rule's raw scalar data."""

    code: str
    reason: str
    source_index: int
    source_kind: str
    source_id: str
    skill_id: str
    rule_slot: int | None
    effect_id: str
    effect_type: str | None
    effect_value_min: int | None
    effect_value_max: int | None
    trigger_id: str
    trigger_phase_type: str | None
    activation_rate_permil: int | None


class UnsupportedPassiveRuntimeError(RuntimeError):
    def __init__(self, blockers: Sequence[PassiveRuntimeBlock]) -> None:
        self.blockers = tuple(blockers)
        codes = ", ".join(block.code for block in self.blockers)
        super().__init__(f"passive runtime is blocked: {codes}")


@dataclass(frozen=True, slots=True)
class PassiveRuntimeResult:
    modifiers: RunModifiers
    status_enchant_installs: tuple[StatusEnchantInstallContract, ...]
    applied_rules: tuple[AppliedPassiveRule, ...]
    blocking_rules: tuple[PassiveRuntimeBlock, ...]
    chance_contracts: tuple[PassiveChanceContract, ...] = ()

    @property
    def fully_supported(self) -> bool:
        return not self.blocking_rules

    @property
    def blocking_codes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(block.code for block in self.blocking_rules))

    def require_supported(self) -> None:
        if self.blocking_rules:
            raise UnsupportedPassiveRuntimeError(self.blocking_rules)


def passive_source_usage_key(source: PassiveSource, source_index: int) -> str:
    """Return the stable per-loadout occurrence key used for activation counts."""

    if not isinstance(source, PassiveSource):
        raise TypeError("source must be PassiveSource")
    if not _is_integer(source_index) or source_index < 0:
        raise ValueError("source_index must be a non-negative integer")
    parent = source.parent_id or "-"
    order = "-" if source.slot_order is None else str(source.slot_order)
    level = "-" if source.skill_level is None else str(source.skill_level)
    return (
        f"passive[{source_index}]|{source.source_kind}|{source.source_id}|"
        f"{parent}|{order}|{source.skill_id}|{level}"
    )


def _status_trigger_supported(rule: PassiveRule) -> bool:
    if rule.trigger_phase_type == PHASE_START_LESSON:
        return _LESSON_STATUS_TRIGGER.fullmatch(rule.trigger_id) is not None
    return rule.trigger_id in _STATUS_TRIGGER_IDS_BY_PHASE.get(
        rule.trigger_phase_type or "", frozenset()
    )


def _runtime_block(
    code: str,
    reason: str,
    *,
    source_index: int,
    source: PassiveSource,
    rule: PassiveRule | None,
) -> PassiveRuntimeBlock:
    return PassiveRuntimeBlock(
        code=code,
        reason=reason,
        source_index=source_index,
        source_kind=source.source_kind,
        source_id=source.source_id,
        skill_id=source.skill_id,
        rule_slot=rule.slot if rule is not None else None,
        effect_id=rule.effect_id if rule is not None else "",
        effect_type=rule.effect_type if rule is not None else None,
        effect_value_min=(rule.effect_value_min if rule is not None else None),
        effect_value_max=(rule.effect_value_max if rule is not None else None),
        trigger_id=rule.trigger_id if rule is not None else "",
        trigger_phase_type=(
            rule.trigger_phase_type if rule is not None else None
        ),
        activation_rate_permil=(
            rule.activation_rate_permil if rule is not None else None
        ),
    )


def _append_unique_block(
    blocks: list[PassiveRuntimeBlock], block: PassiveRuntimeBlock
) -> None:
    identity = (
        block.code,
        block.reason,
        block.source_index,
        block.rule_slot,
        block.effect_id,
        block.trigger_id,
    )
    if all(
        (
            existing.code,
            existing.reason,
            existing.source_index,
            existing.rule_slot,
            existing.effect_id,
            existing.trigger_id,
        )
        != identity
        for existing in blocks
    ):
        blocks.append(block)


def _catalog_reason_is_status_whitelist_only(
    reason: str, rule: PassiveRule
) -> bool:
    expected = (
        f"rule-not-whitelisted:{rule.trigger_id}:"
        f"{EFFECT_EXAM_STATUS_ENCHANT}"
    )
    return reason == expected


def resolve_passive_runtime(
    sources: Sequence[PassiveSource],
    *,
    usage_counts: Mapping[str, int] | None = None,
) -> PassiveRuntimeResult:
    """Resolve a selected passive loadout into safe offline runtime data.

    Supported values are aggregated even when another source is blocked so a
    diagnostic view can explain the known part of the loadout.  Consumers must
    call ``require_supported`` (or use ``apply_run_modifiers``) before commit.
    """

    if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence):
        raise TypeError("sources must be a sequence of PassiveSource")
    normalized_usage: dict[str, int] = {}
    for key, value in (usage_counts or {}).items():
        if not isinstance(key, str) or not key:
            raise ValueError("usage count keys must be non-empty strings")
        if not _is_integer(value) or value < 0:
            raise ValueError(f"usage count must be non-negative: {key}")
        normalized_usage[key] = value

    totals = {field.name: 0 for field in fields(RunModifiers)}
    installs: list[StatusEnchantInstallContract] = []
    applied: list[AppliedPassiveRule] = []
    blocks: list[PassiveRuntimeBlock] = []
    chance_runtime = resolve_passive_chance_runtime(
        sources, usage_counts=normalized_usage
    )
    chance_by_rule = {
        (contract.source_index, contract.rule_slot): contract
        for contract in chance_runtime.contracts
    }

    for source_index, source in enumerate(sources):
        if not isinstance(source, PassiveSource):
            raise TypeError("sources must contain PassiveSource")
        usage_key = passive_source_usage_key(source, source_index)
        use_count = normalized_usage.get(usage_key, 0)

        for reason in source.unsupported_rules:
            _append_unique_block(
                blocks,
                _runtime_block(
                    "unsupported-source",
                    reason,
                    source_index=source_index,
                    source=source,
                    rule=None,
                ),
            )

        activation_count = source.activation_count
        valid_activation_count = (
            _is_integer(activation_count) and activation_count >= 0
        )
        if not valid_activation_count:
            _append_unique_block(
                blocks,
                _runtime_block(
                    "invalid-activation-count",
                    f"activationCount={activation_count!r}",
                    source_index=source_index,
                    source=source,
                    rule=None,
                ),
            )

        for rule in source.rules:
            if not isinstance(rule, PassiveRule):
                raise TypeError("PassiveSource.rules must contain PassiveRule")
            chance_contract = chance_by_rule.get((source_index, rule.slot))

            safe_rate = (
                _is_integer(rule.activation_rate_permil)
                and rule.activation_rate_permil == 0
            )
            safe_range = (
                _is_integer(rule.effect_value_min)
                and _is_integer(rule.effect_value_max)
                and rule.effect_value_min == rule.effect_value_max
            )
            if not safe_rate and chance_contract is None:
                _append_unique_block(
                    blocks,
                    _runtime_block(
                        "nondeterministic-activation",
                        f"activationRatePermil={rule.activation_rate_permil!r}",
                        source_index=source_index,
                        source=source,
                        rule=rule,
                    ),
                )
            if not safe_range:
                _append_unique_block(
                    blocks,
                    _runtime_block(
                        "nondeterministic-effect-range",
                        (
                            f"effectRange={rule.effect_value_min!r}.."
                            f"{rule.effect_value_max!r}"
                        ),
                        source_index=source_index,
                        source=source,
                        rule=rule,
                    ),
                )

            runtime_field = _RUN_EFFECT_FIELDS.get(
                (rule.trigger_id, rule.effect_type or "")
            )
            is_status_contract = (
                rule.effect_type == EFFECT_EXAM_STATUS_ENCHANT
                and _status_trigger_supported(rule)
            )

            for reason in rule.unsupported_rules:
                if chance_contract is not None:
                    # passive_chance only creates a contract after proving all
                    # catalog reasons are the expected probability/whitelist
                    # diagnostics for this exact rule.
                    continue
                if is_status_contract and _catalog_reason_is_status_whitelist_only(
                    reason, rule
                ):
                    continue
                _append_unique_block(
                    blocks,
                    _runtime_block(
                        "catalog-rule-block",
                        reason,
                        source_index=source_index,
                        source=source,
                        rule=rule,
                    ),
                    )

            if chance_contract is not None:
                continue

            if runtime_field is not None:
                if rule.trigger_phase_type != PHASE_PRODUCE_START:
                    _append_unique_block(
                        blocks,
                        _runtime_block(
                            "unknown-trigger",
                            (
                                f"{rule.trigger_id}:"
                                f"{rule.trigger_phase_type or 'unknown'}"
                            ),
                            source_index=source_index,
                            source=source,
                            rule=rule,
                        ),
                    )
                    continue
                if not valid_activation_count or activation_count != 1:
                    _append_unique_block(
                        blocks,
                        _runtime_block(
                            "produce-start-activation-count",
                            f"activationCount={activation_count!r}; expected 1",
                            source_index=source_index,
                            source=source,
                            rule=rule,
                        ),
                    )
                    continue
                if safe_rate and safe_range and not rule.unsupported_rules:
                    value = int(rule.effect_value_min)
                    totals[runtime_field] += value
                    applied.append(
                        AppliedPassiveRule(
                            source_index=source_index,
                            source_kind=source.source_kind,
                            source_id=source.source_id,
                            skill_id=source.skill_id,
                            rule_slot=rule.slot,
                            effect_id=rule.effect_id,
                            trigger_id=rule.trigger_id,
                            outcome=f"run_modifier:{runtime_field}",
                            value=value,
                        )
                    )
                continue

            if rule.effect_type == EFFECT_EXAM_STATUS_ENCHANT:
                if not _status_trigger_supported(rule):
                    _append_unique_block(
                        blocks,
                        _runtime_block(
                            "unknown-status-trigger",
                            (
                                f"{rule.trigger_id}:"
                                f"{rule.trigger_phase_type or 'unknown'}"
                            ),
                            source_index=source_index,
                            source=source,
                            rule=rule,
                        ),
                    )
                    continue
                effect_raw = rule.raw.get("effect")
                status_enchant_id = (
                    effect_raw.get("produceExamStatusEnchantId")
                    if isinstance(effect_raw, Mapping)
                    else None
                )
                if not isinstance(status_enchant_id, str) or not status_enchant_id:
                    _append_unique_block(
                        blocks,
                        _runtime_block(
                            "missing-status-enchant-id",
                            "ProduceEffect.produceExamStatusEnchantId is empty",
                            source_index=source_index,
                            source=source,
                            rule=rule,
                        ),
                    )
                    continue
                if rule.effect_value_min != 0 or rule.effect_value_max != 0:
                    _append_unique_block(
                        blocks,
                        _runtime_block(
                            "invalid-status-enchant-range",
                            (
                                f"effectRange={rule.effect_value_min!r}.."
                                f"{rule.effect_value_max!r}; expected 0..0"
                            ),
                            source_index=source_index,
                            source=source,
                            rule=rule,
                        ),
                    )
                    continue
                allowed_catalog_reasons = all(
                    _catalog_reason_is_status_whitelist_only(reason, rule)
                    for reason in rule.unsupported_rules
                )
                if (
                    safe_rate
                    and safe_range
                    and valid_activation_count
                    and allowed_catalog_reasons
                ):
                    limit = int(activation_count)
                    installs.append(
                        StatusEnchantInstallContract(
                            source_index=source_index,
                            source_kind=source.source_kind,
                            source_id=source.source_id,
                            skill_id=source.skill_id,
                            rule_slot=rule.slot,
                            effect_id=rule.effect_id,
                            produce_trigger_id=rule.trigger_id,
                            produce_trigger_phase_type=(
                                rule.trigger_phase_type or ""
                            ),
                            status_enchant_id=status_enchant_id,
                            usage_key=usage_key,
                            activation_limit=limit,
                            use_count=use_count,
                        )
                    )
                    applied.append(
                        AppliedPassiveRule(
                            source_index=source_index,
                            source_kind=source.source_kind,
                            source_id=source.source_id,
                            skill_id=source.skill_id,
                            rule_slot=rule.slot,
                            effect_id=rule.effect_id,
                            trigger_id=rule.trigger_id,
                            outcome="status_enchant_install",
                            value=status_enchant_id,
                        )
                    )
                continue

            if rule.effect_type not in {
                effect_type for _, effect_type in _RUN_EFFECT_FIELDS
            }:
                code = "unknown-effect-type"
                reason = rule.effect_type or "missing effect type"
            else:
                code = "unknown-trigger"
                reason = (
                    f"{rule.trigger_id}:"
                    f"{rule.trigger_phase_type or 'unknown'}"
                )
            _append_unique_block(
                blocks,
                _runtime_block(
                    code,
                    reason,
                    source_index=source_index,
                    source=source,
                    rule=rule,
                ),
            )

    return PassiveRuntimeResult(
        modifiers=RunModifiers(**totals),
        status_enchant_installs=tuple(installs),
        applied_rules=tuple(applied),
        blocking_rules=tuple(blocks),
        chance_contracts=chance_runtime.contracts,
    )


def apply_run_modifiers(
    base: Mapping[str, Any], runtime: PassiveRuntimeResult
) -> dict[str, Any]:
    """Apply a fully supported runtime result to a run-state mapping.

    Existing unrelated keys are preserved.  The five immediate resources map
    to the run-shadow names (``vocal``, ``produce_points``, etc.); persistent
    configuration modifiers retain the names from ``RunModifiers``.
    """

    if not isinstance(base, Mapping):
        raise TypeError("base must be a mapping")
    if not isinstance(runtime, PassiveRuntimeResult):
        raise TypeError("runtime must be PassiveRuntimeResult")
    runtime.require_supported()

    result = dict(base)
    for modifier_name, delta in runtime.modifiers.as_dict().items():
        if delta == 0:
            continue
        target = _RUN_STATE_FIELD_BY_MODIFIER.get(
            modifier_name, modifier_name
        )
        current = result.get(target, 0)
        if not _is_integer(current):
            raise ValueError(f"base field must be an integer: {target}")
        result[target] = int(current) + delta
    return result
