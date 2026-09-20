"""Bind an original-PC observation to the existing primary policy input.

No game calls, rule simulation, feature encoding or training admission occur
here. The raw state is preserved, including nulls and observed reference IDs.
PC observations remain distinct from DLL game captures and old simulators.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json

from .exact_policy_contract import strict_exact_candidate_index, validate_exact_legal_candidates
from .observed_primary_identity import (
    SCHEMA as SLOT_SCHEMA, normalize_observed_primary_candidates, observed_primary_candidate_index,
)

SCHEMA = 'gkms.original-pc-primary-policy-input.v1'
_KINDS = {1: 'use-hand', 2: 'use-drink', 3: 'turn-end'}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class OriginalPcPrimaryInput:
    state_before: Mapping
    legal_candidates: tuple[Mapping, ...]
    original_actions: tuple[Mapping, ...]
    pre_action_cost: Mapping | None
    observation: Mapping


def prepare_original_pc_primary(event: Mapping) -> OriginalPcPrimaryInput:
    """Use the observed native mask; never infer legality from the inventory.

    Local observation consistency is necessary but does not prove original
    source qualification, source Master compatibility or model suitability.
    File provenance and whole-episode qualification belong to the batch gate.
    """
    if (type(event.get('kind')) is not int or event['kind'] != 1
            or type(event.get('phase')) is not int or event['phase'] != 0
            or type(event.get('log_index')) is not int or event['log_index'] < 0):
        raise ValueError('Original PC primary decision entry required')
    before, after, mask = (event.get(k) for k in ('snapshot', 'after_candidates', 'candidates'))
    if not all(isinstance(x, Mapping) for x in (before, after, mask)):
        raise ValueError('Complete before/candidate/after observations required')
    for snapshot in (before, after):
        if (snapshot.get('schema') != 'gkms.original-pc-machine-state-snapshot.v1'
                or snapshot.get('mode') != 'direct-native-projection'
                or snapshot.get('state_complete') is not True or snapshot.get('pure') is not True
                or snapshot.get('read_errors') != [] or snapshot.get('unqualified') != []
                or not isinstance(snapshot.get('state'), Mapping)
                or snapshot.get('state_sha256') != _digest(snapshot['state'])):
            raise ValueError('Incomplete, mutated or unbound original PC state')
    state = before['state']
    if before['state_sha256'] != after['state_sha256']:
        raise ValueError('Candidate observation changed the original state')
    if type(state.get('phase')) is not int or state['phase'] != 6 or state.get('isExamEndComplete') is not False:
        raise ValueError('Original state is not a nonterminal Main boundary')
    if (mask.get('schema') != 'gkms.original-pc-main-candidates.v1'
            or mask.get('scope') != 'owned-simulator-decision-entry'
            or any(mask.get(k) is not True for k in
                ('complete', 'purity_verified', 'all_family_predicates_observed',
                 'hand_predicates_complete', 'drink_predicates_complete'))
            or mask.get('read_errors') != []
            or mask.get('state_before_sha256') != before['state_sha256']
            or mask.get('state_after_sha256') != after['state_sha256']):
        raise ValueError('Original PC primary candidate mask is incomplete or unbound')
    expected = []
    for family, field, kind in (('hand', 'handList', 1), ('drinks', 'drinkList', 2)):
        rows, inventory = mask.get(family), state.get(field)
        if not isinstance(rows, list) or not isinstance(inventory, list) or len(rows) != len(inventory):
            raise ValueError('Observed predicates do not cover the current ' + family)
        for slot, row in enumerate(rows):
            if row.get('slot_index') != slot or type(row.get('slot_index')) is not int or type(row.get('legal')) is not bool:
                raise ValueError('Invalid original candidate predicate slot')
            if row['legal']:
                expected.append({'type': kind, 'indexes': [slot], 'identity': row})
    if mask.get('end_turn', {}).get('legal') is not True:
        raise ValueError('Primary contract requires an observed legal turn-end action')
    expected.append({'type': 3, 'indexes': []})
    actions = mask.get('legal_actions')
    if (actions != expected or any(type(a.get('type')) is not int
            or any(type(x) is not int for x in a.get('indexes', [])) for a in actions)):
        raise ValueError('Original action list differs from the complete observed predicates')
    candidates = []
    for action in actions:
        kind = _KINDS[action['type']]
        slot = action['indexes'][0] if action['indexes'] else 0
        row = {'kind': kind, 'action_type': kind, 'slot_index': slot, 'legal': True}
        if kind == 'use-hand':
            identity, card = action['identity'], state['handList'][slot]
            data = card['_cardData']
            if (identity.get('guid') != card.get('_guid') or identity.get('id') != data.get('_id')
                    or identity.get('raw_upgrade_count') != data.get('_upgradeCount')
                    or identity.get('native_customize_counts') != data.get('_customizeCountList')
                    or not isinstance(identity.get('id'), str) or not identity['id']
                    or type(identity.get('raw_upgrade_count')) is not int):
                raise ValueError('Original candidate and current card identity disagree')
            row.update(card_guid=identity['guid'], card_id=identity['id'], upgrade=identity['raw_upgrade_count'])
        elif kind == 'use-drink':
            identity = action['identity']
            if identity.get('id') != state['drinkList'][slot].get('_id'):
                raise ValueError('Original candidate and current drink identity disagree')
            row['drink_id'] = identity['id']
        else:
            row['action_id'] = 'END_TURN'
        candidates.append(row)
    missing_guids = [i for i, row in enumerate(state['handList']) if row.get('_guid') is None]
    # Original slot predicates still identify these actions exactly. Requiring
    # a GUID is an existing model-interface limitation, not missing native data.
    # Never invoke GetGuid or substitute invented IDs to pass that old contract.
    if not missing_guids:
        validate_exact_legal_candidates(state, candidates)
    normalize_observed_primary_candidates(state, candidates)
    observation = {'schema': SCHEMA, 'authority': 'original-PC-owned-simulator-observation',
        'event_order': event.get('event_order'), 'source_action_order': event['log_index'],
        'state_sha256': before['state_sha256'], 'primary_mask_complete': True,
        'existing_exact_policy_compatible': not missing_guids,
        'original_null_guid_hand_slots': missing_guids,
        'observed_slot_identity_compatible': True, 'observed_slot_binding_schema': SLOT_SCHEMA,
        'source_identity_verified': False, 'source_master_qualified': False,
        'native_game_equivalence_verified': False, 'training_admitted': False,
        'secondary_supported': False, 'game_io': False}
    return OriginalPcPrimaryInput(deepcopy(state), tuple(candidates), tuple(deepcopy(actions)),
        deepcopy(mask.get('pre_action_cost')), observation)


def bind_original_primary_label(prepared: OriginalPcPrimaryInput, source_action: Mapping) -> int:
    """Bind a source label after input preparation; no future data in features."""
    if (type(source_action.get('order')) is not int
            or source_action['order'] != prepared.observation['source_action_order']
            or not isinstance(source_action.get('indexes'), list)
            or any(type(x) is not int for x in source_action['indexes'])):
        raise ValueError('Source action cursor differs from the observed decision')
    matches = [i for i, original in enumerate(prepared.original_actions)
        if _KINDS[original['type']] == source_action.get('action_type')
        and original['indexes'] == source_action.get('indexes')]
    if len(matches) != 1:
        raise ValueError('Source action has no unique original primary candidate')
    index = matches[0]
    if prepared.observation['existing_exact_policy_compatible']:
        return strict_exact_candidate_index(prepared.state_before, prepared.legal_candidates[index], prepared.legal_candidates)
    return observed_primary_candidate_index(prepared.state_before, prepared.legal_candidates[index], prepared.legal_candidates)
