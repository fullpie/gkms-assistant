"""MAA-backed live routing for N.I.A. outer subpages.

The overview advisor selects the action.  This module handles only the fixed
screens that appear after that selection: Work choices/start, Special Guide
exit, and Maa's common confirmation buttons.  Produce LocalSave is the state
authority; templates answer only which button is currently visible and where
MAA should click it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from functools import lru_cache
import json
from pathlib import Path
from .application_paths import game_file
import re
from typing import Any, Mapping

import numpy as np
from PIL import Image

from .live_actions import SuggestedClick
from .audition_rules import FINAL, MID1, MID2
from .overview_actions import (
    ACTIVITY,
    DANCE_LESSON,
    DEFAULT_TEMPLATE_DIR,
    NIA_TEMPLATE_DIR,
    REST,
    VOCAL_LESSON,
    VISUAL_LESSON,
    WeeklyActionOption,
)
from .produce_outer_local_save import ProduceOuterLocalSaveSnapshot
from .route_calendar import BUSINESS, CONSULTATION, OUTING, SPECIAL_GUIDANCE


_WORK_PAGE_TEMPLATE = "work_choose.png"
_GUIDE_PAGE_TEMPLATE = "guide_choose.png"
_DRINK_REJECT_ROW_BOX = (180, 940, 520, 1035)
_DRINK_REJECT_CONFIRM_BOX = (210, 1050, 510, 1130)
_DRINK_REJECT_ROW_POINT = (360, 1005)
_DRINK_REJECT_CONFIRM_POINT = (360, 1090)
_WORK_START_TEMPLATE = "work_start.png"

_EVENT_SELECTED_ATTRIBUTE_BOXES: Mapping[str, tuple[int, int, int, int]] = {
    VOCAL_LESSON: (125, 930, 275, 1060),
    DANCE_LESSON: (290, 930, 440, 1060),
    VISUAL_LESSON: (453, 930, 600, 1060),
}
_EVENT_SELECTED_GENERIC_BOXES: tuple[tuple[int, int, int, int], ...] = (
    (120, 925, 275, 1108),
    (285, 925, 440, 1108),
    (450, 925, 605, 1108),
)
_EVENT_SELECT_LABEL_BOXES: Mapping[str, tuple[int, int, int, int]] = {
    VOCAL_LESSON: (125, 1050, 280, 1120),
    DANCE_LESSON: (285, 1050, 445, 1120),
    VISUAL_LESSON: (450, 1050, 610, 1120),
}


def _event_select_label_box(
    action: str,
    marker: tuple[int, int, int, int, float],
) -> tuple[int, int, int, int]:
    """Return the SELECT-label band beneath any Maa-identified weekly tile."""

    fixed = _EVENT_SELECT_LABEL_BOXES.get(action)
    if fixed is not None:
        return fixed
    center_x = (marker[0] + marker[2]) // 2
    return (max(0, center_x - 85), 1050, min(720, center_x + 85), 1120)
_STATIC_EVENT_CHOICE_ROWS: tuple[tuple[int, int, int, int], ...] = (
    (40, 740, 680, 850),
    (40, 850, 680, 960),
    (40, 960, 680, 1070),
)
_STATIC_EVENT_CHOICE_LAYOUTS: tuple[
    tuple[tuple[int, int, int, int], ...], ...
] = (
    (
        (40, 620, 680, 760),
        (40, 750, 680, 870),
        (40, 870, 680, 980),
    ),
    _STATIC_EVENT_CHOICE_ROWS,
)
# OCR only the left text lane.  The selectable row extends through the P-cost
# badge on the right; feeding that badge to OCR turns labels such as
# ``書法展`` into ``書法展100`` and prevents an otherwise exact Master match.
# Keep the broad rows above as click/selection geometry.
_STATIC_EVENT_CHOICE_TEXT_LAYOUTS: tuple[
    tuple[tuple[int, int, int, int], ...], ...
] = (
    (
        (55, 650, 540, 750),
        (55, 750, 540, 850),
        (55, 855, 540, 955),
    ),
    (
        (55, 750, 540, 840),
        (55, 855, 540, 950),
        (55, 965, 540, 1060),
    ),
)

_WORK_OPTION_TEMPLATES: Mapping[str, str] = {
    "Vo": "choose_Vo.png",
    "Da": "choose_Da.png",
    "Vi": "choose_Vi.png",
    "none": "choose_null.png",
}

# Maa's original ProduceChooseWorkAuto owns these three stable selectable row
# hit areas, but its policy randomly chooses among no-cost rows when HP is
# healthy.  The live policy keeps the same row geometry while reading each
# row's actual reward/cost text, so reordered or newly introduced business
# objects do not acquire meaning from their position.
_WORK_BUSINESS_ROWS: tuple[
    tuple[int, tuple[int, int, int, int], tuple[int, int, int, int]], ...
] = (
    (760, (60, 680, 350, 790), (350, 725, 680, 790)),
    (880, (60, 820, 350, 930), (350, 845, 680, 925)),
    (1000, (60, 960, 350, 1080), (350, 985, 680, 1070)),
)

_WORK_ATTRIBUTE_HUD_BOXES: Mapping[str, tuple[int, int, int, int]] = {
    "Vo": (145, 300, 290, 390),
    "Da": (285, 300, 440, 390),
    "Vi": (430, 300, 590, 390),
}

_COMMON_BUTTONS: tuple[tuple[str, Path], ...] = (
    ("next", DEFAULT_TEMPLATE_DIR.parent / "next.png"),
    ("continue", DEFAULT_TEMPLATE_DIR / "continue.png"),
    ("round-close", DEFAULT_TEMPLATE_DIR / "round_close_button.png"),
    ("choose", DEFAULT_TEMPLATE_DIR / "choose.png"),
    ("ok", DEFAULT_TEMPLATE_DIR / "ok.png"),
    ("ok-alt", DEFAULT_TEMPLATE_DIR / "ok_1.png"),
    ("decide", DEFAULT_TEMPLATE_DIR / "decide.png"),
    ("skip-confirm", DEFAULT_TEMPLATE_DIR / "skip_confirm.png"),
    ("drink-no", DEFAULT_TEMPLATE_DIR / "drink_no.png"),
    ("shop-exit", DEFAULT_TEMPLATE_DIR / "shop_exit.png"),
    ("close", DEFAULT_TEMPLATE_DIR / "close.png"),
    ("start", DEFAULT_TEMPLATE_DIR / "start.png"),
    # Maa's generic Produce loop uses these exact templates as ordinary
    # continuations.  Keep each interaction to one click; the next outer-loop
    # capture proves which surface follows.
    ("skip-chat", DEFAULT_TEMPLATE_DIR / "skip_chat.png"),
    ("skip-chat-alt", DEFAULT_TEMPLATE_DIR / "skip_chat_1.png"),
    ("yes", DEFAULT_TEMPLATE_DIR / "yes.png"),
    ("cards-get", DEFAULT_TEMPLATE_DIR / "cards_get.png"),
    ("drink-reject", DEFAULT_TEMPLATE_DIR / "drink_reject.png"),
    ("challenge", DEFAULT_TEMPLATE_DIR / "challenge.png"),
    ("retry", DEFAULT_TEMPLATE_DIR / "retry.png"),
    ("cancel", DEFAULT_TEMPLATE_DIR / "cancel.png"),
    ("finished", DEFAULT_TEMPLATE_DIR / "finished.png"),
    ("follow", DEFAULT_TEMPLATE_DIR / "follow.png"),
)

_CARD_OPERATION_ICONS: tuple[tuple[str, str], ...] = (
    ("strengthen", "strengthen_icon.png"),
    ("delete", "delete_icon.png"),
    ("copy", "copy_icon.png"),
    ("exchange", "exchange_icon.png"),
)

# The operation icon is normally the strongest page owner.  Some localized
# skins (and a few animated frames) lower that match enough that Maa does not
# report the icon, while the localized page header remains readable.  Keep
# this vocabulary deliberately small and page-specific: it is a fallback for
# the header ROI below, not a global OCR threshold relaxation.
_CARD_OPERATION_HEADER_TITLES: Mapping[str, tuple[str, ...]] = {
    "strengthen": ("強化", "强化"),
    "delete": ("刪除", "删除", "削除"),
    "copy": ("複製", "复制", "コピー"),
    "exchange": ("變換", "变换", "変換"),
}

# Special Guidance uses the same customization grid as the ordinary card
# operation workflow, but its page header remains ``Special Guidance`` rather
# than ``Customize``.  Localify can replace Maa's Japanese resource text after
# capture, so the original ``guide_choose.png`` template is not always visible
# to Maa.  These are the localized forms of that one official instruction; a
# match only classifies the existing customization transaction and never owns
# a click itself.
_GUIDE_CUSTOMIZE_PROMPTS: tuple[str, ...] = (
    "カスタマイズするスキルカードを1つ選んでください",
    "请选择一张自定义的技能卡",
    "請選擇一張自定義的技能卡",
)

_CARD_OPERATION_HEADER_TRANSLATION = str.maketrans(
    {
        # Traditional/Simplified/Japanese variants can be mixed by OCR on a
        # single line (for example ``與變换``).  Only normalize characters
        # whose variants are unambiguous for these four operation labels.
        "變": "变",
        "変": "变",
        "換": "换",
    }
)

_CARD_OPERATION_BUTTONS: tuple[tuple[str, str], ...] = (
    ("strengthen", "strengthen.png"),
    ("delete", "delete.png"),
    ("copy", "copy.png"),
    ("exchange", "exchange.png"),
)

_CHOICE_PAGE_TEMPLATES: tuple[str, ...] = (
    "choose_cards.png",
    "choose_drink.png",
    "choose_item.png",
    "choose_move_cards.png",
)

_EVENT_OPTION_TEMPLATES: Mapping[str, str] = {
    ACTIVITY: "activity.png",
    CONSULTATION: "chat.png",
    DANCE_LESSON: "Da.png",
    OUTING: "go_out.png",
    SPECIAL_GUIDANCE: "guide.png",
    VISUAL_LESSON: "Vi.png",
    VOCAL_LESSON: "Vo.png",
    BUSINESS: "work.png",
}

_EVENT_ATTRIBUTE_POINT: Mapping[str, tuple[int, int]] = {
    VOCAL_LESSON: (190, 1000),
    DANCE_LESSON: (360, 1000),
    VISUAL_LESSON: (530, 1000),
}

_EVENT_ATTRIBUTE_VALUE: Mapping[str, str] = {
    VOCAL_LESSON: "vocal",
    DANCE_LESSON: "dance",
    VISUAL_LESSON: "visual",
}

_EVENT_ATTRIBUTE_HUD_BOXES: Mapping[str, tuple[int, int, int, int]] = {
    "Vo": (170, 660, 275, 715),
    "Da": (325, 660, 440, 715),
    "Vi": (480, 660, 600, 715),
}

_MAA_WEEKLY_LABEL_TO_ACTION: Mapping[str, str] = {
    "event-action": ACTIVITY,
    "event-consultation": CONSULTATION,
    "event-dance": DANCE_LESSON,
    "event-outing": OUTING,
    "event-guide": SPECIAL_GUIDANCE,
    "event-visual": VISUAL_LESSON,
    "event-vocal": VOCAL_LESSON,
    "event-business": BUSINESS,
    "event-rest": REST,
}

_MIRROR_STAGE_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("mirror_1.png", MID1),
    ("mirror_2.png", MID2),
    ("mirror_3.png", FINAL),
)


@dataclass(frozen=True, slots=True)
class NiaLiveOuterSubpage:
    kind: str
    target: str
    action: SuggestedClick | None
    evidence: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "target": self.target,
            "action": None if self.action is None else self.action.to_dict(),
            "evidence": dict(self.evidence),
        }


_NIA_EVENT_CHOICE_PENDING_SCHEMA = "gkms.nia-event-choice-pending.v1"
_NIA_EVENT_AUTHORITY_FIELDS: tuple[str, ...] = (
    "log_count",
    "week",
    "last_completed_week",
    "stamina",
    "max_stamina",
    "produce_points",
    "vocal",
    "dance",
    "visual",
    "vote_count",
)
_NIA_EVENT_AUTHORITY_SNAPSHOT_FIELDS: Mapping[str, str] = {
    "log_count": "log_count",
    "week": "latest_week_marker",
    "last_completed_week": "last_completed_week",
    "stamina": "stamina",
    "max_stamina": "max_stamina",
    "produce_points": "produce_points",
    "vocal": "vocal",
    "dance": "dance",
    "visual": "visual",
    "vote_count": "vote_count",
}


@dataclass(frozen=True, slots=True)
class NiaEventChoicePending:
    """Durable owner for the second half of one ADV choice.

    The first click is allowed to change the visible page into a selected
    preview (and to cover its text with a tooltip).  Once that click has
    emitted a complete Master resolution, this record is the only authority
    used by the next frame.  It deliberately carries the typed LocalSave
    authority, source capture, exact row geometry, and Master identity so a
    resume can never turn into a blind fixed-coordinate click.
    """

    owner: str
    produce_id: str
    idol_card_id: str
    target: str
    adv_asset_id: str
    selected_slot: int
    choice_rows: tuple[tuple[int, int, int, int], ...]
    required_choice_count: int | None
    authority: Mapping[str, Any]
    source_capture: Mapping[str, Any]
    master_evidence: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.owner not in {"static", "outing"}:
            raise ValueError("event-choice pending owner is invalid")
        if not isinstance(self.produce_id, str) or self.produce_id not in {
            "produce-004",
            "produce-005",
        }:
            raise ValueError("event-choice pending produce identity is invalid")
        if not isinstance(self.idol_card_id, str) or not self.idol_card_id:
            raise ValueError("event-choice pending idol identity is invalid")
        if not isinstance(self.adv_asset_id, str) or not self.adv_asset_id:
            raise ValueError("event-choice pending ADV identity is invalid")
        if not isinstance(self.target, str) or self.target != (
            f"{self.adv_asset_id}:slot-{self.selected_slot}"
        ):
            raise ValueError("event-choice pending target identity is invalid")
        if (
            isinstance(self.selected_slot, bool)
            or not isinstance(self.selected_slot, int)
            or self.selected_slot < 1
            or self.selected_slot > len(self.choice_rows)
        ):
            raise ValueError("event-choice pending selected slot is invalid")
        if len(self.choice_rows) < 2:
            raise ValueError("event-choice pending row layout is incomplete")
        for row in self.choice_rows:
            if (
                not isinstance(row, tuple)
                or len(row) != 4
                or any(
                    isinstance(value, bool) or not isinstance(value, int)
                    for value in row
                )
                or row[2] <= row[0]
                or row[3] <= row[1]
                or row[0] < 0
                or row[1] < 0
                or row[2] > 720
                or row[3] > 1280
            ):
                raise ValueError("event-choice pending row geometry is invalid")
        if self.required_choice_count is not None and (
            isinstance(self.required_choice_count, bool)
            or not isinstance(self.required_choice_count, int)
            or self.required_choice_count != len(self.choice_rows)
        ):
            raise ValueError("event-choice pending required count is invalid")
        if set(self.authority) != set(_NIA_EVENT_AUTHORITY_FIELDS):
            raise ValueError("event-choice pending LocalSave authority is incomplete")
        for field in _NIA_EVENT_AUTHORITY_FIELDS:
            value = self.authority[field]
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int)
            ):
                raise ValueError(
                    f"event-choice pending LocalSave authority field is invalid: {field}"
                )
        if not isinstance(self.source_capture, Mapping):
            raise ValueError("event-choice pending source capture is invalid")
        png_path = self.source_capture.get("png_path")
        if not isinstance(png_path, str) or not png_path:
            raise ValueError("event-choice pending source capture has no PNG")
        for field in ("hwnd", "pid"):
            value = self.source_capture.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(
                    f"event-choice pending source capture has invalid {field}"
                )
        timestamp = self.source_capture.get("timestamp")
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            raise ValueError("event-choice pending source capture timestamp is invalid")
        if not isinstance(self.master_evidence, Mapping):
            raise ValueError("event-choice pending Master evidence is invalid")
        if self.master_evidence.get("adv_asset_id") != self.adv_asset_id:
            raise ValueError("event-choice pending Master ADV identity differs")
        if self.master_evidence.get("chosen_slot") != self.selected_slot:
            raise ValueError("event-choice pending Master slot differs")
        if self.master_evidence.get("owner") != self.owner:
            raise ValueError("event-choice pending Master owner differs")
        if not isinstance(self.master_evidence.get("choice_texts"), list):
            raise ValueError("event-choice pending Master choice texts are missing")
        if len(self.master_evidence["choice_texts"]) != len(self.choice_rows):
            raise ValueError("event-choice pending Master/text row count differs")

    @classmethod
    def from_surface(
        cls,
        surface: Mapping[str, Any],
        *,
        produce_id: str,
        idol_card_id: str,
    ) -> "NiaEventChoicePending":
        """Create a pending record only from a complete Master-bound surface."""

        if not isinstance(surface, Mapping):
            raise TypeError("event-choice surface must be a mapping")
        evidence = surface.get("evidence")
        capture = surface.get("capture")
        if not isinstance(evidence, Mapping) or not isinstance(capture, Mapping):
            raise ValueError("event-choice surface lacks Master/capture evidence")
        owner = evidence.get("owner")
        if owner not in {"static", "outing"}:
            raise ValueError("event-choice surface has no typed owner")
        adv_asset_id = evidence.get("adv_asset_id")
        selected_slot = evidence.get("chosen_slot")
        target = surface.get("target")
        raw_rows = evidence.get("choice_rows")
        required_count = evidence.get("required_choice_count")
        raw_authority = surface.get("outer_authority")
        if not isinstance(adv_asset_id, str) or not adv_asset_id:
            raise ValueError("event-choice surface has no Master ADV identity")
        if isinstance(selected_slot, bool) or not isinstance(selected_slot, int):
            raise ValueError("event-choice surface has no Master selected slot")
        if not isinstance(target, str):
            raise ValueError("event-choice surface has no target")
        if not isinstance(raw_rows, list):
            raise ValueError("event-choice surface has no exact row layout")
        rows = tuple(
            tuple(int(value) for value in row)
            for row in raw_rows
            if isinstance(row, list) and len(row) == 4
        )
        if len(rows) != len(raw_rows):
            raise ValueError("event-choice surface row layout is malformed")
        if required_count is not None and (
            isinstance(required_count, bool) or not isinstance(required_count, int)
        ):
            raise ValueError("event-choice surface required count is malformed")
        if not isinstance(raw_authority, Mapping):
            raise ValueError("event-choice surface has no LocalSave authority")
        if any(field not in raw_authority for field in _NIA_EVENT_AUTHORITY_FIELDS):
            raise ValueError("event-choice surface LocalSave authority is incomplete")
        authority = {
            field: raw_authority.get(field)
            for field in _NIA_EVENT_AUTHORITY_FIELDS
        }
        master_evidence = dict(evidence)
        return cls(
            owner=str(owner),
            produce_id=produce_id,
            idol_card_id=idol_card_id,
            target=target,
            adv_asset_id=adv_asset_id,
            selected_slot=selected_slot,
            choice_rows=rows,
            required_choice_count=required_count,
            authority=authority,
            source_capture=dict(capture),
            master_evidence=master_evidence,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "NiaEventChoicePending":
        if not isinstance(value, Mapping):
            raise TypeError("event-choice pending record must be a mapping")
        if value.get("schema") != _NIA_EVENT_CHOICE_PENDING_SCHEMA:
            raise ValueError("event-choice pending schema is unsupported")
        raw_rows = value.get("choice_rows")
        if not isinstance(raw_rows, list):
            raise ValueError("event-choice pending row layout is missing")
        rows = tuple(
            tuple(int(item) for item in row)
            for row in raw_rows
            if isinstance(row, list) and len(row) == 4
        )
        if len(rows) != len(raw_rows):
            raise ValueError("event-choice pending row layout is malformed")
        authority = value.get("authority")
        source_capture = value.get("source_capture")
        master_evidence = value.get("master_evidence")
        if not isinstance(authority, Mapping):
            raise ValueError("event-choice pending authority is missing")
        if not isinstance(source_capture, Mapping):
            raise ValueError("event-choice pending source capture is missing")
        if not isinstance(master_evidence, Mapping):
            raise ValueError("event-choice pending Master evidence is missing")
        return cls(
            owner=str(value.get("owner", "")),
            produce_id=str(value.get("produce_id", "")),
            idol_card_id=str(value.get("idol_card_id", "")),
            target=str(value.get("target", "")),
            adv_asset_id=str(value.get("adv_asset_id", "")),
            selected_slot=value.get("selected_slot"),
            choice_rows=rows,
            required_choice_count=value.get("required_choice_count"),
            authority={field: authority.get(field) for field in _NIA_EVENT_AUTHORITY_FIELDS},
            source_capture=dict(source_capture),
            master_evidence=dict(master_evidence),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": _NIA_EVENT_CHOICE_PENDING_SCHEMA,
            "owner": self.owner,
            "produce_id": self.produce_id,
            "idol_card_id": self.idol_card_id,
            "target": self.target,
            "adv_asset_id": self.adv_asset_id,
            "selected_slot": self.selected_slot,
            "choice_rows": [list(row) for row in self.choice_rows],
            "required_choice_count": self.required_choice_count,
            "authority": dict(self.authority),
            "source_capture": dict(self.source_capture),
            "master_evidence": dict(self.master_evidence),
        }


def _canonical_screen(image: Image.Image):
    from .training_choice import _canonical_rgb

    return _canonical_rgb(image)


def _maa_recognition_matches(
    payload: Mapping[str, Any] | None,
) -> dict[str, tuple[int, int, int, int, float]]:
    """Normalize one controller-side Maa recognition batch."""

    if not isinstance(payload, Mapping):
        return {}
    rows = payload.get("nodes")
    if not isinstance(rows, list):
        raise ValueError("Maa N.I.A. recognition batch has no node list")
    result: dict[str, tuple[int, int, int, int, float]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("Maa N.I.A. recognition row must be an object")
        name = row.get("node")
        box = row.get("box")
        score = row.get("score")
        if not isinstance(name, str) or not name:
            raise ValueError("Maa N.I.A. recognition row has no node name")
        if (
            not isinstance(box, list)
            or len(box) != 4
            or any(isinstance(value, bool) or not isinstance(value, int) for value in box)
        ):
            raise ValueError(f"Maa N.I.A. recognition {name} has an invalid box")
        left, top, width, height = box
        if left < 0 or top < 0 or width <= 0 or height <= 0:
            raise ValueError(f"Maa N.I.A. recognition {name} box is out of range")
        confidence = (
            float(score)
            if isinstance(score, (int, float)) and not isinstance(score, bool)
            else 1.0
        )
        result[name] = (left, top, left + width, top + height, confidence)
    return result


def nia_weekly_actions_from_maa_recognition(
    payload: Mapping[str, Any],
) -> tuple[WeeklyActionOption, ...]:
    """Project weekly action presence/geometry from one read-only Maa batch."""

    recognized = _maa_recognition_matches(payload)
    options: list[WeeklyActionOption] = []
    for label, action in _MAA_WEEKLY_LABEL_TO_ACTION.items():
        marker = recognized.get(label)
        if marker is None:
            continue
        sp_index = {
            VOCAL_LESSON: 0,
            DANCE_LESSON: 1,
            VISUAL_LESSON: 2,
        }.get(action)
        click_point = _EVENT_ATTRIBUTE_POINT.get(action)
        canonical_box = (
            marker[:4]
            if click_point is None
            else (
                click_point[0],
                click_point[1],
                click_point[0] + 1,
                click_point[1] + 1,
            )
        )
        options.append(
            WeeklyActionOption(
                action=action,
                label=action,
                canonical_box=canonical_box,
                match_score=marker[4],
                is_sp=(
                    sp_index is not None
                    and f"event-sp-{sp_index}" in recognized
                ),
            )
        )
    if not options:
        raise ValueError("Maa batch has no visible N.I.A. weekly action")
    return tuple(
        sorted(
            options,
            key=lambda option: (
                option.canonical_box[1],
                option.canonical_box[0],
                option.action,
            ),
        )
    )


def _match(
    screen,
    template_path: Path,
    *,
    roi: tuple[int, int, int, int],
    threshold: float,
) -> tuple[int, int, int, int, float] | None:
    from .training_choice import _best_template_match

    path = Path(template_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Maa N.I.A. subpage template is missing: {path}")
    with Image.open(path) as template:
        template.load()
        width, height = template.size
        left_bound, top_bound, right_bound, bottom_bound = roi
        maximum_x = min(720 - width, right_bound - width)
        maximum_y = min(1280 - height, bottom_bound - height)
        if maximum_x < left_bound or maximum_y < top_bound:
            return None
        coarse_left, coarse_top, _score = _best_template_match(
            screen,
            template,
            x_range=range(left_bound, maximum_x + 1, 4),
            y_range=range(top_bound, maximum_y + 1, 4),
        )
        left, top, score = _best_template_match(
            screen,
            template,
            x_range=range(
                max(left_bound, coarse_left - 5),
                min(maximum_x, coarse_left + 5) + 1,
            ),
            y_range=range(
                max(top_bound, coarse_top - 5),
                min(maximum_y, coarse_top + 5) + 1,
            ),
        )
    if score < threshold:
        return None
    return left, top, left + width, top + height, score


def _mirror_marker(screen) -> tuple[str, tuple[int, int, int, int, float]] | None:
    for filename, stage in _MIRROR_STAGE_TEMPLATES:
        marker = _match(
            screen,
            NIA_TEMPLATE_DIR / filename,
            roi=(0, 600, 720, 1180),
            threshold=0.90,
        )
        if marker is not None:
            return stage, marker
    return None


@lru_cache(maxsize=32)
def _mirror_master_thresholds(
    produce_id: str,
    idol_card_id: str,
    stage: str,
) -> tuple[int, ...]:
    from .nia_static_inventory import build_nia_static_inventory

    inventory = build_nia_static_inventory(
        idol_card_id,
        produce_id=produce_id,
    )
    thresholds = tuple(
        sorted(
            value.vote_count
            for value in inventory.auditions
            if value.step_type == stage
        )
    )
    if not thresholds or thresholds[0] != 0:
        raise ValueError(f"N.I.A. mirror Master thresholds are unavailable: {stage}")
    return thresholds


def _live_active_run_shadow(
    produce_id: str,
    idol_card_id: str,
) -> Any | None:
    from .run_shadow import load_matching_active_run_shadow

    return load_matching_active_run_shadow(produce_id, idol_card_id)


def _live_audition_deck_context(
    produce_id: str,
    idol_card_id: str,
) -> tuple[int | None, Mapping[int, int]]:
    """Read advisory deck sizes from the matching active-run shadow."""

    shadow = _live_active_run_shadow(produce_id, idol_card_id)
    if shadow is None:
        return None, {}
    try:
        current_count = sum((shadow.deck or {}).values()) or None
        baseline_counts: dict[int, int] = {}
        for observation in shadow.observations:
            if observation.kind != "plan2_exam_save_deck_baseline":
                continue
            context_id = observation.metadata.get("step_context_id")
            count = observation.metadata.get("card_instance_count")
            if (
                isinstance(context_id, str)
                and context_id.startswith("exam-step:")
                and isinstance(count, int)
                and not isinstance(count, bool)
                and count > 0
            ):
                raw_step = context_id.removeprefix("exam-step:")
                if raw_step.isdigit():
                    baseline_counts[int(raw_step)] = count
        return current_count, baseline_counts
    except (TypeError, ValueError):
        return None, {}


def _audition_difficulty_advice(
    snapshot: ProduceOuterLocalSaveSnapshot,
    *,
    produce_id: str | None,
    idol_card_id: str | None,
    screen_stage: str | None,
) -> Mapping[str, Any] | None:
    if produce_id is None or idol_card_id is None:
        return None
    from .nia_audition_difficulty_advisor import advise_nia_audition_difficulty

    current_count, baseline_counts = _live_audition_deck_context(
        produce_id,
        idol_card_id,
    )
    try:
        return advise_nia_audition_difficulty(
            snapshot,
            produce_id=produce_id,
            idol_card_id=idol_card_id,
            screen_stage=screen_stage,
            current_deck_count=current_count,
            prior_deck_counts=baseline_counts,
        ).to_dict()
    except (FileNotFoundError, OSError, TypeError, ValueError) as error:
        return {
            "stage": screen_stage,
            "selected_threshold": 0,
            "forecast_score": None,
            "policy": "difficulty-advisor-unavailable-lowest-row",
            "detail": f"{type(error).__name__}:{error}",
        }


def _recognized_mirror_boxes(
    payload: Mapping[str, Any] | None,
    *,
    expected_thresholds: tuple[int, ...],
) -> Mapping[int, tuple[int, int, int, int, float]]:
    if not isinstance(payload, Mapping) or payload.get("node") != "ProduceRecognitionMirror":
        return {}
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return {}
    expected = set(expected_thresholds)
    result: dict[int, tuple[int, int, int, int, float]] = {}
    for row in raw_results:
        if not isinstance(row, Mapping):
            continue
        text = row.get("text")
        box = row.get("box")
        if not isinstance(text, str) or not isinstance(box, list) or len(box) != 4:
            continue
        digits = "".join(character for character in text if character.isdigit())
        if not digits:
            continue
        threshold = int(digits)
        if threshold not in expected or threshold == 0:
            continue
        if any(isinstance(value, bool) or not isinstance(value, int) for value in box):
            continue
        left, top, width, height = box
        if width <= 0 or height <= 0:
            continue
        score = row.get("score")
        confidence = float(score) if isinstance(score, (int, float)) else 0.0
        result[threshold] = (
            left,
            top,
            left + width,
            top + height,
            confidence,
        )
    return result


def _mirror_choice(
    *,
    vote_count: int | None,
    expected_thresholds: tuple[int, ...],
    recognition: Mapping[str, Any] | None,
    stage: str | None = None,
    recommended_threshold: int | None = None,
    recommendation_evidence: Mapping[str, Any] | None = None,
) -> tuple[int, tuple[int, int], Mapping[str, Any]]:
    vote = 0 if vote_count is None else max(vote_count, 0)
    boxes = _recognized_mirror_boxes(
        recognition,
        expected_thresholds=expected_thresholds,
    )
    available = {0: (360, 1050, 361, 1051, 1.0), **boxes}
    unlocked = tuple(
        sorted(
            threshold
            for threshold in available
            if threshold in expected_thresholds and threshold <= vote
        )
    )
    # Mid1 is useful as a calibration run, but later auditions end the whole
    # produce when they are missed.  Votes only prove that a row is unlocked;
    # they do not prove that the current deck can clear its score/rank target.
    # Until a score forecast is persisted, choose the completion-safe row for
    # Mid2/Final instead of repeating the old "highest affordable" mistake.
    completion_safe = stage in (MID2, FINAL)
    if recommended_threshold is not None:
        recommended = tuple(
            threshold for threshold in unlocked if threshold <= recommended_threshold
        )
        selected = recommended[-1] if recommended else 0
    else:
        selected = unlocked[0] if (completion_safe or stage is None) and unlocked else (
            unlocked[-1] if unlocked else 0
        )
    box = available[selected]
    point = (
        (box[0] + box[2]) // 2,
        max(0, (box[1] + box[3]) // 2 - 20),
    )
    return selected, point, {
        "policy": (
            "forecast-master-threshold-maa-row"
            if recommended_threshold is not None
            else (
                "completion-safe-master-threshold-maa-row"
                if completion_safe
                else "localsave-vote-master-threshold-maa-row"
            )
        ),
        "stage": stage,
        "vote_count": vote_count,
        "expected_thresholds": list(expected_thresholds),
        "recognized_thresholds": sorted(boxes),
        "selected_threshold": selected,
        "difficulty_advice": (
            None if recommendation_evidence is None else dict(recommendation_evidence)
        ),
    }


def _action(
    capture: Mapping[str, Any],
    *,
    kind: str,
    target: str,
    box: tuple[int, int, int, int],
    click_count: int = 1,
) -> SuggestedClick:
    left, top, right, bottom = box
    return SuggestedClick(
        label=f"nia-subpage:{kind}:{target}",
        canonical_x=(left + right) // 2,
        canonical_y=(top + bottom) // 2,
        source_png_path=str(capture["png_path"]),
        source_timestamp=float(capture["timestamp"]),
        source_hwnd=int(capture["hwnd"]),
        source_pid=int(capture["pid"]),
        verification_box=box,
        click_count=click_count,
    )


def _fixed_action(
    capture: Mapping[str, Any],
    *,
    kind: str,
    target: str,
    point: tuple[int, int],
    verification_box: tuple[int, int, int, int],
    click_count: int = 1,
) -> SuggestedClick:
    left, top, right, bottom = verification_box
    if not (left <= point[0] < right and top <= point[1] < bottom):
        # The Maa recognition marker can be a page header while the documented
        # action target is a fixed card/row coordinate elsewhere.  Bind the
        # stale-frame check to the actual input neighbourhood; marker identity
        # is already preserved separately in NiaLiveOuterSubpage.evidence.
        left = max(0, point[0] - 20)
        top = max(0, point[1] - 20)
        right = min(720, point[0] + 21)
        bottom = min(1280, point[1] + 21)
        verification_box = (left, top, right, bottom)
    return SuggestedClick(
        label=f"nia-subpage:{kind}:{target}",
        canonical_x=point[0],
        canonical_y=point[1],
        source_png_path=str(capture["png_path"]),
        source_timestamp=float(capture["timestamp"]),
        source_hwnd=int(capture["hwnd"]),
        source_pid=int(capture["pid"]),
        verification_box=verification_box,
        click_count=click_count,
    )


def _fixed_surface(
    capture: Mapping[str, Any],
    *,
    kind: str,
    target: str,
    point: tuple[int, int],
    marker: tuple[int, int, int, int, float],
    policy: str,
    click_count: int = 1,
) -> NiaLiveOuterSubpage:
    return NiaLiveOuterSubpage(
        kind=kind,
        target=target,
        action=_fixed_action(
            capture,
            kind=kind,
            target=target,
            point=point,
            verification_box=marker[:4],
            click_count=click_count,
        ),
        evidence={"match_score": marker[4], "policy": policy},
    )


def _event_choice_selected_scores(
    screen: np.ndarray,
    choice_rows: tuple[tuple[int, int, int, int], ...],
) -> list[int]:
    """Measure the stable orange selected-row proof without reading text."""

    scores: list[int] = []
    for left, top, right, bottom in choice_rows:
        tile = screen[top:bottom, left:right, :3]
        red = tile[:, :, 0]
        green = tile[:, :, 1]
        blue = tile[:, :, 2]
        scores.append(
            int(
                ((red > 220) & (green > 70) & (green < 200) & (blue < 120)).sum()
            )
        )
    return scores


def _event_choice_unique_selected_slot(
    screen: np.ndarray,
    choice_rows: tuple[tuple[int, int, int, int], ...],
) -> tuple[int | None, list[int]]:
    """Return one selected row only when its proof is unambiguous."""

    scores = _event_choice_selected_scores(screen, choice_rows)
    selected = [index + 1 for index, score in enumerate(scores) if score >= 5_000]
    if len(selected) != 1:
        return None, scores
    winner = selected[0] - 1
    runner_up = max(
        (score for index, score in enumerate(scores) if index != winner),
        default=0,
    )
    if scores[winner] - runner_up < 1_000:
        return None, scores
    return selected[0], scores


def _event_choice_row_gradient_correlation(
    source: np.ndarray,
    current: np.ndarray,
    box: tuple[int, int, int, int],
) -> float:
    """Compare a row's structure while tolerating tooltip dimming."""

    left, top, right, bottom = box
    source_gray = source[top:bottom, left:right, :3].astype(np.float64).mean(axis=2)
    current_gray = current[top:bottom, left:right, :3].astype(np.float64).mean(axis=2)

    def gradient(value: np.ndarray) -> np.ndarray:
        horizontal = np.diff(value, axis=1, prepend=value[:, :1])
        vertical = np.diff(value, axis=0, prepend=value[:1, :])
        return np.hypot(horizontal, vertical)

    source_gradient = gradient(source_gray)
    current_gradient = gradient(current_gray)
    source_gradient = (source_gradient - source_gradient.mean()) / (
        source_gradient.std() + 1e-6
    )
    current_gradient = (current_gradient - current_gradient.mean()) / (
        current_gradient.std() + 1e-6
    )
    return float((source_gradient * current_gradient).mean())


