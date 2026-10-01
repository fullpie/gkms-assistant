"""Explicit private actor registration, separate from immutable training claims.

The completed checkpoint remains a shadow training artifact. A separately
reviewed, hash-bound qualification receipt permits this fixed version in the
private host; neither importing this module nor training promotes a model.
"""
from copy import deepcopy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import threading

from ..application_paths import app_root, public_installation
from ..training_artifact_io import sha256_file
from .actor_handoff import POLICY_ID, OBJECTIVE_ID, validate_actor_metadata

SCHEMA = "gkms.private-shared-actor-registration.v1"
QUALIFICATION_SCHEMA = "gkms.private-shared-actor-qualification.v1"
PRIVATE_TRIAL = "user-authorized-private-trial"
RELATIVE_PATH = Path("var/devtools/private_rl_actor.json")
LABEL = "共用離線 RL（第4階段）"
_CACHE = {}
_LOCK = threading.RLock()


def _stamp(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _same_artifact(left, right):
    return (type(left) is dict and type(right) is dict and left.get('sha256') == right.get('sha256')
        and type(left.get('sha256')) is str and len(left['sha256']) == 64
        and ('bytes' not in left or 'bytes' not in right or left['bytes'] == right['bytes']))


def _count(value, *, positive=False):
    return type(value) is int and value >= (1 if positive else 0)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _private_trial(qualification):
    mode=qualification.get('acceptance_mode')
    if mode not in (None,'full-independent-evaluation',PRIVATE_TRIAL):
        raise ValueError('Unknown private actor acceptance mode')
    if mode!=PRIVATE_TRIAL:return False
    if (qualification.get('user_authorized')is not True
            or qualification.get('offline_evaluation_completed')is not False
            or qualification.get('independent_evaluation_status')!='pending'
            or qualification.get('public_activation')is not False
            or qualification.get('live_workflow_verified',False)is not False
            or qualification.get('score_improvement_verified',False)is not False
            or 'offline_evaluation'in qualification.get('evidence',{})):
        raise ValueError('Private trial requires explicit user authorization and truthful pending evaluation')
    return True


def _verify_trial_checkpoint(spec,training,read,expected_rows):
    """Load the real final actor; sampled validation is never all-row acceptance."""
    import torch
    from .native_actor_policy import load_actor_checkpoint
    read(spec['checkpoint'])
    model,loaded=load_actor_checkpoint(spec['checkpoint'],dataset_identity=spec['dataset_identity'],
        training_source_code=spec['training_source_code'],observed_source_code=spec.get('observed_source_code'))
    del model
    saved=torch.load(spec['checkpoint']['path'],map_location='cpu',weights_only=True)
    read(spec['checkpoint'])
    sampled=training.get('validation');indices=saved.get('validation_indices')
    if (training.get('step')!=4000 or training.get('il_updates')!=0 or training.get('iql_updates')!=4000
            or not _count(training.get('validation_step'))or training['validation_step']!=4000 or loaded.get('checkpoint_step')!=4000
            or saved.get('step')!=4000 or saved.get('phase')!='complete'
            or loaded.get('source_bindings')!=training.get('source_bindings')
            or not isinstance(indices,(list,tuple))or not indices or len(set(indices))!=len(indices)
            or any(type(i)is not int or not 0<=i<expected_rows for i in indices)
            or not isinstance(sampled,dict)or not _count(sampled.get('rows'),positive=True)or sampled['rows']!=len(indices)
            or not _count(sampled.get('available_rows'),positive=True)or sampled['available_rows']!=expected_rows
            or not _number(sampled.get('teacher_nll'))
            or sampled.get('used_for_fitting')is not False or sampled.get('used_for_checkpoint_selection')is not False
            or sampled.get('game_policy_quality_verified')is not False
            or loaded.get('model_metadata',{}).get('model_kind')!='gkms.rl.shared-observed-outer-policy.v2'):
        raise ValueError('Private trial needs actual completed4000 weights and their original sampled validation4000')
    if (saved.get('observed_outer')!=training.get('observed_outer')
            or saved.get('initial_loadout')!=training.get('initial_loadout')):
        raise ValueError('Private trial checkpoint and trained task identities differ')
    json.dumps(sampled,allow_nan=False)
    return loaded['model_metadata'],sampled


def _verify_acceptance_documents(spec, qualification, projection, read):
    """Parse producer reports; a checked file hash is not a completion claim."""
    evidence = qualification['evidence']
    training = read(evidence['training'], True)
    trial=_private_trial(qualification)
    evaluation = None if trial else read(evidence['offline_evaluation'], True)
    interfaces = read(evidence['decision_interfaces'], True)
    if not all(type(document) is dict for document in (training,interfaces)) or not trial and type(evaluation)is not dict:
        raise ValueError('Private RL acceptance reports must be producer report objects')
    il, iql, step = (training.get(key) for key in ('il_updates', 'iql_updates', 'step'))
    # The existing Trainer report predates a schema field. Its complete,
    # source-bound structural contract is checked instead of inventing one.
    if (training.get('status') != 'completed' or training.get('phase') != 'complete'
            or training.get('error') is not None or not _count(il) or not _count(iql, positive=True)
            or not _count(step, positive=True) or step != il + iql
            or training.get('total_steps') != step or training.get('last_saved_step') != step
            or training.get('unsaved_steps') != 0
            or any(not _count(training.get(key)) for key in ('total_steps', 'last_saved_step', 'unsaved_steps'))
            or not _same_artifact(training.get('checkpoint'), spec['checkpoint'])
            or training.get('dataset_identity') != spec['dataset_identity']
            or training.get('objective_id') != OBJECTIVE_ID
            or training.get('test_read') is not False or training.get('formal_model_replaced') is not False
            or training.get('activation_allowed') is not False or training.get('shadow_only') is not True):
        raise ValueError('Private RL training evidence is incomplete or has different counters/model/data/isolation')
    if not trial and (not _same_artifact(evaluation.get('training_report'), evidence['training'])
            or not _same_artifact(evaluation.get('dataset_manifest'), training.get('dataset_manifest'))):
        raise ValueError('Private RL row evaluation is not linked to this completed training report/dataset')
    dataset = read(training['dataset_manifest'], True)
    if type(dataset) is not dict or type(dataset.get('partition_rows')) is not dict:
        raise ValueError('Private RL qualified dataset manifest must contain partition counts')
    dataset_metadata = dataset
    dataset_identity = dataset.get('dataset_sha256')
    exclusion_view = dataset.get('schema') == 'gkms.rl.offline-source-exclusion-view.v1'
    if exclusion_view:
        # The completed Trainer/evaluator already qualified the exact subset.
        # Reuse its original corpus authority and semantic metadata here; do
        # not turn GUI discovery into another half-million-row index scan.
        from devtools.rl.offline_exclusion_view import validate_authorities
        dataset_metadata, _, source_watches = validate_authorities(dataset)
        for key in ('base_dataset', 'qualified_corpus', 'exclusion_receipt', 'implementation'):
            read(dataset[key])
        for watch in source_watches:
            watch.validate()
        dataset_identity = dataset.get('view_identity')
        original_parts = dataset_metadata.get('partition_rows', {})
        removed = dataset.get('excluded_rows')
        if (not _count(removed) or not _count(dataset.get('rows'), positive=True)
                or dataset['rows'] + removed != dataset_metadata.get('rows')
                or set(dataset['partition_rows']) - set(original_parts)
                or any(not _count(value) or value > original_parts.get(key, -1)
                       for key, value in dataset['partition_rows'].items())):
            raise ValueError('Private RL exclusion view changed its qualified original row accounting')
    partitions = dataset.get('partition_rows', {})
    expected_rows = partitions.get('validation')
    if (dataset.get('schema') != 'gkms.rl.offline-transition-dataset.v1' and not exclusion_view
            or dataset_identity != spec['dataset_identity']
            or not _count(partitions.get('train'), positive=True) or not _count(expected_rows, positive=True)
            or any(not _count(value) for value in partitions.values())
            or dataset.get('rows') != sum(partitions.values())
            or partitions.get('test', 0) != 0 or dataset.get('test_consumed') is not False):
        raise ValueError('Private RL evaluation denominator lacks the original qualified train/validation manifest')
    if trial:
        metadata,sampled=_verify_trial_checkpoint(spec,training,read,expected_rows)
    else:
        true_fields = ('evaluation_complete', 'checkpoint_metadata_verified', 'checkpoint_tensors_finite',
            'training_report_completed', 'artifacts_stable_after_evaluation', 'actor_weights_unchanged')
        false_fields = ('used_for_fitting', 'used_for_checkpoint_selection', 'test_read', 'optimizer_created',
            'formal_model_replaced', 'activation_allowed')
        if (evaluation.get('schema') != 'gkms.rl.offline-policy-row-evaluation.v1'
                or evaluation.get('status') != 'completed' or evaluation.get('error') is not None
                or evaluation.get('stop_reason') != 'all_validation_rows_evaluated'
                or any(evaluation.get(key) is not True for key in true_fields)
                or any(evaluation.get(key) is not False for key in false_fields)
                or evaluation.get('partition') != 'validation' or evaluation.get('training_rows_evaluated') != 0
                or evaluation.get('remaining') != 0 or evaluation.get('total') != expected_rows
                or evaluation.get('processed') != expected_rows
                or any(not _count(evaluation.get(key)) for key in
                    ('total', 'processed', 'remaining', 'training_rows_evaluated', 'checkpoint_step', 'il_updates', 'iql_updates', 'anchor_step'))
                or any(evaluation.get(key) != expected for key, expected in
                    (('checkpoint_step', step), ('il_updates', il), ('iql_updates', iql), ('anchor_step', il)))
                or not _same_artifact(evaluation.get('checkpoint'), spec['checkpoint'])
                or evaluation.get('dataset_identity') != spec['dataset_identity']
                or evaluation.get('source_code') != {name: item['sha256'] for name, item in spec['training_source_code'].items()}):
            raise ValueError('Private RL offline evidence is partial or has different model/data/counters/isolation')
        metadata = validate_actor_metadata(evaluation.get('model_metadata')).unpack()
    observed=metadata['model_kind']=='gkms.rl.shared-observed-outer-policy.v2'
    expected_schema=metadata.get('original_exam_schema_sha256',metadata['feature_schema_sha256'])
    if (not trial and evaluation.get('feature_schema_sha256') != expected_schema
            or dataset_metadata.get('feature_schema_sha256') != expected_schema
            or projection.get('feature_schema_sha256') != expected_schema):
        raise ValueError('Private RL training/evaluation/input parity feature schemas differ')
    if observed:
        authority=training.get('observed_outer')or{};refs=spec.get('observed_source_code')
        implementation=authority.get('implementation')
        if (not isinstance(refs,dict)or not isinstance(implementation,dict)or not implementation
                or {name:value.get('sha256')for name,value in refs.items()}!=implementation
                or training.get('observed_source_code')!=refs
                or not {'gkms_tool.rl.observed_outer_policy','gkms_tool.rl.observed_outer_projection'}<=set(refs)
                or authority.get('objective_id')!='recorded-produce-rating-v1'
                or authority.get('score_scale')!=10000. or authority.get('gamma')!=1.):
            raise ValueError('Private observed v2 actor requires its complete exact trained source pins/objective')
        qualification_ref=(authority.get('source_authority')or{}).get('qualification_report')
        outer_data=read(qualification_ref,True)
        if (not isinstance(outer_data,dict)or outer_data.get('schema')!='gkms.rl.observed-outer-index-qualification.v1'
                or outer_data.get('qualified')is not True or outer_data.get('dataset_identity')!=authority.get('dataset_identity')):
            raise ValueError('Private observed v2 actor lacks its original qualified outer data identity')
        expected_outer=sum(row.get('rows',0)for row in outer_data.get('coverage',[])if row.get('partition')=='validation')
        measured=(sampled if trial else evaluation).get('observed_outer')or{}
        denominator_ok=(_count(expected_outer,positive=True)and _count(measured.get('rows'),positive=True)
            and (measured['rows']<=expected_outer if trial else measured['rows']==expected_outer))
        if not denominator_ok:
            raise ValueError('Private observed validation denominator differs')
        if (not _count(expected_outer,positive=True)
                or measured.get('objective_id')!='recorded-produce-rating-v1'
                or measured.get('score_scale')!=10000. or measured.get('gamma')!=1.
                or not trial and measured.get('selection')!='all original observed validation rows'
                or measured.get('used_for_fitting')is not False or measured.get('used_for_checkpoint_selection')is not False
                or sum(row.get('rows',0)for row in measured.get('by_stratum',[]))!=measured['rows']):
            raise ValueError('Private observed v2 actor lacks complete separate outer validation')
    elif 'observed_source_code'in spec or training.get('observed_outer')is not None:
        raise ValueError('Original v1 actor cannot borrow observed v2 source pins')
    bindings = training.get('source_bindings')
    if (type(bindings) is not dict or not bindings or not trial and evaluation.get('source_bindings') != bindings
            or dataset_metadata.get('bindings') != bindings or projection.get('trained_source_binding') not in bindings.values()):
        raise ValueError('Private RL training/evaluation/input parity source bindings differ')
    if not trial:
        actors = evaluation.get('actors', {})
        if type(actors) is not dict or set(actors) != {'final_actor', 'frozen_anchor'} or any(type(actor) is not dict for actor in actors.values()):
            raise ValueError('Private RL evaluation must contain both final and frozen-anchor results')
        for actor in actors.values():
            parts = [actor.get(key) for key in ('actor_decisions', 'forced_empty_rows', 'other_no_actor_choice_rows')]
            if actor.get('rows') != expected_rows or any(not _count(value) for value in parts) or sum(parts) != expected_rows:
                raise ValueError('Private RL actor evaluation does not account for every validation row')
            if any(not _count(actor.get(key)) or actor[key] > actor['actor_decisions'] for key in
                   ('ordered_exact_count', 'first_action_match_count')):
                raise ValueError('Private RL evaluation action counts are inconsistent')
            count = actor['actor_decisions']
            if (not _count(count, positive=True) or not _count(actor.get('teacher_decoder_steps'))
                    or actor['teacher_decoder_steps'] < count
                    or not _number(actor.get('teacher_normalized_nll_sum')) or not _number(actor.get('teacher_nll'))
                    or not math.isclose(actor['teacher_nll'], actor['teacher_normalized_nll_sum'] / count, rel_tol=1e-10, abs_tol=1e-10)
                    or any(not _number(actor.get(rate)) or not math.isclose(actor[rate], actor[numerator] / count,
                        rel_tol=1e-10, abs_tol=1e-10) for rate, numerator in
                        (('ordered_exact_rate', 'ordered_exact_count'), ('first_action_match_rate', 'first_action_match_count')))):
                raise ValueError('Private RL evaluation metrics do not match the actual recorded decision counts')
    if (interfaces.get('schema') != 'gkms.private-actor-retained-input-audit.v1'
            or interfaces.get('constructor') != 'passed' or interfaces.get('decision_ready') is not True
            or interfaces.get('blockers') != [] or interfaces.get('raw_file_unchanged') is not True
            or interfaces.get('game_input_submitted') is not False or interfaces.get('live_workflow_verified') is not False
            or not _same_artifact(interfaces.get('checkpoint'), spec['checkpoint'])
            or not _same_artifact(interfaces.get('projection_equivalence'), spec['projection_equivalence'])
            or interfaces.get('training_projection_id') != projection['trained_source_binding']['projection_id']
            or interfaces.get('runtime_base_projection_id') != projection['runtime_base_projection_id']):
        raise ValueError('Private RL interface evidence is incomplete or belongs to another model/input projection')
    pool = interfaces.get('complete_drink_pool', {})
    if (type(pool) is not dict or type(pool.get('legal_kinds')) is not list
            or pool.get('ready') is not True or pool.get('blockers') != [] or pool.get('teacher_action_used') is not False
            or not {'play', 'drink', 'end_turn'} <= set(pool.get('legal_kinds', []))):
        raise ValueError('Private RL interface evidence lacks the actual complete card/drink/end-turn pool')
    for reference in (interfaces.get('source'), pool.get('source'), interfaces.get('candidate_adapter')):
        read(reference)
    tested_adapter = Path(interfaces['candidate_adapter']['path']).read_bytes()
    current_adapter = (Path(__file__).resolve().parent.parent / 'runtime_rl_exam_policy.py').read_bytes()
    if tested_adapter.replace(b'\r\n', b'\n') != current_adapter.replace(b'\r\n', b'\n'):
        raise ValueError('Private RL live adapter changed beyond file-location/newline relocation after interface acceptance')
    rollback = interfaces.get('bc_rollback', [])
    if (type(rollback) is not list or len(rollback) != 2 or any(type(item) is not dict for item in rollback)
            or {item.get('variant') for item in rollback} != {'baseline', 'integrated'}
            or any(item.get('loaded') is not True or item.get('ready') is not True or item.get('blockers') != [] for item in rollback)):
        raise ValueError('Private RL interface evidence has no successful BC rollback checks')
    return {'training_updates': step, 'il_updates': il, 'iql_updates': iql,
        'validation_rows':sampled['rows']if trial else expected_rows,
        'validation_scope':'sampled-training-validation'if trial else'all-original-validation-rows',
        'full_validation_expected_rows':expected_rows,
        'input_parity_rows':len(projection['rows']),'offline_report_schema':None if trial else evaluation['schema'],
        'interface_report_schema':interfaces['schema']}


def load_private_actor_descriptor(*, project_root=None, refresh=False):
    """No game IO; cached snapshots stat pinned assets only.

    Optional initial-composition capability verifies its saved task state on
    the first load; ordinary registrations retain their existing light reader.

    Absent/private-disabled registration returns None. A configured corrupt
    registration raises a concrete error; callers may display an unavailable
    entry but must not substitute another policy under the same identifier.
    """
    root = Path(project_root) if project_root is not None else app_root()
    if public_installation(root):
        return None
    path = (root / RELATIVE_PATH).resolve()
    if not path.exists():
        return None
    with _LOCK:
        cached = _CACHE.get(str(path))
        if cached and not refresh:
            try:
                if all(_stamp(p) == stamp for p, stamp in cached[0]):
                    return deepcopy(cached[1])
            except OSError:
                pass
        watches = []

        def read(reference, document=False):
            if (type(reference) is not dict or not {"path", "sha256"} <= set(reference)
                    or set(reference) - {"path", "sha256", "bytes"}):
                raise ValueError("Private RL requires a pinned file reference")
            target = Path(reference["path"]).resolve()
            stamp = _stamp(target)
            if (sha256_file(target) != reference["sha256"]
                    or "bytes" in reference and stamp[0] != reference["bytes"]):
                raise ValueError("Private RL asset changed: " + target.name)
            value = json.loads(target.read_bytes()) if document else None
            if _stamp(target) != stamp:
                raise ValueError("Private RL asset changed while loading: " + target.name)
            watches.append((target, stamp))
            return value

        payload = path.read_bytes()
        registration_ref = {"path": str(path), "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}
        registration = read(registration_ref, True)
        if (type(registration) is not dict or registration.get("schema") != SCHEMA
                or registration.get("variant_id") != POLICY_ID or registration.get("private_only") is not True
                or type(registration.get("enabled")) is not bool):
            raise ValueError("Explicit private shared RL registration required")
        if registration["enabled"] is False:
            return None
        spec = registration.get("specification", {})
        required = {"checkpoint", "dataset_identity", "semantic_sources", "training_source_code", "device",
                    "runtime_master_source", "qualification", "projection_equivalence"}
        if (type(spec) is not dict or not required <= set(spec)
                or set(spec) - required - {"runtime_loader_compatibility", "runtime_io_equivalence", "observed_source_code"} or spec["device"] != "cpu"):
            raise ValueError("Private RL requires the fixed CPU inference specification")
        if set(spec["semantic_sources"]) != {"contract_set", "original_shared_encoder", "loader_compatibility"}:
            raise ValueError("Private RL semantic source scope differs")
        if set(spec["training_source_code"]) != {"offline_dataset.py", "offline_policy.py", "features.py", "networks.py", "train_offline_policy.py"}:
            raise ValueError("Private RL training implementation scope differs")
        for reference in [spec["checkpoint"], spec["runtime_master_source"],
                          *spec["semantic_sources"].values(), *spec["training_source_code"].values()]:
            read(reference)
        if 'observed_source_code'in spec:
            if not isinstance(spec['observed_source_code'],dict)or not spec['observed_source_code']:
                raise ValueError('Observed v2 actor source references are incomplete')
            for reference in spec['observed_source_code'].values():read(reference)
        if "runtime_loader_compatibility" in spec:
            read(spec["runtime_loader_compatibility"])
        if "runtime_io_equivalence" in spec:
            read(spec["runtime_io_equivalence"])
        read(spec["projection_equivalence"])
        from .actor_projection_equivalence import read_projection_equivalence
        projection, parity_watches = read_projection_equivalence(spec['projection_equivalence'])
        watches.extend(parity_watches)
        if (type(projection) is not dict or projection.get("schema") != "gkms.rl.actor-input-equivalence.v1" or projection.get("status") != "passed"
                or projection.get("checkpoint_sha256") != spec["checkpoint"]["sha256"]
                or projection.get("dataset_identity") != spec["dataset_identity"] or projection.get("failures") != []):
            raise ValueError("Private RL source-specific runtime input parity is incomplete or for another model")
        qualification = read(spec["qualification"], True)
        trial=_private_trial(qualification)if isinstance(qualification,dict)else False
        if (type(qualification) is not dict or qualification.get("schema") != QUALIFICATION_SCHEMA
                or qualification.get("checkpoint_sha256") != spec["checkpoint"]["sha256"]
                or qualification.get("dataset_identity") != spec["dataset_identity"]
                or qualification.get("private_live_authorized") is not True
                or qualification.get("trained") is not True
                or qualification.get("offline_evaluation_completed") is not (False if trial else True)
                or qualification.get("decision_interfaces_verified") is not True
                or qualification.get("fixed_weights") is not True
                or qualification.get("public_activation") is not False
                or qualification.get("live_workflow_verified", False) is not False
                or qualification.get("score_improvement_verified", False) is not False):
            raise ValueError("Private RL offline/interface qualification is incomplete or for another model")
        scope = qualification.get("scope", {})
        from .game_projection import default_routes
        routes = default_routes()
        if (type(scope) is not dict or scope.get("modes") != [row[-1] for row in routes["modes"]]
                or scope.get("flows") != [row[-1] for row in routes["flows"]]
                or scope.get("stages") != [row[-1] for row in routes["stages"]]
                or qualification.get("full_coverage") is not False
                or not isinstance(qualification.get("coverage_gaps"), list)):
            raise ValueError("Private RL requires its explicit six-flow NIA scope and coverage limitations")
        evidence = qualification.get("evidence", {})
        required_evidence={'training','decision_interfaces','initial_interfaces'}if trial else{'training','offline_evaluation','decision_interfaces'}
        if type(evidence)is not dict or not required_evidence<=set(evidence):
            raise ValueError("Private RL qualification lacks concrete evidence references")
        for reference in evidence.values():
            read(reference)
        actual_acceptance = _verify_acceptance_documents(spec, qualification, projection, read)
        initial_loadout = None
        if registration.get('initial_loadout') is not None:
            from ..private_initial_loadout import verify_capability
            initial_loadout = verify_capability(registration['initial_loadout'], spec, qualification, read)
        if trial and initial_loadout is None:
            raise ValueError('This private shared trial requires its directly trained initial-loadout capability')
        if registration.get("loadout_reference") is not None:
            read(registration["loadout_reference"])
        if any(_stamp(p) != stamp for p, stamp in watches):
            raise ValueError("Private RL assets changed across qualification")
        inference_available = importlib.util.find_spec("torch") is not None
        dependency_reason = None if inference_available else "私人 RL 推論需要 PyTorch；目前 GUI Python 尚未提供此依賴。"
        label="共用離線 RL（私人試用；完整評估中）"if trial else LABEL
        descriptor = {"id": POLICY_ID, "label": label, "model_label": label,
            "model_sha256": spec["checkpoint"]["sha256"], "model": deepcopy(spec["checkpoint"]),
            "available": inference_available, "artifact_available": True, "live_ready": inference_available,
            "reason": dependency_reason, "diagnostic": dependency_reason, "start_block_reason": dependency_reason,
            "inference_dependency_available": inference_available, "can_request_preflight": inference_available,
            "private_only": True, "research_fallback": False, "fixed_weights": True,
            "private_trial":trial,
            "unsupported_route_behavior": "stop-with-reason", "specification": deepcopy(spec),
            "descriptor_reference": registration_ref, "qualification": qualification,
            "evidence_summary": {"trained": True, "offline_evaluation_completed": not trial,
                "private_trial":trial,"independent_evaluation_status":"pending"if trial else"completed",
                **actual_acceptance,
                "decision_interfaces_verified": True, "full_coverage": False,
                "coverage_gaps": deepcopy(qualification["coverage_gaps"]),
                "live_workflow_verified": qualification.get("live_workflow_verified") is True,
                "score_improvement_verified": qualification.get("score_improvement_verified") is True}}
        if registration.get("loadout_reference") is not None:
            descriptor["loadout_reference"] = deepcopy(registration["loadout_reference"])
        if initial_loadout is not None:
            descriptor['initial_loadout'] = initial_loadout
        if registration.get('runtime_reference_trial') is not None:
            from ..runtime_live_exam_input import validate_runtime_reference_trial
            runtime = read(spec['runtime_master_source'], True)
            descriptor['runtime_reference_trial'] = validate_runtime_reference_trial(
                registration['runtime_reference_trial'], checkpoint_sha256=spec['checkpoint']['sha256'],
                runtime_source_sha256=spec['runtime_master_source']['sha256'], reference_master_hash=runtime['master_hash'])
        _CACHE[str(path)] = (tuple(watches), descriptor)
        return deepcopy(descriptor)


__all__ = ["load_private_actor_descriptor", "SCHEMA", "QUALIFICATION_SCHEMA", "RELATIVE_PATH", "LABEL"]
