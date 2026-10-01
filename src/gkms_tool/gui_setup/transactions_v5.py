"""Explicit backup/takeover plans over the retained v4 atomic recovery engine."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shutil
import time
import uuid

from .packages import SetupError, Package, digest, relative_name, EXECUTABLE
from .transactions import InstallStore as V4Store, checked_path, file_hash, atomic_write, write_json, file_lock


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _require(value, reason):
    if not value:
        raise SetupError(reason)


class InstallStore(V4Store):
    SCHEMA = "gkms.install-ledger.v5"
    JOURNAL_SCHEMA = "gkms.backed-up-install.v1"

    def __init__(self, root, state_dir, require_game=True):
        super().__init__(root, state_dir, require_game=require_game)
        self.legacy = V4Store(root, state_dir, require_game=require_game)
        self.manifest_path = self.db / "manifest-v5.json"
        self.journal_path = self.db / "pending-v5.json"
        self.completion_path = self.db / "completion-v5.json"
        self.staging = self.db / "payloads-v5"
        self.receipts = self.db / "transactions-v5"

    def manifest(self):
        if self.manifest_path.exists():
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            _require(value.get("schema") == self.SCHEMA and value.get("root") == str(self.root), "V5 ledger identity differs")
            self._validate_records(value)
            return value
        old = self.legacy.manifest()
        value = deepcopy(old)
        value["schema"] = self.SCHEMA
        for record in value["files"].values():
            record["original"] = None  # V4 only added files; it never replaced external bytes.
        value["legacySource"] = ({"path": "manifest.json", "sha256": file_hash(self.legacy.manifest_path)}
            if self.legacy.manifest_path.exists() else None)
        self._validate_records(value)
        return value

    def _validate_records(self, value):
        _require(isinstance(value.get("files"), dict), "Invalid managed file inventory")
        for relative, record in value["files"].items():
            _require(isinstance(record, dict), "Invalid managed file record")
            self._allowed(record.get("module"), relative)
            for field in ("installed", "original"):
                sha = record.get(field)
                _require((field == "original" and sha is None) or
                    isinstance(sha, str) and re.fullmatch("[a-f0-9]{64}", sha), "Invalid managed file SHA")

    def _validate_journal(self, journal):
        _require(journal.get("schema") == self.JOURNAL_SCHEMA, "Unknown installation journal")
        for value in (journal.get("old", {}), journal.get("target", {})):
            _require(value.get("schema") == self.SCHEMA and value.get("root") == str(self.root), "Journal ledger scope differs")
            self._validate_records(value)
        _require(isinstance(journal.get("changes"), list), "Journal changes are missing")
        paths = set()
        for row in journal["changes"]:
            relative = row["path"]
            _require(relative not in paths, "Duplicate transaction target")
            paths.add(relative)
            owner = journal["target"]["files"].get(relative) or journal["old"]["files"].get(relative)
            _require(owner is not None, "Journal target has no fixed module owner")
            self._allowed(owner["module"], relative)
            for field in ("before", "after"):
                sha = row.get(field)
                _require(sha is None or isinstance(sha, str) and re.fullmatch("[a-f0-9]{64}", sha), "Invalid journal content SHA")

    def _stage(self, data):
        self.staging.mkdir(exist_ok=True)
        sha = digest(data)
        path = checked_path(self.staging, sha + ".blob")
        if not path.exists():
            atomic_write(path, data)
        _require(file_hash(path) == sha, "Durable backup/payload hash differs")
        return sha

    def _payload(self, sha):
        _require(isinstance(sha, str) and re.fullmatch("[a-f0-9]{64}", sha), "Invalid backup/payload identity")
        path = checked_path(self.staging, sha + ".blob")
        _require(file_hash(path) == sha, "Durable backup/payload is missing or changed")
        return path.read_bytes()

    def _clear_staging(self):
        pass  # Original bytes and transaction-before bytes remain available for restoration.

    def _allowed(self, module, relative):
        relative_name(relative)
        control = {"version.dll", "msvcp140.dll", "concrt140.dll", "vcruntime140.dll", "vcruntime140_1.dll",
            "gkms/native/gkms_runtime_command_bridge.dll", "gkms/native/gkms_runtime_exam_recorder.dll",
            "gkms/native/gkms_runtime_outer_observer.dll"}
        allowed = relative in control if module == "control" else (module == "translation" and
            (relative == "gakumas-local/version.txt" or relative.startswith("gakumas-local/local-files/"))
            and Path(relative).suffix.lower() not in EXECUTABLE)
        _require(allowed, "File is outside the fixed module inventory: " + relative)

    def _archive_legacy(self):
        if self.legacy.manifest_path.exists():
            raw = self.legacy.manifest_path.read_bytes()
            path = self.db / "legacy-evidence" / (digest(raw) + ".json")
            if not path.exists(): atomic_write(path, raw)
            _require(file_hash(path) == digest(raw), "Original V4 ledger archive changed")

    def _plan(self, package, metadata, revision):
        _require(not self.journal_path.exists() and not self.completion_path.exists() and not self.legacy.journal_path.exists(),
            "An incomplete transaction must be explicitly recovered first")
        value = self.manifest()
        _require(value["revision"] == revision, "Installation state changed. Refresh before continuing.")
        self._verify_managed(value)
        desired, files = {}, []
        if package is not None:
            for relative, content in package.files.items():
                self._allowed(package.module, relative)
                old = value["files"].get(relative)
                _require(old is None or old["module"] == package.module, "File belongs to another managed module")
                _require(type(content) is bytes and len(content) <= 128 * 1024**2, "Invalid package payload")
                desired[relative] = content
            for relative, old in value["files"].items():
                if old["module"] == package.module and relative not in desired:
                    desired[relative] = self._payload(old["original"]) if old.get("original") else None
        else:
            for relative, old in value["files"].items():
                self._allowed(old["module"], relative)
                desired[relative] = self._payload(old["original"]) if old.get("original") else None
        for relative, content in desired.items():
            old = value["files"].get(relative)
            before = file_hash(checked_path(self.root, relative))
            after = digest(content) if content is not None else None
            installing = package is not None and relative in package.files
            external = old is None and before is not None
            action = ("adopt" if external else "add" if before is None else "update") if installing else (
                "restore" if after is not None else "remove")
            files.append({"path": relative, "role": package.module if installing else old["module"],
                "action": action, "beforeSha256": before, "afterSha256": after,
                "originalSha256": before if external else (old or {}).get("original"),
                "backupRequired": before is not None and before != after, "wouldChange": before != after})
        body = {"schema": "gkms.install-preview.v1", "action": "install-" + package.module if package else "restore-all",
            "gameDirectory": str(self.root), "expectedRevision": revision,
            "gameAssemblySha256": file_hash(checked_path(self.root, "GameAssembly.dll")),
            "package": None if package is None else {"module": package.module, "version": package.version,
                "archiveSha256": metadata.get("archiveSha256"), "sourceMetadataSha256": digest(canonical({
                    key: value for key, value in metadata.items() if key != "_source_envelope"}))},
            "files": files, "requiresTakeoverConfirmation": any(row["action"] == "adopt" for row in files)}
        return body, desired

    def preview(self, package, metadata, revision):
        with file_lock(self.db / "lock"):
            return self._plan(package, metadata, revision)[0]

    def install(self, package, metadata, revision, operation_id, *, preview=None, approve_takeover=False):
        return self._transaction_v5(package, metadata, revision, operation_id, preview, approve_takeover)

    def restore(self, revision, operation_id, *, preview=None):
        return self._transaction_v5(None, {}, revision, operation_id, preview, False)

    def _transaction_v5(self, package, metadata, revision, operation_id, preview, approve_takeover):
        _require(isinstance(operation_id, str) and re.fullmatch("[A-Za-z0-9_-]{8,100}", operation_id), "Invalid transaction identity")
        signature = digest(canonical({"module": None if package is None else package.module,
            "payload": None if package is None else {k: digest(v) for k, v in package.files.items()},
            "metadata": metadata, "revision": revision}))
        with file_lock(self.db / "lock"):
            value = self.manifest()
            if value.get("operationId") == operation_id:
                _require(value.get("operationSignature") == signature, "Transaction ID reused for different work")
                return self.snapshot()
            actual, desired = self._plan(package, metadata, revision)
            if preview is not None:
                _require(canonical(actual) == canonical(preview), "Reviewed file hashes or source identity changed")
            _require(not actual["requiresTakeoverConfirmation"] or approve_takeover is True,
                "Explicit backup/takeover confirmation is required for external files")
            _require(type(approve_takeover) is bool, "Takeover confirmation must be explicit boolean")
            needed = sum(len(data) for data in desired.values() if data is not None)
            _require(min(shutil.disk_usage(self.root).free, shutil.disk_usage(self.db).free) > needed * 2 + 16 * 1024**2,
                "Insufficient disk space for transaction and backups")
            target, changes, new_dirs = deepcopy(value), [], []
            self._archive_legacy()
            for row in actual["files"]:
                relative = row["path"]; path = checked_path(self.root, relative)
                _require(file_hash(path) == row["beforeSha256"], "Target changed before original backup: " + relative)
                before = self._stage(path.read_bytes()) if row["beforeSha256"] is not None else None
                _require(before == row["beforeSha256"], "Original file changed during backup")
                after = self._stage(desired[relative]) if desired[relative] is not None else None
                changes.append({"path": relative, "before": before, "after": after})
                parent = path.parent
                while parent != self.root:
                    if not parent.exists(): new_dirs.append(parent.relative_to(self.root).as_posix())
                    parent = parent.parent
                if package is not None and relative in package.files:
                    old = value["files"].get(relative)
                    target["files"][relative] = {"module": package.module, "installed": after,
                        "original": old.get("original") if old else before}
                else:
                    target["files"].pop(relative, None)
            target.update(revision=uuid.uuid4().hex, operationId=operation_id, operationSignature=signature,
                directories=sorted(set(value["directories"] + new_dirs)))
            if package:
                target["modules"][package.module] = {**metadata, **package.metadata,
                    "version": package.version, "installedAt": time.time()}
            else:
                target["modules"] = {}
            journal = {"schema": self.JOURNAL_SCHEMA, "transactionId": operation_id,
                "old": value, "target": target, "changes": changes, "remove_all": package is None,
                "preview": actual, "previewSha256": digest(canonical(actual)), "approvedTakeover": approve_takeover,
                "sourceEnvelope": deepcopy(metadata.get("_source_envelope"))}
            write_json(self.journal_path, journal)
            self._finish_locked(journal)
            return self.snapshot()

    def _finish_locked(self, journal):
        self._validate_journal(journal)
        # The retained V4 writer clears its own pending file after publication.
        # Keep an independent same-transaction anchor until our receipt is durable.
        if not self.journal_path.exists():
            write_json(self.journal_path, journal)
        anchor = deepcopy(journal)
        anchor["pendingCopySha256"] = file_hash(self.journal_path)
        write_json(self.completion_path, anchor)
        # V4's write engine still enforces all-before validation, each-file CAS,
        # atomic writes, post-hash verification and durable manifest publication.
        super()._finish_locked(journal)
        self.receipts.mkdir(exist_ok=True)
        receipt = {"schema": "gkms.install-transaction-receipt.v5", "transactionId": journal["transactionId"],
            "beforeRevision": journal["old"]["revision"], "afterRevision": journal["target"]["revision"],
            "changes": journal["changes"], "previewSha256": journal["previewSha256"], "status": "completed",
            "recoveryDirection": journal.get("recoveryIntent", "forward")}
        write_json(self.receipts / (journal["transactionId"] + ".json"), receipt)
        self.completion_path.unlink()

    def _pending_source(self):
        if self.completion_path.exists():
            if self.journal_path.exists():
                anchor = json.loads(self.completion_path.read_bytes())
                _require(anchor.get("pendingCopySha256") == file_hash(self.journal_path),
                    "Recovery preview journal copies differ; no target may change")
            return self.completion_path
        return self.journal_path

    def _write_target(self, relative, data):
        guard = getattr(self, "write_guard", None)
        if guard is not None: guard()
        return super()._write_target(relative, data)

    def recovery_preview(self, direction="forward"):
        _require(direction in {"forward", "rollback"}, "Recovery direction must be forward or rollback")
        with file_lock(self.db / "lock"):
            source = self._pending_source()
            _require(source.exists(), "No V5 transaction is awaiting recovery")
            raw = source.read_bytes(); journal = json.loads(raw)
            self._validate_journal(journal)
            rows = []
            for row in journal["changes"]:
                actual = file_hash(checked_path(self.root, row["path"]))
                _require(actual in (row["before"], row["after"]), "Recovery found an external file change")
                destination = row["after"] if direction == journal.get("recoveryIntent", "forward") else row["before"]
                if destination is not None: self._payload(destination)
                rows.append({"path": row["path"], "role": "recovery", "action": "restore" if destination else "remove",
                    "beforeSha256": actual, "afterSha256": destination, "originalSha256": row["before"],
                    "backupRequired": False, "wouldChange": actual != destination})
            return {"schema": "gkms.install-preview.v1", "action": "recover", "gameDirectory": str(self.root),
                "expectedRevision": self.manifest()["revision"], "gameAssemblySha256": journal["preview"]["gameAssemblySha256"],
                "package": journal["preview"]["package"], "files": rows, "requiresTakeoverConfirmation": False,
                "transactionId": journal["transactionId"], "journalSha256": digest(raw), "recoveryDirection": direction}

    def recover(self, *, preview=None, direction="forward"):
        if not self._pending_source().exists():
            _require(not self.legacy.journal_path.exists(), "Recover the retained V4 journal with its original implementation first")
            return self.snapshot()
        actual = self.recovery_preview(direction)
        _require(preview is None or canonical(actual) == canonical(preview), "Recovery preview or file state changed")
        with file_lock(self.db / "lock"):
            raw = self._pending_source().read_bytes()
            _require(digest(raw) == actual["journalSha256"], "Original transaction journal changed")
            journal = json.loads(raw)
            self._validate_journal(journal)
            if direction != journal.get("recoveryIntent", "forward"):
                reverse = deepcopy(journal)
                reverse["old"], reverse["target"] = journal["target"], journal["old"]
                reverse["recoveryIntent"] = direction
                reverse["changes"] = [{"path": row["path"], "before": row["after"], "after": row["before"]}
                    for row in journal["changes"]]
                self._finish_locked(reverse)
                self._prune_empty_dirs(journal["target"]["directories"])
            else:
                _require(direction == "rollback" or file_hash(checked_path(self.root, "GameAssembly.dll")) == actual["gameAssemblySha256"],
                    "Game version changed; forward installation cannot continue")
                self._finish_locked(journal)
            return self.snapshot()

    def snapshot(self):
        result = super().snapshot()
        value = self.manifest()
        result["files"] = [{"path": path, "action": "replaced" if record.get("original") else "added",
            "installedSha256": record["installed"], "originalSha256": record.get("original"),
            "restoreAction": "restore" if record.get("original") else "remove"} for path, record in value["files"].items()]
        result["ledgerSchema"] = self.SCHEMA
        result["legacyLedgerPreserved"] = self.legacy.manifest_path.exists()
        result["legacyOperationPending"] = self.legacy.journal_path.exists()
        if result["legacyOperationPending"]:
            result.update(operationPending=True, safeToModify=False)
        if self._pending_source().exists():
            journal = json.loads(self._pending_source().read_bytes())
            result.update(operationPending=True, safeToModify=False)
            result["pendingTransactionId"] = journal.get("transactionId")
        return result
