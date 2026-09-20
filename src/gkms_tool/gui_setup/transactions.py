"""Managed-file installation and removal. No existing file content is saved."""
from __future__ import annotations
import contextlib, copy, hashlib, json, os, shutil, stat, tempfile, time, uuid
from pathlib import Path
from .packages import SetupError, Package, digest, relative_name

def is_link(path: Path) -> bool:
    try:
        s=path.lstat()
        return stat.S_ISLNK(s.st_mode) or bool(getattr(s,'st_file_attributes',0)&0x400)
    except FileNotFoundError: return False

def checked_path(root: Path, rel: str) -> Path:
    parts=relative_name(rel).split('/')
    # Verify every existing ancestor, including ancestors of the selected game root.
    for p in [root,*root.parents]:
        if is_link(p): raise SetupError('Symlink/junction/reparse point in installation root.')
    target=root
    for part in parts:
        if target.exists():
            if not target.is_dir():raise SetupError('A package parent is not a directory.')
            for other in target.iterdir():
                if other.name.casefold()==part.casefold() and other.name!=part:
                    raise SetupError('Case collision with an existing file.')
        target=target/part
        if is_link(target): raise SetupError('Symlink/junction/reparse point in installation path.')
    if target.exists() and not target.is_file(): raise SetupError('Target is not a regular file.')
    return target

def file_hash(path: Path) -> str|None:
    if not path.exists(): return None
    if is_link(path) or not path.is_file(): raise SetupError('Expected a regular file.')
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024**2),b''):h.update(chunk)
    return h.hexdigest()

def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.gkms-write-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as out:
            out.write(data); out.flush(); os.fsync(out.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)

def write_json(path: Path, value: dict) -> None:
    atomic_write(path,json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2).encode('utf-8'))

@contextlib.contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as f:
        f.seek(0); f.write(b'0'); f.flush();f.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as e: raise SetupError('Another installer process owns this target.') from e
        try: yield
        finally:
            f.seek(0)
            if os.name=='nt':msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(f,fcntl.LOCK_UN)

