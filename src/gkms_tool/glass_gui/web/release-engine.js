/* GitHub release lookup and update decisions. No filesystem access or installation. */
'use strict';
(function(root) {
  const fail = key => Object.assign(new Error(key), {key});
  const component = value => /^[A-Za-z0-9_.-]+$/.test(value) && value!=='.' && value!=='..';
  function parseSource(raw) {
    if (typeof raw !== 'string' || !raw.trim()) throw fail('urlEmpty');
    let u; try { u=new URL(raw.trim()); } catch { throw fail('urlInvalid'); }
    if(u.protocol!=='https:' || u.port || u.username || u.password || u.hash || /\s/.test(raw.trim())) throw fail('urlInvalid');
    // Allow only benign list-pagination parameters, never credentials embedded in URLs.
    for (const [key,value] of u.searchParams) if(!['per_page','page'].includes(key)||!/^\d{1,3}$/.test(value)) throw fail('urlInvalid');
    const parts=u.pathname.replace(/\/+$/,'').split('/').filter(Boolean);
    let owner,repo,tail,assetHint='';
    if(u.hostname==='github.com') {
      [owner,repo,...tail]=parts;
      if(tail.length && tail[0]!=='releases') throw fail('urlInvalid');
      if(tail.length>1 && !['latest','tag','download'].includes(tail[1])) throw fail('urlInvalid');
      if(tail[1]==='latest' && !(tail.length===2 || (tail[2]==='download' && tail.length===4))) throw fail('urlInvalid');
      if(tail[1]==='tag' && tail.length<3) throw fail('urlInvalid');
      if(tail[1]==='download' && tail.length<4) throw fail('urlInvalid');
      if(tail[1]==='download'||tail[2]==='download') {
        try {assetHint=decodeURIComponent(tail[tail.length-1]);} catch {throw fail('urlInvalid');}
      }
    } else if(u.hostname==='api.github.com') {
      if(parts[0]!=='repos') throw fail('urlInvalid');
      [,owner,repo,...tail]=parts;
      if(tail[0]!=='releases') throw fail('urlInvalid');
      if(tail.length>1 && !['latest','tags'].includes(tail[1]) && !/^\d+$/.test(tail[1])) throw fail('urlInvalid');
      if(tail[1]==='latest' && tail.length!==2) throw fail('urlInvalid');
      if(tail[1]==='tags' && tail.length<3) throw fail('urlInvalid');
      if(/^\d+$/.test(tail[1]||'') && tail.length!==2) throw fail('urlInvalid');
    } else {
      if(u.search) throw fail('urlInvalid');
      let api=u.href, repository=u.hostname+u.pathname;
      if(u.hostname==='git.chinosk6.cn'){
        let o,r;
        if(parts[0]==='api'&&parts[1]==='v1'&&parts[2]==='repos'){[o,r]=parts.slice(3);}
        else{[o,r]=parts;}
        if(!component(o||'')||!component(r||''))throw fail('urlInvalid');
        api=`https://git.chinosk6.cn/api/v1/repos/${o}/${r}/releases`;repository=`git.chinosk6.cn/${o}/${r}`;
      }
      return Object.freeze({repository,key:repository.toLowerCase(),api,page:u.href,assetHint:'',requiresHost:true});
    }
    if(!owner||!repo||!component(owner)||!component(repo)) throw fail('urlInvalid');
    repo=repo.replace(/\.git$/i,''); if(!repo||!component(repo)) throw fail('urlInvalid');
    const repository=`${owner}/${repo}`;
    return Object.freeze({repository,key:repository.toLowerCase(),api:`https://api.github.com/repos/${repository}/releases/latest`,page:`https://github.com/${repository}/releases`,assetHint});
  }
  function assetIsSafe(asset, source) {
    if(!asset || !Number.isSafeInteger(asset.id) || asset.id<=0 || typeof asset.name!=='string' || !/\.zip$/i.test(asset.name) || /[\\/\x00-\x1f]/.test(asset.name))return false;
    let u;try{u=new URL(asset.browser_download_url);}catch{return false;}
    if(u.protocol!=='https:'||u.hostname!=='github.com'||u.port||u.username||u.password||u.hash||u.search)return false;
    const parts=u.pathname.split('/').filter(Boolean);
    return parts.length>=6 && `${parts[0]}/${parts[1]}`.toLowerCase()===source.key && parts[2]==='releases' && parts[3]==='download';
  }
  function normalizeRelease(data,source) {
    if(!data||Array.isArray(data)||!Number.isSafeInteger(data.id)||data.id<=0||typeof data.tag_name!=='string'||!data.tag_name.trim()||data.tag_name.length>200||data.draft!==false||data.prerelease!==false||!Array.isArray(data.assets)) throw fail('invalidResponse');
    const assets=data.assets.filter(a=>assetIsSafe(a,source)&&a.state==='uploaded').map(a=>({
      id:a.id,name:a.name,url:a.browser_download_url,size:Number.isFinite(a.size)?a.size:null,
      digest:typeof a.digest==='string'&&/^sha256:[a-f0-9]{64}$/i.test(a.digest)?a.digest.toLowerCase():null,
      updatedAt:typeof a.updated_at==='string'?a.updated_at:null
    }));
    return {repository:source.repository,repositoryKey:source.key,id:data.id,tag:data.tag_name,
      publishedAt:typeof data.published_at==='string'?data.published_at:null,
      page:`https://github.com/${source.repository}/releases/tag/${encodeURIComponent(data.tag_name)}`,assets};
  }
  function createClient(fetcher) {
    const cache=new Map();
    return {async latest(source,{signal}={}) {
      if(source.requiresHost)throw fail('customNeedsHost');
      const previous=cache.get(source.api), headers={Accept:'application/vnd.github+json'};
      if(previous?.etag)headers['If-None-Match']=previous.etag;
      let response;
      try {response=await fetcher(source.api,{method:'GET',headers,credentials:'omit',referrerPolicy:'no-referrer',signal});}
      catch(e){if(e.name==='AbortError')throw e;throw fail('networkError');}
      if(response.status===304&&previous)return structuredClone(previous.release);
      if(response.status===404)throw fail('notFound');
      if(response.status===403||response.status===429)throw fail('rateLimited');
      if(!response.ok)throw fail('networkError');
      let data;try{data=await response.json();}catch{throw fail('invalidResponse');}
      const release=normalizeRelease(data,source);
      cache.set(source.api,{etag:response.headers.get('ETag'),release});
      return structuredClone(release);
    }};
  }
  function numericVersion(tag) {
    if(typeof tag!=='string')return null;
    const m=tag.match(/^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?(?:\+[\w.-]+)?$/i);
    return m?m.slice(1,5).map(n=>BigInt(n||0)):null;
  }
  function compareVersions(a,b) {
    const left=numericVersion(a),right=numericVersion(b);if(!left||!right)return null;
    for(let i=0;i<4;i++)if(left[i]!==right[i])return left[i]>right[i]?1:-1;
    return 0;
  }
  function selectLatestAsset(release) {
    // The standard translation-data package wins over unrelated platform/plugin ZIPs.
    // With custom sources, a single ZIP is unambiguous. Never use list order, a stale
    // chosen filename or a user-supplied asset ID to pick between multiple unknown ZIPs.
    const assets=Array.isArray(release?.assets)?release.assets:[];
    const canonical=assets.filter(a=>typeof a.name==='string'&&a.name.toLowerCase()==='gakumastranslationdata.zip');
    if(canonical.length===1)return canonical[0];
    if(canonical.length>1)return null;
    return assets.length===1?assets[0]:null;
  }
  function decide({enabled,checking,error,record,release,busy}) {
    const result=(kind,key,label='installTranslation',clickable=false)=>({kind,key,label,clickable});
    if(busy)return result('busy',busy==='restore'?'restoring':busy==='install'?'installing':'updating',busy==='update'?'updating':'installing');
    // The optional-management switch already conveys this state; no duplicate banner.
    if(!enabled)return result('disabled',null);
    if(checking)return result('checking','checking','checkingShort');
    if(error)return result('error','checkFailed',record?.installed?'updateTranslation':'installTranslation');
    if(!release)return result('unchecked','needCheck');
    if(!release.assets.length)return result('no-assets','noAsset');
    const asset=selectLatestAsset(release);
    if(!asset)return result('ambiguous-asset','latestAssetUnresolved',record?.installed?'updateTranslation':'installTranslation');
    // Unknown installed state may open a review, but cannot perform a real installation.
    if(!record||record.known!==true)return result('unknown','needLocal','installTranslation',true);
    if(!record.installed)return result('install','installedNone','installTranslation',true);
    if(record.repository?.toLowerCase()!==release.repositoryKey)return result('source-change','switchSourceNote','switchSource',true);
    const cmp=compareVersions(release.tag,record.version);
    if(cmp!==null&&cmp<0)return result('newer-local','newerLocal','updateTranslation');
    const same=record.releaseId===release.id && record.version===release.tag;
    if(same) {
      // Inspect digests where known; do not treat absent legacy digest metadata as a change.
      const changed=(record.assetId!=null && record.assetId!==asset.id) ||
        (record.digest && asset.digest && record.digest!==asset.digest) ||
        (record.assetUpdatedAt && asset.updatedAt && Date.parse(asset.updatedAt)>Date.parse(record.assetUpdatedAt));
      return changed?result('rebuild','sameVersionAsset','updateTranslation',true):result('latest','isLatest','updateTranslation');
    }
    if(cmp!==null&&cmp>0)return result('update','newAvailable','updateTranslation',true);
    const newerDate=Date.parse(release.publishedAt)>Date.parse(record.publishedAt);
    if(newerDate)return result('update','newAvailable','updateTranslation',true);
    // Matching textual version alone is not a proof for arbitrary moving release tags.
    if(record.version===release.tag&&record.releaseId===release.id)return result('latest','isLatest','updateTranslation');
    return result('unordered','versionUnordered','updateTranslation');
  }
  function validateSnapshot(s,gameDirectory) {
    if(!s||s.schema!=='gkms.installer-status.v1'||typeof s.revision!=='string'||!s.revision||
       typeof s.gameDirectory!=='string'||s.gameDirectory!==gameDirectory||!s.modules||!Array.isArray(s.files)||!Array.isArray(s.capabilities))throw fail('unknownRevision');
    const unselected=s.revision==='unselected'&&s.gameDirectory===''&&s.project_frontend_ready===true;
    for(const name of ['control','translation']) {
      const m=s.modules[name];
      if(unselected&&m?.known===false&&m.installed===null)continue;
      if(!m||m.known!==true||typeof m.installed!=='boolean')throw fail('unknownRevision');
      if(m.installed&&(typeof m.version!=='string'||!m.version.trim()))throw fail('unknownRevision');
    }
    for(const f of s.files) {
      if(!f||typeof f.path!=='string'||!f.path||!['added','replaced'].includes(f.action)||
         /(^[/\\]|^[A-Za-z]:|(^|[/\\])\.\.([/\\]|$)|\x00)/.test(f.path))throw fail('unknownRevision');
    }
    if(typeof s.safeToModify!=='boolean'||typeof s.operationPending!=='boolean')throw fail('unknownRevision');
    return structuredClone(s);
  }
  root.GKMS_RELEASE=Object.freeze({parseSource,normalizeRelease,createClient,compareVersions,selectLatestAsset,decide,validateSnapshot});
  if(typeof module!=='undefined'&&module.exports)module.exports=root.GKMS_RELEASE;
})(typeof globalThis!=='undefined'?globalThis:this);
