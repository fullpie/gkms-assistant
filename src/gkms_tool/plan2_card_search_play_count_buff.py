"""Bounded Plan2 ``ExamCardSearchEffectPlayCountBuff`` standalone leaf.

This module owns the exact PlayCountBuff row used by
``p_card-02-ido-3_192`` (``あさり推し♡``), upgrades 0..3.  It deliberately
stops at the native card-command hook and at the handoff boundary for the
same card's ``ExamForcePlayCardSearch`` effect; it does not register a core
executor or modify central coverage.

The status/runtime implementation is reused from the already audited
Plan3/Common leaf.  That implementation is plan-neutral: the status stores
the search predicate, the value1-derived additional-pass count, the
effectCount spend budget, and the observed native UID.  This module supplies
the Plan2 Master graph, Plan2 scalar boundary, and the ForcePlay queue
contract.  GUID zones and execution-time relocation are reused from the
shared ``Plan3UsePool`` primitive.

Native facts represented here:

* ``p_card_search-n-r-sr-ssr-playing`` is a one-card transient Playing
  predicate.  It has no collection order, no Select/Random/All collection
  choice, and consumes no RNG.
* ``value1=1`` is N additional normal direct-effect passes, so the target
  command runs two passes when one matching status is consumed.  It is not a
  playable-count or extra-play allowance.
* ``effectCount=1`` is the status' total accepted-use budget and ``turn=-1``
  is permanent.  Duplicate layers append; native Use selects the newest
  matching layer, records its UID, spends one count, and removes it at zero.
* ``UsePlayCountBuff`` runs once after the live GUID is staged as Playing and
  before direct-effect iteration.  Card/global play counts, history,
  listener callbacks, and the final move remain one core-owned settlement
  after all N+1 passes.

No proxy, agent, process, clock, hash, security, or game/UI call is made.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Any

from .card_search import ProduceCardSearchRule, load_produce_card_search
from .master_db import DEFAULT_DATABASE
from .plan2_force_play_search import (
    ForcePlayEffectRow as Plan2ForcePlayEffectRow,
    Plan2ForcePlayPlanResult,
    Plan2ForcePlayQueuedCommand,
    load_force_play_effect_row,
    probe_accounting as probe_force_play_accounting,
)
from .plan2_state import Plan2State
from .plan3_card_search_effect_play_count_buff import (
    AFFECTED_CARD_VERSION_COUNT as _COMMON_AFFECTED_CARD_VERSION_COUNT,
    CardPlayCountBuffExecution as _CommonCardPlayCountBuffExecution,
    CardPlayCountBuffHookInput as _CommonCardPlayCountBuffHookInput,
    CardPlayEffectSlot,
    CardSearchBuffCardMaster,
    CardSearchBuffMatch,
    CardSearchEffectPlayCountBuffAdapter,
    CardSearchEffectPlayCountBuffContract,
    CardSearchEffectPlayCountBuffEffectRow,
    CardSearchEffectPlayCountBuffError,
    CardSearchEffectPlayCountBuffInputError,
    CardSearchEffectPlayCountBuffResolutionError,
    CardSearchEffectPlayCountBuffUnresolvedInput,
    EFFECT_BY_ID as _COMMON_EFFECT_BY_ID,
    EXACT_EFFECT_ROW as _COMMON_EXACT_EFFECT_ROW,
    MasterCardShape,
    PlayCountBuffRuntime,
    PlayCountBuffStatus,
    PlayCountBuffStatusInstall,
    PlayCountBuffTurnStartTransition,
    PlayCountBuffUse,
    advance_play_count_buff_turn_start,
    contract_for_card_search_effect_play_count_buff as _common_contract_for_effect,
    direct_effect_passes as _common_direct_effect_passes,
    get_play_count_buff as _common_get_play_count_buff,
    install_card_search_effect_play_count_buff as _common_install_buff,
    load_affected_card_versions as _common_load_affected_card_versions,
    load_card_search_buff_card_master as _common_load_card_master,
    load_card_search_effect_play_count_buff_contract as _common_load_contract,
    load_master_card_search_effect_play_count_buff_rows as _common_load_rows,
    match_playing_card_search as _common_match_playing_card_search,
    resolve_card_search_effect_play_count_buff_contract as _common_resolve_contract,
    try_resolve_card_search_effect_play_count_buff_contract as _common_try_resolve,
    use_play_count_buff as _common_use_play_count_buff,
    use_matching_play_count_buff,
)
from .plan3_engine import Plan3State
from .plan3_native_state import (
    Plan3NativeCard,
    Plan3NativeState,
    Plan3NativeStateError,
)
from .plan3_use_pool import (
    Plan3UsePoolCommand,
    Plan3UsePoolStage,
    stage_plan3_use_pool_guid,
)


EFFECT_TYPE = "ProduceExamEffectType_ExamCardSearchEffectPlayCountBuff"
EFFECT_TYPE_VALUE = 38
EFFECT_ID = (
    "e_effect-exam_card_search_effect_play_count_buff-0001-01-inf-"
    "p_card_search-n-r-sr-ssr-playing-all-0_0"
)
SEARCH_ID = "p_card_search-n-r-sr-ssr-playing"
EFFECT_GROUP_ID = (
    "effect_group-visible-exam_card_search_effect_play_count_buff-000"
)

CARD_ID = "p_card-02-ido-3_192"
CARD_NAME = "あさり推し♡"
CARD_UPGRADES = (0, 1, 2, 3)
AFFECTED_CARD_VERSIONS = tuple((CARD_ID, upgrade) for upgrade in CARD_UPGRADES)
AFFECTED_CARD_VERSION_COUNT = len(AFFECTED_CARD_VERSIONS)

FORCE_PLAY_EFFECT_ID = (
    "e_effect-exam_force_play_card_search-p_card_search-deck_grave-select-1_1"
)
FORCE_PLAY_EFFECT_TYPE = "ProduceExamEffectType_ExamForcePlayCardSearch"
FORCE_PLAY_SEARCH_ID = "p_card_search-deck_grave"
FORCE_PLAY_POSITION = "ProduceCardPositionType_DeckGrave"
FORCE_PLAY_PICK_RANGE = "ProducePickRangeType_Select"
FORCE_PLAY_PICK_COUNT_TYPE = "ProducePickCountType_Unknown"
FORCE_PLAY_PICK_COUNT = (1, 1)

PLAYABLE_VALUE_EFFECT_ID = "e_effect-exam_playable_value_add-01"
ORDERED_AFFECTED_CARD_EFFECT_IDS = (
    PLAYABLE_VALUE_EFFECT_ID,
    EFFECT_ID,
    FORCE_PLAY_EFFECT_ID,
)

PLAN_TYPE_PLAN2 = "ProducePlanType_Plan2"
CATEGORY_MENTAL_SKILL = "ProduceCardCategory_MentalSkill"
COST_UNKNOWN = "ExamCostType_Unknown"
MOVE_LOST = "ProduceCardMovePositionType_Lost"
RARITY_SSR = "ProduceCardRarity_Ssr"
SEARCH_RARITIES = (
    "ProduceCardRarity_N",
    "ProduceCardRarity_R",
    "ProduceCardRarity_Sr",
    "ProduceCardRarity_Ssr",
)

POSITION_HAND = "ProduceCardPositionType_Hand"
POSITION_DECK = "ProduceCardPositionType_Deck"
POSITION_GRAVE = "ProduceCardPositionType_Grave"
POSITION_HOLD = "ProduceCardPositionType_Hold"
POSITION_LOST = "ProduceCardPositionType_Lost"

ANDROID_VERSION = "Android v3.2.3"
ANDROID_EXECUTOR_CTOR = "0x7E8BA7C"
ANDROID_EXECUTOR_EXECUTE = "0x7E8BC5C"
ANDROID_TRY_ADD = "0x7E9B64C"
ANDROID_GET = "0x7E9B824"
ANDROID_USE = "0x7E9CDFC"
ANDROID_EXECUTE_CARD_COMMAND_IMPL = "0x7ECEC00"
ANDROID_EXECUTE_CARD_COMMAND_METHOD = "0x7ECE628"

ANDROID_NATIVE_ADDRESSES = MappingProxyType(
    {
        "executor_ctor": ANDROID_EXECUTOR_CTOR,
        "executor_execute": ANDROID_EXECUTOR_EXECUTE,
        "try_add_play_count_buff_status": ANDROID_TRY_ADD,
        "get_play_count_buff": ANDROID_GET,
        "use_play_count_buff": ANDROID_USE,
        "execute_card_command_impl_callsite": ANDROID_EXECUTE_CARD_COMMAND_IMPL,
        "execute_card_command_impl_method": ANDROID_EXECUTE_CARD_COMMAND_METHOD,
    }
)

NATIVE_CARD_TRANSACTION_ORDER = (
    "accepted-play-and-is-playable-gate",
    "cost-paid-once-before-execute-card-command",
    "set-playing-card-transient",
    "capture-normal-listeners-before-direct-effects",
    "use-play-count-buff-once-on-live-playing-guid",
    "direct-effect-pass-0-in-master-order",
    "direct-effect-adds-status-before-following-force-play-slot",
    "direct-effect-repeat-passes-skip-once-slots-after-pass-zero",
    "force-play-queues-command-then-card-force-play-difference",
    "card-play-count-once-after-all-direct-effect-passes",
    "use-card-after-check-history-event-once",
    "move-play-card-once-after-card-play-count",
    "post-move-trigger-and-listener-settlement",
)

FORCE_PLAY_HANDOFF_ORDER = (
    "parent-direct-slots-playable-buff-force-in-master-order",
    "TryAddPlayCountBuffStatus-before-following-force-play-slot",
    "ForcePlay-search-deck-then-grave-native-order",
    "ForcePlay-Select-one-explicit-guid-no-rng",
    "CreateUsePool-command-with-guid-snapshot",
    "append-CardForcePlay-difference-after-command",
    "core-difference-queue-depth-first-source-order",
    "UsePool-rematch-guid-at-execution-time",
    "stage-same-guid-as-live-Playing",
    "UsePlayCountBuff-once-before-child-direct-effects",
    "child-N-plus-one-direct-effect-passes",
    "child-card-count-history-move-each-once",
)

INT32_MAX = 2**31 - 1


# The common Plan3 leaf has already proven the row/status/runtime shape.  The
# aliases are intentional: they make this Plan2 module a reusable facade
# without copying a second PlayCountBuff status implementation.
EXACT_EFFECT_ROW = _COMMON_EXACT_EFFECT_ROW
EFFECT_BY_ID = _COMMON_EFFECT_BY_ID
CardSearchEffectPlayCountBuffContract = CardSearchEffectPlayCountBuffContract
CardSearchEffectPlayCountBuffEffectRow = CardSearchEffectPlayCountBuffEffectRow


class Plan2CardSearchPlayCountBuffError(ValueError):
    """Stable Plan2 handoff error with a machine-readable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


