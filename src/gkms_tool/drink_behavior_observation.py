"""Offline native drink behavior observations and prior.

Cards and drinks have different identity contracts.  A canonical leaderboard
episode stores an ordered drink inventory and ``use-drink.indexes`` that remove
one item from the still-unconsumed inventory.  The native observer's manual
``play_type == 3`` command carries the exact ``drink_id``.  This module joins
those two facts and abstains an entire episode on any mismatch, including an
out-of-range index or a native drink that could have been generated outside
the canonical inventory.

No controller, game process, or live input is used here.  The prior and LOO
evaluation are independent from the card imitation prior and never turn a
drink into a card candidate.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Literal

from .leaderboard_replay_adapter import read_leaderboard_episode
from .telemetry_journal import TelemetryEvent, read_telemetry_events


DRINK_OBSERVATION_SCHEMA = "gkms.native-drink-behavior-observation.v1"
DRINK_PRIOR_SCHEMA = "gkms.native-drink-behavior-prior.v1"
DRINK_EVALUATION_SCHEMA = "gkms.native-drink-behavior-evaluation.v1"
DRINK_TIMING_OBSERVATION_SCHEMA = "gkms.native-drink-timing-observation.v1"
DRINK_TIMING_PRIOR_SCHEMA = "gkms.native-drink-timing-prior.v1"
DRINK_TIMING_EVALUATION_SCHEMA = "gkms.native-drink-timing-evaluation.v1"
NATIVE_EXAM_ACTION_SCHEMA = "gkms.native-exam-action.v1"
DRINK_INVENTORY_CANDIDATE_SET_KIND = "drink_inventory_multiset"
DRINK_TIMING_MAX_BONUS = 80
# These are intentionally machine-readable gaps rather than an inferred
# round/state.  Leaderboard History contains action indexes but no per-action
# native scalar boundary; a live transition recorder can fill that boundary.
DRINK_TIMING_GAP_STATE_MISSING = "native-action-state-missing"
DRINK_TIMING_GAP_STATE_INCOMPLETE = "native-action-state-incomplete"
DRINK_TIMING_GAP_STATE_MISMATCH = "native-action-state-mismatch"
DRINK_TIMING_GAP_NO_ACTION = "no-use-drink-actions"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = (
    PROJECT_ROOT / "var" / "leaderboard_dataset" / "v330_nia_native_drink_behavior_v1"
)
DEFAULT_INPUT_GLOB = "*_window_verified.json"

ActionType = Literal["hand", "drink", "turn-end"]


class DrinkObservationError(ValueError):
    """An episode or native action cannot satisfy the drink contract."""


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DrinkObservationError(f"{label} must be non-empty text")
    return value.strip()


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DrinkObservationError(f"{label} must be an integer >= {minimum}")
    return value


def _sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except (FileNotFoundError, OSError):
        return None


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _pretty_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode(
        "utf-8"
    )


def _compact_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def _flow_id(produce_id: str, plan_type: str, exam_effect_type: str) -> str:
    return "|".join((produce_id, plan_type, exam_effect_type))


@dataclass(frozen=True, slots=True)
class NativeExamAction:
    """One explicit manual native action from the command-dequeue hook."""

    sequence: int
    action_type: ActionType
    index: int
    drink_id: str | None = None
    card_id: str | None = None
    card_guid: str | None = None
    stack_id: str | None = None

    def __post_init__(self) -> None:
        _integer(self.sequence, "native action sequence", minimum=1)
        _integer(self.index, "native action index")
        if self.action_type == "drink":
            _text(self.drink_id, "native drink action drink_id")
            if self.card_id is not None or self.card_guid is not None:
                raise DrinkObservationError("drink action cannot carry card identity")
        elif self.action_type == "hand":
            if not self.card_id or not self.card_guid:
                raise DrinkObservationError("hand action requires card identity")
            if self.drink_id is not None:
                raise DrinkObservationError("hand action cannot carry drink identity")
        elif self.action_type == "turn-end":
            if self.index != 0 or any(
                value is not None for value in (self.drink_id, self.card_id, self.card_guid)
            ):
                raise DrinkObservationError("turn-end action identity is invalid")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema": NATIVE_EXAM_ACTION_SCHEMA,
            "sequence": self.sequence,
            "action_type": self.action_type,
            "index": self.index,
        }
        if self.drink_id is not None:
            result["drink_id"] = self.drink_id
        if self.card_id is not None:
            result["card_id"] = self.card_id
        if self.card_guid is not None:
            result["card_guid"] = self.card_guid
        if self.stack_id is not None:
            result["stack_id"] = self.stack_id
        return result

def _optional_text_tuple(value: object, label: str) -> tuple[str, ...] | None:
    """Read an optional ordered ID list without turning missing into empty."""

    if value is None:
        return None
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise DrinkObservationError(f"{label} must be an array or null")
    return tuple(_text(item, f"{label}[]") for item in value)


def _action_type(value: object, label: str) -> ActionType:
    aliases = {
        "hand": "hand",
        "use-hand": "hand",
        "use_hand": "hand",
        "UseHand": "hand",
        "drink": "drink",
        "use-drink": "drink",
        "use_drink": "drink",
        "UseDrink": "drink",
        "turn-end": "turn-end",
        "turn_end": "turn-end",
        "TurnEnd": "turn-end",
    }
    if isinstance(value, str) and aliases.get(value) in {
        "hand",
        "drink",
        "turn-end",
    }:
        return aliases[value]  # type: ignore[return-value]
    raise DrinkObservationError(f"{label} has unsupported action type")


@dataclass(frozen=True, slots=True)
class NativeExamActionState:
    """One explicit native before/after boundary for a manual action.

    ``ExamAction`` history supplies only a slot index.  This record is the
    smallest live-recorder supplement that can bind that index to a native
    round and scalar state.  It deliberately keeps ``None`` distinct from an
    empty drink list: a missing list is a provenance gap, not an empty
    inventory inferred from the action.
    """

    sequence: int
    manual_command_sequence: int
    action_type: ActionType
    index: int
    before: Mapping[str, object] | None
    after: Mapping[str, object] | None
    available_drink_ids: tuple[str, ...] | None = None
    available_drink_ids_after: tuple[str, ...] | None = None
    drink_id: str | None = None
    state_complete: bool = True
    source: str = "native-action-state"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "action_type",
            _action_type(self.action_type, "native action-state.action_type"),
        )
        _integer(self.sequence, "native action-state sequence", minimum=1)
        _integer(
            self.manual_command_sequence,
            "native action-state manual_command_sequence",
            minimum=1,
        )
        _integer(self.index, "native action-state index")
        if self.before is not None and not isinstance(self.before, Mapping):
            raise DrinkObservationError("native action-state before must be an object or null")
        if self.after is not None and not isinstance(self.after, Mapping):
            raise DrinkObservationError("native action-state after must be an object or null")
        for value, label in (
            (self.available_drink_ids, "available_drink_ids"),
            (self.available_drink_ids_after, "available_drink_ids_after"),
        ):
            if value is not None and any(
                not isinstance(item, str) or not item for item in value
            ):
                raise DrinkObservationError(
                    f"native action-state {label} has an invalid drink ID"
                )
        if self.drink_id is not None:
            _text(self.drink_id, "native action-state drink_id")
        if type(self.state_complete) is not bool:
            raise DrinkObservationError("native action-state state_complete must be bool")
        _text(self.source, "native action-state source")

    @property
    def round_number(self) -> int | None:
        value = None if self.before is None else self.before.get("current_turn")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def turn(self) -> int | None:
        """Compatibility alias for callers that call the round a turn."""

        return self.round_number

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema": DRINK_TIMING_OBSERVATION_SCHEMA,
            "sequence": self.sequence,
            "manual_command_sequence": self.manual_command_sequence,
            "action_type": self.action_type,
            "index": self.index,
            "before": None if self.before is None else dict(self.before),
            "after": None if self.after is None else dict(self.after),
            "state_complete": self.state_complete,
            "source": self.source,
        }
        if self.available_drink_ids is not None:
            result["available_drink_ids"] = list(self.available_drink_ids)
        if self.available_drink_ids_after is not None:
            result["available_drink_ids_after"] = list(
                self.available_drink_ids_after
            )
        if self.drink_id is not None:
            result["drink_id"] = self.drink_id
        if self.round_number is not None:
            result["round"] = self.round_number
        return result


def _native_action_state_from_mapping(
    value: Mapping[str, object],
    *,
    default_sequence: int = 1,
) -> NativeExamActionState:
    """Normalize an action-state row emitted by a live recorder."""

    if not isinstance(value, Mapping):
        # Keep malformed supplements on the explicit ValueError contract used
        # by the projection boundary.  Without this guard a scalar reaches
        # ``value.get`` and leaks an unhelpful AttributeError to callers.
        raise DrinkObservationError("native_action_states row must be an object")

    action_type = _action_type(
        value.get("action_type", value.get("kind")),
        "native action-state.action_type",
    )
    manual_sequence = value.get(
        "manual_command_sequence",
        value.get("manual_sequence", value.get("command_sequence")),
    )
    sequence = value.get("sequence", value.get("state_sequence", default_sequence))
    index = value.get("index", value.get("play_index", 0))
    before = value.get("before", value.get("state_before"))
    after = value.get("after", value.get("state_after"))
    if manual_sequence is None:
        raise DrinkObservationError(
            "native action-state.manual_command_sequence is missing"
        )
    return NativeExamActionState(
        sequence=_integer(sequence, "native action-state.sequence", minimum=1),
        manual_command_sequence=_integer(
            manual_sequence,
            "native action-state.manual_command_sequence",
            minimum=1,
        ),
        action_type=action_type,
        index=_integer(index, "native action-state.play_index"),
        before=before if isinstance(before, Mapping) else None,
        after=after if isinstance(after, Mapping) else None,
        available_drink_ids=_optional_text_tuple(
            value.get(
                "available_drink_ids",
                value.get(
                    "available_drink_ids_before",
                    value.get("drink_inventory", value.get("candidate_drink_ids")),
                ),
            ),
            "native action-state.available_drink_ids",
        ),
        available_drink_ids_after=_optional_text_tuple(
            value.get(
                "available_drink_ids_after",
                value.get("remaining_drink_ids"),
            ),
            "native action-state.available_drink_ids_after",
        ),
        drink_id=(
            value.get("drink_id", value.get("chosen_drink_id"))
            if isinstance(value.get("drink_id", value.get("chosen_drink_id")), str)
            else None
        ),
        state_complete=value.get("state_complete", True),
        source=(
            value.get("source", "native-action-state")
            if isinstance(value.get("source", "native-action-state"), str)
            else "native-action-state"
        ),
    )


def classify_manual_command(
    command: Mapping[str, object],
    *,
    sequence: int,
    stack_id: str | None = None,
) -> NativeExamAction:
    """Classify play type 2/3/12 without conflating cards and drinks."""

    if command.get("manual") is not True:
        raise DrinkObservationError("native command is not manual")
    play_type = _integer(command.get("play_type"), "native command play_type")
    index = _integer(command.get("play_index"), "native command play_index")
    if play_type == 2:
        card = command.get("card")
        if not isinstance(card, Mapping):
            raise DrinkObservationError("manual hand command has no card")
        return NativeExamAction(
            sequence,
            "hand",
            index,
            card_id=_text(card.get("id"), "manual hand card.id"),
            card_guid=_text(card.get("guid"), "manual hand card.guid"),
            stack_id=stack_id,
        )
    if play_type == 3:
        return NativeExamAction(
            sequence,
            "drink",
            index,
            drink_id=_text(command.get("drink_id"), "manual drink command.drink_id"),
            stack_id=stack_id,
        )
    if play_type == 12:
        if index != 0:
            raise DrinkObservationError("turn-end play_index must be zero")
        return NativeExamAction(sequence, "turn-end", 0, stack_id=stack_id)
    raise DrinkObservationError(f"unsupported manual play_type:{play_type}")


def extract_native_exam_actions(
    source: str | Path | Sequence[TelemetryEvent],
    *,
    sequence_start: int | None = None,
    sequence_end: int | None = None,
    stack_id: str | None = None,
) -> tuple[NativeExamAction, ...]:
    """Extract exact manual hand/drink/turn-end actions from one journal range."""

    events = (
        read_telemetry_events(source)
        if isinstance(source, (str, Path))
        else tuple(source)
    )
    if any(not isinstance(event, TelemetryEvent) for event in events):
        raise DrinkObservationError("native event source contains a non-TelemetryEvent")
    if sequence_start is not None:
        _integer(sequence_start, "sequence_start", minimum=1)
    if sequence_end is not None:
        _integer(sequence_end, "sequence_end", minimum=1)
    if sequence_start is not None and sequence_end is not None and sequence_end < sequence_start:
        raise DrinkObservationError("sequence_end precedes sequence_start")
    output: list[NativeExamAction] = []
    for event in events:
        if event.kind != "command_dequeue" or event.hook != "ExamCommandStack.RemoveCurrentCommand":
            continue
        if sequence_start is not None and event.sequence < sequence_start:
            continue
        if sequence_end is not None and event.sequence > sequence_end:
            continue
        raw_stack = event.data.get("stack")
        if stack_id is not None and raw_stack != stack_id:
            continue
        command = event.data.get("command")
        if not isinstance(command, Mapping) or command.get("manual") is not True:
            continue
        try:
            output.append(
                classify_manual_command(
                    command,
                    sequence=event.sequence,
                    stack_id=stack_id if stack_id is not None else (
                        raw_stack if isinstance(raw_stack, str) else None
                    ),
                )
            )
        except DrinkObservationError as error:
            # Unsupported non-drink manual commands are still part of the
            # action stream; malformed/unknown commands must not be silently
            # omitted from an exact drink join.
            raise DrinkObservationError(f"sequence={event.sequence}:{error}") from error
    return tuple(output)


def extract_native_action_states(
    source: str | Path | Sequence[TelemetryEvent],
    *,
    sequence_start: int | None = None,
    sequence_end: int | None = None,
    stack_id: str | None = None,
) -> tuple[NativeExamActionState, ...]:
    """Extract explicit action-state rows from a bounded native journal.

    Protocol-3 ``action_state`` rows are emitted by the read-only transition
    recorder after ``RemoveDrink``/card execution/turn-end.  They are joined
    by the recorder's ``manual_command_sequence``; journal adjacency and
    action order are never used as a substitute.  A malformed row is raised
    so a dataset builder can retain the baseline drink observation while
    reporting a timing gap.
    """

    events = (
        read_telemetry_events(source)
        if isinstance(source, (str, Path))
        else tuple(source)
    )
    if any(not isinstance(event, TelemetryEvent) for event in events):
        raise DrinkObservationError("native event source contains a non-TelemetryEvent")
    if sequence_start is not None:
        _integer(sequence_start, "sequence_start", minimum=1)
    if sequence_end is not None:
        _integer(sequence_end, "sequence_end", minimum=1)
    if sequence_start is not None and sequence_end is not None and sequence_end < sequence_start:
        raise DrinkObservationError("sequence_end precedes sequence_start")

    relevant: list[NativeExamActionState] = []
    manual_by_sequence = {
        event.sequence: event
        for event in events
        if event.kind == "command_dequeue"
        and event.hook == "ExamCommandStack.RemoveCurrentCommand"
    }
    for event in events:
        if event.kind != "action_state" or event.hook not in {
            "ExamSequence.ExecuteCardCommandImpl",
            "ExamParameterModel.RemoveDrink",
            "ExamParameterModel.set_Phase",
        }:
            continue
        if sequence_start is not None and event.sequence < sequence_start:
            continue
        if sequence_end is not None and event.sequence > sequence_end:
            continue
        data = event.data
        # A state hook may be present in a broad journal window for a
        # different command stack.  The explicit stack check is made through
        # the bound command below when the recorder provided it; no guessed
        # stack identity is attached to the state row itself.
        manual_sequence = data.get("manual_command_sequence")
        if not isinstance(manual_sequence, int) or isinstance(manual_sequence, bool) or manual_sequence <= 0:
            raise DrinkObservationError(
                f"sequence={event.sequence}:action-state-manual-sequence-missing"
            )
        if sequence_start is not None and manual_sequence < sequence_start:
            # The state hook can be the first event in a caller's window while
            # its explicit command binding sits just before the window.  It
            # cannot prove an in-window action and is therefore omitted from
            # the supplement (the join will report a missing state if needed).
            continue
        if sequence_end is not None and manual_sequence > sequence_end:
            continue
        manual_event = manual_by_sequence.get(manual_sequence)
        command = None if manual_event is None else manual_event.data.get("command")
        if not isinstance(command, Mapping) or command.get("manual") is not True:
            raise DrinkObservationError(
                f"sequence={event.sequence}:action-state-manual-command-missing"
            )
        raw_stack = None if manual_event is None else manual_event.data.get("stack")
        if stack_id is not None and raw_stack != stack_id:
            continue
        if manual_event is not None and manual_event.thread_id != event.thread_id:
            raise DrinkObservationError(
                f"sequence={event.sequence}:action-state-thread-mismatch"
            )
        play_type = command.get("play_type")
        expected_type = {2: "hand", 3: "drink", 12: "turn-end"}.get(play_type)
        if expected_type is None:
            raise DrinkObservationError(
                f"sequence={event.sequence}:action-state-command-type-unsupported"
            )
        action_type = _action_type(data.get("action_type", expected_type), "action-state.action_type")
        if action_type != expected_type:
            raise DrinkObservationError(
                f"sequence={event.sequence}:action-state-command-type-mismatch"
            )
        index = _integer(
            data.get("play_index", command.get("play_index")),
            "action-state.play_index",
        )
        if (
            expected_type == "drink"
            and index
            != _integer(command.get("play_index"), "manual command.play_index")
        ):
            raise DrinkObservationError(
                f"sequence={event.sequence}:action-state-index-mismatch"
            )
        before = event.before
        after = event.after
        if before is not None and not isinstance(before, Mapping):
            raise DrinkObservationError(
                f"sequence={event.sequence}:action-state-before-invalid"
            )
        if after is not None and not isinstance(after, Mapping):
            raise DrinkObservationError(
                f"sequence={event.sequence}:action-state-after-invalid"
            )
        relevant.append(
            NativeExamActionState(
                sequence=event.sequence,
                manual_command_sequence=manual_sequence,
                action_type=action_type,
                index=index,
                before=before,
                after=after,
                available_drink_ids=_optional_text_tuple(
                    data.get("available_drink_ids"),
                    f"sequence={event.sequence}:available_drink_ids",
                ),
                available_drink_ids_after=_optional_text_tuple(
                    data.get("available_drink_ids_after"),
                    f"sequence={event.sequence}:available_drink_ids_after",
                ),
                drink_id=(
                    data.get("drink_id")
                    if isinstance(data.get("drink_id"), str)
                    else None
                ),
                state_complete=data.get("state_complete", False),
                source=f"telemetry:{event.hook}",
            )
        )
    return tuple(relevant)


_REQUIRED_TIMING_STATE_FIELDS = (
    "current_turn",
    "remain_turn",
    "score",
    "stamina",
    "block",
    "exam_card_play_count",
    "turn_card_play_count",
)


def _validate_timing_state_map(
    state: Mapping[str, object],
    label: str,
    *,
    require_parameter: bool = True,
) -> None:
    """Validate the compact native scalar boundary used by timing prior."""

    for name in _REQUIRED_TIMING_STATE_FIELDS:
        value = state.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DrinkObservationError(f"{label}.{name} is missing or invalid")
    if state.get("current_turn", 0) < 1:
        raise DrinkObservationError(f"{label}.current_turn must be positive")
    if require_parameter:
        parameter = state.get(
            "parameter",
            state.get("parameter_type", state.get("current_parameter")),
        )
        if parameter is None or isinstance(parameter, bool):
            raise DrinkObservationError(f"{label}.parameter is missing or invalid")


def _validate_drink_timing_state(
    before: Mapping[str, object],
    after: Mapping[str, object],
    *,
    round_number: int | None,
    candidate_before: Sequence[str],
    candidate_after: Sequence[str],
    chosen: str,
    drink_index: int,
) -> None:
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        raise DrinkObservationError("drink timing state boundary is incomplete")
    _validate_timing_state_map(before, "state_before")
    _validate_timing_state_map(after, "state_after")
    if round_number is None or round_number != before.get("current_turn"):
        raise DrinkObservationError(
            "drink timing round must equal state_before.current_turn"
        )
    if after.get("current_turn", 0) < 1:
        raise DrinkObservationError("state_after.current_turn must be positive")
    if not isinstance(drink_index, int) or isinstance(drink_index, bool) or drink_index < 0:
        raise DrinkObservationError("drink timing index is invalid")
    if drink_index >= len(candidate_before):
        raise DrinkObservationError("drink timing index is outside candidate inventory")
    if candidate_before[drink_index] != chosen:
        raise DrinkObservationError(
            "drink timing chosen ID does not match candidate inventory index"
        )
    expected_after = (
        tuple(candidate_before[:drink_index])
        + tuple(candidate_before[drink_index + 1 :])
    )
    if tuple(candidate_after) != expected_after:
        raise DrinkObservationError(
            "drink timing after inventory does not remove exactly one selected drink"
        )


@dataclass(frozen=True, slots=True)
class DrinkBehaviorObservation:
    """One exact drink choice over a dynamic inventory multiset."""

    episode_id: str
    trajectory_id: str
    produce_id: str
    idol_card_id: str
    plan_type: str
    exam_effect_type: str
    stage: str
    action_order: int
    native_sequence: int
    drink_index: int
    candidate_drink_ids: tuple[str, ...]
    chosen_drink_id: str
    terminal_score: int
    # Leaderboard replay v2 does not carry these fields.  They are optional
    # until a live transition recorder supplies an explicit native boundary;
    # an absent boundary is represented as a gap, never as a reconstructed
    # round or scalar state.
    round_number: int | None = None
    state_before: Mapping[str, object] | None = None
    state_after: Mapping[str, object] | None = None
    candidate_drink_ids_after: tuple[str, ...] | None = None
    state_source: str | None = None
    state_complete: bool = False
    # Optional cross-source binding.  Canonical leaderboard rows historically
    # lacked this field, while managed headless observations have an explicit
    # history identity that must travel with the timing row.
    history_identity: str | None = None

    @property
    def flow_id(self) -> str:
        return _flow_id(self.produce_id, self.plan_type, self.exam_effect_type)

    @property
    def source_history_identity(self) -> str | None:
        """Compatibility alias for the managed bridge provenance field."""

        return self.history_identity

    def __post_init__(self) -> None:
        _text(self.episode_id, "observation.episode_id")
        _text(self.trajectory_id, "observation.trajectory_id")
        _text(self.produce_id, "observation.produce_id")
        _text(self.idol_card_id, "observation.idol_card_id")
        _text(self.plan_type, "observation.plan_type")
        _text(self.exam_effect_type, "observation.exam_effect_type")
        _text(self.stage, "observation.stage")
        _integer(self.action_order, "observation.action_order")
        _integer(self.native_sequence, "observation.native_sequence", minimum=1)
        _integer(self.drink_index, "observation.drink_index")
        _integer(self.terminal_score, "observation.terminal_score", minimum=1)
        if not self.candidate_drink_ids:
            raise DrinkObservationError("observation candidate inventory is empty")
        if any(not isinstance(value, str) or not value for value in self.candidate_drink_ids):
            raise DrinkObservationError("observation candidate inventory has invalid drink")
        if self.chosen_drink_id not in self.candidate_drink_ids:
            raise DrinkObservationError("chosen drink is outside candidate inventory")
        if self.round_number is not None:
            _integer(self.round_number, "observation.round_number", minimum=1)
        if self.state_before is not None and not isinstance(
            self.state_before, Mapping
        ):
            raise DrinkObservationError("observation.state_before must be an object")
        if self.state_after is not None and not isinstance(
            self.state_after, Mapping
        ):
            raise DrinkObservationError("observation.state_after must be an object")
        if self.candidate_drink_ids_after is not None:
            if any(
                not isinstance(value, str) or not value
                for value in self.candidate_drink_ids_after
            ):
                raise DrinkObservationError(
                    "observation candidate inventory after has an invalid drink"
                )
        if self.state_source is not None:
            _text(self.state_source, "observation.state_source")
        if self.history_identity is not None:
            _text(self.history_identity, "observation.history_identity")
        if type(self.state_complete) is not bool:
            raise DrinkObservationError("observation.state_complete must be bool")
        supplied_state = (
            self.round_number is not None
            or self.state_before is not None
            or self.state_after is not None
            or self.candidate_drink_ids_after is not None
        )
        if self.state_complete:
            if not (
                self.round_number is not None
                and self.state_before is not None
                and self.state_after is not None
                and self.candidate_drink_ids_after is not None
            ):
                raise DrinkObservationError(
                    "complete drink timing observation is missing state fields"
                )
            _validate_drink_timing_state(
                self.state_before,
                self.state_after,
                round_number=self.round_number,
                candidate_before=self.candidate_drink_ids,
                candidate_after=self.candidate_drink_ids_after,
                chosen=self.chosen_drink_id,
                drink_index=self.drink_index,
            )
        elif supplied_state:
            # A partial row can be retained for an audit/gap report, but it is
            # never eligible for the timing prior.
            if self.round_number is not None and self.state_before is not None:
                current_turn = self.state_before.get("current_turn")
                if current_turn != self.round_number:
                    raise DrinkObservationError(
                        "partial timing observation round disagrees with state"
                    )

    @property
    def is_state_conditioned(self) -> bool:
        return self.state_complete

    @property
    def timing_available(self) -> bool:
        """Compatibility alias for callers that use timing terminology."""

        return self.is_state_conditioned

    @property
    def round(self) -> int | None:
        """Return the explicit native turn; never derive it from action order."""

        return self.round_number

    @property
    def remaining_drink_ids(self) -> tuple[str, ...] | None:
        """Alias for the recorder's post-use inventory projection."""

        return self.candidate_drink_ids_after

    @property
    def native_state_before(self) -> Mapping[str, object] | None:
        return self.state_before

    @property
    def native_state_after(self) -> Mapping[str, object] | None:
        return self.state_after

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema": DRINK_OBSERVATION_SCHEMA,
            "episode_id": self.episode_id,
            "trajectory_id": self.trajectory_id,
            "produce_id": self.produce_id,
            "idol_card_id": self.idol_card_id,
            "plan_type": self.plan_type,
            "exam_effect_type": self.exam_effect_type,
            "flow": self.flow_id,
            "stage": self.stage,
            "action_order": self.action_order,
            "native_sequence": self.native_sequence,
            "drink_index": self.drink_index,
            "candidate_drink_ids": list(self.candidate_drink_ids),
            "candidate_set_kind": DRINK_INVENTORY_CANDIDATE_SET_KIND,
            "chosen_drink_id": self.chosen_drink_id,
            "terminal_score": self.terminal_score,
            "behavior_only": True,
            "state_conditioned": self.is_state_conditioned,
        }
        if self.history_identity is not None:
            result["history_identity"] = self.history_identity
            result["source_history_identity"] = self.history_identity
        if self.is_state_conditioned:
            result.update(
                {
                    "timing_schema": DRINK_TIMING_OBSERVATION_SCHEMA,
                    "round": self.round_number,
                    "state_before": dict(self.state_before or {}),
                    "state_after": dict(self.state_after or {}),
                    "candidate_drink_ids_after": list(
                        self.candidate_drink_ids_after or ()
                    ),
                    "remaining_drink_ids": list(
                        self.candidate_drink_ids_after or ()
                    ),
                    "state_source": self.state_source,
                    "state_complete": True,
                }
            )
        return result

    def to_timing_dict(self) -> dict[str, object]:
        """Serialize the same row under the dedicated timing schema."""

        if not self.is_state_conditioned:
            raise DrinkObservationError(
                "identity-only observation has no timing serialization"
            )
        result = self.to_dict()
        result["schema"] = DRINK_TIMING_OBSERVATION_SCHEMA
        result.pop("timing_schema", None)
        return result


