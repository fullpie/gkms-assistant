"""Owned PC observations -> existing Value entities using the reviewed host codec.

This is the source-specific mapping missing from the literal-path V2 mapper.
It does not run game rules, replay actions, manufacture intermediate selector
states or change BC features. Raw provenance/targets remain outside the network.
"""
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

from .contracts import Binding, ContractError, DecisionKind, FrozenJSON, Scope, State, canonical, digest
from .features import FeatureSchema
from .native_observation import inspect_native_decision
from .native_projection import ProjectionResult
from .native_information_view import PUBLIC_ROOT_NAMES, ROOT_FIELD_POLICY, public_root_values, public_semantic_tokens
from .semantic_entity_encoding import ENTITY_FEATURE_NAMES, encode_semantic_tokens

VERSION = 'gkms.rl.native-game-semantic-projection.v3'
ZONES = ('handList', 'deckList', 'graveList', 'lostList', 'holdList')
_CATEGORIES = frozenset({'planType', 'mainEffectType', 'displayMainEffectType', 'examType', 'stepType', 'phase'})
_GLOBALS = tuple(k for k in PUBLIC_ROOT_NAMES if k not in _CATEGORIES) + (
    'selector_minimum', 'selector_maximum', 'selector_offered_count', 'selector_is_hand',
    'selector_forced_empty', 'main_legal_count', 'main_end_allowed')
_ENTITY_TYPES = ('card', 'drink', 'status', 'gimmick', 'item', 'support', 'context', 'selector', 'offered')
_ENTITY_ZONES = (*ZONES, 'inventory', 'active', 'schedule', 'context', 'selector', 'offered')
_SOURCES = {'contract_set', 'original_shared_encoder', 'loader_compatibility', 'runtime_master_source'}
_PROJECTION_FILES = ('game_projection.py', 'native_information_view.py', 'semantic_entity_encoding.py')


def implementation_references():
    root = Path(__file__).parent
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in _PROJECTION_FILES}


def default_routes():
    from ..shared_bc_features import PLAN_EFFECTS, PLAN_BY_NATIVE_VALUE, EFFECT_BY_NATIVE_VALUE, STAGE_BY_NATIVE_VALUE
    return {'flows': [[p, e, PLAN_BY_NATIVE_VALUE[p] + '|' + EFFECT_BY_NATIVE_VALUE[e]] for p, e in PLAN_EFFECTS],
            'modes': [['produce-004', 'nia-pro'], ['produce-005', 'nia-master']],
            'stages': [[n, STAGE_BY_NATIVE_VALUE[n]] for n in (16, 17, 18)]}


def feature_schema(*, vocabulary=(), max_entities=256):
    routes = default_routes()
    # Native numbers remain explicit; semantic entity numbers use the separately
    # bound signed-log codec. Missing values keep the original presence mask.
    scales = tuple(1. if k.startswith('is') or k in ('selector_is_hand', 'selector_forced_empty', 'main_end_allowed')
                   else 10000. if k in ('parameter', 'produceTargetParameter', 'parameterVocal', 'parameterDance', 'parameterVisual',
                                        'npc_current_score_sum', 'npc_current_score_max')
                   else 1000. if 'Permil' in k else 100. for k in _GLOBALS)
    return FeatureSchema(_GLOBALS, scales, tuple(ENTITY_FEATURE_NAMES), (1.,) * len(ENTITY_FEATURE_NAMES),
        tuple(r[-1] for r in routes['flows']), tuple(r[-1] for r in routes['modes']),
        tuple(r[-1] for r in routes['stages']), _ENTITY_TYPES, _ENTITY_ZONES, tuple(vocabulary), max_entities)


