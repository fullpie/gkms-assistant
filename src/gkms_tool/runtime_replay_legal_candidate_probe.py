"""Offline runner/validator for the runtime recorder legal-candidate probe.

The legal-candidate DLL is a separate, shadow-only native build.  This module
does not load that DLL, deploy it, launch a game, send controller input, or
consult the simulator.  It only waits on a retained recorder JSONL and checks
the candidate object copied onto each transition at the native
``AddExecuteCommand`` decision boundary.

The incremental file reader and Replay-stage selector are deliberately shared
with :mod:`gkms_tool.runtime_replay_watcher`; this module adds only the
candidate schema gate and a small hash manifest.  A candidate row is evidence
of observed hand identities, never a proof of legality.  Consequently the
authoritative ``legal_actions`` field remains ``null`` and all promotion
flags remain false even when every identity field is present.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import argparse
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Callable

from .runtime_validate_hand_card_abi import decode_raw_return

from .runtime_replay_watcher import (
    DEFAULT_CHUNK_BYTES,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    RECORDER_SCHEMA,
    IncrementalShadowReader,
    RuntimeReplayStageWatcher,
    RuntimeReplayWatcherError,
    default_shadow_path,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CANDIDATE_DLL = (
    PROJECT_ROOT
    / "var"
    / "native"
    / "runtime_exam_recorder_legal_probe"
    / "gkms_runtime_exam_recorder_legal_probe.dll"
)
DEFAULT_CANDIDATE_SOURCE = (
    PROJECT_ROOT / "native" / "runtime_exam_recorder" / "src" / "recorder.cpp"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT / "var" / "coverage" / "runtime_replay_legal_candidate_probe.json"
)
DEFAULT_MANIFEST_OUTPUT = (
    PROJECT_ROOT / "var" / "coverage" / "runtime_replay_legal_candidate_manifest.json"
)

SCHEMA = "gkms.runtime-replay-legal-candidate-probe.v1"
MANIFEST_SCHEMA = "gkms.runtime-replay-legal-candidate-manifest.v1"
CANDIDATE_SCHEMA = "gkms.runtime-exam-legal-action-candidates.shadow.v1"
PROBE_VERSION = "settled-hand-v1"
SETTLED_BOUNDARY = "ExamSequence.AddExecuteCommand(next-manual-before)"
RECORDER_BOUNDARY = "ExamSequence.AddExecuteCommand"
MAX_REPORTED_ISSUES = 256
MAX_REPORTED_STEPS = 256

# The first candidate probe deliberately remains shadow-only.  The next
# runtime probe can attach these records to the same transition envelope
# without introducing another reader/selector framework.  The strings are
# intentionally descriptive rather than tied to one native writer version;
# the validator accepts the v1 candidate schema plus an explicit probe payload.
RUNTIME_PROBE_VERSION = "main-action-families-v1"
RUNTIME_PROBE_SCHEMA = "gkms.runtime-exam-main-action-runtime-probe.v1"
FAMILY_NAMES = ("hand", "drink", "end_turn")
FAMILY_ALIASES = {
    "hand": "hand",
    "use-hand": "hand",
    "use_hand": "hand",
    "hand_validator": "hand",
    "hand-validator": "hand",
    "drink": "drink",
    "use-drink": "drink",
    "use_drink": "drink",
    "drink_validator": "drink",
    "drink-probe": "drink",
    "end_turn": "end_turn",
    "end-turn": "end_turn",
    "endturn": "end_turn",
    "turn-end": "end_turn",
    "turn_end": "end_turn",
    "end_turn_validator": "end_turn",
}


class RuntimeLegalCandidateProbeError(RuntimeReplayWatcherError):
    """Raised for invalid probe arguments, not for failed evidence checks."""


def _is_bool(value: object) -> bool:
    return type(value) is bool


def _is_int(value: object, *, minimum: int | None = None) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and (minimum is None or value >= minimum)
    )


def _is_text(value: object, *, non_empty: bool = True) -> bool:
    return isinstance(value, str) and (not non_empty or bool(value))


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _issue(
    issues: list[dict[str, object]],
    code: str,
    *,
    position: int | None = None,
    action_order: object | None = None,
    **details: object,
) -> None:
    if len(issues) >= MAX_REPORTED_ISSUES:
        return
    item: dict[str, object] = {"code": code}
    if position is not None:
        item["position"] = position
    if action_order is not None:
        item["action_order"] = action_order
    item.update(details)
    issues.append(item)


def _required(
    value: Mapping[str, object],
    fields: Sequence[str],
    issues: list[dict[str, object]],
    *,
    prefix: str,
    position: int,
    action_order: object,
) -> None:
    for field in fields:
        if field not in value:
            _issue(
                issues,
                "field-missing",
                position=position,
                action_order=action_order,
                field=f"{prefix}.{field}",
            )


def _check_value(
    issues: list[dict[str, object]],
    value: Mapping[str, object],
    field: str,
    predicate: Callable[[object], bool],
    *,
    position: int,
    action_order: object,
    code: str = "field-invalid",
) -> None:
    observed = value.get(field)
    if not predicate(observed):
        _issue(
            issues,
            code,
            position=position,
            action_order=action_order,
            field=field,
            observed=observed,
        )


def _check_exact(
    issues: list[dict[str, object]],
    value: Mapping[str, object],
    field: str,
    expected: object,
    *,
    position: int,
    action_order: object,
    code: str = "field-mismatch",
) -> None:
    if value.get(field) != expected:
        _issue(
            issues,
            code,
            position=position,
            action_order=action_order,
            field=field,
            expected=expected,
            observed=value.get(field),
        )


def _validate_integrity(
    integrity: object,
    *,
    issues: list[dict[str, object]],
    position: int,
    action_order: object,
    prefix: str,
    hand: bool,
) -> bool:
    value = _mapping(integrity)
    if value is None:
        _issue(
            issues,
            "integrity-not-object",
            position=position,
            action_order=action_order,
            field=prefix,
        )
        return False
    if hand:
        expected = {
            "slot_index_observed": True,
            "card_id_observed": True,
            "card_guid_observed": True,
            "upgrade_observed": True,
            "legality_predicate": "unverified",
        }
    else:
        expected = {"candidate_set_complete": False, "hand_legality": "unverified"}
    valid = True
    for field, wanted in expected.items():
        if value.get(field) != wanted:
            valid = False
            _issue(
                issues,
                "integrity-field-mismatch",
                position=position,
                action_order=action_order,
                field=f"{prefix}.{field}",
                expected=wanted,
                observed=value.get(field),
            )
    return valid


def _validate_hand_candidates(
    hand: object,
    *,
    issues: list[dict[str, object]],
    position: int,
    action_order: object,
) -> dict[str, object]:
    value = _mapping(hand)
    if value is None:
        _issue(
            issues,
            "hand-not-object",
            position=position,
            action_order=action_order,
            field="candidates.hand",
        )
        return {
            "present": False,
            "schema_valid": False,
            "count": 0,
            "identity_complete_count": 0,
            "legal_unknown_count": 0,
        }

    _required(
        value,
        (
            "status",
            "complete",
            "exact",
            "actions",
            "observed_candidates",
            "source",
            "identity_complete",
            "blockers",
        ),
        issues,
        prefix="candidates.hand",
        position=position,
        action_order=action_order,
    )
    for field, expected in (
        ("status", "blocked"),
        ("complete", False),
        ("exact", False),
        ("actions", None),
        ("source", "ExamSequence.get_HandList"),
        ("identity_complete", True),
    ):
        _check_exact(
            issues,
            value,
            field,
            expected,
            position=position,
            action_order=action_order,
            code="hand-field-mismatch",
        )
    _check_value(
        issues,
        value,
        "identity_complete",
        _is_bool,
        position=position,
        action_order=action_order,
        code="hand-field-invalid",
    )
    if not isinstance(value.get("blockers"), list):
        _issue(
            issues,
            "hand-blockers-not-array",
            position=position,
            action_order=action_order,
        )

    observed = value.get("observed_candidates")
    if not isinstance(observed, list):
        _issue(
            issues,
            "hand-observed-candidates-missing",
            position=position,
            action_order=action_order,
            observed=observed,
        )
        return {
            "present": False,
            "schema_valid": False,
            "count": 0,
            "identity_complete_count": 0,
            "legal_unknown_count": 0,
        }

    card_required = (
        "kind",
        "action_type",
        "slot_index",
        "card_index",
        "card_id",
        "card_guid",
        "guid",
        "upgrade",
        "legal",
        "source",
        "identity_complete",
        "integrity",
    )
    slots: list[int] = []
    complete_count = 0
    legal_unknown_count = 0
    for card_position, raw_card in enumerate(observed):
        card = _mapping(raw_card)
        prefix = f"candidates.hand.observed_candidates[{card_position}]"
        if card is None:
            _issue(
                issues,
                "hand-candidate-not-object",
                position=position,
                action_order=action_order,
                field=prefix,
            )
            continue
        _required(
            card,
            card_required,
            issues,
            prefix=prefix,
            position=position,
            action_order=action_order,
        )
        for field, expected in (
            ("kind", "use-hand"),
            ("action_type", "use-hand"),
            ("source", "ExamSequence.get_HandList"),
            ("legal", None),
        ):
            _check_exact(
                issues,
                card,
                field,
                expected,
                position=position,
                action_order=action_order,
                code="hand-candidate-field-mismatch",
            )
        for field in ("slot_index", "card_index"):
            _check_value(
                issues,
                card,
                field,
                lambda item: _is_int(item, minimum=0),
                position=position,
                action_order=action_order,
                code="hand-candidate-field-invalid",
            )
        _check_value(
            issues,
            card,
            "card_id",
            lambda item: _is_text(item),
            position=position,
            action_order=action_order,
            code="hand-identity-field-invalid",
        )
        _check_value(
            issues,
            card,
            "card_guid",
            lambda item: _is_text(item),
            position=position,
            action_order=action_order,
            code="hand-identity-field-invalid",
        )
        _check_value(
            issues,
            card,
            "guid",
            lambda item: _is_text(item),
            position=position,
            action_order=action_order,
            code="hand-identity-field-invalid",
        )
        _check_value(
            issues,
            card,
            "upgrade",
            lambda item: _is_int(item, minimum=0),
            position=position,
            action_order=action_order,
            code="hand-identity-field-invalid",
        )
        _check_value(
            issues,
            card,
            "identity_complete",
            _is_bool,
            position=position,
            action_order=action_order,
            code="hand-candidate-field-invalid",
        )
        _validate_integrity(
            card.get("integrity"),
            issues=issues,
            position=position,
            action_order=action_order,
            prefix=f"{prefix}.integrity",
            hand=True,
        )
        slot = card.get("slot_index")
        card_index = card.get("card_index")
        if _is_int(slot, minimum=0):
            slots.append(int(slot))
            if _is_int(card_index, minimum=0) and card_index != slot:
                _issue(
                    issues,
                    "hand-slot-card-index-mismatch",
                    position=position,
                    action_order=action_order,
                    expected=slot,
                    observed=card_index,
                )
        if _is_text(card.get("card_guid")) and card.get("guid") != card.get("card_guid"):
            _issue(
                issues,
                "hand-guid-alias-mismatch",
                position=position,
                action_order=action_order,
                card_guid=card.get("card_guid"),
                guid=card.get("guid"),
            )
        if card.get("identity_complete") is True:
            complete_count += 1
        else:
            _issue(
                issues,
                "hand-candidate-identity-incomplete",
                position=position,
                action_order=action_order,
                field=f"{prefix}.identity_complete",
            )
        if card.get("legal") is None:
            legal_unknown_count += 1

    if slots != list(range(len(observed))):
        _issue(
            issues,
            "hand-slot-order-invalid",
            position=position,
            action_order=action_order,
            expected=list(range(len(observed))),
            observed=slots,
        )
    return {
        "present": True,
        "schema_valid": not any(
            issue.get("position") == position
            and str(issue.get("code", "")).startswith(("hand-", "field-missing", "integrity-"))
            for issue in issues
        ),
        "count": len(observed),
        "identity_complete_count": complete_count,
        "legal_unknown_count": legal_unknown_count,
        "slot_indexes": slots,
    }


def _validate_candidate(
    candidate: object,
    *,
    issues: list[dict[str, object]],
    position: int,
    action_order: object,
) -> dict[str, object]:
    value = _mapping(candidate)
    if value is None:
        _issue(
            issues,
            "candidate-missing",
            position=position,
            action_order=action_order,
            field="legal_action_candidates",
        )
        return {
            "present": False,
            "schema_valid": False,
            "hand_observed_present": False,
            "hand_count": 0,
            "legal_unknown_count": 0,
            "purity_equal": False,
            "settled": False,
        }

    issue_start = len(issues)
    _required(
        value,
        (
            "schema",
            "decision_kind",
            "basis",
            "source",
            "shadow",
            "exact",
            "complete",
            "legal_actions_complete",
            "legal_actions",
            "authoritative_legal_actions",
            "phase",
            "is_command_playing",
            "command_stack_empty",
            "is_turn_card_play_end",
            "is_end_exam",
            "hand_list_known",
            "hand_identity_complete",
            "settled",
            "terminal",
            "enumeration_purity",
            "hand",
            "integrity",
            "blockers",
        ),
        issues,
        prefix="legal_action_candidates",
        position=position,
        action_order=action_order,
    )
    for field, expected in (
        ("schema", CANDIDATE_SCHEMA),
        ("decision_kind", "main"),
        ("basis", "same-exam-sequence-getter-only"),
        ("source", "same-ExamSequence-runtime"),
        ("shadow", True),
        ("exact", False),
        ("complete", False),
        ("legal_actions_complete", False),
        ("legal_actions", None),
        ("authoritative_legal_actions", None),
        ("phase", 6),
        ("is_command_playing", False),
        ("command_stack_empty", True),
        ("is_turn_card_play_end", False),
        ("is_end_exam", False),
        ("hand_list_known", True),
        ("hand_identity_complete", True),
        ("settled", True),
        ("terminal", False),
    ):
        _check_exact(
            issues,
            value,
            field,
            expected,
            position=position,
            action_order=action_order,
            code="candidate-field-mismatch",
        )
    if not isinstance(value.get("blockers"), list):
        _issue(
            issues,
            "candidate-blockers-not-array",
            position=position,
            action_order=action_order,
        )
    purity = _mapping(value.get("enumeration_purity"))
    purity_equal = False
    if purity is None:
        _issue(
            issues,
            "candidate-purity-not-object",
            position=position,
            action_order=action_order,
        )
    else:
        for field in ("before_captured", "after_captured", "compared"):
            _check_exact(
                issues,
                purity,
                field,
                True,
                position=position,
                action_order=action_order,
                code="candidate-purity-field-invalid",
            )
        purity_equal = purity.get("equal") is True
        if not purity_equal:
            _issue(
                issues,
                "candidate-purity-not-equal",
                position=position,
                action_order=action_order,
                observed=purity.get("equal"),
            )
        for field in ("before_bytes", "after_bytes"):
            _check_value(
                issues,
                purity,
                field,
                lambda item: _is_int(item, minimum=0),
                position=position,
                action_order=action_order,
                code="candidate-purity-size-invalid",
            )
    _validate_integrity(
        value.get("integrity"),
        issues=issues,
        position=position,
        action_order=action_order,
        prefix="legal_action_candidates.integrity",
        hand=False,
    )
    hand = _validate_hand_candidates(
        value.get("hand"),
        issues=issues,
        position=position,
        action_order=action_order,
    )
    return {
        "present": True,
        "schema_valid": (
            purity_equal
            and hand["schema_valid"] is True
            and not issues[issue_start:]
        ),
        "hand_observed_present": hand["present"],
        "hand_count": hand["count"],
        "identity_complete_count": hand["identity_complete_count"],
        "legal_unknown_count": hand["legal_unknown_count"],
        "purity_equal": purity_equal,
        "settled": value.get("settled") is True,
    }


# ---------------------------------------------------------------------------
# Current-PC runtime probe extension
# ---------------------------------------------------------------------------
#
# The original v1 candidate payload intentionally contains only observed
# identities.  A subsequent native probe can add a small, explicit runtime
# evidence object to the same transition.  The helpers below are deliberately
# tolerant about field spelling (the recorder and a hand-written fixture use
# slightly different names), but strict about the evidence needed to promote a
# family.  In particular, a projected candidate list by itself is never
# treated as a runtime result.


def _first_value(value: Mapping[str, object] | None, names: Sequence[str]) -> object:
    if value is None:
        return None
    for name in names:
        if name in value:
            return value.get(name)
    return None


def _as_bool(value: object) -> bool | None:
    return value if type(value) is bool else None


def _as_nonnegative_int(value: object) -> int | None:
    return int(value) if _is_int(value, minimum=0) else None


def _runtime_evidence_marker(value: Mapping[str, object] | None) -> bool:
    """Whether a payload explicitly identifies itself as real runtime data."""

    if value is None:
        return False
    for name in (
        "runtime_evidence",
        "real_runtime",
        "runtime_verified",
        "native_runtime",
        "observed_at_runtime",
        "actual_from_runtime",
    ):
        if value.get(name) is True:
            return True
    kind = _first_value(value, ("evidence_kind", "source_kind", "result_source"))
    if isinstance(kind, str) and kind.casefold() in {
        "runtime",
        "native-runtime",
        "current-pc-runtime",
        "live-runtime",
    }:
        return True
    source = value.get("source")
    if isinstance(source, str):
        source_lower = source.casefold()
        if "getter-only" in source_lower or "projection" in source_lower:
            return False
        return source_lower in {
            "runtime",
            "native-runtime",
            "current-pc-runtime",
            "live-runtime",
            "runtime-validator",
            "runtime-passive",
        } or any(
            token in source_lower
            for token in ("native-runtime", "current-pc-runtime", "live-runtime")
        )
    return False


def _looks_like_runtime_extension(value: Mapping[str, object] | None) -> bool:
    if value is None:
        return False
    extension_names = {
        "runtime_probe",
        "runtime_evidence",
        "hand_validator",
        "hand_validator_probe",
        "validator_probe",
        "validator",
        "validation",
        "two_pass",
        "pass_1",
        "pass_2",
        "pass1",
        "pass2",
        "actual_vs_projected",
        "actual_slots",
        "projected_slots",
        "button_actual",
        "button_projected",
        "drink_probe",
        "end_turn_probe",
        "endturn_probe",
        "families",
        "runtime_probe_records",
    }
    if extension_names.intersection(value.keys()):
        return True
    # Most native writers naturally place the richer probe directly under the
    # existing ``hand``/``drink``/``end_turn`` objects.  Recognize that shape
    # without classifying the ordinary v1 getter-only objects as enriched.
    for family in ("hand", "drink", "end_turn", "end-turn", "endturn"):
        nested = _mapping(value.get(family))
        if nested is not None and extension_names.intersection(nested.keys()):
            return True
    return False


def _candidate_runtime_payloads(
    body: Mapping[str, object] | None,
    candidate: Mapping[str, object] | None,
) -> list[Mapping[str, object]]:
    """Collect extension objects without interpreting ordinary v1 fields."""

    found: list[Mapping[str, object]] = []
    seen: set[int] = set()

    def add(value: object) -> None:
        mapping = _mapping(value)
        if mapping is None or id(mapping) in seen:
            return
        seen.add(id(mapping))
        if _looks_like_runtime_extension(mapping):
            found.append(mapping)
        for key in (
            "runtime_probe",
            "runtime_legal_action_probe",
            "runtime_evidence",
            "legal_action_probe",
            "passive_probe",
            "hand_validator",
            "hand_validator_probe",
            "drink_probe",
            "end_turn_probe",
            "endturn_probe",
            "families",
            "probes",
            "hand",
            "drink",
            "end_turn",
            "end-turn",
            "endturn",
            "runtime_probe_records",
        ):
            nested = mapping.get(key)
            if isinstance(nested, list):
                for item in nested:
                    if isinstance(item, Mapping):
                        add(item)
                continue
            nested_map = _mapping(nested)
            if nested_map is not None:
                add(nested_map)
                # A family map has one child per family; retain those children
                # as independent payloads too.
                for child in nested_map.values():
                    if isinstance(child, Mapping):
                        add(child)

    add(candidate)
    add(body)
    return found


def _family_payloads(
    body: Mapping[str, object] | None,
    candidate: Mapping[str, object] | None,
    family: str,
) -> list[Mapping[str, object]]:
    aliases = {
        "hand": (
            "hand",
            "use-hand",
            "use_hand",
            "hand_validator",
            "hand-validator",
            "hand_probe",
            "hand_validator_probe",
        ),
        "drink": (
            "drink",
            "use-drink",
            "use_drink",
            "drink_probe",
            "drink-passive",
            "drink_passive",
        ),
        "end_turn": (
            "end_turn",
            "end-turn",
            "endturn",
            "turn-end",
            "turn_end",
            "end_turn_probe",
            "endturn_probe",
            "end_turn_passive",
            "end_turn_validator",
        ),
    }[family]
    result: list[Mapping[str, object]] = []
    seen: set[int] = set()

    def add(value: object) -> None:
        mapping = _mapping(value)
        if mapping is None or id(mapping) in seen:
            return
        seen.add(id(mapping))
        # A family object may wrap its actual probe in ``probe`` or
        # ``actual_vs_projected``.  Keep both so the normalizer can choose the
        # most specific representation.
        result.append(mapping)
        for key in (
            "probe",
            "runtime_probe",
            "runtime_legal_action_probe",
            "runtime_evidence",
            "validator_probe",
            "validator",
            "validation",
            "actual_vs_projected",
            "comparison",
        ):
            nested = mapping.get(key)
            if isinstance(nested, Mapping):
                add(nested)

    def scan(mapping: Mapping[str, object] | None) -> None:
        if mapping is None:
            return
        for key in aliases:
            if key in mapping:
                add(mapping.get(key))
        for key in (
            "runtime_probe",
            "runtime_legal_action_probe",
            "runtime_evidence",
            "legal_action_probe",
            "passive_probe",
            "families",
            "probes",
            "runtime_probe_records",
        ):
            nested = _mapping(mapping.get(key))
            if isinstance(mapping.get(key), list):
                for item in mapping.get(key, []):
                    if isinstance(item, Mapping):
                        add(item)
                continue
            if nested is None:
                continue
            for key2 in aliases:
                if key2 in nested:
                    add(nested.get(key2))

    scan(candidate)
    scan(body)
    # Some probe writers put the family payload directly under a shared
    # ``families`` object but use canonical keys only.
    for payload in _candidate_runtime_payloads(body, candidate):
        scan(payload)
    return result


def _result_list(value: object) -> list[object]:
    if isinstance(value, list):
        return list(value)
    mapping = _mapping(value)
    if mapping is None:
        return []
    for key in ("results", "result", "slots", "per_slot", "by_slot", "actions"):
        nested = mapping.get(key)
        if isinstance(nested, list):
            return list(nested)
        if isinstance(nested, Mapping):
            pairs: list[tuple[int, object]] = []
            for raw_key, item in nested.items():
                try:
                    index = int(raw_key)
                except (TypeError, ValueError):
                    continue
                pairs.append((index, item))
            if pairs:
                return [
                    ({"slot_index": index, **item} if isinstance(item, Mapping) else
                     {"slot_index": index, "value": item})
                    for index, item in sorted(pairs)
                ]
    # A mapping of numeric slot keys is a compact JSONL representation.
    pairs = []
    for raw_key, item in mapping.items():
        try:
            index = int(raw_key)
        except (TypeError, ValueError):
            continue
        pairs.append((index, item))
    return [
        ({"slot_index": index, **item} if isinstance(item, Mapping) else
         {"slot_index": index, "value": item})
        for index, item in sorted(pairs)
    ]


def _raw_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < (1 << 64):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = int(text, 0)
        except ValueError:
            return None
        return parsed if 0 <= parsed < (1 << 64) else None
    return None


def _normalize_hand_result(
    raw: object,
    *,
    fallback_slot: int,
    position: int,
    action_order: object,
    issues: list[dict[str, object]],
    pass_name: str,
) -> dict[str, object]:
    value = _mapping(raw)
    slot = fallback_slot
    if value is not None:
        candidate_slot = _first_value(value, ("slot_index", "hand_index", "card_index", "index"))
        parsed_slot = _as_nonnegative_int(candidate_slot)
        if parsed_slot is not None:
            slot = parsed_slot
    result: dict[str, object] = {
        "slot_index": slot,
        "valid": None,
        "error": None,
        "error_name": None,
        "abi_shape_valid": None,
        "raw": None,
        "runtime_result": True,
    }
    if value is None:
        # Scalar bool rows are useful in tiny fixtures but are not enough to
        # establish the ValueTuple ABI; make them an explicit failure.
        _issue(
            issues,
            "hand-validator-result-not-object",
            position=position,
            action_order=action_order,
            pass_name=pass_name,
            slot_index=slot,
        )
        return result
    raw_value = _first_value(value, ("raw", "raw_rax", "return_raw", "rax", "return_value"))
    raw_int = _raw_int(raw_value)
    if raw_value is not None and raw_int is None:
        _issue(
            issues,
            "hand-validator-raw-invalid",
            position=position,
            action_order=action_order,
            pass_name=pass_name,
            slot_index=slot,
        )
    if raw_int is not None:
        try:
            decoded = decode_raw_return(raw_int)
        except ValueError:
            decoded = None
        if decoded is None:
            _issue(
                issues,
                "hand-validator-raw-decode-failed",
                position=position,
                action_order=action_order,
                pass_name=pass_name,
                slot_index=slot,
            )
        else:
            supplied_valid = _first_value(value, ("valid", "legal", "is_legal", "allowed"))
            supplied_error = _first_value(value, ("error", "error_code", "reason_code"))
            if type(supplied_valid) is bool and supplied_valid != decoded.valid:
                _issue(
                    issues,
                    "hand-validator-decoded-valid-mismatch",
                    position=position,
                    action_order=action_order,
                    pass_name=pass_name,
                    slot_index=slot,
                )
            if _is_int(supplied_error) and int(supplied_error) != decoded.error:
                _issue(
                    issues,
                    "hand-validator-decoded-error-mismatch",
                    position=position,
                    action_order=action_order,
                    pass_name=pass_name,
                    slot_index=slot,
                )
            result.update(
                {
                    "raw": raw_int,
                    "valid": decoded.valid,
                    "error": decoded.error,
                    "abi_shape_valid": decoded.abi_shape_valid,
                    "valid_byte": decoded.valid_byte,
                    "error_bits": decoded.error_bits,
                }
            )
            if not decoded.abi_shape_valid:
                _issue(
                    issues,
                    "hand-validator-abi-shape-invalid",
                    position=position,
                    action_order=action_order,
                    pass_name=pass_name,
                    slot_index=slot,
                )
    else:
        valid = _first_value(value, ("valid", "legal", "is_legal", "allowed"))
        error = _first_value(value, ("error", "error_code", "reason_code", "reason"))
        if type(valid) is bool:
            result["valid"] = valid
        else:
            _issue(
                issues,
                "hand-validator-valid-missing",
                position=position,
                action_order=action_order,
                pass_name=pass_name,
                slot_index=slot,
            )
        if _is_int(error):
            result["error"] = int(error)
            if int(error) < 0 or int(error) > 5:
                _issue(
                    issues,
                    "hand-validator-error-out-of-range",
                    position=position,
                    action_order=action_order,
                    pass_name=pass_name,
                    slot_index=slot,
                    observed=error,
                )
        else:
            _issue(
                issues,
                "hand-validator-error-missing",
                position=position,
                action_order=action_order,
                pass_name=pass_name,
                slot_index=slot,
            )
        shape = value.get("abi_shape_valid")
        if shape is not None:
            result["abi_shape_valid"] = shape is True
            if shape is not True:
                _issue(
                    issues,
                    "hand-validator-abi-shape-invalid",
                    position=position,
                    action_order=action_order,
                    pass_name=pass_name,
                    slot_index=slot,
                )
        else:
            # A decoded row must explicitly carry the shape assertion.  This
            # prevents a bool/error pair manufactured by an offline rule from
            # becoming current-PC authority.
            _issue(
                issues,
                "hand-validator-abi-shape-missing",
                position=position,
                action_order=action_order,
                pass_name=pass_name,
                slot_index=slot,
            )
    if _is_int(result.get("error"), minimum=0):
        try:
            from .runtime_validate_hand_card_abi import ERROR_VALUES

            result["error_name"] = ERROR_VALUES.get(int(result["error"]))
        except ImportError:  # pragma: no cover - defensive for partial installs
            result["error_name"] = None
    return result


def _pass_results(
    payloads: Sequence[Mapping[str, object]],
    *,
    pass_number: int,
    position: int,
    action_order: object,
    issues: list[dict[str, object]],
) -> tuple[list[dict[str, object]], Mapping[str, object] | None]:
    names = (
        ("pass_1", "pass1", "first_pass", "first", "results_pass_1", "pass_1_results", "pass1_results")
        if pass_number == 1
        else ("pass_2", "pass2", "second_pass", "second", "results_pass_2", "pass_2_results", "pass2_results")
    )
    for payload in payloads:
        candidate: object = None
        for name in names:
            if name in payload:
                candidate = payload.get(name)
                break
        if candidate is None:
            two_pass = _mapping(payload.get("two_pass"))
            if two_pass is not None:
                for name in names:
                    if name in two_pass:
                        candidate = two_pass.get(name)
                        break
        if candidate is None:
            # ``results`` may be paired with an explicit pass label.
            label = _first_value(payload, ("pass", "pass_number", "pass_index"))
            if label == pass_number or label == pass_number - 1:
                candidate = payload.get("results")
        values = _result_list(candidate)
        if not values:
            continue
        result = [
            _normalize_hand_result(
                raw,
                fallback_slot=index,
                position=position,
                action_order=action_order,
                issues=issues,
                pass_name=f"pass_{pass_number}",
            )
            for index, raw in enumerate(values)
        ]
        return result, _mapping(candidate)
    return [], None


def _purity_value(
    mapping: Mapping[str, object] | None,
    names: Sequence[str],
) -> object:
    return _first_value(mapping, names)


def _snapshot_field(snapshot: Mapping[str, object] | None, field: str) -> object:
    if snapshot is None:
        return None
    names: dict[str, tuple[str, ...]] = {
        "state": ("state_bytes", "state_json", "state", "state_sha256", "state_digest"),
        "rng": ("rng_state", "random_state", "random", "rng"),
        "phase": ("phase",),
        "busy": ("command_playing", "is_command_playing", "busy", "is_busy"),
        "stack": ("command_stack_empty", "stack_empty", "is_command_stack_empty"),
        "turn_end": ("turn_card_play_end", "is_turn_card_play_end", "turn_end"),
    }
    return _first_value(snapshot, names[field])


def _purity_for_payload(
    payloads: Sequence[Mapping[str, object]],
    *,
    position: int,
    action_order: object,
    issues: list[dict[str, object]],
) -> dict[str, object]:
    raw: Mapping[str, object] | None = None
    raw_score = -1
    for payload in payloads:
        for name in ("purity", "state_purity", "enumeration_purity", "guards"):
            candidate = _mapping(payload.get(name))
            if candidate is not None:
                # Prefer the full before/after guard over the legacy
                # getter-only ``enumeration_purity`` object when both are
                # present on an enriched transition.
                score = sum(
                    key in candidate
                    for key in (
                        "before",
                        "after",
                        "before_snapshot",
                        "after_snapshot",
                        "state_equal",
                        "rng_equal",
                        "phase_equal",
                        "busy_equal",
                        "stack_equal",
                        "turn_end_equal",
                        "context_created",
                        "dispose_attempted",
                        "dispose_succeeded",
                    )
                )
                if score > raw_score:
                    raw = candidate
                    raw_score = score
    if raw is None:
        _issue(
            issues,
            "runtime-purity-missing",
            position=position,
            action_order=action_order,
        )
        return {
            "before": {},
            "after": {},
            "state_equal": False,
            "rng_equal": False,
            "phase_equal": False,
            "busy_equal": False,
            "stack_equal": False,
            "turn_end_equal": False,
            "context_created": False,
            "dispose_attempted": False,
            "dispose_succeeded": False,
            "equal": False,
            "passed": False,
            "blockers": ["runtime-purity-missing"],
        }
    before = _mapping(_first_value(raw, ("before", "before_snapshot", "state_before")))
    after = _mapping(_first_value(raw, ("after", "after_snapshot", "state_after")))
    # Flat aliases are accepted only when capture markers are present.  This
    # keeps a bare ``state_equal=true`` claim from being mistaken for bytes.
    fields = ("state", "rng", "phase", "busy", "stack", "turn_end")
    flat_names = {
        "state": ("before_state", "before_state_bytes", "before_state_sha256", "before_state_digest"),
        "rng": ("before_rng", "before_rng_state", "rng_before"),
        "phase": ("before_phase", "phase_before"),
        "busy": ("before_busy", "before_command_playing", "command_playing_before"),
        "stack": ("before_stack", "before_command_stack_empty", "command_stack_empty_before"),
        "turn_end": ("before_turn_end", "before_turn_card_play_end", "turn_card_play_end_before"),
    }
    flat_after_names = {
        field: tuple(name.replace("before", "after") for name in names)
        for field, names in flat_names.items()
    }
    before_values: dict[str, object] = {}
    after_values: dict[str, object] = {}
    for field in fields:
        before_values[field] = _snapshot_field(before, field)
        after_values[field] = _snapshot_field(after, field)
        if before_values[field] is None:
            before_values[field] = _first_value(raw, flat_names[field])
        if after_values[field] is None:
            after_values[field] = _first_value(raw, flat_after_names[field])

    blockers: list[str] = []
    state_before_captured = before_values["state"] is not None or raw.get("before_captured") is True
    state_after_captured = after_values["state"] is not None or raw.get("after_captured") is True
    if not state_before_captured:
        blockers.append("validator-purity-before-snapshot-unavailable")
    if not state_after_captured:
        blockers.append("validator-purity-after-snapshot-unavailable")
    equal_names = {
        "state": "state_equal",
        "rng": "rng_equal",
        "phase": "phase_equal",
        "busy": "busy_equal",
        "stack": "stack_equal",
        "turn_end": "turn_end_equal",
    }
    equal_values: dict[str, bool] = {}
    for field in fields:
        explicit = _as_bool(raw.get(equal_names[field]))
        left, right = before_values[field], after_values[field]
        captured = left is not None and right is not None
        equal = explicit if explicit is not None else (captured and left == right)
        equal_values[field] = bool(equal)
        if not captured:
            blockers.append(f"validator-purity-{field.replace('_', '-')}-unavailable")
        elif not equal:
            blockers.append(f"validator-enumeration-mutated-{field.replace('_', '-')}")
    def lifecycle_value(names: Sequence[str]) -> bool:
        explicit = _as_bool(_first_value(raw, names))
        if explicit is not None:
            return explicit
        return _as_bool(_first_value(after, names)) is True

    context_created = lifecycle_value(("context_created", "resolver_created"))
    dispose_attempted = lifecycle_value(("dispose_attempted", "cleanup_attempted"))
    dispose_succeeded = lifecycle_value(("dispose_succeeded", "cleanup_succeeded"))
    if not context_created:
        blockers.append("validator-context-not-created")
    if not dispose_attempted:
        blockers.append("validator-context-dispose-not-attempted")
    elif not dispose_succeeded:
        blockers.append("validator-context-dispose-failed")
    explicit_equal = _as_bool(raw.get("equal"))
    if explicit_equal is False:
        blockers.append("runtime-purity-explicit-failure")
    passed = not blockers and (explicit_equal is not False)
    if not passed and len(issues) < MAX_REPORTED_ISSUES:
        for blocker in blockers:
            _issue(
                issues,
                blocker,
                position=position,
                action_order=action_order,
            )
    return {
        "before": before_values,
        "after": after_values,
        "state_equal": equal_values["state"],
        "rng_equal": equal_values["rng"],
        "phase_equal": equal_values["phase"],
        "busy_equal": equal_values["busy"],
        "stack_equal": equal_values["stack"],
        "turn_end_equal": equal_values["turn_end"],
        "context_created": context_created,
        "dispose_attempted": dispose_attempted,
        "dispose_succeeded": dispose_succeeded,
        "equal": passed,
        "passed": passed,
        "blockers": blockers,
    }


def _hand_observed_slots(candidate: Mapping[str, object] | None) -> list[Mapping[str, object]]:
    if candidate is None:
        return []
    hand = _mapping(candidate.get("hand"))
    if hand is None:
        return []
    raw = hand.get("observed_candidates")
    if not isinstance(raw, list):
        raw = hand.get("actions")
    return [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []


def _hand_family_result(
    payloads: Sequence[Mapping[str, object]],
    *,
    candidate: Mapping[str, object] | None,
    position: int,
    action_order: object,
    issues: list[dict[str, object]],
) -> dict[str, object]:
    pass_1, pass_1_source = _pass_results(
        payloads,
        pass_number=1,
        position=position,
        action_order=action_order,
        issues=issues,
    )
    pass_2, pass_2_source = _pass_results(
        payloads,
        pass_number=2,
        position=position,
        action_order=action_order,
        issues=issues,
    )
    runtime = any(_runtime_evidence_marker(payload) for payload in payloads)
    purity = _purity_for_payload(
        payloads,
        position=position,
        action_order=action_order,
        issues=issues,
    )
    blockers: list[str] = []
    if not runtime:
        blockers.append("hand-validator-runtime-evidence-missing")
    if not pass_1:
        blockers.append("hand-validator-pass-1-missing")
    if not pass_2:
        blockers.append("hand-validator-pass-2-missing")
    observed = _hand_observed_slots(candidate)
    observed_slots = [
        int(item.get("slot_index"))
        for item in observed
        if _is_int(item.get("slot_index"), minimum=0)
    ]
    slots_1 = [int(item["slot_index"]) for item in pass_1]
    slots_2 = [int(item["slot_index"]) for item in pass_2]
    if slots_1 != slots_2:
        blockers.append("hand-validator-pass-slot-order-mismatch")
    if pass_1 and slots_1 != list(range(len(pass_1))):
        blockers.append("hand-validator-pass-slot-order-invalid")
    if observed and observed_slots != slots_1:
        blockers.append("hand-validator-hand-slot-order-mismatch")
    if not observed:
        blockers.append("hand-validator-hand-identity-missing")
    agreement: list[dict[str, object]] = []
    if len(pass_1) == len(pass_2):
        for first, second in zip(pass_1, pass_2):
            equal = (
                first.get("slot_index") == second.get("slot_index")
                and first.get("valid") == second.get("valid")
                and first.get("error") == second.get("error")
                and first.get("abi_shape_valid") is True
                and second.get("abi_shape_valid") is True
            )
            agreement.append(
                {
                    "slot_index": first.get("slot_index"),
                    "pass_1": first,
                    "pass_2": second,
                    "equal": equal,
                }
            )
            if not equal:
                blockers.append("hand-validator-two-pass-result-mismatch")
    else:
        blockers.append("hand-validator-two-pass-count-mismatch")
    if not purity.get("passed"):
        blockers.extend(
            blocker for blocker in purity.get("blockers", [])
            if isinstance(blocker, str) and blocker not in blockers
        )
    # A pass source with ``runtime_evidence`` nested one level down is still
    # valid; retain it in the report for auditability.
    pass_1_source_copy = pass_1_source if isinstance(pass_1_source, Mapping) else None
    pass_2_source_copy = pass_2_source if isinstance(pass_2_source, Mapping) else None
    verified = bool(runtime and pass_1 and pass_2 and not blockers)
    return {
        "family": "hand",
        "runtime_evidence": runtime,
        "family_verified": verified,
        "promotion_eligible": verified,
        "verified": verified,
        "pass_1": pass_1,
        "pass_2": pass_2,
        "pass_1_source": pass_1_source_copy,
        "pass_2_source": pass_2_source_copy,
        "two_pass": {
            "pass_1_count": len(pass_1),
            "pass_2_count": len(pass_2),
            "same_slot_order": slots_1 == slots_2,
            "per_slot": agreement,
            "agreement": bool(agreement) and all(item["equal"] for item in agreement),
        },
        "ordered_slots": slots_1,
        "observed_slots": observed_slots,
        "observed_candidates": [dict(item) for item in observed],
        "purity": purity,
        "blockers": blockers,
    }


def _bool_from_row(value: Mapping[str, object] | None, *, projected: bool) -> bool | None:
    if value is None:
        return None
    names = (
        ("projected", "projected_legal", "projected_can_use", "expected", "eligible", "can_use", "allowed", "legal", "value")
        if projected
        else ("actual", "actual_can_use", "can_use_actual", "can_use", "enabled", "button_enabled", "allowed", "legal", "value")
    )
    raw = _first_value(value, names)
    if type(raw) is bool:
        return raw
    disabled = _first_value(value, ("disabled", "button_disabled", "is_disabled"))
    if type(disabled) is bool:
        return not disabled
    return None


def _slot_rows(value: object, *, slot_count: int | None = None) -> list[Mapping[str, object]]:
    rows = _result_list(value)
    result: list[Mapping[str, object]] = []
    for index, raw in enumerate(rows):
        if isinstance(raw, Mapping):
            if "slot_index" not in raw and "index" not in raw:
                result.append({"slot_index": index, **raw})
            else:
                result.append(raw)
        elif type(raw) is bool:
            result.append({"slot_index": index, "value": raw})
    if slot_count is not None and not result and slot_count == 1 and type(value) is bool:
        return [{"slot_index": 0, "value": value}]
    return result


def _passive_family_result(
    family: str,
    payloads: Sequence[Mapping[str, object]],
    *,
    position: int,
    action_order: object,
    issues: list[dict[str, object]],
) -> dict[str, object]:
    runtime = any(_runtime_evidence_marker(payload) for payload in payloads)
    raw_compare: Mapping[str, object] | None = None
    actual_raw: object = None
    projected_raw: object = None
    slot_count: int | None = None
    slot_count_explicit = False
    for payload in payloads:
        compare = _mapping(
            _first_value(payload, ("actual_vs_projected", "comparison", "compare"))
        )
        if compare is not None:
            raw_compare = compare
            compare_count = compare.get("slot_count")
            if _is_int(compare_count, minimum=0):
                slot_count = int(compare_count)
                slot_count_explicit = True
            actual_raw = _first_value(compare, ("actual", "runtime_actual", "observed", "actual_slots"))
            projected_raw = _first_value(compare, ("projected", "projection", "expected", "projected_slots"))
            # Verified native rows use the compact shape
            # ``{slot_count, slots:[{actual,projected,equal}]}`` rather than
            # repeating separate actual/projected arrays.  Expand it into the
            # same internal slot rows while retaining the original comparison
            # metadata in the output.
            direct_slots = compare.get("slots")
            if (
                actual_raw is None
                and projected_raw is None
                and isinstance(direct_slots, list)
            ):
                actual_raw = [
                    {
                        "slot_index": item.get("slot_index", index),
                        "actual": item.get("actual"),
                        **{
                            key: item.get(key)
                            for key in ("drink_id", "projected_drink_id", "action_id", "source")
                            if key in item
                        },
                    }
                    for index, item in enumerate(direct_slots)
                    if isinstance(item, Mapping)
                ]
                projected_raw = [
                    {
                        "slot_index": item.get("slot_index", index),
                        "projected": item.get("projected"),
                        "drink_id": item.get(
                            "projected_drink_id", item.get("drink_id")
                        ),
                        **{
                            key: item.get(key)
                            for key in ("action_id", "source")
                            if key in item
                        },
                    }
                    for index, item in enumerate(direct_slots)
                    if isinstance(item, Mapping)
                ]
        if actual_raw is None:
            actual_raw = _first_value(
                payload,
                ("actual", "runtime_actual", "observed", "actual_slots", "actual_results", "button_actual"),
            )
        if projected_raw is None:
            projected_raw = _first_value(
                payload,
                ("projected", "projection", "expected", "projected_slots", "projected_results", "button_projected"),
            )
        candidate_count = _first_value(payload, ("slot_count", "slots_count", "total_slots", "count"))
        if _is_int(candidate_count, minimum=0):
            slot_count = int(candidate_count)
            slot_count_explicit = True
        if actual_raw is not None and projected_raw is not None:
            break
    blockers: list[str] = []
    if not runtime:
        blockers.append(f"{family}-runtime-evidence-missing")
    if actual_raw is None:
        blockers.append(f"{family}-actual-results-missing")
    if projected_raw is None:
        blockers.append(f"{family}-projected-results-missing")
    if family == "end_turn" and actual_raw is not None and not isinstance(actual_raw, list):
        actual_rows = _slot_rows(actual_raw, slot_count=1)
    else:
        actual_rows = _slot_rows(actual_raw, slot_count=slot_count)
    if family == "end_turn" and projected_raw is not None and not isinstance(projected_raw, list):
        projected_rows = _slot_rows(projected_raw, slot_count=1)
    else:
        projected_rows = _slot_rows(projected_raw, slot_count=slot_count)
    if slot_count is None:
        if actual_rows and projected_rows:
            slot_count = max(len(actual_rows), len(projected_rows))
        else:
            slot_count = 0
    actual_by_slot = {
        int(row.get("slot_index")): row
        for row in actual_rows
        if _is_int(row.get("slot_index"), minimum=0)
    }
    projected_by_slot = {
        int(row.get("slot_index")): row
        for row in projected_rows
        if _is_int(row.get("slot_index"), minimum=0)
    }
    expected_slots = list(range(slot_count))
    if sorted(actual_by_slot) != expected_slots:
        blockers.append(f"{family}-actual-slot-coverage-incomplete")
    if sorted(projected_by_slot) != expected_slots:
        blockers.append(f"{family}-projected-slot-coverage-incomplete")
    slots: list[dict[str, object]] = []
    for slot in expected_slots:
        actual = _bool_from_row(actual_by_slot.get(slot), projected=False)
        projected = _bool_from_row(projected_by_slot.get(slot), projected=True)
        equal = actual is not None and projected is not None and actual == projected
        if actual is None:
            blockers.append(f"{family}-actual-slot-value-missing")
        if projected is None:
            blockers.append(f"{family}-projected-slot-value-missing")
        if actual is not None and projected is not None and actual != projected:
            blockers.append(f"{family}-actual-projected-mismatch")
        row: dict[str, object] = {
            "slot_index": slot,
            "actual": actual,
            "projected": projected,
            "equal": equal,
        }
        actual_row = actual_by_slot.get(slot)
        projected_row = projected_by_slot.get(slot)
        if actual_row is not None:
            for key in ("drink_id", "action_id", "button", "source"):
                if key in actual_row:
                    row[f"actual_{key}"] = actual_row.get(key)
        if projected_row is not None:
            for key in ("drink_id", "projected_drink_id", "action_id", "source"):
                if key in projected_row:
                    output_key = (
                        "projected_drink_id"
                        if key == "projected_drink_id"
                        else f"projected_{key}"
                    )
                    row[output_key] = projected_row.get(key)
        slots.append(row)
    # For END_TURN a scalar comparison is intentionally represented as slot 0;
    # this keeps the report shape identical to the drink per-slot table while
    # preserving the human-friendly aliases below.
    verified = bool(
        runtime
        and slot_count >= 0
        and (slot_count > 0 or slot_count_explicit)
        and not blockers
        and (
            all(row["equal"] is True for row in slots)
            if slot_count == 0 and slot_count_explicit
            else all(row["equal"] is True for row in slots)
        )
    )
    comparison = {
        "family": family,
        "slot_count": slot_count,
        "slots": slots,
        "all_equal": (
            all(row["equal"] is True for row in slots)
            if slot_count == 0 and slot_count_explicit
            else bool(slots) and all(row["equal"] is True for row in slots)
        ),
        "actual_coverage": len(actual_by_slot) == slot_count,
        "projected_coverage": len(projected_by_slot) == slot_count,
    }
    return {
        "family": family,
        "runtime_evidence": runtime,
        "family_verified": verified,
        "promotion_eligible": verified,
        "verified": verified,
        "actual_vs_projected": comparison,
        "per_slot": slots,
        "actual": [row.get("actual") for row in slots],
        "projected": [row.get("projected") for row in slots],
        "blockers": blockers,
    }


def _settlement_result(
    body: Mapping[str, object] | None,
    candidate: Mapping[str, object] | None,
    *,
    position: int,
    action_order: object,
    issues: list[dict[str, object]],
) -> dict[str, object]:
    values: dict[str, object] = {}
    if candidate is not None:
        values.update(
            {
                "phase": candidate.get("phase"),
                "busy": candidate.get("is_command_playing"),
                "stack_empty": candidate.get("command_stack_empty"),
                "turn_end": candidate.get("is_turn_card_play_end"),
                "settled": candidate.get("settled"),
                "terminal": candidate.get("terminal"),
            }
        )
    # An enriched runtime payload may carry the copied settlement scalars.
    for payload in _candidate_runtime_payloads(body, candidate):
        settlement = _mapping(_first_value(payload, ("settlement", "guards", "boundary")))
        source = settlement or payload
        aliases = {
            "phase": ("phase",),
            "busy": ("busy", "is_command_playing", "command_playing"),
            "stack_empty": ("stack_empty", "command_stack_empty", "is_command_stack_empty"),
            "turn_end": ("turn_end", "turn_card_play_end", "is_turn_card_play_end"),
            "settled": ("settled", "boundary_settled"),
            "terminal": ("terminal", "is_terminal"),
        }
        for key, names in aliases.items():
            value = _first_value(source, names)
            if value is not None:
                values[key] = value
    blockers: list[str] = []
    checks = {
        "phase_main": values.get("phase") == 6,
        "not_busy": values.get("busy") is False,
        "stack_empty": values.get("stack_empty") is True,
        "not_turn_card_play_end": values.get("turn_end") is False,
        "settled": values.get("settled") is True,
        "non_terminal": values.get("terminal") is False,
    }
    for key, passed in checks.items():
        if not passed:
            blockers.append(f"settlement-{key}-failed")
    passed = not blockers
    if not passed and len(issues) < MAX_REPORTED_ISSUES:
        for blocker in blockers:
            _issue(issues, blocker, position=position, action_order=action_order)
    return {
        "phase": values.get("phase"),
        "busy": values.get("busy"),
        "stack_empty": values.get("stack_empty"),
        "turn_end": values.get("turn_end"),
        "settled": values.get("settled"),
        "terminal": values.get("terminal"),
        "checks": checks,
        "passed": passed,
        "blockers": blockers,
    }


def _official_membership(
    body: Mapping[str, object] | None,
    *,
    hand: Mapping[str, object],
    drink: Mapping[str, object],
    end_turn: Mapping[str, object],
    position: int,
    action_order: object,
    issues: list[dict[str, object]],
) -> dict[str, object]:
    supplied_membership = _mapping(body.get("official_chosen_membership")) if body is not None else None
    action = _mapping(body.get("action")) if body is not None else None
    if supplied_membership is not None:
        nested_action = _mapping(supplied_membership.get("action"))
        if nested_action is not None:
            action = nested_action
    if action is None:
        result = {
            "known": False,
            "action_type": None,
            "slot_index": None,
            "member": False,
            "observed": False,
            "reason": "official-action-not-object",
        }
        if supplied_membership is not None:
            result["probe"] = dict(supplied_membership)
        return result
    action_type = _first_value(action, ("action_type", "type"))
    if not isinstance(action_type, str):
        play_type = action.get("play_type")
        action_type = {2: "use-hand", 3: "use-drink", 12: "turn-end"}.get(play_type)
    action_type = FAMILY_ALIASES.get(str(action_type).casefold(), str(action_type))
    canonical_type = {
        "hand": "use-hand",
        "drink": "use-drink",
        "end_turn": "turn-end",
    }.get(action_type, action_type)
    raw_slot = _first_value(action, ("slot_index", "play_index", "playIndex", "index"))
    slot = _as_nonnegative_int(raw_slot)
    # END_TURN's native command uses index zero by contract.
    if canonical_type == "turn-end" and slot is None:
        slot = 0
    family = action_type if action_type in FAMILY_NAMES else None
    member = False
    if family == "hand":
        member = slot is not None and slot in {
            int(item.get("slot_index"))
            for item in hand.get("pass_1", [])
            if item.get("valid") is True and _is_int(item.get("slot_index"), minimum=0)
        }
    elif family == "drink":
        member = slot is not None and any(
            row.get("slot_index") == slot and row.get("projected") is True
            for row in drink.get("per_slot", [])
        )
    elif family == "end_turn":
        member = any(
            row.get("slot_index") == 0 and row.get("projected") is True
            for row in end_turn.get("per_slot", [])
        )
    # A replay caller may be privileged and choose an action outside the Live
    # policy projection.  Preserve that policy mismatch in the step output,
    # but do not let it rewrite or invalidate the independently verified legal
    # set.
    result = {
        "known": action.get("known") is True,
        "action_type": canonical_type,
        "family": family,
        "slot_index": slot,
        "member": member,
        "observed": member,
        "reason": "member" if member else "not-in-runtime-projected-set",
        "raw": dict(action),
    }
    if supplied_membership is not None:
        supplied_member = supplied_membership.get("member")
        supplied_complete = supplied_membership.get("probe_complete")
        result["probe_member"] = supplied_member
        result["probe_complete"] = supplied_complete
        result["probe_reason"] = supplied_membership.get("reason")
        if type(supplied_member) is bool and supplied_member != member:
            _issue(
                issues,
                "official-membership-mismatch",
                position=position,
                action_order=action_order,
                expected=member,
                observed=supplied_member,
            )
        if supplied_complete is not True:
            _issue(
                issues,
                "official-membership-probe-incomplete",
                position=position,
                action_order=action_order,
            )
    return result


def _authoritative_actions(
    hand: Mapping[str, object],
    drink: Mapping[str, object],
    end_turn: Mapping[str, object],
) -> list[dict[str, object]]:
    actions: list[dict[str, object]] = []
    observed_by_slot = {
        int(item.get("slot_index")): item
        for item in hand.get("observed_candidates", [])
        if _is_int(item.get("slot_index"), minimum=0)
    }
    for result in hand.get("pass_1", []):
        if result.get("valid") is not True or not _is_int(result.get("slot_index"), minimum=0):
            continue
        slot = int(result["slot_index"])
        action: dict[str, object] = {
            "action_type": "use-hand",
            "indexes": [slot],
            "slot_index": slot,
            "validator_error": result.get("error"),
        }
        observed = observed_by_slot.get(slot)
        if observed is not None:
            for key in ("card_id", "card_guid", "guid", "upgrade"):
                if key in observed:
                    action[key] = observed.get(key)
        actions.append(action)
    for result in drink.get("per_slot", []):
        if result.get("projected") is not True or not _is_int(result.get("slot_index"), minimum=0):
            continue
        slot = int(result["slot_index"])
        action = {"action_type": "use-drink", "indexes": [slot], "slot_index": slot}
        if result.get("projected_drink_id") is not None:
            action["drink_id"] = result.get("projected_drink_id")
        actions.append(action)
    if any(row.get("projected") is True for row in end_turn.get("per_slot", [])):
        actions.append(
            {
                "action_type": "turn-end",
                "indexes": [0],
                "slot_index": 0,
                "action_id": "END_TURN",
            }
        )
    return actions


def _action_identity(value: object) -> tuple[object, ...] | None:
    """Return the authority-relevant identity of one action row."""

    action = _mapping(value)
    if action is None:
        return None
    raw_type = _first_value(action, ("action_type", "kind", "type"))
    if not isinstance(raw_type, str):
        raw_type = {2: "use-hand", 3: "use-drink", 12: "turn-end"}.get(
            action.get("play_type")
        )
    family = FAMILY_ALIASES.get(str(raw_type).casefold(), str(raw_type))
    canonical_type = {
        "hand": "use-hand",
        "drink": "use-drink",
        "end_turn": "turn-end",
    }.get(family, family)
    slot = _first_value(action, ("slot_index", "play_index", "playIndex", "index"))
    if slot is None:
        indexes = action.get("indexes")
        if isinstance(indexes, list) and indexes:
            slot = indexes[0]
    if not _is_int(slot, minimum=0):
        return None
    if canonical_type == "use-hand":
        return (
            canonical_type,
            int(slot),
            action.get("card_guid", action.get("guid")),
            action.get("card_id", action.get("id")),
            action.get("upgrade"),
        )
    if canonical_type == "use-drink":
        return (canonical_type, int(slot), action.get("drink_id", action.get("id")))
    if canonical_type == "turn-end":
        return (canonical_type, int(slot), action.get("action_id", "END_TURN"))
    return (str(canonical_type), int(slot))


def _runtime_authoritative_actions(
    payloads: Sequence[Mapping[str, object]],
) -> list[Mapping[str, object]] | None:
    ordered_payloads = sorted(
        payloads,
        key=lambda payload: 0
        if payload.get("schema") == RUNTIME_PROBE_SCHEMA
        else 1,
    )
    for payload in ordered_payloads:
        for key in (
            "authoritative_legal_actions",
            "authoritative_actions",
            "legal_actions",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
            nested = _mapping(value)
            if nested is not None and isinstance(nested.get("actions"), list):
                return [item for item in nested["actions"] if isinstance(item, Mapping)]
    return None


def _body_authoritative_actions(
    body: Mapping[str, object] | None,
) -> list[Mapping[str, object]] | None:
    if body is None:
        return None
    legal = _mapping(body.get("legal_actions"))
    if legal is None or not isinstance(legal.get("actions"), list):
        return None
    return [item for item in legal["actions"] if isinstance(item, Mapping)]


def _action_set_consistency(
    body: Mapping[str, object] | None,
    payloads: Sequence[Mapping[str, object]],
    computed: Sequence[Mapping[str, object]],
    *,
    position: int,
    action_order: object,
    issues: list[dict[str, object]],
) -> dict[str, object]:
    body_actions = _body_authoritative_actions(body)
    runtime_actions = _runtime_authoritative_actions(payloads)
    blockers: list[str] = []
    if body_actions is None:
        blockers.append("body-legal-actions-missing")
    if runtime_actions is None:
        blockers.append("runtime-authoritative-legal-actions-missing")
    body_ids = [_action_identity(item) for item in body_actions or []]
    runtime_ids = [_action_identity(item) for item in runtime_actions or []]
    computed_ids = [_action_identity(item) for item in computed]
    if any(item is None for item in body_ids):
        blockers.append("body-legal-actions-identity-invalid")
    if any(item is None for item in runtime_ids):
        blockers.append("runtime-authoritative-identity-invalid")
    if body_actions is not None and any(item.get("legal") is False for item in body_actions):
        blockers.append("body-legal-actions-contains-illegal-row")
    if body_ids != runtime_ids:
        blockers.append("body-runtime-authoritative-actions-mismatch")
    if body_ids != computed_ids:
        blockers.append("body-computed-authoritative-actions-mismatch")
    if blockers:
        for blocker in blockers:
            _issue(
                issues,
                blocker,
                position=position,
                action_order=action_order,
            )
    return {
        "body_actions": [dict(item) for item in body_actions or []],
        "runtime_actions": [dict(item) for item in runtime_actions or []],
        "computed_actions": [dict(item) for item in computed],
        "body_ids": body_ids,
        "runtime_ids": runtime_ids,
        "computed_ids": computed_ids,
        "body_runtime_equal": body_ids == runtime_ids and body_actions is not None and runtime_actions is not None,
        "body_computed_equal": body_ids == computed_ids and body_actions is not None,
        "consistent": not blockers,
        "blockers": blockers,
    }


def validate_runtime_replay_main_action_stage(
    transitions: Sequence[Mapping[str, object]],
    *,
    base_report: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Validate the next hand-validator + passive runtime probe slice.

    This is an extension of :func:`validate_runtime_replay_legal_candidate_stage`.
    It consumes only copied JSONL evidence.  A family is marked verified only
    when its payload explicitly identifies real runtime evidence and every
    required row/guard passes.  The returned authoritative action arrays are
    produced only when all three families and settlement pass for every step.
    """

    issues: list[dict[str, object]] = []
    steps: list[dict[str, object]] = []
    orders: list[object] = []
    structural_pass = True
    terminal_positions: list[int] = []
    turn1_positions: list[int] = []
    candidate_presence_count = 0
    hand_observed_presence_count = 0
    for position, row in enumerate(transitions):
        step_structural_pass = True
        body = _mapping(row.get("body"))
        envelope_issue_start = len(issues)
        action_order = _validate_transition_envelope(
            row,
            body,
            issues=issues,
            position=position,
        )
        if len(issues) != envelope_issue_start:
            structural_pass = False
            step_structural_pass = False
        orders.append(action_order)
        candidate = _mapping(body.get("legal_action_candidates")) if body is not None else None
        if candidate is None:
            structural_pass = False
            step_structural_pass = False
        else:
            candidate_presence_count += 1
            hand_value = _mapping(candidate.get("hand"))
            if hand_value is not None and isinstance(hand_value.get("observed_candidates"), list):
                hand_observed_presence_count += 1
        if body is not None:
            before = _mapping(body.get("state_before"))
            if before is not None and before.get("currentTurn") == 1:
                turn1_positions.append(position)
        payloads = _candidate_runtime_payloads(body, candidate)
        enriched = bool(payloads) and any(_looks_like_runtime_extension(item) for item in payloads)
        # Keep v1 identity/schema checks as a structural guard when a future
        # payload still carries the old observed hand object.  For an enriched
        # hand writer, the strict v1 ``status=blocked`` assertion would reject
        # the very evidence this validator is meant to consume, so only apply
        # it when no runtime extension is present.
        if not enriched:
            old = _validate_candidate(
                candidate,
                issues=issues,
                position=position,
                action_order=action_order,
            )
            structural_pass = structural_pass and bool(old.get("schema_valid"))
            step_structural_pass = step_structural_pass and bool(old.get("schema_valid"))
            hand_result: dict[str, object] = {
                "family": "hand",
                "runtime_evidence": False,
                "family_verified": False,
                "promotion_eligible": False,
                "verified": False,
                "pass_1": [],
                "pass_2": [],
                "two_pass": {"pass_1_count": 0, "pass_2_count": 0, "agreement": False, "per_slot": []},
                "purity": {},
                "blockers": ["hand-validator-runtime-evidence-missing"],
            }
            drink_result = {
                "family": "drink",
                "runtime_evidence": False,
                "family_verified": False,
                "promotion_eligible": False,
                "verified": False,
                "actual_vs_projected": {"slot_count": 0, "slots": [], "all_equal": False},
                "per_slot": [],
                "blockers": ["drink-runtime-evidence-missing"],
            }
            end_result = {
                "family": "end_turn",
                "runtime_evidence": False,
                "family_verified": False,
                "promotion_eligible": False,
                "verified": False,
                "actual_vs_projected": {"slot_count": 0, "slots": [], "all_equal": False},
                "per_slot": [],
                "blockers": ["end_turn-runtime-evidence-missing"],
            }
        else:
            # Enriched payloads still need the core candidate envelope and
            # observed identity list.  ``_validate_candidate`` is intentionally
            # not reused because its status fields describe the old blocked
            # schema, not a verified probe result.
            if candidate is None:
                _issue(issues, "candidate-missing", position=position, action_order=action_order)
                structural_pass = False
                step_structural_pass = False
            else:
                candidate_schema = candidate.get("schema")
                if not (
                    candidate_schema == CANDIDATE_SCHEMA
                    or (
                        isinstance(candidate_schema, str)
                        and candidate_schema.startswith(
                            "gkms.runtime-exam-legal-action-candidates."
                        )
                    )
                ):
                    _issue(
                        issues,
                        "enriched-candidate-schema-mismatch",
                        position=position,
                        action_order=action_order,
                        observed=candidate_schema,
                    )
                    structural_pass = False
                    step_structural_pass = False
                for field, expected in (
                    ("shadow", True),
                    ("exact", False),
                    ("complete", False),
                    ("legal_actions_complete", False),
                    ("legal_actions", None),
                    ("authoritative_legal_actions", None),
                ):
                    if field in candidate and candidate.get(field) != expected:
                        _issue(
                            issues,
                            "enriched-candidate-field-mismatch",
                            position=position,
                            action_order=action_order,
                            field=field,
                            expected=expected,
                            observed=candidate.get(field),
                        )
                        structural_pass = False
                        step_structural_pass = False
            hand_payloads = _family_payloads(body, candidate, "hand") or payloads
            # A compact writer may keep one common purity object beside the
            # three family maps.  Include that shared object as a fallback
            # while retaining the family-specific pass rows.
            if not any(
                any(key in payload for key in ("purity", "state_purity", "enumeration_purity", "guards"))
                for payload in hand_payloads
            ):
                hand_payloads = list(hand_payloads) + [
                    payload
                    for payload in payloads
                    if any(key in payload for key in ("purity", "state_purity", "enumeration_purity", "guards"))
                ]
            hand_result = _hand_family_result(
                hand_payloads,
                candidate=candidate,
                position=position,
                action_order=action_order,
                issues=issues,
            )
            drink_payloads = _family_payloads(body, candidate, "drink") or payloads
            if not any(_runtime_evidence_marker(payload) for payload in drink_payloads):
                drink_payloads = list(drink_payloads) + [
                    payload for payload in payloads if _runtime_evidence_marker(payload)
                ]
            drink_result = _passive_family_result(
                "drink",
                drink_payloads,
                position=position,
                action_order=action_order,
                issues=issues,
            )
            end_payloads = _family_payloads(body, candidate, "end_turn") or payloads
            if not any(_runtime_evidence_marker(payload) for payload in end_payloads):
                end_payloads = list(end_payloads) + [
                    payload for payload in payloads if _runtime_evidence_marker(payload)
                ]
            end_result = _passive_family_result(
                "end_turn",
                end_payloads,
                position=position,
                action_order=action_order,
                issues=issues,
            )
        settlement = _settlement_result(
            body,
            candidate,
            position=position,
            action_order=action_order,
            issues=issues,
        )
        membership = _official_membership(
            body,
            hand=hand_result,
            drink=drink_result,
            end_turn=end_result,
            position=position,
            action_order=action_order,
            issues=issues,
        )
        terminal = body is not None and body.get("terminal") is True
        if terminal:
            terminal_positions.append(position)
        families = {
            "hand": hand_result,
            "drink": drink_result,
            "end_turn": end_result,
            # ``endturn`` is retained as a spelling alias for shell/JSONL
            # consumers that use the probe plan's compact family label.
            "endturn": end_result,
        }
        family_eligibility = {
            family: {
                "family_verified": bool(result.get("family_verified")),
                "promotion_eligible": bool(result.get("promotion_eligible")),
                "runtime_evidence": bool(result.get("runtime_evidence")),
                "blockers": list(result.get("blockers", [])),
            }
            for family, result in families.items()
        }
        actions = _authoritative_actions(hand_result, drink_result, end_result)
        action_consistency = _action_set_consistency(
            body,
            payloads,
            actions,
            position=position,
            action_order=action_order,
            issues=issues,
        )
        step_passed = bool(
            step_structural_pass
            and settlement.get("passed") is True
            and all(result.get("family_verified") is True for result in families.values())
            and action_consistency.get("consistent") is True
        )
        steps.append(
            {
                "position": position,
                "action_order": action_order,
                "hand": hand_result,
                "drink": drink_result,
                "end_turn": end_result,
                "endturn": end_result,
                "families": families,
                "family_promotion_eligibility": family_eligibility,
                "official_chosen_membership": membership,
                "action_set_consistency": action_consistency,
                "settlement": settlement,
                "purity": hand_result.get("purity", {}),
                "authoritative_legal_actions": actions if step_passed else None,
                "legal_actions": actions if step_passed else None,
                "step_verified": step_passed,
                "terminal": terminal,
            }
        )

    count = len(transitions)
    # ``structural_pass`` is intentionally separate from family status: a
    # partial next probe can be ingested and reported without allowing an
    # incomplete family to become authority.
    terminal_once_last = len(terminal_positions) == 1 and terminal_positions[-1] == count - 1
    turn1_to_terminal = {
        "passed": bool(turn1_positions) and turn1_positions[0] == 0 and terminal_once_last,
        "transition_count": count,
        "turn1_state_before_count": len(turn1_positions),
        "turn1_positions": turn1_positions,
        "terminal_transition_count": len(terminal_positions),
        "terminal_positions": terminal_positions,
        "terminal_last": terminal_once_last,
        "candidate_presence_count": candidate_presence_count,
        "candidate_presence_coverage": candidate_presence_count / count if count else 0.0,
        "hand_observed_candidates_presence_count": hand_observed_presence_count,
        "hand_observed_candidates_presence_coverage": hand_observed_presence_count / count if count else 0.0,
        "state_policy": "native runtime snapshots only; no simulator/ExamSave fill-in",
    }
    settlement_complete = bool(steps) and all(step["settlement"].get("passed") is True for step in steps)
    action_consistency_complete = bool(steps) and all(
        step.get("action_set_consistency", {}).get("consistent") is True
        for step in steps
    )
    family_summary: dict[str, object] = {}
    for family in FAMILY_NAMES:
        values = [step["families"][family] for step in steps]
        verified_count = sum(value.get("family_verified") is True for value in values)
        family_summary[family] = {
            "family": family,
            "runtime_evidence_steps": sum(value.get("runtime_evidence") is True for value in values),
            "verified_steps": verified_count,
            "family_verified": bool(values) and verified_count == count,
            "promotion_eligible": bool(values) and verified_count == count,
            "complete": bool(values) and verified_count == count,
            "blockers": [
                {"position": index, "blockers": list(value.get("blockers", []))}
                for index, value in enumerate(values)
                if value.get("blockers")
            ],
        }
    all_families_verified = bool(steps) and all(
        family_summary[family]["family_verified"] is True for family in FAMILY_NAMES
    )
    # Keep the compact ``endturn`` spelling in addition to the canonical
    # ``end_turn`` key used by the recorder's JSON fields.
    family_summary["endturn"] = family_summary["end_turn"]
    authoritative_ready = bool(
        steps
        and all_families_verified
        and settlement_complete
        and terminal_once_last
        and structural_pass
        and not issues
        and action_consistency_complete
    )
    stage_actions = [step["authoritative_legal_actions"] for step in steps]
    candidate_checks = {
        "transition_count": count,
        "structural_passed": structural_pass,
        "settlement_complete": settlement_complete,
        "terminal_once_last": terminal_once_last,
        "family_summary": family_summary,
        "all_families_verified": all_families_verified,
        "steps": steps[:MAX_REPORTED_STEPS],
        "issues": issues,
        "passed": bool(structural_pass and not issues),
    }
    # A family may be verified while the complete action array is not ready;
    # expose both the per-family and whole-array decisions explicitly.
    return {
        "schema": RUNTIME_PROBE_SCHEMA,
        "probe_version": RUNTIME_PROBE_VERSION,
        "candidate_schema": CANDIDATE_SCHEMA,
        "boundary": SETTLED_BOUNDARY,
        "transition_count": count,
        "candidate_presence_count": candidate_presence_count,
        "hand_observed_candidates_presence_count": hand_observed_presence_count,
        "candidate_presence_complete": count > 0 and candidate_presence_count == count,
        "hand_observed_candidates_presence_complete": count > 0 and hand_observed_presence_count == count,
        "turn1_to_terminal": turn1_to_terminal,
        "orders": orders,
        "steps": steps,
        "candidate_checks": candidate_checks,
        "family_promotion_eligibility": family_summary,
        "settlement": {
            "complete": settlement_complete,
            "terminal_once_last": terminal_once_last,
            "per_step": [step["settlement"] for step in steps],
        },
        "action_set_consistency": {
            "complete": action_consistency_complete,
            "per_step": [step["action_set_consistency"] for step in steps],
        },
        "official_chosen_membership": [step["official_chosen_membership"] for step in steps],
        "official_chosen_action_membership": [step["official_chosen_membership"] for step in steps],
        "state_rng_phase_busy_stack_turn_end_purity": [step["purity"] for step in steps],
        "drink_actual_vs_projected": [step["drink"]["actual_vs_projected"] for step in steps],
        "end_turn_actual_vs_projected": [step["end_turn"]["actual_vs_projected"] for step in steps],
        "endturn_actual_vs_projected": [step["end_turn"]["actual_vs_projected"] for step in steps],
        "legal_actions": stage_actions if authoritative_ready else None,
        "authoritative_legal_actions": stage_actions if authoritative_ready else None,
        "legal_actions_complete": authoritative_ready,
        "exact": authoritative_ready,
        "candidate_actions_authoritative": authoritative_ready,
        "legality_inferred": False,
        "promotion": {
            "allowed": authoritative_ready,
            "status": "runtime-verified" if authoritative_ready else "shadow-only",
            "family_verified": all_families_verified,
            "hand_verified": family_summary["hand"]["family_verified"],
            "drink_verified": family_summary["drink"]["family_verified"],
            "end_turn_verified": family_summary["end_turn"]["family_verified"],
            "endturn_verified": family_summary["end_turn"]["family_verified"],
            "settlement_verified": settlement_complete,
            "exact": authoritative_ready,
            "legal_actions_complete": authoritative_ready,
            "reason": (
                "all three runtime predicate families and settlement passed"
                if authoritative_ready
                else "three family runtime evidence and settlement are required"
            ),
        },
        "passed": bool(structural_pass and not issues),
    }


