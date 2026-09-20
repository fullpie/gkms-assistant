"""Standalone Android v3.2.3 lesson and Encore boundaries for one Plan 3 card.

The four versions of ``p_card-03-ido-3_197`` share two previously unsupported
effect families.  ``ExamMultipleEnthusiasticLesson`` is executable here all
the way through its repeated score differences.  ``ExamStatusEnchantEncore``
is executable through listener installation, count scheduling, and creation
of the target-self force-play command.  The later arbitrary-card transaction
remains an explicit core hook and never mutates :class:`Plan3NativeState` here.

Every public planner is fail closed.  An unknown Master shape, missing native
card identity, unavailable once-play state, or invalid UID returns no partial
difference, no queued command, and no counter spend.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Any, Mapping

from .card_search import ProduceCardSearchRule, load_produce_card_search
from .master_db import DEFAULT_DATABASE
from .native_exam_formula import (
    AddingParameterAdditionalData,
    AddingParameterSettings,
    AddingParameterStatus,
    NativeFormulaDomainError,
    ParameterApplication,
    ParameterApplicationStatus,
    apply_parameter_add,
    calculate_adding_parameter,
    f32,
    permille_to_f32,
)
from .plan3_engine import (
    ActivePlan3StatusEnchant,
    Plan3Effect,
    Plan3StatusEnchantRule,
    load_plan3_effect,
    load_plan3_status_enchant,
)
from .plan3_force_play_card_search import (
    ForcePlayDifference,
    ForcePlayQueuedCommand,
    ForcePlayCardSearchResolutionError,
    load_force_play_target_card_master,
)
from .plan3_native_state import Plan3NativeCard, Plan3NativeState


CARD_ID = "p_card-03-ido-3_197"
PLAN_TYPE = "ProducePlanType_Plan3"
CATEGORY = "ProduceCardCategory_ActiveSkill"
COST_TYPE = "ExamCostType_Unknown"
PLAY_TRIGGER_ID = "e_trigger-none-preservation_change_count_up-2"
MOVE_LOST = "ProduceCardMovePositionType_Lost"

LESSON_EFFECT_TYPE = "ProduceExamEffectType_ExamMultipleEnthusiasticLesson"
LESSON_EFFECT_TYPE_VALUE = 183
ENCORE_EFFECT_TYPE = "ProduceExamEffectType_ExamStatusEnchantEncore"
ENCORE_EFFECT_TYPE_VALUE = 219
STATUS_ENCHANT_BLOCK_GATE_EFFECT_TYPE_VALUE = 22
TRIGGER_STATUS_TYPE_VALUE = 31
FORCE_PLAY_EFFECT_TYPE = "ProduceExamEffectType_ExamForcePlayCardSearch"

LESSON_EFFECT_IDS: tuple[str, ...] = (
    "e_effect-exam_multiple_enthusiastic_lesson-0001-1000-01",
    "e_effect-exam_multiple_enthusiastic_lesson-0003-1000-01",
    "e_effect-exam_multiple_enthusiastic_lesson-0005-1000-01",
)
ENCORE_EFFECT_ID = (
    "e_effect-exam_status_enchant_encore-0001-03-inf-"
    "enchant-p_card-03-ido-3_197-enc01"
)
ENCORE_STATUS_ID = "enchant-p_card-03-ido-3_197-enc01"
ENCORE_TRIGGER_ID = "e_trigger-exam_end_turn-remaining_turn-3"
ENCORE_FORCE_PLAY_EFFECT_ID = (
    "e_effect-exam_force_play_card_search-p_card_search-target_is_self-"
    "exam_status_enchant_encore-all-1_1"
)
ENCORE_SEARCH_ID = "p_card_search-target_is_self-exam_status_enchant_encore"
END_TURN_PHASE = "ProduceExamPhaseType_ExamEndTurn"
REMAINING_TURN_FIELD = "ProduceExamFieldStatusType_RemainingTurn"
TARGET_POSITION = "ProduceCardPositionType_Target"

ANDROID_VERSION = "Android v3.2.3"
ANDROID_LESSON_CTOR = "0x7E84D74"
ANDROID_LESSON_EXECUTE = "0x7E84EEC"
ANDROID_ENCORE_CTOR = "0x7E903E4"
ANDROID_ENCORE_EXECUTE = "0x7E90600"
ANDROID_TRY_ADD_TRIGGER_STATUS = "0x7E8FEE0"
ANDROID_TRIGGER_STATUS_SPEND_COUNT = "0x7EAE70C"

DIRECT_CARD_TRANSACTION_ORDER: tuple[str, ...] = (
    "snapshot-exam-card-play-status-and-passive-listeners",
    "consume-card-cost",
    "run-snapshotted-card-play-listeners",
    "run-direct-effects-in-master-order",
    "after-each-effect-insert-difference-trigger-commands-depth-first",
    "after-each-effect-run-post-effect-and-difference-callbacks",
    "run-user-card-after-check",
    "move-playing-card-to-lost",
    "snapshot-and-run-exam-card-play-after-listeners",
)

ENCORE_TRIGGER_ORDER: tuple[str, ...] = (
    "snapshot-current-end-turn-trigger-status-candidates",
    "evaluate-remaining-turn-at-most-three",
    "spend-total-and-per-turn-count-before-nested-effects",
    "run-nested-target-self-force-play-effect",
    "queue-force-use-command",
    "append-card-force-play-difference-after-command",
    "insert-difference-trigger-commands-depth-first",
    "run-post-effect-and-difference-callbacks",
    "remove-exhausted-card-direct-listener-after-trigger-batch",
)

REPLAY_CORE_HOOKS: tuple[str, ...] = (
    "native target-card history binding from trigger UID",
    "per-card isOncePlayEffect consumption state",
    "live GUID-to-zone resolution when queued UsePool executes",
    "live IsPlayable evaluation",
    "normal card passive/status/card-status listener snapshots",
    "ordered direct effects with recursive difference insertion",
    "Playing transient ownership and Lost settlement",
    "card/global play-count synchronization and callbacks",
)


class EncoreLessonError(ValueError):
    """Base error carrying a stable fail-closed code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


