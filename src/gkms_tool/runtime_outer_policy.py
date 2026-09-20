"""One pure outer decision path over a current native snapshot.

Only state/progress/collections/legal_actions supplied by the DLL are live
authority. Master tables provide rules, never historical week or log state.
Plan is a strategy/card-pool input; it does not select a control loop.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
import sqlite3
import math
import struct
from typing import Any

from .audition_rules import DEFAULT_MASTER_DIR, FINAL, MID1, MID2
from .master_db import DEFAULT_DATABASE, get_idol_profile
from .nia_strategy_journal import NiaStrategyState
from .outer_policy_core import OuterDeckSummary, OuterPolicyCandidate, OuterPolicyRequest, OuterPolicyTargets, OuterPolicyWeights, rank_outer_policy
from .outer_resource_value import effective_stamina_cost, recovery_value, required_pre_refresh_reserve, stamina_value_context
from .runtime_schedule_prior import coarse_schedule_label, score_runtime_schedule_prior


SCHEMA = "gkms.native-outer-policy.v1"
SCHEDULE_PRIOR_WEIGHT = 0.25
# This is rule coverage, not a choice of control loop. More modes can provide
# their own Master-backed profile without multiplying Plan-specific runners.
RANKING_PRODUCE_IDS = ("produce-004", "produce-005")
AUDITION_STRATEGIES = ("stable_clear", "highest_available")
_AXES = ("vocal", "dance", "visual")
_LESSONS = {19: ("vocal", False), 20: ("vocal", True), 21: ("dance", False),
            22: ("dance", True), 23: ("visual", False), 24: ("visual", True)}
_STAGES = {16: MID1, 17: MID2, 18: FINAL}
_BUSINESS_TYPES = {"ProduceStepBusinessType_ProduceCard": 1, "ProduceStepBusinessType_ProduceDrink": 2,
                   "ProduceStepBusinessType_ProducePoint": 3, "ProduceStepBusinessType_Stamina": 4}
_LESSON_TYPE_VALUES = {"ProduceStepLessonType_" + name: value for value, name in enumerate((
    "Unknown", "Lesson", "LessonNormal", "LessonSp", "LessonHard", "LessonVocal", "LessonDance", "LessonVisual",
    "LessonVocalNormal", "LessonVocalSp", "LessonVocalHard", "LessonDanceNormal", "LessonDanceSp",
    "LessonDanceHard", "LessonVisualNormal", "LessonVisualSp", "LessonVisualHard"))}


@dataclass(frozen=True, slots=True)
class NativeOuterDecision:
    target: Mapping[str, object] | None
    metadata: Mapping[str, object]

    @property
    def ready(self) -> bool:
        return self.target is not None


def _integer(value: object, label: str, *, minimum=0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} is not an object")
    return value


@lru_cache(maxsize=24)
def _yaml_rows(path: str, modified: int) -> tuple[Mapping[str, object], ...]:
    import yaml
    values = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    if not isinstance(values, list):
        raise ValueError("Master table must be an array")
    return tuple(values)


def _rows(directory: Path, name: str) -> tuple[Mapping[str, object], ...]:
    path = directory / name
    return _yaml_rows(str(path.resolve()), path.stat().st_mtime_ns)


def _master_row(directory: Path, table: str, identity: str) -> Mapping[str, object]:
    matches = [row for row in _rows(directory, table) if row.get("id") == identity]
    if len(matches) != 1:
        raise ValueError(f"Master {table}/{identity} does not resolve once")
    return matches[0]



def _deck(raw: Mapping[str, Any]) -> tuple[dict[str, object], ...]:
    values = _mapping(raw.get("collections"), "native collections").get("cards")
    if not isinstance(values, list):
        raise ValueError("current native deck collection is unavailable")
    result = []
    identities = set()
    for row in values:
        row = dict(_mapping(row, "native deck card"))
        if row.get("deleted") is True or row.get("isDeleted") is True:
            continue
        card_id = row.get("produceCardId", row.get("card_id"))
        if not isinstance(card_id, str) or not card_id:
            raise ValueError("native deck card has no ID")
        number = row.get("number", row.get("instance_id"))
        if number is not None:
            if number in identities:
                raise ValueError("native deck contains duplicate instance identity")
            identities.add(number)
        _integer(row.get("upgradeCount", row.get("upgrade", 0)), "native card upgrade")
        result.append(row)
    return tuple(result)


def _native_state(raw: Mapping[str, Any], produce_id: str, idol_card_id: str) -> dict[str, object]:
    state = _mapping(raw.get("state"), "native current state")
    if state.get("in_progress") is not True:
        raise ValueError("cultivation is not in progress")
    if state.get("produce_id") != produce_id:
        raise ValueError("native produce ID differs from requested mode")
    progress = _mapping(raw.get("progress"), "native progress")
    if progress.get("idolCardId") != idol_card_id:
        raise ValueError("native idol ID differs from requested idol")
    # NIA's real opening event is week 0 (native progress status 4), before
    # the first scheduled week. Keep that identity instead of inventing week 1.
    week = _integer(state.get("week"), "native week")
    values = {key: _integer(state.get(key), "native " + key) for key in
              ("stamina", "max_stamina", "produce_points", "vocal", "dance", "visual", "vote_count")}
    if values["max_stamina"] < 1 or values["stamina"] > values["max_stamina"]:
        raise ValueError("native stamina bounds are invalid")
    return {**values, "produce_id": produce_id, "idol_card_id": idol_card_id, "week": week,
            "in_progress": True,
            "step_type": state.get("step_type"), "progress_status": state.get("progress_status"),
            "state_source": "native state getters"}


def _context(raw: Mapping[str, Any], produce_id: str, idol_card_id: str, directory: Path, database: Path,
             audition_strategy: str = "highest_available") -> dict[str, object]:
    from .runtime_mode_profile import build_mode_rule_context

    # Preserve the existing native identity diagnostics; mode rules now own
    # schedule/resources/targets instead of duplicating a NIA-only calendar.
    if produce_id in RANKING_PRODUCE_IDS:
        _native_state(raw, produce_id, idol_card_id)
    mode = build_mode_rule_context(raw, produce_id=produce_id, idol_card_id=idol_card_id,
                                  audition_strategy=audition_strategy, master_dir=directory)
    mode["audition_entry_stamina_reserve"] = mode["stamina_reserve"]
    mode["stamina_reserve"] = required_pre_refresh_reserve(mode, mode["stamina_reserve"])
    mode["stamina_valuation"] = stamina_value_context(mode)
    profile = get_idol_profile(idol_card_id, database)
    if profile is None:
        raise ValueError("selected idol is absent from current Master")
    progress = raw["progress"]
    return {**mode, "master_dir": str(directory), "deck": _deck(raw), "deck_source": "native collections.cards",
            "plan_type": profile.plan_type, "exam_effect_type": profile.exam_effect_type,
            "character_id": profile.character_id,
            "vote_count": mode.get("vote_count", 0),
            "drink_count": len(progress.get("produceDrinkIds", [])),
            "drink_capacity": int(progress.get("produceDrinkPossessLimit", mode["settings"]["drink_limit"])),
            "produce_items": tuple(progress.get("produceItems", [])),
            "self_lesson_stamina_modifiers": tuple(progress.get("selfLessonTypeStaminaPermils", [])),
            "mode_profile": {**mode["mode_profile"], "scenario": mode["scenario"], "difficulty": mode["difficulty"],
                             "archetype": profile.plan_type, "ranking_rule_version": SCHEMA}}


def build_runtime_outer_context(snapshot, *, produce_id: str, idol_card_id: str,
                                database: Path = DEFAULT_DATABASE,
                                master_dir: Path = DEFAULT_MASTER_DIR,
                                audition_strategy: str = "highest_available") -> dict[str, object]:
    """Build the one live context shared by outer and deck-mutation policies.

    This does not read game state, input state, a save, or an action history.
    Missing authority or unimplemented mode rules raises ``ValueError`` (or a
    concrete Master read error); callers must report it, not invent targets.
    """
    raw = _mapping(snapshot.raw if hasattr(snapshot, "raw") else snapshot, "native snapshot")
    context = _context(raw, produce_id, idol_card_id, Path(master_dir), Path(database), audition_strategy)
    from .outer_pt_value import build_outer_pt_context
    context["produce_point_spending_context"] = build_outer_pt_context(raw, context)
    return context


def _card_delta(context, after, callback):
    if callback is None:
        from .deck_value import evaluate_deck_delta
        callback = evaluate_deck_delta
    result = callback(context["deck"], tuple(after), context)
    value = result.value if hasattr(result, "value") else result
    return float(value), tuple(getattr(result, "unscored_effects", ()))


def _f32(value):
    return struct.unpack("<f", struct.pack("<f", value))[0]


def _event_suggestion_point_cost(raw, base_cost):
    """Price the current event, never a future Master preview or native quote.

    ScheduleEventUtility.GetSuggestionPointCost (APK 0x7CB2D78) checks the
    current StepType == EventActivity (11), then uses the live progress permil.
    Its single-precision multiply is floored, including for a discount.
    """
    base = _integer(base_cost, "Master event P cost")
    step = raw["state"].get("step_type")
    permil = 0
    cost = base
    if step == 11:
        progress = _mapping(raw.get("progress"), "native progress")
        permil = _integer(progress.get("eventActivityProducePointPermil", 0),
                          "native event activity P permil", minimum=-2147483648)
        if permil > 2147483647:
            raise ValueError("native event activity P permil exceeds int32")
        factor = _f32(1.0 + _f32(_f32(permil) / _f32(1000.0)))
        cost = math.floor(_f32(_f32(base) * factor))
    if not 0 <= cost <= 2147483647:
        raise ValueError("native event activity modifier produces an invalid P cost")
    return cost, {"base_cost": base, "effective_cost": cost, "native_step_type": step,
                  "event_activity_permil": permil, "modifier_applied": step == 11,
                  "source": "Master suggestion + native progress.eventActivityProducePointPermil"
                            if step == 11 else "Master suggestion; current step is not EventActivity"}


def _lesson_stamina(lesson, step, context, directory):
    """Native current modifiers plus separately triggered end-lesson effects."""
    base = _integer(lesson["stamina"], "Master lesson stamina")
    axis, sp = _LESSONS[step]
    modifiers = []
    for entry in context["self_lesson_stamina_modifiers"]:
        entry = _mapping(entry, "native lesson stamina modifier")
        kind = entry.get("lessonType", 0)
        kind = _LESSON_TYPE_VALUES.get(kind, kind)
        kind = _integer(kind, "native lesson modifier type")
        amount = _integer(entry.get("permil", 0), "native stamina modifier permil", minimum=-2147483648)
        # Exact IsValid(lessonType, stepType) APK 0x6808ECC bitmasks.
        # Normal/Sp and their combinations do NOT include self-lesson 19..24.
        if kind not in range(2, 17) or kind == {"vocal": 5, "dance": 6, "visual": 7}[axis]:
            modifiers.append(amount)
    permille = sum(modifiers)
    # GetAffectedConsumptionStamina 0x7A8CE6C uses single precision + floor.
    affected = math.floor(_f32(_f32(base) * _f32(1.0 + _f32(_f32(permille) / _f32(1000.0)))))
    if affected < 0:
        raise ValueError("native lesson stamina modifiers produce a negative cost")
    extra, recovery, adjustments, unscored = 0, 0, [], []
    for owned in context["produce_items"]:
        owned = _mapping(owned, "native current ProduceItem")
        identity = owned.get("produceItemId")
        try:
            item = _master_row(directory, "ProduceItem.yaml", str(identity))
        except ValueError:
            unscored.append(f"missing-owned-item:{identity}")
            continue
        fired = _integer(owned.get("fireCount", 0), "native item fireCount")
        limit = _integer(item["fireLimit"], "Master item fireLimit")
        if limit and fired >= limit:
            continue
        global_triggers = tuple(t for t in (item.get("produceTriggerId"), *item.get("produceTriggerIds", [])) if t)
        skills = item.get("skills") or [{"produceItemEffectId": e, "produceTriggerId": ""}
                                        for e in item["produceItemEffectIds"]]
        for skill in skills:
            triggers = (skill["produceTriggerId"],) if skill.get("produceTriggerId") else global_triggers
            relevant, applicability = [], []
            for trigger_id in triggers:
                trigger = _master_row(directory, "ProduceTrigger.yaml", trigger_id)
                if trigger["phaseType"] not in {"ProducePhaseType_EndLesson", "ProducePhaseType_EndLessonBeforePresent"}:
                    continue
                relevant.append(trigger_id)
                applicability.append({"p_trigger-end_lesson-lesson": True,
                                      "p_trigger-end_lesson-lesson_sp": sp}.get(trigger_id))
            if not relevant or all(value is False for value in applicability):
                continue
            effect_link = _master_row(directory, "ProduceItemEffect.yaml", skill["produceItemEffectId"])
            if effect_link["effectType"] != "ProduceItemEffectType_ProduceEffect":
                continue
            effect = _master_row(directory, "ProduceEffect.yaml", effect_link["produceEffectId"])
            kind = effect["produceEffectType"]
            if kind not in {"ProduceEffectType_StaminaReduceFix", "ProduceEffectType_StaminaRecoverFix"}:
                unscored.append(f"deferred-lesson-item-effect:{identity}:{effect['id']}")
                continue
            certain = all(value is True for value in applicability) and item["fireInterval"] == 0
            minimum = _integer(effect["effectValueMin"], "Master item effect minimum")
            maximum = _integer(effect["effectValueMax"], "Master item effect maximum")
            if maximum < minimum:
                raise ValueError("Master item stamina effect range is invalid")
            # Unknown recovery conditions never reduce an affordability cost.
            # A conditional cost is conservatively reserved at its maximum,
            # explicitly labelled; no trigger-name threshold parsing is used.
            amount = maximum if kind == "ProduceEffectType_StaminaReduceFix" else minimum if certain else 0
            if not certain or minimum != maximum:
                unscored.append(f"bounded-lesson-item-effect:{identity}:{effect['id']}")
            if kind == "ProduceEffectType_StaminaReduceFix":
                extra += amount
            else:
                recovery += amount
            adjustments.append({"item_id": identity, "effect_id": effect["id"], "trigger_ids": relevant,
                                "fire_count": fired, "fire_limit": limit, "amount": amount,
                                "effect_type": kind, "exact": certain and minimum == maximum})
    return affected + extra, recovery, {"base_stamina_cost": base, "affected_base_stamina_cost": affected,
        "native_stamina_permille": permille, "end_lesson_stamina_cost": extra,
        "end_lesson_stamina_recovery": recovery, "item_adjustments": adjustments,
        "unscored_effects": sorted(set(unscored)),
        "stamina_source": "native progress modifiers/fireCount + Master item chain; APK GetAffectedConsumptionStamina"}


def _effects(connection, ids, context, callback) -> tuple[float, list[str]]:
    """Known Master lower-bound utility; never invent an unrevealed card."""
    total, unscored = 0.0, []
    for identity in ids:
        row = connection.execute("SELECT effect_type,effect_value_min,effect_value_max,resource_type,pick_count_min,pick_count_max,raw_json FROM produce_effect WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("Master effect is missing: " + str(identity))
        kind, value, maximum, resource, count, count_max, raw_json = row
        if kind in {"ProduceEffectType_VocalAddition", "ProduceEffectType_DanceAddition", "ProduceEffectType_VisualAddition"}:
            axis = {"ProduceEffectType_VocalAddition": "vocal", "ProduceEffectType_DanceAddition": "dance", "ProduceEffectType_VisualAddition": "visual"}[kind]
            effective = int(value) * (1000 + context["growth"][axis]) // 1000
            total += 10 * min(max(0, context["attribute_cap"] - context[axis]), effective)
        elif kind == "ProduceEffectType_StaminaRecoverFix":
            total += 30 * recovery_value(context, int(value))
        elif kind == "ProduceEffectType_StaminaRecoverMultiple":
            # Outer ProduceEffect scaling has not been verified from its
            # executor. ExamStaminaRecoverMultiple is a different executor;
            # neither its divisor nor a digit pattern in the ID is evidence.
            unscored.append(f"unverified-stamina-multiple:{identity}")
        elif kind in {"ProduceEffectType_ProducePointAddition", "ProduceEffectType_ProducePointAdditionDisableTrigger"}:
            total += int(value) * 5
        elif kind == "ProduceEffectType_VoteCountAddition":
            total += int(value) * .1
        elif kind == "ProduceEffectType_ProduceRewardSet":
            # Full drink inventory is now a normal kept-set comparison. It
            # can retain existing drinks; capacity alone is not a loss.
            unscored.append(f"unrevealed-reward:{identity}")
        else:
            unscored.append(str(identity))
    return total, unscored


def _choice_utility(connection, choice, context, callback):
    cost_p, cost_hp = choice.produce_point_cost, choice.stamina_cost
    if cost_p > context["produce_points"]:
        return None
    after_cost = {**context, "stamina": max(0, context["stamina"] - cost_hp),
                  "produce_points": context["produce_points"] - cost_p}
    value, missing = _effects(connection, choice.effect_ids, after_cost, callback)
    value -= cost_p * 5 + effective_stamina_cost(context, cost_hp) * 30
    if choice.direct_card_id:
        after = [*context["deck"], {"instance_id": "proposed:" + choice.suggestion_id,
                 "produceCardId": choice.direct_card_id, "upgradeCount": choice.direct_card_upgrade}]
        delta, gaps = _card_delta(after_cost, after, callback)
        value += delta
        missing.extend(gaps)
    if choice.success_effect_ids or choice.fail_effect_ids:
        success, a = _effects(connection, choice.success_effect_ids, after_cost, callback)
        failure, b = _effects(connection, choice.fail_effect_ids, after_cost, callback)
        probability = 1.0 if choice.always_successful else choice.success_probability_permyriad / 10000
        value += success * probability + failure * (1 - probability)
        missing.extend((*a, *b))
    return value, missing


def _detail(connection, detail_id):
    from .school_event import _load_detail_candidate
    row = connection.execute("SELECT id,suggestion_type,suggestion_ids_json,produce_effect_ids_json,support_card_id,event_type,event_character_type,is_business_excellent FROM step_event_detail WHERE id=?", (detail_id,)).fetchone()
    if row is None:
        raise ValueError("Master event detail missing: " + str(detail_id))
    return _load_detail_candidate(connection, row)


def _business_resource_preference(assessment, context):
    """An explicit inventory preference, never a hidden drink/card score.

    When HP already covers the current mode reserve, acquire lasting resources
    before topping it up. Empty drink slots are an opportunity to stock up;
    actual revealed drinks are still compared by the specialized selector.
    Missing inventory/reserve evidence leaves ordinary utility in control.
    """
    reserve = context.get("stamina_reserve")
    hp = assessment.modeled_stamina_min
    if (type(reserve) is not int or type(hp) not in (int, float)
            or reserve < 0 or hp < reserve):
        return 0
    picks = assessment.reward_pick_min or {}
    count, capacity = context.get("drink_count"), context.get("drink_capacity")
    if (picks.get("ProduceResourceType_ProduceDrink", 0) > 0
            and type(count) is int and type(capacity) is int and 0 <= count < capacity):
        return 2
    if (picks.get("ProduceResourceType_ProduceCard", 0) > 0
            or (assessment.modeled_point_balance_min is not None
                and assessment.modeled_point_balance_min > context["produce_points"])):
        return 1
    return 0


def _business_value(connection, business, context, callback, *, database=DEFAULT_DATABASE):
    from types import SimpleNamespace
    from .outer_event_bundle import combine_exclusive_branches, evaluate_event_bundle
    from .outer_training_budget import budget_priority

    # APK CheckIfGoBusiness 0x78D6BA4 compares both fields against current P/HP.
    cost_p = _integer(business.get("producePoint", 0), "business P cost")
    cost_hp = _integer(business.get("stamina", 0), "business stamina cost")
    if cost_p > context["produce_points"] or cost_hp > context["stamina"]:
        return None
    probability = (min(1000, max(0, _integer(business["excellentPermil"], "native business excellent permille",
        minimum=-2147483648))) / 1000 if "excellentPermil" in business else None)
    branches, assessments, probabilities = [], [], []
    for branch_index, key in enumerate(("produceStepEventDetailId", "excellentProduceStepEventDetailId")):
        weight = ((probability if branch_index else 1 - probability) if probability is not None else None)
        if weight == 0:
            continue  # An impossible branch cannot block the available one.
        detail_id = business.get(key)
        if not isinstance(detail_id, str) or not detail_id:
            raise ValueError("business has no normal/excellent event detail")
        detail = _detail(connection, detail_id)
        # No suggestion means a common-effects-only branch. This is an empty
        # value input, never a fabricated native candidate or submitted action.
        empty_choice = SimpleNamespace(produce_point_cost=0, stamina_cost=0, effect_ids=(),
            direct_card_id="", direct_card_upgrade=0, always_successful=False,
            success_probability_permyriad=0, success_effect_ids=(), fail_effect_ids=())
        choices = detail.choices or (empty_choice,)
        options = [score for choice in choices if (score := evaluate_event_bundle(connection, choice, context,
            database=database, card_value=callback, prefix_effect_ids=detail.detail_effect_ids,
            entry_point_cost=cost_p, entry_stamina_cost=cost_hp)) is not None]
        if detail.choices and not options:
            return None
        if not options:
            return None
        best = max(options, key=lambda row: (
            budget_priority(context.get("training_budget"), row.modeled_point_balance_min),
            row.future_choice_ranking_key))
        branches.append(best)
        probabilities.append(weight)
        assessments.append({"detail_id": detail_id, "best_future_choice": best.to_dict(),
                            "evaluated_choice_count": len(options)})
    if probability is not None:
        combined = combine_exclusive_branches(branches, probabilities)
        branch_policy = "expected utility from native excellentPermil; one future choice"
    else:
        combined = combine_exclusive_branches(branches)
        branch_policy = "lower bound across normal/excellent; probability not observed; one future choice"
    return combined.value, {
        "produce_point_cost": cost_p, "stamina_cost": cost_hp,
        "normal_detail_id": business.get("produceStepEventDetailId"),
        "excellent_detail_id": business.get("excellentProduceStepEventDetailId"),
        "unscored_effects": list(combined.unscored_effects), "branch_policy": branch_policy,
        "ranking_key": list(combined.ranking_key), "plan_progress": combined.plan_progress,
        "budget_priority": budget_priority(context.get("training_budget"), combined.modeled_point_balance_min),
        "modeled_point_balance_min": combined.modeled_point_balance_min,
        "modeled_point_balance_max": combined.modeled_point_balance_max,
        "resource_preference": _business_resource_preference(combined, context),
        "resource_preference_scope": "minimum-Master-reward-picks-and-resulting-HP; no-unrevealed-reward-score",
        "modeled_stamina_min": combined.modeled_stamina_min,
        "reward_pick_min": dict(combined.reward_pick_min or {}),
        "value_kind": "event-ranking-utility", "costs_charged_once": True, "bundle_branches": assessments}


def _audition_decision(raw, actions, metadata, produce_id, audition_strategy):
    """Choose among actual enabled Master rows; enter only their selected row."""
    if produce_id not in RANKING_PRODUCE_IDS:
        raise ValueError(f"unsupported audition mode rules: {produce_id}")
    ui = _mapping(raw.get("ui_state"), "native audition UI state")
    candidates = ui.get("candidates")
    if not isinstance(candidates, list) or ui.get("input_active") is not True:
        raise ValueError("native audition candidate input is not ready")
    stage = ui.get("step_type")
    if stage not in _STAGES:
        raise ValueError("native audition stage is unsupported")
    selected_index = _integer(ui.get("selected_index"), "native audition selected index", minimum=-1)
    offered, identities = [], set()
    for action in actions:
        name = action["action_id"]
        if name not in {"audition.choose", "audition.enter"}:
            continue
        target = action["target"]
        index = _integer(target.get("index"), "native audition index")
        if index >= len(candidates):
            raise ValueError("native audition index is outside current candidate list")
        candidate = _mapping(candidates[index], "native audition candidate")
        identity = _mapping(candidate.get("identity"), "native audition identity")
        if candidate.get("index") != index or candidate.get("enabled") is not True or candidate.get("dearness_locked") is not False:
            raise ValueError("native audition legal target is not an enabled current candidate")
        for key in ("difficulty_id", "produce_id", "number", "step_type", "audition_type"):
            if key not in identity or identity[key] != target.get(key):
                raise ValueError("native audition composite identity differs from current target")
        if identity["produce_id"] != produce_id or identity["step_type"] != stage:
            raise ValueError("native audition candidate belongs to another mode or stage")
        number = _integer(identity["number"], "native audition number", minimum=1)
        signature = tuple(identity[key] for key in ("difficulty_id", "produce_id", "number", "step_type", "audition_type"))
        if not identity["difficulty_id"] or signature in identities:
            raise ValueError("native audition candidate identity is empty or repeated")
        identities.add(signature)
        required = _integer(candidate.get("required_vote_count"), "native audition vote requirement")
        if required > metadata["context"]["vote_count"]:
            raise ValueError("native audition candidate exceeds current votes")
        selected = index == selected_index
        if candidate.get("selected") is not selected or (name == "audition.enter") is not selected:
            raise ValueError("native audition enter target is not the currently selected identity")
        offered.append((number, action, candidate))
    if not offered:
        raise ValueError("native audition has no current legal difficulty")
    stable_clear = audition_strategy == "stable_clear"
    chosen_number = (min if stable_clear else max)(value[0] for value in offered)
    matches = [value for value in offered if value[0] == chosen_number]
    if len(matches) != 1:
        raise ValueError("chosen available native audition tier is ambiguous")
    _, action, candidate = matches[0]
    tier_label = "最低" if stable_clear else "最高"
    return NativeOuterDecision(dict(action["target"]), {**metadata, "status": "ready",
        "reason": f"確認進入目前已選的{tier_label}可用演出檔位" if action["action_id"] == "audition.enter" else f"選擇目前遊戲已解鎖且票數足夠的{tier_label}演出檔位",
        "comparison_scope": ("lowest" if stable_clear else "highest") + "-native-available-difficulty; not a win-probability prediction",
        "master_source": "native CandidateAuditionList Master row",
        "chosen_difficulty": candidate.get("difficulty"), "chosen_number": chosen_number,
        "required_vote_count": candidate["required_vote_count"]})


def _retry_failed_tier(raw, ui, context):
    """CurrentSchedule tracks retries; progress.stepSelectNumber can stay old."""
    stage, week = context["step_type"], context["week"]
    if stage not in _STAGES:
        raise ValueError("native retry is not bound to an audition stage")
    supplied = any(key in ui for key in ("failed_number", "failed_step_type", "failed_step_number"))
    failed = None
    if supplied:
        if ui.get("failed_step_type") != stage or ui.get("failed_step_number") != week:
            raise ValueError("native retry failure context differs from current stage/week")
        failed = _integer(ui.get("failed_number"), "native failed audition number", minimum=1)
    schedules = _mapping(raw.get("collections", {}), "native collections").get("schedule", [])
    current = [row for row in schedules if isinstance(row, Mapping) and row.get("stepNumber") == week]
    if len(current) > 1:
        raise ValueError("native retry current schedule is ambiguous")
    if current:
        row = current[0]
        if row.get("selectedStepType") not in (stage, _STAGES[stage]):
            raise ValueError("native retry current schedule stage differs")
        number = _integer(row.get("stepSelectNumber"), "native current schedule audition number", minimum=1)
        if failed is not None and failed != number:
            raise ValueError("native retry failed number differs from current schedule")
        failed = number
    if failed is None:
        raise ValueError("native retry has no verified failed audition number")
    return failed, "native CurrentSchedule / failure context"


def _audition_retry_decision(raw, actions, metadata, produce_id, audition_strategy):
    if produce_id not in RANKING_PRODUCE_IDS:
        raise ValueError(f"unsupported audition retry mode rules: {produce_id}")
    ui = _mapping(raw.get("ui_state"), "native retry UI state")
    free = _integer(ui.get("free_count"), "native free retry count")
    remaining = _integer(ui.get("remaining_count"), "native retry remaining count", minimum=1)
    if ui.get("data_ready") is not True:
        raise ValueError("native retry quota is not confirmed")
    use_ticket = free == 0
    price = 1 if use_ticket else 0
    if not use_ticket:
        if ui.get("free_retry_available") is not True or type(ui.get("price")) is not int or ui["price"] != 0:
            raise ValueError("native free retry quota/price is not confirmed")
    elif (ui.get("free_retry_available") is not False or ui.get("ticket_retry_available") is not True
          or ui.get("ticket_item_id") != "item-produce_continue-1"
          or type(ui.get("ticket_balance")) is not int or ui["ticket_balance"] < 1
          or type(ui.get("ticket_cost")) is not int or ui["ticket_cost"] != 1
          or type(ui.get("price")) is not int or ui["price"] != 1):
        raise ValueError("免費重試已用完，未核對到可用的單張再挑戰券；停止重試")
    confirm_action = "audition.retry_confirm_ticket" if use_ticket else "audition.retry_confirm_free"
    resource_label = "1 張再挑戰券" if use_ticket else "免費額度"
    failed, failure_source = _retry_failed_tier(raw, ui, metadata["context"])
    detail = {**metadata, "failed_number": failed, "failed_number_source": failure_source,
              "entry_number_diagnostic": raw["progress"].get("stepSelectNumber"),
              "free_count": free, "remaining_count": remaining, "price": price,
              "retry_resource": "continue_ticket" if use_ticket else "free",
              "comparison_scope": "one-lower-available-tier-after-failure; " + ("single-ticket-cap-1" if use_ticket else "free-retry-first")}
    if use_ticket:
        detail.update(ticket_item_id=ui["ticket_item_id"], ticket_balance=ui["ticket_balance"], max_cost=1)
    if failed <= 1:
        return NativeOuterDecision(None, {**detail, "reason": "目前已是最低演出檔位，沒有更低檔可重試；保留培育並停止操作"})
    screen = raw.get("screen_type")
    allowed = {"audition.retry_open", "audition.retry_select", confirm_action}
    for action in actions:
        if action["action_id"] not in allowed:
            continue
        target = action["target"]
        if (target.get("sheet_type") != screen or target.get("parent_instance_id") != ui.get("parent_instance_id")
                or type(target.get("free_count")) is not int or target["free_count"] != free
                or type(target.get("remaining_count")) is not int or target["remaining_count"] != remaining
                or type(target.get("price")) is not int or target["price"] != price
                or any(not isinstance(target.get(key), str) or not target[key]
                       for key in ("sheet_instance_id", "parent_instance_id", "button_instance_id"))):
            raise ValueError("native retry target differs from current sheet/quota/price")
        if action["action_id"] == "audition.retry_confirm_ticket" and (
                target.get("item_id") != "item-produce_continue-1" or target.get("item_id") != ui["ticket_item_id"]
                or type(target.get("item_balance")) is not int or target["item_balance"] != ui["ticket_balance"]
                or type(target.get("max_cost")) is not int or target["max_cost"] != 1):
            raise ValueError("native single-ticket retry target differs from current item/balance/cap")

    def select_action(action_id, reason, **binding):
        matches = [a for a in actions if a["action_id"] == action_id and
                   all(a["target"].get(key) == value for key, value in binding.items())]
        if len(matches) > 1:
            raise ValueError("native retry callback is ambiguous")
        if not matches:
            return NativeOuterDecision(None, {**detail, "status": "waiting", "reason": "等待降檔重試的原生操作就緒"})
        return NativeOuterDecision(dict(matches[0]["target"]), {**detail, "status": "ready", "reason": reason})

    if screen == "ProduceAuditionContinueWarningSheetPresenter":
        return select_action("audition.retry_open", f"已核對{resource_label}，開啟重試並選擇較低的演出檔位")
    if screen != "ProduceAuditionContinueSelectConfirmSheetPresenter":
        raise ValueError("重試未提供可核對的降檔選擇；不確認原檔重試")
    candidates = ui.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("native retry difficulty list is unavailable")
    selected_number = _integer(ui.get("selected_number"), "native retry selected number", minimum=1)
    lower, numbers = [], set()
    for index, row in enumerate(candidates):
        row = _mapping(row, "native retry candidate")
        identity = _mapping(row.get("identity"), "native retry difficulty identity")
        number = _integer(identity.get("number"), "native retry candidate number", minimum=1)
        if (identity.get("index") != index or identity.get("produce_id") != produce_id
                or identity.get("step_type") != metadata["context"]["step_type"]
                or not isinstance(identity.get("difficulty_id"), str) or not identity["difficulty_id"]
                or type(row.get("enabled")) is not bool or row.get("selected") is not (number == selected_number)
                or number in numbers):
            raise ValueError("native retry candidate identity/selection is inconsistent")
        numbers.add(number)
        if number < failed and (row["enabled"] or row["selected"]):
            lower.append(identity)
    if not lower:
        return NativeOuterDecision(None, {**detail, "reason": "目前沒有實際可選的較低演出檔位；保留培育並停止操作"})
    desired = max(lower, key=lambda identity: identity["number"])
    detail["chosen_number"] = desired["number"]
    if selected_number == desired["number"]:
        return select_action(confirm_action, f"已核對第 {desired['number']} 檔與{resource_label}，確認降檔重試",
                             selected_number=desired["number"])
    return select_action("audition.retry_select", f"失敗後從第 {failed} 檔降至第 {desired['number']} 檔", **dict(desired))


def _business_folder_decision(connection, ui, actions, context, card_value, metadata, *, database=DEFAULT_DATABASE):
    """Rank all current offers before binding select versus start callbacks."""
    folders = ui.get("folders")
    if not isinstance(folders, list):
        raise ValueError("native business folders are not an array")
    selected_index = _integer(ui.get("selected_index"), "native business selected index", minimum=-1)
    evaluations, indices = [], set()
    for folder in folders:
        folder = _mapping(folder, "native business folder")
        index = _integer(folder.get("index"), "native business index")
        if index in indices:
            raise ValueError("native business folder index is duplicated")
        indices.add(index)
        selected = index == selected_index
        if folder.get("selected") is not selected or type(folder.get("can_go")) is not bool:
            raise ValueError("native business selection/affordability flags are inconsistent")
        business = _mapping(folder.get("business"), "native business offer")
        kind = _BUSINESS_TYPES.get(business.get("businessType"), business.get("businessType"))
        number = _integer(business.get("number", 0), "native business number")
        if kind not in (1, 2, 3, 4) or folder.get("business_type") != kind or folder.get("business_number") != number:
            raise ValueError("native business folder does not bind its current offer")
        if not folder["can_go"]:
            continue
        score = _business_value(connection, business, context, card_value, database=database)
        if score is not None:
            evaluations.append({"index": index, "business_type": kind, "business_number": number,
                                "selected": selected, "score": score[0], **score[1]})
    if not evaluations:
        if ui.get("data_ready") is False or ui.get("selection_visible") is False:
            return NativeOuterDecision(None, {**metadata, "status": "waiting", "reason": "等待營業選項載入"})
        return NativeOuterDecision(None, {**metadata, "reason": "目前沒有可負擔且可評估的營業選項"})
    # Keep a selected offer on utility ties. Button visibility is not value:
    # the best selected folder stays ranked while its start animation settles.
    best = max(evaluations, key=lambda row: (row["budget_priority"], row["resource_preference"],
                                            row["ranking_key"], row["selected"], -row["index"]))
    folder = next(row for row in folders if row["index"] == best["index"])
    action_id = "business.start" if best["selected"] else "business.choose"
    matches = [action for action in actions if action.get("action_id") == action_id and
               all(action["target"].get(key) == best[key] for key in ("index", "business_type", "business_number"))]
    detail = {**metadata, "evaluations": evaluations, "chosen_offer": best,
              "comparison_scope": "all-native-business-offers-before-callback-binding"}
    if len(matches) > 1:
        raise ValueError("best native business callback is ambiguous")
    if not matches or (best["selected"] and folder.get("start_ready") is not True):
        return NativeOuterDecision(None, {**detail, "status": "waiting", "reason":
            "已選定最佳營業，等待開始按鈕就緒" if best["selected"] else "等待最佳營業項目的選擇介面就緒"})
    return NativeOuterDecision(dict(matches[0]["target"]), {**detail, "status": "ready", "reason":
        "開始已選定的最佳營業" if best["selected"] else "預覽目前資源與後續收益最佳的營業"})


def _schedule_behavior_prior(context, actions, bindings, schedule_week, provider_path=None):
    """Attach one weak preference to the complete current core comparison.

    A selected weekly BC provider may replace the count preference in its
    declared coverage. Productive/detail choices without core projections abstain;
    omitting them would change the candidate signature seen by the prior.
    """
    labels = {f"native:{ordinal}": coarse_schedule_label(action["target"])
              for ordinal, action in enumerate(actions)}
    origin = {"source_kind": "hierarchical-behavior-counts", "model_artifact_used": False,
              "core_mixture_weight": SCHEDULE_PRIOR_WEIGHT, "applied_to_core": False}
    if set(labels) != set(bindings):
        return {}, {**origin, "status": "abstain", "reason": "core-does-not-project-full-native-schedule-set",
                    "candidate_labels": labels, "native_candidate_count": len(labels),
                    "core_candidate_count": len(bindings), "schedule_number": schedule_week}
    bc_diagnostic = None
    if provider_path is not None:
        from .runtime_outer_behavior_prior import score_runtime_outer_behavior_prior

        bc = score_runtime_outer_behavior_prior({**context, "surface": "schedule"},
            {f"native:{ordinal}": action["target"] for ordinal, action in enumerate(actions)},
            schedule_number=schedule_week, candidate_set_complete=True, provider_path=provider_path)
        bc_diagnostic = bc.to_dict()
        if bc.available:
            return dict(bc.scores), {**bc_diagnostic, "source_kind": "candidate-conditioned-weekly-bc",
                "model_artifact_used": True, "core_mixture_weight": SCHEDULE_PRIOR_WEIGHT, "applied_to_core": True}
    result = score_runtime_schedule_prior({**context, "surface": "schedule"}, labels,
                                         schedule_number=schedule_week, candidate_set_complete=True)
    scores = dict(result.scores) if result.available else {}
    return scores, {**result.to_dict(), **origin, "applied_to_core": bool(scores),
                    **({"requested_bc": bc_diagnostic} if bc_diagnostic is not None else {})}


def choose(snapshot, *, produce_id: str, idol_card_id: str, database: Path = DEFAULT_DATABASE,
           master_dir: Path = DEFAULT_MASTER_DIR, card_value: Callable | None = None,
           audition_strategy: str = "highest_available", context_transform: Callable | None = None,
           weekly_bc_provider_path: Path | str | None = None) -> NativeOuterDecision:
    """Return an exact current target, or an explicit abstention; never wait/input."""
    metadata: dict[str, object] = {"schema": SCHEMA, "source": "native-outer-policy", "status": "abstained"}
    try:
        if audition_strategy not in AUDITION_STRATEGIES:
            raise ValueError(f"unsupported audition strategy: {audition_strategy}")
        metadata["audition_strategy"] = audition_strategy
        raw = _mapping(snapshot.raw if hasattr(snapshot, "raw") else snapshot, "native snapshot")
        if raw.get("busy") is not False or raw.get("actions_complete") is not True:
            raise ValueError("native action boundary is not ready")
        actions = raw.get("legal_actions")
        business_folders = raw.get("surface") == "business" and isinstance(raw.get("ui_state"), Mapping) and "folders" in raw["ui_state"]
        retry_surface = raw.get("surface") == "audition_retry"
        if not isinstance(actions, list) or (not actions and not business_folders and not retry_surface):
            raise ValueError("native legal action set is empty")
        for action in actions:
            action = _mapping(action, "native action")
            target = _mapping(action.get("target"), "native action target")
            if target.get("action_id") != action.get("action_id"):
                raise ValueError("native target action identity does not match action")
        if produce_id not in RANKING_PRODUCE_IDS:
            raise ValueError(f"unsupported mode rules: {produce_id}; native decision integration remains pending")
        metadata["context"] = _native_state(raw, produce_id, idol_card_id)
        metadata["ranking_produce_ids"] = RANKING_PRODUCE_IDS
        surface = raw.get("surface")
        if surface == "audition_retry":
            return _audition_retry_decision(raw, actions, metadata, produce_id, audition_strategy)
        if surface == "audition_select":
            return _audition_decision(raw, actions, metadata, produce_id, audition_strategy)
        # No comparison is needed for the sole legal choice in a known family.
        # Do not make its execution depend on absent optional reward/target data.
        # Event buttons are native-filtered by IsValid/!IsDisabled. The game's
        # GetSuggestionAdditionalInfo compares the actual modified P cost to
        # the wallet (APK 0x7CB2B80..0x7CB2BC8), also for this sole-choice path.
        sole_family = {"schedule": "schedule.choose", "event": "event.choose", "business": "business.choose"}
        sole_rest = (surface == "schedule" and len(actions) == 1
                     and actions[0].get("action_id") == "ui.navigation"
                     and actions[0]["target"].get("button_id") == "schedule.refresh")
        if len(actions) == 1 and not business_folders and (actions[0].get("action_id") == sole_family.get(surface) or sole_rest):
            if surface == "business":
                business = _mapping(actions[0].get("evidence", {}).get("business"), "native business offer")
                cost_p = _integer(business.get("producePoint", 0), "business P cost")
                cost_hp = _integer(business.get("stamina", 0), "business stamina cost")
                if cost_p > metadata["context"]["produce_points"] or cost_hp > metadata["context"]["stamina"]:
                    raise ValueError("sole native business offer exceeds current resources")
            return NativeOuterDecision(dict(actions[0]["target"]), {**metadata, "status": "ready",
                "reason": "目前原生合法候選只有一項", "comparison_scope": "sole-native-legal-action"})
        context = build_runtime_outer_context(raw, produce_id=produce_id, idol_card_id=idol_card_id,
                                             master_dir=Path(master_dir), database=Path(database),
                                             audition_strategy=audition_strategy)
        if context_transform is not None:
            # Caller binds an optional run-owned deck reference. The actual
            # context still comes from this snapshot and the same mode rules.
            context = dict(context_transform(context))
        from .outer_training_budget import build_training_budget, budget_priority
        context["training_budget"] = build_training_budget(raw, context, database=database, master_dir=master_dir)
        if card_value is None:
            def card_value(before, after, card_context):
                from .deck_value import evaluate_deck_delta
                return evaluate_deck_delta(before, after, card_context, database=Path(database))
        metadata["context"] = context
        metadata["deck_count"] = len(context["deck"])
        evaluations = []

        def selected(action, reason, **extra):
            return NativeOuterDecision(dict(action["target"]), {**metadata, "status": "ready", "reason": reason,
                "evaluations": evaluations, **extra})

        ui = _mapping(raw.get("ui_state", {}), "native UI state")
        with closing(sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            if surface == "event":
                detail_id = ui.get("event_detail_id")
                detail = _detail(connection, detail_id)
                by_id = {choice.suggestion_id: choice for choice in detail.choices}
                for action in actions:
                    if action.get("action_id") != "event.choose":
                        continue
                    target = action["target"]
                    if target.get("event_detail_id") not in (None, detail_id) or target.get("suggestion_id") not in by_id:
                        raise ValueError("native event suggestion does not belong to its Master detail")
                    choice = by_id[target["suggestion_id"]]
                    point_cost, point_cost_assessment = _event_suggestion_point_cost(raw, choice.produce_point_cost)
                    if point_cost != choice.produce_point_cost:
                        choice = replace(choice, produce_point_cost=point_cost)
                    from .outer_event_bundle import evaluate_event_bundle

                    score = evaluate_event_bundle(connection, choice, context, database=Path(database), card_value=card_value)
                    if score is not None:
                        evaluations.append({"target": dict(target), "score": score.value, "ranking_key": list(score.ranking_key),
                            "point_cost_assessment": point_cost_assessment,
                            "budget_priority": budget_priority(context.get("training_budget"), score.modeled_point_balance_min),
                            "unscored_effects": list(score.unscored_effects), "bundle_assessment": score.to_dict()})
                if evaluations:
                    best = max(evaluations, key=lambda row: (row["budget_priority"], row["ranking_key"]))
                    return selected(next(action for action in actions if action["target"] == best["target"]), "目前資源與牌庫的 Master 事件選擇", effect_source=f"Master event detail:{detail_id}")
            elif surface == "business":
                if business_folders:
                    return _business_folder_decision(connection, ui, actions, context, card_value, metadata, database=database)
                for action in actions:
                    if action.get("action_id") != "business.choose":
                        continue
                    business = _mapping(action.get("evidence", {}).get("business"), "native business offer")
                    kind = business.get("businessType", 0)
                    kind = _BUSINESS_TYPES.get(kind, kind)
                    if kind != action["target"].get("business_type") or kind not in (1, 2, 3, 4):
                        raise ValueError("business type does not bind native target")
                    score = _business_value(connection, business, context, card_value, database=database)
                    if score is not None:
                        evaluations.append({"target": dict(action["target"]), "score": score[0], "business_type": kind, **score[1]})
                if evaluations:
                    best = max(evaluations, key=lambda row: (row["budget_priority"], row["resource_preference"], row["ranking_key"]))
                    return selected(next(action for action in actions if action["target"] == best["target"]), "依目前 P／體力與後續可選收益比較營業")
            elif surface == "schedule":
                schedules = _mapping(raw.get("collections"), "native collections").get("schedule", [])
                # The real ScheduleScreenPresenter reads get_NextSchedule(),
                # not CurrentSchedule. APK 0x7316B38 uses min(count, week + 1).
                # Only accept the actual matching native row; never reuse a
                # previous week's selectedStepType or invent missing contents.
                schedule_week = min(len(schedules), context["week"] + 1)
                current_rows = [row for row in schedules if row.get("stepNumber", 0) == schedule_week]
                if len(current_rows) != 1:
                    raise ValueError("native next schedule does not bind one actual schedule row")
                schedule = current_rows[0]
                metadata["schedule_week"] = schedule_week
                metadata["schedule_source"] = "native schedule collection; verified get_NextSchedule semantics"
                candidates, bindings = [], {}
                for ordinal, action in enumerate(actions):
                    target = action["target"]
                    step = target.get("step_type")
                    identity = f"native:{ordinal}"
                    if step in _LESSONS:
                        axis, sp = _LESSONS[step]
                        field = axis + ("Sp" if sp else "") + "ProduceStepSelfLessonId"
                        lesson = _master_row(Path(master_dir), "ProduceStepSelfLesson.yaml", str(schedule.get(field, "")))
                        cost, recovery, stamina_detail = _lesson_stamina(lesson, step, context, Path(master_dir))
                        candidates.append(OuterPolicyCandidate(identity, "lesson", {axis: int(lesson["parameter"])},
                                                               stamina_cost=cost, stamina_recovery=recovery))
                        bindings[identity] = action
                        evaluations.append({"target": dict(target), "lesson_id": lesson["id"], "field": field,
                                            "base_gain": lesson["parameter"], **stamina_detail, "effect_source": "Master ProduceStepSelfLesson"})
                    elif step == 14 or target.get("button_id") == "schedule.refresh":
                        candidates.append(OuterPolicyCandidate(identity, "recovery", stamina_recovery=context["max_stamina"] * context["rest_permille"] // 1000))
                        bindings[identity] = action
                prior_scores, metadata["schedule_prior"] = _schedule_behavior_prior(context, actions, bindings, schedule_week,
                                                                                    weekly_bc_provider_path)
                recovery_action = None
                if any(candidate.kind == "lesson" for candidate in candidates):
                    state = NiaStrategyState(**{key: context[key] for key in ("stamina", "max_stamina", "produce_points", "vocal", "dance", "visual", "vote_count")})
                    core = rank_outer_policy(OuterPolicyRequest(produce_id, tuple(str(a["target"].get("step_type", "rest")) for a in bindings.values()),
                        schedule_week, context["next_exam"]["stage"], tuple(bindings), state, context["growth"],
                        OuterDeckSummary(len(context["deck"]), sum(int(c.get("upgradeCount", 0)) > 0 for c in context["deck"]), 0, 0),
                        OuterPolicyTargets(context["next_exam"]["weeks_until"], context["weeks_remaining"], context["stamina_reserve"],
                                           context["next_exam"]["targets"], context["final_targets"], context["target_source"]),
                        OuterPolicyWeights(learned_prior=SCHEDULE_PRIOR_WEIGHT), tuple(candidates),
                        learned_prior_scores=prior_scores))
                    if core.ready:
                        metadata["core_evaluations"] = [
                            {"id": value.candidate_id, "eligible": value.eligible, "score": None if value.scores is None else value.scores.total,
                             "reasons": value.reasons,
                             "learned_prior_contribution": None if value.scores is None else value.scores.components["learned_prior"]}
                            for value in core.evaluations]
                        specs = {candidate.candidate_id: candidate for candidate in candidates}
                        useful_lessons = [value for value in core.evaluations
                            if value.eligible and specs[value.candidate_id].kind == "lesson"
                            and any(gain > 0 and context[axis] < context["attribute_cap"]
                                    for axis, gain in specs[value.candidate_id].attribute_gains.items())]
                        if useful_lessons:
                            best_lesson = max(useful_lessons, key=lambda value: value.scores.total)
                            return selected(bindings[best_lesson.candidate_id], "優先可負擔且仍有屬性收益的自主課程",
                                rest_policy="recovery-after-affordable-productive-opportunities")
                        if specs[core.chosen_id].kind == "recovery":
                            recovery_action = bindings[core.chosen_id]
                # Productive weeks use the complete native macro candidate set;
                # fine rewards remain owned by the business/event/card selector.
                from .runtime_schedule_opportunity import choose_runtime_schedule_opportunity
                opportunity, metadata["schedule_opportunity"] = choose_runtime_schedule_opportunity(
                    raw, context, schedule_week=schedule_week, weekly_bc_provider_path=weekly_bc_provider_path)
                if opportunity is not None:
                    return selected(next(action for action in actions if action["target"] == opportunity),
                        "依強化預算與本週回放偏好選擇培育機會", comparison_scope="complete-native-schedule-opportunities")
                # Incomplete optional evidence or a mixed lesson set retains the
                # normal entry fallback. Never synthesize a callback to skip it.
                for step in (25, 26, 11, 10, 28, 27, 15, 12, 13):
                    matches = [a for a in actions if a.get("action_id") == "schedule.choose" and a["target"].get("step_type") == step]
                    if len(matches) == 1:
                        return selected(matches[0], "進入目前可用的培育機會；下一頁依真實候選與牌庫重新比較", comparison_scope="productive-opportunity-order")
                if recovery_action is not None:
                    return selected(recovery_action, "目前沒有可負擔且有收益的培育機會，先恢復體力")
                if len(bindings) == 1 and candidates[0].kind == "recovery":
                    return selected(next(iter(bindings.values())), "目前僅有休息選項")
            return NativeOuterDecision(None, {**metadata, "reason": "native family requires a specialized current-candidate policy", "family": surface})
    except (ImportError, KeyError, TypeError, ValueError, OSError, sqlite3.Error) as error:
        return NativeOuterDecision(None, {**metadata, "reason": str(error), "error_type": type(error).__name__})


__all__ = ["NativeOuterDecision", "build_runtime_outer_context", "choose"]
