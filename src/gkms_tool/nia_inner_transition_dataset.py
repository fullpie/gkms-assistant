"""Fail-closed export of complete N.I.A. exam (inner) transitions.

The existing leaderboard and telemetry exports are deliberately useful for
behaviour labels, but they do not normally contain a complete decision
boundary.  In particular, a scalar ``before``/``after`` pair is not enough to
train an inner policy when the legal hand/drink choices were not captured.
This module is a separate gate for the eventual inner dataset.  It never
reconstructs a missing field and it never changes the live controller.

An eligible row must carry all four fields below, explicitly:

``state_before``
    A JSON object describing the settled state immediately before the
    decision.
``legal_candidates``
    The complete, ordered set of actions offered at that boundary.
``action``
    The action actually selected by the player/runner.
``state_after``
    A JSON object describing the settled state after the action completed.

The source adapters intentionally reject today's ``telemetry_episode``
(``full_rl_transition=false``) and the diagnostic leaderboard prefix report
(``exact=false``).  A transition may carry an explicit
``metadata.candidate_set_kind`` such as ``card_only`` or ``drink_only`` when
the state/action/state proof is exact but the unified card+drink+end-turn
candidate surface is not observable.  Such rows remain useful for exact
dynamics and sub-policy audits; the readiness gate excludes them from
unified full-RL policy training.  Only ``candidate_set_kind=unified`` is
policy-ready.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
from typing import Any


INNER_TRANSITION_SCHEMA = "gkms.nia-inner-transition.v1"
INNER_DATASET_MANIFEST_SCHEMA = "gkms.nia-inner-transition-dataset-manifest.v1"
INNER_REJECTION_SCHEMA = "gkms.nia-inner-transition-rejection.v1"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = PROJECT_ROOT / "var" / "nia_training" / "inner_transitions.jsonl"

SOURCE_TELEMETRY_EPISODE = "telemetry_episode"
SOURCE_LEADERBOARD_REPLAY = "leaderboard_replay"
SOURCE_UNKNOWN = "unknown"

# ``NiaInnerTransition`` stores exact dynamics rows.  A row is eligible for
# policy training only when its candidate set is explicitly known to cover
# every action family exposed at that boundary.  Maa's completion sidecar
# currently observes card, drink, and end-turn surfaces in separate phases;
# those rows remain useful for exact state dynamics and sub-policy audits but
# must not be promoted as a unified policy dataset.
CANDIDATE_SET_KIND_UNIFIED = "unified"
CANDIDATE_SET_KIND_CARD_ONLY = "card_only"
CANDIDATE_SET_KIND_DRINK_ONLY = "drink_only"
CANDIDATE_SET_KIND_END_TURN_ONLY = "end_turn_only"
CANDIDATE_SET_KIND_UNKNOWN = "unknown"
CANDIDATE_SET_KINDS = frozenset(
    {
        CANDIDATE_SET_KIND_UNIFIED,
        CANDIDATE_SET_KIND_CARD_ONLY,
        CANDIDATE_SET_KIND_DRINK_ONLY,
        CANDIDATE_SET_KIND_END_TURN_ONLY,
        CANDIDATE_SET_KIND_UNKNOWN,
    }
)

REQUIRED_TRANSITION_FIELDS = (
    "state_before",
    "legal_candidates",
    "action",
    "state_after",
)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant is not allowed: {value}")


def _detach_json(value: object, label: str) -> object:
    """Validate and detach a JSON value without accepting NaN/Infinity."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{label} contains a non-text object key")
            result[key] = _detach_json(child, f"{label}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _detach_json(child, f"{label}[{index}]")
            for index, child in enumerate(value)
        ]
    raise ValueError(f"{label} contains unsupported JSON value {type(value).__name__}")


def _json_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    detached = _detach_json(value, label)
    if not isinstance(detached, dict) or not detached:
        raise ValueError(f"{label} must be a non-empty object")
    return detached


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _json_candidate(value: object, label: str) -> object:
    if isinstance(value, str):
        return _text(value, label)
    if isinstance(value, Mapping):
        return _json_object(value, label)
    raise ValueError(f"{label} must be a non-empty action ID or object")


