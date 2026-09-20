"""Owned native policy observations through the shared primary feature kernel.

Historical model files keep their original source identities. The explicit
bridge proves that the refactored numerical/token kernel is the original AST,
with unchanged helpers, while the runtime uses a different provenance adapter.
No replay label, original action cursor, score target or training admission is
used to make a policy observation acceptable.
"""
from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import threading

import numpy as np

from . import shared_bc_features as shared
from .behavior_cloning import _outer_forward
from .native_primary_bc_training import load_native_primary_bc_model
from .native_structure_contract_set import resolve_native_structure_contract
from .native_structure_features import load_native_structure_package, NativeStructureCardSemanticFeatures
from .observed_empty_collections import (
    PC_SOURCE, NATIVE_CORE_SHA256, NATIVE_METADATA_SHA256, _semantic_view,
    validate_empty_collection_contract,
)
from .original_pc_primary_input import prepare_original_pc_primary
from .training_artifact_io import canonical_json_bytes, sha256_file

SCHEMA = 'gkms.owned-native-primary-policy-input.v1'


def _read(reference):
    if sha256_file(Path(reference['path'])) != reference.get('sha256'):
        raise ValueError('Native policy source file differs from its reference')
    return json.loads(Path(reference['path']).read_bytes())


def _reference(path):
    path = Path(path).resolve()
    return {'path': str(path), 'sha256': sha256_file(path)}


def _ast(nodes):
    return ast.dump(ast.Module(body=nodes, type_ignores=[]), include_attributes=False)


def verify_primary_kernel_bridge(contract_set, original_shared_reference, *, loader_compatibility=None):
    """Verify exact extraction and unchanged helpers, not merely matching scores."""
    if loader_compatibility is not None:
        from .runtime_loader_compatibility import RuntimeLoaderCompatibility
        if type(loader_compatibility) is not RuntimeLoaderCompatibility:
            raise ValueError('An independently verified loader compatibility receipt is required')
        loader_compatibility.validate_unchanged()
    original_path = Path(original_shared_reference['path'])
    if sha256_file(original_path) != original_shared_reference['sha256']:
        raise ValueError('Frozen original shared encoder reference changed')
    old_tree = ast.parse(original_path.read_text(encoding='utf-8'))
    current_path = Path(shared.__file__)
    current_tree = ast.parse(current_path.read_text(encoding='utf-8'))
    def function(tree, name):
        matches = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
        if len(matches) != 1:
            raise ValueError('Expected shared encoder function is missing or ambiguous')
        return matches[0]
    old = function(old_tree, 'encode_shared_bc_primary')
    wrapper = function(current_tree, 'encode_shared_bc_primary')
    kernel = function(current_tree, 'encode_primary_feature_view')
    split = next(i for i, n in enumerate(old.body) if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == 'pair' for t in n.targets))
    if _ast(old.body[split:]) != _ast(kernel.body[2:]):
        raise ValueError('Refactored primary token kernel differs from the original model encoder')
    if _ast(old.body[:split]) != _ast(wrapper.body[:-1]):
        raise ValueError('Original archived-source admission wrapper was altered')
    expected_call = ast.parse('''return encode_primary_feature_view(raw_state=raw_state, state_before=state_before,
        legal_candidates=legal_candidates, flow=flow, stage=stage, native_mask_complete=native_mask_complete,
        candidate_scope=candidate_scope, features=features, contract=contract, empty_evidence=empty_evidence)''').body
    if _ast([wrapper.body[-1]]) != _ast(expected_call):
        raise ValueError('Shared source adapter does not delegate unchanged arguments to the common kernel')
    ignored = {'encode_shared_bc_primary', 'encode_primary_feature_view'}
    def others(tree):
        return [n for n in tree.body if not isinstance(n, ast.FunctionDef) or n.name not in ignored]
    if _ast(others(old_tree)) != _ast(others(current_tree)):
        raise ValueError('Shared constants or helper definitions differ from the baseline encoder')
    expected_guard = ast.parse('''if native_mask_complete is not True or candidate_scope != PRIMARY_SCOPE:
    raise ValueError('Complete native primary mask required by the shared feature kernel')''').body
    if _ast([kernel.body[1]]) != _ast(expected_guard):
        raise ValueError('Shared primary kernel boundary guard differs')
    dependencies = {}
    for contract in contract_set['contracts'].values():
        if contract['schema'] != shared.NATIVE_STRUCTURE_SCHEMA:
            raise ValueError('This bridge is restricted to the frozen native structure primary representation')
        for name, expected_sha in contract['encoder_source_hashes'].items():
            path = current_path.with_name(name)
            if name == 'shared_bc_features.py':
                if expected_sha != original_shared_reference['sha256']:
                    raise ValueError('Baseline model names another original shared encoder')
            elif sha256_file(path) != expected_sha:
                if loader_compatibility is None:
                    raise ValueError('Baseline encoder dependency changed: ' + name)
                loader_compatibility.verify_dependency(name, expected_sha, path)
            dependencies[name] = _reference(path)
    return {'schema': 'gkms.primary-bc-exact-kernel-bridge.v1',
        'original_shared_encoder': deepcopy(original_shared_reference), 'runtime_shared_encoder': _reference(current_path),
        'kernel_AST_sha256': hashlib.sha256(_ast(old.body[split:]).encode()).hexdigest(),
        'exact_kernel_AST_equal': True, 'other_shared_definitions_equal': True,
        'runtime_sources': dependencies, 'model_contract_set_sha256': contract_set['contract_set_sha256'],
        'original_model_or_source_retagged': False, 'policy_input_schema': SCHEMA,
        'new_policy_observation_training_qualified': False}


