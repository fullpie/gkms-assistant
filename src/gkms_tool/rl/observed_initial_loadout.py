"""Observed pre-start equipment, through the existing source-bound semantic codec.

Only selected support slots, inherited memory choices and recorded idol ranks
are read. No final Produce deck/statistics/new memory, future rewards, current
trigger counters, or estimated loadout effects are reconstructed.
"""
from collections.abc import Mapping
from copy import deepcopy
import hashlib,json,re
from pathlib import Path

from .contracts import ContractError,digest
from .semantic_entity_encoding import encode_semantic_tokens,ENTITY_FEATURE_NAMES
from ..qualification_verification import source_stamp

SCHEMA='gkms.observed-initial-loadout.v1'
SOURCE_SCHEMA='gkms.observed-initial-loadout-source.v1'
IMPLEMENTATION_MODULES=('gkms_tool.rl.observed_initial_loadout','gkms_tool.passive_catalog')


def recorded_initial_loadout(history):
    """Same RAW inventory whitelist used by rl_loadout_reference.baseline_inventory."""
    supports=history.get('deckSupportCards');memories=history.get('deckMemories')
    if supports is not None and(not isinstance(supports,list)or len(supports)!=6):
        raise ContractError('Observed initial support inventory must preserve six slots')
    if memories is not None and(not isinstance(memories,list)or len(memories)!=4):
        raise ContractError('Observed initial memory inventory must preserve four slots')
    selected=[]
    for wrapper in memories or[]:
        row=wrapper.get('memory',wrapper)
        current={key:deepcopy(row.get(key))for key in('idolCardId','planType','produceCardPhaseType')}
        card=row.get('produceCard')
        current['produceCard']=None if card is None else{key:deepcopy(card[key])for key in('id','upgradeCount')if key in card}
        if card is not None and 'customizes'in card:
            current['produceCard']['customizes']=[{key:deepcopy(value[key])for key in('id','customizeCount')if key in value}for value in card['customizes']]
        current['abilities']=None if row.get('abilities')is None else[{key:deepcopy(value[key])for key in('id','level')if key in value}for value in row['abilities']]
        selected.append(current)
    return {'idol':{key:deepcopy(history.get(key))for key in('idolCardId','levelLimitRank','potentialRank')},
        'supports':None if supports is None else[{key:deepcopy(row.get(key))for key in('id','level','levelLimitRank')}for row in supports],
        'memories':None if memories is None else selected}


def _rank(value):
    if value is None:return None
    if type(value)is int and value>=0:return value
    found=re.fullmatch(r'(?:IdolCardLevelLimitRank|IdolCardPotentialRank)_{1,2}(\d+)',str(value))
    if found:return int(found[1])
    raise ContractError('Observed idol rank is not the original explicit enum')


