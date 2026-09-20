"""Explicit source routing for one native-structure BC representation.

This envelope binds the shared encoding and pointer layout across historical
Master packages. It does not qualify those sources, train a model, or establish
runtime compatibility. Each resolved contract still needs its verified package
and the existing shared feature validator before encoding.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from functools import lru_cache
import hashlib
from pathlib import Path

from .shared_bc_features import NATIVE_STRUCTURE_SCHEMA, NATIVE_STRUCTURE_ENCODING, PRIMARY_SCOPE, digest

SCHEMA = 'gkms.shared-bc-native-structure-contract-set.v1'
_COMMON = ('schema', 'encoding', 'card_schema', 'current_state_schema', 'candidate_scope',
    'encoder_source_hashes', 'native_plan_effects', 'native_stages', 'context_extension',
    'actual_payment', 'secondary_selection_complete', 'diagnostic_only', 'dataset_surface',
    'action_after_state_observed', 'RL_transition_qualified',
    'derived_arithmetic_required_for_representation', 'identity_schema')
_NATIVE_COMMON = ('schema', 'native_core_sha256', 'native_metadata_sha256',
    'native_schema_proof_sha256', 'numeric_enums_sha256', 'runtime_grow_fields', 'zones',
    'native_arithmetic', 'lineage_role', 'numeric_encoding', 'static_definition_reference_fields',
    'runtime_definition_reference_fields', 'target_filter_and_group_ids',
    'excluded_display_fields', 'excluded_local_identity_fields',
    'scope', 'diagnostic_only', 'action_after_state_observed', 'training_admitted')


def _sha(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


@lru_cache(maxsize=1)
def _router_source_sha():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _checked_digest(value):
    if not isinstance(value, Mapping) or not _sha(value.get('contract_sha256')):
        raise ValueError('A complete native feature contract digest is required')
    body = {k: v for k, v in value.items() if k != 'contract_sha256'}
    if digest(body) != value['contract_sha256']:
        raise ValueError('Native feature contract content differs from its digest')


def _common(contract):
    try:
        native = contract['native_structure_contract']
        empty = contract['observed_empty_collection_contract']
        return {'shared': {k: deepcopy(contract[k]) for k in _COMMON},
                'native': {k: deepcopy(native[k]) for k in _NATIVE_COMMON},
                'empty_view': {k: deepcopy(v) for k, v in empty.items() if k not in
                    ('card_payload_sha256', 'snapshot_fingerprint', 'contract_sha256')},
                'active_status': deepcopy(contract.get('observed_active_status_contract'))}
    except (KeyError, TypeError) as exc:
        raise ValueError('Native feature contract lacks common representation fields') from exc


def build_native_structure_contract_set(contracts, *, context_buckets=65536, candidate_buckets=65536):
    """Bind already-created v5 contracts; full package/source checks stay required."""
    if (not isinstance(contracts, Sequence) or isinstance(contracts, (str, bytes)) or not contracts
            or any(type(v) is not int or not 2 <= v <= 2**31 for v in (context_buckets, candidate_buckets))):
        raise ValueError('Nonempty native contracts and valid int32 pointer bucket sizes required')
    entries, routes, identities, common = {}, [], set(), None
    for contract in contracts:
        _checked_digest(contract)
        if (contract.get('schema') != NATIVE_STRUCTURE_SCHEMA or contract.get('encoding') != NATIVE_STRUCTURE_ENCODING
                or contract.get('candidate_scope') != PRIMARY_SCOPE or contract.get('diagnostic_only') is not True
                or contract.get('action_after_state_observed') is not False
                or contract.get('RL_transition_qualified') is not False):
            raise ValueError('Only explicit diagnostic BC-only native v5 contracts are supported')
        native = contract.get('native_structure_contract')
        _checked_digest(native)
        current = _common(contract)
        if common is None:
            common = current
        elif digest(common) != digest(current):
            raise ValueError('Source packages do not share the same representation and encoder version')
        if (native.get('card_payload_sha256') != contract.get('card_payload_sha256')
                or native.get('snapshot_fingerprint') != contract.get('snapshot_fingerprint')
                or any(not _sha(native.get(k)) for k in
                       ('source_master_hash', 'native_core_sha256', 'native_metadata_sha256', 'card_payload_sha256'))):
            raise ValueError('Native source package and shared contract identity differ')
        sha = contract['contract_sha256']
        if sha in entries:
            raise ValueError('Duplicate native feature contract in the source set')
        entries[sha] = deepcopy(dict(contract))
        modes = contract.get('produce_ids')
        if (not isinstance(modes, list) or not modes or modes != sorted(set(modes))
                or any(x not in ('produce-004', 'produce-005') for x in modes)):
            raise ValueError('Native source routes need explicit supported mode identities')
        for mode in modes:
            key = (native['source_master_hash'], mode, native['native_core_sha256'], native['native_metadata_sha256'])
            if key in identities:
                raise ValueError('Ambiguous source route; no default or first-package fallback is allowed')
            identities.add(key)
            routes.append(dict(zip(('source_master_hash', 'produce_id', 'native_core_sha256', 'native_metadata_sha256'), key),
                contract_sha256=sha))
    routes.sort(key=lambda x: (x['source_master_hash'], x['produce_id'], x['native_core_sha256'], x['native_metadata_sha256']))
    body = {'schema': SCHEMA, 'common_representation': common,
        'representation_sha256': digest(common), 'router_source_sha256': _router_source_sha(),
        'pointer_layout': {'context_buckets': context_buckets, 'candidate_buckets': candidate_buckets,
            'candidate_feature_prefix': 'exact-candidate-field:', 'integer_storage': 'little-endian-int32',
            'padding_index': 0, 'context_hash': 'existing-pointer-hash', 'candidate_hash': 'existing-pointer-hash-deduplicated'},
        'contracts': {k: entries[k] for k in sorted(entries)}, 'routes': routes,
        'unknown_source_policy': 'reject', 'source_package_validation_required': True,
        'current_runtime_master_compatibility_qualified': False,
        'diagnostic_only': True, 'bc_fit_admitted': False, 'RL_transition_qualified': False}
    return {**body, 'contract_set_sha256': digest(body)}


def validate_native_structure_contract_set(contract_set):
    """Validate envelope structure/version, not referenced source files or fit."""
    if not isinstance(contract_set, Mapping):
        raise ValueError('Native source contract set required')
    try:
        expected = build_native_structure_contract_set(list(contract_set['contracts'].values()),
            context_buckets=contract_set['pointer_layout']['context_buckets'],
            candidate_buckets=contract_set['pointer_layout']['candidate_buckets'])
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('Native source contract set is incomplete') from exc
    if digest(contract_set) != digest(expected):
        raise ValueError('Native source contract set routes, version, or layout differ')
    return contract_set


def resolve_native_structure_contract(contract_set, *, source_master_hash, produce_id,
                                      native_core_sha256, native_metadata_sha256):
    validate_native_structure_contract_set(contract_set)
    key = (source_master_hash, produce_id, native_core_sha256, native_metadata_sha256)
    rows = [x for x in contract_set['routes'] if tuple(x[k] for k in
        ('source_master_hash', 'produce_id', 'native_core_sha256', 'native_metadata_sha256')) == key]
    if len(rows) != 1:
        raise ValueError('Unknown or ambiguous native source route')
    return deepcopy(contract_set['contracts'][rows[0]['contract_sha256']])


__all__ = ['SCHEMA', 'build_native_structure_contract_set', 'validate_native_structure_contract_set',
    'resolve_native_structure_contract']