Plan2CardSearchPlayCountBuffInputError = Plan2CardSearchPlayCountBuffError
Plan2CardSearchPlayCountBuffResolutionError = Plan2CardSearchPlayCountBuffError


def _strict_equal(actual: object, expected: object) -> bool:
    if type(actual) is not type(expected):
        return False
    return actual == expected


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise Plan2CardSearchPlayCountBuffInputError("invalid-text", label)
    return value


def _nonnegative(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Plan2CardSearchPlayCountBuffInputError(
            "invalid-nonnegative-int", label
        )
    return value


def _strict_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise Plan2CardSearchPlayCountBuffInputError("invalid-bool", label)
    return value


def _resolution_detail(error: BaseException) -> tuple[str, str]:
    if isinstance(error, CardSearchEffectPlayCountBuffError):
        return error.code, error.detail or error.code
    if isinstance(error, Plan2CardSearchPlayCountBuffError):
        return error.code, error.detail or error.code
    return "resolution-failed", str(error) or error.__class__.__name__


def _exact_effect_contract(contract: object) -> bool:
    return bool(
        isinstance(contract, CardSearchEffectPlayCountBuffContract)
        and contract.executable
        and contract.row == EXACT_EFFECT_ROW
        and contract.search.id == SEARCH_ID
        and contract.search.card_rarities == SEARCH_RARITIES
        and contract.search.card_position_type == "ProduceCardPositionType_Playing"
        and contract.search.limit_count == 0
    )


def load_master_card_search_effect_play_count_buff_rows(
    database: Path = DEFAULT_DATABASE,
) -> tuple[dict[str, object], ...]:
    return _common_load_rows(Path(database))


def contract_for_card_search_effect_play_count_buff(
    effect_id: str = EFFECT_ID,
    *,
    database: Path = DEFAULT_DATABASE,
) -> CardSearchEffectPlayCountBuffContract:
    contract = _common_contract_for_effect(effect_id, database=Path(database))
    if not _exact_effect_contract(contract):
        raise Plan2CardSearchPlayCountBuffResolutionError("contract-shape")
    return contract


def resolve_card_search_effect_play_count_buff_contract(
    effect_row: Mapping[str, object] | sqlite3.Row,
    *,
    database: Path = DEFAULT_DATABASE,
) -> CardSearchEffectPlayCountBuffContract:
    contract = _common_resolve_contract(effect_row, database=Path(database))
    if not _exact_effect_contract(contract):
        raise Plan2CardSearchPlayCountBuffResolutionError("contract-shape")
    return contract


def load_card_search_effect_play_count_buff_contract(
    database: Path = DEFAULT_DATABASE,
) -> CardSearchEffectPlayCountBuffContract:
    contract = _common_load_contract(Path(database))
    if not _exact_effect_contract(contract):
        raise Plan2CardSearchPlayCountBuffResolutionError("contract-shape")
    return contract


def try_resolve_card_search_effect_play_count_buff_contract(
    effect_row: Mapping[str, object] | sqlite3.Row,
    *,
    database: Path = DEFAULT_DATABASE,
) -> CardSearchEffectPlayCountBuffContract | CardSearchEffectPlayCountBuffUnresolvedInput:
    try:
        return resolve_card_search_effect_play_count_buff_contract(
            effect_row, database=Path(database)
        )
    except (CardSearchEffectPlayCountBuffError, Plan2CardSearchPlayCountBuffError) as error:
        return CardSearchEffectPlayCountBuffUnresolvedInput(
            getattr(error, "code", "contract-shape"),
            getattr(error, "detail", str(error)) or str(error),
        )


def load_card_search_buff_card_master(
    card: Plan3NativeCard,
    database: Path = DEFAULT_DATABASE,
) -> CardSearchBuffCardMaster:
    """Load one live GUID's ordered Master direct-effect list.

    This is intentionally generic over the card being consumed.  The exact
    affected-card check is separate below; the native Playing search itself
    matches the live card's rarity at execution time.
    """

    return _common_load_card_master(card, Path(database))


def _target_master_errors(master: object) -> tuple[str, ...]:
    if not isinstance(master, CardSearchBuffCardMaster):
        return ("master-type",)
    card = master.card
    errors: list[str] = []
    if card.id != CARD_ID:
        errors.append("card-id")
    if card.upgrade not in CARD_UPGRADES:
        errors.append("card-upgrade")
    if (
        card.plan_type,
        card.category,
        card.stamina_cost,
        card.cost_type,
        card.cost_value,
        card.move_position_type,
    ) != (
        PLAN_TYPE_PLAN2,
        CATEGORY_MENTAL_SKILL,
        0,
        COST_UNKNOWN,
        0,
        MOVE_LOST,
    ):
        errors.append("card-shape")
    if master.rarity != RARITY_SSR:
        errors.append("card-rarity")
    if tuple(slot.effect_id for slot in master.effect_slots) != ORDERED_AFFECTED_CARD_EFFECT_IDS:
        errors.append("card-effect-order")
    if any(slot.is_once_play_effect for slot in master.effect_slots):
        errors.append("card-once-shape")
    return tuple(errors)


def matches_affected_card_master(master: object) -> bool:
    return isinstance(master, CardSearchBuffCardMaster) and not _target_master_errors(
        master
    )


def _validate_target_effect_controls(
    master: CardSearchBuffCardMaster, database: Path
) -> None:
    expected: Mapping[str, tuple[str, int, int, int, int, str, tuple[str, ...]]] = {
        PLAYABLE_VALUE_EFFECT_ID: (
            "ProduceExamEffectType_ExamPlayableValueAdd",
            0,
            0,
            1,
            0,
            "",
            ("effect_group-visible-exam_playable_value_add-000",),
        ),
        EFFECT_ID: (
            EFFECT_TYPE,
            1,
            0,
            1,
            -1,
            "",
            (EFFECT_GROUP_ID,),
        ),
        FORCE_PLAY_EFFECT_ID: (
            FORCE_PLAY_EFFECT_TYPE,
            0,
            0,
            0,
            0,
            "",
            (),
        ),
    }
    with sqlite3.connect(Path(database)) as connection:
        connection.row_factory = sqlite3.Row
        for slot in master.effect_slots:
            row = connection.execute(
                "SELECT id, effect_type, value1, value2, effect_count, "
                "effect_turn, status_enchant_id, raw_json FROM effect WHERE id = ?",
                (slot.effect_id,),
            ).fetchone()
            if row is None:
                raise Plan2CardSearchPlayCountBuffResolutionError(
                    "target-effect-master-missing", slot.effect_id
                )
            raw = json.loads(str(row["raw_json"]))
            controls = expected.get(slot.effect_id)
            if (
                not isinstance(raw, Mapping)
                or controls is None
                or raw.get("id") != slot.effect_id
                or not _strict_equal(
                    tuple(raw.get("effectGroupIds", ())), controls[6]
                )
                or (
                    str(row["effect_type"]),
                    int(row["value1"]),
                    int(row["value2"]),
                    int(row["effect_count"]),
                    int(row["effect_turn"]),
                    str(row["status_enchant_id"]),
                )
                != controls[:6]
            ):
                raise Plan2CardSearchPlayCountBuffResolutionError(
                    "target-effect-master-shape", slot.effect_id
                )


def load_affected_card_versions(
    database: Path = DEFAULT_DATABASE,
) -> tuple[CardSearchBuffCardMaster, ...]:
    result: list[CardSearchBuffCardMaster] = []
    for upgrade in CARD_UPGRADES:
        card = Plan3NativeCard(
            guid=f"master-{CARD_ID}-{upgrade}",
            card_id=CARD_ID,
            base_upgrade=upgrade,
            temporary_upgrade=0,
            effective_upgrade=upgrade,
        )
        master = load_card_search_buff_card_master(card, Path(database))
        errors = _target_master_errors(master)
        if errors:
            raise Plan2CardSearchPlayCountBuffResolutionError(
                "affected-card-shape", f"{CARD_ID}+{upgrade}:{','.join(errors)}"
            )
        _validate_target_effect_controls(master, Path(database))
        result.append(master)
    return tuple(result)


def matches_affected_card_version(
    card: Plan3NativeCard,
    database: Path = DEFAULT_DATABASE,
) -> bool:
    if not isinstance(card, Plan3NativeCard):
        return False
    if (card.card_id, card.effective_upgrade) not in AFFECTED_CARD_VERSIONS:
        return False
    try:
        master = load_card_search_buff_card_master(card, Path(database))
        if _target_master_errors(master):
            return False
        _validate_target_effect_controls(master, Path(database))
    except (CardSearchEffectPlayCountBuffError, sqlite3.Error, ValueError, json.JSONDecodeError):
        return False
    return True


def match_playing_card_search(
    state: Plan3NativeState,
    playing_guid: str,
    contract: CardSearchEffectPlayCountBuffContract,
    *,
    database: Path = DEFAULT_DATABASE,
) -> CardSearchBuffMatch:
    if not _exact_effect_contract(contract):
        return CardSearchBuffMatch(
            playing_guid,
            None,
            None,
            False,
            ("contract-shape",),
        )
    return _common_match_playing_card_search(
        state, playing_guid, contract, database=Path(database)
    )


def install_card_search_effect_play_count_buff(
    runtime: PlayCountBuffRuntime,
    contract: CardSearchEffectPlayCountBuffContract,
    *,
    native_status_add_blocked: bool,
) -> PlayCountBuffStatusInstall:
    if not _exact_effect_contract(contract):
        raise Plan2CardSearchPlayCountBuffResolutionError("contract-shape")
    return _common_install_buff(
        runtime, contract, native_status_add_blocked=native_status_add_blocked
    )


def get_play_count_buff(
    runtime: PlayCountBuffRuntime,
    *,
    search_id: str = SEARCH_ID,
) -> int:
    return _common_get_play_count_buff(runtime, search_id=search_id)


def use_play_count_buff(
    runtime: PlayCountBuffRuntime,
    *,
    search_matches: bool,
    search_id: str = SEARCH_ID,
) -> PlayCountBuffUse:
    return _common_use_play_count_buff(
        runtime, search_matches=search_matches, search_id=search_id
    )


def direct_effect_passes(
    effect_slots: Sequence[CardPlayEffectSlot], repeat_count: int
) -> tuple[tuple[CardPlayEffectSlot, ...], ...]:
    return _common_direct_effect_passes(effect_slots, repeat_count)


@dataclass(frozen=True, slots=True)
class Plan2CardPlayCountBuffHookInput:
    plan2_state: Plan2State
    native_state: Plan3NativeState
    runtime: PlayCountBuffRuntime
    playing_guid: str
    accepted_play: bool = True
    is_playable: bool = True
    play_kind: str = "normal"
    is_consume_cost: bool = True
    is_use_playable_count: bool = True
    is_manual: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.plan2_state, Plan2State):
            raise Plan2CardSearchPlayCountBuffInputError("invalid-plan2-state")
        if not isinstance(self.native_state, Plan3NativeState):
            raise Plan2CardSearchPlayCountBuffInputError("invalid-native-state")
        if not isinstance(self.runtime, PlayCountBuffRuntime):
            raise Plan2CardSearchPlayCountBuffInputError("invalid-status-runtime")
        _text(self.playing_guid, "playing_guid")
        _strict_bool(self.accepted_play, "accepted_play")
        _strict_bool(self.is_playable, "is_playable")
        if self.play_kind not in {"normal", "forced", "extra"}:
            raise Plan2CardSearchPlayCountBuffInputError(
                "invalid-play-kind", self.play_kind
            )
        _strict_bool(self.is_consume_cost, "is_consume_cost")
        _strict_bool(self.is_use_playable_count, "is_use_playable_count")
        _strict_bool(self.is_manual, "is_manual")
        if self.play_kind == "forced" and (
            self.is_consume_cost or self.is_use_playable_count or self.is_manual
        ):
            raise Plan2CardSearchPlayCountBuffInputError(
                "forced-use-pool-policy", self.playing_guid
            )


