"""Read-only, release-pinned static Master inputs for the existing loadout scorer."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

SCHEMA = "gkms.portable-loadout-assets.v1"
MASTER_HASH = "12b24ff515f7dcdc2aa107bdea1745fb2f6177cab1f6381ae709ef8b223d7aa8"
SOURCE_ARCHIVE_SHA256 = "aeb99a1573e83cee243866f0ead382e5665e3e916826a3f0c2ec0db258afbca4"
MODEL_MANIFEST_SHA256 = "e85b8eae35b917f04776ef7ea8c208d47e7a5558c38e71c5a0f9a256d1761923"
RELEASE_MANIFEST_SHA256 = "4238d4ee97e5b6a7a610cca05334d7a5ec789a12dbacf141119ecb059ee1c296"
TABLES = ("MemoryGift", "MemoryAbility", "SupportCard", "SupportCardProduceSkillLevelVocal",
          "SupportCardProduceSkillLevelDance", "SupportCardProduceSkillLevelVisual", "SupportCardProduceSkillLevelAssist",
          "ProduceSkill", "ProduceEffect", "ProduceTrigger")


def _read(path, expected, limit):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Portable loadout asset is unavailable")
    before = path.stat()
    if before.st_size > limit:
        raise ValueError("Portable loadout asset exceeds its size bound")
    raw = path.read_bytes()
    after = path.stat()
    if ((before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns)
            or hashlib.sha256(raw).hexdigest() != expected):
        raise ValueError("Portable loadout asset identity changed")
    return json.loads(raw)


def load_portable_passive_catalog(directory):
    from .passive_catalog import MasterPassiveCatalog, SUPPORT_LEVEL_TABLES
    directory = Path(directory).resolve()
    manifest = _read(directory / "manifest.json", RELEASE_MANIFEST_SHA256, 64 * 1024)
    if (manifest.get("schema") != SCHEMA or manifest.get("master_hash") != MASTER_HASH
            or manifest.get("source_archive_sha256") != SOURCE_ARCHIVE_SHA256
            or manifest.get("model_manifest_sha256") != MODEL_MANIFEST_SHA256
            or set(manifest.get("tables", {})) != set(TABLES)):
        raise ValueError("Portable loadout source/version contract differs")
    tables = {}
    for name in TABLES:
        entry = manifest["tables"][name]
        if entry.get("file") != name + ".json":
            raise ValueError("Portable loadout table path differs")
        rows = _read(directory / entry["file"], entry["sha256"], 64 * 1024 * 1024)
        if not isinstance(rows, list) or len(rows) != entry["rows"] or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Portable loadout table shape differs")
        tables[name] = rows
    catalog = MasterPassiveCatalog(memory_gifts=tables["MemoryGift"], memory_abilities=tables["MemoryAbility"],
        support_cards=tables["SupportCard"], support_skill_levels={key: tables[Path(value).stem] for key, value in SUPPORT_LEVEL_TABLES.items()},
        produce_skills=tables["ProduceSkill"], produce_effects=tables["ProduceEffect"], produce_triggers=tables["ProduceTrigger"])
    catalog.portable_source = {"schema": SCHEMA, "master_hash": MASTER_HASH, "manifest_sha256": RELEASE_MANIFEST_SHA256}
    return catalog
