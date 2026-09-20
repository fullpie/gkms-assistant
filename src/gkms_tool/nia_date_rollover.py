"""Strict evidence gate for a N.I.A. resume across a date update.

The live runner has no game-owned ``date changed`` field.  A timestamp gap
therefore remains only a diagnostic.  This module accepts a resume as a
zero-intervention continuation only when an external observer supplied an
explicit date-update event and the run/LocalSave/input bindings agree.

The validator is intentionally independent of the game and Maa controller;
callers must provide the captured evidence.  Missing or partial evidence is
always rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
from typing import Any, Mapping


SCHEMA = "gkms.nia-date-rollover-resume.v1"
KIND = "date-rollover-resume"
REASON = "date-update"


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _iso_date(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        # Date-only values are the preferred wire representation.  Accept an
        # ISO timestamp as a convenience for observers that already record it.
        if len(value) == 10:
            return date.fromisoformat(value)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().date()
    except (TypeError, ValueError, OverflowError):
        return None


def _non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True, slots=True)
class NiaDateRolloverResumeCheck:
    """Result of validating one explicit date-update resume envelope."""

    accepted: bool
    reason: str
    schema: str = SCHEMA
    kind: str = KIND

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "kind": self.kind,
            "accepted": self.accepted,
            "reason": self.reason,
        }


def _reject(reason: str) -> NiaDateRolloverResumeCheck:
    return NiaDateRolloverResumeCheck(False, reason)


def evaluate_date_rollover_resume(
    evidence: object,
    *,
    run_id: object,
    mode: object,
    idol_card_id: object,
    checkpoint_week: object,
    current_week: object,
    checkpoint_local_save_digest: object,
    current_local_save_digest: object,
    controller_pid: object,
    controller_hwnd: object,
) -> NiaDateRolloverResumeCheck:
    """Validate explicit evidence for a zero-input date-rollover resume.

    ``evidence`` must contain the following observer-owned fields:

    * ``schema``/``kind`` and ``interruption_reason == "date-update"``;
    * exact run/mode/idol identity;
    * different observed local dates;
    * a zero-delta controller/game input audit;
    * identical before/after week and LocalSave digest; and
    * unchanged controller PID/HWND.

    The live runner supplies the actual checkpoint/current values through the
    keyword arguments, so copying stale values into the evidence object cannot
    make a mismatched LocalSave resume pass.
    """

    if not isinstance(evidence, Mapping):
        return _reject("evidence-missing")
    if evidence.get("schema") != SCHEMA or evidence.get("kind") != KIND:
        return _reject("evidence-schema-mismatch")
    if evidence.get("interruption_reason") != REASON:
        return _reject("interruption-reason-not-date-update")
    if evidence.get("explicit_date_update") is not True:
        return _reject("explicit-date-update-proof-missing")

    if not all(_is_text(value) for value in (run_id, mode, idol_card_id)):
        return _reject("runtime-identity-missing")
    if any(evidence.get(name) != expected for name, expected in (
        ("run_id", run_id),
        ("mode", mode),
        ("idol_card_id", idol_card_id),
    )):
        return _reject("run-identity-mismatch")

    before_date = _iso_date(evidence.get("date_before"))
    after_date = _iso_date(evidence.get("date_after"))
    if before_date is None or after_date is None or before_date == after_date:
        return _reject("date-boundary-not-proven")

    if not _non_negative_int(checkpoint_week) or not _non_negative_int(current_week):
        return _reject("week-evidence-missing")
    if checkpoint_week != current_week:
        return _reject("week-changed-across-resume")
    if evidence.get("week_before") != checkpoint_week or evidence.get("week_after") != current_week:
        return _reject("week-evidence-mismatch")

    if not _is_sha256(checkpoint_local_save_digest) or not _is_sha256(current_local_save_digest):
        return _reject("localsave-digest-missing")
    if checkpoint_local_save_digest != current_local_save_digest:
        return _reject("localsave-changed-across-resume")
    if evidence.get("localsave_digest_before") != checkpoint_local_save_digest or evidence.get("localsave_digest_after") != current_local_save_digest:
        return _reject("localsave-evidence-mismatch")

    audit = evidence.get("input_audit")
    if not isinstance(audit, Mapping):
        return _reject("input-audit-missing")
    if audit.get("source") != "maa-input-ledger":
        return _reject("input-audit-source-unverified")
    if audit.get("controller_input_delta") != 0 or audit.get("game_input_delta") != 0:
        return _reject("input-observed-across-resume")

    if not _non_negative_int(controller_pid) or not _non_negative_int(controller_hwnd):
        return _reject("controller-binding-missing")
    if controller_pid <= 0 or controller_hwnd <= 0:
        return _reject("controller-binding-invalid")
    if evidence.get("controller_pid_before") != controller_pid or evidence.get("controller_pid_after") != controller_pid:
        return _reject("controller-pid-changed")
    if evidence.get("controller_hwnd_before") != controller_hwnd or evidence.get("controller_hwnd_after") != controller_hwnd:
        return _reject("controller-hwnd-changed")

    return NiaDateRolloverResumeCheck(True, "date-rollover-evidence-accepted")


def localsave_binding_digest(value: Mapping[str, Any]) -> str | None:
    """Hash the stable outer LocalSave authority projection.

    This deliberately excludes timestamps and diagnostics.  It includes the
    log cursor and all scalar state fields used by the weekly CAS, making a
    date-only resume comparable without pretending that a screenshot is proof.
    """

    fields = (
        "log_count",
        "week",
        "last_completed_week",
        "stamina",
        "max_stamina",
        "produce_points",
        "vocal",
        "dance",
        "visual",
        "vote_count",
    )
    if any(field not in value for field in fields):
        return None
    payload = {field: value[field] for field in fields}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "KIND",
    "REASON",
    "SCHEMA",
    "NiaDateRolloverResumeCheck",
    "evaluate_date_rollover_resume",
    "localsave_binding_digest",
]
