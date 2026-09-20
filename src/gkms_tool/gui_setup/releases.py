"""Read-only GitHub/Gitea-compatible release discovery and bounded HTTPS downloads."""
from __future__ import annotations
import http.client, ipaddress, json, re, socket, ssl
from urllib.parse import urlsplit, urlunsplit, urljoin, quote, unquote
from .packages import SetupError, digest, MAX_ARCHIVE

DEFAULT_TRANSLATION_API='https://uma.chinosk6.cn/api/gkms_trans_data'
LOADER_SOURCE='https://git.chinosk6.cn/chinosk/gkms-local/releases'

def source_url(raw: str) -> dict:
    if not isinstance(raw,str) or len(raw)>2048:raise SetupError('Invalid release URL.')
    u=urlsplit(raw.strip())
    if u.scheme!='https' or not u.hostname or u.port not in (None,443) or u.username or u.password or u.fragment:
        raise SetupError('Use a public HTTPS release URL without credentials.')
    if u.query:raise SetupError('Use a release API without query parameters or credentials.')
    p=u.path.strip('/').split('/')
    if u.hostname=='github.com':
        if len(p)<2 or (len(p)>2 and p[2]!='releases'):raise SetupError('Expected a GitHub repository/release URL.')
        owner,repo=p[:2];repo=repo.removesuffix('.git')
        if not all(re.fullmatch(r'[A-Za-z0-9_.-]+',x) and x not in ('.','..') for x in (owner,repo)):raise SetupError('Invalid repository name.')
        return {'api':f'https://api.github.com/repos/{owner}/{repo}/releases/latest','repository':f'{owner}/{repo}','raw':raw.strip(),'provider':'github'}
    if u.hostname=='api.github.com':
        if len(p)<4 or p[0]!='repos' or p[3]!='releases':raise SetupError('Expected GitHub Releases API.')
        return source_url('https://github.com/'+p[1]+'/'+p[2]+'/releases')
    if u.hostname=='git.chinosk6.cn':
        if p[:3]==['api','v1','repos'] and len(p)>=6 and p[5]=='releases':owner,repo=p[3:5]
        elif len(p)>=2 and (len(p)==2 or p[2]=='releases'):owner,repo=p[:2]
        else:raise SetupError('Expected Chinosk Gitea repository/release URL.')
        if not all(re.fullmatch(r'[A-Za-z0-9_.-]+',x) and x not in ('.','..') for x in (owner,repo)):raise SetupError('Invalid repository name.')
        return {'api':f'https://git.chinosk6.cn/api/v1/repos/{owner}/{repo}/releases','repository':f'git.chinosk6.cn/{owner}/{repo}','raw':raw.strip(),'provider':'gitea'}
    # Custom API is a GitHub-release-shaped JSON endpoint, not an LLM/key service.
    return {'api':raw.strip(),'repository':u.hostname+u.path,'raw':raw.strip(),'provider':'custom'}

def public_addresses(host: str) -> list[str]:
    try:ips=list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)))
    except OSError as e:raise SetupError('Cannot resolve release/download host.') from e
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise SetupError('Private, loopback, link-local, and reserved network destinations are forbidden.')
    return ips

class PinnedHTTPS(http.client.HTTPSConnection):
    def connect(self):
        # DNS is resolved once; the actual connection uses that public IP while TLS
        # SNI/certificate validation still uses the original hostname (no rebinding).
        addresses=public_addresses(self.host)
        for ip in addresses:
            try:
                sock=socket.create_connection((ip,443),self.timeout)
                self.sock=self._context.wrap_socket(sock,server_hostname=self.host)
                return
            except OSError:
                try:sock.close()
                except UnboundLocalError:pass
        raise SetupError('Could not establish a verified HTTPS connection.')

