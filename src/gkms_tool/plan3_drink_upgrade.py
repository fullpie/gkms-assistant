"""The proved Hand/All ExamCardUpgrade drink shape and shared kernel binding."""
from dataclasses import dataclass
from pathlib import Path

from .master_db import DEFAULT_DATABASE

EFFECT_TYPE = "ProduceExamEffectType_ExamCardUpgrade"
HAND_ALL = "e_effect-exam_card_upgrade-p_card_search-hand-all-0_0"


def matches_catalog_effect(effect) -> bool:
    expected = {
        "source_kind": "exam", "source_id": HAND_ALL, "effect_type": EFFECT_TYPE,
        "produce_card_search_id": "p_card_search-hand",
        "move_position_type": "ProduceCardMovePositionType_Unknown",
        "pick_range_type": "ProducePickRangeType_All",
        "pick_range_type2": "ProducePickRangeType_Unknown",
        "pick_count_type": "ProducePickCountType_Unknown",
        "pick_count_type2": "ProducePickCountType_Unknown",
        "target_exam_effect_type": "ProduceExamEffectType_Unknown",
        "effect_group_ids": ("effect_group-visible-exam_card_upgrade-000",),
        "is_research": False,
    }
    for key in ("effect_value1", "effect_value2", "effect_value_min", "effect_value_max",
                "effect_count", "effect_turn", "pick_count_min", "pick_count_max",
                "pick_count_min2", "pick_count_max2", "target_upgrade_count"):
        expected[key] = 0
    for key in ("produce_card_search_id2", "produce_card_status_enchant_id", "produce_exam_status_enchant_id",
                "produce_exam_trigger_id", "chain_produce_exam_effect_id", "target_produce_card_id",
                "pick_count_reference_produce_card_search_id", "pick_count_reference_produce_card_search_id2",
                "produce_resource_type", "produce_step_event_detail_id"):
        expected[key] = ""
    for key in ("produce_exam_trigger_effect_ids", "chain_produce_exam_effect_ids",
                "produce_card_grow_effect_ids", "produce_rewards"):
        expected[key] = ()
    return all(getattr(effect, key, None) == value for key, value in expected.items())


@dataclass(frozen=True, slots=True)
class Plan3DrinkCardUpgradeEffect:
    id: str
    database: Path = DEFAULT_DATABASE

    def __post_init__(self):
        if self.id != HAND_ALL:
            raise ValueError("only proven Hand-All drink upgrade is supported")
        object.__setattr__(self, "database", Path(self.database))

    @property
    def effect_type(self):
        return EFFECT_TYPE

    def resolve_contract(self):
        from .plan3_card_upgrade import Plan3CardUpgradeBranch, resolve_plan3_card_upgrade

        resolved = resolve_plan3_card_upgrade(self.id, database=self.database, source_kind="drink")
        if not resolved.resolved or resolved.contract is None:
            raise ValueError("drink Hand-All upgrade contract unavailable:" + str(resolved.pause))
        contract = resolved.contract
        if contract.branch != Plan3CardUpgradeBranch.ALL or contract.search_id != "p_card_search-hand":
            raise ValueError("drink upgrade Master contract changed")
        return contract
