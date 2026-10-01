"""Loopback-only GUI host. Uses project Python + existing Tk callbacks.

Starting the host does not start a run. Controls require an explicit UI command.
A Tk-thread queue owns all callback execution; worker threads only use JSON.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import json
import os
import queue
import secrets
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .adapter import COMMANDS, PUBLIC_COMMANDS, CONTROL_CALLBACKS, SETUP_CALLBACKS, GkmsAdapter, check_source
from ..application_paths import app_root, public_installation, state_root, native_state_root

WEB_ROOT = Path(__file__).resolve().parent / 'web'
DEFAULT_PROJECT_ROOT = app_root()
BODY_LIMIT = 65536
COMMAND_WAIT_SECONDS = 16


class InstanceLock:
    """Prevent this launcher from creating two GkmsApp owners for one project."""
    def __init__(self, root: Path) -> None:
        # A repository GUI and a public version may share the same installed
        # game. GUI record overrides must not create a second input owner.
        identity = native_state_root(root)
        digest = hashlib.sha256(str(identity).casefold().encode()).hexdigest()[:24]
        self.path = Path(tempfile.gettempdir()) / f'gkms-glass-{digest}.lock'
        self.stream = None

    def __enter__(self):
        self.stream = self.path.open('a+b')
        try:
            # Windows byte-range locks also reject reads through another
            # descriptor. Inspect file size without reading the locked byte.
            if os.fstat(self.stream.fileno()).st_size == 0:
                self.stream.write(b'0')
                self.stream.flush()
            self.stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.stream.close()
            raise RuntimeError('這個專案已有一份新 GUI 接線程式。') from error
        return self

    def __exit__(self, *args):
        if self.stream:
            try:
                if os.name == 'nt':
                    import msvcrt
                    self.stream.seek(0)
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self.stream.close()


@dataclass
class Job:
    payload: dict[str, Any]
    deadline: float = field(default_factory=lambda: time.monotonic() + 12)
    event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None


class Shared:
    def __init__(self, *, read_only: bool = False, public_build=False) -> None:
        self.lock = threading.Lock()
        self.snapshot: dict[str, Any] = {}
        self.jobs: queue.Queue[Job] = queue.Queue(maxsize=64)
        self.known: OrderedDict[str, Job] = OrderedDict()
        self.token = secrets.token_urlsafe(32)
        self.read_only = read_only
        self.allowed_commands = PUBLIC_COMMANDS if public_build else COMMANDS
        self.closed = False

    def publish(self, snapshot: dict[str, Any]) -> None:
        if not isinstance(snapshot, dict):
            raise ValueError('snapshot 必須為 object。')
        json.dumps(snapshot, allow_nan=False)
        with self.lock:
            self.snapshot = copy.deepcopy(snapshot)

    def close(self) -> None:
        """Wake queued HTTP callers without executing work after GUI shutdown."""
        with self.lock:
            self.closed = True
            while True:
                try:
                    job = self.jobs.get_nowait()
                except queue.Empty:
                    break
                job.result = {'ok': False, 'error': 'GUI 已關閉；排程未執行。'}
                job.event.set()
                self.jobs.task_done()

    def submit(self, payload: dict[str, Any]) -> Job:
        if not isinstance(payload, dict):
            raise ValueError('命令必須為 object。')
        request_id = payload.get('request_id')
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 100:
            raise ValueError('request_id 不合法。')
        callback = payload.get('callback')
        if not isinstance(callback, str) or callback not in self.allowed_commands:
            raise ValueError('未知接口。')
        if not isinstance(payload.get('values', {}), dict):
            raise ValueError('values 必須為 object。')
        if self.read_only and callback in CONTROL_CALLBACKS:
            raise PermissionError('唯讀模式不接受遊戲控制命令。')
        with self.lock:
            if self.closed:
                raise RuntimeError('GUI 正在關閉，不接受新增命令。')
            if request_id in self.known:
                known = self.known[request_id]
                if known.payload != payload:
                    raise ValueError('同一 request_id 不可更換內容。')
                return known
            while len(self.known) >= 1024:
                # Never evict in-flight idempotency records.
                first_id, first = next(iter(self.known.items()))
                if not first.event.is_set():
                    raise RuntimeError('命令歷史暫滿，請稍候。')
                del self.known[first_id]
            job = Job(copy.deepcopy(payload))
            self.jobs.put_nowait(job)
            self.known[request_id] = job
            return job


def make_handler(shared: Shared, setup_service=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'GKMSLocalGUI/1'
        def log_message(self, *_args):
            pass  # Do not log auth material or local file metadata.

        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def _single_header(self, name):
            values = self.headers.get_all(name, [])
            return values[0] if len(values) == 1 else None

        def _origin(self) -> str:
            return f'http://127.0.0.1:{self.server.server_port}'

        def _valid_host(self) -> bool:
            return self._single_header('Host') == f'127.0.0.1:{self.server.server_port}'

        def _authorized(self, *, write: bool = False) -> bool:
            if not self._valid_host():
                return False
            token = self._single_header('X-GKMS-Token')
            if not isinstance(token, str) or not hmac.compare_digest(token.encode('utf-8'), shared.token.encode('ascii')):
                return False
            if len(self.headers.get_all('Origin', [])) > 1:
                return False
            origin = self._single_header('Origin')
            if write and origin != self._origin():
                return False
            if origin and origin != self._origin():
                return False
            return True

        def _path(self):
            try:
                parts = urlsplit(self.path)
            except ValueError:
                return None
            if not self.path.startswith('/') or parts.scheme or parts.netloc:
                return None
            return parts.path

        def _send(self, code: int, content: bytes, content_type: str):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'")
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, code: int, value: Any):
            self._send(code, json.dumps(value, ensure_ascii=False, allow_nan=False).encode(), 'application/json; charset=utf-8')

        def do_GET(self):
            if not self._valid_host():
                self._json(403, {'error': 'host rejected'}); return
            path = self._path()
            if path == '/api/snapshot':
                if not self._authorized():
                    self._json(403, {'error': 'unauthorized'}); return
                with shared.lock:
                    snapshot = copy.deepcopy(shared.snapshot)
                    snapshot['control_requests'] = {
                        'pending_ids':[key for key,job in shared.known.items() if not job.event.is_set()],
                        'completed':{key:copy.deepcopy(job.result) for key,job in shared.known.items()
                                     if job.event.is_set()},
                    }
                self._json(200, snapshot); return
            addon = getattr(setup_service, 'developer', None)
            resources = (getattr(addon, '_gkms_resources', {})
                         if not getattr(setup_service, 'public_build', True) else {})
            if path in resources:
                source, expected_sha = resources[path]
                try:
                    content = source.read_bytes()
                    if hashlib.sha256(content).hexdigest() != expected_sha:
                        raise ValueError('development resource changed')
                except (OSError, ValueError):
                    self._json(409, {'error': 'development resource unavailable'}); return
                self._send(200, content, 'text/javascript; charset=utf-8'); return
            allowed = {'/': ('index.html', 'text/html'), '/index.html': ('index.html', 'text/html'),
                       '/app.css': ('app.css', 'text/css'), '/app.js': ('app.js', 'text/javascript'),
                       '/host.js': ('host.js', 'text/javascript'), '/release-engine.js': ('release-engine.js', 'text/javascript'),
                       '/i18n.js': ('i18n.js', 'text/javascript'), '/project-state.js': ('project-state.js', 'text/javascript')}
            allowed['/loadout-panel.js'] = ('loadout-panel.js', 'text/javascript')
            if path == '/favicon.ico':
                self._send(204, b'', 'image/x-icon'); return
            if path not in allowed:
                self._json(404, {'error': 'not found'}); return
            filename, kind = allowed[path]
            try:
                content = (WEB_ROOT / filename).read_bytes()
            except OSError:
                self._json(404, {'error': 'asset unavailable'}); return
            self._send(200, content, kind + '; charset=utf-8')

        def do_POST(self):
            path = self._path() or ''
            setup = path.startswith('/setup-api/') and setup_service is not None
            if (path != '/api/command' and not setup) or not self._authorized(write=True):
                self._json(403, {'ok': False, 'error': 'unauthorized'}); return
            if (self._single_header('Content-Type') or '').split(';')[0].strip().lower() != 'application/json':
                self._json(415, {'ok': False, 'error': 'JSON required'}); return
            try:
                if self.headers.get('Transfer-Encoding') is not None:
                    raise ValueError('transfer encoding is not supported')
                length = int(self._single_header('Content-Length') or '-1')
                if length < 1 or length > BODY_LIMIT:
                    raise ValueError('invalid content length')
                content = self.rfile.read(length)
                if len(content) != length:
                    raise ValueError('incomplete request body')
                def invalid_constant(value):
                    raise ValueError('non-finite JSON number')
                payload = json.loads(content, parse_constant=invalid_constant)
                if not isinstance(payload, dict):
                    raise ValueError('object required')
                if setup:
                    action = path.removeprefix('/setup-api/')
                    if action in SETUP_CALLBACKS | getattr(setup_service, 'developer_actions', frozenset()):
                        # A launcher write is queued on the same owner as game
                        # controls, retaining v12's operation ID for idempotency.
                        payload = {'request_id':payload.get('operationId'), 'callback':action, 'values':payload}
                    elif action not in ('status', 'detect', 'launcher-status', 'app-update-status', 'setup-preferences') and not (
                            action == 'release' and getattr(setup_service, 'enable_installation', False)):
                        # This project uses its existing installation/launcher.
                        # A public installer needs a separate verified package;
                        # it must not overwrite an unmanaged local game DLL.
                        self._json(409, {'ok':False,'error':'目前使用既有專案安裝；尚未開放覆寫未納管的 DLL。',
                            'errorCode':'existing-project-maintenance-required','unchanged':True}); return
                    else:
                        try:
                            reply = setup_service.command(action, payload)
                        except Exception:
                            self._json(409, {'ok':False,'error':'無法讀取專案安裝狀態，請查看本機設定。'}); return
                        self._json(200, reply); return
                job = shared.submit(payload)
            except PermissionError as error:
                self._json(403, {'ok': False, 'error': str(error)}); return
            except queue.Full:
                self._json(429, {'ok': False, 'error': '命令佇列已滿，請稍候。'}); return
            except RuntimeError as error:
                self._json(503, {'ok': False, 'error': str(error)}); return
            except (ValueError, TypeError, OSError, RecursionError) as error:
                self._json(400, {'ok': False, 'error': str(error)}); return
            if not job.event.wait(COMMAND_WAIT_SECONDS):
                self._json(202, {'ok': False, 'pending': True,
                                 'request_id': payload['request_id'],
                                 'error': '尚未取得 callback 回覆；請看狀態，重試必須沿用相同 request_id。'}); return
            self._json(200 if job.result and job.result.get('ok') else 409, job.result)
    return Handler


class OwnerPump:
    """Only the existing Tk owner executes callbacks and reads the adapter."""
    def __init__(self, app, adapter, shared: Shared):
        self.app, self.adapter, self.shared = app, adapter, shared
        self.owner_thread = threading.get_ident()
        self.original_close = app._on_close
        self.closed = False
        self.deferred = []

    def _owner(self):
        if threading.get_ident() != self.owner_thread:
            raise RuntimeError('GUI callback 必須由 Tk owner 執行。')

    def _error(self, error):
        message = f'{type(error).__name__}: {error}'
        self.adapter.errors.append('[GUI bridge] ' + message)
        self.adapter.errors = self.adapter.errors[-100:]
        return {'ok': False, 'error': message}

    def request_close(self):
        self._owner()
        if not self.closed:
            try:
                self.adapter.dispatch({'callback': 'shutdown', 'values': {}})
            except Exception as error:
                self._error(error)

    def tick(self):
        self._owner()
        if self.closed:
            return
        try:
            for pending_job, operation in list(self.deferred):
                result = operation.poll()
                if result is not None:
                    pending_job.result = result
                    pending_job.event.set()
                    self.deferred.remove((pending_job, operation))
                    self.shared.jobs.task_done()
            for _ in range(4):
                if self.adapter.close_requested:
                    break
                try:
                    job = self.shared.jobs.get_nowait()
                except queue.Empty:
                    break
                try:
                    if time.monotonic() > job.deadline:
                        job.result = {'ok': False, 'error': '排程已過期，未執行。'}
                    else:
                        try:
                            job.result = self.adapter.dispatch(job.payload)
                            from ..gui_setup.managed_installation import DeferredSetupResult
                            if isinstance(job.result, DeferredSetupResult):
                                self.deferred.append((job, job.result))
                                job.result = None
                        except Exception as error:
                            job.result = self._error(error)
                finally:
                    if not any(item[0] is job for item in self.deferred):
                        job.event.set()
                        self.shared.jobs.task_done()
            if self.adapter.close_requested:
                self.shared.close()
                if not self.adapter.is_busy():
                    self.closed = True
                    self.original_close()
                    return
            self.shared.publish(self.adapter.snapshot())
        except Exception as error:
            self._error(error)
        self.app.after(100, self.tick)


def _write_session(path: Path, shared: Shared, server, root: Path) -> dict[str, Any]:
    origin = f'http://127.0.0.1:{server.server_port}'
    session = {'schema': 'gkms.glass-gui-session.v1', 'pid': os.getpid(),
        'url': f'{origin}/#token={shared.token}', 'origin': origin,
        'project_root': str(root), 'read_only': shared.read_only, 'control_enabled': not shared.read_only}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix='.' + path.name + '.', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(session, handle, ensure_ascii=False, allow_nan=False)
            handle.write('\n')
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return session


def _remove_session(path: Path | None, session: dict | None):
    if path is None or session is None:
        return
    try:
        current = json.loads(path.read_text(encoding='utf-8'))
        if current == session:
            path.unlink()
    except (OSError, ValueError):
        pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='GKMS 玻璃風 GUI / 既有 GkmsApp 接線')
    parser.add_argument('--project-root', type=Path, default=DEFAULT_PROJECT_ROOT)
    control = parser.add_mutually_exclusive_group()
    control.add_argument('--read-only', action='store_true', help='唯讀驗證；停用遊戲控制按鈕')
    control.add_argument('--enable-control', dest='read_only', action='store_false', help='相容選項；預設已允許使用者按鈕操作')
    parser.set_defaults(read_only=False)
    parser.add_argument('--allow-source-drift', action='store_true', help='保留舊版參數；目前依 callback 契約檢查相容性')
    parser.add_argument('--show-legacy', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--session-file', type=Path, help='將本機預覽 URL 與 PID 寫入指定檔案；含權限 token')
    return parser.parse_args(argv)


def _create_app(root):
    if os.name != 'nt':
        raise RuntimeError('實機接線需 Windows；HTTP 自動測試不受此限制。')
    from gkms_tool.gui import GkmsApp, _enable_windows_dpi_awareness
    _enable_windows_dpi_awareness()
    return GkmsApp()  # One owner; no start/read-game callback is invoked here.


def main(argv=None, *, public_build=False) -> int:
    for stream in (sys.stdout, sys.stderr):
        configure = getattr(stream, 'reconfigure', None)
        if callable(configure):
            try:
                configure(encoding='utf-8')
            except (OSError, ValueError):
                pass
    args = parse_args(argv)
    if public_build and args.show_legacy:
        raise ValueError('公開版不提供舊研究介面。')
    root = args.project_root.resolve()
    session_path = args.session_file.resolve() if args.session_file is not None else None
    session = None
    previous_cwd = Path.cwd()
    try:
        compatibility = check_source(root, allow_drift=args.allow_source_drift)
        with InstanceLock(root):
            os.chdir(root)
            app = _create_app(root)
            shared = Shared(read_only=args.read_only, public_build=public_build)
            server = server_thread = pump = native_window = None
            try:
                if not args.show_legacy:
                    app.withdraw()
                adapter = GkmsAdapter(app, enable_control=not args.read_only, compatibility=compatibility, public_build=public_build)
                from ..gui_setup.service import ProjectSetupService
                setup_service = ProjectSetupService(project_root=root, inspection=args.read_only,
                                                    enable_installation=True, public_build=public_build,
                                                    enable_developer_tools=not public_build)
                from ..gui_setup.loadout_service import LoadoutService
                setup_service.loadout = LoadoutService(root, getattr(app, 'card_library_panel', None))
                if not public_build:
                    shared.allowed_commands = shared.allowed_commands | setup_service.developer_actions
                detach_presentation = adapter.attach_setup_service(setup_service)
                from ..gui_setup.app_updates import AppUpdateService
                from ..gui_setup.launcher_guard import guard_project_launcher
                from ..application_paths import state_root
                def safe_update():
                    if adapter.is_busy() or adapter.has_pending_native_transaction():
                        raise RuntimeError('培育或原生操作尚未完成，暫停切換 GUI 版本。')
                    guard_project_launcher(root)
                setup_service.app_updates = AppUpdateService(
                    Path(os.environ.get('GKMS_APP_HOME', str(root))),
                    state_root(root) / 'gui_updates',
                    safety_check=safe_update)
                if not args.read_only:
                    setup_service.app_updates.start_startup_check()
                shared.publish(adapter.snapshot())
                pump = OwnerPump(app, adapter, shared)
                server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(shared, setup_service))
                server.daemon_threads = True
                server_thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.1),
                                                daemon=True, name='gkms-ui-http')
                server_thread.start()
                if session_path is not None:
                    session = _write_session(session_path, shared, server, root)
                url = f'http://127.0.0.1:{server.server_port}/#token={shared.token}'
                print(f'GUI 已就緒：http://127.0.0.1:{server.server_port}/（權限 token 不輸出至主控台）')
                print('READ-ONLY CONNECTION' if args.read_only else 'USER CONTROLS ENABLED — 尚未開始培育')
                if session_path is not None:
                    print(f'預覽工作階段檔：{session_path}')
                if not args.no_browser:
                    from .native_window import NativeWindow
                    native_window = NativeWindow.open(url, root, state_root(root))
                    def check_native_window():
                        if pump.closed:
                            return
                        if native_window.poll() is not None:
                            pump.request_close()
                        else:
                            app.after(500, check_native_window)
                    app.after(500, check_native_window)
                app.protocol('WM_DELETE_WINDOW', pump.request_close)
                app.after(100, pump.tick)
                app.mainloop()
            finally:
                shared.close()
                if server is not None:
                    if server_thread is not None and server_thread.is_alive():
                        server.shutdown()
                    server.server_close()
                    if server_thread is not None:
                        server_thread.join(timeout=2)
                try:
                    if 'detach_presentation' in locals():
                        detach_presentation()
                finally:
                    try:
                        if 'setup_service' in locals():
                            setup_service.close()
                    finally:
                        try:
                            _remove_session(session_path, session)
                        finally:
                            if pump is None or not pump.closed:
                                app._on_close()
                            if native_window is not None:
                                native_window.owner_stopped()
        return 0
    except Exception as error:
        print(f'GUI 未能啟動：{type(error).__name__}: {error}', file=sys.stderr)
        return 1
    finally:
        os.chdir(previous_cwd)

if __name__ == '__main__':
    raise SystemExit(main())