@dataclass(frozen=True, slots=True)
class DrinkEpisodeProjection:
    """Whole-episode acceptance/rejection evidence."""

    episode_id: str | None
    accepted: bool
    reason: str | None
    expected_drink_action_count: int
    native_drink_action_count: int
    initial_drink_ids: tuple[str, ...] = ()
    remaining_drink_ids: tuple[str, ...] = ()
    observations: tuple[DrinkBehaviorObservation, ...] = ()
    timing_observations: tuple[DrinkBehaviorObservation, ...] = ()
    timing_gaps: tuple[str, ...] = ()

    @property
    def state_conditioned(self) -> bool:
        return bool(self.timing_observations) and not self.timing_gaps

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": DRINK_OBSERVATION_SCHEMA,
            "episode_id": self.episode_id,
            "accepted": self.accepted,
            "reason": self.reason,
            "expected_drink_action_count": self.expected_drink_action_count,
            "native_drink_action_count": self.native_drink_action_count,
            "initial_drink_ids": list(self.initial_drink_ids),
            "remaining_drink_ids": list(self.remaining_drink_ids),
            "observations": [value.to_dict() for value in self.observations],
            "timing_observations": [
                value.to_timing_dict() for value in self.timing_observations
            ],
            "timing_observation_count": len(self.timing_observations),
            "timing_gaps": list(self.timing_gaps),
            "timing_gap": self.timing_gaps[0] if self.timing_gaps else None,
            "gap": self.timing_gaps[0] if self.timing_gaps else None,
            "state_conditioned": self.state_conditioned,
            "behavior_only": True,
        }