def _identity(value: object, label: str) -> str:
    """Return a stable action identity for legality checking.

    Native adapter rows commonly use ``action_id`` while telemetry/future
    captures may use a card ``guid`` or plain ``id``.  The small set of
    aliases below is deliberately explicit; unknown object shapes fall back
    to their canonical object digest rather than being guessed into a legal
    action.
    """

    if isinstance(value, str):
        return value
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} has no stable action identity")

    action_id = value.get("action_id")
    if isinstance(action_id, str) and action_id:
        return action_id

    kind = value.get("kind")
    if isinstance(kind, str) and kind:
        if kind in {"play", "card", "use-hand", "use_hand"}:
            guid = value.get("guid", value.get("card_guid"))
            if isinstance(guid, str) and guid:
                slot = value.get(
                    "slot",
                    value.get("hand_slot", value.get("hand_index", value.get("index"))),
                )
                if isinstance(slot, int) and not isinstance(slot, bool) and slot >= 0:
                    return f"PLAY:{guid}:SLOT:{slot}"
                return f"PLAY:{guid}"
        elif kind in {"drink", "use-drink", "use_drink"}:
            slot = value.get("slot", value.get("slot_index"))
            instance = value.get("instance", value.get("instance_id"))
            if (
                isinstance(slot, int)
                and not isinstance(slot, bool)
                and isinstance(instance, str)
                and instance
            ):
                return f"DRINK:{slot}:{instance}"
            drink_id = value.get("drink_id", value.get("id"))
            if (
                isinstance(slot, int)
                and not isinstance(slot, bool)
                and isinstance(drink_id, str)
                and drink_id
            ):
                return f"DRINK:{slot}:ID:{drink_id}"
        elif kind in {"end_turn", "turn-end", "turn_end"}:
            return "END_TURN"

    for key in ("id", "guid", "card_guid", "card_id", "instance_id"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate

    return "json:" + hashlib.sha256(_canonical_json(value)).hexdigest()


def _identities(value: object, label: str) -> frozenset[str]:
    """Return all explicit aliases for a candidate/action.

    The wire shape is not uniform: one producer may emit ``action_id`` while
    another emits ``kind`` + ``guid`` or only ``id``.  Legality checking uses
    the intersection of these aliases, while retaining the canonical digest
    so two otherwise-unknown objects still compare exactly.
    """

    if isinstance(value, str):
        return frozenset({value})
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} has no stable action identity")
    result: set[str] = set()
    action_id = value.get("action_id")
    if isinstance(action_id, str) and action_id:
        result.add(action_id)
    kind = value.get("kind")
    if isinstance(kind, str) and kind:
        if kind in {"play", "card", "use-hand", "use_hand"}:
            guid = value.get("guid", value.get("card_guid"))
            if isinstance(guid, str) and guid:
                slot = value.get(
                    "slot",
                    value.get("hand_slot", value.get("hand_index", value.get("index"))),
                )
                if isinstance(slot, int) and not isinstance(slot, bool) and slot >= 0:
                    result.add(f"PLAY:{guid}:SLOT:{slot}")
                result.add(f"PLAY:{guid}")
        elif kind in {"drink", "use-drink", "use_drink"}:
            slot = value.get("slot", value.get("slot_index"))
            instance = value.get("instance", value.get("instance_id"))
            if (
                isinstance(slot, int)
                and not isinstance(slot, bool)
                and isinstance(instance, str)
                and instance
            ):
                result.add(f"DRINK:{slot}:{instance}")
            drink_id = value.get("drink_id", value.get("id"))
            if (
                isinstance(slot, int)
                and not isinstance(slot, bool)
                and isinstance(drink_id, str)
                and drink_id
            ):
                result.add(f"DRINK:{slot}:ID:{drink_id}")
        elif kind in {"end_turn", "turn-end", "turn_end"}:
            result.add("END_TURN")
    for key in ("id", "guid", "card_guid", "card_id", "instance_id"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            result.add(candidate)
    result.add("json:" + hashlib.sha256(_canonical_json(value)).hexdigest())
    return frozenset(result)


def match_legal_candidate_indices(
    action: object,
    legal_candidates: Sequence[object],
    *,
    selected_candidate_index: int | None = None,
) -> tuple[int, ...]:
    """Match one action to candidates using the dataset's wire-shape aliases.

    This is the shared legality identity contract for validated inner rows and
    downstream canonical labeling.  It deliberately returns every match so a
    caller can fail closed when aliases are ambiguous.
    """

    action_identity = _identity(action, "action")
    candidate_primary = tuple(
        _identity(value, f"legal_candidates[{index}]")
        for index, value in enumerate(legal_candidates)
    )
    primary_matches = tuple(
        index
        for index, identity in enumerate(candidate_primary)
        if identity == action_identity
    )
    if (
        isinstance(selected_candidate_index, int)
        and not isinstance(selected_candidate_index, bool)
        and 0 <= selected_candidate_index < len(legal_candidates)
    ):
        selected_aliases = _identities(
            legal_candidates[selected_candidate_index],
            f"legal_candidates[{selected_candidate_index}]",
        )
        action_aliases = _identities(action, "action")
        return (
            (selected_candidate_index,)
            if action_aliases.intersection(selected_aliases)
            else ()
        )
    if primary_matches:
        return primary_matches
    action_aliases = _identities(action, "action")
    return tuple(
        index
        for index, value in enumerate(legal_candidates)
        if action_aliases.intersection(
            _identities(value, f"legal_candidates[{index}]")
        )
    )


def _normalise_candidates(
    value: object,
    *,
    allow_duplicate_runtime_identity: bool = False,
) -> tuple[object, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("legal_candidates must be a non-empty array")
    if not value:
        raise ValueError("legal_candidates must be a non-empty array")
    result = tuple(
        _json_candidate(item, f"legal_candidates[{index}]")
        for index, item in enumerate(value)
    )
    identities = tuple(
        _identity(item, f"legal_candidates[{index}]")
        for index, item in enumerate(result)
    )
    if len(identities) != len(set(identities)):
        if not allow_duplicate_runtime_identity:
            raise ValueError("legal_candidates must contain unique actions")
        # A managed replay can expose a duplicated runtime GUID/slot while
        # retaining a distinct native candidate object (for example a replay
        # artifact with two copied hand instances).  Preserve the observed
        # entries when their complete JSON payloads differ; exact duplicate
        # objects remain malformed and cannot be selected unambiguously.
        canonical = tuple(_canonical_json(item) for item in result)
        if len(canonical) != len(set(canonical)):
            raise ValueError("legal_candidates must contain unique actions")
    return result


def _normalise_action(value: object) -> object:
    return _json_candidate(value, "action")


@dataclass(frozen=True, slots=True)
class NiaInnerTransition:
    """One complete inner decision boundary accepted by the RL gate."""

    state_before: Mapping[str, object]
    legal_candidates: tuple[object, ...]
    action: object
    state_after: Mapping[str, object]
    source: str = SOURCE_UNKNOWN
    source_id: str = SOURCE_UNKNOWN
    step: int = 0
    reward: int | float | None = None
    terminal: bool = False
    metadata: Mapping[str, object] = field(default_factory=dict)
    schema: str = INNER_TRANSITION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != INNER_TRANSITION_SCHEMA:
            raise ValueError("unsupported N.I.A. inner transition schema")
        _text(self.source, "source")
        _text(self.source_id, "source_id")
        _integer(self.step, "step")
        if not isinstance(self.terminal, bool):
            raise ValueError("terminal must be boolean")
        if isinstance(self.reward, bool) or (
            self.reward is not None and not isinstance(self.reward, (int, float))
        ):
            raise ValueError("reward must be numeric or null")
        if isinstance(self.reward, float) and not math.isfinite(self.reward):
            raise ValueError("reward must be finite")

        before = _json_object(self.state_before, "state_before")
        after = _json_object(self.state_after, "state_after")
        metadata = _detach_json(self.metadata, "metadata")
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        candidates = _normalise_candidates(
            self.legal_candidates,
            allow_duplicate_runtime_identity=(
                metadata.get("allow_duplicate_runtime_identity") is True
            ),
        )
        action = _normalise_action(self.action)
        selected_candidate_index = metadata.get("selected_candidate_index")
        action_matches = match_legal_candidate_indices(
            action,
            candidates,
            selected_candidate_index=(
                selected_candidate_index
                if isinstance(selected_candidate_index, int)
                and not isinstance(selected_candidate_index, bool)
                else None
            ),
        )
        if not action_matches:
            raise ValueError("action is outside the complete legal_candidates set")
        if len(action_matches) != 1:
            raise ValueError("action identity is ambiguous within legal_candidates")
        candidate_set_kind = metadata.get("candidate_set_kind")
        if candidate_set_kind is not None:
            if (
                not isinstance(candidate_set_kind, str)
                or candidate_set_kind not in CANDIDATE_SET_KINDS
            ):
                raise ValueError("metadata.candidate_set_kind is unsupported")
            policy_ready = metadata.get("full_rl_policy_ready")
            if policy_ready is not None and type(policy_ready) is not bool:
                raise ValueError("metadata.full_rl_policy_ready must be boolean")
            if candidate_set_kind != CANDIDATE_SET_KIND_UNIFIED and policy_ready is True:
                raise ValueError(
                    "per-kind candidate sets cannot claim full_rl_policy_ready"
                )

        object.__setattr__(self, "state_before", before)
        object.__setattr__(self, "state_after", after)
        object.__setattr__(self, "legal_candidates", candidates)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "metadata", metadata)

    @property
    def full_rl_transition(self) -> bool:
        return True

    @property
    def candidate_set_kind(self) -> str | None:
        """Return explicit candidate-set provenance, when supplied.

        Older exact exports predate the provenance field and retain their
        historical semantics.  New sidecars must set one of the constants
        above; per-kind values are exact dynamics rows, not unified-policy
        rows.
        """

        value = self.metadata.get("candidate_set_kind")
        return value if isinstance(value, str) else None

    @property
    def full_rl_policy_ready(self) -> bool:
        """Whether this row may participate in unified full-RL policy data."""

        kind = self.candidate_set_kind
        if kind is None:
            # A legacy row may still be an exact action/dynamics transition,
            # but the absence of candidate-set provenance cannot prove that
            # cards, drinks and end-turn were offered together.  Keep it out
            # of unified policy readiness instead of inheriting an old,
            # unverifiable completeness assumption.
            return False
        return (
            kind == CANDIDATE_SET_KIND_UNIFIED
            and self.metadata.get("full_rl_policy_ready", True) is True
        )

    @property
    def behavior_only(self) -> bool:
        return False

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "source": self.source,
            "source_id": self.source_id,
            "step": self.step,
            "state_before": dict(self.state_before),
            "legal_candidates": list(self.legal_candidates),
            "action": self.action,
            "state_after": dict(self.state_after),
            "reward": self.reward,
            "terminal": self.terminal,
            "metadata": dict(self.metadata),
            "behavior_only": False,
            "full_rl_transition": True,
        }

    @classmethod
    def from_dict(cls, value: object) -> "NiaInnerTransition":
        if not isinstance(value, Mapping):
            raise ValueError("inner transition must be an object")
        required = {
            "schema",
            "source",
            "source_id",
            "step",
            "state_before",
            "legal_candidates",
            "action",
            "state_after",
            "reward",
            "terminal",
            "metadata",
            "behavior_only",
            "full_rl_transition",
        }
        if set(value) != required:
            raise ValueError("inner transition has an unsupported shape")
        if value.get("behavior_only") is not False:
            raise ValueError("inner transition cannot be behavior-only")
        if value.get("full_rl_transition") is not True:
            raise ValueError("inner transition is not marked full RL")
        raw_candidates = value.get("legal_candidates")
        if not isinstance(raw_candidates, Sequence) or isinstance(
            raw_candidates, (str, bytes, bytearray)
        ):
            raise ValueError("legal_candidates must be an array")
        return cls(
            schema=value.get("schema"),  # type: ignore[arg-type]
            source=value.get("source"),  # type: ignore[arg-type]
            source_id=value.get("source_id"),  # type: ignore[arg-type]
            step=value.get("step"),  # type: ignore[arg-type]
            state_before=value.get("state_before"),  # type: ignore[arg-type]
            legal_candidates=tuple(raw_candidates),
            action=value.get("action"),
            state_after=value.get("state_after"),  # type: ignore[arg-type]
            reward=value.get("reward"),  # type: ignore[arg-type]
            terminal=value.get("terminal"),  # type: ignore[arg-type]
            metadata=value.get("metadata"),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class NiaInnerTransitionRejection:
    """Auditable reason why a candidate row was not promoted."""

    source: str
    source_id: str
    step: int
    reasons: tuple[str, ...]
    missing_fields: tuple[str, ...] = ()
    schema: str = INNER_REJECTION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != INNER_REJECTION_SCHEMA:
            raise ValueError("unsupported inner transition rejection schema")
        _text(self.source, "rejection.source")
        _text(self.source_id, "rejection.source_id")
        _integer(self.step, "rejection.step")
        reasons = tuple(self.reasons)
        missing = tuple(self.missing_fields)
        if not reasons or any(not isinstance(value, str) or not value for value in reasons):
            raise ValueError("rejection reasons must be non-empty text")
        if any(not isinstance(value, str) or not value for value in missing):
            raise ValueError("rejection missing_fields must be non-empty text")
        object.__setattr__(self, "reasons", tuple(dict.fromkeys(reasons)))
        object.__setattr__(self, "missing_fields", tuple(dict.fromkeys(missing)))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "source": self.source,
            "source_id": self.source_id,
            "step": self.step,
            "reasons": list(self.reasons),
            "missing_fields": list(self.missing_fields),
        }