def project_initial_loadout(history,source=None,*,source_master_hash=None):
    from .observed_outer_fine_projection import _tokens,_card_tokens
    if source is None:
        return _project_recorded_initial_loadout(history,source_master_hash)
    source.validate_unchanged();recorded=recorded_initial_loadout(history);entities=[]
    def add(kind,tokens):
        entities.append({'type':kind,'zone':'outer_current','definition_id':'observed-initial-'+kind,
            'count':1,'values':encode_semantic_tokens(['outer.inventory_scope:str:recorded-initial-loadout',*tokens])})
    missing={'supports':recorded['supports']is None,'memories':recorded['memories']is None,
        'idol_level_rank':recorded['idol']['levelLimitRank']is None,'idol_potential_rank':recorded['idol']['potentialRank']is None,
        'current_trigger_counts':True}
    idol=recorded['idol'];definition=source._one('IdolCard.yaml',idol['idolCardId'])
    # Exact selected identity/ranks and static base; no summed parameter estimate.
    tokens=_tokens('outer.initial.idol',idol)+_tokens('outer.initial.missing',missing)
    tokens+=_tokens('outer.initial.idol.base',{k:v for k,v in definition.items()if k.startswith('produce')})
    rank_definitions=[]
    for table,key,field in(('IdolCardLevelLimitStatusUp.yaml','idolCardLevelLimitStatusUpId','levelLimitRank'),
                          ('IdolCardPotential.yaml','idolCardPotentialId','potentialRank')):
        rank=_rank(idol[field])
        if rank is not None:
            for effect in source._table_rows(table):
                if effect.get('id')==definition[key]and _rank(effect['rank'])<=rank:
                    rank_definitions.append(_tokens('outer.initial.idol.rank_family',table)+_tokens('outer.initial.idol.rank_definition',effect))
    add('outer_context',tokens)
    for tokens in rank_definitions:add('context',tokens)
    catalog=source.initial_passive_catalog()
    for support in recorded['supports']or[]:
        if not isinstance(support['id'],str)or type(support['level'])is not int or support['level']<1:
            raise ContractError('Observed initial support identity/level missing')
        add('support',_tokens('outer.support.current',{'supportCardId':support['id'],'level':support['level'],
            'levelLimitRank':support['levelLimitRank']}))
        for passive in catalog.resolve_support_card(support['id'],support['level']):
            if not passive.skill_id or type(passive.skill_level)is not int:
                raise ContractError('Initial support skill level lacks exact source definition')
            skill={'id':passive.skill_id,'level':passive.skill_level}
            add('context',_tokens('outer.support_skill.current',skill)+source.skill_tokens(skill,'outer.support_skill'))
    for memory in recorded['memories']or[]:
        if memory['abilities']is None:raise ContractError('Initial equipped memory abilities are missing')
        add('context',_tokens('outer.initial.memory',{k:v for k,v in memory.items()if k!='produceCard'}))
        for ability in memory['abilities']:
            definition=source._one('MemoryAbility.yaml',ability['id'],level=ability['level'])
            skill={'id':definition['skillId'],'level':ability['level']}
            add('context',_tokens('outer.memory_ability.current',ability)+_tokens('outer.memory_ability.definition',definition)+
                source.skill_tokens(skill,'outer.memory_ability.skill'))
        card=memory['produceCard']
        if card is not None:
            if not isinstance(card,Mapping)or not isinstance(card.get('id'),str):raise ContractError('Initial inherited card is not a source card')
            upgrade=card.get('upgradeCount',0)
            normalized={'card_id':card['id'],'upgrade':upgrade}
            static=source.payload['cards'].get(card['id']+'@'+str(upgrade))
            if not isinstance(static,Mapping):raise ContractError('Initial inherited card base is not in its exact Master')
            ids=static.get('customize_ids',[]);counts=[0]*len(ids)
            for customize in card.get('customizes',[]):
                if customize['id']not in ids or type(customize['customizeCount'])is not int or customize['customizeCount']<1:
                    raise ContractError('Initial inherited customization lacks exact source mapping')
                counts[ids.index(customize['id'])]=customize['customizeCount']
            normalized['customizeCountList']=counts
            add('card',_tokens('outer.initial.inherited_card',{'phase':memory['produceCardPhaseType'],
                'current_owned':None,'upgradeCount_present':'upgradeCount'in card})+_card_tokens(normalized,source))
    source.validate_unchanged()
    return {'schema':SCHEMA,'observed':recorded,'missing':missing,'entities':entities,
        'source_binding':deepcopy(source.initial_binding),'observed_identity':digest(recorded),
        'semantic_identity':digest(entities),'current_inventory_claimed':False,'future_effects_estimated':False}