@dataclass(frozen=True, slots=True)
class Plan2CardPlayCountBuffExecution:
    before_plan2: Plan2State
    after_plan2: Plan2State
    before_native: Plan3NativeState
    after_native: Plan3NativeState
    before_runtime: PlayCountBuffRuntime
    after_runtime: PlayCountBuffRuntime
    playing_guid: str
    card: Plan3NativeCard | None
    master: CardSearchBuffCardMaster | None
    accepted_play: bool
    is_playable: bool
    play_kind: str
    search_matched: bool
    selected_status_index: int | None
    selected_status_native_uid: int | None
    repeat_count: int
    effect_passes: tuple[tuple[CardPlayEffectSlot, ...], ...]
    unresolved: tuple[str, ...] = ()
    order: tuple[str, ...] = NATIVE_CARD_TRANSACTION_ORDER

    @property
    def executable(self) -> bool:
        return not self.unresolved

    @property
    def native_state_unchanged(self) -> bool:
        return self.before_native is self.after_native

    @property
    def plan2_state_unchanged(self) -> bool:
        return self.before_plan2 is self.after_plan2

    @property
    def status_consumed(self) -> bool:
        return self.selected_status_index is not None and self.executable

    @property
    def total_effect_passes(self) -> int:
        return len(self.effect_passes)

    @property
    def use_play_count_buff_call_count(self) -> int:
        return 1 if self.executable else 0

    @property
    def recently_used_status_uid_append_count(self) -> int:
        return 1 if self.status_consumed else 0

    @property
    def core_card_play_count_delta(self) -> int:
        return 1 if self.executable else 0

    @property
    def core_card_play_count_native_callsite_count(self) -> int:
        return 2 if self.executable else 0

    @property
    def core_global_card_play_count_delta(self) -> int:
        return 1 if self.executable else 0

    @property
    def core_turn_card_play_count_delta(self) -> int:
        return 1 if self.executable else 0

    @property
    def core_history_event_count(self) -> int:
        return 1 if self.executable else 0

    @property
    def core_final_move_count(self) -> int:
        return 1 if self.executable else 0

    @property
    def core_after_move_listener_settlement_count(self) -> int:
        return 1 if self.executable else 0

    @property
    def buff_playable_count_delta(self) -> int:
        return 0

    @property
    def buff_extra_play_count_delta(self) -> int:
        return 0

    @property
    def forced_cost_payment_count(self) -> int:
        return 0 if self.play_kind == "forced" and self.executable else 0


def _empty_execution(
    hook: Plan2CardPlayCountBuffHookInput,
    *,
    unresolved: tuple[str, ...],
) -> Plan2CardPlayCountBuffExecution:
    return Plan2CardPlayCountBuffExecution(
        hook.plan2_state,
        hook.plan2_state,
        hook.native_state,
        hook.native_state,
        hook.runtime,
        hook.runtime,
        hook.playing_guid,
        None,
        None,
        hook.accepted_play,
        hook.is_playable,
        hook.play_kind,
        False,
        None,
        None,
        0,
        (),
        unresolved,
    )


