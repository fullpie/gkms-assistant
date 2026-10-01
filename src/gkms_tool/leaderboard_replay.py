"""Normalize structured 3.3.0 leaderboard histories for offline learning.

The input is protobuf JSON emitted inside the game by either
``ProduceListLatestHistoryResponse.ToString()`` or
``ProduceHistoryResponse.ToString()``.  Profiles and public user identifiers
are intentionally discarded.  This module performs no network access and
does not read or persist authentication material.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Mapping, Sequence


SCHEMA = "gkms.leaderboard-replay-episode.v2"
LEGACY_SCHEMA = "gkms.leaderboard-replay-episode.v1"
INGEST_MANIFEST_SCHEMA = "gkms.leaderboard-replay-ingest-manifest.v2"
LEGACY_INGEST_MANIFEST_SCHEMA = "gkms.leaderboard-replay-ingest-manifest.v1"
DEFAULT_EPISODES_FILENAME = "episodes.jsonl"
DEFAULT_MANIFEST_FILENAME = "manifest.json"
LIST_LATEST_HISTORY_SOURCE_TYPE = "ListLatestHistory"
HISTORY_SOURCE_TYPE = "History"
LEADERBOARD_RESPONSE_SOURCE_TYPES = frozenset(
    {LIST_LATEST_HISTORY_SOURCE_TYPE, HISTORY_SOURCE_TYPE}
)
RAW_HISTORY_COLLECTION_SCHEMA = "gkms.leaderboard-raw-history-collection.v1"
LEADERBOARD_QUERY_RESULT_SCHEMA = "gkms.leaderboard-query-result.v1"
# ``mode-ranking-query-result.v1`` is an offline envelope produced by the
# ranking capture workflow.  It may contain the summaries returned by
# RankingTop/Ranking as well as one or more History responses.  Keep the
# protocol spelling separate from the compact provenance labels written to
# de-identified datasets.
MODE_RANKING_QUERY_RESULT_SCHEMA = "gkms.mode-ranking-query-result.v1"
MODE_RANKING_TOP_SOURCE = "mode_ranking_top"
MODE_RANKING_SOURCE = "mode_ranking"
LIST_LATEST_HISTORY_SOURCE = "list_latest_history"
MODE_RANKING_PROVENANCE_SOURCES = frozenset(
    {MODE_RANKING_TOP_SOURCE, MODE_RANKING_SOURCE, LIST_LATEST_HISTORY_SOURCE}
)
# Explicit ``SOURCE_*`` aliases keep the labels discoverable alongside the
# existing response-type constants and make callers independent of naming
# style.
SOURCE_MODE_RANKING_TOP = MODE_RANKING_TOP_SOURCE
SOURCE_MODE_RANKING = MODE_RANKING_SOURCE
SOURCE_LIST_LATEST_HISTORY = LIST_LATEST_HISTORY_SOURCE
MODE_RANKING_RESPONSE_SOURCE_TYPE = "ModeRanking"
FAILED_QUERY_RESULT_FALLBACK_CODE = "query-result-failed"


class _NoReplayEpisodesError(ValueError):
    """A structurally valid response that carries no replayable episode."""


@dataclass(frozen=True, slots=True)
class LeaderboardRawSource:
    """One read-only JSON input selected for an offline build.

    ``name`` is intentionally only a display name (normally the basename),
    never an absolute path.  Keeping this value separate from ``path`` makes
    it safe for a caller to use the same input list when writing a manifest.
    """

    path: Path
    name: str


@dataclass(frozen=True, slots=True)
class LeaderboardRawHistoryRecord:
    """A complete, de-identified history available to replay/outer builders."""

    source_name: str
    source_sha256: str
    source_type: str
    history_index: int
    history_identity: str
    history: Mapping[str, Any]
    # A single de-identified history can be observed through multiple ranking
    # endpoints.  Keep the compact source labels on the record so downstream
    # builders can merge without losing that audit trail.
    sources: tuple[str, ...] = ()

    @property
    def source_hash(self) -> str:
        """Short alias used by dataset consumers that call it a source hash."""

        return self.source_sha256

    @property
    def identity(self) -> str:
        return self.history_identity

    @property
    def payload(self) -> Mapping[str, Any]:
        return self.history

    @property
    def provenance(self) -> tuple[str, ...]:
        """Compatibility alias for callers that call sources provenance."""

        return self.sources

    @property
    def source_types(self) -> tuple[str, ...]:
        return self.sources


@dataclass(frozen=True, slots=True)
class LeaderboardRawHistoryDrop:
    """An entire raw history excluded from a collection, with an audit reason."""

    source_name: str
    source_sha256: str
    source_type: str
    history_index: int
    history_identity: str
    reason: str
    detail: str = ""
    sources: tuple[str, ...] = ()

    @property
    def source_hash(self) -> str:
        return self.source_sha256

    @property
    def identity(self) -> str:
        return self.history_identity

    @property
    def provenance(self) -> tuple[str, ...]:
        return self.sources


@dataclass(frozen=True, slots=True)
class LeaderboardRawHistoryCollection:
    """Deterministic input list shared by replay and outer dataset builders.

    The collection is read-only with respect to raw files.  ``records`` are
    complete histories after de-identification; ``dropped`` contains one row
    per incomplete history, rather than a partial episode or trajectory.
    """

    records: tuple[LeaderboardRawHistoryRecord, ...]
    dropped: tuple[LeaderboardRawHistoryDrop, ...]
    source_files: tuple[dict[str, Any], ...]
    source_file_count: int
    unique_source_hash_count: int
    duplicate_source_count: int
    duplicate_history_count: int
    skipped_failed_sources: tuple[dict[str, Any], ...] = ()
    provenance_sources: tuple[str, ...] = ()

    @property
    def history_seen(self) -> int:
        return len(self.records) + len(self.dropped) + self.duplicate_history_count

    @property
    def skipped_failed_source_count(self) -> int:
        """Number of failed authenticated queue result files skipped in a batch."""

        return len(self.skipped_failed_sources)

    def to_manifest(self) -> dict[str, Any]:
        """Return a path-free manifest suitable for an offline dataset."""

        reason_counts: dict[str, int] = {}
        for row in self.dropped:
            reason_counts[row.reason] = reason_counts.get(row.reason, 0) + 1
        return {
            "schema": RAW_HISTORY_COLLECTION_SCHEMA,
            "source_file_count": self.source_file_count,
            "unique_source_hash_count": self.unique_source_hash_count,
            "duplicate_raw_file_count": self.duplicate_source_count,
            "history_seen": self.history_seen,
            "history_written": len(self.records),
            "duplicate_history_count": self.duplicate_history_count,
            "dropped_incomplete_history_count": len(self.dropped),
            "drop_reason_counts": dict(sorted(reason_counts.items())),
            "skipped_failed_source_count": len(self.skipped_failed_sources),
            "skipped_failed_sources": [
                dict(row) for row in self.skipped_failed_sources
            ],
            "provenance_sources": list(self.provenance_sources),
            "source_files": [dict(row) for row in self.source_files],
            "dropped_histories": [
                {
                    "source_name": row.source_name,
                    "path": row.source_name,
                    "sha256": row.source_sha256,
                    "source_type": row.source_type,
                    "history_index": row.history_index,
                    "history_identity": row.history_identity,
                    "reason": row.reason,
                    **({"sources": list(row.sources)} if row.sources else {}),
                    **({"detail": row.detail} if row.detail else {}),
                }
                for row in self.dropped
            ],
        }


# The Localify bridge applies the same bound before writing a raw response.
# Keeping the bound here prevents a hand-placed file from causing an
# unbounded allocation during batch ingest.
MAX_RAW_BYTES = 256 * 1024 * 1024
# These fields are useful only while correlating ranking rows.  They are
# deliberately removed before any raw history is exposed to the training
# builders.  ``userMemoryId`` is included here even though older replay
# episodes did not project the enclosing Memory object.
_IDENTITY_FIELD_NAMES = frozenset(
    {"profile", "publicuserid", "username", "usermemoryid"}
)
ACTION_TYPE_NAMES = {
    0: "unknown",
    1: "use-hand",
    2: "use-drink",
    3: "turn-end",
    4: "effect-card-select",
}
_ENUM_ACTION_TYPES = {
    "Unknown": "unknown",
    "UseHand": "use-hand",
    "UseDrink": "use-drink",
    "TurnEnd": "turn-end",
    "EffectCardSelect": "effect-card-select",
    "ExamActionTypeUnknown": "unknown",
    "ExamActionTypeUseHand": "use-hand",
    "ExamActionTypeUseDrink": "use-drink",
    "ExamActionTypeTurnEnd": "turn-end",
    "ExamActionTypeEffectCardSelect": "effect-card-select",
    "ExamActionType_Unknown": "unknown",
    "ExamActionType_UseHand": "use-hand",
    "ExamActionType_UseDrink": "use-drink",
    "ExamActionType_TurnEnd": "turn-end",
    "ExamActionType_EffectCardSelect": "effect-card-select",
}


def _is_identity_field(name: object) -> bool:
    """Return whether *name* is a profile/public-user identity field.

    Protobuf JSON can be emitted with either lower-camel or PascalCase names.
    Removing punctuation as well also covers hand-normalized snake_case input
    without accidentally removing unrelated fields.
    """

    if not isinstance(name, str):
        return False
    normalized = "".join(character for character in name.casefold() if character.isalnum())
    return normalized in _IDENTITY_FIELD_NAMES


def _deidentify(value: object, label: str = "value") -> Any:
    """Recursively remove profile and public-user identifiers from JSON data."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} contains a non-text object key")
            if _is_identity_field(key):
                continue
            result[key] = _deidentify(child, f"{label}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_deidentify(child, f"{label}[]") for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"{label} contains unsupported JSON value {type(value).__name__}")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant is not allowed: {value}")


def _decode_json_bytes(data: bytes, label: str) -> object:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from error
    try:
        return json.loads(text, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError, TypeError) as error:
        raise ValueError(f"{label} is malformed JSON: {error}") from error


def _read_json_file(source: str | Path) -> object:
    path = Path(source)
    if not path.is_file():
        raise FileNotFoundError(f"leaderboard JSON does not exist: {path}")
    size = path.stat().st_size
    if size <= 0:
        raise ValueError(f"leaderboard JSON is empty: {path}")
    if size > MAX_RAW_BYTES:
        raise ValueError(
            f"leaderboard JSON exceeds {MAX_RAW_BYTES} bytes: {path}"
        )
    return _decode_json_bytes(path.read_bytes(), str(path))


