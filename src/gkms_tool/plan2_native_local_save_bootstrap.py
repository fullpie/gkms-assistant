"""Exact, offline-only ExamSaveData bootstrap for the Plan2 native horizon.

The adapter is intentionally fail closed.  It copies only values represented
by :class:`LocalSaveExamState` and :class:`Plan2NativeHorizonState`; an active
native object which cannot be reconstructed as one of the target's typed
runtimes prevents creation of a dispatchable state.  No agent, live process,
clock, network, or runtime proxy participates in this bridge.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

import yaml

from .audition_local_save_state import (
    AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION,
    AuditionLocalSaveStateEvidence,
    LocalSaveExamCard,
    LocalSaveExamState,
)
from .audition_native_ordered_zones import (
    NATIVE_ORDERED_ZONE_SCHEMA_VERSION,
    NativeOrderedCardInstance,
    NativeOrderedZoneError,
    NativeOrderedZoneEvidenceBinding,
    NativeOrderedZoneState,
    canonical_runtime_digest_aggregate,
)
from .plan2_native_catalog_debuff import (
    ANTI_DEBUFF_STATUS_NATIVE_TYPE,
    EFFECT_ANTI_DEBUFF,
    Plan2NativeDebuffRegistry,
    Plan2NativeStatusLifetime,
)
from .plan2_debuff_recover import (
    ExamStatusEffectTargetType,
    StatusEffect,
)
from .plan2_native_catalog_effect_chains import (
    EXECUTOR_BY_TYPE as EFFECT_CHAIN_EXECUTOR_BY_TYPE,
    Plan2NativeEffectChainProgram,
    Plan2NativeEffectChainQueuedTimer,
    Plan2NativeEffectChainRuntime,
)
from .plan2_native_catalog_review_dynamic import (
    LAYER_LESSON_MULTIPLE,
    LAYER_LESSON_MULTIPLE_DOWN,
    LAYER_REVIEW_ADDITIVE,
    Plan2NativeReviewDynamicLayer,
    Plan2NativeReviewDynamicRuntime,
)
from .plan2_native_catalog_stamina import (
    Plan2StaminaModifierRuntime,
    StaminaConsumptionDownFixLayer,
    StaminaConsumptionDownLayer,
)
from .plan2_native_catalog_status_enchant import (
    PHASE_END_TURN,
    PHASE_CARD_PLAY,
    PHASE_CARD_PLAY_AFTER,
    PHASE_START_PLAY,
    PHASE_START_TURN,
    PHASE_STATUS_CHANGE,
    PHASE_STAMINA_REDUCE_CARD,
    PHASE_TURN_TIMER,
    PHASE_PLAY_COUNT_INTERVAL,
    EFFECT_STATUS_ENCHANT,
    load_plan2_native_status_enchant_installer_effect,
    Plan2NativeStatusEnchantListener,
    Plan2NativeStatusEnchantRuntime,
)
from .plan2_native_catalog_status_enchant_triggered import (
    Plan2TriggeredReviewStatusRuntime,
)
from .plan2_core_runtime import (
    install_plan2_end_turn_remaining_three_exact_listener,
)
from .plan2_native_horizon import (
    PHASE_MAIN,
    PLAN2_NATIVE_HORIZON_SCHEMA_VERSION,
    Plan2NativeBlocker,
    Plan2NativeDrinkRuntime,
    Plan2NativeHorizonState,
    Plan2NativeProgramCatalog,
)
from .exam_hand_add_support_runtime import (
    ExamHandAddSupportRuntimeError,
    ExamHandAddSupportRuntimeProjection,
    project_exam_hand_add_support_runtime,
)
from .plan2_native_item_runtime import (
    _SERIALIZED_EFFECT_ENUM,
    _compile_effect as compile_item_effect,
    Plan2NativeItemRuntime,
    restore_plan2_native_item_runtime,
)
from .plan2_search_play_card_stamina_consumption_change import (
    SearchPlayCardStaminaRuntime, SearchPlayCardStaminaStatus,
    resolve_plan2_search_play_card_stamina_consumption_change,
)
from .plan2_native_program_catalog import (
    Plan2MasterEffectChainOperation,
    Plan2MasterPlayingExecutor,
    Plan2MasterRemainingThreeInstallOperation,
    Plan2MasterStatusEnchantOperation,
)
from .item_rules import load_item_rule
from .logic_engine import load_master_effect
from .plan2_native_serializer_progress import (
    Plan2NativeSerializerProgress,
    Plan2NativeSerializerProgressError,
    is_identity_empty_trigger_item_scratch,
    restore_plan2_native_serializer_progress,
)
from .plan2_stamina_consumption_add import (
    StaminaConsumptionAddLayer,
    StaminaConsumptionAddRuntime,
)
from .plan2_exam_save_battle_scoring import (
    plan2_battle_scoring_context_from_exam_save,
)
from .plan2_exam_mode import (
    PLAN2_AUDITION_EXAM_TYPE,
    PLAN2_LESSON_EXAM_TYPE,
    Plan2ExamMode,
    Plan2ExamModeError,
    Plan2ScheduledGimmick,
    Plan2ScheduledGimmickHook,
    Plan2ScheduledGimmickHookKey,
    compile_plan2_scheduled_gimmicks,
    resolve_plan2_exam_mode,
)
from .master_db import DEFAULT_DATABASE
from .drink_catalog import load_drink_catalog
from .plan2_state import Plan2EndTurnListener, Plan2State
from .plan2_native_catalog_aggressive_additive import (
    AggressiveAdditiveLayer, Plan2AggressiveAdditiveRuntime, load_plan2_native_catalog_aggressive_additive,
)


PLAN2_NATIVE_LOCAL_SAVE_BOOTSTRAP_SCHEMA_VERSION: Final = 1
PLAN2_LOCAL_SAVE_PLAN_TYPE: Final = 3
PLAN2_LOCAL_SAVE_LESSON_EXAM_TYPE: Final = PLAN2_LESSON_EXAM_TYPE
PLAN2_LOCAL_SAVE_AUDITION_EXAM_TYPE: Final = PLAN2_AUDITION_EXAM_TYPE
PLAN2_LOCAL_SAVE_MAIN_PHASE: Final = 6
DEFAULT_PLAN2_EXAM_SETTING_MASTER_DIR: Final = (
    Path(__file__).resolve().parents[2] / "_research" / "gakumasu-diff"
)

_ASSEMBLY: Final = "Assembly-CSharp"
_NAMESPACE: Final = "Campus.InGame.Exam"
_REVIEW_CLASS: Final = "ReviewStatusEffect"
_AGGRESSIVE_CLASS: Final = "AggressiveStatusEffect"
_AGGRESSIVE_ADDITIVE_CLASS: Final = "AggressiveAdditiveStatusEffect"
_PLAYABLE_CLASS: Final = "PlayableValueAddStatusEffect"
_STAMINA_DOWN_CLASS: Final = "StaminaConsumptionDownStatusEffect"
_STAMINA_DOWN_FIX_CLASS: Final = "StaminaConsumptionDownFixStatusEffect"
_STAMINA_ADD_CLASS: Final = "StaminaConsumptionAddStatusEffect"
_ANTI_DEBUFF_CLASS: Final = "AntiDebuffStatusEffect"
_REVIEW_ADDITIVE_CLASS: Final = "ReviewAdditiveStatusEffect"
_LESSON_MULTIPLE_CLASS: Final = "LessonParameterMultipleStatusEffect"
_LESSON_MULTIPLE_DOWN_CLASS: Final = (
    "LessonParameterMultipleDownStatusEffect"
)
_SHARED_RUNTIME_STATUS_CLASSES: Final = frozenset(
    {
        _ANTI_DEBUFF_CLASS,
        _REVIEW_ADDITIVE_CLASS,
        _LESSON_MULTIPLE_CLASS,
        _LESSON_MULTIPLE_DOWN_CLASS,
    }
)
_TRIGGER_CLASS: Final = "TriggerEffectStatusEffect"
_STATUS_FIELDS: Final = frozenset(
    {"_effectList", "_removedEffectList", "_effectCreateCount", "_idolStatusEffect"}
)
_IDOL_STATUS_FIELDS: Final = frozenset({"_currentType", "_currentStep"})
_REFERENCE_FIELDS: Final = frozenset({"version", "RefIds"})
_REFERENCE_ENTRY_FIELDS: Final = frozenset({"rid", "type", "data"})
_REFERENCE_TYPE_FIELDS: Final = frozenset({"asm", "class", "ns"})
_LINK_FIELDS: Final = frozenset({"rid"})
_REVIEW_FIELDS: Final = frozenset(
    {"_isPassingTurnStart", "_isTurnLimited", "_turn", "_uid"}
)
_SCALAR_FIELDS: Final = frozenset(
    {"_isPassingTurnStart", "_isTurnLimited", "_turn", "_uid", "_value"}
)
_ANTI_DEBUFF_FIELDS: Final = frozenset(
    {"_isPassingTurnStart", "_isTurnLimited", "_turn", "_uid", "_count"}
)
_TRIGGER_STATUS_FIELDS: Final = frozenset(
    {
        "_descriptionReactiveDataTextList",
        "_descriptionReactiveDataTypeList",
        "_effectList",
        "_fromExamEffectIdList",
        "_isEncoreEnchant",
        "_isFromEnchantEffect",
        "_isItemDirectEnchant",
        "_isPassingTurnStart",
        "_isTurnLimited",
        "_limitCount",
        "_limitCountInTurn",
        "_limitCountInTurnRemain",
        "_originType",
        "_overrideType",
        "_phaseCountDictionary",
        "_startEnchantOriginId",
        "_startEnchantOriginLevel",
        "_startEnchantOriginType",
        "_startEnchantOwnerId",
        "_statusEnchantId",
        "_trigger",
        "_triggerCard",
        "_triggerDrink",
        "_triggerGimmickGroup",
        "_triggerItem",
        "_turn",
        "_turnCount",
        "_uid",
    }
)
_TRIGGER_FIELDS: Final = frozenset(
    {"_id", "_phaseTypeList", "_phaseValueList"}
)
_RAW_CARD_FIELDS: Final = frozenset(
    {
        "_affectGrowEffectIdList",
        "_baseUpgradeCount",
        "_cardData",
        "_fixedDeckOrder",
        "_growEffectExamStartAfterList",
        "_guid",
        "_isMoveProduceExamEffectUseInTurn",
        "_playCount",
        "_staminaConsumptionSpecifyEffectList",
        "_statusEffect",
        "_supportUpgradeIdList",
        "_tmpUpgradeCount",
    }
)
_RAW_CARD_DATA_FIELDS: Final = frozenset(
    {
        "_customizeCountList",
        "_id",
        "_produceCardSkinAssetId",
        "_produceCardSkinId",
        "_upgradeCount",
    }
)
_RAW_CARD_STATUS_FIELDS: Final = frozenset(
    {
        "_effectGroupIdList",
        "_id",
        "_phaseCountDictionary",
        "_produceCardGrowEffectIdList",
        "_produceExamTriggerId",
        "_spendCount",
        "_spendTurn",
        "_triggerCount",
    }
)
_RAW_CHILD_EFFECT_FIELDS: Final = frozenset(
    {
        "_cardGrowEffectIdList",
        "_cardSearchId",
        "_cardSearchId2",
        "_chainEffectId",
        "_chainEffectIdList",
        "_effectCount",
        "_effectGroupIdList",
        "_effectTurn",
        "_effectType",
        "_effectValue1",
        "_effectValue2",
        "_id",
        "_judgeTargetIndex",
        "_movePositionType",
        "_pickCount",
        "_pickCountMax",
        "_pickCountMax2",
        "_pickCountMin",
        "_pickCountMin2",
        "_pickCountReferenceProduceCardSearchId",
        "_pickCountReferenceProduceCardSearchId2",
        "_pickCountType",
        "_pickCountType2",
        "_pickRangeType",
        "_pickRangeType2",
        "_statusEnchantId",
        "_targetExamEffectType",
        "_targetProduceCardId",
        "_targetUpgradeCount",
    }
)
_EMPTY_TRIGGER_GIMMICK: Final = {
    "_gimmickEffect": {"_effectId": ""},
    "_gimmickTrigger": {
        "_id": "",
        "_phaseTypeList": [],
        "_phaseValueList": [],
    },
    "_id": "",
    "_isPositive": False,
    "_priority": 0,
    "_remainingTurn": 0,
    "_remainingTurnPermil": 0,
    "_startTurn": 0,
}
_EMPTY_TRIGGER_CARD: Final = {
    "_affectGrowEffectIdList": [],
    "_baseUpgradeCount": 0,
    "_cardData": {
        "_customizeCountList": [],
        "_id": "",
        "_produceCardSkinAssetId": "",
        "_produceCardSkinId": "",
        "_upgradeCount": 0,
    },
    "_fixedDeckOrder": 0,
    "_growEffectExamStartAfterList": [],
    "_guid": "",
    "_isMoveProduceExamEffectUseInTurn": False,
    "_playCount": 0,
    "_staminaConsumptionSpecifyEffectList": [],
    "_statusEffect": {
        "_effectGroupIdList": [],
        "_id": "",
        "_phaseCountDictionary": {"_list": []},
        "_produceCardGrowEffectIdList": [],
        "_produceExamTriggerId": "",
        "_spendCount": 0,
        "_spendTurn": 0,
        "_triggerCount": 0,
    },
    "_supportUpgradeIdList": [],
    "_tmpUpgradeCount": 0,
}
_EMPTY_CARD_STATUS: Final = {
    "_effectGroupIdList": [],
    "_id": "",
    "_phaseCountDictionary": {"_list": []},
    "_produceCardGrowEffectIdList": [],
    "_produceExamTriggerId": "",
    "_spendCount": 0,
    "_spendTurn": 0,
    "_triggerCount": 0,
}
_END_TURN_PHASE_TYPE_VALUE: Final = 5
_PLAY_COUNT_INTERVAL_PHASE_TYPE_VALUE: Final = 23
_LESSON_DEPEND_REVIEW_EFFECT_TYPE_VALUE: Final = 124
_LESSON_DEPEND_AGGRESSIVE_EFFECT_TYPE_VALUE: Final = 125
_LESSON_DEPEND_BLOCK_EFFECT_TYPE_VALUE: Final = 19
_LESSON_EFFECT_TYPE_VALUE: Final = 1
_REVIEW_EFFECT_TYPE_VALUE: Final = 31


def _plain_int(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _block(
    blockers: list[Plan2NativeBlocker], code: str, detail: str = ""
) -> None:
    blocker = Plan2NativeBlocker(code, detail)
    if blocker not in blockers:
        blockers.append(blocker)


class Plan2NativeExamSettingAuthorityError(ValueError):
    """A PC Master ExamSetting row could not be bound exactly."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


@dataclass(frozen=True, slots=True)
class Plan2NativeExamSettingAuthority:
    """Typed projection of the two ordered-hand rules used by the horizon."""

    setting_id: str
    turn_start_distribute: int
    hand_limit: int
    turn_end_stamina_recovery: int
    source_path: str

    def __post_init__(self) -> None:
        if not isinstance(self.setting_id, str) or not self.setting_id:
            raise ValueError("setting_id must be non-empty text")
        _plain_int(self.turn_start_distribute, "turn_start_distribute")
        _plain_int(self.hand_limit, "hand_limit", minimum=1)
        _plain_int(
            self.turn_end_stamina_recovery,
            "turn_end_stamina_recovery",
        )
        if not isinstance(self.source_path, str) or not self.source_path:
            raise ValueError("source_path must be non-empty text")

    @property
    def draw_count(self) -> int:
        return self.turn_start_distribute


