"""Normalize memory and support-card passives from the static Master tables.

This module deliberately stops at a data contract.  It does not mutate a run
state and it never treats an unknown or probabilistic rule as a zero-valued
effect.  Callers can therefore use ``fully_supported`` as a hard safety gate
before applying a passive to their shadow state.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
from .application_paths import master_directory
DEFAULT_MASTER_DIR = master_directory()

SOURCE_MEMORY = "memory_ability"
SOURCE_SUPPORT = "support_card"

SUPPORT_LEVEL_TABLES = {
    "SupportCardType_Vocal": "SupportCardProduceSkillLevelVocal.yaml",
    "SupportCardType_Dance": "SupportCardProduceSkillLevelDance.yaml",
    "SupportCardType_Visual": "SupportCardProduceSkillLevelVisual.yaml",
    "SupportCardType_Assist": "SupportCardProduceSkillLevelAssist.yaml",
}

EFFECT_VOCAL_ADDITION = "ProduceEffectType_VocalAddition"
EFFECT_DANCE_ADDITION = "ProduceEffectType_DanceAddition"
EFFECT_VISUAL_ADDITION = "ProduceEffectType_VisualAddition"
EFFECT_PRODUCE_POINT_ADDITION = (
    "ProduceEffectType_ProducePointAdditionDisableTrigger"
)
EFFECT_MAX_STAMINA_ADDITION = "ProduceEffectType_MaxStaminaAddition"
EFFECT_VOCAL_GROWTH_RATE_ADDITION = "ProduceEffectType_VocalGrowthRateAddition"
EFFECT_DANCE_GROWTH_RATE_ADDITION = "ProduceEffectType_DanceGrowthRateAddition"
EFFECT_VISUAL_GROWTH_RATE_ADDITION = "ProduceEffectType_VisualGrowthRateAddition"

TRIGGER_PRODUCE_START_INITIAL = "p_trigger-produce_start-initial"
TRIGGER_PRODUCE_START_CONFIGURATION = "p_trigger-produce_start-no_description"

# Fixed, intentionally conservative MVP whitelist.  These rules all fire at
# produce start, have scalar values, and can be committed before the first
# schedule choice.  Other known Master rules are still returned verbatim as
# unsupported instead of disappearing from the result.
_INITIAL_EFFECT_TYPES = frozenset(
    {
        EFFECT_VOCAL_ADDITION,
        EFFECT_DANCE_ADDITION,
        EFFECT_VISUAL_ADDITION,
        EFFECT_PRODUCE_POINT_ADDITION,
    }
)
_CONFIGURATION_EFFECT_TYPES = frozenset(
    {
        EFFECT_MAX_STAMINA_ADDITION,
        EFFECT_VOCAL_GROWTH_RATE_ADDITION,
        EFFECT_DANCE_GROWTH_RATE_ADDITION,
        EFFECT_VISUAL_GROWTH_RATE_ADDITION,
        "ProduceEffectType_LessonSpChangeRatePermilAddition",
        "ProduceEffectType_LessonVocalSpChangeRatePermilAddition",
        "ProduceEffectType_LessonDanceSpChangeRatePermilAddition",
        "ProduceEffectType_LessonVisualSpChangeRatePermilAddition",
        "ProduceEffectType_SupportCardEventParameterAdditionValueUp",
        "ProduceEffectType_SupportCardEventProducePointAdditionValueUp",
        "ProduceEffectType_SupportCardEventStaminaRecoverUp",
        "ProduceEffectType_SupportCardProduceCardUpgradeProbabilityUp",
    }
)
DETERMINISTIC_RULE_WHITELIST = frozenset(
    {(TRIGGER_PRODUCE_START_INITIAL, effect) for effect in _INITIAL_EFFECT_TYPES}
    | {
        (TRIGGER_PRODUCE_START_CONFIGURATION, effect)
        for effect in _CONFIGURATION_EFFECT_TYPES
    }
)

_SKILL_SLOT_KEY = re.compile(
    r"^(?:produceEffectId|produceTriggerId|activationRatePermil)(\d+)$"
)


@dataclass(frozen=True, slots=True)
class MemoryAbilitySelection:
    id: str
    level: int = 1


@dataclass(frozen=True, slots=True)
class SupportCardSelection:
    id: str
    level: int


@dataclass(frozen=True, slots=True)
class PassiveRule:
    """One aligned ProduceSkill effect/trigger/rate slot."""

    slot: int
    effect_id: str
    effect_type: str | None
    effect_value_min: int | None
    effect_value_max: int | None
    produce_resource_type: str | None
    trigger_id: str
    trigger_phase_type: str | None
    activation_rate_permil: int | None
    unsupported_rules: tuple[str, ...]
    raw: dict[str, Any]

    @property
    def fully_supported(self) -> bool:
        return not self.unsupported_rules

    @property
    def deterministic(self) -> bool:
        return (
            self.activation_rate_permil == 0
            and self.effect_value_min is not None
            and self.effect_value_min == self.effect_value_max
        )


@dataclass(frozen=True, slots=True)
class PassiveSource:
    """A normalized selected memory ability or unlocked support-card skill."""

    source_kind: str
    source_id: str
    source_level: int
    parent_id: str | None
    slot_order: int | None
    skill_id: str
    skill_level: int | None
    activation_count: int | None
    rules: tuple[PassiveRule, ...]
    unsupported_rules: tuple[str, ...]
    raw: dict[str, Any]

    @property
    def fully_supported(self) -> bool:
        return not self.unsupported_rules and all(
            rule.fully_supported for rule in self.rules
        )

    @property
    def all_unsupported_rules(self) -> tuple[str, ...]:
        return _deduplicate(
            (
                *self.unsupported_rules,
                *(reason for rule in self.rules for reason in rule.unsupported_rules),
            )
        )


def _deduplicate(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _copy_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(row))


def _strict_int(value: Any, label: str, *, minimum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return value


def _optional_int(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value


def _strict_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _load_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"Master table does not exist: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Master table must be a list: {path}")
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(payload):
        if not isinstance(row, Mapping):
            raise ValueError(f"Master row {index} must be a mapping: {path}")
        rows.append(_copy_row(row))
    return rows


def _index_by_id(
    rows: Iterable[Mapping[str, Any]], table_name: str
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        row_id = _strict_text(row.get("id"), f"{table_name}.id")
        if row_id in indexed:
            raise ValueError(f"duplicate {table_name} id: {row_id}")
        indexed[row_id] = _copy_row(row)
    return indexed


def _index_by_id_level(
    rows: Iterable[Mapping[str, Any]], table_name: str
) -> dict[tuple[str, int], dict[str, Any]]:
    indexed: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        row_id = _strict_text(row.get("id"), f"{table_name}.id")
        level = _strict_int(row.get("level"), f"{table_name}.level", minimum=0)
        key = (row_id, level)
        if key in indexed:
            raise ValueError(f"duplicate {table_name} key: {key}")
        indexed[key] = _copy_row(row)
    return indexed


class MasterPassiveCatalog:
    """In-memory indexes for the memory/support ProduceSkill chains."""

    def __init__(
        self,
        *,
        memory_gifts: Iterable[Mapping[str, Any]],
        memory_abilities: Iterable[Mapping[str, Any]],
        support_cards: Iterable[Mapping[str, Any]],
        support_skill_levels: Mapping[
            str, Iterable[Mapping[str, Any]]
        ],
        produce_skills: Iterable[Mapping[str, Any]],
        produce_effects: Iterable[Mapping[str, Any]],
        produce_triggers: Iterable[Mapping[str, Any]],
    ) -> None:
        self._memory_gifts = _index_by_id(memory_gifts, "MemoryGift")
        self._memory_abilities = _index_by_id_level(
            memory_abilities, "MemoryAbility"
        )
        self._support_cards = _index_by_id(support_cards, "SupportCard")
        self._produce_skills = _index_by_id_level(produce_skills, "ProduceSkill")
        self._produce_effects = _index_by_id(produce_effects, "ProduceEffect")
        self._produce_triggers = _index_by_id(produce_triggers, "ProduceTrigger")
        self._support_skill_levels: dict[
            str, dict[str, tuple[dict[str, Any], ...]]
        ] = {}
        for support_type, rows in support_skill_levels.items():
            by_card: dict[str, list[dict[str, Any]]] = {}
            seen: set[tuple[str, int, int]] = set()
            for row in rows:
                card_id = _strict_text(
                    row.get("supportCardId"),
                    f"{support_type}.supportCardId",
                )
                order = _strict_int(
                    row.get("order"), f"{support_type}.order", minimum=0
                )
                gate = _strict_int(
                    row.get("supportCardLevel"),
                    f"{support_type}.supportCardLevel",
                    minimum=1,
                )
                key = (card_id, order, gate)
                if key in seen:
                    raise ValueError(f"duplicate support skill level key: {key}")
                seen.add(key)
                by_card.setdefault(card_id, []).append(_copy_row(row))
            self._support_skill_levels[support_type] = {
                card_id: tuple(
                    sorted(
                        card_rows,
                        key=lambda value: (
                            int(value["order"]),
                            int(value["supportCardLevel"]),
                            str(value.get("produceSkillId", "")),
                        ),
                    )
                )
                for card_id, card_rows in by_card.items()
            }

    @classmethod
    def load(
        cls, master_dir: Path | None = None
    ) -> MasterPassiveCatalog:
        """Load the seven authoritative Master chains from ``master_dir``."""

        if master_dir is None:
            from .application_paths import asset_directory
            package = asset_directory('loadout')
            if package is not None:
                from .portable_loadout_assets import load_portable_passive_catalog
                return load_portable_passive_catalog(package)
            master_dir = DEFAULT_MASTER_DIR
        master_dir = Path(master_dir)
        support_levels = {
            support_type: _load_rows(master_dir / filename)
            for support_type, filename in SUPPORT_LEVEL_TABLES.items()
        }
        return cls(
            memory_gifts=_load_rows(master_dir / "MemoryGift.yaml"),
            memory_abilities=_load_rows(master_dir / "MemoryAbility.yaml"),
            support_cards=_load_rows(master_dir / "SupportCard.yaml"),
            support_skill_levels=support_levels,
            produce_skills=_load_rows(master_dir / "ProduceSkill.yaml"),
            produce_effects=_load_rows(master_dir / "ProduceEffect.yaml"),
            produce_triggers=_load_rows(master_dir / "ProduceTrigger.yaml"),
        )

    def memory_gift_abilities(
        self, memory_gift_id: str
    ) -> tuple[MemoryAbilitySelection, ...]:
        gift = self._memory_gifts.get(memory_gift_id)
        if gift is None:
            raise KeyError(f"unknown MemoryGift: {memory_gift_id}")
        payload = gift.get("memoryAbilities")
        if not isinstance(payload, Sequence) or isinstance(payload, (str, bytes)):
            raise ValueError(
                f"MemoryGift.memoryAbilities must be a list: {memory_gift_id}"
            )
        selections: list[MemoryAbilitySelection] = []
        for index, value in enumerate(payload):
            if not isinstance(value, Mapping):
                raise ValueError(
                    f"MemoryGift.memoryAbilities[{index}] must be a mapping: "
                    f"{memory_gift_id}"
                )
            selections.append(
                MemoryAbilitySelection(
                    _strict_text(
                        value.get("id"),
                        f"MemoryGift.memoryAbilities[{index}].id",
                    ),
                    _strict_int(
                        value.get("level"),
                        f"MemoryGift.memoryAbilities[{index}].level",
                        minimum=0,
                    ),
                )
            )
        return tuple(selections)

    def resolve_memory_ability(
        self, ability_id: str, level: int = 1
    ) -> PassiveSource:
        return self._resolve_memory_ability(
            ability_id,
            _strict_int(level, "memory ability level", minimum=0),
            parent_id=None,
        )

    def resolve_memory_gift(
        self, memory_gift_id: str
    ) -> tuple[PassiveSource, ...]:
        gift = self._memory_gifts.get(memory_gift_id)
        if gift is None:
            raise KeyError(f"unknown MemoryGift: {memory_gift_id}")
        return tuple(
            self._resolve_memory_ability(
                selection.id,
                selection.level,
                parent_id=memory_gift_id,
                parent_raw=gift,
            )
            for selection in self.memory_gift_abilities(memory_gift_id)
        )

    def _resolve_memory_ability(
        self,
        ability_id: str,
        level: int,
        *,
        parent_id: str | None,
        parent_raw: Mapping[str, Any] | None = None,
    ) -> PassiveSource:
        ability = self._memory_abilities.get((ability_id, level))
        if ability is None:
            raise KeyError(f"unknown MemoryAbility: {(ability_id, level)}")
        skill_id = ability.get("skillId")
        if not isinstance(skill_id, str) or not skill_id:
            return self._missing_skill_source(
                source_kind=SOURCE_MEMORY,
                source_id=ability_id,
                source_level=level,
                parent_id=parent_id,
                slot_order=None,
                skill_id="",
                skill_level=level,
                reason=f"missing-skill-id:{ability_id}",
                source_raw=ability,
                parent_raw=parent_raw,
            )
        return self._source_from_skill(
            source_kind=SOURCE_MEMORY,
            source_id=ability_id,
            source_level=level,
            parent_id=parent_id,
            slot_order=None,
            skill_id=skill_id,
            skill_level=level,
            source_raw=ability,
            level_gate_raw=None,
            parent_raw=parent_raw,
        )

    def resolve_support_card(
        self, support_card_id: str, level: int
    ) -> tuple[PassiveSource, ...]:
        level = _strict_int(level, "support card level", minimum=1)
        card = self._support_cards.get(support_card_id)
        if card is None:
            raise KeyError(f"unknown SupportCard: {support_card_id}")
        support_type = card.get("type")
        if not isinstance(support_type, str) or support_type not in SUPPORT_LEVEL_TABLES:
            return (
                self._missing_skill_source(
                    source_kind=SOURCE_SUPPORT,
                    source_id=support_card_id,
                    source_level=level,
                    parent_id=None,
                    slot_order=None,
                    skill_id="",
                    skill_level=None,
                    reason=f"unsupported-support-card-type:{support_type}",
                    source_raw=card,
                ),
            )
        rows = self._support_skill_levels.get(support_type, {}).get(
            support_card_id, ()
        )
        if not rows:
            return (
                self._missing_skill_source(
                    source_kind=SOURCE_SUPPORT,
                    source_id=support_card_id,
                    source_level=level,
                    parent_id=None,
                    slot_order=None,
                    skill_id="",
                    skill_level=None,
                    reason=f"missing-support-level-rules:{support_card_id}",
                    source_raw=card,
                ),
            )

        active_by_order: dict[int, dict[str, Any]] = {}
        for row in rows:
            gate = int(row["supportCardLevel"])
            if gate > level:
                continue
            order = int(row["order"])
            previous = active_by_order.get(order)
            if previous is None or gate > int(previous["supportCardLevel"]):
                active_by_order[order] = row
        if not active_by_order:
            return (
                self._missing_skill_source(
                    source_kind=SOURCE_SUPPORT,
                    source_id=support_card_id,
                    source_level=level,
                    parent_id=None,
                    slot_order=None,
                    skill_id="",
                    skill_level=None,
                    reason=f"no-unlocked-support-skills:{support_card_id}:level-{level}",
                    source_raw=card,
                ),
            )

        sources: list[PassiveSource] = []
        for order, gate_row in sorted(active_by_order.items()):
            skill_id = gate_row.get("produceSkillId")
            skill_level = _optional_int(gate_row.get("produceSkillLevel"))
            if not isinstance(skill_id, str) or not skill_id or skill_level is None:
                sources.append(
                    self._missing_skill_source(
                        source_kind=SOURCE_SUPPORT,
                        source_id=support_card_id,
                        source_level=level,
                        parent_id=None,
                        slot_order=order,
                        skill_id=skill_id if isinstance(skill_id, str) else "",
                        skill_level=skill_level,
                        reason=f"invalid-support-skill-gate:{support_card_id}:order-{order}",
                        source_raw=card,
                        level_gate_raw=gate_row,
                    )
                )
                continue
            sources.append(
                self._source_from_skill(
                    source_kind=SOURCE_SUPPORT,
                    source_id=support_card_id,
                    source_level=level,
                    parent_id=None,
                    slot_order=order,
                    skill_id=skill_id,
                    skill_level=skill_level,
                    source_raw=card,
                    level_gate_raw=gate_row,
                )
            )
        return tuple(sources)

    def resolve_loadout(
        self,
        *,
        memory_abilities: Iterable[MemoryAbilitySelection] = (),
        support_cards: Iterable[SupportCardSelection] = (),
    ) -> tuple[PassiveSource, ...]:
        """Resolve a selected loadout without deduplicating stackable sources."""

        resolved: list[PassiveSource] = []
        for selection in memory_abilities:
            if not isinstance(selection, MemoryAbilitySelection):
                raise TypeError("memory_abilities must contain MemoryAbilitySelection")
            resolved.append(
                self.resolve_memory_ability(selection.id, selection.level)
            )
        for selection in support_cards:
            if not isinstance(selection, SupportCardSelection):
                raise TypeError("support_cards must contain SupportCardSelection")
            resolved.extend(self.resolve_support_card(selection.id, selection.level))
        return tuple(resolved)

    def _source_from_skill(
        self,
        *,
        source_kind: str,
        source_id: str,
        source_level: int,
        parent_id: str | None,
        slot_order: int | None,
        skill_id: str,
        skill_level: int,
        source_raw: Mapping[str, Any],
        level_gate_raw: Mapping[str, Any] | None,
        parent_raw: Mapping[str, Any] | None = None,
    ) -> PassiveSource:
        skill = self._produce_skills.get((skill_id, skill_level))
        if skill is None:
            return self._missing_skill_source(
                source_kind=source_kind,
                source_id=source_id,
                source_level=source_level,
                parent_id=parent_id,
                slot_order=slot_order,
                skill_id=skill_id,
                skill_level=skill_level,
                reason=f"missing-produce-skill:{skill_id}:level-{skill_level}",
                source_raw=source_raw,
                level_gate_raw=level_gate_raw,
                parent_raw=parent_raw,
            )

        source_unsupported: list[str] = []
        activation_count = _optional_int(skill.get("activationCount"))
        if activation_count is None or activation_count < 0:
            source_unsupported.append(f"invalid-activation-count:{skill_id}")

        slot_numbers = {
            int(match.group(1))
            for key in skill
            if (match := _SKILL_SLOT_KEY.match(str(key))) is not None
        }
        rules: list[PassiveRule] = []
        for slot in sorted(slot_numbers):
            effect_id_value = skill.get(f"produceEffectId{slot}")
            trigger_id_value = skill.get(f"produceTriggerId{slot}")
            rate_value = skill.get(f"activationRatePermil{slot}")
            effect_id = effect_id_value if isinstance(effect_id_value, str) else ""
            trigger_id = trigger_id_value if isinstance(trigger_id_value, str) else ""
            rate = _optional_int(rate_value)
            if not effect_id and not trigger_id and rate in (None, 0):
                continue
            rules.append(
                self._normalize_rule(
                    skill_id=skill_id,
                    slot=slot,
                    effect_id=effect_id,
                    trigger_id=trigger_id,
                    activation_rate_permil=rate,
                    raw_rate=rate_value,
                )
            )
        if not rules:
            source_unsupported.append(f"produce-skill-without-rules:{skill_id}")

        raw = {
            "source": _copy_row(source_raw),
            "levelGate": (
                _copy_row(level_gate_raw) if level_gate_raw is not None else None
            ),
            "skill": _copy_row(skill),
            "parent": _copy_row(parent_raw) if parent_raw is not None else None,
        }
        return PassiveSource(
            source_kind=source_kind,
            source_id=source_id,
            source_level=source_level,
            parent_id=parent_id,
            slot_order=slot_order,
            skill_id=skill_id,
            skill_level=skill_level,
            activation_count=activation_count,
            rules=tuple(rules),
            unsupported_rules=_deduplicate(source_unsupported),
            raw=raw,
        )

    def _normalize_rule(
        self,
        *,
        skill_id: str,
        slot: int,
        effect_id: str,
        trigger_id: str,
        activation_rate_permil: int | None,
        raw_rate: Any,
    ) -> PassiveRule:
        unsupported: list[str] = []
        effect = self._produce_effects.get(effect_id) if effect_id else None
        trigger = self._produce_triggers.get(trigger_id) if trigger_id else None

        if not effect_id:
            unsupported.append(f"missing-effect-id:{skill_id}:slot-{slot}")
        elif effect is None:
            unsupported.append(f"missing-produce-effect:{effect_id}")
        if not trigger_id:
            unsupported.append(f"missing-trigger-id:{skill_id}:slot-{slot}")
        elif trigger is None:
            unsupported.append(f"missing-produce-trigger:{trigger_id}")

        if activation_rate_permil is None:
            unsupported.append(f"invalid-activation-rate:{skill_id}:slot-{slot}")
        elif activation_rate_permil != 0:
            unsupported.append(
                f"probabilistic-activation-rate:{activation_rate_permil}"
            )

        effect_type_value = effect.get("produceEffectType") if effect else None
        effect_type = (
            effect_type_value if isinstance(effect_type_value, str) else None
        )
        phase_value = trigger.get("phaseType") if trigger else None
        trigger_phase_type = phase_value if isinstance(phase_value, str) else None
        value_min = _optional_int(effect.get("effectValueMin")) if effect else None
        value_max = _optional_int(effect.get("effectValueMax")) if effect else None
        resource_value = effect.get("produceResourceType") if effect else None
        resource_type = resource_value if isinstance(resource_value, str) else None

        if effect is not None:
            if effect_type is None:
                unsupported.append(f"invalid-effect-type:{effect_id}")
            if value_min is None or value_max is None:
                unsupported.append(f"invalid-effect-range:{effect_id}")
            elif value_min != value_max:
                unsupported.append(
                    f"random-effect-range:{effect_id}:{value_min}:{value_max}"
                )
        if trigger is not None and trigger_phase_type is None:
            unsupported.append(f"invalid-trigger-phase:{trigger_id}")
        if (
            trigger_id,
            effect_type,
        ) not in DETERMINISTIC_RULE_WHITELIST:
            unsupported.append(
                f"rule-not-whitelisted:{trigger_id}:{effect_type or 'unknown'}"
            )

        return PassiveRule(
            slot=slot,
            effect_id=effect_id,
            effect_type=effect_type,
            effect_value_min=value_min,
            effect_value_max=value_max,
            produce_resource_type=resource_type,
            trigger_id=trigger_id,
            trigger_phase_type=trigger_phase_type,
            activation_rate_permil=activation_rate_permil,
            unsupported_rules=_deduplicate(unsupported),
            raw={
                "effect": _copy_row(effect) if effect is not None else None,
                "trigger": _copy_row(trigger) if trigger is not None else None,
                "activationRatePermil": copy.deepcopy(raw_rate),
            },
        )

    @staticmethod
    def _missing_skill_source(
        *,
        source_kind: str,
        source_id: str,
        source_level: int,
        parent_id: str | None,
        slot_order: int | None,
        skill_id: str,
        skill_level: int | None,
        reason: str,
        source_raw: Mapping[str, Any],
        level_gate_raw: Mapping[str, Any] | None = None,
        parent_raw: Mapping[str, Any] | None = None,
    ) -> PassiveSource:
        return PassiveSource(
            source_kind=source_kind,
            source_id=source_id,
            source_level=source_level,
            parent_id=parent_id,
            slot_order=slot_order,
            skill_id=skill_id,
            skill_level=skill_level,
            activation_count=None,
            rules=(),
            unsupported_rules=(reason,),
            raw={
                "source": _copy_row(source_raw),
                "levelGate": (
                    _copy_row(level_gate_raw)
                    if level_gate_raw is not None
                    else None
                ),
                "skill": None,
                "parent": (
                    _copy_row(parent_raw) if parent_raw is not None else None
                ),
            },
        )
