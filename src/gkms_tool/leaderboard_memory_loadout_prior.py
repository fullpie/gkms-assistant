"""Offline, explainable priors for four-card N.I.A. memory loadouts.

The normalized leaderboard replay already contains two useful pieces of
evidence for a memory-loadout advisor:

* ``memory_loadout`` identifies the four inherited Produce cards, their
  upgrade/customize state, and when the card is activated; and
* ``memory_abilities`` identifies the observed ability rows for each memory
  slot.

This module joins those fields for complete, successful trajectories and
learns a small, score-weighted prior.  It is deliberately an advisory data
layer.  It never samples a server candidate pool, resolves a missing memory,
changes a deck, or sends a game/queue action.  A caller must provide all four
candidate memories and any optional existing deck cards.

The broad evidence key is exactly ``(produce_id, plan_type,
exam_effect_type)``.  When an idol-card-specific group has enough complete
trajectories it is preferred, otherwise the same-flow broad group is used.
Likewise, a full memory signature (inherited card, phase, upgrade,
customizes, and observed abilities) is used only after it reaches the minimum
support threshold; sparse signatures fall back to the stable inherited card
ID.  This keeps the useful exact evidence without turning one observed run
into a recommendation.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
import ast
import hashlib
import itertools
import json
import math
import os
import re
from pathlib import Path
from statistics import fmean
from typing import Any

from .leaderboard_card_prior import DEFAULT_LEADERBOARD_EPISODES
from .leaderboard_replay import (
    LeaderboardRawHistoryCollection,
    LeaderboardReplayEpisode,
    list_leaderboard_raw_sources,
)
from .master_db import DEFAULT_DATABASE, get_idol_profile
from .leaderboard_replay_adapter import read_leaderboard_episode


SCHEMA = "gkms.leaderboard-memory-loadout-prior.v1"
HIERARCHICAL_SCHEMA = "gkms.leaderboard-memory-loadout-prior-hierarchical.v1"
ARTIFACT_SCHEMA = "gkms.leaderboard-memory-loadout-prior-artifact.v1"
OBSERVATION_SCHEMA = "gkms.leaderboard-memory-loadout-observation.v1"
REPORT_SCHEMA = "gkms.leaderboard-memory-loadout-observation-report.v1"
MEMORY_COUNT = 4
MIN_EXACT_SUPPORT = 3
MIN_USEFUL_SUPPORT = 2
MAX_SCORE = 1000
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRIOR_ARTIFACT = (
    PROJECT_ROOT / "var" / "leaderboard_research" / "memory_loadout_prior.v1.json"
)

FlowKey = tuple[str, str, str]
MemorySignature = tuple[
    str,
    int,
    str,
    tuple[tuple[str, int], ...],
    tuple[tuple[str, int], ...],
]
CombinationKey = tuple[str, ...]
ExactCombinationKey = tuple[MemorySignature, ...]

_FINAL_STAGE = "ProduceStepType_AuditionFinal"
_ROLE_TOKEN_RE = re.compile(
    r"(?:^|[-_])(act|men|sup|ido|active|mental|support|idol)(?:[-_]|$)",
    re.IGNORECASE,
)
_ROLE_ALIASES = {
    "active": "act",
    "act": "act",
    "mental": "men",
    "men": "men",
    "support": "sup",
    "sup": "sup",
    "idol": "ido",
    "ido": "ido",
}
_PLAN_TYPE_BY_INT = {
    1: "ProducePlanType_Plan1",
    2: "ProducePlanType_Plan2",
    3: "ProducePlanType_Plan3",
}


def _text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError(f"{label} must be non-empty text")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _number(value: object, label: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number >= {minimum}")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{label} must be a finite number >= {minimum}")
    return result


def _flow(
    produce_id: object,
    plan_type: object,
    exam_effect_type: object,
    *,
    label: str = "flow",
) -> FlowKey:
    return (
        _text(produce_id, f"{label}.produce_id"),
        _text(plan_type, f"{label}.plan_type"),
        _text(exam_effect_type, f"{label}.exam_effect_type"),
    )


def flow_string(flow: FlowKey) -> str:
    """Return the stable pipe-delimited representation used in reports."""

    if len(flow) != 3 or any(not isinstance(value, str) or not value for value in flow):
        raise ValueError("flow must contain produce, plan, and exam-effect text")
    return "|".join(flow)


def _canonical_role(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().casefold()
    if normalized in {"unknown", "unresolved", "none", "null", "n/a", "na"}:
        return None
    return _ROLE_ALIASES.get(normalized, normalized)


def _role_from_card_id(card_id: str) -> str | None:
    match = _ROLE_TOKEN_RE.search(card_id)
    return _canonical_role(match.group(1)) if match is not None else None


def _role_from_mapping(value: Mapping[str, Any], card_id: str) -> str | None:
    """Read an explicitly observed role, then the stable ID category token.

    The ID fallback is intentionally only a category token (``act``, ``men``,
    ``sup``, or ``ido``).  No localized card description or game behavior is
    inferred here.  An unrecognized ID remains role-unknown and is not
    penalized by the balance term.
    """

    for key in (
        "role",
        "card_role",
        "deck_role",
        "category",
        "card_category",
        "type",
        "card_type",
    ):
        role = _canonical_role(value.get(key))
        if role is not None:
            return role
    nested = value.get("produce_card", value.get("card"))
    if isinstance(nested, Mapping):
        for key in (
            "role",
            "card_role",
            "deck_role",
            "category",
            "card_category",
            "type",
            "card_type",
        ):
            role = _canonical_role(nested.get(key))
            if role is not None:
                return role
    return _role_from_card_id(card_id)


def _copy_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only the outer mapping while retaining observed nested values.

    Replay rows are treated as immutable inputs.  ``to_dict`` performs the
    final JSON-compatible copy; keeping the outer copy here avoids accidental
    mutation by callers while not inventing a schema for unknown observed
    fields.
    """

    return dict(value)


def _json_value(value: object) -> object:
    """Make retained observed fields safe for JSON report serialization."""

    if isinstance(value, MemoryAbilityObservation):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class MemoryAbilityObservation:
    """One observed memory ability, retained for exact-signature evidence."""

    ability_id: str
    level: int = 0
    observed_fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.ability_id, "memory ability id")
        _integer(self.level, "memory ability level")
        if not isinstance(self.observed_fields, Mapping):
            raise TypeError("memory ability observed_fields must be a mapping")

    @classmethod
    def from_value(cls, value: object) -> "MemoryAbilityObservation":
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            return cls(value)
        # ``loadout_snapshot.ObservedMemoryAbility`` is intentionally not
        # imported here (the prior remains usable without the live-loadout
        # assembly module), but its stable public fields are accepted.
        object_ability_id = getattr(value, "ability_id", None)
        object_level = getattr(value, "level", None)
        if isinstance(object_ability_id, str) and object_ability_id:
            return cls(
                object_ability_id,
                0 if object_level is None else _integer(object_level, "memory ability level"),
                {},
            )
        if not isinstance(value, Mapping):
            raise TypeError("memory abilities must contain strings or objects")
        ability_id = value.get("ability_id", value.get("id"))
        level = value.get("level", 0)
        return cls(
            _text(ability_id, "memory ability id"),
            _integer(level, "memory ability level"),
            _copy_mapping(value),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ability_id": self.ability_id,
            "level": self.level,
            "observed_fields": _json_value(self.observed_fields),
        }

    @property
    def key(self) -> tuple[str, int]:
        return self.ability_id, self.level


