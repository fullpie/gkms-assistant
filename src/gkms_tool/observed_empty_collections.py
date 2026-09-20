"""Source-bound diagnostic semantics for two original PC optional lists.

Hash-bound qualified PC capture reports or frozen native B88 receipts construct
an admitted input. The original state remains raw; a separate feature view consumes proven null-list semantics.
This is neither JSON inversion nor training/source-equivalence qualification.
"""
from __future__ import annotations

from collections.abc import Mapping
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import secrets

from .original_pc_primary_input import prepare_original_pc_primary, bind_original_primary_label
from .training_artifact_io import canonical_json_bytes

SCHEMA = 'gkms.observed-empty-collections-input.v1'
NATIVE_EVIDENCE_SHA256 = '747e2128043b78742cac876e90691d38617c7724b13c8aaa7abb3515e1e4c1a3'
NATIVE_CORE_SHA256 = '1e730b223cab48c3cf80837d7cd04b1b6b6eb04452135fb01c7d655aaf52017a'
NATIVE_METADATA_SHA256 = '9a6bf153c0c42a2768e619cc7d96fa79fcc1d341e9cf9bcbdc8d6bca48812668'
INACTIVE_STATUS_EVIDENCE_SHA256 = '1b1051c0a80295dbc31b279a741e545181fac555807c95f4988fb2e8e554c476'
OPTIONAL_IDENTIFIER_EVIDENCE_SHA256 = '7569a60fc16576d39d5e2ec17e30f567b79e6e5af72c6f453bd9ef03201cfe02'
PRIMARY_INVENTORY_CLOSURE_SHA256 = '10e4742cbd2cd85940d0e2262d70265db5de023381c72426115dad6d56e24b4d'
PC_SOURCE = 'original-PC-direct-snapshot'
JSON_SOURCE = 'qualified-native-JSON'
ZONES = ('handList', 'deckList', 'graveList', 'lostList', 'holdList')
FIELD_CONTRACTS = (
    {'path_suffix': '/_staminaConsumptionSpecifyEffectList',
     'owner': 'Campus.InGame.Card.ExamCardData', 'field_token': '0x040065e0',
     'offset': 0xa8, 'declared_type': 'System.Collections.Generic.List<Campus.InGame.Exam.ExamCardValueStatusEffect>'},
    {'path_suffix': '/_cardData/_customizeCountList',
     'owner': 'Campus.InGame.Card.ProduceCardData', 'field_token': '0x04006521',
     'offset': 0x28, 'declared_type': 'System.Collections.Generic.List<System.Int32>'},
)
_CONTEXT_SECRET = secrets.token_bytes(32)


