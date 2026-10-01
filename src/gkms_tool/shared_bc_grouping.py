"""Pure provenance grouping for shared BC; never assigns unknown people IDs."""
from collections import defaultdict
import hashlib
import json


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode('utf8')).hexdigest()


def prepare_groups(stages, quarantine_episode_ids):
    """Derive grouping constraints while preserving all historical assignments.

    Evidence resolution is separate. A public player hash is accepted here only
    with a verified witness marker; the file validator rechecks that witness.
    Unknown identities stay unknown and are never qualified heldout players.
    """
    if len({row['stage_id'] for row in stages}) != len(stages):
        raise ValueError('Duplicate stage identity')
    quarantine = set(quarantine_episode_ids)
    by_trajectory = defaultdict(list)
    for stage in stages:
        if not stage.get('whole_trajectory_id'):
            raise ValueError('Missing complete cultivation grouping identity')
        if stage.get('player_sha256') and not stage.get('player_witness_verified'):
            raise ValueError('Unproven player identity cannot enter grouping')
        by_trajectory[stage['whole_trajectory_id']].append(stage)
    trajectory_players = {}
    for trajectory, members in by_trajectory.items():
        players = {s['player_sha256'] for s in members if s.get('player_sha256')}
        if len(players) > 1:
            raise ValueError('One complete cultivation has conflicting original player identities')
        trajectory_players[trajectory] = next(iter(players), None)
    grouped = defaultdict(list)
    for trajectory, members in by_trajectory.items():
        player = trajectory_players[trajectory]
        key = ('public-player', player) if player else ('unknown-player-trajectory', trajectory)
        grouped[key].extend(members)
    groups = []
    for (kind, identity), members in sorted(grouped.items()):
        trajectories = sorted({s['whole_trajectory_id'] for s in members})
        original_splits = sorted({s['historical_split'] for s in members if s.get('historical_split')})
        roles = sorted({s['protected_role'] for s in members if s.get('protected_role')})
        quarantined = sorted(s['stage_id'] for s in members if s.get('original_episode_id') in quarantine)
        player_known = kind == 'public-player'
        protected_eval = any(role in ('evaluation_A', 'reserved_evaluation_C') for role in roles)
        role_conflict = protected_eval and ('training_B' in roles or 'train' in original_splits)
        group_id = 'shared-bc-provenance-group:' + digest({'kind': kind, 'identity': identity})
        groups.append({'group_id': group_id, 'group_kind': kind,
            'player_sha256': identity if player_known else None,
            'player_identity_known': player_known,
            'whole_trajectory_ids': trajectories,
            'stage_ids': sorted(s['stage_id'] for s in members),
            'stage_count': len(members), 'main_rows': sum(s['main_rows'] for s in members),
            'historical_splits': original_splits, 'protected_roles': roles,
            'historical_player_split_leakage_observed': player_known and len(original_splits) > 1,
            'historical_whole_trajectory_split_leakage': any(len({s['historical_split'] for s in by_trajectory[t] if s.get('historical_split')}) > 1 for t in trajectories),
            'protected_evaluation_training_conflict': role_conflict,
            'quarantined_stage_ids': quarantined,
            'fit_forbidden_by_protected_evaluation': protected_eval,
            'known_player_grouping_qualified': player_known and not role_conflict,
            'independent_player_holdout_qualified': False,
            'heldout_qualification_note': 'No new split assigned; unresolved players may overlap any known or unknown group.',
            'future_split_assignment': None,
            'new_training_admitted': False})
    unknown = [s for s in stages if not trajectory_players[s['whole_trajectory_id']]]
    unknown_splits = sorted({s['historical_split'] for s in unknown if s.get('historical_split')})
    return {'schema': 'gkms.shared-bc-provenance-grouping.v1', 'groups': groups,
        'stage_count': len(stages), 'main_row_count': sum(s['main_rows'] for s in stages),
        'whole_trajectory_count': len(by_trajectory),
        'known_public_players': sum(g['player_identity_known'] for g in groups),
        'unknown_player_trajectories': sum(not g['player_identity_known'] for g in groups),
        'unknown_player_stage_ids': sorted(s['stage_id'] for s in unknown),
        'unknown_player_overlap_risk': {'historical_splits': unknown_splits,
            'possible_cross_split_overlap_unresolved': len(unknown_splits) > 1,
            'may_overlap_known_players': bool(unknown),
            'same_player_claimed': False, 'different_players_claimed': False,
            'independent_heldout_use_allowed': False},
        'quarantine_episode_ids': sorted(quarantine),
        'quarantined_stage_ids': sorted(s['stage_id'] for s in stages if s.get('original_episode_id') in quarantine),
        'assembly_candidate_stage_ids': sorted(s['stage_id'] for s in stages if s.get('original_episode_id') not in quarantine),
        'assembly_candidate_is_not_training_admission': True,
        'original_splits_rewritten': False, 'new_split_assigned': False,
        'new_training_admitted': False, 'model_quality_or_population_independence_claimed': False}


def verify_group_manifest(manifest):
    """Check stored constraints against source-linked stages, not score quality."""
    if manifest.get('schema') != 'gkms.shared-bc-group-preparation.v1':
        raise ValueError('Unknown group preparation schema')
    actual = prepare_groups(manifest['stages'], manifest['quarantine_episode_ids'])
    if actual != manifest['grouping']:
        raise ValueError('Stored group constraints differ from source-linked records')
    for stage in manifest['stages']:
        if stage.get('protected_role') in ('evaluation_A', 'reserved_evaluation_C') and stage.get('fit_allowed') is not False:
            raise ValueError('Protected native evaluation cannot be used for fitting')
        if stage.get('original_episode_id') in manifest['quarantine_episode_ids'] and stage.get('quarantine_excluded') is not True:
            raise ValueError('Quarantined source was not excluded')
    return {'schema': 'gkms.shared-bc-group-structure-validation.v1', 'passed': True,
            'stages': len(manifest['stages']), 'groups': len(actual['groups']),
            'source_files_rechecked_by_this_pure_function': False,
            'new_training_admitted': False}


