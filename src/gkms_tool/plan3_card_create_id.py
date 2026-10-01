"""Typed, fail-closed native mutation for ``ExamCardCreateId``.

The Master row is the source of the payload in this module.  The effect id is
only a cross-check: it must agree with the structural fields in ``raw_json``
but is never used to fill a missing field.

The Android v3.2.3 executor and card-pool path prove the complete placement
algorithm used here.  A fixed pick count consumes no count RNG.  For
``DeckRandom``, every created card samples ``GetRandomInt(0, deck.Count)``
against the original deck count, the sampled pairs are stably ordered by
index, and then inserted in that order.  Even ``GetRandomInt(0, 0)`` advances
the native RNG and returns zero.

``ExamCardData`` does not create its GUID in the constructor.  ``get_Guid``
later calls ``Guid.NewGuid`` lazily through ``CreateGuidIfNeed``; neither the
executor nor the placement path calls it, and it is independent of the exam
RNG.  This pure deterministic layer therefore still requires explicit GUID
tokens, but never requires an externally guessed ``DeckRandom`` position.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Literal, Protocol, TypeAlias, runtime_checkable

from .exam_native_rng import UINT32_MASK, advance_state, next_range
from .logic_engine import CARD_CREATE_ID_PATTERN, EFFECT_CARD_CREATE_ID
from .master_db import DEFAULT_DATABASE
from .plan3_native_state import Plan3NativeCard, Plan3NativeState


CARD_CREATE_ID_EFFECT_TYPE = EFFECT_CARD_CREATE_ID
CardCreateDestination: TypeAlias = Literal[
    "deck_first", "deck_random", "grave", "hand"
]

_DESTINATION_BY_MOVE_POSITION: dict[str, CardCreateDestination] = {
    "ProduceCardMovePositionType_DeckFirst": "deck_first",
    "ProduceCardMovePositionType_DeckRandom": "deck_random",
    "ProduceCardMovePositionType_Grave": "grave",
    "ProduceCardMovePositionType_Hand": "hand",
}
_MOVE_POSITION_BY_DESTINATION = {
    destination: move_position
    for move_position, destination in _DESTINATION_BY_MOVE_POSITION.items()
}
_DESTINATIONS = frozenset(_DESTINATION_BY_MOVE_POSITION.values())
_PICK_COUNT_UNKNOWN = "ProducePickCountType_Unknown"
_MISSING = object()
_NO_DEFAULT = object()


class CardCreateError(ValueError):
    """Base error with a stable machine-readable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class CardCreateResolutionError(CardCreateError):
    """The Master row is missing or contains an unsupported shape."""


class CardCreateInputError(CardCreateError):
    """An explicitly supplied execution input is malformed."""


