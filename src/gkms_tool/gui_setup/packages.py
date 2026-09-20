"""Strict package inspection. ZIPs are never blindly extracted into the game."""
from __future__ import annotations
import hashlib, io, json, re, stat, struct, zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

class SetupError(Exception):
    """An actionable failure; callers must not interpret it as an installed state."""

MAX_ARCHIVE = 256 * 1024**2
MAX_EXPANDED = 768 * 1024**2
MAX_FILE = 128 * 1024**2
MAX_ENTRIES = 30000
RESERVED = re.compile(r'^(CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])(?:\.|$)', re.I)
EXECUTABLE = {'.dll','.exe','.sys','.com','.scr','.msi','.bat','.cmd','.ps1','.vbs','.js','.lnk','.so','.dylib','.apk','.py','.html','.hta'}
BRIDGE_NAMES = {'gkms_runtime_command_bridge.dll','gkms_runtime_exam_recorder.dll','gkms_runtime_outer_observer.dll'}
PUBLIC_RUNTIME_DEPENDENCIES = {'msvcp140.dll', 'concrt140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'}

def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def relative_name(name: str) -> str:
    if not isinstance(name,str) or not name or len(name)>220:
        raise SetupError('Invalid or excessively long package path.')
    name=name.replace('\\','/')
    if name.startswith('/') or any(ord(c)<32 for c in name) or any(c in name for c in ':<>"|?*'):
        raise SetupError('Unsafe Windows path in package.')
    parts=name.split('/')
    if any(p in ('','.','..') or p.endswith((' ','.')) or RESERVED.match(p) for p in parts):
        raise SetupError('Unsafe Windows path segment in package.')
    return '/'.join(parts)

@dataclass
class Package:
    module: str
    version: str
    files: dict[str,bytes]
    metadata: dict

def read_zip(data: bytes) -> dict[str,bytes]:
    if len(data)>MAX_ARCHIVE: raise SetupError('ZIP exceeds download limit.')
    result={}; seen=set(); total=0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos=z.infolist()
            if len(infos)>MAX_ENTRIES: raise SetupError('ZIP contains too many entries.')
            # Validate EVERY entry, including entries not selected for installation.
            for info in infos:
                name=relative_name(info.orig_filename.rstrip('/'))
                mode=info.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0,stat.S_IFREG,stat.S_IFDIR)):
                    raise SetupError('ZIP links and special files are not supported.')
                if info.flag_bits & 1: raise SetupError('Encrypted ZIPs are not supported.')
                key=name.casefold()
                if key in seen: raise SetupError('Case-insensitive duplicate ZIP path.')
                seen.add(key)
                if info.is_dir(): continue
                total+=info.file_size
                if info.file_size>MAX_FILE or total>MAX_EXPANDED:
                    raise SetupError('ZIP expanded size exceeds limit.')
                if info.file_size>1024**2 and info.file_size>max(info.compress_size,1)*1000:
                    raise SetupError('Suspicious ZIP compression ratio.')
                with z.open(info) as f: blob=f.read(MAX_FILE+1)
                if len(blob)!=info.file_size: raise SetupError('ZIP size or CRC mismatch.')
                result[name]=blob
    except (zipfile.BadZipFile,RuntimeError,NotImplementedError,EOFError) as e:
        raise SetupError('Invalid, corrupt, or unsupported ZIP.') from e
    keys={k.casefold() for k in result}
    for key in keys:
        parents=PurePosixPath(key).parents
        if any(str(p) in keys for p in parents): raise SetupError('ZIP file/directory collision.')
    if not result: raise SetupError('ZIP has no files.')
    return result

def same_version(a: str,b: str) -> bool:
    return a.strip().removeprefix('v').removeprefix('V') == b.strip().removeprefix('v').removeprefix('V')