def _coerce_abilities(values: Iterable[object]) -> tuple[MemoryAbilityObservation, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("memory abilities must be an iterable, not text")
    return tuple(MemoryAbilityObservation.from_value(value) for value in values)


def _customize_key(customizes: Iterable[Mapping[str, Any]]) -> tuple[tuple[str, int], ...]:
    result: list[tuple[str, int]] = []
    for index, customize in enumerate(customizes):
        if not isinstance(customize, Mapping):
            raise ValueError(f"memory card customize {index} must be an object")
        customize_id = customize.get("id", customize.get("customize_id"))
        count = customize.get("customizeCount", customize.get("customize_count", 0))
        result.append(
            (
                _text(customize_id, f"memory card customize {index}.id"),
                _integer(count, f"memory card customize {index}.count"),
            )
        )
    return tuple(result)


@dataclass(frozen=True, slots=True)
class MemoryCardCandidate:
    """A caller-provided memory with observed inherited-card fields.

    ``card_id`` is required and identifies the inherited Produce card.
    ``memory_id`` is the stronger optional identity for the selected memory
    instance; it is preserved whenever a caller observed it.  Abilities and
    all other fields are observational metadata; they are never synthesized
    from a card catalog.  ``from_mapping`` accepts either the normalized
    replay shape (``produce_card`` and ``produce_card_phase_type``) or a
    convenient direct candidate shape.
    """

    card_id: str
    upgrade_count: int = 0
    phase_type: str | None = None
    abilities: tuple[MemoryAbilityObservation, ...] = ()
    memory_slot: int | None = None
    is_rental: bool = False
    customizes: tuple[Mapping[str, Any], ...] = ()
    role: str | None = None
    observed_fields: Mapping[str, Any] = field(default_factory=dict)
    # ``card_id`` identifies the inherited Produce card.  ``memory_id`` is
    # the selected memory instance and must remain distinct when multiple
    # instances inherit the same card.  It is optional for old leaderboard
    # rows, which predate the instance-id observation.
    memory_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.card_id, "memory candidate card_id")
        if self.memory_id is not None:
            _text(self.memory_id, "memory candidate memory_id")
        _integer(self.upgrade_count, "memory candidate upgrade_count")
        if self.phase_type is not None:
            _text(self.phase_type, "memory candidate phase_type")
        if self.memory_slot is not None:
            _integer(self.memory_slot, "memory candidate memory_slot")
        if not isinstance(self.is_rental, bool):
            raise TypeError("memory candidate is_rental must be boolean")
        abilities = _coerce_abilities(self.abilities)
        object.__setattr__(self, "abilities", abilities)
        if any(not isinstance(value, Mapping) for value in self.customizes):
            raise TypeError("memory candidate customizes must contain mappings")
        customizes = tuple(_copy_mapping(value) for value in self.customizes)
        # Validate IDs/counts now, but preserve all observed customize fields.
        _customize_key(customizes)
        object.__setattr__(self, "customizes", customizes)
        if not isinstance(self.observed_fields, Mapping):
            raise TypeError("memory candidate observed_fields must be a mapping")
        if self.role is None:
            role = _role_from_mapping(self.observed_fields, self.card_id)
        else:
            role = _canonical_role(self.role)
        object.__setattr__(self, "role", role)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MemoryCardCandidate":
        if not isinstance(value, Mapping):
            raise TypeError("memory candidate must be an object")
        nested = value.get("produce_card", value.get("card"))
        card = nested if isinstance(nested, Mapping) else value
        card_id = card.get("id", card.get("card_id"))
        upgrade = card.get("upgradeCount", card.get("upgrade_count", value.get("upgrade", 0)))
        customizes = card.get("customizes", value.get("customizes", ()))
        if not isinstance(customizes, Sequence) or isinstance(customizes, (str, bytes)):
            raise ValueError("memory candidate customizes must be an array")
        raw_abilities = value.get("abilities", ())
        if not isinstance(raw_abilities, Sequence) or isinstance(raw_abilities, (str, bytes)):
            raise ValueError("memory candidate abilities must be an array")
        phase = value.get(
            "produce_card_phase_type",
            value.get("phase_type", value.get("phase")),
        )
        slot = value.get("memory_slot", value.get("slot"))
        if slot is not None:
            slot = _integer(slot, "memory candidate memory_slot")
        rental = value.get("is_rental", value.get("isRental", False))
        if not isinstance(rental, bool):
            raise ValueError("memory candidate is_rental must be boolean")
        return cls(
            card_id=_text(card_id, "memory candidate card_id"),
            upgrade_count=_integer(upgrade, "memory candidate upgrade_count"),
            phase_type=(None if phase is None else _text(phase, "memory candidate phase_type")),
            abilities=_coerce_abilities(raw_abilities),
            memory_slot=slot,
            is_rental=rental,
            customizes=tuple(customizes),
            role=_canonical_role(value.get("role", value.get("card_role"))),
            observed_fields=_copy_mapping(value),
            memory_id=(
                None
                if value.get("memory_id", value.get("memoryId")) is None
                else _text(
                    value.get("memory_id", value.get("memoryId")),
                    "memory candidate memory_id",
                )
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        card: dict[str, Any] = {
            "id": self.card_id,
            "upgradeCount": self.upgrade_count,
            "customizes": [dict(value) for value in self.customizes],
        }
        return {
            "memory_id": self.memory_id,
            "memory_slot": self.memory_slot,
            "is_rental": self.is_rental,
            "produce_card": card,
            "produce_card_phase_type": self.phase_type,
            "abilities": [value.to_dict() for value in self.abilities],
            "role": self.role,
            "observed_fields": _json_value(self.observed_fields),
        }

    @property
    def upgrade(self) -> int:
        """Compatibility alias matching reward/loadout candidate spelling."""

        return self.upgrade_count

    @property
    def produce_card_phase_type(self) -> str | None:
        return self.phase_type

    @property
    def produce_card(self) -> Mapping[str, Any]:
        return {
            "id": self.card_id,
            "upgradeCount": self.upgrade_count,
            "customizes": tuple(dict(value) for value in self.customizes),
        }

    @property
    def instance_id(self) -> str | None:
        """Compatibility alias for callers that use instance terminology."""

        return self.memory_id

    @property
    def identity(self) -> str:
        """Return the strongest available identity for this candidate.

        New observations carry an opaque memory instance ID.  Legacy replay
        rows only carry the inherited card ID, so that ID remains the safe
        compatibility fallback.
        """

        return self.memory_id or self.card_id

    @property
    def signature(self) -> MemorySignature:
        return (
            self.card_id,
            self.upgrade_count,
            self.phase_type or "",
            _customize_key(self.customizes),
            tuple(value.key for value in self.abilities),
        )


# Names used by a few downstream callers are intentionally kept as aliases.
MemoryLoadoutCardCandidate = MemoryCardCandidate
MemoryLoadoutMemoryCandidate = MemoryCardCandidate


def _coerce_candidate(value: object) -> MemoryCardCandidate:
    if isinstance(value, MemoryCardCandidate):
        return value
    if isinstance(value, Mapping):
        return MemoryCardCandidate.from_mapping(value)
    raise TypeError("memory loadout candidates must contain objects")


def _coerce_combination(values: Iterable[object]) -> tuple[MemoryCardCandidate, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("memory loadout candidates must be an iterable")
    result = tuple(_coerce_candidate(value) for value in values)
    if len(result) != MEMORY_COUNT:
        raise ValueError(f"memory loadout candidates must contain exactly {MEMORY_COUNT} cards")
    slots = tuple(value.memory_slot for value in result if value.memory_slot is not None)
    if len(slots) != len(set(slots)):
        raise ValueError("memory loadout candidate memory slots must be unique")
    memory_ids = tuple(value.memory_id for value in result if value.memory_id is not None)
    if len(memory_ids) != len(set(memory_ids)):
        raise ValueError("memory loadout candidate memory IDs must be unique")
    return result


@dataclass(frozen=True, slots=True)
class MemoryLoadoutObservation:
    """One final-stage observation for a complete successful trajectory."""

    source_id: str
    trajectory_id: str
    flow: FlowKey
    idol_card_id: str
    terminal_score: int
    memories: tuple[MemoryCardCandidate, ...]
    support_cards: tuple[Mapping[str, Any], ...] = ()
    final_produce_cards: tuple[Mapping[str, Any], ...] = ()
    rank: int = 1
    grade: str | int | None = None
    schema: str = OBSERVATION_SCHEMA
    source_type: str = "leaderboard-replay"

    def __post_init__(self) -> None:
        _text(self.source_id, "memory observation source_id")
        _text(self.trajectory_id, "memory observation trajectory_id")
        if len(self.flow) != 3 or any(not isinstance(value, str) or not value for value in self.flow):
            raise ValueError("memory observation flow must contain three fields")
        _text(self.idol_card_id, "memory observation idol_card_id")
        _integer(self.terminal_score, "memory observation terminal_score", minimum=1)
        _integer(self.rank, "memory observation rank", minimum=1)
        memories = tuple(_coerce_candidate(value) for value in self.memories)
        if len(memories) != MEMORY_COUNT:
            raise ValueError(f"memory observation must contain exactly {MEMORY_COUNT} memories")
        slots = tuple(value.memory_slot for value in memories if value.memory_slot is not None)
        if len(slots) != len(set(slots)):
            raise ValueError("memory observation memory slots must be unique")
        memory_ids = tuple(
            value.memory_id for value in memories if value.memory_id is not None
        )
        if len(memory_ids) != len(set(memory_ids)):
            raise ValueError("memory observation memory IDs must be unique")
        object.__setattr__(self, "memories", memories)
        for name in ("support_cards", "final_produce_cards"):
            values = tuple(getattr(self, name))
            if any(not isinstance(value, Mapping) for value in values):
                raise TypeError(f"memory observation {name} must contain objects")
            object.__setattr__(self, name, tuple(_copy_mapping(value) for value in values))
        if self.grade is not None and (
            not isinstance(self.grade, (str, int)) or isinstance(self.grade, bool)
        ):
            raise TypeError("memory observation grade must be text, integer, or null")
        if self.schema != OBSERVATION_SCHEMA:
            raise ValueError("unsupported memory loadout observation schema")
        if self.source_type not in {
            "leaderboard-replay",
            "produce-history-summary",
        }:
            raise ValueError("unsupported memory loadout observation source_type")

    @property
    def score(self) -> int:
        return self.terminal_score

    @property
    def memory_loadout(self) -> tuple[MemoryCardCandidate, ...]:
        """Compatibility alias for the normalized replay field."""

        return self.memories

    @property
    def produce_cards(self) -> tuple[Mapping[str, Any], ...]:
        """Compatibility alias for the observed final Produce deck."""

        return self.final_produce_cards

    @property
    def memory_abilities(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            {
                "memory_id": memory.memory_id,
                "memory_slot": memory.memory_slot,
                "id": ability.ability_id,
                "level": ability.level,
                **dict(ability.observed_fields),
            }
            for memory in self.memories
            for ability in memory.abilities
        )

    @property
    def flow_string(self) -> str:
        return flow_string(self.flow)

    @property
    def source_kind(self) -> str:
        return self.source_type

    @property
    def combination_key(self) -> CombinationKey:
        return tuple(sorted(value.card_id for value in self.memories))

    @property
    def instance_key(self) -> tuple[str, ...]:
        """Return the strongest available per-memory identity tuple."""

        return tuple(sorted(value.identity for value in self.memories))

    @property
    def exact_combination_key(self) -> ExactCombinationKey:
        return tuple(sorted((value.signature for value in self.memories)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "trajectory_id": self.trajectory_id,
            "flow": list(self.flow),
            "produce_id": self.flow[0],
            "plan_type": self.flow[1],
            "exam_effect_type": self.flow[2],
            "idol_card_id": self.idol_card_id,
            "terminal_score": self.terminal_score,
            "rank": self.rank,
            "grade": self.grade,
            "memories": [value.to_dict() for value in self.memories],
            # Keep familiar replay names so a report can be joined back to the
            # source without an adapter-specific second schema.
            "memory_loadout": [
                {
                    "memory_id": value.memory_id,
                    "memory_slot": value.memory_slot,
                    "is_rental": value.is_rental,
                    "produce_card": {
                        "id": value.card_id,
                        "upgradeCount": value.upgrade_count,
                        "customizes": [dict(item) for item in value.customizes],
                    },
                    "produce_card_phase_type": value.phase_type,
                }
                for value in self.memories
            ],
            "memory_abilities": [
                {
                    "memory_slot": value.memory_slot,
                    **ability.to_dict(),
                }
                for value in self.memories
                for ability in value.abilities
            ],
            "support_cards": [dict(value) for value in self.support_cards],
            "produce_cards": [dict(value) for value in self.final_produce_cards],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MemoryLoadoutObservation":
        if not isinstance(value, Mapping):
            raise TypeError("memory observation must be an object")
        raw_flow = value.get("flow")
        if isinstance(raw_flow, str):
            parts = tuple(raw_flow.split("|"))
            if len(parts) != 3:
                raise ValueError("memory observation flow must contain three fields")
            parsed_flow = _flow(*parts, label="observation.flow")
        elif isinstance(raw_flow, Sequence) and not isinstance(raw_flow, (str, bytes)):
            if len(raw_flow) != 3:
                raise ValueError("memory observation flow must contain three fields")
            parsed_flow = _flow(*raw_flow, label="observation.flow")
        else:
            parsed_flow = _flow(
                value.get("produce_id"),
                value.get("plan_type"),
                value.get("exam_effect_type"),
                label="observation.flow",
            )
        raw_memories = value.get("memories", value.get("memory_loadout"))
        if not isinstance(raw_memories, Sequence) or isinstance(raw_memories, (str, bytes)):
            raise ValueError("memory observation memories must be an array")
        abilities_by_slot: dict[int, list[object]] = defaultdict(list)
        raw_abilities = value.get("memory_abilities", ())
        if isinstance(raw_abilities, Sequence) and not isinstance(raw_abilities, (str, bytes)):
            for raw in raw_abilities:
                if isinstance(raw, Mapping):
                    slot = raw.get("memory_slot", raw.get("slot"))
                    if isinstance(slot, int) and not isinstance(slot, bool):
                        abilities_by_slot[slot].append(raw)
        memories: list[MemoryCardCandidate] = []
        for index, raw in enumerate(raw_memories):
            if not isinstance(raw, Mapping):
                raise ValueError("memory observation memory rows must be objects")
            candidate = MemoryCardCandidate.from_mapping(raw)
            if not candidate.abilities and candidate.memory_slot is not None:
                candidate = MemoryCardCandidate(
                    card_id=candidate.card_id,
                    upgrade_count=candidate.upgrade_count,
                    phase_type=candidate.phase_type,
                    abilities=_coerce_abilities(abilities_by_slot.get(candidate.memory_slot, ())),
                    memory_slot=candidate.memory_slot,
                    is_rental=candidate.is_rental,
                    customizes=candidate.customizes,
                    role=candidate.role,
                    observed_fields=candidate.observed_fields,
                    memory_id=candidate.memory_id,
                )
            elif not candidate.abilities and index in abilities_by_slot:
                candidate = MemoryCardCandidate(
                    card_id=candidate.card_id,
                    upgrade_count=candidate.upgrade_count,
                    phase_type=candidate.phase_type,
                    abilities=_coerce_abilities(abilities_by_slot[index]),
                    memory_slot=candidate.memory_slot,
                    is_rental=candidate.is_rental,
                    customizes=candidate.customizes,
                    role=candidate.role,
                    observed_fields=candidate.observed_fields,
                    memory_id=candidate.memory_id,
                )
            memories.append(candidate)
        support = value.get("support_cards", ())
        final_cards = value.get("produce_cards", value.get("final_produce_cards", ()))
        if not isinstance(support, Sequence) or isinstance(support, (str, bytes)):
            raise ValueError("memory observation support_cards must be an array")
        if not isinstance(final_cards, Sequence) or isinstance(final_cards, (str, bytes)):
            raise ValueError("memory observation produce_cards must be an array")
        return cls(
            source_id=_text(value.get("source_id", value.get("trajectory_id")), "observation.source_id"),
            trajectory_id=_text(value.get("trajectory_id", value.get("source_id")), "observation.trajectory_id"),
            flow=parsed_flow,
            idol_card_id=_text(value.get("idol_card_id"), "observation.idol_card_id"),
            terminal_score=_integer(value.get("terminal_score", value.get("score")), "observation.terminal_score", minimum=1),
            memories=tuple(memories),
            support_cards=tuple(support),
            final_produce_cards=tuple(final_cards),
            rank=_integer(value.get("rank", 1), "observation.rank", minimum=1),
            grade=value.get("grade"),
            source_type=_text(
                value.get("source_type", "leaderboard-replay"),
                "observation.source_type",
            ),
        )


# An explicit singular alias is useful to callers that treat the projection
# as a row rather than a training collection.
MemoryLoadoutObservationRow = MemoryLoadoutObservation
MemoryLoadoutCandidate = MemoryCardCandidate


@dataclass(frozen=True, slots=True)
class MemoryLoadoutObservationReport:
    episode_count: int
    retained_count: int
    skipped_count: int
    reason_counts: Mapping[str, int]
    scope_counts: Mapping[FlowKey, int]
    source_type_counts: Mapping[str, int] = field(default_factory=dict)
    schema: str = REPORT_SCHEMA

    def __post_init__(self) -> None:
        for name, value in (
            ("episode_count", self.episode_count),
            ("retained_count", self.retained_count),
            ("skipped_count", self.skipped_count),
        ):
            _integer(value, f"observation report {name}")
        if self.retained_count > self.episode_count:
            raise ValueError("observation report retained_count exceeds episode_count")
        if not isinstance(self.reason_counts, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, int) or value < 0
            for key, value in self.reason_counts.items()
        ):
            raise TypeError("observation report reason_counts must be non-negative counts")
        if not isinstance(self.scope_counts, Mapping):
            raise TypeError("observation report scope_counts must be a mapping")
        if not isinstance(self.source_type_counts, Mapping):
            raise TypeError("observation report source_type_counts must be a mapping")
        if self.schema != REPORT_SCHEMA:
            raise ValueError("unsupported memory observation report schema")

    @property
    def observation_count(self) -> int:
        return self.retained_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "episode_count": self.episode_count,
            "retained_count": self.retained_count,
            "skipped_count": self.skipped_count,
            "reason_counts": dict(self.reason_counts),
            "scope_counts": {
                flow_string(key): value for key, value in self.scope_counts.items()
            },
            "source_type_counts": dict(self.source_type_counts),
        }


def _raw_rows(source: str | Path | Mapping[str, Any] | Sequence[object] | LeaderboardReplayEpisode) -> tuple[object, ...]:
    if isinstance(source, LeaderboardReplayEpisode) or isinstance(source, Mapping):
        return (source,)
    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(path)
        text = path.read_text(encoding="utf-8-sig")
        if not text.strip():
            raise ValueError("leaderboard episode source is empty")
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, Mapping):
            return (decoded,)
        if isinstance(decoded, Sequence) and not isinstance(decoded, (str, bytes)):
            return tuple(decoded)
        if decoded is not None:
            raise ValueError("leaderboard episode source must contain objects")
        rows: list[object] = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"leaderboard episode line {line_number} is malformed") from error
            rows.append(row)
        return tuple(rows)
    if isinstance(source, (str, bytes)):
        raise TypeError("episode source text must be a path, not an iterable")
    return tuple(source)


def _episode_from_row(value: object) -> tuple[LeaderboardReplayEpisode, Mapping[str, Any]]:
    if isinstance(value, LeaderboardReplayEpisode):
        return value, {}
    if not isinstance(value, Mapping):
        raise TypeError("leaderboard episode row must be an object")
    return read_leaderboard_episode(value), value


def _is_final_episode(episode: LeaderboardReplayEpisode) -> bool:
    return str(episode.step_type) == _FINAL_STAGE or episode.audition_index == 2


def _memory_candidates_from_episode(
    episode: LeaderboardReplayEpisode,
) -> tuple[MemoryCardCandidate, ...]:
    if len(episode.memory_loadout) != MEMORY_COUNT:
        raise ValueError("episode memory_loadout is not a complete four-memory loadout")
    ability_rows: dict[int, list[MemoryAbilityObservation]] = defaultdict(list)
    for index, raw in enumerate(episode.memory_abilities):
        if not isinstance(raw, Mapping):
            raise ValueError(f"episode memory ability {index} is not an object")
        slot = _integer(raw.get("memory_slot"), f"episode memory ability {index}.memory_slot")
        ability_rows[slot].append(
            MemoryAbilityObservation(
                _text(raw.get("id"), f"episode memory ability {index}.id"),
                _integer(raw.get("level"), f"episode memory ability {index}.level"),
                _copy_mapping(raw),
            )
        )
    result: list[MemoryCardCandidate] = []
    for index, raw in enumerate(episode.memory_loadout):
        if not isinstance(raw, Mapping):
            raise ValueError(f"episode memory loadout {index} is not an object")
        slot = _integer(raw.get("memory_slot"), f"episode memory loadout {index}.memory_slot")
        if slot in {value.memory_slot for value in result}:
            raise ValueError("episode memory_loadout contains duplicate slots")
        candidate = MemoryCardCandidate.from_mapping(
            {
                **dict(raw),
                "abilities": tuple(ability_rows.get(slot, ())),
            }
        )
        result.append(candidate)
    return _coerce_combination(result)


def project_memory_loadout_episode(
    source: LeaderboardReplayEpisode | Mapping[str, Any],
    *,
    source_id: str | None = None,
    require_success: bool = True,
) -> MemoryLoadoutObservation:
    """Project one final replay row to an immutable memory observation."""

    episode, raw = _episode_from_row(source)
    if not _is_final_episode(episode):
        raise ValueError("memory loadout observation requires an AuditionFinal row")
    if require_success and (episode.rank != 1 or episode.terminal_score <= 0):
        raise ValueError("memory loadout observation requires a successful rank-1 row")
    flow = _flow(episode.produce_id, episode.plan_type, episode.exam_effect_type)
    candidate_source = source_id
    if candidate_source is None and isinstance(raw, Mapping):
        raw_source = raw.get("source_id")
        candidate_source = raw_source if isinstance(raw_source, str) and raw_source else None
    candidate_source = candidate_source or episode.trajectory_id
    grade = raw.get("grade") if isinstance(raw, Mapping) else None
    return MemoryLoadoutObservation(
        source_id=_text(candidate_source, "memory observation source_id"),
        trajectory_id=episode.trajectory_id,
        flow=flow,
        idol_card_id=episode.idol_card_id,
        terminal_score=episode.terminal_score,
        memories=_memory_candidates_from_episode(episode),
        support_cards=episode.support_cards,
        final_produce_cards=episode.produce_cards,
        rank=episode.rank,
        grade=grade,
    )


# Verb-first and historical naming aliases.
observe_memory_loadout_episode = project_memory_loadout_episode
observe_leaderboard_episode = project_memory_loadout_episode
project_leaderboard_memory_loadout = project_memory_loadout_episode


def build_memory_loadout_observations(
    source: str | Path | Mapping[str, Any] | Sequence[object] | LeaderboardReplayEpisode = DEFAULT_LEADERBOARD_EPISODES,
    *,
    require_complete: bool = True,
    require_success: bool = True,
) -> tuple[tuple[MemoryLoadoutObservation, ...], MemoryLoadoutObservationReport]:
    """Build one final observation per complete trajectory.

    A complete trajectory contains all three audition stages.  A source with
    only a single final row can be inspected explicitly with
    ``require_complete=False``; the default refuses it so incomplete captures
    cannot silently train the prior.
    """

    rows = _raw_rows(source)
    parsed: list[tuple[LeaderboardReplayEpisode, Mapping[str, Any]]] = []
    reasons: Counter[str] = Counter()
    for row in rows:
        try:
            parsed.append(_episode_from_row(row))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            reasons["malformed-episode"] += 1
    groups: dict[tuple[FlowKey, str, str], list[tuple[LeaderboardReplayEpisode, Mapping[str, Any]]]] = defaultdict(list)
    for episode, raw in parsed:
        key = (
            _flow(episode.produce_id, episode.plan_type, episode.exam_effect_type),
            episode.idol_card_id,
            episode.trajectory_id,
        )
        groups[key].append((episode, raw))
    result: list[MemoryLoadoutObservation] = []
    for key, members in sorted(groups.items(), key=lambda item: repr(item[0])):
        flow, idol_card_id, trajectory_id = key
        stages = {str(episode.step_type) for episode, _raw in members}
        if require_complete and (
            len(members) != 3
            or not {_FINAL_STAGE, "ProduceStepType_AuditionMid1", "ProduceStepType_AuditionMid2"} <= stages
        ):
            reasons["incomplete-trajectory"] += 1
            continue
        finals = tuple((episode, raw) for episode, raw in members if _is_final_episode(episode))
        if len(finals) != 1:
            reasons["final-row-missing-or-ambiguous"] += 1
            continue
        episode, raw = finals[0]
        if episode.idol_card_id != idol_card_id or episode.trajectory_id != trajectory_id:
            reasons["trajectory-identity-mismatch"] += 1
            continue
        try:
            result.append(
                project_memory_loadout_episode(
                    episode,
                    source_id=(
                        raw.get("source_id")
                        if isinstance(raw, Mapping) and isinstance(raw.get("source_id"), str)
                        else trajectory_id
                    ),
                    require_success=require_success,
                )
            )
        except (KeyError, TypeError, ValueError):
            reasons["unprojectable-memory-loadout"] += 1
    reasons["retained"] = len(result)
    report = MemoryLoadoutObservationReport(
        episode_count=len(rows),
        retained_count=len(result),
        skipped_count=max(0, len(rows) - len(result)),
        reason_counts=dict(reasons),
        scope_counts=Counter(value.flow for value in result),
        source_type_counts={"leaderboard-replay": len(result)},
    )
    return tuple(result), report


build_leaderboard_memory_loadout_observations = build_memory_loadout_observations


def _history_token(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _history_field(
    value: Mapping[str, Any],
    *names: str,
    default: object = None,
) -> object:
    wanted = {_history_token(name) for name in names}
    for key, item in value.items():
        if _history_token(key) in wanted:
            return item
    return default


def _history_mapping_field(
    value: Mapping[str, Any],
    *names: str,
) -> Mapping[str, Any] | None:
    raw = _history_field(value, *names)
    return raw if isinstance(raw, Mapping) else None


def _history_sequence_field(
    value: Mapping[str, Any],
    *names: str,
) -> tuple[object, ...]:
    raw = _history_field(value, *names, default=())
    if isinstance(raw, Mapping):
        nested = _history_field(raw, "rows", "items", "values", default=())
        raw = nested
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    return tuple(raw)


def _history_objects(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """Extract full ProduceHistory objects without requiring replay actions."""

    result: list[Mapping[str, Any]] = []
    visited: set[int] = set()

    def visit(value: object, depth: int = 0) -> None:
        if depth > 6 or not isinstance(value, Mapping):
            return
        marker = id(value)
        if marker in visited:
            return
        visited.add(marker)
        has_history_shape = any(
            _history_field(value, name, default=None) is not None
            for name in (
                "auditions",
                "deckMemories",
                "deckSupportCards",
                "produceCards",
            )
        )
        if has_history_shape:
            result.append(value)
        for key, nested in value.items():
            token = _history_token(key)
            if token in {
                "producehistory",
                "history",
                "histories",
                "rows",
                "items",
                "ranking",
                "rankings",
                "ranks",
                "response",
                "result",
            }:
                if isinstance(nested, Mapping):
                    visit(nested, depth + 1)
                elif isinstance(nested, Sequence) and not isinstance(
                    nested, (str, bytes)
                ):
                    for child in nested:
                        visit(child, depth + 1)

    visit(payload)
    # The same history can be reached through both response and produceHistory
    # wrappers; retain first-seen order and use canonical object content for a
    # local duplicate guard.
    unique: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for value in result:
        try:
            marker = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            marker = str(id(value))
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(value)
    return tuple(unique)


def _history_json_int(value: object, label: str) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value, 10)
    return None


def _history_plan_type(value: object) -> str | None:
    if isinstance(value, str) and value:
        if value in _PLAN_TYPE_BY_INT.values():
            return value
        token = _history_token(value)
        for plan in _PLAN_TYPE_BY_INT.values():
            if _history_token(plan) == token:
                return plan
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return _PLAN_TYPE_BY_INT.get(value)
    if isinstance(value, str) and value.isdecimal():
        return _PLAN_TYPE_BY_INT.get(int(value, 10))
    return None


def _history_request(payload: Mapping[str, Any], history: Mapping[str, Any]) -> dict[str, Any]:
    raw_request = _history_mapping_field(
        payload,
        "request",
        "job_request",
        "jobRequest",
    )
    history_request = _history_mapping_field(
        history,
        "request",
        "job_request",
        "jobRequest",
    )
    if raw_request is None:
        raw_request = history_request
    request_containers: tuple[Mapping[str, Any], ...] = tuple(
        value
        for value in (
            raw_request,
            history_request,
            history,
            _history_mapping_field(history, "memory"),
            _history_mapping_field(history, "profile"),
        )
        if isinstance(value, Mapping)
    )
    fields = (
        ("public_user_id", "publicUserId"),
        ("user_memory_id", "userMemoryId"),
        ("is_self", "isSelf"),
        ("produce_group_id", "produceGroupId"),
        ("idol_card_id", "idolCardId"),
    )
    request: dict[str, Any] = {}
    for canonical, *aliases in fields:
        item = next(
            (
                _history_field(container, canonical, *aliases, default=None)
                for container in request_containers
                if _history_field(container, canonical, *aliases, default=None)
                is not None
            ),
            None,
        )
        if item is None:
            continue
        if canonical == "is_self":
            if not isinstance(item, bool):
                continue
        elif not isinstance(item, str) or not item:
            continue
        request[canonical] = item
    return request


def _history_auditions(history: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        value
        for value in _history_sequence_field(history, "auditions")
        if isinstance(value, Mapping)
    )


def _history_final_audition(
    auditions: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    for audition in reversed(auditions):
        step_type = _history_field(audition, "stepType", "step_type", default="")
        if "auditionfinal" in _history_token(step_type) or _history_token(step_type) in {
            "final",
            "audition3",
        }:
            return audition
        index = _history_json_int(
            _history_field(audition, "auditionIndex", "audition_index", "index", default=None),
            "audition index",
        )
        if index == 2:
            return audition
    return auditions[-1] if auditions else None


def _history_card_row(value: Mapping[str, Any]) -> dict[str, Any] | None:
    card = _history_mapping_field(value, "produceCard", "produce_card", "card")
    if card is None:
        card = value
    card_id = _history_field(card, "id", "cardId", "card_id", default=None)
    if not isinstance(card_id, str) or not card_id:
        return None
    upgrade = _history_json_int(
        _history_field(card, "upgradeCount", "upgrade_count", "upgrade", default=0),
        "card upgrade",
    )
    if upgrade is None:
        return None
    customizes = _history_sequence_field(card, "customizes", "customize")
    normalized_customizes: list[Mapping[str, Any]] = []
    for customize in customizes:
        if not isinstance(customize, Mapping):
            return None
        customize_id = _history_field(customize, "id", "customizeId", "customize_id", default=None)
        count = _history_json_int(
            _history_field(customize, "customizeCount", "customize_count", "count", default=0),
            "customize count",
        )
        if not isinstance(customize_id, str) or not customize_id or count is None:
            return None
        normalized_customizes.append(
            {**dict(customize), "id": customize_id, "customizeCount": count}
        )
    return {
        **dict(card),
        "id": card_id,
        "upgradeCount": upgrade,
        "customizes": normalized_customizes,
    }


def _history_ability_rows(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    rows = _history_sequence_field(value, "abilities", "memoryAbilities", "memory_abilities")
    result: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        ability_id = _history_field(row, "id", "abilityId", "ability_id", default=None)
        level = _history_json_int(_history_field(row, "level", default=0), "ability level")
        if isinstance(ability_id, str) and ability_id and level is not None:
            result.append({**dict(row), "id": ability_id, "level": level})
    return tuple(result)


def _history_memory_rows(history: Mapping[str, Any]) -> tuple[dict[str, Any], ...] | None:
    entries = _history_sequence_field(history, "deckMemories", "deck_memories")
    if len(entries) != MEMORY_COUNT:
        return None
    result: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            return None
        nested = _history_mapping_field(entry, "memory") or entry
        card = _history_card_row(nested)
        if card is None:
            return None
        rental = _history_field(entry, "isRental", "is_rental", default=False)
        if not isinstance(rental, bool):
            return None
        phase = _history_field(
            nested,
            "produceCardPhaseType",
            "produce_card_phase_type",
            "phaseType",
            "phase_type",
            default=None,
        )
        if phase is not None and not isinstance(phase, str):
            return None
        memory_id = _history_field(
            entry,
            "memoryId",
            "memory_id",
            default=_history_field(nested, "memoryId", "memory_id", default=None),
        )
        if memory_id is not None and (
            not isinstance(memory_id, str) or not memory_id
        ):
            return None
        result.append(
            {
                "memory_id": memory_id,
                "memory_slot": index,
                "is_rental": rental,
                "produce_card": card,
                "produce_card_phase_type": phase,
                "abilities": _history_ability_rows(nested),
                "observed_fields": dict(entry),
            }
        )
    return tuple(result)


def _history_memory_plan_type(
    history: Mapping[str, Any],
    entries: Sequence[object],
) -> tuple[str | None, str | None]:
    """Resolve the result-memory `planType` without conflating deck origins.

    `produceHistory.memory` is the run's nested idol-memory object and its
    plan type is the authoritative active plan.  The four
    `deckMemories[].memory` objects are inherited memories; their own plan
    types may legitimately differ because memories can be produced under a
    different plan.  We therefore validate that each observed nested value,
    when present, is a known plan token, but do not require all four origins to
    equal the active result-memory plan.
    """

    memory = _history_mapping_field(history, "memory")
    memory_plan = _history_plan_type(
        _history_field(memory, "planType", "plan_type", default=None)
        if memory is not None
        else None
    )
    if memory_plan is None:
        return None, "missing-memory-plan-type"
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            return None, f"invalid-deck-memory:{index}"
        nested = _history_mapping_field(entry, "memory") or entry
        nested_value = _history_field(nested, "planType", "plan_type", default=None)
        if nested_value is not None and _history_plan_type(nested_value) is None:
            return None, f"invalid-nested-memory-plan-type:{index}"
    return memory_plan, None


def _history_request_key(
    request: Mapping[str, Any],
    history: Mapping[str, Any],
    score: int,
) -> str:
    if request:
        identity: object = {"request": dict(request), "score": score}
    else:
        identity = {
            "history": history,
            "score": score,
        }
    try:
        serialized = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        serialized = repr(identity)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _history_observation_from_object(
    history: Mapping[str, Any],
    *,
    payload: Mapping[str, Any],
    source_name: str,
    history_index: int,
    require_success: bool,
    plan_type_override: str | None = None,
    exam_effect_type_override: str | None = None,
    database: Path = DEFAULT_DATABASE,
) -> tuple[MemoryLoadoutObservation | None, str | None, str]:
    produce_id = _history_field(
        history,
        "produceId",
        "produce_id",
        "produceGroupId",
        "produce_group_id",
        default=None,
    )
    idol_card_id = _history_field(
        history,
        "idolCardId",
        "idol_card_id",
        default=None,
    )
    if not isinstance(produce_id, str) or not produce_id:
        return None, "missing-produce-id", ""
    if not isinstance(idol_card_id, str) or not idol_card_id:
        return None, "missing-idol-card-id", ""
    auditions = _history_auditions(history)
    final = _history_final_audition(auditions)
    score = _history_json_int(
        _history_field(
            history,
            "finalScore",
            "final_score",
            "score",
            "terminalScore",
            "terminal_score",
            default=None,
        ),
        "history final score",
    )
    if score is None and final is not None:
        score = _history_json_int(
            _history_field(final, "score", "terminalScore", "terminal_score", default=None),
            "final audition score",
        )
    if score is None or score <= 0:
        return None, "missing-or-nonpositive-final-score", ""
    rank_value = _history_json_int(
        _history_field(history, "rank", default=None),
        "history rank",
    )
    if rank_value is None and final is not None:
        rank_value = _history_json_int(
            _history_field(final, "rank", default=None),
            "final audition rank",
        )
    if rank_value is None:
        return None, "missing-final-rank", ""
    if require_success and rank_value != 1:
        return None, "non-rank-one-history", ""
    raw_memory_entries = _history_sequence_field(history, "deckMemories", "deck_memories")
    memory_plan, memory_plan_reason = _history_memory_plan_type(
        history,
        raw_memory_entries,
    )
    if memory_plan_reason is not None:
        return None, memory_plan_reason, ""
    memory_rows = _history_memory_rows(history)
    if memory_rows is None:
        return None, "missing-or-incomplete-deck-memories", ""
    plan_type = _history_field(
        history,
        "planType",
        "plan_type",
        default=None,
    )
    if not isinstance(plan_type, str) or not plan_type:
        # History responses commonly carry plan type on the nested audition
        # summary.  Preserve the value only when it is explicitly present.
        plan_type = (
            _history_field(final, "planType", "plan_type", default=None)
            if final is not None
            else None
        )
    if not isinstance(plan_type, str) or not plan_type:
        plan_type = memory_plan
    else:
        plan_type = _history_plan_type(plan_type)
    if not isinstance(plan_type, str) or not plan_type:
        plan_type = plan_type_override
    if not isinstance(plan_type, str) or not plan_type:
        return None, "missing-plan-type", ""
    if memory_plan is not None and plan_type != memory_plan:
        return None, "memory-plan-type-mismatch", ""
    profile = get_idol_profile(idol_card_id, Path(database))
    if profile is not None and profile.plan_type != plan_type:
        return None, "master-plan-type-mismatch", ""
    exam_effect_type = _history_field(
        history,
        "examEffectType",
        "exam_effect_type",
        "effectType",
        "effect_type",
        default=None,
    )
    if not isinstance(exam_effect_type, str) or not exam_effect_type:
        exam_effect_type = (
            _history_field(final, "examEffectType", "exam_effect_type", default=None)
            if final is not None
            else None
        )
    master_exam_effect_type = profile.exam_effect_type if profile is not None else None
    if master_exam_effect_type is not None:
        if isinstance(exam_effect_type, str) and exam_effect_type != master_exam_effect_type:
            return None, "master-exam-effect-type-mismatch", ""
        exam_effect_type = master_exam_effect_type
    if not isinstance(exam_effect_type, str) or not exam_effect_type:
        exam_effect_type = exam_effect_type_override
    if not isinstance(exam_effect_type, str) or not exam_effect_type:
        if profile is None:
            return None, "missing-master-idol-profile", ""
    if not isinstance(exam_effect_type, str) or not exam_effect_type:
        return None, "missing-exam-effect-type", ""
    grade = _history_field(history, "grade", "produceGrade", "produce_grade", default=None)
    if grade is None and final is not None:
        grade = _history_field(final, "grade", "produceGrade", "produce_grade", default=None)
    if grade is not None and (
        not isinstance(grade, (str, int)) or isinstance(grade, bool)
    ):
        grade = None
    request = _history_request(payload, history)
    dedupe_key = _history_request_key(request, history, score)
    source_id = "produce-history:" + dedupe_key
    final_cards = tuple(
        value
        for value in _history_sequence_field(history, "produceCards", "produce_cards")
        if isinstance(value, Mapping)
    )
    support_cards = tuple(
        value
        for value in _history_sequence_field(history, "deckSupportCards", "deck_support_cards")
        if isinstance(value, Mapping)
    )
    observation = MemoryLoadoutObservation(
        source_id=source_id,
        trajectory_id=source_id,
        flow=(produce_id, plan_type, exam_effect_type),
        idol_card_id=idol_card_id,
        terminal_score=score,
        memories=tuple(
            MemoryCardCandidate.from_mapping(
                {
                    **dict(row),
                    "observed_fields": {
                        **dict(row.get("observed_fields", {})),
                        "source_name": source_name,
                        "history_index": history_index,
                    },
                }
            )
            for row in memory_rows
        ),
        support_cards=support_cards,
        final_produce_cards=final_cards,
        rank=rank_value,
        grade=grade,
        source_type="produce-history-summary",
    )
    return observation, None, dedupe_key


def build_memory_loadout_observations_from_history_sources(
    sources: str | Path | Sequence[str | Path],
    *,
    require_success: bool = True,
    plan_type: str | None = None,
    exam_effect_type: str | None = None,
    database: str | Path = DEFAULT_DATABASE,
) -> tuple[tuple[MemoryLoadoutObservation, ...], MemoryLoadoutObservationReport]:
    """Build memory observations from raw mode `Produce.History` responses.

    These responses intentionally need not contain replay actions.  Their
    output is safe for memory/deck composition learning only; no
    ``LeaderboardReplayEpisode`` or action/outer observation is manufactured.
    Raw files are de-duplicated by the History job request plus final score
    (with canonical history content as a fallback when a request is absent).
    """

    raw_sources = list_leaderboard_raw_sources(sources)
    result: list[MemoryLoadoutObservation] = []
    reasons: Counter[str] = Counter()
    seen: set[str] = set()
    source_counts: Counter[str] = Counter()
    history_count = 0
    for raw_source in raw_sources:
        try:
            raw_payload = json.loads(raw_source.path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"raw history source is unreadable: {raw_source.path}") from error
        if not isinstance(raw_payload, Mapping):
            raise ValueError(f"raw history source must be an object: {raw_source.path}")
        histories = _history_objects(raw_payload)
        if not histories:
            reasons["no-produce-history-object"] += 1
            continue
        for history_index, history in enumerate(histories):
            history_count += 1
            observation, reason, dedupe_key = _history_observation_from_object(
                history,
                payload=raw_payload,
                source_name=raw_source.name,
                history_index=history_index,
                require_success=require_success,
                plan_type_override=plan_type,
                exam_effect_type_override=exam_effect_type,
                database=Path(database),
            )
            if reason is not None or observation is None:
                reasons[reason or "unprojectable-history"] += 1
                continue
            if dedupe_key in seen:
                reasons["duplicate-job-request-score"] += 1
                continue
            seen.add(dedupe_key)
            result.append(observation)
            source_counts[observation.source_type] += 1
    reasons["retained"] = len(result)
    report = MemoryLoadoutObservationReport(
        episode_count=history_count,
        retained_count=len(result),
        skipped_count=max(0, history_count - len(result)),
        reason_counts=dict(reasons),
        scope_counts=Counter(value.flow for value in result),
        source_type_counts=dict(source_counts),
    )
    return tuple(result), report


def build_memory_loadout_observations_from_history_collection(
    collection: LeaderboardRawHistoryCollection,
    *,
    require_success: bool = True,
    plan_type: str | None = None,
    exam_effect_type: str | None = None,
    database: str | Path = DEFAULT_DATABASE,
) -> tuple[tuple[MemoryLoadoutObservation, ...], MemoryLoadoutObservationReport]:
    """Project retained records from ``collect_list_latest_history_sources``.

    The collector's ``dropped`` rows intentionally contain no raw history
    payload, so no-action histories must use
    :func:`build_memory_loadout_observations_from_history_sources` directly.
    This adapter is still useful when a collection already retained the
    de-identified History objects and makes that boundary explicit.
    """

    if not isinstance(collection, LeaderboardRawHistoryCollection):
        raise TypeError("collection must be LeaderboardRawHistoryCollection")
    result: list[MemoryLoadoutObservation] = []
    reasons: Counter[str] = Counter()
    seen: set[str] = set()
    source_counts: Counter[str] = Counter()
    for history_index, record in enumerate(collection.records):
        observation, reason, dedupe_key = _history_observation_from_object(
            record.history,
            payload={},
            source_name=record.source_name,
            history_index=history_index,
            require_success=require_success,
            plan_type_override=plan_type,
            exam_effect_type_override=exam_effect_type,
            database=Path(database),
        )
        if reason is not None or observation is None:
            reasons[reason or "unprojectable-history"] += 1
            continue
        if dedupe_key in seen:
            reasons["duplicate-job-request-score"] += 1
            continue
        seen.add(dedupe_key)
        result.append(observation)
        source_counts[observation.source_type] += 1
    reasons["retained"] = len(result)
    report = MemoryLoadoutObservationReport(
        episode_count=len(collection.records),
        retained_count=len(result),
        skipped_count=max(0, len(collection.records) - len(result)),
        reason_counts=dict(reasons),
        scope_counts=Counter(value.flow for value in result),
        source_type_counts=dict(source_counts),
    )
    return tuple(result), report


build_history_memory_loadout_observations = (
    build_memory_loadout_observations_from_history_sources
)
build_memory_loadout_observations_from_history = (
    build_memory_loadout_observations_from_history_sources
)
build_memory_loadout_observations_from_collection = (
    build_memory_loadout_observations_from_history_collection
)


def merge_memory_loadout_observations(
    normalized: Sequence[MemoryLoadoutObservation],
    history: Sequence[MemoryLoadoutObservation],
    *,
    normalized_report: MemoryLoadoutObservationReport | None = None,
    history_report: MemoryLoadoutObservationReport | None = None,
) -> tuple[tuple[MemoryLoadoutObservation, ...], MemoryLoadoutObservationReport]:
    """Merge replay and summary-only observations with deterministic de-dup.

    Raw History rows are already de-duplicated by request+score.  This second
    boundary removes an identical final loadout that is present in both a
    normalized replay file and a raw History result, without joining merely
    on an opaque trajectory ID from either producer.
    """

    values = tuple(normalized) + tuple(history)
    if not all(isinstance(value, MemoryLoadoutObservation) for value in values):
        raise TypeError("memory observations must contain MemoryLoadoutObservation values")
    result: list[MemoryLoadoutObservation] = []
    seen: set[tuple[FlowKey, str, int, CombinationKey, ExactCombinationKey]] = set()
    duplicate_count = 0
    for value in values:
        key = (
            value.flow,
            value.idol_card_id,
            value.terminal_score,
            value.combination_key,
            value.exact_combination_key,
        )
        if key in seen:
            duplicate_count += 1
            continue
        seen.add(key)
        result.append(value)
    reasons: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    episode_count = 0
    skipped_count = 0
    for report in (normalized_report, history_report):
        if report is None:
            continue
        episode_count += report.episode_count
        skipped_count += report.skipped_count
        reasons.update(report.reason_counts)
        source_counts.update(report.source_type_counts)
    if duplicate_count:
        reasons["duplicate-cross-source-loadout-score"] += duplicate_count
        skipped_count += duplicate_count
    reasons["retained"] = len(result)
    report = MemoryLoadoutObservationReport(
        episode_count=max(episode_count, len(values)),
        retained_count=len(result),
        skipped_count=max(skipped_count, max(0, max(episode_count, len(values)) - len(result))),
        reason_counts=dict(reasons),
        scope_counts=Counter(value.flow for value in result),
        source_type_counts=dict(source_counts),
    )
    return tuple(result), report


def build_memory_loadout_observations_from_sources(
    source: str | Path = DEFAULT_LEADERBOARD_EPISODES,
    *,
    history_sources: str | Path | Sequence[str | Path] | LeaderboardRawHistoryCollection | None = None,
    require_complete: bool = True,
    history_database: str | Path = DEFAULT_DATABASE,
    history_plan_type: str | None = None,
    history_exam_effect_type: str | None = None,
) -> tuple[tuple[MemoryLoadoutObservation, ...], MemoryLoadoutObservationReport]:
    """Build normalized observations and optionally merge raw History rows."""

    normalized, normalized_report = build_memory_loadout_observations(
        source,
        require_complete=require_complete,
    )
    if history_sources is None:
        return normalized, normalized_report
    if isinstance(history_sources, LeaderboardRawHistoryCollection):
        history, history_report = build_memory_loadout_observations_from_history_collection(
            history_sources,
            database=history_database,
            plan_type=history_plan_type,
            exam_effect_type=history_exam_effect_type,
        )
    else:
        history, history_report = build_memory_loadout_observations_from_history_sources(
            history_sources,
            database=history_database,
            plan_type=history_plan_type,
            exam_effect_type=history_exam_effect_type,
        )
    return merge_memory_loadout_observations(
        normalized,
        history,
        normalized_report=normalized_report,
        history_report=history_report,
    )


def _weighted_score(scores: Sequence[int], maximum: int) -> float:
    if not scores or maximum <= 0:
        return 0.0
    return fmean(float(value) / maximum for value in scores)


def _bounded_utility(
    support: int,
    trajectory_count: int,
    score_signal: float,
    upgrade_signal: float = 0.0,
    *,
    minimum_support: int = MIN_USEFUL_SUPPORT,
) -> int:
    if support < minimum_support or trajectory_count <= 0:
        return 0
    value = (
        520.0 * support / trajectory_count
        + 420.0 * max(0.0, min(1.0, score_signal))
        + 60.0 * max(0.0, min(1.0, upgrade_signal))
    )
    return max(0, min(MAX_SCORE, round(value)))


@dataclass(frozen=True, slots=True)
class MemoryLoadoutCardStat:
    card_id: str
    trajectory_support: int
    copy_count: int
    mean_copy_count: float
    upgraded_copy_count: int
    mean_terminal_score: float
    score_signal: float
    utility: int
    role: str | None = None
    phase_counts: Mapping[str, int] = field(default_factory=dict)
    ability_support: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _text(self.card_id, "memory card stat card_id")
        _integer(self.trajectory_support, "memory card stat trajectory_support", minimum=1)
        _integer(self.copy_count, "memory card stat copy_count", minimum=1)
        if self.trajectory_support > self.copy_count:
            raise ValueError("memory card stat support exceeds copy_count")
        _number(self.mean_copy_count, "memory card stat mean_copy_count", minimum=0.0)
        _integer(self.upgraded_copy_count, "memory card stat upgraded_copy_count")
        if self.upgraded_copy_count > self.copy_count:
            raise ValueError("memory card stat upgraded copies exceed copy_count")
        _number(self.mean_terminal_score, "memory card stat mean_terminal_score", minimum=0.0)
        _number(self.score_signal, "memory card stat score_signal")
        if self.score_signal > 1.0:
            raise ValueError("memory card stat score_signal must be <= 1")
        if not 0 <= self.utility <= MAX_SCORE:
            raise ValueError("memory card stat utility is out of range")

    @property
    def support(self) -> int:
        return self.trajectory_support

    @property
    def bonus(self) -> int:
        """Compatibility name matching :class:`LeaderboardCardPriorStat`."""

        return self.utility

    @property
    def score(self) -> int:
        return self.utility

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "trajectory_support": self.trajectory_support,
            "copy_count": self.copy_count,
            "mean_copy_count": self.mean_copy_count,
            "upgraded_copy_count": self.upgraded_copy_count,
            "mean_terminal_score": self.mean_terminal_score,
            "score_signal": self.score_signal,
            "utility": self.utility,
            "role": self.role,
            "phase_counts": dict(self.phase_counts),
            "ability_support": dict(self.ability_support),
        }


@dataclass(frozen=True, slots=True)
class MemoryLoadoutCombinationStat:
    key: CombinationKey
    trajectory_support: int
    mean_terminal_score: float
    score_signal: float
    utility: int

    def __post_init__(self) -> None:
        if len(self.key) != MEMORY_COUNT or any(not isinstance(value, str) or not value for value in self.key):
            raise ValueError("memory combination key must contain four card IDs")
        _integer(self.trajectory_support, "memory combination trajectory_support", minimum=1)
        _number(self.mean_terminal_score, "memory combination mean_terminal_score", minimum=0.0)
        _number(self.score_signal, "memory combination score_signal")
        if self.score_signal > 1.0:
            raise ValueError("memory combination score_signal must be <= 1")
        if not 0 <= self.utility <= MAX_SCORE:
            raise ValueError("memory combination utility is out of range")

    @property
    def card_ids(self) -> CombinationKey:
        return self.key

    @property
    def support(self) -> int:
        return self.trajectory_support

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_ids": list(self.key),
            "trajectory_support": self.trajectory_support,
            "mean_terminal_score": self.mean_terminal_score,
            "score_signal": self.score_signal,
            "utility": self.utility,
        }


@dataclass(frozen=True, slots=True)
class MemoryLoadoutPairStat:
    key: tuple[str, str]
    trajectory_support: int
    mean_terminal_score: float
    score_signal: float
    utility: int

    def __post_init__(self) -> None:
        if len(self.key) != 2 or any(not isinstance(value, str) or not value for value in self.key):
            raise ValueError("memory pair key must contain two card IDs")
        _integer(self.trajectory_support, "memory pair trajectory_support", minimum=1)
        _number(self.mean_terminal_score, "memory pair mean_terminal_score", minimum=0.0)
        _number(self.score_signal, "memory pair score_signal")
        if self.score_signal > 1.0:
            raise ValueError("memory pair score_signal must be <= 1")
        if not 0 <= self.utility <= MAX_SCORE:
            raise ValueError("memory pair utility is out of range")

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_ids": list(self.key),
            "trajectory_support": self.trajectory_support,
            "mean_terminal_score": self.mean_terminal_score,
            "score_signal": self.score_signal,
            "utility": self.utility,
        }


def _stat_from_accumulator(
    card_id: str,
    accumulator: Mapping[str, Any],
    *,
    trajectory_count: int,
    maximum_score: int,
    exact: bool = False,
) -> MemoryLoadoutCardStat:
    support = int(accumulator["support"])
    copies = int(accumulator["copies"])
    scores = tuple(int(value) for value in accumulator["scores"])
    upgraded = int(accumulator["upgraded"])
    signal = _weighted_score(scores, maximum_score)
    return MemoryLoadoutCardStat(
        card_id=card_id,
        trajectory_support=support,
        copy_count=copies,
        mean_copy_count=copies / trajectory_count,
        upgraded_copy_count=upgraded,
        mean_terminal_score=fmean(scores),
        score_signal=signal,
        utility=_bounded_utility(
            support,
            trajectory_count,
            signal,
            upgraded / copies,
            minimum_support=(MIN_EXACT_SUPPORT if exact else MIN_USEFUL_SUPPORT),
        ),
        role=accumulator.get("role"),
        phase_counts=dict(accumulator.get("phase_counts", {})),
        ability_support=dict(accumulator.get("ability_support", {})),
    )


def _combination_stat(
    key: CombinationKey,
    accumulator: Mapping[str, Any],
    *,
    trajectory_count: int,
    maximum_score: int,
    minimum_support: int,
) -> MemoryLoadoutCombinationStat:
    scores = tuple(int(value) for value in accumulator["scores"])
    signal = _weighted_score(scores, maximum_score)
    support = int(accumulator["support"])
    return MemoryLoadoutCombinationStat(
        key=key,
        trajectory_support=support,
        mean_terminal_score=fmean(scores),
        score_signal=signal,
        utility=_bounded_utility(
            support,
            trajectory_count,
            signal,
            minimum_support=minimum_support,
        ),
    )


def _add_card_accumulator(
    accumulator: dict[str, Any],
    candidate: MemoryCardCandidate,
    score: int,
    maximum_score: int,
) -> None:
    accumulator["copies"] += 1
    accumulator["upgraded"] += int(candidate.upgrade_count > 0)
    accumulator["scores"].append(score)
    if accumulator.get("role") is None and candidate.role is not None:
        accumulator["role"] = candidate.role
    if candidate.phase_type:
        accumulator["phase_counts"][candidate.phase_type] += 1
    for ability in candidate.abilities:
        accumulator["ability_support"][ability.ability_id] += 1


def _new_accumulator() -> dict[str, Any]:
    return {
        "support": 0,
        "copies": 0,
        "upgraded": 0,
        "scores": [],
        "role": None,
        "phase_counts": Counter(),
        "ability_support": Counter(),
    }


def _build_prior_statistics(
    observations: Sequence[MemoryLoadoutObservation],
    *,
    minimum_exact_support: int,
) -> dict[str, Any]:
    trajectory_count = len(observations)
    maximum_score = max(value.terminal_score for value in observations)
    card_acc: dict[str, dict[str, Any]] = defaultdict(_new_accumulator)
    exact_acc: dict[MemorySignature, dict[str, Any]] = defaultdict(_new_accumulator)
    combo_acc: dict[CombinationKey, dict[str, Any]] = defaultdict(
        lambda: {"support": 0, "scores": []}
    )
    exact_combo_acc: dict[ExactCombinationKey, dict[str, Any]] = defaultdict(
        lambda: {"support": 0, "scores": []}
    )
    pair_acc: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"support": 0, "scores": []}
    )
    role_totals: Counter[str] = Counter()
    deck_role_totals: Counter[str] = Counter()
    total_role_weight = 0.0
    total_deck_role_weight = 0.0
    for observation in observations:
        score = observation.terminal_score
        seen_cards: set[str] = set()
        seen_signatures: set[MemorySignature] = set()
        for candidate in observation.memories:
            card_id = candidate.card_id
            _add_card_accumulator(card_acc[card_id], candidate, score, maximum_score)
            signature = candidate.signature
            _add_card_accumulator(exact_acc[signature], candidate, score, maximum_score)
            seen_cards.add(card_id)
            seen_signatures.add(signature)
            if candidate.role is not None:
                # Role balance is intentionally unweighted.  A single
                # unusually high terminal score should not redefine the
                # composition target; card and combination utility already
                # carry the high-score weighting.
                role_totals[candidate.role] += 1.0
        for card_id in seen_cards:
            card_acc[card_id]["support"] += 1
        for signature in seen_signatures:
            exact_acc[signature]["support"] += 1
        combo_key = observation.combination_key
        combo_acc[combo_key]["support"] += 1
        combo_acc[combo_key]["scores"].append(score)
        exact_combo_key = observation.exact_combination_key
        exact_combo_acc[exact_combo_key]["support"] += 1
        exact_combo_acc[exact_combo_key]["scores"].append(score)
        # Pair evidence follows the actual four-card multiset.  In
        # particular, (card-a, card-a) is not observed merely because one
        # copy of card-a is present.
        actual_cards = sorted(candidate.card_id for candidate in observation.memories)
        for left_index, left in enumerate(actual_cards):
            for right in actual_cards[left_index + 1 :]:
                pair = tuple(sorted((left, right)))
                pair_acc[pair]["support"] += 1
                pair_acc[pair]["scores"].append(score)
        known_roles = [candidate.role for candidate in observation.memories if candidate.role]
        total_role_weight += len(known_roles) if known_roles else 0.0
        for role in known_roles:
            role_totals[role] += 0.0  # keep role present even at zero after validation
        final_roles = [
            _role_from_mapping(card, _card_id_from_mapping(card) or "")
            for card in observation.final_produce_cards
            if isinstance(card, Mapping) and _card_id_from_mapping(card)
        ]
        known_final_roles = [role for role in final_roles if role is not None]
        if known_final_roles:
            total_deck_role_weight += len(known_final_roles)
            for role in known_final_roles:
                deck_role_totals[role] += 1.0
    # The support count for card stats is trajectory-level.  ``scores`` has one
    # value per copy, so reduce it to one value per trajectory for the mean
    # terminal score and score signal without changing copy count.
    for card_id, accumulator in card_acc.items():
        accumulator["scores"] = _trajectory_score_values(accumulator["scores"], observations, card_id)
    for signature, accumulator in exact_acc.items():
        accumulator["scores"] = _trajectory_signature_score_values(
            signature, accumulator["scores"], observations
        )
    statistics = {
        card_id: _stat_from_accumulator(
            card_id,
            accumulator,
            trajectory_count=trajectory_count,
            maximum_score=maximum_score,
        )
        for card_id, accumulator in sorted(card_acc.items())
    }
    exact_statistics = {
        signature: _stat_from_accumulator(
            signature[0],
            accumulator,
            trajectory_count=trajectory_count,
            maximum_score=maximum_score,
            exact=True,
        )
        for signature, accumulator in sorted(exact_acc.items(), key=lambda item: repr(item[0]))
        if accumulator["support"] >= minimum_exact_support
    }
    combinations = {
        key: _combination_stat(
            key,
            accumulator,
            trajectory_count=trajectory_count,
            maximum_score=maximum_score,
            minimum_support=MIN_USEFUL_SUPPORT,
        )
        for key, accumulator in sorted(combo_acc.items())
    }
    exact_combinations = {
        signature_key: _combination_stat(
            tuple(signature[0] for signature in signature_key),
            accumulator,
            trajectory_count=trajectory_count,
            maximum_score=maximum_score,
            minimum_support=minimum_exact_support,
        )
        for signature_key, accumulator in sorted(exact_combo_acc.items(), key=lambda item: repr(item[0]))
        if accumulator["support"] >= minimum_exact_support
    }
    pairs: dict[tuple[str, str], MemoryLoadoutPairStat] = {}
    for key, accumulator in sorted(pair_acc.items()):
        scores = tuple(int(value) for value in accumulator["scores"])
        signal = _weighted_score(scores, maximum_score)
        pairs[key] = MemoryLoadoutPairStat(
            key=key,
            trajectory_support=int(accumulator["support"]),
            mean_terminal_score=fmean(scores),
            score_signal=signal,
            utility=_bounded_utility(
                int(accumulator["support"]),
                trajectory_count,
                signal,
                minimum_support=MIN_USEFUL_SUPPORT,
            ),
        )
    role_target = {
        role: value / trajectory_count
        for role, value in sorted(role_totals.items())
        if total_role_weight > 0 and trajectory_count > 0 and value > 0
    }
    deck_role_target = {
        role: value / total_deck_role_weight
        for role, value in sorted(deck_role_totals.items())
        if total_deck_role_weight > 0 and value > 0
    }
    return {
        "statistics": statistics,
        "exact_statistics": exact_statistics,
        "combinations": combinations,
        "exact_combinations": exact_combinations,
        "pairs": pairs,
        "role_target_counts": role_target,
        "deck_role_target_counts": deck_role_target,
        "maximum_score": maximum_score,
    }