def _event_choice_screen_proof(
    image: Image.Image,
    capture: Mapping[str, Any],
    pending: NiaEventChoicePending,
) -> Mapping[str, Any] | None:
    """Prove that the fresh frame is the pending event, not a stale click.

    The selected row is deliberately excluded from the structural comparison:
    it changes colour and is often covered by the tooltip.  At least one
    unselected row must still match the source capture's text/layout edges.
    """

    source_path_value = pending.source_capture.get("png_path")
    current_path_value = capture.get("png_path")
    if not isinstance(source_path_value, str) or not source_path_value:
        return None
    if not isinstance(current_path_value, str) or not current_path_value:
        return None
    if (
        capture.get("hwnd") != pending.source_capture.get("hwnd")
        or capture.get("pid") != pending.source_capture.get("pid")
    ):
        return None
    source_path = Path(source_path_value)
    current_path = Path(current_path_value)
    if not source_path.is_file() or not current_path.is_file():
        return None
    try:
        with Image.open(source_path.resolve()) as source_image:
            source_image.load()
            source_array = _canonical_screen(source_image)
        current_array = _canonical_screen(image)
    except (OSError, TypeError, ValueError):
        return None
    if source_array.shape != current_array.shape:
        return None
    correlations = {
        str(index + 1): _event_choice_row_gradient_correlation(
            source_array,
            current_array,
            row,
        )
        for index, row in enumerate(pending.choice_rows)
        if index + 1 != pending.selected_slot
    }
    if not correlations:
        return {
            "matched": False,
            "reason": "no-unselected-row-screen-proof",
            "row_gradient_correlations": correlations,
        }
    if min(correlations.values()) < 0.50:
        return {
            "matched": False,
            "reason": "event-screen-identity-mismatch",
            "row_gradient_correlations": correlations,
        }
    selected_slot, selected_scores = _event_choice_unique_selected_slot(
        current_array,
        pending.choice_rows,
    )
    if selected_slot is None:
        return {
            "matched": False,
            "reason": "selected-slot-proof-unresolved",
            "selected_scores": selected_scores,
            "row_gradient_correlations": correlations,
        }
    if selected_slot != pending.selected_slot:
        return {
            "matched": False,
            "reason": "selected-slot-differs-from-master-pending-slot",
            "selected_slot": selected_slot,
            "selected_scores": selected_scores,
            "row_gradient_correlations": correlations,
        }
    return {
        "matched": True,
        "selected_slot": selected_slot,
        "selected_scores": selected_scores,
        "row_gradient_correlations": correlations,
        "source_png_path": str(source_path),
        "current_png_path": str(current_path),
        "policy": "same-event-source-rows+unique-selected-slot",
    }


