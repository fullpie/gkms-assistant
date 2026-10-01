"""Compose the existing private outer policy; no alternate input owner.

Loading validates an explicit local plan and freezes its references. Models
are loaded once per run and existing native providers are created only when
the selected source needs them. Only the providers' normal choose path can
start an offline child. A valid plan is not a whole-cultivation quality claim.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import threading

from .application_paths import app_root,public_installation
from .qualification_verification import source_stamp
from .rl.contracts import ContractError,digest

SCHEMA='gkms.private-outer-policy.v1'
RELATIVE_PATH=Path('var/devtools/private_outer_policy.json')
EXECUTION_SOURCES=(
    'src/gkms_tool/private_outer_policy.py','src/gkms_tool/runtime_rl_outer_policy.py',
    'devtools/rl/outer_candidate_preview.py','devtools/rl/outer_outcome_dispatch.py',
    'devtools/rl/outer_source_resolver.py','src/gkms_tool/rl/outer_value.py',
    'src/gkms_tool/rl/outer_card_outcomes.py','src/gkms_tool/rl/outer_drink_outcomes.py',
    'src/gkms_tool/rl/outer_pitem_outcomes.py',
    'src/gkms_tool/future_produce_items.py',
    'src/gkms_tool/rl/outer_event_outcomes.py','src/gkms_tool/customization_budget_outcomes.py',
    'src/gkms_tool/customization_outcomes.py','src/gkms_tool/customization_contract.py',
    'src/gkms_tool/card_mutation_effects.py','src/gkms_tool/lesson_extra_effects.py',
    'src/gkms_tool/lesson_outcome_branches.py','src/gkms_tool/lesson_stamina_quote.py',
    'src/gkms_tool/economy_outcomes.py',
    'devtools/rl/outer_future_visit.py','src/gkms_tool/outer_training_budget.py',
    'src/gkms_tool/rl/outer_card_order.py','src/gkms_tool/special_shop_outcomes.py',
)


class PrivateOuterPolicyUnavailable(ValueError):
    pass


def _unavailable(reason):
    return PrivateOuterPolicyUnavailable('私人外層 RL 尚未就緒：'+str(reason))


def _descriptor(root):
    from .rl.private_actor_assets import load_private_actor_descriptor
    return load_private_actor_descriptor(project_root=root,refresh=True)


def _components():
    # Public entry never imports these repository-only capabilities.
    from devtools.rl.outer_candidate_preview import load_fixed_outer_model,build_outer_value_ranker
    from devtools.rl.outer_source_resolver import OuterSourceResolver
    from devtools.rl.native_outer_preview import NativeOuterPreviewProvider
    from .runtime_rl_outer_policy import RuntimeOuterValuePolicy
    return load_fixed_outer_model,build_outer_value_ranker,OuterSourceResolver,NativeOuterPreviewProvider,RuntimeOuterValuePolicy


class _References:
    def __init__(self):self.watches={};self.documents={}

    def pin(self,ref,*,document=True):
        if (not isinstance(ref,dict)or not {'path','sha256'}<=set(ref)<= {'path','sha256','bytes'}
                or not isinstance(ref['path'],str)or not Path(ref['path']).is_absolute()):
            raise _unavailable('需要絕對路徑與雜湊綁定的設定來源')
        path=Path(ref['path']).resolve();before=source_stamp(path);key=str(path),ref['sha256']
        previous=self.watches.get(str(path))
        if previous is not None and previous!=before:raise _unavailable('來源於載入期間變更：'+path.name)
        if key not in self.documents:
            with path.open('rb')as stream:sha=hashlib.file_digest(stream,'sha256').hexdigest()
            if sha!=ref['sha256']or 'bytes'in ref and before[2]!=ref['bytes']:
                raise _unavailable('來源雜湊不符：'+str(path))
            value=json.loads(path.read_bytes())if document else None
            if source_stamp(path)!=before:raise _unavailable('讀取來源期間檔案變更：'+path.name)
            self.watches[str(path)]=before;self.documents[key]=value
        elif document and self.documents[key]is None:
            value=json.loads(path.read_bytes())
            if source_stamp(path)!=before:raise _unavailable('讀取來源期間檔案變更：'+path.name)
            self.documents[key]=value
        return deepcopy(self.documents[key])if document else None

    def validate(self):
        if any(source_stamp(path)!=stamp for path,stamp in self.watches.items()):
            raise _unavailable('本場固定設定、模型或來源程式已变更')


def _same_asset(left,right):
    return (left.get('sha256')==right.get('sha256')and
        Path(left.get('path','')).resolve()==Path(right.get('path','')).resolve())


def _output_directory(value,root,label):
    if not isinstance(value,str)or not Path(value).is_absolute():raise _unavailable(label+' 必須明確指定絕對路徑')
    path=Path(value).resolve()
    # Mutable output may use the existing user-data cache or an explicitly
    # retained worktree cache. It must never alias project source/assets.
    if path==root or any(path.is_relative_to(root/name) or (root/name).is_relative_to(path)
            for name in ('src','devtools','scripts','native','docs')):
        raise _unavailable(label+' 不能覆蓋專案程式目錄')
    return path


class _ProviderSelection:
    """Select an unchanged provider configuration, preserving its cache ID."""
    def __init__(self,factory,cancelled,emit):
        self.owner=factory;self.cancelled=cancelled;self.emit=emit
        self.instances={};self.active=None

    def select(self,source):
        self.owner.validate_unchanged()
        key=source.get('sha256')
        index=next((i for i,entry in enumerate(self.owner.providers)
            if key in entry['document']['source_exports']),None)
        if index is None:raise ContractError('Selected TRAIN source has no explicitly configured native provider: '+str(key))
        if index not in self.instances:
            entry=self.owner.providers[index]
            self.instances[index]=self.owner.dependencies[3](entry['configuration'],self.owner.cache,
                project_root=self.owner.root,max_workers=self.owner.config['max_workers'],
                emit=self.emit,cancelled=self.cancelled,retained_providers=entry['retained_providers'])
        self.active=self.instances[index]
        if self.active.checkpoint!=self.owner.checkpoint or self.active.master_hash!=self.owner.master_hash:
            raise ContractError('Selected native provider differs from the fixed actor or Master')
        self.validate_unchanged()

    def _required(self):
        if self.active is None:raise ContractError('Exact outer source must be selected before native provider use')
        return self.active

    @property
    def identity(self):return self._required().identity
    @property
    def quote_validator(self):return self._required().quote_validator
    @quote_validator.setter
    def quote_validator(self,value):self._required().quote_validator=value
    def validate_unchanged(self):
        self.owner.validate_unchanged()
        for provider in self.instances.values():provider.validate_unchanged()
    def bind_preparation(self,report):return self._required().bind_preparation(report)
    def __call__(self,requests):return self._required()(requests)


class PrivateOuterPolicyFactory:
    def __init__(self,root,config,manifest_ref,refs,descriptor,providers,selection,progress_callback):
        self.root=root;self.config=config;self.manifest_reference=manifest_ref;self.refs=refs
        self.descriptor=descriptor;self.checkpoint=descriptor['model_sha256'];self.providers=providers
        self.selection=selection;self.master_hash=config['master_hash'];self.progress=progress_callback
        self.cache=_output_directory(config['cache_directory'],root,'快取目錄')
        self.output=_output_directory(config['evidence_directory'],root,'決策證據目錄')
        self.master_dir=Path(config['outer_master_manifest']['path']).resolve().parent/'Master'
        self.database=Path(config['database']['path']).resolve()
        self.dependencies=None;self._lock=threading.RLock();self._run=None;self._policy=None
        self._resolver=None;self._model=None;self._provider=None

    def validate_unchanged(self):self.refs.validate()

    def __call__(self,*,run_id,produce_id,idol_card_id,cancelled=lambda:False):
        with self._lock:
            self.validate_unchanged()
            if any(not isinstance(value,str)or not value for value in (run_id,produce_id,idol_card_id))or not callable(cancelled):
                raise _unavailable('固定場次／角色／模式及停止回呼不完整')
            scope=run_id,produce_id,idol_card_id
            if self._run is not None and scope!=self._run:raise _unavailable('不能把同一工廠的模型／交易意圖跨場重用')
            if self._policy is not None:return self._policy
            if cancelled():raise _unavailable('已要求停止，未建立模型或原生工作')
            self._run=scope;self.dependencies=_components()
            load_model,build_ranker,resolve_sources,_,policy_type=self.dependencies
            future=self.selection.get('future_tiers_by_mode',{}).get(produce_id,self.selection.get('future_tiers'))
            if self.progress is not None:
                self.progress({'source_run_id':run_id,'outer_model_progress':{'status':'loading-fixed-model',
                    'fixed_checkpoint_sha256':self.checkpoint}})
            self._resolver=resolve_sources(self.selection['reference'],checkpoint_sha256=self.checkpoint,
                master_hash=self.master_hash,master_dir=self.master_dir,database=self.database,
                future_tiers=future,supplemental_sources=self.selection.get('supplemental_sources',()))
            if not any(row['produce_id']==produce_id and row['idol_card_id']==idol_card_id for row in self._resolver.contexts):
                raise _unavailable('沒有這個角色／模式的明確 TRAIN 情境來源')
            self._model,schema,validate_model=load_model(self.descriptor['specification'],device='cpu')
            if cancelled():raise _unavailable('模型載入後已要求停止，未建立原生工作')
            def emit(event):
                if self.progress is not None:self.progress({'source_run_id':run_id,'outer_model_progress':deepcopy(event)})
            self._provider=_ProviderSelection(self,cancelled,emit)
            def source(raw,context):
                self.validate_unchanged();self._resolver.validate_unchanged()
                resolved=self._resolver(raw,context)
                if self.config.get('future_items')is not None:
                    resolved['item_reference_context']=self._resolver.continuation_context(resolved['source_ref'])
                self._provider.select(resolved['source_ref'])
                return resolved
            def checked_model(actual):
                self.validate_unchanged();return validate_model(actual)
            ranker=build_ranker(model=self._model,schema=schema,checkpoint_sha256=self.checkpoint,
                validate_model=checked_model,preview_provider=self._provider,source_resolver=source,
                master_dir=self.master_dir,database=self.database,
                evidence_directory=self.output/digest({'run_id':run_id,'checkpoint':self.checkpoint})[:20],
                max_batch=self.config['max_batch'],
                **({'future_items':deepcopy(self.config['future_items'])}if self.config.get('future_items')is not None else{}),
                **({'future_visit':deepcopy(self.config['future_visit'])}if self.config.get('future_visit')is not None else{}))
            def guarded_ranker(native,*,context,operation_context=None):
                self.validate_unchanged();self._resolver.validate_unchanged()
                emit({'status':'preparing','fixed_checkpoint_sha256':self.checkpoint})
                result=ranker(native,context=context,operation_context=operation_context)
                self.validate_unchanged();self._resolver.validate_unchanged()
                emit({'status':result.get('status'),'fixed_checkpoint_sha256':self.checkpoint,
                    'model_used':result.get('model_used',False),'unavailable':deepcopy(result.get('unavailable',[]))})
                return result
            self._policy=policy_type(run_id=run_id,produce_id=produce_id,idol_card_id=idol_card_id,
                checkpoint_sha256=self.checkpoint,ranker=guarded_ranker,cancelled=cancelled)
            self.validate_unchanged();return self._policy


def load_private_outer_policy_factory(*,project_root=None,manifest_path=None,progress_callback=None):
    root=Path(project_root or app_root()).resolve()
    if public_installation(root):raise _unavailable('公開版不能載入私人外層模型／原生研究來源')
    profile=os.environ.get('GKMS_PRIVATE_RUNTIME_PROFILE')
    if profile is not None and Path(profile).resolve()!=(root/'devtools/runtime-profile.json').resolve():
        raise _unavailable('私人 GUI profile 與專案根目錄不一致')
    path=Path(manifest_path).resolve()if manifest_path is not None else(root/RELATIVE_PATH).resolve()
    if not path.is_relative_to(root)or not path.is_file():raise _unavailable('缺少專案內明確設定：'+str(path))
    refs=_References()
    before=source_stamp(path);raw=path.read_bytes()
    if len(raw)>1024*1024 or source_stamp(path)!=before:raise _unavailable('設定過大或讀取期間變更')
    manifest_ref={'path':str(path),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
    try:
        config=refs.pin(manifest_ref)
        if isinstance(config,dict)and config.get('schema')=='gkms.private-observed-outer-policy.v2':
            return _load_observed_factory(root,config,manifest_ref,refs,progress_callback)
        required={'schema','private_only','enabled','registration','checkpoint_sha256','master_hash','providers',
            'source_selection','outer_master_manifest','database','execution_sources','cache_directory','evidence_directory','max_workers','max_batch'}
        if (not isinstance(config,dict)or not required<=set(config)or set(config)-required-{'acceptance_evidence','future_visit','future_items'}
                or config['schema']!=SCHEMA or config['private_only']is not True or config['enabled']is not True):
            raise _unavailable('私人外層設定未啟用或契約不完整')
        for name,maximum in (('max_workers',20),('max_batch',128)):
            if type(config[name])is not int or not 1<=config[name]<=maximum:raise _unavailable(name+' 超出有界範圍')
        items=config.get('future_items')
        if items is not None:
            if not isinstance(items,dict)or set(items)!={'max_plans','max_acquisitions'}:
                raise _unavailable('未來道具效果需要明確且有界的 TRAIN 情境設定')
            for name,maximum in (('max_plans',128),('max_acquisitions',32)):
                if type(items[name])is not int or not 1<=items[name]<=maximum:
                    raise _unavailable('未來道具 '+name+' 超出有界範圍')
        future=config.get('future_visit')
        if future is not None:
            if not isinstance(future,dict)or set(future)!= {'max_plans','max_operations'}:
                raise _unavailable('未來單次強化需要明確且有界的設定')
            for name,maximum in (('max_plans',128),('max_operations',16)):
                if type(future[name])is not int or not 1<=future[name]<=maximum:
                    raise _unavailable('未來單次強化 '+name+' 超出有界範圍')
        if not _same_asset(config['registration'],{'path':str(root/'var/devtools/private_rl_actor.json'),'sha256':config['registration'].get('sha256')}):
            raise _unavailable('設定必須使用此私人 GUI 的已驗收模型登錄')
        refs.pin(config['registration'])
        descriptor=_descriptor(root)
        if (not descriptor or descriptor.get('private_only')is not True or descriptor.get('live_ready')is not True
                or descriptor.get('fixed_weights')is not True or descriptor.get('model_sha256')!=config['checkpoint_sha256']
                or not _same_asset(descriptor['descriptor_reference'],config['registration'])):
            raise _unavailable('固定私人演出模型未合格，或設定指定另一版本')
        spec=descriptor['specification']
        for ref in [spec['checkpoint'],*spec['semantic_sources'].values(),*spec['training_source_code'].values(),
                *[spec[name]for name in ('qualification','projection_equivalence','runtime_master_source',
                    'runtime_io_equivalence','runtime_loader_compatibility')if name in spec]]:
            refs.pin(ref,document=False)
        if set(config['execution_sources'])!=set(EXECUTION_SOURCES):raise _unavailable('外層執行程式來源清單不完整')
        for relative,ref in config['execution_sources'].items():
            if Path(ref['path']).resolve()!=(root/relative).resolve():raise _unavailable('程式來源不屬於目前專案：'+relative)
            refs.pin(ref,document=False)
        for ref in config.get('acceptance_evidence',[]):refs.pin(ref,document=False)
        outer=refs.pin(config['outer_master_manifest']);refs.pin(config['database'],document=False)
        if outer.get('master_hash')!=config['master_hash']:raise _unavailable('外層 Master 與設定版本不同')
        selection=refs.pin(config['source_selection'])
        if (not isinstance(selection,dict)or not {'reference'}<=set(selection)<= {'reference','future_tiers','future_tiers_by_mode','supplemental_sources'}):
            raise _unavailable('需要明確 TRAIN 情境選擇設定')
        if selection.get('future_tiers')and selection.get('future_tiers_by_mode'):
            raise _unavailable('future_tiers 與 future_tiers_by_mode 不能同時指定')
        by_mode=selection.get('future_tiers_by_mode',{})
        if not isinstance(by_mode,dict)or set(by_mode)-{'produce-004','produce-005'}:
            raise _unavailable('未來情境必須按明確 NIA 模式指定')
        for tiers in [selection.get('future_tiers',{}),*by_mode.values()]:
            if not isinstance(tiers,dict)or set(tiers)-{'Mid1','Mid2','Final'}:
                raise _unavailable('未來情境的場次不明確')
            for value in tiers.values():
                if (not isinstance(value,dict)or set(value)!= {'tier','assumption_basis'}or type(value['tier'])is not int
                        or value['tier']<1 or not isinstance(value['assumption_basis'],str)or not value['assumption_basis'].strip()):
                    raise _unavailable('未來情境需要明確難度與假設依據')
        source=refs.pin(selection['reference'])
        if (source.get('checkpoint_sha256')!=config['checkpoint_sha256']or source.get('execution_master_hash')!=config['master_hash']
                or source.get('partition')!='train'or source.get('validation_or_test_consumed')is not False):
            raise _unavailable('TRAIN 情境來源與固定模型或 Master 不一致')
        providers=[]
        if not isinstance(config['providers'],list)or not 1<=len(config['providers'])<=16:raise _unavailable('需要有界的原生 provider 設定清單')
        for entry in config['providers']:
            if not isinstance(entry,dict)or set(entry)!= {'configuration','retained_providers'}or not isinstance(entry['retained_providers'],list):
                raise _unavailable('provider 必須帶明確設定及保留證據清單')
            document=refs.pin(entry['configuration']);policy=refs.pin(document['policy'])
            if document.get('schema')!='gkms.native-outer-preview-provider.v1' or not isinstance(document.get('source_exports'),dict):
                raise _unavailable('原生 provider 契約不符')
            spec=policy.get('specification',{});registered=descriptor['specification']
            for key in ('checkpoint','dataset_identity','semantic_sources','training_source_code'):
                if spec.get(key)!=registered.get(key):raise _unavailable('原生 provider 與本場模型／訓練來源不一致：'+key)
            if spec.get('device')!='cpu':raise _unavailable('原生 provider 必須沿用固定 CPU actor')
            master=refs.pin(document['master_manifest'])
            if master.get('archived_master_hash')!=config['master_hash']:raise _unavailable('原生 provider Master 不同')
            for proof in entry['retained_providers']:refs.pin(proof)
            providers.append({**deepcopy(entry),'document':document})
        refs.validate()
        return PrivateOuterPolicyFactory(root,config,manifest_ref,refs,deepcopy(descriptor),providers,selection,progress_callback)
    except (KeyError,TypeError,OSError,ValueError)as error:
        if isinstance(error,PrivateOuterPolicyUnavailable):raise
        raise _unavailable(error)from error


def _load_observed_factory(root,config,manifest_ref,refs,progress_callback):
    """Explicit v2 composition through this same factory, with no preview provider."""
    from .runtime_observed_outer_policy import FACTORY_SCHEMA,EXECUTION_SOURCES as observed_sources
    required={'schema','private_only','enabled','registration','checkpoint_sha256','outer_master_manifest','execution_sources'}
    if (set(config)-required-{'acceptance_evidence','source_relocations'}or not required<=set(config)
            or config['schema']!=FACTORY_SCHEMA or config['private_only']is not True or config['enabled']is not True):
        raise _unavailable('觀測式 v2 外層設定未明確啟用或契約不完整')
    registration=config['registration']
    if not _same_asset(registration,{'path':str(root/'var/devtools/private_rl_actor.json'),'sha256':registration.get('sha256')}):
        raise _unavailable('觀測式 actor 必須使用本專案已驗收的固定登錄')
    refs.pin(registration);descriptor=_descriptor(root)
    if (not descriptor or descriptor.get('live_ready')is not True or descriptor.get('private_only')is not True
            or descriptor.get('fixed_weights')is not True or descriptor.get('model_sha256')!=config['checkpoint_sha256']
            or not _same_asset(descriptor['descriptor_reference'],registration)):
        raise _unavailable('觀測式 actor 登錄未合格或身份不同')
    spec=descriptor['specification']
    if spec.get('device')!='cpu'or not isinstance(spec.get('observed_source_code'),dict)or not spec.get('runtime_master_source'):
        raise _unavailable('觀測式 v2 必須帶完整學習程式及現行語意來源')
    for ref in [spec['checkpoint'],*spec['training_source_code'].values(),*spec['observed_source_code'].values(),
            *spec['semantic_sources'].values(),*[spec[name]for name in('runtime_master_source','runtime_io_equivalence',
                'runtime_loader_compatibility','projection_equivalence','qualification')if name in spec]]:
        refs.pin(ref,document=False)
    refs.pin(config['outer_master_manifest'])
    if config.get('source_relocations')is not None:refs.pin(config['source_relocations'])
    if set(config['execution_sources'])!=set(observed_sources):raise _unavailable('觀測式執行／歷史來源清單不完整')
    for relative,ref in config['execution_sources'].items():
        if Path(ref['path']).resolve()!=(root/relative).resolve():raise _unavailable('觀測式程式來源不屬於此專案')
        refs.pin(ref,document=False)
    for ref in config.get('acceptance_evidence',[]):refs.pin(ref,document=False)
    refs.validate()
    return PrivateObservedOuterPolicyFactory(root,config,manifest_ref,refs,descriptor,progress_callback)


class PrivateObservedOuterPolicyFactory:
    """One frozen explicit v2 model and observed material package per run."""
    observed_actor=True
    def __init__(self,root,config,manifest_ref,refs,descriptor,progress_callback):
        self.root=root;self.config=deepcopy(config);self.manifest_reference=manifest_ref;self.refs=refs
        self.descriptor=deepcopy(descriptor);self.progress=progress_callback
        self._run=None;self._policy=None;self._lock=threading.RLock()

    def __call__(self,*,run_id,produce_id,idol_card_id,cancelled=lambda:False):
        with self._lock:
            self.refs.validate();scope=run_id,produce_id,idol_card_id
            if any(not isinstance(value,str)or not value for value in scope)or not callable(cancelled):
                raise _unavailable('觀測式 actor 需要固定場次／角色／模式及停止回呼')
            if self._run is not None and self._run!=scope:raise _unavailable('觀測式固定 actor 不可跨場重用')
            if self._policy is not None:return self._policy
            if cancelled():raise _unavailable('停止後不載入觀測式 actor')
            from .rl.native_actor_policy import load_actor_checkpoint
            from .rl.observed_outer_fine_projection import build_fine_semantic_source
            from .runtime_observed_outer_policy import RuntimeObservedOuterPolicy
            spec=self.descriptor['specification'];self._run=scope
            model,metadata=load_actor_checkpoint(spec['checkpoint'],dataset_identity=spec['dataset_identity'],
                training_source_code=spec['training_source_code'],observed_source_code=spec['observed_source_code'])
            material=build_fine_semantic_source(spec['runtime_master_source'],self.config['outer_master_manifest'],
                source_relocations_reference=self.config.get('source_relocations'))
            versions=tuple((key,value._version,value.data_ptr(),str(value.device))for key,value in model.state_dict().items())
            def validate(actual):
                self.refs.validate()
                if (actual is not model or model.training or any(p.requires_grad for p in model.parameters())
                        or tuple((key,value._version,value.data_ptr(),str(value.device))for key,value in model.state_dict().items())!=versions):
                    raise _unavailable('本場觀測式 actor 身份或固定權重改變')
                return metadata
            self._policy=RuntimeObservedOuterPolicy(model=model,semantic_features=material,validate_model=validate,
                run_id=run_id,produce_id=produce_id,idol_card_id=idol_card_id,
                checkpoint_sha256=self.config['checkpoint_sha256'],cancelled=cancelled,
                **({'reference_trial':self.descriptor['runtime_reference_trial']}
                   if self.descriptor.get('runtime_reference_trial') is not None else{}))
            self.refs.validate();return self._policy
