"""Pure HP valuation near a pending audition refresh.

Actual HP/cost/affordability remain unchanged. The near-audition adjustment is
only a ranking metric; missing timing/rules keep the ordinary current-HP cost.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .audition_stamina import audition_start_stamina


_STAGES = {16: "Mid1", 17: "Mid2", 18: "Final",
    "Mid1": "Mid1", "Mid2": "Mid2", "Final": "Final",
    "ProduceStepType_AuditionMid1": "Mid1", "ProduceStepType_AuditionMid2": "Mid2",
    "ProduceStepType_AuditionFinal": "Final"}
_RETRY_SURFACES = {"audition_retry", "audition_retry_select", "audition_retry_confirm", "exam_retry"}
_LESSON_SURFACES = {"lesson", "lesson_exam", "native_lesson"}


def _int(value, name, *, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _field(context, name, default=None):
    if name in context:
        return context[name]
    state = context.get("state")
    return state.get(name, default) if isinstance(state, Mapping) else default


def _stage(value):
    return _STAGES.get(value) if isinstance(value, (int, str)) and not isinstance(value, bool) else None


def _settings(context):
    if context.get("settings") is not None:
        return context["settings"]
    profile = context.get("mode_profile")
    if isinstance(profile, Mapping):
        return profile.get("settings")
    return getattr(profile, "settings", None)


@dataclass(frozen=True, slots=True)
class _StaminaFrame:
    current: int
    maximum: int
    credited: bool
    reason: str
    baseline: object | None
    settings: object | None

    def value_at(self, actual_hp: int) -> int:
        hp = min(self.maximum, max(0, actual_hp))
        if not self.credited:
            return hp
        # The same floor baseline is used on both sides of a comparison.
        return audition_start_stamina(hp, self.maximum, self.settings).stamina_min


def _frame(context: Mapping[str, object]) -> _StaminaFrame:
    if not isinstance(context, Mapping):
        raise TypeError("resource context must be a mapping")
    current = _int(_field(context, "stamina"), "actual stamina")
    maximum = _int(_field(context, "max_stamina"), "actual max_stamina", minimum=1)
    if current > maximum:
        raise ValueError("actual stamina exceeds max_stamina")
    settings = _settings(context)
    try:
        baseline = audition_start_stamina(current, maximum, settings)
    except (TypeError, ValueError):
        # Absence of the new value metadata must never stop the old policy.
        baseline = None

    def keep(reason):
        return _StaminaFrame(current, maximum, False, reason, baseline, settings)

    surface = context.get("surface")
    scope = context.get("resource_scope")
    if (scope == "lesson" or context.get("stage") == "Lesson" or context.get("is_lesson") is True
            or _field(context, "exam_type", _field(context, "examType")) == 0 or surface in _LESSON_SURFACES):
        return keep("lesson-has-no-audition-refresh-credit")
    if scope == "exam":
        return keep("exam-hp-does-not-use-outer-refresh-credit")
    if (context.get("is_retry") is True or context.get("retry") is True or scope == "retry"
            or surface in _RETRY_SURFACES):
        return keep("retry-does-not-repeat-audition-refresh")
    if context.get("audition_refresh_completed") is True:
        return keep("audition-refresh-already-completed")
    upcoming = context.get("next_exam")
    if not isinstance(upcoming, Mapping) or _stage(upcoming.get("stage")) is None:
        return keep("next-audition-unavailable")
    weeks = upcoming.get("weeks_until")
    if type(weeks) is not int or weeks < 0:
        return keep("next-audition-timing-unavailable")
    if weeks > 1:
        return keep("cultivation-stamina-needed-before-future-refresh")
    current_stage = _stage(_field(context, "step_type", _field(context, "step_type_value")))
    same_stage = current_stage == _stage(upcoming["stage"])
    status = _field(context, "progress_status")
    if same_stage and (type(status) is not int or status < 0):
        return keep("current-audition-refresh-timing-unconfirmed")
    if same_stage and type(status) is int and status >= 6:
        return keep("same-audition-refresh-already-completed")
    if weeks == 0 and not (same_stage and type(status) is int and status == 5):
        return keep("current-audition-refresh-timing-unconfirmed")
    if baseline is None:
        return keep("audition-refresh-settings-unavailable")
    return _StaminaFrame(current, maximum, True, "pending-near-audition-refresh", baseline, settings)


def stamina_value_context(context: Mapping[str, object]) -> dict[str, object]:
    """Explain the ranking adjustment without changing the supplied state."""
    frame = _frame(context)
    baseline = frame.baseline
    return {
        "schema": "gkms.outer-stamina-value.v1", "recovery_credited": frame.credited, "reason": frame.reason,
        "actual_stamina": frame.current, "max_stamina": frame.maximum,
        "stamina_for_valuation_min": frame.value_at(frame.current),
        "stamina_for_valuation_max": (baseline.stamina_max if frame.credited else frame.current),
        "mode_recovery_permille": None if baseline is None else baseline.recovery_permille,
        "rounding": None if baseline is None else baseline.rounding,
        "source": "actual native HP" if not frame.credited else baseline.source,
        "exact": False, "modifiers_projected": False, "estimate_scope": "mode-baseline",
        "valuation_is_actual_hp": not frame.credited,
    }


def effective_stamina_cost(context: Mapping[str, object], cost: int, *, recovery: int = 0) -> int:
    """HP loss relevant to ranking; never replace actual cost/legality with it."""
    cost, recovery = _int(cost, "stamina cost"), _int(recovery, "stamina recovery")
    frame = _frame(context)
    # Cost is paid first; recovery does not grant HP to pay an unaffordable cost.
    after = min(frame.maximum, max(0, frame.current - cost) + recovery)
    return max(0, frame.value_at(frame.current) - frame.value_at(after))


def recovery_value(context: Mapping[str, object], recovery: int, *, stamina_cost: int = 0) -> int:
    """Marginal HP recovery value under the same current/floor baseline."""
    recovery, cost = _int(recovery, "stamina recovery"), _int(stamina_cost, "stamina cost")
    frame = _frame(context)
    before = max(0, frame.current - cost)
    after = min(frame.maximum, before + recovery)
    return frame.value_at(after) - frame.value_at(before)


def required_pre_refresh_reserve(context: Mapping[str, object], desired_start_reserve: int) -> int:
    """Minimum HP to retain for the desired entry baseline, without borrowing."""
    desired = _int(desired_start_reserve, "desired entry stamina reserve")
    frame = _frame(context)
    if not frame.credited or desired > frame.maximum:
        return desired
    lower_baseline_gain = audition_start_stamina(0, frame.maximum, frame.settings).stamina_min
    return max(0, desired - lower_baseline_gain)