def load_plan2_native_exam_setting_authority(
    setting_id: str,
    *,
    master_dir: str | Path = DEFAULT_PLAN2_EXAM_SETTING_MASTER_DIR,
) -> Plan2NativeExamSettingAuthority:
    """Load one unique ``ExamSetting`` hand-rule projection from PC Master."""

    if not isinstance(setting_id, str) or not setting_id:
        raise Plan2NativeExamSettingAuthorityError(
            "plan2-exam-setting-id-invalid"
        )
    path = Path(master_dir) / "ExamSetting.yaml"
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise Plan2NativeExamSettingAuthorityError(
            "plan2-exam-setting-master-unavailable",
            f"{type(error).__name__}:{error}",
        ) from error
    except yaml.YAMLError as error:
        raise Plan2NativeExamSettingAuthorityError(
            "plan2-exam-setting-master-invalid",
            type(error).__name__,
        ) from error
    if not isinstance(payload, list):
        raise Plan2NativeExamSettingAuthorityError(
            "plan2-exam-setting-master-invalid",
            "expected-array",
        )
    matches = [
        row
        for row in payload
        if isinstance(row, Mapping) and row.get("id") == setting_id
    ]
    if len(matches) != 1:
        raise Plan2NativeExamSettingAuthorityError(
            "plan2-exam-setting-row-not-unique",
            f"{setting_id}:count={len(matches)}",
        )
    row = matches[0]

    def field(name: str, *, minimum: int = 0) -> int:
        if name not in row:
            raise Plan2NativeExamSettingAuthorityError(
                "plan2-exam-setting-field-missing",
                f"{setting_id}:{name}",
            )
        try:
            return _plain_int(row[name], name, minimum=minimum)
        except ValueError as error:
            raise Plan2NativeExamSettingAuthorityError(
                "plan2-exam-setting-field-invalid",
                f"{setting_id}:{error}",
            ) from error

    return Plan2NativeExamSettingAuthority(
        setting_id=setting_id,
        turn_start_distribute=field("turnStartDistribute"),
        hand_limit=field("handLimit", minimum=1),
        turn_end_stamina_recovery=field("examTurnEndRecoveryStamina"),
        source_path=str(path.resolve()),
    )


@dataclass(frozen=True, slots=True)
class Plan2NativeLocalSaveCardAudit:
    zone: str
    zone_order: int
    guid: str
    card_id: str
    base_upgrade: int
    temporary_upgrade: int
    effective_upgrade: int
    play_count: int | None

    @classmethod
    def from_card(
        cls, zone: str, card: LocalSaveExamCard
    ) -> "Plan2NativeLocalSaveCardAudit":
        return cls(
            zone=zone,
            zone_order=card.zone_order,
            guid=card.guid,
            card_id=card.card_id,
            base_upgrade=card.base_upgrade,
            temporary_upgrade=card.temporary_upgrade,
            effective_upgrade=card.effective_upgrade,
            play_count=(
                None if card.runtime_state is None else card.runtime_state.play_count
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "zone": self.zone,
            "zone_order": self.zone_order,
            "guid": self.guid,
            "card_id": self.card_id,
            "base_upgrade": self.base_upgrade,
            "temporary_upgrade": self.temporary_upgrade,
            "effective_upgrade": self.effective_upgrade,
            "play_count": self.play_count,
        }


@dataclass(frozen=True, slots=True)
class Plan2NativeLocalSaveBootstrapAudit:
    """Typed result and human-readable proof of one bootstrap attempt."""

    schema_version: int
    state: Plan2NativeHorizonState | None
    blockers: tuple[Plan2NativeBlocker, ...]
    cards: tuple[Plan2NativeLocalSaveCardAudit, ...]
    active_status_classes: tuple[str, ...]
    mapped_fields: tuple[str, ...]
    catalog_refs: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if self.schema_version != PLAN2_NATIVE_LOCAL_SAVE_BOOTSTRAP_SCHEMA_VERSION:
            raise ValueError("unsupported Plan2 LocalSave bootstrap audit schema")
        if self.state is not None and not isinstance(
            self.state, Plan2NativeHorizonState
        ):
            raise TypeError("state must be Plan2NativeHorizonState or None")
        if self.blockers and self.state is not None:
            raise ValueError("a blocked bootstrap must not expose a dispatchable state")

    @property
    def simulation_ready(self) -> bool:
        return self.state is not None and not self.blockers

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "simulation_ready": self.simulation_ready,
            "mapped_fields": list(self.mapped_fields),
            "active_status_classes": list(self.active_status_classes),
            "catalog_refs": [list(value) for value in self.catalog_refs],
            "cards": [value.to_dict() for value in self.cards],
            "blockers": [value.to_dict() for value in self.blockers],
        }


@dataclass(frozen=True, slots=True)
class _StatusProjection:
    review: int
    review_present: bool
    review_passing_turn_start: bool
    aggressive: int
    stamina_down: StaminaConsumptionDownLayer | None
    stamina_down_fix_layers: tuple[StaminaConsumptionDownFixLayer, ...]
    stamina_add: StaminaConsumptionAddLayer | None
    plays_remaining: int
    next_status_uid: int
    active_status_classes: tuple[str, ...]
    item_runtime: Plan2NativeItemRuntime
    end_turn_listeners: tuple[Plan2EndTurnListener, ...]
    status_enchant_listeners: tuple[Plan2NativeStatusEnchantListener, ...]
    aggressive_additive_runtime: Plan2AggressiveAdditiveRuntime | None = None
    search_play_card_stamina_runtime: SearchPlayCardStaminaRuntime = SearchPlayCardStaminaRuntime()


def _card_audit(exam: LocalSaveExamState) -> tuple[Plan2NativeLocalSaveCardAudit, ...]:
    values: list[Plan2NativeLocalSaveCardAudit] = []
    for zone in ("hand", "deck", "grave", "lost", "hold"):
        values.extend(
            Plan2NativeLocalSaveCardAudit.from_card(zone, card)
            for card in getattr(exam.zones, zone)
        )
    if exam.playing_card is not None:
        values.append(
            Plan2NativeLocalSaveCardAudit.from_card("playing", exam.playing_card)
        )
    values.extend(
        Plan2NativeLocalSaveCardAudit.from_card("removed", card)
        for card in exam.removed_cards
    )
    for index, group in enumerate(exam.future_deck):
        values.extend(
            Plan2NativeLocalSaveCardAudit.from_card(f"future[{index}]", card)
            for card in group
        )
    for index, group in enumerate(exam.past_deck or ()):
        values.extend(
            Plan2NativeLocalSaveCardAudit.from_card(f"past[{index}]", card)
            for card in group
        )
    return tuple(values)


def _exact_mapping(
    value: object,
    fields: frozenset[str],
    blockers: list[Plan2NativeBlocker],
    code: str,
    detail: str,
) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping) or set(value) != set(fields):
        _block(blockers, code, detail)
        return None
    return value


def _links(
    value: object,
    *,
    label: str,
    blockers: list[Plan2NativeBlocker],
) -> tuple[int, ...]:
    if not isinstance(value, list):
        _block(blockers, "local-save-status-links-invalid", label)
        return ()
    result: list[int] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping) or set(raw) != set(_LINK_FIELDS):
            _block(
                blockers,
                "local-save-status-link-invalid",
                f"{label}[{index}]",
            )
            continue
        try:
            rid = _plain_int(raw["rid"], f"{label}[{index}].rid", minimum=1)
        except ValueError as error:
            _block(blockers, "local-save-status-link-invalid", str(error))
            continue
        result.append(rid)
    if len(result) != len(set(result)):
        _block(blockers, "local-save-status-link-duplicate", label)
    return tuple(result)


def restore_plan2_native_anti_debuff_registry(
    exam: LocalSaveExamState,
    *,
    next_uid: int,
) -> tuple[Plan2NativeDebuffRegistry, tuple[Plan2NativeBlocker, ...]]:
    """Restore the one merged AntiDebuff charge from native active references."""

    if not isinstance(exam, LocalSaveExamState):
        raise TypeError("exam must be LocalSaveExamState")
    try:
        requested_next_uid = _plain_int(
            next_uid,
            "anti-debuff next_uid",
            minimum=1,
        )
    except ValueError as error:
        raise TypeError(str(error)) from error
    blockers: list[Plan2NativeBlocker] = []
    runtime = exam.root_runtime
    opaque = runtime.opaque_fields.to_value() if runtime is not None else {}
    if not isinstance(opaque, Mapping):
        _block(blockers, "local-save-anti-debuff-graph-invalid", "root")
        return Plan2NativeDebuffRegistry(next_uid=requested_next_uid), tuple(blockers)
    status = opaque.get("status")
    references = opaque.get("references")
    if not isinstance(status, Mapping) or not isinstance(references, Mapping):
        _block(blockers, "local-save-anti-debuff-graph-invalid", "status/references")
        return Plan2NativeDebuffRegistry(next_uid=requested_next_uid), tuple(blockers)
    active_rids = _links(
        status.get("_effectList"),
        label="status._effectList",
        blockers=blockers,
    )
    raw_entries = references.get("RefIds")
    if not isinstance(raw_entries, list):
        _block(blockers, "local-save-anti-debuff-graph-invalid", "RefIds")
        return Plan2NativeDebuffRegistry(next_uid=requested_next_uid), tuple(blockers)
    by_rid = {
        raw.get("rid"): raw
        for raw in raw_entries
        if isinstance(raw, Mapping)
        and type(raw.get("rid")) is int
    }
    restored: list[tuple[StatusEffect, Plan2NativeStatusLifetime]] = []
    for rid in active_rids:
        raw = by_rid.get(rid)
        raw_type = raw.get("type") if isinstance(raw, Mapping) else None
        if not isinstance(raw_type, Mapping) or raw_type.get("class") != _ANTI_DEBUFF_CLASS:
            continue
        data = raw.get("data")
        exact = _exact_mapping(
            data,
            _ANTI_DEBUFF_FIELDS,
            blockers,
            "local-save-anti-debuff-status-invalid",
            str(rid),
        )
        if exact is None:
            continue
        try:
            count = _plain_int(exact["_count"], "AntiDebuff._count", minimum=1)
            uid = _plain_int(exact["_uid"], "AntiDebuff._uid", minimum=1)
        except ValueError as error:
            _block(blockers, "local-save-anti-debuff-status-invalid", str(error))
            continue
        passing = exact["_isPassingTurnStart"]
        if (
            exact["_turn"] != -1
            or exact["_isTurnLimited"] is not False
            or type(passing) is not bool
        ):
            _block(
                blockers,
                "local-save-anti-debuff-status-invalid",
                f"rid={rid}:lifecycle",
            )
            continue
        instance_id = f"native:anti-debuff:{uid}"
        restored.append(
            (
                StatusEffect(
                    instance_id=instance_id,
                    uid=uid,
                    effect_type=EFFECT_ANTI_DEBUFF,
                    icon_value=count,
                    layers=1,
                    active=True,
                    target_type=ExamStatusEffectTargetType.BUFF,
                    native_status_type=ANTI_DEBUFF_STATUS_NATIVE_TYPE,
                ),
                Plan2NativeStatusLifetime(
                    instance_id,
                    turn=-1,
                    is_passing_turn_start=passing,
                ),
            )
        )
    if len(restored) > 1:
        _block(
            blockers,
            "local-save-anti-debuff-status-invalid",
            f"active-count={len(restored)}",
        )
        return Plan2NativeDebuffRegistry(next_uid=requested_next_uid), tuple(blockers)
    statuses = tuple(value[0] for value in restored)
    lifetimes = tuple(value[1] for value in restored)
    registry = Plan2NativeDebuffRegistry(
        statuses=statuses,
        lifetimes=lifetimes,
        next_uid=max((requested_next_uid, *(value.uid + 1 for value in statuses))),
    )
    return registry, tuple(blockers)


def restore_plan2_native_review_dynamic_runtime(
    exam: LocalSaveExamState,
    *,
    review: int,
    review_status_present: bool,
    review_passing_turn_start: bool,
    next_uid: int,
) -> tuple[Plan2NativeReviewDynamicRuntime, tuple[Plan2NativeBlocker, ...]]:
    """Restore ordered native Review/Lesson multiplier status layers.

    This is the canonical ExamSave projection used by both live bootstrap and
    offline native diff.  Runtime status identity is the serialized UID and
    active-reference order; a source card is deliberately not guessed for an
    already-active status.
    """

    if not isinstance(exam, LocalSaveExamState):
        raise TypeError("exam must be LocalSaveExamState")
    try:
        requested_next_uid = _plain_int(
            next_uid,
            "review-dynamic next_uid",
            minimum=1,
        )
    except ValueError as error:
        raise TypeError(str(error)) from error
    base = Plan2NativeReviewDynamicRuntime(
        review=review,
        review_status_present=review_status_present,
        review_passing_turn_start=review_passing_turn_start,
        next_status_uid=requested_next_uid,
    )
    blockers: list[Plan2NativeBlocker] = []
    runtime = exam.root_runtime
    opaque = runtime.opaque_fields.to_value() if runtime is not None else {}
    if not isinstance(opaque, Mapping):
        _block(blockers, "native-review-dynamic-reference-unavailable", "root")
        return base, tuple(blockers)
    status = opaque.get("status")
    references = opaque.get("references")
    if not isinstance(status, Mapping) or not isinstance(references, Mapping):
        _block(
            blockers,
            "native-review-dynamic-reference-unavailable",
            "status/references",
        )
        return base, tuple(blockers)
    active_rids = _links(
        status.get("_effectList"),
        label="status._effectList",
        blockers=blockers,
    )
    raw_entries = references.get("RefIds")
    if not isinstance(raw_entries, list):
        _block(blockers, "native-review-dynamic-reference-unavailable", "RefIds")
        return base, tuple(blockers)
    by_rid = {
        raw.get("rid"): raw
        for raw in raw_entries
        if isinstance(raw, Mapping) and type(raw.get("rid")) is int
    }
    layers: list[Plan2NativeReviewDynamicLayer] = []
    specifications = {
        _REVIEW_ADDITIVE_CLASS: (
            LAYER_REVIEW_ADDITIVE,
            "native-review-additive-status-unprojected",
        ),
        _LESSON_MULTIPLE_CLASS: (
            LAYER_LESSON_MULTIPLE,
            "native-lesson-multiple-status-unprojected",
        ),
        _LESSON_MULTIPLE_DOWN_CLASS: (
            LAYER_LESSON_MULTIPLE_DOWN,
            "native-lesson-multiple-down-status-unprojected",
        ),
    }
    for rid in active_rids:
        raw = by_rid.get(rid)
        raw_type = raw.get("type") if isinstance(raw, Mapping) else None
        class_name = (
            raw_type.get("class") if isinstance(raw_type, Mapping) else None
        )
        specification = specifications.get(class_name)
        if specification is None:
            continue
        kind, code = specification
        data = raw.get("data")
        exact = _exact_mapping(data, _SCALAR_FIELDS, blockers, code, f"rid={rid}")
        if exact is None:
            continue
        uid = exact.get("_uid")
        value = exact.get("_value")
        turn = exact.get("_turn")
        passing = exact.get("_isPassingTurnStart")
        limited = exact.get("_isTurnLimited")
        valid_common = (
            type(uid) is int
            and uid >= 1
            and type(value) is int
            and type(turn) is int
            and type(passing) is bool
            and type(limited) is bool
        )
        if kind == LAYER_REVIEW_ADDITIVE:
            valid_lifecycle = (
                type(value) is int
                and value >= 0
                and type(turn) is int
                and turn >= 1
                and limited is True
            )
        elif kind == LAYER_LESSON_MULTIPLE:
            valid_lifecycle = (
                type(turn) is int
                and (turn == -1 or turn >= 1)
                and limited == (turn != -1)
            )
        else:
            valid_lifecycle = (
                type(value) is int
                and value >= 0
                and type(turn) is int
                and turn >= 1
                and limited is True
            )
        if not valid_common or not valid_lifecycle:
            _block(
                blockers,
                code,
                f"rid={rid}:uid={uid!r}:value={value!r}:turn={turn!r}:"
                f"passing={passing!r}:limited={limited!r}",
            )
            continue
        source = f"native-status:{class_name}:{uid}"
        try:
            layers.append(
                Plan2NativeReviewDynamicLayer(
                    status_uid=uid,
                    kind=kind,
                    source_guid=source,
                    source_card_id=f"native-status:{class_name}",
                    source_upgrade=0,
                    source_effect_id=source,
                    permille=value,
                    turn=turn,
                    passing_turn_start=passing,
                    is_turn_limited=limited,
                )
            )
        except (TypeError, ValueError) as error:
            _block(
                blockers,
                code,
                f"rid={rid}:uid={uid!r}:{type(error).__name__}:{error}",
            )
    final_next_uid = max(
        (
            requested_next_uid,
            *(layer.status_uid + 1 for layer in layers),
        )
    )
    try:
        restored = replace(
            base,
            layers=tuple(layers),
            next_status_uid=final_next_uid,
        )
    except (TypeError, ValueError) as error:
        _block(
            blockers,
            "native-review-dynamic-runtime-unprojected",
            f"{type(error).__name__}:{error}",
        )
        return base, tuple(blockers)
    return restored, tuple(blockers)


