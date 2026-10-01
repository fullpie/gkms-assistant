"""Normal-user public launcher for the updater's verified active version slot."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def launch_plan(app_home, *, environment=None):
    from .gui_setup.app_updates import resolve_active_entrypoint
    from .public_environment import configure_public_environment
    home = Path(app_home).resolve()
    executable = resolve_active_entrypoint(home)
    child_environment = dict(os.environ if environment is None else environment)
    child_environment.pop('GKMS_GUI_RECOVERY_LAUNCH', None)
    paths = configure_public_environment(executable.parent, app_home=home, environment=child_environment)
    return {'executable': executable, 'cwd': executable.parent, 'environment': child_environment,
            'paths': paths, 'elevation_requested': False}


def recovery_launch_plan(app_home, *, environment=None):
    from .gui_setup.app_updates import recovery_entrypoint
    from .public_environment import configure_public_environment
    home = Path(app_home).resolve()
    executable = recovery_entrypoint(home)
    child_environment = dict(os.environ if environment is None else environment)
    paths = configure_public_environment(executable.parent, app_home=home, environment=child_environment)
    child_environment['GKMS_GUI_RECOVERY_LAUNCH'] = executable.parent.name
    return {'executable': executable, 'cwd': executable.parent, 'environment': child_environment,
            'paths': paths, 'elevation_requested': False, 'recovery_launch': True}


def _confirm_recovery(plan):
    if os.name != 'nt':
        return False
    import ctypes
    message = ('目前 GUI 版本無法通過完整性檢查。\n\n'
               '是否開啟已驗證的前一版恢復介面？\n'
               '尚未切換版本；不會啟動遊戲或要求維護權限。\n'
               '開啟後請使用「回退上一版」完成恢復。')
    return ctypes.windll.user32.MessageBoxW(None, message, 'GKMS Assistant · 版本恢復', 0x24 | 0x100) == 6


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--read-only', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--recover', action='store_true', help='Ask to open the verified previous GUI without selecting it.')
    parser.add_argument('--self-check-output', type=Path)
    args = parser.parse_args(argv)
    if args.recover and args.self_check_output is not None:
        parser.error('--recover is an interactive recovery entry, not a self-check.')
    home = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(os.environ['GKMS_APP_HOME']).resolve()
    try:
        if args.recover:
            plan = recovery_launch_plan(home)
            if not _confirm_recovery(plan):
                return 1
        else:
            try:
                plan = launch_plan(home)
            except Exception as current_error:
                # Self-checks remain noninteractive and never launch a fallback.
                if args.self_check_output is not None:
                    raise
                try: plan = recovery_launch_plan(home)
                except Exception: raise current_error
                if not _confirm_recovery(plan):
                    return 1
        if args.self_check_output is not None:
            output = args.self_check_output.resolve()
            if output.is_relative_to(home):
                raise ValueError('Bootstrap validation output must be outside the installation.')
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({'schema': 'gkms.public-bootstrap-self-check.v1', 'passed': True,
                'entrypoint': str(plan['executable']), 'user_data_root': str(plan['paths']['user_data_root']),
                'process_started': False, 'game_io': False, 'elevation_requested': False}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            return 0
        command = [str(plan['executable'])]
        if args.read_only: command.append('--read-only')
        if args.no_browser: command.append('--no-browser')
        subprocess.Popen(command, cwd=str(plan['cwd']), env=plan['environment'], shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        return 0
    except Exception as error:
        if args.self_check_output is not None:
            output = args.self_check_output.resolve()
            if not output.is_relative_to(home):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps({'schema': 'gkms.public-bootstrap-self-check.v1', 'passed': False,
                    'error': str(error), 'process_started': False, 'game_io': False}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        elif os.name == 'nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(error), 'GKMS Assistant', 0x10)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