def _project_recorded_initial_loadout(history,source_master_hash):
    """Keep exact equipped facts when no qualified first-Master enrichment exists."""
    from .observed_outer_fine_projection import _tokens
    from ..deck_plan import _cards
    if not isinstance(source_master_hash,str)or not source_master_hash:
        raise ContractError('Recorded initial loadout requires its actual first audition Master')
    recorded=recorded_initial_loadout(history);entities=[]
    def add(kind,tokens):
        entities.append({'type':kind,'zone':'outer_current','definition_id':'observed-initial-'+kind,
            'count':1,'values':encode_semantic_tokens(['outer.inventory_scope:str:recorded-initial-loadout',*tokens])})
    missing={'supports':recorded['supports']is None,'memories':recorded['memories']is None,
        'idol_level_rank':recorded['idol']['levelLimitRank']is None,'idol_potential_rank':recorded['idol']['potentialRank']is None,
        'current_trigger_counts':True,'static_enrichment':True}
    for key in ('levelLimitRank','potentialRank'):_rank(recorded['idol'][key])
    add('outer_context',_tokens('outer.initial.idol',recorded['idol'])+_tokens('outer.initial.missing',missing))
    for support in recorded['supports']or[]:
        if not isinstance(support['id'],str)or not support['id']or type(support['level'])is not int or support['level']<1:
            raise ContractError('Observed initial support identity/level missing')
        add('support',_tokens('outer.support.current',{'supportCardId':support['id'],'level':support['level'],
            'levelLimitRank':support['levelLimitRank']}))
    for memory in recorded['memories']or[]:
        add('context',_tokens('outer.initial.memory',{k:v for k,v in memory.items()if k!='produceCard'}))
        for ability in memory['abilities']or[]:
            if not isinstance(ability.get('id'),str)or not ability['id']or type(ability.get('level'))is not int or ability['level']<1:
                raise ContractError('Observed initial memory ability identity/level missing')
            add('context',_tokens('outer.memory_ability.current',ability))
        card=memory['produceCard']
        if card is not None:
            normalized=_cards([card])[0]
            add('card',_tokens('outer.initial.inherited_card',{'phase':memory['produceCardPhaseType'],
                'current_owned':None,'upgradeCount_present':'upgradeCount'in card})+
                _tokens('native.base',{'id':normalized.card_id,'upgradeCount':normalized.upgrade_count})+
                _tokens('outer.initial.inherited_card.recorded_customizes',normalized.to_dict()['customizes']))
    return {'schema':SCHEMA,'observed':recorded,'missing':missing,'entities':entities,
        'source_binding':{'schema':SOURCE_SCHEMA,'source_master_hash':source_master_hash,
            'enrichment':'recorded-facts-only','static_definitions_available':False},
        'observed_identity':digest(recorded),'semantic_identity':digest(entities),
        'current_inventory_claimed':False,'future_effects_estimated':False}


def prepared_initial_entities(value):
    if (not isinstance(value,dict)or value.get('schema')!=SCHEMA or value.get('current_inventory_claimed')is not False
            or value.get('future_effects_estimated')is not False or digest(value.get('observed'))!=value.get('observed_identity')
            or digest(value.get('entities'))!=value.get('semantic_identity')or not value.get('source_binding')):
        raise ContractError('Qualified observed initial loadout derivative differs')
    entities=value['entities']
    if not isinstance(entities,list)or not entities:raise ContractError('Initial loadout has no explicit observed/missing inputs')
    for row in entities:
        if (row.get('type')not in('outer_context','context','support','card')or row.get('zone')!='outer_current'
                or row.get('count')!=1 or set(row.get('values',{}))-set(ENTITY_FEATURE_NAMES)):
            raise ContractError('Initial loadout semantic entity contract differs')
    return deepcopy(entities)


