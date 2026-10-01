"""Immutable BC-only teachers with separately disclosed evidence-quality tiers.

Complete local replays retain the strict golden criteria. Weak prefixes come
only from unimplemented/unresolved next-command stops, before that failed
command. Known state, slot, score and terminal contradictions exclude the
entire episode. This module does not alter the golden or native loaders.
"""
from __future__ import annotations
import argparse,ast,hashlib,json,math
from collections import Counter,defaultdict
from dataclasses import dataclass
from pathlib import Path

from .fullpower_decision_observation import observation_from_mapping
from .training_artifact_io import sha256_file

ROOT=Path(__file__).resolve().parents[2]
RESEARCH=ROOT/'var/research/fullpower_reconstruction_20260909'
DATA=ROOT/'var/training_dataset/fullpower_replay_bc_v1/episodes.jsonl'
SCHEMA='gkms.fullpower-tiered-teacher-bc-dataset.v1'
ROW_SCHEMA='gkms.simulator-teacher-transition.v1'
SOURCE_SCHEMA='gkms.fullpower-tiered-teacher-source-manifest.v1'
GOLDEN='golden-complete-local-replay'
PREFIX='weak-unverified-simulator-prefix'
WEIGHTS={GOLDEN:1.0,PREFIX:0.25}
SPLITS={'train','validation','test'}


def _canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf8')
def _digest(value):return hashlib.sha256(_canonical(value)).hexdigest()
def _json(raw):
    def unique(pairs):
        out={}
        for key,value in pairs:
            if key in out:raise ValueError('duplicate tiered teacher JSON key:'+key)
            out[key]=value
        return out
    return json.loads(raw,object_pairs_hook=unique)
def _read(path):return _json(Path(path).read_text(encoding='utf8'))
def _rows(path):return [_json(line) for line in Path(path).open(encoding='utf8') if line.strip()]
def _require(value,message):
    if not value:raise ValueError(message)


def episode_quality(original,report,records):
    """Return one disclosed tier or a whole-episode exclusion reason."""
    if report.get('master_access_footprint',{}).get('rejected'):
        return None,'unbound-Master-access-attempt'
    if report.get('status')=='terminal-mismatch':return None,'known-terminal-score-or-clock-disagreement'
    if records and records[-1].get('terminal') is True and report.get('reconstructed_actions',0)<len(original['actions']):
        return None,'known-local-terminal-before-original-action-stream-ended'
    if report.get('complete') is True:
        if (report.get('terminal_score_reconstructed')==original['terminal_score']
            and report.get('terminal_score_matches') is True and report.get('local_terminal_reached') is True
            and report.get('reconstructed_actions')==len(original['actions'])):
            return GOLDEN,None
        return None,'complete-claim-does-not-match-original-terminal'
    if report.get('status')!='stopped':return None,'unclassified-episode-status'
    failure=report.get('failure',{});code=str(failure.get('code',''));detail=str(failure.get('detail',''))
    if any(term in (code+' '+detail).lower() for term in ('mismatch','outside-local','contradict','divergen','guid-not-found')):
        return None,'known-state-or-action-binding-disagreement'
    if code in {'chosen-root-action-not-unique','local-command-not-uniquely-executable'}:
        try:value=ast.literal_eval(detail.split(':',1)[1])
        except (ValueError,SyntaxError,IndexError):return None,'unclassified-next-command-failure'
        gaps=value.get('gaps')
        if not gaps:return None,'recorded-command-has-no-local-explanation'
        combined=' '.join(str(g) for g in gaps).lower()
        if not any(word in combined for word in ('unsupported','unmodel','unresolved','unverified','unprojected','missing','unavailable','unbound','requires','exact-sleepy-trouble-card-family')):
            return None,'unclassified-next-command-gap'
        return PREFIX,None
    if code in {'secondary-parent-select-effect-not-unique','multiple-secondary-commands-not-wired',
        'secondary-prefix-zone-change-requires-command-cursor','secondary-double-search-not-wired',
        'drink-secondary-contract-not-wired'}:
        return PREFIX,None
    if any(word in (code+' '+detail).lower() for word in ('unsupported','unmodel','unresolved','unverified','unprojected','not-wired','requires','unavailable','unbound')):
        return PREFIX,None
    return None,'unclassified-stop-not-admitted'


def _chosen_binds(record):
    chosen=record['chosen'];state=record['sim_state_before'];action=record['original_action']
    kind=chosen['kind'];indexes=action['indexes']
    if kind!=action['action_type']:return False
    if kind=='turn-end':return not indexes and chosen.get('slot_index') is None
    if len(indexes)!=1 or chosen['slot_index']!=indexes[0]:return False
    slot=indexes[0]
    if kind=='use-hand':
        hand=state['cards']['hand']
        if not 0<=slot<len(hand):return False
        card=hand[slot]
        return (card['guid'],card['card_id'],card['effective_upgrade'])==(chosen['sim_card_instance_id'],chosen['card_id'],chosen['effective_upgrade'])
    if kind=='use-drink':
        drinks=state['inventory']['drink_ids']
        return 0<=slot<len(drinks) and drinks[slot]==chosen['drink_id']
    return False