def _event_choice_authority_matches(
    snapshot: ProduceOuterLocalSaveSnapshot,
    pending: NiaEventChoicePending | Mapping[str, Any],
) -> bool:
    authority = getattr(pending, "authority", None)
    if authority is None and isinstance(pending, Mapping):
        authority = pending.get("authority")
    if not isinstance(authority, Mapping):
        return False
    for field in _NIA_EVENT_AUTHORITY_FIELDS:
        snapshot_field = _NIA_EVENT_AUTHORITY_SNAPSHOT_FIELDS[field]
        if getattr(snapshot, snapshot_field, None) != authority.get(field):
            return False
    return True


def _parameter(snapshot: ProduceOuterLocalSaveSnapshot, attribute: str) -> int:
    field = {"Vo": "vocal", "Da": "dance", "Vi": "visual"}[attribute]
    value = getattr(snapshot, field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"N.I.A. LocalSave has no valid {field} parameter")
    return value


@lru_cache(maxsize=1)
def _work_text_recognizer():
    from .text_recognizer import PaddleLineRecognizer

    return PaddleLineRecognizer()


def _drink_keep_remaining_ocr(screen) -> tuple[int, Mapping[str, Any]] | None:
    """Read the one decision that matters on the full P-drink page.

    Unchecked boxes remain visible after the required number of drinks has
    already been selected, so their presence cannot decide whether to keep
    selecting.  Read the visible ``remaining N`` line once instead.  Paddle
    occasionally recognizes the localized zero glyph as ``O``; accepting that
    glyph here does not infer drink identity or button state.
    """

    image = Image.fromarray(screen.astype("uint8"))
    read = _work_text_recognizer().recognize(image.crop((240, 1165, 500, 1245)))
    compact = re.sub(r"\s+", "", read.text)
    if read.confidence < 0.55 or "個" not in compact:
        return None
    match = re.search(r"([0-9OoＯ○])個", compact)
    if match is None:
        return None
    glyph = match.group(1)
    remaining = 0 if glyph in {"O", "o", "Ｏ", "○"} else int(glyph)
    return remaining, {
        "authority": "visible-drink-remaining-ocr",
        "text": read.text,
        "confidence": read.confidence,
        "remaining": remaining,
    }


