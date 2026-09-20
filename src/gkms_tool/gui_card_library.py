"""Tk account library and manual card-data inspection, with isolated workers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
import json
from pathlib import Path
import queue
import sqlite3
import threading
import tkinter as tk
from tkinter import ttk

from .account_inventory import (
    AccountInventorySnapshot, load_account_inventory, save_account_inventory,
)
from .card_data_update import (
    CardDataUpdateCancelled, CardDataUpdateResult, DEFAULT_OUTPUT_ROOT,
    run_card_data_update,
)
from .master_db import DEFAULT_DATABASE
from .account_loadout import LoadoutConstraints, load_loadout_constraints, save_loadout_constraints
from .application_paths import state_root


DEFAULT_ACCOUNT_INVENTORY = state_root() / "account_inventory" / "current.json"


def refresh_account_inventory(
    path: Path = DEFAULT_ACCOUNT_INVENTORY,
) -> tuple[AccountInventorySnapshot, Mapping[str, object]]:
    """Read the full DLL collection and commit only a validated account snapshot."""
    from .runtime_command_client import RuntimeCommandClient

    result = RuntimeCommandClient().execute("read_inventory", timeout=15)
    if result.status != "ok":
        raise RuntimeError("遊戲未提供完整牌庫；請確認 DLL 已連線並已登入遊戲。")
    snapshot = AccountInventorySnapshot.from_dict(result.raw.get("inventory"))
    changes = save_account_inventory(path, snapshot)
    return snapshot, changes


def read_game_loadout_for_gui() -> Mapping[str, object]:
    """Resolve the game's lazy rental cache within the shared input lease."""
    from .account_loadout import read_available_loadout
    from .controller_client import _serialized_controller_request
    from .runtime_outer_runner import RuntimeOuterGateway

    with _serialized_controller_request(30):
        gateway = RuntimeOuterGateway()
        if gateway.pending_path.exists():
            outcome = gateway.resume()
            if outcome.status not in {"settled", "replan"}:
                raise RuntimeError("前一項遊戲操作尚未確認，請稍後再讀取編成。")
        current = gateway.read()
        refresh = [action for action in current.actions if action["action_id"] == "loadout.refresh_rentals"]
        if len(refresh) > 1:
            raise RuntimeError("借卡載入選項不唯一，請重新讀取遊戲狀態。")
        if refresh:
            outcome = gateway.execute(current, refresh[0]["target"])
            if outcome.status not in {"settled", "replan"}:
                raise RuntimeError("借卡載入尚未完成；先前操作會在下次讀取時核對，不會重送。")
        return read_available_loadout(gateway.client)