class InstallStore:
    """Install/update only owned files. Keep NEW payloads for forward recovery.

    Original or superseded file content is never saved. Unmanaged files are
    never overwritten, and uninstall deletes only unchanged managed files.
    A partial write is resumed, not rolled back, because old bytes are absent.
    """
    SCHEMA = 'gkms.install-ledger.v4'
    JOURNAL_SCHEMA = 'gkms.forward-install.v1'

    def __init__(self, root: Path, state_dir: Path, require_game: bool = True):
        self.root = Path(os.path.abspath(root))
        self.state_dir = Path(state_dir)
        if not self.root.is_dir():
            raise SetupError('Installation directory does not exist.')
        checked_path(self.root, 'gakumas.exe')
        if require_game:
            for name in ('gakumas.exe', 'GameAssembly.dll'):
                if not checked_path(self.root, name).is_file():
                    raise SetupError('Select the folder containing gakumas.exe and GameAssembly.dll.')
            if not (self.root / 'gakumas_Data').is_dir() or is_link(self.root / 'gakumas_Data'):
                raise SetupError('Missing or unsafe gakumas_Data directory.')
        key = hashlib.sha256(os.path.normcase(str(self.root)).encode()).hexdigest()
        self.db = self.state_dir / key
        self.db.mkdir(parents=True, exist_ok=True)
        for path in [self.db, *self.db.parents]:
            if is_link(path):
                raise SetupError('Unsafe installer state directory.')
        self.manifest_path = self.db / 'manifest.json'
        self.journal_path = self.db / 'pending.json'
        self.staging = self.db / 'pending-downloads'
        self.empty_revision = 'empty-' + key

    def manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {'schema': self.SCHEMA, 'root': str(self.root),
                    'revision': self.empty_revision, 'files': {}, 'modules': {}, 'directories': []}
        try:
            value = json.loads(self.manifest_path.read_text('utf-8'))
        except (ValueError, OSError) as error:
            raise SetupError('Installer ledger cannot be read. No file will be removed.') from error
        if value.get('root') != str(self.root):
            raise SetupError('Installer ledger root mismatch.')
        if value.get('schema') == 'gkms.install-ledger.v3':
            # Do not silently adopt a prior installation that replaced user files.
            # Older state files are left intact; the user may resolve them with v3.
            if self.journal_path.exists() or any(r.get('original') is not None for r in value['files'].values()):
                raise SetupError('Resolve the existing v3 installation with v3 before using v4. No files were changed.')
            value = copy.deepcopy(value)
            value['schema'] = self.SCHEMA
            for record in value['files'].values():
                record.pop('original', None)
        if value.get('schema') != self.SCHEMA:
            raise SetupError('Installer ledger schema mismatch.')
        return value

    def _stage(self, data: bytes) -> str:
        """Persist only a NEW package payload, never any existing target bytes."""
        self.staging.mkdir(exist_ok=True)
        sha = digest(data)
        path = checked_path(self.staging, sha + '.new')
        if not path.exists():
            atomic_write(path, data)
        if file_hash(path) != sha:
            raise SetupError('New package staging failed verification.')
        return sha

    def _payload(self, sha: str) -> bytes:
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
            raise SetupError('Invalid staged payload identity.')
        path = checked_path(self.staging, sha + '.new')
        if file_hash(path) != sha:
            raise SetupError('New package payload is missing or corrupt; operation cannot resume.')
        return path.read_bytes()

    def _clear_staging(self):
        if not self.staging.exists():
            return
        if is_link(self.staging):
            raise SetupError('Unsafe package staging directory.')
        for entry in self.staging.iterdir():
            # Only files created by _stage; do not recursively delete a directory.
            sha = entry.name[:-4]
            if not entry.name.endswith('.new') or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
                raise SetupError('Unexpected file in package staging directory.')
            path = checked_path(self.staging, entry.name)
            if not path.is_file():
                raise SetupError('Unexpected staging entry.')
            path.unlink()
        self.staging.rmdir()

    def _write_target(self, rel: str, data: bytes | None):
        path = checked_path(self.root, rel)
        if data is None:
            if path.exists():
                path.unlink()
        else:
            atomic_write(path, data)

    def _verify_managed(self, value: dict):
        for rel, record in value['files'].items():
            if file_hash(checked_path(self.root, rel)) != record['installed']:
                raise SetupError('Managed file changed externally; refusing overwrite/delete: ' + rel)

    def _prune_empty_dirs(self, directories):
        for rel in sorted(set(directories), key=lambda x: len(x.split('/')), reverse=True):
            relative_name(rel)
            path = self.root / rel
            for ancestor in [path, *path.parents]:
                if is_link(ancestor):
                    raise SetupError('Directory changed into a link.')
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    pass  # Preserve every untracked runtime/user file.

    def snapshot(self) -> dict:
        value = self.manifest()
        conflicts = []
        for rel, record in value['files'].items():
            try:
                if file_hash(checked_path(self.root, rel)) != record['installed']:
                    conflicts.append(rel)
            except SetupError:
                conflicts.append(rel)
        modules = {}
        for name in ('control', 'translation', 'launcher'):
            record = value['modules'].get(name)
            modules[name] = {'known': True, 'installed': bool(record), **(record or {})}
        pending = self.journal_path.exists()
        return {'schema': 'gkms.installer-status.v1', 'revision': value['revision'],
                'gameDirectory': str(self.root), 'safeToModify': not pending and not conflicts,
                'operationPending': pending,
                'capabilities': ['install_translation', 'install_control', 'restore_all'],
                'modules': modules, 'conflicts': conflicts,
                'files': [{'path': rel, 'action': 'added'} for rel in value['files']]}

    def install(self, package: Package, metadata: dict, revision: str, operation_id: str) -> dict:
        return self._transaction(package, metadata, revision, operation_id)

    def restore(self, revision: str, operation_id: str) -> dict:
        """Compatibility route name; v4 removes managed files, nothing else."""
        return self._transaction(None, {}, revision, operation_id)

    def _transaction(self, package, metadata, revision, operation_id):
        if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 100:
            raise SetupError('Invalid operation identity.')
        with file_lock(self.db / 'lock'):
            value = self.manifest()
            if self.journal_path.exists():
                raise SetupError('An incomplete operation must be resumed before starting another.')
            if value.get('operationId') == operation_id:
                return self.snapshot()
            if value['revision'] != revision:
                raise SetupError('Installation state changed. Refresh before continuing.')
            self._verify_managed(value)
            target = copy.deepcopy(value)
            desired = {}
            if package:
                for rel, data in package.files.items():
                    relative_name(rel)
                    old = value['files'].get(rel)
                    if old and old['module'] != package.module:
                        raise SetupError('File belongs to another managed module: ' + rel)
                    if not old and checked_path(self.root, rel).exists():
                        raise SetupError('An unmanaged file already exists; no overwrite is allowed: ' + rel)
                    if len(data) > 128 * 1024**2:
                        raise SetupError('File exceeds limit.')
                    desired[rel] = data
                for rel, old in list(target['files'].items()):
                    if old['module'] == package.module and rel not in desired:
                        desired[rel] = None
                        del target['files'][rel]
            else:
                desired = {rel: None for rel in value['files']}
                target['files'] = {}
                target['modules'] = {}
            needed = sum(len(data) for data in desired.values() if data is not None)
            if min(shutil.disk_usage(self.root).free, shutil.disk_usage(self.db).free) < needed * 2 + 16 * 1024**2:
                raise SetupError('Insufficient disk space for package staging.')
            self._clear_staging()
            changes, new_dirs = [], []
            for rel, data in desired.items():
                path = checked_path(self.root, rel)
                before = file_hash(path)
                expected = value['files'].get(rel, {}).get('installed')
                if before != expected:
                    raise SetupError('Target changed after preflight. No overwrite is allowed.')
                # Only new downloaded bytes go into staging. Existing files are hashed, not copied.
                after = self._stage(data) if data is not None else None
                changes.append({'path': rel, 'before': before, 'after': after})
                parent = path.parent
                while parent != self.root:
                    if not parent.exists():
                        new_dirs.append(parent.relative_to(self.root).as_posix())
                    parent = parent.parent
                if package and rel in package.files:
                    target['files'][rel] = {'module': package.module, 'installed': after}
            target['revision'] = uuid.uuid4().hex
            target['operationId'] = operation_id
            target['directories'] = sorted(set(value['directories'] + new_dirs))
            if package:
                target['modules'][package.module] = {**metadata, **package.metadata,
                    'version': package.version, 'installedAt': time.time()}
            journal = {'schema': self.JOURNAL_SCHEMA, 'old': value, 'target': target,
                       'changes': changes, 'remove_all': package is None}
            write_json(self.journal_path, journal)
            # On failure retain the journal and new payload for explicit forward recovery.
            self._finish_locked(journal)
            return self.snapshot()

    def recover(self):
        with file_lock(self.db / 'lock'):
            if not self.journal_path.exists():
                return self.snapshot()
            try:
                journal = json.loads(self.journal_path.read_text('utf-8'))
            except (ValueError, OSError) as error:
                raise SetupError('Incomplete operation record cannot be read.') from error
            self._finish_locked(journal)
            return self.snapshot()

    def _finish_locked(self, journal):
        if (journal.get('schema') != self.JOURNAL_SCHEMA or
            journal.get('old', {}).get('root') != str(self.root) or
            journal.get('target', {}).get('root') != str(self.root)):
            raise SetupError('Invalid or unsupported operation record. No file was changed.')
        current = self.manifest()
        target = journal['target']
        if current['revision'] not in (journal['old']['revision'], target['revision']):
            raise SetupError('Installation state differs from the incomplete operation.')
        # Check all paths and payloads before resuming any writes.
        for change in journal['changes']:
            actual = file_hash(checked_path(self.root, change['path']))
            if actual not in (change['before'], change['after']):
                raise SetupError('Resume stopped: externally modified file ' + change['path'])
            if actual != change['after'] and change['after'] is not None:
                self._payload(change['after'])
        for change in journal['changes']:
            actual = file_hash(checked_path(self.root, change['path']))
            if actual == change['after']:
                continue
            if actual != change['before']:
                raise SetupError('Target changed during installation.')
            data = self._payload(change['after']) if change['after'] is not None else None
            self._write_target(change['path'], data)
        for change in journal['changes']:
            if file_hash(checked_path(self.root, change['path'])) != change['after']:
                raise SetupError('Post-write verification failed.')
        self._verify_managed(target)
        write_json(self.manifest_path, target)
        self.journal_path.unlink()
        self._clear_staging()
        self._prune_empty_dirs(target['directories'])