class EncoreLessonResolutionError(EncoreLessonError):
    """Static Master/native evidence does not prove the supplied shape."""


class EncoreLessonInputError(EncoreLessonError):
    """Runtime input cannot be represented by the standalone boundary."""


def _strict_positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EncoreLessonInputError("invalid-positive-int", label)
    return value


@dataclass(frozen=True, slots=True)
class AffectedCardVersion:
    upgrade: int
    stamina: int
    lesson_effect_id: str
    preservation_effect_id: str
    ordered_effect_ids: tuple[str, ...]


def _card_version(upgrade: int, stamina: int, lesson: str, preservation: str) -> AffectedCardVersion:
    return AffectedCardVersion(
        upgrade,
        stamina,
        lesson,
        preservation,
        (
            lesson,
            "e_effect-exam_block-0001",
            preservation,
            ENCORE_EFFECT_ID,
        ),
    )


AFFECTED_CARD_VERSIONS: tuple[AffectedCardVersion, ...] = (
    _card_version(0, 6, LESSON_EFFECT_IDS[0], "e_effect-exam_preservation-0001"),
    _card_version(1, 5, LESSON_EFFECT_IDS[0], "e_effect-exam_preservation-0002"),
    _card_version(2, 4, LESSON_EFFECT_IDS[1], "e_effect-exam_preservation-0002"),
    _card_version(3, 4, LESSON_EFFECT_IDS[2], "e_effect-exam_preservation-0002"),
)
CARD_VERSION_BY_UPGRADE: Mapping[int, AffectedCardVersion] = MappingProxyType(
    {item.upgrade: item for item in AFFECTED_CARD_VERSIONS}
)


@dataclass(frozen=True, slots=True)
class MultipleEnthusiasticLessonContract:
    effect_id: str
    value: int
    enthusiastic_permille: int
    count: int
    turn: int = 0


LESSON_EXPECTED: Mapping[str, tuple[int, int, int, int]] = MappingProxyType(
    {
        LESSON_EFFECT_IDS[0]: (1, 1000, 1, 0),
        LESSON_EFFECT_IDS[1]: (3, 1000, 1, 0),
        LESSON_EFFECT_IDS[2]: (5, 1000, 1, 0),
    }
)


def resolve_multiple_enthusiastic_lesson(
    effect: Plan3Effect,
) -> MultipleEnthusiasticLessonContract:
    if not isinstance(effect, Plan3Effect):
        raise EncoreLessonInputError("invalid-lesson-effect")
    expected = LESSON_EXPECTED.get(effect.id)
    if expected is None:
        raise EncoreLessonResolutionError("unsupported-lesson-effect", effect.id)
    actual = (effect.value1, effect.value2, effect.effect_count, effect.effect_turn)
    if effect.effect_type != LESSON_EFFECT_TYPE or actual != expected:
        raise EncoreLessonResolutionError("lesson-master-shape-drift", effect.id)
    if any(
        (
            effect.status_enchant_id,
            effect.status_enchant,
            effect.chain_effect_id,
            effect.chain_effect_ids,
            effect.chain_effect,
            effect.trigger,
            effect.once,
            effect.card_move_rule,
        )
    ):
        raise EncoreLessonResolutionError("lesson-master-extra-fields", effect.id)
    return MultipleEnthusiasticLessonContract(effect.id, *expected)