def _text(value: object, field: str, *, error: type[CardCreateError]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error("invalid-text", field)
    return value


def _nonnegative_int(
    value: object, field: str, *, error: type[CardCreateError]
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise error("invalid-nonnegative-int", field)
    return value


def _uint32(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CardCreateInputError("invalid-rng-state", field)
    if not 0 <= value <= UINT32_MASK:
        raise CardCreateInputError("invalid-rng-state", field)
    return value


@dataclass(frozen=True, slots=True)
class CardCreateContract:
    """The structural payload of one Master CardCreateId effect.

    ``count_min`` and ``count_max`` are retained exactly as supplied by the
    Master row.  A variable range is not silently interpreted by this class;
    execution needs a :class:`CardCreateRngBranch` with the native resolved
    bounds because the Android helper may resolve the range from a pick-count
    type/search input.
    """

    effect_id: str
    card_id: str
    upgrade: int
    destination: CardCreateDestination
    count_min: int
    count_max: int
    effect_type: str = CARD_CREATE_ID_EFFECT_TYPE
    pick_count_type: str = _PICK_COUNT_UNKNOWN
    search2_id: str = ""
    raw_move_position_type: str = ""

    def __post_init__(self) -> None:
        effect_id = _text(
            self.effect_id, "effect_id", error=CardCreateResolutionError
        )
        if CARD_CREATE_ID_PATTERN.fullmatch(effect_id) is None:
            raise CardCreateResolutionError("unknown-card-create-id-shape", effect_id)
        _text(self.card_id, "card_id", error=CardCreateResolutionError)
        if (
            isinstance(self.upgrade, bool)
            or not isinstance(self.upgrade, int)
            or self.upgrade < 0
        ):
            raise CardCreateResolutionError("invalid-upgrade", "upgrade")
        if self.destination not in _DESTINATIONS:
            raise CardCreateResolutionError(
                "unknown-card-create-destination", str(self.destination)
            )
        _nonnegative_int(
            self.count_min, "count_min", error=CardCreateResolutionError
        )
        _nonnegative_int(
            self.count_max, "count_max", error=CardCreateResolutionError
        )
        if self.count_min > self.count_max:
            raise CardCreateResolutionError("invalid-count-range")
        if self.effect_type != CARD_CREATE_ID_EFFECT_TYPE:
            raise CardCreateResolutionError("unexpected-effect-type", self.effect_type)
        _text(
            self.pick_count_type,
            "pick_count_type",
            error=CardCreateResolutionError,
        )
        if not isinstance(self.search2_id, str):
            raise CardCreateResolutionError("invalid-search2-id")
        if self.raw_move_position_type:
            if (
                self.raw_move_position_type
                not in _DESTINATION_BY_MOVE_POSITION
                or _DESTINATION_BY_MOVE_POSITION[self.raw_move_position_type]
                != self.destination
            ):
                raise CardCreateResolutionError(
                    "move-position-destination-mismatch",
                    self.raw_move_position_type,
                )

    @property
    def target_card_id(self) -> str:
        """Alias matching the raw Master field name semantically."""

        return self.card_id

    @property
    def target_upgrade_count(self) -> int:
        return self.upgrade

    @property
    def count_range(self) -> tuple[int, int]:
        return self.count_min, self.count_max

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)

    @classmethod
    def from_master_row(
        cls, effect_row: Mapping[str, object] | sqlite3.Row
    ) -> "CardCreateContract":
        return resolve_card_create_contract(effect_row)


@dataclass(frozen=True, slots=True)
class CardCreateRngBranch:
    """An explicit native count-RNG branch.

    ``resolved_max_exclusive`` is deliberately explicit.  The Android call
    is max-exclusive, but the ``GetPickCountMinMax`` helper is outside the
    effect payload and must not be reconstructed from a translated label.
    """

    count: int
    state_before: int
    state_after: int
    resolved_min: int | None = None
    resolved_max_exclusive: int | None = None

    def __post_init__(self) -> None:
        _nonnegative_int(self.count, "count", error=CardCreateInputError)
        _uint32(self.state_before, "state_before")
        _uint32(self.state_after, "state_after")
        if self.resolved_min is not None and (
            isinstance(self.resolved_min, bool)
            or not isinstance(self.resolved_min, int)
        ):
            raise CardCreateInputError("invalid-rng-bound", "resolved_min")
        if self.resolved_max_exclusive is not None and (
            isinstance(self.resolved_max_exclusive, bool)
            or not isinstance(self.resolved_max_exclusive, int)
        ):
            raise CardCreateInputError(
                "invalid-rng-bound", "resolved_max_exclusive"
            )
        if (
            self.resolved_min is not None
            and self.resolved_max_exclusive is not None
            and self.resolved_min >= self.resolved_max_exclusive
        ):
            raise CardCreateInputError("invalid-rng-bound", "range")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CardCreatePlacementBranch:
    """Auditable native ``DeckRandom`` placement result.

    ``sampled_insertion_indices`` are in created-card ordinal order.  Native
    code samples all of them against the original deck count, then stably
    orders the ``(index, card)`` pairs before inserting them.  The singular
    ``insertion_index`` is retained as a compatibility alias for one-card
    effects.  Callers do not need to supply this branch; execution derives it
    from the state's native RNG and validates it when supplied.
    """

    destination: CardCreateDestination
    insertion_index: int | None
    random_state_before: int | None
    random_state_after: int | None
    sampled_insertion_indices: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.destination not in _DESTINATIONS:
            raise CardCreateInputError(
                "unknown-placement-destination", str(self.destination)
            )
        if self.insertion_index is not None:
            _nonnegative_int(
                self.insertion_index,
                "insertion_index",
                error=CardCreateInputError,
            )
        if self.random_state_before is not None:
            _uint32(self.random_state_before, "placement_state_before")
        if self.random_state_after is not None:
            _uint32(self.random_state_after, "placement_state_after")
        sampled = tuple(self.sampled_insertion_indices)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in sampled
        ):
            raise CardCreateInputError(
                "invalid-nonnegative-int", "sampled_insertion_indices"
            )
        if self.insertion_index is not None and sampled:
            if len(sampled) != 1 or sampled[0] != self.insertion_index:
                raise CardCreateInputError("placement-index-alias-mismatch")
        object.__setattr__(self, "sampled_insertion_indices", sampled)

    @property
    def ordered_insertion_indices(self) -> tuple[int, ...]:
        if self.sampled_insertion_indices:
            return self.sampled_insertion_indices
        if self.insertion_index is not None:
            return (self.insertion_index,)
        return ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@runtime_checkable
class CardCreateGuidAllocator(Protocol):
    """Explicit, caller-owned GUID allocator interface."""

    def allocate(self, *, contract: CardCreateContract, ordinal: int) -> str:
        ...


@dataclass(frozen=True, slots=True)
class ExplicitGuidAllocator:
    """A deterministic allocator backed by caller-supplied GUID tokens."""

    tokens: tuple[str, ...]

    def __post_init__(self) -> None:
        tokens = tuple(self.tokens)
        if any(not isinstance(token, str) or not token.strip() for token in tokens):
            raise CardCreateInputError("invalid-guid-token")
        if len(set(tokens)) != len(tokens):
            raise CardCreateInputError("duplicate-guid-token")
        object.__setattr__(self, "tokens", tokens)

    @classmethod
    def from_tokens(cls, tokens: Sequence[str]) -> "ExplicitGuidAllocator":
        if isinstance(tokens, (str, bytes)):
            raise CardCreateInputError("guid-tokens-must-be-sequence")
        return cls(tuple(tokens))

    def allocate(self, *, contract: CardCreateContract, ordinal: int) -> str:
        del contract
        try:
            return self.tokens[ordinal]
        except IndexError as error:
            raise CardCreateInputError("guid-token-missing", str(ordinal)) from error


@dataclass(frozen=True, slots=True)
class CardCreateUnresolvedInput:
    """One missing proof/input that prevents a native mutation."""

    field: str
    reason: str

    def __post_init__(self) -> None:
        _text(self.field, "unresolved_field", error=CardCreateInputError)
        _text(self.reason, "unresolved_reason", error=CardCreateInputError)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CardCreateResolvedBranch:
    """A completely specified count and placement branch."""

    count: int
    count_rng: CardCreateRngBranch | None = None
    placement: CardCreatePlacementBranch | None = None

    def __post_init__(self) -> None:
        _nonnegative_int(self.count, "count", error=CardCreateInputError)
        if self.count_rng is not None and not isinstance(
            self.count_rng, CardCreateRngBranch
        ):
            raise CardCreateInputError("invalid-count-rng-branch")
        if self.placement is not None and not isinstance(
            self.placement, CardCreatePlacementBranch
        ):
            raise CardCreateInputError("invalid-placement-branch")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CardCreateUnresolvedBranch:
    """A typed no-op result; the input state is returned unchanged."""

    reason: str
    required_inputs: tuple[CardCreateUnresolvedInput, ...]
    detail: str = ""

    def __post_init__(self) -> None:
        _text(self.reason, "unresolved_reason", error=CardCreateInputError)
        inputs = tuple(self.required_inputs)
        if not all(isinstance(value, CardCreateUnresolvedInput) for value in inputs):
            raise CardCreateInputError("invalid-unresolved-input")
        object.__setattr__(self, "required_inputs", inputs)
        if not isinstance(self.detail, str):
            raise CardCreateInputError("invalid-unresolved-detail")

    @property
    def required_fields(self) -> tuple[str, ...]:
        return tuple(value.field for value in self.required_inputs)

    @property
    def code(self) -> str:
        return self.reason

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


CardCreateBranch: TypeAlias = CardCreateResolvedBranch | CardCreateUnresolvedBranch


@dataclass(frozen=True, slots=True)
class CardCreateMutation:
    ordinal: int
    guid: str
    card_id: str
    upgrade: int
    destination: CardCreateDestination
    insertion_index: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CardCreateMutationTrace:
    """Immutable audit data for one pure state transition.

    ``allocated_guids`` follows constructor/source order.  ``mutations``
    follows native destination-mutation order, while each mutation's
    ``ordinal`` identifies the corresponding source card.
    """

    effect_id: str
    destination: CardCreateDestination
    count: int | None
    random_state_before: int
    random_state_after: int
    rng_consumed: bool
    allocated_guids: tuple[str, ...]
    mutations: tuple[CardCreateMutation, ...]
    unresolved_inputs: tuple[CardCreateUnresolvedInput, ...] = ()
    rng_call_count: int = 0

    def __post_init__(self) -> None:
        _uint32(self.random_state_before, "trace_state_before")
        _uint32(self.random_state_after, "trace_state_after")
        if not isinstance(self.rng_consumed, bool):
            raise CardCreateInputError("invalid-trace-rng-consumed")
        _nonnegative_int(
            self.rng_call_count,
            "trace_rng_call_count",
            error=CardCreateInputError,
        )
        if self.rng_consumed != (self.rng_call_count > 0):
            raise CardCreateInputError("trace-rng-consumption-mismatch")
        if self.count is not None:
            _nonnegative_int(self.count, "trace_count", error=CardCreateInputError)
        guids = tuple(self.allocated_guids)
        if any(not isinstance(value, str) or not value.strip() for value in guids):
            raise CardCreateInputError("invalid-trace-guid")
        if len(set(guids)) != len(guids):
            raise CardCreateInputError("duplicate-trace-guid")
        object.__setattr__(self, "allocated_guids", guids)
        mutations = tuple(self.mutations)
        if not all(isinstance(value, CardCreateMutation) for value in mutations):
            raise CardCreateInputError("invalid-trace-mutation")
        object.__setattr__(self, "mutations", mutations)
        inputs = tuple(self.unresolved_inputs)
        if not all(isinstance(value, CardCreateUnresolvedInput) for value in inputs):
            raise CardCreateInputError("invalid-trace-unresolved-input")
        object.__setattr__(self, "unresolved_inputs", inputs)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CardCreateResult:
    """Pure result: ``after`` is unchanged for an unresolved branch."""

    before: Plan3NativeState
    after: Plan3NativeState
    branch: CardCreateBranch
    trace: CardCreateMutationTrace

    def __post_init__(self) -> None:
        if not isinstance(self.before, Plan3NativeState) or not isinstance(
            self.after, Plan3NativeState
        ):
            raise CardCreateInputError("state-must-be-plan3-native-state")
        if not isinstance(
            self.branch, (CardCreateResolvedBranch, CardCreateUnresolvedBranch)
        ):
            raise CardCreateInputError("invalid-card-create-branch")
        if not isinstance(self.trace, CardCreateMutationTrace):
            raise CardCreateInputError("invalid-card-create-trace")

    @property
    def state(self) -> Plan3NativeState:
        return self.after

    @property
    def new_state(self) -> Plan3NativeState:
        return self.after

    @property
    def resolved(self) -> bool:
        return isinstance(self.branch, CardCreateResolvedBranch)

    @property
    def unresolved(self) -> bool:
        return not self.resolved

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)


