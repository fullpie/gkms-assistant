"""Export verified outer rules and existing inference weights, never raw histories."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from gkms_tool.portable_outer_assets import SCHEMA,MASTER_HASH,MODEL_MANIFEST_SHA256

SOURCE_MANIFEST_SHA256='aeb99a1573e83cee243866f0ead382e5665e3e916826a3f0c2ec0db258afbca4'
PROVIDER_SHA256='c2506b738280924c5cbbab813ba9a20382a2d310a2cbdc4b85ee35a53eaa2be7'
TABLES=('ExamSetting','IdolCard','MemoryAbility','MemoryGift','Produce','ProduceCardCustomize','ProduceCardGrowEffect',
 'ProduceCardStatusEnchant','ProduceEffect','ProduceExamBattleConfig','ProduceExamBattleNpcGroup','ProduceExamBattleNpcMob',
 'ProduceExamBattleScoreConfig','ProduceExamGimmickEffectGroup','ProduceExamTrigger','ProduceGroup','ProduceItem','ProduceItemEffect',
 'ProduceLiveEvaluation','ProduceSetting','ProduceSkill','ProduceStepAuditionDifficulty','ProduceStepSelfLesson','ProduceTrigger',
 'Setting','SupportCard','SupportCardProduceSkillLevelAssist','SupportCardProduceSkillLevelDance','SupportCardProduceSkillLevelVisual','SupportCardProduceSkillLevelVocal')


def sha(raw):return hashlib.sha256(raw).hexdigest()
def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def export(source_manifest,provider,output,*,behavior_count_prior,deck_plan_library,behavior_projection_receipt):
    source_manifest,provider,output=map(Path,(source_manifest,provider,output))
    if output.exists():raise ValueError('Preserve previous exports; choose a new directory')
    source_raw=source_manifest.read_bytes();source=json.loads(source_raw)
    if sha(source_raw)!=SOURCE_MANIFEST_SHA256 or source['master_hash']!=MASTER_HASH:
        raise ValueError('Outer Master source identity differs')
    from gkms_tool.runtime_outer_behavior_prior import _load_provider
    original_provider,original_model=_load_provider(provider)
    if sha(provider.read_bytes())!=PROVIDER_SHA256:raise ValueError('Weekly provider is not the qualified existing version')
    directory=provider.parent/original_provider['candidate_directory']
    original_manifest=json.loads((directory/'manifest.json').read_bytes())
    original_report=json.loads((directory/'report.json').read_bytes())
    projection_audit=json.loads(Path(behavior_projection_receipt).read_bytes())
    if projection_audit.get('passed') is not True or projection_audit.get('model_training_performed') is not False:
        raise ValueError('Qualified behavior projection receipt required')
    # Runtime reads only model schema and split coverage. Do not distribute
    # original training inputs, player/trajectory membership or machine paths.
    report={'schema':original_report['schema'],'coverage':deepcopy(original_report['coverage']),
        'projection':'inference-coverage-only; original metrics/history remain retained private evidence'}
    package_files={'weekly_candidate/model.json':(directory/'model.json').read_bytes(),
                   'weekly_candidate/report.json':encoded(report)}
    manifest=deepcopy(original_manifest)
    manifest['files']={name:sha(package_files['weekly_candidate/'+name]) for name in ('model.json','report.json')}
    package_files['weekly_candidate/manifest.json']=encoded(manifest)
    descriptor=deepcopy(original_provider);descriptor['candidate_directory']='weekly_candidate'
    descriptor['files']={name:sha(package_files['weekly_candidate/'+name]) for name in ('model.json','manifest.json','report.json')}
    package_files['weekly_provider.json']=encoded(descriptor)
    for role,path in (('behavior_count_prior',behavior_count_prior),('deck_plan_library',deck_plan_library)):
        raw=Path(path).read_bytes();value=json.loads(raw)
        if not isinstance(value,dict):raise ValueError('Outer projection must be an object: '+role)
        package_files[role+'.json']=raw
    for name,raw in package_files.items():
        if re.search(rb'(?i)[a-z]:[\\/]+(?:Users[\\/]|gkms_tool)',raw):raise ValueError('Private absolute path in public inference projection: '+name)
    output.mkdir(parents=True)
    tables={}
    master=Path(source['master_dir'])
    for name in TABLES:
        path=master/(name+'.yaml');raw=path.read_bytes()
        if sha(raw)!=source['yaml_sha256'][name+'.yaml']:raise ValueError('Verified current Master table changed: '+name)
        relative='Master/'+name+'.yaml';target=output/relative;target.parent.mkdir(exist_ok=True);target.write_bytes(raw)
        tables[name+'.yaml']={'path':relative,'sha256':sha(raw),'bytes':len(raw)}
    for name,raw in package_files.items():
        target=output/name;target.parent.mkdir(exist_ok=True);target.write_bytes(raw)
    moved,_=_load_provider(output/'weekly_provider.json')
    if (json.loads((output/'weekly_candidate/model.json').read_bytes())!=original_model
            or moved['joint_scopes']!=original_provider['joint_scopes'] or moved['candidate_sets']!=original_provider['candidate_sets']
            or report['coverage']!=original_report['coverage']):raise ValueError('Weekly inference representation changed during export')
    roles={'weekly_provider':'weekly_provider.json','weekly_model':'weekly_candidate/model.json',
        'weekly_manifest':'weekly_candidate/manifest.json','weekly_report':'weekly_candidate/report.json',
        'behavior_count_prior':'behavior_count_prior.json','deck_plan_library':'deck_plan_library.json'}
    result={'schema':SCHEMA,'master_hash':MASTER_HASH,'model_manifest_sha256':MODEL_MANIFEST_SHA256,
        'source_manifest_sha256':SOURCE_MANIFEST_SHA256,'upstream_commit':source['upstream_commit'],'produce_ids':['produce-004','produce-005'],
        'tables':tables,'files':{role:{'path':name,'sha256':sha(package_files[name]),'bytes':len(package_files[name])} for role,name in roles.items()},
        'weekly_original_provider_sha256':PROVIDER_SHA256,'weekly_original_files':original_provider['files'],
        'historical_behavior_provenance':{
            'current_runtime_master_is_not_training_source':True,
            'historical_sources_relabelled':False,
            'weekly_input_dataset_digest':original_provider['input_dataset_digest'],
            'weekly_source_versions':original_report['data_audit'].get('versions'),
            'behavior_and_deck_source_version_counts':projection_audit['source_version_counts'],
            'qualification':projection_audit['comparison_context']},
        'weekly_model_bytes_unchanged':True,'weekly_coverage_unchanged':True,'trained_new_parameters':False,'contains_raw_replays':False,
        'memory_loadout_policy':'game-native-auto-recommendation; learned memory prior is not packaged or required',
        'old_development_master_equivalence_claimed':False,
        'scope_note':'Same verified current Master as exam/loadout assets. Older development YAML differs; no retroactive live-equivalence or quality claim.'}
    raw=encoded(result);(output/'manifest.json').write_bytes(raw)
    (output/'NOTICE.txt').write_text('Outer policy static Master inputs from '+source['upstream_url']+' at '+source['upstream_commit']+'.\n'
        'This provenance notice does not grant a separate license for the upstream game data.\n'
        'Existing weekly inference weights/coverage are retained. Behavior/deck references are qualified privacy projections; no raw replays or account inventories are included. Memory selection uses the game recommendation; its learned prior is excluded.\n',encoding='utf-8')
    return {'schema':'gkms.portable-outer-export-receipt.v1','manifest_sha256':sha(raw),'table_count':len(tables),
        'weekly_model_sha256':roles['weekly_model'] and result['files']['weekly_model']['sha256'],'trained_new_parameters':False,
        'scope_note':result['scope_note'],'game_io':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for field in ('source-manifest','provider','output','behavior-count-prior','deck-plan-library','behavior-projection-receipt'):parser.add_argument('--'+field,type=Path,required=True)
    args=parser.parse_args();receipt=export(args.source_manifest,args.provider,args.output,
        behavior_count_prior=args.behavior_count_prior,deck_plan_library=args.deck_plan_library,
        behavior_projection_receipt=args.behavior_projection_receipt)
    print(json.dumps(receipt,ensure_ascii=False))