class _CardStatusRestoreError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}:{detail}")


def _restore_trigger_serializer_progress(
    data: Mapping[str, object],
    *,
    master_turn: int,
    master_total_limit: int,
    master_per_turn_limit: int,
    phase_type_names: Mapping[int, str],
    label: str,
    error_code: str,
    phase_error_code: str,
) -> Plan2NativeSerializerProgress:
    try:
        return restore_plan2_native_serializer_progress(
            data,
            master_turn=master_turn,
            master_total_limit=master_total_limit,
            master_per_turn_limit=master_per_turn_limit,
            phase_type_names=phase_type_names,
            label=label,
        )
    except Plan2NativeSerializerProgressError as error:
        code = (
            phase_error_code
            if error.code.startswith("phase-")
            else error_code
        )
        raise _CardStatusRestoreError(code, str(error)) from error


def _restore_positive_uid(data: Mapping[str, object], label: str) -> int:
    try:
        return _plain_int(data.get("_uid"), f"{label}._uid", minimum=1)
    except ValueError as error:
        raise _CardStatusRestoreError(
            "local-save-status-uid-invalid", str(error)
        ) from error


def _restore_trigger_source_card(
    exam: LocalSaveExamState,
    raw: object,
) -> tuple[str, str, int]:
    if not isinstance(raw, Mapping) or set(raw) != set(_RAW_CARD_FIELDS):
        raise _CardStatusRestoreError(
            "local-save-card-status-source-shape-invalid", "triggerCard"
        )
    card_data = raw.get("_cardData")
    status_effect = raw.get("_statusEffect")
    if (
        not isinstance(card_data, Mapping)
        or set(card_data) != set(_RAW_CARD_DATA_FIELDS)
        or not isinstance(status_effect, Mapping)
        or set(status_effect) != set(_RAW_CARD_STATUS_FIELDS)
    ):
        raise _CardStatusRestoreError(
            "local-save-card-status-source-shape-invalid", "cardData/statusEffect"
        )
    guid = raw.get("_guid")
    card_id = card_data.get("_id")
    support_ids = raw.get("_supportUpgradeIdList")
    customize_counts = card_data.get("_customizeCountList")
    if (
        not isinstance(guid, str)
        or not guid
        or not isinstance(card_id, str)
        or not card_id
        or not isinstance(support_ids, list)
        or any(not isinstance(value, str) or not value for value in support_ids)
        or len(support_ids) != len(set(support_ids))
        or not isinstance(customize_counts, list)
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value != 0
            for value in customize_counts
        )
    ):
        raise _CardStatusRestoreError(
            "local-save-card-status-source-identity-invalid"
        )
    try:
        base = _plain_int(raw.get("_baseUpgradeCount"), "triggerCard.base")
        temporary = _plain_int(raw.get("_tmpUpgradeCount"), "triggerCard.tmp")
        effective = _plain_int(card_data.get("_upgradeCount"), "triggerCard.upgrade")
        play_count = _plain_int(raw.get("_playCount"), "triggerCard.playCount")
    except ValueError as error:
        raise _CardStatusRestoreError(
            "local-save-card-status-source-counter-invalid", str(error)
        ) from error
    if (
        max(base, temporary, effective) > 3
        or effective != base + temporary + len(support_ids)
        or isinstance(raw.get("_fixedDeckOrder"), bool)
        or not isinstance(raw.get("_fixedDeckOrder"), int)
        or play_count < 0
        or card_data.get("_produceCardSkinId") != ""
        or card_data.get("_produceCardSkinAssetId") != ""
        or dict(status_effect) != _EMPTY_CARD_STATUS
        or raw.get("_growEffectExamStartAfterList") != []
        or raw.get("_affectGrowEffectIdList") != []
        or raw.get("_staminaConsumptionSpecifyEffectList") != []
        or raw.get("_isMoveProduceExamEffectUseInTurn") is not False
    ):
        raise _CardStatusRestoreError(
            "local-save-card-status-source-runtime-unmapped", guid
        )
    # ``removedCardList`` is a positioned pre-play tombstone, not a live card
    # zone.  The same GUID can therefore exist there at an older runtime while
    # the active listener's source is uniquely present in Hand/Deck/Grave/Lost.
    # Resolve only live zones: a duplicate live GUID still fails, and a source
    # that exists only as a removed tombstone remains missing.
    matches = tuple(card for card in exam.zones.all_cards if card.guid == guid)
    if len(matches) != 1:
        raise _CardStatusRestoreError(
            "local-save-card-status-source-guid-not-unique", guid
        )
    current = matches[0]
    if (
        current.card_id != card_id
        or current.base_upgrade != base
        or current.fixed_deck_order != raw["_fixedDeckOrder"]
        or current.runtime_state is None
        or current.runtime_state.customize_count_list.to_value()
        != customize_counts
    ):
        raise _CardStatusRestoreError(
            "local-save-card-status-source-identity-mismatch", guid
        )
    return guid, card_id, effective


def restore_plan2_native_effect_chain_runtime(
    exam: LocalSaveExamState,
    catalog: Plan2NativeProgramCatalog,
    *,
    next_uid: int,
) -> tuple[Plan2NativeEffectChainRuntime, tuple[Plan2NativeBlocker, ...]]:
    """Restore active item/card EffectTimer statuses from captured lineage."""

    blockers: list[Plan2NativeBlocker] = []
    opaque = (
        exam.root_runtime.opaque_fields.to_value()
        if exam.root_runtime is not None
        else None
    )
    status = opaque.get("status") if isinstance(opaque, Mapping) else None
    references = opaque.get("references") if isinstance(opaque, Mapping) else None
    raw_entries = (
        references.get("RefIds") if isinstance(references, Mapping) else None
    )
    if not isinstance(status, Mapping) or not isinstance(raw_entries, list):
        _block(blockers, "local-save-effect-timer-graph-invalid")
        return Plan2NativeEffectChainRuntime(next_status_uid=next_uid), tuple(blockers)
    active_rids = _links(
        status.get("_effectList"),
        label="status._effectList",
        blockers=blockers,
    )
    by_rid = {
        raw.get("rid"): raw
        for raw in raw_entries
        if isinstance(raw, Mapping) and type(raw.get("rid")) is int
    }
    pending: list[tuple[int, str, Plan2NativeEffectChainProgram, int]] = []
    completed_boundaries = 0
    for rid in active_rids:
        raw = by_rid.get(rid)
        raw_type = raw.get("type") if isinstance(raw, Mapping) else None
        data = raw.get("data") if isinstance(raw, Mapping) else None
        if (
            not isinstance(raw_type, Mapping)
            or raw_type.get("class") != _TRIGGER_CLASS
            or not isinstance(data, Mapping)
            or data.get("_statusEnchantId") != ""
        ):
            continue
        trigger = data.get("_trigger")
        effects = data.get("_effectList")
        if not (
            isinstance(trigger, Mapping)
            and trigger.get("_phaseTypeList") == [21]
            and isinstance(trigger.get("_phaseValueList"), list)
            and len(trigger["_phaseValueList"]) == 1
            and isinstance(effects, list)
            and len(effects) == 1
            and isinstance(effects[0], Mapping)
        ):
            continue
        trigger_item = data.get("_triggerItem")
        trigger_card = data.get("_triggerCard")
        item_id = (
            trigger_item.get("_id")
            if isinstance(trigger_item, Mapping)
            else None
        )
        card_guid = (
            trigger_card.get("_guid")
            if isinstance(trigger_card, Mapping)
            else None
        )
        child_raw = effects[0]
        child_id = child_raw.get("_id")
        delay = trigger["_phaseValueList"][0]
        uid = data.get("_uid")
        age = data.get("_turnCount")
        if not (
            ((isinstance(item_id, str) and bool(item_id))
             != (isinstance(card_guid, str) and bool(card_guid)))
            and isinstance(child_id, str)
            and bool(child_id)
            and type(delay) is int
            and delay >= 1
            and type(uid) is int
            and uid >= 1
            and type(age) is int
            and 0 <= age < delay
            and data.get("_limitCount") == 1
            and data.get("_limitCountInTurn") == -1
            and data.get("_limitCountInTurnRemain") == -1
        ):
            _block(blockers, "local-save-effect-timer-status-invalid", str(rid))
            continue
        try:
            if isinstance(item_id, str) and item_id:
                rule = load_item_rule(item_id)
                parents = tuple(
                    effect
                    for enchantment in rule.enchantments
                    for effect in enchantment.effects
                    if effect.effect_type == "ProduceExamEffectType_ExamEffectTimer"
                    and effect.chain_effect_id == child_id
                    and effect.value1 == delay
                    and effect.value2 == 0
                    and effect.effect_count == 1
                    and effect.effect_turn == 0
                )
                if len(parents) != 1:
                    raise ValueError(f"parent-count={len(parents)}")
                parent = parents[0]
                child = load_master_effect(child_id)
                compiled_parent, parent_issue = compile_item_effect(parent)
                if parent_issue or compiled_parent is None or compiled_parent.timer_child is None:
                    raise ValueError(f"item-child-family-unbound:{parent_issue}")
                program = Plan2NativeEffectChainProgram(
                    card_id="item-runtime",
                    upgrade=0,
                    slot_index=0,
                    ordered_effect_ids=(parent.id,),
                    timer_effect_id=parent.id,
                    delay=delay,
                    effect_count=parent.effect_count,
                    effect_turn=parent.effect_turn,
                    child_effect_id=child.id,
                    child_effect_type=child.effect_type,
                    child_value1=child.value1,
                    child_value2=child.value2,
                    child_count=child.effect_count,
                    child_turn=child.effect_turn,
                    child_executor=EFFECT_CHAIN_EXECUTOR_BY_TYPE[child.effect_type],
                )
                source_guid = f"item:{item_id}:{parent.id}"
            else:
                source_guid, card_id, upgrade = _restore_trigger_source_card(
                    exam, trigger_card
                )
                programs = tuple(
                    value for value in catalog.programs
                    if value.ref == (card_id, upgrade)
                )
                if len(programs) != 1 or not isinstance(
                    programs[0].native_playing_executor,
                    Plan2MasterPlayingExecutor,
                ):
                    raise ValueError(f"card-program-count={len(programs)}")
                operations = tuple(
                    getattr(value, "operation", value)
                    for value in programs[0].native_playing_executor.operations
                )
                matches = tuple(
                    value.program
                    for value in operations
                    if isinstance(value, Plan2MasterEffectChainOperation)
                    and value.program.child_effect_id == child_id
                    and value.program.delay == delay
                )
                if len(matches) != 1:
                    raise ValueError(f"card-timer-count={len(matches)}")
                program = matches[0]
            expected = {
                "_id": program.child_effect_id,
                "_effectType": _SERIALIZED_EFFECT_ENUM[program.child_effect_type],
                "_effectValue1": program.child_value1,
                "_effectValue2": program.child_value2,
                "_effectCount": program.child_count,
                "_effectTurn": program.child_turn,
                "_chainEffectId": "",
                "_statusEnchantId": "",
            }
            if any(child_raw.get(name) != value for name, value in expected.items()):
                raise ValueError("child-master-mismatch")
            pending.append((uid, source_guid, program, age))
            completed_boundaries = max(completed_boundaries, age)
        except (KeyError, OSError, TypeError, ValueError) as error:
            _block(
                blockers,
                "local-save-effect-timer-status-unprojected",
                f"rid={rid}:{type(error).__name__}:{error}",
            )
    try:
        queue = tuple(
            Plan2NativeEffectChainQueuedTimer(
                status_uid=uid,
                source_guid=source_guid,
                play_origin="normal",
                install_sequence=index,
                installed_start_turn_boundary=completed_boundaries - age,
                program=program,
            )
            for index, (uid, source_guid, program, age)
            in enumerate(pending, 1)
        )
        runtime = Plan2NativeEffectChainRuntime(
            queue=queue,
            completed_start_turn_boundaries=completed_boundaries,
            next_status_uid=max(next_uid, max((value.status_uid for value in queue), default=0) + 1),
            next_install_sequence=len(queue) + 1,
        )
    except (TypeError, ValueError) as error:
        _block(
            blockers,
            "local-save-effect-timer-runtime-unprojected",
            f"{type(error).__name__}:{error}",
        )
        runtime = Plan2NativeEffectChainRuntime(next_status_uid=next_uid)
    return runtime, tuple(blockers)


def _expected_remaining_three_child(effect: object) -> dict[str, object]:
    effect_type = getattr(effect, "effect_type", None)
    if effect_type != "ProduceExamEffectType_ExamLessonDependExamReview":
        raise _CardStatusRestoreError(
            "local-save-card-status-child-family-unmapped", repr(effect_type)
        )
    return {
        "_cardGrowEffectIdList": [],
        "_cardSearchId": "",
        "_cardSearchId2": "",
        "_chainEffectId": "",
        "_chainEffectIdList": [],
        "_effectCount": effect.count,
        "_effectGroupIdList": list(effect.effect_group_ids),
        "_effectTurn": effect.turn,
        "_effectType": _LESSON_DEPEND_REVIEW_EFFECT_TYPE_VALUE,
        "_effectValue1": effect.value1,
        "_effectValue2": effect.value2,
        "_id": effect.effect_id,
        "_judgeTargetIndex": 0,
        "_movePositionType": 0,
        "_pickCount": 0,
        "_pickCountMax": 0,
        "_pickCountMax2": 0,
        "_pickCountMin": 0,
        "_pickCountMin2": 0,
        "_pickCountReferenceProduceCardSearchId": "",
        "_pickCountReferenceProduceCardSearchId2": "",
        "_pickCountType": 0,
        "_pickCountType2": 0,
        "_pickRangeType": 0,
        "_pickRangeType2": 0,
        "_statusEnchantId": "",
        "_targetExamEffectType": 0,
        "_targetProduceCardId": "",
        "_targetUpgradeCount": 0,
    }