def load_multiple_enthusiastic_lesson(
    effect_id: str, database: Path = DEFAULT_DATABASE
) -> MultipleEnthusiasticLessonContract:
    try:
        effect = load_plan3_effect(effect_id, Path(database))
    except (KeyError, OSError, sqlite3.Error, ValueError) as error:
        raise EncoreLessonResolutionError(
            "lesson-master-unavailable", effect_id
        ) from error
    return resolve_multiple_enthusiastic_lesson(effect)


@dataclass(frozen=True, slots=True)
class MultipleEnthusiasticLessonInput:
    calculation_status: AddingParameterStatus
    settings: AddingParameterSettings
    application_status: ParameterApplicationStatus
    is_buff_active: bool = True


@dataclass(frozen=True, slots=True)
class MultipleEnthusiasticLessonResult:
    contract: MultipleEnthusiasticLessonContract
    supplied: MultipleEnthusiasticLessonInput
    resolved: bool
    reason: str
    enthusiastic_rate: float
    calculated_parameter: int | None
    applications: tuple[ParameterApplication, ...]
    after: ParameterApplicationStatus
    calculate_call_count: int
    rng_consumed: bool
    operations: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.resolved and (
            self.applications or self.calculated_parameter is not None
        ):
            raise EncoreLessonInputError("unresolved-lesson-has-output")


def execute_multiple_enthusiastic_lesson(
    contract: MultipleEnthusiasticLessonContract,
    supplied: MultipleEnthusiasticLessonInput,
) -> MultipleEnthusiasticLessonResult:
    """Execute CalculateAddingParameter once, then AddParameter ``count`` times."""

    if not isinstance(contract, MultipleEnthusiasticLessonContract):
        raise EncoreLessonInputError("invalid-lesson-contract")
    if not isinstance(supplied, MultipleEnthusiasticLessonInput):
        raise EncoreLessonInputError("invalid-lesson-input")
    rate = f32(0.0)
    try:
        rate = f32(f32(1.0) + permille_to_f32(contract.enthusiastic_permille))
        calculated = calculate_adding_parameter(
            contract.value,
            is_buff_active=supplied.is_buff_active,
            status=supplied.calculation_status,
            settings=supplied.settings,
            additional=AddingParameterAdditionalData(
                multiple_enthusiastic_rate=rate
            ),
        )
        current = supplied.application_status
        applications: list[ParameterApplication] = []
        for _ in range(contract.count):
            application = apply_parameter_add(calculated, status=current)
            applications.append(application)
            current = replace(
                current,
                judge_parameter=application.after,
                current_turn_total_add_parameter=(
                    application.current_turn_total_add_parameter
                ),
                judge_parameter_vocal=application.judge_parameter_vocal,
                judge_parameter_dance=application.judge_parameter_dance,
                judge_parameter_visual=application.judge_parameter_visual,
            )
    except (NativeFormulaDomainError, OverflowError, TypeError, ValueError) as error:
        return MultipleEnthusiasticLessonResult(
            contract,
            supplied,
            False,
            f"native-formula-domain:{error}",
            rate,
            None,
            (),
            supplied.application_status,
            0,
            False,
            ("fail-closed-before-score-difference",),
        )
    return MultipleEnthusiasticLessonResult(
        contract,
        supplied,
        True,
        "",
        rate,
        calculated,
        tuple(applications),
        current,
        1,
        False,
        (
            "construct-additional-data-with-native-1.0-defaults",
            "float-from-permille",
            "float32-add-one-and-store-multiple-enthusiastic-rate",
            "calculate-adding-parameter-once",
            *("add-parameter-and-append-difference" for _ in applications),
        ),
    )


@dataclass(frozen=True, slots=True)
class EncoreContract:
    effect: Plan3Effect
    rule: Plan3StatusEnchantRule
    search: ProduceCardSearchRule
    unresolved_reasons: tuple[str, ...]

    @property
    def executable(self) -> bool:
        return not self.unresolved_reasons


