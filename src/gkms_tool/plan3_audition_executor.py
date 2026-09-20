"""Opt-in, one-action MAA executor for Plan 3 lessons and auditions.

The executor deliberately consumes only ``first_action``.  It never plays the
remaining advisory path: after one semantic action it waits for a changed,
settled LocalSave and asks the mode-matched advisor again.  ``unavailable``
reports and any advisor diagnostics are hard zero-input outcomes.

All input is sent through the existing elevated-controller client and the
verified helpers in :mod:`gkms_tool.live_actions`.  The API defaults to dry
run; callers must explicitly opt in to live execution.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import json
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from PIL import Image

from .audition_local_save_state import LocalSaveExamCard, LocalSaveExamState
from .card_art_matcher import PreparedCardArtMatcher
from .live_actions import (
    CardPlayExecutionResult,
    CardPlayFrameAnalysis,
    CardPlayFrameSample,
    ClickExecutionResult,
    SuggestedClick,
    StaleSuggestionError,
    canonical_to_outer_window,
    execute_suggested_click,
    execute_verified_card_click,
)
from .octo_assets import (
    OctoAssetIndex,
    card_asset_name,
    card_character_from_asset,
    card_suffix_from_asset,
)
from .plan3_audition_advisor import (
    Plan3AuditionAdvisorReport,
    advise_plan3_audition_decoded,
)
from .plan3_audition_advisor_gui import (
    Plan3LocalSaveSignature,
    read_plan3_local_save_signature,
    select_plan3_exam_local_save_path,
)
from .plan3_card_history import (
    Plan3CompletedCardReplay,
    Plan3TerminalCardReplay,
    predict_terminal_next_plan3_card_history,
    replay_completed_plan3_card_history,
    replay_next_completed_plan3_card_history,
)
from .plan3_drink import Plan3DrinkSelectedCardMove, load_plan3_drink
from .plan3_lesson_advisor import (
    Plan3LessonAdvisorReport,
    advise_plan3_lesson_decoded,
)
from .plan3_local_save_bridge import (
    DecodedPlan3LocalSave,
    decode_plan3_local_save_bytes,
    decode_plan3_local_save_file,
)
from .screen_state import scale_canonical_box


SCHEMA_NAME = "gkms_tool.plan3_exam_step_execution"
SCHEMA_VERSION = 1

MODE_AUTO = "auto"
MODE_LESSON = "lesson"
MODE_AUDITION = "audition"
ADVISOR_MODES = frozenset({MODE_AUTO, MODE_LESSON, MODE_AUDITION})

STATUS_PLANNED = "planned"
STATUS_EXECUTED = "executed"
STATUS_UNAVAILABLE = "unavailable"
STATUS_FAILED = "failed"

CanonicalBox = tuple[int, int, int, int]
Plan3AdvisorReport = Plan3AuditionAdvisorReport | Plan3LessonAdvisorReport

# Pinned against the 720 x 1280 background captures already used throughout
# live_actions.  The game compacts drink inventory from left to right.
DRINK_SLOT_BOXES: tuple[CanonicalBox, ...] = (
    (25, 1160, 99, 1242),
    (109, 1160, 183, 1242),
    (194, 1160, 268, 1242),
)
DRINK_USE_BOX: CanonicalBox = (370, 1110, 635, 1205)
SKIP_BOX: CanonicalBox = (623, 738, 707, 826)
SKIP_CONFIRM_TITLE_BOX: CanonicalBox = (25, 748, 694, 820)
# The modal is bottom-anchored and moves its header when the body gains or
# loses a line.  These two tight crops cover both observed Traditional Chinese
# layouts without scanning unrelated lesson HUD text.
SKIP_CONFIRM_TITLE_BOXES: tuple[CanonicalBox, ...] = (
    (45, 750, 250, 810),
    (45, 790, 250, 845),
)
# Exclude the check-mark icon: OCR on the whole orange button is materially
# less confident even though the affirmative label itself is stable.
SKIP_CONFIRM_YES_BOX: CanonicalBox = (440, 1115, 580, 1190)
SKIP_CONFIRM_POLL_INTERVAL_SECONDS = 0.25
SKIP_CONFIRM_MAX_SAMPLES = 16
SKIP_CONFIRM_STABLE_SAMPLES = 2

# Some cards ask for an extra confirmation when one of their effects cannot
# fire (for example, trying to raise a stance that is already capped).  The
# modal is visually distinct from the normal SELECT preview and appears only
# after the usual second same-slot click.
CARD_USE_CONFIRM_TITLE_BOXES: tuple[CanonicalBox, ...] = (
    (45, 750, 370, 815),
    (45, 785, 370, 850),
)
CARD_USE_CONFIRM_YES_BOX: CanonicalBox = (435, 1115, 610, 1195)
CARD_USE_CONFIRM_BUTTON_BOX: CanonicalBox = (370, 1110, 635, 1210)

# A Select-1 deck/grave overlay contains at most twelve cards in a fixed 4x3
# row-major grid.  The full eligible set is known exactly from LocalSave.
CARD_SELECTION_GRID_BOXES: tuple[CanonicalBox, ...] = tuple(
    (left, top, left + 120, top + 120)
    for top in (398, 545, 691)
    for left in (79, 227, 374, 521)
)
CARD_SELECTION_COMMIT_BOX: CanonicalBox = (230, 1115, 490, 1215)

# The lesson/audition Hand is a fixed bottom-row layout in the 720x1280
# client.  N.I.A. live execution binds card identity to ExamSaveData's ordered
# Hand, so these boxes are positional input coordinates rather than another
# screen-derived identity source.  They are pinned from genuine 1..5-card
# layouts; cards keep the same size and only their horizontal spacing changes.
FIXED_HAND_SLOT_BOXES: Mapping[int, tuple[CanonicalBox, ...]] = {
    1: ((265, 884, 455, 1135),),
    2: ((156, 884, 346, 1135), (374, 884, 564, 1135)),
    3: (
        (49, 884, 239, 1135),
        (265, 884, 455, 1135),
        (482, 884, 672, 1135),
    ),
    4: (
        (18, 884, 208, 1135),
        (183, 884, 373, 1135),
        (347, 884, 537, 1135),
        (512, 884, 702, 1135),
    ),
    5: (
        (16, 884, 206, 1135),
        (140, 884, 330, 1135),
        (263, 884, 453, 1135),
        (387, 884, 577, 1135),
        (511, 884, 701, 1135),
    ),
}


@dataclass(frozen=True, slots=True)
class PreviewCardArtEvidence:
    expected_suffix: str
    observed_suffix: str | None
    score: float
    margin: float
    matches_expected: bool


@dataclass(frozen=True, slots=True)
class SkipConfirmationEvidence:
    detected: bool
    title_text: str
    title_confidence: float
    yes_text: str
    yes_confidence: float


@dataclass(frozen=True, slots=True)
class CardUseConfirmationEvidence:
    detected: bool
    title_text: str
    title_confidence: float
    confirm_text: str
    confirm_confidence: float


class AdvisorFactory(Protocol):
    def __call__(
        self, decoded: DecodedPlan3LocalSave
    ) -> Plan3AdvisorReport: ...


class PredictionActualCallback(Protocol):
    def __call__(self, artifact: Mapping[str, object]) -> object: ...


class CardReplayResolver(Protocol):
    def __call__(
        self,
        before: DecodedPlan3LocalSave,
        after: DecodedPlan3LocalSave,
        plan: "Plan3AuditionExecutionPlan",
        prior: Plan3CompletedCardReplay | None,
    ) -> Plan3CompletedCardReplay | None: ...


class ReplayAdvisorFactory(Protocol):
    def __call__(
        self,
        decoded: DecodedPlan3LocalSave,
        replay: Plan3CompletedCardReplay,
    ) -> Plan3AdvisorReport: ...


@dataclass(frozen=True, slots=True)
class Plan3ExecutionIssue:
    code: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


class Plan3SettlementFrameTimeout(RuntimeError):
    """The card was committed, but UI frames never proved settlement.

    This is deliberately narrower than ``TimeoutError`` or ``RuntimeError``.
    The outer executor may recover only this condition from authoritative
    LocalSave evidence; every other driver failure keeps the ordinary failed
    path.  ``evidence`` is preserved in ``ui_trace`` and never causes another
    controller input.
    """

    code = "card-settlement-frame-timeout"

    def __init__(self, evidence: Mapping[str, object]) -> None:
        if not isinstance(evidence, Mapping):
            raise TypeError("settlement timeout evidence must be a mapping")
        self.evidence = dict(evidence)
        super().__init__("card UI did not publish a verified settled frame")

    def trace_entry(self) -> dict[str, object]:
        return {
            "step": "card-settlement-frame-timeout",
            "event": self.code,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class Plan3AuditionExecutionPlan:
    kind: str
    action: Mapping[str, object]
    ui_steps: tuple[str, ...]
    selected_card: Mapping[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "action": dict(self.action),
            "ui_steps": list(self.ui_steps),
            "selected_card": (
                None if self.selected_card is None else dict(self.selected_card)
            ),
        }


@dataclass(frozen=True, slots=True)
class Plan3AuditionStepExecutionResult:
    status: str
    dry_run: bool
    source: str
    input_count: int
    before_advisor: Mapping[str, object] | None
    plan: Mapping[str, object] | None
    settled_local_save_changed: bool
    after_advisor: Mapping[str, object] | None
    ui_trace: tuple[Mapping[str, object], ...]
    issues: tuple[Plan3ExecutionIssue, ...]
    advisor_mode: str = MODE_AUTO
    prediction_actual: Mapping[str, object] | None = None
    completed_card_replay: Plan3CompletedCardReplay | None = None
    settled_native_state_changed: bool = False
    executor_backend: str = "maa"
    native_action_status: str | None = None

    @property
    def input_sent(self) -> bool:
        return self.input_count > 0

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": SCHEMA_NAME,
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "dry_run": self.dry_run,
            "advisor_mode": self.advisor_mode,
            "source": self.source,
            "input_sent": self.input_sent,
            "input_count": self.input_count,
            "before_advisor": (
                None if self.before_advisor is None else dict(self.before_advisor)
            ),
            "plan": None if self.plan is None else dict(self.plan),
            "settled_local_save_changed": self.settled_local_save_changed,
            "settled_native_state_changed": self.settled_native_state_changed,
            "executor_backend": self.executor_backend,
            "native_action_status": self.native_action_status,
            "after_advisor": (
                None if self.after_advisor is None else dict(self.after_advisor)
            ),
            "prediction_actual": (
                None
                if self.prediction_actual is None
                else dict(self.prediction_actual)
            ),
            "ui_trace": [dict(value) for value in self.ui_trace],
            "issues": [issue.to_dict() for issue in self.issues],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, indent=indent, allow_nan=False
        )


@dataclass(frozen=True, slots=True)
class SettledPlan3Action:
    signature: Plan3LocalSaveSignature
    decoded: DecodedPlan3LocalSave
    advisor: Plan3AdvisorReport
    completed_card_replay: Plan3CompletedCardReplay | None = None


@dataclass(frozen=True, slots=True)
class Plan3ScreenTerminalAction:
    """Final-card settlement proven without a persisted after-LocalSave."""

    terminal_replay: Plan3TerminalCardReplay
    observed_result: Mapping[str, object]
    capture_paths: tuple[str, str]
    confidence: float

    def __post_init__(self) -> None:
        if len(self.capture_paths) != 2 or any(
            not isinstance(value, str) or not value for value in self.capture_paths
        ):
            raise ValueError("terminal screen action requires two capture paths")
        if (
            not isinstance(self.confidence, (int, float))
            or isinstance(self.confidence, bool)
            or not 0.0 <= self.confidence <= 1.0
        ):
            raise ValueError("terminal screen confidence must be between zero and one")


@dataclass(frozen=True, slots=True)
class Plan3MissingLocalSaveTerminalAction:
    """NIA terminal boundary proven by a bound input then ExamSave removal.

    NIA uses the durable ExamSave as the battle-state authority.  Once a MAA
    action has returned successfully, every irreversible input has passed the
    ExamSave compare-and-swap, and the game removes that exact ExamSave, there
    is no useful card-art/result-number check left to perform.  This boundary
    deliberately carries no inferred score or attribute result.
    """

    action_kind: str
    authority: str = "exam-save-removed-after-bound-maa-action"

    def __post_init__(self) -> None:
        if self.action_kind not in {"card", "skip"}:
            raise ValueError(
                "missing-ExamSave terminal action must be card or skip"
            )


Plan3SettledOutcome = (
    SettledPlan3Action
    | Plan3ScreenTerminalAction
    | Plan3MissingLocalSaveTerminalAction
)


# Neutral public names for new callers.  The audition-prefixed classes remain
# aliases for compatibility with the first version of this executor.
Plan3ExecutionPlan = Plan3AuditionExecutionPlan
Plan3StepExecutionResult = Plan3AuditionStepExecutionResult


def _raw_exam_save(decoded: DecodedPlan3LocalSave) -> Mapping[str, object]:
    value = json.loads(decoded.envelope.plaintext.decode("utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError("decrypted ExamSaveData root must be an object")
    return value


def _drink_ids(decoded: DecodedPlan3LocalSave) -> tuple[str, ...]:
    raw = _raw_exam_save(decoded).get("drinkList")
    if not isinstance(raw, list):
        raise ValueError("ExamSaveData.drinkList must be a list")
    result: list[str] = []
    for index, value in enumerate(raw):
        if not isinstance(value, Mapping):
            raise ValueError(f"ExamSaveData.drinkList[{index}] must be an object")
        drink_id = value.get("_id")
        if not isinstance(drink_id, str) or not drink_id:
            raise ValueError(
                f"ExamSaveData.drinkList[{index}]._id must be non-empty text"
            )
        result.append(drink_id)
    return tuple(result)


def _find_guid(
    state: LocalSaveExamState, guid: str
) -> tuple[str, int, LocalSaveExamCard]:
    matches = tuple(
        (zone, index, card)
        for zone in ("deck", "grave")
        for index, card in enumerate(getattr(state.zones, zone))
        if card.guid == guid
    )
    if len(matches) != 1:
        raise ValueError(f"selected GUID is not unique in Deck/Grave: {guid}")
    return matches[0]


def _normalize_advisor_mode(mode: str) -> str:
    if mode not in ADVISOR_MODES:
        raise ValueError("advisor mode must be auto, lesson, or audition")
    return mode


def _exam_type_advisor_mode(decoded: DecodedPlan3LocalSave) -> str:
    if decoded.exam_state.exam_type == 0:
        return MODE_LESSON
    if decoded.exam_state.exam_type == 1:
        return MODE_AUDITION
    raise ValueError(
        f"unsupported Plan3 exam_type for auto advisor: "
        f"{decoded.exam_state.exam_type}"
    )


def _resolved_advisor_mode(decoded: DecodedPlan3LocalSave, mode: str) -> str:
    requested = _normalize_advisor_mode(mode)
    return (
        _exam_type_advisor_mode(decoded)
        if requested == MODE_AUTO
        else requested
    )


def _default_advisor_factory(source: str, mode: str) -> AdvisorFactory:
    requested = _normalize_advisor_mode(mode)

    def advise(decoded: DecodedPlan3LocalSave) -> Plan3AdvisorReport:
        resolved = _resolved_advisor_mode(decoded, requested)
        if resolved == MODE_LESSON:
            return advise_plan3_lesson_decoded(decoded, source=source)
        return advise_plan3_audition_decoded(decoded, source=source)

    return advise


def build_plan3_exam_execution_plan(
    report: Plan3AdvisorReport,
    decoded: DecodedPlan3LocalSave,
) -> Plan3AuditionExecutionPlan:
    """Bind an advisor first action back to the exact LocalSave instance."""

    if not report.available or report.first_action is None:
        raise ValueError("advisor is unavailable")
    if report.diagnostics:
        raise ValueError("advisor diagnostics block execution")
    action = dict(report.first_action)
    kind = action.get("kind")
    state = decoded.exam_state

    if kind == "card":
        index = action.get("hand_index")
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValueError("card action has no integer hand_index")
        if not 0 <= index < len(state.zones.hand):
            raise ValueError("card hand_index is outside LocalSave Hand")
        card = state.zones.hand[index]
        expected = (
            action.get("card_guid"),
            action.get("card_id"),
            action.get("card_upgrade"),
        )
        actual = (card.guid, card.card_id, card.effective_upgrade)
        if expected != actual:
            raise ValueError(
                f"card action no longer matches LocalSave Hand: {expected!r} != {actual!r}"
            )
        return Plan3AuditionExecutionPlan(
            kind="card",
            action=action,
            ui_steps=("card-select", "card-commit-same-slot"),
        )

    if kind == "drink":
        slot = action.get("drink_slot_index")
        if not isinstance(slot, int) or isinstance(slot, bool):
            raise ValueError("drink action has no integer drink_slot_index")
        drinks = _drink_ids(decoded)
        if not 0 <= slot < len(drinks) or slot >= len(DRINK_SLOT_BOXES):
            raise ValueError("drink slot is outside the supported visible inventory")
        if drinks[slot] != action.get("drink_id"):
            raise ValueError("drink action no longer matches LocalSave inventory")
        drink = load_plan3_drink(drinks[slot])
        requires_selection = any(
            isinstance(effect, Plan3DrinkSelectedCardMove)
            for effect in drink.effects
        )
        selected_guid = action.get("selected_card_guid")
        if requires_selection != bool(selected_guid):
            raise ValueError("drink selected-card requirement does not match advisor")
        selected: Mapping[str, object] | None = None
        steps = ["drink-open-slot", "drink-confirm-use"]
        if selected_guid:
            if not isinstance(selected_guid, str):
                raise ValueError("selected_card_guid must be text")
            eligible = (*state.zones.deck, *state.zones.grave)
            if not eligible or len(eligible) > len(CARD_SELECTION_GRID_BOXES):
                raise ValueError("drink card-selection grid is outside 1..12 cards")
            zone, zone_index, card = _find_guid(state, selected_guid)
            selected = {
                "guid": card.guid,
                "card_id": card.card_id,
                "upgrade": card.effective_upgrade,
                "zone": zone,
                "zone_index": zone_index,
            }
            steps.extend(("drink-select-card-guid", "drink-commit-selection"))
        return Plan3AuditionExecutionPlan(
            kind="drink",
            action=action,
            ui_steps=tuple(steps),
            selected_card=selected,
        )

    if kind == "skip":
        return Plan3AuditionExecutionPlan(
            kind="skip",
            action=action,
            ui_steps=("skip-turn", "skip-confirm-yes-if-present"),
        )
    raise ValueError(f"unsupported advisor action kind: {kind!r}")


def build_plan3_audition_execution_plan(
    report: Plan3AdvisorReport,
    decoded: DecodedPlan3LocalSave,
) -> Plan3AuditionExecutionPlan:
    """Compatibility wrapper for the original audition-only public API."""

    return build_plan3_exam_execution_plan(report, decoded)


def _card_suffix(card_id: str) -> str:
    return card_suffix_from_asset(card_asset_name(card_id))


def selection_grid_index_for_guid(
    state: LocalSaveExamState,
    selected_guid: str,
    observed_suffixes: Sequence[str],
) -> int:
    """Map a GUID to the visible grid, preserving duplicate-card order.

    The native selector receives Deck followed by Grave.  The UI groups equal
    artwork but preserves relative order inside each equal-art group, allowing
    otherwise identical card instances to remain GUID-addressable.
    """

    eligible = (*state.zones.deck, *state.zones.grave)
    if len(observed_suffixes) != len(eligible):
        raise ValueError("selection grid count differs from LocalSave Deck/Grave")
    expected_suffixes = tuple(_card_suffix(card.card_id) for card in eligible)
    observed = tuple(observed_suffixes)
    if Counter(observed) != Counter(expected_suffixes):
        raise ValueError("selection grid cards differ from LocalSave Deck/Grave")
    target_index = next(
        (index for index, card in enumerate(eligible) if card.guid == selected_guid),
        None,
    )
    if target_index is None:
        raise ValueError("selected GUID is absent from LocalSave Deck/Grave")
    target_suffix = expected_suffixes[target_index]
    occurrence = sum(
        1 for suffix in expected_suffixes[:target_index] if suffix == target_suffix
    )
    visible_matches = tuple(
        index for index, suffix in enumerate(observed) if suffix == target_suffix
    )
    if occurrence >= len(visible_matches):
        raise ValueError("selected GUID occurrence is absent from selection grid")
    return visible_matches[occurrence]


def _preferred_card_art(
    cards: Sequence[LocalSaveExamCard], character_id: str
) -> Mapping[str, Path]:
    suffixes = {_card_suffix(card.card_id) for card in cards}
    raw = OctoAssetIndex.load().ensure_card_art(
        character_id=character_id,
        suffixes=suffixes,
    )
    selected: dict[str, tuple[str, Path]] = {}
    for asset_name, path in raw.items():
        suffix = card_suffix_from_asset(asset_name)
        variant = card_character_from_asset(asset_name)
        current = selected.get(suffix)
        if current is None or variant == character_id:
            selected[suffix] = (asset_name, path)
    if set(selected) != suffixes:
        missing = sorted(suffixes - set(selected))
        raise ValueError("static card artwork is missing: " + ",".join(missing))
    # PreparedCardArtMatcher returns its mapping key as ``asset_name``.  Keep
    # the real static asset name here so callers can recover its card suffix.
    return {asset_name: path for asset_name, path in selected.values()}


def _observed_card_suffixes(
    image_path: Path,
    boxes: Sequence[CanonicalBox],
    cards: Sequence[LocalSaveExamCard],
    *,
    character_id: str,
    minimum_score: float = 0.35,
    minimum_margin: float = 0.06,
) -> tuple[str, ...]:
    matcher = PreparedCardArtMatcher.build(
        _preferred_card_art(cards, character_id), size=64
    )
    observed: list[str] = []
    with Image.open(image_path.resolve()) as source:
        image = source.convert("RGB")
        for index, box in enumerate(boxes):
            crop = image.crop(scale_canonical_box(box, image.width, image.height))
            match = matcher.match(crop)
            if match.score < minimum_score or match.margin < minimum_margin:
                raise ValueError(
                    f"card artwork at UI slot {index} is ambiguous: "
                    f"score={match.score:.3f};margin={match.margin:.3f}"
                )
            observed.append(card_suffix_from_asset(match.asset_name))
    return tuple(observed)


def _preview_card_art_evidence(
    image_path: Path,
    box: CanonicalBox,
    cards: Sequence[LocalSaveExamCard],
    *,
    expected_card_id: str,
    character_id: str,
    minimum_score: float = 0.35,
    minimum_margin: float = 0.06,
) -> PreviewCardArtEvidence:
    """Bind a SELECT preview back to the exact pre-click Hand artwork.

    The source Hand slot has already been bound to its ordered LocalSave GUID
    before the first click.  This second read checks that the raised card still
    occupying that same slot has the expected static artwork; it is used only
    when localized title OCR did not resolve any card ID.
    """

    expected_suffix = _card_suffix(expected_card_id)
    matcher = PreparedCardArtMatcher.build(
        _preferred_card_art(cards, character_id), size=64
    )
    with Image.open(image_path.resolve()) as source:
        image = source.convert("RGB")
        crop = image.crop(scale_canonical_box(box, image.width, image.height))
        match = matcher.match(crop)
    observed_suffix = card_suffix_from_asset(match.asset_name)
    return PreviewCardArtEvidence(
        expected_suffix=expected_suffix,
        observed_suffix=observed_suffix,
        score=match.score,
        margin=match.margin,
        matches_expected=bool(
            observed_suffix == expected_suffix
            and match.score >= minimum_score
            and match.margin >= minimum_margin
        ),
    )


def _normalize_ui_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).upper()
    return "".join(character for character in normalized if character.isalnum())


def _skip_confirmation_evidence(
    image_path: Path,
    *,
    recognizer: object | None = None,
    minimum_title_confidence: float = 0.80,
    minimum_yes_confidence: float = 0.85,
) -> SkipConfirmationEvidence:
    """Recognize the localized Turn End modal and its affirmative button."""

    from PIL import ImageOps

    if recognizer is None:
        from .live_source import _live_text_recognizer

        recognizer = _live_text_recognizer()

    def best(image: Image.Image, box: CanonicalBox) -> tuple[str, float]:
        crop = image.crop(scale_canonical_box(box, image.width, image.height))
        variants = (crop, ImageOps.autocontrast(crop.convert("L")))
        results = [recognizer.recognize(value) for value in variants]  # type: ignore[attr-defined]
        result = max(results, key=lambda value: float(value.confidence))
        return str(result.text), float(result.confidence)

    with Image.open(image_path.resolve()) as source:
        image = source.convert("RGB")
        title_text, title_confidence = max(
            (best(image, box) for box in SKIP_CONFIRM_TITLE_BOXES),
            key=lambda value: value[1],
        )
        yes_text, yes_confidence = best(image, SKIP_CONFIRM_YES_BOX)

    title = _normalize_ui_text(title_text)
    yes = _normalize_ui_text(yes_text)
    detected = bool(
        title_confidence >= minimum_title_confidence
        and yes_confidence >= minimum_yes_confidence
        and title in {"回合結束", "回合结束", "回合終了", "ターン終了", "TURNEND"}
        and yes in {"是", "はい", "YES"}
    )
    return SkipConfirmationEvidence(
        detected=detected,
        title_text=title_text,
        title_confidence=title_confidence,
        yes_text=yes_text,
        yes_confidence=yes_confidence,
    )


def _card_use_confirmation_evidence(
    image_path: Path,
    *,
    recognizer: object | None = None,
    minimum_title_confidence: float = 0.80,
    minimum_confirm_confidence: float = 0.80,
) -> CardUseConfirmationEvidence:
    """Recognize the localized optional skill-card-use confirmation modal."""

    from PIL import ImageOps

    if recognizer is None:
        from .live_source import _live_text_recognizer

        recognizer = _live_text_recognizer()

    def best(image: Image.Image, box: CanonicalBox) -> tuple[str, float]:
        crop = image.crop(scale_canonical_box(box, image.width, image.height))
        variants = (crop, ImageOps.autocontrast(crop.convert("L")))
        results = [recognizer.recognize(value) for value in variants]  # type: ignore[attr-defined]
        result = max(results, key=lambda value: float(value.confidence))
        return str(result.text), float(result.confidence)

    with Image.open(image_path.resolve()) as source:
        image = source.convert("RGB")
        title_text, title_confidence = max(
            (best(image, box) for box in CARD_USE_CONFIRM_TITLE_BOXES),
            key=lambda value: value[1],
        )
        confirm_text, confirm_confidence = best(image, CARD_USE_CONFIRM_YES_BOX)

    title = _normalize_ui_text(title_text)
    confirm = _normalize_ui_text(confirm_text)
    detected = bool(
        title_confidence >= minimum_title_confidence
        and confirm_confidence >= minimum_confirm_confidence
        and title
        in {
            "技能卡使用確認",
            "技能卡使用确认",
            "スキルカード使用確認",
            "SKILLCARDUSECONFIRMATION",
        }
        and confirm in {"確定", "确定", "決定", "OK", "はい"}
    )
    return CardUseConfirmationEvidence(
        detected=detected,
        title_text=title_text,
        title_confidence=title_confidence,
        confirm_text=confirm_text,
        confirm_confidence=confirm_confidence,
    )


def _hand_art_box(box: CanonicalBox) -> CanonicalBox:
    """Crop the square artwork out of a full detected hand card."""

    left, top, right, bottom = box
    width = right - left
    return (
        left + 4,
        top + 8,
        right - 4,
        min(bottom, top + width - 2),
    )


def _capture_path(capture: Mapping[str, object]) -> Path:
    value = capture.get("png_path")
    if not isinstance(value, str) or not value:
        raise ValueError("controller capture has no PNG path")
    path = Path(value)
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _action_for_box(
    capture: Mapping[str, object], label: str, box: CanonicalBox
) -> SuggestedClick:
    return SuggestedClick(
        label=label,
        canonical_x=(box[0] + box[2]) // 2,
        canonical_y=(box[1] + box[3]) // 2,
        source_png_path=str(_capture_path(capture)),
        source_timestamp=float(capture["timestamp"]),
        source_hwnd=int(capture["hwnd"]),
        source_pid=int(capture["pid"]),
        verification_box=box,
        click_count=1,
    )


def _state_semantic_key(state: LocalSaveExamState) -> tuple[object, ...]:
    return (
        state.phase,
        state.current_turn,
        state.remain_turn,
        state.score,
        state.stamina,
        state.block,
        state.exam_card_play_count,
        tuple(card.guid for card in state.zones.hand),
        tuple(card.guid for card in state.zones.deck),
        tuple(card.guid for card in state.zones.grave),
        tuple(card.guid for card in state.zones.lost),
        tuple(card.guid for card in state.zones.hold),
    )


def is_plan3_exam_terminal(state: LocalSaveExamState) -> bool:
    """Return the persisted native terminal boundary for lesson/audition.

    ``remain_turn`` can still be positive on a force-ended exam and phase can
    move before the result animation finishes.  The root runtime flag is the
    authoritative common signal; screenshots are intentionally not involved.
    """

    runtime = state.root_runtime
    return bool(
        state.exam_type in {0, 1}
        and runtime is not None
        and runtime.is_exam_end_complete
    )


def _terminal_state(state: LocalSaveExamState) -> bool:
    return is_plan3_exam_terminal(state)


def is_plan3_exam_actionable_settled(state: LocalSaveExamState) -> bool:
    """Common native Main-state boundary for both Plan3 exam types."""

    runtime = state.root_runtime
    return bool(
        runtime is not None
        and state.exam_type in {0, 1}
        and state.phase == 6
        and not state.is_turn_card_play_end
        and state.playing_card is None
        and not state.removed_cards
        and runtime.command_list_is_empty
        and not runtime.draw_card_guid_list
        and not runtime.is_turn_card_grave
        and not runtime.is_turn_card_lost
        and not runtime.is_exam_end_complete
    )


def _action_applied(
    before: DecodedPlan3LocalSave,
    after: DecodedPlan3LocalSave,
    plan: Plan3AuditionExecutionPlan,
) -> bool:
    old = before.exam_state
    new = after.exam_state
    if plan.kind == "card":
        guid = str(plan.action["card_guid"])
        if new.exam_card_play_count != old.exam_card_play_count + 1:
            return False
        before_matches = tuple(
            card for card in old.zones.hand if card.guid == guid
        )
        after_matches = tuple(
            card for card in new.all_card_instances if card.guid == guid
        )
        if len(before_matches) != 1:
            return False
        selected = before_matches[0]
        if len(after_matches) == 1:
            observed = after_matches[0]
            before_runtime = selected.runtime_state
            after_runtime = observed.runtime_state
            if (
                observed.card_id == selected.card_id
                and observed.effective_upgrade == selected.effective_upgrade
                and before_runtime is not None
                and after_runtime is not None
                and after_runtime.play_count > before_runtime.play_count
            ):
                return True

        # On a same-turn boundary the selected GUID leaving Hand while every
        # other prior Hand instance remains is itself an exact instance proof.
        # A turn advance redraws the whole Hand and must use the per-GUID
        # runtime play count above instead of the old "absent from Hand"
        # shortcut, which could accept the wrong card.
        if new.current_turn != old.current_turn or _terminal_state(new):
            return False
        new_hand_guids = {card.guid for card in new.zones.hand}
        other_guids = {
            card.guid for card in old.zones.hand if card.guid != guid
        }
        return guid not in new_hand_guids and other_guids <= new_hand_guids
    if plan.kind == "drink":
        slot = int(plan.action["drink_slot_index"])
        old_drinks = _drink_ids(before)
        new_drinks = _drink_ids(after)
        expected = (*old_drinks[:slot], *old_drinks[slot + 1 :])
        if new_drinks != expected:
            return False
        selected = plan.action.get("selected_card_guid")
        return bool(
            selected is None
            or any(card.guid == selected for card in new.zones.hold)
        )
    if plan.kind == "skip":
        return bool(
            _terminal_state(new)
            or (
                (
                    new.current_turn > old.current_turn
                    or new.remain_turn < old.remain_turn
                )
                and new.exam_card_play_count == old.exam_card_play_count
            )
        )
    return False


def plan3_exam_action_applied(
    before: DecodedPlan3LocalSave,
    after: DecodedPlan3LocalSave,
    plan: Plan3AuditionExecutionPlan,
) -> bool:
    """Public persisted-state proof used by tests and artifact adapters."""

    if _action_applied(before, after, plan):
        return True
    if plan.kind != "card":
        return False
    try:
        return replay_completed_plan3_card_history(
            before,
            after,
            str(plan.action["card_guid"]),
        ) is not None
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _action_expectation_and_actual(
    before: DecodedPlan3LocalSave,
    after: DecodedPlan3LocalSave,
    plan: Plan3AuditionExecutionPlan,
    *,
    completed_card_replay: Plan3CompletedCardReplay | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    old = before.exam_state
    new = after.exam_state
    terminal = is_plan3_exam_terminal(new)
    expected: dict[str, object] = {"kind": plan.kind, "applied": True}
    actual: dict[str, object] = {
        "kind": plan.kind,
        "applied": bool(
            completed_card_replay is not None
            or _action_applied(before, after, plan)
        ),
    }
    if plan.kind == "card":
        guid = str(plan.action["card_guid"])
        expected.update(
            {
                "selected_guid": guid,
                "exact_action_applied": True,
                "exam_card_play_count_delta": (
                    completed_card_replay.exam_card_play_count_delta
                    if completed_card_replay is not None
                    else 1
                ),
            }
        )
        actual.update(
            {
                "selected_guid": guid,
                "exact_action_applied": bool(
                    completed_card_replay is not None
                    or _action_applied(before, after, plan)
                ),
                "exam_card_play_count_delta": (
                    new.exam_card_play_count - old.exam_card_play_count
                ),
            }
        )
        if completed_card_replay is not None:
            expected["completed_command_replay"] = True
            actual["completed_command_replay"] = True
    elif plan.kind == "drink":
        slot = int(plan.action["drink_slot_index"])
        old_drinks = _drink_ids(before)
        expected_drinks = (*old_drinks[:slot], *old_drinks[slot + 1 :])
        expected["inventory_after"] = list(expected_drinks)
        actual["inventory_after"] = list(_drink_ids(after))
        selected = plan.action.get("selected_card_guid")
        if selected is not None:
            expected["selected_guid_in_hold"] = True
            actual["selected_guid_in_hold"] = any(
                card.guid == selected for card in new.zones.hold
            )
    elif plan.kind == "skip":
        expected.update(
            {
                "turn_advanced_or_terminal": True,
                "play_count_unchanged_or_terminal": True,
            }
        )
        actual.update(
            {
                "turn_advanced_or_terminal": (
                    new.current_turn > old.current_turn
                    or new.remain_turn < old.remain_turn
                    or terminal
                ),
                "play_count_unchanged_or_terminal": (
                    new.exam_card_play_count == old.exam_card_play_count
                    or terminal
                ),
            }
        )
    return expected, actual


def build_plan3_prediction_actual_artifact(
    *,
    advisor_mode: str,
    before_report: Plan3AdvisorReport,
    before: DecodedPlan3LocalSave,
    plan: Plan3AuditionExecutionPlan,
    settled: SettledPlan3Action,
) -> dict[str, object]:
    """Build a logger-compatible JSON diff without writing an artifact.

    Callers may store this mapping directly or pass it to an existing training
    logger adapter.  The immediate action comparison uses persisted LocalSave
    fields.  A terminal forecast is compared with actual fields only when the
    newly settled state is truly terminal; otherwise it remains a forecast and
    is compared only with the freshly replanned advisor forecast.
    """

    from .plan3_training_logger import (
        compare_training_values,
        summarize_local_save,
    )

    mode = _normalize_advisor_mode(advisor_mode)
    if mode == MODE_AUTO:
        mode = _resolved_advisor_mode(before, mode)
    expected_action, actual_action = _action_expectation_and_actual(
        before,
        settled.decoded,
        plan,
        completed_card_replay=settled.completed_card_replay,
    )
    action_differences = compare_training_values(
        expected_action,
        actual_action,
        path="action",
        code="plan3-executor-action-mismatch",
    )
    before_terminal = (
        None if before_report.terminal is None else dict(before_report.terminal)
    )
    after_terminal = (
        None
        if settled.advisor.terminal is None
        else dict(settled.advisor.terminal)
    )
    forecast_differences = compare_training_values(
        before_terminal,
        after_terminal,
        path="advisor_terminal",
        code="plan3-advisor-terminal-replan",
    )
    actual_state = settled.decoded.exam_state
    actual_summary = {
        **summarize_local_save(actual_state),
        "exam_type": actual_state.exam_type,
        "executor_actionable_settled": is_plan3_exam_actionable_settled(
            actual_state
        ),
        "executor_terminal": is_plan3_exam_terminal(actual_state),
        "executor_completed_card_replay": (
            settled.completed_card_replay is not None
        ),
        "executor_completed_card_replay_provenance": (
            None
            if settled.completed_card_replay is None
            else settled.completed_card_replay.provenance
        ),
    }
    if settled.completed_card_replay is not None:
        replay = settled.completed_card_replay
        actual_summary["logical_after_replay"] = {
            "score": replay.after.score,
            "stamina": replay.after.stamina,
            "block": replay.after.block,
            "plays_remaining": replay.after.plays_remaining,
            "card_guid": replay.card_guid,
            "provenance": replay.provenance,
            "chain_depth": replay.chain_depth,
            "chain_card_guids": list(replay.chain_card_guids),
        }
    terminal_differences: list[dict[str, object]] | None = None
    if is_plan3_exam_terminal(actual_state) and before_terminal:
        comparable = {
            key: before_terminal[key]
            for key in ("score", "stamina", "block", "max_stamina")
            if key in before_terminal and key in actual_summary
        }
        terminal_differences = [
            item.to_dict()
            for item in compare_training_values(
                comparable,
                {key: actual_summary[key] for key in comparable},
                path="terminal",
                code="plan3-advisor-terminal-actual-mismatch",
            )
        ]
    return {
        "schema": "gkms_tool.plan3_executor_prediction_actual",
        "schema_version": 1,
        "advisor_mode": mode,
        "action": plan.to_dict(),
        "prediction": {
            "current_state": (
                None
                if before_report.current_state is None
                else dict(before_report.current_state)
            ),
            "terminal": before_terminal,
            "action_expectation": expected_action,
        },
        "actual": {
            "local_save": actual_summary,
            "terminal": is_plan3_exam_terminal(actual_state),
            "action_evidence": actual_action,
        },
        "replanned": {
            "status": settled.advisor.status,
            "current_state": (
                None
                if settled.advisor.current_state is None
                else dict(settled.advisor.current_state)
            ),
            "terminal": after_terminal,
        },
        "differences": {
            "action_application": [
                item.to_dict() for item in action_differences
            ],
            "terminal_prediction_vs_actual": terminal_differences,
            "terminal_prediction_replan": [
                item.to_dict() for item in forecast_differences
            ],
        },
    }


def build_plan3_screen_terminal_artifact(
    *,
    advisor_mode: str,
    before_report: Plan3AdvisorReport,
    plan: Plan3AuditionExecutionPlan,
    settled: Plan3ScreenTerminalAction,
) -> dict[str, object]:
    """Record a terminal card whose after-LocalSave was removed by the game."""

    if plan.kind != "card":
        raise ValueError("screen terminal settlement requires a card plan")
    replay = settled.terminal_replay
    if str(plan.action.get("card_guid")) != replay.card_guid:
        raise ValueError("screen terminal replay card differs from the plan")
    predicted = before_report.terminal
    if not isinstance(predicted, Mapping) or predicted.get("complete") is not True:
        raise ValueError("advisor did not predict a complete terminal lesson")
    actual_terminal = {
        "complete": True,
        "clear": replay.path.evaluation.clear,
        "perfect": replay.path.evaluation.perfect,
        "score": replay.after.score,
        "stamina": replay.after.stamina,
        "block": replay.after.block,
        "force_end": True,
        "force_end_stamina_recovered": sum(
            step.forced_end_stamina_recovered
            for step in replay.path.steps
            if step.kind == "force_end"
        ),
    }
    for key in (
        "complete",
        "clear",
        "perfect",
        "score",
        "stamina",
        "block",
        "force_end",
        "force_end_stamina_recovered",
    ):
        if predicted.get(key) != actual_terminal[key]:
            raise ValueError(
                f"advisor terminal {key} differs from strict replay: "
                f"{predicted.get(key)!r}!={actual_terminal[key]!r}"
            )
    action_evidence = {
        "kind": "card",
        "selected_guid": replay.card_guid,
        "exact_action_applied": True,
        "terminal_card_replay": True,
    }
    local_save = {
        "turns_remaining": 0,
        "plays_remaining": 0,
        "score": replay.after.score,
        "stamina": replay.after.stamina,
        "max_stamina": replay.after.max_stamina,
        "block": replay.after.block,
        "executor_actionable_settled": False,
        "executor_terminal": True,
        "executor_completed_card_replay": False,
        "executor_completed_card_replay_provenance": None,
        "executor_terminal_card_replay": True,
        "executor_terminal_card_replay_provenance": replay.provenance,
        "local_save_boundary": "removed-by-game",
        "chain_depth": replay.chain_depth,
        "chain_card_guids": list(replay.chain_card_guids),
    }
    return {
        "schema": "gkms_tool.plan3_executor_prediction_actual",
        "schema_version": 1,
        "advisor_mode": _normalize_advisor_mode(advisor_mode),
        "action": plan.to_dict(),
        "prediction": {
            "current_state": (
                None
                if before_report.current_state is None
                else dict(before_report.current_state)
            ),
            "terminal": dict(predicted),
            "action_expectation": dict(action_evidence),
        },
        "actual": {
            "local_save": local_save,
            "terminal": True,
            "terminal_state": actual_terminal,
            "action_evidence": dict(action_evidence),
            "result_screen": {
                "stable_capture_paths": list(settled.capture_paths),
                "confidence": settled.confidence,
                **dict(settled.observed_result),
            },
        },
        "replanned": {
            "status": "terminal-screen",
            "current_state": None,
            "terminal": actual_terminal,
        },
        "differences": {
            "action_application": [],
            "terminal_prediction_vs_actual": [],
            "terminal_prediction_replan": [],
        },
    }


def build_plan3_missing_local_save_terminal_artifact(
    *,
    advisor_mode: str,
    before_report: Plan3AdvisorReport,
    plan: Plan3AuditionExecutionPlan,
    settled: Plan3MissingLocalSaveTerminalAction,
) -> dict[str, object]:
    """Record a NIA terminal boundary without fabricating result numbers."""

    if plan.kind != settled.action_kind:
        raise ValueError("missing-ExamSave terminal action differs from plan")
    if plan.kind not in {"card", "skip"}:
        raise ValueError("only a card or skip action can end a NIA exam")
    action_evidence = {
        "kind": plan.kind,
        "exact_action_input_bound": True,
        "outcome_authority": settled.authority,
    }
    if plan.kind == "card":
        action_evidence["selected_guid"] = str(plan.action["card_guid"])
    local_save = {
        "turns_remaining": None,
        "plays_remaining": None,
        "score": None,
        "stamina": None,
        "max_stamina": None,
        "block": None,
        "executor_actionable_settled": False,
        "executor_terminal": True,
        "executor_completed_card_replay": False,
        "executor_completed_card_replay_provenance": None,
        "executor_terminal_card_replay": False,
        "executor_terminal_card_replay_provenance": None,
        "local_save_boundary": "removed-by-game-after-bound-action",
        "terminal_authority": settled.authority,
    }
    predicted_terminal = before_report.terminal
    return {
        "schema": "gkms_tool.plan3_executor_prediction_actual",
        "schema_version": 1,
        "advisor_mode": _normalize_advisor_mode(advisor_mode),
        "action": plan.to_dict(),
        "prediction": {
            "current_state": (
                None
                if before_report.current_state is None
                else dict(before_report.current_state)
            ),
            "terminal": (
                dict(predicted_terminal)
                if isinstance(predicted_terminal, Mapping)
                else None
            ),
            "action_expectation": dict(action_evidence),
        },
        "actual": {
            "local_save": local_save,
            "terminal": True,
            # The next outer/result page owns the observed score and attribute
            # result.  Do not turn a solver forecast into an observed value.
            "terminal_state": None,
            "action_evidence": dict(action_evidence),
            "result_screen": None,
        },
        "replanned": {
            "status": "terminal-exam-save-removed",
            "current_state": None,
            "terminal": None,
        },
        "differences": {
            "action_application": [],
            "terminal_prediction_vs_actual": [],
            "terminal_prediction_replan": [],
        },
    }


class Plan3LocalSaveOutcomeProbe:
    """Wait for one changed LocalSave that proves the planned action settled."""

    def __init__(
        self,
        path: Path,
        before_signature: Plan3LocalSaveSignature,
        before: DecodedPlan3LocalSave,
        plan: Plan3AuditionExecutionPlan,
        advisor_factory: AdvisorFactory,
        *,
        require_completed_card_replay: bool = False,
        prior_completed_card_replay: Plan3CompletedCardReplay | None = None,
        card_replay_resolver: CardReplayResolver | None = None,
        replay_advisor_factory: ReplayAdvisorFactory | None = None,
        accept_missing_local_save_as_terminal: bool = False,
    ) -> None:
        if not isinstance(require_completed_card_replay, bool):
            raise TypeError("require_completed_card_replay must be bool")
        if prior_completed_card_replay is not None and not isinstance(
            prior_completed_card_replay, Plan3CompletedCardReplay
        ):
            raise TypeError(
                "prior_completed_card_replay must be Plan3CompletedCardReplay"
            )
        if (
            require_completed_card_replay
            and prior_completed_card_replay is not None
        ):
            raise ValueError(
                "require_completed_card_replay cannot be combined with a prior replay"
            )
        if type(accept_missing_local_save_as_terminal) is not bool:
            raise TypeError(
                "accept_missing_local_save_as_terminal must be boolean"
            )
        self.path = path
        self.before_signature = before_signature
        self.before = before
        self.plan = plan
        self.advisor_factory = advisor_factory
        self.require_completed_card_replay = require_completed_card_replay
        self.prior_completed_card_replay = prior_completed_card_replay
        self.card_replay_resolver = card_replay_resolver
        self.replay_advisor_factory = replay_advisor_factory
        self.accept_missing_local_save_as_terminal = (
            accept_missing_local_save_as_terminal
        )
        self._missing_local_save_terminal_authorized = False
        self._missing_local_save_observations = 0
        self._cached: SettledPlan3Action | None = None
        self._terminal_prediction_resolved = False
        self._terminal_prediction: Plan3TerminalCardReplay | None = None
        self._screen_terminal: Plan3ScreenTerminalAction | None = None

    def authorize_missing_local_save_terminal(self) -> None:
        """Allow NIA-only removal acceptance after the driver returned."""

        if not self.accept_missing_local_save_as_terminal:
            return
        if self.plan.kind not in {"card", "skip"}:
            return
        self._missing_local_save_terminal_authorized = True

    def terminal_card_replay(self) -> Plan3TerminalCardReplay | None:
        """Return the exact last-card prediction, if this action force-ends."""

        if self._terminal_prediction_resolved:
            return self._terminal_prediction
        self._terminal_prediction_resolved = True
        if self.prior_completed_card_replay is None or self.plan.kind != "card":
            return None
        try:
            self._terminal_prediction = predict_terminal_next_plan3_card_history(
                self.prior_completed_card_replay,
                self.before,
                str(self.plan.action["card_guid"]),
            )
        except (KeyError, OSError, TypeError, ValueError):
            self._terminal_prediction = None
        return self._terminal_prediction

    def accept_terminal_screen_samples(
        self,
        samples: Sequence[CardPlayFrameSample],
    ) -> bool:
        """Bind two stable MAA result frames to an exact terminal prediction."""

        prediction = self.terminal_card_replay()
        if (
            prediction is None
            or read_plan3_local_save_signature(self.path) is not None
        ):
            return False
        if len(samples) < 2:
            return False
        first, second = samples[-2:]
        analyses = (first.analysis, second.analysis)
        if any(
            value.phase != "result"
            or value.expected_matches is not True
            or value.semantic_key is None
            or value.confidence < 0.90
            for value in analyses
        ):
            return False
        if analyses[0].semantic_key != analyses[1].semantic_key:
            return False
        if (
            dict(analyses[0].observed_state) != dict(analyses[1].observed_state)
            or dict(analyses[0].verified_state)
            != dict(analyses[1].verified_state)
        ):
            return False
        verified = analyses[1].verified_state
        if (
            verified.get("source") != prediction.provenance
            or verified.get("card_guid") != prediction.card_guid
            or verified.get("terminal") is not True
            or verified.get("score") != prediction.after.score
            or verified.get("stamina") != prediction.after.stamina
            or verified.get("block") != prediction.after.block
            or verified.get("turns_remaining") != 0
            or verified.get("plays_remaining") != 0
        ):
            return False
        observed = analyses[1].observed_state.get("pursuit_result")
        if not isinstance(observed, Mapping):
            return False
        required = {
            "vocal",
            "dance",
            "visual",
            "vocal_delta",
            "dance_delta",
            "visual_delta",
            "vocal_rank",
            "dance_rank",
            "visual_rank",
        }
        if not required <= set(observed):
            return False
        numeric_result_fields = (
            "vocal",
            "dance",
            "visual",
            "vocal_delta",
            "dance_delta",
            "visual_delta",
        )
        if any(
            not isinstance(observed.get(key), int)
            or isinstance(observed.get(key), bool)
            or int(observed[key]) < 0
            for key in numeric_result_fields
        ):
            return False
        if any(
            observed.get(key)
            not in {
                "S",
                "S+",
                "A",
                "A+",
                "B",
                "B+",
                "C",
                "C+",
                "D",
                "D+",
                "E",
                "E+",
                "F",
                "F+",
            }
            for key in ("vocal_rank", "dance_rank", "visual_rank")
        ):
            return False
        try:
            paths = tuple(
                str(Path(_capture_path(sample.capture)).resolve())
                for sample in (first, second)
            )
        except (FileNotFoundError, RuntimeError, TypeError, ValueError):
            return False
        if len(paths) != 2:
            return False
        first_time = first.capture.get("timestamp")
        second_time = second.capture.get("timestamp")
        first_hwnd = first.capture.get("hwnd")
        first_pid = first.capture.get("pid")
        if (
            paths[0] == paths[1]
            or not isinstance(first_time, (int, float))
            or isinstance(first_time, bool)
            or not isinstance(second_time, (int, float))
            or isinstance(second_time, bool)
            or float(second_time) <= float(first_time)
            or not isinstance(first_hwnd, int)
            or isinstance(first_hwnd, bool)
            or first_hwnd <= 0
            or not isinstance(first_pid, int)
            or isinstance(first_pid, bool)
            or first_pid <= 0
            or first_hwnd != second.capture.get("hwnd")
            or first_pid != second.capture.get("pid")
        ):
            return False
        self._screen_terminal = Plan3ScreenTerminalAction(
            terminal_replay=prediction,
            observed_result=dict(observed),
            capture_paths=(paths[0], paths[1]),
            confidence=min(value.confidence for value in analyses),
        )
        return True

    def probe(self) -> Plan3SettledOutcome | None:
        if self._screen_terminal is not None:
            return self._screen_terminal
        signature = read_plan3_local_save_signature(self.path)
        if signature is None:
            if self._missing_local_save_terminal_authorized:
                self._missing_local_save_observations += 1
                if self._missing_local_save_observations >= 2:
                    return Plan3MissingLocalSaveTerminalAction(self.plan.kind)
            return None
        self._missing_local_save_observations = 0
        if signature == self.before_signature:
            return None
        if self._cached is not None and self._cached.signature == signature:
            return self._cached
        try:
            decoded = decode_plan3_local_save_file(self.path)
        except Exception:
            return None
        completed_card_replay: Plan3CompletedCardReplay | None = None
        action_applied = _action_applied(self.before, decoded, self.plan)
        if self.prior_completed_card_replay is not None:
            if self.plan.kind != "card":
                return None
            try:
                completed_card_replay = (
                    self.card_replay_resolver(
                        self.before,
                        decoded,
                        self.plan,
                        self.prior_completed_card_replay,
                    )
                    if self.card_replay_resolver is not None
                    else replay_next_completed_plan3_card_history(
                        self.prior_completed_card_replay,
                        self.before,
                        decoded,
                        str(self.plan.action["card_guid"]),
                    )
                )
            except (KeyError, OSError, TypeError, ValueError):
                return None
            if completed_card_replay is None:
                return None
        elif self.require_completed_card_replay or not action_applied:
            if self.plan.kind != "card":
                return None
            try:
                completed_card_replay = (
                    self.card_replay_resolver(
                        self.before,
                        decoded,
                        self.plan,
                        None,
                    )
                    if self.card_replay_resolver is not None
                    else replay_completed_plan3_card_history(
                        self.before,
                        decoded,
                        str(self.plan.action["card_guid"]),
                    )
                )
            except (KeyError, OSError, TypeError, ValueError):
                return None
            if completed_card_replay is None:
                return None
        if (
            completed_card_replay is not None
            and self.replay_advisor_factory is not None
        ):
            advisor = self.replay_advisor_factory(
                decoded,
                completed_card_replay,
            )
        elif completed_card_replay is not None and decoded.exam_state.exam_type == 0:
            advisor = advise_plan3_lesson_decoded(
                decoded,
                source=str(self.path.resolve()),
                transition_replay=completed_card_replay,
            )
        else:
            advisor = self.advisor_factory(decoded)
        state = decoded.exam_state
        settled = bool(
            completed_card_replay is not None
            or is_plan3_exam_actionable_settled(state)
            or _terminal_state(state)
            or advisor.available
        )
        if not settled:
            return None
        self._cached = SettledPlan3Action(
            signature,
            decoded,
            advisor,
            completed_card_replay,
        )
        return self._cached

    def wait(
        self,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> Plan3SettledOutcome:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        deadline = monotonic() + timeout_seconds
        while True:
            outcome = self.probe()
            if outcome is not None:
                return outcome
            if monotonic() >= deadline:
                raise TimeoutError("LocalSave did not reach a changed settled state")
            sleep(poll_interval_seconds)


class _RecordingCommandSender:
    def __init__(
        self,
        sender: Callable[..., Mapping[str, Any]],
        *,
        source_path: Path,
        before_signature: Plan3LocalSaveSignature,
        trace: list[Mapping[str, object]],
    ) -> None:
        self.sender = sender
        self.source_path = source_path
        self.before_signature = before_signature
        self.trace = trace
        self.pending_labels: list[str] = []
        self._preauthorized_inputs = 0

    def prepare(self, *labels: str) -> None:
        self.pending_labels = list(labels)

    def authorize_input_batch(self, count: int) -> None:
        """Perform one ExamSave CAS for one fixed multi-click game action."""

        if type(count) is not int or count < 1:
            raise ValueError("input batch count must be a positive integer")
        if self._preauthorized_inputs:
            raise RuntimeError("an input batch is already active")
        current = read_plan3_local_save_signature(self.source_path)
        if current != self.before_signature:
            raise StaleSuggestionError(
                "LocalSave changed before the controller input batch"
            )
        self._preauthorized_inputs = count

    def __call__(self, command: str, **payload: Any) -> Mapping[str, Any]:
        is_input = command == "send_input_click_once"
        if is_input:
            if self._preauthorized_inputs:
                self._preauthorized_inputs -= 1
            else:
                current = read_plan3_local_save_signature(self.source_path)
                if current != self.before_signature:
                    raise StaleSuggestionError(
                        "LocalSave changed before the next controller input"
                    )
        result = dict(self.sender(command, **payload))
        if command == "status" and not bool(result.get("background_control")):
            raise RuntimeError("Plan3 execution requires MAA background control")
        if is_input:
            label = self.pending_labels.pop(0) if self.pending_labels else "input"
            self.trace.append(
                {
                    "step": label,
                    "command": command,
                    "window_x": payload.get("window_x"),
                    "window_y": payload.get("window_y"),
                    "controller_result": result,
                }
            )
        return result


class MaaPlan3ActionDriver:
    """Translate one bound plan into verified background-controller inputs."""

    def __init__(
        self,
        *,
        command_sender: Callable[..., Mapping[str, Any]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        skip_confirmation_poll_seconds: float = SKIP_CONFIRM_POLL_INTERVAL_SECONDS,
        skip_confirmation_max_samples: int = SKIP_CONFIRM_MAX_SAMPLES,
        skip_confirmation_stable_samples: int = SKIP_CONFIRM_STABLE_SAMPLES,
        ordered_hand_slot_authority: bool = False,
    ) -> None:
        if command_sender is None:
            from .controller_client import send_command

            command_sender = send_command
        self.command_sender = command_sender
        self.sleep = sleep
        if skip_confirmation_poll_seconds <= 0:
            raise ValueError("skip_confirmation_poll_seconds must be positive")
        if skip_confirmation_max_samples < 1:
            raise ValueError("skip_confirmation_max_samples must be positive")
        if not 1 <= skip_confirmation_stable_samples <= skip_confirmation_max_samples:
            raise ValueError(
                "skip_confirmation_stable_samples must be within max samples"
            )
        self.skip_confirmation_poll_seconds = skip_confirmation_poll_seconds
        self.skip_confirmation_max_samples = skip_confirmation_max_samples
        self.skip_confirmation_stable_samples = skip_confirmation_stable_samples
        if type(ordered_hand_slot_authority) is not bool:
            raise TypeError("ordered_hand_slot_authority must be boolean")
        self.ordered_hand_slot_authority = ordered_hand_slot_authority

    def _capture(self, sender: _RecordingCommandSender) -> Mapping[str, object]:
        capture = dict(sender("capture_screen_once", timeout=15.0))
        _capture_path(capture)
        for field in ("timestamp", "hwnd", "pid"):
            if field not in capture:
                raise ValueError(f"controller capture is missing {field}")
        return capture

    def _wait_for_skip_confirmation(
        self,
        sender: _RecordingCommandSender,
        initial_capture: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        """Return a stable Turn End modal, or stop if LocalSave already moved.

        A SKIP click may first yield an animation frame.  Never infer that a
        missing modal means that the affirmative button is safe: poll bounded
        background captures and require the localized title *and* affirmative
        label in consecutive frames.  Any LocalSave disappearance/change means
        the first click already moved state (or evidence is no longer bound),
        so a second input is forbidden.
        """

        capture = initial_capture
        stable_key: tuple[str, str] | None = None
        stable_count = 0
        for sample_index in range(self.skip_confirmation_max_samples):
            signature = read_plan3_local_save_signature(sender.source_path)
            if signature != sender.before_signature:
                return None
            if sample_index:
                self.sleep(self.skip_confirmation_poll_seconds)
                signature = read_plan3_local_save_signature(sender.source_path)
                if signature != sender.before_signature:
                    return None
                capture = self._capture(sender)

            evidence = _skip_confirmation_evidence(_capture_path(capture))
            if not evidence.detected:
                stable_key = None
                stable_count = 0
                continue
            key = (
                _normalize_ui_text(evidence.title_text),
                _normalize_ui_text(evidence.yes_text),
            )
            if key == stable_key:
                stable_count += 1
            else:
                stable_key = key
                stable_count = 1
            if stable_count >= self.skip_confirmation_stable_samples:
                return capture
        return None

    def _click(
        self,
        sender: _RecordingCommandSender,
        capture: Mapping[str, object],
        *,
        label: str,
        box: CanonicalBox,
        trace_label: str,
        require_visual_change: bool,
    ) -> ClickExecutionResult:
        sender.prepare(trace_label)
        result = execute_suggested_click(
            _action_for_box(capture, label, box),
            command_sender=sender,
            sleep=self.sleep,
            max_age_seconds=30.0,
        )
        if require_visual_change and not result.visual_change_detected:
            raise RuntimeError(f"{trace_label} produced no visible UI change")
        return result

    def _verified_hand_boxes(
        self,
        capture: Mapping[str, object],
        state: LocalSaveExamState,
        *,
        target_index: int | None = None,
    ) -> tuple[CanonicalBox, ...]:
        if self.ordered_hand_slot_authority:
            hand_count = len(state.zones.hand)
            boxes = FIXED_HAND_SLOT_BOXES.get(hand_count)
            if boxes is None:
                raise ValueError(
                    "ExamSave ordered Hand count is outside the fixed 1..5 layout"
                )
            if target_index is not None and not 0 <= target_index < hand_count:
                raise ValueError("target Hand index is outside ExamSave ordered Hand")
            # N.I.A. uses ExamSaveData as the complete card/GUID/order
            # authority.  The visible cards have fixed equal-sized slots, so
            # artwork, localized text, colour and support '+' pixels are not
            # consulted here.
            return boxes

        from .live_source import (
            _canonical_detection_box,
            _card_detector,
            _deduplicate_card_detections,
            _lesson_card_layout_issue,
        )

        path = _capture_path(capture)
        report = _card_detector().detect_path(path)
        detections = tuple(
            sorted(
                _deduplicate_card_detections(
                    tuple(
                        item
                        for item in report.detections
                        if item.label in {"cards", "recommend", "useless"}
                        and item.y >= report.image_height * 0.62
                    )
                ),
                key=lambda item: item.x,
            )
        )
        issue = _lesson_card_layout_issue(report, detections)
        if issue is not None:
            raise ValueError(issue)
        if len(detections) != len(state.zones.hand):
            raise ValueError("visible Hand count differs from LocalSave")
        boxes = tuple(
            _canonical_detection_box(report, detection)
            for detection in detections
        )
        if target_index is None:
            art_boxes = tuple(_hand_art_box(box) for box in boxes)
            cards = state.zones.hand
        else:
            if not 0 <= target_index < len(boxes):
                raise ValueError("target Hand index is outside the visible Hand")
            # A normal card action is already bound to an exact LocalSave GUID
            # and ordered Hand index.  Only the clicked slot needs artwork
            # confirmation; an unrelated slot with similar art must not block
            # the action.  Skip/drink paths still pass ``None`` and retain the
            # complete visible-Hand order check used by their fixed controls.
            art_boxes = (_hand_art_box(boxes[target_index]),)
            cards = (state.zones.hand[target_index],)
        observed = _observed_card_suffixes(
            path,
            art_boxes,
            cards,
            character_id=state.character_id,
        )
        expected = tuple(_card_suffix(card.card_id) for card in cards)
        if observed != expected:
            label = "target Hand card" if target_index is not None else "visible Hand order"
            raise ValueError(f"{label} differs from LocalSave")
        return boxes

    def _send_fixed_input(
        self,
        sender: _RecordingCommandSender,
        capture: Mapping[str, object],
        *,
        box: CanonicalBox,
        trace_label: str,
    ) -> None:
        """Send one MAA background click bound only by window + ExamSave CAS."""

        status = dict(sender("status"))
        if int(status.get("controller_version", 0)) < 7:
            raise RuntimeError("elevated controller is too old for fixed input")
        if int(status.get("target_hwnd", 0)) != int(capture["hwnd"]):
            raise StaleSuggestionError("controller is bound to another game window")
        if int(status.get("target_pid", 0)) != int(capture["pid"]):
            raise StaleSuggestionError("controller is bound to another game process")
        geometry = status.get("geometry")
        if not isinstance(geometry, Mapping):
            raise RuntimeError("controller status is missing window geometry")
        window_x, window_y = canonical_to_outer_window(
            (box[0] + box[2]) // 2,
            (box[1] + box[3]) // 2,
            geometry,
        )
        sender.prepare(trace_label)
        sender(
            "send_input_click_once",
            window_x=window_x,
            window_y=window_y,
        )

    def _execute_ordered_hand_card(
        self,
        sender: _RecordingCommandSender,
        capture: Mapping[str, object],
        box: CanonicalBox,
    ) -> None:
        """Select/commit a fixed ordered-Hand slot, then defer to ExamSave.

        The caller already bound the exact GUID and zero-based Hand index to a
        settled ExamSave.  Re-reading card art, text, support-marker colour or
        SELECT OCR would be duplicate identity validation.  Each irreversible
        MAA input still performs the durable ExamSave compare-and-swap in
        ``_RecordingCommandSender``; the outer executor then accepts the
        action only after a changed settled ExamSave is decoded.
        """

        # Selecting and committing the same fixed Hand slot is one semantic
        # card action.  ExamSave owns the ordered GUID and is compared once
        # before the batch; re-reading the same file between these two Maa
        # inputs was duplicate validation.  Any later optional modal click is
        # outside this batch and receives its own normal CAS.
        sender.authorize_input_batch(2)
        self._send_fixed_input(
            sender,
            capture,
            box=box,
            trace_label="card-select",
        )
        self.sleep(0.5)
        self._send_fixed_input(
            sender,
            capture,
            box=box,
            trace_label="card-commit-same-slot",
        )

        # An effect-capped skill may open a separate affirmative modal instead
        # of committing immediately.  This single optional read is page
        # handling only; it is not used to identify the card or to decide
        # whether the two card inputs were successful.  The changed ExamSave
        # remains the sole outcome authority.
        self.sleep(0.35)
        if read_plan3_local_save_signature(sender.source_path) != sender.before_signature:
            return
        confirmation_capture = self._capture(sender)
        confirmation = _card_use_confirmation_evidence(
            _capture_path(confirmation_capture)
        )
        if confirmation.detected:
            self._send_fixed_input(
                sender,
                confirmation_capture,
                box=CARD_USE_CONFIRM_BUTTON_BOX,
                trace_label="card-confirm-use",
            )

    def _selection_action(
        self,
        capture: Mapping[str, object],
        plan: Plan3AuditionExecutionPlan,
        state: LocalSaveExamState,
    ) -> SuggestedClick:
        selected_guid = str(plan.action["selected_card_guid"])
        eligible = (*state.zones.deck, *state.zones.grave)
        boxes = CARD_SELECTION_GRID_BOXES[: len(eligible)]
        if len(boxes) != len(eligible):
            raise ValueError(
                "drink selection contains more cards than the fixed grid"
            )
        if self.ordered_hand_slot_authority:
            matches = tuple(
                index
                for index, card in enumerate(eligible)
                if card.guid == selected_guid
            )
            if len(matches) != 1:
                raise ValueError(
                    "drink target GUID is not unique in ExamSave Deck+Grave"
                )
            # N.I.A. uses the same authority as ordinary Hand play: the
            # serialized ordered zones own card identity and the selection
            # grid has fixed slots.  Card artwork, localized text and support
            # marker pixels are not a second identity gate.
            index = matches[0]
        else:
            observed = _observed_card_suffixes(
                _capture_path(capture),
                boxes,
                eligible,
                character_id=state.character_id,
            )
            index = selection_grid_index_for_guid(state, selected_guid, observed)
        return _action_for_box(
            capture,
            "Plan3 advisor drink target",
            boxes[index],
        )

    def _card_analyzer(
        self,
        action: SuggestedClick,
        plan: Plan3AuditionExecutionPlan,
        probe: Plan3LocalSaveOutcomeProbe,
        before_state: LocalSaveExamState,
    ) -> Callable[[Path], CardPlayFrameAnalysis]:
        expected_id = str(plan.action["card_id"])
        expected_upgrade = int(plan.action["card_upgrade"])

        def analyze(path: Path) -> CardPlayFrameAnalysis:
            # A force-ended pursuit lesson can delete ExamSaveData before a
            # terminal LocalSave is ever observable.  Test this narrow branch
            # before unrelated SELECT-preview OCR: the verified click state
            # machine still requires the same result on two captures.
            probe_path = getattr(probe, "path", None)
            local_save_missing = bool(
                isinstance(probe_path, Path)
                and read_plan3_local_save_signature(probe_path) is None
            )
            terminal_predictor = getattr(probe, "terminal_card_replay", None)
            terminal_prediction = (
                terminal_predictor()
                if local_save_missing and callable(terminal_predictor)
                else None
            )
            if terminal_prediction is not None:
                try:
                    from .pursuit_lesson_result import (
                        read_pursuit_lesson_result_path,
                    )

                    result = read_pursuit_lesson_result_path(
                        path,
                        minimum_confidence=0.90,
                    )
                except (OSError, RuntimeError, TypeError, ValueError):
                    result = None
                if result is not None:
                    result_payload = result.to_dict()
                    terminal = terminal_prediction.after
                    return CardPlayFrameAnalysis(
                        phase="result",
                        semantic_key=(
                            terminal_prediction.provenance,
                            terminal_prediction.card_guid,
                            result.vocal,
                            result.dance,
                            result.visual,
                            result.vocal_delta,
                            result.dance_delta,
                            result.visual_delta,
                            result.vocal_rank,
                            result.dance_rank,
                            result.visual_rank,
                        ),
                        observed_state={"pursuit_result": result_payload},
                        verified_state={
                            "source": terminal_prediction.provenance,
                            "card_guid": terminal_prediction.card_guid,
                            "chain_depth": terminal_prediction.chain_depth,
                            "chain_card_guids": list(
                                terminal_prediction.chain_card_guids
                            ),
                            "terminal": True,
                            "score": terminal.score,
                            "stamina": terminal.stamina,
                            "block": terminal.block,
                            "turns_remaining": terminal.turns_remaining,
                            "plays_remaining": terminal.plays_remaining,
                        },
                        expected_matches=True,
                        confidence=result.confidence,
                    )
                return CardPlayFrameAnalysis(phase="resolving")

            try:
                from .live_source import (
                    _card_preview_evidence,
                    _live_text_recognizer,
                    _preview_card_identity_evidence,
                )

                recognizer = _live_text_recognizer()
                preview = _card_preview_evidence(
                    path,
                    recognizer,
                    expected_card_x=action.canonical_x,
                )
                if preview.detected:
                    if self.ordered_hand_slot_authority:
                        return CardPlayFrameAnalysis(
                            phase="selected_preview",
                            observed_state={
                                "selected_hand_index": int(
                                    plan.action["hand_index"]
                                ),
                                "selected_identity_source": (
                                    "exam-save-ordered-hand-slot"
                                ),
                            },
                            selected_card_matches=True,
                            confidence=preview.select_confidence,
                        )
                    identity = _preview_card_identity_evidence(
                        path,
                        recognizer,
                        expected_card_id=expected_id,
                        expected_upgrade=expected_upgrade,
                    )
                    # A resolved conflicting title remains a hard mismatch.
                    # Static art is the fallback only when localized OCR did
                    # not resolve any card ID at all.
                    art = (
                        _preview_card_art_evidence(
                            path,
                            _hand_art_box(action.verification_box),
                            before_state.zones.hand,
                            expected_card_id=expected_id,
                            character_id=before_state.character_id,
                        )
                        if identity.card_id is None
                        else None
                    )
                    art_fallback = bool(
                        art is not None and art.matches_expected
                    )
                    matches = bool(identity.matches_expected or art_fallback)
                    identity_source = (
                        "localized-title"
                        if identity.matches_expected
                        else "static-art"
                        if art_fallback
                        else "unresolved"
                    )
                    confidence = min(
                        preview.select_confidence,
                        (
                            min(art.score, art.margin)
                            if art_fallback and art is not None
                            else min(
                                identity.ocr_confidence,
                                identity.name_similarity,
                            )
                        ),
                    )
                    return CardPlayFrameAnalysis(
                        phase="selected_preview",
                        observed_state={
                            "selected_card_id": (
                                expected_id if art_fallback else identity.card_id
                            ),
                            "selected_identity_source": identity_source,
                            "localized_title_text": identity.observed_text,
                            "artwork_suffix": (
                                None if art is None else art.observed_suffix
                            ),
                            "artwork_score": None if art is None else art.score,
                            "artwork_margin": (
                                None if art is None else art.margin
                            ),
                        },
                        selected_card_matches=matches,
                        confidence=confidence,
                        issues=(
                            ()
                            if matches
                            else ("selected-card-identity-mismatch",)
                        ),
                    )
            except Exception as error:
                return CardPlayFrameAnalysis(
                    phase="failed",
                    issues=(f"preview-analysis:{type(error).__name__}:{error}",),
                )

            outcome = probe.probe()
            if outcome is None:
                return CardPlayFrameAnalysis(phase="resolving")
            settled_state = outcome.decoded.exam_state
            terminal = _terminal_state(settled_state)
            replay = outcome.completed_card_replay
            if replay is not None:
                logical = replay.after
                semantic_key = (
                    replay.provenance,
                    replay.card_guid,
                    logical.round_number,
                    logical.turns_remaining,
                    logical.plays_remaining,
                    logical.score,
                    logical.stamina,
                    logical.block,
                )
                observed_state = {
                    "turns_remaining": logical.turns_remaining,
                    "plays_remaining": logical.plays_remaining,
                    "score": logical.score,
                    "stamina": logical.stamina,
                    "block": logical.block,
                }
                verified_state = {
                    "source": replay.provenance,
                    "card_guid": replay.card_guid,
                    "effect_ids": list(replay.effect_ids),
                    "chain_depth": replay.chain_depth,
                }
            else:
                semantic_key = _state_semantic_key(settled_state)
                observed_state = {
                    "turns_remaining": settled_state.remain_turn,
                    "score": settled_state.score,
                    "stamina": settled_state.stamina,
                    "block": settled_state.block,
                }
                verified_state = {"source": "changed-settled-localsave"}
            return CardPlayFrameAnalysis(
                phase="result" if terminal else "settled",
                semantic_key=semantic_key,
                observed_state=observed_state,
                verified_state=verified_state,
                expected_matches=True,
                confidence=1.0,
            )

        return analyze

    def execute(
        self,
        plan: Plan3AuditionExecutionPlan,
        decoded: DecodedPlan3LocalSave,
        source_path: Path,
        before_signature: Plan3LocalSaveSignature,
        probe: Plan3LocalSaveOutcomeProbe,
        trace: list[Mapping[str, object]],
    ) -> None:
        sender = _RecordingCommandSender(
            self.command_sender,
            source_path=source_path,
            before_signature=before_signature,
            trace=trace,
        )
        state = decoded.exam_state
        capture = self._capture(sender)
        if plan.kind == "card":
            hand_index = int(plan.action["hand_index"])
            hand_boxes = self._verified_hand_boxes(
                capture,
                state,
                target_index=hand_index,
            )
            action = _action_for_box(
                capture,
                "Plan3 advisor card",
                hand_boxes[hand_index],
            )
            if self.ordered_hand_slot_authority:
                self._execute_ordered_hand_card(
                    sender,
                    capture,
                    hand_boxes[hand_index],
                )
                return
            sender.prepare("card-select", "card-commit-same-slot")
            execution: CardPlayExecutionResult = execute_verified_card_click(
                action,
                self._card_analyzer(action, plan, probe, state),
                command_sender=sender,
                sleep=self.sleep,
                max_age_seconds=30.0,
                stable_sample_seconds=0.5,
                max_settle_samples=32,
            )
            if not execution.committed:
                execution_payload = (
                    execution.to_dict()
                    if hasattr(execution, "to_dict")
                    else {
                        "committed": False,
                        "failure_reason": execution.failure_reason,
                    }
                )
                if (
                    execution.failure_reason == "settled-frame-timeout"
                    and read_plan3_local_save_signature(source_path)
                    != before_signature
                ):
                    timeout = Plan3SettlementFrameTimeout(
                        {
                            "failure_reason": execution.failure_reason,
                            "card_play_execution": execution_payload,
                            "confirmation_skipped": "local-save-changed",
                        }
                    )
                    trace.append(timeout.trace_entry())
                    raise timeout
                confirmation_capture = self._capture(sender)
                confirmation = _card_use_confirmation_evidence(
                    _capture_path(confirmation_capture)
                )
                if confirmation.detected:
                    self._click(
                        sender,
                        confirmation_capture,
                        label="Plan3 advisor confirm skill card use",
                        box=CARD_USE_CONFIRM_BUTTON_BOX,
                        trace_label="card-confirm-use",
                        require_visual_change=False,
                    )
                    # The outer executor now waits for a changed, settled
                    # LocalSave and performs the normal prediction/actual
                    # comparison.  No further screen-driven input is needed.
                    return
                if execution.failure_reason == "settled-frame-timeout":
                    timeout = Plan3SettlementFrameTimeout(
                        {
                            "failure_reason": execution.failure_reason,
                            "card_play_execution": execution_payload,
                            "confirmation_capture": dict(
                                confirmation_capture
                            ),
                            "card_use_confirmation": {
                                "detected": confirmation.detected,
                                "title_text": confirmation.title_text,
                                "title_confidence": (
                                    confirmation.title_confidence
                                ),
                                "confirm_text": confirmation.confirm_text,
                                "confirm_confidence": (
                                    confirmation.confirm_confidence
                                ),
                            },
                        }
                    )
                    trace.append(timeout.trace_entry())
                    # The SELECT and COMMIT inputs have already been sent.
                    # Only the outer LocalSave probe may recover this result;
                    # the driver must never click the card again.
                    raise timeout
                raise RuntimeError(
                    "card select/commit did not settle: "
                    f"{execution.failure_reason}"
                )
            final_analysis = execution.phase_samples[-1].analysis
            if (
                final_analysis.phase == "result"
                and final_analysis.verified_state.get("source")
                == "terminal-card-replay-chain"
                and not probe.accept_terminal_screen_samples(
                    execution.phase_samples
                )
            ):
                raise RuntimeError(
                    "terminal result frames did not bind to the exact "
                    "missing-LocalSave prediction"
                )
            return

        if plan.kind == "skip":
            skipped = self._click(
                sender,
                capture,
                label="Plan3 advisor skip",
                box=SKIP_BOX,
                trace_label="skip-turn",
                require_visual_change=False,
            )
            confirmation_capture = self._wait_for_skip_confirmation(
                sender,
                skipped.post_capture,
            )
            if confirmation_capture is not None:
                self._click(
                    sender,
                    confirmation_capture,
                    label="Plan3 advisor confirm turn end",
                    box=SKIP_CONFIRM_YES_BOX,
                    trace_label="skip-confirm-yes",
                    require_visual_change=False,
                )
            return

        if plan.kind != "drink":
            raise ValueError(f"unknown execution plan kind: {plan.kind}")
        slot = int(plan.action["drink_slot_index"])
        opened = self._click(
            sender,
            capture,
            label="Plan3 advisor drink slot",
            box=DRINK_SLOT_BOXES[slot],
            trace_label="drink-open-slot",
            require_visual_change=True,
        )
        used = self._click(
            sender,
            opened.post_capture,
            label="Plan3 advisor use drink",
            box=DRINK_USE_BOX,
            trace_label="drink-confirm-use",
            require_visual_change=True,
        )
        if plan.action.get("selected_card_guid") is None:
            return
        selection = self._selection_action(used.post_capture, plan, state)
        sender.prepare("drink-select-card-guid")
        selected = execute_suggested_click(
            selection,
            command_sender=sender,
            sleep=self.sleep,
            max_age_seconds=30.0,
        )
        if not selected.visual_change_detected:
            raise RuntimeError("drink target selection produced no visible UI change")
        self._click(
            sender,
            selected.post_capture,
            label="Plan3 advisor commit drink selection",
            box=CARD_SELECTION_COMMIT_BOX,
            trace_label="drink-commit-selection",
            require_visual_change=False,
        )


def _bound_decoded_snapshot(
    path: Path,
) -> tuple[Plan3LocalSaveSignature, DecodedPlan3LocalSave]:
    for _attempt in range(2):
        before = read_plan3_local_save_signature(path)
        if before is None:
            raise FileNotFoundError(path)
        data = path.read_bytes()
        after = read_plan3_local_save_signature(path)
        if before == after:
            return before, decode_plan3_local_save_bytes(data)
    raise RuntimeError("LocalSave changed while preparing the action")


def _bound_snapshot(
    path: Path,
    advisor_factory: AdvisorFactory,
) -> tuple[
    Plan3LocalSaveSignature,
    DecodedPlan3LocalSave,
    Plan3AdvisorReport,
]:
    signature, decoded = _bound_decoded_snapshot(path)
    return signature, decoded, advisor_factory(decoded)


class _Plan3BootstrapUnavailable(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _completed_card_plan(
    before: DecodedPlan3LocalSave,
    card_guid: str,
) -> Plan3AuditionExecutionPlan:
    matches = tuple(
        (index, card)
        for index, card in enumerate(before.exam_state.zones.hand)
        if card.guid == card_guid
    )
    if len(matches) != 1:
        raise _Plan3BootstrapUnavailable(
            "bootstrap-card-guid-unavailable",
            f"prior settled Hand contains {len(matches)} matches for {card_guid}",
        )
    index, card = matches[0]
    return Plan3AuditionExecutionPlan(
        kind="card",
        action={
            "kind": "card",
            "hand_index": index,
            "card_guid": card.guid,
            "card_id": card.card_id,
            "card_upgrade": card.effective_upgrade,
        },
        # This is evidence for an action that already happened.  No UI step
        # from this plan is ever executed.
        ui_steps=(),
    )


def _bootstrap_bound_snapshot(
    path: Path,
    *,
    prior_settled_path: Path,
    completed_card_guid: str,
    advisor_factory: AdvisorFactory,
) -> tuple[
    Plan3LocalSaveSignature,
    DecodedPlan3LocalSave,
    Plan3AdvisorReport,
    Plan3CompletedCardReplay,
]:
    try:
        prior_signature, prior = _bound_decoded_snapshot(prior_settled_path)
    except Exception as error:
        raise _Plan3BootstrapUnavailable(
            "bootstrap-prior-unavailable",
            f"{type(error).__name__}:{error}",
        ) from error
    if not is_plan3_exam_actionable_settled(prior.exam_state):
        raise _Plan3BootstrapUnavailable(
            "bootstrap-prior-not-settled",
            "prior LocalSave is not an actionable settled Plan3 exam state",
        )
    completed_plan = _completed_card_plan(prior, completed_card_guid)
    probe = Plan3LocalSaveOutcomeProbe(
        path,
        prior_signature,
        prior,
        completed_plan,
        advisor_factory,
        require_completed_card_replay=True,
    )
    settled = probe.probe()
    if settled is None or settled.completed_card_replay is None:
        raise _Plan3BootstrapUnavailable(
            "bootstrap-card-replay-unavailable",
            "transitional LocalSave did not exactly replay from the prior "
            "settled snapshot and completed card GUID",
        )
    return (
        settled.signature,
        settled.decoded,
        settled.advisor,
        settled.completed_card_replay,
    )


def _completed_replay_bound_snapshot(
    path: Path,
    replay: Plan3CompletedCardReplay,
    replay_advisor_factory: ReplayAdvisorFactory | None = None,
) -> tuple[
    Plan3LocalSaveSignature,
    DecodedPlan3LocalSave,
    Plan3AdvisorReport,
]:
    """Bind a carried logical replay to its exact current serialized snapshot."""

    try:
        signature, decoded = _bound_decoded_snapshot(path)
    except Exception as error:
        raise _Plan3BootstrapUnavailable(
            "bootstrap-chain-source-unavailable",
            f"{type(error).__name__}:{error}",
        ) from error
    persisted = replay.persisted_after.envelope
    current = decoded.envelope
    if not (
        persisted.save_data_version == current.save_data_version
        and persisted.encrypted_body_size == current.encrypted_body_size
        and persisted.plaintext == current.plaintext
    ):
        raise _Plan3BootstrapUnavailable(
            "bootstrap-chain-source-mismatch",
            "carried replay does not end at the current LocalSave",
        )
    if decoded.exam_state.exam_type != 0 and replay_advisor_factory is None:
        raise _Plan3BootstrapUnavailable(
            "bootstrap-chain-advisor-unsupported",
            "completed replay chains currently require a Plan3 lesson",
        )
    advisor = (
        replay_advisor_factory(decoded, replay)
        if replay_advisor_factory is not None
        else advise_plan3_lesson_decoded(
            decoded,
            source=str(path.resolve()),
            transition_replay=replay,
        )
    )
    return signature, decoded, advisor


def _trace_input_count(trace: Sequence[Mapping[str, object]]) -> int:
    """Count controller inputs without treating evidence events as clicks."""

    return sum(
        1
        for entry in trace
        if entry.get("event") != Plan3SettlementFrameTimeout.code
    )


def _settlement_timeout_validation_issue(
    *,
    before_signature: Plan3LocalSaveSignature,
    before: DecodedPlan3LocalSave,
    plan: Plan3AuditionExecutionPlan,
    settled: SettledPlan3Action,
) -> Plan3ExecutionIssue | None:
    """Validate the sole authoritative recovery path after a UI timeout."""

    if settled.signature == before_signature:
        return Plan3ExecutionIssue(
            "settlement-timeout-local-save-unchanged",
            "timeout recovery returned the pre-action LocalSave signature",
        )
    if not plan3_exam_action_applied(before, settled.decoded, plan):
        return Plan3ExecutionIssue(
            "settlement-timeout-action-unproven",
            "changed LocalSave does not prove the exact planned action",
        )
    state = settled.decoded.exam_state
    if is_plan3_exam_terminal(state):
        return None
    if (
        settled.completed_card_replay is None
        and not is_plan3_exam_actionable_settled(state)
    ):
        return Plan3ExecutionIssue(
            "settlement-timeout-state-unsettled",
            "changed LocalSave is neither actionable settled nor an exact replay",
        )
    if not settled.advisor.available or settled.advisor.diagnostics:
        return Plan3ExecutionIssue(
            "settlement-timeout-advisor-unready",
            "changed LocalSave did not produce ready diagnostic-free advice",
        )
    return None


def execute_plan3_exam_first_action(
    path: str | Path,
    *,
    advisor_mode: str = MODE_AUTO,
    dry_run: bool = True,
    timeout_seconds: float = 30.0,
    poll_interval_seconds: float = 0.25,
    advisor_factory: AdvisorFactory | None = None,
    driver: MaaPlan3ActionDriver | None = None,
    waiter: Callable[
        [Plan3LocalSaveOutcomeProbe, float, float], Plan3SettledOutcome
    ]
    | None = None,
    prediction_actual_callback: PredictionActualCallback | None = None,
    prior_settled_path: str | Path | None = None,
    completed_card_guid: str | None = None,
    prior_completed_card_replay: Plan3CompletedCardReplay | None = None,
    card_replay_resolver: CardReplayResolver | None = None,
    replay_advisor_factory: ReplayAdvisorFactory | None = None,
    accept_missing_local_save_as_terminal: bool = False,
    stop_requested: Callable[[], bool] = lambda: False,
    progress_callback: Callable[[Mapping[str, object]], None] | None = None,
    expected_run_id: str | None = None,
) -> Plan3AuditionStepExecutionResult:
    """Plan or execute one mode-matched lesson/audition advisor action.

    Live execution is opt-in via ``dry_run=False``.  The returned JSON-shaped
    result records every controller input and the advisor report obtained from
    the changed settled LocalSave.  A same-turn card-command transition is
    accepted at entry only when both ``prior_settled_path`` and
    ``completed_card_guid`` explicitly request an exact completed-card replay.
    """

    from .runtime_command_client import input_backend
    _normalize_advisor_mode(advisor_mode)
    if (driver is None and waiter is None and (not dry_run or advisor_factory is None)
            and input_backend() == "dll"):
        from .runtime_plan3_executor import execute_runtime_plan3_first_action
        return execute_runtime_plan3_first_action(
            path, advisor_mode=advisor_mode, dry_run=dry_run, timeout_seconds=timeout_seconds,
            advisor_factory=advisor_factory, prediction_actual_callback=prediction_actual_callback,
            stop_requested=stop_requested, progress_callback=progress_callback, expected_run_id=expected_run_id)

    requested_mode = _normalize_advisor_mode(advisor_mode)
    if (prior_settled_path is None) != (completed_card_guid is None):
        raise ValueError(
            "prior_settled_path and completed_card_guid must be supplied together"
        )
    if completed_card_guid is not None and (
        not isinstance(completed_card_guid, str) or not completed_card_guid
    ):
        raise ValueError("completed_card_guid must be non-empty text")
    if prior_completed_card_replay is not None and not isinstance(
        prior_completed_card_replay, Plan3CompletedCardReplay
    ):
        raise TypeError(
            "prior_completed_card_replay must be Plan3CompletedCardReplay"
        )
    if prior_completed_card_replay is not None and prior_settled_path is not None:
        raise ValueError(
            "prior_completed_card_replay cannot be combined with legacy bootstrap"
        )
    source_path = Path(path).resolve()
    source = str(source_path)
    if advisor_factory is None:
        advisor_factory = _default_advisor_factory(source, requested_mode)
    bootstrap_replay = prior_completed_card_replay
    try:
        if bootstrap_replay is not None:
            signature, decoded, before_report = _completed_replay_bound_snapshot(
                source_path,
                bootstrap_replay,
                replay_advisor_factory,
            )
        elif prior_settled_path is None:
            signature, decoded, before_report = _bound_snapshot(
                source_path, advisor_factory
            )
        else:
            assert completed_card_guid is not None
            (
                signature,
                decoded,
                before_report,
                bootstrap_replay,
            ) = _bootstrap_bound_snapshot(
                source_path,
                prior_settled_path=Path(prior_settled_path).resolve(),
                completed_card_guid=completed_card_guid,
                advisor_factory=advisor_factory,
            )
        resolved_mode = _resolved_advisor_mode(decoded, requested_mode)
    except _Plan3BootstrapUnavailable as error:
        return Plan3AuditionStepExecutionResult(
            status=STATUS_UNAVAILABLE,
            dry_run=dry_run,
            advisor_mode=requested_mode,
            source=source,
            input_count=0,
            before_advisor=None,
            plan=None,
            settled_local_save_changed=False,
            after_advisor=None,
            ui_trace=(),
            issues=(Plan3ExecutionIssue(error.code, error.detail),),
        )
    except Exception as error:
        return Plan3AuditionStepExecutionResult(
            status=STATUS_UNAVAILABLE,
            dry_run=dry_run,
            advisor_mode=requested_mode,
            source=source,
            input_count=0,
            before_advisor=None,
            plan=None,
            settled_local_save_changed=False,
            after_advisor=None,
            ui_trace=(),
            issues=(
                Plan3ExecutionIssue(
                    "local-save-unavailable",
                    f"{type(error).__name__}:{error}",
                ),
            ),
        )

    before_payload = before_report.to_dict()
    actual_exam_mode = _exam_type_advisor_mode(decoded)
    if resolved_mode != actual_exam_mode:
        return Plan3AuditionStepExecutionResult(
            status=STATUS_UNAVAILABLE,
            dry_run=dry_run,
            advisor_mode=resolved_mode,
            source=source,
            input_count=0,
            before_advisor=before_payload,
            plan=None,
            settled_local_save_changed=False,
            after_advisor=None,
            ui_trace=(),
            issues=(
                Plan3ExecutionIssue(
                    "advisor-mode-mismatch",
                    f"requested={resolved_mode};exam_type={decoded.exam_state.exam_type};"
                    f"actual={actual_exam_mode}",
                ),
            ),
        )
    if not before_report.available or before_report.diagnostics:
        issues = tuple(
            Plan3ExecutionIssue(issue.code, issue.detail)
            for issue in before_report.issues
        )
        if before_report.diagnostics:
            issues += (
                Plan3ExecutionIssue(
                    "advisor-diagnostics",
                    "advisor diagnostics block all controller input",
                ),
            )
        return Plan3AuditionStepExecutionResult(
            status=STATUS_UNAVAILABLE,
            dry_run=dry_run,
            advisor_mode=resolved_mode,
            source=source,
            input_count=0,
            before_advisor=before_payload,
            plan=None,
            settled_local_save_changed=False,
            after_advisor=None,
            ui_trace=(),
            issues=issues,
        )

    try:
        plan = build_plan3_exam_execution_plan(before_report, decoded)
    except Exception as error:
        return Plan3AuditionStepExecutionResult(
            status=STATUS_UNAVAILABLE,
            dry_run=dry_run,
            advisor_mode=resolved_mode,
            source=source,
            input_count=0,
            before_advisor=before_payload,
            plan=None,
            settled_local_save_changed=False,
            after_advisor=None,
            ui_trace=(),
            issues=(
                Plan3ExecutionIssue(
                    "execution-plan-unavailable",
                    f"{type(error).__name__}:{error}",
                ),
            ),
        )

    plan_payload = plan.to_dict()
    if dry_run:
        return Plan3AuditionStepExecutionResult(
            status=STATUS_PLANNED,
            dry_run=True,
            advisor_mode=resolved_mode,
            source=source,
            input_count=0,
            before_advisor=before_payload,
            plan=plan_payload,
            settled_local_save_changed=False,
            after_advisor=None,
            ui_trace=(),
            issues=(),
        )

    trace: list[Mapping[str, object]] = []
    probe = Plan3LocalSaveOutcomeProbe(
        source_path,
        signature,
        decoded,
        plan,
        advisor_factory,
        prior_completed_card_replay=bootstrap_replay,
        card_replay_resolver=card_replay_resolver,
        replay_advisor_factory=replay_advisor_factory,
        accept_missing_local_save_as_terminal=(
            accept_missing_local_save_as_terminal
        ),
    )
    active_driver = driver or MaaPlan3ActionDriver()
    settlement_timeout: Plan3SettlementFrameTimeout | None = None
    try:
        active_driver.execute(
            plan,
            decoded,
            source_path,
            signature,
            probe,
            trace,
        )
        # This authorization happens only after the driver returned normally.
        # A missing file before input, a stale CAS, or a failed controller
        # dispatch therefore cannot be promoted to a terminal outcome.
        if _trace_input_count(trace) > 0:
            probe.authorize_missing_local_save_terminal()
    except Plan3SettlementFrameTimeout as error:
        settlement_timeout = error
        if not any(entry.get("event") == error.code for entry in trace):
            trace.append(error.trace_entry())
    except Exception as error:
        return Plan3AuditionStepExecutionResult(
            status=STATUS_FAILED,
            dry_run=False,
            advisor_mode=resolved_mode,
            source=source,
            input_count=_trace_input_count(trace),
            before_advisor=before_payload,
            plan=plan_payload,
            settled_local_save_changed=False,
            after_advisor=None,
            ui_trace=tuple(trace),
            issues=(
                Plan3ExecutionIssue(
                    "execution-failed",
                    f"{type(error).__name__}:{error}",
                ),
            ),
        )

    try:
        if waiter is None:
            settled = probe.wait(
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
        else:
            settled = waiter(probe, timeout_seconds, poll_interval_seconds)
    except Exception as error:
        issue_code = (
            "settlement-timeout-unconfirmed"
            if settlement_timeout is not None
            else "execution-failed"
        )
        return Plan3AuditionStepExecutionResult(
            status=STATUS_FAILED,
            dry_run=False,
            advisor_mode=resolved_mode,
            source=source,
            input_count=_trace_input_count(trace),
            before_advisor=before_payload,
            plan=plan_payload,
            settled_local_save_changed=False,
            after_advisor=None,
            ui_trace=tuple(trace),
            issues=(
                Plan3ExecutionIssue(
                    issue_code,
                    f"{type(error).__name__}:{error}",
                ),
            ),
        )

    if isinstance(settled, Plan3MissingLocalSaveTerminalAction):
        try:
            artifact = build_plan3_missing_local_save_terminal_artifact(
                advisor_mode=resolved_mode,
                before_report=before_report,
                plan=plan,
                settled=settled,
            )
        except Exception as error:
            return Plan3AuditionStepExecutionResult(
                status=STATUS_FAILED,
                dry_run=False,
                advisor_mode=resolved_mode,
                source=source,
                input_count=_trace_input_count(trace),
                before_advisor=before_payload,
                plan=plan_payload,
                settled_local_save_changed=True,
                after_advisor=None,
                ui_trace=tuple(trace),
                issues=(
                    Plan3ExecutionIssue(
                        "terminal-exam-save-removal-invalid",
                        f"{type(error).__name__}:{error}",
                    ),
                ),
            )
        callback_issues: tuple[Plan3ExecutionIssue, ...] = ()
        if prediction_actual_callback is not None:
            try:
                prediction_actual_callback(artifact)
            except Exception as error:
                callback_issues = (
                    Plan3ExecutionIssue(
                        "prediction-actual-callback-failed",
                        f"{type(error).__name__}:{error}",
                    ),
                )
        return Plan3AuditionStepExecutionResult(
            status=STATUS_EXECUTED,
            dry_run=False,
            advisor_mode=resolved_mode,
            source=source,
            input_count=_trace_input_count(trace),
            before_advisor=before_payload,
            plan=plan_payload,
            settled_local_save_changed=True,
            after_advisor=None,
            prediction_actual=artifact,
            ui_trace=tuple(trace),
            issues=callback_issues,
        )

    if isinstance(settled, Plan3ScreenTerminalAction):
        try:
            artifact = build_plan3_screen_terminal_artifact(
                advisor_mode=resolved_mode,
                before_report=before_report,
                plan=plan,
                settled=settled,
            )
        except Exception as error:
            return Plan3AuditionStepExecutionResult(
                status=STATUS_FAILED,
                dry_run=False,
                advisor_mode=resolved_mode,
                source=source,
                input_count=_trace_input_count(trace),
                before_advisor=before_payload,
                plan=plan_payload,
                settled_local_save_changed=True,
                after_advisor=None,
                ui_trace=tuple(trace),
                issues=(
                    Plan3ExecutionIssue(
                        "terminal-screen-evidence-invalid",
                        f"{type(error).__name__}:{error}",
                    ),
                ),
            )
        callback_issues: tuple[Plan3ExecutionIssue, ...] = ()
        if prediction_actual_callback is not None:
            try:
                prediction_actual_callback(artifact)
            except Exception as error:
                callback_issues = (
                    Plan3ExecutionIssue(
                        "prediction-actual-callback-failed",
                        f"{type(error).__name__}:{error}",
                    ),
                )
        return Plan3AuditionStepExecutionResult(
            status=STATUS_EXECUTED,
            dry_run=False,
            advisor_mode=resolved_mode,
            source=source,
            input_count=_trace_input_count(trace),
            before_advisor=before_payload,
            plan=plan_payload,
            settled_local_save_changed=True,
            after_advisor=None,
            prediction_actual=artifact,
            ui_trace=tuple(trace),
            issues=callback_issues,
        )

    if settlement_timeout is not None:
        recovery_issue = _settlement_timeout_validation_issue(
            before_signature=signature,
            before=decoded,
            plan=plan,
            settled=settled,
        )
        if recovery_issue is not None:
            return Plan3AuditionStepExecutionResult(
                status=STATUS_FAILED,
                dry_run=False,
                advisor_mode=resolved_mode,
                source=source,
                input_count=_trace_input_count(trace),
                before_advisor=before_payload,
                plan=plan_payload,
                settled_local_save_changed=(settled.signature != signature),
                after_advisor=settled.advisor.to_dict(),
                ui_trace=tuple(trace),
                issues=(recovery_issue,),
            )

    artifact: Mapping[str, object] | None = None
    artifact_issues: tuple[Plan3ExecutionIssue, ...] = ()
    try:
        artifact = build_plan3_prediction_actual_artifact(
            advisor_mode=resolved_mode,
            before_report=before_report,
            before=decoded,
            plan=plan,
            settled=settled,
        )
    except Exception as error:
        artifact_issues = (
            Plan3ExecutionIssue(
                "prediction-actual-unavailable",
                f"{type(error).__name__}:{error}",
            ),
        )
    if artifact is not None and prediction_actual_callback is not None:
        try:
            prediction_actual_callback(artifact)
        except Exception as error:
            artifact_issues += (
                Plan3ExecutionIssue(
                    "prediction-actual-callback-failed",
                    f"{type(error).__name__}:{error}",
                ),
            )

    return Plan3AuditionStepExecutionResult(
        status=STATUS_EXECUTED,
        dry_run=False,
        advisor_mode=resolved_mode,
        source=source,
        input_count=_trace_input_count(trace),
        before_advisor=before_payload,
        plan=plan_payload,
        settled_local_save_changed=True,
        after_advisor=settled.advisor.to_dict(),
        prediction_actual=artifact,
        completed_card_replay=settled.completed_card_replay,
        ui_trace=tuple(trace),
        issues=artifact_issues,
    )


def execute_plan3_audition_first_action(
    path: str | Path,
    *,
    dry_run: bool = True,
    timeout_seconds: float = 30.0,
    poll_interval_seconds: float = 0.25,
    advisor_factory: AdvisorFactory | None = None,
    driver: MaaPlan3ActionDriver | None = None,
    waiter: Callable[
        [Plan3LocalSaveOutcomeProbe, float, float], Plan3SettledOutcome
    ]
    | None = None,
    prediction_actual_callback: PredictionActualCallback | None = None,
    prior_settled_path: str | Path | None = None,
    completed_card_guid: str | None = None,
) -> Plan3AuditionStepExecutionResult:
    """Compatibility wrapper that explicitly selects the audition advisor."""

    return execute_plan3_exam_first_action(
        path,
        advisor_mode=MODE_AUDITION,
        dry_run=dry_run,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        advisor_factory=advisor_factory,
        driver=driver,
        waiter=waiter,
        prediction_actual_callback=prediction_actual_callback,
        prior_settled_path=prior_settled_path,
        completed_card_guid=completed_card_guid,
    )


def _main(
    argv: Sequence[str] | None,
    *,
    default_advisor_mode: str,
) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Plan or execute exactly one Plan3 lesson/audition first_action."
        )
    )
    parser.add_argument("local_save", type=Path, nargs="?")
    execution_mode = parser.add_mutually_exclusive_group()
    execution_mode.add_argument(
        "--execute",
        action="store_true",
        help="opt in to MAA background input; default is dry-run",
    )
    execution_mode.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--mode",
        choices=sorted(ADVISOR_MODES),
        default=_normalize_advisor_mode(default_advisor_mode),
        help="advisor routing: auto reads exam_type; lesson/audition are explicit",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument(
        "--prior-settled",
        type=Path,
        help=(
            "explicit prior settled LocalSave for exact completed-card "
            "transitional bootstrap"
        ),
    )
    parser.add_argument(
        "--completed-card-guid",
        help="completed card GUID paired with --prior-settled",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    if (args.prior_settled is None) != (args.completed_card_guid is None):
        parser.error(
            "--prior-settled and --completed-card-guid must be supplied together"
        )

    source = args.local_save or select_plan3_exam_local_save_path()
    if source is None:
        result = Plan3AuditionStepExecutionResult(
            status=STATUS_UNAVAILABLE,
            dry_run=not args.execute,
            advisor_mode=args.mode,
            source="",
            input_count=0,
            before_advisor=None,
            plan=None,
            settled_local_save_changed=False,
            after_advisor=None,
            ui_trace=(),
            issues=(
                Plan3ExecutionIssue(
                    "local-save-unavailable",
                    "no current ExamSaveData LocalSave was found",
                ),
            ),
        )
    else:
        result = execute_plan3_exam_first_action(
            source,
            advisor_mode=args.mode,
            dry_run=not args.execute,
            timeout_seconds=args.timeout,
            poll_interval_seconds=args.poll_interval,
            prior_settled_path=args.prior_settled,
            completed_card_guid=args.completed_card_guid,
        )
    encoded = result.to_json(indent=None if args.compact else 2) + "\n"
    if args.output is None:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(encoded, end="")
    else:
        target = args.output.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(encoded, encoding="utf-8")
    if result.status in {STATUS_PLANNED, STATUS_EXECUTED}:
        return 0
    return 2 if result.status == STATUS_UNAVAILABLE else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Common CLI; auto-routes by persisted ``exam_type`` by default."""

    return _main(argv, default_advisor_mode=MODE_AUTO)