def _expected_catalog_status_child(effect: object) -> dict[str, object]:
    effect_type = getattr(effect, "effect_type", None)
    if effect_type == "ProduceExamEffectType_ExamLesson":
        effect_type_value = _LESSON_EFFECT_TYPE_VALUE
    elif effect_type == "ProduceExamEffectType_ExamReview":
        effect_type_value = _REVIEW_EFFECT_TYPE_VALUE
    elif effect_type == "ProduceExamEffectType_ExamCardPlayAggressive":
        # This child is emitted by the exact CardPlay status-enchant catalog;
        # its native enum value is retained in the ExamSave reference graph.
        effect_type_value = 42
    elif effect_type == "ProduceExamEffectType_ExamLessonDependExamReview":
        # The Plan2 lesson/review dependent child is also executable by the
        # horizon's status-child dispatcher.  Keep the mapping explicit and
        # do not classify arbitrary effect types as this family.
        effect_type_value = _LESSON_DEPEND_REVIEW_EFFECT_TYPE_VALUE
    elif effect_type == (
        "ProduceExamEffectType_ExamLessonDependExamCardPlayAggressive"
    ):
        # Gimmick- and card-owned listeners serialize the same native leaf
        # as enum 125.  Execution remains owned by the shared typed
        # LessonDependAggressive route in the horizon dispatcher.
        effect_type_value = _LESSON_DEPEND_AGGRESSIVE_EFFECT_TYPE_VALUE
    elif effect_type == "ProduceExamEffectType_ExamLessonDependBlock":
        # StatusChange CardPlayAggressive gimmicks can queue a lesson that is
        # scaled from the current Block value.  This child is executable by
        # the same generic status-child dispatcher; preserve its native enum
        # value instead of treating the whole gimmick group as Review-only.
        effect_type_value = _LESSON_DEPEND_BLOCK_EFFECT_TYPE_VALUE
    elif effect_type == "ProduceExamEffectType_ExamStaminaRecoverMultiple":
        # The generic status-child catalog owns this exact family and reuses
        # the plan-neutral float32 recovery kernel.  Preserve the native enum
        # value so StartTurn card listeners can be restored without a
        # card/status identity exception.
        effect_type_value = 117
    else:
        raise _CardStatusRestoreError(
            "local-save-card-status-child-family-unmapped", repr(effect_type)
        )
    return {
        "_cardGrowEffectIdList": list(getattr(effect, "grow_effect_ids", ())),
        "_cardSearchId": getattr(effect, "card_search_id", ""),
        "_cardSearchId2": getattr(effect, "card_search_id2", ""),
        "_chainEffectId": getattr(effect, "chain_effect_id", ""),
        "_chainEffectIdList": list(getattr(effect, "chain_effect_ids", ())),
        "_effectCount": effect.count,
        "_effectGroupIdList": list(effect.effect_group_ids),
        "_effectTurn": effect.turn,
        "_effectType": effect_type_value,
        "_effectValue1": effect.value1,
        "_effectValue2": effect.value2,
        "_id": effect.effect_id,
        "_judgeTargetIndex": 0,
        "_movePositionType": 0,
        "_pickCount": 0,
        "_pickCountMax": 0,
        "_pickCountMax2": 0,
        "_pickCountMin": 0,
        "_pickCountMin2": 0,
        "_pickCountReferenceProduceCardSearchId": "",
        "_pickCountReferenceProduceCardSearchId2": "",
        "_pickCountType": 0,
        "_pickCountType2": 0,
        "_pickRangeType": 0,
        "_pickRangeType2": 0,
        "_statusEnchantId": getattr(effect, "status_enchant_id", ""),
        "_targetExamEffectType": 0,
        "_targetProduceCardId": getattr(effect, "target_card_id", ""),
        "_targetUpgradeCount": getattr(effect, "target_upgrade", 0),
    }


def _restore_catalog_status_listener(
    exam: LocalSaveExamState,
    data: Mapping[str, object],
    catalog: Plan2NativeProgramCatalog,
) -> Plan2NativeStatusEnchantListener:
    source_guid, card_id, effective_upgrade = _restore_trigger_source_card(
        exam, data.get("_triggerCard")
    )
    programs = tuple(value for value in catalog.programs if value.ref == (card_id, effective_upgrade))
    if len(programs) != 1 or not isinstance(
        programs[0].native_playing_executor, Plan2MasterPlayingExecutor
    ):
        raise _CardStatusRestoreError(
            "local-save-card-status-program-not-unique",
            f"{card_id}@{effective_upgrade}:count={len(programs)}",
        )
    enchantment_id = data.get("_statusEnchantId")
    operations = tuple(
        value
        for value in programs[0].native_playing_executor.operations
        if isinstance(value, Plan2MasterStatusEnchantOperation)
        and value.program.status_enchant_id == enchantment_id
    )
    if len(operations) != 1:
        raise _CardStatusRestoreError(
            "local-save-card-status-operation-not-unique",
            f"{card_id}@{effective_upgrade}:{enchantment_id!r}:count={len(operations)}",
        )
    program = operations[0].program
    if program.trigger.phase not in {
        PHASE_END_TURN,
        PHASE_START_TURN,
        PHASE_PLAY_COUNT_INTERVAL,
        PHASE_CARD_PLAY,
        PHASE_CARD_PLAY_AFTER,
        PHASE_STATUS_CHANGE,
    }:
        raise _CardStatusRestoreError(
            "local-save-card-status-trigger-family-unmapped", program.trigger.phase
        )
    expected_effects = tuple(
        _expected_catalog_status_child(value) for value in program.children
    )
    raw_effects = data.get("_effectList")
    trigger = data.get("_trigger")
    if program.trigger.phase == PHASE_PLAY_COUNT_INTERVAL:
        expected_phase_type = _PLAY_COUNT_INTERVAL_PHASE_TYPE_VALUE
        phase_type_names = {
            _PLAY_COUNT_INTERVAL_PHASE_TYPE_VALUE: PHASE_PLAY_COUNT_INTERVAL
        }
    elif program.trigger.phase == PHASE_END_TURN:
        expected_phase_type = _END_TURN_PHASE_TYPE_VALUE
        phase_type_names = {}
    elif program.trigger.phase == PHASE_START_TURN:
        expected_phase_type = 4
        phase_type_names = {}
    elif program.trigger.phase == PHASE_CARD_PLAY:
        expected_phase_type = 2
        phase_type_names = {}
    elif program.trigger.phase == PHASE_STATUS_CHANGE:
        expected_phase_type = 13
        phase_type_names = {13: PHASE_STATUS_CHANGE}
    else:
        expected_phase_type = 3
        phase_type_names = {}
    progress = _restore_trigger_serializer_progress(
        data,
        master_turn=program.turn,
        master_total_limit=program.total_limit,
        master_per_turn_limit=program.per_turn_limit,
        phase_type_names=phase_type_names,
        label=f"card-status:{source_guid}",
        error_code="local-save-card-status-master-mismatch",
        phase_error_code="local-save-card-status-phase-count-invalid",
    )
    uid = _restore_positive_uid(data, "TriggerEffectStatusEffect")
    empty_item_is_identity_empty = is_identity_empty_trigger_item_scratch(
        data.get("_triggerItem")
    )
    if (
        set(data) != set(_TRIGGER_STATUS_FIELDS)
        or data.get("_isItemDirectEnchant") is not False
        or not isinstance(trigger, Mapping)
        or dict(trigger)
        != {
            "_id": program.trigger.trigger_id,
            "_phaseTypeList": [expected_phase_type],
            "_phaseValueList": list(program.trigger.phase_values),
        }
        or not isinstance(raw_effects, list)
        or len(raw_effects) != len(expected_effects)
        or any(
            not isinstance(raw, Mapping)
            or set(raw) != set(_RAW_CHILD_EFFECT_FIELDS)
            or dict(raw) != expected
            for raw, expected in zip(raw_effects, expected_effects, strict=True)
        )
        or data.get("_originType") != 1
        or data.get("_overrideType") != 31
        or data.get("_fromExamEffectIdList") != []
        or data.get("_descriptionReactiveDataTextList") != []
        or data.get("_descriptionReactiveDataTypeList") != []
        or data.get("_isEncoreEnchant") is not False
        or data.get("_isFromEnchantEffect") is not False
        or data.get("_startEnchantOriginId") != ""
        or data.get("_startEnchantOriginLevel") != 0
        or data.get("_startEnchantOriginType") != 0
        or data.get("_startEnchantOwnerId") != ""
        or data.get("_triggerDrink") != {"_id": ""}
        or data.get("_triggerGimmickGroup") != _EMPTY_TRIGGER_GIMMICK
        # Empty TriggerItem is serializer scratch state.  Its counters may be
        # non-zero after earlier transactions and do not change this card
        # listener's identity or phase semantics.
        or not empty_item_is_identity_empty
    ):
        raise _CardStatusRestoreError(
            "local-save-card-status-master-mismatch", str(enchantment_id)
        )
    return Plan2NativeStatusEnchantListener(
        status_uid=uid,
        source_guid=source_guid,
        source_card_id=card_id,
        source_upgrade=effective_upgrade,
        play_origin="normal",
        program=program,
        turns_remaining=progress.turn,
        total_remaining=progress.limit_count,
        per_turn_remaining=progress.limit_count_in_turn_remaining,
        passing_turn_start=progress.is_passing_turn_start,
        phase_counts=progress.phase_counts,
        turn_count=progress.turn_count,
    )


def _restore_gimmick_status_listener(
    data: Mapping[str, object],
    *,
    database: Path = DEFAULT_DATABASE,
) -> Plan2NativeStatusEnchantListener:
    """Restore the exact permanent listener installed by a Master gimmick."""

    raw_group = data.get("_triggerGimmickGroup")
    if not isinstance(raw_group, Mapping) or set(raw_group) != set(
        _EMPTY_TRIGGER_GIMMICK
    ):
        raise _CardStatusRestoreError(
            "local-save-gimmick-status-source-shape-invalid", "gimmickGroup"
        )
    group_id = raw_group.get("_id")
    priority = raw_group.get("_priority")
    start_turn = raw_group.get("_startTurn")
    raw_gimmick_effect = raw_group.get("_gimmickEffect")
    effect_id = (
        raw_gimmick_effect.get("_effectId")
        if isinstance(raw_gimmick_effect, Mapping)
        else None
    )
    if (
        not isinstance(group_id, str)
        or not group_id
        or isinstance(priority, bool)
        or not isinstance(priority, int)
        or priority < 1
        or isinstance(start_turn, bool)
        or not isinstance(start_turn, int)
        or start_turn < 1
        or not isinstance(effect_id, str)
        or not effect_id
    ):
        raise _CardStatusRestoreError(
            "local-save-gimmick-status-source-identity-invalid"
        )

    from .initial_regular_plan2_gimmick_runtime import (
        InitialRegularPlan2GimmickRuntimeBlocked,
        load_plan2_master_gimmick_status_enchant_step,
    )

    try:
        # The ExamSave carries the group/priority/effect identity.  Resolve
        # that identity against the single-row Master status-enchant loader,
        # rather than requiring every sibling row in the group to belong to
        # the Review family.  This keeps the restore exact when a sibling
        # direct gimmick effect is outside the current simulator slice.
        program = load_plan2_master_gimmick_status_enchant_step(
            group_id,
            priority,
            start_turn,
            effect_id,
            database=Path(database),
        )
    except InitialRegularPlan2GimmickRuntimeBlocked as error:
        if any(
            getattr(value, "code", "") == "plan2-gimmick-status-step-not-unique"
            for value in error.blockers
        ):
            detail = ";".join(
                value.detail for value in error.blockers if value.detail
            )
            raise _CardStatusRestoreError(
                "local-save-gimmick-status-program-not-unique",
                detail,
            ) from error
        if any(
            getattr(value, "code", "") == "plan2-gimmick-group-row-missing"
            for value in error.blockers
        ):
            raise _CardStatusRestoreError(
                "local-save-gimmick-status-group-unsupported", group_id
            ) from error
        raise _CardStatusRestoreError(
            "local-save-gimmick-status-master-unavailable", str(error)
        ) from error
    expected_group = {
        "_gimmickEffect": {"_effectId": effect_id},
        "_gimmickTrigger": {
            "_id": "",
            "_phaseTypeList": [],
            "_phaseValueList": [],
        },
        "_id": group_id,
        "_isPositive": True,
        "_priority": priority,
        "_remainingTurn": 0,
        "_remainingTurnPermil": 0,
        "_startTurn": start_turn,
    }
    raw_effects = data.get("_effectList")
    expected_effects = tuple(
        _expected_catalog_status_child(value) for value in program.children
    )
    # The serialized enum value is part of the native Trigger object.  Keep
    # the mapping explicit; in particular, StatusChange installers use 13
    # while PlayCountInterval installers use 23.  Reusing 23 for every
    # gimmick status would reject otherwise exact ExamSave rows (and would
    # misinterpret their phase counters).
    phase_type_values = {
        PHASE_END_TURN: _END_TURN_PHASE_TYPE_VALUE,
        PHASE_STATUS_CHANGE: 13,
        PHASE_PLAY_COUNT_INTERVAL: _PLAY_COUNT_INTERVAL_PHASE_TYPE_VALUE,
        PHASE_CARD_PLAY: 2,
        PHASE_CARD_PLAY_AFTER: 3,
    }
    phase = program.trigger.phase
    expected_phase_type = phase_type_values.get(phase)
    if expected_phase_type is None:
        raise _CardStatusRestoreError(
            "local-save-gimmick-status-trigger-family-unmapped", phase
        )
    phase_type_names = (
        {expected_phase_type: phase}
        if phase == PHASE_PLAY_COUNT_INTERVAL
        else {}
    )
    progress = _restore_trigger_serializer_progress(
        data,
        master_turn=program.turn,
        master_total_limit=program.total_limit,
        master_per_turn_limit=program.per_turn_limit,
        phase_type_names=phase_type_names,
        label=f"gimmick-status:{group_id}:priority={priority}",
        error_code="local-save-gimmick-status-master-mismatch",
        phase_error_code="local-save-gimmick-status-phase-count-invalid",
    )

    uid = _restore_positive_uid(data, "TriggerEffectStatusEffect")
    expected_trigger = {
        "_id": program.trigger.trigger_id,
        "_phaseTypeList": [expected_phase_type],
        "_phaseValueList": list(program.trigger.phase_values),
    }
    empty_item_is_identity_empty = is_identity_empty_trigger_item_scratch(
        data.get("_triggerItem")
    )
    if (
        set(data) != set(_TRIGGER_STATUS_FIELDS)
        or dict(raw_group) != expected_group
        or data.get("_triggerCard") != _EMPTY_TRIGGER_CARD
        or data.get("_triggerDrink") != {"_id": ""}
        or not empty_item_is_identity_empty
        or data.get("_isItemDirectEnchant") is not False
        or data.get("_statusEnchantId") != program.status_enchant_id
        or data.get("_trigger") != expected_trigger
        or not isinstance(raw_effects, list)
        or len(raw_effects) != len(expected_effects)
        or any(
            not isinstance(raw, Mapping)
            or set(raw) != set(_RAW_CHILD_EFFECT_FIELDS)
            or dict(raw) != expected
            for raw, expected in zip(raw_effects, expected_effects, strict=True)
        )
        or data.get("_originType") != 1
        or data.get("_overrideType") != 31
        or data.get("_fromExamEffectIdList") != []
        or data.get("_descriptionReactiveDataTextList") != []
        or data.get("_descriptionReactiveDataTypeList") != []
        or data.get("_isEncoreEnchant") is not False
        or data.get("_isFromEnchantEffect") is not False
        or data.get("_startEnchantOriginId") != ""
        or data.get("_startEnchantOriginLevel") != 0
        or data.get("_startEnchantOriginType") != 0
        or data.get("_startEnchantOwnerId") != ""
    ):
        raise _CardStatusRestoreError(
            "local-save-gimmick-status-master-mismatch",
            f"{group_id}:priority={priority}:effect={effect_id}",
        )
    source_id = f"gimmick:{group_id}:priority={priority}"
    return Plan2NativeStatusEnchantListener(
        status_uid=uid,
        source_guid=source_id,
        source_card_id=program.card_id,
        source_upgrade=program.upgrade,
        play_origin="normal",
        program=program,
        turns_remaining=progress.turn,
        total_remaining=progress.limit_count,
        per_turn_remaining=progress.limit_count_in_turn_remaining,
        passing_turn_start=progress.is_passing_turn_start,
        phase_counts=progress.phase_counts,
        turn_count=progress.turn_count,
    )