@dataclass(frozen=True, slots=True)
class GameProjectionSpec:
    features: FeatureSchema
    rules: FrozenJSON

    def __post_init__(self):
        r = self.rules.unpack()
        if (set(r) != {'version', 'routes', 'sources', 'implementation'} or r['version'] != VERSION
                or r['routes'] != default_routes() or set(r['sources']) != _SOURCES
                or r['implementation'] != implementation_references()
                or self.features != feature_schema(vocabulary=self.features.vocabulary, max_entities=self.features.max_entities)):
            raise ContractError('The real-game projection needs its exact source mapping and implementation')
        for source in r['sources'].values():
            if not isinstance(source, dict) or not {'path', 'sha256'} <= set(source) <= {'path', 'sha256', 'bytes'}:
                raise ContractError('Explicit semantic source references required')

    @property
    def identity(self):
        return digest({'features': asdict(self.features), 'rules': self.rules.unpack()})

    @property
    def information_policy_id(self):
        return VERSION + ':' + self.identity

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {'features', 'rules'}:
            raise ContractError('Invalid game projection document')
        return cls(FeatureSchema.from_dict(value['features']), FrozenJSON.of(value['rules']))


def make_spec(sources, *, vocabulary=(), max_entities=256):
    return GameProjectionSpec(feature_schema(vocabulary=vocabulary, max_entities=max_entities), FrozenJSON.of({
        'version': VERSION, 'routes': default_routes(), 'sources': sources, 'implementation': implementation_references()}))


@lru_cache(maxsize=4)
def _encoder(encoded_sources):
    from ..runtime_loader_compatibility import load_runtime_loader_compatibility
    from ..runtime_master_source import load_runtime_master_source
    from ..runtime_master_features import RuntimeMasterFeatureEncoder
    source = json.loads(encoded_sources)
    compatibility = load_runtime_loader_compatibility(source['loader_compatibility'])
    runtime = load_runtime_master_source(source['runtime_master_source'])
    result = RuntimeMasterFeatureEncoder(source['contract_set'], source['original_shared_encoder'],
        runtime_source=runtime, source_resolver=compatibility.make_source_resolver(), loader_compatibility=compatibility)
    return result, compatibility


def _scope_matches(raw, scope, routes):
    for keys, family, expected in ((('planType', 'mainEffectType'), 'flows', scope.flow_id),
        (('produceId',), 'modes', scope.mode_id), (('stepType',), 'stages', scope.stage_id)):
        actual = [raw.get(k) for k in keys]
        found = [row[-1] for row in routes[family] if row[:-1] == actual
                 and all(type(a) is type(b) for a, b in zip(row[:-1], actual))]
        if found != [expected]:
            raise ContractError('Native scope differs from the game projection: ' + family)


@lru_cache(maxsize=32)
def _source_document(encoded_reference):
    reference = json.loads(encoded_reference)
    path = Path(reference['path'])
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != reference['sha256']:
        raise ContractError('Native source reference differs from its journal')
    return json.loads(payload)


def _check_asset_binding(obs, binding, package):
    inputs = _source_document(canonical(obs['engine_identity']['inputs_reference']))
    original = _source_document(canonical(inputs['reconstruction_manifest']))
    master = _source_document(canonical(inputs['master_manifest']))
    metadata = {row['sha256'] for row in inputs['runtime_files'] if row['path'].endswith('global-metadata.dat')}
    if (binding.engine_id != package.contract['native_core_sha256']
            or original['source_image_sha256'] != binding.engine_id
            or binding.metadata_sha256 != package.contract['native_metadata_sha256']
            or metadata != {binding.metadata_sha256}
            or master['archived_master_hash'] != binding.master_sha256):
        raise ContractError('Native source engine/metadata/Master and Value binding differ')
    return inputs['reconstruction_manifest']['sha256']


def _host_components(view, features):
    """Split the original pre-hash codec output by its actual semantic owner."""
    tokens, gaps, optional = features._context(view)
    if gaps:
        raise ContractError('Host semantic context incomplete: ' + '; '.join(gaps[:8]))
    groups, context = defaultdict(list), []
    for token in tokens:
        match = re.match(r'native\.zone\.(handList|deckList|graveList|lostList|holdList)\[(\d+)\]:(.*)', token)
        if match:
            groups[('card', match[1], int(match[2]))].append(match[3]); continue
        match = re.search(r'(?:native\.|:)(active)\[(\d+)\]', token)
        if match:
            index = int(match[2])
            groups[('status', 'active', index)].append(token.replace('active[' + str(index) + ']', 'active'))
            continue
        match = re.search(r'native\.(inventory\.drink|gimmick)\[(\d+)\]', token)
        if match:
            role = 'drink' if match[1] == 'inventory.drink' else 'gimmick'
            index = int(match[2])
            groups[(role, 'inventory' if role == 'drink' else 'schedule', index)].append(
                token.replace(match[0], 'native.' + match[1]))
            continue
        # class-count is useful aggregate current information; no active ordinal
        # or private reference ID is included in this context entity.
        context.append(token)
    return groups, context, optional