def _card_id_from_mapping(value: Mapping[str, Any]) -> str | None:
    nested = value.get("produce_card", value.get("card"))
    card = nested if isinstance(nested, Mapping) else value
    card_id = card.get("id", card.get("card_id"))
    return card_id if isinstance(card_id, str) and card_id else None


def _trajectory_score_values(
    copy_scores: Sequence[int],
    observations: Sequence[MemoryLoadoutObservation],
    card_id: str,
) -> list[int]:
    # Re-read observations to retain one score per trajectory.  This is small
    # (four memories per row) and makes the statistic semantics explicit.
    return [
        observation.terminal_score
        for observation in observations
        if any(candidate.card_id == card_id for candidate in observation.memories)
    ]


def _trajectory_signature_score_values(
    signature: MemorySignature,
    copy_scores: Sequence[int],
    observations: Sequence[MemoryLoadoutObservation],
) -> list[int]:
    return [
        observation.terminal_score
        for observation in observations
        if any(candidate.signature == signature for candidate in observation.memories)
    ]


def _source_metadata(source: object) -> tuple[Path | None, str | None]:
    if isinstance(source, Sequence) and not isinstance(source, (str, bytes, bytearray)):
        digest = hashlib.sha256()
        if not source:
            return None, None
        for value in source:
            if isinstance(value, LeaderboardRawHistoryCollection):
                for record in value.records:
                    digest.update(record.source_sha256.encode("ascii"))
                for dropped in value.dropped:
                    digest.update(dropped.source_sha256.encode("ascii"))
                continue
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                _path, nested_sha = _source_metadata(value)
                if nested_sha is None:
                    return None, None
                digest.update(nested_sha.encode("ascii"))
                continue
            if not isinstance(value, (str, Path)):
                return None, None
            path = Path(value).resolve()
            if path.is_dir():
                try:
                    members = list_leaderboard_raw_sources(path)
                except (FileNotFoundError, OSError, TypeError, ValueError):
                    return None, None
                for member in members:
                    digest.update(member.name.encode("utf-8"))
                    digest.update(member.path.read_bytes())
            elif path.is_file():
                digest.update(path.name.encode("utf-8"))
                digest.update(path.read_bytes())
            else:
                return None, None
        return None, digest.hexdigest()
    if not isinstance(source, (str, Path)):
        return None, None
    path = Path(source).resolve()
    if path.is_dir():
        try:
            members = list_leaderboard_raw_sources(path)
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return path, None
        digest = hashlib.sha256()
        for member in members:
            digest.update(member.name.encode("utf-8"))
            digest.update(member.path.read_bytes())
        return path, digest.hexdigest()
    if not path.is_file():
        return path, None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return path, digest.hexdigest()


