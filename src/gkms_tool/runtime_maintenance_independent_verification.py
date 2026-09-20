"""Append-only current-native qualification after a completed physical reload.

This does not prove bootstrap ancestry or rewrite the failed maintenance job.
Normal saved-run recovery still has to prove its persisted checkpoint.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import ctypes
from ctypes import wintypes as W
import hashlib
import math
import ntpath
import os
from pathlib import Path

from .runtime_session_continuity import (_JOB_ID, _HASH, _read, _native, _fail, _digest,
    _manifest_digest, _no_pending, checkpoint_signature, CHECKPOINT_SCHEMA)

SCHEMA = "gkms.native-maintenance-independent-verification.v1"
ELIGIBLE_ERROR = ("RuntimeError: game launch returned but native bridge was not verified: "
                  "New game process does not belong to this exact bootstrap lifetime")
FILETIME_EPOCH = 116444736000000000


def _same_path(left, right):
    return ntpath.normcase(ntpath.normpath(str(left))) == ntpath.normcase(ntpath.normpath(str(right)))


def _binary(path):
    path = Path(path).resolve()
    with path.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {"path": str(path), "sha256": digest}


def sources(response_path, bridge_root, expected=None):
    from .runtime_maintenance_late_verification import _job_paths
    job, paths = _job_paths(response_path, bridge_root)
    paths.pop('launch'); paths['bootstrap'] = job / 'bootstrap_launch.json'
    if expected is not None and set(expected) != set(paths):
        _fail('independent qualification source inventory differs')
    values, refs = {}, {}
    for name, path in paths.items():
        exp = None if expected is None else expected[name]
        if exp is not None and (not isinstance(exp,dict) or set(exp)!={'path','sha256'}
                or not isinstance(exp.get('sha256'),str) or not _HASH.fullmatch(exp['sha256'])
                or Path(exp.get('path', '')).resolve() != path):
            _fail('independent qualification source path changed')
        values[name], refs[name] = _read(path, expected=None if exp is None else exp.get('sha256'))
    response, request, operation, execution, bootstrap = (values[k] for k in
        ('response', 'request', 'operation', 'executor', 'bootstrap'))
    payload = request.get('payload') or {}
    finished = response.get('finished_at')
    if (response.get('schema') != 'gkms.native-maintenance.v1' or response.get('status') != 'failed'
            or response.get('error') != ELIGIBLE_ERROR or 'result' in response
            or response.get('request_id') != job.name or response.get('session_id') != job.parent.parent.name
            or type(finished) not in (int, float) or not math.isfinite(finished)
            or set(request) != {'command', 'payload'} or request['command'] != 'reload-dll'
            or execution.get('schema') != 'gkms.native-maintenance-executor.v1'
            or execution.get('operation') != 'reload-dll' or execution.get('game_started') is not True
            or execution.get('inspection_only') is not False or execution.get('uac_requested') is not False
            or execution.get('executor') != 'public-fixed-python-lifecycle'
            or operation.get('operation') != 'reload-dll' or operation.get('expected_pid') != payload.get('expected_game_pid')
            or not isinstance(operation.get('workspace'),str) or not Path(operation['workspace']).is_absolute()
            or operation.get('installed_sha256') != payload.get('expected_installed_sha256')
            or operation.get('candidate_sha256') != payload.get('candidate_sha256')
            or execution.get('installed_sha256') != payload.get('candidate_sha256')
            or Path(operation.get('job_directory', '')).resolve() != job
            or Path(operation.get('candidate_path', '')).resolve() != job / 'candidate.dll'
            or bootstrap.get('schema') != 'gkms.gakumas-direct-cached-bootstrap.v1'
            or bootstrap.get('status') != 'started' or bootstrap.get('already_running') is not False
            or bootstrap.get('nested_UAC_requested') is not False):
        _fail('independent qualification requires the original completed physical reload and exact verification failure')
    for key in ('candidate_sha256', 'expected_installed_sha256', 'expected_generation'):
        if not isinstance(payload.get(key), str) or not _HASH.fullmatch(payload[key]):
            _fail('independent reload identity is incomplete')
    if (job / 'launch.json').exists():
        _fail('independent bootstrap-only qualification cannot replace an existing launch receipt')
    return job, values, refs


def _process_identity(value, *, expected_pid, expected_path, bootstrap, finished):
    original = bootstrap.get('bootstrap_identity') or {}
    lower, upper = bootstrap.get('launch_filetime_lower'), bootstrap.get('launch_filetime_upper')
    if (type(expected_pid) is not int or expected_pid <= 0 or value.get('pid') != expected_pid
            or value.get('alive') is not True or value.get('exit_filetime') != 0
            or value.get('path_source') != 'queried' or not _same_path(value.get('path', ''), expected_path)
            or type(value.get('parent_pid')) is not int or value['parent_pid'] < 0
            or type(value.get('created_filetime')) is not int
            or type(lower) is not int or type(upper) is not int or lower > upper
            or type(original.get('created_filetime')) is not int
            or not lower <= original['created_filetime'] <= upper
            or not lower <= value['created_filetime'] <= FILETIME_EPOCH + int(finished * 10_000_000)
            or not _same_path(bootstrap.get('game_path', ''), expected_path)
            or not _same_path(original.get('path', ''), expected_path)
            or bootstrap.get('game_pid') != original.get('pid')):
        _fail('current native process lacks exact image and birth evidence inside the original launch window')


def validate_body(body, response_path, run, bridge_root):
    from .application_paths import native_bridge_path
    from .runtime_maintenance_late_verification import _status
    if not isinstance(body.get('maintenance_sources'),dict):
        _fail('independent qualification requires SHA-bound original sources')
    job, values, refs = sources(response_path, bridge_root, body['maintenance_sources'])
    payload = values['request']['payload']; result = body.get('result') or {}
    start, end = body.get('started_at'), body.get('verified_at')
    generation, pid = result.get('native_generation'), result.get('game_pid')
    original_root = Path(values['operation'].get('workspace', '')).resolve()
    installed = native_bridge_path(original_root).resolve()
    if (body.get('schema') != SCHEMA or body.get('qualification_kind') != 'independent-current-native'
            or body.get('recovery_scope') not in {'complete-checkpoint','final-presentation-only'}
            or body.get('run_id') != run.run_id or body.get('run_manifest_digest') != _manifest_digest(run)
            or payload.get('expected_run_id') != run.run_id or values['preflight'].get('run_id') != run.run_id
            or values['preflight'].get('pid') != payload.get('expected_game_pid')
            or values['preflight'].get('generation') != payload.get('expected_generation')
            or body.get('from_generation') != payload.get('expected_generation')
            or body.get('source_application_root') != str(original_root)
            or type(pid) is not int or pid <= 0 or pid == payload.get('expected_game_pid')
            or not isinstance(generation, str) or not _HASH.fullmatch(generation) or generation == payload.get('expected_generation')
            or result.get('expected_run_id') != run.run_id or result.get('installed_sha256') != payload['candidate_sha256']
            or result.get('gameplay_resumed') is not False or result.get('uac_requested_by_job') is not False
            or any(type(t) not in (int, float) or not math.isfinite(t) for t in (start, end))
            or not values['response']['finished_at'] <= start <= end
            or any(body.get(key) is not True for key in ('no_pending_before', 'no_pending_after', 'active_run_unchanged'))
            or any(body.get(key) is not False for key in ('launch_lineage_verified', 'original_response_rewritten',
                'game_input_submitted', 'gameplay_continuity_verified','training_admitted','cards_continuity_verified')) or body.get('original_job_status') != 'failed'
            or body.get('installed_binary') != {'path': str(installed), 'sha256': payload['candidate_sha256']}
            or body.get('staged_binary') != _binary(job / 'candidate.dll')
            or body['staged_binary']['sha256'] != payload['candidate_sha256']
            or body.get('backup_binary') != _binary(job / 'previous_bridge.dll')
            or body['backup_binary']['sha256'] != payload['expected_installed_sha256']):
        _fail('independent qualification does not bind the unchanged run and original reload bytes')
    before_identity, after_identity = body.get('process_before') or {}, body.get('process_after') or {}
    _process_identity(before_identity, expected_pid=pid, expected_path=installed.parents[2] / 'gakumas.exe',
        bootstrap=values['bootstrap'], finished=values['response']['finished_at'])
    if before_identity != after_identity or body.get('unique_process_pids_before') != [pid] or body.get('unique_process_pids_after') != [pid]:
        _fail('independent qualification process handle or unique native owner changed')
    for key, at in (('status_before', start), ('status_after', end)):
        _status(body.get(key) or {}, generation, pid, at)
    if body['status_before'].get('pc_version') != body['status_after'].get('pc_version'):
        _fail('independent native PC identity changed')
    native_ref, request_ref = body.get('native_source') or {}, body.get('native_request') or {}
    raw, ref = _native(native_ref.get('path', ''), generation, bridge_root, expected=native_ref.get('sha256'))
    request, req_ref = _read(request_ref.get('path', ''), within=Path(bridge_root) / 'requests', expected=request_ref.get('sha256'))
    response, _ = _read(ref['path'])
    from .runtime_session_continuity import _time
    if (ref != native_ref or req_ref != request_ref or request.get('command') != 'read_outer_snapshot'
            or set(request) != {'schema', 'request_id', 'session_generation', 'command'}
            or request.get('schema') != 'gkms.runtime-command.v1' or request.get('session_generation') != generation
            or not _JOB_ID.fullmatch(request.get('request_id', ''))
            or response.get('request_id') != request['request_id'] or Path(ref['path']).stem != request['request_id']
            or Path(req_ref['path']).stem != request['request_id'] or result.get('snapshot_path') != ref['path']
            or result.get('screen_type') != raw.get('screen_type') or raw.get('busy') is not False
            or not start - 1 <= _time(raw) <= end + 1):
        _fail('independent qualification lacks its fresh correlated read-only native snapshot')
    return result


@contextmanager
def held_process(pid):
    from .gui_setup.launcher_windows import _OwnedProcessWitness
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]; kernel.OpenProcess.restype = W.HANDLE
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        _fail('current native process cannot be held read-only')
    witness = _OwnedProcessWitness(kernel, handle, pid)
    try:
        yield witness
    finally:
        witness.close()


def qualify(response_path, *, expected_run_id, expected_game_pid, expected_generation,
            bridge_root, recovery_scope='complete-checkpoint', client=None, process_owner=None, process_inventory=None, clock=None):
    from .application_paths import app_root, native_bridge_path
    from .runtime_command_client import RuntimeCommandClient, _claim_lock
    from .run_identity import load_active_run
    from .runtime_session_continuity import verified_run_generation
    from .training_artifact_io import canonical_json_bytes
    from .gui_setup.launcher_windows import WindowsPlatform
    root = Path(bridge_root).resolve(); client = client or RuntimeCommandClient(root)
    clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
    process_inventory = process_inventory or (lambda: WindowsPlatform().processes('gakumas.exe'))
    def inventory():
        rows = process_inventory()
        if not isinstance(rows, list) or len(rows) != 1 or rows[0].get('pid') != expected_game_pid:
            _fail('independent qualification requires exactly one current game process')
        return [rows[0]['pid']]
    with _claim_lock():
        _no_pending(root)
        run = load_active_run(root=root.parent / 'runs')
        if run is None or run.run_id != expected_run_id or Path(client.root).resolve() != root:
            _fail('independent qualification run or mailbox differs')
        job, values, refs = sources(response_path, root)
        if Path(values['operation'].get('workspace', '')).resolve() != app_root():
            _fail('independent qualification requires the original application workspace')
        original_generation = verified_run_generation(root, run)
        if original_generation != values['request']['payload'].get('expected_generation'):
            _fail('independent qualification original generation differs')
        if list((job / 'late_native_verifications').glob('*.json')):
            _fail('this job already has a qualification; inspect its original receipt rather than issue another')
        installed = _binary(native_bridge_path()); staged = _binary(job / 'candidate.dll'); backup = _binary(job / 'previous_bridge.dll')
        payload=values['request']['payload']
        if (installed['sha256']!=payload['candidate_sha256'] or staged['sha256']!=payload['candidate_sha256']
                or backup['sha256']!=payload['expected_installed_sha256']):
            _fail('independent qualification installed/staged/backup bytes differ from the original job')
        with (process_owner or held_process)(expected_game_pid) as process:
            start = clock(); first = process.snapshot(); pids_before = inventory(); status_before = dict(client.read_status())
            from .runtime_maintenance_late_verification import _status
            _process_identity(first,expected_pid=expected_game_pid,expected_path=native_bridge_path().parents[2]/'gakumas.exe',
                bootstrap=values['bootstrap'],finished=values['response']['finished_at'])
            _status(status_before,expected_generation,expected_game_pid,start)
            # One correlated read; RuntimeCommandClient preserves any timeout.
            native = client.execute('read_outer_snapshot', timeout=15).require_ok()
            _, native_ref = _read(native.path, within=root / 'results')
            _, request_ref = _read(root / 'requests' / (native.request.request_id + '.json'))
            status_after = dict(client.read_status()); pids_after = inventory(); last = process.snapshot(); end = clock()
            _no_pending(root)
            if load_active_run(root=root.parent / 'runs') != run or _binary(installed['path']) != installed:
                _fail('independent qualification run or installed binary changed')
            result = {'game_pid': expected_game_pid, 'native_generation': expected_generation, 'expected_run_id': run.run_id,
                'installed_sha256': installed['sha256'], 'snapshot_path': native_ref['path'],
                'screen_type': (native.snapshot or {}).get('screen_type'), 'gameplay_resumed': False, 'uac_requested_by_job': False}
            body = {'schema': SCHEMA, 'qualification_kind': 'independent-current-native', 'original_job_status': 'failed',
                'recovery_scope':recovery_scope,
                'run_id': run.run_id, 'run_manifest_digest': _manifest_digest(run), 'from_generation': original_generation,
                'source_application_root': str(app_root()), 'maintenance_sources': refs, 'native_source': native_ref,
                'native_request': request_ref, 'result': result, 'status_before': status_before, 'status_after': status_after,
                'process_before': first, 'process_after': last, 'unique_process_pids_before': pids_before,
                'unique_process_pids_after': pids_after, 'installed_binary': installed, 'staged_binary': staged,
                'backup_binary': backup, 'started_at': start, 'verified_at': end, 'no_pending_before': True,
                'no_pending_after': True, 'active_run_unchanged': True, 'launch_lineage_verified': False,
                'original_response_rewritten': False, 'game_input_submitted': False, 'gameplay_continuity_verified': False}
            body.update(training_admitted=False,cards_continuity_verified=False)
            validate_body(body, response_path, run, root)
            if recovery_scope=='final-presentation-only':
                from .runtime_final_presentation_recovery import validate_preflight
                preflight,_=_native(values['preflight']['snapshot_path'],original_generation,root)
                validate_preflight(preflight,run)
            directory = job / 'late_native_verifications'; directory.mkdir(exist_ok=True)
            path = directory / (_digest(body) + '.json')
            with path.open('xb') as stream:
                stream.write(canonical_json_bytes(body)); stream.flush(); os.fsync(stream.fileno())
    return {'receipt': {'path': str(path), 'sha256': _digest(body)}, 'result': result,
            'launch_lineage_verified': False, 'gameplay_continuity_verified': False}


def authorization(body, response_path, run, before_generation, after_generation, bridge_root,
                  *, body_ref=None, expected_refs=None, checkpoint_schema=CHECKPOINT_SCHEMA):
    result = validate_body(body, response_path, run, bridge_root)
    job, values, refs = sources(response_path, bridge_root, body['maintenance_sources'])
    if body['from_generation'] != before_generation or result['native_generation'] != after_generation:
        _fail('independent qualification does not authorize this generation change')
    before, before_ref = _native(values['preflight']['snapshot_path'], before_generation, bridge_root)
    launched, launched_ref = _native(result['snapshot_path'], after_generation, bridge_root, expected=body['native_source']['sha256'])
    if (before.get('busy') is not False or values['preflight'].get('screen_type') != before.get('screen_type')
            or values['preflight'].get('state') != before.get('state')):
        _fail('independent original native preflight differs')
    refs.update(before=before_ref, launched=launched_ref)
    if body_ref is not None: refs['late_verification'] = body_ref
    if expected_refs is not None and refs != expected_refs:
        _fail('independent maintenance proof sources changed')
    from .runtime_session_continuity import _time
    final=body.get('recovery_scope')=='final-presentation-only'
    if final:
        from .runtime_final_presentation_recovery import validate_preflight
        signature=validate_preflight(before,run)
    else:signature=checkpoint_signature(before,run,schema=checkpoint_schema)
    return {'sources': refs, 'before_signature': signature,
        **({'recovery_scope':'final-presentation-only'} if final else {}),
        'launched_at': _time(launched), 'from_generation': before_generation, 'to_generation': after_generation,
        'job_id': job.name}