def _emit_group(prefix, value, features, *, table=None, identity=None):
    from ..native_structure_features import _emit
    tokens, gaps = [], []
    _emit(prefix, value, tokens, gaps)
    if table is not None:
        features._graph(table, identity, prefix + '.definition', tokens, gaps)
    features._native_links(value, prefix, tokens, gaps)
    if gaps:
        raise ContractError('Host semantic entity incomplete: ' + '; '.join(gaps[:8]))
    return tokens


def project_prepared_information(raw, features, contract, schema, *, primary=None, secondary=None,
                                 source_kind=None):
    """Shared pure semantics after a source-specific parser has verified input.

    Does not invent an owned-policy envelope or replace the source Master.
    The caller retains native/expert provenance and qualification separately.
    """
    from ..observed_empty_collections import _semantic_view, PC_SOURCE
    from ..integrated_exam_bc_features import _command_tokens
    source_kind = PC_SOURCE if source_kind is None else source_kind
    if (primary is None) == (secondary is None):
        raise ContractError('Exactly one verified main or secondary input is required')
    empty = contract['observed_empty_collection_contract']
    view, ledger = _semantic_view(raw, source_kind, inactive_status_contract=empty.get('inactive_card_status_contract'),
        optional_identifiers_contract=empty.get('optional_effect_identifiers_contract'))
    if secondary is not None and secondary.gaps:
        raise ContractError('Secondary parser has required gaps: ' + str(secondary.gaps[:4]))
    groups, context_tokens, optional = _host_components(view, features)
    unknown = set(raw) - set(ROOT_FIELD_POLICY)
    if unknown:
        raise ContractError('Unreviewed native root fields: ' + ', '.join(sorted(unknown)))
    globals_ = {k: float(v) if type(v) is bool else v
                for k, v in public_root_values(raw).items() if k not in _CATEGORIES}
    globals_.update({k: None for k in _GLOBALS if k not in globals_})
    entities, target_bindings, candidates = [], [], []
    owned_entities = {}

    def add(kind, zone, identity, tokens, target=None):
        public = public_semantic_tokens(tokens)
        entity = {'type': kind, 'zone': zone, 'definition_id': str(identity), 'count': 1,
                  'values': encode_semantic_tokens(public)}
        entities.append(entity)
        if target is not None:
            target_bindings.append({'target': target, 'semantic_entity_sha256': digest(entity)})
        return entity

    legal = {r['slot_index'] for r in primary.legal_candidates if r['kind'] == 'use-hand'} if primary else set()
    drink_legal = {r['slot_index'] for r in primary.legal_candidates if r['kind'] == 'use-drink'} if primary else set()
    cost = {r['slot_index']: r for r in (primary.pre_action_cost or {}).get('cards', [])} if primary else {}
    parent = secondary.current_context['command'].get('_playingCard') if secondary else None
    parent_guid = parent.get('_guid') if isinstance(parent, dict) else None
    for zone in ZONES:
        for index, card in enumerate(view[zone]):
            tokens = list(groups[('card', zone, index)])
            if not tokens:
                raise ContractError('Missing owner-bound card semantic tokens')
            if zone == 'handList':
                tokens.append('native.public_hand_position:int:' + str(index))
            if primary and zone == 'handList':
                tokens.append('native.available:bool:' + str(int(index in legal)))
                public_cost = {k: cost.get(index, {}).get(k) for k in ('native_card_cost', 'native_cost_type',
                    'formula_stamina_requirement', 'formula_block_requirement', 'ignores_block')}
                tokens += _emit_group('native.prospective_cost', public_cost, features)
            if secondary:
                tokens.append('native.selector_parent:bool:' + str(int(parent_guid is not None and card.get('_guid') == parent_guid)))
            owned_entities[(zone, index)] = add('card', zone, card['_cardData']['_id'], tokens,
                {'zone': zone, 'index': index, 'guid': card.get('_guid')})
    for index, drink in enumerate(view['drinkList']):
        tokens = list(groups[('drink', 'inventory', index)])
        if primary:
            tokens.append('native.available:bool:' + str(int(index in drink_legal)))
        owned_entities[('drinkList', index)] = add('drink', 'inventory', drink['_id'], tokens, {'type': 2, 'indexes': [index]})
    refs = {r['rid']: r for r in view['references']['RefIds']}
    for index, link in enumerate(view['status']['_effectList']):
        row = refs[link['rid']]
        tokens = groups[('status', 'active', index)]
        if not tokens:
            raise ContractError('Missing typed active status semantics')
        add('status', 'active', 'status:' + row['type']['class'],
            [*tokens, 'native.active_execution_order:int:' + str(index)])
    for index, gimmick in enumerate(view['gimmickList']):
        add('gimmick', 'schedule', gimmick['gimmickEffectId'],
            [*groups[('gimmick', 'schedule', index)], 'native.public_schedule_order:int:' + str(index)])
    for item in view.get('itemList', []):
        add('item', 'inventory', item['_id'], _emit_group('native.item', item, features,
            table='produce_item', identity=item['_id']))
    for support in view.get('supportCardList', []):
        tokens = _emit_group('native.support', support, features)
        if support.get('_cardSearchId'):
            tokens += _emit_group('native.support.search', support['_cardSearchId'], features,
                table='produce_card_search', identity=support['_cardSearchId'])
        add('support', 'inventory', support['_supportCardId'], tokens)
    public_context = {k: view.get(k) for k in ('turnStatusParameterTypeList', 'idolCardId',
        'currentTurnTriggeredStatusEnchantIdList', 'turnUseSupportCardIdList', 'planIgnoreProduceCardWhiteList')}
    context_tokens += _emit_group('native.public_context', public_context, features)
    add('context', 'context', 'current-context', context_tokens)
    if primary:
        globals_.update(main_legal_count=len(primary.original_actions), main_end_allowed=1.)
        target_bindings.extend({'target': a, 'role': 'main-action'} for a in primary.original_actions)
        for action in primary.original_actions:
            kind, indexes = action['type'], action['indexes']
            if kind in (1, 2) and len(indexes) == 1:
                values = owned_entities[('handList' if kind == 1 else 'drinkList', indexes[0])]['values']
            elif kind == 3 and indexes == []:
                values = {}
            else:
                raise ContractError('Unexpected verified main action shape')
            candidates.append({'type': kind, 'values': values, 'target': {'type': kind, 'indexes': list(indexes)}})
    else:
        c = secondary.constraints
        globals_.update(selector_minimum=c['pick_min'], selector_maximum=c['pick_max'],
            selector_offered_count=c['offered_count'], selector_is_hand=float(c['is_hand']),
            selector_forced_empty=float(c['selection_mode'] == 'forced-empty-response'))
        command_tokens, gaps, parent_views = _command_tokens(features, secondary.current_context['command'],
            secondary.current_context['references']['RefIds'], empty)
        if gaps:
            raise ContractError('Current selector semantics incomplete: ' + '; '.join(gaps[:8]))
        add('selector', 'selector', 'current-selector', command_tokens)
        for candidate in secondary.legal_candidates:
            zone, index = candidate['state_zone'], candidate['native_zone_index']
            card = view[zone][index]
            tokens = list(groups[('card', zone, index)]) + ['native.offered_zone:str:' + zone]
            if zone == 'handList':
                tokens.append('native.public_hand_position:int:' + str(index))
            # Selection targets retain exact ordinals in the sidecar. The
            # network sees every offered occurrence, never a raw deck slot.
            entity = add('offered', 'offered', candidate['card_id'], tokens,
                {'offered_ordinal': candidate['offered_ordinal'], 'zone': zone, 'index': index,
                 'guid': candidate['card_guid']})
            candidates.append({'type': 4, 'values': entity['values'], 'target': {'offered_ordinal': candidate['offered_ordinal']}})
    if len(entities) > schema.max_entities:
        raise ContractError('Native semantic entity budget exceeded; no truncation')
    entities.sort(key=canonical)
    information = FrozenJSON.of({'schema': schema.identity, 'global': globals_, 'entities': entities})
    return information, {'target_bindings': target_bindings, 'semantic_view_ledger': ledger,
                         'optional_derived_diagnostics': list(optional), 'entity_count': len(entities),
                         'candidates': candidates, 'constraints': dict(secondary.constraints) if secondary else
                         {'pick_min': 1, 'pick_max': 1, 'selection_mode': 'main', 'offered_count': len(candidates)}}