def _encore_shape_errors(
    effect: Plan3Effect,
    rule: Plan3StatusEnchantRule,
    search: ProduceCardSearchRule,
) -> tuple[str, ...]:
    errors: list[str] = []
    if effect.id != ENCORE_EFFECT_ID:
        errors.append("effect-id")
    if effect.effect_type != ENCORE_EFFECT_TYPE:
        errors.append("effect-type")
    if (effect.value1, effect.value2, effect.effect_count, effect.effect_turn) != (
        1,
        0,
        3,
        -1,
    ):
        errors.append("effect-values")
    if effect.status_enchant_id != ENCORE_STATUS_ID or effect.status_enchant != rule:
        errors.append("effect-status-enchant")
    if any(
        (
            effect.chain_effect_id,
            effect.chain_effect_ids,
            effect.chain_effect,
            effect.trigger,
            effect.once,
            effect.card_move_rule,
        )
    ):
        errors.append("effect-extra-fields")
    trigger = rule.trigger
    if rule.id != ENCORE_STATUS_ID:
        errors.append("status-id")
    if (
        trigger.id != ENCORE_TRIGGER_ID
        or trigger.phase_types != (END_TURN_PHASE,)
        or trigger.phase_values
        or trigger.field_check_types
        or trigger.field_types != (REMAINING_TURN_FIELD,)
        or trigger.field_values != (3,)
        or trigger.field_card_search_ids
        or trigger.produce_card_search_id
        or trigger.upper_search_count != 0
        or trigger.lower_search_count != 0
        or trigger.card_move_position_type != "ProduceCardMovePositionType_Unknown"
        or trigger.effect_types
        or trigger.lesson_type != "ProduceStepLessonType_Unknown"
    ):
        errors.append("trigger-shape")
    if len(rule.effects) != 1:
        errors.append("nested-effect-count")
    else:
        nested = rule.effects[0]
        if (
            nested.id != ENCORE_FORCE_PLAY_EFFECT_ID
            or nested.effect_type != FORCE_PLAY_EFFECT_TYPE
            or (nested.value1, nested.value2, nested.effect_count, nested.effect_turn)
            != (0, 0, 0, 0)
            or nested.status_enchant_id
            or nested.chain_effect_id
            or nested.chain_effect_ids
            or nested.chain_effect is not None
            or nested.trigger is not None
            or nested.once
            or nested.card_move_rule is not None
        ):
            errors.append("nested-effect-shape")
    expected_search = (
        ENCORE_SEARCH_ID,
        (),
        (),
        (),
        "ProducePlanType_Unknown",
        (),
        "ProduceCardSearchStatusType_Unknown",
        "ProduceCardOrderType_Unknown",
        TARGET_POSITION,
        "",
        "",
        0,
        "ConditionMinMaxType_Unknown",
        0,
        0,
        "ProduceExamEffectType_Unknown",
        (),
        True,
        "",
        "ExamCostType_Unknown",
        False,
    )
    actual_search = (
        search.id,
        search.card_rarities,
        search.produce_card_ids,
        search.upgrade_counts,
        search.plan_type,
        search.card_categories,
        search.card_status_type,
        search.order_type,
        search.card_position_type,
        search.card_search_tag,
        search.produce_card_random_pool_id,
        search.limit_count,
        search.stamina_min_max_type,
        search.stamina_min,
        search.stamina_max,
        search.exam_effect_type,
        search.effect_group_ids,
        search.is_self,
        search.produce_card_pool_id,
        search.cost_type,
        search.is_customized,
    )
    if actual_search != expected_search:
        errors.append("target-self-search-shape")
    return tuple(errors)


def load_encore_contract(database: Path = DEFAULT_DATABASE) -> EncoreContract:
    try:
        effect = load_plan3_effect(ENCORE_EFFECT_ID, Path(database))
        rule = load_plan3_status_enchant(ENCORE_STATUS_ID, Path(database))
        search = load_produce_card_search(ENCORE_SEARCH_ID, Path(database))
    except (KeyError, OSError, sqlite3.Error, ValueError) as error:
        raise EncoreLessonResolutionError("encore-master-unavailable") from error
    return EncoreContract(effect, rule, search, _encore_shape_errors(effect, rule, search))


@dataclass(frozen=True, slots=True)
class EncoreInstallInput:
    playing_card: Plan3NativeCard
    status_uid: int
    existing_status_uids: tuple[int, ...] = ()
    once_effect_available: bool = True
    status_enchant_blocked: bool = False


@dataclass(frozen=True, slots=True)
class EncoreListener:
    uid: int
    captured_card: Plan3NativeCard
    active: ActivePlan3StatusEnchant
    is_encore_enchant: bool = True
    origin_type: str = "StatusEnchant"
    origin_type_value: int = 1
    status_type_value: int = TRIGGER_STATUS_TYPE_VALUE
    effect_ids: tuple[str, ...] | None = None

    @property
    def captured_guid(self) -> str:
        return self.captured_card.guid

    @property
    def total_remaining(self) -> int:
        return self.active.max_uses - self.active.uses

    @property
    def per_turn_remaining(self) -> int:
        return self.active.max_uses_per_turn - self.active.uses_this_turn