def translation_package(data: bytes, release_tag: str) -> Package:
    blobs=read_zip(data)
    roots=set()
    for name in blobs:
        p=name.split('/')
        if 'local-files' in p:
            roots.add('/'.join(p[:p.index('local-files')]))
    if len(roots)!=1: raise SetupError('Expected exactly one local-files/ translation directory.')
    root=next(iter(roots)); prefix=(root+'/' if root else '')
    version_name=prefix+'version.txt'
    if version_name not in blobs: raise SetupError('Missing version.txt beside local-files/. No installation performed.')
    try: version=blobs[version_name].decode('utf-8-sig').strip()
    except UnicodeError as e: raise SetupError('Invalid resource version encoding.') from e
    if not re.fullmatch(r'[A-Za-z0-9_.+\-]{1,100}',version): raise SetupError('Invalid resource version.')
    if not same_version(version,release_tag):
        raise SetupError(f'ZIP version.txt ({version}) does not match release tag ({release_tag}).')
    output={'gakumas-local/version.txt':blobs[version_name]}
    for name,blob in blobs.items():
        if name.startswith(prefix+'local-files/'):
            rel=name[len(prefix):]
            if PurePosixPath(rel).suffix.lower() in EXECUTABLE:
                raise SetupError('Executable/script found inside translation resources.')
            output['gakumas-local/'+rel]=blob
    if len(output)<2: raise SetupError('Empty translation directory.')
    return Package('translation',release_tag,output,{'packageVersion':version,'layout':'chinosk-local-files-v1'})

def pe_dll(data: bytes) -> bool:
    try:
        if data[:2]!=b'MZ' or len(data)<512: return False
        off=struct.unpack_from('<I',data,0x3c)[0]
        return data[off:off+4]==b'PE\0\0' and struct.unpack_from('<H',data,off+4)[0]==0x8664 and bool(struct.unpack_from('<H',data,off+22)[0]&0x2000)
    except (struct.error,IndexError): return False

def control_package(data: bytes, gameassembly_sha256: str) -> Package:
    blobs=read_zip(data)
    if 'gkms-package.json' not in blobs: raise SetupError('Select a portable control ZIP created by native/make_control_package.py, not a bare or stock Localify DLL.')
    try: spec=json.loads(blobs['gkms-package.json'].decode('utf-8-sig'))
    except (ValueError,UnicodeError) as e: raise SetupError('Invalid control manifest.') from e
    public = spec.get('schema') == 'gkms.control-package.v2'
    if (spec.get('schema'), spec.get('layout')) not in {
            ('gkms.control-package.v1', 'gkms-portable-v1'), ('gkms.control-package.v2', 'gkms-portable-v2')}:
        raise SetupError('Unsupported control package; hard-coded legacy loader is not portable.')
    if spec.get('gameassembly_sha256','').lower()!=gameassembly_sha256.lower():
        raise SetupError('Control package was not built/approved for this GameAssembly.dll.')
    version=spec.get('version','')
    if not re.fullmatch(r'[A-Za-z0-9_.+\-]{1,100}',version): raise SetupError('Invalid control version.')
    manifest=spec.get('files')
    if not isinstance(manifest,dict): raise SetupError('Invalid control file inventory.')
    allowed={'version.dll'}|{'gkms/native/'+n for n in BRIDGE_NAMES}
    required={'version.dll','gkms/native/gkms_runtime_command_bridge.dll'}
    if public:
        required |= {'gkms/native/gkms_runtime_exam_recorder.dll'} | PUBLIC_RUNTIME_DEPENDENCIES
        allowed = required
        expected_roles = {name: 'runtime-dependency' for name in PUBLIC_RUNTIME_DEPENDENCIES}
        expected_roles.update({'version.dll': 'loader', 'gkms/native/gkms_runtime_command_bridge.dll': 'command-bridge',
                              'gkms/native/gkms_runtime_exam_recorder.dll': 'action-recorder'})
        if (spec.get('build_profile') != 'public' or spec.get('development_tools') is not False
                or spec.get('roles') != expected_roles or spec.get('features') != {
                    'direct_replay_research': False, 'official_replay_core': False, 'optional_probes': False}):
            raise SetupError('Public control component roles or feature boundary are invalid.')
    if not required.issubset(manifest) or not set(manifest).issubset(allowed):
        raise SetupError('Unexpected/missing loader or control DLL path.')
    files={}
    for name,expected in manifest.items():
        blob=blobs.get(name)
        if not isinstance(expected,str) or blob is None or digest(blob)!=expected.lower() or not pe_dll(blob):
            raise SetupError('Control DLL hash or x64 PE format mismatch.')
        files[name]=blob
    notices = {name for name in blobs if name.startswith('licenses/') and PurePosixPath(name).suffix.lower() in {'', '.txt', '.md'}}
    if not set(blobs).issubset(set(manifest)|{'gkms-package.json','LICENSE','NOTICE.txt'}|notices):
        raise SetupError('Control ZIP contains files outside its manifest.')
    # Format/hash checks are not a signature or proof of runtime compatibility.
    return Package('control',version,files,{'layout':spec['layout'],'gameAssemblySha256':gameassembly_sha256,
                                        'buildProfile': 'public' if public else 'legacy', 'roles': spec.get('roles', {})})
