"""Explicit shadow joint-model loading and one owned native decision entry.

The policy never selects a game mode, starts a game, switches the registry or
owns native completion. Existing controller callbacks retain those boundaries.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
from pathlib import Path

import numpy as np

from .integrated_exam_bc_model import IntegratedExamBcPolicy, validate_parameters
from .native_policy_features import OwnedNativePolicySource
from .training_artifact_io import canonical_json_bytes, sha256_file

MODEL_SCHEMA = 'gkms.integrated-exam-bc-shadow-model.v1'
MODEL_KIND = 'shared-main-secondary-candidate-pointer'


def load_integrated_exam_bc_model(reference, *, expected_feature_contract):
    """Load the specified completed candidate; never activate it globally."""
    if (not isinstance(reference, Mapping) or not isinstance(reference.get('path'), str)
            or sha256_file(Path(reference['path'])) != reference.get('sha256')):
        raise ValueError('Integrated model bytes differ from the explicit reference')
    with np.load(reference['path'], allow_pickle=False) as arrays:
        metadata = json.loads(str(arrays['metadata_json'].item()))
        if (metadata.get('schema') != MODEL_SCHEMA or metadata.get('kind') != MODEL_KIND
                or metadata.get('complete') is not True or metadata.get('shadow_only') is not True
                or metadata.get('runtime_promotion_allowed') is not False
                or canonical_json_bytes(metadata.get('feature_contract')) != canonical_json_bytes(expected_feature_contract)):
            raise ValueError('A completed joint shadow model with the exact shared feature contract is required')
        parameters = {key: arrays[key].copy() for key in arrays.files if key != 'metadata_json'}
    validate_parameters(parameters, finite=True)
    layout = expected_feature_contract['pointer_layout']
    if (parameters['context_embedding'].shape[0] != layout['context_buckets']
            or parameters['candidate_embedding'].shape[0] != layout['candidate_buckets']):
        raise ValueError('Joint model embeddings differ from their shared pointer layout')
    if sha256_file(Path(reference['path'])) != reference['sha256']:
        raise ValueError('Joint model changed while loading')
    return parameters, metadata


class IntegratedNativeBcPolicy:
    """The same learned parameter set handles main and ordered secondary steps."""

    def __init__(self, specification, *, engine_identity):
        from .integrated_exam_bc_features import IntegratedExamFeatureEncoder
        if not isinstance(specification, Mapping) or set(specification) != {'model', 'contract_set', 'original_shared_encoder'}:
            raise ValueError('Explicit model, native contract set and original primary encoder references required')
        self.source = OwnedNativePolicySource(engine_identity)
        self.encoder = IntegratedExamFeatureEncoder(specification['contract_set'], specification['original_shared_encoder'])
        parameters, self.metadata = load_integrated_exam_bc_model(specification['model'],
            expected_feature_contract=self.encoder.contract)
        self.model_reference = deepcopy(dict(specification['model']))
        self.policy = IntegratedExamBcPolicy(parameters, self.encoder)

    def decide(self, observation):
        from .native_secondary_input import prepare_native_secondary_input
        kind = observation.get('decision_type')
        if kind not in ('main', 'secondary'):
            raise ValueError('Unimplemented native decision type requires an explicit interface and evidence')
        self.source.validate_owner(observation, kind)
        # Attach ownership to a separate adapter envelope. The controller's
        # original observation remains unchanged and keeps its original hash.
        adapted = dict(observation)
        adapted['native_observation'] = observation
        adapted['owned_source'] = self.source
        if kind == 'secondary':
            adapted['secondary_input'] = prepare_native_secondary_input(observation)
            if adapted['secondary_input'].state_before['produceId'] != self.source.produce:
                raise ValueError('Secondary mode differs from the initialized native policy source')
        response = self.policy.decide(adapted)
        self.source.validate_owner(observation, kind)
        learned = response['policy_source']['learned_model_used']
        response['policy_source'] = {'kind': 'integrated-exam-BC', 'model': self.model_reference,
            'shared_feature_contract_sha256': self.encoder.contract['contract_sha256'],
            'decision_type': kind, 'one_shared_parameter_set': True, 'learned_model_used': learned,
            'native_forced_response': not learned, 'teacher_action_used': False,
            'legacy_rule_fallback': False, 'shadow_only': True, 'automatic_formal_model_activation': False}
        return response
