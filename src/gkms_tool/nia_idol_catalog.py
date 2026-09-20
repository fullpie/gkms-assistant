"""Master-backed N.I.A. idol/card-name recognition catalog.

The N.I.A. idol picker renders two independent OCR labels: the character
name and the idol-card (song) name.  Their Japanese values are authoritative
in the checked-in Master database/YAML, while the installed ``masterTrans``
tables contain the current Traditional-Chinese presentation.  This module
joins those sources by stable IDs and exposes deterministic aliases for the
Maa custom recognizer.

No name is inferred from an ID or from another card.  If a translation row is
absent, the authoritative Japanese value remains the only value for that
field.  Malformed/ambiguous source data fails closed instead of producing a
guess that could select a different idol card.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from .application_paths import game_file
from typing import Any, Mapping

import yaml

from .master_db import DEFAULT_DATABASE


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MASTER_DIR = PROJECT_ROOT / "_research" / "gakumasu-diff"
DEFAULT_CHARACTER_MASTER = DEFAULT_MASTER_DIR / "Character.yaml"
DEFAULT_MASTER_TRANSLATION_DIR = game_file('gakumas-local/local-files/masterTrans')
DEFAULT_CHARACTER_TRANSLATION = DEFAULT_MASTER_TRANSLATION_DIR / "Character.json"
DEFAULT_IDOL_CARD_TRANSLATION = DEFAULT_MASTER_TRANSLATION_DIR / "IdolCard.json"


def _required_text(value: Any, field: str, source: Path | str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source}: {field} must be non-empty text")
    return value


def _load_character_master(path: Path) -> dict[str, str]:
    """Load the Japanese character names from the repository Master table."""

    if Path(path).resolve()==DEFAULT_CHARACTER_MASTER.resolve():
        from .portable_character_labels import load_character_labels
        portable=load_character_labels()
        if portable is not None:return portable

    if not path.is_file():
        raise FileNotFoundError(f"Character Master not found: {path}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ValueError(f"failed to read Character Master: {path}") from error
    if not isinstance(payload, list):
        raise ValueError(f"Character Master must contain an array: {path}")
    result: dict[str, str] = {}
    for index, row in enumerate(payload):
        if not isinstance(row, Mapping):
            raise ValueError(f"Character Master row {index} is not an object: {path}")
        character_id = _required_text(row.get("id"), f"row {index} id", path)
        last_name = _required_text(
            row.get("lastName"), f"{character_id}.lastName", path
        )
        first_name = _required_text(
            row.get("firstName"), f"{character_id}.firstName", path
        )
        if character_id in result:
            raise ValueError(f"duplicate Character Master id: {character_id}")
        result[character_id] = last_name + first_name
    return result


def _load_translation_rows(
    path: Path,
    table_name: str,
    *,
    character_name_fields: bool = False,
) -> dict[str, str]:
    """Load a Localify ``masterTrans`` table keyed by its stable ``id``.

    A missing table is intentionally treated as an empty translation map: the
    caller can still use the Japanese Master value.  If a table exists, every
    row must be structurally valid so a partial/corrupt update cannot silently
    turn into a recognition alias.
    """

    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"failed to read translated {table_name}: {path}") from error
    rows = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError(f"translated {table_name} data must be an array: {path}")
    result: dict[str, str] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"translated {table_name} row {index} is not an object")
        row_id = _required_text(row.get("id"), f"row {index} id", path)
        if character_name_fields:
            last_name = _required_text(
                row.get("lastName"), f"{row_id}.lastName", path
            )
            first_name = _required_text(
                row.get("firstName"), f"{row_id}.firstName", path
            )
            name = last_name + first_name
        else:
            name = _required_text(row.get("name"), f"{row_id}.name", path)
        if row_id in result:
            raise ValueError(f"duplicate translated {table_name} id: {row_id}")
        result[row_id] = name
    return result


def _dedupe_aliases(*values: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


@dataclass(frozen=True, slots=True)
class NiaIdolCatalogEntry:
    """One Master idol-card row plus its translated recognition aliases.

    ``idol_name`` and ``song_name`` are the Japanese Master values retained by
    the game data.  ``idol_name_zh_tw`` and ``song_name_zh_tw`` are the current
    Traditional-Chinese ``masterTrans`` values, falling back to the Japanese
    value when no translation row exists.  The alias tuples intentionally do
    not duplicate the canonical value.
    """

    idol_card_id: str
    character_id: str
    rarity: str
    plan_type: str
    idol_name: str
    idol_name_zh_tw: str
    song_name: str
    song_name_zh_tw: str
    idol_name_aliases: tuple[str, ...]
    song_name_aliases: tuple[str, ...]

    @property
    def translated_idol_name(self) -> str:
        """Readable alias for callers interested in the game presentation."""

        return self.idol_name_zh_tw

    @property
    def translated_song_name(self) -> str:
        return self.song_name_zh_tw

    def bootstrap_tuple(self) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
        """Return the exact shape consumed by ``GkmsToolChooseNiaIdol``."""

        return (
            self.idol_name,
            self.song_name,
            self.idol_name_aliases,
            self.song_name_aliases,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "idol_card_id": self.idol_card_id,
            "character_id": self.character_id,
            "rarity": self.rarity,
            "plan_type": self.plan_type,
            "idol_name": self.idol_name,
            "idol_name_zh_tw": self.idol_name_zh_tw,
            "idol_name_aliases": list(self.idol_name_aliases),
            "song_name": self.song_name,
            "song_name_zh_tw": self.song_name_zh_tw,
            "song_name_aliases": list(self.song_name_aliases),
        }


class NiaIdolCatalog:
    """Complete, immutable ``idol_card_id`` recognition catalog."""

    def __init__(self, entries: tuple[NiaIdolCatalogEntry, ...]) -> None:
        if not entries:
            raise ValueError("N.I.A. idol catalog must not be empty")
        by_id: dict[str, NiaIdolCatalogEntry] = {}
        for entry in entries:
            if entry.idol_card_id in by_id:
                raise ValueError(
                    f"duplicate N.I.A. idol catalog id: {entry.idol_card_id}"
                )
            by_id[entry.idol_card_id] = entry
        self.entries = tuple(entries)
        self._by_id = by_id

    @classmethod
    def load(
        cls,
        database: Path = DEFAULT_DATABASE,
        *,
        character_master: Path = DEFAULT_CHARACTER_MASTER,
        master_dir: Path | None = None,
        master_trans_dir: Path = DEFAULT_MASTER_TRANSLATION_DIR,
        character_translation: Path | None = None,
        idol_card_translation: Path | None = None,
    ) -> "NiaIdolCatalog":
        """Build the catalog from repository Master and Localify tables.

        The SQLite ``idol_card`` table is the row authority.  Character Master
        supplies the Japanese character label (the SQLite import retains only
        ``character_id``), and the two ``masterTrans`` files supply optional
        Traditional-Chinese aliases.  Every SQLite row is represented exactly
        once; no row is synthesized from an ID pattern.
        """

        database = Path(database).resolve()
        if master_dir is not None:
            character_master = Path(master_dir) / "Character.yaml"
        character_master = Path(character_master).resolve()
        master_trans_dir = Path(master_trans_dir).resolve()
        if master_trans_dir.exists() and not master_trans_dir.is_dir():
            raise ValueError(
                f"masterTrans source must be a directory: {master_trans_dir}"
            )
        character_translation = (
            master_trans_dir / "Character.json"
            if character_translation is None
            else Path(character_translation).resolve()
        )
        idol_card_translation = (
            master_trans_dir / "IdolCard.json"
            if idol_card_translation is None
            else Path(idol_card_translation).resolve()
        )
        if not database.is_file():
            raise FileNotFoundError(f"Master database not found: {database}")

        japanese_characters = _load_character_master(character_master)
        translated_characters = _load_translation_rows(
            character_translation, "Character", character_name_fields=True
        )
        translated_cards = _load_translation_rows(idol_card_translation, "IdolCard")

        try:
            with closing(sqlite3.connect(database)) as connection:
                rows = connection.execute(
                    "SELECT id, character_id, name, plan_type, raw_json "
                    "FROM idol_card ORDER BY id"
                ).fetchall()
        except sqlite3.Error as error:
            raise ValueError(f"Master database idol_card query failed: {database}") from error
        if not rows:
            raise ValueError(f"Master database idol_card table is empty: {database}")

        entries: list[NiaIdolCatalogEntry] = []
        seen_ids: set[str] = set()
        for index, row in enumerate(rows):
            if len(row) != 5:
                raise ValueError(f"Master database idol_card row {index} is malformed")
            idol_card_id = _required_text(row[0], f"idol_card row {index} id", database)
            character_id = _required_text(
                row[1], f"{idol_card_id}.character_id", database
            )
            song_name = _required_text(row[2], f"{idol_card_id}.name", database)
            plan_type = _required_text(
                row[3], f"{idol_card_id}.plan_type", database
            )
            raw_json = _required_text(
                row[4], f"{idol_card_id}.raw_json", database
            )
            try:
                raw = json.loads(raw_json)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"invalid Master idol_card raw_json: {idol_card_id}"
                ) from error
            if not isinstance(raw, Mapping):
                raise ValueError(
                    f"Master idol_card raw_json must be an object: {idol_card_id}"
                )
            rarity = _required_text(
                raw.get("rarity"), f"{idol_card_id}.rarity", database
            )
            raw_plan_type = _required_text(
                raw.get("planType"), f"{idol_card_id}.raw_plan_type", database
            )
            if raw_plan_type != plan_type:
                raise ValueError(
                    "Master idol_card plan_type disagrees with raw_json: "
                    f"{idol_card_id} -> {plan_type} / {raw_plan_type}"
                )
            if rarity not in {
                "IdolCardRarity_R",
                "IdolCardRarity_Sr",
                "IdolCardRarity_Ssr",
            }:
                raise ValueError(
                    f"unsupported Master idol_card rarity: "
                    f"{idol_card_id} -> {rarity}"
                )
            if plan_type not in {
                "ProducePlanType_Plan1",
                "ProducePlanType_Plan2",
                "ProducePlanType_Plan3",
            }:
                raise ValueError(
                    f"unsupported Master idol_card plan_type: "
                    f"{idol_card_id} -> {plan_type}"
                )
            if idol_card_id in seen_ids:
                raise ValueError(f"duplicate Master idol_card id: {idol_card_id}")
            seen_ids.add(idol_card_id)
            try:
                idol_name = japanese_characters[character_id]
            except KeyError as error:
                raise ValueError(
                    "Master idol_card references a character missing from "
                    f"Character Master: {idol_card_id} -> {character_id}"
                ) from error

            translated_idol_name = translated_characters.get(character_id, idol_name)
            translated_song_name = translated_cards.get(idol_card_id, song_name)
            entries.append(
                NiaIdolCatalogEntry(
                    idol_card_id=idol_card_id,
                    character_id=character_id,
                    rarity=rarity,
                    plan_type=plan_type,
                    idol_name=idol_name,
                    idol_name_zh_tw=translated_idol_name,
                    song_name=song_name,
                    song_name_zh_tw=translated_song_name,
                    idol_name_aliases=_dedupe_aliases(
                        translated_idol_name if translated_idol_name != idol_name else ""
                    ),
                    song_name_aliases=_dedupe_aliases(
                        translated_song_name if translated_song_name != song_name else ""
                    ),
                )
            )
        return cls(tuple(entries))

    def get(self, idol_card_id: str) -> NiaIdolCatalogEntry | None:
        if not isinstance(idol_card_id, str) or not idol_card_id:
            return None
        return self._by_id.get(idol_card_id)

    def require(self, idol_card_id: str) -> NiaIdolCatalogEntry:
        entry = self.get(idol_card_id)
        if entry is None:
            raise ValueError(f"unsupported N.I.A. bootstrap idol_card_id: {idol_card_id}")
        return entry

    def bootstrap_values(
        self, idol_card_id: str
    ) -> tuple[str, str, tuple[str, ...], tuple[str, ...]]:
        return self.require(idol_card_id).bootstrap_tuple()

    def __len__(self) -> int:
        return len(self.entries)


def load_nia_idol_catalog(
    database: Path = DEFAULT_DATABASE,
    *,
    character_master: Path = DEFAULT_CHARACTER_MASTER,
    master_dir: Path | None = None,
    master_trans_dir: Path = DEFAULT_MASTER_TRANSLATION_DIR,
    character_translation: Path | None = None,
    idol_card_translation: Path | None = None,
) -> NiaIdolCatalog:
    """Functional loader kept convenient for Maa and test callers."""

    return NiaIdolCatalog.load(
        database,
        character_master=character_master,
        master_dir=master_dir,
        master_trans_dir=master_trans_dir,
        character_translation=character_translation,
        idol_card_translation=idol_card_translation,
    )


__all__ = [
    "DEFAULT_CHARACTER_MASTER",
    "DEFAULT_CHARACTER_TRANSLATION",
    "DEFAULT_DATABASE",
    "DEFAULT_IDOL_CARD_TRANSLATION",
    "DEFAULT_MASTER_DIR",
    "DEFAULT_MASTER_TRANSLATION_DIR",
    "NiaIdolCatalog",
    "NiaIdolCatalogEntry",
    "load_nia_idol_catalog",
]
