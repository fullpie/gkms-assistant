"""Source-neutral effect predicates from the original native getter bodies.

Arguments use the full ProduceExamEffectType name, deliberately not an
untyped integer which could belong to a different enum.
"""

BLOCK_ICON_VALUE_EFFECT_TYPES=frozenset({
    'ProduceExamEffectType_ExamBlock',
    'ProduceExamEffectType_ExamBlockPerUseCardCount',
    'ProduceExamEffectType_ExamBlockAddMultipleAggressive',
    'ProduceExamEffectType_ExamBlockPerSearchCount',
})


def is_block_icon_value_effect_type(effect_type:object)->bool:
    """Android 3.2.3 IsBlockIconValueEffectType: effect values 3/133/143/161."""
    return isinstance(effect_type,str) and effect_type in BLOCK_ICON_VALUE_EFFECT_TYPES
