"""Derive a replayable audition-entry context from the static route calendar.

The contextual passive engine intentionally does not infer where an audition
occurs.  This adapter turns a run identity plus an exact audition stage into
the same zero-based route position used by the persisted shadow evidence.
"""

from __future__ import annotations

import re

from .audition_rules import FINAL, MID1, MID2
from .contextual_passive_step import ContextualPassiveStepContext
from .route_calendar import load_route_calendar
from .run_identity import RunIdentity


EXAM_STAGES = (MID1, MID2, FINAL)
_STAGE_SLUGS = {
    MID1: "mid1",
    MID2: "mid2",
    FINAL: "final",
}
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class LiveExamContextError(ValueError):
    """The static route cannot prove one exact audition-entry context."""


def derive_live_exam_entry_context(
    identity: RunIdentity,
    *,
    snapshot_digest: str,
    step_type: str,
    stage_number: int,
    expected_context_digest: str | None = None,
) -> ContextualPassiveStepContext:
    """Return the unique entry context for an audition milestone.

    ``route_position`` is the zero-based index immediately before the
    milestone.  For Initial Regular Mid1 this is 5 because the audition is
    route week 6, matching the already captured live evidence.
    """

    if not isinstance(identity, RunIdentity):
        raise TypeError("identity must be RunIdentity")
    identity.validate()
    if _SHA256_PATTERN.fullmatch(snapshot_digest) is None:
        raise LiveExamContextError(
            "snapshot_digest must be a lowercase SHA-256 digest"
        )
    if step_type not in _STAGE_SLUGS:
        raise LiveExamContextError(f"unsupported audition stage: {step_type}")
    if (
        not isinstance(stage_number, int)
        or isinstance(stage_number, bool)
        or stage_number < 1
    ):
        raise LiveExamContextError("stage_number must be an integer >= 1")

    calendar = load_route_calendar(
        identity.produce_id,
        character_id=identity.character_id,
    )
    matches = tuple(
        milestone
        for milestone in calendar.milestones
        if milestone.step_type == step_type
    )
    if len(matches) != 1:
        raise LiveExamContextError(
            "static route must contain exactly one matching audition "
            f"milestone; found {len(matches)} for {step_type}"
        )
    milestone = matches[0]
    earlier = tuple(
        item.step_type
        for item in sorted(calendar.milestones, key=lambda value: value.week)
        if item.week < milestone.week and item.step_type in EXAM_STAGES
    )
    if len(earlier) != len(set(earlier)):
        raise LiveExamContextError(
            "static route repeats an earlier audition stage"
        )
    context = ContextualPassiveStepContext(
        run_id=identity.run_id,
        snapshot_digest=snapshot_digest,
        produce_id=identity.produce_id,
        context_id=(
            f"{identity.produce_id}:audition-"
            f"{_STAGE_SLUGS[step_type]}:{stage_number}:entry"
        ),
        stage=step_type,
        turn=1,
        route_position=milestone.week - 1,
        passed_audition_stages=earlier,
    )
    if expected_context_digest is not None:
        if _SHA256_PATTERN.fullmatch(expected_context_digest) is None:
            raise LiveExamContextError(
                "expected_context_digest must be a lowercase SHA-256 digest"
            )
        if context.digest != expected_context_digest:
            raise LiveExamContextError(
                "derived audition context does not match persisted evidence: "
                f"{context.digest} != {expected_context_digest}"
            )
    return context


__all__ = [
    "EXAM_STAGES",
    "LiveExamContextError",
    "derive_live_exam_entry_context",
]