def _digest(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def inactive_card_status_contract(*, proof_path):
    reference = {'path': str(Path(proof_path).resolve()), 'sha256': INACTIVE_STATUS_EVIDENCE_SHA256}
    proof = _read_ref(reference)
    if (proof['source_image']['sha256'] != NATIVE_CORE_SHA256
            or proof['source_metadata']['sha256'] != NATIVE_METADATA_SHA256
            or proof['field']['field_token'] != '0x40065d4'
            or proof['field']['offset'] != 0x48
            or proof['allowed_semantic_view']['model_value'] != {}):
        raise ValueError('Inactive card-status consumer proof differs')
    body = {'schema': 'gkms.observed-inactive-card-status-input.v1', 'proof': reference,
        'native_core_sha256': NATIVE_CORE_SHA256, 'native_metadata_sha256': NATIVE_METADATA_SHA256,
        'field': {'path_suffix': '/_statusEffect', 'owner': 'Campus.InGame.Card.ExamCardData',
            'field_token': '0x040065d4', 'offset': 0x48,
            'declared_type': 'Campus.InGame.Card.ProduceCardStatusEffect'},
        'meaning': 'no-triggered-card-status-for-original-dispatcher',
        'native_json_shape': deepcopy(proof['allowed_semantic_view']['native_json_shape']),
        'semantic_model_value': {}, 'original_object_identity_equivalent': False,
        'all_consumer_equivalence_claimed': False, 'numeric_value_recovery_claimed': False,
        'missing_or_other_shape': 'preserve-raw-unresolved', 'diagnostic_only': True}
    return {**body, 'contract_sha256': _digest(body)}


def optional_effect_identifier_contract(*, proof_path):
    reference = {'path': str(Path(proof_path).resolve()), 'sha256': OPTIONAL_IDENTIFIER_EVIDENCE_SHA256}
    proof = _read_ref(reference)
    if (proof['source_core_sha256'] != NATIVE_CORE_SHA256 or proof['source_metadata_sha256'] != NATIVE_METADATA_SHA256
            or proof['parent']['field_token'] != '04004dea'
            or proof['parent']['element_type'] != 'Campus.InGame.Card.ProduceExamEffect'):
        raise ValueError('Optional effect-identifier type/consumer proof differs')
    body = {'schema': 'gkms.observed-optional-effect-identifiers.v1', 'proof': reference,
        'native_core_sha256': NATIVE_CORE_SHA256, 'native_metadata_sha256': NATIVE_METADATA_SHA256,
        'parent_type': {'class': 'TriggerEffectStatusEffect', 'ns': 'Campus.InGame.Exam', 'asm': 'Assembly-CSharp'},
        'parent_field': '_effectList', 'parent_field_token': '04004dea',
        'element_type': 'Campus.InGame.Card.ProduceExamEffect',
        'fields': [{'name': x['name'], 'field_token': x['token']} for x in proof['fields']],
        'semantic_model_value': '', 'meaning': 'identifier absent: same ResolveData guard outcome only',
        'cached_object_absence_claimed': False, 'inactive_effect_claimed': False,
        'missing_or_nonempty_or_invalid': 'preserve-raw-unresolved', 'diagnostic_only': True}
    return {**body, 'contract_sha256': _digest(body)}


def empty_collection_contract(*, card_payload_sha256, snapshot_fingerprint, inactive_status_contract=None,
                              optional_identifiers_contract=None):
    if any(not isinstance(x, str) or len(x) != 64 or any(c not in '0123456789abcdef' for c in x)
           for x in (card_payload_sha256, snapshot_fingerprint)):
        raise ValueError('Empty-collection diagnostics require a bound semantic catalog/fingerprint')
    body = {'schema': SCHEMA, 'native_evidence_sha256': NATIVE_EVIDENCE_SHA256,
        'native_core_sha256': NATIVE_CORE_SHA256, 'native_metadata_sha256': NATIVE_METADATA_SHA256,
        'card_payload_sha256': card_payload_sha256,
        'snapshot_fingerprint': snapshot_fingerprint, 'zones': list(ZONES),
        'fields': deepcopy(list(FIELD_CONTRACTS)), 'source_kinds': [PC_SOURCE, JSON_SOURCE],
        'admitted_source_scope': 'same-PC-consumer-version-qualified-capture-report-or-frozen-native-B88-receipts',
        'source_Master_required_to_equal_semantic_catalog': False,
        'Master_compatibility_qualified_by_this_contract': False,
        'semantic_rule': 'observed-PC-null-or-observed-empty-list-means-known-empty-list',
        'missing_rule': 'remain-missing-unresolved', 'raw_state_preserved': True,
        'JSON_inversion_claimed': False, 'actual_payment_proved': False,
        'diagnostic_only': True, 'training_admitted': False}
    if inactive_status_contract is not None:
        if (not isinstance(inactive_status_contract, Mapping)
                or 'proof' not in inactive_status_contract
                or dict(inactive_status_contract) != inactive_card_status_contract(
                    proof_path=inactive_status_contract['proof']['path'])):
            raise ValueError('Inactive card status requires its explicit original dispatcher proof')
        body['inactive_card_status_contract'] = deepcopy(dict(inactive_status_contract))
    if optional_identifiers_contract is not None:
        if (not isinstance(optional_identifiers_contract, Mapping) or 'proof' not in optional_identifiers_contract
                or _digest(optional_identifiers_contract) != _digest(optional_effect_identifier_contract(
                    proof_path=optional_identifiers_contract['proof']['path']))):
            raise ValueError('Optional identifiers require their exact original resolver/type proof')
        body['optional_effect_identifiers_contract'] = deepcopy(dict(optional_identifiers_contract))
    return {**body, 'contract_sha256': _digest(body)}


def validate_empty_collection_contract(contract, *, card_payload_sha256, snapshot_fingerprint):
    expected = empty_collection_contract(card_payload_sha256=card_payload_sha256,
        snapshot_fingerprint=snapshot_fingerprint, inactive_status_contract=(
            contract.get('inactive_card_status_contract') if isinstance(contract, Mapping) else None),
        optional_identifiers_contract=(contract.get('optional_effect_identifiers_contract') if isinstance(contract, Mapping) else None))
    if not isinstance(contract, Mapping) or dict(contract) != expected:
        raise ValueError('Empty-collection source/field/consumer contract differs')


def _file_stamp(path):
    path=Path(path).resolve(); stat=path.stat()
    return (str(path),stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)


def _read_bytes_ref(reference, *, file_stamps=None):
    if not isinstance(reference, Mapping):
        raise ValueError('Original file reference required')
    path = Path(reference['path'])
    stamp = _file_stamp(path)
    if 'byte_offset' in reference:
        if (type(reference['byte_offset']) is not int or reference['byte_offset'] < 0
                or type(reference.get('bytes')) is not int or reference['bytes'] <= 0):
            raise ValueError('Original byte-span integers required')
        with path.open('rb') as stream:
            stream.seek(reference['byte_offset'])
            raw = stream.read(reference['bytes'])
    else:
        raw = path.read_bytes()
    if (hashlib.sha256(raw).hexdigest() != reference.get('sha256')
            or ('bytes' in reference and len(raw) != reference['bytes'])):
        raise ValueError('Original source receipt hash/length differs')
    if _file_stamp(path) != stamp:
        raise ValueError('Original source file changed while its receipt was read')
    if file_stamps is not None:
        file_stamps.append(stamp)
    return raw


def _read_ref(reference):
    return json.loads(_read_bytes_ref(reference))


def _verify_consumer_proof(proof_path):
    proof_ref = {'path': str(Path(proof_path).resolve()), 'sha256': NATIVE_EVIDENCE_SHA256}
    proof = _read_ref(proof_ref)
    if (proof['source_image']['sha256'] != NATIVE_CORE_SHA256
            or proof['source_metadata']['sha256'] != NATIVE_METADATA_SHA256):
        raise ValueError('Original PC consumer source binding differs')
    return proof_ref, proof


@dataclass(frozen=True)
class ObservedEmptyCollectionInput:
    state_before: Mapping
    legal_candidates: tuple[Mapping, ...]
    source_header: Mapping
    source_context: 'VerifiedPcEmptySource | None' = None


@dataclass(frozen=True)
class VerifiedPcEmptySource:
    """Process-local immutable provenance context; contains no captured states."""
    payload_json: bytes
    file_stamps: tuple[tuple, ...]
    seal: str


def _context_signature(payload_json, file_stamps):
    value=canonical_json_bytes({'payload_sha256':hashlib.sha256(payload_json).hexdigest(),'file_stamps':file_stamps})
    return hmac.new(_CONTEXT_SECRET,value,hashlib.sha256).hexdigest()


def _validated_source_context(context):
    if (not isinstance(context,VerifiedPcEmptySource)
            or not hmac.compare_digest(context.seal,_context_signature(context.payload_json,context.file_stamps))):
        raise ValueError('Verified process-local original source context required')
    if any(_file_stamp(stamp[0]) != stamp for stamp in context.file_stamps):
        raise ValueError('A verified source file changed; rebuild the per-case context')
    return json.loads(context.payload_json)


def prepare_observed_empty_collection_input(*, proof_path, row_index, source_kind):
    """Read and verify original frozen sources, never trust a caller's flags.

    The frozen proof anchors all 88 rows and their original provenance. PC
    snapshots additionally pass the existing complete/pure primary-mask reader.
    The native branch uses the already-qualified transition only after checking
    its original raw state and complete original legal mask against that row.
    """
    if type(row_index) is not int or not 0 <= row_index < 88:
        raise ValueError('Frozen B88 row index must be a non-bool integer')
    if source_kind not in (PC_SOURCE, JSON_SOURCE):
        raise ValueError('Unknown original empty-collection source kind')
    proof_ref, proof = _verify_consumer_proof(proof_path)
    index = _read_ref(proof['source_observations'])['row_references']
    if len(index) != 88:
        raise ValueError('Original B88 source scope differs')
    receipt = index[row_index]
    row = _read_ref(receipt['paired_row'])
    _read_ref(row['source'])
    if (row['native_reference'] != receipt['native_reference']
            or row['pc_reference'] != receipt['PC_reference']):
        raise ValueError('Original source/field reference binding differs')
    if source_kind == PC_SOURCE:
        event = _read_ref(row['pc_reference'])
        prepared = prepare_original_pc_primary(event)
        state, candidates = prepared.state_before, prepared.legal_candidates
        observation = prepared.observation
        source_references = {'state_and_mask': row['pc_reference']}
    else:
        native = _read_ref(row['native_reference'])['body']
        qualified = _read_ref(row['qualified_reference'])
        state = native['state_before']
        if (qualified.get('schema') != 'gkms.nia-inner-transition.v1'
                or qualified.get('candidate_set_kind') != 'unified'
                or qualified.get('metadata', {}).get('policy_surface') != 'main-action-v1'
                or qualified.get('metadata', {}).get('simulator_used') is not False
                or any(qualified['state_before'].get(k) != v for k, v in state.items())
                or native['legal_actions'].get('complete') is not True
                or native['legal_actions'].get('legal_actions_complete') is not True
                or native['legal_actions']['actions'] != qualified['legal_candidates']
                or _digest(state) != row['native_reference']['state_sha256']):
            raise ValueError('Qualified native JSON raw state/mask receipt differs')
        candidates = tuple(qualified['legal_candidates'])
        observation = {'authority': 'original-native-JSON-with-existing-qualified-transition',
            'source': qualified['source'], 'trajectory_id': qualified['trajectory_id'],
            'capture_source_id': qualified['capture_source_id'],
            'original_raw_state_flags': row['native_reference']['original_flags'],
            'new_training_admitted': False}
        source_references = {'raw_state_and_mask': row['native_reference'],
                             'qualified_transition': row['qualified_reference']}
    header = {'schema': SCHEMA, 'source_kind': source_kind, 'source_factory': 'frozen-B88-receipt', 'proof': proof_ref,
        'row_index': row_index, 'paired_row': receipt['paired_row'], 'original_source': row['source'],
        'source_expected': row['source_expected'], 'source_references': source_references,
        'original_observation': observation, 'native_evidence_sha256': NATIVE_EVIDENCE_SHA256,
        'native_core_sha256': NATIVE_CORE_SHA256, 'native_metadata_sha256': NATIVE_METADATA_SHA256,
        'fields': deepcopy(list(FIELD_CONTRACTS)), 'raw_state_sha256': _digest(state),
        'candidate_sha256': _digest(candidates), 'diagnostic_only': True,
        'native_game_equivalence_claimed': False, 'training_admitted': False}
    return ObservedEmptyCollectionInput(deepcopy(state), tuple(deepcopy(candidates)), header)


def verify_pc_empty_collection_source(*, proof_path, qualification_reference):
    """Verify immutable per-case provenance once, without retaining raw states.

    A caller cannot admit a raw snapshot with a source-kind string. This
    factory reads the report, its inputs, native reconstruction identity,
    metadata, frozen readers, original source, full observer-file receipt and
    exact event byte span. Whole-episode training qualification stays separate.
    """
    return _verify_pc_collection_source(proof_path=proof_path, qualification_reference=qualification_reference)


def verify_pc_primary_span_source(*, proof_path, qualification_reference, closure_reference):
    """Reuse sealed whole-stream qualification; verify each consumed main span.

    This BC-only scope never claims to hash the entire raw stream again. The
    existing full-file entry above retains its behavior. A fixed inventory
    closure must bind the exact case, original source and event-index SHA;
    the common owner/reader/source checks still run and every later input read
    verifies its indexed bytes and file identity through the same typed factory.
    """
    if (not isinstance(closure_reference, Mapping)
            or closure_reference.get('sha256') != PRIMARY_INVENTORY_CLOSURE_SHA256):
        raise ValueError('Primary span scope requires the sealed original inventory closure')
    return _verify_pc_collection_source(proof_path=proof_path, qualification_reference=qualification_reference,
        primary_closure_reference=closure_reference)


def _verify_pc_collection_source(*, proof_path, qualification_reference, primary_closure_reference=None):
    proof_ref, _ = _verify_consumer_proof(proof_path)
    stamps=[]
    def binary(reference):return _read_bytes_ref(reference,file_stamps=stamps)
    def read(reference):return json.loads(binary(reference))
    read(proof_ref)
    report = read(qualification_reference)
    span_receipt = None
    if primary_closure_reference is not None:
        closure = read(primary_closure_reference)
        if (closure.get('schema') != 'gkms.shared-primary-feature-inventory-closure-manifest.v1'
                or closure.get('inventory_cases') != 2810 or closure.get('inventory_failures') != 0
                or closure.get('encoded_and_label_bound_rows') != 70888):
            raise ValueError('Sealed primary inventory scope differs')
        case_refs = [x for x in closure['files'] if Path(x['path']).name == 'case_distribution.json']
        if len(case_refs) != 1:
            raise ValueError('Sealed primary inventory case index is missing or ambiguous')
        cases = read(case_refs[0])
        matched = [x for x in cases if x.get('qualification') == dict(qualification_reference)]
        if len(matched) != 1:
            raise ValueError('Qualification is not a unique member of the sealed primary inventory')
        case = matched[0]
        if (case.get('inventory_complete') is not True or case['index'] != report.get('original_index')
                or case['episode_id'] != report.get('episode_id') or case['source'] != report.get('source')
                or case['event_index'] != report.get('event_index')
                or case['counts'].get('encoded_rows') != report.get('main_decisions')
                or case['counts'].get('label_bound_rows') != report.get('main_decisions')):
            raise ValueError('Sealed primary inventory and qualified case identity differ')
        span_receipt = {'scope': 'prior-whole-stream-qualified+current-indexed-main-span-sha',
            'closure_reference': deepcopy(dict(primary_closure_reference)),
            'case_index_reference': deepcopy(case_refs[0]), 'original_index': case['index'],
            'episode_id': case['episode_id'], 'event_index_reference': deepcopy(case['event_index']),
            'current_whole_stream_rehashed': False, 'all_consumed_spans_hash_required': True,
            'primary_BC_only': True, 'action_after_state_observed': False, 'training_admitted': False}
    required = ('source_verified_against_original_export', 'source_spine_passed',
        'required_responses_covered',
        'scoped_owned_capture_verified', 'case_cleanup_passed',
        'parent_membership_passed', 'parent_shared_cleanup_passed')
    if (report.get('status') != 'file-validated' or any(report.get(k) is not True for k in required)
            or type(report.get('source_actions')) is not int or report['source_actions'] <= 0
            or type(report.get('strict_before_after_equal')) is not int
            or report['strict_before_after_equal'] != report['source_actions']
            or type(report.get('source_responses_covered')) is not int
            or report['source_responses_covered'] != report['source_actions']
            or report.get('capture_qualification_gaps') != [] or report.get('source_quarantined') is not False):
        raise ValueError('A successful nonquarantined scoped owned-capture qualification report is required')
    inputs = read(report['inputs_receipt'])
    if inputs.get('executed_from_source_snapshot') is not True or inputs.get('game_process_used') is not False:
        raise ValueError('Original PC executed-reader snapshot receipt required')
    output = Path(report['output']).resolve(); parent_path = Path(report['parent']).resolve()
    if (Path(report['inputs_receipt']['path']).resolve() != output/'inputs.json'
            or Path(report['case_receipt']['path']).resolve() != output/'pc_replay_batch_episode.json'
            or Path(inputs['owned_batch_parent']).resolve() != parent_path
            or output.parent != parent_path/'cases'):
        raise ValueError('Qualified source is not bound to the original owned case/parent paths')
    episode = read(report['case_receipt'])
    if (episode.get('status') != 'completed' or episode.get('inputs') != report['inputs_receipt']
            or Path(episode['output']).resolve() != output
            or Path(episode['source_path']).resolve() != output/'source_snapshot/replay_source.json'
            or episode.get('source_sha256') != report['source']['sha256']
            or episode.get('case_id') != inputs.get('batch_case_id')):
        raise ValueError('Original sealed case/input/source receipt binding differs')
    read({'path':episode['source_path'],'sha256':episode['source_sha256']})
    parent_file = parent_path/'pc_replay_batch.json'
    parent_reference = {'path':str(parent_file),'sha256':hashlib.sha256(parent_file.read_bytes()).hexdigest()}
    parent = read(parent_reference)
    if (parent.get('schema') != 'gkms.original-pc-owned-replay-batch.v1'
            or parent.get('single_managed_owner') is not True or parent.get('game_process_used') is not False):
        raise ValueError('Original owned parent receipt required')
    journal = [json.loads(line) for line in binary(parent['completed_journal']).splitlines() if line]
    journal_rows = [x for x in journal if x.get('case_id') == episode['case_id']]
    if (len(journal_rows) != 1 or journal_rows[0].get('receipt') != report['case_receipt']
            or journal_rows[0].get('source_sha256') != report['source']['sha256']
            or journal_rows[0].get('identity') != episode.get('identity')):
        raise ValueError('Sealed parent journal does not contain this original case receipt')
    parent_inputs = read(parent['prepared_inputs'])
    matches = [x for x in parent_inputs['cases'] if x.get('case_id') == episode['case_id']]
    if (len(matches) != 1 or matches[0].get('inputs') != report['inputs_receipt']
            or matches[0].get('source_sha256') != report['source']['sha256']
            or Path(matches[0]['output']).resolve() != output):
        raise ValueError('Original prepared parent inputs do not bind this case/source')
    execution = read(episode['replay_receipt'])
    if (execution.get('native_replay_complete') is not True
            or execution.get('source_action_cursor_complete') is not True
            or execution['observer'].get('hooks_restored') is not True
            or execution['observer'].get('errors') != []):
        raise ValueError('Original sealed execution/observer completion differs')
    reconstruction = read(inputs['reconstruction_manifest'])
    if (reconstruction.get('schema') != 'gkms.pc-core-rebuilt-pe.v1'
            or reconstruction.get('source_image_sha256') != NATIVE_CORE_SHA256
            or reconstruction.get('original_executable_bytes_unchanged') is not True):
        raise ValueError('Capture uses a different original PC executable consumer')
    metadata = [x for x in inputs['runtime_files'] if Path(x['path']).name == 'global-metadata.dat']
    if (len(metadata) != 1 or metadata[0].get('sha256') != NATIVE_METADATA_SHA256
            or metadata[0].get('copy_sha256') != NATIVE_METADATA_SHA256):
        raise ValueError('Capture uses different original PC field metadata')
    binary({'path': metadata[0]['path'], 'sha256': NATIVE_METADATA_SHA256})
    readers = {Path(x['copy']).name: x for x in inputs['source_artifacts']}
    identity = report['reader_identity']
    if (identity['reconstruction'] != inputs['reconstruction_manifest']['sha256']
            or identity['master_manifest'] != inputs['master_manifest']['sha256']):
        raise ValueError('Qualification reader/core/Master input identity differs')
    for name in ('pc_exam_state.py', 'pc_step_observer.py', 'pc_exam_candidates.py'):
        reader = readers[name]
        if identity['sources'].get(name) != reader['sha256']:
            raise ValueError('Qualification is not bound to the original snapshot reader')
        binary({'path': reader['copy'], 'sha256': reader['sha256']})
    master = read(inputs['master_manifest'])
    source = read(report['source'])
    if inputs.get('original_replay_sha256') != report['source']['sha256']:
        raise ValueError('Capture inputs differ from the qualified original replay source')
    export = read(inputs['replay_export_manifest'])
    spec = report['specification']; original_index = report.get('original_index')
    if (type(original_index) is not int or original_index < 0
            or type(spec.get('index')) is not int or spec['index'] != original_index
            or original_index >= len(export.get('episodes', []))):
        raise ValueError('Original qualified specification/source/export index differs')
    exported = export['episodes'][original_index]
    original_identity = source['source']
    if (spec.get('source') != report['source'] or exported.get('source') != report['source']
            or spec.get('episode_id') != report.get('episode_id')
            or spec.get('episode_id') != exported.get('episode_id')
            or spec.get('episode_id') != original_identity.get('episode_id')
            or spec.get('trajectory_id') != exported.get('trajectory_id')
            or spec.get('trajectory_id') != original_identity.get('trajectory_id')
            or spec.get('player') != exported.get('player')
            or spec.get('original_split') != exported.get('original_split')
            or spec.get('original_split') != original_identity.get('split')
            or spec.get('master_hash') != exported.get('source_master_hash')
            or spec.get('master_hash') != source['expected'].get('master_hash')
            or spec.get('master_hash') != master.get('archived_master_hash')
            or spec.get('original_actions') != exported.get('original_actions')
            or spec.get('original_actions') != len(source['expected']['actions'])
            or spec.get('stage') != source.get('step_type')
            or exported.get('produce_id') != source.get('produce_id')
            or exported.get('step_type') != source.get('step_type')
            or exported['player'].get('raw_source_sha256') != original_identity.get('raw_sha256')
            or inputs.get('batch_case_id') != spec['episode_id']+'/'+inputs.get('context_policy','')):
        raise ValueError('Original qualified specification/source/export identity differs')
    observer = read(report['observer_validation'])
    if (observer.get('schema') != 'gkms.pc-observer-validation.v1'
            or observer['evidence']['source']['sha256'] != report['source']['sha256']
            or any(observer.get(k, {}).get('passed') is not True for k in
                   ('source', 'event_order', 'wrapper_pairing', 'accepted_spine', 'decision_association'))):
        raise ValueError('Original observer/source validation receipt differs')
    membership = observer['candidate_membership']
    if (membership.get('all_main_family_predicates_observed') is not True
            or membership.get('all_required_decision_responses_covered') is not True
            or type(membership.get('source_responses_covered')) is not int
            or membership['source_responses_covered'] != report['source_actions']):
        raise ValueError('Original main-family predicates or required source responses are incomplete')
    events_ref = observer['evidence']['events']
    if (Path(events_ref['path']).resolve() != output/'observed_steps.jsonl'
            or Path(execution['observer']['path']).resolve() != output/'observed_steps.jsonl'
            or observer['evidence']['execution'] != episode['replay_receipt']):
        raise ValueError('Observer events are not the original owned case stream')
    event_index = read(report['event_index']) if 'event_index' in report else None
    if event_index is not None and (event_index.get('schema') != 'gkms.qualified-observer-decision-event-index.v1'
            or event_index.get('whole_event_stream_hashed') is not True
            or event_index['events'] != events_ref or event_index['source'] != report['source']
            or event_index['inputs_receipt'] != report['inputs_receipt']
            or event_index['observer_validation'] != report['observer_validation']):
        raise ValueError('Qualified decision-event index is not bound to this capture/source')
    event_stamp=_file_stamp(events_ref['path'])
    if span_receipt is None:
        # Preserve the existing full-file validator and its exact scope.
        hasher=hashlib.sha256(); length=0; line_count=0
        with Path(events_ref['path']).open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):
                hasher.update(chunk); length+=len(chunk);line_count+=chunk.count(b'\n')
        if (hasher.hexdigest()!=events_ref['sha256'] or length!=events_ref['bytes']
                or _file_stamp(events_ref['path'])!=event_stamp
                or type(execution['observer'].get('records')) is not int
                or line_count != execution['observer']['records']
                or line_count != observer['event_order']['events']):
            raise ValueError('Original observer stream differs from its qualified receipt')
    else:
        if (event_index is None or event_stamp[3] != events_ref['bytes']
                or type(execution['observer'].get('records')) is not int
                or execution['observer']['records'] != observer['event_order']['events']):
            raise ValueError('Prior full stream receipt, index or current file size differs')
        # The qualification/event index are pinned through the sealed closure;
        # current bytes are checked when each indexed primary event is consumed.
        main = [x for x in event_index['decision_events'] if x.get('kind') == 1 and x.get('phase') == 0]
        if (len(main) != report['main_decisions']
                or len({x['source_order'] for x in main}) != len(main)
                or any(Path(x['path']).resolve() != Path(events_ref['path']).resolve()
                    or type(x.get('source_order')) is not int or type(x.get('byte_offset')) is not int
                    or type(x.get('bytes')) is not int or x['byte_offset'] < 0 or x['bytes'] <= 0
                    or x['byte_offset'] + x['bytes'] > events_ref['bytes'] for x in main)):
            raise ValueError('Sealed primary event spans differ from the qualified source scope')
    stamps.append(event_stamp)
    # Only provenance, source actions and event-index/membership receipts are
    # retained. Neither observed states nor semantic states are cached.
    payload={'proof':proof_ref,'qualification_reference':deepcopy(dict(qualification_reference)),
        'report':report,'source':source,'events_reference':events_ref,
        'candidate_membership':observer['candidate_membership']['rows'],
        'event_index':event_index,'owned_parent_receipt':parent_reference,
        'owned_episode_receipt':report['case_receipt'],'export_manifest_reference':inputs['replay_export_manifest'],
        'verified_export_identity':exported}
    if span_receipt is not None:
        payload['stream_verification'] = span_receipt
    payload_json=canonical_json_bytes(payload)
    frozen_stamps=tuple(sorted(set(stamps)))
    context=VerifiedPcEmptySource(payload_json,frozen_stamps,_context_signature(payload_json,frozen_stamps))
    _validated_source_context(context)
    return context