def _canonical_drink_actions(payload: Mapping[str, object]) -> tuple[tuple[int, int], ...]:
    actions = payload.get("actions")
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes)):
        raise DrinkObservationError("canonical actions are not an array")
    ordered: list[tuple[int, Mapping[str, object]]] = []
    seen: set[int] = set()
    for position, raw in enumerate(actions):
        if not isinstance(raw, Mapping):
            raise DrinkObservationError(f"canonical action is not an object:{position}")
        order = _integer(raw.get("order", position), f"canonical action order:{position}")
        if order in seen:
            raise DrinkObservationError("duplicate canonical action order")
        seen.add(order)
        ordered.append((order, raw))
    ordered.sort(key=lambda value: value[0])
    result: list[tuple[int, int]] = []
    for order, raw in ordered:
        if raw.get("action_type") not in {"use-drink", "use_drink", "ExamActionTypeUseDrink", "ExamActionType_UseDrink", "ExamActionType_Use_Drink"}:
            continue
        indexes = raw.get("indexes")
        if not isinstance(indexes, Sequence) or isinstance(indexes, (str, bytes)) or len(indexes) != 1:
            raise DrinkObservationError(f"canonical drink action index invalid:{order}")
        result.append((order, _integer(indexes[0], f"canonical drink index:{order}")))
    return tuple(result)


