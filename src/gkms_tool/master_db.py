from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


from .application_paths import app_root, master_database
PROJECT_ROOT = app_root()
DEFAULT_DATABASE = master_database()
_ADV_ASSET_PATTERN = re.compile(r"^adv_(.+)_\d+$")


@dataclass(frozen=True, slots=True)
class IdolProfile:
    id: str
    name: str
    character_id: str
    asset_id: str
    plan_type: str
    vocal: int
    dance: int
    visual: int
    vocal_growth: int
    dance_growth: int
    visual_growth: int
    stamina: int
    exam_initial_deck_id: str
    exam_effect_type: str
    produce_card_id: str

    @property
    def label(self) -> str:
        return f"{self.name} | {self.id}"


@dataclass(frozen=True, slots=True)
class InitialDeckCard:
    card_id: str
    upgrade: int
    name: str


@dataclass(frozen=True, slots=True)
class ModeInitialDeck:
    id: str
    produce_id: str
    exam_effect_type: str
    cards: tuple[InitialDeckCard, ...]


_PROFILE_COLUMNS = """
    id, name, character_id, asset_id, plan_type,
    vocal, dance, visual,
    vocal_growth, dance_growth, visual_growth,
    stamina, exam_initial_deck_id, exam_effect_type, produce_card_id
"""


def _profile(row: sqlite3.Row | tuple[object, ...]) -> IdolProfile:
    return IdolProfile(*row)


def list_idol_profiles(database: Path = DEFAULT_DATABASE) -> list[IdolProfile]:
    """Return all imported idol-card profiles, or an empty list before import."""

    if not database.is_file():
        return []
    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute(
            f"SELECT {_PROFILE_COLUMNS} FROM idol_card ORDER BY name, id"
        ).fetchall()
    return [_profile(row) for row in rows]


def get_idol_profile(
    idol_card_id: str, database: Path = DEFAULT_DATABASE
) -> IdolProfile | None:
    if not database.is_file():
        return None
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            f"SELECT {_PROFILE_COLUMNS} FROM idol_card WHERE id = ?",
            (idol_card_id,),
        ).fetchone()
    return None if row is None else _profile(row)


def get_mode_initial_deck(
    produce_id: str,
    exam_effect_type: str,
    database: Path = DEFAULT_DATABASE,
) -> ModeInitialDeck | None:
    """Return the mode-level initial deck selected by produce and archetype.

    This deliberately follows ``initial_deck_map``.  An idol card's legacy
    ``exam_initial_deck_id`` and unique ``produce_card_id`` are separate inputs
    and are not silently merged here because Master has no authoritative final
    composition table for that merge.
    """

    if not database.is_file():
        return None
    with closing(sqlite3.connect(database)) as connection:
        row = connection.execute(
            """
            SELECT m.exam_initial_deck_id, d.card_ids_json,
                   d.upgrade_counts_json
              FROM initial_deck_map AS m
              JOIN initial_deck AS d ON d.id = m.exam_initial_deck_id
             WHERE m.produce_id = ? AND m.exam_effect_type = ?
            """,
            (produce_id, exam_effect_type),
        ).fetchone()
        if row is None:
            return None
        card_ids = json.loads(row[1])
        upgrades = json.loads(row[2])
        if not isinstance(card_ids, list) or not all(
            isinstance(card_id, str) and card_id for card_id in card_ids
        ):
            raise ValueError(f"初始牌組卡牌格式不正確：{row[0]}")
        if not isinstance(upgrades, list) or not all(
            isinstance(upgrade, int) and not isinstance(upgrade, bool)
            and upgrade >= 0
            for upgrade in upgrades
        ):
            raise ValueError(f"初始牌組強化格式不正確：{row[0]}")
        if upgrades and len(upgrades) != len(card_ids):
            raise ValueError(f"初始牌組卡牌與強化數量不一致：{row[0]}")
        normalized_upgrades = upgrades or [0] * len(card_ids)
        cards: list[InitialDeckCard] = []
        for card_id, upgrade in zip(card_ids, normalized_upgrades, strict=True):
            card = connection.execute(
                """
                SELECT name FROM card
                 WHERE id = ? AND upgrade_count = ?
                """,
                (card_id, upgrade),
            ).fetchone()
            if card is None:
                raise KeyError(f"初始牌組找不到卡牌：{card_id} +{upgrade}")
            cards.append(InitialDeckCard(card_id, upgrade, str(card[0])))
    return ModeInitialDeck(
        id=str(row[0]),
        produce_id=produce_id,
        exam_effect_type=exam_effect_type,
        cards=tuple(cards),
    )


def find_idol_profile_for_adv_asset(
    adv_asset_id: str, database: Path = DEFAULT_DATABASE
) -> IdolProfile | None:
    """Resolve an ADV asset observed on screen to its owning idol card."""

    if not database.is_file():
        return None
    with closing(sqlite3.connect(database)) as connection:
        story = connection.execute(
            "SELECT id FROM produce_story WHERE adv_asset_id = ?",
            (adv_asset_id,),
        ).fetchone()
        if story is not None and story[0].startswith("p_story-"):
            card_id = story[0].removeprefix("p_story-").rsplit("-", 1)[0]
            row = connection.execute(
                f"SELECT {_PROFILE_COLUMNS} FROM idol_card WHERE id = ?",
                (card_id,),
            ).fetchone()
            if row is not None:
                return _profile(row)

        asset_match = _ADV_ASSET_PATTERN.fullmatch(adv_asset_id)
        if asset_match is None:
            return None
        row = connection.execute(
            f"SELECT {_PROFILE_COLUMNS} FROM idol_card WHERE asset_id = ?",
            (asset_match.group(1),),
        ).fetchone()
    return None if row is None else _profile(row)