def prepare_pc_empty_collection_input(*, event_reference, source_context=None,
                                     proof_path=None, qualification_reference=None):
    """Read one exact decision from a verified case; never rehash its whole stream.

    Without a context this convenience entry first performs case verification.
    Reuse `verify_pc_empty_collection_source`'s immutable context across a case.
    Every input read and encoding rechecks file identity and the exact event SHA.
    """
    if source_context is None:
        source_context=verify_pc_empty_collection_source(proof_path=proof_path,
            qualification_reference=qualification_reference)
    requested_qualification = qualification_reference
    payload=_validated_source_context(source_context)
    proof_ref=payload['proof']; qualification_reference=payload['qualification_reference']
    report=payload['report'];source=payload['source'];events_ref=payload['events_reference']
    if (proof_path is not None and Path(proof_path).resolve()!=Path(proof_ref['path']).resolve()):
        raise ValueError('Supplied consumer proof differs from the verified context')
    if requested_qualification is not None and dict(requested_qualification) != qualification_reference:
        raise ValueError('Supplied qualification receipt differs from the verified context')
    if (Path(event_reference['path']).resolve() != Path(events_ref['path']).resolve()
            or type(event_reference.get('byte_offset')) is not int
            or type(event_reference.get('bytes')) is not int
            or event_reference['byte_offset'] < 0 or event_reference['bytes'] <= 0
            or event_reference['byte_offset'] + event_reference['bytes'] > events_ref['bytes']):
        raise ValueError('Exact event byte span is outside the qualified original observer file')
    if payload['event_index'] is not None:
        keys=('path','byte_offset','bytes','sha256')
        witnesses=[x for x in payload['event_index']['decision_events']
                   if all(x.get(k)==event_reference.get(k) for k in keys)]
        if len(witnesses)!=1 or witnesses[0]['kind']!=1 or witnesses[0]['phase']!=0:
            raise ValueError('Exact main-decision byte span is absent from the qualified event index')
    event = _read_ref(event_reference)
    prepared = prepare_original_pc_primary(event)
    index = event['log_index']
    actions = source['expected']['actions']
    if not 0 <= index < len(actions):
        raise ValueError('Observed decision cursor is outside the original source')
    bind_original_primary_label(prepared, actions[index])
    witnesses = [x for x in payload['candidate_membership']
                 if x.get('event_order') == event['event_order'] and x.get('source_order') == index]
    if (len(witnesses) != 1 or witnesses[0].get('listed_member') is not True
            or witnesses[0].get('read_errors') != []):
        raise ValueError('Exact decision is absent from qualified original candidate membership')
    if prepared.state_before.get('produceId') != source.get('produce_id'):
        raise ValueError('Original source mode and observed decision differ')
    header = {'schema': SCHEMA, 'source_kind': PC_SOURCE, 'source_factory': 'qualified-PC-capture-report-v1',
        'proof': proof_ref, 'qualification_reference': deepcopy(dict(qualification_reference)),
        'event_reference': deepcopy(dict(event_reference)), 'inputs_reference': report['inputs_receipt'],
        'original_source': report['source'], 'source_identity': report['specification'],
        'export_manifest_reference':payload['export_manifest_reference'],
        'verified_export_identity':payload['verified_export_identity'],
        'source_expected': {k: deepcopy(v) for k, v in source['expected'].items() if k != 'actions'},
        'original_observation': prepared.observation, 'reader_identity': report['reader_identity'],
        'native_evidence_sha256': NATIVE_EVIDENCE_SHA256, 'native_core_sha256': NATIVE_CORE_SHA256,
        'native_metadata_sha256': NATIVE_METADATA_SHA256, 'fields': deepcopy(list(FIELD_CONTRACTS)),
        'raw_state_sha256': _digest(prepared.state_before), 'candidate_sha256': _digest(prepared.legal_candidates),
        'diagnostic_only': True, 'native_game_equivalence_claimed': False, 'training_admitted': False,
        'training_eligibility_pending': report['training_eligibility_pending'],
        'verified_source_context_sha256':hashlib.sha256(source_context.payload_json).hexdigest(),
        'source_context_scope':'per-case-provenance-only-no-captured-state-cache',
        'event_index_reference':report.get('event_index')}
    if 'stream_verification' in payload:
        header['source_verification_scope'] = deepcopy(payload['stream_verification'])
    _validated_source_context(source_context)
    return ObservedEmptyCollectionInput(deepcopy(prepared.state_before), tuple(deepcopy(prepared.legal_candidates)), header, source_context)