@dataclass(frozen=True, slots=True)
class EncoreInstallDifference:
    effect_type: str
    effect_type_value: int
    preview_value: int
    current_value: int
    is_consume: bool
    trigger_status_uid: int
    appended_after_listener_install: bool = True


@dataclass(frozen=True, slots=True)
class EncoreInstallResult:
    contract: EncoreContract
    supplied: EncoreInstallInput
    resolved: bool
    reason: str
    listener: EncoreListener | None
    differences: tuple[EncoreInstallDifference, ...]
    operations: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.resolved and (self.listener is not None or self.differences):
            raise EncoreLessonInputError("unresolved-install-has-output")


def _failed_install(
    contract: EncoreContract, supplied: EncoreInstallInput, reason: str
) -> EncoreInstallResult:
    return EncoreInstallResult(
        contract,
        supplied,
        False,
        reason,
        None,
        (),
        ("fail-closed-before-listener-install",),
    )


def install_encore_listener(
    contract: EncoreContract, supplied: EncoreInstallInput
) -> EncoreInstallResult:
    """Model TryAddTriggerEffectStatus and its success-only difference."""

    if not isinstance(contract, EncoreContract):
        raise EncoreLessonInputError("invalid-encore-contract")
    if not isinstance(supplied, EncoreInstallInput):
        raise EncoreLessonInputError("invalid-encore-install-input")
    if not contract.executable:
        return _failed_install(
            contract, supplied, f"encore-contract-unresolved:{contract.unresolved_reasons[0]}"
        )
    if not isinstance(supplied.playing_card, Plan3NativeCard):
        return _failed_install(contract, supplied, "playing-card-unavailable")
    expected_card = CARD_VERSION_BY_UPGRADE.get(
        supplied.playing_card.effective_upgrade
    )
    if supplied.playing_card.card_id != CARD_ID or expected_card is None:
        return _failed_install(contract, supplied, "playing-card-outside-exact-bundle")
    try:
        uid = _strict_positive_int(supplied.status_uid, "status_uid")
    except EncoreLessonInputError as error:
        return _failed_install(contract, supplied, error.code)
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in supplied.existing_status_uids
    ):
        return _failed_install(contract, supplied, "invalid-existing-status-uid")
    if uid in supplied.existing_status_uids:
        return _failed_install(contract, supplied, "status-uid-collision")
    if not supplied.once_effect_available:
        return _failed_install(contract, supplied, "once-play-effect-already-consumed")
    if supplied.status_enchant_blocked:
        return _failed_install(contract, supplied, "status-enchant-add-blocked")
    listener = EncoreListener(
        uid,
        supplied.playing_card,
        ActivePlan3StatusEnchant(
            instance_id=f"encore-status-uid:{uid}",
            source_id=supplied.playing_card.card_id,
            rule=contract.rule,
            max_uses=3,
            max_uses_per_turn=1,
            remaining_turns=-1,
            is_item_direct=False,
        ),
    )
    difference = EncoreInstallDifference(
        ENCORE_EFFECT_TYPE,
        ENCORE_EFFECT_TYPE_VALUE,
        0,
        0,
        False,
        uid,
    )
    return EncoreInstallResult(
        contract,
        supplied,
        True,
        "",
        listener,
        (difference,),
        (
            f"check-block-add-status-effect-type:{STATUS_ENCHANT_BLOCK_GATE_EFFECT_TYPE_VALUE}",
            "copy-status-effect-list-in-source-order",
            "normalize-positive-total-count-else-minus-one",
            "normalize-positive-per-turn-count-else-minus-one",
            "construct-non-unique-trigger-effect-status",
            f"capture-playing-card-guid:{supplied.playing_card.guid}",
            "set-is-encore-enchant",
            f"append-trigger-status-difference:{uid}",
            "effect-wrapper-inserts-difference-results-and-callbacks",
        ),
    )


@dataclass(frozen=True, slots=True)
class EncoreEndTurnInput:
    remaining_turns: int
    once_effect_consumed: bool | None
    phase: str = END_TURN_PHASE


@dataclass(frozen=True, slots=True)
class EncoreReplayCommand:
    force_play: ForcePlayQueuedCommand
    listener_uid: int
    captured_guid: str
    expected_live_zone: str = "lost"
    target_search_position_type: str = TARGET_POSITION
    nested_effect_id: str = ENCORE_FORCE_PLAY_EFFECT_ID
    current_play_effect_ids: tuple[str, ...] = ()
    expected_executed_direct_effect_ids: tuple[str, ...] = ()
    skipped_once_effect_ids: tuple[str, ...] = (ENCORE_EFFECT_ID,)
    skips_consumed_once_installer: bool = True
    rng_consumed: bool = False
    core_hook_requirements: tuple[str, ...] = REPLAY_CORE_HOOKS