def _value(row: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in row:
            return row[name]
    return default


def _name_token(value: object) -> str:
    """Normalize a protobuf JSON field name for case/style-insensitive lookup."""

    if not isinstance(value, str):
        return ""
    return "".join(character for character in value.casefold() if character.isalnum())


def _field_by_token(value: object, *names: str) -> Any:
    """Read one direct object field while accepting protobuf name variants."""

    if not isinstance(value, Mapping):
        return None
    wanted = {_name_token(name) for name in names}
    for key, child in value.items():
        if _name_token(key) in wanted:
            return child
    return None


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _rows(value: object, label: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ValueError(f"{label} must be an array")
    if any(not isinstance(row, Mapping) for row in value):
        raise ValueError(f"{label} contains a non-object row")
    return tuple(value)  # type: ignore[return-value]


def _values(value: object, label: str) -> tuple[Any, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ValueError(f"{label} must be an array")
    return tuple(value)


def _action_rows(
    value: object,
    label: str,
) -> tuple[Mapping[str, Any], ...] | None:
    """Validate an ExamActions array and mark replay-unavailable sentinels.

    The game can serialize a section with no replay as either ``[]`` or the
    exact protobuf repeated-field shape ``[null]``.  Only those two shapes are
    tolerated as unavailable.  Any other non-object row, including a null
    mixed with a real action, is malformed and fails closed.
    """

    values = _values(value, label)
    if not values or (len(values) == 1 and values[0] is None):
        return None
    if any(not isinstance(row, Mapping) for row in values):
        raise ValueError(f"{label} contains a non-object action row")
    return tuple(values)  # type: ignore[return-value]


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _typed_enum_value(value: object, label: str, prefix: str) -> int | str:
    """Validate an enum field emitted as an int or a namespaced string.

    The protobuf JSON seen in the live dump uses names such as
    ``ProduceStepType_AuditionMid1``.  Accepting arbitrary strings or JSON
    values here would make malformed metadata look like a valid RL state, so
    the enum namespace and non-empty suffix are checked explicitly.
    """

    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and value.startswith(prefix) and len(value) > len(prefix):
        return value
    raise ValueError(
        f"{label} must be a non-negative integer or non-empty {prefix} enum"
    )


def _nonnegative_decimal_int(value: object, label: str) -> int:
    """Canonicalize a non-negative integer or an ASCII decimal string.

    Protobuf 64-bit fields can be rendered as decimal JSON strings by the
    game's formatter.  Signs, whitespace, empty strings and non-ASCII/non-
    decimal text are deliberately rejected instead of being coerced.
    """

    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if (
        isinstance(value, str)
        and value
        and all("0" <= character <= "9" for character in value)
    ):
        return int(value, 10)
    raise ValueError(
        f"{label} must be a non-negative integer or pure decimal string"
    )


def _optional_integer(value: object, label: str) -> int | None:
    return None if value is None else _integer(value, label)


def _text(value: object, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValueError(f"{label} must be text")
    return value


def _optional_text(value: object, label: str) -> str:
    """Canonicalize an omitted/null optional text field to ``""``."""

    if value is None:
        return ""
    return _text(value, label, allow_empty=True)


def _produce_card_loadout_projection(
    value: object, label: str
) -> dict[str, Any]:
    """Project the small, de-identified ``ProduceCard`` loadout shape.

    ``ProduceHistory.memory`` and each ``deckMemories[].memory`` carry the
    same protobuf ``Memory`` type.  A card in that object is not the same
    thing as the result/history card list, however, so keep this projection
    deliberately explicit.  Protobuf omits default-valued scalar/repeated
    fields; canonicalizing those defaults here makes the loadout stable while
    retaining the complete card identity and customize counts.
    """

    card = _mapping(value, label)
    card_id = _text(
        _value(card, "id", "Id"),
        f"{label}.id",
    )
    upgrade_count = _integer(
        _value(card, "upgradeCount", "UpgradeCount", default=0),
        f"{label}.upgradeCount",
    )
    customize_values = _values(
        _value(card, "customizes", "Customizes", default=[]),
        f"{label}.customizes",
    )
    customizes: list[Mapping[str, Any]] = []
    for customize_index, raw_customize in enumerate(customize_values):
        customize = _mapping(
            raw_customize,
            f"{label}.customizes[{customize_index}]",
        )
        customizes.append(
            {
                "id": _text(
                    _value(customize, "id", "Id"),
                    f"{label}.customizes[{customize_index}].id",
                ),
                "customizeCount": _integer(
                    _value(
                        customize,
                        "customizeCount",
                        "CustomizeCount",
                        default=0,
                    ),
                    f"{label}.customizes[{customize_index}].customizeCount",
                ),
            }
        )
    return {
        "id": card_id,
        "upgradeCount": upgrade_count,
        "customizes": customizes,
    }


def _deck_memory_entries(
    history: Mapping[str, Any], label: str
) -> tuple[tuple[int, bool, Mapping[str, Any]], ...] | None:
    """Validate and return the equipped memory entries in native order."""

    try:
        entries = _rows(
            _value(history, "deckMemories", "DeckMemories"),
            f"{label}.deckMemories",
        )
        if not entries:
            return None
        result: list[tuple[int, bool, Mapping[str, Any]]] = []
        for memory_slot, entry in enumerate(entries):
            memory = _mapping(
                _value(entry, "memory", "Memory"),
                f"{label}.deckMemories[{memory_slot}].memory",
            )
            is_rental_value = _value(
                entry, "isRental", "IsRental", default=False
            )
            if not isinstance(is_rental_value, bool):
                return None
            result.append((memory_slot, is_rental_value, memory))
        return tuple(result)
    except ValueError:
        return None


def _deck_memory_loadout(
    history: Mapping[str, Any], label: str
) -> tuple[Mapping[str, Any], ...] | None:
    """Project the complete, de-identified equipped-memory loadout.

    Only ``deckMemories[]`` is an audition input.  The sibling
    ``ProduceHistory.memory`` is a result memory and is intentionally never
    consulted here.  This projection is auxiliary to the authoritative final
    deck: an unavailable or partial card projection returns an empty tuple so
    a complete three-audition replay is not discarded merely because an older
    response omitted memory-card metadata.
    """

    entries = _deck_memory_entries(history, label)
    if entries is None:
        return None
    # v2 episode artifacts written before the full memory-card projection only
    # carried ``abilities``.  Keep those rows readable by representing the
    # newly added optional projection as an empty loadout; a raw history that
    # starts providing the two current-run card fields is parsed below.
    # Historical examBattle* fields describe the run that created the memory,
    # not this replay's carried card, and intentionally do not participate.
    loadout_field_names = (
        "produceCard",
        "ProduceCard",
        "produceCardPhaseType",
        "ProduceCardPhaseType",
    )
    if not any(
        any(name in memory for name in loadout_field_names)
        for _slot, _rental, memory in entries
    ):
        return ()
    required_memory_fields = (
        ("produceCard", "ProduceCard"),
        ("produceCardPhaseType", "ProduceCardPhaseType"),
    )
    if any(
        not any(name in memory for name in names)
        for _slot, _rental, memory in entries
        for names in required_memory_fields
    ):
        # Do not emit a partial four-slot projection and do not sacrifice the
        # complete final-deck target.  Unknown auxiliary memory-card metadata
        # is represented explicitly by an empty loadout.
        return ()
    try:
        result: list[Mapping[str, Any]] = []
        for memory_slot, is_rental, memory in entries:
            produce_card = _produce_card_loadout_projection(
                _value(memory, "produceCard", "ProduceCard"),
                f"{label}.deckMemories[{memory_slot}].memory.produceCard",
            )
            phase_type = _typed_enum_value(
                _value(
                    memory,
                    "produceCardPhaseType",
                    "ProduceCardPhaseType",
                ),
                f"{label}.deckMemories[{memory_slot}].memory.produceCardPhaseType",
                "ProduceMemoryProduceCardPhaseType_",
            )
            result.append(
                {
                    "memory_slot": memory_slot,
                    "is_rental": is_rental,
                    "produce_card": produce_card,
                    "produce_card_phase_type": phase_type,
                }
            )
        return tuple(result)
    except ValueError:
        return ()


def _deck_memory_abilities(
    history: Mapping[str, Any], label: str
) -> tuple[Mapping[str, Any], ...] | None:
    """Flatten equipped ``deckMemories`` abilities in native order.

    ``ProduceHistory.memory`` describes a result memory and is not the loadout
    used to start the audition.  The replay loadout is ``deckMemories[]``;
    each entry contributes its nested ``memory.abilities`` in array order.
    Only the slot, rental marker, ability ID and level are emitted.  Missing
    or malformed loadout data returns ``None`` so the caller can discard the
    whole history without manufacturing a partial deck.
    """

    entries = _deck_memory_entries(history, label)
    if entries is None:
        return None
    try:
        abilities: list[Mapping[str, Any]] = []
        for memory_slot, is_rental_value, memory in entries:
            ability_values = _values(
                _value(memory, "abilities", "Abilities"),
                f"{label}.deckMemories[{memory_slot}].memory.abilities",
            )
            if not ability_values:
                return None
            for ability_index, value in enumerate(ability_values):
                ability = _mapping(
                    value,
                    f"{label}.deckMemories[{memory_slot}].memory.abilities[{ability_index}]",
                )
                ability_id = _text(
                    _value(ability, "id", "Id"),
                    f"{label}.deckMemories[{memory_slot}].memory.abilities[{ability_index}].id",
                )
                level = _integer(
                    _value(ability, "level", "Level"),
                    f"{label}.deckMemories[{memory_slot}].memory.abilities[{ability_index}].level",
                )
                abilities.append(
                    {
                        "memory_slot": memory_slot,
                        "is_rental": is_rental_value,
                        "id": ability_id,
                        "level": level,
                    }
                )
        return tuple(abilities)
    except ValueError:
        return None


def _deck_support_catalog(
    history: Mapping[str, Any], label: str
) -> dict[str, int] | None:
    """Read the six equipped support IDs and levels from a history."""

    try:
        rows = _rows(
            _value(history, "deckSupportCards", "DeckSupportCards"),
            f"{label}.deckSupportCards",
        )
        if len(rows) != 6:
            return None
        catalog: dict[str, int] = {}
        for index, row in enumerate(rows):
            support_id = _text(
                _value(row, "id", "Id"),
                f"{label}.deckSupportCards[{index}].id",
            )
            level = _integer(
                _value(row, "level", "Level"),
                f"{label}.deckSupportCards[{index}].level",
            )
            if support_id in catalog:
                return None
            catalog[support_id] = level
        return catalog
    except ValueError:
        return None


def _merge_support_cards(
    player: Mapping[str, Any],
    catalog: Mapping[str, int],
    label: str,
) -> tuple[Mapping[str, Any], ...] | None:
    """Join runtime upgrade permil rows with the equipped support levels."""

    try:
        rows = _rows(
            _value(player, "supportCards", "SupportCards"),
            f"{label}.supportCards",
        )
        if len(rows) != 6:
            return None
        seen: set[str] = set()
        merged: list[Mapping[str, Any]] = []
        for index, row in enumerate(rows):
            support_id = _text(
                _value(row, "supportCardId", "SupportCardId"),
                f"{label}.supportCards[{index}].supportCardId",
            )
            upgrade_permil = _integer(
                _value(
                    row,
                    "produceCardUpgradePermil",
                    "ProduceCardUpgradePermil",
                ),
                f"{label}.supportCards[{index}].produceCardUpgradePermil",
            )
            if support_id in seen or support_id not in catalog:
                return None
            seen.add(support_id)
            merged.append(
                {
                    "supportCardId": support_id,
                    "level": catalog[support_id],
                    "produceCardUpgradePermil": upgrade_permil,
                }
            )
        if seen != set(catalog):
            return None
        return tuple(merged)
    except ValueError:
        return None


def _context_key(name: str) -> str:
    """Return the stable spelling used for a native replay context key.

    Context objects are intentionally a projection of the raw protobuf JSON,
    rather than a second guessed schema.  Known fields get the same snake-case
    spelling as the rest of this module; fields added by a future game build
    remain present under their original spelling after de-identification.
    """

    normalized = "".join(character for character in name.casefold() if character.isalnum())
    aliases = {
        "limitturn": "limit_turn",
        "forceendscore": "force_end_score",
        "produceexamgimmickeffectgroupid": "produce_exam_gimmick_effect_group_id",
        "produceexambattlenpcgroupid": "produce_exam_battle_npc_group_id",
        "examstatusenchants": "exam_status_enchants",
        "exampermanentstatusenchants": "exam_permanent_status_enchants",
    }
    return aliases.get(normalized, name)


def _context_projection(
    value: Mapping[str, Any],
    label: str,
    *,
    excluded: frozenset[str],
) -> dict[str, Any]:
    """Copy only the context around one native stage/section.

    ``selfSections`` and ``player`` are structural children of the raw
    response, not context.  Everything else is retained if it was actually
    serialized by the game.  This keeps status/gimmick/battle extensions
    available without manufacturing defaults for fields absent in a dump.
    """

    result: dict[str, Any] = {}
    for key, child in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{label} contains a non-text object key")
        normalized = "".join(character for character in key.casefold() if character.isalnum())
        if normalized in excluded:
            continue
        result[_context_key(key)] = _deidentify(child, f"{label}.{key}")
    return result


def _context_optional_integer(
    value: Mapping[str, Any], names: tuple[str, ...], label: str
) -> int | None:
    for name in names:
        if name not in value:
            continue
        raw = value[name]
        if raw is None:
            return None
        return _integer(raw, label)
    return None


def _action_type(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        if value not in ACTION_TYPE_NAMES:
            raise ValueError(f"unknown ExamActionType integer: {value}")
        return ACTION_TYPE_NAMES[value]
    if isinstance(value, str) and value in _ENUM_ACTION_TYPES:
        return _ENUM_ACTION_TYPES[value]
    raise ValueError(f"unknown ExamActionType: {value!r}")


@dataclass(frozen=True, slots=True)
class LeaderboardReplayAction:
    order: int
    action_type: str
    indexes: tuple[int, ...]

    def __post_init__(self) -> None:
        _integer(self.order, "action.order")
        if self.action_type not in set(ACTION_TYPE_NAMES.values()):
            raise ValueError("action_type is unsupported")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in self.indexes
        ):
            raise ValueError("action indexes must be non-negative integers")


@dataclass(frozen=True, slots=True)
class LeaderboardReplayEpisode:
    produce_id: str
    idol_card_id: str
    audition_index: int
    step_type: int | str
    step_select_number: int
    rank: int
    terminal_score: int
    stage_index: int
    section_index: int
    app_version: str
    image_version: str
    master_version: str
    master_hash: str
    exam_setting_id: str
    plan_type: int | str
    seed: int
    character_id: str
    exam_effect_type: int | str
    stamina: int
    max_stamina: int
    vocal: int
    dance: int
    visual: int
    vocal_bonus_permil: int
    dance_bonus_permil: int
    visual_bonus_permil: int
    memory_abilities: tuple[Mapping[str, Any], ...]
    memory_loadout: tuple[Mapping[str, Any], ...]
    produce_cards: tuple[Mapping[str, Any], ...]
    produce_items: tuple[Mapping[str, Any], ...]
    produce_customize_item_ids: tuple[str, ...]
    produce_drink_ids: tuple[str, ...]
    support_cards: tuple[Mapping[str, Any], ...]
    actions: tuple[LeaderboardReplayAction, ...]
    trajectory_id: str
    limit_turn: int | None = None
    force_end_score: int | None = None
    stage_context: Mapping[str, Any] = field(default_factory=dict)
    section_context: Mapping[str, Any] = field(default_factory=dict)
    schema: str = SCHEMA
    # Optional because v2 files created before cross-source provenance remain
    # valid.  Ingested mode-ranking rows populate this with the source labels
    # that contributed the same match.
    sources: tuple[str, ...] = ()
    # Optional live-recorder supplement.  Canonical leaderboard History does
    # not provide per-action native state; when present, these rows are joined
    # by explicit ``action_order``/``manual_command_sequence`` and are never
    # synthesized by the normalizer.
    native_action_states: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not self.actions:
            raise ValueError("replay episode actions must be non-empty")
        _text(self.trajectory_id, "episode.trajectory_id")
        if self.limit_turn is not None:
            _integer(self.limit_turn, "episode.limit_turn")
        if self.force_end_score is not None:
            _integer(self.force_end_score, "episode.force_end_score")
        for field_name, value in (
            ("vocal_bonus_permil", self.vocal_bonus_permil),
            ("dance_bonus_permil", self.dance_bonus_permil),
            ("visual_bonus_permil", self.visual_bonus_permil),
        ):
            _integer(value, f"episode.{field_name}")
        for index, ability in enumerate(self.memory_abilities):
            if not isinstance(ability, Mapping) or set(ability) != {
                "memory_slot",
                "is_rental",
                "id",
                "level",
            }:
                raise ValueError(
                    "episode.memory_abilities["
                    f"{index}] must contain only memory_slot, is_rental, id and level"
                )
            _integer(
                ability["memory_slot"],
                f"episode.memory_abilities[{index}].memory_slot",
            )
            if not isinstance(ability["is_rental"], bool):
                raise ValueError(
                    f"episode.memory_abilities[{index}].is_rental must be boolean"
                )
            _text(ability["id"], f"episode.memory_abilities[{index}].id")
            _integer(ability["level"], f"episode.memory_abilities[{index}].level")
        if not isinstance(self.memory_loadout, Sequence) or isinstance(
            self.memory_loadout, (str, bytes)
        ):
            raise ValueError("episode.memory_loadout must be an array")
        loadout_keys = {
            "memory_slot",
            "is_rental",
            "produce_card",
            "produce_card_phase_type",
        }
        for index, loadout in enumerate(self.memory_loadout):
            if not isinstance(loadout, Mapping) or set(loadout) != loadout_keys:
                raise ValueError(
                    "episode.memory_loadout["
                    f"{index}] must contain only memory_slot, is_rental, "
                    "produce_card and produce_card_phase_type"
                )
            _integer(
                loadout["memory_slot"],
                f"episode.memory_loadout[{index}].memory_slot",
            )
            if not isinstance(loadout["is_rental"], bool):
                raise ValueError(
                    f"episode.memory_loadout[{index}].is_rental must be boolean"
                )
            _produce_card_loadout_projection(
                loadout["produce_card"],
                f"episode.memory_loadout[{index}].produce_card",
            )
            _typed_enum_value(
                loadout["produce_card_phase_type"],
                f"episode.memory_loadout[{index}].produce_card_phase_type",
                "ProduceMemoryProduceCardPhaseType_",
            )
        if not isinstance(self.stage_context, Mapping):
            raise ValueError("episode.stage_context must be an object")
        if not isinstance(self.section_context, Mapping):
            raise ValueError("episode.section_context must be an object")
        if self.schema != SCHEMA:
            raise ValueError("unsupported leaderboard replay episode schema")
        if not isinstance(self.sources, Sequence) or isinstance(
            self.sources, (str, bytes)
        ):
            raise ValueError("episode.sources must be an array")
        source_values = tuple(self.sources)
        if any(
            not isinstance(value, str)
            or not value
            or value not in MODE_RANKING_PROVENANCE_SOURCES
            for value in source_values
        ):
            raise ValueError("episode.sources contains an unsupported source")
        if len(source_values) != len(set(source_values)):
            raise ValueError("episode.sources must be unique")
        object.__setattr__(self, "sources", tuple(sorted(source_values)))
        if not isinstance(self.native_action_states, Sequence) or isinstance(
            self.native_action_states, (str, bytes)
        ):
            raise ValueError("episode.native_action_states must be an array")
        state_values = tuple(self.native_action_states)
        if any(not isinstance(value, Mapping) for value in state_values):
            raise ValueError("episode.native_action_states must contain objects")
        object.__setattr__(self, "native_action_states", state_values)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["produce_cards"] = [dict(row) for row in self.produce_cards]
        value["produce_items"] = [dict(row) for row in self.produce_items]
        value["support_cards"] = [dict(row) for row in self.support_cards]
        value["memory_abilities"] = [dict(row) for row in self.memory_abilities]
        value["memory_loadout"] = [
            {
                **dict(row),
                "produce_card": dict(row["produce_card"]),
            }
            for row in self.memory_loadout
        ]
        value["produce_customize_item_ids"] = list(
            self.produce_customize_item_ids
        )
        value["produce_drink_ids"] = list(self.produce_drink_ids)
        value["actions"] = [asdict(action) for action in self.actions]
        value["stage_context"] = dict(self.stage_context)
        value["section_context"] = dict(self.section_context)
        if self.native_action_states:
            value["native_action_states"] = [
                dict(row) for row in self.native_action_states
            ]
        else:
            value.pop("native_action_states", None)
        if self.sources:
            value["sources"] = list(self.sources)
        else:
            value.pop("sources", None)
        # Optional native fields are omitted when the raw response omitted
        # them.  In particular, a missing forceEndScore must not become 0.
        if self.limit_turn is None:
            value.pop("limit_turn", None)
        if self.force_end_score is None:
            value.pop("force_end_score", None)
        # Do this at the final serialization boundary so identifiers nested in
        # card/item/support payloads cannot survive a future schema extension.
        sanitized = _deidentify(value, "episode")
        if not isinstance(sanitized, dict):  # pragma: no cover - defensive
            raise ValueError("episode serialization did not produce an object")
        return sanitized

    @property
    def provenance(self) -> tuple[str, ...]:
        """Alias for callers that use provenance terminology."""

        return self.sources


def normalize_list_latest_history(
    payload: Mapping[str, Any],
    *,
    _skip_missing_situation: bool = True,
    _allow_empty: bool = False,
    _normalization_stats: dict[str, int] | None = None,
) -> tuple[LeaderboardReplayEpisode, ...]:
    """Project a ListLatestHistory protobuf-JSON response to episodes.

    Both response shapes can contain summary rows without
    ``examContestSituation``.  Missing or null situations, absent stages or
    sections, and unavailable action arrays make the entire history
    replay-incomplete; no partial episodes from that history are returned. A
    present non-object situation remains malformed.
    The private ``_allow_empty`` mode is used by the direct ``Produce.History``
    wrapper so it can report its more specific loadout-only error.
    """

    root = _mapping(payload, "response")
    histories = _rows(
        _value(root, "histories", "Histories", default=[]),
        "response.histories",
    )
    episodes: list[LeaderboardReplayEpisode] = []
    saw_audition = False
    for history_index, history_row in enumerate(histories):
        # Profile is deliberately never read into the output.
        history = _mapping(
            _value(history_row, "produceHistory", "ProduceHistory"),
            f"histories[{history_index}].produceHistory",
        )
        produce_id = _text(
            _value(history, "produceId", "ProduceId"), "produceId"
        )
        idol_card_id = _text(
            _value(history, "idolCardId", "IdolCardId"), "idolCardId"
        )
        auditions = _rows(
            _value(history, "auditions", "Auditions", default=[]),
            f"histories[{history_index}].auditions",
        )
        if not auditions:
            raise ValueError(
                "ListLatestHistory response has empty history/no replay episodes"
            )
        history_episodes: list[LeaderboardReplayEpisode] = []
        memory_abilities: tuple[Mapping[str, Any], ...] | None = None
        memory_loadout: tuple[Mapping[str, Any], ...] | None = None
        support_catalog: dict[str, int] | None = None
        history_incomplete = False
        for audition_index, audition in enumerate(auditions):
            saw_audition = True
            situation_keys = ("examContestSituation", "ExamContestSituation")
            if _skip_missing_situation and (
                not any(key in audition for key in situation_keys)
                or _value(audition, *situation_keys) is None
            ):
                # A response may include summary/loadout rows with rank and
                # score only.  They are not replay episodes and must never
                # become synthetic RL transitions.
                history_incomplete = True
                continue
            situation = _mapping(
                _value(
                    audition,
                    "examContestSituation",
                    "ExamContestSituation",
                ),
                f"auditions[{audition_index}].examContestSituation",
            )
            stage_keys = ("stages", "Stages")
            if not any(key in situation for key in stage_keys) or _value(
                situation, *stage_keys
            ) is None:
                history_incomplete = True
                continue
            stages = _rows(
                _value(situation, *stage_keys),
                f"auditions[{audition_index}].stages",
            )
            if not stages:
                history_incomplete = True
                continue
            for stage_index, stage in enumerate(stages):
                section_keys = ("selfSections", "SelfSections")
                if not any(key in stage for key in section_keys) or _value(
                    stage, *section_keys
                ) is None:
                    history_incomplete = True
                    continue
                sections = _rows(
                    _value(stage, *section_keys),
                    f"stages[{stage_index}].selfSections",
                )
                if not sections:
                    history_incomplete = True
                    continue
                for section_index, section in enumerate(sections):
                    player = _mapping(
                        _value(section, "player", "Player"),
                        f"sections[{section_index}].player",
                    )
                    stage_context = _context_projection(
                        stage,
                        f"stages[{stage_index}]",
                        excluded=frozenset({"selfsections"}),
                    )
                    section_context = _context_projection(
                        section,
                        f"sections[{section_index}]",
                        excluded=frozenset({"player"}),
                    )
                    limit_turn = _context_optional_integer(
                        section,
                        ("limitTurn", "LimitTurn"),
                        f"sections[{section_index}].limitTurn",
                    )
                    if limit_turn is None:
                        limit_turn = _context_optional_integer(
                            stage,
                            ("limitTurn", "LimitTurn"),
                            f"stages[{stage_index}].limitTurn",
                        )
                    if limit_turn is None:
                        limit_turn = _context_optional_integer(
                            situation,
                            ("limitTurn", "LimitTurn"),
                            f"auditions[{audition_index}].limitTurn",
                        )
                    force_end_score = _context_optional_integer(
                        section,
                        ("forceEndScore", "ForceEndScore"),
                        f"sections[{section_index}].forceEndScore",
                    )
                    if force_end_score is None:
                        force_end_score = _context_optional_integer(
                            stage,
                            ("forceEndScore", "ForceEndScore"),
                            f"stages[{stage_index}].forceEndScore",
                        )
                    if force_end_score is None:
                        force_end_score = _context_optional_integer(
                            situation,
                            ("forceEndScore", "ForceEndScore"),
                            f"auditions[{audition_index}].forceEndScore",
                        )
                    action_rows = _action_rows(
                        _value(player, "examActions", "ExamActions", default=[]),
                        f"sections[{section_index}].examActions",
                    )
                    if action_rows is None:
                        # No replay data for this section; do not synthesize an
                        # episode from its loadout or score summary.
                        history_incomplete = True
                        continue
                    if memory_abilities is None or memory_loadout is None:
                        memory_abilities = _deck_memory_abilities(
                            history,
                            f"histories[{history_index}].produceHistory",
                        )
                        memory_loadout = _deck_memory_loadout(
                            history,
                            f"histories[{history_index}].produceHistory",
                        )
                        if memory_abilities is None or memory_loadout is None:
                            history_incomplete = True
                            # Keep the pair fail-closed for every later
                            # section in this same history.  Retaining only
                            # abilities here would let a following section
                            # construct an episode with a missing loadout.
                            memory_abilities = None
                            memory_loadout = None
                            continue
                    if support_catalog is None:
                        support_catalog = _deck_support_catalog(
                            history,
                            f"histories[{history_index}].produceHistory",
                        )
                        if support_catalog is None:
                            history_incomplete = True
                            continue
                    support_cards = _merge_support_cards(
                        player,
                        support_catalog,
                        f"auditions[{audition_index}].sections[{section_index}]",
                    )
                    if support_cards is None:
                        history_incomplete = True
                        continue
                    actions = tuple(
                        LeaderboardReplayAction(
                            order=order,
                            action_type=_action_type(
                                _value(row, "actionType", "ActionType")
                            ),
                            indexes=tuple(
                                _integer(value, f"actions[{order}].indexes")
                                for value in _values(
                                    _value(row, "indexes", "Indexes", default=[]),
                                    f"actions[{order}].indexes",
                                )
                            ),
                        )
                        for order, row in enumerate(action_rows)
                    )
                    history_episodes.append(
                        LeaderboardReplayEpisode(
                            produce_id=produce_id,
                            idol_card_id=idol_card_id,
                            audition_index=audition_index,
                            step_type=_typed_enum_value(
                                _value(audition, "stepType", "StepType"),
                                "audition.stepType",
                                "ProduceStepType_",
                            ),
                            step_select_number=_integer(
                                _value(
                                    audition,
                                    "stepSelectNumber",
                                    "StepSelectNumber",
                                ),
                                "audition.stepSelectNumber",
                            ),
                            rank=_integer(
                                _value(audition, "rank", "Rank"),
                                "audition.rank",
                            ),
                            terminal_score=_integer(
                                _value(audition, "score", "Score"),
                                "audition.score",
                            ),
                            stage_index=stage_index,
                            section_index=section_index,
                            app_version=_text(
                                _value(situation, "appVersion", "AppVersion"),
                                "situation.appVersion",
                            ),
                            image_version=_optional_text(
                                _value(
                                    situation, "imageVersion", "ImageVersion"
                                ),
                                "situation.imageVersion",
                            ),
                            master_version=_text(
                                _value(
                                    situation, "masterVersion", "MasterVersion"
                                ),
                                "situation.masterVersion",
                            ),
                            master_hash=_text(
                                _value(situation, "masterHash", "MasterHash"),
                                "situation.masterHash",
                            ),
                            exam_setting_id=_text(
                                _value(
                                    situation,
                                    "examSettingId",
                                    "ExamSettingId",
                                ),
                                "situation.examSettingId",
                            ),
                            plan_type=_typed_enum_value(
                                _value(stage, "planType", "PlanType"),
                                "stage.planType",
                                "ProducePlanType_",
                            ),
                            seed=_nonnegative_decimal_int(
                                _value(player, "seed", "Seed"), "player.seed"
                            ),
                            character_id=_text(
                                _value(
                                    player, "characterId", "CharacterId"
                                ),
                                "player.characterId",
                            ),
                            exam_effect_type=_typed_enum_value(
                                _value(player, "examEffectType", "ExamEffectType"),
                                "player.examEffectType",
                                "ProduceExamEffectType_",
                            ),
                            stamina=_integer(
                                _value(player, "stamina", "Stamina"),
                                "player.stamina",
                            ),
                            max_stamina=_integer(
                                _value(player, "maxStamina", "MaxStamina"),
                                "player.maxStamina",
                            ),
                            vocal=_integer(
                                _value(player, "vocal", "Vocal"),
                                "player.vocal",
                            ),
                            dance=_integer(
                                _value(player, "dance", "Dance"),
                                "player.dance",
                            ),
                            visual=_integer(
                                _value(player, "visual", "Visual"),
                                "player.visual",
                            ),
                            vocal_bonus_permil=_integer(
                                _value(
                                    player,
                                    "vocalBonusPermil",
                                    "VocalBonusPermil",
                                ),
                                "player.vocalBonusPermil",
                            ),
                            dance_bonus_permil=_integer(
                                _value(
                                    player,
                                    "danceBonusPermil",
                                    "DanceBonusPermil",
                                ),
                                "player.danceBonusPermil",
                            ),
                            visual_bonus_permil=_integer(
                                _value(
                                    player,
                                    "visualBonusPermil",
                                    "VisualBonusPermil",
                                ),
                                "player.visualBonusPermil",
                            ),
                            memory_abilities=memory_abilities,
                            memory_loadout=memory_loadout,
                            produce_cards=_rows(
                                _value(
                                    player,
                                    "produceCards",
                                    "ProduceCards",
                                    default=[],
                                ),
                                "player.produceCards",
                            ),
                            produce_items=_rows(
                                _value(
                                    player,
                                    "produceItems",
                                    "ProduceItems",
                                    default=[],
                                ),
                                "player.produceItems",
                            ),
                            produce_customize_item_ids=tuple(
                                _text(value, "produceCustomizeItemId")
                                for value in _values(
                                    _value(
                                        player,
                                        "produceCustomizeItemIds",
                                        "ProduceCustomizeItemIds",
                                        default=[],
                                    ),
                                    "player.produceCustomizeItemIds",
                                )
                            ),
                            produce_drink_ids=tuple(
                                _text(value, "produceDrinkId")
                                for value in _values(
                                    _value(
                                        player,
                                        "produceDrinkIds",
                                        "ProduceDrinkIds",
                                        default=[],
                                    ),
                                    "player.produceDrinkIds",
                                )
                            ),
                            support_cards=support_cards,
                            actions=actions,
                            # A temporary value is replaced with the
                            # history-level anonymous ID below, after all
                            # complete episodes have been collected.
                            trajectory_id="pending",
                            limit_turn=limit_turn,
                            force_end_score=force_end_score,
                            stage_context=stage_context,
                            section_context=section_context,
                        )
                    )
        if history_incomplete:
            if _normalization_stats is not None:
                _normalization_stats["dropped_incomplete_history_count"] = (
                    _normalization_stats.get("dropped_incomplete_history_count", 0)
                    + 1
                )
            continue
        trajectory_id = _trajectory_id_for_history(history_episodes)
        episodes.extend(
            replace(episode, trajectory_id=trajectory_id)
            for episode in history_episodes
        )
    if _normalization_stats is not None:
        _normalization_stats.setdefault("dropped_incomplete_history_count", 0)
    if not episodes and not _allow_empty:
        if saw_audition:
            raise _NoReplayEpisodesError(
                "ListLatestHistory response has empty history/no replay episodes"
            )
        raise _NoReplayEpisodesError(
            "ListLatestHistory response has empty history/no replay episodes"
        )
    return tuple(episodes)


def normalize_history(
    payload: Mapping[str, Any],
) -> tuple[LeaderboardReplayEpisode, ...]:
    """Project a ``Produce.History`` protobuf-JSON response to episodes.

    ``ProduceHistoryResponse`` carries one ``produceHistory`` object directly,
    whereas ``ProduceListLatestHistoryResponse`` wraps the same object in a
    ``histories`` row.  The replay payload below that wrapper is shared, so we
    normalize it through the established list implementation to keep both
    response shapes byte-for-byte equivalent after canonicalization.
    """

    root = _mapping(payload, "response")
    if any(key in root for key in ("histories", "Histories")):
        raise ValueError(
            "History response must not also contain ListLatestHistory histories"
        )
    if not any(key in root for key in ("produceHistory", "ProduceHistory")):
        raise ValueError("History response is missing produceHistory")
    history = _mapping(
        _value(root, "produceHistory", "ProduceHistory"),
        "response.produceHistory",
    )
    auditions = _rows(
        _value(history, "auditions", "Auditions", default=[]),
        "response.produceHistory.auditions",
    )
    if not auditions:
        raise ValueError(
            "History response has empty history/no replay episodes"
        )
    episodes = normalize_list_latest_history(
        {"histories": [{"produceHistory": history}]},
        _skip_missing_situation=True,
        _allow_empty=True,
    )
    if not episodes:
        raise _NoReplayEpisodesError(
            "History response is loadout-only/no replay actions: no replayable "
            "examContestSituation/actions"
        )
    return episodes


def _leaderboard_response_source_type(root: Mapping[str, Any]) -> str:
    """Return the supported response type, rejecting ambiguous roots."""

    if root.get("schema") == MODE_RANKING_QUERY_RESULT_SCHEMA:
        return MODE_RANKING_RESPONSE_SOURCE_TYPE

    has_list = any(key in root for key in ("histories", "Histories"))
    has_history = any(key in root for key in ("produceHistory", "ProduceHistory"))
    if has_list and has_history:
        raise ValueError(
            "leaderboard response ambiguously contains both histories and produceHistory"
        )
    if has_list:
        return LIST_LATEST_HISTORY_SOURCE_TYPE
    if has_history:
        return HISTORY_SOURCE_TYPE
    raise ValueError(
        "unsupported leaderboard response: expected histories or produceHistory"
    )


def _leaderboard_response_root(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Unwrap one successful authenticated queue result, if present."""

    if payload.get("schema") != LEADERBOARD_QUERY_RESULT_SCHEMA:
        return payload
    expected = {"schema", "job_id", "status", "response"}
    if set(payload) != expected:
        raise ValueError("leaderboard query result has an unexpected field set")
    if payload.get("status") != "succeeded":
        raise ValueError("leaderboard query result is not successful")
    job_id = payload.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("leaderboard query result has no job_id")
    response = _mapping(payload.get("response"), "query result response")
    # Protobuf JSON omits an empty repeated field.  This queue is bound to the
    # exact ListLatestHistory method, so a successful response containing only
    # commonResponse is an empty list response, not an unknown schema.
    if (
        not any(key in response for key in ("histories", "Histories"))
        and any(key in response for key in ("commonResponse", "CommonResponse"))
    ):
        return {**response, "histories": []}
    return response


def _mode_ranking_response_root(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Unwrap a successful mode-ranking result without guessing its fields.

    The mode capture worker has shipped both a queue-style envelope (with a
    ``response`` object) and a direct envelope whose ``RankingTop``,
    ``Ranking`` and ``History`` members live next to ``schema``.  Accept both
    wire forms, but require a successful status when one is supplied.
    """

    if payload.get("schema") != MODE_RANKING_QUERY_RESULT_SCHEMA:
        return payload
    if "result" in payload:
        raise ValueError(
            "mode ranking query result uses unsupported field 'result'; "
            "expected 'response'"
        )
    status = payload.get("status")
    if status is not None and status != "succeeded":
        raise ValueError("mode ranking query result is not successful")
    response = payload.get("response")
    if isinstance(response, Mapping):
        return response
    return payload


def _mode_section(root: Mapping[str, Any], name: str) -> Any:
    """Read a direct mode result section using protobuf name variants."""

    wanted = _name_token(name)
    for key, value in root.items():
        if _name_token(key) == wanted:
            return value
    return None


def _mode_rows(value: object, label: str) -> tuple[Mapping[str, Any], ...]:
    """Project one ranking/history section to object rows.

    This helper intentionally only unwraps known repeated-field containers;
    malformed scalar/row shapes are rejected by the caller rather than being
    silently interpreted as a one-row replay.
    """

    if isinstance(value, Mapping):
        for key in ("histories", "rankings", "ranking", "rows", "items"):
            nested = _field_by_token(value, key)
            if nested is not None:
                return _rows(nested, f"{label}.{key}")
        return (value,)
    return _rows(value, label)


def _mode_result_kind(payload: Mapping[str, Any], root: Mapping[str, Any]) -> str:
    """Infer the mode queue operation from its bounded discriminator fields."""

    raw_kind: object = None
    for container in (payload, root):
        for name in (
            "kind",
            "job_type",
            "operation",
            "endpoint",
            "source",
            "provenance",
        ):
            candidate = _field_by_token(container, name)
            if isinstance(candidate, str) and candidate:
                raw_kind = candidate
                break
            if isinstance(candidate, Mapping):
                candidate = _field_by_token(candidate, "kind", "source", "operation")
                if isinstance(candidate, str) and candidate:
                    raw_kind = candidate
                    break
        if raw_kind is not None:
            break
    token = _name_token(raw_kind)
    if "rankingtop" in token or token in {"top", "moderankingtop"}:
        return MODE_RANKING_TOP_SOURCE
    if "history" in token or token in {"produce", "producehistory"}:
        return MODE_RANKING_SOURCE
    if "ranking" in token or token in {"rank", "moderanking"}:
        return MODE_RANKING_SOURCE

    # Direct endpoint responses remain recognizable when an older producer
    # omitted ``kind`` from the envelope.
    if _field_by_token(root, "produceHistory") is not None or _field_by_token(
        root, "auditions"
    ) is not None:
        return MODE_RANKING_SOURCE
    if _field_by_token(root, "rankings") is not None:
        return MODE_RANKING_TOP_SOURCE
    if _field_by_token(root, "ranks") is not None:
        return MODE_RANKING_SOURCE
    return MODE_RANKING_SOURCE


def _mode_result_is_history_job(payload: Mapping[str, Any], root: Mapping[str, Any]) -> bool:
    """Return whether a mode envelope explicitly represents Produce.History."""

    for container in (payload, root):
        for name in ("kind", "job_type", "operation", "endpoint", "source"):
            value = _field_by_token(container, name)
            if isinstance(value, str) and "history" in _name_token(value):
                return True
    return False


def _mode_result_request(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    request = _field_by_token(payload, "request")
    return request if isinstance(request, Mapping) else {}


def _mode_provenance_values(
    payload: Mapping[str, Any],
    default: str,
) -> frozenset[str]:
    """Read bounded provenance labels from a mode queue envelope."""

    value = _field_by_token(
        payload,
        "provenance",
        "upstream_sources",
        "upstreamSources",
    )
    values: list[object]
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = list(value)
    elif isinstance(value, Mapping):
        nested = _field_by_token(value, "sources", "source", "kind")
        if isinstance(nested, str):
            values = [nested]
        elif isinstance(nested, Sequence) and not isinstance(nested, (str, bytes)):
            values = list(nested)
        else:
            values = []
    else:
        values = []
    normalized: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        token = _name_token(raw)
        if "rankingtop" in token or token in {"top", "moderankingtop"}:
            normalized.add(MODE_RANKING_TOP_SOURCE)
        elif "ranking" in token or token in {"rank", "moderanking"}:
            normalized.add(MODE_RANKING_SOURCE)
        elif "listlatesthistory" in token:
            normalized.add(LIST_LATEST_HISTORY_SOURCE)
    if not normalized and default:
        normalized.add(default)
    return frozenset(normalized)


def _mode_row_with_request(
    row: Mapping[str, Any], request: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Overlay identity-bearing request fields without mutating raw JSON."""

    # The request carries the IDs used to fetch a History row in the mode
    # ranking UI.  Overlay only known identity names; arbitrary request fields
    # must never leak into the de-identified ProduceHistory record.
    result = dict(row)
    for canonical in (
        "publicUserId",
        "userMemoryId",
        "produceGroupId",
        "idolCardId",
    ):
        if _field_by_token(result, canonical) is not None:
            continue
        value = _field_by_token(request, canonical)
        if value is not None:
            result[canonical] = value
    return result


def _mode_summary_rows(
    payload: Mapping[str, Any],
    root: Mapping[str, Any],
    kind: str,
) -> tuple[Mapping[str, Any], ...]:
    """Flatten RankingTop/Ranking response rows for cross-file correlation."""

    request = _mode_result_request(payload)
    if kind == MODE_RANKING_TOP_SOURCE:
        values = _field_by_token(root, "rankings")
        if values is None:
            values = _field_by_token(root, "topRanks")
        groups = _mode_rows(values, "RankingTop.rankings") if values is not None else ()
        result: list[Mapping[str, Any]] = []
        for group in groups:
            group_idol = _field_by_token(group, "idolCardId")
            ranks = _field_by_token(group, "topRanks")
            if ranks is None:
                ranks = _field_by_token(group, "ranks")
            if ranks is None:
                result.append(_mode_row_with_request(group, request))
                continue
            for row in _mode_rows(ranks, "RankingTop.topRanks"):
                merged = dict(row)
                if group_idol is not None and _field_by_token(merged, "idolCardId") is None:
                    merged["idolCardId"] = group_idol
                result.append(_mode_row_with_request(merged, request))
        return tuple(result)
    values = _field_by_token(root, "ranks")
    if values is None:
        values = _field_by_token(root, "ranking")
    if values is None:
        return ()
    return tuple(
        _mode_row_with_request(row, request)
        for row in _mode_rows(values, "Ranking.ranks")
    )


def _mode_history_row(value: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Return a ListLatestHistory-compatible wrapper for one mode row."""

    if isinstance(_field_by_token(value, "produceHistory"), Mapping):
        return value
    direct_history = _field_by_token(value, "history")
    if isinstance(direct_history, Mapping):
        if isinstance(_field_by_token(direct_history, "produceHistory"), Mapping):
            return direct_history
        if _field_by_token(direct_history, "auditions") is not None:
            return {"produceHistory": direct_history}
    if _field_by_token(value, "auditions") is not None:
        return {"produceHistory": value}
    return None


def _mode_result_history_entries(
    payload: Mapping[str, Any],
) -> tuple[tuple[Mapping[str, Any], frozenset[str]], ...]:
    """Extract replay rows and source labels from a mode-ranking result.

    RankingTop/Ranking are summary indexes, while History carries the replay
    body.  Their shared four-field identity links the rows.  A History row
    without an index counterpart is conservatively attributed to
    ``mode_ranking``; this preserves provenance without inventing a top-rank
    association.
    """

    root = _mode_ranking_response_root(payload)
    top = _mode_section(root, "RankingTop")
    ranking = _mode_section(root, "Ranking")
    history = _mode_section(root, "History")
    kind = _mode_result_kind(payload, root)
    if top is None and ranking is None and history is None:
        # Actual queue captures are one operation per file.  RankingTop and
        # Ranking are summary-only; History carries one direct
        # ProduceHistoryResponse.  The summary rows are retained separately by
        # the batch collector and joined there when the History file arrives.
        direct = _mode_history_row(root)
        if direct is None:
            if _mode_result_is_history_job(payload, root):
                # Keep one audit row for a malformed/incomplete explicit
                # History job; summary-only RankingTop/Ranking jobs continue
                # to return no replay rows.
                return (
                    (
                        {},
                        _mode_provenance_values(payload, kind),
                    ),
                )
            return ()
        direct = _mode_row_with_request(direct, _mode_result_request(payload))
        return (
            (
                direct,
                _mode_provenance_values(payload, kind),
            ),
        )
    if top is None and ranking is None and history is None:
        raise ValueError(
            "mode ranking result requires RankingTop, Ranking or History"
        )

    summary_sources: dict[str, set[str]] = {}
    for section, source in (
        (top, MODE_RANKING_TOP_SOURCE),
        (ranking, MODE_RANKING_SOURCE),
    ):
        if section is None:
            continue
        for index, row in enumerate(_mode_rows(section, f"{source}.rows")):
            identity, _fields = leaderboard_match_identity(row, index)
            summary_sources.setdefault(identity, set()).add(source)

            # Some captures nest the History response under the ranking row.
            nested = _mode_history_row(row)
            if nested is not None:
                # Preserve a summary-only row's source on the nested replay;
                # direct History rows below are merged by the same identity.
                summary_sources.setdefault(
                    leaderboard_match_identity(nested, index)[0], set()
                ).add(source)

    result: list[tuple[Mapping[str, Any], frozenset[str]]] = []

    def append_section(section: object, section_source: str, label: str) -> None:
        if section is None:
            return
        for index, row in enumerate(_mode_rows(section, label)):
            nested = _mode_history_row(row)
            if nested is None:
                continue
            identity = leaderboard_match_identity(nested, index)[0]
            sources = set(summary_sources.get(identity, ()))
            sources.add(section_source)
            result.append((nested, frozenset(sources)))

    # Nested replay rows are accepted from RankingTop/Ranking for captures
    # where the endpoint returned the full history inline.
    append_section(top, MODE_RANKING_TOP_SOURCE, "RankingTop.rows")
    append_section(ranking, MODE_RANKING_SOURCE, "Ranking.rows")
    append_section(history, MODE_RANKING_SOURCE, "History.rows")
    return tuple(result)


def _mode_result_summary_entries(
    payload: Mapping[str, Any],
) -> tuple[tuple[Mapping[str, Any], frozenset[str]], ...]:
    """Extract summary rows from one mode-ranking queue result."""

    root = _mode_ranking_response_root(payload)
    kind = _mode_result_kind(payload, root)
    top = _mode_section(root, "RankingTop")
    ranking = _mode_section(root, "Ranking")
    result: list[tuple[Mapping[str, Any], frozenset[str]]] = []
    if top is not None:
        rows = _mode_summary_rows(
            {**payload, "request": _mode_result_request(payload)},
            top if isinstance(top, Mapping) else {"rankings": top},
            MODE_RANKING_TOP_SOURCE,
        )
        top_sources = _mode_provenance_values(payload, MODE_RANKING_TOP_SOURCE)
        result.extend((row, top_sources) for row in rows)
    if ranking is not None:
        rows = _mode_summary_rows(
            {**payload, "request": _mode_result_request(payload)},
            ranking if isinstance(ranking, Mapping) else {"ranks": ranking},
            MODE_RANKING_SOURCE,
        )
        ranking_sources = _mode_provenance_values(payload, MODE_RANKING_SOURCE)
        result.extend((row, ranking_sources) for row in rows)
    if top is None and ranking is None and _mode_section(root, "History") is None:
        rows = _mode_summary_rows(payload, root, kind)
        result.extend((row, _mode_provenance_values(payload, kind)) for row in rows)
    return tuple(result)


def normalize_mode_ranking_query_result(
    payload: Mapping[str, Any],
) -> tuple[LeaderboardReplayEpisode, ...]:
    """Normalize all complete replay histories in a mode-ranking result.

    The function returns one or more episodes per complete History row.  It
    never returns a partial history: if any audition/section/action in one row
    is unavailable, that row is omitted as a unit.  Cross-row identity
    de-duplication is performed by the batch collector, where all source files
    are visible and provenance can be merged.
    """

    entries = _mode_result_history_entries(payload)
    if not entries:
        raise _NoReplayEpisodesError(
            "mode ranking result has no complete replay histories"
        )
    result: list[LeaderboardReplayEpisode] = []
    for row, sources in entries:
        try:
            episodes = normalize_list_latest_history({"histories": [row]})
        except _NoReplayEpisodesError:
            # Whole-history atomicity is handled by the established list
            # normalizer.  A malformed field remains a source-level error,
            # matching existing response semantics.
            continue
        result.extend(
            replace(episode, sources=tuple(sorted(sources))) for episode in episodes
        )
    if not result:
        raise _NoReplayEpisodesError(
            "mode ranking result has no complete replay histories"
        )
    return tuple(result)


def _leaderboard_query_failure_code(payload: Mapping[str, Any]) -> str | None:
    """Return a bounded queue failure code without touching response content.

    Queue result files use the same envelope for successful and failed jobs.
    A failed envelope is not leaderboard data: in directory mode it is an
    auditable skipped source, while an explicitly selected file remains a
    fail-closed input error.  Only the small ``error.code`` field is retained;
    the potentially sensitive error message is never read or copied.
    """

    if payload.get("schema") not in {
        LEADERBOARD_QUERY_RESULT_SCHEMA,
        MODE_RANKING_QUERY_RESULT_SCHEMA,
    } or payload.get("status") != "failed":
        return None
    error = payload.get("error")
    code = error.get("code") if isinstance(error, Mapping) else None
    if not isinstance(code, str) or not code or len(code) > 128:
        return FAILED_QUERY_RESULT_FALLBACK_CODE
    # The native queue's SafeError contract permits printable text, but a
    # manifest should carry an error *code*, never arbitrary message-like data.
    # Keep the protocol's machine-code alphabet and fall back for anything
    # that could smuggle whitespace, paths, or control content.
    if any(
        not (character.isascii() and (character.isalnum() or character in "-_."))
        for character in code
    ):
        return FAILED_QUERY_RESULT_FALLBACK_CODE
    if any(
        marker in code.casefold()
        for marker in ("token", "secret", "password", "cookie", "authorization", "bearer")
    ):
        return FAILED_QUERY_RESULT_FALLBACK_CODE
    return code


def _sources_include_directory(
    sources: str | Path | Sequence[str | Path],
) -> bool:
    """Return whether the caller selected at least one raw source directory."""

    if isinstance(sources, (str, Path)):
        values: tuple[str | Path, ...] = (sources,)
    elif isinstance(sources, Sequence) and not isinstance(sources, (bytes, bytearray)):
        values = tuple(sources)
    else:
        return False
    return any(isinstance(value, (str, Path)) and Path(value).is_dir() for value in values)


def normalize_leaderboard_response(
    payload: Mapping[str, Any],
) -> tuple[str, tuple[LeaderboardReplayEpisode, ...]]:
    """Normalize one supported raw response and return its source type.

    The response discriminator is deliberately strict.  An object that has
    neither supported top-level field, or has both fields, is rejected instead
    of guessing which schema the caller intended.
    """

    raw_payload = _mapping(payload, "response")
    if raw_payload.get("schema") == MODE_RANKING_QUERY_RESULT_SCHEMA:
        return MODE_RANKING_RESPONSE_SOURCE_TYPE, normalize_mode_ranking_query_result(
            raw_payload
        )
    root = _leaderboard_response_root(raw_payload)
    source_type = _leaderboard_response_source_type(root)
    if source_type == LIST_LATEST_HISTORY_SOURCE_TYPE:
        return LIST_LATEST_HISTORY_SOURCE_TYPE, normalize_list_latest_history(root)
    return HISTORY_SOURCE_TYPE, normalize_history(root)


def normalize_list_latest_history_file(
    source: str | Path,
) -> tuple[LeaderboardReplayEpisode, ...]:
    raw = _read_json_file(source)
    if not isinstance(raw, Mapping):
        raise ValueError("leaderboard response root must be an object")
    return normalize_list_latest_history(raw)


def normalize_history_file(
    source: str | Path,
) -> tuple[LeaderboardReplayEpisode, ...]:
    """Read and normalize one ``Produce.History`` response file."""

    raw = _read_json_file(source)
    if not isinstance(raw, Mapping):
        raise ValueError("leaderboard response root must be an object")
    return normalize_history(raw)


def normalize_leaderboard_response_file(
    source: str | Path,
) -> tuple[str, tuple[LeaderboardReplayEpisode, ...]]:
    """Read and normalize either supported leaderboard response file."""

    raw = _read_json_file(source)
    if not isinstance(raw, Mapping):
        raise ValueError("leaderboard response root must be an object")
    return normalize_leaderboard_response(raw)


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _trajectory_id_for_history(
    episodes: Sequence[LeaderboardReplayEpisode],
) -> str:
    """Derive one anonymous ID for one complete raw ``histories[]`` row.

    The digest is based only on the normalized, de-identified episode content
    and preserves audition/stage/section order.  It never reads profile,
    public-user, userName or source-path data.  A history-level digest is
    computed once and assigned to every episode in that history, so consumers
    can group the three auditions without relying on JSONL row positions.
    """

    if not episodes:
        raise ValueError("cannot derive a trajectory ID for an empty history")
    normalized: list[dict[str, Any]] = []
    for episode in episodes:
        payload = episode.to_dict()
        payload.pop("trajectory_id", None)
        normalized.append(payload)
    serialized = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"trajectory:{digest}"


def _canonical_episode(episode: LeaderboardReplayEpisode) -> tuple[str, str]:
    payload = episode.to_dict()
    serialized = _canonical_json(payload)
    return serialized, hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def migrate_episode_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Accept a v2 row and reject unsafe v1 row migration.

    A v1 row has no history-level trajectory ID or native stage/section
    context.  Assigning an ID from its JSONL line number (or from one episode
    alone) would silently split the three auditions of a player and is not a
    valid migration.  Callers must re-ingest the original raw response,
    which is the only source that preserves history atomicity.  Future schemas
    are rejected rather than guessed.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("leaderboard replay episode must be an object")
    schema = payload.get("schema")
    if schema == LEGACY_SCHEMA:
        raise ValueError(
            "leaderboard replay schema v1 cannot be migrated safely; "
            "re-ingest the raw response to create a history-level trajectory_id"
        )
    if schema != SCHEMA:
        raise ValueError(f"unsupported leaderboard replay episode schema: {schema!r}")
    trajectory_id = payload.get("trajectory_id")
    if not isinstance(trajectory_id, str) or not trajectory_id:
        raise ValueError("leaderboard replay v2 episode requires trajectory_id")
    value = _deidentify(payload, "episode")
    if not isinstance(value, dict):  # pragma: no cover - defensive
        raise ValueError("leaderboard replay episode did not produce an object")
    return value


def _atomic_write_text(path: Path, text: str) -> None:
    """Write UTF-8 text through a same-directory temporary file and replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _safe_output_filename(value: str | Path, label: str) -> str:
    name = str(value)
    path = Path(name)
    if not name or path.name != name or path in {Path("."), Path("..")}:
        raise ValueError(f"{label} must be a filename within the output directory")
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError(f"{label} must be a filename within the output directory")
    return name


def _dump_json_files(source_dir: Path) -> tuple[Path, ...]:
    if not source_dir.exists():
        raise FileNotFoundError(f"leaderboard dump directory does not exist: {source_dir}")
    if not source_dir.is_dir():
        raise ValueError(f"leaderboard dump source is not a directory: {source_dir}")
    if source_dir.is_symlink():
        raise ValueError("leaderboard dump directory must not be a symlink")

    files: list[Path] = []
    for path in source_dir.rglob("*"):
        if path.is_symlink():
            # A dump is expected to be a set of files produced by the bridge;
            # following links could make an ingest read unrelated credentials.
            if path.suffix.casefold() == ".json":
                raise ValueError(f"symlinked leaderboard JSON is not allowed: {path}")
            continue
        if path.is_file() and path.suffix.casefold() == ".json":
            relative = path.relative_to(source_dir)
            if len(relative.parts) != 1:
                raise ValueError(
                    "leaderboard source JSON path must be a relative filename "
                    "without separators"
                )
            files.append(path)
    if not files:
        raise ValueError(f"leaderboard dump contains no raw JSON files: {source_dir}")
    return tuple(sorted(files, key=lambda value: value.relative_to(source_dir).as_posix()))


def _source_name(path: Path) -> str:
    """Return a manifest-safe display name for one raw source path."""

    name = path.name
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"leaderboard source has an unsafe filename: {path}")
    return name


def list_leaderboard_raw_sources(
    sources: str | Path | Sequence[str | Path],
) -> tuple[LeaderboardRawSource, ...]:
    """Expand a raw dump directory or an explicit JSON input list.

    A directory contributes its direct ``*.json`` files in lexical order.
    Explicit paths are kept in caller order, which lets an outer builder use a
    stable first-seen winner when two de-identified histories share an
    identity.  No file is written or renamed by this function.  Symlinks and
    nested dump paths are rejected so an offline build cannot accidentally
    traverse an unrelated account directory.
    """

    if isinstance(sources, (str, Path)):
        values: tuple[str | Path, ...] = (sources,)
    elif isinstance(sources, Sequence) and not isinstance(sources, (bytes, bytearray)):
        values = tuple(sources)
    else:
        raise TypeError("leaderboard sources must be a path or a sequence of paths")
    if not values:
        raise ValueError("leaderboard source list must not be empty")

    result: list[LeaderboardRawSource] = []
    for value in values:
        if not isinstance(value, (str, Path)):
            raise TypeError("leaderboard source list contains a non-path value")
        path = Path(value)
        if path.is_symlink():
            raise ValueError(f"leaderboard raw source must not be a symlink: {path}")
        if path.is_dir():
            directory_files = _dump_json_files(path)
            result.extend(
                LeaderboardRawSource(file_path, _source_name(file_path))
                for file_path in directory_files
            )
            continue
        if not path.is_file():
            raise FileNotFoundError(f"leaderboard JSON does not exist: {path}")
        if path.suffix.casefold() != ".json":
            raise ValueError(f"leaderboard raw source must be a .json file: {path}")
        result.append(LeaderboardRawSource(path, _source_name(path)))
    if not result:
        raise ValueError("leaderboard source list contains no raw JSON files")
    return tuple(result)


# Short aliases make the reusable input-list boundary easy to discover from
# outer dataset code without making that code depend on a schedule module.
resolve_leaderboard_raw_sources = list_leaderboard_raw_sources
iter_leaderboard_raw_sources = list_leaderboard_raw_sources


def _history_produce_object(
    history_row: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return the ProduceHistory object carried by one ranking/history row."""

    value = _field_by_token(history_row, "produceHistory")
    if isinstance(value, Mapping):
        return value
    # A direct History response is also accepted by the mode queue worker.
    # Keep this branch after ``produceHistory`` so a ranking row that merely
    # contains a nested summary object is not mistaken for a replay.
    if _field_by_token(history_row, "auditions") is not None:
        return history_row
    nested = _field_by_token(history_row, "history")
    if isinstance(nested, Mapping):
        value = _field_by_token(nested, "produceHistory")
        if isinstance(value, Mapping):
            return value
        if _field_by_token(nested, "auditions") is not None:
            return nested
    return None


def canonical_produce_history_sha256(history: Mapping[str, Any]) -> str:
    """Hash one de-identified ``produceHistory`` using canonical JSON.

    Ranking captures can omit one or more of the four server-side identity
    fields.  This digest is the fail-closed fallback in that case.  The
    de-identification happens before hashing so private profile/user-memory
    fields cannot become part of an exported identity or manifest.
    """

    sanitized = _deidentify(history, "produceHistory")
    if not isinstance(sanitized, Mapping):  # pragma: no cover - defensive
        raise ValueError("produceHistory did not de-identify to an object")
    serialized = _canonical_json(sanitized)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# Naming aliases used by dataset callers and older experiments.
canonical_produce_history_hash = canonical_produce_history_sha256


def _ranking_identity_containers(
    history_row: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    """Return likely locations of the ranking identity fields.

    The wire responses have used both flattened ranking rows and a nested
    ``Profile``/``ProduceHistory`` shape.  Identity extraction intentionally
    remains shallow and explicit; recursively searching arbitrary payloads
    could accidentally correlate a rival/profile identifier from an
    unrelated nested object.
    """

    history = _history_produce_object(history_row)
    containers: list[Mapping[str, Any]] = [history_row]
    profile = _field_by_token(history_row, "profile")
    if isinstance(profile, Mapping):
        containers.append(profile)
    if history is not None:
        containers.append(history)
        history_profile = _field_by_token(history, "profile")
        if isinstance(history_profile, Mapping):
            containers.append(history_profile)
        memory = _field_by_token(history, "memory")
        if isinstance(memory, Mapping):
            containers.append(memory)
    nested_history = _field_by_token(history_row, "history")
    if isinstance(nested_history, Mapping) and nested_history not in containers:
        containers.append(nested_history)
    return tuple(containers)


def _identity_text_from_containers(
    containers: Sequence[Mapping[str, Any]],
    *names: str,
) -> str | None:
    for container in containers:
        value = _field_by_token(container, *names)
        if isinstance(value, str) and value:
            return value
    return None


def _leaderboard_identity_components(
    history_row: Mapping[str, Any],
) -> tuple[str | None, str | None, str | None, str | None]:
    """Return the four correlation fields without exposing them externally."""

    containers = _ranking_identity_containers(history_row)
    return tuple(
        _identity_text_from_containers(containers, name)
        for name in (
            "publicUserId",
            "userMemoryId",
            "produceGroupId",
            "idolCardId",
        )
    )  # type: ignore[return-value]


_IdentityComponents = tuple[str | None, str | None, str | None, str | None]
_IdentityPairKey = tuple[int, int, str, str]
_IDENTITY_PAIR_POSITIONS = tuple(
    (left, right)
    for left in range(4)
    for right in range(left + 1, 4)
)


def _identity_components_compatible(
    left: _IdentityComponents,
    right: _IdentityComponents,
) -> bool:
    """Return whether two partial identities satisfy the join contract."""

    overlap = sum(
        left_value is not None and right_value is not None
        for left_value, right_value in zip(left, right)
    )
    if overlap < 2:
        return False
    return not any(
        left_value is not None
        and right_value is not None
        and left_value != right_value
        for left_value, right_value in zip(left, right)
    )


class _IdentityComponentIndex:
    """Reverse index for partial leaderboard identity components.

    A compatible candidate must share two equal, non-null fields.  Indexing
    every pair of fields therefore gives an exact superset of compatible
    candidates; the small compatibility check below still enforces the
    existing no-conflict and ambiguity semantics.  The index is deliberately
    kept private because it contains raw identity values only in memory.
    """

    __slots__ = ("_by_pair",)

    def __init__(self) -> None:
        self._by_pair: dict[_IdentityPairKey, set[_IdentityComponents]] = {}

    def add(self, components: _IdentityComponents) -> None:
        """Index one component tuple, ignoring duplicate keys."""

        for left, right in _IDENTITY_PAIR_POSITIONS:
            left_value = components[left]
            right_value = components[right]
            if left_value is None or right_value is None:
                continue
            self._by_pair.setdefault(
                (left, right, left_value, right_value), set()
            ).add(components)

    def candidate_components(
        self,
        components: _IdentityComponents,
    ) -> set[_IdentityComponents]:
        """Return component keys sharing at least one exact known pair."""

        candidates: set[_IdentityComponents] = set()
        for left, right in _IDENTITY_PAIR_POSITIONS:
            left_value = components[left]
            right_value = components[right]
            if left_value is None or right_value is None:
                continue
            candidates.update(
                self._by_pair.get((left, right, left_value, right_value), ())
            )
        return candidates

    def compatible_components(
        self,
        components: _IdentityComponents,
    ) -> tuple[_IdentityComponents, ...]:
        """Return indexed keys satisfying the complete join predicate."""

        return tuple(
            candidate
            for candidate in self.candidate_components(components)
            if _identity_components_compatible(components, candidate)
        )


class _IndexedIdentityComponentsMap(dict[_IdentityComponents, set[str]]):
    """Mapping that keeps its component-key reverse index in sync."""

    __slots__ = ("index",)

    def __init__(self) -> None:
        super().__init__()
        self.index = _IdentityComponentIndex()

    def __setitem__(
        self,
        key: _IdentityComponents,
        value: set[str],
    ) -> None:
        self.index.add(key)
        super().__setitem__(key, value)

    def setdefault(
        self,
        key: _IdentityComponents,
        default: set[str] | None = None,
    ) -> set[str]:
        if key not in self:
            self[key] = set() if default is None else default
        return super().__getitem__(key)


def _identity_component_matches(
    components: _IdentityComponents,
    candidates: Mapping[_IdentityComponents, set[str]],
) -> tuple[_IdentityComponents, ...]:
    """Find compatible component keys using a map's optional reverse index."""

    index = getattr(candidates, "index", None)
    if isinstance(index, _IdentityComponentIndex):
        # A private map can only contain keys registered through __setitem__;
        # retain the membership guard for defensive compatibility with callers
        # that mutate a dict subclass through an unusual path.
        candidate_keys = (
            candidate
            for candidate in index.compatible_components(components)
            if candidate in candidates
        )
    else:
        candidate_keys = (
            candidate
            for candidate in candidates
            if _identity_components_compatible(components, candidate)
        )
    return tuple(candidate_keys)


def _compatible_identity_sources(
    components: tuple[str | None, str | None, str | None, str | None],
    candidates: Mapping[
        tuple[str | None, str | None, str | None, str | None], set[str]
    ],
) -> set[str]:
    """Join partial mode History metadata to an unambiguous ranking row."""

    matches = _identity_component_matches(components, candidates)
    if len(matches) != 1:
        return set()
    return set(candidates[matches[0]])


def leaderboard_match_identity(
    history_row: Mapping[str, Any],
    index: int = 0,
) -> tuple[str, tuple[str, str, str, str]]:
    """Return an opaque identity for one replay/ranking row.

    The primary key is ``(publicUserId, userMemoryId, produceGroupId,
    idolCardId)``.  It is never returned in clear text: callers get a
    deterministic ``match:<sha256>`` token and the key kind.  If any field is
    absent, the canonical de-identified ProduceHistory digest is used instead.
    ``index`` only affects malformed-row audit fallback and is not part of a
    valid history identity.
    """

    containers = _ranking_identity_containers(history_row)
    values = tuple(
        _identity_text_from_containers(containers, name)
        for name in (
            "publicUserId",
            "userMemoryId",
            "produceGroupId",
            "idolCardId",
        )
    )
    if all(value is not None for value in values):
        key = tuple(value for value in values if value is not None)
        serialized = json.dumps(
            {
                "public_user_id": key[0],
                "user_memory_id": key[1],
                "produce_group_id": key[2],
                "idol_card_id": key[3],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (
            "match:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            key,  # type: ignore[return-value]
        )

    history = _history_produce_object(history_row)
    if history is not None:
        try:
            digest = canonical_produce_history_sha256(history)
        except ValueError:
            pass
        else:
            return "history:" + digest, ()

    # A malformed row still gets a deterministic audit identity.  Do not hash
    # raw malformed values, since they may contain profile/user credentials.
    serialized = json.dumps(
        {"history_index": index},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "history:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest(), ()


def _raw_history_identity(history_row: Mapping[str, Any], index: int) -> str:
    """Return the de-identified cross-source identity for one history row."""

    return leaderboard_match_identity(history_row, index)[0]


def _audit_list_latest_history_drops(
    root: Mapping[str, Any],
    *,
    source_name: str,
    source_sha256: str,
) -> list[dict[str, Any]]:
    """Return path-free reason rows for incomplete ListLatestHistory rows."""

    histories = _rows(
        _value(root, "histories", "Histories", default=[]),
        "response.histories",
    )
    result: list[dict[str, Any]] = []
    for history_index, history_row in enumerate(histories):
        identity = _raw_history_identity(history_row, history_index)
        history_value = _value(
            history_row,
            "produceHistory",
            "ProduceHistory",
        )
        if not isinstance(history_value, Mapping):
            result.append(
                {
                    "source_name": source_name,
                    "sha256": source_sha256,
                    "source_type": LIST_LATEST_HISTORY_SOURCE_TYPE,
                    "history_index": history_index,
                    "history_identity": identity,
                    "reason": "missing-produce-history",
                }
            )
            continue
        try:
            normalize_list_latest_history({"histories": [history_row]})
        except _NoReplayEpisodesError:
            result.append(
                {
                    "source_name": source_name,
                    "sha256": source_sha256,
                    "source_type": LIST_LATEST_HISTORY_SOURCE_TYPE,
                    "history_index": history_index,
                    "history_identity": identity,
                    "reason": "no-replay-episodes",
                }
            )
        except ValueError as error:
            # The batch normalizer remains fail-closed for malformed source
            # content.  This audit is informational and keeps the reason
            # attached to the one history that caused the rejection.
            result.append(
                {
                    "source_name": source_name,
                    "sha256": source_sha256,
                    "source_type": LIST_LATEST_HISTORY_SOURCE_TYPE,
                    "history_index": history_index,
                    "history_identity": identity,
                    "reason": "invalid-history",
                    "detail": str(error),
                }
            )
    return result


RawHistoryValidator = Callable[[Mapping[str, Any]], str | None]


def _source_history_entries(
    payload: Mapping[str, Any],
    response_root: Mapping[str, Any],
    source_type: str,
) -> tuple[tuple[Mapping[str, Any], frozenset[str]], ...]:
    """Return replay/history rows for one raw source and their provenance."""

    if source_type == MODE_RANKING_RESPONSE_SOURCE_TYPE:
        return _mode_result_history_entries(payload)
    if source_type == LIST_LATEST_HISTORY_SOURCE_TYPE:
        history_values = _values(
            _value(response_root, "histories", "Histories", default=[]),
            "response.histories",
        )
        return tuple(
            (
                row if isinstance(row, Mapping) else {},
                frozenset({LIST_LATEST_HISTORY_SOURCE}),
            )
            for row in history_values
        )
    if source_type == HISTORY_SOURCE_TYPE:
        history = _value(response_root, "produceHistory", "ProduceHistory")
        if isinstance(history, Mapping):
            return (({"produceHistory": history}, frozenset()),)
        return ()
    raise ValueError(f"unsupported leaderboard response source type: {source_type}")


def collect_list_latest_history_sources(
    sources: str | Path | Sequence[str | Path],
    *,
    validator: RawHistoryValidator | None = None,
) -> LeaderboardRawHistoryCollection:
    """Collect complete de-identified histories from one or more raw JSONs.

    ``sources`` can be a dump directory, one JSON path, or an explicit list of
    JSON paths.  The operation is read-only.  Exact raw bytes are de-duplicated
    by SHA-256 before parsing; histories from distinct source files are then
    de-duplicated by their ranking identity (with a canonical
    ``produceHistory`` digest fallback).  ListLatestHistory, History and
    ``gkms.mode-ranking-query-result.v1`` jobs are accepted.  A history that
    lacks replayable audition data is dropped as one unit and represented in
    ``dropped`` with a stable reason.  ``validator`` is an optional pure
    history-level gate for consumers such as an outer schedule builder; return
    a short reason string to drop the whole history.  Mode-ranking summary-only
    jobs are retained as provenance/index inputs and are not treated as
    incomplete replay histories.

    Unknown top-level response shapes and malformed JSON remain source-level
    errors.  This distinction prevents a corrupt capture from silently
    producing a deceptively small dataset while still allowing legal
    loadout-only or partially replayable history rows to be audited and
    excluded individually.  When the caller selects a directory, failed
    authenticated queue result wrappers are skipped as whole files and
    recorded in ``skipped_failed_sources``; an explicitly selected failed file
    remains a hard error.
    """

    raw_sources = list_leaderboard_raw_sources(sources)
    allow_failed_query_result_skip = _sources_include_directory(sources)
    # source hash -> (source type, history identities + provenance) lets an
    # exact duplicate be accounted for without normalizing the same raw bytes
    # twice.  The function name is retained for compatibility; mode-ranking
    # and direct History sources are accepted as well.
    source_cache: dict[
        str, tuple[str, tuple[tuple[str, tuple[str, ...]], ...]]
    ] = {}
    seen_history_identities: dict[str, int] = {}
    seen_history_component_index = _IdentityComponentIndex()
    seen_history_component_owners: dict[_IdentityComponents, set[str]] = {}
    records: list[LeaderboardRawHistoryRecord] = []
    dropped: list[LeaderboardRawHistoryDrop] = []
    source_records: list[dict[str, Any]] = []
    source_hashes: set[str] = set()
    duplicate_source_count = 0
    duplicate_history_count = 0
    failed_source_codes: dict[str, str] = {}
    skipped_failed_source_records: list[dict[str, Any]] = []
    mode_summary_sources: dict[str, set[str]] = {}
    mode_summary_components: _IndexedIdentityComponentsMap = (
        _IndexedIdentityComponentsMap()
    )
    collection_provenance_sources: set[str] = set()

    for raw_source in raw_sources:
        path = raw_source.path
        data = path.read_bytes()
        if not data:
            raise ValueError(f"leaderboard JSON is empty: {path}")
        if len(data) > MAX_RAW_BYTES:
            raise ValueError(
                f"leaderboard JSON exceeds {MAX_RAW_BYTES} bytes: {path}"
            )
        source_hash = hashlib.sha256(data).hexdigest()
        if source_hash in failed_source_codes:
            if not allow_failed_query_result_skip:  # pragma: no cover - first occurrence fails below
                raise ValueError(
                    "leaderboard query result is failed: "
                    f"{failed_source_codes[source_hash]}"
                )
            duplicate_source_count += 1
            skipped_failed_source_records.append(
                {
                    "path": raw_source.name,
                    "error_code": failed_source_codes[source_hash],
                }
            )
            continue

        cached = source_cache.get(source_hash)
        if cached is not None:
            duplicate_source_count += 1
            source_type, identity_rows = cached
            identities = tuple(identity for identity, _sources in identity_rows)
            duplicate_history_count += len(identities)
            source_records.append(
                {
                    "name": raw_source.name,
                    "path": raw_source.name,
                    "sha256": source_hash,
                    "source_type": source_type,
                    "history_count": len(identities),
                    "duplicate_raw": True,
                    "duplicate_history_count": len(identities),
                    "provenance_sources": sorted(
                        {
                            source
                            for _identity, sources_for_row in identity_rows
                            for source in sources_for_row
                        }
                    ),
                }
            )
            continue

        raw = _decode_json_bytes(data, str(path))
        if not isinstance(raw, Mapping):
            raise ValueError(f"{path} root must be an object")
        failed_code = _leaderboard_query_failure_code(raw)
        if failed_code is not None:
            if not allow_failed_query_result_skip:
                # Do not expose the queue's error message (which may contain
                # server/account details) in a single-file failure.
                raise ValueError(f"leaderboard query result is failed: {failed_code}")
            failed_source_codes[source_hash] = failed_code
            source_hashes.add(source_hash)
            skipped_failed_source_records.append(
                {
                    "path": raw_source.name,
                    "error_code": failed_code,
                }
            )
            continue
        if raw.get("schema") == MODE_RANKING_QUERY_RESULT_SCHEMA:
            response_root = _mode_ranking_response_root(raw)
            source_type = MODE_RANKING_RESPONSE_SOURCE_TYPE
            source_provenance_sources: set[str] = set()
            # Summary jobs are often separate files from their History jobs.
            # Register them before processing replay rows so first-seen order
            # does not decide whether provenance is retained.
            for summary_row, summary_sources in _mode_result_summary_entries(raw):
                source_provenance_sources.update(summary_sources)
                collection_provenance_sources.update(summary_sources)
                summary_identity = leaderboard_match_identity(summary_row)[0]
                mode_summary_sources.setdefault(summary_identity, set()).update(
                    summary_sources
                )
                summary_components = _leaderboard_identity_components(summary_row)
                mode_summary_components.setdefault(summary_components, set()).update(
                    summary_sources
                )
                existing_index = seen_history_identities.get(summary_identity)
                if existing_index is not None:
                    existing = records[existing_index]
                    merged_sources = tuple(
                        sorted(set(existing.sources).union(summary_sources))
                    )
                    if merged_sources != existing.sources:
                        records[existing_index] = replace(
                            existing, sources=merged_sources
                        )
                # A History job may have been encountered before its
                # RankingTop/Ranking summary file.  Resolve only retained
                # component keys sharing an exact pair.  The helper still
                # enforces the unique-candidate rule across all summaries.
                compatible = _compatible_identity_sources(
                    summary_components,
                    mode_summary_components,
                )
                if compatible:
                    for existing_components in seen_history_component_index.compatible_components(
                        summary_components
                    ):
                        for existing_identity in seen_history_component_owners.get(
                            existing_components, ()
                        ):
                            existing_index = seen_history_identities[existing_identity]
                            existing = records[existing_index]
                            merged_sources = tuple(
                                sorted(set(existing.sources).union(compatible))
                            )
                            if merged_sources != existing.sources:
                                records[existing_index] = replace(
                                    existing, sources=merged_sources
                                )
        else:
            response_root = _leaderboard_response_root(raw)
            source_type = _leaderboard_response_source_type(response_root)
        entries = _source_history_entries(raw, response_root, source_type)
        if not entries:
            source_cache[source_hash] = (source_type, ())
            source_hashes.add(source_hash)
            source_records.append(
                {
                    "name": raw_source.name,
                    "path": raw_source.name,
                    "sha256": source_hash,
                    "source_type": source_type,
                    "history_count": 0,
                    "complete_history_count": 0,
                    "dropped_incomplete_history_count": 0,
                    "duplicate_history_count": 0,
                    "duplicate_raw": False,
                    "empty_source": True,
                    "provenance_sources": [],
                }
            )
            continue

        source_identities: list[tuple[str, tuple[str, ...]]] = []
        source_drop_count = 0
        source_duplicate_count = 0
        if source_type != MODE_RANKING_RESPONSE_SOURCE_TYPE:
            source_provenance_sources = set()
        for history_index, (history_row, provenance) in enumerate(entries):
            identity = _raw_history_identity(history_row, history_index)
            components = _leaderboard_identity_components(history_row)
            provenance = frozenset(
                set(provenance).union(mode_summary_sources.get(identity, ()))
            )
            provenance = frozenset(
                set(provenance).union(
                    _compatible_identity_sources(
                        components,
                        mode_summary_components,
                    )
                )
            )
            source_identities.append((identity, tuple(sorted(provenance))))
            source_provenance_sources.update(provenance)
            collection_provenance_sources.update(provenance)
            history_value = _history_produce_object(history_row)
            if history_value is None:
                dropped.append(
                    LeaderboardRawHistoryDrop(
                        raw_source.name,
                        source_hash,
                        source_type,
                        history_index,
                        identity,
                        "invalid-history",
                        "history row is missing produceHistory",
                        tuple(sorted(provenance)),
                    )
                )
                source_drop_count += 1
                continue

            try:
                # Normalize one wrapper at a time.  This is deliberately a
                # completeness probe only; downstream users consume the raw
                # (de-identified) history so outer schedules remain available.
                normalize_list_latest_history(
                    {"histories": [history_row]},
                )
            except _NoReplayEpisodesError:
                dropped.append(
                    LeaderboardRawHistoryDrop(
                        raw_source.name,
                        source_hash,
                        source_type,
                        history_index,
                        identity,
                        "no-replay-episodes",
                        sources=tuple(sorted(provenance)),
                    )
                )
                source_drop_count += 1
                continue
            except ValueError as error:
                # A malformed field inside one history is still isolated to
                # that history.  Top-level JSON/response-shape failures above
                # remain hard errors for the whole source file.
                dropped.append(
                    LeaderboardRawHistoryDrop(
                        raw_source.name,
                        source_hash,
                        source_type,
                        history_index,
                        identity,
                        "invalid-history",
                        str(error),
                        tuple(sorted(provenance)),
                    )
                )
                source_drop_count += 1
                continue

            sanitized_history = _deidentify(
                history_value,
                f"histories[{history_index}].produceHistory",
            )
            if not isinstance(sanitized_history, Mapping):  # pragma: no cover
                raise ValueError("de-identified produceHistory is not an object")
            if validator is not None:
                try:
                    validation_reason = validator(sanitized_history)
                except ValueError as error:
                    validation_reason = "validator-error"
                    detail = str(error)
                else:
                    detail = ""
                if validation_reason:
                    dropped.append(
                        LeaderboardRawHistoryDrop(
                            raw_source.name,
                            source_hash,
                            source_type,
                            history_index,
                            identity,
                            str(validation_reason),
                            detail,
                        )
                    )
                    source_drop_count += 1
                    continue

            if identity in seen_history_identities:
                duplicate_history_count += 1
                source_duplicate_count += 1
                existing_index = seen_history_identities[identity]
                existing = records[existing_index]
                merged_sources = tuple(
                    sorted(set(existing.sources).union(provenance))
                )
                if merged_sources != existing.sources:
                    records[existing_index] = replace(
                        existing, sources=merged_sources
                    )
                continue
            seen_history_identities[identity] = len(records)
            seen_history_component_index.add(components)
            seen_history_component_owners.setdefault(components, set()).add(identity)
            records.append(
                LeaderboardRawHistoryRecord(
                    raw_source.name,
                    source_hash,
                    source_type,
                    history_index,
                    identity,
                    dict(sanitized_history),
                    tuple(sorted(provenance)),
                )
            )

        source_cache[source_hash] = (source_type, tuple(source_identities))
        source_hashes.add(source_hash)
        source_records.append(
            {
                "name": raw_source.name,
                "path": raw_source.name,
                "sha256": source_hash,
                "source_type": source_type,
                "history_count": len(entries),
                "complete_history_count": len(entries) - source_drop_count,
                "dropped_incomplete_history_count": source_drop_count,
                "duplicate_history_count": source_duplicate_count,
                "duplicate_raw": False,
                "provenance_sources": sorted(source_provenance_sources),
            }
        )

    return LeaderboardRawHistoryCollection(
        records=tuple(records),
        dropped=tuple(dropped),
        source_files=tuple(source_records),
        source_file_count=len(raw_sources),
        unique_source_hash_count=len(source_hashes),
        duplicate_source_count=duplicate_source_count,
        duplicate_history_count=duplicate_history_count,
        skipped_failed_sources=tuple(skipped_failed_source_records),
        provenance_sources=tuple(
            sorted(collection_provenance_sources)
        ),
    )


# ``collect_list_latest_history`` is intentionally short for callers that do
# not need to spell out the response name at their outer/replay boundary.
collect_list_latest_history = collect_list_latest_history_sources


def ingest_leaderboard_dump(
    source: str | Path | Sequence[str | Path],
    output_dir: str | Path,
    *,
    episodes_filename: str | Path = DEFAULT_EPISODES_FILENAME,
    manifest_filename: str | Path = DEFAULT_MANIFEST_FILENAME,
) -> dict[str, Any]:
    """Batch-ingest a read-only dump directory or explicit JSON source list.

    Every selected ``*.json`` file is parsed and normalized before either output is
    created or replaced.  A malformed response, an empty history, or an
    unknown response shape raises ``ValueError`` and leaves the output
    directory untouched.  Structurally valid sources that contain only
    incomplete/no-replay histories are recorded in ``skipped_source_files``;
    failed authenticated queue results from a directory are recorded in
    ``skipped_failed_sources`` without inspecting their response or message;
    an explicitly selected failed result remains a hard error.
    at least one replay episode is still required for output.  Episodes are
    canonicalized after de-identification; their SHA-256 content hashes are
    used for cross-file de-duplication.  Both ``ListLatestHistory`` and
    individual ``History`` responses are accepted; each source file records
    which response shape it contained.
    """

    target_path = Path(output_dir)
    if target_path.exists() and target_path.is_symlink():
        raise ValueError("output directory must not be a symlink")
    target_dir = target_path.resolve()
    episodes_name = _safe_output_filename(episodes_filename, "episodes_filename")
    manifest_name = _safe_output_filename(manifest_filename, "manifest_filename")
    if episodes_name == manifest_name:
        raise ValueError("episodes_filename and manifest_filename must differ")

    raw_sources = list_leaderboard_raw_sources(source)
    allow_failed_query_result_skip = _sources_include_directory(source)
    # A directory input is a raw dump root; keep generated artifacts outside
    # it.  Explicit file lists may live beside their output directory, so for
    # those inputs we only reject an exact output/raw-file collision below.
    source_roots: set[Path] = set()
    source_values: tuple[str | Path, ...]
    if isinstance(source, (str, Path)):
        source_values = (source,)
    else:
        source_values = tuple(source)
    for source_value in source_values:
        if not isinstance(source_value, (str, Path)):
            raise TypeError("leaderboard source list contains a non-path value")
        source_value_path = Path(source_value)
        if source_value_path.is_dir():
            source_roots.add(source_value_path.resolve())
    for source_root in source_roots:
        try:
            target_dir.relative_to(source_root)
        except ValueError:
            continue
        raise ValueError(
            "output directory must be outside the raw leaderboard source directory"
        )
    raw_paths = {raw.path.resolve() for raw in raw_sources}
    # Retained rows are grouped by history identity, rather than episode
    # content.  A single match may have slightly different response metadata
    # in RankingTop/Ranking/History; all its replay episodes still train once.
    retained_rows: list[dict[str, Any]] = []
    retained_identity_indexes: dict[str, list[int]] = {}
    retained_component_index = _IdentityComponentIndex()
    retained_component_owners: dict[_IdentityComponents, set[str]] = {}
    trajectory_ids: set[str] = set()
    source_hashes: set[str] = set()
    source_cache: dict[
        str,
        tuple[
            str,
            tuple[
                tuple[
                    str,
                    tuple[tuple[str, str], ...],
                    tuple[str, ...],
                    tuple[str | None, str | None, str | None, str | None],
                ],
                ...,
            ] | None,
            int,
            tuple[dict[str, Any], ...],
        ],
    ] = {}
    source_records: list[dict[str, Any]] = []
    skipped_source_records: list[dict[str, Any]] = []
    skipped_failed_source_records: list[dict[str, Any]] = []
    failed_source_codes: dict[str, str] = {}
    source_types: set[str] = set()
    episodes_seen = 0
    dropped_incomplete_history_count = 0
    duplicate_episode_count = 0
    duplicate_source_count = 0
    dropped_history_records: list[dict[str, Any]] = []
    mode_summary_sources: dict[str, set[str]] = {}
    mode_summary_components: _IndexedIdentityComponentsMap = (
        _IndexedIdentityComponentsMap()
    )
    collection_provenance_sources: set[str] = set()

    def retain_history_rows(
        identity: str,
        canonical_rows: tuple[tuple[str, str], ...],
        provenance: Sequence[str],
        components: tuple[str | None, str | None, str | None, str | None]
        | None = None,
    ) -> None:
        """Retain one complete history or merge its provenance into a winner."""

        source_values = tuple(sorted(set(provenance)))
        existing_indexes = retained_identity_indexes.get(identity)
        if existing_indexes is not None:
            nonlocal_duplicate_count[0] += len(canonical_rows)
            for row_index in existing_indexes:
                payload = retained_rows[row_index]
                merged = tuple(
                    sorted(set(payload.get("sources", ())).union(source_values))
                )
                if merged:
                    payload["sources"] = list(merged)
                elif "sources" in payload:
                    payload.pop("sources", None)
            return
        indexes: list[int] = []
        for serialized, _episode_hash in canonical_rows:
            payload = json.loads(serialized)
            if not isinstance(payload, dict):  # pragma: no cover - defensive
                raise ValueError("normalized leaderboard episode is not an object")
            if source_values:
                payload["sources"] = list(source_values)
            indexes.append(len(retained_rows))
            retained_rows.append(payload)
        retained_identity_indexes[identity] = indexes
        if components is not None:
            retained_component_index.add(components)
            retained_component_owners.setdefault(components, set()).add(identity)

    nonlocal_duplicate_count = [0]

    # Read and validate the complete input set first.  In particular, output
    # creation is intentionally below this loop so a later bad file cannot
    # leave a partially ingested dataset.
    for raw_source in raw_sources:
        path = raw_source.path
        data = path.read_bytes()
        if not data:
            raise ValueError(f"leaderboard JSON is empty: {path}")
        if len(data) > MAX_RAW_BYTES:
            raise ValueError(
                f"leaderboard JSON exceeds {MAX_RAW_BYTES} bytes: {path}"
            )
        source_hash = hashlib.sha256(data).hexdigest()
        relative_path = raw_source.name
        if not relative_path or "/" in relative_path or "\\" in relative_path:
            raise ValueError(
                "leaderboard source JSON path must be a relative filename "
                "without separators"
            )
        if source_hash in failed_source_codes:
            if not allow_failed_query_result_skip:  # pragma: no cover - first occurrence fails below
                raise ValueError(
                    "leaderboard query result is failed: "
                    f"{failed_source_codes[source_hash]}"
                )
            duplicate_source_count += 1
            skipped_failed_source_records.append(
                {
                    "path": relative_path,
                    "error_code": failed_source_codes[source_hash],
                }
            )
            continue
        duplicate_raw = source_hash in source_cache
        if duplicate_raw:
            duplicate_source_count += 1
            (
                source_type,
                cached_history_rows,
                source_dropped_count,
                audit_rows,
            ) = source_cache[source_hash]
            dropped_history_records.extend(
                {
                    **row,
                    "source_name": relative_path,
                    "sha256": source_hash,
                }
                for row in audit_rows
            )
            if cached_history_rows is None:
                source_types.add(source_type)
                dropped_incomplete_history_count += source_dropped_count
                skipped_source_records.append(
                    {
                        "path": relative_path,
                        "sha256": source_hash,
                        "source_type": source_type,
                        "reason": "no-replay-episodes",
                        "dropped_incomplete_history_count": source_dropped_count,
                    }
                )
                continue
            source_types.add(source_type)
            dropped_incomplete_history_count += source_dropped_count
            if not cached_history_rows and source_dropped_count:
                skipped_source_records.append(
                    {
                        "path": relative_path,
                        "sha256": source_hash,
                        "source_type": source_type,
                        "reason": "no-replay-episodes",
                        "dropped_incomplete_history_count": source_dropped_count,
                    }
                )
            episodes_seen += sum(
                len(canonical_rows)
            for _identity, canonical_rows, _provenance, _components
            in cached_history_rows
            )
            source_provenance: set[str] = set()
            source_episode_count = 0
            for identity, canonical_rows, provenance, components in cached_history_rows:
                source_episode_count += len(canonical_rows)
                source_provenance.update(provenance)
                collection_provenance_sources.update(provenance)
                retain_history_rows(identity, canonical_rows, provenance, components)
            source_records.append(
                {
                    "path": relative_path,
                    "sha256": source_hash,
                    "source_type": source_type,
                    "episodes": source_episode_count,
                    "duplicate_raw": True,
                    "dropped_incomplete_history_count": source_dropped_count,
                    "provenance_sources": sorted(source_provenance),
                }
            )
            continue
        else:
            raw = _decode_json_bytes(data, str(path))
            if not isinstance(raw, Mapping):
                raise ValueError(f"{path} root must be an object")
            failed_code = _leaderboard_query_failure_code(raw)
            if failed_code is not None:
                if not allow_failed_query_result_skip:
                    # Keep the single-file error fail-closed and avoid copying
                    # the queue's potentially sensitive error message.
                    raise ValueError(
                        f"leaderboard query result is failed: {failed_code}"
                    )
                failed_source_codes[source_hash] = failed_code
                source_hashes.add(source_hash)
                skipped_failed_source_records.append(
                    {
                        "path": relative_path,
                        "error_code": failed_code,
                    }
                )
                continue

            if raw.get("schema") == MODE_RANKING_QUERY_RESULT_SCHEMA:
                response_root = _mode_ranking_response_root(raw)
                source_type = MODE_RANKING_RESPONSE_SOURCE_TYPE
                source_provenance: set[str] = set()
                for summary_row, summary_sources in _mode_result_summary_entries(raw):
                    summary_identity = leaderboard_match_identity(summary_row)[0]
                    mode_summary_sources.setdefault(summary_identity, set()).update(
                        summary_sources
                    )
                    summary_components = _leaderboard_identity_components(summary_row)
                    mode_summary_components.setdefault(summary_components, set()).update(
                        summary_sources
                    )
                    source_provenance.update(summary_sources)
                    collection_provenance_sources.update(summary_sources)
                    existing_indexes = retained_identity_indexes.get(summary_identity)
                    if existing_indexes is not None:
                        for row_index in existing_indexes:
                            payload = retained_rows[row_index]
                            merged = tuple(
                                sorted(
                                    set(payload.get("sources", ())).union(
                                        summary_sources
                                    )
                                )
                            )
                            if merged:
                                payload["sources"] = list(merged)
                    # Resolve only retained component keys sharing an exact
                    # pair.  The summary map's indexed compatibility query
                    # still rejects conflicting or ambiguous candidates.
                    compatible = _compatible_identity_sources(
                        summary_components,
                        mode_summary_components,
                    )
                    if compatible:
                        for existing_components in retained_component_index.compatible_components(
                            summary_components
                        ):
                            for existing_identity in retained_component_owners.get(
                                existing_components, ()
                            ):
                                for row_index in retained_identity_indexes[existing_identity]:
                                    payload = retained_rows[row_index]
                                    merged = tuple(
                                        sorted(
                                            set(payload.get("sources", ())).union(
                                                compatible
                                            )
                                        )
                                    )
                                    if merged:
                                        payload["sources"] = list(merged)
            else:
                response_root = _leaderboard_response_root(raw)
                source_type = _leaderboard_response_source_type(response_root)

            entries = _source_history_entries(raw, response_root, source_type)
            source_history_rows: list[
                tuple[
                    str,
                    tuple[tuple[str, str], ...],
                    tuple[str, ...],
                    tuple[str | None, str | None, str | None, str | None],
                ]
            ] = []
            source_dropped_count = 0
            if source_type != MODE_RANKING_RESPONSE_SOURCE_TYPE:
                source_provenance = set()
            source_audit_rows: list[dict[str, Any]] = []
            for history_index, (history_row, row_sources) in enumerate(entries):
                identity = _raw_history_identity(history_row, history_index)
                components = _leaderboard_identity_components(history_row)
                provenance = frozenset(
                    set(row_sources).union(mode_summary_sources.get(identity, ()))
                )
                provenance = frozenset(
                    set(provenance).union(
                        _compatible_identity_sources(
                            components,
                            mode_summary_components,
                        )
                    )
                )
                source_provenance.update(provenance)
                collection_provenance_sources.update(provenance)
                history_value = _history_produce_object(history_row)
                if history_value is None:
                    source_dropped_count += 1
                    source_audit_rows.append(
                        {
                            "source_name": relative_path,
                            "sha256": source_hash,
                            "source_type": source_type,
                            "history_index": history_index,
                            "history_identity": identity,
                            "reason": "missing-produce-history",
                            **({"sources": sorted(provenance)} if provenance else {}),
                        }
                    )
                    continue
                try:
                    episodes = normalize_list_latest_history(
                        {"histories": [history_row]}
                    )
                except _NoReplayEpisodesError:
                    source_dropped_count += 1
                    source_audit_rows.append(
                        {
                            "source_name": relative_path,
                            "sha256": source_hash,
                            "source_type": source_type,
                            "history_index": history_index,
                            "history_identity": identity,
                            "reason": "no-replay-episodes",
                            **({"sources": sorted(provenance)} if provenance else {}),
                        }
                    )
                    continue
                except ValueError as error:
                    source_dropped_count += 1
                    source_audit_rows.append(
                        {
                            "source_name": relative_path,
                            "sha256": source_hash,
                            "source_type": source_type,
                            "history_index": history_index,
                            "history_identity": identity,
                            "reason": "invalid-history",
                            "detail": str(error),
                            **({"sources": sorted(provenance)} if provenance else {}),
                        }
                    )
                    continue

                canonical_rows = tuple(
                    _canonical_episode(
                        replace(
                            episode,
                            sources=tuple(sorted(provenance)),
                        )
                    )
                    for episode in episodes
                )
                source_history_rows.append(
                    (
                        identity,
                        canonical_rows,
                        tuple(sorted(provenance)),
                        components,
                    )
                )

            dropped_history_records.extend(source_audit_rows)
            source_hashes.add(source_hash)
            source_types.add(source_type)
            dropped_incomplete_history_count += source_dropped_count
            source_cache[source_hash] = (
                source_type,
                tuple(source_history_rows),
                source_dropped_count,
                tuple(source_audit_rows),
            )

            if not entries:
                # RankingTop/Ranking jobs are valid summary-only inputs.  A
                # direct ListLatestHistory/History file with no rows remains a
                # skipped source, preserving the old fail-closed behavior.
                if source_type != MODE_RANKING_RESPONSE_SOURCE_TYPE:
                    skipped_source_records.append(
                        {
                            "path": relative_path,
                            "sha256": source_hash,
                            "source_type": source_type,
                            "reason": "no-replay-episodes",
                            "dropped_incomplete_history_count": source_dropped_count,
                        }
                    )
                source_records.append(
                    {
                        "path": relative_path,
                        "sha256": source_hash,
                        "source_type": source_type,
                        "episodes": 0,
                        "trajectory_ids": [],
                        "duplicate_raw": False,
                        "provenance_sources": sorted(source_provenance),
                        "summary_only": source_type == MODE_RANKING_RESPONSE_SOURCE_TYPE,
                    }
                )
                continue

            source_episode_count = sum(
                len(canonical_rows)
                for _identity, canonical_rows, _provenance, _components
                in source_history_rows
            )
            if not source_history_rows:
                skipped_source_records.append(
                    {
                        "path": relative_path,
                        "sha256": source_hash,
                        "source_type": source_type,
                        "reason": "no-replay-episodes",
                        "dropped_incomplete_history_count": source_dropped_count,
                    }
                )
            source_records.append(
                {
                    "path": relative_path,
                    "sha256": source_hash,
                    "source_type": source_type,
                    "episodes": source_episode_count,
                    "trajectory_ids": sorted(
                        {
                            payload.get("trajectory_id")
                            for _identity, canonical_rows, _provenance, _components
                            in source_history_rows
                            for serialized, _hash in canonical_rows
                            for payload in (json.loads(serialized),)
                            if isinstance(payload.get("trajectory_id"), str)
                        }
                    ),
                    "duplicate_raw": False,
                    "dropped_incomplete_history_count": source_dropped_count,
                    "provenance_sources": sorted(source_provenance),
                }
            )
            for identity, canonical_rows, provenance, components in source_history_rows:
                episodes_seen += len(canonical_rows)
                trajectory_ids.update(
                    payload.get("trajectory_id")
                    for serialized, _hash in canonical_rows
                    for payload in (json.loads(serialized),)
                    if isinstance(payload.get("trajectory_id"), str)
                )
                retain_history_rows(identity, canonical_rows, provenance, components)

    duplicate_episode_count = nonlocal_duplicate_count[0]
    row_hashes: list[str] = []
    rows_by_hash: dict[str, str] = {}
    for payload in retained_rows:
        serialized = _canonical_json(payload)
        episode_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        if episode_hash in rows_by_hash:
            # This can happen when two different identity groups contain an
            # identical canonical episode (rare but harmless); retain one
            # deterministic row while counting it as a duplicate episode.
            duplicate_episode_count += 1
            continue
        rows_by_hash[episode_hash] = serialized
        row_hashes.append(episode_hash)

    if not row_hashes:
        raise ValueError(
            "leaderboard dump has empty history/no replay episodes after skipping "
            "no-replay-episodes sources"
        )

    episodes_path = target_dir / episodes_name
    manifest_path = target_dir / manifest_name
    if episodes_path.resolve() in raw_paths or manifest_path.resolve() in raw_paths:
        raise ValueError("dataset output must not overwrite a raw leaderboard JSON")
    manifest: dict[str, Any] = {
        "schema": INGEST_MANIFEST_SCHEMA,
        "episode_schema": SCHEMA,
        "source_type": (
            next(iter(source_types))
            if len(source_types) == 1
            else "mixed"
        ),
        "source_types": sorted(source_types),
        "provenance_sources": sorted(collection_provenance_sources),
        "source_file_count": len(raw_sources),
        "unique_source_hash_count": len(source_hashes),
        "duplicate_raw_file_count": duplicate_source_count,
        "episodes_seen": episodes_seen,
        "episodes_written": len(row_hashes),
        "trajectory_count": len(trajectory_ids),
        "dropped_incomplete_history_count": dropped_incomplete_history_count,
        "duplicate_episode_count": duplicate_episode_count,
        "episode_hashes": row_hashes,
        "source_files": source_records,
        "skipped_source_file_count": len(skipped_source_records),
        "skipped_source_files": skipped_source_records,
        "skipped_failed_source_count": len(skipped_failed_source_records),
        "skipped_failed_sources": skipped_failed_source_records,
        "dropped_history_count": len(dropped_history_records),
        "dropped_history_reason_counts": dict(
            sorted(
                (
                    reason,
                    sum(
                        1
                        for row in dropped_history_records
                        if row.get("reason") == reason
                    ),
                )
                for reason in {
                    str(row.get("reason")) for row in dropped_history_records
                }
            )
        ),
        "dropped_histories": dropped_history_records,
    }
    episodes_text = "".join(
        f"{rows_by_hash[episode_hash]}\n" for episode_hash in row_hashes
    )
    manifest_text = _canonical_json(manifest) + "\n"

    # All parsing and validation succeeded.  Each file is atomically replaced;
    # no temporary file is left behind on a write failure.
    _atomic_write_text(episodes_path, episodes_text)
    _atomic_write_text(manifest_path, manifest_text)
    return manifest


# Descriptive aliases make the batch operation discoverable without changing
# the single-file normalizer's public API.
batch_ingest_leaderboard_dump = ingest_leaderboard_dump
ingest_leaderboard_dump_directory = ingest_leaderboard_dump
ingest_leaderboard_sources = ingest_leaderboard_dump


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Normalize a captured leaderboard protobuf JSON, or batch ingest "
            "a raw dump directory."
        )
    )
    parser.add_argument(
        "source",
        type=Path,
        nargs="+",
        help="one raw JSON, a raw dump directory, or multiple raw JSON files",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="batch output directory when SOURCE is a dump directory",
    )
    parser.add_argument(
        "--episodes-filename",
        default=DEFAULT_EPISODES_FILENAME,
        help="batch JSONL filename inside --output-dir",
    )
    parser.add_argument(
        "--manifest-filename",
        default=DEFAULT_MANIFEST_FILENAME,
        help="batch manifest filename inside --output-dir",
    )
    args = parser.parse_args(argv)

    source_paths: tuple[Path, ...] = tuple(args.source)
    batch_mode = len(source_paths) > 1 or any(path.is_dir() for path in source_paths)
    if args.output_dir is not None:
        batch_mode = True

    if batch_mode:
        if args.output is not None and args.output_dir is not None:
            parser.error("use either --output or --output-dir for a raw source list")
        if args.output_dir is not None:
            target_dir = args.output_dir
        elif args.output is not None and args.output.suffix.casefold() == ".jsonl":
            # Keep the old --output spelling convenient for batch callers that
            # want to choose the JSONL path explicitly.
            target_dir = args.output.parent
            args.episodes_filename = args.output.name
        elif args.output is not None:
            target_dir = args.output
        elif len(source_paths) == 1 and source_paths[0].is_dir():
            target_dir = source_paths[0].parent / f"{source_paths[0].name}-ingested"
        else:
            # Explicit files may live in a raw directory.  Put the default
            # beside that directory, never in it, so a no-flag invocation
            # cannot replace a captured JSON with a dataset artifact.
            first_parent = source_paths[0].resolve().parent
            target_dir = first_parent.parent / f"{first_parent.name}-ingested"
        manifest = ingest_leaderboard_dump(
            source_paths,
            target_dir,
            episodes_filename=args.episodes_filename,
            manifest_filename=args.manifest_filename,
        )
        print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
        return 0

    source = source_paths[0]
    source_type, episodes = normalize_leaderboard_response_file(source)
    if not episodes:
        raise ValueError(f"{source} contains an empty history or no replay episodes")
    output = (
        args.output
        if args.output is not None
        else source.with_suffix(".episodes.jsonl")
    ).resolve()
    if output == source.resolve():
        raise ValueError("output must not overwrite the raw leaderboard JSON")
    serialized = "".join(
        json.dumps(episode.to_dict(), ensure_ascii=False, separators=(",", ":"))
        + "\n"
        for episode in episodes
    )
    _atomic_write_text(output, serialized)
    print(
        json.dumps(
            {
                "source": str(source.resolve()),
                "source_type": source_type,
                "output": str(output),
                "episodes": len(episodes),
            },
            ensure_ascii=False,
        )
    )
    return 0


def ingest_main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point named for the dump-directory operation."""

    return main(argv)


__all__ = [
    "ACTION_TYPE_NAMES",
    "DEFAULT_EPISODES_FILENAME",
    "DEFAULT_MANIFEST_FILENAME",
    "HISTORY_SOURCE_TYPE",
    "INGEST_MANIFEST_SCHEMA",
    "LEGACY_SCHEMA",
    "LEADERBOARD_RESPONSE_SOURCE_TYPES",
    "LEADERBOARD_QUERY_RESULT_SCHEMA",
    "LIST_LATEST_HISTORY_SOURCE_TYPE",
    "LIST_LATEST_HISTORY_SOURCE",
    "MODE_RANKING_QUERY_RESULT_SCHEMA",
    "MODE_RANKING_PROVENANCE_SOURCES",
    "MODE_RANKING_RESPONSE_SOURCE_TYPE",
    "MODE_RANKING_SOURCE",
    "MODE_RANKING_TOP_SOURCE",
    "SOURCE_LIST_LATEST_HISTORY",
    "SOURCE_MODE_RANKING",
    "SOURCE_MODE_RANKING_TOP",
    "RAW_HISTORY_COLLECTION_SCHEMA",
    "LeaderboardRawHistoryCollection",
    "LeaderboardRawHistoryDrop",
    "LeaderboardRawHistoryRecord",
    "LeaderboardRawSource",
    "LeaderboardReplayAction",
    "LeaderboardReplayEpisode",
    "MAX_RAW_BYTES",
    "SCHEMA",
    "batch_ingest_leaderboard_dump",
    "collect_list_latest_history",
    "collect_list_latest_history_sources",
    "canonical_produce_history_sha256",
    "canonical_produce_history_hash",
    "ingest_leaderboard_dump",
    "ingest_leaderboard_dump_directory",
    "ingest_leaderboard_sources",
    "ingest_main",
    "iter_leaderboard_raw_sources",
    "list_leaderboard_raw_sources",
    "migrate_episode_payload",
    "normalize_history",
    "normalize_history_file",
    "normalize_leaderboard_response",
    "normalize_leaderboard_response_file",
    "normalize_mode_ranking_query_result",
    "normalize_list_latest_history",
    "normalize_list_latest_history_file",
    "resolve_leaderboard_raw_sources",
    "leaderboard_match_identity",
]


if __name__ == "__main__":
    raise SystemExit(main())
