"""Resolve localized ADV choice rows to exact Master event suggestions."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
import re
from pathlib import Path
from .application_paths import game_file
import unicodedata

from .event_db import analyze_event_by_adv_asset
from .school_event import SchoolEventChoice, SchoolEventResolution, resolve_school_event_detail


DEFAULT_ADV_RESOURCE_DIR = game_file('gakumas-local/local-files/resource')
# Produce's outer drink inventory is a fixed three-slot resource.  Keep the
# default here, next to the Master effect projection, rather than teaching the
# live page router about a particular event or character.
DEFAULT_DRINK_CAPACITY = 3
_CHOICE_RE = re.compile(r"choices=\[choice text=([^\]]*)\]")
_OCR_CHARACTER_EQUIVALENTS = str.maketrans(
    {"红": "紅", "汤": "湯", "吗": "嗎"}
)


def _normalize(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).translate(
            _OCR_CHARACTER_EQUIVALENTS
        )
        if not character.isspace() and character not in "><》〉"
    ).casefold()


@dataclass(frozen=True, slots=True)
class EventChoiceCatalogEntry:
    adv_asset_id: str
    character_id: str
    choice_texts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolvedEventChoices:
    entry: EventChoiceCatalogEntry
    resolution: SchoolEventResolution
    observed_texts: tuple[str, ...]
    similarities: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class EventChoiceAdvice:
    slot: int
    score: int
    reason: str
    candidates: tuple[dict[str, object], ...]


@lru_cache(maxsize=4)
def _load_catalog(resource_dir_text: str) -> tuple[EventChoiceCatalogEntry, ...]:
    resource_dir = Path(resource_dir_text)
    if not resource_dir.is_dir():
        raise FileNotFoundError(resource_dir)
    entries: list[EventChoiceCatalogEntry] = []
    for path in sorted(resource_dir.glob("adv_pevent_001_*_activity_*.txt")):
        groups: list[tuple[str, ...]] = []
        for line in path.read_text(encoding="utf-8-sig", errors="strict").splitlines():
            if "[choicegroup " not in line:
                continue
            choices = tuple(_CHOICE_RE.findall(line))
            if choices:
                groups.append(choices)
        if len(groups) != 1:
            raise ValueError(f"ADV resource must contain one choicegroup: {path}")
        parts = path.stem.split("_")
        if len(parts) < 5:
            raise ValueError(f"ADV resource name is malformed: {path.name}")
        entries.append(
            EventChoiceCatalogEntry(
                adv_asset_id=path.stem,
                character_id=parts[3],
                choice_texts=groups[0],
            )
        )
    if not entries:
        raise ValueError(f"ADV choice catalog is empty: {resource_dir}")
    return tuple(entries)


def resolve_visible_event_choices(
    character_id: str,
    observed_texts: tuple[str, ...],
    *,
    resource_dir: Path = DEFAULT_ADV_RESOURCE_DIR,
) -> ResolvedEventChoices:
    if not character_id or len(observed_texts) < 2:
        raise ValueError("event character and at least two choice texts are required")
    observed = tuple(_normalize(value) for value in observed_texts)
    if any(not value for value in observed):
        raise ValueError("visible event choice OCR is empty")
    ranked: list[tuple[float, tuple[float, ...], EventChoiceCatalogEntry]] = []
    for entry in _load_catalog(str(resource_dir.resolve())):
        if entry.character_id != character_id or len(entry.choice_texts) != len(observed):
            continue
        similarities = tuple(
            SequenceMatcher(None, actual, _normalize(expected)).ratio()
            for actual, expected in zip(observed, entry.choice_texts)
        )
        if min(similarities) < 0.55:
            continue
        ranked.append((sum(similarities) / len(similarities), similarities, entry))
    ranked.sort(key=lambda value: (value[0], value[2].adv_asset_id), reverse=True)
    if not ranked or ranked[0][0] < 0.68:
        raise LookupError("visible event choices do not match the localized ADV catalog")
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08:
        raise ValueError(
            "visible event choices are ambiguous: "
            f"{ranked[0][2].adv_asset_id}, {ranked[1][2].adv_asset_id}"
        )
    _score, similarities, entry = ranked[0]
    analysis = analyze_event_by_adv_asset(entry.adv_asset_id)
    if analysis is None or not analysis.detail_id:
        raise LookupError(f"event Master detail is unavailable: {entry.adv_asset_id}")
    resolution = resolve_school_event_detail(analysis.detail_id)
    if len(resolution.choices) != len(entry.choice_texts):
        raise ValueError("ADV choice count disagrees with Master suggestion order")
    return ResolvedEventChoices(
        entry=entry,
        resolution=resolution,
        observed_texts=observed_texts,
        similarities=similarities,
    )


def _recovery_multiple(choice: SchoolEventChoice) -> int:
    values: list[int] = []
    for effect_id in choice.effect_ids:
        match = re.search(r"stamina_recover_multiple-(\d{4})_", effect_id)
        if match is not None:
            values.append(int(match.group(1)))
    return max(values, default=0)


def _advise_event_resolution(
    resolution: SchoolEventResolution,
    *,
    choice_texts: tuple[str, ...],
    stamina: int,
    max_stamina: int,
    produce_points: int,
    weeks_remaining: int,
    current_drink_count: int | None = None,
    drink_capacity: int = DEFAULT_DRINK_CAPACITY,
) -> EventChoiceAdvice:
    if not (0 <= stamina <= max_stamina and max_stamina > 0 and produce_points >= 0):
        raise ValueError("event policy requires exact stamina and Produce points")
    if (
        isinstance(drink_capacity, bool)
        or not isinstance(drink_capacity, int)
        or drink_capacity < 1
    ):
        raise ValueError("drink capacity must be a positive integer")
    if current_drink_count is not None and (
        isinstance(current_drink_count, bool)
        or not isinstance(current_drink_count, int)
        or not 0 <= current_drink_count <= drink_capacity
    ):
        raise ValueError("current drink count must fit the drink capacity")
    open_drink_slots = (
        None
        if current_drink_count is None
        else drink_capacity - current_drink_count
    )
    candidates: list[dict[str, object]] = []
    for choice, text in zip(
        resolution.choices,
        choice_texts,
    ):
        affordable = choice.produce_point_cost <= produce_points
        missing = max_stamina - stamina
        recovery = min(
            missing,
            (max_stamina * _recovery_multiple(choice)) // 10_000,
        )
        score = recovery * 30 - choice.produce_point_cost * 5
        score -= choice.stamina_cost * 30
        score += sum(choice.status_delta_min.values()) * 10
        success_card = any("reward_set" in value for value in choice.success_effect_ids)
        fail_sleepy = any("p_card-00-acc-0_002" in value for value in choice.fail_effect_ids)
        failure_probability = max(0, 10_000 - choice.success_probability_permyriad)
        if fail_sleepy:
            penalty = 400 if weeks_remaining > 2 else 700
            score -= (failure_probability * penalty) // 10_000
        # Unknown future card candidates have a conservative lower bound of
        # zero.  A guaranteed reward is recorded but does not receive invented
        # value; a risky reward therefore cannot hide its point/trouble cost.
        if not affordable:
            score = -1_000_000
        no_op = not any(
            (
                choice.produce_point_cost,
                choice.stamina_cost,
                choice.direct_card_id,
                choice.effect_ids,
                choice.success_effect_ids,
                choice.fail_effect_ids,
            )
        )
        drink_rewards = tuple(
            reward
            for reward in choice.reward_selections
            if reward.resource_type == "ProduceResourceType_ProduceDrink"
        )
        drink_pick_count_min = sum(
            reward.pick_count_min for reward in drink_rewards
        )
        drink_pick_count_max = sum(
            reward.pick_count_max for reward in drink_rewards
        )
        # A Master reward set can contain a range.  When the exact current
        # inventory is known, require the whole range to fit.  Otherwise a
        # supposedly safe branch can open the game's full-inventory limit (or
        # silently discard the reward) even though its minimum happens to fit.
        drink_capacity_ok = (
            open_drink_slots is None
            or drink_pick_count_max <= open_drink_slots
        )
        if not drink_capacity_ok:
            score = -1_000_000
        candidates.append(
            {
                "slot": choice.slot,
                "suggestion_id": choice.suggestion_id,
                "text": text,
                "score": score,
                "affordable": affordable,
                "recovery": recovery,
                "success_probability_permyriad": choice.success_probability_permyriad,
                "success_card_reward": success_card,
                "fail_sleepy": fail_sleepy,
                "no_op": no_op,
                "drink_pick_count_min": drink_pick_count_min,
                "drink_pick_count_max": drink_pick_count_max,
                "current_drink_count": current_drink_count,
                "drink_capacity": drink_capacity,
                "open_drink_slots": open_drink_slots,
                "drink_capacity_ok": drink_capacity_ok,
            }
        )
    available = [
        value
        for value in candidates
        if value["affordable"] is True
        and value["drink_capacity_ok"] is True
    ]
    if not available:
        raise LookupError("no affordable event choice within drink capacity")
    selected = max(
        available,
        key=lambda value: (
            int(value["score"]),
            bool(value["no_op"]),
            -int(value["slot"]),
        ),
    )
    reason = "conservative-master-event-lower-bound"
    if current_drink_count is not None:
        reason += "+drink-capacity"
    return EventChoiceAdvice(
        slot=int(selected["slot"]),
        score=int(selected["score"]),
        reason=reason,
        candidates=tuple(candidates),
    )


def advise_event_choices(resolved: ResolvedEventChoices, **kwargs) -> EventChoiceAdvice:
    return _advise_event_resolution(
        resolved.resolution, choice_texts=resolved.entry.choice_texts, **kwargs,
    )


def advise_resolved_event_choices(resolution: SchoolEventResolution, **kwargs) -> EventChoiceAdvice:
    """Rank an exact native event detail without constructing OCR evidence."""
    return _advise_event_resolution(
        resolution, choice_texts=tuple(choice.suggestion_id for choice in resolution.choices), **kwargs,
    )


__all__ = [
    "DEFAULT_ADV_RESOURCE_DIR",
    "DEFAULT_DRINK_CAPACITY",
    "EventChoiceAdvice",
    "EventChoiceCatalogEntry",
    "ResolvedEventChoices",
    "advise_event_choices",
    "advise_resolved_event_choices",
    "resolve_visible_event_choices",
]