class _WorkerPanel(ttk.Frame):
    """Only the Tk polling callback touches widgets; workers use queue/Event."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent, padding=12)
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._cancel = threading.Event()
        self._busy = False
        self._closed = False
        self._after = self.after(100, self._poll)

    def shutdown(self) -> None:
        self._closed = True
        self._cancel.set()
        self.after_cancel(self._after)

    def _launch(self, operation: Callable[[], object]) -> None:
        if self._busy:
            return
        self._busy = True
        self._cancel.clear()
        self._set_busy(True)

        def worker() -> None:
            try:
                self._events.put(("complete", operation()))
            except CardDataUpdateCancelled:
                self._events.put(("cancelled", None))
            except Exception as error:
                self._events.put(("error", error))

        threading.Thread(target=worker, daemon=True, name="gkms-card-library").start()

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind != "progress":
                    self._busy = False
                    self._set_busy(False)
                try:
                    self._handle(kind, payload)
                except (OSError, TypeError, ValueError, KeyError) as error:
                    self.status.set(f"資料無法顯示：{error}")
        except queue.Empty:
            pass
        if not self._closed:
            self._after = self.after(100, self._poll)

    def _set_busy(self, busy: bool) -> None:
        raise NotImplementedError

    def _handle(self, kind: str, payload: object) -> None:
        raise NotImplementedError


class CardDataUpdatePanel(_WorkerPanel):
    def __init__(self, parent: tk.Misc, *, output_root: Path = DEFAULT_OUTPUT_ROOT) -> None:
        super().__init__(parent)
        self.output_root = output_root
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        ttk.Label(self, text="卡片資料更新", font=("Microsoft JhengHei UI", 15, "bold")).grid(
            row=0, column=0, sticky="w",
        )
        ttk.Label(
            self, text="檢查本機卡表的新增與變更，並驗證模擬器支援範圍。", style="Hint.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(5, 12))
        actions = ttk.Frame(self)
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        self.start_button = ttk.Button(actions, text="檢查卡片資料更新", command=self.start, style="Accent.TButton")
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="取消", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=8)
        self.runtime_check = tk.BooleanVar(value=False)
        self.runtime_option = ttk.Checkbutton(actions, text="一併驗證卡片執行（較久）", variable=self.runtime_check)
        self.runtime_option.pack(side="left", padx=8)
        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=130)
        self.progress.pack(side="right")
        self.detail = tk.Text(self, height=15, wrap="word", state="disabled", borderwidth=0, padx=12, pady=12)
        self.detail.grid(row=3, column=0, sticky="nsew")
        self.status = tk.StringVar(value="尚未檢查")
        ttk.Label(self, textvariable=self.status, style="Hint.TLabel", wraplength=850).grid(
            row=4, column=0, sticky="ew", pady=(10, 0),
        )
        self._show_text("更新檢查會保留現有模型。\n\n"
                        "既有效果的數值變動、全新卡片與新效果會分別列出。"
                        "需要驗證的資料不會自動成為正式模型。")
        self._load_last_report()

    def _show_text(self, value: str) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", value)
        self.detail.configure(state="disabled")

    def _load_last_report(self) -> None:
        try:
            marker = json.loads((self.output_root / "latest_report.json").read_text(encoding="utf-8"))
            self._render_report(Path(marker["report_path"]))
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def start(self) -> None:
        full = self.runtime_check.get()
        self.status.set("準備讀取卡片資料…")
        self._launch(lambda: run_card_data_update(
            output_root=self.output_root, runtime_acceptance=full,
            progress=lambda stage: self._events.put(("progress", stage)),
            cancelled=self._cancel.is_set,
        ))

    def cancel(self) -> None:
        self._cancel.set()
        self.cancel_button.configure(state="disabled")
        self.status.set("正在取消；目前編譯步驟結束後停止。")

    def _set_busy(self, busy: bool) -> None:
        self.start_button.configure(state="disabled" if busy else "normal")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        self.runtime_option.configure(state="disabled" if busy else "normal")
        self.progress.start(12) if busy else self.progress.stop()

    def _handle(self, kind: str, payload: object) -> None:
        if kind == "progress":
            if not self._cancel.is_set():
                self.status.set(str(payload))
        elif kind == "complete" and isinstance(payload, CardDataUpdateResult):
            self._render_report(payload.report_path)
        elif kind == "cancelled":
            self.status.set("已取消；目前模型未變更。")
        elif kind == "error":
            self.status.set(f"更新檢查失敗：{payload}")

    def _render_report(self, path: Path) -> None:
        report = json.loads(path.read_text(encoding="utf-8"))
        summary, difference = report["summary"], report["difference"]
        title = {
            "baseline-created": "已建立卡片資料基線", "unchanged": "卡片資料沒有變更",
            "review-required": "發現變更，待模型驗證", "blocked": "部分資料需要補足支援",
        }.get(summary["status"], "卡片資料檢查完成")
        counts = difference["counts"]
        lines = [title, "", f"目前卡片版本：{summary['card_versions']:,}",
                 f"新增 {counts.get('added', 0)} · 數值變更 {counts.get('numeric-change', 0)} · "
                 f"效果變更 {counts.get('semantic-change', 0)} · 移除 {counts.get('removed', 0)}", ""]
        validation = report["validation"]
        validation_label = {"compiled": "效果編譯完成", "accepted": "編譯與執行驗證完成",
                            "blocked": "仍有未支援項目", "not-run": "本次未執行"}
        lines.append("Common／Plan2 驗證：" + validation_label.get(validation["status"], validation["status"]))
        if difference["new_effect_types"]:
            lines.extend(("新效果種類：", *difference["new_effect_types"]))
        compatibility = {"unbound": "現有模型尚未綁定卡表版本", "matched": "卡表版本符合模型宣告",
                         "mismatch": "卡表與模型宣告版本不同", "unavailable": "目前模型資料不可用"}
        lines.extend(("", compatibility.get(summary["bundle_compatibility"], "模型相容性待確認"),
                      "本次僅檢查資料；模型訓練與切換仍需獨立完成。"))
        changes = difference["changes"]
        if changes:
            lines.extend(("", "變更項目："))
            labels = {"added": "新增", "removed": "移除", "numeric-change": "數值", "semantic-change": "效果"}
            lines.extend(f"{labels[row['category']]} · {row['id']}" for row in changes[:80])
            if len(changes) > 80:
                lines.append(f"其餘 {len(changes) - 80} 項保存在報告中。")
        lines.extend(("", f"報告：{path}"))
        self._show_text("\n".join(lines))
        self.status.set(title)


class CardLibraryPanel(_WorkerPanel):
    def __init__(
        self, parent: tk.Misc, *, path: Path = DEFAULT_ACCOUNT_INVENTORY,
        names: Mapping[str, str] | None = None,
        selection_scope: Callable[[], tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(parent)
        self.path, self.names = path, dict(names or {})
        self.selection_scope = selection_scope
        self.constraints = LoadoutConstraints()
        self._task_kind = "inventory"
        self._game_busy = False
        self._pending_apply = None
        self._invalid_pending = False
        from .runtime_command_client import DEFAULT_BRIDGE_ROOT
        self._pending_path = DEFAULT_BRIDGE_ROOT / "pending_loadout.json"
        self._loadout: Mapping[str, object] | None = None
        self._rentals: dict[str, object] = {}
        self._recommendations: tuple[object, ...] = ()
        self._catalog = None
        self.constraints_path = path.parent / "loadout_constraints.json"
        self._load_display_names()
        self.snapshot: AccountInventorySnapshot | None = None
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(top, text="我的卡片", font=("Microsoft JhengHei UI", 15, "bold")).pack(side="left")
        self.refresh_button = ttk.Button(top, text="從遊戲更新牌庫", command=self.refresh, style="Accent.TButton")
        self.refresh_button.pack(side="right")
        self.status = tk.StringVar(value="尚無帳號牌庫資料")
        ttk.Label(self, textvariable=self.status, style="Hint.TLabel", wraplength=1000).grid(
            row=1, column=0, sticky="ew", pady=(0, 10),
        )
        body = ttk.Panedwindow(self, orient="horizontal")
        body.grid(row=2, column=0, sticky="nsew")
        library = ttk.Frame(body)
        body.add(library, weight=3)
        library.columnconfigure(0, weight=1)
        library.rowconfigure(1, weight=1)
        self.kind = tk.StringVar(value="支援卡")
        filters = ttk.Frame(library)
        filters.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        kind = ttk.Combobox(filters, textvariable=self.kind, values=("支援卡", "回憶", "偶像卡"), state="readonly", width=12)
        kind.pack(side="left")
        kind.bind("<<ComboboxSelected>>", lambda _event: self._render())
        self.search_text = tk.StringVar()
        ttk.Entry(filters, textvariable=self.search_text, width=24).pack(side="right")
        ttk.Label(filters, text="搜尋", style="Hint.TLabel").pack(side="right", padx=8)
        self.tree = ttk.Treeview(library, columns=("name", "upgrade", "detail", "constraint"), show="headings", selectmode="extended")
        for key, label, width in (("name", "卡片", 210), ("upgrade", "強化", 115), ("detail", "效果／能力", 210), ("constraint", "偏好", 65)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width)
        self.tree.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(library, orient="vertical", command=self.tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.search_text.trace_add("write", lambda *_args: self._render())
        self.loadout_tabs = ttk.Notebook(body)
        body.add(self.loadout_tabs, weight=1)
        self.loadout_frame = ttk.Frame(self.loadout_tabs, padding=12)
        self.loadout_tabs.add(self.loadout_frame, text="選卡偏好")
        self.apply_frame = ttk.Frame(self.loadout_tabs, padding=12)
        self.loadout_tabs.add(self.apply_frame, text="遊戲編成")
        self.loadout_note = tk.StringVar(value="更新牌庫後，可依實際持有卡片準備編成。")
        ttk.Label(self.loadout_frame, textvariable=self.loadout_note, wraplength=280).pack(fill="x")
        ttk.Separator(self.loadout_frame).pack(fill="x", pady=14)
        ttk.Label(self.loadout_frame, text="選取左側支援卡或回憶，再指定偏好。", wraplength=260, style="Hint.TLabel").pack(fill="x", pady=(0, 10))
        self.constraint_buttons = []
        for label, action in (("鎖定所選", "lock"), ("排除所選", "exclude"), ("取消所選限制", "clear")):
            button = ttk.Button(self.loadout_frame, text=label, command=lambda value=action: self._change_constraint(value))
            button.pack(fill="x", pady=4)
            self.constraint_buttons.append(button)
        self.save_button = ttk.Button(self.loadout_frame, text="儲存編成偏好", command=self._save_constraints)
        self.save_button.pack(fill="x", pady=(14, 4))
        self.constraint_summary = tk.StringVar(value="支援卡已鎖定 0 / 5 · 回憶 0 / 4")
        ttk.Label(self.loadout_frame, textvariable=self.constraint_summary, wraplength=260, style="Hint.TLabel").pack(fill="x", pady=8)
        self.read_loadout_button = ttk.Button(self.apply_frame, text="讀取遊戲編成與借卡", command=self.read_loadout)
        self.read_loadout_button.pack(fill="x", pady=4)
        self.rental_choice_var = tk.StringVar()
        ttk.Label(self.apply_frame, text="手動選擇借卡", style="Hint.TLabel").pack(fill="x", pady=(10, 0))
        self.rental_choice = ttk.Combobox(self.apply_frame, textvariable=self.rental_choice_var, state="readonly", width=24)
        self.rental_choice.pack(fill="x", pady=6)
        self.recommend_button = ttk.Button(self.apply_frame, text="推薦支援卡", command=self.recommend, style="Accent.TButton")
        self.recommend_button.pack(fill="x", pady=(10, 6))
        self.recommendation_choice_var = tk.StringVar(value="手動編成")
        self.recommendation_choice = ttk.Combobox(self.apply_frame, textvariable=self.recommendation_choice_var, values=("手動編成",), state="readonly")
        self.recommendation_choice.pack(fill="x", pady=6)
        self.recommendation_choice.bind("<<ComboboxSelected>>", lambda _event: self._show_recommendation())
        self.recommendation_detail = tk.Text(self.apply_frame, height=13, width=24, wrap="word", state="disabled", borderwidth=0)
        self.recommendation_detail.pack(fill="both", expand=True, pady=8)
        self.apply_button = ttk.Button(self.apply_frame, text="套用支援卡", command=self.apply_loadout, state="disabled")
        self.apply_button.pack(fill="x", pady=4)
        ttk.Label(self.apply_frame, text="推薦支援卡；回憶於培育前使用遊戲自動編成。\n回憶鎖定偏好會在原生推薦後套用。", wraplength=260, style="Hint.TLabel").pack(fill="x", pady=6)
        self.load_cached()
        self._show_recommendation()
        self._restore_pending_apply()

    def _restore_pending_apply(self) -> None:
        if not self._pending_path.is_file():
            return
        from .runtime_command_client import RuntimeCommandPending, RuntimeCommandRequest

        try:
            raw = json.loads(self._pending_path.read_text(encoding="utf-8"))
            request = RuntimeCommandRequest(
                raw["request_id"], raw["session_generation"], raw["command"],
                raw.get("expected_revision"), raw.get("target"),
            )
            if request.command != "loadout.apply":
                raise ValueError("invalid pending loadout command")
            self._pending_apply = RuntimeCommandPending(request, "restored pending loadout")
            self.status.set("上次編成操作尚未確認，請先讀取遊戲編成查詢結果。")
            self._set_busy(False)
        except (OSError, ValueError, KeyError) as error:
            self.status.set(f"上次編成紀錄無法讀取：{error}")
            self._invalid_pending = True
            self._set_busy(False)

    @property
    def game_input_pending(self) -> bool:
        return self._busy and self._task_kind in {"apply", "loadout", "recommend"}

    def set_game_busy(self, busy: bool) -> None:
        self._game_busy = busy
        self._set_busy(self._busy)

    def read_loadout(self) -> None:
        if self._busy or self._game_busy:
            self.status.set("請先停止培育，再讀取遊戲編成頁。")
            return
        self._task_kind = "loadout"
        self.status.set("正在讀取遊戲目前編成與可用借卡…")

        def operation() -> object:
            from .account_loadout import reconcile_account_loadout_pending

            if self._pending_apply is not None:
                audit = reconcile_account_loadout_pending()
                return {"reconciled": audit, "loadout": audit["loadout"]}
            return {"loadout": read_game_loadout_for_gui()}

        self._launch(operation)

    def recommend(self) -> None:
        if self._busy or self._game_busy or self.snapshot is None or self.selection_scope is None:
            self.status.set("請先更新牌庫，並停止培育後計算編成。")
            return
        snapshot, constraints = self.snapshot, self.constraints
        produce_id, idol_card_id = self.selection_scope()
        self._task_kind = "recommend"
        self.status.set("正在依實際強化數值與 SP 加成推薦支援卡…")

        def operation() -> object:
            from .account_loadout import recommend_initial_supports
            from .passive_catalog import MasterPassiveCatalog

            loadout = read_game_loadout_for_gui()
            if self._catalog is None:
                self._catalog = MasterPassiveCatalog.load()
            proposals = recommend_initial_supports(
                snapshot, loadout, constraints=constraints, idol_card_id=idol_card_id,
                produce_id=produce_id, catalog=self._catalog,
            )
            return {"loadout": loadout, "recommendations": proposals}

        self._launch(operation)

    def _selected_recommendation(self):
        label = self.recommendation_choice_var.get()
        labels = tuple(f"建議 {index + 1}" for index in range(len(self._recommendations)))
        return self._recommendations[labels.index(label)] if label in labels else None

    def _show_recommendation(self) -> None:
        proposal = self._selected_recommendation()
        if proposal is None:
            text = "手動支援編成\n\n在「選卡偏好」鎖定 5 張支援卡，再選擇借卡。\n回憶於培育前使用遊戲自動編成，並保留指定鎖定。"
        else:
            selection = proposal.selection
            supports = "\n".join(self.names.get(key, key) for key in selection.support_card_ids)
            text = ("支援卡\n" + supports + "\n借卡：" + self.names.get(selection.borrowed_support.card_id, selection.borrowed_support.card_id)
                    + "\n\n回憶於培育前使用遊戲自動編成。"
                    + "\n\n推薦依據\n" + "\n".join(str(value) for value in proposal.reasons))
        self.recommendation_detail.configure(state="normal")
        self.recommendation_detail.delete("1.0", "end")
        self.recommendation_detail.insert("1.0", text)
        self.recommendation_detail.configure(state="disabled")

    def apply_loadout(self) -> None:
        if self._busy or self._game_busy or self._pending_apply is not None or self._invalid_pending:
            self.status.set("目前不能重複送出編成；請先完成正在進行的操作。")
            return
        if self.snapshot is None or self.selection_scope is None or self._loadout is None:
            self.status.set("請先更新牌庫，並讀取遊戲編成與借卡。")
            return
        if self._loadout.get("active_section") != "support":
            self.status.set("請停在支援卡編成頁；本版回憶由培育前的遊戲自動編成處理。")
            return
        proposal = self._selected_recommendation()
        if proposal is None and len(self.constraints.locked_support_ids) != 5:
            self.status.set("請先鎖定恰好 5 張自有支援卡。")
            return
        rental = proposal.selection.borrowed_support if proposal is not None else self._rentals.get(self.rental_choice_var.get())
        if rental is None:
            self.status.set("請先選擇一張目前可用的借卡。")
            return
        from .account_loadout import LoadoutSelection

        produce_id, idol_card_id = self.selection_scope()
        selection = proposal.selection if proposal is not None else LoadoutSelection(
            idol_card_id, self.constraints.locked_support_ids, rental,
            (), self.snapshot.content_digest,
            self.snapshot.account_scope, produce_id,
        )
        if selection.produce_id != produce_id or selection.idol_card_id != idol_card_id:
            self.status.set("培育偶像或模式已變更，請重新計算編成。")
            return
        if selection.memory_ids:
            self.status.set("舊方案包含自訂回憶；請重新推薦支援卡。")
            return
        snapshot = self.snapshot
        self._task_kind = "apply"
        self.status.set("正在核對版本並套用遊戲編成…")

        def operation() -> object:
            from .account_loadout import apply_account_loadout

            return apply_account_loadout(snapshot, selection, section="support")

        self._launch(operation)

    def _render_loadout(self, loadout: Mapping[str, object]) -> None:
        from .account_loadout import BorrowedSupportCard

        if self.snapshot is None or loadout.get("account_scope") != self.snapshot.account_scope:
            raise ValueError("遊戲編成與目前牌庫帳號不同，請重新更新牌庫。")
        rentals = {}
        for row in loadout.get("rental_support_cards", ()):
            rental = BorrowedSupportCard(**{name: row[name] for name in (
                "rental_key", "card_id", "level", "plan_type", "expires_at",
            )})
            label = f"{self.names.get(rental.card_id, rental.card_id)} · Lv.{rental.level} · {rental.rental_key[-6:]}"
            rentals[label] = rental
        self._loadout = loadout
        self._rentals = rentals
        labels = tuple(self._rentals)
        self.rental_choice.configure(values=labels)
        if self.rental_choice_var.get() not in self._rentals:
            self.rental_choice_var.set(labels[0] if labels else "")
        self._set_busy(False)

    def _load_display_names(self) -> None:
        from .nia_idol_catalog import DEFAULT_MASTER_TRANSLATION_DIR, _load_translation_rows

        try:
            self.names.update(_load_translation_rows(DEFAULT_MASTER_TRANSLATION_DIR / "SupportCard.json", "SupportCard"))
            with sqlite3.connect(DEFAULT_DATABASE.as_uri() + "?mode=ro", uri=True) as connection:
                self.names.update(dict(connection.execute("SELECT id,name FROM card WHERE upgrade_count=0")))
        except (OSError, ValueError, sqlite3.Error):
            # Identity remains explicit if a local display-name source is absent.
            pass

    def _restore_constraints(self) -> None:
        if self.snapshot is None or not self.constraints_path.is_file():
            return
        try:
            self.constraints = load_loadout_constraints(self.constraints_path, account_scope=self.snapshot.account_scope)
        except (OSError, ValueError):
            self.constraints = LoadoutConstraints()

    def _change_constraint(self, action: str) -> None:
        selected = set(self.tree.selection())
        if not selected or self.snapshot is None:
            self.status.set("請先選取支援卡或回憶。")
            return
        if self.kind.get() == "偶像卡":
            self.status.set("培育偶像請在培育頁選擇。")
            return
        suffix = "support_ids" if self.kind.get() == "支援卡" else "memory_ids"
        if suffix == "memory_ids" and action == "exclude":
            self.status.set("本版不提供回憶排除；回憶由遊戲自動編成。可鎖定指定回憶，或解除舊排除設定。")
            return
        locked = set(getattr(self.constraints, "locked_" + suffix)) - selected
        excluded = set(getattr(self.constraints, "excluded_" + suffix)) - selected
        if action == "lock":
            locked |= selected
        elif action == "exclude":
            excluded |= selected
        try:
            self.constraints = replace(self.constraints, **{
                "locked_" + suffix: tuple(sorted(locked)),
                "excluded_" + suffix: tuple(sorted(excluded)),
            })
        except ValueError:
            self.status.set("最多可鎖定 5 張自有支援卡、4 個回憶。")
            return
        self._render()
        self._recommendations = ()
        self.recommendation_choice.configure(values=("手動編成",))
        self.recommendation_choice_var.set("手動編成")
        self._show_recommendation()
        self.status.set("編成偏好已修改，按儲存保留設定。")

    def _save_constraints(self) -> None:
        if self.snapshot is None or self.selection_scope is None:
            self.status.set("請先更新牌庫並選擇培育偶像。")
            return
        produce_id, idol_card_id = self.selection_scope()
        if idol_card_id not in {row.card_id for row in self.snapshot.idol_cards}:
            self.status.set("請先在培育頁選擇自己持有的偶像卡。")
            return
        try:
            save_loadout_constraints(
                self.constraints_path, self.constraints,
                account_scope=self.snapshot.account_scope,
                inventory_digest=self.snapshot.content_digest,
                produce_id=produce_id, idol_card_id=idol_card_id,
            )
        except (OSError, ValueError) as error:
            self.status.set(f"偏好無法儲存：{error}")
            return
        self.status.set("已儲存下次編成偏好；遊戲中的編成尚未更改。")

    def load_cached(self) -> None:
        try:
            self.snapshot = load_account_inventory(self.path)
        except (OSError, TypeError, ValueError) as error:
            self.status.set(f"已保存牌庫無法讀取：{error}")
            return
        if self.snapshot is not None:
            self._restore_constraints()
            self._render()
            self.status.set(f"已保存牌庫 · {self.snapshot.captured_at} · 按更新讀取遊戲最新數值")

    def refresh(self) -> None:
        if self._busy:
            return
        self._task_kind = "inventory"
        self._catalog = None
        self.status.set("正在從 DLL 讀取完整帳號牌庫…")
        self._launch(lambda: refresh_account_inventory(self.path))

    def _set_busy(self, busy: bool) -> None:
        self.refresh_button.configure(state="disabled" if busy else "normal")
        if hasattr(self, "read_loadout_button"):
            self.read_loadout_button.configure(state="disabled" if busy or self._game_busy else "normal")
            self.recommend_button.configure(state="disabled" if busy or self._game_busy else "normal")
            self.apply_button.configure(state=(
                "disabled" if busy or self._game_busy or self._pending_apply is not None or self._invalid_pending
                    or self._loadout is None or self._loadout.get("active_section") != "support"
                else "normal"
            ))
            self.rental_choice.configure(state="disabled" if busy else "readonly")
            self.recommendation_choice.configure(state="disabled" if busy else "readonly")

    def _handle(self, kind: str, payload: object) -> None:
        if kind == "complete":
            if self._task_kind == "inventory":
                self.snapshot, changes = payload
                self._loadout = None
                self._recommendations = ()
                self.recommendation_choice.configure(values=("手動編成",))
                self.recommendation_choice_var.set("手動編成")
                self._show_recommendation()
                self._restore_constraints()
                self._render()
                self._set_busy(False)
                changed = sum(len(rows[key]) for rows in changes.values() for key in ("added", "removed", "changed"))
                self.status.set(f"已從遊戲更新 · {self.snapshot.captured_at} · {changed} 項變更")
            elif self._task_kind == "loadout":
                try:
                    self._render_loadout(payload["loadout"])
                    if not self._pending_path.is_file():
                        self._pending_apply = None
                    self._set_busy(False)
                    self.status.set(
                        "已讀取遊戲編成；先前操作仍待確認，暫停重送。"
                        if self._pending_apply is not None else "已讀取遊戲目前編成與借卡；可選擇後套用。"
                    )
                    if "reconciled" in payload and self._pending_apply is None:
                        self.status.set(
                            "已核對目前編成符合原方案；先前指令的結果以其回執為準。"
                            if payload["reconciled"]["current_selection_matches_target"]
                            else "已核對目前編成與原方案不同；請檢視後再選擇套用。"
                        )
                except (TypeError, ValueError, KeyError) as error:
                    self.status.set(f"編成資料不可用：{error}")
            elif self._task_kind == "recommend":
                self._render_loadout(payload["loadout"])
                self._recommendations = tuple(row for row in payload["recommendations"] if not row.selection.memory_ids)
                labels = ("手動編成", *(f"建議 {index + 1}" for index in range(len(self._recommendations))))
                self.recommendation_choice.configure(values=labels)
                self.recommendation_choice_var.set(labels[1] if self._recommendations else labels[0])
                self._show_recommendation()
                self.status.set("支援卡建議已更新；回憶由遊戲自動編成。" if self._recommendations else "目前限制下沒有可用支援卡編成。")
            elif self._task_kind == "apply":
                if payload.status == "submitted" and payload.raw.get("applied") is True:
                    sections = payload.raw.get("applied_sections", [])
                    if sections == ["support"]:
                        self.status.set("支援卡已套用；回憶於培育前使用遊戲自動編成。")
                    else:
                        self.status.set("回覆範圍與本次僅套用支援卡不同，請重新讀取遊戲編成核對。")
                else:
                    self.status.set("遊戲編成未確認成功；請重新讀取編成檢查結果。")
                self._loadout = None
                if self._pending_path.is_file():
                    self._restore_pending_apply()
                self._set_busy(False)
        elif kind == "error":
            from .runtime_command_client import RuntimeCommandPending

            if isinstance(payload, RuntimeCommandPending) and payload.request.command == "loadout.apply":
                self._pending_apply = payload
                self._set_busy(False)
                self.status.set("編成操作結果尚未確認，已禁止重送；按「讀取遊戲編成與借卡」查詢原操作。")
            else:
                self.status.set(f"操作未完成：{payload} 已保存牌庫仍保留。")

    def _render(self) -> None:
        self.tree.delete(*self.tree.get_children())
        if self.snapshot is None:
            return
        rows = {"支援卡": self.snapshot.support_cards, "回憶": self.snapshot.memories,
                "偶像卡": self.snapshot.idol_cards}[self.kind.get()]
        self.tree.heading("detail", text={"支援卡": "流派", "回憶": "繼承卡", "偶像卡": "初始能力"}[self.kind.get()])
        if self.kind.get() == "支援卡":
            rows = sorted(rows, key=lambda row: (-row.level, -row.level_limit_rank, row.card_id))
        for row in rows:
            identity = getattr(row, "card_id", getattr(row, "memory_id", ""))
            if self.kind.get() == "支援卡":
                upgrade = f"Lv.{row.level}／上限解放 {row.level_limit_rank}"
                detail = {"ProducePlanType_Common": "共通", "ProducePlanType_Plan1": "好調・集中",
                          "ProducePlanType_Plan2": "元氣・好印象", "ProducePlanType_Plan3": "指針・全力"}.get(row.plan_type, row.plan_type)
            elif self.kind.get() == "偶像卡":
                upgrade = f"才能開花 {row.potential_rank}／特訓 {row.level_limit_rank}"
                values = row.produce_parameters
                detail = "待讀取能力" if values is None else f"Vo {values.vocal} · Da {values.dance} · Vi {values.visual}"
            else:
                upgrade = f"{len(row.abilities)} 項能力"
                inherited = None if row.candidate is None else str(row.candidate.to_dict()["produce_card"]["id"])
                detail = "無繼承卡" if inherited is None else self.names.get(inherited, inherited)
            locked = identity in (*self.constraints.locked_support_ids, *self.constraints.locked_memory_ids)
            excluded = identity in (*self.constraints.excluded_support_ids, *self.constraints.excluded_memory_ids)
            name = self.names.get(identity, identity)
            if self.kind.get() == "回憶":
                name = self.names.get(row.idol_card_id, row.idol_card_id) + " · " + identity[-8:]
            if self.search_text.get().casefold() not in f"{name} {identity} {upgrade} {detail}".casefold():
                continue
            self.tree.insert("", "end", iid=identity, values=(name, upgrade, detail, "鎖定" if locked else "排除" if excluded else "—"))
        self.constraint_summary.set(
            f"支援卡已鎖定 {len(self.constraints.locked_support_ids)} / 5 · 回憶 {len(self.constraints.locked_memory_ids)} / 4\n"
            f"已排除 {len(self.constraints.excluded_support_ids) + len(self.constraints.excluded_memory_ids)} 項"
        )
        self.loadout_note.set(
            f"已持有 {len(self.snapshot.support_cards)} 張支援卡、{len(self.snapshot.memories)} 個回憶。\n\n"
            "強化後按「從遊戲更新牌庫」更新個人數值。"
        )


__all__ = ["CardDataUpdatePanel", "CardLibraryPanel", "refresh_account_inventory"]