class OwnedNativePolicySource:
    """Run-scoped source/version witness; decisions remain label-free."""
    def __init__(self, identity):
        if (not isinstance(identity, dict) or identity.get('worker_pid') != os.getpid()
                or not isinstance(identity.get('run_id'), str) or not identity['run_id']):
            raise ValueError('A current owned native policy run identity is required')
        inputs = _read(identity['inputs_reference'])
        source = _read(identity['source_reference'])
        reconstruction = _read(inputs['reconstruction_manifest'])
        initialization = _read(identity['initialization_reference'])
        if (reconstruction.get('source_image_sha256') != NATIVE_CORE_SHA256
                or reconstruction.get('original_executable_bytes_unchanged') is not True
                or initialization.get('exam_data_initialized') is not True
                or initialization.get('source_sha256') != identity['source_reference']['sha256']
                or inputs.get('original_replay_sha256') != identity['source_reference']['sha256']
                or inputs.get('game_process_used') is not False):
            raise ValueError('Policy initialization/source/original PC execution identity differs')
        metadata = [r for r in inputs['runtime_files'] if Path(r['path']).name == 'global-metadata.dat']
        if len(metadata) != 1 or metadata[0].get('sha256') != NATIVE_METADATA_SHA256:
            raise ValueError('Policy engine metadata differs from the original feature consumer')
        self.identity = deepcopy(identity)
        self.master = source['expected']['master_hash']
        self.produce = source['produce_id']
        self.thread = threading.get_ident()
        self.native_core = NATIVE_CORE_SHA256
        self.native_metadata = NATIVE_METADATA_SHA256
        self._stamps = []
        for reference in (identity['inputs_reference'], identity['source_reference'], inputs['reconstruction_manifest']):
            p = Path(reference['path']); s = p.stat()
            self._stamps.append((p, s.st_size, s.st_mtime_ns, s.st_ctime_ns))

    def validate_owner(self, observation, decision_type):
        if (os.getpid() != self.identity['worker_pid'] or threading.get_ident() != self.thread
                or observation.get('run_id') != self.identity['run_id']
                or observation.get('decision_type') != decision_type):
            raise ValueError('Policy observation does not belong to its current native owner/type')
        for p, size, modified, changed in self._stamps:
            s = p.stat()
            if (s.st_size, s.st_mtime_ns, s.st_ctime_ns) != (size, modified, changed):
                raise ValueError('Native policy source inputs changed during execution')

    def primary(self, observation):
        self.validate_owner(observation, 'main')
        prepared = prepare_original_pc_primary(observation)
        if prepared.state_before.get('produceId') != self.produce:
            raise ValueError('Observed policy mode differs from initialized source mode')
        return prepared


