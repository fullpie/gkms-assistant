"""Public inference projections of existing count priors and deck targets.

No observations, account identifiers, replay actions or source paths are loaded
by these readers. Numerical ranking remains in the original policy classes.
The outer package manifest authenticates each file before these readers run.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re


COUNT_SCHEMA = "gkms.public-behavior-counts.v1"
DECK_SCHEMA = "gkms.public-deck-plan-library.v1"
PROJECTION_SCHEMA = "gkms.public-deck-reference.v1"


class RequiredPortableBehaviorAssetError(RuntimeError):
    """Configured public inference data is unusable; optional fallback is unsafe."""


def load_public_behavior_role(role, reader, **kwargs):
    from .portable_outer_assets import role_path

    try:
        path = role_path(role)
        if path is None:
            return None  # Development without a public asset package only.
        value = reader(path, **kwargs)
        if value is None:
            raise ValueError("public inference reader returned no data")
        return value
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        # Legacy consumers intentionally abstain on ValueError/OSError for an
        # optional local corpus. A configured public package is required and
        # must stop before switching the active policy to an unreported fallback.
        raise RequiredPortableBehaviorAssetError(f"required-public-{role}-unavailable:{error}") from error


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _freeze(value):
    return tuple(_freeze(v) for v in value) if isinstance(value, list) else value


def _integer(value):
    if type(value) is not int or value < 0:
        raise ValueError("public count must be a nonnegative integer")
    return value


def _is_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _mapping_rows(mapping, encode=lambda v: v):
    return [[key, encode(value)] for key, value in sorted(mapping.items())]


def project_behavior_prior(prior, *, source_reference):
    """Return a public projection plus a PRIVATE ordinal/source mapping.

    Broad rank unions source units across matching signatures. Anonymous unit
    ordinals preserve that overlap; a count per cell would change the policy.
    """
    from .nia_training_dataset import NiaHierarchicalCandidateBehaviorPrior

    if not isinstance(prior, NiaHierarchicalCandidateBehaviorPrior):
        raise TypeError("expected the existing hierarchical count prior")
    broad = prior.broad_prior
    units = sorted({unit for values in (() if broad is None else broad.source_action_counts.values()) for unit in values})
    ordinal = {unit: index for index, unit in enumerate(units)}
    private_mapping = [[index, list(unit)] for unit, index in ordinal.items()]

    def project(part):
        if part is None:
            return None
        result = {"schema": part.schema, "observation_count": part.observation_count,
            "source_count": part.source_count,
            **{name: _mapping_rows(getattr(part, name)) for name in
                ("scope_source_counts", "decision_scope_source_counts", "counts")},
            "source_kinds_by_key": _mapping_rows(part.source_kinds_by_key, lambda v: sorted(v))}
        if hasattr(part, "source_action_counts"):
            result.update(scope_idol_card_counts=_mapping_rows(part.scope_idol_card_counts),
                source_action_counts=_mapping_rows(part.source_action_counts,
                    lambda v: sorted(ordinal[unit] for unit in v)))
        return result

    return {"schema": COUNT_SCHEMA, "prior_schema": prior.schema,
        "source_reference": dict(source_reference), "unit_count": len(units),
        "private_source_mapping_sha256": _hash(private_mapping),
        "exact": project(prior.exact_prior), "broad": project(broad),
        "training_performed": False, "raw_observations_included": False}, private_mapping


def read_behavior_prior(path):
    from .nia_training_dataset import (NiaExactCandidateBehaviorPrior,
        NiaBroadCandidateBehaviorPrior, NiaHierarchicalCandidateBehaviorPrior)

    data = json.loads(Path(path).read_bytes())
    if data.get("schema") != COUNT_SCHEMA or data.get("raw_observations_included") is not False:
        raise ValueError("public behavior projection schema differs")
    units = _integer(data["unit_count"])

    def read_rows(rows, length, transform):
        result = {}
        for key, value in rows:
            key = _freeze(key)
            if not isinstance(key, tuple) or len(key) != length or key in result:
                raise ValueError("invalid or duplicate public prior key")
            # Keys are immutable scope/candidate signatures, never arbitrary
            # dictionaries or observations. Validate them before ranking.
            if length in (8, 10):
                if key[0] not in ("outer_action", "reward_card"):
                    raise ValueError("unknown count decision type")
                if any(type(x) is not int or x < 0 for x in key[-4:-2]):
                    raise ValueError("invalid count week/phase")
                if not isinstance(key[-2], tuple) or not key[-2] or any(not isinstance(x, str) for x in key[-2]):
                    raise ValueError("invalid candidate signature")
                if key[-1] not in key[-2]:
                    raise ValueError("count choice outside signature")
                text = (*key[:-4], key[-1])
            else:
                text = key
            if any(not isinstance(x, str) or not x for x in text):
                raise ValueError("invalid public scope key")
            result[key] = transform(value)
        return result

    def kinds(values):
        if not isinstance(values, list) or any(v not in ("leaderboard_outer", "accepted_live") for v in values):
            raise ValueError("unknown source family")
        return frozenset(values)

    def source_units(values):
        if not isinstance(values, list) or len(values) != len(set(values)):
            raise ValueError("invalid source-unit ordinals")
        if any(type(v) is not int or not 0 <= v < units for v in values):
            raise ValueError("source-unit ordinal outside projection")
        return frozenset(("anonymous-unit", str(v)) for v in values)

    def part(row, broad):
        if row is None and broad:
            return None
        scope_n, key_n = (3, 8) if broad else (5, 10)
        kwargs = {"schema": row["schema"], "observation_count": _integer(row["observation_count"]),
            "source_count": _integer(row["source_count"]),
            "scope_source_counts": read_rows(row["scope_source_counts"], scope_n, _integer),
            "decision_scope_source_counts": read_rows(row["decision_scope_source_counts"], scope_n + 1, _integer),
            "counts": read_rows(row["counts"], key_n, _integer),
            "source_kinds_by_key": read_rows(row["source_kinds_by_key"], key_n, kinds)}
        if broad:
            kwargs.update(scope_idol_card_counts=read_rows(row["scope_idol_card_counts"], 3, _integer),
                source_action_counts=read_rows(row["source_action_counts"], 8, source_units))
        return (NiaBroadCandidateBehaviorPrior if broad else NiaExactCandidateBehaviorPrior)(**kwargs)

    return NiaHierarchicalCandidateBehaviorPrior(part(data["exact"], False), part(data["broad"], True),
        schema=data["prior_schema"])


def project_deck_library(library, *, source_reference):
    """Keep composition/milestones and original tie order, drop private context."""
    from .deck_plan import DeckPlanLibrary

    if not isinstance(library, DeckPlanLibrary):
        raise TypeError("expected the existing whole-deck reference library")
    plans, private_mapping = [], []
    for original in library.plans:
        ref = original.reference
        public = {name: ref[name] for name in ("stage", "composition_timing",
            "historical_terminal_score", "historical_rank", "step_select_number") if name in ref}
        public["versions"] = {key: value for key, value in ref.get("versions", {}).items()
            if key in ("app_version", "image_version", "master_version", "master_hash")}
        if any(not isinstance(v, str) or re.fullmatch(r"[A-Za-z0-9_.-]*", v) is None
               for v in public["versions"].values()):
            raise ValueError("unexpected historical version identity")
        public["audition_decks"] = [{name: row[name] for name in
            ("stage", "composition_timing", "target_composition")} for row in ref.get("audition_decks", ())]
        public["projection"] = {"schema": PROJECTION_SCHEMA,
            "original_reference_sha256": original.reference_fingerprint,
            "selection_tie_breaker": original.plan_id,
            "trajectory_identity_sha256": _hash(ref.get("trajectory_id")),
            "source_identity_sha256": sorted({_hash(value) for value in
                (ref.get("source_id"), ref.get("trajectory_id"), *ref.get("sources", ())) if value is not None})}
        projected = replace(original, reference_json=_json(public))
        plans.append(projected.to_dict())
        private_mapping.append({"public_plan_id": projected.plan_id, "original_plan_id": original.plan_id,
            "original_reference": ref})
    return {"schema": DECK_SCHEMA, "source_reference": dict(source_reference),
        "source_name_sha256": _hash(library.source),
        "private_reference_mapping_sha256": _hash(private_mapping), "plans": plans,
        "training_performed": False, "raw_replays_included": False}, private_mapping


def read_deck_library(path, *, context=None, excluded_trajectory_ids=(), excluded_source_ids=(), require_success=True):
    from .deck_plan import DeckPlan, DeckPlanLibrary, _scope

    data = json.loads(Path(path).read_bytes())
    if data.get("schema") != DECK_SCHEMA or data.get("raw_replays_included") is not False:
        raise ValueError("public deck projection schema differs")
    source = "public-deck-projection:" + data["source_reference"]["sha256"]
    excluded_sources = {_hash(value) for value in excluded_source_ids}
    if data["source_name_sha256"] in excluded_sources:
        return DeckPlanLibrary((), source, (("excluded-source", 1),))
    excluded_trajectories = {_hash(value) for value in excluded_trajectory_ids}
    scope = _scope(context) if context is not None else None
    plans = []
    seen = set()
    for row in data["plans"]:
        plan = DeckPlan.from_dict(row)
        ref = plan.reference
        projection = ref.get("projection", {})
        if (projection.get("schema") != PROJECTION_SCHEMA
                or set(ref) - {"stage", "composition_timing", "historical_terminal_score", "historical_rank",
                    "step_select_number", "audition_decks", "projection", "versions"}):
            raise ValueError("deck projection has unsupported/private reference fields")
        if (set(projection) != {"schema", "original_reference_sha256", "selection_tie_breaker",
                "trajectory_identity_sha256", "source_identity_sha256"}
                or not _is_sha(projection.get("trajectory_identity_sha256"))
                or not isinstance(projection.get("source_identity_sha256"), list)
                or not all(_is_sha(value) for value in projection["source_identity_sha256"])):
            raise ValueError("invalid public source hash projection")
        versions = ref.get("versions", {})
        if (not isinstance(versions, dict) or set(versions) - {"app_version", "image_version", "master_version", "master_hash"}
                or any(not isinstance(v, str) or re.fullmatch(r"[A-Za-z0-9_.-]*", v) is None for v in versions.values())):
            raise ValueError("invalid historical version identity")
        for milestone in ref.get("audition_decks", ()):
            if set(milestone) != {"stage", "composition_timing", "target_composition"}:
                raise ValueError("private or unsupported audition milestone fields")
        original = projection.get("original_reference_sha256", "")
        if (not _is_sha(original)
                or projection.get("selection_tie_breaker") != "observed-deck-v1-" + original[:24]
                or original in seen):
            raise ValueError("deck projection identity or original ordering differs")
        seen.add(original)
        if scope is not None and plan.scope != scope:
            continue
        if (projection["trajectory_identity_sha256"] in excluded_trajectories
                or set(projection["source_identity_sha256"]) & excluded_sources):
            continue
        if require_success and (ref.get("historical_rank") != 1 or ref.get("historical_terminal_score", 0) <= 0):
            continue
        plans.append(plan)
    # The default library is a successful-Final projection. It deliberately
    # cannot reconstruct excluded failed replay rows for diagnostic callers.
    return DeckPlanLibrary(tuple(plans), source)


def selection_tie_breaker(plan):
    projection = plan.reference.get("projection")
    if isinstance(projection, Mapping) and projection.get("schema") == PROJECTION_SCHEMA:
        original = projection.get("original_reference_sha256", "")
        expected = "observed-deck-v1-" + original[:24]
        if not _is_sha(original) or projection.get("selection_tie_breaker") != expected:
            raise ValueError("invalid projected deck tie identity")
        return expected
    return plan.plan_id