@dataclass(frozen=True, slots=True)
class EncoreTriggerResult:
    before_state: Plan3NativeState
    after_state: Plan3NativeState
    before_listener: EncoreListener
    after_listener: EncoreListener
    supplied: EncoreEndTurnInput
    resolved: bool
    triggered: bool
    reason: str
    commands: tuple[EncoreReplayCommand, ...]
    differences: tuple[ForcePlayDifference, ...]
    remove_after_trigger_batch: bool
    operations: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.after_state is not self.before_state:
            raise EncoreLessonInputError("encore-planner-mutated-native-state")
        if not self.resolved and (
            self.triggered
            or self.commands
            or self.differences
            or self.after_listener != self.before_listener
        ):
            raise EncoreLessonInputError("unresolved-trigger-has-output")

    @property
    def rng_consumed(self) -> bool:
        return False

    @property
    def core_hook_required(self) -> bool:
        return bool(self.commands)


def _trigger_result(
    state: Plan3NativeState,
    listener: EncoreListener,
    supplied: EncoreEndTurnInput,
    *,
    resolved: bool,
    triggered: bool = False,
    reason: str = "",
    after_listener: EncoreListener | None = None,
    commands: tuple[EncoreReplayCommand, ...] = (),
    differences: tuple[ForcePlayDifference, ...] = (),
    remove_after_trigger_batch: bool = False,
    operations: tuple[str, ...] = (),
) -> EncoreTriggerResult:
    return EncoreTriggerResult(
        state,
        state,
        listener,
        after_listener or listener,
        supplied,
        resolved,
        triggered,
        reason,
        commands,
        differences,
        remove_after_trigger_batch,
        operations,
    )


