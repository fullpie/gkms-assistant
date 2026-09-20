"""Source-level Python port of fullpie/GakumasDirectLauncher core.

Upstream commit 11c76742b8aacf1acdec0cc9e092a337547420dc.
DmmLogParser, Models, CacheStore and LauncherEnvironment map directly below;
MainForm's nonvisual launch/sync state machine is implemented by DirectLauncher.
No external launcher, DLL reflection, CLR, WinForms or PowerShell is required.
"""
from __future__ import annotations
from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import ntpath
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any
from .packages import SetupError
from .launcher_windows import WindowsPlatform, NativeError

UPSTREAM_COMMIT = '11c76742b8aacf1acdec0cc9e092a337547420dc'
ENTROPY = b'GakumasSmartLauncher:cache:v1'  # Must match the original DPAPI cache.
MAX_CACHE = 256 * 1024
TOKEN_FIELDS = ('ViewerId', 'OnetimeToken', 'OpenId', 'PfAccessToken')
RECORD_RE = re.compile(r'time="(?P<time>[^"]+)".*?Execute of::\s*gakumas\s+exe:\s*(?P<exe>.+?\.exe)\s+dir:(?P<dir>.*?)\s+arg:(?P<args>.*?)\s+admin:\s*(?P<admin>true|false)', re.I)
ARGUMENT_RE = re.compile(r'/(?P<name>viewer_id|onetime_token|open_id|pf_access_token)=(?P<value>[^\s"]+)', re.I)

