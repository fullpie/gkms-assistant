"""Read-only ``ExamSaveData`` to Plan 3 one-card advice bridge.

The bridge has deliberately narrow authority: it decrypts bytes supplied by a
caller, projects the persisted exam snapshot, and asks the existing Plan 3
engine/advisor to simulate the visible hand.  It never discovers a process,
reads process memory, writes a save, or executes an input action.

``ExamSaveData.status`` is a reference graph whose complete Plan 3 mapping is
not yet implemented here.  The bridge restores only the locally verified
status and item-listener slice.  Any unknown or malformed active node remains
an explicit unsupported condition; callers may still request the clearly
labelled heuristic fallback projection, but must never treat that result as
executable advice.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
import json
from pathlib import Path
import sqlite3
from typing import Mapping

import yaml

from .audition_local_save_state import (
    EXAM_SAVE_DATA_SOURCE_TYPE,
    LocalSaveExamCard,
    LocalSaveExamState,
    empty_local_save_exam_card_runtime_state,
    parse_local_save_exam_state,
    parse_raw_local_save_exam_card,
)
from .local_save_decoder import (
    LocalSaveDecodeError,
    LocalSaveEnvelope,
    decode_local_save_bytes,
    decode_local_save_file,
)
from .master_db import DEFAULT_DATABASE
from .nia_lesson_value_multiple import (
    LessonParameterMultipleState,
    LessonParameterMultipleStatus,
)
from .plan3_advisor import Plan3HandAdvice, advise_plan3_visible_hand
from .plan3_anti_debuff import AntiDebuffRuntime, INT32_MAX
from .enthusiastic_runtime import (
    EnthusiasticRuntime,
    EnthusiasticStatus,
)
from .playable_value_add_runtime import (
    PlayableValueAddRuntime,
    PlayableValueAddStatus,
)
from .plan3_engine import (
    DEFAULT_MASTER_DIR,
    EFFECT_ADD_GROW,
    EFFECT_BLOCK,
    EFFECT_FULL_POWER_POINT,
    EFFECT_PRESERVATION,
    EFFECT_STAMINA_CONSUMPTION_DOWN,
    EFFECT_STATUS_ENCHANT,
    EFFECT_TIMER,
    LESSON_DANCE,
    LESSON_UNKNOWN,
    LESSON_VISUAL,
    LESSON_VOCAL,
    PHASE_CARD_PLAY,
    PHASE_CARD_PLAY_AFTER,
    PHASE_END_TURN,
    PHASE_NONE,
    PHASE_PLAY_COUNT_INTERVAL,
    PHASE_START_PLAY,
    PHASE_START_TURN,
    PHASE_STATUS_CHANGE,
    PHASE_STANCE_CHANGE_CONCENTRATION,
    PHASE_STANCE_CHANGE_FROM_FULL_POWER,
    PHASE_STANCE_CHANGE_FULL_POWER,
    PHASE_TURN_TIMER,
    STANCE_CONCENTRATION,
    STANCE_FULL_POWER,
    STANCE_NEUTRAL,
    STANCE_PRESERVATION,
    ActivePlan3StatusEnchant,
    Plan3EnthusiasmAdditiveStatus,
    Plan3Effect,
    Plan3CardRef,
    Plan3ExamSettings,
    Plan3StaminaConsumptionDownFixStatus,
    Plan3StatusEnchantRule,
    Plan3Trigger,
    Plan3State,
    load_plan3_card,
    load_plan3_exam_settings,
    load_plan3_gimmick_profile,
    load_plan3_item,
    load_plan3_status_enchant,
    validate_plan3_status_enchant,
)
from .plan3_native_state import (
    Plan3NativeStateError,
    parse_plan3_runtime_card_grow_status,
    parse_plan3_runtime_customization_with_full_power,
    parse_plan3_runtime_lesson_add,
)
from .trigger_effect_serializer_progress import (
    TriggerEffectSerializerLifecyclePolicy,
    TriggerEffectSerializerProgressError,
    restore_trigger_effect_serializer_progress,
)


RANKING_EXACT = "exact"
RANKING_HEURISTIC_FALLBACK = "heuristic_fallback"
RANKING_UNSUPPORTED = "unsupported"

_STATUS_FIELDS = frozenset(
    {"_effectList", "_removedEffectList", "_effectCreateCount", "_idolStatusEffect"}
)
_PLAN3_NATIVE_PLAN_TYPE = 4
_PLAN3_MAIN_EFFECT_TYPES = {
    # Android v3.2.3 ProduceExamEffectType. Both use the same Plan3 stance,
    # gauge and card kernel; the real flow tag remains in the source state.
    45: "ProduceExamEffectType_ExamConcentration",
    47: "ProduceExamEffectType_ExamFullPower",
}
_LESSON_EXAM_TYPE = 0
_AUDITION_EXAM_TYPE = 1
_MAIN_PHASE = 6
from .runtime_lesson_context import LESSON_STEP_VALUES, lesson_step_rule

_LESSON_PARAMETER_TYPE_BY_STEP_TYPE = {step: int(lesson_step_rule(step).parameter_type) for step in LESSON_STEP_VALUES}
_LESSON_TYPE_BY_STEP_TYPE = {step: lesson_step_rule(step).lesson_type for step in LESSON_STEP_VALUES}
_ASSEMBLY = "Assembly-CSharp"
_NAMESPACE = "Campus.InGame.Exam"
_TRIGGER_LISTENER_CLASS = "TriggerEffectStatusEffect"
_ENTHUSIASM_CLASS = "EnthusiasticStatusEffect"
_ENTHUSIASM_ADDITIVE_CLASS = "EnthusiasticAdditiveStatusEffect"
_FULL_POWER_POINT_CLASS = "FullPowerPointStatusEffect"
_BLOCK_RESTRICTION_CLASS = "BlockRestrictionStatusEffect"
_STAMINA_CONSUMPTION_DOWN_CLASS = "StaminaConsumptionDownStatusEffect"
_STAMINA_CONSUMPTION_DOWN_FIX_CLASS = "StaminaConsumptionDownFixStatusEffect"
_PLAYABLE_VALUE_ADD_CLASS = "PlayableValueAddStatusEffect"
_LESSON_PARAMETER_MULTIPLE_CLASS = "LessonParameterMultipleStatusEffect"
_ANTI_DEBUFF_CLASS = "AntiDebuffStatusEffect"
_SCALAR_STATUS_FIELDS = frozenset(
    {"_isPassingTurnStart", "_isTurnLimited", "_turn", "_uid", "_value"}
)
_TURN_STATUS_FIELDS = frozenset(
    {"_isPassingTurnStart", "_isTurnLimited", "_turn", "_uid"}
)
_ANTI_DEBUFF_STATUS_FIELDS = frozenset(
    {"_count", "_isPassingTurnStart", "_isTurnLimited", "_turn", "_uid"}
)
_ITEM_SOURCE_FIELDS = frozenset(
    {"_fireCount", "_id", "_itemType", "_parentCustomItemIds", "_reactionCount"}
)
_STANCE_BY_NATIVE_TYPE = {
    0: (STANCE_NEUTRAL, 0),
    1: (STANCE_CONCENTRATION, None),
    2: (STANCE_PRESERVATION, None),
    3: (STANCE_FULL_POWER, 1),
}
_PRODUCE_EFFECT_STATUS_ENCHANT = "ProduceEffectType_ExamStatusEnchant"
_MEMORY_EFFECT_TYPE_VALUE = {
    EFFECT_BLOCK: 3,
    EFFECT_STAMINA_CONSUMPTION_DOWN: 5,
    EFFECT_PRESERVATION: 46,
    EFFECT_FULL_POWER_POINT: 49,
    EFFECT_ADD_GROW: 156,
}
_TURN_TIMER_PHASE_VALUE = 21
# ExamSaveData serializes the trigger phase as its native enum value while the
# Plan3 Master model keeps the symbolic name.  This is also used for card-owned
# status listeners restored from ``TriggerEffectStatusEffect._trigger``.
_NATIVE_PHASE_TYPE_BY_NAME = {
    PHASE_NONE: 999,
    PHASE_START_TURN: 4,
    PHASE_START_PLAY: 27,
    PHASE_CARD_PLAY: 2,
    PHASE_CARD_PLAY_AFTER: 3,
    PHASE_PLAY_COUNT_INTERVAL: 23,
    PHASE_STATUS_CHANGE: 13,
    PHASE_END_TURN: 5,
    PHASE_TURN_TIMER: _TURN_TIMER_PHASE_VALUE,
    PHASE_STANCE_CHANGE_CONCENTRATION: 37,
    PHASE_STANCE_CHANGE_FROM_FULL_POWER: 47,
    PHASE_STANCE_CHANGE_FULL_POWER: 39,
}
_NATIVE_PHASE_NAME_BY_VALUE = {
    value: name for name, value in _NATIVE_PHASE_TYPE_BY_NAME.items()
}


def _native_effect_payload_matches_master(
    raw: object,
    effect_id: str,
    *,
    database: Path,
) -> bool:
    """Compare one serialized status child with its complete Master row.

    ``TriggerEffectStatusEffect._effectList`` is a copied serializer graph,
    rather than a reference to the card's original effect row.  Comparing only
    the child ID would therefore allow a stale or altered payload to be
    projected as executable.  Keep the enum conversion in the established
    serializer table and compare the fields the native ExamEffect serializer
    actually writes; descriptions are intentionally not part of that graph.
    """

    if not isinstance(raw, Mapping) or not isinstance(effect_id, str) or not effect_id:
        return False
    try:
        with sqlite3.connect(Path(database)) as connection:
            row = connection.execute(
                "SELECT raw_json FROM effect WHERE id = ?", (effect_id,)
            ).fetchone()
    except sqlite3.Error:
        return False
    if row is None or not isinstance(row[0], str):
        return False
    try:
        master = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(master, Mapping):
        return False
    # These tables are the single v3.2.3 serializer enum authority already
    # used by the native item restore path.  Import lazily to avoid making the
    # Plan3 bridge depend on that module during its import graph.
    from .plan2_native_item_runtime import (
        _SERIALIZED_EFFECT_ENUM,
        _SERIALIZED_MOVE_POSITION_ENUM,
        _SERIALIZED_PICK_COUNT_ENUM,
        _SERIALIZED_PICK_RANGE_ENUM,
    )

    enum_values = (
        ("effectType", "_effectType", _SERIALIZED_EFFECT_ENUM),
        ("targetExamEffectType", "_targetExamEffectType", _SERIALIZED_EFFECT_ENUM),
        ("movePositionType", "_movePositionType", _SERIALIZED_MOVE_POSITION_ENUM),
        ("pickRangeType", "_pickRangeType", _SERIALIZED_PICK_RANGE_ENUM),
        ("pickRangeType2", "_pickRangeType2", _SERIALIZED_PICK_RANGE_ENUM),
        ("pickCountType", "_pickCountType", _SERIALIZED_PICK_COUNT_ENUM),
        ("pickCountType2", "_pickCountType2", _SERIALIZED_PICK_COUNT_ENUM),
    )
    expected: dict[str, object] = {
        "_id": master.get("id"),
        "_effectValue1": master.get("effectValue1"),
        "_effectValue2": master.get("effectValue2"),
        "_effectCount": master.get("effectCount"),
        "_effectTurn": master.get("effectTurn"),
        "_targetProduceCardId": master.get("targetProduceCardId"),
        "_targetUpgradeCount": master.get("targetUpgradeCount"),
        "_cardSearchId": master.get("produceCardSearchId"),
        "_cardSearchId2": master.get("produceCardSearchId2"),
        "_pickCountReferenceProduceCardSearchId": master.get(
            "pickCountReferenceProduceCardSearchId"
        ),
        "_pickCountReferenceProduceCardSearchId2": master.get(
            "pickCountReferenceProduceCardSearchId2"
        ),
        "_pickCountMin": master.get("pickCountMin"),
        "_pickCountMax": master.get("pickCountMax"),
        "_pickCountMin2": master.get("pickCountMin2"),
        "_pickCountMax2": master.get("pickCountMax2"),
        "_chainEffectId": master.get("chainProduceExamEffectId"),
        "_chainEffectIdList": master.get("chainProduceExamEffectIds"),
        "_statusEnchantId": master.get("produceExamStatusEnchantId"),
        "_cardGrowEffectIdList": master.get("produceCardGrowEffectIds"),
        "_effectGroupIdList": master.get("effectGroupIds"),
        # These two fields are not present in Master's effect row but are
        # deterministic defaults in the native serializer.
        "_judgeTargetIndex": 0,
        "_pickCount": 0,
    }
    for master_name, native_name, table in enum_values:
        master_value = master.get(master_name)
        encoded = table.get(master_value)
        if encoded is None:
            return False
        expected[native_name] = encoded
    return all(raw.get(name) == value for name, value in expected.items())
# ExamSaveData serializes ``ProduceExamPhaseType`` as its native enum value,
# while the local Master database uses the symbolic name.  Keep this small
# bridge table here instead of inferring a phase from the HUD/current-turn
# marker.  The table covers every trigger family admitted by Plan3's existing
# trigger evaluator; an unlisted Master phase remains fail-closed.
_NATIVE_PHASE_TYPE_BY_NAME = {
    PHASE_NONE: 999,
    PHASE_START_TURN: 4,
    PHASE_START_PLAY: 27,
    PHASE_CARD_PLAY: 2,
    PHASE_CARD_PLAY_AFTER: 3,
    PHASE_PLAY_COUNT_INTERVAL: 23,
    PHASE_STATUS_CHANGE: 13,
    PHASE_END_TURN: 5,
    PHASE_TURN_TIMER: _TURN_TIMER_PHASE_VALUE,
    PHASE_STANCE_CHANGE_CONCENTRATION: 37,
    PHASE_STANCE_CHANGE_FROM_FULL_POWER: 47,
    PHASE_STANCE_CHANGE_FULL_POWER: 39,
}


@lru_cache(maxsize=4)
def _memory_status_source_index(
    master_dir: Path,
) -> Mapping[tuple[str, int], tuple[str, ...]]:
    """Resolve direct memory skill -> status lineage from static Master."""

    skill_path = Path(master_dir) / "ProduceSkill.yaml"
    effect_path = Path(master_dir) / "ProduceEffect.yaml"
    with skill_path.open("r", encoding="utf-8") as stream:
        skills = yaml.safe_load(stream)
    with effect_path.open("r", encoding="utf-8") as stream:
        effects = yaml.safe_load(stream)
    if not isinstance(skills, list) or not isinstance(effects, list):
        raise ValueError("memory source Master must contain YAML lists")
    effect_by_id = {
        row.get("id"): row
        for row in effects
        if isinstance(row, Mapping) and isinstance(row.get("id"), str)
    }
    result: dict[tuple[str, int], tuple[str, ...]] = {}
    for skill in skills:
        if not isinstance(skill, Mapping):
            continue
        skill_id = skill.get("id")
        level = skill.get("level")
        if (
            not isinstance(skill_id, str)
            or not skill_id.startswith("p_memory_skill-")
            or skill.get("planType")
            not in {"ProducePlanType_Common", "ProducePlanType_Plan3"}
            or not isinstance(level, int)
            or isinstance(level, bool)
        ):
            continue
        status_ids: list[str] = []
        for slot in range(1, 4):
            effect_id = skill.get(f"produceEffectId{slot}")
            if not isinstance(effect_id, str) or not effect_id:
                continue
            effect = effect_by_id.get(effect_id)
            if not isinstance(effect, Mapping):
                raise ValueError(f"memory source effect missing: {effect_id}")
            if effect.get("produceEffectType") != _PRODUCE_EFFECT_STATUS_ENCHANT:
                continue
            status_id = effect.get("produceExamStatusEnchantId")
            if not isinstance(status_id, str) or not status_id:
                raise ValueError(f"memory source status missing: {effect_id}")
            neutral = (
                effect.get("effectValueMin") == 0
                and effect.get("effectValueMax") == 0
                and effect.get("produceResourceType")
                == "ProduceResourceType_Unknown"
                and effect.get("produceRewards") == []
                and effect.get("produceCardSearchId") == ""
                and effect.get("produceStepEventDetailId") == ""
                and effect.get("pickRangeType") == "ProducePickRangeType_Unknown"
                and effect.get("pickCountMin") == 0
                and effect.get("pickCountMax") == 0
                and effect.get("isResearch") is False
            )
            if not neutral:
                raise ValueError(f"memory source effect shape unsupported: {effect_id}")
            status_ids.append(status_id)
        key = (skill_id, level)
        if key in result:
            raise ValueError(f"duplicate memory source Master row: {key!r}")
        result[key] = tuple(status_ids)
    return result


def _memory_runtime_effect_matches(
    raw: object, effect: Plan3Effect
) -> bool:
    """Require live effect payload values to equal the resolved Master row."""

    if not isinstance(raw, Mapping):
        return False
    effect_type_value = _MEMORY_EFFECT_TYPE_VALUE.get(effect.effect_type)
    if effect_type_value is None:
        return False
    common = (
        raw.get("_id") == effect.id
        and raw.get("_effectType") == effect_type_value
        and raw.get("_effectValue1") == effect.value1
        and raw.get("_effectValue2") == effect.value2
        and raw.get("_effectCount") == effect.effect_count
        and raw.get("_effectTurn") == effect.effect_turn
        and raw.get("_chainEffectId") == effect.chain_effect_id
        and raw.get("_chainEffectIdList") == []
        and raw.get("_statusEnchantId") == ""
        and raw.get("_targetProduceCardId") == ""
        and raw.get("_targetUpgradeCount") == 0
        and raw.get("_targetExamEffectType") == 0
        and raw.get("_cardSearchId2") == ""
        and raw.get("_pickRangeType2") == 0
        and raw.get("_pickCountReferenceProduceCardSearchId2") == ""
        and raw.get("_pickCountType2") == 0
        and raw.get("_pickCountMin2") == 0
        and raw.get("_pickCountMax2") == 0
    )
    if not common:
        return False
    rule = effect.card_move_rule
    if effect.effect_type == EFFECT_ADD_GROW:
        return bool(
            rule is not None
            and raw.get("_cardSearchId") == rule.search_id
            and raw.get("_movePositionType") == 0
            and raw.get("_pickRangeType") == 3
            and raw.get("_pickCountReferenceProduceCardSearchId") == ""
            and raw.get("_pickCountType") == 0
            and raw.get("_pickCountMin") == 0
            and raw.get("_pickCountMax") == 0
            and raw.get("_cardGrowEffectIdList")
            == list(rule.card_grow_effect_ids)
        )
    return bool(
        rule is None
        and raw.get("_cardSearchId") == ""
        and raw.get("_movePositionType") == 0
        and raw.get("_pickRangeType") == 0
        and raw.get("_pickCountReferenceProduceCardSearchId") == ""
        and raw.get("_pickCountType") == 0
        and raw.get("_pickCountMin") == 0
        and raw.get("_pickCountMax") == 0
        and raw.get("_cardGrowEffectIdList") == []
    )


def _restore_gimmick_trigger_listener(
    data: Mapping[str, object],
    *,
    rid: int,
    database: Path,
    master_dir: Path,
    issues: list[Plan3LocalSaveIssue],
) -> ActivePlan3StatusEnchant | None:
    """Restore a status listener installed by a native NIA gimmick.

    A gimmick listener has no item/card origin.  Its durable identity is the
    ``_triggerGimmickGroup`` + ``_gimmickEffect`` pair, with the status graph
    entry supplying the live trigger/effect payload.  Resolve that pair back
    through the local gimmick Master row before creating the normal Plan3
    active-enchant object; never infer it from HUD status strings.
    """

    gimmick = data.get("_triggerGimmickGroup")
    if not isinstance(gimmick, Mapping):
        _issue(
            issues,
            "gimmick-trigger-listener-shape-unmapped",
            f"root_runtime.references[{rid}]._triggerGimmickGroup",
            "expected object",
        )
        return None
    group_id = gimmick.get("_id")
    effect_ref = gimmick.get("_gimmickEffect")
    effect_id = (
        effect_ref.get("_effectId") if isinstance(effect_ref, Mapping) else None
    )
    status_id = data.get("_statusEnchantId")
    if (
        not isinstance(group_id, str)
        or not group_id
        or not isinstance(effect_id, str)
        or not effect_id
        or not isinstance(status_id, str)
        or not status_id
    ):
        _issue(
            issues,
            "gimmick-trigger-listener-shape-unmapped",
            f"root_runtime.references[{rid}]",
            f"group={group_id!r};effect={effect_id!r};status={status_id!r}",
        )
        return None

    # Owner/origin fields are independent of the common mutable serializer
    # progress restored below after the Master installer is bound.
    origin = (
        data.get("_isItemDirectEnchant"),
        data.get("_originType"),
        data.get("_isFromEnchantEffect"),
        data.get("_isEncoreEnchant"),
    )
    if (
        origin != (False, 1, False, False)
        or data.get("_fromExamEffectIdList") != []
    ):
        _issue(
            issues,
            "gimmick-trigger-listener-lifecycle-unsupported",
            f"root_runtime.references[{rid}]",
            f"origin={origin!r}",
        )
        return None

    # The group metadata is part of the serialized gimmick identity.  Only
    # compare fields represented by Plan3GimmickStep; remainingTurn is a live
    # counter and is therefore validated for shape rather than against the
    # static start row.
    metadata = (
        gimmick.get("_priority"),
        gimmick.get("_remainingTurnPermil"),
        gimmick.get("_startTurn"),
    )
    if (
        any(
            not isinstance(value, int) or isinstance(value, bool)
            for value in metadata
        )
        or not isinstance(gimmick.get("_remainingTurn"), int)
        or isinstance(gimmick.get("_remainingTurn"), bool)
        or gimmick.get("_remainingTurn") < 0
        or not isinstance(gimmick.get("_isPositive"), bool)
    ):
        _issue(
            issues,
            "gimmick-trigger-listener-shape-unmapped",
            f"root_runtime.references[{rid}]._triggerGimmickGroup",
            f"metadata={metadata!r}",
        )
        return None

    try:
        profile = load_plan3_gimmick_profile(
            group_id,
            master_dir=Path(master_dir),
            database=database,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        sqlite3.Error,
        yaml.YAMLError,
    ) as error:
        _issue(
            issues,
            "gimmick-trigger-listener-master-unavailable",
            f"root_runtime.references[{rid}]",
            f"{type(error).__name__}:{group_id}",
        )
        return None
    matches = [step for step in profile.steps if step.effect.id == effect_id]
    if len(matches) != 1:
        _issue(
            issues,
            "gimmick-trigger-listener-master-mismatch",
            f"root_runtime.references[{rid}]",
            f"group={group_id!r};effect={effect_id!r};matches={len(matches)}",
        )
        return None
    step = matches[0]
    if metadata != (
        step.priority,
        step.remaining_turn_permille,
        step.start_turn,
    ):
        _issue(
            issues,
            "gimmick-trigger-listener-master-mismatch",
            f"root_runtime.references[{rid}]._triggerGimmickGroup",
            f"metadata={metadata!r};master={step.priority, step.remaining_turn_permille, step.start_turn!r}",
        )
        return None
    master_effect = step.effect
    if (
        master_effect.effect_type != EFFECT_STATUS_ENCHANT
        or master_effect.status_enchant_id != status_id
        or master_effect.status_enchant is None
    ):
        _issue(
            issues,
            "gimmick-trigger-listener-master-mismatch",
            f"root_runtime.references[{rid}]",
            (
                f"effect-type={master_effect.effect_type!r};"
                f"effect-status={master_effect.status_enchant_id!r};"
                f"runtime-status={status_id!r}"
            ),
        )
        return None
    try:
        progress = restore_trigger_effect_serializer_progress(
            data,
            master_turn=master_effect.effect_turn,
            master_total_limit=(
                -1
                if master_effect.effect_count == 0
                else master_effect.effect_count
            ),
            # StatusEnchantEffectExecutor passes installer value1 as the
            # per-turn limit; native converts zero to unlimited (-1).  The
            # NIA v3.2.3 status-enchant audit proves this exact outer shape.
            master_per_turn_limit=(
                -1 if master_effect.value1 == 0 else master_effect.value1
            ),
            phase_type_names=_NATIVE_PHASE_NAME_BY_VALUE,
            label=f"root_runtime.references[{rid}]",
        )
    except TriggerEffectSerializerProgressError as error:
        _issue(
            issues,
            "gimmick-trigger-listener-lifecycle-unsupported",
            f"root_runtime.references[{rid}]",
            str(error),
        )
        return None
    try:
        rule = load_plan3_status_enchant(status_id, database)
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as error:
        _issue(
            issues,
            "gimmick-trigger-listener-master-unavailable",
            f"root_runtime.references[{rid}]",
            f"{type(error).__name__}:{status_id}",
        )
        return None
    if master_effect.status_enchant != rule:
        _issue(
            issues,
            "gimmick-trigger-listener-master-mismatch",
            f"root_runtime.references[{rid}]",
            f"status rule differs from gimmick effect: {status_id}",
        )
        return None
    rule_errors = validate_plan3_status_enchant(rule)
    if rule_errors:
        _issue(
            issues,
            "gimmick-trigger-listener-master-unsupported",
            f"root_runtime.references[{rid}]",
            repr(rule_errors),
        )
        return None

    native_trigger = data.get("_trigger")
    if not isinstance(native_trigger, Mapping):
        _issue(
            issues,
            "gimmick-trigger-listener-shape-unmapped",
            f"root_runtime.references[{rid}]._trigger",
            "expected object",
        )
        return None
    native_trigger_id = native_trigger.get("_id")
    native_phase_types = native_trigger.get("_phaseTypeList")
    native_phase_values = native_trigger.get("_phaseValueList")
    expected_phase_types = [
        _NATIVE_PHASE_TYPE_BY_NAME.get(phase)
        for phase in rule.trigger.phase_types
    ]
    if any(value is None for value in expected_phase_types):
        _issue(
            issues,
            "gimmick-trigger-listener-master-unsupported",
            f"root_runtime.references[{rid}]._trigger",
            f"phase-types={rule.trigger.phase_types!r}",
        )
        return None
    if (
        native_trigger_id != rule.trigger.id
        or native_phase_types != expected_phase_types
        or native_phase_values != list(rule.trigger.phase_values)
    ):
        _issue(
            issues,
            "gimmick-trigger-listener-master-mismatch",
            f"root_runtime.references[{rid}]._trigger",
            (
                f"trigger={native_trigger_id!r}/{rule.trigger.id!r};"
                f"phase={native_phase_types!r}/{expected_phase_types!r};"
                f"values={native_phase_values!r}/{list(rule.trigger.phase_values)!r}"
            ),
        )
        return None

    raw_effects = data.get("_effectList")
    native_effect_ids = (
        tuple(
            effect.get("_id") if isinstance(effect, Mapping) else None
            for effect in raw_effects
        )
        if isinstance(raw_effects, list)
        else ()
    )
    master_effect_ids = tuple(effect.id for effect in rule.effects)
    effects_match = (
        isinstance(raw_effects, list)
        and len(raw_effects) == len(rule.effects)
        and native_effect_ids == master_effect_ids
    )
    # The NIA concentration listener currently resolves an AddGrow effect;
    # compare its complete serialized payload.  Other Master-backed effect
    # families still receive the common identity/chain validation above and
    # remain governed by the existing Plan3 executor shape checks.
    if effects_match and all(
        effect.effect_type == EFFECT_ADD_GROW for effect in rule.effects
    ):
        effects_match = all(
            _memory_runtime_effect_matches(raw, effect)
            for raw, effect in zip(raw_effects, rule.effects, strict=True)
        )
    if not effects_match:
        _issue(
            issues,
            "gimmick-trigger-listener-master-mismatch",
            f"root_runtime.references[{rid}]._effectList",
            f"effects={native_effect_ids!r}/{master_effect_ids!r}",
        )
        return None

    uid = data.get("_uid")
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        _issue(
            issues,
            "gimmick-trigger-listener-shape-unmapped",
            f"root_runtime.references[{rid}]._uid",
            repr(uid),
        )
        return None
    return ActivePlan3StatusEnchant(
        instance_id=f"localsave:{rid}:{status_id}",
        source_id=group_id,
        rule=rule,
        max_uses=0,
        uses=0,
        # ``currentTurnTriggeredStatusEnchantIdList`` is trace/history, not
        # the unlimited listener's per-turn remaining/used serializer count.
        uses_this_turn=0,
        remaining_turns=progress.turn,
        passing_turn_start=progress.is_passing_turn_start,
        is_item_direct=False,
        turn_count=progress.turn_count,
        phase_counts=progress.phase_counts,
        native_uid=uid,
    )


@dataclass(frozen=True, slots=True)
class Plan3LocalSaveIssue:
    """One reason the native snapshot cannot be mapped exactly."""

    code: str
    field: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.code or not self.field:
            raise ValueError("issue code and field must be non-empty")


@dataclass(frozen=True, slots=True)
class DecodedPlan3LocalSave:
    """The immutable result of decrypting and strictly parsing one save."""

    envelope: LocalSaveEnvelope
    exam_state: LocalSaveExamState


@dataclass(frozen=True, slots=True)
class Plan3LocalSaveProjection:
    """A Plan3State projection plus its exactness boundary."""

    state: Plan3State
    visible_hand: tuple[Plan3CardRef, ...]
    issues: tuple[Plan3LocalSaveIssue, ...]

    @property
    def exact(self) -> bool:
        return not self.issues

    @property
    def heuristic_fallback(self) -> bool:
        return bool(self.issues)


@dataclass(frozen=True, slots=True)
class Plan3LocalSaveRankedCard:
    """One visible slot with an explainable one-card score, when supported."""

    slot_index: int
    card_ref: Plan3CardRef
    card_name: str
    rank: int | None
    score: int | None
    explanation: tuple[str, ...]
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Plan3LocalSaveRanking:
    """Read-only ranking result; deliberately contains no execution field."""

    save_data_version: int
    projection: Plan3LocalSaveProjection
    mode: str
    cards: tuple[Plan3LocalSaveRankedCard, ...]
    recommended_slot_index: int | None

    def __post_init__(self) -> None:
        if self.mode not in {
            RANKING_EXACT,
            RANKING_HEURISTIC_FALLBACK,
            RANKING_UNSUPPORTED,
        }:
            raise ValueError(f"unsupported ranking mode: {self.mode}")
        if self.mode == RANKING_EXACT and not self.projection.exact:
            raise ValueError("exact ranking requires an exact projection")
        if self.mode == RANKING_HEURISTIC_FALLBACK and self.projection.exact:
            raise ValueError("heuristic fallback requires projection issues")


@dataclass(frozen=True, slots=True)
class _MappedPlan3Runtime:
    stance: str = STANCE_NEUTRAL
    stance_level: int = 0
    enthusiasm: int = 0
    enthusiastic_runtime: EnthusiasticRuntime = EnthusiasticRuntime()
    enthusiasm_additive: int = 0
    enthusiasm_additive_statuses: tuple[
        Plan3EnthusiasmAdditiveStatus, ...
    ] = ()
    full_power_points: int = 0
    playable_value_add_runtime: PlayableValueAddRuntime = (
        PlayableValueAddRuntime()
    )
    lesson_parameter_multiple_state: LessonParameterMultipleState = (
        LessonParameterMultipleState()
    )
    stamina_consumption_down_turns: int = 0
    # ``StaminaConsumptionDownStatusEffect`` is created after TurnStart with
    # ``_isPassingTurnStart=False``.  Preserve that native freshness bit so a
    # transition-local reducer does not spend the status at the first boundary.
    stamina_consumption_down_fresh: bool = False
    stamina_consumption_down_fix_statuses: tuple[
        Plan3StaminaConsumptionDownFixStatus, ...
    ] = ()
    block_restriction_turns: int = 0
    anti_debuff_runtime: AntiDebuffRuntime = AntiDebuffRuntime()
    active_status_enchants: tuple[ActivePlan3StatusEnchant, ...] = ()
    mapped_listener_ids: tuple[str, ...] = ()
    next_status_uid: int = 1


def _decode_json(envelope: LocalSaveEnvelope) -> Mapping[str, object]:
    try:
        payload = json.loads(envelope.plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LocalSaveDecodeError(
            "decrypted ExamSaveData body is not valid UTF-8 JSON"
        ) from error
    if not isinstance(payload, Mapping):
        raise LocalSaveDecodeError("decrypted ExamSaveData JSON root must be an object")
    return payload


def decode_plan3_local_save_bytes(data: bytes) -> DecodedPlan3LocalSave:
    """Decrypt and strictly parse supplied ``ExamSaveData`` file bytes."""

    envelope = decode_local_save_bytes(data, EXAM_SAVE_DATA_SOURCE_TYPE)
    return DecodedPlan3LocalSave(
        envelope=envelope,
        exam_state=parse_local_save_exam_state(_decode_json(envelope)),
    )


def decode_plan3_local_save_file(path: str | Path) -> DecodedPlan3LocalSave:
    """Open one local-save path read-only, then decrypt and strictly parse it."""

    envelope = decode_local_save_file(path, EXAM_SAVE_DATA_SOURCE_TYPE)
    return DecodedPlan3LocalSave(
        envelope=envelope,
        exam_state=parse_local_save_exam_state(_decode_json(envelope)),
    )


def _issue(
    issues: list[Plan3LocalSaveIssue], code: str, field: str, detail: str = ""
) -> None:
    candidate = Plan3LocalSaveIssue(code, field, detail)
    if candidate not in issues:
        issues.append(candidate)


def _runtime_opaque(
    exam: LocalSaveExamState, issues: list[Plan3LocalSaveIssue]
) -> Mapping[str, object]:
    runtime = exam.root_runtime
    if runtime is None:
        _issue(issues, "root-runtime-missing", "root_runtime")
        return {}
    value = runtime.opaque_fields.to_value()
    if not isinstance(value, Mapping):
        _issue(issues, "root-runtime-invalid", "root_runtime.opaque_fields")
        return {}
    return value


def _integer_field(
    opaque: Mapping[str, object],
    native_name: str,
    issues: list[Plan3LocalSaveIssue],
    *,
    fallback: int = 0,
    minimum: int | None = None,
) -> int:
    value = opaque.get(native_name)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or (minimum is not None and value < minimum)
    ):
        _issue(
            issues,
            "runtime-field-unmapped",
            f"root_runtime.{native_name}",
            f"expected integer{'' if minimum is None else f' >= {minimum}'}",
        )
        return fallback
    return value


def _same_card_lineage(
    left: LocalSaveExamCard,
    right: LocalSaveExamCard,
) -> bool:
    return bool(
        left.guid == right.guid
        and left.card_id == right.card_id
        and left.base_upgrade == right.base_upgrade
        and left.temporary_upgrade == right.temporary_upgrade
        and left.effective_upgrade == right.effective_upgrade
        and left.support_upgrade_ids == right.support_upgrade_ids
        and left.fixed_deck_order == right.fixed_deck_order
    )


def _positioned_turn_lost_history(exam: LocalSaveExamState) -> bool:
    """Prove that Plan3's Lost flag is settled current-turn history.

    ``isTurnCardLost`` remains true until ``ClearTurnState``.  It therefore
    does not by itself describe a pending command.  Admit only an ordinary
    playable-count MovePlayCard-to-Lost history whose logs are complete,
    uniquely positioned, and agree with the typed Lost card.  Forced/Encore
    moves remain fail-closed until their separate native contract is proven.
    """

    runtime = exam.root_runtime
    if runtime is None or not runtime.is_turn_card_lost:
        return False
    opaque = runtime.opaque_fields.to_value()
    if not isinstance(opaque, Mapping):
        return False
    play_logs = opaque.get("playLogList")
    if not isinstance(play_logs, list):
        return False

    settled_cards: list[LocalSaveExamCard] = []
    for index, raw_log in enumerate(play_logs):
        if not isinstance(raw_log, Mapping):
            continue
        command = raw_log.get("_command")
        if not isinstance(command, Mapping):
            continue
        raw_current_turn = raw_log.get("_currentTurn")
        if type(raw_current_turn) is not int or raw_current_turn != exam.current_turn:
            continue
        play_type = command.get("_playType")
        destination = command.get("_playCardPositionType")
        if type(play_type) is not int or type(destination) is not int:
            continue
        if play_type != 6 or destination != 2:
            continue
        phase_type = raw_log.get("_phaseType")
        if (
            type(phase_type) is not int
            or phase_type != 6
            or raw_log.get("_isCostFailed") is not False
            or raw_log.get("_isSelectLog") is not False
            or raw_log.get("_selectIndex") != []
            or command.get("_isUsePlayableCardCount") is not True
            or command.get("_isCardSelect") is not False
            or command.get("_isCardSelect2") is not False
        ):
            return False
        try:
            play_card = parse_raw_local_save_exam_card(
                raw_log.get("_playCard"),
                source_label=f"playLogList[{index}]._playCard",
                order=0,
            )
            settled_card = parse_raw_local_save_exam_card(
                command.get("_playingCard"),
                source_label=(
                    f"playLogList[{index}]._command._playingCard"
                ),
                order=0,
            )
        except (TypeError, ValueError):
            return False
        if (
            not _same_card_lineage(play_card, settled_card)
            or play_card.runtime_state is None
            or settled_card.runtime_state is None
            or settled_card.runtime_state.play_count
            != play_card.runtime_state.play_count + 1
        ):
            return False
        settled_cards.append(settled_card)

    guids = tuple(card.guid for card in settled_cards)
    if not guids or len(guids) != len(set(guids)):
        return False
    other_positioned = (
        *exam.zones.hand,
        *exam.zones.deck,
        *exam.zones.grave,
        *exam.zones.hold,
    )
    for settled_card in settled_cards:
        lost_matches = tuple(
            card for card in exam.zones.lost if card.guid == settled_card.guid
        )
        if (
            len(lost_matches) != 1
            or any(card.guid == settled_card.guid for card in other_positioned)
            or not _same_card_lineage(settled_card, lost_matches[0])
            or settled_card.runtime_payload() != lost_matches[0].runtime_payload()
        ):
            return False
    return True


def _audit_settled_boundary(
    exam: LocalSaveExamState, issues: list[Plan3LocalSaveIssue]
) -> None:
    runtime = exam.root_runtime
    if runtime is None:
        return
    # DrawCardGuidList is retained as current-turn draw history after the
    # corresponding cards have already settled into ordinary zones.  It is
    # not a pending draw queue.  Accept that history only when every recorded
    # GUID is uniquely positioned outside Deck/Playing/removed; all other
    # settled guards below remain mandatory.  The shared contract treats the
    # field as history; this Plan3-local audit additionally proves that every
    # retained GUID is uniquely positioned.
    draw_history = runtime.draw_card_guid_list
    positioned_counts: dict[str, int] = {}
    for card in (
        *exam.zones.hand,
        *exam.zones.grave,
        *exam.zones.lost,
        *exam.zones.hold,
    ):
        positioned_counts[card.guid] = positioned_counts.get(card.guid, 0) + 1
    excluded_guids = {
        card.guid for card in (*exam.zones.deck, *exam.removed_cards)
    }
    if exam.playing_card is not None:
        excluded_guids.add(exam.playing_card.guid)
    positioned_draw_history = bool(
        len(draw_history) == len(set(draw_history))
        and all(positioned_counts.get(guid) == 1 for guid in draw_history)
        and not any(guid in excluded_guids for guid in draw_history)
    )
    positioned_lost_history = bool(
        not runtime.is_turn_card_lost
        or _positioned_turn_lost_history(exam)
    )
    settled = bool(
        exam.exam_type in {_LESSON_EXAM_TYPE, _AUDITION_EXAM_TYPE}
        and exam.phase == _MAIN_PHASE
        and not exam.is_turn_card_play_end
        and exam.playing_card is None
        and not exam.removed_cards
        and runtime.command_list_is_empty
        and (not draw_history or positioned_draw_history)
        and not runtime.is_turn_card_grave
        and positioned_lost_history
        and not runtime.is_exam_end_complete
    )
    if not settled:
        _issue(
            issues,
            "not-actionable-settled",
            "ExamSaveData",
            "phase/playingCard/command/draw transition fields do not prove a settled hand",
        )


def _audit_card_runtime(
    exam: LocalSaveExamState,
    issues: list[Plan3LocalSaveIssue],
    *,
    database: Path,
    native_card_play_counts: bool,
) -> None:
    neutral = empty_local_save_exam_card_runtime_state()
    # A GUID-native search advances every card listener even while its owner
    # is in Deck/Grave/Lost/Hold.  The legacy one-card projection only needs
    # Hand, but native callers explicitly request the complete universe.
    zone_names = (
        ("hand", "deck", "grave", "lost", "hold")
        if native_card_play_counts
        else ("hand",)
    )
    for zone_name in zone_names:
        for index, card in enumerate(getattr(exam.zones, zone_name)):
            location = f"zones.{zone_name}[{index}].runtime_state"
            runtime = card.runtime_state
            if runtime is None:
                _issue(issues, "card-runtime-missing", location)
                continue
            # Android v3.2.3 reads the card's prior PlayCount while building
            # direct effects only for a Master isOncePlayEffect slot.  The
            # later MovePlayCard read increments/persists the count but does
            # not change this play's non-once effects.  Audit that exact
            # Master dependency instead of requiring every previously played
            # ordinary card to look newly created.
            if runtime.play_count > 0 and not native_card_play_counts:
                try:
                    master_card = load_plan3_card(
                        card.card_id,
                        card.effective_upgrade,
                        database,
                    )
                except (KeyError, ValueError) as error:
                    _issue(
                        issues,
                        "card-play-count-master-unresolved",
                        f"{location}.play_count",
                        f"{type(error).__name__}:{error}",
                    )
                else:
                    once_effect_ids = tuple(
                        effect.id
                        for effect in master_card.effects
                        if effect.once
                    )
                    if once_effect_ids:
                        _issue(
                            issues,
                            "card-play-count-once-effect",
                            f"{location}.play_count",
                            (
                                f"count={runtime.play_count}; "
                                f"isOncePlayEffect={','.join(once_effect_ids)}"
                            ),
                        )

            # Skin IDs are cosmetic.  Every other retained runtime field can
            # affect a trigger, cost, move, or customized card definition and
            # is therefore required to match the proven neutral shape.
            comparable = (
                runtime.is_move_produce_exam_effect_use_in_turn,
                runtime.stamina_consumption_specify_effect_list,
            )
            neutral_comparable = (
                neutral.is_move_produce_exam_effect_use_in_turn,
                neutral.stamina_consumption_specify_effect_list,
            )
            try:
                parse_plan3_runtime_lesson_add(runtime)
                parse_plan3_runtime_customization_with_full_power(
                    card.card_id,
                    card.effective_upgrade,
                    runtime,
                    database=database,
                )
                parse_plan3_runtime_card_grow_status(
                    card.card_id,
                    card.effective_upgrade,
                    runtime,
                    database=database,
                )
            except (TypeError, Plan3NativeStateError) as error:
                _issue(
                    issues,
                    "card-runtime-unsupported",
                    location,
                    f"{card.guid}:{getattr(error, 'code', type(error).__name__)}",
                )
            if comparable != neutral_comparable:
                _issue(
                    issues,
                    "card-runtime-unsupported",
                    location,
                    f"{card.guid}:{card.card_id}@{card.effective_upgrade}",
                )


def _audit_plan3_identity(
    opaque: Mapping[str, object], issues: list[Plan3LocalSaveIssue]
) -> None:
    plan_type = opaque.get("planType")
    if plan_type != _PLAN3_NATIVE_PLAN_TYPE:
        _issue(
            issues,
            "plan-type-unsupported",
            "root_runtime.planType",
            (
                f"expected ProducePlanType_Plan3 ({_PLAN3_NATIVE_PLAN_TYPE}); "
                f"got {plan_type!r}"
            ),
        )
    main_effect_type = opaque.get("mainEffectType")
    if type(main_effect_type) is not int or main_effect_type not in _PLAN3_MAIN_EFFECT_TYPES:
        _issue(
            issues,
            "main-effect-type-unsupported",
            "root_runtime.mainEffectType",
            (
                f"expected a proven Plan3 main effect (Concentration=45 or FullPower=47); "
                f"got {main_effect_type!r}"
            ),
        )


def _audit_turn_runtime(
    opaque: Mapping[str, object],
    mapped_listener_ids: tuple[str, ...],
    issues: list[Plan3LocalSaveIssue],
) -> None:
    triggered = opaque.get("currentTurnTriggeredStatusEnchantIdList")
    if not isinstance(triggered, list):
        _issue(
            issues,
            "runtime-field-unmapped",
            "root_runtime.currentTurnTriggeredStatusEnchantIdList",
            "expected list",
        )
    elif any(not isinstance(value, str) or not value for value in triggered):
        _issue(
            issues,
            "runtime-field-unmapped",
            "root_runtime.currentTurnTriggeredStatusEnchantIdList",
            "expected non-empty status-enchant IDs",
        )
    # ExamSaveData/ExamParameterModel store this shared field as a List<String>
    # of trigger history, not status-instance identities. Native FullPower Mid1
    # contains repeated IDs in this list. Preserve that trace in
    # opaque_fields; membership consumers above may use a set, but multiplicity
    # is neither a malformed graph nor an inferred remaining-use count.
    # Actual active status RID/UID uniqueness is checked separately.


def _links(
    value: object,
    *,
    field: str,
    issues: list[Plan3LocalSaveIssue],
) -> tuple[int, ...]:
    if not isinstance(value, list):
        _issue(issues, "status-graph-unmapped", field, "expected list")
        return ()
    result: list[int] = []
    for index, raw in enumerate(value):
        if (
            not isinstance(raw, Mapping)
            or set(raw) != {"rid"}
            or not isinstance(raw.get("rid"), int)
            or isinstance(raw.get("rid"), bool)
            or int(raw["rid"]) <= 0
        ):
            _issue(
                issues,
                "status-graph-unmapped",
                f"{field}[{index}]",
                "expected one positive rid",
            )
            continue
        result.append(int(raw["rid"]))
    if len(result) != len(set(result)):
        _issue(issues, "status-graph-unmapped", field, "duplicate rid")
    return tuple(result)


def _reference_table(
    opaque: Mapping[str, object], issues: list[Plan3LocalSaveIssue]
) -> dict[int, tuple[str, Mapping[str, object]]]:
    raw = opaque.get("references")
    if not isinstance(raw, Mapping) or set(raw) != {"version", "RefIds"}:
        _issue(
            issues,
            "status-reference-table-unmapped",
            "root_runtime.references",
        )
        return {}
    entries = raw.get("RefIds")
    if not isinstance(entries, list):
        _issue(
            issues,
            "status-reference-table-unmapped",
            "root_runtime.references.RefIds",
            "expected list",
        )
        return {}
    result: dict[int, tuple[str, Mapping[str, object]]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or set(entry) != {"rid", "type", "data"}:
            continue
        rid = entry.get("rid")
        raw_type = entry.get("type")
        data = entry.get("data")
        if (
            not isinstance(rid, int)
            or isinstance(rid, bool)
            or rid <= 0
            or not isinstance(raw_type, Mapping)
            or set(raw_type) != {"asm", "class", "ns"}
            or raw_type.get("asm") != _ASSEMBLY
            or raw_type.get("ns") != _NAMESPACE
            or not isinstance(raw_type.get("class"), str)
            or not isinstance(data, Mapping)
        ):
            continue
        result[rid] = (str(raw_type["class"]), data)
    return result


def _map_idol_status(
    raw: object, issues: list[Plan3LocalSaveIssue]
) -> tuple[str, int]:
    if (
        not isinstance(raw, Mapping)
        or set(raw) != {"_currentType", "_currentStep"}
        or not isinstance(raw.get("_currentType"), int)
        or isinstance(raw.get("_currentType"), bool)
        or not isinstance(raw.get("_currentStep"), int)
        or isinstance(raw.get("_currentStep"), bool)
    ):
        _issue(
            issues,
            "idol-status-unmapped",
            "root_runtime.status._idolStatusEffect",
        )
        return STANCE_NEUTRAL, 0
    native_type = int(raw["_currentType"])
    native_step = int(raw["_currentStep"])
    mapped = _STANCE_BY_NATIVE_TYPE.get(native_type)
    if mapped is None:
        _issue(
            issues,
            "idol-status-type-unsupported",
            "root_runtime.status._idolStatusEffect._currentType",
            str(native_type),
        )
        return STANCE_NEUTRAL, 0
    stance, fixed_level = mapped
    level = native_step if fixed_level is None else fixed_level
    valid = (
        (stance == STANCE_NEUTRAL and native_step == 0)
        or (stance in {STANCE_CONCENTRATION, STANCE_PRESERVATION} and 1 <= level <= 3)
        or (stance == STANCE_FULL_POWER and native_step == 1)
    )
    if not valid:
        _issue(
            issues,
            "idol-status-step-unsupported",
            "root_runtime.status._idolStatusEffect._currentStep",
            f"type={native_type}; step={native_step}",
        )
        return STANCE_NEUTRAL, 0
    return stance, level


def _scalar_status_value(
    data: Mapping[str, object],
    *,
    rid: int,
    class_name: str,
    issues: list[Plan3LocalSaveIssue],
) -> int | None:
    if set(data) != _SCALAR_STATUS_FIELDS:
        _issue(
            issues,
            "active-status-shape-unmapped",
            f"root_runtime.references[{rid}]",
            class_name,
        )
        return None
    value = data.get("_value")
    uid = data.get("_uid")
    lifecycle = (
        data.get("_isPassingTurnStart"),
        data.get("_isTurnLimited"),
        data.get("_turn"),
    )
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        or not isinstance(uid, int)
        or isinstance(uid, bool)
        or uid <= 0
    ):
        _issue(
            issues,
            "active-status-value-unmapped",
            f"root_runtime.references[{rid}]",
            class_name,
        )
        return None
    if lifecycle != (True, False, -1):
        _issue(
            issues,
            "active-status-lifecycle-unsupported",
            f"root_runtime.references[{rid}]",
            f"{class_name}:{lifecycle!r}",
        )
        return None
    return value


def _enthusiasm_additive_unlimited_status(
    data: Mapping[str, object],
    *,
    rid: int,
    create_count: int,
    issues: list[Plan3LocalSaveIssue],
) -> Plan3EnthusiasmAdditiveStatus | None:
    """Restore the proven unlimited native EnthusiasticAdditive layer."""

    field = f"root_runtime.references[{rid}]"
    if set(data) != _SCALAR_STATUS_FIELDS:
        _issue(
            issues,
            "active-status-shape-unmapped",
            field,
            _ENTHUSIASM_ADDITIVE_CLASS,
        )
        return None
    value = data.get("_value")
    uid = data.get("_uid")
    passing = data.get("_isPassingTurnStart")
    limited = data.get("_isTurnLimited")
    turn = data.get("_turn")
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= INT32_MAX
        or isinstance(uid, bool)
        or not isinstance(uid, int)
        or not 1 <= uid <= create_count
    ):
        _issue(
            issues,
            "active-status-value-unmapped",
            field,
            f"{_ENTHUSIASM_ADDITIVE_CLASS}:value={value!r};uid={uid!r}",
        )
        return None
    if (
        type(passing) is not bool
        or limited is not False
        or type(turn) is not int
        or turn != -1
    ):
        _issue(
            issues,
            "active-status-lifecycle-unsupported",
            field,
            (
                f"{_ENTHUSIASM_ADDITIVE_CLASS}:passing={passing!r};"
                f"limited={limited!r};turn={turn!r}"
            ),
        )
        return None
    return Plan3EnthusiasmAdditiveStatus(value=value, turns=-1)


def _anti_debuff_status(
    data: Mapping[str, object],
    *,
    rid: int,
    create_count: int,
    issues: list[Plan3LocalSaveIssue],
) -> AntiDebuffRuntime | None:
    """Restore the one exact active native AntiDebuff count status."""

    field = f"root_runtime.references[{rid}]"
    if set(data) != _ANTI_DEBUFF_STATUS_FIELDS:
        _issue(
            issues,
            "active-status-shape-unmapped",
            field,
            _ANTI_DEBUFF_CLASS,
        )
        return None
    count = data.get("_count")
    uid = data.get("_uid")
    passing = data.get("_isPassingTurnStart")
    limited = data.get("_isTurnLimited")
    turn = data.get("_turn")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not 1 <= count <= INT32_MAX
        or isinstance(uid, bool)
        or not isinstance(uid, int)
        or not 1 <= uid <= create_count
    ):
        _issue(
            issues,
            "active-status-shape-unmapped",
            field,
            f"{_ANTI_DEBUFF_CLASS}:count={count!r};uid={uid!r}",
        )
        return None
    if (
        type(passing) is not bool
        or limited is not False
        or type(turn) is not int
        or turn != -1
    ):
        _issue(
            issues,
            "active-status-lifecycle-unsupported",
            field,
            (
                f"{_ANTI_DEBUFF_CLASS}:passing={passing!r};"
                f"limited={limited!r};turn={turn!r}"
            ),
        )
        return None
    return AntiDebuffRuntime(
        count=count,
        uid=uid,
        passing_turn_start=passing,
    )


def _playable_value_add_status(
    data: Mapping[str, object],
    *,
    rid: int,
    create_count: int,
    issues: list[Plan3LocalSaveIssue],
) -> PlayableValueAddRuntime | None:
    """Restore the single exact active native PlayableValueAdd status."""

    field = f"root_runtime.references[{rid}]"
    if set(data) != _SCALAR_STATUS_FIELDS:
        _issue(
            issues,
            "active-status-shape-unmapped",
            field,
            _PLAYABLE_VALUE_ADD_CLASS,
        )
        return None
    value = data.get("_value")
    uid = data.get("_uid")
    passing = data.get("_isPassingTurnStart")
    limited = data.get("_isTurnLimited")
    turn = data.get("_turn")
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= INT32_MAX
        or isinstance(uid, bool)
        or not isinstance(uid, int)
        or not 1 <= uid <= create_count
    ):
        _issue(
            issues,
            "active-status-shape-unmapped",
            field,
            f"{_PLAYABLE_VALUE_ADD_CLASS}:value={value!r};uid={uid!r}",
        )
        return None
    if (
        type(passing) is not bool
        or limited is not True
        or type(turn) is not int
        or turn != 1
    ):
        _issue(
            issues,
            "active-status-lifecycle-unsupported",
            field,
            (
                f"{_PLAYABLE_VALUE_ADD_CLASS}:passing={passing!r};"
                f"limited={limited!r};turn={turn!r}"
            ),
        )
        return None
    return PlayableValueAddRuntime(
        PlayableValueAddStatus(
            uid=uid,
            value=value,
            turn=turn,
            passing_turn_start=passing,
        )
    )


def _enthusiastic_status(
    data: Mapping[str, object],
    *,
    rid: int,
    create_count: int,
    issues: list[Plan3LocalSaveIssue],
) -> EnthusiasticRuntime | None:
    """Restore one exact passing turn-one Enthusiastic status."""

    field = f"root_runtime.references[{rid}]"
    if set(data) != _SCALAR_STATUS_FIELDS:
        _issue(
            issues,
            "active-status-shape-unmapped",
            field,
            _ENTHUSIASM_CLASS,
        )
        return None
    value = data.get("_value")
    uid = data.get("_uid")
    passing = data.get("_isPassingTurnStart")
    limited = data.get("_isTurnLimited")
    turn = data.get("_turn")
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= INT32_MAX
        or isinstance(uid, bool)
        or not isinstance(uid, int)
        or not 1 <= uid <= create_count
    ):
        _issue(
            issues,
            "active-status-shape-unmapped",
            field,
            f"{_ENTHUSIASM_CLASS}:value={value!r};uid={uid!r}",
        )
        return None
    if (
        passing is not True
        or limited is not True
        or type(turn) is not int
        or turn != 1
    ):
        _issue(
            issues,
            "active-status-lifecycle-unsupported",
            field,
            (
                f"{_ENTHUSIASM_CLASS}:passing={passing!r};"
                f"limited={limited!r};turn={turn!r}"
            ),
        )
        return None
    return EnthusiasticRuntime(
        EnthusiasticStatus(
            uid=uid,
            value=value,
            turn=turn,
            passing_turn_start=True,
        )
    )


def _turn_limited_status_value(
    data: Mapping[str, object],
    *,
    rid: int,
    class_name: str,
    issues: list[Plan3LocalSaveIssue],
    requires_value: bool,
) -> int | None:
    expected_fields = _SCALAR_STATUS_FIELDS if requires_value else _TURN_STATUS_FIELDS
    if set(data) != expected_fields:
        _issue(
            issues,
            "active-status-shape-unmapped",
            f"root_runtime.references[{rid}]",
            class_name,
        )
        return None
    passing = data.get("_isPassingTurnStart")
    limited = data.get("_isTurnLimited")
    turns = data.get("_turn")
    uid = data.get("_uid")
    if (
        passing is not True
        or limited is not True
        or not isinstance(turns, int)
        or isinstance(turns, bool)
        or turns <= 0
        or not isinstance(uid, int)
        or isinstance(uid, bool)
        or uid <= 0
    ):
        _issue(
            issues,
            "active-status-lifecycle-unsupported",
            f"root_runtime.references[{rid}]",
            f"{class_name}:passing={passing!r}; limited={limited!r}; turns={turns!r}",
        )
        return None
    if not requires_value:
        return turns
    value = data.get("_value")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _issue(
            issues,
            "active-status-value-unmapped",
            f"root_runtime.references[{rid}]",
            class_name,
        )
        return None
    return value


def _stamina_consumption_down_status(
    data: Mapping[str, object],
    *,
    rid: int,
    issues: list[Plan3LocalSaveIssue],
    class_name: str = _STAMINA_CONSUMPTION_DOWN_CLASS,
) -> tuple[int, bool] | None:
    """Restore the native Down status and retain its fresh lifecycle bit.

    Unlike the turn-start scalar statuses, this status is commonly created by
    an effect after the current turn has already started.  Native therefore
    legitimately serializes ``_isPassingTurnStart=False`` with a finite
    positive ``_turn``.  That false value is the exact evidence needed to keep
    the Plan3 ``stamina_consumption_down_fresh`` boundary marker set.
    """

    if set(data) != _TURN_STATUS_FIELDS:
        _issue(
            issues,
            "active-status-shape-unmapped",
            f"root_runtime.references[{rid}]",
            class_name,
        )
        return None
    passing = data.get("_isPassingTurnStart")
    limited = data.get("_isTurnLimited")
    turns = data.get("_turn")
    uid = data.get("_uid")
    if (
        type(passing) is not bool
        or type(limited) is not bool
        or isinstance(turns, bool)
        or not isinstance(turns, int)
        or turns < -1
        or isinstance(uid, bool)
        or not isinstance(uid, int)
        or uid <= 0
        or limited != (turns != -1)
        or (limited and turns <= 0)
    ):
        _issue(
            issues,
            "active-status-lifecycle-unsupported",
            f"root_runtime.references[{rid}]",
            f"{class_name}:passing={passing!r};"
            f" limited={limited!r}; turns={turns!r}",
        )
        return None
    return turns, passing


def _stamina_consumption_down_fix_status(
    data: Mapping[str, object],
    *,
    rid: int,
    create_count: int,
    issues: list[Plan3LocalSaveIssue],
) -> Plan3StaminaConsumptionDownFixStatus | None:
    """Restore each fixed reduction layer without merging its lifetime."""

    field = f"root_runtime.references[{rid}]"
    if set(data) != _SCALAR_STATUS_FIELDS:
        _issue(issues, "active-status-shape-unmapped", field,
               _STAMINA_CONSUMPTION_DOWN_FIX_CLASS)
        return None
    value, uid = data.get("_value"), data.get("_uid")
    if (type(value) is not int or not 1 <= value <= INT32_MAX
            or type(uid) is not int or not 1 <= uid <= create_count):
        _issue(issues, "active-status-value-unmapped", field,
               f"{_STAMINA_CONSUMPTION_DOWN_FIX_CLASS}:value={value!r};uid={uid!r}")
        return None
    lifecycle = _stamina_consumption_down_status(
        {key: data[key] for key in _TURN_STATUS_FIELDS},
        rid=rid,
        issues=issues,
        class_name=_STAMINA_CONSUMPTION_DOWN_FIX_CLASS,
    )
    if lifecycle is None:
        return None
    turns, passing = lifecycle
    if turns > INT32_MAX:
        _issue(issues, "active-status-lifecycle-unsupported", field,
               f"{_STAMINA_CONSUMPTION_DOWN_FIX_CLASS}:turns={turns!r}")
        return None
    return Plan3StaminaConsumptionDownFixStatus(
        value=value, turns=turns, fresh=not passing,
    )


def _restore_card_trigger_listener(
    data: Mapping[str, object],
    *,
    rid: int,
    database: Path,
    issues: list[Plan3LocalSaveIssue],
) -> ActivePlan3StatusEnchant | None:
    """Restore a status listener installed by a played card.

    A card-owned ``TriggerEffectStatusEffect`` has no item or gimmick source;
    its source card is serialized in ``_triggerCard``.  Resolve that card and
    its status-enchant effect through Master, then compare the copied native
    trigger/effect graph before exposing it to the Plan3 reducer.  The mutable
    phase/turn counters come from the reference graph, never from the display
    turn-start list.
    """

    status_id = data.get("_statusEnchantId")
    trigger_card = data.get("_triggerCard")
    card_data = (
        trigger_card.get("_cardData")
        if isinstance(trigger_card, Mapping)
        else None
    )
    card_id = card_data.get("_id") if isinstance(card_data, Mapping) else None
    upgrade = (
        card_data.get("_upgradeCount") if isinstance(card_data, Mapping) else None
    )
    if (
        not isinstance(status_id, str)
        or not status_id
        or not isinstance(card_id, str)
        or not card_id
        or isinstance(upgrade, bool)
        or not isinstance(upgrade, int)
        or upgrade < 0
        or upgrade > 3
    ):
        _issue(
            issues,
            "card-trigger-listener-shape-unmapped",
            f"root_runtime.references[{rid}]",
            f"card={card_id!r}+{upgrade!r};status={status_id!r}",
        )
        return None

    # Card-origin status creation has a distinct serializer lineage.  Keep the
    # checks on the reference object rather than accepting a HUD/display hint.
    lifecycle = (
        data.get("_isItemDirectEnchant"),
        data.get("_isTurnLimited"),
        data.get("_turn"),
        data.get("_originType"),
        data.get("_isFromEnchantEffect"),
        data.get("_isEncoreEnchant"),
    )
    if (
        lifecycle[0] is not False
        or lifecycle[3:] != (1, False, False)
        or data.get("_startEnchantOriginId") != ""
        or data.get("_startEnchantOriginLevel") != 0
        or data.get("_startEnchantOriginType") != 0
        or data.get("_startEnchantOwnerId") != ""
        or data.get("_fromExamEffectIdList") != []
    ):
        _issue(
            issues,
            "card-trigger-listener-lifecycle-unsupported",
            f"root_runtime.references[{rid}]",
            repr(lifecycle),
        )
        return None

    try:
        card = load_plan3_card(card_id, upgrade, database)
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as error:
        _issue(
            issues,
            "card-trigger-listener-master-unavailable",
            f"root_runtime.references[{rid}]",
            f"{type(error).__name__}:{card_id}+{upgrade}",
        )
        return None
    matches = [
        effect
        for effect in card.effects
        if (
            effect.effect_type == EFFECT_STATUS_ENCHANT
            and effect.status_enchant_id == status_id
            and effect.status_enchant is not None
        )
    ]
    if len(matches) != 1:
        _issue(
            issues,
            "card-trigger-listener-master-mismatch",
            f"root_runtime.references[{rid}]",
            f"card={card_id}+{upgrade};status={status_id};matches={len(matches)}",
        )
        return None
    installer = matches[0]
    assert installer.status_enchant is not None
    rule = installer.status_enchant
    rule_errors = validate_plan3_status_enchant(rule)
    if rule_errors:
        _issue(
            issues,
            "card-trigger-listener-master-unsupported",
            f"root_runtime.references[{rid}]",
            repr(rule_errors),
        )
        return None

    native_trigger = data.get("_trigger")
    if not isinstance(native_trigger, Mapping):
        _issue(
            issues,
            "card-trigger-listener-shape-unmapped",
            f"root_runtime.references[{rid}]._trigger",
            "expected object",
        )
        return None
    expected_phase_types = [
        _NATIVE_PHASE_TYPE_BY_NAME.get(phase)
        for phase in rule.trigger.phase_types
    ]
    if any(value is None for value in expected_phase_types):
        _issue(
            issues,
            "card-trigger-listener-master-unsupported",
            f"root_runtime.references[{rid}]._trigger",
            f"phase-types={rule.trigger.phase_types!r}",
        )
        return None
    if (
        native_trigger.get("_id") != rule.trigger.id
        or native_trigger.get("_phaseTypeList") != expected_phase_types
        or native_trigger.get("_phaseValueList") != list(rule.trigger.phase_values)
    ):
        _issue(
            issues,
            "card-trigger-listener-master-mismatch",
            f"root_runtime.references[{rid}]._trigger",
            (
                f"trigger={native_trigger.get('_id')!r}/{rule.trigger.id!r};"
                f"phase-types={native_trigger.get('_phaseTypeList')!r}/{expected_phase_types!r};"
                f"phase-values={native_trigger.get('_phaseValueList')!r}/"
                f"{list(rule.trigger.phase_values)!r}"
            ),
        )
        return None

    raw_effects = data.get("_effectList")
    if not isinstance(raw_effects, list) or len(raw_effects) != len(rule.effects):
        _issue(
            issues,
            "card-trigger-listener-master-mismatch",
            f"root_runtime.references[{rid}]._effectList",
            f"native-count={len(raw_effects) if isinstance(raw_effects, list) else None};"
            f"master-count={len(rule.effects)}",
        )
        return None
    for raw_effect, master_effect in zip(raw_effects, rule.effects, strict=True):
        if not _native_effect_payload_matches_master(
            raw_effect, master_effect.id, database=database
        ):
            _issue(
                issues,
                "card-trigger-listener-master-mismatch",
                f"root_runtime.references[{rid}]._effectList",
                f"effect={master_effect.id}",
            )
            return None

    uid = data.get("_uid")
    turn_count = data.get("_turnCount")
    if (
        isinstance(uid, bool)
        or not isinstance(uid, int)
        or uid <= 0
        or isinstance(turn_count, bool)
        or not isinstance(turn_count, int)
        or turn_count < 0
    ):
        _issue(
            issues,
            "card-trigger-listener-lifecycle-unsupported",
            f"root_runtime.references[{rid}]",
            f"uid={uid!r};turn_count={turn_count!r}",
        )
        return None

    raw_phase_counts = data.get("_phaseCountDictionary")
    raw_phase_count_rows = (
        raw_phase_counts.get("_list")
        if isinstance(raw_phase_counts, Mapping)
        else None
    )
    if not isinstance(raw_phase_count_rows, list):
        _issue(
            issues,
            "card-trigger-listener-phase-counts-unsupported",
            f"root_runtime.references[{rid}]._phaseCountDictionary",
            "expected {_list: array}",
        )
        return None
    phase_counts: list[tuple[str, int]] = []
    seen_phase_keys: set[int] = set()
    for row in raw_phase_count_rows:
        if not isinstance(row, Mapping):
            _issue(
                issues,
                "card-trigger-listener-phase-counts-unsupported",
                f"root_runtime.references[{rid}]._phaseCountDictionary",
                "phase row is not an object",
            )
            return None
        key = row.get("key")
        value = row.get("value")
        current = value.get("current") if isinstance(value, Mapping) else None
        phase_name = _NATIVE_PHASE_NAME_BY_VALUE.get(key)
        if (
            isinstance(key, bool)
            or not isinstance(key, int)
            or key in seen_phase_keys
            or phase_name is None
            or phase_name not in rule.trigger.phase_types
            or isinstance(current, bool)
            or not isinstance(current, int)
            or current < 0
        ):
            _issue(
                issues,
                "card-trigger-listener-phase-counts-unsupported",
                f"root_runtime.references[{rid}]._phaseCountDictionary",
                repr(row),
            )
            return None
        seen_phase_keys.add(key)
        phase_counts.append((phase_name, current))

    max_uses = installer.effect_count
    max_uses_per_turn = installer.value1
    remaining_total = data.get("_limitCount")
    remaining_per_turn = data.get("_limitCountInTurnRemain")
    serialized_per_turn = data.get("_limitCountInTurn")
    expected_per_turn = -1 if max_uses_per_turn == 0 else max_uses_per_turn
    if serialized_per_turn != expected_per_turn:
        _issue(
            issues,
            "card-trigger-listener-remaining-invalid",
            f"root_runtime.references[{rid}]",
            f"per-turn={serialized_per_turn!r};master={expected_per_turn!r}",
        )
        return None
    if max_uses == 0:
        if remaining_total != -1:
            _issue(
                issues,
                "card-trigger-listener-remaining-invalid",
                f"root_runtime.references[{rid}]",
                f"unlimited remaining={remaining_total!r}",
            )
            return None
        uses = 0
    else:
        if (
            isinstance(remaining_total, bool)
            or not isinstance(remaining_total, int)
            or not 0 <= remaining_total <= max_uses
        ):
            _issue(
                issues,
                "card-trigger-listener-remaining-invalid",
                f"root_runtime.references[{rid}]",
                f"remaining={remaining_total!r};max={max_uses}",
            )
            return None
        uses = max_uses - remaining_total
    if max_uses_per_turn == 0:
        if remaining_per_turn != -1:
            _issue(
                issues,
                "card-trigger-listener-remaining-invalid",
                f"root_runtime.references[{rid}]",
                f"unlimited per-turn remaining={remaining_per_turn!r}",
            )
            return None
        uses_this_turn = 0
    else:
        if (
            isinstance(remaining_per_turn, bool)
            or not isinstance(remaining_per_turn, int)
            or not 0 <= remaining_per_turn <= max_uses_per_turn
        ):
            _issue(
                issues,
                "card-trigger-listener-remaining-invalid",
                f"root_runtime.references[{rid}]",
                f"per-turn-remaining={remaining_per_turn!r};"
                f"max={max_uses_per_turn}",
            )
            return None
        uses_this_turn = max_uses_per_turn - remaining_per_turn

    native_turn = data.get("_turn")
    expected_turn = installer.effect_turn
    expected_limited = expected_turn != -1
    if (
        isinstance(native_turn, bool)
        or not isinstance(native_turn, int)
        or native_turn != expected_turn
        or data.get("_isTurnLimited") is not expected_limited
    ):
        _issue(
            issues,
            "card-trigger-listener-lifecycle-unsupported",
            f"root_runtime.references[{rid}]",
            f"turn={native_turn!r}/{expected_turn!r};"
            f"limited={data.get('_isTurnLimited')!r}/{expected_limited!r}",
        )
        return None

    return ActivePlan3StatusEnchant(
        instance_id=f"localsave:{rid}:{status_id}",
        source_id=card_id,
        rule=rule,
        max_uses=max_uses,
        uses=uses,
        max_uses_per_turn=max_uses_per_turn,
        uses_this_turn=uses_this_turn,
        remaining_turns=native_turn,
        passing_turn_start=data.get("_isPassingTurnStart") is True,
        is_item_direct=False,
        turn_count=turn_count,
        phase_counts=tuple(phase_counts),
        native_uid=uid,
    )


def _restore_effect_timer_listener(
    data: Mapping[str, object],
    *,
    rid: int,
    database: Path,
    issues: list[Plan3LocalSaveIssue],
    source_cards: Mapping[str, tuple[str, int]] | None = None,
) -> ActivePlan3StatusEnchant | None:
    """Restore one native ``ExamEffectTimer`` listener from its source card.

    ``ExamEffectTimer`` is serialized as a ``TriggerEffectStatusEffect`` with
    ``overrideType=30`` and ``originType=2``.  Unlike a card-owned status
    listener it has no status-enchant ID; its durable identity is the source
    card GUID, the timer phase value, and the chained Master effect ID.  The
    timer is therefore resolved against the captured source card and Master
    card effect graph instead of being inferred from a RID or a display list.
    """

    trigger_card = data.get("_triggerCard")
    card_data = (
        trigger_card.get("_cardData")
        if isinstance(trigger_card, Mapping)
        else None
    )
    card_id = card_data.get("_id") if isinstance(card_data, Mapping) else None
    upgrade = (
        card_data.get("_upgradeCount") if isinstance(card_data, Mapping) else None
    )
    source_guid = (
        trigger_card.get("_guid")
        if isinstance(trigger_card, Mapping)
        else None
    )
    trigger = data.get("_trigger")
    phase_types = trigger.get("_phaseTypeList") if isinstance(trigger, Mapping) else None
    phase_values = trigger.get("_phaseValueList") if isinstance(trigger, Mapping) else None
    raw_effects = data.get("_effectList")
    from_effect_ids = data.get("_fromExamEffectIdList")
    lifecycle = (
        data.get("_overrideType"),
        data.get("_originType"),
        data.get("_isItemDirectEnchant"),
        data.get("_isTurnLimited"),
        data.get("_turn"),
        data.get("_isFromEnchantEffect"),
        data.get("_isEncoreEnchant"),
        data.get("_statusEnchantId"),
    )
    if (
        not isinstance(card_id, str)
        or not card_id
        or isinstance(upgrade, bool)
        or not isinstance(upgrade, int)
        or upgrade < 0
        or upgrade > 3
        or not isinstance(source_guid, str)
        or not source_guid
        or lifecycle
        != (30, 2, False, False, -1, False, False, "")
        or phase_types != [_TURN_TIMER_PHASE_VALUE]
        or not isinstance(phase_values, list)
        or len(phase_values) != 1
        or isinstance(phase_values[0], bool)
        or not isinstance(phase_values[0], int)
        or phase_values[0] <= 0
        or not isinstance(raw_effects, list)
        or len(raw_effects) != 1
        or not isinstance(from_effect_ids, list)
        or len(from_effect_ids) != 1
        or not isinstance(from_effect_ids[0], str)
        or not from_effect_ids[0]
        or not isinstance(data.get("_phaseCountDictionary"), Mapping)
        or data.get("_phaseCountDictionary") != {"_list": []}
    ):
        _issue(
            issues,
            "effect-timer-listener-shape-unmapped",
            f"root_runtime.references[{rid}]",
            f"card={card_id!r}+{upgrade!r};guid={source_guid!r}",
        )
        return None

    # The GUID must bind to the current live card instance.  This prevents a
    # stale removed-card snapshot from selecting a different Master row or
    # silently attaching a timer to an unrelated duplicate card.
    if source_cards is not None:
        observed = source_cards.get(source_guid)
        if observed is None:
            _issue(
                issues,
                "effect-timer-source-card-missing",
                f"root_runtime.references[{rid}]._triggerCard._guid",
                source_guid,
            )
            return None
        if observed != (card_id, upgrade):
            _issue(
                issues,
                "effect-timer-source-card-mismatch",
                f"root_runtime.references[{rid}]._triggerCard",
                f"serialized={(card_id, upgrade)!r};live={observed!r}",
            )
            return None

    uid = data.get("_uid")
    turn_count = data.get("_turnCount")
    remaining = data.get("_limitCount")
    if (
        isinstance(uid, bool)
        or not isinstance(uid, int)
        or uid <= 0
        or isinstance(turn_count, bool)
        or not isinstance(turn_count, int)
        or turn_count < 0
        or isinstance(remaining, bool)
        or not isinstance(remaining, int)
        or remaining not in (0, 1)
        or data.get("_limitCountInTurn") != -1
        or data.get("_limitCountInTurnRemain") != -1
    ):
        _issue(
            issues,
            "effect-timer-listener-lifecycle-unsupported",
            f"root_runtime.references[{rid}]",
            f"uid={uid!r};turn_count={turn_count!r};remaining={remaining!r}",
        )
        return None

    child_id = from_effect_ids[0]
    raw_child = raw_effects[0]
    if (
        not isinstance(raw_child, Mapping)
        or raw_child.get("_id") != child_id
        or not _native_effect_payload_matches_master(
            raw_child,
            child_id,
            database=database,
        )
    ):
        _issue(
            issues,
            "effect-timer-listener-child-mismatch",
            f"root_runtime.references[{rid}]._effectList",
            f"child={child_id!r}",
        )
        return None

    try:
        card = load_plan3_card(card_id, upgrade, database)
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as error:
        _issue(
            issues,
            "effect-timer-listener-master-unavailable",
            f"root_runtime.references[{rid}]",
            f"{type(error).__name__}:{card_id}+{upgrade}",
        )
        return None

    # A source card may contain several timers.  Match the serialized delay
    # and chained child, then require one unique Master row; no RID or card-ID
    # special case is used here.
    delay = phase_values[0]
    timer_candidates = tuple(
        effect
        for effect in card.effects
        if (
            effect.effect_type == EFFECT_TIMER
            and effect.value1 == delay
            and effect.chain_effect_id == child_id
            and effect.chain_effect is not None
            and effect.chain_effect.id == child_id
        )
    )
    if len(timer_candidates) != 1:
        _issue(
            issues,
            "effect-timer-listener-master-mismatch",
            f"root_runtime.references[{rid}]",
            f"card={card_id}+{upgrade};delay={delay};child={child_id};matches={len(timer_candidates)}",
        )
        return None
    timer = timer_candidates[0]
    if (
        timer.value2 != 0
        or timer.effect_count != 1
        or timer.effect_turn != 0
        or timer.chain_effect_ids
        or timer.status_enchant_id
        or timer.status_enchant is not None
        or timer.trigger is not None
    ):
        _issue(
            issues,
            "effect-timer-listener-master-unsupported",
            f"root_runtime.references[{rid}]",
            f"timer={timer.id}",
        )
        return None

    assert timer.chain_effect is not None
    rule = Plan3StatusEnchantRule(
        id=f"effect-timer:{timer.id}",
        trigger=Plan3Trigger(
            id=f"effect-timer-trigger:{timer.id}",
            phase_types=(PHASE_TURN_TIMER,),
            phase_values=(delay,),
        ),
        effects=(timer.chain_effect,),
    )
    return ActivePlan3StatusEnchant(
        instance_id=f"localsave:{rid}:{timer.id}:{source_guid}",
        source_id=timer.id,
        rule=rule,
        max_uses=1,
        uses=1 - remaining,
        remaining_turns=-1,
        passing_turn_start=data.get("_isPassingTurnStart") is True,
        is_item_direct=False,
        turn_count=turn_count,
        native_uid=uid,
    )


def _lesson_parameter_multiple_status(
    data: Mapping[str, object],
    *,
    rid: int,
    issues: list[Plan3LocalSaveIssue],
) -> LessonParameterMultipleStatus | None:
    """Restore one live ``LessonParameterMultipleStatusEffect`` reference.

    The status collection is linked from ``status._effectList``; the display
    ``currentTurnStartStatusList`` is only a stale turn-start rendering and is
    never consulted here.  Keep the native active-list order and all four
    mutable identity fields (value, duration, passing marker, and UID).
    """

    if set(data) != _SCALAR_STATUS_FIELDS:
        _issue(
            issues,
            "active-status-shape-unmapped",
            f"root_runtime.references[{rid}]",
            _LESSON_PARAMETER_MULTIPLE_CLASS,
        )
        return None
    value = data.get("_value")
    turn = data.get("_turn")
    passing = data.get("_isPassingTurnStart")
    limited = data.get("_isTurnLimited")
    uid = data.get("_uid")
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or isinstance(turn, bool)
        or not isinstance(turn, int)
        or type(passing) is not bool
        or type(limited) is not bool
        or isinstance(uid, bool)
        or not isinstance(uid, int)
        or uid <= 0
    ):
        _issue(
            issues,
            "active-status-value-unmapped",
            f"root_runtime.references[{rid}]",
            _LESSON_PARAMETER_MULTIPLE_CLASS,
        )
        return None
    # Native LessonParameterMultipleStatusEffect uses -1 for an unlimited
    # layer; finite layers have a positive counter and keep the corresponding
    # _isTurnLimited flag.  A zero counter is already expired at a settled
    # boundary, so accepting it would create a state the native collection
    # cannot expose to the next action.
    if turn == 0 or turn < -1 or limited != (turn != -1):
        _issue(
            issues,
            "active-status-lifecycle-unsupported",
            f"root_runtime.references[{rid}]",
            f"{_LESSON_PARAMETER_MULTIPLE_CLASS}:turn={turn!r};limited={limited!r}",
        )
        return None
    try:
        return LessonParameterMultipleStatus(
            permil=value,
            turn=turn,
            passing_turn_start=passing,
            is_turn_limited=limited,
            uid=uid,
        )
    except (TypeError, ValueError) as error:
        _issue(
            issues,
            "active-status-value-unmapped",
            f"root_runtime.references[{rid}]",
            f"{_LESSON_PARAMETER_MULTIPLE_CLASS}:{type(error).__name__}:{error}",
        )
        return None


def _restore_trigger_listener(
    data: Mapping[str, object],
    *,
    rid: int,
    database: Path,
    master_dir: Path,
    issues: list[Plan3LocalSaveIssue],
    triggered_ids: frozenset[str] = frozenset(),
    source_cards: Mapping[str, tuple[str, int]] | None = None,
) -> ActivePlan3StatusEnchant | None:
    item = data.get("_triggerItem")
    item_id = item.get("_id") if isinstance(item, Mapping) else None
    status_id = data.get("_statusEnchantId")
    gimmick = data.get("_triggerGimmickGroup")
    gimmick_id = gimmick.get("_id") if isinstance(gimmick, Mapping) else None
    drink = data.get("_triggerDrink")
    if isinstance(drink, Mapping) and drink.get("_id"):
        from .plan3_drink_status import restore_plan3_drink_listener
        try:
            return restore_plan3_drink_listener(data, rid=rid, database=database,
                phase_values=_NATIVE_PHASE_TYPE_BY_NAME, effect_matches=_native_effect_payload_matches_master)
        except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as error:
            _issue(issues, "drink-trigger-listener-invalid", f"root_runtime.references[{rid}]", str(error))
            return None
    # Native ExamEffectTimer listeners use the same TriggerEffectStatusEffect
    # serializer as card listeners, but carry no status-enchant ID.  Dispatch
    # this shape before the card-status branch so it is resolved from the
    # source card's Master timer chain rather than rejected as malformed card
    # status data.
    if (
        item_id == ""
        and data.get("_overrideType") == 30
        and data.get("_originType") == 2
        and status_id == ""
    ):
        return _restore_effect_timer_listener(
            data,
            rid=rid,
            database=database,
            issues=issues,
            source_cards=source_cards,
        )
    if item_id == "" and isinstance(gimmick_id, str) and gimmick_id:
        return _restore_gimmick_trigger_listener(
            data,
            rid=rid,
            database=database,
            master_dir=master_dir,
            issues=issues,
        )
    if item_id == "" and data.get("_startEnchantOriginType") == 4:
        origin_id = data.get("_startEnchantOriginId")
        origin_level = data.get("_startEnchantOriginLevel")
        owner_id = data.get("_startEnchantOwnerId")
        lifecycle = (
            data.get("_isPassingTurnStart"),
            data.get("_isTurnLimited"),
            data.get("_turn"),
            data.get("_isItemDirectEnchant"),
        )
        counters = (
            data.get("_limitCount"),
            data.get("_limitCountInTurn"),
            data.get("_limitCountInTurnRemain"),
            data.get("_turnCount"),
        )
        uid = data.get("_uid")
        if (
            not isinstance(origin_id, str)
            or not origin_id.startswith("p_memory_skill-")
            or not isinstance(origin_level, int)
            or isinstance(origin_level, bool)
            or origin_level <= 0
            or not isinstance(owner_id, str)
            or not owner_id
            or not isinstance(status_id, str)
            or not status_id
            or lifecycle[0] not in (True, False)
            or lifecycle[1:] != (False, -1, False)
            or counters != (-1, -1, -1, 0)
            or not isinstance(uid, int)
            or isinstance(uid, bool)
            or uid <= 0
            or data.get("_overrideType") != 31
            or data.get("_originType") != 1
            or data.get("_isFromEnchantEffect") is not False
            or data.get("_isEncoreEnchant") is not False
            or data.get("_fromExamEffectIdList") != []
        ):
            _issue(
                issues,
                "memory-trigger-listener-lifecycle-unsupported",
                f"root_runtime.references[{rid}]",
                f"origin={origin_id!r}+{origin_level!r}; lifecycle={lifecycle!r}; counters={counters!r}",
            )
            return None
        try:
            source_statuses = _memory_status_source_index(
                Path(master_dir)
            ).get((origin_id, origin_level), ())
            rule = load_plan3_status_enchant(status_id, database)
        except (
            KeyError,
            TypeError,
            ValueError,
            OSError,
            sqlite3.Error,
            yaml.YAMLError,
        ) as error:
            _issue(
                issues,
                "memory-trigger-listener-master-unavailable",
                f"root_runtime.references[{rid}]",
                f"{type(error).__name__}:{origin_id}",
            )
            return None
        if source_statuses.count(status_id) != 1:
            _issue(
                issues,
                "memory-trigger-listener-source-mismatch",
                f"root_runtime.references[{rid}]",
                f"origin={origin_id!r}+{origin_level}; status={status_id!r}; candidates={source_statuses!r}",
            )
            return None
        rule_errors = validate_plan3_status_enchant(
            rule, allow_native_grow=True
        )
        if rule_errors:
            _issue(
                issues,
                "memory-trigger-listener-master-unsupported",
                f"root_runtime.references[{rid}]",
                repr(rule_errors),
            )
            return None
        native_trigger = data.get("_trigger")
        native_trigger_id = (
            native_trigger.get("_id")
            if isinstance(native_trigger, Mapping)
            else None
        )
        native_phase_types = (
            native_trigger.get("_phaseTypeList")
            if isinstance(native_trigger, Mapping)
            else None
        )
        native_phase_values = (
            native_trigger.get("_phaseValueList")
            if isinstance(native_trigger, Mapping)
            else None
        )
        raw_effects = data.get("_effectList")
        native_effect_ids = tuple(
            effect.get("_id") if isinstance(effect, Mapping) else None
            for effect in raw_effects
        ) if isinstance(raw_effects, list) else ()
        master_effect_ids = tuple(effect.id for effect in rule.effects)
        if (
            native_trigger_id != rule.trigger.id
            or native_phase_types != [_TURN_TIMER_PHASE_VALUE]
            or native_phase_values != list(rule.trigger.phase_values)
            or native_effect_ids != master_effect_ids
            or not isinstance(raw_effects, list)
            or len(raw_effects) != len(rule.effects)
            or not all(
                _memory_runtime_effect_matches(raw, effect)
                for raw, effect in zip(raw_effects, rule.effects, strict=True)
            )
            or status_id in triggered_ids
        ):
            _issue(
                issues,
                "memory-trigger-listener-master-mismatch",
                f"root_runtime.references[{rid}]",
                (
                    f"trigger={native_trigger_id!r}/{rule.trigger.id!r}; "
                    f"phase={native_phase_types!r}/{native_phase_values!r}; "
                    f"effects={native_effect_ids!r}/{master_effect_ids!r}; "
                    f"already_triggered={status_id in triggered_ids}"
                ),
            )
            return None
        return ActivePlan3StatusEnchant(
            instance_id=f"localsave:{rid}:{status_id}",
            source_id=origin_id,
            rule=rule,
            max_uses=0,
            uses=0,
            uses_this_turn=0,
            remaining_turns=-1,
            passing_turn_start=bool(lifecycle[0]),
            is_item_direct=False,
        )
    trigger_card = data.get("_triggerCard")
    card_data = (
        trigger_card.get("_cardData")
        if isinstance(trigger_card, Mapping)
        else None
    )
    card_id = card_data.get("_id") if isinstance(card_data, Mapping) else None
    if (
        item_id == ""
        and isinstance(card_id, str)
        and card_id
        and isinstance(status_id, str)
        and status_id
    ):
        return _restore_card_trigger_listener(
            data,
            rid=rid,
            database=database,
            issues=issues,
        )
    if (
        not isinstance(item_id, str)
        or not item_id
        or not isinstance(status_id, str)
        or not status_id
    ):
        _issue(
            issues,
            "trigger-listener-unsupported",
            f"root_runtime.references[{rid}]",
            f"item={item_id!r}; status={status_id!r}",
        )
        return None
    if (
        not isinstance(item, Mapping)
        or set(item) != set(_ITEM_SOURCE_FIELDS)
        or item.get("_itemType") != 1
        or item.get("_parentCustomItemIds") != []
        or any(
            isinstance(item.get(name), bool)
            or not isinstance(item.get(name), int)
            or item.get(name, -1) < 0
            for name in ("_fireCount", "_reactionCount")
        )
        or data.get("_isItemDirectEnchant") is not True
    ):
        _issue(
            issues,
            "trigger-listener-lifecycle-unsupported",
            f"root_runtime.references[{rid}]",
            f"item={item!r}; is_item_direct={data.get('_isItemDirectEnchant')!r}",
        )
        return None
    try:
        item_rule = load_plan3_item(item_id, database)
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error) as error:
        _issue(
            issues,
            "trigger-listener-master-unavailable",
            f"root_runtime.references[{rid}]",
            f"{type(error).__name__}:{item_id}",
        )
        return None
    matches = [
        (rule, max_uses)
        for rule, max_uses in item_rule.enchantments
        if rule.id == status_id
    ]
    if item_rule.unsupported_rules or len(matches) != 1:
        _issue(
            issues,
            "trigger-listener-master-unsupported",
            f"root_runtime.references[{rid}]",
            f"matches={len(matches)}; rules={item_rule.unsupported_rules!r}",
        )
        return None
    rule, max_uses = matches[0]
    rule_errors = validate_plan3_status_enchant(rule)
    if rule_errors:
        _issue(
            issues,
            "trigger-listener-master-unsupported",
            f"root_runtime.references[{rid}]",
            repr(rule_errors),
        )
        return None
    native_trigger = data.get("_trigger")
    raw_effects = data.get("_effectList")
    expected_phase_types = tuple(
        _NATIVE_PHASE_TYPE_BY_NAME.get(phase)
        for phase in rule.trigger.phase_types
    )
    native_effect_ids = tuple(
        effect.get("_id") if isinstance(effect, Mapping) else None
        for effect in raw_effects
    ) if isinstance(raw_effects, list) else ()
    master_effect_ids = tuple(effect.id for effect in rule.effects)
    if (
        not isinstance(native_trigger, Mapping)
        or set(native_trigger) != {"_id", "_phaseTypeList", "_phaseValueList"}
        or native_trigger.get("_id") != rule.trigger.id
        or None in expected_phase_types
        or native_trigger.get("_phaseTypeList") != list(expected_phase_types)
        or native_trigger.get("_phaseValueList")
        != list(rule.trigger.phase_values)
        or not isinstance(raw_effects, list)
        or len(raw_effects) != len(rule.effects)
        or native_effect_ids != master_effect_ids
        or not all(
            _native_effect_payload_matches_master(
                raw,
                effect.id,
                database=database,
            )
            for raw, effect in zip(raw_effects, rule.effects, strict=True)
        )
    ):
        _issue(
            issues,
            "trigger-listener-master-mismatch",
            f"root_runtime.references[{rid}]",
            (
                f"trigger={native_trigger!r}/{rule.trigger.id!r}; "
                f"effects={native_effect_ids!r}/{master_effect_ids!r}"
            ),
        )
        return None
    try:
        progress = restore_trigger_effect_serializer_progress(
            data,
            master_turn=-1,
            master_total_limit=(-1 if max_uses == 0 else max_uses),
            master_per_turn_limit=-1,
            phase_type_names=_NATIVE_PHASE_NAME_BY_VALUE,
            label=f"plan3-item-listener:{item_id}:{status_id}",
            lifecycle_policy=(
                TriggerEffectSerializerLifecyclePolicy.ITEM_ACTIVE_OR_EXHAUSTED
            ),
        )
    except TriggerEffectSerializerProgressError as error:
        _issue(
            issues,
            "trigger-listener-remaining-invalid",
            f"root_runtime.references[{rid}]",
            str(error),
        )
        return None
    uid = data.get("_uid")
    if (
        isinstance(uid, bool)
        or not isinstance(uid, int)
        or uid <= 0
    ):
        _issue(
            issues,
            "trigger-listener-lifecycle-unsupported",
            f"root_runtime.references[{rid}]",
            f"uid={uid!r}",
        )
        return None
    uses = 0 if max_uses == 0 else max_uses - progress.limit_count
    return ActivePlan3StatusEnchant(
        instance_id=f"localsave:{rid}:{status_id}",
        source_id=item_id,
        rule=rule,
        max_uses=max_uses,
        uses=uses,
        uses_this_turn=0,
        remaining_turns=progress.turn,
        passing_turn_start=progress.is_passing_turn_start,
        is_item_direct=True,
        turn_count=progress.turn_count,
        phase_counts=progress.phase_counts,
        native_uid=uid,
    )


def _map_plan3_status(
    opaque: Mapping[str, object],
    issues: list[Plan3LocalSaveIssue],
    *,
    database: Path,
    master_dir: Path,
    source_cards: Mapping[str, tuple[str, int]] | None = None,
) -> _MappedPlan3Runtime:
    raw = opaque.get("status")
    if not isinstance(raw, Mapping) or set(raw) != _STATUS_FIELDS:
        _issue(
            issues,
            "status-graph-unmapped",
            "root_runtime.status",
            "expected the v3.2.3 ExamStatusEffectCollection shape",
        )
        return _MappedPlan3Runtime()
    active = _links(
        raw.get("_effectList"),
        field="root_runtime.status._effectList",
        issues=issues,
    )
    removed = _links(
        raw.get("_removedEffectList"),
        field="root_runtime.status._removedEffectList",
        issues=issues,
    )
    create_count = raw.get("_effectCreateCount")
    if (
        not isinstance(create_count, int)
        or isinstance(create_count, bool)
        or create_count < 0
        or create_count >= 2**31 - 1
    ):
        _issue(
            issues,
            "status-graph-unmapped",
            "root_runtime.status",
            "effect lists/count have invalid shapes",
        )
        return _MappedPlan3Runtime()
    stance, stance_level = _map_idol_status(raw.get("_idolStatusEffect"), issues)
    # This is a monotonic native creation counter, not a live-link count.
    # JsonUtility may omit historical, no-longer-linked objects from RefIds.
    if create_count < len(active) + len(removed):
        _issue(
            issues,
            "status-history-inconsistent",
            "root_runtime.status._effectCreateCount",
            (
                f"count={create_count}; linked={len(active) + len(removed)}; "
                "creation count is below linked status count"
            ),
        )

    references = _reference_table(opaque, issues) if active else {}
    active_uid_owner: dict[int, int] = {}
    for rid in active:
        reference = references.get(rid)
        if reference is None:
            continue
        _class_name, data = reference
        uid = data.get("_uid")
        if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
            continue
        if uid > create_count:
            _issue(
                issues,
                "active-status-uid-out-of-range",
                f"root_runtime.references[{rid}]",
                f"uid={uid};effectCreateCount={create_count}",
            )
        previous_rid = active_uid_owner.get(uid)
        if previous_rid is not None:
            _issue(
                issues,
                "active-status-uid-duplicate",
                "root_runtime.status._effectList",
                f"uid={uid};rids={previous_rid},{rid}",
            )
        else:
            active_uid_owner[uid] = rid
    enthusiasm = 0
    enthusiastic_runtime: EnthusiasticRuntime | None = None
    enthusiasm_additive = 0
    enthusiasm_additive_statuses: list[
        Plan3EnthusiasmAdditiveStatus
    ] = []
    full_power_points: int | None = None
    playable_value_add_runtime: PlayableValueAddRuntime | None = None
    lesson_parameter_multiple_statuses: list[LessonParameterMultipleStatus] = []
    stamina_consumption_down_turns: int | None = None
    stamina_consumption_down_fresh = False
    stamina_consumption_down_fix_statuses: list[
        Plan3StaminaConsumptionDownFixStatus
    ] = []
    block_restriction_turns: int | None = None
    anti_debuff_runtime: AntiDebuffRuntime | None = None
    active_status_enchants: list[ActivePlan3StatusEnchant] = []
    mapped_listener_ids: list[str] = []
    raw_triggered = opaque.get("currentTurnTriggeredStatusEnchantIdList")
    triggered_ids = frozenset(
        value
        for value in raw_triggered
        if isinstance(value, str) and value
    ) if isinstance(raw_triggered, list) else frozenset()
    for rid in active:
        reference = references.get(rid)
        if reference is None:
            _issue(
                issues,
                "active-status-reference-missing",
                f"root_runtime.status._effectList[{rid}]",
            )
            continue
        class_name, data = reference
        if class_name == _ENTHUSIASM_CLASS:
            restored_enthusiastic = _enthusiastic_status(
                data,
                rid=rid,
                create_count=create_count,
                issues=issues,
            )
            if restored_enthusiastic is not None:
                if enthusiastic_runtime is not None:
                    _issue(
                        issues,
                        "enthusiastic-status-multiple",
                        "root_runtime.status._effectList",
                    )
                else:
                    enthusiastic_runtime = restored_enthusiastic
                    enthusiasm = restored_enthusiastic.value
        elif class_name == _ENTHUSIASM_ADDITIVE_CLASS:
            restored_additive = _enthusiasm_additive_unlimited_status(
                data,
                rid=rid,
                create_count=create_count,
                issues=issues,
            )
            if restored_additive is not None:
                if enthusiasm_additive_statuses:
                    _issue(
                        issues,
                        "enthusiasm-additive-unlimited-status-multiple",
                        "root_runtime.status._effectList",
                    )
                else:
                    enthusiasm_additive_statuses.append(restored_additive)
                    enthusiasm_additive = restored_additive.value
        elif class_name == _FULL_POWER_POINT_CLASS:
            value = _scalar_status_value(
                data,
                rid=rid,
                class_name=class_name,
                issues=issues,
            )
            if value is not None:
                if full_power_points is not None:
                    _issue(
                        issues,
                        "full-power-point-status-multiple",
                        "root_runtime.status._effectList",
                    )
                else:
                    full_power_points = value
        elif class_name == _ANTI_DEBUFF_CLASS:
            restored_anti_debuff = _anti_debuff_status(
                data,
                rid=rid,
                create_count=create_count,
                issues=issues,
            )
            if restored_anti_debuff is not None:
                if anti_debuff_runtime is not None:
                    _issue(
                        issues,
                        "anti-debuff-status-multiple",
                        "root_runtime.status._effectList",
                    )
                else:
                    anti_debuff_runtime = restored_anti_debuff
        elif class_name == _TRIGGER_LISTENER_CLASS:
            restored = _restore_trigger_listener(
                data,
                rid=rid,
                database=database,
                master_dir=master_dir,
                issues=issues,
                triggered_ids=triggered_ids,
                source_cards=source_cards,
            )
            if restored is not None:
                active_status_enchants.append(restored)
                mapped_listener_ids.append(restored.rule.id)
        elif class_name == _PLAYABLE_VALUE_ADD_CLASS:
            restored_playable = _playable_value_add_status(
                data,
                rid=rid,
                create_count=create_count,
                issues=issues,
            )
            if restored_playable is not None:
                if playable_value_add_runtime is not None:
                    _issue(
                        issues,
                        "playable-value-add-status-multiple",
                        "root_runtime.status._effectList",
                    )
                else:
                    playable_value_add_runtime = restored_playable
        elif class_name == _LESSON_PARAMETER_MULTIPLE_CLASS:
            restored = _lesson_parameter_multiple_status(
                data,
                rid=rid,
                issues=issues,
            )
            if restored is not None:
                lesson_parameter_multiple_statuses.append(restored)
        elif class_name == _STAMINA_CONSUMPTION_DOWN_CLASS:
            restored_stamina = _stamina_consumption_down_status(
                data,
                rid=rid,
                issues=issues,
            )
            if restored_stamina is not None:
                turns, passing = restored_stamina
                if stamina_consumption_down_turns is not None:
                    _issue(
                        issues,
                        "stamina-consumption-down-status-multiple",
                        "root_runtime.status._effectList",
                    )
                else:
                    stamina_consumption_down_turns = turns
                    stamina_consumption_down_fresh = not passing
        elif class_name == _STAMINA_CONSUMPTION_DOWN_FIX_CLASS:
            restored_fixed = _stamina_consumption_down_fix_status(
                data, rid=rid, create_count=create_count, issues=issues,
            )
            if restored_fixed is not None:
                stamina_consumption_down_fix_statuses.append(restored_fixed)
        elif class_name == _BLOCK_RESTRICTION_CLASS:
            turns = _turn_limited_status_value(
                data,
                rid=rid,
                class_name=class_name,
                issues=issues,
                requires_value=False,
            )
            if turns is not None:
                if turns != 1:
                    _issue(
                        issues,
                        "active-status-lifecycle-unsupported",
                        f"root_runtime.references[{rid}]",
                        f"{class_name}:turns={turns!r}",
                    )
                elif block_restriction_turns is not None:
                    _issue(
                        issues,
                        "block-restriction-status-multiple",
                        "root_runtime.status._effectList",
                    )
                else:
                    block_restriction_turns = turns
        else:
            _issue(
                issues,
                "active-status-class-unsupported",
                f"root_runtime.references[{rid}]",
                class_name,
            )
    return _MappedPlan3Runtime(
        stance=stance,
        stance_level=stance_level,
        enthusiasm=enthusiasm,
        enthusiastic_runtime=(
            enthusiastic_runtime or EnthusiasticRuntime()
        ),
        enthusiasm_additive=enthusiasm_additive,
        enthusiasm_additive_statuses=tuple(
            enthusiasm_additive_statuses
        ),
        full_power_points=full_power_points or 0,
        playable_value_add_runtime=(
            playable_value_add_runtime or PlayableValueAddRuntime()
        ),
        lesson_parameter_multiple_state=LessonParameterMultipleState(
            tuple(lesson_parameter_multiple_statuses)
        ),
        stamina_consumption_down_turns=(stamina_consumption_down_turns or 0),
        stamina_consumption_down_fresh=stamina_consumption_down_fresh,
        stamina_consumption_down_fix_statuses=tuple(
            stamina_consumption_down_fix_statuses
        ),
        block_restriction_turns=(block_restriction_turns or 0),
        anti_debuff_runtime=(anti_debuff_runtime or AntiDebuffRuntime()),
        active_status_enchants=tuple(active_status_enchants),
        mapped_listener_ids=tuple(mapped_listener_ids),
        next_status_uid=create_count + 1,
    )


def _card_ref(card: LocalSaveExamCard) -> Plan3CardRef:
    return Plan3CardRef(card.card_id, card.effective_upgrade)


def project_plan3_local_save(
    exam: LocalSaveExamState,
    *,
    database: Path = DEFAULT_DATABASE,
    master_dir: Path = DEFAULT_MASTER_DIR,
    native_card_play_counts: bool = False,
) -> Plan3LocalSaveProjection:
    """Project persisted state, failing closed on unimplemented runtime meaning.

    The returned ``state`` always exists so an analysis-only heuristic can be
    produced.  ``projection.exact`` is the gate for treating that simulation
    as a faithful one-step transition.  ``native_card_play_counts`` may only
    be enabled by a GUID-native caller that separately retains and advances
    every card instance's persisted ``_playCount``.
    """

    if not isinstance(exam, LocalSaveExamState):
        raise TypeError("exam must be LocalSaveExamState")
    if not isinstance(native_card_play_counts, bool):
        raise TypeError("native_card_play_counts must be bool")

    issues: list[Plan3LocalSaveIssue] = []
    opaque = _runtime_opaque(exam, issues)
    _audit_settled_boundary(exam, issues)
    _audit_card_runtime(
        exam,
        issues,
        database=database,
        native_card_play_counts=native_card_play_counts,
    )
    _audit_plan3_identity(opaque, issues)
    source_cards = {
        card.guid: (card.card_id, card.effective_upgrade)
        for card in exam.zones.all_cards
    }
    mapped_runtime = _map_plan3_status(
        opaque,
        issues,
        database=database,
        master_dir=master_dir,
        source_cards=source_cards,
    )
    _audit_turn_runtime(opaque, mapped_runtime.mapped_listener_ids, issues)

    hand = tuple(_card_ref(card) for card in exam.zones.hand)
    draw = tuple(_card_ref(card) for card in exam.zones.deck)
    discard = tuple(_card_ref(card) for card in exam.zones.grave)
    lost = tuple(_card_ref(card) for card in exam.zones.lost)
    hold = tuple(_card_ref(card) for card in exam.zones.hold)

    if exam.exam_type == _LESSON_EXAM_TYPE:
        current_parameter_type = _LESSON_PARAMETER_TYPE_BY_STEP_TYPE.get(
            exam.step_type_value, 0
        )
        if current_parameter_type == 0:
            _issue(
                issues,
                "lesson-parameter-type-unmapped",
                "step_type_value",
                f"unsupported ProduceStepType value {exam.step_type_value}",
            )
    elif exam.current_turn <= len(exam.turn_parameter_types):
        current_parameter_type = exam.turn_parameter_types[exam.current_turn - 1]
    else:
        current_parameter_type = 0
        _issue(
            issues,
            "extra-turn-parameter-type-unmapped",
            "turn_parameter_types",
            f"current_turn={exam.current_turn}; schedule={len(exam.turn_parameter_types)}",
        )
    lesson_type = (
        _LESSON_TYPE_BY_STEP_TYPE.get(exam.step_type_value, LESSON_UNKNOWN)
        if exam.exam_type == _LESSON_EXAM_TYPE
        else LESSON_UNKNOWN
    )
    step_type_value = (
        exam.step_type_value
        if exam.exam_type == _LESSON_EXAM_TYPE
        and exam.step_type_value in _LESSON_TYPE_BY_STEP_TYPE
        else 0
    )
    plays_remaining = (
        (0 if exam.is_turn_card_play_end else 1)
        + mapped_runtime.playable_value_add_runtime.value
    )
    from .plan3_play_history import project_play_history
    try:
        # PlayingCard is a typed LocalSaveExamState field, deliberately absent
        # from its opaque sidecar. Use that same snapshot's parsed presence.
        play_history = project_play_history(opaque, database=database, playing_card_present=exam.playing_card is not None)
    except (KeyError, OSError, TypeError, ValueError) as error:
        play_history = None
        # Old hand-built scalar fixtures have no native history carrier. Only
        # its actual presence commits this projection to the new field shape.
        if "playLogList" in opaque and opaque.get("playLogList") not in (None, []):
            _issue(issues, "play-history-unavailable", "root_runtime.playLogList", str(error))
    state = Plan3State(
        turns_remaining=exam.remain_turn,
        stamina=exam.stamina,
        max_stamina=exam.max_stamina,
        play_history=play_history,
        extra_turn=exam.extra_turn,
        score=exam.score,
        block=exam.block,
        round_number=exam.current_turn,
        plays_remaining=plays_remaining,
        awaiting_turn_start=exam.is_turn_card_play_end,
        stance=mapped_runtime.stance,
        stance_level=mapped_runtime.stance_level,
        stance_change_count=_integer_field(
            opaque, "stanceChangeCount", issues, minimum=0
        ),
        concentration_change_count=_integer_field(
            opaque, "stanceConcentrationChangeCount", issues, minimum=0
        ),
        preservation_change_count=_integer_field(
            opaque, "stancePreservationChangeCount", issues, minimum=0
        ),
        full_power_change_count=_integer_field(
            opaque, "stanceFullPowerChangeCount", issues, minimum=0
        ),
        enthusiasm=mapped_runtime.enthusiasm,
        enthusiastic_runtime=mapped_runtime.enthusiastic_runtime,
        enthusiasm_additive=mapped_runtime.enthusiasm_additive,
        enthusiasm_additive_statuses=(
            mapped_runtime.enthusiasm_additive_statuses
        ),
        enthusiasm_gain_bonus_percent=0,
        parameter_bonus_percent=0,
        slump=False,
        full_power_points=mapped_runtime.full_power_points,
        lesson_parameter_multiple_state=(
            mapped_runtime.lesson_parameter_multiple_state
        ),
        lesson_type=lesson_type,
        step_type_value=step_type_value,
        stamina_consumption_down_turns=(
            mapped_runtime.stamina_consumption_down_turns
        ),
        # Preserve the active status' native passing-turn-start bit.  A false
        # bit means the status was installed after this turn's boundary and
        # must survive the next end-turn without an extra decrement.
        stamina_consumption_down_fresh=mapped_runtime.stamina_consumption_down_fresh,
        stamina_consumption_down_fix_statuses=(
            mapped_runtime.stamina_consumption_down_fix_statuses
        ),
        block_restriction_turns=mapped_runtime.block_restriction_turns,
        anti_debuff_runtime=mapped_runtime.anti_debuff_runtime,
        playable_value_add_runtime=(
            mapped_runtime.playable_value_add_runtime
        ),
        active_status_enchants=mapped_runtime.active_status_enchants,
        next_status_uid=mapped_runtime.next_status_uid,
        limit_border=_integer_field(opaque, "limitBorder", issues, fallback=-1),
        clear_border=_integer_field(opaque, "clearBorder", issues, fallback=-1),
        current_turn_total_add_parameter=_integer_field(
            opaque, "currentTurnTotalAddParameter", issues, minimum=0
        ),
        # Native examType 1 uses battle bonus scaling and maintains both the
        # overall judge parameter and its three attribute subtotals.
        is_battle=exam.exam_type == _AUDITION_EXAM_TYPE,
        current_parameter_type=current_parameter_type,
        battle_bonus_permille_vocal=exam.vocal_bonus_permille,
        battle_bonus_permille_dance=exam.dance_bonus_permille,
        battle_bonus_permille_visual=exam.visual_bonus_permille,
        judge_parameter_vocal=_integer_field(
            opaque, "parameterVocal", issues, minimum=0
        ),
        judge_parameter_dance=_integer_field(
            opaque, "parameterDance", issues, minimum=0
        ),
        judge_parameter_visual=_integer_field(
            opaque, "parameterVisual", issues, minimum=0
        ),
        full_power_points_total=_integer_field(
            opaque, "fullPowerPointGetSumCount", issues, minimum=0
        ),
        hand=hand,
        draw_pile=draw,
        discard_pile=discard,
        lost_pile=lost,
        hold_pile=hold,
    )
    return Plan3LocalSaveProjection(
        state,
        hand,
        tuple(issues),
    )


def _ranked_cards(advice: Plan3HandAdvice) -> tuple[Plan3LocalSaveRankedCard, ...]:
    eligible = sorted(
        (candidate for candidate in advice.candidates if candidate.score is not None),
        key=lambda candidate: (-int(candidate.score), candidate.slot_index),
    )
    ranks: dict[int, int] = {}
    previous_score: int | None = None
    previous_rank = 0
    for position, candidate in enumerate(eligible, start=1):
        if candidate.score != previous_score:
            previous_rank = position
            previous_score = candidate.score
        ranks[candidate.slot_index] = previous_rank
    return tuple(
        Plan3LocalSaveRankedCard(
            slot_index=candidate.slot_index,
            card_ref=candidate.card_ref,
            card_name=candidate.card_name,
            rank=ranks.get(candidate.slot_index),
            score=candidate.score,
            explanation=candidate.explanation,
            blockers=candidate.blockers,
        )
        for candidate in advice.candidates
    )


def rank_plan3_exam_state(
    exam: LocalSaveExamState,
    *,
    save_data_version: int = 0,
    allow_heuristic_fallback: bool = True,
    database: Path = DEFAULT_DATABASE,
    settings: Plan3ExamSettings | None = None,
) -> Plan3LocalSaveRanking:
    """Rank the current hand without exposing or performing an input action."""

    projection = project_plan3_local_save(exam, database=database)
    if settings is None:
        try:
            settings = load_plan3_exam_settings(exam.setting_id)
        except (KeyError, TypeError, ValueError, OSError) as error:
            projection = replace(
                projection,
                issues=(
                    *projection.issues,
                    Plan3LocalSaveIssue(
                        "exam-settings-unavailable",
                        "setting_id",
                        f"{type(error).__name__}:{exam.setting_id}",
                    ),
                ),
            )
            return Plan3LocalSaveRanking(
                save_data_version,
                projection,
                RANKING_UNSUPPORTED,
                (),
                None,
            )
    elif settings.id != exam.setting_id:
        projection = replace(
            projection,
            issues=(
                *projection.issues,
                Plan3LocalSaveIssue(
                    "exam-settings-mismatch",
                    "setting_id",
                    f"save={exam.setting_id}; settings={settings.id}",
                ),
            ),
        )
        return Plan3LocalSaveRanking(
            save_data_version,
            projection,
            RANKING_UNSUPPORTED,
            (),
            None,
        )
    if projection.issues and not allow_heuristic_fallback:
        return Plan3LocalSaveRanking(
            save_data_version,
            projection,
            RANKING_UNSUPPORTED,
            (),
            None,
        )

    try:
        advice = advise_plan3_visible_hand(
            projection.state,
            projection.visible_hand,
            total_turns=exam.limit_turn + exam.extra_turn,
            database=database,
            settings=settings,
        )
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error):
        # State construction is strict, but Master/settings availability is an
        # external offline dependency.  Keep that failure fail-closed.
        return Plan3LocalSaveRanking(
            save_data_version,
            projection,
            RANKING_UNSUPPORTED,
            (),
            None,
        )

    cards = _ranked_cards(advice)
    scored = [card for card in cards if card.score is not None]
    if not scored:
        mode = RANKING_UNSUPPORTED
        recommended = None
    else:
        mode = (
            RANKING_EXACT if projection.exact else RANKING_HEURISTIC_FALLBACK
        )
        recommended = min(
            scored,
            key=lambda card: (-int(card.score), card.slot_index),
        ).slot_index
    return Plan3LocalSaveRanking(
        save_data_version,
        projection,
        mode,
        cards,
        recommended,
    )


def rank_plan3_local_save_bytes(
    data: bytes,
    *,
    allow_heuristic_fallback: bool = True,
    database: Path = DEFAULT_DATABASE,
    settings: Plan3ExamSettings | None = None,
) -> Plan3LocalSaveRanking:
    """Decrypt supplied bytes and produce read-only current-hand advice."""

    decoded = decode_plan3_local_save_bytes(data)
    return rank_plan3_exam_state(
        decoded.exam_state,
        save_data_version=decoded.envelope.save_data_version,
        allow_heuristic_fallback=allow_heuristic_fallback,
        database=database,
        settings=settings,
    )


def rank_plan3_local_save_file(
    path: str | Path,
    *,
    allow_heuristic_fallback: bool = True,
    database: Path = DEFAULT_DATABASE,
    settings: Plan3ExamSettings | None = None,
) -> Plan3LocalSaveRanking:
    """Read one save path and produce advice without writing or controlling it."""

    decoded = decode_plan3_local_save_file(path)
    return rank_plan3_exam_state(
        decoded.exam_state,
        save_data_version=decoded.envelope.save_data_version,
        allow_heuristic_fallback=allow_heuristic_fallback,
        database=database,
        settings=settings,
    )


__all__ = [
    "DecodedPlan3LocalSave",
    "Plan3LocalSaveIssue",
    "Plan3LocalSaveProjection",
    "Plan3LocalSaveRankedCard",
    "Plan3LocalSaveRanking",
    "RANKING_EXACT",
    "RANKING_HEURISTIC_FALLBACK",
    "RANKING_UNSUPPORTED",
    "decode_plan3_local_save_bytes",
    "decode_plan3_local_save_file",
    "project_plan3_local_save",
    "rank_plan3_exam_state",
    "rank_plan3_local_save_bytes",
    "rank_plan3_local_save_file",
]
