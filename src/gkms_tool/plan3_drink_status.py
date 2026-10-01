"""Restore actual drink-origin listeners through Master and native progress.

The native serializer is shared across Plans. This reader checks the source
drink's ordered effect graph rather than accepting a drink/status ID alone.
"""
from __future__ import annotations
from collections.abc import Mapping
from pathlib import Path

from .drink_catalog import load_drink_catalog
from .master_db import DEFAULT_DATABASE
from .plan3_engine import ActivePlan3StatusEnchant, load_plan3_effect, validate_plan3_status_enchant
from .trigger_effect_serializer_progress import (
    restore_trigger_effect_serializer_progress, is_identity_empty_trigger_item_scratch,
)

STATUS_FIELDS=frozenset({
    "_descriptionReactiveDataTextList","_descriptionReactiveDataTypeList","_effectList",
    "_fromExamEffectIdList","_isEncoreEnchant","_isFromEnchantEffect","_isItemDirectEnchant",
    "_isPassingTurnStart","_isTurnLimited","_limitCount","_limitCountInTurn",
    "_limitCountInTurnRemain","_originType","_overrideType","_phaseCountDictionary",
    "_startEnchantOriginId","_startEnchantOriginLevel","_startEnchantOriginType",
    "_startEnchantOwnerId","_statusEnchantId","_trigger","_triggerCard","_triggerDrink",
    "_triggerGimmickGroup","_triggerItem","_turn","_turnCount","_uid",
})


def restore_plan3_drink_listener(data: Mapping, *, rid: int, phase_values: Mapping,
                                 effect_matches, database: Path=DEFAULT_DATABASE):
    if not isinstance(data,Mapping) or set(data)!=STATUS_FIELDS:
        raise ValueError("drink-listener-fields-invalid")
    drink=data["_triggerDrink"]
    if not isinstance(drink,Mapping) or set(drink)!={"_id"} or not isinstance(drink["_id"],str) or not drink["_id"]:
        raise ValueError("drink-listener-source-invalid")
    drink_id=drink["_id"]
    item=load_drink_catalog(Path(database)).get_drink(drink_id)
    if item.plan_type not in {"ProducePlanType_Common","ProducePlanType_Plan3"}:
        raise ValueError("drink-listener-plan-mismatch")
    matches=[ref for ref in item.effect_refs if ref.effect.source_kind=="exam"
        and ref.effect.effect_type=="ProduceExamEffectType_ExamStatusEnchant"
        and ref.effect.produce_exam_status_enchant_id==data["_statusEnchantId"]]
    if len(matches)!=1:
        raise ValueError("drink-listener-Master-source-not-unique")
    installer=load_plan3_effect(matches[0].effect.source_id,Path(database))
    rule=installer.status_enchant
    if rule is None or validate_plan3_status_enchant(rule):
        raise ValueError("drink-listener-Master-program-unsupported")
    if (installer.value2!=0 or installer.effect_count<0 or installer.value1<0
            or installer.effect_turn==0 or installer.effect_turn < -1):
        raise ValueError("drink-listener-installer-shape-invalid")
    card=data["_triggerCard"];gimmick=data["_triggerGimmickGroup"]
    if (not isinstance(card,Mapping) or card.get("_guid")!=""
            or not isinstance(card.get("_cardData"),Mapping) or card["_cardData"].get("_id")!=""
            or not isinstance(gimmick,Mapping) or gimmick.get("_id")!=""
            or not is_identity_empty_trigger_item_scratch(data["_triggerItem"])
            or data["_isItemDirectEnchant"] is not False
            or data["_isFromEnchantEffect"] is not False or data["_isEncoreEnchant"] is not False
            or type(data["_originType"]) is not int or data["_originType"]!=1
            or type(data["_overrideType"]) is not int or data["_overrideType"]!=31):
        raise ValueError("drink-listener-origin-mismatch")
    for key,expected in {"_startEnchantOriginId":"","_startEnchantOwnerId":"",
            "_startEnchantOriginLevel":0,"_startEnchantOriginType":0,
            "_fromExamEffectIdList":[],"_descriptionReactiveDataTextList":[],
            "_descriptionReactiveDataTypeList":[]}.items():
        if type(data[key]) is not type(expected) or data[key]!=expected:
            raise ValueError("drink-listener-origin-mismatch:"+key)
    phases=[phase_values.get(phase) for phase in rule.trigger.phase_types]
    expected_trigger={"_id":rule.trigger.id,"_phaseTypeList":phases,"_phaseValueList":list(rule.trigger.phase_values)}
    if None in phases or data["_trigger"]!=expected_trigger:
        raise ValueError("drink-listener-trigger-Master-mismatch")
    effects=data["_effectList"]
    if not isinstance(effects,list) or len(effects)!=len(rule.effects):
        raise ValueError("drink-listener-effect-order-mismatch")
    for raw,child in zip(effects,rule.effects,strict=True):
        if not effect_matches(raw,child.id,database=Path(database)):
            raise ValueError("drink-listener-child-Master-mismatch:"+child.id)
    progress=restore_trigger_effect_serializer_progress(data,master_turn=installer.effect_turn,
        master_total_limit=installer.effect_count or -1,master_per_turn_limit=installer.value1 or -1,
        phase_type_names={value:phase for phase,value in zip(rule.trigger.phase_types,phases,strict=True)},
        label=f"drink-listener:{drink_id}:{rule.id}")
    uid=data["_uid"]
    if type(uid) is not int or uid<=0:raise ValueError("drink-listener-uid-invalid")
    return ActivePlan3StatusEnchant(instance_id=f"localsave:{rid}:{rule.id}",source_id=drink_id,rule=rule,
        max_uses=installer.effect_count,uses=0 if not installer.effect_count else installer.effect_count-progress.limit_count,
        max_uses_per_turn=installer.value1,
        uses_this_turn=0 if not installer.value1 else installer.value1-progress.limit_count_in_turn_remaining,
        remaining_turns=progress.turn,passing_turn_start=progress.is_passing_turn_start,is_item_direct=False,
        turn_count=progress.turn_count,phase_counts=progress.phase_counts,native_uid=uid)
