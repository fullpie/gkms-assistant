"""Allowlisted public-source export and Windows GUI candidate packaging.

No repository history, raw research assets, runtime user state or game binaries
are copied. A candidate is not a release: unresolved requirements are explicit.
"""
from __future__ import annotations

import argparse
import ast
from collections import deque
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
import types
import zipfile

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE / 'src'))
from gkms_tool.app_version import VERSION, PRODUCT, COMPONENT, CHANNEL, PLATFORM
from gkms_tool.gui_setup.app_updates import initialize_managed_layout, verify_slot
from gkms_tool.portable_model_assets import RELEASE_MANIFEST_SHA256
from gkms_tool.portable_loadout_assets import load_portable_passive_catalog, RELEASE_MANIFEST_SHA256 as LOADOUT_SHA

ENTRY_MODULES = ('gkms_tool.public_gui_entry', 'gkms_tool.public_gui_bootstrap')
MODEL_SHA = RELEASE_MANIFEST_SHA256
PRIVATE_SOURCE = re.compile(r'(?i)[a-z]:[\\/]+(?:Users[\\/]+[^\\/]+|gkms_tool(?:[\\/]|$))')
EXCLUDED_NAMES = ('__pycache__', '.git', 'tests', 'fixtures', '_archive', '_research')
PUBLIC_EXCLUDED_MODULES = {'gkms_tool.gui_setup.developer_addon',
    'gkms_tool.private_gui', 'gkms_tool.native_maintenance_development'}
BOOTSTRAP_EXCLUDED_MODULES = frozenset({'gkms_tool.gui_setup.service', 'gkms_tool.gui',
    'gkms_tool.glass_gui', 'gkms_tool.public_gui_entry', 'gkms_tool.portable_model_assets',
    'gkms_tool.portable_loadout_assets', 'gkms_tool.runtime_gui_exam_policy', 'gkms_tool.native_maintenance',
    'numpy', 'scipy', 'pandas', 'matplotlib', 'PIL', 'cv2', 'maa', 'tkinter'})
IDENTITY = {'product': PRODUCT, 'component': COMPONENT, 'channel': CHANNEL, 'platform': PLATFORM}
PUBLIC_GITIGNORE = ('__pycache__/\n*.py[cod]\n.venv/\nvar/\nbuild/\ndist/\n*.egg-info/\n'
    'assets/model_runtime/\nassets/loadout/\nassets/outer_runtime/\nassets/display_labels/\nassets/control/\n.pytest_cache/\n')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')


def module_file(source, name):
    location = source.joinpath(*name.split('.'))
    if location.with_suffix('.py').is_file(): return location.with_suffix('.py')
    package = location / '__init__.py'
    return package if package.is_file() else None


def source_closure(workspace, entries=ENTRY_MODULES, *, excluded_modules=PUBLIC_EXCLUDED_MODULES):
    """Conservative real import edges, including optional function-local imports."""
    source = Path(workspace) / 'src'
    pending = deque(entries); included = {}; incoming = {}; external = set(); dynamic = []
    while pending:
        name = pending.popleft()
        if name in included: continue
        path = module_file(source, name)
        if path is None:
            raise ValueError('Required GUI module missing: ' + name)
        resolved = path.resolve()
        if path.is_symlink() or not resolved.is_relative_to(source.resolve()) or any(part in EXCLUDED_NAMES for part in resolved.relative_to(source.resolve()).parts):
            raise ValueError('GUI import resolves outside the allowed source tree: ' + name)
        included[name] = path
        for count in range(1, len(name.split('.'))):
            parent = '.'.join(name.split('.')[:count])
            if module_file(source, parent):
                pending.append(parent); incoming.setdefault(parent, set()).add(name + ':package-init')
        tree = ast.parse(path.read_text('utf-8-sig'), filename=str(path))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = name.split('.') if path.name == '__init__.py' else name.split('.')[:-1]
                parent = '.'.join(base[:len(base) - node.level + 1]) if node.level else ''
                target = '.'.join(value for value in (parent, node.module) if value) if node.level else node.module or ''
                targets = [target] + [target + '.' + alias.name for alias in node.names if alias.name != '*']
            elif isinstance(node, ast.Call):
                is_import = (isinstance(node.func, ast.Name) and node.func.id == '__import__') or (
                    isinstance(node.func, ast.Attribute) and node.func.attr in ('import_module', 'spec_from_file_location'))
                if is_import:
                    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                        targets = [node.args[0].value]
                    else:
                        dynamic.append({'module': name, 'line': node.lineno, 'kind': 'computed-import'})
            for target in targets:
                if target == 'gkms_tool' or target.startswith('gkms_tool.'):
                    if any(target==item or target.startswith(item+'.') for item in excluded_modules):
                        continue  # The immutable public entry disables this guarded optional addon.
                    if module_file(source, target):
                        incoming.setdefault(target, set()).add(f'{name}:{node.lineno}')
                        pending.append(target)
                elif target and not target.startswith('.'):
                    external.add(target.split('.')[0])
    return included, incoming, sorted(external), dynamic


def validate_model_bundle(root, expected_sha):
    root = Path(root).resolve(); manifest = root / 'manifest.json'
    if digest(manifest) != expected_sha: raise ValueError('Portable model manifest SHA differs')
    body = json.loads(manifest.read_bytes())
    if (body.get('schema') != 'gkms.portable-model-assets.v1' or body.get('raw_game_or_replay_assets_included') is not False
            or body.get('training_admitted') is not False or set(body.get('models', {})) != {'baseline', 'integrated'}):
        raise ValueError('Portable model bundle is outside the public inference scope')
    names = {'manifest.json'}
    for reference in body['files'].values():
        name = reference['path']; path = root / name
        if (Path(name).name != name or path.is_symlink() or not path.is_file()
                or path.stat().st_size != reference['bytes'] or digest(path) != reference['sha256']):
            raise ValueError('Portable asset differs: ' + name)
        names.add(name)
    return body, sorted(names)


