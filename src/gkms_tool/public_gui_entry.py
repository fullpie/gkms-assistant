"""Public version-slot entry; delegates to the existing GUI owner unchanged."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


from .public_environment import configure_public_environment

def package_self_check(slot, *, import_gui=True):
    """File/model/import verification only: no Tk root, network or game calls."""
    from .app_version import info
    from .gui_setup.app_updates import verify_slot
    from .portable_model_assets import load_portable_model_assets, RELEASE_MANIFEST_SHA256
    from .portable_loadout_assets import load_portable_passive_catalog
    from .runtime_gui_exam_policy import build_live_exam_policy
    from .portable_outer_assets import preflight_outer_assets, role_path
    manifest = verify_slot(slot)
    from .glass_gui.native_window import verify_window_package
    window_executable=verify_window_package(Path(slot)/'ui_host')
    if manifest['version'] != info()['version']:
        raise ValueError('The executable version differs from its package manifest.')
    assets = load_portable_model_assets(Path(slot) / 'assets/model_runtime', expected_sha256=RELEASE_MANIFEST_SHA256)
    loadout = load_portable_passive_catalog(Path(slot) / 'assets/loadout')
    outer = preflight_outer_assets(produce_id='produce-004',idol_card_id='i_card-hume-3-006',
                                  weekly_provider_path=role_path('weekly_provider'))
    from .portable_character_labels import load_character_labels
    display_names=load_character_labels()
    if not display_names:raise ValueError('Public character display names are unavailable')
    from .nia_idol_catalog import load_nia_idol_catalog
    character_catalog=load_nia_idol_catalog()
    if any(entry.character_id not in display_names for entry in character_catalog.entries):
        raise ValueError('Public idol selector has an unqualified character label')
    models = {}
    for variant in ('baseline', 'integrated'):
        parameters, metadata = assets.load_parameters(variant)
        policy = build_live_exam_policy(variant, produce_id='produce-004', idol_card_id='i_card-hume-3-006', run_id='public-package-self-check')
        binding = policy.runtime_policy_binding
        if binding['model_sha256'] != metadata['original_model_sha256'] or binding['portable_manifest_sha256'] != RELEASE_MANIFEST_SHA256:
            raise ValueError('The actual public policy factory differs from its frozen artifact identity.')
        models[variant] = {'original_model_sha256': metadata['original_model_sha256'],
            'artifact_sha256': metadata['artifact_sha256'], 'tensors_equal_original': metadata['tensors_equal_original'],
            'tensor_count': len(parameters), 'policy_factory_loaded': True, 'policy_binding_variant': binding['variant_id']}
    if import_gui:
        from .gui import GkmsApp
        from .glass_gui.launcher import main
        import inspect
        if 'public_build' not in inspect.signature(main).parameters or not callable(GkmsApp):
            raise ValueError('The existing GUI owner does not expose its public build gate.')
    return {'schema': 'gkms.public-gui-package-self-check.v1', 'application': info(), 'passed': True,
        'model_manifest_sha256': RELEASE_MANIFEST_SHA256, 'models': models, 'loadout_source': loadout.portable_source,
        'presentation':'native-webview2','native_window_verified':window_executable.is_file(),
        'outer_source':outer,
        'character_display_name_count':len(display_names),
        'idol_display_catalog_count':len(character_catalog.entries),
        'gui_import_verified': import_gui, 'gui_created': False, 'game_io': False, 'network_used': False,
        'public_package_declared': manifest['development_tools'] is False, 'live_workflow_verified': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--read-only', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--self-check-output', type=Path)
    parser.add_argument('--session-file', type=Path, help='Isolated read-only host diagnostic receipt; contains a private local token.')
    parser.add_argument('--maintenance-worker', action='store_true')
    args = parser.parse_args(argv)
    if args.maintenance_worker and (args.read_only or args.no_browser or args.self_check_output is not None or args.session_file is not None):
        parser.error('The fixed maintenance worker role cannot be combined with GUI or diagnostic options.')
    if args.session_file is not None and (not args.read_only or not args.no_browser or args.self_check_output is not None):
        parser.error('A diagnostic session file requires read-only mode without a browser or self-check.')
    slot = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(os.environ['GKMS_APP_ROOT']).resolve()
    try:
        paths = configure_public_environment(slot)
        if args.session_file is not None and not args.session_file.resolve().is_relative_to(paths['user_data_root']):
            raise ValueError('The diagnostic session receipt must remain inside the isolated user-data directory.')
        if args.maintenance_worker:
            from .gui_setup.app_updates import verify_slot
            verify_slot(slot)
            from .native_maintenance import public_worker_entry
            return public_worker_entry()
        if args.self_check_output is not None:
            output = args.self_check_output.resolve()
            if output.is_relative_to(slot):
                raise ValueError('Self-check output must not modify its verified package.')
            result = package_self_check(slot)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            return 0
        from .gui_setup.app_updates import resolve_active_entrypoint
        recovery_slot = os.environ.get('GKMS_GUI_RECOVERY_LAUNCH')
        if recovery_slot:
            from .gui_setup.app_updates import validate_recovery_entrypoint
            if validate_recovery_entrypoint(paths['app_home'], slot, recovery_slot).parent != slot:
                raise ValueError('Recovery entrypoint differs from this verified previous slot.')
        elif resolve_active_entrypoint(paths['app_home']).parent != slot:
            raise ValueError('Start the public launcher to use the currently selected GUI version.')
        from .glass_gui.launcher import main as launch_gui
        options = ['--project-root', str(slot)]
        if args.read_only: options.append('--read-only')
        if args.no_browser: options.append('--no-browser')
        if args.session_file is not None: options.extend(['--session-file', str(args.session_file.resolve())])
        return launch_gui(options, public_build=True)
    except Exception as error:
        import traceback
        result = {'schema': 'gkms.public-gui-package-self-check.v1', 'passed': False, 'error': str(error),
                  'traceback': traceback.format_exc(), 'gui_created': False, 'game_io': False, 'network_used': False}
        if args.self_check_output is not None:
            output = args.self_check_output.resolve()
            if not output.is_relative_to(slot):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        elif os.name == 'nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(error), 'GKMS Assistant', 0x10)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
