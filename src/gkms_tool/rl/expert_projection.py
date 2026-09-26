"""Original-version expert input into the same public entity representation.

No owned-policy envelope, current-Master alias or fabricated native input is
created. The expert reader/legacy adapter owns source and episode qualification.
"""
from dataclasses import asdict
import hashlib
from pathlib import Path
from types import SimpleNamespace

from .contracts import ContractError, DecisionKind, FrozenJSON, Scope, State, digest
from .game_projection import feature_schema, default_routes, project_prepared_information
from ..observed_empty_collections import PC_SOURCE, JSON_SOURCE
from ..original_pc_primary_input import prepare_original_pc_primary
from ..native_secondary_input import prepare_native_secondary_input
from ..native_secondary_binding import load_verified_secondary_command_bindings
from ..integrated_exam_bc_features import IntegratedExamFeatureEncoder
from ..runtime_loader_compatibility import load_runtime_loader_compatibility
from ..runtime_optional_parent_features import RuntimeOptionalParentCardFeatures

VERSION = 'gkms.rl.expert-public-semantics.v1'
OBJECTIVE = 'remaining_return_iql_v1'
ALIASES = {'score':'parameter', 'current_turn':'currentTurn', 'remain_turn':'remainTurn',
    'max_stamina':'maxStamina', 'turn_card_play_count':'turnCardPlayCount',
    'exam_card_play_count':'examCardPlayCount', 'is_turn_card_play_end':'isTurnCardPlayEnd',
    'step_type_value':'stepType', 'random_state':'random'}


def original_native_state(value):
    """Drop only redundant, equal legacy aliases; never fill native values."""
    raw = dict(value)
    for alias, native in ALIASES.items():
        if alias in raw:
            if native not in raw or type(raw[alias]) is not type(raw[native]) or raw[alias] != raw[native]:
                raise ContractError('Legacy/native state alias differs: ' + alias)
            del raw[alias]
    return raw


def _scope(raw, run, exam):
    routes=default_routes()
    def find(family, values):
        found=[row[-1] for row in routes[family] if row[:-1]==values]
        if len(found)!=1:raise ContractError('Unrepresented expert flow/mode/stage: '+family)
        return found[0]
    return Scope(run,exam,find('modes',[raw['produceId']]),find('flows',[raw['planType'],raw['mainEffectType']]),
                 find('stages',[raw['stepType']]))


