"""Pure before-audition recovery estimates; server observations stay exact."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass


MASTER_FIELD = "beforeAuditionRefreshStaminaRecoveryPermil"


def _integer(value, label, *, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**31 - 1:
        raise ValueError(f"{label} must be Int32 >= {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class AuditionStartStamina:
    current_stamina: int
    max_stamina: int
    recovery_permille: int
    stamina_min: int
    stamina_max: int
    recovery_min: int
    recovery_max: int
    exact: bool
    source: str
    rounding: str
    already_applied: bool
    observed_matches_baseline: bool | None
    basis: str = "max_stamina"
    application_phase: str = "BeforeAuditionRefresh (5) -> BeforeStepCharacterEvent (6)"

    def to_dict(self):
        return asdict(self)


def audition_start_stamina(current_stamina: int, max_stamina: int, mode_settings, *,
                            observed_before_stamina: int | None = None,
                            observed_after_stamina: int | None = None) -> AuditionStartStamina:
    """Return capped floor/ceil bounds, without waiting on or reading the game.

    ``mode_settings`` accepts ModeSettings, RuntimeModeProfile, its settings
    dict, or the actual ProduceSetting row. Only a supplied server before/after
    pair is ``exact``. Equal baseline bounds mean rounding is irrelevant, not
    that unknown recovery-up/down effects have been observed or modeled.
    """
    current = _integer(current_stamina, "current_stamina")
    maximum = _integer(max_stamina, "max_stamina", minimum=1)
    if current > maximum:
        raise ValueError("current_stamina exceeds max_stamina")
    settings = getattr(mode_settings, "settings", mode_settings)
    if isinstance(settings, Mapping):
        permille = settings.get("before_audition_refresh_permille", settings.get(MASTER_FIELD))
    else:
        permille = getattr(settings, "before_audition_refresh_permille", None)
    permille = _integer(permille, MASTER_FIELD)
    numerator = maximum * permille
    lower_gain, remainder = divmod(numerator, 1000)
    upper_gain = lower_gain + int(remainder != 0)
    lower, upper = min(maximum, current + lower_gain), min(maximum, current + upper_gain)
    if (observed_before_stamina is None) != (observed_after_stamina is None):
        raise ValueError("server recovery observation requires both before and after stamina")
    if observed_before_stamina is not None:
        before = _integer(observed_before_stamina, "observed_before_stamina")
        after = _integer(observed_after_stamina, "observed_after_stamina")
        if before > maximum or after > maximum:
            raise ValueError("observed stamina exceeds the supplied maximum")
        if current not in {before, after}:
            raise ValueError("server recovery pair does not bind current_stamina")
        baseline_lower = min(maximum, before + lower_gain)
        baseline_upper = min(maximum, before + upper_gain)
        return AuditionStartStamina(current, maximum, permille, after, after, after - current, after - current,
            True, "observed server BeforeStamina/AfterStamina", "server-observed",
            current == after, baseline_lower <= after <= baseline_upper)
    return AuditionStartStamina(current, maximum, permille, lower, upper, lower - current, upper - current,
        False, "ProduceSetting." + MASTER_FIELD + "; max-based native-transition inference; modifiers not projected",
        "floor..ceil-unverified" if lower != upper else "baseline-rounding-irrelevant", False, None)