def execute_card_search_effect_play_count_buff(
    hook: Plan2CardPlayCountBuffHookInput,
    *,
    contract: CardSearchEffectPlayCountBuffContract | None = None,
    database: Path = DEFAULT_DATABASE,
) -> Plan2CardPlayCountBuffExecution:
    """Run only the pre-direct-effect Plan2 hook.

    The hook consumes one status layer, expands the direct-effect passes, and
    leaves both Plan2 scalar state and GUID zones untouched.  Final card
    settlement is a core/ForcePlay handoff responsibility.
    """

    if not isinstance(hook, Plan2CardPlayCountBuffHookInput):
        raise Plan2CardSearchPlayCountBuffInputError("invalid-hook-input")
    if not hook.accepted_play:
        return _empty_execution(hook, unresolved=("play-rejected",))
    if not hook.is_playable:
        return _empty_execution(hook, unresolved=("card-not-playable",))
    try:
        resolved = contract or load_card_search_effect_play_count_buff_contract(
            Path(database)
        )
    except (CardSearchEffectPlayCountBuffError, Plan2CardSearchPlayCountBuffError) as error:
        return _empty_execution(hook, unresolved=(getattr(error, "code", str(error)),))
    match = match_playing_card_search(
        hook.native_state,
        hook.playing_guid,
        resolved,
        database=Path(database),
    )
    if not match.executable:
        return Plan2CardPlayCountBuffExecution(
            hook.plan2_state,
            hook.plan2_state,
            hook.native_state,
            hook.native_state,
            hook.runtime,
            hook.runtime,
            hook.playing_guid,
            match.card,
            match.master,
            hook.accepted_play,
            hook.is_playable,
            hook.play_kind,
            False,
            None,
            None,
            0,
            (),
            match.unresolved,
        )
    assert match.card is not None and match.master is not None
    try:
        use = use_play_count_buff(
            hook.runtime,
            search_matches=match.matched,
            search_id=resolved.search.id,
        )
        if use.unresolved:
            return Plan2CardPlayCountBuffExecution(
                hook.plan2_state,
                hook.plan2_state,
                hook.native_state,
                hook.native_state,
                hook.runtime,
                hook.runtime,
                hook.playing_guid,
                match.card,
                match.master,
                hook.accepted_play,
                hook.is_playable,
                hook.play_kind,
                match.matched,
                None,
                None,
                0,
                (),
                use.unresolved,
            )
        passes = direct_effect_passes(match.master.effect_slots, use.repeat_count)
        selected_uid = None
        if use.selected_status_index is not None:
            selected_uid = hook.runtime.statuses[use.selected_status_index].native_uid
    except (CardSearchEffectPlayCountBuffError, Plan2CardSearchPlayCountBuffError) as error:
        return Plan2CardPlayCountBuffExecution(
            hook.plan2_state,
            hook.plan2_state,
            hook.native_state,
            hook.native_state,
            hook.runtime,
            hook.runtime,
            hook.playing_guid,
            match.card,
            match.master,
            hook.accepted_play,
            hook.is_playable,
            hook.play_kind,
            match.matched,
            None,
            None,
            0,
            (),
            (getattr(error, "code", str(error)),),
        )
    return Plan2CardPlayCountBuffExecution(
        hook.plan2_state,
        hook.plan2_state,
        hook.native_state,
        hook.native_state,
        hook.runtime,
        use.after,
        hook.playing_guid,
        match.card,
        match.master,
        hook.accepted_play,
        hook.is_playable,
        hook.play_kind,
        match.matched,
        use.selected_status_index,
        selected_uid,
        use.repeat_count,
        passes,
    )


@dataclass(frozen=True, slots=True)
class ForcePlayHandoffContract:
    row: Plan2ForcePlayEffectRow
    search: ProduceCardSearchRule

    @property
    def unresolved_reasons(self) -> tuple[str, ...]:
        row = self.row
        expected_row = {
            "effect_id": FORCE_PLAY_EFFECT_ID,
            "effect_type": FORCE_PLAY_EFFECT_TYPE,
            "value1": 0,
            "value2": 0,
            "effect_count": 0,
            "effect_turn": 0,
            "target_card_id": "",
            "target_upgrade_count": 0,
            "target_effect_type": "ProduceExamEffectType_Unknown",
            "search_id": FORCE_PLAY_SEARCH_ID,
            "move_position_type": "ProduceCardMovePositionType_Unknown",
            "pick_range_type": FORCE_PLAY_PICK_RANGE,
            "pick_count_reference_search_id": "",
            "pick_count_type": FORCE_PLAY_PICK_COUNT_TYPE,
            "pick_count_min": 1,
            "pick_count_max": 1,
            "search_id2": "",
            "pick_range_type2": "ProducePickRangeType_Unknown",
            "pick_count_reference_search_id2": "",
            "pick_count_type2": FORCE_PLAY_PICK_COUNT_TYPE,
            "pick_count_min2": 0,
            "pick_count_max2": 0,
            "chain_effect_id": "",
            "chain_effect_ids": (),
            "status_enchant_id": "",
            "card_status_enchant_id": "",
            "card_grow_effect_ids": (),
            "effect_group_ids": (),
        }
        reasons = [
            f"force-row:{field}"
            for field, expected in expected_row.items()
            if getattr(row, field) != expected
        ]
        expected_search = {
            "id": FORCE_PLAY_SEARCH_ID,
            "card_rarities": (),
            "produce_card_ids": (),
            "upgrade_counts": (),
            "plan_type": "ProducePlanType_Unknown",
            "card_categories": (),
            "card_status_type": "ProduceCardSearchStatusType_Unknown",
            "order_type": "ProduceCardOrderType_Unknown",
            "card_position_type": FORCE_PLAY_POSITION,
            "card_search_tag": "",
            "produce_card_random_pool_id": "",
            "limit_count": 0,
            "stamina_min_max_type": "ConditionMinMaxType_Unknown",
            "stamina_min": 0,
            "stamina_max": 0,
            "exam_effect_type": "ProduceExamEffectType_Unknown",
            "effect_group_ids": (),
            "is_self": False,
            "produce_card_pool_id": "",
            "cost_type": COST_UNKNOWN,
            "is_customized": False,
        }
        reasons.extend(
            f"force-search:{field}"
            for field, expected in expected_search.items()
            if getattr(self.search, field) != expected
        )
        return tuple(dict.fromkeys(reasons))

    @property
    def executable(self) -> bool:
        return not self.unresolved_reasons


def load_force_play_handoff_contract(
    database: Path = DEFAULT_DATABASE,
) -> ForcePlayHandoffContract:
    """Read the same target ForcePlay row through the Plan2 force parser."""

    row = load_force_play_effect_row(FORCE_PLAY_EFFECT_ID, Path(database))
    try:
        search = load_produce_card_search(row.search_id, Path(database))
    except (KeyError, ValueError, sqlite3.Error) as error:
        raise Plan2CardSearchPlayCountBuffResolutionError(
            "force-search-load-failed", row.search_id
        ) from error
    contract = ForcePlayHandoffContract(row, search)
    if not contract.executable:
        raise Plan2CardSearchPlayCountBuffResolutionError(
            "force-handoff-shape", ",".join(contract.unresolved_reasons)
        )
    return contract


def try_load_force_play_handoff_contract(
    database: Path = DEFAULT_DATABASE,
) -> ForcePlayHandoffContract | CardSearchEffectPlayCountBuffUnresolvedInput:
    try:
        return load_force_play_handoff_contract(Path(database))
    except (Plan2CardSearchPlayCountBuffError, sqlite3.Error) as error:
        return CardSearchEffectPlayCountBuffUnresolvedInput(
            getattr(error, "code", "force-handoff-shape"),
            getattr(error, "detail", str(error)) or str(error),
        )


