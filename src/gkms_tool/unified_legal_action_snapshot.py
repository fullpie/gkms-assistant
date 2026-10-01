"""Read-only native legal-action snapshots for settled ExamSave boundaries.

The inner transition datasets need one candidate surface that covers every
player input offered by the game at *one* settled boundary.  The older Maa
sidecars deliberately publish card, drink, and end-turn surfaces separately;
joining those rows by their chosen action would be an unsafe inference.  This
module is the small, explicit bridge for callers that already have a typed
native state (or a flow-specific exact enumerator).

The provider never dispatches input, reads a controller, or treats inventory
membership as legality.  It only accepts an explicit completeness claim from
one of the reviewed native enumerators and normalises their typed actions to
the plan-neutral ``play``/``drink``/``end_turn`` wire shape.  A drink that
requires a card target is accepted only when the exact enumerator supplies
that target; a list of drink IDs alone is not enough.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeAlias

from .audition_local_save_state import (
    AuditionLocalSaveStateEvidence,
    LocalSaveExamState,
)
from .nia_native_action_contract import (
    KIND_DRINK,
    KIND_END_TURN,
    KIND_PLAY,
    NiaNativeAction,
)
from .master_db import DEFAULT_DATABASE
from .plan2_exam_mode import (
    Plan2ScheduledGimmickHook,
    Plan2ScheduledGimmickHookKey,
)


SCHEMA = "gkms.unified-legal-action-snapshot.v1"
UNIFIED_CANDIDATE_SET_KIND = "unified"
UNKNOWN_CANDIDATE_SET_KIND = "unknown"
UNIFIED_LEGAL_ACTION_AUTHORITY = "native-exact-unified-root-enumerator-v1"

FlowKey: TypeAlias = tuple[str, str, str]
NativeEnumerator: TypeAlias = Callable[[object], object]

_PLAN_TYPES = {
    2: "ProducePlanType_Plan1",
    3: "ProducePlanType_Plan2",
    4: "ProducePlanType_Plan3",
}
_EFFECT_TYPES = {
    2: "ProduceExamEffectType_ExamParameterBuff",
    10: "ProduceExamEffectType_ExamLessonBuff",
    31: "ProduceExamEffectType_ExamReview",
    42: "ProduceExamEffectType_ExamCardPlayAggressive",
    45: "ProduceExamEffectType_ExamConcentration",
}


class UnifiedLegalActionSnapshotError(ValueError):
    """The supplied boundary cannot prove one complete legal action set."""


def _text(value: object, label: str, *, optional: bool = False) -> str:
    if optional and value in (None, ""):
        return ""
    if not isinstance(value, str) or not value.strip():
        raise UnifiedLegalActionSnapshotError(f"{label} must be non-empty text")
    return value.strip()


def _nonnegative(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise UnifiedLegalActionSnapshotError(
            f"{label} must be a non-negative integer"
        )
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise UnifiedLegalActionSnapshotError(f"{label} must be an object")
    return value


def _detach_json(value: object, label: str) -> object:
    """Detach a candidate without accepting NaN/Infinity or custom objects."""

    try:
        result = json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as error:
        raise UnifiedLegalActionSnapshotError(
            f"{label} is not JSON-compatible"
        ) from error
    return result


def _state_of(value: object) -> object:
    if isinstance(value, AuditionLocalSaveStateEvidence):
        return value.state
    # Test/integration envelopes intentionally mirror the typed evidence
    # shape without subclassing it.  Treat an explicit ``.state`` member as
    # an envelope; never inspect arbitrary mappings this way because a state
    # mapping may itself contain a user field named ``state``.
    if not isinstance(value, Mapping):
        nested = getattr(value, "state", None)
        if nested is not None:
            return nested
    return value


def _member(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _json_value(value: object) -> object:
    to_value = getattr(value, "to_value", None)
    if callable(to_value):
        try:
            return to_value()
        except (TypeError, ValueError, RuntimeError):
            return None
    return value


def _opaque(value: object) -> Mapping[str, object] | None:
    state = _state_of(value)
    runtime = _member(state, "root_runtime")
    fields = _json_value(_member(runtime, "opaque_fields"))
    return fields if isinstance(fields, Mapping) else None


def _exam_drink_inventory(value: object) -> tuple[dict[str, object], ...] | None:
    """Project only the ordered drink identity fields from typed ExamSave."""

    fields = _opaque(value)
    if fields is None:
        return None
    raw = fields.get("drinkList")
    if not isinstance(raw, list):
        return None if raw is None else ()
    result: list[dict[str, object]] = []
    for index, row in enumerate(raw):
        if not isinstance(row, Mapping):
            return None
        drink_id = row.get("_id", row.get("id", row.get("drink_id")))
        if not isinstance(drink_id, str) or not drink_id:
            return None
        instance = row.get(
            "_instanceId",
            row.get("instance_id", row.get("instanceId", row.get("_uid", row.get("uid")))),
        )
        instance_id = (
            str(instance)
            if isinstance(instance, (str, int)) and not isinstance(instance, bool) and str(instance)
            else f"localsave-drink:{index}:{drink_id}"
        )
        result.append(
            {
                "slot_index": index,
                "instance_id": instance_id,
                "drink_id": drink_id,
            }
        )
    return tuple(result)


def _digest(value: object) -> str | None:
    digest = getattr(value, "digest", None)
    if callable(digest):
        try:
            result = digest()
        except (TypeError, ValueError, RuntimeError, OSError):
            result = None
        if isinstance(result, str) and result:
            return result
    state = _state_of(value)
    to_dict = getattr(state, "to_dict", None)
    if not callable(to_dict):
        return None
    try:
        payload = to_dict()
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _flow_from_exam(value: object) -> FlowKey | None:
    opaque = _opaque(value)
    if opaque is None:
        return None
    produce_id = opaque.get("produceId")
    if not isinstance(produce_id, str) or not produce_id:
        return None
    plan_value = opaque.get("planType")
    plan_type = (
        _PLAN_TYPES.get(plan_value) if isinstance(plan_value, int) else plan_value
    )
    if not isinstance(plan_type, str) or not plan_type:
        return None
    effect_value = opaque.get("mainEffectType", opaque.get("displayMainEffectType"))
    effect_type = (
        _EFFECT_TYPES.get(effect_value)
        if isinstance(effect_value, int)
        else effect_value
    )
    if not isinstance(effect_type, str) or not effect_type:
        return None
    return produce_id, plan_type, effect_type


def _flow_value(value: object, *, source: object) -> FlowKey:
    if value is None:
        resolved = _flow_from_exam(source)
        if resolved is None:
            raise UnifiedLegalActionSnapshotError("flow-identity-unavailable")
        return resolved
    if isinstance(value, str):
        fields = tuple(value.split("|"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        fields = tuple(value)
    else:
        fields = ()
    if len(fields) != 3 or any(not isinstance(item, str) or not item for item in fields):
        raise UnifiedLegalActionSnapshotError(
            "flow must contain produce/plan/effect"
        )
    return fields  # type: ignore[return-value]


def _raw_candidates(result: object) -> tuple[object, ...] | None:
    if isinstance(result, Mapping):
        values = result.get(
            "candidates",
            result.get("legal_candidates", result.get("actions")),
        )
    else:
        values = getattr(
            result,
            "candidates",
            getattr(result, "legal_candidates", getattr(result, "actions", None)),
        )
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        return None
    return tuple(values)


def _is_complete(result: object) -> bool:
    if isinstance(result, Mapping):
        return result.get("complete") is True
    return getattr(result, "complete", False) is True


def _result_blockers(result: object) -> tuple[str, ...]:
    raw = result.get("blockers", ()) if isinstance(result, Mapping) else getattr(result, "blockers", ())
    if raw is None:
        return ()
    if isinstance(raw, (str, bytes, bytearray)):
        return (str(raw),)
    if not isinstance(raw, Sequence):
        return (f"native-enumerator-blockers-invalid:{type(raw).__name__}",)
    values: list[str] = []
    for value in raw:
        if isinstance(value, str) and value:
            values.append(value)
        else:
            code = getattr(value, "code", None)
            detail = getattr(value, "detail", None)
            if isinstance(code, str) and code:
                values.append(code if not detail else f"{code}:{detail}")
            else:
                values.append(f"native-enumerator-blocker-invalid:{type(value).__name__}")
    return tuple(dict.fromkeys(values))


def _kind(value: object) -> str | None:
    if isinstance(value, Mapping):
        raw = value.get("kind", value.get("action_type", value.get("native_kind")))
    else:
        raw = getattr(value, "kind", getattr(value, "action_type", None))
    if not isinstance(raw, str):
        return None
    return {
        "card": KIND_PLAY,
        "use-hand": KIND_PLAY,
        "use_hand": KIND_PLAY,
        "play": KIND_PLAY,
        "drink": KIND_DRINK,
        "use-drink": KIND_DRINK,
        "use_drink": KIND_DRINK,
        "turn-end": KIND_END_TURN,
        "turn_end": KIND_END_TURN,
        "end_turn": KIND_END_TURN,
        "skip": KIND_END_TURN,
    }.get(raw, raw)


def _value(value: object, *names: str, default: object = None) -> object:
    for name in names:
        current = value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)
        if current is not None:
            return current
    return default


def _normalise_candidate(value: object, index: int) -> dict[str, object]:
    """Normalise a reviewed typed action without discarding target identity."""

    if isinstance(value, NiaNativeAction):
        raw: object = value.to_dict()
    elif hasattr(value, "to_dict") and callable(value.to_dict):
        raw = value.to_dict()
    elif isinstance(value, Mapping):
        raw = value
    else:
        # Plan2 typed actions intentionally do not share NiaNativeAction's
        # class.  Their public attributes are still exact native identity.
        raw = {
            "kind": _value(value, "kind", default=None),
            "card_guid": _value(value, "card_guid", "guid", default=None),
            "card_id": _value(value, "card_id", default=None),
            "slot_index": _value(value, "slot_index", "slot", default=None),
            "instance_id": _value(value, "instance_id", "instance", default=None),
            "drink_id": _value(value, "drink_id", default=None),
            "selected_card_guid": _value(
                value, "selected_card_guid", "selection", default=""
            ),
            "action_id": _value(value, "action_id", default=None),
            "hand_slot": _value(value, "hand_slot", "hand_index", default=None),
            "upgrade": _value(value, "upgrade", "card_upgrade", default=None),
        }
    mapping = _mapping(raw, f"native candidate[{index}]")
    kind = _kind(mapping)
    if kind == KIND_PLAY:
        guid = _text(_value(mapping, "card_guid", "guid"), f"candidate[{index}].card_guid")
        card_id = _text(
            _value(mapping, "card_id", default=""),
            f"candidate[{index}].card_id",
            optional=True,
        )
        slot = _value(mapping, "hand_slot", "hand_index", "slot")
        upgrade = _value(mapping, "card_upgrade", "upgrade")
        result: dict[str, object] = {
            "kind": KIND_PLAY,
            "action_id": f"PLAY:{guid}",
            "guid": guid,
            "card_guid": guid,
        }
        if card_id:
            result["card_id"] = card_id
        if slot is not None:
            result["slot"] = _nonnegative(slot, f"candidate[{index}].slot")
        if upgrade is not None:
            result["upgrade"] = _nonnegative(upgrade, f"candidate[{index}].upgrade")
        supplied = _value(mapping, "action_id")
        if supplied is not None and supplied != result["action_id"]:
            raise UnifiedLegalActionSnapshotError(
                f"candidate[{index}].action_id does not match card GUID"
            )
        return result
    if kind == KIND_DRINK:
        slot = _nonnegative(
            _value(mapping, "slot_index", "slot"),
            f"candidate[{index}].slot_index",
        )
        drink_id = _text(
            _value(mapping, "drink_id"), f"candidate[{index}].drink_id"
        )
        instance = _text(
            _value(mapping, "instance_id", "instance", default=""),
            f"candidate[{index}].instance_id",
            optional=True,
        )
        selected = _text(
            _value(mapping, "selected_card_guid", "selection", default=""),
            f"candidate[{index}].selected_card_guid",
            optional=True,
        )
        # Plan 3's exact candidate uses a slot+drink+target key.  Plan 2's
        # typed action includes its synthetic LocalSave instance token.  Keep
        # either exact identity stable instead of folding duplicate slots.
        supplied = _value(mapping, "action_id")
        expected = (
            f"DRINK:{slot}:{instance}:{drink_id}:{selected or '-'}"
            if instance
            else f"DRINK:{slot}:{drink_id}:{selected or '-'}"
        )
        if isinstance(supplied, str) and supplied:
            action_id = supplied
        else:
            action_id = expected
        result = {
            "kind": KIND_DRINK,
            "action_id": action_id,
            "slot_index": slot,
            "drink_id": drink_id,
            "selected_card_guid": selected,
        }
        if instance:
            result["instance_id"] = instance
        # Do not classify a target-less mapping as targetful.  The exact
        # enumerator may legitimately prove an automatic Deck/Grave search;
        # target-required drinks are represented by a non-empty selection.
        return result
    if kind == KIND_END_TURN:
        supplied = _value(mapping, "action_id", default="END_TURN")
        if supplied != "END_TURN":
            raise UnifiedLegalActionSnapshotError(
                f"candidate[{index}].end-turn-action-id-invalid"
            )
        return {"kind": KIND_END_TURN, "action_id": "END_TURN"}
    raise UnifiedLegalActionSnapshotError(
        f"candidate[{index}].unsupported-action-kind:{kind}"
    )


def _validate_target_candidates(
    candidates: Sequence[Mapping[str, object]],
    source: object,
) -> tuple[str, ...]:
    """Check target GUIDs against the exact source zones when available."""

    state = _state_of(source)
    zones = _member(state, "zones")
    if zones is None:
        # A prepared native state may expose zones directly rather than the
        # LocalSave ``zones`` wrapper.
        zones = state
    guids: set[str] = set()
    for zone in ("hand", "deck", "grave", "lost", "hold"):
        values = _member(zones, zone, ())
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
            continue
        for card in values:
            guid = _value(card, "guid", "card_guid")
            if isinstance(guid, str) and guid:
                guids.add(guid)
    if not guids:
        return ()
    blockers: list[str] = []
    for index, candidate in enumerate(candidates):
        if candidate.get("kind") != KIND_DRINK:
            continue
        selected = candidate.get("selected_card_guid")
        if isinstance(selected, str) and selected and selected not in guids:
            blockers.append(f"drink-target-not-in-source-zones:{index}:{selected}")
    return tuple(blockers)


def _enumerate_plan2_state(state: object, catalog: object) -> tuple[tuple[object, ...], tuple[str, ...]]:
    from .plan2_native_expectimax import enumerate_plan2_native_actions
    from .plan2_native_horizon import (
        Plan2NativeHorizonState,
        Plan2NativeProgramCatalog,
        enumerate_plan2_native_drink_actions,
    )

    if not isinstance(state, Plan2NativeHorizonState):
        raise TypeError("plan2 state must be Plan2NativeHorizonState")
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("plan2 catalog must be Plan2NativeProgramCatalog")
    if state.terminal:
        return (), ("plan2-terminal-boundary",)
    enumeration = enumerate_plan2_native_actions(state, catalog)
    # ``enumerate_plan2_native_actions(state, catalog)`` intentionally omits
    # effectless drinks for planner tie-breaking.  A legal-action snapshot
    # must retain every mapped inventory slot, so ask the same native runtime
    # for the unfiltered ordered drink actions here.
    drinks = enumerate_plan2_native_drink_actions(state, None)
    actions: list[object] = [*drinks]
    actions.extend(
        value
        for value in enumeration.actions
        if getattr(value, "kind", None) != "drink"
    )
    blockers: list[str] = []
    if state.unmapped_drink_slots:
        blockers.extend(
            f"unmapped-drink-slot:{value}" for value in state.unmapped_drink_slots
        )
    # Cost/condition failures are ordinary ineligibility.  Unsupported
    # programs or opaque global queues mean that the exact Hand set was not
    # enumerated and therefore block completeness.
    nonfatal = {
        "illegal-action-no-playable-count",
        "card-play-condition-not-met",
        "illegal-action-cost-rejected",
    }
    for blocker in enumeration.blockers:
        if blocker.code not in nonfatal:
            blockers.append(
                blocker.code if not blocker.detail else f"{blocker.code}:{blocker.detail}"
            )
    return tuple(actions), tuple(dict.fromkeys(blockers))


@lru_cache(maxsize=8)
def _compile_plan2_catalog_cached(
    database_path: str,
    database_mtime_ns: int,
    database_size: int,
) -> object:
    """Compile one immutable Plan2 catalog for one Master file identity.

    The catalog compiler walks the complete Master graph and is intentionally
    expensive.  A unified provider is called once per settled boundary, so
    recompiling that graph for every row would make corpus auditing needlessly
    slow.  ``mtime_ns`` and ``size`` are part of the key so a replaced or
    edited database automatically gets a fresh catalog; the returned catalog
    is immutable by contract (all of its native program records are frozen).
    """

    from .plan2_native_program_catalog import compile_plan2_native_program_catalog

    compilation = compile_plan2_native_program_catalog(database=Path(database_path))
    return compilation.catalog


def _compile_plan2_catalog(database: str | Path) -> object:
    """Resolve and cache a Plan2 Master catalog without masking file errors."""

    path = Path(database).resolve()
    try:
        stat = path.stat()
    except OSError:
        # Preserve the compiler's established FileNotFoundError/OS error and
        # let the public snapshot builder convert it into a typed blocker.
        from .plan2_native_program_catalog import compile_plan2_native_program_catalog

        return compile_plan2_native_program_catalog(database=path).catalog
    return _compile_plan2_catalog_cached(
        str(path),
        stat.st_mtime_ns,
        stat.st_size,
    )


def _plan2_gimmick_hooks(
    source: AuditionLocalSaveStateEvidence,
    *,
    database: str | Path,
) -> Mapping[Plan2ScheduledGimmickHookKey, Plan2ScheduledGimmickHook] | None:
    """Bind only future ExamSave gimmicks with a reviewed Master program.

    The serialized schedule is authoritative for group, effect, and turn.  A
    hook is added only after the complete group and effect rows are validated
    by the existing Master-backed loader.  Unknown groups are deliberately
    left unbound; the bootstrap then reports the exact missing effect instead
    of fabricating a default gimmick.
    """

    state = source.state
    opaque = _opaque(source)
    if opaque is None:
        return None
    raw_schedule = opaque.get("gimmickList")
    if not isinstance(raw_schedule, list):
        return None
    current_turn = _member(state, "current_turn")
    if isinstance(current_turn, bool) or not isinstance(current_turn, int):
        return None
    future_groups: set[str] = set()
    for row in raw_schedule:
        if not isinstance(row, Mapping):
            continue
        turn = row.get("turn")
        group_id = row.get("gimmickGroupId")
        if (
            isinstance(turn, int)
            and not isinstance(turn, bool)
            and turn > current_turn
            and isinstance(group_id, str)
            and group_id
        ):
            future_groups.add(group_id)
    if not future_groups:
        return {}

    from .initial_regular_plan2_gimmick_runtime import (
        InitialRegularPlan2GimmickRuntimeBlocked,
        build_plan2_master_review_gimmick_hooks,
        load_plan2_master_review_gimmick_group,
    )

    hooks: dict[
        Plan2ScheduledGimmickHookKey,
        Plan2ScheduledGimmickHook,
    ] = {}
    for group_id in sorted(future_groups):
        try:
            group = load_plan2_master_review_gimmick_group(
                group_id,
                database=Path(database).resolve(),
            )
            hooks.update(build_plan2_master_review_gimmick_hooks(group))
        except InitialRegularPlan2GimmickRuntimeBlocked:
            # Direct Review/Block/ReviewValueMultiple/StatusEnchant groups
            # are the currently reviewed exact surface.  Other Master groups
            # remain explicit blockers in compile_plan2_scheduled_gimmicks.
            continue
    return hooks


def _enumerate_plan1_state(
    state: object,
    compilation: object,
    *,
    drink_inventory: Sequence[object] | None,
    drink_candidate_provider: Callable[..., Sequence[object]] | None,
    settings: object | None,
    hand_add_support_resolver: object | None,
) -> tuple[tuple[object, ...], tuple[str, ...]]:
    from .nia_plan1_native_sidecar import enumerate_plan1_native_legal_candidates
    from .plan1_native_core import Plan1CompiledCard, Plan1DeckCompilation, Plan1NativeSettings
    from .plan1_native_stage import Plan1NativeStageState

    if not isinstance(state, Plan1NativeStageState):
        raise TypeError("plan1 state must be Plan1NativeStageState")
    if not isinstance(compilation, (Plan1DeckCompilation, Sequence)):
        raise TypeError("plan1 compilation must be Plan1DeckCompilation or sequence")
    if settings is not None and not isinstance(settings, Plan1NativeSettings):
        raise TypeError("plan1 settings must be Plan1NativeSettings or None")
    result = enumerate_plan1_native_legal_candidates(
        state,
        compilation,
        settings=settings,
        hand_add_support_resolver=hand_add_support_resolver,
        drink_inventory=drink_inventory,
        drink_candidate_provider=drink_candidate_provider,
        include_end_turn=True,
        end_turn_available=state.scalar.plays_remaining > 0,
    )
    return tuple(result.candidates), tuple(result.blockers)


def _enumerate_plan3_state(
    state: object,
    native_state: object,
    *,
    search_options: Mapping[str, object],
) -> tuple[tuple[object, ...], tuple[str, ...]]:
    from .plan3_engine import Plan3State
    from .plan3_native_search import enumerate_plan3_native_legal_candidates
    from .plan3_native_state import Plan3NativeState

    if not isinstance(state, Plan3State):
        raise TypeError("plan3 state must be Plan3State")
    if not isinstance(native_state, Plan3NativeState):
        raise TypeError("plan3 native state must be Plan3NativeState")
    options = dict(search_options)
    options.setdefault("include_drinks", True)
    options.setdefault("include_turn_end", True)
    result = enumerate_plan3_native_legal_candidates(state, native_state, **options)
    return tuple(result.candidates), tuple(result.blockers)


def _exam_settled(source: object) -> tuple[bool, str | None]:
    state = _state_of(source)
    settled = _member(state, "is_native_actionable_settled", None)
    if type(settled) is not bool:
        # Prepared native states do not carry the LocalSave evidence
        # property, but their own constructors expose the same settled-main
        # boundary.  This branch is only for callers that explicitly pass a
        # prepared exact native state; the ExamSave path above remains strict.
        type_name = type(state).__name__
        if type_name == "Plan2NativeHorizonState":
            terminal = _member(state, "terminal", False)
            phase = _member(state, "phase", "main")
            queue = _member(state, "command_queue", ())
            opaque_queue = _member(state, "opaque_status_queue", ())
            if (
                terminal is False
                and phase == "main"
                and isinstance(queue, Sequence)
                and not queue
                and isinstance(opaque_queue, Sequence)
                and not opaque_queue
            ):
                return True, None
        if type_name == "Plan3State":
            turns = _member(state, "turns_remaining", 0)
            awaiting = _member(state, "awaiting_turn_start", False)
            if type(turns) is int and turns > 0 and awaiting is False:
                return True, None
        if type_name == "Plan1NativeStageState":
            scalar = _member(state, "scalar")
            plays = _member(scalar, "plays_remaining", 0)
            if type(plays) is int and plays >= 0:
                return True, None
        return False, "settled-boundary-proof-missing"
    if not settled:
        return False, "exam-save-not-actionable-settled"
    return True, None


@dataclass(frozen=True, slots=True)
class UnifiedLegalActionSnapshot:
    """Immutable complete candidate set at one settled native boundary."""

    flow: FlowKey
    boundary_digest: str
    candidates: tuple[Mapping[str, object], ...]
    authority: str = UNIFIED_LEGAL_ACTION_AUTHORITY
    complete: bool = True
    blockers: tuple[str, ...] = ()
    source: str = "typed-examsave-native-enumerator"
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise UnifiedLegalActionSnapshotError("unsupported snapshot schema")
        flow = self.flow
        if isinstance(flow, str):
            flow = tuple(flow.split("|"))  # type: ignore[assignment]
        else:
            flow = tuple(flow)
        if len(flow) != 3 or any(
            not isinstance(value, str) or not value for value in flow
        ):
            raise UnifiedLegalActionSnapshotError("snapshot.flow is invalid")
        object.__setattr__(self, "flow", flow)
        _text(self.boundary_digest, "snapshot.boundary_digest")
        _text(self.authority, "snapshot.authority")
        _text(self.source, "snapshot.source")
        if type(self.complete) is not bool:
            raise TypeError("snapshot.complete must be bool")
        blockers = tuple(self.blockers)
        if any(not isinstance(value, str) or not value for value in blockers):
            raise UnifiedLegalActionSnapshotError("snapshot blockers are invalid")
        values = tuple(self.candidates)
        if any(not isinstance(value, Mapping) for value in values):
            raise UnifiedLegalActionSnapshotError("snapshot candidates must be objects")
        normalised = tuple(_normalise_candidate(value, index) for index, value in enumerate(values))
        identities = tuple(str(value["action_id"]) for value in normalised)
        if len(identities) != len(set(identities)):
            raise UnifiedLegalActionSnapshotError("duplicate-unified-action-id")
        if self.complete:
            if not normalised:
                raise UnifiedLegalActionSnapshotError("complete-unified-set-empty")
            if blockers:
                raise UnifiedLegalActionSnapshotError(
                    "complete-unified-set-has-blockers"
                )
            if "END_TURN" not in identities:
                raise UnifiedLegalActionSnapshotError("end-turn-legality-unproven")
        elif normalised and not blockers:
            raise UnifiedLegalActionSnapshotError(
                "incomplete-unified-set-needs-blocker"
            )
        detached = tuple(
            _detach_json(value, f"snapshot.candidates[{index}]")
            for index, value in enumerate(normalised)
        )
        if any(not isinstance(value, Mapping) for value in detached):
            raise UnifiedLegalActionSnapshotError("snapshot candidate detach failed")
        object.__setattr__(self, "candidates", detached)
        object.__setattr__(self, "blockers", tuple(dict.fromkeys(blockers)))

    @property
    def flow_id(self) -> str:
        return "|".join(self.flow)

    @property
    def state_digest(self) -> str:
        """Alias retained for consumers that call the boundary a state."""

        return self.boundary_digest

    @property
    def candidate_set_kind(self) -> str:
        return UNIFIED_CANDIDATE_SET_KIND if self.complete else UNKNOWN_CANDIDATE_SET_KIND

    @property
    def full_rl_policy_ready(self) -> bool:
        return self.complete

    @property
    def legal_candidates(self) -> tuple[Mapping[str, object], ...]:
        return self.candidates

    @property
    def native_candidates(self) -> tuple[NiaNativeAction, ...]:
        """Return plan-neutral typed actions for N.I.A. collector adapters."""

        return tuple(NiaNativeAction.from_value(value) for value in self.candidates)

    def to_nia_candidate_set(self) -> object:
        """Convert to the existing collector set without importing it eagerly."""

        from .nia_native_action_contract import NiaNativeCandidateSet

        return NiaNativeCandidateSet(
            self.native_candidates,
            authority=self.authority,
            complete=self.complete,
            blockers=self.blockers,
        )

    @property
    def action_ids(self) -> tuple[str, ...]:
        return tuple(str(value["action_id"]) for value in self.candidates)

    def contains(self, action: object) -> bool:
        try:
            candidate = _normalise_candidate(action, 0)
        except (TypeError, ValueError, UnifiedLegalActionSnapshotError):
            return False
        return candidate["action_id"] in self.action_ids

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "flow": self.flow_id,
            "flow_key": list(self.flow),
            "boundary_digest": self.boundary_digest,
            "state_digest": self.state_digest,
            "authority": self.authority,
            "source": self.source,
            "complete": self.complete,
            "candidate_set_kind": self.candidate_set_kind,
            "full_rl_policy_ready": self.full_rl_policy_ready,
            "blockers": list(self.blockers),
            "candidates": [dict(value) for value in self.candidates],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "UnifiedLegalActionSnapshot":
        """Load a detached snapshot artifact and re-run all identity checks."""

        if not isinstance(payload, Mapping):
            raise UnifiedLegalActionSnapshotError("snapshot must be an object")
        required = {
            "schema",
            "flow",
            "boundary_digest",
            "authority",
            "source",
            "complete",
            "blockers",
            "candidates",
        }
        if not required <= set(payload):
            raise UnifiedLegalActionSnapshotError("snapshot fields are incomplete")
        raw_candidates = payload["candidates"]
        raw_blockers = payload["blockers"]
        if not isinstance(raw_candidates, Sequence) or isinstance(
            raw_candidates, (str, bytes, bytearray)
        ):
            raise UnifiedLegalActionSnapshotError("snapshot.candidates must be an array")
        if not isinstance(raw_blockers, Sequence) or isinstance(
            raw_blockers, (str, bytes, bytearray)
        ):
            raise UnifiedLegalActionSnapshotError("snapshot.blockers must be an array")
        flow = payload["flow"]
        return cls(
            flow=_flow_value(flow, source=payload),  # type: ignore[arg-type]
            boundary_digest=_text(payload["boundary_digest"], "snapshot.boundary_digest"),
            candidates=tuple(
                _mapping(value, f"snapshot.candidates[{index}]")
                for index, value in enumerate(raw_candidates)
            ),
            authority=_text(payload["authority"], "snapshot.authority"),
            complete=payload["complete"],  # type: ignore[arg-type]
            blockers=tuple(
                _text(value, f"snapshot.blockers[{index}]")
                for index, value in enumerate(raw_blockers)
            ),
            source=_text(payload["source"], "snapshot.source"),
            schema=_text(payload["schema"], "snapshot.schema"),
        )


def build_unified_legal_action_snapshot(
    source: object,
    *,
    flow: FlowKey | str | Sequence[str] | None = None,
    native_enumerator: NativeEnumerator | None = None,
    plan1_state: object | None = None,
    plan1_compilation: object | None = None,
    plan1_settings: object | None = None,
    plan1_drink_inventory: Sequence[object] | None = None,
    plan1_drink_candidate_provider: Callable[..., Sequence[object]] | None = None,
    plan1_hand_add_support_resolver: object | None = None,
    plan2_state: object | None = None,
    plan2_catalog: object | None = None,
    plan2_exam_setting_master_dir: str | Path | None = None,
    database: str | Path = DEFAULT_DATABASE,
    plan3_state: object | None = None,
    plan3_native_state: object | None = None,
    plan3_search_options: Mapping[str, object] | None = None,
    authority: str = UNIFIED_LEGAL_ACTION_AUTHORITY,
    source_label: str = "typed-examsave-native-enumerator",
) -> UnifiedLegalActionSnapshot:
    """Build one exact unified set, abstaining on every unresolved family.

    ``native_enumerator`` is the preferred integration seam.  It must return
    an existing typed enumeration with ``complete=True``; a bare list is
    intentionally rejected because list membership does not prove legality.
    The plan-specific keyword inputs are convenience adapters for the exact
    Plan1/Plan2/Plan3 enumerators and are never combined heuristically.
    """

    if not callable(native_enumerator) and native_enumerator is not None:
        raise TypeError("native_enumerator must be callable or None")
    if not callable(plan1_drink_candidate_provider) and plan1_drink_candidate_provider is not None:
        raise TypeError("plan1_drink_candidate_provider must be callable or None")
    if plan3_search_options is not None and not isinstance(plan3_search_options, Mapping):
        raise TypeError("plan3_search_options must be a mapping or None")
    digest = _digest(source)
    if digest is None:
        digest = _digest(_state_of(source))
    blockers: list[str] = []
    if digest is None:
        blockers.append("settled-boundary-digest-unavailable")
        digest = "unidentified-boundary"
    settled, settled_blocker = _exam_settled(source)
    if not settled and settled_blocker:
        blockers.append(settled_blocker)
    try:
        flow_key = _flow_value(flow, source=source)
    except UnifiedLegalActionSnapshotError as error:
        flow_key = ("unknown", "unknown", "unknown")
        blockers.append(str(error))

    raw_actions: tuple[object, ...] = ()
    if not blockers:
        try:
            if native_enumerator is not None:
                enumeration = native_enumerator(source)
                if not _is_complete(enumeration):
                    blockers.extend(_result_blockers(enumeration) or ("native-enumerator-incomplete",))
                else:
                    values = _raw_candidates(enumeration)
                    if values is None:
                        blockers.append("native-enumerator-candidates-missing")
                    else:
                        raw_actions = values
                        blockers.extend(_result_blockers(enumeration))
            elif plan2_state is not None:
                if plan2_catalog is None:
                    plan2_catalog = _compile_plan2_catalog(database)
                raw_actions, native_blockers = _enumerate_plan2_state(
                    plan2_state, plan2_catalog
                )
                blockers.extend(native_blockers)
            elif plan1_state is not None:
                if plan1_drink_inventory is None:
                    # For a LocalSave-backed Plan1 projection, an explicit
                    # empty ``drinkList`` proves there are no drinks.  A
                    # non-empty list still requires the reviewed Plan1
                    # drink-target provider; IDs alone are never promoted.
                    plan1_drink_inventory = _exam_drink_inventory(source)
                raw_actions, native_blockers = _enumerate_plan1_state(
                    plan1_state,
                    plan1_compilation,
                    drink_inventory=plan1_drink_inventory,
                    drink_candidate_provider=plan1_drink_candidate_provider,
                    settings=plan1_settings,
                    hand_add_support_resolver=plan1_hand_add_support_resolver,
                )
                blockers.extend(native_blockers)
            elif plan3_state is not None or plan3_native_state is not None:
                if plan3_state is None or plan3_native_state is None:
                    blockers.append("plan3-state-pair-incomplete")
                else:
                    raw_actions, native_blockers = _enumerate_plan3_state(
                        plan3_state,
                        plan3_native_state,
                        search_options=(plan3_search_options or {}),
                    )
                    blockers.extend(native_blockers)
            elif (
                isinstance(source, AuditionLocalSaveStateEvidence)
                and flow_key[1] == "ProducePlanType_Plan2"
            ):
                # This is the only fully self-contained ExamSave bridge in
                # the current codebase.  It reuses the exact catalog and
                # LocalSave bootstrap used by the native Plan2 orchestrator;
                # no card/drink action is reconstructed from the UI.
                from .plan2_native_local_save_bootstrap import (
                    DEFAULT_PLAN2_EXAM_SETTING_MASTER_DIR,
                    Plan2NativeExamSettingAuthorityError,
                    load_plan2_native_exam_setting_authority,
                )
                from .plan2_native_local_save_bootstrap import (
                    bootstrap_plan2_native_horizon_from_evidence,
                )

                database_path = Path(database).resolve()
                catalog = _compile_plan2_catalog(database_path)
                setting_master_dir = (
                    DEFAULT_PLAN2_EXAM_SETTING_MASTER_DIR
                    if plan2_exam_setting_master_dir is None
                    else Path(plan2_exam_setting_master_dir)
                )
                try:
                    setting = load_plan2_native_exam_setting_authority(
                        source.state.setting_id,
                        master_dir=setting_master_dir,
                    )
                except (
                    Plan2NativeExamSettingAuthorityError,
                    KeyError,
                    OSError,
                    TypeError,
                    ValueError,
                ) as error:
                    code = getattr(error, "code", "authority-unavailable")
                    detail = getattr(error, "detail", str(error))
                    blockers.append(
                        "local-save-exam-setting-authority-unavailable:"
                        f"{code if not detail else f'{code}:{detail}'}"
                    )
                    setting = None
                gimmick_hooks = _plan2_gimmick_hooks(
                    source,
                    database=database_path,
                )
                if setting is None:
                    audit = None
                else:
                    audit = bootstrap_plan2_native_horizon_from_evidence(
                        source,
                        catalog=catalog,
                        draw_count=setting.draw_count,
                        hand_limit=setting.hand_limit,
                        database=database_path,
                        gimmick_hooks=gimmick_hooks,  # type: ignore[arg-type]
                        observe_external_effects=False,
                    )
                if audit is None:
                    pass
                elif audit.state is None:
                    blockers.extend(
                        value.code if not value.detail else f"{value.code}:{value.detail}"
                        for value in audit.blockers
                    )
                else:
                    raw_actions, native_blockers = _enumerate_plan2_state(
                        audit.state, catalog
                    )
                    blockers.extend(native_blockers)
            else:
                if flow_key[1] == "ProducePlanType_Plan1":
                    blockers.append(
                        "plan1-exact-enumerator-state-unavailable"
                    )
                elif flow_key[1] == "ProducePlanType_Plan3":
                    blockers.append(
                        "plan3-exact-enumerator-state-unavailable"
                    )
                else:
                    blockers.append("native-enumerator-unavailable")
        except (KeyError, OSError, RuntimeError, TypeError, ValueError, IndexError) as error:
            blockers.append(f"native-enumerator-failed:{type(error).__name__}:{error}")

    candidates: list[Mapping[str, object]] = []
    if not blockers or raw_actions:
        for index, action in enumerate(raw_actions):
            try:
                candidates.append(_normalise_candidate(action, index))
            except (TypeError, ValueError, UnifiedLegalActionSnapshotError) as error:
                blockers.append(f"candidate-invalid:{index}:{error}")
    blockers.extend(_validate_target_candidates(candidates, source))

    # A complete exact enumerator must cover all action families offered by
    # the runtime, including the runtime-verified END_TURN surface.  The
    # source's empty inventory is itself enough to explain no drink
    # candidates; a non-empty inventory with no drink candidate is not
    # silently accepted.
    kinds = {value.get("kind") for value in candidates}
    if "END_TURN" not in {value.get("action_id") for value in candidates}:
        blockers.append("end-turn-legality-unproven")
    opaque = _opaque(source)
    if isinstance(opaque, Mapping):
        raw_drinks = opaque.get("drinkList")
        if isinstance(raw_drinks, list):
            drink_candidates = [value for value in candidates if value.get("kind") == KIND_DRINK]
            if raw_drinks and not drink_candidates:
                blockers.append("nonempty-drink-inventory-not-enumerated")
        elif raw_drinks is not None:
            blockers.append("drinkList-invalid")
    if KIND_PLAY not in kinds and KIND_DRINK not in kinds:
        # An END_TURN-only root can be exact (for example a zero-hand turn),
        # but if the source exposes a Hand we need proof that it was empty.
        state = _state_of(source)
        zones = _member(state, "zones")
        hand = _member(zones, "hand", None)
        if isinstance(hand, Sequence) and hand:
            blockers.append("hand-not-enumerated")

    unique_blockers = tuple(dict.fromkeys(value for value in blockers if value))
    complete = settled and bool(candidates) and not unique_blockers
    return UnifiedLegalActionSnapshot(
        flow=flow_key,
        boundary_digest=digest,
        candidates=tuple(candidates) if complete else (),
        authority=authority,
        complete=complete,
        blockers=unique_blockers,
        source=source_label,
    )


def enumerate_unified_legal_actions(source: object, **kwargs: object) -> UnifiedLegalActionSnapshot:
    """Alias with an enumerator-oriented name for native integrations."""

    return build_unified_legal_action_snapshot(source, **kwargs)


def unified_legal_action_snapshot(source: object, **kwargs: object) -> UnifiedLegalActionSnapshot:
    """Short provider alias used by read-only Maa transition recorders."""

    return build_unified_legal_action_snapshot(source, **kwargs)


@dataclass(frozen=True, slots=True)
class UnifiedLegalActionSnapshotProvider:
    """Callable read-only provider bound to one exact native enumerator."""

    native_enumerator: NativeEnumerator | None = None
    flow: FlowKey | str | Sequence[str] | None = None
    authority: str = UNIFIED_LEGAL_ACTION_AUTHORITY
    options: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.native_enumerator is not None and not callable(self.native_enumerator):
            raise TypeError("native_enumerator must be callable or None")
        if not isinstance(self.options, Mapping):
            raise TypeError("provider options must be a mapping")
        object.__setattr__(self, "options", dict(self.options))

    def __call__(self, source: object) -> UnifiedLegalActionSnapshot:
        return build_unified_legal_action_snapshot(
            source,
            flow=self.flow,
            native_enumerator=self.native_enumerator,
            authority=self.authority,
            **dict(self.options),
        )


build_unified_legal_action_provider = UnifiedLegalActionSnapshotProvider
make_unified_legal_action_snapshot_provider = UnifiedLegalActionSnapshotProvider
provide_unified_legal_action_snapshot = unified_legal_action_snapshot


__all__ = [
    "FlowKey",
    "SCHEMA",
    "UNIFIED_CANDIDATE_SET_KIND",
    "UNIFIED_LEGAL_ACTION_AUTHORITY",
    "UNKNOWN_CANDIDATE_SET_KIND",
    "UnifiedLegalActionSnapshot",
    "UnifiedLegalActionSnapshotError",
    "UnifiedLegalActionSnapshotProvider",
    "build_unified_legal_action_provider",
    "build_unified_legal_action_snapshot",
    "enumerate_unified_legal_actions",
    "make_unified_legal_action_snapshot_provider",
    "provide_unified_legal_action_snapshot",
    "unified_legal_action_snapshot",
]
