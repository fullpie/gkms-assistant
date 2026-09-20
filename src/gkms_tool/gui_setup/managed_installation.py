"""Managed setup operations composed with the existing GUI input owner.

Network and archive preparation may run off-thread. Game-directory mutations
are committed on the owner thread after checking the same revision and guards
again. Existing unmanaged files are never silently adopted or overwritten.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, Future
from copy import deepcopy
from pathlib import Path
import json
import io
import os
import re
import threading
import uuid
import zipfile

from . import releases
from .packages import SetupError, MAX_ARCHIVE, control_package, translation_package, digest
from .transactions import atomic_write, file_hash
from .transactions_v5 import InstallStore, canonical

READ_ACTIONS = frozenset({'status', 'detect', 'release', 'launcher-status'})
INSTALL_ACTIONS = frozenset({'choose-folder', 'choose-control', 'install-control',
                             'install-translation', 'restore-all', 'recover',
                             'preview-install-control', 'preview-install-translation',
                             'preview-restore-all', 'preview-recover'})
ACTIONS = INSTALL_ACTIONS


class DeferredSetupResult:
    """A single request whose completion remains owned by the GUI pump."""

    def __init__(self, future, finish, release):
        self.future, self.finish, self.release = future, finish, release
        self.owner = threading.get_ident()
        self.result = None
        self.pending = None

    def poll(self):
        if threading.get_ident() != self.owner:
            raise SetupError('Setup commit requires its original GUI owner.')
        if self.result is not None:
            return deepcopy(self.result)
        if self.pending is None and not self.future.done():
            return None
        try:
            if self.pending is None:
                value = self.finish(self.future.result())
                if callable(getattr(value, 'poll', None)):
                    self.pending = value
                else:
                    self.result = value
            if self.pending is not None:
                self.result = self.pending.poll()
                if self.result is None: return None
        except Exception as error:
            self.result = {'ok': False, 'error': str(error), 'committed': False,
                           'unchanged': False, 'errorCode': 'SETUP_OPERATION_FAILED'}
        finally:
            if self.result is not None: self.release()
        return deepcopy(self.result)


class ManagedInstallation:
    def __init__(self, service, *, data_dir=None, picker=None, running=None):
        self.service = service
        self.data = Path(data_dir or service.root / 'var/gui_setup')
        self.picker = picker or self._pick
        if running is None:
            from .launcher import game_running
            running = game_running
        self.running = running
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='gkms-setup-prepare')
        self._staged = {}
        self._operations = {}
        self._bundled = None
        from ..application_paths import native_state_root
        self.transaction_data = (native_state_root(service.root) / 'gui_setup') if service.public_build else self.data
        self._previews = {}
        self.elevation_factory = None

    def bundled_candidate(self):
        path = self.service.root / 'assets/control/gkms-control.zip'
        if not path.is_file():
            return None
        stat = path.stat()
        stamp = stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
        if self._bundled is not None and self._bundled[0] == stamp:
            return deepcopy(self._bundled[1])
        if not 0 < stat.st_size <= MAX_ARCHIVE:
            raise SetupError('BUNDLED_CONTROL_SIZE_INVALID')
        raw = path.read_bytes()
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if archive.getinfo('gkms-package.json').file_size > 65536:
                    raise SetupError('BUNDLED_CONTROL_MANIFEST_TOO_LARGE')
                specification = json.loads(archive.read('gkms-package.json'))
        except (zipfile.BadZipFile, KeyError, ValueError) as error:
            raise SetupError('BUNDLED_CONTROL_MANIFEST_INVALID') from error
        if (not isinstance(specification, dict) or not isinstance(specification.get('gameassembly_sha256'), str)
                or not re.fullmatch('[a-fA-F0-9]{64}', specification['gameassembly_sha256'])):
            raise SetupError('BUNDLED_CONTROL_GAME_IDENTITY_INVALID')
        package = control_package(raw, specification['gameassembly_sha256'])
        if package.metadata.get('buildProfile') != 'public':
            raise SetupError('BUNDLED_CONTROL_NOT_PUBLIC')
        sha = digest(raw)
        candidate_id = 'bundled-' + sha
        self._staged[candidate_id] = (path, sha)
        descriptor = {'stagedCandidateId': candidate_id, 'name': path.name, 'size': len(raw),
                      'sha256': sha, 'version': package.version, 'source': 'bundled-public-package',
                      'files': sorted(package.files), 'gameAssemblySha256': package.metadata['gameAssemblySha256']}
        self._bundled = (stamp, descriptor)
        return deepcopy(descriptor)

    def store(self, directory):
        if not isinstance(directory, str) or not directory.strip():
            raise SetupError('請先偵測或選擇遊戲資料夾。')
        return InstallStore(Path(directory), self.transaction_data / 'ledgers')

    def status(self, directory, *, completing=False):
        store = self.store(directory)
        result = store.snapshot()
        result.update(integration_mode='managed', project_frontend_ready=True,
                      managed_installation=True, native_execution_checked=False,
                      model_readiness_checked=False, platformSupported=os.name == 'nt',
                      readOnly=self.service.inspection)
        try:
            result['gameRunning'] = self.running()
        except Exception:
            result['gameRunning'] = None
        result['safeToModify'] = (result['safeToModify'] and result['gameRunning'] is False
                                  and not self.service.inspection and (completing or not self.service.lock.locked()))
        result['setupBusy'] = self.service.lock.locked() and not completing
        result['externalLoader'] = (store.root / 'version.dll').is_file() and not result['modules']['control']['installed']
        result['canPreview'] = (result['gameRunning'] is False and not self.service.inspection
                                and not self.service.closing and (completing or not self.service.lock.locked()))
        result['previewActions'] = (['preview-recover'] if result['operationPending'] else
            ['preview-install-control', 'preview-install-translation', 'preview-restore-all']) if result['canPreview'] else []
        version = store.root / 'gakumas-local/version.txt'
        if version.is_file() and version.stat().st_size < 2048:
            try:
                result['observedTranslationVersion'] = version.read_text('utf-8-sig').strip()
            except (OSError, UnicodeError):
                pass
        return result

    @staticmethod
    def _pick(kind):
        # Tk's existing owner invokes this; no shell or browser-supplied command.
        from tkinter import filedialog
        return (filedialog.askdirectory(title='選擇學園偶像大師遊戲資料夾') if kind == 'folder'
                else filedialog.askopenfilename(title='選擇正式操控安裝包', filetypes=[('ZIP', '*.zip')]))

    def choose(self, kind):
        selected = self.picker(kind)
        if not selected:
            return {'ok': True, 'cancelled': True}
        if kind == 'folder':
            store = self.store(str(selected))
            self.service._mutation_guard(self.service.root)
            self.service.remember_game_directory(str(store.root))
            return {'ok': True, 'gameDirectory': str(store.root), 'snapshot': self.status(str(store.root))}
        path = Path(selected)
        if path.suffix.lower() != '.zip' or not 0 < path.stat().st_size <= MAX_ARCHIVE:
            raise SetupError('請選擇大小合規的操控 ZIP 安裝包。')
        raw = path.read_bytes()
        if len(raw) > MAX_ARCHIVE:
            raise SetupError('安裝包超過大小限制。')
        candidate = uuid.uuid4().hex
        staged = self.data / 'staged' / (candidate + '.zip')
        atomic_write(staged, raw)
        self._staged[candidate] = (staged, digest(raw))
        return {'ok': True, 'stagedCandidateId': candidate, 'name': path.name,
                'size': len(raw), 'sha256': digest(raw)}

    def guard(self):
        if self.service.inspection or self.service.closing:
            raise SetupError('READ_ONLY' if self.service.inspection else 'ASSISTANT_CLOSING')
        self.service._mutation_guard(self.service.root)
        if self.running():
            raise SetupError('請先關閉遊戲，再安裝、更新或還原模組。')

    def file_guard(self):
        # The outer claim already excludes other app/helper mutations. Do not
        # issue a helper status RPC for every translation file.
        from .launcher_guard import local_blocker
        if self.service.inspection or self.service.closing or self.running():
            raise SetupError('Installer owner is closing, read-only, or the game started')
        reason = local_blocker(self.service.root)
        if reason: raise SetupError(reason)

    def _prepare(self, action, plan, store):
        if action != 'install-translation':
            candidate = (plan.get('candidate') or {}).get('stagedCandidateId')
            record = self._staged.get(candidate)
            if not record:
                raise SetupError('請先使用選檔按鈕選擇操控 ZIP。')
            path, sha = record
            raw = path.read_bytes()
            if digest(raw) != sha:
                raise SetupError('選取的安裝包已變更。')
            package = control_package(raw, file_hash(store.root / 'GameAssembly.dll'))
            if self.service.public_build and package.metadata.get('buildProfile') != 'public':
                raise SetupError('公開版需要正式可攜式 v2 操控包，不接受本機研究或舊版套件。')
            if 'gkms/native/gkms_runtime_exam_recorder.dll' not in package.files:
                raise SetupError('正式操控包缺少動作完成確認元件，不能作為 0.1 操控安裝。')
            if self.service.public_build:
                bundled = self.service.root / 'assets/control/gkms-control.zip'
                if not bundled.is_file() or file_hash(bundled) != sha:
                    raise SetupError('PUBLIC_CONTROL_SOURCE_NOT_CURRENT_SLOT')
            metadata = {'repository': 'reviewed-control-package', 'archiveSha256': sha}
            return self._archive(package, raw, metadata,
                'bundled-control' if self.service.public_build else 'local-control')
        if not (store.root / 'version.dll').is_file():
            raise SetupError('請先安裝操控模組，再安裝翻譯包。')
        source = plan.get('sourceUrl', '')
        release, asset, raw = releases.download_selected(source, plan.get('releaseId'), (plan.get('asset') or {}).get('id'))
        if release['tag'] != plan.get('version') or release['repository'] != plan.get('repository'):
            raise SetupError('Release 身分已變更，請重新確認。')
        metadata = {
            'repository': release['repository'], 'releaseId': release['id'], 'assetId': asset['id'],
            'digest': asset['digest'], 'archiveSha256': digest(raw), 'publishedAt': release['publishedAt'],
            'assetUpdatedAt': asset['updatedAt'], 'sourceUrl': source, 'releaseTag': release['tag']}
        return self._archive(translation_package(raw, release['tag']), raw, metadata, 'verified-translation')

    def _archive(self, package, raw, metadata, kind):
        sha = digest(raw)
        path = self.transaction_data / 'archives' / (sha + '.zip')
        if not path.exists(): atomic_write(path, raw)
        if file_hash(path) != sha: raise SetupError('Prepared archive changed')
        source = {'module': package.module, 'kind': kind, 'archiveSha256': sha, 'metadata': deepcopy(metadata)}
        return package, {**metadata, '_source_envelope': source}

    def _preview(self, action, plan, store, prepared=None):
        if action.startswith('install-'):
            package, metadata = prepared
            body = store.preview(package, metadata, plan['expectedRevision'])
            source = metadata['_source_envelope']
        elif action == 'restore-all':
            package, metadata, source = None, {}, None
            body = store.preview(None, {}, plan['expectedRevision'])
        else:
            package, metadata = None, {}
            body = store.recovery_preview(plan.get('direction', 'forward'))
            raw = store._pending_source().read_bytes()
            if digest(raw) != body['journalSha256']: raise SetupError('Recovery source changed while preparing preview')
            source = json.loads(raw).get('sourceEnvelope')
            saved = self.transaction_data / 'recovery_sources' / (body['journalSha256'] + '.json')
            if not saved.exists(): atomic_write(saved, raw)
            if file_hash(saved) != body['journalSha256']: raise SetupError('Reviewed recovery source changed')
        preview_id = uuid.uuid4().hex
        preview = {**body, 'previewId': preview_id, 'previewSha256': digest(canonical(body))}
        envelope = {'schema': 'gkms.persisted-install-preview.v1', 'preview': preview, 'source': source}
        if action == 'recover': envelope['recoverySourceSha256'] = body['journalSha256']
        atomic_write(self.transaction_data / 'previews' / (preview_id + '.json'), canonical(envelope))
        self._previews[preview_id] = (envelope, package, metadata)
        return {'ok': True, 'committed': False, 'confirmationRequired': True,
                'operationId': plan['operationId'], 'preview': preview}

    def _confirmed(self, action, plan, store):
        allowed = {'operationId', 'gameDirectory', 'expectedRevision', 'previewId', 'previewSha256', 'approveTakeover'}
        if action == 'recover': allowed |= {'transactionId', 'journalSha256', 'recoveryDirection'}
        if set(plan) - allowed: raise SetupError('UNEXPECTED_CONFIRMATION_FIELDS')
        preview_id = plan.get('previewId')
        if not isinstance(preview_id, str) or not re.fullmatch('[a-f0-9]{32}', preview_id):
            raise SetupError('A concrete prepared preview is required before confirmation')
        path = self.transaction_data / 'previews' / (preview_id + '.json')
        envelope = json.loads(path.read_bytes())
        preview = envelope['preview']
        body = {key: value for key, value in preview.items() if key not in ('previewId', 'previewSha256')}
        if (envelope.get('schema') != 'gkms.persisted-install-preview.v1' or preview['previewId'] != preview_id
                or digest(canonical(body)) != preview['previewSha256'] or plan.get('previewSha256') != preview['previewSha256']
                or body['action'] != action or body['gameDirectory'] != str(store.root)
                or body['expectedRevision'] != plan.get('expectedRevision')):
            raise SetupError('Confirmed preview identity/source/revision differs')
        if type(plan.get('approveTakeover')) is not bool:
            raise SetupError('EXPLICIT_TAKEOVER_CHOICE_REQUIRED')
        if body['requiresTakeoverConfirmation'] and not plan['approveTakeover']:
            raise SetupError('External files require explicit backup/takeover confirmation')
        for name in ('transactionId', 'journalSha256', 'recoveryDirection'):
            if name in plan and plan[name] != body.get(name): raise SetupError('Confirmed recovery scope differs')
        if action.startswith('install-'):
            source = envelope['source']; archive = self.transaction_data / 'archives' / (source['archiveSha256'] + '.zip')
            raw = archive.read_bytes()
            if digest(raw) != source['archiveSha256']: raise SetupError('Confirmed archive changed')
            package = (control_package(raw, file_hash(store.root / 'GameAssembly.dll')) if action == 'install-control' else
                translation_package(raw, source['metadata']['releaseTag']))
            metadata = {**source['metadata'], '_source_envelope': source}
        else:
            package, metadata = None, {}
        return preview, body, package, metadata

    def _commit(self, action, plan, store):
        preview, body, package, metadata = self._confirmed(action, plan, store)
        store.write_guard = self.file_guard
        try:
            with self.service._claim_factory():
                self.guard()
                if action.startswith('install-'):
                    store.install(package, metadata, plan['expectedRevision'], plan['operationId'],
                        preview=body, approve_takeover=plan['approveTakeover'])
                elif action == 'restore-all':
                    store.restore(plan['expectedRevision'], plan['operationId'], preview=body)
                else:
                    store.recover(preview=body, direction=body['recoveryDirection'])
        except PermissionError:
            if not store._pending_source().exists(): raise
            if self.elevation_factory is None:
                if not self.service.public_build: raise SetupError('A public helper is required to resume this protected installation')
                from .installation_elevation import begin_elevation
                factory = begin_elevation
            else:
                factory = self.elevation_factory
            return factory(root=self.service.root, data=self.transaction_data, store=store,
                operation_id=plan['operationId'], preview=preview, approve_takeover=plan['approveTakeover'], pool=self._pool)
        result = {'ok': True, 'committed': True, 'operationId': plan['operationId'],
                  'snapshot': self.status(str(store.root), completing=True)}
        if action == 'recover': result['transactionId'] = body['transactionId']
        return result

    def command(self, action, plan):
        if threading.get_ident() != self.service._owner_thread:
            raise SetupError('Setup commands require the GUI owner thread.')
        if self.service.inspection or self.service.closing:
            raise SetupError('READ_ONLY' if self.service.inspection else 'ASSISTANT_CLOSING')
        operation = plan.get('operationId')
        if not isinstance(operation, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', operation):
            raise SetupError('REQUEST_ID_REQUIRED')
        identity = json.dumps([action, plan], ensure_ascii=False, sort_keys=True, allow_nan=False)
        if operation in self._operations:
            previous, result = self._operations[operation]
            if previous != identity:
                raise SetupError('REQUEST_ID_REUSED')
            return result
        if not self.service.lock.acquire(blocking=False):
            raise SetupError('SETUP_OPERATION_ACTIVE')
        deferred = False
        try:
            if action in ('choose-folder', 'choose-control'):
                result = self.choose('folder' if action == 'choose-folder' else 'control')
            else:
                self.guard()
                store = self.store(plan.get('gameDirectory'))
                base_action = action.removeprefix('preview-')
                if base_action != 'recover' and store.snapshot()['revision'] != plan.get('expectedRevision'):
                    raise SetupError('安裝狀態已改變，請重新確認。')
                preview_only = action.startswith('preview-') or (self.service.public_build and not plan.get('previewId'))
                if preview_only:
                    self.service.remember_game_directory(str(store.root))
                    frozen = deepcopy(plan)
                    if base_action.startswith('install-'):
                        def finish_preview(prepared):
                            self.guard()
                            return self._preview(base_action, frozen, store, prepared)
                        result = DeferredSetupResult(self._pool.submit(self._prepare, base_action, frozen, store),
                            finish_preview, self.service.lock.release)
                        deferred = True
                    else:
                        result = self._preview(base_action, frozen, store)
                elif plan.get('previewId'):
                    result = self._commit(action, plan, store)
                    if callable(getattr(result, 'poll', None)):
                        pending = result
                        complete = Future(); complete.set_result(None)
                        result = DeferredSetupResult(complete, lambda _: pending, self.service.lock.release)
                        deferred = True
                elif action in ('install-control', 'install-translation'):
                    frozen = deepcopy(plan)
                    def finish(prepared):
                        self.guard()
                        with self.service._claim_factory():
                            self.guard()
                            package, metadata = prepared
                            if package.module == 'control' and package.metadata['gameAssemblySha256'] != file_hash(store.root / 'GameAssembly.dll'):
                                raise SetupError('準備安裝期間遊戲版本已改變，請重新確認操控包。')
                            store.install(package, metadata, frozen['expectedRevision'], operation)
                        return {'ok': True, 'committed': True, 'operationId': operation,
                                'snapshot': self.status(str(store.root), completing=True)}
                    result = DeferredSetupResult(self._pool.submit(self._prepare, action, frozen, store),
                                                 finish, self.service.lock.release)
                    deferred = True
                else:
                    with self.service._claim_factory():
                        self.guard()
                        if action == 'restore-all':
                            store.restore(plan['expectedRevision'], operation)
                        elif action == 'recover':
                            store.recover()
                        else:
                            raise SetupError('UNKNOWN_SETUP_ACTION')
                    result = {'ok': True, 'committed': True, 'operationId': operation,
                              'snapshot': self.status(str(store.root), completing=True)}
            self._operations[operation] = (identity, result)
            return result
        finally:
            if not deferred:
                self.service.lock.release()

    def close(self):
        self._pool.shutdown(wait=False, cancel_futures=True)
