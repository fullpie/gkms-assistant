"""Learned outer choice adapter; the existing gateway remains the only input owner.

The ranker projects consequences and invokes the same qualified shared exam
model. This adapter only accepts its bound result and resolves a current native
target. It neither submits commands nor adjusts model values with old rules.
"""
from collections.abc import Mapping
from copy import deepcopy
import math
from types import MappingProxyType

from .rl.contracts import ContractError, digest, sha256
from .rl.outer_value import SCHEMA as VALUE_SCHEMA

SCHEMA = 'gkms.runtime-shared-outer-value.v1'
_SURFACES = frozenset({'schedule', 'event', 'business', 'shop', 'interval',
    'drink_inventory', 'reward_drink_capacity'})
_FAMILIES = frozenset({'reward', 'reward_group', 'card-selector', 'customize', 'lesson-choice'})
_PURCHASE_ACTIONS = frozenset({'shop.buy', 'shop.open_product', 'interval.execute', 'interval.open_product'})
_DIRECT_PURCHASE_RESOURCES = frozenset({1, 3, 5})
_STORY_CONTROLS = frozenset({'story.confirm_skip', 'story.skip', 'story.advance',
    'event.skip_story', 'event.advance', 'event.confirm_choice'})


def requires_outer_comparison(raw):
    """Lifecycle confirmations keep their existing owner and never use Value."""
    actions = raw.get('legal_actions')
    if isinstance(actions, list) and actions and all(a.get('action_id') in _STORY_CONTROLS for a in actions):
        # Event presenters also own message-touch and menu-skip. Multiple
        # navigation callbacks do not make an event choice; keep the existing
        # runner's lifecycle routing. Any real choice keeps the learned gate.
        return False
    if isinstance(actions, list) and any(a.get('action_id') == 'customize.execute' for a in actions):
        # An enabled payment is not a forced navigation step. Back/finish may
        # not yet be exposed, and the old selector would still rank its price.
        return True
    if isinstance(actions, list) and any(a.get('action_id') in _PURCHASE_ACTIONS
            and (a.get('target') or {}).get('resource_type') not in (997, 998) for a in actions):
        return True
    if isinstance(actions, list) and len(actions) <= 1:
        folders = (raw.get('ui_state') or {}).get('folders')
        if raw.get('surface') == 'business' and isinstance(folders, list):
            # Native controls are a readiness-filtered view of the full offer
            # pool. A selected offer can still be waiting for its Start target.
            return sum(row.get('can_go') is True for row in folders) > 1
        return False
    if raw.get('surface') in ('shop', 'interval') and (raw.get('ui_state') or {}).get('phase') in (
            'confirm_purchase', 'confirm_finish'):
        return False
    return raw.get('surface') in _SURFACES or (raw.get('ui_state') or {}).get('family') in _FAMILIES


def _business_controls_pending(raw):
    """Do not rank a temporary subset of the already observed business pool."""
    ui = raw.get('ui_state') or {}
    folders = ui.get('folders')
    if raw.get('surface') != 'business' or not isinstance(folders, list):
        return []
    pending = []
    for row in folders:
        if row.get('can_go') is not True:
            continue
        if type(row.get('selected')) is not bool:
            raise ContractError('Native business selected flag is unavailable')
        name = 'business.start' if row['selected'] else 'business.choose'
        identity = {key: row.get(key) for key in ('index', 'business_type', 'business_number')}
        if any(type(value) is not int for value in identity.values()):
            raise ContractError('Native business offer identity is incomplete')
        matches = [action for action in raw.get('legal_actions', [])
            if action.get('action_id') == name and all(
                (action.get('target') or {}).get(key) == value for key, value in identity.items())]
        if len(matches) > 1:
            raise ContractError('Native business offer has ambiguous current callbacks')
        if not matches or (row['selected'] and row.get('start_ready') is not True):
            pending.append({'action_id': name, **identity})
    return pending