def eligible_rows(original,report,records):
    """Validate real source-bound local rows; yield no failed/current command."""
    _require(report['source']['original_row_sha256']==_digest(original),'report changed original History row')
    for key in ('episode_id','trajectory_id','seed','master_hash','master_version'):
        _require(report['source'][key]==original[key],'report original identity/version/seed differs:'+key)
    _require(report['source']['split']==original['freeze']['split']['assignment'],'report changed original split')
    _require(len(records)==report['teacher_rows'],'report/local row count mismatch')
    tier,reason=episode_quality(original,report,records)
    if tier is None:return [],reason
    previous=None;prior_settled=True;accepted=[]
    failure_cursor=report.get('failure',{}).get('cursor',len(original['actions']))
    _require(type(failure_cursor)is int and 0<=failure_cursor<=len(original['actions']),'failure cursor invalid')
    expected_ordinals=[i for i,a in enumerate(original['actions'][:report['reconstructed_actions']]) if a['action_type']!='effect-card-select']
    _require([r['action_ordinal'] for r in records]==expected_ordinals,'reconstruction rows are not the original committed action prefix')
    for record in records:
        _require(record['schema']==ROW_SCHEMA and record['evidence_authority']=='simulator-teacher','row authority/schema mismatch')
        _require(record['source']==report['source'],'row source differs from its episode')
        for key in ('produce_id','plan_type','exam_effect_type','step_type','idol_card_id','character_id'):
            _require(record['decision_scope'][key]==original[key],'teacher decision scope differs from original History')
        _require(record.get('native_state_claimed') is False and record.get('native_legal_pool_claimed') is False,'teacher claims native authority')
        ordinal=record['action_ordinal']
        _require(type(ordinal)is int and 0<=ordinal<len(original['actions']),'original action ordinal invalid')
        _require(record['original_action']==original['actions'][ordinal],'original teacher action differs')
        _require(_chosen_binds(record),'chosen action does not bind local hand/drink and original slot')
        before=record['sim_state_before'];after=record['sim_state_after']
        _require(record['before_sha256']==_digest(before) and record['after_sha256']==_digest(after),'local state digest mismatch')
        _require(previous is None or previous==record['before_sha256'],'local prefix state continuity is broken')
        observation_from_mapping(before,decision_scope=record['decision_scope'],source_origin={'authority':'simulator-teacher-validation'})
        observation_from_mapping(after,decision_scope=record['decision_scope'],source_origin={'authority':'simulator-teacher-validation'})
        before_main=(before['scalar']['turns_remaining']>0 and before['scalar']['plays_remaining']>0
                     and not before['scalar']['awaiting_turn_start'])
        after_settled=(after['scalar']['turns_remaining']==0 or (after['scalar']['plays_remaining']>0
                       and not after['scalar']['awaiting_turn_start']))
        pool=record['legal_candidates']
        complete=(pool.get('authority')=='simulator-enumeration' and pool.get('complete') is True
                  and not pool.get('unresolved') and len([c for c in pool['rows'] if c==record['chosen']])==1
                  and len(pool['rows'])>=2)
        if (prior_settled and before_main and after_settled and complete and ordinal<failure_cursor
                and before['scalar'].get('play_history') is not None):
            accepted.append({**record,'quality':tier,'sample_weight':WEIGHTS[tier],
                'training_admission':'BC-only:'+tier,
                'terminal_validation':'complete-score-agreement' if tier==GOLDEN else 'unverified-after-unsupported-boundary',
                'native_state_claimed':False,'native_legal_pool_claimed':False})
        previous=record['after_sha256'];prior_settled=prior_settled and after_settled
    return accepted,None


@dataclass(frozen=True)
class ValidatedTieredTeacherDataset:
    manifest_path:Path
    manifest_sha256:str
    rows:tuple[dict,...]
    trajectory_splits:dict[str,str]
    source_episodes:dict[str,dict]

    @property
    def split_counts(self):return dict(Counter(row['source']['split'] for row in self.rows))
    @property
    def quality_counts(self):return dict(Counter(row['quality'] for row in self.rows))


