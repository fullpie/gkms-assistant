"""Typed Plan3 definition changes before ordinary numeric grow application.

Android 3.2.3 GrowEffectAffectProduceCardEffectList first visits pre-affect
types (EffectAdd=32/Delete=33/Change=34), creating and appending EffectAdd's
effect object, then visits ordinary numeric grows. InitialAdd=39 is an
IsInitial marker: base flag OR presence, with no Playing-time effect.
"""
from dataclasses import replace

EFFECT_ADD='ProduceCardGrowEffectType_EffectAdd'
INITIAL_ADD='ProduceCardGrowEffectType_InitialAdd'


def has_initial_customization(customization)->bool:
    return any(entry.grow_effect_type==INITIAL_ADD for entry in customization.effects)


def append_customization_effects(card,customization):
    """Preserve slot/list order; do not flatten the child or overwrite base effects."""
    children=[]
    for entry in customization.effects:
        if entry.grow_effect_type!=EFFECT_ADD:continue
        if entry.added_effect is None:
            raise ValueError('Plan3 EffectAdd lacks its version-bound child effect:'+entry.grow_effect_id)
        children.append(entry.added_effect)
    return replace(card,effects=(*card.effects,*children)) if children else card
