"""Shared exam/observed-OUTER actor with explicitly separate critic objectives."""
from copy import deepcopy
from dataclasses import asdict,dataclass,replace
from pathlib import Path

import torch
from torch import nn

from .contracts import ContractError,digest
from .features import EntityBatch,FeatureSchema
from .networks import NetworkConfig
from .offline_policy import (OfflinePolicyNet,OfflinePolicyBatch,OrderedAction,_Rule,_action_rows,
    _STATE_FIELDS,offline_il_loss,offline_iql_loss)
from .observed_outer_projection import extend_schema,lift_exam_states,ACTION_TYPES,OUTER_OBJECTIVE,VERSION

MODEL_KIND='gkms.rl.shared-observed-outer-policy.v2'
OUTER_SCORE_SCALE=10000.0
OUTER_GAMMA=1.0
FINE_SUBTYPES=frozenset(('outer-native-control','weekly-rest','reward-1','reward-2','reward-3','reward-4','reward-5','reward-skip',
    'card-Add','card-Delete','card-Upgrade','card-Duplicate','card-DuplicateUpgrade','card-Change','card-ChangeUpgrade',
    'pt-card','pt-option','pt-option-already-selected','pt-finish','weekly-action','business-offer','event-option',
    'shop-product','shop-finish','interval-product','interval-finish','audition-tier','audition-retry-tier',
    'confirmation-choice','drink-discard-slot','drink-retained-set'))
FINE_SELECTION_SEMANTICS='recorded-native-legal-controls; no-pool-invention'
OBSERVED_SOURCE_MODULES=(
    'devtools.rl.observed_outer_training','devtools.rl.observed_outer_dataset',
    'devtools.rl.observed_outer_batch','devtools.rl.weekly_observed_dataset',
    'gkms_tool.rl.observed_outer_policy','gkms_tool.rl.observed_outer_projection',
    'gkms_tool.rl.observed_initial_loadout','gkms_tool.passive_catalog',
    'gkms_tool.rl.semantic_entity_encoding','gkms_tool.outer_behavior_candidate',
    'gkms_tool.leaderboard_outer_schedule','gkms_tool.deck_plan','gkms_tool.deck_value','gkms_tool.nia_static_adapter')
OBSERVED_OPTIONAL_SOURCE_MODULES=('gkms_tool.rl.observed_outer_fine_projection','gkms_tool.observed_outer_transitions',
    'gkms_tool.observed_outer_legacy_binding')
_OBSERVED_ARCHIVE_ONLY=frozenset(('gkms_tool.observed_outer_transitions','gkms_tool.observed_outer_legacy_binding'))
OBSERVED_INFERENCE_MODULES=frozenset(name for name in (*OBSERVED_SOURCE_MODULES,*OBSERVED_OPTIONAL_SOURCE_MODULES)
    if name.startswith('gkms_tool.')and name not in _OBSERVED_ARCHIVE_ONLY)


def observed_source_module_names(*,include_fine=False):
    names=list(OBSERVED_SOURCE_MODULES)
    if include_fine:
        from .observed_outer_fine_projection import FINE_IMPLEMENTATION_MODULES
        names.extend((*FINE_IMPLEMENTATION_MODULES,'gkms_tool.observed_outer_transitions',
            'gkms_tool.observed_outer_legacy_binding','devtools.rl.observed_outer_fine_dataset'))
    return tuple(dict.fromkeys(names))


def observed_inference_module_names(*,include_fine=False):
    return frozenset(name for name in observed_source_module_names(include_fine=include_fine)
        if name.startswith('gkms_tool.')and name not in _OBSERVED_ARCHIVE_ONLY)


@dataclass(frozen=True)
class ObservedOuterBatch:
    states:EntityBatch
    candidate_values:torch.Tensor
    candidate_types:torch.Tensor
    selection_mask:torch.Tensor
    resource_legality:torch.Tensor # -1 unobserved, 0 unavailable, 1 observed legal
    decision_subtypes:tuple[str,...]
    selection_semantics:tuple[str,...]
    objective_id:str=OUTER_OBJECTIVE

    def to(self,device):
        return replace(self,states=self.states.to(device),**{name:getattr(self,name).to(device)for name in
            ('candidate_values','candidate_types','selection_mask','resource_legality')})


