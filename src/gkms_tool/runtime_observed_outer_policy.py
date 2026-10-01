"""Thin observed v2 actor adapter over the existing outer runner and receipts.

No future rollout, native engine, optimizer, rule ranking or input submission.
History is reconstructed only from the current runner's ordered steps and the
same run's verified immutable segments and completed callback journals.
"""
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

import torch

from .qualification_verification import source_stamp
from .rl.contracts import ContractError, digest
from .rl.observed_outer_policy import MODEL_KIND, ObservedOuterPolicyNet, batch_from_projected
from .rl.observed_outer_projection import OUTER_OBJECTIVE
from .rl.observed_outer_fine_projection import project_fine_observation
from .runtime_rl_outer_policy import (RuntimeOuterValuePolicy, requires_outer_comparison,
    _business_controls_pending)

SCHEMA='gkms.runtime-observed-outer-actor.v2'
FACTORY_SCHEMA='gkms.private-observed-outer-policy.v2'
EXECUTION_SOURCES=(
    'src/gkms_tool/private_outer_policy.py','src/gkms_tool/runtime_observed_outer_policy.py',
    'src/gkms_tool/runtime_rl_outer_policy.py','src/gkms_tool/runtime_outer_runner.py',
    'src/gkms_tool/observed_outer_transitions.py','src/gkms_tool/native_cultivation_evidence.py',
    'src/gkms_tool/runtime_economy_context.py','src/gkms_tool/runtime_economy_policy.py',
    'src/gkms_tool/runtime_reward_drink_capacity.py',
)
_STRATEGIC_ACTIONS=frozenset({
    'schedule.choose','event.choose','business.choose','business.start',
    'reward.select','reward.skip','reward.receive','reward.confirm_skip',
    'card_choice.select','card_choice.reveal','card_choice.confirm',
    'customize.select_card','customize.select_option','customize.execute','customize.finish',
    'shop.select','shop.open_product','shop.buy','shop.finish','shop.confirm_operation','shop.cancel_operation','shop.confirm_finish',
    'interval.select','interval.open_product','interval.execute','interval.finish','interval.confirm_operation','interval.cancel_operation','interval.confirm_finish',
    'drink_choice.toggle','drink_choice.confirm','drink_choice.confirm_skip_new','drink_choice.cancel_skip',
    'reward.drink_capacity_open','reward.drink_capacity_trash',
    'reward.drink_capacity_confirm','reward.drink_capacity_cancel',
    'ui.sheet_confirm','ui.sheet_cancel',
})
_MECHANICAL_ACTIONS=frozenset({'reward.open_group','customize.open_options','customize.back_to_cards',
    'customize.confirm_execute','event.confirm_choice','effect.advance','effect.confirm_card_change'})


def requires_observed_outer_comparison(raw):
    if requires_outer_comparison(raw):return True
    actions=raw.get('legal_actions')or[]
    # A confirmation phase by itself is never proof of a selected intent.
    return any(a.get('action_id')in _STRATEGIC_ACTIONS or
        (a.get('target')or{}).get('button_id')=='schedule.refresh'for a in actions)


