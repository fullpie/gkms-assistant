"""Public aggregate memory-loadout prior; original scoring classes are reused."""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path

SCHEMA = "gkms.public-memory-loadout-prior.v1"
PART_FIELDS = ("schema", "source_sha256", "trajectory_count", "flow", "requested_idol_card_id", "scope_kind",
               "statistics", "exact_statistics", "combinations", "exact_combinations", "pair_statistics",
               "role_target_counts", "deck_role_target_counts", "maximum_score", "minimum_exact_support")
CARD_FIELDS = ("card_id", "trajectory_support", "copy_count", "mean_copy_count", "upgraded_copy_count",
               "mean_terminal_score", "score_signal", "utility", "role", "phase_counts", "ability_support")
COMBINATION_FIELDS = ("card_ids", "trajectory_support", "mean_terminal_score", "score_signal", "utility")


def project_memory_prior(payload, *, source_reference):
    from .leaderboard_memory_loadout_prior import ARTIFACT_SCHEMA
    if payload.get("schema") != ARTIFACT_SCHEMA or not isinstance(payload.get("scopes"), dict):
        raise ValueError("Existing memory prior artifact schema differs")
    private = {"source_path": payload.get("source_path"), "parts": {}}

    def part(value, identity):
        result = {key: deepcopy(value[key]) for key in PART_FIELDS}
        private["parts"][identity] = {"source_path": value.get("source_path")}
        for family in ("statistics", "exact_statistics", "combinations", "exact_combinations", "pair_statistics"):
            keys = CARD_FIELDS if family in ("statistics", "exact_statistics") else COMBINATION_FIELDS
            result[family] = {key: {name: deepcopy(row[name]) for name in keys} for key, row in value[family].items()}
        return result

    scopes = {}
    for scope, value in payload["scopes"].items():
        scopes[scope] = {"broad": part(value["broad"], scope + "|broad"),
            "exact_by_idol": {idol: part(prior, scope + "|" + idol) for idol, prior in value["exact_by_idol"].items()}}
    return {"schema": SCHEMA, "source_reference": dict(source_reference), "source_sha256": payload.get("source_sha256"),
            "scopes": scopes, "raw_observations_included": False, "training_performed": False}, private


@lru_cache(maxsize=2)
def _read_cached(path_text, stamp):
    path = Path(path_text)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024**2:
        raise ValueError("Public memory prior is unavailable or oversized")
    raw = path.read_bytes()
    after = path.stat()
    if stamp != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError("Public memory prior changed during read")
    payload = json.loads(raw)
    allowed = {"schema", "source_reference", "source_sha256", "scopes", "raw_observations_included", "training_performed"}
    if (set(payload) != allowed or payload["schema"] != SCHEMA or payload["raw_observations_included"] is not False
            or payload["training_performed"] is not False or not isinstance(payload["scopes"], dict)):
        raise ValueError("Public memory prior projection schema differs")
    return payload


def prior_from_projection(payload, *, produce_id, plan_type, exam_effect_type, idol_card_id=""):
    from .leaderboard_memory_loadout_prior import _load_prior_from_artifact_payload, SCHEMA as PART_SCHEMA
    scope = "|".join((produce_id, plan_type, exam_effect_type))
    family = payload["scopes"].get(scope)
    if not isinstance(family, dict) or set(family) != {"broad", "exact_by_idol"}:
        raise ValueError("Required public memory prior has no matching flow scope")
    selected = family["exact_by_idol"].get(idol_card_id, family["broad"]) if idol_card_id else family["broad"]
    if not isinstance(selected, dict) or set(selected) != set(PART_FIELDS) or selected["schema"] != PART_SCHEMA:
        raise ValueError("Public memory prior has unrecognized/private fields")
    for name in ("statistics", "exact_statistics", "combinations", "exact_combinations", "pair_statistics"):
        allowed = CARD_FIELDS if name in ("statistics", "exact_statistics") else COMBINATION_FIELDS
        if not isinstance(selected[name], dict) or any(not isinstance(row, dict) or set(row) != set(allowed) for row in selected[name].values()):
            raise ValueError("Public memory prior statistics fields differ")
    prior = _load_prior_from_artifact_payload(selected)
    if not prior.applies_to(produce_id=produce_id, plan_type=plan_type, exam_effect_type=exam_effect_type, idol_card_id=idol_card_id):
        raise ValueError("Public memory prior selected scope differs")
    return prior


def read_memory_loadout_prior(path, **scope):
    path = Path(path).resolve()
    info = path.stat()
    return prior_from_projection(_read_cached(str(path), (info.st_size, info.st_mtime_ns, info.st_ctime_ns)), **scope)
