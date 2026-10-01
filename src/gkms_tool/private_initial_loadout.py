"""Private initial-composition inference over one qualified shared actor.

No native runner, game command, optimizer, registration write or fallback.
The existing owner receives an ordinary LoadoutSelection only after its
account/rental/lock contract passes.
"""
from collections import Counter, OrderedDict
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.util
import json
import math
import re
from pathlib import Path
import threading
import time

from .qualification_verification import source_stamp
from .rl.contracts import canonical, digest
from .rl_loadout_advisor import LoadoutRecommendationUnavailable, LoadoutRecommendationCancelled

SCHEMA = 'gkms.private-initial-loadout-capability.v2'
METHOD = 'shared-initial-composition-return-RL'
RETURN_OBJECTIVE = 'initial-loadout-recorded-final-return-v1'
RETURN_SOURCE = 'qualified-same-trajectory-final-produce-rating'
PRIVATE_TRIAL = 'user-authorized-private-trial'
INTERFACE_SCHEMA = 'gkms.private-initial-loadout-interface.v1'
EXECUTION_MODULES = ('gkms_tool.private_initial_loadout', 'gkms_tool.rl.initial_loadout_policy',
    'devtools.rl.initial_loadout_batch')
_CACHE = OrderedDict()
_LOCK = threading.RLock()


def _fail(message):
    raise LoadoutRecommendationUnavailable('INITIAL_LOADOUT_NOT_QUALIFIED: ' + message)


def _same(left, right):
    return (isinstance(left, dict) and isinstance(right, dict) and isinstance(left.get('sha256'), str)
        and len(left['sha256']) == 64 and all(left.get(k) == right.get(k) for k in ('sha256', 'bytes')))


def _verify_trial_interface(capability,specification,qualification,read):
    reference=qualification.get('evidence',{}).get('initial_interfaces')
    if not isinstance(reference,dict):_fail('私人試用缺少新版模型的實際編成接口驗收。')
    report=read(reference,True)
    checks=('owned_inventory_only','locks_preserved','excluded_absent','rental_supported',
        'model_choices_inside_masks','invalid_lock_rejected','expired_rental_rejected','weights_unchanged')
    if (not isinstance(report,dict)or report.get('schema')!=INTERFACE_SCHEMA or report.get('status')!='passed'
            or not _same(report.get('checkpoint'),specification['checkpoint'])
            or not _same(report.get('training_report'),qualification['evidence']['training'])
            or report.get('dataset_identity')!=specification['dataset_identity']
            or not _same(report.get('runtime_master_source'),specification['runtime_master_source'])
            or report.get('execution_sources')!=capability['execution_sources']
            or report.get('model_kind')!='gkms.rl.shared-observed-outer-policy.v2' or report.get('method')!=METHOD
            or report.get('actor_steps')!=10
            or report.get('selected_counts')!={'owned_supports':5,'rental_supports':1,'memories':4}
            or any((report.get('checks')or{}).get(key)is not True for key in checks)
            or any(report.get(key)is not False for key in ('game_io','native_execution','optimizer_created'))):
        _fail('私人試用編成接口的模型、來源、合法遮罩或鎖定驗收不完整。')
    decisions=report.get('decisions')
    if (not isinstance(decisions,list)or len(decisions)!=10
            or [item.get('family')for item in decisions]!=['support']*6+['memory']*4
            or any(not isinstance(item.get('selection_key'),str)or not item['selection_key']
                or type(item.get('log_probability'))not in(int,float)
                or not math.isfinite(item['log_probability'])or item['log_probability']>0 for item in decisions)
            or len({item['selection_key']for item in decisions})!=10
            or any(sum(item['selection_key'].startswith(prefix)for item in decisions)!=count
                for prefix,count in (('owned:',5),('rental:',1),('memory:',4)))):
        _fail('私人試用需要新版 actor 十次真實且合法的編成選擇。')
    for name in ('inventory_snapshot','loadout_snapshot'):
        reference=report.get(name)
        if not isinstance(reference,dict):_fail('編成接口缺少實際持有／借卡快照來源。')
        read(reference)
    return report


