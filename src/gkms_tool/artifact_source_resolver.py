"""Explicit content-verified relocation of immutable historical artifacts.

Logical references remain the original provenance. A relocation supplies only
another physical file containing the identical bytes. No directory search,
current-game substitution, permissive SHA fallback or encoder qualification is
performed here. Existing loaders must opt in by accepting this resolver.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any


SCHEMA = "gkms.hash-bound-artifact-relocations.v1"
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_MAX_MANIFEST_BYTES = 1024 * 1024


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _validated_reference(reference):
    if not isinstance(reference, Mapping):
        raise ValueError("An explicit path/SHA artifact reference is required")
    value = deepcopy(dict(reference))
    if (not isinstance(value.get("path"), str) or not Path(value["path"]).is_absolute()
            or not isinstance(value.get("sha256"), str) or not _SHA.fullmatch(value["sha256"])):
        raise ValueError("Artifact reference requires an absolute path and lowercase SHA-256")
    if "bytes" in value and (type(value["bytes"]) is not int or value["bytes"] < 0):
        raise ValueError("Artifact byte count must be a nonnegative integer")
    _canonical(value)
    return value


def _path_key(path):
    return os.path.normcase(str(Path(path).resolve()))


def _stat_key(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _read_verified(reference, *, keep_bytes=False, maximum_bytes=None):
    path = Path(reference["path"]).resolve(strict=True)
    if not path.is_file():
        raise ValueError("Artifact source must be a regular file")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if maximum_bytes is not None and before.st_size > maximum_bytes:
            raise ValueError("Artifact source exceeds the explicit read limit")
        digest, pieces, size = hashlib.sha256(), [], 0
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block); size += len(block)
            if maximum_bytes is not None and size > maximum_bytes:
                raise ValueError("Artifact source exceeded the explicit read limit")
            if keep_bytes:
                pieces.append(block)
        after = os.fstat(stream.fileno())
    stamp = _stat_key(after)
    if (_stat_key(before) != stamp or _stat_key(path.stat()) != stamp or size != after.st_size):
        raise ValueError("Artifact source changed during the verified read")
    if digest.hexdigest() != reference["sha256"]:
        raise ValueError("Artifact source SHA-256 differs from the exact requested content")
    if "bytes" in reference and size != reference["bytes"]:
        raise ValueError("Artifact source byte count differs from its reference")
    return path, stamp, b"".join(pieces) if keep_bytes else None


@dataclass(frozen=True, slots=True)
class ResolvedArtifact:
    physical_path: Path
    content_sha256: str
    byte_count: int
    relocated: bool
    _logical_json: str
    _physical_stamp: tuple[int, ...]

    @property
    def logical_reference(self) -> dict[str, Any]:
        """Original reference, including its unchanged historical path."""
        return json.loads(self._logical_json)

    @property
    def physical_reference(self) -> dict[str, Any]:
        return {"path": str(self.physical_path), "sha256": self.content_sha256, "bytes": self.byte_count}

    @property
    def cache_key(self):
        """Cache on the real archived file, never on an overwritten game file."""
        return str(self.physical_path), self.content_sha256, self._physical_stamp

    def validate_unchanged(self):
        if _stat_key(self.physical_path.stat()) != self._physical_stamp:
            raise ValueError("Previously resolved artifact changed during its consumer lifetime")

    def to_dict(self):
        return {"logical_reference": self.logical_reference, "physical_reference": self.physical_reference,
            "relocated": self.relocated, "identical_bytes_verified": True,
            "logical_reference_rewritten": False, "training_or_consumer_compatibility_granted": False}


class ArtifactSourceResolver:
    """Resolve only an explicitly pinned logical-reference-to-file map.

    The map itself is hash-verified. Unlisted references retain direct lookup;
    a mismatching file never triggers discovery or a different hash/version.
    Every mapped destination is checked at construction and again on use.
    """

    def __init__(self, manifest_reference):
        manifest_ref = _validated_reference(manifest_reference)
        path, stamp, raw = _read_verified(manifest_ref, keep_bytes=True, maximum_bytes=_MAX_MANIFEST_BYTES)
        body = json.loads(raw)
        if (not isinstance(body, dict) or set(body) != {"schema", "relocations"}
                or body["schema"] != SCHEMA or not isinstance(body["relocations"], list)
                or len(body["relocations"]) > 4096):
            raise ValueError("A bounded explicit artifact relocation manifest is required")
        self._manifest_reference = manifest_ref
        self._manifest_path, self._manifest_stamp = path, stamp
        self._entries = {}
        self._resolutions: dict[tuple[str, str], ResolvedArtifact] = {}
        self._manifest_destinations = []
        for row in body["relocations"]:
            if not isinstance(row, dict) or set(row) != {"logical_reference", "physical_reference"}:
                raise ValueError("Each relocation must contain only logical and physical references")
            logical = _validated_reference(row["logical_reference"])
            physical = _validated_reference(row["physical_reference"])
            if (logical["sha256"] != physical["sha256"]
                    or "bytes" in logical and "bytes" in physical and logical["bytes"] != physical["bytes"]):
                raise ValueError("Relocation cannot change the logical artifact hash or byte count")
            key = _path_key(logical["path"]), logical["sha256"]
            if key in self._entries:
                raise ValueError("Duplicate or ambiguous artifact relocation")
            destination, destination_stamp, _ = _read_verified(physical)
            if "bytes" in logical and destination_stamp[2] != logical["bytes"]:
                raise ValueError("Physical bytes do not satisfy the unchanged logical reference")
            self._entries[key] = physical
            self._manifest_destinations.append((destination, destination_stamp))
        self.validate_unchanged()

    @property
    def manifest_reference(self):
        return deepcopy(self._manifest_reference)

    def resolve(self, reference, *, expected_sha256=None) -> ResolvedArtifact:
        logical = _validated_reference(reference)
        if expected_sha256 is not None and logical["sha256"] != expected_sha256:
            raise ValueError("Logical artifact differs from the caller's pinned authority SHA")
        self.validate_unchanged()
        key = _path_key(logical["path"]), logical["sha256"]
        physical = self._entries.get(key, logical)
        path, stamp, _ = _read_verified(physical)
        if "bytes" in logical and logical["bytes"] != stamp[2]:
            raise ValueError("Resolved content does not satisfy the original logical byte count")
        result = ResolvedArtifact(path, logical["sha256"], stamp[2], key in self._entries,
            _canonical(logical), stamp)
        self._resolutions[key] = result
        self.validate_unchanged()
        return result

    def read_bytes(self, reference, *, expected_sha256=None, maximum_bytes=None):
        resolved = self.resolve(reference, expected_sha256=expected_sha256)
        _, stamp, raw = _read_verified(resolved.physical_reference, keep_bytes=True, maximum_bytes=maximum_bytes)
        if stamp != resolved._physical_stamp:
            raise ValueError("Artifact was replaced between resolution and consumption")
        self.validate_unchanged()
        return raw, resolved

    def validate_unchanged(self):
        if _stat_key(self._manifest_path.stat()) != self._manifest_stamp:
            raise ValueError("Artifact relocation manifest changed during its consumer lifetime")
        for path, stamp in self._manifest_destinations:
            if _stat_key(path.stat()) != stamp:
                raise ValueError("Artifact relocation destination changed during its consumer lifetime")
        for resolved in self._resolutions.values():
            resolved.validate_unchanged()

    @property
    def provenance(self):
        self.validate_unchanged()
        return {"schema": "gkms.artifact-source-resolution.v1", "manifest_reference": self.manifest_reference,
            "resolved": [self._resolutions[k].to_dict() for k in sorted(self._resolutions)],
            "implicit_directory_search": False, "logical_references_rewritten": False,
            "files_copied_or_replaced": False, "training_or_consumer_compatibility_granted": False}


__all__ = ["SCHEMA", "ResolvedArtifact", "ArtifactSourceResolver"]