def _drink_keep_candidate_layout(screen) -> Mapping[str, Any]:
    """Read the visible N-drink chooser geometry and checkbox state.

    The capacity page is a scrolling list, not a fixed five-choice widget.
    Every currently visible drink row owns one of two neutral background
    colours: pale yellow when selected and pale grey when unselected.  Their
    vertical extents and the larger section gap are therefore observable from
    the current frame without assigning any identity to a thumbnail or slot.

    A partially visible final row is retained as long as at least 24 pixels of
    its live hit area remain above the footer.  If a frame does not expose a
    complete acquired/owned section boundary, callers must wait without input.
    """

    height, width = screen.shape[:2]
    if width < 640 or height < 1080:
        return {
            "rows": [],
            "section_break_after": None,
            "complete": False,
            "reason": "drink-layout-canvas-too-small",
        }

    def row_colour(pixel) -> str | None:
        red, green, blue = (int(value) for value in pixel[:3])
        if (
            red >= 248
            and 222 <= green <= 248
            and 172 <= blue <= 218
            and red - green >= 7
            and green - blue >= 18
        ):
            return "selected"
        if (
            236 <= red <= 252
            and 236 <= green <= 252
            and 234 <= blue <= 252
            and max(red, green, blue) - min(red, green, blue) <= 16
        ):
            return "unselected"
        return None

    # The sample columns sit between the drink artwork and description text.
    # A majority makes isolated antialiasing/text pixels irrelevant while the
    # returned click box is still derived from the complete live row below.
    sample_xs = (152, 157, 162)
    classifications: list[str | None] = []
    for y in range(280, min(height, 1080)):
        votes = [row_colour(screen[y, x]) for x in sample_xs]
        selected = votes.count("selected")
        unselected = votes.count("unselected")
        classifications.append(
            "selected"
            if selected >= 2
            else "unselected"
            if unselected >= 2
            else None
        )

    runs: list[tuple[int, int, str]] = []
    start: int | None = None
    state: str | None = None
    for offset, current in enumerate((*classifications, None)):
        y = 280 + offset
        if current == state:
            continue
        if state is not None and start is not None and y - start >= 24:
            runs.append((start, y, state))
        start = y if current is not None else None
        state = current

    rows: list[dict[str, Any]] = []
    for index, (top, bottom, state) in enumerate(runs, start=1):
        # Derive horizontal bounds from this row's own background pixels.  Use
        # a line near its top so multi-line descriptions and checkmarks do not
        # fragment the measurement.
        probe_y = min(bottom - 1, top + min(10, max(2, (bottom - top) // 4)))
        matches = [
            x
            for x in range(20, min(width, 700))
            if row_colour(screen[probe_y, x]) == state
        ]
        if not matches:
            return {
                "rows": rows,
                "section_break_after": None,
                "complete": False,
                "reason": f"drink-row-{index}-has-no-live-horizontal-extent",
            }
        left = min(matches)
        right = max(matches) + 1
        if right - left < 500:
            return {
                "rows": rows,
                "section_break_after": None,
                "complete": False,
                "reason": f"drink-row-{index}-is-not-a-full-width-control",
            }
        rows.append(
            {
                "index": index,
                "box": [left, top, right, bottom],
                "selected": state == "selected",
                "visible_height": bottom - top,
            }
        )

    gaps = [
        (int(rows[index + 1]["box"][1]) - int(rows[index]["box"][3]), index + 1)
        for index in range(len(rows) - 1)
    ]
    section_gaps = [(gap, after) for gap, after in gaps if gap >= 55]
    section_break_after = (
        section_gaps[0][1] if len(section_gaps) == 1 else None
    )
    complete = len(rows) >= 3 and section_break_after is not None
    return {
        "rows": rows,
        "section_break_after": section_break_after,
        "complete": complete,
        "reason": (
            "visible-acquired-and-owned-sections"
            if complete
            else "drink-layout-section-boundary-unresolved"
        ),
    }


def _classify_drink_reward_ocr(
    *,
    prompt_text: str,
    prompt_confidence: float,
    title_text: str,
    title_confidence: float,
    effect_text: str,
    effect_confidence: float,
    button_text: str,
    button_confidence: float,
) -> str | None:
    """Classify the P-drink reward page without relying on button colour.

    The bundled Maa graph recognizes the Japanese prompt.  Localify may render
    that prompt in Traditional Chinese, so the live router supplements Maa with
    its bundled OCR model.  Paddle can confuse a few glyphs in the prompt, but
    the page still has two stable semantic states:

    * before selection, the prompt contains ``P`` and a receive verb;
    * after selection, a drink title and its effect replace that prompt.

    The unselected prompt is already the page's semantic owner: it explicitly
    says to choose a P drink to receive.  Do not additionally require OCR of
    the disabled Receive button; its low-contrast label is often invisible to
    OCR even though Maa has already recognized the shared receive control.
    The selected state still requires that enabled control alongside the exact
    drink title/effect.
    """

    def compact(value: str) -> str:
        return re.sub(r"\s+", "", value).replace("Ｐ", "P")

    prompt = compact(prompt_text)
    title = compact(title_text)
    effect = compact(effect_text)
    button = compact(button_text)
    receive_button = (
        button_confidence >= 0.65
        and "取" in button
        and ("領" in button or "领" in button)
    )
    if (
        prompt_confidence >= 0.60
        and "P" in prompt.upper()
        and "取" in prompt
        and ("領" in prompt or "领" in prompt)
    ):
        return "unselected"
    if (
        receive_button
        and
        title_confidence >= 0.75
        and effect_confidence >= 0.55
        and bool(title)
        and bool(effect)
        and title in _localized_drink_names()
    ):
        return "selected"
    return None


@lru_cache(maxsize=1)
def _localized_drink_names() -> frozenset[str]:
    """Return exact Japanese/Traditional-Chinese Master drink names."""

    from .drink_catalog import load_drink_catalog

    names = {
        re.sub(r"\s+", "", value.name)
        for value in load_drink_catalog().drinks
        if value.name
    }
    translation = game_file('gakumas-local/local-files/masterTrans/ProduceDrink.json')
    if translation.is_file():
        payload = json.loads(translation.read_text(encoding="utf-8-sig"))
        rows = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            raise ValueError("ProduceDrink translation data must be a list")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("ProduceDrink translation row must be an object")
            name = row.get("name")
            if isinstance(name, str) and name.strip():
                names.add(re.sub(r"\s+", "", name))
    return frozenset(names)


def _drink_reward_ocr_state(screen) -> tuple[str, Mapping[str, Any]] | None:
    """Read the current P-drink reward state in canonical screen coordinates."""

    image = Image.fromarray(screen.astype("uint8"))
    recognizer = _work_text_recognizer()
    detail = recognizer.recognize(image.crop((80, 535, 640, 635)))
    title = recognizer.recognize(image.crop((100, 470, 620, 560)))
    button = recognizer.recognize(image.crop((220, 1020, 500, 1160)))
    state = _classify_drink_reward_ocr(
        prompt_text=detail.text,
        prompt_confidence=detail.confidence,
        title_text=title.text,
        title_confidence=title.confidence,
        effect_text=detail.text,
        effect_confidence=detail.confidence,
        button_text=button.text,
        button_confidence=button.confidence,
    )
    if state is None:
        return None
    return state, {
        "authority": "maa-paddle-ocr-visible-drink-reward-state",
        "state": state,
        "prompt_text": detail.text,
        "prompt_confidence": detail.confidence,
        "title_text": title.text,
        "title_confidence": title.confidence,
        "effect_text": detail.text,
        "effect_confidence": detail.confidence,
        "button_text": button.text,
        "button_confidence": button.confidence,
    }


def _drink_full_reject_surface(
    screen,
    capture: Mapping[str, Any],
) -> NiaLiveOuterSubpage | None:
    """Handle the localized full-inventory reject row with one OCR state."""

    image = Image.fromarray(screen.astype("uint8"))
    recognizer = _work_text_recognizer()
    reject = recognizer.recognize(image.crop(_DRINK_REJECT_ROW_BOX))
    confirm = recognizer.recognize(image.crop(_DRINK_REJECT_CONFIRM_BOX))
    compact_reject = re.sub(r"\s+", "", reject.text)
    compact_confirm = re.sub(r"\s+", "", confirm.text)
    reject_visible = (
        reject.confidence >= 0.70
        and any(value in compact_reject for value in ("不領取", "不领取"))
    )
    if not reject_visible or confirm.confidence < 0.70:
        return None
    selected = any(
        compact_confirm == value for value in ("不領取", "不领取")
    )
    unselected = any(
        compact_confirm == value for value in ("領取", "领取")
    )
    if not selected and not unselected:
        return None
    kind = "drink-reject-confirm" if selected else "drink-reject-select"
    target = "confirm-no-receive" if selected else "select-no-receive"
    point = _DRINK_REJECT_CONFIRM_POINT if selected else _DRINK_REJECT_ROW_POINT
    box = _DRINK_REJECT_CONFIRM_BOX if selected else _DRINK_REJECT_ROW_BOX
    return NiaLiveOuterSubpage(
        kind=kind,
        target=target,
        action=_fixed_action(
            capture,
            kind=kind,
            target=target,
            point=point,
            verification_box=box,
        ),
        evidence={
            "policy": (
                "ocr-no-receive-selected-then-confirm"
                if selected
                else "ocr-select-no-receive-row"
            ),
            "selected": selected,
            "reject_text": reject.text,
            "confirm_text": confirm.text,
        },
    )


def nia_drink_reject_confirm_after_selection(
    outer: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Advance a Maa-submitted reject row without re-reading its check colour."""

    if outer.get("kind") != "drink-reject-select":
        raise ValueError("drink reject promotion requires the select surface")
    capture = outer.get("capture")
    if not isinstance(capture, Mapping):
        raise ValueError("drink reject surface has no capture authority")
    return {
        "capture": dict(capture),
        "kind": "drink-reject-confirm",
        "target": "confirm-no-receive",
        "action": _fixed_action(
            capture,
            kind="drink-reject-confirm",
            target="confirm-no-receive",
            point=_DRINK_REJECT_CONFIRM_POINT,
            verification_box=_DRINK_REJECT_CONFIRM_BOX,
        ).to_dict(),
        "evidence": {
            **dict(outer.get("evidence", {})),
            "policy": "maa-transaction-select-submitted-then-confirm",
            "selection_submission_authority": "previous-maa-input-success",
        },
        "outer_authority": dict(outer.get("outer_authority", {})),
    }


def _work_business_ocr_choice(
    screen,
    snapshot: ProduceOuterLocalSaveSnapshot,
) -> tuple[str, tuple[int, int, int, int], Mapping[str, Any]]:
    """Choose the best visible business row that current stamina can pay."""

    # ``screen`` is already the canonical 720x1280 RGB ndarray used by the Maa
    # template router.  Convert without resizing so OCR and the eventual Maa
    # click refer to one coordinate system.
    image = Image.fromarray(screen.astype("uint8"))
    recognizer = _work_text_recognizer()
    rows: list[dict[str, Any]] = []
    for index, (center_y, reward_box, cost_box) in enumerate(
        _WORK_BUSINESS_ROWS, start=1
    ):
        reward_read = recognizer.recognize(image.crop(reward_box))
        cost_read = recognizer.recognize(image.crop(cost_box))
        reward_digits = "".join(character for character in reward_read.text if character.isdigit())
        cost_digits = "".join(character for character in cost_read.text if character.isdigit())
        if not reward_digits or reward_read.confidence < 0.70:
            raise ValueError(
                f"N.I.A. Work row {index} reward OCR is unresolved: "
                f"{reward_read.text!r}@{reward_read.confidence:.3f}"
            )
        if not cost_read.text.strip() or cost_read.confidence < 0.45:
            raise ValueError(
                f"N.I.A. Work row {index} stamina OCR is unresolved: "
                f"{cost_read.text!r}@{cost_read.confidence:.3f}"
            )
        reward = int(reward_digits)
        cost = int(cost_digits) if cost_digits else 0
        rows.append(
            {
                "index": index,
                "center_y": center_y,
                "reward": reward,
                "stamina_cost": cost,
                "reward_text": reward_read.text,
                "reward_confidence": reward_read.confidence,
                "cost_text": cost_read.text,
                "cost_confidence": cost_read.confidence,
            }
        )
    stamina = snapshot.stamina
    if isinstance(stamina, bool) or not isinstance(stamina, int) or stamina < 0:
        stamina = 0
    affordable = tuple(
        row for row in rows if row["stamina_cost"] <= stamina
    )
    if not affordable:
        raise ValueError("N.I.A. Work has no stamina-affordable visible row")
    selected = max(
        affordable,
        key=lambda row: (row["reward"], -row["stamina_cost"], -row["index"]),
    )
    center_y = int(selected["center_y"])
    return (
        f"business-row-{selected['index']}",
        (80, center_y - 20, 121, center_y + 21),
        {
            "selection_policy": "ocr-max-affordable-reward-then-min-stamina-cost",
            "rows": rows,
            "current_stamina": stamina,
            "affordable_rows": [row["index"] for row in affordable],
            "selected_row": selected["index"],
            "selected_reward": selected["reward"],
            "selected_stamina_cost": selected["stamina_cost"],
        },
    )


def _work_business_prompt_visible(screen) -> bool:
    """Recognize the localized business-object prompt, independent of skin."""

    image = Image.fromarray(screen.astype("uint8"))
    read = _work_text_recognizer().recognize(image.crop((160, 480, 570, 570)))
    compact = re.sub(r"\s+", "", read.text)
    return read.confidence >= 0.70 and (
        "選擇營業物件" in compact or "選擇管樂物件" in compact
    )


def _work_start_cost_authority(
    screen,
    start_box: tuple[int, int, int, int, float],
    snapshot: ProduceOuterLocalSaveSnapshot,
) -> Mapping[str, Any]:
    """Read the selected expanded row's cost before authorizing Start."""

    image = Image.fromarray(screen.astype("uint8"))
    top = int(start_box[1])
    cost_box = (340, max(0, top - 300), 690, max(1, top - 205))
    read = _work_text_recognizer().recognize(image.crop(cost_box))
    compact = re.sub(r"\s+", "", read.text)
    digits = "".join(character for character in compact if character.isdigit())
    no_cost = any(marker in compact for marker in ("無消耗", "无消耗"))
    if read.confidence < 0.45 or (not digits and not no_cost):
        raise ValueError(
            "N.I.A. selected Work stamina cost is unresolved: "
            f"{read.text!r}@{read.confidence:.3f}"
        )
    cost = 0 if no_cost else int(digits)
    stamina = snapshot.stamina
    if isinstance(stamina, bool) or not isinstance(stamina, int) or stamina < 0:
        stamina = 0
    return {
        "authority": "selected-work-visible-cost+produce-localsave-stamina",
        "stamina": stamina,
        "stamina_cost": cost,
        "affordable": cost <= stamina,
        "cost_text": read.text,
        "cost_confidence": read.confidence,
        "cost_box": list(cost_box),
    }


def _outing_ocr_choice(
    screen,
    capture: Mapping[str, Any],
    snapshot: ProduceOuterLocalSaveSnapshot,
) -> NiaLiveOuterSubpage | None:
    """Choose an outing row by its visible name and P cost.

    Maa exposes only the dialogue fast-forward control on this page.  Row hit
    areas are stable, but row meaning comes from OCR so reordered/new rows do
    not inherit an old ordinal.  The highest affordable displayed cost is the
    stronger outing bundle.  LocalSave, not another screenshot gate, confirms
    the result after input.
    """

    image = Image.fromarray(screen.astype("uint8"))
    recognizer = _work_text_recognizer()
    points = snapshot.produce_points
    points_source = "produce-local-save"
    if not isinstance(points, int) or isinstance(points, bool) or points < 0:
        # Initial's outer LocalSave variant does not always serialize current
        # Produce points, even though the outing screen renders an explicit
        # ``P 80`` counter.  Use that visible numeric authority only when the
        # Save field is absent; do not infer points from enabled colours or
        # from the option order.
        point_read = recognizer.recognize(image.crop((275, 100, 430, 155)))
        point_match = re.search(r"P\s*(\d+)", point_read.text, flags=re.IGNORECASE)
        if point_read.confidence < 0.70 or point_match is None:
            return None
        points = int(point_match.group(1))
        points_source = "visible-p-counter"
    rows = (
        ((40, 635, 680, 750), (500, 625, 690, 695)),
        ((40, 745, 680, 855), (500, 735, 690, 805)),
        ((40, 850, 680, 960), (500, 840, 690, 910)),
    )
    known_names = ("博物館", "博物馆", "美術館", "美术馆", "演唱會", "演唱会")
    visible: list[dict[str, Any]] = []
    explicit_point_costs = 0
    known_name_count = 0
    for index, (row_box, cost_box) in enumerate(rows, start=1):
        row_read = recognizer.recognize(image.crop(row_box))
        compact = re.sub(r"\s+", "", row_read.text)
        # The selectable outing copy changes with the scenario and locale.  The
        # row itself is the authority; do not require it to match an old list of
        # museum/concert labels.  OCR occasionally appends a nearby score/cost,
        # so remove only that trailing decoration for the action description.
        canonical_known = next(
            (value for value in known_names if value in compact),
            None,
        )
        if canonical_known is not None:
            known_name_count += 1
        name = canonical_known or re.sub(r"[<>+−-]?\d+$", "", compact).strip()
        if not name or row_read.confidence < 0.60:
            continue
        cost_read = recognizer.recognize(image.crop(cost_box))
        # The P icon is graphical in some localized layouts, so OCR often
        # returns just ``-50``.  The signed number is the semantic price; do
        # not make recognition depend on the icon text or its colour.
        cost_is_explicit = bool(
            re.search(r"(?:P\s*)?[-−－]\s*\d+", cost_read.text, flags=re.IGNORECASE)
        )
        if cost_is_explicit:
            explicit_point_costs += 1
        digits = re.findall(r"\d+", cost_read.text)
        visible.append(
            {
                "index": index,
                "name": name,
                "cost": int(digits[-1]) if digits else 0,
                "cost_is_explicit": cost_is_explicit,
                "box": row_box,
                "text": row_read.text,
                "confidence": row_read.confidence,
            }
        )
    # The dialogue page owns rows with explicit Produce-point prices.  This
    # keeps other three-row inventories/modals from being mistaken for an
    # outing without constraining the localized option text or its ordering.
    if len(visible) < 2 or explicit_point_costs < 1:
        return None
    # A visible choice without a rendered P deduction is the zero-cost row.
    # Initial's follow-up outing dialogue commonly contains one paid challenge
    # and one free "finish here" choice.  Dropping the latter when the paid
    # row is unaffordable turns the whole settled page into an unknown surface.
    # Row visibility and text are already established above; no colour or
    # ordinal inference is needed here.
    candidates = tuple(visible)
    affordable = tuple(row for row in candidates if row["cost"] <= points)
    if not affordable:
        return None
    selected = max(
        affordable,
        key=lambda row: (row["cost"], -row["index"]),
    )
    box = selected["box"]
    return NiaLiveOuterSubpage(
        kind="outing-choice",
        target=str(selected["name"]),
        action=_fixed_action(
            capture,
            kind="outing-choice",
            target=str(selected["name"]),
            point=(360, (box[1] + box[3]) // 2),
            verification_box=box,
            click_count=2,
        ),
        evidence={
            "policy": "ocr-visible-name-and-highest-affordable-cost",
            "selected_row": selected["index"],
            "selected_name": selected["name"],
            "selected_cost": selected["cost"],
            "produce_points": points,
            "produce_points_source": points_source,
            "visible": [
                {"row": row["index"], "name": row["name"], "cost": row["cost"]}
                for row in visible
            ],
        },
    )


def _work_attribute_parameters(
    screen,
    snapshot: ProduceOuterLocalSaveSnapshot,
) -> tuple[Mapping[str, int], Mapping[str, Any]]:
    """Use LocalSave values first and OCR only attributes not logged yet."""

    image = Image.fromarray(screen.astype("uint8"))
    recognizer = _work_text_recognizer()
    values: dict[str, int] = {}
    evidence: dict[str, Any] = {}
    for attribute, field in (("Vo", "vocal"), ("Da", "dance"), ("Vi", "visual")):
        local = getattr(snapshot, field)
        if isinstance(local, int) and not isinstance(local, bool) and local >= 0:
            values[attribute] = local
            evidence[attribute] = {"authority": "ProduceLocalSave", "value": local}
            continue
        read = recognizer.recognize(image.crop(_WORK_ATTRIBUTE_HUD_BOXES[attribute]))
        digits = "".join(character for character in read.text if character.isdigit())
        if not digits or read.confidence < 0.70:
            raise ValueError(
                f"N.I.A. Work {attribute} HUD OCR is unresolved: "
                f"{read.text!r}@{read.confidence:.3f}"
            )
        value = int(digits)
        values[attribute] = value
        evidence[attribute] = {
            "authority": "work-page-hud-ocr-fallback",
            "value": value,
            "text": read.text,
            "confidence": read.confidence,
        }
    return values, evidence


def _work_choice(
    screen,
    snapshot: ProduceOuterLocalSaveSnapshot,
    *,
    recognized: Mapping[str, tuple[int, int, int, int, float]] | None = None,
) -> tuple[str, tuple[int, int, int, int], Mapping[str, Any]] | None:
    marker = (
        recognized.get("work-page")
        if recognized is not None
        else _match(
            screen,
            NIA_TEMPLATE_DIR / _WORK_PAGE_TEMPLATE,
            roi=(150, 350, 570, 610),
            threshold=0.90,
        )
    )
    visible: dict[str, tuple[int, int, int, int, float]] = {}
    for attribute, filename in _WORK_OPTION_TEMPLATES.items():
        match = (
            recognized.get(f"work-{attribute.lower()}")
            if recognized is not None
            else _match(
                screen,
                DEFAULT_TEMPLATE_DIR / filename,
                roi=(0, 620, 720, 1010),
                threshold=0.90,
            )
        )
        if match is not None:
            visible[attribute] = match
    # ``choose_null`` is reused by other dialogs (notably Initial outing's
    # orange decline row).  A lone null-button hit never proves the Work page,
    # including on Maa's production batched-recognition path.  Exact Vo/Da/Vi
    # rows remain independently authoritative because those templates encode
    # the actual attribute choice.
    if marker is None:
        visible.pop("none", None)
    # A blank grey crop can weakly resemble choose_null.  Null is a real
    # fallback only when no exact Vo/Da/Vi row is present; it must never
    # override an attribute button already proven by its own template.
    if any(value in visible for value in ("Vo", "Da", "Vi")):
        visible.pop("none", None)
    candidates = tuple(value for value in ("Vo", "Da", "Vi") if value in visible)
    if candidates:
        parameters, parameter_evidence = _work_attribute_parameters(screen, snapshot)
        # Filling the largest remaining parameter headroom is equivalent to
        # selecting the lowest current parameter because each N.I.A. mode has
        # one common cap.  No exact reward/gain prediction is attempted.
        selected = min(candidates, key=lambda value: (parameters[value], value))
    elif "none" in visible:
        selected = "none"
    else:
        if marker is None:
            return None
        return _work_business_ocr_choice(screen, snapshot)
    box = visible[selected][:4]
    return selected, box, {
        "marker_score": None if marker is None else marker[4],
        "visible_choices": list(visible),
        "selection_policy": "largest-parameter-headroom",
        "parameters": (
            dict(parameters)
            if candidates
            else {
                value: _parameter(snapshot, value)
                for value in ("Vo", "Da", "Vi")
            }
        ),
        "parameter_evidence": (
            dict(parameter_evidence) if candidates else {}
        ),
    }


def _event_attribute_parameters(
    screen,
    snapshot: ProduceOuterLocalSaveSnapshot,
) -> tuple[Mapping[str, int], Mapping[str, Any]]:
    """Use ProduceLocalSave values and OCR only attributes not logged yet."""

    parameters: dict[str, int] = {}
    parameter_evidence: dict[str, Any] = {}
    image = Image.fromarray(screen.astype("uint8"))
    recognizer = _work_text_recognizer()
    for attribute, field in (
        ("Vo", "vocal"),
        ("Da", "dance"),
        ("Vi", "visual"),
    ):
        local = getattr(snapshot, field)
        if isinstance(local, int) and not isinstance(local, bool) and local >= 0:
            parameters[attribute] = local
            parameter_evidence[attribute] = {
                "authority": "ProduceLocalSave",
                "value": local,
            }
            continue
        read = recognizer.recognize(
            image.crop(_EVENT_ATTRIBUTE_HUD_BOXES[attribute])
        )
        digits = "".join(character for character in read.text if character.isdigit())
        if not digits or read.confidence < 0.70:
            raise ValueError(
                f"N.I.A. event {attribute} HUD OCR is unresolved: "
                f"{read.text!r}@{read.confidence:.3f}"
            )
        value = int(digits)
        parameters[attribute] = value
        parameter_evidence[attribute] = {
            "authority": "event-page-hud-ocr-fallback",
            "value": value,
            "text": read.text,
            "confidence": read.confidence,
        }
    return parameters, parameter_evidence


def _event_choice(
    screen,
    capture: Mapping[str, Any],
    snapshot: ProduceOuterLocalSaveSnapshot,
    *,
    recognized: Mapping[str, tuple[int, int, int, int, float]] | None = None,
    produce_id: str | None = None,
    idol_card_id: str | None = None,
    pending_event_choice: NiaEventChoicePending | Mapping[str, Any] | None = None,
) -> NiaLiveOuterSubpage | None:
    # A visible SELECT is only a completion signal after this reader has
    # observed the same page's preceding selection transaction.  The generic
    # event path carries that small binding as ``pending_event_choice``;
    # Master-bound ADV uses its richer NiaEventChoicePending resume path above.
    # Do not infer ownership from a preselected first frame.
    pending_target: str | None = None
    pending_authority_matches: bool | None = None
    pending_capture_matches: bool | None = None
    if isinstance(pending_event_choice, Mapping):
        raw_target = pending_event_choice.get("target")
        if isinstance(raw_target, str) and raw_target:
            pending_target = raw_target
        if isinstance(pending_event_choice.get("authority"), Mapping):
            pending_authority_matches = _event_choice_authority_matches(
                snapshot,
                pending_event_choice,
            )
            if not pending_authority_matches:
                pending_target = None
        pending_capture = pending_event_choice.get("source_capture")
        if isinstance(pending_capture, Mapping):
            source_hwnd = pending_capture.get("hwnd")
            source_pid = pending_capture.get("pid")
            current_hwnd = capture.get("hwnd")
            current_pid = capture.get("pid")
            if source_hwnd is not None and source_pid is not None:
                pending_capture_matches = (
                    source_hwnd == current_hwnd and source_pid == current_pid
                )
                if not pending_capture_matches:
                    pending_target = None
    elif pending_event_choice is not None:
        raw_target = getattr(pending_event_choice, "target", None)
        if isinstance(raw_target, str) and raw_target:
            pending_target = raw_target
        if isinstance(getattr(pending_event_choice, "authority", None), Mapping):
            pending_authority_matches = _event_choice_authority_matches(
                snapshot,
                pending_event_choice,
            )
            if not pending_authority_matches:
                pending_target = None

    visible: dict[str, tuple[int, int, int, int, float]] = {}
    for action, filename in _EVENT_OPTION_TEMPLATES.items():
        label = {
            ACTIVITY: "event-action",
            CONSULTATION: "event-consultation",
            DANCE_LESSON: "event-dance",
            OUTING: "event-outing",
            SPECIAL_GUIDANCE: "event-guide",
            VISUAL_LESSON: "event-visual",
            VOCAL_LESSON: "event-vocal",
            BUSINESS: "event-business",
            REST: "event-rest",
        }[action]
        match = (
            recognized.get(label)
            if recognized is not None
            else _match(
                screen,
                NIA_TEMPLATE_DIR / filename,
                roi=(0, 850, 720, 1130),
                threshold=0.92,
            )
        )
        if match is not None:
            visible[action] = match
    # The shared rest bitmap weakly overlaps some single-option event
    # backgrounds.  Only Maa's named weekly-action batch is authoritative for
    # Rest; never synthesize it from a second local template pass.
    rest_match = None if recognized is None else recognized.get("event-rest")
    if rest_match is not None:
        visible[REST] = rest_match
    if not visible:
        return None

    locked_pending_target: str | None = None
    if pending_target is not None:
        canonical_pending = (
            pending_target[:-8]
            if pending_target.endswith("-confirm")
            else pending_target
        )
        if canonical_pending not in visible:
            return NiaLiveOuterSubpage(
                kind="wait",
                target="pending-event-choice-target-not-visible",
                action=None,
                evidence={
                    "policy": "pending-event-choice-owner-never-reselects",
                    "pending_target": canonical_pending,
                    "visible_actions": sorted(visible),
                    "pending_authority_matches": pending_authority_matches,
                    "pending_capture_matches": pending_capture_matches,
                },
            )
        locked_pending_target = canonical_pending

    attributes = tuple(
        action for action in _EVENT_ATTRIBUTE_POINT if action in visible
    )
    sp_actions: list[str] = []
    sp_template = DEFAULT_TEMPLATE_DIR / "sp.png"
    for index, action in enumerate(
        (VOCAL_LESSON, DANCE_LESSON, VISUAL_LESSON)
    ):
        if action not in visible:
            continue
        badge = (
            recognized.get(f"event-sp-{index}")
            if recognized is not None
            else None
        )
        if badge is None:
            # The selected training tile redraws the same official SP badge
            # with a brighter outline.  Maa's 0.90 batch match can therefore
            # lose exactly the selected SP action and make the recommendation
            # oscillate between captures.  Reuse the same bundled template at
            # 0.85 only for a missing batch hit; real captures keep non-SP
            # alternatives below 0.80 while selected/unselected SP variants
            # remain at 0.87 or higher.
            badge = _match(
                screen,
                sp_template,
                roi=(45 + 180 * index, 870, 155 + 180 * index, 970),
                threshold=0.85,
            )
        if badge is not None:
            sp_actions.append(action)

    parameters: Mapping[str, int] = {}
    parameter_evidence: Mapping[str, Any] = {}
    if attributes:
        parameters, parameter_evidence = _event_attribute_parameters(
            screen, snapshot
        )

    def attribute_key(action: str) -> tuple[bool, int, str]:
        field = _EVENT_ATTRIBUTE_VALUE[action]
        attribute = {"vocal": "Vo", "dance": "Da", "visual": "Vi"}[field]
        value = parameters[attribute]
        return action in sp_actions, -value, action

    advisor_evidence: dict[str, Any] = {}
    selected: str | None = locked_pending_target
    if (
        selected is None
        and produce_id in {"produce-004", "produce-005"}
        and isinstance(idol_card_id, str)
        and idol_card_id
    ):
        from .nia_outer_advisor import (
            DEFAULT_NIA_LIVE_POLICY,
            STATUS_READY,
            advise_nia_outer,
        )

        stamina = snapshot.stamina
        max_stamina = snapshot.max_stamina
        stamina_evidence: dict[str, Any] = {
            "current_authority": "ProduceLocalSave",
            "current": stamina,
            "maximum": max_stamina,
        }
        if (
            not isinstance(stamina, int)
            or isinstance(stamina, bool)
            or not isinstance(max_stamina, int)
            or isinstance(max_stamina, bool)
            or max_stamina <= 0
        ):
            stamina_read = _work_text_recognizer().recognize(
                Image.fromarray(screen.astype("uint8")).crop((280, 35, 440, 110))
            )
            stamina_match = re.search(r"(\d+)\s*/\s*(\d+)", stamina_read.text)
            stamina_evidence.update(
                {
                    "hud_text": stamina_read.text,
                    "hud_confidence": stamina_read.confidence,
                }
            )
            if stamina_read.confidence >= 0.70 and stamina_match is not None:
                if not isinstance(stamina, int) or isinstance(stamina, bool):
                    stamina = int(stamina_match.group(1))
                    stamina_evidence["current_authority"] = "event-page-hud-ocr"
                max_stamina = int(stamina_match.group(2))
                stamina_evidence["maximum_authority"] = "event-page-hud-ocr"
        state = {
            "stamina": stamina,
            "max_stamina": max_stamina,
            "vocal": parameters.get("Vo", snapshot.vocal),
            "dance": parameters.get("Da", snapshot.dance),
            "visual": parameters.get("Vi", snapshot.visual),
        }
        advice = advise_nia_outer(
            snapshot,
            overview_output={
                "options": [
                    {"action": action, "is_sp": action in sp_actions}
                    for action in sorted(visible)
                ],
                "state": state,
            },
            produce_id=produce_id,
            idol_card_id=idol_card_id,
            policy=DEFAULT_NIA_LIVE_POLICY,
            run_shadow=_live_active_run_shadow(produce_id, idol_card_id),
        )
        advisor_evidence = {
            "advisor_schema": advice.schema,
            "advisor_status": advice.status,
            "advisor_reason": advice.reason,
            "advisor_action": advice.action,
            "eligible_actions": list(advice.eligible_actions),
            "estimated_lesson_hp_cost": advice.estimated_lesson_hp_cost,
            "predicted_hp": advice.predicted_hp,
            "stamina_evidence": stamina_evidence,
        }
        if advice.status == STATUS_READY and advice.action in visible:
            selected = advice.action
        else:
            return NiaLiveOuterSubpage(
                kind="wait",
                target="event-weekly-advice-unavailable",
                action=None,
                evidence={
                    "policy": "single-nia-outer-advisor-only",
                    "visible_actions": sorted(visible),
                    "sp_actions": sorted(sp_actions),
                    **advisor_evidence,
                },
            )
    if selected is None:
        hp_low = bool(
            isinstance(snapshot.stamina, int)
            and not isinstance(snapshot.stamina, bool)
            and (
                snapshot.stamina < 8
                or (
                    isinstance(snapshot.max_stamina, int)
                    and not isinstance(snapshot.max_stamina, bool)
                    and snapshot.max_stamina > 0
                    and snapshot.stamina * 5 < snapshot.max_stamina
                )
            )
        )
        if hp_low and REST in visible:
            selected = REST
        elif hp_low and OUTING in visible:
            selected = OUTING
        elif sp_actions:
            selected = max(sp_actions, key=attribute_key)
        elif BUSINESS in visible:
            selected = BUSINESS
        elif attributes:
            selected = max(attributes, key=attribute_key)
        else:
            selected = next(
                action
                for action in (
                    ACTIVITY,
                    OUTING,
                    SPECIAL_GUIDANCE,
                    CONSULTATION,
                    REST,
                )
                if action in visible
            )

    marker = visible[selected]
    select_box = (280, 1060, 440, 1145)
    select_read = _work_text_recognizer().recognize(
        Image.fromarray(screen.astype("uint8")).crop(select_box)
    )
    normalized_select = "".join(
        character for character in str(select_read.text).upper() if character.isalpha()
    )
    select_similarity = SequenceMatcher(
        None, normalized_select, "SELECT"
    ).ratio()
    explicit_select_ocr = (
        select_read.confidence >= 0.50 and select_similarity >= 0.65
    )
    actual_selected: str | None = None
    selection_mode = ""
    selected_scores: dict[str, int] = {}
    generic_scores: list[int] = []

    # On the current three-training page, SELECT is drawn under the selected
    # tile rather than in the old fixed centre OCR band.  Measure only that
    # narrow orange/yellow label band; the ordinary Vo/Da/Vi card art contains
    # similar colours elsewhere and is not selection evidence.
    if len(visible) > 1:
        label_scores: dict[str, int] = {}
        for action, marker_box in visible.items():
            box = _event_select_label_box(action, marker_box)
            left, top, right, bottom = box
            tile = screen[top:bottom, left:right, :3]
            red = tile[:, :, 0]
            green = tile[:, :, 1]
            blue = tile[:, :, 2]
            label_scores[action] = int(
                (
                    (red > 220)
                    & (green > 80)
                    & (green < 210)
                    & (blue < 180)
                ).sum()
            )
        ranked_labels = sorted(
            label_scores,
            key=lambda action: (label_scores[action], action),
            reverse=True,
        )
        if len(ranked_labels) >= 2:
            label_selected = ranked_labels[0]
            label_runner_up = label_scores[ranked_labels[1]]
            if (
                label_scores[label_selected] >= 1_500
                and label_scores[label_selected] - label_runner_up >= 1_000
            ):
                actual_selected = label_selected
                selection_mode = "visible-select-label"
                selected_scores = label_scores

    if len(visible) > 1 and actual_selected is None and explicit_select_ocr:
        for action, box in _EVENT_SELECTED_ATTRIBUTE_BOXES.items():
            left, top, right, bottom = box
            tile = screen[top:bottom, left:right, :3]
            red = tile[:, :, 0]
            green = tile[:, :, 1]
            blue = tile[:, :, 2]
            selected_scores[action] = int(
                ((red < 100) & (green > 160) & (blue > 170)).sum()
            )
        ranked_selected = sorted(
            selected_scores,
            key=lambda action: (selected_scores[action], action),
            reverse=True,
        )
        cyan_selected = ranked_selected[0]
        cyan_runner_up = selected_scores[ranked_selected[1]]
        if (
            selected_scores[cyan_selected] >= 1_000
            and selected_scores[cyan_selected] - cyan_runner_up >= 500
            and cyan_selected in visible
        ):
            actual_selected = cyan_selected
            selection_mode = "attribute-cyan"
        else:
            for box in _EVENT_SELECTED_GENERIC_BOXES:
                left, top, right, bottom = box
                tile = screen[top:bottom, left:right, :3]
                red = tile[:, :, 0]
                green = tile[:, :, 1]
                blue = tile[:, :, 2]
                generic_scores.append(
                    int(
                        (
                            (red > 220)
                            & (green > 70)
                            & (green < 200)
                            & (blue < 180)
                        ).sum()
                    )
                )
            ranked_generic = sorted(
                range(len(generic_scores)),
                key=lambda index: (generic_scores[index], -index),
                reverse=True,
            )
            selected_index = ranked_generic[0]
            generic_runner_up = generic_scores[ranked_generic[1]]
            slot_center = (197, 362, 527)[selected_index]
            ordered_visible = sorted(
                visible,
                key=lambda action: (
                    (visible[action][0] + visible[action][2]) // 2,
                    action,
                ),
            )
            three_slot_selected = (
                len(ordered_visible) == len(_EVENT_SELECTED_GENERIC_BOXES)
                and abs(
                    (
                        visible[ordered_visible[selected_index]][0]
                        + visible[ordered_visible[selected_index]][2]
                    )
                    // 2
                    - slot_center
                )
                <= 80
            )
            if (
                generic_scores[selected_index] >= 1_000
                and generic_scores[selected_index] - generic_runner_up >= 500
                and three_slot_selected
            ):
                actual_selected = ordered_visible[selected_index]
                selection_mode = "generic-orange"
                selected_scores = {
                    f"slot-{index + 1}": score
                    for index, score in enumerate(generic_scores)
                }

    visually_selected_attribute = (
        actual_selected in _EVENT_ATTRIBUTE_POINT
        and actual_selected in visible
        and selection_mode
        in {"attribute-cyan", "generic-orange", "visible-select-label"}
    )
    if explicit_select_ocr or visually_selected_attribute:
        if len(visible) == 1:
            # SELECT is rendered only after a tile has been selected.  When
            # exactly one Maa-identified legal tile exists, that tile is the
            # only possible owner; its colour varies by action (purple
            # Business, green Special Guidance) and is not identity evidence.
            actual_selected = next(iter(visible))
            selection_mode = "single-visible-explicit-select"
            selected_scores = {}
        if actual_selected not in visible:
            return NiaLiveOuterSubpage(
                kind="wait",
                target="event-selected-option-unresolved",
                action=None,
                evidence={
                    "policy": "zero-input-until-selected-event-option-is-exact",
                    "select_text": select_read.text,
                    "select_confidence": select_read.confidence,
                    "selected_scores": selected_scores,
                },
            )
        # A selected tile may be a carry-over from the action that opened this
        # page.  Moving the selection is not a submission.  Confirm only when
        # the current SELECT belongs to the same recommendation that this
        # reader selected on the immediately preceding frame; otherwise click
        # the recommendation once and let the next capture prove ownership.
        pending_matches = pending_target in {selected, f"{selected}-confirm"}
        if actual_selected == selected and pending_matches:
            target = f"{actual_selected}-confirm"
            selected_marker = visible[actual_selected]
            confirm_point = _EVENT_ATTRIBUTE_POINT.get(
                actual_selected,
                (
                    (selected_marker[0] + selected_marker[2]) // 2,
                    (selected_marker[1] + selected_marker[3]) // 2,
                ),
            )
            verification_box = _EVENT_SELECTED_ATTRIBUTE_BOXES.get(
                actual_selected,
                _event_select_label_box(actual_selected, selected_marker),
            )
            return NiaLiveOuterSubpage(
                kind="event-choice-confirm",
                target=target,
                action=_fixed_action(
                    capture,
                    kind="event-choice-confirm",
                    target=target,
                    point=confirm_point,
                    verification_box=verification_box,
                ),
                evidence={
                    "policy": "selected-event-option-then-explicit-select",
                    "recommended": selected,
                    "selected": actual_selected,
                    "pending_target": pending_target,
                    "pending_authority_matches": pending_authority_matches,
                    "pending_capture_matches": pending_capture_matches,
                    "selection_mode": selection_mode,
                    "selected_scores": selected_scores,
                    "visible_actions": sorted(visible),
                    "sp_actions": sorted(sp_actions),
                    "parameters": parameters,
                    "select_text": select_read.text,
                    "select_confidence": select_read.confidence,
                    "select_similarity": select_similarity,
                    **advisor_evidence,
                },
            )
    point = _EVENT_ATTRIBUTE_POINT.get(
        selected,
        ((marker[0] + marker[2]) // 2, (marker[1] + marker[3]) // 2),
    )
    return NiaLiveOuterSubpage(
        kind="event-choice",
        target=selected,
        action=_fixed_action(
            capture,
            kind="event-choice",
            target=selected,
            point=point,
            verification_box=marker[:4],
            click_count=1,
        ),
        evidence={
            "match_score": marker[4],
            "policy": "maa-sp-local-save-or-hud-headroom-v2",
            "pending_target": pending_target,
            "pending_authority_matches": pending_authority_matches,
            "pending_capture_matches": pending_capture_matches,
            "select_text": select_read.text,
            "select_confidence": select_read.confidence,
            "select_similarity": select_similarity,
            "selected": actual_selected,
            "visible_actions": sorted(visible),
            "sp_actions": sorted(sp_actions),
            "parameters": parameters,
            "parameter_evidence": parameter_evidence,
            **advisor_evidence,
        },
    )


def _master_bound_event_choice(
    screen: np.ndarray,
    capture: Mapping[str, Any],
    snapshot: ProduceOuterLocalSaveSnapshot,
    *,
    idol_card_id: str | None,
    produce_id: str | None,
    required_choice_count: int | None,
    owner: str,
    click_count: int,
    current_drink_count: int | None = None,
) -> NiaLiveOuterSubpage | None:
    """Resolve one complete ADV choice set through localized Master data.

    ``owner`` is deliberately explicit.  The ordinary static ADV reader and
    the outing transaction share the catalog/advisor, but they are separate
    surface owners: callers must choose which transaction is allowed to emit
    the click.  Outing also requires all three rows to bind and uses Maa's
    two-click choice submission.
    """

    if not idol_card_id or produce_id not in {"produce-004", "produce-005"}:
        return None
    if owner not in {"static", "outing"}:
        raise ValueError("unsupported Master event-choice owner")
    if click_count not in {1, 2}:
        raise ValueError("event-choice click count must be one or two")
    if (
        required_choice_count is not None
        and (
            isinstance(required_choice_count, bool)
            or not isinstance(required_choice_count, int)
            or required_choice_count < 2
        )
    ):
        raise ValueError("required event-choice count must be at least two")
    from .event_choice_catalog import advise_event_choices, resolve_visible_event_choices
    from .master_db import get_idol_profile
    from .nia_route_profile import nia_final_week

    profile = get_idol_profile(idol_card_id)
    if profile is None:
        return None
    image = Image.fromarray(screen.astype("uint8"))
    recognizer = _work_text_recognizer()
    resolved = None
    observed: list[str] = []
    choice_rows: tuple[tuple[int, int, int, int], ...] = ()
    for layout, text_layout in zip(
        _STATIC_EVENT_CHOICE_LAYOUTS,
        _STATIC_EVENT_CHOICE_TEXT_LAYOUTS,
        strict=True,
    ):
        reads = [recognizer.recognize(image.crop(box)) for box in text_layout]
        layout_observed: list[str] = []
        for read in reads:
            compact = re.sub(r"\s+", "", str(read.text))
            if read.confidence < 0.45 or len(compact) < 2:
                break
            layout_observed.append(read.text)
        if len(layout_observed) < 2:
            continue
        # A two-choice page can leave dialogue in the third band.  The static
        # owner resolves the longest contiguous Master-backed prefix; outing
        # requires exactly all three visible rows so a stray dialogue line can
        # never be submitted as a choice.
        choice_counts = (
            (required_choice_count,)
            if required_choice_count is not None
            else range(len(layout_observed), 1, -1)
        )
        for choice_count in choice_counts:
            if choice_count > len(layout_observed):
                continue
            try:
                candidate = resolve_visible_event_choices(
                    profile.character_id,
                    tuple(layout_observed[:choice_count]),
                )
            except (
                FileNotFoundError,
                KeyError,
                LookupError,
                OSError,
                TypeError,
                ValueError,
            ):
                continue
            if len(candidate.resolution.choices) == choice_count:
                resolved = candidate
                observed = layout_observed[:choice_count]
                choice_rows = layout[:choice_count]
                break
        if resolved is not None:
            break
    if resolved is None:
        return None
    observed = list(resolved.observed_texts)
    stamina = snapshot.stamina
    max_stamina = snapshot.max_stamina
    points = snapshot.produce_points
    if not isinstance(stamina, int) or isinstance(stamina, bool):
        return None
    if not isinstance(max_stamina, int) or isinstance(max_stamina, bool):
        stamina_read = recognizer.recognize(image.crop((280, 35, 440, 100)))
        match = re.search(r"(\d+)\s*/\s*(\d+)", stamina_read.text)
        if stamina_read.confidence < 0.70 or match is None:
            return None
        max_stamina = int(match.group(2))
    if not isinstance(points, int) or isinstance(points, bool):
        return None
    try:
        advice = advise_event_choices(
            resolved,
            stamina=stamina,
            max_stamina=max_stamina,
            produce_points=points,
            weeks_remaining=max(
                0,
                nia_final_week(produce_id)
                - int(snapshot.latest_week_marker or 0),
            ),
            current_drink_count=current_drink_count,
        )
    except LookupError as error:
        if (
            owner == "outing"
            and current_drink_count is not None
            and "within drink capacity" in str(error)
        ):
            # A reliable Master bind with no capacity-compatible branch is a
            # typed zero-input wait, not permission to hand the same rows to
            # the weaker OCR owner (which may choose a drink anyway).
            return NiaLiveOuterSubpage(
                kind="wait",
                target="outing-master-choice-capacity-unavailable",
                action=None,
                evidence={
                    "policy": "outing-master-bind-no-capacity-compatible-choice",
                    "current_drink_count": current_drink_count,
                },
            )
        return None
    except (TypeError, ValueError):
        return None

    # The click must carry the exact Master suggestion selected by the
    # advisor, not merely an ordinal row.  Keep both projections independent:
    # the advisor candidate proves which suggestion it ranked, while the
    # resolved Master choice provides the ordered effect sequence executed by
    # that suggestion.  Any duplicate/missing/mismatched identity is a typed
    # zero-input result; falling back to a row click would silently attach the
    # wrong semantics to the durable pending transaction.
    advised_candidates = tuple(
        candidate
        for candidate in advice.candidates
        if isinstance(candidate, Mapping)
        and candidate.get("slot") == advice.slot
    )
    resolved_choices = tuple(
        choice
        for choice in resolved.resolution.choices
        if choice.slot == advice.slot
    )
    if len(advised_candidates) != 1 or len(resolved_choices) != 1:
        return NiaLiveOuterSubpage(
            kind="wait",
            target=f"{owner}-master-selected-identity-unresolved",
            action=None,
            evidence={
                "policy": "master-selected-suggestion-must-bind-uniquely",
                "owner": owner,
                "adv_asset_id": resolved.entry.adv_asset_id,
                "chosen_slot": advice.slot,
                "advice_candidate_match_count": len(advised_candidates),
                "master_choice_match_count": len(resolved_choices),
            },
        )
    advised_candidate = advised_candidates[0]
    resolved_choice = resolved_choices[0]
    selected_suggestion_id = advised_candidate.get("suggestion_id")
    if (
        not isinstance(selected_suggestion_id, str)
        or not selected_suggestion_id
        or selected_suggestion_id != resolved_choice.suggestion_id
    ):
        return NiaLiveOuterSubpage(
            kind="wait",
            target=f"{owner}-master-selected-identity-unresolved",
            action=None,
            evidence={
                "policy": "master-selected-suggestion-must-match-advisor",
                "owner": owner,
                "adv_asset_id": resolved.entry.adv_asset_id,
                "chosen_slot": advice.slot,
                "advice_suggestion_id": selected_suggestion_id,
                "master_suggestion_id": resolved_choice.suggestion_id,
            },
        )
    selected_effect_ids = list(resolved_choice.effect_ids)
    selected_success_effect_ids = list(resolved_choice.success_effect_ids)
    selected_fail_effect_ids = list(resolved_choice.fail_effect_ids)

    selected_scores = _event_choice_selected_scores(screen, choice_rows)
    selected_slots = [
        index + 1 for index, score in enumerate(selected_scores) if score >= 5_000
    ]
    # Preserve the original Master reader's permissive selected-slot
    # classification for evidence, but never let the initial frame submit.
    # The stricter margin/unique proof is reserved for the typed resume path,
    # where the prior same-page selection is the only evidence allowed to emit
    # a confirm.
    selected_slot = selected_slots[0] if len(selected_slots) == 1 else None
    chosen_box = choice_rows[advice.slot - 1]
    if owner == "outing":
        # Outing rows use the same visible geometry, but selecting a row is not
        # the transaction: the second click is Maa's required submit gesture.
        kind = "outing-choice"
    else:
        kind = "static-event-choice-select"
    target = f"{resolved.entry.adv_asset_id}:slot-{advice.slot}"
    return NiaLiveOuterSubpage(
        kind=kind,
        target=target,
        action=_action(
            capture,
            kind=kind,
            target=target,
            box=chosen_box,
            click_count=click_count,
        ),
        evidence={
            "policy": (
                f"outing-master-{advice.reason}"
                if owner == "outing"
                else advice.reason
            ),
            "owner": owner,
            "required_choice_count": required_choice_count,
            "adv_asset_id": resolved.entry.adv_asset_id,
            "detail_ids": list(resolved.resolution.matching_detail_ids),
            "choice_texts": list(resolved.entry.choice_texts),
            "observed_texts": list(resolved.observed_texts),
            "similarities": list(resolved.similarities),
            "chosen_slot": advice.slot,
            "chosen_score": advice.score,
            "selected_suggestion_id": selected_suggestion_id,
            "selected_effect_ids": selected_effect_ids,
            "selected_success_effect_ids": selected_success_effect_ids,
            "selected_fail_effect_ids": selected_fail_effect_ids,
            "selected_success_probability_permyriad": (
                resolved_choice.success_probability_permyriad
            ),
            "selected_slot": selected_slot,
            "selected_scores": selected_scores,
            "choice_rows": [list(row) for row in choice_rows],
            "candidates": list(advice.candidates),
            "stamina": stamina,
            "max_stamina": max_stamina,
            "produce_points": points,
            "current_drink_count": current_drink_count,
        },
    )


def _resume_master_event_choice(
    image: Image.Image,
    capture: Mapping[str, Any],
    snapshot: ProduceOuterLocalSaveSnapshot,
    *,
    pending_event_choice: NiaEventChoicePending | Mapping[str, Any],
    produce_id: str | None,
    idol_card_id: str | None,
) -> NiaLiveOuterSubpage:
    """Confirm a selected row from the original typed Master resolution.

    This path intentionally performs no full ADV OCR.  The source capture's
    stable unselected rows establish the same event screen, while the fresh
    frame contributes exactly one selected-slot proof.  The LocalSave CAS is
    carried separately by the caller and remains unchanged until the action
    is submitted.
    """

    pending = (
        pending_event_choice
        if isinstance(pending_event_choice, NiaEventChoicePending)
        else NiaEventChoicePending.from_dict(pending_event_choice)
    )
    if pending.produce_id != produce_id or pending.idol_card_id != idol_card_id:
        raise ValueError("pending event-choice run identity differs")
    if not _event_choice_authority_matches(snapshot, pending):
        raise ValueError("pending event-choice LocalSave authority differs")
    proof = _event_choice_screen_proof(image, capture, pending)
    if proof is None:
        return NiaLiveOuterSubpage(
            kind="wait",
            target="event-choice-resume-evidence-unavailable",
            action=None,
            evidence={
                "policy": "typed-event-choice-resume-requires-source-screen-proof",
                "owner": pending.owner,
                "target": pending.target,
                "adv_asset_id": pending.adv_asset_id,
                "chosen_slot": pending.selected_slot,
                "authority": dict(pending.authority),
                "master_evidence": dict(pending.master_evidence),
            },
        )
    if not bool(proof.get("matched")):
        return NiaLiveOuterSubpage(
            kind="wait",
            target="event-choice-resume-proof-unresolved",
            action=None,
            evidence={
                "policy": "typed-event-choice-resume-requires-one-selected-slot",
                "owner": pending.owner,
                "target": pending.target,
                "adv_asset_id": pending.adv_asset_id,
                "chosen_slot": pending.selected_slot,
                "authority": dict(pending.authority),
                "master_evidence": dict(pending.master_evidence),
                "screen_evidence": dict(proof),
            },
        )
    chosen_box = pending.choice_rows[pending.selected_slot - 1]
    kind = (
        "static-event-choice-confirm"
        if pending.owner == "static"
        else "outing-choice-confirm"
    )
    return NiaLiveOuterSubpage(
        kind=kind,
        target=pending.target,
        action=_action(
            capture,
            kind=kind,
            target=pending.target,
            box=chosen_box,
            click_count=1,
        ),
        evidence={
            "policy": "pending-master-event-choice-confirm",
            "resume": True,
            "owner": pending.owner,
            "required_choice_count": pending.required_choice_count,
            "adv_asset_id": pending.adv_asset_id,
            "detail_ids": list(pending.master_evidence["detail_ids"])
            if isinstance(pending.master_evidence.get("detail_ids"), list)
            else [],
            "choice_texts": list(pending.master_evidence["choice_texts"]),
            "chosen_slot": pending.selected_slot,
            "selected_suggestion_id": pending.master_evidence.get(
                "selected_suggestion_id"
            ),
            "selected_effect_ids": list(
                pending.master_evidence.get("selected_effect_ids", [])
            ),
            "selected_success_effect_ids": list(
                pending.master_evidence.get("selected_success_effect_ids", [])
            ),
            "selected_fail_effect_ids": list(
                pending.master_evidence.get("selected_fail_effect_ids", [])
            ),
            "selected_success_probability_permyriad": (
                pending.master_evidence.get(
                    "selected_success_probability_permyriad"
                )
            ),
            "selected_slot": pending.selected_slot,
            "authority": dict(pending.authority),
            "durable_evidence": {
                "authority_source": "ProduceLocalSave",
                "authority": dict(pending.authority),
            },
            "master_evidence": dict(pending.master_evidence),
            "pending_event_choice": pending.to_dict(),
            "screen_evidence": dict(proof),
        },
    )


def _static_adv_event_choice(
    screen: np.ndarray,
    capture: Mapping[str, Any],
    snapshot: ProduceOuterLocalSaveSnapshot,
    *,
    idol_card_id: str | None,
    produce_id: str | None,
) -> NiaLiveOuterSubpage | None:
    """Resolve non-outing character activity choices through Master order."""

    return _master_bound_event_choice(
        screen,
        capture,
        snapshot,
        idol_card_id=idol_card_id,
        produce_id=produce_id,
        required_choice_count=None,
        owner="static",
        click_count=1,
    )


def _outing_master_event_choice(
    screen: np.ndarray,
    capture: Mapping[str, Any],
    snapshot: ProduceOuterLocalSaveSnapshot,
    *,
    idol_card_id: str | None,
    produce_id: str | None,
    current_drink_count: int | None = None,
) -> NiaLiveOuterSubpage | None:
    """Resolve an outing's three rows through ADV/Master, if all bind."""

    if current_drink_count is None:
        snapshot_count = getattr(snapshot, "drink_count", None)
        if (
            isinstance(snapshot_count, int)
            and not isinstance(snapshot_count, bool)
            and 0 <= snapshot_count <= 3
        ):
            current_drink_count = snapshot_count
    return _master_bound_event_choice(
        screen,
        capture,
        snapshot,
        idol_card_id=idol_card_id,
        produce_id=produce_id,
        required_choice_count=3,
        owner="outing",
        click_count=2,
        current_drink_count=current_drink_count,
    )


def analyze_nia_outer_subpage(
    image: Image.Image,
    capture: Mapping[str, Any],
    snapshot: ProduceOuterLocalSaveSnapshot,
    *,
    allow_event_choice: bool = False,
    pending_weekly_action: str | None = None,
    current_drink_count: int | None = None,
    produce_id: str | None = None,
    idol_card_id: str | None = None,
    pending_event_choice: NiaEventChoicePending | Mapping[str, Any] | None = None,
    mirror_recognition: Mapping[str, Any] | None = None,
    maa_recognition: Mapping[str, Any] | None = None,
    allow_development_outing_ocr: bool = False,
) -> NiaLiveOuterSubpage:
    """Return one exact MAA click for the current N.I.A. outer subpage."""

    if not isinstance(snapshot, ProduceOuterLocalSaveSnapshot):
        raise TypeError("snapshot must be ProduceOuterLocalSaveSnapshot")
    from .live_source import _live_text_recognizer

    screen = _canonical_screen(image)

    # Controller-side Maa recognition is the production fast path.  Python's
    # original exact matcher remains as an offline/test fallback and for the
    # few policy-specific pages not yet present in this batch.
    recognized = _maa_recognition_matches(maa_recognition)

    def match(
        label: str,
        path: Path,
        *,
        roi: tuple[int, int, int, int],
        threshold: float,
    ) -> tuple[int, int, int, int, float] | None:
        if maa_recognition is not None:
            return recognized.get(label)
        return _match(screen, path, roi=roi, threshold=threshold)

    # A localized communication failure owns the foreground.  Its long
    # ``return to title`` button reuses Maa's X glyph but scores just below the
    # generic circular-close threshold.  Recognition is performed in one
    # narrow icon ROI by the controller; the click is derived only from that
    # matched box and retains the usual capture/window identity contract.
    communication_return = recognized.get("communication-return-title")
    if communication_return is not None:
        return NiaLiveOuterSubpage(
            kind="continue",
            target="communication-return-title",
            action=_action(
                capture,
                kind="continue",
                target="communication-return-title",
                box=communication_return[:4],
            ),
            evidence={
                "match_score": communication_return[4],
                "policy": "maa-localized-communication-return-title",
            },
        )

    # Maa's skip-confirm modal is foreground UI.  Let its dedicated node own
    # the one confirmation click before detectors inspect the still-visible
    # drink/reward page behind it.
    skip_confirm = recognized.get("common-skip-confirm")
    if skip_confirm is not None:
        return NiaLiveOuterSubpage(
            kind="continue",
            target="skip-confirm",
            action=_action(
                capture,
                kind="continue",
                target="skip-confirm",
                box=skip_confirm[:4],
            ),
            evidence={
                "match_score": skip_confirm[4],
                "policy": "maa-foreground-skip-confirm",
            },
        )

    # Rest confirmation is a foreground modal whose positive control has a
    # dedicated Maa asset (``rest_take.png``).  The generic ``round-close``
    # X remains visible through the dimmed background and can score higher
    # than the localized ``decide`` asset, so it must never own this frame.
    # Reuse Maa's exact ProduceTakeRest ROI.  Continue, Decide and Shop Buy
    # share part of this artwork, so an exact semantic button match outranks a
    # weaker rest-template overlap.  On production captures every candidate
    # comes from the same one-frame Maa batch; ``match`` only scans templates
    # for offline fixtures where no batch was supplied.
    rest_take = match(
        "rest-confirm",
        DEFAULT_TEMPLATE_DIR / "rest_take.png",
        roi=(360, 1100, 720, 1280),
        threshold=0.90,
    )
    rest_competitors: list[tuple[int, int, int, int, float]] = []
    for label, filename, roi in (
        ("common-continue", "continue.png", (0, 700, 720, 1280)),
        ("decide", "decide.png", (0, 980, 720, 1280)),
        ("common-decide", "decide.png", (0, 700, 720, 1280)),
        ("shop-buy", "shopping_buy.png", (0, 980, 720, 1280)),
        ("shop-exchange", "shopping_exchange.png", (0, 980, 720, 1280)),
        ("shop-point-runout", "point_runout.png", (0, 980, 720, 1280)),
    ):
        competitor = match(
            label,
            DEFAULT_TEMPLATE_DIR / filename,
            roi=roi,
            threshold=(
                0.90
                if label
                in {
                    "decide",
                    "shop-buy",
                    "shop-exchange",
                    "shop-point-runout",
                }
                else 0.92
            ),
        )
        if competitor is not None:
            rest_competitors.append(competitor)
    strongest_rest_competitor = max(
        (value[4] for value in rest_competitors),
        default=-1.0,
    )
    if rest_take is not None and (
        rest_take[4] >= 0.98 or rest_take[4] > strongest_rest_competitor
    ):
        return NiaLiveOuterSubpage(
            kind="continue",
            target="rest-confirm",
            action=_action(
                capture,
                kind="continue",
                target="rest-confirm",
                box=rest_take[:4],
            ),
            evidence={
                "match_score": rest_take[4],
                "strongest_competing_button_score": (
                    None
                    if not rest_competitors
                    else strongest_rest_competitor
                ),
                "policy": "maa-foreground-rest-confirmation-positive",
                "template": "rest_take.png",
            },
        )

    # Server-settled card mutations are foreground notifications.  Their
    # background still exposes Cancel/X artwork, so this semantic notice must
    # own the frame before Maa's generic round-close result.
    mutation_notice = _live_text_recognizer().recognize(
        image.crop((80, 960, 640, 1060))
    )
    mutation_text = re.sub(r"\s+", "", mutation_notice.text)
    customize_success_markers = (
        "已自定義",
        "已自定义",
        "カスタマイズしました",
    )
    customized_notice = any(
        marker in mutation_text
        for marker in customize_success_markers
    )
    if mutation_notice.confidence >= 0.60 and any(
        marker in mutation_text
        for marker in (
            "已刪除",
            "已删除",
            "已強化",
            "已强化",
            *customize_success_markers,
        )
    ):
        box = (45, 900, 675, 1135)
        target = (
            "card-customize-notification"
            if customized_notice
            else "card-mutation-notification"
        )
        return NiaLiveOuterSubpage(
            kind="continue",
            target=target,
            action=_action(
                capture,
                kind="continue",
                target=target,
                box=box,
            ),
            evidence={
                "policy": "ocr-dismiss-server-settled-card-mutation",
                "text": mutation_notice.text,
                "confidence": mutation_notice.confidence,
            },
        )

    # Maa can still recognize the audition/mirror row through the dimmed
    # background while the foreground ``round close`` modal is open.  The X
    # is the only actionable control on that frame: dismiss it once, then let
    # the next fresh capture re-evaluate the background page.
    round_close = recognized.get("common-round-close")
    if round_close is not None:
        return NiaLiveOuterSubpage(
            kind="continue",
            target="round-close",
            action=_action(
                capture,
                kind="continue",
                target="round-close",
                box=round_close[:4],
            ),
            evidence={
                "match_score": round_close[4],
                "policy": "maa-foreground-round-close",
            },
        )

    # Global Home is not a Produce subpage.  It nevertheless carries Maa's
    # authoritative distinction between a new Produce (``home.png``) and the
    # currently active Produce (``home_1.png``).  Surface policy binds the
    # latter to the expected run before invoking a dedicated Maa continuation
    # chain; the generic Click_1 fallback must never own either Home variant.
    active_home = recognized.get("home-active-produce")
    new_home = recognized.get("home-new-produce")
    if active_home is not None:
        return NiaLiveOuterSubpage(
            kind="active-produce-resume",
            target="same-run-home",
            action=None,
            evidence={
                "match_score": active_home[4],
                "also_matched_new_produce": new_home is not None,
                "policy": "maa-produce-continue-bound-to-expected-run-v1",
                "pipeline_entry": "ProduceContinue",
            },
        )
    if new_home is not None:
        return NiaLiveOuterSubpage(
            kind="global-home-new-produce-blocked",
            target="new-produce",
            action=None,
            evidence={
                "match_score": new_home[4],
                "policy": "maa-produce-start-is-never-a-live-resume",
                "pipeline_entry": "ProduceStart",
            },
        )

    # Modal decisions own the screen over the background page title.  A full
    # P-drink inventory explicitly selects "receive none" and therefore must
    # be handled before the still-visible fan-present header behind it.
    reject_surface = _drink_full_reject_surface(screen, capture)
    if reject_surface is not None:
        return reject_surface

    # The full-inventory sheet is foreground UI.  Its drink descriptions
    # contain several numbers in exactly the same vertical bands used by the
    # final-audition threshold OCR, while the dimmed weekly page remains
    # visible behind it.  Establish this modal owner before any outing, ADV,
    # result, or audition-row classifier.  Dedicated foreground confirmations
    # above (skip/rest/round-close) still retain higher priority.
    drink_full = match(
        "drink-full",
        DEFAULT_TEMPLATE_DIR / "drink_full_on_keep_window.png",
        roi=(0, 0, 720, 300),
        threshold=0.90,
    )
    if drink_full is not None:
        layout = _drink_keep_candidate_layout(screen)
        remaining_read = _drink_keep_remaining_ocr(screen)
        keep = match(
            "decide",
            DEFAULT_TEMPLATE_DIR / "decide.png",
            roi=(0, 980, 720, 1280),
            threshold=0.70,
        )
        evidence: dict[str, Any] = {
            "header_score": drink_full[4],
            "full_inventory": True,
            **layout,
            "keep_box": None if keep is None else list(keep[:4]),
            "button_score": None if keep is None else keep[4],
            "policy": "foreground-current-frame-n-drink-ranked-selection",
        }
        if remaining_read is None:
            evidence["complete"] = False
            evidence["reason"] = "drink-remaining-count-unresolved"
        else:
            _remaining, remaining_evidence = remaining_read
            evidence.update(remaining_evidence)
        if keep is None:
            evidence["complete"] = False
            evidence["reason"] = "drink-keep-button-unresolved"
        return NiaLiveOuterSubpage(
            kind="drink-keep",
            target="rank-visible-candidates",
            action=None,
            evidence=evidence,
        )

    # An outing follow-up can reuse the same localized row text as a named
    # character ADV event.  Once the weekly overview owner is ``outing``,
    # keep that owner until the bound LocalSave changes.  The outing owner may
    # use the catalog only after all three rows bind to ADV/Master.  Production
    # does not downgrade that owner to OCR row semantics.  Static ADV is
    # intentionally never called from this branch.
    if allow_event_choice and pending_event_choice is not None:
        pending_owner = (
            pending_event_choice.owner
            if isinstance(pending_event_choice, NiaEventChoicePending)
            else pending_event_choice.get("owner")
        )
        pending_schema = (
            _NIA_EVENT_CHOICE_PENDING_SCHEMA
            if isinstance(pending_event_choice, NiaEventChoicePending)
            else pending_event_choice.get("schema")
            if isinstance(pending_event_choice, Mapping)
            else None
        )
        if (
            pending_owner in {"static", "outing"}
            and pending_schema == _NIA_EVENT_CHOICE_PENDING_SCHEMA
        ):
            return _resume_master_event_choice(
                image,
                capture,
                snapshot,
                pending_event_choice=pending_event_choice,
                produce_id=produce_id,
                idol_card_id=idol_card_id,
            )

    if pending_weekly_action == OUTING:
        outing_master_choice = _outing_master_event_choice(
            screen,
            capture,
            snapshot,
            idol_card_id=idol_card_id,
            produce_id=produce_id,
            current_drink_count=current_drink_count,
        )
        if outing_master_choice is not None:
            return outing_master_choice
        # A proved option layout owns the foreground even if a newly-localised
        # row cannot yet bind uniquely to Master.  The same page intentionally
        # displays Maa's fast-forward control, so fail closed here instead of
        # allowing the common skip detector to click it repeatedly.
        outing_page = _outing_ocr_choice(screen, capture, snapshot)
        if outing_page is not None:
            return NiaLiveOuterSubpage(
                kind="wait",
                target="outing-choice-master-identity-unresolved",
                action=None,
                evidence={
                    "policy": "outing-option-page-blocks-dialogue-skip",
                    "option_page": dict(outing_page.evidence),
                },
            )
    else:
        static_event_choice = _static_adv_event_choice(
            screen,
            capture,
            snapshot,
            idol_card_id=idol_card_id,
            produce_id=produce_id,
        )
        if static_event_choice is not None:
            return static_event_choice

    # Item-effect banners are informational and have no choice.  Identify the
    # banner from its localized full title; do not infer it from artwork,
    # colour, animation, or a fixed item slot.  Produce LocalSave remains the
    # before/after state authority in the caller.
    from .initial_regular_autopilot import _match_nia_produce_item

    # Foreground confirmation modals own the screen before any background
    # overview nodes.  OCR the modal title and its affirmative button; one Maa
    # click submits it and Produce LocalSave confirms the recovered stamina.
    # Initial and N.I.A. place the same rest-confirm modal at different
    # vertical offsets.  Read the two known title bands and select by the
    # localized title itself; the pending outer transaction remains the
    # reason this modal is actionable.
    rest_title_reads = tuple(
        _live_text_recognizer().recognize(image.crop(box))
        for box in ((20, 620, 700, 730), (20, 730, 700, 850))
    )
    rest_title = max(
        rest_title_reads,
        key=lambda value: (
            "休息確認" in re.sub(r"\s+", "", value.text),
            value.confidence,
        ),
    )
    rest_button = _live_text_recognizer().recognize(
        image.crop((350, 1100, 650, 1220))
    )
    rest_title_text = re.sub(r"\s+", "", rest_title.text)
    rest_button_text = re.sub(r"\s+", "", rest_button.text)
    if (
        rest_title.confidence >= 0.60
        and "休息確認" in rest_title_text
        and rest_button.confidence >= 0.60
        and "休息" in rest_button_text
    ):
        box = (365, 1100, 650, 1215)
        return NiaLiveOuterSubpage(
            kind="continue",
            target="rest-confirm",
            action=_action(
                capture,
                kind="continue",
                target="rest-confirm",
                box=box,
            ),
            evidence={
                "policy": "ocr-foreground-rest-confirmation",
                "title": rest_title.text,
                "button": rest_button.text,
            },
        )

    # Audition briefing has one semantic action printed at the bottom.  The
    # displayed percentages are informational and are not prediction gates.
    audition_next = _live_text_recognizer().recognize(
        image.crop((160, 1160, 560, 1240))
    )
    audition_next_text = re.sub(r"\s+", "", audition_next.text)
    if audition_next.confidence >= 0.60 and any(
        marker in audition_next_text
        for marker in ("前往下一步", "前往下ー步", "次へ")
    ):
        box = (120, 1080, 600, 1260)
        return NiaLiveOuterSubpage(
            kind="continue",
            target="audition-briefing-next",
            action=_action(
                capture,
                kind="continue",
                target="audition-briefing-next",
                box=box,
            ),
            evidence={
                "policy": "ocr-audition-briefing-next-only",
                "text": audition_next.text,
                "confidence": audition_next.confidence,
            },
        )

    # The three audition rows print their vote requirements directly.  Maa's
    # old row templates cover the unselected colours but not the highlighted
    # orange row, so OCR the same visible thresholds and use LocalSave votes.
    audition_rows = (
        ((20, 725, 700, 855), (240, 790, 490, 850)),
        ((20, 845, 700, 970), (240, 905, 490, 965)),
        ((20, 965, 700, 1085), (240, 1025, 490, 1085)),
    )
    visible_auditions: list[dict[str, Any]] = []
    for index, (row_box, threshold_box) in enumerate(audition_rows):
        threshold_read = _live_text_recognizer().recognize(image.crop(threshold_box))
        compact_threshold = re.sub(r"\s+", "", threshold_read.text)
        digits = re.findall(r"\d+", compact_threshold)
        unconditional = any(
            value in compact_threshold for value in ("無條件", "无条件")
        )
        if threshold_read.confidence < 0.60 or (not digits and not unconditional):
            continue
        visible_auditions.append(
            {
                "index": index,
                "threshold": int("".join(digits)) if digits else 0,
                "box": row_box,
                "text": threshold_read.text,
            }
        )
    if len(visible_auditions) == 3 and any(
        row["threshold"] > 0 for row in visible_auditions
    ):
        vote_count = max(0, snapshot.vote_count or 0)
        affordable = tuple(
            row for row in visible_auditions if row["threshold"] <= vote_count
        )
        visible_stage = None
        for label, candidate_stage in (
            ("mirror-mid1", MID1),
            ("mirror-mid2", MID2),
            ("mirror-final", FINAL),
            ("mirror-final-high", FINAL),
        ):
            if label in recognized:
                visible_stage = candidate_stage
                break
        if visible_stage is None and maa_recognition is None:
            marker = _mirror_marker(screen)
            if marker is not None:
                visible_stage = marker[0]
        difficulty_advice = _audition_difficulty_advice(
            snapshot,
            produce_id=produce_id,
            idol_card_id=idol_card_id,
            screen_stage=visible_stage,
        )
        advised_threshold = (
            difficulty_advice.get("selected_threshold")
            if isinstance(difficulty_advice, Mapping)
            else 0
        )
        if isinstance(difficulty_advice, Mapping):
            advised_stage = difficulty_advice.get("stage")
            if advised_stage in (MID1, MID2, FINAL):
                visible_stage = advised_stage
        eligible = tuple(
            row
            for row in affordable
            if isinstance(advised_threshold, int)
            and row["threshold"] <= advised_threshold
        )
        # A missing forecast/identity is never permission to select a harder
        # row.  The always-available zero-vote row remains the safe fallback.
        selected = max(
            eligible or (min(affordable, key=lambda row: row["threshold"]),),
            key=lambda row: (row["threshold"], -row["index"]),
        )
        box = selected["box"]
        return NiaLiveOuterSubpage(
            kind="mirror-choice",
            target=f"vote-threshold:{selected['threshold']}",
            action=_fixed_action(
                capture,
                kind="mirror-choice",
                target=f"vote-threshold:{selected['threshold']}",
                point=(360, (box[1] + box[3]) // 2),
                verification_box=box,
                click_count=2,
            ),
            evidence={
                "policy": "ocr-visible-vote-threshold-strength-forecast",
                "stage": visible_stage,
                "vote_count": snapshot.vote_count,
                "selected_threshold": selected["threshold"],
                "difficulty_advice": difficulty_advice,
                "visible": [
                    {
                        "row": row["index"],
                        "threshold": row["threshold"],
                        "text": row["text"],
                    }
                    for row in visible_auditions
                ],
            },
        )

    # Self-lesson result pages are informational: the server has already
    # settled the attribute gain in Produce LocalSave.  OCR only the visible
    # result sentence and dismiss it; the numeric gain is deliberately not
    # parsed or compared with a prediction.
    lesson_result = _live_text_recognizer().recognize(
        image.crop((170, 1000, 575, 1090))
    )
    lesson_result_text = re.sub(r"\s+", "", lesson_result.text)
    if drink_full is None and lesson_result.confidence >= 0.70 and any(
        marker in lesson_result_text
        for marker in ("上升了", "上升", "上昇した", "アップ")
    ):
        box = (45, 930, 675, 1135)
        return NiaLiveOuterSubpage(
            kind="continue",
            target="self-lesson-result",
            action=_action(
                capture,
                kind="continue",
                target="self-lesson-result",
                box=box,
            ),
            evidence={
                "policy": "ocr-dismiss-server-settled-self-lesson-result",
                "text": lesson_result.text,
                "confidence": lesson_result.confidence,
            },
        )

    # Fan-present card acquisition is a settled informational overlay.  It
    # has no card title yet and must advance exactly once before the next
    # fresh capture is allowed to enter the three-card reward reader.  Reuse
    # the shared OCR/Maa typed detector so every Plan uses the same semantic
    # gate and tap geometry.
    from .live_source import detect_card_acquire_notification

    card_acquire = detect_card_acquire_notification(
        Image.fromarray(screen.astype("uint8")),
        maa_nodes=recognized,
    )
    if card_acquire is not None:
        return NiaLiveOuterSubpage(
            kind="continue",
            # Keep the long-standing fan-present action target for N.I.A.
            # callers; the typed notification evidence below distinguishes
            # this animation from the ordinary parcel-open frame without
            # breaking existing route telemetry.
            target="fan-present-open",
            action=_action(
                capture,
                kind="continue",
                target="fan-present-open",
                box=card_acquire.action_box,
            ),
            evidence={
                "policy": "typed-card-acquire-notification",
                "notification_type": "card-acquire-notification",
                **card_acquire.to_dict(),
            },
        )

    # A single enlarged card with its description is a dismissible detail
    # page.  This also makes recovery restart-safe: no in-memory knowledge of
    # which reward preceded the page is required.  Identity, artwork, title,
    # rarity and upgrade-marker colours are intentionally ignored.
    from .live_source import _card_detector

    capture_path = capture.get("png_path")
    if isinstance(capture_path, str) and capture_path:
        detected_cards = tuple(
            item
            for item in _card_detector().detect_path(Path(capture_path)).detections
            if item.label == "cards"
        )
        choice_tiles = tuple(
            item
            for item in detected_cards
            if 130 <= item.x <= 500
            and 780 <= item.y <= 900
            and 100 <= item.width <= 145
            and 100 <= item.height <= 145
        )
        enlarged_cards = tuple(
            item
            for item in detected_cards
            if 180 <= item.x <= 520
            and 430 <= item.y <= 900
            and item.width >= 180
            and item.height >= 230
            # The dialogue continuation begins below this boundary.  The
            # generic detector can report the lower text/button panels as two
            # additional large ``cards`` boxes; neither is card artwork.
            and item.y + item.height <= 950
        )
        # The generic detector can merge a choice tile with the right-side UI
        # and report one false enlarged card.  Three real square controls own
        # the frame as a reward chooser, so never let that merged detection
        # turn the chooser into a dismissible detail page.
        if len(choice_tiles) != 3 and len(enlarged_cards) == 1:
            # The enlarged artwork itself is not a continuation target.  The
            # game advances these informational overlays from the dialogue
            # panel below the card; bind the generic detail-page action there
            # so every card/reward variant shares the same Maa interaction.
            box = (45, 950, 675, 1125)
            return NiaLiveOuterSubpage(
                kind="continue",
                target="single-card-detail",
                action=_action(
                    capture,
                    kind="continue",
                    target="single-card-detail",
                    box=box,
                ),
                evidence={
                    "policy": "single-enlarged-card-layout-dialogue-continue"
                },
            )

    notification_title = _live_text_recognizer().recognize(
        image.crop((160, 830, 570, 900))
    )
    notification_item = (
        _match_nia_produce_item(notification_title.text)
        if notification_title.confidence >= 0.60
        else None
    )
    if notification_item is not None:
        box = (70, 820, 650, 1070)
        return NiaLiveOuterSubpage(
            kind="continue",
            target="item-effect-notification",
            action=_action(
                capture,
                kind="continue",
                target="item-effect-notification",
                box=box,
            ),
            evidence={
                "policy": "ocr-localized-produce-item-title",
                "item_id": notification_item.item_id,
                "title": notification_title.text,
                "confidence": notification_title.confidence,
            },
        )

    # Server-settled card-upgrade notices are informational.  LocalSave owns
    # which card changed; the visible sentence only identifies the dismissible
    # page, so no card artwork/title matching is needed here.
    upgraded_notice = _live_text_recognizer().recognize(
        image.crop((80, 940, 620, 1035))
    )
    upgraded_text = re.sub(r"\s+", "", upgraded_notice.text)
    if upgraded_notice.confidence >= 0.70 and (
        "已強化" in upgraded_text or "已强化" in upgraded_text
    ):
        box = (45, 900, 675, 1135)
        return NiaLiveOuterSubpage(
            kind="continue",
            target="card-upgrade-notification",
            action=_action(
                capture,
                kind="continue",
                target="card-upgrade-notification",
                box=box,
            ),
            evidence={
                "policy": "ocr-dismiss-server-settled-card-upgrade",
                "text": upgraded_notice.text,
                "confidence": upgraded_notice.confidence,
            },
        )

    # Fan-present opening has no choice or textual button.  Its small header is
    # stable localized UI text, while the glowing parcel itself is the only
    # interaction.  OCR the header first so an arbitrary animation can never
    # inherit this click.
    present_title = _live_text_recognizer().recognize(
        image.crop((65, 35, 160, 72))
    )
    # Reward modals leave the small background header visible.  Their receive
    # control is enough to give the modal precedence; card artwork is not part
    # of this decision.
    modal_button = _live_text_recognizer().recognize(
        image.crop((80, 1000, 650, 1130))
    )
    modal_button_text = re.sub(r"\s+", "", modal_button.text)
    modal_receive = any(
        value in modal_button_text for value in ("領取", "领取")
    )
    if (
        present_title.confidence >= 0.75
        and re.sub(r"\s+", "", present_title.text) in {"慰問品", "慰问品"}
        and not modal_receive
    ):
        box = (250, 610, 520, 1080)
        return NiaLiveOuterSubpage(
            kind="continue",
            target="fan-present-open",
            action=_action(
                capture,
                kind="continue",
                target="fan-present-open",
                box=box,
            ),
            evidence={
                "policy": "ocr-fan-present-header",
                "title": present_title.text,
                "confidence": present_title.confidence,
            },
        )

    # Card management must own the page before work/event/common-button
    # recognizers inspect the card artwork behind it.  Identity and choice are
    # handled by the caller's title-OCR workflow; this classification itself
    # returns no click.
    for target, filename in _CARD_OPERATION_ICONS:
        marker = (
            recognized.get(f"card-icon-{target}")
            if maa_recognition is not None
            else _match(
                screen,
                DEFAULT_TEMPLATE_DIR / filename,
                roi=(0, 20, 220, 100),
                threshold=0.90,
            )
        )
        if marker is not None:
            return NiaLiveOuterSubpage(
                kind="card-operation-page",
                target=target,
                action=None,
                evidence={
                    "match_score": marker[4],
                    "policy": "ocr-full-title-card-operation",
                },
            )

    # A custom/localized skin may omit the icon match even though the same
    # operation page header is visible.  Read only the small header band that
    # owns the icon above, and require one of the four exact localized
    # operation labels.  The OCR line can contain a stray mixed-language glyph
    # (for example ``与變换``), so matching is substring-based after the
    # operation-specific variant normalization.  This owner is intentionally
    # before event/reward readers; it returns no click and therefore leaves the
    # existing `_nia_card_operation_surface` title-OCR ranking in charge.
    operation_header = _live_text_recognizer().recognize(
        image.crop((0, 20, 220, 100))
    )
    operation_header_text = re.sub(r"\s+", "", operation_header.text)
    normalized_header = operation_header_text.translate(
        _CARD_OPERATION_HEADER_TRANSLATION
    )
    if operation_header.confidence >= 0.60:
        for target, titles in _CARD_OPERATION_HEADER_TITLES.items():
            normalized_titles = tuple(
                title.translate(_CARD_OPERATION_HEADER_TRANSLATION)
                for title in titles
            )
            matched_title = next(
                (
                    title
                    for title, normalized_title in zip(titles, normalized_titles)
                    if normalized_title in normalized_header
                ),
                None,
            )
            if matched_title is None:
                continue
            return NiaLiveOuterSubpage(
                kind="card-operation-page",
                target=target,
                action=None,
                evidence={
                    "match_score": operation_header.confidence,
                    "header_text": operation_header.text,
                    "header_confidence": operation_header.confidence,
                    "header_title": matched_title,
                    "policy": "ocr-semantic-card-operation-header-fallback",
                },
            )

    # The Special Guidance card grid has no ordinary ``Customize`` header:
    # the top-left label remains Special Guidance and the official selection
    # instruction sits in the centre panel.  When a translation overlay makes
    # Maa's Japanese template unavailable, route that same instruction into
    # the already shared card-operation session.  Preview, ranking, selection,
    # and submission continue to have exactly one implementation in the
    # caller; this branch only restores the missing page owner.
    guide_prompt = (
        _live_text_recognizer().recognize(image.crop((120, 320, 610, 450)))
        if pending_weekly_action == SPECIAL_GUIDANCE
        else None
    )
    if guide_prompt is not None and guide_prompt.confidence >= 0.60:
        guide_prompt_text = re.sub(r"\s+", "", guide_prompt.text)
        matched_prompt = next(
            (
                prompt
                for prompt in _GUIDE_CUSTOMIZE_PROMPTS
                if prompt in guide_prompt_text
            ),
            None,
        )
        if matched_prompt is not None:
            return NiaLiveOuterSubpage(
                kind="card-operation-page",
                target="customize",
                action=None,
                evidence={
                    "match_score": guide_prompt.confidence,
                    "prompt_text": guide_prompt.text,
                    "prompt_title": matched_prompt,
                    "policy": "ocr-guide-shared-card-customization",
                },
            )

    # A committed generic weekly action can keep its overview tile visible
    # behind the dialogue overlay.  Maa's exact skip-chat button is the typed
    # successor in that frame; classify it before the background event marker.
    # OUTING/REST retain their dedicated option/modal precedence above.
    if pending_weekly_action in {
        ACTIVITY,
        BUSINESS,
        CONSULTATION,
        SPECIAL_GUIDANCE,
        VOCAL_LESSON,
        DANCE_LESSON,
        VISUAL_LESSON,
    }:
        dialogue_matches = tuple(
            value
            for value in (
                recognized.get("common-skip-chat"),
                recognized.get("common-skip-chat-alt"),
            )
            if value is not None
        )
        if dialogue_matches:
            strongest = max(dialogue_matches, key=lambda value: value[4])
            return NiaLiveOuterSubpage(
                kind="continue",
                target="skip-chat",
                action=_action(
                    capture,
                    kind="continue",
                    target="skip-chat",
                    box=strongest[:4],
                ),
                evidence={
                    "policy": "maa-typed-dialogue-before-background-event-marker",
                    "match_score": strongest[4],
                },
            )

    if allow_event_choice:
        event_choice = _event_choice(
            screen,
            capture,
            snapshot,
            recognized=(recognized if maa_recognition is not None else None),
            produce_id=produce_id,
            idol_card_id=idol_card_id,
            pending_event_choice=pending_event_choice,
        )
        if event_choice is not None:
            return event_choice

    # Localify changes the icon template but preserves the prompt and row
    # text.  Use those visible semantics directly instead of falling through
    # to Maa's story click when the work-page bitmap does not match.
    if maa_recognition is not None and _work_business_prompt_visible(screen):
        target, box, evidence = _work_business_ocr_choice(screen, snapshot)
        return NiaLiveOuterSubpage(
            kind="work-choice",
            target=target,
            action=_action(
                capture,
                kind="work-choice",
                target=target,
                box=box,
            ),
            evidence=evidence,
        )

    work = _work_choice(
        screen,
        snapshot,
        recognized=(recognized if maa_recognition is not None else None),
    )
    if work is not None:
        target, box, evidence = work
        return NiaLiveOuterSubpage(
            kind="work-choice",
            target=target,
            action=_action(
                capture,
                kind="work-choice",
                target=target,
                box=box,
                # Maa's business-object row is a single selection; its
                # subsequent Vo/Da/Vi dialogue uses ProduceChooseOptionsAuto,
                # whose established commit gesture is a double click.
                click_count=(2 if target in {"Vo", "Da", "Vi"} else 1),
            ),
            evidence=evidence,
        )

    work_start = match(
        "work-start",
        NIA_TEMPLATE_DIR / _WORK_START_TEMPLATE,
        roi=(0, 850, 720, 1280),
        threshold=0.97,
    )
    if work_start is not None:
        try:
            start_authority = dict(
                _work_start_cost_authority(screen, work_start, snapshot)
            )
        except ValueError as error:
            return NiaLiveOuterSubpage(
                kind="wait",
                target="work-start-cost-unresolved",
                action=None,
                evidence={
                    "match_score": work_start[4],
                    "policy": "unresolved-work-start-is-zero-input",
                    "stamina": snapshot.stamina,
                    "reason": str(error),
                },
            )
        if not bool(start_authority["affordable"]):
            return NiaLiveOuterSubpage(
                kind="wait",
                target="work-start-disabled",
                action=None,
                evidence={
                    "match_score": work_start[4],
                    "policy": "unaffordable-work-start-is-zero-input",
                    **start_authority,
                },
            )
        box = work_start[:4]
        return NiaLiveOuterSubpage(
            kind="work-start",
            target="start",
            action=_action(
                capture,
                kind="work-start",
                target="start",
                box=box,
            ),
            evidence={"match_score": work_start[4], **start_authority},
        )

    # A production outing transaction is Master-bound.  The legacy OCR policy
    # remains directly actionable only for non-outing/development callers; an
    # unresolved exact outing page was already converted to a typed wait above.
    if pending_weekly_action != OUTING or allow_development_outing_ocr:
        outing_choice = _outing_ocr_choice(screen, capture, snapshot)
        if outing_choice is not None:
            return outing_choice

    # Reward pages must be routed before generic Decide/common buttons.  Their
    # disabled and enabled receive buttons share most of the same artwork, so a
    # template score alone cannot say whether an option has been selected.
    # Japanese Maa installations use ProduceChooseGetFlag; Localify's Chinese
    # prompt is classified with the same bundled OCR model instead.
    reward_confirm = match(
        "common-cards-get",
        DEFAULT_TEMPLATE_DIR / "cards_get.png",
        roi=(0, 700, 720, 1280),
        threshold=0.92,
    )
    if reward_confirm is not None:
        drink_state = _drink_reward_ocr_state(screen)
        if drink_state is not None:
            state, drink_evidence = drink_state
            if state == "unselected":
                recommendation = match(
                    "recommend",
                    DEFAULT_TEMPLATE_DIR / "recommend.png",
                    roi=(40, 450, 680, 1110),
                    threshold=0.90,
                )
                if recommendation is not None:
                    point = (
                        (recommendation[0] + recommendation[2]) // 2,
                        max(0, (recommendation[1] + recommendation[3]) // 2 - 80),
                    )
                    return NiaLiveOuterSubpage(
                        kind="reward-choice",
                        target="recommended",
                        action=_fixed_action(
                            capture,
                            kind="reward-choice",
                            target="recommended",
                            point=point,
                            verification_box=recommendation[:4],
                        ),
                        evidence={
                            **drink_evidence,
                            "selection_policy": "maa-recommend-marker",
                            "recommendation_score": recommendation[4],
                        },
                    )
                return NiaLiveOuterSubpage(
                    kind="reward-choice",
                    target="first-visible",
                    action=_fixed_action(
                        capture,
                        kind="reward-choice",
                        target="first-visible",
                        point=(222, 886),
                        verification_box=(160, 824, 284, 948),
                    ),
                    evidence={
                        **drink_evidence,
                        "selection_policy": "maa-original-first-visible-fallback",
                    },
                )
            return NiaLiveOuterSubpage(
                kind="reward-confirm",
                target="cards-get",
                action=_action(
                    capture,
                    kind="reward-confirm",
                    target="cards-get",
                    box=reward_confirm[:4],
                ),
                evidence={
                    **drink_evidence,
                    "match_score": reward_confirm[4],
                    "selection_policy": "ocr-selected-then-maa-confirm",
                },
            )

    # Consultation shop: mirror Maa's bounded policy exactly.  Buy at most the
    # visible Sale item, then follow the ordinary buy/exchange confirmation.
    # When no Sale is visible (or the drink inventory is full), the existing
    # shop-exit button below remains the completion path.
    shop_button_matches: list[
        tuple[str, str, tuple[int, int, int, int, float]]
    ] = []
    for kind, target, filename in (
        ("shop-buy", "buy", "shopping_buy.png"),
        ("shop-confirm", "exchange", "shopping_exchange.png"),
        ("shop-confirm", "point-runout", "point_runout.png"),
    ):
        button = match(
            {
                "shopping_buy.png": "shop-buy",
                "shopping_exchange.png": "shop-exchange",
                "point_runout.png": "shop-point-runout",
            }[filename],
            DEFAULT_TEMPLATE_DIR / filename,
            roi=(0, 980, 720, 1280),
            threshold=0.90,
        )
        if button is not None:
            shop_button_matches.append((kind, target, button))

    # Maa's Decide and shopping buttons share a substantial visual base.  Do
    # not let the first merely-above-threshold shop template steal an exact
    # Decide match (or vice versa); the strongest original Maa template owns
    # the one click.  This is surface routing only, not an extra action gate.
    decide = match(
        "decide",
        DEFAULT_TEMPLATE_DIR / "decide.png",
        roi=(0, 980, 720, 1280),
        threshold=0.90,
    )
    completed = snapshot.completed_steps[-1] if snapshot.completed_steps else None
    audition_failed = bool(
        completed is not None
        and completed.step_type in {16, 17, 18}
        and any(
            line.line_type_name == "audition_failure"
            for line in completed.lines
        )
    )
    next_button = (
        recognized.get("common-next") if maa_recognition is not None else None
    )
    if (
        audition_failed
        and next_button is not None
        and (decide is None or next_button[4] > decide[4])
    ):
        return NiaLiveOuterSubpage(
            kind="audition-failure-end",
            target="next",
            action=_action(
                capture,
                kind="audition-failure-end",
                target="next",
                box=next_button[:4],
            ),
            evidence={
                "match_score": next_button[4],
                "decide_match_score": None if decide is None else decide[4],
                "audition_failed": True,
                "audition_step_type": completed.step_type,
                "failure_authority": "produce-local-save-completed-audition-failure",
                "policy": "maa-nia-failed-next-ends-produce",
            },
        )
    strongest_shop = (
        max(shop_button_matches, key=lambda item: item[2][4])
        if shop_button_matches
        else None
    )

    # A three-card reward transaction owns this frame before the orange
    # Receive button is compared with generic Decide/Click_1 templates.  The
    # final reversible preview legitimately leaves one tile selected; that is
    # transaction state, not permission to collect it without ranking.
    try:
        from .initial_regular_autopilot import InitialRegularLiveSurfaceReader

        card_reward_layout = InitialRegularLiveSurfaceReader._nia_card_reward_layout(
            capture
        )
    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError):
        card_reward_layout = False
    if card_reward_layout:
        return NiaLiveOuterSubpage(
            kind="reward-choice",
            target="cards-get",
            action=None,
            evidence={
                "policy": "ocr-three-card-reward-modal-before-decide-v1",
                "preselected_tile_allowed": True,
                "selection_owner": "InitialRegularLiveSurfaceReader",
            },
        )

    if decide is not None and (
        strongest_shop is None or decide[4] > strongest_shop[2][4]
    ):
        return NiaLiveOuterSubpage(
            kind="continue",
            target="decide",
            action=_action(
                capture,
                kind="continue",
                target="decide",
                box=decide[:4],
            ),
            evidence={
                "match_score": decide[4],
                "policy": "maa-strongest-overlapping-button",
            },
        )
    if strongest_shop is not None:
        kind, target, button = strongest_shop
        return NiaLiveOuterSubpage(
            kind=kind,
            target=target,
            action=_action(
                capture,
                kind=kind,
                target=target,
                box=button[:4],
            ),
            evidence={
                "match_score": button[4],
                "policy": "maa-sale-only-shop",
            },
        )
    shop = match(
        "shop-page",
        DEFAULT_TEMPLATE_DIR / "shopping.png",
        roi=(30, 150, 700, 470),
        threshold=0.90,
    )
    if shop is not None:
        sale = match(
            "shop-sale",
            DEFAULT_TEMPLATE_DIR / "Sale.png",
            roi=(40, 460, 680, 1030),
            threshold=0.90,
        )
        if sale is not None:
            point = (
                (sale[0] + sale[2]) // 2,
                max(0, (sale[1] + sale[3]) // 2 - 65),
            )
            return _fixed_surface(
                capture,
                kind="shop-sale-select",
                target="sale",
                point=point,
                marker=sale,
                policy="maa-sale-only-shop",
            )
        exit_match = match(
            "shop-exit",
            DEFAULT_TEMPLATE_DIR / "shop_exit.png",
            roi=(0, 780, 720, 1280),
            threshold=0.90,
        )
        if exit_match is not None:
            return NiaLiveOuterSubpage(
                kind="shop-exit",
                target="exit",
                action=_action(
                    capture,
                    kind="shop-exit",
                    target="exit",
                    box=exit_match[:4],
                ),
                evidence={
                    "shop_score": shop[4],
                    "exit_score": exit_match[4],
                    "policy": "maa-sale-only-shop",
                },
            )

    guide = match(
        "guide-page",
        NIA_TEMPLATE_DIR / _GUIDE_PAGE_TEMPLATE,
        roi=(20, 150, 700, 650),
        threshold=0.90,
    )
    if guide is not None:
        # ``guide_choose`` is already the shared skill-card customization
        # grid.  The old route treated its Exit button as successful guidance,
        # producing zero card mutations at every fixed guidance week.  Hand
        # ownership and ranking live in the caller's existing card-operation
        # session, so this classifier returns no click and lets that one
        # transaction preview, select, and submit a card.
        return NiaLiveOuterSubpage(
            kind="card-operation-page",
            target="customize",
            action=None,
            evidence={
                "guide_score": guide[4],
                "policy": "guide-shared-card-customization",
            },
        )

    # Card-management pages expose a stable operation icon.  This router only
    # classifies the page: card order is mutable, so the live surface reader
    # previews every selectable tile and resolves its full localized title by
    # OCR before it returns any card click.  In particular, do not let the
    # enabled/disabled operation buttons' nearly identical artwork select a
    # fixed first tile.
    # A legacy/custom skin may expose the operation button without the header
    # icon.  Classify it, but still return no input: callers must establish a
    # title-backed target first.
    for target, filename in _CARD_OPERATION_BUTTONS:
        button = (
            recognized.get(f"card-submit-{target}")
            if maa_recognition is not None
            else _match(
                screen,
                DEFAULT_TEMPLATE_DIR / filename,
                roi=(0, 650, 720, 1280),
                threshold=0.96,
            )
        )
        if button is not None:
            return NiaLiveOuterSubpage(
                kind="card-operation-page",
                target=target,
                action=None,
                evidence={
                    "match_score": button[4],
                    "policy": "ocr-full-title-card-operation",
                },
            )

    # Reward-choice pages prefer Maa's recommendation marker.  The marker is
    # below/above the corresponding row, matching Maa's documented offsets.
    recommendation = match(
        "recommend",
        DEFAULT_TEMPLATE_DIR / "recommend.png",
        roi=(40, 450, 680, 1110),
        threshold=0.90,
    )
    if recommendation is not None:
        point = (
            (recommendation[0] + recommendation[2]) // 2,
            max(0, (recommendation[1] + recommendation[3]) // 2 - 80),
        )
        return _fixed_surface(
            capture,
            kind="reward-choice",
            target="recommended",
            point=point,
            marker=recommendation,
            policy="maa-recommend-marker",
        )
    event_recommendation = match(
        "event-recommend",
        DEFAULT_TEMPLATE_DIR / "event_recommend.png",
        roi=(40, 450, 680, 1110),
        threshold=0.90,
    )
    if event_recommendation is not None:
        point = (
            (event_recommendation[0] + event_recommendation[2]) // 2,
            min(1279, (event_recommendation[1] + event_recommendation[3]) // 2 + 80),
        )
        return _fixed_surface(
            capture,
            kind="reward-choice",
            target="event-recommended",
            point=point,
            marker=event_recommendation,
            policy="maa-event-recommend-marker",
        )
    if maa_recognition is not None and "choice-page" in recognized:
        marker = recognized["choice-page"]
        return _fixed_surface(
            capture,
            kind="reward-choice",
            target="first",
            point=(222, 886),
            marker=marker,
            policy="maa-original-choice-page-first-fallback",
        )
    for filename in _CHOICE_PAGE_TEMPLATES:
        marker = (
            recognized.get(
                {
                    "choose_cards.png": "choice-cards",
                    "choose_drink.png": "choice-drink",
                    "choose_item.png": "choice-item",
                    "choose_move_cards.png": "choice-move-cards",
                }[filename]
            )
            if maa_recognition is not None
            else _match(
                screen,
                DEFAULT_TEMPLATE_DIR / filename,
                roi=(20, 430, 700, 800),
                threshold=0.90,
            )
        )
        if marker is not None:
            return _fixed_surface(
                capture,
                kind="reward-choice",
                target="first",
                point=(222, 886),
                marker=marker,
                policy="maa-first-choice-fallback",
            )

    # Mirror rows are located by Maa OCR, but the state decision is not OCR:
    # LocalSave owns the current vote count and Master owns the legal thresholds.
    # Missing OCR rows simply degrade to Maa's always-available zero-vote row.
    mirror = None
    if maa_recognition is not None:
        for label, stage in (
            ("mirror-mid1", MID1),
            ("mirror-mid2", MID2),
            ("mirror-final", FINAL),
        ):
            if label in recognized:
                mirror = (stage, recognized[label])
                break
    else:
        mirror = _mirror_marker(screen)
    if mirror is not None:
        stage, marker = mirror
        expected_thresholds = (0,)
        if produce_id is not None and idol_card_id is not None:
            expected_thresholds = _mirror_master_thresholds(
                produce_id,
                idol_card_id,
                stage,
            )
        difficulty_advice = _audition_difficulty_advice(
            snapshot,
            produce_id=produce_id,
            idol_card_id=idol_card_id,
            screen_stage=stage,
        )
        advised_threshold = (
            difficulty_advice.get("selected_threshold")
            if isinstance(difficulty_advice, Mapping)
            else None
        )
        threshold, point, evidence = _mirror_choice(
            vote_count=snapshot.vote_count,
            expected_thresholds=expected_thresholds,
            recognition=mirror_recognition,
            stage=stage,
            recommended_threshold=(
                advised_threshold if isinstance(advised_threshold, int) else None
            ),
            recommendation_evidence=difficulty_advice,
        )
        return NiaLiveOuterSubpage(
            kind="mirror-choice",
            target=f"vote-threshold:{threshold}",
            action=_fixed_action(
                capture,
                kind="mirror-choice",
                target=f"vote-threshold:{threshold}",
                point=point,
                verification_box=marker[:4],
                click_count=2,
            ),
            evidence={"match_score": marker[4], "stage": stage, **evidence},
        )

    # Maa's last-challenge row is a marker; its click target is offset to the
    # selectable card at the left.  Challenge and final Decide use ordinary
    # common-button handling below.
    last_challenge = match(
        "last-challenge",
        DEFAULT_TEMPLATE_DIR / "last_challenge.png",
        roi=(450, 320, 720, 930),
        threshold=0.90,
    )
    if last_challenge is not None:
        point = (
            max(0, (last_challenge[0] + last_challenge[2]) // 2 - 236),
            min(1279, (last_challenge[1] + last_challenge[3]) // 2 + 130),
        )
        return _fixed_surface(
            capture,
            kind="audition-retry",
            target="last-challenge",
            point=point,
            marker=last_challenge,
            policy="maa-last-challenge",
        )
    matched_buttons: list[tuple[str, tuple[int, int, int, int, float]]] = []
    for name, path in _COMMON_BUTTONS:
        common_match = (
            recognized.get(f"common-{name}")
            if maa_recognition is not None
            else _match(
                screen,
                path,
                roi=(0, 700, 720, 1280),
                threshold=0.92,
            )
        )
        if common_match is not None:
            matched_buttons.append((name, common_match))
    if not matched_buttons:
        # The original Maa graph treats the failed-audition banner as a
        # DoNothing parent and waits for Challenge to become actionable.  This
        # check deliberately follows Challenge/last-challenge/Decide so a
        # persistent banner never masks a now-clickable retry control.
        exam_failed = match(
            "exam-failed",
            DEFAULT_TEMPLATE_DIR / "exam_failed.png",
            roi=(0, 700, 720, 950),
            threshold=0.90,
        )
        if exam_failed is None:
            raise ValueError("screen is not a supported N.I.A. outer subpage")
        return _fixed_surface(
            capture,
            kind="wait",
            target="audition-failed-controls",
            point=(20, 20),
            marker=exam_failed,
            policy="maa-produce-nia-failed-donothing",
        )
    # Multiple variants may overlap one physical confirmation button.  The
    # strongest Maa template owns the single click.
    target, strongest_match = max(matched_buttons, key=lambda item: item[1][4])
    box = strongest_match[:4]
    if target == "next" and audition_failed:
        return NiaLiveOuterSubpage(
            kind="audition-failure-end",
            target=target,
            action=_action(
                capture,
                kind="audition-failure-end",
                target=target,
                box=box,
            ),
            evidence={
                "match_score": strongest_match[4],
                "matched_buttons": [name for name, _value in matched_buttons],
                "audition_failed": True,
                "audition_step_type": completed.step_type,
                "failure_authority": "produce-local-save-completed-audition-failure",
                "policy": "maa-nia-failed-next-ends-produce",
            },
        )
    return NiaLiveOuterSubpage(
        kind="continue",
        target=target,
        action=_action(
            capture,
            kind="continue",
            target=target,
            box=box,
        ),
        evidence={
            "match_score": strongest_match[4],
            "matched_buttons": [name for name, _value in matched_buttons],
        },
    )


def read_live_nia_outer_subpage(
    snapshot: ProduceOuterLocalSaveSnapshot,
    *,
    allow_event_choice: bool = False,
    pending_weekly_action: str | None = None,
    current_drink_count: int | None = None,
    produce_id: str | None = None,
    idol_card_id: str | None = None,
    pending_event_choice: NiaEventChoicePending | Mapping[str, Any] | None = None,
    recognition_scope: str | None = None,
    allow_development_outing_ocr: bool = False,
) -> Mapping[str, Any]:
    """Capture once and project one supported N.I.A. subpage click."""

    from .controller_client import send_command

    if recognition_scope is None:
        recognition_scope = "all" if allow_event_choice else "subpage"
    maa_batch = dict(
        send_command(
            "recognize_nia_outer_once", timeout=20.0, scope=recognition_scope
        )
    )
    raw_capture = maa_batch.get("capture")
    if not isinstance(raw_capture, Mapping):
        raise ValueError("Maa N.I.A. recognition batch has no capture authority")
    capture = dict(raw_capture)
    path_value = capture.get("png_path")
    if not isinstance(path_value, str) or not path_value:
        raise ValueError("N.I.A. subpage capture has no PNG path")
    path = Path(path_value)
    with Image.open(path.resolve()) as image:
        image.load()
        mirror_recognition = None
        recognized = _maa_recognition_matches(maa_batch)
        has_mirror_marker = any(
            label in recognized
            for label in (
                "mirror-mid1",
                "mirror-mid2",
                "mirror-final",
                "mirror-final-high",
            )
        )
        if (
            has_mirror_marker
            and "common-round-close" not in recognized
            and snapshot.vote_count is not None
            and produce_id is not None
            and idol_card_id is not None
        ):
            try:
                mirror_recognition = dict(
                    send_command("recognize_nia_mirror_once", timeout=20.0)
                )
            except (ConnectionError, RuntimeError, TimeoutError):
                # Older controller processes remain completion-safe until the
                # next UAC restart; the analyzer falls back to the zero row.
                mirror_recognition = None
        surface = analyze_nia_outer_subpage(
            image,
            capture,
            snapshot,
            allow_event_choice=allow_event_choice,
            pending_weekly_action=pending_weekly_action,
            current_drink_count=current_drink_count,
            produce_id=produce_id,
            idol_card_id=idol_card_id,
            pending_event_choice=pending_event_choice,
            mirror_recognition=mirror_recognition,
            maa_recognition=maa_batch,
            allow_development_outing_ocr=allow_development_outing_ocr,
        )
    return {
        "capture": capture,
        **surface.to_dict(),
        "outer_authority": _outer_authority(snapshot),
    }


def _outer_authority(
    snapshot: ProduceOuterLocalSaveSnapshot,
) -> Mapping[str, Any]:
    return {
        "kind": "produce-local-save-before-maa-subpage-input",
        "log_count": snapshot.log_count,
        "week": snapshot.latest_week_marker,
        "last_completed_week": snapshot.last_completed_week,
        "stamina": snapshot.stamina,
        "max_stamina": snapshot.max_stamina,
        "produce_points": snapshot.produce_points,
        "vocal": snapshot.vocal,
        "dance": snapshot.dance,
        "visual": snapshot.visual,
        "vote_count": snapshot.vote_count,
    }


def read_live_nia_click1_fallback(
    snapshot: ProduceOuterLocalSaveSnapshot,
) -> Mapping[str, Any]:
    """Project Maa's final ``Click_1`` fallback after all typed readers fail.

    Maa places this direct top-left click last in ``ProduceEntryNIA`` to move
    otherwise-unlabelled story and transition frames.  The live router calls
    this function only after every named subpage/result/reward reader has
    failed.  The autopilot separately bounds repetitions for one unchanged
    Produce LocalSave authority.
    """

    if not isinstance(snapshot, ProduceOuterLocalSaveSnapshot):
        raise TypeError("snapshot must be ProduceOuterLocalSaveSnapshot")
    from .controller_client import send_command

    # Production N.I.A. is Maa-only.  Do not inherit ``live_source``'s
    # development fallback to a desktop/screen capture when the elevated Maa
    # controller disappears: without the bound PrintWindow authority this
    # last-resort Click_1 must fail before any input is constructed.
    capture = dict(send_command("capture_once", timeout=15.0))
    _capture_path = capture.get("png_path")
    if not isinstance(_capture_path, str) or not _capture_path:
        raise ValueError("N.I.A. Click_1 capture has no PNG path")
    surface = NiaLiveOuterSubpage(
        kind="click-1-fallback",
        target="top-left",
        action=_fixed_action(
            capture,
            kind="click-1-fallback",
            target="top-left",
            point=(22, 22),
            verification_box=(0, 0, 60, 60),
        ),
        evidence={
            "policy": "maa-produce-entry-last-fallback",
            "pipeline_node": "Click_1",
        },
    )
    return {
        "capture": capture,
        **surface.to_dict(),
        "outer_authority": _outer_authority(snapshot),
    }


__all__ = [
    "NiaEventChoicePending",
    "NiaLiveOuterSubpage",
    "analyze_nia_outer_subpage",
    "nia_drink_reject_confirm_after_selection",
    "nia_weekly_actions_from_maa_recognition",
    "read_live_nia_click1_fallback",
    "read_live_nia_outer_subpage",
]
