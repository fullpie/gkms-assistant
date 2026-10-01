"""Bounded account loadout search scored by the frozen shared offline learner.

The old initial-stat/prior ranking supplies a diverse shortlist only. The final
ordering must come from the pinned model or its actual native rollouts. A
source-specific projector owns hypothetical exam initialization; this module
never fabricates an exam by adding numbers to an unrelated replay observation.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
from pathlib import Path
import threading
import time
from typing import Mapping

from .account_loadout import (
    InitialLoadoutRecommendation, LoadoutConstraints, LoadoutSelection,
    recommend_initial_loadouts, validate_selection,
)
from .passive_runtime import resolve_passive_runtime

METHOD = "shared-offline-iql-loadout-assistance"
REFERENCE_SCHEMA = "gkms.rl-loadout-reference.v1"
MAX_CANDIDATES = 128
_FACTORY_CACHE = OrderedDict()
_FACTORY_LOCK = threading.RLock()
RESOURCE_FIELDS = ("vocal", "dance", "visual", "vocal_growth", "dance_growth",
                   "visual_growth", "stamina", "produce_points")
_INITIAL_MODIFIERS = {
    "vocal_addition": "vocal", "dance_addition": "dance", "visual_addition": "visual",
    "max_stamina_addition": "stamina", "vocal_growth_rate_addition": "vocal_growth",
    "dance_growth_rate_addition": "dance_growth", "visual_growth_rate_addition": "visual_growth",
    "produce_point_addition": "produce_points",
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def semantic_selection_key(selection):
    """Evaluation identity ignores only the observed rental freshness deadline.

    The selection itself keeps that deadline. Legality/application must still
    validate a fresh rental; account, inventory, rental key/ID/level/plan and all
    owned card identities remain part of this score-cache key.
    """
    value = asdict(selection)
    value["borrowed_support"].pop("expires_at")
    return _digest(value)


class LoadoutRecommendationUnavailable(ValueError):
    """Expected, visible abstention; callers retain manual/legacy choices."""


class LoadoutRecommendationCancelled(Exception):
    """Requested stop observed between fully closed candidate evaluations."""
    def __init__(self, report):
        self.report = report
        super().__init__("Loadout recommendation stopped after the current trial completed")


@dataclass(frozen=True)
class LoadoutEffectProjection:
    initial_resources: Mapping[str, float]
    persistent_modifiers: Mapping[str, float]
    memory_cards: tuple[dict, ...]
    sources: tuple[dict, ...]
    limitations: tuple[str, ...]

    @property
    def identity(self):
        return _digest(asdict(self))


def resolve_loadout_effects(snapshot, loadout, selection, catalog, *, now=None):
    """Reuse the existing passive runtime and actual account-level getters.

    Partial resolution is diagnostic for an estimator, never authority to apply
    incomplete passives to the live game. Unsupported rules remain explicit and
    are handed to the source-specific outer/inner evaluator for further handling.
    """
    validate_selection(snapshot, selection, now=now)
    initial = dict.fromkeys(RESOURCE_FIELDS, 0.0)
    persistent, sources, gaps = {}, [], []
    if loadout.get("account_scope", snapshot.account_scope) != snapshot.account_scope:
        raise LoadoutRecommendationUnavailable("RL_LOADOUT_ACCOUNT_CHANGED")

    def consume(label, passives, observed=None):
        runtime = resolve_passive_runtime(tuple(passives))
        observed_values = {} if observed is None else asdict(observed)
        for key, value in observed_values.items():
            initial[key] += value
        for key, value in runtime.modifiers.as_dict().items():
            resource = _INITIAL_MODIFIERS.get(key)
            if resource:
                if resource not in observed_values:
                    # Actual support getters and Master GrowthRateAddition
                    # agree in permil (e.g. s_card-1-0004 L1: both Visual12).
                    # Do not multiply these already resolved values again.
                    initial[resource] += value
            else:
                persistent[key] = persistent.get(key, 0.0) + value
        gaps.extend(f"{label}:{block.reason}" for block in runtime.blocking_rules)
        if runtime.status_enchant_installs:
            gaps.append(f"{label}:exam-status-enchants-require-scenario-installation")
        if runtime.chance_contracts:
            gaps.append(f"{label}:probabilistic-passives-require-scenario-outcomes")
        sources.append({"owner": label, "observed_parameters": observed_values,
            "passives": [asdict(source) for source in passives],
            "status_enchant_installs": [asdict(item) for item in runtime.status_enchant_installs],
            "chance_contracts": [asdict(item) for item in runtime.chance_contracts]})

    idol = next(row for row in snapshot.idol_cards if row.card_id == selection.idol_card_id)
    if idol.produce_parameters is None:
        raise LoadoutRecommendationUnavailable("RL_LOADOUT_IDOL_PARAMETERS_NOT_OBSERVED")
    for key, value in asdict(idol.produce_parameters).items():
        initial[key] += value
    supports = {row.card_id: row for row in snapshot.support_cards}
    for key in selection.support_card_ids:
        row = supports[key]
        consume("support:" + key, catalog.resolve_support_card(key, row.level), row.produce_parameters)
    rental = selection.borrowed_support
    raw_rentals = [row for row in loadout.get("rental_support_cards", ())
        if row.get("rental_key") == rental.rental_key and row.get("card_id") == rental.card_id
        and row.get("level") == rental.level]
    if len(raw_rentals) != 1:
        raise LoadoutRecommendationUnavailable("RL_LOADOUT_RENTAL_CHANGED")
    from .account_inventory import ProduceParameters
    consume("rental:" + rental.rental_key, catalog.resolve_support_card(rental.card_id, rental.level),
        ProduceParameters.from_dict(raw_rentals[0].get("produce_parameters")))
    memories = {row.memory_id: row for row in snapshot.memories}
    selected_memories = [memories[key] for key in selection.memory_ids]
    unique, ordinary = {}, []
    for memory in selected_memories:
        for key, level in memory.abilities:
            try:
                source = catalog.resolve_memory_ability(key, level)
            except (KeyError, ValueError) as error:
                gaps.append(f"memory:{memory.memory_id}:{type(error).__name__}:{error}")
                continue
            if source.raw.get("source", {}).get("isUniqueActivation") is True:
                if source.source_id not in unique or unique[source.source_id].source_level < source.source_level:
                    unique[source.source_id] = source
            else:
                ordinary.append(source)
    consume("memory-abilities", (*ordinary, *unique.values()))
    return LoadoutEffectProjection(initial, persistent,
        tuple(memory.to_dict() for memory in selected_memories), tuple(sources), tuple(dict.fromkeys(gaps)))


@dataclass(frozen=True)
class LoadoutModelEvaluation:
    score: float
    score_kind: str
    checkpoint_sha256: str
    context_ids: tuple[str, ...]
    limitations: tuple[str, ...]
    evidence: Mapping
    elapsed_seconds: float
    cache_hit: bool = False


class SharedLoadoutEvaluator:
    """Reuse one model, a bounded LRU and the source-specific scenario runner.

    ``projector(snapshot, selection, effect_projection)`` returns either exact
    native scores under this actor or fully projected hypothetical exam States.
    It also reports context IDs, scope and limitations. No old-rule fallback is
    accepted from this boundary. Native callbacks and game input stay elsewhere.
    """

    def __init__(self, *, checkpoint_sha256, projector, model=None, schema=None,
                 source_identity, cache_size=256, validate_unchanged=None):
        if (not isinstance(checkpoint_sha256, str) or len(checkpoint_sha256) != 64
                or any(c not in "0123456789abcdef" for c in checkpoint_sha256)
                or not 1 <= cache_size <= 4096 or not source_identity):
            raise ValueError("bounded evaluator and pinned source/model identity required")
        self.checkpoint_sha256, self.projector = checkpoint_sha256, projector
        self.model, self.schema, self.source_identity = model, schema, source_identity
        self.cache_size, self._cache = cache_size, OrderedDict()
        self.validate_unchanged = validate_unchanged or getattr(projector, "validate_unchanged", None)
        self._lock = threading.RLock()
        self.last_report = {}

    def evaluate(self, snapshot, selection, effects):
        key = _digest({"model": self.checkpoint_sha256, "source": self.source_identity,
            "selection": semantic_selection_key(selection), "effects": effects.identity})
        with self._lock:
            if self.validate_unchanged is not None:
                self.validate_unchanged()
            if key in self._cache:
                value = self._cache.pop(key); self._cache[key] = value
                return LoadoutModelEvaluation(**{**asdict(value), "cache_hit": True})
            started = time.monotonic()
            projected = self.projector(snapshot, selection, effects)
            if (not isinstance(projected, Mapping) or projected.get("checkpoint_sha256") != self.checkpoint_sha256
                    or projected.get("produce_id") != selection.produce_id
                    or projected.get("idol_card_id") != selection.idol_card_id
                    or projected.get("legacy_rule_fallback") is not False):
                raise LoadoutRecommendationUnavailable("RL_LOADOUT_PROJECTOR_SCOPE_OR_MODEL_MISMATCH")
            contexts = projected.get("context_ids")
            if (not isinstance(contexts, (list, tuple)) or not 1 <= len(contexts) <= 64
                    or any(not isinstance(value, str) or not value for value in contexts)
                    or len(set(contexts)) != len(contexts)):
                raise LoadoutRecommendationUnavailable("RL_LOADOUT_PAIRED_CONTEXTS_MISSING")
            if "native_scores" in projected:
                if projected.get("complete") is not True or projected.get("policy_source") != "shared-offline-IQL":
                    raise LoadoutRecommendationUnavailable("RL_LOADOUT_NATIVE_ROLLOUT_INCOMPLETE")
                values = projected["native_scores"]
                score_kind = "native-policy-score"
            else:
                if self.model is None or self.schema is None:
                    raise LoadoutRecommendationUnavailable("RL_LOADOUT_VALUE_MODEL_UNAVAILABLE")
                from .rl.features import encode_batch
                import torch
                states = projected.get("states", ())
                if len(states) != len(contexts) or not states:
                    raise LoadoutRecommendationUnavailable("RL_LOADOUT_PROJECTED_CONTEXTS_MISSING")
                with torch.inference_mode():
                    values = self.model.state_value(encode_batch(states, self.schema)).tolist()
                score_kind = "remaining-return-value-proxy"
            if (not isinstance(values, (list, tuple)) or len(values) != len(contexts)
                    or any(type(value) not in (int, float) or not math.isfinite(value) for value in values)):
                raise LoadoutRecommendationUnavailable("RL_LOADOUT_NONFINITE_OR_INCOMPLETE_VALUES")
            gaps = tuple(dict.fromkeys((*effects.limitations, *projected.get("limitations", ()))))
            value = LoadoutModelEvaluation(sum(values) / len(values), score_kind, self.checkpoint_sha256,
                tuple(contexts), gaps, {**projected.get("evidence", {}), "per_context_values": list(values),
                    "source_identity": self.source_identity, "effect_projection_sha256": effects.identity,
                    "model_used": True, "score_improvement_verified": False}, time.monotonic() - started)
            if self.validate_unchanged is not None:
                self.validate_unchanged()
            self._cache[key] = value
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
            return value


_RENTAL_OBSERVED_FIELDS = frozenset({"rental_key", "card_id", "level", "plan_type",
    "produce_parameters", "expires_at", "availability_source"})
_RENTAL_PARAMETER_FIELDS = frozenset(RESOURCE_FIELDS) - {"produce_points"}


def _rental_material_key(row):
    """Only the currently observed, complete DLL rental schema is equivalent.

    A new effect field, absent getter, partial parameter map or malformed value
    is NOT evidence of equivalence. Such offers remain separate candidates.
    """
    if (not isinstance(row, Mapping) or set(row) - _RENTAL_OBSERVED_FIELDS
            or any(not isinstance(row.get(key), str) or not row[key]
                for key in ("rental_key", "card_id", "plan_type"))
            or type(row.get("level")) is not int or row["level"] < 1):
        return None
    values = row.get("produce_parameters")
    if (not isinstance(values, Mapping) or set(values) != _RENTAL_PARAMETER_FIELDS
            or any(type(value) is not int or value < 0 for value in values.values())):
        return None
    return _digest({"card_id": row["card_id"], "level": row["level"], "plan_type": row["plan_type"],
        "produce_parameters": dict(values)})


def _deduplicate_rentals(loadout, constraints, now):
    """Filter a COPY before the old top-three rental shortlist is constructed."""
    rows = list(loadout.get("rental_support_cards", ()))
    groups, retained, unknown = {}, [], 0
    known_schema = loadout.get("schema") == "gkms.account-loadout.v1"
    identity_counts = {}
    for row in rows:
        if isinstance(row, Mapping) and isinstance(row.get("rental_key"), str):
            key = row.get("rental_key")
            identity_counts[key] = identity_counts.get(key, 0) + 1
    for index, row in enumerate(rows):
        key = (_rental_material_key(row) if known_schema and isinstance(row, Mapping)
            and isinstance(row.get("rental_key"), str)
            and identity_counts.get(row.get("rental_key")) == 1 else None)
        if key is None:
            retained.append(index); unknown += 1
        else:
            groups.setdefault(key, []).append(index)
    def available(index):
        try:
            value = rows[index].get("expires_at")
            expires = (datetime.fromisoformat(value.replace("Z", "+00:00")) if value is not None else
                datetime.fromisoformat(str(loadout["captured_at"]).replace("Z", "+00:00")) + timedelta(minutes=5))
            return expires.tzinfo is not None and expires > now
        except (TypeError, ValueError, KeyError, AttributeError):
            return False
    for indexes in groups.values():
        locked = [index for index in indexes if rows[index]["rental_key"] == constraints.locked_rental_key]
        # A locked expired offer must fail ordinary availability validation;
        # it must never be silently replaced by another lender of that card.
        chosen = locked[0] if locked else min(indexes,
            key=lambda index: (not available(index), rows[index]["rental_key"], index))
        retained.append(chosen)
    output = {**loadout, "rental_support_cards": [rows[index] for index in sorted(retained)]}
    return output, {"observed_offers": len(rows), "retained_offers": len(retained),
        "equivalent_offers_removed": len(rows) - len(retained), "unresolved_offers_retained": unknown,
        "compared_fields": ["card_id", "level", "plan_type", "complete_actual_produce_parameters"],
        "locked_rental_substituted": False, "availability_renewed": False}


def _material_indexes(snapshot, loadout):
    if snapshot is None or loadout is None:
        return None
    supports = {row.card_id: _digest(asdict(row)) for row in snapshot.support_cards}
    memories = {}
    for memory in snapshot.memories:
        value = memory.to_dict(); value.pop("memory_id")
        memories[memory.memory_id] = _digest(value)
    rental_rows = {}
    for row in loadout.get("rental_support_cards", ()):
        if isinstance(row, Mapping):
            rental_rows.setdefault(row.get("rental_key"), []).append(row)
    known_schema = loadout.get("schema") == "gkms.account-loadout.v1"
    rentals = {key: _rental_material_key(rows[0]) if len(rows) == 1 and known_schema else None
        for key, rows in rental_rows.items()}
    return supports, memories, rentals


def _loadout_material_keys(selection, indexes):
    if indexes is None:
        # This path is for callers without observations, never an equivalence
        # proof for different lenders. IDs alone only establish real changes.
        return selection.support_card_ids, selection.memory_ids, None
    supports, memories, rentals = indexes
    owned = tuple(supports[key] for key in selection.support_card_ids)
    memory = tuple(memories[key] for key in selection.memory_ids)
    return owned, memory, rentals.get(selection.borrowed_support.rental_key)


def _diverse_shortlist(rows, budget, *, snapshot=None, loadout=None):
    """Reserve one observed support change and one memory change when possible."""
    if not rows:
        return []
    indexes = _material_indexes(snapshot, loadout)
    keys = {id(row): _loadout_material_keys(row.selection, indexes) for row in rows}
    first = rows[0]; base_owned, base_memory, base_rental = keys[id(first)]
    selected, seen = [first], {semantic_selection_key(first.selection)}
    def add(row):
        identity = semantic_selection_key(row.selection)
        if identity not in seen and len(selected) < budget:
            selected.append(row); seen.add(identity)
    support = next((row for row in rows if keys[id(row)][0] != base_owned
        or (base_rental is not None and keys[id(row)][2] is not None
            and keys[id(row)][2] != base_rental)), None)
    memory = next((row for row in rows if keys[id(row)][1] != base_memory), None)
    if support is not None:
        add(support)
    if memory is not None:
        add(memory)
    for row in rows:
        add(row)
        if len(selected) == budget:
            break
    return selected


def _variant_coverage(results, snapshot, loadout):
    indexes = _material_indexes(snapshot, loadout)
    material = [_loadout_material_keys(row.selection, indexes) for row in results]
    return {"support_variants_evaluated": len({(owned, rental) for owned, _, rental in material if rental is not None}),
        "support_identity_variants_evaluated": len({(row.selection.support_card_ids,
            row.selection.borrowed_support.rental_key) for row in results}),
        "unresolved_rental_variants_evaluated": len({row.selection.borrowed_support.rental_key
            for row, (_, _, rental) in zip(results, material) if rental is None}),
        "support_material_equivalence_complete": all(rental is not None for _, _, rental in material),
        "memory_variants_evaluated": len({memory for _, memory, _ in material}),
        "memory_instance_variants_evaluated": len({row.selection.memory_ids for row in results}),
        "coverage_basis": "observed-resource-materials; different lender keys alone are not effect variants"}


def _candidate_budget(value):
    if type(value) is not int or not 3 <= value <= MAX_CANDIDATES:
        raise ValueError("RL loadout search requires 3..128 candidates to reserve both component families")
    return value
def recommend_rl_loadouts(snapshot, loadout, *, constraints=LoadoutConstraints(),
        idol_card_id=None, produce_id=None, catalog=None, evaluator=None, descriptor=None,
        database=None, limit=3, budget=None, progress_callback=None, cancelled=None):
    started = time.monotonic()
    if progress_callback is not None:
        progress_callback({"phase":"preparing","processed":0,"total":None,"succeeded":0,"failed":0,
            "cached_candidates":0,"elapsed_seconds":0.})
    if budget is not None:
        _candidate_budget(budget)
    if type(limit) is not int or not 1 <= limit <= MAX_CANDIDATES:
        raise ValueError("RL loadout result count must be bounded")
    # All hypothetical comparisons share the real observed availability at
    # search start. Long offline rollouts do not invalidate successful scores.
    # Returned selections keep their ORIGINAL deadline; live application must
    # obtain a fresh exact-identity observation and validate real current time.
    search_started_at = datetime.now(timezone.utc)
    if evaluator is None:
        evaluator = build_private_loadout_evaluator(descriptor)
    budget = _candidate_budget(getattr(evaluator, "candidate_budget", 3) if budget is None else budget)
    if limit > budget:
        raise ValueError("RL loadout result count exceeds search budget")
    search_loadout, rental_deduplication = _deduplicate_rentals(loadout, constraints, search_started_at)
    if catalog is None:
        from .passive_catalog import MasterPassiveCatalog
        catalog = MasterPassiveCatalog.load()
    candidates = recommend_initial_loadouts(snapshot, search_loadout, constraints=constraints,
        idol_card_id=idol_card_id, produce_id=produce_id, catalog=catalog,
        database=database, limit=1728, now=search_started_at, use_memory_prior=False)
    candidates = _diverse_shortlist(candidates, budget, snapshot=snapshot, loadout=search_loadout)
    for row in candidates:
        validate_selection(snapshot, row.selection, constraints=constraints, now=search_started_at)
    results, failures, paired_contexts, score_kind, details = [], [], None, None, []
    cached_candidates = 0
    def emit(phase, current=None):
        if progress_callback is not None:
            progress_callback({"phase":phase,"processed":len(results)+len(failures),"total":len(candidates),
                "succeeded":len(results),"failed":len(failures),"cached_candidates":cached_candidates,
                "current_candidate":current,"elapsed_seconds":time.monotonic()-started})
    def publish(status):
        evaluator.last_report = {"schema": "gkms.rl-loadout-search.v1", "checkpoint_sha256": evaluator.checkpoint_sha256,
            "inventory_digest": snapshot.content_digest, "candidates": len(candidates), "completed": len(results),
            "offline_availability_as_of_utc": search_started_at.isoformat(),
            "rental_availability_renewed": False, "candidate_budget": budget,
            "rental_deduplication": rental_deduplication,
            "evaluations": details,
            "failed": failures, "elapsed_seconds": time.monotonic() - started, "score_kind": score_kind,
            "context_ids": paired_contexts, "support_and_memory_model_ranked": bool(results),
            **_variant_coverage(results, snapshot, loadout),
            "score_improvement_verified": False, "native_full_cultivation_verified": False}
        evaluator.last_report["status"] = status
        evaluator.last_report["cached_candidates"] = cached_candidates
    def stop_if_requested():
        if cancelled is not None and cancelled():
            publish("cancelled");emit("cancelled")
            raise LoadoutRecommendationCancelled(evaluator.last_report)
    emit("evaluating")
    for row in candidates:
        stop_if_requested()
        emit("evaluating",len(results)+len(failures)+1)
        selection = row.selection
        try:
            effects = resolve_loadout_effects(snapshot, loadout, selection, catalog, now=search_started_at)
            evaluation = evaluator.evaluate(snapshot, selection, effects)
            if paired_contexts is None:
                paired_contexts, score_kind = evaluation.context_ids, evaluation.score_kind
            if paired_contexts != evaluation.context_ids or score_kind != evaluation.score_kind:
                raise LoadoutRecommendationUnavailable("RL_LOADOUT_COMPARISON_CONDITIONS_DIFFER")
        except (KeyError, ValueError) as error:
            failures.append({"selection_sha256": _digest(asdict(selection)), "reason": str(error)})
            emit("evaluating")
            continue
        reasons = (
            "規則只篩選候選；支援與回憶組合由同一份固定 IQL 模型評估排序。",
            ("按固定 RL 策略在原生模擬器的實際結算分數排序；培育收益使用明列的估算情境。"
                if evaluation.score_kind == "native-policy-score" else
                "使用模型剩餘回報 Value 估計排序；不是原生演出實際得分。"),
            "模型 " + evaluation.checkpoint_sha256[:12] + "；" + evaluation.score_kind,
            "配對情境 " + str(len(evaluation.context_ids)) + "；未宣稱分數或通關優於舊推薦。",
            "使用帳號持有卡、實際等級、觀測能力；保留鎖定與借卡限制。",
            *("尚未完整估計：" + reason for reason in evaluation.limitations[:8]),
            *((f"另有 {len(evaluation.limitations) - 8} 項限制，完整明細保存在推薦診斷。",)
                if len(evaluation.limitations) > 8 else ()),
        )
        results.append(InitialLoadoutRecommendation(selection, evaluation.score, reasons, METHOD))
        details.append({"selection_sha256": _digest(asdict(selection)), **asdict(evaluation)})
        native_cases=evaluation.evidence.get("cases", ())
        cached_candidates += int(evaluation.cache_hit or bool(native_cases) and all(row.get("cache_hit") is True for row in native_cases))
        emit("evaluating")
    results.sort(key=lambda row: (-row.ranking_score, row.selection.support_card_ids,
        row.selection.memory_ids, row.selection.borrowed_support.rental_key))
    publish("completed" if results else "failed")
    emit("complete" if results else "failed")
    if not results:
        first = failures[0]["reason"] if failures else "no legal candidates"
        raise LoadoutRecommendationUnavailable("RL_LOADOUT_NO_EVALUABLE_COMBINATIONS: " + first)
    return tuple(results[:limit])


def build_private_loadout_evaluator(descriptor):
    """Construct the existing native scenario adapter; never invent contexts."""
    if not isinstance(descriptor, Mapping):
        raise LoadoutRecommendationUnavailable("RL_LOADOUT_MODEL_DESCRIPTOR_UNAVAILABLE")
    reference = descriptor.get("loadout_reference")
    if not isinstance(reference, Mapping) or not {"path", "sha256"} <= reference.keys():
        raise LoadoutRecommendationUnavailable("RL_LOADOUT_REFERENCE_UNAVAILABLE")
    path = Path(reference["path"])
    payload = path.read_bytes()
    if len(payload) > 64 * 1024 * 1024 or hashlib.sha256(payload).hexdigest() != reference["sha256"]:
        raise LoadoutRecommendationUnavailable("RL_LOADOUT_REFERENCE_CHANGED")
    document = json.loads(payload)
    if document.get("schema") != REFERENCE_SCHEMA:
        raise LoadoutRecommendationUnavailable("RL_LOADOUT_REFERENCE_SCHEMA_UNSUPPORTED")
    # This adapter is deliberately source-specific. It reuses the existing
    # isolated native executor, and does not acquire the live game input owner.
    key = _digest({"descriptor": dict(descriptor), "reference": reference["sha256"]})
    with _FACTORY_LOCK:
        if key in _FACTORY_CACHE:
            evaluator = _FACTORY_CACHE.pop(key)
            if evaluator.validate_unchanged is not None:
                evaluator.validate_unchanged()
            _FACTORY_CACHE[key] = evaluator
            return evaluator
        from .rl_loadout_native import build_loadout_projector
        evaluator = build_loadout_projector(document, descriptor, reference_identity=reference["sha256"])
        if not isinstance(evaluator, SharedLoadoutEvaluator):
            raise LoadoutRecommendationUnavailable("RL_LOADOUT_NATIVE_EVALUATOR_CONTRACT_DIFFERS")
        evaluator.candidate_budget = _candidate_budget(document.get("candidate_budget", 3))
        _FACTORY_CACHE[key] = evaluator
        while len(_FACTORY_CACHE) > 2:
            _FACTORY_CACHE.popitem(last=False)
        return evaluator


def build_private_initial_loadout_selector(descriptor):
    """Distinct learned composition policy; never impersonate native/value scores."""
    from .private_initial_loadout import build_selector
    return build_selector(descriptor)