def _row_dict(effect_row: Mapping[str, object] | sqlite3.Row) -> dict[str, object]:
    if isinstance(effect_row, Mapping):
        return dict(effect_row)
    keys = getattr(effect_row, "keys", None)
    if callable(keys):
        try:
            return {key: effect_row[key] for key in keys()}
        except (KeyError, IndexError, TypeError) as error:
            raise CardCreateResolutionError("invalid-master-row") from error
    raise CardCreateResolutionError("master-row-must-be-mapping")


def _value(
    row: Mapping[str, object], *names: str, default: object = _NO_DEFAULT
) -> object:
    for name in names:
        if name in row:
            return row[name]
    if default is not _NO_DEFAULT:
        return default
    raise CardCreateResolutionError("missing-structural-field", names[0])


def _raw_master_payload(outer: Mapping[str, object]) -> dict[str, object]:
    candidate = _value(outer, "raw_json", "rawJson", "raw", default=_MISSING)
    if candidate is _MISSING or candidate is None or candidate == "":
        structural_keys = {
            "effectType",
            "targetProduceCardId",
            "targetUpgradeCount",
            "movePositionType",
            "pickCountMin",
            "pickCountMax",
        }
        if structural_keys.intersection(outer):
            return dict(outer)
        raise CardCreateResolutionError("missing-master-raw-json")
    if isinstance(candidate, (bytes, bytearray)):
        candidate = candidate.decode("utf-8")
    if isinstance(candidate, str):
        try:
            candidate = json.loads(candidate)
        except json.JSONDecodeError as error:
            raise CardCreateResolutionError("invalid-master-raw-json") from error
    if not isinstance(candidate, Mapping):
        raise CardCreateResolutionError("master-raw-json-must-be-object")
    return dict(candidate)


