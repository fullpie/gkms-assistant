"""Independent private source compatibility, never coupled to model activation.

BC rollback must remain usable when the RL checkpoint/report is absent,
disabled or corrupt. This small inference-only setting does not alter the
maintenance profile or its approved high-privilege lifecycle binding.
"""
from copy import deepcopy
import json
from pathlib import Path
import threading

from .application_paths import app_root, public_installation

SCHEMA = 'gkms.private-runtime-sources.v1'
RELATIVE_PATH = Path('var/devtools/private_runtime_sources.json')
_CACHE = {}
_LOCK = threading.RLock()


def _stamp(path):
    value=path.stat()
    return value.st_size,value.st_mtime_ns,value.st_ctime_ns


def private_runtime_io_reference(*, project_root=None, refresh=False):
    root=Path(project_root).resolve() if project_root is not None else app_root()
    if public_installation(root):return None
    path=root/RELATIVE_PATH
    if not path.exists():return None
    with _LOCK:
        stamp=_stamp(path)
        if not path.is_file() or not 0<stamp[0]<=65536:
            raise ValueError('Private runtime source configuration is not a bounded file')
        key=str(path.resolve());cached=_CACHE.get(key)
        if cached and not refresh and cached[0]==stamp:
            cached[2].validate_unchanged()
            return deepcopy(cached[1])
        raw=path.read_bytes()
        if _stamp(path)!=stamp:
            raise ValueError('Private runtime source configuration changed while reading')
        document=json.loads(raw)
        if type(document) is not dict or set(document)!={'schema','runtime_io_equivalence'} or document['schema']!=SCHEMA:
            raise ValueError('Explicit private source compatibility configuration required')
        reference=document['runtime_io_equivalence']
        if (type(reference) is not dict or not {'path','sha256'}<=set(reference)<={'path','sha256','bytes'}
                or type(reference['path']) is not str or not Path(reference['path']).is_absolute()
                or type(reference['sha256']) is not str or len(reference['sha256'])!=64):
            raise ValueError('Private source compatibility needs an absolute pinned receipt')
        from devtools.rl.import_qualified_io_equivalence import load_compatibility
        proof=load_compatibility(reference)
        if _stamp(path)!=stamp:
            raise ValueError('Private runtime source configuration changed across validation')
        _CACHE[key]=(stamp,deepcopy(reference),proof)
        return deepcopy(reference)


__all__=['private_runtime_io_reference','SCHEMA','RELATIVE_PATH']
