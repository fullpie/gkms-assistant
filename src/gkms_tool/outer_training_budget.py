"""A concrete current-deck budget for the next NIA Pro pre-audition Customize.

This is a revisable training preference, not a purchase/entry command or a hard
PT floor. Native menus still own prices and eligibility. No future card gains,
business income, discounts or server terminal conversion are presumed.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from typing import Any

from .deck_value import (available_card_customizations, card_customizations,
                         evaluate_deck_delta, normalize_deck, with_card_customization)
from .logic_engine import load_master_card
from .master_db import DEFAULT_DATABASE
from .passive_catalog import DEFAULT_MASTER_DIR


SCHEMA = "gkms.outer-training-budget.v1"
_CUSTOMIZE = "ProduceStepType_Customize"
_AUDITIONS = frozenset("ProduceStepType_" + name for name in ("AuditionMid1", "AuditionMid2", "AuditionFinal"))
_UNSELECTED = (None, "ProduceStepType_Unknown")
# Named native lifecycle phases, not numeric status ordering: campaign's
# BeforeStep phase is 20, while its AfterStep phase is 21. A finished Customize
# can still be the visible screen after the API advanced to SelectNextStep or
# an AfterStep event (recorded finish receipts 9388c9... and e4a5d5...).
_CURRENT_STEP_FINISHED_PHASES = frozenset("ProduceProgressStatus_" + name for name in (
    "SelectNextStep", "AfterStepAuditionCharacterEvent", "AfterStepCharacterGrowthEvent",
    "AfterStepCharacterDearnessStory", "AfterStepIdolCardEvent", "AfterStepSupportCardEvent",
    "AfterStepCharacterEvent", "AfterStepCampaignEvent",
))
_LIMITATIONS = (
    "Master option and price are planning evidence; native menu must recheck eligibility and price",
    "current observed deck/resources only; future cards, upgrades, discounts and income are not assumed",
    "estimated gain is a semantic ranking proxy, not a predicted exam score or guaranteed clear",
    "replay customization goals are preferences; missing cards/ordinary upgrades remain conditional",
    "budget priority expresses a preference; insufficient funds do not block all candidates",
)


def _object(value, name):
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _int(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _deck_identity(deck):
    return sorted([{"card_number": _int(row.get("deck_number"), "native card Number"),
                    "card_id": row["card_id"], "upgrade": row["upgrade"],
                    "customizes": sorted(card_customizations(row))} for row in deck],
                  key=lambda row: row["card_number"])


def _remaining_schedule(raw, context, week):
    total = _int(context.get("total_steps"), "Master total steps", 1)
    if week > total:
        raise ValueError("native week exceeds Master total steps")
    schedule = _object(raw.get("collections"), "native collections").get("schedule")
    if not isinstance(schedule, (list, tuple)) or not schedule:
        raise ValueError("actual native schedule is missing")
    rows = {}
    for row in schedule:
        row = _object(row, "native schedule row")
        number = _int(row.get("stepNumber"), "native schedule stepNumber", 1)
        steps = row.get("stepTypes")
        if (number > total or number in rows or not isinstance(steps, (list, tuple)) or not steps
                or any(not isinstance(step, str) or not step for step in steps)
                or len(set(steps)) != len(steps)):
            raise ValueError("native schedule row is malformed or duplicated")
        selected = row.get("selectedStepType")
        if selected not in _UNSELECTED and selected not in steps:
            raise ValueError("native selected step is not in the actual schedule row")
        rows[number] = {"stepNumber": number, "stepTypes": list(steps), "selectedStepType": selected}
    numbers = range(max(1, week), total + 1)
    if any(number not in rows for number in numbers):
        raise ValueError("actual native remaining schedule is incomplete; no calendar fallback")
    current_commit = None
    progress = _object(raw.get("progress"), "native progress")
    current_finished = progress.get("status") in _CURRENT_STEP_FINISHED_PHASES
    remaining = [rows[number] for number in numbers if number != week or not current_finished]
    if week in rows and progress.get("status") == "ProduceProgressStatus_StepAction":
        current_commit = progress.get("stepType")
        selected = rows[week]["selectedStepType"]
        if (current_commit not in rows[week]["stepTypes"]
                or selected not in _UNSELECTED and selected != current_commit):
            raise ValueError("current native step action conflicts with schedule selection")
        # Normalize the same observed commitment for every downstream window,
        # including optional Customize rows before the final deadline.
        rows[week]["selectedStepType"] = current_commit
    for row in remaining:
        number = row["stepNumber"]
        following = rows.get(number + 1)
        selected = current_commit if number == week and current_commit else row["selectedStepType"]
        if (_CUSTOMIZE in row["stepTypes"] and selected in (*_UNSELECTED, _CUSTOMIZE)
                and following and len(following["stepTypes"]) == 1 and following["stepTypes"][0] in _AUDITIONS):
            return remaining, (number, following["stepTypes"][0])
    return remaining, None


def _valuation_context(context, target_week, audition, master_dir):
    outlooks = context.get("exam_outlooks")
    if not isinstance(outlooks, (list, tuple)) or not outlooks:
        raise ValueError("Master-backed exam outlooks are missing")
    retained = []
    for exam in outlooks:
        exam = _object(exam, "exam outlook")
        week = _int(exam.get("week"), "exam outlook week", 1)
        if week > target_week:
            retained.append(dict(exam))
    if not retained or retained[0].get("week") != target_week + 1 or retained[0].get("stage") != audition:
        raise ValueError("exam outlook does not match the observed customization/audition window")
    # Purchase utility must not be suppressed by today's low wallet; the cost
    # is the amount to reserve. Earlier auditions cannot benefit from this edit.
    return {**context, "exam_outlooks": retained, "next_exam": retained[0],
            "produce_point_cost": 0, "stamina_cost": 0, "master_dir": str(master_dir)}


def build_training_budget(raw, context, *, database=DEFAULT_DATABASE,
                          master_dir=DEFAULT_MASTER_DIR) -> dict[str, Any]:
    """Budget the adopted deck's missing paid customizations at the next exam.

    The shared plan matcher supplies missing customization ID/count steps;
    ordinary upgrade gaps receive no PT price. Missing cards/upgrade prerequisites
    are conditional earmarks, never fabricated native inventory. Without a usable
    reference quote, the prior useful-current-deck estimate remains a fallback.
    Missing native identity, complete remaining schedule or deck is unavailable.
    """
    result: dict[str, Any] = {
        "schema": SCHEMA, "status": "unavailable", "target_week": None, "target_cost": None,
        "current_points": None, "shortfall": None, "target": None,
        "native_option_legality_verified": False, "future_income_assumed": False,
        "limitations": list(_LIMITATIONS), "sources": {},
    }
    try:
        raw, context = _object(raw, "native snapshot"), _object(context, "current outer context")
        state, progress = _object(raw.get("state"), "native state"), _object(raw.get("progress"), "native progress")
        if (raw.get("schema") != "gkms.outer-runtime-snapshot.v1" or state.get("in_progress") is not True
                or context.get("produce_id") != "produce-004" or state.get("produce_id") != "produce-004"
                or progress.get("produceId") != "produce-004"):
            raise ValueError("active NIA Pro identity is unavailable or conflicting")
        idol = context.get("idol_card_id")
        if not isinstance(idol, str) or not idol or progress.get("idolCardId") != idol:
            raise ValueError("current idol identity is unavailable or conflicting")
        week, wallet = _int(state.get("week"), "native week"), _int(state.get("produce_points"), "native PT")
        if (context.get("week") != week or progress.get("stepNumber", 0) != week
                or context.get("produce_points") != wallet or progress.get("producePoint", 0) != wallet):
            raise ValueError("current week or PT differs between native state and context")
        result["current_points"] = wallet
        remaining, window = _remaining_schedule(raw, context, week)
        result["sources"] = {"snapshot_revision": raw.get("revision"), "captured_at": raw.get("captured_at"),
            "schedule": "native collections.schedule", "remaining_schedule_digest": _digest(remaining),
            "remaining_native_schedule": remaining, "mode_rule_digest": context.get("rule_digest"),
            "current_step": {"source": "native progress.status/stepType",
                             "status": progress.get("status"), "step_type": progress.get("stepType")}}
        if window is None:
            result.update(status="no_future_window", reason="no remaining uncommitted Customize immediately before an audition")
            return result
        target_week, audition = window
        result.update(target_week=target_week, audition_week=target_week + 1, audition_stage=audition)
        native_cards = _object(raw.get("collections"), "native collections").get("cards")
        if not isinstance(native_cards, (list, tuple)) or context.get("deck_source") != "native collections.cards":
            raise ValueError("complete native deck evidence is missing")
        # Match the runtime's deleted-row contract before canonicalizing Number,
        # upgrade and actual customization counts; no projected cards are used.
        for card in native_cards:
            card = _object(card, "native card")
            if type(card.get("isDeleted", False)) is not bool:
                raise ValueError("native isDeleted must be boolean")
        native_cards = [row for row in native_cards if not row.get("isDeleted", False)]
        deck, contextual_deck = normalize_deck(native_cards), normalize_deck(context.get("deck"))
        identity = _deck_identity(deck)
        if identity != _deck_identity(contextual_deck):
            raise ValueError("context deck differs from complete observed native deck")
        database, master_dir = Path(database), Path(master_dir)
        result["sources"].update(deck="native collections.cards", deck_digest=_digest(identity),
            master_database=str(database.resolve()),
            customize_master=str((master_dir / "ProduceCardCustomize.yaml").resolve()),
            grow_effect_master=str((master_dir / "ProduceCardGrowEffect.yaml").resolve()))
        from .deck_plan_cost import quote_plan_customizations
        plan_cost = quote_plan_customizations(deck, context, stage=audition,
                                             database=database, master_dir=master_dir)
        result["plan_cost"] = plan_cost
        required_operations = [operation for requirement in plan_cost["requirements"]
                               for operation in requirement["operations"]]
        valuation = _valuation_context(context, target_week, audition, master_dir)
        baseline = evaluate_deck_delta(deck, deck, valuation, database=database)
        baseline_unknown = set(baseline.unscored_effects)
        candidates = []
        for index, card in enumerate(deck):
            options = available_card_customizations(card, database=database, master_dir=master_dir)
            if not options:
                continue
            name = load_master_card(card["card_id"], card["upgrade"], database).name
            for option in options:
                cost = _int(option.get("produce_points"), "Master customization PT cost")
                after = list(deck)
                after[index] = with_card_customization(card, option)
                delta = evaluate_deck_delta(deck, after, valuation, database=database)
                gain = _number(delta.value, "customization utility delta")
                added_unknown = sorted(set(delta.unscored_effects) - baseline_unknown)
                plan = delta.plan_progress or {}
                plan_gain = (_number(plan.get("value"), "same-plan progress") if plan.get("status") == "compared" else 0.)
                modeled = gain > 0 and not added_unknown
                structural = plan_gain > 0 and gain >= 0
                plan_required = plan_gain > 0 and any(
                    operation["card_id"] == card["card_id"]
                    and operation["customize_id"] == option["customize_id"]
                    and operation["customize_count"] == option["customize_count"]
                    for operation in required_operations)
                from .outer_pt_value import produce_point_opportunity_cost
                reserve = valuation.get("produce_point_reserve")
                net_if_funded = gain - produce_point_opportunity_cost(cost, max(wallet, cost),
                    40 if reserve is None else reserve, valuation.get("produce_point_spending_context"))
                candidate = {"card_number": card["deck_number"], "card_id": card["card_id"],
                    "card_name": name, "upgrade": card["upgrade"], "customize_id": option["customize_id"],
                    "customize_count": option["customize_count"], "description": option.get("description", ""),
                    "cost": cost, "estimated_gain": gain, "net_gain_if_funded": net_if_funded,
                    "new_unscored_effects": added_unknown,
                    "unscored_effects": list(delta.unscored_effects), "plan_progress": delta.plan_progress,
                    "useful_target": modeled or structural or plan_required, "plan_required": plan_required, "selection_basis":
                    "same-adopted-plan-gap" if structural else "modeled-positive-gain" if modeled else "no-evaluable-useful-change",
                    "valuation_method": delta.method, "valuation_context_digest": delta.context_digest}
                candidates.append(candidate)
        result.update(evaluated_option_count=len(candidates), candidates=candidates,
                      baseline_unscored_effects=sorted(baseline_unknown))
        watch = [{**row, "mandatory_reserve": False, "occupies_card_slot": False,
                  "reason": "await-card" if row["requires_acquisition"] else "await-ordinary-upgrade"}
                 for row in plan_cost["requirements"]
                 if row["requires_acquisition"] or row["requires_normal_upgrade"]]
        result.update(conditional_goals=watch, preparation_only=True, cross_week_assignment=False)
        # One preparation target, not a prepaid execution schedule. At the real
        # visit the selector chooses affordable useful offers using the live
        # wallet and native per-card/visit limits, recomputing after each spend.
        if plan_cost["status"] == "quoted" and plan_cost["total_pt"] == 0:
            result.update(status="no_target", reason="no outstanding reference customization to prepare")
            return result
        preferred = [row for row in candidates if row["plan_required"]]
        useful = preferred or [row for row in candidates if row["useful_target"] and row["net_gain_if_funded"] > 0]
        if not useful:
            result.update(status="no_target", reason="no useful currently held customization target")
            return result
        def ranking(row):
            progress = row["plan_progress"] or {}
            return (row["net_gain_if_funded"] > 0,
                    float(progress.get("value", 0)) if progress.get("status") == "compared" else 0.,
                    row["net_gain_if_funded"], -row["cost"], -row["card_number"], row["customize_id"])
        target = max(useful, key=ranking)
        result.update(status="active", target=target, target_cost=target["cost"],
                      target_cost_kind="single-preparation-target; execution-replans-from-live-wallet",
                      target_source="observed-paid-customization-goals" if preferred else "useful-current-deck-alternative",
                      shortfall=max(0, target["cost"] - wallet),
                      reason="prepare one useful next customization; spend only affordable native offers at the visit")
        return result
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as error:
        result.update(status="unavailable", reason=f"{type(error).__name__}: {error}")
        return result


def budget_priority(budget, minimum_post_points) -> float:
    """Prefer preserving the concrete target, while allowing every legal option.

    The caller must supply a candidate's evidenced minimum resulting wallet.
    All sufficient candidates tie at zero; when none suffice, smaller deficits
    rank higher. This never guesses an option's cost or future income.
    """
    if not isinstance(budget, Mapping) or budget.get("schema") != SCHEMA or budget.get("status") != "active":
        return 0.
    points = max(0., _number(minimum_post_points, "minimum post-choice PT"))
    target = _number(budget.get("target_cost"), "training budget target cost")
    if target < 0:
        raise ValueError("training budget target cost must be nonnegative")
    return -max(0., target - points)