class RuntimeObservedHistory:
    """Read the existing ordered run ledger once, then only new settled receipts."""
    def __init__(self,*,run_id,produce_id,idol_card_id,command_root,current_steps,run_root=None):
        from .native_cultivation_evidence import _verified_native_segments
        from .run_identity import DEFAULT_RUN_ROOT
        self.run_id=run_id;self.produce_id=produce_id;self.idol_card_id=idol_card_id
        self.root=Path(command_root);self.current_steps=current_steps
        run,segments=_verified_native_segments(run_id,Path(run_root or DEFAULT_RUN_ROOT))
        if run.produce_id!=produce_id or run.idol_card_id!=idol_card_id:
            raise ContractError('Observed history belongs to another run mode/idol')
        self.saved=[step for segment in segments for step in segment['result']['steps']]
        self._run_material_preflight = (deepcopy(run.evidence.get('outer_asset_preflight'))
            if run.evidence.get('public_reference_materials') is not None else None)
        self._receipts={};self._watches={}

    def _receipt(self,rid):
        if not isinstance(rid,str)or Path(rid).name!=rid or any(c in rid for c in '/\\'):
            raise ContractError('Exact journal request identity required')
        path=self.root/'completed_outer'/(rid+'.json')
        if rid not in self._receipts:
            before=source_stamp(path);raw=path.read_bytes()
            if source_stamp(path)!=before:raise ContractError('Outer history receipt changed while reading')
            self._receipts[rid]=(json.loads(raw),{'path':str(path.resolve()),'sha256':hashlib.sha256(raw).hexdigest()})
            self._watches[path]=before
        if source_stamp(path)!=self._watches[path]:raise ContractError('Outer history receipt changed')
        return self._receipts[rid]

    def settled_control(self,native,target,outcome):
        from .observed_outer_transitions import _control_errors
        if outcome.status!='settled' or not outcome.request_id:return None
        receipt,reference=self._receipt(outcome.request_id);request=receipt.get('request')or{}
        if (_control_errors(receipt,native.session_generation)or request.get('target')!=target
                or request.get('expected_revision')!=native.revision or digest(receipt['before'])!=digest(native.raw)):
            raise ContractError('Continuation lacks the exact settled actor callback receipt')
        return {'after':deepcopy(receipt['outcome']['after']),'source':deepcopy(reference)}

    def latest_settled_skip(self,native,context):
        """Recover only the last recorded actor skip, never search past inputs."""
        from .observed_outer_transitions import _control_errors
        for value in reversed([*self.saved,*self.current_steps()]):
            step=value if isinstance(value,Mapping)else {key:getattr(value,key)for key in ('index','page','action','target','outcome')}
            outcome=step.get('outcome')or{};rid=outcome.get('request_id')
            if not rid:continue
            decision=outcome.get('decision')or{}
            if (step.get('action')not in ('reward.skip','reward.confirm_skip')or outcome.get('status')!='settled'
                    or decision.get('source')!='shared-observed-outer-actor' or decision.get('model_applied')is not True
                    or decision.get('legacy_rule_fallback')is not False
                    or any(decision.get(key)!=value for key,value in context.items())
                    or type(decision.get('actor_logit'))not in (int,float)or not math.isfinite(decision['actor_logit'])
                    or not isinstance(decision.get('candidate_id'),str)):
                return None
            receipt,reference=self._receipt(rid);request=receipt.get('request')or{}
            if (_control_errors(receipt,native.session_generation)
                    or request.get('command')!='outer.action' or (request.get('target')or{}).get('action_id')!=step['action']
                    or (receipt.get('outcome',{}).get('after')or{}).get('revision')!=native.revision):
                return None
            return {'before':deepcopy(receipt['before']),'after':deepcopy(receipt['outcome']['after']),
                'target':deepcopy(request['target']),'revision':request['expected_revision'],
                'session_generation':request['session_generation'],'source':deepcopy(reference),
                'actor_logit':decision['actor_logit'],'candidate_id':decision['candidate_id']}
        return None

    def __call__(self,native):
        from .observed_outer_transitions import _boundary_kind,_control_errors,completed_auditions_from_chain
        from .runtime_economy_context import advance_economy_context
        steps=[];seen={};preflights=[]
        if getattr(self,'_run_material_preflight',None) is not None:
            preflights.append({'source':'normal-run-material-preflight', 'run_id':self.run_id,
                              'value':deepcopy(self._run_material_preflight)})
        for value in [*self.saved,*self.current_steps()]:
            # Do not recursively materialize completed exam traces for an
            # outer-prefix read. These frozen host records remain read-only.
            step=value if isinstance(value,Mapping)else{key:getattr(value,key)for key in('index','page','action','target','outcome')}
            outcome=step.get('outcome')or{};rid=outcome.get('request_id')
            if rid in seen:
                core={k:v for k,v in step.items()if k!='index'}
                if seen[rid]!=core:raise ContractError('Conflicting observed history step receipt')
                continue
            if rid:seen[rid]={k:v for k,v in step.items()if k!='index'}
            proof=(outcome.get('decision')or{}).get('outer_asset_preflight')
            if isinstance(proof,dict):preflights.append({'value':deepcopy(proof)})
            steps.append(step)
        records=[];economy=None
        for ordinal,step in enumerate(steps):
            outcome=step.get('outcome')or{};rid=outcome.get('request_id')
            if not rid or step.get('page')=='exam':continue
            receipt,reference=self._receipt(rid);before=receipt.get('before')or{}
            state,progress=before.get('state')or{},before.get('progress')or{}
            if state.get('in_progress')is not True:continue
            if state.get('produce_id')!=self.produce_id or progress.get('idolCardId')!=self.idol_card_id:
                raise ContractError('Prior active history belongs to another cultivation')
            generation=(receipt.get('request')or{}).get('session_generation')
            errors=_control_errors(receipt,generation)
            if errors:raise ContractError('Unsettled/invalid prior callback history: '+','.join(errors))
            prior=deepcopy(economy);advanced=advance_economy_context(economy,receipt)
            if advanced is not None and advanced is not economy and advanced.get('request_id')==rid:
                advanced={**advanced,'origin_receipt':deepcopy(reference)}
            economy=advanced
            records.append({'position':len(records),'ordinal':ordinal,'step':step,'receipt':receipt,'source':reference,
                'economy_before':prior})
        boundaries=[]
        for record in records:
            previous=boundaries[-1]if boundaries else None
            kind=_boundary_kind(record,previous,records)
            target=record['receipt']['request']['target']
            # Actual selector prefixes are separately observed action/state
            # transitions in the shared fine corpus, never a fabricated set.
            if target['action_id']in('drink_choice.toggle','card_choice.select')and kind is None:
                kind=previous['kind']
            if kind is not None:boundaries.append({'kind':kind,'position':record['position'],'ordinal':record['ordinal']})
        history=[]
        for index,boundary in enumerate(boundaries):
            first=records[boundary['position']]
            end=boundaries[index+1]['position']if index+1<len(boundaries)else len(records)
            following=records[end]['receipt']['before']if end<len(records)else native.raw
            completed=completed_auditions_from_chain(records[boundary['position']:end])
            history.append({'run_step_ordinal':first['ordinal'],'decision_kind':boundary['kind'],
                'state_before':deepcopy(first['receipt']['before']['state']),
                'chosen_action':deepcopy(first['receipt']['request']['target']),
                'state_after':deepcopy(following['state']),'completed_auditions':completed})
        return {'observed_history':history,'original_run_step_ordinal':len(steps),
            'observed_asset_preflights':preflights,'history_source':'existing-native-segments-and-settled-callback-journal'}


