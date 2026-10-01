"""Pure, GUI-friendly projection of an Initial Regular fixed run.

``run_initial_regular_fixed_scenario`` deliberately returns the complete
domain states at every transition.  A GUI normally needs only a small set of
stable scalar values, though, and should not have to know about the rollout
kernel's internal objects.  This module provides that read-only projection.

The projection never invokes a policy, chance provider, solver, or device
reader.  It only walks an already-produced
:class:`~gkms_tool.initial_regular_fixed_run.InitialRegularFixedRunResult`.
Unknown values (for example a branch probability that was not supplied by an
external source) remain ``None`` rather than being guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from .initial_regular_fixed_run import (
    InitialRegularFixedRunResult,
    InitialRegularFixedRunStep,
    InitialRegularFixedRunStepKind,
    InitialRegularFixedRunStop,
)
from .produce_rollout import (
    ActionKind,
    DeckEntry,
    ExternalKind,
    Lifecycle,
    ProduceRolloutState,
    RolloutPhase,
)
from .produce_rollout_expectimax import OuterSearchIssue


SCHEMA_NAME = "gkms_tool.initial_regular_fixed_run_report"
SCHEMA_VERSION = 1


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be non-empty text")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _optional_float(value: object, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric or None")
    return float(value)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _object_list(value: object, label: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError(f"{label} must be a list of objects")
    return tuple(value)  # type: ignore[return-value]


def _enum(value: object, enum_type: type[StrEnum], label: str) -> StrEnum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid {label}: {value!r}") from error


def _action_kind(value: object, label: str = "action_kind") -> ActionKind:
    return _enum(value, ActionKind, label)  # type: ignore[return-value]


def _external_kind(value: object, label: str = "request_kind") -> ExternalKind:
    return _enum(value, ExternalKind, label)  # type: ignore[return-value]


def _step_kind(value: object) -> InitialRegularFixedRunStepKind:
    return _enum(value, InitialRegularFixedRunStepKind, "step kind")  # type: ignore[return-value]


def _stop(value: object) -> InitialRegularFixedRunStop:
    return _enum(value, InitialRegularFixedRunStop, "run stop")  # type: ignore[return-value]


def _phase(value: object) -> RolloutPhase:
    return _enum(value, RolloutPhase, "rollout phase")  # type: ignore[return-value]


def _lifecycle(value: object) -> Lifecycle:
    return _enum(value, Lifecycle, "rollout lifecycle")  # type: ignore[return-value]


def _issue_to_dict(issue: OuterSearchIssue) -> dict[str, str]:
    return {"code": issue.code, "field": issue.field, "detail": issue.detail}


def _issue_from_dict(payload: Mapping[str, object]) -> OuterSearchIssue:
    detail = payload.get("detail", "")
    if not isinstance(detail, str):
        raise ValueError("issue.detail must be text")
    return OuterSearchIssue(
        _text(payload.get("code"), "issue.code"),
        _text(payload.get("field"), "issue.field"),
        detail,
    )


@dataclass(frozen=True, slots=True)
class InitialRegularFixedRunStateReport:
    """The scalar state subset useful to a GUI before or after one step."""

    week: int
    stamina: int
    vocal: int
    dance: int
    visual: int
    deck_count: int
    phase: RolloutPhase
    lifecycle: Lifecycle

    def __post_init__(self) -> None:
        if self.week < 1:
            raise ValueError("report week must be positive")
        if min(self.stamina, self.vocal, self.dance, self.visual, self.deck_count) < 0:
            raise ValueError("report scalar values cannot be negative")
        if not isinstance(self.phase, RolloutPhase):
            raise TypeError("phase must be RolloutPhase")
        if not isinstance(self.lifecycle, Lifecycle):
            raise TypeError("lifecycle must be Lifecycle")

    @classmethod
    def from_state(cls, state: ProduceRolloutState) -> "InitialRegularFixedRunStateReport":
        if not isinstance(state, ProduceRolloutState):
            raise TypeError("state must be ProduceRolloutState")
        return cls(
            week=state.week,
            stamina=state.stamina,
            vocal=state.attributes.vocal,
            dance=state.attributes.dance,
            visual=state.attributes.visual,
            deck_count=sum(entry.count for entry in state.deck),
            phase=state.phase,
            lifecycle=state.lifecycle,
        )

    # ``from_state`` is the domain-facing name; this alias is convenient for
    # callers that describe the value as a snapshot.
    from_snapshot = from_state

    @property
    def attributes(self) -> tuple[int, int, int]:
        return self.vocal, self.dance, self.visual

    def to_dict(self) -> dict[str, object]:
        return {
            "week": self.week,
            "stamina": self.stamina,
            "vocal": self.vocal,
            "dance": self.dance,
            "visual": self.visual,
            "deck_count": self.deck_count,
            "phase": self.phase.value,
            "lifecycle": self.lifecycle.value,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "InitialRegularFixedRunStateReport":
        return cls(
            week=_integer(payload.get("week"), "state.week", minimum=1),
            stamina=_integer(payload.get("stamina"), "state.stamina"),
            vocal=_integer(payload.get("vocal"), "state.vocal"),
            dance=_integer(payload.get("dance"), "state.dance"),
            visual=_integer(payload.get("visual"), "state.visual"),
            deck_count=_integer(payload.get("deck_count"), "state.deck_count"),
            phase=_phase(payload.get("phase")),
            lifecycle=_lifecycle(payload.get("lifecycle")),
        )

    @property
    def summary_text(self) -> str:
        return (
            f"week {self.week}; stamina {self.stamina}; "
            f"Vo {self.vocal}/Da {self.dance}/Vi {self.visual}; "
            f"deck {self.deck_count}; phase {self.phase.value}; "
            f"lifecycle {self.lifecycle.value}"
        )


# A descriptive alias used by some GUI callers.
InitialRegularFixedRunStateSnapshot = InitialRegularFixedRunStateReport


@dataclass(frozen=True, slots=True)
class InitialRegularFixedRunDeckEntryReport:
    """One exact outer-deck stack retained for GUI rendering."""

    card_id: str
    upgrade: int
    count: int
    instance_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.card_id, "deck.card_id")
        _integer(self.upgrade, "deck.upgrade")
        _integer(self.count, "deck.count", minimum=1)
        values = tuple(self.instance_ids)
        if values and len(values) != self.count:
            raise ValueError("deck.instance_ids must be empty or match count")
        if any(not isinstance(value, str) or not value for value in values):
            raise ValueError("deck.instance_ids must contain non-empty text")
        object.__setattr__(self, "instance_ids", values)

    @classmethod
    def from_entry(cls, entry: DeckEntry) -> "InitialRegularFixedRunDeckEntryReport":
        if not isinstance(entry, DeckEntry):
            raise TypeError("entry must be DeckEntry")
        return cls(entry.card_id, entry.upgrade, entry.count, entry.instance_ids)

    @property
    def identity_complete(self) -> bool:
        return len(self.instance_ids) == self.count

    def to_dict(self) -> dict[str, object]:
        return {
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "count": self.count,
            "instance_ids": list(self.instance_ids),
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "InitialRegularFixedRunDeckEntryReport":
        raw_instance_ids = payload.get("instance_ids", [])
        if not isinstance(raw_instance_ids, list):
            raise ValueError("deck.instance_ids must be a list")
        return cls(
            card_id=_text(payload.get("card_id"), "deck.card_id"),
            upgrade=_integer(payload.get("upgrade"), "deck.upgrade"),
            count=_integer(payload.get("count"), "deck.count", minimum=1),
            instance_ids=tuple(
                _text(value, "deck.instance_id") for value in raw_instance_ids
            ),
        )


@dataclass(frozen=True, slots=True)
class InitialRegularFixedRunMilestoneReport:
    """One lesson/audition result supplied by the fixed scenario."""

    stage: str
    score: int
    rank: int | None
    cleared: bool

    def __post_init__(self) -> None:
        _text(self.stage, "milestone.stage")
        _integer(self.score, "milestone.score")
        if self.rank is not None:
            _integer(self.rank, "milestone.rank", minimum=1)
        if not isinstance(self.cleared, bool):
            raise TypeError("milestone.cleared must be boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "score": self.score,
            "rank": self.rank,
            "cleared": self.cleared,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "InitialRegularFixedRunMilestoneReport":
        rank = payload.get("rank")
        cleared = payload.get("cleared")
        if not isinstance(cleared, bool):
            raise ValueError("milestone.cleared must be boolean")
        return cls(
            stage=_text(payload.get("stage"), "milestone.stage"),
            score=_integer(payload.get("score"), "milestone.score"),
            rank=None
            if rank is None
            else _integer(rank, "milestone.rank", minimum=1),
            cleared=cleared,
        )


@dataclass(frozen=True, slots=True)
class InitialRegularFixedRunCardCoverageReport:
    """Card-catalog coverage measured by the worker that produced the report.

    Keeping this typed snapshot on the report lets the GUI remain a pure
    renderer while avoiding stale, hand-maintained coverage counts.
    """

    scope: str
    executable_versions: int
    total_versions: int
    blocked_versions: int

    def __post_init__(self) -> None:
        _text(self.scope, "card_coverage.scope")
        _integer(
            self.executable_versions,
            "card_coverage.executable_versions",
        )
        _integer(self.total_versions, "card_coverage.total_versions")
        _integer(self.blocked_versions, "card_coverage.blocked_versions")
        if self.executable_versions + self.blocked_versions != self.total_versions:
            raise ValueError(
                "card coverage executable and blocked counts must equal total"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "executable_versions": self.executable_versions,
            "total_versions": self.total_versions,
            "blocked_versions": self.blocked_versions,
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, object]
    ) -> "InitialRegularFixedRunCardCoverageReport":
        return cls(
            scope=_text(payload.get("scope"), "card_coverage.scope"),
            executable_versions=_integer(
                payload.get("executable_versions"),
                "card_coverage.executable_versions",
            ),
            total_versions=_integer(
                payload.get("total_versions"),
                "card_coverage.total_versions",
            ),
            blocked_versions=_integer(
                payload.get("blocked_versions"),
                "card_coverage.blocked_versions",
            ),
        )


@dataclass(frozen=True, slots=True)
class InitialRegularFixedRunStepReport:
    """A typed, scalar view of one fixed-run transition."""

    index: int
    kind: InitialRegularFixedRunStepKind
    week: int
    action: str | None
    request_kind: ExternalKind | None
    request_id: str | None
    branch: str | None
    before: InitialRegularFixedRunStateReport
    after: InitialRegularFixedRunStateReport
    action_kind: ActionKind | None = None
    branch_probability: float | None = None
    branch_rng_token: str | None = None
    branch_event_id: str | None = None
    branch_was_sp: bool | None = None

    def __post_init__(self) -> None:
        if self.index < 1:
            raise ValueError("report step index must be positive")
        if not isinstance(self.kind, InitialRegularFixedRunStepKind):
            raise TypeError("kind must be InitialRegularFixedRunStepKind")
        if self.week < 1:
            raise ValueError("report step week must be positive")
        if self.action is not None and not self.action:
            raise ValueError("report step action cannot be empty")
        if self.request_id is not None and not self.request_id:
            raise ValueError("report request_id cannot be empty")
        if self.branch is not None and not self.branch:
            raise ValueError("report branch cannot be empty")
        if self.request_kind is not None and not isinstance(self.request_kind, ExternalKind):
            raise TypeError("request_kind must be ExternalKind or None")
        if self.action_kind is not None and not isinstance(self.action_kind, ActionKind):
            raise TypeError("action_kind must be ActionKind or None")
        if self.branch_probability is not None and not 0.0 <= self.branch_probability <= 1.0:
            raise ValueError("branch_probability must be in 0..1")
        if self.branch_rng_token is not None and not self.branch_rng_token:
            raise ValueError("branch_rng_token cannot be empty")
        if self.branch_event_id is not None and not self.branch_event_id:
            raise ValueError("branch_event_id cannot be empty")
        if self.branch_was_sp is not None and not isinstance(self.branch_was_sp, bool):
            raise TypeError("branch_was_sp must be bool or None")
        if not isinstance(self.before, InitialRegularFixedRunStateReport):
            raise TypeError("before must be InitialRegularFixedRunStateReport")
        if not isinstance(self.after, InitialRegularFixedRunStateReport):
            raise TypeError("after must be InitialRegularFixedRunStateReport")

    @classmethod
    def from_step(cls, step: InitialRegularFixedRunStep) -> "InitialRegularFixedRunStepReport":
        if not isinstance(step, InitialRegularFixedRunStep):
            raise TypeError("step must be InitialRegularFixedRunStep")
        action = step.action
        request = step.request
        outcome = step.outcome
        branch = None if outcome is None else outcome.branch
        return cls(
            index=step.index,
            kind=step.kind,
            week=step.before.week,
            action=None if action is None else action.choice_id,
            action_kind=None if action is None else action.kind,
            request_kind=None if request is None else request.kind,
            request_id=None if request is None else request.request_id,
            branch=None if branch is None else branch.branch_id,
            branch_probability=None if branch is None else branch.probability,
            branch_rng_token=None if branch is None else branch.rng_token,
            branch_event_id=None if branch is None else branch.event_id,
            branch_was_sp=None if branch is None else branch.was_sp,
            before=InitialRegularFixedRunStateReport.from_state(step.before),
            after=InitialRegularFixedRunStateReport.from_state(step.after),
        )

    # ``from_run_step`` reads naturally in report-building code.
    from_run_step = from_step
    from_result_step = from_step

    @property
    def action_id(self) -> str | None:
        return self.action

    @property
    def branch_id(self) -> str | None:
        return self.branch

    # Flattened accessors keep simple table-oriented GUI code concise while
    # retaining typed ``before``/``after`` snapshots as the canonical fields.
    @property
    def before_stamina(self) -> int:
        return self.before.stamina

    @property
    def after_stamina(self) -> int:
        return self.after.stamina

    @property
    def before_vocal(self) -> int:
        return self.before.vocal

    @property
    def after_vocal(self) -> int:
        return self.after.vocal

    @property
    def before_dance(self) -> int:
        return self.before.dance

    @property
    def after_dance(self) -> int:
        return self.after.dance

    @property
    def before_visual(self) -> int:
        return self.before.visual

    @property
    def after_visual(self) -> int:
        return self.after.visual

    @property
    def before_deck_count(self) -> int:
        return self.before.deck_count

    @property
    def after_deck_count(self) -> int:
        return self.after.deck_count

    @property
    def before_phase(self) -> RolloutPhase:
        return self.before.phase

    @property
    def after_phase(self) -> RolloutPhase:
        return self.after.phase

    @property
    def before_lifecycle(self) -> Lifecycle:
        return self.before.lifecycle

    @property
    def after_lifecycle(self) -> Lifecycle:
        return self.after.lifecycle

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "kind": self.kind.value,
            "week": self.week,
            "action": self.action,
            "action_kind": None if self.action_kind is None else self.action_kind.value,
            "request_kind": None if self.request_kind is None else self.request_kind.value,
            "request_id": self.request_id,
            "branch": self.branch,
            "branch_probability": self.branch_probability,
            "branch_rng_token": self.branch_rng_token,
            "branch_event_id": self.branch_event_id,
            "branch_was_sp": self.branch_was_sp,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "InitialRegularFixedRunStepReport":
        action_kind = payload.get("action_kind")
        request_kind = payload.get("request_kind")
        return cls(
            index=_integer(payload.get("index"), "step.index", minimum=1),
            kind=_step_kind(payload.get("kind")),
            week=_integer(payload.get("week"), "step.week", minimum=1),
            action=_optional_text(payload.get("action"), "step.action"),
            action_kind=None if action_kind is None else _action_kind(action_kind),
            request_kind=None if request_kind is None else _external_kind(request_kind),
            request_id=_optional_text(payload.get("request_id"), "step.request_id"),
            branch=_optional_text(payload.get("branch"), "step.branch"),
            branch_probability=_optional_float(
                payload.get("branch_probability"), "step.branch_probability"
            ),
            branch_rng_token=_optional_text(
                payload.get("branch_rng_token"), "step.branch_rng_token"
            ),
            branch_event_id=_optional_text(
                payload.get("branch_event_id"), "step.branch_event_id"
            ),
            branch_was_sp=payload.get("branch_was_sp"),
            before=InitialRegularFixedRunStateReport.from_dict(
                _mapping(payload.get("before"), "step.before")
            ),
            after=InitialRegularFixedRunStateReport.from_dict(
                _mapping(payload.get("after"), "step.after")
            ),
        )

    @property
    def summary_text(self) -> str:
        action = "-" if self.action is None else self.action
        branch = "-" if self.branch is None else self.branch
        request = "-" if self.request_id is None else self.request_id
        return (
            f"#{self.index} week {self.week} {self.kind.value}: "
            f"action={action}; request={request}; branch={branch}; "
            f"stamina {self.before.stamina}->{self.after.stamina}; "
            f"deck {self.before.deck_count}->{self.after.deck_count}"
        )


# Shorter aliases are useful to a GUI table model and preserve discoverability.
InitialRegularFixedRunStepSnapshot = InitialRegularFixedRunStepReport
# The full report-oriented spelling is kept as the public name expected by
# consumers; ``...StepReport`` remains available for explicitness.
InitialRegularFixedRunReportStep = InitialRegularFixedRunStepReport


@dataclass(frozen=True, slots=True)
class InitialRegularFixedRunReport:
    """Complete scalar report for one already-executed fixed scenario."""

    initial_state: InitialRegularFixedRunStateReport
    final_state: InitialRegularFixedRunStateReport
    stop: InitialRegularFixedRunStop
    completed: bool
    issues: tuple[OuterSearchIssue, ...]
    steps: tuple[InitialRegularFixedRunStepReport, ...]
    initial_deck: tuple[InitialRegularFixedRunDeckEntryReport, ...] = ()
    final_deck: tuple[InitialRegularFixedRunDeckEntryReport, ...] = ()
    milestones: tuple[InitialRegularFixedRunMilestoneReport, ...] = ()
    card_coverage: InitialRegularFixedRunCardCoverageReport | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.initial_state, InitialRegularFixedRunStateReport):
            raise TypeError("initial_state must be InitialRegularFixedRunStateReport")
        if not isinstance(self.final_state, InitialRegularFixedRunStateReport):
            raise TypeError("final_state must be InitialRegularFixedRunStateReport")
        if not isinstance(self.stop, InitialRegularFixedRunStop):
            raise TypeError("stop must be InitialRegularFixedRunStop")
        if not isinstance(self.completed, bool):
            raise TypeError("completed must be bool")
        if any(not isinstance(issue, OuterSearchIssue) for issue in self.issues):
            raise TypeError("issues must contain OuterSearchIssue values")
        if any(not isinstance(step, InitialRegularFixedRunStepReport) for step in self.steps):
            raise TypeError("steps must contain InitialRegularFixedRunStepReport values")
        if any(
            not isinstance(entry, InitialRegularFixedRunDeckEntryReport)
            for entry in (*self.initial_deck, *self.final_deck)
        ):
            raise TypeError("deck reports must contain typed deck entries")
        if any(
            not isinstance(value, InitialRegularFixedRunMilestoneReport)
            for value in self.milestones
        ):
            raise TypeError("milestones must contain typed milestone reports")
        if self.card_coverage is not None and not isinstance(
            self.card_coverage, InitialRegularFixedRunCardCoverageReport
        ):
            raise TypeError(
                "card_coverage must be InitialRegularFixedRunCardCoverageReport or None"
            )
        object.__setattr__(self, "initial_deck", tuple(self.initial_deck))
        object.__setattr__(self, "final_deck", tuple(self.final_deck))
        object.__setattr__(self, "milestones", tuple(self.milestones))

    @classmethod
    def from_result(cls, result: InitialRegularFixedRunResult) -> "InitialRegularFixedRunReport":
        """Project ``result`` without running any part of the rollout again."""

        if not isinstance(result, InitialRegularFixedRunResult):
            raise TypeError("result must be InitialRegularFixedRunResult")
        return cls(
            initial_state=InitialRegularFixedRunStateReport.from_state(result.initial_state),
            final_state=InitialRegularFixedRunStateReport.from_state(result.final_state),
            stop=result.stop,
            completed=result.completed,
            issues=tuple(result.issues),
            steps=tuple(InitialRegularFixedRunStepReport.from_step(step) for step in result.steps),
            initial_deck=tuple(
                InitialRegularFixedRunDeckEntryReport.from_entry(entry)
                for entry in result.initial_state.deck
            ),
            final_deck=tuple(
                InitialRegularFixedRunDeckEntryReport.from_entry(entry)
                for entry in result.final_state.deck
            ),
        )

    from_run_result = from_result

    @property
    def initial(self) -> InitialRegularFixedRunStateReport:
        return self.initial_state

    @property
    def final(self) -> InitialRegularFixedRunStateReport:
        return self.final_state

    @property
    def final_stop(self) -> InitialRegularFixedRunStop:
        """Alias matching GUI labels that call the terminal reason a stop."""

        return self.stop

    @property
    def step_count(self) -> int:
        return len(self.steps)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": SCHEMA_NAME,
            "schema_version": SCHEMA_VERSION,
            "initial_state": self.initial_state.to_dict(),
            "final_state": self.final_state.to_dict(),
            "stop": self.stop.value,
            "completed": self.completed,
            "issues": [_issue_to_dict(issue) for issue in self.issues],
            "steps": [step.to_dict() for step in self.steps],
            "initial_deck": [entry.to_dict() for entry in self.initial_deck],
            "final_deck": [entry.to_dict() for entry in self.final_deck],
            "milestones": [value.to_dict() for value in self.milestones],
        }
        if self.card_coverage is not None:
            payload["card_coverage"] = self.card_coverage.to_dict()
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "InitialRegularFixedRunReport":
        schema = payload.get("schema")
        version = payload.get("schema_version")
        # Schema metadata is emitted by ``to_dict``.  Accepting an omitted
        # pair keeps the report convenient to embed in a larger GUI payload,
        # while still rejecting an explicitly incompatible version.
        if schema is not None and schema != SCHEMA_NAME:
            raise ValueError("unsupported fixed-run report schema")
        if version is not None and version != SCHEMA_VERSION:
            raise ValueError("unsupported fixed-run report schema version")
        raw_issues = _object_list(payload.get("issues", []), "issues")
        raw_steps = _object_list(payload.get("steps", []), "steps")
        raw_initial_deck = _object_list(
            payload.get("initial_deck", []), "initial_deck"
        )
        raw_final_deck = _object_list(
            payload.get("final_deck", []), "final_deck"
        )
        raw_milestones = _object_list(
            payload.get("milestones", []), "milestones"
        )
        completed = payload.get("completed")
        if not isinstance(completed, bool):
            raise ValueError("report.completed must be bool")
        initial_payload = payload.get("initial_state", payload.get("initial"))
        final_payload = payload.get("final_state", payload.get("final"))
        raw_card_coverage = payload.get("card_coverage")
        return cls(
            initial_state=InitialRegularFixedRunStateReport.from_dict(
                _mapping(initial_payload, "initial_state")
            ),
            final_state=InitialRegularFixedRunStateReport.from_dict(
                _mapping(final_payload, "final_state")
            ),
            stop=_stop(payload.get("stop")),
            completed=completed,
            issues=tuple(_issue_from_dict(issue) for issue in raw_issues),
            steps=tuple(InitialRegularFixedRunStepReport.from_dict(step) for step in raw_steps),
            initial_deck=tuple(
                InitialRegularFixedRunDeckEntryReport.from_dict(entry)
                for entry in raw_initial_deck
            ),
            final_deck=tuple(
                InitialRegularFixedRunDeckEntryReport.from_dict(entry)
                for entry in raw_final_deck
            ),
            milestones=tuple(
                InitialRegularFixedRunMilestoneReport.from_dict(value)
                for value in raw_milestones
            ),
            card_coverage=(
                None
                if raw_card_coverage is None
                else InitialRegularFixedRunCardCoverageReport.from_dict(
                    _mapping(raw_card_coverage, "card_coverage")
                )
            ),
        )

    @property
    def summary_text(self) -> str:
        issue_text = "none" if not self.issues else ",".join(issue.code for issue in self.issues)
        return (
            f"stop={self.stop.value}; completed={'yes' if self.completed else 'no'}; "
            f"steps={len(self.steps)}; {self.final_state.summary_text}; "
            f"issues={issue_text}"
        )

    @property
    def summary(self) -> str:
        """Compatibility alias for widgets that use ``summary`` as a label."""

        return self.summary_text


def project_initial_regular_fixed_run(
    result: InitialRegularFixedRunResult,
) -> InitialRegularFixedRunReport:
    """Return a GUI projection of an existing fixed-run result."""

    return InitialRegularFixedRunReport.from_result(result)


def build_initial_regular_fixed_run_report(
    result: InitialRegularFixedRunResult,
) -> InitialRegularFixedRunReport:
    """Explicitly named builder alias for :func:`project_initial_regular_fixed_run`."""

    return project_initial_regular_fixed_run(result)


def initial_regular_fixed_run_report(
    result: InitialRegularFixedRunResult,
) -> InitialRegularFixedRunReport:
    """Function-style alias for callers that prefer a noun-like API."""

    return project_initial_regular_fixed_run(result)


__all__ = [
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "InitialRegularFixedRunCardCoverageReport",
    "InitialRegularFixedRunDeckEntryReport",
    "InitialRegularFixedRunMilestoneReport",
    "InitialRegularFixedRunReport",
    "InitialRegularFixedRunStateReport",
    "InitialRegularFixedRunStateSnapshot",
    "InitialRegularFixedRunReportStep",
    "InitialRegularFixedRunStepReport",
    "InitialRegularFixedRunStepSnapshot",
    "build_initial_regular_fixed_run_report",
    "initial_regular_fixed_run_report",
    "project_initial_regular_fixed_run",
]