@dataclass(frozen=True, slots=True)
class Plan2ForcePlayHandoffCommand:
    """Minimal central-ready command shape used by the handoff adapter."""

    ordinal: int
    guid: str
    card: Plan3NativeCard
    original_source_zone: str
    original_source_index: int
    base_move_position_type: str = POSITION_LOST
    is_consume_cost: bool = False
    is_use_playable_count: bool = False
    is_manual: bool = False
    resolves_source_by_guid_at_execution: bool = True
    enchant_effect_uid: int = 0

    def __post_init__(self) -> None:
        _nonnegative(self.ordinal, "ordinal")
        _text(self.guid, "guid")
        if not isinstance(self.card, Plan3NativeCard) or self.card.guid != self.guid:
            raise Plan2CardSearchPlayCountBuffInputError(
                "command-guid-card-mismatch", self.guid
            )
        _text(self.original_source_zone, "original_source_zone")
        _nonnegative(self.original_source_index, "original_source_index")
        for label in ("is_consume_cost", "is_use_playable_count", "is_manual"):
            _strict_bool(getattr(self, label), label)
        if self.is_consume_cost or self.is_use_playable_count or self.is_manual:
            raise Plan2CardSearchPlayCountBuffInputError(
                "forced-use-pool-policy", self.guid
            )
        if self.resolves_source_by_guid_at_execution is not True:
            raise Plan2CardSearchPlayCountBuffInputError(
                "forced-use-pool-guid-policy", self.guid
            )
        _nonnegative(self.enchant_effect_uid, "enchant_effect_uid")

    def to_use_pool_command(self) -> Plan3UsePoolCommand:
        return Plan3UsePoolCommand(
            guid=self.guid,
            is_consume_cost=False,
            is_use_playable_count=False,
            is_manual=False,
            enchant_effect_uid=self.enchant_effect_uid,
        )


def _command_guid(command: object) -> str:
    return _text(getattr(command, "guid", None), "command.guid")


def _command_card(command: object) -> Plan3NativeCard:
    card = getattr(command, "card", None)
    if not isinstance(card, Plan3NativeCard):
        raise Plan2CardSearchPlayCountBuffInputError("command-card-shape")
    if card.guid != _command_guid(command):
        raise Plan2CardSearchPlayCountBuffInputError(
            "command-guid-card-mismatch", card.guid
        )
    return card


def _command_source_snapshot(command: object) -> tuple[str, int]:
    source = getattr(command, "original_source_zone", None)
    if not isinstance(source, str) or not source:
        source = getattr(command, "original_source_position_type", None)
    index = getattr(command, "original_source_index", None)
    if not isinstance(source, str) or not source:
        raise Plan2CardSearchPlayCountBuffInputError(
            "command-source-zone-shape", _command_guid(command)
        )
    _nonnegative(index, "command.original_source_index")
    return source, index


def _command_to_use_pool(command: object) -> Plan3UsePoolCommand:
    if isinstance(command, Plan3UsePoolCommand):
        return command
    converter = getattr(command, "to_use_pool_command", None)
    if callable(converter):
        candidate = converter()
    else:
        candidate = Plan3UsePoolCommand.from_queued(command)
    if not isinstance(candidate, Plan3UsePoolCommand):
        raise Plan2CardSearchPlayCountBuffInputError("invalid-use-pool-command")
    if candidate.guid != _command_guid(command):
        raise Plan2CardSearchPlayCountBuffInputError(
            "use-pool-command-guid-mismatch", _command_guid(command)
        )
    return candidate


def _validate_handoff_command(command: object, ordinal: int) -> tuple[str, Plan3NativeCard, tuple[str, int], Plan3UsePoolCommand]:
    guid = _command_guid(command)
    card = _command_card(command)
    source_snapshot = _command_source_snapshot(command)
    use_pool = _command_to_use_pool(command)
    for label in ("is_consume_cost", "is_use_playable_count", "is_manual"):
        value = getattr(command, label, False)
        if value is not False:
            raise Plan2CardSearchPlayCountBuffInputError(
                "forced-use-pool-policy", f"{guid}:{label}"
            )
    if getattr(command, "resolves_source_by_guid_at_execution", True) is not True:
        raise Plan2CardSearchPlayCountBuffInputError(
            "forced-use-pool-guid-policy", guid
        )
    command_ordinal = getattr(command, "ordinal", ordinal)
    if command_ordinal != ordinal:
        raise Plan2CardSearchPlayCountBuffInputError(
            "force-command-order", guid
        )
    return guid, card, source_snapshot, use_pool


def _difference_guid(difference: object) -> str:
    return _text(getattr(difference, "guid", None), "difference.guid")


@dataclass(frozen=True, slots=True)
class Plan2CardSearchPlayCountBuffHandoff:
    contract: ForcePlayHandoffContract
    runtime_before: PlayCountBuffRuntime
    runtime_after_parent: PlayCountBuffRuntime
    parent_guid: str
    commands: tuple[object, ...]
    command_guids: tuple[str, ...]
    command_source_snapshots: tuple[tuple[str, int], ...]
    difference_guids: tuple[str, ...]
    rng_consumed: bool
    status_consumed_at_handoff: bool
    unresolved: tuple[str, ...] = ()
    order: tuple[str, ...] = FORCE_PLAY_HANDOFF_ORDER

    @property
    def executable(self) -> bool:
        return not self.unresolved

    @property
    def selected_count(self) -> int:
        return len(self.command_guids)

    @property
    def use_pool_command_count(self) -> int:
        return len(self.command_guids)

    @property
    def expected_use_play_count_buff_calls_at_execution(self) -> int:
        return len(self.command_guids)

    @property
    def expected_card_settlements(self) -> int:
        return len(self.command_guids)


def _unresolved_handoff(
    runtime: PlayCountBuffRuntime,
    contract: ForcePlayHandoffContract,
    *,
    parent_guid: str,
    reason: str,
) -> Plan2CardSearchPlayCountBuffHandoff:
    return Plan2CardSearchPlayCountBuffHandoff(
        contract,
        runtime,
        runtime,
        parent_guid,
        (),
        (),
        (),
        (),
        False,
        False,
        (reason,),
    )


def build_force_play_handoff(
    runtime: PlayCountBuffRuntime,
    commands: Sequence[object],
    *,
    differences: Sequence[object] | None = None,
    parent_guid: str = "",
    contract: ForcePlayHandoffContract | None = None,
    database: Path = DEFAULT_DATABASE,
) -> Plan2CardSearchPlayCountBuffHandoff:
    """Validate the source-order queue and defer UsePlayCountBuff to core.

    The PlayCountBuff status is installed by the parent direct slot before
    the following ForcePlay slot.  No status is consumed while merely
    constructing the queue; each live command consumes at most one matching
    layer after GUID relocation.
    """

    if not isinstance(runtime, PlayCountBuffRuntime):
        raise Plan2CardSearchPlayCountBuffInputError("invalid-status-runtime")
    try:
        resolved_contract = contract or load_force_play_handoff_contract(
            Path(database)
        )
    except (Plan2CardSearchPlayCountBuffError, CardSearchEffectPlayCountBuffError) as error:
        fallback = ForcePlayHandoffContract(
            Plan2ForcePlayEffectRow(
                FORCE_PLAY_EFFECT_ID,
                FORCE_PLAY_EFFECT_TYPE,
                0,
                0,
                0,
                0,
                "",
                0,
                "ProduceExamEffectType_Unknown",
                FORCE_PLAY_SEARCH_ID,
                "ProduceCardMovePositionType_Unknown",
                FORCE_PLAY_PICK_RANGE,
                "",
                FORCE_PLAY_PICK_COUNT_TYPE,
                1,
                1,
                "",
                "ProducePickRangeType_Unknown",
                "",
                FORCE_PLAY_PICK_COUNT_TYPE,
                0,
                0,
                "",
                (),
                "",
                "",
                (),
                (),
            ),
            ProduceCardSearchRule(
                FORCE_PLAY_SEARCH_ID,
                (),
                (),
                (),
                "ProducePlanType_Unknown",
                (),
                "ProduceCardSearchStatusType_Unknown",
                "ProduceCardOrderType_Unknown",
                FORCE_PLAY_POSITION,
                "",
                "",
                0,
                "ConditionMinMaxType_Unknown",
                0,
                0,
                "ProduceExamEffectType_Unknown",
                (),
                False,
                "",
                COST_UNKNOWN,
                False,
            ),
        )
        return _unresolved_handoff(
            runtime,
            fallback,
            parent_guid=parent_guid,
            reason=getattr(error, "code", str(error)),
        )
    if not isinstance(resolved_contract, ForcePlayHandoffContract):
        raise Plan2CardSearchPlayCountBuffInputError("invalid-force-contract")
    if not resolved_contract.executable:
        return _unresolved_handoff(
            runtime,
            resolved_contract,
            parent_guid=parent_guid,
            reason="force-handoff-shape",
        )
    try:
        parent = "" if not parent_guid else _text(parent_guid, "parent_guid")
        queue = tuple(commands)
        normalized = tuple(
            _validate_handoff_command(command, ordinal)
            for ordinal, command in enumerate(queue)
        )
        guids = tuple(item[0] for item in normalized)
        if len(set(guids)) != len(guids):
            raise Plan2CardSearchPlayCountBuffInputError(
                "force-guid-revisit", ",".join(guids)
            )
        if differences is None:
            difference_guids = guids
        else:
            difference_tuple = tuple(differences)
            difference_guids = tuple(_difference_guid(item) for item in difference_tuple)
            if difference_guids != guids:
                raise Plan2CardSearchPlayCountBuffInputError(
                    "command-difference-order-mismatch"
                )
        return Plan2CardSearchPlayCountBuffHandoff(
            resolved_contract,
            runtime,
            runtime,
            parent,
            queue,
            guids,
            tuple(item[2] for item in normalized),
            difference_guids,
            False,
            False,
        )
    except (Plan2CardSearchPlayCountBuffError, TypeError, AttributeError) as error:
        return _unresolved_handoff(
            runtime,
            resolved_contract,
            parent_guid=parent_guid,
            reason=getattr(error, "code", str(error)),
        )