def public_manifest(directory):
    return {path.relative_to(directory).as_posix(): digest(path) for path in sorted(directory.rglob('*')) if path.is_file()}


def verify_executable_source_boundary(executable, *, allowed_modules=None):
    """Inspect code metadata without importing or executing the frozen archive."""
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(executable)).open_embedded_archive('PYZ.pyz')
    names = [name for name in archive.toc if name == 'gkms_tool' or name.startswith('gkms_tool.')]
    if any(name in archive.toc for name in PUBLIC_EXCLUDED_MODULES):
        raise ValueError('A developer addon entered the public executable archive')
    if allowed_modules is not None and set(names)-set(allowed_modules):
        raise ValueError('Bootstrap archive contains unnecessary application modules: '+', '.join(sorted(set(names)-set(allowed_modules))))
    count = 0
    for name in names:
        pending = [archive.extract(name)]
        while pending:
            code = pending.pop()
            if not isinstance(code, types.CodeType): continue
            count += 1
            if PRIVATE_SOURCE.search(code.co_filename) or re.match(r'^[A-Za-z]:|^[/\\]',code.co_filename):
                raise ValueError('A compiled application source exposes a machine path: ' + name)
            pending.extend(value for value in code.co_consts if isinstance(value, types.CodeType))
    return {'gkms_module_count':len(names),'gkms_modules':sorted(names),'code_object_count':count,
        'machine_source_paths_found':False,'developer_addon_included':False,'code_executed':False}


def validate_loadout_bundle(root):
    root = Path(root).resolve()
    catalog = load_portable_passive_catalog(root)
    manifest = json.loads((root/'manifest.json').read_bytes())
    names = ['manifest.json', 'NOTICE.txt', *[entry['file'] for entry in manifest['tables'].values()]]
    if any((root/name).is_symlink() or not (root/name).is_file() for name in names):
        raise ValueError('Public loadout asset or provenance notice missing')
    return catalog.portable_source, names


def validate_outer_bundle(root):
    from gkms_tool.portable_outer_assets import verify_assets, RELEASE_MANIFEST_SHA256
    root=Path(root).resolve();manifest=verify_assets(root)
    files=['manifest.json','NOTICE.txt',*[row['path'] for row in manifest['files'].values()],
           *[row['path'] for row in manifest['tables'].values()]]
    if not (root/'NOTICE.txt').is_file():raise ValueError('Public outer provenance notice missing')
    return {'manifest_sha256':RELEASE_MANIFEST_SHA256,'master_hash':manifest['master_hash'],
        'weekly_model_sha256':manifest['files']['weekly_model']['sha256']},sorted(set(files))


def validate_display_labels(root):
    from gkms_tool.portable_character_labels import load_character_labels,RELEASE_MANIFEST_SHA256
    root=Path(root).resolve();labels=load_character_labels(root)
    if not (root/'NOTICE.txt').is_file():raise ValueError('Display-label provenance notice missing')
    return {'manifest_sha256':RELEASE_MANIFEST_SHA256,'character_count':len(labels)},['manifest.json','characters.json','NOTICE.txt']


