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


def portable_initial_descriptor(assets):
    """Read the release's model-bound aggregate evidence, never private data."""
    from copy import deepcopy
    assets.validate_unchanged()
    reference = assets.manifest['files']['actor_qualification']
    qualification = assets.read(reference)
    value = qualification.get('initial_loadout')
    checkpoint = qualification.get('original_checkpoint_identity')
    if (not isinstance(value, dict) or not isinstance(checkpoint, dict) or value.get('checkpoint') != checkpoint
            or checkpoint.get('sha256') != assets.manifest['models']['rl_shared_iql']['original_model_sha256']
            or type(checkpoint.get('bytes')) is not int or checkpoint['bytes'] <= 0
            or value.get('method') != 'shared-initial-composition-return-RL'
            or value.get('semantic_mode') != 'rich-static'
            or type(value.get('trained_updates')) is not int or value['trained_updates'] <= 0
            or value.get('trained_critic_updates') != value['trained_updates']
            or value['trained_updates'] != qualification.get('training_updates')
            or value.get('master_hash') != assets.manifest['master_hash']
            or value.get('execution_master_version') != assets.manifest['execution_master_version']
            or type(value.get('validation_histories')) is not int or value['validation_histories'] <= 0
            or value.get('validation_critic_rows') != 10 * value['validation_histories']
            or value['validation_histories'] != qualification.get('initial_validation_histories')
            or qualification.get('evaluation_complete') is not True
            or value.get('runtime_reference_trial_exported') is not False):
        raise ValueError('公開編成模型缺少同版本訓練／完整評估證據。')
    cells = value.get('scope_cells')
    if (not isinstance(cells, list) or not cells or any(not isinstance(cell, list) or len(cell) != 2
            or any(not isinstance(item, str) or not item for item in cell) for cell in cells)
            or len({tuple(cell) for cell in cells}) != len(cells)):
        raise ValueError('公開編成流派／模式範圍無法確認。')
    return {**deepcopy(value), 'portable': True, 'reference': assets.physical_reference(reference)}


def build_portable_initial_source(assets):
    """Reuse the original semantic projector over the verified public tables."""
    from .portable_model_assets import PortableCardFeatures
    from .portable_outer_assets import asset_directory, verify_assets, RELEASE_MANIFEST_SHA256
    from .rl.observed_outer_fine_projection import FineOuterSemanticSource
    from .rl.observed_initial_loadout import SOURCE_SCHEMA
    root = asset_directory()
    if root is None:
        raise ValueError('公開編成所需的遊戲資料尚未安裝。')
    manifest = verify_assets(root)
    if manifest['master_hash'] != assets.manifest['master_hash']:
        raise ValueError('公開編成與模型的遊戲資料版本不同。')
    path = root / 'manifest.json'
    reference = {'path': str(path.resolve()), 'sha256': RELEASE_MANIFEST_SHA256, 'bytes': path.stat().st_size}

    class Source(FineOuterSemanticSource):
        def initial_passive_catalog(self):
            from .passive_catalog import MasterPassiveCatalog, SUPPORT_LEVEL_TABLES
            if not hasattr(self, '_initial_catalog'):
                self._initial_catalog = MasterPassiveCatalog(memory_gifts=[],
                    memory_abilities=self._table_rows('MemoryAbility.yaml'),
                    support_cards=self._table_rows('SupportCard.yaml'),
                    support_skill_levels={key: self._table_rows(name) for key, name in SUPPORT_LEVEL_TABLES.items()},
                    produce_skills=self._table_rows('ProduceSkill.yaml'),
                    produce_effects=self._table_rows('ProduceEffect.yaml'),
                    produce_triggers=self._table_rows('ProduceTrigger.yaml'))
            return self._initial_catalog

    def unchanged():
        assets.validate_unchanged()
        verify_assets(root)

    source = Source(PortableCardFeatures(assets), reference, validate_materials=unchanged)
    source.initial_binding = {'schema': SOURCE_SCHEMA, 'source_master_hash': manifest['master_hash'],
        'portable_models_manifest': assets.reference, 'portable_outer_manifest': reference}
    source.validate_unchanged()
    return source
