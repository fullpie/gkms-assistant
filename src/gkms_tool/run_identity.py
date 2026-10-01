"""Run-scoped identity and durable active-run selection.

This module deliberately owns no gameplay state.  It only creates immutable
run manifests and points to one of them atomically, so callers can scope their
existing shadow/session/deck files without ever replacing an older run.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .master_db import DEFAULT_DATABASE, get_idol_profile
from .route_calendar import supported_produce_ids


from .application_paths import app_root, state_root
PROJECT_ROOT = app_root()
DEFAULT_RUN_ROOT = state_root() / "runs"
ACTIVE_POINTER_NAME = "active_run.json"
MANIFEST_NAME = "manifest.json"
RUN_ID_PREFIX = "run-"
LIVE_EXAM_PREFLIGHT_PUBLICATION_DIRECTORY = "live_exam_preflight_publications"
LEGACY_LIVE_EXAM_PREFLIGHT_PUBLICATION_NAME = "live_exam_preflight_publication.json"
LEGACY_LIVE_EXAM_PREFLIGHT_LOCK_NAME = ".live_exam_preflight_publication.lock"


def _timestamp(value: datetime | str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("created_at must be a non-empty ISO timestamp")
    return value


def _strict_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@dataclass(frozen=True, slots=True)
class RunIdentity:
    """Immutable identity for one Produce attempt."""

    run_id: str
    idol_card_id: str
    character_id: str
    produce_id: str
    created_at: str
    evidence: Mapping[str, Any]

    def validate(self) -> None:
        if not self.run_id.startswith(RUN_ID_PREFIX) or len(self.run_id) <= len(RUN_ID_PREFIX):
            raise ValueError("run_id is invalid")
        for name in ("idol_card_id", "character_id", "produce_id", "created_at"):
            _strict_text(getattr(self, name), name)
        if not isinstance(self.evidence, Mapping):
            raise ValueError("evidence must be a mapping")
        json.dumps(dict(self.evidence), ensure_ascii=False, allow_nan=False)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "idol_card_id": self.idol_card_id,
            "character_id": self.character_id,
            "produce_id": self.produce_id,
            "created_at": self.created_at,
            "evidence": dict(self.evidence),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RunIdentity":
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported run manifest schema")
        evidence = payload.get("evidence", {})
        if not isinstance(evidence, Mapping):
            raise ValueError("run manifest evidence is invalid")
        result = cls(
            run_id=_strict_text(payload.get("run_id"), "run_id"),
            idol_card_id=_strict_text(payload.get("idol_card_id"), "idol_card_id"),
            character_id=_strict_text(payload.get("character_id"), "character_id"),
            produce_id=_strict_text(payload.get("produce_id"), "produce_id"),
            created_at=_strict_text(payload.get("created_at"), "created_at"),
            evidence=dict(evidence),
        )
        result.validate()
        return result


@dataclass(frozen=True, slots=True)
class RunPaths:
    root: Path
    run_id: str

    @property
    def directory(self) -> Path:
        return self.root / self.run_id

    @property
    def manifest(self) -> Path:
        return self.directory / MANIFEST_NAME

    @property
    def shadow(self) -> Path:
        return self.directory / "run_shadow.json"

    @property
    def exam_session(self) -> Path:
        return self.directory / "exam_session.json"

    @property
    def audition_zone_checkpoint(self) -> Path:
        """Durable Deck/Hand/Grave/Lost lineage for the active Exam stage."""

        return self.directory / "audition_zone_checkpoint.json"

    @property
    def audition_multiplier_checkpoint(self) -> Path:
        """Run-bound direct observations of the three audition multipliers."""

        return self.directory / "audition_multiplier_checkpoint.json"

    @property
    def audition_support_runtime(self) -> Path:
        """Exact support HandAdd probabilities and current-turn use state."""

        return self.directory / "audition_support_runtime.json"

    @property
    def audition_support_runtime_evidence(self) -> Path:
        """Independent LocalSave proof for exact support upgrade permils."""

        return self.directory / "audition_support_runtime_evidence.json"

    @property
    def audition_local_save_state(self) -> Path:
        """Minimal, read-only projection of the current ExamSaveData."""

        return self.directory / "audition_local_save_state.json"

    @property
    def audition_zone_reconciliation(self) -> Path:
        """Reviewed companion correction over an immutable raw zone checkpoint."""

        return self.directory / "audition_zone_reconciliation.json"

    @property
    def audition_horizon_read(self) -> Path:
        """Latest capture-bound, read-only full-deck recommendation."""

        return self.directory / "audition_horizon_read.json"

    @property
    def audition_hand_hold_audit(self) -> Path:
        """Run-bound proof for the native ResetHand/HandHold boundary."""

        return self.directory / "audition_hand_hold_audit.json"

    @property
    def lesson_session(self) -> Path:
        return self.directory / "lesson_session.json"

    @property
    def deck_snapshot(self) -> Path:
        return self.directory / "deck_snapshot.json"

    @property
    def loadout_observation_draft(self) -> Path:
        return self.directory / "loadout_observation_draft.json"

    @property
    def loadout_snapshot(self) -> Path:
        return self.directory / "loadout_snapshot.json"

    @property
    def loadout_runtime_session(self) -> Path:
        return self.directory / "loadout_runtime_session.json"

    @property
    def live_exam_preflight_publication(self) -> Path:
        """Legacy run-wide marker; never use it as a current ready proof."""

        return self.directory / LEGACY_LIVE_EXAM_PREFLIGHT_PUBLICATION_NAME

    @property
    def live_exam_preflight_lock(self) -> Path:
        """Legacy run-wide lock retained only for migration diagnostics."""

        return self.directory / LEGACY_LIVE_EXAM_PREFLIGHT_LOCK_NAME

    @property
    def live_exam_preflight_publication_directory(self) -> Path:
        return self.directory / LIVE_EXAM_PREFLIGHT_PUBLICATION_DIRECTORY

    @property
    def live_exam_preflight_bundle_lock(self) -> Path:
        """Serialize writes to the run-wide prerequisite/supplement files."""

        return self.live_exam_preflight_publication_directory / ".bundle.lock"

    def live_exam_preflight_publication_for(
        self,
        *,
        context_id: str,
        context_digest: str,
        stage_number: int,
    ) -> Path:
        """Return the immutable marker path for one exact Exam context.

        ``context_id`` is hashed instead of placed directly in a filename.  The
        full context digest and stage number remain visible and jointly scope
        the marker, so a later Mid2/Final publication cannot replace Mid1.
        """

        if not isinstance(context_id, str) or not context_id.strip():
            raise ValueError("context_id must be non-empty text")
        if (
            not isinstance(context_digest, str)
            or len(context_digest) != 64
            or any(character not in "0123456789abcdef" for character in context_digest)
        ):
            raise ValueError("context_digest must be a lowercase SHA-256 digest")
        if (
            not isinstance(stage_number, int)
            or isinstance(stage_number, bool)
            or stage_number < 1
        ):
            raise ValueError("stage_number must be an integer >= 1")
        context_key = hashlib.sha256(context_id.encode("utf-8")).hexdigest()[:16]
        name = f"stage-{stage_number:03d}-{context_key}-{context_digest}.json"
        return self.live_exam_preflight_publication_directory / name

    def live_exam_preflight_lock_for(
        self,
        *,
        context_id: str,
        context_digest: str,
        stage_number: int,
    ) -> Path:
        marker = self.live_exam_preflight_publication_for(
            context_id=context_id,
            context_digest=context_digest,
            stage_number=stage_number,
        )
        return marker.with_name(f".{marker.name}.lock")

    @property
    def contextual_passive_session(self) -> Path:
        return self.directory / "contextual_passive_session.json"

    @property
    def initial_modifier_reconciliation(self) -> Path:
        return self.directory / "initial_modifier_reconciliation.json"

    @property
    def equipped_item_snapshot(self) -> Path:
        return self.directory / "equipped_item_snapshot.json"

    @property
    def archive(self) -> Path:
        return self.directory / "archive"


def paths_for(identity: RunIdentity, *, root: Path = DEFAULT_RUN_ROOT) -> RunPaths:
    identity.validate()
    return RunPaths(Path(root), identity.run_id)


def validate_identity_against_master(
    *,
    idol_card_id: str,
    character_id: str,
    produce_id: str,
    database: Path = DEFAULT_DATABASE,
) -> None:
    """Fail closed unless the selected idol and mode are known locally."""

    idol_card_id = _strict_text(idol_card_id, "idol_card_id")
    character_id = _strict_text(character_id, "character_id")
    produce_id = _strict_text(produce_id, "produce_id")
    profile = get_idol_profile(idol_card_id, database)
    if profile is None:
        raise ValueError(f"idol_card_id is not present in Master: {idol_card_id}")
    if profile.character_id != character_id:
        raise ValueError("idol_card_id does not belong to character_id")
    if produce_id not in supported_produce_ids():
        raise ValueError(f"produce_id is not supported: {produce_id}")


def _new_run_id() -> str:
    return RUN_ID_PREFIX + uuid.uuid4().hex


def create_run(
    *,
    idol_card_id: str,
    character_id: str,
    produce_id: str,
    evidence: Mapping[str, Any],
    root: Path = DEFAULT_RUN_ROOT,
    database: Path = DEFAULT_DATABASE,
    created_at: datetime | str | None = None,
) -> RunIdentity:
    """Create a new run and atomically make it active.

    A new UUID is always generated, including for the exact same idol/mode.
    Existing run directories are never edited here.
    """

    validate_identity_against_master(
        idol_card_id=idol_card_id,
        character_id=character_id,
        produce_id=produce_id,
        database=database,
    )
    if not isinstance(evidence, Mapping):
        raise ValueError("evidence must be a mapping")
    identity = RunIdentity(
        run_id=_new_run_id(),
        idol_card_id=idol_card_id,
        character_id=character_id,
        produce_id=produce_id,
        created_at=_timestamp(created_at),
        evidence=dict(evidence),
    )
    run_paths = paths_for(identity, root=root)
    run_paths.root.mkdir(parents=True, exist_ok=True)
    run_paths.directory.mkdir(exist_ok=False)
    _atomic_json(run_paths.manifest, identity.to_dict())
    set_active_run(identity, root=root)
    return identity


def load_run(run_id: str, *, root: Path = DEFAULT_RUN_ROOT) -> RunIdentity:
    run_id = _strict_text(run_id, "run_id")
    payload = json.loads((Path(root) / run_id / MANIFEST_NAME).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("run manifest must be an object")
    identity = RunIdentity.from_dict(payload)
    if identity.run_id != run_id:
        raise ValueError("run manifest ID does not match its directory")
    return identity


def set_active_run(identity: RunIdentity, *, root: Path = DEFAULT_RUN_ROOT) -> None:
    """Atomically switch only the pointer; prior run data remains untouched."""

    run_paths = paths_for(identity, root=root)
    if not run_paths.manifest.is_file():
        raise FileNotFoundError(f"run manifest does not exist: {run_paths.manifest}")
    stored = load_run(identity.run_id, root=root)
    if stored != identity:
        raise ValueError("active run identity differs from stored manifest")
    _atomic_json(
        Path(root) / ACTIVE_POINTER_NAME,
        {"schema_version": 1, "run_id": identity.run_id},
    )


def load_active_run(*, root: Path = DEFAULT_RUN_ROOT) -> RunIdentity | None:
    pointer = Path(root) / ACTIVE_POINTER_NAME
    if not pointer.is_file():
        return None
    payload = json.loads(pointer.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        raise ValueError("active run pointer is invalid")
    return load_run(_strict_text(payload.get("run_id"), "active run_id"), root=root)


def clear_active_run(
    *,
    expected_run_id: str | None = None,
    root: Path = DEFAULT_RUN_ROOT,
) -> bool:
    """Remove only the active pointer after an accepted terminal run.

    Per-run manifests, shadows, telemetry and archives remain untouched, so
    the identity can still be inspected or explicitly reactivated with
    :func:`set_active_run`.  ``expected_run_id`` prevents a late completion
    callback from clearing a newer run's pointer.
    """

    pointer = Path(root) / ACTIVE_POINTER_NAME
    # A brief reader can deny deletion on Windows. Retry only sharing/locking
    # violations, re-reading the identity on every attempt; never delete a later
    # run's pointer based on a read from before the wait.
    delays = (0.05, 0.10, 0.20)
    guarded_run_id = expected_run_id
    for attempt in range(len(delays) + 1):
        try:
            if not pointer.is_file():
                return False
            payload = json.loads(pointer.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
                raise ValueError("active run pointer is invalid")
            active_run_id = _strict_text(payload.get("run_id"), "active run_id")
            if guarded_run_id is None:
                guarded_run_id = active_run_id
            if active_run_id != _strict_text(guarded_run_id, "expected_run_id"):
                raise ValueError("active run pointer changed before terminal clear")
            pointer.unlink()
            return True
        except OSError as error:
            if getattr(error, "winerror", None) not in (32, 33) or attempt == len(delays):
                raise
            time.sleep(delays[attempt])


def copy_legacy_singleton(
    source: Path,
    *,
    identity: RunIdentity,
    root: Path = DEFAULT_RUN_ROOT,
    archive_name: str | None = None,
) -> Path:
    """Copy, never move or alter, an old singleton artifact into this run."""

    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(f"legacy singleton does not exist: {source}")
    run_paths = paths_for(identity, root=root)
    if not run_paths.manifest.is_file():
        raise FileNotFoundError(f"run manifest does not exist: {run_paths.manifest}")
    name = archive_name or source.name
    if Path(name).name != name or not name:
        raise ValueError("archive_name must be a basename")
    run_paths.archive.mkdir(exist_ok=True)
    destination = run_paths.archive / name
    if destination.exists():
        raise FileExistsError(f"legacy archive already exists: {destination}")
    shutil.copy2(source, destination)
    return destination