def _semantic_view(state, source_kind, *, inactive_status_contract=None, optional_identifiers_contract=None):
    """Internal typed projection; admission is enforced by the public validator."""
    semantic = deepcopy(state)
    ledger = []
    for zone in ZONES:
        cards = semantic.get(zone)
        if not isinstance(cards, list):
            continue
        for index, card in enumerate(cards):
            for field in FIELD_CONTRACTS:
                suffix = field['path_suffix'].strip('/').split('/')
                owner = card
                for parent in suffix[:-1]:
                    owner = owner.get(parent) if isinstance(owner, Mapping) else None
                leaf = suffix[-1]
                present = isinstance(owner, Mapping) and leaf in owner
                value = owner.get(leaf) if present else None
                converted = present and value is None and source_kind == PC_SOURCE
                if not present:
                    presence = 'field-missing'
                elif value is None:
                    presence = 'observed-null'
                elif isinstance(value, list):
                    presence = ('observed-json-empty-list' if source_kind == JSON_SOURCE else 'observed-empty-list') if not value else 'observed-nonempty-list'
                else:
                    presence = 'observed-invalid-type:' + type(value).__name__
                if converted:
                    owner[leaf] = []
                known_empty = converted or (present and isinstance(value, list) and not value)
                ledger.append({'path': f'/{zone}/{index}' + field['path_suffix'],
                    'owner': field['owner'], 'field_token': field['field_token'],
                    'raw_field_present': present, 'raw_presence': presence,
                    'raw_value': deepcopy(value), 'semantic_known_empty': known_empty,
                    'semantic_view_changed': converted})
            if inactive_status_contract is not None:
                field = inactive_status_contract['field']
                present = isinstance(card, Mapping) and '_statusEffect' in card
                value = card.get('_statusEffect') if present else None
                exact_json_zero = (source_kind == JSON_SOURCE and isinstance(value, Mapping)
                    and _digest(value) == _digest(inactive_status_contract['native_json_shape']))
                inactive = (present and value is None and source_kind == PC_SOURCE) or exact_json_zero
                if inactive:
                    card['_statusEffect'] = {}
                ledger.append({'path': f'/{zone}/{index}/_statusEffect',
                    'owner': field['owner'], 'field_token': field['field_token'],
                    'raw_field_present': present,
                    'raw_presence': 'field-missing' if not present else 'observed-null' if value is None
                        else 'observed-json-exact-zero-status' if exact_json_zero else 'observed-other-status-shape',
                    'raw_value': deepcopy(value), 'semantic_known_inactive_card_status': inactive,
                    'semantic_scope': 'original-triggered-card-status-dispatcher-only',
                    'semantic_view_changed': inactive})
    if optional_identifiers_contract is not None:
        _optional_identifier_view(semantic, source_kind, optional_identifiers_contract, ledger)
    return semantic, tuple(ledger)