def verify_capability(reference, specification, qualification, read):
    """Read producer evidence, not a flag claiming an untrained v2 can rank."""
    import torch
    from .rl.initial_loadout_policy import HISTORICAL_SEMANTICS
    capability = read(reference, True)
    if (not isinstance(capability, dict) or capability.get('schema') != SCHEMA
            or not _same(capability.get('checkpoint'), specification['checkpoint'])
            or capability.get('dataset_identity') != specification['dataset_identity']
            or capability.get('semantic_mode') != 'rich-static'):
        _fail('編成能力的模型、資料或完整語義來源不符。')
    trial=qualification.get('acceptance_mode')==PRIVATE_TRIAL
    if (qualification.get('acceptance_mode')not in(None,PRIVATE_TRIAL)
            or capability.get('acceptance_mode')!=(PRIVATE_TRIAL if trial else None)):
        _fail('編成能力與明示驗收範圍不符。')
    if trial and (qualification.get('user_authorized')is not True
            or qualification.get('offline_evaluation_completed')is not False
            or qualification.get('independent_evaluation_status')!='pending'
            or qualification.get('public_activation')is not False):
        _fail('私人試用必須明示使用者授權、公開禁用與完整評估待完成。')
    training = read(qualification['evidence']['training'], True)
    evaluation = None if trial else read(qualification['evidence']['offline_evaluation'], True)
    task = training.get('initial_loadout'); progress = training.get('initial_loadout_training') or {}
    if (not isinstance(task, dict) or task.get('schema') != 'gkms.initial-loadout-training.v2'
            or task.get('objective_id') != RETURN_OBJECTIVE
            or task.get('config', {}).get('objective') != RETURN_OBJECTIVE
            or training.get('status') != 'completed' or training.get('phase') != 'complete' or training.get('error') is not None
            or task.get('candidate_authority') != HISTORICAL_SEMANTICS
            or task.get('reward_or_critic_training') is not True
            or type(progress.get('updates')) is not int or progress['updates'] <= 0
            or progress['updates'] != training.get('step')
            or type(progress.get('critic_updates')) is not int or progress['critic_updates'] != progress['updates']
            or training.get('iql_updates') != progress['updates'] or training.get('il_updates') != 0
            or not _same(training.get('checkpoint'), specification['checkpoint'])):
        _fail('目前權重未完成同場培育回報的編成 RL 訓練；純模仿或未訓練版本不能啟用。')
    if not trial and (evaluation.get('status')!='completed' or evaluation.get('evaluation_complete')is not True
            or evaluation.get('error')is not None or evaluation.get('actor_weights_unchanged')is not True
            or evaluation.get('artifacts_stable_after_evaluation')is not True
            or evaluation.get('optimizer_created')is not False or evaluation.get('test_read')is not False
            or evaluation.get('used_for_fitting')is not False
            or evaluation.get('model_metadata',{}).get('model_kind')!='gkms.rl.shared-observed-outer-policy.v2'
            or not _same(evaluation.get('checkpoint'),specification['checkpoint'])
            or not _same(evaluation.get('training_report'),qualification['evidence']['training'])):
        _fail('完整編成驗收需要同一固定模型的獨立評估。')
    if trial and (training.get('step')!=4000 or training.get('total_steps')!=4000
            or training.get('last_saved_step')!=4000 or training.get('unsaved_steps')!=0
            or training.get('validation_step')!=4000):
        _fail('這次私人試用需要已保存4000步及同權重的實際抽樣驗證。')
    return_binding = task.get('return_binding') or {}
    if (return_binding.get('schema') != 'gkms.initial-loadout-return-binding.v1'
            or return_binding.get('objective_id') != RETURN_OBJECTIVE
            or return_binding.get('value_objective_id') != 'recorded-produce-rating-v1'
            or return_binding.get('return_source') != RETURN_SOURCE
            or return_binding.get('score_scale') != 10000.
            or return_binding.get('dataset_identity') != task.get('dataset_identity')
            or not _same(return_binding.get('dataset_manifest'), task.get('dataset_manifest'))
            or any(return_binding.get(key) is not False for key in
                ('outcome_in_features', 'game_terminal_or_next_state_claimed',
                 'counterfactual_return_claimed', 'historical_owned_pool_claimed'))):
        _fail('編成 RL 缺少同場最終培育評分的固定來源與單位。')
    checkpoint = read(specification['checkpoint'])
    saved = torch.load(specification['checkpoint']['path'], map_location='cpu', weights_only=True)
    if (saved.get('phase') != 'complete' or saved.get('step') != progress['updates']
            or canonical(saved.get('initial_loadout')) != canonical(task)
            or (saved.get('initial_loadout_state') or {}).get('identity') != task
            or saved['initial_loadout_state'].get('updates') != progress['updates']
            or saved['initial_loadout_state'].get('critic_updates') != progress['critic_updates']
            or saved.get('iql_updates') != progress['updates'] or saved.get('il_updates') != 0
            or saved.get('config', {}).get('il_steps') != 0
            or saved.get('config', {}).get('iql_steps') != progress['updates']
            or saved.get('dataset_identity') != specification['dataset_identity']):
        _fail('編成任務與實際 checkpoint 不一致。')
    del saved, checkpoint
    sources = training.get('initial_loadout_source_code') or {}; expected = task.get('implementation') or {}
    if not expected or set(sources) != set(expected) or not trial and evaluation.get('initial_loadout_source_code') != sources:
        _fail('缺少完整的編成訓練與獨立評估程式來源。')
    for name, ref in sources.items():
        if ref.get('sha256') != expected[name]: _fail('編成程式來源雜湊不符。')
        read(ref)
    required = {'devtools.rl.initial_loadout_returns', 'gkms_tool.rl.initial_loadout_rl',
        'devtools.rl.initial_loadout_training', 'devtools.rl.initial_loadout_dataset',
        'gkms_tool.rl.initial_loadout_policy', 'devtools.rl.initial_loadout_batch'}
    bound_readers = return_binding.get('implementation') or {}
    if (not required <= set(expected) or set(bound_readers) != {'devtools.rl.initial_loadout_returns'}
            or not _same(bound_readers['devtools.rl.initial_loadout_returns'], sources['devtools.rl.initial_loadout_returns'])):
        _fail('編成 RL 回報讀取器或 critic 訓練來源雜湊不符。')
    for name in ('gkms_tool.rl.initial_loadout_policy', 'devtools.rl.initial_loadout_batch'):
        module = importlib.util.find_spec(name)
        if name not in expected or module is None or hashlib.sha256(Path(module.origin).read_bytes()).hexdigest() != expected[name]:
            _fail('目前編成特徵／batch 程式與訓練版本不符。')
    execution = capability.get('execution_sources')
    if not isinstance(execution, dict) or set(execution) != set(EXECUTION_MODULES):
        _fail('需要固定的私人編成推論程式來源。')
    for name, ref in execution.items():
        read(ref); module = importlib.util.find_spec(name)
        if module is None or hashlib.sha256(Path(module.origin).read_bytes()).hexdigest() != ref['sha256']:
            _fail('私人編成推論程式已變更。')
    manifest = read(task['dataset_manifest'], True)
    partitions = manifest.get('partition_counts') or {}; total = partitions.get('validation')
    full_total=total
    validation=[row['metadata']for row in manifest.get('sources',[])if row['metadata']['partition']=='validation']
    if trial:
        indices=task.get('validation_indices')
        if (not isinstance(indices,list)or not indices
                or any(type(index)is not int or not 0<=index<len(validation)for index in indices)
                or len(indices)!=len(set(indices))
                or type(full_total)is not int or len(validation)!=full_total):
            _fail('私人試用抽樣驗證必須保留原始驗證索引與完整分母。')
        validation=[validation[index]for index in indices];total=len(indices)
        measured=(training.get('validation')or{}).get('initial_loadout')or{}
    else:measured=evaluation.get('initial_loadout')or{}
    if (manifest.get('schema') != 'gkms.initial-loadout-dataset.v1'
            or manifest.get('identity') != task.get('dataset_identity')
            or type(total) is not int or total <= 0 or type(partitions.get('train')) is not int
            or partitions['train'] <= 0 or partitions.get('test', 0) != 0
            or not trial and (not _same(evaluation.get('initial_loadout_manifest'), task['dataset_manifest'])
                or evaluation.get('initial_loadout_dataset_identity') != task['dataset_identity']
                or measured.get('status') != 'completed' or measured.get('evaluation_complete') is not True
                or measured.get('processed') != total or measured.get('remaining') != 0)
            or measured.get('demonstrations') != total
            or measured.get('selection') != ('fixed seed original validation groups'if trial else 'all original validation demonstrations')
            or measured.get('total_demonstrated_choices') != 10 * total
            or measured.get('objective_id') != RETURN_OBJECTIVE
            or measured.get('return_histories') != total or measured.get('critic_rows') != 10 * total
            or measured.get('supervised_choices') != 10 * total
            or measured.get('return_source') != RETURN_SOURCE
            or measured.get('return_binding_identity') != digest(return_binding)
            or measured.get('value_objective_id') != 'recorded-produce-rating-v1'
            or measured.get('score_scale') != 10000.
            or measured.get('target_kind') != 'observed-full-cultivation-return'
            or measured.get('error_units') != 'raw-produce-rating-points'
            or measured.get('full_template_coverage') is not True
            or measured.get('fully_covered_demonstrations') != total or measured.get('vocabulary_uncovered') != []
            or measured.get('validation_candidate_authority') !=
                'qualified-own-VAL-teacher-and-prefix; TRAIN-only-sampled-background; ownership-unobserved'
            or measured.get('teacher_nll_is_rl_quality') is not False
            or measured.get('bootstrap_used') is not False or measured.get('game_terminal_created') is not False
            or measured.get('validation_templates_added_to_train') is not False
            or measured.get('used_for_fitting') is not False or measured.get('test_read') is not False
            or not trial and measured.get('used_for_checkpoint_selection') is not False):
        _fail('缺少完整原始 validation 的編成評估與 OOV 分母。')
    coverage = measured.get('family_coverage') or {}
    for family, size in (('support', 6), ('memory', 4)):
        row = coverage.get(family) or {}
        fields = ('total_targets', 'known_vocabulary_targets', 'unseen_train_vocabulary_targets',
            'missing_targets', 'scored_choices', 'known_but_unscored_targets')
        if (any(type(row.get(k)) is not int or row[k] < 0 for k in fields)
                or row['total_targets'] != total * size
                or row['known_vocabulary_targets'] + row['unseen_train_vocabulary_targets'] + row['missing_targets'] != row['total_targets']
                or row['known_but_unscored_targets'] + row['scored_choices'] != row['known_vocabulary_targets'] + row['unseen_train_vocabulary_targets']
                or row['missing_targets'] != 0 or row['scored_choices'] != row['total_targets']):
            _fail('編成 validation 覆蓋統計不完整。')
    if measured.get('supervised_choices') != sum(coverage[x]['scored_choices'] for x in ('support', 'memory')):
        _fail('編成評分分母不一致。')
    for metric in ('q_mae', 'q_rmse', 'v_mae', 'v_rmse'):
        value = measured.get(metric)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            _fail('缺少有限且單位明確的編成回報／critic 獨立評估。')
    expected_trajectories = {row.get('trajectory_id') for row in validation}
    labels = measured.get('return_sources')
    if (len(validation) != total or len(expected_trajectories) != total or None in expected_trajectories
            or not isinstance(labels, list) or len(labels) != total
            or {row.get('trajectory_id') for row in labels} != expected_trajectories):
        _fail('編成 validation 的同場回報來源分母或原始分組不一致。')
    for label in labels:
        locator = label.get('week1_row') or {}; certificate = label.get('observed_source_certificate') or {}
        if (not re.fullmatch('[0-9a-f]{64}', str(label.get('label_identity', '')))
                or not re.fullmatch(r'/response/histories/\d+/produceHistory/score', str(label.get('score_source_path', '')))
                or not Path(str(locator.get('path', ''))).is_absolute()
                or type(locator.get('offset')) is not int or locator['offset'] < 0
                or type(locator.get('length')) is not int or locator['length'] <= 0
                or not re.fullmatch('[0-9a-f]{64}', str(locator.get('sha256', '')))
                or not _same(certificate, certificate) or not Path(str(certificate.get('path', ''))).is_absolute()
                or type(certificate.get('bytes')) is not int or certificate['bytes'] <= 0):
            _fail('編成 validation 回報缺少固定來源位置與雜湊。')
        read(certificate)
    nll = measured.get('teacher_nll')
    if (measured['supervised_choices'] and (type(nll) not in (int, float) or not math.isfinite(nll) or nll < 0)
            or not measured['supervised_choices'] and nll is not None):
        _fail('編成 teacher NLL 與實際可評分分母不符。')
    material = capability.get('material_specification') or {}
    if material.get('schema') != 'gkms.observed-initial-loadout-source.v1' or not material.get('runtime_master_source'):
        _fail('需要目前 Master 的完整來源綁定編成語義。')
    historical = read(material['historical_master_manifest'], True)
    runtime = read(material['runtime_master_source'], True)
    if (historical.get('archived_master_hash') != capability.get('master_hash')
            or runtime.get('master_hash') != capability.get('master_hash')
            or not _same(material['runtime_master_source'], specification.get('runtime_master_source'))):
        _fail('初始編成與現用模型的 Master 來源不一致。')
    for key in ('source_relocations',):
        if material.get(key): read(material[key])
    train_metadata = [row['metadata'] for row in manifest.get('sources', [])
        if row['metadata']['partition'] == 'train' and row['metadata'].get('semantic_mode') == capability['semantic_mode']]
    cells = sorted({(row['flow_id'], row['mode_id']) for row in train_metadata})
    if not cells: _fail('完整語義域沒有原始 TRAIN 編成示範。')
    # Exact current material is verified above and by the ordinary runtime
    # source/projection proof. A newer Master need not already have historical
    # demonstrations; additions do not invalidate old source-qualified facts.
    masters = Counter(row['source_master_hash'] for row in train_metadata)
    interface=_verify_trial_interface(capability,specification,qualification,read)if trial else None
    return {'reference': deepcopy(reference), 'checkpoint': deepcopy(specification['checkpoint']),
        'training_identity': deepcopy(task), 'source_code': deepcopy(sources), 'master_hash': capability['master_hash'],
        'execution_master_version': runtime.get('execution_master_version', capability['master_hash']),
        'semantic_mode': capability['semantic_mode'], 'material_specification': deepcopy(material), 'scope_cells': cells,
        'training_master_demonstrations': dict(sorted(masters.items())),
        'current_master_training_demonstrations': masters[capability['master_hash']],
        'current_master_seen_in_training': masters[capability['master_hash']] > 0,
        'runtime_compatibility_references': {name: deepcopy(specification[name]) for name in
            ('runtime_master_source', 'runtime_io_equivalence', 'projection_equivalence') if name in specification},
        'new_definition_quality_verified': False,
        'validation_coverage': deepcopy(measured), 'trained_updates': progress['updates'],
        'trained_critic_updates': progress['critic_updates'], 'return_binding': deepcopy(return_binding),
        'method': METHOD, 'score_improvement_verified': False,
        **({'acceptance_mode':PRIVATE_TRIAL,'validation_scope':'training-sampled',
            'sampled_validation_count':total,'full_validation_count':full_total,
            'independent_evaluation_status':'pending','offline_evaluation_completed':False,
            'interface_evidence':deepcopy(qualification['evidence']['initial_interfaces'])}if trial else{})}