def _restore_drink_status_listener(
    data: Mapping[str, object],
    *,
    database: Path = DEFAULT_DATABASE,
) -> Plan2NativeStatusEnchantListener:
    """Restore one ``TriggerEffectStatusEffect`` owned by a drink.

    Drink-owned listeners have the same native StatusEnchant serializer as
    card/gimmick listeners, but their source identity is carried by
    ``_triggerDrink`` rather than ``_triggerCard`` or
    ``_triggerGimmickGroup``.  Resolve the source through the ordered Master
    drink references and compile the status graph from Master; the captured
    serializer remains authoritative for UID, lifetime, and progress.
    """

    if set(data) != set(_TRIGGER_STATUS_FIELDS):
        raise _CardStatusRestoreError(
            "local-save-drink-status-shape-invalid", "fields"
        )
    raw_drink = data.get("_triggerDrink")
    if (
        not isinstance(raw_drink, Mapping)
        or set(raw_drink) != {"_id"}
        or not isinstance(raw_drink.get("_id"), str)
        or not raw_drink.get("_id")
    ):
        raise _CardStatusRestoreError(
            "local-save-drink-status-source-identity-invalid", "triggerDrink"
        )
    drink_id = raw_drink["_id"]
    status_id = data.get("_statusEnchantId")
    raw_trigger = data.get("_trigger")
    if not isinstance(status_id, str) or not status_id:
        raise _CardStatusRestoreError(
            "local-save-drink-status-source-identity-invalid", "statusEnchantId"
        )
    if not isinstance(raw_trigger, Mapping) or set(raw_trigger) != set(
        _TRIGGER_FIELDS
    ):
        raise _CardStatusRestoreError(
            "local-save-drink-status-trigger-invalid", drink_id
        )
    trigger_id = raw_trigger.get("_id")
    if not isinstance(trigger_id, str) or not trigger_id:
        raise _CardStatusRestoreError(
            "local-save-drink-status-trigger-invalid", drink_id
        )

    try:
        drink = load_drink_catalog(Path(database)).get_drink(drink_id)
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise _CardStatusRestoreError(
            "local-save-drink-status-master-unavailable",
            f"{drink_id}:{type(error).__name__}:{error}",
        ) from error
    matches = tuple(
        reference
        for reference in drink.effect_refs
        if (
            reference.link.source_kind == "exam"
            and reference.effect.effect_type == EFFECT_STATUS_ENCHANT
            and reference.effect.produce_exam_status_enchant_id == status_id
            and reference.effect.produce_exam_trigger_id == trigger_id
        )
    )
    if len(matches) != 1:
        raise _CardStatusRestoreError(
            "local-save-drink-status-program-not-unique",
            f"{drink_id}:{status_id}:{trigger_id}:count={len(matches)}",
        )
    effect_id = matches[0].effect.source_id
    try:
        program = load_plan2_native_status_enchant_installer_effect(
            effect_id,
            source_id=drink_id,
            source_upgrade=0,
            database=Path(database),
        )
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise _CardStatusRestoreError(
            "local-save-drink-status-master-mismatch",
            f"{drink_id}:{effect_id}:{type(error).__name__}:{error}",
        ) from error

    # The typed loader proves the complete trigger/status/child graph.  Keep
    # the source and captured serializer identities pinned as well, so a
    # different status on the same drink cannot be silently accepted.
    if (
        program.card_id != drink_id
        or program.upgrade != 0
        or program.installer_effect_id != effect_id
        or program.status_enchant_id != status_id
        or program.trigger.trigger_id != trigger_id
        or program.trigger.phase not in {
            PHASE_END_TURN,
            PHASE_START_TURN,
            PHASE_START_PLAY,
            PHASE_CARD_PLAY,
            PHASE_CARD_PLAY_AFTER,
            PHASE_STATUS_CHANGE,
            PHASE_PLAY_COUNT_INTERVAL,
            PHASE_TURN_TIMER,
            PHASE_STAMINA_REDUCE_CARD,
        }
    ):
        raise _CardStatusRestoreError(
            "local-save-drink-status-master-mismatch",
            f"{drink_id}:{effect_id}",
        )

    # Trigger phase values are enum-backed in the serializer.  Keep this
    # mapping explicit instead of accepting an arbitrary integer from the
    # save; interval phases additionally authorize their phase-count keys.
    phase_type_values = {
        PHASE_END_TURN: _END_TURN_PHASE_TYPE_VALUE,
        PHASE_START_TURN: 4,
        PHASE_START_PLAY: 27,
        PHASE_CARD_PLAY: 2,
        PHASE_CARD_PLAY_AFTER: 3,
        PHASE_STATUS_CHANGE: 13,
        PHASE_PLAY_COUNT_INTERVAL: _PLAY_COUNT_INTERVAL_PHASE_TYPE_VALUE,
        PHASE_TURN_TIMER: 21,
        PHASE_STAMINA_REDUCE_CARD: 20,
    }
    expected_phase_type = phase_type_values[program.trigger.phase]
    phase_type_names = (
        {expected_phase_type: program.trigger.phase}
        if program.trigger.phase
        in {PHASE_PLAY_COUNT_INTERVAL, PHASE_TURN_TIMER}
        else {}
    )
    progress = _restore_trigger_serializer_progress(
        data,
        master_turn=program.turn,
        master_total_limit=program.total_limit,
        master_per_turn_limit=program.per_turn_limit,
        phase_type_names=phase_type_names,
        label=f"drink-status:{drink_id}:{status_id}",
        error_code="local-save-drink-status-master-mismatch",
        phase_error_code="local-save-drink-status-phase-count-invalid",
    )

    raw_effects = data.get("_effectList")
    expected_effects = tuple(
        _expected_catalog_status_child(value) for value in program.children
    )
    empty_item_is_identity_empty = is_identity_empty_trigger_item_scratch(
        data.get("_triggerItem")
    )
    expected_trigger = {
        "_id": program.trigger.trigger_id,
        "_phaseTypeList": [expected_phase_type],
        "_phaseValueList": list(program.trigger.phase_values),
    }
    if (
        data.get("_triggerCard") != _EMPTY_TRIGGER_CARD
        or data.get("_triggerGimmickGroup") != _EMPTY_TRIGGER_GIMMICK
        or data.get("_isItemDirectEnchant") is not False
        or data.get("_statusEnchantId") != program.status_enchant_id
        or dict(raw_trigger) != expected_trigger
        or not isinstance(raw_effects, list)
        or len(raw_effects) != len(expected_effects)
        or any(
            not isinstance(raw, Mapping)
            or set(raw) != set(_RAW_CHILD_EFFECT_FIELDS)
            or dict(raw) != expected
            for raw, expected in zip(raw_effects, expected_effects, strict=True)
        )
        or data.get("_originType") != 1
        or data.get("_overrideType") != 31
        or data.get("_fromExamEffectIdList") != []
        or data.get("_descriptionReactiveDataTextList") != []
        or data.get("_descriptionReactiveDataTypeList") != []
        or data.get("_isEncoreEnchant") is not False
        or data.get("_isFromEnchantEffect") is not False
        or data.get("_startEnchantOriginId") != ""
        or data.get("_startEnchantOriginLevel") != 0
        or data.get("_startEnchantOriginType") != 0
        or data.get("_startEnchantOwnerId") != ""
        or not empty_item_is_identity_empty
    ):
        raise _CardStatusRestoreError(
            "local-save-drink-status-master-mismatch",
            f"{drink_id}:{effect_id}",
        )
    uid = _restore_positive_uid(data, "TriggerEffectStatusEffect")
    return Plan2NativeStatusEnchantListener(
        status_uid=uid,
        source_guid=f"drink:{drink_id}",
        source_card_id=drink_id,
        source_upgrade=0,
        play_origin="normal",
        program=program,
        turns_remaining=progress.turn,
        total_remaining=progress.limit_count,
        per_turn_remaining=progress.limit_count_in_turn_remaining,
        passing_turn_start=progress.is_passing_turn_start,
        phase_counts=progress.phase_counts,
        turn_count=progress.turn_count,
    )


def _restore_card_trigger_listener(
    exam: LocalSaveExamState,
    data: Mapping[str, object],
    catalog: Plan2NativeProgramCatalog,
) -> Plan2EndTurnListener:
    if set(data) != set(_TRIGGER_STATUS_FIELDS):
        raise _CardStatusRestoreError(
            "local-save-card-status-shape-invalid", "fields"
        )
    if data.get("_isItemDirectEnchant") is not False:
        raise _CardStatusRestoreError(
            "local-save-card-status-origin-invalid", "not-card"
        )
    source_guid, card_id, effective_upgrade = _restore_trigger_source_card(
        exam, data.get("_triggerCard")
    )
    programs = tuple(
        value
        for value in catalog.programs
        if value.ref == (card_id, effective_upgrade)
    )
    if len(programs) != 1:
        raise _CardStatusRestoreError(
            "local-save-card-status-program-not-unique",
            f"{card_id}@{effective_upgrade}:count={len(programs)}",
        )
    executor = programs[0].native_playing_executor
    if not isinstance(executor, Plan2MasterPlayingExecutor):
        raise _CardStatusRestoreError(
            "local-save-card-status-playing-compiler-unmapped",
            f"{card_id}@{effective_upgrade}",
        )
    enchantment_id = data.get("_statusEnchantId")
    operations = tuple(
        value
        for value in executor.operations
        if isinstance(value, Plan2MasterRemainingThreeInstallOperation)
        and value.program.listener_program.status_enchant_id == enchantment_id
    )
    if len(operations) != 1:
        raise _CardStatusRestoreError(
            "local-save-card-status-operation-not-unique",
            f"{card_id}@{effective_upgrade}:{enchantment_id!r}:count={len(operations)}",
        )
    operation = operations[0]
    uid = _restore_positive_uid(data, "TriggerEffectStatusEffect")
    installed = install_plan2_end_turn_remaining_three_exact_listener(
        Plan2State(next_status_uid=uid), operation.program
    )
    listener = installed.after.end_turn_listeners[0]
    trigger = data.get("_trigger")
    raw_effects = data.get("_effectList")
    expected_effects = tuple(
        _expected_remaining_three_child(value) for value in listener.effects
    )
    if (
        operation.effect_id != listener.wrapper_effect_id
        or not isinstance(trigger, Mapping)
        or set(trigger) != set(_TRIGGER_FIELDS)
        or operation.program.trigger.phase_types
        != ("ProduceExamPhaseType_ExamEndTurn",)
        or operation.program.trigger.phase_values
        or dict(trigger)
        != {
            "_id": listener.trigger_id,
            "_phaseTypeList": [_END_TURN_PHASE_TYPE_VALUE],
            "_phaseValueList": [],
        }
        or not isinstance(raw_effects, list)
        or len(raw_effects) != len(expected_effects)
        or any(
            not isinstance(raw, Mapping)
            or set(raw) != set(_RAW_CHILD_EFFECT_FIELDS)
            or dict(raw) != expected
            for raw, expected in zip(raw_effects, expected_effects, strict=True)
        )
    ):
        raise _CardStatusRestoreError(
            "local-save-card-status-master-mismatch", listener.status_enchant_id
        )
    progress = _restore_trigger_serializer_progress(
        data,
        master_turn=listener.turn,
        master_total_limit=listener.limit_count,
        master_per_turn_limit=listener.limit_count_in_turn,
        phase_type_names={},
        label=f"card-status:{source_guid}",
        error_code="local-save-card-status-runtime-mismatch",
        phase_error_code="local-save-card-status-phase-count-invalid",
    )
    trigger_item_is_empty = is_identity_empty_trigger_item_scratch(
        data.get("_triggerItem")
    )
    if (
        data.get("_descriptionReactiveDataTextList") != []
        or data.get("_descriptionReactiveDataTypeList") != []
        or data.get("_fromExamEffectIdList") != []
        or data.get("_isEncoreEnchant") is not False
        or data.get("_isFromEnchantEffect") is not False
        or data.get("_originType") != 1
        or data.get("_overrideType") != 31
        or data.get("_startEnchantOriginId") != ""
        or data.get("_startEnchantOriginLevel") != 0
        or data.get("_startEnchantOriginType") != 0
        or data.get("_startEnchantOwnerId") != ""
        or enchantment_id != listener.status_enchant_id
        or data.get("_triggerDrink") != {"_id": ""}
        or data.get("_triggerGimmickGroup") != _EMPTY_TRIGGER_GIMMICK
        # Empty TriggerItem is a serializer scratch object.  Its identity is
        # empty, while fire/reaction counters may retain non-negative values
        # from earlier transactions; those counters do not own this card
        # listener's trigger semantics.
        or not trigger_item_is_empty
    ):
        raise _CardStatusRestoreError(
            "local-save-card-status-runtime-mismatch", source_guid
        )
    return replace(
        listener,
        turn=progress.turn,
        limit_count=progress.limit_count,
        limit_count_in_turn_remaining=progress.limit_count_in_turn_remaining,
        turn_count=progress.turn_count,
        is_passing_turn_start=progress.is_passing_turn_start,
        phase_counts=progress.phase_counts,
    )


def _restore_removed_playable_tombstone(
    rid: int,
    reference: tuple[str, Mapping[str, object]] | None,
    blockers: list[Plan2NativeBlocker],
) -> int | None:
    if reference is None:
        _block(blockers, "local-save-removed-status-reference-missing", str(rid))
        return None
    class_name, data = reference
    if class_name == _REVIEW_CLASS:
        exact = _exact_mapping(
            data,
            _REVIEW_FIELDS,
            blockers,
            "local-save-removed-review-status-invalid",
            str(rid),
        )
        if exact is None:
            return None
        try:
            uid = _plain_int(exact["_uid"], "removed Review._uid", minimum=1)
            turn = _plain_int(exact["_turn"], "removed Review._turn", minimum=0)
        except ValueError as error:
            _block(blockers, "local-save-removed-review-status-invalid", str(error))
            return None
        # Review is moved to the removed list after its final TurnStart spend.
        # It is a serializer tombstone only: turn has reached zero and it is
        # no longer passing a TurnStart.  Preserve the UID/create-count audit,
        # but do not reconstruct an active runtime status from it.
        if (
            turn != 0
            or exact["_isPassingTurnStart"] is not False
            or exact["_isTurnLimited"] is not True
        ):
            _block(
                blockers,
                "local-save-removed-review-status-invalid",
                f"{rid}:{dict(exact)!r}",
            )
            return None
        return uid
    if class_name != _PLAYABLE_CLASS:
        # Removed effects are inert serializer tombstones.  Their concrete
        # class no longer contributes any future gameplay semantics; only the
        # allocator UID must remain accounted for.  Requiring an executor for
        # every historical scalar status (for example spent Aggressive) made
        # an otherwise settled Save impossible to resume.
        try:
            return _plain_int(
                data.get("_uid"),
                f"removed {class_name}._uid",
                minimum=1,
            )
        except ValueError as error:
            _block(
                blockers,
                "local-save-removed-status-uid-invalid",
                f"{rid}:{error}",
            )
            return None
    exact = _exact_mapping(
        data,
        _SCALAR_FIELDS,
        blockers,
        "local-save-removed-playable-status-invalid",
        str(rid),
    )
    if exact is None:
        return None
    try:
        uid = _plain_int(exact["_uid"], "removed Playable._uid", minimum=1)
        turn = _plain_int(exact["_turn"], "removed Playable._turn", minimum=1)
    except ValueError as error:
        _block(blockers, "local-save-removed-playable-status-invalid", str(error))
        return None
    if (
        type(exact["_isPassingTurnStart"]) is not bool
        or exact["_isTurnLimited"] is not True
        or exact["_value"] != 0
        or turn < 1
    ):
        _block(
            blockers,
            "local-save-removed-playable-status-invalid",
            f"{rid}:{dict(exact)!r}",
        )
        return None
    return uid


def _restore_search_play_stamina_status(data: Mapping[str, object], *, database: Path = DEFAULT_DATABASE) -> SearchPlayCardStaminaStatus:
    from .plan2_search_play_card_stamina_consumption_change import restore_search_play_card_stamina_status
    return restore_search_play_card_stamina_status(data, database=database)


