"""Initial equipment imitation through a view of the existing shared actor.

No new parameters, checkpoint schema, reward head, optimizer or game owner.
Historical alternatives are a TRAIN demonstration vocabulary, not a claim
about another player's available inventory. Live availability is explicit.
"""
from copy import deepcopy
from collections.abc import Mapping
from dataclasses import dataclass, replace
import re

import torch
from torch import nn

from .contracts import ContractError, DecisionKind, digest, finite, integer, text
from .features import EntityBatch, _numeric, _category
from .observed_outer_policy import ObservedOuterPolicyNet
from .observed_outer_projection import ACTION_IDS
from .observed_outer_fine_projection import _tokens, _card_tokens
from .semantic_entity_encoding import ENTITY_FEATURE_NAMES, encode_semantic_tokens
from .offline_policy import OfflinePolicyNet, OrderedAction, _Rule, _STATE_FIELDS, offline_il_loss

SCHEMA = 'gkms.initial-loadout-policy-view.v1'
HISTORICAL_SEMANTICS = 'TRAIN-demonstration-vocabulary; historical-ownership-unobserved'
LIVE_SEMANTICS = 'live-owned-inventory; observed-availability-and-locks'
SEMANTICS = frozenset((HISTORICAL_SEMANTICS, LIVE_SEMANTICS))
FAMILIES = ('support', 'memory')
SUBTYPES = tuple('initial-loadout-' + family for family in FAMILIES)
MAX_CANDIDATES = 4096
_MODES = {'produce-004': 'nia-pro', 'produce-005': 'nia-master'}
_CONDITION = {'produce_id', 'idol_card_id', 'plan_type', 'exam_effect_type', 'idol_ranks'}
_SUPPORT = {'support_card_id', 'level', 'level_limit_rank', 'level_limit_rank_present', 'is_rental', 'rental_field_present'}
_MEMORY = {'idol_card_id', 'plan_type', 'produce_card_phase_type', 'produce_card', 'abilities', 'is_rental', 'rental_field_present'}
_TEMPLATE = {'key', 'family', 'role', 'facts', 'semantic_mode', 'source_master_hash', 'semantic_values'}


def _shape(value, fields, label):
    if not isinstance(value, dict) or set(value) != fields:
        raise ContractError(label + ': exact reviewed fields required')


def _boolean(value, label):
    if type(value) is not bool: raise ContractError(label + ': explicit boolean required')
    return value


def _rank(value, present, label, *, protocol_zero=False):
    _boolean(present, label + ' presence')
    if not present and value is None: return None
    if isinstance(value, str):
        match = re.fullmatch(r'(?:IdolCardLevelLimitRank|IdolCardPotentialRank|SupportCardLevelLimitRank)_{1,2}(\d+)', value)
        if match: value = int(match[1])
    integer(value, label)
    if not present and not (protocol_zero and value == 0):
        raise ContractError(label + ': missing fact cannot borrow a numeric default')
    return value


def canonical_condition(condition):
    _shape(condition, _CONDITION, 'initial condition')
    result = deepcopy(condition)
    for name in _CONDITION - {'idol_ranks'}: text(result[name], name)
    if result['produce_id'] not in _MODES: raise ContractError('Initial loadout supports explicit NIA modes only')
    ranks = result['idol_ranks']
    _shape(ranks, {'level_limit', 'potential', 'level_limit_present', 'potential_present'}, 'idol ranks')
    for name in ('level_limit', 'potential'):
        ranks[name] = _rank(ranks[name], ranks[name + '_present'], 'idol ' + name)
    return result