def project_drink_episode(
    payload: Mapping[str, object],
    native_actions: Sequence[NativeExamAction],
    native_action_states: Sequence[
        NativeExamActionState | Mapping[str, object]
    ] | None = None,
    *,
    native_states: Sequence[NativeExamActionState | Mapping[str, object]] | None = None,
) -> DrinkEpisodeProjection:
    """Replay dynamic drink inventory and join native exact drink IDs.

    The optional ``native_action_states`` argument is a live-transition
    supplement.  Canonical leaderboard History can prove the selected drink
    and its within-stage action order, but not the native round/scalar state
    immediately before that action.  When the supplement is absent this
    function keeps the proven baseline observations and emits an explicit
    timing gap; it never derives a round from ``action_order``.
    """

    if not isinstance(payload, Mapping):
        converter = getattr(payload, "to_dict", None)
        if not callable(converter):
            raise TypeError("payload must be a mapping or typed episode")
        payload = converter()
        if not isinstance(payload, Mapping):
            raise TypeError("typed episode to_dict() must return a mapping")
    if native_action_states is not None and native_states is not None:
        raise TypeError("native_action_states and native_states disagree")
    if native_action_states is None:
        native_action_states = native_states
    episode_id = payload.get("episode_id")
    try:
        episode_id = _text(episode_id, "episode_id")
        typed = read_leaderboard_episode(payload)
        canonical_actions = _canonical_drink_actions(payload)
        raw_initial = payload.get("produce_drink_ids")
        if not isinstance(raw_initial, Sequence) or isinstance(raw_initial, (str, bytes)):
            raise DrinkObservationError("produce_drink_ids is not an array")
        initial = tuple(_text(value, "produce_drink_ids[]") for value in raw_initial)
        if not initial:
            raise DrinkObservationError("missing-produce-drink-inventory")
        native_drinks = tuple(value for value in native_actions if value.action_type == "drink")
        expected_count = len(canonical_actions)
        native_count = len(native_drinks)
        if expected_count == 0:
            return DrinkEpisodeProjection(
                episode_id,
                False,
                "no-use-drink-actions",
                expected_count,
                native_count,
                initial_drink_ids=initial,
                timing_gaps=(DRINK_TIMING_GAP_NO_ACTION,),
            )
        if native_count != expected_count:
            return DrinkEpisodeProjection(
                episode_id,
                False,
                "drink-action-count-mismatch",
                expected_count,
                native_count,
                initial_drink_ids=initial,
            )
        inventory = list(initial)
        observations: list[DrinkBehaviorObservation] = []
        for (order, drink_index), native in zip(canonical_actions, native_drinks, strict=True):
            if native.index != drink_index:
                return DrinkEpisodeProjection(
                    episode_id,
                    False,
                    "drink-index-mismatch",
                    expected_count,
                    native_count,
                    initial_drink_ids=initial,
                )
            if drink_index >= len(inventory):
                return DrinkEpisodeProjection(
                    episode_id,
                    False,
                    "drink-index-out-of-range",
                    expected_count,
                    native_count,
                    initial_drink_ids=initial,
                )
            candidate = tuple(inventory)
            chosen = inventory.pop(drink_index)
            if native.drink_id != chosen:
                return DrinkEpisodeProjection(
                    episode_id,
                    False,
                    "drink-id-mismatch-or-generated-drink",
                    expected_count,
                    native_count,
                    initial_drink_ids=initial,
                )
            observations.append(
                DrinkBehaviorObservation(
                    episode_id=episode_id,
                    trajectory_id=typed.trajectory_id,
                    produce_id=typed.produce_id,
                    idol_card_id=typed.idol_card_id,
                    plan_type=str(typed.plan_type),
                    exam_effect_type=str(typed.exam_effect_type),
                    stage=str(typed.step_type),
                    action_order=order,
                    native_sequence=native.sequence,
                    drink_index=drink_index,
                    candidate_drink_ids=candidate,
                    chosen_drink_id=chosen,
                    terminal_score=typed.terminal_score,
                )
            )
        # State-conditioned timing is a separate, stricter projection.  A
        # missing or incomplete state never invalidates the exact identity
        # observations above, but it does invalidate timing for the whole
        # episode so one unknown boundary cannot reindex later uses.
        timing_gaps: list[str] = []
        timing_observations: list[DrinkBehaviorObservation] = []
        if native_action_states is None:
            timing_gaps.append(DRINK_TIMING_GAP_STATE_MISSING)
        elif not isinstance(native_action_states, Sequence) or isinstance(
            native_action_states, (str, bytes)
        ):
            # A scalar supplement is malformed input, not an empty recorder.
            # Convert it to the same explicit gap as any other incomplete
            # recorder while retaining the independently proven identity rows.
            timing_gaps.append(DRINK_TIMING_GAP_STATE_INCOMPLETE)
        else:
            normalized_states: list[NativeExamActionState] = []
            try:
                for index, raw_state in enumerate(native_action_states):
                    normalized_states.append(
                        raw_state
                        if isinstance(raw_state, NativeExamActionState)
                        else _native_action_state_from_mapping(
                            raw_state,
                            default_sequence=index + 1,
                        )
                    )
            except (AttributeError, DrinkObservationError, TypeError, ValueError):
                timing_gaps.append(DRINK_TIMING_GAP_STATE_INCOMPLETE)
                normalized_states = []
            by_manual: dict[int, NativeExamActionState] = {}
            duplicate_state = False
            for state in normalized_states:
                if state.manual_command_sequence in by_manual:
                    duplicate_state = True
                    break
                by_manual[state.manual_command_sequence] = state
            if duplicate_state:
                timing_gaps.append(DRINK_TIMING_GAP_STATE_MISMATCH)
            else:
                for observation, native in zip(
                    observations,
                    native_drinks,
                    strict=True,
                ):
                    state = by_manual.get(native.sequence)
                    if state is None:
                        timing_gaps.append(DRINK_TIMING_GAP_STATE_MISSING)
                        continue
                    if (
                        state.action_type != "drink"
                        or state.index != native.index
                        or state.drink_id != native.drink_id
                        or state.available_drink_ids
                        != observation.candidate_drink_ids
                    ):
                        timing_gaps.append(DRINK_TIMING_GAP_STATE_MISMATCH)
                        continue
                    try:
                        timing_observations.append(
                            replace(
                                observation,
                                round_number=(
                                    state.round_number
                                    if state.round_number is not None
                                    else None
                                ),
                                state_before=state.before,
                                state_after=state.after,
                                candidate_drink_ids_after=(
                                    state.available_drink_ids_after
                                ),
                                state_source=state.source,
                                state_complete=state.state_complete,
                            )
                        )
                    except (AttributeError, DrinkObservationError, TypeError, ValueError):
                        timing_gaps.append(DRINK_TIMING_GAP_STATE_INCOMPLETE)
                # Timing observations are episode-atomic.  Keep no partial
                # timing rows if any drink action lacks a complete boundary.
                if timing_gaps:
                    timing_observations = []
        if not observations:
            timing_gaps.append(DRINK_TIMING_GAP_NO_ACTION)
        if timing_gaps:
            timing_observations = []
        return DrinkEpisodeProjection(
            episode_id,
            True,
            None,
            expected_count,
            native_count,
            initial_drink_ids=initial,
            remaining_drink_ids=tuple(inventory),
            observations=tuple(observations),
            timing_observations=tuple(timing_observations),
            timing_gaps=tuple(dict.fromkeys(timing_gaps)),
        )
    except (
        AttributeError,
        DrinkObservationError,
        KeyError,
        OSError,
        TypeError,
        ValueError,
    ) as error:
        reason = str(error) or "malformed-drink-episode"
        return DrinkEpisodeProjection(
            episode_id if isinstance(episode_id, str) else None,
            False,
            reason,
            0,
            sum(value.action_type == "drink" for value in native_actions),
        )