def handoff_from_plan2_force_play(
    runtime: PlayCountBuffRuntime,
    force_plan: Plan2ForcePlayPlanResult,
    *,
    parent_guid: str = "",
    database: Path = DEFAULT_DATABASE,
) -> Plan2CardSearchPlayCountBuffHandoff:
    """Chain an already resolved Plan2 ForcePlay plan without re-planning it."""

    if not isinstance(force_plan, Plan2ForcePlayPlanResult):
        raise Plan2CardSearchPlayCountBuffInputError("invalid-force-plan")
    if force_plan.unresolved:
        contract = load_force_play_handoff_contract(Path(database))
        return _unresolved_handoff(
            runtime,
            contract,
            parent_guid=parent_guid,
            reason="force-plan-unresolved",
        )
    return build_force_play_handoff(
        runtime,
        force_plan.commands,
        differences=force_plan.differences,
        parent_guid=parent_guid,
        database=Path(database),
    )


# Names used by callers that describe this boundary as a chain rather than a
# handoff.  They remain aliases, not separate implementations.
chain_play_count_buff_to_force_play = build_force_play_handoff
handoff_play_count_buff_to_force_play = build_force_play_handoff


def _plan3_scalar_bridge(
    plan2_state: Plan2State, native_state: Plan3NativeState
) -> Plan3State:
    return Plan3State(
        turns_remaining=1,
        stamina=plan2_state.stamina,
        plays_remaining=1,
        hand=tuple(card.ref for card in native_state.hand),
        draw_pile=tuple(card.ref for card in native_state.deck),
        discard_pile=tuple(card.ref for card in native_state.grave),
        lost_pile=tuple(card.ref for card in native_state.lost),
        hold_pile=tuple(card.ref for card in native_state.hold),
    )


@dataclass(frozen=True, slots=True)
class Plan2ForcePlayBuffStage:
    command: object
    use_pool_command: Plan3UsePoolCommand
    plan2_before: Plan2State
    native_before: Plan3NativeState
    plan2_playing: Plan2State
    native_playing: Plan3NativeState
    shared_stage: Plan3UsePoolStage
    card: Plan3NativeCard
    snapshot_source_zone: str
    snapshot_source_index: int
    execution_source_zone: str
    execution_source_index: int
    order: tuple[str, ...] = FORCE_PLAY_HANDOFF_ORDER


def stage_force_play_guid_for_buff(
    plan2_state: Plan2State,
    native_state: Plan3NativeState,
    command: object,
) -> Plan2ForcePlayBuffStage:
    """Rematch a queued GUID across current zones and stage it as Playing."""

    if not isinstance(plan2_state, Plan2State):
        raise Plan2CardSearchPlayCountBuffInputError("invalid-plan2-state")
    if not isinstance(native_state, Plan3NativeState):
        raise Plan2CardSearchPlayCountBuffInputError("invalid-native-state")
    guid, snapshot_card, source_snapshot, use_pool = _validate_handoff_command(
        command, getattr(command, "ordinal", 0)
    )
    bridge = _plan3_scalar_bridge(plan2_state, native_state)
    try:
        shared = stage_plan3_use_pool_guid(bridge, native_state, use_pool)
    except Plan3NativeStateError as error:
        raise Plan2CardSearchPlayCountBuffError(error.code, error.detail) from error
    current = shared.card
    if (
        current.card_id,
        current.base_upgrade,
        current.temporary_upgrade,
        current.effective_upgrade,
    ) != (
        snapshot_card.card_id,
        snapshot_card.base_upgrade,
        snapshot_card.temporary_upgrade,
        snapshot_card.effective_upgrade,
    ):
        raise Plan2CardSearchPlayCountBuffError(
            "guid-card-snapshot-mismatch", guid
        )
    return Plan2ForcePlayBuffStage(
        command,
        use_pool,
        plan2_state,
        native_state,
        plan2_state,
        shared.native_playing,
        shared,
        current,
        source_snapshot[0],
        source_snapshot[1],
        shared.source_zone,
        shared.source_index,
    )


@dataclass(frozen=True, slots=True)
class Plan2ForcePlayBuffExecution:
    before_plan2: Plan2State
    after_plan2: Plan2State
    before_native: Plan3NativeState
    after_native: Plan3NativeState
    before_runtime: PlayCountBuffRuntime
    after_runtime: PlayCountBuffRuntime
    command_guid: str
    stage: Plan2ForcePlayBuffStage | None
    buff: Plan2CardPlayCountBuffExecution | None
    unresolved: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return not self.unresolved and self.stage is not None and self.buff is not None

    @property
    def status_consumed(self) -> bool:
        return bool(self.executable and self.buff and self.buff.status_consumed)

    @property
    def use_play_count_buff_call_count(self) -> int:
        return 1 if self.executable else 0

    @property
    def source_zone(self) -> str | None:
        return None if self.stage is None else self.stage.execution_source_zone

    @property
    def source_index(self) -> int | None:
        return None if self.stage is None else self.stage.execution_source_index

    @property
    def final_move_deferred(self) -> bool:
        return self.executable

    @property
    def core_card_play_count_delta_after_settlement(self) -> int:
        return 1 if self.executable else 0

    @property
    def core_global_card_play_count_delta_after_settlement(self) -> int:
        return 1 if self.executable else 0

    @property
    def core_history_event_count_after_settlement(self) -> int:
        return 1 if self.executable else 0

    @property
    def core_final_move_count_after_settlement(self) -> int:
        return 1 if self.executable else 0


def execute_force_play_buff_once(
    plan2_state: Plan2State,
    native_state: Plan3NativeState,
    runtime: PlayCountBuffRuntime,
    command: object,
    *,
    contract: CardSearchEffectPlayCountBuffContract | None = None,
    database: Path = DEFAULT_DATABASE,
) -> Plan2ForcePlayBuffExecution:
    """Stage one forced GUID, consume the status once, and defer settlement."""

    if not isinstance(runtime, PlayCountBuffRuntime):
        raise Plan2CardSearchPlayCountBuffInputError("invalid-status-runtime")
    guid = getattr(command, "guid", "")
    try:
        stage = stage_force_play_guid_for_buff(
            plan2_state, native_state, command
        )
        hook = Plan2CardPlayCountBuffHookInput(
            plan2_state=plan2_state,
            native_state=stage.native_playing,
            runtime=runtime,
            playing_guid=stage.card.guid,
            play_kind="forced",
            is_consume_cost=False,
            is_use_playable_count=False,
            is_manual=False,
        )
        buff = execute_card_search_effect_play_count_buff(
            hook, contract=contract, database=Path(database)
        )
        if not buff.executable:
            return Plan2ForcePlayBuffExecution(
                plan2_state,
                plan2_state,
                native_state,
                native_state,
                runtime,
                runtime,
                str(guid),
                None,
                None,
                buff.unresolved,
            )
    except (Plan2CardSearchPlayCountBuffError, CardSearchEffectPlayCountBuffError, Plan3NativeStateError) as error:
        return Plan2ForcePlayBuffExecution(
            plan2_state,
            plan2_state,
            native_state,
            native_state,
            runtime,
            runtime,
            str(guid),
            None,
            None,
            (getattr(error, "code", str(error)),),
        )
    return Plan2ForcePlayBuffExecution(
        plan2_state,
        plan2_state,
        native_state,
        stage.native_playing,
        runtime,
        buff.after_runtime,
        stage.card.guid,
        stage,
        buff,
    )