def require_scope(descriptor, *, idol_card_id, produce_id, profile=None):
    capability = descriptor.get('initial_loadout') if isinstance(descriptor, dict) else None
    if (not isinstance(capability, dict) or capability.get('trained_updates', 0) <= 0
            or capability.get('trained_critic_updates') != capability.get('trained_updates') or capability.get('method') != METHOD):
        _fail('目前固定模型沒有已驗收的初始編成任務。')
    if not _same(capability.get('checkpoint'), descriptor.get('specification', {}).get('checkpoint')):
        _fail('編成能力屬於另一個固定模型。')
    if profile is None:
        from .master_db import get_idol_profile
        profile = get_idol_profile(idol_card_id)
    if profile is None: _fail('目前 Master 找不到所選偶像卡。')
    flow = profile.plan_type + '|' + profile.exam_effect_type
    mode = {'produce-004': 'nia-pro', 'produce-005': 'nia-master'}.get(produce_id)
    if not any(tuple(cell) == (flow, mode) for cell in capability['scope_cells']):
        _fail('這個流派／模式不在已訓練的編成範圍；不會替換成其他角色或模式。')
    return capability


class InitialLoadoutSelector:
    def __init__(self, descriptor):
        from .rl.native_actor_policy import load_actor_checkpoint
        from .rl.observed_initial_loadout import build_initial_loadout_source
        from .rl.initial_loadout_policy import InitialLoadoutPolicyView
        self.descriptor = deepcopy(descriptor); self.watches = {}; self.last_report = {}; self.lock = threading.RLock()
        if descriptor.get('portable_actor') is True:
            from .portable_model_assets import configured_portable_assets
            from .portable_actor_assets import actor_descriptor, load_actor_parameters
            from .portable_loadout_assets import portable_initial_descriptor, build_portable_initial_source
            assets = configured_portable_assets()
            if assets is None or descriptor != actor_descriptor(assets):
                _fail('公開編成模型在載入前已變更。')
            self.capability = portable_initial_descriptor(assets)
            self.model, self.metadata = load_actor_parameters(assets)
            self.model.eval().requires_grad_(False); self.view = InitialLoadoutPolicyView(self.model)
            self.source = build_portable_initial_source(assets)
            self.templates = OrderedDict(); self.validate_unchanged()
            return
        def read(ref, document=False):
            path = Path(ref['path']).resolve(); before = source_stamp(path); raw = path.read_bytes()
            if (hashlib.sha256(raw).hexdigest() != ref['sha256'] or ref.get('bytes', len(raw)) != len(raw)
                    or source_stamp(path) != before):
                _fail('編成來源檔案已變更。')
            self.watches[path] = before
            return json.loads(raw) if document else raw
        spec = descriptor['specification']; qualification = read(spec['qualification'], True)
        self.capability = verify_capability(descriptor['initial_loadout']['reference'], spec, qualification, read)
        self.model, self.metadata = load_actor_checkpoint(spec['checkpoint'], dataset_identity=spec['dataset_identity'],
            training_source_code=spec['training_source_code'], observed_source_code=spec.get('observed_source_code'))
        self.model.eval().requires_grad_(False); self.view = InitialLoadoutPolicyView(self.model)
        self.source = build_initial_loadout_source(self.capability['material_specification'])
        self.templates = OrderedDict(); self.validate_unchanged()

    def validate_unchanged(self):
        if any(source_stamp(path) != stamp for path, stamp in self.watches.items()): _fail('本次固定模型或編成來源已變更。')
        self.source.validate_unchanged()

    def _candidate(self, family, facts, selection_key):
        from .rl.initial_loadout_policy import canonical_candidate
        key = family, digest(facts)
        if key not in self.templates:
            try:
                self.templates[key] = canonical_candidate(family, facts, semantic_source=self.source,
                    source_master_hash=self.capability['master_hash'])
            except (ValueError, KeyError) as exc:
                _fail('目前精確來源無法完整解讀這張支援卡／回憶；不會改用規則或缺省語義：' + str(exc))
            while len(self.templates) > 4096: self.templates.popitem(last=False)
        self.templates.move_to_end(key)
        return {**deepcopy(self.templates[key]), 'selection_key': selection_key, 'available': True}

    def recommend(self, snapshot, loadout, *, constraints, idol_card_id, produce_id, limit=1, progress_callback=None, cancelled=None):
        from .account_loadout import BorrowedSupportCard, LoadoutSelection, InitialLoadoutRecommendation, validate_selection
        from .master_db import get_idol_profile
        from .rl.initial_loadout_policy import project_initial_loadout, LIVE_SEMANTICS, MAX_CANDIDATES
        if self.descriptor.get('portable_actor') is True:
            from .rl.initial_loadout_policy import batch_from_projected
        else:
            from devtools.rl.initial_loadout_batch import batch_from_projected
        import torch
        if type(limit) is not int or limit < 1: _fail('推薦數量必須為正整數。')
        with self.lock:
            self.validate_unchanged(); started = time.monotonic(); now = datetime.now(timezone.utc)
            self.last_report = {'method': METHOD, 'score_kind': 'initial-composition-log-probability',
                'checkpoint_sha256': self.descriptor['model_sha256'], 'status': 'preparing', 'completed': 0,
                'processed': 0, 'total': 10, 'native_execution': False, 'game_io': False,
                'score_improvement_verified': False}
            loadout = deepcopy(loadout)
            profile = get_idol_profile(idol_card_id)
            require_scope({**self.descriptor, 'initial_loadout': self.capability}, idol_card_id=idol_card_id,
                produce_id=produce_id, profile=profile)
            if any(loadout.get(key) != value for key, value in (('account_scope', snapshot.account_scope),
                    ('idol_card_id', idol_card_id), ('produce_id', produce_id))): _fail('帳號或編成畫面與選角模式不一致。')
            idol = next((x for x in snapshot.idol_cards if x.card_id == idol_card_id), None)
            if idol is None: _fail('所選偶像卡不在實際持有牌庫。')
            if snapshot.master_version is not None and snapshot.master_version not in (
                    self.capability['master_hash'], self.capability.get('execution_master_version')):
                if self.descriptor.get('portable_actor') is not True:
                    _fail('實際牌庫版本與已驗收的編成語義來源不一致。')
                from .runtime_live_exam_input import validate_public_reference_material_scope
                self.last_report['material_lookup'] = validate_public_reference_material_scope(
                    self.descriptor['public_reference_material_policy'],
                    checkpoint_sha256=self.descriptor['model_sha256'],
                    reference_master_hash=self.capability['master_hash'],
                    model_manifest_sha256=self.descriptor['portable_assets_reference']['sha256'],
                    actual_master_version=snapshot.master_version, idol_card_id=idol_card_id, produce_id=produce_id)
            condition = {'produce_id': produce_id, 'idol_card_id': idol_card_id, 'plan_type': profile.plan_type,
                'exam_effect_type': profile.exam_effect_type, 'idol_ranks': {'level_limit': idol.level_limit_rank,
                    'level_limit_present': True, 'potential': idol.potential_rank, 'potential_present': True}}
            candidates = []; originals = {}; owned = {}; rentals = {}; memories = {}
            for row in snapshot.support_cards:
                if row.plan_type not in (profile.plan_type, 'ProducePlanType_Common') or row.card_id in constraints.excluded_support_ids: continue
                key = 'owned:' + row.card_id; owned[key] = row
                facts = {'support_card_id': row.card_id, 'level': row.level, 'level_limit_rank': row.level_limit_rank,
                    'level_limit_rank_present': True, 'is_rental': False, 'rental_field_present': True}
                candidates.append(self._candidate('support', facts, key)); originals[key] = row
            captured = datetime.fromisoformat(str(loadout['captured_at']).replace('Z', '+00:00'))
            for raw in loadout.get('rental_support_cards', []):
                if raw['plan_type'] not in (profile.plan_type, 'ProducePlanType_Common') or constraints.locked_rental_key not in (None, raw['rental_key']): continue
                rental = BorrowedSupportCard(raw['rental_key'], raw['card_id'], raw['level'], raw['plan_type'],
                    raw.get('expires_at', (captured + timedelta(minutes=5)).isoformat()))
                if datetime.fromisoformat(rental.expires_at.replace('Z', '+00:00')) <= now: continue
                key = 'rental:' + rental.rental_key
                if key in originals: _fail('借卡身份重複，請重新讀取編成。')
                rentals[key] = rental; originals[key] = rental
                facts = {'support_card_id': rental.card_id, 'level': rental.level, 'level_limit_rank': None,
                    'level_limit_rank_present': False, 'is_rental': True, 'rental_field_present': True}
                candidates.append(self._candidate('support', facts, key))
            for row in snapshot.memories:
                if row.memory_id in constraints.excluded_memory_ids: continue
                raw = row.to_dict(); card = raw['produce_card']; key = 'memory:' + row.memory_id
                memories[key] = row; originals[key] = row
                facts = {'idol_card_id': row.idol_card_id, 'plan_type': row.plan_type, 'produce_card_phase_type': row.phase_type,
                    'produce_card': None if card is None else {'id': card['id'], 'upgrade_count': card['upgradeCount'],
                        'upgrade_count_present': True, 'customizes': [{'id': x['id'], 'count': x['customizeCount']} for x in card['customizes']]},
                    'abilities': raw['abilities'], 'is_rental': False, 'rental_field_present': True}
                candidates.append(self._candidate('memory', facts, key))
            locks = ['owned:' + x for x in constraints.locked_support_ids] + ['memory:' + x for x in constraints.locked_memory_ids]
            if constraints.locked_rental_key is not None: locks.append('rental:' + constraints.locked_rental_key)
            if set(locks) - originals.keys() or len(owned) < 5 or len(memories) < 4 or not rentals:
                _fail('實際持有／借卡不足或鎖定項目不可用，無法組成五自有、一借卡及四回憶。')
            if len(candidates) > MAX_CANDIDATES: _fail('實際候選超過已驗收容量；不會截斷牌庫。')
            candidates.sort(key=lambda x: x['selection_key']); prefix = []; logps = []; decisions = []
            def feasible(selected):
                own_ids = {x['facts']['support_card_id'] for x in selected if x['family'] == 'support' and x['role'] == 'owned'}
                chosen = [originals[x['selection_key']] for x in selected if x['family'] == 'support' and x['role'] == 'rental']
                if len(own_ids) > 5 or len(chosen) > 1: return False
                required = set(constraints.locked_support_ids)
                return any(r.card_id not in own_ids | required and
                    len({x.card_id for x in owned.values()} - own_ids - {r.card_id}) >= 5 - len(own_ids)
                    for r in chosen or rentals.values())
            if not feasible(prefix): _fail('自有支援與借卡鎖定互斥，沒有完整合法組合。')
            for family, count in (('support', 6), ('memory', 4)):
                for _ in range(count):
                    if cancelled and cancelled():
                        self.last_report.update(status='cancelled')
                        raise LoadoutRecommendationCancelled(self.last_report)
                    self.validate_unchanged(); current = deepcopy(candidates); infeasible = []
                    if family == 'support':
                        for candidate in current:
                            if (candidate['family'] == 'support'
                                    and candidate['selection_key'] not in {row['selection_key'] for row in prefix}
                                    and not feasible([*prefix, candidate])):
                                infeasible.append(candidate['selection_key'])
                    projected = project_initial_loadout(condition, prefix, current, family=family,
                        selection_semantics=LIVE_SEMANTICS, schema=self.model.schema,
                        constraints={'locked_keys': locks, 'excluded_keys': infeasible})
                    batch = batch_from_projected([projected], self.model.schema, device=next(self.model.parameters()).device)
                    with torch.inference_mode(): logits, mask = self.view.decoder_logits(batch)
                    index = int(logits[0].argmax()); logp = float(logits[0].log_softmax(-1)[index])
                    if index >= len(current) or not mask[0, index]: _fail('模型選擇不在實際可選編成內。')
                    chosen = deepcopy(current[index]); chosen['available'] = True; prefix.append(chosen); logps.append(logp)
                    decisions.append({'family': family, 'selection_key': chosen['selection_key'], 'log_probability': logp})
                    self.last_report = {'method': METHOD, 'score_kind': 'initial-composition-log-probability',
                        'checkpoint_sha256': self.descriptor['model_sha256'], 'status': 'running', 'completed': 0, 'decisions': decisions,
                        'processed': len(prefix), 'total': 10, 'elapsed_seconds': time.monotonic() - started,
                        'native_execution': False, 'game_io': False, 'score_improvement_verified': False}
                    if progress_callback: progress_callback({'phase': 'selecting-initial-loadout', 'processed': len(prefix),
                        'total': 10, 'succeeded': len(prefix), 'failed': 0, 'cached_candidates': 0})
            selected_owned = tuple(originals[x['selection_key']].card_id for x in prefix if x['family'] == 'support' and x['role'] == 'owned')
            selected_rental = next(originals[x['selection_key']] for x in prefix if x['family'] == 'support' and x['role'] == 'rental')
            selected_memories = tuple(originals[x['selection_key']].memory_id for x in prefix if x['family'] == 'memory')
            selection = LoadoutSelection(idol_card_id, selected_owned, selected_rental, selected_memories,
                snapshot.content_digest, snapshot.account_scope, produce_id)
            validate_selection(snapshot, selection, constraints=constraints)
            self.validate_unchanged(); self.last_report.update(status='completed', completed=1)
            return (InitialLoadoutRecommendation(selection, sum(logps) / len(logps),
                ('共用 RL 依同場培育結果學習後逐步選卡；對數機率不是演出分數或通關保證。',), METHOD),)


def build_selector(descriptor):
    if (not isinstance(descriptor, dict) or not isinstance(descriptor.get('initial_loadout'), dict)
            or descriptor['initial_loadout'].get('trained_updates', 0) <= 0
            or descriptor['initial_loadout'].get('trained_critic_updates') != descriptor['initial_loadout'].get('trained_updates')
            or descriptor['initial_loadout'].get('method') != METHOD
            or not isinstance(descriptor['initial_loadout'].get('reference'), dict)):
        _fail('目前權重沒有已驗收的初始編成任務。')
    key = digest(descriptor)
    with _LOCK:
        if key not in _CACHE:
            _CACHE[key] = InitialLoadoutSelector(descriptor)
            while len(_CACHE) > 2: _CACHE.popitem(last=False)
        selector = _CACHE[key]; selector.validate_unchanged(); _CACHE.move_to_end(key)
        return selector
