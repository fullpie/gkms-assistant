"""GUI-only releases, background staging and atomic managed-version switching.

No game process, native input, model registry or source checkout is modified.
The host supplies the existing owner/transaction guard for apply and rollback.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import threading
from urllib.parse import urlsplit, urljoin
import uuid
import zipfile

from ..app_version import VERSION, PRODUCT, COMPONENT, CHANNEL, PLATFORM, REPOSITORY, info
from .packages import SetupError, relative_name
from .releases import https_get, PinnedHTTPS
from .transactions import atomic_write, checked_path, file_lock, is_link

STATUS_SCHEMA = "gkms.app-update-status.v1"
RELEASE_SCHEMA = "gkms.app-release.v1"
PACKAGE_SCHEMA = "gkms.app-package.v1"
POINTER_SCHEMA = "gkms.app-version-pointer.v1"
OP_SCHEMA = "gkms.app-update-operation.v1"
RELEASE_ASSET = "gkms-assistant-gui-release.json"
PACKAGE_MANIFEST = "gkms-app-package.json"
ENTRYPOINT = "gkms-assistant.exe"
API = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100"
MAX_ARCHIVE = 4 * 1024**3
MAX_EXPANDED = 8 * 1024**3
MAX_FILE = 2 * 1024**3
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")
_ID = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
_IDENTITY = {"product": PRODUCT, "component": COMPONENT, "channel": CHANNEL, "platform": PLATFORM}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _require(valid, message):
    if not valid:
        raise SetupError(message)


def _version(value):
    _require(isinstance(value, str) and len(value) <= 64 and _VERSION.fullmatch(value), "INVALID_GUI_VERSION")
    return tuple(map(int, value.split('.')))


def _hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _json(path, limit=4 * 1024**2):
    _require(Path(path).is_file() and Path(path).stat().st_size <= limit and not is_link(Path(path)), "INVALID_GUI_UPDATE_DOCUMENT")
    value = json.loads(Path(path).read_bytes())
    _require(isinstance(value, dict), "INVALID_GUI_UPDATE_DOCUMENT")
    return value


def _write(path, value):
    atomic_write(Path(path), json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode())


def _identity(document):
    _require(all(document.get(key) == value for key, value in _IDENTITY.items()), "NOT_A_STABLE_GUI_RELEASE")


def _asset_url(url, tag, name):
    expected = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
    _require(url == expected, "GUI_ASSET_SOURCE_MISMATCH")
    return url


def _download(url, destination, expected_size, progress):
    """Stream large packages with per-read timeout, never a total 20-second cap."""
    current = url
    allowed = {'github.com', 'objects.githubusercontent.com', 'release-assets.githubusercontent.com'}
    for _ in range(6):
        parsed = urlsplit(current)
        _require(parsed.scheme == 'https' and parsed.hostname in allowed and parsed.port in (None, 443)
                 and not parsed.username and not parsed.password and not parsed.fragment, "UNSAFE_GUI_REDIRECT")
        connection = PinnedHTTPS(parsed.hostname, timeout=90)
        try:
            target = parsed.path + ('?' + parsed.query if parsed.query else '')
            connection.request('GET', target, headers={'User-Agent': f'GKMS-Assistant/{VERSION}',
                'Accept': 'application/octet-stream', 'Accept-Encoding': 'identity'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                _require(bool(location), "GUI_REDIRECT_WITHOUT_LOCATION")
                current = urljoin(current, location)
                continue
            _require(response.status == 200, f"GUI_DOWNLOAD_HTTP_{response.status}")
            length = response.getheader('Content-Length')
            _require(length is None or int(length) == expected_size, "GUI_DOWNLOAD_SIZE_CHANGED")
            total = 0
            with Path(destination).open('xb') as output:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    _require(total <= expected_size <= MAX_ARCHIVE, "GUI_DOWNLOAD_TOO_LARGE")
                    output.write(block)
                    progress(total, expected_size)
                output.flush(); os.fsync(output.fileno())
            _require(total == expected_size, "GUI_DOWNLOAD_INCOMPLETE")
            return
        finally:
            connection.close()
    raise SetupError("TOO_MANY_GUI_REDIRECTS")


def discover_release(fetch=https_get):
    releases = json.loads(fetch(API, 4 * 1024**2))
    _require(isinstance(releases, list), "INVALID_GUI_RELEASE_LIST")
    eligible = []
    for release in releases:
        if not isinstance(release, dict) or release.get('draft') is not False or release.get('prerelease') is not False:
            continue
        tag = release.get('tag_name', '')
        if not isinstance(tag, str) or not tag.startswith('gui-v') or not _VERSION.fullmatch(tag[5:]):
            continue
        assets = release.get('assets')
        if not isinstance(assets, list):
            continue
        descriptors = [a for a in assets if isinstance(a, dict) and a.get('name') == RELEASE_ASSET and a.get('state') == 'uploaded']
        if len(descriptors) == 1:
            eligible.append((_version(tag[5:]), release, descriptors[0]))
    if not eligible:
        return None
    _, release, descriptor = max(eligible, key=lambda row: row[0])
    tag = release['tag_name']
    blob = fetch(_asset_url(descriptor.get('browser_download_url'), tag, RELEASE_ASSET), 1024**2, True)
    if descriptor.get('digest') is not None:
        _require(descriptor['digest'] == 'sha256:' + hashlib.sha256(blob).hexdigest(), "GUI_RELEASE_DESCRIPTOR_HASH_MISMATCH")
    manifest = json.loads(blob)
    _require(isinstance(manifest, dict) and manifest.get('schema') == RELEASE_SCHEMA, "INVALID_GUI_RELEASE_DESCRIPTOR")
    _identity(manifest)
    _require(manifest.get('version') == tag[5:], "GUI_RELEASE_VERSION_MISMATCH")
    package = manifest.get('package') or {}
    filename = f"gkms-assistant-gui-{manifest['version']}-{PLATFORM}.zip"
    _require(package.get('name') == filename and isinstance(package.get('sha256'), str)
             and _HASH.fullmatch(package['sha256']) and type(package.get('size')) is int
             and 0 < package['size'] <= MAX_ARCHIVE, "INVALID_GUI_PACKAGE_DESCRIPTOR")
    matches = [a for a in release['assets'] if isinstance(a, dict) and a.get('name') == filename and a.get('state') == 'uploaded']
    _require(len(matches) == 1, "GUI_PACKAGE_ASSET_AMBIGUOUS")
    asset = matches[0]
    _require(type(release.get('id')) is int and release['id'] > 0 and type(asset.get('id')) is int
             and asset['id'] > 0 and asset.get('size') == package['size'], "GUI_PACKAGE_ASSET_CHANGED")
    if asset.get('digest') is not None:
        _require(asset['digest'] == 'sha256:' + package['sha256'], "GUI_PACKAGE_DIGEST_MISMATCH")
    return {**_IDENTITY, 'version': manifest['version'], 'release_id': release['id'], 'asset_id': asset['id'],
        'tag': tag, 'package': deepcopy(package), 'descriptor_sha256': hashlib.sha256(blob).hexdigest(),
        'download_url': _asset_url(asset.get('browser_download_url'), tag, filename),
        'release_url': f'https://github.com/{REPOSITORY}/releases/tag/{tag}',
        'notes': str(release.get('body') or '')[:16000], 'published_at': release.get('published_at')}


def _package_manifest(value):
    _require(isinstance(value, dict) and value.get('schema') == PACKAGE_SCHEMA, "INVALID_GUI_PACKAGE_MANIFEST")
    _identity(value); _version(value.get('version'))
    _require(value.get('build_profile') == 'public' and value.get('development_tools') is False
             and value.get('entrypoint') == ENTRYPOINT, "GUI_PACKAGE_NOT_PUBLIC")
    files = value.get('files')
    _require(isinstance(files, dict) and 0 < len(files) <= 50000 and ENTRYPOINT in files, "INVALID_GUI_FILE_INVENTORY")
    seen = set()
    for name, digest in files.items():
        normalized = relative_name(name)
        parts = normalized.casefold().split('/')
        _require(normalized == name and name.casefold() not in seen and name != PACKAGE_MANIFEST
            and parts[0] not in {'var', 'userdata', 'user-data', 'state', 'updates', 'versions', '.git', '_research', '_archive'}
            and not any(part in {'credentials.dat', 'session.json', 'active_run.json'} for part in parts)
            and not ('gkms_' in parts[-1] and any(x in parts[-1] for x in ('probe', 'research', 'candidate')))
            and isinstance(digest, str) and _HASH.fullmatch(digest), "UNSAFE_GUI_FILE_INVENTORY")
        seen.add(name.casefold())
    return value


def _pe_entrypoint(path):
    with path.open('rb') as stream:
        header = stream.read(64)
        _require(len(header) == 64 and header[:2] == b'MZ', "GUI_ENTRYPOINT_NOT_X64_EXE")
        offset = struct.unpack_from('<I', header, 60)[0]
        _require(64 <= offset <= 1024**2, "GUI_ENTRYPOINT_NOT_X64_EXE")
        stream.seek(offset); pe = stream.read(24)
    _require(len(pe) == 24 and pe[:4] == b'PE\0\0' and struct.unpack_from('<H', pe, 4)[0] == 0x8664
             and struct.unpack_from('<H', pe, 22)[0] & 2
             and not struct.unpack_from('<H', pe, 22)[0] & 0x2000, "GUI_ENTRYPOINT_NOT_X64_EXE")


def verify_slot(slot):
    slot = Path(slot).resolve()
    manifest = _package_manifest(_json(checked_path(slot, PACKAGE_MANIFEST)))
    actual = set()
    for directory, children, filenames in os.walk(slot, followlinks=False):
        _require(not any(is_link(Path(directory) / name) for name in children), "UNSAFE_GUI_SLOT_LINK")
        actual.update((Path(directory) / name).relative_to(slot).as_posix() for name in filenames)
    _require(actual == set(manifest['files']) | {PACKAGE_MANIFEST}, "GUI_INSTALLED_INVENTORY_CHANGED")
    for name, digest in manifest['files'].items():
        path = checked_path(slot, name)
        _require(path.is_file() and _hash(path) == digest, "GUI_INSTALLED_FILE_CHANGED")
    _pe_entrypoint(checked_path(slot, ENTRYPOINT))
    return manifest


def _pointer(root):
    root = Path(root).resolve()
    value = _json(checked_path(root, 'active.json'))
    _require(value.get('schema') == POINTER_SCHEMA, "INVALID_GUI_VERSION_POINTER")
    _identity(value)
    for name in ('current', 'previous'):
        entry = value.get(name)
        if entry is None and name == 'previous':
            continue
        _require(isinstance(entry, dict) and isinstance(entry.get('slot'), str)
                 and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+-[a-f0-9]{12,64}', entry['slot'])
                 and isinstance(entry.get('manifest_sha256'), str) and _HASH.fullmatch(entry['manifest_sha256']), "INVALID_GUI_VERSION_SLOT")
        _version(entry.get('version'))
    return value


def _slot_record(root, slot):
    base = Path(root).resolve() / 'versions'
    path = Path(slot).resolve()
    _require(path.parent == base and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+-[a-f0-9]{12,64}', path.name), "GUI_SLOT_OUTSIDE_INSTALLATION")
    manifest = verify_slot(path)
    _require(path.name.startswith(manifest['version'] + '-'), "GUI_SLOT_VERSION_MISMATCH")
    return {'slot': path.name, 'version': manifest['version'], 'manifest_sha256': _hash(path / PACKAGE_MANIFEST)}


def initialize_managed_layout(app_root, initial_slot):
    """Packager/bootstrap first-install seam; never adopts an arbitrary checkout."""
    root = Path(app_root).resolve()
    with file_lock(root / '.update.lock'):
        _require(not (root / 'active.json').exists(), "GUI_INSTALLATION_ALREADY_INITIALIZED")
        current = _slot_record(root, initial_slot)
        _write(root / 'active.json', {'schema': POINTER_SCHEMA, **_IDENTITY, 'current': current, 'previous': None})
    return current


def resolve_active_entrypoint(app_root):
    """Used by the public bootstrap; returns a verified executable, never starts it."""
    root = Path(app_root).resolve(); pointer = _pointer(root); current = pointer['current']
    slot = root / 'versions' / current['slot']
    _require(_slot_record(root, slot) == current, "GUI_ACTIVE_SLOT_CHANGED")
    return checked_path(slot, ENTRYPOINT)


def validate_recovery_entrypoint(app_root, slot, expected_slot_id):
    """Verify the retained previous GUI without selecting it or starting it."""
    root = Path(app_root).resolve(); pointer = _pointer(root)
    previous = pointer.get('previous')
    _require(isinstance(previous, dict) and previous['slot'] == expected_slot_id,
             "GUI_RECOVERY_PREVIOUS_VERSION_UNAVAILABLE")
    previous_slot = root / 'versions' / previous['slot']
    _require(Path(slot).resolve() == previous_slot and _slot_record(root, previous_slot) == previous,
             "GUI_RECOVERY_PREVIOUS_VERSION_CHANGED")
    return checked_path(previous_slot, ENTRYPOINT)


def recovery_entrypoint(app_root):
    root = Path(app_root).resolve(); pointer = _pointer(root)
    previous = pointer.get('previous')
    _require(isinstance(previous, dict), "GUI_RECOVERY_PREVIOUS_VERSION_UNAVAILABLE")
    return validate_recovery_entrypoint(root, root / 'versions' / previous['slot'], previous['slot'])


class AppUpdateService:
    def __init__(self, app_root, state_dir, *, safety_check=None, fetch=https_get, download=_download):
        self.root, self.state_dir = Path(app_root).resolve(), Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        checked_path(self.state_dir, 'update-state.json')
        self._fetch, self._download, self._safety = fetch, download, safety_check
        self._lock = threading.RLock(); self._owner = threading.get_ident(); self._worker = None
        self._startup_started = False; self._closing = False; self._operations = {}
        self._state = {'status': 'unchecked', 'checked_at': None, 'release': None, 'error': None, 'staged': None,
                       'activation_warning': None, 'last_activation_operation_id': None}
        self._recovery_slot = os.environ.get('GKMS_GUI_RECOVERY_LAUNCH')
        if self._recovery_slot:
            validate_recovery_entrypoint(self.root, os.environ.get('GKMS_APP_ROOT', ''), self._recovery_slot)
        saved = self.state_dir / 'update-state.json'
        if saved.exists():
            prior = _json(saved)
            if prior.get('schema') == STATUS_SCHEMA:
                self._state['staged'] = prior.get('staged')
                self._state['activation_warning'] = prior.get('activation_warning')
                self._state['last_activation_operation_id'] = prior.get('last_activation_operation_id')
        operations = self.state_dir / 'operations'; operations.mkdir(exist_ok=True)
        for path in sorted(operations.glob('*.json'))[-512:]:
            value = _json(path)
            _require(value.get('schema') == OP_SCHEMA and value.get('operation_id') == path.stem, "INVALID_GUI_UPDATE_OPERATION")
            if value.get('status') == 'running':
                activation = value.get('activation')
                if isinstance(activation, dict):
                    try: actual = _pointer(self.root)
                    except (OSError, ValueError, SetupError): actual = None
                    if actual == activation.get('after') and actual is not None:
                        value.update(status='completed', phase='complete', error=None, activated=True,
                                     pointer_reconciled=True, warning='GUI_UPDATE_POINTER_RECONCILED', finished_at=_now())
                    else:
                        value.update(status='interrupted', activated=False if actual == activation.get('before') and actual is not None else None,
                                     pointer_reconciled=True, error='GUI_UPDATE_INTERRUPTED', finished_at=_now())
                else:
                    value.update(status='interrupted', error='GUI_UPDATE_INTERRUPTED', finished_at=_now())
                try: _write(path, value)
                except OSError:
                    if value.get('activated') is True: value['warning'] = 'GUI_ACTIVATED_RECEIPT_SAVE_WARNING'
            self._operations[path.stem] = value
        # The pre-activation operation document is durable before active.json.
        # Reconcile bookkeeping from that pointer; never replay a pointer write.
        try: actual = _pointer(self.root) if self._managed() else None
        except (OSError, ValueError, SetupError): actual = None
        activated = [row for row in self._operations.values() if actual is not None and row.get('activated') is True
                     and isinstance(row.get('activation'), dict) and row['activation'].get('after') == actual]
        if activated:
            latest = max(activated, key=lambda row: row.get('started_at', ''))
            if self._state['last_activation_operation_id'] != latest['operation_id']:
                self._state.update(staged=None, last_activation_operation_id=latest['operation_id'],
                                   activation_warning=latest.get('warning') or 'GUI_UPDATE_POINTER_RECONCILED')
                try: self._save()
                except OSError: self._state['activation_warning'] = 'GUI_ACTIVATED_STATE_SAVE_WARNING'
            elif latest.get('warning'):
                self._state['activation_warning'] = latest['warning']

    def _managed(self):
        return (self.root / 'active.json').is_file()

    def _check_safety(self):
        _require(callable(self._safety), "GUI_UPDATE_REQUIRES_OWNER_GUARD")
        permitted = self._safety()
        _require(permitted is None or permitted is True, "GUI_UPDATE_BLOCKED_BY_ACTIVE_WORK")

    def snapshot(self):
        with self._lock:
            active, previous, error = None, None, None
            if self._managed():
                try:
                    pointer = _pointer(self.root); active, previous = pointer['current'], pointer['previous']
                except Exception as failure:
                    error = str(failure)
            running = any(row['status'] == 'running' for row in self._operations.values())
            recovery = bool(self._recovery_slot and previous is not None and previous['slot'] == self._recovery_slot)
            return {'schema': STATUS_SCHEMA, 'application': info(), **deepcopy(self._state),
                'managed_installation': self._managed(), 'installation_error': error,
                'active_version': None if active is None else active['version'],
                'previous_version': None if previous is None else previous['version'],
                'restart_required': active is not None and active['version'] != VERSION,
                'busy': running, 'can_apply': self._managed() and error is None and self._state['staged'] is not None and not running and not recovery,
                'can_rollback': previous is not None and error is None and not running,
                'recovery_launch': recovery, 'recovery_target_version': previous['version'] if recovery else None,
                'recovery_notice': '目前開啟已驗證的前一版恢復介面，尚未切換版本；請按回退上一版完成恢復。' if recovery else None,
                'operations': deepcopy(self._operations), 'auto_install': False}

    def _save(self):
        _write(self.state_dir / 'update-state.json', {'schema': STATUS_SCHEMA, **self._state})

    def _operation(self, operation_id, **changes):
        with self._lock:
            self._operations[operation_id].update(changes)
            _write(self.state_dir / 'operations' / (operation_id + '.json'), self._operations[operation_id])

    def start_startup_check(self):
        with self._lock:
            if self._startup_started:
                return self.snapshot()
            self._startup_started = True
        return self.command('app-update-check', {'operationId': 'startup-' + uuid.uuid4().hex})

    def command(self, action, payload):
        if action == 'app-update-status':
            return self.snapshot()
        _require(threading.get_ident() == self._owner, "GUI_UPDATE_REQUIRES_OWNER")
        fields = {'app-update-check': {'operationId'}, 'app-update-download': {'operationId', 'release_id', 'version'},
                  'app-update-apply': {'operationId', 'staged_id'}, 'app-update-rollback': {'operationId'}}
        _require(action in fields and isinstance(payload, dict) and set(payload) == fields[action], "INVALID_GUI_UPDATE_REQUEST")
        operation_id = payload['operationId']
        _require(isinstance(operation_id, str) and _ID.fullmatch(operation_id), "GUI_UPDATE_OPERATION_ID_REQUIRED")
        with self._lock:
            if operation_id in self._operations:
                previous = self._operations[operation_id]
                _require(previous['action'] == action and previous['payload'] == payload, "GUI_UPDATE_OPERATION_ID_REUSED")
                return {'ok': True, 'operationId': operation_id, 'pending': previous['status'] == 'running',
                        'operation': deepcopy(previous), 'snapshot': self.snapshot(), 'replayed': True}
            _require(not self._closing and not any(row['status'] == 'running' for row in self._operations.values()), "GUI_UPDATE_BUSY")
            if action in ('app-update-apply', 'app-update-rollback'):
                _require(self._managed() and callable(self._safety), "GUI_UPDATE_REQUIRES_MANAGED_INSTALLATION")
                self._check_safety()
            self._operations[operation_id] = {'schema': OP_SCHEMA, 'operation_id': operation_id, 'action': action,
                'payload': deepcopy(payload), 'status': 'running', 'started_at': _now(), 'phase': 'starting',
                'bytes_received': 0, 'bytes_total': None, 'error': None}
            self._operation(operation_id)
            if action in ('app-update-check', 'app-update-download'):
                self._worker = threading.Thread(target=self._perform, args=(action, payload), name='gkms-gui-update', daemon=True)
                self._worker.start()
            else:
                self._perform(action, payload)
            return {'ok': True, 'operationId': operation_id, 'pending': self._operations[operation_id]['status'] == 'running',
                    'operation': deepcopy(self._operations[operation_id]), 'snapshot': self.snapshot()}

    def _perform(self, action, payload):
        operation_id = payload['operationId']
        try:
            if action == 'app-update-check':
                with self._lock: self._state.update(status='checking', error=None)
                release = discover_release(self._fetch)
                with self._lock:
                    self._state.update(release=release, checked_at=_now(), status='no-release' if release is None else
                        'available' if _version(release['version']) > _version(VERSION) else 'current', error=None)
                    self._save()
            elif action == 'app-update-download':
                self._stage(payload)
            else:
                self._switch(action, payload)
            self._operation(operation_id, status='completed', phase='complete', finished_at=_now())
        except Exception as error:
            if action in ('app-update-apply', 'app-update-rollback') and self._operations[operation_id].get('activated') is True:
                # The version pointer is authoritative. A later metadata write
                # failure must not tell the user to submit the activation again.
                with self._lock:
                    warning = self._state.get('activation_warning') or 'GUI_ACTIVATED_RECEIPT_SAVE_WARNING'
                    self._state.update(error=None, activation_warning=warning)
                    self._operations[operation_id].update(status='completed', phase='complete', error=None,
                        warning=warning, persistence_error=str(error), finished_at=_now())
                    try: self._save()
                    except OSError: pass
                    try: _write(self.state_dir / 'operations' / (operation_id + '.json'), self._operations[operation_id])
                    except OSError: pass
                return
            with self._lock:
                self._state.update(error=str(error))
                if action == 'app-update-check': self._state.update(status='failed', checked_at=_now())
                try:
                    self._save()
                except OSError:
                    self._state['error'] = 'GUI_UPDATE_STATE_SAVE_FAILED: ' + str(error)
            try:
                self._operation(operation_id, status='failed', phase='failed', error=self._state['error'], finished_at=_now())
            except OSError:
                # Keep an actionable terminal result in this process even if
                # storage failed. The durable running receipt becomes interrupted
                # on next startup; it is never automatically executed again.
                pass

    def _stage(self, payload):
        operation_id = payload['operationId']
        release = discover_release(self._fetch)
        _require(release is not None and release['release_id'] == payload['release_id'] and release['version'] == payload['version']
                 and _version(release['version']) > _version(VERSION), "GUI_RELEASE_CHANGED_OR_NOT_NEWER")
        package = release['package']; slot_id = release['version'] + '-' + package['sha256'][:16]
        slots = self.root / 'versions' if self._managed() else self.state_dir / 'staged'
        slots.mkdir(parents=True, exist_ok=True)
        work = checked_path(slots, '.work-' + operation_id); work.mkdir(exist_ok=False)
        archive = work / 'download.zip'
        _require(shutil.disk_usage(slots).free > package['size'] + 64 * 1024**2, "GUI_UPDATE_DISK_SPACE")
        self._operation(operation_id, phase='downloading', bytes_total=package['size'])
        self._download(release['download_url'], archive, package['size'],
            lambda received, total: self._operation(operation_id, bytes_received=received, bytes_total=total))
        _require(archive.stat().st_size == package['size'] and _hash(archive) == package['sha256'], "GUI_DOWNLOAD_HASH_MISMATCH")
        self._operation(operation_id, phase='verifying')
        extracted = work / 'application'; extracted.mkdir()
        with zipfile.ZipFile(archive) as package_zip:
            members = {}; total = 0; names = set()
            for member in package_zip.infolist():
                name = relative_name(member.orig_filename.rstrip('/')); mode = member.external_attr >> 16
                _require(name.casefold() not in names and not member.flag_bits & 1 and
                    stat.S_IFMT(mode) in (0, stat.S_IFREG, stat.S_IFDIR) and not stat.S_ISLNK(mode), "UNSAFE_GUI_ARCHIVE")
                names.add(name.casefold())
                if member.is_dir(): continue
                total += member.file_size
                _require(len(members) < 50000 and member.file_size <= MAX_FILE and total <= MAX_EXPANDED,
                         "GUI_ARCHIVE_LIMIT_EXCEEDED")
                members[name] = member
            _require(PACKAGE_MANIFEST in members and members[PACKAGE_MANIFEST].file_size <= 4 * 1024**2,
                     "GUI_PACKAGE_MANIFEST_MISSING")
            manifest_bytes = package_zip.read(members[PACKAGE_MANIFEST])
            manifest = _package_manifest(json.loads(manifest_bytes))
            _require(manifest['version'] == release['version'] and set(members) == set(manifest['files']) | {PACKAGE_MANIFEST},
                     "GUI_ARCHIVE_INVENTORY_MISMATCH")
            _require(shutil.disk_usage(slots).free > total + 64 * 1024**2, "GUI_UPDATE_DISK_SPACE")
            for name, member in members.items():
                target = checked_path(extracted, name); target.parent.mkdir(parents=True, exist_ok=True)
                with package_zip.open(member) as source, target.open('xb') as destination:
                    shutil.copyfileobj(source, destination, 1024 * 1024)
        verify_slot(extracted)
        target = slots / slot_id
        if target.exists():
            _require(verify_slot(target) == manifest, "GUI_EXISTING_SLOT_CONFLICT")
        else:
            os.rename(extracted, target)
        staged = {'id': slot_id, 'version': release['version'], 'manifest_sha256': _hash(target / PACKAGE_MANIFEST),
                  'package_sha256': package['sha256'], 'release_id': release['release_id'], 'managed': self._managed()}
        with self._lock:
            self._state.update(staged=staged, release=release, error=None); self._save()

    def _switch(self, action, payload):
        with file_lock(self.root / '.update.lock'):
            pointer = _pointer(self.root)
            if action == 'app-update-apply':
                _require(_slot_record(self.root, self.root / 'versions' / pointer['current']['slot']) == pointer['current'],
                         "GUI_CURRENT_SLOT_CHANGED")
                staged = self._state['staged']
                _require(isinstance(staged, dict) and staged.get('managed') is True and staged.get('id') == payload['staged_id'],
                         "GUI_STAGED_VERSION_UNAVAILABLE")
                selected = _slot_record(self.root, self.root / 'versions' / staged['id'])
                _require(selected['manifest_sha256'] == staged['manifest_sha256']
                         and _version(selected['version']) > _version(pointer['current']['version']), "GUI_STAGED_VERSION_CHANGED")
            else:
                selected = pointer.get('previous')
                _require(selected is not None and _slot_record(self.root, self.root / 'versions' / selected['slot']) == selected,
                         "GUI_ROLLBACK_VERSION_UNAVAILABLE")
            # Recheck after file validation and immediately before the only activation write.
            self._check_safety()
            after = {'schema': POINTER_SCHEMA, **_IDENTITY, 'current': selected, 'previous': pointer['current']}
            operation_id = payload['operationId']
            self._operation(operation_id, phase='activating', activation={'before': pointer, 'after': after}, activated=False)
            _write(self.root / 'active.json', after)
            self._operations[operation_id]['activated'] = True
            with self._lock:
                self._state.update(staged=None, error=None, activation_warning=None, last_activation_operation_id=operation_id)
                try: self._save()
                except OSError:
                    self._state['activation_warning'] = 'GUI_ACTIVATED_STATE_SAVE_WARNING'
                    raise

    def close(self):
        self._closing = True  # Existing background download is not replayed or promoted.


__all__ = ['AppUpdateService', 'discover_release', 'initialize_managed_layout', 'resolve_active_entrypoint',
           'recovery_entrypoint', 'validate_recovery_entrypoint']
