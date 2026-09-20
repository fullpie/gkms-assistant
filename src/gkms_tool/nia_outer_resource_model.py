"""Exact outer-Produce stamina and N.I.A. outing resource projections.

The outer advisor used to treat the number printed on a lesson tile as the
whole stamina cost and treated Outing as if it shared Rest's recovery rate.
Neither assumption is true.  This module keeps the two pieces of evidence
separate:

* owned Produce items are reconstructed from ``ProducePlayLogSaveData`` and
  joined to their exact Master trigger/effect chain;
* one N.I.A. outing suggestion is compiled from its exact
  ``ProduceStepEventSuggestion`` row, including its non-stamina trade-offs.

It is deliberately click-free.  Unknown lesson-trigger grammar or an
incomplete Master chain is a typed blocker rather than a zero-cost guess.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .nia_static_adapter import DEFAULT_MASTER_DIR
from .overview_actions import DANCE_LESSON, REST, VOCAL_LESSON, VISUAL_LESSON
from .produce_outer_local_save import ProduceOuterLocalSaveSnapshot


STATUS_READY = "ready"
STATUS_BLOCKED = "blocked"

_ITEM_ADD_LINE_TYPES = frozenset({"item_add", "custom_item_add"})
_LESSON_ACTION_FIELDS = {
    VOCAL_LESSON: "vocal",
    DANCE_LESSON: "dance",
    VISUAL_LESSON: "visual",
}
_ITEM_EFFECT_PRODUCE_EFFECT = "ProduceItemEffectType_ProduceEffect"
_STAMINA_REDUCE_FIX = "ProduceEffectType_StaminaReduceFix"
_STAMINA_RECOVER_FIX = "ProduceEffectType_StaminaRecoverFix"
_STAMINA_RECOVER_MULTIPLE = "ProduceEffectType_StaminaRecoverMultiple"
_PRODUCE_REWARD_SET = "ProduceEffectType_ProduceRewardSet"
_PRODUCE_CARD_UPGRADE = "ProduceEffectType_ProduceCardUpgrade"
_PRODUCE_CARD_DELETE = "ProduceEffectType_ProduceCardDelete"
_RESOURCE_DRINK = "ProduceResourceType_ProduceDrink"
_TROUBLE_CATEGORY = "ProduceCardCategory_Trouble"


@dataclass(frozen=True, slots=True)
class NiaOuterResourceIssue:
    code: str
    detail: str
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NiaLessonItemAdjustment:
    item_id: str
    acquired_log_index: int
    trigger_id: str
    effect_id: str
    stamina_cost_delta: int
    remaining_uses: int | None


@dataclass(frozen=True, slots=True)
class NiaLessonStaminaProjection:
    status: str
    base_cost: int
    item_cost_delta: int
    effective_cost: int | None
    adjustments: tuple[NiaLessonItemAdjustment, ...]
    issues: tuple[NiaOuterResourceIssue, ...]

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class NiaOutingOptionEffect:
    status: str
    suggestion_id: str
    produce_point_cost: int | None
    stamina_recovery_permille: int | None
    stamina_recovery: int | None
    added_card_ids: tuple[str, ...]
    trouble_card_ids: tuple[str, ...]
    drink_reward_count: int | None
    card_upgrade_count: int | None
    card_delete_count: int | None
    produce_effect_ids: tuple[str, ...]
    unmodeled_effect_ids: tuple[str, ...]
    issues: tuple[NiaOuterResourceIssue, ...]

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class NiaTwoWeekCandidate:
    """One exact next-week action used by the bounded recovery comparison."""

    action: str
    score: int
    stamina_delta: int = 0


@dataclass(frozen=True, slots=True)
class NiaTwoWeekRecoveryContext:
    """Caller-owned complete candidate set; no candidate is inferred here."""

    candidate_set_complete: bool
    current_rest_score: int
    next_candidates: tuple[NiaTwoWeekCandidate, ...]
    next_required_hp: int


@dataclass(frozen=True, slots=True)
class NiaTwoWeekRecoveryComparison:
    status: str
    forced_rest: bool | None
    forced_rest_penalty: int
    current_then_next_score: int | None
    rest_then_non_rest_score: int | None
    preferred_route: str | None
    next_action: str | None
    issues: tuple[NiaOuterResourceIssue, ...]

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@lru_cache(maxsize=32)
def _master_rows(path: Path) -> tuple[Mapping[str, Any], ...]:
    if not path.is_file():
        raise FileNotFoundError(path)
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    value = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    if not isinstance(value, list):
        raise ValueError(f"{path.name} must contain a list")
    return tuple(row for row in value if isinstance(row, Mapping))


@lru_cache(maxsize=32)
def _master_by_id(path: Path) -> Mapping[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in _master_rows(path):
        row_id = row.get("id")
        if isinstance(row_id, str) and row_id:
            if row_id in result:
                raise ValueError(f"duplicate {path.name} id: {row_id}")
            result[row_id] = row
    return result


def _exact_non_negative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(entry, str) and entry for entry in value
    ):
        raise ValueError(f"{label} must be a list of non-empty strings")
    return tuple(value)


def _owned_items(
    snapshot: ProduceOuterLocalSaveSnapshot,
    *,
    before_log_index: int,
) -> tuple[tuple[str, int], ...]:
    acquired: dict[str, int] = {}
    for event in snapshot.inventory_events:
        if event.log_index >= before_log_index:
            continue
        if event.line_type_name not in _ITEM_ADD_LINE_TYPES:
            continue
        if not event.target_id:
            continue
        acquired.setdefault(event.target_id, event.log_index)
    return tuple(sorted(acquired.items(), key=lambda value: (value[1], value[0])))


def _trigger_applies(
    trigger_id: str,
    *,
    lesson_action: str,
    is_sp: bool,
) -> bool | None:
    """Return true/false for exact grammar and None for a relevant unknown."""

    if not trigger_id.startswith("p_trigger-end_lesson"):
        return False
    exact = trigger_id.removeprefix("p_trigger-end_lesson-")
    if exact == "lesson":
        return True
    field = _LESSON_ACTION_FIELDS[lesson_action]
    supported: dict[str, bool] = {
        "lesson_sp": is_sp,
        "lesson_normal": not is_sp,
        "lesson_vocal": field == "vocal",
        "lesson_dance": field == "dance",
        "lesson_visual": field == "visual",
        "lesson_vocal_sp": field == "vocal" and is_sp,
        "lesson_dance_sp": field == "dance" and is_sp,
        "lesson_visual_sp": field == "visual" and is_sp,
    }
    if exact in supported:
        return supported[exact]

    # Attribute/tier prefixes still prove non-applicability.  Once that prefix
    # matches, the remaining threshold grammar must be evaluated by a richer
    # state model and therefore blocks instead of silently becoming zero.
    if exact.startswith("lesson_vocal") and field != "vocal":
        return False
    if exact.startswith("lesson_dance") and field != "dance":
        return False
    if exact.startswith("lesson_visual") and field != "visual":
        return False
    if exact.startswith("lesson_sp") and not is_sp:
        return False
    return None


def _fired_item_uses(
    snapshot: ProduceOuterLocalSaveSnapshot,
    *,
    item_id: str,
    before_log_index: int,
) -> int:
    activations = {
        (line.log_index, line.detail_index)
        for line in snapshot.triggered_effect_lines
        if line.log_index < before_log_index and line.trigger_id == item_id
    }
    return len(activations)


def project_nia_lesson_stamina_cost(
    snapshot: ProduceOuterLocalSaveSnapshot,
    *,
    base_cost: int,
    lesson_action: str,
    is_sp: bool,
    before_log_index: int | None = None,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaLessonStaminaProjection:
    """Add exact owned-item post-lesson stamina effects to a base tile cost."""

    if not isinstance(snapshot, ProduceOuterLocalSaveSnapshot):
        raise TypeError("snapshot must be ProduceOuterLocalSaveSnapshot")
    if isinstance(base_cost, bool) or not isinstance(base_cost, int) or base_cost < 0:
        raise ValueError("base_cost must be a non-negative integer")
    if lesson_action not in _LESSON_ACTION_FIELDS:
        raise ValueError(f"unsupported lesson_action: {lesson_action!r}")
    if type(is_sp) is not bool:
        raise TypeError("is_sp must be boolean")
    boundary = snapshot.log_count if before_log_index is None else before_log_index
    if isinstance(boundary, bool) or not isinstance(boundary, int) or boundary < 0:
        raise ValueError("before_log_index must be a non-negative integer")

    directory = Path(master_dir).resolve()
    try:
        items = _master_by_id(directory / "ProduceItem.yaml")
        item_effects = _master_by_id(directory / "ProduceItemEffect.yaml")
        effects = _master_by_id(directory / "ProduceEffect.yaml")
    except (OSError, ValueError) as error:
        issue = NiaOuterResourceIssue(
            "outer-item-master-unavailable",
            f"outer item Master could not be loaded: {type(error).__name__}: {error}",
        )
        return NiaLessonStaminaProjection(
            STATUS_BLOCKED, base_cost, 0, None, (), (issue,)
        )

    adjustments: list[NiaLessonItemAdjustment] = []
    blockers: list[NiaOuterResourceIssue] = []
    for item_id, acquired_at in _owned_items(snapshot, before_log_index=boundary):
        item = items.get(item_id)
        if item is None:
            blockers.append(
                NiaOuterResourceIssue(
                    "outer-item-master-missing",
                    f"owned item {item_id} has no ProduceItem row",
                    (item_id,),
                )
            )
            continue
        trigger_ids: list[str] = []
        primary = item.get("produceTriggerId")
        if isinstance(primary, str) and primary:
            trigger_ids.append(primary)
        try:
            trigger_ids.extend(
                _string_list(item.get("produceTriggerIds", []), f"{item_id}.produceTriggerIds")
            )
        except ValueError as error:
            blockers.append(
                NiaOuterResourceIssue(
                    "outer-item-trigger-list-invalid", str(error), (item_id,)
                )
            )
            continue
        relevant = tuple(
            (trigger_id, _trigger_applies(
                trigger_id, lesson_action=lesson_action, is_sp=is_sp
            ))
            for trigger_id in trigger_ids
            if trigger_id.startswith("p_trigger-end_lesson")
        )
        if not relevant or all(applies is False for _trigger, applies in relevant):
            continue

        try:
            fire_limit = _exact_non_negative_int(
                item.get("fireLimit"), f"{item_id}.fireLimit"
            )
            fire_interval = _exact_non_negative_int(
                item.get("fireInterval"), f"{item_id}.fireInterval"
            )
        except ValueError as error:
            blockers.append(
                NiaOuterResourceIssue(
                    "outer-item-lifecycle-invalid", str(error), (item_id,)
                )
            )
            continue
        fired = _fired_item_uses(
            snapshot, item_id=item_id, before_log_index=boundary
        )
        remaining = None if fire_limit == 0 else max(0, fire_limit - fired)
        if remaining == 0:
            continue
        if fire_interval != 0:
            blockers.append(
                NiaOuterResourceIssue(
                    "outer-item-fire-interval-unsupported",
                    f"{item_id} has fireInterval={fire_interval}",
                    (item_id,),
                )
            )
            continue
        unknown_triggers = tuple(
            trigger_id for trigger_id, applies in relevant if applies is None
        )
        if unknown_triggers:
            blockers.append(
                NiaOuterResourceIssue(
                    "outer-item-lesson-trigger-unsupported",
                    f"{item_id} has conditional lesson trigger(s) that cannot be "
                    "evaluated from the supplied outer state",
                    (item_id, *unknown_triggers),
                )
            )
            continue
        applicable = tuple(
            trigger_id for trigger_id, applies in relevant if applies is True
        )
        if not applicable:
            continue

        try:
            item_effect_ids = _string_list(
                item.get("produceItemEffectIds"), f"{item_id}.produceItemEffectIds"
            )
        except ValueError as error:
            blockers.append(
                NiaOuterResourceIssue(
                    "outer-item-effect-list-invalid", str(error), (item_id,)
                )
            )
            continue
        for item_effect_id in item_effect_ids:
            item_effect = item_effects.get(item_effect_id)
            if item_effect is None:
                blockers.append(
                    NiaOuterResourceIssue(
                        "outer-item-effect-master-missing",
                        f"{item_id} references missing {item_effect_id}",
                        (item_id, item_effect_id),
                    )
                )
                continue
            effect_type = item_effect.get("effectType")
            if effect_type != _ITEM_EFFECT_PRODUCE_EFFECT:
                blockers.append(
                    NiaOuterResourceIssue(
                        "outer-item-effect-kind-unsupported",
                        f"lesson-triggered {item_id} uses {effect_type!r}",
                        (item_id, item_effect_id),
                    )
                )
                continue
            effect_id = item_effect.get("produceEffectId")
            if not isinstance(effect_id, str) or not effect_id:
                blockers.append(
                    NiaOuterResourceIssue(
                        "outer-item-produce-effect-id-invalid",
                        f"{item_effect_id}.produceEffectId is unavailable",
                        (item_id, item_effect_id),
                    )
                )
                continue
            effect = effects.get(effect_id)
            if effect is None:
                blockers.append(
                    NiaOuterResourceIssue(
                        "outer-produce-effect-master-missing",
                        f"{item_effect_id} references missing {effect_id}",
                        (item_id, item_effect_id, effect_id),
                    )
                )
                continue
            produce_effect_type = effect.get("produceEffectType")
            if produce_effect_type not in {
                _STAMINA_REDUCE_FIX,
                _STAMINA_RECOVER_FIX,
            }:
                # A complete, typed non-stamina ProduceEffect cannot alter the
                # stamina projection and remains outside this helper's scope.
                if not isinstance(produce_effect_type, str) or not produce_effect_type:
                    blockers.append(
                        NiaOuterResourceIssue(
                            "outer-produce-effect-type-invalid",
                            f"{effect_id}.produceEffectType is unavailable",
                            (item_id, effect_id),
                        )
                    )
                continue
            try:
                minimum = _exact_non_negative_int(
                    effect.get("effectValueMin"), f"{effect_id}.effectValueMin"
                )
                maximum = _exact_non_negative_int(
                    effect.get("effectValueMax"), f"{effect_id}.effectValueMax"
                )
            except ValueError as error:
                blockers.append(
                    NiaOuterResourceIssue(
                        "outer-stamina-effect-value-invalid",
                        str(error),
                        (item_id, effect_id),
                    )
                )
                continue
            if minimum != maximum:
                blockers.append(
                    NiaOuterResourceIssue(
                        "outer-stamina-effect-random",
                        f"{effect_id} has range {minimum}..{maximum}",
                        (item_id, effect_id),
                    )
                )
                continue
            delta = minimum if produce_effect_type == _STAMINA_REDUCE_FIX else -minimum
            adjustments.append(
                NiaLessonItemAdjustment(
                    item_id=item_id,
                    acquired_log_index=acquired_at,
                    trigger_id=applicable[0],
                    effect_id=effect_id,
                    stamina_cost_delta=delta,
                    remaining_uses=remaining,
                )
            )

    if blockers:
        return NiaLessonStaminaProjection(
            STATUS_BLOCKED,
            base_cost,
            sum(value.stamina_cost_delta for value in adjustments),
            None,
            tuple(adjustments),
            tuple(blockers),
        )
    item_delta = sum(value.stamina_cost_delta for value in adjustments)
    return NiaLessonStaminaProjection(
        STATUS_READY,
        base_cost,
        item_delta,
        max(0, base_cost + item_delta),
        tuple(adjustments),
        (),
    )


def load_nia_outing_option_effect(
    suggestion_id: str,
    *,
    max_stamina: int,
    master_dir: Path = DEFAULT_MASTER_DIR,
) -> NiaOutingOptionEffect:
    """Compile one exact N.I.A. outing option without valuing its trade-offs."""

    if not isinstance(suggestion_id, str) or not suggestion_id:
        raise ValueError("suggestion_id must be non-empty")
    if (
        isinstance(max_stamina, bool)
        or not isinstance(max_stamina, int)
        or max_stamina <= 0
    ):
        raise ValueError("max_stamina must be a positive integer")
    directory = Path(master_dir).resolve()
    issues: list[NiaOuterResourceIssue] = []
    try:
        suggestions = _master_by_id(directory / "ProduceStepEventSuggestion.yaml")
        effects = _master_by_id(directory / "ProduceEffect.yaml")
        card_rows = _master_rows(directory / "ProduceCard.yaml")
    except (OSError, ValueError) as error:
        issues.append(
            NiaOuterResourceIssue(
                "nia-outing-master-unavailable",
                f"outing Master could not be loaded: {type(error).__name__}: {error}",
            )
        )
        return NiaOutingOptionEffect(
            STATUS_BLOCKED, suggestion_id, None, None, None, (), (), None,
            None, None, (), (), tuple(issues)
        )
    row = suggestions.get(suggestion_id)
    if row is None:
        issues.append(
            NiaOuterResourceIssue(
                "nia-outing-suggestion-missing",
                f"no ProduceStepEventSuggestion row for {suggestion_id}",
                (suggestion_id,),
            )
        )
        return NiaOutingOptionEffect(
            STATUS_BLOCKED, suggestion_id, None, None, None, (), (), None,
            None, None, (), (), tuple(issues)
        )
    if "event-detail-activity-produce_004-" not in suggestion_id:
        issues.append(
            NiaOuterResourceIssue(
                "nia-outing-suggestion-id-invalid",
                f"{suggestion_id} is not an exact N.I.A. Pro outing suggestion",
                (suggestion_id,),
            )
        )

    try:
        point_cost = _exact_non_negative_int(
            row.get("producePoint"), f"{suggestion_id}.producePoint"
        )
        effect_ids = _string_list(
            row.get("produceEffectIds"), f"{suggestion_id}.produceEffectIds"
        )
    except ValueError as error:
        issues.append(
            NiaOuterResourceIssue(
                "nia-outing-suggestion-field-invalid", str(error), (suggestion_id,)
            )
        )
        point_cost = None
        effect_ids = ()

    added_card_ids: tuple[str, ...] = ()
    card_id = row.get("produceCardId")
    if isinstance(card_id, str) and card_id:
        added_card_ids = (card_id,)
    elif not isinstance(card_id, str):
        issues.append(
            NiaOuterResourceIssue(
                "nia-outing-added-card-invalid",
                f"{suggestion_id}.produceCardId must be a string",
                (suggestion_id,),
            )
        )
    trouble_cards: list[str] = []
    for added in added_card_ids:
        matching_cards = tuple(
            card for card in card_rows if card.get("id") == added
        )
        if not matching_cards:
            issues.append(
                NiaOuterResourceIssue(
                    "nia-outing-card-master-missing",
                    f"outing adds unknown card {added}",
                    (suggestion_id, added),
                )
            )
        categories = {
            card.get("category")
            for card in matching_cards
            if isinstance(card.get("category"), str)
        }
        if matching_cards and len(categories) != 1:
            issues.append(
                NiaOuterResourceIssue(
                    "nia-outing-card-category-ambiguous",
                    f"outing card {added} has no single exact category",
                    (suggestion_id, added),
                )
            )
        elif categories == {_TROUBLE_CATEGORY}:
            trouble_cards.append(added)

    recovery_permilles: list[int] = []
    drink_count = 0
    upgrade_count = 0
    delete_count = 0
    unmodeled: list[str] = []
    for effect_id in effect_ids:
        effect = effects.get(effect_id)
        if effect is None:
            issues.append(
                NiaOuterResourceIssue(
                    "nia-outing-effect-master-missing",
                    f"outing references unknown effect {effect_id}",
                    (suggestion_id, effect_id),
                )
            )
            continue
        effect_type = effect.get("produceEffectType")
        try:
            pick_min = _exact_non_negative_int(
                effect.get("pickCountMin"), f"{effect_id}.pickCountMin"
            )
            pick_max = _exact_non_negative_int(
                effect.get("pickCountMax"), f"{effect_id}.pickCountMax"
            )
        except ValueError as error:
            issues.append(
                NiaOuterResourceIssue(
                    "nia-outing-pick-count-invalid", str(error), (effect_id,)
                )
            )
            continue
        if pick_min != pick_max:
            issues.append(
                NiaOuterResourceIssue(
                    "nia-outing-pick-count-random",
                    f"{effect_id} count is {pick_min}..{pick_max}",
                    (effect_id,),
                )
            )
            continue
        if effect_type == _STAMINA_RECOVER_MULTIPLE:
            try:
                minimum = _exact_non_negative_int(
                    effect.get("effectValueMin"), f"{effect_id}.effectValueMin"
                )
                maximum = _exact_non_negative_int(
                    effect.get("effectValueMax"), f"{effect_id}.effectValueMax"
                )
            except ValueError as error:
                issues.append(
                    NiaOuterResourceIssue(
                        "nia-outing-recovery-invalid", str(error), (effect_id,)
                    )
                )
                continue
            if minimum != maximum:
                issues.append(
                    NiaOuterResourceIssue(
                        "nia-outing-recovery-random",
                        f"{effect_id} recovery is {minimum}..{maximum} permille",
                        (effect_id,),
                    )
                )
            else:
                recovery_permilles.append(minimum)
        elif effect_type == _PRODUCE_REWARD_SET and effect.get("produceResourceType") == _RESOURCE_DRINK:
            drink_count += pick_min
        elif effect_type == _PRODUCE_CARD_UPGRADE:
            upgrade_count += pick_min
        elif effect_type == _PRODUCE_CARD_DELETE:
            delete_count += pick_min
        else:
            unmodeled.append(effect_id)

    if len(recovery_permilles) != 1:
        issues.append(
            NiaOuterResourceIssue(
                "nia-outing-recovery-not-exact",
                f"{suggestion_id} resolved {len(recovery_permilles)} recovery effects",
                (suggestion_id, *effect_ids),
            )
        )
    permille = recovery_permilles[0] if len(recovery_permilles) == 1 else None
    status = STATUS_READY if not issues else STATUS_BLOCKED
    return NiaOutingOptionEffect(
        status=status,
        suggestion_id=suggestion_id,
        produce_point_cost=point_cost,
        stamina_recovery_permille=permille,
        stamina_recovery=(
            None if permille is None else max_stamina * permille // 1000
        ),
        added_card_ids=added_card_ids,
        trouble_card_ids=tuple(trouble_cards),
        drink_reward_count=(drink_count if status == STATUS_READY else None),
        card_upgrade_count=(upgrade_count if status == STATUS_READY else None),
        card_delete_count=(delete_count if status == STATUS_READY else None),
        produce_effect_ids=effect_ids,
        unmodeled_effect_ids=tuple(unmodeled),
        issues=tuple(issues),
    )


def compare_nia_two_week_recovery(
    *,
    current_action: str,
    current_action_score: int,
    current_post_hp: int,
    current_rest_post_hp: int,
    context: NiaTwoWeekRecoveryContext,
) -> NiaTwoWeekRecoveryComparison:
    """Compare two recovery placements only with a complete candidate set."""

    integers = {
        "current_action_score": current_action_score,
        "current_post_hp": current_post_hp,
        "current_rest_post_hp": current_rest_post_hp,
        "current_rest_score": context.current_rest_score,
        "next_required_hp": context.next_required_hp,
    }
    for label, value in integers.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{label} must be a non-negative integer")
    if not current_action:
        raise ValueError("current_action must be non-empty")
    if not context.candidate_set_complete:
        issue = NiaOuterResourceIssue(
            "nia-two-week-candidates-incomplete",
            "two-week recovery comparison requires every next-week candidate",
        )
        return NiaTwoWeekRecoveryComparison(
            STATUS_BLOCKED, None, 0, None, None, None, None, (issue,)
        )
    if not context.next_candidates:
        issue = NiaOuterResourceIssue(
            "nia-two-week-candidates-empty",
            "complete next-week candidate set is empty",
        )
        return NiaTwoWeekRecoveryComparison(
            STATUS_BLOCKED, None, 0, None, None, None, None, (issue,)
        )
    if any(
        not candidate.action
        or isinstance(candidate.score, bool)
        or not isinstance(candidate.score, int)
        or candidate.score < 0
        or isinstance(candidate.stamina_delta, bool)
        or not isinstance(candidate.stamina_delta, int)
        for candidate in context.next_candidates
    ):
        issue = NiaOuterResourceIssue(
            "nia-two-week-candidate-invalid",
            "next-week candidates require action, non-negative score and integer stamina_delta",
        )
        return NiaTwoWeekRecoveryComparison(
            STATUS_BLOCKED, None, 0, None, None, None, None, (issue,)
        )

    rest_candidates = tuple(
        value for value in context.next_candidates if value.action == REST
    )
    non_rest = tuple(
        value for value in context.next_candidates if value.action != REST
    )
    if not rest_candidates or not non_rest:
        issue = NiaOuterResourceIssue(
            "nia-two-week-route-pair-missing",
            "comparison requires both Rest and at least one non-Rest next-week candidate",
        )
        return NiaTwoWeekRecoveryComparison(
            STATUS_BLOCKED, None, 0, None, None, None, None, (issue,)
        )
    safe_after_current = tuple(
        value
        for value in non_rest
        if current_post_hp + value.stamina_delta >= context.next_required_hp
    )
    safe_after_rest = tuple(
        value
        for value in non_rest
        if current_rest_post_hp + value.stamina_delta >= context.next_required_hp
    )
    forced_rest = not safe_after_current
    if not safe_after_rest:
        issue = NiaOuterResourceIssue(
            "nia-two-week-rest-route-unsafe",
            "Rest now does not make any complete next-week non-Rest candidate safe",
        )
        return NiaTwoWeekRecoveryComparison(
            STATUS_BLOCKED, forced_rest, 0, None, None, None, None, (issue,)
        )
    best_rest = max(rest_candidates, key=lambda value: (value.score, value.action))
    best_non_rest = max(safe_after_rest, key=lambda value: (value.score, value.action))
    if forced_rest:
        current_total = current_action_score + best_rest.score
        alternative_total = context.current_rest_score + best_non_rest.score
        penalty = max(0, best_non_rest.score - best_rest.score)
    else:
        best_current_non_rest = max(
            safe_after_current, key=lambda value: (value.score, value.action)
        )
        current_total = current_action_score + best_current_non_rest.score
        alternative_total = context.current_rest_score + best_non_rest.score
        penalty = 0
    preferred = (
        "rest-then-non-rest"
        if alternative_total > current_total
        else "current-then-next"
    )
    return NiaTwoWeekRecoveryComparison(
        STATUS_READY,
        forced_rest,
        penalty,
        current_total,
        alternative_total,
        preferred,
        best_non_rest.action,
        (),
    )


__all__ = [
    "NiaLessonItemAdjustment",
    "NiaLessonStaminaProjection",
    "NiaOuterResourceIssue",
    "NiaOutingOptionEffect",
    "NiaTwoWeekCandidate",
    "NiaTwoWeekRecoveryComparison",
    "NiaTwoWeekRecoveryContext",
    "STATUS_BLOCKED",
    "STATUS_READY",
    "compare_nia_two_week_recovery",
    "load_nia_outing_option_effect",
    "project_nia_lesson_stamina_cost",
]