@dataclass(frozen=True, slots=True)
class DrinkBehaviorPrior:
    """Terminal-score-weighted prior over dynamic drink inventory choices."""

    observations: tuple[DrinkBehaviorObservation, ...]
    context_weights: Mapping[tuple[str, str, int, str], float]
    maximum_score_by_flow: Mapping[str, int]
    schema: str = DRINK_PRIOR_SCHEMA

    @classmethod
    def from_observations(
        cls,
        observations: Sequence[DrinkBehaviorObservation],
    ) -> "DrinkBehaviorPrior":
        values = tuple(observations)
        maximum: dict[str, int] = {}
        for observation in values:
            maximum[observation.flow_id] = max(
                maximum.get(observation.flow_id, 0), observation.terminal_score
            )
        weights: defaultdict[tuple[str, str, int, str], float] = defaultdict(float)
        for observation in values:
            max_score = maximum[observation.flow_id]
            weight = 1.0 + 4.0 * observation.terminal_score / max_score
            weights[
                (
                    observation.flow_id,
                    observation.stage,
                    observation.action_order,
                    observation.chosen_drink_id,
                )
            ] += weight
        return cls(values, dict(weights), dict(maximum))

    def score_for(
        self,
        *,
        flow_id: str,
        stage: str,
        action_order: int,
        drink_id: str,
    ) -> int:
        selected = {
            key[3]: value
            for key, value in self.context_weights.items()
            if key[:3] == (flow_id, stage, action_order)
        }
        if not selected:
            # Action orders are native replay positions and can be sparse
            # across runs.  Fall back only within the same flow/stage; never
            # borrow another archetype or turn sequence.
            selected = {
                key[3]: value
                for key, value in self.context_weights.items()
                if key[0] == flow_id and key[1] == stage
            }
        total = sum(selected.values())
        if total <= 0:
            return 0
        return max(0, min(1000, round(1000 * selected.get(drink_id, 0.0) / total)))

    def rank(self, observation: DrinkBehaviorObservation) -> tuple[str, ...]:
        candidates = tuple(dict.fromkeys(observation.candidate_drink_ids))
        scored = {
            drink_id: self.score_for(
                flow_id=observation.flow_id,
                stage=observation.stage,
                action_order=observation.action_order,
                drink_id=drink_id,
            )
            for drink_id in candidates
        }
        if not any(scored.values()):
            return ()
        return tuple(
            sorted(
                (drink_id for drink_id in candidates if scored[drink_id] > 0),
                key=lambda value: (-scored[value], candidates.index(value)),
            )
        )

    @property
    def timing_prior(self) -> "DrinkTimingPrior":
        """Return the state-conditioned companion prior for this dataset."""

        return DrinkTimingPrior.from_observations(self.observations)

    @property
    def timing_observation_count(self) -> int:
        return sum(value.is_state_conditioned for value in self.observations)

    def timing_score_for(self, observation: DrinkBehaviorObservation) -> int:
        return self.timing_prior.score_for(observation)

    def timing_bonus_for(self, observation: DrinkBehaviorObservation) -> int:
        return self.timing_prior.bonus_for(observation)

    def summary(self) -> dict[str, object]:
        timing_count = sum(
            value.is_state_conditioned for value in self.observations
        )
        return {
            "schema": self.schema,
            "behavior_only": True,
            "rl_transition_model": False,
            "candidate_set_kind": DRINK_INVENTORY_CANDIDATE_SET_KIND,
            "observation_count": len(self.observations),
            "trajectory_count": len({value.trajectory_id for value in self.observations}),
            "flow_count": len(self.maximum_score_by_flow),
            "flows": sorted(self.maximum_score_by_flow),
            "state_conditioned_observation_count": timing_count,
            "timing_schema": DRINK_TIMING_PRIOR_SCHEMA,
            "timing_available": timing_count > 0,
        }

    def to_dict(self) -> dict[str, object]:
        result = self.summary()
        result["maximum_score_by_flow"] = dict(sorted(self.maximum_score_by_flow.items()))
        result["context_weights"] = [
            {
                "flow": flow,
                "stage": stage,
                "action_order": action_order,
                "drink_id": drink_id,
                "weight": weight,
            }
            for (flow, stage, action_order, drink_id), weight in sorted(
                self.context_weights.items()
            )
        ]
        return result


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _timing_state_signature(state: Mapping[str, object] | None) -> str:
    if not isinstance(state, Mapping):
        return ""
    # Keep only stable native scalar fields.  ``random_state`` and object
    # pointers are useful for transition audits but would make a behavioural
    # context spuriously unique across otherwise identical turns.
    fields = (
        "current_turn",
        "remain_turn",
        "score",
        "stamina",
        "max_stamina",
        "block",
        "exam_card_play_count",
        "turn_card_play_count",
        "parameter",
        "parameter_type",
        "current_parameter",
    )
    return _canonical_json(
        {name: state[name] for name in fields if name in state}
    )