def _retired_timer_effects(data: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    trigger = data.get("_trigger")
    if (data.get("_statusEnchantId") != "" or data.get("_originType") != 2 or data.get("_overrideType") != 30
            or type(data.get("_limitCount")) is not int or data["_limitCount"] != 0
            or not isinstance(trigger, Mapping) or trigger.get("_id") != "" or trigger.get("_phaseTypeList") != [21]):
        return ()
    phases = trigger.get("_phaseValueList")
    effects = data.get("_effectList")
    if (not isinstance(phases, list) or len(phases) != 1 or type(phases[0]) is not int or phases[0] < 1
            or type(data.get("_turnCount")) is not int or data["_turnCount"] < phases[0]
            or not isinstance(effects, list) or not effects or any(not isinstance(e, Mapping) for e in effects)):
        return ()
    return tuple(effects)


def _proven_fired_timer_uids(opaque: Mapping[str, object], current_turn: int,
    retired: Mapping[int, tuple[Mapping[str, object], ...]]) -> frozenset[int]:
    """Bind the timer display marker to an already committed native child.

    A bare !EFFECT_TIMER string is never sufficient: require the removed,
    exhausted timer UID and the exact same effect in this turn's play log.
    """
    logs = opaque.get("playLogList")
    if not isinstance(logs, list):
        return frozenset()
    proven = set()
    for log in logs:
        if not isinstance(log, Mapping) or log.get("_currentTurn") != current_turn:
            continue
        command = log.get("_command")
        if not isinstance(command, Mapping) or command.get("_isManual") is not False or command.get("_playType") != 5:
            continue
        uid = command.get("_enchantEffectUid")
        if type(uid) is int and uid in retired and any(command.get("_playEffect") == effect for effect in retired[uid]):
            proven.add(uid)
    return frozenset(proven)


def _status_projection(
    exam: LocalSaveExamState,
    opaque: Mapping[str, object],
    catalog: Plan2NativeProgramCatalog,
    blockers: list[Plan2NativeBlocker],
    *,
    database: Path = DEFAULT_DATABASE,
    observe_external_effects: bool = False,
    source_sha256: str = "",
) -> _StatusProjection | None:
    status = _exact_mapping(
        opaque.get("status"),
        _STATUS_FIELDS,
        blockers,
        "local-save-status-graph-invalid",
        "status",
    )
    references = _exact_mapping(
        opaque.get("references"),
        _REFERENCE_FIELDS,
        blockers,
        "local-save-status-references-invalid",
        "references",
    )
    if status is None or references is None:
        return None

    raw_idol = status["_idolStatusEffect"]
    if isinstance(raw_idol, Mapping) and dict(raw_idol) == {"rid": -2}:
        # Newtonsoft's empty-reference sentinel is the other observed exact
        # encoding of the neutral idol status.
        idol = None
    else:
        idol = _exact_mapping(
            raw_idol,
            _IDOL_STATUS_FIELDS,
            blockers,
            "local-save-idol-status-invalid",
            "status._idolStatusEffect",
        )
        if idol is not None and dict(idol) != {
            "_currentType": 0,
            "_currentStep": 0,
        }:
            _block(
                blockers,
                "local-save-idol-status-unmapped",
                repr(dict(idol)),
            )

    active_rids = _links(
        status["_effectList"], label="status._effectList", blockers=blockers
    )
    removed_rids = _links(
        status["_removedEffectList"],
        label="status._removedEffectList",
        blockers=blockers,
    )
    if set(active_rids) & set(removed_rids):
        _block(blockers, "local-save-status-active-removed-overlap")

    try:
        create_count = _plain_int(
            status["_effectCreateCount"], "status._effectCreateCount"
        )
    except ValueError as error:
        _block(blockers, "local-save-status-create-count-invalid", str(error))
        return None
    # ``_effectCreateCount`` is the monotonically increasing UID allocator,
    # not a count of objects that must still be retained by the serializer.
    # The game may garbage-collect an expired status after it has left both
    # active and removed lists.  Linked objects therefore form a subset of
    # the historical allocation range.
    if create_count < len(active_rids) + len(removed_rids):
        _block(
            blockers,
            "local-save-status-create-count-mismatch",
            f"count={create_count};linked={len(active_rids) + len(removed_rids)}",
        )

    raw_entries = references.get("RefIds")
    if not isinstance(raw_entries, list):
        _block(blockers, "local-save-status-references-invalid", "RefIds")
        return None
    by_rid: dict[int, tuple[str, Mapping[str, object]]] = {}
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, Mapping) or set(raw) != set(_REFERENCE_ENTRY_FIELDS):
            _block(
                blockers,
                "local-save-status-reference-entry-invalid",
                str(index),
            )
            continue
        raw_type = raw.get("type")
        data = raw.get("data")
        if not isinstance(raw_type, Mapping) or set(raw_type) != set(
            _REFERENCE_TYPE_FIELDS
        ) or not isinstance(data, Mapping):
            _block(
                blockers,
                "local-save-status-reference-entry-invalid",
                str(index),
            )
            continue
        rid = raw.get("rid")
        if isinstance(rid, bool) or not isinstance(rid, int):
            _block(
                blockers,
                "local-save-status-reference-rid-invalid",
                str(index),
            )
            continue
        if rid in by_rid:
            _block(blockers, "local-save-status-reference-duplicate", str(rid))
            continue
        if rid > 0:
            if raw_type.get("asm") != _ASSEMBLY or raw_type.get("ns") != _NAMESPACE:
                _block(
                    blockers,
                    "local-save-status-reference-type-invalid",
                    str(rid),
                )
                continue
            class_name = raw_type.get("class")
            if not isinstance(class_name, str) or not class_name:
                _block(
                    blockers,
                    "local-save-status-reference-type-invalid",
                    str(rid),
                )
                continue
            by_rid[rid] = (class_name, data)

    reviews: list[tuple[int, bool, int]] = []
    aggressives: list[tuple[int, int]] = []
    aggressive_additive_layers: list[AggressiveAdditiveLayer] = []
    playable_values: list[tuple[int, int]] = []
    stamina_down_layers: list[StaminaConsumptionDownLayer] = []
    stamina_down_fix_layers: list[StaminaConsumptionDownFixLayer] = []
    stamina_add_layers: list[StaminaConsumptionAddLayer] = []
    search_stamina_statuses: list[SearchPlayCardStaminaStatus] = []
    end_turn_listeners: list[Plan2EndTurnListener] = []
    status_enchant_listeners: list[Plan2NativeStatusEnchantListener] = []
    active_classes: list[str] = []
    active_uids: list[int] = []
    removed_uids: list[int] = []
    removed_timers: dict[int, tuple[Mapping[str, object], ...]] = {}
    item_statuses = tuple(
        (rid, by_rid[rid][1])
        for rid in active_rids
        if (
            rid in by_rid
            and by_rid[rid][0] == _TRIGGER_CLASS
            and by_rid[rid][1].get("_isItemDirectEnchant") is True
        )
    )
    item_compilation = restore_plan2_native_item_runtime(
        opaque.get("itemList"),
        item_statuses,
        lesson_step_type_value=(
            exam.step_type_value
            if exam.exam_type == PLAN2_LOCAL_SAVE_LESSON_EXAM_TYPE
            else None
        ),
    )
    if not item_compilation.supported and observe_external_effects:
        # The live unattended path replans from the next durable ExamSave
        # after every input.  Game-owned listeners that the local evaluator
        # does not model yet therefore affect ranking precision only; they do
        # not make the current Hand or the MAA input target ambiguous.  Keep
        # equipped source identity, omit only the unresolved listeners, and
        # let the next ExamSave supply their actual scalar result.
        item_runtime = Plan2NativeItemRuntime(
            next_status_uid=max(create_count + 1, 1),
        )
    elif not item_compilation.supported:
        for issue in item_compilation.blockers:
            _block(blockers, "local-save-item-runtime-unmapped", issue)
        item_runtime = Plan2NativeItemRuntime()
    else:
        assert item_compilation.runtime is not None
        item_runtime = item_compilation.runtime
    for native_order, rid in enumerate(active_rids, 1):
        reference = by_rid.get(rid)
        if reference is None:
            _block(blockers, "local-save-active-status-reference-missing", str(rid))
            continue
        class_name, data = reference
        active_classes.append(class_name)
        if class_name == _REVIEW_CLASS:
            exact = _exact_mapping(
                data,
                _REVIEW_FIELDS,
                blockers,
                "local-save-review-status-invalid",
                str(rid),
            )
            if exact is None:
                continue
            try:
                turn = _plain_int(exact["_turn"], "Review._turn", minimum=1)
                uid = _plain_int(exact["_uid"], "Review._uid", minimum=1)
            except ValueError as error:
                _block(blockers, "local-save-review-status-invalid", str(error))
                continue
            passing = exact["_isPassingTurnStart"]
            limited = exact["_isTurnLimited"]
            if type(passing) is not bool or limited is not True:
                _block(blockers, "local-save-review-status-lifecycle-unmapped", str(rid))
                continue
            reviews.append((turn, passing, uid))
            active_uids.append(uid)
        elif class_name == _AGGRESSIVE_CLASS:
            exact = _exact_mapping(
                data,
                _SCALAR_FIELDS,
                blockers,
                "local-save-scalar-status-invalid",
                f"{rid}:{class_name}",
            )
            if exact is None:
                continue
            try:
                value = _plain_int(
                    exact["_value"], f"{class_name}._value", minimum=1
                )
                uid = _plain_int(exact["_uid"], f"{class_name}._uid", minimum=1)
            except ValueError as error:
                _block(blockers, "local-save-scalar-status-invalid", str(error))
                continue
            lifecycle = (
                exact["_isPassingTurnStart"],
                exact["_isTurnLimited"],
                exact["_turn"],
            )
            # Aggressive is a permanent scalar status.  The serializer flips
            # ``_isPassingTurnStart`` from False to True after the status has
            # crossed a turn boundary, but that flag does not change its
            # value or lifetime.  Treat both observed boolean phases as the
            # same native scalar state instead of rejecting an otherwise
            # settled ExamSave.
            if (
                type(exact["_isPassingTurnStart"]) is not bool
                or exact["_isTurnLimited"] is not False
                or exact["_turn"] != -1
            ):
                _block(
                    blockers,
                    "local-save-scalar-status-lifecycle-unmapped",
                    f"{rid}:{class_name}:{lifecycle!r}",
                )
                continue
            aggressives.append((value, uid))
            active_uids.append(uid)
        elif class_name == _AGGRESSIVE_ADDITIVE_CLASS:
            exact = _exact_mapping(data, _SCALAR_FIELDS, blockers, "local-save-aggressive-additive-invalid", str(rid))
            if exact is None:
                continue
            try:
                turn, value = exact["_turn"], exact["_value"]
                uid = _plain_int(exact["_uid"], "AggressiveAdditive._uid", minimum=1)
                if (type(turn) is not int or turn == 0 or turn < -1 or type(value) is not int
                        or not -(2**31) <= value < 2**31 or type(exact["_isPassingTurnStart"]) is not bool
                        or type(exact["_isTurnLimited"]) is not bool or exact["_isTurnLimited"] != (turn != -1)):
                    raise ValueError("native additive value/lifetime fields are invalid")
                # The status object does not serialize its installer identity.
                # Preserve what it does prove, rather than guessing a card or
                # item source from coincidentally equal value/remaining turn.
                aggressive_additive_layers.append(AggressiveAdditiveLayer(uid, value, turn, native_order,
                    source_effect_id="", passing_turn_start=exact["_isPassingTurnStart"],
                    native_rid=rid, native_source_sha256=source_sha256))
                active_uids.append(uid)
            except (TypeError, ValueError) as error:
                _block(blockers, "local-save-aggressive-additive-invalid", f"{rid}:{error}")
        elif class_name == _PLAYABLE_CLASS:
            exact = _exact_mapping(
                data,
                _SCALAR_FIELDS,
                blockers,
                "local-save-scalar-status-invalid",
                f"{rid}:{class_name}",
            )
            if exact is None:
                continue
            try:
                value = _plain_int(
                    exact["_value"], f"{class_name}._value", minimum=1
                )
                uid = _plain_int(exact["_uid"], f"{class_name}._uid", minimum=1)
            except ValueError as error:
                _block(blockers, "local-save-scalar-status-invalid", str(error))
                continue
            if (
                type(exact["_isPassingTurnStart"]) is not bool
                or exact["_isTurnLimited"] is not True
                or exact["_turn"] != 1
            ):
                _block(
                    blockers,
                    "local-save-scalar-status-lifecycle-unmapped",
                    f"{rid}:{class_name}:{dict(exact)!r}",
                )
                continue
            playable_values.append((value, uid))
            active_uids.append(uid)
        elif class_name == _STAMINA_DOWN_CLASS:
            exact = _exact_mapping(
                data,
                _REVIEW_FIELDS,
                blockers,
                "local-save-stamina-down-status-invalid",
                str(rid),
            )
            if exact is None:
                continue
            turn = exact["_turn"]
            uid = exact["_uid"]
            passing = exact["_isPassingTurnStart"]
            limited = exact["_isTurnLimited"]
            if (
                isinstance(turn, bool)
                or not isinstance(turn, int)
                or turn < -1
                or isinstance(uid, bool)
                or not isinstance(uid, int)
                or uid < 1
                or type(passing) is not bool
                or type(limited) is not bool
                or limited != (turn != -1)
            ):
                _block(
                    blockers,
                    "local-save-stamina-down-status-invalid",
                    f"{rid}:{dict(exact)!r}",
                )
                continue
            try:
                stamina_down_layers.append(
                    StaminaConsumptionDownLayer(
                        turn=turn,
                        is_passing_turn_start=passing,
                        status_uid=uid,
                        is_turn_limited=limited,
                    )
                )
            except (TypeError, ValueError) as error:
                _block(
                    blockers,
                    "local-save-stamina-down-status-invalid",
                    f"{rid}:{error}",
                )
                continue
            active_uids.append(uid)
        elif class_name == _STAMINA_DOWN_FIX_CLASS:
            exact = _exact_mapping(
                data,
                _SCALAR_FIELDS,
                blockers,
                "local-save-stamina-down-fix-status-invalid",
                str(rid),
            )
            if exact is None:
                continue
            try:
                value = _plain_int(
                    exact["_value"],
                    "StaminaConsumptionDownFix._value",
                    minimum=1,
                )
                uid = _plain_int(
                    exact["_uid"],
                    "StaminaConsumptionDownFix._uid",
                    minimum=1,
                )
            except ValueError as error:
                _block(
                    blockers,
                    "local-save-stamina-down-fix-status-invalid",
                    str(error),
                )
                continue
            if (
                type(exact["_isPassingTurnStart"]) is not bool
                or exact["_isTurnLimited"] is not False
                or exact["_turn"] != -1
            ):
                _block(
                    blockers,
                    "local-save-stamina-down-fix-status-invalid",
                    f"{rid}:{dict(exact)!r}",
                )
                continue
            stamina_down_fix_layers.append(
                StaminaConsumptionDownFixLayer(value=value, status_uid=uid)
            )
            active_uids.append(uid)
        elif class_name == _STAMINA_ADD_CLASS:
            exact = _exact_mapping(
                data,
                _REVIEW_FIELDS,
                blockers,
                "local-save-stamina-add-status-invalid",
                str(rid),
            )
            if exact is None:
                continue
            turn = exact["_turn"]
            uid = exact["_uid"]
            passing = exact["_isPassingTurnStart"]
            limited = exact["_isTurnLimited"]
            if (
                isinstance(turn, bool)
                or not isinstance(turn, int)
                or turn < -1
                or isinstance(uid, bool)
                or not isinstance(uid, int)
                or uid < 1
                or type(passing) is not bool
                or type(limited) is not bool
                or limited != (turn != -1)
            ):
                _block(
                    blockers,
                    "local-save-stamina-add-status-invalid",
                    f"{rid}:{dict(exact)!r}",
                )
                continue
            try:
                stamina_add_layers.append(
                    StaminaConsumptionAddLayer(
                        turn=turn,
                        is_passing_turn_start=passing,
                        status_uid=uid,
                        is_turn_limited=limited,
                    )
                )
            except (TypeError, ValueError) as error:
                _block(
                    blockers,
                    "local-save-stamina-add-status-invalid",
                    f"{rid}:{error}",
                )
                continue
            active_uids.append(uid)
        elif class_name == "SearchPlayCardStaminaConsumptionChangeStatusEffect":
            try:
                restored_search = _restore_search_play_stamina_status(data, database=database)
            except (ValueError, TypeError, OSError) as error:
                _block(blockers, "local-save-search-play-stamina-status-invalid", f"{rid}:{error}")
                continue
            search_stamina_statuses.append(restored_search)
            active_uids.append(restored_search.status_uid)
        elif class_name == _TRIGGER_CLASS:
            try:
                uid = _restore_positive_uid(data, "TriggerEffectStatusEffect")
            except _CardStatusRestoreError as error:
                _block(
                    blockers,
                    error.code,
                    error.detail,
                )
                continue
            active_uids.append(uid)
            origin = data.get("_isItemDirectEnchant")
            if origin is True:
                # The item runtime compiler above owns exact item-trigger shape.
                continue
            if origin is not False:
                _block(
                    blockers,
                    "local-save-trigger-status-origin-invalid",
                    f"{rid}:{origin!r}",
                )
                continue
            raw_timer_trigger = data.get("_trigger")
            if (
                data.get("_statusEnchantId") == ""
                and isinstance(raw_timer_trigger, Mapping)
                and raw_timer_trigger.get("_phaseTypeList") == [21]
            ):
                # The effect-chain queue owns delayed item/card effects.
                # Do not also classify the same native object as an end-turn
                # or StatusEnchant listener.
                continue
            raw_gimmick = data.get("_triggerGimmickGroup")
            if (
                isinstance(raw_gimmick, Mapping)
                and isinstance(raw_gimmick.get("_id"), str)
                and bool(raw_gimmick.get("_id"))
            ):
                try:
                    status_enchant_listeners.append(
                        _restore_gimmick_status_listener(data, database=database)
                    )
                except _CardStatusRestoreError as error:
                    # A recognized Master gimmick owns future card-play
                    # consequences even in observation-driven mode.  Shape
                    # drift therefore remains a blocker; only wholly unknown
                    # external groups retain the pre-existing observation
                    # fallback.
                    if (
                        error.code
                        != "local-save-gimmick-status-group-unsupported"
                        or not observe_external_effects
                    ):
                        _block(blockers, error.code, error.detail)
                continue
            try:
                end_turn_listeners.append(
                    _restore_card_trigger_listener(exam, data, catalog)
                )
            except _CardStatusRestoreError as error:
                if error.code == "local-save-card-status-operation-not-unique":
                    try:
                        status_enchant_listeners.append(
                            _restore_catalog_status_listener(exam, data, catalog)
                        )
                    except _CardStatusRestoreError as fallback_error:
                        if not observe_external_effects:
                            _block(blockers, fallback_error.code, fallback_error.detail)
                else:
                    if not observe_external_effects:
                        _block(blockers, error.code, error.detail)
        else:
            if (
                class_name not in _SHARED_RUNTIME_STATUS_CLASSES
                and not observe_external_effects
            ):
                _block(
                    blockers,
                    "local-save-active-status-class-unmapped",
                    f"{rid}:{class_name}",
                )
            # In observation-driven live play, unknown active status objects
            # remain owned by the game.  Their current scalar consequences
            # are already present in this ExamSave; future consequences are
            # read from the next one after a single MAA action.
            try:
                uid = _plain_int(
                    data.get("_uid"),
                    f"{class_name}._uid",
                    minimum=1,
                )
            except ValueError:
                uid = None
            if uid is not None:
                active_uids.append(uid)

    if len(reviews) > 1:
        _block(blockers, "local-save-review-status-multiple")
    if len(aggressives) > 1:
        _block(blockers, "local-save-aggressive-status-multiple")
    if len(playable_values) > 1:
        _block(blockers, "local-save-playable-status-multiple")
    if len(stamina_down_layers) > 1:
        _block(blockers, "local-save-stamina-down-status-multiple")
    if len(stamina_add_layers) > 1:
        _block(blockers, "local-save-stamina-add-status-multiple")
    removed_trigger_enchantment_ids: list[str] = []
    for rid in removed_rids:
        reference = by_rid.get(rid)
        if reference is not None and reference[0] == _TRIGGER_CLASS:
            exact = _exact_mapping(
                reference[1],
                _TRIGGER_STATUS_FIELDS,
                blockers,
                "local-save-removed-trigger-status-invalid",
                str(rid),
            )
            uid = None
            if exact is not None:
                raw_uid = exact.get("_uid")
                enchantment_id = exact.get("_statusEnchantId")
                if (
                    isinstance(raw_uid, bool)
                    or not isinstance(raw_uid, int)
                    or raw_uid < 1
                    or not isinstance(enchantment_id, str)
                ):
                    _block(
                        blockers,
                        "local-save-removed-trigger-status-invalid",
                        f"{rid}:uid/status-id",
                    )
                else:
                    uid = raw_uid
                    # Native timer commands are represented by a transient
                    # TriggerEffectStatusEffect whose status-enchant ID is
                    # intentionally empty.  Once it has fired, PC retains the
                    # full object in ``_removedEffectList`` as a tombstone.
                    # The UID/create-count relation remains authoritative;
                    # an empty optional enchantment ID has no future runtime
                    # semantics and must not make a settled save unusable.
                    if enchantment_id:
                        removed_trigger_enchantment_ids.append(enchantment_id)
                    else:
                        effects = _retired_timer_effects(exact)
                        if effects:
                            removed_timers[uid] = effects
        else:
            uid = _restore_removed_playable_tombstone(rid, reference, blockers)
        if uid is not None:
            removed_uids.append(uid)

    linked_uids = active_uids + removed_uids
    if len(linked_uids) != len(set(linked_uids)):
        _block(blockers, "local-save-status-uid-duplicate")
    if active_uids != sorted(active_uids):
        _block(blockers, "local-save-active-status-uid-order-unmapped")
    # Removed effects are serialized in removal chronology, not creation/UID
    # order.  A later-created one-turn status may expire before an older
    # status, so requiring sorted UIDs rejects valid settled saves.  Uniqueness
    # and the create-count range below are the authoritative invariants.
    invalid_uids = tuple(
        uid for uid in linked_uids if uid < 1 or uid > create_count
    )
    if invalid_uids:
        _block(
            blockers,
            "local-save-status-uid-create-count-mismatch",
            f"uids={sorted(linked_uids)};create_count={create_count}",
        )

    review = reviews[0][0] if len(reviews) == 1 else 0
    review_passing = reviews[0][1] if len(reviews) == 1 else False
    aggressive = aggressives[0][0] if len(aggressives) == 1 else 0
    aggressive_additive_runtime = None
    if aggressive_additive_layers:
        catalogs = {id(program.native_playing_executor.aggressive_additive_catalog): program.native_playing_executor.aggressive_additive_catalog
                    for program in catalog.programs if isinstance(program.native_playing_executor, Plan2MasterPlayingExecutor)
                    and program.native_playing_executor.aggressive_additive_catalog is not None}
        if len(catalogs) > 1:
            _block(blockers, "local-save-aggressive-additive-catalog-ambiguous")
        else:
            try:
                additive_catalog = next(iter(catalogs.values()), None) or load_plan2_native_catalog_aggressive_additive(database=database)
                aggressive_additive_runtime = Plan2AggressiveAdditiveRuntime(
                    catalog=additive_catalog, aggressive=aggressive, additive_layers=tuple(aggressive_additive_layers),
                    next_status_uid=max(create_count + 1, max(active_uids, default=0) + 1),
                    next_install_sequence=len(active_rids) + 1, turn_index=exam.current_turn)
            except (TypeError, ValueError, OSError) as error:
                _block(blockers, "local-save-aggressive-additive-runtime-invalid", str(error))
    playable_add = playable_values[0][0] if len(playable_values) == 1 else 0
    plays_remaining = playable_add + (0 if exam.is_turn_card_play_end else 1)
    if exam.is_turn_card_play_end and playable_add:
        _block(blockers, "local-save-turn-end-playable-status-conflict")

    raw_triggered = opaque.get("currentTurnTriggeredStatusEnchantIdList")
    if not isinstance(raw_triggered, list):
        _block(blockers, "local-save-triggered-status-list-invalid")
    elif any(not isinstance(value, str) or not value for value in raw_triggered):
        _block(blockers, "local-save-triggered-status-list-invalid")
    else:
        known_item_enchantments = {
            value.enchantment_id for value in item_runtime.listeners
        } | {
            value.enchantment_id for value in item_runtime.proven_exhausted
        }
        known_card_enchantments = {
            value.status_enchant_id for value in end_turn_listeners
        }
        known_catalog_enchantments = {
            value.status_enchant_id for value in status_enchant_listeners
        }
        known_triggered_enchantments = (
            known_item_enchantments
            | known_card_enchantments
            | known_catalog_enchantments
            | set(removed_trigger_enchantment_ids)
        )
        timer_firings = _proven_fired_timer_uids(opaque, exam.current_turn, removed_timers)
        if 0 < raw_triggered.count("!EFFECT_TIMER") <= len(timer_firings):
            known_triggered_enchantments.add("!EFFECT_TIMER")
        # This list is an ordered firing ledger, not a set of installed
        # listeners.  Distinct status instances may legitimately fire the
        # same enchantment in one turn, so duplicate IDs are meaningful.
        unknown_triggered = tuple(dict.fromkeys(
            value
            for value in raw_triggered
            if value not in known_triggered_enchantments
        ))
        if unknown_triggered and not observe_external_effects:
            _block(
                blockers,
                "local-save-triggered-status-id-unmapped",
                ",".join(unknown_triggered),
            )
    return _StatusProjection(
        review=review,
        review_present=len(reviews) == 1,
        review_passing_turn_start=review_passing,
        aggressive=aggressive,
        stamina_down=(
            stamina_down_layers[0] if len(stamina_down_layers) == 1 else None
        ),
        stamina_down_fix_layers=tuple(stamina_down_fix_layers),
        stamina_add=(
            stamina_add_layers[0] if len(stamina_add_layers) == 1 else None
        ),
        plays_remaining=plays_remaining,
        next_status_uid=max(create_count + 1, max(active_uids, default=0) + 1),
        active_status_classes=tuple(active_classes),
        item_runtime=item_runtime,
        end_turn_listeners=tuple(end_turn_listeners),
        status_enchant_listeners=tuple(status_enchant_listeners),
        aggressive_additive_runtime=aggressive_additive_runtime,
        search_play_card_stamina_runtime=SearchPlayCardStaminaRuntime(
            tuple(search_stamina_statuses), max(create_count + 1, max(active_uids, default=0) + 1)),
    )


