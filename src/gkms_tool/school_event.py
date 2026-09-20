"""Resolve visible school-event choices from the imported local Master data.

The screen is used only to identify the ADV story and the displayed stamina
costs. Exact parameter/card/reward consequences come from Master, so a new
character card does not require replaying every possible school event by hand.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from .master_db import DEFAULT_DATABASE


_STATUS_EFFECTS: Mapping[str, str] = {
    "ProduceEffectType_VocalAddition": "vocal",
    "ProduceEffectType_DanceAddition": "dance",
    "ProduceEffectType_VisualAddition": "visual",
}
_REWARD_SET = "ProduceEffectType_ProduceRewardSet"
_CARD_RESOURCE = "ProduceResourceType_ProduceCard"
_DRINK_RESOURCE = "ProduceResourceType_ProduceDrink"


@dataclass(frozen=True, slots=True)
class RewardSelection:
    """One exact Master-backed reward selection opened by an event choice."""

    effect_id: str
    resource_type: str
    pick_range_type: str
    pick_count_min: int
    pick_count_max: int

    def __post_init__(self) -> None:
        if not self.effect_id or not self.resource_type or not self.pick_range_type:
            raise ValueError("reward selection identity is incomplete")
        if self.pick_count_min < 0 or self.pick_count_max < self.pick_count_min:
            raise ValueError("reward selection count range is invalid")


@dataclass(frozen=True, slots=True)
class SchoolEventChoice:
    slot: int
    suggestion_id: str
    stamina_cost: int
    produce_point_cost: int
    direct_card_id: str
    direct_card_upgrade: int
    status_delta_min: Mapping[str, int]
    status_delta_max: Mapping[str, int]
    effect_ids: tuple[str, ...]
    reward_selections: tuple[RewardSelection, ...]
    step_type: str
    step_id: str
    success_probability_permyriad: int
    success_effect_ids: tuple[str, ...]
    success_step_type: str
    success_step_id: str
    fail_effect_ids: tuple[str, ...]
    fail_step_type: str
    fail_step_id: str
    always_successful: bool
    produce_effect_fire_step: int

    def __post_init__(self) -> None:
        if self.stamina_cost < 0 or self.produce_point_cost < 0:
            raise ValueError("event direct costs cannot be negative")
        if not 0 <= self.success_probability_permyriad <= 10_000:
            raise ValueError("success probability must be in 0..10000 permyriad")
        if not isinstance(self.always_successful, bool):
            raise TypeError("always_successful must be boolean")
        if self.produce_effect_fire_step < 0:
            raise ValueError("produce_effect_fire_step cannot be negative")

    @property
    def produce_point_delta(self) -> int:
        """Compatibility projection; Master ``producePoint`` is a cost."""

        return -self.produce_point_cost

    @property
    def grants_card_selection(self) -> bool:
        return any(
            selection.resource_type == _CARD_RESOURCE
            and selection.pick_count_min >= 1
            for selection in self.reward_selections
        )

    @property
    def grants_drink_selection(self) -> bool:
        return any(
            selection.resource_type == _DRINK_RESOURCE
            and selection.pick_count_min >= 1
            for selection in self.reward_selections
        )

    @property
    def fixed_status_delta(self) -> dict[str, int]:
        if dict(self.status_delta_min) != dict(self.status_delta_max):
            raise ValueError("school-event parameter outcome is not fixed")
        return dict(self.status_delta_min)

    def outcome_signature(self) -> tuple[object, ...]:
        return (
            self.stamina_cost,
            self.produce_point_cost,
            self.direct_card_id,
            self.direct_card_upgrade,
            tuple(sorted(self.status_delta_min.items())),
            tuple(sorted(self.status_delta_max.items())),
            tuple(
                (
                    selection.effect_id,
                    selection.resource_type,
                    selection.pick_range_type,
                    selection.pick_count_min,
                    selection.pick_count_max,
                )
                for selection in self.reward_selections
            ),
            self.step_type,
            self.step_id,
            self.success_probability_permyriad,
            self.success_effect_ids,
            self.success_step_type,
            self.success_step_id,
            self.fail_effect_ids,
            self.fail_step_type,
            self.fail_step_id,
            self.always_successful,
            self.produce_effect_fire_step,
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SchoolEventResolution:
    adv_asset_id: str
    story_id: str
    title: str
    matching_detail_ids: tuple[str, ...]
    choices: tuple[SchoolEventChoice, ...]
    detail_effect_ids: tuple[str, ...] = ()
    suggestion_type: str = ""
    support_card_id: str = ""
    event_type: str = ""
    event_character_type: str = ""
    is_business_excellent: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "adv_asset_id": self.adv_asset_id,
            "story_id": self.story_id,
            "title": self.title,
            "matching_detail_ids": list(self.matching_detail_ids),
            "choices": [choice.to_dict() for choice in self.choices],
            "detail_effect_ids": list(self.detail_effect_ids),
            "suggestion_type": self.suggestion_type,
            "support_card_id": self.support_card_id,
            "event_type": self.event_type,
            "event_character_type": self.event_character_type,
            "is_business_excellent": self.is_business_excellent,
        }


@dataclass(frozen=True, slots=True)
class _SchoolEventDetailCandidate:
    detail_id: str
    suggestion_type: str
    detail_effect_ids: tuple[str, ...]
    support_card_id: str
    event_type: str
    event_character_type: str
    is_business_excellent: bool
    choices: tuple[SchoolEventChoice, ...]

    def semantic_signature(self) -> tuple[object, ...]:
        return (
            self.suggestion_type,
            self.detail_effect_ids,
            self.support_card_id,
            self.event_type,
            self.event_character_type,
            self.is_business_excellent,
            tuple(choice.outcome_signature() for choice in self.choices),
        )


def _json_string_list(raw: str, *, field: str) -> tuple[str, ...]:
    value = json.loads(raw)
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(f"{field} must be a list of non-empty strings")
    return tuple(value)


def _load_choice(
    connection: sqlite3.Connection,
    *,
    slot: int,
    suggestion_id: str,
) -> SchoolEventChoice:
    suggestion = connection.execute(
        """
        SELECT stamina, produce_point, produce_card_id,
               produce_card_upgrade_count, produce_effect_ids_json,
               step_type, step_id, success_probability_permyriad,
               success_produce_effect_ids_json, success_step_type,
               success_step_id, fail_produce_effect_ids_json,
               fail_step_type, fail_step_id, always_successful,
               produce_effect_fire_step
          FROM step_event_suggestion
         WHERE id = ?
        """,
        (suggestion_id,),
    ).fetchone()
    if suggestion is None:
        raise KeyError(f"school-event suggestion not found: {suggestion_id}")

    effect_ids = _json_string_list(
        str(suggestion[4]), field="produce_effect_ids_json"
    )
    success_effect_ids = _json_string_list(
        str(suggestion[8]), field="success_produce_effect_ids_json"
    )
    fail_effect_ids = _json_string_list(
        str(suggestion[11]), field="fail_produce_effect_ids_json"
    )
    status_min: dict[str, int] = {}
    status_max: dict[str, int] = {}
    reward_selections: list[RewardSelection] = []
    for effect_id in effect_ids:
        effect = connection.execute(
            """
            SELECT effect_type, effect_value_min, effect_value_max,
                   resource_type, pick_range_type, pick_count_min,
                   pick_count_max
              FROM produce_effect
             WHERE id = ?
            """,
            (effect_id,),
        ).fetchone()
        if effect is None:
            raise KeyError(f"school-event produce effect not found: {effect_id}")
        attribute = _STATUS_EFFECTS.get(str(effect[0]))
        if attribute is not None:
            status_min[attribute] = status_min.get(attribute, 0) + int(effect[1])
            status_max[attribute] = status_max.get(attribute, 0) + int(effect[2])
        if effect[0] == _REWARD_SET:
            reward_selections.append(
                RewardSelection(
                    effect_id=effect_id,
                    resource_type=str(effect[3]),
                    pick_range_type=str(effect[4]),
                    pick_count_min=int(effect[5]),
                    pick_count_max=int(effect[6]),
                )
            )

    return SchoolEventChoice(
        slot=slot,
        suggestion_id=suggestion_id,
        stamina_cost=int(suggestion[0]),
        produce_point_cost=int(suggestion[1]),
        direct_card_id=str(suggestion[2]),
        direct_card_upgrade=int(suggestion[3]),
        status_delta_min=status_min,
        status_delta_max=status_max,
        effect_ids=effect_ids,
        reward_selections=tuple(reward_selections),
        step_type=str(suggestion[5]),
        step_id=str(suggestion[6]),
        success_probability_permyriad=int(suggestion[7]),
        success_effect_ids=success_effect_ids,
        success_step_type=str(suggestion[9]),
        success_step_id=str(suggestion[10]),
        fail_effect_ids=fail_effect_ids,
        fail_step_type=str(suggestion[12]),
        fail_step_id=str(suggestion[13]),
        always_successful=bool(suggestion[14]),
        produce_effect_fire_step=int(suggestion[15]),
    )


def _load_detail_candidate(
    connection: sqlite3.Connection,
    row: tuple[object, ...],
) -> _SchoolEventDetailCandidate:
    suggestion_ids = _json_string_list(
        str(row[2]), field="suggestion_ids_json"
    )
    return _SchoolEventDetailCandidate(
        detail_id=str(row[0]),
        suggestion_type=str(row[1]),
        detail_effect_ids=_json_string_list(
            str(row[3]), field="produce_effect_ids_json"
        ),
        support_card_id=str(row[4]),
        event_type=str(row[5]),
        event_character_type=str(row[6]),
        is_business_excellent=bool(row[7]),
        choices=tuple(
            _load_choice(
                connection,
                slot=slot,
                suggestion_id=suggestion_id,
            )
            for slot, suggestion_id in enumerate(suggestion_ids, 1)
        ),
    )


def resolve_school_event(
    adv_asset_id: str,
    observed_stamina_costs: tuple[int, ...],
    *,
    database: Path = DEFAULT_DATABASE,
) -> SchoolEventResolution:
    """Resolve one visible choice set, collapsing Master-equivalent variants."""

    if not adv_asset_id:
        raise ValueError("adv_asset_id is empty")
    if not observed_stamina_costs or any(cost < 0 for cost in observed_stamina_costs):
        raise ValueError("observed stamina costs must be non-negative")

    with closing(sqlite3.connect(database)) as connection:
        story = connection.execute(
            "SELECT id, title FROM produce_story WHERE adv_asset_id = ?",
            (adv_asset_id,),
        ).fetchone()
        if story is None:
            raise KeyError(f"school-event story not found: {adv_asset_id}")

        matches: list[_SchoolEventDetailCandidate] = []
        detail_rows = connection.execute(
            """
            SELECT id, suggestion_type, suggestion_ids_json,
                   produce_effect_ids_json, support_card_id, event_type,
                   event_character_type, is_business_excellent
              FROM step_event_detail
             WHERE produce_story_id = ?
               AND suggestion_type = 'ProduceEventSuggestionType_Primary'
            """,
            (story[0],),
        ).fetchall()
        for detail_row in detail_rows:
            candidate = _load_detail_candidate(connection, detail_row)
            if len(candidate.choices) != len(observed_stamina_costs):
                continue
            if (
                tuple(choice.stamina_cost for choice in candidate.choices)
                == observed_stamina_costs
            ):
                matches.append(candidate)

    if not matches:
        raise LookupError(
            f"no school-event variant matches {adv_asset_id} costs "
            f"{observed_stamina_costs}"
        )
    signatures = {
        candidate.semantic_signature()
        for candidate in matches
    }
    if len(signatures) != 1:
        detail_ids = ", ".join(candidate.detail_id for candidate in matches)
        raise ValueError(
            "visible stamina costs are insufficient to disambiguate school "
            f"event variants: {detail_ids}"
        )

    selected = matches[0]
    return SchoolEventResolution(
        adv_asset_id=adv_asset_id,
        story_id=str(story[0]),
        title=str(story[1]),
        matching_detail_ids=tuple(candidate.detail_id for candidate in matches),
        choices=selected.choices,
        detail_effect_ids=selected.detail_effect_ids,
        suggestion_type=selected.suggestion_type,
        support_card_id=selected.support_card_id,
        event_type=selected.event_type,
        event_character_type=selected.event_character_type,
        is_business_excellent=selected.is_business_excellent,
    )


def resolve_school_event_detail(
    detail_id: str,
    *,
    database: Path = DEFAULT_DATABASE,
) -> SchoolEventResolution:
    """Resolve one exact server-selected event detail directly from Master.

    Live screen parsing has to identify an event variant from its visible ADV
    asset and stamina-cost vector.  An offline scenario already owns the exact
    ``ProduceStepEventDetail`` identity, so forcing it through that visual
    disambiguation would throw away stronger information.  This entry point
    preserves the exact detail and loads every referenced suggestion in the
    serialized order.
    """

    if not isinstance(detail_id, str) or not detail_id:
        raise ValueError("detail_id is empty")
    with closing(sqlite3.connect(database)) as connection:
        detail = connection.execute(
            """
            SELECT id, suggestion_type, suggestion_ids_json,
                   produce_effect_ids_json, support_card_id, event_type,
                   event_character_type, is_business_excellent,
                   produce_story_id
              FROM step_event_detail
             WHERE id = ?
            """,
            (detail_id,),
        ).fetchone()
        if detail is None:
            raise KeyError(f"school-event detail not found: {detail_id}")
        story = connection.execute(
            """
            SELECT adv_asset_id, title
              FROM produce_story
             WHERE id = ?
            """,
            (detail[8],),
        ).fetchone()
        if story is None:
            raise KeyError(
                f"school-event story not found for detail {detail_id}: {detail[8]}"
            )
        selected = _load_detail_candidate(connection, tuple(detail[:8]))

    return SchoolEventResolution(
        adv_asset_id=str(story[0]),
        story_id=str(detail[8]),
        title=str(story[1]),
        matching_detail_ids=(detail_id,),
        choices=selected.choices,
        detail_effect_ids=selected.detail_effect_ids,
        suggestion_type=selected.suggestion_type,
        support_card_id=selected.support_card_id,
        event_type=selected.event_type,
        event_character_type=selected.event_character_type,
        is_business_excellent=selected.is_business_excellent,
    )
