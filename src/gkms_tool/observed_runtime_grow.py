"""Typed, opt-in input for original PC materialized runtime grows.

The original list is already materialized. Its repeated ID lineage is a
separate witness, never another set of modifiers. No source value is filled
into the native state, and a Master ID never replaces the observed value.
"""
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .card_semantic_features import CardSemanticFeatures, _v4_card_tokens
from .training_artifact_io import canonical_json_bytes

SCHEMA='gkms.observed-runtime-grow-input.v1'
NATIVE_CORE_SHA256='1e730b223cab48c3cf80837d7cd04b1b6b6eb04452135fb01c7d655aaf52017a'
NATIVE_METADATA_SHA256='9a6bf153c0c42a2768e619cc7d96fa79fcc1d341e9cf9bcbdc8d6bca48812668'
NATIVE_EVIDENCE_SHA256='b8b8d626b94cadfcab847c30326fc6e960f8755eb88f7a25fca9cd3a6caf00b8'
SOURCE_MASTER_HASH='6609655d0c35511465a525af13bc60d697dd411972087c90ae756f2c1089406f'
SOURCE_CARD_PAYLOAD_SHA256='c01b54059f4a5387fe3c492c83425eeeaaac834d077f2eb8ffe84ccfbe12d56e'
SOURCE_SNAPSHOT_FINGERPRINT='05e809462fc53abfb39747b22be17d61db807bdfe560c22560644a5cb9d6310a'
SOURCE_GROW_CATALOG_SHA256='ecd641931d5a12dd58492f1af5b4381b308eebc45c5326b612bbb77961dd894d'
HISTORICAL_COMPARISON_SHA256='ed1c53260b7443d0738440c32faef98c163450aeb184dde13afe0480d770a8ee'
HISTORICAL_INVENTORY_SHA256='24f164de28bea7f20efdb2647e07fc51a9333bbd2cf3d9a306165fbc518b5264'
# These are distinct source Master identities, even when two frozen catalog
# payloads happen to be equal. Only their consumed 453-entry grow table is
# proved equal to B660; card/PT/gimmick catalogs remain individually bound.
HISTORICAL_CATALOGS={
    '621fb4d8e3ba2394e222b29e01a7a1e9cef035e90f40ac8dd486d35c6d103867':(
        'd99c3fb48f17ceea894e57fc4f4be937e51d4e2c644df918fb8531a556e3c69e',
        '0dbf9ac4083794189cdfcdb70f9b104824d32ea33dc49c00349891a9aa2b6dfe'),
    '960d512dc4399a58d8745177d9bdeb895107686f9fbb6cf9e54e2d0d20a2371f':(
        'd94631e8ccc09b863266f44967b63b481cf9584022a191f11481a9537c27f958',
        'ec400c048c99bf2e8e5ad31518a23f039fc73a4dc1840921016db56fa27d5744'),
    'aabab9c1c8c5641648b9ee560de557f112ee969fe71a850ba49d046186c92169':(
        '38a1ca8d710c5f4ea61bb4a8765139cc8a8601a2fbf1ee1259de835869bc5040',
        'dc7b60d180707d4ce2970fb716746103f47b648cfc45b81f1905205d4ccbecc7'),
    'd56c5c5ee6f5b88ea72e0c163a92993d691a0cd1dbccc8a4fbcec6fd34507321':(
        'd94631e8ccc09b863266f44967b63b481cf9584022a191f11481a9537c27f958',
        'ec400c048c99bf2e8e5ad31518a23f039fc73a4dc1840921016db56fa27d5744'),
}
TYPE_NAMES={1:'LessonAdd',3:'LessonCountAdd',5:'BlockAdd',13:'CostAdd'}
FIELDS={'_id','_effectType','_value','_playProduceExamTriggerId','_playEffectProduceExamTriggerId',
    '_targetPlayEffectProduceExamTriggerIdList','_playProduceExamEffectId',
    '_targetPlayProduceExamEffectIdList','_produceCardStatusEnchantId','_playMovePositionType'}
FIELD_MAP={'_playProduceExamTriggerId':'playProduceExamTriggerId',
    '_playEffectProduceExamTriggerId':'playEffectProduceExamTriggerId',
    '_targetPlayEffectProduceExamTriggerIdList':'targetPlayEffectProduceExamTriggerIds',
    '_playProduceExamEffectId':'playProduceExamEffectId',
    '_targetPlayProduceExamEffectIdList':'targetPlayProduceExamEffectIds',
    '_produceCardStatusEnchantId':'produceCardStatusEnchantId'}
