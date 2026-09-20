/* Pure view projection and control guards; never submit game operations. */
'use strict';
((root) => {
  const variantIds = Object.freeze(['baseline', 'integrated']);
  function projectSnapshot(input) {
    if (!input || input.schema !== 'gkms.glass-view.v1' || !input.selection || !input.catalog || !input.live)
      throw new Error('Incompatible project snapshot');
    const result = structuredClone(input);
    result.selection = {policy_variant_id: 'baseline', ...result.selection};
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
  const availableVariant = (project, id) => variantIds.includes(id) &&
    project?.catalog?.policy_variants?.some(item => item.id === id && item.available === true);
  const modelSelectionLocked = project => project?.policy?.selection_locked === true || Boolean(project?.active_run?.run_id);
  function startAllowed(project, ui) {
    return editingAllowed(project, ui) && project.live.start_enabled === true &&
      Boolean(project.selection.idol_card_id) && availableVariant(project, project.selection.policy_variant_id);
  }
  const api = Object.freeze({variantIds, projectSnapshot, running, editingAllowed, availableVariant, modelSelectionLocked, startAllowed});
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.GKMS_PROJECT_STATE = api;
})(typeof window === 'object' ? window : globalThis);
