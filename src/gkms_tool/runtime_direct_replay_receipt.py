"""Normal DLL replay callbacks, separate from local pending-phase changes."""
from collections.abc import Mapping


# Exact original PC StartReplayAsync.MoveNext call/return pairs. These are
# rejection branches after the awaited RestoreAsync or its caught exception.
_REJECTION_TOAST_CALLS = {
    '0x2523080': '0x2523085',
    '0x252294c': '0x2522951',
    '0x2523107': '0x252310c',
}


def _mapping(value):
    return value if isinstance(value, Mapping) else {}


def _identity(value):
    return isinstance(value, str) and bool(value) and value != '0x0'


def _same_pointer(left, right):
    # The outer reader uses decimal strings; the direct adapter uses hex.
    def parse(value):
        if not isinstance(value, str) or not value or not value.isascii():
            return None
        hexadecimal = value.startswith('0x')
        digits = value[2:] if hexadecimal else value
        alphabet = '0123456789abcdefABCDEF' if hexadecimal else '0123456789'
        if not digits or any(character not in alphabet for character in digits):
            return None
        number = int(value, 16 if hexadecimal else 10)
        return number if 0 < number < 2 ** 64 else None
    expected = parse(left)
    return expected is not None and expected == parse(right)


def _archive_identity_matches(state, target):
    if target.get('archived_replay') is not True:
        return state.get('archived_replay') is not True
    proof = _mapping(state.get('archive_identity'))
    if state.get('archived_replay') is not True:
        return False
    for key in ('source_sha256', 'archive_input_sha256'):
        value = target.get(key)
        if (not isinstance(value, str) or len(value) != 64
                or any(c not in '0123456789abcdef' for c in value) or proof.get(key) != value):
            return False
    return (_identity(target.get('archive_request_id'))
            and proof.get('request_id') == target['archive_request_id']
            and _identity(target.get('session_generation'))
            and proof.get('session_generation') == target['session_generation']
            and all(proof.get(key) == target.get(key) for key in ('produce_id', 'idol_card_id')))


def _archived_return_proof(ui, state, returned, target, selected):
    diagnostic = _mapping(returned.get('return_diagnostic'))
    profile = _mapping(diagnostic.get('return_profile'))
    if (selected.get('archived_replay') is not True
            or not _archive_identity_matches(state, selected)
            or state.get('archive_completed') is not True
            or returned.get('archived_scope') is not True
            or any(returned.get(key) is not True for key in (
                'context_return_data_verified', 'return_profile_verified', 'retained_archived_source_verified'))
            or any(key not in returned or returned[key] is not None for key in (
                'cached_item_membership_verified', 'cached_return_item_membership_verified'))
            or diagnostic.get('archived_scope') is not True
            or diagnostic.get('basis') != 'archived-retained-source-normal-context-return-and-origin-profile'
            or any(diagnostic.get(key) is not True for key in (
                'return_ready', 'context_return_data_verified', 'retained_source_verified',
                'adapter_did_not_modify_page_or_return_cache'))
            or any(diagnostic.get(key) is not False for key in ('page_membership_required', 'cache_membership_required'))):
        return False
    for key in ('produce_id', 'idol_card_id', 'producer_level_min', 'producer_level_max'):
        if key not in selected or profile.get(key) != selected[key] or ui.get(key) != selected[key]:
            return False
    if any(type(selected[key]) is not int for key in ('producer_level_min', 'producer_level_max')):
        return False
    return all(_same_pointer(returned.get(key), target.get(key))
               for key in ('sequence_id', 'replay_context_instance_id', 'return_data_instance_id'))