def resolve_card_create_contract(
    effect_row: Mapping[str, object] | sqlite3.Row,
) -> CardCreateContract:
    """Resolve one real Master effect row using structural fields first."""

    outer = _row_dict(effect_row)
    raw = _raw_master_payload(outer)

    outer_type = _value(
        outer, "effect_type", "effectType", default=_MISSING
    )
    raw_type = _value(raw, "effectType", "effect_type", default=_MISSING)
    for value in (outer_type, raw_type):
        if value is not _MISSING and value != CARD_CREATE_ID_EFFECT_TYPE:
            raise CardCreateResolutionError("unexpected-effect-type", str(value))
    if outer_type is _MISSING and raw_type is _MISSING:
        raise CardCreateResolutionError("missing-effect-type")

    outer_id = _value(outer, "id", "effect_id", default=_MISSING)
    raw_id = _value(raw, "id", "effect_id", default=_MISSING)
    effect_id_value = raw_id if raw_id is not _MISSING else outer_id
    effect_id = _text(
        effect_id_value, "effect_id", error=CardCreateResolutionError
    )
    if outer_id is not _MISSING and raw_id is not _MISSING and outer_id != raw_id:
        raise CardCreateResolutionError("master-id-mismatch")
    match = CARD_CREATE_ID_PATTERN.fullmatch(effect_id)
    if match is None:
        raise CardCreateResolutionError("unknown-card-create-id-shape", effect_id)

    # These are intentionally read only from the raw structural payload.  A
    # normalized effect row's id/value columns cannot fill a missing payload.
    card_id = _text(
        _value(raw, "targetProduceCardId", "target_card_id"),
        "targetProduceCardId",
        error=CardCreateResolutionError,
    )
    upgrade = _nonnegative_int(
        _value(raw, "targetUpgradeCount", "target_upgrade_count"),
        "targetUpgradeCount",
        error=CardCreateResolutionError,
    )
    move_position = _text(
        _value(raw, "movePositionType", "move_position_type"),
        "movePositionType",
        error=CardCreateResolutionError,
    )
    try:
        destination = _DESTINATION_BY_MOVE_POSITION[move_position]
    except KeyError as error:
        raise CardCreateResolutionError(
            "unknown-card-create-destination", move_position
        ) from error
    count_min = _nonnegative_int(
        _value(raw, "pickCountMin", "pick_count_min"),
        "pickCountMin",
        error=CardCreateResolutionError,
    )
    count_max = _nonnegative_int(
        _value(raw, "pickCountMax", "pick_count_max"),
        "pickCountMax",
        error=CardCreateResolutionError,
    )
    pick_count_type = _text(
        _value(raw, "pickCountType", "pick_count_type"),
        "pickCountType",
        error=CardCreateResolutionError,
    )
    search2_id = _value(
        raw,
        "produceCardSearchId2",
        "produce_card_search_id2",
        default="",
    )
    if not isinstance(search2_id, str):
        raise CardCreateResolutionError("invalid-produceCardSearchId2")

    expected = (
        match.group(1),
        int(match.group(2)),
        match.group(3),
        int(match.group(4)),
        int(match.group(5)),
    )
    observed = (card_id, upgrade, destination, count_min, count_max)
    if observed != expected:
        raise CardCreateResolutionError(
            "master-id-structural-mismatch",
            f"id={expected!r}:structure={observed!r}",
        )

    return CardCreateContract(
        effect_id=effect_id,
        card_id=card_id,
        upgrade=upgrade,
        destination=destination,
        count_min=count_min,
        count_max=count_max,
        effect_type=CARD_CREATE_ID_EFFECT_TYPE,
        pick_count_type=pick_count_type,
        search2_id=search2_id,
        raw_move_position_type=move_position,
    )