@dataclass(frozen=True, slots=True)
class NiaInnerTransitionCollection:
    """Accepted rows plus fail-closed diagnostics from one source batch."""

    transitions: tuple[NiaInnerTransition, ...] = ()
    rejections: tuple[NiaInnerTransitionRejection, ...] = ()

    def __post_init__(self) -> None:
        transitions = tuple(self.transitions)
        rejections = tuple(self.rejections)
        if any(not isinstance(value, NiaInnerTransition) for value in transitions):
            raise TypeError("transitions must contain NiaInnerTransition values")
        if any(not isinstance(value, NiaInnerTransitionRejection) for value in rejections):
            raise TypeError("rejections must contain NiaInnerTransitionRejection values")
        keys = [(value.source, value.source_id, value.step) for value in transitions]
        if len(keys) != len(set(keys)):
            raise ValueError("inner transition boundaries must be unique")
        object.__setattr__(self, "transitions", transitions)
        object.__setattr__(self, "rejections", rejections)

    @property
    def full_rl_transition_count(self) -> int:
        return len(self.transitions)

    @property
    def full_rl_policy_ready_count(self) -> int:
        return sum(value.full_rl_policy_ready for value in self.transitions)

    @property
    def accepted(self) -> tuple[NiaInnerTransition, ...]:
        return self.transitions

    @property
    def rejected(self) -> tuple[NiaInnerTransitionRejection, ...]:
        return self.rejections