class LauncherError(SetupError):
    """No raw DMM line, path containing arguments, token or OS error text."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def canonical(path):
    return ntpath.normcase(ntpath.normpath(str(path))) if path else ''

def parse_time(value):
    if not isinstance(value, str):
        raise ValueError('invalid timestamp')
    d = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d

@dataclass(repr=False)
class LaunchRecord:
    SchemaVersion: int
    CapturedAt: str
    ExecutablePath: str
    WorkingDirectory: str
    ViewerId: str
    OnetimeToken: str
    OpenId: str
    PfAccessToken: str
    RunAsAdministrator: bool
    SourceFingerprint: str

    def __repr__(self):
        return '<LaunchRecord: sensitive fields redacted>'

    def validate(self):
        if type(self.SchemaVersion) is not int or self.SchemaVersion != 1:
            raise LauncherError('CACHE_SCHEMA_UNSUPPORTED')
        path = self.ExecutablePath
        if (not isinstance(path, str) or len(path) > 32767 or not (ntpath.isabs(path) or Path(path).is_absolute())
            or ntpath.basename(path).casefold() != 'gakumas.exe'
            or any(ord(c) < 32 or c == '"' for c in path)):
            raise LauncherError('GAME_PATH_INVALID')
        if not isinstance(self.WorkingDirectory, str) or len(self.WorkingDirectory) > 32767:
            raise LauncherError('GAME_PATH_INVALID')
        if type(self.RunAsAdministrator) is not bool:
            raise LauncherError('CACHE_INVALID')
        for field in TOKEN_FIELDS:
            token = getattr(self, field)
            if (not isinstance(token, str) or not 1 <= len(token) <= 8192
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c in '\"\'' for c in token)):
                raise LauncherError('LAUNCH_DATA_INVALID')
        if not isinstance(self.SourceFingerprint, str) or not re.fullmatch('[0-9a-fA-F]{64}', self.SourceFingerprint):
            raise LauncherError('CACHE_INVALID')
        try:
            parse_time(self.CapturedAt)
        except (ValueError, TypeError):
            raise LauncherError('CACHE_INVALID') from None
        return self

    def arguments(self):
        self.validate()
        return (f'/viewer_id={self.ViewerId} /onetime_token={self.OnetimeToken} '
                f'/open_id={self.OpenId} /pf_access_token={self.PfAccessToken}')

    def _cache_json(self):
        # Private persistence only. Never return to UI/HTTP or a log.
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_json(cls, data):
        if not isinstance(data, dict):
            raise LauncherError('CACHE_INVALID')
        try:
            return cls(**{name: data[name] for name in cls.__dataclass_fields__}).validate()
        except (KeyError, TypeError):
            raise LauncherError('CACHE_INVALID') from None

def parse_log_line(line: str) -> LaunchRecord | None:
    if not isinstance(line, str) or len(line) > 1024*1024:
        return None
    if 'execute of::' not in line.lower():
        return None
    match = RECORD_RE.search(line)
    if not match:
        return None
    args = {m['name'].lower(): m['value'].strip() for m in ARGUMENT_RE.finditer(match['args'])}
    try:
        timestamp = parse_time(match['time']).astimezone(timezone.utc).isoformat(timespec='microseconds')
        clean = lambda value: value.strip().replace('\\\\', '\\').replace('\\"', '"')
        return LaunchRecord(1, timestamp, clean(match['exe']), clean(match['dir']),
            args['viewer_id'], args['onetime_token'], args['open_id'], args['pf_access_token'],
            match['admin'].lower() == 'true', hashlib.sha256(line.encode('utf-8')).hexdigest()).validate()
    except (KeyError, ValueError, LauncherError):
        return None

class CacheStore:
    """Read/write original C# property names and original CurrentUser entropy.

    No plaintext file or fallback encryption. Cache stays at the original path.
    Python immutable strings cannot be reliably erased from the process heap.
    """
    def __init__(self, path: Path, platform):
        self.path = path; self.platform = platform

    def load(self):
        if not self.path.is_file():
            return None
        try:
            if not 0 < self.path.stat().st_size <= MAX_CACHE:
                raise LauncherError('CACHE_INVALID')
            encrypted = self.path.read_bytes()
            decoded = self.platform.unprotect(encrypted, ENTROPY)
            if len(decoded) > MAX_CACHE:
                raise LauncherError('CACHE_INVALID')
            return LaunchRecord.from_json(json.loads(decoded.decode('utf-8-sig')))
        except LauncherError:
            raise
        except Exception:
            raise LauncherError('CACHE_UNREADABLE') from None

    def save(self, record):
        record.validate()
        temporary = None
        try:
            raw = json.dumps(record._cache_json(), ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            protected = self.platform.protect(raw, ENTROPY)
            if not protected or len(protected) > MAX_CACHE:
                raise LauncherError('CACHE_SAVE_FAILED')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='credentials.', suffix='.tmp', dir=self.path.parent)
            with os.fdopen(fd, 'wb') as f:
                f.write(protected); f.flush(); os.fsync(f.fileno())
            os.replace(temporary, self.path)
        except Exception:
            raise LauncherError('CACHE_SAVE_FAILED') from None
        finally:
            if temporary:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass

def detect_from_dmm_config(appdata: Path) -> dict | None:
    p = appdata/'dmmgameplayer5/dmmgame.cnf'
    if not p.is_file():
        return None
    try:
        if p.stat().st_size > 8*1024**2:
            raise LauncherError('CONFIG_UNREADABLE')
        conf = json.loads(p.read_text('utf-8-sig'))
        for item in conf.get('contents', []):
            if isinstance(item, dict) and str(item.get('productId','')).lower() == 'gakumas':
                detail = item.get('detail') or {}; folder = detail.get('path')
                if isinstance(folder, str) and folder:
                    return {'gamePath': str(Path(folder)/'gakumas.exe'), 'gameVersion': detail.get('version'),
                            'source': 'dmmgame.cnf'}
    except (OSError, ValueError, AttributeError):
        raise LauncherError('CONFIG_UNREADABLE') from None
    return None

class DirectLauncher:
    """Lives in the GUI host process. Owns launch monitoring independently of tabs."""
    def __init__(self, appdata=None, localappdata=None, platform=None, clock=time.monotonic,
                 monitor_thread=True, log_root=None):
        self.platform = platform or WindowsPlatform(); self.clock = clock
        self.appdata = Path(appdata) if appdata is not None else Path(os.environ.get('APPDATA', str(Path.home())))
        local = Path(localappdata) if localappdata is not None else Path(os.environ.get('LOCALAPPDATA', str(Path.home())))
        current = local/'GakumasDirectLauncher'; legacy = local/'GakumasSmartLauncher'
        cache_root = current if (current/'credentials.dat').is_file() else legacy if (legacy/'credentials.dat').is_file() else current
        self.cache = CacheStore(cache_root/'credentials.dat', self.platform)
        self.log_path = self.appdata/'dmmgameplayer5/logs/dll.log'
        self.log_root = Path(log_root) if log_root else None
        self.lock = threading.RLock(); self._stop = threading.Event(); self._thread = None
        self._enable_thread = monitor_thread; self._log_identity = None; self._latest_record = None
        self._phase = 'idle'; self._error = None; self._last_result = None; self._monitor = None
        self._requests = OrderedDict()

    def close(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        # Closing the GUI/host does NOT kill the game or DMM.

    def _audit(self, event):
        if not self.log_root:
            return
        # Store only enumerated event codes and UTC time. No raw exception/record.
        allowed = {'launch_requested','launch_already_running','process_stable','process_exited_early',
                   'cache_updated','no_new_data','launch_cancelled','operation_failed','dmm_open_requested'}
        if event not in allowed:
            return
        try:
            self.log_root.mkdir(parents=True, exist_ok=True)
            p = self.log_root/'launcher-events.jsonl'
            if p.exists() and p.stat().st_size > 1024*1024:
                p.unlink()
            with p.open('a', encoding='utf-8') as f:
                f.write(json.dumps({'event': event, 'time': datetime.now(timezone.utc).isoformat()})+'\n')
        except OSError:
            pass

    def latest_record(self):
        if not self.log_path.is_file():
            self._log_identity = None; self._latest_record = None
            return None
        try:
            stat = self.log_path.stat(); identity = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            if identity == self._log_identity:
                return self._latest_record
            record = None
            for line in self.platform.read_lines(self.log_path):
                parsed = parse_log_line(line)
                if parsed:
                    record = parsed  # Same as upstream: last complete valid launch line.
            # A changing log is re-read next time, never marked as an immutable snapshot.
            after = self.log_path.stat()
            if (after.st_mtime_ns, after.st_size, after.st_ino) == identity:
                self._log_identity = identity
            self._latest_record = record
            return record
        except Exception:
            self._log_identity = None
            raise LauncherError('LOG_UNREADABLE') from None

    def _records(self):
        errors = []; cached = latest = installation = None
        try:
            cached = self.cache.load()
        except LauncherError as e:
            errors.append(e.code)
        try:
            latest = self.latest_record()
        except LauncherError as e:
            errors.append(e.code)
        try:
            installation = detect_from_dmm_config(self.appdata)
        except LauncherError as e:
            errors.append(e.code)
        return cached, latest, installation, errors

    def _eligible(self, record):
        return record is not None and self.platform.exists(record.ExecutablePath)

    def _pick(self, cached, latest):
        # Mirror launch-time selection, not stale GUI cache priority from upstream.
        if self._eligible(latest) and (cached is None or latest.SourceFingerprint != cached.SourceFingerprint):
            return latest, 'dmm-log'
        if self._eligible(cached):
            return cached, 'encrypted-cache'
        if self._eligible(latest):
            return latest, 'dmm-log'
        return None, None

    def _safe_processes(self, name):
        try:
            return self.platform.processes(name), None
        except Exception:
            return None, 'PROCESS_STATUS_UNKNOWN'

    def _advance_monitor(self):
        if self._phase != 'monitoring' or not self._monitor:
            return
        found, error = self._safe_processes('gakumas.exe')
        if error:
            self._phase = 'unknown'; self._error = error
            return
        target = self._monitor['path']
        exact = [p for p in found if canonical(p.get('path')) == canonical(target)]
        opaque = [p for p in found if p.get('path') is None]
        elapsed = max(0, self.clock()-self._monitor['start'])
        if exact:
            self._monitor['seen'] = True
        elif opaque:
            self._phase = 'unknown'; self._error = 'PROCESS_PATH_UNVERIFIED'
            return
        elif self._monitor['seen'] or elapsed >= 3:
            self._phase = 'needs-refresh'; self._error = 'PROCESS_EXITED_EARLY'
            self._audit('process_exited_early')
            return
        if exact and elapsed >= 35:
            self._phase = 'process-stable'; self._error = None
            self._audit('process_stable')

    def _monitor_loop(self):
        while not self._stop.wait(.5):
            with self.lock:
                self._advance_monitor()
                if self._phase != 'monitoring':
                    return

    def snapshot(self, target_directory=''):
        with self.lock:
            supported = self.platform.supported
            if not supported:
                return {'schema':'gkms.launcher-status.v1','integrated':True,'platformSupported':False,
                        'state':'unsupported','errorCode':'WINDOWS_REQUIRED','gamePath':None,'gameVersion':None,
                        'gamePathExists':None,'gameRunning':None,'dmmRunning':None,'cachePresent':None,
                        'cacheReadable':None,'cacheCapturedAt':None,'latestLogRecordPresent':None,
                        'latestLogCapturedAt':None,'cacheMatchesLatestLog':None,'canLaunch':False,'monitor':None,
                        'upstreamCommit':UPSTREAM_COMMIT,'lastResult':self._last_result}
            self._advance_monitor()
            cached, latest, installation, errors = self._records()
            preferred, source = self._pick(cached, latest)
            displayed = preferred or cached or latest
            path = displayed.ExecutablePath if displayed else (installation or {}).get('gamePath')
            if not displayed and installation:
                source = 'dmmgame.cnf'
            path_exists = bool(path and self.platform.exists(path))
            version = None
            if installation and path and canonical(installation['gamePath']) == canonical(path):
                value = installation.get('gameVersion'); version = str(value)[:80] if value is not None else None
            game, ge = self._safe_processes('gakumas.exe'); dmm, de = self._safe_processes('DMMGamePlayer.exe')
            if ge or de:
                errors.append('PROCESS_STATUS_UNKNOWN')
            same_target = not target_directory or bool(path and canonical(ntpath.dirname(path)) == canonical(target_directory))
            state = ('game-running' if game else 'ready' if cached and path_exists else
                     'sync-available' if preferred else 'needs-refresh')
            if not same_target:
                state = 'path-mismatch'
            if self._phase != 'idle':
                state = self._phase
            if self._phase in ('process-stable','game-running') and game == []:
                self._phase = 'idle'; state = 'ready' if preferred else 'needs-refresh'
            remaining = None
            if self._monitor and self._phase == 'monitoring':
                remaining = max(0, 35-math.floor(max(0,self.clock()-self._monitor['start'])))
            monitor = {'duration':35,'remaining':remaining,'verified':'process-only'} if self._monitor else None
            can = (bool(preferred) and same_target and game is not None and not game
                   and self._phase not in ('monitoring','starting','syncing'))
            return {'schema':'gkms.launcher-status.v1','integrated':True,'platformSupported':True,
                    'state':state,'errorCode':self._error,'warnings':list(dict.fromkeys(errors)),
                    'gamePath':path,'gameDirectory':ntpath.dirname(path) if path else None,'gameVersion':version,
                    'gamePathSource':source,'gamePathExists':path_exists,'targetMatches':same_target,
                    'gameRunning':None if game is None else bool(game),'dmmRunning':None if dmm is None else bool(dmm),
                    'cachePresent':self.cache.path.is_file(),'cacheReadable':cached is not None,
                    'cacheCapturedAt':cached.CapturedAt if cached else None,
                    'latestLogRecordPresent':latest is not None,'latestLogCapturedAt':latest.CapturedAt if latest else None,
                    'cacheMatchesLatestLog':(cached.SourceFingerprint == latest.SourceFingerprint) if cached and latest else None,
                    'canLaunch':can,'monitor':monitor,'lastResult':self._last_result,'upstreamCommit':UPSTREAM_COMMIT}

    def _check_target(self, record, directory):
        if not directory or canonical(ntpath.dirname(record.ExecutablePath)) != canonical(directory):
            raise LauncherError('TARGET_PATH_MISMATCH')

    def _launch(self, directory):
        if self._phase == 'monitoring':
            raise LauncherError('LAUNCH_IN_PROGRESS')
        game, error = self._safe_processes('gakumas.exe')
        if error:
            raise LauncherError(error)
        expected = canonical(ntpath.join(directory, 'gakumas.exe'))
        if game:
            if not any(canonical(p.get('path')) == expected for p in game):
                raise LauncherError('PROCESS_PATH_UNVERIFIED')
            self._phase = 'game-running'; self._error = None; self._audit('launch_already_running')
            return {'ok':True,'alreadyRunning':True,'submitted':False,'resultCode':'ALREADY_RUNNING'}
        cached, latest, _, errors = self._records()
        record, _ = self._pick(cached, latest)
        if not record:
            raise LauncherError('NO_LAUNCH_DATA')
        # Reject BEFORE mutating cache, so a mismatched target never follows a newer log.
        self._check_target(record, directory); record.validate()
        if cached is None or record.SourceFingerprint != cached.SourceFingerprint:
            self.cache.save(record)
        self._phase = 'starting'; self._error = None
        try:
            directory = record.WorkingDirectory if record.WorkingDirectory and Path(record.WorkingDirectory).is_dir() else ntpath.dirname(record.ExecutablePath)
            pid = self.platform.launch(record.ExecutablePath, record.arguments(), directory, record.RunAsAdministrator)
        except NativeError as e:
            self._phase = 'cancelled' if e.code == 'UAC_CANCELLED' else 'needs-refresh'; self._error = e.code
            self._audit('launch_cancelled' if e.code == 'UAC_CANCELLED' else 'operation_failed')
            raise LauncherError(e.code) from None
        self._monitor = {'path':record.ExecutablePath,'pid':pid,'start':self.clock(),'seen':False}
        self._phase = 'monitoring'; self._audit('launch_requested')
        if self._enable_thread:
            self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name='gkms-game-monitor')
            self._thread.start()
        return {'ok':True,'alreadyRunning':False,'submitted':True,'resultCode':'LAUNCH_SUBMITTED'}

    def _sync(self, directory, close_dmm, force_close):
        if self._phase == 'monitoring':
            raise LauncherError('LAUNCH_IN_PROGRESS')
        latest = self.latest_record()
        if latest is None:
            raise LauncherError('NO_NEW_DATA')
        self._check_target(latest, directory)
        if not self._eligible(latest):
            raise LauncherError('GAME_PATH_MISSING')
        try:
            cached = self.cache.load()
        except LauncherError:
            cached = None
        if cached and cached.SourceFingerprint == latest.SourceFingerprint:
            self._phase = 'no-new-data'; self._error = None; self._audit('no_new_data')
            return {'ok':True,'updated':False,'resultCode':'NO_NEW_DATA','dmmClose':None}
        self.cache.save(latest)  # Failure never reaches CloseDmm.
        self._audit('cache_updated'); self._phase = 'sync-complete'; self._error = None
        closed = None; close_error = None
        if close_dmm:
            try:
                closed = self.platform.close_dmm(force=force_close)
            except Exception:
                close_error = 'DMM_CLOSE_UNCONFIRMED'
        return {'ok':True,'updated':True,'resultCode':close_error or
                ('SYNC_DMM_OPEN' if closed and closed.get('remaining') else 'SYNC_COMPLETE'), 'dmmClose':closed}

    def command(self, action, operation_id, directory='', close_dmm=False, force_close=False):
        if not self.platform.supported:
            raise LauncherError('WINDOWS_REQUIRED')
        if action not in ('launch','sync','repair'):
            raise LauncherError('UNSUPPORTED_ACTION')
        if not isinstance(operation_id, str) or not re.fullmatch('[A-Za-z0-9_-]{8,128}', operation_id):
            raise LauncherError('REQUEST_ID_REQUIRED')
        if type(close_dmm) is not bool or type(force_close) is not bool or (force_close and not close_dmm):
            raise LauncherError('INVALID_CLOSE_OPTIONS')
        key = (action, canonical(directory), close_dmm, force_close)
        with self.lock:
            if operation_id in self._requests:
                previous_key, receipt = self._requests[operation_id]
                if key != previous_key:
                    raise LauncherError('REQUEST_ID_REUSED')
                return {**receipt,'snapshot':self.snapshot(directory),'replayed':True}
            # Reserve an unknown result before any native side effect.
            self._requests[operation_id] = (key, {'ok':False,'errorCode':'RESULT_UNKNOWN','operationId':operation_id})
            try:
                with getattr(self.platform, 'action_lock', nullcontext)():
                    if action == 'launch':
                        result = self._launch(directory)
                    elif action == 'sync':
                        result = self._sync(directory, close_dmm, force_close)
                    else:
                        if self._phase == 'monitoring':
                            raise LauncherError('LAUNCH_IN_PROGRESS')
                        self.platform.repair(); self._audit('dmm_open_requested')
                        result = {'ok':True,'submitted':True,'resultCode':'DMM_OPEN_SUBMITTED'}
            except (LauncherError, NativeError) as e:
                result = {'ok':False,'errorCode':e.code}
                self._error = e.code
            except Exception:
                if self._phase == 'starting':self._phase = 'unknown'
                result = {'ok':False,'errorCode':'RESULT_UNKNOWN'}
                self._error = 'RESULT_UNKNOWN'
            result['operationId'] = operation_id; self._last_result = result.copy()
            self._requests[operation_id] = (key, result.copy())
            while len(self._requests) > 256:
                self._requests.popitem(last=False)
            return {**result,'snapshot':self.snapshot(directory)}

def game_running() -> bool:
    try:
        return bool(WindowsPlatform().processes('gakumas.exe'))
    except NativeError as e:
        raise SetupError(e.code) from None