def try_resolve_card_create_contract(
    effect_row: Mapping[str, object] | sqlite3.Row,
) -> CardCreateContract | CardCreateUnresolvedInput:
    """Return a typed unresolved value instead of raising for unknown shape."""

    try:
        return resolve_card_create_contract(effect_row)
    except CardCreateResolutionError as error:
        return CardCreateUnresolvedInput(error.code, error.detail or error.code)


def load_master_card_create_id_rows(
    database: Path = DEFAULT_DATABASE,
) -> tuple[dict[str, object], ...]:
    """Read all real CardCreateId effect rows from the local Master DB."""

    try:
        with sqlite3.connect(Path(database)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM effect WHERE effect_type = ? ORDER BY id",
                (CARD_CREATE_ID_EFFECT_TYPE,),
            ).fetchall()
    except sqlite3.Error as error:
        raise CardCreateResolutionError("master-effect-read-failed") from error
    return tuple(dict(row) for row in rows)


def _unresolved_result(
    state: Plan3NativeState,
    contract: CardCreateContract,
    *,
    reason: str,
    inputs: Sequence[CardCreateUnresolvedInput],
    detail: str = "",
    count: int | None = None,
) -> CardCreateResult:
    unresolved = tuple(inputs)
    branch = CardCreateUnresolvedBranch(reason, unresolved, detail)
    trace = CardCreateMutationTrace(
        effect_id=contract.effect_id,
        destination=contract.destination,
        count=count,
        random_state_before=state.random_state,
        random_state_after=state.random_state,
        rng_consumed=False,
        allocated_guids=(),
        mutations=(),
        unresolved_inputs=unresolved,
    )
    return CardCreateResult(state, state, branch, trace)