# Discoverable aliases for callers that use the wording from the probe plan.
validate_runtime_replay_legal_action_stage = validate_runtime_replay_main_action_stage
validate_runtime_replay_hand_and_passive_stage = validate_runtime_replay_main_action_stage
validate_runtime_replay_legal_probe_stage = validate_runtime_replay_main_action_stage


def _validate_transition_envelope(
    row: Mapping[str, object],
    body: Mapping[str, object] | None,
    *,
    issues: list[dict[str, object]],
    position: int,
) -> object:
    action_order = body.get("action_order") if body is not None else None
    if row.get("schema") != RECORDER_SCHEMA:
        _issue(issues, "recorder-schema-mismatch", position=position, action_order=action_order)
    for field, expected in (("shadow", True), ("exact", False), ("legal_actions_complete", False)):
        if row.get(field) != expected:
            _issue(
                issues,
                "recorder-envelope-field-mismatch",
                position=position,
                action_order=action_order,
                field=field,
                expected=expected,
                observed=row.get(field),
            )
    if body is None:
        _issue(issues, "transition-body-not-object", position=position, action_order=action_order)
        return None
    if body.get("record") != "transition":
        _issue(issues, "transition-record-mismatch", position=position, action_order=action_order)
    # Current recorder rows identify the state pairing boundary separately
    # from the nested candidate payload.  Keep this check conditional for
    # older retained rows that predate the field, but reject a claimed
    # non-settled boundary if it is present.
    if "boundary" in body:
        boundary = body.get("boundary")
        allowed_boundaries = {SETTLED_BOUNDARY, "terminal-marker"}
        if boundary not in allowed_boundaries:
            _issue(
                issues,
                "transition-boundary-mismatch",
                position=position,
                action_order=action_order,
                expected=sorted(allowed_boundaries),
                observed=boundary,
            )
    if "legal_actions" not in body:
        _issue(
            issues,
            "transition-legal-actions-missing",
            position=position,
            action_order=action_order,
        )
    raw_legal_actions = body.get("legal_actions")
    # Older shadow contracts represented the whole field as null; the current
    # recorder wraps it in a policy object whose ``actions`` member is null.
    # Both forms preserve the same non-authoritative meaning and are accepted
    # here, while any concrete action array remains a schema failure.
    if raw_legal_actions is None:
        legal_actions = None
    else:
        legal_actions = _mapping(raw_legal_actions)
    if raw_legal_actions is not None and legal_actions is None:
        _issue(issues, "transition-legal-actions-not-object", position=position, action_order=action_order)
    elif legal_actions is not None:
        # The verified probe writes a concrete body-level ``legal_actions``
        # object only after all three runtime families pass.  Legacy shadow
        # rows use the null/incomplete shape below.  The outer recorder row
        # itself remains shadow-only, so this is safe to inspect offline.
        runtime_probe = _mapping(body.get("runtime_legal_action_probe"))
        runtime_family_map = _mapping(runtime_probe.get("families")) if runtime_probe is not None else None
        if runtime_family_map is None and runtime_probe is not None:
            runtime_family_map = runtime_probe
        runtime_settlement_map = _mapping(runtime_probe.get("settlement")) if runtime_probe is not None else None
        runtime_families_verified = (
            runtime_family_map is not None
            and all(
                _mapping(runtime_family_map.get(name)) is not None
                and _mapping(runtime_family_map.get(name)).get("family_verified") is True  # type: ignore[union-attr]
                for name in ("hand", "drink", "end_turn")
            )
            and runtime_settlement_map is not None
            and runtime_settlement_map.get("complete") is True
        )
        body_verified = (
            runtime_probe is not None
            and runtime_probe.get("schema") == RUNTIME_PROBE_SCHEMA
            and (
                (
                    runtime_probe.get("legal_actions_complete") is True
                    and runtime_probe.get("exact") is True
                )
                or runtime_families_verified
            )
        )
        if body_verified and legal_actions.get("actions") is not None:
            for field, expected in (
                ("complete", True),
                ("legal_actions_complete", True),
                ("exact", True),
            ):
                if legal_actions.get(field) != expected:
                    _issue(
                        issues,
                        "transition-legal-actions-field-mismatch",
                        position=position,
                        action_order=action_order,
                        field=f"legal_actions.{field}",
                        expected=expected,
                        observed=legal_actions.get(field),
                    )
            if not isinstance(legal_actions.get("actions"), list):
                _issue(
                    issues,
                    "transition-legal-actions-not-array",
                    position=position,
                    action_order=action_order,
                )
        else:
            for field, expected in (
                ("complete", False),
                ("legal_actions_complete", False),
                ("exact", False),
                ("actions", None),
            ):
                if legal_actions.get(field) != expected:
                    _issue(
                        issues,
                        "transition-legal-actions-field-mismatch",
                        position=position,
                        action_order=action_order,
                        field=f"legal_actions.{field}",
                        expected=expected,
                        observed=legal_actions.get(field),
                    )
    return action_order