def batch_from_projected(projected,schema,*,device='cpu'):
    """One tensor path for qualified training rows and current native inference.

    Projection owns source semantics; this routine neither invents unavailable
    controls nor requires teacher actions, reward labels, or devtools imports.
    Each fine row is one actual callback boundary, including selector prefixes.
    """
    from .features import encode_batch
    from .semantic_entity_encoding import ENTITY_FEATURE_NAMES
    projected=tuple(projected)
    if not projected:raise ContractError('Nonempty observed projections required')
    width=max(len(value['candidates'])for value in projected);count=len(projected)
    values=torch.zeros((count,width,len(ENTITY_FEATURE_NAMES)),dtype=torch.float32)
    kinds=torch.zeros((count,width),dtype=torch.long)
    selection=torch.zeros((count,width),dtype=torch.bool)
    legality=torch.zeros((count,width),dtype=torch.int8)
    columns={name:index for index,name in enumerate(ENTITY_FEATURE_NAMES)}
    targets=[]
    for i,entry in enumerate(projected):
        row=[]
        for j,candidate in enumerate(entry['candidates']):
            available=candidate['resource_feasibility']
            if available is not None and type(available)is not bool:
                raise ContractError('Explicit observed candidate legality required')
            kinds[i,j]=candidate['type'];selection[i,j]=available is not False
            legality[i,j]=-1 if available is None else int(available)
            for key,value in candidate['values'].items():values[i,j,columns[key]]=value
            row.append(candidate['target'])
        if len({digest(target)for target in row})!=len(row):
            raise ContractError('Observed candidate targets must be unique')
        targets.append(tuple(row))
    batch=ObservedOuterBatch(encode_batch([p['state']for p in projected],schema),values,kinds,selection,legality,
        tuple(p['decision_subtype']for p in projected),tuple(p['selection_semantics']for p in projected)).to(device)
    _outer_rules(batch,len(ENTITY_FEATURE_NAMES),torch.device(device))
    return batch,tuple(targets)


def _outer_rules(batch,candidate_dim,device):
    values=batch.candidate_values
    if (values.ndim!=3 or values.shape[0]<1 or values.shape[1]<1 or values.shape[1]>4096 or
            values.shape[2]!=candidate_dim or values.dtype!=torch.float32 or values.device!=device or
            not torch.isfinite(values).all()or batch.objective_id!=OUTER_OBJECTIVE):
        raise ContractError('Explicit observed OUTER candidate tensor/objective required')
    b,c,_=values.shape
    for name,dtype in (('candidate_types',torch.long),('selection_mask',torch.bool),('resource_legality',torch.int8)):
        tensor=getattr(batch,name)
        if tensor.shape!=(b,c)or tensor.dtype!=dtype or tensor.device!=device:raise ContractError('Observed OUTER tensor shape/device differs: '+name)
    if (batch.states.global_values.shape[0]!=b or any(getattr(batch.states,name).device!=device for name in _STATE_FIELDS)
            or len(batch.decision_subtypes)!=b or len(batch.selection_semantics)!=b or
            any(x!='outer-weekly-option'and x not in FINE_SUBTYPES for x in batch.decision_subtypes)
            or torch.any((batch.resource_legality<-1)|(batch.resource_legality>1))):
        raise ContractError('Unqualified observed OUTER subtype/offer provenance')
    rules=[]
    for mask,kinds,legal,subtype,semantics in zip(batch.selection_mask.cpu().tolist(),batch.candidate_types.cpu().tolist(),
            batch.resource_legality.cpu().tolist(),batch.decision_subtypes,batch.selection_semantics):
        columns=tuple(i for i,x in enumerate(mask)if x)
        if subtype=='outer-weekly-option':
            if semantics!='recorded-offers; resource-legality-unknown-when-null'or not columns or any(
                    kinds[i]!=ACTION_TYPES.index('schedule')or legal[i]==0 for i in columns):
                raise ContractError('Weekly offers require schedule candidates and cannot include observed illegal options')
        elif semantics!=FINE_SELECTION_SEMANTICS or not columns or any(
                not 1<=kinds[i]<len(ACTION_TYPES)or legal[i]==0 for i in columns):
            raise ContractError('Fine observed choices require their actual complete native legal controls')
        if any(kinds[i]!=0 and legal[i]!=0 for i in range(c)if not mask[i]):
            raise ContractError('Masked candidate must be padding or observed illegal, not an omitted unknown offer')
        rules.append(_Rule(columns,None))
    return tuple(rules)


