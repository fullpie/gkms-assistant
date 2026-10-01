"""Explicit admission profiles for retained original-PC capture receipts.

Legacy readers keep their existing paired-capture requirements. A source-full
capture proves its own original terminal and never claims an events comparison.
These checks supplement each consumer's source/hash/cleanup binding checks.
"""
from collections.abc import Mapping
from pathlib import Path

PAIRED_EVENTS_FULL = 'paired-events-full-v1'
SOURCE_FULL = 'source-full-v1'
SHARED_PARENT_CLEANUP = 'shared-parent-cleanup'
OWNED_PROCESS_DISCARD = 'owned-process-discard'
CAPTURE_REQUIREMENTS = (
    'source_verified_against_original_export', 'source_spine_passed',
    'required_responses_covered', 'scoped_owned_capture_verified',
    'case_cleanup_passed', 'parent_membership_passed', 'parent_shared_cleanup_passed',
    'snapshot_hash_and_candidate_rng_verified',
)


def capture_qualification_requirements(report, legacy_requirements=()):
    """Return booleans to check alongside ``capture_profile_errors``.

    Unknown profiles are rejected by the profile check, never inferred from
    missing baseline files. Omitted profiles preserve historical paired rules.
    """
    additional = (('original_score_matches', 'source_full_terminal_verified')
        if report.get('capture_profile', PAIRED_EVENTS_FULL) == SOURCE_FULL else legacy_requirements)
    required = tuple('parent_discard_boundary_verified'
        if key == 'parent_shared_cleanup_passed' and report.get('parent_closure_kind') == OWNED_PROCESS_DISCARD
        else key for key in CAPTURE_REQUIREMENTS)
    return tuple(dict.fromkeys((*required, *additional)))


def capture_discard_evidence(observer):
    """References to watch after ``capture_profile_errors`` verified the seal.

    The proof is checked once at each case boundary, never for each training row.
    Consumers retain their existing final file-stamp checks while streaming.
    """
    cleanup = observer.get('native_cleanup', {}) if isinstance(observer, Mapping) else {}
    if cleanup.get('parent_closure_kind') != OWNED_PROCESS_DISCARD:
        return ()
    proof = cleanup.get('process_discard_boundary', {})
    evidence = proof.get('evidence', {}) if isinstance(proof, Mapping) else {}
    return tuple(evidence.values()) if isinstance(evidence, Mapping) else ()


def _parent_closure_errors(report, observer):
    kind = report.get('parent_closure_kind', SHARED_PARENT_CLEANUP)
    cleanup = observer.get('native_cleanup', {}) if isinstance(observer, Mapping) else {}
    if not isinstance(cleanup, Mapping):
        cleanup = {}
    if kind == SHARED_PARENT_CLEANUP:
        if (report.get('parent_discard_boundary_verified') is True
                or cleanup.get('parent_closure_kind', SHARED_PARENT_CLEANUP) != kind):
            return ['shared-parent-cleanup-claims-discard-boundary']
        return []
    if kind != OWNED_PROCESS_DISCARD:
        return ['unknown-parent-closure-kind']
    errors = []
    if report.get('capture_profile') != SOURCE_FULL:
        errors.append('discard-boundary-requires-source-full-profile')
    if report.get('parent_discard_boundary_verified') is not True:
        errors.append('parent-discard-boundary-not-verified')
    if report.get('parent_shared_cleanup_passed') is not False:
        errors.append('discard-boundary-must-not-claim-shared-cleanup')
    if (cleanup.get('parent_closure_kind') != kind or cleanup.get('passed') is not True
            or cleanup.get('errors') != []):
        errors.append('discard-observer-cleanup-proof-differs')
    shared = cleanup.get('shared_parent_provider_cleanup', {})
    if not isinstance(shared, Mapping) or shared.get('passed') is not False:
        errors.append('discard-observer-must-preserve-failed-shared-cleanup')
    for name in ('case_task_loop_observer', 'batch_parent_membership'):
        value = cleanup.get(name, {})
        if not isinstance(value, Mapping) or value.get('passed') is not True or value.get('errors') != []:
            errors.append('discard-' + name + '-not-proven')
    proof = cleanup.get('process_discard_boundary', {})
    if not isinstance(proof, Mapping) or proof.get('passed') is not True or proof.get('errors') != []:
        return [*errors, 'discard-observer-owner-proof-required']
    try:
        from .capture_parent_seal import verify_discarded_completed_prefix
        fresh = verify_discarded_completed_prefix(report['output'])
        if fresh.get('passed') is not True or fresh != proof:
            errors.append('discard-owner-source-evidence-changed-or-unproven')
        evidence = fresh.get('evidence', {})
        for name in ('case_receipt', 'inputs_receipt'):
            reference = report[name]
            actual = evidence.get(str(Path(reference['path']).resolve()))
            if (not isinstance(actual, Mapping) or actual.get('sha256') != reference.get('sha256')
                    or 'bytes' in reference and actual.get('bytes') != reference['bytes']):
                errors.append('discard-' + name + '-binding-differs')
        if (fresh.get('source_sha256') != report['source']['sha256']
                or fresh.get('episode_id') != report['episode_id']
                or Path(fresh['parent']).resolve() != Path(report['parent']).resolve()
                or observer.get('evidence', {}).get('source') != report['source']):
            errors.append('discard-original-source-identity-differs')
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        errors.append('discard-owner-source-evidence-invalid')
    return errors