def _scope_filter(
    observations: Sequence[MemoryLoadoutObservation],
    flow: FlowKey,
    idol_card_id: str,
    *,
    minimum_exact_trajectories: int,
) -> tuple[tuple[MemoryLoadoutObservation, ...], str]:
    same_flow = tuple(value for value in observations if value.flow == flow)
    if not same_flow:
        raise ValueError("memory loadout prior has no matching produce/plan/effect scope")
    if idol_card_id:
        exact = tuple(value for value in same_flow if value.idol_card_id == idol_card_id)
        if len(exact) >= minimum_exact_trajectories:
            return exact, "exact"
    if len(same_flow) < 1:
        raise ValueError("memory loadout prior has no evidence")
    return same_flow, "broad"


@dataclass(frozen=True, slots=True)
class MemoryLoadoutPrior:
    """Score-weighted memory-card and four-card-combination evidence."""

    source_path: Path | None
    source_sha256: str | None
    trajectory_count: int
    flow: FlowKey
    requested_idol_card_id: str
    scope_kind: str
    statistics: Mapping[str, MemoryLoadoutCardStat]
    exact_statistics: Mapping[MemorySignature, MemoryLoadoutCardStat]
    combinations: Mapping[CombinationKey, MemoryLoadoutCombinationStat]
    exact_combinations: Mapping[ExactCombinationKey, MemoryLoadoutCombinationStat]
    pair_statistics: Mapping[tuple[str, str], MemoryLoadoutPairStat]
    role_target_counts: Mapping[str, float]
    deck_role_target_counts: Mapping[str, float]
    maximum_score: int
    schema: str = SCHEMA
    minimum_exact_support: int = MIN_EXACT_SUPPORT
    observation_report: MemoryLoadoutObservationReport | None = None

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise ValueError("unsupported memory loadout prior schema")
        _integer(self.trajectory_count, "memory prior trajectory_count")
        if len(self.flow) != 3 or any(not isinstance(value, str) or not value for value in self.flow):
            raise ValueError("memory prior flow is invalid")
        if self.scope_kind not in {"exact", "broad"}:
            raise ValueError("memory prior scope_kind must be exact or broad")
        _integer(self.maximum_score, "memory prior maximum_score", minimum=1)
        _integer(self.minimum_exact_support, "memory prior minimum_exact_support", minimum=1)
        if self.scope_kind == "exact" and not self.requested_idol_card_id:
            raise ValueError("exact memory prior requires requested idol card")

    @property
    def produce_id(self) -> str:
        return self.flow[0]

    @property
    def plan_type(self) -> str:
        return self.flow[1]

    @property
    def exam_effect_type(self) -> str:
        return self.flow[2]

    @property
    def idol_card_id(self) -> str:
        return self.requested_idol_card_id

    @property
    def evidence_scope(self) -> str:
        return self.scope_kind

    @property
    def scope(self) -> tuple[str, str, str, str | None]:
        return (
            self.produce_id,
            self.plan_type,
            self.exam_effect_type,
            self.requested_idol_card_id if self.scope_kind == "exact" else None,
        )

    @property
    def source(self) -> Path | None:
        return self.source_path

    @classmethod
    def from_observations(
        cls,
        observations: Sequence[MemoryLoadoutObservation],
        *,
        flow: FlowKey | None = None,
        requested_idol_card_id: str = "",
        scope_kind: str = "broad",
        source_path: Path | None = None,
        source_sha256: str | None = None,
        minimum_exact_support: int = MIN_EXACT_SUPPORT,
        observation_report: MemoryLoadoutObservationReport | None = None,
    ) -> "MemoryLoadoutPrior":
        values = tuple(observations)
        if not all(isinstance(value, MemoryLoadoutObservation) for value in values):
            raise TypeError("memory prior observations must contain MemoryLoadoutObservation values")
        if not values:
            if flow is None:
                raise ValueError("empty memory prior requires an explicit flow")
            chosen_flow = _flow(*flow, label="prior.flow")
            if scope_kind not in {"exact", "broad"}:
                raise ValueError("memory prior scope_kind must be exact or broad")
            if scope_kind == "exact" and not requested_idol_card_id:
                raise ValueError("exact memory prior requires requested idol card")
            minimum_exact = _integer(
                minimum_exact_support,
                "minimum_exact_support",
                minimum=1,
            )
            return cls(
                source_path=None if source_path is None else Path(source_path),
                source_sha256=source_sha256,
                trajectory_count=0,
                flow=chosen_flow,
                requested_idol_card_id=requested_idol_card_id,
                scope_kind=scope_kind,
                statistics={},
                exact_statistics={},
                combinations={},
                exact_combinations={},
                pair_statistics={},
                role_target_counts={},
                deck_role_target_counts={},
                maximum_score=1,
                minimum_exact_support=minimum_exact,
                observation_report=observation_report,
            )
        chosen_flow = values[0].flow if flow is None else _flow(*flow, label="prior.flow")
        if any(value.flow != chosen_flow for value in values):
            raise ValueError("memory prior observations cross flow scope")
        if scope_kind not in {"exact", "broad"}:
            raise ValueError("memory prior scope_kind must be exact or broad")
        if scope_kind == "exact" and not requested_idol_card_id:
            raise ValueError("exact memory prior requires requested idol card")
        if scope_kind == "exact" and any(
            value.idol_card_id != requested_idol_card_id for value in values
        ):
            raise ValueError("exact memory prior observations cross idol scope")
        minimum_exact = _integer(minimum_exact_support, "minimum_exact_support", minimum=1)
        built = _build_prior_statistics(values, minimum_exact_support=minimum_exact)
        return cls(
            source_path=None if source_path is None else Path(source_path),
            source_sha256=source_sha256,
            trajectory_count=len(values),
            flow=chosen_flow,
            requested_idol_card_id=requested_idol_card_id,
            scope_kind=scope_kind,
            statistics=built["statistics"],
            exact_statistics=built["exact_statistics"],
            combinations=built["combinations"],
            exact_combinations=built["exact_combinations"],
            pair_statistics=built["pairs"],
            role_target_counts=built["role_target_counts"],
            deck_role_target_counts=built["deck_role_target_counts"],
            maximum_score=built["maximum_score"],
            minimum_exact_support=minimum_exact,
            observation_report=observation_report,
        )

    def applies_to(
        self,
        *,
        produce_id: str,
        plan_type: str,
        exam_effect_type: str,
        idol_card_id: str = "",
    ) -> bool:
        if (produce_id, plan_type, exam_effect_type) != self.flow:
            return False
        return self.scope_kind != "exact" or idol_card_id == self.requested_idol_card_id

    def _card_stat(self, candidate: MemoryCardCandidate) -> tuple[MemoryLoadoutCardStat | None, str | None]:
        exact = self.exact_statistics.get(candidate.signature)
        if exact is not None:
            return exact, "exact-card"
        stat = self.statistics.get(candidate.card_id)
        return (stat, "card") if stat is not None else (None, None)

    def statistic_for(
        self,
        candidate: MemoryCardCandidate | Mapping[str, Any],
    ) -> tuple[MemoryLoadoutCardStat | None, str | None]:
        card = _coerce_candidate(candidate)
        return self._card_stat(card)

    card_stat_for = statistic_for

    def evidence_scope_for(
        self,
        candidate: MemoryCardCandidate | Mapping[str, Any],
    ) -> str | None:
        return self.statistic_for(candidate)[1]

    scope_for = evidence_scope_for

    def score_for(
        self,
        candidate: MemoryCardCandidate | Mapping[str, Any] | str,
        *,
        upgrade: int | None = None,
    ) -> int:
        if isinstance(candidate, str):
            card = MemoryCardCandidate(candidate, 0 if upgrade is None else _integer(upgrade, "upgrade"))
        else:
            card = _coerce_candidate(candidate)
            if upgrade is not None:
                card = MemoryCardCandidate(
                    card_id=card.card_id,
                    upgrade_count=_integer(upgrade, "upgrade"),
                    phase_type=card.phase_type,
                    abilities=card.abilities,
                    memory_slot=card.memory_slot,
                    is_rental=card.is_rental,
                    customizes=card.customizes,
                    role=card.role,
                    observed_fields=card.observed_fields,
                    memory_id=card.memory_id,
                )
        stat, _scope = self._card_stat(card)
        if stat is None:
            return 0
        value = stat.utility
        if card.upgrade_count > 0 and stat.upgraded_copy_count > 0:
            value += min(40, round(40 * stat.upgraded_copy_count / stat.copy_count))
        if card.phase_type and stat.phase_counts:
            phase_total = sum(stat.phase_counts.values())
            value += min(40, round(40 * stat.phase_counts.get(card.phase_type, 0) / phase_total))
        ability_hits = sum(stat.ability_support.get(ability.ability_id, 0) for ability in card.abilities)
        if card.abilities and ability_hits:
            value += min(40, 10 * ability_hits)
        return min(MAX_SCORE, max(0, value))

    composition_score_for = score_for

    def _role_balance(
        self,
        candidates: Sequence[MemoryCardCandidate],
        deck_cards: Sequence[object],
    ) -> tuple[int, str]:
        candidate_roles = [value.role for value in candidates if value.role is not None]
        if deck_cards:
            parsed_deck: list[MemoryCardCandidate] = []
            for value in deck_cards:
                if isinstance(value, MemoryCardCandidate):
                    parsed_deck.append(value)
                elif isinstance(value, Mapping):
                    card_id = _card_id_from_mapping(value)
                    if card_id is None:
                        continue
                    parsed_deck.append(MemoryCardCandidate.from_mapping(value))
                elif isinstance(value, str) and value:
                    parsed_deck.append(MemoryCardCandidate(value))
            deck_roles = [value.role for value in parsed_deck if value.role is not None]
            if self.deck_role_target_counts and deck_roles:
                counts = Counter(deck_roles + candidate_roles)
                total = sum(counts.values())
                target_distribution = dict(self.deck_role_target_counts)
                target = {
                    role: value * total
                    for role, value in target_distribution.items()
                }
            else:
                counts = Counter(candidate_roles)
                target = dict(self.role_target_counts)
                total = len(candidate_roles)
        else:
            counts = Counter(candidate_roles)
            target = dict(self.role_target_counts)
            total = len(candidate_roles)
        if not counts:
            return MAX_SCORE // 2, "role-balance:unknown-neutral"
        if not target:
            target = {role: total / len(counts) for role in counts}
        keys = set(counts) | set(target)
        l1 = sum(abs(float(counts.get(role, 0)) - float(target.get(role, 0))) for role in keys)
        denominator = max(1.0, 2.0 * max(float(total), sum(float(value) for value in target.values())))
        score = max(0, min(MAX_SCORE, round(MAX_SCORE * (1.0 - l1 / denominator))))
        target_text = ",".join(f"{key}={target[key]:g}" for key in sorted(target))
        actual_text = ",".join(f"{key}={counts[key]}" for key in sorted(counts))
        return score, f"role-balance:actual={actual_text};target={target_text};score={score}"

    def combination_score_for(
        self,
        candidates: Iterable[object],
        *,
        deck_cards: Iterable[object] = (),
    ) -> "MemoryLoadoutCombinationScore":
        values = _coerce_combination(candidates)
        deck_values = tuple(deck_cards)
        card_scores: list[int] = []
        card_scopes: list[str] = []
        unknown: list[str] = []
        reasons: list[str] = []
        for candidate in values:
            score = self.score_for(candidate)
            card_scores.append(score)
            scope = self.evidence_scope_for(candidate)
            if scope is None:
                unknown.append(candidate.card_id)
                reasons.append(f"card:{candidate.card_id}=unknown")
            else:
                card_scopes.append(scope)
                reasons.append(f"card:{candidate.card_id}={score};evidence={scope}")
        key = tuple(sorted(value.card_id for value in values))
        exact_key = tuple(sorted(value.signature for value in values))
        combo = self.exact_combinations.get(exact_key)
        combo_scope = "exact-combination" if combo is not None else None
        if combo is None:
            combo = self.combinations.get(key)
            combo_scope = "combination" if combo is not None else None
        if combo is not None:
            combination_score = combo.utility
            reasons.append(
                f"combination:{','.join(key)}={combination_score};support={combo.trajectory_support};evidence={combo_scope}"
            )
        else:
            pair_values: list[int] = []
            for left_index, left in enumerate(key):
                for right in key[left_index + 1 :]:
                    pair = tuple(sorted((left, right)))
                    if pair in self.pair_statistics:
                        pair_values.append(self.pair_statistics[pair].utility)
            combination_score = round(fmean(pair_values)) if pair_values else 0
            if pair_values:
                reasons.append(f"pair-evidence:mean={combination_score};pairs={len(pair_values)}")
            else:
                reasons.append("combination:no-observed-combination")
        role_score, role_reason = self._role_balance(values, deck_values)
        reasons.append(role_reason)
        card_score = round(fmean(card_scores)) if card_scores else 0
        has_signal = bool(
            any(value > 0 for value in card_scores)
            or combination_score > 0
        )
        abstained = not has_signal
        total = (
            0
            if abstained
            else min(
                MAX_SCORE,
                max(
                    0,
                    round(
                        0.60 * card_score
                        + 0.25 * combination_score
                        + 0.15 * role_score
                    ),
                ),
            )
        )
        if unknown:
            reasons.append("unknown-cards=" + ",".join(sorted(set(unknown))))
        if abstained:
            reasons.append("abstain:no-card-or-combination-evidence")
        return MemoryLoadoutCombinationScore(
            candidates=values,
            total_score=total,
            card_score=card_score,
            combination_score=combination_score,
            role_balance_score=role_score,
            known_cards=tuple(sorted(set(value.card_id for value in values if value.card_id not in unknown))),
            unknown_cards=tuple(sorted(set(unknown))),
            evidence_scope=(
                "exact" if combo_scope == "exact-combination" or "exact-card" in card_scopes
                else (self.scope_kind if card_scopes or combo is not None else "none")
            ),
            reasons=tuple(reasons),
            abstained=abstained,
        )

    score_combination = combination_score_for
    score_loadout = combination_score_for

    @property
    def card_stats(self) -> Mapping[str, MemoryLoadoutCardStat]:
        return self.statistics

    @property
    def combination_stats(self) -> Mapping[CombinationKey, MemoryLoadoutCombinationStat]:
        return self.combinations

    def rank_combinations(
        self,
        combinations: Iterable[Iterable[object]],
        *,
        deck_cards: Iterable[object] = (),
    ) -> tuple["MemoryLoadoutCombinationScore", ...]:
        evaluations = tuple(
            self.combination_score_for(value, deck_cards=deck_cards)
            for value in combinations
        )
        return tuple(
            sorted(
                evaluations,
                key=lambda value: (-value.total_score, tuple(card.card_id for card in value.candidates)),
            )
        )

    rank = rank_combinations

    def summary(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "scope_kind": self.scope_kind,
            "source_path": None if self.source_path is None else str(self.source_path),
            "source_sha256": self.source_sha256,
            "trajectory_count": self.trajectory_count,
            "flow": list(self.flow),
            "produce_id": self.produce_id,
            "plan_type": self.plan_type,
            "exam_effect_type": self.exam_effect_type,
            "requested_idol_card_id": self.requested_idol_card_id,
            "maximum_score": self.maximum_score,
            "card_count": len(self.statistics),
            "exact_card_count": len(self.exact_statistics),
            "combination_count": len(self.combinations),
            "exact_combination_count": len(self.exact_combinations),
            "pair_count": len(self.pair_statistics),
            "role_target_counts": dict(self.role_target_counts),
            "deck_role_target_counts": dict(self.deck_role_target_counts),
            "minimum_exact_support": self.minimum_exact_support,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "statistics": {key: value.to_dict() for key, value in self.statistics.items()},
            "exact_statistics": {
                repr(key): value.to_dict() for key, value in self.exact_statistics.items()
            },
            "combinations": {"|".join(key): value.to_dict() for key, value in self.combinations.items()},
            "exact_combinations": {
                repr(key): value.to_dict() for key, value in self.exact_combinations.items()
            },
            "pair_statistics": {
                "|".join(key): value.to_dict()
                for key, value in self.pair_statistics.items()
            },
            "observation_report": (
                None if self.observation_report is None else self.observation_report.to_dict()
            ),
        }


