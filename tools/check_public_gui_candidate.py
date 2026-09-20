"""Start one isolated read-only public GUI, inspect its HTTP view, then close it.

No game, native maintenance, install or model-selection operation is submitted.
The local authentication token stays only in the temporary session receipt.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from gkms_tool.gui_setup.app_updates import resolve_active_entrypoint


def inspect_candidate(home, output, *, timeout=90, stored_game_path=False):
    home, output = Path(home).resolve(), Path(output).resolve()
    executable = resolve_active_entrypoint(home)
    if output == home or output.is_relative_to(home):
        raise ValueError('Diagnostic state must remain outside the immutable public installation.')
    output.mkdir(parents=True, exist_ok=False)
    user = output/'user-state'; user.mkdir()
    receipt = user/'readonly-session.json'
    environment = dict(os.environ)
    environment.update(GKMS_APP_ROOT=str(executable.parent), GKMS_APP_HOME=str(home),
        GKMS_USER_DATA_ROOT=str(user), LOCALAPPDATA=str(output/'native-empty'), PYTHONDONTWRITEBYTECODE='1')
    report = {'schema':'gkms.public-gui-readonly-smoke.v1', 'passed':False, 'gui_started':False,
        'game_input_submitted':False, 'maintenance_submitted':False, 'install_submitted':False,
        'private_session_printed':False, 'isolated_user_state':True, 'isolated_native_state':True}
    configured_game=None
    if stored_game_path:
        configured_game=output/'fake-game';configured_game.mkdir()
        (configured_game/'gakumas.exe').write_bytes(b'not-executable-test-fixture')
        (configured_game/'GameAssembly.dll').write_bytes(b'no-native-code-loaded')
        (configured_game/'gakumas_Data').mkdir()
        settings=output/'native-empty/gkms-assistant/gui_setup/game-location.json';settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({'schema':'gkms.gui-game-location.v1','game_directory':str(configured_game)}),encoding='utf-8')
    process = None
    session = None

    def request(path, payload=None):
        token = urllib.parse.parse_qs(urllib.parse.urlsplit(session['url']).fragment)['token'][0]
        headers = {'X-GKMS-Token':token, 'Origin':session['origin']}
        if payload is not None: headers['Content-Type']='application/json'
        req = urllib.request.Request(session['origin']+path,
            data=None if payload is None else json.dumps(payload).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    try:
        process = subprocess.Popen([str(executable), '--read-only', '--no-browser', '--session-file',str(receipt)],
            cwd=executable.parent, env=environment, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline and not receipt.is_file():
            if process.poll() is not None: raise RuntimeError('Public GUI exited before publishing its isolated session: '+str(process.returncode))
            time.sleep(.2)
        if not receipt.is_file(): raise TimeoutError('Public GUI did not publish a diagnostic session.')
        session=json.loads(receipt.read_text(encoding='utf-8'))
        if (session.get('pid')!=process.pid or session.get('read_only') is not True or session.get('control_enabled') is not False
                or Path(session.get('project_root','')).resolve()!=executable.parent):
            raise ValueError('Diagnostic session is not bound to the process just started.')
        parts=urllib.parse.urlsplit(session.get('origin',''))
        if parts.scheme!='http' or parts.hostname!='127.0.0.1' or not parts.port:
            raise ValueError('Diagnostic session is not loopback HTTP.')
        report['gui_started']=True
        code,raw=request('/api/snapshot');snapshot=json.loads(raw)
        if code!=200 or snapshot.get('schema')!='gkms.glass-view.v1' or snapshot.get('bridge',{}).get('control_enabled') is not False:
            raise ValueError('Actual GUI did not expose the expected read-only snapshot.')
        (output/'snapshot.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        statuses={}
        for path in ('/','/app.js','/loadout-panel.js','/i18n.js','/developer/panel.js'):
            code,body=request(path);statuses[path]=code
            if code!=(404 if path.startswith('/developer/') else 200): raise ValueError('Public asset boundary differs: '+path)
        if (snapshot.get('developer') or {}).get('available') is True:
            raise ValueError('Public GUI unexpectedly enabled its developer addon.')
        code,raw=request('/setup-api/status',{'gameDirectory':''});setup=json.loads(raw)
        if code!=200 or setup.get('readOnly') is not True:
            raise ValueError('Actual public installer is not read-only for this diagnostic.')
        if configured_game is not None:
            if setup.get('gameDirectory')!=str(configured_game):
                raise ValueError('Persisted game location did not survive the public startup path.')
            report['persisted_game_path_read_verified']=True
            browser_script=Path(__file__).with_name('check_public_gui_dom.cjs')
            if not browser_script.is_file():raise ValueError('Stored-path DOM check script missing')
            browser=subprocess.run(['node',str(browser_script),str(receipt),str(output/'browser'),str(configured_game)],
                cwd=Path(__file__).resolve().parents[1],env=environment,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=45,check=False)
            if browser.returncode:
                raise ValueError('Isolated public DOM startup check failed; see browser/report.json')
            report['stored_game_path_dom_verified']=True
        report.update(http_snapshot_verified=True, public_assets=statuses, developer_area_enabled=False,
            application=snapshot.get('application'), loadout_available=(snapshot.get('loadout') or {}).get('available'),
            setup_status_schema=setup.get('schema'), policy_variants=snapshot.get('catalog',{}).get('policy_variants',[]))
        report['passed']=True
    except Exception as error:
        report['error']=type(error).__name__+': '+str(error)
    finally:
        if process is not None and process.poll() is None:
            if session is not None:
                try:
                    code,_=request('/api/command',{'request_id':uuid.uuid4().hex,'callback':'shutdown','values':{}})
                    report['shutdown_http_status']=code
                    process.wait(timeout=15)
                    report['graceful_shutdown']=process.returncode==0
                except Exception as error:
                    report['shutdown_error']=type(error).__name__
            if process.poll() is None:
                process.terminate();process.wait(timeout=10)
                report['terminated_own_diagnostic_process']=True
                report['passed']=False
        if process is not None: report['exit_code']=process.returncode
        report['session_removed']=not receipt.exists()
        report['native_requests_created']=[str(path.relative_to(output)) for directory in ('native-empty','user-state')
            for path in (output/directory).rglob('*.json') if path.parent.name in ('inbox','running')]
        if report['native_requests_created']: report['passed']=False
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--managed-root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--stored-game-path',action='store_true',help='Use an isolated non-executable game-path fixture and verify its real browser display.')
    args=parser.parse_args(argv)
    result=inspect_candidate(args.managed_root,args.output,stored_game_path=args.stored_game_path)
    print(json.dumps({key:result.get(key) for key in ('passed','gui_started','graceful_shutdown','error')},ensure_ascii=False))
    return 0 if result['passed'] else 2


if __name__=='__main__': raise SystemExit(main())