@dataclass(frozen=True, slots=True)
class Plan2ForcePlayBuffQueueExecution:
    before_plan2: Plan2State
    after_plan2: Plan2State
    before_native: Plan3NativeState
    after_native: Plan3NativeState
    before_runtime: PlayCountBuffRuntime
    after_runtime: PlayCountBuffRuntime
    executions: tuple[Plan2ForcePlayBuffExecution, ...]
    unresolved: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return not self.unresolved

    @property
    def use_play_count_buff_call_count(self) -> int:
        return sum(item.use_play_count_buff_call_count for item in self.executions)

    @property
    def status_consumed_count(self) -> int:
        return sum(item.status_consumed for item in self.executions)

    @property
    def expected_card_settlements(self) -> int:
        return len(self.executions)

    @property
    def expected_history_event_count(self) -> int:
        return len(self.executions)

    @property
    def expected_final_move_count(self) -> int:
        return len(self.executions)


def execute_force_play_buff_handoff(
    plan2_state: Plan2State,
    native_state: Plan3NativeState,
    runtime: PlayCountBuffRuntime,
    handoff: Plan2CardSearchPlayCountBuffHandoff,
    *,
    database: Path = DEFAULT_DATABASE,
) -> Plan2ForcePlayBuffQueueExecution:
    """Run the pre-settlement hook for every queued GUID in source order.

    This function deliberately does not move a played card to Lost/Grave or
    increment Plan2 counts.  It returns a ready-to-settle sequence so the
    existing ForcePlay/core boundary remains the sole owner of those effects.
    A missing/disappeared GUID is preflighted before any status layer is spent.
    """

    if not isinstance(plan2_state, Plan2State):
        raise Plan2CardSearchPlayCountBuffInputError("invalid-plan2-state")
    if not isinstance(native_state, Plan3NativeState):
        raise Plan2CardSearchPlayCountBuffInputError("invalid-native-state")
    if not isinstance(runtime, PlayCountBuffRuntime):
        raise Plan2CardSearchPlayCountBuffInputError("invalid-status-runtime")
    if not isinstance(handoff, Plan2CardSearchPlayCountBuffHandoff):
        raise Plan2CardSearchPlayCountBuffInputError("invalid-handoff")
    if not handoff.executable:
        return Plan2ForcePlayBuffQueueExecution(
            plan2_state,
            plan2_state,
            native_state,
            native_state,
            runtime,
            runtime,
            (),
            handoff.unresolved or ("handoff-unresolved",),
        )
    try:
        # Preflight every GUID and immutable identity before consuming any
        # status.  The live source index is intentionally not compared.
        seen: set[str] = set()
        for ordinal, command in enumerate(handoff.commands):
            guid, card, _snapshot, _use_pool = _validate_handoff_command(
                command, ordinal
            )
            if guid in seen:
                raise Plan2CardSearchPlayCountBuffError(
                    "force-guid-revisit", guid
                )
            seen.add(guid)
            current = native_state.card_by_guid(guid)
            if (
                current.card_id,
                current.base_upgrade,
                current.temporary_upgrade,
                current.effective_upgrade,
            ) != (
                card.card_id,
                card.base_upgrade,
                card.temporary_upgrade,
                card.effective_upgrade,
            ):
                raise Plan2CardSearchPlayCountBuffError(
                    "guid-card-snapshot-mismatch", guid
                )
    except (Plan2CardSearchPlayCountBuffError, Plan3NativeStateError) as error:
        return Plan2ForcePlayBuffQueueExecution(
            plan2_state,
            plan2_state,
            native_state,
            native_state,
            runtime,
            runtime,
            (),
            (getattr(error, "code", str(error)),),
        )

    current_native = native_state
    current_runtime = runtime
    results: list[Plan2ForcePlayBuffExecution] = []
    for command in handoff.commands:
        result = execute_force_play_buff_once(
            plan2_state,
            current_native,
            current_runtime,
            command,
            database=Path(database),
        )
        if not result.executable:
            return Plan2ForcePlayBuffQueueExecution(
                plan2_state,
                plan2_state,
                native_state,
                native_state,
                runtime,
                runtime,
                (),
                result.unresolved or ("force-queue-unresolved",),
            )
        results.append(result)
        current_native = result.after_native
        current_runtime = result.after_runtime
    return Plan2ForcePlayBuffQueueExecution(
        plan2_state,
        plan2_state,
        native_state,
        current_native,
        runtime,
        current_runtime,
        tuple(results),
    )


@dataclass(frozen=True, slots=True)
class PlayCountBuffAffectedCardVersion:
    card_id: str
    upgrade: int
    relation: str = "co-overlap-only"
    effect_id: str = EFFECT_ID
    force_effect_id: str = FORCE_PLAY_EFFECT_ID

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PlayCountBuffAccounting:
    affected: int
    direct: int
    co: int
    direct_when_force_play_ready: int
    affected_unique_cards: int
    direct_unique_cards: int
    co_unique_cards: int
    versions: tuple[PlayCountBuffAffectedCardVersion, ...]
    baseline_force_play_affected: int
    baseline_force_play_direct: int
    baseline_force_play_co: int

    def to_dict(self) -> dict[str, object]:
        return {
            "affected": self.affected,
            "direct": self.direct,
            "co": self.co,
            "direct_when_force_play_ready": self.direct_when_force_play_ready,
            "affected_unique_cards": self.affected_unique_cards,
            "direct_unique_cards": self.direct_unique_cards,
            "co_unique_cards": self.co_unique_cards,
            "versions": [item.to_dict() for item in self.versions],
            "baseline_force_play": {
                "affected": self.baseline_force_play_affected,
                "direct": self.baseline_force_play_direct,
                "co": self.baseline_force_play_co,
            },
        }


def probe_accounting(
    database: Path = DEFAULT_DATABASE,
) -> PlayCountBuffAccounting:
    """Chain this leaf with the current Plan2 ForcePlay baseline.

    Buff alone cannot unlock this card because the same four card versions
    still contain the ForcePlay slot.  Once that central ForcePlay leaf is
    ready, all four become direct; the returned fields keep both facts
    explicit instead of changing the current central coverage artifact.
    """

    load_affected_card_versions(Path(database))
    force = probe_force_play_accounting(Path(database))
    target_force_rows = tuple(
        item
        for item in force.versions
        if (item.card_id, item.upgrade) in AFFECTED_CARD_VERSIONS
        and FORCE_PLAY_EFFECT_ID in item.force_effect_ids
    )
    if len(target_force_rows) != AFFECTED_CARD_VERSION_COUNT:
        raise Plan2CardSearchPlayCountBuffResolutionError(
            "force-baseline-target-mismatch", str(len(target_force_rows))
        )
    versions = tuple(
        PlayCountBuffAffectedCardVersion(CARD_ID, upgrade)
        for upgrade in CARD_UPGRADES
    )
    co = len(target_force_rows)
    direct = 0
    return PlayCountBuffAccounting(
        affected=AFFECTED_CARD_VERSION_COUNT,
        direct=direct,
        co=co,
        direct_when_force_play_ready=co,
        affected_unique_cards=1,
        direct_unique_cards=0,
        co_unique_cards=1,
        versions=versions,
        baseline_force_play_affected=force.affected,
        baseline_force_play_direct=force.direct,
        baseline_force_play_co=force.co,
    )