def project_game_decision(observation, spec, *, expected_observation_sha256, scope,
                          session_generation, binding):
    if not isinstance(spec, GameProjectionSpec) or not isinstance(binding, Binding) or not isinstance(scope, Scope):
        raise ContractError('Typed game projection arguments required')
    if (binding.feature_schema_sha256 != spec.features.identity or binding.information_policy_id != spec.information_policy_id
            or binding.objective_id != 'exam_score_v1'):
        raise ContractError('Game projection and model binding differ')
    checked = inspect_native_decision(observation, expected_observation_sha256=expected_observation_sha256)
    obs = checked.observation.unpack()
    if scope.run_id != obs['run_id'] or scope.exam_id is None:
        raise ContractError('Game projection belongs to another run/exam')
    raw, rules = obs['snapshot']['state'], spec.rules.unpack()
    _scope_matches(raw, scope, rules['routes'])
    try:
        encoder, compatibility = _encoder(canonical(rules['sources']))
        compatibility.validate_unchanged(); encoder.validate_runtime_source()
        if binding.master_sha256 != encoder.runtime_master_hash:
            raise ContractError('Actual execution Master differs from semantic projection')
        features, contract = encoder._route(binding.master_sha256, raw['produceId'])
        _check_asset_binding(obs, binding, features.package)
        from ..observed_empty_collections import _semantic_view, PC_SOURCE
        from ..original_pc_primary_input import prepare_original_pc_primary
        from ..native_secondary_input import prepare_native_secondary_input
        from ..integrated_exam_bc_features import _command_tokens
        primary = prepare_original_pc_primary(obs) if checked.kind == 'main' else None
        secondary = prepare_native_secondary_input(obs) if checked.kind == 'secondary' else None
        information, details = project_prepared_information(raw, features, contract, spec.features,
            primary=primary, secondary=secondary, source_kind=PC_SOURCE)
        state = State(scope, session_generation, checked.identity,
            DecisionKind.MAIN if checked.kind == 'main' else DecisionKind.SECONDARY, information, binding.identity)
        audit = FrozenJSON.of({'schema': VERSION, 'spec_sha256': spec.identity,
            'source_observation_sha256': checked.identity, 'source_state_sha256': checked.state_sha256,
            'decision_kind': checked.kind, 'feature_sha256': information.sha256, 'entity_count': details['entity_count'],
            'unmapped_fields': [], 'source_bytes_unchanged': True, 'intermediate_native_state_fabricated': False,
            'target_bindings': details['target_bindings'], 'semantic_view_ledger': details['semantic_view_ledger'],
            'optional_derived_diagnostics': details['optional_derived_diagnostics'], 'information_policy': dict(ROOT_FIELD_POLICY),
            'lossless_native_reconstruction_claimed': False, 'full_markov_state_claimed': False,
            'training_admitted': False, 'native_semantics_qualified': False, 'live_enabled': False})
        return ProjectionResult(state, audit)
    except (ValueError, KeyError, TypeError) as error:
        if isinstance(error, ContractError):
            raise
        raise ContractError('Real semantic projection blocked: ' + str(error)) from error