@dataclass(frozen=True, slots=True)
class HierarchicalMemoryLoadoutPrior:
    """Exact-idol memory evidence with a same-flow broad fallback.

    The wrapper mirrors the existing leaderboard card-prior contract.  Exact
    statistics take precedence per candidate/combination, while a card absent
    from the exact group can still use broad evidence from the same
    ``produce_id + plan_type + exam_effect_type`` flow.
    """

    requested_produce_id: str
    requested_plan_type: str
    requested_exam_effect_type: str
    requested_idol_card_id: str
    exact: MemoryLoadoutPrior | None
    broad: MemoryLoadoutPrior | None
    source_path: Path | None = None
    source_sha256: str | None = None
    schema: str = HIERARCHICAL_SCHEMA

    def __post_init__(self) -> None:
        _flow(
            self.requested_produce_id,
            self.requested_plan_type,
            self.requested_exam_effect_type,
            label="hierarchical requested_flow",
        )
        if self.schema != HIERARCHICAL_SCHEMA:
            raise ValueError("unsupported hierarchical memory prior schema")
        if self.exact is None and self.broad is None:
            raise ValueError("hierarchical memory prior has no evidence")
        for name, prior in (("exact", self.exact), ("broad", self.broad)):
            if prior is None:
                continue
            if prior.flow != self.requested_flow:
                raise ValueError(f"hierarchical {name} prior crosses flow scope")
        if self.exact is not None and (
            not self.requested_idol_card_id
            or self.exact.scope_kind != "exact"
            or self.exact.requested_idol_card_id != self.requested_idol_card_id
        ):
            raise ValueError("hierarchical exact prior crosses idol scope")

    @property
    def requested_flow(self) -> FlowKey:
        return (
            self.requested_produce_id,
            self.requested_plan_type,
            self.requested_exam_effect_type,
        )

    @property
    def flow(self) -> FlowKey:
        return self.requested_flow

    @property
    def produce_id(self) -> str:
        return self.requested_produce_id

    @property
    def plan_type(self) -> str:
        return self.requested_plan_type

    @property
    def exam_effect_type(self) -> str:
        return self.requested_exam_effect_type

    @property
    def idol_card_id(self) -> str:
        return self.requested_idol_card_id

    @property
    def scope_kind(self) -> str:
        return "hierarchical"

    @property
    def evidence_scope(self) -> str:
        return "exact" if self.exact is not None else "broad"

    @property
    def selected(self) -> MemoryLoadoutPrior:
        selected = self.exact or self.broad
        assert selected is not None
        return selected

    @property
    def source(self) -> Path | None:
        return self.source_path or self.selected.source_path

    @property
    def trajectory_count(self) -> int:
        return self.selected.trajectory_count

    @property
    def maximum_score(self) -> int:
        return self.selected.maximum_score

    @property
    def statistics(self) -> Mapping[str, MemoryLoadoutCardStat]:
        merged: dict[str, MemoryLoadoutCardStat] = {}
        if self.broad is not None:
            merged.update(self.broad.statistics)
        if self.exact is not None:
            merged.update(self.exact.statistics)
        return merged

    @property
    def exact_statistics(self) -> Mapping[MemorySignature, MemoryLoadoutCardStat]:
        merged: dict[MemorySignature, MemoryLoadoutCardStat] = {}
        if self.broad is not None:
            merged.update(self.broad.exact_statistics)
        if self.exact is not None:
            merged.update(self.exact.exact_statistics)
        return merged

    @property
    def combinations(self) -> Mapping[CombinationKey, MemoryLoadoutCombinationStat]:
        merged: dict[CombinationKey, MemoryLoadoutCombinationStat] = {}
        if self.broad is not None:
            merged.update(self.broad.combinations)
        if self.exact is not None:
            merged.update(self.exact.combinations)
        return merged

    @property
    def exact_combinations(self) -> Mapping[ExactCombinationKey, MemoryLoadoutCombinationStat]:
        merged: dict[ExactCombinationKey, MemoryLoadoutCombinationStat] = {}
        if self.broad is not None:
            merged.update(self.broad.exact_combinations)
        if self.exact is not None:
            merged.update(self.exact.exact_combinations)
        return merged

    @property
    def pair_statistics(self) -> Mapping[tuple[str, str], MemoryLoadoutPairStat]:
        merged: dict[tuple[str, str], MemoryLoadoutPairStat] = {}
        if self.broad is not None:
            merged.update(self.broad.pair_statistics)
        if self.exact is not None:
            merged.update(self.exact.pair_statistics)
        return merged

    @property
    def role_target_counts(self) -> Mapping[str, float]:
        return self.selected.role_target_counts

    @property
    def deck_role_target_counts(self) -> Mapping[str, float]:
        return self.selected.deck_role_target_counts

    def applies_to(
        self,
        *,
        produce_id: str,
        plan_type: str,
        exam_effect_type: str,
        idol_card_id: str = "",
    ) -> bool:
        return self.requested_flow == (produce_id, plan_type, exam_effect_type) and (
            not self.requested_idol_card_id
            or idol_card_id == self.requested_idol_card_id
        )

    def statistic_for(
        self,
        candidate: MemoryCardCandidate | Mapping[str, Any],
    ) -> tuple[MemoryLoadoutCardStat | None, str | None]:
        card = _coerce_candidate(candidate)
        if self.exact is not None:
            value = self.exact.exact_statistics.get(card.signature)
            if value is not None:
                return value, "exact-card"
            value = self.exact.statistics.get(card.card_id)
            if value is not None:
                return value, "exact"
        if self.broad is not None:
            value = self.broad.exact_statistics.get(card.signature)
            if value is not None:
                return value, "broad-exact-card"
            value = self.broad.statistics.get(card.card_id)
            if value is not None:
                return value, "broad"
        return None, None

    card_stat_for = statistic_for

    def evidence_scope_for(
        self,
        candidate: MemoryCardCandidate | Mapping[str, Any],
    ) -> str | None:
        return self.statistic_for(candidate)[1]

    scope_for = evidence_scope_for

    def score_for(
        self,
        candidate: MemoryCardCandidate | Mapping[str, Any] | str,
        *,
        upgrade: int | None = None,
    ) -> int:
        card = (
            MemoryCardCandidate(
                candidate,
                0 if upgrade is None else _integer(upgrade, "upgrade"),
            )
            if isinstance(candidate, str)
            else _coerce_candidate(candidate)
        )
        if upgrade is not None and not isinstance(candidate, str):
            card = replace(card, upgrade_count=_integer(upgrade, "upgrade"))
        stat, _scope = self.statistic_for(card)
        if stat is None:
            return 0
        # Reuse the selected prior's small adjustment policy while selecting
        # evidence per card.  The merged maps make exact stats win over broad
        # stats and retain broad fallbacks for missing exact cards.
        selected = self.selected
        merged = replace(
            selected,
            statistics=self.statistics,
            exact_statistics=self.exact_statistics,
            combinations=self.combinations,
            exact_combinations=self.exact_combinations,
            pair_statistics=self.pair_statistics,
        )
        return merged.score_for(card)

    composition_score_for = score_for

    def combination_score_for(
        self,
        candidates: Iterable[object],
        *,
        deck_cards: Iterable[object] = (),
    ) -> "MemoryLoadoutCombinationScore":
        selected = self.selected
        merged = replace(
            selected,
            statistics=self.statistics,
            exact_statistics=self.exact_statistics,
            combinations=self.combinations,
            exact_combinations=self.exact_combinations,
            pair_statistics=self.pair_statistics,
        )
        return merged.combination_score_for(candidates, deck_cards=deck_cards)

    score_combination = combination_score_for
    score_loadout = combination_score_for

    def rank_combinations(
        self,
        combinations: Iterable[Iterable[object]],
        *,
        deck_cards: Iterable[object] = (),
    ) -> tuple["MemoryLoadoutCombinationScore", ...]:
        selected = self.selected
        merged = replace(
            selected,
            statistics=self.statistics,
            exact_statistics=self.exact_statistics,
            combinations=self.combinations,
            exact_combinations=self.exact_combinations,
            pair_statistics=self.pair_statistics,
        )
        return merged.rank_combinations(combinations, deck_cards=deck_cards)

    rank = rank_combinations

    @property
    def scope(self) -> tuple[str, str, str, str | None]:
        return (
            self.requested_produce_id,
            self.requested_plan_type,
            self.requested_exam_effect_type,
            self.requested_idol_card_id or None,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "scope_kind": "hierarchical",
            "selected_scope": {
                "produce_id": self.requested_produce_id,
                "plan_type": self.requested_plan_type,
                "exam_effect_type": self.requested_exam_effect_type,
                "idol_card_id": self.requested_idol_card_id,
            },
            "exact": None if self.exact is None else self.exact.summary(),
            "broad": None if self.broad is None else self.broad.summary(),
            "card_count": len(self.statistics),
            "combination_count": len(self.combinations),
        }


