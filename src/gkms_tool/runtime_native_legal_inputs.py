"""Pure reader for the DLL's primary-input legality observation.

Consumes a whole native snapshot so GUID/slot identities cannot be assembled
from another observation. No simulator/training flag, actor, I/O or controller
is consulted. Native revision is an opaque CAS token, not a Python rehash of
the C++ serializer's complete snapshot.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import re
from types import MappingProxyType

from .training_artifact_io import canonical_json_bytes

SCHEMA = "gkms.native-exam-legal-inputs.v1"
_SOURCES = {"play": "ExamCardUtility.ValidateUseHandCard", "drink": "ExamFooterPresenter.CanUseDrink",
            "end_turn": "existing-native-settled-Main-command-boundary"}


class NativeLegalInputsError(ValueError):
    """The supplied observation does not satisfy the native pool contract."""


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _text(value, label):
    if not isinstance(value, str) or not value:
        raise NativeLegalInputsError(f"{label} is absent")
    return value


def _object(value, label):
    if not isinstance(value, Mapping):
        raise NativeLegalInputsError(f"{label} must be an object")
    return value


def _rows(value, label):
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise NativeLegalInputsError(f"{label} must be an observed ordered array")
    return value


def _boolean(value, label):
    if type(value) is not bool:
        raise NativeLegalInputsError(f"{label} must be a native boolean")
    return value


def _sequence_key(value):
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", value):
        number = int(value, 16 if value.lower().startswith("0x") else 10)
    else:
        raise NativeLegalInputsError("native sequence identity is invalid")
    if not 0 < number < 2**64:
        raise NativeLegalInputsError("native sequence identity is outside a nonzero 64-bit pointer")
    return str(number)


@dataclass(frozen=True, slots=True)
class NativePrimaryInput:
    kind: str
    command: str
    target: Mapping[str, object]
    validation_source: str
    secondary_selection: Mapping[str, object]

    def __post_init__(self):
        object.__setattr__(self, "target", _freeze(self.target))
        object.__setattr__(self, "secondary_selection", _freeze(self.secondary_selection))

    def to_dict(self):
        return {"kind": self.kind, "command": self.command, "target": _plain(self.target),
                "validation_source": self.validation_source, "secondary_selection": _plain(self.secondary_selection)}


@dataclass(frozen=True, slots=True)
class RuntimeNativeLegalInputs:
    session_generation: str
    sequence_id: str | int
    sequence_key: str
    revision: str
    exam_save_sha256: str
    boundary_ready: bool
    complete: bool
    hand: tuple[Mapping, ...]
    drinks: tuple[Mapping, ...]
    end_turn: Mapping
    unknown_checks: tuple[Mapping, ...]
    actions: tuple[NativePrimaryInput, ...]
    schema: str = SCHEMA

    def __post_init__(self):
        for name in ("hand", "drinks", "end_turn", "unknown_checks"):
            object.__setattr__(self, name, _freeze(getattr(self, name)))

    @property
    def partial(self):
        return not self.complete

    def lookup(self, kind: str, *, card_guid: str | None = None, slot: int | None = None,
               drink_id: str | None = None) -> NativePrimaryInput | None:
        """Find one proven native primary target; denied/unknown returns None.

        A drink requires its slot, preserving repeated drink IDs. No simulator
        action-ID fragment or secondary GUID is used to invent identity.
        """
        if not isinstance(kind, str) or kind not in _SOURCES:
            raise NativeLegalInputsError("unknown primary input kind")
        if slot is not None and (type(slot) is not int or slot < 0):
            raise NativeLegalInputsError("lookup slot must be a nonnegative integer")
        if kind == "play":
            if drink_id is not None or (card_guid is None and slot is None):
                raise NativeLegalInputsError("play lookup requires GUID or slot")
            if card_guid is not None:
                _text(card_guid, "lookup card GUID")
        elif kind == "drink":
            if card_guid is not None or slot is None:
                raise NativeLegalInputsError("drink lookup requires its observed slot")
            if drink_id is not None:
                _text(drink_id, "lookup drink ID")
        elif any(value is not None for value in (card_guid, slot, drink_id)):
            raise NativeLegalInputsError("END lookup has no card/drink identity")
        matches = [action for action in self.actions if action.kind == kind
            and (slot is None or action.target.get("slot") == slot)
            and (card_guid is None or action.target.get("card_guid") == card_guid)
            and (drink_id is None or action.target.get("drink_id") == drink_id)]
        if len(matches) > 1:
            raise NativeLegalInputsError("native primary target is ambiguous")
        return matches[0] if matches else None

    def to_dict(self):
        return {"schema": self.schema, "scope": "primary-inputs-only", "boundary_ready": self.boundary_ready,
            "complete": self.complete, "partial": self.partial, "hand": _plain(self.hand),
            "drinks": _plain(self.drinks), "end_turn": _plain(self.end_turn),
            "unknown_checks": _plain(self.unknown_checks), "legal_actions": [action.to_dict() for action in self.actions],
            "secondary_selection_complete": False,
            "provenance": {"source": "same-native-read_snapshot", "session_generation": self.session_generation,
                "sequence_id": self.sequence_id, "sequence_key": self.sequence_key,
                "native_revision": self.revision, "exam_save_sha256": self.exam_save_sha256,
                "revision_validation": "opaque-native-CAS-token; optional-exact-caller-binding",
                "exam_save_modified": False, "simulator_coverage_used": False}}


def parse_runtime_native_legal_inputs(snapshot: Mapping, *, session_generation: str,
                                     expected_revision: str | None = None,
                                     expected_sequence_id: str | int | None = None) -> RuntimeNativeLegalInputs:
    """Validate the DLL v1 pool against this same snapshot's ExamSaveData.

    Native denied inputs are ordinary observations. Unknown validation keeps
    the pool partial and never removes independently proven legal targets.
    Contradictory identity/readiness/action evidence is a protocol error.
    """
    snapshot = _object(snapshot, "native snapshot")
    generation = _text(session_generation, "native session generation")
    revision = _text(snapshot.get("revision"), "native revision")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", revision):
        raise NativeLegalInputsError("native revision is not a SHA-256 CAS token")
    sequence_id = snapshot.get("sequence_id")
    sequence = _sequence_key(sequence_id)
    if expected_revision is not None and revision != expected_revision:
        raise NativeLegalInputsError("native revision differs from caller binding")
    if expected_sequence_id is not None and sequence != _sequence_key(expected_sequence_id):
        raise NativeLegalInputsError("native sequence differs from caller binding")
    phase = snapshot.get("phase")
    if type(phase) is not int or phase < 0:
        raise NativeLegalInputsError("native phase is invalid")
    flags = {key: _boolean(snapshot.get(key), "snapshot." + key)
             for key in ("busy", "queue_empty", "terminal", "turn_card_play_end", "is_replay")}
    ready = phase == 6 and flags["queue_empty"] and not any(flags[key]
        for key in ("busy", "terminal", "turn_card_play_end", "is_replay"))
    raw = _object(snapshot.get("exam_save"), "same native ExamSaveData")
    for key, expected in (("phase", phase), ("isReplay", flags["is_replay"]),
                          ("isTurnCardPlayEnd", flags["turn_card_play_end"])):
        if type(raw.get(key)) is not type(expected) or raw[key] != expected:
            raise NativeLegalInputsError("ExamSaveData differs from snapshot readiness:" + key)
    pool = _object(snapshot.get("native_legal_inputs"), "native legal input pool")
    if pool.get("schema") != SCHEMA or pool.get("scope") != "primary-inputs-only":
        raise NativeLegalInputsError("native legal input schema/scope differs")
    if _boolean(pool.get("boundary_ready"), "pool.boundary_ready") != ready:
        raise NativeLegalInputsError("native pool contradicts current boundary readiness")
    complete = _boolean(pool.get("complete"), "pool.complete")
    if pool.get("secondary_selection_complete") is not False:
        raise NativeLegalInputsError("primary-input pool cannot claim complete secondary selection")
    expected_actions, expected_unknown = {}, {}
    rows_by_kind = {}
    for kind, pool_key, raw_key, identity_key, raw_identity in (
        ("play", "hand", "handList", "card_guid", "_guid"),
        ("drink", "drinks", "drinkList", "drink_id", "_id"),
    ):
        rows, raw_rows = _rows(pool.get(pool_key), pool_key), _rows(raw.get(raw_key), "ExamSaveData." + raw_key)
        rows_by_kind[kind] = rows
        if len(rows) != len(raw_rows):
            raise NativeLegalInputsError("native pool inventory length differs:" + pool_key)
        identities = [_text(row.get(raw_identity), raw_key + " identity") for row in raw_rows]
        for index, (row, identity) in enumerate(zip(rows, identities)):
            if type(row.get("slot")) is not int or row["slot"] != index or row.get(identity_key) != identity:
                raise NativeLegalInputsError("native pool slot/identity differs from same ExamSaveData:" + pool_key)
            if row.get("validation_source") != _SOURCES[kind] or "can_use" not in row:
                raise NativeLegalInputsError("native validator authority/result is absent:" + pool_key)
            allowed = row["can_use"]
            if allowed is not None and type(allowed) is not bool:
                raise NativeLegalInputsError("native can_use must be true, false or unknown")
            if kind == "play" and identities.count(identity) > 1 and allowed is not None:
                raise NativeLegalInputsError("ambiguous hand GUID cannot carry known legality")
            if kind == "play" and allowed is not None and (
                    type(row.get("reason_code")) is not int or not 0 <= row["reason_code"] <= 5):
                raise NativeLegalInputsError("native hand validation reason is invalid")
            if allowed is None:
                expected_unknown[(kind, index)] = row.get("validation_error", "native-validation-not-observed")
            elif allowed and ready:
                expected_actions[(kind, index)] = {"slot": index, identity_key: identity}
    if complete != (ready and not expected_unknown):
        raise NativeLegalInputsError("pool completeness contradicts native validation coverage")
    unknown_rows = _rows(pool.get("unknown_checks"), "native unknown checks")
    observed_unknown = {}
    for row in unknown_rows:
        if not isinstance(row.get("kind"), str) or row["kind"] not in {"play", "drink"}:
            raise NativeLegalInputsError("unknown check kind is invalid")
        key = (row.get("kind"), row.get("slot"))
        if type(row.get("slot")) is not int or key in observed_unknown or key not in expected_unknown:
            raise NativeLegalInputsError("unknown check does not bind one unresolved native row")
        observed_unknown[key] = _text(row.get("reason"), "native unknown reason")
    if observed_unknown != expected_unknown:
        raise NativeLegalInputsError("unknown checks differ from native validation rows")
    end = _object(pool.get("end_turn"), "native END validation")
    if (_boolean(end.get("can_use"), "END.can_use") != ready or end.get("validation_source") != _SOURCES["end_turn"]
            or end.get("separate_validator_available") is not False):
        raise NativeLegalInputsError("END validation differs from its existing native boundary")
    if ready:
        expected_actions[("end_turn", None)] = {}
    actions, found = [], set()
    for row in _rows(pool.get("legal_actions"), "native legal actions"):
        kind, target = row.get("kind"), _object(row.get("target"), "native primary target")
        if not isinstance(kind, str) or kind not in _SOURCES:
            raise NativeLegalInputsError("unrecognized native primary input kind")
        key = (kind, target.get("slot"))
        if (key in found or key not in expected_actions or dict(target) != expected_actions[key]
                or (kind != "end_turn" and type(target.get("slot")) is not int)
                or row.get("command") != "exam." + kind or row.get("validation_source") != _SOURCES[kind]):
            raise NativeLegalInputsError("primary target is outside same native validator results")
        secondary = _object(row.get("secondary_selection"), "native secondary-selection observation")
        expected_secondary = ({"required": False, "choices_complete": True, "resolve_at_native_selector": False}
            if kind == "end_turn" else {"required": None, "choices_complete": False, "resolve_at_native_selector": True})
        if dict(secondary) != expected_secondary or any(type(secondary.get(key)) is not type(value)
                                                       for key, value in expected_secondary.items()):
            raise NativeLegalInputsError("secondary selection cannot be inferred from primary legality")
        found.add(key)
        actions.append(NativePrimaryInput(kind, row["command"], target, row["validation_source"], secondary))
    if found != set(expected_actions):
        raise NativeLegalInputsError("legal action list omitted an observed native true result")
    try:
        raw_sha = hashlib.sha256(canonical_json_bytes(_plain(raw))).hexdigest()
    except (TypeError, ValueError) as error:
        raise NativeLegalInputsError("same native ExamSaveData is not valid JSON") from error
    return RuntimeNativeLegalInputs(generation, sequence_id, sequence, revision, raw_sha, ready, complete,
        tuple(rows_by_kind["play"]), tuple(rows_by_kind["drink"]), end, tuple(unknown_rows), tuple(actions))
