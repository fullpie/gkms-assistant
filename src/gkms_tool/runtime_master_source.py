"""Pinned current-Master artifacts for inference, separate from training data."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
from pathlib import Path
import re

from .card_data_update import load_card_data_snapshot
from .card_semantic_features import FEATURE_SCHEMA
from .live_pc_consumer_contract import validate_live_pc_consumer_identity
from .training_artifact_io import sha256_file

SCHEMA = 'gkms.runtime-Master-inference-source.v1'
_SEAL = object()
_HASH = re.compile(r'[a-f0-9]{64}\Z')


class RuntimeMasterSource:
    def __init__(self, reference, body, materials, watched, *, _authority=None):
        if _authority is not _SEAL:
            raise ValueError('Use load_runtime_master_source')
        self._reference, self._body = deepcopy(reference), deepcopy(body)
        self._materials, self._watched = materials, tuple(watched)

    @property
    def reference(self): return deepcopy(self._reference)
    @property
    def master_hash(self): return self._body['master_hash']
    @property
    def execution_master_version(self): return self._body['execution_master_version']
    @property
    def produce_ids(self): return tuple(self._body['produce_ids'])
    @property
    def source_database(self): return deepcopy(self._body['database'])
    @property
    def native_schema_reference(self): return deepcopy(self._body['native_schema'])
    @property
    def native_contract_template(self): return deepcopy(self._materials['template'])
    @property
    def provenance(self):
        self.validate_unchanged()
        return {'schema': SCHEMA, 'receipt': self.reference, 'source_master_hash': self.master_hash,
            'execution_master_version': self.execution_master_version,
            'runtime_observation': deepcopy(self._body['runtime_observation']),
            'source_archive': deepcopy(self._body['archive']),
            'snapshot': deepcopy(self._body['snapshot']), 'catalog': deepcopy(self._body['catalog']),
            'inference_only': True, 'historical_Master_aliased': False, 'training_admitted': False,
            'observed_native_Master_hash_invented': False, 'model_quality_accepted': False}

    def validate_unchanged(self):
        for path, wanted in self._watched:
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) != wanted:
                raise ValueError('Current inference Master artifact changed: ' + str(path))

    def package(self, *, source_resolver=None):
        from .native_structure_features import load_native_structure_inference_package
        self.validate_unchanged()
        return load_native_structure_inference_package(self.reference, source_resolver=source_resolver)

    def _package_materials(self):
        """Internal handoff to the public package factory; never a training row."""
        self.validate_unchanged()
        return self._materials, deepcopy(self._body)


_CACHE = {}


def load_runtime_master_source(reference):
    if not isinstance(reference, Mapping) or not isinstance(reference.get('path'), str) or not _HASH.fullmatch(reference.get('sha256', '')):
        raise ValueError('An exact current-Master source receipt is required')
    key = str(Path(reference['path']).resolve()), reference['sha256']
    if key in _CACHE:
        _CACHE[key].validate_unchanged()
        return _CACHE[key]
    watched = []
    def read(ref, *, document=True):
        path = Path(ref['path']).resolve(); before = path.stat()
        if sha256_file(path) != ref['sha256']:
            raise ValueError('Current inference Master source SHA differs: ' + str(path))
        value = json.loads(path.read_bytes()) if document else None
        after = path.stat(); stamp = after.st_size, after.st_mtime_ns, after.st_ctime_ns
        if stamp != (before.st_size, before.st_mtime_ns, before.st_ctime_ns):
            raise ValueError('Current inference Master changed during verification')
        watched.append((path, stamp)); return value
    body = read(reference)
    required = {'schema', 'master_hash', 'execution_master_version', 'produce_ids', 'runtime_observation',
        'archive', 'database', 'snapshot', 'catalog', 'supplemental_table', 'native_schema',
        'representation_contract_set', 'template_contract_sha256', 'inference_only', 'training_admitted'}
    if (set(body) != required or body.get('schema') != SCHEMA or body.get('inference_only') is not True
            or body.get('training_admitted') is not False or not _HASH.fullmatch(body.get('master_hash', ''))
            or body.get('execution_master_version') != body['master_hash']
            or body.get('produce_ids') != ['produce-004', 'produce-005']):
        raise ValueError('Current-Master source must remain explicit, inference-only and mode-bounded')
    observation = read(body['runtime_observation'])
    context = observation.get('model_context', {})
    validate_live_pc_consumer_identity(context.get('engine_identity'))
    actual = context.get('execution_master', {})
    if (observation.get('status') != 'ok' or actual.get('schema') != 'gkms.native-execution-master-observation.v1'
            or actual.get('authority') != 'native-existing-MasterManager' or actual.get('ready') is not True
            or actual.get('manager_count') != 1 or actual.get('master_tables_initialized') is not True
            or actual.get('master_update_succeeded') is not True
            or actual.get('execution_master_version') != body['execution_master_version']
            or actual.get('execution_master_hash') not in (None, body['master_hash'])):
        raise ValueError('Current source is not bound to an actual ready MasterManager observation')
    archive = read(body['archive'])
    if (archive.get('schema') != 'gkms.version-bound-reconstruction-master.v1'
            or archive.get('master_hash') != body['master_hash'] or archive.get('upstream_commit_subject') != body['master_hash']
            or archive.get('upstream_url') != 'https://github.com/vertesan/gakumasu-diff.git'
            or archive.get('database_sha256') != body['database']['sha256']
            or Path(archive.get('database', '')).resolve() != Path(body['database']['path']).resolve()):
        raise ValueError('Current Master archive/DB does not preserve the exact observed public release identity')
    read(body['database'], document=False)
    snapshot = read(body['snapshot'])
    verified_snapshot = load_card_data_snapshot(Path(body['snapshot']['path']))
    if snapshot != verified_snapshot or Path(snapshot['source_database']).resolve() != Path(body['database']['path']).resolve():
        raise ValueError('Current Master snapshot differs from its own DB/fingerprint')
    catalog = read(body['catalog'])
    if catalog.get('schema') != FEATURE_SCHEMA or catalog.get('snapshot_fingerprint') != snapshot['fingerprint'] or catalog.get('blockers') != []:
        raise ValueError('Current semantic catalog lacks complete matching source definitions')
    supplement_path = Path(body['supplemental_table']['path']).resolve()
    if (supplement_path != Path(archive['master_dir']).resolve() / 'ProduceCardStatusEnchant.yaml'
            or body['supplemental_table']['sha256'] != archive['yaml_sha256']['ProduceCardStatusEnchant.yaml']):
        raise ValueError('Current supplemental table is outside the same exact archive')
    read(body['supplemental_table'], document=False)
    import yaml
    rows = yaml.load(supplement_path.read_text(encoding='utf-8'), Loader=getattr(yaml, 'CSafeLoader', yaml.SafeLoader))
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get('id'), str) for row in rows):
        raise ValueError('Current supplemental Master rows are invalid')
    supplement = {row['id']: row for row in rows}
    if len(supplement) != len(rows):
        raise ValueError('Current supplemental Master row IDs are duplicated')
    contract_set = read(body['representation_contract_set'])
    from .native_structure_contract_set import validate_native_structure_contract_set
    validate_native_structure_contract_set(contract_set)
    template = contract_set['contracts'][body['template_contract_sha256']]['native_structure_contract']
    if template['native_schema_reference'] != body['native_schema'] or body['master_hash'] in {row['source_master_hash'] for row in contract_set['routes']}:
        raise ValueError('A distinct runtime source and the actual frozen representation authority are required')
    read(body['native_schema'])
    source = RuntimeMasterSource(reference, body, {'payload': catalog, 'snapshot': snapshot,
        'supplement': supplement, 'template': template}, watched, _authority=_SEAL)
    source.validate_unchanged(); _CACHE[key] = source; return source


__all__ = ['RuntimeMasterSource', 'load_runtime_master_source', 'SCHEMA']
