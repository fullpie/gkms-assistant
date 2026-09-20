/* Present only under the authenticated loopback host. A file:// preview has no host. */
'use strict';
window.GKMS_PROJECT_MODE = true;
(() => {
  if(location.protocol!=='http:' || location.hostname!=='127.0.0.1')return;
  const project=window.GKMS_PROJECT_MODE===true;
  const key=project?'gkms-local-token':'gkms-setup-token-v4';
  const supplied=new URLSearchParams(location.hash.slice(1)).get('token');
  let token=supplied||'';
  try{if(!token)token=sessionStorage.getItem(key)||'';if(token)sessionStorage.setItem(key,token);}catch{}
  if(!token)return;
  if(supplied)history.replaceState(null,'',location.pathname);
  const header=project?'X-GKMS-Token':'X-GKMS-Setup-Token';
  const ownerWrites=new Set(['detect','install-control','install-translation','restore-all','recover','choose-folder','choose-control',
    'loadout.refresh','loadout.read','loadout.recommend','loadout.constraints','loadout.apply',
    'preview-install-control','preview-install-translation','preview-restore-all','preview-recover','save-setup-preferences']);
  const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  function completed(data,request_id) {
    if(data?.ok===false){const error=new Error(data.errorCode||data.error||'Local operation failed.');error.code=data.errorCode;error.receipt=data;error.confirmed=true;error.request_id=request_id;throw error;}
    return data;
  }
  async function awaitOwnerReceipt(request_id) {
    // Observe one accepted/unknown request until its exact terminal receipt.
    // A lost POST response never causes another POST, even after a slow download.
    for(;;) {
      let snapshot;
      try {
        const response=await fetch('/api/snapshot',{headers:{'X-GKMS-Token':token},credentials:'omit',signal:AbortSignal.timeout(8000)});
        if(response.ok)snapshot=await response.json();
      } catch {}
      const receipt=snapshot?.control_requests?.completed?.[request_id];
      if(receipt&&typeof receipt.ok==='boolean')return completed(receipt,request_id);
      await pause(snapshot?1000:2500);
    }
  }
  async function command(action,payload={}) {
    const ownerWrite=project&&ownerWrites.has(action);
    payload={...payload};
    if(ownerWrite&&!payload.operationId)payload.operationId=crypto.randomUUID();
    const request_id=payload.operationId;
    try{
      const response=await fetch((project?'/setup-api/':'/api/')+action,{
        method:'POST',headers:{'Content-Type':'application/json',[header]:token},
        credentials:'omit',body:JSON.stringify(payload),signal:AbortSignal.timeout(20000)
      });
      const data=await response.json();
      if(response.status===202&&data?.pending===true)
        return ownerWrite?await awaitOwnerReceipt(request_id):{...data,request_id:data.request_id||request_id};
      if(!response.ok || data?.ok===false){const e=new Error(data?.errorCode||data?.error||'Local operation failed.');e.code=data?.errorCode;e.receipt=data;e.confirmed=data?.ok===false&&data.pending!==true;throw e;}
      return data;
    }catch(error){error.request_id=request_id;if(ownerWrite&&error.confirmed!==true)return awaitOwnerReceipt(request_id);throw error;}
  }
  window.GKMS_SETUP_HOST=Object.freeze({command,
    getStatus:p=>command('status',p),
    lookupRelease:({url})=>command('release',{url}),
    chooseControlModule:p=>command('choose-control',p),
    installControl:p=>command('install-control',p),
    installTranslation:p=>command('install-translation',p),
    restoreAll:p=>command('restore-all',p)
  });
  if(project)window.GKMS_PROJECT_HOST=Object.freeze({
    async snapshot(){const r=await fetch('/api/snapshot',{headers:{'X-GKMS-Token':token},credentials:'omit',signal:AbortSignal.timeout(8000)});if(!r.ok)throw new Error('Snapshot unavailable');return r.json();},
    async command(callback,values){
      const request_id=crypto.randomUUID();
      try{
        const r=await fetch('/api/command',{method:'POST',headers:{'X-GKMS-Token':token,'Content-Type':'application/json'},credentials:'omit',signal:AbortSignal.timeout(20000),body:JSON.stringify({callback,values,request_id})});
        const d=await r.json();
        if(r.status===202&&d.pending===true)return {...d,request_id};
        if(!r.ok||d.ok!==true){const error=new Error(d.error||'Control not confirmed');error.confirmed=d.ok===false&&d.pending!==true;throw error;}
        return {...d,request_id};
      }catch(error){error.request_id=request_id;throw error;}
    }
  });
})();
