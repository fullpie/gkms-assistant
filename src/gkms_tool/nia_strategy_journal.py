"""Append-only NIA decision evidence for offline strategy improvement.

This module is deliberately persistence-only.  It records the inputs, scored
candidates, immutable strategy version, and later observations needed by an
offline evaluator.  It neither loads policy weights nor changes a live policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Final, Mapping, Sequence, TypeAlias

from .nia_route_profile import nia_final_week


PROJECT_ROOT = Path(__file__).resolve().parents[2]
NIA_STRATEGY_JOURNAL_SCHEMA: Final = "gkms.nia-strategy-journal"
NIA_STRATEGY_JOURNAL_SCHEMA_VERSION: Final = 1
DEFAULT_NIA_STRATEGY_JOURNAL_ROOT: Final = (
    PROJECT_ROOT / "var" / "nia_strategy_journal"
)
_NIA_MODES: Final = frozenset({"produce-004", "produce-005"})
_SAFE_RUN_ID: Final = re.compile(r"^[A-Za-z0-9._-]+$")

JsonMapping: TypeAlias = Mapping[str, object]


class NiaStrategyJournalError(ValueError):
    """Stable, machine-readable validation failure for a journal row."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NiaStrategyJournalError("field-invalid", label)
    return value


def _integer(
    value: object,
    label: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise NiaStrategyJournalError("field-invalid", label)
    if maximum is not None and value > maximum:
        raise NiaStrategyJournalError("field-invalid", label)
    return value


def _number(value: object, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NiaStrategyJournalError("field-invalid", label)
    if isinstance(value, float) and not math.isfinite(value):
        raise NiaStrategyJournalError("field-invalid", label)
    return value


def _optional_number(value: object, label: str) -> int | float | None:
    return None if value is None else _number(value, label)


def _optional_integer(value: object, label: str) -> int | None:
    return None if value is None else _integer(value, label)


def _optional_boolean(value: object, label: str) -> bool | None:
    if value is not None and not isinstance(value, bool):
        raise NiaStrategyJournalError("field-invalid", label)
    return value


def _strict_mapping(
    value: object,
    label: str,
    fields: set[str],
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise NiaStrategyJournalError("row-shape-invalid", label)
    return value


def _freeze_json(value: object, label: str) -> object:
    """Validate and detach an arbitrary strict-JSON candidate/state payload."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NiaStrategyJournalError("json-value-invalid", label)
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise NiaStrategyJournalError("json-key-invalid", label)
            result[key] = _freeze_json(item, f"{label}.{key}")
        return MappingProxyType(result)
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return tuple(
            _freeze_json(item, f"{label}[{index}]")
            for index, item in enumerate(value)
        )
    raise NiaStrategyJournalError("json-value-invalid", label)


def _freeze_mapping(value: object, label: str) -> Mapping[str, object]:
    frozen = _freeze_json(value, label)
    if not isinstance(frozen, Mapping):
        raise NiaStrategyJournalError("field-invalid", label)
    return frozen


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _validate_context(
    *,
    record_id: object,
    recorded_at: object,
    run_id: object,
    mode: object,
    idol: object,
    archetype: object,
    week: object,
    phase: object,
    strategy_version: object,
) -> None:
    _text(record_id, "record_id")
    _text(recorded_at, "recorded_at")
    _text(run_id, "run_id")
    if mode not in _NIA_MODES:
        raise NiaStrategyJournalError("field-invalid", "mode")
    _text(idol, "idol")
    _text(archetype, "archetype")
    _integer(week, "week", minimum=1, maximum=nia_final_week(str(mode)))
    _integer(phase, "phase", minimum=1, maximum=3)
    _text(strategy_version, "strategy_version")


@dataclass(frozen=True, slots=True)
class NiaStrategyState:
    """Decision-visible NIA state plus lossless auxiliary evidence."""

    stamina: int
    max_stamina: int
    produce_points: int
    vocal: int
    dance: int
    visual: int
    vote_count: int
    exam_score: int | None = None
    turns_remaining: int | None = None
    plays_remaining: int | None = None
    details: JsonMapping = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "stamina",
            "max_stamina",
            "produce_points",
            "vocal",
            "dance",
            "visual",
            "vote_count",
        ):
            _integer(getattr(self, name), f"state.{name}")
        if self.stamina > self.max_stamina:
            raise NiaStrategyJournalError(
                "field-invalid", "state.stamina>max_stamina"
            )
        for name in ("exam_score", "turns_remaining", "plays_remaining"):
            value = getattr(self, name)
            if value is not None:
                _integer(value, f"state.{name}")
        object.__setattr__(
            self, "details", _freeze_mapping(self.details, "state.details")
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "stamina": self.stamina,
            "max_stamina": self.max_stamina,
            "produce_points": self.produce_points,
            "vocal": self.vocal,
            "dance": self.dance,
            "visual": self.visual,
            "vote_count": self.vote_count,
            "exam_score": self.exam_score,
            "turns_remaining": self.turns_remaining,
            "plays_remaining": self.plays_remaining,
            "details": _thaw_json(self.details),
        }

    @classmethod
    def from_dict(cls, value: object) -> "NiaStrategyState":
        raw = _strict_mapping(
            value,
            "state",
            {
                "stamina",
                "max_stamina",
                "produce_points",
                "vocal",
                "dance",
                "visual",
                "vote_count",
                "exam_score",
                "turns_remaining",
                "plays_remaining",
                "details",
            },
        )
        return cls(
            stamina=_integer(raw["stamina"], "state.stamina"),
            max_stamina=_integer(raw["max_stamina"], "state.max_stamina"),
            produce_points=_integer(
                raw["produce_points"], "state.produce_points"
            ),
            vocal=_integer(raw["vocal"], "state.vocal"),
            dance=_integer(raw["dance"], "state.dance"),
            visual=_integer(raw["visual"], "state.visual"),
            vote_count=_integer(raw["vote_count"], "state.vote_count"),
            exam_score=_optional_integer(raw["exam_score"], "state.exam_score"),
            turns_remaining=_optional_integer(
                raw["turns_remaining"], "state.turns_remaining"
            ),
            plays_remaining=_optional_integer(
                raw["plays_remaining"], "state.plays_remaining"
            ),
            details=_freeze_mapping(raw["details"], "state.details"),
        )


@dataclass(frozen=True, slots=True)
class NiaCandidateScores:
    """Frozen total and named components emitted by one strategy version."""

    total: int | float
    components: Mapping[str, int | float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _number(self.total, "scores.total")
        if not isinstance(self.components, Mapping):
            raise NiaStrategyJournalError("field-invalid", "scores.components")
        validated: dict[str, int | float] = {}
        for name, value in self.components.items():
            key = _text(name, "scores.components.name")
            validated[key] = _number(value, f"scores.components.{key}")
        object.__setattr__(self, "components", MappingProxyType(validated))

    def to_dict(self) -> dict[str, object]:
        return {"total": self.total, "components": dict(self.components)}

    @classmethod
    def from_dict(cls, value: object) -> "NiaCandidateScores":
        raw = _strict_mapping(value, "scores", {"total", "components"})
        components = raw["components"]
        if not isinstance(components, Mapping):
            raise NiaStrategyJournalError("field-invalid", "scores.components")
        return cls(
            total=_number(raw["total"], "scores.total"),
            components={
                _text(name, "scores.components.name"): _number(
                    score, f"scores.components.{name}"
                )
                for name, score in components.items()
            },
        )


@dataclass(frozen=True, slots=True)
class NiaStrategyCandidate:
    """One complete candidate snapshot and its strategy-owned scores."""

    candidate_id: str
    kind: str
    payload: JsonMapping
    scores: NiaCandidateScores

    def __post_init__(self) -> None:
        _text(self.candidate_id, "candidate.candidate_id")
        _text(self.kind, "candidate.kind")
        if not isinstance(self.scores, NiaCandidateScores):
            raise TypeError("scores must be NiaCandidateScores")
        object.__setattr__(
            self,
            "payload",
            _freeze_mapping(self.payload, f"candidate.{self.candidate_id}.payload"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "payload": _thaw_json(self.payload),
            "scores": self.scores.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> "NiaStrategyCandidate":
        raw = _strict_mapping(
            value,
            "candidate",
            {"candidate_id", "kind", "payload", "scores"},
        )
        return cls(
            candidate_id=_text(raw["candidate_id"], "candidate.candidate_id"),
            kind=_text(raw["kind"], "candidate.kind"),
            payload=_freeze_mapping(raw["payload"], "candidate.payload"),
            scores=NiaCandidateScores.from_dict(raw["scores"]),
        )


_CONTEXT_FIELDS = {
    "record_id",
    "recorded_at",
    "run_id",
    "mode",
    "idol",
    "archetype",
    "week",
    "phase",
    "strategy_version",
}


def _context_dict(record: object) -> dict[str, object]:
    return {
        name: getattr(record, name)
        for name in (
            "record_id",
            "recorded_at",
            "run_id",
            "mode",
            "idol",
            "archetype",
            "week",
            "phase",
            "strategy_version",
        )
    }


def _context_kwargs(raw: Mapping[str, object]) -> dict[str, object]:
    mode = _text(raw["mode"], "mode")
    return {
        "record_id": _text(raw["record_id"], "record_id"),
        "recorded_at": _text(raw["recorded_at"], "recorded_at"),
        "run_id": _text(raw["run_id"], "run_id"),
        "mode": mode,
        "idol": _text(raw["idol"], "idol"),
        "archetype": _text(raw["archetype"], "archetype"),
        "week": _integer(
            raw["week"],
            "week",
            minimum=1,
            maximum=nia_final_week(mode),
        ),
        "phase": _integer(raw["phase"], "phase", minimum=1, maximum=3),
        "strategy_version": _text(
            raw["strategy_version"], "strategy_version"
        ),
    }


@dataclass(frozen=True, slots=True)
class NiaDecisionRecord:
    record_id: str
    recorded_at: str
    run_id: str
    mode: str
    idol: str
    archetype: str
    week: int
    phase: int
    strategy_version: str
    decision_kind: str
    state: NiaStrategyState
    candidates: tuple[NiaStrategyCandidate, ...]
    chosen: str

    def __post_init__(self) -> None:
        _validate_context(**_context_dict(self))
        _text(self.decision_kind, "decision_kind")
        if not isinstance(self.state, NiaStrategyState):
            raise TypeError("state must be NiaStrategyState")
        if not isinstance(self.candidates, tuple) or not self.candidates:
            raise NiaStrategyJournalError("field-invalid", "candidates")
        if not all(
            isinstance(candidate, NiaStrategyCandidate)
            for candidate in self.candidates
        ):
            raise TypeError("candidates must contain NiaStrategyCandidate values")
        candidate_ids = tuple(value.candidate_id for value in self.candidates)
        if len(set(candidate_ids)) != len(candidate_ids):
            raise NiaStrategyJournalError("candidate-id-duplicate")
        _text(self.chosen, "chosen")
        if self.chosen not in candidate_ids:
            raise NiaStrategyJournalError("chosen-candidate-missing", self.chosen)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": NIA_STRATEGY_JOURNAL_SCHEMA,
            "schema_version": NIA_STRATEGY_JOURNAL_SCHEMA_VERSION,
            "record_type": "decision",
            **_context_dict(self),
            "decision_kind": self.decision_kind,
            "state": self.state.to_dict(),
            "candidates": [value.to_dict() for value in self.candidates],
            "chosen": self.chosen,
        }

    @classmethod
    def from_dict(cls, value: object) -> "NiaDecisionRecord":
        fields = {
            "schema",
            "schema_version",
            "record_type",
            *_CONTEXT_FIELDS,
            "decision_kind",
            "state",
            "candidates",
            "chosen",
        }
        raw = _strict_mapping(value, "decision", fields)
        _validate_header(raw, "decision")
        candidates = raw["candidates"]
        if not isinstance(candidates, list):
            raise NiaStrategyJournalError("field-invalid", "candidates")
        return cls(
            **_context_kwargs(raw),
            decision_kind=_text(raw["decision_kind"], "decision_kind"),
            state=NiaStrategyState.from_dict(raw["state"]),
            candidates=tuple(
                NiaStrategyCandidate.from_dict(candidate)
                for candidate in candidates
            ),
            chosen=_text(raw["chosen"], "chosen"),
        )


@dataclass(frozen=True, slots=True)
class NiaOutcomeRecord:
    """A later observation linked to one immutable decision record."""

    record_id: str
    recorded_at: str
    run_id: str
    mode: str
    idol: str
    archetype: str
    week: int
    phase: int
    strategy_version: str
    decision_id: str
    state: NiaStrategyState
    outcome: str
    reward: int | float | None = None
    passed: bool | None = None
    details: JsonMapping = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_context(**_context_dict(self))
        _text(self.decision_id, "decision_id")
        _text(self.outcome, "outcome")
        if not isinstance(self.state, NiaStrategyState):
            raise TypeError("state must be NiaStrategyState")
        _optional_number(self.reward, "reward")
        _optional_boolean(self.passed, "passed")
        object.__setattr__(
            self, "details", _freeze_mapping(self.details, "outcome.details")
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": NIA_STRATEGY_JOURNAL_SCHEMA,
            "schema_version": NIA_STRATEGY_JOURNAL_SCHEMA_VERSION,
            "record_type": "outcome",
            **_context_dict(self),
            "decision_id": self.decision_id,
            "state": self.state.to_dict(),
            "outcome": self.outcome,
            "reward": self.reward,
            "passed": self.passed,
            "details": _thaw_json(self.details),
        }

    @classmethod
    def from_dict(cls, value: object) -> "NiaOutcomeRecord":
        fields = {
            "schema",
            "schema_version",
            "record_type",
            *_CONTEXT_FIELDS,
            "decision_id",
            "state",
            "outcome",
            "reward",
            "passed",
            "details",
        }
        raw = _strict_mapping(value, "outcome", fields)
        _validate_header(raw, "outcome")
        return cls(
            **_context_kwargs(raw),
            decision_id=_text(raw["decision_id"], "decision_id"),
            state=NiaStrategyState.from_dict(raw["state"]),
            outcome=_text(raw["outcome"], "outcome"),
            reward=_optional_number(raw["reward"], "reward"),
            passed=_optional_boolean(raw["passed"], "passed"),
            details=_freeze_mapping(raw["details"], "outcome.details"),
        )


@dataclass(frozen=True, slots=True)
class NiaFinalRecord:
    """Terminal pass/fail label for one NIA run."""

    record_id: str
    recorded_at: str
    run_id: str
    mode: str
    idol: str
    archetype: str
    week: int
    phase: int
    strategy_version: str
    state: NiaStrategyState
    outcome: str
    passed: bool
    final_score: int | float | None = None
    details: JsonMapping = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_context(**_context_dict(self))
        _text(self.outcome, "outcome")
        if not isinstance(self.state, NiaStrategyState):
            raise TypeError("state must be NiaStrategyState")
        if not isinstance(self.passed, bool):
            raise NiaStrategyJournalError("field-invalid", "passed")
        _optional_number(self.final_score, "final_score")
        object.__setattr__(
            self, "details", _freeze_mapping(self.details, "final.details")
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": NIA_STRATEGY_JOURNAL_SCHEMA,
            "schema_version": NIA_STRATEGY_JOURNAL_SCHEMA_VERSION,
            "record_type": "final",
            **_context_dict(self),
            "state": self.state.to_dict(),
            "outcome": self.outcome,
            "passed": self.passed,
            "final_score": self.final_score,
            "details": _thaw_json(self.details),
        }

    @classmethod
    def from_dict(cls, value: object) -> "NiaFinalRecord":
        fields = {
            "schema",
            "schema_version",
            "record_type",
            *_CONTEXT_FIELDS,
            "state",
            "outcome",
            "passed",
            "final_score",
            "details",
        }
        raw = _strict_mapping(value, "final", fields)
        _validate_header(raw, "final")
        passed = raw["passed"]
        if not isinstance(passed, bool):
            raise NiaStrategyJournalError("field-invalid", "passed")
        return cls(
            **_context_kwargs(raw),
            state=NiaStrategyState.from_dict(raw["state"]),
            outcome=_text(raw["outcome"], "outcome"),
            passed=passed,
            final_score=_optional_number(raw["final_score"], "final_score"),
            details=_freeze_mapping(raw["details"], "final.details"),
        )


@dataclass(frozen=True, slots=True)
class NiaOuterTransactionReceipt:
    """A v2 outer-transaction anchor stored by the existing journal owner.

    Begin and terminal observations are separate rows with separate
    ``record_id`` values.  The shared ``transaction_id`` joins them without
    promoting an incomplete native observer/LocalSave anchor into training
    truth.
    """

    record_id: str
    recorded_at: str
    run_id: str
    mode: str
    idol: str
    archetype: str
    week: int
    phase: int
    strategy_version: str
    transaction_id: str
    selected_action: str
    legal_action_ids: tuple[str, ...]
    target_pid: int | None
    authority: JsonMapping
    authority_complete: bool
    observer_anchor: JsonMapping
    observer_anchor_complete: bool
    terminal_receipt_id: str | None
    terminal_kind: str
    candidate_set_complete: bool = False
    reward: None = None
    bc_eligible: bool = False
    rl_eligible: bool = False
    details: JsonMapping = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_context(**_context_dict(self))
        _text(self.transaction_id, "transaction_id")
        _text(self.selected_action, "selected_action")
        legal_action_ids = tuple(self.legal_action_ids)
        if not legal_action_ids or any(
            not isinstance(value, str) or not value.strip()
            for value in legal_action_ids
        ):
            raise NiaStrategyJournalError("field-invalid", "legal_action_ids")
        if len(legal_action_ids) != len(set(legal_action_ids)):
            raise NiaStrategyJournalError("legal-action-id-duplicate")
        if self.selected_action not in legal_action_ids:
            raise NiaStrategyJournalError(
                "selected-action-not-legal", self.selected_action
            )
        object.__setattr__(self, "legal_action_ids", legal_action_ids)
        if self.target_pid is not None:
            _integer(self.target_pid, "target_pid", minimum=1)
        object.__setattr__(
            self,
            "authority",
            _freeze_mapping(self.authority, "outer_transaction.authority"),
        )
        object.__setattr__(
            self,
            "observer_anchor",
            _freeze_mapping(
                self.observer_anchor,
                "outer_transaction.observer_anchor",
            ),
        )
        for name in (
            "authority_complete",
            "observer_anchor_complete",
            "candidate_set_complete",
            "bc_eligible",
            "rl_eligible",
        ):
            if type(getattr(self, name)) is not bool:
                raise NiaStrategyJournalError("field-invalid", name)
        if self.terminal_receipt_id is not None:
            _text(self.terminal_receipt_id, "terminal_receipt_id")
        if self.terminal_kind not in {
            "awaiting",
            "week_successor",
            "run_terminal",
        }:
            raise NiaStrategyJournalError("field-invalid", "terminal_kind")
        if self.terminal_kind == "awaiting":
            if self.terminal_receipt_id is not None:
                raise NiaStrategyJournalError(
                    "awaiting-terminal-receipt-present"
                )
        elif self.terminal_receipt_id is None:
            raise NiaStrategyJournalError("terminal-receipt-missing")
        if self.reward is not None:
            raise NiaStrategyJournalError("field-invalid", "reward-must-be-null")
        if self.rl_eligible:
            raise NiaStrategyJournalError("null-reward-rl-eligible")
        if (
            not self.authority_complete
            or not self.observer_anchor_complete
            or not self.candidate_set_complete
        ) and self.bc_eligible:
            raise NiaStrategyJournalError(
                "incomplete-anchor-training-eligible"
            )
        object.__setattr__(
            self,
            "details",
            _freeze_mapping(self.details, "outer_transaction.details"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": NIA_STRATEGY_JOURNAL_SCHEMA,
            "schema_version": NIA_STRATEGY_JOURNAL_SCHEMA_VERSION,
            "record_type": "outer_transaction_v2",
            **_context_dict(self),
            "transaction_id": self.transaction_id,
            "selected_action": self.selected_action,
            "legal_action_ids": list(self.legal_action_ids),
            "candidate_set_complete": self.candidate_set_complete,
            "target_pid": self.target_pid,
            "authority": _thaw_json(self.authority),
            "authority_complete": self.authority_complete,
            "observer_anchor": _thaw_json(self.observer_anchor),
            "observer_anchor_complete": self.observer_anchor_complete,
            "terminal_receipt_id": self.terminal_receipt_id,
            "terminal_kind": self.terminal_kind,
            "reward": None,
            "bc_eligible": self.bc_eligible,
            "rl_eligible": self.rl_eligible,
            "details": _thaw_json(self.details),
        }

    @classmethod
    def from_dict(cls, value: object) -> "NiaOuterTransactionReceipt":
        fields = {
            "schema",
            "schema_version",
            "record_type",
            *_CONTEXT_FIELDS,
            "transaction_id",
            "selected_action",
            "legal_action_ids",
            "candidate_set_complete",
            "target_pid",
            "authority",
            "authority_complete",
            "observer_anchor",
            "observer_anchor_complete",
            "terminal_receipt_id",
            "terminal_kind",
            "reward",
            "bc_eligible",
            "rl_eligible",
            "details",
        }
        raw = _strict_mapping(value, "outer_transaction_v2", fields)
        _validate_header(raw, "outer_transaction_v2")
        legal_action_ids = raw["legal_action_ids"]
        if not isinstance(legal_action_ids, list):
            raise NiaStrategyJournalError(
                "field-invalid", "legal_action_ids"
            )
        target_pid = raw["target_pid"]
        terminal_receipt_id = raw["terminal_receipt_id"]
        terminal_kind = raw["terminal_kind"]
        return cls(
            **_context_kwargs(raw),
            transaction_id=_text(raw["transaction_id"], "transaction_id"),
            selected_action=_text(raw["selected_action"], "selected_action"),
            legal_action_ids=tuple(legal_action_ids),
            candidate_set_complete=raw["candidate_set_complete"],  # type: ignore[arg-type]
            target_pid=(
                None
                if target_pid is None
                else _integer(target_pid, "target_pid", minimum=1)
            ),
            authority=_freeze_mapping(raw["authority"], "authority"),
            authority_complete=raw["authority_complete"],  # type: ignore[arg-type]
            observer_anchor=_freeze_mapping(
                raw["observer_anchor"], "observer_anchor"
            ),
            observer_anchor_complete=raw["observer_anchor_complete"],  # type: ignore[arg-type]
            terminal_receipt_id=(
                None
                if terminal_receipt_id is None
                else _text(terminal_receipt_id, "terminal_receipt_id")
            ),
            terminal_kind=_text(terminal_kind, "terminal_kind"),
            reward=raw["reward"],  # type: ignore[arg-type]
            bc_eligible=raw["bc_eligible"],  # type: ignore[arg-type]
            rl_eligible=raw["rl_eligible"],  # type: ignore[arg-type]
            details=_freeze_mapping(raw["details"], "details"),
        )


NiaStrategyRecord: TypeAlias = (
    NiaDecisionRecord
    | NiaOutcomeRecord
    | NiaFinalRecord
    | NiaOuterTransactionReceipt
)


def _validate_header(raw: Mapping[str, object], record_type: str) -> None:
    if raw["schema"] != NIA_STRATEGY_JOURNAL_SCHEMA:
        raise NiaStrategyJournalError("schema-name-unsupported")
    if raw["schema_version"] != NIA_STRATEGY_JOURNAL_SCHEMA_VERSION:
        raise NiaStrategyJournalError("schema-version-unsupported")
    if raw["record_type"] != record_type:
        raise NiaStrategyJournalError("record-type-invalid")


def nia_strategy_record_from_dict(value: object) -> NiaStrategyRecord:
    if not isinstance(value, Mapping):
        raise NiaStrategyJournalError("row-shape-invalid")
    record_type = value.get("record_type")
    if record_type == "decision":
        return NiaDecisionRecord.from_dict(value)
    if record_type == "outcome":
        return NiaOutcomeRecord.from_dict(value)
    if record_type == "final":
        return NiaFinalRecord.from_dict(value)
    if record_type == "outer_transaction_v2":
        return NiaOuterTransactionReceipt.from_dict(value)
    raise NiaStrategyJournalError("record-type-unsupported")


def nia_strategy_journal_path(
    run_id: str,
    *,
    root: str | Path = DEFAULT_NIA_STRATEGY_JOURNAL_ROOT,
) -> Path:
    safe_run_id = _text(run_id, "run_id")
    if _SAFE_RUN_ID.fullmatch(safe_run_id) is None:
        raise NiaStrategyJournalError("run-id-path-unsafe")
    return Path(root) / f"{safe_run_id}.jsonl"


def read_nia_strategy_journal(
    path: str | Path,
) -> tuple[NiaStrategyRecord, ...]:
    target = Path(path)
    if not target.exists():
        return ()
    records: list[NiaStrategyRecord] = []
    record_ids: set[str] = set()
    decisions: dict[str, NiaDecisionRecord] = {}
    finals: set[str] = set()
    for line_number, line in enumerate(
        target.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            record = nia_strategy_record_from_dict(value)
            if record.record_id in record_ids:
                raise NiaStrategyJournalError(
                    "record-id-duplicate", record.record_id
                )
            if isinstance(record, NiaDecisionRecord):
                decisions[record.record_id] = record
            elif isinstance(record, NiaOutcomeRecord):
                decision = decisions.get(record.decision_id)
                if decision is None:
                    raise NiaStrategyJournalError(
                        "outcome-decision-missing", record.decision_id
                    )
                if (
                    decision.run_id != record.run_id
                    or decision.mode != record.mode
                    or decision.idol != record.idol
                    or decision.archetype != record.archetype
                    or decision.strategy_version != record.strategy_version
                ):
                    raise NiaStrategyJournalError(
                        "outcome-decision-context-mismatch", record.decision_id
                    )
            elif isinstance(record, NiaFinalRecord):
                if record.run_id in finals:
                    raise NiaStrategyJournalError(
                        "run-final-duplicate", record.run_id
                    )
                finals.add(record.run_id)
            record_ids.add(record.record_id)
            records.append(record)
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise NiaStrategyJournalError(
                "journal-row-invalid",
                f"line={line_number}:{type(error).__name__}:{error}",
            ) from error
    return tuple(records)


def append_nia_strategy_journal(
    path: str | Path,
    record: NiaStrategyRecord,
) -> bool:
    if not isinstance(
        record,
        (
            NiaDecisionRecord,
            NiaOutcomeRecord,
            NiaFinalRecord,
            NiaOuterTransactionReceipt,
        ),
    ):
        raise TypeError("record must be a typed NIA strategy record")
    target = Path(path)
    existing = read_nia_strategy_journal(target)
    same_id = next(
        (value for value in existing if value.record_id == record.record_id),
        None,
    )
    if (
        isinstance(record, NiaOuterTransactionReceipt)
        and isinstance(same_id, NiaOuterTransactionReceipt)
        and replace(record, recorded_at=same_id.recorded_at) == same_id
    ):
        return False
    if same_id is not None:
        raise NiaStrategyJournalError("record-id-duplicate", record.record_id)
    if isinstance(record, NiaOutcomeRecord):
        decision = next(
            (
                value
                for value in existing
                if isinstance(value, NiaDecisionRecord)
                and value.record_id == record.decision_id
            ),
            None,
        )
        if decision is None:
            raise NiaStrategyJournalError(
                "outcome-decision-missing", record.decision_id
            )
        if (
            decision.run_id != record.run_id
            or decision.mode != record.mode
            or decision.idol != record.idol
            or decision.archetype != record.archetype
            or decision.strategy_version != record.strategy_version
        ):
            raise NiaStrategyJournalError(
                "outcome-decision-context-mismatch", record.decision_id
            )
    if isinstance(record, NiaFinalRecord) and any(
        isinstance(value, NiaFinalRecord) and value.run_id == record.run_id
        for value in existing
    ):
        raise NiaStrategyJournalError("run-final-duplicate", record.run_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        record.to_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    with target.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded + "\n")
        stream.flush()
    return True


__all__ = [
    "DEFAULT_NIA_STRATEGY_JOURNAL_ROOT",
    "NIA_STRATEGY_JOURNAL_SCHEMA",
    "NIA_STRATEGY_JOURNAL_SCHEMA_VERSION",
    "NiaCandidateScores",
    "NiaDecisionRecord",
    "NiaFinalRecord",
    "NiaOuterTransactionReceipt",
    "NiaOutcomeRecord",
    "NiaStrategyCandidate",
    "NiaStrategyJournalError",
    "NiaStrategyRecord",
    "NiaStrategyState",
    "append_nia_strategy_journal",
    "nia_strategy_journal_path",
    "nia_strategy_record_from_dict",
    "read_nia_strategy_journal",
]