def build_initial_loadout_source(specification):
    """Use verified historical/current packages, all under their actual Master."""
    from ..artifact_source_resolver import ArtifactSourceResolver
    from ..runtime_optional_parent_features import RuntimeOptionalParentCardFeatures
    from ..native_structure_features import load_native_structure_package
    from ..runtime_master_source import load_runtime_master_source
    from .observed_outer_fine_projection import FineOuterSemanticSource
    spec=deepcopy(specification)
    if (spec.get('schema')!=SOURCE_SCHEMA or set(spec)-{'schema','historical_master_manifest','runtime_master_source','contract_set','source_relocations'}
            or bool(spec.get('runtime_master_source'))==bool(spec.get('contract_set'))):
        raise ContractError('Explicit unambiguous initial-loadout material source required')
    watches={}
    def read(ref):
        path=Path(ref['path']).resolve();before=source_stamp(path);raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=ref['sha256']or source_stamp(path)!=before:raise ContractError('Initial material source changed')
        watches[path]=before;return json.loads(raw)
    original=read(spec['historical_master_manifest']);master=original['archived_master_hash']
    archive_ref=original['archived_master'];archive=read(archive_ref)
    if archive['master_hash']!=master:raise ContractError('Initial supplemental tables use a different Master')
    resolver=ArtifactSourceResolver(spec['source_relocations'])if spec.get('source_relocations')else None
    if spec.get('runtime_master_source'):
        runtime=load_runtime_master_source(spec['runtime_master_source']);package=runtime.package(source_resolver=resolver)
    else:
        document=read(spec['contract_set']);matches=[v['native_structure_contract']for v in document['contracts'].values()
            if v['native_structure_contract']['source_master_hash']==master]
        if len(matches)!=1:raise ContractError('No unique exact historical initial-loadout material contract')
        native=matches[0];runtime=None
        package=load_native_structure_package(source_master_hash=master,source_resolver=resolver,
            **{key:native[key]for key in('source_inventory_reference','native_schema_reference','catalog_reference','snapshot_reference')})
    if (package.contract['source_master_hash']!=master or
            package.contract['source_archive_manifest_reference']['sha256']!=archive_ref['sha256']):
        raise ContractError('Initial material package/archive differs from historical observation')
    def referenced_paths(value):
        if isinstance(value,dict):
            if isinstance(value.get('path'),str)and isinstance(value.get('sha256'),str):yield Path(value['path']).resolve()
            for child in value.values():yield from referenced_paths(child)
        elif isinstance(value,(list,tuple)):
            for child in value:yield from referenced_paths(child)
    for path in referenced_paths(package.contract):watches[path]=source_stamp(path)
    def validate():
        if runtime is not None:runtime.validate_unchanged()
        if resolver is not None:resolver.validate_unchanged()
        if any(source_stamp(path)!=stamp for path,stamp in watches.items()):raise ContractError('Initial source material changed')
    class Source(FineOuterSemanticSource):
        def _table_rows(self,name):
            reference=self.manifest.get('tables',{}).get(name)
            if reference is not None and 'bytes'not in reference:
                # Original archives carry exact hashes, not portable byte
                # lengths. Only the requested table is stat'ed; the existing
                # loader still verifies its authoritative SHA before decoding.
                reference['bytes']=(self.root/reference['path']).stat().st_size
            return super()._table_rows(name)
        def qualified_source_paths(self):
            self.validate_unchanged()
            return tuple(dict.fromkeys([*watches,*self._watches]))
        def initial_passive_catalog(self):
            from ..passive_catalog import MasterPassiveCatalog,SUPPORT_LEVEL_TABLES
            if not hasattr(self,'_initial_catalog'):
                self._initial_catalog=MasterPassiveCatalog(memory_gifts=[],memory_abilities=self._table_rows('MemoryAbility.yaml'),
                    support_cards=self._table_rows('SupportCard.yaml'),
                    support_skill_levels={key:self._table_rows(name)for key,name in SUPPORT_LEVEL_TABLES.items()},
                    produce_skills=self._table_rows('ProduceSkill.yaml'),produce_effects=self._table_rows('ProduceEffect.yaml'),
                    produce_triggers=self._table_rows('ProduceTrigger.yaml'))
            return self._initial_catalog
    source=Source(RuntimeOptionalParentCardFeatures(package),archive_ref,validate_materials=validate)
    # Adapt only the source manifest layout; these hashes come from the verified
    # original archive, not a fabricated current-Master material identity.
    source.manifest={'master_hash':master,'tables':{name:{'path':'yaml/'+name,'sha256':sha}
        for name,sha in archive['yaml_sha256'].items()}}
    source.master_dir=source.root/'yaml'
    source.initial_binding={'schema':SOURCE_SCHEMA,'specification':spec,'source_master_hash':master,
        'semantic_package':package.contract['contract_sha256'],'archive':archive_ref}
    source.validate_unchanged();return source
