"""Existing secondary choice rule over an actual original-PC callback pool.

The calling policy adapter owns run/source/boundary verification and dispatch.
This pure selector reads no replay answers and manufactures no UI frames. It
returns one ordered native type-4 response using the existing direction/count/
rank rule, without changing the UI policy or the official calculation engine.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

from . import runtime_exam_continuation_policy as legacy
from .runtime_exam_continuation_policy import SECONDARY_POLICY_ID, _BENEFICIAL, _rank
from .training_artifact_io import sha256_file

SCHEMA = "gkms.native-policy-secondary-response.v1"
NATIVE_METADATA_SHA256 = "9a6bf153c0c42a2768e619cc7d96fa79fcc1d341e9cf9bcbdc8d6bca48812668"
# Original PC field-default metadata, not guessed gameplay constants.
# Evidence: var/research/pc_core_20260914/native_policy_secondary_v1/enum_evidence.json
_MOVE_EFFECT, _UPGRADE_EFFECT, _SELECT_RANGE = 9, 11, 1
_MOVE_DIRECTIONS = {1: "retrieve", 5: "discard", 6: "discard", 7: "hold"}
_COMMAND_SOURCE = "actual-OnEffectSelectParameterAsync-command-argument"


@lru_cache(maxsize=1)
def _policy_source():
    return {"kind": "legacy-secondary-rule", "policy_id": SECONDARY_POLICY_ID,
        "implementation": {"path": str(Path(__file__).resolve()), "sha256": sha256_file(Path(__file__))},
        "legacy_rank_implementation": {"path": str(Path(legacy.__file__).resolve()),
            "sha256": sha256_file(Path(legacy.__file__))}, "training_model": False}


def _int(value, name, *, minimum=0, maximum=4096):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(name + " must be an original bounded integer")
    return value


def _sha(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _stamp(path):
    value = path.stat()
    return (value.st_size, value.st_mtime_ns, value.st_ctime_ns)


@lru_cache(maxsize=8)
def _checked_database(path, sha, stamp):
    path = Path(path)
    if sha256_file(path) != sha or _stamp(path) != stamp:
        raise ValueError("Source Master database SHA or identity differs")
    return path


def _database(reference, master):
    if reference is None:
        return None, None
    if (not isinstance(reference, Mapping) or not _sha(master)
            or reference.get("source_master_hash") != master
            or not isinstance(reference.get("path"), str) or not _sha(reference.get("sha256"))):
        raise ValueError("Secondary upgrade ranking needs a database bound to this source Master")
    path = Path(reference["path"]).resolve()
    stamp = _stamp(path)
    return _checked_database(str(path), reference["sha256"], stamp), stamp


def _resolve(value, references):
    seen = set()
    for _ in range(32):
        if not isinstance(value, Mapping) or set(value) != {"rid"}:
            return value
        rid = value["rid"]
        if type(rid) is not int or rid in seen:
            raise ValueError("Current command has an invalid or cyclic reference")
        seen.add(rid)
        matches = [x for x in references if isinstance(x, Mapping) and type(x.get("rid")) is int and x["rid"] == rid]
        if len(matches) != 1 or not isinstance(matches[0].get("data"), Mapping):
            raise ValueError("Current command/effect reference is missing or ambiguous")
        value = matches[0]["data"]
    raise ValueError("Current command reference depth exceeded")


def _direction(observation):
    context = observation.get("decision_context")
    unresolved = {"direction_source": "unknown", "current_effect_bound": False,
        "reason": "direct-current-command-binding-unavailable; last-log not used"}
    if (not isinstance(context, Mapping) or context.get("complete") is not True
            or context.get("read_errors") != [] or context.get("unqualified") != []):
        return "unknown", unresolved
    binding = context.get("pointer_binding")
    if (not isinstance(binding, Mapping) or context.get("command_source") != _COMMAND_SOURCE
            or context.get("effect_field_path") != "_playEffect"):
        return "unknown", unresolved
    if (binding.get("context_parameter_matches_sequence") is not True
            or any(binding.get(key) != observation.get(key + "_identity")
                   for key in ("command", "effect_context", "parameter"))
            or not isinstance(binding.get("command"), str) or binding["command"] in ("", "0x0")
            or not isinstance(binding.get("parameter"), str) or binding["parameter"] in ("", "0x0")):
        raise ValueError("Current secondary command/context does not match the actual callback owner")
    references = context.get("references", {}).get("RefIds", [])
    if not isinstance(references, list):
        raise ValueError("Current decision reference collection is invalid")
    command = _resolve(context.get("command"), references)
    if not isinstance(command, Mapping):
        return "unknown", unresolved
    effect = _resolve(command.get("_playEffect"), references)
    if not isinstance(effect, Mapping):
        return "unknown", unresolved
    second = command.get("_isCardSelect2")
    if type(second) is not bool:
        raise ValueError("Current command secondary-selection flag is not a native Boolean")
    suffix = "2" if second else ""
    pick_range = effect.get("_pickRangeType" + suffix)
    effect_type = effect.get("_effectType")
    if type(pick_range) is not int or type(effect_type) is not int:
        raise ValueError("Current effect range/type must be original native enums")
    search = command.get("_cardSelectSearchId" + suffix)
    effect_search = effect.get("_cardSearchId" + suffix)
    if ((search is not None and not isinstance(search, str))
            or (effect_search is not None and not isinstance(effect_search, str))):
        raise ValueError("Current command/effect search IDs have invalid native shapes")
    evidence = {"direction_source": "actual-current-command._playEffect", "current_effect_bound": True,
        "effect_id": effect.get("_id"), "native_effect_type": effect_type,
        "native_pick_range_type": pick_range, "native_card_search_id": search,
        "second_selection": second, "pointer_binding": dict(binding)}
    if pick_range != _SELECT_RANGE or (search and effect_search != search):
        return "unknown", {**evidence, "reason": "current Select-effect/search direction unresolved"}
    if effect_type == _MOVE_EFFECT:
        move = effect.get("_movePositionType")
        if type(move) is not int:
            raise ValueError("Current move destination is not an original native enum")
        direction = _MOVE_DIRECTIONS.get(move, "unknown")
        return direction, {**evidence, "native_move_position_type": move,
            "reason": "existing-Move-direction-rule" if direction != "unknown" else "unsupported-native-move-direction"}
    if effect_type == _UPGRADE_EFFECT:
        return "upgrade", {**evidence, "reason": "existing-Upgrade-direction-rule"}
    return "unknown", {**evidence, "reason": "unsupported-current-Select-effect-direction"}


def decide_native_secondary(observation, *, source_database_reference=None):
    """Return the old rule's ordered offered ordinals for one native callback.

    ``decision_context`` must come directly from the current callback command;
    a missing graph falls back to the old unknown/native-order rule. An explicit
    but contradictory pointer binding is rejected. Supplied pool evaluations
    are not consumed: Reader.card did not observe that getter in this baseline.
    """
    if (not isinstance(observation, Mapping) or observation.get("decision_type") != "secondary"
            or type(observation.get("kind")) is not int or observation["kind"] != 2
            or type(observation.get("phase")) is not int or observation["phase"] != 0):
        raise ValueError("A current native secondary decision-entry observation is required")
    if observation.get("native_metadata_sha256") != NATIVE_METADATA_SHA256:
        raise ValueError("Native secondary enum/field metadata differs from the fixed PC evidence")
    low = _int(observation.get("pick_min"), "pick_min")
    high = _int(observation.get("pick_max"), "pick_max", minimum=low)
    hand = observation.get("is_hand")
    if type(hand) not in (bool, int) or hand not in (False, True, 0, 1):
        raise ValueError("is_hand must preserve the actual Boolean callback argument")
    pool = observation.get("candidates")
    if (not isinstance(pool, Mapping) or pool.get("schema") != "gkms.original-pc-secondary-candidates.v1"
            or pool.get("read_errors") != [] or pool.get("purity_verified") is not True
            or any(pool.get(k) != observation.get(k) for k in ("pick_min", "pick_max"))
            or type(pool.get("pick_min")) is not int or type(pool.get("pick_max")) is not int
            or type(pool.get("is_hand")) is not bool or pool["is_hand"] != bool(hand)):
        raise ValueError("Native secondary pool and original callback constraints differ")
    candidates = pool.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("Original offered candidate array is missing")
    diagnostics = {"schema": SCHEMA, "pick_min": low, "pick_max": high, "is_hand": bool(hand),
        "offered_count": len(candidates), "teacher_indexes_used": False, "UI_frame_fabricated": False,
        "native_evaluation_observed": False, "evaluation_policy": "native-order/evaluation-unavailable unless source-Master upgrade delta is available",
        "optimality_claimed": False, "predicts_exam_score": False, "trained_secondary_model_used": False,
        "constraint_owner": "actual-OnEffectSelectParameterAsync-arguments"}
    if pool.get("selection_mode") == "forced-empty-response":
        null, count = pool.get("container_null"), pool.get("offered_container_count")
        if (candidates or type(null) is not bool or not ((null and count is None) or
                (not null and type(count) is int and count == 0))
                or pool.get("forced_empty_entry_qualified") is not True):
            raise ValueError("Forced-empty response needs the actual null/Count-zero native branch")
        return {"action": {"type": 4, "indexes": []}, "policy_source": deepcopy(_policy_source()),
            "diagnostics": {**diagnostics, "selection_mode": "forced-empty-response", "policy_choice_required": False,
                "original_min_max_preserved": True, "reason": "original-native-null-or-empty-early-return"}}
    if (pool.get("selection_mode") != "offered-card-pool" or pool.get("offered_pool_complete") is not True
            or pool.get("choices_qualified") is not True or pool.get("container_null") is not False
            or type(pool.get("offered_container_count")) is not int or pool["offered_container_count"] != len(candidates)
            or not candidates or low > len(candidates)):
        raise ValueError("A complete ordinary native offered pool and satisfiable original bounds are required")
    rows, bindings = [], []
    for ordinal, candidate in enumerate(candidates):
        if (not isinstance(candidate, Mapping) or type(candidate.get("offered_ordinal")) is not int
                or candidate["offered_ordinal"] != ordinal or candidate.get("card_data_present") is not True
                or not isinstance(candidate.get("id"), str) or not candidate["id"]):
            raise ValueError("Offered ordinal/card identity is missing or conflicts with original pool order")
        upgrade = _int(candidate.get("raw_upgrade_count"), "raw_upgrade_count", maximum=2**31 - 1)
        if (type(candidate.get("native_zone_index")) is not int or type(candidate.get("native_zone_type")) is not int
                or not isinstance(candidate.get("native_zone_name"), str)
                or candidate.get("guid") is not None and not isinstance(candidate.get("guid"), str)):
            raise ValueError("Native pool position or card identity has invalid types")
        # GUID can genuinely be null. Offered ordinal remains the response
        # identity, never native_zone_index, card ID or a generated GUID.
        rows.append({"index": ordinal, "card_id": candidate["id"], "upgrade": upgrade,
            "card_guid": candidate.get("guid"), "evaluation": None})
        bindings.append({k: candidate.get(k) for k in ("offered_ordinal", "native_zone_index", "native_zone_type",
            "native_zone_name", "id", "raw_upgrade_count", "guid", "owned_object_identity", "position_object_identity")})
    direction, direction_evidence = _direction(observation)
    desired = min(high, len(rows)) if direction in _BENEFICIAL else low
    database = database_stamp = None
    if direction == "upgrade" and desired:
        database, database_stamp = _database(source_database_reference, observation.get("source_master_hash"))
    ranked = sorted((_rank(row, direction, database) for row in rows), key=lambda item: item[0], reverse=True) if desired else []
    if database is not None and _stamp(database) != database_stamp:
        raise ValueError("Source Master database changed during secondary ranking")
    selected = [evidence["index"] for _, evidence in ranked[:desired]]
    if len(selected) != desired or len(set(selected)) != len(selected) or not low <= len(selected) <= high:
        raise ValueError("Existing secondary rule did not produce a valid native response")
    return {"action": {"type": 4, "indexes": selected}, "policy_source": deepcopy(_policy_source()),
        "diagnostics": {**diagnostics, "selection_mode": "offered-card-pool", "policy_choice_required": True,
            "direction": direction, "direction_evidence": direction_evidence, "desired_count": desired,
            "rankings": [evidence for _, evidence in ranked], "selected_bindings": [bindings[i] for i in selected],
            "source_database_reference": dict(source_database_reference) if database is not None else None,
            "selection_rule": "existing-direction-then-desired-count-then-rank; ordered offered ordinals"}}


__all__ = ["SCHEMA", "NATIVE_METADATA_SHA256", "decide_native_secondary"]
