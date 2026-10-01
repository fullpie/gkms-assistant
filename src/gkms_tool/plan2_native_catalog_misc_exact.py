"""Bounded catalog for ten direct effects with existing exact Plan2 owners.

This leaf preserves each Master card/version/slot and delegates execution to
the four already-audited standalone owners.  It accepts only an immutable
execution-boundary request, returns the owner's typed persistent-state/zone
handoff, and never executes neighboring slots or claims a whole card.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import sqlite3
from typing import Final, Mapping

from .master_db import DEFAULT_DATABASE
from .plan2_add_grow_effect import (
    ADD_GROW_EFFECT_TYPE,
    TARGET_ADD_GROW_EFFECT_ID,
    Plan2AddGrowCapture,
    Plan2AddGrowContractError,
    Plan2AddGrowDeckAllState,
    Plan2AddGrowExecution,
    Plan2AddGrowProgram,
    capture_plan2_add_grow_groups,
    execute_captured_plan2_add_grow_groups,
    load_plan2_add_grow_program,
)
from .plan2_aggressive_value_multiple import (
    EFFECT_AGGRESSIVE_VALUE_MULTIPLE,
    MULTIPLE_EFFECT_ID,
    Plan2AggressiveValueMultipleCardVersion,
    Plan2AggressiveValueMultipleContractError,
    execute_p127_aggressive_value_multiple_handoff,
    load_plan2_aggressive_value_multiple_catalog,
)
from .plan2_lesson_depend_block_consumption_final import (
    DIRECT_EFFECT_ID as LESSON_BLOCK_SUM_EFFECT_ID,
    DIRECT_EFFECT_TYPE as LESSON_BLOCK_SUM_EFFECT_TYPE,
    FinalCardContract,
    FinalCardRuntime,
    Plan2FinalCardError,
    execute_lesson_depend_block_consumption_sum,
    load_plan2_final_card_contract,
)
from .plan2_search_play_card_stamina_consumption_change import (
    EFFECT_ID as SEARCH_STAMINA_EFFECT_ID,
    EFFECT_TYPE as SEARCH_STAMINA_EFFECT_TYPE,
    SearchPlayCardStaminaContract,
    SearchPlayCardStaminaRuntime,
    apply_plan2_search_play_card_stamina_change,
    load_plan2_search_play_card_stamina_affected_versions,
    resolve_plan2_search_play_card_stamina_consumption_change,
)


PLAN2_NATIVE_CATALOG_MISC_EXACT_SCHEMA_VERSION: Final = 1
MISC_EXACT_ADAPTER_ID: Final = "plan2.native.catalog.misc_exact"
EXPECTED_OCCURRENCE_COUNT: Final = 10
EXPECTED_UNIQUE_VERSION_COUNT: Final = 10
EXPECTED_UNIQUE_EFFECT_COUNT: Final = 4
EXPECTED_CARD_ID_COUNT: Final = 4
EXPECTED_COMPANION_SLOT_COUNT: Final = 20


class Plan2MiscExactFamily(str, Enum):
    AGGRESSIVE_VALUE_MULTIPLE = "aggressive-value-multiple"
    SEARCH_PLAY_CARD_STAMINA_CHANGE = "search-play-card-stamina-change"
    ADD_GROW_EFFECT = "add-grow-effect"
    LESSON_DEPEND_BLOCK_CONSUMPTION_SUM = (
        "lesson-depend-block-consumption-sum"
    )


class Plan2MiscExactOrigin(str, Enum):
    NORMAL = "normal"
    FORCED = "forced"
    EXTRA = "extra"


class Plan2MiscExactBoundary(str, Enum):
    DIRECT_EFFECT_EXECUTION_AFTER_COST_POLICY = (
        "direct-effect-execution-after-cost-policy"
    )
    PRE_PAYMENT_CANDIDATE_BUILD = "pre-payment-candidate-build"
    POST_DIRECT_EFFECT_EXECUTION = "post-direct-effect-execution"


ALL_ORIGINS: Final = tuple(Plan2MiscExactOrigin)
DIRECT_BOUNDARY: Final = (
    Plan2MiscExactBoundary.DIRECT_EFFECT_EXECUTION_AFTER_COST_POLICY
)

_EFFECT_FAMILY: Final = {
    MULTIPLE_EFFECT_ID: Plan2MiscExactFamily.AGGRESSIVE_VALUE_MULTIPLE,
    SEARCH_STAMINA_EFFECT_ID: Plan2MiscExactFamily.SEARCH_PLAY_CARD_STAMINA_CHANGE,
    TARGET_ADD_GROW_EFFECT_ID: Plan2MiscExactFamily.ADD_GROW_EFFECT,
    LESSON_BLOCK_SUM_EFFECT_ID: (
        Plan2MiscExactFamily.LESSON_DEPEND_BLOCK_CONSUMPTION_SUM
    ),
}
_EFFECT_TYPE: Final = {
    MULTIPLE_EFFECT_ID: EFFECT_AGGRESSIVE_VALUE_MULTIPLE,
    SEARCH_STAMINA_EFFECT_ID: SEARCH_STAMINA_EFFECT_TYPE,
    TARGET_ADD_GROW_EFFECT_ID: ADD_GROW_EFFECT_TYPE,
    LESSON_BLOCK_SUM_EFFECT_ID: LESSON_BLOCK_SUM_EFFECT_TYPE,
}

EXPECTED_OCCURRENCES: Final = (
    (
        "p_card-00-sup-3_158",
        0,
        0,
        SEARCH_STAMINA_EFFECT_ID,
        Plan2MiscExactFamily.SEARCH_PLAY_CARD_STAMINA_CHANGE,
    ),
    (
        "p_card-00-sup-3_158",
        1,
        0,
        SEARCH_STAMINA_EFFECT_ID,
        Plan2MiscExactFamily.SEARCH_PLAY_CARD_STAMINA_CHANGE,
    ),
    (
        "p_card-00-sup-3_158",
        2,
        1,
        SEARCH_STAMINA_EFFECT_ID,
        Plan2MiscExactFamily.SEARCH_PLAY_CARD_STAMINA_CHANGE,
    ),
    (
        "p_card-00-sup-3_158",
        3,
        1,
        SEARCH_STAMINA_EFFECT_ID,
        Plan2MiscExactFamily.SEARCH_PLAY_CARD_STAMINA_CHANGE,
    ),
    (
        "p_card-02-act-100_010",
        0,
        0,
        LESSON_BLOCK_SUM_EFFECT_ID,
        Plan2MiscExactFamily.LESSON_DEPEND_BLOCK_CONSUMPTION_SUM,
    ),
    (
        "p_card-02-ido-3_127",
        0,
        0,
        MULTIPLE_EFFECT_ID,
        Plan2MiscExactFamily.AGGRESSIVE_VALUE_MULTIPLE,
    ),
    (
        "p_card-02-ido-3_127",
        1,
        0,
        MULTIPLE_EFFECT_ID,
        Plan2MiscExactFamily.AGGRESSIVE_VALUE_MULTIPLE,
    ),
    (
        "p_card-02-ido-3_127",
        2,
        0,
        MULTIPLE_EFFECT_ID,
        Plan2MiscExactFamily.AGGRESSIVE_VALUE_MULTIPLE,
    ),
    (
        "p_card-02-ido-3_127",
        3,
        0,
        MULTIPLE_EFFECT_ID,
        Plan2MiscExactFamily.AGGRESSIVE_VALUE_MULTIPLE,
    ),
    (
        "p_card-02-men-100_008",
        0,
        1,
        TARGET_ADD_GROW_EFFECT_ID,
        Plan2MiscExactFamily.ADD_GROW_EFFECT,
    ),
)

EXPECTED_FAMILY_OCCURRENCE_COUNTS: Final = (
    (Plan2MiscExactFamily.AGGRESSIVE_VALUE_MULTIPLE.value, 4),
    (Plan2MiscExactFamily.SEARCH_PLAY_CARD_STAMINA_CHANGE.value, 4),
    (Plan2MiscExactFamily.ADD_GROW_EFFECT.value, 1),
    (Plan2MiscExactFamily.LESSON_DEPEND_BLOCK_CONSUMPTION_SUM.value, 1),
)


class Plan2NativeCatalogMiscExactContractError(ValueError):
    """Master or runtime input is outside the exact ten-occurrence leaf."""


@dataclass(frozen=True, slots=True)
class Plan2NativeCatalogMiscExactHandoff:
    card_id: str
    upgrade: int
    slot_index: int
    effect_id: str
    effect_type: str
    ordered_effect_ids: tuple[str, ...]
    family: Plan2MiscExactFamily
    owner_contract: object
    allowed_origins: tuple[Plan2MiscExactOrigin, ...] = ALL_ORIGINS
    boundary: Plan2MiscExactBoundary = DIRECT_BOUNDARY
    target_effect_executable: bool = True
    companion_effect_executable_here: bool = False
    whole_card_executable: bool = False
    snapshot_timing: str = "target-slot-execution-time-after-cost-policy"

    @property
    def ref(self) -> tuple[str, int]:
        return self.card_id, self.upgrade

    @property
    def occurrence_key(self) -> tuple[str, int, int]:
        return self.card_id, self.upgrade, self.slot_index

    @property
    def effects_before(self) -> tuple[str, ...]:
        return self.ordered_effect_ids[: self.slot_index]

    @property
    def effects_after(self) -> tuple[str, ...]:
        return self.ordered_effect_ids[self.slot_index + 1 :]

    @property
    def companion_effect_ids(self) -> tuple[str, ...]:
        return (*self.effects_before, *self.effects_after)


@dataclass(frozen=True, slots=True)
class Plan2NativeCatalogMiscExactAccounting:
    occurrence_count: int
    unique_version_count: int
    unique_effect_count: int
    card_id_count: int
    target_executable_occurrence_count: int
    blocked_occurrence_count: int
    companion_slot_count: int
    companion_executed_occurrence_count: int
    whole_card_executable_version_count: int
    family_occurrences: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class Plan2NativeCatalogMiscExactCatalog:
    schema_version: int
    adapter_id: str
    handoffs: tuple[Plan2NativeCatalogMiscExactHandoff, ...]
    accounting: Plan2NativeCatalogMiscExactAccounting

    def occurrence(
        self, card_id: str, upgrade: int, slot_index: int
    ) -> Plan2NativeCatalogMiscExactHandoff:
        for handoff in self.handoffs:
            if handoff.occurrence_key == (card_id, upgrade, slot_index):
                return handoff
        raise KeyError((card_id, upgrade, slot_index))

    def for_version(
        self, card_id: str, upgrade: int
    ) -> tuple[Plan2NativeCatalogMiscExactHandoff, ...]:
        return tuple(row for row in self.handoffs if row.ref == (card_id, upgrade))


def _load_owners(database: Path) -> Mapping[Plan2MiscExactFamily, object]:
    try:
        aggressive = load_plan2_aggressive_value_multiple_catalog(
            database=database
        )
        search_resolution = (
            resolve_plan2_search_play_card_stamina_consumption_change(
                SEARCH_STAMINA_EFFECT_ID, database
            )
        )
        if not search_resolution.executable:
            raise Plan2NativeCatalogMiscExactContractError(
                "search-stamina-owner-unresolved"
            )
        search = search_resolution.require_ready()
        affected = load_plan2_search_play_card_stamina_affected_versions(database)
        if tuple((row.card_id, row.upgrade_count) for row in affected) != (
            ("p_card-00-sup-3_158", 0),
            ("p_card-00-sup-3_158", 1),
            ("p_card-00-sup-3_158", 2),
            ("p_card-00-sup-3_158", 3),
        ) or not all(row.direct for row in affected):
            raise Plan2NativeCatalogMiscExactContractError(
                "search-stamina-affected-versions-changed"
            )
        add_grow = load_plan2_add_grow_program(database=database)
        lesson = load_plan2_final_card_contract(database)
    except (
        Plan2AggressiveValueMultipleContractError,
        Plan2AddGrowContractError,
        Plan2FinalCardError,
        KeyError,
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
    ) as error:
        if isinstance(error, Plan2NativeCatalogMiscExactContractError):
            raise
        raise Plan2NativeCatalogMiscExactContractError(
            "existing-exact-owner-changed"
        ) from error
    return {
        Plan2MiscExactFamily.AGGRESSIVE_VALUE_MULTIPLE: aggressive,
        Plan2MiscExactFamily.SEARCH_PLAY_CARD_STAMINA_CHANGE: search,
        Plan2MiscExactFamily.ADD_GROW_EFFECT: add_grow,
        Plan2MiscExactFamily.LESSON_DEPEND_BLOCK_CONSUMPTION_SUM: lesson,
    }


def _owner_contract(
    family: Plan2MiscExactFamily,
    upgrade: int,
    owners: Mapping[Plan2MiscExactFamily, object],
) -> object:
    owner = owners[family]
    if family is Plan2MiscExactFamily.AGGRESSIVE_VALUE_MULTIPLE:
        return owner.version(upgrade)  # type: ignore[union-attr]
    return owner


def load_plan2_native_catalog_misc_exact(
    database: Path = DEFAULT_DATABASE,
) -> Plan2NativeCatalogMiscExactCatalog:
    """Load the exact ten Master links and bind their existing owners."""

    database = Path(database)
    if not database.is_file():
        raise Plan2NativeCatalogMiscExactContractError("master-database-missing")
    owners = _load_owners(database)
    uri = f"file:{database.resolve().as_posix()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            effect_types = {
                str(row["id"]): str(row["effect_type"])
                for row in connection.execute(
                    "SELECT id, effect_type FROM effect WHERE effect_type IN (?, ?, ?, ?)",
                    tuple(dict.fromkeys(_EFFECT_TYPE.values())),
                )
            }
            cards = connection.execute(
                "SELECT id, upgrade_count, play_effects_json, raw_json FROM card "
                "WHERE plan_type IN (?, ?) ORDER BY id, upgrade_count",
                ("ProducePlanType_Common", "ProducePlanType_Plan2"),
            ).fetchall()
    except sqlite3.Error as error:
        raise Plan2NativeCatalogMiscExactContractError(
            "master-read-failed"
        ) from error

    found: list[
        tuple[str, int, int, str, Plan2MiscExactFamily, tuple[str, ...]]
    ] = []
    for card in cards:
        try:
            links = json.loads(str(card["play_effects_json"]))
            raw = json.loads(str(card["raw_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise Plan2NativeCatalogMiscExactContractError(
                "card-json-invalid"
            ) from error
        if not isinstance(links, list) or not isinstance(raw, dict):
            raise Plan2NativeCatalogMiscExactContractError("card-json-shape")
        if raw.get("playEffects") != links:
            raise Plan2NativeCatalogMiscExactContractError(
                "card-column-raw-link-mismatch"
            )
        ordered_ids: list[str] = []
        for index, link in enumerate(links):
            if not isinstance(link, dict):
                raise Plan2NativeCatalogMiscExactContractError(
                    "card-effect-link-shape"
                )
            effect_id = link.get("produceExamEffectId")
            if type(effect_id) is not str or not effect_id:
                raise Plan2NativeCatalogMiscExactContractError(
                    "card-effect-id-shape"
                )
            ordered_ids.append(effect_id)
            effect_type = effect_types.get(effect_id)
            if effect_type not in _EFFECT_TYPE.values():
                continue
            family = _EFFECT_FAMILY.get(effect_id)
            if family is None or _EFFECT_TYPE[effect_id] != effect_type:
                raise Plan2NativeCatalogMiscExactContractError(
                    "unowned-effect-row-in-target-family"
                )
            if link != {
                "produceExamTriggerId": "",
                "produceExamEffectId": effect_id,
                "hideIcon": False,
                "isOncePlayEffect": False,
            }:
                raise Plan2NativeCatalogMiscExactContractError(
                    "target-link-shape-changed"
                )
            found.append(
                (
                    str(card["id"]),
                    int(card["upgrade_count"]),
                    index,
                    effect_id,
                    family,
                    (),
                )
            )
        ordered = tuple(ordered_ids)
        for position in range(len(found) - 1, -1, -1):
            item = found[position]
            if item[0] != str(card["id"]) or item[1] != int(card["upgrade_count"]):
                break
            if not item[5]:
                found[position] = (*item[:5], ordered)

    actual = tuple(item[:5] for item in found)
    if actual != EXPECTED_OCCURRENCES:
        raise Plan2NativeCatalogMiscExactContractError(
            f"misc-exact-inventory-changed:{actual!r}"
        )

    handoffs = tuple(
        Plan2NativeCatalogMiscExactHandoff(
            card_id,
            upgrade,
            slot_index,
            effect_id,
            _EFFECT_TYPE[effect_id],
            ordered_effect_ids,
            family,
            _owner_contract(family, upgrade, owners),
        )
        for card_id, upgrade, slot_index, effect_id, family, ordered_effect_ids in found
    )
    family_counts = Counter(row.family.value for row in handoffs)
    accounting = Plan2NativeCatalogMiscExactAccounting(
        len(handoffs),
        len({row.ref for row in handoffs}),
        len({row.effect_id for row in handoffs}),
        len({row.card_id for row in handoffs}),
        sum(row.target_effect_executable for row in handoffs),
        sum(not row.target_effect_executable for row in handoffs),
        sum(len(row.companion_effect_ids) for row in handoffs),
        sum(row.companion_effect_executable_here for row in handoffs),
        len({row.ref for row in handoffs if row.whole_card_executable}),
        tuple(
            (family, family_counts[family])
            for family, _ in EXPECTED_FAMILY_OCCURRENCE_COUNTS
        ),
    )
    scalars = (
        accounting.occurrence_count,
        accounting.unique_version_count,
        accounting.unique_effect_count,
        accounting.card_id_count,
        accounting.target_executable_occurrence_count,
        accounting.blocked_occurrence_count,
        accounting.companion_slot_count,
        accounting.companion_executed_occurrence_count,
        accounting.whole_card_executable_version_count,
    )
    if scalars != (10, 10, 4, 4, 10, 0, 20, 0, 0) or (
        accounting.family_occurrences != EXPECTED_FAMILY_OCCURRENCE_COUNTS
    ):
        raise Plan2NativeCatalogMiscExactContractError(
            f"misc-exact-accounting-changed:{scalars!r}"
        )
    return Plan2NativeCatalogMiscExactCatalog(
        PLAN2_NATIVE_CATALOG_MISC_EXACT_SCHEMA_VERSION,
        MISC_EXACT_ADAPTER_ID,
        handoffs,
        accounting,
    )


compile_plan2_native_catalog_misc_exact = load_plan2_native_catalog_misc_exact


@dataclass(frozen=True, slots=True)
class Plan2MiscExactExecutionContext:
    origin: object = Plan2MiscExactOrigin.NORMAL
    boundary: object = DIRECT_BOUNDARY
    source_guid: object = ""
    cost_policy_resolved: object = True
    payment_committed_before_target: object = True
    completed_slot_indexes: object = ()


@dataclass(frozen=True, slots=True)
class Plan2MiscExactAggressiveRequest:
    context: Plan2MiscExactExecutionContext
    current_aggressive: object
    status_add_blocked: object = False


@dataclass(frozen=True, slots=True)
class Plan2MiscExactSearchStaminaRequest:
    context: Plan2MiscExactExecutionContext
    persistent_state: object
    zone_state: object
    playing_card: object
    status_add_blocked: object = False


@dataclass(frozen=True, slots=True)
class Plan2MiscExactAddGrowRequest:
    context: Plan2MiscExactExecutionContext
    search_zone_state: object
    rematch_zone_state: object | None = None


@dataclass(frozen=True, slots=True)
class Plan2MiscExactLessonBlockSumRequest:
    context: Plan2MiscExactExecutionContext
    persistent_state: object


@dataclass(frozen=True, slots=True)
class Plan2MiscExactAddGrowOwnerResult:
    capture: Plan2AddGrowCapture
    execution: Plan2AddGrowExecution


@dataclass(frozen=True, slots=True)
class Plan2MiscExactExecution:
    occurrence_key: tuple[str, int, int] | None
    family: str
    supported: bool
    target_effect_executed: bool
    owner_result: object | None
    persistent_before: object | None
    persistent_after: object | None
    zone_snapshot: object | None
    zone_after: object | None
    precompleted_effect_ids: tuple[str, ...]
    pending_effect_ids: tuple[str, ...]
    companion_effects_executed: bool
    whole_card_executable: bool
    reasons: tuple[str, ...]

    @property
    def fail_closed(self) -> bool:
        return not self.supported or not self.target_effect_executed

    @property
    def persistent_state_unchanged(self) -> bool:
        return self.persistent_before is self.persistent_after


_REQUEST_TYPES: Final = {
    Plan2MiscExactFamily.AGGRESSIVE_VALUE_MULTIPLE: (
        Plan2MiscExactAggressiveRequest
    ),
    Plan2MiscExactFamily.SEARCH_PLAY_CARD_STAMINA_CHANGE: (
        Plan2MiscExactSearchStaminaRequest
    ),
    Plan2MiscExactFamily.ADD_GROW_EFFECT: Plan2MiscExactAddGrowRequest,
    Plan2MiscExactFamily.LESSON_DEPEND_BLOCK_CONSUMPTION_SUM: (
        Plan2MiscExactLessonBlockSumRequest
    ),
}


def _unsupported(
    handoff: Plan2NativeCatalogMiscExactHandoff | None,
    reasons: tuple[str, ...] | list[str],
) -> Plan2MiscExactExecution:
    return Plan2MiscExactExecution(
        None if handoff is None else handoff.occurrence_key,
        "" if handoff is None else handoff.family.value,
        False,
        False,
        None,
        None,
        None,
        None,
        None,
        (),
        (),
        False,
        False,
        tuple(dict.fromkeys(reasons)),
    )


def _validate_context(
    handoff: Plan2NativeCatalogMiscExactHandoff,
    context: object,
) -> tuple[Plan2MiscExactOrigin | None, tuple[str, ...]]:
    reasons: list[str] = []
    if not isinstance(context, Plan2MiscExactExecutionContext):
        return None, ("execution-context-missing-or-unknown",)
    try:
        origin = Plan2MiscExactOrigin(context.origin)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        origin = None
        reasons.append("play-origin-unknown")
    try:
        boundary = Plan2MiscExactBoundary(context.boundary)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        boundary = None
        reasons.append("execution-boundary-unknown")
    if boundary is not handoff.boundary:
        reasons.append("execution-boundary-unproven")
    if origin is not None and origin not in handoff.allowed_origins:
        reasons.append("play-origin-unproven")
    if type(context.source_guid) is not str or not context.source_guid:
        reasons.append("source-guid-missing-or-unknown")
    if context.cost_policy_resolved is not True:
        reasons.append("cost-policy-not-resolved")
    expected_payment = origin is Plan2MiscExactOrigin.NORMAL
    if type(context.payment_committed_before_target) is not bool:
        reasons.append("payment-commit-state-not-boolean")
    elif origin is not None and context.payment_committed_before_target != expected_payment:
        reasons.append("payment-commit-state-origin-mismatch")
    expected_slots = tuple(range(handoff.slot_index))
    if context.completed_slot_indexes != expected_slots:
        reasons.append("completed-slot-order-mismatch")
    return origin, tuple(reasons)


def execute_plan2_native_catalog_misc_exact(
    source: Plan2NativeCatalogMiscExactHandoff | object,
    request: object,
) -> Plan2MiscExactExecution:
    """Execute one target slot through its exact owner and stop at handoff."""

    if not isinstance(source, Plan2NativeCatalogMiscExactHandoff):
        return _unsupported(None, ("unknown-occurrence-failed-closed",))
    handoff = source
    expected_request = _REQUEST_TYPES.get(handoff.family)
    if (
        expected_request is None
        or not handoff.target_effect_executable
        or handoff.companion_effect_executable_here
        or handoff.whole_card_executable
        or handoff.effect_id not in _EFFECT_FAMILY
        or _EFFECT_FAMILY[handoff.effect_id] is not handoff.family
    ):
        return _unsupported(handoff, ("handoff-contract-changed",))
    if not isinstance(request, expected_request):
        return _unsupported(handoff, ("family-request-missing-or-unknown",))
    origin, reasons = _validate_context(handoff, request.context)
    if reasons or origin is None:
        return _unsupported(handoff, reasons)
    context = request.context

    try:
        owner_result: object
        persistent_before: object
        persistent_after: object
        zone_snapshot: object | None = None
        zone_after: object | None = None
        if handoff.family is Plan2MiscExactFamily.AGGRESSIVE_VALUE_MULTIPLE:
            if not isinstance(
                handoff.owner_contract, Plan2AggressiveValueMultipleCardVersion
            ):
                return _unsupported(handoff, ("aggressive-owner-contract-changed",))
            aggressive_request = request
            owner_result = execute_p127_aggressive_value_multiple_handoff(
                handoff.owner_contract,
                current_aggressive=aggressive_request.current_aggressive,
                play_origin=origin.value,
                source_guid=context.source_guid,
                status_add_blocked=aggressive_request.status_add_blocked,
            )
            persistent_before = aggressive_request.current_aggressive
            persistent_after = owner_result.transition.after
        elif handoff.family is Plan2MiscExactFamily.SEARCH_PLAY_CARD_STAMINA_CHANGE:
            if not isinstance(handoff.owner_contract, SearchPlayCardStaminaContract):
                return _unsupported(handoff, ("search-owner-contract-changed",))
            search_request = request
            if not isinstance(
                search_request.persistent_state, SearchPlayCardStaminaRuntime
            ):
                return _unsupported(handoff, ("search-persistent-state-unknown",))
            if (
                getattr(search_request.playing_card, "guid", None)
                != context.source_guid
                or getattr(search_request.playing_card, "card_id", None)
                != handoff.card_id
                or getattr(search_request.playing_card, "effective_upgrade", None)
                != handoff.upgrade
            ):
                return _unsupported(handoff, ("search-playing-card-identity-drift",))
            owner_result = apply_plan2_search_play_card_stamina_change(
                search_request.persistent_state,
                handoff.owner_contract,
                search_request.zone_state,
                playing_card=search_request.playing_card,
                status_add_blocked=search_request.status_add_blocked,
            )
            persistent_before = owner_result.runtime_before
            persistent_after = owner_result.runtime_after
            zone_snapshot = search_request.zone_state
            zone_after = search_request.zone_state
        elif handoff.family is Plan2MiscExactFamily.ADD_GROW_EFFECT:
            if not isinstance(handoff.owner_contract, Plan2AddGrowProgram):
                return _unsupported(handoff, ("add-grow-owner-contract-changed",))
            add_request = request
            if not isinstance(add_request.search_zone_state, Plan2AddGrowDeckAllState):
                return _unsupported(handoff, ("add-grow-search-state-unknown",))
            playing = add_request.search_zone_state.playing
            if (
                playing is None
                or playing.guid != context.source_guid
                or playing.card_id != handoff.card_id
                or playing.play_effects != handoff.owner_contract.play_effects
            ):
                return _unsupported(handoff, ("add-grow-playing-card-identity-drift",))
            rematch = (
                add_request.search_zone_state
                if add_request.rematch_zone_state is None
                else add_request.rematch_zone_state
            )
            if not isinstance(rematch, Plan2AddGrowDeckAllState):
                return _unsupported(handoff, ("add-grow-rematch-state-unknown",))
            capture = capture_plan2_add_grow_groups(
                add_request.search_zone_state, handoff.owner_contract
            )
            execution = execute_captured_plan2_add_grow_groups(rematch, capture)
            owner_result = Plan2MiscExactAddGrowOwnerResult(capture, execution)
            persistent_before = execution.before
            persistent_after = execution.after
            zone_snapshot = capture.state_at_search
            zone_after = execution.after
        else:
            if not isinstance(handoff.owner_contract, FinalCardContract):
                return _unsupported(handoff, ("lesson-owner-contract-changed",))
            lesson_request = request
            if not isinstance(lesson_request.persistent_state, FinalCardRuntime):
                return _unsupported(handoff, ("lesson-persistent-state-unknown",))
            owner_result = execute_lesson_depend_block_consumption_sum(
                lesson_request.persistent_state,
                handoff.owner_contract.direct_effect,
            )
            persistent_before = owner_result.before
            persistent_after = owner_result.after
            zone_snapshot = owner_result.before.native_state
            zone_after = owner_result.after.native_state
    except (TypeError, ValueError, ArithmeticError) as error:
        return _unsupported(
            handoff,
            (f"exact-owner-rejected-input:{type(error).__name__}",),
        )

    return Plan2MiscExactExecution(
        handoff.occurrence_key,
        handoff.family.value,
        True,
        True,
        owner_result,
        persistent_before,
        persistent_after,
        zone_snapshot,
        zone_after,
        handoff.effects_before,
        handoff.effects_after,
        False,
        False,
        (),
    )


execute_plan2_misc_exact = execute_plan2_native_catalog_misc_exact


__all__ = [
    "ALL_ORIGINS",
    "DIRECT_BOUNDARY",
    "EXPECTED_CARD_ID_COUNT",
    "EXPECTED_COMPANION_SLOT_COUNT",
    "EXPECTED_FAMILY_OCCURRENCE_COUNTS",
    "EXPECTED_OCCURRENCE_COUNT",
    "EXPECTED_OCCURRENCES",
    "EXPECTED_UNIQUE_EFFECT_COUNT",
    "EXPECTED_UNIQUE_VERSION_COUNT",
    "MISC_EXACT_ADAPTER_ID",
    "PLAN2_NATIVE_CATALOG_MISC_EXACT_SCHEMA_VERSION",
    "Plan2MiscExactAddGrowOwnerResult",
    "Plan2MiscExactAddGrowRequest",
    "Plan2MiscExactAggressiveRequest",
    "Plan2MiscExactBoundary",
    "Plan2MiscExactExecution",
    "Plan2MiscExactExecutionContext",
    "Plan2MiscExactFamily",
    "Plan2MiscExactLessonBlockSumRequest",
    "Plan2MiscExactOrigin",
    "Plan2MiscExactSearchStaminaRequest",
    "Plan2NativeCatalogMiscExactAccounting",
    "Plan2NativeCatalogMiscExactCatalog",
    "Plan2NativeCatalogMiscExactContractError",
    "Plan2NativeCatalogMiscExactHandoff",
    "compile_plan2_native_catalog_misc_exact",
    "execute_plan2_misc_exact",
    "execute_plan2_native_catalog_misc_exact",
    "load_plan2_native_catalog_misc_exact",
]