def _timing_key(
    observation: DrinkBehaviorObservation,
    *,
    include_candidates: bool = True,
    include_state: bool = True,
) -> tuple[object, ...]:
    if not observation.is_state_conditioned or observation.round_number is None:
        raise DrinkObservationError("timing key requires a complete state observation")
    return (
        observation.flow_id,
        observation.stage,
        observation.round_number,
        observation.candidate_drink_ids if include_candidates else (),
        _timing_state_signature(observation.state_before) if include_state else "",
    )


@dataclass(frozen=True, slots=True)
class DrinkTimingPrior:
    """Bounded state-conditioned prior over exact drink timing/identity.

    The prior is deliberately separate from the static drink-effect score.
    It can rank only IDs present in the explicit candidate inventory and only
    adds a small advisory bonus to a caller's existing native search score.
    """

    observations: tuple[DrinkBehaviorObservation, ...]
    context_weights: Mapping[tuple[object, ...], Mapping[str, float]]
    maximum_score_by_flow: Mapping[str, int]
    schema: str = DRINK_TIMING_PRIOR_SCHEMA

    @classmethod
    def from_observations(
        cls,
        observations: Sequence[DrinkBehaviorObservation],
    ) -> "DrinkTimingPrior":
        values = tuple(
            value for value in observations if value.is_state_conditioned
        )
        maximum: dict[str, int] = {}
        for observation in values:
            maximum[observation.flow_id] = max(
                maximum.get(observation.flow_id, 0), observation.terminal_score
            )
        weights: defaultdict[tuple[object, ...], defaultdict[str, float]] = defaultdict(
            lambda: defaultdict(float)
        )
        for observation in values:
            max_score = maximum[observation.flow_id]
            weight = 1.0 + 4.0 * observation.terminal_score / max_score
            # Exact state/candidate context is preferred.  The score_for
            # fallback keys are built lazily below and stay within the same
            # flow/stage/round, never another archetype.
            weights[_timing_key(observation)][observation.chosen_drink_id] += weight
        return cls(
            values,
            {key: dict(value) for key, value in weights.items()},
            dict(maximum),
        )

    @classmethod
    def from_rows(
        cls,
        rows: Sequence[Mapping[str, object]],
    ) -> "DrinkTimingPrior":
        """Load the path-free timing JSONL rows emitted by the builder.

        Timing rows are a separate contract from identity-only drink rows.
        Validate that boundary before constructing an observation so a
        caller cannot accidentally train a state-conditioned prior from a
        generic/legacy row (or from a row that was explicitly marked as
        non-behavioural).
        """

        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise DrinkObservationError("timing rows must be an array")

        observations: list[DrinkBehaviorObservation] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                raise DrinkObservationError(f"timing row {index} is not an object")
            schema = row.get("schema")
            if schema != DRINK_TIMING_OBSERVATION_SCHEMA:
                raise DrinkObservationError(
                    f"timing row {index} has unsupported schema: {schema!r}"
                )
            if row.get("behavior_only") is not True:
                raise DrinkObservationError(
                    f"timing row {index} must declare behavior_only=true"
                )
            if row.get("state_conditioned") is not True:
                raise DrinkObservationError(
                    f"timing row {index} must declare state_conditioned=true"
                )
            if row.get("state_complete", True) is not True:
                raise DrinkObservationError(
                    f"timing row {index} must declare state_complete=true"
                )
            candidates = row.get("candidate_drink_ids")
            candidate_after = row.get(
                "candidate_drink_ids_after", row.get("remaining_drink_ids")
            )
            before = row.get("state_before", row.get("native_state_before"))
            after = row.get("state_after", row.get("native_state_after"))
            round_number = row.get("round", row.get("round_number"))
            if not isinstance(candidates, Sequence) or isinstance(
                candidates, (str, bytes)
            ):
                raise DrinkObservationError(
                    f"timing[{index}].candidate_drink_ids must be an array"
                )
            if not isinstance(candidate_after, Sequence) or isinstance(
                candidate_after, (str, bytes)
            ):
                raise DrinkObservationError(
                    f"timing[{index}].candidate_drink_ids_after must be an array"
                )
            if not isinstance(before, Mapping) or not isinstance(after, Mapping):
                raise DrinkObservationError(
                    f"timing[{index}] requires state_before/state_after objects"
                )
            observations.append(
                DrinkBehaviorObservation(
                    episode_id=_text(row.get("episode_id"), f"timing[{index}].episode_id"),
                    trajectory_id=_text(
                        row.get("trajectory_id"), f"timing[{index}].trajectory_id"
                    ),
                    produce_id=_text(row.get("produce_id"), f"timing[{index}].produce_id"),
                    idol_card_id=_text(
                        row.get("idol_card_id"), f"timing[{index}].idol_card_id"
                    ),
                    plan_type=_text(row.get("plan_type"), f"timing[{index}].plan_type"),
                    exam_effect_type=_text(
                        row.get("exam_effect_type"),
                        f"timing[{index}].exam_effect_type",
                    ),
                    stage=_text(row.get("stage"), f"timing[{index}].stage"),
                    action_order=_integer(
                        row.get("action_order"), f"timing[{index}].action_order"
                    ),
                    native_sequence=_integer(
                        row.get("native_sequence", index + 1),
                        f"timing[{index}].native_sequence",
                        minimum=1,
                    ),
                    drink_index=_integer(
                        row.get("drink_index"), f"timing[{index}].drink_index"
                    ),
                    candidate_drink_ids=tuple(
                        _text(value, f"timing[{index}].candidate_drink_ids[]")
                        for value in candidates
                    ),
                    chosen_drink_id=_text(
                        row.get("chosen_drink_id"), f"timing[{index}].chosen_drink_id"
                    ),
                    terminal_score=_integer(
                        row.get("terminal_score"),
                        f"timing[{index}].terminal_score",
                        minimum=1,
                    ),
                    round_number=_integer(
                        round_number, f"timing[{index}].round", minimum=1
                    ),
                    state_before=before,
                    state_after=after,
                    candidate_drink_ids_after=tuple(
                        _text(value, f"timing[{index}].candidate_drink_ids_after[]")
                        for value in candidate_after
                    ),
                    state_source=(
                        row.get("state_source")
                        if isinstance(row.get("state_source"), str)
                        else "timing-jsonl"
                    ),
                    state_complete=row.get("state_complete", True),
                )
            )
        return cls.from_observations(observations)

    def _selected_for_context(
        self,
        *,
        flow_id: str,
        stage: str,
        round_number: int,
        candidate_drink_ids: Sequence[str],
        state_before: Mapping[str, object] | None,
    ) -> Mapping[str, float]:
        exact_key = (
            flow_id,
            stage,
            round_number,
            tuple(candidate_drink_ids),
            _timing_state_signature(state_before),
        )
        exact = self.context_weights.get(exact_key)
        if exact:
            return exact
        candidate_key = (flow_id, stage, round_number, tuple(candidate_drink_ids), "")
        candidates = self.context_weights.get(candidate_key)
        if candidates:
            return candidates
        round_key = (flow_id, stage, round_number, (), "")
        selected: defaultdict[str, float] = defaultdict(float)
        for key, weights in self.context_weights.items():
            if key[:3] == round_key[:3]:
                for drink_id, weight in weights.items():
                    selected[drink_id] += weight
        if selected:
            return dict(selected)
        # Last fallback is still exact flow/stage, rather than borrowing a
        # different mode/card archetype.  This is useful when a live state
        # has a new scalar field that old telemetry did not retain.
        selected = defaultdict(float)
        for key, weights in self.context_weights.items():
            if key[:2] == (flow_id, stage):
                for drink_id, weight in weights.items():
                    selected[drink_id] += weight
        return dict(selected)

    def _selected(
        self,
        observation: DrinkBehaviorObservation,
    ) -> Mapping[str, float]:
        return self._selected_for_context(
            flow_id=observation.flow_id,
            stage=observation.stage,
            round_number=observation.round_number or 0,
            candidate_drink_ids=observation.candidate_drink_ids,
            state_before=observation.state_before,
        )

    def score_for(self, observation: DrinkBehaviorObservation) -> int:
        if not isinstance(observation, DrinkBehaviorObservation):
            raise TypeError("timing score requires DrinkBehaviorObservation")
        if not observation.is_state_conditioned:
            return 0
        selected = self._selected(observation)
        total = sum(selected.values())
        if total <= 0:
            return 0
        return max(
            0,
            min(
                1000,
                round(
                    1000
                    * selected.get(observation.chosen_drink_id, 0.0)
                    / total
                ),
            ),
        )

    def score_for_state(
        self,
        *,
        flow_id: str | None = None,
        stage: str,
        round_number: int,
        candidate_drink_ids: Sequence[str],
        state_before: Mapping[str, object],
        drink_id: str,
        terminal_score: int = 1,
    ) -> int:
        """Score a prospective native search drink without inventing state."""
        if flow_id is None or flow_id == "":
            if len(self.maximum_score_by_flow) != 1:
                return 0
            flow_id = next(iter(self.maximum_score_by_flow))
        if (
            not isinstance(flow_id, str)
            or not flow_id
            or not isinstance(stage, str)
            or not stage
            or not isinstance(round_number, int)
            or isinstance(round_number, bool)
            or round_number < 1
            or not isinstance(state_before, Mapping)
            or state_before.get("current_turn") != round_number
        ):
            return 0
        candidates = tuple(candidate_drink_ids)
        if not candidates or not all(isinstance(value, str) and value for value in candidates):
            return 0
        if drink_id not in candidates:
            return 0
        selected = self._selected_for_context(
            flow_id=flow_id,
            stage=stage,
            round_number=round_number,
            candidate_drink_ids=candidates,
            state_before=state_before,
        )
        total = sum(selected.values())
        if total <= 0:
            return 0
        return max(0, min(1000, round(1000 * selected.get(drink_id, 0.0) / total)))

    def bonus_for(self, observation: DrinkBehaviorObservation) -> int:
        return round(DRINK_TIMING_MAX_BONUS * self.score_for(observation) / 1000)

    def bonus_for_state(self, **kwargs: object) -> int:
        score = self.score_for_state(**kwargs)  # type: ignore[arg-type]
        return round(DRINK_TIMING_MAX_BONUS * score / 1000)

    def rank(self, observation: DrinkBehaviorObservation) -> tuple[str, ...]:
        if not isinstance(observation, DrinkBehaviorObservation):
            raise TypeError("timing rank requires DrinkBehaviorObservation")
        selected = self._selected(observation) if observation.is_state_conditioned else {}
        candidates = tuple(dict.fromkeys(observation.candidate_drink_ids))
        scored = {
            drink_id: max(
                0,
                min(
                    1000,
                    round(1000 * selected.get(drink_id, 0.0) / sum(selected.values()))
                    if selected and sum(selected.values()) > 0
                    else 0,
                ),
            )
            for drink_id in candidates
        }
        return tuple(
            sorted(
                (drink_id for drink_id in candidates if scored[drink_id] > 0),
                key=lambda value: (-scored[value], candidates.index(value)),
            )
        )

    def summary(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "behavior_only": True,
            "shadow_only": True,
            "rl_transition_model": False,
            "candidate_set_kind": DRINK_INVENTORY_CANDIDATE_SET_KIND,
            "observation_count": len(self.observations),
            "trajectory_count": len({value.trajectory_id for value in self.observations}),
            "flow_count": len(self.maximum_score_by_flow),
            "context_count": len(self.context_weights),
            "max_bonus": DRINK_TIMING_MAX_BONUS,
            "flows": sorted(self.maximum_score_by_flow),
        }

    def to_dict(self) -> dict[str, object]:
        result = self.summary()
        result["maximum_score_by_flow"] = dict(
            sorted(self.maximum_score_by_flow.items())
        )
        result["context_weights"] = [
            {
                "flow": key[0],
                "stage": key[1],
                "round": key[2],
                "candidate_drink_ids": list(key[3]),
                "state_signature": key[4],
                "drink_weights": dict(sorted(weights.items())),
            }
            for key, weights in sorted(
                self.context_weights.items(), key=lambda item: repr(item[0])
            )
        ]
        return result


