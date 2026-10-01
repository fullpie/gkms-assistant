"""Observe the game's current LoginBonus WaitClickAsync phase."""
from collections.abc import Mapping

SCHEMA = "gkms.login-touch.v1"
LOGIN_SCREENS = frozenset({"DailyLoginBonusScreenPresenter",
    "EventSimpleLoginBonusScreenPresenter", "EventSpecialLoginBonusScreenPresenter"})


def _touch(raw):
    ui = raw.get("ui_state")
    touch = ui.get("login_touch") if isinstance(ui, Mapping) else None
    return touch if isinstance(touch, Mapping) and touch.get("schema") == SCHEMA else None


def _identity(row):
    names = ("closure_instance_id", "completion_source_instance_id")
    if not isinstance(row, Mapping) or any(not isinstance(row.get(k), str) or not row[k] for k in names):
        return None
    return tuple(row[k] for k in names)


def _waiters(touch):
    rows = touch.get("waiters")
    if not isinstance(rows, list):
        return None
    result = {}
    for row in rows:
        key = _identity(row)
        if (key is None or key in result or row.get("source") != "CampusButtonBase.WaitClickAsync"
                or type(row.get("status")) is not int or row["status"] not in (0, 1, 2, 3)):
            return None
        result[key] = row["status"]
    return result


def login_touch_advanced(before, after, target):
    """Require a bound waiter to finish/leave, or the owning screen to change.

    A legacy snapshot with no waiter identity cannot gain completion evidence
    merely by adding the new schema. Readiness/animation flags are not progress.
    """
    if before.get("screen_type") not in LOGIN_SCREENS:
        return False
    if after.get("screen_type") != before.get("screen_type"):
        return True
    previous_owner, current_owner = before.get("screen_instance_id"), after.get("screen_instance_id")
    if previous_owner and current_owner and previous_owner != current_owner:
        return True
    first, current = _touch(before), _touch(after)
    if first is None or current is None:
        return False
    for name in ("owner_instance_id", "button_instance_id"):
        value = first.get(name)
        if not isinstance(value, str) or not value or target.get(name) != value or current.get(name) != value:
            return False
    initial, latest = _waiters(first), _waiters(current)
    rows = target.get("pending_waiters")
    if initial is None or latest is None or not isinstance(rows, list) or not rows:
        return False
    expected = [_identity(row) for row in rows]
    if (None in expected or len(set(expected)) != len(expected)
            or set(expected) != {key for key, status in initial.items() if status == 0}):
        return False
    return all(key not in latest or latest[key] == 1 for key in expected)


def waiting_for_login_touch(raw):
    touch = _touch(raw)
    return (raw.get("screen_type") in LOGIN_SCREENS and touch is not None
            and touch.get("ready") is False and _waiters(touch) is not None
            and not raw.get("legal_actions"))
