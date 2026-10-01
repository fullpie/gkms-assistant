"""Continue one already-journaled installer transaction through its fixed helper."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .packages import SetupError
from .transactions import file_hash, write_json


class ElevatedInstallation:
    def __init__(self, *, root, data, store, operation_id, preview, approve_takeover, pool,
                 ensure=None, submit=None, read_result=None):
        from .. import native_maintenance as maintenance
        self.store, self.data, self.operation_id = store, Path(data), operation_id
        self.pool = pool
        self.submit = submit or maintenance.submit
        self.read_result = read_result or maintenance.read_result
        source = store._pending_source()
        journal = json.loads(source.read_bytes())
        direction = preview.get("recoveryDirection", "forward")
        intent_id = hashlib.sha256(("installation:" + operation_id).encode()).hexdigest()[:32]
        self.request_id = intent_id
        intent = {"schema": "gkms.installation-elevation-intent.v1", "operationId": operation_id,
            "gameDirectory": str(store.root), "transactionId": journal["transactionId"],
            "journalSha256": file_hash(source), "previewId": preview["previewId"],
            "previewSha256": preview["previewSha256"], "approveTakeover": approve_takeover, "direction": direction}
        path = self.data / "elevation_intents" / (intent_id + ".json")
        if path.exists():
            if json.loads(path.read_bytes()) != intent:
                raise SetupError("The same elevated operation cannot replace its recorded transaction intent")
        else:
            write_json(path, intent)
        self.payload = {"intent_id": intent_id, "intent_sha256": file_hash(path), "expected_run_id": None}
        bootstrap_id = hashlib.sha256(("installation-helper:" + operation_id).encode()).hexdigest()[:32]
        self.bootstrap_id = bootstrap_id
        self.ensure = ensure or maintenance.ensure_public_helper
        self.future = pool.submit(self.ensure, bootstrap_id)
        self.session_id = None
        self.sent = False

    def poll(self):
        if not self.sent:
            if not self.future.done(): return None
            ready = self.future.result()
            if ready.get("status") == "pending":
                # Same bootstrap ID; ensure reuses the original process/prompt.
                self.future = self._next_bootstrap()
                return None
            if ready.get("status") != "ready":
                raise SetupError("INSTALL_ELEVATION_REQUIRED: " + str(ready.get("status", "unknown")))
            response = self.submit("complete-installation", self.payload, request_id=self.request_id, timeout=0)
            self.session_id = response.get("session_id")
            if not self.session_id:
                raise SetupError("The existing elevated installation request has no session identity")
            self.sent = True
        response = self.read_result(self.session_id, self.request_id)
        if response is None or response.get("status") in ("pending", "running"):
            return None
        if response.get("status") != "completed":
            raise SetupError("INSTALLATION_RECOVERY_REQUIRED: " + str(response.get("error", "helper outcome unknown")))
        result = response.get("result") or {}
        if result.get("transactionId") != self._transaction_id():
            raise SetupError("Elevated completion belongs to another original installation transaction")
        snapshot = self.store.snapshot()
        if (snapshot["operationPending"] or snapshot.get("conflicts") or result.get("revision") != snapshot["revision"]
                or result.get("manifestSha256") != file_hash(self.store.manifest_path)
                or result.get("receiptSha256") != file_hash(self.store.receipts / (result["transactionId"] + ".json"))):
            raise SetupError("Elevated installer completion has not been verified in the same ledger")
        return {"ok": True, "committed": True, "operationId": self.operation_id,
            "transactionId": result["transactionId"], "elevated": True, "snapshot": snapshot}

    def _transaction_id(self):
        path = self.data / "elevation_intents" / (self.request_id + ".json")
        if file_hash(path) != self.payload['intent_sha256']:
            raise SetupError('Original elevated installation intent changed')
        return json.loads(path.read_bytes())["transactionId"]

    def _next_bootstrap(self):
        # A completed Future keeps the owner pump nonblocking while status is
        # checked by the one existing preparation worker.
        return self.pool.submit(self.ensure, self.bootstrap_id)


def begin_elevation(**kwargs):
    return ElevatedInstallation(**kwargs)