class RuntimeOuterValuePolicy:
    def __init__(self, *, run_id, produce_id, idol_card_id, checkpoint_sha256, ranker,
                 cancelled=lambda: False):
        for value in (run_id, produce_id, idol_card_id):
            if not isinstance(value, str) or not value:
                raise ContractError('Fixed run/idol/mode required for learned outer policy')
        sha256(checkpoint_sha256, 'fixed outer model')
        if not callable(ranker) or not callable(cancelled):
            raise ContractError('Qualified ranker and cancellation callback required')
        self.context = MappingProxyType({'run_id': run_id, 'produce_id': produce_id, 'idol_card_id': idol_card_id,
            'checkpoint_sha256': checkpoint_sha256})
        self.ranker, self.cancelled = ranker, cancelled
        self._card_proposal = None
        self._confirmed_card = None
        self._purchase_proposal = None
        self._confirmed_purchase = None

    @property
    def identity(self):
        return {'schema': SCHEMA, **self.context, 'policy_role': 'outer-candidate-model-value',
            'legacy_ranking_applied': False}

    def choose(self, native, *, operation_context=None):
        raw = native.raw
        if not requires_outer_comparison(raw):
            return None
        self._card_proposal = self._confirmed_card = None
        self._purchase_proposal = self._confirmed_purchase = None
        metadata = {**self.identity, 'source': 'shared-outer-value', 'model_applied': False}
        actions = raw.get('legal_actions') or []
        if (len(actions) == 1 and actions[0].get('action_id') in _PURCHASE_ACTIONS
                and actions[0].get('target', {}).get('resource_type') not in _DIRECT_PURCHASE_RESOURCES | {997, 998}):
            return None, {**metadata, 'status': 'model-unavailable', 'legacy_rule_fallback': False,
                'reason': 'this resource type has no qualified purchase outcome',
                'unavailable': [{'reason': 'learned-purchase-resource-not-supported'}]}
        if self.cancelled():
            return None, {**metadata, 'status': 'cancelled', 'reason': 'outer model evaluation cancelled'}
        if (raw.get('busy') is not False or raw.get('actions_complete') is not True
                or raw.get('exam_continuation') is True):
            raise ContractError('Learned outer choice requires a settled non-exam native owner')
        if (raw.get('state') or {}).get('produce_id') not in (None, self.context['produce_id']):
            raise ContractError('Observed mode differs from the frozen outer policy')
        if (raw.get('progress') or {}).get('idolCardId') not in (None, self.context['idol_card_id']):
            raise ContractError('Observed idol differs from the frozen outer policy')
        pending_controls = _business_controls_pending(raw)
        if pending_controls:
            return None, {**metadata, 'status': 'waiting', 'legacy_rule_fallback': False,
                'reason': 'waiting for the current callbacks of all observed affordable business offers',
                'waiting_for': pending_controls}
        actions = native.actions
        legal = {digest(row['target']): row['target'] for row in actions}
        if not legal or len(legal) != len(actions):
            raise ContractError('Complete unique native outer targets required')
        observation = {**self.context, 'session_generation': native.session_generation,
            'revision': native.revision, 'snapshot_sha256': digest(raw),
            'operation_context_sha256': digest(operation_context)}
        report = self.ranker(native, context=dict(self.context), operation_context=deepcopy(operation_context))
        if self.cancelled():
            return None, {**metadata, 'status': 'cancelled', 'reason': 'outer model evaluation cancelled'}
        if digest(raw) != observation['snapshot_sha256']:
            raise ContractError('Outer ranker changed the observed snapshot')
        if digest(operation_context) != observation['operation_context_sha256']:
            raise ContractError('Outer operation quote changed during model evaluation')
        if (not isinstance(report, Mapping) or report.get('schema') != VALUE_SCHEMA
                or report.get('checkpoint_sha256') != self.context['checkpoint_sha256']
                or report.get('observation') != observation):
            raise ContractError('Outer ranking belongs to another observation or model')
        if report.get('status') != 'ready':
            return None, {**metadata, 'status': 'model-unavailable',
                'reason': 'complete learned outer comparison unavailable',
                'unavailable': deepcopy(report.get('unavailable', [])), 'legacy_rule_fallback': False}
        if report.get('model_used') is not True or report.get('legacy_rule_fallback') is not False:
            raise ContractError('Ready outer decision must come from the actual model')
        coverage = report.get('action_coverage')
        if not isinstance(coverage, list):
            raise ContractError('Outer ranking has no complete native action coverage')
        covered = {}
        for row in coverage:
            key = digest(row.get('native_target'))
            if key not in legal or key in covered or row.get('handling') not in ('compared', 'fixed-lifecycle'):
                raise ContractError('Outer ranking omitted or misclassified a native action')
            covered[key] = row['handling']
        if set(covered) != set(legal):
            raise ContractError('Outer ranking omitted native actions')
        rankings = report.get('ranking')
        if not isinstance(rankings, list) or not rankings:
            raise ContractError('Ready outer model must supply ranked complete plans')
        ids, represented = set(), set()
        for row in rankings:
            identity, value = row.get('candidate_id'), row.get('value')
            if not isinstance(identity, str) or not identity or identity in ids:
                raise ContractError('Ranked strategic plans require unique IDs')
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ContractError('Ranked outer model value must be finite')
            ids.add(identity)
            key = digest(row.get('native_target'))
            if key not in legal or covered[key] != 'compared':
                raise ContractError('Model plan escaped the current legal native target set')
            represented.add(key)
        # A joint selection plan can represent other valid orders leading to
        # the same final set. Such coverage must be declared by the projector;
        # it is never used as a license for unknown outcomes.
        alternate = {digest(row['native_target']) for row in coverage
            if row.get('reason') == 'represented-in-complete-unordered-plan'}
        if {key for key, value in covered.items() if value == 'compared'} - represented - alternate:
            raise ContractError('A compared native action has no complete strategic plan')
        best = min(rankings, key=lambda row: (-row['value'], row['candidate_id']))
        target = deepcopy(legal[digest(best['native_target'])])
        if (target.get('action_id') in _PURCHASE_ACTIONS
                and target.get('resource_type') not in _DIRECT_PURCHASE_RESOURCES | {997, 998}):
            return None, {**metadata, 'status': 'model-unavailable', 'legacy_rule_fallback': False,
                'reason': 'this resource type has no qualified purchase outcome',
                'unavailable': [{'reason': 'learned-purchase-resource-not-supported'}]}
        self._remember_card_proposal(native, target, best, operation_context)
        self._remember_purchase_proposal(native, target, best)
        return target, {**metadata, 'status': 'ready', 'model_applied': True,
            'reason': 'fixed shared model ranked complete outer consequences',
            'candidate_id': best['candidate_id'], 'model_value': best['value'],
            'units': report.get('units'), 'calibrated': False,
            'model_rankings': deepcopy(rankings), 'action_coverage': deepcopy(coverage),
            'observation': observation, 'assumptions': deepcopy(report.get('assumptions', [])),
            'ranking_basis': best.get('ranking_basis', report.get('ranking_basis')),
            'value_interval': deepcopy(best.get('value_interval')),
            'future_exam_scenario': deepcopy(report.get('future_exam_scenario')),
            'currently_resource_eligible_exam': deepcopy(report.get('currently_resource_eligible_exam')),
            'legacy_rule_fallback': False}

    @staticmethod
    def _quote_identity(context):
        if not isinstance(context, Mapping) or not context.get('origin_receipt'):
            return None
        return digest({key: context.get(key) for key in ('schema', 'surface', 'session_generation',
            'parent_instance_id', 'progress_scope', 'action_id', 'origin_target', 'quote',
            'request_id', 'submission_status', 'origin_receipt')})

    @staticmethod
    def _card_identity(row):
        from .runtime_economy_policy import _card_signature
        if not isinstance(row, Mapping):
            return None
        if type(row.get('deck_number')) is int:
            identity = ('number', row['deck_number'])
        elif (isinstance(row.get('instance_key'), str) and row['instance_key']
                and type(row.get('index')) is int):
            # Real selector models may expose only their stable item identity.
            # Never turn a same-signature arithmetic representative into a
            # fabricated native Number.
            identity = ('selector', row['instance_key'], row['index'])
        else:
            return None
        return identity, _card_signature(row)

    @staticmethod
    def _deck_identity(raw):
        from .deck_value import normalize_deck
        from .runtime_economy_policy import _card_signature
        deck = normalize_deck(raw['collections']['cards'])
        return digest([(row.get('deck_number'), _card_signature(row)) for row in deck])

    @staticmethod
    def _resource_identity(raw):
        """Only mechanical state used by the comparison, not UI selection flags."""
        state, progress, collections = raw.get('state') or {}, raw.get('progress') or {}, raw.get('collections') or {}
        return digest({'deck': RuntimeOuterValuePolicy._deck_identity(raw),
            'state': {key: state.get(key) for key in ('produce_id', 'week', 'step_type',
                'vocal', 'dance', 'visual', 'stamina', 'max_stamina', 'produce_points',
                'vote_count', 'star', 'star_permil')},
            'progress': {key: progress.get(key) for key in ('idolCardId', 'produceDrinkIds', 'produceItems',
                'vocalGrowthRatePermil', 'danceGrowthRatePermil', 'visualGrowthRatePermil',
                'produceDrinkPossessLimit', 'characterProduceSkills', 'idolCardProduceSkills',
                'auditionEffectParameterBonusPermil', 'selfLessonTypeStaminaPermils')},
            'passives': {key: collections.get(key) for key in ('support_cards', 'memories', 'effects')}})

    @staticmethod
    def _product_identity(row, surface):
        from .runtime_economy_policy import _quote
        return digest({'quote': _quote(row, surface), 'effects': {key: row.get(key) for key in
            ('card', 'data', 'stamina_recover_value', 'is_legend', 'remaining_count')}})

    @staticmethod
    def _ranking_evidence(ranking):
        # Direct actor preferences are not values or predicted score units.
        if 'actor_logit' in ranking:
            return {'actor_logit': ranking['actor_logit'], 'ranking_basis': 'shared-actor-argmax'}
        return {'model_value': ranking['value']}

    def _remember_purchase_proposal(self, native, target, ranking):
        if target.get('action_id') not in _PURCHASE_ACTIONS or target.get('resource_type') not in _DIRECT_PURCHASE_RESOURCES:
            return
        from .runtime_economy_policy import _quote
        surface = native.raw.get('surface')
        if surface not in ('shop', 'interval'):
            raise ContractError('Purchase target is not owned by the observed economy surface')
        rows = [row for row in (native.raw.get('ui_state') or {}).get('products', ())
            if all(target.get(key) == value for key, value in _quote(row, surface).items())]
        if len(rows) != 1 or rows[0].get('eligible') is not True:
            raise ContractError('Learned purchase must bind one actual eligible product quote')
        row = rows[0]
        points = (native.raw.get('ui_state') or {}).get('produce_points')
        if (type(points) is not int or type(row.get('price')) is not int or row['price'] < 0 or row['price'] > points
                or row.get('can_buy') is not True or row.get('purchased') is not False):
            raise ContractError('Learned purchase no longer fits the native wallet or availability')
        self._purchase_proposal = {'target': deepcopy(target), 'revision': native.revision,
            'session_generation': native.session_generation, 'resource_identity': self._resource_identity(native.raw),
            'product_identity': self._product_identity(row, surface),
            **self._ranking_evidence(ranking), 'candidate_id': ranking['candidate_id']}

    def _remember_card_proposal(self, native, target, ranking, context):
        ui = native.raw.get('ui_state') or {}
        self._card_proposal = self._confirmed_card = None
        quote = self._quote_identity(context)
        if ui.get('family') != 'card-selector' or ui.get('selection_type') not in ('Delete', 'Upgrade') or quote is None:
            return
        name = target['action_id']; rows = ui.get('candidates', ())
        if name in ('card_choice.select', 'card_choice.reveal'):
            chosen = [row for row in rows if row.get('index') == target.get('index')]
        elif name == 'card_choice.confirm':
            chosen = [row for row in rows if row.get('selected') is True]
        else:
            return
        if len(chosen) == 1 and self._card_identity(chosen[0]) is not None:
            self._card_proposal = {'quote_identity': quote, 'card_identity': self._card_identity(chosen[0]),
                'selection_type': ui['selection_type'], 'session_generation': native.session_generation,
                'deck_identity': self._deck_identity(native.raw),
                **self._ranking_evidence(ranking), 'candidate_id': ranking['candidate_id']}

    def observe_settled(self, native, target, outcome, operation_context):
        """A proposal alone never authorizes payment; require the actual receipt."""
        purchase = self._purchase_proposal
        if purchase is not None and target.get('action_id') in _PURCHASE_ACTIONS:
            self._confirmed_purchase = None
            context = operation_context or {}
            quote_identity = self._quote_identity(context)
            if (outcome.status == 'settled' and quote_identity is not None
                    and target == purchase['target'] and context.get('origin_target') == target
                    and context.get('request_id') == outcome.request_id
                    and context.get('source_revision') == purchase['revision']
                    and context.get('session_generation') == purchase['session_generation']
                    and native.session_generation == purchase['session_generation']):
                self._confirmed_purchase = {**purchase, 'quote_identity': quote_identity,
                    'purchase_request_id': outcome.request_id}
            # A skip-confirm purchase can finish immediately and clear context.
            # Its actual settled result does not authorize any later payment.
            self._purchase_proposal = None
        proposal = self._card_proposal
        if proposal is None or outcome.status != 'settled':
            return
        if self._quote_identity(operation_context) != proposal['quote_identity']:
            self._card_proposal = self._confirmed_card = None
            return
        if target.get('action_id') != 'card_choice.confirm':
            return
        selected = operation_context.get('card_selection') or {}
        if (selected.get('request_id') != outcome.request_id or selected.get('submission_status') != 'settled'
                or selected.get('session_generation') != proposal['session_generation']
                or native.session_generation != proposal['session_generation']
                or selected.get('selected_count') != 1
                or selected.get('selection_type') != proposal['selection_type']
                or self._card_identity(selected.get('selected_card')) != proposal['card_identity']):
            self._confirmed_card = None
            return
        self._confirmed_card = {**proposal, 'selection_request_id': outcome.request_id}

    def approve_economy_confirmation(self, raw, context, row):
        """Called only after the original economy policy checked the live quote."""
        purchase = self._confirmed_purchase
        if row.get('resource_type') in _DIRECT_PURCHASE_RESOURCES:
            if (purchase is None or self.cancelled()
                    or self._quote_identity(context) != purchase['quote_identity']
                    or (context or {}).get('request_id') != purchase['purchase_request_id']):
                return None
            try:
                if (self._resource_identity(raw) != purchase['resource_identity']
                        or self._product_identity(row, raw.get('surface')) != purchase['product_identity']):
                    return None
            except (KeyError, TypeError, ValueError):
                return None
            return {**self.identity, 'model_applied': True,
                **{key:purchase[key]for key in ('model_value','actor_logit','ranking_basis')if key in purchase},
                'candidate_id': purchase['candidate_id'], 'purchase_request_id': purchase['purchase_request_id'],
                'legacy_rule_fallback': False, 'fresh_native_quote_verified': True}
        approved = self._confirmed_card
        selected = (context or {}).get('card_selection') or {}
        if (approved is None or self.cancelled() or self._quote_identity(context) != approved['quote_identity']
                or selected.get('request_id') != approved['selection_request_id']
                or self._card_identity(selected.get('selected_card')) != approved['card_identity']):
            return None
        try:
            from .deck_value import normalize_deck
            from .runtime_card_choice_policy import _deck_index
            _deck_index(normalize_deck(raw['collections']['cards']), selected['selected_card'])
            if self._deck_identity(raw) != approved['deck_identity']:
                return None
        except (KeyError, TypeError, ValueError):
            return None
        return {**self.identity, 'model_applied': True,
            **{key:approved[key]for key in ('model_value','actor_logit','ranking_basis')if key in approved},
            'candidate_id': approved['candidate_id'], 'selection_request_id': approved['selection_request_id'],
            'legacy_rule_fallback': False, 'fresh_native_quote_verified': True}
