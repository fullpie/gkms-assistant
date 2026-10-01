"""Screenshot-only analysis for the three-card post-lesson reward.

The visible tile supplies identity/upgrade state; the exact rules, rarity, and
the game's own static evaluation value come from the imported Master database.
No process memory or runtime network traffic is read.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, ClassVar, Iterable, Mapping, Sequence

import numpy as np
from PIL import Image

from .card_art_matcher import CardArtMatch, match_card_art_color
from .card_identity import CardNameCatalog, CardNameEntry, normalize_card_text
from .logic_engine import (
    EFFECT_BLOCK,
    EFFECT_CARD_DRAW,
    EFFECT_EXTRA_TURN,
    EFFECT_GOOD_IMPRESSION,
    EFFECT_GOOD_IMPRESSION_ADDITIVE,
    EFFECT_GOOD_IMPRESSION_BONUS,
    EFFECT_GOOD_IMPRESSION_MULTIPLE,
    EFFECT_LESSON,
    EFFECT_LESSON_BY_BLOCK,
    EFFECT_LESSON_BY_GOOD_IMPRESSION,
    EFFECT_LESSON_BY_MOTIVATION,
    EFFECT_MOTIVATION,
    EFFECT_PLAYABLE_VALUE_ADD,
    MasterCard,
    load_master_card,
)
from .leaderboard_card_prior import (
    Plan2HierarchicalLeaderboardCardPrior,
    Plan2LeaderboardCardPrior,
)
from .master_db import DEFAULT_DATABASE
from .octo_assets import card_suffix_from_asset
from .reward_card_semantics import (
    filter_excluded_reward_card_candidates,
    normalize_excluded_reward_card_ids,
)


REWARD_COLLECT_BOX = (228, 1048, 492, 1138)
REWARD_PREVIEW_NAME_BOX = (220, 480, 510, 555)
REWARD_CARD_EXCLUDE_EFFECT_TYPE = "ProduceEffectType_ProduceCardExcludeCountUp"
REWARD_CARD_EXCLUDE_NATIVE_REQUEST = "ProduceExcludeProduceCardRequest"
REWARD_CARD_REROLL_NATIVE_REQUEST = "ProduceRerollSelectProduceCardRequest"
PRODUCE_CARD_DELETE_EFFECT_TYPE = "ProduceEffectType_ProduceCardDelete"
PRODUCE_CARD_DELETE_NATIVE_TARGET_FIELDS = ("deck_index", "card_id")
SUPPORTED_REWARD_PLAN_TYPES = frozenset(
    {
        "ProducePlanType_Plan1",
        "ProducePlanType_Plan2",
        "ProducePlanType_Plan3",
    }
)
_TROUBLE_COUNT_TRIGGER = (
    "e_trigger-none-card_search_count_up-2-p_card_search-trouble-not_lost"
)
_COST_GOOD_IMPRESSION = "ExamCostType_ExamReview"
_COST_MOTIVATION = "ExamCostType_ExamCardPlayAggressive"
_EFFECT_PLAYABLE_COUNT = "ProduceExamEffectType_ExamPlayableValueAdd"
_REGULAR_REWARD_CATEGORIES = {
    "ProduceCardCategory_ActiveSkill",
    "ProduceCardCategory_MentalSkill",
}

# NIA Plan2's two supported idol archetypes value different exam primitives.
# Keep this mapping effect-based (rather than card/idol based) so it remains
# valid as the Master reward pool changes.  Review-value multiplication is a
# review-compatible signal even when a card does not contain a direct Review
# effect; the other effects are the direct/lesson-dependent primitives used by
# the two archetypes.
_NIA_REWARD_COMPATIBLE_EFFECTS = {
    "ProduceExamEffectType_ExamReview": frozenset(
        {
            EFFECT_GOOD_IMPRESSION,
            EFFECT_LESSON_BY_GOOD_IMPRESSION,
            EFFECT_GOOD_IMPRESSION_MULTIPLE,
        }
    ),
    "ProduceExamEffectType_ExamCardPlayAggressive": frozenset(
        {
            EFFECT_MOTIVATION,
            EFFECT_BLOCK,
            EFFECT_LESSON_BY_BLOCK,
            EFFECT_LESSON_BY_MOTIVATION,
        }
    ),
}


def _validated_reward_plan_type(plan_type: str) -> str:
    if not isinstance(plan_type, str) or plan_type not in SUPPORTED_REWARD_PLAN_TYPES:
        raise ValueError(f"unsupported active-run reward plan type: {plan_type!r}")
    return plan_type


def _eligible_reward_card_ids(
    plan_type: str,
    database: Path,
) -> frozenset[str]:
    plan_type = _validated_reward_plan_type(plan_type)
    allowed_plans = {"ProducePlanType_Common", plan_type}
    eligible_ids: set[str] = set()
    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute(
            """
            SELECT id, plan_type, category, raw_json
              FROM card
             WHERE upgrade_count = 0
            """
        ).fetchall()
    for card_id, row_plan_type, category, raw_json in rows:
        if (
            row_plan_type not in allowed_plans
            or category not in _REGULAR_REWARD_CATEGORIES
        ):
            continue
        metadata = json.loads(raw_json)
        if not isinstance(metadata, dict):
            raise ValueError(f"Master 卡牌 metadata 不是物件：{card_id}")
        if any(
            (
                bool(metadata.get("isInitial")),
                bool(metadata.get("isRestrict")),
                bool(metadata.get("libraryHidden")),
                bool(metadata.get("isReward")),
                bool(metadata.get("originIdolCardId")),
                bool(metadata.get("originSupportCardId")),
                bool(metadata.get("isInitialDeckProduceCard")),
            )
        ):
            continue
        eligible_ids.add(str(card_id))
    if not eligible_ids:
        raise ValueError(f"Master 找不到 {plan_type} 的一般技能卡獎勵候選")
    return frozenset(eligible_ids)


def regular_reward_candidate_entries(
    *,
    catalog: CardNameCatalog,
    plan_type: str,
    database: Path = DEFAULT_DATABASE,
    include_upgrades: bool = False,
    excluded_reward_card_ids: Iterable[str] = (),
) -> tuple[CardNameEntry, ...]:
    """Return the closed Common + active-plan translated reward catalog.

    ``excluded_reward_card_ids`` is the current run's canonical shadow field.
    It is applied after Master/catalog consistency checks and by card ID, so
    every upgrade row for an excluded card is removed while surviving rows
    retain their catalog order and metadata.
    """

    if not isinstance(catalog, CardNameCatalog):
        raise TypeError("catalog must be CardNameCatalog")
    eligible_ids = _eligible_reward_card_ids(plan_type, database)
    entries = tuple(
        entry
        for entry in catalog.entries
        if entry.card_id in eligible_ids
        and (include_upgrades or entry.upgrade == 0)
    )
    catalog_ids = frozenset(entry.card_id for entry in entries)
    missing = sorted(eligible_ids - catalog_ids)
    if missing:
        raise ValueError(
            "local translated card catalog is missing eligible Master rows: "
            + ", ".join(missing)
        )
    return filter_excluded_reward_card_candidates(
        entries,
        excluded_reward_card_ids,
    )


def _reward_asset_entry(
    asset_name: str,
    entries: tuple[CardNameEntry, ...],
) -> CardNameEntry | None:
    suffix = card_suffix_from_asset(asset_name)
    candidates = tuple(
        entry for entry in entries if entry.card_id.endswith("-" + suffix)
    )
    if len(candidates) > 1:
        raise ValueError(
            f"reward asset {asset_name} conflicts inside active plan: "
            + ", ".join(sorted(entry.card_id for entry in candidates))
        )
    return candidates[0] if candidates else None


def filter_regular_reward_card_art(
    card_art: Mapping[str, Path | Image.Image],
    *,
    catalog: CardNameCatalog,
    plan_type: str,
    database: Path = DEFAULT_DATABASE,
    excluded_reward_card_ids: Iterable[str] = (),
) -> dict[str, Path | Image.Image]:
    """Keep only cards that Master marks as ordinary selectable rewards.

    The lesson/deck matcher intentionally contains idol- and support-origin
    artwork because those cards can be present in an active deck.  Those cards
    are not members of the ordinary post-activity reward pool and
    make compact reward tiles unnecessarily ambiguous.  The raw Master flags
    let this reader remove them without hard-coding card names.

    Producer-level unlocks are deliberately not filtered here: the live game
    is authoritative about which unlocked cards it offered, while this filter
    only removes structurally impossible candidates.
    """

    entries = regular_reward_candidate_entries(
        catalog=catalog,
        plan_type=plan_type,
        database=database,
        excluded_reward_card_ids=excluded_reward_card_ids,
    )

    filtered: dict[str, Path | Image.Image] = {}
    for asset_name, source in card_art.items():
        identity = _reward_asset_entry(asset_name, entries)
        if identity is None:
            continue
        filtered[asset_name] = source
    if not filtered:
        raise ValueError(f"Master 找不到 {plan_type} 的一般技能卡獎勵素材")
    return filtered


@dataclass(frozen=True, slots=True)
class RewardCardOffer:
    slot: int
    box: tuple[int, int, int, int]
    asset_name: str | None
    card_id: str
    upgrade: int
    display_name: str
    rarity: str
    evaluation: int
    stamina_cost: int
    force_stamina_cost: int
    effect_summary: tuple[str, ...]
    art_score: float
    art_margin: float
    game_recommended: bool
    selected: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Plan2RewardCardFeatures:
    """Static Master features used by the deck-building policy.

    These values describe what an offered card can contribute.  They are not
    a substitute for the in-exam simulator: trigger timing and the actual
    terminal score still belong to the native horizon.  Keeping the features
    explicit also makes reward choices usable as future training records.
    """

    review_gain: int = 0
    review_conversion_permil: int = 0
    review_multiplier_permil: int = 0
    review_additive_permil: int = 0
    playable_count: int = 0
    extra_turns: int = 0
    draw_count: int = 0
    review_cost: int = 0
    stamina_cost: int = 0
    force_stamina_cost: int = 0
    moves_to_lost: bool = False


@dataclass(frozen=True, slots=True)
class Plan2RewardDeckSummary:
    known: bool
    card_count: int
    review_generator_cards: int
    review_conversion_cards: int
    review_gain: int
    review_conversion_permil: int
    playable_count: int


@dataclass(frozen=True, slots=True)
class Plan2RewardOfferEvaluation:
    offer: RewardCardOffer
    strategic_score: int
    duplicate_count: int
    features: Plan2RewardCardFeatures
    reasons: tuple[str, ...]
    learned_prior_bonus: int = 0
    composition_current_count: int | None = None
    composition_future_guaranteed_count: int | None = None
    composition_target_count: float | None = None
    composition_gap: float | None = None
    composition_bonus: int = 0
    composition_scope: str | None = None
    composition_support: int | None = None


@dataclass(frozen=True, slots=True)
class GenericRewardRanking:
    """Master-backed ranking result for non-Plan2 NIA archetypes.

    Plan1/Plan3 do not use the Plan2 native counterfactual simulator.  Their
    visible offers are still resolved against Master, and an explicitly
    matching final-deck prior may add a bounded composition bonus.  Keeping
    the result shape close to ``Plan2RewardStrongRanking`` lets the live
    reader expose one telemetry format without pretending cross-archetype
    simulator evidence exists.
    """

    ranked_offers: tuple[RewardCardOffer, ...]
    policy: str
    heuristic_evaluations: tuple[Plan2RewardOfferEvaluation, ...] = ()

    def __post_init__(self) -> None:
        if self.policy != "master-scoped-composition-prior":
            raise ValueError("unsupported generic reward policy")


@dataclass(frozen=True, slots=True)
class RewardCardRerollAuthority:
    """Exact server-owned availability for one reward-list reroll.

    The live card-reward screen is not allowed to infer either field from
    colour, text, or a fixed button position.  Production callers must bind
    these values to ``UserProduceProgress`` (or an equivalent decoded server
    response) before a reroll can become executable.
    """

    remaining_count: int
    hidden: bool
    source: str = "UserProduceProgress"

    def __post_init__(self) -> None:
        if (
            isinstance(self.remaining_count, bool)
            or not isinstance(self.remaining_count, int)
            or self.remaining_count < 0
        ):
            raise ValueError("remaining_count must be a non-negative integer")
        if not isinstance(self.hidden, bool):
            raise TypeError("hidden must be bool")
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("source must be non-empty text")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RewardCardRerollBlocker:
    """Typed reason why a deliberately poor reward set cannot be rerolled."""

    code: str
    field: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.code or not self.field:
            raise ValueError("reroll blocker code and field must be non-empty")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class NiaRewardRerollDecision:
    """Conservative decision made only after all three offers are resolved."""

    action: str
    reason: str
    evaluations: tuple[Plan2RewardOfferEvaluation, ...]
    authority: RewardCardRerollAuthority | None = None
    blocker: RewardCardRerollBlocker | None = None
    conservative_baseline: int = 0
    native_request: str = REWARD_CARD_REROLL_NATIVE_REQUEST
    discard_old_candidates_after_submit: bool = True

    def __post_init__(self) -> None:
        if self.action not in {"receive", "reroll", "blocked"}:
            raise ValueError("unsupported reward reroll decision action")
        if not self.reason:
            raise ValueError("reward reroll decision reason must be non-empty")
        if self.action == "blocked" and self.blocker is None:
            raise ValueError("blocked reward reroll decision requires a blocker")
        if self.action != "blocked" and self.blocker is not None:
            raise ValueError("non-blocked reward reroll decision cannot carry blocker")
        if self.action == "reroll" and (
            self.authority is None
            or self.authority.hidden
            or self.authority.remaining_count <= 0
        ):
            raise ValueError("reroll decision requires visible positive authority")

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "reason": self.reason,
            "native_request": self.native_request,
            "conservative_baseline": self.conservative_baseline,
            "discard_old_candidates_after_submit": (
                self.discard_old_candidates_after_submit
            ),
            "authority": (
                None if self.authority is None else self.authority.to_dict()
            ),
            "blocker": None if self.blocker is None else self.blocker.to_dict(),
            "evaluations": [
                {
                    "slot": value.offer.slot,
                    "card_id": value.offer.card_id,
                    "upgrade": value.offer.upgrade,
                    "strategic_score": value.strategic_score,
                    "duplicate_count": value.duplicate_count,
                    "learned_prior_bonus": value.learned_prior_bonus,
                    "reasons": list(value.reasons),
                }
                for value in self.evaluations
            ],
        }


def _iter_reward_effects(card: MasterCard):
    """Yield ordered direct and status-enchant child effects."""

    def visit(effect):
        yield effect
        rule = effect.status_enchant_rule
        if rule is not None:
            for child in rule.effects:
                yield from visit(child)

    for effect in card.effects:
        yield from visit(effect)


def _positive_effect_count(effect) -> int:
    if effect.effect_count > 0:
        return effect.effect_count
    if effect.value1 > 0:
        return effect.value1
    return 1


def plan2_reward_card_features(card: MasterCard) -> Plan2RewardCardFeatures:
    if not isinstance(card, MasterCard):
        raise TypeError("card must be MasterCard")
    review_gain = 0
    conversion = 0
    multiplier = 0
    additive = 0
    playable = 0
    extra_turns = 0
    draw_count = 0
    for effect in _iter_reward_effects(card):
        if effect.effect_type == EFFECT_GOOD_IMPRESSION:
            review_gain += max(0, effect.value1)
        elif effect.effect_type == EFFECT_LESSON_BY_GOOD_IMPRESSION:
            conversion += max(0, effect.value1) * _positive_effect_count(effect)
        elif effect.effect_type in {
            EFFECT_GOOD_IMPRESSION_MULTIPLE,
            EFFECT_GOOD_IMPRESSION_BONUS,
        }:
            multiplier += max(0, effect.value1)
        elif effect.effect_type == EFFECT_GOOD_IMPRESSION_ADDITIVE:
            additive += max(0, effect.value1)
        elif effect.effect_type == EFFECT_PLAYABLE_VALUE_ADD:
            playable += _positive_effect_count(effect)
        elif effect.effect_type == EFFECT_EXTRA_TURN:
            extra_turns += _positive_effect_count(effect)
        elif effect.effect_type == EFFECT_CARD_DRAW:
            draw_count += max(0, effect.value1)
    return Plan2RewardCardFeatures(
        review_gain=review_gain,
        review_conversion_permil=conversion,
        review_multiplier_permil=multiplier,
        review_additive_permil=additive,
        playable_count=playable,
        extra_turns=extra_turns,
        draw_count=draw_count,
        review_cost=(
            max(0, card.cost_value) if card.cost_type == _COST_GOOD_IMPRESSION else 0
        ),
        stamina_cost=max(0, card.stamina_cost),
        force_stamina_cost=max(0, card.force_stamina_cost),
        moves_to_lost=card.move_position_type == "ProduceCardMovePositionType_Lost",
    )


def _deck_identity(value: str) -> tuple[str, int]:
    if not isinstance(value, str) or not value:
        raise ValueError("deck keys must be CARD_ID@UPGRADE")
    card_id, separator, raw_upgrade = value.rpartition("@")
    if not separator or not card_id or not raw_upgrade.isdigit():
        raise ValueError("deck keys must be CARD_ID@UPGRADE")
    return card_id, int(raw_upgrade)


def summarize_plan2_reward_deck(
    deck_counts: Mapping[str, int] | None,
    *,
    database: Path = DEFAULT_DATABASE,
) -> Plan2RewardDeckSummary:
    if deck_counts is None:
        return Plan2RewardDeckSummary(False, 0, 0, 0, 0, 0, 0)
    generators = conversions = review_gain = conversion = playable = card_count = 0
    for key, count in deck_counts.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("deck counts must be positive integers")
        card_id, upgrade = _deck_identity(key)
        features = plan2_reward_card_features(
            load_master_card(card_id, upgrade, database)
        )
        card_count += count
        generators += count if features.review_gain > 0 else 0
        conversions += count if features.review_conversion_permil > 0 else 0
        review_gain += count * features.review_gain
        conversion += count * features.review_conversion_permil
        playable += count * (features.playable_count + features.extra_turns)
    return Plan2RewardDeckSummary(
        True,
        card_count,
        generators,
        conversions,
        review_gain,
        conversion,
        playable,
    )


def _stable_deck_counts(deck_counts: Mapping[str, int]) -> dict[str, int]:
    """Merge every ``CARD_ID@UPGRADE`` entry by stable card ID.

    Upgrade/customization variants are the same card for final-deck
    composition.  Keep the exact keys elsewhere for the native duplicate
    semantics, but never create a sparse composition key for a variant.
    ``summarize_plan2_reward_deck`` has already validated the mapping; this
    helper repeats the small identity parse so the composition signal is
    explicit and cannot accidentally depend on one upgrade row.
    """

    merged: dict[str, int] = defaultdict(int)
    for key, count in deck_counts.items():
        card_id, _upgrade = _deck_identity(key)
        merged[card_id] += count
    return dict(merged)


@dataclass(frozen=True, slots=True)
class RewardSelectionState:
    offers: tuple[RewardCardOffer, ...]
    recommended_slot: int
    recommendation_basis: str

    @property
    def recommended_offer(self) -> RewardCardOffer:
        return next(
            offer for offer in self.offers if offer.slot == self.recommended_slot
        )

    @property
    def selected_slot(self) -> int | None:
        selected = [offer.slot for offer in self.offers if offer.selected]
        return selected[0] if len(selected) == 1 else None

    def to_dict(self) -> dict[str, object]:
        return {
            "offers": [offer.to_dict() for offer in self.offers],
            "recommended_slot": self.recommended_slot,
            "selected_slot": self.selected_slot,
            "recommendation_basis": self.recommendation_basis,
        }


@dataclass(frozen=True, slots=True)
class CardRemovalSemantics:
    """Native action contract; reward exclusion and deck deletion are distinct."""

    action_kind: str
    target_domain: str
    native_operation: str
    native_target_fields: tuple[str, ...]
    effect_type: str
    mutates_active_deck: bool
    excludes_from_future_reward_pool: bool
    excludes_from_result_memory_pool: bool


REWARD_CARD_EXCLUDE_SEMANTICS = CardRemovalSemantics(
    action_kind="reward_offer_exclude",
    target_domain="current_reward_offer",
    native_operation=REWARD_CARD_EXCLUDE_NATIVE_REQUEST,
    native_target_fields=("pick_index",),
    effect_type=REWARD_CARD_EXCLUDE_EFFECT_TYPE,
    mutates_active_deck=False,
    excludes_from_future_reward_pool=True,
    excludes_from_result_memory_pool=True,
)

PRODUCE_CARD_DELETE_SEMANTICS = CardRemovalSemantics(
    action_kind="produce_card_delete",
    target_domain="active_deck",
    native_operation="ProduceCardDelete/ProduceStepShopDeleteCardRequestAsync",
    native_target_fields=PRODUCE_CARD_DELETE_NATIVE_TARGET_FIELDS,
    effect_type=PRODUCE_CARD_DELETE_EFFECT_TYPE,
    mutates_active_deck=True,
    excludes_from_future_reward_pool=False,
    excludes_from_result_memory_pool=False,
)


@dataclass(frozen=True, slots=True)
class RewardCardExcludePlan:
    """Permanently exclude one offer for this Produce without changing the deck."""

    ACTION_KIND: ClassVar[str] = "reward_offer_exclude"
    selected_slot: int
    native_pick_index: int
    card_id: str
    upgrade: int
    display_name: str
    remaining_exclude_count_before: int
    mutates_active_deck: bool = False
    excludes_from_future_reward_pool: bool = True
    excludes_from_result_memory_pool: bool = True
    requires_fresh_reward_capture: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "action_kind": self.ACTION_KIND,
            "selected_slot": self.selected_slot,
            "native_request": REWARD_CARD_EXCLUDE_NATIVE_REQUEST,
            "native_pick_index": self.native_pick_index,
            "target_domain": REWARD_CARD_EXCLUDE_SEMANTICS.target_domain,
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "display_name": self.display_name,
            "remaining_exclude_count_before": self.remaining_exclude_count_before,
            "mutates_active_deck": self.mutates_active_deck,
            "excludes_from_future_reward_pool": self.excludes_from_future_reward_pool,
            "excludes_from_result_memory_pool": self.excludes_from_result_memory_pool,
            "requires_fresh_reward_capture": self.requires_fresh_reward_capture,
        }


@dataclass(frozen=True, slots=True)
class RewardCardExcludeTransition:
    """Verified persistent result of replacing one excluded reward offer."""

    replaced_slot: int
    excluded_card_id: str
    replacement_card_id: str
    replacement_upgrade: int
    excluded_card_ids: tuple[str, ...]
    remaining_exclude_count_after: int
    mutates_active_deck: bool = False
    excludes_from_future_reward_pool: bool = True
    excludes_from_result_memory_pool: bool = True

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def plan_reward_card_exclude(
    state: RewardSelectionState,
    *,
    remaining_exclude_count: int,
) -> RewardCardExcludePlan:
    """Bind native ``PickIndex`` to the selected offer, never to a deck card.

    The screen uses one-based public slot numbers while the native presenter
    receives the zero-based list index.  The excluded card is removed from the
    remaining reward pool for this Produce and from the result-memory card
    candidate pool; it is not removed from the active deck.  The server response
    may replace the visible offer, so callers must capture and identify the
    refreshed reward list before making another decision.
    """

    if not isinstance(state, RewardSelectionState):
        raise TypeError("state must be RewardSelectionState")
    if (
        not isinstance(remaining_exclude_count, int)
        or isinstance(remaining_exclude_count, bool)
        or remaining_exclude_count <= 0
    ):
        raise ValueError("remaining_exclude_count must be a positive integer")
    selected_slot = state.selected_slot
    if selected_slot is None:
        raise ValueError("reward exclusion requires exactly one selected offer")
    offer = next(item for item in state.offers if item.slot == selected_slot)
    return RewardCardExcludePlan(
        selected_slot=selected_slot,
        native_pick_index=selected_slot - 1,
        card_id=offer.card_id,
        upgrade=offer.upgrade,
        display_name=offer.display_name,
        remaining_exclude_count_before=remaining_exclude_count,
    )


def reconcile_reward_card_exclude(
    before: RewardSelectionState,
    after: RewardSelectionState,
    plan: RewardCardExcludePlan,
    *,
    excluded_card_ids_before: Sequence[str] = (),
) -> RewardCardExcludeTransition:
    """Accept only a fresh reward list that replaced the planned offer.

    The native response updates one ``PickIndex``.  Keeping the unchanged slots
    exact prevents a stale or unrelated reward screen from being committed as
    the result.  The returned exclusion list belongs to the whole Produce and
    must also be supplied to result-memory generation.
    """

    if not isinstance(before, RewardSelectionState):
        raise TypeError("before must be RewardSelectionState")
    if not isinstance(after, RewardSelectionState):
        raise TypeError("after must be RewardSelectionState")
    if not isinstance(plan, RewardCardExcludePlan):
        raise TypeError("plan must be RewardCardExcludePlan")
    normalized_excluded: list[str] = []
    for card_id in excluded_card_ids_before:
        if not isinstance(card_id, str) or not card_id:
            raise ValueError("excluded_card_ids_before must contain card IDs")
        if card_id not in normalized_excluded:
            normalized_excluded.append(card_id)

    before_by_slot = {offer.slot: offer for offer in before.offers}
    after_by_slot = {offer.slot: offer for offer in after.offers}
    if set(before_by_slot) != set(after_by_slot):
        raise ValueError("refreshed reward slots differ from the planned screen")
    selected = before_by_slot.get(plan.selected_slot)
    if selected is None or selected.card_id != plan.card_id or selected.upgrade != plan.upgrade:
        raise ValueError("exclude plan no longer matches the selected reward offer")
    if plan.native_pick_index != plan.selected_slot - 1:
        raise ValueError("exclude plan PickIndex does not match its selected slot")

    replacement = after_by_slot[plan.selected_slot]
    if replacement.card_id == plan.card_id:
        raise ValueError("excluded card still occupies the refreshed reward slot")
    if any(offer.card_id == plan.card_id for offer in after.offers):
        raise ValueError("excluded card remains in the refreshed reward list")
    for slot, prior in before_by_slot.items():
        if slot == plan.selected_slot:
            continue
        current = after_by_slot[slot]
        if (current.card_id, current.upgrade) != (prior.card_id, prior.upgrade):
            raise ValueError("more than the selected reward slot changed")

    if plan.card_id not in normalized_excluded:
        normalized_excluded.append(plan.card_id)
    return RewardCardExcludeTransition(
        replaced_slot=plan.selected_slot,
        excluded_card_id=plan.card_id,
        replacement_card_id=replacement.card_id,
        replacement_upgrade=replacement.upgrade,
        excluded_card_ids=tuple(normalized_excluded),
        remaining_exclude_count_after=plan.remaining_exclude_count_before - 1,
    )


@dataclass(frozen=True, slots=True)
class RewardThumbnailEvidence:
    slot: int
    box: tuple[int, int, int, int]
    asset_name: str
    art_score: float
    art_margin: float
    ambiguous: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RewardPreviewProbe:
    """A reversible inspection request, never a collect/confirm action."""

    slot: int
    box: tuple[int, int, int, int]
    operation: str = "select_for_preview"
    reversible: bool = True
    collect_allowed: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RewardPreviewIdentity:
    slot: int
    selected_slot: int
    observed_text: str
    ocr_confidence: float
    card_id: str
    upgrade: int
    display_name: str
    plan_type: str
    asset_name: str | None
    match_method: str = "exact-localized-master-title"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RewardResolvedIdentity:
    """One reward slot whose identity is already supported by visible evidence."""

    slot: int
    card_id: str
    upgrade: int
    display_name: str
    plan_type: str
    asset_name: str | None
    confidence: float
    match_method: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RewardPreviewDisambiguation:
    """Immutable state for a select-preview-only reward inspection sequence."""

    plan_type: str
    slot_boxes: tuple[tuple[int, int, int, int], ...]
    thumbnail_evidence: tuple[RewardThumbnailEvidence, ...]
    preview_identities: tuple[RewardPreviewIdentity, ...] = ()

    @property
    def unresolved_slots(self) -> tuple[int, ...]:
        resolved = frozenset(item.slot for item in self.preview_identities)
        return tuple(
            item.slot
            for item in self.thumbnail_evidence
            if item.ambiguous and item.slot not in resolved
        )

    @property
    def next_probe(self) -> RewardPreviewProbe | None:
        unresolved = self.unresolved_slots
        if not unresolved:
            return None
        slot = unresolved[0]
        return RewardPreviewProbe(slot=slot, box=self.slot_boxes[slot - 1])

    @property
    def complete(self) -> bool:
        return not self.unresolved_slots

    def to_dict(self) -> dict[str, object]:
        probe = self.next_probe
        return {
            "plan_type": self.plan_type,
            "candidate_plan_types": [
                "ProducePlanType_Common",
                self.plan_type,
            ],
            "thumbnail_evidence": [
                item.to_dict() for item in self.thumbnail_evidence
            ],
            "preview_identities": [
                item.to_dict() for item in self.preview_identities
            ],
            "unresolved_slots": list(self.unresolved_slots),
            "next_probe": None if probe is None else probe.to_dict(),
            "status": "identified" if self.complete else "needs_preview",
            "collect_allowed": False,
        }


def _red_orange_pixels(rgb: np.ndarray) -> np.ndarray:
    red, green, blue = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    return (
        (red > 0.80)
        & (red - green > 0.12)
        & (red - blue > 0.25)
    )


def _load_rgb(source: Path | Image.Image, size: tuple[int, int]) -> np.ndarray:
    if isinstance(source, Image.Image):
        image = source
        close = False
    else:
        image = Image.open(source)
        close = True
    try:
        return np.asarray(
            image.convert("RGB").resize(size, Image.Resampling.LANCZOS),
            dtype=np.float32,
        ) / 255.0
    finally:
        if close:
            image.close()


def detect_reward_upgrade(
    rendered: Image.Image,
    matched_art: Path | Image.Image,
) -> int:
    """Return 1 when the reward tile has the visible rainbow ``+`` overlay.

    Reward choices in this flow are either base or +1.  Comparing red/orange
    pixels against the matched unadorned artwork avoids confusing naturally
    orange cards with the upgrade glyph.
    """

    observed = np.asarray(rendered.convert("RGB"), dtype=np.float32) / 255.0
    height, width = observed.shape[:2]
    candidate = _load_rgb(matched_art, (width, height))
    # The real upgrade marker is a compact red/orange plus centred near the
    # card's right edge.  Cost/effect badges can form a tall red strip in the
    # same broad area, so density alone is insufficient: a plus must have both
    # a long horizontal stroke and a long vertical stroke.
    left, right = round(width * 0.74), round(width * 0.98)
    top, bottom = round(height * 0.34), round(height * 0.60)
    observed_roi = observed[top:bottom, left:right]
    candidate_roi = candidate[top:bottom, left:right]
    observed_red = _red_orange_pixels(observed_roi)
    candidate_red = _red_orange_pixels(candidate_roi)
    residual = observed_red & (
        ~candidate_red
        | (np.mean(np.abs(observed_roi - candidate_roi), axis=2) > 0.18)
    )
    density = float(np.mean(residual))
    horizontal_stroke = float(np.max(np.mean(residual, axis=1)))
    vertical_stroke = float(np.max(np.mean(residual, axis=0)))
    return 1 if (
        density >= 0.08
        and horizontal_stroke >= 0.45
        and vertical_stroke >= 0.40
    ) else 0


def reward_card_is_selected(
    image: Image.Image,
    box: tuple[int, int, int, int],
) -> bool:
    """Detect the orange corner frame drawn around the selected reward."""

    x, y, width, height = box
    padding = max(8, round(min(width, height) * 0.13))
    left = max(0, x - padding)
    top = max(0, y - padding)
    right = min(image.width, x + width + padding)
    bottom = min(image.height, y + height + padding)
    rgb = np.asarray(
        image.crop((left, top, right, bottom)).convert("RGB"),
        dtype=np.float32,
    ) / 255.0
    yy, xx = np.mgrid[: rgb.shape[0], : rgb.shape[1]]
    inner = (
        (xx >= x - left)
        & (xx < x - left + width)
        & (yy >= y - top)
        & (yy < y - top + height)
    )
    ring = ~inner
    red, green, blue = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    orange = (
        (red > 0.82)
        & (green > 0.25)
        & (green < 0.78)
        & (blue < 0.32)
        & ring
    )
    ring_pixels = max(1, int(np.sum(ring)))
    return int(np.sum(orange)) / ring_pixels >= 0.12


def describe_master_card(card: MasterCard) -> tuple[str, ...]:
    """Render the verified Master effect slice as compact Chinese text."""

    descriptions: list[str] = []
    if card.force_stamina_cost:
        descriptions.append(f"直接耗體力 {card.force_stamina_cost}")
    elif card.stamina_cost:
        descriptions.append(f"體力／元氣成本 {card.stamina_cost}")
    if card.cost_type == _COST_GOOD_IMPRESSION and card.cost_value:
        descriptions.append(f"消耗好印象 {card.cost_value}")
    elif card.cost_type == _COST_MOTIVATION and card.cost_value:
        descriptions.append(f"消耗幹勁 {card.cost_value}")
    for effect in card.effects:
        condition = ""
        if effect.trigger_id == _TROUBLE_COUNT_TRIGGER:
            condition = "未除外干擾卡至少 2 張時，"
        elif effect.trigger_id:
            condition = f"條件 {effect.trigger_id}："
        if effect.effect_type == EFFECT_BLOCK:
            description = f"元氣 +{effect.value1}"
        elif effect.effect_type == EFFECT_GOOD_IMPRESSION:
            description = f"好印象 +{effect.value1}"
        elif effect.effect_type == EFFECT_MOTIVATION:
            description = f"幹勁 +{effect.value1}"
        elif effect.effect_type == EFFECT_GOOD_IMPRESSION_MULTIPLE:
            description = f"好印象 ×{(1000 + effect.value1) / 1000:g}"
        elif effect.effect_type == EFFECT_LESSON:
            description = f"打分 +{effect.value1}"
        elif effect.effect_type == EFFECT_LESSON_BY_BLOCK:
            description = f"按元氣 {effect.value1 / 10:g}% 打分"
        elif effect.effect_type == EFFECT_LESSON_BY_GOOD_IMPRESSION:
            description = f"按好印象 {effect.value1 / 10:g}% 打分"
        elif effect.effect_type == EFFECT_LESSON_BY_MOTIVATION:
            description = f"按幹勁 {effect.value1 / 10:g}% 打分"
        elif effect.effect_type == _EFFECT_PLAYABLE_COUNT:
            description = f"技能卡使用次數 +{effect.effect_count}"
        else:
            description = f"尚未翻譯：{effect.effect_type}"
        descriptions.append(condition + description)
    if card.move_position_type == "ProduceCardMovePositionType_Lost":
        descriptions.append("訓練中限 1 次")
    return tuple(descriptions)


def _rarity_rank(rarity: str) -> int:
    return {
        "ProduceCardRarity_N": 1,
        "ProduceCardRarity_R": 2,
        "ProduceCardRarity_Sr": 3,
        "ProduceCardRarity_Ssr": 4,
        "ProduceCardRarity_Legend": 5,
    }.get(rarity, 0)


def rank_reward_offers(
    offers: Sequence[RewardCardOffer],
) -> tuple[RewardCardOffer, ...]:
    """Return the deterministic Master-backed display ranking."""

    return tuple(
        sorted(
            offers,
            key=lambda offer: (
                offer.evaluation,
                _rarity_rank(offer.rarity),
                offer.game_recommended,
                offer.art_score,
                -offer.slot,
            ),
            reverse=True,
        )
    )


def rank_generic_nia_reward_offers(
    offers: Sequence[RewardCardOffer],
    *,
    plan_type: str,
    produce_id: str,
    idol_card_id: str,
    exam_effect_type: str,
    deck_counts: Mapping[str, int] | None = None,
    leaderboard_prior: (
        Plan2LeaderboardCardPrior | Plan2HierarchicalLeaderboardCardPrior | None
    ) = None,
    database: Path = DEFAULT_DATABASE,
) -> GenericRewardRanking:
    """Rank Plan1/Plan3 (and other non-Plan2) rewards with scoped evidence.

    Offer identity, legality, rarity and the game's displayed evaluation stay
    Master/OCR authoritative.  The leaderboard signal is additive and only
    applies when its complete ``produce + plan + effect`` scope matches; an
    exact idol statistic wins over the same-scope broad statistic inside the
    prior.  No Plan2 simulator or Review/Aggressive rule is reused here.
    """

    if plan_type not in SUPPORTED_REWARD_PLAN_TYPES:
        raise ValueError(f"unsupported NIA reward plan type: {plan_type!r}")
    candidates = tuple(offers)
    stable_counts = (
        None if deck_counts is None else _stable_deck_counts(deck_counts)
    )
    evaluations: list[Plan2RewardOfferEvaluation] = []
    for offer in candidates:
        # Loading the Master row is an intentional authority check.  The
        # visible candidate list remains unchanged if a row is absent: the
        # caller can still use the already resolved offer and baseline order.
        try:
            master = load_master_card(offer.card_id, offer.upgrade, database)
            master_plan = getattr(master, "plan_type", plan_type)
        except (KeyError, OSError, TypeError, ValueError):
            master = None
            master_plan = plan_type
        static_score = (
            offer.evaluation * 12
            + _rarity_rank(offer.rarity) * 55
            + (25 if offer.game_recommended else 0)
            + round(max(0.0, offer.art_score) * 10)
            - offer.slot
        )
        reasons: list[str] = ["archetype:master-scoped"]
        if master is not None and master_plan != plan_type:
            # Master legality remains the authority.  Do not create a hard
            # blocker from this advisory mismatch; just make it visible.
            reasons.append("master-plan-mismatch")

        prior_stat = None
        prior_scope = None
        prior_bonus = 0
        composition_current: int | None = None
        composition_target: float | None = None
        composition_gap: float | None = None
        composition_bonus = 0
        if isinstance(
            leaderboard_prior,
            (Plan2LeaderboardCardPrior, Plan2HierarchicalLeaderboardCardPrior),
        ) and leaderboard_prior.applies_to(
            produce_id=produce_id,
            idol_card_id=idol_card_id,
            plan_type=plan_type,
            exam_effect_type=exam_effect_type,
        ):
            prior_stat, prior_scope = leaderboard_prior.statistic_for(offer.card_id)
            if stable_counts is None:
                prior_bonus = leaderboard_prior.score_for(offer.card_id, offer.upgrade)
                reasons.append(
                    "composition-prior:"
                    f"scope={prior_scope or 'none'},bonus={prior_bonus}"
                )
            else:
                composition_current = stable_counts.get(offer.card_id, 0)
                composition_target = (
                    0.0 if prior_stat is None else prior_stat.mean_copy_count
                )
                composition_gap = max(
                    0.0,
                    composition_target - composition_current,
                )
                composition_bonus = leaderboard_prior.composition_score_for(
                    offer.card_id,
                    offer.upgrade,
                    composition_current,
                    0,
                )
                prior_bonus = composition_bonus
                reasons.append(
                    "composition-prior:"
                    f"scope={prior_scope or 'none'},"
                    f"current={composition_current},"
                    f"target={composition_target:g},"
                    f"gap={composition_gap:g},"
                    f"bonus={composition_bonus}"
                )
        elif leaderboard_prior is not None:
            reasons.append("composition-prior:scope=out-of-scope,bonus=0")

        total = static_score + prior_bonus
        evaluations.append(
            Plan2RewardOfferEvaluation(
                offer=offer,
                strategic_score=total,
                duplicate_count=(
                    0
                    if stable_counts is None
                    else stable_counts.get(offer.card_id, 0)
                ),
                features=Plan2RewardCardFeatures(),
                reasons=tuple(reasons),
                learned_prior_bonus=prior_bonus,
                composition_current_count=composition_current,
                composition_target_count=composition_target,
                composition_gap=composition_gap,
                composition_bonus=composition_bonus,
                composition_scope=prior_scope,
                composition_support=(
                    None if prior_stat is None else prior_stat.trajectory_support
                ),
            )
        )
    evaluations.sort(
        key=lambda value: (
            value.strategic_score,
            value.offer.evaluation,
            _rarity_rank(value.offer.rarity),
            value.offer.game_recommended,
            value.offer.art_score,
            -value.offer.slot,
        ),
        reverse=True,
    )
    return GenericRewardRanking(
        tuple(value.offer for value in evaluations),
        "master-scoped-composition-prior",
        tuple(evaluations),
    )


# Verb-first alias for integrations that do not want the historical Plan2
# naming in their import surface.
rank_archetype_reward_offers = rank_generic_nia_reward_offers


def evaluate_plan2_reward_offers(
    offers: Sequence[RewardCardOffer],
    exam_effect_type: str,
    *,
    deck_counts: Mapping[str, int] | None = None,
    weeks_remaining: int | None = None,
    leaderboard_prior: Plan2LeaderboardCardPrior | None = None,
    database: Path = DEFAULT_DATABASE,
) -> tuple[Plan2RewardOfferEvaluation, ...]:
    """Evaluate reward cards against the current deck and route phase.

    The live screen remains authoritative about the three offers.  This policy
    only answers which verified Master card best improves the deck.  Review
    decks are deliberately phase-aware: early rewards build Review generation,
    while late rewards value conversion into score once that foundation exists.
    Missing deck/route context is a supported fallback, not a live blocker.
    """

    compatible_effects = _NIA_REWARD_COMPATIBLE_EFFECTS.get(exam_effect_type)
    if compatible_effects is None:
        raise ValueError(
            f"unsupported NIA Plan2 exam effect type: {exam_effect_type!r}"
        )
    if weeks_remaining is not None and (
        isinstance(weeks_remaining, bool)
        or not isinstance(weeks_remaining, int)
        or weeks_remaining < 0
    ):
        raise ValueError("weeks_remaining must be a non-negative integer")

    deck = summarize_plan2_reward_deck(deck_counts, database=database)
    exact_counts = dict(deck_counts or {})
    # ``None`` means the live deck is unknown and preserves the historical
    # flat leaderboard bonus.  An empty mapping is known to contain no cards,
    # so it intentionally participates in composition scoring.
    stable_counts = (
        None if deck_counts is None else _stable_deck_counts(deck_counts)
    )
    evaluations: list[Plan2RewardOfferEvaluation] = []
    for offer in offers:
        master = load_master_card(offer.card_id, offer.upgrade, database)
        features = plan2_reward_card_features(master)
        duplicate_count = exact_counts.get(f"{offer.card_id}@{offer.upgrade}", 0)
        direct_compatible = any(
            effect.effect_type in compatible_effects
            for effect in _iter_reward_effects(master)
        )
        reasons: list[str] = []
        score = (
            offer.evaluation * 12
            + _rarity_rank(offer.rarity) * 55
            + (25 if offer.game_recommended else 0)
        )

        if exam_effect_type == "ProduceExamEffectType_ExamReview":
            # Draw and additional plays are genuine Review-deck enablers even
            # when the card does not itself add Review or score.
            compatible = direct_compatible or any(
                (
                    features.review_gain,
                    features.review_conversion_permil,
                    features.review_multiplier_permil,
                    features.review_additive_permil,
                    features.playable_count,
                    features.extra_turns,
                    features.draw_count,
                    features.review_cost,
                )
            )
            if not compatible:
                score -= 4_000
                reasons.append("archetype:off-plan")
            else:
                reasons.append("archetype:review")

            if weeks_remaining is None:
                phase = "unknown"
                generator_weight, conversion_weight = 42, 65
            elif weeks_remaining >= 9:
                phase = "early"
                generator_weight, conversion_weight = 60, 25
            elif weeks_remaining <= 4:
                phase = "late"
                generator_weight, conversion_weight = 22, 135
            else:
                phase = "mid"
                generator_weight, conversion_weight = 42, 75
            reasons.append(f"route:{phase}")

            score += features.review_gain * generator_weight
            # Preserve sub-1000 conversion ratios.  Dividing first erased a
            # 600-permil converter entirely and truncated 1700 to 1000.
            score += (
                features.review_conversion_permil * conversion_weight
            ) // 1000
            score += (
                features.review_multiplier_permil
                + features.review_additive_permil
            ) // 20
            score += features.playable_count * 280
            score += features.extra_turns * 360
            score += features.draw_count * 70
            score -= features.review_cost * 24

            if deck.known:
                reasons.append(
                    "deck:"
                    f"generator={deck.review_generator_cards},"
                    f"converter={deck.review_conversion_cards}"
                )
                if (
                    features.review_gain > 0
                    and deck.review_generator_cards < 3
                ):
                    score += 260
                    reasons.append("fills:review-generator")
                elif (
                    features.review_gain > 0
                    and deck.review_generator_cards >= 6
                ):
                    score -= 120
                    reasons.append("surplus:review-generator")

                has_review_foundation = (
                    deck.review_generator_cards >= 2 or deck.review_gain >= 10
                )
                if features.review_conversion_permil > 0:
                    if not has_review_foundation:
                        score -= 550
                        reasons.append("premature:review-converter")
                    elif deck.review_conversion_cards < 2:
                        score += 280 if phase == "late" else 150
                        reasons.append("fills:review-converter")
            else:
                reasons.append("deck:unknown-fallback")

            if features.moves_to_lost:
                score += 110 if phase == "late" else 25
                reasons.append("deck:thinning")
            score -= features.stamina_cost * (14 if phase == "late" else 10)
            score -= features.force_stamina_cost * 40
        else:
            # Aggressive/Motivation keeps the established effect-based policy
            # until its deck-role model is backed by the same amount of native
            # evidence as Review.  The score form makes it composable with the
            # common duplicate and stamina terms without changing its order.
            score = (
                (1_000_000 if direct_compatible else 0)
                + offer.evaluation * 10_000
                + _rarity_rank(offer.rarity) * 100
            )
            reasons.append(
                "archetype:aggressive"
                if direct_compatible
                else "archetype:off-plan"
            )

        if duplicate_count:
            duplicate_penalty = duplicate_count * (
                180 + max(0, deck.card_count - 12) * 15
            )
            score -= duplicate_penalty
            reasons.append(f"duplicate:{duplicate_count}")

        learned_prior_bonus = 0
        composition_current_count: int | None = None
        composition_future_guaranteed_count: int | None = None
        composition_target_count: float | None = None
        composition_gap: float | None = None
        composition_bonus = 0
        composition_scope: str | None = None
        composition_support: int | None = None
        if (
            isinstance(
                leaderboard_prior,
                (Plan2LeaderboardCardPrior, Plan2HierarchicalLeaderboardCardPrior),
            )
            and leaderboard_prior.plan_type == "ProducePlanType_Plan2"
            and leaderboard_prior.exam_effect_type == exam_effect_type
        ):
            stat, composition_scope = leaderboard_prior.statistic_for(offer.card_id)
            composition_support = (
                None if stat is None else stat.trajectory_support
            )
            if stable_counts is None:
                # Keep the old behavior when the caller cannot provide deck
                # authority.  This is deliberately not a guessed zero-count
                # composition decision.
                learned_prior_bonus = leaderboard_prior.score_for(
                    offer.card_id,
                    offer.upgrade,
                )
                reasons.append(
                    "composition-prior:"
                    f"scope={composition_scope or 'none'},"
                    f"support={composition_support or 0},"
                    "target=unknown,current=unknown,gap=unknown,"
                    f"bonus={learned_prior_bonus}"
                )
            else:
                composition_current_count = stable_counts.get(offer.card_id, 0)
                composition_future_guaranteed_count = 0
                composition_target_count = (
                    0.0 if stat is None else stat.mean_copy_count
                )
                composition_gap = max(
                    0.0,
                    composition_target_count
                    - composition_current_count
                    - composition_future_guaranteed_count,
                )
                composition_bonus = leaderboard_prior.composition_score_for(
                    offer.card_id,
                    offer.upgrade,
                    composition_current_count,
                    composition_future_guaranteed_count,
                )
                learned_prior_bonus = composition_bonus
                reasons.append(
                    "composition:"
                    f"current={composition_current_count},"
                    f"future={composition_future_guaranteed_count},"
                    f"target={composition_target_count:g},"
                    f"gap={composition_gap:g},"
                    f"bonus={composition_bonus}"
                )
                # Keep the historical compact composition reason above for
                # compatibility, and add a scope/support record for telemetry
                # so exact and broad evidence are distinguishable at runtime.
                reasons.append(
                    "composition-prior:"
                    f"scope={composition_scope or 'none'},"
                    f"support={composition_support or 0},"
                    f"target={composition_target_count:g},"
                    f"current={composition_current_count},"
                    f"gap={composition_gap:g},"
                    f"bonus={composition_bonus}"
                )
            if learned_prior_bonus:
                score += learned_prior_bonus
                reasons.append(f"learned:leaderboard-card-prior={learned_prior_bonus}")

        evaluations.append(
            Plan2RewardOfferEvaluation(
                offer=offer,
                strategic_score=score,
                duplicate_count=duplicate_count,
                features=features,
                reasons=tuple(reasons),
                learned_prior_bonus=learned_prior_bonus,
                composition_current_count=composition_current_count,
                composition_future_guaranteed_count=(
                    composition_future_guaranteed_count
                ),
                composition_target_count=composition_target_count,
                composition_gap=composition_gap,
                composition_bonus=composition_bonus,
                composition_scope=composition_scope,
                composition_support=composition_support,
            )
        )

    return tuple(
        sorted(
            evaluations,
            key=lambda item: (
                item.strategic_score,
                item.offer.evaluation,
                _rarity_rank(item.offer.rarity),
                item.offer.game_recommended,
                item.offer.art_score,
                -item.offer.slot,
            ),
            reverse=True,
        )
    )


def plan_nia_reward_reroll(
    offers: Sequence[RewardCardOffer],
    exam_effect_type: str,
    *,
    authority: RewardCardRerollAuthority | None,
    has_dedicated_button_action: bool,
    deck_counts: Mapping[str, int] | None = None,
    weeks_remaining: int | None = None,
    leaderboard_prior: Plan2LeaderboardCardPrior | None = None,
    conservative_baseline: int = 0,
    database: Path = DEFAULT_DATABASE,
) -> NiaRewardRerollDecision:
    """Reroll only a completely resolved, conservatively poor three-card set.

    A set is eligible only when every offer scores below the caller-visible
    baseline, or every offer is explicitly off-plan for the active archetype.
    Missing server availability or a dedicated Maa action disables the
    optional reroll and falls back to receiving the best verified offer.  It
    must never turn a legal reward page into a whole-run stop, nor substitute
    OCR text, colour, or a fixed coordinate for the missing authority.
    """

    candidates = tuple(offers)
    if (
        len(candidates) != 3
        or any(not isinstance(value, RewardCardOffer) for value in candidates)
        or {value.slot for value in candidates} != {1, 2, 3}
    ):
        return NiaRewardRerollDecision(
            "blocked",
            "reward candidates are not the complete three-slot offer",
            (),
            authority=authority,
            blocker=RewardCardRerollBlocker(
                "reward-reroll-candidates-incomplete",
                "offers",
                "resolved_slots="
                + repr(
                    sorted(
                        value.slot
                        for value in candidates
                        if isinstance(value, RewardCardOffer)
                    )
                ),
            ),
            conservative_baseline=conservative_baseline,
        )
    if isinstance(conservative_baseline, bool) or not isinstance(
        conservative_baseline, int
    ):
        raise TypeError("conservative_baseline must be an integer")
    if not isinstance(has_dedicated_button_action, bool):
        raise TypeError("has_dedicated_button_action must be bool")

    evaluations = evaluate_plan2_reward_offers(
        candidates,
        exam_effect_type,
        deck_counts=deck_counts,
        weeks_remaining=weeks_remaining,
        leaderboard_prior=leaderboard_prior,
        database=database,
    )
    all_below_baseline = all(
        value.strategic_score < conservative_baseline for value in evaluations
    )
    all_off_plan = all(
        "archetype:off-plan" in value.reasons for value in evaluations
    )
    if not (all_below_baseline or all_off_plan):
        return NiaRewardRerollDecision(
            "receive",
            "at least one candidate clears the conservative keep policy",
            evaluations,
            authority=authority,
            conservative_baseline=conservative_baseline,
        )

    poor_reason = (
        "all-off-plan" if all_off_plan else "all-below-conservative-baseline"
    )
    if authority is None:
        return NiaRewardRerollDecision(
            "receive",
            "poor set but reroll authority is unavailable; keep best offer",
            evaluations,
            conservative_baseline=conservative_baseline,
        )
    if authority.hidden or authority.remaining_count == 0:
        return NiaRewardRerollDecision(
            "receive",
            "poor set but server state exposes no legal reroll",
            evaluations,
            authority=authority,
            conservative_baseline=conservative_baseline,
        )
    if not has_dedicated_button_action:
        return NiaRewardRerollDecision(
            "receive",
            "poor set but no capture-bound Maa reroll action; keep best offer",
            evaluations,
            authority=authority,
            conservative_baseline=conservative_baseline,
        )
    return NiaRewardRerollDecision(
        "reroll",
        poor_reason,
        evaluations,
        authority=authority,
        conservative_baseline=conservative_baseline,
    )


def rank_plan2_reward_offers(
    offers: Sequence[RewardCardOffer],
    exam_effect_type: str,
    *,
    deck_counts: Mapping[str, int] | None = None,
    weeks_remaining: int | None = None,
    leaderboard_prior: Plan2LeaderboardCardPrior | None = None,
    database: Path = DEFAULT_DATABASE,
) -> tuple[RewardCardOffer, ...]:
    return tuple(
        item.offer
        for item in evaluate_plan2_reward_offers(
            offers,
            exam_effect_type,
            deck_counts=deck_counts,
            weeks_remaining=weeks_remaining,
            leaderboard_prior=leaderboard_prior,
            database=database,
        )
    )


def rank_nia_plan2_reward_offers(
    offers: Sequence[RewardCardOffer],
    exam_effect_type: str,
    *,
    deck_counts: Mapping[str, int] | None = None,
    weeks_remaining: int | None = None,
    leaderboard_prior: Plan2LeaderboardCardPrior | None = None,
    database: Path = DEFAULT_DATABASE,
) -> tuple[RewardCardOffer, ...]:
    """Rank NIA Plan2 reward offers for the idol's exam archetype.

    Archetype compatibility is derived from each offer's ordered Master card
    effects.  A compatible offer always sorts ahead of an incompatible offer;
    within either group the existing evaluation, rarity, in-game
    recommendation, art confidence, and slot tie-breaks are preserved.  Only
    the two NIA Plan2 archetypes whose semantics are established here are
    accepted.  Unknown archetypes fail closed instead of silently using the
    generic ranking.

    ``database`` is injectable for callers/tests using an imported Master
    snapshot; card IDs and idol IDs are never used as compatibility rules.
    """

    return rank_plan2_reward_offers(
        offers,
        exam_effect_type,
        deck_counts=deck_counts,
        weeks_remaining=weeks_remaining,
        leaderboard_prior=leaderboard_prior,
        database=database,
    )


def _validate_reward_layout(
    detections: tuple[Any, ...], image: Image.Image
) -> None:
    if len(detections) not in {3, 4}:
        raise ValueError(
            f"獎勵畫面必須辨識到 3 或 4 張卡，目前為 {len(detections)}"
        )
    for item in detections:
        width_ratio = item.width / image.width
        height_ratio = item.height / image.height
        y_ratio = item.y / image.height
        if not (0.13 <= width_ratio <= 0.24):
            raise ValueError("獎勵卡寬度不符合選擇畫面縮圖")
        if not (0.075 <= height_ratio <= 0.15):
            raise ValueError("獎勵卡高度不符合選擇畫面縮圖")
        if not (0.55 <= y_ratio <= 0.82):
            raise ValueError("獎勵卡位置不符合選擇畫面")


def _is_reward_card_candidate(item: Any, image: Image.Image) -> bool:
    """Return whether a card-like detection can belong to the reward panel.

    The general detector can occasionally classify the square schedule icon
    near the top-left as a card. Reward cards always occupy the lower central
    panel, so geometry is applied before enforcing the three-card invariant.
    """

    if item.label != "cards":
        return False
    width_ratio = item.width / image.width
    height_ratio = item.height / image.height
    y_ratio = item.y / image.height
    return (
        0.13 <= width_ratio <= 0.24
        and 0.075 <= height_ratio <= 0.15
        and 0.55 <= y_ratio <= 0.82
    )


def _reward_cards(
    image: Image.Image,
    detections: tuple[Any, ...],
) -> tuple[Any, ...]:
    cards = tuple(
        sorted(
            (
                item
                for item in detections
                if _is_reward_card_candidate(item, image)
            ),
            key=lambda item: item.x,
        )
    )
    _validate_reward_layout(cards, image)
    return cards


def inspect_reward_thumbnails(
    image: Image.Image,
    detections: tuple[Any, ...],
    *,
    card_art: Mapping[str, Path | Image.Image],
    minimum_art_score: float = 0.30,
    minimum_art_margin: float = 0.075,
) -> tuple[RewardThumbnailEvidence, ...]:
    """Measure each compact tile without guessing below art thresholds."""

    cards = _reward_cards(image, detections)
    result: list[RewardThumbnailEvidence] = []
    for slot, item in enumerate(cards, 1):
        box = (item.x, item.y, item.width, item.height)
        rendered = image.crop(
            (item.x, item.y, item.x + item.width, item.y + item.height)
        )
        match = match_card_art_color(rendered, card_art)
        result.append(
            RewardThumbnailEvidence(
                slot=slot,
                box=box,
                asset_name=match.asset_name,
                art_score=match.score,
                art_margin=match.margin,
                ambiguous=bool(
                    match.score < minimum_art_score
                    or match.margin < minimum_art_margin
                ),
            )
        )
    return tuple(result)


def begin_reward_preview_disambiguation(
    image: Image.Image,
    detections: tuple[Any, ...],
    *,
    card_art: Mapping[str, Path | Image.Image],
    plan_type: str,
    minimum_art_score: float = 0.30,
    minimum_art_margin: float = 0.075,
) -> RewardPreviewDisambiguation:
    """Plan reversible selected-preview probes for ambiguous thumbnails."""

    plan_type = _validated_reward_plan_type(plan_type)
    evidence = inspect_reward_thumbnails(
        image,
        detections,
        card_art=card_art,
        minimum_art_score=minimum_art_score,
        minimum_art_margin=minimum_art_margin,
    )
    return RewardPreviewDisambiguation(
        plan_type=plan_type,
        slot_boxes=tuple(item.box for item in evidence),
        thumbnail_evidence=evidence,
    )


def _exact_preview_title_entry(
    preview_image: Image.Image,
    *,
    recognizer: Any,
    entries: tuple[CardNameEntry, ...],
    minimum_ocr_confidence: float,
) -> tuple[CardNameEntry, str, float]:
    from PIL import ImageOps

    from .screen_state import scale_canonical_box

    crop = preview_image.crop(
        scale_canonical_box(
            REWARD_PREVIEW_NAME_BOX,
            preview_image.width,
            preview_image.height,
        )
    )
    grayscale = ImageOps.autocontrast(crop.convert("L"))
    variants = (
        crop,
        grayscale,
        grayscale.point(lambda value: 255 if value > 150 else 0),
    )
    observations: list[tuple[CardNameEntry, str, float]] = []
    raw_results: list[str] = []
    for variant in variants:
        result = recognizer.recognize(variant)
        text = str(result.text)
        confidence = float(result.confidence)
        raw_results.append(f"{text!r}@{confidence:.1%}")
        normalized = normalize_card_text(text)
        if not normalized or confidence < minimum_ocr_confidence:
            continue
        matches = tuple(
            entry
            for entry in entries
            if normalized
            in {
                normalize_card_text(entry.official_name),
                normalize_card_text(entry.display_name),
            }
        )
        if len(matches) > 1:
            raise ValueError(
                f"reward preview title {text!r} conflicts inside Common + active plan: "
                + ", ".join(sorted(entry.card_id for entry in matches))
            )
        if matches:
            observations.append((matches[0], text, confidence))
    identities = frozenset(item[0].card_id for item in observations)
    if len(identities) > 1:
        raise ValueError(
            "reward preview OCR variants resolve to conflicting Master cards: "
            + ", ".join(sorted(identities))
        )
    if not observations:
        raise ValueError(
            "reward preview title has no exact local-translation Master match: "
            + ", ".join(raw_results)
        )
    return max(observations, key=lambda item: item[2])


def _art_for_reward_card(
    card_art: Mapping[str, Path | Image.Image],
    card_id: str,
) -> tuple[str, Path | Image.Image] | None:
    assets = tuple(
        (asset_name, source)
        for asset_name, source in card_art.items()
        if card_id.endswith("-" + card_suffix_from_asset(asset_name))
    )
    if len(assets) > 1:
        raise ValueError(
            f"reward card {card_id} has conflicting local art assets: "
            f"found {[name for name, _source in assets]}"
        )
    return assets[0] if assets else None


def _catalog_entry_for_card(
    catalog: CardNameCatalog,
    *,
    card_id: str,
    upgrade: int,
    plan_type: str,
) -> CardNameEntry:
    allowed_plans = {"ProducePlanType_Common", plan_type}
    entries = tuple(
        entry
        for entry in catalog.entries
        if entry.card_id == card_id
        and entry.upgrade == upgrade
        and entry.plan_type in allowed_plans
    )
    if len(entries) != 1:
        raise ValueError(
            f"translated Master identity is missing/conflicting for "
            f"{card_id}@{upgrade}: {len(entries)} rows"
        )
    return entries[0]


def identify_reward_preview(
    overview_image: Image.Image,
    preview_image: Image.Image,
    *,
    slot_boxes: tuple[tuple[int, int, int, int], ...],
    slot: int,
    plan_type: str,
    card_art: Mapping[str, Path | Image.Image],
    catalog: CardNameCatalog,
    recognizer: Any,
    database: Path = DEFAULT_DATABASE,
    minimum_ocr_confidence: float = 0.85,
    excluded_reward_card_ids: Iterable[str] = (),
) -> RewardPreviewIdentity:
    """Resolve one reversibly selected reward from its full title panel.

    This function consumes screenshots only.  It returns identity evidence and
    deliberately has no collect/confirm field or executable click payload.
    """

    plan_type = _validated_reward_plan_type(plan_type)
    excluded_ids = normalize_excluded_reward_card_ids(excluded_reward_card_ids)
    if overview_image.size != preview_image.size:
        raise ValueError("reward overview and preview dimensions differ")
    if not 1 <= slot <= len(slot_boxes):
        raise ValueError(f"reward preview slot {slot} is outside the layout")
    selected_slots = tuple(
        index
        for index, box in enumerate(slot_boxes, 1)
        if reward_card_is_selected(preview_image, box)
    )
    if selected_slots != (slot,):
        raise ValueError(
            f"reward preview must select only slot {slot}; observed {selected_slots}"
        )
    candidates = regular_reward_candidate_entries(
        catalog=catalog,
        plan_type=plan_type,
        database=database,
        include_upgrades=True,
        excluded_reward_card_ids=excluded_ids,
    )
    identity, observed_text, confidence = _exact_preview_title_entry(
        preview_image,
        recognizer=recognizer,
        entries=candidates,
        minimum_ocr_confidence=minimum_ocr_confidence,
    )
    art = _art_for_reward_card(card_art, identity.card_id)
    asset_name = None if art is None else art[0]
    if art is not None:
        _asset_name, matched_art = art
        x, y, width, height = slot_boxes[slot - 1]
        rendered = overview_image.crop((x, y, x + width, y + height))
        thumbnail_upgrade = detect_reward_upgrade(rendered, matched_art)
        if thumbnail_upgrade != identity.upgrade:
            raise ValueError(
                f"reward preview title/thumbnail upgrade conflict for slot {slot}: "
                f"{identity.upgrade} != {thumbnail_upgrade}"
            )
    master = load_master_card(identity.card_id, identity.upgrade, database)
    if master.plan_type != identity.plan_type:
        raise ValueError(
            f"translated catalog/Master plan conflict for {identity.card_id}: "
            f"{identity.plan_type} != {master.plan_type}"
        )
    return RewardPreviewIdentity(
        slot=slot,
        selected_slot=selected_slots[0],
        observed_text=observed_text,
        ocr_confidence=confidence,
        card_id=identity.card_id,
        upgrade=identity.upgrade,
        display_name=identity.display_name,
        plan_type=identity.plan_type,
        asset_name=asset_name,
    )


def advance_reward_preview_disambiguation(
    state: RewardPreviewDisambiguation,
    overview_image: Image.Image,
    preview_image: Image.Image,
    *,
    card_art: Mapping[str, Path | Image.Image],
    catalog: CardNameCatalog,
    recognizer: Any,
    database: Path = DEFAULT_DATABASE,
    excluded_reward_card_ids: Iterable[str] = (),
) -> RewardPreviewDisambiguation:
    """Consume exactly the next requested full preview and advance state."""

    if not isinstance(state, RewardPreviewDisambiguation):
        raise TypeError("state must be RewardPreviewDisambiguation")
    probe = state.next_probe
    if probe is None:
        raise ValueError("reward preview disambiguation is already complete")
    identity = identify_reward_preview(
        overview_image,
        preview_image,
        slot_boxes=state.slot_boxes,
        slot=probe.slot,
        plan_type=state.plan_type,
        card_art=card_art,
        catalog=catalog,
        recognizer=recognizer,
        database=database,
        excluded_reward_card_ids=excluded_reward_card_ids,
    )
    return replace(
        state,
        preview_identities=tuple(
            sorted((*state.preview_identities, identity), key=lambda item: item.slot)
        ),
    )


def resolved_reward_identities(
    state: RewardPreviewDisambiguation,
    overview_image: Image.Image,
    *,
    card_art: Mapping[str, Path | Image.Image],
    catalog: CardNameCatalog,
    database: Path = DEFAULT_DATABASE,
    excluded_reward_card_ids: Iterable[str] = (),
) -> tuple[RewardResolvedIdentity, ...]:
    """Return all slots already proven by a thumbnail or a full preview.

    Ambiguous thumbnails are deliberately absent until their requested full
    preview has been consumed.  This is presentation evidence only: it has no
    click, confirm, or collect semantics.
    """

    if not isinstance(state, RewardPreviewDisambiguation):
        raise TypeError("state must be RewardPreviewDisambiguation")
    excluded_ids = normalize_excluded_reward_card_ids(excluded_reward_card_ids)
    candidates = regular_reward_candidate_entries(
        catalog=catalog,
        plan_type=state.plan_type,
        database=database,
        excluded_reward_card_ids=excluded_ids,
    )
    previews = {item.slot: item for item in state.preview_identities}
    resolved: list[RewardResolvedIdentity] = []
    for evidence in state.thumbnail_evidence:
        preview = previews.get(evidence.slot)
        if preview is not None:
            if preview.card_id in excluded_ids:
                raise ValueError(
                    f"reward preview slot {preview.slot} is excluded from the "
                    "current reward candidate pool"
                )
            identity = _catalog_entry_for_card(
                catalog,
                card_id=preview.card_id,
                upgrade=preview.upgrade,
                plan_type=state.plan_type,
            )
            if (
                identity.display_name != preview.display_name
                or identity.plan_type != preview.plan_type
            ):
                raise ValueError(
                    f"reward preview identity changed for slot {preview.slot}"
                )
            art = _art_for_reward_card(card_art, preview.card_id)
            asset_name = None if art is None else art[0]
            if asset_name != preview.asset_name:
                raise ValueError(
                    f"reward preview local-art binding changed for slot "
                    f"{preview.slot}"
                )
            master = load_master_card(
                preview.card_id,
                preview.upgrade,
                database,
            )
            if master.plan_type not in {
                "ProducePlanType_Common",
                state.plan_type,
            }:
                raise ValueError(
                    f"reward Master plan conflict at slot {preview.slot}: "
                    f"{master.plan_type}"
                )
            resolved.append(
                RewardResolvedIdentity(
                    slot=preview.slot,
                    card_id=preview.card_id,
                    upgrade=preview.upgrade,
                    display_name=preview.display_name,
                    plan_type=preview.plan_type,
                    asset_name=asset_name,
                    confidence=preview.ocr_confidence,
                    match_method=preview.match_method,
                )
            )
            continue
        if evidence.ambiguous:
            continue
        base = _reward_asset_entry(evidence.asset_name, candidates)
        if base is None:
            raise ValueError(
                f"reward thumbnail slot {evidence.slot} is outside "
                f"Common + {state.plan_type}"
            )
        matched_art = card_art.get(evidence.asset_name)
        if matched_art is None:
            raise ValueError(
                f"reward thumbnail art disappeared for slot {evidence.slot}: "
                f"{evidence.asset_name}"
            )
        x, y, width, height = evidence.box
        rendered = overview_image.crop((x, y, x + width, y + height))
        upgrade = detect_reward_upgrade(rendered, matched_art)
        identity = _catalog_entry_for_card(
            catalog,
            card_id=base.card_id,
            upgrade=upgrade,
            plan_type=state.plan_type,
        )
        master = load_master_card(identity.card_id, identity.upgrade, database)
        if master.plan_type not in {
            "ProducePlanType_Common",
            state.plan_type,
        }:
            raise ValueError(
                f"reward Master plan conflict at slot {evidence.slot}: "
                f"{master.plan_type}"
            )
        resolved.append(
            RewardResolvedIdentity(
                slot=evidence.slot,
                card_id=identity.card_id,
                upgrade=identity.upgrade,
                display_name=identity.display_name,
                plan_type=identity.plan_type,
                asset_name=evidence.asset_name,
                confidence=evidence.art_score,
                match_method="confident-thumbnail-art",
            )
        )
    return tuple(sorted(resolved, key=lambda item: item.slot))


def _game_recommended_slots(
    detections: tuple[Any, ...],
    cards: tuple[Any, ...],
) -> frozenset[int]:
    """Map Maa's visible in-game recommendation frame to reward slots."""

    recommendation_boxes = tuple(
        item for item in detections if item.label == "recommend"
    )
    slots: set[int] = set()
    for marker in recommendation_boxes:
        marker_left = marker.x
        marker_top = marker.y
        marker_right = marker.x + marker.width
        marker_bottom = marker.y + marker.height
        overlaps: list[tuple[int, int]] = []
        for slot, card in enumerate(cards, 1):
            intersection_width = max(
                0,
                min(marker_right, card.x + card.width) - max(marker_left, card.x),
            )
            intersection_height = max(
                0,
                min(marker_bottom, card.y + card.height) - max(marker_top, card.y),
            )
            overlaps.append((intersection_width * intersection_height, slot))
        area, slot = max(overlaps, default=(0, 0))
        if slot and area >= cards[slot - 1].width * cards[slot - 1].height * 0.40:
            slots.add(slot)
    return frozenset(slots)