# Concise aliases used by callers that call the collection an export/result.
NiaInnerTransitionExport = NiaInnerTransitionCollection
InnerTransition = NiaInnerTransition
InnerTransitionRejection = NiaInnerTransitionRejection


def _source_values(
    row: Mapping[str, object],
    *,
    source: str,
    source_id: str,
    step: int,
) -> tuple[str, str, int]:
    raw_source = row.get("source", source)
    raw_source_id = row.get("source_id", source_id)
    raw_step = row.get("step", step)
    return (
        raw_source if isinstance(raw_source, str) and raw_source.strip() else source,
        raw_source_id
        if isinstance(raw_source_id, str) and raw_source_id.strip()
        else source_id,
        raw_step if isinstance(raw_step, int) and not isinstance(raw_step, bool) and raw_step >= 0 else step,
    )


def _rejection(
    *,
    source: str,
    source_id: str,
    step: int,
    reasons: Iterable[str],
    missing_fields: Iterable[str] = (),
) -> NiaInnerTransitionRejection:
    return NiaInnerTransitionRejection(
        source=source or SOURCE_UNKNOWN,
        source_id=source_id or SOURCE_UNKNOWN,
        step=step if isinstance(step, int) and not isinstance(step, bool) and step >= 0 else 0,
        reasons=tuple(reasons),
        missing_fields=tuple(missing_fields),
    )