def inspect_research_prefixes(episode_root:Path):
    originals={r['episode_id']:r for line in DATA.open(encoding='utf8') if (r:=json.loads(line))}
    entries=[];excluded=[]
    for path in sorted(episode_root.glob('*/report.json')):
        report_bytes=path.read_bytes();report=_json(report_bytes)
        original=originals[report['source']['episode_id']]
        rows_path=path.parent/'transitions.jsonl';rows_bytes=rows_path.read_bytes()
        records=[_json(line) for line in rows_bytes.split(b'\n') if line.strip()]
        try:accepted,reason=eligible_rows(original,report,records)
        except (ValueError,KeyError,TypeError) as error:
            accepted=[];reason='source-or-local-contract-validation:'+str(error)
        if not accepted:
            excluded.append({'episode_id':original['episode_id'],'reason':reason or 'no-eligible-complete-local-pool-prefix',
                             'completed_local_rows':len(records)})
            continue
        entries.append((original,report_bytes,rows_bytes,accepted,str(path)))
    return entries,excluded


def freeze_tiered_teacher_dataset(episode_root:Path,output:Path):
    _require(not (output/'manifest.json').exists(),'frozen tiered teacher output already exists')
    entries,excluded=inspect_research_prefixes(episode_root)
    _require(entries,'no eligible tiered simulator examples')
    output=output.resolve();output.mkdir(parents=True,exist_ok=True)
    rows=[];sources=[];splits={};seen=set();tier_splits=defaultdict(Counter)
    for original,report_bytes,rows_bytes,accepted,original_path in entries:
        episode=original['episode_id'];trajectory=original['trajectory_id'];split=original['freeze']['split']['assignment']
        _require(split in SPLITS and splits.setdefault(trajectory,split)==split,'original trajectory crosses splits')
        directory=output/'sources'/_digest(episode)[:20];directory.mkdir(parents=True,exist_ok=True)
        files={'report.json':report_bytes,'reconstruction_rows.jsonl':rows_bytes,'original_history_episode.json':_canonical(original)+b'\n'}
        references={}
        for name,body in files.items():
            (directory/name).write_bytes(body)
            references[name]={'path':str((directory/name).relative_to(output)),'sha256':hashlib.sha256(body).hexdigest()}
        sources.append({'episode_id':episode,'trajectory_id':trajectory,'split':split,'original_row_sha256':_digest(original),
            'files':references,'research_report_origin':original_path})
        for row in accepted:
            key=(episode,row['action_ordinal'])
            _require(key not in seen,'duplicate teacher action source');seen.add(key)
            row={**row,'source_snapshot_report_sha256':references['report.json']['sha256']}
            rows.append(row);tier_splits[row['quality']][split]+=1
    rows_file=output/'teacher_transitions.jsonl';rows_file.write_bytes(b''.join(_canonical(row)+b'\n' for row in rows))
    source_file=output/'source_manifest.json'
    source_file.write_bytes(_canonical({'schema':SOURCE_SCHEMA,'source_dataset_path':str(DATA),'source_dataset_sha256':sha256_file(DATA),'episodes':sources})+b'\n')
    manifest={'schema':SCHEMA,'supervision':'behavior-cloning-only','authority':'simulator-teacher-with-disclosed-quality',
        'native_evidence_claimed':False,'runtime_promotion_allowed':False,
        'teacher_rows_path':rows_file.name,'teacher_rows_sha256':sha256_file(rows_file),
        'source_manifest_path':source_file.name,'source_manifest_sha256':sha256_file(source_file),
        'row_count':len(rows),'trajectory_count':len(splits),'trajectory_splits':splits,
        'quality_sample_weights':WEIGHTS,'quality_split_row_counts':{k:dict(v) for k,v in tier_splits.items()},
        'scope':'Original NIA Pro/Master FullPower replay sources, unchanged original trajectory splits',
        'quality_policy':{'golden':'Full original stream, terminal reached and recorded terminal score agrees; locally complete candidate pools.',
            'weak':'Only committed prefixes before an unsupported/unresolved next-command stop; original action/slot/source/hash/version, scalar-card consistency and complete local pools required.',
            'excluded':'Known score/terminal/slot/state contradictions, premature terminal, unexplained absent local command, incomplete/unknown candidate pools, failed command and all following commands.',
            'initial_history':'Known empty at actual new-exam start; successful uses appended by existing execution owner.'},
        'limitations':['Weak local states and inferred hand identities are hypotheses, not native labels; terminal after the stopping point is unverified.',
            'Teacher validation/test must be reported by quality tier and separately from native heldout and live results.',
            'A complete local score match does not independently prove every hidden native intermediate field.',
            'Source IDs, seed, GUIDs and UIDs are provenance and must not be training shortcuts.'],
        'excluded_episodes':excluded}
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf8')
    return load_tiered_teacher_dataset(output/'manifest.json')


def _frozen_path(root,record):
    path=(root/record['path']).resolve()
    _require(path.is_relative_to(root),'tiered source escapes immutable dataset directory')
    _require(path.is_file() and sha256_file(path)==record['sha256'],'tiered source file hash mismatch')
    return path