def _candidate_facts(family, facts):
    if family not in FAMILIES: raise ContractError('Explicit initial support/memory family required')
    _shape(facts, _SUPPORT if family == 'support' else _MEMORY, 'initial candidate facts')
    value = deepcopy(facts)
    _boolean(value['is_rental'], 'rental value'); _boolean(value['rental_field_present'], 'rental presence')
    if not value['rental_field_present'] and value['is_rental']:
        raise ContractError('An absent protobuf rental flag can only have its documented false default')
    if family == 'support':
        text(value['support_card_id'], 'support definition'); integer(value['level'], 'support level', 1)
        value['level_limit_rank'] = _rank(value['level_limit_rank'], value['level_limit_rank_present'], 'support rank')
    else:
        for name in ('idol_card_id', 'plan_type', 'produce_card_phase_type'): text(value[name], name)
        card = value['produce_card']
        if card is not None:
            _shape(card, {'id', 'upgrade_count', 'upgrade_count_present', 'customizes'}, 'inherited card')
            text(card['id'], 'card definition')
            card['upgrade_count'] = _rank(card['upgrade_count'], card['upgrade_count_present'], 'card upgrade', protocol_zero=True)
            if not isinstance(card['customizes'], list): raise ContractError('Recorded customization list required')
            for custom in card['customizes']:
                _shape(custom, {'id', 'count'}, 'customization'); text(custom['id'], 'customization ID')
                integer(custom['count'], 'customization count', 1)
            if len({x['id'] for x in card['customizes']}) != len(card['customizes']):
                raise ContractError('Duplicate customization identity')
            card['customizes'].sort(key=lambda x: x['id'])
        if not isinstance(value['abilities'], list): raise ContractError('Complete recorded memory abilities required')
        for ability in value['abilities']:
            _shape(ability, {'id', 'level'}, 'memory ability'); text(ability['id'], 'memory ability ID')
            integer(ability['level'], 'memory ability level', 1)
        value['abilities'].sort(key=lambda x: (x['id'], x['level']))
    return value


def canonical_candidate(family, facts, *, semantic_source=None, source_master_hash=None):
    """One identical feature path for every target/distractor/live template.

    Semantic source is the existing exact-Master initial material provider.
    None explicitly selects facts-only for the entire candidate domain. Local
    slot/GUID/rental keys are not accepted as feature facts.
    """
    value = _candidate_facts(family, facts)
    if not isinstance(source_master_hash, str) or not source_master_hash:
        raise ContractError('Candidate needs its actual source Master identity')
    mode = 'recorded-facts-only' if semantic_source is None else 'rich-static'
    if semantic_source is not None:
        semantic_source.validate_unchanged()
        if semantic_source.initial_binding['source_master_hash'] != source_master_hash:
            raise ContractError('Initial candidate material belongs to another Master')
    # Rental roles constrain live selection; the same card/level has the same
    # semantic representation whether owned, rented or from a demonstration.
    public = {key: child for key, child in value.items() if key not in ('is_rental', 'rental_field_present')}
    tokens = _tokens('initial_loadout.candidate.family', family)
    tokens += _tokens('initial_loadout.candidate.semantic_mode', mode)
    tokens += _tokens('initial_loadout.candidate.facts', public)
    if semantic_source is not None:
        source = semantic_source
        if family == 'support':
            for index, passive in enumerate(source.initial_passive_catalog().resolve_support_card(value['support_card_id'], value['level'])):
                if not passive.skill_id or type(passive.skill_level) is not int:
                    raise ContractError('Candidate support skill has no exact source definition')
                skill = {'id': passive.skill_id, 'level': passive.skill_level}
                tokens += source.skill_tokens(skill, f'initial_loadout.support.skill[{index}]')
        else:
            for index, ability in enumerate(value['abilities']):
                definition = source._one('MemoryAbility.yaml', ability['id'], level=ability['level'])
                prefix = f'initial_loadout.memory.ability[{index}]'
                tokens += _tokens(prefix + '.definition', definition)
                tokens += source.skill_tokens({'id': definition['skillId'], 'level': ability['level']}, prefix + '.skill')
            card = value['produce_card']
            if card is not None:
                upgrade = card['upgrade_count']
                if upgrade is None: raise ContractError('Static inherited card needs its explicit/protocol-default upgrade')
                static = source.payload['cards'].get(card['id'] + '@' + str(upgrade))
                if not isinstance(static, Mapping): raise ContractError('Inherited candidate card is absent from exact Master')
                ids = static.get('customize_ids', []); counts = [0] * len(ids)
                for custom in card['customizes']:
                    if custom['id'] not in ids: raise ContractError('Inherited candidate customization is absent from exact Master')
                    counts[ids.index(custom['id'])] = custom['count']
                tokens += _card_tokens({'card_id': card['id'], 'upgrade': upgrade, 'customizeCountList': counts}, source)
        source.validate_unchanged()
    result = {'family': family, 'role': 'rental' if value['is_rental'] else 'owned', 'facts': value,
        'semantic_mode': mode, 'source_master_hash': source_master_hash,
        'semantic_values': encode_semantic_tokens(tokens)}
    return {'key': digest(result), **result}


