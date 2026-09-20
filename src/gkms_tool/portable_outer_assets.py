"""Release-pinned outer rules and qualified behavior projections; no game I/O."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .application_paths import asset_directory as runtime_asset_directory

SCHEMA = 'gkms.portable-outer-assets.v1'
MASTER_HASH = '12b24ff515f7dcdc2aa107bdea1745fb2f6177cab1f6381ae709ef8b223d7aa8'
MODEL_MANIFEST_SHA256 = 'e85b8eae35b917f04776ef7ea8c208d47e7a5558c38e71c5a0f9a256d1761923'
RELEASE_MANIFEST_SHA256 = '1dcfa769f87e52fd5beb8931581bc7d24745621104fae830c54cd0ea24783abb'
REQUIRED_ROLES = frozenset({'weekly_provider','weekly_model','weekly_manifest','weekly_report',
                            'behavior_count_prior','deck_plan_library'})
_cached = None


def asset_directory():
    return runtime_asset_directory('outer_runtime')


def _signature(path):
    stat=path.stat()
    return stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns


def _verified_file(root, reference):
    name=reference.get('path')
    if not isinstance(name,str) or '\\' in name or any(part in ('','.','..') for part in name.split('/')):
        raise ValueError('Portable outer asset path is invalid')
    path=root/name
    if (Path(name).is_absolute() or not path.resolve().is_relative_to(root.resolve()) or path.is_symlink()
            or not path.is_file() or path.stat().st_size!=reference.get('bytes')):
        raise ValueError('Portable outer asset is missing or has changed: '+name)
    before=_signature(path)
    with path.open('rb') as stream: digest=hashlib.file_digest(stream,'sha256').hexdigest()
    if digest!=reference.get('sha256') or _signature(path)!=before:
        raise ValueError('Portable outer asset SHA differs: '+name)
    return path,before


def verify_assets(directory=None):
    global _cached
    root=Path(directory) if directory is not None else asset_directory()
    if root is None:return None
    manifest=root/'manifest.json'
    if _cached is not None and _cached[0]==root.resolve():
        try:
            if all(_signature(path)==signature for path,signature in _cached[2]):return _cached[1]
        except OSError:pass
    if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size>256*1024:
        raise ValueError('Required public outer-policy assets are unavailable before AP spending')
    raw=manifest.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=RELEASE_MANIFEST_SHA256:
        raise ValueError('Public outer-policy manifest is not the qualified release')
    value=json.loads(raw)
    if (value.get('schema')!=SCHEMA or value.get('master_hash')!=MASTER_HASH
            or value.get('model_manifest_sha256')!=MODEL_MANIFEST_SHA256
            or not REQUIRED_ROLES<=set(value.get('files',{})) or not value.get('tables')
            or value.get('contains_raw_replays') is not False or value.get('trained_new_parameters') is not False):
        raise ValueError('Public outer-policy asset contract differs')
    proofs=[(manifest,_signature(manifest))]
    for ref in (*value['files'].values(),*value['tables'].values()):proofs.append(_verified_file(root,ref))
    _cached=(root.resolve(),value,proofs)
    return value


def role_path(role):
    root=asset_directory()
    if root is None:return None
    manifest=verify_assets(root)
    if role not in manifest['files']:raise ValueError('Required outer-policy projection is unavailable: '+role)
    return root/manifest['files'][role]['path']


def preflight_outer_assets(*, produce_id, idol_card_id, weekly_provider_path):
    root=asset_directory()
    if root is None:return {'required':False,'source':'development-explicit-assets'}
    manifest=verify_assets(root)
    if produce_id not in manifest['produce_ids']:
        raise ValueError('Public outer assets do not cover this mode')
    expected=role_path('weekly_provider')
    if weekly_provider_path is None or Path(weekly_provider_path).resolve()!=expected.resolve():
        raise ValueError('Public weekly behavior provider must retain its qualified package identity')
    from .runtime_outer_behavior_prior import _load_provider
    _load_provider(expected)
    from .runtime_mode_profile import load_runtime_mode_profile
    profile=load_runtime_mode_profile(produce_id,master_dir=root/'Master')
    from .master_db import DEFAULT_DATABASE,get_idol_profile
    idol=get_idol_profile(idol_card_id,DEFAULT_DATABASE)
    if idol is None:
        raise ValueError('Selected idol is absent from the current public Master')
    from .portable_behavior_assets import read_behavior_prior,read_deck_library
    read_behavior_prior(role_path('behavior_count_prior'))
    read_deck_library(role_path('deck_plan_library'))
    return {'required':True,'manifest_sha256':RELEASE_MANIFEST_SHA256,'master_hash':MASTER_HASH,
        'produce_id':produce_id,'idol_card_id':idol_card_id,'mode_profile_schema':getattr(profile,'schema',None),
        'weekly_provider_sha256':manifest['files']['weekly_provider']['sha256'],
        'scope_coverage_required_for_each_decision':True,'input_submitted':False}
