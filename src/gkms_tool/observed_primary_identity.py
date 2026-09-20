"""Current native-slot identities for the new shared primary feature path.

Raw GUIDs are checked when supplied and may remain null. Slots bind this one
observed decision only; they are not persistent cross-turn card identities.
The caller still owns complete-mask/source qualification and live input CAS.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from .exact_policy_contract import _kind

SCHEMA = 'gkms.observed-primary-slot-binding.v1'


def _normalize(state, raw):
    if not isinstance(raw, Mapping) or raw.get('legal') is not True or raw.get('selected_card_guid'):
        raise ValueError('Authoritative primary candidate required')
    kind = _kind(raw)
    slot = raw.get('slot_index', raw.get('slot'))
    if type(slot) is not int or slot < 0:
        raise ValueError('Explicit current native slot required')
    row = dict(raw)
    row.update(kind=kind, action_type=kind, slot_index=slot)
    if kind == 'use-hand':
        hand = state.get('handList')
        if not isinstance(hand, list) or slot >= len(hand) or not isinstance(hand[slot], Mapping):
            raise ValueError('Candidate slot is outside the observed hand')
        card = hand[slot]
        data, guid = card.get('_cardData'), card.get('_guid')
        if not isinstance(data, Mapping) or '_guid' not in card:
            raise ValueError('Original card data and observed GUID field required')
        if guid is not None and (not isinstance(guid, str) or not guid):
            raise ValueError('Invalid observed card GUID')
        card_id, upgrade = data.get('_id'), data.get('_upgradeCount')
        if not isinstance(card_id, str) or not card_id or type(upgrade) is not int or upgrade < 0:
            raise ValueError('Observed card identity/effective upgrade is invalid')
        for key, observed in (('card_guid', guid), ('guid', guid), ('card_id', card_id), ('upgrade', upgrade)):
            if key in raw and (raw[key] != observed or (key == 'upgrade' and type(raw[key]) is not int)):
                raise ValueError('Supplied card identity differs from the current native slot')
        row.update(card_guid=guid, guid=guid, card_id=card_id, upgrade=upgrade)
    elif kind == 'use-drink':
        drinks = state.get('drinkList')
        if not isinstance(drinks, list) or slot >= len(drinks) or not isinstance(drinks[slot], Mapping):
            raise ValueError('Candidate slot is outside the observed drinks')
        drink_id = drinks[slot].get('_id')
        if not isinstance(drink_id, str) or not drink_id or raw.get('drink_id', drink_id) != drink_id:
            raise ValueError('Supplied drink identity differs from the current native slot')
        row['drink_id'] = drink_id
    elif kind == 'turn-end':
        if slot != 0 or raw.get('action_id', 'END_TURN') != 'END_TURN':
            raise ValueError('Invalid original turn-end binding')
        row['action_id'] = 'END_TURN'
    else:
        raise ValueError('Unsupported primary candidate kind')
    return row


def normalize_observed_primary_candidates(state: Mapping, candidates: Sequence) -> tuple[dict, ...]:
    """Keep supplied mask order and bind every candidate to this raw state."""
    if not isinstance(state, Mapping) or not candidates:
        raise ValueError('Observed state and nonempty primary mask required')
    rows = tuple(_normalize(state, c) for c in candidates)
    keys = [(r['kind'], r['slot_index']) for r in rows]
    if len(set(keys)) != len(keys) or keys.count(('turn-end', 0)) != 1:
        raise ValueError('Primary mask has duplicate slots or lacks one turn-end action')
    return rows


def observed_primary_candidate_index(state: Mapping, action: Mapping, candidates: Sequence) -> int:
    rows = normalize_observed_primary_candidates(state, candidates)
    action = _normalize(state, action)
    key = (action['kind'], action['slot_index'])
    matches = [i for i, row in enumerate(rows) if (row['kind'], row['slot_index']) == key]
    if len(matches) != 1:
        raise ValueError('Chosen action is outside the observed primary mask')
    return matches[0]


def observed_hand_card(state: Mapping, candidate: Mapping) -> Mapping:
    """Read effective card fields by the validated current slot for features."""
    row = _normalize(state, candidate)
    if row['kind'] != 'use-hand':
        raise ValueError('Hand candidate required')
    return state['handList'][row['slot_index']]
