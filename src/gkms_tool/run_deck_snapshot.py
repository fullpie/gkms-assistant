"""Run-scoped, screenshot-backed authoritative deck snapshots."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from .deck_snapshot import (
    DEFAULT_AUTHORITATIVE_CONFIDENCE,
    DeckReconciliation,
    DeckSnapshot,
    reconcile_deck,
)
from .master_db import DEFAULT_DATABASE
from .run_identity import DEFAULT_RUN_ROOT, RunIdentity, paths_for


RUN_DECK_SNAPSHOT_SCHEMA_VERSION = 2
CardExists = Callable[[str, int], bool]


def _timestamp(value: datetime | str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str) or not value:
        raise ValueError("captured_at must be a non-empty timestamp")
    return value


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _master_card_exists(card_id: str, upgrade: int, database: Path) -> bool:
    if not database.is_file():
        return False
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT 1 FROM card WHERE id = ? AND upgrade_count = ?", (card_id, upgrade)
        ).fetchone()
    return row is not None


@dataclass(frozen=True, slots=True)
class RunDeckSnapshot:
    run_id: str
    captured_at: str
    evidence_path: str
    snapshot: DeckSnapshot
    evidence_sha256: str | None = None

    @property
    def minimum_confidence(self) -> float:
        return self.snapshot.minimum_confidence

    def to_dict(self) -> dict[str, object]:
        if not self.run_id or not self.evidence_path:
            raise ValueError("run deck snapshot identity/evidence is incomplete")
        self.snapshot.validate()
        if self.evidence_sha256 is not None and (
            len(self.evidence_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.evidence_sha256)
        ):
            raise ValueError("run deck snapshot evidence_sha256 is invalid")
        payload: dict[str, object] = {
            "schema_version": (
                RUN_DECK_SNAPSHOT_SCHEMA_VERSION
                if self.evidence_sha256 is not None
                else 1
            ),
            "run_id": self.run_id,
            "captured_at": self.captured_at,
            "evidence_path": self.evidence_path,
            "minimum_confidence": self.minimum_confidence,
            "snapshot": self.snapshot.to_dict(),
        }
        if self.evidence_sha256 is not None:
            payload["evidence_sha256"] = self.evidence_sha256
        return payload

    @property
    def evidence_valid(self) -> bool:
        if self.evidence_sha256 is None:
            return False
        path = Path(self.evidence_path)
        if not path.is_file():
            return False
        try:
            actual = _file_sha256(path)
        except OSError:
            return False
        return hmac.compare_digest(actual, self.evidence_sha256)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "RunDeckSnapshot":
        version = payload.get("schema_version")
        if version not in {1, RUN_DECK_SNAPSHOT_SCHEMA_VERSION}:
            raise ValueError("unsupported run deck snapshot schema")
        expected = {
            "schema_version", "run_id", "captured_at", "evidence_path",
            "minimum_confidence", "snapshot",
        }
        if version == RUN_DECK_SNAPSHOT_SCHEMA_VERSION:
            expected.add("evidence_sha256")
        if set(payload) != expected:
            raise ValueError("run deck snapshot fields are invalid")
        raw_snapshot = payload.get("snapshot")
        if not isinstance(raw_snapshot, Mapping):
            raise ValueError("run deck snapshot body is invalid")
        result = cls(
            run_id=str(payload.get("run_id", "")),
            captured_at=str(payload.get("captured_at", "")),
            evidence_path=str(payload.get("evidence_path", "")),
            snapshot=DeckSnapshot.from_dict(raw_snapshot),
            evidence_sha256=(
                None
                if version == 1
                else str(payload.get("evidence_sha256", ""))
            ),
        )
        serialized = result.to_dict()
        confidence = payload.get("minimum_confidence")
        if confidence != serialized["minimum_confidence"]:
            raise ValueError("run deck snapshot confidence is inconsistent")
        return result


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def commit_run_deck_snapshot(
    *,
    identity: RunIdentity,
    snapshot: DeckSnapshot,
    evidence_path: str | Path,
    root: Path = DEFAULT_RUN_ROOT,
    database: Path = DEFAULT_DATABASE,
    confidence_threshold: float = DEFAULT_AUTHORITATIVE_CONFIDENCE,
    captured_at: datetime | str | None = None,
    card_exists: CardExists | None = None,
) -> RunDeckSnapshot:
    """Validate then atomically replace this run's current full-deck snapshot."""

    identity.validate()
    snapshot.validate()
    if not snapshot.is_authoritative(confidence_threshold=confidence_threshold):
        raise ValueError("deck snapshot is incomplete, unresolved, empty, or low-confidence")
    source = Path(evidence_path)
    if not source.is_file():
        raise FileNotFoundError(f"deck snapshot evidence does not exist: {source}")
    exists = card_exists or (lambda card_id, upgrade: _master_card_exists(card_id, upgrade, database))
    unknown = [f"{card.card_id}@{card.upgrade}" for card in snapshot.cards if not exists(card.card_id, card.upgrade)]
    if unknown:
        raise ValueError("deck snapshot has unknown cards: " + ", ".join(unknown))
    paths = paths_for(identity, root=root)
    if not paths.manifest.is_file():
        raise FileNotFoundError("run manifest does not exist")
    record = RunDeckSnapshot(
        identity.run_id,
        _timestamp(captured_at),
        str(source),
        snapshot,
        _file_sha256(source),
    )
    _atomic_json(paths.deck_snapshot, record.to_dict())
    return record


def load_run_deck_snapshot(
    identity: RunIdentity, *, root: Path = DEFAULT_RUN_ROOT
) -> RunDeckSnapshot | None:
    identity.validate()
    path = paths_for(identity, root=root).deck_snapshot
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("run deck snapshot must be an object")
    record = RunDeckSnapshot.from_dict(payload)
    if record.run_id != identity.run_id:
        raise ValueError("run deck snapshot belongs to a different run")
    return record


def diff_run_deck_snapshots(
    previous: RunDeckSnapshot | None,
    current: RunDeckSnapshot,
    *,
    confidence_threshold: float = DEFAULT_AUTHORITATIVE_CONFIDENCE,
) -> DeckReconciliation:
    """Compare two authoritative snapshots; upgrades remain distinct keys."""

    if previous is not None and previous.run_id != current.run_id:
        raise ValueError("cannot compare deck snapshots from different runs")
    expected = () if previous is None else previous.snapshot.cards
    return reconcile_deck(expected, current.snapshot, confidence_threshold=confidence_threshold)
