"""Lossless, sharded storage for already-hashed shared primary pointer inputs.

The caller owns encoding, source qualification and label binding. This module
never hashes tokens, fits a model or truncates a row. A published manifest binds
the exact int32 arrays, int64 offsets and same-order JSON metadata. Only requested
rows are padded, using the existing OuterDataset index/mask conventions.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile

import numpy as np

from .training_artifact_io import atomic_write, canonical_json_bytes, sha256_file

SCHEMA = "gkms.packed-primary-pointer-features.v1"
_DTYPES = {"context_values": "<i4", "candidate_values": "<i4",
    "context_offsets": "<i8", "candidate_token_offsets": "<i8",
    "row_candidate_offsets": "<i8", "metadata_offsets": "<i8"}
_FILES = {**{k: k + ".bin" for k in _DTYPES}, "metadata": "metadata.jsonl"}


def _integer(value, name, minimum=0, maximum=None):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise ValueError(name + " must be an integer")
    value = int(value)
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(name + " is outside the supported range")
    return value


def _sha(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _indices(values, buckets):
    if isinstance(values, (list, tuple)) and any(isinstance(x, (bool, np.bool_)) for x in values):
        raise ValueError("Boolean values are not pointer indices")
    array = np.asarray(values)
    if array.ndim != 1 or array.dtype.kind not in "iu" or not array.size:
        raise ValueError("Each token row must contain integer indices")
    if np.any(array < 0) or np.any(array >= buckets):
        raise ValueError("Pointer index is outside its bucket range")
    return np.asarray(array, dtype="<i4")


def _metadata(value, candidate_count, contract_sha):
    if not isinstance(value, Mapping):
        raise ValueError("Same-order source row metadata is required")
    value = json.loads(canonical_json_bytes(dict(value)))
    _integer(value.get("label_index"), "label_index", maximum=candidate_count - 1)
    if value.get("feature_contract_sha256") != contract_sha:
        raise ValueError("Row feature contract differs from the packed contract")
    if type(value.get("representation_complete")) is not bool or not isinstance(value.get("mandatory_gaps"), list):
        raise ValueError("Row representation status and mandatory gaps must be explicit")
    if value["representation_complete"] and value["mandatory_gaps"]:
        raise ValueError("A row with mandatory gaps cannot claim representation completeness")
    value.setdefault("training_admitted", False)
    if value["training_admitted"] is not False:
        raise ValueError("Packed storage does not grant training admission")
    ref = value.get("event_reference")
    if not isinstance(ref, dict) or not isinstance(ref.get("path"), str) or not ref["path"] or not _sha(ref.get("sha256")):
        raise ValueError("Row event reference needs its original path and SHA256")
    return value


def _ref(path):
    return {"path": str(path.resolve()), "sha256": sha256_file(path), "bytes": path.stat().st_size}


def _close_arrays(arrays):
    for value in arrays.values():
        if isinstance(value, np.memmap):
            value._mmap.close()


class PackedPrimaryWriter:
    """Stream into a private staging directory; finish publishes once verified.

    shard_bytes is a soft boundary between whole rows. One larger row occupies
    its own shard; no candidate or token is discarded. On failure, abandon this
    writer and retain its staging_path for diagnosis. Existing output is refused.
    """

    def __init__(self, output_dir, *, context_buckets, candidate_buckets,
                 metadata_contract, shard_rows=128, shard_bytes=64 * 1024 * 1024):
        self.output_dir = Path(output_dir).resolve()
        if self.output_dir.exists():
            raise FileExistsError(self.output_dir)
        self.context_buckets = _integer(context_buckets, "context_buckets", 1, 2**31)
        self.candidate_buckets = _integer(candidate_buckets, "candidate_buckets", 1, 2**31)
        self.shard_rows = _integer(shard_rows, "shard_rows", 1)
        self.shard_bytes = _integer(shard_bytes, "shard_bytes", 1)
        if not isinstance(metadata_contract, Mapping) or not _sha(metadata_contract.get("feature_contract_sha256")):
            raise ValueError("metadata_contract needs the exact feature_contract_sha256")
        self.metadata_contract = json.loads(canonical_json_bytes(dict(metadata_contract)))
        owner = self.metadata_contract.get("pointer_index_owner")
        if owner is not None and (not isinstance(owner, str) or not owner.strip()):
            raise ValueError("An explicit pointer index owner must be a nonempty name")
        self.output_dir.parent.mkdir(parents=True, exist_ok=True)
        self.staging_path = Path(tempfile.mkdtemp(prefix="." + self.output_dir.name + ".partial-", dir=self.output_dir.parent))
        self._handles = {}
        self._shards = []
        self._rows = 0
        self._failed = False
        self._finished = False

    def _check_open(self):
        if self._failed or self._finished:
            raise ValueError("Packed writer is failed or already finished")

    def _start_shard(self):
        self._shard_path = self.staging_path / f"shard-{len(self._shards):05d}"
        self._shard_path.mkdir()
        self._counts = dict.fromkeys(_FILES, 0)
        self._local_rows = 0
        self._local_complete = 0
        self._shard_start = self._rows
        for name, filename in _FILES.items():
            self._handles[name] = (self._shard_path / filename).open("xb")
        for name in _DTYPES:
            if name.endswith("offsets"):
                self._write_array(name, np.array([0], dtype="<i8"))

    def _write_array(self, name, array):
        self._handles[name].write(array.tobytes(order="C"))
        self._counts[name] += len(array)

    def _seal_shard(self):
        if not self._handles:
            return
        for handle in self._handles.values():
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
        self._handles = {}
        files = {}
        for name, filename in _FILES.items():
            path = self._shard_path / filename
            files[name] = {"name": filename, "sha256": sha256_file(path), "bytes": path.stat().st_size,
                **({"dtype": _DTYPES[name], "count": self._counts[name]} if name in _DTYPES else {})}
        self._shards.append({"directory": self._shard_path.name, "row_start": self._shard_start,
            "row_count": self._local_rows, "candidate_count": self._counts["candidate_token_offsets"] - 1,
            "representation_complete_row_count": self._local_complete,
            "files": files})

    def append(self, context_indices, candidate_indices, *, metadata):
        self._check_open()
        try:
            context = _indices(context_indices, self.context_buckets)
            candidates = [_indices(row, self.candidate_buckets) for row in candidate_indices]
            if not candidates:
                raise ValueError("A primary row needs at least one candidate")
            record = _metadata(metadata, len(candidates), self.metadata_contract["feature_contract_sha256"])
            raw = canonical_json_bytes({"row_index": self._rows, "data": record}) + b"\n"
            row_bytes = context.nbytes + sum(x.nbytes + 8 for x in candidates) + len(raw) + 24
            if self._handles and (self._local_rows >= self.shard_rows or
                    sum(h.tell() for h in self._handles.values()) + row_bytes > self.shard_bytes):
                self._seal_shard()
            if not self._handles:
                self._start_shard()
            result = {"row_index": self._rows, "shard_index": len(self._shards)}
            self._write_array("context_values", context)
            self._write_array("context_offsets", np.array([self._counts["context_values"]], dtype="<i8"))
            for candidate in candidates:
                self._write_array("candidate_values", candidate)
                self._write_array("candidate_token_offsets", np.array([self._counts["candidate_values"]], dtype="<i8"))
            self._write_array("row_candidate_offsets", np.array([self._counts["candidate_token_offsets"] - 1], dtype="<i8"))
            self._handles["metadata"].write(raw)
            self._write_array("metadata_offsets", np.array([self._handles["metadata"].tell()], dtype="<i8"))
            self._local_rows += 1
            self._local_complete += int(record["representation_complete"])
            self._rows += 1
            return result
        except BaseException:
            self.abort()
            raise

    def abort(self):
        """Close resources and retain partial evidence without publishing it."""
        self._failed = True
        for handle in self._handles.values():
            handle.close()
        self._handles = {}

    def finish(self):
        self._check_open()
        try:
            if not self._rows:
                raise ValueError("Cannot publish an empty packed dataset")
            self._seal_shard()
            manifest = {"schema": SCHEMA, "status": "complete", "row_count": self._rows,
                "storage_complete": True,
                "representation_complete_row_count": sum(s["representation_complete_row_count"] for s in self._shards),
                "representation_status_owner": "caller row metadata; storage does not recompute source features",
                "candidate_count": sum(s["candidate_count"] for s in self._shards),
                "context_buckets": self.context_buckets, "candidate_buckets": self.candidate_buckets,
                "metadata_contract": self.metadata_contract, "shards": self._shards,
                "index_owner": self.metadata_contract.get("pointer_index_owner",
                    "SharedPrimaryFeatures.pointer_indices; caller supplied; no rehash"),
                "padding": "batch-only; zero index is valid and masks determine presence",
                "tokens_truncated": False, "training_admitted": False}
            manifest["representation_incomplete_row_count"] = self._rows - manifest["representation_complete_row_count"]
            manifest["representation_complete"] = manifest["representation_incomplete_row_count"] == 0
            _validate_manifest(manifest)
            for shard in self._shards:
                arrays = _load_shard(self.staging_path, shard, manifest)
                _close_arrays(arrays)
            atomic_write(self.staging_path / "manifest.json", canonical_json_bytes(manifest) + b"\n")
            receipt = _ref(self.staging_path / "manifest.json")
            if self.output_dir.exists():
                raise FileExistsError(self.output_dir)
            os.rename(self.staging_path, self.output_dir)
            self._finished = True
            return {**receipt, "path": str(self.output_dir / "manifest.json"), "row_count": self._rows,
                "candidate_count": manifest["candidate_count"], "schema": SCHEMA}
        except BaseException:
            self.abort()
            (self.staging_path / "manifest.json").unlink(missing_ok=True)
            raise


def _validate_manifest(manifest):
    if manifest.get("schema") != SCHEMA or manifest.get("status") != "complete":
        raise ValueError("Packed manifest is not complete or has an unsupported schema")
    if manifest.get("storage_complete") is not True or manifest.get("training_admitted") is not False:
        raise ValueError("Packed completion is storage-only, never training admission")
    for name in ("context_buckets", "candidate_buckets"):
        _integer(manifest[name], name, 1, 2**31)
    _integer(manifest["row_count"], "row_count", 1)
    if not _sha(manifest.get("metadata_contract", {}).get("feature_contract_sha256")):
        raise ValueError("Packed feature contract is missing")
    rows = candidates = complete_rows = 0
    if not isinstance(manifest.get("shards"), list) or not manifest["shards"]:
        raise ValueError("Packed shards are missing")
    for index, shard in enumerate(manifest["shards"]):
        if shard["directory"] != f"shard-{index:05d}" or _integer(shard["row_start"], "row_start") != rows:
            raise ValueError("Packed shard order or row offsets differ")
        rows += _integer(shard["row_count"], "shard row_count", 1)
        complete_rows += _integer(shard["representation_complete_row_count"], "representation complete rows", maximum=shard["row_count"])
        candidates += _integer(shard["candidate_count"], "candidate_count", shard["row_count"])
        if set(shard["files"]) != set(_FILES):
            raise ValueError("Packed file set differs")
        for name, ref in shard["files"].items():
            if ref["name"] != _FILES[name] or not _sha(ref["sha256"]):
                raise ValueError("Packed filename or SHA differs")
            _integer(ref["bytes"], "file bytes", 1)
            if name in _DTYPES and (ref["dtype"] != _DTYPES[name] or
                    ref["bytes"] != _integer(ref["count"], "array count", 1) * np.dtype(_DTYPES[name]).itemsize):
                raise ValueError("Packed array dtype or shape differs")
    if rows != manifest["row_count"] or candidates != _integer(manifest["candidate_count"], "candidate_count", 1):
        raise ValueError("Packed manifest total counts differ")
    if (complete_rows != _integer(manifest["representation_complete_row_count"], "representation complete rows") or
            rows - complete_rows != _integer(manifest["representation_incomplete_row_count"], "representation incomplete rows") or
            manifest.get("representation_complete") is not (complete_rows == rows)):
        raise ValueError("Packed representation totals differ from the explicit row statuses")


def _offsets(array, expected_count, end):
    if len(array) != expected_count or array[0] != 0 or array[-1] != end:
        raise ValueError("Packed offset shape or endpoint differs")
    for start in range(0, len(array) - 1, 65536):
        block = array[start:start + 65537]
        if np.any(block[1:] <= block[:-1]):
            raise ValueError("Packed offsets must strictly increase")


def _row_metadata(stream, offsets, local_index, global_index, candidates, contract_sha):
    start, end = int(offsets[local_index]), int(offsets[local_index + 1])
    stream.seek(start)
    raw = stream.read(end - start)
    if not raw.endswith(b"\n") or b"\n" in raw[:-1]:
        raise ValueError("Packed metadata row boundary differs")
    envelope = json.loads(raw)
    if type(envelope.get("row_index")) is not int or envelope["row_index"] != global_index:
        raise ValueError("Packed metadata row order differs")
    return _metadata(envelope["data"], candidates, contract_sha)


def _load_shard(root, shard, manifest):
    arrays = {}
    try:
        directory = root / shard["directory"]
        for name, ref in shard["files"].items():
            path = directory / ref["name"]
            if path.stat().st_size != ref["bytes"] or sha256_file(path) != ref["sha256"]:
                raise ValueError("Packed file SHA or byte size differs: " + name)
            if name in _DTYPES:
                arrays[name] = np.memmap(path, dtype=_DTYPES[name], mode="r", shape=(ref["count"],))
        rows, candidates = shard["row_count"], shard["candidate_count"]
        _offsets(arrays["context_offsets"], rows + 1, len(arrays["context_values"]))
        _offsets(arrays["row_candidate_offsets"], rows + 1, candidates)
        _offsets(arrays["candidate_token_offsets"], candidates + 1, len(arrays["candidate_values"]))
        _offsets(arrays["metadata_offsets"], rows + 1, shard["files"]["metadata"]["bytes"])
        for name, buckets in (("context_values", manifest["context_buckets"]), ("candidate_values", manifest["candidate_buckets"])):
            for start in range(0, len(arrays[name]), 65536):
                block = arrays[name][start:start + 65536]
                if np.any(block < 0) or np.any(block >= buckets):
                    raise ValueError("Packed pointer index is outside its bucket range")
        with (directory / _FILES["metadata"]).open("rb") as stream:
            complete_rows = 0
            for local in range(rows):
                count = int(arrays["row_candidate_offsets"][local + 1] - arrays["row_candidate_offsets"][local])
                record = _row_metadata(stream, arrays["metadata_offsets"], local, shard["row_start"] + local,
                    count, manifest["metadata_contract"]["feature_contract_sha256"])
                complete_rows += int(record["representation_complete"])
            if complete_rows != shard["representation_complete_row_count"]:
                raise ValueError("Packed shard representation count differs from row metadata")
        return arrays
    except BaseException:
        _close_arrays(arrays)
        raise


@dataclass(frozen=True)
class PackedPrimaryBatch:
    context_indices: np.ndarray
    context_mask: np.ndarray
    candidate_indices: np.ndarray
    candidate_token_mask: np.ndarray
    candidate_mask: np.ndarray
    targets: np.ndarray
    row_indices: tuple[int, ...]
    metadata: tuple[dict, ...]

    @property
    def labels(self):
        return self.targets


class PackedPrimaryReader:
    """Verify selected shards and pad only the explicitly requested batch."""

    def __init__(self, path, *, expected_sha256=None):
        path = Path(path).resolve()
        self.path = path / "manifest.json" if path.is_dir() else path
        raw = self.path.read_bytes()
        self._manifest_sha = hashlib.sha256(raw).hexdigest()
        if expected_sha256 is not None and (not _sha(expected_sha256) or self._manifest_sha != expected_sha256):
            raise ValueError("Packed manifest SHA differs from the owning receipt")
        self._manifest = json.loads(raw)
        _validate_manifest(self._manifest)
        self.row_count = self._manifest["row_count"]

    @property
    def manifest(self):
        return json.loads(canonical_json_bytes(self._manifest))

    def _check_manifest(self):
        if sha256_file(self.path) != self._manifest_sha:
            raise ValueError("Packed manifest changed after reader construction")

    def verify(self):
        """Hash and validate every derived shard, never the original captures."""
        self._check_manifest()
        for shard in self._manifest["shards"]:
            arrays = _load_shard(self.path.parent, shard, self._manifest)
            _close_arrays(arrays)
        return _ref(self.path)

    def get_rows(self, row_indices, *, max_batch_bytes=256 * 1024 * 1024):
        self._check_manifest()
        selected = tuple(_integer(x, "row index", maximum=self.row_count - 1) for x in row_indices)
        if not selected:
            raise ValueError("A packed batch must select at least one row")
        budget = _integer(max_batch_bytes, "max_batch_bytes", 1)
        requested = {}
        for position, index in enumerate(selected):
            requested.setdefault(index, []).append(position)
        values = [None] * len(selected)
        width = candidate_width = token_width = 0
        for shard in self._manifest["shards"]:
            indices = [x for x in requested if shard["row_start"] <= x < shard["row_start"] + shard["row_count"]]
            if not indices:
                continue
            arrays = _load_shard(self.path.parent, shard, self._manifest)
            try:
                with (self.path.parent / shard["directory"] / _FILES["metadata"]).open("rb") as stream:
                    for index in indices:
                        local = index - shard["row_start"]
                        start, end = arrays["context_offsets"][local:local + 2]
                        first, last = arrays["row_candidate_offsets"][local:local + 2]
                        width = max(width, int(end - start))
                        candidate_width = max(candidate_width, int(last - first))
                        offsets = arrays["candidate_token_offsets"][first:last + 1]
                        token_width = max(token_width, int(np.max(offsets[1:] - offsets[:-1])))
                        required = len(selected) * (width * 8 + candidate_width * token_width * 8 + candidate_width * 4 + 4)
                        if required > budget:
                            raise MemoryError(f"Requested batch padding needs {required} bytes; limit is {budget}; select fewer rows")
                        context = arrays["context_values"][start:end].copy()
                        candidates = []
                        for candidate in range(int(first), int(last)):
                            start, end = arrays["candidate_token_offsets"][candidate:candidate + 2]
                            candidates.append(arrays["candidate_values"][start:end].copy())
                        metadata = _row_metadata(stream, arrays["metadata_offsets"], local, index,
                            len(candidates), self._manifest["metadata_contract"]["feature_contract_sha256"])
                        for position in requested[index]:
                            values[position] = (context, candidates, metadata)
            finally:
                _close_arrays(arrays)
        n = len(values)
        contexts = np.zeros((n, width), dtype=np.int32)
        context_mask = np.zeros_like(contexts, dtype=np.float32)
        candidates = np.zeros((n, candidate_width, token_width), dtype=np.int32)
        token_mask = np.zeros_like(candidates, dtype=np.float32)
        candidate_mask = np.zeros((n, candidate_width), dtype=np.float32)
        labels = np.empty(n, dtype=np.int32)
        for index, (context, rows, metadata) in enumerate(values):
            contexts[index, :len(context)] = context
            context_mask[index, :len(context)] = 1
            for candidate, row in enumerate(rows):
                candidates[index, candidate, :len(row)] = row
                token_mask[index, candidate, :len(row)] = 1
                candidate_mask[index, candidate] = 1
            labels[index] = metadata["label_index"]
        return PackedPrimaryBatch(contexts, context_mask, candidates, token_mask, candidate_mask,
            labels, selected, tuple(x[2] for x in values))


__all__ = ["SCHEMA", "PackedPrimaryWriter", "PackedPrimaryReader", "PackedPrimaryBatch"]
