"""Shared, evidence-bound live-PC eligibility for frozen model inference.

Actual execution identity remains current. Structural eligibility does not
assert equal calculations, current Master compatibility, training admission,
policy quality, workflow acceptance or permission to send game input.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path

from .observed_empty_collections import NATIVE_CORE_SHA256, NATIVE_METADATA_SHA256

SCHEMA = 'gkms.live-model-consumer-structural-eligibility.v1'
IDENTITY_SOURCE = 'ExamAdapter.initialize current-PC image/header and full metadata verification'
OLD_IDENTITY = {'metadata_sha256': NATIVE_METADATA_SHA256,
    'game_assembly_image_size': 0x0BE0E000, 'game_assembly_timestamp': 0x6A73E78D}
NEW_IDENTITY = {'metadata_sha256': '9349bc965fb08a434ab1c9547a3440a8ee02a05e761723792aabe5f4a9ecb635',
    'game_assembly_image_size': 0x0BE54000, 'game_assembly_timestamp': 0x6A996E38}
NEW_METHOD_PROFILE = 'pc-9349bc96-default'
EVIDENCE_REFERENCE = {
    'path': str(Path(__file__).resolve().parents[2] / 'var/research/post_update_live_20260919/model_consumer_eligibility_v1/receipt.json'),
    'sha256': '6f3699ad2670e67300bf556dbd02f318b329899901ba38ac19bdedb58888e907'}
_REQUIRED_CHECKS = ['original-serializer-before-after-equality', 'native-owner-and-session-binding',
    'complete-current-legal-candidate-pool', 'current-source-Master-binding', 'full-feature-coverage-with-no-required-gaps']
_CORE_TYPES = {'Campus.InGame.Exam.ExamSaveData', 'Campus.InGame.Exam.ExamParameterModel',
    'Campus.InGame.Exam.ExamPlayCommand', 'Campus.InGame.Card.ExamCardData'}
_LIMITS = {'training_admitted', 'model_quality_accepted', 'full_workflow_accepted',
    'native_calculation_equivalence_claimed', 'native_input_authority_granted',
    'current_Master_aliased', 'excluded_storage_type_compatibility_claimed'}


def _require(valid, reason):
    if not valid:
        raise ValueError(reason)


def _stamp(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


class _Evidence:
    def __init__(self):
        self.watched, self.documents = {}, {}

    def read(self, reference):
        path = Path(reference['path']).resolve()
        key = (str(path), reference['sha256'])
        if key in self.documents:
            return self.documents[key]
        before = _stamp(path); raw = path.read_bytes(); after = _stamp(path)
        _require(before == after and hashlib.sha256(raw).hexdigest() == reference['sha256'],
            'Current-PC consumer evidence changed or has the wrong SHA')
        self.watched[path] = after
        self.documents[key] = json.loads(raw)
        return self.documents[key]

    def validate_unchanged(self):
        _require(all(_stamp(path) == stamp for path, stamp in self.watched.items()),
            'Current-PC structural evidence changed during inference')


def _validate_evidence_documents(body, profile, shapes, typed):
    _require(body.get('schema') == SCHEMA and body.get('source_identity') == OLD_IDENTITY
        and body.get('target_identity') == NEW_IDENTITY and body.get('required_method_profile') == NEW_METHOD_PROFILE,
        'Consumer structural receipt does not bind both exact PC identities')
    _require(body.get('required_per_decision_checks') == _REQUIRED_CHECKS
        and isinstance(body.get('limits'), Mapping) and set(body['limits']) == _LIMITS
        and all(value is False for value in body['limits'].values()),
        'Structural eligibility cannot waive live checks or claim other qualifications')
    _require(profile.get('schema') == 'gkms.pc-version-method-contract-profile.v1'
        and profile.get('source_identity') == OLD_IDENTITY and profile.get('target_identity') == NEW_IDENTITY
        and len(profile.get('entries', [])) == 141 and len(profile.get('class_layouts', [])) == 56
        and profile.get('unknown_method_behavior') == 'reject', 'Exact default native method/layout profile required')
    _require(len(shapes.get('types', [])) == 44 and all(row.get('field_name_order_equal') is True for row in shapes['types'])
        and len(shapes.get('enums', [])) == 6
        and all(row.get('same_existing_encoded_defaults') is True and row.get('changed_encoded_default') == {}
            and row.get('removed_names') == [] for row in shapes['enums']),
        'Original/current declared field or existing enum default changes are unqualified')
    _require(typed.get('changed_fields') == [] and typed.get('live_layout_count') == 56,
        'An original/current field type difference cannot enter the model')
    covered = [row for row in typed.get('types', []) if not row.get('unproven') and not row.get('changed')]
    _require(len(covered) == 32 and sum(row['declared_fields'] for row in covered) == 293
        and _CORE_TYPES <= {row['type'] for row in covered}
        and all(row['declared_fields'] == row['direct_typed_equal'] + row['same_native_type_index_witness_equal'] for row in covered),
        'Mandatory root/card/command/status field types lack original typed evidence')
    coverage = body.get('structural_coverage', {})
    _require(coverage.get('typed_payload_classes') == covered and coverage.get('typed_payload_fields') == 293
        and coverage.get('changed_typed_payload_fields') == 0 and coverage.get('runtime_layouts') == 56
        and coverage.get('excluded_unproven_field_count') == len(typed['unproven_fields']) == 115,
        'Excluded storage or known field evidence has been silently reclassified')
    getters = coverage.get('candidate_position_authority', {}).get('getters', [])
    expected = {'get_CardData': ('Campus.InGame.Card.ExamCardData', 18), 'get_Index': ('System.Int32', 8),
        'get_CardPositionType': ('Campus.Common.Proto.Client.Enums.ProduceCardPositionType', 17)}
    _require(len(getters) == 3 and {row['source']['name'] for row in getters} == set(expected),
        'Candidate positions require all three original/current typed getters')
    for row in getters:
        matches = [entry for entry in profile['entries'] if entry['source'] == row['source']]
        _require(len(matches) == 1 and matches[0]['expected'] == row['expected']
            and matches[0].get('original_metadata_nominal_signature_equal') is True
            and matches[0].get('current_declaration_observed') is True,
            'Candidate position getter has no exact typed source/current binding')
        method = row['expected']; return_type = method['return_type']
        _require(method['namespace'] == 'Campus.InGame.Card' and method['type_path'] == ['ExamCardPositionData']
            and method['arity'] == 0 and method['static'] is False and return_type['byref'] is False
            and (return_type['name'], return_type['kind']) == expected[method['name']],
            'Unproven candidate position getter signature change')


@lru_cache(maxsize=1)
def _load_evidence(path, sha):
    reader = _Evidence()
    body = reader.read({'path': path, 'sha256': sha})
    docs = {name: reader.read(reference) for name, reference in body['evidence'].items()}
    profile = docs['checked_method_profile']
    _validate_evidence_documents(body, profile, docs['static_shapes'], docs['typed_fields'])
    for reference in body['runtime_read_reports']:
        reader.read(reference)
    for reference in body['raw_runtime_receipts']:
        reader.read(reference)
    # Verify the actual rows in each original RPC receipt, not just counts or
    # a self-declared successful profile. The one historical failed query stays
    # preserved; its later nested-class correction is a separate valid row.
    for family, entries in (('methods', profile['entries']), ('classes', profile['class_layouts'])):
        for entry in entries:
            witness = entry['evidence']; receipt = reader.read(witness['receipt'])
            report = receipt.get('pc_contracts', {})
            _require(receipt.get('request_id') == witness['request_id']
                and receipt.get('session_generation') == witness['session_generation']
                and receipt.get('status') == 'ok' and report.get('version_stable') is True,
                'Native method/layout evidence is not a bound read-only response')
            identity = report.get('version', {}).get('engine_identity', {})
            _require(all(identity.get(key) == value for key, value in NEW_IDENTITY.items()),
                'Native method/layout response belongs to another PC consumer')
            expected = entry['expected'] if family == 'methods' else entry['observed']
            rows = [row for row in report.get(family, []) if row.get('query_index') == witness['query_index']]
            _require(len(rows) == 1 and all(rows[0].get(key) == value for key, value in expected.items()),
                'Compiled profile differs from its actual original metadata read')
    reader.validate_unchanged()
    return reader, body


def _portable_consumer_reference(package):
    """Accept only the packaged derivative of the same complete source proof."""
    package.validate_unchanged()
    body = package.consumer_evidence()
    required = {'schema', 'source_identity', 'target_identity', 'required_method_profile',
                'required_per_decision_checks', 'limits', 'source_receipt_sha256', 'verified_reference',
                'build_source_evidence_verified', 'raw_RPCs_included'}
    _require(isinstance(body, Mapping) and set(body) == required
        and body.get('schema') == 'gkms.portable-PC-consumer-evidence.v1'
        and body.get('source_identity') == OLD_IDENTITY and body.get('target_identity') == NEW_IDENTITY
        and body.get('required_method_profile') == NEW_METHOD_PROFILE
        and body.get('source_receipt_sha256') == EVIDENCE_REFERENCE['sha256']
        and body.get('build_source_evidence_verified') is True and body.get('raw_RPCs_included') is False,
        'Portable consumer evidence must derive from the exact original/current PC source proof')
    _require(body.get('required_per_decision_checks') == _REQUIRED_CHECKS
        and isinstance(body.get('limits'), Mapping) and set(body['limits']) == _LIMITS
        and all(value is False for value in body['limits'].values()),
        'Portable consumer evidence cannot waive live checks or claim other qualifications')
    reference = body['verified_reference']
    _require(isinstance(reference, Mapping) and {'path', 'sha256'} <= set(reference) <= {'path', 'sha256', 'bytes'}
        and isinstance(reference.get('path'), str) and Path(reference['path']).is_absolute()
        and isinstance(reference.get('sha256'), str) and len(reference['sha256']) == 64
        and all(character in '0123456789abcdef' for character in reference['sha256'])
        and ('bytes' not in reference or type(reference['bytes']) is int and reference['bytes'] > 0),
        'Portable consumer evidence lacks its verified local package reference')
    package.validate_unchanged()
    return deepcopy(dict(reference))


def validate_live_pc_consumer_identity(engine):
    """Validate structural inference eligibility; retain actual execution ID."""
    _require(isinstance(engine, Mapping) and engine.get('identity_source') == IDENTITY_SOURCE
        and type(engine.get('game_assembly_image_size')) is int and type(engine.get('game_assembly_timestamp')) is int,
        'Verified current native consumer identity required')
    identity = {key: engine.get(key) for key in OLD_IDENTITY}
    evidence, route = None, 'original-pc-consumer'
    if identity == OLD_IDENTITY:
        _require(engine.get('method_profile') in (None, 'pc-9a6bf153-original'),
            'Original PC identity contradicts its method profile')
    elif identity == NEW_IDENTITY:
        _require(engine.get('method_profile') == NEW_METHOD_PROFILE,
            'Updated PC model input requires the verified default native method profile')
        from .portable_model_assets import configured_portable_assets
        package = configured_portable_assets()
        if package is None:
            reader, body = _load_evidence(EVIDENCE_REFERENCE['path'], EVIDENCE_REFERENCE['sha256'])
            reader.validate_unchanged()
            evidence, route = deepcopy(EVIDENCE_REFERENCE), 'updated-pc-structural-inference-only'
        else:
            evidence = _portable_consumer_reference(package)
            route = 'portable-derived-structural-inference-only'
    else:
        raise ValueError('Current PC consumer identity is outside the verified model adapter')
    return {'schema': 'gkms.validated-live-model-consumer.v1', 'route': route,
        'actual_engine_identity': deepcopy(dict(engine)), 'trained_consumer_identity': {**OLD_IDENTITY, 'native_core_sha256': NATIVE_CORE_SHA256},
        'evidence': evidence, 'inference_structure_eligible': True,
        'required_per_decision_checks': list(_REQUIRED_CHECKS),
        'native_calculation_equivalence_claimed': False, 'training_admitted': False,
        'model_quality_accepted': False, 'full_workflow_accepted': False,
        'native_input_authority_granted': False, 'current_Master_compatibility_verified': False}


__all__ = ['validate_live_pc_consumer_identity', 'EVIDENCE_REFERENCE', 'NEW_IDENTITY', 'NEW_METHOD_PROFILE']
