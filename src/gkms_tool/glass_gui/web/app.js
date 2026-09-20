// v12 design supplied by the user; original archive retained in the research index.
// The packaged page uses the existing authenticated controller and no demo data.
'use strict';
(() => {
  const variant=document.body.dataset.variant;
  if(variant!=='original')throw new Error('Unknown design');
  const $=id=>document.getElementById(id), R=window.GKMS_RELEASE;
  const dict=window.GKMS_TRANSLATIONS, P=window.GKMS_PROJECT_STATE;
  const languages=['zh-Hant','en','ja'], names={'zh-Hant':'繁體中文',en:'English',ja:'日本語'};
  const localeKey='gkms.setup.locale.v4', configKey='gkms.setup.preferences.v4';
  const escape=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let locale='zh-Hant', requestGeneration=0, setupStatusGeneration=0, currentAbort=null, notifyTimer=null;
  const state={page:'setup',step:0,module:'control',gameDir:'',translationEnabled:false,
    apiUrl:'https://github.com/chinosk6/GakumasTranslationData/releases',restartPrompt:false,policyVariant:'baseline',runCount:1,
    release:null,assetId:null,checking:false,checkError:null,lastCheck:null,status:null,file:null,
    connected:false,installationVerified:false,launcherStatus:null,project:null,projectLost:false,projectPending:false,controlUnknown:false,stopPending:false,exiting:false,exitLost:false,busy:null,outcomeUnknown:false,dialog:null,toastKey:null,demo:null,
    appUpdateStatus:null,appUpdatePending:null,appUpdateError:null,loadoutPending:false,loadoutError:null,preferencesReady:false,preferencesReadOnly:false};
  const loadoutView=window.GKMS_LOADOUT.create();
  // Host is deliberately not discovered over the network. The native app must inject a
  // trusted, authenticated adapter before this script; a downloaded HTML never installs.
  const host=window.GKMS_SETUP_HOST||null;
  const projectMode=window.GKMS_PROJECT_MODE===true;
  const client=R.createClient((...args)=>fetch(...args));
  const unresolvedRequests=new Set();
  let developerPanelLoad=null, developerPanelController=null;
  let preferenceQueue=Promise.resolve(),preferenceLoad=Promise.resolve(false),localeEdits=0,settingEdits=0;
  try {
    const savedLocale=localStorage.getItem(localeKey);if(languages.includes(savedLocale))locale=savedLocale;
    const c=JSON.parse(localStorage.getItem(configKey)||'null');
    if(c?.schema==='gkms.setup-preferences.v4') {
      state.gameDir=typeof c.game_directory==='string'?c.game_directory.slice(0,1024):'';
      state.translationEnabled=c.translation?.enabled===true;
      if(typeof c.translation?.release_url==='string'){try{R.parseSource(c.translation.release_url);state.apiUrl=c.translation.release_url;}catch{}}
      state.restartPrompt=c.installation?.ask_launch===true||c.installation?.ask_restart===true;
    }
  } catch {}

  const paths = {
    star:'<path d="m12 2 2.6 7.4L22 12l-7.4 2.6L12 22l-2.6-7.4L2 12l7.4-2.6Z"/>',
    spark:'<path d="m14 3 1.9 5.1L21 10l-5.1 1.9L14 17l-1.9-5.1L7 10l5.1-1.9ZM5 14l1.4 3.6L10 19l-3.6 1.4L5 24l-1.4-3.6L0 19l3.6-1.4Z"/>',
    setup:'<rect x="4" y="5" width="16" height="15" rx="3"/><path d="M8 3v4m8-4v4M4 10h16m-12 5 2 2 5-5"/>',
    play:'<path d="m8 4 12 8-12 8Z"/>',
    stop:'<rect x="6" y="6" width="12" height="12" rx="2"/>',
    cards:'<rect x="7" y="5" width="13" height="16" rx="3"/><path d="M15 2H6a3 3 0 0 0-3 3v12m8-6h5m-5 4h3"/>',
    settings:'<path d="m9 3-1 3-3 1v4l2 2-1 3 3 3 3-1 3 1 3-3-1-3 2-2V7l-3-1-1-3Z"/><circle cx="12" cy="11" r="3"/>',
    chip:'<rect x="6" y="6" width="12" height="12" rx="3"/><path d="M9 2v4m6-4v4M9 18v4m6-4v4M2 9h4m-4 6h4m12-6h4m-4 6h4"/><path d="M10 10h4v4h-4Z"/>',
    globe:'<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18M5 7h14M5 17h14"/>',
    translate:'<path d="M3 5h12M9 2v3m4 0c-1 6-5 10-10 12m1-9c1 3 4 6 8 8m2 5 4-10 4 10m-6-4h4"/>',
    folder:'<path d="M3 7V5a2 2 0 0 1 2-2h5l3 3h6a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/><path d="M3 9h18"/>',
    download:'<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
    upload:'<path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5"/>',
    info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v1"/>',
    shield:'<path d="m12 2 8 4v6c0 5-8 10-8 10S4 17 4 12V6Z"/><path d="m8 11 3 3 5-5"/>',
    link:'<path d="m9 7 2-2a5 5 0 0 1 7 7l-2 2m-1 3-2 2a5 5 0 0 1-7-7l2-2m0 6 8-8"/>',
    arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>',
    chevron:'<path d="m9 5 7 7-7 7"/>',
    refresh:'<path d="M20 9a8 8 0 1 0 0 6m0-12v6h-6"/>',
    file:'<path d="M6 3h9l5 5v12a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Zm8 0v6h6M9 13h7m-7 4h5"/>',
    check:'<path d="m5 12 4 4L19 6"/>',
    close:'<path d="m6 6 12 12M6 18 18 6"/>',
    pulse:'<path d="M2 12h5l3-8 4 16 3-8h5"/>',
    layers:'<path d="m12 2 10 6-10 6L2 8Zm-9 11 9 5 9-5M3 17l9 5 9-5"/>',
  };

  const t=key=>dict[locale]?.[key]??dict['zh-Hant'][key]??key, txt=key=>escape(t(key));
  const icon=key=>`<svg class="ico" viewBox="0 0 24 24" aria-hidden="true">${paths[key]||paths.info}</svg>`;
  const btn=(key,action,style='',ic='',disabled=false,extra='')=>`<button class="btn ${style}" data-action="${action}" ${disabled?'disabled':''} ${extra}>${ic?icon(ic):''}<span>${txt(key)}</span></button>`;
  const option=(v,label,current)=>`<option value="${escape(v)}" ${String(current)===String(v)?'selected':''}>${escape(label)}</option>`;
  const langButtons=()=>`<div class="lang-control">${icon('globe')}<div class="language-switch" role="group" aria-label="${txt('uiLanguage')}">${languages.map(l=>`<button data-locale="${l}" lang="${l}" class="${locale===l?'active':''}" aria-pressed="${locale===l}">${names[l]}</button>`).join('')}</div></div>`;
  const notice=(key,ic='info')=>`<div class="note-box">${icon(ic)}<span>${txt(key)}</span></div>`;
  const checkbox=(f,key,checked,disabled=false)=>`<label class="checkbox"><input type="checkbox" data-field="${f}" ${checked?'checked':''} ${disabled?'disabled':''}><span>${txt(key)}</span></label>`;
  const unknown=key=>`<span class="unknown-chip"><i class="dot"></i>${txt(key)}</span>`;
  const statusRow=(label,value)=>`<div class="status-row"><dt>${txt(label)}</dt><dd>${value}</dd></div>`;
  const designLabel=()=>'designOriginal';
  const active=()=>state.demo||state;
  const snapshot=()=>active().status;
  const record=(module='translation')=>snapshot()?.modules?.[module]||{known:false};
  const getSource=()=>{try{return R.parseSource(state.apiUrl);}catch{return null;}};
  const selectedAsset=()=>R.selectLatestAsset(active().release);
  const decision=()=>R.decide({enabled:state.translationEnabled,checking:active().checking,error:active().checkError,
    record:record(),release:active().release,busy:state.busy});
  const formatDate=raw=>Number.isFinite(Date.parse(raw))?new Date(raw).toLocaleString(locale==='zh-Hant'?'zh-TW':locale,{hour12:false}):'—';
  const locked=()=>!!state.busy||state.loadoutPending||state.outcomeUnknown||state.exiting;
  const installedText=m=>!m.known?t('installedUnknown'):m.installed?m.version:t('installedNone');
  const demoMark=()=>state.demo?`<div class="demo-mark"><i class="dot"></i>${txt('demoBadge')}</div>`:'';
  function hero() {
    return `<header class="view-heading setup-heading"><h1>${txt('setup')}</h1><p>${txt(existingProject()?'existingProjectHeadline':'heroNote')}</p></header>`;
  }
  function demoBanner() {
    return state.demo?`<div class="demo-banner">${icon('info')}<div><strong>${txt('demoBadge')}</strong><span>${txt('demoNote')}</span></div>${btn('exitDemo','exit-demo','ghost','close',!!state.busy)}</div>`:'';
  }
  function locationCard() {
    const disabled=locked()||!!state.demo;
    return `<section class="card location-card" id="gameLocationPanel" aria-label="${txt('gameLocation')}"><div class="location-title">${icon('folder')}<h2><label for="gameDir">${txt('gameLocation')}</label></h2></div><div class="location-main"><div class="input-row"><input id="gameDir" data-field="gameDir" value="${escape(state.gameDir)}" placeholder="${txt('folderPlaceholder')}" autocomplete="off" spellcheck="false" maxlength="1024" ${disabled?'disabled':''}>${btn('detect','detect-game','','refresh',disabled)}${btn('browseFolder','choose-folder','','folder',disabled)}${btn('refreshLocal','check-status','ghost','refresh',disabled)}</div><p class="help">${txt('folderNote')}</p></div></section>`;
  }
  function loaderStatusKey() {
    const s=snapshot(),m=record('control');
    // The standalone preview starts uninstalled. It is not a scan of the user's PC.
    if(!s)return host&&state.gameDir?'loaderUnverified':'loaderNotInstalled';
    if(state.outcomeUnknown||s.operationPending||(!state.demo&&host&&!state.installationVerified)||m.known!==true)
      return 'loaderUnverified';
    // An unrelated existing version.dll is not proof that our required loader is installed.
    if(m.installed!==true)return s.externalLoader===true?'loaderUnverified':'loaderNotInstalled';
    const isLoader=path=>typeof path==='string'&&path.replace(/\\/g,'/').toLowerCase()==='version.dll';
    const listed=(s.files||[]).some(f=>isLoader(f.path));
    const changed=(s.conflicts||[]).some(isLoader);
    // Only accepted installer status/receipts with an intact managed loader say installed.
    return listed&&!changed?'loaderInstalled':'loaderUnverified';
  }
  function modulesReady() {
    if(state.installationVerified===true&&snapshot()?.project_frontend_ready===true)return true;
    if(existingProject())return state.installationVerified===true&&state.status.project_frontend_ready===true;
    if(loaderStatusKey()!=='loaderInstalled')return false;
    const st=snapshot(),normal=p=>String(p||'').replace(/\\/g,'/').toLowerCase();
    const required='gkms/native/gkms_runtime_command_bridge.dll';
    return (st.files||[]).some(f=>normal(f.path)===required)&&!(st.conflicts||[]).some(p=>normal(p)===required);
  }
  const projectData=()=>state.demo?.project||(!state.demo?state.project:null);
  const sessionData=()=>state.demo?.session||(!state.demo?state.project?.session:null);
  const existingProject=()=>state.status?.schema==='gkms.existing-project-setup.v1'&&state.status.integration_mode==='existing-project';
  const projectUi=()=>({...state,busy:state.busy||state.loadoutPending||projectData()?.loadout?.busy,modulesReady:modulesReady()});
  const editingAllowed=()=>!state.loadoutPending&&!projectData()?.loadout?.busy&&P.editingAllowed(projectData(),projectUi());
  const startAllowed=()=>!state.demo&&P.startAllowed(projectData(),projectUi());
  const canNavigate=page=>page==='setup'||modulesReady();
  const interpolate=(key,values)=>t(key).replace(/\{(\w+)\}/g,(_,k)=>String(values[k]??'—'));
  function canStop() {
    if(state.stopPending||state.exiting)return false;
    const p=projectData();
    if(p?.live?.phase==='cancelling'&&!state.projectLost)return false;
    return !!(window.GKMS_PROJECT_HOST||state.demo?.session)&&(p?.batch?.running||p?.live?.busy||p?.control_requests?.pending_ids?.length||['running','cancelling'].includes(p?.live?.phase)||(!state.demo&&(state.projectLost||state.controlUnknown)));
  }
  function batchProgress() {
    const b=sessionData()?.batch;
    const idle=!b||!Number.isInteger(b.current)||b.current===0;
    const summary=idle?txt('batchNotStarted'):
      `<strong>${escape(interpolate(b.running?'batchCurrent':'batchStopped',b))}</strong><span>${escape(interpolate('batchCompleted',{count:b.completed}))}</span><span>${escape(interpolate('batchInterrupted',{count:b.interrupted}))}</span>`;
    // Navigation only: use the same page handler/gate as the sidebar. Running
    // cultivation stays active; no command, network call or history mutation.
    const disabled=!canNavigate('history')||state.exiting;
    return `<div class="batch-progress${idle?' idle':''}" id="batchProgress" role="group" aria-label="${txt('batchProgress')}"><div class="batch-progress-summary" role="status" aria-live="polite" aria-atomic="true">${summary}</div><button type="button" id="batchHistoryButton" class="btn ghost batch-history-button" data-page="history" ${disabled?'disabled aria-disabled="true"':''}><span>${txt('viewHistory')}</span>${icon('arrow')}</button></div>`;
  }
  function phaseLabel() {
    if(!state.demo&&state.projectLost)return t('stateDisconnected');
    return t(({running:'phaseRunning',cancelling:'phaseCancelling',cancelled:'phaseCancelled',completed:'phaseCompleted',failed:'phaseFailed',blocked:'phaseBlocked',idle:'phaseIdle'})[projectData()?.live?.phase]||'wait');
  }
  function controlCard() {
    const m=record('control'),loaderKey=loaderStatusKey();
    const selectedInstalled=!!state.file&&state.installationVerified&&!state.outcomeUnknown&&snapshot()?.operationPending===false&&
      m.known===true&&m.installed===true&&/^[a-f0-9]{64}$/.test(state.file.sha256||'')&&
      m.archiveSha256===state.file.sha256&&Array.isArray(snapshot()?.conflicts)&&snapshot().conflicts.length===0;
    const packageNote=!state.gameDir?'controlChooseLocation':
      snapshot()?.operationPending||state.outcomeUnknown?'controlPendingInstallation':
      snapshot()?.conflicts?.length?'controlChangedFiles':
      !state.installationVerified||m.known!==true?'controlReadStatus':
      selectedInstalled?'controlInstalledReady':
      !state.file?.stagedCandidateId?'controlPackageMissing':
      m.installed===true?'controlDifferentPackage':'controlPackageReady';
    return `<section class="card module-card" id="controlCard"><div class="card-heading"><div class="module-icon">${icon('chip')}</div><div><h2>${txt('control')}</h2><div class="subtitle-code">RUNTIME COMMAND BRIDGE</div></div><span class="badge required">${txt('required')}</span></div><p class="card-desc">${txt('controlNote')}</p>${demoMark()}
      <div class="file-box">${icon('file')}<div><div class="file-name mono">version.dll + gkms/native/…</div><p>${txt(selectedInstalled?'fileSelectedInstalled':state.file?'fileSelected':'fileNotSelected')}</p>${state.file?`<p class="mono">SHA-256: ${escape(state.file.sha256.slice(0,18))}…</p>`:''}</div></div>
      <dl class="status-rows">${statusRow('currentVersion',escape(installedText(m)))}${statusRow('loader',`<span id="loaderInstallationStatus" data-state="${loaderKey}" role="status" aria-live="polite">${txt(loaderKey)}</span>`)}</dl>${notice(packageNote,'link')}
      <div class="checks">${checkbox('restartPrompt','restart',state.restartPrompt,locked())}</div>
      <div class="button-row module-auxiliary">${btn('chooseDll','choose-dll','','folder',locked())}${btn('maintenance','check-status','ghost','refresh',!!state.busy)}</div>
      <div class="card-bottom module-actions">${btn('installControl','install-control','primary module-install-button','download',locked())}<p class="install-note module-action-status">${txt('statusSource')}</p></div></section>`;
  }
  function versions() {
    const m=record(),release=active().release,external=!m.installed?snapshot()?.observedTranslationVersion:null;
    const installed=`<span id="installedVersion">${escape(external||installedText(m))}</span>${external?`<small class="version-origin">${txt('externalTranslation')}</small>`:''}`;
    const latest=`<span id="latestVersion">${release?escape(release.tag):txt('latestUnknown')}</span>`;
    // Same unframed information rows as the control module; versions are read-only.
    return `<dl class="status-rows translation-versions">${statusRow('currentVersion',installed)}${statusRow('latestVersion',latest)}</dl>`;
  }
  function translationCard() {
    const a=active(),enabled=state.translationEnabled,d=decision(),asset=selectedAsset(),source=getSource();
    const disabled=!enabled||locked(),statusClass=d.kind==='error'?'error':d.kind==='latest'?'positive':d.clickable?'attention':'';
    const status=d.kind==='disabled'?`<p class="install-note module-action-status">${txt('versionRule')}</p>`:`<p class="install-note module-action-status ${statusClass}" role="status" id="releaseStatus">${txt(d.key)}${a.checkError?`<span class="status-detail">${txt(a.checkError)}</span>`:''}</p>`;
    return `<section class="card module-card translationCard" id="translationCard"><div class="card-heading"><div class="module-icon blue">${icon('translate')}</div><div><h2>${txt('translation')}</h2><div class="subtitle-code">GITHUB RELEASE · OPTIONAL</div></div><span class="badge blue">${txt('optional')}</span></div>
      <div class="translation-callout">${icon('info')}<span>${txt('translationNote')}</span></div>
      <label class="toggle-row"><div><strong>${txt('enableTranslation')}</strong></div><input class="switch" id="translationEnabled" data-field="translationEnabled" type="checkbox" role="switch" aria-label="${txt('enableTranslation')}" ${enabled?'checked':''} ${locked()?'disabled':''}></label>
      ${demoMark()}<label class="field release-field"><span>${txt('apiUrl')}</span><div class="input-row"><input id="apiUrl" data-field="apiUrl" type="url" value="${escape(state.demo?'https://github.com/demo-owner/translation-pack/releases':state.apiUrl)}" placeholder="${txt('apiPlaceholder')}" autocomplete="off" spellcheck="false" maxlength="2048" ${disabled||state.demo?'disabled':''}>${btn(a.checking?'checkingShort':'checkUrl','check-release','','refresh',disabled||a.checking||(!source&&!state.demo))}</div></label>
      ${versions()}
      ${a.lastCheck?`<p class="release-stamp">${txt('lastCheck')} · ${escape(formatDate(a.lastCheck))}${a.release?.publishedAt?`<br>${txt('published')} · ${escape(formatDate(a.release.publishedAt))}`:''}</p>`:''}
      ${a.release&&!state.demo?`<div class="release-links module-auxiliary"><a href="${escape(a.release.page)}" target="_blank" rel="noopener noreferrer">${txt('openRelease')} ↗</a>${asset?`<a href="${escape(asset.url)}" target="_blank" rel="noopener noreferrer">${txt('openDownload')} ↗</a>`:''}</div>`:''}
      <div class="card-bottom module-actions">${btn(d.label,'install-translation','primary module-install-button','download',!d.clickable||locked(),d.kind==='disabled'?'':'aria-describedby="releaseStatus"')}${status}</div></section>`;
  }
  function restoreCard() {
    const s=snapshot(),known=!!s,files=s?.files||[],added=files.length;
    return `<section class="restore-card" id="restoreCard"><div class="restore-copy"><h2>${icon('refresh')}${txt('restoreTitle')}</h2><p>${txt('restoreNote')}</p><p>${txt('restoreProtect')}</p>${known?`<div class="restore-counts"><span>${txt('removeNew')} <b>${added}</b></span></div>`:''}</div><div class="restore-actions">${btn('restoreButton','restore-all','danger','refresh',locked()||(known&&files.length===0))}<small>${txt(state.demo?'demoAction':!known?'manifestMissing':!files.length?'noManaged':'restoreBefore')}</small></div></section>`;
  }
  function demoPanel(page='setup') {
    if(projectMode||host)return '';
    const launch=page==='cultivate';
    const choices=launch?
      [['ready','launchDemoReady'],['monitoring','launchDemoMonitoring'],['failed','launchDemoFailed']].map(([v,k])=>btn(k,'launcher-demo-'+v,'','',!!state.busy)).join(''):
      [['uninstalled','demoUninstalled'],['update','demoUpdate'],['latest','demoLatest'],['check-error','demoCheckFailure'],['install-error','demoInstallFailure']].map(([v,k])=>btn(k,'demo-'+v,state.demo?.scenario===v?'secondary':'','',!!state.busy)).join('');
    return `<details class="demo-panel" ${state.demo?'open':''}><summary>${txt('demoTitle')}</summary><p>${txt('demoNote')}</p><div class="button-row">${choices}${btn('sessionDemo','session-demo','','pulse',!!state.busy)}${state.demo?btn('exitDemo','exit-demo','ghost','',!!state.busy):''}</div></details>`;
  }
  function actionBar() {
    return `<section class="selection-bar"><div><strong>${txt('controlRequired')}</strong><p>${txt('statusSource')}</p></div><div class="button-row">${btn('save','save','','check',locked()||!!state.demo)}${btn('continue','go-cultivate','primary','arrow',!modulesReady()||state.exiting)}</div></section>`;
  }
  function setupPage() {
    if(existingProject())return existingProjectSetup();
    return hero()+demoBanner()+locationCard()+`<div class="two-col">${controlCard()}${translationCard()}</div>`+
      (snapshot()?.operationPending?`<section class="card recover-panel">${notice('recoveryNote')}${btn('recover','recover','danger','refresh',!!state.busy)}</section>`:'')+
      (snapshot()?.conflicts?.length?`<div class="operation-notice">${txt('conflictNote')}<code>${snapshot().conflicts.map(escape).join('<br>')}</code></div>`:'')+
      restoreCard()+actionBar()+demoPanel();
  }

  function existingProjectSetup() {
    const s=state.status;
    return hero()+`<section class="card"><div class="card-heading"><span class="module-icon">${icon('link')}</span><h2>${txt('existingProject')}</h2></div><p class="card-desc">${txt('existingProjectNote')}</p><dl class="status-rows">${statusRow('gameLocation',escape(s.gameDirectory||state.gameDir||'—'))}</dl><p class="help">${txt('existingProjectUnverified')}</p><div class="button-row">${btn('refreshLocal','check-status','','refresh',!!state.busy)}${btn('continue','go-cultivate','primary','arrow',!modulesReady()||state.exiting)}</div></section>`;
  }

  function cultivationPage() {
    const s=projectData(),sel=s?.selection||{},live=s?.live||{},catalog=s?.catalog||{},v=live.state||{};
    const connected=!!s&&(!!state.demo||!state.projectLost);
    const busy=!editingAllowed();
    const variants=catalog.policy_variants||[];
    const modelLocked=P.modelSelectionLocked(s);
    const modelLockNote=s?.policy?.selection_lock_kind==='active-run-unavailable'?'modelLockUnverified':
      s?.policy?.selection_run_id||s?.active_run?.run_id?'modelLockedForRun':null;
    const start=startAllowed();
    const stop=canStop();
    const num=x=>Number.isFinite(x)?x.toLocaleString(locale):'—';
    const metrics=[['week',v.week??v.route_week??v.current_turn],['stamina',v.stamina],['points',v.score??v.player_score??v.produce_points]];
    const noteKeys={'current-selection':'currentSelectionNote','next-selection':'nextSelectionNote',
      'awaiting-identity':'awaitingRunIdentity','waiting-update':'awaitingProgress',
      'next-selection-idle':'nextRunNote','retained-run':'retainedRunNote'};
    const selectionNote=noteKeys[sel.note_kind]?t(noteKeys[sel.note_kind]):v.in_progress===true?t('awaitingRunIdentity'):(sel.note||'');
    const activeRun=s?.active_run;
    const activeMode=(catalog.modes||[]).find(row=>row.id===activeRun?.produce_id)?.label||activeRun?.produce_id;
    const activeRunText=activeRun?[activeRun.idol_name||activeRun.idol_card_id,activeMode].filter(Boolean).join(' · '):v.in_progress===true?t('awaitingRunIdentity'):t('noActiveRun');
    const coverage=s?.policy?.selected_flow_coverage;
    const coverageKey=coverage?(coverage.supported!==true?'flowScopeUnsupported':coverage.trained_flow_covered===true?'flowScopeTrained':'flowScopeTransfer'):null;
    return `<header class="view-heading cultivate-heading"><h1>${txt('cultivate')}</h1><p>${txt(state.demo?'demoBadge':connected?(s?.bridge?.control_enabled===false?'projectReadOnly':'projectConnected'):'projectUnavailable')}</p></header>${launcherCard()}${batchProgress()}<div class="run-grid"><section class="card cultivation-settings" id="cultivationSettings" aria-labelledby="cultivationTitle"><div class="card-heading"><span class="module-icon">${icon('star')}</span><h2 id="cultivationTitle">${txt('gameReady')}</h2></div><div class="grid-fields">
    <label class="field"><span>${txt('idol')}</span><select data-field="projectIdol" ${!connected||busy?'disabled':''}>${option('',t('idolPlaceholder'),sel.idol_card_id)}${(catalog.profiles||[]).map(p=>option(p.id,p.label,sel.idol_card_id)).join('')}</select></label>
    <label class="field"><span>${txt('mode')}</span><select data-field="projectMode" ${!connected||busy?'disabled':''}>${(catalog.modes||[]).length?(catalog.modes||[]).map(m=>option(m.id,m.label,sel.mode_id)).join(''):'<option>—</option>'}</select></label>
    <label class="field"><span>${txt('model')}</span><select data-field="policyVariant" ${!connected||busy||modelLocked?'disabled':''}>${P.variantIds.map(id=>`<option value="${id}" ${id===(sel.policy_variant_id||state.policyVariant)?'selected':''} ${P.availableVariant(s,id)?'':'disabled'}>${txt(id==='baseline'?'modelBaseline':'modelIntegrated')}${P.availableVariant(s,id)?'':` · ${txt('modelUnavailable')}`}</option>`).join('')}</select></label>
    <label class="field"><span>${txt('count')}</span><input data-field="runCount" type="number" min="1" max="999" step="1" value="${sel.target_cycles||state.runCount}" ${!connected||busy?'disabled':''}></label></div>
    ${selectionNote?`<p class="help selection-note">${escape(selectionNote)}</p>`:''}${modelLockNote?`<p class="help model-lock">${txt(modelLockNote)}</p>`:''}${coverageKey?`<p class="help flow-coverage">${txt(coverageKey)}${coverage.quality_accepted===false?' '+txt('flowQualityUnverified'):''}</p>`:''}${!start&&!P.running(s)&&!state.demo?notice('startBlock','shield'):''}${live.start_reason?`<p class="help">${escape(live.start_reason)}</p>`:''}${variants.find(x=>x.id===sel.policy_variant_id)?.reason?`<p class="help">${escape(variants.find(x=>x.id===sel.policy_variant_id).reason)}</p>`:''}${state.controlUnknown?notice('projectUnconfirmed'):''}
    <div class="button-row">${btn('start','start','primary','play',!start)}${btn(state.stopPending||live.phase==='cancelling'?'stopping':'stop','stop','','stop',!stop)}</div><div class="cultivation-setup-link">${btn('fixSetup','go-setup','ghost','setup')}</div></section>
    <section class="card cultivation-status"><div class="card-heading"><div class="module-icon">${icon('pulse')}</div><div><h2>${txt('runStatus')}</h2><p class="help">${escape(phaseLabel())}</p></div></div>
    <div class="metrics">${metrics.map(([k,val])=>`<div class="metric"><span>${txt(k)}</span><strong>${num(val)}</strong></div>`).join('')}</div>
    <dl class="status-rows">${statusRow('currentCultivation',escape(activeRunText||t('awaitingRunIdentity')))}${statusRow('actualModel',escape(s?.policy?.actual_model_label||t('modelNotObserved')))}${statusRow('lastAction',escape(live.last_action||'—'))}${statusRow('stopReason',escape(live.stop_reason||'—'))}</dl>${live.display_source==='retained-terminal-view'?`<p class="help">${txt('retainedObservation')}</p>`:''}${s?.policy?.actual_model_sha256?`<details class="model-evidence"><summary>${txt('modelIdentity')}</summary><code>${escape(s.policy.actual_model_sha256)}</code><p>${escape(s.policy.actual_run_id||'—')}</p></details>`:''}
    </section></div>`+(state.demo?'':loadoutCard())+demoPanel('cultivate');
  }
  function loadoutCan(action) {
    const s=projectData(),value=s?.loadout;
    if(!host||state.demo||state.projectLost||state.exiting||state.busy||state.projectPending||state.loadoutPending||
      state.controlUnknown||state.outcomeUnknown||s?.bridge?.control_enabled!==true||value?.capabilities?.[action]!==true)return false;
    if(editingAllowed())return true;
    return action==='read'&&value.can_reconcile_pending===true&&!value.busy&&!value.game_busy&&
      !s?.live?.busy&&!s?.batch?.running&&!['running','cancelling'].includes(s?.live?.phase)&&
      !(s?.control_requests?.pending_ids?.length);
  }
  function loadoutCard() {
    return loadoutView.render(projectData()?.loadout,{t,escape,allowed:loadoutCan})+
      (state.loadoutError?`<p class="help app-update-error" role="alert">${escape(state.loadoutError)}</p>`:'');
  }
  function loadoutReview() {
    if(!loadoutCan('apply'))return;
    const payload=loadoutView.payload(projectData()?.loadout);if(!payload)return;
    state.dialog={type:'loadout',payload};renderDialog();$('dialog').showModal();
  }
  function loadoutReviewCurrent() {
    return state.dialog?.type==='loadout'&&JSON.stringify(loadoutView.payload(projectData()?.loadout))===JSON.stringify(state.dialog.payload);
  }
  function renderLoadoutDialog() {
    $('dialog').innerHTML=`<div class="dialog-head"><h2>${txt('loadoutApply')}</h2>${btn('close','close-dialog','ghost','close')}</div><p>${txt('loadoutConfirm')}</p><p class="help">${txt('loadoutApplyNote')}</p><div class="button-row">${btn('cancel','close-dialog','ghost')}${btn('loadoutApply','loadout-commit','primary','check',!loadoutCan('apply')||!loadoutReviewCurrent())}</div>`;
  }
  async function loadoutAction(action,payload={}) {
    if(!loadoutCan(action))return;
    if(action==='recommend'&&loadoutView.dirty())return;
    if(action==='constraints')payload={constraints:loadoutView.constraints()};
    if(action==='apply'&&(!loadoutReviewCurrent()||!loadoutCan('apply')))return;
    const operationId=crypto.randomUUID();state.loadoutPending=true;state.loadoutError=null;
    if(action==='apply'){state.dialog=null;$('dialog').close();}render();
    try {
      const response=await host.command('loadout.'+action,{operationId,...payload});
      if(response.snapshot?.schema==='gkms.gui-loadout.v1'&&state.project)state.project.loadout=response.snapshot;
      if(action==='constraints')loadoutView.saved(state.project?.loadout);
      // Accepted means the owner started its worker. Only subsequent cached
      // observations establish the resulting inventory / applied composition.
    } catch(error){state.loadoutError=error.message||t('projectFailed');}
    finally{state.loadoutPending=false;render();}
  }
  function historyPage() {
    const session=sessionData(),rows=session?.rows||[];
    return `<header class="view-heading"><h1>${txt('history')}</h1><p>${txt('historyNote')}</p></header>${demoBanner()}`+
      `<section class="card session-history" id="sessionHistory"><div class="section-title"><h2>${txt('sessionOnly')}</h2><span class="badge">${rows.length}</span></div><div class="table-wrap"><table><thead><tr>${['resultsTime','resultsCharacter','resultsMode','resultsStatus','resultsScore'].map(k=>`<th>${txt(k)}</th>`).join('')}</tr></thead><tbody>${rows.length?rows.map(row=>`<tr><td>${escape(formatDate(row.ended_at||row.started_at))}</td><td>${escape(row.character||'—')}</td><td>${escape(row.mode||'—')}</td><td><span class="result-status ${row.status==='completed'?'completed':'interrupted'}">${txt(row.status==='completed'?'resultCompleted':'resultInterrupted')}</span></td><td class="result-score" title="${txt('scoreTooltip')}">${Number.isFinite(row.score)?escape(row.score.toLocaleString(locale)):'—'}</td></tr>`).join(''):`<tr><td colspan="5" class="session-empty"><strong>${txt('noHistory')}</strong><p>${txt('historyEmpty')}</p></td></tr>`}</tbody></table></div><p class="help">${txt('scoreNote')}</p>${session?.recording_ok===false?notice('recordingWarning'):''}</section>`;
  }

  function launcherSnapshot() {return state.demo?.launcher||(!state.demo?state.launcherStatus:null);}
  const launchCode=code=>dict[locale]?.['launchErr_'+code]?t('launchErr_'+code):t('launchUnknownResult');
  function launcherValues() {
    const s=launcherSnapshot(), known=!!s;
    const words=(v,yes,no)=>v===true?t(yes):v===false?t(no):'—';
    const phase=s?.state||'disconnected';
    const stateKey='launchState_'+phase;
    const issue=s?.errorCode||(['SYNC_DMM_OPEN','DMM_CLOSE_UNCONFIRMED'].includes(s?.lastResult?.resultCode)?s.lastResult.resultCode:null);
    const msg=phase==='existing-maintenance'?t('existingLauncherNote'):issue?launchCode(issue):phase==='monitoring'?t('launchMonitoringNote'):phase==='process-stable'?t('launchStableNote'):
      phase==='no-new-data'?t('launchNoNewNote'):phase==='sync-complete'?t('launchSyncDoneNote'):
      phase==='ready'?t('launchReadyNote'):phase==='sync-available'?t('launchSyncAvailableNote'):
      phase==='game-running'?t('launchRunningNote'):phase==='unsupported'?t('launchWindowsNote'):t('launchNeedsDmmNote');
    const current=state.demo?.launcher?.gameDirectory||state.gameDir;
    const projectBusy=state.projectPending||state.project?.live?.busy||['running','cancelling'].includes(state.project?.live?.phase)||state.project?.batch?.running;
    const projectReady=!projectMode||editingAllowed();
    const available=!!(host||state.demo?.launcher)&&known&&s.platformSupported===true&&s.readOnly===false&&!s.operationBusy&&!s.projectPending&&!locked()&&!projectBusy&&!state.projectLost&&!state.controlUnknown&&projectReady&&(!state.launcherLost||!!state.demo?.launcher);
    const monitoring=['starting','monitoring','syncing'].includes(phase);
    const close=s?.lastResult?.dmmClose;
    return {
      phase,label:dict[locale]?.[stateKey]?t(stateKey):t('launchState_disconnected'),
      message:!known?t('launchDisconnectedNote'):typeof s.blockedReason==='string'&&s.blockedReason?s.blockedReason:msg,
      version:s?.gameVersion?(String(s.gameVersion).startsWith('v')?'':'v')+s.gameVersion:'—',
      synced:s?.cacheReadable?formatDate(s.cacheCapturedAt):s?.cachePresent?t('launchCacheUnreadable'):known?t('launchNeverSynced'):'—',
      dmm:words(s?.dmmRunning,'launchDmmRunning','launchDmmClosed'),
      game:words(s?.gameRunning,'launchGameRunning','launchGameClosed'),
      path:s?.gamePath||'—',source:s?.gamePathSource?t('launchSource_'+s.gamePathSource):'—',
      cached:words(s?.cacheReadable,'launchCacheReadable',s?.cachePresent?'launchCacheUnreadable':'launchNeverSynced'),
      latest:s?.latestLogRecordPresent?formatDate(s.latestLogCapturedAt):known?t('launchNoRecord'):'—',
      matches:words(s?.cacheMatchesLatestLog,'launchDataSame','launchDataNew'),
      close:close?`${t('launchClosedCount')} ${close.closed} · ${t('launchRemainingCount')} ${close.remaining}`:'—',
      pathMatch:s?.targetMatches===false?t('launchPathMismatch'):s?.targetMatches===true&&current?t('launchPathMatched'):'—',
      warn:(s?.warnings||[]).map(launchCode).join(' · '),
      remaining:s?.monitor?.remaining??35,monitoring:phase==='monitoring',
      startDisabled:!available||s.canLaunch!==true||!current,
      syncDisabled:!available||s.canSync!==true||monitoring||!current,
      repairDisabled:!available||s.canRepair!==true||monitoring,
      readOnly:s?.readOnly===true
    };
  }
  const launchText=(key,value,tag='span',cls='')=>`<${tag} class="${cls}" data-launcher-text="${key}">${escape(value)}</${tag}>`;
  function launcherCard() {
    const v=launcherValues();
    return `<section class="card launch-workspace" id="launcherPanel" aria-label="${txt('launchPanelTitle')}">
      ${state.demo?demoMark():''}<div class="launch-hero"><div class="launch-title"><span class="module-icon blue">${icon('play')}</span><div><h2>${txt('launchPanelTitle')}</h2><span class="help" id="launcherDescription">${txt('launchUsageDescription')}</span></div></div>
      <div class="launch-actions">${btn('launchGame','launcher-launch','primary','play',v.startDisabled)}${btn('syncLauncher','launcher-sync','','refresh',v.syncDisabled)}${btn('repairLauncher','launcher-repair','ghost','link',v.repairDisabled)}</div></div>
      <div class="launch-status-line">${launchText('label',v.label,'strong','launch-state')}<p class="launch-message" data-launcher-text="message">${escape(v.message)}</p></div>
      <div id="launchMonitor" ${v.monitoring?'':'hidden'}><div class="monitor-label"><span>${txt('launchMonitoring')}</span><b><span data-launcher-text="remaining">${v.remaining}</span> ${txt('seconds')}</b></div><progress id="launchProgress" max="35" value="${35-v.remaining}" aria-label="${txt('launchMonitoring')}"></progress></div>
      <div class="launch-metrics"><div><span>${txt('launchGameVersion')}</span>${launchText('version',v.version,'strong')}</div><div><span>${txt('launchLastSync')}</span>${launchText('synced',v.synced,'strong')}</div><div><span>${txt('launchDmmStatus')}</span>${launchText('dmm',v.dmm,'strong')}</div></div>
      <div class="launch-path-row"><p class="launch-executable"><span>${txt('launchExecutable')}</span>${launchText('path',v.path,'code')}</p>${btn('configureGameLocation','go-setup','ghost','folder',false)}</div>
      <details class="launch-details" id="launcherDetails"><summary>${txt('launchDetails')}</summary><dl>${[['game','launchProcessStatus'],['source','launchPathSource'],['cached','launchCacheState'],['latest','launchLatestDmmRecord'],['matches','launchLogComparison'],['close','launchDmmCloseResult']].map(([field,k])=>`<div><dt>${txt(k)}</dt><dd data-launcher-text="${field}">${escape(v[field])}</dd></div>`).join('')}</dl><p class="help" data-launcher-text="pathMatch">${escape(v.pathMatch)}</p><p class="help">${txt('launchTimeNote')}</p></details>
      <p class="launch-warning" id="launcherWarning" ${v.warn?'':'hidden'}>${escape(v.warn)}</p><p class="help" id="launcherReadOnly" ${v.readOnly?'':'hidden'}>${txt('launchReadOnly')}</p>
    </section>`;
  }
  function updateLauncherPanel() {
    const v=launcherValues(),panel=$('launcherPanel');if(!panel)return;
    panel.dataset.phase=v.phase;
    panel.querySelectorAll('[data-launcher-text]').forEach(el=>{const key=el.dataset.launcherText;if(key in v)el.textContent=String(v[key]);});
    [['launcher-launch','startDisabled'],['launcher-sync','syncDisabled'],['launcher-repair','repairDisabled']].forEach(([a,k])=>{const b=panel.querySelector(`[data-action="${a}"]`);if(b)b.disabled=v[k];});
    $('launchMonitor').hidden=!v.monitoring;$('launchProgress').value=35-v.remaining;
    $('launcherWarning').textContent=v.warn;$('launcherWarning').hidden=!v.warn;
    $('launcherReadOnly').hidden=!v.readOnly;
  }
  let launcherPollTimer=null,launcherPollPending=false;
  async function launcherPoll() {
    if(!host||state.exiting)return;
    if(launcherPollPending)return;
    launcherPollPending=true;
    try {
      const s=await host.command('launcher-status',{gameDirectory:state.gameDir});
      if(s?.schema!=='gkms.launcher-status.v1')throw new Error('launcher schema');
      state.launcherStatus=s;state.launcherLost=false;state.connected=true;
      updateLauncherPanel();
      if(state.dialog?.type==='post-install-launch')renderPostInstallLaunchDialog();
    }catch{state.launcherLost=true;if(state.launcherStatus){state.launcherStatus={...state.launcherStatus,state:'disconnected',canLaunch:false,errorCode:null};updateLauncherPanel();}}
    finally{launcherPollPending=false;clearTimeout(launcherPollTimer);launcherPollTimer=setTimeout(launcherPoll,document.hidden?3500:1000);}
  }
  function launcherReview(action) {
    const v=launcherValues();
    if(v[action==='launcher-sync'?'syncDisabled':'repairDisabled'])return;
    state.dialog={type:'launcher',action,closeDmm:false,forceCloseDmm:false};
    renderLauncherDialog();$('dialog').showModal();
  }
  function renderLauncherDialog() {
    const d=state.dialog;if(d?.type!=='launcher')return;
    const sync=d.action==='launcher-sync';
    $('dialog').innerHTML=`<div class="dialog-head"><h2>${txt(sync?'syncLauncher':'repairLauncher')}</h2>${btn('close','close-dialog','ghost','close',!!state.busy)}</div><p>${txt(sync?'launchSyncConfirm':'launchRepairConfirm')}</p>`+
      (sync?`<div class="checks launch-sync-options">${checkbox('closeDmm','launchCloseDmm',d.closeDmm,!!state.busy)}${checkbox('forceCloseDmm','launchForceCloseDmm',d.forceCloseDmm,!d.closeDmm||!!state.busy)}</div>`:'')+
      `<div class="button-row">${btn('cancel','close-dialog','ghost','',!!state.busy)}${btn('launchConfirm','commit-launcher','primary','check',!!state.busy)}</div>`;
  }
  async function reviewPostInstallLaunch(gameDirectory) {
    const dialog={type:'post-install-launch',gameDirectory,checking:true};state.dialog=dialog;renderDialog();$('dialog').showModal();
    try {
      const snapshot=await host.command('launcher-status',{gameDirectory});
      if(state.dialog!==dialog)return;
      if(snapshot?.schema!=='gkms.launcher-status.v1')throw new Error('Launcher status unavailable');
      state.launcherStatus=snapshot;state.launcherLost=false;
    }catch{if(state.dialog===dialog)state.launcherLost=true;}
    finally{if(state.dialog===dialog){dialog.checking=false;renderDialog();}}
  }
  function renderPostInstallLaunchDialog() {
    const d=state.dialog,v=launcherValues();
    $('dialog').innerHTML=`<div class="dialog-head"><h2>${txt('postInstallLaunchTitle')}</h2>${btn('close','close-dialog','ghost','close')}</div><p>${txt('postInstallLaunchNote')}</p><p class="help">${d.checking?txt('postInstallLaunchChecking'):escape(v.message)}</p><div class="button-row">${btn('postInstallLaunchLater','close-dialog','ghost')}${btn('launchGame','confirm-post-install-launch','primary','play',d.checking||d.gameDirectory!==state.gameDir||v.startDisabled)}</div>`;
  }
  async function launcherAction(action,options={}) {
    const guard={'launcher-launch':'startDisabled','launcher-sync':'syncDisabled','launcher-repair':'repairDisabled'}[action];
    if(!guard||locked()||!modulesReady()||launcherValues()[guard])return;
    if(state.demo?.launcher){
      const d=state.demo.launcher;
      if(action==='launcher-launch'){d.state='monitoring';d.canLaunch=false;d.gameRunning=true;d.monitor={duration:35,remaining:35};}
      else if(action==='launcher-sync'){d.state='no-new-data';d.lastResult={resultCode:'NO_NEW_DATA'};}
      state.dialog=null;$('dialog').close();render();return;
    }
    if(!host)return;
    const frozen={gameDirectory:state.gameDir,operationId:crypto.randomUUID(),...options};
    state.busy='launcher';state.operationNotice=null;state.rawError=null;render();
    try{
      const result=await host.command(action,frozen);
      if(result.snapshot?.schema==='gkms.launcher-status.v1')state.launcherStatus=result.snapshot;
      if(result.pending===true){unresolvedRequests.add(result.request_id||frozen.operationId);state.controlUnknown=true;}
      state.launcherLost=false;
    }catch(e){
      if(e.confirmed!==true){unresolvedRequests.add(e.request_id||frozen.operationId);state.controlUnknown=true;}
      if(e.receipt?.snapshot?.schema==='gkms.launcher-status.v1')state.launcherStatus=e.receipt.snapshot;
      else if(state.launcherStatus)state.launcherStatus={...state.launcherStatus,state:'unknown',canLaunch:false,errorCode:e.code||'RESULT_UNKNOWN'};
      // Never auto-retry a launch or sync after an uncertain response.
    }finally{state.busy=null;state.dialog=null;$('dialog').close();render();}
  }
  function launcherDemo(phase='ready') {
    if(state.busy||host||projectMode)return;
    if(!state.demo)selectDemo('latest');
    state.demo.scenario='launcher-'+phase;
    state.demo.launcher={schema:'gkms.launcher-status.v1',integrated:true,platformSupported:true,
      state:phase,gamePath:'D:\\Games\\Gakumas\\gakumas.exe',gameDirectory:'D:\\Games\\Gakumas',gameVersion:'3.3.0 · DEMO',gamePathSource:'encrypted-cache',
      gamePathExists:true,targetMatches:true,cachePresent:true,cacheReadable:true,cacheCapturedAt:'2026-09-15T06:42:18Z',
      latestLogRecordPresent:true,latestLogCapturedAt:'2026-09-15T06:42:18Z',cacheMatchesLatestLog:true,
      gameRunning:['monitoring','process-stable'].includes(phase),dmmRunning:false,canLaunch:!['monitoring','process-stable'].includes(phase),
      monitor:phase==='monitoring'?{duration:35,remaining:22}:null,errorCode:phase==='needs-refresh'?'PROCESS_EXITED_EARLY':null};
    state.page='cultivate';render();window.scrollTo(0,0);
  }

  // The existing GkmsApp and OwnerPump remain the only game-controller owner.
  function projectValues() {
    const s=state.project?.selection||{},c=state.project?.catalog||{};
    const p=c.profiles?.find(x=>x.id===s.idol_card_id),m=c.modes?.find(x=>x.id===s.mode_id);
    return {idol_card_id:s.idol_card_id||'',profile_selection_var:p?.label||'',
      console_mode_var:s.mode_id||'',console_mode_choice_var:m?.label||'',
      home_cycle_target_var:s.target_cycles||1,batch_target_var:s.target_cycles||1,
      policy_variant_id:s.policy_variant_id||'baseline',console_bundle_var:s.bundle_id||''};
  }
  async function projectCommand(callback,override={}) {
    const ph=window.GKMS_PROJECT_HOST;
    const safety=['_stop_console_autopilot','_stop_console_batch','shutdown'].includes(callback);
    if(!ph||state.demo||(!safety&&!editingAllowed()))return;
    if(callback==='set_policy_variant'&&P.modelSelectionLocked(state.project))return;
    if(callback==='_start_console_autopilot'&&!startAllowed())return;
    state.projectPending=true;render();
    try{const r=await ph.command(callback,{...projectValues(),...override});if(r.snapshot)state.project=P.projectSnapshot(r.snapshot);if(r.pending===true){state.controlUnknown=true;unresolvedRequests.add(r.request_id);}notify('projectPending');}
    catch(error){if(error.confirmed!==true){state.controlUnknown=true;unresolvedRequests.add(error.request_id);}notify('projectFailed');}
    finally{state.projectPending=false;render();}
  }
  function projectSelection(field,value){
    if(!editingAllowed())return;
    const c=state.project.catalog||{};
    if(field==='projectIdol'){const p=c.profiles?.find(x=>x.id===value);if(p)projectCommand('_on_console_profile_changed',{idol_card_id:value,profile_selection_var:p.label});}
    if(field==='projectMode'){const m=c.modes?.find(x=>x.id===value);if(m)projectCommand('_on_console_mode_changed',{console_mode_var:value,console_mode_choice_var:m.label});}
    if(field==='policyVariant'&&!P.modelSelectionLocked(state.project)&&P.availableVariant(state.project,value))projectCommand('set_policy_variant',{policy_variant_id:value});
    if(field==='runCount'){const n=Number(value);if(Number.isInteger(n)&&n>=1&&n<=999)projectCommand('set_target_cycles',{home_cycle_target_var:n,batch_target_var:n});else notify('countInvalid');}
  }
  async function projectPoll(){
    if(!window.GKMS_PROJECT_HOST||state.exitLost)return;
    const previousUpdate=JSON.stringify([appIdentity(),appUpdate()?.status,appUpdate()?.release?.release_id,appUpdate()?.restart_required,appUpdate()?.active_version,appUpdate()?.recovery_launch,appUpdate()?.activation_warning]);
    try{const s=P.projectSnapshot(await window.GKMS_PROJECT_HOST.snapshot());
      reconcileAppUpdate(s);
      if(!state.projectPending)state.project=s;state.projectLost=false;if(s.assistant?.closing===true)state.exiting=true;
      for(const id of unresolvedRequests){const receipt=id&&s.control_requests?.completed?.[id];if(receipt&&typeof receipt.ok==='boolean'){unresolvedRequests.delete(id);if(receipt.ok===false)notify('projectFailed');}}
      state.controlUnknown=unresolvedRequests.size>0;
    }catch{state.projectLost=true;if(state.exiting)state.exitLost=true;}
    // A focused button must not freeze progress. Preserve an open select only
    // while settings remain editable; safety/state transitions still redraw it.
    const editingSelect=document.activeElement?.tagName==='SELECT'&&editingAllowed();
    const changedUpdate=previousUpdate!==JSON.stringify([appIdentity(),appUpdate()?.status,appUpdate()?.release?.release_id,appUpdate()?.restart_required,appUpdate()?.active_version,appUpdate()?.recovery_launch,appUpdate()?.activation_warning]);
    if((state.exiting||changedUpdate||['cultivate','history','advanced'].includes(state.page))&&!editingSelect)render();
    if(!state.exitLost)setTimeout(projectPoll,1200);
  }

  async function requestStop() {
    if(!canStop())return;
    if(state.demo?.session) {
      const se=state.demo.session,when=new Date().toISOString();
      se.rows.unshift({id:'demo-stopped',started_at:when,ended_at:when,character:t('demoRunCharacter'),mode:'N.I.A. Pro',status:'interrupted',score:null,score_kind:'final-exam'});
      se.batch.interrupted++;se.batch.running=false;
      state.demo.project.live={...state.demo.project.live,phase:'cancelled',busy:false};state.demo.project.batch.running=false;
      render();return;
    }
    state.stopPending=true;render();
    try {const r=await window.GKMS_PROJECT_HOST.command('_stop_console_autopilot',{});if(r.pending===true){unresolvedRequests.add(r.request_id);state.controlUnknown=true;}notify('stopSent');}
    catch(error){if(error.confirmed!==true){unresolvedRequests.add(error.request_id);state.controlUnknown=true;}notify('projectFailed');}
    finally{state.stopPending=false;render();}
  }
  function reviewExit() {
    if(state.exiting)return;
    state.dialog={type:'exit'};renderExitDialog();$('dialog').showModal();
  }
  function renderExitDialog() {
    const busy=!!state.busy;
    $('dialog').innerHTML=`<div class="dialog-head"><h2>${txt('exitTitle')}</h2>${btn('close','close-dialog','ghost','close')}</div><p>${txt('exitNote')}</p>${busy?notice('exitBusyNote'):''}${!host&&!state.demo?notice('exitPreview'):''}<div class="button-row exit-confirm-actions">${btn('cancel','close-dialog','ghost')}${btn(canStop()?'exitConfirm':'exitIdleConfirm','confirm-exit','danger','close',busy)}</div>`;
  }
  async function confirmExit() {
    if(state.busy||state.exiting)return;
    if(!host) {state.dialog=null;$('dialog').close();notify(state.demo?'demoAction':'exitPreview');return;}
    state.dialog=null;$('dialog').close();state.exiting=true;render();
    try {
      const reply=window.GKMS_PROJECT_HOST?await window.GKMS_PROJECT_HOST.command('shutdown',{}):await host.command('shutdown',{});
      if(reply?.ok!==true&&reply?.pending!==true)throw new Error('unconfirmed exit');
      if(!window.GKMS_PROJECT_HOST) {
        for(let attempt=0;attempt<20;attempt++) {
          await new Promise(r=>setTimeout(r,400));
          try{await host.command('launcher-status',{gameDirectory:state.gameDir});}
          catch{state.exitLost=true;break;}
        }
      }
      render();
    } catch{state.exiting=false;notify('exitUnknown');render();}
  }
  function sessionDemo() {
    if(host||projectMode){notify('demoAction');return;}
    selectDemo('latest');
    launcherDemo('game-running');state.demo.launcher.gameRunning=true;state.demo.launcher.canLaunch=false;
    const now=Date.now(),row=i=>({id:'demo-'+i,ended_at:new Date(now-i*900000).toISOString(),
      character:t('demoRunCharacter'),mode:'N.I.A. Pro',status:'completed',score:[null,82640,79318,85472][i],score_kind:'final-exam'});
    state.demo.session={schema:'gkms.session-results.v1',session_id:'DEMO',rows:[1,2,3].map(row),
      batch:{current:4,target:10,completed:3,interrupted:0,running:true},recording_ok:true};
    state.demo.project={selection:{idol_card_id:'demo-idol',mode_id:'produce-004',policy_variant_id:'baseline',target_cycles:10},
      catalog:{profiles:[{id:'demo-idol',label:t('demoRunCharacter')}],modes:[{id:'produce-004',label:'N.I.A. Pro'}],policy_variants:[{id:'baseline',available:true},{id:'integrated',available:true}]},
      batch:{running:true},live:{phase:'running',busy:true,state:{week:9,stamina:26,score:18640},last_action:t('demoRunAction')},policy:{actual_model_label:t('modelBaseline')}};
    state.page='cultivate';render();window.scrollTo(0,0);
  }

  function appUpdate() {return state.demo?null:(state.appUpdateStatus||state.project?.app_update||null);}
  function appIdentity() {return state.project?.application||appUpdate()?.application||null;}
  function versionText() {return appIdentity()?.display_version||'—';}
  function appUpdateActivationAllowed() {
    const s=projectData(),u=appUpdate();
    return !!host&&!state.demo&&!state.projectLost&&!state.exiting&&!state.busy&&!state.outcomeUnknown&&
      !state.controlUnknown&&!state.projectPending&&!state.appUpdatePending&&!u?.busy&&
      s?.bridge?.control_enabled===true&&!P.running(s)&&!s?.active_run?.run_id&&!s?.policy?.selection_run_id&&
      s?.live?.state?.in_progress!==true&&!(s?.control_requests?.pending_ids?.length);
  }
  function reconcileAppUpdate(snapshot) {
    if(snapshot?.app_update?.schema==='gkms.app-update-status.v1')state.appUpdateStatus=structuredClone(snapshot.app_update);
    const id=state.appUpdatePending;if(!id)return;
    const operation=appUpdate()?.operations?.[id];
    if(operation&&['completed','failed','interrupted'].includes(operation.status)){
      state.appUpdatePending=null;state.appUpdateError=operation.status==='completed'?null:operation.error||'appUpdateFailed';
      return;
    }
    const dispatched=snapshot?.control_requests?.completed?.[id];
    if(dispatched?.ok===false&&dispatched.pending!==true){state.appUpdatePending=null;state.appUpdateError=dispatched.error||'appUpdateFailed';}
  }
  function appUpdateBanner() {
    const update=appUpdate();
    if(update?.recovery_launch)return `<div class="app-update-banner" role="status"><span>${escape(interpolate('appRecoveryNotice',{version:update.recovery_target_version||appIdentity()?.version||'—'}))}</span>${btn('appUpdateOpen','app-update-open','ghost','refresh')}</div>`;
    if(!update||!(update.status==='available'||update.restart_required))return '';
    const key=update.restart_required?'appUpdateRestart':'appUpdateAvailable';
    return `<div class="app-update-banner" role="status"><span>${escape(interpolate(key,{version:update.restart_required?update.active_version:update.release?.version}))}</span>${btn('appUpdateOpen','app-update-open','ghost','download')}</div>`;
  }
  function appUpdateCard() {
    const u=appUpdate(), pending=!!state.appUpdatePending||!!u?.busy;
    const phase=Object.values(u?.operations||{}).find(row=>row.status==='running');
    const downloaded=Number.isFinite(phase?.bytes_received)?phase.bytes_received:0,total=phase?.bytes_total;
    const percent=Number.isFinite(total)&&total>0?Math.max(0,Math.min(100,100*downloaded/total)):0;
    const statusKey=({'unchecked':'appUpdateUnchecked','checking':'appUpdateChecking','no-release':'appUpdateNoRelease',
      'current':'appUpdateCurrent','available':'appUpdateAvailable','failed':'appUpdateCheckFailed'})[u?.status]||'appUpdateUnchecked';
    const stateText=interpolate(statusKey,{version:u?.release?.version||'—'});
    const connected=!!host&&!state.demo&&!state.projectLost&&!state.exiting;
    return `<section class="card app-update-card"><div class="card-heading"><span class="module-icon">${icon('download')}</span><h2>${txt('appUpdateTitle')}</h2></div>
      <p class="card-desc">${txt('appUpdateStartupNote')}</p><dl class="status-rows">${statusRow('currentVersion',escape(appIdentity()?.version||'—'))}${statusRow('appUpdateSource','fullpie/gkms-assistant · GUI · stable')}${statusRow('runStatus',escape(stateText))}</dl>
      ${u?.recovery_launch?`<p class="help">${escape(interpolate('appRecoveryNotice',{version:u.recovery_target_version||'—'}))}</p>`:''}
      ${u?.activation_warning?`<p class="help" role="status">${escape(interpolate('appActivationWarning',{version:u.active_version||'—'}))}</p>`:''}
      ${u?.release?.notes?`<details class="app-release-notes"><summary>${txt('appUpdateNotes')}</summary><pre>${escape(u.release.notes)}</pre></details>`:''}
      ${phase?`<div class="app-update-progress" role="status"><span>${txt(phase.phase==='downloading'?'appUpdateDownloading':phase.phase==='verifying'?'appUpdateVerifying':'appUpdateWorking')}</span>${total?`<progress max="100" value="${percent}"></progress><span>${escape((downloaded/1048576).toFixed(1))} / ${escape((total/1048576).toFixed(1))} MB</span>`:''}</div>`:''}
      ${u?.staged?`<p class="help">${escape(interpolate('appUpdateStaged',{version:u.staged.version}))}</p>`:''}
      ${u?.restart_required?`<p class="help">${escape(interpolate('appUpdateRestart',{version:u.active_version}))}</p>`:''}
      ${u&&u.managed_installation===false?`<p class="help">${txt('appUpdateUnmanaged')}</p>`:''}
      ${state.appUpdateError||u?.error?`<p class="help app-update-error" role="alert">${txt('appUpdateFailed')} <span>${escape(state.appUpdateError||u.error)}</span></p>`:''}
      <div class="button-row">${btn('appUpdateCheck','app-update-check','','refresh',!connected||pending)}${btn('appUpdateDownload','app-update-download','primary','download',!connected||pending||u?.status!=='available')}${btn('appUpdateApply','app-update-apply','','check',!appUpdateActivationAllowed()||u?.can_apply!==true)}${btn('appUpdateRollback','app-update-rollback','ghost','refresh',!appUpdateActivationAllowed()||u?.can_rollback!==true)}</div>
      <p class="help">${txt('appUpdateSafeBoundary')}</p></section>`;
  }
  function appUpdateReview(action) {
    const u=appUpdate();if(!appUpdateActivationAllowed()||!u)return;
    if(action==='app-update-apply'&&u.can_apply!==true||action==='app-update-rollback'&&u.can_rollback!==true)return;
    state.dialog={type:'app-update',action,version:action==='app-update-apply'?u.staged.version:u.previous_version,
      staged_id:action==='app-update-apply'?u.staged.id:null};
    renderDialog();$('dialog').showModal();
  }
  function renderAppUpdateDialog() {
    const d=state.dialog;if(!d)return;
    $('dialog').innerHTML=`<div class="dialog-head"><h2>${txt(d.action==='app-update-rollback'?'appUpdateRollback':'appUpdateApply')}</h2>${btn('close','close-dialog','ghost','close')}</div><p>${escape(interpolate('appUpdateConfirm',{version:d.version}))}</p><p class="help">${txt('appUpdateSafeBoundary')}</p><div class="button-row">${btn('cancel','close-dialog','ghost')}${btn('confirmUpdate','commit-app-update','primary','check',!appUpdateActivationAllowed())}</div>`;
  }
  async function appUpdateAction(action,dialog=null) {
    const u=appUpdate();if(!host||state.demo||state.projectLost||state.exiting||state.appUpdatePending||u?.busy)return;
    if(['app-update-apply','app-update-rollback'].includes(action)&&!appUpdateActivationAllowed())return;
    const operationId=crypto.randomUUID(),payload={operationId};
    if(action==='app-update-download'){
      if(u?.status!=='available'||!u.release)return;
      payload.release_id=u.release.release_id;payload.version=u.release.version;
    }
    if(action==='app-update-apply')payload.staged_id=dialog?.staged_id;
    state.appUpdatePending=operationId;state.appUpdateError=null;
    if(dialog){state.dialog=null;$('dialog').close();}render();
    try {
      const response=await host.command(action,payload);
      if(response.snapshot?.schema==='gkms.app-update-status.v1')state.appUpdateStatus=response.snapshot;
      reconcileAppUpdate({...state.project,app_update:state.appUpdateStatus});
      if(response.operation&&['completed','failed','interrupted'].includes(response.operation.status)){
        state.appUpdatePending=null;
        if(response.operation.status!=='completed')state.appUpdateError=response.operation.error||'appUpdateFailed';
      }
    }catch(error){state.appUpdateError=error.message||'appUpdateFailed';if(error.confirmed===true)state.appUpdatePending=null;}
    render();
  }
  function advancedPage() {
    const variants=projectData()?.catalog?.policy_variants||[];
    return `<header class="view-heading"><h1>${txt('advanced')}</h1><p>${txt('advancedNote')}</p></header>${appUpdateCard()}<div class="advanced-grid"><section class="card"><h2>${txt('modelEval')}</h2><div class="table-wrap"><table><thead><tr><th>${txt('model')}</th><th>${txt('modelState')}</th><th>${txt('stopReason')}</th></tr></thead><tbody>${P.variantIds.map(id=>{const v=variants.find(row=>row.id===id);return `<tr><td>${txt(id==='baseline'?'modelBaseline':'modelIntegrated')}</td><td>${txt(v?.available===true?'modelAvailable':'modelUnavailable')}</td><td>${escape(v?.reason||'—')}</td></tr>`;}).join('')}</tbody></table></div><p class="help">${txt('modelEvidenceNote')}</p></section></div>`+(existingProject()?'':restoreCard())+(projectData()?.developer?.available?'<div id="developerPanel"></div>':projectData()?.developer?.error?`<section class="card" role="status"><p>本機開發工具未啟用：${escape(projectData().developer.error)}</p></section>`:'');
  }
  function mountDeveloperPanel() {
    const container=$('developerPanel');if(!container||!projectData()?.developer?.available)return;
    if(window.GKMS_DEVELOPER_PANEL){
      developerPanelController=window.GKMS_DEVELOPER_PANEL.mount(container,projectData(),{command:host.command,snapshot:window.GKMS_PROJECT_HOST?.snapshot});return;
    }
    if(!developerPanelLoad){
      developerPanelLoad=new Promise((resolve,reject)=>{const script=document.createElement('script');script.src='/developer/panel.js';script.onload=resolve;script.onerror=reject;document.head.appendChild(script);});
      developerPanelLoad.then(()=>render()).catch(()=>{const current=$('developerPanel');if(current)current.textContent='本機開發面板載入失敗，請核對工具套件。';});
    }
  }
  function render() {
    if(!canNavigate(state.page))state.page='setup';
    const activeElement=document.activeElement,focusId=activeElement?.id,focusField=activeElement?.dataset?.field,caret=activeElement?.selectionStart;
    const editValue=activeElement?.tagName==='INPUT'?activeElement.value:null;
    const launchExpanded=$('launcherDetails')?.open===true;
    const loadoutScroll=Array.from($('app').querySelectorAll('[data-loadout-scroll]'),el=>[el.dataset.loadoutScroll,el.scrollTop]);
    document.documentElement.lang=locale;document.title=`GKMS · ${t(state.page)} · ${versionText()}`;
    document.body.className=variant;
    developerPanelController?.dispose?.();developerPanelController=null;
    $('app').innerHTML=`<div class="app"><aside class="sidebar"><div class="logo"><div class="logo-mark">${icon('star')}</div><div><div class="wordmark">GKMS</div><small>${txt('appName')}</small></div></div><div class="nav-caption">${txt('workspace')}</div><nav class="nav" aria-label="${txt('workspace')}">${[['setup','setup'],['cultivate','play'],['history','cards'],['advanced','settings']].map(([p,ic])=>`<button data-page="${p}" ${!canNavigate(p)?`disabled aria-disabled="true" title="${txt('gateNote')}"`:''} class="${state.page===p?'active':''}" ${state.page===p?'aria-current="page"':''}>${icon(ic)}<span>${txt(p)}</span>${state.page===p?'<i class="nav-dot"></i>':''}</button>`).join('')}</nav>${!modulesReady()?`<p class="nav-lock-note">${txt(loaderStatusKey()==='loaderNotInstalled'?'gateNote':'gateUnknown')}</p>`:''}<div class="side-info">${icon('globe')}<strong>${txt('choiceHeadline')}</strong><p>${txt('choiceNote')}</p></div><div class="side-version"><i class="dot"></i>GUI · ${escape(versionText())}</div></aside><div class="body"><header class="topbar"><div class="crumb"><span>${txt('workspace')}</span>${icon('chevron')}<b>${txt(state.page)}</b></div><div class="top-actions"><div class="connection-chip"><i class="dot"></i>${txt(state.demo?'demoBadge':state.projectLost?'stateDisconnected':state.connected?(existingProject()?'projectConnected':'backendConnected'):'preview')}</div>${state.page!=='cultivate'&&canStop()?btn('stop','stop','compact-stop','stop'):''}${langButtons()}${btn('exitAssistant','exit-assistant','exit-button','close',state.exiting)}</div></header><main class="main">${appUpdateBanner()}${state.page==='setup'?setupPage():state.page==='cultivate'?demoBanner()+cultivationPage():state.page==='history'?historyPage():advancedPage()}${state.exiting?`<div class="exit-state" role="status">${txt(state.exitLost?'exitDisconnected':'exitWaiting')}</div>`:''}${state.controlUnknown?notice('projectUnconfirmed'):''}${state.outcomeUnknown?notice('outcomeUnknown'):''}${state.operationNotice?`<div class="operation-notice" role="status">${txt(state.operationNotice)}${state.rawError?`<p class="help">${escape(state.rawError)}</p>`:''}</div>`:''}</main><footer class="footer"><i class="dot accent"></i><span>${txt(state.demo?'demoNote':state.connected?(existingProject()?'projectFooter':'connectedNote'):'previewNote')}</span><span class="footer-end">GKMS / GUI ${escape(versionText())}</span></footer></div></div>`;
    if($('launcherDetails'))$('launcherDetails').open=launchExpanded;
    for(const [key,top] of loadoutScroll){const element=$('app').querySelector(`[data-loadout-scroll="${key}"]`);if(element)element.scrollTop=top;}
    updateLauncherPanel();
    mountDeveloperPanel();
    if(state.dialog)renderDialog();
    if(state.toastKey)$('toast').textContent=t(state.toastKey);
    const focus=focusId?$(focusId):focusField?document.querySelector(`[data-field="${focusField}"]`):null;
    if(focus&&!focus.disabled&&['INPUT','SELECT'].includes(focus.tagName)) {if(editValue!==null&&focus.tagName==='INPUT')focus.value=editValue;focus.focus({preventScroll:true});try{if(Number.isInteger(caret))focus.setSelectionRange(caret,caret);}catch{}}
  }
  function notify(key) {
    state.toastKey=key;$('toast').textContent=t(key);$('toast').hidden=false;clearTimeout(notifyTimer);
    notifyTimer=setTimeout(()=>{state.toastKey=null;$('toast').hidden=true;},6000);
  }
  function setLocale(value) {
    if(!languages.includes(value))return;locale=value;localeEdits++;
    if(host&&!state.demo)persistPreferences({ui_locale:value});
    else try{localStorage.setItem(localeKey,locale);}catch{}
    render();
  }
  function checkedPreferences(data) {
    const p=data?.preferences;
    if(data?.schema!=='gkms.setup-preferences-status.v1'||typeof data.readOnly!=='boolean'||
      p?.schema!=='gkms.setup-preferences.v1'||!languages.includes(p.ui_locale)||typeof p.translation?.enabled!=='boolean'||
      typeof p.installation?.ask_launch!=='boolean'||typeof p.translation?.release_url!=='string')throw new Error(t('preferencesReadFailed'));
    R.parseSource(p.translation.release_url);return p;
  }
  async function loadPreferences() {
    if(!host||state.demo)return false;
    const languageRevision=localeEdits,settingsRevision=settingEdits;
    try {
      const data=await host.command('setup-preferences',{}),p=checkedPreferences(data);
      if(localeEdits===languageRevision)locale=p.ui_locale;
      if(settingEdits===settingsRevision){state.translationEnabled=p.translation.enabled;state.apiUrl=p.translation.release_url;state.restartPrompt=p.installation.ask_launch;}
      state.preferencesReady=true;state.preferencesReadOnly=data.readOnly;render();return true;
    }catch{state.preferencesReady=false;notify('preferencesReadFailed');return false;}
  }
  function persistPreferences(patch,showSaved=false) {
    const operationId=crypto.randomUUID(),frozen=structuredClone(patch);
    if(showSaved)notify('preferencesSaving');
    preferenceQueue=preferenceQueue.then(async()=>{
      await preferenceLoad;
      if(!state.preferencesReady)throw new Error(t('preferencesReadFailed'));
      if(state.preferencesReadOnly){notify('preferencesReadOnly');return;}
      const response=await host.command('save-setup-preferences',{operationId,patch:frozen});
      if(response?.ok!==true||response.saved!==true||response.operationId!==operationId)throw new Error(t('preferencesSaveFailed'));
      checkedPreferences(response.snapshot);if(showSaved)notify('saved');
    }).catch(()=>notify('preferencesSaveFailed'));
    return preferenceQueue;
  }
  function invalidateLookup() {
    requestGeneration++;currentAbort?.abort();currentAbort=null;state.checking=false;state.release=null;state.assetId=null;state.checkError=null;state.lastCheck=null;
  }
  async function checkRelease() {
    if(!state.translationEnabled||locked()||active().checking)return;
    if(state.demo) {state.demo.checkError=null;state.demo.lastCheck=new Date().toISOString();render();return;}
    const source=getSource();if(!source){state.checkError=state.apiUrl.trim()?'urlInvalid':'urlEmpty';render();return;}
    const generation=++requestGeneration,controller=new AbortController();currentAbort?.abort();currentAbort=controller;
    const timer=setTimeout(()=>controller.abort(),12000),before=state.release;
    state.checking=true;state.checkError=null;render();
    try {
      const release=host?.lookupRelease ? await host.lookupRelease({url:state.apiUrl,signal:controller.signal}) : await client.latest(source,{signal:controller.signal});
      if(generation!==requestGeneration||getSource()?.key!==source.key||state.demo)return;
      state.release=release;
      // Always resolve the package from the latest formal release, never from a UI choice.
      state.assetId=R.selectLatestAsset(release)?.id??null;
      state.lastCheck=new Date().toISOString();
    } catch(e) {
      if(generation!==requestGeneration||state.demo)return;
      state.checkError=e.key||'networkError';state.lastCheck=new Date().toISOString();
      // Keep installed state and the last fetched release; never turn a failed check into "latest".
      state.release=before;
    } finally {
      clearTimeout(timer);
      if(generation===requestGeneration){state.checking=false;currentAbort=null;render();}
    }
  }
  async function readLocalStatus(silent=false) {
    if(state.demo){if(!silent)notify('demoAction');return;}
    if(!host||typeof host.getStatus!=='function'){if(!silent)notify('backendNeeded');return;}
    if(state.busy)return;
    const dir=state.gameDir,generation=++setupStatusGeneration;
    try {
      const data=await host.getStatus({gameDirectory:dir});
      if(state.demo||generation!==setupStatusGeneration||dir!==state.gameDir)return;
      // An empty request asks the backend for its already saved location.
      // Validate that complete receipt first, then adopt only its resolved path.
      acceptSetupSnapshot(data,dir,{allowResolvedPath:dir===''});
      render();
    } catch {
      if(state.demo||generation!==setupStatusGeneration||dir!==state.gameDir)return;
      state.connected=false;state.installationVerified=false;if(!silent)notify('unknownRevision');render();
    }
  }
  function acceptSetupSnapshot(data,dir,{allowResolvedPath=false}={}) {
    if(projectMode&&data?.schema==='gkms.existing-project-setup.v1'&&data.integration_mode==='existing-project'){
      state.status=structuredClone(data);state.gameDir=data.gameDirectory||dir;
    }else {
      const expected=allowResolvedPath&&dir===''?data?.gameDirectory:dir;
      const verified=R.validateSnapshot(data,expected);
      state.status=verified;
      if(allowResolvedPath&&dir==='')state.gameDir=verified.gameDirectory;
    }
    if(!state.file&&data?.bundledControl?.stagedCandidateId)state.file=structuredClone(data.bundledControl);
    state.connected=true;state.installationVerified=true;state.outcomeUnknown=state.status.operationPending===true;
  }
  function settings() {
    let source=null;try{source=R.parseSource(state.apiUrl);}catch{}
    return {schema:'gkms.setup-preferences.v4',ui_locale:locale,game_directory:state.gameDir.trim(),
      installation:{ask_launch:state.restartPrompt},
      translation:{enabled:state.translationEnabled,release_url:source?state.apiUrl.trim():null,
        latest_api_url:source?.api||null,repository:source?.repository||null,
        update_mode:'manual',auto_install:false},
      restore:{scope:'installer-owned-files-only',mode:'remove-managed-files'},
      // The frontend never persists installed versions or successful installation flags.
      installation_state_source:'native-installer-manifest'};
  }
  function saveConfig() {
    if(state.demo){notify('demoNoSaving');return;}
    if(state.translationEnabled&&state.apiUrl.trim()&&!getSource()){notify('urlInvalid');return;}
    if(host){
      if(!getSource()){notify('urlInvalid');return;}
      persistPreferences({ui_locale:locale,translation:{enabled:state.translationEnabled,release_url:state.apiUrl.trim()},installation:{ask_launch:state.restartPrompt}},true);return;
    }
    try{localStorage.setItem(configKey,JSON.stringify(settings()));notify('savedPreview');}catch{notify('storageUnavailable');}
  }
  function demoSnapshot(installed=true,version='v1.2.0') {
    const translated=installed?[{path:'gakumas-local/local-files/ui.json',action:'added'},{path:'gakumas-local/local-files/story.json',action:'added'}]:[];
    const controlFiles=installed?[{path:'version.dll',action:'added'},{path:'gkms/native/gkms_runtime_command_bridge.dll',action:'added'}]:[];
    return {schema:'gkms.installer-status.v1',revision:'demo-'+Date.now(),gameDirectory:state.gameDir,
      safeToModify:true,operationPending:false,capabilities:['install_translation','restore_all','install_control'],
      modules:{control:installed?{known:true,installed:true,version:'v0.9.0-demo'}:{known:true,installed:false},translation:installed?{
        known:true,installed:true,version,repository:'demo-owner/translation-pack',releaseId:version==='v1.3.0'?1030:1020,
        assetId:version==='v1.3.0'?2030:2020,digest:'sha256:'+(version==='v1.3.0'?'b':'a').repeat(64),
        assetUpdatedAt:version==='v1.3.0'?'2026-09-15T02:00:00Z':'2026-09-01T02:00:00Z',publishedAt:version==='v1.3.0'?'2026-09-15T02:00:00Z':'2026-09-01T02:00:00Z'
      }:{known:true,installed:false}},
      files:[...controlFiles,...translated]};
  }
  const demoRelease=()=>({repository:'demo-owner/translation-pack',repositoryKey:'demo-owner/translation-pack',id:1030,tag:'v1.3.0',publishedAt:'2026-09-15T02:00:00Z',page:'https://github.com/demo-owner/translation-pack/releases/tag/v1.3.0',assets:[{id:2030,name:'translation-pack.zip',url:'https://github.com/demo-owner/translation-pack/releases/download/v1.3.0/translation-pack.zip',size:2516582,digest:'sha256:'+'b'.repeat(64),updatedAt:'2026-09-15T02:00:00Z'}]});
  let beforeDemo=null;
  function selectDemo(scenario) {
    if(state.busy||host||projectMode)return;
    if(!state.demo)beforeDemo={translationEnabled:state.translationEnabled,release:state.release,assetId:state.assetId,checkError:state.checkError,lastCheck:state.lastCheck,outcomeUnknown:state.outcomeUnknown};
    invalidateLookup(); // Invalidate in-flight real lookups before switching to fictitious records.
    state.demo={scenario,status:demoSnapshot(scenario!=='uninstalled',scenario==='latest'?'v1.3.0':'v1.2.0'),
      release:demoRelease(),assetId:2030,checking:false,checkError:scenario==='check-error'?'networkError':null,lastCheck:new Date().toISOString()};
    state.translationEnabled=true;state.page='setup';state.step=2;state.module='translation';state.operationNotice=null;state.outcomeUnknown=false;
    render();window.scrollTo(0,0);
  }
  function exitDemo() {
    if(state.busy)return;
    state.demo=null;Object.assign(state,beforeDemo||{translationEnabled:false});beforeDemo=null;state.operationNotice=null;
    render();
  }
  function canCommit(dialog) {
    if(state.busy||(state.outcomeUnknown&&dialog.type!=='recover'))return false;
    if(state.demo)return true;
    const s=state.status;
    if(!canPreview(dialog)||!dialog.preview||dialog.preview.expectedRevision!==s.revision||
      (dialog.preview.requiresTakeoverConfirmation&&dialog.approveTakeover!==true))return false;
    if(dialog.type==='recover')return true;
    if(dialog.type==='restore')return s.capabilities.includes('restore_all')&&typeof host.restoreAll==='function';
    if(dialog.module==='translation')return s.capabilities.includes('install_translation')&&typeof host.installTranslation==='function'&&!!selectedAsset();
    return s.capabilities.includes('install_control')&&typeof host.installControl==='function'&&!!state.file?.stagedCandidateId;
  }
  function installerAction(dialog) {return dialog.type==='recover'?'recover':dialog.type==='restore'?'restore-all':'install-'+dialog.module;}
  function canPreview(dialog) {
    const s=state.status;
    return !!host&&!state.demo&&!state.busy&&!state.loadoutPending&&!state.exiting&&
      (!state.outcomeUnknown||dialog.type==='recover')&&!state.projectPending&&!state.controlUnknown&&state.connected&&!!s&&
      s.readOnly!==true&&s.platformSupported===true&&s.canPreview===true&&
      s.previewActions?.includes('preview-'+installerAction(dialog))&&s.revision===dialog.plan.expectedRevision&&
      !!dialog.plan.gameDirectory&&(dialog.type==='recover'?s.operationPending===true:s.operationPending!==true);
  }
  function checkedInstallPreview(result,dialog,operationId) {
    const p=result?.preview,sha=value=>value===null||typeof value==='string'&&/^[a-f0-9]{64}$/.test(value);
    if(result?.ok!==true||result.committed!==false||result.confirmationRequired!==true||result.operationId!==operationId||
      p?.schema!=='gkms.install-preview.v1'||p.action!==installerAction(dialog)||p.gameDirectory!==dialog.plan.gameDirectory||
      p.expectedRevision!==dialog.plan.expectedRevision||typeof p.previewId!=='string'||!p.previewId||!sha(p.previewSha256)||!p.previewSha256||
      typeof p.requiresTakeoverConfirmation!=='boolean'||!Array.isArray(p.files)||
      p.files.some(f=>typeof f.path!=='string'||!['add','update','adopt','remove','restore'].includes(f.action)||
        !sha(f.beforeSha256)||!sha(f.afterSha256)||!sha(f.originalSha256)||typeof f.backupRequired!=='boolean'||typeof f.wouldChange!=='boolean'))
      throw new Error(t('installPreviewInvalid'));
    if(dialog.type==='recover'&&(typeof p.transactionId!=='string'||!p.transactionId||!sha(p.journalSha256)||!p.journalSha256||p.recoveryDirection!==dialog.direction))
      throw new Error(t('installPreviewInvalid'));
    return structuredClone(p);
  }
  async function previewOperation() {
    const d=state.dialog;if(!d||!canPreview(d))return;
    const operationId=crypto.randomUUID();state.busy='preview';d.preview=null;d.approveTakeover=false;d.previewError=null;d.proofExpanded=false;d.previewPage=0;render();
    try {
      const payload={...d.plan,operationId};
      if(d.type==='recover')payload.direction=d.direction;
      const result=await host.command('preview-'+installerAction(d),payload);
      d.preview=checkedInstallPreview(result,d,operationId);
    } catch(error){d.previewError=error.message||t('installPreviewInvalid');}
    finally{state.busy=null;render();}
  }
  function recoverReview() {
    if(state.busy||state.loadoutPending||state.exiting||state.controlUnknown)return;
    state.dialog={type:'recover',direction:'rollback',plan:{expectedRevision:snapshot()?.revision,gameDirectory:state.gameDir}};
    renderDialog();$('dialog').showModal();
  }
  function installerPreviewHtml(d) {
    if(state.demo)return '';
    const p=d.preview,pageCount=Math.max(1,Math.ceil((p?.files?.length||0)/50));
    d.previewPage=Math.min(Math.max(0,d.previewPage||0),pageCount-1);
    const shown=p?.files.slice(d.previewPage*50,(d.previewPage+1)*50)||[];
    const backupKey=f=>f.action==='restore'?'installRestoreOriginal':f.action==='remove'?'installRemoveAdded':
      f.backupRequired?'installBackupYes':f.originalSha256?'installBackupRetained':'installBackupNo';
    const rows=p?`<p class="help">${escape(interpolate('installFileRange',{start:p.files.length?d.previewPage*50+1:0,end:Math.min((d.previewPage+1)*50,p.files.length),count:p.files.length}))}</p><div class="table-wrap install-preview-table"><table><thead><tr>${['installFile','installChange','installBackupSummary'].map(k=>`<th>${txt(k)}</th>`).join('')}</tr></thead><tbody>${shown.map(f=>`<tr><td><code>${escape(f.path)}</code></td><td>${txt('installAction_'+f.action)}${f.wouldChange?'':' · '+txt('installNoChange')}</td><td>${txt(backupKey(f))}</td></tr>`).join('')}</tbody></table></div>
      <div class="button-row">${btn('installPreviousFiles','installer-prev-files','ghost','',d.previewPage===0||!!state.busy)}<span class="help">${d.previewPage+1} / ${pageCount}</span>${btn('installNextFiles','installer-next-files','ghost','',d.previewPage+1>=pageCount||!!state.busy)}</div><details class="install-verification" data-installer-proof ${d.proofExpanded?'open':''}><summary>${txt('installVerification')}</summary><div class="install-verification-files">${d.proofExpanded?shown.map(f=>`<div><strong><code>${escape(f.path)}</code></strong><dl>${[['installBeforeHash',f.beforeSha256],['installAfterHash',f.afterSha256],['installOriginalHash',f.originalSha256]].map(([key,hash])=>`<dt>${txt(key)}</dt><dd><code>${escape(hash||'—')}</code></dd>`).join('')}</dl></div>`).join(''):''}</div></details>
      ${p.requiresTakeoverConfirmation?`<label class="checkbox-row"><input type="checkbox" data-field="approveTakeover" ${d.approveTakeover?'checked':''} ${state.busy?'disabled':''}><span>${txt('installApproveTakeover')}</span></label>`:''}`:'';
    return `<section class="install-preview"><p class="help">${txt('installPreviewNote')}</p>${d.previewError?`<p role="alert">${escape(d.previewError)}</p>`:''}${rows}<div class="button-row">${btn(state.busy==='preview'?'installPreviewWorking':'installPreview','preview-operation','ghost','refresh',!canPreview(d))}</div></section>`;
  }
  function installReview(module) {
    if(locked())return;
    const d=decision();if(module==='translation'&&(!state.translationEnabled||!d.clickable))return;
    const rel=active().release,asset=selectedAsset();
    state.dialog={type:'install',module,kind:module==='translation'?d.kind:'install',plan:{
      expectedRevision:snapshot()?.revision||null,gameDirectory:state.gameDir,sourceUrl:state.apiUrl,
      operationId:globalThis.crypto?.randomUUID?.()||`${Date.now()}-${Math.random().toString(16).slice(2)}`,
      repository:module==='translation'?rel?.repository:null,releaseId:module==='translation'?rel?.id:null,
      version:module==='translation'?rel?.tag:null,publishedAt:module==='translation'?rel?.publishedAt:null,
      asset:module==='translation'?structuredClone(asset):null,
      candidate:module==='control'?state.file:null,previousVersion:record(module).installed?record(module).version:null,
      allowSourceChange:d.kind==='source-change'}};
    renderDialog();$('dialog').showModal();
  }
  function restoreReview() {
    if(locked())return;
    state.dialog={type:'restore',plan:{expectedRevision:snapshot()?.revision||null,gameDirectory:state.gameDir,sourceUrl:state.apiUrl,
      operationId:globalThis.crypto?.randomUUID?.()||`${Date.now()}-${Math.random().toString(16).slice(2)}`,
      files:structuredClone(snapshot()?.files||[])}};
    renderDialog();$('dialog').showModal();
  }
  function renderDialog() {
    const d=state.dialog;if(!d)return;
    if(d.type==='exit'){renderExitDialog();return;}
    if(d.type==='launcher'){renderLauncherDialog();return;}
    if(d.type==='post-install-launch'){renderPostInstallLaunchDialog();return;}
    if(d.type==='app-update'){renderAppUpdateDialog();return;}
    if(d.type==='loadout'){renderLoadoutDialog();return;}
    const scrolls=['.install-preview-table','.install-verification-files'].map(selector=>[selector,$('dialog').querySelector(selector)?.scrollTop||0]);
    const restore=d.type==='restore',recover=d.type==='recover',available=canCommit(d),updates=['update','rebuild','source-change'].includes(d.kind);
    const title=restore?'restoreConfirmTitle':'dialogTitle';
    const header=`<div class="dialog-head"><h2>${txt(title)}</h2>${btn('close','close-dialog','ghost','close',!!state.busy)}</div>${state.demo?notice('demoNote'):''}`;
    let body;
    if(recover) {
      body=`<p>${txt('installRecoverNote')}</p><label class="field"><span>${txt('installRecoveryDirection')}</span><select data-field="recoveryDirection" ${state.busy?'disabled':''}><option value="rollback" ${d.direction==='rollback'?'selected':''}>${txt('installRecoveryRollback')}</option><option value="forward" ${d.direction==='forward'?'selected':''}>${txt('installRecoveryForward')}</option></select></label>`;
    } else if(restore) {
      body=`<p>${txt('restoreNote')}</p>${notice('restoreProtect','shield')}<div class="destination"><span>${txt('destination')}</span>${escape(d.plan.gameDirectory||t('unset'))}</div>
        <p class="help">${txt('restoreBefore')}</p>`;
    } else {
      body=`<p>${txt('dialogNote')}</p><div class="destination"><span>${txt(d.module)}</span><strong>${escape(d.module==='translation'?d.plan.asset?.name:(state.file?.name||'GKMS portable control ZIP'))}</strong></div><div class="destination"><span>${txt('destination')}</span>${escape(d.plan.gameDirectory||t('unset'))}</div>
        ${d.module==='translation'?`<div class="source-frozen dialog-source">${txt('trackedSource')}: ${escape(d.plan.repository)}<br>${txt('currentVersion')}: ${escape(d.plan.previousVersion||t('installedNone'))} → ${escape(d.plan.version)}</div>`:''}
        ${!d.preview?`<section class="plan"><strong>${txt('installPlan')}</strong><ol>${['plan1','plan2','plan3','plan4'].map(k=>`<li>${txt(k)}</li>`).join('')}</ol></section>`:''}
        ${d.kind==='source-change'?notice('switchSourceNote'):''}`;
    }
    $('dialog').innerHTML=header+body+installerPreviewHtml(d)+
      `<div class="button-row dialog-actions">${btn('cancel','close-dialog','ghost','',!!state.busy)}${btn(state.busy?(restore?'restoring':updates?'updating':'installing'):recover?'recover':restore?'restoreConfirm':updates?'confirmUpdate':'confirmInstall','commit-operation',restore?'danger':'primary',restore?'refresh':'download',!available)}</div>`;
    for(const [selector,top] of scrolls){const element=$('dialog').querySelector(selector);if(element)element.scrollTop=top;}
  }
  async function commitOperation() {
    const d=state.dialog;if(!d||!canCommit(d))return;
    if(d.type==='restore'&&!d.plan.files.length)return;
    const frozen=structuredClone(d),demo=state.demo;
    let promptLaunch=false;
    if(!demo)frozen.plan.operationId=crypto.randomUUID();
    state.busy=d.type==='restore'?'restore':['update','rebuild','source-change'].includes(d.kind)?'update':'install';state.operationNotice=null;render();
    try {
      let result;
      if(demo) {
        // Deliberately synthetic receipts, confined to the labelled in-memory demo.
        await new Promise(resolve=>setTimeout(resolve,250));
        if(demo.scenario==='install-error'&&d.type!=='restore')result={ok:false,unchanged:true};
        else {
          let next=structuredClone(demo.status);next.revision='demo-'+Date.now();
          if(d.type==='restore') {next.files=[];for(const name of Object.keys(next.modules))next.modules[name]={known:true,installed:false};}
          else if(d.module==='translation') {
            next.modules.translation={known:true,installed:true,version:frozen.plan.version,repository:frozen.plan.repository,releaseId:frozen.plan.releaseId,
              assetId:frozen.plan.asset.id,digest:frozen.plan.asset.digest,assetUpdatedAt:frozen.plan.asset.updatedAt,publishedAt:frozen.plan.publishedAt};
            for(const f of [{path:'gakumas-local/local-files/ui.json',action:'added'},{path:'gakumas-local/local-files/story.json',action:'added'}])if(!next.files.some(x=>x.path===f.path))next.files.push(f);
          } else {
            next.modules.control={known:true,installed:true,version:'v0.9.0-demo'};
            for(const path of ['version.dll','gkms/native/gkms_runtime_command_bridge.dll'])
              if(!next.files.some(f=>f.path===path))next.files.push({path,action:'added'});
          }
          result={ok:true,committed:true,operationId:frozen.plan.operationId,snapshot:next};
        }
      } else {
        result=await host.command(installerAction(frozen),{operationId:frozen.plan.operationId,gameDirectory:frozen.plan.gameDirectory,
          expectedRevision:frozen.plan.expectedRevision,previewId:frozen.preview.previewId,previewSha256:frozen.preview.previewSha256,
          approveTakeover:frozen.approveTakeover===true,...(frozen.type==='recover'?{transactionId:frozen.preview.transactionId,
            journalSha256:frozen.preview.journalSha256,recoveryDirection:frozen.preview.recoveryDirection}:{})});
      }
      if(result?.ok===false&&result.unchanged===true) {
        state.operationNotice=demo?'demoFailureDone':d.type==='restore'?'restoreFailed':'installFailed';notify(state.operationNotice);
      } else {
        if(result?.ok!==true||result.committed!==true||result.operationId!==frozen.plan.operationId)throw new Error('unconfirmed');
        const next=R.validateSnapshot(result.snapshot,frozen.plan.gameDirectory);
        if(next.revision===frozen.plan.expectedRevision||next.operationPending)throw new Error('unconfirmed');
        if(d.type==='recover') {
          // Recovery has its own hash-bound file plan; do not infer a module install.
        } else if(d.type==='restore') {
          if(next.files.length||next.modules.translation.installed||next.modules.control.installed)throw new Error('partial restore');
        } else if(d.module==='translation') {
          const m=next.modules.translation;
          if(!m.installed||m.version!==frozen.plan.version||m.releaseId!==frozen.plan.releaseId||m.assetId!==frozen.plan.asset.id||m.repository?.toLowerCase()!==frozen.plan.repository.toLowerCase())throw new Error('receipt mismatch');
        } else if(!next.modules.control.installed)throw new Error('receipt mismatch');
        if(demo){demo.status=next;demo.scenario=d.type==='restore'?'uninstalled':'latest';}
        else {state.status=next;state.installationVerified=true;state.connected=true;}
        state.operationNotice=demo?(d.type==='restore'?'demoRestoreDone':'demoInstallDone'):(d.type==='restore'?'restored':'installedSuccess');notify(state.operationNotice);
        promptLaunch=!demo&&d.type==='install'&&state.restartPrompt;
      }
    } catch(error) {
      state.outcomeUnknown=error.confirmed!==true;state.operationNotice=state.outcomeUnknown?'outcomeUnknown':'operationFailed';
      state.rawError=error.message;notify(state.operationNotice);
    } finally {
      state.busy=null;state.dialog=null;$('dialog').close();render();
      if(promptLaunch&&!state.exiting)await reviewPostInstallLaunch(frozen.plan.gameDirectory);
    }
  }
  async function chooseDll() {
    if(host?.chooseControlModule&&!state.demo) {
      try {const candidate=await host.chooseControlModule({gameDirectory:state.gameDir});if(candidate?.stagedCandidateId&&candidate?.name&&candidate?.sha256)state.file=candidate;render();}catch{notify('fileReadError');}
    } else $('dllFile').click();
  }
  async function readDll(file) {
    if(!file)return;
    if(!file.name.toLowerCase().endsWith('.zip')||file.size>256*1024*1024){notify('fileWrong');return;}
    try {const buffer=await file.arrayBuffer();const hash=await crypto.subtle.digest('SHA-256',buffer);
      state.file={name:file.name,size:file.size,sha256:Array.from(new Uint8Array(hash),b=>b.toString(16).padStart(2,'0')).join('')};render();notify('previewZipOnly');
    }catch{notify('fileReadError');}finally{$('dllFile').value='';}
  }
  async function setupAction(action) {
    if(state.demo){notify('demoAction');return;}
    if(!host||state.busy){notify('backendNeeded');return;}
    state.busy='setup';render();
    try {
      const result=await host.command(action,{gameDirectory:state.gameDir});
      if(result.gameDirectory){setupStatusGeneration++;state.gameDir=result.gameDirectory;state.status=null;state.connected=false;state.installationVerified=false;state.file=null;}
      if(result.snapshot)acceptSetupSnapshot(result.snapshot,state.gameDir);
      if(result.launcherSnapshot)state.launcherStatus=result.launcherSnapshot;
      state.operationNotice=action==='launcher-launch'?'launchSubmitted':action==='detect'?'pathDetected':'operationComplete';
    } catch(e){state.operationNotice='operationFailed';state.rawError=String(e.message||'').slice(0,500);}
    finally{state.busy=null;render();if(state.gameDir)await readLocalStatus(true);}
  }
  document.addEventListener('click',e=>{
    const lang=e.target.closest('[data-locale]');if(lang){setLocale(lang.dataset.locale);return;}
    const nav=e.target.closest('[data-page]');if(nav){if(nav.disabled||!canNavigate(nav.dataset.page)||state.exiting)return;state.page=nav.dataset.page;render();window.scrollTo(0,0);return;}
    const step=e.target.closest('[data-step]');if(step){state.step=Number(step.dataset.step);render();return;}
    const mod=e.target.closest('[data-module]');if(mod){state.module=mod.dataset.module;render();return;}
    const b=e.target.closest('[data-action]');if(!b||b.disabled)return;
    const action=b.dataset.action;if(action.startsWith('demo-')){selectDemo(action.slice(5));return;}
    switch(action) {
      case 'detect-game':setupAction('detect');break;
      case 'choose-folder':setupAction('choose-folder');break;
      case 'launcher-launch':launcherAction(action);break;
      case 'confirm-post-install-launch':if(state.dialog?.type==='post-install-launch'&&!state.dialog.checking&&state.dialog.gameDirectory===state.gameDir)launcherAction('launcher-launch');break;
      case 'launcher-sync':case 'launcher-repair':launcherReview(action);break;
      case 'commit-launcher':if(state.dialog?.type==='launcher'){const d=state.dialog;launcherAction(d.action,d.action==='launcher-sync'?{closeDmm:d.closeDmm,forceCloseDmm:d.forceCloseDmm}:{});}break;
      case 'launcher-demo-ready':launcherDemo('ready');break;
      case 'launcher-demo-monitoring':launcherDemo('monitoring');break;
      case 'launcher-demo-failed':launcherDemo('needs-refresh');break;
      case 'recover':recoverReview();break;
      case 'start':projectCommand('_start_console_autopilot');break;
      case 'stop':requestStop();break;
      case 'exit-assistant':reviewExit();break;
      case 'confirm-exit':confirmExit();break;
      case 'session-demo':sessionDemo();break;
      case 'check-release':checkRelease();break;
      case 'app-update-open':state.page='advanced';render();break;
      case 'app-update-check':case 'app-update-download':appUpdateAction(action);break;
      case 'app-update-apply':case 'app-update-rollback':appUpdateReview(action);break;
      case 'commit-app-update':if(state.dialog?.type==='app-update')appUpdateAction(state.dialog.action,state.dialog);break;
      case 'loadout-refresh':case 'loadout-read':case 'loadout-recommend':case 'loadout-constraints':loadoutAction(action.slice(8));break;
      case 'loadout-clear-memory-exclusions':if(loadoutCan('constraints')){loadoutView.clearMemoryExclusions(projectData()?.loadout);loadoutAction('constraints');}break;
      case 'loadout-review':loadoutReview();break;
      case 'loadout-commit':if(state.dialog?.type==='loadout')loadoutAction('apply',state.dialog.payload);break;
      case 'check-status':readLocalStatus();break;
      case 'install-translation':installReview('translation');break;
      case 'install-control':installReview('control');break;
      case 'restore-all':restoreReview();break;
      case 'commit-operation':commitOperation();break;
      case 'preview-operation':previewOperation();break;
      case 'installer-prev-files':case 'installer-next-files':if(state.dialog?.preview&&!state.busy){state.dialog.previewPage=(state.dialog.previewPage||0)+(action==='installer-next-files'?1:-1);state.dialog.proofExpanded=false;renderDialog();}break;
      case 'choose-dll':chooseDll();break;
      case 'exit-demo':exitDemo();break;
      case 'save':saveConfig();break;
      case 'go-cultivate':if(!canNavigate('cultivate')||state.exiting)return;state.page='cultivate';render();window.scrollTo(0,0);break;
      case 'go-setup':state.page='setup';render();window.scrollTo(0,0);break;
      case 'step-back':state.step=Math.max(0,state.step-1);render();break;
      case 'step-next':state.step=Math.min(3,state.step+1);render();break;
      case 'close-dialog':if(!state.busy){state.dialog=null;$('dialog').close();}break;
    }
  });
  document.addEventListener('toggle',e=>{
    const section=e.target.dataset?.loadoutSection;if(section)loadoutView.toggle(section,e.target.open===true);
    if(e.target.dataset&&Object.hasOwn(e.target.dataset,'installerProof')&&state.dialog?.preview&&state.dialog.proofExpanded!==(e.target.open===true)){state.dialog.proofExpanded=e.target.open===true;renderDialog();}
  },true);
  document.addEventListener('input',e=>{
    const f=e.target.dataset.field;if(!f||locked())return;
    if(['apiUrl','translationEnabled','restartPrompt'].includes(f))settingEdits++;
    if(f==='apiUrl'&&!state.demo) {
      state.apiUrl=e.target.value;invalidateLookup();render();
    } else if(f==='gameDir') {
      setupStatusGeneration++;
      state.gameDir=e.target.value;
      if(!state.demo){state.status=null;state.connected=false;state.installationVerified=false;state.file=null;}
      render();
    }
  });
  document.addEventListener('change',e=>{
    const f=e.target.dataset.field;if(!f||state.busy||state.loadoutPending||state.exiting)return;
    if(f==='approveTakeover'&&state.dialog?.preview){state.dialog.approveTakeover=e.target.checked===true;renderDialog();return;}
    if(f==='recoveryDirection'&&state.dialog?.type==='recover'&&['rollback','forward'].includes(e.target.value)){state.dialog.direction=e.target.value;state.dialog.preview=null;state.dialog.approveTakeover=false;renderDialog();return;}
    if(locked())return;
    if(f.startsWith('loadout')){if(loadoutCan(f==='loadoutProposal'?'apply':'constraints')&&loadoutView.change(f,e.target.value,projectData()?.loadout))render();return;}
    if(state.dialog?.type==='launcher'&&['closeDmm','forceCloseDmm'].includes(f)){state.dialog[f]=e.target.checked;if(!state.dialog.closeDmm)state.dialog.forceCloseDmm=false;renderLauncherDialog();return;}
    if(window.GKMS_PROJECT_HOST && ['projectIdol','projectMode','policyVariant','runCount'].includes(f)){projectSelection(f,e.target.value);return;}
    if(['translationEnabled','restartPrompt'].includes(f)){settingEdits++;state[f]=e.target.checked;if(f==='translationEnabled'&&!state[f])invalidateLookup();render();}
    else if(f==='apiUrl'){if(state.apiUrl.trim()&&!getSource()){state.checkError='urlInvalid';if($('releaseStatus'))$('releaseStatus').textContent=t('urlInvalid');}}
    else if(f==='gameDir'){/* Input handler already invalidated records; do not detach a button during blur/click. */}
    else if(f==='policyVariant'&&['baseline','integrated'].includes(e.target.value))state[f]=e.target.value;
    else if(f==='runCount'){const n=Number(e.target.value);if(Number.isInteger(n)&&n>=1&&n<=999)state.runCount=n;else{e.target.value=state.runCount;notify('countInvalid');}}
  });
  $('dllFile').addEventListener('change',e=>readDll(e.target.files?.[0]));
  $('dialog').addEventListener('cancel',e=>{if(state.busy)e.preventDefault();else state.dialog=null;});
  window.GKMS_SETUP_PREVIEW=Object.freeze({
    getPreferences:()=>structuredClone(settings()),
    getStatus:()=>({demo:!!state.demo,decision:decision(),modules:structuredClone(snapshot()?.modules||{}),files:structuredClone(snapshot()?.files||[]),connected:state.connected,busy:state.busy,outcomeUnknown:state.outcomeUnknown}),
    getSession:()=>structuredClone(sessionData()),showSessionDemo:sessionDemo,modulesReady,
    getLauncherStatus:()=>structuredClone(launcherSnapshot()),showLauncherDemo:launcherDemo,
    parseSource:R.parseSource,checkRelease
  });
  window.GKMS=Object.freeze({getSnapshot:()=>structuredClone(projectData()),getSelection:()=>structuredClone(projectValues())});
  render();
  projectPoll();
  launcherPoll();
  // The host performs one background GUI release check per process start.
  // Translation release lookup remains a separate explicit user action.
  readLocalStatus(true);
  preferenceLoad=loadPreferences();
})();