MASTER_FIELDS={'id','effectType','value','costType','effectGroupIds','playMovePositionType',*FIELD_MAP.values()}
# Original PC predicate sets; broader targets remain unsupported by the old
# direct-only fold. These sets are exclusion guards, not a new effect allowlist.
NATIVE_TARGETS={
    'LessonAdd':frozenset(('ExamLesson','ExamMultipleLessonBuffLesson','ExamLessonAddBlock',
        'ExamLessonFullPowerPoint','ExamLessonPerSearchCount','ExamLessonAddMultipleParameterBuff',
        'ExamLessonDependPlayCardCountSum','ExamMultipleEnthusiasticLesson',
        'ExamMultipleConcentrationLesson','ExamMultipleFullPowerLesson')),
    'LessonCountAdd':frozenset(('ExamLesson','ExamLessonDependBlock','ExamMultipleLessonBuffLesson',
        'ExamLessonFix','ExamLessonAddBlock','ExamLessonFullPowerPoint','ExamLessonPerSearchCount',
        'ExamLessonDependExamReview','ExamLessonDependExamCardPlayAggressive','ExamLessonDependParameterBuff',
        'ExamLessonAddMultipleParameterBuff','ExamLessonDependStamina','ExamLessonDependStaminaConsumptionSum',
        'ExamLessonDependPlayCardCountSum','ExamLessonDependBlockAndSearchCount',
        'ExamLessonDependAggressiveAndSearchCount','ExamLessonDependReviewAndSearchCount',
        'ExamMultipleEnthusiasticLesson','ExamMultipleConcentrationLesson','ExamMultipleFullPowerLesson',
        'ExamLessonDependBlockConsumptionSum','ExamLessonDependEnthusiasticGetSum')),
    'BlockAdd':frozenset(('ExamBlock','ExamBlockPerUseCardCount','ExamBlockAddMultipleAggressive','ExamBlockPerSearchCount'))}


def _digest(value):return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _historical_bridge_binding(source_master_hash,card_payload_sha256,snapshot_fingerprint):
    if not isinstance(source_master_hash,str) or HISTORICAL_CATALOGS.get(source_master_hash)!=(card_payload_sha256,snapshot_fingerprint):
        raise ValueError('Historical runtime grow source Master/catalog/fingerprint tuple is not proved')
    return {'comparison_sha256':HISTORICAL_COMPARISON_SHA256,'source_inventory_sha256':HISTORICAL_INVENTORY_SHA256,
        'source_master_hash':source_master_hash,'card_payload_sha256':card_payload_sha256,
        'snapshot_fingerprint':snapshot_fingerprint,'consumed_table':'grow_effects',
        'consumed_table_rows':453,'consumed_table_semantic_sha256':SOURCE_GROW_CATALOG_SHA256,
        'reference_source_master_hash':SOURCE_MASTER_HASH,'card_PT_gimmick_equivalence_claimed':False,
        'shared_feature_use':'existing-v4-only-with-verified-source-input','training_admitted':False}


def historical_runtime_grow_contract(*,source_master_hash,features,comparison_path,inventory_path):
    """Verify existing source catalogs and consumed-table proof; keep original fold."""
    def read(path,sha):
        raw=Path(path).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=sha:
            raise ValueError('Historical grow bridge evidence hash differs')
        return json.loads(raw)
    comparison=read(comparison_path,HISTORICAL_COMPARISON_SHA256)
    inventory=read(inventory_path,HISTORICAL_INVENTORY_SHA256)
    if not isinstance(features,CardSemanticFeatures):
        raise ValueError('Historical grow bridge requires a frozen card semantic catalog')
    payload=features.payload
    # These preserved v4 artifacts use canonical JSON followed by one newline.
    # Hash actual decoded content, not a caller-provided fingerprint or label.
    payload_sha=hashlib.sha256(canonical_json_bytes(payload)+b'\n').hexdigest()
    fingerprint=payload.get('snapshot_fingerprint')
    bridge=_historical_bridge_binding(source_master_hash,payload_sha,fingerprint)
    matches=[x for x in comparison['versions'] if x['master_hash']==source_master_hash]
    sources=[x for x in inventory['versions'] if x['master_hash']==source_master_hash]
    if (len(matches)!=1 or len(sources)!=1 or matches[0]['source_feature_payload']!=sources[0]['feature_payload']
            or matches[0]['source_feature_payload']['sha256']!=payload_sha
            or matches[0]['snapshot_fingerprint']!=fingerprint
            or matches[0]['grow_count']!=453 or matches[0]['equal_common']!=453
            or any(matches[0][k] for k in ('changed_ids','historical_only_ids','B_only_ids'))
            or _digest(payload.get('grow_effects'))!=SOURCE_GROW_CATALOG_SHA256):
        raise ValueError('Historical grow table/source catalog differs from the preserved proof')
    return runtime_grow_contract(source_master_hash=source_master_hash,card_payload_sha256=payload_sha,
        snapshot_fingerprint=fingerprint,native_evidence_sha256=NATIVE_EVIDENCE_SHA256,historical_catalog_bridge=bridge)