class RuntimeObservedOuterPolicy(RuntimeOuterValuePolicy):
    observed_actor=True
    def __init__(self,*,model,semantic_features,validate_model,history_source=None,reference_trial=None,
                 public_reference_material_policy=None,**kwargs):
        if type(model)is not ObservedOuterPolicyNet or model.model_kind!=MODEL_KIND:
            raise ContractError('Observed outer inference requires the explicit v2 shared actor')
        if model.training or any(p.requires_grad for p in model.parameters()):
            raise ContractError('Runtime observed actor must be frozen and in evaluation mode')
        if not callable(validate_model):raise ContractError('Source-bound observed actor validation required')
        super().__init__(ranker=lambda *a,**k:None,**kwargs)
        self.model=model;self.material=semantic_features;self.validate_model=validate_model;self.history_source=history_source
        self.reference_trial=deepcopy(reference_trial)
        self.public_reference_material_policy=None
        self.public_reference_materials=None
        if public_reference_material_policy is not None:
            if reference_trial is not None:
                raise ContractError('Public material lookup cannot carry private trial authorization')
            from .runtime_live_exam_input import validate_public_reference_material_policy
            self.public_reference_material_policy=validate_public_reference_material_policy(
                public_reference_material_policy,checkpoint_sha256=self.context['checkpoint_sha256'],
                reference_master_hash=self.material.package.contract['source_master_hash'],
                model_manifest_sha256=public_reference_material_policy['model_manifest_sha256'])
        if self.reference_trial is not None:
            from .runtime_live_exam_input import validate_runtime_reference_trial
            trial=self.reference_trial
            self.reference_trial=validate_runtime_reference_trial(trial,
                checkpoint_sha256=self.context['checkpoint_sha256'], runtime_source_sha256=trial['runtime_source_sha256'],
                reference_master_hash=self.material.package.contract['source_master_hash'],
                idol_card_id=self.context['idol_card_id'],produce_id=self.context['produce_id'])
        self._finish_proposal=self._confirmed_finish=None
        self._continuation_proposal=self._continuation=None

    @property
    def identity(self):
        return {'schema':SCHEMA,**self.context,'policy_role':'observed-outer-shared-actor',
            'model_kind':MODEL_KIND,'ranking_basis':'shared-actor-argmax','legacy_ranking_applied':False,
            'objective_id':OUTER_OBJECTIVE,'value_used_for_ranking':False,
            **({'public_reference_material_policy':deepcopy(self.public_reference_material_policy),
                'public_reference_materials':deepcopy(self.public_reference_materials),
                'source_equivalence_verified':False,'training_admitted':False,'reference_lookup_only':True}
               if getattr(self,'public_reference_material_policy',None) is not None else{}),
            **({'runtime_reference_trial':deepcopy(self.reference_trial), 'source_equivalence_verified':False,
                'training_admitted':False, 'reference_lookup_only':True}if getattr(self,'reference_trial',None) is not None else{})}

    def bind_history_source(self,*,command_root,current_steps,run_root=None):
        if self.history_source is not None:raise ContractError('Runtime history owner is already bound')
        self.history_source=RuntimeObservedHistory(run_id=self.context['run_id'],produce_id=self.context['produce_id'],
            idol_card_id=self.context['idol_card_id'],command_root=command_root,current_steps=current_steps,run_root=run_root)

    def choose(self,native,*,operation_context=None):
        if getattr(self,'public_reference_material_policy',None) is not None:
            from .run_identity import load_run
            from .runtime_live_exam_input import public_reference_material_owner
            run=load_run(self.context['run_id'])
            binding=run.evidence.get('public_reference_materials')
            if (not isinstance(binding,Mapping)or binding.get('policy')!=self.public_reference_material_policy
                    or binding.get('run_id')not in(None,self.context['run_id'])):
                raise ContractError('Public outer materials differ from the normal run or fixed release')
            binding={**deepcopy(binding),'run_id':self.context['run_id']}
            owner=public_reference_material_owner(binding,
                **{key:self.context[key]for key in('run_id','idol_card_id','produce_id')})
            if native.session_generation!=owner['session_generation']:
                raise ContractError('Outer observation belongs to another public material session')
            self.public_reference_materials=binding
        if getattr(self,'reference_trial',None) is not None:
            from .runtime_live_exam_input import runtime_reference_trial_owner
            owner=runtime_reference_trial_owner(self.reference_trial,
                **{key:self.context[key]for key in ('run_id','idol_card_id','produce_id')})
            if native.session_generation!=owner['session_generation']:
                raise ContractError('Outer observation belongs to another reference-trial session')
        raw=native.raw
        if not requires_observed_outer_comparison(raw):return None
        if self._continuation is None and raw.get('surface')=='reward_drink_capacity'and (raw.get('ui_state')or{}).get('phase')=='confirm_skip':
            recover=getattr(self.history_source,'latest_settled_skip',None)
            retained=recover(native,self.context)if callable(recover)else None
            if retained is not None:
                from .runtime_reward_drink_capacity import validate_runtime_reward_drink_capacity
                checked=validate_runtime_reward_drink_capacity(retained['before'],skip_intent=True)
                if checked is None or checked['phase']!='reward_full':
                    raise ContractError('Recorded actor skip lacks its original full reward authority')
                self._continuation={**retained,'resource_identity':self._resource_identity(retained['before']),
                    'reward_capacity_identity':{key:deepcopy(checked['state'][key])for key in checked['identity_keys']}}
        continuation=self._complete_selected_intent(native,operation_context)
        if continuation is not None:return continuation
        mechanical=self._economy_confirmation(native,operation_context)
        if mechanical is not None:return mechanical
        self._card_proposal=self._confirmed_card=None
        self._purchase_proposal=self._confirmed_purchase=None
        metadata={**self.identity,'source':'shared-observed-outer-actor','model_applied':False,'legacy_rule_fallback':False}
        if self.cancelled():return None,{**metadata,'status':'cancelled','reason':'observed actor cancelled'}
        if (raw.get('busy')is not False or raw.get('actions_complete')is not True or raw.get('exam_continuation')is True):
            raise ContractError('Observed actor requires settled non-exam native owner')
        if ((raw.get('state')or{}).get('produce_id')!=self.context['produce_id']or
                (raw.get('progress')or{}).get('idolCardId')!=self.context['idol_card_id']):
            raise ContractError('Observed actor run mode/idol differs')
        pending=_business_controls_pending(raw)
        if pending:return None,{**metadata,'status':'waiting','reason':'waiting for complete observed business callbacks','waiting_for':pending}
        actions=native.actions
        if list(actions)!=raw.get('legal_actions')or not actions or len({digest(x['target'])for x in actions})!=len(actions):
            raise ContractError('Observed actor needs the exact complete current native target set')
        unsupported=[x['target']for x in actions if x['action_id']not in _STRATEGIC_ACTIONS|_MECHANICAL_ACTIONS and
            not(x['action_id']=='ui.navigation'and x['target'].get('button_id')=='schedule.refresh')]
        unsupported.extend(x['target']for x in actions if x['action_id']in('shop.buy','shop.open_product','interval.execute','interval.open_product')
            and x['target'].get('resource_type')not in(1,3,5,997,998))
        if unsupported:return None,{**metadata,'status':'model-unavailable','reason':'unqualified observed action controls',
            'unavailable':unsupported}
        snapshot=digest(raw);quote=digest(operation_context)
        if raw.get('surface')=='drink_inventory':
            from .runtime_drink_choice import validate_runtime_drink_inventory
            validate_runtime_drink_inventory(native)
        if raw.get('surface')=='reward_drink_capacity':
            from .runtime_reward_drink_capacity import validate_runtime_reward_drink_capacity
            validate_runtime_reward_drink_capacity(native)
        self.validate_model(self.model);self.material.validate_unchanged()
        if not callable(self.history_source):raise ContractError('Existing ordered runtime history source is required')
        history=self.history_source(native)
        if history.get('history_source')!='existing-native-segments-and-settled-callback-journal':
            raise ContractError('Unqualified runtime history source')
        row={'schema':'gkms.observed-outer-transition.v1','objective':{'id':OUTER_OBJECTIVE},
            'action_form':'recorded-native-target','observation':raw,'decision_kind':'outer-native-control',
            'observed_history':history['observed_history'],'provenance':{
                'trajectory_id':self.context['run_id'],'session_generation':native.session_generation,
                'original_run_step_ordinal':history['original_run_step_ordinal'],
                'observed_asset_preflights':history['observed_asset_preflights']}}
        if getattr(self,'reference_trial',None) is not None:
            row['provenance'].update(runtime_reference_trial=deepcopy(self.reference_trial),
                training_admitted=False,source_equivalence_verified=False,reference_lookup_only=True)
        if getattr(self,'public_reference_materials',None) is not None:
            row['provenance'].update(public_reference_materials=deepcopy(self.public_reference_materials),
                training_admitted=False,source_equivalence_verified=False,reference_lookup_only=True)
        projected=project_fine_observation(row,self.model.schema,semantic_features=self.material,
            scope_context=self.material.scope_context(raw))
        batch,targets=batch_from_projected([projected],self.model.schema,device=next(self.model.parameters()).device)
        with torch.inference_mode():
            logits,mask=self.model.decoder_logits(batch)
            index=int(logits[0].argmax().item())
        self.validate_model(self.model);self.material.validate_unchanged()
        if self.cancelled():return None,{**metadata,'status':'cancelled','reason':'observed actor cancelled'}
        if digest(raw)!=snapshot or digest(operation_context)!=quote:
            raise ContractError('Native snapshot/quote changed during observed actor inference')
        if index>=len(targets[0])or not mask[0,index]:raise ContractError('Actor selected outside actual legal controls')
        target=deepcopy(targets[0][index]);ranking={'candidate_id':digest(target),'actor_logit':float(logits[0,index])}
        self._remember_card_proposal(native,target,ranking,operation_context)
        self._remember_purchase_proposal(native,target,ranking)
        self._remember_continuation(native,target,ranking)
        self._finish_proposal=({'target':deepcopy(target),'revision':native.revision,
            'session_generation':native.session_generation,**ranking}if target['action_id']in('shop.finish','interval.finish')else None)
        return target,{**metadata,'status':'ready','model_applied':True,**ranking,
            'reason':'fixed shared actor chose the current observed legal callback','score_units':'actor-logit',
            'model_rankings':[{'candidate_id':digest(value),'native_target':deepcopy(value),
                'actor_logit':float(logits[0,i])}for i,value in enumerate(targets[0])],
            'observation':{'session_generation':native.session_generation,'revision':native.revision,
                'snapshot_sha256':snapshot,'operation_context_sha256':quote},
            'history_source':history['history_source'],'history_boundary_ordinal':history['original_run_step_ordinal']}

    def observe_settled(self,native,target,outcome,operation_context):
        super().observe_settled(native,target,outcome,operation_context)
        proposal=self._continuation_proposal;self._continuation_proposal=None
        if proposal is not None:
            self._continuation=None
            reader=getattr(self.history_source,'settled_control',None)
            if (callable(reader)and target==proposal['target']and native.revision==proposal['revision']
                    and native.session_generation==proposal['session_generation']):
                settled=reader(native,target,outcome)
                if settled is not None:self._continuation={**proposal,**settled}
        proposal=self._finish_proposal
        if proposal is not None:
            self._confirmed_finish=None;context=operation_context or{}
            if (outcome.status=='settled'and target==proposal['target']and context.get('origin_target')==target
                    and context.get('request_id')==outcome.request_id and context.get('source_revision')==proposal['revision']
                    and context.get('session_generation')==proposal['session_generation']and self._quote_identity(context)is not None):
                self._confirmed_finish={**proposal,'quote_identity':self._quote_identity(context)}
            self._finish_proposal=None

    def _remember_continuation(self,native,target,ranking):
        names={'business.choose','reward.select','reward.skip','reward.confirm_skip','card_choice.select','drink_choice.toggle','drink_choice.confirm',
            'customize.select_card','customize.select_option','reward.drink_capacity_open','reward.drink_capacity_trash',
            'shop.select','interval.select'}
        self._continuation=None
        self._continuation_proposal=({'target':deepcopy(target),'revision':native.revision,
            'session_generation':native.session_generation,'resource_identity':self._resource_identity(native.raw),
            'actor_logit':ranking['actor_logit'],'candidate_id':ranking['candidate_id']}if target['action_id']in names else None)
        if (target['action_id']in ('reward.skip','reward.confirm_skip')
                and (native.raw.get('ui_state')or{}).get('drink_capacity')is not None):
            from .runtime_reward_drink_capacity import validate_runtime_reward_drink_capacity
            checked=validate_runtime_reward_drink_capacity(native,skip_intent=True)
            if checked is not None and checked['phase']=='reward_full':
                self._continuation_proposal['reward_capacity_identity']={
                    key:deepcopy(checked['state'][key])for key in checked['identity_keys']}

    def _complete_selected_intent(self,native,context):
        intent=self._continuation
        if intent is None:return None
        raw=native.raw;after=intent['after'];ui=raw.get('ui_state')or{};chosen=intent['target'];name=chosen['action_id']
        if (native.session_generation!=intent['session_generation']or raw.get('surface')!=after.get('surface')
                or self._resource_identity(raw)!=intent['resource_identity']):
            self._continuation=None;return None
        if name not in('drink_choice.toggle','drink_choice.confirm','reward.drink_capacity_open','reward.drink_capacity_trash')and raw.get('screen_instance_id')!=after.get('screen_instance_id'):
            self._continuation=None;return None
        metadata={**self.identity,'source':'shared-observed-outer-actor','status':'ready','model_applied':True,
            'mechanical_continuation':True,'legacy_rule_fallback':False,'actor_logit':intent['actor_logit'],
            'candidate_id':intent['candidate_id'],'settled_selection_receipt':intent['source'],
            'reason':'complete the same source-bound settled actor selection'}
        if self.cancelled():return None,{**metadata,'status':'cancelled','model_applied':False}
        self.validate_model(self.model)
        if raw.get('busy')is not False or raw.get('actions_complete')is not True:
            return None,{**metadata,'status':'waiting','model_applied':False}
        keys=();expected=None
        if name=='business.choose':
            keys=('index','business_type','business_number','folder_instance_id');expected='business.start'
            folders=[x for x in ui.get('folders',[])if all(x.get(k)==chosen.get(k)for k in('index','business_type','business_number'))]
            if len(folders)!=1 or folders[0].get('selected')is not True:
                self._continuation=None;return None
        elif name=='reward.select':
            keys=('index','instance_key','resource_id');expected='reward.receive'
            if ui.get('selected_index')!=chosen.get('index'):
                self._continuation=None;return None
            capacity=ui.get('drink_capacity')or{}
            if capacity.get('phase')=='reward_full'and capacity.get('is_drink_max')is True:
                from .runtime_reward_drink_capacity import validate_runtime_reward_drink_capacity
                checked=validate_runtime_reward_drink_capacity(native)
                prior=(after.get('ui_state')or{}).get('drink_capacity')or{}
                if (checked is None or checked['phase']!='reward_full'
                        or capacity['selected_reward_index']!=chosen['index']
                        or capacity['selected_reward_id']!=chosen.get('resource_id')
                        or chosen.get('instance_key')!=capacity['reward_view_instance_id']+':'+str(chosen['index'])
                        or any(prior.get(key)!=capacity[key]for key in checked['identity_keys'])):
                    raise ContractError('Settled reward capacity differs from the selected actor reward/pool')
                # Receive is deliberately disabled at capacity. The newly
                # offered discard/skip controls need a fresh actor decision
                # over the complete current pool, never a forced discard.
                self._continuation=None;return None
        elif name in('reward.skip','reward.confirm_skip'):
            identity=intent.get('reward_capacity_identity')
            if identity is None:
                self._continuation=None;return None
            from .runtime_reward_drink_capacity import validate_runtime_reward_drink_capacity
            if raw.get('surface')=='reward_drink_capacity':
                checked=validate_runtime_reward_drink_capacity(native)
                if checked is None or checked['phase']!='confirm_skip' or 'skip_result'not in checked:
                    raise ContractError('Settled drink skip has no bound native confirmation')
                state=checked['state'];expected='reward.drink_capacity_confirm_skip'
            else:
                state=ui.get('drink_capacity')or{}
                if (state.get('phase')!='reward_full' or ui.get('select_status')!=2
                        or state.get('select_status')!=2):
                    raise ContractError('Settled drink skip is not the native selected-skip state')
                expected='reward.confirm_skip'
            if any(type(state.get(key))is not type(value)or state.get(key)!=value for key,value in identity.items()):
                raise ContractError('Settled drink skip belongs to another reward, pool or inventory')
            keys=tuple(identity)
        elif name=='card_choice.select':
            maximum=ui.get('maximum')
            if type(maximum)is not int or maximum<1 or ui.get('selected_count')!=maximum:return None
            keys=('parent_instance_id','parent_screen_type','selector_type');expected='card_choice.confirm'
        elif name=='customize.select_card':
            keys=('deck_number','selector_type');expected='customize.open_options'
            if (ui.get('selected_card')or{}).get('deck_number')!=chosen.get('deck_number'):
                self._continuation=None;return None
        elif name in('shop.select','interval.select'):
            from .runtime_economy_policy import _quote
            products=[row for row in ui.get('products',[])if all(chosen.get(k)==v for k,v in _quote(row,raw['surface']).items())]
            if (len(products)!=1 or products[0].get('selected')is not True or products[0].get('eligible')is not True
                    or products[0].get('direct_open')is not False):
                self._continuation=None;return None
            keys=tuple(_quote(products[0],raw['surface']))
            expected='shop.buy'if name=='shop.select'else'interval.execute'
        elif name=='customize.select_option':
            keys=('deck_number','selector_type','customize_id');expected='customize.execute'
            selected=[row for row in ui.get('customizes',[])if row.get('customize_id')==chosen.get('customize_id')
                and row.get('customize_count')==chosen.get('customize_count')and row.get('selected')is True]
            original=[row for row in(after.get('ui_state')or{}).get('customizes',[])if row.get('customize_id')==chosen.get('customize_id')
                and row.get('customize_count')==chosen.get('customize_count')and row.get('selected')is True]
            if len(selected)!=1 or len(original)!=1 or selected!=original:
                self._continuation=None;return None
            price=selected[0].get('produce_points');wallet=(raw.get('state')or{}).get('produce_points')
            if type(price)is not int or price<0 or type(wallet)is not int or price>wallet:
                raise ContractError('Settled actor customization has no fresh affordable native price')
        elif name in('reward.drink_capacity_open','reward.drink_capacity_trash'):
            from .runtime_reward_drink_capacity import validate_runtime_reward_drink_capacity
            checked=validate_runtime_reward_drink_capacity(native)
            if checked is None or 'skip_result'in checked:raise ContractError('Settled discard intent has no complete current capacity binding')
            keys=('parent_instance_id','inventory_fingerprint','owned_index','drink_id','selected_reward_id',
                'selected_reward_index','reward_pool_fingerprint','limit_count')
            expected='reward.drink_capacity_trash'if checked['phase']=='drink_detail'else'reward.drink_capacity_confirm'
        elif name in('drink_choice.toggle','drink_choice.confirm'):
            from .runtime_drink_choice import validate_runtime_drink_inventory
            checked=validate_runtime_drink_inventory(native)
            if checked is None or len(checked['selected'])!=checked['limit']:return None
            if checked['fingerprint']!=(after.get('ui_state')or{}).get('selection_fingerprint'):
                self._continuation=None;return None
            expected='drink_choice.confirm_skip_new'if checked['phase']=='confirm_no_new_drinks'else'drink_choice.confirm'
            keys=('parent_instance_id',)
        matches=[row['target']for row in native.actions if row['action_id']==expected and
            all(k in chosen and row['target'].get(k)==chosen[k]for k in keys)]
        if len(matches)>1:raise ContractError('Settled actor selection has ambiguous continuation callbacks')
        if not matches:return None,{**metadata,'status':'waiting','model_applied':False,'reason':'waiting for the selected callback continuation'}
        target=deepcopy(matches[0])
        if name=='card_choice.select'and target.get('selected_count')!=ui['selected_count']:
            raise ContractError('Card commit differs from the settled selected cardinality')
        self._remember_card_proposal(native,target,metadata,context)
        self._remember_purchase_proposal(native,target,metadata)
        self._remember_continuation(native,target,metadata)
        if name in('reward.skip','reward.confirm_skip')and self._continuation_proposal is not None:
            self._continuation_proposal['reward_capacity_identity']=deepcopy(intent['reward_capacity_identity'])
        return target,metadata

    def _economy_confirmation(self,native,context):
        raw=native.raw;phase=(raw.get('ui_state')or{}).get('phase')
        if raw.get('surface')not in('shop','interval')or phase not in('confirm_purchase','confirm_finish'):return None
        metadata={**self.identity,'source':'shared-observed-outer-actor','model_applied':False,'legacy_rule_fallback':False}
        if self.cancelled():return None,{**metadata,'status':'cancelled'}
        self.validate_model(self.model)
        # The existing economy validator owns parent/quote/selection/payment
        # checks. Its explicit learned hook avoids all old utility calculation.
        from .runtime_economy_policy import choose_runtime_economy_action
        if phase=='confirm_finish'and(self._confirmed_finish is None or
                self._quote_identity(context)!=self._confirmed_finish['quote_identity']):
            return None,{**metadata,'status':'model-unavailable','reason':'finish lacks the settled actor intent'}
        target,detail=choose_runtime_economy_action(native,deck_context={},operation_context=context,
            learned_confirmation=self.approve_economy_confirmation)
        if target is None or not target['action_id'].endswith(('confirm_operation','confirm_finish')):
            return None,{**metadata,'status':'model-unavailable','reason':detail.get('reason','confirmation lacks bound actor intent'),
                'unavailable':[{'reason':'observed-actor-confirmation-not-source-bound'}]}
        approval=detail.get('learned_confirmation',self._confirmed_finish or{})
        return target,{**detail,**metadata,'status':'ready','model_applied':True,'mechanical_continuation':True,
            'reason':'confirm the unchanged quote of the settled actor choice',
            **{key:approval[key]for key in('actor_logit','candidate_id','ranking_basis')if key in approval}}