def _template(candidate):
    if not isinstance(candidate, dict) or set(candidate) - {'selection_key', 'available'} != _TEMPLATE:
        raise ContractError('Complete canonical candidate template required')
    body = {key: candidate[key] for key in _TEMPLATE - {'key'}}
    if digest(body) != candidate['key'] or _candidate_facts(candidate['family'], candidate['facts']) != candidate['facts']:
        raise ContractError('Initial candidate template identity/facts changed')
    values = candidate['semantic_values']
    if not isinstance(values, dict) or set(values) - set(ENTITY_FEATURE_NAMES):
        raise ContractError('Initial candidate uses undeclared semantic dimensions')
    for value in values.values(): finite(value, 'candidate feature')
    if candidate['semantic_mode'] not in ('rich-static', 'recorded-facts-only'):
        raise ContractError('Explicit candidate static coverage required')
    if candidate['role'] != ('rental' if candidate['facts']['is_rental'] else 'owned'):
        raise ContractError('Candidate rental role contradicts its observed fact')
    key = candidate.get('selection_key', candidate['key']); text(key, 'selection key')
    return key


def _duplicate_identity(candidate):
    return ('support', candidate['facts']['support_card_id']) if candidate['family'] == 'support' else (
        'memory', candidate.get('selection_key', candidate['key']))


def semantic_identity(candidate):
    """Alias only identical model-visible templates inside one source domain.

    Keep the original full key/role/instance for provenance and live masks.
    Historical vocabulary sampling/evaluation can use this key to avoid
    indistinguishable owned/rental aliases becoming false negatives or OOV.
    """
    _template(candidate)
    return digest({'schema': SCHEMA, **{name: candidate[name] for name in
        ('family', 'semantic_mode', 'source_master_hash', 'semantic_values')}})


class _BatchProjection:
    """Fixed-template work owned by one synchronous collate call only.

    Object identity, not the supplied content key, determines reuse: a different
    dictionary must pass the original validator even when it claims the same
    key. Strong references prevent identity reuse. Dynamic legality is never
    retained here, and neither this owner nor its private dictionaries escape
    the caller's immediate projection/packing operation.
    """
    def __init__(self):
        self._templates = {}

    def key(self, candidate):
        identity = id(candidate)
        if identity not in self._templates:
            self._templates[identity] = candidate, _template(candidate), None
        return self._templates[identity][1]

    def values(self, candidate):
        self.key(candidate)
        identity = id(candidate); original, key, values = self._templates[identity]
        if values is None:
            values = deepcopy(candidate['semantic_values'])
            self._templates[identity] = original, key, values
        return values