class ObservedOuterPolicyNet(OfflinePolicyNet):
    model_kind=MODEL_KIND

    def __init__(self,exam_schema,network=NetworkConfig(),*,candidate_dim=512):
        super().__init__(extend_schema(exam_schema),network,candidate_dim=candidate_dim)
        self.original_schema=exam_schema
        self.action_types=nn.Embedding(len(ACTION_TYPES),network.embedding,padding_idx=0)
        d=network.embedding
        self.outer_q1=nn.Sequential(nn.Linear(2*d,64),nn.GELU(),nn.Linear(64,1))
        self.outer_q2=nn.Sequential(nn.Linear(2*d,64),nn.GELU(),nn.Linear(64,1))
        self.outer_expectile_v=nn.Sequential(nn.Linear(d,64),nn.GELU(),nn.Linear(64,1))

    def checkpoint_metadata(self):
        original=super().checkpoint_metadata()
        return {**original,'model_kind':MODEL_KIND,'original_exam_schema_sha256':self.original_schema.identity,
            'projection_version':VERSION,'action_types':list(ACTION_TYPES),
            'outer_objective':{'id':OUTER_OBJECTIVE,'score_scale':OUTER_SCORE_SCALE,'gamma':OUTER_GAMMA,
                'heads':['outer_q1','outer_q2','outer_expectile_v'],'reward':'recorded final Produce rating; sparse nonterminal zero'}}

    def encode(self,states):
        return super().encode(lift_exam_states(states,self.original_schema,self.schema))

    def _encode_policy(self,batch):
        if not isinstance(batch,ObservedOuterBatch):return super()._encode_policy(batch)
        rules=_outer_rules(batch,self.candidate_dim,self.global_proj[0].weight.device)
        context=self.encode(batch.states)[:,0]
        candidates=self.candidate_proj(batch.candidate_values)+self.action_types(batch.candidate_types)
        return context,candidates,rules

    def state_value(self,states):
        # An explicit outer objective must never silently use the exam critic.
        self._require_kind(states,outer=False)
        return super().state_value(states)

    def q_values(self,batch,actions):
        if isinstance(batch,ObservedOuterBatch):raise ContractError('Use explicit outer_q_values for Produce-rating objective')
        self._require_kind(batch.states,outer=False)
        return super().q_values(batch,actions)

    def forward(self,batch,actions=None):
        if isinstance(batch,ObservedOuterBatch):raise ContractError('Observed OUTER critic needs explicit objective dispatch')
        self._require_kind(batch.states,outer=False)
        return super().forward(batch,actions)

    def _require_kind(self,states,*,outer):
        from .contracts import DecisionKind
        kinds=tuple(DecisionKind);column=self.schema.global_dim-len(kinds)+kinds.index(DecisionKind.OUTER)
        values=states.global_values[:,column]
        if not torch.all(values==(1 if outer else 0)):raise ContractError('Critic objective does not match the encoded decision domain')

    def outer_state_value(self,states):
        self._require_kind(states,outer=True)
        value=self.outer_expectile_v(self.encode(states)[:,0]).squeeze(-1)
        if not torch.isfinite(value).all():raise ContractError('Nonfinite outer Produce-rating value')
        return value

    def _outer_q_values(self,encoding,rows):
        context,candidates,_=encoding
        action=self._action_state(context,candidates,rows,self.action_gru,self.action_init)
        joined=torch.cat((context,action),dim=-1)
        values=self.outer_q1(joined).squeeze(-1),self.outer_q2(joined).squeeze(-1)
        if any(not torch.isfinite(value).all()for value in values):raise ContractError('Nonfinite outer Produce-rating Q')
        return values

    def outer_q_values(self,batch,actions):
        if not isinstance(batch,ObservedOuterBatch):raise ContractError('Typed observed OUTER batch required')
        self._require_kind(batch.states,outer=True)
        encoding=self._encode_policy(batch)
        rows,_=_action_rows(actions,encoding[2],device=encoding[0].device,complete=True)
        return self._outer_q_values(encoding,rows)


class _OuterObjectiveView(OfflinePolicyNet):
    """Temporary loss dispatch only; all parameters belong to the one shared model."""
    def __init__(self,shared):
        nn.Module.__init__(self);self.shared=shared
    def checkpoint_metadata(self):return {**self.shared.checkpoint_metadata(),'active_loss_objective':OUTER_OBJECTIVE}
    @property
    def expectile_v(self):return self.shared.outer_expectile_v
    def _encode_policy(self,batch):return self.shared._encode_policy(batch)
    def state_value(self,states):return self.shared.outer_state_value(states)
    def q_values(self,batch,actions):return self.shared.outer_q_values(batch,actions)
    def _q_values(self,encoding,rows):return self.shared._outer_q_values(encoding,rows)
    def _teacher_statistics(self,encoding,rows,plans,*,anchor=None,anchor_encoding=None):
        return self.shared._teacher_statistics(encoding,rows,plans,
            anchor=None if anchor is None else anchor.shared,anchor_encoding=anchor_encoding)