def native_audit_to_dict(
    database: Path = DEFAULT_DATABASE,
) -> dict[str, object]:
    contract = load_card_search_effect_play_count_buff_contract(Path(database))
    force_contract = load_force_play_handoff_contract(Path(database))
    masters = load_affected_card_versions(Path(database))
    accounting = probe_accounting(Path(database))
    return {
        "schema_version": 1,
        "scope": "Plan2 standalone exact PlayCountBuff for p_card-02-ido-3_192",
        "internet_used": False,
        "scope_guard": {
            "new_runtime": "src/gkms_tool/plan2_card_search_play_count_buff.py",
            "new_tests": "tests/test_plan2_card_search_play_count_buff.py",
            "native_audit": "var/coverage/plan2_card_search_play_count_buff_native_audit.json",
            "central_coverage_modified": False,
            "plan2_core_runtime_modified": False,
            "plan2_native_search_modified": False,
            "plan3_modified": False,
            "gui_or_controller_modified": False,
            "runtime_proxy_called": False,
            "hash_security_time_validation": False,
        },
        "master": {
            "database": str(Path(database)),
            "target_card": {
                "id": CARD_ID,
                "name": CARD_NAME,
                "plan_type": PLAN_TYPE_PLAN2,
                "category": CATEGORY_MENTAL_SKILL,
                "rarity": RARITY_SSR,
                "upgrades": list(CARD_UPGRADES),
                "stamina": 0,
                "cost_type": COST_UNKNOWN,
                "cost_value": 0,
                "move_position_type": MOVE_LOST,
                "ordered_play_effect_ids": list(ORDERED_AFFECTED_CARD_EFFECT_IDS),
                "is_once_play_effect": [False, False, False],
            },
            "effect": {
                "id": contract.row.id,
                "type": contract.row.effect_type,
                "type_value": EFFECT_TYPE_VALUE,
                "value1": contract.row.value1,
                "value2": contract.row.value2,
                "effect_count": contract.row.effect_count,
                "effect_turn": contract.row.effect_turn,
                "effect_group_ids": [EFFECT_GROUP_ID],
            },
            "search": {
                "id": contract.search.id,
                "card_rarities": list(contract.search.card_rarities),
                "position": contract.search.card_position_type,
                "order": contract.search.order_type,
                "limit_count": contract.search.limit_count,
                "select_random_all": "none: transient Playing predicate",
                "rng": "none",
            },
            "force_play_handoff": {
                "effect_id": force_contract.row.effect_id,
                "search_id": force_contract.row.search_id,
                "position": force_contract.search.card_position_type,
                "pick_range": force_contract.row.pick_range_type,
                "pick_count_type": force_contract.row.pick_count_type,
                "pick_count_min": force_contract.row.pick_count_min,
                "pick_count_max": force_contract.row.pick_count_max,
                "order": "Deck source order followed by Grave source order",
                "rng": "none",
            },
            "versions": [
                {
                    "card_id": master.card.id,
                    "upgrade": master.card.upgrade,
                    "ordered_effect_ids": [
                        slot.effect_id for slot in master.effect_slots
                    ],
                }
                for master in masters
            ],
        },
        "native": {
            "android_version": ANDROID_VERSION,
            "sources": [
                "_research/android/game-v3.2.3/native-analysis/create-executor-mapping.json",
                "_research/android/game-v3.2.3/cpp2il-isil/IsilDump/Assembly-CSharp/Campus/InGame/Exam/ExamSequence.txt",
                "_research/android/game-v3.2.3/cpp2il-plugin/DiffableCs/Assembly-CSharp/Campus/Ingame/Exam/ExamStatusEffectCollection.cs",
                "_research/il2cpp/B6120AF66E75E72757E3D4086E2C48D0E7D31DA9F3A3DFBCDC7B3ED5BFFD61DA/targeted-metadata-index.json",
            ],
            "addresses": dict(ANDROID_NATIVE_ADDRESSES),
            "executor_shape": {
                "type": "Campus.InGame.Exam.SearchEffectPlayCountBuffEffectExecutor",
                "fields": ["_value", "_turn", "_count", "_search"],
                "ctor_parameters": 1,
                "execute_parameters": 1,
            },
            "status_shape": {
                "type": "Campus.InGame.Exam.PlayCountBuffStatusEffect",
                "fields": ["_count", "_searchId", "_descriptions"],
                "ctor_parameters": 4,
                "spend_count_parameters": 0,
            },
            "collection_shape": {
                "try_add_parameters": 5,
                "get_parameters": 1,
                "use_parameters": 1,
                "active_list_fields": ["_effectList", "_recentlyUseEffectUidList"],
            },
            "verified_semantics": {
                "status_fields": "value1 -> limit/PlayCount; effectCount -> total status count; effectTurn=-1 -> permanent; search -> IProduceCardSearch",
                "stack": "generic AddStatus appends duplicate matching layers; no PlayCount-specific merge/overwrite",
                "use": "newest indexed matching layer; AddRecentlyUseEffectUidList; SpendCount once; remove at zero",
                "outer_repeat": "N additional normal direct-effect passes; total N+1",
                "once_slots": "pass zero keeps all slots; later passes skip isOncePlayEffect",
                "callsite": "ExecuteCardCommandImpl at 0x7ECEC00 calls UsePlayCountBuff once before direct-effect iteration",
                "settlement": "PlayCardCount, history/UseCardAfterCheck, MovePlayCard, global/per-turn counts and post-move listeners remain exactly once",
            },
        },
        "queue_contract": {
            "parent_order": list(ORDERED_AFFECTED_CARD_EFFECT_IDS),
            "handoff_order": list(FORCE_PLAY_HANDOFF_ORDER),
            "guid_policy": "snapshot GUID/card at search; ignore stale source index; rematch unique live GUID across Hand/Deck/Grave/Hold/Lost at execution",
            "missing_guid": "fail closed before status spend or card side effects",
            "zero_one_many": "0 -> no command/Use; 1 -> one explicit Select command/one Use; many -> explicit candidate branch per selected GUID, no RNG",
            "forced_policy": "isConsumeCost=false, isUsePlayableCount=false, isManual=false; same GUID receives the already-installed status at live Playing stage",
            "status_lifetime": "effectCount=1 total accepted use, turn=-1 permanent; no turn reset",
        },
        "accounting": accounting.to_dict(),
    }


__all__ = [
    "AFFECTED_CARD_VERSION_COUNT",
    "AFFECTED_CARD_VERSIONS",
    "ANDROID_EXECUTE_CARD_COMMAND_IMPL",
    "ANDROID_NATIVE_ADDRESSES",
    "ANDROID_VERSION",
    "CARD_ID",
    "CARD_NAME",
    "CARD_UPGRADES",
    "CATEGORY_MENTAL_SKILL",
    "COST_UNKNOWN",
    "EFFECT_BY_ID",
    "EFFECT_GROUP_ID",
    "EFFECT_ID",
    "EFFECT_TYPE",
    "EFFECT_TYPE_VALUE",
    "EXACT_EFFECT_ROW",
    "FORCE_PLAY_EFFECT_ID",
    "FORCE_PLAY_EFFECT_TYPE",
    "FORCE_PLAY_HANDOFF_ORDER",
    "FORCE_PLAY_PICK_COUNT",
    "FORCE_PLAY_PICK_RANGE",
    "FORCE_PLAY_POSITION",
    "FORCE_PLAY_SEARCH_ID",
    "MOVE_LOST",
    "NATIVE_CARD_TRANSACTION_ORDER",
    "ORDERED_AFFECTED_CARD_EFFECT_IDS",
    "PLAN_TYPE_PLAN2",
    "RARITY_SSR",
    "SEARCH_ID",
    "SEARCH_RARITIES",
    "CardPlayEffectSlot",
    "CardSearchBuffCardMaster",
    "CardSearchBuffMatch",
    "CardSearchEffectPlayCountBuffAdapter",
    "CardSearchEffectPlayCountBuffContract",
    "CardSearchEffectPlayCountBuffEffectRow",
    "CardSearchEffectPlayCountBuffError",
    "CardSearchEffectPlayCountBuffInputError",
    "CardSearchEffectPlayCountBuffResolutionError",
    "CardSearchEffectPlayCountBuffUnresolvedInput",
    "ForcePlayHandoffContract",
    "MasterCardShape",
    "Plan2CardPlayCountBuffExecution",
    "Plan2CardPlayCountBuffHookInput",
    "Plan2CardSearchPlayCountBuffError",
    "Plan2CardSearchPlayCountBuffHandoff",
    "Plan2CardSearchPlayCountBuffInputError",
    "Plan2CardSearchPlayCountBuffResolutionError",
    "Plan2ForcePlayBuffExecution",
    "Plan2ForcePlayBuffQueueExecution",
    "Plan2ForcePlayBuffStage",
    "Plan2ForcePlayEffectRow",
    "Plan2ForcePlayHandoffCommand",
    "PlayCountBuffAffectedCardVersion",
    "PlayCountBuffAccounting",
    "PlayCountBuffRuntime",
    "PlayCountBuffStatus",
    "PlayCountBuffStatusInstall",
    "PlayCountBuffTurnStartTransition",
    "PlayCountBuffUse",
    "advance_play_count_buff_turn_start",
    "build_force_play_handoff",
    "chain_play_count_buff_to_force_play",
    "contract_for_card_search_effect_play_count_buff",
    "direct_effect_passes",
    "execute_card_search_effect_play_count_buff",
    "execute_force_play_buff_handoff",
    "execute_force_play_buff_once",
    "get_play_count_buff",
    "handoff_from_plan2_force_play",
    "handoff_play_count_buff_to_force_play",
    "install_card_search_effect_play_count_buff",
    "load_affected_card_versions",
    "load_card_search_buff_card_master",
    "load_card_search_effect_play_count_buff_contract",
    "load_force_play_handoff_contract",
    "load_master_card_search_effect_play_count_buff_rows",
    "match_playing_card_search",
    "matches_affected_card_master",
    "matches_affected_card_version",
    "native_audit_to_dict",
    "probe_accounting",
    "resolve_card_search_effect_play_count_buff_contract",
    "stage_force_play_guid_for_buff",
    "try_load_force_play_handoff_contract",
    "try_resolve_card_search_effect_play_count_buff_contract",
    "use_play_count_buff",
    "use_matching_play_count_buff",
]