HierarchicalLeaderboardMemoryLoadoutPrior = HierarchicalMemoryLoadoutPrior


@dataclass(frozen=True, slots=True)
class MemoryLoadoutCombinationScore:
    """A deterministic, explainable score for one four-memory candidate."""

    candidates: tuple[MemoryCardCandidate, ...]
    total_score: int
    card_score: int
    combination_score: int
    role_balance_score: int
    known_cards: tuple[str, ...]
    unknown_cards: tuple[str, ...]
    evidence_scope: str
    reasons: tuple[str, ...]
    abstained: bool

    def __post_init__(self) -> None:
        if len(self.candidates) != MEMORY_COUNT:
            raise ValueError("memory combination score must contain four candidates")
        for name, value in (
            ("total_score", self.total_score),
            ("card_score", self.card_score),
            ("combination_score", self.combination_score),
            ("role_balance_score", self.role_balance_score),
        ):
            if not 0 <= value <= MAX_SCORE:
                raise ValueError(f"{name} is outside 0..{MAX_SCORE}")
        if self.evidence_scope not in {"exact", "broad", "none"}:
            raise ValueError("unsupported memory combination evidence_scope")
        if not isinstance(self.abstained, bool):
            raise TypeError("memory combination abstained must be boolean")

    @property
    def score(self) -> int:
        return self.total_score

    @property
    def card_ids(self) -> tuple[str, ...]:
        return tuple(value.card_id for value in self.candidates)

    @property
    def memory_ids(self) -> tuple[str | None, ...]:
        """Return candidate instance IDs without collapsing equal card IDs."""

        return tuple(value.memory_id for value in self.candidates)

    @property
    def top1(self) -> tuple[MemoryCardCandidate, ...]:
        return self.candidates

    def to_dict(self) -> dict[str, Any]:
        return {
            "cards": [value.to_dict() for value in self.candidates],
            "card_ids": [value.card_id for value in self.candidates],
            "memory_ids": [value.memory_id for value in self.candidates],
            "total_score": self.total_score,
            "card_score": self.card_score,
            "combination_score": self.combination_score,
            "role_balance_score": self.role_balance_score,
            "known_cards": list(self.known_cards),
            "unknown_cards": list(self.unknown_cards),
            "evidence_scope": self.evidence_scope,
            "reasons": list(self.reasons),
            "abstained": self.abstained,
        }


