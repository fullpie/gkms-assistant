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


def _verify_acceptance_documents(spec, qualification, projection, read):
    """Parse producer reports; a checked file hash is not a completion claim."""
    evidence = qualification['evidence']
    training = read(evidence['training'], True)
    evaluation = read(evidence['offline_evaluation'], True)
    interfaces = read(evidence['decision_interfaces'], True)
    if not all(type(document) is dict for document in (training, evaluation, interfaces)):
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
    if (not _same_artifact(evaluation.get('training_report'), evidence['training'])
            or not _same_artifact(evaluation.get('dataset_manifest'), training.get('dataset_manifest'))):
        raise ValueError('Private RL row evaluation is not linked to this completed training report/dataset')
    dataset = read(training['dataset_manifest'], True)
    if type(dataset) is not dict or type(dataset.get('partition_rows')) is not dict:
        raise ValueError('Private RL qualified dataset manifest must contain partition counts')
    partitions = dataset.get('partition_rows', {})
    expected_rows = partitions.get('validation')
    if (dataset.get('schema') != 'gkms.rl.offline-transition-dataset.v1'
            or dataset.get('dataset_sha256') != spec['dataset_identity']
            or not _count(partitions.get('train'), positive=True) or not _count(expected_rows, positive=True)
            or any(not _count(value) for value in partitions.values())
            or dataset.get('rows') != sum(partitions.values())
            or partitions.get('test', 0) != 0 or dataset.get('test_consumed') is not False):
        raise ValueError('Private RL evaluation denominator lacks the original qualified train/validation manifest')
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
    if (evaluation.get('feature_schema_sha256') != metadata['feature_schema_sha256']
            or dataset.get('feature_schema_sha256') != metadata['feature_schema_sha256']
            or projection.get('feature_schema_sha256') != metadata['feature_schema_sha256']):
        raise ValueError('Private RL training/evaluation/input parity feature schemas differ')
    bindings = training.get('source_bindings')
    if (type(bindings) is not dict or not bindings or evaluation.get('source_bindings') != bindings
            or dataset.get('bindings') != bindings or projection.get('trained_source_binding') not in bindings.values()):
        raise ValueError('Private RL training/evaluation/input parity source bindings differ')
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
    return {'training_updates': step, 'il_updates': il, 'iql_updates': iql, 'validation_rows': expected_rows,
        'input_parity_rows': len(projection['rows']), 'offline_report_schema': evaluation['schema'],
        'interface_report_schema': interfaces['schema']}


def load_private_actor_descriptor(*, project_root=None, refresh=False):
    """No Torch import and no game IO; cached snapshots stat pinned assets only.

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
                or set(spec) - required - {"runtime_loader_compatibility", "runtime_io_equivalence"} or spec["device"] != "cpu"):
            raise ValueError("Private RL requires the fixed CPU inference specification")
        if set(spec["semantic_sources"]) != {"contract_set", "original_shared_encoder", "loader_compatibility"}:
            raise ValueError("Private RL semantic source scope differs")
        if set(spec["training_source_code"]) != {"offline_dataset.py", "offline_policy.py", "features.py", "networks.py", "train_offline_policy.py"}:
            raise ValueError("Private RL training implementation scope differs")
        for reference in [spec["checkpoint"], spec["runtime_master_source"],
                          *spec["semantic_sources"].values(), *spec["training_source_code"].values()]:
            read(reference)
        if "runtime_loader_compatibility" in spec:
            read(spec["runtime_loader_compatibility"])
        if "runtime_io_equivalence" in spec:
            read(spec["runtime_io_equivalence"])
        projection = read(spec["projection_equivalence"], True)
        if (type(projection) is not dict or projection.get("schema") != "gkms.rl.actor-input-equivalence.v1" or projection.get("status") != "passed"
                or projection.get("checkpoint_sha256") != spec["checkpoint"]["sha256"]
                or projection.get("dataset_identity") != spec["dataset_identity"] or projection.get("failures") != []):
            raise ValueError("Private RL source-specific runtime input parity is incomplete or for another model")
        qualification = read(spec["qualification"], True)
        if (type(qualification) is not dict or qualification.get("schema") != QUALIFICATION_SCHEMA
                or qualification.get("checkpoint_sha256") != spec["checkpoint"]["sha256"]
                or qualification.get("dataset_identity") != spec["dataset_identity"]
                or qualification.get("private_live_authorized") is not True
                or qualification.get("trained") is not True
                or qualification.get("offline_evaluation_completed") is not True
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
        if type(evidence) is not dict or not {"training", "offline_evaluation", "decision_interfaces"} <= set(evidence):
            raise ValueError("Private RL qualification lacks concrete evidence references")
        for reference in evidence.values():
            read(reference)
        from .actor_projection_equivalence import read_projection_equivalence
        projection, parity_watches = read_projection_equivalence(spec['projection_equivalence'])
        watches.extend(parity_watches)
        actual_acceptance = _verify_acceptance_documents(spec, qualification, projection, read)
        if registration.get("loadout_reference") is not None:
            read(registration["loadout_reference"])
        if any(_stamp(p) != stamp for p, stamp in watches):
            raise ValueError("Private RL assets changed across qualification")
        inference_available = importlib.util.find_spec("torch") is not None
        dependency_reason = None if inference_available else "私人 RL 推論需要 PyTorch；目前 GUI Python 尚未提供此依賴。"
        descriptor = {"id": POLICY_ID, "label": LABEL, "model_label": LABEL,
            "model_sha256": spec["checkpoint"]["sha256"], "model": deepcopy(spec["checkpoint"]),
            "available": inference_available, "artifact_available": True, "live_ready": inference_available,
            "reason": dependency_reason, "diagnostic": dependency_reason, "start_block_reason": dependency_reason,
            "inference_dependency_available": inference_available, "can_request_preflight": inference_available,
            "private_only": True, "research_fallback": False, "fixed_weights": True,
            "unsupported_route_behavior": "stop-with-reason", "specification": deepcopy(spec),
            "descriptor_reference": registration_ref, "qualification": qualification,
            "evidence_summary": {"trained": True, "offline_evaluation_completed": True,
                **actual_acceptance,
                "decision_interfaces_verified": True, "full_coverage": False,
                "coverage_gaps": deepcopy(qualification["coverage_gaps"]),
                "live_workflow_verified": qualification.get("live_workflow_verified") is True,
                "score_improvement_verified": qualification.get("score_improvement_verified") is True}}
        if registration.get("loadout_reference") is not None:
            descriptor["loadout_reference"] = deepcopy(registration["loadout_reference"])
        _CACHE[str(path)] = (tuple(watches), descriptor)
        return deepcopy(descriptor)


__all__ = ["load_private_actor_descriptor", "SCHEMA", "QUALIFICATION_SCHEMA", "RELATIVE_PATH", "LABEL"]