def validate_runtime_replay_legal_candidate_stage(
    transitions: Sequence[Mapping[str, object]],
    *,
    base_report: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Validate candidate evidence on one already selected Replay stage.

    ``transitions`` must be the selected envelopes from
    :attr:`RuntimeReplayStageWatcher.stage_transition_rows`.  The function is
    pure and performs no file, process, controller, or simulator access.
    """

    # The v1 public entry point remains the single place callers need to know
    # about.  When the next native probe attaches an explicit runtime payload,
    # route it through the family-aware validator; ordinary v1 rows continue
    # through the historical shadow-only checks below unchanged.
    for raw_row in transitions:
        raw_body = _mapping(raw_row.get("body"))
        raw_candidate = (
            _mapping(raw_body.get("legal_action_candidates"))
            if raw_body is not None
            else None
        )
        if _candidate_runtime_payloads(raw_body, raw_candidate):
            return validate_runtime_replay_main_action_stage(
                transitions,
                base_report=base_report,
            )

    issues: list[dict[str, object]] = []
    steps: list[dict[str, object]] = []
    candidate_presence_count = 0
    schema_valid_count = 0
    hand_presence_count = 0
    settled_count = 0
    purity_equal_count = 0
    legal_unknown_count = 0
    turn1_positions: list[int] = []
    terminal_positions: list[int] = []
    orders: list[object] = []

    for position, row in enumerate(transitions):
        body = _mapping(row.get("body"))
        action_order = _validate_transition_envelope(
            row,
            body,
            issues=issues,
            position=position,
        )
        orders.append(action_order)
        if body is not None:
            before = _mapping(body.get("state_before"))
            if before is not None and before.get("currentTurn") == 1:
                turn1_positions.append(position)
            if body.get("terminal") is True:
                terminal_positions.append(position)
        candidate = _mapping(body.get("legal_action_candidates")) if body is not None else None
        result = _validate_candidate(
            candidate,
            issues=issues,
            position=position,
            action_order=action_order,
        )
        if result["present"]:
            candidate_presence_count += 1
        if result["schema_valid"]:
            schema_valid_count += 1
        if result["hand_observed_present"]:
            hand_presence_count += 1
        if result["settled"]:
            settled_count += 1
        if result["purity_equal"]:
            purity_equal_count += 1
        legal_unknown_count += int(result["legal_unknown_count"])
        if len(steps) < MAX_REPORTED_STEPS:
            steps.append(
                {
                    "position": position,
                    "action_order": action_order,
                    "candidate_present": result["present"],
                    "candidate_schema_valid": result["schema_valid"],
                    "settled": result["settled"],
                    "purity_equal": result["purity_equal"],
                    "hand_observed_candidates_present": result["hand_observed_present"],
                    "hand_observed_candidate_count": result["hand_count"],
                    "identity_complete_count": result["identity_complete_count"],
                    "legal_unknown_count": result["legal_unknown_count"],
                }
            )

    count = len(transitions)
    candidate_presence_passed = count > 0 and candidate_presence_count == count
    hand_presence_passed = count > 0 and hand_presence_count == count
    schema_passed = count > 0 and schema_valid_count == count
    settled_passed = count > 0 and settled_count == count
    purity_passed = count > 0 and purity_equal_count == count
    terminal_once_last = (
        len(terminal_positions) == 1 and terminal_positions[-1] == count - 1
    )
    turn1_to_terminal_passed = bool(turn1_positions) and turn1_positions[0] == 0 and terminal_once_last
    base_stage = base_report.get("stage") if isinstance(base_report, Mapping) else None
    base_stage_passed = base_stage.get("passed") is True if isinstance(base_stage, Mapping) else True

    turn1_to_terminal = {
        "passed": turn1_to_terminal_passed and candidate_presence_passed and hand_presence_passed,
        "transition_count": count,
        "turn1_state_before_count": len(turn1_positions),
        "turn1_positions": turn1_positions,
        "terminal_transition_count": len(terminal_positions),
        "terminal_positions": terminal_positions,
        "terminal_last": terminal_once_last,
        "candidate_presence_count": candidate_presence_count,
        "candidate_presence_coverage": (
            candidate_presence_count / count if count else 0.0
        ),
        "hand_observed_candidates_presence_count": hand_presence_count,
        "hand_observed_candidates_presence_coverage": (
            hand_presence_count / count if count else 0.0
        ),
        "state_policy": "native runtime snapshots only; no simulator/ExamSave fill-in",
    }
    candidate_checks = {
        "passed": (
            candidate_presence_passed
            and hand_presence_passed
            and schema_passed
            and settled_passed
            and purity_passed
            and not issues
        ),
        "transition_count": count,
        "candidate_presence_count": candidate_presence_count,
        "candidate_presence_complete": candidate_presence_passed,
        "candidate_schema_valid_count": schema_valid_count,
        "candidate_schema_valid_complete": schema_passed,
        "hand_observed_candidates_presence_count": hand_presence_count,
        "hand_observed_candidates_presence_complete": hand_presence_passed,
        "settled_boundary_count": settled_count,
        "settled_boundary_complete": settled_passed,
        "enumeration_purity_equal_count": purity_equal_count,
        "enumeration_purity_equal_complete": purity_passed,
        "legal_unknown_count": legal_unknown_count,
        "legality_inferred": False,
        "orders": orders,
        "steps": steps,
        "turn1_to_terminal": turn1_to_terminal,
        "issues": issues,
    }
    return {
        "schema": SCHEMA,
        "probe_version": PROBE_VERSION,
        "candidate_schema": CANDIDATE_SCHEMA,
        "boundary": SETTLED_BOUNDARY,
        "transition_count": count,
        "candidate_presence_count": candidate_presence_count,
        "hand_observed_candidates_presence_count": hand_presence_count,
        "candidate_presence_complete": candidate_presence_passed,
        "hand_observed_candidates_presence_complete": hand_presence_passed,
        "turn1_to_terminal": turn1_to_terminal,
        "candidate_checks": candidate_checks,
        "legal_actions": None,
        "legal_actions_complete": False,
        "exact": False,
        "candidate_actions_authoritative": False,
        "legality_inferred": False,
        "base_stage_passed": base_stage_passed,
        "passed": bool(base_stage_passed and candidate_checks["passed"]),
        "promotion": {
            "allowed": False,
            "status": "shadow-only",
            "exact": False,
            "legal_actions_complete": False,
            "reason": (
                "observed hand identities are candidate evidence only; legality is not inferred"
            ),
        },
    }


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_path(path: Path) -> dict[str, object]:
    """Return a deterministic read-only hash record for a file or source tree."""

    resolved = path.expanduser().resolve()
    if resolved.is_file():
        try:
            return {
                "path": str(resolved),
                "kind": "file",
                "exists": True,
                "sha256": _sha256_bytes(resolved.read_bytes()),
                "file_count": 1,
                "error": None,
            }
        except OSError as error:
            return {
                "path": str(resolved),
                "kind": "file",
                "exists": False,
                "sha256": None,
                "file_count": 0,
                "error": f"{type(error).__name__}:{error}",
            }
    if resolved.is_dir():
        digest = hashlib.sha256()
        file_count = 0
        try:
            files = sorted(
                child
                for child in resolved.rglob("*")
                if child.is_file()
            )
            for child in files:
                relative = child.relative_to(resolved).as_posix().encode("utf-8")
                data = child.read_bytes()
                digest.update(len(relative).to_bytes(8, "big"))
                digest.update(relative)
                digest.update(len(data).to_bytes(8, "big"))
                digest.update(data)
                file_count += 1
            return {
                "path": str(resolved),
                "kind": "directory",
                "exists": True,
                "sha256": digest.hexdigest(),
                "file_count": file_count,
                "error": None,
            }
        except OSError as error:
            return {
                "path": str(resolved),
                "kind": "directory",
                "exists": False,
                "sha256": None,
                "file_count": file_count,
                "error": f"{type(error).__name__}:{error}",
            }
    return {
        "path": str(resolved),
        "kind": "missing",
        "exists": False,
        "sha256": None,
        "file_count": 0,
        "error": "path-not-found",
    }


def build_runtime_legal_candidate_manifest(
    candidate_dll: str | Path | None = DEFAULT_CANDIDATE_DLL,
    candidate_source: str | Path | None = DEFAULT_CANDIDATE_SOURCE,
    observer_restore: str | Path | None = None,
) -> dict[str, object]:
    """Hash the candidate build and restore witness without deploying anything."""

    candidate_record = (
        _hash_path(Path(candidate_dll)) if candidate_dll is not None else {
            "path": None,
            "kind": "unspecified",
            "exists": False,
            "sha256": None,
            "file_count": 0,
            "error": "candidate-dll-not-specified",
        }
    )
    source_record = (
        _hash_path(Path(candidate_source)) if candidate_source is not None else {
            "path": None,
            "kind": "unspecified",
            "exists": False,
            "sha256": None,
            "file_count": 0,
            "error": "candidate-source-not-specified",
        }
    )
    restore_record = (
        _hash_path(Path(observer_restore)) if observer_restore is not None else {
            "path": None,
            "kind": "unspecified",
            "exists": False,
            "sha256": None,
            "file_count": 0,
            "error": "observer-restore-not-specified",
        }
    )
    candidate_path = candidate_record.get("path")
    installed_observer_path = bool(
        isinstance(candidate_path, str)
        and "observer_candidate" in candidate_path.replace("/", "\\").casefold()
    )
    blockers: list[str] = []
    for name, record in (
        ("candidate-dll", candidate_record),
        ("candidate-source", source_record),
        ("observer-restore", restore_record),
    ):
        if record.get("exists") is not True:
            blockers.append(f"{name}-hash-unavailable")
    if installed_observer_path:
        blockers.append("candidate-path-is-installed-observer-candidate")
    return {
        "schema": MANIFEST_SCHEMA,
        "manifest_version": "hashes-v1",
        "read_only": True,
        "deployment_performed": False,
        "deployment_allowed": False,
        "game_started": False,
        "controller_access": False,
        "candidate_dll": candidate_record,
        "candidate_source": source_record,
        "observer_restore": restore_record,
        "artifacts": {
            "candidate_dll": candidate_record,
            "candidate_source": source_record,
            "observer_restore": restore_record,
        },
        "hashes": {
            "candidate_dll_sha256": candidate_record.get("sha256"),
            "candidate_source_sha256": source_record.get("sha256"),
            "observer_restore_sha256": restore_record.get("sha256"),
        },
        # Flat aliases keep the manifest convenient for shell/CI consumers
        # and mirror the naming used by the existing deployment receipts.
        "candidate_dll_sha256": candidate_record.get("sha256"),
        "candidate_sha256": candidate_record.get("sha256"),
        "candidate_source_sha256": source_record.get("sha256"),
        "source_sha256": source_record.get("sha256"),
        "observer_restore_sha256": restore_record.get("sha256"),
        "restore_sha256": restore_record.get("sha256"),
        "installed_observer_candidate_untouched": not installed_observer_path,
        "blockers": blockers,
        "passed": not blockers,
    }


def write_runtime_legal_candidate_manifest(
    manifest: Mapping[str, object], output_path: str | Path = DEFAULT_MANIFEST_OUTPUT
) -> Path:
    """Write a hash manifest; the only mutation permitted by this module."""

    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    return output


def _validate_wait_args(
    *,
    input_path: str | Path | None,
    pid: int | None,
    expected_pid: int | None,
    timeout_seconds: float,
    poll_interval_seconds: float,
    chunk_bytes: int,
) -> None:
    if input_path is not None and pid is not None:
        raise RuntimeLegalCandidateProbeError("provide either input_path or pid, not both")
    if input_path is None and pid is None:
        raise RuntimeLegalCandidateProbeError("one of input_path or pid is required")
    if pid is not None and expected_pid is not None:
        raise RuntimeLegalCandidateProbeError("provide either pid or expected_pid, not both")
    if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
        raise RuntimeLegalCandidateProbeError("pid must be a positive integer")
    if expected_pid is not None and (
        isinstance(expected_pid, bool)
        or not isinstance(expected_pid, int)
        or expected_pid <= 0
    ):
        raise RuntimeLegalCandidateProbeError("expected_pid must be a positive integer")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds < 0
    ):
        raise RuntimeLegalCandidateProbeError("timeout_seconds must be a finite number >= 0")
    if (
        isinstance(poll_interval_seconds, bool)
        or not isinstance(poll_interval_seconds, (int, float))
        or not math.isfinite(float(poll_interval_seconds))
        or poll_interval_seconds <= 0
    ):
        raise RuntimeLegalCandidateProbeError(
            "poll_interval_seconds must be a finite number > 0"
        )
    if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int) or chunk_bytes <= 0:
        raise RuntimeLegalCandidateProbeError("chunk_bytes must be a positive integer")


def watch_runtime_replay_legal_candidates(
    input_path: str | Path | None = None,
    *,
    pid: int | None = None,
    expected_pid: int | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    candidate_dll: str | Path | None = DEFAULT_CANDIDATE_DLL,
    candidate_source: str | Path | None = DEFAULT_CANDIDATE_SOURCE,
    observer_restore: str | Path | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Incrementally wait for the first complete Replay stage and validate it."""

    _validate_wait_args(
        input_path=input_path,
        pid=pid,
        expected_pid=expected_pid,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        chunk_bytes=chunk_bytes,
    )
    path = default_shadow_path(pid) if pid is not None else Path(input_path)  # type: ignore[arg-type]
    reader = IncrementalShadowReader(path, chunk_bytes=chunk_bytes)
    watcher = RuntimeReplayStageWatcher(
        expected_mode="replay",
        expected_pid=pid if pid is not None else expected_pid,
    )
    started_at = monotonic()
    deadline = started_at + float(timeout_seconds)
    while True:
        rows, errors = reader.poll()
        watcher.consume(rows, errors)
        now = monotonic()
        if watcher.stage_complete or now >= deadline:
            elapsed = max(0.0, now - started_at)
            break
        sleep(min(float(poll_interval_seconds), max(0.0, deadline - now)))

    base = watcher.report(
        path=path,
        reader=reader,
        timed_out=not watcher.stage_complete,
        elapsed_seconds=elapsed,
    )
    candidate_report = validate_runtime_replay_legal_candidate_stage(
        watcher.stage_transition_rows,
        base_report=base,
    )
    manifest = build_runtime_legal_candidate_manifest(
        candidate_dll=candidate_dll,
        candidate_source=candidate_source,
        observer_restore=observer_restore,
    )
    scan = base.get("scan") if isinstance(base.get("scan"), Mapping) else {}
    parse_errors = scan.get("parse_errors", []) if isinstance(scan, Mapping) else []
    parse_clean = isinstance(parse_errors, list) and not parse_errors
    runtime_legal_actions = candidate_report.get("legal_actions")
    runtime_authoritative_actions = candidate_report.get("authoritative_legal_actions")
    runtime_complete = candidate_report.get("legal_actions_complete") is True
    runtime_promotion = candidate_report.get("promotion")
    if not isinstance(runtime_promotion, Mapping):
        runtime_promotion = {
            "allowed": False,
            "status": "shadow-only",
            "exact": False,
            "legal_actions_complete": False,
            "reason": "candidate-only evidence",
        }
    result: dict[str, object] = dict(base)
    result.update(
        {
            "schema": SCHEMA,
            "probe_version": PROBE_VERSION,
            "legal_candidate_probe": candidate_report,
            "candidate_presence": {
                "count": candidate_report["candidate_presence_count"],
                "complete": candidate_report["candidate_presence_complete"],
                "steps_with_candidates": candidate_report["candidate_presence_count"],
                "steps_total": candidate_report["transition_count"],
                "coverage": (
                    candidate_report["candidate_presence_count"]
                    / candidate_report["transition_count"]
                    if candidate_report["transition_count"]
                    else 0.0
                ),
                "hand_observed_count": candidate_report[
                    "hand_observed_candidates_presence_count"
                ],
                "hand_observed_complete": candidate_report[
                    "hand_observed_candidates_presence_complete"
                ],
                "hand_steps_with_candidates": candidate_report[
                    "hand_observed_candidates_presence_count"
                ],
            },
            "turn1_to_terminal": candidate_report["turn1_to_terminal"],
            "runtime_legal_action_probe": candidate_report,
            "family_promotion_eligibility": candidate_report.get(
                "family_promotion_eligibility", {}
            ),
            "official_chosen_membership": candidate_report.get(
                "official_chosen_membership", []
            ),
            "settlement": candidate_report.get("settlement", {}),
            "hash_manifest": manifest,
            "manifest": manifest,
            "hash_manifest_passed": manifest["passed"],
            "legal_actions": runtime_legal_actions,
            "authoritative_legal_actions": runtime_authoritative_actions,
            "legal_actions_complete": runtime_complete,
            "exact": candidate_report.get("exact") is True,
            "candidate_actions_authoritative": candidate_report.get(
                "candidate_actions_authoritative"
            ) is True,
            "legality_inferred": False,
            "candidate_dll_sha256": manifest["candidate_dll_sha256"],
            "candidate_source_sha256": manifest["candidate_source_sha256"],
            "observer_restore_sha256": manifest["observer_restore_sha256"],
            "input_parse_errors": list(parse_errors) if isinstance(parse_errors, list) else parse_errors,
            "promotion": dict(runtime_promotion),
            "passed": bool(
                candidate_report["passed"]
                and base.get("complete") is True
                and parse_clean
            ),
        }
    )
    return result


def write_runtime_replay_legal_candidate_probe(
    report: Mapping[str, object], output_path: str | Path = DEFAULT_OUTPUT
) -> Path:
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return output


# Short aliases make the offline API easy to find beside the existing watcher.
run_runtime_replay_legal_candidate_probe = watch_runtime_replay_legal_candidates
probe_runtime_replay_legal_candidates = watch_runtime_replay_legal_candidates
watch_runtime_replay_legal_candidate = watch_runtime_replay_legal_candidates
validate_runtime_replay_legal_candidates = validate_runtime_replay_legal_candidate_stage
validate_legal_candidate_stage = validate_runtime_replay_legal_candidate_stage
build_runtime_legal_candidate_hash_manifest = build_runtime_legal_candidate_manifest


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Incrementally wait for one Replay stage in a runtime shadow JSONL "
            "and validate observed hand candidates or the three-family runtime "
            "probe (offline only)."
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="runtime recorder shadow JSONL")
    source.add_argument(
        "--pid", type=int, help="game PID; derives the recorder telemetry path (read only)"
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL_SECONDS)
    parser.add_argument("--chunk-bytes", type=int, default=DEFAULT_CHUNK_BYTES)
    parser.add_argument(
        "--candidate-dll",
        "--candidate",
        dest="candidate_dll",
        type=Path,
        default=DEFAULT_CANDIDATE_DLL,
    )
    parser.add_argument(
        "--candidate-source",
        "--source",
        dest="candidate_source",
        type=Path,
        default=DEFAULT_CANDIDATE_SOURCE,
    )
    parser.add_argument(
        "--observer-restore",
        "--restore",
        dest="observer_restore",
        type=Path,
        help="read-only restore DLL/backup to hash",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_OUTPUT)
    args = parser.parse_args(argv)
    try:
        input_source = (
            default_shadow_path(args.pid)
            if args.pid is not None
            else Path(args.input)
        ).expanduser().resolve()
        output_path = args.output.expanduser().resolve()
        manifest_path = args.manifest.expanduser().resolve()
        if input_source in {output_path, manifest_path}:
            raise RuntimeLegalCandidateProbeError(
                "probe output/manifest must not overwrite the shadow JSONL input"
            )
        if output_path == manifest_path:
            raise RuntimeLegalCandidateProbeError(
                "probe output and hash manifest must be different paths"
            )
        report = watch_runtime_replay_legal_candidates(
            args.input,
            pid=args.pid,
            timeout_seconds=args.timeout,
            poll_interval_seconds=args.poll_interval,
            chunk_bytes=args.chunk_bytes,
            candidate_dll=args.candidate_dll,
            candidate_source=args.candidate_source,
            observer_restore=args.observer_restore,
        )
        write_runtime_replay_legal_candidate_probe(report, output_path)
        write_runtime_legal_candidate_manifest(report["hash_manifest"], manifest_path)
    except (RuntimeLegalCandidateProbeError, OSError, ValueError) as error:
        parser.error(str(error))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report.get("passed") is True else 2


