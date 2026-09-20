/* Cached account loadout presentation. Only explicit user actions request work. */
'use strict';
((root) => {
  const defaults = () => ({locked_support_ids:[],excluded_support_ids:[],locked_memory_ids:[],excluded_memory_ids:[],locked_rental_key:null});
  const valid = value => value?.schema==='gkms.gui-loadout.v1'&&value.available===true;
  function create() {
    let key=null,draft=defaults(),dirty=false,selected=null;
    const expanded=new Set();
    function toggle(section,open) {if(['support','memory','current'].includes(section)){if(open)expanded.add(section);else expanded.delete(section);}}
    function observe(value) {
      const next=valid(value)?JSON.stringify([value.inventory_digest,value.scope]):null;
      if(next!==key){key=next;dirty=false;selected=null;}
      if(!dirty)draft={...defaults(),...structuredClone(value?.constraints||{})};
      if(selected&&!value?.recommendations?.some(row=>row.id===selected))selected=null;
    }
    function change(field,value,snapshot) {
      observe(snapshot);
      if(field==='loadoutProposal') {selected=snapshot?.recommendations?.some(row=>row.id===value)?value:null;return true;}
      if(field==='loadoutRental') {draft.locked_rental_key=value||null;dirty=true;return true;}
      const family=field.startsWith('loadoutSupport:')?'support':field.startsWith('loadoutMemory:')?'memory':null;
      if(!family||!['any','lock','exclude'].includes(value))return false;
      if(family==='memory'&&value==='exclude'&&snapshot.memory_exclusion_supported===false)return false;
      const id=field.slice(field.indexOf(':')+1), rows=snapshot?.inventory?.[family==='support'?'support_cards':'memories']||[];
      if(!rows.some(row=>(family==='support'?row.card_id:row.memory_id)===id))return false;
      for(const kind of ['locked','excluded'])draft[`${kind}_${family}_ids`]=draft[`${kind}_${family}_ids`].filter(item=>item!==id);
      if(value!=='any')draft[`${value==='lock'?'locked':'excluded'}_${family}_ids`].push(id);
      dirty=true;return true;
    }
    function proposal(snapshot) {observe(snapshot);return snapshot?.recommendations?.find(row=>row.id===selected)||null;}
    function payload(snapshot) {const row=proposal(snapshot);return row&&!dirty?{inventory_digest:snapshot.inventory_digest,recommendation_id:row.id}:null;}
    function saved(snapshot) {dirty=false;observe(snapshot);}
    function clearMemoryExclusions(snapshot) {observe(snapshot);draft.excluded_memory_ids=[];dirty=true;}
    function render(snapshot,{t,escape:e,allowed}) {
      observe(snapshot);
      const button=(label,action,disabled)=>`<button class="btn" data-action="loadout-${action}" ${disabled?'disabled':''}>${e(t(label))}</button>`;
      if(!valid(snapshot))return `<section class="card loadout-panel"><h2>${e(t('loadoutTitle'))}</h2><p class="help">${e(t('loadoutUnavailable'))}</p></section>`;
      const names=snapshot.names||{},inventory=snapshot.inventory;
      const supportName=id=>names[id]||id;
      const memoryName=id=>{const row=inventory?.memories?.find(item=>item.memory_id===id);return row?[names[row.produce_card?.id]||row.produce_card?.id||t('loadoutNoInheritedCard'),names[row.idol_card_id]||row.idol_card_id,id].join(' · '):id;};
      const options=(family,id)=>['any','lock','exclude'].filter(value=>value!=='exclude'||family!=='memory'||snapshot.memory_exclusion_supported!==false||draft.excluded_memory_ids.includes(id)).map(value=>`<option value="${value}" ${family==='memory'&&value==='exclude'&&snapshot.memory_exclusion_supported===false?'disabled':''} ${value===(draft[`locked_${family}_ids`].includes(id)?'lock':draft[`excluded_${family}_ids`].includes(id)?'exclude':'any')?'selected':''}>${e(t('loadoutPref_'+value))}</option>`).join('');
      const table=(family,rows)=>`<details class="loadout-library" data-loadout-section="${family}" ${expanded.has(family)?'open':''}><summary>${e(t(family==='support'?'loadoutSupports':'loadoutMemories'))} (${rows.length})</summary><div class="loadout-card-list" data-loadout-scroll="${family}">${rows.map(row=>{const id=family==='support'?row.card_id:row.memory_id;return `<label class="loadout-card"><span>${e(family==='support'?supportName(id):memoryName(id))}${family==='support'?` <small>Lv. ${e(row.level)}</small>`:''}</span><select data-field="${family==='support'?'loadoutSupport:':'loadoutMemory:'}${e(id)}" ${allowed('constraints')?'':'disabled'}>${options(family,id)}</select></label>`;}).join('')}</div></details>`;
      const chosen=proposal(snapshot), selection=chosen?.selection;
      const current=snapshot.loadout;
      const currentRows=(family)=>{
        const field=family==='support'?'support_cards':'memories';
        if(!['ready','empty'].includes(current?.[field+'_state'])||!Array.isArray(current?.[field]))return e(t('loadoutNotObserved'));
        return current[field].length?current[field].map(row=>row?e((family==='support'?supportName(row.card_id):memoryName(row.memory_id))+(row.is_rental?' · '+t('loadoutRental'):'')):e(t('loadoutEmptySlot'))).join('<br>'):e(t('loadoutEmptySlot'));
      };
      return `<section class="card loadout-panel"><div class="card-heading"><h2>${e(t('loadoutTitle'))}</h2></div><p class="card-desc">${e(t('loadoutNote'))}</p><p class="help" role="status">${e(snapshot.status||t('loadoutNoInventory'))}</p>
        <div class="button-row">${button('loadoutRefresh','refresh',!allowed('refresh'))}${button(snapshot.can_reconcile_pending?'loadoutReconcile':'loadoutRead','read',!allowed('read'))}${button('loadoutRecommend','recommend',!allowed('recommend')||dirty)}</div>
        ${snapshot.pending_apply||snapshot.pending_invalid?`<p class="help" role="status">${e(t('loadoutPending'))}${snapshot.pending_error?' '+e(snapshot.pending_error):''}</p>`:''}
        ${current?`<details class="loadout-library" data-loadout-section="current" ${expanded.has('current')?'open':''}><summary>${e(t('loadoutCurrent'))} · ${e(current.captured_at||'—')}</summary><dl class="status-rows"><div class="status-row"><dt>${e(t('loadoutSupports'))}</dt><dd>${currentRows('support')}</dd></div><div class="status-row"><dt>${e(t('loadoutMemories'))}</dt><dd>${currentRows('memory')}</dd></div></dl></details>`:''}
        <p class="help">${e(t('loadoutMemoryAuto'))}</p>
        ${draft.excluded_memory_ids.length&&snapshot.memory_exclusion_supported===false?`<p class="help" role="alert">${e(t('loadoutLegacyExclusions'))}</p>${button('loadoutClearMemoryExclusions','clear-memory-exclusions',!allowed('constraints'))}`:''}
        ${inventory?`<p class="help">${e(t('loadoutInventoryTime'))}: ${e(inventory.captured_at||'—')}</p>${table('support',inventory.support_cards||[])}${table('memory',inventory.memories||[])}
          <label class="field"><span>${e(t('loadoutRental'))}</span><select data-field="loadoutRental" ${allowed('constraints')?'':'disabled'}><option value="">${e(t('loadoutPref_any'))}</option>${(snapshot.rentals||[]).map(row=>`<option value="${e(row.rental_key)}" ${row.rental_key===draft.locked_rental_key?'selected':''}>${e(row.name||row.card_id)} · Lv. ${e(row.level)}</option>`).join('')}</select></label>
          <div class="button-row">${button('loadoutSaveConstraints','constraints',!allowed('constraints')||!dirty)}</div>${dirty?`<p class="help">${e(t('loadoutUnsaved'))}</p>`:''}`:''}
        <label class="field"><span>${e(t('loadoutProposal'))}</span><select data-field="loadoutProposal" ${allowed('apply')&&!dirty?'':'disabled'}><option value="">${e(t('loadoutChooseProposal'))}</option>${(snapshot.recommendations||[]).map((row,index)=>`<option value="${e(row.id)}" ${row.id===selected?'selected':''}>${e(t('loadoutProposal'))} ${index+1}</option>`).join('')}</select></label>
        ${selection?`<dl class="status-rows"><div class="status-row"><dt>${e(t('loadoutSupports'))}</dt><dd>${(selection.support_card_ids||[]).map(id=>e(supportName(id))).join('<br>')}</dd></div><div class="status-row"><dt>${e(t('loadoutRental'))}</dt><dd>${e(supportName(selection.borrowed_support?.card_id||''))}</dd></div><div class="status-row"><dt>${e(t('loadoutMemories'))}</dt><dd>${e(t('loadoutMemoryAuto'))}</dd></div></dl><p class="help">${(chosen.reasons||[]).map(e).join(' · ')}</p>`:''}
        <p class="help">${e(t('loadoutRankingNote'))}</p><div class="button-row">${button('loadoutApply','review',!allowed('apply')||!payload(snapshot))}</div></section>`;
    }
    return Object.freeze({observe,change,proposal,payload,saved,render,toggle,clearMemoryExclusions,constraints:()=>structuredClone(draft),dirty:()=>dirty});
  }
  const api=Object.freeze({create,valid});
  if(typeof module==='object'&&module.exports)module.exports=api;else root.GKMS_LOADOUT=api;
})(typeof window==='object'?window:globalThis);
