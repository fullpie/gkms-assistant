/* Pure view projection and control guards; never submit game operations. */
'use strict';
((root) => {
  const variantIds = Object.freeze(['rl_shared_iql']);
  // Display aliases only. Exact checkpoint identity, qualification and selection
  // stay with the host; a different checkpoint does not inherit either name.
  const rlNames = Object.freeze({
    'f5427391136b205b4b5b3d9b89686480c10a3fcf843b6f9f5d5005c3e19fae1b': 'RL_v0',
    '9ca3b3dcdcebf8797fe9d7a3b8f1b63830eeb977bce6983fa5615a9344c9d1bf': 'RL_v1'
  });
  const rlName = sha => typeof sha === 'string' ? rlNames[sha.toLowerCase()] : undefined;
  function projectSnapshot(input) {
    if (!input || input.schema !== 'gkms.glass-view.v1' || !input.selection || !input.catalog || !input.live)
      throw new Error('Incompatible project snapshot');
    const result = structuredClone(input);
    result.selection = {policy_variant_id: 'rl_shared_iql', ...result.selection};
    for (const row of result.catalog.policy_variants || []) {
      const name = row.id === 'rl_shared_iql' && rlName(row.model_sha256);
      if (name) { row.label = name; row.model_label = name; }
    }
    const selected = result.models?.selected_view;
    const selectedName = selected?.variant_id === 'rl_shared_iql' && rlName(selected.model_sha256);
    if (selectedName) {
      const prior = selected.label;
      selected.label = selectedName;
      if (result.models.rows?.[0]) result.models.rows[0][0] = selectedName;
      if (typeof prior === 'string' && prior) {
        if (typeof result.models.detail === 'string') result.models.detail = result.models.detail.replace(prior, selectedName);
        for (const key of ['selected_label', 'configured_owner_label'])
          if (typeof result.policy?.[key] === 'string') result.policy[key] = result.policy[key].replace(prior, selectedName);
      }
    }
    const actualName = result.policy?.actual_variant_id === 'rl_shared_iql' && rlName(result.policy.actual_model_sha256);
    if (actualName && result.policy.actual_model_label) result.policy.actual_model_label = actualName;
    if (result.live.state?.in_progress === false) {
      result.active_run = null;
      result.live = {...result.live, state: {in_progress: false}, cards: [], drinks: [],
        legal_actions: [], recommendations: [], last_action: null};
    }
    return result;
  }
  const running = project => Boolean(project?.live?.busy || project?.batch?.running ||
    project?.live?.pending_transaction ||
    ['running', 'cancelling'].includes(project?.live?.phase));
  function editingAllowed(project, ui) {
    return Boolean(project?.bridge?.connected === true && project.bridge.control_enabled === true &&
      !running(project) && !ui.projectPending && !ui.busy && !ui.exiting && !ui.projectLost &&
      !ui.outcomeUnknown && !ui.controlUnknown && !(project.control_requests?.pending_ids?.length) && ui.modulesReady === true);
  }
  const selectableVariantIds = project => {
    const rl=project?.catalog?.policy_variants?.find(item=>item.id==='rl_shared_iql');
    return rl?.fixed_weights===true?[...variantIds]:[];
  };
  const availableVariant = (project, id) => selectableVariantIds(project).includes(id) &&
    project?.catalog?.policy_variants?.some(item => item.id === id && item.available === true &&
      (id!=='rl_shared_iql'||item.live_ready===true));
  const modelSelectionLocked = project => project?.policy?.selection_locked === true || Boolean(project?.active_run?.run_id);
  function startAllowed(project, ui) {
    return editingAllowed(project, ui) && project.live.start_enabled === true &&
      Boolean(project.selection.idol_card_id) && availableVariant(project, project.selection.policy_variant_id);
  }
  const api = Object.freeze({variantIds, selectableVariantIds, projectSnapshot, running, editingAllowed, availableVariant, modelSelectionLocked, startAllowed});
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.GKMS_PROJECT_STATE = api;
})(typeof window === 'object' ? window : globalThis);
