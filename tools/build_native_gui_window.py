"""Build the small WinForms/WebView2 presentation host, not another game owner."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import urllib.request
import zipfile
import ast
import re

ROOT = Path(__file__).resolve().parents[1]
VERSION = '1.0.4191.47'
PACKAGE_SHA = 'f492bbf547d0da329553b6727435b677579b1e9f91cc9e4a1ad029366d5f23d0'
PACKAGE_URL = 'https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/' + VERSION + '/microsoft.web.webview2.' + VERSION + '.nupkg'
MEMBERS = {'Microsoft.Web.WebView2.Core.dll':'lib/net462/Microsoft.Web.WebView2.Core.dll',
           'Microsoft.Web.WebView2.WinForms.dll':'lib/net462/Microsoft.Web.WebView2.WinForms.dll',
           'WebView2Loader.dll':'runtimes/win-x64/native/WebView2Loader.dll', 'LICENSE-WebView2.txt':'LICENSE.txt'}

def digest(path):
    with Path(path).open('rb') as stream: return hashlib.file_digest(stream,'sha256').hexdigest()

def build(output, *, workspace=ROOT, package=None, work_dir=None):
    workspace,output=Path(workspace).resolve(),Path(output).resolve()
    output.mkdir(parents=True,exist_ok=False)
    work_dir=Path(work_dir).resolve() if work_dir else output.parent/(output.name+'-compiler')
    work_dir.mkdir(parents=True,exist_ok=True)
    source=workspace/'native/gui_window/Program.cs'
    win32_manifest=workspace/'native/gui_window/app.manifest'
    if not win32_manifest.is_file():raise ValueError('Native window compatibility manifest is required')
    if package is None:
        package=work_dir/('microsoft.web.webview2.'+VERSION+'.nupkg')
        if not package.exists():
            with urllib.request.urlopen(PACKAGE_URL,timeout=45) as response:
                raw=response.read(16*1024**2+1)
            if len(raw)>16*1024**2:raise ValueError('WebView2 SDK exceeds bounded package size')
            package.write_bytes(raw)
    if digest(package)!=PACKAGE_SHA:raise ValueError('Pinned Microsoft WebView2 SDK archive differs')
    with zipfile.ZipFile(package) as archive:
        for target,member in MEMBERS.items():
            (output/target).write_bytes(archive.read(member))
    compiler=Path(os.environ.get('WINDIR','C:/Windows'))/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    if not compiler.is_file():raise ValueError('.NET Framework 4.x C# compiler is required for the Windows host build')
    tree=ast.parse((workspace/'src/gkms_tool/app_version.py').read_text(encoding='utf-8'))
    versions=[node.value.value for node in tree.body if isinstance(node,ast.Assign) and
        any(isinstance(target,ast.Name) and target.id=='VERSION' for target in node.targets) and isinstance(node.value,ast.Constant)]
    if len(versions)!=1 or not re.fullmatch(r'\d+\.\d+\.\d+',versions[0]):raise ValueError('Single application version is required')
    version=versions[0]
    version_source=work_dir/'version.cs'
    version_source.write_text('using System.Reflection;\n[assembly:System.Runtime.Versioning.TargetFramework(".NETFramework,Version=v4.7.2")]\n[assembly:AssemblyTitle("GKMS Assistant")]\n'
        '[assembly:AssemblyProduct("GKMS Assistant")]\n[assembly:AssemblyCompany("fullpie")]\n'
        '[assembly:AssemblyVersion("'+version+'.0")]\n[assembly:AssemblyFileVersion("'+version+'.0")]\n',encoding='utf-8')
    command=[str(compiler),'/nologo','/target:winexe','/platform:x64','/optimize+',
        '/win32manifest:'+str(win32_manifest),
        '/out:'+str(output/'GKMS-Window.exe'),'/r:System.Windows.Forms.dll','/r:System.Drawing.dll',
        '/r:System.Runtime.Serialization.dll','/r:'+str(compiler.parent/'System.Net.Http.dll'),
        '/r:'+str(output/'Microsoft.Web.WebView2.Core.dll'),
        '/r:'+str(output/'Microsoft.Web.WebView2.WinForms.dll'),str(source),str(version_source)]
    result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace')
    (work_dir/'build.log').write_text(result.stdout+result.stderr,encoding='utf-8')
    if result.returncode:raise RuntimeError('Native GUI build failed; inspect the retained build log')
    (output/'GKMS-Window.exe.config').write_text('<?xml version="1.0"?><configuration><startup useLegacyV2RuntimeActivationPolicy="true"><supportedRuntime version="v4.0" sku=".NETFramework,Version=v4.7.2"/></startup><System.Windows.Forms.ApplicationConfigurationSection><add key="DpiAwareness" value="PerMonitorV2" /></System.Windows.Forms.ApplicationConfigurationSection></configuration>',encoding='utf-8')
    manifest={'schema':'gkms.native-window-package.v1','presentation':'native-webview2','game_io':False,
        'framework':'.NET Framework 4.7.2+','runtime':'Microsoft Edge WebView2 Evergreen',
        'preferred_dpi_awareness':'PerMonitorV2','win32_manifest_sha256':digest(win32_manifest),
        'application_version':version,'sdk_version':VERSION,'sdk_sha256':PACKAGE_SHA,'sdk_url':PACKAGE_URL,
        'source_sha256':digest(source),'files':{p.name:digest(p) for p in sorted(output.iterdir()) if p.is_file()}}
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    return {'output':str(output),'manifest_sha256':digest(output/'manifest.json'),'files':len(manifest['files']),
        'source_sha256':manifest['source_sha256'],'process_started':False,'game_io':False}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--workspace',type=Path,default=ROOT)
    parser.add_argument('--package',type=Path)
    parser.add_argument('--work-dir',type=Path)
    args=parser.parse_args()
    print(json.dumps(build(args.output,workspace=args.workspace,package=args.package,work_dir=args.work_dir)))
