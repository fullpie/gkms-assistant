"""Canonical JSON bytes and digest shared by runtime simulator bridges.

This module deliberately contains no simulator or state-model dependencies.
It preserves the strict JSON representation historically used by the Plan 1,
Plan 2 runtime-diff, and Plan 3 bridge entry points.
"""

from __future__ import annotations

import hashlib
import json


def canonical_json_bytes(value: object) -> bytes:
    """Return the strict, stable UTF-8 JSON representation of ``value``."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    """Return the lowercase SHA-256 of :func:`canonical_json_bytes`."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


__all__ = ["canonical_json_bytes", "canonical_sha256"]