def verify_public_source_roundtrip(source, numeric_sources, destination, archive):
    """A fresh local Git commit/clone with autocrlf=true must retain model bytes."""
    source, destination = Path(source), Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    repository, clone = destination/'repository', destination/'clone'
    shutil.copytree(source, repository)
    git = shutil.which('git')
    if not git: raise ValueError('Git is required to verify public source byte preservation')
    hooks = destination/'empty-hooks'; hooks.mkdir()
    environment = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
    base = [git,'-c','core.autocrlf=true','-c','commit.gpgsign=false','-c','core.hooksPath='+str(hooks),
        '-c','user.name=GKMS source verification','-c','user.email=source-verification@invalid']
    for args in (['init','--initial-branch=source-verification',str(repository)],
                 ['-C',str(repository),'add','.'], ['-C',str(repository),'commit','-m','Verify exported source bytes'],
                 ['clone','--no-local',str(repository),str(clone)]):
        result = subprocess.run(base+args,env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
        if result.returncode:
            raise ValueError('Public source Git roundtrip failed: '+result.stderr.decode('utf-8','replace')[-2000:])
    rows = []
    with zipfile.ZipFile(archive) as zipped:
        for name, expected in sorted(numeric_sources.items()):
            relative = 'src/gkms_tool/'+name
            extracted = destination/'extracted'/relative; extracted.parent.mkdir(parents=True, exist_ok=True)
            extracted.write_bytes(zipped.read(relative))
            hashes = {'source':digest(source/relative),'fresh_git_clone':digest(clone/relative),'archive_extraction':digest(extracted)}
            if any(value!=expected for value in hashes.values()):
                raise ValueError('Public numeric source bytes changed during distribution: '+name)
            rows.append({'path':relative,'expected_sha256':expected,**hashes})
    report = {'schema':'gkms.public-source-byte-roundtrip.v1','passed':True,'core_autocrlf':True,
        'gitattributes':'* -text whitespace=cr-at-eol','numeric_source_count':len(rows),'files':rows,
        'remote_network_used':False,'main_workspace_git_modified':False}
    write_json(destination/'report.json',report)
    return report


def export_source(workspace, output):
    workspace, output = Path(workspace).resolve(), Path(output).resolve()
    included, incoming, external, dynamic = source_closure(workspace)
    output.mkdir(parents=True, exist_ok=False)
    (output/'.gitattributes').write_bytes(b'* -text whitespace=cr-at-eol\n')
    (output/'.gitignore').write_bytes(PUBLIC_GITIGNORE.encode('utf-8'))
    blockers = []; records = []; coupled = []
    for name, path in sorted(included.items()):
        relative = path.relative_to(workspace)
        if any(part in EXCLUDED_NAMES for part in relative.parts):
            raise ValueError('Import escaped the public source boundary: ' + str(relative))
        raw = path.read_bytes()
        hits = [line for line, text in enumerate(raw.decode('utf-8-sig').splitlines(), 1) if PRIVATE_SOURCE.search(text)]
        if hits:
            blockers.append({'code': 'PRIVATE_SOURCE_PATH', 'path': relative.as_posix(), 'lines': hits})
            continue
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True); destination.write_bytes(raw)
        records.append({'module': name, 'path': relative.as_posix(), 'sha256': hashlib.sha256(raw).hexdigest(),
                        'required_by': sorted(incoming.get(name, {'public-entry'}))})
        if any(term in name for term in ('training', 'replay', 'vision', 'maa', 'supervisor', 'canary', 'telemetry')):
            coupled.append({'module': name, 'reason': 'retained by an actual conservative GUI import edge',
                            'required_by': sorted(incoming.get(name, ())), 'standalone_entrypoint_exported': False})
    web = workspace / 'src/gkms_tool/glass_gui/web'
    for name in ('index.html', 'app.css', 'app.js', 'host.js', 'release-engine.js', 'project-state.js', 'loadout-panel.js', 'i18n.js', 'i18n.json', 'build-i18n.cjs'):
        destination = output / 'src/gkms_tool/glass_gui/web' / name
        destination.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(web / name, destination)
    tools = output / 'tools'; tools.mkdir()
    shutil.copyfile(Path(__file__), tools / 'build_public_gui.py')
    window_builder=workspace/'scripts/build_native_gui_window.py'
    if not window_builder.is_file():window_builder=workspace/'tools/build_native_gui_window.py'
    if not window_builder.is_file():raise ValueError('Native application window builder is missing')
    shutil.copyfile(window_builder,tools/'build_native_gui_window.py')
    window_source=workspace/'native/gui_window/Program.cs'
    window_destination=output/'native/gui_window/Program.cs'
    window_destination.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(window_source,window_destination)
    shutil.copyfile(workspace/'native/gui_window/app.manifest',window_destination.parent/'app.manifest')
    smoke = workspace/'scripts/check_public_gui_candidate.py'
    if not smoke.is_file(): smoke = workspace/'tools/check_public_gui_candidate.py'
    if smoke.is_file(): shutil.copyfile(smoke, tools/'check_public_gui_candidate.py')
    browser_smoke=workspace/'scripts/check_public_gui_dom.cjs'
    if not browser_smoke.is_file():browser_smoke=workspace/'tools/check_public_gui_dom.cjs'
    if browser_smoke.is_file():shutil.copyfile(browser_smoke,tools/'check_public_gui_dom.cjs')
    native_exporter = workspace/'scripts/export_public_native_sources.py'
    if not native_exporter.is_file(): native_exporter = workspace/'tools/export_public_native_sources.py'
    if native_exporter.is_file(): shutil.copyfile(native_exporter, tools/'export_public_native_sources.py')
    loadout_exporter = workspace/'scripts/export_portable_loadout_assets.py'
    if not loadout_exporter.is_file(): loadout_exporter = workspace/'tools/export_portable_loadout_assets.py'
    if loadout_exporter.is_file(): shutil.copyfile(loadout_exporter, tools/'export_portable_loadout_assets.py')
    outer_exporter=workspace/'scripts/export_portable_outer_assets.py'
    if not outer_exporter.is_file():outer_exporter=workspace/'tools/export_portable_outer_assets.py'
    if outer_exporter.is_file():shutil.copyfile(outer_exporter,tools/'export_portable_outer_assets.py')
    label_exporter=workspace/'scripts/export_portable_character_labels.py'
    if not label_exporter.is_file():label_exporter=workspace/'tools/export_portable_character_labels.py'
    if label_exporter.is_file():shutil.copyfile(label_exporter,tools/'export_portable_character_labels.py')
    for name in ('public-gui-packaging.md','gui-update-recovery.md','portable-outer-assets.md'):
        packaging_doc = workspace/'docs'/name
        if packaging_doc.is_file():
            (output/'docs').mkdir(exist_ok=True)
            shutil.copyfile(packaging_doc, output/'docs'/name)
    root_metadata = tomllib.loads((workspace / 'pyproject.toml').read_text('utf-8'))
    dependencies = root_metadata['project']['dependencies']
    configuration = ('[build-system]\nrequires = ["setuptools>=68"]\nbuild-backend = "setuptools.build_meta"\n\n'
        f'[project]\nname = "gkms-assistant"\nversion = "{VERSION}"\nrequires-python = ">=3.11"\n'
        'description = "Local GUI assistant for Gakumas"\ndependencies = ' + json.dumps(dependencies) + '\n\n'
        '[project.optional-dependencies]\nbuild = ["pyinstaller==6.22.3"]\n\n'
        '[project.scripts]\ngkms-assistant = "gkms_tool.public_gui_entry:main"\n'
        'gkms-assistant-launcher = "gkms_tool.public_gui_bootstrap:main"\n\n'
        '[tool.setuptools]\npackage-dir = {"" = "src"}\n[tool.setuptools.packages.find]\nwhere = ["src"]\n'
        '[tool.setuptools.package-data]\n"gkms_tool.glass_gui" = ["web/*.html", "web/*.css", "web/*.js", "web/i18n.json"]\n')
    (output / 'pyproject.toml').write_text(configuration, encoding='utf-8')
    license_files = []
    for name in ('LICENSE', 'LICENSE.txt', 'LICENSE.md', 'NOTICE.txt', 'NOTICE.md'):
        if (workspace / name).is_file():
            shutil.copyfile(workspace / name, output / name); license_files.append(name)
    if not license_files:
        (output / 'NOTICE.txt').write_text(
            'Original GKMS Assistant project portions: All rights reserved. No additional open-source license is granted.\n'
            'This notice applies only to original project portions. Third-party components and modifications governed by their licenses retain those terms and source rights.\n'
            'See the accompanying third-party license inventories and corresponding source.\n', encoding='utf-8')
    (output / 'README.md').write_text(
        '# GKMS Assistant ' + VERSION + '\n\n'
        'This is the allowlisted public GUI source export. It does not include account state, login credentials, research captures, game binaries or test DLLs.\n\n'
        'Original project portions are all rights reserved; no additional open-source grant is made. Third-party licenses and source rights remain unchanged. See NOTICE and the license inventories.\n\n'
        'The published application uses one existing GUI/controller owner. Two BC model choices share their original verified weights; selectable flows do not imply trained coverage or accepted policy quality.\n\n'
        'Install the pinned dependencies and build extra, then use `python tools/build_public_gui.py --workspace . --output BUILD --model-assets MODEL_ASSETS --model-manifest-sha256 SHA --loadout-assets LOADOUT_ASSETS --outer-assets OUTER_ASSETS --display-labels DISPLAY_LABELS --control-package CONTROL_ZIP --control-sha256 CONTROL_SHA`. '
        'Qualified model, loadout, outer-rule/behavior assets and the control package are required. The exported native-source ledger is reused without private build receipts. '
        'Assets are separate release files, not checked into the source repository. See docs/public-gui-packaging.md. Inspect the machine-readable build report; a candidate is not a published release.\n\n'
        'The managed Windows layout has a normal-user launcher and immutable version slots. The interface opens in its own Windows WebView2 window, not in Chrome. '
        'Microsoft Edge WebView2 Evergreen Runtime and .NET Framework 4.7.2+ are required; the small presentation host is compiled from native/gui_window/Program.cs using the pinned Microsoft SDK. '
        'User state stays in LocalAppData/gkms-assistant. GUI updates do not start or restart the game.\n', encoding='utf-8')
    manifest = {'schema': 'gkms.public-source-export.v1', **IDENTITY, 'version': VERSION,
        'entry_modules': list(ENTRY_MODULES), 'module_count': len(records), 'modules': records,
        'coupled_legacy_modules': coupled, 'computed_import_sites': dynamic, 'external_import_roots': external,
        'research_cli_entrypoints_exported': False, 'raw_research_or_user_assets_included': False,
        'excluded_developer_modules': sorted(PUBLIC_EXCLUDED_MODULES),
        'project_license': 'all-rights-reserved', 'project_license_authority': 'explicit-owner-instruction-2026-09-20',
        'blockers': blockers, 'source_export_complete': not any(x['code'] == 'PRIVATE_SOURCE_PATH' for x in blockers)}
    manifest['files'] = public_manifest(output)
    write_json(output / 'public-source-manifest.json', manifest)
    return manifest


