"""Verified display names for public selectors; never derive names from IDs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .application_paths import asset_directory

SCHEMA='gkms.portable-character-labels.v1'
MASTER_HASH='12b24ff515f7dcdc2aa107bdea1745fb2f6177cab1f6381ae709ef8b223d7aa8'
MODEL_MANIFEST_SHA256='e85b8eae35b917f04776ef7ea8c208d47e7a5558c38e71c5a0f9a256d1761923'
RELEASE_MANIFEST_SHA256='75233dca694d317ad0254b72326a488284cf31d28ef0858bb4ece1f0a9e41d91'


def load_character_labels(directory=None):
    root=Path(directory) if directory is not None else asset_directory('display_labels')
    if root is None:return None
    manifest=root/'manifest.json'
    if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size>16384:
        raise ValueError('Public character display labels are unavailable')
    raw=manifest.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=RELEASE_MANIFEST_SHA256:
        raise ValueError('Public character display label manifest differs')
    data=json.loads(raw)
    if (data.get('schema')!=SCHEMA or data.get('master_hash')!=MASTER_HASH
            or data.get('model_manifest_sha256')!=MODEL_MANIFEST_SHA256
            or data.get('labels',{}).get('path')!='characters.json'):
        raise ValueError('Public character display source differs')
    path=root/'characters.json'
    if path.is_symlink() or not path.is_file() or path.stat().st_size!=data['labels']['bytes']:
        raise ValueError('Public character name data is unavailable')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=data['labels']['sha256']:
        raise ValueError('Public character name data changed')
    labels=json.loads(raw)
    if (not isinstance(labels,dict) or len(labels)!=data['character_count']
            or any(not isinstance(key,str) or not key or not isinstance(name,str) or not name.strip() for key,name in labels.items())):
        raise ValueError('Public character name mapping is invalid')
    return labels