MemoryLoadoutScore = MemoryLoadoutCombinationScore


@dataclass(frozen=True, slots=True)
class MemoryLoadoutRanking:
    evaluations: tuple[MemoryLoadoutCombinationScore, ...]

    @property
    def ranked(self) -> tuple[MemoryLoadoutCombinationScore, ...]:
        return self.evaluations

    @property
    def top(self) -> MemoryLoadoutCombinationScore | None:
        return self.evaluations[0] if self.evaluations else None

    def to_dict(self) -> dict[str, Any]:
        return {"ranked": [value.to_dict() for value in self.evaluations]}


def load_memory_loadout_prior(
    source: str | Path = DEFAULT_LEADERBOARD_EPISODES,
    *,
    produce_id: str,
    plan_type: str,
    exam_effect_type: str,
    idol_card_id: str = "",
    minimum_exact_trajectories: int = MIN_EXACT_SUPPORT,
    require_complete: bool = True,
    history_sources: str | Path | Sequence[str | Path] | LeaderboardRawHistoryCollection | None = None,
    database: str | Path = DEFAULT_DATABASE,
) -> MemoryLoadoutPrior:
    """Load exact-idol evidence when available, else same-flow broad evidence.

    Pass ``history_sources`` to use raw `Produce.History` responses whose
    auditions have summary scores but no replay actions.  That branch is
    intentionally scoped to memory/deck composition and never produces a
    replay episode.
    """

    minimum_exact = _integer(
        minimum_exact_trajectories,
        "minimum_exact_trajectories",
        minimum=1,
    )
    requested_flow = _flow(produce_id, plan_type, exam_effect_type)
    if not isinstance(require_complete, bool):
        raise TypeError("require_complete must be boolean")
    if history_sources is not None:
        observations, report = build_memory_loadout_observations_from_sources(
            source,
            history_sources=history_sources,
            require_complete=require_complete,
            history_database=database,
            history_plan_type=plan_type,
            history_exam_effect_type=exam_effect_type,
        )
        source_for_metadata: object = (source, history_sources)
    else:
        observations, report = build_memory_loadout_observations(
            source,
            require_complete=require_complete,
        )
        source_for_metadata = source
    selected, scope_kind = _scope_filter(
        observations,
        requested_flow,
        idol_card_id,
        minimum_exact_trajectories=minimum_exact,
    )
    source_path, source_sha256 = _source_metadata(source_for_metadata)
    return MemoryLoadoutPrior.from_observations(
        selected,
        flow=requested_flow,
        requested_idol_card_id=(idol_card_id if scope_kind == "exact" else ""),
        scope_kind=scope_kind,
        source_path=source_path,
        source_sha256=source_sha256,
        minimum_exact_support=minimum_exact,
        observation_report=report,
    )


load_leaderboard_memory_loadout_prior = load_memory_loadout_prior
build_memory_loadout_prior = load_memory_loadout_prior


def write_default_memory_loadout_prior_artifact(
    source: str | Path = DEFAULT_LEADERBOARD_EPISODES,
    output: str | Path = DEFAULT_PRIOR_ARTIFACT,
    *,
    minimum_exact_trajectories: int = MIN_EXACT_SUPPORT,
    history_sources: str | Path | Sequence[str | Path] | LeaderboardRawHistoryCollection | None = None,
    history_plan_type: str | None = None,
    history_exam_effect_type: str | None = None,
    database: str | Path = DEFAULT_DATABASE,
) -> dict[str, Any]:
    """Write a deterministic, all-scope offline prior artifact.

    The artifact is a portable report/cache of broad same-flow evidence plus
    exact-idol evidence where available.  It contains no candidate sampler,
    queue handle, runtime session, or game-control data.  Scoring callers can
    continue to load directly from the source JSONL; the artifact exists so
    the default prior build has a stable, hash-bound output for inspection and
    handoff.
    """

    minimum_exact = _integer(
        minimum_exact_trajectories,
        "minimum_exact_trajectories",
        minimum=1,
    )
    if history_sources is not None:
        observations, report = build_memory_loadout_observations_from_sources(
            source,
            history_sources=history_sources,
            history_database=database,
            history_plan_type=history_plan_type,
            history_exam_effect_type=history_exam_effect_type,
        )
        source_for_metadata: object = (source, history_sources)
    else:
        observations, report = build_memory_loadout_observations(source)
        source_for_metadata = source
    source_path, source_sha256 = _source_metadata(source_for_metadata)
    by_flow: dict[FlowKey, list[MemoryLoadoutObservation]] = defaultdict(list)
    for observation in observations:
        by_flow[observation.flow].append(observation)
    scopes: dict[str, Any] = {}
    for flow, flow_observations in sorted(by_flow.items()):
        broad = MemoryLoadoutPrior.from_observations(
            tuple(flow_observations),
            flow=flow,
            scope_kind="broad",
            source_path=source_path,
            source_sha256=source_sha256,
            minimum_exact_support=minimum_exact,
        )
        by_idol: dict[str, list[MemoryLoadoutObservation]] = defaultdict(list)
        for observation in flow_observations:
            by_idol[observation.idol_card_id].append(observation)
        exact: dict[str, Any] = {}
        for idol, idol_observations in sorted(by_idol.items()):
            if len(idol_observations) < minimum_exact:
                continue
            exact_prior = MemoryLoadoutPrior.from_observations(
                tuple(idol_observations),
                flow=flow,
                requested_idol_card_id=idol,
                scope_kind="exact",
                source_path=source_path,
                source_sha256=source_sha256,
                minimum_exact_support=minimum_exact,
            )
            exact[idol] = exact_prior.to_dict()
        scopes[flow_string(flow)] = {
            "broad": broad.to_dict(),
            "exact_by_idol": exact,
        }
    payload: dict[str, Any] = {
        "schema": ARTIFACT_SCHEMA,
        "source_path": None if source_path is None else str(source_path),
        "source_sha256": source_sha256,
        "minimum_exact_trajectories": minimum_exact,
        "observation_report": report.to_dict(),
        "scope_count": len(scopes),
        "scopes": scopes,
    }
    destination = Path(output).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return payload


def _stat_from_artifact_payload(
    value: Mapping[str, Any],
    *,
    card_id: str | None = None,
) -> MemoryLoadoutCardStat:
    selected_id = value.get("card_id", card_id)
    return MemoryLoadoutCardStat(
        card_id=_text(selected_id, "artifact card_id"),
        trajectory_support=_integer(
            value.get("trajectory_support"),
            "artifact trajectory_support",
            minimum=1,
        ),
        copy_count=_integer(value.get("copy_count"), "artifact copy_count", minimum=1),
        mean_copy_count=_number(value.get("mean_copy_count"), "artifact mean_copy_count"),
        upgraded_copy_count=_integer(
            value.get("upgraded_copy_count"),
            "artifact upgraded_copy_count",
        ),
        mean_terminal_score=_number(
            value.get("mean_terminal_score"),
            "artifact mean_terminal_score",
        ),
        score_signal=_number(value.get("score_signal"), "artifact score_signal"),
        utility=_integer(value.get("utility"), "artifact utility"),
        role=_canonical_role(value.get("role")),
        phase_counts=(
            dict(value.get("phase_counts"))
            if isinstance(value.get("phase_counts"), Mapping)
            else {}
        ),
        ability_support=(
            dict(value.get("ability_support"))
            if isinstance(value.get("ability_support"), Mapping)
            else {}
        ),
    )


def _combination_stat_from_artifact_payload(
    value: Mapping[str, Any],
    *,
    key: CombinationKey,
) -> MemoryLoadoutCombinationStat:
    return MemoryLoadoutCombinationStat(
        key=key,
        trajectory_support=_integer(
            value.get("trajectory_support"),
            "artifact combination trajectory_support",
            minimum=1,
        ),
        mean_terminal_score=_number(
            value.get("mean_terminal_score"),
            "artifact combination mean_terminal_score",
        ),
        score_signal=_number(
            value.get("score_signal"),
            "artifact combination score_signal",
        ),
        utility=_integer(value.get("utility"), "artifact combination utility"),
    )


