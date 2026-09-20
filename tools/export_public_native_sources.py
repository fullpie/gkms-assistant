"""Corresponding public bridge/recorder source plus audited Localify archive.

Select existing public preprocessor branches without changing their active text.
Never copy generated research profiles, probe sources, binaries or build caches.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

DEFINES = {'GKMS_PUBLIC_PORTABLE': True, 'GKMS_RUNTIME_COMMAND_TRACE': False,
    'GKMS_DIRECT_REPLAY_RESEARCH': False, 'GKMS_OFFICIAL_REPLAY_CORE': False,
    'GKMS_RUNTIME_LEGAL_CANDIDATE_PROBE': False, 'GKMS_RUNTIME_LEGAL_VERIFIED_PROBE': False,
    'GKMS_RUNTIME_HAND_VALIDATOR_PROBE': False, 'GKMS_RUNTIME_VALIDATOR_ABI_PROBE': False,
    'GKMS_RUNTIME_PASSIVE_POLICY_PROBE': False}
PRIVATE = re.compile(rb'(?i)[a-z]:[\\/]+(?:users[\\/]|gkms_tool)')
SOURCE_SUFFIXES = {'.cpp', '.c', '.hpp', '.h', '.asm', '.def'}


def sha(data): return hashlib.sha256(data).hexdigest()


def select_public(raw):
    result, stack = [], []
    for line in raw.decode('utf-8-sig').replace('\r\n', '\n').splitlines(keepends=True):
        directive = re.match(r'^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)$', line)
        enabled = all(frame[1] for frame in stack)
        if not directive:
            if enabled: result.append(line)
            continue
        kind, condition = directive.groups(); condition = re.sub(r'\s+', '', condition)
        if kind in ('if', 'ifdef', 'ifndef'):
            known = None
            if kind in ('ifdef','ifndef') and condition in DEFINES:
                known = DEFINES[condition] if kind == 'ifdef' else not DEFINES[condition]
            elif kind == 'if':
                match = re.fullmatch(r'(!?)defined\((\w+)\)', condition)
                if match and match[2] in DEFINES:
                    known = DEFINES[match[2]] if not match[1] else not DEFINES[match[2]]
            if known is None and enabled: result.append(line)
            stack.append([known, known if known is not None else True])
        elif kind == 'endif':
            if not stack: raise ValueError('Unbalanced public source guard')
            known, _ = stack.pop()
            if known is None and all(frame[1] for frame in stack): result.append(line)
        else:
            if not stack: raise ValueError('Unbalanced public source branch')
            if stack[-1][0] is not None:
                if kind == 'elif': raise ValueError('Public elif needs explicit review')
                stack[-1][1] = not stack[-1][1]
            elif enabled: result.append(line)
    if stack: raise ValueError('Unclosed public source guard')
    return ''.join(result).encode()


def export(workspace, output, receipt_path):
    workspace, output = Path(workspace).resolve(), Path(output).resolve()
    receipt = json.loads(Path(receipt_path).read_bytes())
    if receipt.get('schema') not in {'gkms.public-native-control-validation.v1','gkms.public-native-control-validation.v2'}:
        raise ValueError('Public native validation receipt required')
    audit_ref = receipt['audit']; audit_path = Path(audit_ref['path'])
    if sha(audit_path.read_bytes()) != audit_ref['sha256']: raise ValueError('Native audit changed')
    audit = json.loads(audit_path.read_bytes())
    if audit.get('private_compiler_paths_absent_from_project_DLLs') is not True:
        raise ValueError('Private native compiler paths are not qualified')
    archive_ref = receipt['localify_source_archive']; archive_path = Path(archive_ref['path'])
    if sha(archive_path.read_bytes()) != archive_ref['sha256']: raise ValueError('Localify source archive changed')
    output.mkdir(parents=True, exist_ok=False)
    vendor = output/'third_party/localify'; vendor.mkdir(parents=True)
    with zipfile.ZipFile(archive_path) as archive:
        seen = set()
        for row in archive.infolist():
            name = row.filename; path = Path(name)
            if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name or name.casefold() in seen:
                raise ValueError('Unsafe native source archive member')
            seen.add(name.casefold())
            if row.is_dir(): continue
            if path.suffix.lower() in ('.dll','.exe','.pdb','.obj','.lib') or PRIVATE.search(archive.read(row)):
                raise ValueError('Native source archive contains a binary/private path')
            target = vendor/path; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(archive.read(row))
    roots = [workspace/'native/runtime_command_bridge/src', workspace/'native/runtime_exam_recorder/src',
             workspace/'native/telemetry_observer/src', workspace/'native/shared']
    selected, projections, projects = {}, [], {}
    def include(path):
        path = path.resolve()
        if path in selected: return
        if not path.is_file() or not path.is_relative_to(workspace/'native'):
            raise ValueError('Native source outside explicit roots: '+str(path))
        if path.name == 'pc_generated_method_profile.hpp' or 'probe' in path.name:
            raise ValueError('Research source reached public closure: '+path.name)
        raw = path.read_bytes(); public = select_public(raw)
        if PRIVATE.search(public): raise ValueError('Unresolved private native source path: '+path.name)
        if select_public(public) != public: raise ValueError('Public source projection is not stable')
        relative = path.relative_to(workspace)
        target = output/relative; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(public)
        selected[path] = relative.as_posix()
        projections.append({'path':relative.as_posix(),'original_sha256':sha(raw),'exported_sha256':sha(public),
            'operation':'select-existing-public-build-branches','active_branch_text_preserved':True})
        for name in re.findall(rb'^\s*#\s*include\s*"([^"\r\n]+)"', public, re.M):
            name = name.decode(); candidates = [path.parent/name, *[root/name for root in roots]]
            child = next((p for p in candidates if p.is_file()), None)
            if child is not None: include(child)
            elif not any((base/name).is_file() for base in (vendor/'src',vendor/'src/deps',vendor/'deps/minhook/include')):
                raise ValueError('Public native include closure missing: '+name)
    for project in audit['build_projects']:
        component = project['component']
        if component == 'loader': continue
        if project.get('research_sources_excluded') is not True: raise ValueError('Research compiler inputs are not excluded')
        compilation = []
        for name in project['compiled_sources']:
            source = next((root/name for root in roots if (root/name).is_file()), None)
            if source is not None:
                include(source); compilation.append(selected[source.resolve()])
            else:
                matches = list((vendor/'deps/minhook/src').rglob(name))
                if len(matches) != 1: raise ValueError('Actual native compiler input missing: '+name)
                compilation.append(matches[0].relative_to(output).as_posix())
        projects[component] = compilation
    lines = ['workspace "gkms_public_control"','    location "build/projects"','    architecture "x86_64"',
        '    configurations { "Release" }','    system "windows"','    systemversion "latest"']
    for component, files in projects.items():
        name = 'gkms_'+component
        lines += [f'project "{name}"','    kind "SharedLib"','    language "C++"','    cppdialect "C++20"',
            '    characterset "Unicode"','    staticruntime "Off"','    optimize "Speed"','    symbols "On"',
            '    targetdir "build/bin"',f'    objdir "build/obj/{name}"',
            '    defines { "GKMS_PUBLIC_PORTABLE", "WIN32_LEAN_AND_MEAN", "NOMINMAX" }',
            '    includedirs { "native/runtime_command_bridge/src", "native/runtime_exam_recorder/src", "native/telemetry_observer/src", "native/shared", "third_party/localify/src/deps", "third_party/localify/deps/minhook/include", "third_party/localify/deps/minhook/src" }',
            '    links { "Bcrypt", "Psapi" }',
            '    buildoptions { "/experimental:deterministic", \'/pathmap:"\' .. path.getabsolute(".") .. \'=public-source"\' }',
            f'    linkoptions {{ "/PDBALTPATH:{name}.pdb" }}',
            '    files { '+', '.join(json.dumps(name) for name in files)+' }']
    (output/'premake5.lua').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    (output/'README.md').write_text(
        '# Public native control corresponding source\n\n'
        'The source closure follows the compiler inputs of the audited public bridge and normal action recorder. '
        'Only existing public preprocessor branches are selected; research profiles/probes are not included. '
        'The normal recorder is required for action settlement.\n\n'
        'On Windows x64 with Visual Studio 2022 C++ tools, Windows SDK and Premake5 on PATH, run '
        '`premake5 vs2022`, then `msbuild build/projects/gkms_public_control.sln /m /p:Configuration=Release /p:Platform=x64`. '
        'This builds only bridge/recorder under build/bin and never installs or starts a game. '
        'Build the Localify loader separately using third_party/localify/README.md and its pinned Conan recipe.\n\n'
        'Original project portions are all rights reserved. Third-party and derivative components retain their existing licenses; '
        'see third_party/localify/LICENSE-INVENTORY.json and retained inline notices. '
        'This source closure is not a claim of byte-identical reproduction or live workflow validation.\n', encoding='utf-8')
    files = {p.relative_to(output).as_posix():sha(p.read_bytes()) for p in sorted(output.rglob('*')) if p.is_file()}
    manifest = {'schema':'gkms.public-native-corresponding-source.v1','control_archive_sha256':receipt['control_archive']['sha256'],
        'localify_source_archive_sha256':archive_ref['sha256'],'public_profiles_only':True,'test_or_research_DLLs_included':False,
        'compiler_projects':projects,'source_projections':projections,'files':files,'clean_rebuild_verified':False,
        'game_io':False,'deployed':False}
    (output/'SOURCE-MANIFEST.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    return manifest


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--receipt',type=Path,required=True)
    args=parser.parse_args();value=export(args.workspace,args.output,args.receipt)
    print(json.dumps({'files':len(value['files']),'projects':list(value['compiler_projects']),'clean_rebuild_verified':False}))