def validate_historical_grow_payload(payload,contract):
    """New v4 boundary only: bind all actual catalog content to its own source."""
    if 'historical_catalog_bridge' not in contract:
        return
    if hashlib.sha256(canonical_json_bytes(payload)+b'\n').hexdigest()!=contract['card_payload_sha256']:
        raise ValueError('Historical grow input catalog content differs from its frozen source payload')


def runtime_grow_contract(*,source_master_hash,card_payload_sha256,snapshot_fingerprint,native_evidence_sha256,
                          historical_catalog_bridge=None):
    values=(source_master_hash,card_payload_sha256,snapshot_fingerprint,native_evidence_sha256)
    if any(not isinstance(x,str) or len(x)!=64 or any(c not in '0123456789abcdef' for c in x) for x in values):
        raise ValueError('Runtime grow source Master/catalog/native proof hashes required')
    if native_evidence_sha256!=NATIVE_EVIDENCE_SHA256:
        raise ValueError('Runtime grow requires the frozen original PC field/enum/fold proof')
    if historical_catalog_bridge is not None:
        if (not isinstance(historical_catalog_bridge,Mapping)
                or _digest(historical_catalog_bridge)!=_digest(_historical_bridge_binding(source_master_hash,card_payload_sha256,snapshot_fingerprint))):
            raise ValueError('Historical runtime grow bridge contract differs')
    elif (source_master_hash,card_payload_sha256,snapshot_fingerprint)!=(
            SOURCE_MASTER_HASH,SOURCE_CARD_PAYLOAD_SHA256,SOURCE_SNAPSHOT_FINGERPRINT):
        raise ValueError('Runtime grow requires the qualified Master660/catalog/fingerprint tuple')
    body={'schema':SCHEMA,'source_master_hash':source_master_hash,'card_payload_sha256':card_payload_sha256,
        'snapshot_fingerprint':snapshot_fingerprint,'native_evidence_sha256':native_evidence_sha256,
        'native_core_sha256':NATIVE_CORE_SHA256,'native_metadata_sha256':NATIVE_METADATA_SHA256,
        'grow_catalog_semantic_sha256':SOURCE_GROW_CATALOG_SHA256,
        'fold_order':'PT-then-observed-materialized-runtime-list','lineage_role':'witness-only-never-folded',
        'positive_direct_fold_types':[1,3,5],'recognized_unfolded_types':[13],
        'unknown_fields_types_targets_or_order':'retain-gap','training_admitted':False}
    if historical_catalog_bridge is not None:
        body['historical_catalog_bridge']=deepcopy(dict(historical_catalog_bridge))
    return {**body,'contract_sha256':_digest(body)}


def validate_runtime_grow_contract(contract,*,card_payload_sha256,snapshot_fingerprint):
    if not isinstance(contract,Mapping):raise ValueError('Runtime grow contract required')
    expected=runtime_grow_contract(source_master_hash=contract.get('source_master_hash'),
        card_payload_sha256=card_payload_sha256,snapshot_fingerprint=snapshot_fingerprint,
        native_evidence_sha256=contract.get('native_evidence_sha256'),
        historical_catalog_bridge=contract.get('historical_catalog_bridge'))
    if dict(contract)!=expected:raise ValueError('Runtime grow source/field/fold contract differs')


@dataclass(frozen=True)
class ObservedRuntimeGrowInput:
    raw_rows: object
    lineage: object
    normalized_grows: tuple[Mapping,...]
    gaps: tuple[str,...]
    source_master_hash: str
    source_catalog_sha256: str