def plan_encore_end_turn_replay(
    state: Plan3NativeState,
    listener: EncoreListener,
    supplied: EncoreEndTurnInput,
    *,
    database: Path = DEFAULT_DATABASE,
) -> EncoreTriggerResult:
    """Spend one listener use and queue the captured card's forced replay."""

    if not isinstance(state, Plan3NativeState):
        raise EncoreLessonInputError("invalid-native-state")
    if not isinstance(listener, EncoreListener):
        raise EncoreLessonInputError("invalid-encore-listener")
    if not isinstance(supplied, EncoreEndTurnInput):
        raise EncoreLessonInputError("invalid-end-turn-input")
    if listener.active.rule.id != ENCORE_STATUS_ID or not listener.is_encore_enchant:
        return _trigger_result(
            state, listener, supplied, resolved=False, reason="unknown-listener-shape",
            operations=("fail-closed-before-count-spend",),
        )
    if supplied.phase != END_TURN_PHASE:
        return _trigger_result(
            state, listener, supplied, resolved=True, reason="phase-not-matched",
            operations=("listener-not-candidate-for-phase",),
        )
    if (
        isinstance(supplied.remaining_turns, bool)
        or not isinstance(supplied.remaining_turns, int)
        or supplied.remaining_turns < 0
    ):
        return _trigger_result(
            state, listener, supplied, resolved=False, reason="invalid-remaining-turns",
            operations=("fail-closed-before-count-spend",),
        )
    if supplied.remaining_turns > 3:
        return _trigger_result(
            state, listener, supplied, resolved=True, reason="remaining-turn-not-matched",
            operations=("evaluate-remaining-turn-at-most-three:false",),
        )
    if listener.active.uses >= listener.active.max_uses:
        return _trigger_result(
            state, listener, supplied, resolved=True, reason="total-count-exhausted",
            operations=("listener-not-candidate-total-count-exhausted",),
        )
    if listener.active.uses_this_turn >= listener.active.max_uses_per_turn:
        return _trigger_result(
            state, listener, supplied, resolved=True, reason="per-turn-count-exhausted",
            operations=("listener-not-candidate-per-turn-count-exhausted",),
        )
    if supplied.once_effect_consumed is not True:
        return _trigger_result(
            state,
            listener,
            supplied,
            resolved=False,
            reason=(
                "once-play-state-unavailable"
                if supplied.once_effect_consumed is None
                else "once-play-installer-not-consumed"
            ),
            operations=("fail-closed-before-count-spend",),
        )
    matches = tuple(
        (index, card)
        for index, card in enumerate(state.lost)
        if card.guid == listener.captured_guid
    )
    if len(matches) != 1:
        elsewhere = tuple(
            card for card in state.all_cards if card.guid == listener.captured_guid
        )
        return _trigger_result(
            state,
            listener,
            supplied,
            resolved=False,
            reason=("captured-guid-not-in-lost" if elsewhere else "captured-guid-missing"),
            operations=("fail-closed-before-count-spend",),
        )
    source_index, card = matches[0]
    if (
        card.card_id != listener.captured_card.card_id
        or card.effective_upgrade != listener.captured_card.effective_upgrade
    ):
        return _trigger_result(
            state, listener, supplied, resolved=False,
            reason="captured-card-identity-drift",
            operations=("fail-closed-before-count-spend",),
        )
    try:
        target_master = load_force_play_target_card_master(card, Path(database))
    except ForcePlayCardSearchResolutionError as error:
        return _trigger_result(
            state, listener, supplied, resolved=False,
            reason=f"{error.code}:{error.detail}" if error.detail else error.code,
            operations=("fail-closed-before-count-spend",),
        )
    if target_master.has_recursive_force_play:
        # The native ForcePlay predicate excludes cards whose *own current play
        # effect list* contains enum 24.  The listener's nested enum 24 is not
        # part of this card's list and therefore does not exclude this bundle.
        return _trigger_result(
            state, listener, supplied, resolved=False,
            reason="captured-card-recursive-force-play",
            operations=("fail-closed-before-count-spend",),
        )
    expected_card = CARD_VERSION_BY_UPGRADE.get(card.effective_upgrade)
    if (
        card.card_id != CARD_ID
        or expected_card is None
        or target_master.play_effect_ids != expected_card.ordered_effect_ids
    ):
        return _trigger_result(
            state, listener, supplied, resolved=False,
            reason="captured-card-play-effect-order-drift",
            operations=("fail-closed-before-count-spend",),
        )
    if target_master.base_move_position_type != MOVE_LOST:
        return _trigger_result(
            state, listener, supplied, resolved=False,
            reason="captured-card-move-destination-drift",
            operations=("fail-closed-before-count-spend",),
        )
    spent_active = replace(
        listener.active,
        uses=listener.active.uses + 1,
        uses_this_turn=listener.active.uses_this_turn + 1,
    )
    after_listener = replace(listener, active=spent_active)
    force_play = ForcePlayQueuedCommand(
        0,
        card.guid,
        card,
        "target-captured-currently-lost",
        TARGET_POSITION,
        source_index,
        target_master.base_move_position_type,
        listener.uid,
    )
    command = EncoreReplayCommand(
        force_play,
        listener.uid,
        card.guid,
        current_play_effect_ids=target_master.play_effect_ids,
        expected_executed_direct_effect_ids=tuple(
            effect_id
            for effect_id in target_master.play_effect_ids
            if effect_id != ENCORE_EFFECT_ID
        ),
    )
    difference = ForcePlayDifference(0, card.guid)
    exhausted = spent_active.uses >= spent_active.max_uses
    return _trigger_result(
        state,
        listener,
        supplied,
        resolved=True,
        triggered=True,
        after_listener=after_listener,
        commands=(command,),
        differences=(difference,),
        remove_after_trigger_batch=exhausted,
        operations=(
            "snapshot-current-end-turn-trigger-status-candidates",
            "evaluate-remaining-turn-at-most-three:true",
            "spend-total-and-per-turn-count-before-nested-effects",
            "resolve-target-from-captured-trigger-card-guid",
            "confirm-target-current-play-effect-list-has-no-type-24",
            "queue-use-pool-cost-false-manual-false-playable-count-false",
            "append-card-force-play-difference-after-command",
            "effect-wrapper-inserts-difference-results-and-callbacks",
        ),
    )


def reset_encore_per_turn_count(listener: EncoreListener) -> EncoreListener:
    """Mirror IncrementTurnCount: reset the per-turn remain, keep -1 lifetime."""

    if not isinstance(listener, EncoreListener):
        raise EncoreLessonInputError("invalid-encore-listener")
    return replace(
        listener,
        active=replace(
            listener.active,
            uses_this_turn=0,
            turn_count=listener.active.turn_count + 1,
            remaining_turns=-1,
        ),
    )