def _guid_values(
    state: Plan3NativeState,
    contract: CardCreateContract,
    count: int,
    *,
    guid_allocator: CardCreateGuidAllocator
    | Callable[[CardCreateContract, int], str]
    | None,
    guid_tokens: Sequence[str] | None,
) -> tuple[str, ...] | CardCreateUnresolvedInput:
    if guid_allocator is not None and guid_tokens is not None:
        raise CardCreateInputError("guid-allocator-and-tokens-both-supplied")
    if guid_allocator is None and guid_tokens is None:
        return CardCreateUnresolvedInput(
            "guid_allocator_or_guid_tokens",
            "Android ExamCardData does not expose a client GUID source",
        )
    if guid_tokens is not None:
        if isinstance(guid_tokens, (str, bytes)):
            raise CardCreateInputError("guid-tokens-must-be-sequence")
        tokens = tuple(guid_tokens)
        if len(tokens) < count:
            return CardCreateUnresolvedInput(
                f"guid_tokens[{len(tokens)}:{count}]",
                "not enough explicit GUID tokens",
            )
        if len(tokens) > count:
            raise CardCreateInputError("extra-guid-tokens")
        if any(not isinstance(token, str) or not token.strip() for token in tokens):
            raise CardCreateInputError("invalid-guid-token")
        values = tokens
    else:
        if not hasattr(guid_allocator, "allocate") and not callable(guid_allocator):
            raise CardCreateInputError("invalid-guid-allocator")
        allocated: list[str] = []
        for ordinal in range(count):
            if hasattr(guid_allocator, "allocate"):
                value = guid_allocator.allocate(  # type: ignore[union-attr]
                    contract=contract, ordinal=ordinal
                )
            else:
                value = guid_allocator(contract, ordinal)  # type: ignore[misc]
            if not isinstance(value, str) or not value.strip():
                raise CardCreateInputError("invalid-allocated-guid", str(ordinal))
            allocated.append(value)
        values = tuple(allocated)
    if len(set(values)) != len(values):
        raise CardCreateInputError("duplicate-allocated-guid")
    existing = {card.guid for card in state.all_cards}
    collision = next((value for value in values if value in existing), None)
    if collision is not None:
        raise CardCreateInputError("guid-already-in-state", collision)
    return values