def try_build_nia_inner_transition(
    row: object,
    *,
    source: str = SOURCE_UNKNOWN,
    source_id: str = SOURCE_UNKNOWN,
    step: int = 0,
) -> tuple[NiaInnerTransition | None, NiaInnerTransitionRejection | None]:
    """Build one row, returning a structured rejection instead of guessing.

    This is the preferred boundary for ingestion code.  Callers that need a
    hard error can use :class:`NiaInnerTransition` directly or
    :func:`require_nia_inner_transition`.
    """

    if not isinstance(row, Mapping):
        return None, _rejection(
            source=source,
            source_id=source_id,
            step=step,
            reasons=("row-is-not-an-object",),
        )

    resolved_source, resolved_id, resolved_step = _source_values(
        row, source=source, source_id=source_id, step=step
    )
    missing = tuple(
        name for name in REQUIRED_TRANSITION_FIELDS if name not in row or row.get(name) is None
    )
    reasons: list[str] = []
    if row.get("full_rl_transition") is False:
        reasons.append("declared-full-rl-transition-false")
    if row.get("behavior_only") is True:
        reasons.append("declared-behavior-only")
    if missing:
        reasons.append("missing-required-fields")
    if reasons:
        return None, _rejection(
            source=resolved_source,
            source_id=resolved_id,
            step=resolved_step,
            reasons=reasons,
            missing_fields=missing,
        )

    try:
        transition = NiaInnerTransition(
            state_before=row["state_before"],  # type: ignore[arg-type]
            legal_candidates=row["legal_candidates"],  # type: ignore[arg-type]
            action=row["action"],
            state_after=row["state_after"],  # type: ignore[arg-type]
            source=resolved_source,
            source_id=resolved_id,
            step=resolved_step,
            reward=row.get("reward"),  # type: ignore[arg-type]
            terminal=row.get("terminal", False),  # type: ignore[arg-type]
            metadata=row.get("metadata", {}),  # type: ignore[arg-type]
        )
    except (TypeError, ValueError, KeyError) as error:
        return None, _rejection(
            source=resolved_source,
            source_id=resolved_id,
            step=resolved_step,
            reasons=("invalid-complete-transition:" + str(error),),
        )
    return transition, None


def require_nia_inner_transition(row: object, **kwargs: object) -> NiaInnerTransition:
    transition, rejection = try_build_nia_inner_transition(row, **kwargs)  # type: ignore[arg-type]
    if transition is None:
        assert rejection is not None
        detail = ";".join(rejection.reasons)
        if rejection.missing_fields:
            detail += ":missing=" + ",".join(rejection.missing_fields)
        raise ValueError(detail)
    return transition


