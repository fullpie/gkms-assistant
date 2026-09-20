"""Manual BC/RL update controls backed by the real frozen-data training job."""

from collections.abc import Callable
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

from .gui_card_library import _WorkerPanel
from .model_update_job import DEFAULT_OUTPUT_ROOT, ModelUpdateRequest, ModelUpdateResult, run_model_update
from .policy_bundle import PROJECT_ROOT


class ModelUpdatePanel(_WorkerPanel):
    def __init__(self, parent, *, on_candidate_ready: Callable[[], None] | None = None,
                 output_root: Path = DEFAULT_OUTPUT_ROOT) -> None:
        super().__init__(parent)
        self.output_root, self.on_candidate_ready = output_root, on_candidate_ready
        self.source_spec: Path | None = None
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)
        ttk.Label(self, text="建立新版卡片模型", font=("Microsoft JhengHei UI", 15, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(self, text="檢查新卡覆蓋 → 訓練 BC／RL → 驗證 → 加入可選模型。當前模型持續保留。",
                  style="Hint.TLabel", wraplength=1000).grid(row=1, column=0, sticky="ew", pady=(6, 12))
        source = ttk.Frame(self)
        source.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.source_text = tk.StringVar(value="資料集：沿用目前模型的完整 frozen 資料")
        ttk.Label(source, textvariable=self.source_text, wraplength=760).pack(side="left", fill="x", expand=True)
        self.choose_button = ttk.Button(source, text="選擇其他資料集", command=self.choose_source)
        self.choose_button.pack(side="right")
        actions = ttk.Frame(self)
        actions.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        self.start_button = ttk.Button(actions, text="建立新版模型", command=self.start, style="Accent.TButton")
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="取消工作", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=8)
        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=160)
        self.progress.pack(side="right")
        self.detail = tk.Text(self, height=17, wrap="word", state="disabled", borderwidth=0, padx=12, pady=12)
        self.detail.grid(row=4, column=0, sticky="nsew")
        self.status = tk.StringVar(value="等待手動開始")
        ttk.Label(self, textvariable=self.status, style="Hint.TLabel", wraplength=1000).grid(row=5, column=0, sticky="ew", pady=(10, 0))
        self._text("使用完整且已驗證的演出資料訓練候選模型。\n\n"
                   "新卡需要出現在獨立的訓練、驗證與測試場次中。缺資料或出現未知效果時，"
                   "工作會列出需要補足的項目；不會用舊卡資料冒充新卡訓練。\n\n"
                   "驗證通過的候選會出現在「模型評估」，由你選擇下一場使用。")
        try:
            marker = json.loads((output_root / "latest_job.json").read_text(encoding="utf-8"))
            self._render_report(Path(marker["report_path"]))
        except (OSError, ValueError, KeyError):
            pass

    def _text(self, value: str) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", value)
        self.detail.configure(state="disabled")

    def choose_source(self) -> None:
        path = filedialog.askopenfilename(parent=self, title="選擇已凍結的 training_spec.json",
            initialdir=PROJECT_ROOT / "var" / "training_specs", filetypes=[("訓練資料規格", "*.json")])
        if path:
            self.source_spec = Path(path)
            self.source_text.set(f"資料集：{self.source_spec}")

    def start(self) -> None:
        if self._busy:
            return
        request = ModelUpdateRequest(source_spec_path=self.source_spec, output_root=self.output_root)
        self.status.set("準備驗證卡表與完整資料集…")
        self._launch(lambda: run_model_update(request,
            progress=lambda value: self._events.put(("progress", value)), cancelled=self._cancel.is_set))

    def cancel(self) -> None:
        self._cancel.set()
        self.cancel_button.configure(state="disabled")
        self.status.set("正在取消；當前資料檢查或訓練回合結束後停止。")

    def _set_busy(self, busy: bool) -> None:
        self.start_button.configure(state="disabled" if busy else "normal")
        self.choose_button.configure(state="disabled" if busy else "normal")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        self.progress.start(12) if busy else self.progress.stop()

    def _handle(self, kind: str, payload: object) -> None:
        if kind == "progress" and not self._cancel.is_set():
            self.status.set(str(payload))
        elif kind == "complete" and isinstance(payload, ModelUpdateResult):
            self._render_report(payload.report_path)
            if payload.status == "candidate-ready" and self.on_candidate_ready is not None:
                self.on_candidate_ready()
        elif kind == "error":
            self.status.set(f"模型工作未完成：{payload}")

    def _render_report(self, path: Path) -> None:
        report = json.loads(path.read_text(encoding="utf-8"))
        title = {"candidate-ready": "候選模型已完成，可在模型評估選用", "blocked": "訓練前提尚未滿足",
                 "validation-failed": "候選已訓練，尚未通過驗證", "cancelled": "模型工作已取消"}.get(report["status"], "上次工作尚無完成紀錄")
        phases = {"card-data-validated": "卡片資料檢查", "dataset-audited": "完整資料與新卡覆蓋檢查",
                  "bc-trained-and-validated": "BC 訓練與驗證", "rl-trained-and-validated": "RL 訓練與驗證",
                  "candidate-bundle-verified": "候選模型包驗證"}
        lines = [title, ""]
        lines.extend("已執行 · " + phases.get(value, value) for value in report.get("phases", ()))
        audit = report.get("dataset_audit")
        if audit:
            lines.extend(("", f"資料：{audit['stage_count']} 個演出／{audit['transition_count']} 筆動作",
                          f"需要更新的卡片：{len(audit['required_card_ids'])} 種"))
        for key, label in (("bc_acceptance", "BC"), ("rl_acceptance", "RL")):
            if key in report:
                lines.append(f"{label} 驗證：" + ("通過" if report[key].get("shadow_ready") else "未通過"))
        blockers = report.get("blockers", [])
        if blockers:
            lines.extend(("", "待補足項目：", *(str(value) for value in blockers[:25])))
            if len(blockers) > 25:
                lines.append(f"另有 {len(blockers) - 25} 項，請見報告。")
        if report.get("bundle_path"):
            policy = "RL → BC" if report.get("enabled_policy") == "rl-to-bc" else "BC"
            lines.extend(("", "候選模型：" + report["job_id"], f"實際使用策略：{policy}", "尚未變更目前選用模型。"))
        if report.get("warnings"):
            lines.extend(("", *(str(value) for value in report["warnings"])))
        lines.extend(("", f"報告：{path}"))
        self._text("\n".join(lines))
        self.status.set(title)


__all__ = ["ModelUpdatePanel"]