def outer_iql_loss(model,target_model,batch,actions,next_states,rewards,terminal,**kwargs):
    if model is target_model or not isinstance(batch,ObservedOuterBatch)or any(not isinstance(m,ObservedOuterPolicyNet)for m in (model,target_model)):
        raise ContractError('Separate outer objective requires the shared observed model and typed batch')
    model._require_kind(batch.states,outer=True)
    anchor=kwargs.pop('anchor_model',None)
    return offline_iql_loss(_OuterObjectiveView(model),_OuterObjectiveView(target_model),batch,actions,next_states,rewards,terminal,
        anchor_model=None if anchor is None else _OuterObjectiveView(anchor),**kwargs)


def map_exam_batch(payload,model):
    value=dict(payload);batch=value['batch']
    if not isinstance(batch,OfflinePolicyBatch):raise ContractError('Original exam offline payload required')
    value['batch']=replace(batch,states=lift_exam_states(batch.states,model.original_schema,model.schema))
    if 'next_states'in value:value['next_states']=lift_exam_states(value['next_states'],model.original_schema,model.schema)
    return value


def initialize_from_exam(parent,*,seed=0):
    """Explicit architecture initialization; never a loose resume or partial load."""
    if type(parent)is not OfflinePolicyNet or parent.candidate_dim!=512:
        raise ContractError('An original exact exam OfflinePolicyNet is required for migration')
    with torch.random.fork_rng(devices=[]):
        torch.manual_seed(seed)
        model=ObservedOuterPolicyNet(parent.schema,parent.config,candidate_dim=parent.candidate_dim)
    old=parent.state_dict();new=model.state_dict();copied=[];extended=[];added=[]
    expandable={'types.weight','zones.weight','action_types.weight'}
    for name,target in new.items():
        if name not in old:
            if not name.startswith(('outer_q1.','outer_q2.','outer_expectile_v.')):raise ContractError('Undeclared new model parameter '+name)
            added.append(name);continue
        source=old[name].detach().cpu()
        if name in expandable:
            if target.ndim!=2 or target.shape[1]!=source.shape[1]or target.shape[0]<=source.shape[0]:
                raise ContractError('Declared categorical append migration shape differs')
            target[:source.shape[0]].copy_(source);extended.append({'name':name,'old_rows':source.shape[0],'new_rows':target.shape[0]})
        else:
            if target.shape!=source.shape or target.dtype!=source.dtype:raise ContractError('Unexpected changed parent tensor '+name)
            target.copy_(source);copied.append(name)
    if set(old)-set(new):raise ContractError('Parent checkpoint parameters were removed')
    model.load_state_dict(new,strict=True)
    if any(not torch.equal(old[name].detach().cpu(),model.state_dict()[name][:value.shape[0]]if name in expandable else model.state_dict()[name])
            for name,value in old.items()):raise ContractError('Parent weights did not survive exact initialization')
    model.to(next(parent.parameters()).device).train(parent.training)
    return model,{'schema':'gkms.observed-outer-exam-initialization.v1','initializer_seed':seed,
        'parent_metadata':parent.checkpoint_metadata(),'new_metadata':model.checkpoint_metadata(),
        'copied_parameters':copied,'append_only_parameters':extended,'new_parameters':added,
        'parent_tensors_preserved_exactly':True,'optimizer_state_reused':False,'resume':False}


def load_initialized_model(weights,metadata,original_schema):
    """Strict model-only restore; Trainer owns full optimizer/RNG/source resume."""
    original_schema=FeatureSchema.from_dict(original_schema)if isinstance(original_schema,dict)else original_schema
    model=ObservedOuterPolicyNet(original_schema,NetworkConfig(**metadata['network']),candidate_dim=metadata['candidate_dim'])
    if model.checkpoint_metadata()!=metadata:raise ContractError('Observed model objective/schema/category metadata differs')
    expected=model.state_dict()
    if set(weights)!=set(expected)or any(weights[k].shape!=v.shape or weights[k].dtype!=v.dtype or not torch.isfinite(weights[k]).all()for k,v in expected.items()):
        raise ContractError('Observed model exact weight layout differs')
    model.load_state_dict(weights,strict=True)
    return model


def initialize_from_exam_checkpoint(checkpoint,*,dataset_identity,training_source_code,seed=0):
    """Strict original v1 loader, then explicit append-only initialization.

    The original numerical module bytes remain unchanged. This is a new model
    initialization, never a resume, source exception, or activation grant.
    """
    from .native_actor_policy import load_actor_checkpoint
    parent,metadata=load_actor_checkpoint(checkpoint,dataset_identity=dataset_identity,
        training_source_code=training_source_code)
    result,receipt=initialize_from_exam(parent,seed=seed)
    receipt.update(parent_checkpoint=deepcopy(checkpoint),dataset_identity=dataset_identity,
        original_source_code=deepcopy(training_source_code),strict_original_loader=True,
        original_source_bindings=deepcopy(metadata['source_bindings']),native_or_live_activation=False)
    return result,receipt
