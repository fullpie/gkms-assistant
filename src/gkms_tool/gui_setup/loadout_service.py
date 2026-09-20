"""Owner-thread web projection of the existing card-library panel and workers."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import threading

from ..account_loadout import LoadoutConstraints, LoadoutSelection, save_loadout_constraints, validate_selection

ACTIONS = frozenset({"loadout.status", "loadout.refresh", "loadout.read", "loadout.recommend", "loadout.constraints", "loadout.apply"})
READ_ACTIONS = frozenset({"loadout.status"})


def _plain(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _read_request(path):
    if path.stat().st_size > 65536:
        raise ValueError("Pending loadout descriptor is too large")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Pending loadout descriptor must be an object")
    return value


def _proposal(row):
    value = _plain(asdict(row))
    key = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return {"id": key, **value}


class LoadoutService:
    actions = ACTIONS
    read_actions = READ_ACTIONS

    def __init__(self, root, panel):
        self.root, self.panel = Path(root).resolve(), panel
        self.owner_thread = threading.get_ident()
        self._operations = {}
        self._operation_errors = {}

    def _owner(self):
        if threading.get_ident() != self.owner_thread:
            raise RuntimeError("配裝服務必須由原 GUI owner 執行。")

    def _pending(self):
        panel = self.panel
        pending = getattr(panel, "_pending_apply", None)
        request = getattr(pending, "request", None)
        if request is not None:
            return request.to_dict()
        path = getattr(panel, "_pending_path", None)
        if path is not None and Path(path).is_file():
            return _read_request(Path(path))
        return None

    def _has_pending_files(self):
        path = Path(self.panel._pending_path)
        return bool(list(path.parent.glob("*pending*.json")))

    def can_reconcile_pending(self):
        """Only the panel's exact original loadout request can pass this guard."""
        self._owner()
        panel = self.panel
        if panel is None or panel._busy or panel._game_busy or panel._invalid_pending:
            return False
        request = getattr(getattr(panel, "_pending_apply", None), "request", None)
        if request is None or request.command != "loadout.apply":
            return False
        original = request.to_dict()
        path = Path(panel._pending_path)
        try:
            if not path.is_file() or _read_request(path) != original:
                return False
            for descriptor in path.parent.glob("*pending*.json"):
                if descriptor.name not in {path.name, "pending_action.json"} or _read_request(descriptor) != original:
                    return False
            for directory in ("inbox", "running"):
                for descriptor in (path.parent / directory).glob("*.json"):
                    if _read_request(descriptor) != original:
                        return False
        except (OSError, ValueError, TypeError):
            return False
        return True

    def snapshot(self):
        self._owner()
        panel = self.panel
        if panel is None:
            return {"schema": "gkms.gui-loadout.v1", "available": False, "reason": "CARD_LIBRARY_UNAVAILABLE"}
        inventory = panel.snapshot
        try:
            pending = self._pending()
            pending_error = None
        except (OSError, ValueError, TypeError) as error:
            pending, pending_error = None, str(error)
        blocked = bool(panel._busy or panel._game_busy or panel._invalid_pending or pending_error or pending or self._has_pending_files())
        proposals = [_proposal(row) for row in panel._recommendations if not row.selection.memory_ids]
        scope = panel.selection_scope() if panel.selection_scope is not None else (None, None)
        raw = None if inventory is None else inventory.to_dict()
        names = {}
        if raw is not None:
            for family in ("idol_cards", "support_cards", "memories"):
                for row in raw[family]:
                    for key in (row.get("card_id"), row.get("idol_card_id"), (row.get("produce_card") or {}).get("id")):
                        if key:
                            names[key] = panel.names.get(key, key)
        rentals = []
        for rental in panel._rentals.values():
            rentals.append({**asdict(rental), "name": panel.names.get(rental.card_id, rental.card_id)})
        return _plain({"schema": "gkms.gui-loadout.v1", "available": True, "status": panel.status.get(),
            "busy": bool(panel._busy), "game_busy": bool(panel._game_busy), "task": panel._task_kind,
            "inventory": raw, "inventory_source": "cached-validated-DLL-inventory" if raw is not None else None,
            "inventory_digest": None if inventory is None else inventory.content_digest, "names": names,
            "scope": {"produce_id": scope[0], "idol_card_id": scope[1]}, "constraints": asdict(panel.constraints),
            "rentals": rentals, "loadout": panel._loadout, "recommendations": proposals,
            "pending_apply": pending, "pending_invalid": bool(panel._invalid_pending or pending_error),
            "pending_error": pending_error, "can_reconcile_pending": self.can_reconcile_pending(),
            "capabilities": {"refresh": not blocked, "read": not blocked or self.can_reconcile_pending(),
                "recommend": not blocked and inventory is not None and panel.selection_scope is not None,
                "constraints": not blocked and inventory is not None and panel.selection_scope is not None,
                "apply": not blocked and inventory is not None and isinstance(panel._loadout, dict)
                    and panel._loadout.get("active_section") == "support"},
            "recommendation_sections": ["support"], "memory_selection_method": "game-native-auto-before-produce",
            "memory_exclusion_supported": False,
            "unsupported_memory_exclusions": list(panel.constraints.excluded_memory_ids),
            "recommendation_scope": "推薦支援卡；回憶於培育前使用遊戲自動編成。支援依已知初始能力與 SP 排序，不是整場預估分數。"})

    def _ready(self, action):
        panel = self.panel
        if panel is None:
            raise ValueError("牌庫介面尚未建立。")
        if panel._busy or panel._game_busy:
            raise ValueError("請先等待牌庫工作完成並停止培育，再變更編成。")
        if action == "loadout.read" and self.can_reconcile_pending():
            return
        if panel._invalid_pending or self._pending() is not None or self._has_pending_files():
            raise ValueError("原生操作仍待確認；僅能查詢原配裝請求，不能重送或更改方案。")

    def _constraints(self, data):
        panel, inventory = self.panel, self.panel.snapshot
        if inventory is None or panel.selection_scope is None:
            raise ValueError("請先更新牌庫並選擇培育偶像。")
        fields = {"locked_support_ids", "excluded_support_ids", "locked_memory_ids", "excluded_memory_ids", "locked_rental_key"}
        if not isinstance(data, dict) or set(data) - fields:
            raise ValueError("Unknown loadout constraint fields")
        constraints = LoadoutConstraints.from_dict(data)
        if constraints.excluded_memory_ids and constraints.excluded_memory_ids != panel.constraints.excluded_memory_ids:
            raise ValueError("本版回憶使用遊戲自動編成，不支援新增回憶排除；既有排除可明確解除，鎖定仍可保留。")
        supports = set(constraints.locked_support_ids) | set(constraints.excluded_support_ids)
        memories = set(constraints.locked_memory_ids) | set(constraints.excluded_memory_ids)
        if not supports <= {row.card_id for row in inventory.support_cards} or not memories <= {row.memory_id for row in inventory.memories}:
            raise ValueError("指定卡片不在目前帳號牌庫。")
        if constraints.locked_rental_key is not None and constraints.locked_rental_key not in {row.rental_key for row in panel._rentals.values()}:
            raise ValueError("指定借卡尚未從遊戲讀取。")
        produce_id, idol_card_id = panel.selection_scope()
        if idol_card_id not in {row.card_id for row in inventory.idol_cards}:
            raise ValueError("請選擇目前帳號持有的偶像卡。")
        save_loadout_constraints(panel.constraints_path, constraints, account_scope=inventory.account_scope,
            inventory_digest=inventory.content_digest, produce_id=produce_id, idol_card_id=idol_card_id)
        panel.constraints = constraints
        panel._recommendations = ()
        panel.recommendation_choice.configure(values=("手動編成",))
        panel.recommendation_choice_var.set("手動編成")
        panel._render()
        panel._show_recommendation()
        panel.status.set("已儲存編成偏好；遊戲中的編成尚未更改。")

    def _select_apply(self, payload):
        panel, inventory = self.panel, self.panel.snapshot
        if inventory is None or panel._loadout is None or panel.selection_scope is None:
            raise ValueError("請先更新牌庫並讀取遊戲編成。")
        if panel._loadout.get("active_section") != "support":
            raise ValueError("本版僅能套用支援卡；請停在支援卡編成頁，回憶由培育前的遊戲自動編成處理。")
        if payload.get("inventory_digest") != inventory.content_digest:
            raise ValueError("牌庫版本已變更，請重新檢視方案。")
        selected_id = payload.get("recommendation_id", "manual")
        choice = "手動編成"
        if selected_id == "manual":
            rentals = [(label, row) for label, row in panel._rentals.items() if row.rental_key == payload.get("rental_key")]
            if len(rentals) != 1:
                raise ValueError("請選擇一張目前可用的借卡。")
            label, rental = rentals[0]
            produce_id, idol_card_id = panel.selection_scope()
            selection = LoadoutSelection(idol_card_id, panel.constraints.locked_support_ids, rental,
                (), inventory.content_digest, inventory.account_scope, produce_id)
        else:
            matches = [(index, row) for index, row in enumerate(panel._recommendations) if _proposal(row)["id"] == selected_id]
            if len(matches) != 1:
                raise ValueError("編成建議已變更，請重新計算。")
            index, recommendation = matches[0]
            selection = recommendation.selection
            choice = f"建議 {index + 1}"
            label = None
        if (selection.produce_id, selection.idol_card_id) != panel.selection_scope():
            raise ValueError("培育偶像或模式已變更，請重新計算編成。")
        validate_selection(inventory, selection, constraints=panel.constraints, section="support")
        panel.recommendation_choice_var.set(choice)
        if label is not None:
            panel.rental_choice_var.set(label)
        panel._show_recommendation()

    def command(self, action, payload):
        self._owner()
        if action not in self.actions or not isinstance(payload, dict):
            raise ValueError("Unsupported loadout action")
        allowed = {"operationId"}
        if action == "loadout.constraints": allowed.add("constraints")
        if action == "loadout.apply": allowed |= {"inventory_digest", "recommendation_id", "rental_key"}
        if set(payload) - allowed:
            raise ValueError("Unknown loadout command field")
        operation_id = payload.get("operationId")
        if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", operation_id):
            raise ValueError("operationId is required")
        if action == "loadout.status":
            return {"ok": True, "snapshot": self.snapshot()}
        identity = _plain({"action": action, "payload": payload})
        if operation_id in self._operations:
            if self._operations[operation_id] != identity:
                raise ValueError("operationId cannot be reused with another loadout operation")
            if operation_id in self._operation_errors:
                raise RuntimeError(self._operation_errors[operation_id])
            return {"ok": True, "accepted": True, "replayed": True, "operationId": operation_id, "snapshot": self.snapshot()}
        self._ready(action)
        if action in {"loadout.recommend", "loadout.constraints", "loadout.apply"} and self.panel.snapshot is None:
            raise ValueError("請先更新完整帳號牌庫。")
        if action in {"loadout.recommend", "loadout.constraints", "loadout.apply"} and self.panel.selection_scope is None:
            raise ValueError("請先選擇培育偶像與模式。")
        if action == "loadout.recommend":
            produce_id, idol_card_id = self.panel.selection_scope()
            if not produce_id or idol_card_id not in {row.card_id for row in self.panel.snapshot.idol_cards}:
                raise ValueError("請先選擇目前帳號持有的偶像卡與培育模式。")
        if action == "loadout.apply": self._select_apply(payload)
        # Reserve before starting the panel's existing worker. A later HTTP
        # retry observes this operation; it never invokes another worker.
        self._operations[operation_id] = identity
        try:
            if action == "loadout.constraints": self._constraints(payload.get("constraints"))
            else:
                method = {"loadout.refresh": "refresh", "loadout.read": "read_loadout", "loadout.recommend": "recommend", "loadout.apply": "apply_loadout"}[action]
                getattr(self.panel, method)()
        except Exception as error:
            self._operation_errors[operation_id] = str(error)
            raise
        return {"ok": True, "accepted": True, "operationId": operation_id, "application_confirmed": False, "snapshot": self.snapshot()}