def _optional_identifier_view(semantic, source_kind, contract, ledger):
    """Only source-typed, uniquely bound active status effect-list elements."""
    references = semantic.get('references')
    rows = references.get('RefIds') if isinstance(references, Mapping) else None
    status = semantic.get('status')
    links = status.get('_effectList') if isinstance(status, Mapping) else None
    if not isinstance(rows, list) or not isinstance(links, list):
        return
    link_counts = Counter(link.get('rid') for link in links
        if isinstance(link, Mapping) and type(link.get('rid')) is int)
    matches = {}
    for i, row in enumerate(rows):
        if isinstance(row, Mapping) and type(row.get('rid')) is int:
            matches.setdefault(row['rid'], []).append((i, row))
    for rid, count in link_counts.items():
        candidates = matches.get(rid, ())
        if count != 1 or len(candidates) != 1:
            continue  # Preserve ambiguity for the active-graph validator.
        position, row = candidates[0]
        if row.get('type') != contract['parent_type'] or not isinstance(row.get('data'), Mapping):
            continue
        effects = row['data'].get(contract['parent_field'])
        if not isinstance(effects, list):
            continue
        for index, effect in enumerate(effects):
            if not isinstance(effect, Mapping):
                continue
            for field in contract['fields']:
                name = field['name']; present = name in effect; value = effect.get(name)
                changed = present and value is None and source_kind == PC_SOURCE
                if changed:
                    effect[name] = contract['semantic_model_value']
                ledger.append({'path': f'/references/RefIds/{position}/data/_effectList/{index}/{name}',
                    'owner': contract['element_type'], 'field_token': field['field_token'],
                    'raw_field_present': present, 'raw_presence': 'field-missing' if not present
                        else 'observed-null' if value is None else 'observed-empty-identifier' if value == ''
                        else 'observed-other-identifier', 'raw_value': deepcopy(value),
                    'semantic_known_absent_identifier': changed or (present and type(value) is str and value == ''),
                    'semantic_scope': 'identifier-resolution-guard-only', 'semantic_view_changed': changed,
                    'cached_object_absence_claimed': False, 'inactive_effect_claimed': False})