def _source_blockers(
    exam: LocalSaveExamState,
    blockers: list[Plan2NativeBlocker],
) -> Mapping[str, object] | None:
    try:
        resolve_plan2_exam_mode(exam.exam_type, exam.step_type_value)
    except Plan2ExamModeError as error:
        _block(blockers, "local-save-exam-mode-unsupported", str(error))
    if exam.phase != PLAN2_LOCAL_SAVE_MAIN_PHASE:
        _block(blockers, "local-save-phase-unrepresentable", str(exam.phase))
    if exam.current_turn > exam.limit_turn + exam.extra_turn:
        _block(blockers, "local-save-terminal-boundary-unrepresentable")
    if exam.zones.hold:
        _block(
            blockers,
            "local-save-hold-zone-unrepresentable",
            ",".join(card.guid for card in exam.zones.hold),
        )
    if exam.playing_card is not None:
        _block(
            blockers,
            "local-save-playing-card-unsettled",
            exam.playing_card.guid,
        )
    if exam.removed_cards and not exam.removed_cards_are_positioned_tombstones:
        _block(blockers, "local-save-removed-cards-unrepresentable")
    if exam.future_deck:
        _block(blockers, "local-save-future-deck-unrepresentable")
    if exam.past_deck is None:
        _block(blockers, "local-save-past-deck-unknown")
    elif exam.past_deck:
        _block(blockers, "local-save-past-deck-unrepresentable")
    root = exam.root_runtime
    if root is None:
        _block(blockers, "local-save-root-runtime-missing")
        return None
    if not root.command_list_is_empty:
        _block(blockers, "local-save-command-queue-unsettled")
    # Draw GUIDs and Grave/Lost flags are cumulative current-turn history.
    # Native stable-Main snapshots retain them while another action is legal;
    # playingCard/commandList own the active transaction boundary above.
    if root.is_exam_end_complete:
        _block(blockers, "local-save-exam-end-complete-unrepresentable")
    opaque = root.opaque_fields.to_value()
    if not isinstance(opaque, Mapping):
        _block(blockers, "local-save-root-runtime-invalid")
        return None
    if opaque.get("planType") != PLAN2_LOCAL_SAVE_PLAN_TYPE:
        _block(blockers, "local-save-not-plan2", repr(opaque.get("planType")))
    return opaque


def _root_counter(
    opaque: Mapping[str, object],
    name: str,
    blockers: list[Plan2NativeBlocker],
) -> int | None:
    try:
        return _plain_int(opaque.get(name), name)
    except ValueError as error:
        _block(blockers, "local-save-root-counter-invalid", str(error))
        return None


def _zone_instances(
    exam: LocalSaveExamState,
    blockers: list[Plan2NativeBlocker],
) -> tuple[
    tuple[NativeOrderedCardInstance, ...],
    tuple[NativeOrderedCardInstance, ...],
    tuple[NativeOrderedCardInstance, ...],
    tuple[NativeOrderedCardInstance, ...],
] | None:
    converted: list[tuple[NativeOrderedCardInstance, ...]] = []
    for zone_name in ("hand", "deck", "grave", "lost"):
        try:
            converted.append(
                tuple(
                    NativeOrderedCardInstance.from_local_save(card)
                    for card in getattr(exam.zones, zone_name)
                )
            )
        except (NativeOrderedZoneError, TypeError, ValueError) as error:
            code = (
                error.code
                if isinstance(error, NativeOrderedZoneError)
                else "local-save-card-runtime-unavailable"
            )
            _block(blockers, code, str(error))
            return None
    return tuple(converted)  # type: ignore[return-value]


def _audit_result(
    *,
    state: Plan2NativeHorizonState | None,
    blockers: Sequence[Plan2NativeBlocker],
    exam: LocalSaveExamState,
    active_status_classes: Sequence[str] = (),
    catalog_refs: Sequence[tuple[str, int]] = (),
) -> Plan2NativeLocalSaveBootstrapAudit:
    mapped_fields = (
        "guid/card_id/base_upgrade/temporary_upgrade/effective_upgrade/play_count",
        "hand/deck/grave/lost",
        "random_state",
        "current_turn/limit_turn/remain_turn/extra_turn",
        "exam_type/step_type/fixed_lesson_parameter/difficulty",
        "score/stamina/max_stamina/block",
        "exam_card_play_count/turn_card_play_count",
        "review/aggressive/plays_remaining",
        "review-additive/lesson-multiple/lesson-multiple-down ordered runtime layers",
        "anti-debuff/effect-timer source/age/status-uid",
        "equipped-item-source/remaining-uses/status-uid",
        "card-status-enchant/end-turn-listener",
        "removed-playable-tombstone/status-uid-create-count",
        "review_consumption_sum/block_consumption_sum_count",
        "total_effect_draw_card_count/judge_parameter/clear_border",
        "scheduled_gimmick_start_turn_hooks",
        "supportCardList/runtime_permil/filterParameterType/cardSearchId/turnUseSupportCardIdList (non-empty rows; explicit [] keeps no-support fixture compatibility)",
        "drinkList/ordered-supported-native-drink-actions/unmapped-slots",
    )
    return Plan2NativeLocalSaveBootstrapAudit(
        PLAN2_NATIVE_LOCAL_SAVE_BOOTSTRAP_SCHEMA_VERSION,
        state,
        tuple(blockers),
        _card_audit(exam),
        tuple(active_status_classes),
        mapped_fields,
        tuple(catalog_refs),
    )


