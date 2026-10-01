"""Project-local checkpoint for one screenshot-verified audition stage.

An audition checkpoint ending at zero turns means only that Mid/Final stage is
complete.  It must never be interpreted as completion of the whole produce run.

Schema 3 binds a live checkpoint to the exact preflight artifacts that produced
its first-turn state.  Schemas 1 and 2 remain readable for display and stage
routing, but are deliberately ineligible for live automatic execution.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .logic_engine import LogicExamState


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SESSION_PATH = PROJECT_ROOT / "var" / "exam_session.json"
SESSION_SCHEMA_VERSION = 3

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_LEGACY_SCHEMA_VERSIONS = frozenset({1, 2})
_BASE_FIELDS = frozenset(
    {
        "schema_version",
        "logic_state",
        "idol_card_id",
        "produce_id",
        "step_type",
        "stage_number",
    }
)
_RUN_IDENTITY_FIELDS = frozenset({"run_id", "character_id"})
_PREFLIGHT_BINDING_FIELDS = frozenset(
    {
        "loadout_snapshot_digest",
        "step_context_id",
        "step_context_digest",
        "contextual_session_digest",
        "initial_reconciliation_digest",
        "equipped_item_snapshot_digest",
        "deck_snapshot_digest",
        "bootstrap_status_digest",
    }
)
_V3_SESSION_FIELDS = frozenset(
    {
        *_BASE_FIELDS,
        *_RUN_IDENTITY_FIELDS,
        *_PREFLIGHT_BINDING_FIELDS,
        "bootstrap_consumed_context_id",
        "transition_id",
    }
)


def _strict_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def _strict_sha256(value: object, label: str) -> str:
    text = _strict_text(value, label)
    if _SHA256_PATTERN.fullmatch(text) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _require_exact_fields(
    payload: Mapping[str, object], expected: frozenset[str], label: str
) -> None:
    actual = set(payload)
    if actual != set(expected):
        raise ValueError(
            f"{label} fields are invalid: "
            f"missing={sorted(set(expected) - actual)} "
            f"unknown={sorted(actual - set(expected))}"
        )


@dataclass(frozen=True, slots=True)
class ExamSessionPreflightBinding:
    """Immutable identity of the evidence bundle used for Exam bootstrap."""

    loadout_snapshot_digest: str
    step_context_id: str
    step_context_digest: str
    contextual_session_digest: str
    initial_reconciliation_digest: str
    equipped_item_snapshot_digest: str
    deck_snapshot_digest: str
    bootstrap_status_digest: str

    def __post_init__(self) -> None:
        _strict_text(self.step_context_id, "step_context_id")
        for label in (
            "loadout_snapshot_digest",
            "step_context_digest",
            "contextual_session_digest",
            "initial_reconciliation_digest",
            "equipped_item_snapshot_digest",
            "deck_snapshot_digest",
            "bootstrap_status_digest",
        ):
            _strict_sha256(getattr(self, label), label)

    def to_dict(self) -> dict[str, str]:
        return {
            "loadout_snapshot_digest": self.loadout_snapshot_digest,
            "step_context_id": self.step_context_id,
            "step_context_digest": self.step_context_digest,
            "contextual_session_digest": self.contextual_session_digest,
            "initial_reconciliation_digest": self.initial_reconciliation_digest,
            "equipped_item_snapshot_digest": self.equipped_item_snapshot_digest,
            "deck_snapshot_digest": self.deck_snapshot_digest,
            "bootstrap_status_digest": self.bootstrap_status_digest,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "ExamSessionPreflightBinding":
        if not isinstance(payload, Mapping):
            raise ValueError("exam preflight binding must be an object")
        _require_exact_fields(
            payload, _PREFLIGHT_BINDING_FIELDS, "exam preflight binding"
        )
        return cls(
            loadout_snapshot_digest=_strict_sha256(
                payload["loadout_snapshot_digest"], "loadout_snapshot_digest"
            ),
            step_context_id=_strict_text(
                payload["step_context_id"], "step_context_id"
            ),
            step_context_digest=_strict_sha256(
                payload["step_context_digest"], "step_context_digest"
            ),
            contextual_session_digest=_strict_sha256(
                payload["contextual_session_digest"],
                "contextual_session_digest",
            ),
            initial_reconciliation_digest=_strict_sha256(
                payload["initial_reconciliation_digest"],
                "initial_reconciliation_digest",
            ),
            equipped_item_snapshot_digest=_strict_sha256(
                payload["equipped_item_snapshot_digest"],
                "equipped_item_snapshot_digest",
            ),
            deck_snapshot_digest=_strict_sha256(
                payload["deck_snapshot_digest"], "deck_snapshot_digest"
            ),
            bootstrap_status_digest=_strict_sha256(
                payload["bootstrap_status_digest"], "bootstrap_status_digest"
            ),
        )


def bootstrap_transition_id(binding: ExamSessionPreflightBinding) -> str:
    """Return the deterministic origin ID for a newly bootstrapped checkpoint."""

    if not isinstance(binding, ExamSessionPreflightBinding):
        raise TypeError("binding must be ExamSessionPreflightBinding")
    return f"bootstrap-{binding.bootstrap_status_digest}"


@dataclass(frozen=True, slots=True)
class ExamSession:
    logic_state: LogicExamState
    idol_card_id: str
    produce_id: str
    step_type: str
    stage_number: int
    run_id: str | None = None
    character_id: str | None = None
    schema_version: int = SESSION_SCHEMA_VERSION
    preflight_binding: ExamSessionPreflightBinding | None = None
    bootstrap_consumed_context_id: str | None = None
    transition_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.logic_state, LogicExamState):
            raise TypeError("logic_state must be LogicExamState")
        self.logic_state.validate(allow_completed=True)
        for label in ("idol_card_id", "produce_id", "step_type"):
            _strict_text(getattr(self, label), label)
        if (
            not isinstance(self.stage_number, int)
            or isinstance(self.stage_number, bool)
            or self.stage_number < 1
        ):
            raise ValueError("stage_number must be an integer >= 1")
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version not in {*_LEGACY_SCHEMA_VERSIONS, SESSION_SCHEMA_VERSION}
        ):
            raise ValueError("unsupported exam session schema version")

        if self.schema_version == 1:
            if self.run_id is not None or self.character_id is not None:
                raise ValueError("schema v1 cannot contain run-scoped identity")
            self._require_no_live_binding()
            return

        _strict_text(self.run_id, "run_id")
        _strict_text(self.character_id, "character_id")
        if self.schema_version == 2:
            self._require_no_live_binding()
            return

        if not isinstance(self.preflight_binding, ExamSessionPreflightBinding):
            raise ValueError("schema v3 requires a complete preflight binding")
        consumed = _strict_text(
            self.bootstrap_consumed_context_id,
            "bootstrap_consumed_context_id",
        )
        if consumed != self.preflight_binding.step_context_id:
            raise ValueError(
                "bootstrap_consumed_context_id must equal step_context_id"
            )
        _strict_text(self.transition_id, "transition_id")

    def _require_no_live_binding(self) -> None:
        if any(
            value is not None
            for value in (
                self.preflight_binding,
                self.bootstrap_consumed_context_id,
                self.transition_id,
            )
        ):
            raise ValueError("legacy exam sessions cannot contain live binding fields")

    @property
    def stage_completed(self) -> bool:
        return self.logic_state.turns_remaining == 0

    def live_auto_click_blockers(self) -> tuple[str, ...]:
        """Return durable schema blockers for the live execution gate."""

        if self.schema_version in _LEGACY_SCHEMA_VERSIONS:
            return (f"legacy-exam-session-schema-v{self.schema_version}",)
        return ()

    def matches_preflight_binding(
        self, binding: ExamSessionPreflightBinding
    ) -> bool:
        if not isinstance(binding, ExamSessionPreflightBinding):
            raise TypeError("binding must be ExamSessionPreflightBinding")
        return bool(
            self.schema_version == SESSION_SCHEMA_VERSION
            and self.preflight_binding == binding
            and self.bootstrap_consumed_context_id == binding.step_context_id
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "logic_state": asdict(self.logic_state),
            "idol_card_id": self.idol_card_id,
            "produce_id": self.produce_id,
            "step_type": self.step_type,
            "stage_number": self.stage_number,
        }
        if self.schema_version >= 2:
            payload.update(
                {
                    "run_id": self.run_id,
                    "character_id": self.character_id,
                }
            )
        if self.schema_version == SESSION_SCHEMA_VERSION:
            assert self.preflight_binding is not None
            payload.update(self.preflight_binding.to_dict())
            payload.update(
                {
                    "bootstrap_consumed_context_id": (
                        self.bootstrap_consumed_context_id
                    ),
                    "transition_id": self.transition_id,
                }
            )
        return payload

    def matches_run(
        self,
        *,
        run_id: str,
        idol_card_id: str,
        character_id: str,
        produce_id: str,
    ) -> bool:
        return (
            self.run_id == run_id
            and self.idol_card_id == idol_card_id
            and self.character_id == character_id
            and self.produce_id == produce_id
        )


class ExamSessionConflictError(RuntimeError):
    """Raised when a stale writer no longer owns the expected checkpoint."""

    def __init__(self, expected: str | None, actual: str | None) -> None:
        self.expected_previous_transition_id = expected
        self.actual_previous_transition_id = actual
        super().__init__(
            "exam session transition compare-and-swap failed: "
            f"expected={expected!r}, actual={actual!r}"
        )


def load_exam_session(
    path: Path = DEFAULT_SESSION_PATH,
) -> ExamSession | None:
    path = Path(path)
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("exam session root must be an object")
    schema_version = payload.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in {*_LEGACY_SCHEMA_VERSIONS, SESSION_SCHEMA_VERSION}
    ):
        raise ValueError("unsupported exam session schema version")

    if schema_version == 1:
        expected_fields = _BASE_FIELDS
    elif schema_version == 2:
        expected_fields = frozenset({*_BASE_FIELDS, *_RUN_IDENTITY_FIELDS})
    else:
        expected_fields = _V3_SESSION_FIELDS
    _require_exact_fields(payload, expected_fields, "exam session")

    raw_state = payload["logic_state"]
    if not isinstance(raw_state, Mapping):
        raise ValueError("exam session logic_state must be an object")
    state = LogicExamState(**dict(raw_state))
    state.validate(allow_completed=True)
    stage_number = payload["stage_number"]
    if (
        not isinstance(stage_number, int)
        or isinstance(stage_number, bool)
        or stage_number < 1
    ):
        raise ValueError("exam session stage_number must be an integer >= 1")

    binding = None
    if schema_version == SESSION_SCHEMA_VERSION:
        binding = ExamSessionPreflightBinding.from_dict(
            {key: payload[key] for key in _PREFLIGHT_BINDING_FIELDS}
        )
    return ExamSession(
        logic_state=state,
        idol_card_id=_strict_text(payload["idol_card_id"], "idol_card_id"),
        produce_id=_strict_text(payload["produce_id"], "produce_id"),
        step_type=_strict_text(payload["step_type"], "step_type"),
        stage_number=stage_number,
        run_id=(
            None
            if schema_version == 1
            else _strict_text(payload["run_id"], "run_id")
        ),
        character_id=(
            None
            if schema_version == 1
            else _strict_text(payload["character_id"], "character_id")
        ),
        schema_version=schema_version,
        preflight_binding=binding,
        bootstrap_consumed_context_id=(
            None
            if schema_version != SESSION_SCHEMA_VERSION
            else _strict_text(
                payload["bootstrap_consumed_context_id"],
                "bootstrap_consumed_context_id",
            )
        ),
        transition_id=(
            None
            if schema_version != SESSION_SCHEMA_VERSION
            else _strict_text(payload["transition_id"], "transition_id")
        ),
    )


def _coerce_state(
    logic_state: Mapping[str, Any] | LogicExamState,
) -> LogicExamState:
    state = (
        logic_state
        if isinstance(logic_state, LogicExamState)
        else LogicExamState(**dict(logic_state))
    )
    state.validate(allow_completed=True)
    return state


def _atomic_write_session(path: Path, session: ExamSession) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                session.to_dict(),
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _assert_expected_previous_transition(
    path: Path, expected_previous_transition_id: str | None
) -> None:
    previous = load_exam_session(path)
    actual = None if previous is None else previous.transition_id
    if actual != expected_previous_transition_id:
        raise ExamSessionConflictError(expected_previous_transition_id, actual)


def save_exam_session(
    logic_state: Mapping[str, Any] | LogicExamState,
    *,
    idol_card_id: str,
    produce_id: str,
    step_type: str,
    stage_number: int,
    run_id: str | None = None,
    character_id: str | None = None,
    preflight_binding: ExamSessionPreflightBinding | None = None,
    bootstrap_consumed_context_id: str | None = None,
    transition_id: str | None = None,
    expected_previous_transition_id: str | None = None,
    path: Path = DEFAULT_SESSION_PATH,
) -> ExamSession:
    """Save one schema-v3 live checkpoint with transition CAS protection.

    ``expected_previous_transition_id=None`` means the caller expects no v3
    transition at the destination.  Updating an existing v3 checkpoint must
    therefore name the transition ID it previously read.
    """

    state = _coerce_state(logic_state)
    session = ExamSession(
        logic_state=state,
        idol_card_id=idol_card_id,
        produce_id=produce_id,
        step_type=step_type,
        stage_number=stage_number,
        run_id=run_id,
        character_id=character_id,
        schema_version=SESSION_SCHEMA_VERSION,
        preflight_binding=preflight_binding,
        bootstrap_consumed_context_id=bootstrap_consumed_context_id,
        transition_id=transition_id,
    )
    path = Path(path)
    _assert_expected_previous_transition(path, expected_previous_transition_id)
    _atomic_write_session(path, session)
    return session


def save_legacy_exam_session(
    logic_state: Mapping[str, Any] | LogicExamState,
    *,
    schema_version: int,
    idol_card_id: str,
    produce_id: str,
    step_type: str,
    stage_number: int,
    run_id: str | None = None,
    character_id: str | None = None,
    path: Path = DEFAULT_SESSION_PATH,
) -> ExamSession:
    """Explicitly write a display-only legacy fixture or import.

    Production live code must use :func:`save_exam_session`; this API exists so
    legacy fixtures never regain the old implicit ``run_id`` schema switch.
    """

    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in _LEGACY_SCHEMA_VERSIONS
    ):
        raise ValueError("legacy schema_version must be 1 or 2")
    session = ExamSession(
        logic_state=_coerce_state(logic_state),
        idol_card_id=idol_card_id,
        produce_id=produce_id,
        step_type=step_type,
        stage_number=stage_number,
        run_id=run_id,
        character_id=character_id,
        schema_version=schema_version,
    )
    _atomic_write_session(Path(path), session)
    return session


__all__ = [
    "DEFAULT_SESSION_PATH",
    "SESSION_SCHEMA_VERSION",
    "ExamSession",
    "ExamSessionConflictError",
    "ExamSessionPreflightBinding",
    "bootstrap_transition_id",
    "load_exam_session",
    "save_exam_session",
    "save_legacy_exam_session",
]