def _load_prior_from_artifact_payload(value: Mapping[str, Any]) -> MemoryLoadoutPrior:
    raw_flow = value.get("flow")
    if not isinstance(raw_flow, Sequence) or isinstance(raw_flow, (str, bytes)):
        raise ValueError("artifact prior flow must be an array")
    flow = _flow(*raw_flow, label="artifact.flow")
    raw_statistics = value.get("statistics")
    raw_exact = value.get("exact_statistics")
    raw_combinations = value.get("combinations")
    raw_exact_combinations = value.get("exact_combinations")
    raw_pairs = value.get("pair_statistics")
    if not all(
        isinstance(raw, Mapping)
        for raw in (
            raw_statistics,
            raw_exact,
            raw_combinations,
            raw_exact_combinations,
            raw_pairs,
        )
    ):
        raise ValueError("artifact prior statistics must be objects")
    statistics = {
        _text(card_id, "artifact statistics card_id"): _stat_from_artifact_payload(
            stat,
            card_id=card_id,
        )
        for card_id, stat in raw_statistics.items()
        if isinstance(stat, Mapping)
    }
    exact_statistics: dict[MemorySignature, MemoryLoadoutCardStat] = {}
    for key, stat in raw_exact.items():
        if not isinstance(key, str) or not isinstance(stat, Mapping):
            continue
        parsed = ast.literal_eval(key)
        if not isinstance(parsed, tuple) or len(parsed) != 5:
            raise ValueError("artifact exact card signature is invalid")
        exact_statistics[parsed] = _stat_from_artifact_payload(stat)
    combinations: dict[CombinationKey, MemoryLoadoutCombinationStat] = {}
    for key, stat in raw_combinations.items():
        if not isinstance(key, str) or not isinstance(stat, Mapping):
            continue
        parsed = tuple(key.split("|"))
        if len(parsed) != MEMORY_COUNT:
            raise ValueError("artifact combination key is invalid")
        combinations[parsed] = _combination_stat_from_artifact_payload(
            stat,
            key=parsed,
        )
    exact_combinations: dict[ExactCombinationKey, MemoryLoadoutCombinationStat] = {}
    for key, stat in raw_exact_combinations.items():
        if not isinstance(key, str) or not isinstance(stat, Mapping):
            continue
        parsed = ast.literal_eval(key)
        if not isinstance(parsed, tuple) or len(parsed) != MEMORY_COUNT:
            raise ValueError("artifact exact combination key is invalid")
        exact_key = tuple(parsed)
        card_key = tuple(signature[0] for signature in exact_key)
        exact_combinations[exact_key] = _combination_stat_from_artifact_payload(
            stat,
            key=card_key,
        )
    pair_statistics: dict[tuple[str, str], MemoryLoadoutPairStat] = {}
    for key, stat in raw_pairs.items():
        if not isinstance(key, str) or not isinstance(stat, Mapping):
            continue
        parsed = tuple(key.split("|"))
        if len(parsed) != 2:
            raise ValueError("artifact pair key is invalid")
        pair_statistics[parsed] = MemoryLoadoutPairStat(
            key=parsed,
            trajectory_support=_integer(
                stat.get("trajectory_support"),
                "artifact pair trajectory_support",
                minimum=1,
            ),
            mean_terminal_score=_number(
                stat.get("mean_terminal_score"),
                "artifact pair mean_terminal_score",
            ),
            score_signal=_number(stat.get("score_signal"), "artifact pair score_signal"),
            utility=_integer(stat.get("utility"), "artifact pair utility"),
        )
    role_target = value.get("role_target_counts", {})
    deck_role_target = value.get("deck_role_target_counts", {})
    return MemoryLoadoutPrior(
        source_path=(
            None
            if value.get("source_path") is None
            else Path(_text(value.get("source_path"), "artifact source_path"))
        ),
        source_sha256=value.get("source_sha256"),
        trajectory_count=_integer(value.get("trajectory_count"), "artifact trajectory_count"),
        flow=flow,
        requested_idol_card_id=_text(
            value.get("requested_idol_card_id", ""),
            "artifact requested_idol_card_id",
            allow_empty=True,
        ),
        scope_kind=_text(value.get("scope_kind"), "artifact scope_kind"),
        statistics=statistics,
        exact_statistics=exact_statistics,
        combinations=combinations,
        exact_combinations=exact_combinations,
        pair_statistics=pair_statistics,
        role_target_counts=(dict(role_target) if isinstance(role_target, Mapping) else {}),
        deck_role_target_counts=(
            dict(deck_role_target) if isinstance(deck_role_target, Mapping) else {}
        ),
        maximum_score=_integer(value.get("maximum_score"), "artifact maximum_score", minimum=1),
        minimum_exact_support=_integer(
            value.get("minimum_exact_support"),
            "artifact minimum_exact_support",
            minimum=1,
        ),
    )


def load_memory_loadout_prior_artifact(
    artifact: str | Path = DEFAULT_PRIOR_ARTIFACT,
    *,
    produce_id: str,
    plan_type: str,
    exam_effect_type: str,
    idol_card_id: str = "",
) -> MemoryLoadoutPrior:
    """Load one broad/exact prior from a generated offline artifact."""

    if Path(artifact) == DEFAULT_PRIOR_ARTIFACT:
        from .portable_behavior_assets import load_public_behavior_role
        from .portable_memory_loadout_prior import read_memory_loadout_prior
        portable = load_public_behavior_role("memory_loadout_prior", read_memory_loadout_prior,
            produce_id=produce_id, plan_type=plan_type, exam_effect_type=exam_effect_type, idol_card_id=idol_card_id)
        if portable is not None:
            return portable
    path = Path(artifact).resolve()
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping) or payload.get("schema") != ARTIFACT_SCHEMA:
        raise ValueError("unsupported memory loadout prior artifact schema")
    scope_key = flow_string(_flow(produce_id, plan_type, exam_effect_type))
    scopes = payload.get("scopes")
    if not isinstance(scopes, Mapping) or not isinstance(scopes.get(scope_key), Mapping):
        raise ValueError("memory loadout prior artifact has no matching flow scope")
    scope_payload = scopes[scope_key]
    selected: object = scope_payload.get("broad")
    exact_by_idol = scope_payload.get("exact_by_idol", {})
    if idol_card_id and isinstance(exact_by_idol, Mapping):
        selected = exact_by_idol.get(idol_card_id, selected)
    if not isinstance(selected, Mapping):
        raise ValueError("memory loadout prior artifact has no selected prior")
    prior = _load_prior_from_artifact_payload(selected)
    if not prior.applies_to(
        produce_id=produce_id,
        plan_type=plan_type,
        exam_effect_type=exam_effect_type,
        idol_card_id=idol_card_id,
    ):
        raise ValueError("memory loadout prior artifact selected scope mismatch")
    return prior


def load_hierarchical_memory_loadout_prior(
    source: str | Path = DEFAULT_LEADERBOARD_EPISODES,
    *,
    produce_id: str,
    plan_type: str,
    exam_effect_type: str,
    idol_card_id: str = "",
    minimum_exact_trajectories: int = MIN_EXACT_SUPPORT,
    require_complete: bool = True,
    history_sources: str | Path | Sequence[str | Path] | LeaderboardRawHistoryCollection | None = None,
    database: str | Path = DEFAULT_DATABASE,
) -> HierarchicalMemoryLoadoutPrior:
    """Load exact and broad evidence while retaining both audit views."""

    minimum_exact = _integer(
        minimum_exact_trajectories,
        "minimum_exact_trajectories",
        minimum=1,
    )
    requested_flow = _flow(produce_id, plan_type, exam_effect_type)
    if not isinstance(require_complete, bool):
        raise TypeError("require_complete must be boolean")
    if history_sources is not None:
        observations, _report = build_memory_loadout_observations_from_sources(
            source,
            history_sources=history_sources,
            require_complete=require_complete,
            history_database=database,
            history_plan_type=plan_type,
            history_exam_effect_type=exam_effect_type,
        )
        source_for_metadata: object = (source, history_sources)
    else:
        observations, _report = build_memory_loadout_observations(
            source,
            require_complete=require_complete,
        )
        source_for_metadata = source
    same_flow = tuple(value for value in observations if value.flow == requested_flow)
    if not same_flow:
        raise ValueError("memory loadout prior has no matching produce/plan/effect scope")
    exact_values = tuple(
        value for value in same_flow
        if idol_card_id and value.idol_card_id == idol_card_id
    )
    source_path, source_sha256 = _source_metadata(source_for_metadata)
    exact = (
        MemoryLoadoutPrior.from_observations(
            exact_values,
            flow=requested_flow,
            requested_idol_card_id=idol_card_id,
            scope_kind="exact",
            source_path=source_path,
            source_sha256=source_sha256,
            minimum_exact_support=minimum_exact,
        )
        if len(exact_values) >= minimum_exact
        else None
    )
    broad = MemoryLoadoutPrior.from_observations(
        same_flow,
        flow=requested_flow,
        requested_idol_card_id="",
        scope_kind="broad",
        source_path=source_path,
        source_sha256=source_sha256,
        minimum_exact_support=minimum_exact,
    )
    return HierarchicalMemoryLoadoutPrior(
        requested_produce_id=produce_id,
        requested_plan_type=plan_type,
        requested_exam_effect_type=exam_effect_type,
        requested_idol_card_id=idol_card_id,
        exact=exact,
        broad=broad,
        source_path=source_path,
        source_sha256=source_sha256,
    )


def try_load_memory_loadout_prior(
    *,
    source: str | Path = DEFAULT_LEADERBOARD_EPISODES,
    produce_id: str,
    plan_type: str,
    exam_effect_type: str,
    idol_card_id: str = "",
    minimum_exact_trajectories: int = MIN_EXACT_SUPPORT,
    require_complete: bool = True,
    history_sources: str | Path | Sequence[str | Path] | LeaderboardRawHistoryCollection | None = None,
    database: str | Path = DEFAULT_DATABASE,
) -> MemoryLoadoutPrior | None:
    try:
        return load_memory_loadout_prior(
            source,
            produce_id=produce_id,
            plan_type=plan_type,
            exam_effect_type=exam_effect_type,
            idol_card_id=idol_card_id,
            minimum_exact_trajectories=minimum_exact_trajectories,
            require_complete=require_complete,
            history_sources=history_sources,
            database=database,
        )
    except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


try_load_default_memory_loadout_prior = try_load_memory_loadout_prior


def try_load_hierarchical_memory_loadout_prior(
    *,
    source: str | Path = DEFAULT_LEADERBOARD_EPISODES,
    produce_id: str,
    plan_type: str,
    exam_effect_type: str,
    idol_card_id: str = "",
    minimum_exact_trajectories: int = MIN_EXACT_SUPPORT,
    require_complete: bool = True,
    history_sources: str | Path | Sequence[str | Path] | LeaderboardRawHistoryCollection | None = None,
    database: str | Path = DEFAULT_DATABASE,
) -> HierarchicalMemoryLoadoutPrior | None:
    try:
        return load_hierarchical_memory_loadout_prior(
            source,
            produce_id=produce_id,
            plan_type=plan_type,
            exam_effect_type=exam_effect_type,
            idol_card_id=idol_card_id,
            minimum_exact_trajectories=minimum_exact_trajectories,
            require_complete=require_complete,
            history_sources=history_sources,
            database=database,
        )
    except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


try_load_default_hierarchical_memory_loadout_prior = (
    try_load_hierarchical_memory_loadout_prior
)


def score_memory_loadout_combination(
    prior: MemoryLoadoutPrior | Iterable[object],
    candidates: Iterable[object] | MemoryLoadoutPrior,
    *,
    deck_cards: Iterable[object] = (),
) -> MemoryLoadoutCombinationScore:
    # Accept both ``(prior, candidates)`` and the natural consumer ordering
    # ``(candidates, prior)``; the method form remains unambiguous.
    if isinstance(prior, MemoryLoadoutPrior):
        selected_prior = prior
        selected_candidates = candidates
    elif isinstance(candidates, MemoryLoadoutPrior):
        selected_prior = candidates
        selected_candidates = prior
    else:
        raise TypeError("one argument must be MemoryLoadoutPrior")
    return selected_prior.combination_score_for(selected_candidates, deck_cards=deck_cards)


def rank_memory_loadout_combinations(
    prior: MemoryLoadoutPrior | Iterable[Iterable[object]],
    combinations: Iterable[Iterable[object]] | MemoryLoadoutPrior,
    *,
    deck_cards: Iterable[object] = (),
) -> MemoryLoadoutRanking:
    if isinstance(prior, MemoryLoadoutPrior):
        selected_prior = prior
        selected_combinations = combinations
    elif isinstance(combinations, MemoryLoadoutPrior):
        selected_prior = combinations
        selected_combinations = prior
    else:
        raise TypeError("one argument must be MemoryLoadoutPrior")
    return MemoryLoadoutRanking(
        selected_prior.rank_combinations(selected_combinations, deck_cards=deck_cards)
    )


score_memory_loadout = score_memory_loadout_combination
score_memory_loadout_candidates = score_memory_loadout_combination
rank_memory_loadouts = rank_memory_loadout_combinations


__all__ = [
    "ARTIFACT_SCHEMA",
    "CombinationKey",
    "DEFAULT_LEADERBOARD_EPISODES",
    "DEFAULT_PRIOR_ARTIFACT",
    "ExactCombinationKey",
    "FlowKey",
    "HIERARCHICAL_SCHEMA",
    "HierarchicalLeaderboardMemoryLoadoutPrior",
    "HierarchicalMemoryLoadoutPrior",
    "MAX_SCORE",
    "MEMORY_COUNT",
    "MIN_EXACT_SUPPORT",
    "MemoryAbilityObservation",
    "MemoryCardCandidate",
    "MemoryLoadoutCardCandidate",
    "MemoryLoadoutCandidate",
    "MemoryLoadoutCombinationScore",
    "MemoryLoadoutCombinationStat",
    "MemoryLoadoutMemoryCandidate",
    "MemoryLoadoutObservation",
    "MemoryLoadoutObservationReport",
    "MemoryLoadoutObservationRow",
    "MemoryLoadoutPairStat",
    "MemoryLoadoutPrior",
    "MemoryLoadoutRanking",
    "MemoryLoadoutScore",
    "OBSERVATION_SCHEMA",
    "REPORT_SCHEMA",
    "SCHEMA",
    "build_leaderboard_memory_loadout_observations",
    "build_history_memory_loadout_observations",
    "build_memory_loadout_observations",
    "build_memory_loadout_observations_from_history",
    "build_memory_loadout_observations_from_history_sources",
    "build_memory_loadout_observations_from_history_collection",
    "build_memory_loadout_observations_from_collection",
    "build_memory_loadout_prior",
    "flow_string",
    "load_hierarchical_memory_loadout_prior",
    "load_leaderboard_memory_loadout_prior",
    "load_memory_loadout_prior",
    "load_memory_loadout_prior_artifact",
    "observe_memory_loadout_episode",
    "observe_leaderboard_episode",
    "project_leaderboard_memory_loadout",
    "project_memory_loadout_episode",
    "rank_memory_loadout_combinations",
    "rank_memory_loadouts",
    "score_memory_loadout",
    "score_memory_loadout_candidates",
    "score_memory_loadout_combination",
    "try_load_memory_loadout_prior",
    "try_load_default_memory_loadout_prior",
    "try_load_hierarchical_memory_loadout_prior",
    "try_load_default_hierarchical_memory_loadout_prior",
    "write_default_memory_loadout_prior_artifact",
]