def _build_executable(source, output, name, entry_module, logger, *, bootstrap=False, numeric_sources=()):
    entry = output / (name + '-entry.py')
    entry.write_text(f'from {entry_module} import main\nif __name__ == "__main__":\n    raise SystemExit(main())\n', encoding='utf-8')
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--windowed',
        '--name', name, '--distpath', str(output/'dist'), '--workpath', str(output/'work'/name),
        '--specpath', str(output/'spec'), '--paths', str(source/'src'),
        '--exclude-module', 'pytest', '--exclude-module', 'IPython', '--exclude-module', 'notebook',
        '--exclude-module', 'speakeasy', '--exclude-module', 'torch']
    for module in sorted(PUBLIC_EXCLUDED_MODULES): command.extend(['--exclude-module',module])
    if bootstrap:
        excluded=BOOTSTRAP_EXCLUDED_MODULES|{'gkms_tool.'+Path(name).stem for name in numeric_sources}
        for module in sorted(excluded): command.extend(['--exclude-module',module])
    else:
        command.extend(['--add-data',str(source/'src/gkms_tool')+os.pathsep+'gkms_tool'])
    command.append(str(entry))
    # Analysis must resolve the copied allowlist, not this development checkout.
    environment = dict(os.environ); environment['PYTHONPATH'] = str(source/'src')
    with logger.open('wb') as log:
        result = subprocess.run(command, cwd=source, env=environment, stdout=log, stderr=subprocess.STDOUT, check=False)
    if result.returncode: raise RuntimeError(f'PyInstaller {name} failed; inspect {logger.name}')
    return output/'dist'/name