def capture_profile_errors(report, observer=None):
    """Reject mixed profiles and unproven or invented source-full evidence."""
    profile = report.get('capture_profile', PAIRED_EVENTS_FULL)
    if profile not in (PAIRED_EVENTS_FULL, SOURCE_FULL):
        return ['unknown-capture-profile']
    errors = _parent_closure_errors(report, observer)
    if observer is not None and observer.get('capture_profile', PAIRED_EVENTS_FULL) != profile:
        errors.append('qualification-observer-capture-profile-differs')
    specification = report.get('specification', {})
    if isinstance(specification, Mapping) and specification.get('capture_profile', profile) != profile:
        errors.append('qualification-specification-capture-profile-differs')
    if profile == PAIRED_EVENTS_FULL:
        if report.get('source_full_terminal_verified') is True:
            errors.append('paired-capture-claims-source-full-profile')
        return errors
    if report.get('fresh_independent_events_baseline_required') is not False:
        errors.append('source-full-independent-events-baseline-claim')
    for key in ('terminal_rng_matches_events_baseline', 'terminal_payload_equivalence_to_events'):
        if key not in report or report[key] is not None:
            errors.append('source-full-' + key + '-must-be-null')
    if isinstance(specification, Mapping) and specification.get('previous_events_only_output') is not None:
        errors.append('source-full-specification-claims-events-baseline')
    if not isinstance(observer, Mapping):
        return [*errors, 'source-full-observer-proof-required']
    if 'terminal_comparison' not in observer or observer['terminal_comparison'] is not None:
        errors.append('source-full-terminal-comparison-must-be-null')
    evidence = observer.get('evidence', {})
    if not isinstance(evidence, Mapping) or 'baseline_execution' not in evidence or evidence['baseline_execution'] is not None:
        errors.append('source-full-baseline-execution-must-be-null')
    terminal = observer.get('source_terminal', {})
    if not isinstance(terminal, Mapping):
        terminal = {}
    for key in ('passed', 'snapshot_hash_verified', 'terminal_rng_present'):
        if terminal.get(key) is not True:
            errors.append('source-full-terminal-' + key + '-not-proven')
    if terminal.get('errors') != []:
        errors.append('source-full-terminal-errors')
    for key in ('terminal_facts', 'source_cursor'):
        section = terminal.get(key, {})
        if not isinstance(section, Mapping) or section.get('passed') is not True or section.get('errors') != []:
            errors.append('source-full-terminal-' + key + '-not-proven')
    quality = terminal.get('snapshot_quality', {})
    if not isinstance(quality, Mapping) or any(quality.get(key) is not True for key in (
            'complete_field_evidence', 'strict_pure_reported')):
        errors.append('source-full-terminal-snapshot-quality-not-proven')
    return errors
