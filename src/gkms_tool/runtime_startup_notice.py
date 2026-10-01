"""One normal startup-ad close, bound to its observed native owner.

This is independent of mode/week. Download sheets, resource confirmations and
advertisement links are not dismissible through this action family.
"""
from collections.abc import Mapping


STARTUP_SCREEN = "StartupImageOverlayPresenter"
DISMISS_ACTION = "notice.dismiss_startup_image"


def startup_notice_target(before, target):
    return (isinstance(target, Mapping) and before.get("screen_type") == STARTUP_SCREEN
        and target.get("action_id") == DISMISS_ACTION and target.get("owner_type") == STARTUP_SCREEN
        and target.get("button_source") == "close"
        and isinstance(before.get("screen_instance_id"), str) and bool(before["screen_instance_id"])
        and target.get("screen_instance_id") == before["screen_instance_id"]
        and all(isinstance(target.get(name), str) and target[name] not in ("", "0x0", "0")
            for name in ("owner_instance_id", "button_instance_id", "callback_instance_id", "close_callback_instance_id")))


def startup_notice_advanced(before, after, target):
    """A close animation/button flag is not completion of its original owner."""
    if not startup_notice_target(before, target) or after.get("busy") is not False:
        return False
    screen, identity = after.get("screen_type"), after.get("screen_instance_id")
    if not isinstance(screen, str) or not screen or not isinstance(identity, str) or not identity:
        return False
    return screen != before["screen_type"] or identity != before["screen_instance_id"]