def project_initial_loadout(condition, selected_prefix, candidates, *, family, selection_semantics, schema, constraints=None,
        _prepared=None):
    """Project current condition and selected set only; no teacher suffix/score."""
    if _prepared is not None and type(_prepared) is not _BatchProjection:
        raise ContractError('Only the original collator may own batch template preparation')
    template_key = _template if _prepared is None else _prepared.key
    semantic_values = (lambda row: deepcopy(row['semantic_values'])) if _prepared is None else _prepared.values
    condition = canonical_condition(condition)
    if family not in FAMILIES or selection_semantics not in SEMANTICS:
        raise ContractError('Explicit initial-loadout subtype and source semantics required')
    if not isinstance(selected_prefix, (tuple, list)) or not isinstance(candidates, (tuple, list)):
        raise ContractError('Explicit chosen prefix and candidate inventory required')
    if not 1 <= len(candidates) <= MAX_CANDIDATES or len(selected_prefix) > 10:
        raise ContractError('Bounded initial candidate pool/prefix required')
    keys = [template_key(row) for row in candidates]; prefix_keys = [template_key(row) for row in selected_prefix]
    if len(set(keys)) != len(keys) or selection_semantics == LIVE_SEMANTICS and len(set(prefix_keys)) != len(prefix_keys):
        raise ContractError('Duplicate selectable candidate identity')
    domain = {(row['semantic_mode'], row['source_master_hash']) for row in (*candidates, *selected_prefix)}
    if len(domain) != 1: raise ContractError('Entire pool and prefix must share exact Master/static coverage; no rich teacher shortcut')
    duplicates = [_duplicate_identity(row) for row in selected_prefix]
    if selection_semantics == LIVE_SEMANTICS and len(set(duplicates)) != len(duplicates):
        raise ContractError('Chosen prefix repeats the same equipment instance/definition')
    counts = {kind: sum(row['family'] == kind for row in selected_prefix) for kind in FAMILIES}
    if counts['support'] > 6 or counts['memory'] > 4 or counts[family] >= (6 if family == 'support' else 4):
        raise ContractError('Initial selection exceeded the six-support/four-memory capacity')
    constraints = {} if constraints is None else constraints
    if not isinstance(constraints, dict) or set(constraints) - {'locked_keys', 'excluded_keys'}:
        raise ContractError('Only explicit initial equipment lock/exclusion constraints are accepted')
    if selection_semantics == HISTORICAL_SEMANTICS and constraints:
        raise ContractError('Historical ownership and locks are unobserved, not assumed legal')
    locks, excluded = (tuple(constraints.get(key, ())) for key in ('locked_keys', 'excluded_keys'))
    for values in (locks, excluded):
        if len(set(values)) != len(values) or any(not isinstance(key, str) or not key for key in values):
            raise ContractError('Unique explicit lock/exclusion identities required')
    if set(locks) & set(excluded) or set(prefix_keys) & set(excluded):
        raise ContractError('Equipment locks/exclusions contradict the selected prefix')
    if set(locks) - set(keys) - set(prefix_keys): raise ContractError('A locked equipment instance is unavailable')
    pending_locks = {key for key, row in zip(keys, candidates) if key in locks and key not in prefix_keys and row['family'] == family}
    own = sum(row['family'] == 'support' and row['role'] == 'owned' for row in selected_prefix)
    rental = sum(row['family'] == 'support' and row['role'] == 'rental' for row in selected_prefix)
    if selection_semantics == LIVE_SEMANTICS and (own > 5 or rental > 1):
        raise ContractError('Live support prefix exceeds five owned plus one rental')
    if selection_semantics == LIVE_SEMANTICS and any(row.get('available') is not True
            or row['family'] == 'memory' and row['role'] != 'owned' for row in selected_prefix):
        raise ContractError('Live selected prefix must retain actual available owned memories/support or rental support')
    masks = []
    for key, row in zip(keys, candidates):
        allowed = row['family'] == family and key not in excluded
        if pending_locks: allowed = allowed and key in pending_locks
        if selection_semantics == LIVE_SEMANTICS:
            _boolean(row.get('available'), 'live candidate availability')
            allowed = allowed and row['available'] and _duplicate_identity(row) not in duplicates
            if row['family'] == 'support': allowed = allowed and (own < 5 if row['role'] == 'owned' else rental < 1)
            elif row['role'] != 'owned': allowed = False
        elif 'available' in row:
            raise ContractError('Historical vocabulary must not claim account availability')
        masks.append(bool(allowed))
    if not any(masks): raise ContractError('No selectable initial candidate remains under the explicit constraints')
    tokens = _tokens('initial_loadout.condition', condition)
    tokens += _tokens('initial_loadout.family', family)
    tokens += _tokens('initial_loadout.selected_counts', counts)
    tokens += _tokens('initial_loadout.semantic_mode', next(iter(domain))[0])
    entities = [{'type': 'outer_context', 'zone': 'outer_current', 'definition_id': 'initial-loadout-context',
        'count': 1, 'values': encode_semantic_tokens(tokens)}]
    for row in sorted(selected_prefix, key=lambda x: (x['family'], x['key'])):
        entities.append({'type': 'support' if row['family'] == 'support' else 'context', 'zone': 'outer_current',
            'definition_id': 'initial-loadout-' + row['family'], 'count': 1, 'values': semantic_values(row)})
    if len(entities) > schema.max_entities: raise ContractError('Initial prefix exceeds model entity capacity')
    return {'schema': SCHEMA, 'decision_subtype': 'initial-loadout-' + family,
        'selection_semantics': selection_semantics, 'condition': condition,
        'stage': None, 'global': {}, 'entities': entities, 'candidate_keys': tuple(keys),
        'candidate_values': [semantic_values(row) for row in candidates], 'selection_mask': tuple(masks)}