def assign_player_splits(stages, grouping):
    """Propose a new player-grouped assignment without rewriting old splits.

    Unknown people are blocked from this assembly. Old fit exposure remains
    train; old evaluation is reused as evaluation, never called a fresh test.
    A/C retain their protected evaluation roles and B retains training.
    """
    if prepare_groups(stages, grouping['quarantine_episode_ids']) != grouping:
        raise ValueError('Grouping constraints do not match supplied stages')
    by_id = {s['stage_id']: s for s in stages}
    assignments = []; stage_assignments = []
    for group in grouping['groups']:
        roles, old_splits = set(group['protected_roles']), set(group['historical_splits'])
        if not group['player_identity_known']:
            split, reason = 'blocked-unresolved-player', 'Original person identity not established; no assumed account or independent person'
        elif group['protected_evaluation_training_conflict']:
            split, reason = 'blocked-role-conflict', 'Protected evaluation overlaps historical fit exposure or B training'
        elif 'reserved_evaluation_C' in roles:
            split, reason = 'test', 'Preserve original C evaluation-only role'
        elif 'evaluation_A' in roles:
            split, reason = 'validation', 'Preserve original A evaluation-only role'
        elif 'training_B' in roles:
            split, reason = 'train', 'Preserve original B training role'
        elif 'train' in old_splits:
            split, reason = 'train', 'Keep every history of an originally fitted player out of heldout; original old split values retained'
        elif 'validation' in old_splits:
            split, reason = 'validation', 'Reuse old evaluated player group; not a fresh unseen control holdout'
        elif old_splits == {'test'}:
            split, reason = 'test', 'Reuse old evaluated player group; not a fresh unseen control holdout'
        else:
            split, reason = 'blocked-missing-role', 'No source-supported fitting/evaluation role'
        exposure = {'v8_five_flow_fit': 'train' in old_splits,
                    'v8_five_flow_evaluated': bool(old_splits.intersection(('validation', 'test'))),
                    'fullpower_B_prior_fit': 'training_B' in roles,
                    'fullpower_A_C_prior_control_evaluation': bool(roles.intersection(('evaluation_A', 'reserved_evaluation_C')))}
        row = {'group_id': group['group_id'], 'player_sha256': group['player_sha256'],
               'whole_trajectory_ids': group['whole_trajectory_ids'], 'stage_ids': group['stage_ids'],
               'new_split': split, 'reason': reason, 'historical_splits': group['historical_splits'],
               'protected_roles': group['protected_roles'], 'prior_control_exposure': exposure,
               'independent_unseen_to_prior_controls': False,
               'old_control_evaluation_is_not_new_holdout': any(exposure.values())}
        assignments.append(row)
        for stage_id in group['stage_ids']:
            stage = by_id[stage_id]
            effective = 'excluded-quarantine' if stage_id in grouping['quarantined_stage_ids'] else split
            stage_assignments.append({'stage_id': stage_id, 'group_id': group['group_id'],
                'player_sha256': group['player_sha256'], 'whole_trajectory_id': stage['whole_trajectory_id'],
                'source_trajectory_id': stage['source_trajectory_id'], 'original_episode_id': stage['original_episode_id'],
                'historical_split': stage['historical_split'], 'new_split': effective,
                'protected_role': stage['protected_role'], 'main_rows': stage['main_rows'],
                'step_type': stage['step_type'], 'effect_type': stage['identity']['mainEffectType'],
                'feature_admission_still_required': True, 'new_training_admitted': False})
    return {'schema': 'gkms.shared-bc-player-split-assignment.v1',
        'algorithm': 'protected A/C evaluation; B train; historical fit exposure stays train; old validation/test reused without cold-control claim; unknown identity blocked',
        'groups': assignments, 'stages': sorted(stage_assignments, key=lambda s: s['stage_id']),
        'original_split_fields_rewritten': False, 'assignment_applied_to_existing_dataset': False,
        'new_training_admitted': False, 'independent_unseen_control_holdout_claimed': False}


def verify_split_assignment(stages, grouping, assignment):
    if assign_player_splits(stages, grouping) != assignment:
        raise ValueError('New split assignment differs from source/role constraints')
    player_splits = defaultdict(set); trajectory_splits = defaultdict(set)
    for row in assignment['stages']:
        if row['new_split'].startswith(('blocked-', 'excluded-')): continue
        if not row['player_sha256']: raise ValueError('Unknown player entered a train/evaluation split')
        player_splits[row['player_sha256']].add(row['new_split'])
        trajectory_splits[row['whole_trajectory_id']].add(row['new_split'])
        if row['protected_role'] in ('evaluation_A', 'reserved_evaluation_C') and row['new_split'] == 'train':
            raise ValueError('Protected evaluation entered fitting')
    if any(len(s) != 1 for s in player_splits.values()) or any(len(s) != 1 for s in trajectory_splits.values()):
        raise ValueError('New assignment leaks a known player or complete cultivation across splits')
    return {'schema': 'gkms.shared-bc-player-split-validation.v1', 'passed': True,
        'known_player_disjoint_for_this_proposed_assignment': True,
        'complete_cultivation_groups_preserved': True,
        'unknown_players_not_assigned': True,
        'independent_unseen_to_prior_controls': False, 'new_training_admitted': False}
