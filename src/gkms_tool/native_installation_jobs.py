"""Complete one approved, durable V5 file transaction under the existing helper.

No new installation plan, arbitrary executable, game input, launch, or UAC is
available here. All addresses are fixed roles beneath the native state root.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from .application_paths import game_directory, native_state_root, state_root
from .gui_setup.packages import MAX_ARCHIVE, SetupError, control_package, translation_package
from .gui_setup.transactions import checked_path, file_hash
from .gui_setup.transactions_v5 import InstallStore, canonical

_ID = re.compile(r"[a-f0-9]{32}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_OPERATION = re.compile(r"[A-Za-z0-9_-]{8,100}\Z")


def _require(condition, reason):
    if not condition:
        raise SetupError(reason)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _stamp(path):
    value = checked_path(path.parent, path.name).stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _identifier(value, pattern, reason):
    _require(isinstance(value, str) and pattern.fullmatch(value), reason)
    return value


def _read(base, relative, *, expected=None, limit=65536, json_object=True):
    path = checked_path(Path(base), relative)
    before = path.stat()
    _require(before.st_size <= limit, "Installation evidence exceeds its size bound")
    raw = path.read_bytes()
    after = path.stat()
    _require((before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns),
             "Installation evidence changed during read")
    digest = _sha(raw)
    _require(expected is None or digest == expected, "Installation evidence SHA differs")
    value = json.loads(raw) if json_object else raw
    if json_object:
        _require(isinstance(value, dict), "Installation evidence must be an object")
    return value, path, digest


def _intent(payload, root):
    base = native_state_root(root) / "gui_setup"
    intent, path, digest = _read(base, "elevation_intents/" + payload["intent_id"] + ".json", expected=payload["intent_sha256"])
    expected = {"schema", "operationId", "gameDirectory", "transactionId", "journalSha256", "previewId",
                "previewSha256", "approveTakeover", "direction"}
    _require(set(intent) == expected and intent["schema"] == "gkms.installation-elevation-intent.v1", "Installation intent fields differ")
    for name in ("operationId", "transactionId"):
        _identifier(intent[name], _OPERATION, "Invalid installation operation identity")
    _identifier(intent["previewId"], _ID, "Invalid installation preview identity")
    for name in ("journalSha256", "previewSha256"):
        _identifier(intent[name], _SHA, "Invalid installation evidence SHA")
    _require(type(intent["approveTakeover"]) is bool and intent["direction"] in {"forward", "rollback"}, "Invalid installation approval/direction")
    _require(isinstance(intent["gameDirectory"], str) and Path(intent["gameDirectory"]).is_absolute(), "Installation target must be absolute")
    return base, intent, path, digest


def validate_payload(payload, *, root):
    _require(isinstance(payload, dict) and set(payload) == {"intent_id", "intent_sha256", "expected_run_id"}, "Invalid complete-installation payload fields")
    _identifier(payload["intent_id"], _ID, "Invalid installation intent ID")
    _identifier(payload["intent_sha256"], _SHA, "Invalid installation intent SHA")
    _require(payload["expected_run_id"] is None, "Installation completion requires no active cultivation")
    _intent(payload, Path(root).resolve())
    return dict(payload)


def _forward(journal):
    value = deepcopy(journal)
    value.pop("pendingCopySha256", None)
    orientation = value.pop("recoveryIntent", "forward")
    _require(orientation in {"forward", "rollback"}, "Unknown saved recovery orientation")
    if orientation == "rollback":
        value["old"], value["target"] = value["target"], value["old"]
        value["changes"] = [{"path": row["path"], "before": row["after"], "after": row["before"]} for row in value["changes"]]
    return value


def _rows(preview):
    rows = preview.get("files")
    _require(isinstance(rows, list) and len(rows) <= 30000, "Invalid approved file inventory")
    result = {}
    for row in rows:
        _require(isinstance(row, dict) and isinstance(row.get("path"), str) and row["path"] not in result, "Duplicate/invalid approved file role")
        for field in ("beforeSha256", "afterSha256", "originalSha256"):
            value = row.get(field)
            _require(value is None or isinstance(value, str) and _SHA.fullmatch(value), "Invalid approved file hash")
        result[row["path"]] = row
    return result


def _source_package(source, original, base, root, references):
    if source is None:
        _require(original["action"] == "restore-all" and original.get("package") is None, "Package source is missing")
        return None
    _require(isinstance(source, dict) and set(source) == {"module", "kind", "archiveSha256", "metadata"}, "Installation source fields differ")
    archive_sha = _identifier(source["archiveSha256"], _SHA, "Invalid source archive SHA")
    metadata = source["metadata"]
    _require(isinstance(metadata, dict) and metadata.get("archiveSha256") == archive_sha, "Source metadata archive identity differs")
    raw, path, digest = _read(base, "archives/" + archive_sha + ".zip", expected=archive_sha, limit=MAX_ARCHIVE, json_object=False)
    references.append((path, digest))
    if source["kind"] == "bundled-control" and source["module"] == "control":
        bundled, bundled_path, bundled_sha = _read(root, "assets/control/gkms-control.zip", expected=archive_sha, limit=MAX_ARCHIVE, json_object=False)
        _require(raw == bundled, "Installation archive differs from this application slot")
        references.append((bundled_path, bundled_sha))
        package = control_package(raw, original["gameAssemblySha256"])
        _require(package.metadata.get("buildProfile") == "public", "Only the formal public control package may be elevated")
    elif source["kind"] == "verified-translation" and source["module"] == "translation":
        from .gui_setup.releases import source_url
        tag = metadata.get("releaseTag")
        _require(isinstance(tag, str) and tag, "Verified translation release tag is missing")
        _require(source_url(metadata.get("sourceUrl"))["repository"] == metadata.get("repository"), "Verified translation repository differs")
        for key in ("releaseId", "assetId"):
            _require(type(metadata.get(key)) in (int, str) and bool(metadata[key]), "Verified translation release/asset identity is missing")
        upstream_digest = metadata.get("digest")
        _require(upstream_digest in (None, "", "sha256:" + archive_sha), "Verified translation asset digest differs")
        package = translation_package(raw, tag)
    else:
        raise SetupError("Unsupported fixed installation source")
    declared = original.get("package") or {}
    _require(declared.get("sourceMetadataSha256") == _sha(canonical(metadata)), "Reviewed source metadata identity differs")
    _require(original["action"] == "install-" + package.module and declared.get("module") == package.module
             and declared.get("version") == package.version and declared.get("archiveSha256") == archive_sha,
             "Approved package identity differs from verified source")
    return package


def _verify_original(store, journal, package):
    forward = _forward(journal)
    original = forward["preview"]
    _require(original.get("schema") == "gkms.install-preview.v1" and forward["previewSha256"] == _sha(canonical(original)), "Original transaction preview identity differs")
    approved, changes = _rows(original), {row["path"]: row for row in forward["changes"]}
    _require(set(approved) == set(changes), "Journal writes differ from approved inventory")
    old, target = forward["old"]["files"], forward["target"]["files"]
    _require({k: v for k, v in old.items() if k not in changes} == {k: v for k, v in target.items() if k not in changes}, "Unreviewed ledger ownership change")
    if package is None:
        _require(set(changes) == set(old) and not target, "Restore must retain its original managed inventory")
    else:
        _require(set(changes) == set(package.files) | {name for name, row in old.items() if row["module"] == package.module}, "Package change inventory is incomplete")
    for name, change in changes.items():
        row = approved[name]
        owner = target.get(name) or old.get(name)
        _require(owner is not None and row.get("role") == owner["module"], "Approved file role differs")
        store._allowed(owner["module"], name)
        _require(change["before"] == row["beforeSha256"] and change["after"] == row["afterSha256"], "Journal hashes differ from original approval")
        if name in old:
            _require(change["before"] == old[name]["installed"], "Original managed bytes differ from journal")
        elif change["before"] is not None:
            _require(forward.get("approvedTakeover") is True and original.get("requiresTakeoverConfirmation") is True
                     and row.get("action") == "adopt", "External file takeover lacks original explicit approval")
        if package is not None and name in package.files:
            _require(change["after"] == _sha(package.files[name]) and target.get(name, {}).get("installed") == change["after"], "Journal target differs from verified package bytes")
        else:
            _require(name in old and name not in target and change["after"] == old[name].get("original"), "Restore target differs from retained original bytes")
        for sha in (change["before"], change["after"]):
            if sha is not None:
                store._payload(sha)
    return forward


def execute_installation_job(payload, job_dir, root, runner, script_hashes, public_executor):
    from . import native_maintenance as maintenance
    from .run_identity import load_active_run
    from .runtime_command_client import _claim_lock
    root = Path(root).resolve()
    payload = validate_payload(payload, root=root)
    base, intent, intent_path, intent_sha = _intent(payload, root)
    references = [(intent_path, intent_sha)]
    game = Path(intent["gameDirectory"]).resolve()
    expected_game = Path(public_executor.config["game"]).resolve() if public_executor is not None else game_directory(root)
    _require(expected_game is not None and game == Path(expected_game).resolve(), "Installation target differs from the configured game")
    job = Path(job_dir).resolve()
    sessions = native_state_root(root) / "native_maintenance/sessions"
    _require(job.parent.name == "jobs" and job.parents[2] == sessions.resolve()
             and _ID.fullmatch(job.name) and _ID.fullmatch(job.parents[1].name), "Installation job is outside its fixed session role")
    checked_path(job, "installation_completion.json")
    if public_executor is None:
        _require(isinstance(script_hashes, dict) and set(script_hashes) == set(maintenance.FIXED_SCRIPTS), "Original maintenance executor pins are required")
    game_sha = file_hash(checked_path(game, "GameAssembly.dll"))
    _require(game_sha is not None, "Selected GameAssembly is unavailable")
    verified_stamps = {}

    def fence(*, full=False):
        if public_executor is not None:
            if full:
                public_executor.validate_unchanged()
            _require(Path(public_executor.config["root"]).resolve() == root and Path(public_executor.config["game"]).resolve() == game,
                     "Public installation configuration differs")
            configured = game_directory(root)
            _require(configured is not None and Path(configured).resolve() == game, "Configured game changed")
            _require(public_executor.platform.processes("gakumas.exe") == [], "Close the game before installation completion")
        else:
            _require(game_directory(root) is not None and Path(game_directory(root)).resolve() == game, "Configured game changed")
            maintenance.require_game_closed()
        maintenance.check_pending(root=root)
        _require(load_active_run(root=state_root(root) / "runs") is None, "Installation completion cannot operate during an active run")
        if full:
            fixed = [(checked_path(Path(runner), name), expected) for name, expected in script_hashes.items()]
            fixed += references + [(checked_path(game, "GameAssembly.dll"), game_sha)]
            if public_executor is not None and public_executor.config.get("manifest_sha256"):
                fixed.append((root / "gkms-app-package.json", public_executor.config["manifest_sha256"]))
            for path, expected in fixed:
                before = _stamp(path)
                _require(file_hash(path) == expected and _stamp(path) == before, "Approved installation evidence changed")
                verified_stamps[path] = before
        else:
            # Translation packages can contain thousands of files. Rehashing
            # the whole slot/archives/GameAssembly per write would multiply
            # hundreds of MB by that count. Full hashes bracket the operation;
            # each individual write retains closed-game and file-identity fences.
            for path, expected in verified_stamps.items():
                _require(_stamp(path) == expected, "Approved installation evidence changed")

    with _claim_lock():
        fence(full=True)
        envelope, preview_path, preview_file_sha = _read(base, "previews/" + intent["previewId"] + ".json", limit=32 * 1024**2)
        references.append((preview_path, preview_file_sha))
        _require(envelope.get("schema") == "gkms.persisted-install-preview.v1", "Unknown persisted installation preview")
        preview = deepcopy(envelope.get("preview"))
        _require(isinstance(preview, dict) and preview.get("schema") == "gkms.install-preview.v1", "Invalid approved installation preview")
        _require(preview.pop("previewId", None) == intent["previewId"] and preview.pop("previewSha256", None) == intent["previewSha256"]
                 and _sha(canonical(preview)) == intent["previewSha256"], "Approved preview identity differs")
        _require(Path(preview["gameDirectory"]).resolve() == game, "Approved preview targets another game")
        store = InstallStore(game, base / "ledgers")
        pending_path = store._pending_source()
        journal, _, _ = _read(store.db, pending_path.name, expected=intent["journalSha256"], limit=64 * 1024**2)
        store._validate_journal(journal)
        _require(journal["transactionId"] == intent["transactionId"], "Original transaction identity differs")
        _require(envelope.get("source") == journal.get("sourceEnvelope"), "Approved source envelope differs from original transaction")
        package = _source_package(envelope.get("source"), journal["preview"], base, root, references)
        forward = _verify_original(store, journal, package)
        if preview["action"] == "recover":
            reviewed_sha = _identifier(preview.get("journalSha256"), _SHA, "Recovery preview lacks its reviewed source hash")
            _require(envelope.get("recoverySourceSha256") == reviewed_sha and preview.get("transactionId") == intent["transactionId"]
                     and preview.get("recoveryDirection") == intent["direction"], "Recovery approval identity/direction differs")
            reviewed, path, digest = _read(base, "recovery_sources/" + reviewed_sha + ".json", expected=reviewed_sha, limit=64 * 1024**2)
            references.append((path, digest))
            store._validate_journal(reviewed)
            _require(canonical(_forward(reviewed)) == canonical(forward), "Current journal is not the reviewed transaction lineage")
        else:
            _require(intent["direction"] == "forward" and canonical(preview) == canonical(forward["preview"]), "Initial approval differs from original transaction")
            _require(not preview.get("requiresTakeoverConfirmation") or intent["approveTakeover"] is True, "External takeover was not explicitly approved")
        if intent["direction"] == "forward":
            _require(game_sha == preview["gameAssemblySha256"], "Game version changed before forward completion")
        approved = _rows(preview)
        changes = {row["path"]: row for row in journal["changes"]}
        _require(set(approved) == set(changes), "Approved recovery inventory differs")
        after_key = "after" if intent["direction"] == journal.get("recoveryIntent", "forward") else "before"
        for name, row in changes.items():
            _require(approved[name]["afterSha256"] == row[after_key], "Approved destination differs from transaction orientation")
            _require(approved[name]["beforeSha256"] in (row["before"], row["after"]), "Reviewed source bytes are unrelated to this transaction")
        fence(full=True)
        _require(file_hash(store._pending_source()) == intent["journalSha256"], "Transaction changed before privileged completion")
        store.write_guard = fence
        recovery_preview = store.recovery_preview(intent["direction"])
        store.recover(preview=recovery_preview, direction=intent["direction"])
        fence(full=True)
        for name, row in approved.items():
            _require(file_hash(checked_path(game, name)) == row["afterSha256"], "Installation completion hash verification failed")
        _require(not store._pending_source().exists(), "Original transaction is still awaiting completion")
        receipt, receipt_path, receipt_sha = _read(store.receipts, intent["transactionId"] + ".json", limit=32 * 1024**2)
        manifest_sha = file_hash(store.manifest_path)
        _require(receipt.get("transactionId") == intent["transactionId"] and receipt.get("status") == "completed"
                 and receipt.get("recoveryDirection") == intent["direction"] and receipt.get("afterRevision") == store.manifest()["revision"],
                 "Original transaction receipt is unconfirmed")
        result = {"schema": "gkms.installation-elevated-result.v1", "status": "completed", "intent_id": payload["intent_id"],
            "operationId": intent["operationId"], "transactionId": intent["transactionId"], "previewId": intent["previewId"],
            "direction": intent["direction"], "revision": receipt["afterRevision"], "manifestSha256": manifest_sha,
            "receiptSha256": receipt_sha, "game_started": False, "game_input_submitted": False, "uac_requested": False}
        maintenance.write_json(job / "installation_completion.json", result)
        return result