def _encode_states(projected, schema):
    """Original entity numeric codec; absent exam-stage columns remain zero."""
    width = max(len(row['entities']) for row in projected); batch = len(projected)
    values = torch.zeros(batch, width, schema.entity_dim, dtype=torch.float32)
    kinds = torch.zeros(batch, width, dtype=torch.long); zones = torch.zeros_like(kinds); cards = torch.zeros_like(kinds)
    padding = torch.ones(batch, width, dtype=torch.bool); globals_ = []; vectors = {}
    for i, row in enumerate(projected):
        condition = row['condition']; flow = condition['plan_type'] + '|' + condition['exam_effect_type']
        global_ = _numeric(row['global'], schema.global_names, schema.global_scales)
        global_ += _category(flow, schema.flows) + _category(_MODES[condition['produce_id']], schema.modes)
        global_ += [0.] * len(schema.stages) + _category(DecisionKind.OUTER.value, tuple(x.value for x in DecisionKind))
        globals_.append(global_)
        for j, entity in enumerate(row['entities']):
            source = entity['values']; identity = id(source)
            if identity not in vectors:
                vectors[identity] = source, _numeric(source, schema.entity_names, schema.entity_scales)
            vector = vectors[identity][1] + [float(entity['count'])]
            values[i, j] = torch.tensor(vector)
            kinds[i, j] = schema.entity_types.index(entity['type']) + 1
            zones[i, j] = schema.zones.index(entity['zone']) + 1
            cards[i, j] = schema.vocabulary.index(entity['definition_id']) + 2 if entity['definition_id'] in schema.vocabulary else 1
            padding[i, j] = False
    return EntityBatch(torch.tensor(globals_, dtype=torch.float32), values, kinds, zones, cards, padding, schema.identity)


@dataclass(frozen=True)
class InitialLoadoutBatch:
    states: EntityBatch
    candidate_values: torch.Tensor
    candidate_types: torch.Tensor
    selection_mask: torch.Tensor
    decision_subtypes: tuple[str, ...]
    selection_semantics: tuple[str, ...]
    candidate_keys: tuple[tuple[str, ...], ...]

    def to(self, device):
        return replace(self, states=self.states.to(device), **{name: getattr(self, name).to(device)
            for name in ('candidate_values', 'candidate_types', 'selection_mask')})


def batch_from_projected(projected, schema, *, device='cpu'):
    projected = tuple(projected)
    if not projected or any(row.get('schema') != SCHEMA or row.get('stage') is not None for row in projected):
        raise ContractError('Typed initial-loadout projections required')
    width = max(len(row['candidate_values']) for row in projected); columns = {name: i for i, name in enumerate(ENTITY_FEATURE_NAMES)}
    values = torch.zeros(len(projected), width, len(columns), dtype=torch.float32)
    types = torch.zeros(len(projected), width, dtype=torch.long); masks = torch.zeros_like(types, dtype=torch.bool)
    for i, row in enumerate(projected):
        if len(row['candidate_values']) != len(row['candidate_keys']) or len(row['selection_mask']) != len(row['candidate_keys']):
            raise ContractError('Initial candidate columns differ from declared keys/masks')
        for j, candidate in enumerate(row['candidate_values']):
            for key, value in candidate.items(): values[i, j, columns[key]] = finite(value, 'candidate feature')
            types[i, j] = ACTION_IDS['select']; masks[i, j] = row['selection_mask'][j]
    return InitialLoadoutBatch(_encode_states(projected, schema), values, types, masks,
        tuple(row['decision_subtype'] for row in projected), tuple(row['selection_semantics'] for row in projected),
        tuple(tuple(row['candidate_keys']) for row in projected)).to(device)


def collate_initial_loadout(examples, schema, *, device='cpu'):
    examples = tuple(examples)
    prepared = _BatchProjection()
    projected = [project_initial_loadout(row['condition'], row['selected_prefix'], row['candidates'],
        family=row['family'], selection_semantics=row['selection_semantics'], schema=schema,
        constraints=row.get('constraints'), _prepared=prepared) for row in examples]
    batch = batch_from_projected(projected, schema, device=device); choices = []
    for row, item in zip(examples, projected):
        if row['target_key'] not in item['candidate_keys']: raise ContractError('Expert template is absent from this explicit candidate vocabulary')
        target = item['candidate_keys'].index(row['target_key'])
        if not item['selection_mask'][target]: raise ContractError('Expert initial choice violates the explicit prefix/role/lock mask')
        choices.append((target,))
    return {'batch': batch, 'actions': OrderedAction.from_sequences(choices, device=device)}


