"""Unattended Initial-Regular outer loop built from existing live backends.

The orchestrator is intentionally dependency-injected: it owns no OCR model,
solver, controller, or agent call.  Production defaults reuse the established
live readers, outer advisor, MAA click helpers, LocalSave lifecycle reader, and
the Plan 2/Plan 3 exam backends.  Unsupported plan families stop before input.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
from functools import lru_cache
import hashlib
from itertools import combinations
import json
from pathlib import Path
from .application_paths import game_file
import re
import sqlite3
import time
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol, Sequence
import unicodedata

from PIL import Image

from .cultivation_contracts import (
    InitialRegularAutopilotStep, InitialRegularAutopilotResult, InitialRegularPlan2ExamContext,
    SCHEMA_NAME, STATUS_COMPLETED, STATUS_HARD_STOP, STATUS_STOPPED,
)
from .produce_effect_prior import rank_produce_effect_categories

if TYPE_CHECKING:
    from .plan2_replay_journal import PendingReplayResult

from .exam_execution_policy import (
    EXACT_EXAM_POLICY,
    ExamExecutionMode,
    ExamExecutionPolicy,
)
from .initial_regular_outer_advisor import (
    STATUS_READY as OUTER_READY,
    InitialRegularOuterAdvice,
    advise_initial_regular_outer,
)
from .nia_route_profile import nia_final_week, nia_phase_for_week, nia_stage_positions
from .live_actions import (
    ChoiceExecutionResult,
    ChoicePreviewAnalysis,
    ClickExecutionResult,
    StaleSuggestionError,
    SuggestedClick,
    SuggestedSwipe,
    compare_frame_paths,
    execute_suggested_click,
    execute_verified_choice_click,
)
from .overview_actions import (
    ACTIVITY,
    DANCE_LESSON,
    REST,
    VISUAL_LESSON,
    VOCAL_LESSON,
)
from .produce_outer_transaction import (
    PHASE_SETTLING,
    PHASE_SUBMITTED,
    ProduceOuterTransaction,
    TransactionOwner,
)
from .route_calendar import BUSINESS, CONSULTATION, OUTING, SPECIAL_GUIDANCE
from .produce_outer_local_save import (
    DEFAULT_PC_GAME_ROOT,
    ProduceOuterLocalSaveSnapshot,
    read_current_produce_outer_local_save,
    read_produce_outer_local_save,
)


PAGE_OVERVIEW = "overview"
PAGE_TRAINING = "training"
PAGE_EXAM = "exam"
PAGE_RESULT = "result"
PAGE_POST_AUDITION_DIALOGUE = "post-audition-dialogue"
PAGE_FINAL_LIVE_START = "final-live-start"
PAGE_FINAL_LIVE_PLAYING = "final-live-playing"
PAGE_POST_LIVE = "post-live"
PAGE_ACTIVITY_REWARD = "activity-reward"
PAGE_TRAINING_REWARD = "training-reward"
PAGE_PASSIVE_NOTIFICATION = "passive-notification"
PAGE_CARD_ACQUIRE_NOTIFICATION = "card-acquire-notification"
PAGE_CARD_REWARD_DISPLAY = "card-reward-display"
PAGE_REWARD = "reward"
PAGE_NIA_OUTER = "nia-outer-subpage"
PAGE_COMPLETED = "completed"
PAGE_UNKNOWN = "unknown"


PLAN2 = "ProducePlanType_Plan2"
PLAN3 = "ProducePlanType_Plan3"
PLAN1 = "ProducePlanType_Plan1"
# This is the budget for an entire 26/27-week cultivation, including every
# outer page, exam turn, animation, reward screen, and Final Live transition.
# Keep it separate from the short same-page retry budget below: a healthy full
# run must not be terminated merely because it legitimately has many pages.
# At the production 0.5 s polling cadence this is at least one hour of polling
# time; controller calls and game animations can extend the wall clock.
NIA_LIVE_MAX_CYCLES = 7200
# Live transitions can legitimately spend several seconds in server/network
# waits and animation.  At the production 0.5 s polling cadence this gives a
# at least a 30 s polling window before a typed hard stop (capture/input time
# can extend the wall clock).  Weekly choices are still submitted exactly
# once; Maa's final Click_1 story fallback is retried more slowly below.
NIA_LIVE_MAX_UNKNOWN_READS = 60
# Read-only transport retries are owned once by ``controller_client``.  This
# semantic loop never retries a whole surface batch, which could mix frames.
# Initial Pro/Master have fewer weeks than N.I.A., but a complete cultivation
# still contains far more than the old 200 polling cycles.  Use a full-run
# envelope here too; click and reconciliation paths keep their own safeguards.
INITIAL_LIVE_MAX_CYCLES = 7200
INITIAL_LIVE_MAX_UNKNOWN_READS = 60
NIA_CLICK1_RETRY_EVERY_READS = 2
# The cross-plan inner imitation bridge is intentionally opt-in.  Keep this
# single constant visible to both the live entrypoint and focused audits.
INNER_IMITATION_RUNTIME_ENABLED_DEFAULT = False
INNER_IMITATION_VERIFIED_PRIOR_CANDIDATE = (
    Path(__file__).resolve().parents[2]
    / "var"
    / "leaderboard_dataset"
    / "v330_nia_native_verified_v1"
    / "episodes.jsonl"
)


@dataclass(slots=True)
class _NiaCardOperationSession:
    target: str
    slots: tuple[tuple[int, int, int, int], ...]
    offers: list[Any] = field(default_factory=list)
    next_slot: int = 1
    pending_slot: int | None = None
    selected_slot: int | None = None
    submitted: bool = False
    submit_attempts: int = 0
    submit_wait_reads: int = 0
    customize_option_candidates: list[Mapping[str, Any]] = field(default_factory=list)
    customize_option_slot: int | None = None
    customize_option_id: str | None = None
    customize_option_select_attempts: int = 0
    customize_option_confirmed: bool = False
    customize_execute_submitted: bool = False
    customize_execute_attempts: int = 0
    customize_execute_wait_reads: int = 0
    customize_confirm_submitted: bool = False
    customize_confirm_attempts: int = 0
    customize_confirm_wait_reads: int = 0
    customize_applied_ids: list[str] = field(default_factory=list)
    customize_exhausted_ids: set[str] = field(default_factory=set)
    customize_total_remaining: int | None = None
    customize_exit_ready: bool = False
    customize_notification_pending: bool = False
    # Special Guidance capability is GUID/count-vector local.  Keep the
    # expensive Plan2 catalog and the resulting materialization decisions on
    # this page transaction so repeated OCR frames never rebuild either one.
    customize_plan2_catalog: Any | None = None
    customize_plan2_capabilities: dict[
        tuple[str, int, tuple[int, ...]], Mapping[str, Any]
    ] = field(default_factory=dict)
    ranking_source_offers: tuple[Any, ...] | None = None
    ranked_offers: tuple[Any, ...] = ()
    ranking_evidence: Mapping[str, Any] | None = None


@dataclass(slots=True)
class _NiaRewardOcrSession:
    slots: tuple[tuple[int, int, int, int], ...]
    unresolved_slots: list[int]
    offers: list[Any] = field(default_factory=list)
    preview_attempts: dict[int, int] = field(default_factory=dict)
    pending_slot: int | None = None
    selected_slot: int | None = None
    submitted: bool = False
    reroll_authority: Any | None = None
    reroll_action: SuggestedClick | None = None
    ranking_source_offers: tuple[Any, ...] | None = None
    ranked_offers: tuple[Any, ...] = ()
    ranking_evidence: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class _NiaRewardRerollPending:
    """One submitted reroll awaiting its server-owned counter decrement.

    Candidate identities and slot coordinates are intentionally absent.  The
    next reward row must be located and previewed from scratch after the
    counter changes.
    """

    remaining_count_before: int
    authority_source: str


@dataclass(slots=True)
class _NiaItemRewardSession:
    slots: tuple[tuple[int, int, int, int], ...]
    unresolved_slots: list[int]
    offers: list[Mapping[str, Any]] = field(default_factory=list)
    preview_attempts: dict[int, int] = field(default_factory=dict)
    pending_slot: int | None = None
    selected_slot: int | None = None
    submitted: bool = False
    inventory_before: tuple[str, ...] = ()
    winner_offer: Mapping[str, Any] | None = None
    replacement_acquired: bool = False
    receive_attempts: int = 0
    receive_wait_reads: int = 0
    card_handoff_probe_slot: int | None = None
    card_handoff_probe_attempts: int = 0
    card_handoff_probe_wait_reads: int = 0


@dataclass(slots=True)
class _NiaDrinkKeepSession:
    candidate_signature: tuple[tuple[object, ...], ...]
    desired_indices: frozenset[int]
    pending_kind: str | None = None
    pending_index: int | None = None
    pending_state: tuple[tuple[int, ...], int] | None = None
    unchanged_reads: int = 0
    pending_attempts: int = 0


@dataclass(frozen=True, slots=True)
class _InitialChoiceTransaction:
    """One persistent owner for a multi-frame outer choice transaction.

    Card and item reward pages deliberately share much of the same Maa/card
    geometry.  Classification is therefore performed only when a transaction
    begins.  Until the server-owned Produce snapshot changes (or the submitted
    page is visibly gone), later animation/detail frames stay with that owner
    instead of entering the global page-reader competition again.
    """

    owner: str
    authority_key: tuple[object, ...]

    def __post_init__(self) -> None:
        if self.owner not in {"card-reward", "item-reward"}:
            raise ValueError("unsupported Initial choice transaction owner")


class _InitialChoiceOwnerMismatch(ValueError):
    """A reversible full-title preview proved a different transaction type."""

    def __init__(self, owner: str, title: str) -> None:
        super().__init__(f"{owner}:{title}")
        self.owner = owner
        self.title = title


class _NiaRewardPreviewNotReady(ValueError):
    """A reversible reward-card selection has not exposed a usable title yet."""

    def __init__(self, title: str) -> None:
        super().__init__(title)
        self.title = title


@dataclass(frozen=True, slots=True)
class _NiaProduceItemName:
    item_id: str
    official_name: str
    display_name: str
    effect_types: tuple[str, ...]
    resource_types: tuple[str, ...]
    rarity: str = ""
    asset_id: str = ""


_NIA_PRODUCE_ITEM_TRANSLATION = game_file('gakumas-local/local-files/masterTrans/ProduceItem.json')
_PRODUCE_DRINK_TRANSLATION = game_file('gakumas-local/local-files/masterTrans/ProduceDrink.json')


def _normalize_nia_item_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


_NIA_DRINK_EFFECT_OCR_TRANSLATION = str.maketrans(
    {
        # The PC text is Traditional Chinese, but the OCR model can emit the
        # visually equivalent Simplified glyph for 幹勁.  Canonicalize the
        # observed glyph before matching against translated Master text.
        "劲": "勁",
    }
)


def _normalize_nia_drink_effect_text(value: str) -> str:
    return _normalize_nia_item_text(value).translate(
        _NIA_DRINK_EFFECT_OCR_TRANSLATION
    )


@lru_cache(maxsize=1)
def _nia_produce_item_names() -> tuple[_NiaProduceItemName, ...]:
    """Load the complete Master/localized P-item name table once.

    Visible slot order is intentionally absent from this catalog.  Master
    supplies the drink artwork key and broad effect kind; localized names
    remain available for the OCR fallback and reward previews.
    """

    from .master_db import DEFAULT_DATABASE

    translations: dict[str, str] = {}
    if _NIA_PRODUCE_ITEM_TRANSLATION.is_file():
        raw = json.loads(
            _NIA_PRODUCE_ITEM_TRANSLATION.read_text(encoding="utf-8-sig")
        )
        rows = raw.get("data") if isinstance(raw, Mapping) else None
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                item_id = row.get("id")
                name = row.get("name")
                if isinstance(item_id, str) and isinstance(name, str) and name.strip():
                    translations[item_id] = name.strip()

    drink_translations: dict[str, str] = {}
    if _PRODUCE_DRINK_TRANSLATION.is_file():
        raw = json.loads(
            _PRODUCE_DRINK_TRANSLATION.read_text(encoding="utf-8-sig")
        )
        rows = raw.get("data") if isinstance(raw, Mapping) else None
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                drink_id = row.get("id")
                name = row.get("name")
                if isinstance(drink_id, str) and isinstance(name, str) and name.strip():
                    drink_translations[drink_id] = name.strip()

    from .drink_catalog import load_drink_catalog

    drink_catalog = {
        drink.id: drink for drink in load_drink_catalog(DEFAULT_DATABASE).drinks
    }
    with closing(sqlite3.connect(DEFAULT_DATABASE)) as connection:
        connection.row_factory = sqlite3.Row
        item_effects = {
            str(row["id"]): (
                str(row["effect_type"]),
                str(row["produce_effect_id"]),
            )
            for row in connection.execute(
                "SELECT id, effect_type, produce_effect_id FROM produce_item_effect"
            )
        }
        produce_effects = {
            str(row["id"]): (
                str(row["effect_type"]),
                str(row["resource_type"]),
            )
            for row in connection.execute(
                "SELECT id, effect_type, resource_type FROM produce_effect"
            )
        }
        result: list[_NiaProduceItemName] = []
        for row in connection.execute(
            "SELECT id, name, produce_item_effect_ids_json "
            "FROM produce_item ORDER BY id"
        ):
            item_id = str(row["id"])
            effect_types: list[str] = []
            resource_types: list[str] = []
            try:
                effect_ids = json.loads(str(row["produce_item_effect_ids_json"]))
            except json.JSONDecodeError:
                effect_ids = []
            if isinstance(effect_ids, list):
                for effect_id in effect_ids:
                    item_effect = item_effects.get(str(effect_id))
                    if item_effect is None:
                        continue
                    item_effect_type, produce_effect_id = item_effect
                    produce_effect = produce_effects.get(produce_effect_id)
                    if produce_effect is None:
                        effect_types.append(item_effect_type)
                        continue
                    effect_type, resource_type = produce_effect
                    effect_types.append(effect_type)
                    resource_types.append(resource_type)
            official_name = str(row["name"])
            result.append(
                _NiaProduceItemName(
                    item_id=item_id,
                    official_name=official_name,
                    display_name=translations.get(item_id, official_name),
                    effect_types=tuple(effect_types),
                    resource_types=tuple(resource_types),
                    rarity="",
                )
            )
        for row in connection.execute(
            "SELECT id, name FROM produce_drink ORDER BY id"
        ):
            drink_id = str(row["id"])
            official_name = str(row["name"])
            drink = drink_catalog.get(drink_id)
            drink_effect_types = () if drink is None else tuple(
                ref.effect.effect_type for ref in drink.effect_refs
            )
            result.append(
                _NiaProduceItemName(
                    item_id=drink_id,
                    official_name=official_name,
                    display_name=drink_translations.get(drink_id, official_name),
                    effect_types=("ProduceDrink", *drink_effect_types),
                    resource_types=(),
                    rarity="" if drink is None else drink.rarity,
                    asset_id="" if drink is None else drink.asset_id,
                )
            )
    return tuple(result)


_NIA_DRINK_ICON_SIZE = 62
_NIA_DRINK_ICON_OFFSET_X = 18
_NIA_DRINK_ICON_OFFSET_Y = 22
_NIA_DRINK_ICON_ALPHA_THRESHOLD = 0.35
_NIA_DRINK_ICON_MSE_THRESHOLD = 0.05
_NIA_DRINK_ICON_MSE_MARGIN = 0.02


@lru_cache(maxsize=1)
def _nia_drink_icon_templates() -> tuple[tuple[str, Any], ...]:
    """Load the fixed 512px P-drink artwork from the local Octo cache.

    This is deliberately read-only and lazy.  If the game cache, manifest, or
    Unity texture reader is unavailable, the empty tuple makes the caller use
    the existing effect-text OCR path instead of blocking a live transaction.
    """

    import numpy as np

    try:
        from .octo_assets import OctoAssetIndex

        index = OctoAssetIndex.load()
    except Exception:
        # Static artwork is an optional accelerator; any cache/parser failure
        # must leave the established OCR fallback available.
        return ()

    templates: list[tuple[str, Any]] = []
    for asset in index.assets:
        if re.fullmatch(r"img_general_pdrink_[123]-\d{3}", asset.name) is None:
            continue
        try:
            image = index.extract_texture(asset.name).convert("RGBA")
            array = np.asarray(
                image.resize(
                    (_NIA_DRINK_ICON_SIZE, _NIA_DRINK_ICON_SIZE),
                    Image.Resampling.LANCZOS,
                ),
                dtype=np.float32,
            ) / 255.0
        except Exception:
            continue
        if array.shape == (_NIA_DRINK_ICON_SIZE, _NIA_DRINK_ICON_SIZE, 4):
            templates.append((asset.name, array))
    return tuple(sorted(templates, key=lambda value: value[0]))


def _nia_drink_icon_identity(
    screen: Any,
    row: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Resolve a visible drink row from its fixed artwork, if unique.

    The row box is the full-width pale control.  In the canonical 720x1280
    capture the drink artwork is a 62px square at a stable offset from that
    box.  Only opaque pixels of the artwork template participate in the score;
    the top-right status/info badge is excluded, so the rarity frame, badge,
    and checkbox cannot become identity features.
    """

    raw_box = row.get("box")
    if not (
        isinstance(raw_box, list)
        and len(raw_box) == 4
        and all(isinstance(value, int) and not isinstance(value, bool) for value in raw_box)
    ):
        return None
    left, top, right, bottom = raw_box
    row_width = right - left
    row_height = bottom - top
    if (
        row_width < 500
        or row_height < 90
        or left < 0
        or top < 0
        or right <= left
        or bottom <= top
    ):
        return None
    icon_left = left + _NIA_DRINK_ICON_OFFSET_X
    icon_top = top + _NIA_DRINK_ICON_OFFSET_Y
    icon_right = icon_left + _NIA_DRINK_ICON_SIZE
    icon_bottom = icon_top + _NIA_DRINK_ICON_SIZE
    import numpy as np

    try:
        array = np.asarray(screen)
    except (TypeError, ValueError):
        return None
    if array.ndim != 3 or array.shape[2] < 3:
        return None
    if icon_right > array.shape[1] or icon_bottom > array.shape[0]:
        return None

    target = np.asarray(
        array[icon_top:icon_bottom, icon_left:icon_right, :3],
        dtype=np.float32,
    )
    if target.shape != (_NIA_DRINK_ICON_SIZE, _NIA_DRINK_ICON_SIZE, 3):
        return None
    if float(target.max()) > 1.5:
        target /= 255.0

    yy, xx = np.indices((_NIA_DRINK_ICON_SIZE, _NIA_DRINK_ICON_SIZE))
    scored: list[tuple[float, str]] = []
    for asset_name, template in _nia_drink_icon_templates():
        mask = template[:, :, 3] >= _NIA_DRINK_ICON_ALPHA_THRESHOLD
        # The small circular info badge overlaps this corner of the artwork
        # in the live page.  It is UI chrome, not part of the fixed drink art.
        mask &= ~((xx >= 48) & (yy < 18))
        if int(mask.sum()) < 100:
            continue
        score = float(np.mean((target[mask] - template[:, :, :3][mask]) ** 2))
        scored.append((score, asset_name))
    if len(scored) < 2:
        return None
    scored.sort()
    best_score, best_asset = scored[0]
    runner_up_score, _runner_up_asset = scored[1]
    margin = runner_up_score - best_score
    if (
        best_score > _NIA_DRINK_ICON_MSE_THRESHOLD
        or margin < _NIA_DRINK_ICON_MSE_MARGIN
    ):
        return None
    return {
        "asset_id": best_asset,
        "icon_box": [icon_left, icon_top, icon_right, icon_bottom],
        "icon_mse": best_score,
        "icon_runner_up_mse": runner_up_score,
        "icon_mse_margin": margin,
        "icon_template_count": len(scored),
    }


def _nia_drink_owned_tail_clip_evidence(
    rows: Sequence[Mapping[str, Any]],
    *,
    section_break_after: int,
) -> Mapping[str, Any] | None:
    """Describe a bottom-owned row whose identity area is footer-clipped.

    The capacity sheet is a scrolling list.  Its row height is stable within
    one frame, while the final visible row becomes shorter when the list
    viewport meets the footer.  Compare that row with the visible rows in the
    same frame instead of using a fixed pixel cutoff: this remains meaningful
    for captures produced at another scale and is independent of a drink's
    rarity, icon, or effect text.  The caller uses this evidence only after an
    identity read failed, so a clipped row with a complete icon/text identity
    does not cause an unnecessary swipe.
    """

    if (
        isinstance(section_break_after, bool)
        or not isinstance(section_break_after, int)
        or section_break_after < 1
        or section_break_after >= len(rows)
    ):
        return None
    owned_rows = rows[section_break_after:]
    if not owned_rows:
        return None

    def height(row: Mapping[str, Any]) -> int | None:
        raw_box = row.get("box")
        if not (
            isinstance(raw_box, list)
            and len(raw_box) == 4
            and all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in raw_box
            )
        ):
            return None
        value = int(raw_box[3]) - int(raw_box[1])
        return value if value > 0 else None

    tail_height = height(owned_rows[-1])
    if tail_height is None:
        return None
    reference_heights = [
        value
        for row in rows[:-1]
        if (value := height(row)) is not None
    ]
    if not reference_heights:
        return None
    reference_heights.sort()
    reference_height = reference_heights[len(reference_heights) // 2]
    if reference_height <= 0:
        return None

    # A normal row's colour run can vary by a pixel or two at antialiased
    # edges.  A six-percent deficit is therefore reserved for a real viewport
    # cut, while the observed 91/107px row is unambiguously clipped.
    ratio = tail_height / reference_height
    if ratio >= 0.94:
        return None
    return {
        "basis": "owned-tail-visible-height-relative-to-current-row-baseline",
        "tail_row_index": section_break_after + len(owned_rows),
        "tail_visible_height": tail_height,
        "reference_visible_height": reference_height,
        "tail_to_reference_ratio": round(ratio, 4),
        "identity_area": "bottom-owned-row-footer-clipped",
    }


def _match_nia_produce_item(title: str) -> _NiaProduceItemName | None:
    query = _normalize_nia_item_text(title)
    if not query:
        return None
    best: _NiaProduceItemName | None = None
    best_score = 0.0
    for entry in _nia_produce_item_names():
        for alias in (entry.display_name, entry.official_name):
            candidate = _normalize_nia_item_text(alias)
            if not candidate:
                continue
            score = 1.0 if query == candidate else SequenceMatcher(
                None, query, candidate
            ).ratio()
            if score > best_score:
                best = entry
                best_score = score
    return best if best_score >= 0.68 else None


@lru_cache(maxsize=1)
def _nia_drink_effect_catalog() -> tuple[tuple[_NiaProduceItemName, str], ...]:
    """Return exact translated effect text for every Master P drink."""

    if not _PRODUCE_DRINK_TRANSLATION.is_file():
        return ()
    raw = json.loads(
        _PRODUCE_DRINK_TRANSLATION.read_text(encoding="utf-8-sig")
    )
    rows = raw.get("data") if isinstance(raw, Mapping) else None
    if not isinstance(rows, list):
        return ()
    entries = {entry.item_id: entry for entry in _nia_produce_item_names()}
    result: list[tuple[_NiaProduceItemName, str]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        drink_id = row.get("id")
        descriptions = row.get("produceDescriptions")
        entry = entries.get(drink_id) if isinstance(drink_id, str) else None
        if entry is None or not isinstance(descriptions, list):
            continue
        fragments = [
            str(description.get("text", ""))
            for description in descriptions
            if isinstance(description, Mapping)
        ]
        visible = re.sub(r"<[^>]+>", "", "".join(fragments))
        normalized = _normalize_nia_drink_effect_text(visible)
        if normalized:
            result.append((entry, normalized))
    return tuple(result)


def _match_nia_drink_effect(
    visible_effect: str,
) -> tuple[_NiaProduceItemName, float, float] | None:
    """Resolve one acquired drink from its current translated effect lines.

    A unique margin is required in addition to similarity.  If Localify, OCR,
    or a future Master update makes the visible description ambiguous, the
    capacity transaction remains a zero-input wait instead of assigning an
    identity from artwork, colour, or row order.
    """

    query = _normalize_nia_drink_effect_text(visible_effect)
    if len(query) < 2:
        return None
    # A one-glyph OCR dropout can reduce compact effects such as 幹勁+3 to
    # 勁+3.  Accept that shape only when it still contains the numeric value;
    # the unique-score and runner-up margin below remain authoritative.
    if len(query) == 2 and not any(character.isdigit() for character in query):
        return None
    query_numbers = tuple(re.findall(r"\d+", query))
    ranked = sorted(
        (
            SequenceMatcher(None, query, candidate).ratio(),
            entry.item_id,
            entry,
        )
        for entry, candidate in _nia_drink_effect_catalog()
        # Similarity is allowed to recover a missing or variant glyph, never
        # a changed numeric effect value.  A mistaken +2/+3 identity can alter
        # the selected three-drink set even when the surrounding words look
        # almost identical.
        if tuple(re.findall(r"\d+", candidate)) == query_numbers
    )
    if not ranked:
        return None
    best_score, _best_id, best = ranked[-1]
    second_score = ranked[-2][0] if len(ranked) > 1 else 0.0
    minimum_score = 0.78 if len(query) == 2 else 0.70
    minimum_margin = 0.15 if len(query) == 2 else 0.08
    if (
        best_score < minimum_score
        or best_score - second_score < minimum_margin
    ):
        return None
    return best, best_score, second_score


_NIA_DRINK_EFFECT_FAMILIES = (
    ("ExamCardUpgrade", "card-upgrade"),
    ("ExamExtraTurn", "extra-turn"),
    ("ExamPlayableValueAdd", "extra-play"),
    ("ExamLessonValueMultiple", "score-multiplier"),
    ("ExamCardSearchEffectPlayCountBuff", "play-count-buff"),
    ("ExamStaminaConsumptionDown", "cost-down"),
    ("ExamCardCreateSearch", "card-create"),
    ("ExamForcePlayCardSearch", "force-play"),
    ("ExamStaminaRecoverFix", "stamina-recovery"),
    ("ExamHandGraveCountCardDraw", "redraw"),
    ("ExamCardDraw", "draw"),
    ("ExamBlock", "block"),
    ("ExamLessonDependExamReview", "review-score"),
    ("ExamReview", "review"),
    ("ExamCardPlayAggressive", "aggressive"),
    ("ExamParameterBuff", "parameter-buff"),
    ("ExamLessonBuff", "lesson-buff"),
    ("ExamConcentration", "concentration"),
    ("ExamPreservation", "preservation"),
    ("ExamFullPowerPoint", "full-power"),
    ("ExamLesson", "direct-score"),
)


def _nia_drink_effect_families(
    entry: _NiaProduceItemName,
) -> frozenset[str]:
    exact_types = frozenset(
        value.rsplit("_", 1)[-1]
        for value in entry.effect_types
    )
    return frozenset(
        family
        for token, family in _NIA_DRINK_EFFECT_FAMILIES
        if token in exact_types
    )


def _nia_drink_inventory_adjustment(
    entry: _NiaProduceItemName,
    owned_drink_ids: Sequence[str],
    *,
    exam_effect_type: str,
) -> tuple[int, str]:
    """Score only proven marginal inventory value within one rarity tier."""

    if not owned_drink_ids:
        return 0, ""
    by_id = {value.item_id: value for value in _nia_produce_item_names()}
    owned = tuple(
        value
        for drink_id in owned_drink_ids
        if (value := by_id.get(drink_id)) is not None
        and "ProduceDrink" in value.effect_types
    )
    if not owned:
        return 0, ""

    exact_count = sum(value.item_id == entry.item_id for value in owned)
    candidate_families = _nia_drink_effect_families(entry)
    owned_family_counts: dict[str, int] = {}
    for value in owned:
        for family in _nia_drink_effect_families(value):
            owned_family_counts[family] = owned_family_counts.get(family, 0) + 1
    overlap = sum(
        owned_family_counts.get(family, 0)
        for family in candidate_families
    )
    new_families = candidate_families.difference(owned_family_counts)

    # Rarity remains the outer ordering.  Inside that tier, an exact duplicate
    # is worse than a new useful effect family, while a genuinely missing
    # archetype tool receives a small additional marginal bonus.
    adjustment = -min(2_400, exact_count * 1_600)
    adjustment -= min(480, overlap * 120)
    adjustment += min(360, len(new_families) * 180)
    reason_bits: list[str] = []
    if exact_count:
        reason_bits.append("exact-duplicate")
    if new_families:
        reason_bits.append("inventory-complement")

    archetype_family = {
        "ProduceExamEffectType_ExamReview": "review",
        "ProduceExamEffectType_ExamCardPlayAggressive": "aggressive",
    }.get(exam_effect_type)
    if (
        archetype_family is not None
        and archetype_family in candidate_families
        and owned_family_counts.get(archetype_family, 0) == 0
    ):
        adjustment += 200
        reason_bits.append("archetype-complement")
    return adjustment, "+".join(reason_bits)


def _rank_nia_item_offer(
    entry: _NiaProduceItemName | None,
    visible_effect: str,
    *,
    exam_effect_type: str = "",
    owned_drink_ids: Sequence[str] = (),
) -> tuple[int, str]:
    """Coarse policy only; the game remains authority for the actual reward."""

    effect_types = () if entry is None else entry.effect_types
    resource_types = () if entry is None else entry.resource_types
    joined = " ".join((*effect_types, *resource_types, visible_effect))
    if "ProduceDrink" in effect_types:
        # The reward frame's colour is not an identity source.  Master rarity
        # is: N.I.A. drink rewards use the same exact drink ID resolved from
        # the reversible full-title preview.  Rarity is intentionally the
        # primary ordering, then the existing archetype/effect policy decides
        # between drinks of the same rarity.
        rarity_bonus = {
            "ProduceDrinkRarity_R": 0,
            "ProduceDrinkRarity_Sr": 5_000,
            "ProduceDrinkRarity_Ssr": 10_000,
            "R": 0,
            "SR": 5_000,
            "SSR": 10_000,
        }.get("" if entry is None else entry.rarity, 0)
        marginal, marginal_reason = _nia_drink_inventory_adjustment(
            entry,
            owned_drink_ids,
            exam_effect_type=exam_effect_type,
        )

        def ranked(score: int, reason: str) -> tuple[int, str]:
            suffix = f"+{marginal_reason}" if marginal_reason else ""
            return rarity_bonus + score + marginal, reason + suffix

        review = "ProduceExamEffectType_ExamReview"
        aggressive = "ProduceExamEffectType_ExamCardPlayAggressive"
        if exam_effect_type == review and aggressive in effect_types:
            return ranked(200, "drink-archetype-mismatch-aggressive")
        if exam_effect_type == aggressive and review in effect_types:
            return ranked(200, "drink-archetype-mismatch-review")
        if (
            exam_effect_type == review
            and any("ExamReview" in value for value in effect_types)
        ):
            return ranked(1160, "drink-review-synergy")
        if exam_effect_type == aggressive and aggressive in effect_types:
            return ranked(1160, "drink-aggressive-synergy")
        drink_priorities = (
            ("ExamCardUpgrade", 1200, "drink-card-upgrade"),
            ("ExamExtraTurn", 1180, "drink-extra-turn"),
            ("ExamPlayableValueAdd", 1160, "drink-extra-play"),
            ("ExamLessonValueMultiple", 1100, "drink-score-multiplier"),
            ("ExamCardSearchEffectPlayCountBuff", 1080, "drink-play-count-buff"),
            ("ExamStaminaConsumptionDown", 1040, "drink-cost-down"),
            ("ExamCardCreateSearch", 1020, "drink-card-create"),
            ("ExamForcePlayCardSearch", 1010, "drink-force-play"),
            ("ExamStaminaRecoverFix", 940, "drink-stamina-recovery"),
            ("ExamHandGraveCountCardDraw", 920, "drink-redraw"),
            ("ExamCardDraw", 900, "drink-draw"),
            ("ExamBlock", 780, "drink-block"),
            ("ExamLesson", 760, "drink-direct-score"),
        )
        matches = tuple(
            (score, reason)
            for token, score, reason in drink_priorities
            if token in joined
        )
        if matches:
            score, reason = max(matches)
            return ranked(score, reason)
        return ranked(750, "drink")
    category_prior = rank_produce_effect_categories((joined,))
    if category_prior is not None:
        return category_prior
    visible_priorities = (
        (("強化", "强化"), 1000, "visible-card-upgrade"),
        (("弱化",), 900, "visible-weaken"),
        (("投票",), 850, "visible-vote"),
        (("屬性", "属性"), 800, "visible-attribute"),
        (("飲料", "饮料"), 750, "visible-drink"),
        (("體力", "体力"), 700, "visible-stamina"),
        (("P點", "P点"), 600, "visible-produce-point"),
    )
    for tokens, score, reason in visible_priorities:
        if any(token in visible_effect for token in tokens):
            return score, reason
    return 100, "visible-item"


def _best_nia_drink_keep_indices(
    entries: Sequence[_NiaProduceItemName],
    *,
    exam_effect_type: str,
    capacity: int = 3,
    leaderboard_drink_prior: Any | None = None,
) -> tuple[tuple[int, ...], tuple[Mapping[str, Any], ...]] | None:
    """Rank complete UI-reachable sets with one retained row per drink ID.

    ``leaderboard_drink_prior`` is advisory only.  Entries are still the
    identity-resolved Master rows supplied by the caller; the prior can add a
    bounded bonus to their existing static score, but cannot create, remove,
    or otherwise legalize a candidate.
    """

    if capacity < 1 or len(entries) < capacity:
        return None
    best_indices: tuple[int, ...] | None = None
    best_details: tuple[Mapping[str, Any], ...] = ()
    best_key: tuple[Any, ...] | None = None
    for indices in combinations(range(len(entries)), capacity):
        ids = tuple(entries[index].item_id for index in indices)
        # The capacity chooser exposes duplicate rows as replacement choices,
        # but only one checkbox for an exact drink ID can remain selected at a
        # time.  Ranking a simultaneous duplicate set creates an unreachable
        # target and an endless toggle loop.
        if len(set(ids)) != len(ids):
            continue
        details: list[Mapping[str, Any]] = []
        for position, index in enumerate(indices):
            retained = (*ids[:position], *ids[position + 1 :])
            static_score, reason = _rank_nia_item_offer(
                entries[index],
                "",
                exam_effect_type=exam_effect_type,
                owned_drink_ids=retained,
            )
            learned_bonus = 0
            if leaderboard_drink_prior is not None:
                score_for = getattr(leaderboard_drink_prior, "score_for", None)
                if callable(score_for):
                    try:
                        candidate_bonus = score_for(entries[index].item_id)
                    except (TypeError, ValueError):
                        candidate_bonus = 0
                    if (
                        isinstance(candidate_bonus, int)
                        and not isinstance(candidate_bonus, bool)
                        and candidate_bonus > 0
                    ):
                        learned_bonus = candidate_bonus
            score = static_score + learned_bonus
            if learned_bonus:
                reason = f"{reason}+leaderboard-drink-prior={learned_bonus}"
            details.append(
                {
                    "index": index,
                    "item_id": entries[index].item_id,
                    "static_score": static_score,
                    "leaderboard_drink_prior_bonus": learned_bonus,
                    "score": score,
                    "reason": reason,
                }
            )
        scores = tuple(int(detail["score"]) for detail in details)
        # The first two terms express set value; the remaining terms are only
        # deterministic tie breakers and never encode visible slot meaning.
        key = (
            sum(scores),
            tuple(sorted(scores, reverse=True)),
            tuple(sorted(ids)),
            tuple(-index for index in indices),
        )
        if best_key is None or key > best_key:
            best_key = key
            best_indices = indices
            best_details = tuple(details)
    if best_indices is None:
        return None
    return best_indices, best_details


def _replay_produce_play_log_drink_inventory(
    raw: Any,
    *,
    capacity: int = 3,
) -> tuple[str, ...] | None:
    """Replay only schema-proven P-drink mutations from ProducePlayLog.

    ``DrinkAdd`` appends before capacity.  A full-inventory replacement is
    accepted only when ``targetId2`` names the exact removed drink; the game
    has no generic DrinkRemove line enum.  A ``Drink`` cell records use of its
    top-level trigger drink.  Any unknown/malformed mutation fails closed so a
    visible capacity row can never inherit identity from acquisition order
    after an unmodelled change.
    """

    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
        return None
    if not isinstance(raw, Mapping):
        return None
    logs = raw.get("_logList")
    if not isinstance(logs, list):
        return None
    inventory: list[str] = []

    def drink_id(value: Any) -> str | None:
        return value if isinstance(value, str) and value.startswith("pdrink_") else None

    for raw_log in logs:
        if not isinstance(raw_log, Mapping):
            return None
        cell_type = raw_log.get("_cellType")
        if isinstance(cell_type, bool) or not isinstance(cell_type, int):
            return None
        top_trigger = raw_log.get("_triggerId")
        if not isinstance(top_trigger, str):
            return None
        if cell_type == 1:
            used_id = drink_id(top_trigger)
            if used_id is None:
                return None
            matching = [
                index for index, value in enumerate(inventory) if value == used_id
            ]
            if not matching:
                return None
            # Identical adjacent duplicates have one observable post-use ID
            # order.  Non-adjacent duplicates would produce different orders
            # and the serialized subscription number has no proven slot
            # semantics, so preserve ambiguity as a blocker.
            if len(matching) > 1 and matching != list(
                range(matching[0], matching[-1] + 1)
            ):
                return None
            inventory.pop(matching[0])
        elif drink_id(top_trigger) is not None:
            # A future non-Drink cell carrying a top-level drink trigger could
            # encode a new mutation class; do not silently treat it as no-op.
            return None

        details = raw_log.get("_detailList")
        if not isinstance(details, list):
            return None
        for raw_detail in details:
            if not isinstance(raw_detail, Mapping):
                return None
            lines = raw_detail.get("_detailLineList")
            if not isinstance(lines, list):
                return None
            for raw_line in lines:
                if not isinstance(raw_line, Mapping):
                    return None
                line_type = raw_line.get("_detailLineType")
                if isinstance(line_type, bool) or not isinstance(line_type, int):
                    return None
                target = raw_line.get("_targetId")
                target2 = raw_line.get("_targetId2")
                if not isinstance(target, str) or not isinstance(target2, str):
                    return None
                target_drink = drink_id(target)
                target2_drink = drink_id(target2)
                if line_type != 16:
                    if target_drink is not None or target2_drink is not None:
                        return None
                    continue
                if target_drink is None:
                    return None
                if target2:
                    if target2_drink is None or inventory.count(target2_drink) != 1:
                        return None
                    replaced_index = inventory.index(target2_drink)
                    inventory[replaced_index] = target_drink
                else:
                    if len(inventory) >= capacity:
                        # A full-capacity add without an exact removed ID is an
                        # ambiguous replacement, not an append of a fourth
                        # owned bottle.
                        return None
                    inventory.append(target_drink)
    return tuple(inventory)


class OuterAdvice(Protocol):
    status: str
    action: str
    reason: str
    attribute: str | None


def _nia_outer_decision_telemetry_source(
    advice: Any,
    legal_actions: Sequence[str],
    *,
    prior: Any | None,
    produce_id: str,
    idol_card_id: str,
    character_id: str | None,
    plan_type: str | None,
    exam_effect_type: str | None,
) -> dict[str, Any]:
    """Serialize the already-loaded outer prior decision context.

    This helper is intentionally telemetry-only.  It never supplies an action
    and never participates in the advisor's selection key.  ``legal_actions``
    is the deduplicated runtime order produced by the advisor itself, so the
    recorded prior rank cannot be mistaken for a separately reconstructed UI
    candidate order.
    """

    legal = tuple(
        dict.fromkeys(value for value in legal_actions if isinstance(value, str))
    )
    rank: tuple[str, ...] = ()
    rank_source: str | None = None
    rank_provenance: tuple[str, ...] = ()
    week = getattr(advice, "week", None)
    phase = (
        nia_phase_for_week(produce_id, week)
        if type(week) is int and produce_id in {"produce-004", "produce-005"}
        else None
    )
    if prior is not None:
        features = {
            "produce_id": produce_id,
            "idol_card_id": idol_card_id,
            "character_id": character_id,
            "plan_type": plan_type,
            "exam_effect_type": exam_effect_type,
            "difficulty": getattr(advice, "difficulty", None),
            "week": week,
            "phase": phase,
            "stage": getattr(advice, "stage", None),
        }
        try:
            rank_with_source = getattr(prior, "rank_with_source", None)
            if callable(rank_with_source):
                raw_rank, raw_source = rank_with_source(features, legal)
            else:
                raw_rank = prior.rank(features, legal)
                raw_source = getattr(prior, "last_rank_source", None)
            rank = tuple(
                dict.fromkeys(value for value in raw_rank if value in legal)
            )
            if rank and raw_source in {"exact", "broad"}:
                rank_source = raw_source
                raw_provenance = getattr(prior, "last_rank_provenance", None)
                if raw_provenance is None:
                    raw_provenance = getattr(prior, "rank_provenance", None)
                if isinstance(raw_provenance, (tuple, list)):
                    rank_provenance = tuple(
                        value
                        for value in raw_provenance
                        if isinstance(value, str) and value
                    )
        except (KeyError, TypeError, ValueError):
            rank = ()
            rank_source = None
    return {
        "advisor_reason": str(getattr(advice, "reason", "")),
        "advisor_action": getattr(advice, "action", None),
        "runtime_legal_actions": list(legal),
        "learned_prior_available": prior is not None,
        "learned_prior_abstained": not bool(rank),
        "learned_prior_rank": list(rank),
        "learned_prior_top1": rank[0] if rank else None,
        "learned_prior_source": rank_source,
        "learned_prior_provenance": list(rank_provenance),
    }


@dataclass(frozen=True, slots=True)
class InitialRegularSurface:
    page: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.page not in {
            PAGE_OVERVIEW,
            PAGE_TRAINING,
            PAGE_EXAM,
            PAGE_RESULT,
            PAGE_POST_AUDITION_DIALOGUE,
            PAGE_FINAL_LIVE_START,
            PAGE_FINAL_LIVE_PLAYING,
            PAGE_POST_LIVE,
            PAGE_ACTIVITY_REWARD,
            PAGE_TRAINING_REWARD,
            PAGE_PASSIVE_NOTIFICATION,
            PAGE_CARD_REWARD_DISPLAY,
            PAGE_REWARD,
            PAGE_NIA_OUTER,
            PAGE_COMPLETED,
            PAGE_UNKNOWN,
        }:
            raise ValueError(f"unsupported Initial Regular surface: {self.page}")
        if not isinstance(self.payload, Mapping):
            raise TypeError("surface payload must be a mapping")


@dataclass(frozen=True, slots=True)
class _NiaWeeklyEventSubmission:
    produce_id: str
    week: int
    target: str
    surfaces: tuple[Any, ...]
    legal_action_ids: tuple[str, ...]
    candidate_set_complete: bool
    phase: str | None
    observer_anchor: Mapping[str, Any]
    observer_anchor_complete: bool
    decision_source: Mapping[str, Any]


_MONITOR_CAPTURE_FIELDS = (
    "png_path",
    "timestamp",
    "hwnd",
    "pid",
    "width",
    "height",
    "capture_method",
    "backend",
)
_MONITOR_STATE_FIELDS = (
    "week",
    "weeks_remaining",
    "current_turn",
    "remain_turn",
    "round_number",
    "turn",
    "turns_remaining",
    "stamina",
    "max_stamina",
    "produce_points",
    "vocal",
    "dance",
    "visual",
    "score",
    "player_score",
    "block",
    "good_impression",
    "motivation",
    "plays_remaining",
    "turn_card_play_count",
    "exam_card_play_count",
    "status_effect_count",
    "removed_status_effect_count",
    "step_type",
    "step_type_value",
    "stage_number",
)
_MONITOR_CARD_FIELDS = (
    "card_id",
    "id",
    "guid",
    "display_name",
    "name",
    "upgrade",
    "effective_upgrade",
    "hand_index",
    "slot",
    "legal",
    "fully_supported",
    "play_count",
)


def _exam_state_monitor_payload(
    state: object,
    source: Path,
    *,
    source_marker: tuple[int, int],
) -> dict[str, Any]:
    """Project the already-decoded exact ExamSave state for display only."""

    state_values: dict[str, Any] = {}
    for field in (
        "current_turn",
        "remain_turn",
        "score",
        "stamina",
        "max_stamina",
        "block",
        "turn_card_play_count",
        "exam_card_play_count",
        "step_type_value",
    ):
        value = getattr(state, field, None)
        if isinstance(value, int) and not isinstance(value, bool):
            state_values[field] = value

    hand_rows: list[dict[str, Any]] = []
    zones = getattr(state, "zones", None)
    hand = getattr(zones, "hand", ())
    if isinstance(hand, (list, tuple)):
        for index, card in enumerate(hand):
            row: dict[str, Any] = {"hand_index": index}
            for source_name, target_name in (
                ("guid", "guid"),
                ("card_id", "card_id"),
                ("effective_upgrade", "effective_upgrade"),
            ):
                value = getattr(card, source_name, None)
                if isinstance(value, (str, int)) and not isinstance(value, bool):
                    row[target_name] = value
            runtime_state = getattr(card, "runtime_state", None)
            play_count = getattr(runtime_state, "play_count", None)
            if isinstance(play_count, int) and not isinstance(play_count, bool):
                row["play_count"] = play_count
            hand_rows.append(row)

    drinks: list[dict[str, str]] | None = None
    runtime = getattr(state, "root_runtime", None)
    opaque_value = getattr(getattr(runtime, "opaque_fields", None), "to_value", None)
    opaque = opaque_value() if callable(opaque_value) else None
    if isinstance(opaque, Mapping):
        raw_status = opaque.get("status")
        if isinstance(raw_status, Mapping):
            active = raw_status.get("_effectList")
            removed = raw_status.get("_removedEffectList")
            if isinstance(active, list):
                state_values["status_effect_count"] = len(active)
            if isinstance(removed, list):
                state_values["removed_status_effect_count"] = len(removed)
        raw_drinks = opaque.get("drinkList")
        if isinstance(raw_drinks, list):
            drinks = []
            for raw in raw_drinks:
                drink_id = raw.get("_id") if isinstance(raw, Mapping) else None
                if isinstance(drink_id, str) and drink_id:
                    drinks.append({"drink_id": drink_id})

    return {
        "state": state_values,
        "hand": hand_rows,
        "drinks": drinks,
        "state_update": {
            "timestamp": source_marker[1] / 1_000_000_000,
            "kind": "exam-save-mtime",
            "path": str(source),
        },
    }


def _exam_save_file_marker(source: Path) -> tuple[int, int]:
    """Return the fields that must remain stable across one exact decode."""

    stat = source.stat()
    return stat.st_size, stat.st_mtime_ns


def _monitor_sequence(
    value: object,
    *,
    fields: Sequence[str],
) -> list[dict[str, Any]] | None:
    """Copy a small display-only sequence without exposing live objects."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    rows: list[dict[str, Any]] = []
    for raw in value:
        if isinstance(raw, Mapping):
            row = {
                key: item
                for key in fields
                if key in raw
                if isinstance(
                    (item := raw.get(key)),
                    (str, int, float, bool),
                )
            }
            if row:
                rows.append(row)
        elif isinstance(raw, str) and raw:
            rows.append({"id": raw})
    return rows


def _monitor_surface_snapshot(
    surface: InitialRegularSurface,
) -> dict[str, Any]:
    """Project existing reader output into observational GUI telemetry.

    This projection performs no capture or recognition. Missing fields stay
    absent so the monitor can label them unavailable instead of guessing.
    """

    payload = surface.payload
    snapshot: dict[str, Any] = {
        "schema": "gkms.live-monitor.v1",
        "page": surface.page,
    }
    raw_capture = payload.get("capture")
    if isinstance(raw_capture, Mapping):
        snapshot["capture"] = {
            key: value
            for key in _MONITOR_CAPTURE_FIELDS
            if isinstance(
                (value := raw_capture.get(key)),
                (str, int, float, bool),
            )
        }
    raw_state_update = payload.get("state_update")
    if isinstance(raw_state_update, Mapping):
        snapshot["state_update"] = {
            key: value
            for key in ("timestamp", "kind", "path")
            if isinstance(
                (value := raw_state_update.get(key)),
                (str, int, float),
            )
        }

    raw_state = payload.get("state")
    outer_authority = payload.get("outer_authority")
    state = (
        raw_state
        if isinstance(raw_state, Mapping)
        else outer_authority
        if isinstance(outer_authority, Mapping)
        else payload
    )
    projected_state = {
        key: value
        for key in _MONITOR_STATE_FIELDS
        if isinstance((value := state.get(key)), (str, int, float, bool))
    }
    if projected_state:
        snapshot["state"] = projected_state

    hand = state.get("hand")
    if hand is None:
        hand = payload.get("hand")
    cards = _monitor_sequence(hand, fields=_MONITOR_CARD_FIELDS)
    if cards is not None:
        snapshot["cards"] = cards

    drinks = state.get("drinks")
    if drinks is None:
        drinks = state.get("drink_list")
    if drinks is None:
        drinks = payload.get("drinks")
    projected_drinks = _monitor_sequence(
        drinks,
        fields=("drink_id", "id", "display_name", "name", "legal", "slot"),
    )
    if projected_drinks is not None:
        snapshot["drinks"] = projected_drinks

    legal_actions = state.get("legal_actions")
    if legal_actions is None:
        legal_actions = payload.get("legal_actions")
    projected_legal = _monitor_sequence(
        legal_actions,
        fields=("action_id", "action", "label", "card_id", "drink_id", "kind"),
    )
    if projected_legal is None:
        projected_legal = _monitor_sequence(
            payload.get("options"),
            fields=("action", "attribute", "label", "card_id", "drink_id"),
        )
    if projected_legal is not None:
        snapshot["legal_actions"] = projected_legal
    return snapshot


class SurfaceReader(Protocol):
    def __call__(self, previous_page: str | None) -> InitialRegularSurface: ...


class ChoiceExecutor(Protocol):
    def __call__(
        self,
        action: SuggestedClick,
        *,
        page: str,
        target: str,
        payload: Mapping[str, Any],
    ) -> ChoiceExecutionResult | Mapping[str, Any]: ...


class SingleClickExecutor(Protocol):
    def __call__(
        self, action: SuggestedClick
    ) -> ClickExecutionResult | Mapping[str, Any]: ...


class ExamDispatcher(Protocol):
    def __call__(
        self, plan_type: str, exam_save_path: Path
    ) -> Mapping[str, Any]: ...


class InnerImitationExamRunner(Protocol):
    """Opt-in plan-neutral inner runner around the existing Maa baseline.

    The callback owns the per-step dependency injection (settled ExamSave,
    fresh ``ProduceRecognitionCards`` results, legal gate, imitation prior,
    and fixed-slot/CAS dispatcher).  It receives a zero-argument baseline
    callback and must call that callback exactly once when its one-way runtime
    abstains.  The default production path leaves this callback unset.
    """

    def __call__(
        self,
        plan_type: str,
        exam_save_path: Path,
        baseline_runner: Callable[[], Mapping[str, Any]],
    ) -> Mapping[str, Any]: ...


class RunContextBootstrapper(Protocol):
    def __call__(
        self,
        snapshot: ProduceOuterLocalSaveSnapshot,
        surface: InitialRegularSurface,
    ) -> Mapping[str, Any]: ...


OuterObserverAnchorProvider = Callable[[int, int], Mapping[str, Any]]


def _incomplete_outer_observer_anchor(
    code: str,
    error: BaseException | None = None,
) -> dict[str, Any]:
    issue: dict[str, Any] = {"code": code}
    if error is not None:
        issue["error_type"] = type(error).__name__
        issue["detail"] = str(error)
    return {
        "complete": False,
        "error_count": 1,
        "integration_errors": [issue],
    }


def _capture_outer_observer_anchor(
    provider: OuterObserverAnchorProvider | None,
    *,
    expected_generation: int,
    minimum_next: int,
) -> tuple[dict[str, Any], bool]:
    """Read one observer cursor without ever affecting live gameplay.

    The cursor is observational.  Provider, shape, and JSON-serialization
    failures therefore become an explicit incomplete anchor instead of
    escaping into the unattended control loop.
    """

    if provider is None:
        return {}, False
    try:
        raw = provider(expected_generation, minimum_next)
        if not isinstance(raw, Mapping):
            raise TypeError("outer observer anchor provider must return a mapping")
        # Receipts are persisted as JSON.  Round-trip here so a malformed
        # injected provider cannot make the later progress callback fail.
        anchor = json.loads(
            json.dumps(raw, ensure_ascii=False, allow_nan=False)
        )
        if not isinstance(anchor, dict):
            raise TypeError("outer observer anchor must encode as an object")
        return anchor, anchor.get("complete") is True
    except Exception as error:
        return _incomplete_outer_observer_anchor(
            "outer-observer-anchor-provider-error",
            error,
        ), False


def _terminal_outer_observer_anchor(
    provider: OuterObserverAnchorProvider | None,
    begin_anchor: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    if provider is None:
        return {}, False
    generation = begin_anchor.get("writer_generation")
    target = begin_anchor.get("target_next_enqueue_sequence")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
        or isinstance(target, bool)
        or not isinstance(target, int)
        or target < 1
    ):
        return _incomplete_outer_observer_anchor(
            "outer-observer-terminal-token-missing"
        ), False
    return _capture_outer_observer_anchor(
        provider,
        expected_generation=generation,
        minimum_next=target + 1,
    )


Plan2EvidenceLoader = Callable[[Path], Any]
Plan2DecisionOrchestratorFactory = Callable[[InitialRegularPlan2ExamContext], Any]
Plan2MaaDependenciesFactory = Callable[
    [Path, InitialRegularPlan2ExamContext, Plan2EvidenceLoader, Any], Any
]
Plan2ActionExecutorFactory = Callable[[Any], Any]
Plan2BridgeFactory = Callable[..., Any]
Plan2LoopRunner = Callable[..., Any]
Plan2OuterSnapshotReader = Callable[[Path], ProduceOuterLocalSaveSnapshot]


@dataclass(frozen=True, slots=True)
class InitialRegularPlan2ExamDependencies:
    """Production composition seams for one Plan2 unattended exam."""

    evidence_loader: Plan2EvidenceLoader
    decision_orchestrator_factory: Plan2DecisionOrchestratorFactory
    maa_dependencies_factory: Plan2MaaDependenciesFactory
    action_executor_factory: Plan2ActionExecutorFactory
    bridge_factory: Plan2BridgeFactory
    loop_runner: Plan2LoopRunner
    replay_journal_root: Path | None = None
    outer_snapshot_reader: Plan2OuterSnapshotReader | None = None

    def __post_init__(self) -> None:
        for name in (
            "evidence_loader",
            "decision_orchestrator_factory",
            "maa_dependencies_factory",
            "action_executor_factory",
            "bridge_factory",
            "loop_runner",
        ):
            if not callable(getattr(self, name)):
                raise TypeError(f"{name} must be callable")
        if self.outer_snapshot_reader is not None and not callable(
            self.outer_snapshot_reader
        ):
            raise TypeError("outer_snapshot_reader must be callable or None")


@dataclass(frozen=True, slots=True)
class InitialRegularLiveAutopilotDependencies:
    """Offline-testable boundaries for the production convenience entrypoint.

    Supplying this object replaces only the live page, click, checkpoint, and
    optional exam-composition boundaries.  When ``exam_dispatcher`` is omitted,
    the production Plan2/Plan3/N.I.A. dispatcher remains in control.
    """

    surface_reader: SurfaceReader
    snapshot_reader: Callable[[], ProduceOuterLocalSaveSnapshot]
    outer_advisor: Callable[..., OuterAdvice]
    choice_executor: ChoiceExecutor
    single_click_executor: SingleClickExecutor
    plan2_exam_dependencies: InitialRegularPlan2ExamDependencies | None = None
    exam_dispatcher: ExamDispatcher | None = None
    # Explicitly opt-in only.  The callback is the host-owned bridge to the
    # generic inner runtime; leaving it unset preserves the existing exam
    # dispatcher byte-for-byte.
    inner_imitation_enabled: bool = INNER_IMITATION_RUNTIME_ENABLED_DEFAULT
    inner_imitation_runner: InnerImitationExamRunner | None = None
    lesson_result_checkpointer: (
        Callable[[Mapping[str, Any]], Mapping[str, Any]] | None
    ) = None
    activity_reward_checkpointer: (
        Callable[[Mapping[str, Any]], Mapping[str, Any]] | None
    ) = None
    training_reward_checkpointer: (
        Callable[[Mapping[str, Any]], Mapping[str, Any]] | None
    ) = None
    reward_checkpointer: Callable[..., Mapping[str, Any]] | None = None
    run_context_bootstrapper: RunContextBootstrapper | None = None
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        for name in (
            "surface_reader",
            "snapshot_reader",
            "outer_advisor",
            "choice_executor",
            "single_click_executor",
            "sleep",
        ):
            if not callable(getattr(self, name)):
                raise TypeError(f"{name} must be callable")
        if self.plan2_exam_dependencies is not None and not isinstance(
            self.plan2_exam_dependencies,
            InitialRegularPlan2ExamDependencies,
        ):
            raise TypeError("plan2_exam_dependencies must be typed or None")
        if self.exam_dispatcher is not None and not callable(self.exam_dispatcher):
            raise TypeError("exam_dispatcher must be callable or None")
        if type(self.inner_imitation_enabled) is not bool:
            raise TypeError("inner_imitation_enabled must be bool")
        if self.inner_imitation_runner is not None and not callable(
            self.inner_imitation_runner
        ):
            raise TypeError("inner_imitation_runner must be callable or None")
        for name in (
            "lesson_result_checkpointer",
            "activity_reward_checkpointer",
            "training_reward_checkpointer",
            "reward_checkpointer",
            "run_context_bootstrapper",
        ):
            value = getattr(self, name)
            if value is not None and not callable(value):
                raise TypeError(f"{name} must be callable or None")


def _capture_mapping(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    capture = payload.get("capture")
    if not isinstance(capture, Mapping):
        raise ValueError("live reader payload is missing capture evidence")
    return capture


def _bound_action(
    payload: Mapping[str, Any],
    *,
    label: str,
    box: tuple[int, int, int, int],
    click_count: int,
) -> SuggestedClick:
    capture = _capture_mapping(payload)
    left, top, right, bottom = box
    return SuggestedClick(
        label=label,
        canonical_x=(left + right) // 2,
        canonical_y=(top + bottom) // 2,
        source_png_path=str(capture["png_path"]),
        source_timestamp=float(capture["timestamp"]),
        source_hwnd=int(capture["hwnd"]),
        source_pid=int(capture["pid"]),
        verification_box=box,
        click_count=click_count,
    )


def _tuple_box(value: object, label: str) -> tuple[int, int, int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"{label} must contain four coordinates")
    return tuple(int(item) for item in value)  # type: ignore[return-value]


def _overview_action(
    payload: Mapping[str, Any], advice: OuterAdvice
) -> tuple[SuggestedClick, str]:
    options = payload.get("options")
    if not isinstance(options, list):
        raise ValueError("overview options are missing")
    matches = [
        value
        for value in options
        if isinstance(value, Mapping) and value.get("action") == advice.action
    ]
    if len(matches) != 1:
        raise ValueError("outer advice does not identify exactly one visible option")
    option = matches[0]
    target = str(option["action"])
    route = payload.get("route")
    is_nia = bool(
        isinstance(route, Mapping)
        and route.get("produce_id") in {"produce-004", "produce-005"}
    )
    return (
        _bound_action(
            payload,
            label=(
                f"nia-overview:{target}"
                if is_nia
                else f"initial-regular-overview:{target}"
            ),
            box=_tuple_box(option.get("canonical_box"), "overview option box"),
            click_count=1 if target == REST else 2,
        ),
        target,
    )


def _training_action(
    payload: Mapping[str, Any], advice: OuterAdvice
) -> tuple[SuggestedClick, str]:
    if advice.attribute not in {"Vo", "Da", "Vi"}:
        raise ValueError("training advice has no exact Vo/Da/Vi target")
    state = payload.get("state")
    if not isinstance(state, Mapping) or not isinstance(state.get("options"), list):
        raise ValueError("training options are missing")
    matches = [
        value
        for value in state["options"]
        if isinstance(value, Mapping) and value.get("attribute") == advice.attribute
    ]
    if len(matches) != 1:
        raise ValueError("training advice does not identify exactly one visible option")
    option = matches[0]
    target = str(advice.attribute)
    return (
        _bound_action(
            payload,
            label=f"initial-regular-training:{target}",
            box=_tuple_box(option.get("canonical_box"), "training option box"),
            click_count=2,
        ),
        target,
    )


def _overview_preview_analyzer(
    payload: Mapping[str, Any], action: SuggestedClick, target: str
) -> Callable[[Path], ChoicePreviewAnalysis]:
    from difflib import SequenceMatcher

    import numpy as np

    from .overview_actions import detect_weekly_actions
    from .screen_state import ParameterTemporalEvidence, read_overview_path
    from .live_source import _live_text_recognizer

    source = Path(action.source_png_path)
    route = payload.get("route")
    produce_id = (
        str(route.get("produce_id"))
        if isinstance(route, Mapping) and isinstance(route.get("produce_id"), str)
        else "produce-001"
    )
    source_state = payload.get("state")
    temporal = None
    if produce_id != "produce-001" and isinstance(source_state, Mapping):
        attributes = {
            field: source_state.get(field)
            for field in ("vocal", "dance", "visual")
        }
        if all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in attributes.values()
        ):
            temporal = ParameterTemporalEvidence(
                before={field: int(value) for field, value in attributes.items()},
                expected_deltas={"vocal": 0, "dance": 0, "visual": 0},
            )

    def analyze(path: Path) -> ChoicePreviewAnalysis:
        state = read_overview_path(
            path,
            produce_id=produce_id,
            parameter_temporal_evidence=temporal,
        )
        with Image.open(path.resolve()) as image:
            image.load()
            options = detect_weekly_actions(image)
        matches = [value for value in options if value.action == target]
        option = matches[0] if len(matches) == 1 else None
        difference = compare_frame_paths(
            source, path, canonical_box=action.verification_box
        )
        select_proof = None
        selected_preview_proof = False
        preview_targets = {
            "class",
            "vocal_lesson",
            "dance_lesson",
            "visual_lesson",
        }
        if option is None and target in preview_targets:
            # Selected overview tiles grow/shift and the game dims their icon,
            # so the neutral MAA template can disappear for both Class and
            # lesson tiles.  Bind this fallback to the already-authorized
            # source box, demand the fresh orange selection frame, and require
            # a SELECT-like OCR marker immediately beneath it.  We do not
            # lower the global action-template threshold.  The surrounding
            # week/stamina/attribute state is parsed independently, and two
            # identical semantic samples remain mandatory upstream.
            left, top, right, bottom = action.verification_box
            select_box = (
                max(0, left - 15),
                max(0, bottom - 20),
                min(720, right + 15),
                min(1280, bottom + 105),
            )
            select_proof = _live_text_recognizer().recognize_path(path, select_box)
            normalized_select = "".join(
                value for value in select_proof.text.upper() if value.isalpha()
            )
            select_similarity = SequenceMatcher(
                None, normalized_select, "SELECT"
            ).ratio()
            with Image.open(path.resolve()) as selected_image:
                selected_rgb = np.asarray(selected_image.convert("RGB"), dtype=np.float32) / 255.0
            ring_left = max(0, left - 18)
            ring_top = max(0, top - 20)
            ring_right = min(720, right + 18)
            ring_bottom = min(1280, bottom + 82)
            ring = selected_rgb[ring_top:ring_bottom, ring_left:ring_right]
            red, green, blue = ring[:, :, 0], ring[:, :, 1], ring[:, :, 2]
            orange_density = float(
                np.mean(
                    (red > 0.85)
                    & (green > 0.30)
                    & (green < 0.80)
                    & (blue < 0.35)
                )
            )
            if (
                select_similarity < 0.65
                or select_proof.confidence < 0.50
                or orange_density < 0.015
            ):
                raise ValueError("overview preview lost the intended target")
            selected_preview_proof = True
        elif option is not None and target in preview_targets:
            # During the first few animation frames the shifted/dimmed tile
            # can still weakly match its neutral template.  Prefer the same
            # target-bound selection proof whenever it is visibly present so
            # the semantic key does not oscillate between template/fallback.
            left, top, right, bottom = action.verification_box
            select_box = (
                max(0, left - 15),
                max(0, bottom - 20),
                min(720, right + 15),
                min(1280, bottom + 105),
            )
            candidate_proof = _live_text_recognizer().recognize_path(path, select_box)
            normalized_select = "".join(
                value for value in candidate_proof.text.upper() if value.isalpha()
            )
            select_similarity = SequenceMatcher(
                None, normalized_select, "SELECT"
            ).ratio()
            with Image.open(path.resolve()) as selected_image:
                selected_rgb = np.asarray(selected_image.convert("RGB"), dtype=np.float32) / 255.0
            ring = selected_rgb[
                max(0, top - 20) : min(1280, bottom + 82),
                max(0, left - 18) : min(720, right + 18),
            ]
            red, green, blue = ring[:, :, 0], ring[:, :, 1], ring[:, :, 2]
            orange_density = float(
                np.mean(
                    (red > 0.85)
                    & (green > 0.30)
                    & (green < 0.80)
                    & (blue < 0.35)
                )
            )
            if (
                select_similarity >= 0.65
                and candidate_proof.confidence >= 0.50
                and orange_density >= 0.015
            ):
                select_proof = candidate_proof
                selected_preview_proof = True
        elif option is None:
            raise ValueError("overview preview lost the intended target")
        highlighted = bool(
            (difference.mean_absolute >= 0.012 and difference.changed_fraction >= 0.04)
            or selected_preview_proof
        )
        option_box = (
            action.verification_box
            if selected_preview_proof or option is None
            else option.canonical_box
        )
        option_confidence = (
            # OCR similarity/confidence and the bound orange frame are all
            # hard gates above.  Once all three pass, confidence represents
            # their combined target-bound proof rather than the weakest OCR
            # character alone (the small game font commonly reads SELECT as
            # VELCET while retaining the exact six-glyph topology).
            min(
                0.99,
                0.86 + min(0.13, max(0.0, orange_density - 0.015) * 4.0),
            )
            if selected_preview_proof and select_proof is not None
            else float(option.match_score)
        )
        return ChoicePreviewAnalysis(
            page=PAGE_OVERVIEW,
            target_key=target,
            highlighted=highlighted,
            semantic_key=(
                state.weeks_remaining,
                state.stamina,
                state.max_stamina,
                state.produce_points,
                state.vocal,
                state.dance,
                state.visual,
                state.countdown_target,
                (
                    ("selected", target)
                    if selected_preview_proof
                    else tuple(value.action for value in options)
                ),
                option_box,
                "select-ocr" if selected_preview_proof else "tile-detection",
            ),
            confidence=min(float(state.confidence), option_confidence),
        )

    return analyze


def _training_preview_analyzer(
    target: str,
) -> Callable[[Path], ChoicePreviewAnalysis]:
    from .training_choice import analyze_training_choice_path

    def analyze(path: Path) -> ChoicePreviewAnalysis:
        state = analyze_training_choice_path(path)
        matches = [value for value in state.options if value.attribute == target]
        if len(matches) != 1:
            raise ValueError("training preview lost the intended target")
        option = matches[0]
        return ChoicePreviewAnalysis(
            page=PAGE_TRAINING,
            target_key=target,
            highlighted=bool(option.highlighted),
            semantic_key=(
                target,
                tuple(
                    (value.attribute, value.highlighted) for value in state.options
                ),
            ),
            confidence=float(option.match_score),
        )

    return analyze


def execute_initial_regular_choice(
    action: SuggestedClick,
    *,
    page: str,
    target: str,
    payload: Mapping[str, Any],
) -> ChoiceExecutionResult | Mapping[str, Any]:
    """Execute one Initial choice through Maa background input.

    Weekly overview tiles use the game's native two-click SELECT contract.
    The source Maa frame already identified the tile; after the first click
    the neutral tile deliberately changes into a highlighted preview, so
    re-running the neutral recognizer before the confirmation click only
    rejects normal UI state.  Send the declared two clicks to the same bound
    target and let the following Produce LocalSave/surface read confirm the
    transition.  Training choices retain their dedicated preview analyzer.
    """

    if page == PAGE_OVERVIEW:
        if action.click_count != 2:
            raise ValueError("Initial overview choices require two Maa clicks")
        from .controller_client import send_command
        from .live_actions import (
            StaleSuggestionError,
            _capture_path,
            _require_bound_capture,
            canonical_to_outer_window,
        )

        status = dict(send_command("status"))
        if not bool(status.get("background_control")):
            raise RuntimeError("Initial overview requires Maa background control")
        if int(status.get("target_hwnd", 0)) != action.source_hwnd:
            raise StaleSuggestionError("controller is bound to a different game window")
        if int(status.get("target_pid", 0)) != action.source_pid:
            raise StaleSuggestionError("controller is bound to a different game process")
        geometry = status.get("geometry")
        if not isinstance(geometry, Mapping):
            raise RuntimeError("controller status is missing window geometry")
        raw_capture = payload.get("capture")
        source_capture = (
            dict(raw_capture)
            if isinstance(raw_capture, Mapping)
            else {
                "png_path": action.source_png_path,
                "timestamp": action.source_timestamp,
                "hwnd": action.source_hwnd,
                "pid": action.source_pid,
            }
        )
        _require_bound_capture(source_capture, action)
        _capture_path(source_capture)
        window_x, window_y = canonical_to_outer_window(
            action.canonical_x,
            action.canonical_y,
            geometry,
        )
        clicks: list[Mapping[str, Any]] = []
        for index in range(2):
            clicks.append(
                dict(
                    send_command(
                        "send_input_click_once",
                        window_x=window_x,
                        window_y=window_y,
                    )
                )
            )
            if index == 0:
                time.sleep(0.3)
        time.sleep(0.65)
        post_capture = dict(send_command("capture_screen_once", timeout=15.0))
        _require_bound_capture(post_capture, action)
        return {
            "action_label": action.label,
            "target": target,
            "window_x": window_x,
            "window_y": window_y,
            "source_capture": source_capture,
            "post_capture": post_capture,
            "click_results": [dict(value) for value in clicks],
            "confirmation": "next-surface-or-produce-local-save",
        }
    if page != PAGE_TRAINING:
        raise ValueError(f"two-stage choices are unsupported on {page}")
    return execute_verified_choice_click(
        action,
        _training_preview_analyzer(target),
        expected_page=page,
        expected_target=target,
    )


def execute_nia_outer_choice(
    action: SuggestedClick,
    *,
    page: str,
    target: str,
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Double-click one N.I.A. overview tile through MAA background input.

    N.I.A. does not reuse Initial's highlighted-preview/OCR confirmation.  Maa
    already models these overview actions as a direct double-click.  The
    action remains bound to the freshly detected tile and game process; the
    following surface read and Produce LocalSave transition are the semantic
    confirmation that the route advanced.
    """

    from .controller_client import send_command
    from .live_actions import (
        StaleSuggestionError,
        _capture_path,
        _require_bound_capture,
        canonical_to_outer_window,
    )

    if page != PAGE_OVERVIEW or not action.label.startswith("nia-overview:"):
        raise ValueError("N.I.A. outer choice requires a bound overview action")
    if action.click_count != 2 or target != action.label.split(":", 1)[1]:
        raise ValueError("N.I.A. outer choice target binding is inconsistent")
    authority = payload.get("outer_authority")
    if not isinstance(authority, Mapping):
        raise ValueError("N.I.A. outer choice is missing LocalSave authority")

    status = dict(send_command("status"))
    if not bool(status.get("background_control")):
        raise RuntimeError("N.I.A. execution requires MAA background control")
    if int(status.get("target_hwnd", 0)) != action.source_hwnd:
        raise StaleSuggestionError("controller is bound to a different game window")
    if int(status.get("target_pid", 0)) != action.source_pid:
        raise StaleSuggestionError("controller is bound to a different game process")
    geometry = status.get("geometry")
    if not isinstance(geometry, Mapping):
        raise RuntimeError("controller status is missing window geometry")

    raw_source_capture = payload.get("capture")
    pre_capture = (
        dict(raw_source_capture)
        if isinstance(raw_source_capture, Mapping)
        else {
            "png_path": action.source_png_path,
            "timestamp": action.source_timestamp,
            "hwnd": action.source_hwnd,
            "pid": action.source_pid,
        }
    )
    # Reuse the same Maa frame which produced the suggestion.  Capturing a
    # second identical frame here only rechecked PID/HWND and added no state
    # authority beyond the controller binding + LocalSave CAS below.
    _require_bound_capture(pre_capture, action)
    _capture_path(pre_capture)
    # A current LocalSave compare-and-set is the one semantic pre-input gate.
    from .produce_outer_local_save import read_current_produce_outer_local_save

    current = read_current_produce_outer_local_save()
    current_week = current.latest_week_marker
    if (
        current_week is not None
        and current.last_completed_week == current_week
    ):
        current_week += 1
    expected = (
        authority.get("log_count"),
        authority.get("week"),
        authority.get("last_completed_week"),
        authority.get("stamina"),
        authority.get("max_stamina"),
        authority.get("produce_points"),
        authority.get("vocal"),
        authority.get("dance"),
        authority.get("visual"),
        authority.get("vote_count"),
    )
    actual = (
        current.log_count,
        current_week,
        current.last_completed_week,
        current.stamina,
        current.max_stamina,
        current.produce_points,
        current.vocal,
        current.dance,
        current.visual,
        current.vote_count,
    )
    if actual != expected:
        raise StaleSuggestionError("N.I.A. Produce LocalSave changed before input")

    window_x, window_y = canonical_to_outer_window(
        action.canonical_x,
        action.canonical_y,
        geometry,
    )
    clicks = []
    clicks.append(
        dict(
            send_command(
                "send_input_click_once",
                window_x=window_x,
                window_y=window_y,
            )
        )
    )
    time.sleep(0.5)
    clicks.append(
        dict(
            send_command(
                "send_input_click_once",
                window_x=window_x,
                window_y=window_y,
            )
        )
    )
    time.sleep(0.65)
    post_capture = dict(send_command("capture_screen_once", timeout=15.0))
    _require_bound_capture(post_capture, action)
    return {
        "action_label": action.label,
        "expected_page": page,
        "expected_target": target,
        "window_x": window_x,
        "window_y": window_y,
        "pre_capture": pre_capture,
        "post_capture": post_capture,
        "click_results": clicks,
        "authority": dict(authority),
        "confirmation": "next-surface-and-produce-local-save",
        "pre_png_path": _capture_path(pre_capture),
    }


def execute_nia_outer_subpage_click(
    action: SuggestedClick,
    *,
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Execute one Maa-recognized N.I.A. subpage button.

    This is intentionally a single input.  Confirmation is the next typed
    subpage or a changed Produce LocalSave, not another colour/OCR proof of the
    same button.
    """

    if not action.label.startswith("nia-subpage:") or action.click_count not in {1, 2}:
        raise ValueError("N.I.A. subpage action binding is invalid")
    authority = payload.get("outer_authority")
    if not isinstance(authority, Mapping):
        raise ValueError("N.I.A. subpage is missing LocalSave authority")
    from .produce_outer_local_save import read_current_produce_outer_local_save

    current = read_current_produce_outer_local_save()
    if (
        current.log_count,
        current.latest_week_marker,
        current.last_completed_week,
        current.stamina,
        current.max_stamina,
        current.produce_points,
        current.vocal,
        current.dance,
        current.visual,
        current.vote_count,
    ) != (
        authority.get("log_count"),
        authority.get("week"),
        authority.get("last_completed_week"),
        authority.get("stamina"),
        authority.get("max_stamina"),
        authority.get("produce_points"),
        authority.get("vocal"),
        authority.get("dance"),
        authority.get("visual"),
        authority.get("vote_count"),
    ):
        # The game can settle an animation and write LocalSave between the
        # capture and this input.  That is successful forward progress, so do
        # not click the now-stale page and do not turn it into a hard stop;
        # simply let the outer loop read the new state/screen.
        return {
            "skipped": True,
            "reason": "produce-local-save-advanced-before-subpage-input",
            "authority": dict(authority),
            "current_authority": {
                "log_count": current.log_count,
                "week": current.latest_week_marker,
                "last_completed_week": current.last_completed_week,
                "stamina": current.stamina,
                "max_stamina": current.max_stamina,
                "produce_points": current.produce_points,
                "vocal": current.vocal,
                "dance": current.dance,
                "visual": current.visual,
                "vote_count": current.vote_count,
            },
            "confirmation": "outer-state-already-advanced; no-input-submitted",
        }
    # N.I.A. subpages use Maa's own fixed single/double-click semantics.  The
    # one semantic pre-input gate is the Produce LocalSave CAS above.  Do not
    # layer generic pixel stale/reconciliation checks over the same action.
    from .controller_client import send_command
    from .live_actions import (
        _capture_path,
        _require_bound_capture,
        canonical_to_outer_window,
    )

    status = dict(send_command("status"))
    if not bool(status.get("background_control")):
        raise RuntimeError("N.I.A. subpage execution requires MAA background control")
    if int(status.get("target_hwnd", 0)) != action.source_hwnd:
        from .live_actions import StaleSuggestionError

        raise StaleSuggestionError("controller is bound to a different game window")
    if int(status.get("target_pid", 0)) != action.source_pid:
        from .live_actions import StaleSuggestionError

        raise StaleSuggestionError("controller is bound to a different game process")
    geometry = status.get("geometry")
    if not isinstance(geometry, Mapping):
        raise RuntimeError("controller status is missing window geometry")
    raw_source_capture = payload.get("capture")
    pre_capture = (
        dict(raw_source_capture)
        if isinstance(raw_source_capture, Mapping)
        else {
            "png_path": action.source_png_path,
            "timestamp": action.source_timestamp,
            "hwnd": action.source_hwnd,
            "pid": action.source_pid,
        }
    )
    _require_bound_capture(pre_capture, action)
    _capture_path(pre_capture)
    window_x, window_y = canonical_to_outer_window(
        action.canonical_x,
        action.canonical_y,
        geometry,
    )
    clicks: list[Mapping[str, Any]] = []
    for index in range(action.click_count):
        try:
            clicks.append(
                dict(
                    send_command(
                        "send_input_click_once",
                        window_x=window_x,
                        window_y=window_y,
                    )
                )
            )
        except RuntimeError as exc:
            detail = str(exc)
            if not (
                "MaaFramework 背景點擊失敗" in detail
                or "MaaFramework background click failed" in detail
            ):
                raise
            # Maa's Win32 backend can report a one-off input job failure even
            # while the bound controller and LocalSave remain healthy.  Do not
            # guess whether the OS delivered part of that job and do not issue
            # an immediate double-click.  The outer loop will read the current
            # page and Produce LocalSave again; it then either observes forward
            # progress or safely retries the same typed action.
            return {
                "skipped": True,
                "reason": "maa-background-click-job-failed",
                "detail": detail,
                "attempted_click_index": index,
                "authority": dict(authority),
                "confirmation": "re-read-page-and-produce-local-save",
            }
        if index + 1 < action.click_count:
            time.sleep(0.3)
    time.sleep(0.65)
    post_capture = dict(send_command("capture_screen_once", timeout=15.0))
    _require_bound_capture(post_capture, action)
    return {
        "click": {
            "action_label": action.label,
            "window_x": window_x,
            "window_y": window_y,
            "pre_capture": pre_capture,
            "post_capture": post_capture,
            "click_results": [dict(value) for value in clicks],
            "pre_png_path": _capture_path(pre_capture),
        },
        "authority": dict(authority),
        "confirmation": "next-subpage-or-produce-local-save",
    }


def execute_nia_outer_subpage_swipe(
    action: SuggestedSwipe,
    *,
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Execute one explicit N.I.A. capacity-page recovery swipe.

    The action is proposed by the drink surface reader/ranker and executed
    here, after the same Produce LocalSave CAS and Maa window binding used by
    subpage clicks.  A fresh capture is returned as evidence; the outer loop
    re-enters the reader on the next cycle and never reuses the clipped frame.
    """

    if (
        not action.label.startswith("nia-subpage:drink-keep-scroll-")
        or action.action_type != "swipe"
        or (
            action.canonical_x1,
            action.canonical_y1,
            action.canonical_x2,
            action.canonical_y2,
            action.duration_ms,
        )
        != (360, 900, 360, 730, 400)
    ):
        raise ValueError("N.I.A. drink scroll action binding is invalid")
    authority = payload.get("outer_authority")
    if not isinstance(authority, Mapping):
        raise ValueError("N.I.A. drink scroll is missing LocalSave authority")
    from .produce_outer_local_save import read_current_produce_outer_local_save

    current = read_current_produce_outer_local_save()
    authority_key = (
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
    current_values = (
        current.log_count,
        current.latest_week_marker,
        current.last_completed_week,
        current.stamina,
        current.max_stamina,
        current.produce_points,
        current.vocal,
        current.dance,
        current.visual,
        current.vote_count,
    )
    if current_values != tuple(authority.get(key) for key in authority_key):
        return {
            "skipped": True,
            "reason": "produce-local-save-advanced-before-drink-scroll",
            "authority": dict(authority),
            "confirmation": "outer-state-already-advanced; no-input-submitted",
        }
    from .controller_client import send_command
    from .live_actions import _capture_path, _require_bound_capture, canonical_to_outer_window

    status = dict(send_command("status"))
    if not bool(status.get("background_control")):
        raise RuntimeError("N.I.A. drink scroll requires MAA background control")
    if int(status.get("target_hwnd", 0)) != action.source_hwnd:
        raise StaleSuggestionError("controller is bound to a different game window")
    if int(status.get("target_pid", 0)) != action.source_pid:
        raise StaleSuggestionError("controller is bound to a different game process")
    geometry = status.get("geometry")
    if not isinstance(geometry, Mapping):
        raise RuntimeError("controller status is missing window geometry")
    raw_source_capture = payload.get("capture")
    pre_capture = (
        dict(raw_source_capture)
        if isinstance(raw_source_capture, Mapping)
        else {
            "png_path": action.source_png_path,
            "timestamp": action.source_timestamp,
            "hwnd": action.source_hwnd,
            "pid": action.source_pid,
        }
    )
    _require_bound_capture(pre_capture, action)
    _capture_path(pre_capture)
    window_x1, window_y1 = canonical_to_outer_window(
        action.canonical_x1, action.canonical_y1, geometry
    )
    window_x2, window_y2 = canonical_to_outer_window(
        action.canonical_x2, action.canonical_y2, geometry
    )
    swipe = dict(
        send_command(
            "send_input_swipe_once",
            # MaaWin32Session.swipe consumes canonical 720x1280 client
            # coordinates.  Keep the converted outer-window points only as
            # telemetry; sending them here would double-scale the gesture.
            x1=action.canonical_x1,
            y1=action.canonical_y1,
            x2=action.canonical_x2,
            y2=action.canonical_y2,
            duration_ms=action.duration_ms,
        )
    )
    time.sleep(0.65)
    post_capture = dict(send_command("capture_screen_once", timeout=15.0))
    _require_bound_capture(post_capture, action)
    return {
        "swipe": {
            "action_label": action.label,
            "window_x1": window_x1,
            "window_y1": window_y1,
            "window_x2": window_x2,
            "window_y2": window_y2,
            "duration_ms": action.duration_ms,
            "pre_capture": pre_capture,
            "post_capture": post_capture,
            "swipe_result": swipe,
        },
        "authority": dict(authority),
        "input_submitted": True,
        "confirmation": "fresh-capture-and-re-rank",
    }


def _prove_nia_active_produce_resume_authority(
    snapshot: Mapping[str, Any],
    *,
    active_run: Mapping[str, Any],
    run_shadow: Mapping[str, Any],
    expected_run_id: str,
    expected_idol_card_id: str,
    expected_produce_id: str,
    expected_plan_type: str,
) -> Mapping[str, Any]:
    """Bind a global-Home resume to one already-active N.I.A. run.

    The Home template proves only which Maa route is visible.  It is not run
    identity.  This proof therefore joins the immutable active-run manifest,
    its durable shadow, and the still-in-progress Produce LocalSave before the
    mutating Maa continuation route is exposed to the runner.
    """

    expected_identity = {
        "run_id": expected_run_id,
        "idol_card_id": expected_idol_card_id,
        "produce_id": expected_produce_id,
    }
    if any(not isinstance(value, str) or not value for value in expected_identity.values()):
        raise ValueError("active Produce resume expected identity is incomplete")
    if expected_produce_id not in {"produce-004", "produce-005"}:
        raise ValueError("active Produce resume is N.I.A.-only")
    if not isinstance(expected_plan_type, str) or not expected_plan_type:
        raise ValueError("active Produce resume expected plan is incomplete")
    if any(active_run.get(name) != value for name, value in expected_identity.items()):
        raise ValueError("active Produce resume run identity mismatch")
    character_id = active_run.get("character_id")
    if not isinstance(character_id, str) or not character_id:
        raise ValueError("active Produce resume character identity is incomplete")
    if active_run.get("plan_type") != expected_plan_type:
        raise ValueError("active Produce resume plan identity mismatch")
    if any(
        run_shadow.get(name) != value
        for name, value in {
            "idol_card_id": expected_idol_card_id,
            "produce_id": expected_produce_id,
            "character_id": character_id,
        }.items()
    ):
        raise ValueError("active Produce resume shadow identity mismatch")

    lifecycle = snapshot.get("lifecycle")
    if not isinstance(lifecycle, Mapping):
        raise ValueError("active Produce resume lifecycle is unavailable")
    if lifecycle.get("is_in_progress") is not True or lifecycle.get("is_end_live") is not False:
        raise ValueError("active Produce resume lifecycle is not resumable")
    if snapshot.get("produce_type") != 2 or snapshot.get("produce_type_name") != "next_idol_audition":
        raise ValueError("active Produce resume mode is not N.I.A.")

    week = snapshot.get("week")
    completed_week = snapshot.get("last_completed_week")
    final_week = nia_final_week(expected_produce_id)
    next_route_week = week + 1 if type(week) is int else None
    weeks_remaining = (
        final_week - next_route_week
        if type(next_route_week) is int
        else None
    )
    stage_positions = nia_stage_positions(expected_produce_id)
    if (
        type(week) is not int
        or not 1 <= week < final_week
        or completed_week != week
        or run_shadow.get("route_week") != next_route_week
        or run_shadow.get("weeks_remaining") != weeks_remaining
    ):
        raise ValueError("active Produce resume route boundary mismatch")
    expected_stage_flags = {
        "is_started_audition_mid1": week >= 9,
        "is_started_audition_mid2": week >= stage_positions["exam-step:17"][0],
        "is_started_audition_final": False,
    }
    if any(lifecycle.get(name) is not value for name, value in expected_stage_flags.items()):
        raise ValueError("active Produce resume audition lifecycle mismatch")

    state_fields = ("stamina", "produce_points", "vocal", "dance", "visual")
    state: dict[str, int] = {}
    for name in state_fields:
        value = snapshot.get(name)
        if type(value) is not int or value < 0 or run_shadow.get(name) != value:
            raise ValueError(f"active Produce resume settled state mismatch: {name}")
        state[name] = value
    max_stamina = run_shadow.get("max_stamina")
    if type(max_stamina) is not int or max_stamina < 1 or state["stamina"] > max_stamina:
        raise ValueError("active Produce resume shadow max stamina is invalid")
    observed_max_stamina = snapshot.get("max_stamina")
    if observed_max_stamina is not None and observed_max_stamina != max_stamina:
        raise ValueError("active Produce resume max stamina mismatch")
    log_count = snapshot.get("log_count")
    if type(log_count) is not int or log_count < 1:
        raise ValueError("active Produce resume log authority is invalid")
    vote_count = snapshot.get("vote_count")
    if type(vote_count) is not int or vote_count < 0:
        raise ValueError("active Produce resume vote authority is invalid")
    state["vote_count"] = vote_count
    schedule_number = lifecycle.get("schedule_environment_drawing_number")
    schedule_type = lifecycle.get("schedule_environment_type")
    if (
        type(schedule_number) is not int
        or schedule_number < 0
        or type(schedule_type) is not int
        or schedule_type < 0
    ):
        raise ValueError("active Produce resume schedule authority is invalid")

    return {
        "kind": "durable-same-run-home-resume",
        "run_id": expected_run_id,
        "idol_card_id": expected_idol_card_id,
        "character_id": character_id,
        "produce_id": expected_produce_id,
        "plan_type": expected_plan_type,
        "log_count": log_count,
        "last_completed_week": week,
        "next_route_week": next_route_week,
        "weeks_remaining": weeks_remaining,
        "state": state,
        "max_stamina": max_stamina,
        "vote_count": vote_count,
        "schedule_environment_drawing_number": schedule_number,
        "schedule_environment_type": schedule_type,
        "policy": "active-run+shadow+in-progress-localsave+maa-home-active-v1",
    }


def _resolve_nia_active_produce_resume_authority(
    snapshot: ProduceOuterLocalSaveSnapshot,
    *,
    expected_run_id: str | None,
    expected_idol_card_id: str,
    expected_produce_id: str,
    expected_plan_type: str,
) -> Mapping[str, Any]:
    """Load the durable same-run records and return one exact resume proof."""

    if not isinstance(expected_run_id, str) or not expected_run_id:
        raise ValueError("active Produce resume requires expected_run_id")
    from .master_db import get_idol_profile
    from .run_identity import load_active_run, paths_for
    from .run_shadow import load_run_shadow

    active = load_active_run()
    if active is None:
        raise ValueError("active Produce resume has no active run")
    shadow = load_run_shadow(paths_for(active).shadow)
    if shadow is None:
        raise ValueError("active Produce resume has no run shadow")
    profile = get_idol_profile(active.idol_card_id)
    if profile is None or profile.character_id != active.character_id:
        raise ValueError("active Produce resume Master identity is unavailable")
    active_payload = active.to_dict()
    active_payload["plan_type"] = profile.plan_type
    lifecycle = snapshot.lifecycle
    snapshot_payload = {
        "log_count": snapshot.log_count,
        "week": snapshot.latest_week_marker,
        "last_completed_week": snapshot.last_completed_week,
        "produce_type": snapshot.produce_type,
        "produce_type_name": snapshot.produce_type_name,
        "stamina": snapshot.stamina,
        "max_stamina": snapshot.max_stamina,
        "produce_points": snapshot.produce_points,
        "vocal": snapshot.vocal,
        "dance": snapshot.dance,
        "visual": snapshot.visual,
        "vote_count": snapshot.vote_count,
        "lifecycle": (
            None
            if lifecycle is None
            else {
                "is_in_progress": lifecycle.is_in_progress,
                "is_started_audition_mid1": lifecycle.is_started_audition_mid1,
                "is_started_audition_mid2": lifecycle.is_started_audition_mid2,
                "is_started_audition_final": lifecycle.is_started_audition_final,
                "is_end_live": lifecycle.is_end_live,
                "schedule_environment_drawing_number": (
                    lifecycle.schedule_environment_drawing_number
                ),
                "schedule_environment_type": lifecycle.schedule_environment_type,
            }
        ),
    }
    return _prove_nia_active_produce_resume_authority(
        snapshot_payload,
        active_run=active_payload,
        run_shadow=shadow.to_dict(),
        expected_run_id=expected_run_id,
        expected_idol_card_id=expected_idol_card_id,
        expected_produce_id=expected_produce_id,
        expected_plan_type=expected_plan_type,
    )


def execute_nia_active_produce_resume(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """Resume one proven active N.I.A. run through Maa's dedicated chain."""

    if payload.get("kind") != "active-produce-resume" or payload.get("target") != "same-run-home":
        raise ValueError("active Produce resume surface binding is invalid")
    authority = payload.get("resume_authority")
    capture = payload.get("capture")
    if not isinstance(authority, Mapping) or not isinstance(capture, Mapping):
        raise ValueError("active Produce resume evidence is incomplete")
    current = read_current_produce_outer_local_save()
    rebound = _resolve_nia_active_produce_resume_authority(
        current,
        expected_run_id=str(authority.get("run_id", "")),
        expected_idol_card_id=str(authority.get("idol_card_id", "")),
        expected_produce_id=str(authority.get("produce_id", "")),
        expected_plan_type=str(authority.get("plan_type", "")),
    )
    if dict(rebound) != dict(authority):
        raise StaleSuggestionError("active Produce resume authority changed before input")

    from .controller_client import send_command

    status = dict(send_command("status"))
    if int(status.get("controller_version", 0)) < 23:
        raise RuntimeError("active Produce resume requires controller v23")
    if not bool(status.get("background_control")):
        raise RuntimeError("active Produce resume requires Maa background control")
    source_hwnd = capture.get("hwnd")
    source_pid = capture.get("pid")
    if (
        type(source_hwnd) is not int
        or source_hwnd <= 0
        or type(source_pid) is not int
        or source_pid <= 0
        or status.get("target_hwnd") != source_hwnd
        or status.get("target_pid") != source_pid
    ):
        raise StaleSuggestionError("active Produce resume controller/window binding changed")
    result = dict(
        send_command(
            "resume_active_produce",
            timeout=45.0,
            timeout_seconds=30.0,
        )
    )
    visited = result.get("visited_nodes")
    expected = ["ProduceContinue", "ProduceChooseContinue", "ProduceEntryFlag"]
    if (
        result.get("resumed") is not True
        or visited != expected
        or "ProduceStart" in visited
    ):
        raise RuntimeError("Maa active Produce resume result is invalid")
    return {
        "accepted": True,
        "input_submitted": True,
        "action": "maa-resume-active-produce",
        "authority": dict(authority),
        "maa": result,
    }


def execute_initial_regular_single_click(
    action: SuggestedClick,
) -> ClickExecutionResult:
    """Execute a one-stage action only through the MAA background controller."""

    from .controller_client import send_command

    status = dict(send_command("status"))
    if not bool(status.get("background_control")):
        raise RuntimeError("Initial Regular execution requires MAA background control")
    semantic_stale_verifier = None
    if action.label == "training-drink-reward:receive":
        from .activity_reward import read_training_drink_reward_path

        def same_training_reward(
            source: Path, current: Path, _action: SuggestedClick
        ) -> bool:
            before = read_training_drink_reward_path(source)
            after = read_training_drink_reward_path(current)
            return (
                before.item_name,
                before.effect_name,
                before.effect_value,
                before.stamina,
                before.max_stamina,
                before.produce_points,
                before.earned_produce_points,
            ) == (
                after.item_name,
                after.effect_name,
                after.effect_value,
                after.stamina,
                after.max_stamina,
                after.produce_points,
                after.earned_produce_points,
            )

        semantic_stale_verifier = same_training_reward
    elif (
        action.label.startswith("initial-regular-overview:")
        and action.label.endswith(":confirm-selected")
    ):
        parts = action.label.split(":")
        if len(parts) != 3 or parts[1] not in {
            "class",
            "vocal_lesson",
            "dance_lesson",
            "visual_lesson",
        }:
            raise ValueError("selected overview confirmation label is malformed")
        target = parts[1]
        analyzer = _overview_preview_analyzer({}, action, target)

        def same_selected_overview(
            source: Path, current: Path, _action: SuggestedClick
        ) -> bool:
            before = analyzer(source)
            after = analyzer(current)
            return bool(
                before.highlighted
                and after.highlighted
                and before.confidence >= 0.80
                and after.confidence >= 0.80
                and not before.issues
                and not after.issues
                and before.semantic_key == after.semantic_key
            )

        semantic_stale_verifier = same_selected_overview
    elif action.label.startswith("continue-post-audition-"):
        from .live_source import (
            _fixed_post_audition_dialogue_layout,
            _fixed_post_audition_summary_layout,
        )

        def same_post_audition_continuation(
            _source: Path, current: Path, _action: SuggestedClick
        ) -> bool:
            # Post-Final portrait dialogue can animate into the next dialogue
            # or the fixed condition sheet between capture and MAA input.  The
            # outer Final ledger remains the state authority; either known
            # continuation layout keeps the central tap safe.
            return bool(
                _fixed_post_audition_dialogue_layout(current)
                or _fixed_post_audition_summary_layout(current)
            )

        semantic_stale_verifier = same_post_audition_continuation
    return execute_suggested_click(
        action,
        command_sender=send_command,
        semantic_stale_verifier=semantic_stale_verifier,
    )


def load_initial_regular_plan2_exam_evidence(exam_save_path: Path):
    """Decode the selected ExamSaveData into self-contained typed evidence.

    The outer autopilot has no mutable audition checkpoint.  Its evidence
    identity is therefore derived from the selected save location and immutable
    exam identity; each read still carries the exact source bytes and typed
    state, so the Plan2 executor can require changed, stable post-action data.
    """

    from .audition_local_save_state import (
        AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION,
        EXAM_SAVE_DATA_SOURCE_TYPE,
        AuditionLocalSaveStateEvidence,
        parse_local_save_exam_state,
    )
    from .local_save_decoder import decode_local_save_bytes

    path = Path(exam_save_path).resolve()
    source = path.read_bytes()
    if not source:
        raise ValueError("ExamSaveData is empty")
    envelope = decode_local_save_bytes(source, EXAM_SAVE_DATA_SOURCE_TYPE)
    try:
        payload = json.loads(envelope.plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("decrypted ExamSaveData is not valid UTF-8 JSON") from error
    if not isinstance(payload, Mapping):
        raise ValueError("decrypted ExamSaveData root must be an object")
    state = parse_local_save_exam_state(payload)
    source_digest = hashlib.sha256(source).hexdigest()
    identity = (
        f"{path.parent}|{state.character_id}|{state.setting_id}|"
        f"{state.step_type_value}"
    )
    identity_digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    zones_digest = hashlib.sha256(
        json.dumps(
            state.zones.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return AuditionLocalSaveStateEvidence(
        schema_version=AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION,
        run_id=f"initial-regular-plan2:{path.parent}",
        step_context_id=f"exam-step:{state.step_type_value}",
        step_context_digest=identity_digest,
        session_transition_id=f"exam-setting:{state.setting_id}",
        zone_checkpoint_digest=zones_digest,
        source_path=str(path),
        source_sha256=source_digest,
        source_size=len(source),
        source_type=EXAM_SAVE_DATA_SOURCE_TYPE,
        envelope_version=envelope.save_data_version,
        state=state,
    )


def _plan2_state_is_terminal(state: Any) -> bool:
    runtime = getattr(state, "root_runtime", None)
    return bool(runtime is not None and runtime.is_exam_end_complete)


def _plan2_runtime_stage_identity_matches(left: Any, right: Any) -> bool:
    """Bind runtime-derived state to one existing ExamSave stage envelope."""

    evidence_fields = (
        "run_id",
        "step_context_id",
        "step_context_digest",
        "session_transition_id",
        "source_path",
        "source_type",
    )
    state_fields = (
        "character_id",
        "setting_id",
        "exam_type",
        "step_type_value",
        "limit_turn",
        "max_stamina",
        "turn_parameter_types",
        "vocal_bonus_permille",
        "dance_bonus_permille",
        "visual_bonus_permille",
    )
    return bool(
        all(getattr(left, name, None) == getattr(right, name, None) for name in evidence_fields)
        and all(
            getattr(getattr(left, "state", None), name, None)
            == getattr(getattr(right, "state", None), name, None)
            for name in state_fields
        )
    )


def _plan2_terminal_exam_result(
    evidence: Any,
    deck_shadow_sync: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    """Return the phase-8 save as the final inner-exam authority."""

    return {
        "accepted": True,
        "terminal": True,
        "reason": "terminal",
        "actions_executed": 0,
        "steps": [],
        "differences": [],
        "orchestration": {
            "source": "exam-save-terminal",
            "current_turn": evidence.state.current_turn,
            "score": evidence.state.score,
            "stamina": evidence.state.stamina,
            "block": evidence.state.block,
            **(
                {}
                if deck_shadow_sync is None
                else {"deck_shadow_sync": dict(deck_shadow_sync)}
            ),
        },
    }


def _plan2_step_type(step_type_value: int) -> str:
    names = {
        1: "ProduceStepType_LessonVocalNormal",
        2: "ProduceStepType_LessonVocalSp",
        3: "ProduceStepType_LessonVocalHard",
        4: "ProduceStepType_LessonDanceNormal",
        5: "ProduceStepType_LessonDanceSp",
        6: "ProduceStepType_LessonDanceHard",
        7: "ProduceStepType_LessonVisualNormal",
        8: "ProduceStepType_LessonVisualSp",
        9: "ProduceStepType_LessonVisualHard",
        16: "ProduceStepType_AuditionMid1",
        17: "ProduceStepType_AuditionMid2",
        18: "ProduceStepType_AuditionFinal",
    }
    try:
        return names[step_type_value]
    except KeyError as error:
        raise ValueError(
            f"unsupported Initial Regular Plan2 step type: {step_type_value}"
        ) from error


def _plan2_expected_screen_state(
    horizon: Any,
    evidence: Any,
    *,
    turn_end_stamina_recovery: int,
) -> Mapping[str, Any]:
    """Project only the legacy screenshot verifier's Plan2-compatible fields."""

    from .logic_engine import LogicExamState

    scalar = horizon.scalar
    review = horizon.review_dynamic
    if horizon.exam_mode.is_lesson:
        if evidence.state.turn_parameter_types:
            raise ValueError("Plan2 lesson screen projection requires an empty schedule")
        multiplier = 1000
    else:
        if not evidence.state.turn_parameter_types:
            raise ValueError("Plan2 audition screen projection requires a schedule")
        turn_index = min(
            max(scalar.current_turn - 1, 0),
            len(evidence.state.turn_parameter_types) - 1,
        )
        parameter = evidence.state.turn_parameter_types[turn_index]
        multiplier = {
            1: evidence.state.vocal_bonus_permille,
            2: evidence.state.dance_bonus_permille,
            3: evidence.state.visual_bonus_permille,
        }[parameter]
    return asdict(
        LogicExamState(
            turns_remaining=horizon.remaining_turns,
            stamina=scalar.stamina,
            score=scalar.score,
            block=scalar.block,
            good_impression=scalar.review,
            motivation=scalar.review_count_add,
            round_number=scalar.current_turn,
            lost_card_count=len(horizon.zones.lost),
            last_resolved_turn_start_round=scalar.current_turn,
            score_multiplier_permille=multiplier,
            max_stamina=scalar.max_stamina,
            turn_end_stamina_recovery=turn_end_stamina_recovery,
            plays_remaining=max(horizon.plays_remaining, 0),
            good_impression_exists=review.review_status_present,
            good_impression_passing_turn_start=(
                review.review_passing_turn_start
            ),
        )
    )


def _prove_plan2_submitted_play_physical_settlement(
    before: Any,
    action: Any,
    submitted: Any,
    settled: Any,
    *,
    catalog: Any = None,
) -> tuple[str, ...]:
    """Prove one already-submitted PLAY from native truth, then rebootstrap.

    The transitional ``playingCard``/command queue owns the GUID receipt.  The
    later idle save must append exactly that card version to ``userPlayLog``,
    advance only one aggregate play, retain the same GUID/runtime lineage, and
    produce a fresh exact bootstrap.  Local simulator replay is deliberately
    absent from this acceptance boundary.
    """

    from .audition_local_save_state import AuditionLocalSaveStateEvidence
    from .audition_native_ordered_zones import NativeOrderedCardInstance
    from .plan2_native_horizon import Plan2NativeAction
    from .plan2_native_exam_save_orchestrator import (
        Plan2NativeExamSaveDependencies,
    )
    from .plan2_native_program_catalog import (
        compile_plan2_native_program_catalog,
    )
    from .plan2_replay_journal import (
        _native_user_play_action_sequence,
        _settled_turn_schedule_extends_exactly,
    )

    if not all(
        isinstance(value, AuditionLocalSaveStateEvidence)
        for value in (before, submitted, settled)
    ):
        raise TypeError("physical settlement requires typed evidence")
    if not isinstance(action, Plan2NativeAction) or action.kind != "play":
        raise TypeError("physical settlement requires one typed PLAY")
    identity_fields = (
        "run_id",
        "step_context_id",
        "step_context_digest",
        "session_transition_id",
        "source_path",
        "source_type",
    )
    stage_fields = (
        "character_id",
        "setting_id",
        "exam_type",
        "step_type_value",
        "limit_turn",
        "max_stamina",
    )
    if any(
        getattr(submitted, name) != getattr(before, name)
        or getattr(settled, name) != getattr(before, name)
        for name in identity_fields
    ):
        raise ValueError("submitted physical settlement identity changed")
    if any(
        getattr(submitted.state, name) != getattr(before.state, name)
        or getattr(settled.state, name) != getattr(before.state, name)
        for name in stage_fields
    ):
        raise ValueError("submitted physical settlement crossed exam stage")
    if not before.state.is_native_actionable_settled:
        raise ValueError("submitted physical settlement before is not settled")
    terminal = _plan2_state_is_terminal(settled.state)
    if not settled.state.is_native_actionable_settled and not terminal:
        raise ValueError("submitted physical settlement after is not settled")
    playing = submitted.state.playing_card
    submitted_runtime = submitted.state.root_runtime
    if (
        playing is None
        or playing.guid != action.card_guid
        or submitted_runtime is None
        or submitted_runtime.command_list_is_empty
        or submitted_runtime.is_exam_end_complete
    ):
        raise ValueError("submitted physical settlement GUID receipt is invalid")
    before_matches = tuple(
        card
        for card in before.state.zones.hand
        if card.guid == action.card_guid
    )
    if len(before_matches) != 1:
        raise ValueError("submitted PLAY GUID is not unique in before Hand")
    before_card = before_matches[0]
    if (
        playing.card_id != before_card.card_id
        or playing.base_upgrade != before_card.base_upgrade
        or playing.fixed_deck_order != before_card.fixed_deck_order
        or playing.runtime_state is None
        or before_card.runtime_state is None
        or playing.runtime_state.play_count
        <= before_card.runtime_state.play_count
    ):
        raise ValueError("submitted PLAY card version/runtime is invalid")
    before_log = _native_user_play_action_sequence(before)
    expected_log = (
        *before_log,
        (
            before.state.current_turn,
            playing.card_id,
            playing.effective_upgrade,
        ),
    )
    submitted_log = _native_user_play_action_sequence(submitted)
    if submitted_log not in {before_log, expected_log}:
        raise ValueError("submitted PLAY native userPlayLog prefix changed")
    if _native_user_play_action_sequence(settled) != expected_log:
        raise ValueError("submitted PLAY native userPlayLog is not exact")
    if (
        settled.state.exam_card_play_count
        != before.state.exam_card_play_count + 1
        or submitted.state.exam_card_play_count
        not in {
            before.state.exam_card_play_count,
            settled.state.exam_card_play_count,
        }
        or not _settled_turn_schedule_extends_exactly(
            submitted.state,
            settled.state,
        )
    ):
        raise ValueError("submitted PLAY native counters are not exact")
    current_matches = tuple(
        card
        for zone_name in ("hand", "deck", "grave", "lost", "hold")
        for card in getattr(settled.state.zones, zone_name)
        if card.guid == action.card_guid
    )
    if len(current_matches) != 1:
        raise ValueError("submitted PLAY GUID is not unique after settlement")
    current_card = current_matches[0]
    if (
        current_card.card_id != playing.card_id
        or current_card.base_upgrade != playing.base_upgrade
        or current_card.fixed_deck_order != playing.fixed_deck_order
        or current_card.runtime_state is None
        or current_card.runtime_state.play_count
        < playing.runtime_state.play_count
    ):
        raise ValueError("submitted PLAY settled GUID/runtime is invalid")

    if terminal:
        return (
            "physical-settlement:submitted-playing-guid-exact",
            "physical-settlement:native-user-play-log-one-action",
            "physical-settlement:card-runtime-progress-exact",
            "physical-settlement:native-terminal-exact",
        )

    if catalog is None:
        catalog = compile_plan2_native_program_catalog().catalog
    bootstrap = Plan2NativeExamSaveDependencies().bootstrapper(
        settled,
        catalog,
        None,
        None,
    )
    if not bootstrap.simulation_ready or bootstrap.state is None:
        raise ValueError(
            "submitted PLAY fresh bootstrap unavailable:"
            + ",".join(value.code for value in bootstrap.blockers)
        )
    fresh = bootstrap.state
    scalar = fresh.scalar
    native = settled.state
    if (
        scalar.current_turn != native.current_turn
        or fresh.remaining_turns != native.remain_turn
        or scalar.exam_card_play_count != native.exam_card_play_count
        or scalar.turn_card_play_count != native.turn_card_play_count
        or scalar.score != native.score
        or scalar.stamina != native.stamina
        or scalar.max_stamina != native.max_stamina
        or scalar.block != native.block
        or fresh.zones.random_state != native.random_state
    ):
        raise ValueError("submitted PLAY fresh bootstrap scalar mismatch")
    for zone_name in ("hand", "deck", "grave", "lost"):
        expected_zone = tuple(
            NativeOrderedCardInstance.from_local_save(card)
            for card in getattr(native.zones, zone_name)
        )
        if getattr(fresh.zones, zone_name) != expected_zone:
            raise ValueError(
                f"submitted PLAY fresh bootstrap {zone_name} mismatch"
            )
    return (
        "physical-settlement:submitted-playing-guid-exact",
        "physical-settlement:native-user-play-log-one-action",
        "physical-settlement:card-runtime-progress-exact",
        "physical-settlement:fresh-bootstrap-exact",
    )


def _complete_plan2_pending_play(
    journal_sink: Any,
    receipt: Any,
    settled: Any,
    *,
    logical_before: Any = None,
    prior_replay: Any = None,
) -> None:
    """Prove one pending PLAY, append its normal journal row, then clear it."""

    from .plan2_card_history import (
        Plan2CardHistoryObservation,
        Plan2CardHistoryObservationAuthority,
    )
    from .plan2_native_maa_action_executor import (
        Plan2StableSettledEvidence,
        _validate_next_evidence,
    )
    from .plan2_replay_journal import (
        PENDING_PLAY_BEFORE_LOGICAL_CONTINUATION,
        observation_from_horizon,
    )

    if receipt.before_authority == PENDING_PLAY_BEFORE_LOGICAL_CONTINUATION:
        if logical_before is None or prior_replay is None:
            raise ValueError("pending PLAY logical continuation is unavailable")
        _validate_next_evidence(
            receipt.persisted_before,
            receipt.action,
            Plan2StableSettledEvidence(settled, settled),
            logical_before=logical_before,
            prior_replay=prior_replay,
            require_duplicate_play_proof=True,
        )
    else:
        _prove_plan2_submitted_play_physical_settlement(
            receipt.persisted_before,
            receipt.action,
            receipt.submitted_evidence,
            settled,
        )

    if _plan2_state_is_terminal(settled.state):
        observation = Plan2CardHistoryObservation(
            turns_remaining=settled.state.remain_turn,
            score=settled.state.score,
            stamina=settled.state.stamina,
            block=settled.state.block,
            source="native-pending-play-terminal",
            authority=Plan2CardHistoryObservationAuthority.DIAGNOSTIC,
        )
    else:
        from .plan2_native_exam_save_orchestrator import (
            decide_plan2_native_exam_save,
        )

        decision = decide_plan2_native_exam_save(settled)
        logical_after = decision.logical_root
        if logical_after is None and decision.bootstrap is not None:
            logical_after = decision.bootstrap.state
        if logical_after is None:
            raise ValueError("pending PLAY settled bootstrap is unavailable")
        observation = observation_from_horizon(
            logical_after,
            source="native-pending-play-settled",
        )
    journal_sink.complete_pending_play_chain(
        receipt,
        observation,
        settled,
    )


def _resume_plan2_pending_play_cleanup(
    journal_sink: Any,
    receipt: Any,
    current: Any,
) -> bool:
    """Consume a durable settled tombstone before replay context recovery."""

    if getattr(receipt, "state_after", None) is None:
        return False
    journal_sink.cleanup_pending_play_tombstone(receipt, current)
    return True


def _recover_plan2_pending_play_before_wait(
    waiter_owner: Any,
    journal_sink: Any,
    receipt: Any,
    current: Any,
    *,
    logical_before: Any = None,
    prior_replay: Any = None,
    completion: Callable[..., None] = _complete_plan2_pending_play,
) -> tuple[Any | None, Any]:
    """Replay one pending PLAY, waiting only for a typed external-item gap."""

    from .plan2_replay_journal import (
        PendingReplayDisposition,
        PendingReplayResult,
        Plan2ReplayJournalError,
        classify_pending_replay_result,
    )

    replay_pending = getattr(waiter_owner, "replay_pending_submission", None)
    pending_result = (
        replay_pending(
            receipt,
            logical_before=logical_before,
            prior_replay=prior_replay,
        )
        if callable(replay_pending)
        else classify_pending_replay_result(None)
    )
    if not isinstance(pending_result, PendingReplayResult):
        raise TypeError("pending PLAY resolver returned an invalid typed result")
    if pending_result.disposition is PendingReplayDisposition.REPLAYED:
        replay = pending_result.replay
        assert replay is not None
        journal_sink.append_accepted_pending_play(
            receipt.persisted_before,
            receipt.action,
            receipt.submitted_evidence,
            replay,
        )
        return replay, current
    detail = ",".join(
        value.code + (":" + value.detail if value.detail else "")
        for value in pending_result.issues
    )
    if (
        pending_result.disposition
        is not PendingReplayDisposition.WAIT_EXTERNAL_SETTLEMENT
    ):
        code = (
            "pending-play-replay-resolver-unavailable"
            if pending_result.disposition
            is PendingReplayDisposition.RESOLVER_UNAVAILABLE
            else "pending-play-replay-rejected"
        )
        raise Plan2ReplayJournalError(code, detail)
    wait_pending = getattr(
        waiter_owner,
        "wait_external_retained_settlement",
        None,
    )
    if not callable(wait_pending):
        raise RuntimeError("plan2-pending-play-zero-input-waiter-unavailable")
    stable = wait_pending(current)
    settled = stable.second
    completion(
        journal_sink,
        receipt,
        settled,
        logical_before=logical_before,
        prior_replay=prior_replay,
    )
    return None, settled


class _InitialRegularPlan2EvidencePoller:
    """Bounded file reader used by the loop and post-input adapter waiter."""

    def __init__(
        self,
        path: Path,
        loader: Plan2EvidenceLoader,
        *,
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: float = 0.25,
        max_samples: int = 96,
        require_duplicate_reads: bool = True,
        return_exact_retained_replay: bool = False,
        completed_card_replay_resolver: Callable[[Any, Any, Any], Any] | None = None,
        completed_card_chain_replay_resolver: (
            Callable[[Any, Any, Any, Any, Any], Any] | None
        ) = None,
        physical_settlement_prover: (
            Callable[[Any, Any, Any, Any], object] | None
        ) = None,
        accepted_pending_play_sink: (
            Callable[[Any, Any, Any, Any], object] | None
        ) = None,
        submitted_pending_play_sink: (
            Callable[[Any, Any, Any, Any, int, Any, Any], object] | None
        ) = None,
        settled_pending_play_sink: (
            Callable[[Any, Any, Any, Any, Any, Any], object] | None
        ) = None,
        visual_stability_gate: Any = None,
    ) -> None:
        self.path = Path(path)
        self.loader = loader
        self.sleep = sleep
        self.poll_seconds = poll_seconds
        self.max_samples = max_samples
        self.require_duplicate_reads = require_duplicate_reads
        if not isinstance(return_exact_retained_replay, bool):
            raise TypeError("return_exact_retained_replay must be boolean")
        self.return_exact_retained_replay = return_exact_retained_replay
        self.completed_card_replay_resolver = completed_card_replay_resolver
        self.completed_card_chain_replay_resolver = (
            completed_card_chain_replay_resolver
        )
        self.physical_settlement_prover = physical_settlement_prover
        if accepted_pending_play_sink is not None and not callable(
            accepted_pending_play_sink
        ):
            raise TypeError("accepted_pending_play_sink must be callable or None")
        self.accepted_pending_play_sink = accepted_pending_play_sink
        if submitted_pending_play_sink is not None and not callable(
            submitted_pending_play_sink
        ):
            raise TypeError("submitted_pending_play_sink must be callable or None")
        self.submitted_pending_play_sink = submitted_pending_play_sink
        self._submitted_play_dispatch_witness: Mapping[str, Any] | None = None
        self._submitted_play_hand_slot: int | None = None
        self._submitted_play_logical_before: Any = None
        self._submitted_play_prior_replay: Any = None
        self._submitted_action_kind: str | None = None
        self._submitted_action_slot: int | None = None
        self._runtime_action_settlement_cursor: Mapping[str, Any] | None = None
        self._runtime_action_settlement_watcher: Any = None
        self._latest_runtime_action_settlement: Any = None
        self._pending_runtime_transition: Any = None
        self._runtime_before_provenance: Any = None
        if settled_pending_play_sink is not None and not callable(
            settled_pending_play_sink
        ):
            raise TypeError("settled_pending_play_sink must be callable or None")
        self.settled_pending_play_sink = settled_pending_play_sink
        self._persisted_pending_play_keys: set[tuple[str, str]] = set()
        if visual_stability_gate is not None and not all(
            callable(getattr(visual_stability_gate, name, None))
            for name in ("begin", "sample")
        ):
            raise TypeError("visual_stability_gate must provide begin/sample")
        self.visual_stability_gate = visual_stability_gate

    def bind_accepted_pending_play_sink(
        self,
        sink: Callable[[Any, Any, Any, Any], object],
    ) -> None:
        if not callable(sink):
            raise TypeError("accepted pending PLAY sink must be callable")
        if self.accepted_pending_play_sink is not None:
            raise ValueError("accepted pending PLAY sink is already bound")
        self.accepted_pending_play_sink = sink

    def bind_submitted_pending_play_sink(
        self,
        sink: Callable[[Any, Any, Any, Any], object],
    ) -> None:
        if not callable(sink):
            raise TypeError("submitted pending PLAY sink must be callable")
        if self.submitted_pending_play_sink is not None:
            raise ValueError("submitted pending PLAY sink is already bound")
        self.submitted_pending_play_sink = sink

    def bind_settled_pending_play_sink(
        self,
        sink: Callable[[Any, Any, Any, Any, Any, Any], object],
    ) -> None:
        if not callable(sink):
            raise TypeError("settled pending PLAY sink must be callable")
        if self.settled_pending_play_sink is not None:
            raise ValueError("settled pending PLAY sink is already bound")
        self.settled_pending_play_sink = sink

    def bind_runtime_before_provenance(self, provenance: Any) -> None:
        """Bind the native state whose disk checkpoint may still lag behind."""

        if provenance is not None:
            from .runtime_action_state_evidence import (
                RuntimeActionStateProvenance,
            )

            if not isinstance(provenance, RuntimeActionStateProvenance):
                raise TypeError("runtime-before provenance must be typed or None")
        self._runtime_before_provenance = provenance

    def begin_runtime_action_receipt(
        self,
        action: Any,
        expected_slot: int | None = None,
    ) -> None:
        """Open a clean receipt scope before any screen gate or input."""

        action_kind = getattr(action, "kind", None)
        if action_kind not in {"play", "drink", "end_turn"}:
            raise ValueError("runtime receipt action kind is invalid")
        if action_kind == "play":
            if type(expected_slot) is not int or expected_slot < 0:
                raise ValueError("runtime PLAY receipt slot is invalid")
            action_slot = expected_slot
        elif action_kind == "drink":
            action_slot = getattr(action, "slot_index", None)
            if type(action_slot) is not int or action_slot < 0:
                raise ValueError("runtime DRINK receipt slot is invalid")
        else:
            action_slot = 0
        self._submitted_action_kind = action_kind
        self._submitted_action_slot = action_slot
        self._submitted_play_dispatch_witness = None
        self._submitted_play_hand_slot = None
        self._submitted_play_logical_before = None
        self._submitted_play_prior_replay = None
        self._runtime_action_settlement_cursor = None
        self._runtime_action_settlement_watcher = None
        self._latest_runtime_action_settlement = None
        self._pending_runtime_transition = None

    def bind_submitted_play_dispatch_witness(
        self,
        dispatch: Mapping[str, Any],
        hand_slot: int,
        logical_before: Any = None,
        prior_replay: Any = None,
    ) -> None:
        if type(hand_slot) is not int or hand_slot < 0:
            raise ValueError("submitted PLAY Hand slot is invalid")
        self._bind_submitted_action_dispatch_witness(
            dispatch,
            action_kind="play",
            action_slot=hand_slot,
            logical_before=logical_before,
            prior_replay=prior_replay,
        )

    def bind_submitted_action_dispatch_witness(
        self,
        dispatch: Mapping[str, Any],
        action: Any,
        hand_slot: int | None = None,
        logical_before: Any = None,
        prior_replay: Any = None,
    ) -> None:
        """Start one native receipt lifecycle for the submitted Exam action."""

        action_kind = getattr(action, "kind", None)
        if action_kind not in {"play", "drink", "end_turn"}:
            raise ValueError("submitted Exam action kind is invalid")
        if action_kind == "play":
            if type(hand_slot) is not int or hand_slot < 0:
                raise ValueError("submitted PLAY Hand slot is invalid")
            action_slot = hand_slot
        elif action_kind == "drink":
            action_slot = getattr(action, "slot_index", None)
            if type(action_slot) is not int or action_slot < 0:
                raise ValueError("submitted DRINK slot is invalid")
        else:
            action_slot = 0
        self._bind_submitted_action_dispatch_witness(
            dispatch,
            action_kind=action_kind,
            action_slot=action_slot,
            logical_before=logical_before,
            prior_replay=prior_replay,
        )

    def _bind_submitted_action_dispatch_witness(
        self,
        dispatch: Mapping[str, Any],
        *,
        action_kind: str,
        action_slot: int | None,
        logical_before: Any,
        prior_replay: Any,
    ) -> None:
        if not isinstance(dispatch, Mapping) or dispatch.get("submitted") is not True:
            raise ValueError("submitted Exam action dispatch witness is invalid")
        if (
            self._submitted_action_kind is not None
            and (
                self._submitted_action_kind != action_kind
                or self._submitted_action_slot != action_slot
            )
        ):
            raise ValueError("submitted Exam action differs from receipt scope")
        self._submitted_action_kind = action_kind
        self._submitted_action_slot = action_slot
        is_play = action_kind == "play"
        self._submitted_play_dispatch_witness = dict(dispatch) if is_play else None
        self._submitted_play_hand_slot = action_slot if is_play else None
        self._submitted_play_logical_before = logical_before if is_play else None
        self._submitted_play_prior_replay = prior_replay if is_play else None
        audit = dispatch.get("audit")
        cursor = audit.get("native_action_settlement") if isinstance(audit, Mapping) else None
        self._runtime_action_settlement_cursor = None
        self._runtime_action_settlement_watcher = None
        self._latest_runtime_action_settlement = None
        self._pending_runtime_transition = None
        if isinstance(cursor, Mapping):
            path = cursor.get("path")
            start_offset = cursor.get("start_offset")
            if (
                isinstance(path, str)
                and path
                and type(start_offset) is int
                and start_offset >= 0
            ):
                self._runtime_action_settlement_cursor = dict(cursor)

    def _persist_accepted_pending_play(
        self,
        before: Any,
        action: Any,
        retained: Any,
        replay: Any,
    ) -> None:
        from .plan2_native_maa_action_executor import (
            Plan2SubmittedPhysicalSettlementError,
        )

        sink = self.accepted_pending_play_sink
        if sink is None:
            return
        key = (retained.digest(), action.action_id)
        if key in self._persisted_pending_play_keys:
            return
        try:
            sink(before, action, retained, replay)
        except Exception as error:
            raise Plan2SubmittedPhysicalSettlementError(
                "accepted pending PLAY journal persist failed:"
                f"{type(error).__name__}:{error}"
            ) from error
        self._persisted_pending_play_keys.add(key)

    def _persist_submitted_pending_play(
        self,
        before: Any,
        action: Any,
        retained: Any,
    ) -> None:
        sink = self.submitted_pending_play_sink
        if sink is None:
            return
        witness = self._submitted_play_dispatch_witness
        hand_slot = self._submitted_play_hand_slot
        if witness is None or hand_slot is None:
            raise ValueError("submitted PLAY dispatch witness is unavailable")
        try:
            sink(
                before,
                action,
                retained,
                witness,
                hand_slot,
                self._submitted_play_logical_before,
                self._submitted_play_prior_replay,
            )
        except Exception as error:
            from .plan2_native_maa_action_executor import (
                Plan2SubmittedPhysicalSettlementError,
            )
            raise Plan2SubmittedPhysicalSettlementError(
                "submitted pending PLAY journal persist failed:"
                f"{type(error).__name__}:{error}"
            ) from error

    def _read(self):
        from .audition_local_save_state import AuditionLocalSaveStateEvidence

        evidence = self.loader(self.path)
        if not isinstance(evidence, AuditionLocalSaveStateEvidence):
            raise TypeError("Plan2 evidence loader returned an invalid value")
        return evidence

    def initial(self, previous: Any):
        if previous is not None:
            raise ValueError("initial Plan2 evidence reader expects previous=None")
        evidence = self._read()
        if not (
            evidence.state.is_native_actionable_settled
            or _plan2_state_is_terminal(evidence.state)
        ):
            raise ValueError("initial ExamSaveData is not settled or terminal")
        return evidence

    def wait_next(self, before: Any, _action: Any):
        return self._wait_next(
            before,
            _action,
            resolver=self.completed_card_replay_resolver,
            logical_before=None,
            prior_replay=None,
        )

    def wait_next_logical(
        self,
        before: Any,
        action: Any,
        logical_before: Any,
        prior_replay: Any,
    ):
        resolver = self.completed_card_chain_replay_resolver
        if resolver is None:
            raise RuntimeError("completed card chain replay resolver is not configured")
        return self._wait_next(
            before,
            action,
            resolver=lambda old, new, selected: resolver(
                old, new, selected, logical_before, prior_replay
            ),
            logical_before=logical_before,
            prior_replay=prior_replay,
        )

    def _wait_changed_settled(
        self,
        retained: Any,
        timeout_message: str,
    ):
        """Wait without input for one changed actionable or terminal save."""

        from .plan2_native_maa_action_executor import Plan2StableSettledEvidence

        prior = None
        last_error: Exception | None = None
        for index in range(self.max_samples):
            if index:
                self.sleep(self.poll_seconds)
            try:
                current = self._read()
            except (OSError, TypeError, ValueError) as error:
                last_error = error
                prior = None
                continue
            if current == retained:
                prior = None
                continue
            if not (
                current.state.is_native_actionable_settled
                or _plan2_state_is_terminal(current.state)
            ):
                prior = None
                continue
            if not self.require_duplicate_reads or prior == current:
                return Plan2StableSettledEvidence(current, current)
            prior = current
        detail = (
            ""
            if last_error is None
            else f":{type(last_error).__name__}:{last_error}"
        )
        raise RuntimeError(timeout_message + detail)

    def wait_retained_settlement(self, retained: Any, replay: Any):
        """Wait without input until one replay-proven native queue settles."""

        from .plan2_card_history import (
            Plan2CompletedCardReplay,
            Plan2CompletedDrinkReplay,
        )

        if not isinstance(
            replay,
            (Plan2CompletedCardReplay, Plan2CompletedDrinkReplay),
        ):
            raise TypeError("retained replay must be typed")
        if replay.persisted_transition != retained:
            raise ValueError("retained replay does not bind retained evidence")
        return self._wait_changed_settled(
            retained,
            "retained command queue did not physically settle",
        )

    def wait_external_retained_settlement(self, retained: Any):
        """Wait without input when an external item blocks logical replay."""

        return self._wait_changed_settled(
            retained,
            "external item command queue did not physically settle",
        )

    def replay_pending_submission(
        self,
        receipt: Any,
        *,
        logical_before: Any = None,
        prior_replay: Any = None,
    ) -> PendingReplayResult:
        """Try the already-bound exact resolver once without polling or input."""

        from .plan2_replay_journal import (
            PendingReplayDisposition,
            classify_pending_replay_result,
        )

        before = receipt.persisted_before
        action = receipt.action
        submitted = receipt.submitted_evidence
        if logical_before is None and prior_replay is None:
            resolver = self.completed_card_replay_resolver
            if resolver is None:
                return classify_pending_replay_result(None)
            replay_result = resolver(before, submitted, action)
        else:
            resolver = self.completed_card_chain_replay_resolver
            if resolver is None or logical_before is None or prior_replay is None:
                return classify_pending_replay_result(None)
            replay_result = resolver(
                before,
                submitted,
                action,
                logical_before,
                prior_replay,
            )
        result = classify_pending_replay_result(replay_result)
        if result.disposition is not PendingReplayDisposition.REPLAYED:
            return result
        replay = result.replay
        assert replay is not None
        if (
            replay.persisted_before != before
            or replay.persisted_transition != submitted
            or replay.action != action
        ):
            raise ValueError("pending PLAY exact replay identity differs")
        return result

    def _wait_next(
        self,
        before: Any,
        _action: Any,
        *,
        resolver: Callable[[Any, Any, Any], Any] | None,
        logical_before: Any,
        prior_replay: Any,
    ):
        from .plan2_native_maa_action_executor import (
            Plan2StableSettledEvidence,
            Plan2SubmittedPhysicalSettlementError,
            Plan2SubmittedPhysicalSettlementTimeout,
        )
        from .plan2_replay_journal import (
            PendingReplayDisposition,
            classify_pending_replay_result,
        )

        prior = None
        retained_action_replay = None
        submitted_transition = None
        proven_settled = None
        last_error: Exception | None = None
        last_replay_failure = None
        runtime_before_provenance = self._runtime_before_provenance
        runtime_base_source_sha = (
            None
            if runtime_before_provenance is None
            else runtime_before_provenance.base_source_sha256
        )
        cursor = self._runtime_action_settlement_cursor
        if cursor is not None:
            from .runtime_action_settlement_watcher import (
                RuntimeActionSettlementWatcher,
            )

            if self._submitted_action_kind != _action.kind:
                raise ValueError("submitted Exam action witness kind differs")
            watcher_kwargs: dict[str, Any] = {
                "start_offset": cursor["start_offset"],
                "expected_action_kind": _action.kind,
                "expected_slot": self._submitted_action_slot,
            }
            if _action.kind == "play":
                watcher_kwargs["expected_card_guid"] = _action.card_guid
            elif _action.kind == "drink":
                watcher_kwargs["expected_drink_id"] = _action.drink_id
            self._runtime_action_settlement_watcher = RuntimeActionSettlementWatcher(
                cursor["path"],
                **watcher_kwargs,
            )

        def native_transition() -> Any | None:
            receipt = self._pending_runtime_transition
            if receipt is None:
                return None
            from .runtime_action_state_evidence import (
                adapt_runtime_action_state_evidence,
            )

            raw_state = json.loads(receipt.state_after)
            if not isinstance(raw_state, Mapping):
                raise ValueError("native transition state_after must be an object")
            transition_base = (
                submitted_transition
                if submitted_transition is not None
                else before
            )
            evidence, provenance = adapt_runtime_action_state_evidence(
                transition_base,
                raw_state,
                telemetry_path=receipt.path,
                telemetry_end_offset=receipt.end_offset,
            )
            if (
                submitted_transition is None
                and runtime_base_source_sha is not None
            ):
                provenance = replace(
                    provenance,
                    base_source_sha256=runtime_base_source_sha,
                )
            if not (
                evidence.state.is_native_actionable_settled
                or _plan2_state_is_terminal(evidence.state)
            ):
                raise ValueError(
                    "native transition state_after is not actionable or terminal"
                )
            if (
                _action.kind == "play"
                and self.settled_pending_play_sink
                and submitted_transition is not None
            ):
                self.settled_pending_play_sink(
                    before,
                    _action,
                    submitted_transition,
                    evidence,
                    logical_before,
                    prior_replay,
                )
            return Plan2StableSettledEvidence(
                evidence,
                evidence,
                runtime_provenance=provenance,
            )

        visual_stable = bool(
            self.visual_stability_gate is None or _action.kind != "play"
        )
        if self.visual_stability_gate is not None:
            self.visual_stability_gate.begin(_action, before)
        for index in range(self.max_samples):
            if index:
                self.sleep(self.poll_seconds)
            if not visual_stable and self.visual_stability_gate is not None:
                try:
                    visual_stable = bool(
                        self.visual_stability_gate.sample()
                    )
                except (OSError, TypeError, ValueError, RuntimeError) as error:
                    last_error = error
            native_receipt = None
            if self._runtime_action_settlement_watcher is not None:
                native_receipt = self._runtime_action_settlement_watcher.poll()
            if native_receipt is not None:
                self._latest_runtime_action_settlement = native_receipt
                if (
                    native_receipt.source_record == "transition"
                    and native_receipt.state_after is not None
                ):
                    self._pending_runtime_transition = native_receipt
                    stable = native_transition()
                    if stable is not None:
                        return stable
            try:
                current = self._read()
            except (OSError, TypeError, ValueError) as error:
                last_error = error
                prior = None
                continue
            if (
                runtime_base_source_sha is not None
                and current.source_sha256 == runtime_base_source_sha
            ):
                prior = None
                last_replay_failure = None
                continue
            if (
                runtime_before_provenance is not None
                and current.state == before.state
            ):
                runtime_base_source_sha = current.source_sha256
                prior = None
                last_replay_failure = None
                continue
            if current == before:
                prior = None
                last_replay_failure = None
                continue
            current_runtime = current.state.root_runtime
            submitted_playing = bool(
                _action.kind == "play"
                and current.state.playing_card is not None
                and current_runtime is not None
                and not current_runtime.command_list_is_empty
            )
            if submitted_playing:
                assert current.state.playing_card is not None
                if current.state.playing_card.guid != _action.card_guid:
                    raise Plan2SubmittedPhysicalSettlementError(
                        "submitted PLAY retained a different playing-card GUID"
                    )
                if submitted_transition is None:
                    submitted_transition = current
                    self._persist_submitted_pending_play(
                        before,
                        _action,
                        current,
                    )
            settled = bool(
                current.state.is_native_actionable_settled
                or _plan2_state_is_terminal(current.state)
            )
            if not settled:
                stable = native_transition()
                if stable is not None:
                    return stable
            transitional_card = bool(
                _action.kind == "play"
                and resolver is not None
                and current.state.playing_card is not None
                and current.state.playing_card.guid == _action.card_guid
                and current.state.root_runtime is not None
                and not current.state.root_runtime.command_list_is_empty
            )
            command_rows = (
                None
                if current_runtime is None
                else current_runtime.command_list.to_value()
            )
            retained_drink_command_ids = tuple(
                row["_playingDrink"]["_id"]
                for row in command_rows
                if isinstance(row, Mapping)
                and isinstance(row.get("_playingDrink"), Mapping)
                and isinstance(row["_playingDrink"].get("_id"), str)
                and bool(row["_playingDrink"].get("_id"))
            ) if isinstance(command_rows, list) else ()
            transitional_drink = bool(
                _action.kind == "drink"
                and resolver is not None
                and isinstance(getattr(_action, "drink_id", None), str)
                and bool(retained_drink_command_ids)
                and set(retained_drink_command_ids)
                == {getattr(_action, "drink_id")}
            )
            if not settled and not transitional_card and not transitional_drink:
                prior = None
                last_replay_failure = None
                continue
            if not self.require_duplicate_reads or prior == current:
                if settled:
                    if (
                        _action.kind == "play"
                        and submitted_transition is not None
                        and proven_settled != current
                    ):
                        try:
                            if self.settled_pending_play_sink is not None:
                                self.settled_pending_play_sink(
                                    before,
                                    _action,
                                    submitted_transition,
                                    current,
                                    logical_before,
                                    prior_replay,
                                )
                            elif (
                                self.physical_settlement_prover is not None
                                and before.state.is_native_actionable_settled
                            ):
                                self.physical_settlement_prover(
                                    before,
                                    _action,
                                    submitted_transition,
                                    current,
                                )
                        except Plan2SubmittedPhysicalSettlementError:
                            raise
                        except Exception as error:
                            raise Plan2SubmittedPhysicalSettlementError(
                                "submitted PLAY physical settlement proof failed:"
                                f"{type(error).__name__}:{error}"
                            ) from error
                        proven_settled = current
                    # Native settlement + the physical prover is the action
                    # authority. HUD stability belongs to the separate
                    # pre-dispatch surface gate and cannot veto exact S'.
                    return Plan2StableSettledEvidence(
                        current,
                        current,
                        retained_action_replay=retained_action_replay,
                    )
                assert resolver is not None
                if (
                    retained_action_replay is not None
                    and retained_action_replay.persisted_transition == current
                ):
                    # The immutable retained SHA was already replayed exactly.
                    # Do not rebuild the Master catalog/search on every poll
                    # while a non-production caller waits for physical idle.
                    if self.return_exact_retained_replay:
                        if _action.kind == "play":
                            self._persist_accepted_pending_play(
                                before,
                                _action,
                                current,
                                retained_action_replay,
                            )
                        return Plan2StableSettledEvidence(
                            current,
                            current,
                            completed_card_replay=retained_action_replay,
                        )
                    prior = current
                    continue
                if last_replay_failure == current:
                    # The same immutable ExamSave SHA cannot produce a
                    # different replay result.  Wait cheaply for the game to
                    # publish a new state; never rerun the solver for every
                    # polling sample.
                    prior = current
                    continue
                try:
                    replay_result = resolver(
                        before,
                        current,
                        _action,
                    )
                except (OSError, TypeError, ValueError, RuntimeError) as error:
                    # A retained command queue can be published immediately
                    # before the game commits the next settled (or terminal)
                    # ExamSave.  HUD capture/OCR is only supplemental evidence
                    # for replaying that queue; it must not abort the waiter
                    # while a terminal ExamSave may still be arriving.
                    last_error = error
                    last_replay_failure = current
                    prior = current
                    continue
                pending_result = classify_pending_replay_result(replay_result)
                replay = pending_result.replay
                issues = pending_result.issues
                if pending_result.disposition is not PendingReplayDisposition.REPLAYED:
                    issue_codes = tuple(
                        getattr(value, "code", "invalid-replay-result")
                        for value in issues
                    )
                    detail = ",".join(
                        getattr(value, "code", "invalid-replay-result")
                        + (
                            ":" + getattr(value, "detail", "")
                            if getattr(value, "detail", "")
                            else ""
                        )
                        for value in issues
                    ) or "completed-card-replay-unavailable"
                    # A settled bootstrap may deliberately leave an
                    # unsupported game-owned item listener outside the local
                    # horizon.  Its retained command queue is still exact
                    # action evidence, but cannot be promoted to a logical
                    # replay.  Wait for the physical command queue to drain;
                    # the next settled ExamSave then owns every scalar/status
                    # result and the ordinary GUID/runtime validator proves
                    # the submitted PLAY.  Other replay failures remain hard
                    # failures under the duplicate-read production contract.
                    external_item_wait = bool(
                        pending_result.disposition
                        is PendingReplayDisposition.WAIT_EXTERNAL_SETTLEMENT
                    )
                    if (
                        self.require_duplicate_reads
                        and not external_item_wait
                        and submitted_transition is None
                    ):
                        raise RuntimeError(detail)
                    last_error = RuntimeError(detail)
                    last_replay_failure = current
                    prior = current
                    continue
                retained_action_replay = replay
                if self.return_exact_retained_replay:
                    if (
                        submitted_transition is not None
                        and self._runtime_action_settlement_watcher is not None
                    ):
                        from .runtime_action_settlement_watcher import (
                            RuntimeActionSettlementWatcherError,
                        )

                        try:
                            receipt = (
                                self._runtime_action_settlement_watcher
                                .watch_transition(timeout_seconds=1.0)
                            )
                        except RuntimeActionSettlementWatcherError:
                            receipt = None
                        if receipt is not None:
                            self._latest_runtime_action_settlement = receipt
                            if (
                                receipt.source_record == "transition"
                                and receipt.state_after is not None
                            ):
                                self._pending_runtime_transition = receipt
                                stable = native_transition()
                                if stable is not None:
                                    return stable
                    if _action.kind == "play":
                        self._persist_accepted_pending_play(
                            before,
                            _action,
                            current,
                            replay,
                        )
                    # Production installs a Maa SkipRound/CAS surface gate
                    # before every following external action.  Once native
                    # command replay proves this submitted PLAY/DRINK exactly,
                    # return its logical transition immediately; the shared
                    # gate owns the short animation/readiness wait and no
                    # duplicate input can be sent from this retained save.
                    return Plan2StableSettledEvidence(
                        current,
                        current,
                        completed_card_replay=replay,
                    )
                # A replay proves the submitted transaction, but the game is
                # still executing its command queue.  Never feed that logical
                # state back into another input cycle.  Keep polling until the
                # physical save is idle (possibly on the next turn); timeout
                # remains restart-safe through the retained journal path.
                prior = current
                continue
            prior = current
        if submitted_transition is not None:
            # The first replay may have sampled an in-animation HUD and is
            # deliberately cached for the immutable ExamSave during polling.
            # At the final boundary, retry that same exact resolver once so a
            # fresh HUD capture can prove the retained action without another
            # Maa input. Exceptions and identity mismatches remain fatal.
            if resolver is not None:
                final_result = resolver(
                    before,
                    submitted_transition,
                    _action,
                )
                final_pending = classify_pending_replay_result(final_result)
                if (
                    final_pending.disposition
                    is PendingReplayDisposition.REPLAYED
                ):
                    final_replay = final_pending.replay
                    assert final_replay is not None
                    if (
                        final_replay.persisted_before != before
                        or final_replay.persisted_transition
                        != submitted_transition
                        or final_replay.action != _action
                    ):
                        raise ValueError("final pending PLAY replay identity differs")
                    if _action.kind == "play":
                        self._persist_accepted_pending_play(
                            before,
                            _action,
                            submitted_transition,
                            final_replay,
                        )
                    return Plan2StableSettledEvidence(
                        submitted_transition,
                        submitted_transition,
                        completed_card_replay=final_replay,
                    )
            raise Plan2SubmittedPhysicalSettlementTimeout(
                "submitted PLAY command queue did not physically settle"
            )
        detail = "" if last_error is None else f":{type(last_error).__name__}:{last_error}"
        raise RuntimeError("next stable settled ExamSaveData was not observed" + detail)


def _read_plan2_card_history_observation(
    evidence: Any,
    *,
    source_prefix: str,
):
    """Read one mode-correct HUD projection for a retained Plan2 card.

    Lesson and audition screens are different layouts.  ExamSave ``exam_type``
    is the sole router; neither reader is allowed to fall through into the
    other.  The result is the common card-history observation consumed by
    both in-process replay and restart recovery.
    """

    from .controller_client import send_command
    from .exam_screen import (
        _active_logic_status_rows,
        _recognize_exam_logic_status_row,
        read_exam_screen_path,
    )
    from .lesson_screen import read_lesson_screen_path
    from .live_source import _live_text_recognizer
    from .plan2_card_history import (
        PLAN2_HUD_TURNS_RAW_REMAIN_V2,
        Plan2CardHistoryObservation,
        Plan2CardHistoryObservationAuthority,
    )

    capture = dict(send_command("capture_screen_once", timeout=15.0))
    raw_path = capture.get("png_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise RuntimeError("Plan2 HUD capture did not return png_path")
    image_path = Path(raw_path)
    recognizer = _live_text_recognizer()
    runtime = evidence.state.root_runtime
    opaque = None if runtime is None else runtime.opaque_fields.to_value()
    command_rows = (
        [] if runtime is None else runtime.command_list.to_value()
    )
    if not isinstance(command_rows, list):
        raise ValueError("Plan2 commandList must be an array")
    # A retained queue is serialized before its direct effects are committed.
    # The lesson HUD can already render ``6+1`` while state.extra_turn is
    # still zero. Count only the exact queued native ExtraTurn effect; no
    # colour or plus-marker identity participates.
    pending_extra_turns = sum(
        1
        for row in command_rows
        if isinstance(row, Mapping)
        and isinstance(row.get("_playEffect"), Mapping)
        and row["_playEffect"].get("_id") == "e_effect-exam_extra_turn"
    )
    # The large HUD counter and LocalSave ``remainTurn`` are the same total:
    # both already include every materialized ``extraTurn``.  Adding the Save
    # field again double-counted Final T13 as 2 while both the image and Save
    # said 1.  Only an ExtraTurn still present in the retained pre-effect queue
    # needs to be combined with the large-number OCR.
    visible_extra_turns = pending_extra_turns
    lesson_screen = None
    observation_source = source_prefix
    score_authoritative = True
    if evidence.state.exam_type == 0:
        screen = read_lesson_screen_path(image_path, recognizer)
        lesson_screen = screen
        base_parameter = (
            None
            if not isinstance(opaque, Mapping)
            else opaque.get("produceTargetParameter")
        )
        if (
            not isinstance(base_parameter, bool)
            and isinstance(base_parameter, int)
            and screen.lesson_parameter >= base_parameter
        ):
            score = screen.lesson_parameter - base_parameter
            observation_source += ":hud-score"
        else:
            # Animation frames and some localized layouts can temporarily
            # hide or misread the cumulative lesson parameter.  ExamSave is
            # already the authoritative gameplay state, so a decorative HUD
            # read must never block the next Maa action.  Keep the OCR values
            # that are actually visible, but source score from Save here.
            save_score = getattr(evidence.state, "score", None)
            if (
                isinstance(save_score, bool)
                or not isinstance(save_score, int)
                or save_score < 0
            ):
                raise ValueError("Plan2 lesson has no Save score authority")
            score = save_score
            score_authoritative = False
            observation_source += ":save-score"
        turns_remaining = screen.turns_remaining + visible_extra_turns
        stamina = screen.stamina
        block = screen.block
    elif evidence.state.exam_type == 1:
        # Card effects can temporarily cover the expanded player ranking tile
        # even though ExamSave has already recorded the submitted PLAY.  This
        # observation is read-only, so wait on that narrow visual condition
        # instead of turning one animation frame into a run-level hard stop.
        # The final path is retained below as the exact observation source.
        for read_attempt in range(4):
            try:
                screen = read_exam_screen_path(image_path, recognizer)
                break
            except ValueError:
                if read_attempt >= 3:
                    raise
                time.sleep(1.0)
                capture = dict(send_command("capture_screen_once", timeout=15.0))
                retry_path = capture.get("png_path")
                if not isinstance(retry_path, str) or not retry_path:
                    raise RuntimeError(
                        "Plan2 HUD retry capture did not return png_path"
                    )
                image_path = Path(retry_path)
        score = screen.player_score
        turns_remaining = screen.turns_remaining + visible_extra_turns
        stamina = screen.stamina
        block = screen.block
    else:
        raise ValueError(f"unsupported Plan2 exam_type: {evidence.state.exam_type}")

    review = None
    has_review = False
    has_aggressive = False
    if isinstance(opaque, Mapping):
        status = opaque.get("status")
        references = opaque.get("references")
        active = status.get("_effectList") if isinstance(status, Mapping) else None
        refs = references.get("RefIds") if isinstance(references, Mapping) else None
        if isinstance(active, list) and isinstance(refs, list):
            active_rids = {
                value.get("rid")
                for value in active
                if isinstance(value, Mapping)
            }
            has_review = any(
                isinstance(value, Mapping)
                and value.get("rid") in active_rids
                and isinstance(value.get("type"), Mapping)
                and value["type"].get("class") == "ReviewStatusEffect"
                for value in refs
            )
            has_aggressive = any(
                isinstance(value, Mapping)
                and value.get("rid") in active_rids
                and isinstance(value.get("type"), Mapping)
                and value["type"].get("class") == "AggressiveStatusEffect"
                for value in refs
            )
        if has_review and lesson_screen is not None and not has_aggressive:
            # A single compact lesson status can occupy the upper row even
            # when its semantic type is Review. ExamSave proves the class;
            # use the sole visible numeric value rather than its row index.
            candidates = tuple(
                value
                for value in (
                    lesson_screen.good_impression,
                    lesson_screen.motivation,
                )
                if isinstance(value, int) and not isinstance(value, bool)
            )
            if len(candidates) == 1 and not has_aggressive:
                review = candidates[0]
            elif lesson_screen.good_impression is not None:
                review = lesson_screen.good_impression
        if has_review and review is None and not has_aggressive:
            with Image.open(image_path.resolve()) as image:
                rows = _active_logic_status_rows(image)
                if rows:
                    reading, _ = _recognize_exam_logic_status_row(
                        image, recognizer, rows[0]
                    )
                    digits = "".join(value for value in reading.text if value.isdigit())
                    if digits:
                        review = int(digits)
    return Plan2CardHistoryObservation(
        turns_remaining=turns_remaining,
        score=score,
        stamina=stamina,
        block=block,
        review=review,
        # ``_remainCanPlayCardCount`` on effect commands is captured before
        # MovePlayCard spends the accepted play.  It is command context, not
        # an after-state HUD reading, so the native replay remains authority.
        plays_remaining=None,
        source=(
            f"{observation_source}:{PLAN2_HUD_TURNS_RAW_REMAIN_V2}:"
            f"{image_path.name}"
        ),
        score_authoritative=score_authoritative,
        authority=Plan2CardHistoryObservationAuthority.DIAGNOSTIC,
    )


def _plan2_local_save_drink_history_observation(
    evidence: Any,
    *,
    source_prefix: str,
):
    """Build the source-only DRINK replay marker without reading the HUD.

    Retained-drink recovery proves every semantic field from LocalSave, the
    exact native command queue, Master, and the ordered receipt.  Its
    observation parameter is used only to label the trace, so capturing/OCRing
    the same state here adds no authority and can only turn a valid commit into
    a timeout.
    """

    from .plan2_card_history import (
        Plan2CardHistoryObservation,
        Plan2CardHistoryObservationAuthority,
    )

    state = evidence.state
    return Plan2CardHistoryObservation(
        turns_remaining=state.remain_turn,
        score=state.score,
        stamina=state.stamina,
        block=state.block,
        source=f"{source_prefix}:{evidence.source_sha256}",
        score_authoritative=False,
        authority=Plan2CardHistoryObservationAuthority.DIAGNOSTIC,
    )


def _default_plan2_decision_orchestrator_factory(
    context: InitialRegularPlan2ExamContext,
):
    from .plan2_native_unattended_loop import (
        bind_plan2_native_exam_save_orchestrator,
    )
    from .plan2_native_exam_save_orchestrator import (
        Plan2NativeExamSaveDependencies,
    )
    from .plan2_strategy_runtime import (
        build_plan2_production_strategy_planner,
    )

    planner = build_plan2_production_strategy_planner(
        context.idol_card_id,
        context.produce_id,
    )

    orchestrator = bind_plan2_native_exam_save_orchestrator(
        draw_count=context.draw_count,
        hand_limit=context.hand_limit,
        dependencies=Plan2NativeExamSaveDependencies(planner=planner),
    )
    # Observation-only sidecar: this consumes the exact candidate provider
    # exposed by the bound planner and never participates in action choice or
    # input dispatch.
    try:
        from .nia_inner_transition_collector import (
            NiaInnerTransitionCollector,
            build_plan2_native_enumerator_candidate_provider,
        )
        from .nia_legal_decision_dataset import NiaLegalDecisionCollector

        candidate_provider = build_plan2_native_enumerator_candidate_provider(
            orchestrator
        )
        run_binding_id = None
        try:
            from .run_identity import load_active_run

            active_run = load_active_run()
        except (OSError, TypeError, ValueError):
            active_run = None
        if (
            active_run is not None
            and active_run.idol_card_id == context.idol_card_id
            and active_run.produce_id == context.produce_id
        ):
            run_binding_id = active_run.run_id
        collector = NiaInnerTransitionCollector(
            candidate_provider,
            run_binding_id=run_binding_id,
        )
        legal_decision_collector = NiaLegalDecisionCollector(
            candidate_provider,
            run_binding_id=run_binding_id,
        )
    except (ImportError, OSError, TypeError, ValueError) as error:
        setattr(
            orchestrator,
            "nia_inner_transition_collector_error",
            f"{type(error).__name__}:{error}",
        )
    else:
        setattr(orchestrator, "nia_inner_transition_collector", collector)
        setattr(
            orchestrator,
            "nia_legal_decision_collector",
            legal_decision_collector,
        )
        setattr(
            orchestrator,
            "nia_inner_transition_candidate_provider",
            "plan2_native_candidate_provider",
        )
        setattr(orchestrator, "nia_training_run_binding_id", run_binding_id)
    if context.learned_policy_bundle_path is not None:
        from .policy_bundle import PolicyBundle

        return _bind_plan2_learned_policy_bundle(
            orchestrator,
            PolicyBundle.load(context.learned_policy_bundle_path),
        )
    return _bind_active_plan2_learned_policy(orchestrator)


def _bind_plan2_learned_policy_bundle(orchestrator: Any, bundle: Any):
    """Bind one explicit learned bundle, leaving Maa as the only executor."""

    try:
        from .canonical_training_labels import KNOWN_READINESS_V5_FLOWS
        from .exact_bc_live_canary import ExactBCCanaryPolicy
        from .plan2_exact_bc_canary_bridge import (
            bind_plan2_exact_bc_canary_orchestrator,
        )
        from .offline_rl_runtime_adapter import (
            build_offline_rl_artifact_selector,
        )
        runtime = bundle.payload.get("runtime")
        component = bundle.component("exact_exam_policy")
        if not (
            isinstance(runtime, Mapping)
            and runtime.get("mode") == "learned-policy"
            and runtime.get("default_enabled") is True
            and runtime.get("fallback") == "stop-on-no-learned-decision"
            and component.get("shadow_ready") is True
            and component.get("live_apply_allowed") is True
        ):
            return orchestrator
        try:
            offline_component = bundle.component("offline_rl_policy")
        except KeyError:
            offline_rl = None
        else:
            try:
                offline_rl = build_offline_rl_artifact_selector(
                    offline_component,
                    project_root=bundle.project_root,
                )
            except (FileNotFoundError, OSError, TypeError, ValueError):
                # A broken optional RL artifact is an RL abstention.  The
                # already-bound, live Exact BC component remains the sole
                # learned fallback; Maa still never becomes a policy.
                offline_rl = None
        return bind_plan2_exact_bc_canary_orchestrator(
            orchestrator,
            bundle=bundle,
            policy=ExactBCCanaryPolicy(
                enabled=True,
                allowed_flows=tuple(sorted(KNOWN_READINESS_V5_FLOWS)),
                # In the formal RL→BC chain, BC is the final learned fallback
                # and therefore ranks every non-tied legal set.  Confidence
                # thresholds belong to the one-step canary harness only.
                minimum_probability=0.0,
                minimum_margin=0.0,
            ),
            offline_rl=offline_rl,
        )
    except (ImportError, KeyError, OSError, TypeError, ValueError):
        # An inactive/malformed learned bundle must not silently become a
        # partially wired decision owner.  Until activation, keep the current
        # explicit execution mode unchanged.
        return orchestrator


def _bind_active_plan2_learned_policy(orchestrator: Any):
    """Bind the active Exact BC decision owner, leaving Maa as executor only."""

    try:
        from .policy_bundle import PolicyBundle

        bundle = PolicyBundle.load()
    except (ImportError, OSError, TypeError, ValueError):
        return orchestrator
    return _bind_plan2_learned_policy_bundle(orchestrator, bundle)


def _default_plan2_maa_dependencies_factory(
    path: Path,
    context: InitialRegularPlan2ExamContext,
    loader: Plan2EvidenceLoader,
    orchestrator: Any,
):
    from .audition_rules import load_audition_rules
    from .live_source import make_live_card_frame_analyzer
    from .plan2_native_local_save_bootstrap import (
        load_plan2_native_exam_setting_authority,
    )
    from .plan2_native_maa_action_executor import (
        MaaPostClickHudStabilityGate,
        build_plan2_maa_background_dependencies,
    )
    from .plan2_card_history import (
        Plan2CardHistoryObservation,
        Plan2CardHistoryIssue,
        Plan2CardHistoryReplayResult,
        replay_completed_plan2_card_history,
        replay_next_completed_plan2_card_history,
    )
    from .plan2_native_program_catalog import (
        compile_plan2_native_program_catalog,
    )
    from .plan2_replay_journal import _journal_replay_root

    cached_catalog: list[Any] = [compile_plan2_native_program_catalog().catalog]
    cached_screen_decision: list[tuple[Any, Any]] = []
    cached_logical_decision: list[tuple[Any, Any, Any]] = []
    from .audition_native_ordered_zones import NativeOrderedCardInstance

    def observe_completed_card(after: Any) -> Any:
        """Read the already-rendered HUD once; no preview/artwork validation."""
        return _read_plan2_card_history_observation(
            after,
            source_prefix="maa-hud",
        )

    def resolve_completed_card(before: Any, after: Any, action: Any):
        if getattr(action, "kind", None) == "drink":
            from .plan2_card_history import recover_retained_plan2_drink_history

            decision = orchestrator(before)
            root = _journal_replay_root(decision)
            if (
                root is None
                or decision.best_action != action
                or decision.predicted_after_state is None
            ):
                return Plan2CardHistoryReplayResult(
                    None,
                    (
                        Plan2CardHistoryIssue(
                            "plan2-card-history-decision-context-unavailable"
                        ),
                    ),
                )
            if not cached_catalog:
                cached_catalog.append(compile_plan2_native_program_catalog().catalog)

            return recover_retained_plan2_drink_history(
                after,
                observation=_plan2_local_save_drink_history_observation(
                    after,
                    source_prefix="maa-drink-local-save",
                ),
                expected_action=action,
                persisted_before=before,
                before_horizon=root,
                catalog=cached_catalog[0],
            )
        decision = orchestrator(before)
        replay_root = _journal_replay_root(decision)
        if (
            replay_root is None
            or decision.predicted_after_state is None
            or decision.best_action != action
        ):
            return Plan2CardHistoryReplayResult(
                None,
                (
                    Plan2CardHistoryIssue(
                        "plan2-card-history-decision-context-unavailable"
                    ),
                ),
            )
        if not cached_catalog:
            cached_catalog.append(compile_plan2_native_program_catalog().catalog)
        result = replay_completed_plan2_card_history(
            before,
            after,
            action,
            before_horizon=replay_root,
            catalog=cached_catalog[0],
            observation=observe_completed_card(after),
        )
        return result

    logical_orchestrator = getattr(
        orchestrator, "logical_horizon_orchestrator", None
    )

    def resolve_next_completed_card(
        before: Any,
        after: Any,
        action: Any,
        logical_before: Any,
        prior_replay: Any,
    ):
        if getattr(action, "kind", None) == "drink":
            from .plan2_card_history import recover_retained_plan2_drink_history

            if not callable(logical_orchestrator):
                return Plan2CardHistoryReplayResult(
                    None,
                    (
                        Plan2CardHistoryIssue(
                            "plan2-card-history-logical-planner-unbound"
                        ),
                    ),
                )
            decision = logical_orchestrator(logical_before)
            replay_root = _journal_replay_root(decision)
            if (
                replay_root is None
                or decision.best_action != action
                or decision.predicted_after_state is None
            ):
                return Plan2CardHistoryReplayResult(
                    None,
                    (
                        Plan2CardHistoryIssue(
                            "plan2-card-history-decision-context-unavailable"
                        ),
                    ),
                )
            if not cached_catalog:
                cached_catalog.append(compile_plan2_native_program_catalog().catalog)

            return recover_retained_plan2_drink_history(
                after,
                observation=_plan2_local_save_drink_history_observation(
                    after,
                    source_prefix="maa-logical-drink-local-save",
                ),
                expected_action=action,
                persisted_before=before,
                before_horizon=replay_root,
                catalog=cached_catalog[0],
            )
        if not callable(logical_orchestrator):
            return Plan2CardHistoryReplayResult(
                None,
                (Plan2CardHistoryIssue("plan2-card-history-logical-planner-unbound"),),
            )
        decision = logical_orchestrator(logical_before)
        replay_root = _journal_replay_root(decision)
        if (
            replay_root is None
            or decision.best_action != action
            or decision.predicted_after_state is None
        ):
            return Plan2CardHistoryReplayResult(
                None,
                (Plan2CardHistoryIssue("plan2-card-history-decision-context-unavailable"),),
            )
        if not cached_catalog:
            cached_catalog.append(compile_plan2_native_program_catalog().catalog)
        result = replay_next_completed_plan2_card_history(
            before,
            replay_root,
            after,
            action,
            prior_replay=prior_replay,
            catalog=cached_catalog[0],
            observation=observe_completed_card(after),
        )
        return result

    def prove_physical_settlement(
        before: Any,
        action: Any,
        submitted: Any,
        settled: Any,
    ) -> object:
        if not cached_catalog:
            cached_catalog.append(compile_plan2_native_program_catalog().catalog)
        return _prove_plan2_submitted_play_physical_settlement(
            before,
            action,
            submitted,
            settled,
            catalog=cached_catalog[0],
        )

    poller = _InitialRegularPlan2EvidencePoller(
        path,
        loader,
        max_samples=96,
        completed_card_replay_resolver=resolve_completed_card,
        completed_card_chain_replay_resolver=resolve_next_completed_card,
        physical_settlement_prover=prove_physical_settlement,
        visual_stability_gate=MaaPostClickHudStabilityGate(),
        require_duplicate_reads=True,
        # Exact replay may return its logical S' once the post-click HUD is
        # stable.  If replay is unavailable, the same waiter falls back to
        # native settlement + physical proof without another input.
        return_exact_retained_replay=True,
    )

    def analyzer_for(evidence: Any, horizon: Any, card: Any, *, card_x: int | None):
        step_type = _plan2_step_type(evidence.state.step_type_value)
        lesson = horizon.exam_mode.is_lesson
        clear_target = perfect_target = None
        gimmick_group_id = None
        if lesson:
            setting = load_plan2_native_exam_setting_authority(
                evidence.state.setting_id
            )
            runtime = evidence.state.root_runtime
            opaque = None if runtime is None else runtime.opaque_fields.to_value()
            if not isinstance(opaque, Mapping):
                raise ValueError("Plan2 lesson screen projection has no root runtime")
            clear_target = opaque.get("clearBorder")
            perfect_target = opaque.get("limitBorder")
            if (
                isinstance(clear_target, bool)
                or not isinstance(clear_target, int)
                or clear_target < 0
                or isinstance(perfect_target, bool)
                or not isinstance(perfect_target, int)
                or perfect_target < clear_target
            ):
                raise ValueError("Plan2 lesson screen targets are invalid")
            turn_end_stamina_recovery = setting.turn_end_stamina_recovery
            # The native horizon already applied the exact LocalSave/Master
            # gimmick schedule.  Screen verification compares the resulting
            # HUD directly and must not execute that schedule a second time.
            gimmick_group_id = "none-observed"
        else:
            rules = load_audition_rules(
                context.idol_card_id,
                produce_id=context.produce_id,
                step_type=step_type,
                number=context.stage_number,
            )
            turn_end_stamina_recovery = rules.turn_end_stamina_recovery
        expected_screen = dict(
            _plan2_expected_screen_state(
                horizon,
                evidence,
                turn_end_stamina_recovery=turn_end_stamina_recovery,
            )
        )
        # ``plays_remaining`` is not visible in either lesson or audition HUD.
        # The legacy analyzer validates an ordinary selectable LogicExamState,
        # so a zero-play logical horizon uses one only inside OCR matching; the
        # typed logical reconciliation below retains the authoritative zero.
        if horizon.plays_remaining == 0:
            expected_screen["plays_remaining"] = 1
        return make_live_card_frame_analyzer(
            mode="lesson" if lesson else "exam",
            expected_card_id=card.card_id,
            expected_upgrade=card.effective_upgrade,
            expected_after=expected_screen,
            expected_card_x=card_x,
            expected_ordered_hand=tuple(
                (
                    hand_card.card_id,
                    hand_card.effective_upgrade,
                    tuple(hand_card.support_upgrade_ids),
                )
                for hand_card in horizon.zones.hand
            ),
            clear_target=clear_target,
            lesson_perfect_target=perfect_target,
            gimmick_group_id=gimmick_group_id,
            idol_card_id=context.idol_card_id,
            produce_id=context.produce_id,
            step_type=step_type,
            stage_number=context.stage_number,
        )

    def current_screen_analyzer(evidence: Any):
        decision = orchestrator(evidence)
        if decision.bootstrap is None or decision.bootstrap.state is None:
            raise ValueError("Plan2 current screen has no bootstrap state")
        cached_screen_decision[:] = [(evidence, decision)]
        hand = evidence.state.zones.hand
        if not hand:
            raise ValueError("Plan2 current screen has no visible Hand")
        return analyzer_for(evidence, decision.bootstrap.state, hand[0], card_x=None)

    def play_analyzer(action: Any, evidence: Any, binding: Any):
        decision = (
            cached_screen_decision[0][1]
            if cached_screen_decision
            and cached_screen_decision[0][0] == evidence
            else orchestrator(evidence)
        )
        if decision.best_action != action or decision.predicted_after_state is None:
            raise ValueError("Plan2 PLAY no longer matches the current decision")
        hand = evidence.state.zones.hand
        index = next(
            (
                slot
                for slot, card in enumerate(hand)
                if card.guid == action.card_guid
            ),
            None,
        )
        if index is None:
            raise ValueError("Plan2 PLAY GUID is absent from the current Hand")
        box = binding.hand_boxes[index]
        return analyzer_for(
            evidence,
            decision.predicted_after_state,
            hand[index],
            card_x=(box[0] + box[2]) // 2,
        )

    def logical_current_screen_analyzer(evidence: Any, horizon: Any):
        if not callable(logical_orchestrator):
            raise ValueError("Plan2 logical continuation planner is not configured")
        logical_decision = logical_orchestrator(horizon)
        cached_logical_decision[:] = [(evidence, horizon, logical_decision)]
        hand = horizon.zones.hand
        if not hand:
            raise ValueError("Plan2 logical current screen has no visible Hand")
        return analyzer_for(evidence, horizon, hand[0], card_x=None)

    def logical_play_analyzer(
        action: Any,
        evidence: Any,
        binding: Any,
        logical_before: Any,
    ):
        if not callable(logical_orchestrator):
            raise ValueError("Plan2 logical continuation planner is not configured")
        decision = (
            cached_logical_decision[0][2]
            if cached_logical_decision
            and cached_logical_decision[0][0] == evidence
            and cached_logical_decision[0][1] == logical_before
            else logical_orchestrator(logical_before)
        )
        if decision.best_action != action or decision.predicted_after_state is None:
            raise ValueError("Plan2 logical PLAY no longer matches the decision")
        hand = logical_before.zones.hand
        index = next(
            (slot for slot, card in enumerate(hand) if card.guid == action.card_guid),
            None,
        )
        if index is None:
            raise ValueError("Plan2 logical PLAY GUID is absent from logical Hand")
        box = binding.hand_boxes[index]
        return analyzer_for(
            evidence,
            decision.predicted_after_state,
            hand[index],
            card_x=(box[0] + box[2]) // 2,
        )

    def play_input_authorizer_factory(
        expected_evidence: Any,
        logical_before: Any,
        prior_replay: Any,
        runtime_before_provenance: Any = None,
    ):
        # The verified click helper invokes this immediately before SELECT and
        # again immediately before the irreversible confirmation click.  A
        # changed ExamSave means the previously reconciled Hand is stale.
        def authorize(_capture: Mapping[str, Any]) -> None:
            current = loader(path)
            runtime_base_transition = False
            if runtime_before_provenance is not None:
                from .runtime_action_state_evidence import (
                    RuntimeActionStateProvenance,
                )

                if not isinstance(
                    runtime_before_provenance,
                    RuntimeActionStateProvenance,
                ):
                    raise TypeError("runtime before provenance must be typed")
                same_runtime_stage = _plan2_runtime_stage_identity_matches(
                    expected_evidence,
                    current,
                )
                runtime_base_transition = bool(
                    same_runtime_stage
                    and (
                        current.source_sha256
                        == runtime_before_provenance.base_source_sha256
                        # The DLL state is serialized canonically while the
                        # same state later written by the game has a different
                        # encrypted-file SHA.  Equal typed state is therefore
                        # the authoritative disk catch-up for this stage, not
                        # an input race.
                        or current.state == expected_evidence.state
                    )
                )
            confirmation_guid = _capture.get(
                "plan2_confirmation_playing_card_guid"
            )
            confirmation_transition = bool(
                isinstance(confirmation_guid, str)
                and confirmation_guid
                and current.run_id == expected_evidence.run_id
                and current.source_path == expected_evidence.source_path
                and current.source_type == expected_evidence.source_type
                and current.state.character_id
                == expected_evidence.state.character_id
                and current.state.setting_id == expected_evidence.state.setting_id
                and current.state.exam_type == expected_evidence.state.exam_type
                and current.state.step_type_value
                == expected_evidence.state.step_type_value
                and current.state.playing_card is not None
                and current.state.playing_card.guid == confirmation_guid
                and current.state.root_runtime is not None
                and not current.state.root_runtime.command_list_is_empty
            )
            if (
                current != expected_evidence
                and not confirmation_transition
                and not runtime_base_transition
            ):
                from .plan2_native_maa_action_executor import (
                    Plan2InputStateAdvanced,
                )

                raise Plan2InputStateAdvanced(
                    "Plan2 ExamSaveData changed after screen reconciliation"
                )
            if (
                logical_before is not None
                and not confirmation_transition
                and not runtime_base_transition
                and (
                prior_replay is None
                or prior_replay.persisted_transition != current
                or prior_replay.logical_after != logical_before
                )
            ):
                raise ValueError("Plan2 logical replay authority changed")

        return authorize

    def drink_confirmation_effect_policy(
        evidence: Any,
        action: Any,
        logical_before: Any,
    ) -> bool:
        """Authorize the warning's Confirm only for an exact useful effect."""

        from .plan2_native_horizon import (
            Plan2NativeDrinkAction,
            plan2_native_drink_transition_has_effect,
            simulate_plan2_native_action_lifecycle,
        )

        if not isinstance(action, Plan2NativeDrinkAction):
            raise TypeError("drink confirmation policy requires a drink action")
        if logical_before is None:
            decision = orchestrator(evidence)
            root = _journal_replay_root(decision)
        else:
            if not callable(logical_orchestrator):
                raise ValueError("Plan2 logical continuation planner is not configured")
            decision = logical_orchestrator(logical_before)
            root = _journal_replay_root(decision)
        if (
            root is None
            or decision.best_action != action
            or decision.predicted_after_state is None
        ):
            raise ValueError("drink confirmation no longer matches exact decision")
        if not cached_catalog:
            cached_catalog.append(compile_plan2_native_program_catalog().catalog)
        transition = simulate_plan2_native_action_lifecycle(
            root,
            action,
            cached_catalog[0],
        )
        if (
            not transition.supported
            or transition.after is None
            or transition.after != decision.predicted_after_state
        ):
            raise ValueError("drink confirmation Master transition is unavailable")
        return plan2_native_drink_transition_has_effect(
            root,
            action,
            transition,
        )

    def observe_generated_card_animation(
        evidence: Any,
        action: Any,
        logical_before: Any,
        prior_replay: Any,
        dispatch: Any,
    ):
        """Record two Maa frames for a Master-predicted card creation.

        This runs only after the irreversible input has been submitted.  The
        visual result is diagnostic early identity evidence; the next ExamSave
        still owns the native GUID and settlement decision.
        """

        del prior_replay, dispatch
        from .plan2_generated_card_animation import (
            master_generated_card_expectation_for_drink,
            master_generated_card_expectation_for_play,
            observe_plan2_generated_card_animation,
        )

        expectation = None
        if getattr(action, "kind", None) == "play":
            cards = (
                logical_before.zones.hand
                if logical_before is not None
                else evidence.state.zones.hand
            )
            source = next(
                (card for card in cards if card.guid == action.card_guid),
                None,
            )
            if source is None:
                return None
            native_source = (
                source
                if isinstance(source, NativeOrderedCardInstance)
                else NativeOrderedCardInstance.from_local_save(source)
            )
            expectation = master_generated_card_expectation_for_play(
                native_source,
                cached_catalog[0],
            )
            if expectation is not None:
                expectation = replace(expectation, source_id=action.card_guid)
        elif getattr(action, "kind", None) == "drink":
            expectation = master_generated_card_expectation_for_drink(
                action.drink_id,
                instance_id=action.instance_id,
            )
            if expectation is not None:
                expectation = replace(
                    expectation,
                    source_id=action.instance_id,
                )
        if expectation is None:
            return None
        return observe_plan2_generated_card_animation(
            action,
            expectation,
        )

    def resolve_submitted_drink_barrier(
        evidence: Any,
        action: Any,
        logical_before: Any,
        prior_replay: Any,
        dispatch: Any,
    ):
        """Advance one submitted DRINK while an older queue stays persisted.

        This is deliberately a one-PLAY barrier, not a claim that LocalSave
        published the second drink's own command queue.  The exact Maa receipt
        proves only that slot/open/use completed; Master owns the deterministic
        logical effect, and the next PLAY save must prove ordered inventory
        removal together with its requested card GUID.
        """

        from .plan2_card_history import (
            Plan2CompletedDrinkReplay,
        )
        from .plan2_native_horizon import (
            Plan2NativeDrinkAction,
            simulate_plan2_native_action_lifecycle,
        )
        from .plan2_replay_journal import (
            Plan2PendingDrinkDispatchWitness,
        )

        if (
            not isinstance(action, Plan2NativeDrinkAction)
            or not isinstance(prior_replay, Plan2CompletedDrinkReplay)
            or prior_replay.provisional_from_submitted_receipt
            or prior_replay.persisted_transition != evidence
            or prior_replay.logical_after != logical_before
        ):
            raise ValueError("submitted drink barrier authority is invalid")
        current = loader(path)
        if current != evidence:
            raise ValueError("submitted drink barrier ExamSave changed")
        Plan2PendingDrinkDispatchWitness.from_dispatch(
            action,
            dispatch.to_dict(),
        )
        if not callable(logical_orchestrator):
            raise ValueError("Plan2 logical continuation planner is not configured")
        decision = logical_orchestrator(logical_before)
        replay_root = _journal_replay_root(decision)
        if (
            replay_root != logical_before
            or decision.best_action != action
            or decision.predicted_after_state is None
        ):
            raise ValueError("submitted drink no longer matches logical decision")
        if not cached_catalog:
            cached_catalog.append(compile_plan2_native_program_catalog().catalog)
        transition = simulate_plan2_native_action_lifecycle(
            logical_before,
            action,
            cached_catalog[0],
        )
        if (
            not transition.supported
            or transition.after is None
            or transition.after != decision.predicted_after_state
        ):
            raise ValueError("submitted drink Master transition is unavailable")
        instance = logical_before.drink_runtime.resolve(
            action.slot_index,
            instance_id=action.instance_id,
            drink_id=action.drink_id,
        )
        return Plan2CompletedDrinkReplay(
            action=action,
            before=logical_before,
            after=transition.after,
            transition=transition,
            persisted_transition=evidence,
            command_effect_ids=tuple(value.effect_id for value in instance.effects),
            trace=(
                f"drink-history:action:{action.action_id}",
                "drink-history:provisional-submitted-receipt",
                "drink-history:next-action-barrier:play-only",
                *transition.trace,
            ),
            persisted_before=evidence,
            provisional_from_submitted_receipt=True,
        )

    return build_plan2_maa_background_dependencies(
        screen_analyzer_factory=current_screen_analyzer,
        play_analyzer_factory=play_analyzer,
        next_settled_evidence_waiter=poller.wait_next,
        logical_screen_analyzer_factory=logical_current_screen_analyzer,
        logical_play_analyzer_factory=logical_play_analyzer,
        logical_next_settled_evidence_waiter=poller.wait_next_logical,
        retained_settlement_waiter=poller.wait_retained_settlement,
        play_input_authorizer_factory=play_input_authorizer_factory,
        drink_confirmation_effect_policy=drink_confirmation_effect_policy,
        submitted_drink_replay_resolver=resolve_submitted_drink_barrier,
        post_input_observation_sink=observe_generated_card_animation,
        exam_save_minimal_verification=True,
    ), poller.initial


def _default_plan2_action_executor_factory(dependencies: Any):
    from .plan2_native_maa_action_executor import (
        Plan2MaaBackgroundActionExecutor,
    )

    return Plan2MaaBackgroundActionExecutor(dependencies)


def _default_plan2_bridge_factory(
    executor: Any,
    initial_reader: Callable[[Any], Any],
    *,
    initial_evidence: Any = None,
    initial_replay: Any = None,
):
    from .plan2_native_maa_action_executor import Plan2MaaUnattendedLoopBridge

    return Plan2MaaUnattendedLoopBridge(
        executor,
        initial_reader,
        initial_evidence=initial_evidence,
        initial_replay=initial_replay,
    )


def _default_plan2_loop_runner(**kwargs: Any):
    from .plan2_native_unattended_loop import run_plan2_native_unattended_loop

    return run_plan2_native_unattended_loop(**kwargs)


def initial_regular_plan2_exam_dependencies() -> InitialRegularPlan2ExamDependencies:
    """Return the lazy production composition without starting any runtime."""

    from .plan2_replay_journal import DEFAULT_PLAN2_REPLAY_JOURNAL_ROOT

    return InitialRegularPlan2ExamDependencies(
        evidence_loader=load_initial_regular_plan2_exam_evidence,
        decision_orchestrator_factory=_default_plan2_decision_orchestrator_factory,
        maa_dependencies_factory=_default_plan2_maa_dependencies_factory,
        action_executor_factory=_default_plan2_action_executor_factory,
        bridge_factory=_default_plan2_bridge_factory,
        loop_runner=_default_plan2_loop_runner,
        replay_journal_root=DEFAULT_PLAN2_REPLAY_JOURNAL_ROOT,
        outer_snapshot_reader=lambda path: read_produce_outer_local_save(
            path.parent
        ),
    )


def _plan2_terminal_exit_removed_exam_save(
    result: Any,
    path: Path,
    *,
    current_evidence: Any = None,
    outer_before: Any = None,
    outer_after: Any = None,
) -> str | None:
    """Recognize the normal PC lesson transition that removes ExamSaveData.

    The game can delete the inner save immediately after the final submitted
    action and publish the result page without ever leaving a phase-8 file for
    the waiter.  A predicted terminal is not enough: the sibling Produce save
    must contain a newly completed step for this exact stage.  Ordinary
    rejected actions and transient visual changes remain rejected.
    """

    if bool(getattr(result, "terminal_reached", False)):
        return None
    if str(getattr(result, "stop_reason", "")) != "action-rejected":
        return None
    records = tuple(getattr(result, "records", ()))
    if not records or Path(path).exists():
        return None
    record = records[-1]
    execution = getattr(record, "execution", None)
    detail = "" if execution is None else str(getattr(execution, "detail", ""))
    missing_after_submitted_input = bool(
        execution is not None
        and getattr(execution, "accepted", True) is False
        and detail.startswith("next-settled-evidence-failed:")
        and "FileNotFoundError" in detail
    )
    if not missing_after_submitted_input:
        return None
    # A lesson can hit its server/native LimitBorder by more than the bounded
    # solver predicted (for example, an outer-owned result multiplier).  The
    # PC then removes ExamSaveData immediately, so no terminal inner save can
    # be observed.  Accept that transition only when the sibling Produce play
    # log gained a new completed step of this exact ExamSave step type.  This
    # is LocalSave authority, not another screenshot/OCR confirmation.
    state = getattr(current_evidence, "state", None)
    step_type = getattr(state, "step_type_value", None)
    before_log_count = getattr(outer_before, "log_count", None)
    after_log_count = getattr(outer_after, "log_count", None)
    completed_steps = tuple(getattr(outer_after, "completed_steps", ()))
    if (
        type(step_type) is int
        and type(before_log_count) is int
        and type(after_log_count) is int
        and after_log_count > before_log_count
        and any(
            type(getattr(step, "log_index", None)) is int
            and getattr(step, "log_index") >= before_log_count
            and getattr(step, "step_type", None) == step_type
            for step in completed_steps
        )
    ):
        return "outer-completed-step-and-exam-save-removed"
    return None


def _plan2_pending_submitted_settlement_authority(
    result: Any,
    journal_sink: Any,
    receipt_on_entry: Any,
) -> Mapping[str, Any] | None:
    """Accept only the new durable receipt created by a timed-out PLAY.

    The action remains physically unsettled, so this does not manufacture an
    ``S'`` or append a replay row.  It only proves that Maa submitted the last
    recorded action and that the existing pending-play startup owner can take
    over on the next outer dispatch without another click.
    """

    if (
        journal_sink is None
        or str(getattr(result, "stop_reason", "")) != "action-rejected"
    ):
        return None
    records = tuple(getattr(result, "records", ()))
    if not records:
        return None
    record = records[-1]
    execution = getattr(record, "execution", None)
    action = getattr(record, "action", None)
    before = getattr(record, "evidence_before", None)

    from .audition_local_save_state import AuditionLocalSaveStateEvidence
    from .plan2_native_horizon import Plan2NativeAction
    from .plan2_native_unattended_loop import (
        ExamBoundaryDisposition,
        Plan2NativeActionExecution,
    )
    from .plan2_replay_journal import (
        load_plan2_pending_play_receipt,
        pending_play_transitional_owner_matches,
    )

    if (
        not isinstance(before, AuditionLocalSaveStateEvidence)
        or not isinstance(action, Plan2NativeAction)
        or action.kind != "play"
        or not isinstance(execution, Plan2NativeActionExecution)
        or execution.accepted
        or execution.replan
        or execution.boundary_disposition
        is not ExamBoundaryDisposition.SUBMITTED_PENDING
        or not execution.issues
        or getattr(record, "actual_next_evidence", None) is not None
    ):
        return None
    try:
        receipt = load_plan2_pending_play_receipt(journal_sink.pending_play_path)
    except (OSError, TypeError, ValueError):
        return None
    if (
        receipt is None
        or receipt == receipt_on_entry
        or receipt.state_after is not None
        or receipt.persisted_before != before
        or receipt.action != action
        or receipt.submitted_evidence == before
        or not pending_play_transitional_owner_matches(
            receipt,
            receipt.submitted_evidence,
        )
    ):
        return None
    witness = dict(receipt.dispatch_witness)
    return {
        "kind": "pending-submitted-settlement",
        "receipt_path": str(journal_sink.pending_play_path),
        "action_id": action.action_id,
        "card_guid": action.card_guid,
        "before_digest": before.digest(),
        "submitted_digest": receipt.submitted_evidence.digest(),
        "dispatch_witness": witness,
        "input_submitted": True,
        "next": "restart-safe-pending-play-recovery",
    }


def _plan2_retryable_advanced_settled_evidence(
    result: Any,
    path: Path,
    loader: Plan2EvidenceLoader,
) -> Any | None:
    """Return a newer same-stage settled save after a no-decision stop."""

    if str(getattr(result, "stop_reason", "")) != "decision-unavailable":
        return None
    records = tuple(getattr(result, "records", ()))
    if not records or not Path(path).exists():
        return None
    last = records[-1]
    prior = getattr(last, "actual_next_evidence", None) or getattr(
        last, "evidence_before", None
    )
    from .audition_local_save_state import AuditionLocalSaveStateEvidence

    if not isinstance(prior, AuditionLocalSaveStateEvidence):
        return None
    fresh = loader(Path(path))
    if not isinstance(fresh, AuditionLocalSaveStateEvidence):
        return None
    if fresh.source_sha256 == prior.source_sha256:
        return None
    before_state = prior.state
    after_state = fresh.state
    if (
        fresh.run_id != prior.run_id
        or fresh.step_context_id != prior.step_context_id
        or fresh.step_context_digest != prior.step_context_digest
        or fresh.session_transition_id != prior.session_transition_id
        or after_state.character_id != before_state.character_id
        or after_state.setting_id != before_state.setting_id
        or after_state.exam_type != before_state.exam_type
        or after_state.step_type_value != before_state.step_type_value
        or after_state.limit_turn != before_state.limit_turn
        or not after_state.is_native_actionable_settled
    ):
        return None
    return fresh


def _refresh_plan2_run_shadow_deck(
    context: InitialRegularPlan2ExamContext,
    evidence: Any,
) -> Mapping[str, Any] | None:
    """Refresh the advisory deck shadow from an exact settled ExamSave.

    Turn one prefers the narrow Hand+Deck baseline.  If a background
    transition has already moved cards into another settled zone, the same
    ExamSave's complete active-card universe is authoritative.  This never
    gates card play; it only lets later reward choices run deck-aware native
    counterfactuals instead of falling back to a generic archetype score.
    """

    from .audition_local_save_state import AuditionLocalSaveStateEvidence

    if not isinstance(evidence, AuditionLocalSaveStateEvidence):
        return None
    try:
        from .master_db import get_idol_profile
        from .run_identity import load_active_run, paths_for
        from .run_shadow import (
            apply_plan2_exam_save_active_deck_snapshot,
            apply_plan2_exam_save_deck_baseline,
            load_run_shadow,
            save_run_shadow,
        )

        active = load_active_run()
        if active is None or (
            active.idol_card_id != context.idol_card_id
            or active.produce_id != context.produce_id
            or active.character_id != evidence.state.character_id
        ):
            return None
        paths = paths_for(active)
        shadow = load_run_shadow(paths.shadow)
        if shadow is None:
            return None
        profile = get_idol_profile(context.idol_card_id)
        if profile is None or not profile.produce_card_id:
            return None
        source = Path(evidence.source_path)
        captured_at = source.stat().st_mtime
        authority = "typed-plan2-exam-save-turn-one"
        if evidence.state.current_turn == 1:
            try:
                updated = apply_plan2_exam_save_deck_baseline(
                    shadow,
                    evidence,
                    captured_at=captured_at,
                    required_card_id=profile.produce_card_id,
                )
            except ValueError:
                # A background transition can leave the runner at turn one
                # after cards have already entered Grave/Lost.  The settled
                # ExamSave still exposes the complete active card universe;
                # use that typed state instead of permanently losing the
                # deck authority for this audition.
                updated = apply_plan2_exam_save_active_deck_snapshot(
                    shadow,
                    evidence,
                    captured_at=captured_at,
                )
                authority = "typed-plan2-exam-save-active-card-universe"
        else:
            updated = apply_plan2_exam_save_active_deck_snapshot(
                shadow,
                evidence,
                captured_at=captured_at,
            )
            authority = "typed-plan2-exam-save-active-card-universe"
        if updated != shadow:
            save_run_shadow(updated, paths.shadow)
        return {
            "status": "refreshed" if updated != shadow else "unchanged",
            "authority": authority,
            "card_count": sum((updated.deck or {}).values()),
            "stack_count": len(updated.deck or {}),
        }
    except (OSError, TypeError, ValueError) as error:
        # Reward ranking is advisory.  Preserve the native card solver and
        # expose the reason in its report instead of blocking the exam.
        return {
            "status": "unavailable",
            "detail": f"{type(error).__name__}:{error}",
        }


def _compose_plan2_record_sinks(
    journal_sink: Callable[[object], object] | None,
    sidecar_sink: Callable[[object], object] | None,
    *,
    sidecar_errors: list[str],
) -> Callable[[object], None] | None:
    """Preserve the replay journal while isolating the learning sidecar.

    The replay journal remains part of restart/recovery authority, so its
    exception semantics are unchanged.  The transition collector is only a
    data sink: an unexpected failure is retained for audit and cannot stop or
    alter the formal exam loop.
    """

    if journal_sink is None and sidecar_sink is None:
        return None
    if not isinstance(sidecar_errors, list):
        raise TypeError("sidecar_errors must be a list")

    def fanout(record: object) -> None:
        if journal_sink is not None:
            journal_sink(record)
        if sidecar_sink is not None:
            try:
                sidecar_sink(record)
            except Exception as error:  # learning telemetry never owns input
                sidecar_errors.append(f"{type(error).__name__}:{error}")

    return fanout


def _resolve_plan2_terminal_inner_sidecar(
    records: tuple[object, ...],
    resolver: Callable[..., bool],
    sidecar_errors: list[str],
) -> None:
    """Best-effort final pending resolution; never owns formal completion."""

    if not records:
        return
    final_record = records[-1]
    for evidence in (
        getattr(final_record, "actual_next_evidence", None),
        getattr(final_record, "evidence_before", None),
    ):
        try:
            resolved = resolver(evidence, kind="terminal-last-settled-boundary")
        except Exception as error:
            sidecar_errors.append(
                f"pending-terminal-resolve:{type(error).__name__}:{error}"
            )
            return
        if resolved:
            return


def _clear_plan2_durable_terminal_stage_journal(
    result: object,
    journal_path: Path | None,
    *,
    exam_save_path: Path | None = None,
    evidence_loader: Callable[[Path], object] | None = None,
) -> bool:
    """Retire one completed stage journal before a same-stage retry starts."""

    if journal_path is None or not bool(
        getattr(result, "terminal_reached", False)
    ):
        return False
    records = tuple(getattr(result, "records", ()))
    evidence = (
        None
        if not records
        else getattr(records[-1], "actual_next_evidence", None)
    )
    from .audition_local_save_state import AuditionLocalSaveStateEvidence

    runtime = (
        None
        if not isinstance(evidence, AuditionLocalSaveStateEvidence)
        else evidence.state.root_runtime
    )
    if runtime is None or not runtime.is_exam_end_complete:
        if exam_save_path is None or evidence_loader is None:
            return False
        try:
            evidence = evidence_loader(Path(exam_save_path))
        except (OSError, TypeError, ValueError):
            return False
        if not isinstance(evidence, AuditionLocalSaveStateEvidence):
            return False
        runtime = evidence.state.root_runtime
        if runtime is None or not runtime.is_exam_end_complete:
            return False
    from .plan2_replay_journal import clear_plan2_replay_journal

    clear_plan2_replay_journal(journal_path)
    return True


def _dispatch_initial_regular_plan2_exam(
    exam_save_path: Path,
    *,
    context: InitialRegularPlan2ExamContext,
    dependencies: InitialRegularPlan2ExamDependencies,
) -> Mapping[str, Any]:
    current_evidence = dependencies.evidence_loader(Path(exam_save_path))
    deck_shadow_sync = _refresh_plan2_run_shadow_deck(context, current_evidence)
    outer_before = None
    if dependencies.outer_snapshot_reader is not None:
        try:
            outer_before = dependencies.outer_snapshot_reader(
                Path(exam_save_path)
            )
        except (OSError, TypeError, ValueError):
            outer_before = None
    from .audition_local_save_state import AuditionLocalSaveStateEvidence

    orchestrator = dependencies.decision_orchestrator_factory(context)
    inner_transition_collector = getattr(
        orchestrator, "nia_inner_transition_collector", None
    )
    legal_decision_collector = getattr(
        orchestrator, "nia_legal_decision_collector", None
    )
    inner_transition_collector_error = getattr(
        orchestrator, "nia_inner_transition_collector_error", None
    )
    inner_transition_sidecar_errors: list[str] = []
    inner_transition_start_count = len(
        getattr(inner_transition_collector, "transitions", ())
    )
    inner_transition_rejection_start_count = len(
        getattr(inner_transition_collector, "rejections", ())
    )
    legal_decision_start_count = len(
        getattr(legal_decision_collector, "rows", ())
    )
    legal_decision_rejection_start_count = len(
        getattr(legal_decision_collector, "rejections", ())
    )
    pending_resolution_proof: dict[str, object] | None = None
    pending_resolution_attempted = False
    pending_resolution_succeeded = False

    journal_sink = None
    journal_path = None
    pending_drink_receipt = None
    pending_play_receipt = None
    pending_play_receipt_on_entry = None
    pending_play_wait_required = False
    pending_play_logical_before = None
    pending_play_prior_replay = None
    initial_replay = None
    if (
        isinstance(current_evidence, AuditionLocalSaveStateEvidence)
        and dependencies.replay_journal_root is not None
    ):
        from .plan2_replay_journal import (
            Plan2ReplayJournalSink,
            clear_plan2_replay_journal,
            load_plan2_pending_drink_receipt,
            load_plan2_pending_play_receipt,
            load_plan2_replay_journal,
            pending_play_transitional_owner_matches,
            plan2_replay_journal_path,
            reconcile_settled_plan2_replay_journal,
        )

        journal_sink = Plan2ReplayJournalSink(
            current_evidence,
            root=dependencies.replay_journal_root,
        )
        journal_path = plan2_replay_journal_path(
            current_evidence,
            root=dependencies.replay_journal_root,
        )
        pending_drink_receipt = load_plan2_pending_drink_receipt(
            journal_sink.pending_drink_path
        )
        pending_play_receipt = load_plan2_pending_play_receipt(
            journal_sink.pending_play_path
        )
        pending_play_receipt_on_entry = pending_play_receipt
        if pending_play_receipt is not None:
            if _resume_plan2_pending_play_cleanup(
                journal_sink,
                pending_play_receipt,
                current_evidence,
            ):
                # The settled tombstone is written only after full nested
                # reconciliation.  Resume artifact cleanup before attempting
                # any journal/drink context reconstruction; partial cleanup may
                # intentionally have removed either predecessor already.
                pending_play_receipt = None
            else:
                (
                    pending_play_logical_before,
                    pending_play_prior_replay,
                ) = journal_sink.pending_play_context(pending_play_receipt)
        if pending_play_receipt is not None:
            if (
                current_evidence.state.is_native_actionable_settled
                or _plan2_state_is_terminal(current_evidence.state)
            ):
                _complete_plan2_pending_play(
                    journal_sink,
                    pending_play_receipt,
                    current_evidence,
                    logical_before=pending_play_logical_before,
                    prior_replay=pending_play_prior_replay,
                )
                pending_play_receipt = None
            elif pending_play_transitional_owner_matches(
                pending_play_receipt,
                current_evidence,
            ):
                pending_play_wait_required = True
            else:
                raise RuntimeError(
                    "plan2-pending-play-current-owner-mismatch"
                )
        if (
            current_evidence.state.is_native_actionable_settled
            or _plan2_state_is_terminal(current_evidence.state)
        ):
            existing_journal = (
                load_plan2_replay_journal(journal_path)
                if journal_path.exists()
                else ()
            )
            if (
                existing_journal
                and current_evidence.state.is_native_actionable_settled
            ):
                try:
                    settled_replay_proof = reconcile_settled_plan2_replay_journal(
                        current_evidence,
                        root=dependencies.replay_journal_root,
                    )
                except (TypeError, ValueError) as error:
                    raise RuntimeError(
                        "plan2-settled-replay-journal-unresolved:"
                        f"{type(error).__name__}:{error}"
                    ) from error
                pending_resolution_proof = {
                    "kind": "plan2-replay-journal-reconciled",
                    "proof_type": type(settled_replay_proof).__name__,
                    "after_digest": current_evidence.digest(),
                }
            clear_plan2_replay_journal(journal_path)
            if pending_drink_receipt is not None:
                from .plan2_native_maa_action_executor import (
                    Plan2StableSettledEvidence,
                    _validate_next_evidence,
                )

                try:
                    _validate_next_evidence(
                        pending_drink_receipt.persisted_before,
                        pending_drink_receipt.action,
                        Plan2StableSettledEvidence(
                            current_evidence,
                            current_evidence,
                        ),
                    )
                except (TypeError, ValueError) as error:
                    raise RuntimeError(
                        "plan2-pending-drink-receipt-unresolved:"
                        f"{type(error).__name__}:{error}"
                    ) from error
                pending_resolution_proof = {
                    "kind": "plan2-pending-drink-next-evidence-validated",
                    "action_id": pending_drink_receipt.action.action_id,
                    "before_digest": (
                        pending_drink_receipt.persisted_before.digest()
                    ),
                    "after_digest": current_evidence.digest(),
                }
                journal_sink.clear_pending_drink_receipt(current_evidence)
                pending_drink_receipt = None

    if (
        inner_transition_collector is not None
        and pending_resolution_proof is not None
        and getattr(inner_transition_collector, "pending", ())
    ):
        pending_resolution_attempted = True
        try:
            pending_resolution_succeeded = bool(
                inner_transition_collector.resolve_pending(
                    current_evidence,
                    pending_resolution_proof,
                )
            )
        except Exception as error:  # sidecar cannot own formal recovery
            inner_transition_sidecar_errors.append(
                f"pending-resolve:{type(error).__name__}:{error}"
            )

    if isinstance(
        current_evidence, AuditionLocalSaveStateEvidence
    ) and _plan2_state_is_terminal(current_evidence.state):
        # ExamSave is the durable acceptance authority. A phase-8 completed
        # save must not be sent back through the phase-6 card solver merely to
        # prove the same terminal fact again.
        return _plan2_terminal_exam_result(current_evidence, deck_shadow_sync)
    maa_dependencies, initial_reader = dependencies.maa_dependencies_factory(
        Path(exam_save_path),
        context,
        dependencies.evidence_loader,
        orchestrator,
    )
    if journal_sink is not None:
        from .plan2_native_maa_action_executor import (
            Plan2MaaBackgroundDependencies,
        )

        if isinstance(maa_dependencies, Plan2MaaBackgroundDependencies):
            maa_dependencies = replace(
                maa_dependencies,
                drink_input_submitted_sink=(
                    journal_sink.append_pending_drink_receipt
                ),
                settled_evidence_sink=(
                    journal_sink.clear_pending_drink_receipt
                ),
            )
            waiter_owner = getattr(
                maa_dependencies.next_settled_evidence_waiter,
                "__self__",
                None,
            )
            bind_pending_play = getattr(
                waiter_owner,
                "bind_accepted_pending_play_sink",
                None,
            )
            if callable(bind_pending_play):
                bind_pending_play(journal_sink.append_accepted_pending_play)
            bind_submitted_pending = getattr(
                waiter_owner,
                "bind_submitted_pending_play_sink",
                None,
            )
            if callable(bind_submitted_pending):
                bind_submitted_pending(
                    journal_sink.append_pending_play_receipt
                )
            bind_settled_pending = getattr(
                waiter_owner,
                "bind_settled_pending_play_sink",
                None,
            )
            if callable(bind_settled_pending):
                def complete_pending_in_process(
                    before: Any,
                    action: Any,
                    submitted: Any,
                    settled: Any,
                    logical_before: Any,
                    prior_replay: Any,
                ) -> None:
                    from .plan2_replay_journal import (
                        load_plan2_pending_play_receipt,
                    )

                    receipt = load_plan2_pending_play_receipt(
                        journal_sink.pending_play_path
                    )
                    if receipt is None or (
                        receipt.persisted_before != before
                        or receipt.action != action
                        or receipt.submitted_evidence != submitted
                    ):
                        raise RuntimeError(
                            "plan2-pending-play-completion-owner-mismatch"
                        )
                    _complete_plan2_pending_play(
                        journal_sink,
                        receipt,
                        settled,
                        logical_before=logical_before,
                        prior_replay=prior_replay,
                    )

                bind_settled_pending(complete_pending_in_process)
            if pending_play_wait_required and pending_play_receipt is not None:
                identity_binder = getattr(
                    orchestrator,
                    "bind_retained_evidence_identity",
                    None,
                )
                if callable(identity_binder) and identity_binder(
                    pending_play_receipt.persisted_before
                ) is not True:
                    raise RuntimeError(
                        "plan2-pending-play-learned-identity-mismatch"
                    )
                exact_pending_replay, recovered_evidence = (
                    _recover_plan2_pending_play_before_wait(
                        waiter_owner,
                        journal_sink,
                        pending_play_receipt,
                        current_evidence,
                        logical_before=pending_play_logical_before,
                        prior_replay=pending_play_prior_replay,
                    )
                )
                if exact_pending_replay is not None:
                    initial_replay = exact_pending_replay
                    pending_play_receipt = None
                else:
                    current_evidence = recovered_evidence
                pending_play_wait_required = False
    executor = dependencies.action_executor_factory(maa_dependencies)
    if not (
        current_evidence.state.is_native_actionable_settled
        or _plan2_state_is_terminal(current_evidence.state)
    ):
        current_runtime = current_evidence.state.root_runtime
        current_commands = (
            []
            if current_runtime is None
            else current_runtime.command_list.to_value()
        )
        retained_drink = bool(
            isinstance(current_commands, list)
            and any(
                isinstance(row, Mapping)
                and isinstance(row.get("_playingDrink"), Mapping)
                and isinstance(row["_playingDrink"].get("_id"), str)
                and bool(row["_playingDrink"].get("_id"))
                for row in current_commands
            )
        )
        if retained_drink:
            from .plan2_card_history import (
                Plan2CompletedDrinkReplay,
                recover_retained_plan2_drink_history,
            )

            startup_observation = _plan2_local_save_drink_history_observation(
                current_evidence,
                source_prefix="maa-startup-drink-local-save",
            )
            chained_receipt = bool(
                pending_drink_receipt is not None
                and pending_drink_receipt.prior is not None
            )
            materialized_chained_queue = bool(
                chained_receipt
                and current_evidence
                != pending_drink_receipt.persisted_before
            )
            if materialized_chained_queue:
                from .plan2_replay_journal import (
                    recover_materialized_plan2_pending_drink_chain,
                )

                assert pending_drink_receipt is not None
                recovered_drink = (
                    recover_materialized_plan2_pending_drink_chain(
                        current_evidence,
                        pending_drink_receipt,
                        observation=startup_observation,
                    )
                )
            else:
                receipt_for_retained_queue = (
                    pending_drink_receipt
                    if not chained_receipt
                    else pending_drink_receipt.prior
                )
                prior_drink_replay = None
                if (
                    receipt_for_retained_queue is not None
                    and journal_path is not None
                    and journal_path.exists()
                ):
                    from .plan2_replay_journal import (
                        load_plan2_replay_journal,
                        recover_plan2_replay_journal,
                    )

                    journal_entries = load_plan2_replay_journal(journal_path)
                    if (
                        journal_entries
                        and journal_entries[-1].persisted_transition
                        == receipt_for_retained_queue.persisted_before
                    ):
                        prior_drink_replay = recover_plan2_replay_journal(
                            receipt_for_retained_queue.persisted_before,
                            root=dependencies.replay_journal_root,
                        )
                        if (
                            prior_drink_replay is None
                            or prior_drink_replay.persisted_transition
                            != receipt_for_retained_queue.persisted_before
                        ):
                            raise RuntimeError(
                                "plan2-pending-drink-prior-journal-authority-"
                                "mismatch"
                            )
                recovered_drink = recover_retained_plan2_drink_history(
                    current_evidence,
                    observation=startup_observation,
                    expected_action=(
                        None
                        if receipt_for_retained_queue is None
                        else receipt_for_retained_queue.action
                    ),
                    persisted_before=(
                        None
                        if receipt_for_retained_queue is None
                        else receipt_for_retained_queue.persisted_before
                    ),
                    before_horizon=(
                        None
                        if prior_drink_replay is None
                        else prior_drink_replay.logical_after
                    ),
                )
            if not recovered_drink.supported or recovered_drink.replay is None:
                detail = ",".join(
                    value.code + (":" + value.detail if value.detail else "")
                    for value in recovered_drink.issues
                )
                raise RuntimeError(f"plan2-retained-drink-recovery-failed:{detail}")
            initial_replay = recovered_drink.replay
            if (
                chained_receipt
                and not materialized_chained_queue
            ):
                from .plan2_native_horizon import (
                    simulate_plan2_native_action_lifecycle,
                )
                from .plan2_native_program_catalog import (
                    compile_plan2_native_program_catalog,
                )
                from .plan2_replay_journal import (
                    bind_plan2_pending_drink_action_identity,
                )

                assert pending_drink_receipt is not None
                if (
                    pending_drink_receipt.persisted_before != current_evidence
                    or pending_drink_receipt.dispatch_witness is None
                ):
                    raise RuntimeError(
                        "plan2-pending-drink-barrier-authority-mismatch"
                    )
                catalog = compile_plan2_native_program_catalog().catalog
                logical_before = bind_plan2_pending_drink_action_identity(
                    initial_replay.logical_after,
                    pending_drink_receipt,
                )
                action = pending_drink_receipt.action
                transition = simulate_plan2_native_action_lifecycle(
                    logical_before,
                    action,
                    catalog,
                )
                if not transition.supported or transition.after is None:
                    detail = ",".join(
                        value.code
                        + (":" + value.detail if value.detail else "")
                        for value in transition.blockers
                    )
                    raise RuntimeError(
                        "plan2-pending-drink-barrier-transition-blocked:"
                        + detail
                    )
                instance = logical_before.drink_runtime.resolve(
                    action.slot_index,
                    instance_id=action.instance_id,
                    drink_id=action.drink_id,
                )
                initial_replay = Plan2CompletedDrinkReplay(
                    action=action,
                    before=logical_before,
                    after=transition.after,
                    transition=transition,
                    persisted_transition=current_evidence,
                    command_effect_ids=tuple(
                        value.effect_id for value in instance.effects
                    ),
                    trace=(
                        f"drink-history:action:{action.action_id}",
                        "drink-history:provisional-submitted-receipt",
                        "drink-history:next-action-barrier:play-only",
                        *transition.trace,
                    ),
                    persisted_before=current_evidence,
                    provisional_from_submitted_receipt=True,
                )
            if journal_path is not None and journal_path.exists():
                from .plan2_replay_journal import clear_plan2_replay_journal

                clear_plan2_replay_journal(journal_path)
        journal_entries = ()
        if (
            initial_replay is None
            and journal_path is not None
            and journal_path.exists()
        ):
            from .plan2_replay_journal import load_plan2_replay_journal

            journal_entries = load_plan2_replay_journal(journal_path)
        if initial_replay is not None:
            pass
        elif journal_entries:
            from .plan2_card_history import (
                Plan2CardHistoryIssue,
                Plan2CardHistoryReplayResult,
            )
            from .plan2_replay_journal import (
                PendingReplayDisposition,
                Plan2ReplayJournalError,
                classify_pending_replay_result,
                recover_plan2_replay_journal,
                reconcile_settled_plan2_replay_journal,
            )

            observation = None
            external_item_reconciled = False
            if current_evidence != journal_entries[-1].persisted_transition:
                observation = _read_plan2_card_history_observation(
                    current_evidence,
                    source_prefix="maa-journal-hud",
                )
            try:
                initial_replay = recover_plan2_replay_journal(
                    current_evidence,
                    observation=observation,
                    root=dependencies.replay_journal_root,
                )
            except Plan2ReplayJournalError as replay_error:
                external_item_replay_gap = bool(
                    len(journal_entries) == 1
                    and current_evidence
                    == journal_entries[0].persisted_transition
                    and replay_error.code == "journal-first-replay-failed"
                    and replay_error.detail
                    and classify_pending_replay_result(
                        Plan2CardHistoryReplayResult(
                            None,
                            (Plan2CardHistoryIssue(replay_error.detail),),
                        )
                    ).disposition
                    is PendingReplayDisposition.WAIT_EXTERNAL_SETTLEMENT
                )
                external_waiter = getattr(
                    getattr(initial_reader, "__self__", None),
                    "wait_external_retained_settlement",
                    None,
                )
                if external_item_replay_gap and callable(external_waiter):
                    # The submitted PLAY is already retained in an exact
                    # native queue, but one game-owned item listener is outside
                    # the local horizon.  Do not replay or resend it.  Wait for
                    # physical idle, then retire the one-row journal only via
                    # the independent settled-save/card-universe proof.
                    stable = external_waiter(current_evidence)
                    settled = stable.second
                    settled_proof = reconcile_settled_plan2_replay_journal(
                        settled,
                        root=dependencies.replay_journal_root,
                    )
                    current_evidence = settled
                    clear_plan2_replay_journal(journal_path)
                    external_item_reconciled = True
                    if (
                        inner_transition_collector is not None
                        and getattr(
                            inner_transition_collector,
                            "pending",
                            (),
                        )
                    ):
                        pending_resolution_attempted = True
                        try:
                            pending_resolution_succeeded = bool(
                                inner_transition_collector.resolve_pending(
                                    settled,
                                    {
                                        "kind": (
                                            "plan2-external-item-settled-"
                                            "journal-reconciled"
                                        ),
                                        "proof_type": type(
                                            settled_proof
                                        ).__name__,
                                        "after_digest": settled.digest(),
                                    },
                                )
                            )
                        except Exception as error:
                            inner_transition_sidecar_errors.append(
                                "pending-resolve-external-item:"
                                f"{type(error).__name__}:{error}"
                            )
                # The last retained queue can finish and publish phase 8 while
                # replay is compiling the same card chain.  Re-read once after
                # a failed replay; a terminal save is stronger authority than
                # any intermediate HUD observation stored in the journal.
                if not external_item_reconciled:
                    refreshed = dependencies.evidence_loader(
                        Path(exam_save_path)
                    )
                    if not (
                        isinstance(refreshed, AuditionLocalSaveStateEvidence)
                        and _plan2_state_is_terminal(refreshed.state)
                    ):
                        raise
                    clear_plan2_replay_journal(journal_path)
                    return _plan2_terminal_exam_result(
                        refreshed,
                        deck_shadow_sync,
                    )
            if initial_replay is None and not external_item_reconciled:
                raise RuntimeError("plan2-replay-journal-empty-after-load")
        else:
            from .plan2_transitional_startup_recovery import (
                recover_plan2_transitional_startup,
            )
            try:
                # LocalSave + Master command replay is the primary authority.  A
                # screenshot is only a fallback for a proven external item
                # listener that the native runtime cannot yet project.
                initial_replay = recover_plan2_transitional_startup(current_evidence)
            except Exception as exact_error:
                from .plan2_transitional_startup_recovery import (
                    Plan2TransitionalStartupRecoveryError,
                )

                if not (
                    isinstance(exact_error, Plan2TransitionalStartupRecoveryError)
                    and exact_error.code == "startup-recovery-card-replay-rejected"
                    and "plan2-card-history-item-runtime-unresolved"
                    in exact_error.detail
                ):
                    raise
                initial_replay = recover_plan2_transitional_startup(
                    current_evidence,
                    observation=_read_plan2_card_history_observation(
                        current_evidence,
                        source_prefix="maa-startup-hud",
                    ),
                )
    if initial_replay is not None:
        # A restarted dispatch creates a fresh learned wrapper, then resumes
        # directly from the journal's retained logical horizon.  Bind the
        # model's flow/stage from the current physical ExamSave identity before
        # that logical startup; this performs no enumeration, scoring, or
        # simulation and never substitutes context.stage_number.
        identity_binder = getattr(
            orchestrator,
            "bind_retained_evidence_identity",
            None,
        )
        if callable(identity_binder):
            if identity_binder(current_evidence) is not True:
                raise RuntimeError(
                    "plan2-retained-replay-learned-identity-mismatch"
                )
    if initial_replay is None:
        bridge = dependencies.bridge_factory(executor, initial_reader)
    else:
        try:
            bridge = dependencies.bridge_factory(
                executor,
                initial_reader,
                initial_evidence=current_evidence,
                initial_replay=initial_replay,
            )
        except TypeError as error:
            # Preserve the historical two-argument injection seam for focused
            # tests. Production uses the keyword-capable default factory.
            if "unexpected keyword" not in str(error):
                raise
            bridge = dependencies.bridge_factory(executor, initial_reader)
    def resolve_inner_pending_at_boundary(
        evidence: object,
        *,
        kind: str,
    ) -> bool:
        if not callable(inner_transition_collector) or not isinstance(
            evidence, AuditionLocalSaveStateEvidence
        ):
            return False
        runtime = evidence.state.root_runtime
        if not (
            evidence.state.is_native_actionable_settled
            or (runtime is not None and runtime.is_exam_end_complete)
        ):
            return False
        if not getattr(inner_transition_collector, "pending", ()):
            return False
        return bool(
            inner_transition_collector.resolve_pending(
                evidence,
                {
                    "kind": kind,
                    "after_digest": evidence.digest(),
                },
            )
        )

    def learning_sidecar(record: object) -> None:
        if callable(inner_transition_collector):
            # The next record's settled before-boundary is the authoritative
            # after-state of a prior submitted pending action.  Resolve it
            # before any rejection or collection path can return early.
            resolve_inner_pending_at_boundary(
                getattr(record, "evidence_before", None),
                kind="next-record-settled-boundary",
            )
            execution = getattr(record, "execution", None)
            next_evidence = getattr(record, "actual_next_evidence", None)
            next_runtime = (
                None
                if not isinstance(next_evidence, AuditionLocalSaveStateEvidence)
                else next_evidence.state.root_runtime
            )
            next_is_settled = bool(
                isinstance(next_evidence, AuditionLocalSaveStateEvidence)
                and (
                    next_evidence.state.is_native_actionable_settled
                    or (
                        next_runtime is not None
                        and next_runtime.is_exam_end_complete
                    )
                )
            )
            if (
                execution is not None
                and execution.replan
                and next_is_settled
                and initial_replay is not None
                and getattr(inner_transition_collector, "pending", ())
            ):
                inner_transition_collector.resolve_pending(
                    next_evidence,
                    {
                        "kind": "plan2-replay-recovered-replan-settled",
                        "after_digest": next_evidence.digest(),
                    },
                )
            inner_transition_collector(record)
            if (
                execution is not None
                and execution.accepted
                and not execution.replan
                and not next_is_settled
            ):
                before = getattr(record, "evidence_before", None)
                action = getattr(record, "action", None)
                inner_transition_collector.stage_pending(
                    record,
                    {
                        "kind": "accepted-exact-execution-unsettled",
                        "action_id": getattr(action, "action_id", None),
                        "before_digest": (
                            None
                            if not isinstance(
                                before, AuditionLocalSaveStateEvidence
                            )
                            else before.digest()
                        ),
                        "after_digest": (
                            None
                            if not isinstance(
                                next_evidence,
                                AuditionLocalSaveStateEvidence,
                            )
                            else next_evidence.digest()
                        ),
                    },
                )
        if callable(legal_decision_collector):
            legal_decision_collector(record)

    learning_enabled = bool(
        callable(inner_transition_collector)
        or callable(legal_decision_collector)
    )

    record_sink = _compose_plan2_record_sinks(
        journal_sink,
        learning_sidecar if learning_enabled else None,
        sidecar_errors=inner_transition_sidecar_errors,
    )
    loop_dependencies = (
        bridge.dependencies(orchestrator, record_sink=record_sink)
        if record_sink is not None
        else bridge.dependencies(orchestrator)
    )
    result = dependencies.loop_runner(
        dependencies=loop_dependencies,
        max_actions=context.max_actions,
    )
    records = tuple(result.records)
    _clear_plan2_durable_terminal_stage_journal(
        result,
        journal_path,
        exam_save_path=Path(exam_save_path),
        evidence_loader=dependencies.evidence_loader,
    )
    if inner_transition_collector is not None and records:
        # A terminal loop may not produce another record.  Give its final
        # observed settled/terminal evidence the same current-run resolver;
        # removed ExamSave handling below then remains a lifecycle authority,
        # not a substitute state_after.
        _resolve_plan2_terminal_inner_sidecar(
            records,
            resolve_inner_pending_at_boundary,
            inner_transition_sidecar_errors,
        )
    steps = [record.to_dict() for record in records]
    differences = [
        {
            "step_index": record.step_index,
            "differences": [value.to_dict() for value in record.differences],
        }
        for record in records
        if record.differences
    ]
    outer_after = None
    if (
        not Path(exam_save_path).exists()
        and outer_before is not None
        and dependencies.outer_snapshot_reader is not None
    ):
        try:
            outer_after = dependencies.outer_snapshot_reader(
                Path(exam_save_path)
            )
        except (OSError, TypeError, ValueError):
            outer_after = None
    removed_terminal_authority = _plan2_terminal_exit_removed_exam_save(
        result,
        Path(exam_save_path),
        current_evidence=current_evidence,
        outer_before=outer_before,
        outer_after=outer_after,
    )
    removed_terminal = removed_terminal_authority is not None
    pending_submitted_authority = None
    if not removed_terminal:
        pending_submitted_authority = (
            _plan2_pending_submitted_settlement_authority(
                result,
                journal_sink,
                pending_play_receipt_on_entry,
            )
        )
    orchestration = dict(result.to_dict())
    if (
        inner_transition_collector is not None
        or legal_decision_collector is not None
        or inner_transition_collector_error is not None
    ):
        transitions = tuple(
            getattr(inner_transition_collector, "transitions", ())
        )
        rejections = tuple(
            getattr(inner_transition_collector, "rejections", ())
        )
        new_transitions = transitions[inner_transition_start_count:]
        new_rejections = rejections[inner_transition_rejection_start_count:]
        legal_decisions = tuple(
            getattr(legal_decision_collector, "rows", ())
        )
        legal_decision_rejections = tuple(
            getattr(legal_decision_collector, "rejections", ())
        )
        new_legal_decisions = legal_decisions[legal_decision_start_count:]
        new_legal_decision_rejections = legal_decision_rejections[
            legal_decision_rejection_start_count:
        ]
        rejection_reason_counts: dict[str, int] = {}
        missing_field_counts: dict[str, int] = {}
        for rejection in new_rejections:
            for reason in getattr(rejection, "reasons", ()):
                rejection_reason_counts[reason] = (
                    rejection_reason_counts.get(reason, 0) + 1
                )
            for field_name in getattr(rejection, "missing_fields", ()):
                missing_field_counts[field_name] = (
                    missing_field_counts.get(field_name, 0) + 1
                )
        output_path = getattr(inner_transition_collector, "output", None)
        legal_decision_output_path = getattr(
            legal_decision_collector, "output", None
        )
        orchestration["inner_transition_audit"] = {
            "schema": "gkms.nia-inner-transition-collector-audit.v1",
            "enabled": inner_transition_collector is not None,
            "collector_schema": "gkms.nia-inner-transition-collector.v2",
            "provider": getattr(
                orchestrator,
                "nia_inner_transition_candidate_provider",
                None,
            ),
            "run_binding_id": getattr(
                orchestrator,
                "nia_training_run_binding_id",
                None,
            ),
            "record_count": len(records),
            "candidate_attempt_count": sum(
                bool(
                    record.action is not None
                    and record.execution is not None
                    and record.execution.accepted
                    and not record.execution.replan
                )
                for record in records
            ),
            "accepted_transition_count": len(new_transitions),
            "rejected_transition_count": len(new_rejections),
            "accepted_steps": [value.step for value in new_transitions],
            "pending_count": len(
                getattr(inner_transition_collector, "pending", ())
            ),
            "pending_keys": list(
                getattr(inner_transition_collector, "pending_keys", ())
            ),
            "pending_resolution_attempted": pending_resolution_attempted,
            "pending_resolution_succeeded": pending_resolution_succeeded,
            "pending_resolution_proof_kind": (
                None
                if pending_resolution_proof is None
                else pending_resolution_proof.get("kind")
            ),
            "legal_decision_count": len(new_legal_decisions),
            "legal_decision_boundary_digests": [
                value.boundary_digest for value in new_legal_decisions
            ],
            "legal_decision_rejected_count": len(
                new_legal_decision_rejections
            ),
            "legal_decision_rejections": list(
                new_legal_decision_rejections
            ),
            "legal_decision_output_path": (
                None
                if legal_decision_output_path is None
                else str(legal_decision_output_path)
            ),
            "rejection_reason_counts": dict(sorted(rejection_reason_counts.items())),
            "missing_field_counts": dict(sorted(missing_field_counts.items())),
            "output_path": None if output_path is None else str(output_path),
            "journal_sink_preserved": journal_sink is not None,
            "formal_control_unchanged": True,
            "sidecar_errors": [
                *(
                    []
                    if inner_transition_collector_error is None
                    else [str(inner_transition_collector_error)]
                ),
                *inner_transition_sidecar_errors,
            ],
        }
    if deck_shadow_sync is not None:
        orchestration["deck_shadow_sync"] = dict(deck_shadow_sync)
    if removed_terminal:
        orchestration["terminal_exit_authority"] = {
            "kind": removed_terminal_authority,
            "exam_save_path": str(Path(exam_save_path)),
            "input_submitted": True,
        }
    if pending_submitted_authority is not None:
        orchestration["pending_submitted_settlement_authority"] = dict(
            pending_submitted_authority
        )
    retry_evidence = None
    if (
        not removed_terminal
        and pending_submitted_authority is None
    ):
        try:
            retry_evidence = _plan2_retryable_advanced_settled_evidence(
                result,
                Path(exam_save_path),
                dependencies.evidence_loader,
            )
        except (OSError, TypeError, ValueError):
            retry_evidence = None
    if retry_evidence is not None:
        orchestration["decision_retry_authority"] = {
            "kind": "newer-actionable-settled-exam-save",
            "source_sha256": retry_evidence.source_sha256,
            "input_submitted": False,
            "next": "re-enter-exam-dispatch",
        }
    return {
        "accepted": bool(
            result.terminal_reached
            or removed_terminal
            or pending_submitted_authority is not None
            or retry_evidence is not None
        ),
        "terminal": bool(result.terminal_reached or removed_terminal),
        "reason": (
            "terminal"
            if removed_terminal
            else "pending-submitted-settlement"
            if pending_submitted_authority is not None
            else "decision-retryable-newer-settled-save"
            if retry_evidence is not None
            else str(result.stop_reason)
        ),
        "actions_executed": (
            int(result.actions_executed)
            + int(removed_terminal)
            + int(pending_submitted_authority is not None)
        ),
        "steps": steps,
        "differences": differences,
        "orchestration": orchestration,
    }


def dispatch_initial_regular_exam(
    plan_type: str,
    exam_save_path: Path,
    *,
    exam_policy: ExamExecutionPolicy = EXACT_EXAM_POLICY,
    expected_run_id: str | None = None,
    plan2_context: InitialRegularPlan2ExamContext | None = None,
    plan2_dependencies: InitialRegularPlan2ExamDependencies | None = None,
    produce_id: str = "produce-001",
    idol_card_id: str | None = None,
    game_root: str | Path = DEFAULT_PC_GAME_ROOT,
    inner_imitation_enabled: bool = INNER_IMITATION_RUNTIME_ENABLED_DEFAULT,
    inner_imitation_runner: InnerImitationExamRunner | None = None,
    exact_exam_bc_submitter: Callable[..., object] | None = None,
    stop_requested: Callable[[], bool] = lambda: False,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
) -> Mapping[str, Any]:
    """Route an exam to the matching unattended production backend."""

    if not isinstance(exam_policy, ExamExecutionPolicy):
        raise TypeError("exam_policy must be ExamExecutionPolicy")
    if type(inner_imitation_enabled) is not bool:
        raise TypeError("inner_imitation_enabled must be bool")
    if inner_imitation_runner is not None and not callable(inner_imitation_runner):
        raise TypeError("inner_imitation_runner must be callable or None")
    if exact_exam_bc_submitter is not None and not callable(
        exact_exam_bc_submitter
    ):
        raise TypeError("exact_exam_bc_submitter must be callable or None")

    if exam_policy.mode is ExamExecutionMode.MAA_COMPLETION_BASELINE:
        if not isinstance(expected_run_id, str) or not expected_run_id.strip():
            return {
                "accepted": False,
                "terminal": False,
                "reason": "maa-completion-run-binding-required",
                "execution_policy": exam_policy.to_dict(),
            }
        expected_native_plan_type = {
            PLAN1: 2,
            PLAN2: 3,
            PLAN3: 4,
        }.get(plan_type)
        if expected_native_plan_type is None:
            return {
                "accepted": False,
                "terminal": False,
                "reason": f"unsupported-exam-plan:{plan_type}",
                "execution_policy": exam_policy.to_dict(),
            }
        evidence = load_initial_regular_plan2_exam_evidence(exam_save_path)
        state = evidence.state
        runtime = state.root_runtime
        raw = None if runtime is None else runtime.opaque_fields.to_value()
        if (
            not state.is_native_actionable_settled
            or state.exam_type not in {0, 1}
            or not isinstance(raw, Mapping)
        ):
            return {
                "accepted": False,
                "terminal": False,
                "reason": "maa-completion-exam-save-not-settled",
                "execution_policy": exam_policy.to_dict(),
            }
        if raw.get("planType") != expected_native_plan_type:
            return {
                "accepted": False,
                "terminal": False,
                "reason": "maa-completion-exam-plan-mismatch",
                "execution_policy": exam_policy.to_dict(),
            }
        exam_save_identity = {
            "exam_source_run_id": getattr(evidence, "run_id", None),
            "run_binding_id": expected_run_id,
            "step_context_id": getattr(evidence, "step_context_id", None),
            "session_transition_id": getattr(
                evidence, "session_transition_id", None
            ),
            "source_sha256": getattr(evidence, "source_sha256", None),
        }
        if any(
            not isinstance(value, str) or not value.strip()
            for value in exam_save_identity.values()
        ):
            return {
                "accepted": False,
                "terminal": False,
                "reason": "maa-completion-exam-identity-unavailable",
                "execution_policy": exam_policy.to_dict(),
            }

        # The Maa completion backend deliberately bypasses the exact native
        # Plan2 dispatcher below. Seed the run shadow here, while this
        # settled ExamSave still exists, or a successful baseline exam would
        # finish without ever publishing its authoritative deck. The helper
        # is advisory and fail-closed: an unavailable/ambiguous snapshot is
        # reported in the result but never blocks Maa completion.
        deck_shadow_sync: Mapping[str, Any] | None = None
        if plan_type in {PLAN1, PLAN2, PLAN3} and isinstance(idol_card_id, str) and idol_card_id:
            context = plan2_context or InitialRegularPlan2ExamContext(
                idol_card_id=idol_card_id,
                produce_id=produce_id,
            )
            deck_shadow_sync = _refresh_plan2_run_shadow_deck(context, evidence)

        from .controller_client import send_command

        if inner_imitation_enabled and inner_imitation_runner is None:
            try:
                from .generic_inner_policy_runtime_v2 import (
                    build_production_inner_imitation_exam_runner,
                    nia_exam_effect_type_from_value,
                )

                # Plan and exam archetype are independent native fields.  The
                # old bridge always admitted only ExamReview, which caused the
                # imitation path to abstain for Concentration, ParameterBuff,
                # LessonBuff, and CardPlayAggressive runs even though the
                # runner already normalised all five Master-backed effects.
                # Scope this run to the archetype actually present in its
                # settled ExamSave; an unknown effect keeps the baseline as
                # the sole owner.  The review fallback is retained only for
                # legacy injected saves that omit mainEffectType entirely.
                raw_effect = raw.get("mainEffectType")
                effect_type = nia_exam_effect_type_from_value(raw_effect)
                if raw_effect is None:
                    effect_type = "ProduceExamEffectType_ExamReview"
                allowed_effect_flows = (
                    ()
                    if effect_type is None
                    else ((produce_id, plan_type, effect_type),)
                )

                inner_imitation_runner = build_production_inner_imitation_exam_runner(
                    evidence_loader=load_initial_regular_plan2_exam_evidence,
                    command_sender=send_command,
                    recognition_command=send_command,
                    produce_id=produce_id,
                    allowed_flows=allowed_effect_flows,
                )
            except (ImportError, OSError, TypeError, ValueError):
                # The opt-in bridge is advisory.  If its dependencies cannot
                # be constructed, the existing baseline below remains the
                # sole owner of the exam.
                inner_imitation_runner = None

        def baseline_runner() -> Mapping[str, Any]:
            return dict(
                send_command(
                    "run_maa_baseline_exam",
                    timeout=630.0,
                    timeout_seconds=600.0,
                    monitor_source_run_id=expected_run_id,
                )
            )

        result = _dispatch_optional_inner_imitation(
            plan_type,
            Path(exam_save_path),
            baseline_runner=baseline_runner,
            enabled=inner_imitation_enabled,
            runner=inner_imitation_runner,
        )
        inner_status = (
            result.get("inner_imitation", {}).get("status")
            if isinstance(result.get("inner_imitation"), Mapping)
            else None
        )
        if result.get("simulator_required") is not False:
            if inner_status == "imitation":
                # The generic inner contract is explicitly simulator-free;
                # keep the existing audit field without making an injected
                # runner repeat this invariant in its result payload.
                result["simulator_required"] = False
            else:
                raise RuntimeError(
                    "Maa completion backend did not prove simulator independence"
                )
        result["execution_policy"] = exam_policy.to_dict()
        result["exam_save_authority"] = "settled-native-exam-save"
        result["exam_save_identity"] = exam_save_identity
        # A production inner runner can legitimately observe a terminal save
        # before dispatching a card (for example, a bounded resume).  Preserve
        # that receipt instead of reporting a Maa input that never happened;
        # the established baseline path still owns the unconditional true
        # receipt below.
        if inner_status == "imitation":
            result["input_submitted"] = bool(
                result.get("input_submitted", result.get("actions_executed", 0) > 0)
            )
        else:
            result["input_submitted"] = True
        if deck_shadow_sync is not None:
            result["deck_shadow_sync"] = dict(deck_shadow_sync)
        # The controller has already returned the formal Maa completion
        # receipt at this point. Hand its immutable transition rows to the
        # single diagnostic daemon; bundle load, score, and ledger I/O must
        # never run synchronously on the cultivation path.
        exact_rows = result.get("exact_transitions")
        native_capture = result.get("native_runtime_stage_capture")
        sidecar_submitted = False
        sidecar_source: str | None = None
        if isinstance(native_capture, Mapping):
            try:
                from .exact_exam_bc_sidecar import (
                    submit_native_runtime_exact_exam_bc_diagnostics,
                )

                sidecar_submitted = bool(
                    submit_native_runtime_exact_exam_bc_diagnostics(
                        native_capture,
                        source_run_id=expected_run_id,
                    )
                )
                if sidecar_submitted:
                    sidecar_source = "native-runtime-stage"
            except Exception:
                sidecar_submitted = False
        if (
            not isinstance(native_capture, Mapping)
            and not sidecar_submitted
            and isinstance(exact_rows, (list, tuple))
            and exact_rows
        ):
            try:
                if exact_exam_bc_submitter is None:
                    from .exact_exam_bc_sidecar import (
                        submit_exact_exam_bc_diagnostics,
                    )

                    exact_exam_bc_submitter = submit_exact_exam_bc_diagnostics
                sidecar_submitted = bool(
                    exact_exam_bc_submitter(
                        tuple(exact_rows),
                        source_run_id=expected_run_id,
                    )
                )
                if sidecar_submitted:
                    sidecar_source = "maa-exact-transition-fallback"
            except Exception:
                sidecar_submitted = False
        result["exact_exam_bc_sidecar_submitted"] = sidecar_submitted
        result["exact_exam_bc_sidecar_source"] = sidecar_source
        return result

    if plan_type == PLAN1:
        if produce_id in {"produce-004", "produce-005"}:
            from .plan1_exact_learned_exam import run_plan1_exact_learned_exam

            return run_plan1_exact_learned_exam(
                exam_save_path,
                expected_produce_id=produce_id,
                expected_idol_card_id=idol_card_id,
                evidence_loader=load_initial_regular_plan2_exam_evidence,
                stop_requested=stop_requested,
                progress_callback=progress_callback,
                run_id=expected_run_id,
            )
        if produce_id not in {"produce-001", "produce-002", "produce-003"}:
            return {
                "accepted": False,
                "terminal": False,
                "reason": f"initial-plan1-produce-unsupported:{produce_id}",
            }
        from .controller_client import send_command

        evidence = load_initial_regular_plan2_exam_evidence(exam_save_path)
        state = evidence.state
        runtime = state.root_runtime
        raw = None if runtime is None else runtime.opaque_fields.to_value()
        if (
            not state.is_native_actionable_settled
            or state.exam_type not in {0, 1}
            or not isinstance(raw, Mapping)
            or raw.get("planType") != 2
        ):
            return {
                "accepted": False,
                "terminal": False,
                "reason": "initial-plan1-exam-save-not-actionable",
            }
        result = dict(send_command("run_initial_plan1_exam", timeout=270.0))
        return result

    if plan_type == PLAN2:
        if plan2_context is None:
            return {
                "accepted": False,
                "terminal": False,
                "reason": "plan2-runtime-context-required",
            }
        from .runtime_command_client import input_backend

        if plan2_dependencies is None and input_backend() == "dll":
            from .runtime_plan2_executor import run_runtime_plan2_exam

            return run_runtime_plan2_exam(exam_save_path, context=plan2_context,
                                         stop_requested=stop_requested, progress_callback=progress_callback,
                                         run_id=expected_run_id)
        if plan2_dependencies is None:
            dependencies = initial_regular_plan2_exam_dependencies()
            if (
                isinstance(expected_run_id, str)
                and expected_run_id.strip()
                and dependencies.replay_journal_root is not None
            ):
                dependencies = replace(
                    dependencies,
                    replay_journal_root=(
                        Path(dependencies.replay_journal_root)
                        / expected_run_id.strip()
                    ),
                )
        else:
            dependencies = plan2_dependencies
        if not isinstance(dependencies, InitialRegularPlan2ExamDependencies):
            raise TypeError(
                "plan2_dependencies must be InitialRegularPlan2ExamDependencies"
            )
        return _dispatch_initial_regular_plan2_exam(
            Path(exam_save_path),
            context=plan2_context,
            dependencies=dependencies,
        )

    if plan_type != PLAN3:
        return {
            "accepted": False,
            "terminal": False,
            "reason": f"unsupported-exam-plan:{plan_type}",
        }
    if produce_id in {"produce-004", "produce-005"}:
        from .nia_live_exam import run_live_nia_plan3_exam

        result = run_live_nia_plan3_exam(
            exam_save_path,
            game_root=game_root,
            max_actions=100,
            expected_produce_id=produce_id,
            expected_idol_card_id=idol_card_id,
            expected_run_id=expected_run_id,
            stop_requested=stop_requested,
            progress_callback=progress_callback,
        )
    else:
        from .plan3_exam_orchestrator import run_plan3_exam_orchestrator

        result = run_plan3_exam_orchestrator(
            exam_save_path,
            expected_run_id=expected_run_id,
            stop_requested=stop_requested,
            progress_callback=progress_callback,
            dry_run=False,
            max_actions=100,
        )
    return {
        "accepted": bool(result.terminal_reached),
        "terminal": bool(result.terminal_reached),
        "reason": result.stop_reason,
        "orchestration": result.to_dict(),
    }


class InitialRegularLiveSurfaceReader:
    """Production page router over existing strict readers and LocalSave."""

    def __init__(
        self,
        *,
        idol_card_id: str,
        produce_id: str = "produce-001",
        plan_type: str,
        game_root: str | Path = DEFAULT_PC_GAME_ROOT,
        expected_run_id: str | None = None,
        exam_save_selector: Callable[[Path], Path | None] | None = None,
        plan2_evidence_loader: Plan2EvidenceLoader = (
            load_initial_regular_plan2_exam_evidence
        ),
    ) -> None:
        self.idol_card_id = idol_card_id
        self.produce_id = produce_id
        self.plan_type = plan_type
        self.game_root = Path(game_root)
        self.expected_run_id = expected_run_id
        self.exam_save_selector = exam_save_selector
        if not callable(plan2_evidence_loader):
            raise TypeError("plan2_evidence_loader must be callable")
        self.plan2_evidence_loader = plan2_evidence_loader
        # N.I.A. reward identity is established from reversible full-card
        # previews, not from thumbnail order or a fixed number of clicks.
        # This state lives only for the current visible reward page.
        self._nia_reward_preview_session: Any | None = None
        self._nia_reward_pending_state: Mapping[str, Any] | None = None
        self._nia_reward_ocr_session: _NiaRewardOcrSession | None = None
        # The ranking evidence is emitted with the typed reward surface.  It
        # is deliberately a read-only snapshot of the exact ranking pass that
        # already selected the card; no later telemetry consumer reranks it.
        self._nia_reward_ranking_evidence: Mapping[str, Any] | None = None
        self._nia_reward_reroll_pending: _NiaRewardRerollPending | None = None
        self._nia_reward_ocr_authority_key: tuple[object, ...] | None = None
        self._nia_item_reward_session: _NiaItemRewardSession | None = None
        self._nia_item_reward_authority_key: tuple[object, ...] | None = None
        self._initial_choice_transaction: _InitialChoiceTransaction | None = None
        self._nia_card_operation_session: _NiaCardOperationSession | None = None
        self._nia_drink_keep_session: _NiaDrinkKeepSession | None = None
        # A static ADV/outing row becomes a tooltip-covered selected preview
        # after its first click.  Keep the original typed Master resolution
        # until the next frame confirms that exact selected slot; the
        # stateless reader must never fall through to Click_1 in between.
        self._nia_event_choice_pending: Any | None = None
        # Windows accepting a background click does not prove Unity consumed
        # it.  Generic event SELECT confirmations retain one bounded retry of
        # the exact same target/authority; a changed page or owner resets it.
        self._nia_event_confirm_signature: tuple[object, ...] | None = None
        self._nia_event_confirm_attempts = 0
        self._nia_event_confirm_wait_reads = 0
        # A clipped owned-drink tail gets one explicit recovery swipe per
        # capacity transaction.  The reader remains pure; the executor owns
        # the eventual Maa input and the next cycle captures the fresh page.
        self._nia_drink_keep_scroll_attempted = False
        self._nia_drink_reject_selected = False
        self._nia_drink_reject_select_attempts = 0
        self._nia_drink_reject_select_wait_reads = 0
        self._nia_drink_reject_submitted = False
        self._nia_drink_reject_confirm_attempts = 0
        self._nia_drink_reject_wait_reads = 0
        # N.I.A. has exactly one outer transaction owner.  Page-local reward,
        # drink, and card-operation parsers are children of this cursor; they
        # are reset only when its identity, ordinal, or phase changes.
        self._active_outer_transaction: ProduceOuterTransaction | None = None
        self._active_outer_cursor_key: tuple[object, ...] | None = None
        self._active_outer_begin_authority: Mapping[str, Any] | None = None

    @staticmethod
    def _choice_authority_key(snapshot: Any) -> tuple[object, ...]:
        return (
            getattr(snapshot, "log_count", None),
            getattr(snapshot, "latest_week_marker", None),
            getattr(snapshot, "last_completed_week", None),
            getattr(snapshot, "stamina", None),
            getattr(snapshot, "max_stamina", None),
            getattr(snapshot, "produce_points", None),
            getattr(snapshot, "vocal", None),
            getattr(snapshot, "dance", None),
            getattr(snapshot, "visual", None),
            getattr(snapshot, "vote_count", None),
        )

    def set_active_outer_transaction(
        self,
        transaction: ProduceOuterTransaction | None,
        authority: Mapping[str, Any] | None = None,
    ) -> None:
        """Bind the sole N.I.A. weekly owner supplied by the run loop."""

        if transaction is not None and not isinstance(
            transaction, ProduceOuterTransaction
        ):
            raise TypeError("active outer transaction must be a transaction or None")
        if authority is not None and not isinstance(authority, Mapping):
            raise TypeError("active outer transaction authority must be a mapping or None")
        key = (
            None
            if transaction is None
            else (
                transaction.transaction_id,
                transaction.current_ordinal,
            )
        )
        if key != self._active_outer_cursor_key:
            self._clear_all_nia_sessions()
        self._active_outer_transaction = transaction
        self._active_outer_cursor_key = key
        self._active_outer_begin_authority = (
            None
            if transaction is None or authority is None
            else dict(authority)
        )

    def _nia_reward_policy_context(self) -> Mapping[str, Any]:
        """Read the one deck/route snapshot shared by rank and reroll policy."""

        from .master_db import get_idol_profile
        from .leaderboard_card_prior import (
            try_load_default_hierarchical_leaderboard_card_prior,
        )

        profile = get_idol_profile(self.idol_card_id)
        if profile is None:
            raise ValueError(f"Master has no idol profile: {self.idol_card_id}")
        deck_counts = None
        weeks_remaining = None
        route_week = None
        stamina = None
        max_stamina = None
        try:
            from .run_shadow import load_matching_active_run_shadow

            shadow = load_matching_active_run_shadow(
                self.produce_id,
                self.idol_card_id,
                expected_run_id=self.expected_run_id,
            )
            if shadow is not None:
                deck_counts = shadow.deck
                weeks_remaining = shadow.weeks_remaining
                route_week = shadow.route_week
                stamina = shadow.stamina
                max_stamina = shadow.max_stamina
        except (FileNotFoundError, KeyError, TypeError, ValueError):
            # Strategy context is advisory.  The OCR-resolved offers and the
            # established archetype ranking remain sufficient to continue.
            pass
        leaderboard_prior = try_load_default_hierarchical_leaderboard_card_prior(
            produce_id=self.produce_id,
            plan_type=profile.plan_type,
            idol_card_id=self.idol_card_id,
            exam_effect_type=profile.exam_effect_type,
        )
        if leaderboard_prior is not None and not leaderboard_prior.applies_to(
            produce_id=self.produce_id,
            idol_card_id=self.idol_card_id,
            plan_type=profile.plan_type,
            exam_effect_type=profile.exam_effect_type,
        ):
            leaderboard_prior = None
        return {
            "exam_effect_type": profile.exam_effect_type,
            "character_id": profile.character_id,
            "plan_type": profile.plan_type,
            "deck_counts": deck_counts,
            "weeks_remaining": weeks_remaining,
            "route_week": route_week,
            "stamina": stamina,
            "max_stamina": max_stamina,
            "leaderboard_prior": leaderboard_prior,
        }

    def _nia_reward_evidence_fields(self) -> dict[str, Any]:
        """Return ranking fields without overwriting the surface policy name."""

        if not isinstance(self._nia_reward_ranking_evidence, Mapping):
            return {}
        fields = dict(self._nia_reward_ranking_evidence)
        ranking_policy = fields.pop("policy", None)
        if ranking_policy is not None:
            fields["ranking_policy"] = ranking_policy
        return fields

    def _rank_nia_reward_offers(self, offers: Sequence[Any]) -> tuple[Any, ...]:
        """Use current deck/route context when available, without blocking UI."""

        context = self._nia_reward_policy_context()
        self._nia_reward_ranking_evidence = None
        plan_type = str(context["plan_type"])
        exam_effect_type = str(context["exam_effect_type"])
        if plan_type == "ProducePlanType_Plan2" and exam_effect_type in {
            "ProduceExamEffectType_ExamReview",
            "ProduceExamEffectType_ExamCardPlayAggressive",
        }:
            from .plan2_reward_rollout import rank_plan2_reward_offers_strong

            strong = rank_plan2_reward_offers_strong(
                offers,
                exam_effect_type,
                idol_card_id=self.idol_card_id,
                produce_id=self.produce_id,
                deck_counts=context["deck_counts"],
                weeks_remaining=context["weeks_remaining"],
                stamina=context["stamina"],
                max_stamina=context["max_stamina"],
                leaderboard_prior=context["leaderboard_prior"],
            )
        else:
            # Plan1/Plan3 and any newly introduced effect type do not enter
            # the Plan2 native simulator.  Master-resolved legality/order is
            # still authoritative, with only a fully matching final-deck
            # prior allowed as an additive tie-break signal.
            from .reward_state import rank_generic_nia_reward_offers

            strong = rank_generic_nia_reward_offers(
                offers,
                plan_type=plan_type,
                produce_id=self.produce_id,
                idol_card_id=self.idol_card_id,
                exam_effect_type=exam_effect_type,
                deck_counts=context["deck_counts"],
                leaderboard_prior=context["leaderboard_prior"],
            )
        ranked = strong.ranked_offers

        def save_ranking_evidence(
            final_ranked: Sequence[Any],
            *,
            exact_prior_available: bool | None = None,
            exact_prior_rank: Sequence[str] = (),
        ) -> None:
            evaluations = getattr(strong, "heuristic_evaluations", ())
            heuristic_rows: list[dict[str, Any]] = []
            if isinstance(evaluations, (list, tuple)):
                for evaluation in evaluations:
                    offer = getattr(evaluation, "offer", None)
                    card_id = getattr(offer, "card_id", None)
                    upgrade = getattr(offer, "upgrade", None)
                    total = getattr(evaluation, "strategic_score", None)
                    bonus = getattr(evaluation, "learned_prior_bonus", 0)
                    reasons = getattr(evaluation, "reasons", ())
                    if not isinstance(card_id, str) or not isinstance(upgrade, int):
                        continue
                    if not isinstance(total, int) or not isinstance(bonus, int):
                        continue
                    row = {
                        "card_id": card_id,
                        "upgrade": upgrade,
                        "static_score": total - bonus,
                        "leaderboard_card_prior_bonus": bonus,
                        "score": total,
                        "reason": "+".join(
                            value for value in reasons if isinstance(value, str)
                        ),
                    }
                    composition_fields = {
                        "leaderboard_card_composition_current": getattr(
                            evaluation, "composition_current_count", None
                        ),
                        "leaderboard_card_composition_future": getattr(
                            evaluation,
                            "composition_future_guaranteed_count",
                            None,
                        ),
                        "leaderboard_card_composition_target": getattr(
                            evaluation, "composition_target_count", None
                        ),
                        "leaderboard_card_composition_gap": getattr(
                            evaluation, "composition_gap", None
                        ),
                        "leaderboard_card_composition_bonus": getattr(
                            evaluation, "composition_bonus", 0
                        ),
                    }
                    if composition_fields["leaderboard_card_composition_current"] is not None:
                        row.update(composition_fields)
                    heuristic_rows.append(row)
            prior = context.get("leaderboard_prior")
            self._nia_reward_ranking_evidence = {
                "policy": str(getattr(strong, "policy", "unknown")),
                "ordered_card_ids": [
                    str(getattr(value, "card_id"))
                    for value in final_ranked
                    if isinstance(getattr(value, "card_id", None), str)
                ],
                "heuristic_offers": heuristic_rows,
                "leaderboard_card_prior_available": prior is not None,
                "leaderboard_card_prior_abstained": prior is None,
                "exact_candidate_behavior_prior_available": exact_prior_available,
                "exact_candidate_behavior_prior_abstained": (
                    None
                    if exact_prior_available is None
                    else not bool(exact_prior_rank)
                ),
                "exact_candidate_behavior_prior_rank": list(exact_prior_rank),
            }

        if strong.policy != "deck-aware-heuristic-fallback":
            save_ranking_evidence(ranked)
            return ranked
        try:
            from .nia_route_profile import nia_phase_for_week
            from .nia_training_dataset import (
                try_load_default_nia_exact_candidate_behavior_prior,
            )
            from .nia_bounded_outer_advisor import rank_bounded_candidates

            route_week = context["route_week"]
            prior = try_load_default_nia_exact_candidate_behavior_prior()
            card_ids = tuple(offer.card_id for offer in offers)
            learned = ()
            if (
                prior is not None
                and not isinstance(route_week, bool)
                and isinstance(route_week, int)
            ):
                learned_result = rank_bounded_candidates(
                    prior,
                    {
                        "produce_id": self.produce_id,
                        "idol_card_id": self.idol_card_id,
                        "character_id": context["character_id"],
                        "plan_type": context["plan_type"],
                        "exam_effect_type": context["exam_effect_type"],
                        "week": route_week,
                        "phase": nia_phase_for_week(self.produce_id, route_week),
                    },
                    card_ids,
                    candidate_set_complete=(
                        bool(card_ids)
                        and len(card_ids) == len(set(card_ids))
                    ),
                    decision_kind="reward_card",
                )
                learned = learned_result.ranked if learned_result.used else ()
            if learned:
                by_id = {offer.card_id: offer for offer in ranked}
                if set(by_id) == set(learned):
                    final_ranked = tuple(by_id[card_id] for card_id in learned)
                    save_ranking_evidence(
                        final_ranked,
                        exact_prior_available=prior is not None,
                        exact_prior_rank=learned,
                    )
                    return final_ranked
            save_ranking_evidence(
                ranked,
                exact_prior_available=prior is not None,
                exact_prior_rank=learned,
            )
        except (KeyError, TypeError, ValueError):
            save_ranking_evidence(ranked)
        return ranked

    @staticmethod
    def _nia_reward_reroll_authority(snapshot: Any) -> Any | None:
        """Project exact UserProduceProgress fields when a reader exposes them."""

        from .reward_state import RewardCardRerollAuthority

        if snapshot is None:
            return None
        remaining = getattr(
            snapshot,
            "produce_card_remain_select_reroll_count",
            None,
        )
        hidden = getattr(snapshot, "hidden_produce_card_reroll", None)
        if (
            isinstance(remaining, bool)
            or not isinstance(remaining, int)
            or remaining < 0
            or not isinstance(hidden, bool)
        ):
            return None
        return RewardCardRerollAuthority(
            remaining,
            hidden,
            "UserProduceProgress LocalSave/response callback",
        )

    @staticmethod
    def _nia_reward_reroll_action(
        payload: Mapping[str, Any] | None,
    ) -> SuggestedClick | None:
        """Accept only an explicitly named Maa reward-reroll action."""

        if not isinstance(payload, Mapping):
            return None
        raw = payload.get("reward_reroll_action")
        if not isinstance(raw, Mapping):
            return None
        action = SuggestedClick.from_dict(raw)
        if "reward-reroll" not in action.label.casefold():
            return None
        return action

    @staticmethod
    def _nia_reward_action_matches_capture(
        action: SuggestedClick | None,
        capture: Mapping[str, Any],
    ) -> bool:
        """Require the Maa button box to come from the current reward frame."""

        return bool(
            action is not None
            and action.source_png_path == capture.get("png_path")
            and action.source_timestamp == capture.get("timestamp")
            and action.source_hwnd == capture.get("hwnd")
            and action.source_pid == capture.get("pid")
        )

    @staticmethod
    def _nia_reward_reroll_blocked_surface(
        capture: Mapping[str, Any],
        decision: Any,
    ) -> Mapping[str, Any]:
        blocker = decision.blocker
        if blocker is None:
            raise ValueError("blocked reward reroll decision has no typed blocker")
        return {
            "capture": dict(capture),
            "kind": "reward-reroll-blocked",
            "target": "reward-reroll",
            "blocker": blocker.to_dict(),
            "evidence": {
                "policy": "complete-three-offer-conservative-reroll",
                "decision": decision.to_dict(),
            },
        }

    def _rank_nia_strengthen_offers(self, offers: Sequence[Any]) -> tuple[Any, ...]:
        """Rank one N.I.A. strengthen page without crossing archetypes.

        The native counterfactual is intentionally limited to Plan2's
        Review/Aggressive kernel.  Plan1 (ParameterBuff/LessonBuff) and Plan3
        (Concentration) still use the Master-scoped generic ranking plus the
        matching final-deck prior; sending either archetype into the Plan2
        simulator raises an unsupported-effect error and aborts the live run.
        """

        context = self._nia_reward_policy_context()
        plan_type = str(context["plan_type"])
        exam_effect_type = str(context["exam_effect_type"])
        if plan_type == PLAN2 and exam_effect_type in {
            "ProduceExamEffectType_ExamReview",
            "ProduceExamEffectType_ExamCardPlayAggressive",
        }:
            from .plan2_reward_rollout import rank_plan2_upgrade_offers_strong

            return rank_plan2_upgrade_offers_strong(
                offers,
                exam_effect_type,
                idol_card_id=self.idol_card_id,
                produce_id=self.produce_id,
                deck_counts=context["deck_counts"],
                weeks_remaining=context["weeks_remaining"],
                stamina=context["stamina"],
                max_stamina=context["max_stamina"],
                leaderboard_prior=context["leaderboard_prior"],
            ).ranked_offers

        from .reward_state import rank_generic_nia_reward_offers

        return rank_generic_nia_reward_offers(
            offers,
            plan_type=plan_type,
            produce_id=self.produce_id,
            idol_card_id=self.idol_card_id,
            exam_effect_type=exam_effect_type,
            deck_counts=context["deck_counts"],
            leaderboard_prior=context["leaderboard_prior"],
        ).ranked_offers

    def _nia_session_ranked_offers(
        self,
        session: _NiaRewardOcrSession | _NiaCardOperationSession,
    ) -> tuple[Any, ...]:
        """Rank one immutable page offer set once and retain its evidence."""

        offers = tuple(session.offers)
        if session.ranking_source_offers != offers:
            self._nia_reward_ranking_evidence = None
            if (
                isinstance(session, _NiaCardOperationSession)
                and session.target in {"strengthen", "customize"}
            ):
                ranked = self._rank_nia_strengthen_offers(offers)
            else:
                ranked = self._rank_nia_reward_offers(offers)
            session.ranking_source_offers = offers
            session.ranked_offers = tuple(ranked)
            session.ranking_evidence = (
                dict(self._nia_reward_ranking_evidence)
                if isinstance(self._nia_reward_ranking_evidence, Mapping)
                else None
            )
        self._nia_reward_ranking_evidence = (
            dict(session.ranking_evidence)
            if isinstance(session.ranking_evidence, Mapping)
            else None
        )
        return session.ranked_offers

    def _current_plan2_drink_ids(
        self,
        *,
        expected_count: int | None = None,
    ) -> tuple[str, ...]:
        """Read exact ordered drinks, preferring ExamSave over PlayLog replay.

        ExamSave exists during lessons/auditions and remains the strongest
        current-state authority.  Outer pages can legitimately remove that
        sibling file; then the active directory's ProducePlayLog is replayed
        only while every drink mutation is schema-proven.  An ambiguous use or
        replacement returns no inventory instead of guessing from the last
        three acquisitions.
        """

        if (
            expected_count is not None
            and (
                isinstance(expected_count, bool)
                or not isinstance(expected_count, int)
                or expected_count < 0
            )
        ):
            return ()
        try:
            from .plan3_audition_advisor_gui import (
                select_plan3_exam_local_save_path,
            )

            selector = self.exam_save_selector or select_plan3_exam_local_save_path
            selected = selector(self.game_root)
            path = None if selected is None else Path(selected)
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return ()
        if path is None:
            return ()

        if path.is_file():
            try:
                evidence = self.plan2_evidence_loader(path)
                runtime = evidence.state.root_runtime
                raw = None if runtime is None else runtime.opaque_fields.to_value()
                rows = raw.get("drinkList") if isinstance(raw, Mapping) else None
                if not isinstance(rows, list):
                    return ()
                result: list[str] = []
                for row in rows:
                    if not isinstance(row, Mapping):
                        return ()
                    drink_id = row.get("_id")
                    if (
                        not isinstance(drink_id, str)
                        or not drink_id.startswith("pdrink_")
                    ):
                        return ()
                    result.append(drink_id)
                if expected_count is not None and len(result) != expected_count:
                    return ()
                return tuple(result)
            except (FileNotFoundError, OSError, TypeError, ValueError):
                # An existing but unreadable ExamSave is not permission to use
                # a weaker historical replay.
                return ()

        try:
            from .local_save_decoder import decode_local_save_file
            from .produce_outer_local_save import (
                PRODUCE_PLAY_LOG_FILENAME,
                PRODUCE_PLAY_LOG_SOURCE_TYPE,
            )

            play_log_path = path.parent / PRODUCE_PLAY_LOG_FILENAME
            envelope = decode_local_save_file(
                play_log_path,
                PRODUCE_PLAY_LOG_SOURCE_TYPE,
            )
            raw_play_log = json.loads(envelope.plaintext.decode("utf-8"))
            replayed = _replay_produce_play_log_drink_inventory(
                raw_play_log,
                capacity=3,
            )
            if replayed is None:
                return ()
            if expected_count is not None and len(replayed) != expected_count:
                return ()
            return replayed
        except (
            FileNotFoundError,
            OSError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
        ):
            return ()

    def _current_outer_drink_count(
        self,
        snapshot: ProduceOuterLocalSaveSnapshot | None = None,
    ) -> int | None:
        """Return a proven non-empty outer drink count when one is available.

        The outer LocalSave snapshot does not serialize the current ordered
        drink list.  Reuse the existing ExamSave/PlayLog identity reader only
        for an outing owner; an unresolved or genuinely empty result remains
        ``None`` so the catalog advisor does not invent a capacity fact.
        """

        snapshot_count = getattr(snapshot, "drink_count", None)
        if (
            isinstance(snapshot_count, int)
            and not isinstance(snapshot_count, bool)
            and 0 <= snapshot_count <= 3
        ):
            return snapshot_count
        try:
            drink_ids = self._current_plan2_drink_ids()
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return None
        return len(drink_ids) if drink_ids else None

    @staticmethod
    def _nia_drink_keep_effect_reads(
        capture: Mapping[str, Any],
        rows: Sequence[Mapping[str, Any]],
    ) -> tuple[Mapping[str, Any], ...]:
        """Resolve rows from fixed artwork, with effect OCR as fallback."""

        import numpy as np

        from .live_source import _live_text_recognizer

        path_value = capture.get("png_path")
        if not isinstance(path_value, str) or not path_value:
            raise ValueError("N.I.A. drink keep capture has no PNG path")
        path = Path(path_value)
        recognizer = None
        with Image.open(path) as source:
            image = source.convert("RGB")
            array = np.asarray(image)
            result: list[Mapping[str, Any]] = []
            try:
                entry_by_asset = {
                    entry.asset_id: entry
                    for entry in _nia_produce_item_names()
                    if entry.asset_id and "ProduceDrink" in entry.effect_types
                }
            except (
                FileNotFoundError,
                OSError,
                TypeError,
                ValueError,
                sqlite3.Error,
            ):
                entry_by_asset = {}
            for row in rows:
                raw_box = row.get("box")
                if not (
                    isinstance(raw_box, list)
                    and len(raw_box) == 4
                    and all(isinstance(value, int) for value in raw_box)
                ):
                    raise ValueError("N.I.A. drink keep row has no typed box")
                left, top, right, bottom = raw_box

                icon_identity = _nia_drink_icon_identity(array, row)
                if icon_identity is not None:
                    entry = entry_by_asset.get(str(icon_identity["asset_id"]))
                    if entry is not None:
                        icon_mse = float(icon_identity["icon_mse"])
                        runner_up_mse = float(
                            icon_identity["icon_runner_up_mse"]
                        )
                        result.append(
                            {
                                "text": "",
                                "lines": [],
                                "item_id": entry.item_id,
                                "identity_authority": (
                                    "visible-drink-icon-template"
                                ),
                                "asset_id": entry.asset_id,
                                "icon_box": list(icon_identity["icon_box"]),
                                "icon_mse": icon_mse,
                                "icon_runner_up_mse": runner_up_mse,
                                "icon_mse_margin": float(
                                    icon_identity["icon_mse_margin"]
                                ),
                                "identity_score": 1.0 - icon_mse,
                                "runner_up_score": 1.0 - runner_up_mse,
                            }
                        )
                        continue

                # The blue Plan2 status glyph overlaps the first two glyphs of
                # ``幹勁`` when the crop starts immediately after the drink
                # icon.  Begin after that status glyph; ordinary multi-line
                # effects still start farther right and remain intact.
                text_left = max(left, left + 127)
                text_right = min(right, right - 48)
                if text_right - text_left < 200 or bottom - top < 24:
                    raise ValueError("N.I.A. drink keep row has no OCR area")
                crop = array[top:bottom, text_left:text_right]
                if crop.size == 0:
                    raise ValueError("N.I.A. drink keep row OCR crop is empty")
                if recognizer is None:
                    recognizer = _live_text_recognizer()
                density = (crop.mean(axis=2) < 180).mean(axis=1)
                mask = density >= 0.006
                bands: list[tuple[int, int]] = []
                start: int | None = None
                for offset, active in enumerate((*mask.tolist(), False)):
                    if active and start is None:
                        start = offset
                    elif not active and start is not None:
                        if offset - start >= 2:
                            bands.append(
                                (
                                    max(top, top + start - 7),
                                    min(bottom, top + offset + 7),
                                )
                            )
                        start = None
                lines: list[Mapping[str, Any]] = []
                for band_top, band_bottom in bands:
                    read = recognizer.recognize(
                        image.crop((text_left, band_top, text_right, band_bottom))
                    )
                    text = str(read.text).strip()
                    if read.confidence >= 0.65 and _normalize_nia_item_text(text):
                        lines.append(
                            {
                                "text": text,
                                "confidence": read.confidence,
                                "box": [text_left, band_top, text_right, band_bottom],
                            }
                        )
                result.append(
                    {
                        "text": " ".join(str(line["text"]) for line in lines),
                        "lines": lines,
                    }
                )
        return tuple(result)

    def _ranked_nia_drink_keep_surface(
        self,
        outer: Mapping[str, Any],
        *,
        capture: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        """Rebuild and drive a generic current-frame N-select-3 transaction."""

        if outer.get("kind") not in {"drink-keep", "drink-keep-confirm"}:
            return None

        outer_evidence = outer.get("evidence")
        evidence = (
            dict(outer_evidence) if isinstance(outer_evidence, Mapping) else {}
        )

        def wait(
            reason: str,
            *,
            timeout_reason: str = "drink-keep-candidates-unresolved",
            **details: Any,
        ) -> Mapping[str, Any]:
            return {
                "capture": dict(capture),
                "kind": "wait",
                "target": "drink-keep-ranked-selection",
                "wait_timeout_reason": timeout_reason,
                "evidence": {
                    "policy": "zero-input-until-exact-n-drink-set",
                    "reason": reason,
                    **details,
                },
            }

        if evidence.get("complete") is not True:
            return wait(
                str(evidence.get("reason") or "drink-layout-incomplete"),
                observed=evidence,
            )
        raw_rows = evidence.get("rows")
        section_break_after = evidence.get("section_break_after")
        remaining = evidence.get("remaining")
        keep_box = evidence.get("keep_box")
        if not isinstance(raw_rows, list) or len(raw_rows) < 3:
            return wait("drink-candidate-row-count-unresolved")
        if not (
            isinstance(section_break_after, int)
            and not isinstance(section_break_after, bool)
            and 1 <= section_break_after < len(raw_rows)
        ):
            return wait("drink-section-boundary-unresolved")
        if not (
            isinstance(remaining, int)
            and not isinstance(remaining, bool)
            and 0 <= remaining <= 3
        ):
            return wait("drink-remaining-count-unresolved")

        rows: list[Mapping[str, Any]] = []
        for expected_index, raw_row in enumerate(raw_rows, start=1):
            if not isinstance(raw_row, Mapping):
                return wait("drink-row-is-not-structured")
            raw_box = raw_row.get("box")
            selected = raw_row.get("selected")
            if not (
                raw_row.get("index") == expected_index
                and isinstance(raw_box, list)
                and len(raw_box) == 4
                and all(isinstance(value, int) for value in raw_box)
                and isinstance(selected, bool)
            ):
                return wait("drink-row-state-unresolved", row=expected_index)
            left, top, right, bottom = raw_box
            if not (0 <= left < right <= 720 and 0 <= top < bottom <= 1280):
                return wait("drink-row-box-invalid", row=expected_index)
            rows.append(dict(raw_row))

        selected_indices = frozenset(
            index
            for index, row in enumerate(rows)
            if row.get("selected") is True
        )
        capacity = len(selected_indices) + remaining
        if capacity != 3:
            return wait(
                "drink-selected-and-remaining-count-disagree",
                selected_count=len(selected_indices),
                remaining=remaining,
            )

        owned_row_count = len(rows) - section_break_after
        inventory = self._current_plan2_drink_ids(
            expected_count=owned_row_count,
        )
        by_id = {entry.item_id: entry for entry in _nia_produce_item_names()}
        acquired_rows = rows[:section_break_after]
        # ExamSave is the strongest identity authority while an exam is open.
        # Between exams the game removes that file, and ProducePlayLog does not
        # record drinks consumed inside the preceding exam.  In that case its
        # acquisition replay can be intentionally unresolved even though this
        # capacity page exposes every owned drink's full effect text.  Read all
        # rows only for that fallback; each row still needs the same unique
        # translated-Master match used for newly acquired drinks.
        use_visible_owned_identity = len(inventory) != owned_row_count
        effect_rows = rows if use_visible_owned_identity else acquired_rows

        # Defer the one recovery swipe until after the current frame has had a
        # chance to resolve every visible identity.  A clipped row whose icon
        # or effect text is still complete must not be scrolled needlessly.
        tail_clip_evidence = _nia_drink_owned_tail_clip_evidence(
            rows,
            section_break_after=section_break_after,
        )

        def propose_owned_tail_scroll() -> Mapping[str, Any]:
            """Return the single typed swipe or the bounded post-swipe wait."""

            clipped_indices = [
                int(tail_clip_evidence["tail_row_index"])
            ] if tail_clip_evidence is not None else []
            if self._nia_drink_keep_scroll_attempted:
                return wait(
                    "drink-owned-visible-identity-still-clipped-after-scroll",
                    timeout_reason="drink-owned-visible-identity-scroll-unresolved",
                    full_inventory=True,
                    visible_owned_identity=True,
                    clipped_owned_rows=clipped_indices,
                    clip_detection=dict(tail_clip_evidence or {}),
                    scroll_attempted=True,
                )
            try:
                scroll_action = SuggestedSwipe(
                    label="nia-subpage:drink-keep-scroll-owned-tail",
                    canonical_x1=360,
                    canonical_y1=900,
                    canonical_x2=360,
                    canonical_y2=730,
                    duration_ms=400,
                    source_png_path=str(capture["png_path"]),
                    source_timestamp=float(capture["timestamp"]),
                    source_hwnd=int(capture["hwnd"]),
                    source_pid=int(capture["pid"]),
                    verification_box=(0, 280, 720, 1080),
                )
            except (KeyError, TypeError, ValueError) as error:
                return wait(
                    "drink-owned-visible-identity-scroll-action-unbound",
                    error=f"{type(error).__name__}:{error}",
                    full_inventory=True,
                    visible_owned_identity=True,
                    clipped_owned_rows=clipped_indices,
                    clip_detection=dict(tail_clip_evidence or {}),
                )
            self._nia_drink_keep_scroll_attempted = True
            return {
                "capture": dict(capture),
                "kind": "drink-keep-scroll",
                "target": "scroll-owned-tail",
                "action": scroll_action.to_dict(),
                "evidence": {
                    "policy": "one-small-swipe-for-clipped-owned-drink-identity",
                    "full_inventory": True,
                    "visible_owned_identity": True,
                    "owned_row_count": owned_row_count,
                    "clipped_owned_rows": clipped_indices,
                    "clip_detection": dict(tail_clip_evidence or {}),
                    "scroll_attempted": True,
                    "scroll": {
                        "canonical_x1": 360,
                        "canonical_y1": 900,
                        "canonical_x2": 360,
                        "canonical_y2": 730,
                        "duration_ms": 400,
                        "fresh_capture": "executor-post-swipe",
                    },
                },
            }
        try:
            effect_reads = self._nia_drink_keep_effect_reads(
                capture,
                effect_rows,
            )
        except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError) as error:
            if (
                evidence.get("full_inventory") is True
                and use_visible_owned_identity
                and tail_clip_evidence is not None
            ):
                return propose_owned_tail_scroll()
            return wait(
                (
                    "drink-all-candidate-effect-ocr-failed"
                    if use_visible_owned_identity
                    else "drink-acquired-effect-ocr-failed"
                ),
                error=f"{type(error).__name__}:{error}",
            )
        if len(effect_reads) != len(effect_rows):
            if (
                evidence.get("full_inventory") is True
                and use_visible_owned_identity
                and tail_clip_evidence is not None
            ):
                return propose_owned_tail_scroll()
            return wait(
                "drink-effect-read-count-does-not-match-visible-rows",
                effect_read_count=len(effect_reads),
                visible_effect_rows=len(effect_rows),
            )

        candidates: list[Mapping[str, Any]] = []
        entries: list[_NiaProduceItemName] = []
        for index, (row, effect_read) in enumerate(
            zip(acquired_rows, effect_reads),
            start=1,
        ):
            visible_effect = str(effect_read.get("text", ""))
            matched = None
            icon_item_id = effect_read.get("item_id")
            if isinstance(icon_item_id, str) and icon_item_id:
                icon_entry = by_id.get(icon_item_id)
                if icon_entry is not None and "ProduceDrink" in icon_entry.effect_types:
                    matched = (
                        icon_entry,
                        float(effect_read.get("identity_score", 0.0)),
                        float(effect_read.get("runner_up_score", 0.0)),
                    )
            if matched is None:
                matched = _match_nia_drink_effect(visible_effect)
            if matched is None:
                return wait(
                    "drink-acquired-identity-unresolved",
                    row=index,
                    effect_read=dict(effect_read),
                )
            entry, match_score, runner_up_score = matched
            entries.append(entry)
            candidates.append(
                {
                    "index": index,
                    "section": "acquired",
                    "item_id": entry.item_id,
                    "display_name": entry.display_name,
                    "rarity": entry.rarity,
                    "selected": row["selected"],
                    "box": list(row["box"]),
                    "visible_effect": visible_effect,
                    "effect_lines": list(effect_read.get("lines", [])),
                    "identity_score": match_score,
                    "runner_up_score": runner_up_score,
                    "identity_authority": str(
                        effect_read.get("identity_authority")
                        or "visible-effect-unique-translated-master-match"
                    ),
                    **{
                        key: effect_read[key]
                        for key in (
                            "asset_id",
                            "icon_box",
                            "icon_mse",
                            "icon_runner_up_mse",
                            "icon_mse_margin",
                        )
                        if key in effect_read
                    },
                }
            )

        if use_visible_owned_identity:
            visible_inventory: list[str] = []
            owned_effect_reads = effect_reads[section_break_after:]
            for offset, (row, effect_read) in enumerate(
                zip(rows[section_break_after:], owned_effect_reads),
                start=section_break_after + 1,
            ):
                visible_effect = str(effect_read.get("text", ""))
                matched = None
                icon_item_id = effect_read.get("item_id")
                if isinstance(icon_item_id, str) and icon_item_id:
                    icon_entry = by_id.get(icon_item_id)
                    if icon_entry is not None and "ProduceDrink" in icon_entry.effect_types:
                        matched = (
                            icon_entry,
                            float(effect_read.get("identity_score", 0.0)),
                            float(effect_read.get("runner_up_score", 0.0)),
                        )
                if matched is None:
                    matched = _match_nia_drink_effect(visible_effect)
                if matched is None:
                    if (
                        offset == len(rows)
                        and evidence.get("full_inventory") is True
                        and tail_clip_evidence is not None
                    ):
                        return propose_owned_tail_scroll()
                    return wait(
                        "drink-owned-visible-identity-unresolved",
                        row=offset,
                        effect_read=dict(effect_read),
                        unresolved_locsave_inventory=list(inventory),
                        visible_owned_rows=owned_row_count,
                        clip_detection=dict(tail_clip_evidence or {}),
                    )
                entry, match_score, runner_up_score = matched
                entries.append(entry)
                visible_inventory.append(entry.item_id)
                candidates.append(
                    {
                        "index": offset,
                        "section": "owned",
                        "item_id": entry.item_id,
                        "display_name": entry.display_name,
                        "rarity": entry.rarity,
                        "selected": row["selected"],
                        "box": list(row["box"]),
                        "visible_effect": visible_effect,
                        "effect_lines": list(effect_read.get("lines", [])),
                        "identity_score": match_score,
                        "runner_up_score": runner_up_score,
                        "identity_authority": (
                            str(
                                effect_read.get("identity_authority")
                                or "visible-effect-unique-translated-master-match"
                            )
                        ),
                        **{
                            key: effect_read[key]
                            for key in (
                                "asset_id",
                                "icon_box",
                                "icon_mse",
                                "icon_runner_up_mse",
                                "icon_mse_margin",
                            )
                            if key in effect_read
                        },
                    }
                )
            inventory = tuple(visible_inventory)
        else:
            for offset, (row, drink_id) in enumerate(
                zip(rows[section_break_after:], inventory),
                start=section_break_after + 1,
            ):
                entry = by_id.get(drink_id)
                if entry is None or "ProduceDrink" not in entry.effect_types:
                    return wait(
                        "drink-localsave-owned-identity-unresolved",
                        row=offset,
                        item_id=drink_id,
                    )
                entries.append(entry)
                candidates.append(
                    {
                        "index": offset,
                        "section": "owned",
                        "item_id": entry.item_id,
                        "display_name": entry.display_name,
                        "rarity": entry.rarity,
                        "selected": row["selected"],
                        "box": list(row["box"]),
                        "identity_authority": "ordered-exam-save-drink-list",
                    }
                )

        exam_effect_type = (
            ""
            if self.plan_type not in {PLAN1, PLAN2, PLAN3}
            else self._current_exam_effect_type()
        )
        leaderboard_drink_prior = None
        if exam_effect_type:
            try:
                from .leaderboard_drink_prior import (
                    try_load_default_leaderboard_drink_prior,
                )

                leaderboard_drink_prior = (
                    try_load_default_leaderboard_drink_prior(
                        produce_id=self.produce_id,
                        idol_card_id=self.idol_card_id,
                        plan_type=self.plan_type,
                        exam_effect_type=exam_effect_type,
                    )
                )
            except (FileNotFoundError, OSError, TypeError, ValueError):
                # The learned signal is optional.  Static Master/effect
                # ranking remains the complete fallback when a source scope
                # is unavailable or abstains.
                leaderboard_drink_prior = None
        ranked = _best_nia_drink_keep_indices(
            entries,
            exam_effect_type=exam_effect_type,
            capacity=capacity,
            leaderboard_drink_prior=leaderboard_drink_prior,
        )
        if ranked is None:
            return wait("drink-best-set-unresolved", candidates=candidates)
        best_indices, best_details = ranked
        desired = frozenset(best_indices)
        candidate_signature = tuple(
            (
                str(candidate["section"]),
                str(candidate["item_id"]),
                *tuple(int(value) for value in candidate["box"]),
            )
            for candidate in candidates
        )
        observed_state = (tuple(sorted(selected_indices)), remaining)
        session = self._nia_drink_keep_session
        retry_pending: tuple[
            str | None,
            int | None,
            tuple[tuple[int, ...], int],
        ] | None = None
        if (
            session is None
            or session.candidate_signature != candidate_signature
            or session.desired_indices != desired
        ):
            session = _NiaDrinkKeepSession(
                candidate_signature=candidate_signature,
                desired_indices=desired,
            )
            self._nia_drink_keep_session = session
        elif session.pending_state is not None:
            if observed_state == session.pending_state:
                session.unchanged_reads += 1
                # SendMessage proves that Windows accepted the request, not
                # that Unity consumed it.  Hold the transaction owner for four
                # unchanged reads, then allow exactly one retry of the same
                # input.  This prevents both click spam and a permanent wait
                # after one dropped input.
                if session.unchanged_reads < 4 or session.pending_attempts >= 2:
                    return wait(
                        "drink-input-awaiting-visible-state-change",
                        timeout_reason="drink-keep-state-transition-timeout",
                        pending_kind=session.pending_kind,
                        pending_index=(
                            None
                            if session.pending_index is None
                            else session.pending_index + 1
                        ),
                        pending_state={
                            "selected_indices": [
                                index + 1 for index in session.pending_state[0]
                            ],
                            "remaining": session.pending_state[1],
                        },
                        unchanged_reads=session.unchanged_reads,
                        pending_attempts=session.pending_attempts,
                    )
                retry_pending = (
                    session.pending_kind,
                    session.pending_index,
                    session.pending_state,
                )
                session.pending_kind = None
                session.pending_index = None
                session.pending_state = None
                session.unchanged_reads = 0
            else:
                session.pending_kind = None
                session.pending_index = None
                session.pending_state = None
                session.unchanged_reads = 0
                session.pending_attempts = 0

        def arm_pending(kind: str, index: int | None) -> None:
            if retry_pending == (kind, index, observed_state):
                session.pending_attempts += 1
            else:
                session.pending_attempts = 1
            session.pending_kind = kind
            session.pending_index = index
            session.pending_state = observed_state
            session.unchanged_reads = 0
        ranking_evidence = {
            "policy": "exact-candidate-master-rarity-effect-set-ranking",
            "candidate_count": len(candidates),
            "capacity": capacity,
            "ordered_inventory": list(inventory),
            "candidates": candidates,
            "best_indices": [index + 1 for index in best_indices],
            "best_set": [entries[index].item_id for index in best_indices],
            "best_details": [dict(detail) for detail in best_details],
            "selected_indices": [index + 1 for index in sorted(selected_indices)],
            "remaining": remaining,
        }

        extras = selected_indices.difference(desired)
        if extras:
            index = max(extras)
            candidate = candidates[index]
            action = self._nia_card_operation_action(
                capture,
                label=(
                    "nia-subpage:drink-keep-toggle-off:"
                    f"{index + 1}:{candidate['item_id']}"
                ),
                box=tuple(int(value) for value in rows[index]["box"]),
            )
            arm_pending("toggle-off", index)
            return {
                "capture": dict(capture),
                "kind": "drink-keep-toggle-off",
                "target": f"row-{index + 1}:{candidate['item_id']}",
                "action": action.to_dict(),
                "evidence": ranking_evidence,
            }

        missing = desired.difference(selected_indices)
        if missing:
            if remaining <= 0:
                return wait(
                    "drink-best-row-missing-with-no-open-capacity",
                    **ranking_evidence,
                )
            detail_by_index = {
                int(detail["index"]): detail for detail in best_details
            }
            index = max(
                missing,
                key=lambda value: (
                    int(detail_by_index[value]["score"]),
                    entries[value].item_id,
                    -value,
                ),
            )
            candidate = candidates[index]
            action = self._nia_card_operation_action(
                capture,
                label=(
                    "nia-subpage:drink-keep-toggle-on:"
                    f"{index + 1}:{candidate['item_id']}"
                ),
                box=tuple(int(value) for value in rows[index]["box"]),
            )
            arm_pending("toggle-on", index)
            return {
                "capture": dict(capture),
                "kind": "drink-keep-toggle-on",
                "target": f"row-{index + 1}:{candidate['item_id']}",
                "action": action.to_dict(),
                "evidence": ranking_evidence,
            }

        if remaining != 0 or len(selected_indices) != capacity:
            return wait(
                "drink-best-set-not-yet-stable",
                **ranking_evidence,
            )
        if not (
            isinstance(keep_box, list)
            and len(keep_box) == 4
            and all(isinstance(value, int) for value in keep_box)
        ):
            return wait("drink-keep-button-box-unresolved", **ranking_evidence)
        action = self._nia_card_operation_action(
            capture,
            label="nia-subpage:drink-keep-confirm:best-set",
            box=tuple(keep_box),
        )
        arm_pending("confirm", None)
        return {
            "capture": dict(capture),
            "kind": "drink-keep-confirm",
            "target": "keep-best-three",
            "action": action.to_dict(),
            "evidence": ranking_evidence,
        }

    def _current_exam_effect_type(self) -> str:
        from .master_db import get_idol_profile

        profile = get_idol_profile(self.idol_card_id)
        return "" if profile is None else profile.exam_effect_type

    def _clear_initial_choice_transaction(self) -> None:
        self._initial_choice_transaction = None
        self._nia_reward_preview_session = None
        self._nia_reward_pending_state = None
        self._nia_reward_ranking_evidence = None
        self._nia_reward_ocr_session = None
        self._nia_reward_reroll_pending = None
        self._nia_reward_ocr_authority_key = None
        self._nia_item_reward_session = None
        self._nia_item_reward_authority_key = None

    def _clear_all_nia_sessions(self) -> None:
        self._clear_initial_choice_transaction()
        self._nia_event_choice_pending = None
        self._reset_event_confirm_retry()
        self._nia_card_operation_session = None
        self._nia_drink_keep_session = None
        self._nia_drink_keep_scroll_attempted = False
        self._nia_drink_reject_selected = False
        self._nia_drink_reject_select_attempts = 0
        self._nia_drink_reject_select_wait_reads = 0
        self._nia_drink_reject_submitted = False
        self._nia_drink_reject_confirm_attempts = 0
        self._nia_drink_reject_wait_reads = 0

    def _nia_active_recognition_scope(
        self,
        legacy_action: str | None = None,
    ) -> str | None:
        """Project the active transaction cursor to one Maa recognition family."""

        transaction = self._active_outer_transaction
        if transaction is None:
            # Page-local sessions may narrow legacy/direct readers, but can
            # never override a bound transaction cursor.
            if self._nia_card_operation_session is not None:
                return "card-operation"
            if any(
                value is not None
                for value in (
                    self._initial_choice_transaction,
                    self._nia_reward_preview_session,
                    self._nia_reward_ocr_session,
                    self._nia_item_reward_session,
                    self._nia_drink_keep_session,
                    self._nia_reward_reroll_pending,
                )
            ) or any(
                (
                    self._nia_drink_reject_selected,
                    self._nia_drink_reject_submitted,
                )
            ):
                return "reward"
            if legacy_action is None:
                return None
            from .nia_week_gate import nia_recognition_scope_for_action

            return nia_recognition_scope_for_action(legacy_action)
        surface = transaction.current_surface
        if (
            transaction.phase in {PHASE_SUBMITTED, PHASE_SETTLING}
            and transaction.current_ordinal < len(transaction.surfaces)
        ):
            surface = transaction.surfaces[transaction.current_ordinal]
        if surface is not None:
            if surface.kind in {
                "reward-choice",
                "reward-confirm",
                "item-reward-choice",
                "item-reward-confirm",
                "drink-keep",
            }:
                return "reward"
            if surface.kind in {
                "card-operation-page",
                "card-operation-submit",
                "card-customize-option-select",
                "card-customize-option-execute",
            }:
                if (
                    transaction.owner.selected_action == SPECIAL_GUIDANCE
                ):
                    # Special Guidance begins on Maa's guide-page template and
                    # then enters the shared card-operation controls.  Keep the
                    # wider guidance batch so the original Japanese/template
                    # owner remains available alongside localized OCR.
                    return "guidance"
                return "card-operation"
            for family, kinds in {
                "business": {"business-choice", "work-start"},
                "activity": {"activity-story"},
                "outing": {"outing-choice"},
                "lesson": {"lesson-start", "lesson-result"},
                "consultation": {"consultation-shop", "shop-exit"},
                "guidance": {"guide-exit"},
                "rest": {"rest-confirm"},
            }.items():
                if surface.kind in kinds:
                    return family
        from .nia_week_gate import nia_recognition_scope_for_action

        return nia_recognition_scope_for_action(transaction.owner.selected_action)

    def _event_choice_authority_matches(
        self,
        snapshot: ProduceOuterLocalSaveSnapshot,
        pending: Any,
    ) -> bool:
        fields = (
            ("log_count", "log_count"),
            ("week", "latest_week_marker"),
            ("last_completed_week", "last_completed_week"),
            ("stamina", "stamina"),
            ("max_stamina", "max_stamina"),
            ("produce_points", "produce_points"),
            ("vocal", "vocal"),
            ("dance", "dance"),
            ("visual", "visual"),
            ("vote_count", "vote_count"),
        )
        authority = getattr(pending, "authority", None)
        if authority is None and isinstance(pending, Mapping):
            authority = pending.get("authority")
        if not isinstance(authority, Mapping):
            return False
        return all(
            getattr(snapshot, snapshot_field, None) == authority.get(authority_field)
            for authority_field, snapshot_field in fields
        )

    def _remember_event_choice_pending(
        self,
        outer: Mapping[str, Any],
    ) -> None:
        """Record one event selection before its Maa input executes."""

        kind = outer.get("kind")
        if kind == "event-choice":
            self._reset_event_confirm_retry()
            # Generic weekly event pages do not have a Master ADV identity to
            # put in NiaEventChoicePending.  They only need the selected action
            # and the same outer LocalSave authority; this small owner is
            # enough to bind the next SELECT to the click that produced it.
            target = outer.get("target")
            authority = outer.get("outer_authority")
            capture = outer.get("capture")
            if (
                not isinstance(target, str)
                or not target
                or not isinstance(authority, Mapping)
                or not isinstance(capture, Mapping)
            ):
                self._nia_event_choice_pending = None
                return
            pending = {
                "schema": "gkms.nia-event-choice-selection-pending.v1",
                "owner": "generic",
                "target": target,
                "authority": dict(authority),
                "source_capture": dict(capture),
            }
            self._nia_event_choice_pending = pending
            if isinstance(outer, dict):
                evidence = outer.get("evidence")
                if isinstance(evidence, Mapping):
                    outer["evidence"] = {
                        **dict(evidence),
                        "pending_event_choice": dict(pending),
                    }
            return
        if kind not in {
            "static-event-choice-select",
            "static-event-choice-confirm",
            "outing-choice",
        }:
            return
        evidence = outer.get("evidence")
        if not isinstance(evidence, Mapping) or evidence.get("resume") is True:
            return
        from .nia_live_outer import NiaEventChoicePending

        try:
            pending = NiaEventChoicePending.from_surface(
                outer,
                produce_id=self.produce_id,
                idol_card_id=self.idol_card_id,
            )
            self._nia_event_choice_pending = pending
            if isinstance(outer, dict):
                outer["evidence"] = {
                    **dict(evidence),
                    "pending_event_choice": pending.to_dict(),
                }
        except (TypeError, ValueError):
            # A Master-bound surface without the new exact row/capture proof
            # is not safe to resume.  Keep no speculative owner; the typed
            # surface itself remains auditable and the normal reader can
            # rebind it on the next frame.
            self._nia_event_choice_pending = None

    def _reset_event_confirm_retry(self) -> None:
        self._nia_event_confirm_signature = None
        self._nia_event_confirm_attempts = 0
        self._nia_event_confirm_wait_reads = 0

    @staticmethod
    def _event_confirm_signature(
        outer: Mapping[str, Any],
    ) -> tuple[object, ...]:
        authority = outer.get("outer_authority")
        if not isinstance(authority, Mapping):
            authority = {}
        action = outer.get("action")
        if not isinstance(action, Mapping):
            action = {}
        return (
            outer.get("kind"),
            outer.get("target"),
            action.get("label"),
            action.get("canonical_x"),
            action.get("canonical_y"),
            authority.get("log_count"),
            authority.get("week"),
            authority.get("last_completed_week"),
            authority.get("stamina"),
            authority.get("produce_points"),
            authority.get("vocal"),
            authority.get("dance"),
            authority.get("visual"),
        )

    def _route_generic_event_confirm_retry(
        self,
        outer: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Retry one unchanged generic SELECT after four read-only polls."""

        signature = self._event_confirm_signature(outer)
        if signature != self._nia_event_confirm_signature:
            self._nia_event_confirm_signature = signature
            self._nia_event_confirm_attempts = 1
            self._nia_event_confirm_wait_reads = 0
            return outer
        self._nia_event_confirm_wait_reads += 1
        if (
            self._nia_event_confirm_attempts < 2
            and self._nia_event_confirm_wait_reads >= 4
        ):
            self._nia_event_confirm_attempts = 2
            self._nia_event_confirm_wait_reads = 0
            evidence = outer.get("evidence")
            return {
                **dict(outer),
                "evidence": {
                    **(dict(evidence) if isinstance(evidence, Mapping) else {}),
                    "confirm_attempt": 2,
                    "retry_reason": "unchanged-page-after-maa-background-input",
                },
            }
        return {
            "capture": dict(outer.get("capture", {})),
            "kind": "wait",
            "target": "event-choice-page-exit",
            "evidence": {
                "policy": "bounded-maa-confirm-then-await-next-page",
                "confirm_attempts": self._nia_event_confirm_attempts,
                "unchanged_reads": self._nia_event_confirm_wait_reads,
                "pending_target": outer.get("target"),
            },
            "outer_authority": dict(outer.get("outer_authority", {})),
        }

    def _submit_or_wait_for_drink_reject_confirm(
        self,
        outer: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Retry one unchanged Maa confirmation without turning it into spam.

        A successful ``SendMessage`` only proves that Windows accepted the
        input request; Unity can still drop that individual click.  Keep the
        same OCR-resolved transaction and retry once, four unchanged reads
        apart.  No additional pixel, colour, or text gate is introduced.
        """

        if not self._nia_drink_reject_submitted:
            self._nia_drink_reject_submitted = True
            self._nia_drink_reject_confirm_attempts = 1
            self._nia_drink_reject_wait_reads = 0
            return outer
        self._nia_drink_reject_wait_reads += 1
        if (
            self._nia_drink_reject_confirm_attempts < 2
            and self._nia_drink_reject_wait_reads >= 4
        ):
            self._nia_drink_reject_confirm_attempts += 1
            self._nia_drink_reject_wait_reads = 0
            return outer
        return {
            "capture": dict(outer.get("capture", {})),
            "kind": "wait",
            "target": "drink-reject-page-exit",
            "evidence": {
                "policy": "bounded-maa-confirm-then-await-next-page",
                "confirm_attempts": self._nia_drink_reject_confirm_attempts,
                "unchanged_reads": self._nia_drink_reject_wait_reads,
            },
            "outer_authority": dict(outer.get("outer_authority", {})),
        }

    def _route_drink_reject_transaction(
        self,
        outer: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        kind = outer.get("kind")
        if kind == "drink-reject-confirm":
            self._nia_drink_reject_selected = True
            self._nia_drink_reject_select_attempts = 0
            self._nia_drink_reject_select_wait_reads = 0
            return self._submit_or_wait_for_drink_reject_confirm(outer)
        if kind == "drink-reject-select":
            # Do not promote Windows delivery into a visual selection claim.
            # Only the reader's next OCR state (`drink-reject-confirm`) proves
            # that Unity selected 不領取 and enabled the orange confirm button.
            if not self._nia_drink_reject_selected:
                self._nia_drink_reject_selected = True
                self._nia_drink_reject_select_attempts = 1
                self._nia_drink_reject_select_wait_reads = 0
                self._nia_drink_reject_submitted = False
                self._nia_drink_reject_confirm_attempts = 0
                self._nia_drink_reject_wait_reads = 0
                return outer
            self._nia_drink_reject_select_wait_reads += 1
            if (
                self._nia_drink_reject_select_attempts < 2
                and self._nia_drink_reject_select_wait_reads >= 4
            ):
                self._nia_drink_reject_select_attempts = 2
                self._nia_drink_reject_select_wait_reads = 0
                evidence = outer.get("evidence")
                return {
                    **dict(outer),
                    "evidence": {
                        **(
                            dict(evidence)
                            if isinstance(evidence, Mapping)
                            else {}
                        ),
                        "selection_attempt": 2,
                        "retry_reason": (
                            "unselected-after-maa-background-input"
                        ),
                    },
                }
            return {
                "capture": dict(outer.get("capture", {})),
                "kind": "wait",
                "target": "drink-reject-selection",
                "evidence": {
                    "policy": "bounded-maa-select-then-await-ocr-selected",
                    "selection_attempts": self._nia_drink_reject_select_attempts,
                    "unchanged_reads": self._nia_drink_reject_select_wait_reads,
                },
                "outer_authority": dict(outer.get("outer_authority", {})),
            }
        self._nia_drink_reject_selected = False
        self._nia_drink_reject_select_attempts = 0
        self._nia_drink_reject_select_wait_reads = 0
        self._nia_drink_reject_submitted = False
        self._nia_drink_reject_confirm_attempts = 0
        self._nia_drink_reject_wait_reads = 0
        return None

    @staticmethod
    def _reward_probe_action(
        probe: Mapping[str, Any],
        capture: Mapping[str, Any],
    ) -> SuggestedClick:
        raw_box = probe.get("box")
        if not isinstance(raw_box, (list, tuple)) or len(raw_box) != 4:
            raise ValueError("reward preview probe has no valid card box")
        x, y, width, height = (int(value) for value in raw_box)
        box = (x, y, x + width, y + height)
        slot = int(probe.get("slot", 0))
        if slot < 1:
            raise ValueError("reward preview probe has no valid slot")
        return SuggestedClick(
            label=f"nia-reward:preview-slot-{slot}",
            canonical_x=(box[0] + box[2]) // 2,
            canonical_y=(box[1] + box[3]) // 2,
            source_png_path=str(capture["png_path"]),
            source_timestamp=float(capture["timestamp"]),
            source_hwnd=int(capture["hwnd"]),
            source_pid=int(capture["pid"]),
            verification_box=box,
            click_count=1,
        )

    def _nia_reward_surface(
        self,
        *,
        capture: Mapping[str, Any],
        outer_payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Advance one OCR-backed N.I.A. card-reward inspection step."""

        from .live_source import read_live_reward_preview_workflow
        from .reward_state import RewardCardOffer

        if self._nia_reward_pending_state is not None:
            if outer_payload.get("target") != "cards-get":
                raise ValueError("selected N.I.A. reward has no receive button")
            state = self._nia_reward_pending_state
            offers = tuple(
                RewardCardOffer(**dict(value)) for value in state["offers"]
            )
            recommended = offers[0]
            from .reward_state import REWARD_COLLECT_BOX

            left, top, right, bottom = REWARD_COLLECT_BOX
            action = SuggestedClick(
                label=f"nia-reward:receive:{recommended.card_id}@{recommended.upgrade}",
                canonical_x=(left + right) // 2,
                canonical_y=(top + bottom) // 2,
                source_png_path=str(capture["png_path"]),
                source_timestamp=float(capture["timestamp"]),
                source_hwnd=int(capture["hwnd"]),
                source_pid=int(capture["pid"]),
                verification_box=REWARD_COLLECT_BOX,
                click_count=1,
            )
            self._nia_reward_pending_state = None
            self._nia_reward_preview_session = None
            return {
                "capture": dict(capture),
                "action": action.to_dict(),
                "stage": "confirm",
                "state": state,
                "evidence": {
                    "policy": "single-receive-after-title-ranking",
                    "offers": [dict(value) for value in state["offers"]],
                    **self._nia_reward_evidence_fields(),
                },
                "method": "MAA receive button + next Produce LocalSave state",
            }

        session = self._nia_reward_preview_session
        result = dict(
            read_live_reward_preview_workflow(
                session=session,
                expected_run_id=self.expected_run_id,
                expected_idol_card_id=self.idol_card_id,
                expected_produce_id=self.produce_id,
                capture=capture,
                require_preview_all=True,
            )
        )
        self._nia_reward_preview_session = result["workflow"]
        stage = str(result.get("stage", ""))
        capture = result.get("capture")
        if not isinstance(capture, Mapping):
            raise ValueError("N.I.A. reward preview returned no capture")
        if stage == "needs_preview":
            probe = result.get("next_probe")
            if not isinstance(probe, Mapping):
                raise ValueError("N.I.A. reward preview returned no next probe")
            action = self._reward_probe_action(probe, capture)
            return {
                **result,
                "stage": "preview",
                "target_slot": int(probe["slot"]),
                "action": action.to_dict(),
            }
        if stage != "identified":
            raise ValueError(f"unsupported N.I.A. reward workflow stage: {stage}")

        state = result.get("selection_state")
        if not isinstance(state, Mapping) or not isinstance(state.get("offers"), list):
            raise ValueError("N.I.A. reward workflow returned no resolved offers")
        offers = tuple(RewardCardOffer(**dict(value)) for value in state["offers"])
        ranked = self._rank_nia_reward_offers(offers)
        recommended = ranked[0]
        ranked_state = {
            "offers": [offer.to_dict() for offer in ranked],
            "recommended_slot": recommended.slot,
        }
        self._nia_reward_pending_state = ranked_state
        last_preview = self._nia_reward_preview_session.state.preview_identities[-1]
        if last_preview.selected_slot == recommended.slot:
            # The final reversible preview already selected the winner.  Do
            # not click the same card again; receive it on this same page.
            return self._nia_reward_surface(
                capture=capture,
                outer_payload=outer_payload,
            )
        x, y, width, height = recommended.box
        box = (x, y, x + width, y + height)
        action = SuggestedClick(
            label=f"nia-reward:select:{recommended.card_id}@{recommended.upgrade}",
            canonical_x=(box[0] + box[2]) // 2,
            canonical_y=(box[1] + box[3]) // 2,
            source_png_path=str(capture["png_path"]),
            source_timestamp=float(capture["timestamp"]),
            source_hwnd=int(capture["hwnd"]),
            source_pid=int(capture["pid"]),
            verification_box=box,
            click_count=1,
        )
        return {
            **result,
            "stage": "select",
            "state": ranked_state,
            "recommended": recommended.to_dict(),
            "ranking": [offer.to_dict() for offer in ranked],
            "action": action.to_dict(),
            "method": (
                "all visible slots previewed by OCR; Master identity; "
                "N.I.A. Plan2 archetype ranking"
            ),
        }

    @staticmethod
    def _nia_card_operation_slots(capture: Mapping[str, Any]) -> tuple[tuple[int, int, int, int], ...]:
        """Locate the visible selectable grid; identity never comes from order."""

        from .live_source import _card_detector

        path_value = capture.get("png_path")
        if not isinstance(path_value, str) or not path_value:
            raise ValueError("N.I.A. card operation capture has no PNG path")
        detections = tuple(
            item
            for item in _card_detector().detect_path(Path(path_value)).detections
            if item.label in {"cards", "recommend"} and 600 <= item.y <= 1050
        )
        rows: dict[int, set[int]] = {}
        for item in detections:
            centre_x = item.x + item.width // 2
            centre_y = item.y + item.height // 2
            column = min(range(4), key=lambda value: abs(centre_x - (140 + 147 * value)))
            row = min(range(3), key=lambda value: abs(centre_y - (755 + 149 * value)))
            if abs(centre_x - (140 + 147 * column)) <= 55 and abs(
                centre_y - (755 + 149 * row)
            ) <= 65:
                rows.setdefault(row, set()).add(column)
        if not rows:
            raise ValueError("N.I.A. card operation has no visible selectable cards")

        last_row = max(rows)
        slots: list[tuple[int, int, int, int]] = []
        for row in range(last_row + 1):
            observed = rows.get(row, set())
            if not observed:
                continue
            # Every row above the last occupied row is full.  On the last row,
            # the grid is filled left-to-right; this also recovers a dark tile
            # that the geometry detector omitted without assigning identity.
            final_column = 3 if row < last_row else max(observed)
            for column in range(final_column + 1):
                left = 80 + 147 * column
                top = 692 + 149 * row
                slots.append((left, top, left + 120, top + 128))
        if not slots:
            raise ValueError("N.I.A. card operation grid is empty")
        return tuple(slots)

    def _nia_card_operation_offer(
        self,
        capture: Mapping[str, Any],
        *,
        target: str,
        slot: int,
        box: tuple[int, int, int, int],
    ) -> Any:
        """Read the one selected full title and project its Master value."""

        import re

        from .live_source import _live_text_recognizer, _shop_card_catalog
        from .logic_engine import load_master_card
        from .reward_state import RewardCardOffer, describe_master_card

        path_value = capture.get("png_path")
        if not isinstance(path_value, str) or not path_value:
            raise ValueError("N.I.A. card preview capture has no PNG path")
        title_box = (
            (180, 165, 600, 225)
            if target == "customize"
            else (180, 95, 600, 150)
        )
        recognized = _live_text_recognizer().recognize_path(
            Path(path_value),
            title_box,
        )
        allowed_plans = {"ProducePlanType_Common", self.plan_type}
        raw_title = str(recognized.text).strip()
        # The translucent title panel can expose the PP counter behind a short
        # card name (for example ``閃耀210``).  Digits at the end are not part
        # of any displayed card title on this page; remove only that overlay
        # suffix and retain ``+`` when the selected card is already upgraded.
        title = re.sub(r"\d+$", "", raw_title).strip()
        matches = tuple(
            item
            for item in _shop_card_catalog().match(title, limit=50)
            if item.plan_type in allowed_plans
        )
        if not matches or matches[0].similarity < 0.68:
            raise ValueError(
                f"N.I.A. selected card title was not recognized: {raw_title!r}"
            )
        identity = matches[0]
        master = load_master_card(identity.card_id, identity.upgrade)
        return RewardCardOffer(
            slot=slot,
            box=(box[0], box[1], box[2] - box[0], box[3] - box[1]),
            asset_name=None,
            card_id=identity.card_id,
            upgrade=identity.upgrade,
            display_name=identity.display_name,
            rarity=master.rarity,
            evaluation=master.evaluation,
            stamina_cost=master.stamina_cost,
            force_stamina_cost=master.force_stamina_cost,
            effect_summary=describe_master_card(master),
            art_score=0.0,
            art_margin=0.0,
            game_recommended=False,
            selected=True,
        )

    @staticmethod
    def _nia_card_operation_action(
        capture: Mapping[str, Any],
        *,
        label: str,
        box: tuple[int, int, int, int],
    ) -> SuggestedClick:
        return SuggestedClick(
            label=label,
            canonical_x=(box[0] + box[2]) // 2,
            canonical_y=(box[1] + box[3]) // 2,
            source_png_path=str(capture["png_path"]),
            source_timestamp=float(capture["timestamp"]),
            source_hwnd=int(capture["hwnd"]),
            source_pid=int(capture["pid"]),
            verification_box=box,
            click_count=1,
        )

    @staticmethod
    def _nia_card_operation_submit_box(target: str) -> tuple[int, int, int, int]:
        # Special Guidance shares the card preview/ranking transaction, but
        # places its Customize button above the persistent drink/menu footer.
        # Other card-management pages retain the ordinary bottom button row.
        return (
            (232, 1035, 488, 1130)
            if target == "customize"
            else (232, 1120, 488, 1205)
        )

    @staticmethod
    def _nia_customize_option_header(
        capture: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Read the official third-layer Special Guidance page title."""

        from .live_source import _live_text_recognizer

        path_value = capture.get("png_path")
        if not isinstance(path_value, str) or not path_value:
            raise ValueError("N.I.A. customize option capture has no PNG path")
        read = _live_text_recognizer().recognize_path(
            Path(path_value),
            (250, 740, 470, 800),
        )
        compact = re.sub(r"\s+", "", str(read.text))
        titles = (
            "自定義選單",
            "自定义选单",
            "自定义菜单",
            "カスタマイズメニュー",
            "カスタマイズ選択肢",
        )
        matched = next((title for title in titles if title in compact), None)
        if read.confidence < 0.60 or matched is None:
            raise ValueError("N.I.A. customize option page is not ready")
        return {
            "text": read.text,
            "confidence": read.confidence,
            "matched_title": matched,
        }

    @staticmethod
    def _nia_customize_confirm_header(
        capture: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Recognize the optional final confirmation without using its Cancel X."""

        from .live_source import _live_text_recognizer

        path_value = capture.get("png_path")
        if not isinstance(path_value, str) or not path_value:
            raise ValueError("N.I.A. customize confirm capture has no PNG path")
        read = _live_text_recognizer().recognize_path(
            Path(path_value),
            (40, 180, 500, 245),
        )
        compact = re.sub(r"\s+", "", str(read.text))
        titles = (
            "自定義執行確認",
            "自定义执行确认",
            "カスタマイズ実行確認",
        )
        matched = next((title for title in titles if title in compact), None)
        if read.confidence < 0.60 or matched is None:
            raise ValueError("N.I.A. customize confirmation is not visible")
        return {
            "text": read.text,
            "confidence": read.confidence,
            "matched_title": matched,
        }

    def _nia_customize_confirm_surface(
        self,
        *,
        capture: Mapping[str, Any],
        header: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Submit the optional API confirmation with one bounded right-button owner."""

        session = self._nia_card_operation_session
        if (
            session is None
            or session.target != "customize"
            or not session.customize_execute_submitted
        ):
            raise ValueError("N.I.A. customize confirmation has no execute owner")
        header = (
            dict(header)
            if isinstance(header, Mapping)
            else self._nia_customize_confirm_header(capture)
        )
        session.customize_confirm_wait_reads += int(session.customize_confirm_submitted)
        should_submit = not session.customize_confirm_submitted
        should_retry = (
            session.customize_confirm_submitted
            and session.customize_confirm_attempts < 3
            and session.customize_confirm_wait_reads >= 4
        )
        if should_submit or should_retry:
            session.customize_confirm_submitted = True
            session.customize_confirm_attempts += 1
            session.customize_confirm_wait_reads = 0
            return {
                "capture": dict(capture),
                "kind": "card-customize-confirm-execute",
                "target": "execute",
                "action": self._nia_card_operation_action(
                    capture,
                    label=(
                        "nia-subpage:card-customize-confirm:execute"
                        if session.customize_confirm_attempts == 1
                        else "nia-subpage:card-customize-confirm:execute-retry-"
                        f"{session.customize_confirm_attempts}"
                    ),
                    box=(368, 1110, 632, 1205),
                ).to_dict(),
                "evidence": {
                    "policy": "optional-confirm-right-execute-bounded",
                    "header": dict(header),
                    "confirm_attempt": session.customize_confirm_attempts,
                    "selected_customize_id": session.customize_option_id,
                },
            }
        return {
            "capture": dict(capture),
            "kind": "wait",
            "target": "customize-confirm-settlement",
            "evidence": {
                "policy": "await-customize-confirm-page-exit",
                "confirm_attempts": session.customize_confirm_attempts,
                "confirm_wait_reads": session.customize_confirm_wait_reads,
            },
        }

    def _nia_complete_customize_round(self) -> None:
        """Retain the card owner while resetting one completed option round."""

        session = self._nia_card_operation_session
        if session is None or session.target != "customize":
            return
        option_id = session.customize_option_id
        if option_id is not None:
            session.customize_applied_ids.append(option_id)
            if session.customize_total_remaining is not None:
                session.customize_total_remaining = max(
                    0, session.customize_total_remaining - 1
                )
            selected = next(
                (
                    value
                    for value in session.customize_option_candidates
                    if value.get("customize_id") == option_id
                ),
                None,
            )
            if isinstance(selected, Mapping) and int(selected.get("remaining", 0)) <= 1:
                session.customize_exhausted_ids.add(option_id)
        session.customize_option_candidates.clear()
        session.customize_option_slot = None
        session.customize_option_id = None
        session.customize_option_select_attempts = 0
        session.customize_option_confirmed = False
        session.customize_execute_submitted = False
        session.customize_execute_attempts = 0
        session.customize_execute_wait_reads = 0
        session.customize_confirm_submitted = False
        session.customize_confirm_attempts = 0
        session.customize_confirm_wait_reads = 0

    def _nia_plan2_customize_capability(
        self,
        *,
        card_id: str,
        upgrade: int,
        customize_counts: Sequence[int],
    ) -> Mapping[str, Any] | None:
        """Materialize one proposed Plan2 card instance through the exam owner.

        Other plans deliberately return ``None``: their Special Guidance
        ranking keeps its existing behavior until they have an equivalent
        shared runtime materializer.
        """

        if self.plan_type != PLAN2:
            return None
        session = self._nia_card_operation_session
        if session is None or session.target != "customize":
            raise ValueError("Plan2 customize capability has no card owner")
        counts = tuple(customize_counts)
        if any(type(value) is not int or value < 0 for value in counts):
            raise ValueError("Plan2 customize capability has an invalid count vector")
        key = (card_id, upgrade, counts)
        cached = session.customize_plan2_capabilities.get(key)
        if cached is not None:
            return dict(cached)

        from .audition_local_save_state import (
            CanonicalJsonValue,
            LocalSaveExamCard,
            empty_local_save_exam_card_runtime_state,
        )
        from .audition_native_ordered_zones import NativeOrderedCardInstance
        from .plan2_native_horizon import (
            Plan2NativeHorizonError,
            materialize_plan2_native_card_program,
        )
        from .plan2_native_program_catalog import (
            compile_plan2_native_program_catalog,
        )

        try:
            if session.customize_plan2_catalog is None:
                session.customize_plan2_catalog = (
                    compile_plan2_native_program_catalog().catalog
                )
            runtime = replace(
                empty_local_save_exam_card_runtime_state(),
                customize_count_list=CanonicalJsonValue.from_value(
                    list(counts),
                    "special_guidance.customize_count_list",
                ),
            )
            native_card = NativeOrderedCardInstance.from_local_save(
                LocalSaveExamCard(
                    zone_order=0,
                    guid=(
                        f"special-guidance:{card_id}@{upgrade}:"
                        + ",".join(str(value) for value in counts)
                    ),
                    card_id=card_id,
                    base_upgrade=upgrade,
                    temporary_upgrade=0,
                    effective_upgrade=upgrade,
                    support_upgrade_ids=(),
                    fixed_deck_order=0,
                    runtime_state=runtime,
                )
            )
            materialize_plan2_native_card_program(
                session.customize_plan2_catalog,
                native_card,
            )
        except Plan2NativeHorizonError as error:
            result: Mapping[str, Any] = {
                "supported": False,
                "customize_count_list": list(counts),
                "blockers": [error.blocker.to_dict()],
            }
        except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError) as error:
            result = {
                "supported": False,
                "customize_count_list": list(counts),
                "blockers": [
                    {
                        "code": "plan2-special-guidance-capability-unavailable",
                        "detail": f"{type(error).__name__}:{error}",
                    }
                ],
            }
        else:
            result = {
                "supported": True,
                "customize_count_list": list(counts),
                "blockers": [],
            }
        session.customize_plan2_capabilities[key] = result
        return dict(result)

    def _nia_customize_option_candidates(
        self,
        capture: Mapping[str, Any],
        *,
        produce_points: int,
    ) -> tuple[Mapping[str, Any], ...]:
        """Bind visible option slots to the selected card's ordered Master IDs."""

        from .live_source import _live_text_recognizer
        from .master_db import DEFAULT_DATABASE
        from .plan1_runtime_customization import (
            _card_master_customization_ids,
            _customization_master_indexes,
        )
        from .plan3_engine import DEFAULT_MASTER_DIR
        import numpy as np

        if type(produce_points) is not int or produce_points < 0:
            raise ValueError("N.I.A. customize option has no Produce Point authority")
        session = self._nia_card_operation_session
        if session is None or session.target != "customize" or not session.submitted:
            raise ValueError("N.I.A. customize option has no submitted card owner")
        ranked = self._nia_session_ranked_offers(session)
        if not ranked:
            raise ValueError("N.I.A. customize option has no selected card identity")
        winner = ranked[0]
        option_ids = _card_master_customization_ids(
            winner.card_id,
            winner.upgrade,
            database=DEFAULT_DATABASE,
        )
        option_max_counts: tuple[int, ...] | None = None
        if self.plan_type == PLAN2:
            customize_index, _grow_index = _customization_master_indexes(
                DEFAULT_MASTER_DIR
            )
            maximums: list[int] = []
            for option_id in option_ids:
                counts = tuple(
                    sorted(
                        count
                        for candidate_id, count in customize_index
                        if candidate_id == option_id
                    )
                )
                if not counts or counts != tuple(range(1, counts[-1] + 1)):
                    raise ValueError(
                        "N.I.A. customize option has no contiguous Master count range: "
                        f"{option_id}"
                    )
                maximums.append(counts[-1])
            option_max_counts = tuple(maximums)
        option_boxes = (
            (38, 790, 243, 904),
            (258, 790, 463, 904),
            (478, 790, 683, 904),
        )
        remaining_boxes = (
            (70, 780, 220, 820),
            (285, 780, 445, 820),
            (500, 780, 665, 820),
        )
        cost_boxes = (
            (100, 855, 225, 905),
            (320, 855, 445, 905),
            (535, 855, 665, 905),
        )
        if len(option_ids) > len(option_boxes):
            raise ValueError("N.I.A. customize option count exceeds the official row")

        path = Path(str(capture["png_path"]))
        recognizer = _live_text_recognizer()
        total_read = recognizer.recognize_path(path, (400, 990, 590, 1040))
        total_match = re.search(r"\d+", str(total_read.text))
        if total_read.confidence < 0.55 or total_match is None:
            raise ValueError("N.I.A. customize total remaining count is not ready")
        session.customize_total_remaining = int(total_match.group())
        with Image.open(path.resolve()) as image:
            image.load()
            screen = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
        candidates: list[Mapping[str, Any]] = []
        for index, option_id in enumerate(option_ids):
            left, top, right, bottom = option_boxes[index]
            tile = screen[top:bottom, left:right]
            dark_fraction = float((tile.mean(axis=2) < 0.45).mean())
            # Native ScheduleCustomizeInfoButtonView.SetView applies this
            # disable mask only for the per-slot/per-card customize limits.
            # Insufficient Produce Points changes the cost-label style instead,
            # so it must not make an unapplied slot look exhausted here.
            disabled = (
                option_id in session.customize_exhausted_ids
                or dark_fraction >= 0.25
            )
            if disabled:
                candidates.append(
                    {
                        "slot": index + 1,
                        "customize_id": option_id,
                        "remaining": 0,
                        "produce_point_cost": None,
                        "affordable": False,
                        "policy_priority": self._nia_customize_option_priority(
                            option_id
                        ),
                        "disabled_fraction": dark_fraction,
                        "box": list(option_boxes[index]),
                    }
                )
                continue
            remaining_read = recognizer.recognize_path(path, remaining_boxes[index])
            cost_read = recognizer.recognize_path(path, cost_boxes[index])
            remaining_match = re.search(r"\d+", str(remaining_read.text))
            cost_match = re.search(r"\d+", str(cost_read.text))
            if (
                remaining_read.confidence < 0.55
                or cost_read.confidence < 0.55
                or remaining_match is None
                or cost_match is None
            ):
                raise ValueError(
                    f"N.I.A. customize option {index + 1} counters are not ready"
                )
            remaining = int(remaining_match.group())
            cost = int(cost_match.group())
            policy_priority = max(
                0,
                self._nia_customize_option_priority(option_id)
                - 2 * session.customize_applied_ids.count(option_id),
            )
            candidates.append(
                {
                    "slot": index + 1,
                    "customize_id": option_id,
                    "remaining": remaining,
                    "produce_point_cost": cost,
                    "affordable": remaining > 0 and cost <= produce_points,
                    "policy_priority": policy_priority,
                    "disabled_fraction": dark_fraction,
                    "box": list(option_boxes[index]),
                }
            )
        if option_max_counts is not None:
            current_counts: list[int] = []
            for index, candidate in enumerate(candidates):
                remaining = int(candidate["remaining"])
                maximum = option_max_counts[index]
                if remaining < 0 or remaining > maximum:
                    raise ValueError(
                        "N.I.A. customize remaining count exceeds Master: "
                        f"{candidate['customize_id']}:{remaining}>{maximum}"
                    )
                current_counts.append(maximum - remaining)
            current_vector = tuple(current_counts)
            enriched: list[Mapping[str, Any]] = []
            for index, candidate in enumerate(candidates):
                value = dict(candidate)
                value["current_customize_count_list"] = list(current_vector)
                if int(candidate["remaining"]) > 0:
                    proposed = list(current_vector)
                    proposed[index] += 1
                    capability = self._nia_plan2_customize_capability(
                        card_id=winner.card_id,
                        upgrade=winner.upgrade,
                        customize_counts=proposed,
                    )
                    assert capability is not None
                    value["proposed_customize_count_list"] = proposed
                    value["runtime_supported"] = capability["supported"]
                    value["runtime_blockers"] = list(capability["blockers"])
                else:
                    value["proposed_customize_count_list"] = None
                    value["runtime_supported"] = None
                    value["runtime_blockers"] = []
                enriched.append(value)
            candidates = enriched
        return tuple(candidates)

    def _nia_customize_option_priority(self, customize_id: str) -> int:
        """Apply one shared archetype-weight table to a Master option ID."""

        if "-g_effect-cost_" in customize_id or "-g_effect-cost_reduce-" in customize_id:
            return 1
        effect_type = self._current_exam_effect_type()
        if effect_type == "ProduceExamEffectType_ExamReview":
            if "lesson_depend_exam_review" in customize_id:
                return 4
            if "review_add" in customize_id:
                return 3
        elif effect_type == "ProduceExamEffectType_ExamCardPlayAggressive":
            if (
                "lesson_depend_exam_card_play_aggressive" in customize_id
                or "aggressive_add" in customize_id
            ):
                return 4
            if "block" in customize_id:
                return 3
        elif effect_type == "ProduceExamEffectType_ExamLessonBuff":
            if "lesson_buff" in customize_id:
                return 4
            if "parameter_buff" in customize_id:
                return 2
        elif effect_type == "ProduceExamEffectType_ExamParameterBuff":
            if "parameter_buff" in customize_id:
                return 4
            if "lesson_buff" in customize_id:
                return 2
        elif effect_type in {
            "ProduceExamEffectType_ExamConcentration",
            "ProduceExamEffectType_ExamFullPower",
        } and any(
            marker in customize_id
            for marker in (
                "full_power",
                "concentration",
                "preservation",
                "stance_change",
            )
        ):
            return 4
        return 2

    @staticmethod
    def _nia_customize_option_selected_slot(
        capture: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
    ) -> tuple[int | None, Mapping[str, Any]]:
        """Resolve the option selected by the game's orange corner frame."""

        import numpy as np

        path_value = capture.get("png_path")
        if not isinstance(path_value, str) or not path_value:
            raise ValueError("N.I.A. customize selection proof has no PNG path")
        with Image.open(Path(path_value).resolve()) as image:
            image.load()
            screen = np.asarray(image.convert("RGB"))
        counts: list[int] = []
        height, width = screen.shape[:2]
        padding = 14
        for candidate in candidates:
            raw_box = candidate.get("box")
            if not isinstance(raw_box, list) or len(raw_box) != 4:
                raise ValueError("N.I.A. customize candidate has no option box")
            left, top, right, bottom = (int(value) for value in raw_box)
            outer_left = max(0, left - padding)
            outer_top = max(0, top - padding)
            outer_right = min(width, right + padding)
            outer_bottom = min(height, bottom + padding)
            region = screen[outer_top:outer_bottom, outer_left:outer_right]
            orange = (
                (region[:, :, 0] > 235)
                & (region[:, :, 1] >= 80)
                & (region[:, :, 1] <= 204)
                & (region[:, :, 2] < 115)
            )
            inner_left = left - outer_left
            inner_top = top - outer_top
            inner_right = right - outer_left
            inner_bottom = bottom - outer_top
            orange[
                inner_top:inner_bottom,
                inner_left:inner_right,
            ] = False
            counts.append(int(orange.sum()))
        if not counts:
            return None, {"orange_ring_counts": []}
        ranked = sorted(
            enumerate(counts, start=1),
            key=lambda value: value[1],
            reverse=True,
        )
        winner_slot, winner_count = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0
        margin = winner_count - runner_up
        selected = winner_slot if winner_count >= 1000 and margin >= 500 else None
        return selected, {
            "orange_ring_counts": counts,
            "winner_count": winner_count,
            "runner_up_count": runner_up,
            "margin": margin,
            "threshold": 1000,
            "margin_threshold": 500,
        }

    def _nia_customize_option_surface(
        self,
        *,
        capture: Mapping[str, Any],
        produce_points: int,
        header: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Select one Master-bound customization option, then execute once."""

        header = (
            dict(header)
            if isinstance(header, Mapping)
            else self._nia_customize_option_header(capture)
        )
        session = self._nia_card_operation_session
        if session is None or session.target != "customize" or not session.submitted:
            raise ValueError("N.I.A. customize option has no submitted card session")
        if session.customize_total_remaining == 0:
            session.customize_exit_ready = True
            return {
                "capture": dict(capture),
                "kind": "wait",
                "target": "customize-exit-ready",
                "evidence": {
                    "policy": "release-to-existing-guide-exit",
                    "total_remaining": 0,
                },
            }
        if not session.customize_option_candidates:
            session.customize_option_candidates.extend(
                self._nia_customize_option_candidates(
                    capture,
                    produce_points=produce_points,
                )
            )
        available = tuple(
            value
            for value in session.customize_option_candidates
            if value.get("affordable") is True
            and (
                self.plan_type != PLAN2
                or value.get("runtime_supported") is True
            )
        )
        if session.customize_total_remaining == 0 or not available:
            session.customize_exit_ready = True
            return {
                "capture": dict(capture),
                "kind": "wait",
                "target": "customize-exit-ready",
                "evidence": {
                    "policy": "release-to-existing-guide-exit",
                    "total_remaining": session.customize_total_remaining,
                    "options": [dict(value) for value in session.customize_option_candidates],
                },
            }
        selected = max(
            available,
            key=lambda value: (
                int(value["policy_priority"]),
                -int(value["produce_point_cost"]),
                -int(value["slot"]),
            ),
        )

        if session.customize_option_slot is None:
            session.customize_option_slot = int(selected["slot"])
            session.customize_option_id = str(selected["customize_id"])
            session.customize_option_select_attempts = 1
            box = tuple(int(value) for value in selected["box"])
            return {
                "capture": dict(capture),
                "kind": "card-customize-option-select",
                "target": session.customize_option_id,
                "action": self._nia_card_operation_action(
                    capture,
                    label=(
                        "nia-subpage:card-customize-option:select:"
                        f"{session.customize_option_id}"
                    ),
                    box=box,
                ).to_dict(),
                "evidence": {
                    "policy": "master-effect-family-priority-then-order",
                    "header": dict(header),
                    "options": [dict(value) for value in session.customize_option_candidates],
                    "selected": dict(selected),
                },
            }

        proved_slot, selection_proof = self._nia_customize_option_selected_slot(
            capture,
            session.customize_option_candidates,
        )
        if proved_slot != session.customize_option_slot:
            if session.customize_option_select_attempts >= 3:
                return {
                    "capture": dict(capture),
                    "kind": "wait",
                    "target": "customize-option-selection",
                    "evidence": {
                        "policy": "await-master-bound-option-after-bounded-retries",
                        "requested_slot": session.customize_option_slot,
                        "proved_slot": proved_slot,
                        "selection_proof": dict(selection_proof),
                        "select_attempts": session.customize_option_select_attempts,
                    },
                }
            selected = next(
                value
                for value in session.customize_option_candidates
                if int(value["slot"]) == session.customize_option_slot
            )
            session.customize_option_select_attempts += 1
            box = tuple(int(value) for value in selected["box"])
            return {
                "capture": dict(capture),
                "kind": "card-customize-option-select",
                "target": session.customize_option_id or str(selected["customize_id"]),
                "action": self._nia_card_operation_action(
                    capture,
                    label=(
                        "nia-subpage:card-customize-option:select-retry-"
                        f"{session.customize_option_select_attempts}"
                    ),
                    box=box,
                ).to_dict(),
                "evidence": {
                    "policy": "retry-same-master-bound-option-until-selected",
                    "requested_slot": session.customize_option_slot,
                    "proved_slot": proved_slot,
                    "selection_proof": dict(selection_proof),
                    "select_attempt": session.customize_option_select_attempts,
                },
            }

        if not session.customize_execute_submitted:
            session.customize_option_confirmed = True
            session.customize_execute_submitted = True
            session.customize_execute_attempts = 1
            session.customize_execute_wait_reads = 0
            execute_box = (368, 1035, 625, 1130)
            return {
                "capture": dict(capture),
                "kind": "card-customize-option-execute",
                "target": session.customize_option_id or "customize-option",
                "action": self._nia_card_operation_action(
                    capture,
                    label="nia-subpage:card-customize-option:execute",
                    box=execute_box,
                ).to_dict(),
                "evidence": {
                    "policy": "single-execute-after-master-bound-option",
                    "header": dict(header),
                    "selected_slot": session.customize_option_slot,
                    "selected_customize_id": session.customize_option_id,
                    "selection_proof": dict(selection_proof),
                },
            }

        session.customize_execute_wait_reads += 1
        if (
            session.customize_execute_attempts < 3
            and session.customize_execute_wait_reads >= 4
            and proved_slot == session.customize_option_slot
        ):
            session.customize_execute_attempts += 1
            session.customize_execute_wait_reads = 0
            execute_box = (368, 1035, 625, 1130)
            return {
                "capture": dict(capture),
                "kind": "card-customize-option-execute",
                "target": session.customize_option_id or "customize-option",
                "action": self._nia_card_operation_action(
                    capture,
                    label=(
                        "nia-subpage:card-customize-option:execute-retry-"
                        f"{session.customize_execute_attempts}"
                    ),
                    box=execute_box,
                ).to_dict(),
                "evidence": {
                    "policy": "bounded-same-selected-option-execute-retry",
                    "execute_attempt": session.customize_execute_attempts,
                    "selection_proof": dict(selection_proof),
                    "selected_customize_id": session.customize_option_id,
                },
            }
        return {
            "capture": dict(capture),
            "kind": "wait",
            "target": "customize-option-settlement",
            "evidence": {
                "policy": "await-customize-option-page-exit",
                "selected_slot": session.customize_option_slot,
                "selected_customize_id": session.customize_option_id,
                "execute_attempts": session.customize_execute_attempts,
                "execute_wait_reads": session.customize_execute_wait_reads,
            },
        }

    def _nia_customize_option_surface_or_wait(
        self,
        *,
        capture: Mapping[str, Any],
        produce_points: int,
        header: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        try:
            return self._nia_customize_option_surface(
                capture=capture,
                produce_points=produce_points,
                header=header,
            )
        except (FileNotFoundError, KeyError, OSError, RuntimeError, ValueError) as error:
            return {
                "capture": dict(capture),
                "kind": "wait",
                "target": "customize-option-ready",
                "evidence": {
                    "policy": "retain-customize-owner-until-option-page-ready",
                    "detail": f"{type(error).__name__}:{error}",
                },
            }

    def _nia_card_operation_surface(
        self,
        *,
        capture: Mapping[str, Any],
        target: str,
    ) -> Mapping[str, Any]:
        """Preview titles once, choose by archetype, then submit once."""

        session = self._nia_card_operation_session
        if session is None or session.target != target:
            session = _NiaCardOperationSession(
                target=target,
                slots=self._nia_card_operation_slots(capture),
            )
            self._nia_card_operation_session = session

        if session.submitted:
            session.submit_wait_reads += 1
            if session.submit_attempts < 3 and session.submit_wait_reads >= 4:
                ranked = self._nia_session_ranked_offers(session)
                winner = (
                    ranked[0]
                    if target in {"strengthen", "customize", "copy"}
                    else ranked[-1]
                )
                if session.selected_slot is None:
                    raise ValueError("submitted N.I.A. card operation has no selected slot")
                selected = self._nia_card_operation_offer(
                    capture,
                    target=target,
                    slot=session.selected_slot,
                    box=session.slots[session.selected_slot - 1],
                )
                if (
                    selected.card_id != winner.card_id
                    or selected.upgrade != winner.upgrade
                ):
                    return {
                        "capture": dict(capture),
                        "kind": "wait",
                        "target": target,
                        "evidence": {
                            "policy": "submitted-card-operation-selection-mismatch",
                            "expected_card_id": winner.card_id,
                            "expected_upgrade": winner.upgrade,
                            "visible_card_id": selected.card_id,
                            "visible_upgrade": selected.upgrade,
                        },
                    }
                session.submit_attempts += 1
                session.submit_wait_reads = 0
                action = self._nia_card_operation_action(
                    capture,
                    label=(
                        f"nia-subpage:card-operation:{target}:submit-retry-"
                        f"{session.submit_attempts}"
                    ),
                    box=self._nia_card_operation_submit_box(target),
                )
                return {
                    "capture": dict(capture),
                    "kind": "card-operation-submit",
                    "target": target,
                    "action": action.to_dict(),
                    "evidence": {
                        "card_id": winner.card_id,
                        "upgrade": winner.upgrade,
                        "submit_attempt": session.submit_attempts,
                        "policy": "bounded-same-page-same-selection-submit-retry",
                    },
                }
            return {
                "capture": dict(capture),
                "kind": "wait",
                "target": target,
                "evidence": {
                    "policy": "await-card-operation-localsave",
                    "submit_attempts": session.submit_attempts,
                    "submit_wait_reads": session.submit_wait_reads,
                },
            }

        if session.pending_slot is not None:
            index = session.pending_slot - 1
            session.offers.append(
                self._nia_card_operation_offer(
                    capture,
                    target=target,
                    slot=session.pending_slot,
                    box=session.slots[index],
                )
            )
            session.selected_slot = session.pending_slot
            session.pending_slot = None

        if session.next_slot <= len(session.slots):
            slot = session.next_slot
            session.next_slot += 1
            session.pending_slot = slot
            box = session.slots[slot - 1]
            action = self._nia_card_operation_action(
                capture,
            label=f"nia-subpage:card-operation:{target}:preview-{slot}",
                box=box,
            )
            return {
                "capture": dict(capture),
                "kind": "card-operation-preview",
                "target": target,
                "action": action.to_dict(),
                "evidence": {"slot": slot, "policy": "one-full-title-ocr-per-card"},
            }

        ranked = self._nia_session_ranked_offers(session)
        winner = (
            ranked[0]
            if target in {"strengthen", "customize", "copy"}
            else ranked[-1]
        )
        if session.selected_slot != winner.slot:
            session.selected_slot = winner.slot
            box = session.slots[winner.slot - 1]
            action = self._nia_card_operation_action(
                capture,
                label=(
                    f"nia-subpage:card-operation:{target}:select:"
                    f"{winner.card_id}@{winner.upgrade}"
                ),
                box=box,
            )
            return {
                "capture": dict(capture),
                "kind": "card-operation-select",
                "target": target,
                "action": action.to_dict(),
                "evidence": {
                    "card_id": winner.card_id,
                    "upgrade": winner.upgrade,
                    "title": winner.display_name,
                    "policy": "ocr-title-master-archetype-ranking",
                },
            }

        submit_box = self._nia_card_operation_submit_box(target)
        action = self._nia_card_operation_action(
            capture,
            label=f"nia-subpage:card-operation:{target}:submit",
            box=submit_box,
        )
        session.submitted = True
        session.submit_attempts = 1
        session.submit_wait_reads = 0
        return {
            "capture": dict(capture),
            "kind": "card-operation-submit",
            "target": target,
            "action": action.to_dict(),
            "evidence": {
                "cards": [offer.to_dict() for offer in session.offers],
                "selected_card_id": winner.card_id,
                "selected_upgrade": winner.upgrade,
                "policy": "single-submit-after-ocr-ranking",
            },
        }

    def _nia_card_operation_surface_or_wait(
        self,
        *,
        capture: Mapping[str, Any],
        target: str,
    ) -> Mapping[str, Any]:
        """Keep the established card-operation owner while its UI settles.

        Card-grid detection and selected-title OCR are read-only observations.
        During entrance/preview animation either can be temporarily unresolved;
        that is not permission for the outer router to drop this transaction and
        fall through to Maa's generic Click_1 continuation.
        """

        try:
            return self._nia_card_operation_surface(
                capture=capture,
                target=target,
            )
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
            session = self._nia_card_operation_session
            return {
                "capture": dict(capture),
                "kind": "wait",
                "target": target,
                "evidence": {
                    "policy": "retain-card-operation-owner-until-ui-ready",
                    "detail": f"{type(error).__name__}:{error}",
                    "pending_slot": (
                        None if session is None else session.pending_slot
                    ),
                    "submitted": bool(session is not None and session.submitted),
                },
            }

    @staticmethod
    def _nia_card_reward_layout(capture: Mapping[str, Any]) -> bool:
        """Detect three reward cards plus the localized receive button."""

        from .card_detector import CardDetector
        from .live_source import _live_text_recognizer

        path = capture.get("png_path")
        if not isinstance(path, str) or not path:
            return False
        detections = tuple(
            item
            for item in CardDetector().detect_path(Path(path)).detections
            if item.label == "cards"
            and 130 <= item.x <= 500
            and 780 <= item.y <= 900
            and 100 <= item.width <= 145
            and 100 <= item.height <= 145
        )
        if len(detections) != 3:
            return False
        with Image.open(Path(path)) as image:
            rendered = image.convert("RGB")
            prompt = _live_text_recognizer().recognize(
                rendered.crop((80, 1000, 650, 1130))
            )
            choice_prompt = _live_text_recognizer().recognize(
                rendered.crop((180, 600, 540, 655))
            )
        normalized = str(prompt.text).replace(" ", "")
        choice_text = str(choice_prompt.text).replace(" ", "")
        explicit_card_choice = bool(
            choice_prompt.confidence >= 0.75
            and (
                (
                    "技能卡" in choice_text
                    and ("選擇" in choice_text or "选择" in choice_text)
                    and ("領取" in choice_text or "领取" in choice_text)
                )
                or (
                    "スキルカード" in choice_text
                    and "選" in choice_text
                    and "受け取" in choice_text
                )
            )
        )
        return bool(
            explicit_card_choice
            or
            # The orange receive button reduces Paddle's confidence on the
            # first glyph, but the recognized word plus three card controls is
            # unambiguous.  Do not add artwork or plus-colour gates here.
            prompt.confidence >= 0.50
            and (
                "領取" in normalized
                or "领取" in normalized
            )
        )

    @staticmethod
    def _nia_received_card_detail_layout(capture: Mapping[str, Any]) -> bool:
        """Recognize the single enlarged card shown after Receive.

        This is layout-only: it does not identify artwork, title, rarity,
        upgrade markers, or colours.  The preceding receive session and
        Produce LocalSave own the reward identity.
        """

        from .live_source import _card_detector

        path = capture.get("png_path")
        if not isinstance(path, str) or not path:
            return False
        detected = tuple(
            item
            for item in _card_detector().detect_path(Path(path)).detections
            if item.label == "cards"
        )
        choice_tiles = tuple(
            item
            for item in detected
            if 130 <= item.x <= 500
            and 780 <= item.y <= 900
            and 100 <= item.width <= 145
            and 100 <= item.height <= 145
        )
        cards = tuple(
            item
            for item in detected
            if 180 <= item.x <= 520
            and 430 <= item.y <= 900
            and item.width >= 180
            and item.height >= 230
            and item.y + item.height <= 950
        )
        return len(choice_tiles) != 3 and len(cards) == 1

    @staticmethod
    def _nia_reward_slots(capture: Mapping[str, Any]) -> tuple[tuple[int, int, int, int], ...]:
        from .live_source import _card_detector

        path_value = capture.get("png_path")
        if not isinstance(path_value, str) or not path_value:
            raise ValueError("N.I.A. reward capture has no PNG path")
        rows = tuple(
            sorted(
                (
                    item
                    for item in _card_detector().detect_path(Path(path_value)).detections
                    if item.label == "cards"
                    and 130 <= item.x <= 500
                    and 780 <= item.y <= 900
                ),
                key=lambda item: item.x,
            )
        )
        if len(rows) != 3:
            raise ValueError(f"N.I.A. reward layout expected 3 cards, observed {len(rows)}")
        return tuple((item.x, item.y, item.x + item.width, item.y + item.height) for item in rows)

    @classmethod
    def _nia_item_reward_layout(cls, capture: Mapping[str, Any]) -> bool:
        """Recognize a three-P-item page by OCR text plus visible tile geometry."""

        from .live_source import _live_text_recognizer

        path_value = capture.get("png_path")
        if not isinstance(path_value, str) or not path_value:
            return False
        path = Path(path_value)
        with Image.open(path) as source:
            image = source.convert("RGB")
            recognizer = _live_text_recognizer()
            header_red, header_green, header_blue = image.getpixel((50, 110))
            if (
                header_red >= 245
                and 120 <= header_green <= 225
                and header_blue <= 130
            ):
                overflow_title = recognizer.recognize(
                    image.crop((45, 85, 680, 145))
                )
                overflow_text = re.sub(r"\s+", "", str(overflow_title.text))
                if (
                    overflow_title.confidence >= 0.75
                    and "P" in overflow_text.upper()
                    and (
                        "持有上限" in overflow_text
                        or "所持上限" in overflow_text
                        or "所持数上限" in overflow_text
                    )
                ):
                    # The N-row capacity transaction is reconstructed from its
                    # current frame and LocalSave.  It must never become a
                    # three-tile reward-preview transaction, even after restart.
                    return False
            prompt = recognizer.recognize(
                image.crop((200, 605, 520, 655))
            )
            title = recognizer.recognize(
                image.crop((180, 490, 540, 540))
            )
        prompt_text = str(prompt.text).replace(" ", "")
        title_text = str(title.text).strip()
        # A selected P-item exposes its full catalog title, while its three
        # square tiles can also be labelled ``cards`` by Maa's generic model.
        # Exact item-title ownership therefore wins for an active preview;
        # otherwise the genuine three-card reward layout remains stronger and
        # prevents a stale item session from capturing the following page.
        if (
            _match_nia_produce_item(title_text) is not None
            or _normalize_nia_item_text(title_text).startswith("nia")
        ):
            return True
        # An explicit P-drink prompt belongs to the dedicated drink reward /
        # full-inventory reject reader.  Drink tiles do not expose the P-item
        # detail title consumed by this reversible preview workflow, so
        # claiming them here can only loop on an empty title.
        if any(value in prompt_text for value in ("飲料", "饮料", "ドリンク")):
            return False
        if "P" in prompt_text.upper() and (
            "道具" in prompt_text
            or "アイテム" in prompt_text
        ):
            return True
        # Localified OCR can read the P-drink glyph/icon as "技能卡".  Card
        # rewards already have the stronger three-card detector; without those
        # cards, route one stable receive-selection sentence into the same
        # reversible three-slot item workflow.  Every slot is still previewed
        # by full title before the single commit.
        compact_prompt = _normalize_nia_item_text(prompt_text)
        semantic_item_prompt = bool(
            prompt.confidence >= 0.75
            and (
                ("選擇" in prompt_text and "領取" in prompt_text)
                or ("选择" in prompt_text and "领取" in prompt_text)
                or ("選ん" in prompt_text and "受け取" in prompt_text)
                or (
                    "select" in compact_prompt
                    and "receive" in compact_prompt
                )
            )
        )
        # Card and drink tiles share the same generic detector geometry.  Only
        # let an ambiguous localized selection sentence claim the frame after
        # the stronger three-card layout has been excluded.  Exact item titles
        # and explicit P-item/P-drink wording above still keep priority.
        if cls._nia_card_reward_layout(capture):
            return False
        return semantic_item_prompt

    @staticmethod
    def _nia_item_reward_slots() -> tuple[tuple[int, int, int, int], ...]:
        """Return the three equal UI hit areas, never their item identities.

        The page OCR establishes that this is the P-item chooser.  These boxes
        only locate its three visible controls; each control is then selected
        reversibly and identified from the full OCR title.  Consequently a new
        item or a different server order cannot inherit meaning from its slot.
        """

        return (
            (158, 820, 286, 950),
            (296, 820, 424, 950),
            (434, 820, 564, 950),
        )

    def _nia_item_reward_offer(
        self,
        capture: Mapping[str, Any],
        *,
        slot: int,
        box: tuple[int, int, int, int],
    ) -> Mapping[str, Any]:
        """Read one reversibly selected P-item title and its visible effect."""

        from .live_source import _live_text_recognizer

        path_value = capture.get("png_path")
        if not isinstance(path_value, str) or not path_value:
            raise ValueError("N.I.A. item reward capture has no PNG path")
        path = Path(path_value)
        recognizer = _live_text_recognizer()
        title_read = recognizer.recognize_path(path, (180, 490, 540, 540))
        title = str(title_read.text).strip()
        if not title:
            raise _NiaRewardPreviewNotReady(title)
        visible_lines = tuple(
            str(recognizer.recognize_path(path, crop).text).strip()
            for crop in (
                (130, 550, 640, 590),
                (130, 585, 640, 625),
                (130, 620, 640, 660),
            )
        )
        visible_effect = " ".join(value for value in visible_lines if value)
        identity = _match_nia_produce_item(title)
        from .master_db import get_idol_profile

        profile = get_idol_profile(self.idol_card_id)
        score, reason = _rank_nia_item_offer(
            identity,
            visible_effect,
            exam_effect_type=("" if profile is None else profile.exam_effect_type),
            owned_drink_ids=(
                ()
                if self._nia_item_reward_session is None
                else self._nia_item_reward_session.inventory_before
            ),
        )
        return {
            "slot": slot,
            "box": list(box),
            "title": title,
            "visible_effect": visible_effect,
            "item_id": None if identity is None else identity.item_id,
            "official_name": None if identity is None else identity.official_name,
            "display_name": title if identity is None else identity.display_name,
            "effect_types": [] if identity is None else list(identity.effect_types),
            "resource_types": [] if identity is None else list(identity.resource_types),
            "rarity": None if identity is None else identity.rarity,
            "rank_score": score,
            "rank_reason": reason,
        }

    def _nia_item_reward_surface(
        self,
        *,
        capture: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Preview every P-item by OCR, choose once, then receive once."""

        session = self._nia_item_reward_session
        if session is None:
            slots = self._nia_item_reward_slots()
            session = _NiaItemRewardSession(
                slots=slots,
                unresolved_slots=list(range(1, len(slots) + 1)),
                inventory_before=self._current_plan2_drink_ids(),
            )
            self._nia_item_reward_session = session

        if session.submitted:
            session.receive_wait_reads += 1
            if (
                session.winner_offer is not None
                and session.receive_attempts < 3
                and session.receive_wait_reads >= 4
            ):
                winner = session.winner_offer
                target = str(winner.get("item_id") or winner.get("title") or "item")
                session.receive_attempts += 1
                session.receive_wait_reads = 0
                action = self._nia_card_operation_action(
                    capture,
                    label=f"nia-subpage:item-reward-receive-retry:{target}",
                    box=(228, 1048, 492, 1138),
                )
                return {
                    "capture": dict(capture),
                    "kind": "item-reward-confirm",
                    "target": target,
                    "action": action.to_dict(),
                    "evidence": {
                        "policy": "bounded-maa-receive-retry",
                        "receive_attempt": session.receive_attempts,
                    },
                }
            return {
                "capture": dict(capture),
                "kind": "wait",
                "target": "item-reward-receive",
                "evidence": {
                    "policy": "await-item-reward-localsave",
                    "receive_attempts": session.receive_attempts,
                    "unchanged_reads": session.receive_wait_reads,
                },
            }

        if session.pending_slot is not None:
            slot = session.pending_slot
            try:
                offer = self._nia_item_reward_offer(
                    capture,
                    slot=slot,
                    box=session.slots[slot - 1],
                )
            except _NiaRewardPreviewNotReady as not_ready:
                # The fan-present modal can become visually stable before Unity
                # accepts its first background click.  Keep this reversible
                # preview transaction as the page owner and retry only the same
                # uncommitted slot.  Never leak the frame to generic Click_1.
                attempts = session.preview_attempts.get(slot, 1)
                if attempts < 3:
                    session.preview_attempts[slot] = attempts + 1
                    action = self._nia_card_operation_action(
                        capture,
                        label=f"nia-subpage:item-reward-preview:{slot}",
                        box=session.slots[slot - 1],
                    )
                    return {
                        "capture": dict(capture),
                        "kind": "item-reward-preview",
                        "target": f"slot-{slot}",
                        "action": action.to_dict(),
                        "evidence": {
                            "policy": "retry-same-item-reward-preview",
                            "slot": slot,
                            "attempt": attempts + 1,
                            "observed_title": not_ready.title,
                        },
                    }
                return {
                    "capture": dict(capture),
                    "kind": "wait",
                    "target": f"item-reward-title-slot-{slot}",
                    "evidence": {
                        "policy": "item-reward-preview-clicks-exhausted-await-title",
                        "slot": slot,
                        "attempts": attempts,
                        "observed_title": not_ready.title,
                    },
                }
            session.offers.append(offer)
            session.selected_slot = slot
            session.pending_slot = None
            session.unresolved_slots.remove(slot)

        if session.unresolved_slots:
            slot = session.unresolved_slots[0]
            session.pending_slot = slot
            session.preview_attempts[slot] = 1
            action = self._nia_card_operation_action(
                capture,
                label=f"nia-subpage:item-reward-preview:{slot}",
                box=session.slots[slot - 1],
            )
            return {
                "capture": dict(capture),
                "kind": "item-reward-preview",
                "target": f"slot-{slot}",
                "action": action.to_dict(),
                "evidence": {
                    "slot": slot,
                    "policy": "one-full-title-ocr-per-item",
                },
            }

        ranked = sorted(
            session.offers,
            key=lambda value: (
                -int(value["rank_score"]),
                str(value.get("item_id") or value["title"]),
                int(value["slot"]),
            ),
        )
        winner = ranked[0]
        session.winner_offer = dict(winner)
        winner_slot = int(winner["slot"])
        if session.selected_slot != winner_slot:
            session.selected_slot = winner_slot
            action = self._nia_card_operation_action(
                capture,
                label=(
                    "nia-subpage:item-reward-select:"
                    + str(winner.get("item_id") or winner["title"])
                ),
                box=session.slots[winner_slot - 1],
            )
            return {
                "capture": dict(capture),
                "kind": "item-reward-select",
                "target": str(winner.get("item_id") or winner["title"]),
                "action": action.to_dict(),
                "evidence": {
                    "policy": "ocr-title-master-effect-ranking",
                    "offers": [dict(value) for value in ranked],
                },
            }

        action = self._nia_card_operation_action(
            capture,
            label=(
                "nia-subpage:item-reward-receive:"
                + str(winner.get("item_id") or winner["title"])
            ),
            box=(228, 1048, 492, 1138),
        )
        session.submitted = True
        session.receive_attempts = 1
        session.receive_wait_reads = 0
        return {
            "capture": dict(capture),
            "kind": "item-reward-confirm",
            "target": str(winner.get("item_id") or winner["title"]),
            "action": action.to_dict(),
            "evidence": {
                "policy": "single-receive-after-item-title-ranking",
                "offers": [dict(value) for value in ranked],
            },
        }

    def _nia_reward_title_offer(
        self,
        capture: Mapping[str, Any],
        *,
        slot: int,
        box: tuple[int, int, int, int],
    ) -> Any:
        """Resolve a selected N.I.A. reward from full title OCR only."""

        import re

        from .live_source import _live_text_recognizer, _shop_card_catalog
        from .logic_engine import load_master_card
        from .reward_state import RewardCardOffer, describe_master_card

        path_value = capture.get("png_path")
        if not isinstance(path_value, str) or not path_value:
            raise ValueError("N.I.A. reward preview capture has no PNG path")
        read = _live_text_recognizer().recognize_path(
            Path(path_value),
            (180, 470, 540, 560),
        )
        raw_title = str(read.text).strip()
        title = re.sub(r"\d+$", "", raw_title).strip()
        allowed_plans = {"ProducePlanType_Common", self.plan_type}
        matches = tuple(
            item
            for item in _shop_card_catalog().match(title, limit=50)
            if item.plan_type in allowed_plans
        )
        if not matches or matches[0].similarity < 0.68:
            if _match_nia_produce_item(raw_title) is not None:
                raise _InitialChoiceOwnerMismatch("item-reward", raw_title)
            # Selecting a reward is reversible and does not write Produce
            # LocalSave.  Maa can occasionally have one background click
            # swallowed, while animation frames can expose the title a little
            # later.  Keep the same transaction/slot alive instead of turning
            # that transient frame into a whole-run failure.
            raise _NiaRewardPreviewNotReady(raw_title)
        identity = matches[0]
        master = load_master_card(identity.card_id, identity.upgrade)
        return RewardCardOffer(
            slot=slot,
            box=(box[0], box[1], box[2] - box[0], box[3] - box[1]),
            asset_name=None,
            card_id=identity.card_id,
            upgrade=identity.upgrade,
            display_name=identity.display_name,
            rarity=master.rarity,
            evaluation=master.evaluation,
            stamina_cost=master.stamina_cost,
            force_stamina_cost=master.force_stamina_cost,
            effect_summary=describe_master_card(master),
            art_score=0.0,
            art_margin=0.0,
            game_recommended=False,
            selected=True,
        )

    @staticmethod
    def _nia_selected_reward_slots(
        capture: Mapping[str, Any],
        slots: tuple[tuple[int, int, int, int], ...],
    ) -> tuple[int, ...]:
        """Return only slots with the existing strict selected-preview proof."""

        from .reward_state import reward_card_is_selected

        path_value = capture.get("png_path")
        if not isinstance(path_value, str) or not path_value:
            raise ValueError("N.I.A. reward capture has no PNG path")
        with Image.open(Path(path_value)) as source:
            image = source.convert("RGB")
            return tuple(
                index
                for index, box in enumerate(slots, 1)
                if reward_card_is_selected(
                    image,
                    (box[0], box[1], box[2] - box[0], box[3] - box[1]),
                )
            )

    def _submitted_item_reward_card_handoff(
        self,
        *,
        capture: Mapping[str, Any],
        allow_probe: bool,
        snapshot: Any | None = None,
        reroll_action: SuggestedClick | None = None,
    ) -> Mapping[str, Any] | None:
        """Prove a following card reward before releasing submitted item ownership.

        Receiving an item does not always advance Produce LocalSave.  A second
        reward row can therefore appear under the same authority key while the
        submitted item session is still alive.  An unselected three-card row is
        not sufficient by itself because item tiles share the same detector.
        Permit one reversible slot preview only when the caller has independently
        recognized a reward-choice surface, then transfer ownership only after
        the selected slot exposes an exact Master card title.  Item-title and
        unresolved frames leave the original item transaction untouched.
        """

        item_session = self._nia_item_reward_session
        if item_session is None or not item_session.submitted:
            return None
        probe_slot = getattr(item_session, "card_handoff_probe_slot", None)
        if probe_slot is None and not allow_probe:
            return None

        def wait(reason: str, **evidence: Any) -> Mapping[str, Any]:
            return {
                "capture": dict(capture),
                "kind": "wait",
                "target": "item-reward-card-owner-proof",
                "evidence": {
                    "policy": "retain-submitted-item-owner-until-card-title-proof",
                    "reason": reason,
                    **evidence,
                },
            }

        try:
            slots = self._nia_reward_slots(capture)
        except (FileNotFoundError, OSError, TypeError, ValueError):
            if probe_slot is None:
                return None
            return wait("three-card-preview-layout-not-stable", probe_slot=probe_slot)

        try:
            selected_slots = self._nia_selected_reward_slots(capture, slots)
        except (FileNotFoundError, OSError, TypeError, ValueError):
            if probe_slot is None:
                return None
            return wait("selected-card-preview-not-stable", probe_slot=probe_slot)

        if probe_slot is None and not selected_slots:
            probe_slot = 1
            item_session.card_handoff_probe_slot = probe_slot
            item_session.card_handoff_probe_attempts = 1
            item_session.card_handoff_probe_wait_reads = 0
            action = self._nia_card_operation_action(
                capture,
                label=f"nia-subpage:reward-owner-preview:{probe_slot}",
                box=slots[probe_slot - 1],
            )
            return {
                "capture": dict(capture),
                "kind": "reward-owner-preview",
                "target": f"slot-{probe_slot}",
                "action": action.to_dict(),
                "evidence": {
                    "policy": "one-reversible-preview-before-item-to-card-handoff",
                    "from_owner": "item-reward",
                    "probe_slot": probe_slot,
                },
            }

        if probe_slot is None:
            if len(selected_slots) != 1:
                return None
            probe_slot = selected_slots[0]
        elif not selected_slots:
            item_session.card_handoff_probe_wait_reads += 1
            if (
                item_session.card_handoff_probe_attempts < 2
                and item_session.card_handoff_probe_wait_reads >= 4
            ):
                item_session.card_handoff_probe_attempts = 2
                item_session.card_handoff_probe_wait_reads = 0
                action = self._nia_card_operation_action(
                    capture,
                    label=f"nia-subpage:reward-owner-preview:{probe_slot}:retry-2",
                    box=slots[probe_slot - 1],
                )
                return {
                    "capture": dict(capture),
                    "kind": "reward-owner-preview",
                    "target": f"slot-{probe_slot}",
                    "action": action.to_dict(),
                    "evidence": {
                        "policy": "bounded-retry-item-to-card-owner-preview",
                        "from_owner": "item-reward",
                        "probe_slot": probe_slot,
                        "attempt": 2,
                    },
                }
            return wait(
                "owner-probe-click-awaiting-selected-slot",
                probe_slot=probe_slot,
                confirm_attempts=item_session.card_handoff_probe_attempts,
                unchanged_reads=item_session.card_handoff_probe_wait_reads,
            )
        elif selected_slots != (probe_slot,):
            return wait(
                "selected-slot-does-not-match-owner-probe",
                probe_slot=probe_slot,
                selected_slots=list(selected_slots),
            )

        try:
            offer = self._nia_reward_title_offer(
                capture,
                slot=probe_slot,
                box=slots[probe_slot - 1],
            )
        except _InitialChoiceOwnerMismatch as mismatch:
            if mismatch.owner != "item-reward":
                raise
            item_session.card_handoff_probe_slot = None
            item_session.card_handoff_probe_attempts = 0
            item_session.card_handoff_probe_wait_reads = 0
            return None
        except _NiaRewardPreviewNotReady:
            item_session.card_handoff_probe_slot = probe_slot
            return wait("selected-card-title-not-ready", probe_slot=probe_slot)

        card_session = _NiaRewardOcrSession(
            slots=slots,
            unresolved_slots=[
                slot for slot in range(1, len(slots) + 1) if slot != probe_slot
            ],
            offers=[offer],
            selected_slot=probe_slot,
        )
        transaction = self._initial_choice_transaction
        if transaction is not None and transaction.owner == "item-reward":
            self._initial_choice_transaction = _InitialChoiceTransaction(
                "card-reward",
                transaction.authority_key,
            )
        self._nia_item_reward_session = None
        self._nia_item_reward_authority_key = None
        self._nia_reward_reroll_pending = None
        self._nia_reward_ocr_session = card_session
        if transaction is not None:
            self._nia_reward_ocr_authority_key = transaction.authority_key
        elif snapshot is not None:
            self._nia_reward_ocr_authority_key = self._choice_authority_key(snapshot)
        self._nia_drink_reject_selected = False
        self._nia_drink_reject_select_attempts = 0
        self._nia_drink_reject_select_wait_reads = 0
        self._nia_drink_reject_submitted = False
        self._nia_drink_reject_confirm_attempts = 0
        self._nia_drink_reject_wait_reads = 0

        projected = dict(
            self._nia_reward_ocr_surface(
                capture=capture,
                snapshot=snapshot,
                reroll_action=reroll_action,
            )
        )
        raw_evidence = projected.get("evidence")
        evidence = dict(raw_evidence) if isinstance(raw_evidence, Mapping) else {}
        evidence["owner_handoff"] = {
            "from": "item-reward",
            "to": "card-reward",
            "proof": "exact-selected-slot-master-card-title",
            "probe_slot": probe_slot,
            "card_id": str(offer.card_id),
            "upgrade": int(offer.upgrade),
        }
        projected["evidence"] = evidence
        return projected

    def _nia_reward_ocr_surface(
        self,
        *,
        capture: Mapping[str, Any],
        snapshot: Any | None = None,
        reroll_action: SuggestedClick | None = None,
    ) -> Mapping[str, Any]:
        from PIL import Image

        from .reward_state import (
            RewardCardRerollBlocker,
            plan_nia_reward_reroll,
            reward_card_is_selected,
        )

        current_reroll_authority = self._nia_reward_reroll_authority(snapshot)
        rerolled_slots: tuple[tuple[int, int, int, int], ...] | None = None
        pending_reroll = self._nia_reward_reroll_pending
        if pending_reroll is not None:
            if current_reroll_authority is None:
                blocker = RewardCardRerollBlocker(
                    "reward-reroll-reconciliation-authority-missing",
                    "UserProduceProgress.ProduceCardRemainSelectRerollCount",
                    "submitted reroll cannot be reconciled without its counter",
                )
                return {
                    "capture": dict(capture),
                    "kind": "reward-reroll-blocked",
                    "target": "reward-reroll",
                    "blocker": blocker.to_dict(),
                    "evidence": {
                        "policy": "await-reroll-counter-decrement",
                        "remaining_count_before": (
                            pending_reroll.remaining_count_before
                        ),
                    },
                }
            expected = pending_reroll.remaining_count_before - 1
            observed = current_reroll_authority.remaining_count
            if observed == pending_reroll.remaining_count_before:
                return {
                    "capture": dict(capture),
                    "kind": "wait",
                    "target": "reward-reroll-response",
                    "evidence": {
                        "policy": "await-reroll-counter-decrement",
                        "remaining_count_before": (
                            pending_reroll.remaining_count_before
                        ),
                        "remaining_count_observed": observed,
                    },
                }
            if observed != expected:
                blocker = RewardCardRerollBlocker(
                    "reward-reroll-counter-transition-invalid",
                    "UserProduceProgress.ProduceCardRemainSelectRerollCount",
                    f"expected={expected}:observed={observed}",
                )
                return {
                    "capture": dict(capture),
                    "kind": "reward-reroll-blocked",
                    "target": "reward-reroll",
                    "blocker": blocker.to_dict(),
                    "evidence": {
                        "policy": "exact-single-reroll-counter-decrement",
                    },
                }

            # The server accepted one reroll.  Wait for its unselected row,
            # then locate all three slots again.  No old identity or geometry
            # survives in ``_NiaRewardRerollPending``.
            fresh_slots = self._nia_reward_slots(capture)
            path_value = capture.get("png_path")
            if not isinstance(path_value, str) or not path_value:
                raise ValueError("N.I.A. reward capture has no PNG path")
            with Image.open(Path(path_value)) as source:
                fresh_image = source.convert("RGB")
                selected_after_reroll = tuple(
                    index
                    for index, box in enumerate(fresh_slots, 1)
                    if reward_card_is_selected(
                        fresh_image,
                        (box[0], box[1], box[2] - box[0], box[3] - box[1]),
                    )
                )
            if selected_after_reroll:
                return {
                    "capture": dict(capture),
                    "kind": "wait",
                    "target": "reward-reroll-fresh-row",
                    "evidence": {
                        "policy": "await-unselected-rerolled-row",
                        "remaining_count": observed,
                    },
                }
            self._nia_reward_reroll_pending = None
            self._nia_reward_ocr_session = None
            rerolled_slots = fresh_slots

        session = self._nia_reward_ocr_session
        if session is not None:
            if current_reroll_authority is not None:
                session.reroll_authority = current_reroll_authority
            if reroll_action is not None:
                session.reroll_action = reroll_action
        if session is not None and session.submitted:
            # One Business result can provide more than one consecutive reward
            # set.  A fresh three-card page has no selected tile; that alone is
            # enough to start the next title-OCR set.  Do not compare artwork
            # or assume the card order changed.
            path_value = capture.get("png_path")
            if isinstance(path_value, str) and path_value:
                fresh_slots = self._nia_reward_slots(capture)
                with Image.open(Path(path_value)) as source:
                    fresh_image = source.convert("RGB")
                    selected = tuple(
                        index
                        for index, box in enumerate(fresh_slots, 1)
                        if reward_card_is_selected(
                            fresh_image,
                            (box[0], box[1], box[2] - box[0], box[3] - box[1]),
                        )
                    )
                if not selected:
                    session = None
                    self._nia_reward_ocr_session = None
        if session is None:
            slots = rerolled_slots or self._nia_reward_slots(capture)
            path_value = capture.get("png_path")
            if not isinstance(path_value, str):
                raise ValueError("N.I.A. reward capture has no PNG path")
            with Image.open(Path(path_value)) as source:
                image = source.convert("RGB")
                selected = [
                    index
                    for index, box in enumerate(slots, 1)
                    if reward_card_is_selected(
                        image,
                        (box[0], box[1], box[2] - box[0], box[3] - box[1]),
                    )
                ]
            session = _NiaRewardOcrSession(
                slots=slots,
                unresolved_slots=list(range(1, len(slots) + 1)),
                selected_slot=(selected[0] if len(selected) == 1 else None),
                reroll_authority=current_reroll_authority,
                reroll_action=reroll_action,
            )
            self._nia_reward_ocr_session = session
            if session.selected_slot is not None:
                slot = session.selected_slot
                try:
                    offer = self._nia_reward_title_offer(
                        capture,
                        slot=slot,
                        box=session.slots[slot - 1],
                    )
                except _InitialChoiceOwnerMismatch as mismatch:
                    return self._rebind_card_reward_to_item(
                        capture,
                        session=session,
                        pending_slot=slot,
                        mismatch=mismatch,
                    )
                except _NiaRewardPreviewNotReady:
                    session.pending_slot = slot
                    return {
                        "capture": dict(capture),
                        "kind": "wait",
                        "target": f"reward-title-slot-{slot}",
                        "evidence": {
                            "policy": "selected-reward-title-not-ready",
                            "slot": slot,
                        },
                    }
                session.offers.append(offer)
                session.unresolved_slots.remove(slot)

        if session.pending_slot is not None:
            slot = session.pending_slot
            try:
                offer = self._nia_reward_title_offer(
                    capture,
                    slot=slot,
                    box=session.slots[slot - 1],
                )
            except _InitialChoiceOwnerMismatch as mismatch:
                return self._rebind_card_reward_to_item(
                    capture,
                    session=session,
                    pending_slot=slot,
                    mismatch=mismatch,
                )
            except _NiaRewardPreviewNotReady as not_ready:
                attempts = session.preview_attempts.get(slot, 1)
                if attempts < 3:
                    session.preview_attempts[slot] = attempts + 1
                    action = self._nia_card_operation_action(
                        capture,
                        label=f"nia-subpage:reward-preview:{slot}",
                        box=session.slots[slot - 1],
                    )
                    return {
                        "capture": dict(capture),
                        "kind": "reward-preview",
                        "target": f"slot-{slot}",
                        "action": action.to_dict(),
                        "evidence": {
                            "policy": "retry-same-reward-preview",
                            "slot": slot,
                            "attempt": attempts + 1,
                            "observed_title": not_ready.title,
                        },
                    }
                return {
                    "capture": dict(capture),
                    "kind": "wait",
                    "target": f"reward-title-slot-{slot}",
                    "evidence": {
                        "policy": "reward-preview-clicks-exhausted-await-title",
                        "slot": slot,
                        "attempts": attempts,
                        "observed_title": not_ready.title,
                    },
                }
            session.offers.append(offer)
            session.selected_slot = slot
            session.pending_slot = None
            session.unresolved_slots.remove(slot)

        if session.unresolved_slots:
            slot = session.unresolved_slots[0]
            session.pending_slot = slot
            session.preview_attempts[slot] = 1
            action = self._nia_card_operation_action(
                capture,
                label=f"nia-subpage:reward-preview:{slot}",
                box=session.slots[slot - 1],
            )
            return {
                "capture": dict(capture),
                "kind": "reward-preview",
                "target": f"slot-{slot}",
                "action": action.to_dict(),
                "evidence": {"policy": "one-full-title-ocr-per-reward", "slot": slot},
            }

        if self.produce_id in {"produce-004", "produce-005"}:
            policy_context = self._nia_reward_policy_context()
            plan_type = str(policy_context["plan_type"])
            exam_effect_type = str(policy_context["exam_effect_type"])
            # The optional reroll evaluator currently models only Plan2's
            # Review/Aggressive deck semantics.  Plan1/Plan3 still rank all
            # three exact OCR/Master offers below, but must never enter that
            # Plan2-only evaluator merely because the UI exposes a reroll
            # button.
            if plan_type == PLAN2 and exam_effect_type in {
                "ProduceExamEffectType_ExamReview",
                "ProduceExamEffectType_ExamCardPlayAggressive",
            }:
                has_current_reroll_action = self._nia_reward_action_matches_capture(
                    session.reroll_action,
                    capture,
                )
                reroll_decision = plan_nia_reward_reroll(
                    tuple(session.offers),
                    exam_effect_type,
                    authority=session.reroll_authority,
                    has_dedicated_button_action=has_current_reroll_action,
                    deck_counts=policy_context["deck_counts"],
                    weeks_remaining=policy_context["weeks_remaining"],
                    leaderboard_prior=policy_context["leaderboard_prior"],
                )
                if reroll_decision.action == "blocked":
                    return self._nia_reward_reroll_blocked_surface(
                        capture,
                        reroll_decision,
                    )
                if reroll_decision.action == "reroll":
                    action = session.reroll_action
                    authority = reroll_decision.authority
                    if (
                        action is None
                        or authority is None
                        or not has_current_reroll_action
                    ):
                        raise ValueError("executable reroll lost its typed authority")
                    self._nia_reward_ocr_session = None
                    self._nia_reward_reroll_pending = _NiaRewardRerollPending(
                        authority.remaining_count,
                        authority.source,
                    )
                    return {
                        "capture": dict(capture),
                        "kind": "reward-reroll",
                        "target": "reward-reroll",
                        "action": action.to_dict(),
                        "evidence": {
                            "policy": "complete-three-offer-conservative-reroll",
                            "decision": reroll_decision.to_dict(),
                            "old_candidates_discarded": True,
                            "old_slot_coordinates_discarded": True,
                        },
                    }

        ranked = self._nia_session_ranked_offers(session)
        winner = ranked[0]
        if session.selected_slot != winner.slot:
            session.selected_slot = winner.slot
            action = self._nia_card_operation_action(
                capture,
                label=f"nia-subpage:reward-select:{winner.card_id}@{winner.upgrade}",
                box=session.slots[winner.slot - 1],
            )
            return {
                "capture": dict(capture),
                "kind": "reward-select",
                "target": winner.card_id,
                "action": action.to_dict(),
                "evidence": {"policy": "ocr-title-master-archetype-ranking"},
            }

        action = self._nia_card_operation_action(
            capture,
            label=f"nia-subpage:reward-receive:{winner.card_id}@{winner.upgrade}",
            box=(228, 1048, 492, 1138),
        )
        session.submitted = True
        evidence = {
            "policy": "single-receive-after-title-ranking",
            "offers": [offer.to_dict() for offer in ranked],
        }
        evidence.update(self._nia_reward_evidence_fields())
        return {
            "capture": dict(capture),
            "kind": "reward-confirm",
            "target": winner.card_id,
            "action": action.to_dict(),
            "evidence": evidence,
        }

    def _rebind_card_reward_to_item(
        self,
        capture: Mapping[str, Any],
        *,
        session: _NiaRewardOcrSession,
        pending_slot: int,
        mismatch: _InitialChoiceOwnerMismatch,
    ) -> Mapping[str, Any]:
        """Transfer the untouched first preview to the proven item workflow."""

        if mismatch.owner != "item-reward" or session.offers:
            raise mismatch
        item_session = _NiaItemRewardSession(
            slots=session.slots,
            unresolved_slots=list(session.unresolved_slots),
            pending_slot=pending_slot,
            selected_slot=session.selected_slot,
        )
        self._nia_reward_ocr_session = None
        self._nia_reward_ocr_authority_key = None
        self._nia_item_reward_session = item_session
        transaction = self._initial_choice_transaction
        if transaction is not None:
            self._initial_choice_transaction = _InitialChoiceTransaction(
                "item-reward",
                transaction.authority_key,
            )
        return self._nia_item_reward_surface(capture=capture)

    def _initial_reward_ocr_surface(
        self,
        *,
        capture: Mapping[str, Any],
        snapshot: Any,
    ) -> Mapping[str, Any]:
        """Bind one Initial reward session to the current ProduceSave.

        Preview clicks do not mutate ProduceSave, so every card title in one
        reward set keeps the same key.  A later reward set may have the same
        three-card layout and a preselected tile; the changed outer key is the
        authoritative session boundary and avoids reusing the prior offers.
        """

        from .nia_live_outer import _outer_authority

        authority = dict(_outer_authority(snapshot))
        authority_key = (
            authority.get("log_count"),
            authority.get("week"),
            authority.get("last_completed_week"),
            authority.get("stamina"),
            authority.get("max_stamina"),
            authority.get("produce_points"),
            authority.get("vocal"),
            authority.get("dance"),
            authority.get("visual"),
            authority.get("vote_count"),
        )
        if (
            self._nia_reward_ocr_authority_key is not None
            and self._nia_reward_ocr_authority_key != authority_key
        ):
            self._nia_reward_ocr_session = None
        projected = dict(self._nia_reward_ocr_surface(capture=capture))
        if (
            self._nia_reward_ocr_session is None
            and self._nia_item_reward_session is not None
        ):
            self._nia_reward_ocr_authority_key = None
            self._nia_item_reward_authority_key = authority_key
        else:
            self._nia_reward_ocr_authority_key = authority_key
        projected["outer_authority"] = authority
        return projected

    def _initial_item_reward_surface(
        self,
        *,
        capture: Mapping[str, Any],
        snapshot: Any,
    ) -> Mapping[str, Any]:
        from .nia_live_outer import _outer_authority

        # Initial/Plan1/Plan2 can reach the same post-present animation while
        # their reversible item transaction is still in memory.  Resolve the
        # shared typed notification before the speculative card-handoff probe
        # below; otherwise a stale probe slot can reinterpret the glowing
        # parcel as a selected reward tile forever.
        item_session = self._nia_item_reward_session
        if item_session is not None and item_session.submitted:
            try:
                from PIL import Image

                from .live_source import detect_card_acquire_notification

                path_value = capture.get("png_path")
                if isinstance(path_value, str) and path_value:
                    with Image.open(Path(path_value)) as image:
                        card_acquire = detect_card_acquire_notification(
                            image.convert("RGB")
                        )
                else:
                    card_acquire = None
            except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError):
                card_acquire = None
            if card_acquire is not None:
                action = self._nia_card_operation_action(
                    capture,
                    label="nia-subpage:continue:card-acquire-notification",
                    box=card_acquire.action_box,
                )
                self._clear_initial_choice_transaction()
                return {
                    "capture": dict(capture),
                    "kind": "continue",
                    "target": "card-acquire-notification",
                    "action": action.to_dict(),
                    "evidence": {
                        "policy": "typed-card-acquire-notification",
                        **card_acquire.to_dict(),
                    },
                }

        authority = dict(_outer_authority(snapshot))
        authority_key = (
            authority.get("log_count"),
            authority.get("week"),
            authority.get("last_completed_week"),
            authority.get("stamina"),
            authority.get("max_stamina"),
            authority.get("produce_points"),
            authority.get("vocal"),
            authority.get("dance"),
            authority.get("visual"),
            authority.get("vote_count"),
        )
        if (
            self._nia_item_reward_authority_key is not None
            and self._nia_item_reward_authority_key != authority_key
        ):
            self._nia_item_reward_session = None
        handoff = self._submitted_item_reward_card_handoff(
            capture=capture,
            allow_probe=True,
            snapshot=snapshot,
        )
        if handoff is not None:
            projected = dict(handoff)
            if self._nia_item_reward_session is not None:
                self._nia_item_reward_authority_key = authority_key
            else:
                self._nia_reward_ocr_authority_key = authority_key
            projected["outer_authority"] = authority
            return projected
        was_submitted = bool(
            self._nia_item_reward_session is not None
            and self._nia_item_reward_session.submitted
        )
        projected = dict(self._nia_item_reward_surface(capture=capture))
        if projected.get("kind") in {"item-reward-confirm", "wait"}:
            try:
                from .nia_live_outer import read_live_nia_outer_subpage

                delegated = dict(
                    read_live_nia_outer_subpage(
                        snapshot,
                        allow_event_choice=False,
                        produce_id=self.produce_id,
                        idol_card_id=self.idol_card_id,
                    )
                )
            except (FileNotFoundError, RuntimeError, TypeError, ValueError):
                delegated = None
            if delegated is not None:
                if delegated.get("kind") == "reward-confirm":
                    if not was_submitted:
                        projected = delegated
                elif delegated.get("kind") in {
                    "drink-reject-select",
                    "drink-reject-confirm",
                }:
                    # A selected drink reward cannot be received while the
                    # inventory already contains three drinks.  The same
                    # foreground still looks like the three-choice item
                    # layout, so leaving the submitted item session in its
                    # generic ``wait`` state deadlocks forever.  Hand this
                    # exact Maa/OCR-owned surface to the existing bounded
                    # reject transaction instead.  Drink use is scheduled on
                    # an actionable lesson/exam screen; do not invent an
                    # unsupported outer-page use or replacement gesture here.
                    routed = self._route_drink_reject_transaction(delegated)
                    if routed is not None:
                        projected = dict(routed)
                elif was_submitted and delegated.get("kind") not in {
                    "click-1-fallback",
                    "drink-reject-select",
                    "drink-reject-confirm",
                    "reward-choice",
                    "reward-confirm",
                    "wait",
                }:
                    # The submitted transaction left its page.  The already
                    # classified Maa surface is the next owner; do not capture
                    # and classify the same frame a second time.  Empty-node
                    # Click_1 and ambiguous reward rows are not page-exit proof.
                    self._clear_initial_choice_transaction()
                    projected = delegated
        self._nia_item_reward_authority_key = authority_key
        projected["outer_authority"] = authority
        return projected

    def _initial_reward_choice_surface(
        self,
        *,
        capture: Mapping[str, Any],
        snapshot: Any,
    ) -> Mapping[str, Any] | None:
        """Give a visible three-choice reward exactly one transaction owner."""

        authority_key = self._choice_authority_key(snapshot)
        transaction = self._initial_choice_transaction
        if transaction is not None and transaction.authority_key != authority_key:
            self._clear_initial_choice_transaction()
            transaction = None

        if transaction is not None:
            if transaction.owner == "item-reward":
                # The submitted drink page may replace its grid with the
                # foreground "receive none" row while ProduceSave is still
                # unchanged.  Keep the owner and let its Maa subpage route
                # decide whether this is reject/select/confirm or the next page.
                return self._initial_item_reward_surface(
                    capture=capture,
                    snapshot=snapshot,
                )
            submitted = bool(
                self._nia_reward_ocr_session is not None
                and self._nia_reward_ocr_session.submitted
            )
            if submitted and not self._nia_card_reward_layout(capture):
                self._clear_initial_choice_transaction()
                return None
            return self._initial_reward_ocr_surface(
                capture=capture,
                snapshot=snapshot,
            )

        if self._nia_item_reward_layout(capture):
            self._nia_reward_ocr_session = None
            self._nia_reward_ocr_authority_key = None
            self._initial_choice_transaction = _InitialChoiceTransaction(
                "item-reward", authority_key
            )
            return self._initial_item_reward_surface(
                capture=capture,
                snapshot=snapshot,
            )
        if self._nia_card_reward_layout(capture):
            self._nia_item_reward_session = None
            self._nia_item_reward_authority_key = None
            self._initial_choice_transaction = _InitialChoiceTransaction(
                "card-reward", authority_key
            )
            return self._initial_reward_ocr_surface(
                capture=capture,
                snapshot=snapshot,
            )
        return None

    def _exam_surface(self) -> InitialRegularSurface | None:
        from .plan3_audition_advisor_gui import select_plan3_exam_local_save_path

        selector = self.exam_save_selector or select_plan3_exam_local_save_path
        path = selector(self.game_root)
        if path is None:
            return None
        try:
            exists = path.is_file()
        except OSError:
            return InitialRegularSurface(
                PAGE_UNKNOWN,
                {
                    "reason": "exam-save-moving",
                    "exam_save_path": str(path),
                },
            )
        if not exists:
            # The selector may retain the canonical save location after the
            # game deletes ExamSave on normal terminal settlement.  A file
            # absent at the beginning of this poll no longer owns the route;
            # result/reward readers must be allowed to take over.
            return None
        try:
            marker_before = _exam_save_file_marker(path)
        except OSError:
            return InitialRegularSurface(
                PAGE_UNKNOWN,
                {
                    "reason": "exam-save-moving",
                    "exam_save_path": str(path),
                },
            )
        decode_error: Exception | None = None
        state: Any = None
        terminal = False
        try:
            if self.plan_type == PLAN3:
                from .plan3_audition_executor import is_plan3_exam_terminal
                from .plan3_local_save_bridge import decode_plan3_local_save_file

                decoded = decode_plan3_local_save_file(path)
                state = decoded.exam_state
                terminal = is_plan3_exam_terminal(state)
            elif self.plan_type in {PLAN1, PLAN2}:
                evidence = self.plan2_evidence_loader(path)
                state = evidence.state
                terminal = _plan2_state_is_terminal(state)
            else:
                return InitialRegularSurface(
                    PAGE_UNKNOWN,
                    {
                        "reason": "exam-plan-unsupported",
                        "exam_save_path": str(path),
                    },
                )
        except (OSError, TypeError, ValueError) as error:
            decode_error = error
        try:
            marker_after = _exam_save_file_marker(path)
        except OSError:
            return InitialRegularSurface(
                PAGE_UNKNOWN,
                {
                    "reason": "exam-save-moving",
                    "exam_save_path": str(path),
                },
            )
        if marker_before != marker_after:
            # The decoder observed a moving file.  Do not attach that state to
            # either timestamp.  Exam ownership remains exclusive for this
            # poll, so outer/reward readers cannot reinterpret an Exam frame.
            return InitialRegularSurface(
                PAGE_UNKNOWN,
                {
                    "reason": "exam-save-moving",
                    "exam_save_path": str(path),
                },
            )
        if decode_error is not None:
            return InitialRegularSurface(
                PAGE_UNKNOWN,
                {
                    "reason": "exam-save-decode-pending",
                    "exam_save_path": str(path),
                    "detail": f"{type(decode_error).__name__}: {decode_error}",
                },
            )
        if terminal:
            return None
        payload: dict[str, Any] = {
            "exam_save_path": str(path),
            **_exam_state_monitor_payload(
                state,
                path,
                source_marker=marker_after,
            ),
        }
        if self.plan_type in {PLAN1, PLAN2, PLAN3}:
            for field in ("character_id", "exam_type", "step_type_value"):
                value = getattr(state, field, None)
                if value is not None:
                    payload[
                        "observed_character_id" if field == "character_id" else field
                    ] = value
        return InitialRegularSurface(PAGE_EXAM, payload)

    @staticmethod
    def _final_live_surface(
        snapshot: ProduceOuterLocalSaveSnapshot,
        previous_page: str | None = None,
    ) -> InitialRegularSurface | None:
        """Read the post-Final route from LocalSave outcome and orientation.

        Final completion in Produce LocalSave is the semantic authority.  Maa's
        original ProduceShowStart path sends its safe Click_1 before it checks
        landscape orientation, so no title-card colour or text classifier is
        required here.  During a cleared Final, ``is_end_live`` can become true
        while the performance is still landscape.  Orientation therefore owns
        the presentation boundary: landscape keeps waiting for the performance,
        and only the portrait result route may enter ProduceEnd.
        """

        completed_steps = getattr(snapshot, "completed_steps", ())
        completed = completed_steps[-1] if completed_steps else None
        if completed is None or completed.step_type != 18:
            return None
        cleared = any(
            line.line_type_name == "audition_clear" for line in completed.lines
        )
        failed = any(
            line.line_type_name == "audition_failure" for line in completed.lines
        )
        if not (cleared or failed):
            return None
        from .controller_client import send_command
        try:
            capture = dict(send_command("capture_native_once", timeout=15.0))
        except (ConnectionError, RuntimeError, TimeoutError):
            return None
        path = capture.get("png_path")
        if not isinstance(path, str) or not path:
            return None
        native_size = (capture.get("width"), capture.get("height"))
        if failed:
            # After selecting End on a failed Final, Initial enters the same
            # native landscape ProduceEnd route as a cleared live.  The exact
            # completed-step failure is the state authority; Maa owns the
            # existing landscape confirmation and return-home graph.
            if native_size != (1280, 720):
                return None
            return InitialRegularSurface(
                PAGE_POST_LIVE,
                {
                    "capture": capture,
                    "authority": {
                        "kind": (
                            "outer-completed-final-failure+maa-native-landscape"
                        ),
                        "step_type": completed.step_type,
                        "log_index": completed.log_index,
                    },
                },
            )
        lifecycle = getattr(snapshot, "lifecycle", None)
        end_live = bool(
            lifecycle is not None
            and getattr(lifecycle, "is_in_progress", False)
            and getattr(lifecycle, "is_end_live", False)
        )
        if native_size == (720, 1280):
            if not end_live:
                # A cleared Final first returns to portrait result/dialogue
                # pages before the live starts.  Portrait alone is not enough
                # to skip those pages; LocalSave must also prove end-live.
                return None
            return InitialRegularSurface(
                PAGE_POST_LIVE,
                {
                    "capture": capture,
                    "authority": {
                        "kind": "produce-local-save-post-live-in-progress",
                        "is_end_live": True,
                        "is_in_progress": True,
                        "step_type": completed.step_type,
                        "log_index": completed.log_index,
                        "orientation": "portrait",
                    },
                },
            )
        if native_size != (1280, 720):
            return None
        already_started = previous_page in {
            PAGE_FINAL_LIVE_START,
            PAGE_FINAL_LIVE_PLAYING,
        } or end_live
        kind = "playing" if already_started else "start"
        page = PAGE_FINAL_LIVE_PLAYING if already_started else PAGE_FINAL_LIVE_START
        return InitialRegularSurface(
            page,
            {
                "capture": capture,
                "kind": kind,
                "authority": {
                    "kind": "outer-completed-final+maa-native-landscape",
                    "step_type": completed.step_type,
                    "log_index": completed.log_index,
                },
            },
        )

    def __call__(self, previous_page: str | None) -> InitialRegularSurface:
        try:
            snapshot = read_current_produce_outer_local_save(self.game_root)
        except FileNotFoundError:
            # The game removes ProduceProgress immediately after the final
            # "Complete" submission.  That disappearance is authoritative
            # only when this same loop was already advancing post-live pages;
            # at every earlier page a missing save remains an error.
            if previous_page == PAGE_POST_LIVE:
                return InitialRegularSurface(
                    PAGE_COMPLETED,
                    {"lifecycle": "produce-save-removed-after-post-live"},
                )
            raise
        active_outer_transaction = self._active_outer_transaction
        pending_weekly_action = (
            active_outer_transaction.owner.selected_action
            if active_outer_transaction is not None
            else None
        )
        lifecycle = snapshot.lifecycle
        if lifecycle is not None and not lifecycle.is_in_progress:
            return InitialRegularSurface(PAGE_COMPLETED, {"lifecycle": "produce-ended"})
        live = self._final_live_surface(snapshot, previous_page)
        if live is not None:
            return live
        if lifecycle is not None and lifecycle.is_end_live:
            completed_steps = getattr(snapshot, "completed_steps", ())
            completed = completed_steps[-1] if completed_steps else None
            if completed is not None and completed.step_type == 18 and any(
                line.line_type_name in {"audition_clear", "audition_failure"}
                for line in completed.lines
            ):
                # The Final-specific reader already tried the only exact
                # orientation route.  If its Maa capture was unavailable or
                # neither native landscape nor portrait, never let the broad
                # lifecycle flag fall through to ProduceEnd.
                return InitialRegularSurface(
                    PAGE_UNKNOWN,
                    {
                        "reason": (
                            "completed Final native orientation is unavailable"
                        ),
                        "authority": {
                            "kind": "outer-completed-final-orientation-unresolved",
                            "step_type": completed.step_type,
                            "log_index": completed.log_index,
                        },
                    },
                )
            from .controller_client import send_command

            capture = dict(send_command("capture_native_once", timeout=15.0))
            return InitialRegularSurface(
                PAGE_POST_LIVE,
                {
                    "capture": capture,
                    "authority": {
                        "kind": "produce-local-save-post-live-in-progress",
                        "is_end_live": True,
                        "is_in_progress": True,
                    },
                },
            )
        exam = self._exam_surface()
        if exam is not None:
            return exam

        from .live_source import read_live_overview_decision

        readers: Mapping[str, Callable[[], Mapping[str, Any]]] = {
            PAGE_OVERVIEW: lambda: read_live_overview_decision(
                idol_card_id=self.idol_card_id,
                produce_id=self.produce_id,
            ),
        }
        is_nia = self.produce_id in {"produce-004", "produce-005"}
        if not is_nia and (
            self._nia_reward_ocr_session is not None
            or self._nia_item_reward_session is not None
        ):
            # Once title preview has started, keep the whole reversible
            # three-card sequence under one owner.  Letting a generic reward
            # or card-display reader run between previews can press Receive
            # before all titles have been ranked.
            from .controller_client import send_command

            capture = dict(send_command("capture_screen_once", timeout=15.0))
            projected = self._initial_reward_choice_surface(
                capture=capture,
                snapshot=snapshot,
            )
            if projected is not None:
                return InitialRegularSurface(
                    PAGE_NIA_OUTER,
                    projected,
                )
            self._nia_reward_ocr_session = None
            self._nia_reward_ocr_authority_key = None
            self._nia_item_reward_session = None
            self._nia_item_reward_authority_key = None
        if is_nia:
            from .nia_live_outer import read_live_nia_outer_subpage

            def read_nia_surface() -> Mapping[str, Any]:
                if (
                    self._nia_item_reward_session is not None
                    and not self._nia_item_reward_session.submitted
                ):
                    from .controller_client import send_command

                    capture = dict(send_command("capture_once", timeout=15.0))
                    if self._nia_item_reward_layout(capture):
                        projected = dict(
                            self._nia_item_reward_surface(capture=capture)
                        )
                        snapshot_now = read_current_produce_outer_local_save(
                            self.game_root
                        )
                        projected["outer_authority"] = {
                            "kind": "produce-local-save-before-maa-subpage-input",
                            "log_count": snapshot_now.log_count,
                            "week": snapshot_now.latest_week_marker,
                            "last_completed_week": snapshot_now.last_completed_week,
                            "stamina": snapshot_now.stamina,
                            "max_stamina": snapshot_now.max_stamina,
                            "produce_points": snapshot_now.produce_points,
                            "vocal": snapshot_now.vocal,
                            "dance": snapshot_now.dance,
                            "visual": snapshot_now.visual,
                            "vote_count": snapshot_now.vote_count,
                        }
                        return projected
                    if self._nia_card_reward_layout(capture):
                        self._nia_item_reward_session = None
                        self._nia_item_reward_authority_key = None
                if (
                    self._nia_reward_ocr_session is not None
                    and not self._nia_reward_ocr_session.submitted
                ):
                    from .controller_client import send_command

                    capture = dict(send_command("capture_once", timeout=15.0))
                    if self._nia_card_reward_layout(capture):
                        snapshot_now = read_current_produce_outer_local_save(
                            self.game_root
                        )
                        projected = dict(
                            self._nia_reward_ocr_surface(
                                capture=capture,
                                snapshot=snapshot_now,
                            )
                        )
                        projected["outer_authority"] = {
                            "kind": "produce-local-save-before-maa-subpage-input",
                            "log_count": snapshot_now.log_count,
                            "week": snapshot_now.latest_week_marker,
                            "last_completed_week": snapshot_now.last_completed_week,
                            "stamina": snapshot_now.stamina,
                            "max_stamina": snapshot_now.max_stamina,
                            "produce_points": snapshot_now.produce_points,
                            "vocal": snapshot_now.vocal,
                            "dance": snapshot_now.dance,
                            "visual": snapshot_now.visual,
                            "vote_count": snapshot_now.vote_count,
                        }
                        return projected
                    # A reversible preview session owns only the visible
                    # three-card reward page.  Loading, overview, result, and
                    # later pages immediately discard it instead of letting a
                    # stale title read mask the current Maa/OCR surface.
                    self._nia_reward_ocr_session = None
                if (
                    self._nia_reward_ocr_session is not None
                    and self._nia_reward_ocr_session.submitted
                ):
                    # The stateless Maa subpage analyzer owns the enlarged
                    # detail page, including its current LocalSave authority.
                    self._nia_reward_ocr_session = None
                if self._nia_reward_preview_session is not None:
                    from .controller_client import send_command

                    capture = dict(send_command("capture_once", timeout=15.0))
                    return {
                        "page_override": PAGE_REWARD,
                        **self._nia_reward_surface(
                            capture=capture,
                            outer_payload={"target": "cards-get"},
                        ),
                    }
                card_operation = self._nia_card_operation_session
                if (
                    card_operation is not None
                    and not card_operation.customize_notification_pending
                    and (
                        not card_operation.submitted
                        or (
                            card_operation.target == "customize"
                            and not card_operation.customize_execute_submitted
                            and not card_operation.customize_exit_ready
                        )
                    )
                ):
                    from .controller_client import send_command

                    capture = dict(send_command("capture_once", timeout=15.0))
                    snapshot_now = read_current_produce_outer_local_save(
                        self.game_root
                    )
                    if card_operation.submitted:
                        try:
                            option_header = self._nia_customize_option_header(capture)
                        except (FileNotFoundError, OSError, RuntimeError, ValueError):
                            option_header = None
                        if option_header is None:
                            # The card-page submit may have been dropped.  Keep
                            # its existing bounded retry owner until the option
                            # page is actually visible.
                            projected = dict(
                                self._nia_card_operation_surface_or_wait(
                                    capture=capture,
                                    target=card_operation.target,
                                )
                            )
                        else:
                            projected = dict(
                                self._nia_customize_option_surface_or_wait(
                                    capture=capture,
                                    produce_points=snapshot_now.produce_points,
                                    header=option_header,
                                )
                            )
                    else:
                        projected = dict(
                            self._nia_card_operation_surface_or_wait(
                                capture=capture,
                                target=card_operation.target,
                            )
                        )
                    projected["outer_authority"] = {
                        "kind": "produce-local-save-before-maa-subpage-input",
                        "log_count": snapshot_now.log_count,
                        "week": snapshot_now.latest_week_marker,
                        "last_completed_week": snapshot_now.last_completed_week,
                        "stamina": snapshot_now.stamina,
                        "max_stamina": snapshot_now.max_stamina,
                        "produce_points": snapshot_now.produce_points,
                        "vocal": snapshot_now.vocal,
                        "dance": snapshot_now.dance,
                        "visual": snapshot_now.visual,
                        "vote_count": snapshot_now.vote_count,
                    }
                    return projected
                current_drink_count = None
                if self._nia_event_choice_pending is not None and not self._event_choice_authority_matches(
                    snapshot,
                    self._nia_event_choice_pending,
                ):
                    # A changed typed LocalSave owner starts a new event
                    # transaction.  Never carry a prior row/slot across it.
                    self._nia_event_choice_pending = None
                    self._reset_event_confirm_retry()
                synthesized_transaction_pending = False
                transaction = self._active_outer_transaction
                generic_event_actions = {
                    ACTIVITY,
                    BUSINESS,
                    CONSULTATION,
                    SPECIAL_GUIDANCE,
                    VOCAL_LESSON,
                    DANCE_LESSON,
                    VISUAL_LESSON,
                }
                transaction_week_completed = bool(
                    transaction is not None
                    and transaction.owner.produce_id == self.produce_id
                    and type(snapshot.last_completed_week) is int
                    and snapshot.last_completed_week >= transaction.owner.week
                )
                transaction_owner_week_matches = bool(
                    transaction is not None
                    and (
                        transaction.owner.week == snapshot.latest_week_marker
                        or (
                            type(snapshot.latest_week_marker) is int
                            and type(snapshot.last_completed_week) is int
                            and snapshot.last_completed_week
                            == snapshot.latest_week_marker
                            and transaction.owner.week
                            == snapshot.latest_week_marker + 1
                        )
                    )
                )
                transaction_event_candidate = bool(
                    transaction is not None
                    and not transaction_week_completed
                    and transaction.phase in {PHASE_SUBMITTED, PHASE_SETTLING}
                    and transaction.owner.produce_id == self.produce_id
                    and transaction_owner_week_matches
                    and transaction.owner.selected_action in generic_event_actions
                )
                begin_authority = self._active_outer_begin_authority
                authority_matches_begin = bool(
                    transaction_event_candidate
                    and isinstance(begin_authority, Mapping)
                    and self._event_choice_authority_matches(
                        snapshot,
                        {"authority": begin_authority},
                    )
                )
                if pending_weekly_action == OUTING and previous_page in {
                    PAGE_OVERVIEW,
                    PAGE_NIA_OUTER,
                }:
                    # The outing Master policy may use capacity only when the
                    # existing ordered inventory is proven.  An empty result
                    # stays unresolved/unknown and therefore leaves the
                    # generic outing OCR fallback in charge of that frame.
                    current_drink_count = self._current_outer_drink_count(snapshot)
                subpage_kwargs: dict[str, Any] = {
                    "allow_event_choice": previous_page
                    in {PAGE_OVERVIEW, PAGE_NIA_OUTER},
                    "pending_weekly_action": (
                        None
                        if transaction_week_completed
                        else pending_weekly_action
                    ),
                    "produce_id": self.produce_id,
                    "idol_card_id": self.idol_card_id,
                    "pending_event_choice": (
                        None
                        if transaction_event_candidate
                        else self._nia_event_choice_pending
                    ),
                }
                # Once the outer save proves the selected week complete, the
                # transaction-specific family no longer owns recognition.
                # The successor can be a mirror/audition page (including
                # Final), which only Maa's all-scope batch can see.  The run
                # loop will terminalize this same transaction after it reads
                # that authoritative week successor.
                recognition_scope = (
                    "all"
                    if transaction_week_completed
                    else self._nia_active_recognition_scope()
                )
                if recognition_scope is not None:
                    subpage_kwargs["recognition_scope"] = recognition_scope
                if pending_weekly_action == OUTING and previous_page in {
                    PAGE_OVERVIEW,
                    PAGE_NIA_OUTER,
                }:
                    subpage_kwargs["current_drink_count"] = current_drink_count
                outer = dict(
                    read_live_nia_outer_subpage(
                        snapshot,
                        **subpage_kwargs,
                    )
                )
                outer.setdefault(
                    "route",
                    {"produce_id": self.produce_id},
                )
                if transaction_event_candidate and outer.get("kind") == "event-choice":
                    if not authority_matches_begin:
                        return {
                            "capture": dict(outer.get("capture", {})),
                            "kind": "wait",
                            "target": "outer-transaction-authority-drift",
                            "evidence": {
                                "policy": "submitted-outer-authority-is-immutable",
                                "transaction_id": transaction.transaction_id,
                                "selected_action": transaction.owner.selected_action,
                            },
                            "outer_authority": dict(
                                outer.get("outer_authority", {})
                            ),
                        }
                    # First classify the fresh frame without a synthetic owner.
                    # Only a proven option page may inherit the overview
                    # transaction; dialogue/work/reward successors pass through
                    # untouched and therefore cannot be masked by a stale target.
                    first_capture = outer.get("capture")
                    self._nia_event_choice_pending = {
                        "schema": "gkms.nia-event-choice-selection-pending.v1",
                        "owner": "generic",
                        "target": transaction.owner.selected_action,
                        "authority": dict(begin_authority),
                        **(
                            {"source_capture": dict(first_capture)}
                            if isinstance(first_capture, Mapping)
                            else {}
                        ),
                    }
                    synthesized_transaction_pending = True
                    subpage_kwargs["pending_event_choice"] = (
                        self._nia_event_choice_pending
                    )
                    outer = dict(
                        read_live_nia_outer_subpage(
                            snapshot,
                            **subpage_kwargs,
                        )
                    )
                elif transaction_event_candidate:
                    pending = self._nia_event_choice_pending
                    if (
                        isinstance(pending, Mapping)
                        and pending.get("owner") == "generic"
                        and pending.get("target")
                        in {
                            transaction.owner.selected_action,
                            f"{transaction.owner.selected_action}-confirm",
                        }
                    ):
                        self._nia_event_choice_pending = None
                        self._reset_event_confirm_retry()
                if synthesized_transaction_pending:
                    pending = self._nia_event_choice_pending
                    capture = outer.get("capture")
                    if isinstance(pending, Mapping) and isinstance(capture, Mapping):
                        self._nia_event_choice_pending = {
                            **dict(pending),
                            "source_capture": dict(capture),
                        }
                prior_event_pending = self._nia_event_choice_pending
                if outer.get("kind") in {
                    "event-choice",
                    "static-event-choice-select",
                    "static-event-choice-confirm",
                    "outing-choice",
                }:
                    self._remember_event_choice_pending(outer)
                elif (
                    outer.get("kind") == "event-choice-confirm"
                    and isinstance(prior_event_pending, Mapping)
                    and prior_event_pending.get("owner") == "generic"
                ):
                    # Retain the same selected target/LocalSave owner for one
                    # bounded resend.  A successful click changes the page or
                    # authority and resets the transaction before another
                    # input can be emitted.
                    outer = dict(self._route_generic_event_confirm_retry(outer))
                elif prior_event_pending is not None and outer.get("kind") not in {
                    "wait",
                    "event-choice",
                    "event-choice-confirm",
                    "static-event-choice-confirm",
                    "outing-choice-confirm",
                }:
                    # A typed next page or a changed owner releases the
                    # previous selection.  Resume waits and direct confirms
                    # retain it for a bounded retry if Maa dropped input.
                    self._nia_event_choice_pending = None
                    self._reset_event_confirm_retry()
                elif prior_event_pending is not None and outer.get("kind") == "wait":
                    wait_evidence = outer.get("evidence")
                    screen_evidence = (
                        wait_evidence.get("screen_evidence")
                        if isinstance(wait_evidence, Mapping)
                        else None
                    )
                    if (
                        isinstance(screen_evidence, Mapping)
                        and screen_evidence.get("reason")
                        == "event-screen-identity-mismatch"
                    ):
                        # The old event is visibly gone while the LocalSave
                        # write is still catching up.  Release the owner so
                        # the next typed reader may classify the new page;
                        # no input is emitted on this mismatch frame.
                        self._nia_event_choice_pending = None
                        self._reset_event_confirm_retry()
                capture = outer.get("capture")
                if not isinstance(capture, Mapping):
                    raise ValueError("N.I.A. subpage returned no capture")
                card_operation = self._nia_card_operation_session
                if (
                    card_operation is not None
                    and card_operation.target == "customize"
                    and card_operation.customize_notification_pending
                ):
                    if outer.get("target") == "card-customize-notification":
                        return outer
                    try:
                        option_header = self._nia_customize_option_header(capture)
                    except (FileNotFoundError, OSError, RuntimeError, ValueError):
                        option_header = None
                    if option_header is not None:
                        self._nia_complete_customize_round()
                        card_operation.customize_notification_pending = False
                        projected = dict(
                            self._nia_customize_option_surface_or_wait(
                                capture=capture,
                                produce_points=snapshot.produce_points,
                                header=option_header,
                            )
                        )
                        projected["outer_authority"] = dict(
                            outer.get("outer_authority", {})
                        )
                        return projected
                    if outer.get("kind") == "mirror-choice":
                        self._nia_complete_customize_round()
                        card_operation.customize_notification_pending = False
                    else:
                        return {
                            "capture": dict(capture),
                            "kind": "wait",
                            "target": "customize-notification-transition",
                            "evidence": {
                                "policy": (
                                    "retain-customize-notification-owner-until-"
                                    "typed-successor"
                                ),
                                "unproven_outer_kind": outer.get("kind"),
                                "unproven_outer_target": outer.get("target"),
                            },
                            "outer_authority": dict(
                                outer.get("outer_authority", {})
                            ),
                        }
                if (
                    card_operation is not None
                    and card_operation.target == "customize"
                    and card_operation.customize_execute_submitted
                ):
                    try:
                        confirm_header = self._nia_customize_confirm_header(capture)
                    except (FileNotFoundError, OSError, RuntimeError, ValueError):
                        confirm_header = None
                    if confirm_header is not None:
                        projected = dict(
                            self._nia_customize_confirm_surface(
                                capture=capture,
                                header=confirm_header,
                            )
                        )
                        projected["outer_authority"] = dict(
                            outer.get("outer_authority", {})
                        )
                        return projected
                    try:
                        option_header = self._nia_customize_option_header(capture)
                    except (FileNotFoundError, OSError, RuntimeError, ValueError):
                        option_header = None
                    if option_header is not None:
                        if card_operation.customize_confirm_submitted:
                            return {
                                "capture": dict(capture),
                                "kind": "wait",
                                "target": "post-confirm-customize-grid",
                                "evidence": {
                                    "policy": "retain-confirmed-customize-owner",
                                    "header": dict(option_header),
                                },
                                "outer_authority": dict(
                                    outer.get("outer_authority", {})
                                ),
                            }
                        projected = dict(
                            self._nia_customize_option_surface_or_wait(
                                capture=capture,
                                produce_points=snapshot.produce_points,
                                header=option_header,
                            )
                        )
                        projected["outer_authority"] = dict(
                            outer.get("outer_authority", {})
                        )
                        return projected
                if (
                    outer.get("target") == "card-customize-notification"
                    and self._nia_card_operation_session is not None
                    and self._nia_card_operation_session.target == "customize"
                ):
                    self._nia_card_operation_session.customize_notification_pending = True
                    return outer
                if outer.get("kind") == "global-home-new-produce-blocked":
                    # An exact Home ``ProduceStart`` tile is never a generic
                    # story continuation.  Returning a typed unknown here
                    # prevents the downstream Click_1 fallback from starting
                    # a different Produce attempt.
                    return {
                        **outer,
                        "page_override": PAGE_UNKNOWN,
                        "reason": "global-home-shows-new-produce; same-run resume unavailable",
                    }
                if outer.get("kind") == "active-produce-resume":
                    try:
                        resume_authority = _resolve_nia_active_produce_resume_authority(
                            snapshot,
                            expected_run_id=self.expected_run_id,
                            expected_idol_card_id=self.idol_card_id,
                            expected_produce_id=self.produce_id,
                            expected_plan_type=self.plan_type,
                        )
                    except (FileNotFoundError, OSError, RuntimeError, TypeError, ValueError) as error:
                        # Home recognition alone says nothing about which run
                        # owns the tile.  Preserve the recognized frame as a
                        # typed, zero-input blocker instead of allowing any
                        # other page reader or Click_1 fallback to compete.
                        return {
                            **outer,
                            "page_override": PAGE_UNKNOWN,
                            "reason": (
                                "active-produce-home-binding-failed:"
                                f"{type(error).__name__}:{error}"
                            ),
                        }
                    outer["resume_authority"] = dict(resume_authority)
                    return outer
                ranked_keep = self._ranked_nia_drink_keep_surface(
                    outer,
                    capture=capture,
                )
                if ranked_keep is not None:
                    authority = outer.get("outer_authority")
                    projected = dict(ranked_keep)
                    if isinstance(authority, Mapping):
                        projected["outer_authority"] = dict(authority)
                    return projected
                self._nia_drink_keep_session = None
                self._nia_drink_keep_scroll_attempted = False
                if outer.get("kind") in {"drink-keep", "drink-keep-confirm"}:
                    # Capacity pages never fall through to generic item
                    # previews or inherit the low-level Keep action.  This is
                    # only a defensive fallback: the stateless ranker above
                    # normally returns either one exact toggle/Keep action or
                    # its own typed zero-input wait.
                    blocked = dict(outer)
                    blocked.pop("action", None)
                    blocked["kind"] = "wait"
                    blocked["target"] = "drink-keep-ranked-selection"
                    blocked["wait_timeout_reason"] = (
                        "drink-keep-candidates-unresolved"
                    )
                    return blocked
                if (
                    outer.get("kind") == "continue"
                    and outer.get("target") in {"skip-confirm", "yes"}
                ):
                    # Maa's exact foreground confirmation owns the frame even
                    # though the dimmed drink chooser remains visible behind
                    # it.  This also makes recovery after a process restart
                    # independent of the in-memory item session.
                    return outer
                reward_evidence = outer.get("evidence")
                drink_reward = bool(
                    isinstance(reward_evidence, Mapping)
                    and str(reward_evidence.get("authority", "")).startswith(
                        "maa-paddle-ocr-visible-drink-reward-state"
                    )
                )
                if (
                    self._nia_item_reward_session is not None
                    and self._nia_item_reward_session.submitted
                    and outer.get("kind") in {
                        "drink-reject-select",
                        "drink-reject-confirm",
                    }
                ):
                    # The selected reward remains visually a three-item
                    # chooser even though its Receive button is disabled by
                    # the three-drink inventory limit.  Once the item
                    # transaction has submitted, the Maa/OCR-owned reject row
                    # is the only actionable foreground state.  Route it
                    # before re-entering the generic item-layout wait.
                    drink_reject = self._route_drink_reject_transaction(outer)
                    if drink_reject is not None:
                        return drink_reject
                if (
                    self._nia_item_reward_session is not None
                    and self._nia_item_reward_session.submitted
                    and outer.get("kind") == "continue"
                    and isinstance(outer.get("action"), Mapping)
                ):
                    # A typed foreground notification (for example the
                    # post-present "skill card acquired" animation) proves
                    # that the submitted item chooser has ended.  Release its
                    # speculative card-handoff probe before it can reinterpret
                    # the notification artwork as another three-tile row.
                    self._nia_item_reward_session = None
                    self._nia_item_reward_authority_key = None
                    self._clear_initial_choice_transaction()
                    return outer
                if (
                    self._nia_item_reward_session is not None
                    and self._nia_item_reward_session.submitted
                ):
                    # The next card reward can share the exact same ProduceSave
                    # authority as the item reward.  Keep item ownership while
                    # issuing one reversible preview, and transfer it only when
                    # that selected slot exposes an exact Master card title.
                    handoff = self._submitted_item_reward_card_handoff(
                        capture=capture,
                        allow_probe=(
                            not drink_reward
                            and outer.get("kind")
                            in {"reward-choice", "reward-confirm"}
                        ),
                        snapshot=snapshot,
                        reroll_action=self._nia_reward_reroll_action(outer),
                    )
                    if handoff is not None:
                        projected = dict(handoff)
                        authority = outer.get("outer_authority")
                        if isinstance(authority, Mapping):
                            projected["outer_authority"] = dict(authority)
                        return projected
                # A full drink chooser contains a visible "do not receive"
                # row, so the Maa subpage detector can legitimately report a
                # reject control on the very same frame.  The three-choice
                # item layout owns that frame: preview and rank the actual
                # drinks before the optional reject row is considered.
                item_reward_visible = self._nia_item_reward_layout(capture)
                if item_reward_visible:
                    projected = dict(
                        self._nia_item_reward_surface(capture=capture)
                    )
                    authority = outer.get("outer_authority")
                    if isinstance(authority, Mapping):
                        projected["outer_authority"] = dict(authority)
                    return projected
                drink_reject = self._route_drink_reject_transaction(outer)
                if drink_reject is not None:
                    # This foreground choice belongs to the active drink
                    # transaction; card/item readers behind it cannot compete.
                    return drink_reject
                if outer.get("kind") == "card-operation-page":
                    target = outer.get("target")
                    if not isinstance(target, str) or not target:
                        raise ValueError("N.I.A. card operation has no target")
                    card_operation = self._nia_card_operation_session
                    if (
                        target == "customize"
                        and card_operation is not None
                        and (
                            card_operation.customize_confirm_submitted
                            or card_operation.customize_exit_ready
                        )
                    ):
                        return {
                            "capture": dict(capture),
                            "kind": "wait",
                            "target": (
                                "customize-exit-ready"
                                if card_operation.customize_exit_ready
                                else "post-confirm-customize-grid"
                            ),
                            "evidence": {
                                "policy": (
                                    "release-to-existing-guide-exit"
                                    if card_operation.customize_exit_ready
                                    else "retain-confirmed-customize-owner"
                                ),
                            },
                            "outer_authority": dict(
                                outer.get("outer_authority", {})
                            ),
                        }
                    projected = dict(
                        self._nia_card_operation_surface_or_wait(
                            capture=capture,
                            target=target,
                        )
                    )
                    authority = outer.get("outer_authority")
                    if isinstance(authority, Mapping):
                        projected["outer_authority"] = dict(authority)
                    return projected
                if self._nia_card_operation_session is not None:
                    card_operation = self._nia_card_operation_session
                    if (
                        card_operation.target == "customize"
                        and (
                            card_operation.customize_execute_submitted
                            or card_operation.customize_exit_ready
                        )
                        and outer.get("kind")
                        in {
                            "click-1-fallback",
                            "reward-choice",
                            "reward-confirm",
                            "wait",
                        }
                    ):
                        return {
                            "capture": dict(capture),
                            "kind": "wait",
                            "target": "customize-option-settlement",
                            "evidence": {
                                "policy": "retain-customize-owner-until-typed-successor",
                                "unproven_outer_kind": outer.get("kind"),
                            },
                            "outer_authority": dict(
                                outer.get("outer_authority", {})
                            ),
                        }
                    # A typed successor after the final Execute is the only
                    # page-local terminal for this card operation.
                    if (
                        card_operation.submitted
                        and card_operation.target in {"strengthen", "delete"}
                        and card_operation.selected_slot is not None
                    ):
                        try:
                            from .run_identity import load_active_run, paths_for
                            from .run_shadow import (
                                card_operation_observation,
                                load_matching_active_run_shadow,
                                save_run_shadow,
                            )

                            selected_card = next(
                                (
                                    offer
                                    for offer in card_operation.offers
                                    if getattr(offer, "slot", None)
                                    == card_operation.selected_slot
                                ),
                                None,
                            )
                            shadow = load_matching_active_run_shadow(
                                self.produce_id,
                                self.idol_card_id,
                                expected_run_id=self.expected_run_id,
                            )
                            active = load_active_run()
                            if (
                                selected_card is not None
                                and shadow is not None
                                and active is not None
                                and active.produce_id == self.produce_id
                                and active.idol_card_id == self.idol_card_id
                                and (
                                    self.expected_run_id is None
                                    or active.run_id == self.expected_run_id
                                )
                            ):
                                observation = card_operation_observation(
                                    shadow,
                                    operation=card_operation.target,
                                    selected_card=selected_card,
                                    settled_successor=outer,
                                )
                                save_run_shadow(
                                    shadow.apply(observation),
                                    paths_for(active).shadow,
                                )
                        except (OSError, TypeError, ValueError):
                            # Shadow learning is observational.  A missing or
                            # stale deck must never reopen a completed UI
                            # transaction or block the cultivation flow.
                            pass
                    self._nia_card_operation_session = None
                if (
                    self._nia_item_reward_session is not None
                    and self._nia_item_reward_session.submitted
                ):
                    if outer.get("kind") in {
                        "click-1-fallback",
                        "reward-choice",
                        "reward-confirm",
                        "wait",
                    }:
                        # No fresh owner has been proven.  In particular an
                        # empty Maa node batch and its generic Click_1 fallback
                        # cannot release the submitted item transaction.
                        return {
                            "capture": dict(capture),
                            "kind": "wait",
                            "target": "item-reward-owner-handoff",
                            "evidence": {
                                "policy": (
                                    "retain-submitted-item-owner-until-next-"
                                    "typed-surface"
                                ),
                                "unproven_outer_kind": outer.get("kind"),
                            },
                            "outer_authority": (
                                dict(outer["outer_authority"])
                                if isinstance(
                                    outer.get("outer_authority"), Mapping
                                )
                                else {}
                            ),
                        }
                    self._nia_item_reward_session = None
                    self._nia_item_reward_authority_key = None
                reward_visible = (
                    not drink_reward
                    and (
                        outer.get("target") == "cards-get"
                        or self._nia_card_reward_layout(capture)
                    )
                )
                if reward_visible:
                    projected = dict(
                        self._nia_reward_ocr_surface(
                            capture=capture,
                            snapshot=snapshot,
                            reroll_action=self._nia_reward_reroll_action(outer),
                        )
                    )
                    authority = outer.get("outer_authority")
                    if isinstance(authority, Mapping):
                        projected["outer_authority"] = dict(authority)
                    return projected
                if (
                    self._nia_reward_ocr_session is not None
                    and self._nia_reward_ocr_session.submitted
                ):
                    self._nia_reward_ocr_session = None
                if self._nia_reward_preview_session is not None:
                    try:
                        return {
                            "page_override": PAGE_REWARD,
                            **self._nia_reward_surface(
                                capture=capture,
                                outer_payload=outer,
                            ),
                        }
                    except (FileNotFoundError, RuntimeError, TypeError, ValueError):
                        if self._nia_reward_preview_session is not None:
                            raise
                return outer

            readers = {
                **readers,
                PAGE_NIA_OUTER: read_nia_surface,
            }
        else:
            # Initial/Plan2 retain their established screenshot readers. N.I.A.
            # deliberately does not inherit these fallbacks: its original Maa
            # graph owns result/reward/passive/card-display continuations, while
            # Produce LocalSave is rebased before the resulting click. Card
            # artwork, text, colour and support markers are therefore not N.I.A.
            # unattended state authorities.
            from .live_source import (
                read_live_activity_reward,
                read_live_initial_regular_post_audition_dialogue,
                read_live_initial_regular_result,
                read_live_reward,
                read_live_settled_card_reward_display,
                read_live_settled_passive_notification,
                read_live_card_acquire_notification,
                read_live_training_choice,
                read_live_training_drink_reward,
            )

            # Read Maa's page graph once for this routing pass.  A proven
            # foreground modal gets first refusal over the still-visible
            # result/reward page behind it; a non-modal Maa hit is cached and
            # reused by the ordinary fallback after the semantic readers.
            # This is a layer-order rule, not an additional screenshot gate.
            initial_maa_surface_cache: Mapping[str, Any] | None = None

            def read_initial_maa_surface() -> Mapping[str, Any]:
                nonlocal initial_maa_surface_cache
                if initial_maa_surface_cache is None:
                    from .nia_live_outer import read_live_nia_outer_subpage

                    maa_surface = dict(
                        read_live_nia_outer_subpage(
                            snapshot,
                            allow_event_choice=False,
                            produce_id=self.produce_id,
                            idol_card_id=self.idol_card_id,
                        )
                    )
                    if maa_surface.get("kind") == "card-operation-page":
                        target = maa_surface.get("target")
                        capture = maa_surface.get("capture")
                        if not isinstance(target, str) or not target:
                            raise ValueError("Maa card operation has no target")
                        if not isinstance(capture, Mapping):
                            raise ValueError("Maa card operation has no capture")
                        projected = dict(
                            self._nia_card_operation_surface_or_wait(
                                capture=capture,
                                target=target,
                            )
                        )
                        authority = maa_surface.get("outer_authority")
                        if isinstance(authority, Mapping):
                            projected["outer_authority"] = dict(authority)
                        maa_surface = projected
                    elif self._nia_card_operation_session is not None:
                        self._nia_card_operation_session = None
                    initial_maa_surface_cache = maa_surface
                return initial_maa_surface_cache

            def read_initial_foreground_surface() -> Mapping[str, Any]:
                surface = read_initial_maa_surface()
                evidence = surface.get("evidence")
                policy = (
                    str(evidence.get("policy", ""))
                    if isinstance(evidence, Mapping)
                    else ""
                )
                if (
                    policy.startswith("maa-foreground-")
                    or surface.get("target") == "rest-confirm"
                    or surface.get("kind")
                    in {"drink-reject-select", "drink-reject-confirm"}
                ):
                    return surface
                raise ValueError("Maa surface is not a foreground modal")

            readers = {
                **readers,
                PAGE_NIA_OUTER: read_initial_foreground_surface,
                PAGE_TRAINING: read_live_training_choice,
                PAGE_RESULT: read_live_initial_regular_result,
                PAGE_POST_AUDITION_DIALOGUE: (
                    read_live_initial_regular_post_audition_dialogue
                ),
                PAGE_TRAINING_REWARD: read_live_training_drink_reward,
                PAGE_PASSIVE_NOTIFICATION: read_live_settled_passive_notification,
                PAGE_CARD_ACQUIRE_NOTIFICATION: read_live_card_acquire_notification,
                PAGE_CARD_REWARD_DISPLAY: read_live_settled_card_reward_display,
                PAGE_ACTIVITY_REWARD: read_live_activity_reward,
                PAGE_REWARD: lambda: read_live_reward(
                    expected_run_id=self.expected_run_id,
                    expected_idol_card_id=self.idol_card_id,
                    expected_produce_id=self.produce_id,
                ),
            }
        order_by_previous = {
            None: (
                PAGE_OVERVIEW,
                PAGE_NIA_OUTER,
                PAGE_TRAINING,
                PAGE_RESULT,
                PAGE_TRAINING_REWARD,
                PAGE_PASSIVE_NOTIFICATION,
                PAGE_CARD_ACQUIRE_NOTIFICATION,
                PAGE_CARD_REWARD_DISPLAY,
                PAGE_ACTIVITY_REWARD,
                PAGE_POST_AUDITION_DIALOGUE,
                PAGE_REWARD,
            ),
            PAGE_OVERVIEW: (
                PAGE_NIA_OUTER,
                PAGE_TRAINING,
                PAGE_RESULT,
                PAGE_TRAINING_REWARD,
                PAGE_PASSIVE_NOTIFICATION,
                PAGE_CARD_ACQUIRE_NOTIFICATION,
                PAGE_CARD_REWARD_DISPLAY,
                PAGE_ACTIVITY_REWARD,
                PAGE_OVERVIEW,
                PAGE_REWARD,
            ),
            PAGE_NIA_OUTER: (
                PAGE_NIA_OUTER,
                PAGE_RESULT,
                PAGE_ACTIVITY_REWARD,
                PAGE_PASSIVE_NOTIFICATION,
                PAGE_CARD_ACQUIRE_NOTIFICATION,
                PAGE_CARD_REWARD_DISPLAY,
                PAGE_REWARD,
                PAGE_OVERVIEW,
            ),
            PAGE_TRAINING: (
                PAGE_RESULT,
                PAGE_TRAINING_REWARD,
                PAGE_PASSIVE_NOTIFICATION,
                PAGE_CARD_ACQUIRE_NOTIFICATION,
                PAGE_CARD_REWARD_DISPLAY,
                PAGE_ACTIVITY_REWARD,
                PAGE_REWARD,
                PAGE_OVERVIEW,
                PAGE_TRAINING,
            ),
            PAGE_EXAM: (
                PAGE_RESULT,
                PAGE_TRAINING_REWARD,
                PAGE_PASSIVE_NOTIFICATION,
                PAGE_CARD_ACQUIRE_NOTIFICATION,
                PAGE_CARD_REWARD_DISPLAY,
                PAGE_ACTIVITY_REWARD,
                PAGE_REWARD,
                PAGE_OVERVIEW,
            ),
            PAGE_RESULT: (
                PAGE_POST_AUDITION_DIALOGUE,
                PAGE_TRAINING_REWARD,
                PAGE_PASSIVE_NOTIFICATION,
                PAGE_CARD_ACQUIRE_NOTIFICATION,
                PAGE_CARD_REWARD_DISPLAY,
                PAGE_ACTIVITY_REWARD,
                PAGE_REWARD,
                PAGE_OVERVIEW,
                PAGE_RESULT,
            ),
            PAGE_POST_AUDITION_DIALOGUE: (
                PAGE_POST_AUDITION_DIALOGUE,
                PAGE_TRAINING_REWARD,
                PAGE_PASSIVE_NOTIFICATION,
                PAGE_CARD_ACQUIRE_NOTIFICATION,
                PAGE_CARD_REWARD_DISPLAY,
                PAGE_ACTIVITY_REWARD,
                PAGE_REWARD,
                PAGE_OVERVIEW,
                PAGE_RESULT,
            ),
            PAGE_ACTIVITY_REWARD: (
                PAGE_OVERVIEW,
                PAGE_ACTIVITY_REWARD,
                PAGE_CARD_ACQUIRE_NOTIFICATION,
                PAGE_RESULT,
                PAGE_REWARD,
            ),
            PAGE_TRAINING_REWARD: (
                PAGE_PASSIVE_NOTIFICATION,
                PAGE_CARD_ACQUIRE_NOTIFICATION,
                PAGE_CARD_REWARD_DISPLAY,
                PAGE_OVERVIEW,
                PAGE_TRAINING_REWARD,
                PAGE_ACTIVITY_REWARD,
                PAGE_RESULT,
                PAGE_REWARD,
            ),
            PAGE_PASSIVE_NOTIFICATION: (
                PAGE_PASSIVE_NOTIFICATION,
                PAGE_CARD_ACQUIRE_NOTIFICATION,
                PAGE_CARD_REWARD_DISPLAY,
                PAGE_REWARD,
                PAGE_OVERVIEW,
                PAGE_RESULT,
            ),
            PAGE_CARD_REWARD_DISPLAY: (
                PAGE_CARD_ACQUIRE_NOTIFICATION,
                PAGE_CARD_REWARD_DISPLAY,
                PAGE_PASSIVE_NOTIFICATION,
                PAGE_OVERVIEW,
                PAGE_REWARD,
            ),
            PAGE_REWARD: (
                PAGE_CARD_ACQUIRE_NOTIFICATION,
                PAGE_CARD_REWARD_DISPLAY,
                PAGE_PASSIVE_NOTIFICATION,
                PAGE_OVERVIEW,
                PAGE_REWARD,
                PAGE_RESULT,
            ),
        }
        errors: dict[str, str] = {}
        ordered_pages = order_by_previous.get(previous_page, order_by_previous[None])
        if is_nia:
            # N.I.A. has Maa-native continuation, reward, retry, drink and card
            # management pages between every major state.  Give its fixed
            # subpage router first refusal regardless of the prior page; when
            # no supported Maa template is visible it fails closed and the
            # more specific existing reader gets its normal turn.
            ordered_pages = (
                PAGE_NIA_OUTER,
                *(page for page in ordered_pages if page != PAGE_NIA_OUTER),
            )
        elif PAGE_NIA_OUTER not in ordered_pages:
            # Initial uses the same modal layer as N.I.A.  Only the foreground
            # wrapper can win here; ordinary Maa hits deliberately fall
            # through to the existing semantic page readers.
            ordered_pages = (PAGE_NIA_OUTER, *ordered_pages)
        for page in ordered_pages:
            if page not in readers:
                continue
            try:
                payload = readers[page]()
                if (
                    not is_nia
                    and page in {PAGE_ACTIVITY_REWARD, PAGE_TRAINING_REWARD}
                    and not isinstance(payload.get("action"), Mapping)
                ):
                    # The strict reader may still preserve useful OCR/LocalSave
                    # telemetry while failing to prove a bespoke continuation
                    # box.  Do not return that half-actionable surface and stop
                    # the run; let Maa's existing Produce nodes below own the
                    # button instead.
                    raise ValueError(
                        f"{page} has no action; delegate continuation to Maa"
                    )
                override = payload.get("page_override")
                if (
                    is_nia
                    and page == PAGE_OVERVIEW
                    and override == PAGE_NIA_OUTER
                    and payload.get("kind") == "wait"
                    and payload.get("target") == "expected-route-actions"
                ):
                    # A received card can leave one enlarged, unlabelled
                    # detail frame after its reward transaction has already
                    # settled.  The exact-route overview reader quite
                    # correctly refuses to rank its empty Maa batch, but that
                    # wait used to mask Maa's final Click_1 forever.  Permit
                    # exactly that existing continuation only when the
                    # durable strategy checkpoint says the immediately prior
                    # action was reward-confirm, the run shadow proves the
                    # same card was received, and this LocalSave equals the
                    # settled next-week state.  No screen/card identity is
                    # inferred here.
                    from .live_source import (
                        resolve_nia_post_reward_detail_authority,
                    )

                    post_reward = resolve_nia_post_reward_detail_authority(
                        snapshot,
                        expected_run_id=self.expected_run_id,
                        expected_idol_card_id=self.idol_card_id,
                        expected_produce_id=self.produce_id,
                        expected_plan_type=self.plan_type,
                    )
                    wait_authority = _outer_authority_key(payload)
                    if (
                        post_reward is not None
                        and wait_authority == self._choice_authority_key(snapshot)
                    ):
                        from .nia_live_outer import read_live_nia_click1_fallback

                        fallback = dict(read_live_nia_click1_fallback(snapshot))
                        if (
                            _outer_authority_key(fallback) == wait_authority
                            and _capture_window_key(fallback)
                            == _capture_window_key(payload)
                        ):
                            evidence = fallback.get("evidence")
                            fallback["evidence"] = {
                                **(
                                    dict(evidence)
                                    if isinstance(evidence, Mapping)
                                    else {}
                                ),
                                "policy": (
                                    "durable-post-reward-detail-uses-existing-"
                                    "maa-click1-v1"
                                ),
                                "post_reward_authority": dict(post_reward),
                            }
                            fallback["target"] = "single-card-detail"
                            payload = fallback
                            override = PAGE_NIA_OUTER
                if override is not None:
                    payload = {
                        key: value for key, value in payload.items()
                        if key != "page_override"
                    }
                return InitialRegularSurface(
                    str(override) if override is not None else page,
                    payload,
                )
            except (FileNotFoundError, RuntimeError, TypeError, ValueError) as error:
                errors[page] = f"{type(error).__name__}: {error}"
        if not is_nia:
            try:
                from .controller_client import send_command

                reward_capture = dict(
                    send_command("capture_screen_once", timeout=15.0)
                )
                projected = self._initial_reward_choice_surface(
                    capture=reward_capture,
                    snapshot=snapshot,
                )
                if projected is not None:
                    return InitialRegularSurface(
                        PAGE_NIA_OUTER,
                        projected,
                    )
            except (FileNotFoundError, RuntimeError, TypeError, ValueError) as error:
                errors["maa-title-reward"] = (
                    f"{type(error).__name__}: {error}"
                )
            try:
                from .live_source import read_live_initial_opening_plan_choice

                return InitialRegularSurface(
                    PAGE_NIA_OUTER,
                    read_live_initial_opening_plan_choice(
                        snapshot,
                        idol_card_id=self.idol_card_id,
                    ),
                )
            except (FileNotFoundError, RuntimeError, TypeError, ValueError) as error:
                errors["opening-plan-choice"] = (
                    f"{type(error).__name__}: {error}"
                )
            # Initial uses the same MaaGakumasu Produce page graph as N.I.A.
            # for ordinary buttons, full-drink handling, reward fallbacks,
            # card-operation pages and other non-exam subpages.  The strict
            # readers above retain first refusal when they can add useful
            # LocalSave-backed state, but a localized title/artwork/OCR miss
            # must not strand an otherwise actionable Maa page.  Reuse Maa's
            # own recognizers and one-click policy before considering the
            # surface unknown; this is intentionally not another visual gate.
            try:
                return InitialRegularSurface(
                    PAGE_NIA_OUTER,
                    read_initial_maa_surface(),
                )
            except (FileNotFoundError, RuntimeError, TypeError, ValueError) as error:
                errors["maa-common-subpage"] = (
                    f"{type(error).__name__}: {error}"
                )

        # MaaGakumasu keeps Click_1 as the final ProduceEntry branch for
        # unlabelled dialogue/result animations in both Initial and N.I.A.
        # All named readers and Maa subpage nodes above have already had first
        # refusal, so reuse that existing continuation rather than creating a
        # new text/colour/coordinate classifier.  The run loop binds and
        # rate-limits it against the current Produce LocalSave authority.
        try:
            from .nia_live_outer import read_live_nia_click1_fallback

            return InitialRegularSurface(
                PAGE_NIA_OUTER,
                read_live_nia_click1_fallback(snapshot),
            )
        except (FileNotFoundError, RuntimeError, TypeError, ValueError) as error:
            errors["maa-click-1-fallback"] = (
                f"{type(error).__name__}: {error}"
            )
        # A failed OCR/Maa pass is not permission for any further input.
        return InitialRegularSurface(PAGE_UNKNOWN, {"reader_errors": errors})


def _outcome_mapping(value: object) -> Mapping[str, Any]:
    if hasattr(value, "to_dict"):
        mapped = value.to_dict()  # type: ignore[union-attr]
        if isinstance(mapped, Mapping):
            return mapped
    if isinstance(value, Mapping):
        return dict(value)
    return {"result": repr(value)}


def _outer_authority_key(payload: Mapping[str, Any]) -> tuple[object, ...] | None:
    """Return the semantic Produce snapshot used to bind one weekly choice.

    The descriptive ``kind`` differs between overview and subpage readers, so
    only the actual LocalSave values participate in the transition identity.
    """

    authority = payload.get("outer_authority")
    if not isinstance(authority, Mapping):
        return None
    fields = (
        "log_count",
        "week",
        "last_completed_week",
        "stamina",
        "max_stamina",
        "produce_points",
        "vocal",
        "dance",
        "visual",
    )
    if any(field not in authority for field in fields):
        return None
    return (*tuple(authority[field] for field in fields), authority.get("vote_count"))


def _weekly_owner_semantic_key(
    value: Mapping[str, Any] | Any,
) -> tuple[object, ...] | None:
    """Return only the stable state fields used by a pending weekly owner.

    ``_outer_authority_key`` intentionally keeps ``log_count`` for complete
    transition/duplicate telemetry.  The owner hand-off has a narrower
    lifetime: a LocalSave log append is not a new weekly transaction, while a
    change to any week/state field is.  Keep this projection local to the
    pending-owner path rather than changing the global authority contract.
    """

    if isinstance(value, Mapping):
        authority = _outer_authority_key(value)
        return None if authority is None else authority[1:]
    return (
        getattr(value, "latest_week_marker", None),
        getattr(value, "last_completed_week", None),
        getattr(value, "stamina", None),
        getattr(value, "max_stamina", None),
        getattr(value, "produce_points", None),
        getattr(value, "vocal", None),
        getattr(value, "dance", None),
        getattr(value, "visual", None),
        getattr(value, "vote_count", None),
    )


def _input_was_submitted(outcome: Mapping[str, Any]) -> bool:
    """Return true only when an executor reports an actual input command."""

    if outcome.get("skipped") is True:
        return False
    if "input_submitted" in outcome:
        return outcome.get("input_submitted") is True
    if "submitted" in outcome:
        return outcome.get("submitted") is True
    click_results = outcome.get("click_results")
    return isinstance(click_results, (list, tuple)) and bool(click_results)


def _nia_surface_owner(
    payload: Mapping[str, Any],
) -> tuple[str | None, int | None]:
    route = payload.get("route")
    route_produce = route.get("produce_id") if isinstance(route, Mapping) else None
    authority = payload.get("outer_authority")
    week = authority.get("week") if isinstance(authority, Mapping) else None
    return (
        route_produce if isinstance(route_produce, str) else None,
        week if type(week) is int else None,
    )


def _capture_window_key(payload: Mapping[str, Any]) -> tuple[int, int] | None:
    """Return the native window identity attached to one read-only capture."""

    capture = payload.get("capture")
    if not isinstance(capture, Mapping):
        return None
    hwnd = capture.get("hwnd")
    pid = capture.get("pid")
    if (
        isinstance(hwnd, bool)
        or not isinstance(hwnd, int)
        or hwnd <= 0
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
    ):
        return None
    return hwnd, pid


def _inner_imitation_prior_status() -> dict[str, Any]:
    """Report the exact native-verified artifact used by production runner."""

    try:
        from .leaderboard_card_imitation_prior import (
            load_leaderboard_card_imitation_prior,
        )
        from .generic_inner_policy_runtime_v2 import (
            DEFAULT_NATIVE_VERIFIED_PRIOR_PATH,
        )

        prior = load_leaderboard_card_imitation_prior(
            DEFAULT_NATIVE_VERIFIED_PRIOR_PATH
        )
    except Exception as error:
        return {
            "state": "missing",
            "source": str(
                locals().get(
                    "DEFAULT_NATIVE_VERIFIED_PRIOR_PATH",
                    INNER_IMITATION_VERIFIED_PRIOR_CANDIDATE,
                )
            ),
            "reason": f"{type(error).__name__}: {error}",
        }
    source_path = Path(
        getattr(prior, "source_path", None) or DEFAULT_NATIVE_VERIFIED_PRIOR_PATH
    )
    if prior is None:
        return {
            "state": "missing",
            "source": str(DEFAULT_NATIVE_VERIFIED_PRIOR_PATH),
        }
    count = getattr(prior, "observation_count", None)
    if not isinstance(count, int) or count < 1:
        return {
            "state": "empty",
            "source": str(source_path),
            "observation_count": 0 if not isinstance(count, int) else count,
            "reason": "verified-card-identity-unavailable",
        }
    promotion_gate: dict[str, Any] | None = None
    try:
        from .native_search_imitation_gate import (
            evaluate_native_search_imitation_readiness,
        )

        readiness = evaluate_native_search_imitation_readiness(
            episodes_source=source_path,
        )
        summary = readiness.get("summary")
        runtime = readiness.get("runtime")
        promotion_gate = {
            "status": readiness.get("promotion", {}).get("status")
            if isinstance(readiness.get("promotion"), Mapping)
            else "not-ready",
            "default_enabled": (
                runtime.get("inner_imitation_default_enabled")
                if isinstance(runtime, Mapping)
                else False
            ),
            "eligible_opt_in_flows": (
                runtime.get("eligible_opt_in_flows", [])
                if isinstance(runtime, Mapping)
                else []
            ),
            "blocker_counts": (
                summary.get("blocker_counts", {})
                if isinstance(summary, Mapping)
                else {}
            ),
        }
    except Exception as error:
        # Readiness is an audit side channel; it must never take ownership of
        # the explicit opt-in runner or make the existing baseline unavailable.
        promotion_gate = {
            "status": "unavailable",
            "default_enabled": False,
            "eligible_opt_in_flows": [],
            "blockers": [f"readiness-audit-failed:{type(error).__name__}"],
        }
    return {
        "state": "ready",
        "source": str(source_path),
        "observation_count": count,
        "source_count": getattr(prior, "source_count", None),
        "flow_count": len(getattr(prior, "known_cards_by_flow", {})),
        "allowed_treatment": "explicit-flow-and-card-ready-marker-required",
        "promotion_gate": promotion_gate,
    }


def _dispatch_optional_inner_imitation(
    plan_type: str,
    exam_save_path: Path,
    *,
    baseline_runner: Callable[[], Mapping[str, Any]],
    enabled: bool,
    runner: InnerImitationExamRunner | None,
) -> dict[str, Any]:
    """Run an injected inner policy or the existing Maa baseline once.

    This is deliberately a wrapper around, rather than a rewrite of, the
    baseline call.  The inner callback owns its per-step runtime and receives
    the baseline callback for one-way abstention.  A malformed callback result
    is treated as an abstention and invokes baseline exactly once.
    """

    if not enabled:
        return dict(baseline_runner())

    baseline_cache: dict[str, Any] | None = None
    baseline_attempted = False
    baseline_error: Exception | None = None

    def baseline_once() -> Mapping[str, Any]:
        """Make the one-way baseline callback idempotent at this boundary."""

        nonlocal baseline_cache, baseline_attempted, baseline_error
        if baseline_attempted:
            if baseline_error is not None:
                # Preserve the original failure without re-entering a
                # side-effecting Maa boundary whose first attempt may already
                # have submitted input.
                raise baseline_error
            if baseline_cache is None:
                raise RuntimeError("baseline callback was attempted without a receipt")
            return dict(baseline_cache)
        baseline_attempted = True
        try:
            baseline_cache = dict(baseline_runner())
        except Exception as error:
            baseline_error = error
            raise
        return dict(baseline_cache)

    metadata: dict[str, Any] = {
        "enabled": True,
        "plan_type": plan_type,
        "runtime": "generic_inner_policy_runtime_v2",
        "prior": _inner_imitation_prior_status(),
    }
    if runner is None:
        result = dict(baseline_once())
        metadata.update(
            {
                "status": "baseline",
                "reason": "inner-imitation-runner-not-configured",
            }
        )
        result["inner_imitation"] = metadata
        return result

    try:
        raw = runner(plan_type, Path(exam_save_path), baseline_once)
    except Exception as error:
        result = dict(baseline_once())
        metadata.update(
            {
                "status": "baseline",
                "reason": "inner-imitation-runner-failed",
                "error": f"{type(error).__name__}: {error}",
            }
        )
        result["inner_imitation"] = metadata
        return result
    if not isinstance(raw, Mapping):
        result = dict(baseline_once())
        metadata.update(
            {
                "status": "baseline",
                "reason": "inner-imitation-runner-result-invalid",
            }
        )
        result["inner_imitation"] = metadata
        return result

    raw_result = dict(raw)
    mode = raw_result.get("mode")
    baseline_called = raw_result.get("baseline_called") is True
    fallback = raw_result.get("fallback") is True
    # Once the callback has touched the baseline boundary, it owns the rest
    # of the exam.  Even a malformed callback that then claims imitation must
    # not be allowed to switch back and submit another card.
    if baseline_cache is not None and not (
        baseline_called or fallback or mode == "maa-completion-baseline"
    ):
        result = dict(baseline_once())
        metadata.update(
            {
                "status": "baseline",
                "reason": "inner-imitation-baseline-already-started",
                "decision": raw_result,
            }
        )
        result["inner_imitation"] = metadata
        return result
    if baseline_called or fallback or mode == "maa-completion-baseline":
        # The outer callback is the only baseline authority.  Do not trust a
        # mode/receipt flag from an injected runner: a malformed runner may
        # claim fallback without ever entering Maa.  A successful outer call
        # is also preferable to a runner-supplied copy, which could be stale or
        # fabricated.  ``baseline_once`` remembers failures as well, so this
        # branch never retries a side-effecting attempt.
        if baseline_cache is None:
            result = dict(baseline_once())
        else:
            result = dict(baseline_cache)
        metadata.update(
            {
                "status": "baseline",
                "reason": "inner-imitation-abstained",
            }
        )
        result["inner_imitation"] = {
            **metadata,
            "decision": raw_result,
        }
        return result
    # An injected exam runner is authoritative only when it returned a
    # complete exam-shaped mapping.  It may contain any existing orchestration
    # fields, but must at least expose the accepted/terminal pair used by the
    # surrounding autopilot.
    if not isinstance(raw_result.get("accepted"), bool) or not isinstance(
        raw_result.get("terminal"), bool
    ):
        result = dict(baseline_once())
        metadata.update(
            {
                "status": "baseline",
                "reason": "inner-imitation-runner-result-incomplete",
                "decision": raw_result,
            }
        )
        result["inner_imitation"] = metadata
        return result
    metadata.update({"status": "imitation", "decision": raw_result})
    raw_result["inner_imitation"] = metadata
    return raw_result


def _reward_offer(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    state = payload.get("state")
    if not isinstance(state, Mapping) or not isinstance(state.get("offers"), list):
        raise ValueError("reward state is missing offers")
    recommended_slot = state.get("recommended_slot")
    matches = [
        value
        for value in state["offers"]
        if isinstance(value, Mapping) and value.get("slot") == recommended_slot
    ]
    if len(matches) != 1:
        raise ValueError("reward recommendation is not unique")
    return matches[0]


def run_initial_regular_autopilot(
    *,
    plan_type: str,
    surface_reader: SurfaceReader,
    snapshot_reader: Callable[[], ProduceOuterLocalSaveSnapshot],
    outer_advisor: Callable[..., OuterAdvice] = advise_initial_regular_outer,
    choice_executor: ChoiceExecutor = execute_initial_regular_choice,
    single_click_executor: SingleClickExecutor = execute_initial_regular_single_click,
    exam_dispatcher: ExamDispatcher = dispatch_initial_regular_exam,
    lesson_result_checkpointer: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    activity_reward_checkpointer: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    training_reward_checkpointer: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    reward_checkpointer: Callable[..., Mapping[str, Any]] | None = None,
    run_context_bootstrapper: RunContextBootstrapper | None = None,
    outer_observer_anchor_provider: OuterObserverAnchorProvider | None = None,
    source_run_id: str | None = None,
    nia_active_resume_executor: Callable[[Mapping[str, Any]], Mapping[str, Any]] = (
        execute_nia_active_produce_resume
    ),
    stop_requested: Callable[[], bool] = lambda: False,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval_seconds: float = 0.5,
    max_cycles: int = 200,
    max_unknown_reads: int = 3,
    exam_action_budget: int | None = None,
) -> InitialRegularAutopilotResult:
    """Run the Initial-Regular page loop until completion or an explicit stop."""

    if not plan_type:
        raise ValueError("plan_type must be non-empty")
    if max_cycles < 1 or max_unknown_reads < 1:
        raise ValueError("cycle and unknown-read limits must be positive")
    if poll_interval_seconds < 0:
        raise ValueError("poll_interval_seconds cannot be negative")
    if exam_action_budget is not None and (
        type(exam_action_budget) is not int or exam_action_budget < 1
    ):
        raise ValueError("exam_action_budget must be a positive integer or None")
    if not callable(nia_active_resume_executor):
        raise TypeError("nia_active_resume_executor must be callable")
    if outer_observer_anchor_provider is not None and not callable(
        outer_observer_anchor_provider
    ):
        raise TypeError("outer_observer_anchor_provider must be callable or None")
    if source_run_id is not None and (
        not isinstance(source_run_id, str) or not source_run_id.strip()
    ):
        raise ValueError("source_run_id must be non-empty text or None")

    steps: list[InitialRegularAutopilotStep] = []
    previous_page: str | None = None
    overview_output: Mapping[str, Any] | None = None
    unknown_reads = 0
    pending_overview_authority: tuple[object, ...] | None = None
    pending_overview_owner_authority: tuple[object, ...] | None = None
    pending_overview_reads = 0
    pending_overview_target: str | None = None
    pending_overview_followup_target: str | None = None
    pending_nia_click1_authority: tuple[object, ...] | None = None
    pending_nia_click1_inputs = 0
    pending_nia_click1_reads = 0
    pending_nia_wait_authority: tuple[object, ...] | None = None
    pending_nia_wait_reads = 0
    pending_nia_wait_target: str | None = None
    pending_nia_wait_window: tuple[int, int] | None = None
    pending_overview_alignment_authority: tuple[object, ...] | None = None
    pending_overview_alignment_reads = 0
    pending_terminal_exam_path: str | None = None
    pending_terminal_exam_reads = 0
    last_surface_diagnostic: Mapping[str, Any] | None = None
    last_monitor_snapshot: Mapping[str, Any] | None = None
    resolved_run_id = None if source_run_id is None else source_run_id.strip()
    exam_actions_dispatched = 0
    active_outer_transaction: ProduceOuterTransaction | None = None
    latest_outer_transaction_receipt: Mapping[str, Any] | None = None

    def outer_receipt_time() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def terminalize_outer_receipt(
        transaction: ProduceOuterTransaction,
        *,
        receipt_id: str,
        terminal_kind: str,
        terminal_surface: InitialRegularSurface,
    ) -> None:
        """Publish the terminal half of the current journal anchor.

        The terminal authority is always taken from the newly observed
        successor/completed surface.  Missing state-after authority remains an
        explicit incomplete mapping; the begin authority is never copied into
        the terminal row.
        """

        nonlocal latest_outer_transaction_receipt
        begin = latest_outer_transaction_receipt
        if (
            not isinstance(begin, Mapping)
            or begin.get("transaction_id") != transaction.transaction_id
        ):
            return
        authority = terminal_surface.payload.get("outer_authority")
        authority_mapping = dict(authority) if isinstance(authority, Mapping) else {}
        authority_complete = _outer_authority_key(terminal_surface.payload) is not None
        terminal_anchor, terminal_anchor_complete = (
            _terminal_outer_observer_anchor(
                outer_observer_anchor_provider,
                begin.get("observer_anchor", {}),
            )
        )
        details = begin.get("details")
        terminal_details = dict(details) if isinstance(details, Mapping) else {}
        terminal_details["receipt_phase"] = "terminal"
        terminal_details["terminal_authority_source"] = (
            "surface.outer_authority" if authority_mapping else "unavailable"
        )
        latest_outer_transaction_receipt = {
            **dict(begin),
            "record_id": (
                f"outer-transaction-v2:{transaction.transaction_id}:"
                f"{terminal_kind}"
            ),
            "recorded_at": outer_receipt_time(),
            "authority": authority_mapping,
            "authority_complete": authority_complete,
            "observer_anchor": terminal_anchor,
            "observer_anchor_complete": terminal_anchor_complete,
            "terminal_receipt_id": receipt_id,
            "terminal_kind": terminal_kind,
            "bc_eligible": False,
            "rl_eligible": False,
            "details": terminal_details,
        }

    def begin_submitted_outer_transaction(
        *,
        produce_id: str,
        week: int,
        target: str,
        surfaces: tuple[Any, ...],
        cycle: int,
        surface: InitialRegularSurface,
        decision_source: Mapping[str, Any] | None,
        legal_action_ids: tuple[str, ...],
        candidate_set_complete: bool,
        phase: str | None,
        observer_anchor: Mapping[str, Any],
        observer_anchor_complete: bool,
        submission_kind: str,
    ) -> ProduceOuterTransaction:
        """Create the one transaction owner shared by every weekly UI form."""

        nonlocal latest_outer_transaction_receipt
        assert resolved_run_id is not None
        authority_key = _outer_authority_key(surface.payload)
        if authority_key is None:
            raise ValueError("outer transaction has no complete LocalSave authority")
        receipt_authority = surface.payload.get("outer_authority")
        receipt_capture = surface.payload.get("capture")
        owner = TransactionOwner(
            resolved_run_id,
            produce_id,
            week,
            target,
        )
        transaction_id = (
            f"{owner.run_id}:{owner.produce_id}:"
            f"week-{owner.week}:{owner.selected_action}"
        )
        transaction = ProduceOuterTransaction.begin(
            transaction_id,
            owner,
            surfaces,
        ).record_submission(
            f"{submission_kind}:{cycle}:{authority_key[0]}"
        )
        latest_outer_transaction_receipt = {
            "record_id": f"outer-transaction-v2:{transaction_id}:awaiting",
            "recorded_at": outer_receipt_time(),
            "transaction_id": transaction_id,
            "run_id": owner.run_id,
            "produce_id": owner.produce_id,
            "week": owner.week,
            "phase": phase,
            "selected_action": owner.selected_action,
            "legal_action_ids": list(legal_action_ids),
            "candidate_set_complete": candidate_set_complete,
            "target_pid": (
                receipt_capture.get("pid")
                if isinstance(receipt_capture, Mapping)
                and type(receipt_capture.get("pid")) is int
                and receipt_capture.get("pid") > 0
                else None
            ),
            "authority": (
                dict(receipt_authority)
                if isinstance(receipt_authority, Mapping)
                else {}
            ),
            "authority_complete": authority_key is not None,
            "observer_anchor": dict(observer_anchor),
            "observer_anchor_complete": observer_anchor_complete,
            "terminal_receipt_id": None,
            "terminal_kind": "awaiting",
            "reward": None,
            "bc_eligible": False,
            "rl_eligible": False,
            "details": {
                "receipt_phase": "begin",
                "decision_source": (
                    dict(decision_source)
                    if isinstance(decision_source, Mapping)
                    else {}
                ),
            },
        }
        return transaction

    def bind_active_outer_transaction() -> None:
        setter = getattr(type(surface_reader), "set_active_outer_transaction", None)
        if callable(setter):
            if isinstance(surface_reader, InitialRegularLiveSurfaceReader):
                begin_authority = None
                if (
                    active_outer_transaction is not None
                    and isinstance(latest_outer_transaction_receipt, Mapping)
                    and latest_outer_transaction_receipt.get("transaction_id")
                    == active_outer_transaction.transaction_id
                    and isinstance(
                        latest_outer_transaction_receipt.get("authority"),
                        Mapping,
                    )
                ):
                    begin_authority = latest_outer_transaction_receipt["authority"]
                setter(
                    surface_reader,
                    active_outer_transaction,
                    begin_authority,
                )
            else:
                setter(surface_reader, active_outer_transaction)
            return
        # Compatibility for injected/read-only test readers.  Production
        # N.I.A. always consumes the transaction itself and never uses this
        # older action-only hand-off as an owner.
        legacy_setter = getattr(type(surface_reader), "set_pending_weekly_action", None)
        if callable(legacy_setter):
            legacy_setter(
                surface_reader,
                (
                    active_outer_transaction.owner.selected_action
                    if active_outer_transaction is not None
                    else pending_overview_target
                    if pending_overview_owner_authority is not None
                    else None
                ),
                pending_overview_owner_authority,
            )

    def surface_diagnostic(
        surface: InitialRegularSurface,
        previous: str | None,
    ) -> Mapping[str, Any]:
        payload = surface.payload
        diagnostic: dict[str, Any] = {
            "page": surface.page,
            "previous_page": previous,
        }
        for key in ("kind", "target", "reason", "exam_save_path", "reader_errors"):
            value = payload.get(key)
            if isinstance(value, str):
                diagnostic[key] = value
            elif key == "reader_errors" and isinstance(value, Mapping):
                diagnostic[key] = {
                    str(name): str(detail) for name, detail in value.items()
                }
        authority = payload.get("outer_authority")
        if isinstance(authority, Mapping):
            diagnostic["outer_authority"] = dict(authority)
        capture = payload.get("capture")
        if isinstance(capture, Mapping):
            diagnostic["capture"] = {
                key: value
                for key in (
                    "png_path",
                    "timestamp",
                    "hwnd",
                    "pid",
                    "width",
                    "height",
                    "capture_method",
                    "backend",
                )
                if isinstance((value := capture.get(key)), str | int | float | bool)
            }
        return diagnostic

    def publish(page: str | None, cycles: int, stop_reason: str = "") -> None:
        if progress_callback is None:
            return
        recent = None if not steps else steps[-1].to_dict()
        monitor_snapshot = (
            None
            if last_monitor_snapshot is None
            else dict(last_monitor_snapshot)
        )
        if monitor_snapshot is not None and recent is not None:
            # The recent action is already part of the authoritative autopilot
            # journal. Attach it only as display telemetry; never recompute a
            # recommendation in the GUI.
            monitor_snapshot["recent_step"] = recent
        if monitor_snapshot is not None and resolved_run_id is not None:
            monitor_snapshot["source_run_id"] = resolved_run_id
        try:
            payload: dict[str, Any] = {
                "current_page": page or PAGE_UNKNOWN,
                "cycles": cycles,
                "outer_action_count": len(steps),
                "recent_step": recent,
                "stop_reason": stop_reason,
                "monitor_snapshot": monitor_snapshot,
                "source_run_id": resolved_run_id,
            }
            if latest_outer_transaction_receipt is not None:
                payload["outer_transaction_receipt"] = dict(
                    latest_outer_transaction_receipt
                )
            progress_callback(payload)
        except Exception:
            # Display telemetry is observational and must not change gameplay.
            return

    def finish(status: str, reason: str, cycles: int) -> InitialRegularAutopilotResult:
        result = InitialRegularAutopilotResult(
            status=status,
            stop_reason=reason,
            plan_type=plan_type,
            cycles=cycles,
            steps=tuple(steps),
            last_surface=last_surface_diagnostic,
        )
        publish(previous_page, cycles, reason)
        return result

    for cycle in range(1, max_cycles + 1):
        if stop_requested():
            return finish(STATUS_STOPPED, "stop-requested", cycle - 1)
        try:
            bind_active_outer_transaction()
            surface = surface_reader(previous_page)
        except TimeoutError as error:
            # Transport retry belongs to controller_client and is restricted
            # to its explicit read-only command allow-list.  The run loop owns
            # semantic pages only; retrying the whole surface here would mix
            # frames and create a second timeout owner.
            return finish(
                STATUS_HARD_STOP,
                f"surface-reader-failed:{type(error).__name__}:{error}",
                cycle,
            )
        except Exception as error:
            return finish(
                STATUS_HARD_STOP,
                f"surface-reader-failed:{type(error).__name__}:{error}",
                cycle,
            )
        last_surface_diagnostic = surface_diagnostic(surface, previous_page)
        last_monitor_snapshot = _monitor_surface_snapshot(surface)
        publish(surface.page, cycle)

        if surface.page == PAGE_COMPLETED:
            if active_outer_transaction is not None:
                terminal_receipt_id = f"terminal:{cycle}"
                active_outer_transaction = active_outer_transaction.record_terminal(
                    terminal_receipt_id
                )
                terminalize_outer_receipt(
                    active_outer_transaction,
                    receipt_id=terminal_receipt_id,
                    terminal_kind="run_terminal",
                    terminal_surface=surface,
                )
                active_outer_transaction = None
                bind_active_outer_transaction()
            return finish(STATUS_COMPLETED, "final-performance-ended", cycle)
        if surface.page == PAGE_UNKNOWN:
            unknown_reads += 1
            if unknown_reads >= max_unknown_reads:
                return finish(STATUS_HARD_STOP, "surface-unrecognized", cycle)
            sleep(poll_interval_seconds)
            continue
        unknown_reads = 0

        if active_outer_transaction is not None:
            surface_produce_id, surface_week = _nia_surface_owner(surface.payload)
            if surface_produce_id is None:
                surface_produce_id = active_outer_transaction.owner.produce_id
            mirror_successor = (
                surface.page == PAGE_NIA_OUTER
                and surface.payload.get("kind") == "mirror-choice"
            )
            authority = surface.payload.get("outer_authority")
            completed_week = (
                authority.get("last_completed_week")
                if isinstance(authority, Mapping)
                else None
            )
            weekly_event_successor = bool(
                surface.page == PAGE_NIA_OUTER
                and surface.payload.get("kind") == "event-choice"
                and type(completed_week) is int
                and completed_week >= active_outer_transaction.owner.week
            )
            explicit_week_successor = (
                surface.page == PAGE_OVERVIEW
                or mirror_successor
                or weekly_event_successor
            )
            successor_week = surface_week
            terminal_surface = surface
            if (
                (mirror_successor or weekly_event_successor)
                and (
                    surface_week is None
                    or surface_week == active_outer_transaction.owner.week
                )
                and isinstance(authority, Mapping)
                and type(completed_week) is int
                and completed_week >= active_outer_transaction.owner.week
            ):
                # Produce LocalSave retains the completed week's raw marker on
                # the immediately following audition selector or weekly event
                # page.  That typed page proves the old transaction has ended;
                # project only the receipt's successor week while preserving
                # every other native field.
                successor_week = completed_week + 1
                projected_payload = dict(surface.payload)
                projected_authority = dict(authority)
                projected_authority["week"] = successor_week
                projected_payload["outer_authority"] = projected_authority
                terminal_surface = InitialRegularSurface(
                    surface.page,
                    projected_payload,
                )
            if (
                explicit_week_successor
                and surface_produce_id == active_outer_transaction.owner.produce_id
                and successor_week is not None
                and successor_week > active_outer_transaction.owner.week
            ):
                terminal_transaction = active_outer_transaction
                successor_receipt_id = f"week-successor:{cycle}:{successor_week}"
                active_outer_transaction = (
                    active_outer_transaction.record_week_successor(
                        successor_receipt_id,
                        run_id=active_outer_transaction.owner.run_id,
                        produce_id=surface_produce_id,
                        week=successor_week,
                    )
                )
                terminalize_outer_receipt(
                    terminal_transaction,
                    receipt_id=successor_receipt_id,
                    terminal_kind="week_successor",
                    terminal_surface=terminal_surface,
                )
                # A new weekly decision may be submitted later in this same
                # cycle and replace the retained receipt.  Publish this exact
                # terminal row before that overwrite occurs.
                publish(surface.page, cycle)
                active_outer_transaction = None
                bind_active_outer_transaction()

        # A transaction-submitted N.I.A. overview can remain visible while
        # Unity changes pages.  It is display residue, never a second weekly
        # decision.  Subpages (including event-choice) are not folded into
        # this gate because they may be the transaction's legitimate next
        # semantic surface.
        if active_outer_transaction is not None and surface.page == PAGE_OVERVIEW:
            surface_produce_id, surface_week = _nia_surface_owner(surface.payload)
            if surface_produce_id is None:
                surface_produce_id = active_outer_transaction.owner.produce_id
            if (
                surface_produce_id == active_outer_transaction.owner.produce_id
                and surface_week == active_outer_transaction.owner.week
            ):
                pending_overview_reads += 1
                if pending_overview_reads >= max_unknown_reads:
                    return finish(
                        STATUS_HARD_STOP,
                        "overview-transition-timeout",
                        cycle,
                    )
                previous_page = PAGE_OVERVIEW
                sleep(poll_interval_seconds)
                continue

        # Every exam dispatcher owns the whole exam until terminal.  A result
        # screen can become visible a fraction before the durable ExamSave is
        # replaced, so the surface reader may briefly publish the same exam
        # again.  Never re-enter a terminal dispatcher for that unchanged
        # path: doing so could launch a second Maa card loop after completion.
        if pending_terminal_exam_path is not None:
            current_exam_path = surface.payload.get("exam_save_path")
            if (
                surface.page == PAGE_EXAM
                and isinstance(current_exam_path, str)
                and str(Path(current_exam_path).resolve())
                == pending_terminal_exam_path
            ):
                pending_terminal_exam_reads += 1
                if pending_terminal_exam_reads >= max_unknown_reads:
                    return finish(
                        STATUS_HARD_STOP,
                        "exam-terminal-transition-timeout",
                        cycle,
                    )
                previous_page = PAGE_EXAM
                sleep(poll_interval_seconds)
                continue
            pending_terminal_exam_path = None
            pending_terminal_exam_reads = 0

        is_nia_wait = (
            surface.page == PAGE_NIA_OUTER
            and surface.payload.get("kind") == "wait"
        )
        if is_nia_wait:
            wait_authority = _outer_authority_key(surface.payload)
            if wait_authority is None:
                return finish(
                    STATUS_HARD_STOP,
                    "nia-subpage-wait-missing-localsave-authority",
                    cycle,
                )
            raw_wait_target = surface.payload.get("target")
            wait_target = (
                str(raw_wait_target)
                if isinstance(raw_wait_target, str) and raw_wait_target
                else None
            )
            if (
                wait_authority != pending_nia_wait_authority
                or wait_target != pending_nia_wait_target
            ):
                pending_nia_wait_authority = wait_authority
                pending_nia_wait_reads = 0
                pending_nia_wait_target = wait_target
                pending_nia_wait_window = None
            if wait_target in {
                "expected-route-actions",
                "milestone-subpage",
            }:
                wait_window = _capture_window_key(surface.payload)
                if wait_window is None:
                    return finish(
                        STATUS_HARD_STOP,
                        (
                            "nia-exact-route-wait-missing-window-authority"
                            if wait_target == "expected-route-actions"
                            else "nia-milestone-wait-missing-window-authority"
                        ),
                        cycle,
                    )
                if pending_nia_wait_window is None:
                    pending_nia_wait_window = wait_window
                elif wait_window != pending_nia_wait_window:
                    return finish(
                        STATUS_HARD_STOP,
                        (
                            "nia-exact-route-wait-window-changed"
                            if wait_target == "expected-route-actions"
                            else "nia-milestone-wait-window-changed"
                        ),
                        cycle,
                    )
            pending_nia_wait_reads += 1
            if pending_nia_wait_reads >= max_unknown_reads:
                timeout_reason = surface.payload.get("wait_timeout_reason")
                return finish(
                    STATUS_HARD_STOP,
                    (
                        str(timeout_reason)
                        if timeout_reason
                        in {
                            "expected-route-action-not-visible",
                            "nia-milestone-surface-not-visible",
                        }
                        else "nia-subpage-wait-timeout"
                    ),
                    cycle,
                )
        else:
            if (
                pending_nia_wait_target == "expected-route-actions"
                and surface.page == PAGE_OVERVIEW
                and _outer_authority_key(surface.payload)
                == pending_nia_wait_authority
            ):
                ready_window = _capture_window_key(surface.payload)
                if ready_window is None:
                    return finish(
                        STATUS_HARD_STOP,
                        "nia-exact-route-ready-missing-window-authority",
                        cycle,
                    )
                if ready_window != pending_nia_wait_window:
                    return finish(
                        STATUS_HARD_STOP,
                        "nia-exact-route-ready-window-changed",
                        cycle,
                    )
            pending_nia_wait_authority = None
            pending_nia_wait_reads = 0
            pending_nia_wait_target = None
            pending_nia_wait_window = None

        is_nia_click1 = (
            surface.page == PAGE_NIA_OUTER
            and surface.payload.get("kind") == "click-1-fallback"
        )
        if is_nia_click1:
            click1_authority = _outer_authority_key(surface.payload)
            if click1_authority is None:
                return finish(
                    STATUS_HARD_STOP,
                    "nia-click-1-missing-localsave-authority",
                    cycle,
                )
            if click1_authority != pending_nia_click1_authority:
                pending_nia_click1_authority = click1_authority
                pending_nia_click1_inputs = 0
                pending_nia_click1_reads = 0
            pending_nia_click1_reads += 1
            if pending_nia_click1_reads >= max_unknown_reads:
                return finish(
                    STATUS_HARD_STOP,
                    "nia-click-1-transition-timeout",
                    cycle,
                )
            # Maa's original Click_1 is a zero-delay top-left story advance.
            # Repeating it is required for consecutive dialogue pages, but it
            # must not become a 2 Hz input storm while one page is animating.
            # Poll every 0.5 s and submit only on reads 1, 3, 5, ... .
            if (
                pending_nia_click1_inputs > 0
                and (pending_nia_click1_reads - 1)
                % NIA_CLICK1_RETRY_EVERY_READS
                != 0
            ):
                previous_page = PAGE_NIA_OUTER
                sleep(poll_interval_seconds)
                continue
        else:
            pending_nia_click1_authority = None
            pending_nia_click1_inputs = 0
            pending_nia_click1_reads = 0

        # Weekly overview actions are one Produce transaction in every mode.
        # During the game's transition, the previous overview (or N.I.A.'s
        # equivalent event-choice representation) can remain visible while
        # Produce LocalSave is still unchanged.  Never submit that same
        # semantic weekly choice twice.
        # A real subpage (work/card/reward/continue/etc.) is allowed through
        # even before LocalSave advances.  ``log_count`` may change while the
        # same subpage is settling, so only a changed semantic owner key starts
        # the next weekly decision normally.
        if pending_overview_authority is not None:
            current_authority = _outer_authority_key(surface.payload)
            current_owner_authority = _weekly_owner_semantic_key(surface.payload)
            owner_authority_unchanged = (
                current_authority is not None
                and pending_overview_owner_authority is not None
                and current_owner_authority == pending_overview_owner_authority
            )
            if owner_authority_unchanged:
                # Keep the complete authority current for duplicate/transition
                # gates and telemetry while the semantic owner remains bound.
                pending_overview_authority = current_authority
            is_stale_choice_surface = surface.page == PAGE_OVERVIEW or (
                surface.page == PAGE_NIA_OUTER
                and surface.payload.get("kind") == "event-choice"
            )
            surface_target = surface.payload.get("target")
            expected_followup = (
                None
                if pending_overview_target is None
                else f"{pending_overview_target}-confirm"
            )
            is_owned_followup = (
                surface.page == PAGE_NIA_OUTER
                and isinstance(surface_target, str)
                and surface_target == expected_followup
                and current_authority == pending_overview_authority
            )
            # A generic cancel template can be visible on top of a pending
            # rest transaction.  It may never cancel and reopen that same
            # weekly choice; wait for the typed affirmative modal instead.
            is_conflicting_cancel = (
                surface.page == PAGE_NIA_OUTER
                and pending_overview_target == "rest"
                and surface_target == "cancel"
                and current_authority == pending_overview_authority
            )
            if (
                (
                    is_stale_choice_surface
                    or is_conflicting_cancel
                    or (
                        is_owned_followup
                        and pending_overview_followup_target == surface_target
                    )
                )
                and current_authority == pending_overview_authority
            ):
                pending_overview_reads += 1
                if pending_overview_reads >= max_unknown_reads:
                    return finish(
                        STATUS_HARD_STOP,
                        "overview-transition-timeout",
                        cycle,
                    )
                previous_page = PAGE_OVERVIEW
                sleep(poll_interval_seconds)
                continue
            if not owner_authority_unchanged:
                pending_overview_authority = None
                pending_overview_owner_authority = None
                pending_overview_reads = 0
                pending_overview_target = None
                pending_overview_followup_target = None

        try:
            snapshot: ProduceOuterLocalSaveSnapshot | None = None
            if run_context_bootstrapper is not None:
                snapshot = snapshot_reader()
                binding_before_bootstrap = resolved_run_id
                try:
                    run_context = run_context_bootstrapper(snapshot, surface)
                    if isinstance(run_context, Mapping):
                        run_id = run_context.get("run_id")
                        if isinstance(run_id, str) and run_id.strip():
                            run_id = run_id.strip()
                            if (
                                resolved_run_id is not None
                                and run_id != resolved_run_id
                            ):
                                raise ValueError(
                                    "run context changed during unattended cultivation"
                                )
                            resolved_run_id = run_id
                except ValueError as error:
                    if (
                        surface.page in {PAGE_OVERVIEW, PAGE_TRAINING}
                        and str(error)
                        == "overview week differs from outer LocalSave"
                    ):
                        alignment_authority = _outer_authority_key(surface.payload)
                        if alignment_authority is None:
                            raise
                        if (
                            alignment_authority
                            != pending_overview_alignment_authority
                        ):
                            pending_overview_alignment_authority = alignment_authority
                            pending_overview_alignment_reads = 0
                        pending_overview_alignment_reads += 1
                        if pending_overview_alignment_reads >= max_unknown_reads:
                            return finish(
                                STATUS_HARD_STOP,
                                "overview-localsave-alignment-timeout",
                                cycle,
                            )
                        previous_page = surface.page
                        publish(previous_page, cycle)
                        sleep(poll_interval_seconds)
                        continue
                    raise
                pending_overview_alignment_authority = None
                pending_overview_alignment_reads = 0
                if binding_before_bootstrap is None and resolved_run_id is not None:
                    # A fresh cultivation starts intentionally unbound.  As
                    # soon as LocalSave resolves its durable identity, publish
                    # the already-decoded surface once before any Exam action
                    # can dispatch.  The GUI uses this event to bind its
                    # blocking transition watcher; no second read is made.
                    publish(surface.page, cycle)
            if surface.page in {PAGE_OVERVIEW, PAGE_TRAINING}:
                if surface.page == PAGE_OVERVIEW:
                    overview_output = surface.payload
                    training_output = None
                else:
                    if overview_output is None:
                        reconstructed = surface.payload.get("overview_context")
                        if not isinstance(reconstructed, Mapping):
                            return finish(
                                STATUS_HARD_STOP,
                                "training-without-current-overview-evidence",
                                cycle,
                            )
                        overview_output = dict(reconstructed)
                    training_output = surface.payload
                advice_snapshot = (
                    snapshot if snapshot is not None else snapshot_reader()
                )
                advice = outer_advisor(
                    advice_snapshot,
                    overview_output=overview_output,
                    training_output=training_output,
                )
                if advice.status != OUTER_READY:
                    return finish(
                        STATUS_HARD_STOP,
                        f"outer-advice-unavailable:{advice.reason}",
                        cycle,
                    )
                if surface.page == PAGE_OVERVIEW:
                    action, target = _overview_action(surface.payload, advice)
                else:
                    action, target = _training_action(surface.payload, advice)
                route = surface.payload.get("route")
                route_produce_id = (
                    route.get("produce_id") if isinstance(route, Mapping) else None
                )
                authority = surface.payload.get("outer_authority")
                authority_week = (
                    authority.get("week")
                    if isinstance(authority, Mapping)
                    else None
                )
                advice_week = getattr(advice, "current_week", None)
                selected_week = (
                    advice_week if type(advice_week) is int else authority_week
                )
                is_nia_overview = (
                    surface.page == PAGE_OVERVIEW
                    and route_produce_id in {"produce-004", "produce-005"}
                )
                compiled_nia_chain = None
                if is_nia_overview:
                    if resolved_run_id is None or type(selected_week) is not int:
                        return finish(
                            STATUS_HARD_STOP,
                            "nia-transaction-owner-unavailable",
                            cycle,
                        )
                    from .nia_selected_action_chain import (
                        SelectedAction,
                        compile_selected_action_chain,
                    )

                    compiled = compile_selected_action_chain(SelectedAction(target))
                    if not compiled.ready or compiled.chain is None:
                        issue_codes = ",".join(issue.code for issue in compiled.issues)
                        return finish(
                            STATUS_HARD_STOP,
                            f"nia-selected-action-chain-unavailable:{issue_codes}",
                            cycle,
                        )
                    compiled_nia_chain = compiled.chain
                begin_observer_anchor: Mapping[str, Any] = {}
                begin_observer_anchor_complete = False
                if is_nia_overview:
                    (
                        begin_observer_anchor,
                        begin_observer_anchor_complete,
                    ) = _capture_outer_observer_anchor(
                        outer_observer_anchor_provider,
                        expected_generation=0,
                        minimum_next=0,
                    )
                if action.click_count == 2:
                    executed = choice_executor(
                        action,
                        page=surface.page,
                        target=target,
                        payload=surface.payload,
                    )
                else:
                    executed = single_click_executor(action)
                decision_outcome = dict(_outcome_mapping(executed))
                raw_options = surface.payload.get("options")
                raw_eligible_actions = getattr(advice, "eligible_actions", None)
                eligible_action_ids = (
                    {
                        value
                        for value in raw_eligible_actions
                        if isinstance(value, str)
                    }
                    if isinstance(raw_eligible_actions, (list, tuple))
                    else {
                        str(value.get("action"))
                        for value in raw_options
                        if isinstance(value, Mapping)
                        and isinstance(value.get("action"), str)
                    }
                    if isinstance(raw_options, list)
                    else {target}
                )
                candidate_rows = (
                    [
                        dict(value)
                        for value in raw_options
                        if isinstance(value, Mapping)
                        and value.get("action") in eligible_action_ids
                    ]
                    if isinstance(raw_options, list)
                    else []
                )
                candidate_ids = tuple(
                    str(value["action"])
                    for value in candidate_rows
                    if isinstance(value.get("action"), str)
                )
                runtime_legal_actions = (
                    tuple(
                        value
                        for value in raw_eligible_actions
                        if isinstance(value, str) and value in candidate_ids
                    )
                    if isinstance(raw_eligible_actions, (list, tuple))
                    else candidate_ids
                )
                advice_week = selected_week
                advice_produce_id = route_produce_id
                advice_phase = (
                    nia_phase_for_week(advice_produce_id, advice_week)
                    if advice_produce_id in {"produce-004", "produce-005"}
                    and type(advice_week) is int
                    else None
                )
                decision_evidence = {
                    "kind": "outer-action",
                    "chosen": target,
                    "route_week": advice_week,
                    "route_phase": advice_phase,
                    "candidate_set_complete": (
                        len(candidate_ids) == len(set(candidate_ids))
                        and set(candidate_ids) == eligible_action_ids
                    ),
                    "candidates": candidate_rows,
                }
                telemetry_source = getattr(
                    outer_advisor, "_nia_telemetry_source", None
                )
                if callable(telemetry_source):
                    source = telemetry_source(advice, runtime_legal_actions)
                    if isinstance(source, Mapping):
                        decision_evidence["source"] = dict(source)
                else:
                    # Custom/test advisors do not necessarily expose a prior.
                    # Keep their runtime reason and legal order auditable while
                    # leaving prior-specific fields absent rather than guessed.
                    decision_evidence["source"] = {
                        "advisor_reason": str(getattr(advice, "reason", "")),
                        "advisor_action": getattr(advice, "action", None),
                        "runtime_legal_actions": list(runtime_legal_actions),
                    }
                shadow_provider = getattr(
                    outer_advisor, "_nia_outer_bc_shadow", None
                )
                if callable(shadow_provider):
                    from .outer_bc_shadow import attach_outer_bc_shadow_evidence

                    decision_evidence = attach_outer_bc_shadow_evidence(
                        decision_evidence,
                        provider=shadow_provider,
                        advice=advice,
                        snapshot=advice_snapshot,
                        candidate_ids=runtime_legal_actions,
                        formal_action=target,
                    )
                decision_outcome["decision_evidence"] = decision_evidence
                steps.append(
                    InitialRegularAutopilotStep(
                        cycle,
                        surface.page,
                        "choose",
                        target,
                        decision_outcome,
                    )
                )
                if surface.page == PAGE_OVERVIEW:
                    authority_key = _outer_authority_key(surface.payload)
                    if authority_key is None:
                        raise ValueError(
                            "overview action has no complete LocalSave authority"
                        )
                    submitted = _input_was_submitted(decision_outcome)
                    if (
                        is_nia_overview
                        and compiled_nia_chain is not None
                        and submitted
                    ):
                        assert isinstance(route_produce_id, str)
                        assert type(selected_week) is int
                        receipt_source = decision_evidence.get("source")
                        active_outer_transaction = begin_submitted_outer_transaction(
                            produce_id=route_produce_id,
                            week=selected_week,
                            target=target,
                            surfaces=compiled_nia_chain.surfaces,
                            cycle=cycle,
                            surface=surface,
                            decision_source=(
                                receipt_source
                                if isinstance(receipt_source, Mapping)
                                else None
                            ),
                            legal_action_ids=runtime_legal_actions,
                            candidate_set_complete=(
                                decision_evidence.get("candidate_set_complete")
                                is True
                            ),
                            phase=advice_phase,
                            observer_anchor=begin_observer_anchor,
                            observer_anchor_complete=begin_observer_anchor_complete,
                            submission_kind="overview-submit",
                        )
                        pending_overview_authority = None
                        pending_overview_owner_authority = None
                        pending_overview_reads = 0
                        pending_overview_target = None
                        pending_overview_followup_target = None
                        bind_active_outer_transaction()
                    elif not is_nia_overview:
                        # Initial modes retain their established semantic
                        # overview owner. N.I.A. never falls back to this
                        # second owner path.
                        pending_overview_authority = authority_key
                        pending_overview_owner_authority = (
                            _weekly_owner_semantic_key(surface.payload)
                        )
                        if pending_overview_owner_authority is None:
                            raise ValueError(
                                "overview action has no semantic owner authority"
                            )
                        pending_overview_reads = 0
                        pending_overview_target = target
                        pending_overview_followup_target = None

            elif surface.page == PAGE_EXAM:
                path_value = surface.payload.get("exam_save_path")
                if not isinstance(path_value, str) or not path_value:
                    raise ValueError("exam surface has no LocalSave path")
                dispatched = dict(exam_dispatcher(plan_type, Path(path_value)))
                steps.append(
                    InitialRegularAutopilotStep(
                        cycle, PAGE_EXAM, "dispatch", plan_type, dispatched
                    )
                )
                if not bool(dispatched.get("accepted")):
                    return finish(
                        STATUS_HARD_STOP,
                        str(dispatched.get("reason", "exam-dispatch-rejected")),
                        cycle,
                    )
                if bool(dispatched.get("terminal")):
                    pending_terminal_exam_path = str(Path(path_value).resolve())
                    pending_terminal_exam_reads = 0
                action_count = dispatched.get("actions_executed", 0)
                if type(action_count) is int and action_count > 0:
                    exam_actions_dispatched += action_count
                if (
                    exam_action_budget is not None
                    and exam_actions_dispatched >= exam_action_budget
                ):
                    return finish(
                        STATUS_HARD_STOP,
                        "exam-action-budget-reached",
                        cycle,
                    )

            elif surface.page == PAGE_NIA_OUTER:
                if surface.payload.get("kind") == "wait":
                    # Exact-route and milestone waits are produced by the
                    # overview reader after the named N.I.A. router found no
                    # complete candidate set.  Preserve overview provenance
                    # so the next read invokes Maa's ``all`` scope: mirror
                    # audition nodes can appear there while ``subpage`` omits
                    # them.  An unchanged empty batch still returns this
                    # typed wait before the generic Click_1 fallback, so this
                    # changes recognition scope without authorizing input.
                    previous_page = (
                        PAGE_OVERVIEW
                        if surface.payload.get("target")
                        in {
                            "expected-route-actions",
                            "milestone-subpage",
                        }
                        else PAGE_NIA_OUTER
                    )
                    publish(previous_page, cycle)
                    sleep(poll_interval_seconds)
                    continue
                if surface.payload.get("kind") == "active-produce-resume":
                    executed = dict(nia_active_resume_executor(surface.payload))
                    if (
                        executed.get("accepted") is not True
                        or executed.get("input_submitted") is not True
                    ):
                        raise RuntimeError("active Produce resume executor did not accept")
                    steps.append(
                        InitialRegularAutopilotStep(
                            cycle,
                            PAGE_NIA_OUTER,
                            "resume",
                            "same-run-home",
                            _outcome_mapping(executed),
                        )
                    )
                    # The dedicated Maa chain ends at ProduceEntryFlag.  Rebase
                    # the next read from overview provenance so named N.I.A.
                    # routes get first refusal; never replay the Home action.
                    previous_page = PAGE_OVERVIEW
                    publish(previous_page, cycle)
                    sleep(poll_interval_seconds)
                    continue
                if surface.payload.get("kind") == "reward-reroll-blocked":
                    blocker = surface.payload.get("blocker")
                    code = (
                        blocker.get("code")
                        if isinstance(blocker, Mapping)
                        else None
                    )
                    reason = (
                        str(code)
                        if isinstance(code, str) and code
                        else "reward-reroll-blocker-malformed"
                    )
                    steps.append(
                        InitialRegularAutopilotStep(
                            cycle,
                            PAGE_NIA_OUTER,
                            "blocked",
                            "reward-reroll",
                            {
                                "input_submitted": False,
                                "blocker": (
                                    dict(blocker)
                                    if isinstance(blocker, Mapping)
                                    else None
                                ),
                            },
                        )
                    )
                    return finish(STATUS_HARD_STOP, reason, cycle)
                weekly_event_transaction: _NiaWeeklyEventSubmission | None = None
                if (
                    surface.payload.get("kind") == "event-choice"
                    and active_outer_transaction is None
                ):
                    authority_key = _outer_authority_key(surface.payload)
                    route_produce_id, authority_week = _nia_surface_owner(
                        surface.payload
                    )
                    authority = surface.payload.get("outer_authority")
                    evidence = surface.payload.get("evidence")
                    target = surface.payload.get("target")
                    completed_week = (
                        authority.get("last_completed_week")
                        if isinstance(authority, Mapping)
                        else None
                    )
                    visible_actions = (
                        tuple(
                            value
                            for value in evidence.get("visible_actions", ())
                            if isinstance(value, str) and value
                        )
                        if isinstance(evidence, Mapping)
                        else ()
                    )
                    if (
                        resolved_run_id is None
                        or route_produce_id not in {"produce-004", "produce-005"}
                        or authority_key is None
                        or type(authority_week) is not int
                        or not isinstance(target, str)
                        or not target
                    ):
                        return finish(
                            STATUS_HARD_STOP,
                            "nia-event-choice-transaction-owner-unavailable",
                            cycle,
                        )
                    owner_week = (
                        completed_week + 1
                        if type(completed_week) is int
                        and completed_week >= authority_week
                        else authority_week
                    )
                    from .nia_selected_action_chain import (
                        SelectedAction,
                        compile_selected_action_chain,
                    )

                    compiled = compile_selected_action_chain(
                        SelectedAction(target)
                    )
                    if not compiled.ready or compiled.chain is None:
                        issue_codes = ",".join(
                            issue.code for issue in compiled.issues
                        )
                        return finish(
                            STATUS_HARD_STOP,
                            "nia-selected-action-chain-unavailable:"
                            + issue_codes,
                            cycle,
                        )
                    begin_anchor, begin_anchor_complete = (
                        _capture_outer_observer_anchor(
                            outer_observer_anchor_provider,
                            expected_generation=0,
                            minimum_next=0,
                        )
                    )
                    weekly_event_transaction = _NiaWeeklyEventSubmission(
                        produce_id=route_produce_id,
                        week=owner_week,
                        target=target,
                        surfaces=compiled.chain.surfaces,
                        legal_action_ids=visible_actions,
                        candidate_set_complete=bool(
                            visible_actions
                            and len(visible_actions) == len(set(visible_actions))
                            and target in visible_actions
                        ),
                        phase=nia_phase_for_week(
                            route_produce_id,
                            owner_week,
                        ),
                        observer_anchor=begin_anchor,
                        observer_anchor_complete=begin_anchor_complete,
                        decision_source=(
                            dict(evidence)
                            if isinstance(evidence, Mapping)
                            else {}
                        ),
                    )
                action_payload = surface.payload.get("action")
                if not isinstance(action_payload, Mapping):
                    raise ValueError("N.I.A. subpage has no typed MAA action")
                if action_payload.get("action_type") == "swipe":
                    action = SuggestedSwipe.from_dict(action_payload)
                    executed = dict(
                        execute_nia_outer_subpage_swipe(
                            action,
                            payload=surface.payload,
                        )
                    )
                else:
                    action = SuggestedClick.from_dict(action_payload)
                    executed = dict(
                        execute_nia_outer_subpage_click(
                            action,
                            payload=surface.payload,
                        )
                    )
                surface_evidence = surface.payload.get("evidence")
                if isinstance(surface_evidence, Mapping):
                    raw_candidates = surface_evidence.get("offers")
                    candidates = (
                        [dict(value) for value in raw_candidates if isinstance(value, Mapping)]
                        if isinstance(raw_candidates, list)
                        else None
                    )
                    executed["decision_evidence"] = {
                        "kind": str(surface.payload.get("kind", "continue")),
                        "chosen": surface.payload.get("target"),
                        "candidate_set_complete": bool(
                            candidates
                            and len(candidates) == len(raw_candidates)
                            and surface.payload.get("kind") == "reward-confirm"
                        ),
                        "candidates": candidates,
                        "source": dict(surface_evidence),
                    }
                if weekly_event_transaction is not None:
                    executed["decision_evidence"] = {
                        "kind": "outer-action",
                        "chosen": weekly_event_transaction.target,
                        "route_week": weekly_event_transaction.week,
                        "route_phase": weekly_event_transaction.phase,
                        "candidate_set_complete": (
                            weekly_event_transaction.candidate_set_complete
                        ),
                        "candidates": [
                            {"action": value}
                            for value in weekly_event_transaction.legal_action_ids
                        ],
                        "source": dict(weekly_event_transaction.decision_source),
                    }
                if (
                    surface.payload.get("kind") == "reward-confirm"
                    and reward_checkpointer is not None
                ):
                    evidence_payload = surface.payload.get("evidence")
                    offers = (
                        evidence_payload.get("offers")
                        if isinstance(evidence_payload, Mapping)
                        else None
                    )
                    target_card_id = surface.payload.get("target")
                    matches = (
                        tuple(
                            offer
                            for offer in offers
                            if isinstance(offer, Mapping)
                            and offer.get("card_id") == target_card_id
                        )
                        if isinstance(offers, list)
                        else ()
                    )
                    click_payload = executed.get("click")
                    post_capture = (
                        click_payload.get("post_capture")
                        if isinstance(click_payload, Mapping)
                        else None
                    )
                    if len(matches) == 1 and isinstance(post_capture, Mapping):
                        offer = matches[0]
                        try:
                            executed["checkpoint"] = dict(
                                reward_checkpointer(
                                    card_id=str(offer["card_id"]),
                                    upgrade=int(offer["upgrade"]),
                                    display_name=str(offer["display_name"]),
                                    confidence=float(offer["art_score"]),
                                    capture=post_capture,
                                )
                            )
                        except (RuntimeError, TypeError, ValueError) as error:
                            executed["checkpoint"] = {
                                "accepted": False,
                                "reason": f"{type(error).__name__}: {error}",
                            }
                    else:
                        executed["checkpoint"] = {
                            "accepted": False,
                            "reason": "N.I.A. reward confirmation lacks one resolved offer/capture",
                        }
                    checkpoint = executed.get("checkpoint")
                    if (
                        isinstance(checkpoint, Mapping)
                        and checkpoint.get("accepted") is False
                    ):
                        executed["checkpoint_warning"] = (
                            "non-blocking-reward-observation"
                        )
                current_authority = _outer_authority_key(surface.payload)
                surface_target = surface.payload.get("target")
                if (
                    pending_overview_authority is not None
                    and current_authority == pending_overview_authority
                    and pending_overview_target is not None
                    and surface_target == f"{pending_overview_target}-confirm"
                    and not bool(executed.get("skipped"))
                ):
                    pending_overview_followup_target = str(surface_target)
                    pending_overview_reads = 0
                if (
                    surface.payload.get("kind") == "click-1-fallback"
                    and not bool(executed.get("skipped"))
                ):
                    pending_nia_click1_inputs += 1
                steps.append(
                    InitialRegularAutopilotStep(
                        cycle,
                        PAGE_NIA_OUTER,
                        str(surface.payload.get("kind", "continue")),
                        str(surface.payload.get("target", action.label)),
                        _outcome_mapping(executed),
                    )
                )
                if (
                    weekly_event_transaction is not None
                    and _input_was_submitted(executed)
                ):
                    active_outer_transaction = begin_submitted_outer_transaction(
                        produce_id=weekly_event_transaction.produce_id,
                        week=weekly_event_transaction.week,
                        target=weekly_event_transaction.target,
                        surfaces=weekly_event_transaction.surfaces,
                        cycle=cycle,
                        surface=surface,
                        decision_source=weekly_event_transaction.decision_source,
                        legal_action_ids=weekly_event_transaction.legal_action_ids,
                        candidate_set_complete=(
                            weekly_event_transaction.candidate_set_complete
                        ),
                        phase=weekly_event_transaction.phase,
                        observer_anchor=weekly_event_transaction.observer_anchor,
                        observer_anchor_complete=(
                            weekly_event_transaction.observer_anchor_complete
                        ),
                        submission_kind="event-choice-submit",
                    )
                    bind_active_outer_transaction()
                if (
                    surface.payload.get("kind") == "audition-failure-end"
                    and not bool(executed.get("skipped"))
                ):
                    from .live_source import (
                        execute_initial_regular_nia_early_end_advance,
                    )

                    ended = execute_initial_regular_nia_early_end_advance(
                        surface.payload
                    )
                    steps.append(
                        InitialRegularAutopilotStep(
                            cycle,
                            PAGE_POST_LIVE,
                            "advance",
                            str(
                                ended.get(
                                    "action",
                                    "maa-nia-audition-failure-produce-end",
                                )
                            ),
                            _outcome_mapping(ended),
                        )
                    )
                    previous_page = PAGE_POST_LIVE
                    publish(previous_page, cycle)
                    sleep(poll_interval_seconds)
                    continue

            elif surface.page == PAGE_RESULT:
                mode = str(surface.payload.get("mode", ""))
                checkpoint: Mapping[str, Any] | None = None
                if mode == "lesson" and lesson_result_checkpointer is not None:
                    # The page reader and Maa action own the visible result
                    # transition.  This checkpoint only enriches the run
                    # shadow with result-screen OCR; it must not veto a page
                    # that was already classified, especially when the
                    # server-owned Produce LocalSave will be read again after
                    # the click.  Keep failures as diagnostics instead of
                    # turning optional OCR into a second input gate.
                    try:
                        checkpoint = dict(
                            lesson_result_checkpointer(
                                _capture_mapping(surface.payload)
                            )
                        )
                    except (RuntimeError, TypeError, ValueError) as error:
                        checkpoint = {
                            "accepted": False,
                            "reason": f"{type(error).__name__}: {error}",
                        }
                action = SuggestedClick.from_dict(surface.payload["action"])
                executed = single_click_executor(action)
                outcome = dict(_outcome_mapping(executed))
                if checkpoint is not None:
                    outcome["lesson_result_checkpoint"] = dict(checkpoint)
                    if checkpoint.get("accepted") is False:
                        outcome["checkpoint_warning"] = (
                            "non-blocking-result-observation"
                        )
                steps.append(
                    InitialRegularAutopilotStep(
                        cycle,
                        PAGE_RESULT,
                        "continue",
                        mode or None,
                        outcome,
                    )
                )

            elif surface.page == PAGE_POST_AUDITION_DIALOGUE:
                action = SuggestedClick.from_dict(surface.payload["action"])
                executed = single_click_executor(action)
                steps.append(
                    InitialRegularAutopilotStep(
                        cycle,
                        PAGE_POST_AUDITION_DIALOGUE,
                        "continue",
                        "post-final-dialogue",
                        _outcome_mapping(executed),
                    )
                )

            elif surface.page == PAGE_FINAL_LIVE_START:
                from .live_source import execute_initial_regular_final_live_start

                executed = execute_initial_regular_final_live_start(surface.payload)
                steps.append(
                    InitialRegularAutopilotStep(
                        cycle,
                        PAGE_FINAL_LIVE_START,
                        "start",
                        "final-live",
                        _outcome_mapping(executed),
                    )
                )

            elif surface.page == PAGE_FINAL_LIVE_PLAYING:
                # The performance advances on its own.  Do not turn its
                # animated frames into blind taps; re-read LocalSave/orientation.
                previous_page = PAGE_FINAL_LIVE_PLAYING
                publish(previous_page, cycle)
                sleep(poll_interval_seconds)
                continue

            elif surface.page == PAGE_POST_LIVE:
                from .live_source import execute_initial_regular_post_live_advance

                executed = execute_initial_regular_post_live_advance(surface.payload)
                steps.append(
                    InitialRegularAutopilotStep(
                        cycle,
                        PAGE_POST_LIVE,
                        "advance",
                        str(executed.get("action", "maa-produce-end")),
                        _outcome_mapping(executed),
                    )
                )

            elif surface.page == PAGE_ACTIVITY_REWARD:
                checkpoint: Mapping[str, Any] = {
                    "accepted": False,
                    "reason": "activity-reward-checkpointer-unavailable",
                }
                if activity_reward_checkpointer is not None:
                    try:
                        checkpoint = dict(
                            activity_reward_checkpointer(surface.payload)
                        )
                    except (RuntimeError, TypeError, ValueError) as error:
                        checkpoint = {
                            "accepted": False,
                            "reason": f"{type(error).__name__}: {error}",
                        }
                action_payload = surface.payload.get("action")
                if not isinstance(action_payload, Mapping):
                    state = surface.payload.get("state")
                    target = (
                        str(state.get("item_name"))
                        if isinstance(state, Mapping) and state.get("item_name")
                        else None
                    )
                    steps.append(
                        InitialRegularAutopilotStep(
                            cycle,
                            PAGE_ACTIVITY_REWARD,
                            "checkpoint",
                            target,
                            {"checkpoint": checkpoint},
                        )
                    )
                    return finish(
                        STATUS_HARD_STOP,
                        str(
                            surface.payload.get(
                                "action_blocker",
                                "activity-reward-continue-unproven",
                            )
                        ),
                        cycle,
                    )
                action = SuggestedClick.from_dict(action_payload)
                executed = single_click_executor(action)
                click_outcome = dict(_outcome_mapping(executed))
                if checkpoint.get("accepted") is False:
                    click_outcome["checkpoint_warning"] = (
                        "non-blocking-reward-observation"
                    )
                steps.append(
                    InitialRegularAutopilotStep(
                        cycle,
                        PAGE_ACTIVITY_REWARD,
                        "continue",
                        action.label,
                        {
                            "checkpoint": checkpoint,
                            "click": click_outcome,
                        },
                    )
                )

            elif surface.page == PAGE_TRAINING_REWARD:
                checkpoint = {
                    "accepted": False,
                    "reason": "training-reward-checkpointer-unavailable",
                }
                if training_reward_checkpointer is not None:
                    try:
                        checkpoint = dict(
                            training_reward_checkpointer(surface.payload)
                        )
                    except (RuntimeError, TypeError, ValueError) as error:
                        checkpoint = {
                            "accepted": False,
                            "reason": f"{type(error).__name__}: {error}",
                        }
                action_payload = surface.payload.get("action")
                if not isinstance(action_payload, Mapping):
                    return finish(
                        STATUS_HARD_STOP,
                        "training-reward-receive-action-unavailable",
                        cycle,
                    )
                action = SuggestedClick.from_dict(action_payload)
                executed = single_click_executor(action)
                click_outcome = dict(_outcome_mapping(executed))
                if checkpoint.get("accepted") is False:
                    click_outcome["checkpoint_warning"] = (
                        "non-blocking-reward-observation"
                    )
                steps.append(
                    InitialRegularAutopilotStep(
                        cycle,
                        PAGE_TRAINING_REWARD,
                        "receive",
                        str(surface.payload.get("state", {}).get("item_name", ""))
                        if isinstance(surface.payload.get("state"), Mapping)
                        else None,
                        {
                            "checkpoint": checkpoint,
                            "click": click_outcome,
                        },
                    )
                )

            elif surface.page == PAGE_PASSIVE_NOTIFICATION:
                action = SuggestedClick.from_dict(surface.payload["action"])
                executed = single_click_executor(action)
                state = surface.payload.get("state")
                target = None
                if isinstance(state, Mapping):
                    target = (
                        f"{state.get('attribute')}+{state.get('delta')}"
                    )
                steps.append(
                    InitialRegularAutopilotStep(
                        cycle,
                        PAGE_PASSIVE_NOTIFICATION,
                        "dismiss",
                        target,
                        _outcome_mapping(executed),
                    )
                )

            elif surface.page == PAGE_CARD_ACQUIRE_NOTIFICATION:
                # This is an informational handoff between a settled card
                # acquisition and the next reward page.  It owns one tap;
                # the following loop must capture the fresh three-card view.
                action = SuggestedClick.from_dict(surface.payload["action"])
                executed = single_click_executor(action)
                steps.append(
                    InitialRegularAutopilotStep(
                        cycle,
                        PAGE_CARD_ACQUIRE_NOTIFICATION,
                        "continue",
                        "card-acquire-notification",
                        _outcome_mapping(executed),
                    )
                )

            elif surface.page == PAGE_CARD_REWARD_DISPLAY:
                action = SuggestedClick.from_dict(surface.payload["action"])
                executed = single_click_executor(action)
                state = surface.payload.get("state")
                target = (
                    str(state.get("card_id"))
                    if isinstance(state, Mapping) and state.get("card_id")
                    else None
                )
                steps.append(
                    InitialRegularAutopilotStep(
                        cycle,
                        PAGE_CARD_REWARD_DISPLAY,
                        "dismiss",
                        target,
                        _outcome_mapping(executed),
                    )
                )

            elif surface.page == PAGE_REWARD:
                stage = str(surface.payload.get("stage", ""))
                if stage not in {"preview", "select", "confirm"}:
                    raise ValueError("reward stage must be preview, select or confirm")
                offer = None if stage == "preview" else _reward_offer(surface.payload)
                action = SuggestedClick.from_dict(surface.payload["action"])
                executed = single_click_executor(action)
                outcome = dict(_outcome_mapping(executed))
                if stage == "confirm" and reward_checkpointer is not None:
                    post_capture = getattr(executed, "post_capture", None)
                    if not isinstance(post_capture, Mapping):
                        outcome["checkpoint"] = {
                            "accepted": False,
                            "reason": "reward confirmation has no post-capture evidence",
                        }
                        outcome["checkpoint_warning"] = (
                            "non-blocking-reward-observation"
                        )
                    else:
                        try:
                            outcome["checkpoint"] = dict(
                                reward_checkpointer(
                                    card_id=str(offer["card_id"]),  # type: ignore[index]
                                    upgrade=int(offer["upgrade"]),  # type: ignore[index]
                                    display_name=str(offer["display_name"]),  # type: ignore[index]
                                    confidence=float(offer["art_score"]),  # type: ignore[index]
                                    capture=post_capture,
                                )
                            )
                        except (RuntimeError, TypeError, ValueError) as error:
                            outcome["checkpoint"] = {
                                "accepted": False,
                                "reason": f"{type(error).__name__}: {error}",
                            }
                        checkpoint_value = outcome.get("checkpoint")
                        if (
                            isinstance(checkpoint_value, Mapping)
                            and checkpoint_value.get("accepted") is False
                        ):
                            outcome["checkpoint_warning"] = (
                                "non-blocking-reward-observation"
                            )
                target = (
                    f"slot-{int(surface.payload.get('target_slot', 0))}"
                    if stage == "preview"
                    else str(offer["card_id"])  # type: ignore[index]
                )
                steps.append(
                    InitialRegularAutopilotStep(
                        cycle,
                        PAGE_REWARD,
                        stage,
                        target,
                        outcome,
                    )
                )
            else:
                return finish(
                    STATUS_HARD_STOP,
                    f"unsupported-surface:{surface.page}",
                    cycle,
                )
        except StaleSuggestionError as error:
            detail = str(error)
            if detail.startswith(
                (
                    "the suggested UI target is no longer on screen",
                    "suggestion expired; analyze the current screen again",
                    "semantic stale verifier",
                )
            ):
                # No input was submitted: the UI advanced between analysis
                # and the executor's pre-click capture.  Discard the stale
                # suggestion and let the next cycle classify the new surface.
                steps.append(
                    InitialRegularAutopilotStep(
                        cycle,
                        surface.page,
                        "reanalyze",
                        None,
                        {
                            "input_submitted": False,
                            "reason": detail,
                        },
                    )
                )
                previous_page = None
                publish(previous_page, cycle)
                sleep(poll_interval_seconds)
                continue
            return finish(
                STATUS_HARD_STOP,
                f"{surface.page}-execution-failed:{type(error).__name__}:{error}",
                cycle,
            )
        except Exception as error:
            return finish(
                STATUS_HARD_STOP,
                f"{surface.page}-execution-failed:{type(error).__name__}:{error}",
                cycle,
            )

        previous_page = surface.page
        publish(previous_page, cycle)
        sleep(poll_interval_seconds)

    return finish(STATUS_HARD_STOP, "max-cycles-reached", max_cycles)


def run_live_initial_regular_autopilot(
    *,
    idol_card_id: str,
    plan_type: str,
    produce_id: str = "produce-001",
    game_root: str | Path = DEFAULT_PC_GAME_ROOT,
    expected_run_id: str | None = None,
    stage_number: int = 1,
    selected_idol_confirmed: bool = False,
    exam_policy: ExamExecutionPolicy = EXACT_EXAM_POLICY,
    stop_requested: Callable[[], bool] = lambda: False,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    outer_observer_anchor_provider: OuterObserverAnchorProvider | None = None,
    dependencies: InitialRegularLiveAutopilotDependencies | None = None,
    inner_imitation_enabled: bool = INNER_IMITATION_RUNTIME_ENABLED_DEFAULT,
    inner_imitation_runner: InnerImitationExamRunner | None = None,
    plan2_max_actions: int = 100,
    plan2_policy_bundle_path: str | Path | None = None,
) -> InitialRegularAutopilotResult:
    """Production convenience entry point; no agent is part of this runtime."""

    if type(selected_idol_confirmed) is not bool:
        raise TypeError("selected_idol_confirmed must be bool")
    if not isinstance(exam_policy, ExamExecutionPolicy):
        raise TypeError("exam_policy must be ExamExecutionPolicy")
    if type(inner_imitation_enabled) is not bool:
        raise TypeError("inner_imitation_enabled must be bool")
    if inner_imitation_runner is not None and not callable(inner_imitation_runner):
        raise TypeError("inner_imitation_runner must be callable or None")
    if type(plan2_max_actions) is not int or plan2_max_actions < 1:
        raise ValueError("plan2_max_actions must be a positive integer")
    if dependencies is None:
        from .runtime_command_client import input_backend

        if input_backend() == "dll":
            from .runtime_outer_runner import run_runtime_cultivation

            return run_runtime_cultivation(
                idol_card_id=idol_card_id, plan_type=plan_type, produce_id=produce_id,
                game_root=game_root, expected_run_id=expected_run_id,
                stop_requested=stop_requested, progress_callback=progress_callback,
                plan2_max_actions=plan2_max_actions,
                plan2_policy_bundle_path=plan2_policy_bundle_path,
            )
    root = Path(game_root)
    resolved_run_id: dict[str, str | None] = {
        "value": (
            expected_run_id.strip()
            if isinstance(expected_run_id, str) and expected_run_id.strip()
            else None
        )
    }
    injected_exam_dispatcher: ExamDispatcher | None = None
    plan2_context = InitialRegularPlan2ExamContext(
        idol_card_id=idol_card_id,
        produce_id=produce_id,
        stage_number=stage_number,
        max_actions=plan2_max_actions,
        learned_policy_bundle_path=(
            None
            if plan2_policy_bundle_path is None
            else Path(plan2_policy_bundle_path)
        ),
    )
    if dependencies is None:
        from .live_source import (
            checkpoint_confirmed_card_reward,
            checkpoint_live_activity_reward,
            checkpoint_live_nia_result,
            checkpoint_live_pursuit_lesson_result,
            checkpoint_live_training_drink_reward,
            ensure_initial_regular_active_run,
        )

        reader: SurfaceReader = InitialRegularLiveSurfaceReader(
            idol_card_id=idol_card_id,
            produce_id=produce_id,
            plan_type=plan_type,
            game_root=root,
            expected_run_id=expected_run_id,
        )
        snapshot_reader = lambda: read_current_produce_outer_local_save(root)
        if produce_id in {"produce-004", "produce-005"}:
            from .leaderboard_outer_schedule import (
                load_leaderboard_outer_choice_prior,
            )
            from .master_db import get_idol_profile
            from .nia_outer_prior import (
                compose_nia_outer_priors,
            )
            from .nia_training_dataset import (
                try_load_default_nia_exact_candidate_behavior_prior,
            )
            from .nia_outer_advisor import (
                DEFAULT_NIA_LIVE_POLICY,
                advise_nia_outer,
            )
            from .nia_bounded_outer_advisor import (
                build_bounded_nia_outer_advisor,
            )
            from .run_shadow import load_matching_active_run_shadow
            profile = get_idol_profile(idol_card_id)
            try:
                from .outer_bc_shadow import OuterBCShadowEvaluator

                outer_bc_shadow_evaluator = OuterBCShadowEvaluator.load(
                    produce_id=produce_id,
                    idol_card_id=idol_card_id,
                    character_id=(
                        None if profile is None else profile.character_id
                    ),
                    plan_type=(None if profile is None else profile.plan_type),
                    exam_effect_type=(
                        None if profile is None else profile.exam_effect_type
                    ),
                )
                outer_bc_shadow_error: str | None = None
            except Exception as error:
                outer_bc_shadow_evaluator = None
                outer_bc_shadow_error = f"{type(error).__name__}: {error}"
            leaderboard_outer_prior = None
            try:
                candidate_prior = load_leaderboard_outer_choice_prior()
                if (
                    profile is not None
                    and candidate_prior.applies_to(
                        produce_id=produce_id,
                        idol_card_id=idol_card_id,
                        exam_effect_type=profile.exam_effect_type,
                    )
                ):
                    leaderboard_outer_prior = candidate_prior
            except (FileNotFoundError, OSError, TypeError, ValueError):
                pass
            learned_outer_prior = compose_nia_outer_priors(
                try_load_default_nia_exact_candidate_behavior_prior(),
                leaderboard_outer_prior,
            )
            bounded_outer_advisor = build_bounded_nia_outer_advisor(
                advise_nia_outer,
                learned_outer_prior,
                fixed_kwargs={
                    "produce_id": produce_id,
                    "idol_card_id": idol_card_id,
                    "policy": DEFAULT_NIA_LIVE_POLICY,
                },
            )

            def outer_advisor_with_telemetry(snapshot, **kwargs):
                kwargs["run_shadow"] = load_matching_active_run_shadow(
                    produce_id,
                    idol_card_id,
                    expected_run_id=resolved_run_id["value"],
                )
                return bounded_outer_advisor(snapshot, **kwargs)

            profile_for_telemetry = profile

            def outer_advisor_telemetry(advice, legal_actions):
                return _nia_outer_decision_telemetry_source(
                    advice,
                    legal_actions,
                    prior=learned_outer_prior,
                    produce_id=produce_id,
                    idol_card_id=idol_card_id,
                    character_id=(
                        None
                        if profile_for_telemetry is None
                        else profile_for_telemetry.character_id
                    ),
                    plan_type=(
                        None
                        if profile_for_telemetry is None
                        else profile_for_telemetry.plan_type
                    ),
                    exam_effect_type=(
                        None
                        if profile_for_telemetry is None
                        else profile_for_telemetry.exam_effect_type
                    ),
                )

            # Keep the provider on the callable rather than threading a new
            # behavior-bearing dependency through every test/custom advisor.
            # The loop only calls it after the normal advisor has selected the
            # action, so this metadata cannot affect ordering or input.
            outer_advisor_with_telemetry._nia_telemetry_source = (
                outer_advisor_telemetry
            )

            def outer_bc_shadow_provider(advice, snapshot, candidates, formal_action):
                if outer_bc_shadow_evaluator is None:
                    raise RuntimeError(
                        outer_bc_shadow_error or "Outer BC shadow unavailable"
                    )
                return outer_bc_shadow_evaluator.evaluate(
                    advice=advice,
                    snapshot=snapshot,
                    candidate_ids=candidates,
                    formal_action=formal_action,
                )

            outer_advisor_with_telemetry._nia_outer_bc_shadow = (
                outer_bc_shadow_provider
            )
            outer_advisor = outer_advisor_with_telemetry
            choice_executor = execute_nia_outer_choice
        else:
            outer_advisor = advise_initial_regular_outer
            choice_executor = execute_initial_regular_choice
        single_click_executor = execute_initial_regular_single_click
        plan2_dependencies = None
        lesson_result_checkpointer = (
            (
                lambda capture: checkpoint_live_nia_result(
                    capture,
                    produce_id=produce_id,
                    game_root=root,
                )
            )
            if produce_id in {"produce-004", "produce-005"}
            else checkpoint_live_pursuit_lesson_result
        )
        activity_reward_checkpointer = lambda payload: (
            checkpoint_live_activity_reward(
                result=payload,
                produce_id=produce_id,
                idol_card_id=idol_card_id,
            )
        )
        training_reward_checkpointer = lambda payload: (
            checkpoint_live_training_drink_reward(
                result=payload,
                produce_id=produce_id,
                idol_card_id=idol_card_id,
            )
        )
        reward_checkpointer = checkpoint_confirmed_card_reward
        def production_run_context_bootstrapper(
            snapshot: ProduceOuterLocalSaveSnapshot,
            surface: InitialRegularSurface,
        ) -> Mapping[str, Any]:
            context = ensure_initial_regular_active_run(
                idol_card_id=idol_card_id,
                produce_id=produce_id,
                snapshot=snapshot,
                overview_output=(
                    surface.payload if surface.page == PAGE_OVERVIEW else None
                ),
                observed_character_id=(
                    str(surface.payload["observed_character_id"])
                    if surface.page == PAGE_EXAM
                    and isinstance(surface.payload.get("observed_character_id"), str)
                    else None
                ),
                selected_idol_confirmed=selected_idol_confirmed,
                expected_run_id=resolved_run_id["value"],
            )
            run_id = context.get("run_id")
            if not isinstance(run_id, str) or not run_id.strip():
                raise ValueError("active run bootstrap did not return run_id")
            resolved_run_id["value"] = run_id.strip()
            # Once the initially-unbound run is resolved, every later reader
            # checkpoint and Exam dispatcher shares that same identity.
            reader.expected_run_id = run_id.strip()
            return context

        run_context_bootstrapper: RunContextBootstrapper | None = (
            production_run_context_bootstrapper
        )
        sleep = time.sleep
    else:
        if not isinstance(
            dependencies,
            InitialRegularLiveAutopilotDependencies,
        ):
            raise TypeError("dependencies must be typed or None")
        reader = dependencies.surface_reader
        snapshot_reader = dependencies.snapshot_reader
        outer_advisor = dependencies.outer_advisor
        choice_executor = dependencies.choice_executor
        single_click_executor = dependencies.single_click_executor
        plan2_dependencies = dependencies.plan2_exam_dependencies
        injected_exam_dispatcher = dependencies.exam_dispatcher
        lesson_result_checkpointer = dependencies.lesson_result_checkpointer
        activity_reward_checkpointer = dependencies.activity_reward_checkpointer
        training_reward_checkpointer = dependencies.training_reward_checkpointer
        reward_checkpointer = dependencies.reward_checkpointer
        injected_run_context_bootstrapper = dependencies.run_context_bootstrapper
        if injected_run_context_bootstrapper is None:
            run_context_bootstrapper = None
        else:
            def bound_injected_run_context_bootstrapper(
                snapshot: ProduceOuterLocalSaveSnapshot,
                surface: InitialRegularSurface,
            ) -> Mapping[str, Any]:
                context = injected_run_context_bootstrapper(snapshot, surface)
                run_id = context.get("run_id")
                if not isinstance(run_id, str) or not run_id.strip():
                    raise ValueError("active run bootstrap did not return run_id")
                current = resolved_run_id["value"]
                if current is not None and current != run_id.strip():
                    raise ValueError(
                        "run context changed during unattended cultivation"
                    )
                resolved_run_id["value"] = run_id.strip()
                return context

            run_context_bootstrapper = bound_injected_run_context_bootstrapper
        inner_imitation_enabled = (
            inner_imitation_enabled or dependencies.inner_imitation_enabled
        )
        if inner_imitation_runner is None:
            inner_imitation_runner = dependencies.inner_imitation_runner
        sleep = dependencies.sleep
    return run_initial_regular_autopilot(
        plan_type=plan_type,
        surface_reader=reader,
        snapshot_reader=snapshot_reader,
        outer_advisor=outer_advisor,
        choice_executor=choice_executor,
        single_click_executor=single_click_executor,
        exam_dispatcher=(
            injected_exam_dispatcher
            if dependencies is not None and injected_exam_dispatcher is not None
            else lambda selected_plan, path: dispatch_initial_regular_exam(
                selected_plan,
                path,
                exam_policy=exam_policy,
                expected_run_id=resolved_run_id["value"],
                plan2_context=(plan2_context if selected_plan == PLAN2 else None),
                plan2_dependencies=(
                    plan2_dependencies if selected_plan == PLAN2 else None
                ),
                produce_id=produce_id,
                idol_card_id=idol_card_id,
                game_root=root,
                inner_imitation_enabled=inner_imitation_enabled,
                inner_imitation_runner=inner_imitation_runner,
            )
        ),
        lesson_result_checkpointer=lesson_result_checkpointer,
        activity_reward_checkpointer=activity_reward_checkpointer,
        training_reward_checkpointer=training_reward_checkpointer,
        reward_checkpointer=reward_checkpointer,
        run_context_bootstrapper=run_context_bootstrapper,
        outer_observer_anchor_provider=outer_observer_anchor_provider,
        source_run_id=resolved_run_id["value"],
        stop_requested=stop_requested,
        progress_callback=progress_callback,
        sleep=sleep,
        max_cycles=(
            NIA_LIVE_MAX_CYCLES
            if produce_id in {"produce-004", "produce-005"}
            else INITIAL_LIVE_MAX_CYCLES
        ),
        max_unknown_reads=(
            NIA_LIVE_MAX_UNKNOWN_READS
            if produce_id in {"produce-004", "produce-005"}
            else INITIAL_LIVE_MAX_UNKNOWN_READS
        ),
        exam_action_budget=(
            plan2_max_actions
            if plan2_policy_bundle_path is not None
            else None
        ),
    )


__all__ = [
    "InitialRegularAutopilotResult",
    "InitialRegularAutopilotStep",
    "InitialRegularLiveAutopilotDependencies",
    "InnerImitationExamRunner",
    "InitialRegularPlan2ExamContext",
    "InitialRegularPlan2ExamDependencies",
    "InitialRegularLiveSurfaceReader",
    "InitialRegularSurface",
    "PAGE_COMPLETED",
    "PAGE_ACTIVITY_REWARD",
    "PAGE_TRAINING_REWARD",
    "PAGE_PASSIVE_NOTIFICATION",
    "PAGE_CARD_ACQUIRE_NOTIFICATION",
    "PAGE_CARD_REWARD_DISPLAY",
    "PAGE_EXAM",
    "PAGE_FINAL_LIVE_PLAYING",
    "PAGE_FINAL_LIVE_START",
    "PAGE_OVERVIEW",
    "PAGE_POST_AUDITION_DIALOGUE",
    "PAGE_POST_LIVE",
    "PAGE_RESULT",
    "PAGE_REWARD",
    "PAGE_TRAINING",
    "PAGE_UNKNOWN",
    "PLAN1",
    "PLAN2",
    "PLAN3",
    "INITIAL_LIVE_MAX_CYCLES",
    "INITIAL_LIVE_MAX_UNKNOWN_READS",
    "NIA_LIVE_MAX_CYCLES",
    "NIA_CLICK1_RETRY_EVERY_READS",
    "NIA_LIVE_MAX_UNKNOWN_READS",
    "INNER_IMITATION_RUNTIME_ENABLED_DEFAULT",
    "INNER_IMITATION_VERIFIED_PRIOR_CANDIDATE",
    "SCHEMA_NAME",
    "STATUS_COMPLETED",
    "STATUS_HARD_STOP",
    "STATUS_STOPPED",
    "dispatch_initial_regular_exam",
    "execute_initial_regular_choice",
    "execute_initial_regular_single_click",
    "initial_regular_plan2_exam_dependencies",
    "load_initial_regular_plan2_exam_evidence",
    "run_initial_regular_autopilot",
    "run_live_initial_regular_autopilot",
]
