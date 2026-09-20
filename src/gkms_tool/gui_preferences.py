"""Small, local next-run preferences, separate from the active run identity."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile


from .application_paths import state_root
DEFAULT_GUI_PREFERENCES = state_root() / "gui/preferences.json"
AUDITION_STRATEGY_LABELS = {
    "highest_available": "高檔優先，失敗降檔",
    "stable_clear": "穩定通關（最低可選）",
}
EXAM_POLICY_VARIANTS = ("baseline", "integrated")


@dataclass(frozen=True, slots=True)
class GuiPreferences:
    idol_card_id: str = "i_card-fktn-3-007"
    produce_id: str = "produce-004"
    target_cycles: int = 1
    audition_strategy: str = "highest_available"
    policy_variant_id: str = "baseline"

    def __post_init__(self) -> None:
        for name in ("idol_card_id", "produce_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be nonempty text")
        if type(self.target_cycles) is not int or not 1 <= self.target_cycles <= 999:
            raise ValueError("連續培育場數必須介於 1 與 999。")
        if self.audition_strategy not in AUDITION_STRATEGY_LABELS:
            raise ValueError("演出檔位設定無效。")
        if self.policy_variant_id not in EXAM_POLICY_VARIANTS:
            raise ValueError("演出模型設定無效。")


def load_gui_preferences(path: Path = DEFAULT_GUI_PREFERENCES) -> GuiPreferences:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return GuiPreferences()
    if not isinstance(payload, dict) or payload.get("schema") != "gkms.gui-preferences.v1":
        raise ValueError("GUI 偏好資料格式無效。")
    # The new next-run model preference does not rewrite historical settings.
    original = {key: payload[key] for key in ("idol_card_id", "produce_id", "target_cycles", "audition_strategy")}
    return GuiPreferences(**original, policy_variant_id=payload.get("policy_variant_id", "baseline"))


def save_gui_preferences(preferences: GuiPreferences, path: Path = DEFAULT_GUI_PREFERENCES) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                prefix="preferences-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"schema": "gkms.gui-preferences.v1", **asdict(preferences)},
                      stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