class InitialLoadoutPolicyView(OfflinePolicyNet):
    """Pure IL/selection view; all tensors still belong to one shared model."""
    def __init__(self, shared):
        if not isinstance(shared, ObservedOuterPolicyNet): raise ContractError('Initial loadout view requires the existing observed shared actor')
        nn.Module.__init__(self); self.shared = shared

    def checkpoint_metadata(self): return self.shared.checkpoint_metadata()
    @property
    def actor_init(self): return self.shared.actor_init
    @property
    def actor_gru(self): return self.shared.actor_gru
    def _logits(self, hidden, candidates): return self.shared._logits(hidden, candidates)

    def _encode_policy(self, batch):
        if not isinstance(batch, InitialLoadoutBatch): raise ContractError('Initial-loadout view rejects ordinary exam/fine/weekly batches')
        device = self.shared.global_proj[0].weight.device; values = batch.candidate_values
        if (values.ndim != 3 or values.shape[0] < 1 or not 1 <= values.shape[1] <= MAX_CANDIDATES
                or values.shape[2] != self.shared.candidate_dim or values.dtype != torch.float32
                or values.device != device or not torch.isfinite(values).all()):
            raise ContractError('Bounded initial candidate tensor on the shared device required')
        b, width, _ = values.shape
        if (batch.selection_mask.shape != (b, width) or batch.selection_mask.dtype != torch.bool
                or batch.candidate_types.shape != (b, width) or batch.candidate_types.dtype != torch.long
                or any(getattr(batch.states, name).device != device for name in _STATE_FIELDS)
                or batch.selection_mask.device != device or batch.candidate_types.device != device
                or len(batch.decision_subtypes) != b or len(batch.selection_semantics) != b or len(batch.candidate_keys) != b
                or batch.states.schema_id != self.shared.schema.identity):
            raise ContractError('Initial batch fields/schema/device disagree')
        stage_start = 2 * len(self.shared.schema.global_names) + len(self.shared.schema.flows) + len(self.shared.schema.modes)
        if torch.any(batch.states.global_values[:, stage_start:stage_start + len(self.shared.schema.stages)] != 0):
            raise ContractError('Initial loadout has no observed exam stage; cannot borrow Mid1/W1')
        self.shared._require_kind(batch.states, outer=True)
        rules = []
        for i, (subtype, semantics, keys) in enumerate(zip(batch.decision_subtypes, batch.selection_semantics, batch.candidate_keys)):
            if subtype not in SUBTYPES or semantics not in SEMANTICS or not keys or len(set(keys)) != len(keys) or len(keys) > width:
                raise ContractError('Initial batch subtype/vocabulary semantics are not explicit')
            if (torch.any(batch.candidate_types[i, :len(keys)] != ACTION_IDS['select'])
                    or torch.any(batch.candidate_types[i, len(keys):] != 0)
                    or torch.any(batch.selection_mask[i, len(keys):])):
                raise ContractError('Initial candidate family/padding mask differs')
            columns = tuple(batch.selection_mask[i].nonzero().flatten().tolist())
            if not columns: raise ContractError('Initial batch has no selectable candidate')
            rules.append(_Rule(columns, None))
        context = self.shared.encode(batch.states)[:, 0]
        candidates = self.shared.candidate_proj(values) + self.shared.action_types(batch.candidate_types)
        return context, candidates, tuple(rules)

    def _teacher_statistics(self, encoding, rows, plans, *, anchor=None, anchor_encoding=None):
        return self.shared._teacher_statistics(encoding, rows, plans,
            anchor=None if anchor is None else anchor.shared, anchor_encoding=anchor_encoding)

    def state_value(self, *args, **kwargs): raise ContractError('Initial loadout view provides pure imitation, not an invented value objective')
    def q_values(self, *args, **kwargs): raise ContractError('Initial loadout view provides pure imitation, not an invented reward')
    def forward(self, *args, **kwargs): raise ContractError('Use the explicit initial-loadout decoder or IL loss')


def initial_loadout_il_loss(shared, batch, actions, *, weights=None):
    """Return the original differentiable IL loss for the existing joint update."""
    if not isinstance(batch, InitialLoadoutBatch) or any(value != HISTORICAL_SEMANTICS for value in batch.selection_semantics):
        raise ContractError('Initial imitation loss requires the explicit demonstration-vocabulary contract')
    return offline_il_loss(InitialLoadoutPolicyView(shared), batch, actions, weights=weights)