@dataclass(frozen=True, slots=True)
class DrinkTimingEvaluation:
    observation_count: int
    scored_count: int
    top1_correct: int
    coverage: float
    top1_accuracy: float
    top1_over_all: float
    by_flow: Mapping[str, Mapping[str, int | float]] = field(default_factory=dict)
    schema: str = DRINK_TIMING_EVALUATION_SCHEMA

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "observation_count": self.observation_count,
            "scored_count": self.scored_count,
            "top1_correct": self.top1_correct,
            "coverage": self.coverage,
            "top1_accuracy": self.top1_accuracy,
            "top1_over_all": self.top1_over_all,
            "by_flow": {key: dict(value) for key, value in self.by_flow.items()},
            "state_conditioned": True,
            "behavior_only": True,
            "shadow_only": True,
            "rl_transition_model": False,
            "candidate_set_kind": DRINK_INVENTORY_CANDIDATE_SET_KIND,
        }


def evaluate_drink_timing(
    observations: Sequence[DrinkBehaviorObservation],
) -> DrinkTimingEvaluation:
    """Trajectory-LOO evaluation for complete state-conditioned rows only."""

    values = tuple(value for value in observations if value.is_state_conditioned)
    by_trajectory: defaultdict[str, list[DrinkBehaviorObservation]] = defaultdict(list)
    for value in values:
        by_trajectory[value.trajectory_id].append(value)
    totals: Counter[str] = Counter()
    scored: Counter[str] = Counter()
    correct: Counter[str] = Counter()
    scored_count = top1_correct = 0
    for trajectory_id, held_out in by_trajectory.items():
        train = tuple(value for value in values if value.trajectory_id != trajectory_id)
        if not train:
            continue
        prior = DrinkTimingPrior.from_observations(train)
        for observation in held_out:
            totals[observation.flow_id] += 1
            ranking = prior.rank(observation)
            if not ranking:
                continue
            scored_count += 1
            scored[observation.flow_id] += 1
            if ranking[0] == observation.chosen_drink_id:
                top1_correct += 1
                correct[observation.flow_id] += 1
    by_flow = {
        flow: {
            "observation_count": totals[flow],
            "scored_count": scored[flow],
            "top1_correct": correct[flow],
            "coverage": scored[flow] / totals[flow] if totals[flow] else 0.0,
            "top1_accuracy": correct[flow] / scored[flow] if scored[flow] else 0.0,
        }
        for flow in sorted(totals)
    }
    return DrinkTimingEvaluation(
        observation_count=len(values),
        scored_count=scored_count,
        top1_correct=top1_correct,
        coverage=scored_count / len(values) if values else 0.0,
        top1_accuracy=top1_correct / scored_count if scored_count else 0.0,
        top1_over_all=top1_correct / len(values) if values else 0.0,
        by_flow=by_flow,
    )


# Descriptive aliases for downstream dataset builders and hidden/offline
# callers that use the longer state-conditioned terminology.
evaluate_state_conditioned_drink_behavior = evaluate_drink_timing


def load_drink_timing_prior(source: str | Path) -> DrinkTimingPrior:
    """Load timing observations JSONL and build a fail-closed prior."""

    path = Path(source)
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    if any(not isinstance(row, Mapping) for row in rows):
        raise DrinkObservationError("timing JSONL contains a non-object row")
    return DrinkTimingPrior.from_rows(rows)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class DrinkBehaviorEvaluation:
    observation_count: int
    scored_count: int
    top1_correct: int
    coverage: float
    top1_accuracy: float
    top1_over_all: float
    by_flow: Mapping[str, Mapping[str, int | float]] = field(default_factory=dict)
    schema: str = DRINK_EVALUATION_SCHEMA

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "observation_count": self.observation_count,
            "scored_count": self.scored_count,
            "top1_correct": self.top1_correct,
            "coverage": self.coverage,
            "top1_accuracy": self.top1_accuracy,
            "top1_over_all": self.top1_over_all,
            "by_flow": {key: dict(value) for key, value in self.by_flow.items()},
            "behavior_only": True,
            "rl_transition_model": False,
            "candidate_set_kind": DRINK_INVENTORY_CANDIDATE_SET_KIND,
        }


def evaluate_drink_behavior(
    observations: Sequence[DrinkBehaviorObservation],
    *,
    state_conditioned: bool = False,
) -> DrinkBehaviorEvaluation | DrinkTimingEvaluation:
    values = tuple(observations)
    if type(state_conditioned) is not bool:
        raise TypeError("state_conditioned must be bool")
    if state_conditioned:
        return evaluate_drink_timing(values)
    by_trajectory: defaultdict[str, list[DrinkBehaviorObservation]] = defaultdict(list)
    for value in values:
        by_trajectory[value.trajectory_id].append(value)
    totals: Counter[str] = Counter()
    scored: Counter[str] = Counter()
    correct: Counter[str] = Counter()
    scored_count = top1_correct = 0
    for trajectory_id, held_out in by_trajectory.items():
        train = tuple(value for value in values if value.trajectory_id != trajectory_id)
        if not train:
            continue
        prior = DrinkBehaviorPrior.from_observations(train)
        for observation in held_out:
            totals[observation.flow_id] += 1
            ranking = prior.rank(observation)
            if not ranking:
                continue
            scored_count += 1
            scored[observation.flow_id] += 1
            if ranking[0] == observation.chosen_drink_id:
                top1_correct += 1
                correct[observation.flow_id] += 1
    by_flow = {
        flow: {
            "observation_count": totals[flow],
            "scored_count": scored[flow],
            "top1_correct": correct[flow],
            "coverage": scored[flow] / totals[flow] if totals[flow] else 0.0,
            "top1_accuracy": correct[flow] / scored[flow] if scored[flow] else 0.0,
        }
        for flow in sorted(totals)
    }
    return DrinkBehaviorEvaluation(
        observation_count=len(values),
        scored_count=scored_count,
        top1_correct=top1_correct,
        coverage=scored_count / len(values) if values else 0.0,
        top1_accuracy=top1_correct / scored_count if scored_count else 0.0,
        top1_over_all=top1_correct / len(values) if values else 0.0,
        by_flow=by_flow,
    )


