"""One current-state/candidate encoder for new six-flow BC candidates.

Reuses the existing v4 semantic/token encoders and pointer hash layout. The
new contract adds the observed producePoint and explicit payment-unknown
tokens; historical v4 encoders/weights are never reinterpreted. Legality,
source qualification, player grouping and model activation remain separate.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
import hashlib
from pathlib import Path

from .active_state_features import SCHEMA as ACTIVE_SCHEMA, extract_active_state_features, observed_payload_tokens
from .behavior_cloning import (
    _exact_candidate_context_semantics, _hash_index, build_exact_main_action_context_tokens,
    exact_candidate_field_tokens, exact_candidate_semantics,
)
from .canonical_training_labels import EFFECT_BY_NATIVE_VALUE, PLAN_BY_NATIVE_VALUE, STAGE_BY_NATIVE_VALUE
from .card_semantic_features import FEATURE_SCHEMA, CardSemanticFeatures
from .exact_exam_state_projection import normalize_exact_exam_legal_candidates
from .exact_policy_contract import strict_exact_candidate_index, validate_exact_legal_candidates
from .training_artifact_io import canonical_json_bytes

SCHEMA = 'gkms.shared-bc-current-primary-features.v1'
ENCODING = 'field-token-bag+card-semantic-v4+shared-primary-v1'
SLOT_SCHEMA = 'gkms.shared-bc-current-primary-features.v2'
SLOT_ENCODING = 'field-token-bag+card-semantic-v4+shared-primary-v2-slot'
GROW_SCHEMA = 'gkms.shared-bc-current-primary-features.v3-runtime-grow'
GROW_ENCODING = 'field-token-bag+card-semantic-v4+shared-primary-v3-slot-runtime-grow'
EMPTY_SCHEMA = 'gkms.shared-bc-current-primary-features.v4-observed-empty-collections'
EMPTY_ENCODING = 'field-token-bag+card-semantic-v4+shared-primary-v4-slot-observed-empty-collections'
NATIVE_STRUCTURE_SCHEMA = 'gkms.shared-bc-current-primary-features.v5-native-structure'
NATIVE_STRUCTURE_ENCODING = 'field-token-bag+native-structure+shared-primary-v5-slot'
PRIMARY_SCOPE = 'primary-inputs-only'
PLAN_EFFECTS = ((2, 2), (2, 10), (3, 31), (3, 42), (4, 45), (4, 47))


def digest(value):
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@lru_cache(maxsize=16)
def _encoder_source_hashes(observed_slot_binding=False, observed_runtime_grow=False, observed_empty_collections=False,
                          native_structure=False):
    # Code metadata is stable for this imported encoder's lifetime. Never
    # cache a decision state, candidate getter or any game value here.
    names = ('shared_bc_features.py', 'behavior_cloning.py', 'card_semantic_features.py',
        'active_state_features.py', 'exact_exam_state_projection.py', 'exact_policy_contract.py')
    if observed_slot_binding:
        names += ('observed_primary_identity.py',)
    if observed_runtime_grow:
        names += ('observed_runtime_grow.py',)
    if observed_empty_collections:
        names += ('observed_empty_collections.py', 'original_pc_primary_input.py')
    if native_structure:
        names += ('native_structure_features.py',)
    return tuple((name, hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()) for name in names)


def _native_structure_feature_contract(features, *, payload_sha256, produce_ids, diagnostic_only,
                                      empty_contract, active_status_contract):
    from .native_structure_features import NativeStructureCardSemanticFeatures, native_structure_contract
    from .observed_empty_collections import validate_empty_collection_contract
    if not isinstance(features, NativeStructureCardSemanticFeatures):
        raise ValueError('Native structure requires its source-verified immutable semantic package')
    if features.schema != FEATURE_SCHEMA or features.current_state_schema != ACTIVE_SCHEMA:
        raise ValueError('Native structure requires its frozen card/current-state schemas')
    native = native_structure_contract(features.package)
    if payload_sha256 != native['card_payload_sha256']:
        raise ValueError('Native structure package and card payload SHA differ')
    # Representation and corpus admission are separate milestones. This first
    # migration cannot silently turn a diagnostic catalog into a trained model.
    if diagnostic_only is not True:
        raise ValueError('Native structure migration is diagnostic-only until corpus admission is implemented')
    modes = ['produce-004'] if produce_ids is None else list(produce_ids)
    if (not modes or modes != sorted(set(modes))
            or any(x not in ('produce-004', 'produce-005') for x in modes)):
        raise ValueError('Native structure mode scope must be explicit Pro/Master')
    validate_empty_collection_contract(empty_contract, card_payload_sha256=payload_sha256,
        snapshot_fingerprint=native['snapshot_fingerprint'])
    if any(empty_contract[k] != native[k] for k in ('native_core_sha256', 'native_metadata_sha256')):
        raise ValueError('Native structure schema and source-view consumer versions differ')
    if active_status_contract != getattr(features, 'active_status_contract', None):
        raise ValueError('Native structure active-status wrapper and shared contract differ')
    if active_status_contract is not None:
        from .active_state_features import validate_active_status_extension
        validate_active_status_extension(active_status_contract)
        if any(active_status_contract[k] != native[k] for k in ('native_core_sha256', 'native_metadata_sha256')):
            raise ValueError('Native structure active-status consumer version differs')
    body = {'schema': NATIVE_STRUCTURE_SCHEMA, 'encoding': NATIVE_STRUCTURE_ENCODING,
        'card_schema': features.schema, 'current_state_schema': features.current_state_schema,
        'card_payload_sha256': payload_sha256, 'snapshot_fingerprint': native['snapshot_fingerprint'],
        'native_structure_contract': native, 'candidate_scope': PRIMARY_SCOPE,
        'encoder_source_hashes': dict(_encoder_source_hashes(True, False, True, True)),
        'produce_ids': modes, 'native_plan_effects': [list(x) for x in PLAN_EFFECTS],
        'native_stages': [16, 17, 18], 'context_extension': 'observed-producePoint-with-explicit-missing.v1',
        'actual_payment': 'unknown-unless-a-separate-native-payment-contract-is-added',
        'secondary_selection_complete': False, 'diagnostic_only': True,
        'dataset_surface': 'source-bound-primary-BC-before-state-and-label-only',
        'action_after_state_observed': False, 'RL_transition_qualified': False,
        'derived_arithmetic_required_for_representation': False,
        'observed_empty_collection_contract': dict(empty_contract)}
    from .observed_primary_identity import SCHEMA as IDENTITY_SCHEMA
    body['identity_schema'] = IDENTITY_SCHEMA
    if active_status_contract is not None:
        body['observed_active_status_contract'] = dict(active_status_contract)
    return {**body, 'contract_sha256': digest(body)}


def feature_contract(features: CardSemanticFeatures, *, payload_sha256: str,
                     identity_mode='guid', produce_ids=None, diagnostic_only=False,
                     observed_runtime_grow_contract=None, observed_empty_collection_contract=None,
                     observed_active_status_contract=None):
    if identity_mode == 'observed-slot-native-structure':
        if observed_runtime_grow_contract is not None:
            raise ValueError('Native structure does not compose the legacy derived grow adapter')
        return _native_structure_feature_contract(features, payload_sha256=payload_sha256,
            produce_ids=produce_ids, diagnostic_only=diagnostic_only,
            empty_contract=observed_empty_collection_contract, active_status_contract=observed_active_status_contract)
    if getattr(features, 'package', None) is not None:
        raise ValueError('Native structure package requires its explicit shared feature version')
    if getattr(features, 'runtime_grow_contract', None) is not None:
        raise ValueError('Select the runtime grow adapter through its shared contract, not a legacy catalog wrapper')
    if getattr(features, 'active_status_contract', None) is not None:
        raise ValueError('Select active-status recognition through its shared contract')
    if features.schema != FEATURE_SCHEMA or features.current_state_schema != ACTIVE_SCHEMA:
        raise ValueError('Shared primary features require frozen card v4/current-state v2')
    if len(payload_sha256) != 64 or any(c not in '0123456789abcdef' for c in payload_sha256):
        raise ValueError('Feature payload SHA256 is required')
    if identity_mode not in ('guid', 'observed-slot', 'observed-slot-runtime-grow', 'observed-slot-empty-collections') or type(diagnostic_only) is not bool:
        raise ValueError('Unknown shared primary identity/diagnostic contract')
    observed_grow = identity_mode == 'observed-slot-runtime-grow'
    observed_empty = identity_mode == 'observed-slot-empty-collections'
    observed_slot = identity_mode in ('observed-slot', 'observed-slot-runtime-grow', 'observed-slot-empty-collections')
    grow_enabled = observed_grow or (observed_empty and observed_runtime_grow_contract is not None)
    modes = ['produce-004'] if produce_ids is None else list(produce_ids)
    if grow_enabled:
        from .observed_runtime_grow import validate_runtime_grow_contract,validate_historical_grow_payload
        historical_bridge = isinstance(observed_runtime_grow_contract,Mapping) and 'historical_catalog_bridge' in observed_runtime_grow_contract
        if historical_bridge and not observed_empty:
            raise ValueError('Historical runtime grow bridge requires the existing source-bound shared v4')
        if not diagnostic_only or (not historical_bridge and modes != ['produce-004']):
            raise ValueError('Observed runtime grow scope is diagnostic-only produce-004')
        validate_runtime_grow_contract(observed_runtime_grow_contract,card_payload_sha256=payload_sha256,
            snapshot_fingerprint=features.payload['snapshot_fingerprint'])
        if historical_bridge:
            validate_historical_grow_payload(features.payload,observed_runtime_grow_contract)
    elif observed_runtime_grow_contract is not None:
        raise ValueError('Runtime grow adapter requires its explicit new feature schema')
    if observed_empty:
        from .observed_empty_collections import validate_empty_collection_contract
        if not diagnostic_only:
            raise ValueError('Observed empty-collection scope is diagnostic-only')
        validate_empty_collection_contract(observed_empty_collection_contract, card_payload_sha256=payload_sha256,
            snapshot_fingerprint=features.payload['snapshot_fingerprint'])
    elif observed_empty_collection_contract is not None:
        raise ValueError('Empty-collection adapter requires its explicit new feature schema')
    if observed_active_status_contract is not None:
        from .active_state_features import validate_active_status_extension
        if not observed_empty or not diagnostic_only:
            raise ValueError('Active-status recognition requires the source-bound diagnostic v4')
        validate_active_status_extension(observed_active_status_contract)
    if (not modes or len(set(modes)) != len(modes) or modes != sorted(modes)
            or any(x not in ('produce-004', 'produce-005') for x in modes)
            or (not observed_slot and (modes != ['produce-004'] or diagnostic_only))
            or ('produce-005' in modes and not diagnostic_only)):
        raise ValueError('Master scope requires an explicit diagnostic-only slot contract')
    body = {'schema': SCHEMA, 'encoding': ENCODING, 'card_schema': features.schema,
        'current_state_schema': features.current_state_schema, 'card_payload_sha256': payload_sha256,
        'snapshot_fingerprint': features.payload['snapshot_fingerprint'], 'candidate_scope': PRIMARY_SCOPE,
        'encoder_source_hashes': dict(_encoder_source_hashes(observed_slot, grow_enabled, observed_empty)),
        'produce_ids': ['produce-004'], 'native_plan_effects': [list(x) for x in PLAN_EFFECTS],
        'native_stages': [16, 17, 18], 'context_extension': 'observed-producePoint-with-explicit-missing.v1',
        'actual_payment': 'unknown-unless-a-separate-native-payment-contract-is-added',
        'secondary_selection_complete': False}
    if observed_slot:
        from .observed_primary_identity import SCHEMA as IDENTITY_SCHEMA
        body.update(schema=SLOT_SCHEMA, encoding=SLOT_ENCODING, identity_schema=IDENTITY_SCHEMA,
            produce_ids=modes, diagnostic_only=diagnostic_only)
    if observed_grow:
        body.update(schema=GROW_SCHEMA,encoding=GROW_ENCODING,
            observed_runtime_grow_contract=dict(observed_runtime_grow_contract))
    if observed_empty:
        body.update(schema=EMPTY_SCHEMA, encoding=EMPTY_ENCODING,
            observed_empty_collection_contract=dict(observed_empty_collection_contract))
        if grow_enabled:
            body['observed_runtime_grow_contract'] = dict(observed_runtime_grow_contract)
        if observed_active_status_contract is not None:
            body['observed_active_status_contract'] = dict(observed_active_status_contract)
    return {**body, 'contract_sha256': digest(body)}


@dataclass(frozen=True)
class SharedPrimaryFeatures:
    contract_sha256: str
    state_sha256: str
    flow: str
    stage: str
    context_tokens: tuple[str, ...]
    candidate_tokens: tuple[tuple[str, ...], ...]
    candidate_semantics: tuple[str, ...]
    candidate_bindings: tuple[Mapping, ...]
    normalized_candidates: tuple[Mapping, ...]
    coverage: Mapping

    @property
    def feature_sha256(self):
        # GUID, slot binding receipts and source/player/labels are excluded.
        return digest({'contract_sha256': self.contract_sha256, 'context': self.context_tokens,
            'candidates': self.candidate_tokens, 'semantics': self.candidate_semantics})

    def pointer_indices(self, *, context_buckets: int, candidate_buckets: int):
        """Existing pointer optimizer layout, with no model or fit operation."""
        contexts = tuple(_hash_index(x, context_buckets) for x in self.context_tokens)
        candidates = tuple(tuple(dict.fromkeys(_hash_index('exact-candidate-field:' + x, candidate_buckets)
            for x in row)) for row in self.candidate_tokens)
        if len(set(candidates)) != len(candidates):
            raise ValueError('Candidate hash collision in requested pointer dimensions')
        return contexts, candidates


def _validate_contract(contract, features):
    slot = uses_observed_slot_binding(contract)
    expected = feature_contract(features, payload_sha256=contract['card_payload_sha256'],
        identity_mode='observed-slot-native-structure' if contract.get('schema') == NATIVE_STRUCTURE_SCHEMA else 'observed-slot-empty-collections' if contract.get('schema') == EMPTY_SCHEMA else 'observed-slot-runtime-grow' if contract.get('schema') == GROW_SCHEMA else 'observed-slot' if slot else 'guid',
        produce_ids=contract.get('produce_ids'),diagnostic_only=contract.get('diagnostic_only', False),
        observed_runtime_grow_contract=contract.get('observed_runtime_grow_contract'),
        observed_empty_collection_contract=contract.get('observed_empty_collection_contract'),
        observed_active_status_contract=contract.get('observed_active_status_contract'))
    if dict(contract) != expected:
        raise ValueError('Shared feature contract or frozen semantic version mismatch')


def uses_observed_slot_binding(contract):
    return contract.get('schema') in (SLOT_SCHEMA, GROW_SCHEMA, EMPTY_SCHEMA, NATIVE_STRUCTURE_SCHEMA)


def shared_primary_candidate_index(state, action, candidates, contract):
    if uses_observed_slot_binding(contract):
        from .observed_primary_identity import observed_primary_candidate_index
        return observed_primary_candidate_index(state, action, candidates)
    return strict_exact_candidate_index(state, action, candidates)


def validate_shared_primary_contract(contract, features, *, card_feature_contract=None):
    """Validate version identity; this does not qualify a dataset or model."""
    if not isinstance(contract, Mapping) or not isinstance(features, CardSemanticFeatures):
        raise ValueError('Shared primary contract requires a verified frozen semantic catalog')
    _validate_contract(contract, features)
    if card_feature_contract is not None and (
            card_feature_contract.get('schema') != contract['card_schema']
            or card_feature_contract.get('features_sha256') != contract['card_payload_sha256']
            or card_feature_contract.get('snapshot_fingerprint') != contract['snapshot_fingerprint']):
        raise ValueError('Shared primary contract differs from the model/spec card artifact')
    return contract


def _is_semantic_gap_token(token: str) -> bool:
    """Recognize gap markers emitted by the required card v4/active v2 pair.

    Numeric field names (such as stamina_gap) and string payloads are not
    markers. V4 context also wraps card gaps in a per-zone histogram.
    """
    if token.startswith(('semantic:gap:', 'active-v2:gap:', 'semantic-context:gap:')):
        return True
    parts = token.split(':', 5)
    return (len(parts) == 6 and parts[:2] == ['semantic-context', 'zone']
        and parts[2] in ('handList', 'deckList', 'graveList', 'lostList', 'holdList')
        and parts[3:5] == ['semantic', 'gap'])


def encode_shared_bc_primary(*, state_before: Mapping, legal_candidates: Sequence,
                            flow: str, stage: str, native_mask_complete: bool,
                            candidate_scope: str, features: CardSemanticFeatures,
                            contract: Mapping, observed_empty_input=None) -> SharedPrimaryFeatures:
    """The only encode implementation; train and runtime adapters call here.

    Accepts a supplied native primary mask, never enumerates inventory as legal.
    It consumes only the current raw state and candidates, never a label,
    after-state, score target, replay action list, player identity or split.
    """
    _validate_contract(contract, features)
    if native_mask_complete is not True or candidate_scope != PRIMARY_SCOPE:
        raise ValueError('Complete native primary mask required; secondary is a separate scope')
    raw_state = state_before
    empty_evidence = None
    if contract['schema'] in (EMPTY_SCHEMA, NATIVE_STRUCTURE_SCHEMA):
        from .observed_empty_collections import validated_empty_collection_view
        state_before, empty_evidence = validated_empty_collection_view(observed_empty_input,
            state_before=state_before, legal_candidates=legal_candidates,
            contract=contract['observed_empty_collection_contract'])
        if ('observed_runtime_grow_contract' in contract
                and empty_evidence['source_header']['source_expected'].get('master_hash')
                    != contract['observed_runtime_grow_contract']['source_master_hash']):
            raise ValueError('Verified input source Master differs from the composed runtime grow contract')
        if 'observed_active_status_contract' in contract:
            status_contract = contract['observed_active_status_contract']
            if any(empty_evidence['source_header'].get(key) != status_contract[key]
                   for key in ('native_core_sha256', 'native_metadata_sha256')):
                raise ValueError('Verified input differs from the active-status consumer version')
        if contract['schema'] == NATIVE_STRUCTURE_SCHEMA:
            native = contract['native_structure_contract']
            header = empty_evidence['source_header']
            if (header['source_expected'].get('master_hash') != native['source_master_hash']
                    or any(header[k] != native[k] for k in ('native_core_sha256', 'native_metadata_sha256'))):
                raise ValueError('Verified native input differs from the structure package source or schema')
    elif observed_empty_input is not None:
        raise ValueError('Observed empty-collection input requires its explicit new feature schema')
    return encode_primary_feature_view(raw_state=raw_state, state_before=state_before,
        legal_candidates=legal_candidates, flow=flow, stage=stage, native_mask_complete=native_mask_complete,
        candidate_scope=candidate_scope, features=features, contract=contract, empty_evidence=empty_evidence)


def encode_primary_feature_view(*, raw_state, state_before, legal_candidates, flow, stage,
                                native_mask_complete, candidate_scope, features, contract, empty_evidence=None):
    """Deterministic shared feature kernel; source adapters own provenance admission.

    This function alone does not qualify a source. Both archived training
    observations and owned native policy observations use their explicit
    source checks before entering this same state/candidate token encoder.
    """
    if native_mask_complete is not True or candidate_scope != PRIMARY_SCOPE:
        raise ValueError('Complete native primary mask required by the shared feature kernel')
    pair = (state_before.get('planType'), state_before.get('mainEffectType'))
    step = state_before.get('stepType')
    if (any(type(x) is not int for x in pair) or pair not in PLAN_EFFECTS
            or state_before.get('produceId') not in contract['produce_ids']
            or type(step) is not int or step not in contract['native_stages']
            or state_before.get('examType') != 1 or type(state_before.get('examType')) is not int):
        raise ValueError('Mode/flow/stage is outside the shared native contract')
    expected_flow = '|'.join((state_before['produceId'], PLAN_BY_NATIVE_VALUE[pair[0]], EFFECT_BY_NATIVE_VALUE[pair[1]]))
    if flow != expected_flow or stage != STAGE_BY_NATIVE_VALUE[step]:
        raise ValueError('Supplied flow/stage conflicts with the current native state')
    if state_before.get('phase') != 6 or type(state_before.get('phase')) is not int or state_before.get('isExamEndComplete') is not False:
        raise ValueError('Current state is not a nonterminal Main boundary')
    if not legal_candidates or any(not isinstance(x, Mapping) for x in legal_candidates):
        raise ValueError('Native candidates must be nonempty mappings')
    for row in legal_candidates:
        if ('legal' in row and row['legal'] is not True) or row.get('selected_card_guid'):
            raise ValueError('Denied or secondary-target candidate cannot enter primary features')
    slot_binding = uses_observed_slot_binding(contract)
    if slot_binding:
        from .observed_primary_identity import normalize_observed_primary_candidates
        # The already required complete native mask supplies legality. A
        # UnifiedLegalActionSnapshot omits its redundant per-row legal flag.
        # END has the existing wire slot 0; hand/drink slots are never inferred.
        supplied = []
        for candidate in legal_candidates:
            row = dict(candidate)
            row.setdefault('legal', True)
            if row.get('kind') in ('end_turn', 'turn-end'):
                row.setdefault('slot_index', 0)
            supplied.append(row)
        normalized = normalize_observed_primary_candidates(state_before, supplied)
    else:
        normalized = normalize_exact_exam_legal_candidates(state_before, legal_candidates)
        validate_exact_legal_candidates(state_before, normalized)
    bindings, upgrades, gaps, optional_diagnostics = [], [], [], []
    for original, candidate in zip(legal_candidates, normalized):
        kind, slot = candidate['kind'], candidate['slot_index']
        supplied_slot = original.get('slot_index', original.get('slot', original.get('hand_slot')))
        if supplied_slot is not None and (type(supplied_slot) is not int or supplied_slot != slot):
            raise ValueError('Supplied candidate slot conflicts with its original native identity')
        binding = {'kind': kind, 'slot': slot}
        if kind == 'use-hand':
            raw = state_before['handList'][slot]
            data = raw['_cardData']
            value = data.get('_upgradeCount')
            if type(value) is not int or value < 0 or type(candidate.get('upgrade')) is not int or candidate['upgrade'] != value:
                raise ValueError('Candidate effective upgrade differs from current native card data')
            binding.update(card_guid=candidate['card_guid'], card_id=data['_id'], effective_upgrade=value)
            facts = {k: raw.get(k) for k in ('_baseUpgradeCount', '_tmpUpgradeCount', '_supportUpgradeIdList')}
            facts.update(card_guid=candidate['card_guid'], effective_upgrade=value,
                pt_customization_counts=data.get('_customizeCountList'))
            if type(facts['_baseUpgradeCount']) is int and type(facts['_tmpUpgradeCount']) is int and isinstance(facts['_supportUpgradeIdList'], list):
                if facts['_baseUpgradeCount'] + facts['_tmpUpgradeCount'] + len(facts['_supportUpgradeIdList']) != value:
                    if contract['schema'] == NATIVE_STRUCTURE_SCHEMA:
                        optional_diagnostics.append('upgrade-component-sum-differs-from-observed-effective:' + str(slot))
                    else:
                        raise ValueError('Native base/temporary/support upgrades do not explain effective upgrade')
            else:
                gaps.append('upgrade-components-unobserved:' + str(slot))
            counts = facts['pt_customization_counts']
            if not isinstance(counts, list) or any(type(x) is not int or x < 0 for x in counts):
                gaps.append('pt-customization-counts-unobserved-or-invalid:' + str(slot))
            upgrades.append(facts)
        elif kind == 'use-drink':
            binding['drink_id'] = candidate['drink_id']
        bindings.append(binding)
    encoding_features = features
    if contract['schema'] == GROW_SCHEMA or (contract['schema'] == EMPTY_SCHEMA and 'observed_runtime_grow_contract' in contract):
        from .observed_runtime_grow import ObservedRuntimeGrowSemanticFeatures
        encoding_features = ObservedRuntimeGrowSemanticFeatures(features.payload,contract['observed_runtime_grow_contract'])
    active_status_contract = contract.get('observed_active_status_contract')
    if active_status_contract is not None and contract['schema'] != NATIVE_STRUCTURE_SCHEMA:
        from .card_semantic_features import ActiveStatusSemanticFeatures
        encoding_features = ActiveStatusSemanticFeatures(features.payload, encoding_features, active_status_contract)
    fields = exact_candidate_field_tokens(state_before, normalized, semantic_features=encoding_features,
        observed_slot_binding=slot_binding)
    semantics = exact_candidate_semantics(state_before, normalized, observed_slot_binding=slot_binding)
    context = build_exact_main_action_context_tokens(state_before=state_before, flow=flow, stage=stage,
        candidate_semantics=_exact_candidate_context_semantics(fields), semantic_features=encoding_features)
    # New version only: existing v4 artifacts/weights retain their exact tokens.
    point = state_before.get('producePoint')
    point_observed = type(point) is int and point >= 0
    if not point_observed:
        gaps.append('producePoint-unobserved-or-invalid')
    extension = ('shared-bc-schema:' + contract['schema'], *('shared-bc:' + x for x in
        observed_payload_tokens('current.producePoint', point if point_observed else None)))
    active = extract_active_state_features(state_before, qualified_status_extension=active_status_contract)
    semantic_gaps = sorted({token for group in (tuple(context), *fields) for token in group if _is_semantic_gap_token(token)})
    actual_payment = tuple({'kind': row['kind'], 'slot': row['slot_index'],
        'native_actual_payment': None, 'status': 'not-observed-by-this-native-primary-contract'} for row in normalized)
    fields = tuple((*row, 'shared-bc:actual-payment:unobserved') for row in fields)
    coverage = {'schema': contract['schema'], 'primary_mask_complete': True, 'current_graph_complete': active.graph_complete,
        'active_state_gaps': [vars(x) for x in active.gaps], 'semantic_gap_tokens': semantic_gaps,
        'contract_gaps': gaps, 'produce_point': point if point_observed else None,
        'produce_point_observed': point_observed, 'candidate_upgrade_sources': upgrades,
        'native_payment_observations': actual_payment, 'actual_payment_complete': False,
        'master_card_cost_parameters_are_not_actual_payment': True,
        'secondary_selection_complete': False, 'precise_training_admitted': False}
    if empty_evidence is not None:
        coverage['observed_empty_collections'] = empty_evidence
        coverage['diagnostic_only'] = True
    if active_status_contract is not None:
        coverage['observed_active_status_contract'] = active_status_contract
    if contract['schema'] == NATIVE_STRUCTURE_SCHEMA:
        structure_coverage = encoding_features.representation_coverage(state_before, normalized)
        coverage['native_structure'] = structure_coverage
        # Existing inventory readers consume these top-level gap fields. A
        # new mandatory structure gap must not disappear inside a new object.
        structure_gaps = ['native-structure:' + (value if isinstance(value, str) else
            canonical_json_bytes(value).decode('utf-8')) for value in structure_coverage['mandatory_gaps']]
        coverage['contract_gaps'] = [*gaps, *structure_gaps]
        coverage['optional_derived_diagnostics'] = [
            *structure_coverage['optional_derived_diagnostics'], *optional_diagnostics]
        coverage['current_graph_complete'] = structure_coverage['graph_complete']
        coverage['representation_complete'] = (structure_coverage['representation_complete'] is True
            and not coverage['contract_gaps'])
        coverage['derived_arithmetic_required_for_representation'] = False
        coverage['action_after_state_observed'] = False
        coverage['RL_transition_qualified'] = False
    return SharedPrimaryFeatures(contract['contract_sha256'], digest(raw_state), flow, stage,
        (*context, *extension), fields, semantics, tuple(bindings), normalized, coverage)


def encode_training_primary(transition, *, flow, stage, features, contract):
    if (transition.get('schema') != 'gkms.nia-inner-transition.v1'
            or transition.get('candidate_set_kind') != 'unified'
            or transition.get('metadata', {}).get('policy_surface') != 'main-action-v1'
            or transition.get('metadata', {}).get('simulator_used') is not False):
        raise ValueError('Recorded native primary transition required')
    encoded = encode_shared_bc_primary(state_before=transition['state_before'],
        legal_candidates=transition['legal_candidates'], flow=flow, stage=stage,
        native_mask_complete=True, candidate_scope=PRIMARY_SCOPE, features=features, contract=contract)
    # Label binding is after encoding, never a feature input.
    target = shared_primary_candidate_index(transition['state_before'], transition['action'], encoded.normalized_candidates, contract)
    return encoded, target


def encode_runtime_primary(*, state_before, snapshot, stage, features, contract):
    return encode_shared_bc_primary(state_before=state_before, legal_candidates=snapshot.candidates,
        flow=snapshot.flow_id, stage=stage, native_mask_complete=snapshot.complete,
        candidate_scope=PRIMARY_SCOPE, features=features, contract=contract)


def require_shared_model_contract(metadata, contract):
    """A future new pointer candidate must explicitly bind these new tokens."""
    if metadata.get('shared_primary_feature_contract') != dict(contract) or metadata.get('candidate_encoding') != contract['encoding']:
        raise ValueError('Model does not bind the shared primary feature contract; legacy weights remain legacy')
    if (metadata.get('kind') != 'exact-main-action-candidate-pointer'
            or metadata.get('current_state_feature_schema') != ACTIVE_SCHEMA
            or metadata.get('candidate_feature_prefix') != 'exact-candidate-field:'
            or metadata.get('action_surface') != 'main-action-v1'):
        raise ValueError('Shared primary model kind/state/action/pointer contract differs')


def inspect_training_source_scope(transition, source_stage, *, quarantine_episode_ids):
    """Preserve source/group restrictions; encoding is never fit admission.

    The file preparation owner verifies referenced report/line hashes. This
    pure check binds the supplied original identity and keeps unknown players,
    protected roles, quarantine and not-yet-assigned new splits visible.
    """
    if transition.get('trajectory_id') != source_stage.get('source_trajectory_id'):
        raise ValueError('Training row and original source trajectory differ')
    metadata = transition.get('metadata', {})
    if transition.get('capture_source_id') != source_stage.get('capture_source_id'):
        raise ValueError('Training row and native capture source differ')
    if metadata.get('runtime_stage_index') != source_stage.get('native_stage_index'):
        raise ValueError('Training row and native stage index differ')
    state = transition['state_before']
    for key, value in source_stage['identity'].items():
        if state.get(key) != value:
            raise ValueError('Training row native identity differs: ' + key)
    quarantined = source_stage.get('original_episode_id') in set(quarantine_episode_ids)
    protected = source_stage.get('protected_role') in ('evaluation_A', 'reserved_evaluation_C')
    return {'stage_id': source_stage['stage_id'], 'original_episode_id': source_stage.get('original_episode_id'),
        'source_trajectory_id': source_stage['source_trajectory_id'],
        'whole_trajectory_id': source_stage['whole_trajectory_id'],
        'player_sha256': source_stage.get('player_sha256'),
        'player_identity_known': bool(source_stage.get('player_sha256')),
        'historical_split': source_stage.get('historical_split'),
        'protected_role': source_stage.get('protected_role'), 'quarantine_excluded': quarantined,
        'fit_forbidden_by_protected_evaluation': protected,
        'feature_diagnostic_allowed': not quarantined,
        'new_training_admitted': False, 'new_split_assigned': False,
        'precise_source_master_version_verified': False,
        'file_hashes_rechecked_by_this_pure_function': False}
