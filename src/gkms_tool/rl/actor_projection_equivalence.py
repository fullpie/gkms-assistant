"""Read a source-bound exact-input parity receipt without rebuilding the corpus."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading

from .contracts import ContractError, digest
from .game_projection import default_routes

SCHEMA = 'gkms.rl.actor-input-equivalence.v1'
CONTINUATION_SCHEMA = 'gkms.rl.actor-input-equivalence-continuation.v1'
_CACHE = {}
_LOCK = threading.RLock()


def _stamp(path):
    value=path.stat()
    return value.st_size,value.st_mtime_ns,value.st_ctime_ns


def read_projection_equivalence(reference):
    key=(reference['path'],reference['sha256'])
    with _LOCK:
        cached=_CACHE.get(key)
        if cached:
            try:
                if all(_stamp(path)==value for path,value in cached[1]):
                    return deepcopy(cached[0]),cached[1]
            except OSError:
                pass
        path=Path(reference['path']);raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=reference['sha256']:
            raise ContractError('Actor input equivalence receipt changed')
        receipt=json.loads(raw);watches={path:_stamp(path)}
        if receipt.get('schema') == CONTINUATION_SCHEMA:
            parent,parent_watches=read_projection_equivalence(receipt['original_input_proof'])
            checkpoint=receipt['checkpoint'];checkpoint_path=Path(checkpoint['path'])
            if hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()!=checkpoint['sha256']:
                raise ContractError('Continuation checkpoint differs from reused input proof')
            import torch
            saved=torch.load(checkpoint_path,map_location='cpu',weights_only=True)
            metadata=saved.get('model_metadata',{})
            exam_schema=metadata.get('feature_schema_sha256')
            if metadata.get('model_kind')=='gkms.rl.shared-observed-outer-policy.v2':
                from .actor_handoff import validate_actor_metadata
                from .features import FeatureSchema
                from .observed_outer_projection import extend_schema
                metadata=validate_actor_metadata(metadata).unpack()
                original=FeatureSchema.from_dict(saved.get('feature_schema',{}))
                expanded=FeatureSchema.from_dict(saved.get('model_feature_schema',{}))
                exam_schema=metadata['original_exam_schema_sha256']
                if (original.identity!=exam_schema or expanded!=extend_schema(original)
                        or expanded.identity!=metadata['feature_schema_sha256']):
                    raise ContractError('Continuation changed the proved original exam schema')
            expected={'feature_schema_sha256':parent['feature_schema_sha256'],
                'source_master_hash':parent['source_master_hash'],
                'runtime_base_projection_id':parent['runtime_base_projection_id'],
                'numeric_implementations':parent['numeric_implementations']}
            if (any(receipt.get(key)!=value for key,value in expected.items())
                    or receipt.get('new_parity_tests_performed') is not False
                    or saved.get('phase')!='complete' or saved.get('dataset_identity')!=receipt.get('dataset_identity')
                    or exam_schema!=parent['feature_schema_sha256']
                    or saved.get('source_bindings',{}).get(digest(parent['trained_source_binding']))!=parent['trained_source_binding']):
                raise ContractError('Continuation changed the proved feature/source contract')
            # Parity is about the exact model INPUTS, not weight values or the
            # new generated dataset. Retain the original proof verbatim on disk.
            result={**parent,'checkpoint_sha256':checkpoint['sha256'],'dataset_identity':receipt['dataset_identity'],
                'continuation_input_proof':deepcopy(receipt),'parity_evidence_reused':True}
            watches.update(parent_watches);watches[checkpoint_path]=_stamp(checkpoint_path)
            return result,tuple(watches.items())
        if (receipt.get('schema')!=SCHEMA or receipt.get('status')!='passed' or receipt.get('failures')!=[]
                or receipt.get('training_partition_only') is not True or receipt.get('new_native_execution') is not False
                or receipt.get('training_performed') is not False or receipt.get('game_io') is not False):
            raise ContractError('Completed TRAIN input-equivalence evidence required')
        checked=set()
        for row in receipt.get('rows',[]):
            if (row.get('partition')!='train' or any(row.get(name) is not True for name in
                    ('information_equal','candidates_equal','all_model_tensors_equal'))):
                raise ContractError('Actor parity evidence contains an incomplete/non-TRAIN comparison')
            for name in ('transition','raw_event','shard_manifest','qualification'):
                ref=row[name];item=Path(ref['path']);offset=ref.get('byte_offset',0)
                identity=(str(item),offset,ref['bytes'],ref['sha256'])
                if identity in checked:continue
                before=_stamp(item)
                with item.open('rb') as stream:
                    stream.seek(offset);data=stream.read(ref['bytes'])
                if (len(data)!=ref['bytes'] or hashlib.sha256(data).hexdigest()!=ref['sha256']
                        or _stamp(item)!=before):
                    raise ContractError('Retained qualified row/raw/qualification evidence changed')
                watches[item]=before;checked.add(identity)
        routes=default_routes()
        expected={(flow[-1],mode[-1],stage[-1]) for flow in routes['flows'] for mode in routes['modes'] for stage in routes['stages']}
        actual={(row['scope']['flow_id'],row['scope']['mode_id'],row['scope']['stage_id'])
            for row in receipt['rows'] if row['kind']=='exam_main'}
        if actual!=expected or not any(row['kind']=='exam_secondary' for row in receipt['rows']):
            raise ContractError('Actor parity needs all six-flow/two-mode/three-stage main cells and observed secondary inputs')
        for name,expected_sha in receipt['numeric_implementations'].items():
            if name not in ('game_projection.py','native_information_view.py','semantic_entity_encoding.py'):
                raise ContractError('Unexpected numerical equivalence module')
            source=Path(__file__).parent/name
            if hashlib.sha256(source.read_bytes()).hexdigest()!=expected_sha:
                raise ContractError('Actor numerical projection changed after exact parity: '+name)
            watches[source]=_stamp(source)
        if set(receipt['numeric_implementations'])!={'game_projection.py','native_information_view.py','semantic_entity_encoding.py'}:
            raise ContractError('Complete actor numerical projection inventory required')
        result=tuple(watches.items())
        if len(_CACHE)>=4:_CACHE.clear()
        _CACHE[key]=(deepcopy(receipt),result)
        return receipt,result


def bind_projection_equivalence(reference, *, checkpoint_sha256, dataset_identity, source_master_hash,
                                feature_schema_sha256, runtime_base_projection_id, source_bindings,
                                runtime_master_source):
    receipt,watches=read_projection_equivalence(reference)
    expected={'checkpoint_sha256':checkpoint_sha256,'dataset_identity':dataset_identity,
        'source_master_hash':source_master_hash,'feature_schema_sha256':feature_schema_sha256,
        'runtime_base_projection_id':runtime_base_projection_id}
    if any(receipt.get(key)!=value for key,value in expected.items()):
        raise ContractError('Actor input equivalence belongs to another model/source/runtime projection')
    if receipt.get('runtime_master_source',{}).get('sha256')!=runtime_master_source.get('sha256'):
        raise ContractError('Actor runtime Master package differs from the exact-input proof')
    binding=receipt['trained_source_binding']
    if source_bindings.get(digest(binding))!=binding:
        raise ContractError('Actor parity training binding is absent from the completed checkpoint')
    return deepcopy(binding),watches


__all__=['read_projection_equivalence','bind_projection_equivalence','SCHEMA']
