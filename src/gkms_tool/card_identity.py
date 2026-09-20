"""Resolve OCR card names against the offline master and local translation data.

Only static files are read: ``var/master.sqlite3`` and, when installed, the
gakumas-local skill-card translation JSON.  This module never opens the game
process.
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from .application_paths import game_file

from .master_db import DEFAULT_DATABASE


DEFAULT_TRANSLATION_PATH = game_file('gakumas-local/local-files/genericTrans/genericTrans/index/skill_card_name.json')
DEFAULT_MASTER_TRANSLATION_PATH = game_file('gakumas-local/local-files/masterTrans/ProduceCard.json')

# Paddle's Chinese line model occasionally emits the simplified form for one
# character inside an otherwise Traditional-Chinese card name.  Keep this map
# deliberately evidence-based instead of lowering the fuzzy-name threshold for
# every short card name.
_OCR_CHARACTER_EQUIVALENTS = str.maketrans({"满": "滿", "刚": "剛"})


def normalize_card_text(value: str) -> str:
    """Normalize OCR noise while retaining upgrade markers and percentages."""

    normalized = (
        unicodedata.normalize("NFKC", value)
        .translate(_OCR_CHARACTER_EQUIVALENTS)
        .casefold()
    )
    return "".join(
        character
        for character in normalized
        if character.isalnum() or character in {"+", "%"}
    )


def load_name_translations(path: Path = DEFAULT_TRANSLATION_PATH) -> dict[str, str]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"card-name translation must be an object: {path}")
    return {
        str(source): str(translated)
        for source, translated in payload.items()
        if isinstance(source, str) and isinstance(translated, str)
    }


def load_master_name_translations(
    path: Path = DEFAULT_MASTER_TRANSLATION_PATH,
) -> dict[tuple[str, int], str]:
    """Load exact translated names keyed by master card id and upgrade."""

    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError(f"translated ProduceCard data must be a list: {path}")
    result: dict[tuple[str, int], str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        card_id = row.get("id")
        upgrade = row.get("upgradeCount")
        name = row.get("name")
        if isinstance(card_id, str) and isinstance(upgrade, int) and isinstance(name, str):
            result[(card_id, upgrade)] = name
    return result


@dataclass(frozen=True, slots=True)
class CardNameEntry:
    card_id: str
    upgrade: int
    official_name: str
    display_name: str
    plan_type: str
    category: str


@dataclass(frozen=True, slots=True)
class CardNameMatch:
    card_id: str
    upgrade: int
    official_name: str
    display_name: str
    matched_alias: str
    similarity: float
    plan_type: str
    category: str


def _base_name(name: str, upgrade: int) -> str:
    suffix = "+" * max(0, upgrade)
    return name[: -len(suffix)] if suffix and name.endswith(suffix) else name


class CardNameCatalog:
    def __init__(self, entries: tuple[CardNameEntry, ...]) -> None:
        self.entries = entries

    @classmethod
    def load(
        cls,
        database: Path = DEFAULT_DATABASE,
        translation_path: Path = DEFAULT_TRANSLATION_PATH,
        master_translation_path: Path = DEFAULT_MASTER_TRANSLATION_PATH,
    ) -> "CardNameCatalog":
        if not database.is_file():
            raise FileNotFoundError(f"master database not found: {database}")
        translations = load_name_translations(translation_path)
        exact_translations = load_master_name_translations(master_translation_path)
        with closing(sqlite3.connect(database)) as connection:
            rows = connection.execute(
                """SELECT id, upgrade_count, name, plan_type, category
                   FROM card ORDER BY id, upgrade_count"""
            ).fetchall()
        entries = []
        for card_id, upgrade, official_name, plan_type, category in rows:
            upgrade = int(upgrade)
            base = _base_name(str(official_name), upgrade)
            translated = translations.get(base, base)
            display_name = exact_translations.get(
                (str(card_id), upgrade), translated + "+" * max(0, upgrade)
            )
            entries.append(
                CardNameEntry(
                    card_id=str(card_id),
                    upgrade=upgrade,
                    official_name=str(official_name),
                    display_name=display_name,
                    plan_type=str(plan_type),
                    category=str(category),
                )
            )
        return cls(tuple(entries))

    def match(
        self,
        observed_text: str,
        *,
        limit: int = 5,
        plan_type: str | None = None,
    ) -> tuple[CardNameMatch, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        query = normalize_card_text(observed_text)
        if not query:
            return ()
        matches: list[CardNameMatch] = []
        for entry in self.entries:
            if plan_type is not None and entry.plan_type != plan_type:
                continue
            aliases = {entry.official_name, entry.display_name}
            best_alias = ""
            best_score = 0.0
            for alias in aliases:
                candidate = normalize_card_text(alias)
                if not candidate:
                    continue
                score = (
                    1.0
                    if query == candidate
                    else SequenceMatcher(None, query, candidate).ratio()
                )
                if score > best_score:
                    best_alias = alias
                    best_score = score
            matches.append(
                CardNameMatch(
                    card_id=entry.card_id,
                    upgrade=entry.upgrade,
                    official_name=entry.official_name,
                    display_name=entry.display_name,
                    matched_alias=best_alias,
                    similarity=best_score,
                    plan_type=entry.plan_type,
                    category=entry.category,
                )
            )
        matches.sort(
            key=lambda item: (
                -item.similarity,
                item.upgrade,
                item.card_id,
            )
        )
        return tuple(matches[:limit])

    def resolve_asset(self, asset_name: str, *, upgrade: int = 0) -> CardNameEntry:
        """Resolve generic or character-specific artwork to one Master row."""

        from .octo_assets import card_suffix_from_asset

        suffix = card_suffix_from_asset(asset_name)
        candidates = [
            entry
            for entry in self.entries
            if entry.upgrade == upgrade and entry.card_id.endswith("-" + suffix)
        ]
        if len(candidates) != 1:
            ids = sorted(entry.card_id for entry in candidates)
            raise ValueError(f"card asset {asset_name} resolves ambiguously: {ids}")
        return candidates[0]