def direct_replay_receipt(before, after, target):
    """Return completion only from the actual owned native replay lifecycle."""
    action = target.get('action_id')
    if action not in {'replay.direct.open', 'replay.direct.start', 'replay.direct.exit', 'replay.direct.home'}:
        return None
    ui = _mapping(after.get('ui_state'))
    state = _mapping(ui.get('direct_replay'))
    if (target.get('direct_replay') is not True
            or not all(_identity(target.get(key)) for key in ('produce_id', 'idol_card_id'))
            or _mapping(after.get('state')).get('in_progress') is not False
            or after.get('screen_type') != after.get('underlying_screen_type')
            or state.get('error') or state.get('ready') is False
            or not _archive_identity_matches(state, target)):
        return None
    receipt = {'schema': 'gkms.native-direct-replay-receipt.v1', 'action_id': action,
               'before_revision': before.get('revision'), 'after_revision': after.get('revision'),
               'owner_token': state.get('owner_token')}
    if target.get('archived_replay') is True:
        receipt.update(archived_replay=True, source_sha256=target['source_sha256'],
                       archive_input_sha256=target['archive_input_sha256'], archive_request_id=target['archive_request_id'])
    if action == 'replay.direct.open':
        if (after.get('busy') is not False
                or after.get('screen_type') != 'ProducerRankingRecommendReplayScreenPresenter'
                or state.get('phase') != 'ready' or not _identity(state.get('owner_token'))
                or ui.get('produce_id') != target.get('produce_id')
                or ui.get('idol_card_id') != target.get('idol_card_id')):
            return None
        if target.get('archived_replay') is True:
            archive = _mapping(ui.get('archived_scope'))
            if (archive.get('retained_source_verified') is not True
                    or archive.get('source_sha256') != target['source_sha256']
                    or archive.get('archive_input_sha256') != target['archive_input_sha256']
                    or archive.get('request_id') != target['archive_request_id']
                    or archive.get('original_page_models_unchanged') is not True
                    or archive.get('page_membership_required_for_archived_item') is not False):
                return None
            receipt.update(archived_replay=True, source_sha256=target['source_sha256'],
                           archive_input_sha256=target['archive_input_sha256'], archive_request_id=target['archive_request_id'])
        return {**receipt, 'completion': 'owned-recommendation-page-ready'}
    if not _identity(target.get('owner_token')) or state.get('owner_token') != target['owner_token']:
        return None
    attempt = target.get('attempt')
    if type(attempt) is not int or attempt < 0:
        return None
    if action == 'replay.direct.home':
        returned = _mapping(state.get('last_home'))
        selected = _mapping(target.get('selected_source'))
        if (after.get('busy') is not False or after.get('screen_type') != 'HomeTopScreenPresenter'
                or state.get('phase') != 'home-returned'
                or type(state.get('attempt')) is not int or state['attempt'] != attempt
                or selected.get('action_id') != 'replay.direct.start'
                or selected.get('owner_token') != target['owner_token']
                or not _archive_identity_matches(state, selected)
                or returned.get('selected_source') != selected
                or returned.get('source_bound') is not True
                or returned.get('original_navigation_invoked') is not True
                or not _same_pointer(returned.get('returned_home_instance_id'), after.get('screen_instance_id'))):
            return None
        return {**receipt, 'completion': 'owned-original-home-return-observed',
                'selected_source': dict(selected), 'native_home': dict(returned)}
    if action == 'replay.direct.start':
        rejected = _mapping(state.get('last_start_result'))
        if state.get('phase') == 'start-rejected':
            required = ('branch_observed', 'source_bound', 'toast_return_observed',
                        'move_next_return_observed', 'fresh_native_read')
            if (after.get('busy') is not False
                    or after.get('screen_type') != 'ProducerRankingRecommendReplayScreenPresenter'
                    or type(state.get('attempt')) is not int or state['attempt'] != attempt
                    or state.get('selected_source') != target or rejected.get('selected_source') != target
                    or rejected.get('outcome') != 'official-prevalidation-rejected'
                    or any(rejected.get(key) is not True for key in required)
                    or rejected.get('visible_playback') is not False
                    or rejected.get('task_SetResult_observed') is not False
                    or rejected.get('toast_key') != 'produce.replay.log_invalid.toast'
                    or not isinstance(rejected.get('toast_callsite_rva'), str)
                    or _REJECTION_TOAST_CALLS.get(rejected['toast_callsite_rva']) != rejected.get('toast_return_rva')
                    or rejected.get('toast_return_rva') is None
                    or not _same_pointer(rejected.get('context_instance_id'), rejected.get('context_instance_id'))
                    or not _same_pointer(rejected.get('origin_page_instance_id'), target.get('presenter_instance_id'))
                    or not _same_pointer(rejected.get('origin_page_instance_id'), after.get('screen_instance_id'))):
                return None
            return {**receipt, 'completion': 'owned-official-prevalidation-rejection-observed',
                    'selected_source': dict(target), 'native_start_result': dict(rejected)}
        observed = _mapping(ui.get('direct_replay_observation'))
        identities = ('sequence_id', 'replay_context_instance_id', 'return_data_instance_id')
        if (after.get('screen_type') != 'ExamScreenPresenter' or state.get('phase') != 'replaying'
                or type(state.get('attempt')) is not int or state['attempt'] != attempt
                or state.get('selected_source') != target
                or observed.get('is_replay') is not True or observed.get('source_bound') is not True
                or not all(_identity(observed.get(key)) for key in identities)):
            return None
        # Starting is settled once the real owned playback exists. Its ongoing
        # animation is not an invitation to submit exam inputs or another start.
        return {**receipt, 'completion': 'owned-playback-observed', 'selected_source': dict(target),
                **{key: observed[key] for key in identities}}
    returned = _mapping(state.get('last_return'))
    selected = _mapping(target.get('selected_source'))
    if (after.get('busy') is not False
            or after.get('screen_type') != 'ProducerRankingRecommendReplayScreenPresenter'
            or state.get('phase') != 'ready' or type(state.get('attempt')) is not int
            or state['attempt'] != attempt + 1 or state.get('selected_source') != {}
            or selected.get('action_id') != 'replay.direct.start'
            or selected.get('owner_token') != target['owner_token']
            or selected.get('attempt') != attempt
            or any(selected.get(key) != target.get(key) for key in ('produce_id', 'idol_card_id'))
            or returned.get('selected_source') != selected
            or returned.get('replay_exit_observed') is not True
            or not _same_pointer(returned.get('returned_page_instance_id'), after.get('screen_instance_id'))):
        return None
    if target.get('archived_replay') is True:
        if not _archived_return_proof(ui, state, returned, target, selected):
            return None
        receipt.update(archived_replay=True, source_sha256=target['source_sha256'],
                       archive_input_sha256=target['archive_input_sha256'], archive_request_id=target['archive_request_id'],
                       membership_scope='archived-retained-source-and-normal-return; no page/cache membership claim')
    elif (returned.get('cached_item_membership_verified') is not True
          or selected.get('archived_replay') is True or returned.get('archived_scope') is True):
        return None
    return {**receipt, 'completion': 'owned-playback-return-observed',
            'selected_source': dict(target['selected_source']),
            'returned_page_instance_id': returned['returned_page_instance_id']}