def _copy_licenses(directory, distributions, *, include_tk=True):
    records, missing = [], []
    for name in sorted(set(distributions)):
        if name.casefold().replace('_', '-') in {'gkms-tool', 'gkms-assistant'}:
            continue  # The project's own grant/notice is tracked separately.
        try: distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError: continue
        count = 0
        for file in distribution.files or ():
            if not re.search(r'(?i)licen[sc]e|copying|notice', Path(str(file)).name) or Path(str(file)).suffix.lower() not in ('', '.txt', '.md', '.rst', '.html', '.license', '.licence', '.terms'):
                continue
            path = Path(distribution.locate_file(file))
            if not path.is_file() or path.stat().st_size > 8*1024**2: continue
            target = directory / name / Path(str(file)).name
            if target.exists() and digest(target) != digest(path): target = target.with_name(digest(path)[:12] + '-' + target.name)
            target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(path, target)
            records.append({'distribution': name, 'version': distribution.version,
                'file': target.relative_to(directory).as_posix(), 'sha256': digest(target)})
            count += 1
        if not count: missing.append(name)
    for relative in (('LICENSE.txt', 'tcl/tcl8.6/license.terms', 'tcl/tk8.6/license.terms') if include_tk else ('LICENSE.txt',)):
        original = Path(sys.base_prefix) / relative
        if original.is_file():
            target = directory / 'CPython' / relative
            target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(original, target)
            records.append({'distribution': 'CPython', 'version': sys.version.split()[0],
                'file': target.relative_to(directory).as_posix(), 'sha256': digest(target)})
    return records, missing


def _analyzed_distributions(compiler_root):
    """Collect notices for actual frozen imports, including transitive packages."""
    modules = set()
    def visit(value):
        if isinstance(value, (tuple, list)):
            if (len(value) == 3 and isinstance(value[0], str) and isinstance(value[2], str)
                    and value[2] in ('PYMODULE', 'PYSOURCE', 'EXTENSION')):
                modules.add(value[0].split('.')[0])
            else:
                for item in value: visit(item)
    for path in Path(compiler_root).rglob('Analysis-00.toc'):
        visit(ast.literal_eval(path.read_text('utf-8')))
    installed = importlib.metadata.packages_distributions()
    return {name for module in modules for name in installed.get(module, ())}


def _archive(directory, destination):
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(directory.rglob('*')):
            if path.is_file(): archive.write(path, path.relative_to(directory).as_posix())


