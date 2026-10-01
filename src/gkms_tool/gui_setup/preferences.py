"""Small persistent presentation preferences, independent of cultivation settings."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from .packages import SetupError
from .releases import source_url
from .transactions import atomic_write, file_lock, is_link

SCHEMA = 'gkms.setup-preferences.v1'
STORE_SCHEMA = 'gkms.setup-preferences-store.v1'
DEFAULTS = {'schema': SCHEMA, 'ui_locale': 'zh-Hant',
    'translation': {'enabled': False, 'release_url': 'https://github.com/chinosk6/GakumasTranslationData/releases'},
    'installation': {'ask_launch': False}}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def validate_patch(value):
    if not isinstance(value, dict) or not value or set(value)-{'ui_locale','translation','installation'}:
        raise SetupError('Unsupported setup preference fields')
    result = deepcopy(value)
    if 'ui_locale' in value and value['ui_locale'] not in ('zh-Hant','en','ja'):
        raise SetupError('Unsupported interface language')
    for family, keys in (('translation', {'enabled','release_url'}), ('installation', {'ask_launch'})):
        if family in value and (not isinstance(value[family],dict) or not value[family] or set(value[family])-keys):
            raise SetupError('Unsupported '+family+' preference fields')
    translation = value.get('translation',{})
    if 'enabled' in translation and type(translation['enabled']) is not bool:
        raise SetupError('Translation preference must be boolean')
    if 'release_url' in translation:
        result['translation']['release_url']=source_url(translation['release_url'])['raw']
    if 'ask_launch' in value.get('installation',{}) and type(value['installation']['ask_launch']) is not bool:
        raise SetupError('Launch confirmation preference must be boolean')
    return result


class SetupPreferences:
    def __init__(self, directory):
        self.directory=Path(directory)
        self.path=self.directory/'setup-preferences.json'

    def _safe(self):
        for path in (self.directory,*self.directory.parents,self.path):
            if is_link(path):raise SetupError('Unsafe setup preference path')

    def _load(self):
        self._safe()
        if not self.path.exists():
            return {'schema':STORE_SCHEMA,'preferences':deepcopy(DEFAULTS),'operations':{}}
        if not self.path.is_file() or self.path.stat().st_size>2*1024*1024:
            raise SetupError('Setup preference file is invalid or too large')
        data=json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(data,dict) or data.get('schema')!=STORE_SCHEMA or set(data)-{'schema','preferences','operations'}:
            raise SetupError('Unknown setup preference format')
        preferences=data.get('preferences')
        if not isinstance(preferences,dict) or preferences.get('schema')!=SCHEMA or set(preferences)!=set(DEFAULTS):
            raise SetupError('Incomplete setup preferences')
        if any(not isinstance(preferences.get(family),dict) or set(preferences[family])!=set(DEFAULTS[family])
               for family in ('translation','installation')):
            raise SetupError('Incomplete setup preference group')
        validate_patch({key:value for key,value in preferences.items() if key!='schema'})
        if not isinstance(data.get('operations'),dict) or len(data['operations'])>512:
            raise SetupError('Invalid setup preference receipts')
        return data

    def read(self, *, read_only=False):
        preferences=self._load()['preferences']
        return {'ok':True,'schema':'gkms.setup-preferences-status.v1','readOnly':bool(read_only),
            'preferences':deepcopy(preferences),'revision':hashlib.sha256(_canonical(preferences)).hexdigest()}

    def save(self, operation_id, patch):
        if not isinstance(operation_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',operation_id):
            raise SetupError('Setup preference operationId is required')
        patch=validate_patch(patch);identity=hashlib.sha256(_canonical(patch)).hexdigest()
        self._safe()
        with file_lock(self.directory/'setup-preferences.lock'):
            data=self._load();previous=data['operations'].get(operation_id)
            if previous is not None:
                if previous!=identity:raise SetupError('Preference operationId cannot change its payload')
                return {'ok':True,'saved':True,'operationId':operation_id,'replayed':True,'snapshot':self.read()}
            current=deepcopy(data['preferences'])
            for key,value in patch.items():
                if isinstance(value,dict):current[key].update(value)
                else:current[key]=value
            operations={**data['operations'],operation_id:identity}
            # This is a bounded receipt cache for reversible local preferences;
            # native transactions use their own permanent settlement records.
            operations=dict(list(operations.items())[-512:])
            atomic_write(self.path,_canonical({'schema':STORE_SCHEMA,'preferences':current,'operations':operations}))
        return {'ok':True,'saved':True,'operationId':operation_id,'snapshot':self.read()}