def collect_nia_inner_transitions(
    rows: Iterable[object],
    *,
    source: str = SOURCE_UNKNOWN,
    source_id: str = SOURCE_UNKNOWN,
) -> NiaInnerTransitionCollection:
    """Collect rows while preserving every rejection for auditability."""

    accepted: list[NiaInnerTransition] = []
    rejected: list[NiaInnerTransitionRejection] = []
    seen: set[tuple[str, str, int]] = set()
    for index, row in enumerate(rows):
        if isinstance(row, NiaInnerTransition):
            key = (row.source, row.source_id, row.step)
            if key in seen:
                rejected.append(
                    _rejection(
                        source=row.source,
                        source_id=row.source_id,
                        step=row.step,
                        reasons=("duplicate-transition-boundary",),
                    )
                )
            else:
                seen.add(key)
                accepted.append(row)
            continue
        row_source = source
        row_id = source_id
        row_step = index
        if isinstance(row, Mapping):
            row_source, row_id, row_step = _source_values(
                row, source=source, source_id=source_id, step=index
            )
        transition, rejection = try_build_nia_inner_transition(
            row,
            source=row_source,
            source_id=row_id,
            step=row_step,
        )
        if transition is None:
            assert rejection is not None
            rejected.append(rejection)
            continue
        key = (transition.source, transition.source_id, transition.step)
        if key in seen:
            rejected.append(
                _rejection(
                    source=transition.source,
                    source_id=transition.source_id,
                    step=transition.step,
                    reasons=("duplicate-transition-boundary",),
                )
            )
            continue
        seen.add(key)
        accepted.append(transition)
    return NiaInnerTransitionCollection(tuple(accepted), tuple(rejected))


def _payload(source: object) -> Mapping[str, object] | list[object]:
    if isinstance(source, Mapping):
        return source
    to_dict = getattr(source, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        if isinstance(value, Mapping):
            return value
        raise ValueError("source.to_dict() must return an object")
    if isinstance(source, (str, Path)):
        path = Path(source)
        raw = path.read_text(encoding="utf-8-sig")
        if not raw.strip():
            raise ValueError(f"inner transition source is empty: {path}")
        try:
            decoded = json.loads(raw, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as error:
            rows = []
            for line_number, line in enumerate(raw.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line, parse_constant=_reject_json_constant))
                except (json.JSONDecodeError, ValueError) as line_error:
                    raise ValueError(
                        f"invalid transition JSON at line {line_number}: {line_error}"
                    ) from error
            decoded = rows
        if isinstance(decoded, Mapping):
            return decoded
        if isinstance(decoded, list):
            return decoded
        raise ValueError("inner transition source must be an object or array")
    raise TypeError("inner transition source must be an object, path, or report")


def _raw_rows(value: Mapping[str, object] | list[object], *names: str) -> list[object]:
    if isinstance(value, list):
        return list(value)
    for name in names:
        rows = value.get(name)
        if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray)):
            return list(rows)
    return []


def _append_source_gate_rejection(
    row: object,
    rejection: NiaInnerTransitionRejection | None,
    *,
    source: str,
    source_id: str,
    step: int,
    reason: str,
) -> NiaInnerTransitionRejection:
    missing = () if rejection is None else rejection.missing_fields
    reasons = () if rejection is None else rejection.reasons
    return _rejection(
        source=source,
        source_id=source_id,
        step=step,
        reasons=(*reasons, reason),
        missing_fields=missing,
    )


def _finalise_source_collection(
    accepted: Sequence[NiaInnerTransition],
    rejected: Sequence[NiaInnerTransitionRejection],
) -> NiaInnerTransitionCollection:
    """Deduplicate adapter rows without allowing collection construction to abort."""

    rows: list[NiaInnerTransition] = []
    rejections = list(rejected)
    seen: set[tuple[str, str, int]] = set()
    for value in accepted:
        key = (value.source, value.source_id, value.step)
        if key in seen:
            rejections.append(
                _rejection(
                    source=value.source,
                    source_id=value.source_id,
                    step=value.step,
                    reasons=("duplicate-transition-boundary",),
                )
            )
            continue
        seen.add(key)
        rows.append(value)
    return NiaInnerTransitionCollection(tuple(rows), tuple(rejections))