def archive_public_source(directory, destination):
    """Only the clean export ledger enters source distribution, never probe caches."""
    manifest = json.loads((directory/'public-source-manifest.json').read_bytes())
    with zipfile.ZipFile(destination,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for name, expected in sorted(manifest['files'].items()):
            path=directory/name
            if (not path.resolve().is_relative_to(directory.resolve()) or path.is_symlink()
                    or '__pycache__' in path.parts or path.suffix in ('.pyc','.pyo') or digest(path)!=expected):
                raise ValueError('Unqualified public source entry: '+name)
            archive.write(path,name)
        archive.write(directory/'public-source-manifest.json','public-source-manifest.json')


def attach_native_sources(workspace, source, receipt_path=None):
    source, workspace = Path(source), Path(workspace)
    destination = source/'native-source'
    if receipt_path is not None:
        path = workspace/'scripts/export_public_native_sources.py'
        if not path.is_file(): path = workspace/'tools/export_public_native_sources.py'
        spec = importlib.util.spec_from_file_location('gkms_public_native_exporter', path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        native = module.export(workspace, destination, receipt_path)
    elif (workspace/'native-source/SOURCE-MANIFEST.json').is_file():
        original = workspace/'native-source'; native = json.loads((original/'SOURCE-MANIFEST.json').read_bytes())
        if native.get('schema') != 'gkms.public-native-corresponding-source.v1' or native.get('public_profiles_only') is not True:
            raise ValueError('Existing native source export is not public-qualified')
        destination.mkdir()
        for name, expected in native['files'].items():
            path = original/name
            if not path.resolve().is_relative_to(original.resolve()) or digest(path) != expected:
                raise ValueError('Existing native source file differs: '+name)
            target = destination/name; target.parent.mkdir(parents=True,exist_ok=True); shutil.copyfile(path,target)
        shutil.copyfile(original/'SOURCE-MANIFEST.json',destination/'SOURCE-MANIFEST.json')
    else:
        return None
    manifest_path = source/'public-source-manifest.json'; manifest = json.loads(manifest_path.read_bytes())
    manifest['native_sources'] = {'root':'native-source','manifest_sha256':digest(destination/'SOURCE-MANIFEST.json'),
        'control_archive_sha256':native['control_archive_sha256'],'localify_source_archive_sha256':native['localify_source_archive_sha256'],
        'public_profiles_only':True,'clean_rebuild_claimed':native['clean_rebuild_verified']}
    manifest['files'] = {name:value for name,value in public_manifest(source).items() if name!='public-source-manifest.json'}
    write_json(manifest_path,manifest)
    return manifest['native_sources']


def diagnose_source_imports(source, model_assets, output):
    """Clean-root import/model load probe before spending time on freezing.

    An audit hook rejects network, child processes, game binaries and historical
    research trees. This cannot open Tk or the game as part of the probe.
    """
    source, model_assets, output = Path(source).resolve(), Path(model_assets).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    slot = output/'managed/versions/source-probe'; slot.mkdir(parents=True)
    (slot/'gkms-app-package.json').write_text('{"diagnostic_only":true}', encoding='utf-8')
    script = output/'probe.py'
    script.write_text('''import hashlib,json,os,sys,traceback,platform
from pathlib import Path
source,assets,destination=map(Path,sys.argv[1:4])
sys.path.insert(0,str(source/'src'))
sys.meta_path=[f for f in sys.meta_path if not getattr(f,'__module__','').startswith('__editable__')]
platform.uname()  # Cache the standard library's read-only OS-version query.
def audit(event,args):
    if event in ('socket.connect','subprocess.Popen'):raise RuntimeError('Forbidden during offline source probe: '+event)
    if event=='ctypes.dlopen' and args and 'gameassembly' in str(args[0]).lower():raise RuntimeError('Game binary load forbidden')
    if event=='open' and args and isinstance(args[0],(str,bytes,os.PathLike)):
        path=Path(os.fsdecode(args[0]))
        if '_research' in path.parts:raise RuntimeError('Historical research tree access forbidden: '+str(path))
sys.addaudithook(audit)
report={'schema':'gkms.public-source-import-probe.v1','passed':False,'gui_created':False,'game_io':False,'network_used':False,'package_validation_claimed':False}
try:
    from gkms_tool.gui import GkmsApp
    from gkms_tool.portable_model_assets import load_portable_model_assets,RELEASE_MANIFEST_SHA256
    package=load_portable_model_assets(assets,expected_sha256=RELEASE_MANIFEST_SHA256)
    report['models']={}
    for variant in ('baseline','integrated'):
        weights,metadata=package.load_parameters(variant)
        report['models'][variant]={'original_model_sha256':metadata['original_model_sha256'],'tensors_equal_original':metadata['tensors_equal_original'],'tensor_count':len(weights)}
    report['passed']=True
except Exception as error:report.update(error=str(error),traceback=traceback.format_exc())
destination.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\\n',encoding='utf-8')
raise SystemExit(0 if report['passed'] else 2)
''', encoding='utf-8')
    environment = dict(os.environ); environment.update(GKMS_APP_ROOT=str(slot), GKMS_APP_HOME=str(slot.parent.parent),
        GKMS_USER_DATA_ROOT=str(output/'user-state'), GKMS_PORTABLE_MODEL_ASSETS=str(model_assets),
        GKMS_INPUT_BACKEND='dll', LOCALAPPDATA=str(output/'isolated-localappdata'),
        PYTHONPATH=str(source/'src'), PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
    report = output/'report.json'
    with (output/'process.log').open('wb') as log:
        code = subprocess.run([sys.executable, str(script), str(source), str(model_assets), str(report)],
            cwd=source, env=environment, stdout=log, stderr=subprocess.STDOUT, check=False, timeout=90).returncode
    if not report.is_file(): raise RuntimeError('Source probe produced no report; inspect process.log')
    result = json.loads(report.read_text('utf-8'))
    if code and result.get('passed') is True: raise RuntimeError('Source probe exit/report mismatch')
    return result


def build(workspace, output, *, model_assets=None, model_manifest_sha256=MODEL_SHA, loadout_assets=None, outer_assets=None, display_labels=None, source_only=False,
          control_package=None, control_sha256=None, native_source_receipt=None):
    workspace, output = Path(workspace).resolve(), Path(output).resolve()
    if output == workspace or workspace.is_relative_to(output): raise ValueError('Build output cannot own the source workspace')
    output.mkdir(parents=True, exist_ok=False)
    report = {'schema': 'gkms.public-gui-build.v1', **IDENTITY, 'version': VERSION, 'source_only': source_only,
        'source_export_complete': False, 'executable_build_complete': False, 'package_validation_complete': False,
        'self_check_complete': False, 'candidate_complete': False, 'release_ready': False,
        'release_approval': 'requires-owner-acceptance', 'published': False, 'game_io': False,
        'project_license': 'all-rights-reserved',
        'phase': 'exporting-source', 'blockers': []}
    def save(): write_json(output/'build-report.json', report)
    save()
    try:
        source = output/'source'; exported = export_source(workspace, source)
        native_sources = attach_native_sources(workspace, source, native_source_receipt)
        report.update(source_export_complete=exported['source_export_complete'],
            source_manifest_sha256=digest(source/'public-source-manifest.json'), blockers=exported['blockers'])
        report['native_source_export'] = native_sources
        archive_public_source(source, output/f'gkms-assistant-source-{VERSION}.zip')
        report['phase'] = 'source-export-complete'; save()
        if source_only: return report
        if not report['source_export_complete']:
            save(); return report
        if os.name != 'nt':
            report['blockers'].append({'code': 'WINDOWS_BUILD_REQUIRED'}); save(); return report
        if model_assets is None:
            report['blockers'].append({'code': 'MODEL_ASSETS_PENDING'}); save(); return report
        model, model_files = validate_model_bundle(model_assets, model_manifest_sha256)
        if loadout_assets is None:
            report['blockers'].append({'code': 'LOADOUT_ASSETS_PENDING'}); save(); return report
        loadout_source, loadout_files = validate_loadout_bundle(loadout_assets)
        report['loadout_source'] = loadout_source
        if outer_assets is None:
            report['blockers'].append({'code':'OUTER_POLICY_ASSETS_PENDING'});save();return report
        outer_source,outer_files=validate_outer_bundle(outer_assets)
        report['outer_source']=outer_source
        if display_labels is None:
            report['blockers'].append({'code':'CHARACTER_DISPLAY_LABELS_PENDING'});save();return report
        label_source,label_files=validate_display_labels(display_labels)
        report['character_labels']=label_source
        for name, expected in model['numeric_sources'].items():
            if digest(source/'src/gkms_tool'/name) != expected:
                raise ValueError('Public numerical source differs from model contract: ' + name)
        report['source_roundtrip'] = verify_public_source_roundtrip(source, model['numeric_sources'],
            output/'source-roundtrip', output/f'gkms-assistant-source-{VERSION}.zip')
        report['model_manifest_sha256'] = model_manifest_sha256
        report['models'] = {name: {'original_model_sha256': row['original_model_sha256'],
            'artifact_sha256': row['artifact']['sha256']} for name, row in model['models'].items()}
        if control_package is None:
            report['blockers'].append({'code': 'CONTROL_PACKAGE_PENDING'})
        elif not control_sha256 or digest(control_package) != control_sha256:
            raise ValueError('Separate control package SHA differs')
        if native_sources is None:
            report['blockers'].append({'code': 'NATIVE_SOURCE_LICENSE_RECEIPT_PENDING'})
        else:
            report['native_source_manifest_sha256'] = native_sources['manifest_sha256']
        build_root = output/'compiler'; build_root.mkdir()
        report['phase'] = 'building-gui'; save()
        gui = _build_executable(source, build_root, 'gkms-assistant', ENTRY_MODULES[0], output/'gui-build.log')
        report['phase'] = 'building-bootstrap'; save()
        # Names differing only by case would alias and overwrite one another
        # on Windows. Keep distinct compiler/output identities for both EXEs.
        bootstrap_modules,_,_,_=source_closure(source,entries=(ENTRY_MODULES[1],),
            excluded_modules=PUBLIC_EXCLUDED_MODULES|BOOTSTRAP_EXCLUDED_MODULES)
        bootstrap = _build_executable(source, build_root, 'gkms-assistant-launcher', ENTRY_MODULES[1], output/'bootstrap-build.log',
            bootstrap=True,numeric_sources=model['numeric_sources'])
        report['compiled_source_boundary'] = {
            'gui':verify_executable_source_boundary(gui/'gkms-assistant.exe'),
            'bootstrap':verify_executable_source_boundary(bootstrap/'gkms-assistant-launcher.exe',allowed_modules=bootstrap_modules)}
        if (bootstrap/'_internal/gkms_tool').exists():
            raise ValueError('Bootstrap unexpectedly contains raw GUI/model source data')
        report['executable_build_complete'] = True
        report['phase'] = 'packaging'; save()
        candidate = output/'managed'; shutil.copytree(bootstrap, candidate)
        (candidate/'gkms-assistant-launcher.exe').rename(candidate/'GKMS-Assistant.exe')
        (candidate/'恢復 GUI.cmd').write_bytes(b'@"%~dp0GKMS-Assistant.exe" --recover\r\n')
        (candidate/'使用說明.txt').write_text(
            '平常請執行 GKMS-Assistant.exe。\n'
            '若更新後助手無法開啟，請執行「恢復 GUI.cmd」。確認後會開啟已驗證的前一版復原介面，再由你選擇回退；不會自動切換版本或啟動遊戲。\n'
            '首次安裝尚無前一版時，無法使用回退。請保留整個資料夾，勿單獨搬移 exe。\n\n'
            'Normally start GKMS-Assistant.exe. If an update cannot open, use 恢復 GUI.cmd. '
            'After confirmation, the verified previous GUI opens for explicit rollback. '
            'It does not switch versions or launch the game automatically. Keep the full folder together.\n',encoding='utf-8')
        bootstrap_licenses,bootstrap_missing=_copy_licenses(candidate/'licenses/bootstrap',
            ['pyinstaller']+list(_analyzed_distributions(build_root/'work/gkms-assistant-launcher')),include_tk=False)
        if bootstrap_missing:
            report['blockers'].append({'code':'BOOTSTRAP_LICENSE_PENDING','distributions':bootstrap_missing})
        write_json(candidate/'licenses/bootstrap/manifest.json',{'schema':'gkms.python-runtime-license-inventory.v1','files':bootstrap_licenses})
        report['bootstrap_packaging']={'raw_application_source_data_included':False,'gui_or_model_modules_included':False,
            'allowed_modules':sorted(bootstrap_modules),'files':public_manifest(bootstrap),
            'bytes':sum(path.stat().st_size for path in bootstrap.rglob('*') if path.is_file())}
        payload = output/'gui-payload'; shutil.copytree(gui, payload)
        # Compile the small native presentation child from the exact exported
        # source. It neither embeds another controller nor opens Chrome.
        window_builder=source/'tools/build_native_gui_window.py'
        result=subprocess.run([sys.executable,'-X','utf8',str(window_builder),
            '--workspace',str(source),'--output',str(payload/'ui_host'),
            '--work-dir',str(output/'native-window-compiler')],capture_output=True,text=True,encoding='utf-8')
        (output/'native-window-build.log').write_text(result.stdout+result.stderr,encoding='utf-8')
        if result.returncode:raise ValueError('Native application window build failed; see native-window-build.log')
        report['native_window']=json.loads(result.stdout.strip().splitlines()[-1])
        # Existing check_source reads the original GUI callbacks; numerical
        # validators read raw package sources beside their frozen __file__ paths.
        (payload/'src/gkms_tool').mkdir(parents=True)
        shutil.copyfile(source/'src/gkms_tool/gui.py', payload/'src/gkms_tool/gui.py')
        assets = payload/'assets/model_runtime'; assets.mkdir(parents=True)
        for name in model_files: shutil.copyfile(Path(model_assets)/name, assets/name)
        loadout = payload/'assets/loadout'; loadout.mkdir()
        for name in loadout_files: shutil.copyfile(Path(loadout_assets)/name, loadout/name)
        outer=payload/'assets/outer_runtime';outer.mkdir()
        for name in outer_files:
            target=outer/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(Path(outer_assets)/name,target)
        labels=payload/'assets/display_labels';labels.mkdir()
        for name in label_files:shutil.copyfile(Path(display_labels)/name,labels/name)
        if control_package is not None:
            control = payload/'assets/control'; control.mkdir()
            shutil.copyfile(control_package, control/'gkms-control.zip')
        requirements = tomllib.loads((source/'pyproject.toml').read_text())['project']['dependencies']
        licenses, missing = _copy_licenses(payload/'licenses',
            [re.split(r'[<>=!~\[]', req)[0] for req in requirements] + ['pyinstaller'] + list(_analyzed_distributions(build_root)))
        if missing: report['blockers'].append({'code': 'THIRD_PARTY_LICENSE_DOCUMENT_PENDING', 'distributions': missing})
        for name in ('LICENSE', 'LICENSE.txt', 'LICENSE.md', 'NOTICE.txt', 'NOTICE.md'):
            if (source/name).is_file():
                target = payload/'licenses/project'/name; target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source/name, target)
        write_json(payload/'licenses/manifest.json', {'schema': 'gkms.python-runtime-license-inventory.v1', 'files': licenses})
        package = {'schema': 'gkms.app-package.v1', **IDENTITY, 'version': VERSION,
            'build_profile': 'public', 'development_tools': False, 'entrypoint': 'gkms-assistant.exe',
            'files': public_manifest(payload)}
        write_json(payload/'gkms-app-package.json', package); verify_slot(payload)
        gui_zip = output/f'gkms-assistant-gui-{VERSION}-{PLATFORM}.zip'; _archive(payload, gui_zip)
        slot = candidate/'versions'/(VERSION + '-' + digest(gui_zip)[:16]); slot.parent.mkdir()
        shutil.copytree(payload, slot); initialize_managed_layout(candidate, slot)
        report['package_validation_complete'] = True
        report['phase'] = 'self-checking'; save()
        check = output/'gui-self-check.json'
        environment = dict(os.environ); environment.update(GKMS_APP_HOME=str(candidate), GKMS_APP_ROOT=str(slot),
            GKMS_USER_DATA_ROOT=str(output/'self-check-user-state'), LOCALAPPDATA=str(output/'self-check-localappdata'),
            PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([str(slot/'gkms-assistant.exe'), '--self-check-output', str(check)],
            cwd=slot, env=environment, creationflags=subprocess.CREATE_NO_WINDOW, check=False, timeout=180)
        report['self_check_complete'] = result.returncode == 0 and check.is_file() and json.loads(check.read_text())['passed'] is True
        if not report['self_check_complete']: report['blockers'].append({'code': 'PACKAGED_GUI_SELF_CHECK_FAILED'})
        boot_check = output/'bootstrap-self-check.json'
        boot = subprocess.run([str(candidate/'GKMS-Assistant.exe'), '--self-check-output', str(boot_check)],
            cwd=candidate, env=environment, creationflags=subprocess.CREATE_NO_WINDOW, check=False, timeout=180)
        report['bootstrap_self_check_complete'] = boot.returncode == 0 and boot_check.is_file() and json.loads(boot_check.read_text())['passed'] is True
        if not report['bootstrap_self_check_complete']: report['blockers'].append({'code': 'BOOTSTRAP_SELF_CHECK_FAILED'})
        _archive(candidate, output/f'gkms-assistant-{VERSION}-{PLATFORM}.zip')
        release = {'schema': 'gkms.app-release.v1', **IDENTITY, 'version': VERSION,
            'package': {'name': gui_zip.name, 'sha256': digest(gui_zip), 'size': gui_zip.stat().st_size}}
        write_json(output/'gkms-assistant-gui-release.json', release)
        report['candidate_complete'] = report['self_check_complete'] and report['bootstrap_self_check_complete']
        # Compilation and offline load checks are not the owner's final public
        # acceptance, native lifecycle validation, or authorization to publish.
        report['phase'] = 'candidate-complete' if report['candidate_complete'] else 'blocked'
        save(); return report
    except Exception as error:
        report['blockers'].append({'code': 'BUILD_FAILED', 'reason': str(error)})
        report['phase'] = 'blocked'
        save(); return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=WORKSPACE)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-only', action='store_true')
    parser.add_argument('--model-assets', type=Path)
    parser.add_argument('--model-manifest-sha256', default=MODEL_SHA)
    parser.add_argument('--loadout-assets', type=Path)
    parser.add_argument('--outer-assets', type=Path)
    parser.add_argument('--display-labels', type=Path)
    parser.add_argument('--control-package', type=Path)
    parser.add_argument('--control-sha256')
    parser.add_argument('--native-source-receipt', type=Path)
    args = parser.parse_args(argv)
    result = build(args.workspace, args.output, model_assets=args.model_assets,
        model_manifest_sha256=args.model_manifest_sha256, loadout_assets=args.loadout_assets, outer_assets=args.outer_assets, display_labels=args.display_labels, source_only=args.source_only,
        control_package=args.control_package, control_sha256=args.control_sha256,
        native_source_receipt=args.native_source_receipt)
    print(json.dumps({key: result[key] for key in ('source_export_complete', 'executable_build_complete',
        'package_validation_complete', 'self_check_complete', 'release_ready', 'blockers')}, ensure_ascii=False))
    return 0 if result['source_export_complete'] and (args.source_only or result['candidate_complete']) else 2


if __name__ == '__main__':
    raise SystemExit(main())
