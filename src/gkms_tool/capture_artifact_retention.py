"""Capture allocation and reference-aware cleanup plans; never deletes files.

PNG pixels are not opened during inventory. Historical captures remain intact
until an operator reviews a complete reference scan and applies its plan.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Iterable
import uuid

SCHEMA = "gkms.capture-retention-inventory.v1"
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})
TEXT_SUFFIXES = frozenset({".json", ".jsonl", ".ndjson", ".md", ".txt", ".log", ".yaml", ".yml",
                          ".py", ".cpp", ".hpp", ".h", ".cs", ".ps1", ".toml", ".ini", ".csv", ".tsv"})
EXCLUDED_DIRECTORIES = frozenset({".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"})
CAPTURE_NAME = re.compile(rb"(?:live(?:_[a-z0-9-]+)?|audition_cards|capture(?:_[a-z0-9-]+)*)_\d{10,20}(?:_[a-f0-9]{6,32})?\.(?:png|jpg|jpeg|webp|bmp)", re.I)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def workspace_path(workspace: Path, path: str | Path) -> Path:
    """Reject traversal, junction/symlink escapes, and the workspace root itself."""
    root = Path(workspace).resolve()
    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError(f"path escapes intended workspace: {resolved}")
    return resolved


@dataclass(frozen=True, slots=True)
class CaptureArtifactAllocation:
    path: Path | None
    storage_class: str
    retention_seconds: int | None
    reason: str

    def metadata(self) -> dict[str, object]:
        return {"path": None if self.path is None else str(self.path),
                "storage_class": self.storage_class, "retention_seconds": self.retention_seconds,
                "reason": self.reason}


def allocate_capture_artifact(workspace: Path, *, backend: str, purpose: str,
                              kind: str = "live", timestamp: float | None = None) -> CaptureArtifactAllocation:
    """Allocate a unique path; retention is planned separately, never evicted here.

    Existing recognizers which consume a PNG request purpose='vision-input'.
    DLL monitor code requests purpose='monitor' and gets no persistent image.
    Failures/checkpoints/manual evidence have no automatic TTL.
    """
    if backend not in {"dll", "maa"}:
        raise ValueError("capture backend must be dll or maa")
    if purpose not in {"monitor", "vision-input", "failure", "checkpoint", "manual"}:
        raise ValueError("unknown capture purpose")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", kind):
        raise ValueError("capture kind must be a bounded safe name")
    if backend == "dll" and purpose == "monitor":
        return CaptureArtifactAllocation(None, "memory-only", 0, "DLL monitor has native state; PNG persistence disabled")
    timestamp = time.time() if timestamp is None else timestamp
    day = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
    pinned = purpose in {"failure", "checkpoint", "manual"}
    directory = "capture_evidence" if pinned else "capture_cache"
    filename = f"capture_{kind}_{int(timestamp * 1000)}_{uuid.uuid4().hex[:12]}.png"
    path = workspace_path(workspace, Path("var") / directory / day / filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    return CaptureArtifactAllocation(path, "evidence" if pinned else "cache", None if pinned else 86400,
                                     purpose)


@dataclass(frozen=True, slots=True)
class CaptureFile:
    path: Path
    size: int
    mtime_ns: int
    recognized_name: bool


def capture_files(workspace: Path, candidate_root: str | Path = "var/captures") -> tuple[CaptureFile, ...]:
    root = Path(workspace).resolve()
    candidates = workspace_path(root, candidate_root)
    allowed = (root / "var/captures", root / "var/capture_cache")
    if not any(candidates == value or candidates.is_relative_to(value) for value in allowed):
        raise ValueError("capture candidates must be under var/captures or var/capture_cache")
    results = []
    if not candidates.is_dir():
        return ()
    for directory, names, files in os.walk(candidates, followlinks=False):
        names[:] = [name for name in names if not _is_link(Path(directory) / name)]
        for name in files:
            path = Path(directory) / name
            if path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            checked = workspace_path(root, path)
            if not checked.is_relative_to(candidates) or _is_link(path):
                raise ValueError("capture candidate escapes the approved capture root")
            stat = path.stat()
            results.append(CaptureFile(path, stat.st_size, stat.st_mtime_ns,
                                       CAPTURE_NAME.fullmatch(name.encode("utf-8")) is not None))
    return tuple(results)


def _reference_category(path: Path, workspace: Path) -> str:
    parts = tuple(part.lower() for part in path.relative_to(workspace).parts)
    if parts[0] == "tests": return "fixture-or-test"
    if any("frozen" in part for part in parts): return "frozen-training"
    if any(part in {"telemetry", "runtime_exact_stage", "runtime_exact_stage_superseded", "exact_v3_capture",
                    "exact_v4_capture", "simulator_v3_exact_regression"} for part in parts):
        return "native-training-evidence"
    if any(part in {"models", "offline_rl", "datasets", "training_dataset", "training_labels", "training_specs",
                    "leaderboard_dataset", "nia_training", "plan3_training"} for part in parts):
        return "training-or-model"
    if parts[0] in {"src", "scripts", "docs", "native"}: return "source-or-documentation"
    if parts[0] in {"_archive", "_research"}: return "research-or-archive"
    return "runtime-history"


def _source_files(workspace: Path, scan_roots: Iterable[Path], excluded: set[Path]):
    seen = set()
    for scan_root in scan_roots:
        resolved = scan_root.resolve()
        if resolved != workspace and not resolved.is_relative_to(workspace):
            raise ValueError("reference scan root escapes workspace")
        paths = [resolved] if resolved.is_file() else None
        if paths is not None:
            if resolved not in seen and resolved.suffix.lower() in TEXT_SUFFIXES:
                seen.add(resolved); yield resolved
            continue
        for directory, names, files in os.walk(resolved, followlinks=False):
            names[:] = [name for name in names if name not in EXCLUDED_DIRECTORIES
                        and not _is_link(Path(directory) / name)
                        and (Path(directory) / name).resolve() not in excluded]
            for name in files:
                path = Path(directory) / name
                if path.suffix.lower() not in TEXT_SUFFIXES or path in seen or _is_link(path):
                    continue
                seen.add(path)
                yield path


def scan_capture_references(workspace: Path, captures: Iterable[CaptureFile], *,
                             scan_roots: Iterable[str | Path] | None = None,
                             excluded_roots: Iterable[str | Path] = (),
                             max_source_bytes: int | None = 32 * 1024 * 1024,
                             progress=None) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    """Stream ASCII capture locators, including JSON-escaped Windows paths.

    Basename references conservatively pin every capture with the same name.
    A bounded scan reports every skipped source and never certifies no-reference
    deletion candidates while any selected text source remains unscanned.
    """
    workspace = Path(workspace).resolve()
    names = {capture.path.name.casefold() for capture in captures}
    roots = [workspace] if scan_roots is None else [Path(value) if Path(value).is_absolute() else workspace / value for value in scan_roots]
    # Image suffixes are skipped by the iterator, but metadata kept beside
    # captures/evidence can itself pin a capture and must still be scanned.
    excluded: set[Path] = set()
    excluded.update(workspace_path(workspace, value) for value in excluded_roots)
    references: dict[str, dict[str, object]] = {}
    skipped = []
    scanned = 0
    scanned_bytes = 0
    rg = shutil.which("rg")
    for path in _source_files(workspace, roots, excluded):
        try:
            size = path.stat().st_size
            if max_source_bytes is not None and size > max_source_bytes:
                skipped.append({"path": str(path), "size": size, "reason": "source-exceeds-scan-limit"})
                continue
            matched = set()
            if rg is not None and size >= 8 * 1024 * 1024:
                # Rust's streaming matcher avoids repeatedly interpreting a
                # broad Python expression across multi-GiB native datasets.
                # argv is passed directly, never composed as a shell command.
                with subprocess.Popen([rg, "--no-config", "--color=never", "--text", "--only-matching", "--no-filename", "--no-line-number",
                        "--no-heading", "--ignore-case", CAPTURE_NAME.pattern.decode("ascii"), "--", str(path)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) |
                                      getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)) as process:
                    assert process.stdout is not None
                    for line in process.stdout:
                        if not line.isascii():
                            continue  # Generated capture filenames are ASCII.
                        name = line.strip().decode("ascii").casefold()
                        if name in names:
                            matched.add(name)
                    error = process.stderr.read() if process.stderr is not None else b""
                    code = process.wait()
                    if code not in (0, 1):
                        raise OSError(f"rg source scan failed ({code}): {error[:300]!r}")
                scanned_bytes += size
            else:
                with path.open("rb") as stream:
                    tail = b""
                    while chunk := stream.read(4 * 1024 * 1024):
                        block = tail + chunk
                        matched.update(match.group().decode("ascii").casefold() for match in CAPTURE_NAME.finditer(block)
                                       if match.group().decode("ascii").casefold() in names)
                        tail = block[-512:]
                        scanned_bytes += len(chunk)
            category = _reference_category(path, workspace)
            for name in matched:
                value = references.setdefault(name, {"source_count": 0, "categories": set(), "source_samples": []})
                value["source_count"] += 1
                value["categories"].add(category)
                if len(value["source_samples"]) < 5:
                    value["source_samples"].append(str(path))
            scanned += 1
            if progress is not None and scanned % 1000 == 0:
                progress({"scanned_files": scanned, "scanned_bytes": scanned_bytes, "referenced_names": len(references)})
        except OSError as error:
            skipped.append({"path": str(path), "reason": f"read-failed:{type(error).__name__}"})
    for value in references.values():
        value["categories"] = sorted(value["categories"])
    return references, {"complete": not skipped, "scanned_files": scanned, "scanned_bytes": scanned_bytes,
        "skipped_sources": skipped, "scan_roots": [str(path) for path in roots],
        "excluded_directory_names": sorted(EXCLUDED_DIRECTORIES), "excluded_roots": sorted(str(path) for path in excluded),
        "scope": "workspace text reference scan; image pixels and binary databases are not opened"}


def write_capture_retention_inventory(workspace: Path, output: str | Path, *,
                                       candidate_root: str | Path = "var/captures",
                                       scan_roots: Iterable[str | Path] | None = None,
                                       max_source_bytes: int | None = 32 * 1024 * 1024,
                                       minimum_age_days: int = 7, keep_newest: int = 500,
                                       now: float | None = None, progress=None) -> dict[str, object]:
    workspace = Path(workspace).resolve()
    output = workspace_path(workspace, output)
    if minimum_age_days < 0 or keep_newest < 0:
        raise ValueError("retention age/count cannot be negative")
    captures = capture_files(workspace, candidate_root)
    if progress is not None:
        progress({"capture_files": len(captures), "capture_bytes": sum(row.size for row in captures)})
    references, scan = scan_capture_references(workspace, captures, scan_roots=scan_roots,
        excluded_roots=(output,), max_source_bytes=max_source_bytes, progress=progress)
    output.mkdir(parents=True, exist_ok=True)
    newest = {row.path for row in sorted(captures, key=lambda row: (-row.mtime_ns, str(row.path)))[:keep_newest]}
    now = time.time() if now is None else now
    counts = Counter()
    sizes = Counter()
    with (output / "captures.jsonl").open("w", encoding="utf-8") as manifest, \
         (output / "unreferenced-review.jsonl").open("w", encoding="utf-8") as candidates:
        for capture in sorted(captures, key=lambda row: str(row.path)):
            reference = references.get(capture.path.name.casefold())
            age = (now - capture.mtime_ns / 1e9) / 86400
            reasons = []
            if reference is not None: reasons.append("referenced")
            if not capture.recognized_name: reasons.append("unrecognized-capture-name")
            if age < minimum_age_days: reasons.append("recent-capture")
            if capture.path in newest: reasons.append("keep-newest-budget")
            if not scan["complete"]: reasons.append("reference-scan-incomplete")
            category = "referenced" if reference else "unreferenced-in-scanned-text"
            eligible = not reasons
            record = {"path": str(capture.path), "size": capture.size, "mtime_ns": capture.mtime_ns,
                "age_days": round(age, 3), "classification": category,
                "references": reference, "retained_reasons": reasons,
                "eligible_for_deletion_review": eligible}
            manifest.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            if reference is None:
                candidates.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            counts[category] += 1; sizes[category] += capture.size
            if eligible: counts["eligible_for_deletion_review"] += 1; sizes["eligible_for_deletion_review"] += capture.size
            for group in (() if reference is None else reference["categories"]):
                counts[f"reference:{group}"] += 1; sizes[f"reference:{group}"] += capture.size
    summary = {"schema": SCHEMA, "created_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "workspace": str(workspace), "candidate_root": str(workspace_path(workspace, candidate_root)),
        "capture_count": len(captures), "capture_bytes": sum(row.size for row in captures),
        "counts": dict(counts), "bytes": dict(sizes), "scan": scan,
        "policy": {"minimum_age_days": minimum_age_days, "keep_newest": keep_newest},
        "actions_taken": "inventory-only; no capture has been moved or deleted",
        "apply_requirements": ["review references and scan coverage", "resolve every path inside approved capture root",
                               "recheck size and mtime against manifest", "recheck references after new runs or training updates"]}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