class ExpertSemanticProjector:
    def __init__(self, sources, *, secondary_binding_reference=None, max_entities=256):
        if set(sources)!={'contract_set','original_shared_encoder','loader_compatibility'}:
            raise ContractError('Original historical semantic sources are required')
        self.sources=FrozenJSON.of(sources)
        self.schema=feature_schema(max_entities=max_entities)
        self.compatibility=load_runtime_loader_compatibility(sources['loader_compatibility'])
        self.encoder=IntegratedExamFeatureEncoder(sources['contract_set'],sources['original_shared_encoder'],
            source_resolver=self.compatibility.make_source_resolver(),loader_compatibility=self.compatibility)
        self.secondary_bindings=(load_verified_secondary_command_bindings(secondary_binding_reference)
                                 if secondary_binding_reference is not None else None)
        self._historical_features={}
        root=Path(__file__).parent
        code={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in (
            'expert_projection.py','game_projection.py','native_information_view.py','semantic_entity_encoding.py')}
        self.identity=digest({'schema':VERSION,'features':asdict(self.schema),'sources':sources,'implementation':code})

    def _project(self, raw, primary, secondary, *, provenance, group, episode, source_identity, source_kind):
        self.compatibility.validate_unchanged()
        master=provenance['source_master_hash']
        features,contract=self.encoder._route(master,raw['produceId'])
        if master not in self._historical_features:
            self._historical_features[master]=RuntimeOptionalParentCardFeatures(features.package,
                active_status_contract=contract.get('observed_active_status_contract'))
        features=self._historical_features[master]
        package=features.package.contract
        legacy=source_kind==JSON_SOURCE
        if (provenance.get('metadata_sha256')!=package['native_metadata_sha256'] or
                not legacy and provenance.get('engine_id')!=package['native_core_sha256']):
            raise ContractError('Expert source engine/metadata differs from its semantic package')
        if legacy:
            reference=provenance.get('source_contract_reference')
            if provenance.get('engine_id') is not None or not isinstance(reference,dict):
                raise ContractError('Legacy JSON requires its actual typed source contract and unknown image hash')
            from .contracts import sha256
            sha256(reference.get('sha256'),'legacy typed source contract')
            path=Path(reference['path'])
            if hashlib.sha256(path.read_bytes()).hexdigest()!=reference['sha256']:
                raise ContractError('Legacy typed source contract changed')
        information,detail=project_prepared_information(raw,features,contract,self.schema,
            primary=primary,secondary=secondary,source_kind=source_kind)
        binding={'source_master_hash':master,'engine_id':provenance['engine_id'],
                 'metadata_sha256':provenance['metadata_sha256'],'projection_id':self.identity,
                 'feature_schema_sha256':self.schema.identity,'objective_id':OBJECTIVE,
                 'source_kind':source_kind,'label_family':'expert-off-policy'}
        if legacy:
            binding.update(engine_identity_kind='recorded-native-json-contract',
                source_contract_sha256=provenance['source_contract_reference']['sha256'],
                semantic_package_core_sha256=package['native_core_sha256'],execution_master_hash=None)
        kind=DecisionKind.MAIN if primary is not None else DecisionKind.SECONDARY
        state=State(_scope(raw,group,episode),'recorded-expert',source_identity,kind,information,digest(binding))
        return {'state':state,'candidates':detail['candidates'],'constraints':detail['constraints'],
                'source_binding':binding,'audit':{'source_identity':source_identity,'projection_id':self.identity,
                    'source_master_preserved':True,'owned_policy_fabricated':False,'source_bytes_unchanged':True,
                    'target_bindings':detail['target_bindings'],'full_markov_state_claimed':False}}

    def expert_event(self,row):
        if (row.get('schema')!='gkms.rl.expert-observation.v1' or row.get('off_policy') is not True
                or row.get('fixed_policy_mc') is not False or row.get('partition') not in ('train','validation')):
            raise ContractError('Explicit qualified expert record required; never test or policy MC')
        event=row['observation'];raw=event['snapshot']['state']
        if row.get('source_master_hash')!=row['provenance'].get('source_master_hash'):
            raise ContractError('Expert row and original Master provenance disagree')
        primary=secondary=None
        if row['decision_type']=='main':
            primary=prepare_original_pc_primary(event)
        elif row['decision_type']=='secondary':
            if self.secondary_bindings is None:raise ContractError('Original secondary command binding is required')
            reference=row['event_reference']
            with Path(reference['path']).open('rb') as stream:
                stream.seek(reference['byte_offset']);payload=stream.read(reference['bytes'])
            if len(payload)!=reference['bytes'] or hashlib.sha256(payload).hexdigest()!=reference['sha256']:
                raise ContractError('Expert selector source span changed')
            import json
            if json.loads(payload)!=event:raise ContractError('Expert selector differs from original event')
            bound=self.secondary_bindings.bind_event(payload,reference)
            secondary=prepare_native_secondary_input(event,current_command_binding=bound)
        else:raise ContractError('Unsupported expert decision kind')
        result=self._project(raw,primary,secondary,provenance=row['provenance'],group=row['source_group'],
            episode=row['episode_id'],source_identity=row['event_reference']['sha256'],source_kind=PC_SOURCE)
        if primary is not None:
            index=row['expert_label']['primary_candidate_index']
            if type(index) is not int or not 0<=index<len(result['candidates']):
                raise ContractError('Expert main candidate index is invalid')
            if result['candidates'][index]['target']!=row['expert_label']['native_action']:
                raise ContractError('Expert main action does not match projected candidate')
            selected=[index]
        else:
            selected=list(row['secondary_input']['ordered_teacher_ordinals'])
            if selected!=row['expert_label']['native_action']['indexes']:
                raise ContractError('Expert secondary ordering changed')
        result['expert_indices']=selected
        return result

    def legacy_main(self,raw_state,legal_candidates,*,native_actions,provenance,group,episode,source_identity,
                    expert_candidate_index=None):
        """Already qualified JSON/native main transitions keep their own route."""
        raw=original_native_state(raw_state)
        primary=SimpleNamespace(legal_candidates=tuple(legal_candidates),original_actions=tuple(native_actions),
                                pre_action_cost=None)
        if not native_actions or not any(a.get('type')==3 and a.get('indexes')==[] for a in native_actions):
            raise ContractError('Complete legacy native main candidates required')
        result=self._project(raw,primary,None,provenance=provenance,group=group,episode=episode,
                             source_identity=source_identity,source_kind=JSON_SOURCE)
        if expert_candidate_index is not None:
            if type(expert_candidate_index) is not int or not 0<=expert_candidate_index<len(native_actions):
                raise ContractError('Legacy expert action not in complete candidate pool')
            result['expert_indices']=[expert_candidate_index]
        return result


__all__=['ExpertSemanticProjector','original_native_state','VERSION','OBJECTIVE']