def validated_empty_collection_view(prepared, *, state_before, legal_candidates, contract):
    """Rebind the typed receipt to original files and supplied unmodified input."""
    if not isinstance(prepared, ObservedEmptyCollectionInput):
        raise ValueError('Verified original empty-collection input is required; raw headers are not admission')
    validate_empty_collection_contract(contract, card_payload_sha256=contract['card_payload_sha256'],
        snapshot_fingerprint=contract['snapshot_fingerprint'])
    header = prepared.source_header
    try:
        if header['source_factory'] == 'qualified-PC-capture-report-v1':
            if prepared.source_context is None:
                raise ValueError('The original per-case verified context is required')
            original = prepare_pc_empty_collection_input(source_context=prepared.source_context,
                event_reference=header['event_reference'])
        elif header['source_factory'] == 'frozen-B88-receipt':
            original = prepare_observed_empty_collection_input(proof_path=header['proof']['path'],
                row_index=header['row_index'], source_kind=header['source_kind'])
        else:
            raise ValueError('Unknown original source factory')
    except (KeyError, TypeError) as exc:
        raise ValueError('Original empty-collection source receipt is incomplete') from exc
    if (original.source_header != header or _digest(prepared.state_before) != header['raw_state_sha256']
            or _digest(prepared.legal_candidates) != header['candidate_sha256']
            or _digest(state_before) != header['raw_state_sha256']
            or _digest(legal_candidates) != header['candidate_sha256']):
        raise ValueError('Supplied state/candidates differ from the original observed source')
    status_contract = contract.get('inactive_card_status_contract')
    identifiers = contract.get('optional_effect_identifiers_contract')
    semantic, ledger = _semantic_view(state_before, header['source_kind'], inactive_status_contract=status_contract,
        optional_identifiers_contract=identifiers)
    return semantic, {'source_header': deepcopy(header), 'presence_ledger': ledger,
        'inactive_card_status_contract': deepcopy(status_contract),
        **({'optional_effect_identifiers_contract': deepcopy(identifiers)} if identifiers is not None else {}),
        'raw_state_sha256': header['raw_state_sha256'], 'semantic_view_sha256': _digest(semantic),
        'converted_paths': [x['path'] for x in ledger if x['semantic_view_changed']],
        'raw_state_changed': False, 'raw_values_equal_across_sources_claimed': False,
        'JSON_inversion_claimed': False, 'actual_payment_proved': False,
        'training_admitted': False}