def bootstrap_plan2_native_horizon_from_exam_save(
    exam: LocalSaveExamState,
    *,
    binding: NativeOrderedZoneEvidenceBinding,
    catalog: Plan2NativeProgramCatalog,
    draw_count: int | None,
    hand_limit: int | None,
    database: str | Path = DEFAULT_DATABASE,
    gimmick_hooks: Mapping[
        Plan2ScheduledGimmickHookKey,
        Plan2ScheduledGimmickHook,
    ]
    | None = None,
    observe_external_effects: bool = False,
) -> Plan2NativeLocalSaveBootstrapAudit:
    """Build one dispatchable horizon only when every target field is exact."""

    if not isinstance(exam, LocalSaveExamState):
        raise TypeError("exam must be LocalSaveExamState")
    if not isinstance(binding, NativeOrderedZoneEvidenceBinding):
        raise TypeError("binding must be NativeOrderedZoneEvidenceBinding")
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be Plan2NativeProgramCatalog")
    database = Path(database).resolve()
    if type(observe_external_effects) is not bool:
        raise TypeError("observe_external_effects must be boolean")
    blockers: list[Plan2NativeBlocker] = []
    opaque = _source_blockers(exam, blockers)
    exam_mode: Plan2ExamMode | None = None
    scheduled_gimmicks: tuple[Plan2ScheduledGimmick, ...] = ()
    try:
        exam_mode = resolve_plan2_exam_mode(
            exam.exam_type,
            exam.step_type_value,
        )
    except Plan2ExamModeError:
        # _source_blockers already retained the stable public blocker.
        pass
    if opaque is not None:
        try:
            scheduled_gimmicks = compile_plan2_scheduled_gimmicks(
                opaque.get("gimmickList"),
                hooks=gimmick_hooks,
                maximum_turn=exam.limit_turn + exam.extra_turn,
                completed_through_turn=exam.current_turn,
                observe_unmapped_hooks=observe_external_effects,
            )
        except Plan2ExamModeError as error:
            _block(blockers, error.code, error.detail)

    if draw_count is None:
        _block(blockers, "local-save-draw-count-unavailable")
    else:
        try:
            draw_count = _plain_int(draw_count, "draw_count")
        except ValueError as error:
            _block(blockers, "local-save-draw-count-invalid", str(error))
            draw_count = None
    if hand_limit is None:
        _block(blockers, "local-save-hand-limit-unavailable")
    else:
        try:
            hand_limit = _plain_int(hand_limit, "hand_limit", minimum=1)
        except ValueError as error:
            _block(blockers, "local-save-hand-limit-invalid", str(error))
            hand_limit = None
    if draw_count is not None and hand_limit is not None and draw_count > hand_limit:
        _block(
            blockers,
            "local-save-draw-exceeds-hand-limit",
            f"draw={draw_count};hand_limit={hand_limit}",
        )
    if hand_limit is not None and len(exam.zones.hand) > hand_limit:
        _block(
            blockers,
            "local-save-hand-exceeds-limit",
            f"hand={len(exam.zones.hand)};hand_limit={hand_limit}",
        )

    zones_tuple = _zone_instances(exam, blockers)
    catalog_refs: tuple[tuple[str, int], ...] = ()
    zones: NativeOrderedZoneState | None = None
    if zones_tuple is not None:
        hand, deck, grave, lost = zones_tuple
        all_cards = (*hand, *deck, *grave, *lost)
        if not all_cards:
            _block(blockers, "local-save-card-universe-empty")
        aggregate = canonical_runtime_digest_aggregate(all_cards)
        if binding.runtime_evidence_digest != aggregate:
            _block(
                blockers,
                "local-save-runtime-binding-mismatch",
                f"binding={binding.runtime_evidence_digest};actual={aggregate}",
            )
        catalog_refs = tuple(
            sorted({(card.card_id, card.effective_upgrade) for card in all_cards})
        )
        for card_id, upgrade in catalog_refs:
            representative = next(
                card
                for card in all_cards
                if (card.card_id, card.effective_upgrade) == (card_id, upgrade)
            )
            if catalog.get(representative) is None:
                _block(
                    blockers,
                    "local-save-card-program-missing",
                    f"{card_id}@{upgrade}",
                )
        try:
            zones = NativeOrderedZoneState(
                schema_version=NATIVE_ORDERED_ZONE_SCHEMA_VERSION,
                binding=binding,
                random_state=exam.random_state,
                card_universe=tuple(sorted(all_cards, key=lambda card: card.guid)),
                hand=hand,
                deck=deck,
                grave=grave,
                lost=lost,
            )
        except (NativeOrderedZoneError, TypeError, ValueError) as error:
            code = (
                error.code
                if isinstance(error, NativeOrderedZoneError)
                else "local-save-zone-bootstrap-failed"
            )
            _block(blockers, code, str(error))

    status_projection: _StatusProjection | None = None
    debuff_registry = Plan2NativeDebuffRegistry()
    review_runtime: Plan2NativeReviewDynamicRuntime | None = None
    effect_chains = Plan2NativeEffectChainRuntime()
    support_runtime: ExamHandAddSupportRuntimeProjection | None = None
    drink_runtime = Plan2NativeDrinkRuntime()
    unmapped_drink_slots: list[str] = []
    clear_border = review_sum = block_sum = total_draw = None
    if opaque is not None:
        status_projection = _status_projection(
            exam,
            opaque,
            catalog,
            blockers,
            database=database,
            observe_external_effects=observe_external_effects,
            source_sha256=binding.local_save_source_sha256,
        )
        if status_projection is not None:
            debuff_registry, debuff_blockers = (
                restore_plan2_native_anti_debuff_registry(
                    exam,
                    next_uid=status_projection.next_status_uid,
                )
            )
            blockers.extend(debuff_blockers)
            review_runtime, review_dynamic_blockers = (
                restore_plan2_native_review_dynamic_runtime(
                    exam,
                    review=status_projection.review,
                    review_status_present=status_projection.review_present,
                    review_passing_turn_start=(
                        status_projection.review_passing_turn_start
                    ),
                    next_uid=max(
                        status_projection.next_status_uid,
                        debuff_registry.next_uid,
                    ),
                )
            )
            blockers.extend(review_dynamic_blockers)
            effect_chains, effect_chain_blockers = (
                restore_plan2_native_effect_chain_runtime(
                    exam,
                    catalog,
                    next_uid=max(
                        status_projection.next_status_uid,
                        debuff_registry.next_uid,
                        review_runtime.next_status_uid,
                    ),
                )
            )
            blockers.extend(effect_chain_blockers)
        raw_supports = opaque.get("supportCardList")
        if isinstance(raw_supports, list) and raw_supports:
            # ExamSave owns these runtime permils and order. A malformed or
            # incomplete non-empty list blocks bootstrap instead of silently
            # falling back to Master support rows.
            try:
                support_runtime = project_exam_hand_add_support_runtime(exam)
                if catalog.master_card_refs:
                    support_runtime = replace(
                        support_runtime,
                        catalog=replace(
                            support_runtime.catalog,
                            master_card_refs=catalog.master_card_refs,
                        ),
                    )
            except ExamHandAddSupportRuntimeError as error:
                _block(
                    blockers,
                    "local-save-support-runtime-unsupported",
                    f"{error.code}:{error.detail}",
                )
        elif isinstance(raw_supports, list):
            # Synthetic/legacy fixtures explicitly carrying [] retain the
            # previous no-support behavior. No catalog is invented.
            pass
        elif raw_supports is not None:
            _block(
                blockers,
                "local-save-support-runtime-invalid",
                "supportCardList must be a list",
            )
        raw_drinks = opaque.get("drinkList")
        if isinstance(raw_drinks, list) and raw_drinks:
            from .drink_catalog import load_drink_catalog
            from .initial_regular_plan2_drink_runtime import (
                compile_plan2_native_drink_instance,
            )

            drink_catalog = load_drink_catalog()
            compiled_drinks = []
            for index, row in enumerate(raw_drinks):
                drink_id = row.get("_id") if isinstance(row, Mapping) else None
                if not isinstance(drink_id, str) or not drink_id:
                    unmapped_drink_slots.append(f"{index}:identity-unavailable")
                    continue
                try:
                    drink = drink_catalog.get_drink(drink_id)
                    compilation = compile_plan2_native_drink_instance(
                        drink,
                        instance_id=f"localsave-drink:{index}:{drink_id}",
                        session_ref=drink_id,
                    )
                except (KeyError, TypeError, ValueError) as error:
                    unmapped_drink_slots.append(
                        f"{index}:{drink_id}:{type(error).__name__}"
                    )
                    continue
                if not compilation.supported or compilation.instance is None:
                    codes = ",".join(value.code for value in compilation.blockers)
                    unmapped_drink_slots.append(f"{index}:{drink_id}:{codes}")
                    continue
                compiled_drinks.append(compilation.instance)
            drink_runtime = Plan2NativeDrinkRuntime(tuple(compiled_drinks))
        elif raw_drinks is not None and not isinstance(raw_drinks, list):
            unmapped_drink_slots.append("drinkList:not-a-list")
        clear_border = _root_counter(opaque, "clearBorder", blockers)
        review_sum = _root_counter(opaque, "reviewConsumptionSumCount", blockers)
        block_sum = _root_counter(opaque, "blockConsumptionSumCount", blockers)
        total_draw = _root_counter(opaque, "totalDrawCardCount", blockers)

    active_classes = (
        () if status_projection is None else status_projection.active_status_classes
    )
    if (
        blockers
        or zones is None
        or status_projection is None
        or review_runtime is None
        or draw_count is None
        or hand_limit is None
        or clear_border is None
        or review_sum is None
        or block_sum is None
        or total_draw is None
        or exam_mode is None
    ):
        return _audit_result(
            state=None,
            blockers=blockers,
            exam=exam,
            active_status_classes=active_classes,
            catalog_refs=catalog_refs,
        )

    next_uid = max(
        status_projection.next_status_uid,
        debuff_registry.next_uid,
        review_runtime.next_status_uid,
        effect_chains.next_status_uid,
    )
    battle_scoring = plan2_battle_scoring_context_from_exam_save(exam)
    scalar = Plan2State(
        current_turn=exam.current_turn,
        review=status_projection.review,
        score=exam.score,
        block=exam.block,
        card_play_aggressive=status_projection.aggressive,
        stamina=exam.stamina,
        max_stamina=exam.max_stamina,
        exam_card_play_count=exam.exam_card_play_count,
        turn_card_play_count=exam.turn_card_play_count,
        next_status_uid=next_uid,
        battle_scoring=battle_scoring,
        end_turn_listeners=status_projection.end_turn_listeners,
    )
    review_runtime = replace(review_runtime, next_status_uid=next_uid)
    state = Plan2NativeHorizonState(
        schema_version=PLAN2_NATIVE_HORIZON_SCHEMA_VERSION,
        scalar=scalar,
        zones=zones,
        limit_turn=exam.limit_turn,
        exam_mode=exam_mode,
        stamina_modifiers=Plan2StaminaModifierRuntime(
            down=status_projection.stamina_down,
            down_fix_layers=status_projection.stamina_down_fix_layers,
            add=StaminaConsumptionAddRuntime(
                layers=(
                    ()
                    if status_projection.stamina_add is None
                    else (status_projection.stamina_add,)
                ),
                next_status_uid=next_uid,
            ),
            next_status_uid=next_uid,
        ),
        status_enchant=Plan2NativeStatusEnchantRuntime(
            listeners=status_projection.status_enchant_listeners,
            next_status_uid=next_uid,
        ),
        review_dynamic=review_runtime,
        aggressive_additive_runtime=status_projection.aggressive_additive_runtime,
        triggered_review_status_runtime=Plan2TriggeredReviewStatusRuntime(
            review_runtime=review_runtime,
            next_status_uid=next_uid,
        ),
        debuff_registry=replace(debuff_registry, next_uid=next_uid),
        effect_chains=replace(effect_chains, next_status_uid=next_uid),
        total_effect_draw_card_count=total_draw,
        review_consumption_sum=review_sum,
        block_consumption_sum_count=block_sum,
        judge_parameter=exam.score,
        clear_border=clear_border,
        extra_turn=exam.extra_turn,
        draw_count=draw_count,
        hand_limit=hand_limit,
        plays_remaining=status_projection.plays_remaining,
        item_runtime=status_projection.item_runtime,
        search_play_card_stamina_runtime=replace(status_projection.search_play_card_stamina_runtime, next_status_uid=next_uid),
        drink_runtime=drink_runtime,
        scheduled_gimmicks=scheduled_gimmicks,
        phase=PHASE_MAIN,
        source_kind="exam-save-data-plan2-schema-v5",
        hand_add_support_catalog=(
            None if support_runtime is None else support_runtime.catalog
        ),
        hand_add_lesson_type=(
            None
            if support_runtime is None
            else support_runtime.request.lesson_type
        ),
        turn_used_support_ids=(
            ()
            if support_runtime is None
            else support_runtime.request.used_support_ids
        ),
        unmapped_drink_slots=tuple(unmapped_drink_slots),
    )
    return _audit_result(
        state=state,
        blockers=(),
        exam=exam,
        active_status_classes=active_classes,
        catalog_refs=catalog_refs,
    )


def bootstrap_plan2_native_horizon_from_evidence(
    evidence: AuditionLocalSaveStateEvidence,
    *,
    catalog: Plan2NativeProgramCatalog,
    draw_count: int | None,
    hand_limit: int | None,
    database: str | Path = DEFAULT_DATABASE,
    gimmick_hooks: Mapping[
        Plan2ScheduledGimmickHookKey,
        Plan2ScheduledGimmickHook,
    ]
    | None = None,
    observe_external_effects: bool = False,
) -> Plan2NativeLocalSaveBootstrapAudit:
    """Convenience entry point preserving the LocalSave evidence provenance."""

    if not isinstance(evidence, AuditionLocalSaveStateEvidence):
        raise TypeError("evidence must be AuditionLocalSaveStateEvidence")
    if not isinstance(catalog, Plan2NativeProgramCatalog):
        raise TypeError("catalog must be Plan2NativeProgramCatalog")
    exam = evidence.state
    blockers: list[Plan2NativeBlocker] = []
    zones_tuple = _zone_instances(exam, blockers)
    if blockers or zones_tuple is None:
        return _audit_result(state=None, blockers=blockers, exam=exam)
    aggregate = canonical_runtime_digest_aggregate(
        tuple(card for zone in zones_tuple for card in zone)
    )
    binding = NativeOrderedZoneEvidenceBinding(
        schema_version=NATIVE_ORDERED_ZONE_SCHEMA_VERSION,
        local_save_schema_version=AUDITION_LOCAL_SAVE_STATE_SCHEMA_VERSION,
        run_id=evidence.run_id,
        step_context_id=evidence.step_context_id,
        step_context_digest=evidence.step_context_digest,
        session_transition_id=evidence.session_transition_id,
        zone_checkpoint_digest=evidence.zone_checkpoint_digest,
        local_save_source_sha256=evidence.source_sha256,
        local_save_evidence_digest=evidence.digest(),
        runtime_evidence_digest=aggregate,
    )
    return bootstrap_plan2_native_horizon_from_exam_save(
        exam,
        binding=binding,
        catalog=catalog,
        draw_count=draw_count,
        hand_limit=hand_limit,
        database=database,
        gimmick_hooks=gimmick_hooks,
        observe_external_effects=observe_external_effects,
    )


__all__ = [
    "DEFAULT_PLAN2_EXAM_SETTING_MASTER_DIR",
    "PLAN2_NATIVE_LOCAL_SAVE_BOOTSTRAP_SCHEMA_VERSION",
    "PLAN2_LOCAL_SAVE_AUDITION_EXAM_TYPE",
    "PLAN2_LOCAL_SAVE_LESSON_EXAM_TYPE",
    "Plan2NativeExamSettingAuthority",
    "Plan2NativeExamSettingAuthorityError",
    "Plan2NativeLocalSaveBootstrapAudit",
    "Plan2NativeLocalSaveCardAudit",
    "bootstrap_plan2_native_horizon_from_evidence",
    "bootstrap_plan2_native_horizon_from_exam_save",
    "load_plan2_native_exam_setting_authority",
    "restore_plan2_native_anti_debuff_registry",
    "restore_plan2_native_effect_chain_runtime",
    "restore_plan2_native_review_dynamic_runtime",
]