def audition_main(argv: Sequence[str] | None = None) -> int:
    """Legacy CLI entry point with audition routing as its default."""

    return _main(argv, default_advisor_mode=MODE_AUDITION)


def lesson_main(argv: Sequence[str] | None = None) -> int:
    """Lesson-specific CLI entry point."""

    return _main(argv, default_advisor_mode=MODE_LESSON)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ADVISOR_MODES",
    "CARD_SELECTION_COMMIT_BOX",
    "CARD_SELECTION_GRID_BOXES",
    "DRINK_SLOT_BOXES",
    "DRINK_USE_BOX",
    "MaaPlan3ActionDriver",
    "MODE_AUDITION",
    "MODE_AUTO",
    "MODE_LESSON",
    "Plan3AuditionExecutionPlan",
    "Plan3AuditionStepExecutionResult",
    "Plan3ExecutionPlan",
    "Plan3ExecutionIssue",
    "Plan3LocalSaveOutcomeProbe",
    "Plan3MissingLocalSaveTerminalAction",
    "Plan3ScreenTerminalAction",
    "Plan3SettledOutcome",
    "Plan3SettlementFrameTimeout",
    "Plan3StepExecutionResult",
    "PredictionActualCallback",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "SKIP_BOX",
    "SKIP_CONFIRM_TITLE_BOX",
    "SKIP_CONFIRM_YES_BOX",
    "PreviewCardArtEvidence",
    "SkipConfirmationEvidence",
    "STATUS_EXECUTED",
    "STATUS_FAILED",
    "STATUS_PLANNED",
    "STATUS_UNAVAILABLE",
    "SettledPlan3Action",
    "build_plan3_audition_execution_plan",
    "build_plan3_exam_execution_plan",
    "build_plan3_prediction_actual_artifact",
    "build_plan3_missing_local_save_terminal_artifact",
    "build_plan3_screen_terminal_artifact",
    "execute_plan3_exam_first_action",
    "execute_plan3_audition_first_action",
    "is_plan3_exam_actionable_settled",
    "is_plan3_exam_terminal",
    "audition_main",
    "lesson_main",
    "main",
    "plan3_exam_action_applied",
    "selection_grid_index_for_guid",
]
