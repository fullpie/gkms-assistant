from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .master_db import DEFAULT_DATABASE


@dataclass(frozen=True, slots=True)
class ProduceEffectRecord:
    id: str
    effect_type: str
    value_min: int
    value_max: int
    resource_type: str

    def describe(self) -> str:
        effect = self.effect_type.removeprefix("ProduceEffectType_")
        resource = self.resource_type.removeprefix("ProduceResourceType_")
        value = (
            str(self.value_min)
            if self.value_min == self.value_max
            else f"{self.value_min}–{self.value_max}"
        )
        parts = [effect or self.id]
        if resource and resource != "Unknown":
            parts.append(resource)
        if self.value_min or self.value_max:
            parts.append(value)
        return " / ".join(parts)


@dataclass(frozen=True, slots=True)
class EventSuggestionRecord:
    id: str
    produce_point: int
    stamina: int
    produce_card_id: str
    success_probability_permyriad: int
    always_successful: bool
    effects: tuple[ProduceEffectRecord, ...]
    success_effects: tuple[ProduceEffectRecord, ...]
    fail_effects: tuple[ProduceEffectRecord, ...]
    descriptions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EventAnalysis:
    adv_asset_id: str
    story_id: str
    title: str
    detail_id: str
    event_type: str
    effects: tuple[ProduceEffectRecord, ...]
    suggestions: tuple[EventSuggestionRecord, ...]

    @property
    def has_numeric_branch(self) -> bool:
        return bool(self.effects or self.suggestions)


def _ids(payload: str) -> list[str]:
    value = json.loads(payload)
    return [item for item in value if isinstance(item, str)]


def _description_texts(payload: str) -> tuple[str, ...]:
    value = json.loads(payload)
    return tuple(
        item.get("text", "")
        for item in value
        if isinstance(item, dict) and item.get("text")
    )


def _effects(
    connection: sqlite3.Connection, effect_ids: list[str]
) -> tuple[ProduceEffectRecord, ...]:
    records: list[ProduceEffectRecord] = []
    for effect_id in effect_ids:
        row = connection.execute(
            """SELECT id, effect_type, effect_value_min, effect_value_max, resource_type
               FROM produce_effect WHERE id = ?""",
            (effect_id,),
        ).fetchone()
        if row is not None:
            records.append(ProduceEffectRecord(*row))
    return tuple(records)


def analyze_event_by_adv_asset(
    adv_asset_id: str, database: Path = DEFAULT_DATABASE
) -> EventAnalysis | None:
    if not database.is_file():
        return None
    with closing(sqlite3.connect(database)) as connection:
        story = connection.execute(
            "SELECT id, title FROM produce_story WHERE adv_asset_id = ?",
            (adv_asset_id,),
        ).fetchone()
        if story is None:
            return None
        detail = connection.execute(
            """SELECT id, event_type, produce_effect_ids_json, suggestion_ids_json
               FROM step_event_detail WHERE produce_story_id = ?""",
            (story[0],),
        ).fetchone()
        if detail is None:
            return EventAnalysis(adv_asset_id, story[0], story[1], "", "", (), ())

        event_effects = _effects(connection, _ids(detail[2]))
        suggestions: list[EventSuggestionRecord] = []
        for suggestion_id in _ids(detail[3]):
            row = connection.execute(
                """SELECT id, produce_point, stamina, produce_card_id,
                          success_probability_permyriad, always_successful,
                          produce_effect_ids_json, success_produce_effect_ids_json,
                          fail_produce_effect_ids_json, descriptions_json
                   FROM step_event_suggestion WHERE id = ?""",
                (suggestion_id,),
            ).fetchone()
            if row is None:
                continue
            suggestions.append(
                EventSuggestionRecord(
                    id=row[0],
                    produce_point=row[1],
                    stamina=row[2],
                    produce_card_id=row[3],
                    success_probability_permyriad=row[4],
                    always_successful=bool(row[5]),
                    effects=_effects(connection, _ids(row[6])),
                    success_effects=_effects(connection, _ids(row[7])),
                    fail_effects=_effects(connection, _ids(row[8])),
                    descriptions=_description_texts(row[9]),
                )
            )
        return EventAnalysis(
            adv_asset_id=adv_asset_id,
            story_id=story[0],
            title=story[1],
            detail_id=detail[0],
            event_type=detail[1],
            effects=event_effects,
            suggestions=tuple(suggestions),
        )
