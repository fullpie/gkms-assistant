"""Presentation-only native WebView2 child; the existing Tk owner keeps control."""
from __future__ import annotations
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import uuid
from urllib.parse import urlsplit, parse_qs


def verify_window_package(directory):
    from ..app_version import VERSION
    directory=Path(directory).resolve()
    manifest=json.loads((directory/'manifest.json').read_bytes())
    expected={'GKMS-Window.exe','GKMS-Window.exe.config','Microsoft.Web.WebView2.Core.dll',
              'Microsoft.Web.WebView2.WinForms.dll','WebView2Loader.dll','LICENSE-WebView2.txt'}
    if (manifest.get('schema')!='gkms.native-window-package.v1' or set(manifest.get('files',{}))!=expected
            or manifest.get('presentation')!='native-webview2' or manifest.get('game_io') is not False
            or manifest.get('application_version')!=VERSION):
        raise ValueError('Native application window package is incomplete')
    for name,digest in manifest['files'].items():
        path=directory/name
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
            raise ValueError('Native application window file changed: '+name)
    return directory/'GKMS-Window.exe'


class NativeWindow:
    def __init__(self, process, report):
        self.process,self.report=process,report

    @classmethod
    def open(cls, url, root, state, *, package=None, capture=False, probe_close=False):
        from ..application_paths import public_installation
        from ..app_version import DISPLAY_VERSION
        parts=urlsplit(url)
        token=parse_qs(parts.fragment).get('token',[])
        if (parts.scheme!='http' or parts.hostname!='127.0.0.1' or not parts.port or parts.path!='/'
                or parts.query or parts.username or len(token)!=1 or not re.fullmatch(r'[A-Za-z0-9_-]{24,128}',token[0])
                or parts.fragment!='token='+token[0]):
            raise ValueError('A valid authenticated loopback GUI connection is required')
        root,state=Path(root).resolve(),Path(state).resolve()
        if package is None:
            package=(root/'ui_host' if public_installation(root)
                     else root/'var/native/gui_webview2/candidate_v6_dpi')
        executable=verify_window_package(package)
        data=state/'native_window';data.mkdir(parents=True,exist_ok=True)
        report=data/('window-'+uuid.uuid4().hex+'.json')
        payload={'schema':'gkms.native-window-launch.v1','url':url,'owner_pid':os.getpid(),
            'user_data':str(data),'report':str(report),'title':'GKMS Assistant '+DISPLAY_VERSION,
            'capture':bool(capture),'probe_close':bool(probe_close and capture)}
        with report.with_suffix('.stderr').open('wb') as diagnostic:
            process=subprocess.Popen([str(executable)],cwd=executable.parent,stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,stderr=diagnostic,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        try:
            process.stdin.write((json.dumps(payload,ensure_ascii=False)+'\n').encode('utf-8'))
            process.stdin.flush()
        except Exception:
            process.stdin.close()
            raise
        return cls(process,report)

    def poll(self):
        return self.process.poll()

    def owner_stopped(self):
        """Only called after the owner completed its normal shutdown path."""
        if self.process.poll() is not None:return
        try:
            self.process.stdin.write(b'owner-stopped\n');self.process.stdin.flush();self.process.stdin.close()
        except (OSError,ValueError):pass
        try:self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Do not force a game/owner or invent shutdown completion. Closing
            # the private pipe and eventual parent exit also close the renderer.
            pass