__all__ = [
    "CANDIDATE_SCHEMA",
    "DEFAULT_CANDIDATE_DLL",
    "DEFAULT_CANDIDATE_SOURCE",
    "DEFAULT_MANIFEST_OUTPUT",
    "DEFAULT_OUTPUT",
    "FAMILY_NAMES",
    "MANIFEST_SCHEMA",
    "PROBE_VERSION",
    "RECORDER_BOUNDARY",
    "RUNTIME_PROBE_SCHEMA",
    "RUNTIME_PROBE_VERSION",
    "RuntimeLegalCandidateProbeError",
    "SCHEMA",
    "SETTLED_BOUNDARY",
    "build_runtime_legal_candidate_hash_manifest",
    "build_runtime_legal_candidate_manifest",
    "main",
    "probe_runtime_replay_legal_candidates",
    "run_runtime_replay_legal_candidate_probe",
    "validate_legal_candidate_stage",
    "validate_runtime_replay_hand_and_passive_stage",
    "validate_runtime_replay_legal_action_stage",
    "validate_runtime_replay_legal_candidate_stage",
    "validate_runtime_replay_legal_candidates",
    "validate_runtime_replay_legal_probe_stage",
    "validate_runtime_replay_main_action_stage",
    "watch_runtime_replay_legal_candidate",
    "watch_runtime_replay_legal_candidates",
    "write_runtime_legal_candidate_manifest",
    "write_runtime_replay_legal_candidate_probe",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