def validate_affected_card_versions(
    database: Path = DEFAULT_DATABASE,
) -> tuple[AffectedCardVersion, ...]:
    """Validate the four exact Master rows, including trigger and once flags."""

    with sqlite3.connect(Path(database)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT upgrade_count, plan_type, category, stamina, cost_type, "
            "cost_value, play_trigger_id, move_position_type, play_effects_json "
            "FROM card WHERE id = ? ORDER BY upgrade_count",
            (CARD_ID,),
        ).fetchall()
    if len(rows) != 4:
        raise EncoreLessonResolutionError("affected-card-version-count-drift")
    import json

    for row, expected in zip(rows, AFFECTED_CARD_VERSIONS, strict=True):
        try:
            effects = json.loads(row["play_effects_json"])
        except (TypeError, json.JSONDecodeError) as error:
            raise EncoreLessonResolutionError("affected-card-effects-invalid") from error
        actual_ids = tuple(item.get("produceExamEffectId") for item in effects)
        triggers = tuple(item.get("produceExamTriggerId") for item in effects)
        once = tuple(item.get("isOncePlayEffect") for item in effects)
        actual = (
            int(row["upgrade_count"]),
            str(row["plan_type"]),
            str(row["category"]),
            int(row["stamina"]),
            str(row["cost_type"]),
            int(row["cost_value"]),
            str(row["play_trigger_id"]),
            str(row["move_position_type"]),
            actual_ids,
            triggers,
            once,
        )
        wanted = (
            expected.upgrade,
            PLAN_TYPE,
            CATEGORY,
            expected.stamina,
            COST_TYPE,
            0,
            PLAY_TRIGGER_ID,
            MOVE_LOST,
            expected.ordered_effect_ids,
            ("", "", "e_trigger-none-concentration_up", ""),
            (False, False, False, True),
        )
        if actual != wanted:
            raise EncoreLessonResolutionError(
                "affected-card-master-shape-drift", str(expected.upgrade)
            )
    return AFFECTED_CARD_VERSIONS


def catalog_to_dict() -> dict[str, Any]:
    return {
        "card_id": CARD_ID,
        "affected_card_versions": [asdict(item) for item in AFFECTED_CARD_VERSIONS],
        "lesson": {
            "effect_type": LESSON_EFFECT_TYPE,
            "effect_type_value": LESSON_EFFECT_TYPE_VALUE,
            "effect_ids": list(LESSON_EFFECT_IDS),
            "android": {
                "constructor": ANDROID_LESSON_CTOR,
                "execute": ANDROID_LESSON_EXECUTE,
            },
            "standalone_executable": True,
        },
        "encore": {
            "effect_type": ENCORE_EFFECT_TYPE,
            "effect_type_value": ENCORE_EFFECT_TYPE_VALUE,
            "effect_id": ENCORE_EFFECT_ID,
            "status_id": ENCORE_STATUS_ID,
            "nested_effect_id": ENCORE_FORCE_PLAY_EFFECT_ID,
            "android": {
                "constructor": ANDROID_ENCORE_CTOR,
                "execute": ANDROID_ENCORE_EXECUTE,
                "try_add_trigger_status": ANDROID_TRY_ADD_TRIGGER_STATUS,
                "spend_count": ANDROID_TRIGGER_STATUS_SPEND_COUNT,
            },
            "standalone_install_and_scheduler_executable": True,
            "replay_transaction_core_hook_required": True,
        },
        "direct_card_transaction_order": list(DIRECT_CARD_TRANSACTION_ORDER),
        "encore_trigger_order": list(ENCORE_TRIGGER_ORDER),
        "replay_core_hooks": list(REPLAY_CORE_HOOKS),
    }


__all__ = [
    "AFFECTED_CARD_VERSIONS",
    "ANDROID_VERSION",
    "CARD_ID",
    "DIRECT_CARD_TRANSACTION_ORDER",
    "ENCORE_EFFECT_ID",
    "ENCORE_EFFECT_TYPE",
    "ENCORE_EFFECT_TYPE_VALUE",
    "ENCORE_FORCE_PLAY_EFFECT_ID",
    "ENCORE_STATUS_ID",
    "ENCORE_TRIGGER_ORDER",
    "EncoreContract",
    "EncoreEndTurnInput",
    "EncoreInstallDifference",
    "EncoreInstallInput",
    "EncoreInstallResult",
    "EncoreLessonError",
    "EncoreLessonInputError",
    "EncoreLessonResolutionError",
    "EncoreListener",
    "EncoreReplayCommand",
    "EncoreTriggerResult",
    "LESSON_EFFECT_IDS",
    "LESSON_EFFECT_TYPE",
    "LESSON_EFFECT_TYPE_VALUE",
    "MultipleEnthusiasticLessonContract",
    "MultipleEnthusiasticLessonInput",
    "MultipleEnthusiasticLessonResult",
    "REPLAY_CORE_HOOKS",
    "catalog_to_dict",
    "execute_multiple_enthusiastic_lesson",
    "install_encore_listener",
    "load_encore_contract",
    "load_multiple_enthusiastic_lesson",
    "plan_encore_end_turn_replay",
    "reset_encore_per_turn_count",
    "resolve_multiple_enthusiastic_lesson",
    "validate_affected_card_versions",
]
