"""Ownership audit for the 12 triggered Plan2 StatusEnchant occurrences.

These occurrences are deliberately outside the generic StatusEnchant catalog:
their card ``playEffects`` link has its own predicate.  Eight already compose
through exact StartPlay/EndTurn owners.  The four ``p_card-02-men-3_040``
versions additionally need the small immutable lifecycle below because their
StartTurn child is ReviewValueMultiple, a child type outside the generic
status-child dispatcher.

The module is standalone and read-only with respect to the program catalog,
coverage, horizon, and core runtime.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import sqlite3
from typing import Final, Literal, TypeAlias

from .master_db import DEFAULT_DATABASE
from .plan2_end_turn_trigger import load_plan2_end_turn_program
from .plan2_native_catalog_effect_triggers import (
    EffectTriggerBoundary,
    Plan2NativeEffectTriggerSnapshot,
    evaluate_plan2_native_effect_trigger,
    load_plan2_native_catalog_effect_triggers,
)
from .plan2_native_catalog_effect_triggers_remaining import (
    PHASE_NONE,
    Plan2RemainingEffectTriggerSnapshot,
    RemainingEffectTriggerBoundary,
    evaluate_plan2_native_catalog_effect_trigger_remaining,
    load_plan2_native_catalog_effect_triggers_remaining,
)
from .plan2_native_catalog_review_dynamic import (
    OP_REVIEW_VALUE_MULTIPLE,
    Plan2NativeReviewDynamicEventInput,
    Plan2NativeReviewDynamicProgram,
    Plan2NativeReviewDynamicRuntime,
    compile_plan2_native_catalog_review_dynamic,
    execute_plan2_native_review_dynamic_event,
)
from .plan2_native_catalog_status_children import (
    CARD_DRAW,
    compile_plan2_native_catalog_status_children,
)
from .plan2_native_catalog_status_enchant import (
    compile_plan2_native_catalog_status_enchant,
)
from .plan2_native_catalog_status_enchant_encore import (
    compile_plan2_native_catalog_status_enchant_encore,
)
from .plan2_start_play_trigger import load_plan2_start_play_trigger_catalog


SCHEMA_VERSION: Final = 1
STATUS_ENCHANT: Final = "ProduceExamEffectType_ExamStatusEnchant"
REVIEW_VALUE_MULTIPLE: Final = (
    "ProduceExamEffectType_ExamReviewValueMultiple"
)
REVIEW_CHILD_ID: Final = "e_effect-exam_review_value_multiple-0100"
PHASE_START_TURN: Final = "ProduceExamPhaseType_ExamStartTurn"
PHASE_START_PLAY: Final = "ProduceExamPhaseType_StartPlay"
PHASE_END_TURN: Final = "ProduceExamPhaseType_ExamEndTurn"

CARD_REVIEW_MULTIPLE: Final = "p_card-02-men-3_040"
CARD_START_PLAY_DRAW: Final = "p_card-02-sup-3_156"
CARD_END_TURN_REVIEW: Final = "p_card-02-sup-3_180"
TARGET_CARD_IDS: Final = (
    CARD_REVIEW_MULTIPLE,
    CARD_START_PLAY_DRAW,
    CARD_END_TURN_REVIEW,
)
EXPECTED_TARGET_COUNT: Final = 12
EXPECTED_DIRECT_COUNT: Final = 12
EXPECTED_CO_BLOCKED_COUNT: Final = 0
EXPECTED_CARD_ID_COUNT: Final = 3
EXPECTED_OWNER_COUNTS: Final = {
    "local-start-turn-review-value-multiple": 4,
    "start-play-card-draw": 4,
    "end-turn-review": 4,
}

OwnerKind: TypeAlias = Literal[
    "local-start-turn-review-value-multiple",
    "start-play-card-draw",
    "end-turn-review",
]


class Plan2TriggeredStatusEnchantError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


def _json_array(value: object, label: str) -> tuple[object, ...]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise Plan2TriggeredStatusEnchantError("invalid-json", label) from error
    if not isinstance(parsed, list):
        raise Plan2TriggeredStatusEnchantError("invalid-json-array", label)
    return tuple(parsed)


@dataclass(frozen=True, slots=True)
class Plan2TriggeredStatusEnchantProgram:
    card_id: str
    upgrade: int
    slot_index: int
    ordered_effect_ids: tuple[str, ...]
    outer_trigger_id: str
    installer_effect_id: str
    status_enchant_id: str
    listener_trigger_id: str
    child_effect_id: str
    child_effect_type: str
    listener_turn: int
    owner_kind: OwnerKind
    predicate_owner: str
    installer_owner: str
    child_owner: str
    review_program: Plan2NativeReviewDynamicProgram | None = None

    def __post_init__(self) -> None:
        if self.slot_index >= len(self.ordered_effect_ids) or self.ordered_effect_ids[
            self.slot_index
        ] != self.installer_effect_id:
            raise Plan2TriggeredStatusEnchantError(
                "card-slot-order-drift", self.installer_effect_id
            )
        if not self.outer_trigger_id:
            raise Plan2TriggeredStatusEnchantError("outer-trigger-missing")
        if self.listener_turn <= 0:
            raise Plan2TriggeredStatusEnchantError("listener-turn-shape")
        if self.owner_kind == "local-start-turn-review-value-multiple":
            if (
                self.listener_trigger_id != "e_trigger-exam_start_turn"
                or self.child_effect_id != REVIEW_CHILD_ID
                or self.child_effect_type != REVIEW_VALUE_MULTIPLE
                or self.review_program is None
            ):
                raise Plan2TriggeredStatusEnchantError("local-owner-shape-drift")

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def prior_effect_ids(self) -> tuple[str, ...]:
        return self.ordered_effect_ids[: self.slot_index]


@dataclass(frozen=True, slots=True)
class Plan2TriggeredStatusEnchantOwnershipAudit:
    schema_version: int
    database: str
    programs: tuple[Plan2TriggeredStatusEnchantProgram, ...]
    direct_refs: tuple[tuple[str, int], ...]
    co_blocked_refs: tuple[tuple[str, int], ...]
    generic_status_overlap_refs: tuple[tuple[str, int], ...]
    encore_overlap_refs: tuple[tuple[str, int], ...]
    status_children_catalog_overlap_refs: tuple[tuple[str, int], ...]
    blockers: tuple[str, ...] = ()

    @property
    def target_refs(self) -> tuple[tuple[str, int], ...]:
        return tuple(item.ref for item in self.programs)

    @property
    def target_count(self) -> int:
        return len(self.programs)

    @property
    def direct_count(self) -> int:
        return len(self.direct_refs)

    @property
    def co_blocked_count(self) -> int:
        return len(self.co_blocked_refs)

    @property
    def owner_counts(self) -> Mapping[str, int]:
        return dict(Counter(item.owner_kind for item in self.programs))

    @property
    def handoff_ready(self) -> bool:
        return (
            not self.blockers
            and self.target_count == EXPECTED_TARGET_COUNT
            and self.direct_count == EXPECTED_DIRECT_COUNT
            and self.co_blocked_count == EXPECTED_CO_BLOCKED_COUNT
        )

    def program(
        self, card_id: str, upgrade: int
    ) -> Plan2TriggeredStatusEnchantProgram:
        return next(item for item in self.programs if item.ref == (card_id, upgrade))


def _effect_rows(connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {
        str(row["id"]): row
        for row in connection.execute("SELECT * FROM effect").fetchall()
    }


def _review_evaluator_program(
    database: Path,
    *,
    card_id: str,
    upgrade: int,
) -> Plan2NativeReviewDynamicProgram:
    exact_catalog = compile_plan2_native_catalog_review_dynamic(database=database)
    exact = next(
        item
        for item in exact_catalog.catalog.programs
        if item.operation_kind == OP_REVIEW_VALUE_MULTIPLE
        and item.effect_id == REVIEW_CHILD_ID
    )
    return replace(
        exact,
        card_id=card_id,
        upgrade=upgrade,
        slot_index=0,
        ordered_effect_ids=(REVIEW_CHILD_ID,),
        companion_blockers=(),
    )


def compile_plan2_native_catalog_status_enchant_triggered(
    database: Path = DEFAULT_DATABASE,
) -> Plan2TriggeredStatusEnchantOwnershipAudit:
    """Certify the exact 12 central StatusEnchant blocker occurrences."""

    database = Path(database).resolve()
    remaining = load_plan2_native_catalog_effect_triggers_remaining(database)
    base_triggers = load_plan2_native_catalog_effect_triggers(database)
    start_play = load_plan2_start_play_trigger_catalog(database)
    generic = compile_plan2_native_catalog_status_enchant(database=database)
    encore = compile_plan2_native_catalog_status_enchant_encore(database)
    children = compile_plan2_native_catalog_status_children(database)

    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        effects = _effect_rows(connection)
        statuses = {
            str(row["id"]): row
            for row in connection.execute(
                "SELECT * FROM produce_exam_status_enchant"
            ).fetchall()
        }
        cards = connection.execute(
            "SELECT id, upgrade_count, play_effects_json FROM card "
            "WHERE id IN (?,?,?) ORDER BY id, upgrade_count",
            TARGET_CARD_IDS,
        ).fetchall()

    programs: list[Plan2TriggeredStatusEnchantProgram] = []
    blockers: list[str] = []
    for card in cards:
        card_id = str(card["id"])
        upgrade = int(card["upgrade_count"])
        links = _json_array(card["play_effects_json"], f"{card_id}#{upgrade}")
        ordered = tuple(str(item["produceExamEffectId"]) for item in links)  # type: ignore[index]
        matches = tuple(
            (index, item)
            for index, item in enumerate(links)
            if isinstance(item, Mapping)
            and str(item.get("produceExamTriggerId", ""))
            and str(item.get("produceExamEffectId", "")) in effects
            and str(effects[str(item["produceExamEffectId"])]["effect_type"])
            == STATUS_ENCHANT
        )
        if len(matches) != 1:
            blockers.append(f"{card_id}#{upgrade}:triggered-installer-cardinality")
            continue
        slot, link = matches[0]
        installer_id = str(link["produceExamEffectId"])
        outer_trigger = str(link["produceExamTriggerId"])
        installer = effects[installer_id]
        status_id = str(installer["status_enchant_id"])
        status = statuses.get(status_id)
        if status is None:
            blockers.append(f"{card_id}#{upgrade}:status-row-missing")
            continue
        child_ids = tuple(
            str(item)
            for item in _json_array(
                status["produce_exam_effect_ids_json"], status_id
            )
        )
        if len(child_ids) != 1 or child_ids[0] not in effects:
            blockers.append(f"{card_id}#{upgrade}:child-shape")
            continue
        child = effects[child_ids[0]]
        listener_trigger = str(status["produce_exam_trigger_id"])
        review_program = None
        try:
            if card_id == CARD_REVIEW_MULTIPLE:
                predicate = remaining.occurrence(card_id, upgrade, slot)
                if not predicate.target_predicate_executable:
                    raise Plan2TriggeredStatusEnchantError("predicate-owner-blocked")
                review_program = _review_evaluator_program(
                    database, card_id=card_id, upgrade=upgrade
                )
                owner: OwnerKind = "local-start-turn-review-value-multiple"
                predicate_owner = "plan2.native.catalog.effect_triggers.remaining"
                installer_owner = "plan2.native.status_enchant.triggered.review"
                child_owner = "plan2.native.catalog.review_dynamic"
            elif card_id == CARD_START_PLAY_DRAW:
                predicate = remaining.occurrence(card_id, upgrade, slot)
                start = next(
                    item
                    for item in start_play.card_versions
                    if (item.card_id, item.upgrade_count) == (card_id, upgrade)
                )
                start_program = start_play.program(status_id)
                if (
                    not predicate.target_predicate_executable
                    or not start.direct_from_start_play_fix
                    or start_program.child_effect_ids != child_ids
                    or str(child["effect_type"]) != CARD_DRAW
                ):
                    raise Plan2TriggeredStatusEnchantError("start-play-owner-drift")
                owner = "start-play-card-draw"
                predicate_owner = "plan2.native.catalog.effect_triggers.remaining"
                installer_owner = "plan2.start_play_trigger"
                child_owner = "plan2.native.catalog.status_children:card-draw"
            else:
                predicate = base_triggers.occurrence(card_id, upgrade, slot)
                end_program = load_plan2_end_turn_program(installer_id, database)
                if (
                    not predicate.target_predicate_executable
                    or end_program.status_enchant_id != status_id
                    or tuple(item.effect_id for item in end_program.effects) != child_ids
                ):
                    raise Plan2TriggeredStatusEnchantError("end-turn-owner-drift")
                owner = "end-turn-review"
                predicate_owner = "plan2.native.catalog.effect_triggers"
                installer_owner = "plan2.end_turn_trigger"
                child_owner = "plan2.end_turn_trigger:review"
            programs.append(
                Plan2TriggeredStatusEnchantProgram(
                    card_id,
                    upgrade,
                    slot,
                    ordered,
                    outer_trigger,
                    installer_id,
                    status_id,
                    listener_trigger,
                    child_ids[0],
                    str(child["effect_type"]),
                    int(installer["effect_turn"]),
                    owner,
                    predicate_owner,
                    installer_owner,
                    child_owner,
                    review_program,
                )
            )
        except (KeyError, StopIteration, TypeError, ValueError) as error:
            blockers.append(f"{card_id}#{upgrade}:{error}")

    programs.sort(key=lambda item: item.ref)
    refs = tuple(item.ref for item in programs)
    generic_overlap = tuple(sorted(set(refs) & set(generic.compiled_refs)))
    encore_refs = set(encore.compiled_refs)
    encore_overlap = tuple(sorted(set(refs) & encore_refs))
    child_overlap = tuple(sorted(set(refs) & set(children.affected_refs)))
    owner_counts = dict(Counter(item.owner_kind for item in programs))
    if (
        len(programs) != EXPECTED_TARGET_COUNT
        or len({item.card_id for item in programs}) != EXPECTED_CARD_ID_COUNT
        or owner_counts != EXPECTED_OWNER_COUNTS
        or generic_overlap
        or encore_overlap
        or child_overlap
    ):
        blockers.append(
            "ownership-inventory-drift:"
            f"targets={len(programs)},owners={owner_counts},"
            f"overlap={(generic_overlap, encore_overlap, child_overlap)!r}"
        )
    return Plan2TriggeredStatusEnchantOwnershipAudit(
        SCHEMA_VERSION,
        str(database),
        tuple(programs),
        refs,
        (),
        generic_overlap,
        encore_overlap,
        child_overlap,
        tuple(blockers),
    )


load_plan2_native_catalog_status_enchant_triggered = (
    compile_plan2_native_catalog_status_enchant_triggered
)


@dataclass(frozen=True, slots=True)
class Plan2TriggeredReviewStatusListener:
    status_uid: int
    source_guid: str
    play_origin: str
    program: Plan2TriggeredStatusEnchantProgram
    turns_remaining: int
    passing_turn_start: bool = False


@dataclass(frozen=True, slots=True)
class Plan2TriggeredReviewStatusRuntime:
    review_runtime: Plan2NativeReviewDynamicRuntime = field(
        default_factory=Plan2NativeReviewDynamicRuntime
    )
    listeners: tuple[Plan2TriggeredReviewStatusListener, ...] = ()
    next_status_uid: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.review_runtime, Plan2NativeReviewDynamicRuntime):
            raise TypeError("review_runtime has the wrong type")
        if self.next_status_uid <= 0:
            raise Plan2TriggeredStatusEnchantError("next-status-uid-shape")
        uids = tuple(item.status_uid for item in self.listeners)
        if len(uids) != len(set(uids)) or any(uid >= self.next_status_uid for uid in uids):
            raise Plan2TriggeredStatusEnchantError("listener-uid-shape")


@dataclass(frozen=True, slots=True)
class Plan2TriggeredReviewStatusInstallInput:
    source_guid: str
    review: int | None
    completed_effect_ids: tuple[str, ...]
    play_origin: str = "normal"
    accepted: bool | None = True
    context_present: bool | None = True
    status_addition_blocked: bool | None = False

    def __post_init__(self) -> None:
        if not isinstance(self.source_guid, str) or not self.source_guid:
            raise Plan2TriggeredStatusEnchantError("source-guid-shape")
        values = tuple(self.completed_effect_ids)
        if any(not isinstance(item, str) or not item for item in values):
            raise Plan2TriggeredStatusEnchantError("completed-effect-order-shape")
        object.__setattr__(self, "completed_effect_ids", values)


@dataclass(frozen=True, slots=True)
class Plan2TriggeredReviewStatusInstallResult:
    before: Plan2TriggeredReviewStatusRuntime
    after: Plan2TriggeredReviewStatusRuntime
    program: Plan2TriggeredStatusEnchantProgram
    installed: bool
    created_status_uid: int | None = None
    unresolved: tuple[str, ...] = ()
    predicate: object | None = None

    @property
    def executable(self) -> bool:
        return not self.unresolved


def install_plan2_triggered_review_status(
    runtime: Plan2TriggeredReviewStatusRuntime,
    program: Plan2TriggeredStatusEnchantProgram,
    execution_input: Plan2TriggeredReviewStatusInstallInput,
    *,
    database: Path = DEFAULT_DATABASE,
) -> Plan2TriggeredReviewStatusInstallResult:
    """Evaluate ReviewUp15, then install one fresh finite listener."""

    if program.owner_kind != "local-start-turn-review-value-multiple":
        return Plan2TriggeredReviewStatusInstallResult(
            runtime, runtime, program, False, unresolved=("owner-kind-unbound",)
        )
    unknown = tuple(
        name
        for name in ("accepted", "context_present", "status_addition_blocked")
        if getattr(execution_input, name) is None
    )
    if unknown or execution_input.review is None:
        reasons = unknown + (("review",) if execution_input.review is None else ())
        return Plan2TriggeredReviewStatusInstallResult(
            runtime,
            runtime,
            program,
            False,
            unresolved=reasons,
        )
    if (
        execution_input.completed_effect_ids != program.prior_effect_ids
        or execution_input.review != runtime.review_runtime.review
        or execution_input.play_origin not in {"normal", "forced", "extra"}
    ):
        return Plan2TriggeredReviewStatusInstallResult(
            runtime, runtime, program, False, unresolved=("install-snapshot-drift",)
        )
    if not execution_input.accepted or not execution_input.context_present:
        return Plan2TriggeredReviewStatusInstallResult(
            runtime, runtime, program, False, unresolved=("card-not-executable",)
        )
    handoff = load_plan2_native_catalog_effect_triggers_remaining(database).occurrence(
        program.card_id, program.upgrade, program.slot_index
    )
    predicate = evaluate_plan2_native_catalog_effect_trigger_remaining(
        handoff,
        Plan2RemainingEffectTriggerSnapshot(
            PHASE_NONE,
            RemainingEffectTriggerBoundary.PRE_PAYMENT_DIRECT_EFFECT_BUILD,
            origin=execution_input.play_origin,
            review=execution_input.review,
        ),
    )
    if predicate.fail_closed:
        return Plan2TriggeredReviewStatusInstallResult(
            runtime,
            runtime,
            program,
            False,
            unresolved=predicate.reasons,
            predicate=predicate,
        )
    if not predicate.fires or execution_input.status_addition_blocked:
        return Plan2TriggeredReviewStatusInstallResult(
            runtime, runtime, program, False, predicate=predicate
        )
    uid = runtime.next_status_uid
    listener = Plan2TriggeredReviewStatusListener(
        uid,
        execution_input.source_guid,
        execution_input.play_origin,
        program,
        program.listener_turn,
    )
    after = replace(
        runtime,
        listeners=(*runtime.listeners, listener),
        next_status_uid=uid + 1,
    )
    return Plan2TriggeredReviewStatusInstallResult(
        runtime, after, program, True, uid, predicate=predicate
    )


@dataclass(frozen=True, slots=True)
class Plan2TriggeredReviewStatusActivation:
    status_uid: int
    child_result: object


@dataclass(frozen=True, slots=True)
class Plan2TriggeredReviewStatusTurnResult:
    before: Plan2TriggeredReviewStatusRuntime
    after: Plan2TriggeredReviewStatusRuntime
    activations: tuple[Plan2TriggeredReviewStatusActivation, ...] = ()
    expired_status_uids: tuple[int, ...] = ()
    unresolved: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return not self.unresolved


def start_plan2_triggered_review_status_turn(
    runtime: Plan2TriggeredReviewStatusRuntime,
    *,
    accepted: bool | None = True,
    context_present: bool | None = True,
    status_addition_blocked: bool | None = False,
    difference_list_available: bool | None = True,
) -> Plan2TriggeredReviewStatusTurnResult:
    """Spend finite turns, queue, and execute ReviewValueMultiple atomically."""

    if None in (
        accepted,
        context_present,
        status_addition_blocked,
        difference_list_available,
    ):
        return Plan2TriggeredReviewStatusTurnResult(
            runtime, runtime, unresolved=("start-turn-input-unknown",)
        )
    if not accepted or not context_present:
        return Plan2TriggeredReviewStatusTurnResult(
            runtime, runtime, unresolved=("start-turn-not-executable",)
        )
    review = runtime.review_runtime
    survivors: list[Plan2TriggeredReviewStatusListener] = []
    activations: list[Plan2TriggeredReviewStatusActivation] = []
    expired: list[int] = []
    try:
        for snapshot in runtime.listeners:
            current = snapshot
            if current.passing_turn_start:
                current = replace(current, turns_remaining=current.turns_remaining - 1)
            if current.turns_remaining <= 0:
                expired.append(current.status_uid)
                continue
            current = replace(current, passing_turn_start=True)
            assert current.program.review_program is not None
            child = execute_plan2_native_review_dynamic_event(
                review,
                current.program.review_program,
                Plan2NativeReviewDynamicEventInput(
                    current.source_guid,
                    current.play_origin,
                    (),
                    True,
                    True,
                    status_addition_blocked,
                    difference_list_available,
                ),
            )
            if not child.executable:
                raise Plan2TriggeredStatusEnchantError(
                    "review-child-unresolved", ",".join(child.unresolved)
                )
            review = child.after
            survivors.append(current)
            activations.append(
                Plan2TriggeredReviewStatusActivation(current.status_uid, child)
            )
    except (TypeError, ValueError) as error:
        return Plan2TriggeredReviewStatusTurnResult(
            runtime,
            runtime,
            unresolved=(f"child-execution-failed:{error}",),
        )
    return Plan2TriggeredReviewStatusTurnResult(
        runtime,
        replace(runtime, review_runtime=review, listeners=tuple(survivors)),
        tuple(activations),
        tuple(expired),
    )