def apply_card_create_id(
    state: Plan3NativeState,
    contract: CardCreateContract,
    *,
    guid_allocator: CardCreateGuidAllocator
    | Callable[[CardCreateContract, int], str]
    | None = None,
    guid_tokens: Sequence[str] | None = None,
    rng_branch: CardCreateRngBranch | None = None,
    rng_state: int | None = None,
    random_state: int | None = None,
    placement_branch: CardCreatePlacementBranch | None = None,
    hand_limit: int | None = None,
) -> CardCreateResult:
    """Apply one pure CardCreateId mutation or return an unchanged state.

    Fixed real rows need no RNG input.  Variable rows need an explicit
    ``rng_branch`` containing the native resolved bounds and verified state
    transition.  ``deck_random`` placement is derived directly from the
    state's RNG; an optional ``placement_branch`` is validation-only.
    Every created card needs caller-owned GUID tokens or an explicit allocator
    because native GUID generation is lazy and independent of the exam RNG.
    This function never creates UUIDs.
    """

    if not isinstance(state, Plan3NativeState):
        raise CardCreateInputError("state-must-be-plan3-native-state")
    if not isinstance(contract, CardCreateContract):
        raise CardCreateInputError("contract-must-be-card-create-contract")
    if rng_state is not None and random_state is not None and rng_state != random_state:
        raise CardCreateInputError("conflicting-rng-state-inputs")
    explicit_rng_state = rng_state if rng_state is not None else random_state
    if explicit_rng_state is not None:
        explicit_rng_state = _uint32(explicit_rng_state, "rng_state")
        if explicit_rng_state != state.random_state:
            raise CardCreateInputError("rng-state-does-not-match-state")
    if hand_limit is not None:
        if (
            isinstance(hand_limit, bool)
            or not isinstance(hand_limit, int)
            or hand_limit < 0
        ):
            raise CardCreateInputError("invalid-hand-limit")
    if placement_branch is not None and not isinstance(
        placement_branch, CardCreatePlacementBranch
    ):
        raise CardCreateInputError("invalid-placement-branch")
    if rng_branch is not None and not isinstance(rng_branch, CardCreateRngBranch):
        raise CardCreateInputError("invalid-count-rng-branch")

    count: int | None
    count_rng: CardCreateRngBranch | None = None
    current_random_state = state.random_state
    rng_call_count = 0
    unresolved: list[CardCreateUnresolvedInput] = []
    if contract.count_min == contract.count_max:
        count = contract.count_min
        if rng_branch is not None:
            raise CardCreateInputError("fixed-count-must-not-have-count-rng")
    else:
        count = None
        if rng_branch is None:
            unresolved.append(
                CardCreateUnresolvedInput(
                    "rng_branch",
                    "variable count needs native resolved min/max and state transition",
                )
            )
        else:
            if rng_branch.state_before != state.random_state:
                raise CardCreateInputError("count-rng-state-does-not-match-state")
            if (
                rng_branch.resolved_min is None
                or rng_branch.resolved_max_exclusive is None
            ):
                unresolved.append(
                    CardCreateUnresolvedInput(
                        "rng_branch.resolved_min/resolved_max_exclusive",
                        "GetPickCountMinMax result is not supplied",
                    )
                )
            else:
                try:
                    expected_count, expected_state = next_range(
                        rng_branch.state_before,
                        rng_branch.resolved_min,
                        rng_branch.resolved_max_exclusive,
                    )
                except (TypeError, ValueError) as error:
                    raise CardCreateInputError(
                        "invalid-count-rng-branch", str(error)
                    ) from error
                if (
                    rng_branch.count != expected_count
                    or rng_branch.state_after != expected_state
                ):
                    raise CardCreateInputError("count-rng-branch-mismatch")
                if not contract.count_min <= rng_branch.count <= contract.count_max:
                    raise CardCreateInputError("count-outside-contract-range")
                count = rng_branch.count
                count_rng = rng_branch
                current_random_state = rng_branch.state_after
                rng_call_count = 1

    assert count is None or count >= 0
    if count is None:
        return _unresolved_result(
            state,
            contract,
            reason="count-branch-unresolved",
            inputs=unresolved,
        )

    if count == 0:
        if guid_allocator is not None or guid_tokens is not None:
            # A zero-card branch must not consume or validate an unrelated
            # token; it is still a complete no-op mutation.
            pass
        if placement_branch is not None:
            raise CardCreateInputError("zero-count-placement-branch")
        after = replace(state, random_state=current_random_state)
        branch = CardCreateResolvedBranch(count=count, count_rng=count_rng)
        trace = CardCreateMutationTrace(
            effect_id=contract.effect_id,
            destination=contract.destination,
            count=count,
            random_state_before=state.random_state,
            random_state_after=after.random_state,
            rng_consumed=rng_call_count > 0,
            allocated_guids=(),
            mutations=(),
            rng_call_count=rng_call_count,
        )
        return CardCreateResult(state, after, branch, trace)

    derived_placement: CardCreatePlacementBranch | None = None
    sampled_insertion_indices: tuple[int, ...] = ()
    if contract.destination == "deck_random":
        placement_state_before = current_random_state
        sampled: list[int] = []
        original_deck_count = len(state.deck)
        for _ in range(count):
            if original_deck_count == 0:
                # Native GetRandomInt does not special-case an empty interval:
                # multiply-high by width zero returns the minimum, then the
                # xorshift32 state is stored as usual.
                insertion_index = 0
                current_random_state = advance_state(current_random_state)
            else:
                insertion_index, current_random_state = next_range(
                    current_random_state,
                    0,
                    original_deck_count,
                )
            sampled.append(insertion_index)
            rng_call_count += 1
        sampled_insertion_indices = tuple(sampled)
        derived_placement = CardCreatePlacementBranch(
            destination="deck_random",
            insertion_index=(sampled[0] if count == 1 else None),
            random_state_before=placement_state_before,
            random_state_after=current_random_state,
            sampled_insertion_indices=sampled_insertion_indices,
        )
        if placement_branch is not None:
            if placement_branch.destination != contract.destination:
                raise CardCreateInputError("placement-destination-mismatch")
            if (
                placement_branch.ordered_insertion_indices
                != sampled_insertion_indices
                or placement_branch.random_state_before != placement_state_before
                or placement_branch.random_state_after != current_random_state
            ):
                raise CardCreateInputError("placement-branch-mismatch")
    elif placement_branch is not None:
        raise CardCreateInputError("unexpected-placement-branch")

    if contract.destination == "hand":
        if hand_limit is None:
            unresolved.append(
                CardCreateUnresolvedInput(
                    "hand_limit",
                    "hand upper-limit input is required before direct Hand insertion",
                )
            )

    if unresolved:
        return _unresolved_result(
            state,
            contract,
            reason="placement-or-input-unresolved",
            inputs=unresolved,
            count=count,
        )

    values = _guid_values(
        state,
        contract,
        count,
        guid_allocator=guid_allocator,
        guid_tokens=guid_tokens,
    )
    if isinstance(values, CardCreateUnresolvedInput):
        return _unresolved_result(
            state,
            contract,
            reason="guid-source-unresolved",
            inputs=(values,),
            count=count,
        )

    new_cards = tuple(
        Plan3NativeCard(
            guid=guid,
            card_id=contract.card_id,
            base_upgrade=contract.upgrade,
            temporary_upgrade=0,
            effective_upgrade=contract.upgrade,
        )
        for guid in values
    )
    if contract.destination == "deck_first":
        after = replace(
            state,
            deck=(*new_cards, *state.deck),
            random_state=current_random_state,
        )
        mutations = tuple(
            CardCreateMutation(
                ordinal=ordinal,
                guid=card.guid,
                card_id=card.card_id,
                upgrade=card.effective_upgrade,
                destination="deck_first",
                insertion_index=ordinal,
            )
            for ordinal, card in enumerate(new_cards)
        )
    elif contract.destination == "grave":
        after = replace(
            state,
            grave=(*state.grave, *new_cards),
            random_state=current_random_state,
        )
        mutations = tuple(
            CardCreateMutation(
                ordinal=ordinal,
                guid=card.guid,
                card_id=card.card_id,
                upgrade=card.effective_upgrade,
                destination="grave",
                insertion_index=len(state.grave) + ordinal,
            )
            for ordinal, card in enumerate(new_cards)
        )
    elif contract.destination == "hand":
        assert hand_limit is not None
        hand_capacity = max(0, hand_limit - len(state.hand))
        hand_count = min(count, hand_capacity)
        hand_cards = new_cards[:hand_count]
        overflow_cards = new_cards[hand_count:]
        after = replace(
            state,
            # AddCard recursively routes the overflow tail to DeckFirst
            # before appending the accepted prefix to Hand.
            deck=(*overflow_cards, *state.deck),
            hand=(*state.hand, *hand_cards),
            random_state=current_random_state,
        )
        mutations_list: list[CardCreateMutation] = []
        for overflow_ordinal, card in enumerate(overflow_cards):
            mutations_list.append(
                CardCreateMutation(
                    ordinal=hand_count + overflow_ordinal,
                    guid=card.guid,
                    card_id=card.card_id,
                    upgrade=card.effective_upgrade,
                    destination="deck_first",
                    insertion_index=overflow_ordinal,
                )
            )
        for ordinal, card in enumerate(hand_cards):
            mutations_list.append(
                CardCreateMutation(
                    ordinal=ordinal,
                    guid=card.guid,
                    card_id=card.card_id,
                    upgrade=card.effective_upgrade,
                    destination="hand",
                    insertion_index=len(state.hand) + ordinal,
                )
            )
        mutations = tuple(mutations_list)
    elif contract.destination == "deck_random":
        deck = list(state.deck)
        indexed_cards = tuple(
            (sampled_insertion_indices[ordinal], ordinal, card)
            for ordinal, card in enumerate(new_cards)
        )
        # Enumerable.OrderBy is stable, so equal sampled indices retain card
        # order before sequential List.Insert calls (which reverses equal-index
        # cards in the final deck).
        for insertion_index, _ordinal, card in sorted(
            indexed_cards, key=lambda value: value[0]
        ):
            deck.insert(insertion_index, card)
        after = replace(
            state,
            deck=tuple(deck),
            random_state=current_random_state,
        )
        mutations = tuple(
            CardCreateMutation(
                ordinal=ordinal,
                guid=card.guid,
                card_id=card.card_id,
                upgrade=card.effective_upgrade,
                destination="deck_random",
                insertion_index=insertion_index,
            )
            for insertion_index, ordinal, card in sorted(
                indexed_cards, key=lambda value: value[0]
            )
        )
    else:  # pragma: no cover - CardCreateContract validates this.
        raise CardCreateInputError("unknown-card-create-destination")

    branch = CardCreateResolvedBranch(
        count=count,
        count_rng=count_rng,
        placement=derived_placement,
    )
    trace = CardCreateMutationTrace(
        effect_id=contract.effect_id,
        destination=contract.destination,
        count=count,
        random_state_before=state.random_state,
        random_state_after=after.random_state,
        rng_consumed=rng_call_count > 0,
        allocated_guids=values,
        mutations=mutations,
        rng_call_count=rng_call_count,
    )
    return CardCreateResult(state, after, branch, trace)