def read_reward_state(
    image: Image.Image,
    detections: tuple[Any, ...],
    *,
    card_art: Mapping[str, Path | Image.Image],
    catalog: CardNameCatalog,
    database: Path = DEFAULT_DATABASE,
    plan_type: str | None = None,
    preview_identities: Mapping[int, RewardPreviewIdentity] | None = None,
    minimum_art_score: float = 0.30,
    minimum_art_margin: float = 0.075,
    excluded_reward_card_ids: Iterable[str] = (),
) -> RewardSelectionState:
    cards = _reward_cards(image, detections)
    previews = dict(preview_identities or {})
    excluded_ids = normalize_excluded_reward_card_ids(excluded_reward_card_ids)
    if previews and plan_type is None:
        raise ValueError("preview identities require an active reward plan type")
    if excluded_ids and plan_type is None:
        raise ValueError(
            "excluded reward card IDs require an active reward plan type"
        )
    candidates = (
        ()
        if plan_type is None
        else regular_reward_candidate_entries(
            catalog=catalog,
            plan_type=_validated_reward_plan_type(plan_type),
            database=database,
            excluded_reward_card_ids=excluded_ids,
        )
    )
    game_recommended_slots = _game_recommended_slots(detections, cards)
    offers: list[RewardCardOffer] = []
    for slot, item in enumerate(cards, 1):
        box = (item.x, item.y, item.width, item.height)
        rendered = image.crop(
            (item.x, item.y, item.x + item.width, item.y + item.height)
        )
        match: CardArtMatch = match_card_art_color(rendered, card_art)
        ambiguous = bool(
            match.score < minimum_art_score
            or match.margin < minimum_art_margin
        )
        preview = previews.get(slot)
        if ambiguous and preview is None:
            raise ValueError(
                f"獎勵槽位 {slot} 卡圖仍有歧義："
                f"分數 {match.score:.1%}，領先 {match.margin:.1%}"
            )
        if preview is not None:
            assert plan_type is not None
            if preview.slot != slot or preview.selected_slot != slot:
                raise ValueError(
                    f"reward preview identity is bound to slot {preview.slot}, "
                    f"not slot {slot}"
                )
            if preview.plan_type not in {"ProducePlanType_Common", plan_type}:
                raise ValueError(
                    f"reward preview slot {slot} plan {preview.plan_type} is "
                    f"outside Common + {plan_type}"
                )
            identity = _catalog_entry_for_card(
                catalog,
                card_id=preview.card_id,
                upgrade=preview.upgrade,
                plan_type=plan_type,
            )
            if identity.card_id in excluded_ids:
                raise ValueError(
                    f"reward preview slot {slot} is excluded from the "
                    "current reward candidate pool"
                )
            art = _art_for_reward_card(
                card_art,
                identity.card_id,
            )
            asset_name = None if art is None else art[0]
            if preview.asset_name != asset_name:
                raise ValueError(
                    f"reward preview slot {slot} local-art binding changed: "
                    f"{preview.asset_name!r} != {asset_name!r}"
                )
            upgrade = identity.upgrade
            # A supplied full-title preview is the identity authority.  The
            # compact thumbnail remains useful only as slot geometry/art
            # confidence; visually similar or newly introduced cards must not
            # veto an exact localized Master title.
        else:
            asset_name = match.asset_name
            matched_art = card_art[asset_name]
            upgrade = detect_reward_upgrade(rendered, matched_art)
            if plan_type is None:
                identity = catalog.resolve_asset(asset_name, upgrade=upgrade)
            else:
                base = _reward_asset_entry(asset_name, candidates)
                if base is None:
                    raise ValueError(
                        f"reward thumbnail slot {slot} is outside Common + {plan_type}"
                    )
                identity = _catalog_entry_for_card(
                    catalog,
                    card_id=base.card_id,
                    upgrade=upgrade,
                    plan_type=plan_type,
                )
        master = load_master_card(identity.card_id, upgrade, database)
        if plan_type is not None and master.plan_type not in {
            "ProducePlanType_Common",
            plan_type,
        }:
            raise ValueError(
                f"reward Master plan conflict at slot {slot}: {master.plan_type}"
            )
        offers.append(
            RewardCardOffer(
                slot=slot,
                box=box,
                asset_name=asset_name,
                card_id=identity.card_id,
                upgrade=upgrade,
                display_name=identity.display_name,
                rarity=master.rarity,
                evaluation=master.evaluation,
                stamina_cost=master.stamina_cost,
                force_stamina_cost=master.force_stamina_cost,
                effect_summary=describe_master_card(master),
                art_score=match.score,
                art_margin=match.margin,
                game_recommended=slot in game_recommended_slots,
                selected=reward_card_is_selected(image, box),
            )
        )
    extra_previews = sorted(set(previews) - set(range(1, len(cards) + 1)))
    if extra_previews:
        raise ValueError(
            f"reward preview slots are outside the layout: {extra_previews}"
        )
    recommended = rank_reward_offers(offers)[0]
    return RewardSelectionState(
        offers=tuple(offers),
        recommended_slot=recommended.slot,
        recommendation_basis=(
            "依 Master evaluation 由高到低排序；同分再依稀有度、"
            "遊戲推薦標記、縮圖辨識分數與較前槽位決定。"
        ),
    )


def finalize_reward_preview_disambiguation(
    state: RewardPreviewDisambiguation,
    overview_image: Image.Image,
    detections: tuple[Any, ...],
    *,
    card_art: Mapping[str, Path | Image.Image],
    catalog: CardNameCatalog,
    database: Path = DEFAULT_DATABASE,
    excluded_reward_card_ids: Iterable[str] = (),
) -> RewardSelectionState:
    """Merge preview-proven ambiguous slots; never create a collect action."""

    if not isinstance(state, RewardPreviewDisambiguation):
        raise TypeError("state must be RewardPreviewDisambiguation")
    if not state.complete:
        raise ValueError(
            f"reward preview slots remain unresolved: {state.unresolved_slots}"
        )
    return read_reward_state(
        overview_image,
        detections,
        card_art=card_art,
        catalog=catalog,
        database=database,
        plan_type=state.plan_type,
        preview_identities={
            item.slot: item for item in state.preview_identities
        },
        excluded_reward_card_ids=excluded_reward_card_ids,
    )


def read_reward_path(path: Path, detections: tuple[Any, ...], **kwargs: Any):
    with Image.open(path.resolve()) as image:
        image.load()
        return read_reward_state(image, detections, **kwargs)