def load_tiered_teacher_dataset(manifest_path:Path)->ValidatedTieredTeacherDataset:
    manifest_path=Path(manifest_path).resolve();root=manifest_path.parent;manifest=_read(manifest_path)
    _require(manifest.get('schema')==SCHEMA and manifest.get('supervision')=='behavior-cloning-only','wrong tiered teacher schema/supervision')
    _require(manifest.get('native_evidence_claimed') is False and manifest.get('runtime_promotion_allowed') is False,'tiered teacher cannot claim native/promotion authority')
    _require(manifest.get('quality_sample_weights')==WEIGHTS,'tiered quality weights changed')
    rows_path=_frozen_path(root,{'path':manifest['teacher_rows_path'],'sha256':manifest['teacher_rows_sha256']})
    source_path=_frozen_path(root,{'path':manifest['source_manifest_path'],'sha256':manifest['source_manifest_sha256']})
    sources=_read(source_path);_require(sources['schema']==SOURCE_SCHEMA,'wrong tiered source manifest')
    expected={};episodes={};splits={}
    for source in sources['episodes']:
        files={name:_frozen_path(root,ref) for name,ref in source['files'].items()}
        original=_read(files['original_history_episode.json']);report=_read(files['report.json']);records=_rows(files['reconstruction_rows.jsonl'])
        _require(_digest(original)==source['original_row_sha256'],'frozen original row hash differs')
        _require(original['episode_id']==source['episode_id'] and original['trajectory_id']==source['trajectory_id'],'frozen source identity differs')
        _require(source['split']==original['freeze']['split']['assignment'] and source['split'] in SPLITS,'original split changed')
        _require(splits.setdefault(source['trajectory_id'],source['split'])==source['split'],'source trajectory crosses splits')
        accepted,reason=eligible_rows(original,report,records)
        _require(accepted and reason is None,'frozen episode no longer meets its declared tier')
        for row in accepted:
            key=(source['episode_id'],row['action_ordinal']);_require(key not in expected,'duplicate expected source action')
            expected[key]={**row,'source_snapshot_report_sha256':source['files']['report.json']['sha256']}
        episodes[source['episode_id']]=original
    rows=_rows(rows_path);seen=set();counts=defaultdict(Counter)
    for row in rows:
        key=(row['source']['episode_id'],row['action_ordinal']);_require(key not in seen,'duplicate emitted tiered row');seen.add(key)
        _require(key in expected and _canonical(row)==_canonical(expected[key]),'emitted row differs from independently validated frozen source')
        _require(type(row['sample_weight']) is float and math.isfinite(row['sample_weight']) and row['sample_weight']==WEIGHTS[row['quality']],'quality/sample weight mismatch')
        counts[row['quality']][row['source']['split']]+=1
    _require(seen==set(expected),'tiered dataset omits or adds eligible source rows')
    _require(len(rows)==manifest['row_count'] and len(splits)==manifest['trajectory_count'],'tiered counts differ')
    _require(splits==manifest['trajectory_splits'],'tiered split manifest differs')
    _require({k:dict(v) for k,v in counts.items()}==manifest['quality_split_row_counts'],'tiered quality split counts differ')
    return ValidatedTieredTeacherDataset(manifest_path,sha256_file(manifest_path),tuple(rows),splits,episodes)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path)
    parser.add_argument('--inspect',action='store_true');parser.add_argument('--validate',type=Path)
    args=parser.parse_args()
    if args.validate:
        result=load_tiered_teacher_dataset(args.validate)
        print(json.dumps({'rows':len(result.rows),'splits':result.split_counts,'quality':result.quality_counts}));return
    if args.inspect:
        entries,excluded=inspect_research_prefixes(RESEARCH/'episodes');counts=defaultdict(Counter);trj=defaultdict(set)
        for original,_,_,rows,_ in entries:
            for row in rows:counts[row['quality']][row['source']['split']]+=1;trj[row['quality']].add(row['source']['trajectory_id'])
        report={'quality_split_rows':{k:dict(v) for k,v in counts.items()},'quality_trajectories':{k:len(v) for k,v in trj.items()},
                'excluded_reasons':dict(Counter(e['reason'] for e in excluded)),'accepted_episodes':len(entries)}
        (RESEARCH/'tiered_freeze_inspection.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps(report,ensure_ascii=False));return
    _require(args.output is not None,'--output or --inspect or --validate is required')
    result=freeze_tiered_teacher_dataset(RESEARCH/'episodes',args.output)
    print(json.dumps({'manifest':str(result.manifest_path),'rows':len(result.rows),'splits':result.split_counts,'quality':result.quality_counts}))


if __name__=='__main__':main()
