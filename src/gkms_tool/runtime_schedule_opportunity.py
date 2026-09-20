"""Non-lesson schedule preferences over one complete native candidate set.

Weekly imitation supplies preferences, never invented reward amounts. The
caller owns lesson/HP planning, the revisable training budget, and all input.
Actual business/event/product selectors recheck their own rewards and costs.
"""
from __future__ import annotations

from collections.abc import Mapping
import math

from .outer_training_budget import SCHEMA as BUDGET_SCHEMA
from .runtime_outer_behavior_prior import score_runtime_outer_behavior_prior
from .runtime_schedule_prior import coarse_schedule_label, score_runtime_schedule_prior


SCHEMA = "gkms.runtime-schedule-opportunity.v1"
_NAMES = {10: "Event", 11: "EventActivity", 12: "EventSchool", 13: "Shop", 14: "Refresh",
          15: "Present", 25: "Business", 26: "EventBusiness", 27: "FanPresent", 28: "Customize"}
_LESSONS = frozenset(range(19, 25))
_FALLBACK_ORDER = (25, 26, 27, 15, 11, 28, 13, 10, 12)


def _int(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(label + " must be an observed nonnegative integer")
    return value


def _prior(context, targets, week, provider_path, behavior_prior):
    attempts = []
    if provider_path is not None:
        bc = score_runtime_outer_behavior_prior({**context, "surface": "schedule"}, targets,
            schedule_number=week, candidate_set_complete=True, provider_path=provider_path)
        attempts.append(bc.to_dict())
        if bc.available:
            return dict(bc.scores), {"source_kind": "candidate-conditioned-weekly-bc", "attempts": attempts}
    counts = score_runtime_schedule_prior({**context, "surface": "schedule"},
        {key: coarse_schedule_label(target) for key, target in targets.items()},
        schedule_number=week, candidate_set_complete=True, prior=behavior_prior)
    attempts.append(counts.to_dict())
    return (dict(counts.scores) if counts.available else {}), {
        "source_kind": "weekly-behavior-counts" if counts.available else "unavailable", "attempts": attempts}


def _customize_status(budget, wallet, week, revision):
    """Use all useful current-deck quotes, not only the most expensive target."""
    result = {"knowledge": "unknown", "affordable_useful_count": None, "window_due": False,
              "reason": "no-current-training-budget"}
    if not isinstance(budget, Mapping) or budget.get("schema") != BUDGET_SCHEMA:
        return result
    sources = budget.get("sources") or {}
    if not isinstance(sources, Mapping):
        return {**result, "reason": "training-budget-provenance-malformed"}
    if (type(budget.get("current_points")) is not int or budget["current_points"] != wallet
            or sources.get("snapshot_revision") not in (None, revision)):
        return {**result, "reason": "training-budget-stale; no-affordability-inference"}
    target_week = budget.get("target_week")
    if type(target_week) is not int or target_week < week:
        return {**result, "reason": "training-budget-window-unavailable-or-past"}
    if budget.get("status") not in {"active", "no_target"}:
        return {**result, "reason": "training-budget-has-no-current-quotes"}
    candidates = budget.get("candidates")
    if not isinstance(candidates, list):
        return {**result, "reason": "training-budget-candidate-quotes-unavailable"}
    useful, uncertain = [], []
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or type(candidate.get("useful_target")) is not bool:
            return {**result, "reason": "training-budget-candidate-quote-malformed"}
        cost = candidate.get("cost")
        if type(cost) is not int or cost < 0:
            return {**result, "reason": "training-budget-price-malformed"}
        if candidate["useful_target"]:
            useful.append(candidate)
        elif candidate.get("new_unscored_effects") and cost <= wallet:
            uncertain.append(candidate)
    affordable = [row for row in useful if row["cost"] <= wallet]
    return {"knowledge": "partial" if uncertain else "known", "affordable_useful_count": len(affordable),
            "affordable_unscored_count": len(uncertain), "useful_count": len(useful),
            "minimum_useful_cost": min((row["cost"] for row in useful), default=None),
            "affordable_customizes": [{key: row.get(key) for key in ("card_number", "card_id", "customize_id", "cost")}
                                      for row in affordable],
            "window_due": target_week == week, "target_week": target_week,
            "native_option_legality_verified": False,
            "reason": ("affordable customization effects are unscored; keep the entry for weekly preference, not proof of zero benefit"
                       if uncertain else "current-deck Master quotes; later native selector verifies each actual operation")}


def choose_runtime_schedule_opportunity(native, context, *, schedule_week=None, training_budget=None,
                                        weekly_bc_provider_path=None, behavior_prior=None):
    """Choose an unchanged non-lesson target, or return None with diagnostics.

    A useful affordable customization has priority in its upcoming training
    window. Other productive entries use whole-candidate weekly preferences;
    recovery is last. No future PT income or reward portfolio is synthesized.
    None on a lesson-containing set delegates the complete comparison to the
    caller's existing lesson core. A sole known-unaffordable Customize may
    enter its normal page with ``normal_skip_required``; this does not claim
    an affordable purchase or invent a disabled recovery/skip callback.
    """
    detail = {"schema": SCHEMA, "source": "native-schedule-opportunity", "status": "not-applicable",
              "owns_input": False, "predicts_game_score": False, "future_income_assumed": False,
              "reward_values_invented": False}
    try:
        raw = native.raw if hasattr(native, "raw") else native
        if not isinstance(raw, Mapping) or raw.get("surface") != "schedule":
            return None, detail
        if not isinstance(context, Mapping):
            raise ValueError("current outer context is unavailable")
        if context.get("produce_id") not in {"produce-004", "produce-005"}:
            return None, {**detail, "reason": "outside-current-NIA-rule-coverage"}
        if raw.get("busy") is not False or raw.get("actions_complete") is not True or raw.get("blockers", []):
            return None, {**detail, "status": "waiting", "reason": "native-schedule-boundary-not-ready"}
        state = raw.get("state") or {}
        wallet, current_week = _int(state.get("produce_points"), "native wallet"), _int(state.get("week"), "native week")
        for key, value in (("produce_id", context["produce_id"]), ("week", context.get("week")),
                           ("produce_points", context.get("produce_points"))):
            if type(state.get(key)) is not type(value) or state.get(key) != value:
                raise ValueError("schedule context differs from native " + key)
        total = _int(context.get("total_steps"), "Master total steps", 1)
        expected_week = min(current_week + 1, total)
        week = expected_week if schedule_week is None else _int(schedule_week, "pending schedule week", 1)
        if week != expected_week:
            raise ValueError("pending schedule number differs from current native week")
        schedule = (raw.get("collections") or {}).get("schedule")
        if not isinstance(schedule, list):
            raise ValueError("actual native schedule is unavailable")
        rows = [row for row in schedule if isinstance(row, Mapping) and row.get("stepNumber") == week]
        if len(rows) != 1 or not isinstance(rows[0].get("stepTypes"), list):
            raise ValueError("pending schedule must bind one actual native row")
        steps = rows[0]["stepTypes"]
        actions = raw.get("legal_actions")
        if not isinstance(actions, list) or not actions:
            return None, {**detail, "status": "waiting", "reason": "native-schedule-actions-unavailable"}
        targets, mapped = {}, {}
        for index, action in enumerate(actions):
            if not isinstance(action, Mapping) or not isinstance(action.get("target"), Mapping):
                raise ValueError("native schedule action is malformed")
            target = action["target"]
            if target.get("action_id") != action.get("action_id"):
                raise ValueError("native action and target disagree")
            if action["action_id"] == "ui.navigation" and target.get("button_id") == "schedule.refresh":
                step = 14
            elif action["action_id"] == "schedule.choose":
                step = _int(target.get("step_type"), "native step type")
                _int(target.get("index"), "native candidate index")
            else:
                return None, {**detail, "reason": "unmapped-action; no-subset-ranking"}
            identity = f"native:{index}"
            targets[identity], mapped[identity] = target, step
        if any(step in _LESSONS for step in mapped.values()):
            return None, {**detail, "reason": "lesson-core-owns-full-comparison", "native_candidate_count": len(targets)}
        if any(step not in _NAMES for step in mapped.values()):
            return None, {**detail, "reason": "unmapped-step; no-subset-ranking"}
        if any("ProduceStepType_" + _NAMES[step] not in steps for step in mapped.values()):
            raise ValueError("native action does not belong to the pending schedule row")
        budget = training_budget if training_budget is not None else context.get("training_budget")
        customization = _customize_status(budget, wallet, week, raw.get("revision"))
        scores, prior_detail = _prior(context, targets, week, weekly_bc_provider_path, behavior_prior)
        if scores and (set(scores) != set(targets) or any(type(v) not in (int, float) or not math.isfinite(v)
                                                        or not 0 <= v <= 1 for v in scores.values())):
            scores = {}
            prior_detail = {**prior_detail, "source_kind": "unavailable", "discarded_reason": "incomplete-or-invalid-prior"}
        evaluations = []
        for identity, step in mapped.items():
            useful = step != 14
            reason = "productive-entry; detailed rewards and costs remain owned by the later native selector"
            if step == 28 and customization["knowledge"] == "known" and customization["affordable_useful_count"] == 0:
                useful, reason = False, "no-affordable-useful-current-deck-customization"
            window_priority = step == 28 and useful and customization["knowledge"] == "known" and customization["window_due"]
            evaluations.append({"id": identity, "target": dict(targets[identity]), "step_type": step,
                "productive": useful, "training_window_priority": bool(window_priority),
                "weekly_preference": scores.get(identity) if scores else None,
                "reason": "recovery-last" if step == 14 else reason})
        productive = [row for row in evaluations if row["productive"]]
        detail.update(schedule_week=week, training_budget=customization, weekly_prior=prior_detail,
                      evaluations=evaluations, native_candidate_count=len(targets),
                      reward_quote_scope="schedule entry only; stale received presents are not future cash")
        if productive:
            def ranking(row):
                fallback = len(_FALLBACK_ORDER) - _FALLBACK_ORDER.index(row["step_type"])
                return row["training_window_priority"], row["weekly_preference"] or 0., fallback
            best = max(productive, key=ranking)
            return dict(best["target"]), {**detail, "status": "ready", "chosen_id": best["id"],
                "reason": "affordable useful training window first; weekly preferences among productive entries"}
        rest = [row for row in evaluations if row["step_type"] == 14]
        if rest:
            return dict(rest[0]["target"]), {**detail, "status": "ready", "chosen_id": rest[0]["id"],
                "reason": "no useful affordable productive entry; use the existing native recovery action"}
        if len(evaluations) == 1 and evaluations[0]["step_type"] == 28:
            return dict(evaluations[0]["target"]), {**detail, "status": "ready", "normal_skip_required": True,
                "chosen_id": evaluations[0]["id"], "entry_purpose": "reach-existing-normal-customize-skip-flow",
                "reason": "sole native Customize entry; later page must use its normal no-purchase route"}
        return None, {**detail, "status": "no-useful-entry", "normal_skip_required": True,
                      "reason": "no useful productive entry or native recovery; do not invent a disabled action"}
    except (KeyError, TypeError, ValueError, OSError) as error:
        return None, {**detail, "status": "invalid", "reason": f"{type(error).__name__}: {error}"}