# Explicit aliases keep the API discoverable without multiplying behavior.
resolve_card_create_id_contract = resolve_card_create_contract
resolve_plan3_card_create_contract = resolve_card_create_contract
apply_plan3_card_create_id = apply_card_create_id
execute_card_create_id = apply_card_create_id


__all__ = [
    "CARD_CREATE_ID_EFFECT_TYPE",
    "CARD_CREATE_ID_PATTERN",
    "CardCreateBranch",
    "CardCreateContract",
    "CardCreateDestination",
    "CardCreateError",
    "CardCreateGuidAllocator",
    "CardCreateInputError",
    "CardCreateMutation",
    "CardCreateMutationTrace",
    "CardCreatePlacementBranch",
    "CardCreateResolvedBranch",
    "CardCreateResult",
    "CardCreateResolutionError",
    "CardCreateRngBranch",
    "CardCreateUnresolvedBranch",
    "CardCreateUnresolvedInput",
    "ExplicitGuidAllocator",
    "apply_card_create_id",
    "apply_plan3_card_create_id",
    "execute_card_create_id",
    "load_master_card_create_id_rows",
    "resolve_card_create_contract",
    "resolve_card_create_id_contract",
    "resolve_plan3_card_create_contract",
    "try_resolve_card_create_contract",
]