def adapt_observed_runtime_grows(runtime,*,payload,contract):
    """Preserve raw values/order, project only fully known neutral PC rows."""
    validate_runtime_grow_contract(contract,card_payload_sha256=contract['card_payload_sha256'],
        snapshot_fingerprint=payload['snapshot_fingerprint'])
    catalog=payload['grow_effects'];raw_rows=runtime.get('_growEffectExamStartAfterList')
    # The helper consumes this table only. Check its canonical contents, not
    # a self-asserted fingerprint or the unrelated pretty-printed file bytes.
    if _digest(catalog)!=SOURCE_GROW_CATALOG_SHA256:
        raise ValueError('Observed runtime grow catalog contents differ from the frozen source')
    lineage=runtime.get('_affectGrowEffectIdList');gaps=[];normalized=[]
    if not isinstance(raw_rows,list):gaps.append('runtime-grow-rows-not-observed-list')
    if not isinstance(lineage,list) or any(not isinstance(x,str) or not x or x not in catalog for x in lineage):
        gaps.append('runtime-grow-lineage-unbound')
    for index,raw in enumerate(raw_rows if isinstance(raw_rows,list) else ()):
        prefix=f'runtime-grow[{index}]'
        if not isinstance(raw,Mapping) or set(raw)!=FIELDS:
            gaps.append(prefix+':unknown-or-missing-field');continue
        identity,kind,value=raw['_id'],raw['_effectType'],raw['_value']
        master=catalog.get(identity) if isinstance(identity,str) else None
        if not isinstance(master,Mapping) or set(master)!=MASTER_FIELDS or master.get('id')!=identity:
            gaps.append(prefix+':source-Master-row-unbound');continue
        name=TYPE_NAMES.get(kind) if type(kind) is int else None
        if name is None:
            gaps.append(prefix+':type-not-qualified');continue
        if master['effectType']!='ProduceCardGrowEffectType_'+name:
            gaps.append(prefix+':type-differs-from-source-definition');continue
        if type(value) is not int or not -(2**31)<=value<2**31:
            gaps.append(prefix+':value-not-Int32');continue
        if (raw['_playMovePositionType']!=0 or type(raw['_playMovePositionType']) is not int
                or master['playMovePositionType']!='ProduceCardMovePositionType_Unknown'
                or master['costType']!='ExamCostType_Unknown' or master['effectGroupIds']!=[]):
            gaps.append(prefix+':unqualified-static-or-move-field');continue
        valid=True
        for field,target in FIELD_MAP.items():
            neutral=[] if field.endswith('IdList') else ''
            if raw[field]!=neutral or type(raw[field]) is not type(neutral) or master[target]!=neutral:
                gaps.append(prefix+':conditional-field-unqualified:'+field);valid=False
        if not valid:continue
        # Unrepresented Master fields are admitted only as the exact neutral
        # source definition above. Every actual native field remains separate.
        grow=deepcopy(dict(master));grow['value']=value
        for field,target in FIELD_MAP.items():grow[target]=deepcopy(raw[field])
        normalized.append(grow)
        if value<0:gaps.append(prefix+':negative-value-fold-unqualified')
    # Check this bounded positive/neutral lineage against materialized groups.
    # Its sums are only witnesses: even a disagreement never replaces value.
    if not gaps and isinstance(lineage,list):
        sums={};first={};names=[]
        for identity in lineage:
            item=catalog[identity];name=item.get('effectType')
            if name not in {'ProduceCardGrowEffectType_'+x for x in TYPE_NAMES.values()} or type(item.get('value')) is not int or item['value']<0:
                gaps.append('runtime-grow-lineage-operation-unqualified');break
            if name not in sums:names.append(name);sums[name]=0;first[name]=identity
            sums[name]+=item['value']
        if not gaps and (names!=[x['effectType'] for x in normalized] or any(
                x['id']!=first.get(x['effectType']) or x['value']!=sums.get(x['effectType']) for x in normalized)):
            gaps.append('runtime-grow-materialization-order-or-lineage-unqualified')
    return ObservedRuntimeGrowInput(deepcopy(raw_rows),deepcopy(lineage),tuple(normalized),tuple(dict.fromkeys(gaps)),
        contract['source_master_hash'],contract['card_payload_sha256'])


def direct_runtime_fold_gaps(kind,effects,value):
    """Guard the old direct fold against the proven broader PC predicates."""
    if kind not in NATIVE_TARGETS:return ()  # Existing fold emits its unsupported-operation gap.
    target='ExamBlock' if kind=='BlockAdd' else 'ExamLesson'
    field='effectCount' if kind=='LessonCountAdd' else 'effectValue1'
    gaps=[]
    for effect in effects:
        name=str(effect.get('effectType','')).removeprefix('ProduceExamEffectType_')
        if name in NATIVE_TARGETS[kind] and name!=target:
            gaps.append('runtime-grow-target-coverage-unqualified:'+kind)
        if name==target:
            old=effect.get(field)
            if (type(old) is not int or type(value) is not int or value<0
                    or not 1<=old+value<2**31):
                gaps.append('runtime-grow-direct-clamp-or-overflow-unqualified:'+kind)
    return tuple(dict.fromkeys(gaps))


@dataclass(frozen=True,slots=True)
class ObservedRuntimeGrowSemanticFeatures(CardSemanticFeatures):
    runtime_grow_contract: Mapping

    def card_tokens(self,card_id,upgrade,runtime):
        return _v4_card_tokens(self.payload,card_id,upgrade,runtime,
            observed_runtime_grow_contract=self.runtime_grow_contract)