def collect_telemetry_episode_inner_transitions(
    source: object,
) -> NiaInnerTransitionCollection:
    """Read only explicitly complete rows from a telemetry episode payload."""

    value = _payload(source)
    if isinstance(value, list):
        payload: Mapping[str, object] = {}
        rows = value
    else:
        payload = value
        rows = _raw_rows(
            payload,
            "inner_transitions",
            "authoritative_manual_card_actions",
            "transitions",
        )
    run_id = payload.get("run_id") if isinstance(payload, Mapping) else None
    source_id = run_id if isinstance(run_id, str) and run_id else "telemetry-source"
    gate = payload.get("training_eligibility") if isinstance(payload, Mapping) else None
    gate_disabled = isinstance(gate, Mapping) and gate.get("full_rl_transition") is False

    accepted: list[NiaInnerTransition] = []
    rejected: list[NiaInnerTransitionRejection] = []
    for index, row in enumerate(rows):
        row_step = index
        if isinstance(row, Mapping):
            raw_step = row.get("execute_sequence", row.get("sequence", row.get("step", index)))
            if isinstance(raw_step, int) and not isinstance(raw_step, bool) and raw_step >= 0:
                row_step = raw_step
        # Provenance belongs to the adapter, not to an untrusted nested row.
        # Keep the row's transition fields/metadata but bind source identity
        # to the file/report being read.
        candidate_row = (
            {**row, "source": SOURCE_TELEMETRY_EPISODE, "source_id": source_id, "step": row_step}
            if isinstance(row, Mapping)
            else row
        )
        transition, rejection = try_build_nia_inner_transition(
            candidate_row,
            source=SOURCE_TELEMETRY_EPISODE,
            source_id=source_id,
            step=row_step,
        )
        if gate_disabled:
            rejected.append(
                _append_source_gate_rejection(
                    row,
                    rejection,
                    source=SOURCE_TELEMETRY_EPISODE,
                    source_id=source_id,
                    step=row_step,
                    reason="episode-full-rl-transition-disabled",
                )
            )
        elif transition is None:
            assert rejection is not None
            rejected.append(rejection)
        else:
            accepted.append(transition)
    return _finalise_source_collection(accepted, rejected)


def collect_leaderboard_replay_inner_transitions(
    source: object,
) -> NiaInnerTransitionCollection:
    """Read explicitly complete rows from a replay report.

    ``leaderboard_replay_adapter`` currently emits a diagnostic report with
    ``exact=false`` and scalar-only boundaries.  Both conditions remain
    explicit rejection reasons here; no native state or legal set is inferred
    from the report.
    """

    value = _payload(source)
    if isinstance(value, list):
        payload: Mapping[str, object] = {}
        rows = value
    else:
        payload = value
        rows = _raw_rows(payload, "inner_transitions", "steps", "transitions")
    report_exact = payload.get("exact") is True
    episode = payload.get("episode")
    trajectory = (
        episode.get("trajectory_id")
        if isinstance(episode, Mapping)
        else payload.get("trajectory_id")
    )
    source_id = trajectory if isinstance(trajectory, str) and trajectory else "leaderboard-source"

    accepted: list[NiaInnerTransition] = []
    rejected: list[NiaInnerTransitionRejection] = []
    for index, row in enumerate(rows):
        row_step = index
        if isinstance(row, Mapping):
            raw_step = row.get("order", row.get("step", index))
            if isinstance(raw_step, int) and not isinstance(raw_step, bool) and raw_step >= 0:
                row_step = raw_step
        candidate_row = (
            {**row, "source": SOURCE_LEADERBOARD_REPLAY, "source_id": source_id, "step": row_step}
            if isinstance(row, Mapping)
            else row
        )
        transition, rejection = try_build_nia_inner_transition(
            candidate_row,
            source=SOURCE_LEADERBOARD_REPLAY,
            source_id=source_id,
            step=row_step,
        )
        if not report_exact:
            rejected.append(
                _append_source_gate_rejection(
                    row,
                    rejection,
                    source=SOURCE_LEADERBOARD_REPLAY,
                    source_id=source_id,
                    step=row_step,
                    reason="leaderboard-replay-not-exact",
                )
            )
        elif transition is None:
            assert rejection is not None
            rejected.append(rejection)
        else:
            accepted.append(transition)
    return _finalise_source_collection(accepted, rejected)


def _manifest_for(
    payload: bytes,
    transitions: Sequence[NiaInnerTransition],
    rejections: Sequence[NiaInnerTransitionRejection],
    output: Path,
) -> dict[str, object]:
    by_reason = Counter(
        reason
        for rejection in rejections
        for reason in rejection.reasons
    )
    by_source = Counter(value.source for value in transitions)
    by_candidate_set_kind = Counter(
        value.candidate_set_kind or "legacy"
        for value in transitions
    )
    return {
        "schema": INNER_DATASET_MANIFEST_SCHEMA,
        "output_path": str(output.resolve()),
        "output_sha256": hashlib.sha256(payload).hexdigest(),
        "transition_count": len(transitions),
        "full_rl_transition_count": len(transitions),
        "full_rl_policy_ready_count": sum(
            value.full_rl_policy_ready for value in transitions
        ),
        "candidate_set_kind_counts": dict(sorted(by_candidate_set_kind.items())),
        "rejection_count": len(rejections),
        "source_count": len({(value.source, value.source_id) for value in transitions}),
        "by_source": dict(sorted(by_source.items())),
        "rejections_by_reason": dict(sorted(by_reason.items())),
    }


