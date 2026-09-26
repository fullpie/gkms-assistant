"""Shared byte-level I/O helpers for training artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def replace_flushed_file(source, target, timeout: float = 2.0) -> None:
    """Retry only Windows sharing failures for the same already-flushed file.

    Serialization and flushing belong to the caller. This retries only rename;
    it never recreates bytes, retries an operation, or removes retained state.
    """
    if type(timeout) not in (int, float) or not 0 <= timeout <= 2.0:
        raise ValueError('Replacement retry timeout must be between 0 and 2 seconds')
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.replace(source, target)
            return
        except OSError as error:
            if os.name != 'nt' or getattr(error, 'winerror', None) not in (5, 32, 33):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(0.02, remaining))


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