class NativePrimaryBcScorer:
    """One primary head, usable by the shared decision-level policy adapter."""
    def __init__(self, specification, *, engine_identity):
        self.source = OwnedNativePolicySource(engine_identity)
        contract_set = _read(specification['contract_set'])
        self.parameters, self.metadata = load_native_primary_bc_model(specification['model'],
            expected_contract_set_sha256=contract_set['contract_set_sha256'])
        if canonical_json_bytes(self.metadata['contract_set']) != canonical_json_bytes(contract_set):
            raise ValueError('Requested baseline and model contract sets differ')
        self.bridge = verify_primary_kernel_bridge(contract_set, specification['original_shared_encoder'])
        self.contract = resolve_native_structure_contract(contract_set, source_master_hash=self.source.master,
            produce_id=self.source.produce, native_core_sha256=self.source.native_core,
            native_metadata_sha256=self.source.native_metadata)
        native = self.contract['native_structure_contract']
        package = load_native_structure_package(source_master_hash=self.source.master,
            source_inventory_reference=native['source_inventory_reference'], native_schema_reference=native['native_schema_reference'],
            catalog_reference=native['catalog_reference'], snapshot_reference=native['snapshot_reference'])
        self.features = NativeStructureCardSemanticFeatures(package,
            active_status_contract=self.contract.get('observed_active_status_contract'))
        self.model_reference = deepcopy(specification['model'])
        self.layout = contract_set['pointer_layout']

    def encode(self, observation):
        prepared = self.source.primary(observation)
        contract = self.contract['observed_empty_collection_contract']
        validate_empty_collection_contract(contract, card_payload_sha256=self.contract['card_payload_sha256'],
            snapshot_fingerprint=self.contract['snapshot_fingerprint'])
        state, ledger = _semantic_view(prepared.state_before, PC_SOURCE,
            inactive_status_contract=contract.get('inactive_card_status_contract'),
            optional_identifiers_contract=contract.get('optional_effect_identifiers_contract'))
        flow = '|'.join((state['produceId'], shared.PLAN_BY_NATIVE_VALUE[state['planType']],
            shared.EFFECT_BY_NATIVE_VALUE[state['mainEffectType']]))
        encoded = shared.encode_primary_feature_view(raw_state=prepared.state_before, state_before=state,
            legal_candidates=prepared.legal_candidates, flow=flow, stage=shared.STAGE_BY_NATIVE_VALUE[state['stepType']],
            native_mask_complete=True, candidate_scope=shared.PRIMARY_SCOPE, features=self.features,
            contract=self.contract)
        if (not encoded.coverage['representation_complete'] or encoded.coverage['semantic_gap_tokens']):
            raise ValueError('Primary policy representation gap: ' + json.dumps({
                'contract': encoded.coverage['contract_gaps'], 'semantic': encoded.coverage['semantic_gap_tokens']}))
        return prepared, encoded, ledger

    def decide(self, observation):
        prepared, encoded, ledger = self.encode(observation)
        context, candidates = encoded.pointer_indices(context_buckets=self.layout['context_buckets'],
            candidate_buckets=self.layout['candidate_buckets'])
        width = max(map(len, candidates))
        values = np.zeros((1, len(candidates), width), dtype=np.int32)
        mask = np.zeros_like(values, dtype=np.float32)
        for i, row in enumerate(candidates):
            values[0, i, :len(row)] = row; mask[0, i, :len(row)] = 1
        probabilities, _ = _outer_forward(self.parameters, np.array([context], dtype=np.int32),
            np.ones((1, len(context)), dtype=np.float32), values, mask,
            np.ones((1, len(candidates)), dtype=np.float32), cache=False)
        target = int(np.argmax(probabilities[0]))
        selected = encoded.normalized_candidates[target]
        kind = {'use-hand': 1, 'use-drink': 2, 'turn-end': 3}[selected['kind']]
        return {'action': {'type': kind, 'indexes': [] if kind == 3 else [selected['slot_index']]},
            'policy_source': {'kind': 'primary-BC', 'model': self.model_reference,
                'encoder_bridge': self.bridge, 'teacher_action_used': False, 'legacy_primary_fallback': False},
            'diagnostics': {'feature_sha256': encoded.feature_sha256, 'raw_state_sha256': encoded.state_sha256,
                'candidate_order': list(encoded.candidate_bindings), 'selected_candidate': target,
                'probabilities': probabilities[0].tolist(), 'presence_view_changes': sum(x['semantic_view_changed'] for x in ledger),
                'new_observation_training_qualified': False}}


class MixedNativeBcPolicy:
    """Baseline's single decision interface; every branch names its source.

    This is deliberately a mixed baseline. The integrated model will implement
    the same interface with a joint representation/backbone instead of keeping
    this baseline's secondary rule as an unreported learned-model substitute.
    """
    def __init__(self, specification, *, engine_identity):
        self.primary = NativePrimaryBcScorer(specification, engine_identity=engine_identity)
        inventory_ref = self.primary.contract['native_structure_contract']['source_inventory_reference']
        inventory = _read(inventory_ref)
        matches = [x for x in inventory['versions'] if x['master_hash'] == self.primary.source.master]
        if len(matches) != 1:
            raise ValueError('Baseline secondary source Master is not uniquely bound')
        self.database = {**matches[0]['source_database'], 'source_master_hash': self.primary.source.master}

    def decide(self, observation):
        kind = observation.get('decision_type')
        if kind == 'main':
            return self.primary.decide(observation)
        if kind == 'secondary':
            self.primary.source.validate_owner(observation, kind)
            from .native_policy_secondary import decide_native_secondary
            enriched = {**observation, 'source_master_hash': self.primary.source.master,
                'native_metadata_sha256': self.primary.source.native_metadata}
            return decide_native_secondary(enriched, source_database_reference=self.database)
        raise ValueError('Unsupported native decision type: ' + str(kind))


__all__ = ['SCHEMA', 'OwnedNativePolicySource', 'NativePrimaryBcScorer', 'MixedNativeBcPolicy', 'verify_primary_kernel_bridge']