def _read_episode(path: Path) -> Mapping[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, Mapping):
        raise DrinkObservationError("episode source is not an object")
    return raw


def _window(payload: Mapping[str, object]) -> tuple[int, int, str]:
    raw = payload.get("telemetry_window")
    if not isinstance(raw, Mapping):
        raise DrinkObservationError("missing-telemetry-window")
    start = _integer(
        raw.get("stack_command_sequence_start", raw.get("sequence_start")),
        "telemetry window sequence start",
        minimum=1,
    )
    end = _integer(
        raw.get("stack_command_sequence_end", raw.get("sequence_end")),
        "telemetry window sequence end",
        minimum=1,
    )
    stack = _text(raw.get("stack_id"), "telemetry window stack_id")
    if end < start:
        raise DrinkObservationError("telemetry window sequence order invalid")
    return start, end, stack


def build_drink_behavior_dataset(
    episode_sources: Sequence[str | Path],
    journal: str | Path,
    output_dir: str | Path,
) -> dict[str, object]:
    """Build independent drink observations, prior, evaluation, and manifest."""

    paths = tuple(sorted({Path(value).resolve() for value in episode_sources}, key=lambda value: value.as_posix()))
    if not paths:
        raise DrinkObservationError("no episode sources supplied")
    events = read_telemetry_events(journal)
    accepted: list[DrinkBehaviorObservation] = []
    timing_accepted: list[DrinkBehaviorObservation] = []
    timing_gap_counts: Counter[str] = Counter()
    audits: list[dict[str, object]] = []
    seen_episode_ids: set[str] = set()
    for path in paths:
        digest = _sha256(path)
        try:
            payload = _read_episode(path)
            start, end, stack = _window(payload)
            native = extract_native_exam_actions(
                events,
                sequence_start=start,
                sequence_end=end,
                stack_id=stack,
            )
            raw_native_states = payload.get(
                "native_action_states", payload.get("action_states")
            )
            if raw_native_states is not None:
                native_states = raw_native_states
            else:
                try:
                    native_states = extract_native_action_states(
                        events,
                        sequence_start=start,
                        sequence_end=end,
                        stack_id=stack,
                    )
                except (
                    DrinkObservationError,
                    KeyError,
                    OSError,
                    TypeError,
                    ValueError,
                ):
                    # Preserve the exact identity-only projection while
                    # recording that timing could not be joined from this
                    # journal window.
                    native_states = ()
            projection = project_drink_episode(
                payload,
                native,
                native_action_states=native_states,
            )
        except (DrinkObservationError, KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            projection = DrinkEpisodeProjection(None, False, str(error), 0, 0)
        if projection.accepted and projection.episode_id in seen_episode_ids:
            projection = DrinkEpisodeProjection(
                projection.episode_id,
                False,
                "duplicate-episode-id",
                projection.expected_drink_action_count,
                projection.native_drink_action_count,
                initial_drink_ids=projection.initial_drink_ids,
            )
        if projection.accepted and projection.episode_id is not None:
            seen_episode_ids.add(projection.episode_id)
        record = projection.to_dict()
        record.update({"file": path.name, "sha256": digest})
        audits.append(record)
        if projection.accepted:
            accepted.extend(projection.observations)
            timing_accepted.extend(projection.timing_observations)
            timing_gap_counts.update(projection.timing_gaps)
    observations = tuple(accepted)
    timing_observations = tuple(timing_accepted)
    prior = DrinkBehaviorPrior.from_observations(observations)
    evaluation = evaluate_drink_behavior(observations)
    timing_prior = DrinkTimingPrior.from_observations(timing_observations)
    timing_evaluation = evaluate_drink_timing(timing_observations)
    output = Path(output_dir).resolve()
    observation_bytes = b"".join(_compact_json(value.to_dict()) + b"\n" for value in observations)
    _atomic_write(output / "drink_observations.jsonl", observation_bytes)
    _atomic_write(output / "drink_prior.json", _pretty_json(prior.to_dict()))
    _atomic_write(output / "drink_evaluation.json", _pretty_json(evaluation.to_dict()))
    _atomic_write(
        output / "drink_timing_observations.jsonl",
        b"".join(
            _compact_json(value.to_timing_dict()) + b"\n"
            for value in timing_observations
        ),
    )
    _atomic_write(
        output / "drink_timing_prior.json",
        _pretty_json(timing_prior.to_dict()),
    )
    _atomic_write(
        output / "drink_timing_evaluation.json",
        _pretty_json(timing_evaluation.to_dict()),
    )
    audit_fields = (
        "schema",
        "episode_id",
        "accepted",
        "reason",
        "expected_drink_action_count",
        "native_drink_action_count",
        "initial_drink_ids",
        "remaining_drink_ids",
        "timing_observation_count",
        "timing_gaps",
        "timing_gap",
        "file",
        "sha256",
    )
    source_audits = [
        {key: row.get(key) for key in audit_fields}
        for row in audits
    ]
    manifest = {
        "schema": "gkms.native-drink-behavior-dataset.v1",
        "episode_count": len(paths),
        "accepted_episode_count": sum(bool(row.get("accepted")) for row in audits),
        "observation_count": len(observations),
        "accepted_sources": [row for row in source_audits if row.get("accepted") is True],
        "rejected_sources": [row for row in source_audits if row.get("accepted") is not True],
        "journal": str(Path(journal).resolve()),
        "journal_sha256": _sha256(Path(journal)),
        "prior": prior.to_dict(),
        "evaluation": evaluation.to_dict(),
        "timing_observation_count": len(timing_observations),
        "timing_gap_counts": dict(sorted(timing_gap_counts.items())),
        "timing_gap_codes": sorted(timing_gap_counts),
        "timing_gap": (
            min(timing_gap_counts) if timing_gap_counts else None
        ),
        "timing_prior": timing_prior.to_dict(),
        "timing_evaluation": timing_evaluation.to_dict(),
        "timing_state_source": (
            "native_action_state"
            if timing_observations
            else "missing-live-transition-recorder"
        ),
        "behavior_only": True,
    }
    _atomic_write(output / "manifest.json", _pretty_json(manifest))
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build an independent native drink behavior observation/prior dataset"
    )
    parser.add_argument("--episode", nargs="+", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)
    manifest = build_drink_behavior_dataset(args.episode, args.journal, args.output_dir)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


__all__ = [
    "DRINK_EVALUATION_SCHEMA",
    "DRINK_INVENTORY_CANDIDATE_SET_KIND",
    "DRINK_OBSERVATION_SCHEMA",
    "DRINK_PRIOR_SCHEMA",
    "DRINK_TIMING_EVALUATION_SCHEMA",
    "DRINK_TIMING_GAP_NO_ACTION",
    "DRINK_TIMING_GAP_STATE_INCOMPLETE",
    "DRINK_TIMING_GAP_STATE_MISMATCH",
    "DRINK_TIMING_GAP_STATE_MISSING",
    "DRINK_TIMING_MAX_BONUS",
    "DRINK_TIMING_OBSERVATION_SCHEMA",
    "DRINK_TIMING_PRIOR_SCHEMA",
    "DrinkBehaviorEvaluation",
    "DrinkBehaviorObservation",
    "DrinkBehaviorPrior",
    "DrinkEpisodeProjection",
    "DrinkObservationError",
    "DrinkTimingEvaluation",
    "DrinkTimingPrior",
    "NativeExamAction",
    "NativeExamActionState",
    "NATIVE_EXAM_ACTION_SCHEMA",
    "build_drink_behavior_dataset",
    "classify_manual_command",
    "evaluate_drink_behavior",
    "evaluate_drink_timing",
    "evaluate_state_conditioned_drink_behavior",
    "extract_native_action_states",
    "extract_native_exam_actions",
    "load_drink_timing_prior",
    "main",
    "project_drink_episode",
]


if __name__ == "__main__":
    raise SystemExit(main())
