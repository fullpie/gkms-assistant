"""Pure PT opportunity policy for the last observed NIA Pro activity choice.

The terminal reserve policy is an explicit ranking prior: once the complete
native remaining schedule contains only the final audition after this activity,
PT above the caller's reserve has no *modeled scheduled opportunity* charge.
Conditional event costs and the server's unused-PT terminal value remain unknown.
Neither a free terminal conversion nor the absence of conditional benefits is
asserted. Affordability and the value of the actual option belong to the caller.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
from typing import Any


SCHEMA = "gkms.outer-pt-opportunity-context.v1"
TERMINAL_RESERVE_POLICY = "nia-pro-last-activity-reserve-prior-v1"
DEFAULT_RESERVE = 40.0
_ACTIVITY = "ProduceStepType_EventActivity"
_FINAL = "ProduceStepType_AuditionFinal"


def _mapping(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def build_outer_pt_context(raw: Mapping, mode_context: Mapping) -> dict[str, Any]:
    """Bind a bounded spending prior to actual native schedule/choice evidence.

    ``mode_context`` is the already resolved Master-backed mode rule context;
    its ``total_steps`` supplies the endpoint, never a guessed week count.
    Audition outlooks/static calendar fallbacks cannot stand in for missing
    native schedule rows. Missing or conflicting evidence retains the original
    wallet/reserve heuristic. The scope deliberately excludes repeat-purchase
    menus (Shop/Customize) and event choices before the final activity boundary.
    """
    raw, mode_context = _mapping(raw), _mapping(mode_context)
    state, progress = _mapping(raw.get("state")), _mapping(raw.get("progress"))
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "policy": "wallet-reserve-heuristic-v1",
        "terminal_reserve_eligible": False,
        "reason": "native-evidence-incomplete",
        "unused_pt_terminal_value": "unknown; server conversion is unproven",
        "conditional_event_spending": "unknown; caller reserve is a policy prior",
        "reserve_source": "caller produce_point_reserve; default 40 PT",
        "future_scheduled_discretionary_pt_windows": None,
        "schedule_source": "native collections.schedule; no calendar substitution",
        "endpoint_source": "caller Master-backed mode_context.total_steps",
        "mode_rule_digest": mode_context.get("rule_digest"),
        "snapshot_revision": raw.get("revision"),
    }

    def unchanged(reason: str) -> dict[str, Any]:
        result["reason"] = reason
        return result

    if mode_context.get("produce_id") != "produce-004":
        return unchanged("outside-nia-pro")
    if (state.get("produce_id") != "produce-004" or progress.get("produceId") != "produce-004"
            or state.get("in_progress") is not True):
        return unchanged("native-mode-mismatch-or-inactive")
    week, total = state.get("week"), mode_context.get("total_steps")
    if (type(week) is not int or type(total) is not int or not 1 <= week < total
            or progress.get("stepNumber") != week or mode_context.get("week") != week):
        return unchanged("native-week-or-master-endpoint-missing-or-conflicting")
    result.update(week=week, total_steps=total)
    schedule = _mapping(raw.get("collections")).get("schedule")
    if not isinstance(schedule, (list, tuple)) or not schedule:
        return unchanged("native-schedule-missing")
    rows = {}
    for row in schedule:
        if not isinstance(row, Mapping):
            return unchanged("native-schedule-row-malformed")
        number, steps = row.get("stepNumber"), row.get("stepTypes")
        if (type(number) is not int or not 1 <= number <= total or number in rows
                or not isinstance(steps, (list, tuple)) or not steps
                or any(not isinstance(step, str) or not step for step in steps)):
            return unchanged("native-schedule-row-malformed-or-duplicated")
        rows[number] = row
    if any(number not in rows for number in range(week, total + 1)):
        return unchanged("native-remaining-schedule-incomplete")
    remaining = [{"stepNumber": number, "stepTypes": list(rows[number]["stepTypes"]),
                  "selectedStepType": rows[number].get("selectedStepType")}
                 for number in range(week, total + 1)]
    result.update(remaining_native_schedule=remaining,
                  schedule_digest=hashlib.sha256(json.dumps(remaining, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")).hexdigest())
    # Keep this prior local to the observed final activity -> Final boundary.
    # Other future step types need their own spending/effect semantics first.
    if week != total - 1 or list(rows[total]["stepTypes"]) != [_FINAL]:
        return unchanged("not-last-activity-before-final-only-schedule")
    if (rows[week].get("selectedStepType") != _ACTIVITY
            or _ACTIVITY not in rows[week]["stepTypes"]
            or progress.get("stepType") != _ACTIVITY
            or progress.get("status") != "ProduceProgressStatus_StepAction"
            or state.get("step_type") != 11 or state.get("progress_status") != 8):
        return unchanged("current-step-is-not-selected-native-activity")
    ui = _mapping(raw.get("ui_state"))
    detail_id = ui.get("event_detail_id")
    if (raw.get("schema") != "gkms.outer-runtime-snapshot.v1"
            or raw.get("surface") != "event" or raw.get("screen_type") != "ScheduleEventScreenPresenter"
            or raw.get("actions_complete") is not True or raw.get("busy") is not False
            or raw.get("blockers") != [] or ui.get("busy") is not False
            or ui.get("executed") is not False or "selected_index" not in ui
            or ui.get("selected_index") is not None
            or not isinstance(detail_id, str) or not detail_id or progress.get("stepId") != detail_id):
        return unchanged("native-single-event-choice-boundary-unproven")
    actions = raw.get("legal_actions")
    if not isinstance(actions, (list, tuple)) or not actions:
        return unchanged("native-event-choices-missing")
    indexes, suggestions = set(), set()
    for action in actions:
        action = _mapping(action)
        target = _mapping(action.get("target"))
        index, suggestion_id = target.get("index"), target.get("suggestion_id")
        suggestion = _mapping(_mapping(action.get("evidence")).get("suggestion"))
        if (action.get("action_id") != "event.choose" or target.get("action_id") != "event.choose"
                or target.get("event_detail_id") != detail_id or type(index) is not int or index < 0
                or index in indexes or not isinstance(suggestion_id, str) or not suggestion_id
                or suggestion_id in suggestions or suggestion.get("id") != suggestion_id):
            return unchanged("native-exclusive-event-choice-set-unproven")
        indexes.add(index)
        suggestions.add(suggestion_id)
    result.update(policy=TERMINAL_RESERVE_POLICY, terminal_reserve_eligible=True,
                  reason="complete-native-final-only-tail-and-exclusive-activity-choice",
                  future_scheduled_discretionary_pt_windows=0,
                  event_detail_id=detail_id, event_choice_count=len(actions),
                  opportunity_scope="scheduled windows only; conditional/terminal values remain unknown")
    return result


def produce_point_opportunity_cost(points: float, wallet: float, reserve: float = DEFAULT_RESERVE,
                                   spending_context: Mapping | None = None) -> float:
    """Return ranking utility charge, without approving or forcing a purchase.

    Under the bounded terminal prior, only the portion consuming the reserve
    receives the old per-PT charge. A cost exceeding the wallet remains the
    caller's affordability error; this helper never treats cost as available PT.
    """
    values = []
    for name, value in (("points", points), ("wallet", wallet), ("reserve", reserve)):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number")
        values.append(max(0.0, float(value)))
    points, wallet, reserve = values
    chargeable = points
    context = _mapping(spending_context)
    if (context.get("schema") == SCHEMA and context.get("policy") == TERMINAL_RESERVE_POLICY
            and context.get("terminal_reserve_eligible") is True):
        chargeable = max(0.0, points - max(0.0, wallet - reserve))
    return chargeable * (.04 + .12 * min(1.0, reserve / max(1.0, wallet)))


def produce_point_reward_value(delta: float, wallet: float, reserve: float = DEFAULT_RESERVE,
                               spending_context: Mapping | None = None) -> float:
    """Value a PT change with the same bounded opportunity prior as spending.

    Terminal gains only replenish a missing reserve; additional PT has no
    modeled scheduled use. Terminal conversion remains unknown. The unit price
    is fixed at the observed wallet, so a larger reward cannot lower utility.
    Losses receive exactly the negative opportunity charge at that same wallet.
    """
    if type(delta) not in (int, float) or not math.isfinite(delta):
        raise ValueError("delta must be a finite number")
    if delta < 0:
        return -produce_point_opportunity_cost(-delta, wallet, reserve, spending_context)
    # This also validates/clamps wallet and reserve exactly as the cost helper.
    unit_price = produce_point_opportunity_cost(1.0, wallet, reserve)
    credited = float(delta)
    context = _mapping(spending_context)
    if (context.get("schema") == SCHEMA and context.get("policy") == TERMINAL_RESERVE_POLICY
            and context.get("terminal_reserve_eligible") is True):
        reserve_gap = max(0.0, max(0.0, float(reserve)) - max(0.0, float(wallet)))
        credited = min(credited, reserve_gap)
    return credited * unit_price
