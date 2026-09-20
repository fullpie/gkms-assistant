"""Strict adapter for runtime ``generated_card_identity`` observations.

The runtime recorder is deliberately a shadow recorder.  A generated-card
identity row is useful to the Plan2 simulator only when the recorder itself
proved the identity at the managed ``CreateGuidIfNeed`` boundary.  This
module therefore does not try to repair incomplete rows, correlate a card by
its post-state position, or manufacture a UUID.  It turns an exact recorder
row into :class:`Plan2NativeGeneratedAllocatorInput` and exposes every
rejection as a typed blocker.

The recorder publishes the identity twice in the normal stream: once as a
standalone ``record=generated_card_identity`` row and once under the related
transition.  Batch adaptation accepts one byte-equivalent semantic copy of
that pair, but rejects conflicting copies for the same ``action_order``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Final

from .plan2_native_horizon import Plan2NativeGeneratedAllocatorInput


GENERATED_CARD_IDENTITY_SCHEMA: Final = (
    "gkms.runtime-generated-card-identity.v1"
)
GENERATED_CARD_IDENTITY_RECORD: Final = "generated_card_identity"
CARD_CREATE_ID_EFFECT_TYPE: Final = (
    "ProduceExamEffectType_ExamCardCreateId"
)
CARD_CREATE_ID_EFFECT_VALUE: Final = 6

DESTINATION_TYPES: Final = {
    1: "hand",
    2: "deck_first",
    3: "deck_last",
    4: "deck_random",
    5: "grave",
    6: "lost",
    7: "hold",
}
DESTINATION_VALUES: Final = {value: key for key, value in DESTINATION_TYPES.items()}


class GeneratedCardAdapterError(ValueError):
    """Raised for an invalid API argument, not for an evidence blocker."""


@dataclass(frozen=True, slots=True)
class GeneratedCardAdapterBlocker:
    """One fail-closed reason attached to a recorder observation."""

    code: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("blocker code must be non-empty text")
        if not isinstance(self.detail, str):
            raise TypeError("blocker detail must be text")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class GeneratedCardPlacementCall:
    """One native ``GetRandomInt`` placement observation."""

    minimum: int
    maximum: int
    result: int

    def to_dict(self) -> dict[str, int]:
        return {
            "minimum": self.minimum,
            "maximum": self.maximum,
            "result": self.result,
        }


@dataclass(frozen=True, slots=True)
class GeneratedCardObservation:
    """A single card identity copied by the native recorder."""

    ordinal: int
    card_id: str
    upgrade: int
    guid: str
    destination: str
    destination_type: int
    destination_order: int

    def to_dict(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "card_id": self.card_id,
            "upgrade": self.upgrade,
            "guid": self.guid,
            "destination": self.destination,
            "destination_type": self.destination_type,
            "destination_order": self.destination_order,
        }


@dataclass(frozen=True, slots=True)
class GeneratedCardIdentityObservation:
    """Normalized exact identity evidence before Plan2 input conversion."""

    action_order: int
    source_guid: str
    effect_id: str
    effect_type: str
    effect_type_value: int
    target_card_id: str
    target_upgrade: int
    destination: str
    destination_type: int
    cards: tuple[GeneratedCardObservation, ...]
    placement_calls: tuple[GeneratedCardPlacementCall, ...]

    @property
    def guid_tokens(self) -> tuple[str, ...]:
        """Return GUIDs in recorder/source ordinal order."""

        return tuple(value.guid for value in self.cards)

    def signature(self) -> tuple[object, ...]:
        """Semantic identity used to deduplicate standalone/transition copies."""

        return (
            self.action_order,
            self.source_guid,
            self.effect_id,
            self.effect_type,
            self.effect_type_value,
            self.target_card_id,
            self.target_upgrade,
            self.destination,
            self.destination_type,
            tuple(
                (
                    value.ordinal,
                    value.card_id,
                    value.upgrade,
                    value.guid,
                    value.destination,
                    value.destination_type,
                    value.destination_order,
                )
                for value in self.cards
            ),
            tuple(
                (value.minimum, value.maximum, value.result)
                for value in self.placement_calls
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "action_order": self.action_order,
            "source_guid": self.source_guid,
            "effect_id": self.effect_id,
            "effect_type": self.effect_type,
            "effect_type_value": self.effect_type_value,
            "target_card_id": self.target_card_id,
            "target_upgrade": self.target_upgrade,
            "destination": self.destination,
            "destination_type": self.destination_type,
            "cards": [value.to_dict() for value in self.cards],
            "placement_calls": [value.to_dict() for value in self.placement_calls],
        }


@dataclass(frozen=True, slots=True)
class GeneratedCardAdapterResult:
    """Result for one generated identity row."""

    observation: GeneratedCardIdentityObservation | None
    allocator_input: Plan2NativeGeneratedAllocatorInput | None
    blockers: tuple[GeneratedCardAdapterBlocker, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.observation is not None and self.allocator_input is not None and not self.blockers

    @property
    def ready(self) -> bool:
        return self.accepted

    @property
    def exact(self) -> bool:
        return self.accepted

    @property
    def input(self) -> Plan2NativeGeneratedAllocatorInput | None:
        """Short alias useful at the simulator boundary."""

        return self.allocator_input

    @property
    def plan2_input(self) -> Plan2NativeGeneratedAllocatorInput | None:
        return self.allocator_input

    @property
    def source_guid(self) -> str | None:
        return None if self.observation is None else self.observation.source_guid

    @property
    def guid_tokens(self) -> tuple[str, ...]:
        return () if self.observation is None else self.observation.guid_tokens

    def require_input(self) -> Plan2NativeGeneratedAllocatorInput:
        """Return the input or raise with the explicit evidence blockers."""

        if not self.accepted or self.allocator_input is None:
            detail = ";".join(
                f"{value.code}:{value.detail}".rstrip(":")
                for value in self.blockers
            ) or "generated identity is not exact"
            raise GeneratedCardAdapterError(detail)
        return self.allocator_input

    @property
    def blocker_codes(self) -> tuple[str, ...]:
        return tuple(value.code for value in self.blockers)

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "identity_exact": self.accepted,
            "observation": (
                None if self.observation is None else self.observation.to_dict()
            ),
            "allocator_input": (
                None
                if self.allocator_input is None
                else {
                    "source_guid": self.allocator_input.source_guid,
                    "guid_tokens": list(self.allocator_input.guid_tokens or ()),
                    "plan_ignore_card_ids": list(
                        self.allocator_input.plan_ignore_card_ids or ()
                    ),
                }
            ),
            "blockers": [value.to_dict() for value in self.blockers],
        }


@dataclass(frozen=True, slots=True)
class GeneratedCardIdentityBatchResult:
    """Batch result keyed by exact recorder action order."""

    observations: tuple[GeneratedCardIdentityObservation, ...] = ()
    allocator_inputs: tuple[Plan2NativeGeneratedAllocatorInput, ...] = ()
    blockers: tuple[GeneratedCardAdapterBlocker, ...] = ()

    @property
    def accepted(self) -> bool:
        return bool(self.observations) and not self.blockers and len(self.observations) == len(self.allocator_inputs)

    @property
    def ready(self) -> bool:
        return self.accepted

    @property
    def exact(self) -> bool:
        return self.accepted

    @property
    def inputs(self) -> tuple[Plan2NativeGeneratedAllocatorInput, ...]:
        return self.allocator_inputs

    @property
    def inputs_by_action_order(self) -> dict[int, Plan2NativeGeneratedAllocatorInput]:
        return self.by_action_order

    @property
    def blocker_codes(self) -> tuple[str, ...]:
        return tuple(value.code for value in self.blockers)

    @property
    def by_action_order(self) -> dict[int, Plan2NativeGeneratedAllocatorInput]:
        if self.blockers or len(self.observations) != len(self.allocator_inputs):
            return {}
        return {
            observation.action_order: allocator_input
            for observation, allocator_input in zip(
                self.observations,
                self.allocator_inputs,
                strict=True,
            )
        }

    @property
    def observations_by_action_order(self) -> dict[int, GeneratedCardIdentityObservation]:
        return {value.action_order: value for value in self.observations}

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "identity_exact": self.accepted,
            "observation_count": len(self.observations),
            "observations": [value.to_dict() for value in self.observations],
            "allocator_inputs": [
                {
                    "source_guid": value.source_guid,
                    "guid_tokens": list(value.guid_tokens or ()),
                    "plan_ignore_card_ids": list(value.plan_ignore_card_ids or ()),
                }
                for value in self.allocator_inputs
            ],
            "blockers": [value.to_dict() for value in self.blockers],
        }


_MISSING = object()


def _blocker(code: str, detail: object = "") -> GeneratedCardAdapterBlocker:
    return GeneratedCardAdapterBlocker(code, "" if detail is None else str(detail))


def _dedupe_blockers(
    blockers: Sequence[GeneratedCardAdapterBlocker],
) -> tuple[GeneratedCardAdapterBlocker, ...]:
    seen: set[tuple[str, str]] = set()
    result: list[GeneratedCardAdapterBlocker] = []
    for value in blockers:
        key = (value.code, value.detail)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return tuple(result)


def _mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    if any(not isinstance(key, str) for key in value):
        return None
    return value


def _nonempty_text(value: object) -> str | None:
    return value if isinstance(value, str) and bool(value.strip()) else None


def _integer(value: object, *, minimum: int | None = None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if minimum is not None and value < minimum:
        return None
    return int(value)


def _sequence(value: object) -> tuple[object, ...] | None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(value)
    return None


def _alias_value(
    primary: Mapping[str, object],
    aliases: Sequence[str],
) -> tuple[object, bool, tuple[GeneratedCardAdapterBlocker, ...]]:
    """Read aliases and report conflicting copies instead of choosing one."""

    values = [(name, primary[name]) for name in aliases if name in primary]
    if not values:
        return _MISSING, False, ()
    first_name, first = values[0]
    conflicts = [
        f"{name}={value!r}"
        for name, value in values[1:]
        if value != first
    ]
    if conflicts:
        return (
            first,
            True,
            (
                _blocker(
                    "generated-identity-alias-conflict",
                    f"{first_name}={first!r};" + ";".join(conflicts),
                ),
            ),
        )
    return first, True, ()


def _destination_pair(
    mapping: Mapping[str, object],
    *,
    label: str,
    blockers: list[GeneratedCardAdapterBlocker],
) -> tuple[str | None, int | None]:
    raw_name = mapping.get("destination", _MISSING)
    raw_type = mapping.get("destination_type", _MISSING)
    name = raw_name if isinstance(raw_name, str) and raw_name in DESTINATION_VALUES else None
    type_value = _integer(raw_type, minimum=1)
    if raw_name is _MISSING or name is None:
        blockers.append(_blocker("target-destination-unavailable", f"{label}.destination"))
    if raw_type is _MISSING or type_value not in DESTINATION_TYPES:
        blockers.append(_blocker("target-destination-unavailable", f"{label}.destination_type"))
    if name is not None and type_value in DESTINATION_TYPES:
        if DESTINATION_TYPES[type_value] != name:
            blockers.append(
                _blocker(
                    "generated-card-destination-mismatch",
                    f"{label}:{name!r}!={type_value}:{DESTINATION_TYPES[type_value]!r}",
                )
            )
    return name, type_value if type_value in DESTINATION_TYPES else None


def _unwrap_single_identity(value: object) -> Mapping[str, object] | None:
    """Unwrap one envelope without treating a captured after-state as identity."""

    mapping = _mapping(value)
    if mapping is None:
        return None
    if mapping.get("record") == GENERATED_CARD_IDENTITY_RECORD:
        return mapping
    body = _mapping(mapping.get("body"))
    if body is not None:
        nested = _unwrap_single_identity(body)
        if nested is not None:
            return nested
    nested_value = mapping.get("generated_card_identity", _MISSING)
    if nested_value is not _MISSING:
        nested = _sequence(nested_value)
        if nested is not None:
            if len(nested) != 1:
                return None
            return _unwrap_single_identity(nested[0])
        return _unwrap_single_identity(nested_value)
    return None


def _extract_identity_values(value: object) -> tuple[tuple[Mapping[str, object], ...], tuple[GeneratedCardAdapterBlocker, ...]]:
    """Extract standalone and transition-nested identity objects from a source."""

    found: list[Mapping[str, object]] = []
    blockers: list[GeneratedCardAdapterBlocker] = []

    def identity_order(raw_identity: Mapping[str, object]) -> int | None:
        value, present, _alias_errors = _alias_value(
            raw_identity, ("action_order", "source_action_order")
        )
        return _integer(value, minimum=1) if present else None

    def identity_source_guid(raw_identity: Mapping[str, object]) -> str | None:
        source = _mapping(raw_identity.get("source"))
        action = None if source is None else _mapping(source.get("action"))
        card = None if action is None else _mapping(action.get("source_card"))
        direct = _nonempty_text(raw_identity.get("source_guid"))
        return (
            _nonempty_text(card.get("guid"))
            if card is not None
            else direct
        )

    def compare_transition_context(
        transition: Mapping[str, object],
        raw_identity: Mapping[str, object],
        label: str,
    ) -> None:
        """Keep the nested copy tied to its own transition boundary."""

        transition_order = _integer(transition.get("action_order"), minimum=1)
        nested_order = identity_order(raw_identity)
        if transition_order is not None and nested_order is not None and transition_order != nested_order:
            blockers.append(
                _blocker(
                    "generated-identity-action-order-mismatch",
                    f"transition={transition_order};identity={nested_order};{label}",
                )
            )
        action = _mapping(transition.get("action"))
        card = None if action is None else _mapping(action.get("source_card"))
        transition_guid = None if card is None else _nonempty_text(card.get("guid"))
        nested_guid = identity_source_guid(raw_identity)
        if transition_guid is not None and nested_guid is not None and transition_guid != nested_guid:
            blockers.append(
                _blocker(
                    "generated-input-source-guid-mismatch",
                    f"transition={transition_guid!r};identity={nested_guid!r};{label}",
                )
            )

    def visit(
        raw: object,
        label: str,
        transition_context: Mapping[str, object] | None = None,
    ) -> None:
        mapping = _mapping(raw)
        if mapping is not None:
            if mapping.get("record") == GENERATED_CARD_IDENTITY_RECORD:
                if transition_context is not None:
                    compare_transition_context(transition_context, mapping, label)
                found.append(mapping)
                return
            body = _mapping(mapping.get("body"))
            if body is not None:
                visit(body, f"{label}.body", transition_context)
            nested = mapping.get("generated_card_identity", _MISSING)
            if nested is not _MISSING and nested is not None:
                nested_values = _sequence(nested)
                if nested_values is None:
                    visit(
                        nested,
                        f"{label}.generated_card_identity",
                        mapping if mapping.get("record") == "transition" else transition_context,
                    )
                else:
                    for index, child in enumerate(nested_values):
                        visit(
                            child,
                            f"{label}.generated_card_identity[{index}]",
                            mapping if mapping.get("record") == "transition" else transition_context,
                        )
            rows = mapping.get("rows", mapping.get("records", _MISSING))
            if rows is not _MISSING:
                row_values = _sequence(rows)
                if row_values is None:
                    blockers.append(_blocker("generated-identity-source-not-array", label))
                else:
                    for index, child in enumerate(row_values):
                        visit(child, f"{label}[{index}]", transition_context)
            return
        values = _sequence(raw)
        if values is not None:
            for index, child in enumerate(values):
                visit(child, f"{label}[{index}]", transition_context)

    visit(value, "source")
    return tuple(found), _dedupe_blockers(blockers)


def _load_source(source: object) -> tuple[object | None, tuple[GeneratedCardAdapterBlocker, ...]]:
    if isinstance(source, (Mapping, Sequence)) and not isinstance(
        source, (str, bytes, bytearray, Path)
    ):
        return source, ()
    if not isinstance(source, (str, Path)):
        return None, (_blocker("generated-identity-source-invalid", "expected path/object/array"),)
    path = Path(source)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        return None, (_blocker("generated-identity-source-read-failed", f"{type(error).__name__}:{error}"),)
    if not text.strip():
        return None, (_blocker("generated-identity-source-empty", str(path)),)
    try:
        return json.loads(text), ()
    except json.JSONDecodeError:
        rows: list[object] = []
        blockers: list[GeneratedCardAdapterBlocker] = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as error:
                blockers.append(
                    _blocker(
                        "generated-identity-source-json-invalid",
                        f"line={line_number}:{error}",
                    )
                )
        return rows, _dedupe_blockers(blockers)


def adapt_generated_card_identity(
    record: Mapping[str, object] | object,
    *,
    expected_action_order: int | None = None,
    expected_source_guid: str | None = None,
    expected_effect_id: str | None = None,
    expected_destination: str | int | None = None,
) -> GeneratedCardAdapterResult:
    """Convert one exact recorder identity row into a Plan2 allocator input.

    ``identity_exact`` and ``post_state_diff_used`` are required literal
    booleans.  Supporting aliases are checked for consistency, but never
    promote an alias in place of the canonical exactness field.
    """

    raw = _unwrap_single_identity(record)
    if raw is None:
        return GeneratedCardAdapterResult(
            None,
            None,
            (_blocker("generated-card-identity-record-missing"),),
        )
    blockers: list[GeneratedCardAdapterBlocker] = []

    if type(raw.get("identity_exact")) is not bool:
        blockers.append(_blocker("generated-identity-exact-flag-missing"))
    elif raw.get("identity_exact") is not True:
        blockers.append(_blocker("generated-identity-not-exact"))
    alias_exact = raw.get("exact_generated_identity", _MISSING)
    if alias_exact is not _MISSING and (
        type(alias_exact) is not bool or alias_exact is not True
    ):
        blockers.append(_blocker("generated-identity-exact-alias-false"))
    if type(raw.get("post_state_diff_used")) is not bool:
        blockers.append(_blocker("generated-identity-post-state-flag-missing"))
    elif raw.get("post_state_diff_used") is not False:
        blockers.append(_blocker("generated-identity-post-state-diff-used"))
    if (
        "identity_schema" in raw
        and raw.get("identity_schema") != GENERATED_CARD_IDENTITY_SCHEMA
    ):
        blockers.append(
            _blocker("generated-identity-schema-mismatch", raw.get("identity_schema"))
        )
    native_blockers = raw.get("blockers", _MISSING)
    if native_blockers is not _MISSING:
        if not isinstance(native_blockers, Sequence) or isinstance(
            native_blockers, (str, bytes, bytearray)
        ):
            blockers.append(_blocker("generated-identity-native-blockers-invalid"))
        elif native_blockers:
            blockers.append(
                _blocker("generated-identity-native-blocked", repr(tuple(native_blockers)))
            )

    raw_order, order_present, alias_blockers = _alias_value(
        raw, ("action_order", "source_action_order")
    )
    blockers.extend(alias_blockers)
    action_order = _integer(raw_order, minimum=1) if order_present else None
    if action_order is None:
        blockers.append(_blocker("source-action-order-unavailable"))
    if expected_action_order is not None and action_order != expected_action_order:
        blockers.append(
            _blocker(
                "generated-identity-action-order-mismatch",
                f"observed={action_order!r};expected={expected_action_order!r}",
            )
        )

    source_mapping = _mapping(raw.get("source"))
    source_action = None if source_mapping is None else _mapping(source_mapping.get("action"))
    source_action_alias = _mapping(raw.get("source_action"))
    if source_action is not None and source_action_alias is not None:
        source_guid_values = (
            _mapping(source_action.get("source_card")),
            _mapping(source_action_alias.get("source_card")),
        )
        if (
            source_guid_values[0] is not None
            and source_guid_values[1] is not None
            and source_guid_values[0].get("guid") != source_guid_values[1].get("guid")
        ):
            blockers.append(_blocker("generated-identity-source-guid-ambiguous"))
    if source_action is None:
        source_action = source_action_alias
    source_card = None if source_action is None else _mapping(source_action.get("source_card"))
    source_guid = None
    if source_card is not None:
        source_guid = _nonempty_text(source_card.get("guid"))
    if source_guid is None:
        direct_source = _nonempty_text(raw.get("source_guid"))
        if direct_source is not None:
            source_guid = direct_source
        else:
            blockers.append(_blocker("source-action-card-guid-unavailable"))
    direct_source = _nonempty_text(raw.get("source_guid"))
    if direct_source is not None and source_guid is not None and direct_source != source_guid:
        blockers.append(
            _blocker(
                "generated-identity-source-guid-ambiguous",
                f"nested={source_guid!r};direct={direct_source!r}",
            )
        )
    if source_action is not None:
        play_type = source_action.get("play_type")
        action_type = source_action.get("action_type")
        if play_type is not None and play_type != 2:
            blockers.append(_blocker("source-action-card-guid-unavailable", "play_type-not-hand"))
        if action_type is not None and action_type not in ("use-hand", "UseHand"):
            blockers.append(_blocker("source-action-card-guid-unavailable", "action_type-not-use-hand"))
    if expected_source_guid is not None and source_guid != expected_source_guid:
        blockers.append(
            _blocker(
                "generated-input-source-guid-mismatch",
                f"observed={source_guid!r};expected={expected_source_guid!r}",
            )
        )

    effect = _mapping(raw.get("effect"))
    if effect is None:
        blockers.append(_blocker("generated-identity-effect-incomplete", "effect"))
        effect = {}
    effect_id_values: list[tuple[str, str]] = []
    nested_effect_id = _nonempty_text(effect.get("effect_id"))
    top_effect_id = _nonempty_text(raw.get("effect_id"))
    if nested_effect_id is not None:
        effect_id_values.append(("effect.effect_id", nested_effect_id))
    if top_effect_id is not None:
        effect_id_values.append(("effect_id", top_effect_id))
    if not effect_id_values:
        blockers.append(_blocker("playing-effect-id-unavailable"))
        effect_id = None
    else:
        effect_id = effect_id_values[0][1]
        if any(value != effect_id for _name, value in effect_id_values[1:]):
            blockers.append(_blocker("generated-identity-effect-id-ambiguous"))
    if expected_effect_id is not None and effect_id != expected_effect_id:
        blockers.append(
            _blocker(
                "generated-identity-effect-id-mismatch",
                f"observed={effect_id!r};expected={expected_effect_id!r}",
            )
        )

    effect_type = effect.get("effect_type", _MISSING)
    effect_type_value = _integer(effect.get("effect_type_value"), minimum=0)
    if effect_type is _MISSING:
        blockers.append(_blocker("generated-identity-effect-incomplete", "effect.effect_type"))
        effect_type_text = CARD_CREATE_ID_EFFECT_TYPE
    elif isinstance(effect_type, str):
        effect_type_text = effect_type
        if effect_type != CARD_CREATE_ID_EFFECT_TYPE:
            blockers.append(_blocker("executor-effect-type-mismatch", effect_type))
    elif effect_type == CARD_CREATE_ID_EFFECT_VALUE:
        effect_type_text = CARD_CREATE_ID_EFFECT_TYPE
    else:
        blockers.append(_blocker("executor-effect-type-mismatch", effect_type))
        effect_type_text = CARD_CREATE_ID_EFFECT_TYPE
    if effect_type_value is None:
        # The C++ output always supplies the numeric enum.  A compact fixture
        # may use the numeric effect_type itself, which is still authoritative.
        effect_type_value = (
            CARD_CREATE_ID_EFFECT_VALUE
            if effect_type == CARD_CREATE_ID_EFFECT_VALUE
            else None
        )
    if effect_type_value != CARD_CREATE_ID_EFFECT_VALUE:
        blockers.append(_blocker("executor-effect-type-mismatch", effect_type_value))

    target_card_id = _nonempty_text(effect.get("target_card_id"))
    direct_target_card_id = _nonempty_text(raw.get("target_card_id"))
    if target_card_id is None:
        target_card_id = direct_target_card_id
    elif direct_target_card_id is not None and direct_target_card_id != target_card_id:
        blockers.append(_blocker("generated-identity-target-card-ambiguous"))
    if target_card_id is None:
        blockers.append(_blocker("target-card-id-unavailable"))
    target_upgrade = _integer(effect.get("target_upgrade"), minimum=0)
    direct_target_upgrade = _integer(raw.get("target_upgrade"), minimum=0)
    if target_upgrade is None:
        target_upgrade = direct_target_upgrade
    elif direct_target_upgrade is not None and direct_target_upgrade != target_upgrade:
        blockers.append(_blocker("generated-identity-target-upgrade-ambiguous"))
    if target_upgrade is None:
        blockers.append(_blocker("target-upgrade-unavailable"))
    destination, destination_type = _destination_pair(
        effect,
        label="effect",
        blockers=blockers,
    )
    if destination is None or destination_type is None:
        # Do not let the top-level alias silently repair an incomplete effect.
        top_destination = raw.get("destination", _MISSING)
        top_destination_type = _integer(raw.get("destination_type"), minimum=1)
        if (
            destination is None
            and isinstance(top_destination, str)
            and top_destination in DESTINATION_VALUES
        ):
            destination = top_destination
        if destination_type is None and top_destination_type in DESTINATION_TYPES:
            destination_type = top_destination_type
        if destination is not None and destination_type is not None and DESTINATION_TYPES[destination_type] != destination:
            blockers.append(_blocker("generated-card-destination-mismatch", "top-level-effect-alias"))
    direct_destination = raw.get("destination", _MISSING)
    direct_destination_type = _integer(raw.get("destination_type"), minimum=1)
    if (
        isinstance(direct_destination, str)
        and direct_destination in DESTINATION_VALUES
        and destination is not None
        and direct_destination != destination
    ):
        blockers.append(_blocker("generated-card-destination-mismatch", "effect-vs-top-level"))
    if (
        direct_destination_type in DESTINATION_TYPES
        and destination_type is not None
        and direct_destination_type != destination_type
    ):
        blockers.append(_blocker("generated-card-destination-mismatch", "effect-type-vs-top-level"))
    if expected_destination is not None:
        expected_destination_type = (
            expected_destination
            if isinstance(expected_destination, int)
            else DESTINATION_VALUES.get(expected_destination)
        )
        if expected_destination_type is None or destination_type != expected_destination_type:
            blockers.append(
                _blocker(
                    "generated-identity-destination-mismatch",
                    f"observed={destination!r};expected={expected_destination!r}",
                )
            )

    raw_cards = _sequence(raw.get("cards"))
    if raw_cards is None or not raw_cards:
        blockers.append(_blocker("generated-card-list-empty"))
        raw_cards = ()
    cards: list[GeneratedCardObservation] = []
    ordinals: list[int] = []
    guids: list[str] = []
    for index, raw_card_value in enumerate(raw_cards):
        raw_card = _mapping(raw_card_value)
        if raw_card is None:
            blockers.append(_blocker("generated-card-guid-or-placement-unresolved", f"card[{index}]:not-object"))
            continue
        ordinal = _integer(raw_card.get("ordinal"), minimum=0)
        card_id = _nonempty_text(raw_card.get("card_id"))
        upgrade = _integer(raw_card.get("upgrade"), minimum=0)
        guid = _nonempty_text(raw_card.get("guid"))
        if ordinal is None:
            blockers.append(_blocker("generated-card-guid-or-placement-unresolved", f"card[{index}].ordinal"))
        if card_id is None:
            blockers.append(_blocker("generated-card-guid-or-placement-unresolved", f"card[{index}].card_id"))
        if upgrade is None:
            blockers.append(_blocker("generated-card-guid-or-placement-unresolved", f"card[{index}].upgrade"))
        if guid is None:
            blockers.append(_blocker("generated-card-guid-or-placement-unresolved", f"card[{index}].guid"))
        card_destination, card_destination_type = _destination_pair(
            raw_card,
            label=f"cards[{index}]",
            blockers=blockers,
        )
        if (
            card_destination is not None
            and destination is not None
            and card_destination != destination
        ):
            blockers.append(_blocker("generated-card-destination-mismatch", f"cards[{index}]"))
        if (
            card_destination_type is not None
            and destination_type is not None
            and card_destination_type != destination_type
        ):
            blockers.append(_blocker("generated-card-destination-mismatch", f"cards[{index}]"))
        if target_card_id is not None and card_id is not None and card_id != target_card_id:
            blockers.append(_blocker("generated-card-target-mismatch", f"cards[{index}].card_id"))
        if target_upgrade is not None and upgrade is not None and upgrade != target_upgrade:
            blockers.append(_blocker("generated-card-target-mismatch", f"cards[{index}].upgrade"))
        target_match = raw_card.get("target_match", _MISSING)
        if target_match is not _MISSING and target_match is not True:
            blockers.append(_blocker("generated-card-target-mismatch", f"cards[{index}].target_match"))
        guid_creation = _mapping(raw_card.get("guid_creation"))
        if guid_creation is None:
            blockers.append(_blocker("generated-card-guid-or-placement-unresolved", f"cards[{index}].guid_creation"))
        else:
            if guid_creation.get("observed") is not True:
                blockers.append(_blocker("generated-card-guid-or-placement-unresolved", f"cards[{index}].guid_creation.observed"))
            if guid_creation.get("preexisting") is not False:
                blockers.append(_blocker("generated-card-guid-or-placement-unresolved", f"cards[{index}].guid_creation.preexisting"))
            if guid_creation.get("readable") is not True:
                blockers.append(_blocker("generated-card-guid-or-placement-unresolved", f"cards[{index}].guid_creation.readable"))
        order_values: list[tuple[str, object]] = []
        for name in ("destination_order", "insertion_index", "order"):
            order_values.append((name, raw_card.get(name, _MISSING)))
        parsed_orders = [_integer(value, minimum=0) for _name, value in order_values]
        if any(value is None for value in parsed_orders):
            blockers.append(_blocker("generated-card-guid-or-placement-unresolved", f"cards[{index}].destination_order"))
            card_order = None
        else:
            card_order = parsed_orders[0]
            if any(value != card_order for value in parsed_orders[1:]):
                blockers.append(_blocker("generated-card-placement-ambiguous", f"cards[{index}]"))
        if None not in (ordinal, card_id, upgrade, guid, card_destination, card_destination_type, card_order):
            assert ordinal is not None and card_id is not None and upgrade is not None and guid is not None
            assert card_destination is not None and card_destination_type is not None and card_order is not None
            cards.append(
                GeneratedCardObservation(
                    ordinal,
                    card_id,
                    upgrade,
                    guid,
                    card_destination,
                    card_destination_type,
                    card_order,
                )
            )
            ordinals.append(ordinal)
            guids.append(guid)
    if len(set(ordinals)) != len(ordinals):
        blockers.append(_blocker("generated-card-ordinal-duplicate"))
    if len(set(guids)) != len(guids):
        blockers.append(_blocker("generated-card-guid-duplicate"))
    cards.sort(key=lambda value: value.ordinal)
    if cards and tuple(value.ordinal for value in cards) != tuple(range(len(cards))):
        blockers.append(_blocker("generated-card-ordinal-ambiguous"))

    placement = _mapping(raw.get("placement"))
    if placement is None:
        blockers.append(_blocker("card-move-list-capture-failed", "placement"))
        raw_calls: tuple[object, ...] = ()
    else:
        raw_calls = _sequence(placement.get("calls")) or ()
        if "calls" not in placement:
            blockers.append(_blocker("card-move-list-capture-failed", "placement.calls"))
    calls: list[GeneratedCardPlacementCall] = []
    for index, raw_call_value in enumerate(raw_calls):
        raw_call = _mapping(raw_call_value)
        if raw_call is None:
            blockers.append(_blocker("deck-random-placement-call-count-mismatch", f"call[{index}]"))
            continue
        minimum = _integer(raw_call.get("minimum"))
        maximum = _integer(raw_call.get("maximum"))
        result = _integer(raw_call.get("result"), minimum=0)
        if minimum is None or maximum is None or result is None:
            blockers.append(_blocker("deck-random-placement-call-count-mismatch", f"call[{index}]"))
            continue
        if minimum > maximum or not minimum <= result <= maximum:
            blockers.append(
                _blocker(
                    "generated-card-placement-ambiguous",
                    f"call[{index}]:range={minimum}..{maximum};result={result}",
                )
            )
        calls.append(GeneratedCardPlacementCall(minimum, maximum, result))
    if len(calls) != len(cards):
        blockers.append(
            _blocker(
                "deck-random-placement-call-count-mismatch",
                f"calls={len(calls)};cards={len(cards)}",
            )
        )
    for index, (card, call) in enumerate(zip(cards, calls)):
        if card.destination_order != call.result:
            blockers.append(
                _blocker(
                    "generated-card-placement-ambiguous",
                    f"ordinal={card.ordinal};card={card.destination_order};call={call.result};index={index}",
                )
            )

    observation: GeneratedCardIdentityObservation | None = None
    allocator_input: Plan2NativeGeneratedAllocatorInput | None = None
    if (
        action_order is not None
        and source_guid is not None
        and effect_id is not None
        and effect_type_value is not None
        and target_card_id is not None
        and target_upgrade is not None
        and destination is not None
        and destination_type is not None
        and cards
    ):
        observation = GeneratedCardIdentityObservation(
            action_order=action_order,
            source_guid=source_guid,
            effect_id=effect_id,
            effect_type=effect_type_text,
            effect_type_value=effect_type_value,
            target_card_id=target_card_id,
            target_upgrade=target_upgrade,
            destination=destination,
            destination_type=destination_type,
            cards=tuple(cards),
            placement_calls=tuple(calls),
        )
        if not blockers:
            allocator_input = Plan2NativeGeneratedAllocatorInput(
                source_guid=source_guid,
                guid_tokens=observation.guid_tokens,
                # The recorder contract does not expose the Plan2 search
                # ignore list.  An empty tuple is explicit; it is never
                # recovered from a later native state.
                plan_ignore_card_ids=(),
            )
    if not blockers and (observation is None or allocator_input is None):
        blockers.append(_blocker("generated-card-guid-or-placement-unresolved"))
    return GeneratedCardAdapterResult(
        observation,
        allocator_input,
        _dedupe_blockers(blockers),
    )


def adapt_generated_card_identity_rows(
    rows: object,
) -> GeneratedCardIdentityBatchResult:
    """Adapt standalone/transition identity rows and reject ambiguity.

    Identical standalone and transition copies are one observation.  A second
    row with the same action order but any differing source/effect/destination
    or card placement is rejected instead of selecting one arbitrarily.
    """

    raw_values, extraction_blockers = _extract_identity_values(rows)
    blockers: list[GeneratedCardAdapterBlocker] = list(extraction_blockers)
    by_order: dict[int, GeneratedCardIdentityObservation] = {}
    for index, raw in enumerate(raw_values):
        result = adapt_generated_card_identity(raw)
        blockers.extend(result.blockers)
        if result.observation is None or result.allocator_input is None:
            continue
        observation = result.observation
        previous = by_order.get(observation.action_order)
        if previous is None:
            by_order[observation.action_order] = observation
        elif previous.signature() != observation.signature():
            blockers.append(
                _blocker(
                    "generated-identity-action-order-ambiguous",
                    f"action_order={observation.action_order};row={index}",
                )
            )
    observations = tuple(by_order[key] for key in sorted(by_order))
    all_guids: list[str] = []
    for observation in observations:
        all_guids.extend(observation.guid_tokens)
    if len(set(all_guids)) != len(all_guids):
        blockers.append(_blocker("generated-card-guid-duplicate"))
    # A batch is an atomic hand-off.  Diagnostics retain the observations so
    # callers can explain a conflict, but no partially trusted allocator map
    # escapes when any row is blocked.
    inputs = () if blockers else tuple(
        Plan2NativeGeneratedAllocatorInput(
            source_guid=observation.source_guid,
            guid_tokens=observation.guid_tokens,
            plan_ignore_card_ids=(),
        )
        for observation in observations
        if observation.action_order in by_order
    )
    return GeneratedCardIdentityBatchResult(
        observations=observations,
        allocator_inputs=inputs,
        blockers=_dedupe_blockers(blockers),
    )


def adapt_generated_card_identity_source(source: object) -> GeneratedCardIdentityBatchResult:
    """Read a JSON/JSONL source and adapt all exact generated identities."""

    loaded, load_blockers = _load_source(source)
    if loaded is None:
        return GeneratedCardIdentityBatchResult(blockers=load_blockers)
    result = adapt_generated_card_identity_rows(loaded)
    return GeneratedCardIdentityBatchResult(
        observations=result.observations,
        allocator_inputs=result.allocator_inputs,
        blockers=_dedupe_blockers((*load_blockers, *result.blockers)),
    )


def generated_inputs_for_transition_local(
    source: object,
) -> GeneratedCardIdentityBatchResult:
    """Return the exact action-order map consumed by transition-local replay."""

    return adapt_generated_card_identity_source(source)


# Descriptive aliases keep the boundary discoverable for callers that use
# ``record``/``projection``/``allocator`` terminology.
adapt_generated_card_identity_record = adapt_generated_card_identity
adapt_generated_card_identity_to_plan2 = adapt_generated_card_identity
generated_card_identity_to_plan2_input = adapt_generated_card_identity
project_generated_card_identity = adapt_generated_card_identity
build_plan2_native_generated_allocator_input = adapt_generated_card_identity
build_plan2_native_generated_input = adapt_generated_card_identity
build_plan2_native_generated_allocator_inputs = adapt_generated_card_identity_rows
adapt_generated_card_identities = adapt_generated_card_identity_rows
read_generated_card_identity_source = adapt_generated_card_identity_source


__all__ = [
    "CARD_CREATE_ID_EFFECT_TYPE",
    "CARD_CREATE_ID_EFFECT_VALUE",
    "DESTINATION_TYPES",
    "DESTINATION_VALUES",
    "GENERATED_CARD_IDENTITY_RECORD",
    "GENERATED_CARD_IDENTITY_SCHEMA",
    "GeneratedCardAdapterBlocker",
    "GeneratedCardAdapterError",
    "GeneratedCardAdapterResult",
    "GeneratedCardIdentityBatchResult",
    "GeneratedCardIdentityObservation",
    "GeneratedCardObservation",
    "GeneratedCardPlacementCall",
    "adapt_generated_card_identity",
    "adapt_generated_card_identity_to_plan2",
    "adapt_generated_card_identity_record",
    "adapt_generated_card_identities",
    "adapt_generated_card_identity_rows",
    "adapt_generated_card_identity_source",
    "build_plan2_native_generated_allocator_input",
    "build_plan2_native_generated_allocator_inputs",
    "build_plan2_native_generated_input",
    "generated_card_identity_to_plan2_input",
    "generated_inputs_for_transition_local",
    "project_generated_card_identity",
    "read_generated_card_identity_source",
]