def https_get(url: str,limit: int,asset: bool=False) -> bytes:
    allowed={urlsplit(url).hostname,'github.com','api.github.com','objects.githubusercontent.com','release-assets.githubusercontent.com','git.chinosk6.cn'}
    current=url
    for _ in range(6):
        u=urlsplit(current)
        if u.scheme!='https' or not u.hostname or u.port not in (None,443) or u.hostname not in allowed or u.username or u.password:
            raise SetupError('Unsafe or unapproved download redirect.')
        connection=PinnedHTTPS(u.hostname,timeout=25,context=ssl.create_default_context())
        try:
            path=urlunsplit(('', '', u.path or '/',u.query,''))
            connection.request('GET',path,headers={'User-Agent':'GKMS-Setup/3.0','Accept':'application/octet-stream' if asset else 'application/json','Accept-Encoding':'identity'})
            response=connection.getresponse()
            if response.status in (301,302,303,307,308):
                dest=response.getheader('Location')
                if not dest:raise SetupError('Release redirect has no destination.')
                current=urljoin(current,dest);continue
            if response.status in (403,429):raise SetupError('Release API rate limit or access restriction; existing version is unchanged.')
            if response.status!=200:raise SetupError(f'Release/download returned HTTP {response.status}.')
            length=response.getheader('Content-Length')
            if length and int(length)>limit:raise SetupError('Download size exceeds limit.')
            chunks=[];total=0
            while True:
                chunk=response.read(min(1024**2,limit-total+1))
                if not chunk:break
                total+=len(chunk)
                if total>limit:raise SetupError('Download size exceeds limit.')
                chunks.append(chunk)
            return b''.join(chunks)
        except (OSError,http.client.HTTPException,ValueError) as e:
            raise SetupError('Network/TLS read failed; existing installation is unchanged.') from e
        finally:connection.close()
    raise SetupError('Too many release redirects.')

def normalize_release(data,source):
    if isinstance(data,list):
        data=[r for r in data if isinstance(r,dict) and not r.get('draft') and not r.get('prerelease')]
        if not data:raise SetupError('No stable release was returned.')
        data.sort(key=lambda r:r.get('published_at') or r.get('created_at') or '',reverse=True)
        data=data[0]
    if not isinstance(data,dict) or data.get('draft') or data.get('prerelease') or not isinstance(data.get('tag_name'),str) or not isinstance(data.get('assets'),list):
        raise SetupError('Expected GitHub/Gitea-compatible release JSON (tag_name and assets).')
    tag=data['tag_name'];rid=data.get('id')
    if not tag or len(tag)>200 or not isinstance(rid,int):raise SetupError('Invalid release identity.')
    assets=[]
    for a in data['assets']:
        if not isinstance(a,dict) or not isinstance(a.get('id'),int):continue
        name=a.get('name','');url=a.get('browser_download_url','')
        if not name.lower().endswith('.zip') or '/' in name or '\\' in name:continue
        u=urlsplit(url)
        if u.scheme!='https' or not u.hostname or u.username or u.password or u.fragment:continue
        if source['provider']=='github':
            p=u.path.strip('/').split('/')
            if u.hostname!='github.com' or len(p)<6 or '/'.join(p[:2]).lower()!=source['repository'].lower() or p[2:4]!=['releases','download']:continue
        sha=a.get('digest')
        if not isinstance(sha,str) or not re.fullmatch(r'sha256:[a-fA-F0-9]{64}',sha):sha=None
        assets.append({'id':a['id'],'name':name,'url':url,'size':a.get('size'),'digest':sha.lower() if sha else None,'updatedAt':a.get('updated_at')})
    page=data.get('html_url','')
    if not isinstance(page,str) or not page.startswith('https://'):page=source['raw']
    return {'repository':source['repository'],'repositoryKey':source['repository'].lower(),'id':rid,'tag':tag,'publishedAt':data.get('published_at'),
            'page':page,'assets':assets,'sourceUrl':source['raw'],'provider':source['provider']}

def latest(raw: str) -> dict:
    source=source_url(raw)
    try:data=json.loads(https_get(source['api'],4*1024**2))
    except (ValueError,UnicodeError) as e:raise SetupError('API returned HTML or invalid JSON; no update was installed.') from e
    return normalize_release(data,source)

def select_latest_asset(release: dict) -> dict | None:
    """Resolve one translation ZIP from the latest release without a user selector.

    Match the author's data-package filename, otherwise accept exactly one ZIP.
    Multiple unknown attachments are refused, never picked by API ordering.
    """
    assets=release.get('assets',[])
    canonical=[a for a in assets if isinstance(a.get('name'),str) and a['name'].lower()=='gakumastranslationdata.zip']
    if len(canonical)==1:return canonical[0]
    if canonical:return None
    return assets[0] if len(assets)==1 else None

def download_selected(raw: str,release_id: int,asset_id: int) -> tuple[dict,dict,bytes]:
    release=latest(raw)
    if release['id']!=release_id:raise SetupError('Release changed after confirmation; check again.')
    selected=select_latest_asset(release)
    if not selected:raise SetupError('Cannot identify a unique translation ZIP in the latest release. Check the source.')
    if selected['id']!=asset_id:raise SetupError('Latest translation ZIP changed after confirmation; check again.')
    data=https_get(selected['url'],MAX_ARCHIVE,True)
    if selected['digest'] and 'sha256:'+digest(data)!=selected['digest']:raise SetupError('Release archive SHA-256 mismatch.')
    return release,selected,data