def write_nia_inner_transition_export(
    output: str | Path,
    export: NiaInnerTransitionCollection,
    *,
    allow_empty: bool = True,
) -> dict[str, object]:
    """Write accepted rows and an auditable rejection manifest.

    ``allow_empty`` defaults to true so a blocked capture can produce a
    manifest documenting why no RL rows were promoted.  The manifest never
    turns those rejections into training data.
    """

    if not isinstance(export, NiaInnerTransitionCollection):
        raise TypeError("export must be NiaInnerTransitionCollection")
    if not allow_empty and not export.transitions:
        raise ValueError("inner transition export has no eligible transitions")
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = b"".join(
        _canonical_json(value.to_dict()) + b"\n" for value in export.transitions
    )
    target.write_bytes(payload)
    manifest = _manifest_for(
        payload,
        export.transitions,
        export.rejections,
        target,
    )
    manifest_path = target.with_suffix(".manifest.json")
    manifest["manifest_path"] = str(manifest_path.resolve())
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def write_nia_inner_transition_dataset(
    output: str | Path = DEFAULT_OUTPUT,
    *,
    transitions: Sequence[NiaInnerTransition],
) -> dict[str, object]:
    """Write only already-validated transitions (hard fail on empty input)."""

    values = tuple(transitions)
    if not values:
        raise ValueError("N.I.A. inner transition dataset has no eligible transitions")
    if any(not isinstance(value, NiaInnerTransition) for value in values):
        raise TypeError("transitions must contain NiaInnerTransition values")
    return write_nia_inner_transition_export(
        output,
        NiaInnerTransitionCollection(values),
        allow_empty=False,
    )


def load_nia_inner_transition_dataset(
    source: str | Path = DEFAULT_OUTPUT,
) -> tuple[NiaInnerTransition, ...]:
    path = Path(source)
    rows: list[NiaInnerTransition] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line, parse_constant=_reject_json_constant)
            rows.append(NiaInnerTransition.from_dict(raw))
        except (json.JSONDecodeError, ValueError, TypeError) as error:
            raise ValueError(f"invalid inner transition JSONL line {line_number}") from error
    if not rows:
        raise ValueError("N.I.A. inner transition dataset is empty")
    # Reuse the same boundary uniqueness check as collection/export.
    NiaInnerTransitionCollection(tuple(rows))
    return tuple(rows)


# Friendly aliases for callers using "export"/"build" terminology.
build_nia_inner_transition = require_nia_inner_transition
collect_inner_transitions = collect_nia_inner_transitions
export_telemetry_episode_inner_transitions = collect_telemetry_episode_inner_transitions
export_leaderboard_replay_inner_transitions = collect_leaderboard_replay_inner_transitions


__all__ = [
    "CANDIDATE_SET_KIND_CARD_ONLY",
    "CANDIDATE_SET_KIND_DRINK_ONLY",
    "CANDIDATE_SET_KIND_END_TURN_ONLY",
    "CANDIDATE_SET_KIND_UNKNOWN",
    "CANDIDATE_SET_KIND_UNIFIED",
    "CANDIDATE_SET_KINDS",
    "DEFAULT_OUTPUT",
    "INNER_DATASET_MANIFEST_SCHEMA",
    "INNER_REJECTION_SCHEMA",
    "INNER_TRANSITION_SCHEMA",
    "REQUIRED_TRANSITION_FIELDS",
    "SOURCE_LEADERBOARD_REPLAY",
    "SOURCE_TELEMETRY_EPISODE",
    "InnerTransition",
    "InnerTransitionRejection",
    "NiaInnerTransition",
    "NiaInnerTransitionCollection",
    "NiaInnerTransitionExport",
    "NiaInnerTransitionRejection",
    "build_nia_inner_transition",
    "collect_inner_transitions",
    "collect_leaderboard_replay_inner_transitions",
    "collect_nia_inner_transitions",
    "collect_telemetry_episode_inner_transitions",
    "export_leaderboard_replay_inner_transitions",
    "export_telemetry_episode_inner_transitions",
    "load_nia_inner_transition_dataset",
    "match_legal_candidate_indices",
    "require_nia_inner_transition",
    "try_build_nia_inner_transition",
    "write_nia_inner_transition_dataset",
    "write_nia_inner_transition_export",
]
