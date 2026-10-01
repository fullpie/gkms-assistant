"""Strict, evidence-bound snapshots of the equipped Exam item sources.

This module records item identity and evidence only.  Item behavior remains in
``gkms_tool.item_rules``; this contract loads those rules and merges them for
consumers that have passed the evidence and support gates.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import uuid
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import InitVar, dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .item_rules import (
    EquippedItemRule,
    load_idol_item_id,
    load_item_rule,
    merge_equipped_item_rules,
)
from .master_db import DEFAULT_DATABASE


EQUIPPED_ITEM_SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_SCHEMA_VERSION = EQUIPPED_ITEM_SNAPSHOT_SCHEMA_VERSION
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EQUIPPED_ITEM_SNAPSHOT_PATH = (
    PROJECT_ROOT / "var" / "equipped_item_snapshot.json"
)

ORIGIN_IDOL_STATIC = "idol_static"
ORIGIN_OBSERVED_INVENTORY = "observed_inventory"
EQUIPPED_ITEM_ORIGINS = frozenset(
    {ORIGIN_IDOL_STATIC, ORIGIN_OBSERVED_INVENTORY}
)
ORIGINS = EQUIPPED_ITEM_ORIGINS

# The values are deliberately short and stable so callers can persist or log
# classifier results without depending on an enum implementation.
INVENTORY_DRINK = "drink"
INVENTORY_SUPPORTED_EXAM_ITEM = "supported_exam_item"
INVENTORY_RECOGNIZED_NON_EXAM_DEFERRED = "recognized_non_exam_deferred"
INVENTORY_UNKNOWN = "unknown"
INVENTORY_AMBIGUOUS = "ambiguous"
INVENTORY_UNSUPPORTED_EXAM_ITEM = "unsupported_exam_item"

# Descriptive aliases make the classification vocabulary discoverable without
# changing the serialized values.
RECOGNIZED_DRINK = INVENTORY_DRINK
SUPPORTED_EXAM_ITEM = INVENTORY_SUPPORTED_EXAM_ITEM
RECOGNIZED_NON_EXAM_DEFERRED = INVENTORY_RECOGNIZED_NON_EXAM_DEFERRED
UNKNOWN_NAME = INVENTORY_UNKNOWN
AMBIGUOUS_NAME = INVENTORY_AMBIGUOUS

_INVENTORY_CLASSIFICATIONS = frozenset(
    {
        INVENTORY_DRINK,
        INVENTORY_SUPPORTED_EXAM_ITEM,
        INVENTORY_RECOGNIZED_NON_EXAM_DEFERRED,
        INVENTORY_UNKNOWN,
        INVENTORY_AMBIGUOUS,
        INVENTORY_UNSUPPORTED_EXAM_ITEM,
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# The imported SQLite database contains the Japanese Master names while the
# visible run evidence uses the localized names below.  These are identity
# mappings, not OCR or fuzzy matching.  The IDs are checked against Master
# before a supported-item result is returned.
_LOCALIZED_ITEM_IDS: dict[str, tuple[str, ...]] = {
    "睡得很舒服": ("pitem_02-3-262-0",),
    "只屬於我的一番星": ("pitem_02-3-071-0",),
    "頭頂的記錄": ("pitem_00-3-054-0",),
}
_LOCALIZED_DRINK_IDS: dict[str, tuple[str, ...]] = {
    "路易波士茶": ("pdrink_02-1-006",),
}


def _strict_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _strict_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _strict_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _sha256_text(value: Any, label: str) -> str:
    value = _strict_text(value, label)
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _exact_mapping(
    payload: Mapping[str, Any], expected: frozenset[str], label: str
) -> None:
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    actual = set(payload)
    if actual != set(expected):
        missing = sorted(set(expected) - actual, key=repr)
        unknown = sorted(actual - set(expected), key=repr)
        raise ValueError(
            f"{label} fields are invalid: missing={missing} unknown={unknown}"
        )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_database(database: Path | str) -> Path:
    if not isinstance(database, (Path, str)):
        raise TypeError("database must be a path")
    return Path(database)


def _validate_source_tuple(
    sources: tuple["EquippedItemObservation", ...],
    idol_card_id: str,
    *,
    database: Path,
) -> None:
    if not isinstance(sources, tuple):
        raise TypeError("sources must be a tuple of EquippedItemObservation")
    if not sources:
        raise ValueError("sources must contain the mandatory idol item")
    if not all(isinstance(source, EquippedItemObservation) for source in sources):
        raise TypeError("sources must contain EquippedItemObservation values")

    item_ids = tuple(source.item_id for source in sources)
    if len(item_ids) != len(set(item_ids)):
        raise ValueError("Duplicate item ID in equipped item sources")
    idol_sources = tuple(
        source for source in sources if source.origin == ORIGIN_IDOL_STATIC
    )
    if len(idol_sources) != 1:
        raise ValueError("sources must contain exactly one idol_static source")
    if not database.is_file():
        raise ValueError(f"Master database does not exist: {database}")

    try:
        expected_id = load_idol_item_id(idol_card_id, database=database)
    except (KeyError, OSError, sqlite3.Error) as error:
        raise ValueError(
            f"idol_card_id cannot resolve its mandatory Master item: {idol_card_id}"
        ) from error
    if idol_sources[0].item_id != expected_id:
        raise ValueError(
            "idol_static item does not match load_idol_item_id(idol_card_id)"
        )


@dataclass(frozen=True, slots=True)
class EquippedItemObservation:
    """One immutable source row in an equipped-item snapshot."""

    schema_version: ClassVar[int] = EQUIPPED_ITEM_SNAPSHOT_SCHEMA_VERSION
    item_id: str
    origin: str
    observed_count: int | None = 1

    def __post_init__(self) -> None:
        _strict_text(self.item_id, "item_id")
        if self.origin not in EQUIPPED_ITEM_ORIGINS:
            raise ValueError(
                "origin must be exactly idol_static or observed_inventory"
            )
        if self.origin == ORIGIN_IDOL_STATIC:
            if self.observed_count is not None:
                _strict_int(self.observed_count, "observed_count", minimum=1)
                if self.observed_count != 1:
                    raise ValueError("idol_static observed_count must be exactly 1")
        else:
            _strict_int(self.observed_count, "observed_count", minimum=1)

    @property
    def count(self) -> int | None:
        """Compatibility alias for callers that call the field ``count``."""

        return self.observed_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "origin": self.origin,
            "observed_count": self.observed_count,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EquippedItemObservation":
        _exact_mapping(
            payload,
            frozenset({"item_id", "origin", "observed_count"}),
            "equipped item source",
        )
        return cls(
            item_id=_strict_text(payload["item_id"], "item_id"),
            origin=_strict_text(payload["origin"], "origin"),
            observed_count=payload["observed_count"],
        )


# Natural aliases for the same schema type.  Keeping one class prevents the
# aliases from becoming separate, subtly incompatible contracts.
EquippedItemSource = EquippedItemObservation
EquippedItemSourceObservation = EquippedItemObservation


@dataclass(frozen=True, slots=True)
class EquippedItemSnapshotResolution:
    """Non-serialized audit output for one snapshot resolution attempt."""

    item_rules: tuple[EquippedItemRule, ...]
    merged_rule: EquippedItemRule | None
    blocking_reasons: tuple[str, ...]
    evidence_valid: bool
    authoritative: bool

    @property
    def decision_ready(self) -> bool:
        return (
            self.merged_rule is not None
            and self.evidence_valid
            and self.authoritative
            and not self.blocking_reasons
        )

    @property
    def safe_to_apply(self) -> bool:
        return self.decision_ready


class EquippedItemSnapshotBlockedError(RuntimeError):
    """Raised when a caller explicitly requires a ready item snapshot."""

    def __init__(self, reasons: Iterable[str]) -> None:
        self.reasons = tuple(dict.fromkeys(reason for reason in reasons if reason))
        if not self.reasons:
            raise ValueError("blocked error requires at least one reason")
        super().__init__("equipped item snapshot blocked: " + "; ".join(self.reasons))


@dataclass(frozen=True, slots=True)
class EquippedItemSnapshot:
    """Immutable run-bound snapshot of the mandatory and observed item sources."""

    run_id: str
    loadout_snapshot_digest: str
    idol_card_id: str
    evidence_path: str
    evidence_sha256: str
    authoritative: bool
    sources: tuple[EquippedItemObservation, ...]
    schema_version: int = EQUIPPED_ITEM_SNAPSHOT_SCHEMA_VERSION
    # InitVar keeps the Master database out of the persisted contract while
    # allowing deterministic tests and callers to use a specific Master copy.
    database: InitVar[Path | str] = DEFAULT_DATABASE
    _database: Path = field(init=False, repr=False, compare=False)

    def __post_init__(self, database: Path | str) -> None:
        _strict_text(self.run_id, "run_id")
        _sha256_text(self.loadout_snapshot_digest, "loadout_snapshot_digest")
        _strict_text(self.idol_card_id, "idol_card_id")
        _strict_text(self.evidence_path, "evidence_path")
        _sha256_text(self.evidence_sha256, "evidence_sha256")
        _strict_bool(self.authoritative, "authoritative")
        _strict_int(self.schema_version, "schema_version", minimum=1)
        if self.schema_version != EQUIPPED_ITEM_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("unsupported equipped item snapshot schema version")
        database = _validate_database(database)
        object.__setattr__(self, "_database", database)
        _validate_source_tuple(
            self.sources,
            self.idol_card_id,
            database=database,
        )

    @property
    def item_sources(self) -> tuple[EquippedItemObservation, ...]:
        return self.sources

    @property
    def observations(self) -> tuple[EquippedItemObservation, ...]:
        return self.sources

    @property
    def item_ids(self) -> tuple[str, ...]:
        return tuple(source.item_id for source in self.sources)

    @property
    def idol_static_source(self) -> EquippedItemObservation:
        return next(
            source for source in self.sources if source.origin == ORIGIN_IDOL_STATIC
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "loadout_snapshot_digest": self.loadout_snapshot_digest,
            "idol_card_id": self.idol_card_id,
            "evidence_path": self.evidence_path,
            "evidence_sha256": self.evidence_sha256,
            "authoritative": self.authoritative,
            "sources": [source.to_dict() for source in self.sources],
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        database: Path | str = DEFAULT_DATABASE,
    ) -> "EquippedItemSnapshot":
        _exact_mapping(
            payload,
            frozenset(
                {
                    "schema_version",
                    "run_id",
                    "loadout_snapshot_digest",
                    "idol_card_id",
                    "evidence_path",
                    "evidence_sha256",
                    "authoritative",
                    "sources",
                }
            ),
            "equipped item snapshot",
        )
        schema_version = _strict_int(
            payload["schema_version"], "schema_version", minimum=1
        )
        if schema_version != EQUIPPED_ITEM_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("unsupported equipped item snapshot schema version")
        raw_sources = payload["sources"]
        if not isinstance(raw_sources, list) or not all(
            isinstance(source, Mapping) for source in raw_sources
        ):
            raise ValueError("sources must be an array of objects")
        return cls(
            run_id=_strict_text(payload["run_id"], "run_id"),
            loadout_snapshot_digest=_sha256_text(
                payload["loadout_snapshot_digest"], "loadout_snapshot_digest"
            ),
            idol_card_id=_strict_text(payload["idol_card_id"], "idol_card_id"),
            evidence_path=_strict_text(payload["evidence_path"], "evidence_path"),
            evidence_sha256=_sha256_text(
                payload["evidence_sha256"], "evidence_sha256"
            ),
            authoritative=_strict_bool(payload["authoritative"], "authoritative"),
            sources=tuple(
                EquippedItemObservation.from_dict(source) for source in raw_sources
            ),
            schema_version=schema_version,
            database=database,
        )

    def evidence_validation_reasons(self) -> tuple[str, ...]:
        """Return deterministic blockers without turning an archive read into a crash."""

        path = Path(self.evidence_path)
        if not path.is_file():
            return ("evidence-file-missing",)
        try:
            actual = _file_sha256(path)
        except OSError:
            return ("evidence-file-unreadable",)
        if not hmac.compare_digest(actual, self.evidence_sha256):
            return ("evidence-sha256-mismatch",)
        return ()

    @property
    def evidence_valid(self) -> bool:
        return not self.evidence_validation_reasons()

    def validate_evidence(self) -> bool:
        return self.evidence_valid

    def audit_resolution(
        self, *, database: Path | str | None = None
    ) -> EquippedItemSnapshotResolution:
        """Load every source and merge only the rules that Master resolves.

        This method intentionally does not evaluate triggers or effects.  It
        delegates all such behavior to :mod:`gkms_tool.item_rules`.
        """

        database = self._database if database is None else _validate_database(database)
        reasons = list(self.evidence_validation_reasons())
        if not self.authoritative:
            reasons.append("snapshot-not-authoritative")

        if not database.is_file():
            reasons.extend(
                f"item-missing-or-unresolvable:{source.item_id}"
                for source in self.sources
            )
            return EquippedItemSnapshotResolution(
                item_rules=(),
                merged_rule=None,
                blocking_reasons=tuple(dict.fromkeys(reasons)),
                evidence_valid=self.evidence_valid,
                authoritative=self.authoritative,
            )

        rules: list[EquippedItemRule] = []
        for source in self.sources:
            try:
                rule = load_item_rule(source.item_id, database)
            except (KeyError, OSError, sqlite3.Error, ValueError) as error:
                reasons.append(f"item-missing-or-unresolvable:{source.item_id}")
                # Keep the exception text out of the contract: it can contain
                # database details and is not stable across Master versions.
                del error
                continue
            if rule.id != source.item_id:
                reasons.append(f"item-id-mismatch:{source.item_id}")
                continue
            rules.append(rule)
            if rule.unsupported_rules:
                reasons.extend(
                    f"unsupported-item-rule:{source.item_id}:{unsupported}"
                    for unsupported in rule.unsupported_rules
                )
            if not rule.enchantments:
                reasons.append(f"item-has-no-exam-enchantment:{source.item_id}")

        merged: EquippedItemRule | None = None
        if len(rules) == len(self.sources):
            try:
                merged = merge_equipped_item_rules(
                    tuple(rules),
                    id=f"{self.run_id}:equipped-items",
                    name="Current equipped Exam items",
                )
            except ValueError:
                reasons.append("equipped-item-rule-merge-failed")

        return EquippedItemSnapshotResolution(
            item_rules=tuple(rules),
            merged_rule=merged,
            blocking_reasons=tuple(dict.fromkeys(reasons)),
            evidence_valid=self.evidence_valid,
            authoritative=self.authoritative,
        )

    @property
    def decision_ready(self) -> bool:
        return self.audit_resolution().decision_ready

    def is_decision_ready(self) -> bool:
        return self.decision_ready

    @property
    def merged_rule(self) -> EquippedItemRule | None:
        return self.audit_resolution().merged_rule

    @property
    def resolved_rule(self) -> EquippedItemRule | None:
        return self.merged_rule

    def resolve(
        self, *, database: Path | str | None = None
    ) -> EquippedItemRule | None:
        return self.audit_resolution(database=database).merged_rule

    def require_decision_ready(
        self, *, database: Path | str | None = None
    ) -> EquippedItemRule:
        resolution = self.audit_resolution(database=database)
        if not resolution.decision_ready:
            raise EquippedItemSnapshotBlockedError(resolution.blocking_reasons)
        assert resolution.merged_rule is not None
        return resolution.merged_rule


def build_equipped_item_snapshot(
    *,
    run_id: str,
    loadout_snapshot_digest: str,
    idol_card_id: str,
    evidence_path: str,
    evidence_sha256: str,
    authoritative: bool,
    sources: Iterable[EquippedItemObservation],
    database: Path | str = DEFAULT_DATABASE,
) -> EquippedItemSnapshot:
    return EquippedItemSnapshot(
        run_id=run_id,
        loadout_snapshot_digest=loadout_snapshot_digest,
        idol_card_id=idol_card_id,
        evidence_path=evidence_path,
        evidence_sha256=evidence_sha256,
        authoritative=authoritative,
        sources=tuple(sources),
        database=database,
    )


make_equipped_item_snapshot = build_equipped_item_snapshot


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
    except sqlite3.Error:
        return set()


def _master_item_rows(
    database: Path | str,
) -> tuple[tuple[str, str, bool | None], ...]:
    database = _validate_database(database)
    if not database.is_file():
        return ()
    try:
        with closing(sqlite3.connect(database)) as connection:
            columns = _table_columns(connection, "produce_item")
            if not {"id", "name"}.issubset(columns):
                return ()
            has_exam_flag = "is_exam_effect" in columns
            select = "id, name, is_exam_effect" if has_exam_flag else "id, name"
            rows = connection.execute(
                f"SELECT {select} FROM produce_item ORDER BY name, id"
            ).fetchall()
    except (OSError, sqlite3.Error):
        return ()
    result: list[tuple[str, str, bool | None]] = []
    for row in rows:
        if not isinstance(row[0], str) or not row[0] or not isinstance(row[1], str):
            continue
        if not row[1].strip():
            continue
        flag: bool | None
        if has_exam_flag:
            if row[2] in (0, 1):
                flag = bool(row[2])
            else:
                flag = None
        else:
            flag = None
        result.append((row[0], row[1], flag))
    return tuple(result)


def _produce_drink_yaml_path() -> Path:
    return PROJECT_ROOT / "_research" / "gakumasu-diff" / "ProduceDrink.yaml"


def _yaml_drink_rows() -> tuple[tuple[str, str], ...]:
    """Read the optional raw Master drink table without adding a dependency."""

    path = _produce_drink_yaml_path()
    if not path.is_file():
        return ()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return ()
    rows: list[tuple[str, str]] = []
    current_id: str | None = None
    for line in lines:
        if line.startswith("- id: "):
            current_id = line.removeprefix("- id: ").strip()
            continue
        if current_id is None or not re.match(r"^  name:\s*", line):
            continue
        name = line.split(":", 1)[1].strip()
        if len(name) >= 2 and name[0] == name[-1] and name[0] in "'\"":
            name = name[1:-1]
        if name:
            rows.append((current_id, name))
        current_id = None
    return tuple(rows)


def _master_drink_rows(
    database: Path | str,
) -> tuple[tuple[str, str], ...]:
    database = _validate_database(database)
    rows: list[tuple[str, str]] = []
    if database.is_file():
        try:
            with closing(sqlite3.connect(database)) as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                if "produce_drink" in tables:
                    columns = _table_columns(connection, "produce_drink")
                    name_column = next(
                        (
                            column
                            for column in ("name", "display_name")
                            if column in columns
                        ),
                        None,
                    )
                    if name_column is not None and "id" in columns:
                        rows.extend(
                            (str(row[0]), str(row[1]))
                            for row in connection.execute(
                                f"SELECT id, {name_column} FROM produce_drink "
                                f"ORDER BY {name_column}, id"
                            ).fetchall()
                            if isinstance(row[0], str)
                            and row[0]
                            and isinstance(row[1], str)
                            and row[1].strip()
                        )
        except (OSError, sqlite3.Error):
            rows = []
    if rows:
        return tuple(rows)
    return _yaml_drink_rows()


def _deduplicate_rows(
    rows: Iterable[tuple[str, str, bool | None]],
) -> tuple[tuple[str, str, bool | None], ...]:
    seen: set[tuple[str, str, bool | None]] = set()
    result: list[tuple[str, str, bool | None]] = []
    for row in rows:
        if row not in seen:
            seen.add(row)
            result.append(row)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class InventoryClassification:
    """Deterministic Master-backed classification of one inventory display name."""

    name: str
    classification: str
    item_ids: tuple[str, ...] = ()
    drink_ids: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        _strict_text(self.name, "inventory name")
        if self.classification not in _INVENTORY_CLASSIFICATIONS:
            raise ValueError("invalid inventory classification")
        if not isinstance(self.item_ids, tuple) or not all(
            isinstance(item_id, str) and item_id.strip() for item_id in self.item_ids
        ):
            raise TypeError("item_ids must be a tuple of non-blank strings")
        if len(self.item_ids) != len(set(self.item_ids)):
            raise ValueError("item_ids must not contain duplicates")
        if not isinstance(self.drink_ids, tuple) or not all(
            isinstance(drink_id, str) and drink_id.strip()
            for drink_id in self.drink_ids
        ):
            raise TypeError("drink_ids must be a tuple of non-blank strings")
        if len(self.drink_ids) != len(set(self.drink_ids)):
            raise ValueError("drink_ids must not contain duplicates")
        if self.classification == INVENTORY_DRINK and len(self.drink_ids) != 1:
            raise ValueError("a recognized drink must have exactly one drink ID")
        if self.classification == INVENTORY_SUPPORTED_EXAM_ITEM and len(
            self.item_ids
        ) != 1:
            raise ValueError("a supported exam item must have exactly one item ID")

    @property
    def kind(self) -> str:
        return self.classification

    @property
    def category(self) -> str:
        return self.classification

    @property
    def item_id(self) -> str | None:
        return self.item_ids[0] if len(self.item_ids) == 1 else None

    @property
    def drink_id(self) -> str | None:
        return self.drink_ids[0] if len(self.drink_ids) == 1 else None

    @property
    def is_snapshot_candidate(self) -> bool:
        return self.classification == INVENTORY_SUPPORTED_EXAM_ITEM

    def to_observation(self, observed_count: int = 1) -> EquippedItemObservation:
        if not self.is_snapshot_candidate or self.item_id is None:
            raise ValueError(
                "only an unambiguous supported exam item can become an observation"
            )
        return EquippedItemObservation(
            item_id=self.item_id,
            origin=ORIGIN_OBSERVED_INVENTORY,
            observed_count=_strict_int(observed_count, "observed_count", minimum=1),
        )

    as_observation = to_observation


def _localized_item_rows(
    name: str,
    item_rows: tuple[tuple[str, str, bool | None], ...],
) -> tuple[tuple[str, str, bool | None], ...]:
    ids = _LOCALIZED_ITEM_IDS.get(name, ())
    if not ids:
        return ()
    return _deduplicate_rows(
        row for row in item_rows if row[0] in ids
    )


def _classify_item_rows(
    name: str,
    rows: tuple[tuple[str, str, bool | None], ...],
    database: Path | str,
) -> InventoryClassification:
    if len(rows) > 1:
        return InventoryClassification(
            name,
            INVENTORY_AMBIGUOUS,
            item_ids=tuple(row[0] for row in rows),
            reason="Master contains multiple item IDs for this name",
        )
    if not rows:
        return InventoryClassification(name, INVENTORY_UNKNOWN)

    item_id, _master_name, is_exam_effect = rows[0]
    if is_exam_effect is False:
        return InventoryClassification(
            name,
            INVENTORY_RECOGNIZED_NON_EXAM_DEFERRED,
            item_ids=(item_id,),
            reason="recognized Master produce item is not an Exam effect",
        )

    try:
        rule = load_item_rule(item_id, _validate_database(database))
    except (KeyError, OSError, sqlite3.Error, ValueError):
        return InventoryClassification(
            name,
            INVENTORY_UNSUPPORTED_EXAM_ITEM,
            item_ids=(item_id,),
            reason="Master item rule cannot be resolved",
        )
    if not rule.enchantments or rule.unsupported_rules:
        return InventoryClassification(
            name,
            INVENTORY_UNSUPPORTED_EXAM_ITEM,
            item_ids=(item_id,),
            reason="Master item has no fully supported Exam enchantment chain",
        )
    return InventoryClassification(
        name,
        INVENTORY_SUPPORTED_EXAM_ITEM,
        item_ids=(item_id,),
        reason="recognized Master Exam item",
    )


def classify_inventory_name(
    name: str,
    *,
    database: Path | str = DEFAULT_DATABASE,
) -> InventoryClassification:
    """Classify an exact inventory name using local Master data.

    No OCR, fuzzy matching, or substring matching is performed.  An unknown
    or ambiguous result carries no usable observation conversion.
    """

    name = _strict_text(name, "inventory name")
    database = _validate_database(database)
    item_rows = _master_item_rows(database)
    item_matches = tuple(row for row in item_rows if row[1] == name)
    localized_matches = _localized_item_rows(name, item_rows)
    if localized_matches:
        item_matches = _deduplicate_rows((*item_matches, *localized_matches))

    drink_rows = _master_drink_rows(database)
    drink_matches = tuple(row for row in drink_rows if row[1] == name)
    localized_drink_ids = _LOCALIZED_DRINK_IDS.get(name, ())
    if localized_drink_ids:
        drink_matches = tuple(
            dict.fromkeys(
                (*drink_matches, *(
                    (drink_id, name) for drink_id in localized_drink_ids
                ))
            )
        )

    if item_matches and drink_matches:
        return InventoryClassification(
            name,
            INVENTORY_AMBIGUOUS,
            item_ids=tuple(row[0] for row in item_matches),
            drink_ids=tuple(row[0] for row in drink_matches),
            reason="name matches both Master produce items and drinks",
        )
    if len(drink_matches) > 1:
        return InventoryClassification(
            name,
            INVENTORY_AMBIGUOUS,
            drink_ids=tuple(row[0] for row in drink_matches),
            reason="Master contains multiple drink IDs for this name",
        )
    if drink_matches:
        return InventoryClassification(
            name,
            INVENTORY_DRINK,
            drink_ids=(drink_matches[0][0],),
            reason="recognized Master drink",
        )
    if item_matches:
        return _classify_item_rows(name, item_matches, database)
    return InventoryClassification(name, INVENTORY_UNKNOWN)


classify_inventory_item = classify_inventory_name
classify_inventory = classify_inventory_name


def classify_inventory_names(
    names: Iterable[str],
    *,
    database: Path | str = DEFAULT_DATABASE,
) -> tuple[InventoryClassification, ...]:
    return tuple(
        classify_inventory_name(name, database=database) for name in names
    )


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                )
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def save_equipped_item_snapshot(
    snapshot: EquippedItemSnapshot,
    path: Path = DEFAULT_EQUIPPED_ITEM_SNAPSHOT_PATH,
) -> None:
    if not isinstance(snapshot, EquippedItemSnapshot):
        raise TypeError("snapshot must be EquippedItemSnapshot")
    _atomic_write_json(Path(path), snapshot.to_dict())


def load_equipped_item_snapshot(
    path: Path = DEFAULT_EQUIPPED_ITEM_SNAPSHOT_PATH,
    *,
    database: Path | str = DEFAULT_DATABASE,
) -> EquippedItemSnapshot | None:
    path = Path(path)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("equipped item snapshot root must be a JSON object")
    return EquippedItemSnapshot.from_dict(payload, database=database)


save_snapshot = save_equipped_item_snapshot
load_snapshot = load_equipped_item_snapshot


def file_sha256(path: Path) -> str:
    """Return the lowercase SHA-256 used by snapshot evidence fields."""

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    return _file_sha256(path)
