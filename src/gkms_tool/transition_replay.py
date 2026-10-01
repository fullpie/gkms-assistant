"""Durable, project-local records for screenshot-verified transitions.

The replay store is intentionally independent from recognition and execution.
Callers provide the state snapshots, semantic phase trace, identifiers, and any
screenshots that were captured while resolving one action.  Both successful
and failed/unknown transitions use the same schema so later audits and rule
calibration do not lose negative evidence.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TRANSITION_REPLAY_ROOT = PROJECT_ROOT / "var" / "transition_replays"
TRANSITION_REPLAY_SCHEMA_VERSION = 1

_RECORD_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_MISSING = object()
_OBSERVATION_METADATA_KEY = "_observation"


@dataclass(frozen=True, slots=True)
class TransitionReplayIds:
    """Master identifiers involved in one transition.

    Every category is plural because a trigger can involve a played card,
    support passives, memories, items, gimmicks, and several chained effects.
    Empty categories are still serialized to keep the replay schema stable.
    """

    card_ids: tuple[str, ...] = ()
    item_ids: tuple[str, ...] = ()
    memory_ids: tuple[str, ...] = ()
    support_ids: tuple[str, ...] = ()
    gimmick_ids: tuple[str, ...] = ()
    effect_ids: tuple[str, ...] = ()
    trigger_ids: tuple[str, ...] = ()
    status_enchant_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for category, values in self.to_dict().items():
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"{category} must contain non-empty strings")

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "card_ids": list(self.card_ids),
            "item_ids": list(self.item_ids),
            "memory_ids": list(self.memory_ids),
            "support_ids": list(self.support_ids),
            "gimmick_ids": list(self.gimmick_ids),
            "effect_ids": list(self.effect_ids),
            "trigger_ids": list(self.trigger_ids),
            "status_enchant_ids": list(self.status_enchant_ids),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "TransitionReplayIds":
        known = {
            "card_ids",
            "item_ids",
            "memory_ids",
            "support_ids",
            "gimmick_ids",
            "effect_ids",
            "trigger_ids",
            "status_enchant_ids",
        }
        unknown = set(payload) - known
        if unknown:
            raise ValueError(f"unknown replay ID categories: {sorted(unknown)}")

        def values_for(name: str) -> tuple[str, ...]:
            raw = payload.get(name, ())
            if isinstance(raw, str):
                return (raw,)
            if not isinstance(raw, Sequence):
                raise ValueError(f"{name} must be a string sequence")
            return tuple(str(value) for value in raw)

        return cls(**{name: values_for(name) for name in sorted(known)})


@dataclass(frozen=True, slots=True)
class TransitionReplayEvidence:
    """Screenshot sources captured across one card/action resolution."""

    pre: object | None = None
    preview: object | None = None
    resolving: tuple[object, ...] = ()
    settled: tuple[object, ...] = ()


@dataclass(frozen=True, slots=True)
class SavedTransitionReplay:
    record_id: str
    directory: Path
    json_path: Path
    payload: Mapping[str, Any]


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("replay values cannot contain NaN or infinity")
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return _iso_timestamp(value)
    if isinstance(value, Enum):
        return _json_safe(value.value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_safe(to_dict())
    if is_dataclass(value) and not isinstance(value, type):
        # Walk fields ourselves instead of using dataclasses.asdict().  The
        # latter deep-copies leaf values before we can normalize them, which
        # fails for intentionally immutable values such as Plan3Session's
        # MappingProxyType context.  Recursive normalization also lets nested
        # domain objects retain their own versioned ``to_dict`` contract.
        return {
            item.name: _json_safe(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("replay mapping keys must be strings")
            result[key] = _json_safe(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_json_safe(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
    raise TypeError(f"unsupported replay value: {type(value).__name__}")


def _iso_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_created_at(value: datetime | str | None) -> str:
    if value is None:
        return _iso_timestamp(datetime.now(timezone.utc))
    if isinstance(value, datetime):
        return _iso_timestamp(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("created_at must be a non-empty ISO timestamp")
    return value


def _new_record_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{uuid.uuid4().hex[:10]}"


def _validate_record_id(record_id: str) -> None:
    if not _RECORD_ID_PATTERN.fullmatch(record_id) or record_id in {".", ".."}:
        raise ValueError("record_id contains unsafe path characters")


def _pointer(path: tuple[str, ...]) -> str:
    if not path:
        return ""
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in path)


def _append_delta(
    changes: list[dict[str, Any]],
    path: tuple[str, ...],
    before: Any,
    after: Any,
) -> None:
    if before is _MISSING:
        changes.append({"path": _pointer(path), "kind": "added", "after": after})
        return
    if after is _MISSING:
        changes.append({"path": _pointer(path), "kind": "removed", "before": before})
        return
    entry: dict[str, Any] = {
        "path": _pointer(path),
        "kind": "changed",
        "before": before,
        "after": after,
    }
    numeric = (
        isinstance(before, (int, float))
        and not isinstance(before, bool)
        and isinstance(after, (int, float))
        and not isinstance(after, bool)
    )
    if numeric:
        entry["delta"] = after - before
    changes.append(entry)


def _diff(before: Any, after: Any, path: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for key in sorted(set(before) | set(after)):
            before_value = before.get(key, _MISSING)
            after_value = after.get(key, _MISSING)
            if before_value is _MISSING or after_value is _MISSING:
                _append_delta(changes, (*path, key), before_value, after_value)
            else:
                changes.extend(_diff(before_value, after_value, (*path, key)))
        return changes
    if before != after:
        _append_delta(changes, path, before, after)
    return changes


def compute_transition_delta(
    before: Any,
    predicted: Any | None,
    observed: Any | None,
) -> dict[str, list[dict[str, Any]] | None]:
    """Return audit-friendly state differences for one transition.

    Paths use JSON Pointer escaping.  ``None`` means a snapshot was unavailable;
    an available snapshot with no differences is represented by an empty list.

    Callers must supply snapshots from the same semantic boundary.  In
    particular, an end-of-turn prediction whose hand is still unknown must be
    reconciled with observed turn-start draw evidence before it is compared to
    a settled next-turn observation.  Intermediate boundaries belong in the
    replay context/phase trace, not in ``observed_minus_predicted``.
    """

    safe_before = _json_safe(before)
    safe_predicted = _json_safe(predicted)
    safe_observed = _json_safe(observed)

    def observation_view(value: Any) -> tuple[Any, bool]:
        if not isinstance(value, Mapping):
            return value, True
        metadata = value.get(_OBSERVATION_METADATA_KEY)
        if metadata is None:
            return value, True
        if not isinstance(metadata, Mapping) or metadata.get("partial") is not True:
            raise ValueError("partial observation metadata is invalid")
        if metadata.get("schema_version") != 1:
            raise ValueError("unsupported partial observation schema")
        boundary = metadata.get("semantic_boundary")
        if not isinstance(boundary, str) or not boundary:
            raise ValueError("partial observation requires a semantic boundary")
        known = metadata.get("known_fields")
        if not isinstance(known, list) or any(
            not isinstance(field, str) or not field for field in known
        ):
            raise ValueError("partial observation known_fields are invalid")
        if len(set(known)) != len(known):
            raise ValueError("partial observation known_fields contain duplicates")
        allowed = set(known) | {_OBSERVATION_METADATA_KEY}
        unknown = set(value) - allowed
        missing = set(known) - set(value)
        if unknown or missing:
            raise ValueError(
                "partial observation fields disagree with known_fields: "
                f"unknown={sorted(unknown)}, missing={sorted(missing)}"
            )
        provenance = metadata.get("provenance")
        if not isinstance(provenance, Mapping) or set(provenance) != set(known):
            raise ValueError("partial observation provenance is incomplete")
        if any(
            not isinstance(source, str) or not source
            for source in provenance.values()
        ):
            raise ValueError("partial observation provenance values are invalid")
        comparable = metadata.get("comparable_to_prediction", True)
        if not isinstance(comparable, bool):
            raise ValueError("partial observation comparability must be boolean")
        return {field: value[field] for field in known}, comparable

    observed_view, comparable = observation_view(safe_observed)

    def project(snapshot: Any) -> Any:
        if not isinstance(observed_view, Mapping) or not isinstance(snapshot, Mapping):
            return snapshot
        return {
            field: snapshot[field]
            for field in observed_view
            if field in snapshot
        }

    return {
        "predicted_from_before": (
            None if predicted is None else _diff(safe_before, safe_predicted)
        ),
        "observed_from_before": (
            None
            if observed is None or not comparable
            else _diff(project(safe_before), observed_view)
        ),
        "observed_minus_predicted": (
            None
            if predicted is None or observed is None or not comparable
            else _diff(project(safe_predicted), observed_view)
        ),
    }


def _evidence_path(source: object) -> Path:
    raw: object = source
    if isinstance(source, Mapping):
        raw = source.get("png_path")
    elif not isinstance(source, (str, Path)):
        raw = getattr(source, "png_path", None)
    if not isinstance(raw, (str, Path)) or not str(raw):
        raise ValueError("replay evidence must provide a png_path")
    path = Path(raw)
    if not path.is_file():
        raise FileNotFoundError(f"replay evidence does not exist: {path}")
    return path


def _plan_evidence(
    evidence: TransitionReplayEvidence,
) -> tuple[list[tuple[Path, str]], dict[str, Any]]:
    copies: list[tuple[Path, str]] = []

    def plan_one(role: str, source: object | None, index: int | None = None) -> str | None:
        if source is None:
            return None
        path = _evidence_path(source)
        suffix = path.suffix.lower() or ".png"
        name = f"{role}{'' if index is None else f'_{index:02d}'}{suffix}"
        relative = str(Path("evidence") / name).replace("\\", "/")
        copies.append((path, relative))
        return relative

    manifest = {
        "pre": plan_one("pre", evidence.pre),
        "preview": plan_one("preview", evidence.preview),
        "resolving": [
            plan_one("resolving", source, index)
            for index, source in enumerate(evidence.resolving, start=1)
        ],
        "settled": [
            plan_one("settled", source, index)
            for index, source in enumerate(evidence.settled, start=1)
        ],
    }
    return copies, manifest


def _normalize_ids(
    ids: TransitionReplayIds | Mapping[str, Any] | None,
) -> TransitionReplayIds:
    if ids is None:
        return TransitionReplayIds()
    if isinstance(ids, TransitionReplayIds):
        return ids
    return TransitionReplayIds.from_mapping(ids)


def _normalize_evidence(
    evidence: TransitionReplayEvidence | Mapping[str, Any] | None,
) -> TransitionReplayEvidence:
    if evidence is None:
        return TransitionReplayEvidence()
    if isinstance(evidence, TransitionReplayEvidence):
        return evidence
    unknown = set(evidence) - {"pre", "preview", "resolving", "settled"}
    if unknown:
        raise ValueError(f"unknown replay evidence roles: {sorted(unknown)}")

    def sequence(name: str) -> tuple[object, ...]:
        raw = evidence.get(name, ())
        if raw is None:
            return ()
        if isinstance(raw, (str, Path, Mapping)) or hasattr(raw, "png_path"):
            return (raw,)
        if not isinstance(raw, Sequence):
            raise ValueError(f"{name} evidence must be a sequence")
        return tuple(raw)

    return TransitionReplayEvidence(
        pre=evidence.get("pre"),
        preview=evidence.get("preview"),
        resolving=sequence("resolving"),
        settled=sequence("settled"),
    )


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def save_transition_replay(
    *,
    status: str,
    before: Any,
    predicted: Any | None,
    observed: Any | None,
    action: Any,
    card: Any | None = None,
    context: Any | None = None,
    ids: TransitionReplayIds | Mapping[str, Any] | None = None,
    phase_trace: Sequence[Any] = (),
    checkpoint: Any | None = None,
    evidence: TransitionReplayEvidence | Mapping[str, Any] | None = None,
    delta: Mapping[str, Any] | None = None,
    failure_reason: str | None = None,
    unknown_effect: bool = False,
    root: Path = DEFAULT_TRANSITION_REPLAY_ROOT,
    record_id: str | None = None,
    created_at: datetime | str | None = None,
) -> SavedTransitionReplay:
    """Persist one complete transition record and copied screenshot evidence."""

    if not isinstance(status, str) or not status.strip():
        raise ValueError("replay status must be non-empty")
    if failure_reason is not None and not isinstance(failure_reason, str):
        raise TypeError("failure_reason must be a string or None")
    chosen_id = record_id or _new_record_id()
    _validate_record_id(chosen_id)

    safe_before = _json_safe(before)
    safe_predicted = _json_safe(predicted)
    safe_observed = _json_safe(observed)
    safe_delta = (
        _json_safe(delta)
        if delta is not None
        else compute_transition_delta(safe_before, safe_predicted, safe_observed)
    )
    normalized_ids = _normalize_ids(ids)
    normalized_evidence = _normalize_evidence(evidence)
    copies, evidence_manifest = _plan_evidence(normalized_evidence)
    payload: dict[str, Any] = {
        "schema_version": TRANSITION_REPLAY_SCHEMA_VERSION,
        "record_id": chosen_id,
        "created_at": _normalize_created_at(created_at),
        "status": status,
        "failure_reason": failure_reason,
        "unknown_effect": bool(unknown_effect),
        "before": safe_before,
        "predicted": safe_predicted,
        "observed": safe_observed,
        "delta": safe_delta,
        "action": _json_safe(action),
        "card": _json_safe(card),
        "context": _json_safe(context),
        "ids": normalized_ids.to_dict(),
        "phase_trace": _json_safe(list(phase_trace)),
        "checkpoint": _json_safe(checkpoint),
        "evidence": evidence_manifest,
    }
    # Serialize before touching the destination so unsupported caller data does
    # not leave behind a record directory that looks valid.
    json.dumps(payload, ensure_ascii=False, allow_nan=False)

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    directory = root / chosen_id
    directory.mkdir(exist_ok=False)
    evidence_directory = directory / "evidence"
    if copies:
        evidence_directory.mkdir()
    for source, relative in copies:
        destination = directory / Path(relative)
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)

    json_path = directory / "replay.json"
    _atomic_write_json(json_path, payload)
    return SavedTransitionReplay(chosen_id, directory, json_path, payload)


def finalize_transition_replay(
    saved: SavedTransitionReplay,
    *,
    status: str,
    failure_reason: str | None,
    checkpoint_committed: bool,
) -> SavedTransitionReplay:
    """Atomically finalize a verified replay after checkpoint persistence.

    Only a ``verified_pending_checkpoint`` record can be finalized.  If this
    write fails, the durable record remains pending and can never masquerade
    as a committed transition.
    """

    if not isinstance(saved, SavedTransitionReplay):
        raise TypeError("saved must be SavedTransitionReplay")
    allowed = {"committed", "checkpoint_failed"}
    if status not in allowed:
        raise ValueError(f"unsupported finalized replay status: {status}")
    if not isinstance(checkpoint_committed, bool):
        raise TypeError("checkpoint_committed must be a boolean")
    if (status == "committed") != checkpoint_committed:
        raise ValueError("final replay status disagrees with checkpoint result")
    if status == "committed" and failure_reason is not None:
        raise ValueError("committed replay cannot have a failure reason")
    if status != "committed" and (
        not isinstance(failure_reason, str) or not failure_reason
    ):
        raise ValueError("failed replay requires a failure reason")

    payload = load_transition_replay(saved.json_path)
    if payload.get("record_id") != saved.record_id:
        raise ValueError("saved replay record_id mismatch")
    if payload.get("status") != "verified_pending_checkpoint":
        raise ValueError("only a pending verified replay can be finalized")
    payload["status"] = status
    payload["failure_reason"] = failure_reason
    for container_name in ("context", "checkpoint"):
        container = payload.get(container_name)
        if not isinstance(container, dict):
            continue
        verification = container.get("verification")
        if not isinstance(verification, dict):
            continue
        verification["checkpoint_committed"] = checkpoint_committed
        verification["checkpoint_outcome"] = (
            "committed" if checkpoint_committed else "checkpoint_failed"
        )
    _atomic_write_json(saved.json_path, payload)
    return SavedTransitionReplay(
        saved.record_id, saved.directory, saved.json_path, payload
    )


def load_transition_replay(path: Path) -> dict[str, Any]:
    """Load a saved replay from either its record directory or JSON path."""

    path = Path(path)
    if path.is_dir():
        path = path / "replay.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("transition replay must be a JSON object")
    if payload.get("schema_version") != TRANSITION_REPLAY_SCHEMA_VERSION:
        raise ValueError("unsupported transition replay schema version")
    return payload
