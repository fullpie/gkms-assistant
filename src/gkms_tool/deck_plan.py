"""Observed whole-deck targets, independent of input ownership and score models.

Final-start decks select a reference trajectory; its observed earlier audition
decks provide optional milestones. They are never weekly before-state or
predicted exam scores. Persist one plan and compare alternatives against it.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path

from .deck_value import card_customizations
from .leaderboard_card_prior import DEFAULT_LEADERBOARD_EPISODES
from .leaderboard_replay import LeaderboardReplayEpisode
from .leaderboard_replay_adapter import read_leaderboard_episode


SCHEMA = "gkms.observed-deck-plan.v1"
_SCOPE = ("produce_id", "plan_type", "exam_effect_type")
_AUDITION_STAGES = {16: "Mid1", 17: "Mid2", 18: "Final", **{
    "ProduceStepType_Audition" + stage: stage for stage in ("Mid1", "Mid2", "Final")}}
_LIMITATIONS = ("structural-target-progress-not-exam-score", "functional-substitution-not-evaluated",
               "acquisition-and-loadout-reachability-not-evaluated", "extra-customizations-not-assumed-equivalent")


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _text(value, name):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is missing")
    return value


def _int(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _scope(context):
    return tuple(_text(context.get(name), name) for name in _SCOPE)


@dataclass(frozen=True, order=True)
class DeckCard:
    card_id: str
    upgrade_count: int = 0
    customizes: tuple[tuple[str, int], ...] = ()

    def to_dict(self):
        return {"card_id": self.card_id, "upgrade_count": self.upgrade_count,
                "customizes": [{"id": key, "customizeCount": count} for key, count in self.customizes]}


def _cards(deck):
    """Read actual native rows or episode composition rows; ignore cosmetic skin.

    One row means one copy unless an explicit aggregate ``count`` is supplied.
    Omitted upgrade/customize counts retain the existing protobuf-zero contract.
    Temporary in-exam upgrades must not be supplied as permanent upgradeCount.
    """
    if not isinstance(deck, (list, tuple)):
        raise ValueError("complete deck must be an array")
    result = []
    for row in deck:
        if not isinstance(row, Mapping):
            raise ValueError("deck contains a non-object row")
        if type(row.get("deleted", False)) is not bool:
            raise ValueError("card deleted flag must be boolean")
        if row.get("deleted", False):
            continue
        identity = row.get("produceCardId", row.get("card_id", row.get("id")))
        upgrade = row.get("upgradeCount", row.get("upgrade_count", row.get("upgrade", 0)))
        card = DeckCard(_text(identity, "card_id"), _int(upgrade, "upgrade count"),
                        tuple(sorted(card_customizations(row))))
        count = _int(row.get("count", 1), "copy count")
        if count == 0:
            raise ValueError("copy count must be positive")
        result.extend([card] * count)
    return tuple(sorted(result))


@dataclass(frozen=True)
class DeckPlanObservedRef:
    row_index: int | None = None
    copy_index: int | None = None
    number: int | None = None

    def to_dict(self):
        return {"row_index": self.row_index, "copy_index": self.copy_index, "number": self.number}


def _cards_with_refs(deck):
    """Keep the existing composition parser and carry identity beside it.

    Number is optional planning lineage, never a synthesized row/target index.
    Aggregate copies, proposed cards, malformed/conflicting/duplicate Numbers
    remain usable as compositions but cannot acquire a native Number binding.
    """
    if not isinstance(deck, (list, tuple)):
        raise ValueError("complete deck must be an array")
    entries = []
    for row_index, row in enumerate(deck):
        expanded = _cards([row])  # Single parser for aliases, deletion and counts.
        number = None
        if len(expanded) == 1 and not any(str(row.get(key, "")).startswith("proposed:")
                                          for key in ("instance_id", "instance_key")):
            supplied = [row[key] for key in ("number", "deck_number") if key in row]
            if (supplied and all(type(value) is int and value >= 0 for value in supplied)
                    and len(set(supplied)) == 1):
                number = supplied[0]
        for copy_index, card in enumerate(expanded):
            entries.append((card, DeckPlanObservedRef(row_index, copy_index, number)))
    # Preserve _cards' exact canonical ordering (stable for identical copies),
    # and keep the source row attached through that same sort.
    entries.sort(key=lambda pair: pair[0])
    counts = Counter(ref.number for _, ref in entries if ref.number is not None)
    return (tuple(card for card, _ in entries),
            tuple(replace(ref, number=None) if ref.number is not None and counts[ref.number] > 1 else ref
                  for _, ref in entries))


def _composition(cards):
    return tuple(sorted(Counter(cards).items()))


@dataclass(frozen=True)
class DeckPlan:
    scope: tuple[str, str, str]
    idol_card_id: str
    character_id: str
    target_composition: tuple[tuple[DeckCard, int], ...]
    reference_json: str

    @property
    def reference(self):
        return json.loads(self.reference_json)

    @property
    def reference_fingerprint(self):
        return _hash({"scope": self.scope, "idol_card_id": self.idol_card_id,
                      "character_id": self.character_id, "target_composition": self.composition_rows(),
                      "reference": self.reference})

    @property
    def plan_id(self):
        return "observed-deck-v1-" + self.reference_fingerprint[:24]

    def composition_rows(self):
        return [{**card.to_dict(), "count": count} for card, count in self.target_composition]

    def to_dict(self):
        return {"schema": SCHEMA, "plan_id": self.plan_id, "reference_fingerprint": self.reference_fingerprint,
                **dict(zip(_SCOPE, self.scope)), "idol_card_id": self.idol_card_id,
                "character_id": self.character_id, "target_composition": self.composition_rows(),
                "target_kind": "observed-entire-final-start-deck", "reference": self.reference,
                "limitations": list(_LIMITATIONS), "predicts_exam_score": False,
                "usable_as_weekly_before_state": False}

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, Mapping) or value.get("schema") != SCHEMA:
            raise ValueError("unsupported persisted deck-plan schema")
        cards = _cards(value.get("target_composition"))
        reference = value.get("reference")
        if not cards or not isinstance(reference, Mapping):
            raise ValueError("persisted deck-plan reference is incomplete")
        plan = cls(_scope(value), _text(value.get("idol_card_id"), "idol_card_id"),
                   _text(value.get("character_id"), "character_id"), _composition(cards), _json(reference))
        if value.get("plan_id") != plan.plan_id or value.get("reference_fingerprint") != plan.reference_fingerprint:
            raise ValueError("persisted deck-plan fingerprint does not match its reference")
        return plan


def project_deck_plan_episode(source, *, source_id=None, require_success=True):
    """Project one proven Final audition composition, without replaying it."""
    # The typed reader intentionally ignores malformed card rows. For a whole
    # deck target, validate the original list first so it cannot become partial.
    raw = source if isinstance(source, Mapping) else source.to_dict()
    if raw.get("step_type") not in (18, "ProduceStepType_AuditionFinal"):
        raise ValueError("not-final-audition")
    cards = _cards(raw.get("produce_cards"))
    if not cards:
        raise ValueError("empty-final-deck")
    episode = read_leaderboard_episode(source)
    if require_success and (episode.rank != 1 or episode.terminal_score <= 0):
        raise ValueError("not-successful-final")
    scope = _scope(raw)
    source_id = source_id or raw.get("source_id") or episode.trajectory_id
    reference = {
        "source_id": _text(source_id, "source_id"), "trajectory_id": episode.trajectory_id,
        "sources": sorted(episode.sources), "stage": "Final", "composition_timing": "audition-start",
        "historical_terminal_score": episode.terminal_score, "historical_rank": episode.rank,
        "versions": {name: getattr(episode, name) for name in
                     ("app_version", "image_version", "master_version", "master_hash")},
        "exam_setting_id": episode.exam_setting_id, "step_select_number": episode.step_select_number,
        "support_cards": list(episode.support_cards), "memory_loadout": list(episode.memory_loadout),
        "memory_abilities": list(episode.memory_abilities), "produce_items": list(episode.produce_items),
        "produce_customize_item_ids": list(episode.produce_customize_item_ids),
        "stage_context": dict(episode.stage_context),
        "quality": {"criterion": "rank-1-and-positive-Final-terminal-score",
                    "successful_final": episode.rank == 1 and episode.terminal_score > 0,
                    "difficulty_known": type(episode.step_select_number) is int,
                    "estimated_win_probability": None,
                    "missing_reference_fields": [name for name in
                        ("app_version", "image_version", "master_version", "master_hash", "exam_setting_id",
                         "support_cards", "memory_loadout") if not getattr(episode, name)]},
    }
    return DeckPlan(scope, _text(episode.idol_card_id, "idol_card_id"),
                    _text(episode.character_id, "character_id"), _composition(cards), _json(reference))


@dataclass(frozen=True)
class DeckPlanLibrary:
    plans: tuple[DeckPlan, ...]
    source: str
    rejected: tuple[tuple[str, int], ...] = ()

    def to_dict(self):
        return {"plan_count": len(self.plans), "source": self.source, "rejected": dict(self.rejected),
                "status": "available" if self.plans else "no-plan", "training_performed": False}


def _rows(source):
    if isinstance(source, (Mapping, LeaderboardReplayEpisode)):
        yield source
    elif isinstance(source, (str, Path)):
        path = Path(source)
        with path.open(encoding="utf-8-sig") as handle:
            if path.suffix.lower() == ".jsonl":
                for line in handle:
                    if line.strip():
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            yield None
            else:
                value = json.load(handle)
                yield from ([value] if isinstance(value, Mapping) else value)
    else:
        yield from source


def load_deck_plan_library(source=DEFAULT_LEADERBOARD_EPISODES, *, context=None,
                           excluded_trajectory_ids=(), excluded_source_ids=(), require_success=True):
    """Read optional reference data. Source/trajectory holdouts are never used.

    ``context`` optionally filters only the exact mode/Plan/mainEffect flow at
    load time. The caller owns caching, adoption timing and run persistence.
    Missing/malformed data produces an empty/partial library, never an input gate.
    """
    if isinstance(source, (str, Path)) and Path(source) == DEFAULT_LEADERBOARD_EPISODES:
        from .portable_behavior_assets import load_public_behavior_role, read_deck_library
        projected = load_public_behavior_role("deck_plan_library", read_deck_library, context=context,
            excluded_trajectory_ids=excluded_trajectory_ids, excluded_source_ids=excluded_source_ids,
            require_success=require_success)
        if projected is not None:
            return projected
    source_name = str(source) if isinstance(source, (str, Path)) else "provided-episodes"
    rejected, plans, audition_decks = Counter(), {}, {}
    try:
        scope = _scope(context) if context is not None else None
        excluded_trajectories, excluded_sources = set(excluded_trajectory_ids), set(excluded_source_ids)
        if source_name in excluded_sources:
            return DeckPlanLibrary((), source_name, (("excluded-source", 1),))
        for source_row in _rows(source):
            try:
                raw = source_row.to_dict() if isinstance(source_row, LeaderboardReplayEpisode) else source_row
                if not isinstance(raw, Mapping):
                    raise ValueError("malformed-episode")
                stage = _AUDITION_STAGES.get(raw.get("step_type"))
                provenance = {raw.get("source_id"), raw.get("trajectory_id"), *raw.get("sources", ())}
                if (stage and (scope is None or _scope(raw) == scope)
                        and raw.get("trajectory_id") not in excluded_trajectories
                        and not provenance & excluded_sources):
                    try:
                        episode = read_leaderboard_episode(source_row)
                        cards = _cards(raw.get("produce_cards"))
                        if cards and episode.rank == 1 and episode.terminal_score > 0:
                            key = (episode.trajectory_id, _scope(raw), episode.idol_card_id)
                            composition = [{**card.to_dict(), "count": count} for card, count in _composition(cards)]
                            entry = {"stage": stage, "composition_timing": "audition-start",
                                     "target_composition": composition, "sources": sorted(episode.sources)}
                            audition_decks.setdefault(key, {}).setdefault(stage, {})[_hash(composition)] = entry
                    except (ValueError, TypeError, KeyError, AttributeError):
                        pass  # Optional earlier milestones never erase a valid Final reference.
                if raw.get("step_type") not in (18, "ProduceStepType_AuditionFinal"):
                    rejected["not-final-audition"] += 1
                    continue
                if scope is not None and _scope(raw) != scope:
                    rejected["other-flow"] += 1
                    continue
                provenance = {raw.get("source_id"), raw.get("trajectory_id"), *raw.get("sources", ())}
                if raw.get("trajectory_id") in excluded_trajectories or provenance & excluded_sources:
                    rejected["excluded-source-or-trajectory"] += 1
                    continue
                plan = project_deck_plan_episode(source_row, require_success=require_success)
                plans.setdefault(plan.reference_fingerprint, plan)
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                rejected[str(error)] += 1
    except (OSError, ValueError, TypeError, KeyError) as error:
        rejected[f"source-unavailable:{type(error).__name__}"] += 1
    enriched = []
    for plan in plans.values():
        stages = audition_decks.get((plan.reference.get("trajectory_id"), plan.scope, plan.idol_card_id), {})
        final = stages.get("Final", {})
        # Earlier decks belong to this exact successful trajectory, not a union
        # of popular cards or snapshots from another exemplar.
        if len(final) == 1 and _hash(plan.composition_rows()) in final:
            milestones = [next(iter(stages[stage].values())) for stage in ("Mid1", "Mid2", "Final")
                          if len(stages.get(stage, {})) == 1]
            if len(milestones) > 1:
                plan = replace(plan, reference_json=_json({**plan.reference, "audition_decks": milestones}))
        enriched.append(plan)
    return DeckPlanLibrary(tuple(sorted(enriched, key=lambda plan: plan.reference_fingerprint)),
                           source_name, tuple(sorted(rejected.items())))


def deck_plan_for_stage(plan, stage=None):
    """Read a milestone of the same adopted trajectory; never reselect a plan."""
    stage = _AUDITION_STAGES.get(stage, stage)
    milestones = plan.reference.get("audition_decks", ())
    matches = [row for row in milestones if isinstance(row, Mapping) and row.get("stage") == stage]
    if len(matches) == 1:
        cards = _cards(matches[0].get("target_composition"))
        if not cards:
            raise ValueError("empty adopted audition deck")
        return replace(plan, target_composition=_composition(cards))
    return plan


def _deficits(target, observed):
    current_customizes = dict(observed.customizes) if observed else {}
    return (max(0, target.upgrade_count - (observed.upgrade_count if observed else 0)),
            tuple((key, count - current_customizes.get(key, 0)) for key, count in target.customizes
                  if count > current_customizes.get(key, 0)))


def _assignment(cost):
    """Rectangular minimum-cost matching; each actual copy can be used once."""
    n, m = len(cost), len(cost[0])
    u, v, p, way = [0] * (n + 1), [0] * (m + 1), [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minimum, used = [float("inf")] * (m + 1), [False] * (m + 1)
        while True:
            used[j0], i0 = True, p[j0]
            delta, j1 = float("inf"), 0
            for j in range(1, m + 1):
                if not used[j]:
                    value = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if value < minimum[j]:
                        minimum[j], way[j] = value, j0
                    if minimum[j] < delta:
                        delta, j1 = minimum[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minimum[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0], j0 = p[j1], j1
    result = [0] * n
    for j in range(1, m + 1):
        if p[j]:
            result[p[j] - 1] = j - 1
    return result


@dataclass(frozen=True)
class DeckPlanGap:
    missing_cards: int
    upgrade_steps: int
    customize_steps: int
    unplanned_cards: int
    unplanned_customize_steps: int
    total_requirements: int
    details_json: str

    @property
    def gap_units(self):
        return self.missing_cards + self.upgrade_steps + self.customize_steps

    @property
    def completion(self):
        return 1.0 - self.gap_units / self.total_requirements

    def to_dict(self):
        return {"missing_cards": self.missing_cards, "upgrade_steps": self.upgrade_steps,
                "customize_steps": self.customize_steps, "gap_units": self.gap_units,
                "unplanned_cards": self.unplanned_cards, "unplanned_customize_steps": self.unplanned_customize_steps,
                "total_requirements": self.total_requirements, "completion": self.completion,
                "details": json.loads(self.details_json), "unit_kind": "structural-counts-not-score"}


@dataclass(frozen=True)
class DeckPlanAssignment:
    target_slot: int
    target: DeckCard
    observed: DeckCard | None
    observed_ref: DeckPlanObservedRef
    upgrade_steps: int
    missing_customizes: tuple[tuple[str, int], ...]
    unplanned_customizes: tuple[tuple[str, int], ...]

    @property
    def completed(self):
        return self.observed is not None and self.upgrade_steps == 0 and not self.missing_customizes

    def to_dict(self):
        return {"target_slot": self.target_slot, "target": self.target.to_dict(),
                "observed": self.observed.to_dict() if self.observed is not None else None,
                "observed_ref": self.observed_ref.to_dict(), "missing_card": self.observed is None,
                "upgrade_steps": self.upgrade_steps, "missing_customizes": dict(self.missing_customizes),
                "unplanned_customizes": dict(self.unplanned_customizes), "completed": self.completed}


@dataclass(frozen=True)
class DeckPlanMatching:
    plan_id: str | None
    target_stage: str | None
    gap: DeckPlanGap | None
    assignments: tuple[DeckPlanAssignment, ...]
    reason: str

    def to_dict(self):
        return {"status": "matched" if self.gap is not None else "unavailable", "plan_id": self.plan_id,
                "target_stage": self.target_stage, "gap": self.gap.to_dict() if self.gap is not None else None,
                "assignments": [row.to_dict() for row in self.assignments], "reason": self.reason,
                "native_option_legality_verified": False}


def _match_cards(cards, plan, refs=None):
    """One minimum structural-cost assignment, shared by gaps and PT quotes."""
    targets = [card for card, count in plan.target_composition for _ in range(count)]
    if not targets:
        raise ValueError("empty-plan-target")
    if refs is not None and len(refs) != len(cards):
        raise ValueError("observed card/reference counts differ")
    missing, upgrades, customizes, matched, extra_customizes, details, assignments = 0, 0, 0, 0, 0, [], []
    for identity in sorted({card.card_id for card in targets}):
        group = [(slot, card) for slot, card in enumerate(targets) if card.card_id == identity]
        actual = [index for index, card in enumerate(cards) if card.card_id == identity]
        pool = actual + [None] * max(0, len(group) - len(actual))
        costs = []
        for _, target in group:
            row = []
            for actual_index in pool:
                observed = cards[actual_index] if actual_index is not None else None
                upgrade_gap, custom_gap = _deficits(target, observed)
                row.append((observed is None) + upgrade_gap + sum(count for _, count in custom_gap))
            costs.append(row)
        for (target_slot, target), index in zip(group, _assignment(costs)):
            actual_index = pool[index]
            observed = cards[actual_index] if actual_index is not None else None
            upgrade_gap, custom_gap = _deficits(target, observed)
            missing += observed is None
            matched += observed is not None
            upgrades += upgrade_gap
            customizes += sum(count for _, count in custom_gap)
            extra = tuple((key, count - dict(target.customizes).get(key, 0)) for key, count in observed.customizes
                          if count > dict(target.customizes).get(key, 0)) if observed else ()
            extra_customizes += sum(count for _, count in extra)
            reference = refs[actual_index] if refs is not None and actual_index is not None else DeckPlanObservedRef()
            assignment = DeckPlanAssignment(target_slot, target, observed, reference, upgrade_gap, custom_gap, extra)
            assignments.append(assignment)
            if observed is None or upgrade_gap or custom_gap or extra:
                details.append(assignment.to_dict())
    total = sum(1 + card.upgrade_count + sum(count for _, count in card.customizes) for card in targets)
    return (DeckPlanGap(missing, upgrades, customizes, len(cards) - matched, extra_customizes, total, _json(details)),
            tuple(sorted(assignments, key=lambda row: row.target_slot)))


def _gap(cards, plan):
    return _match_cards(cards, plan)[0]


def match_plan_requirements(deck, plan, *, stage=None):
    """Match every target copy once, retaining completed-copy/source lineage.

    ``observed_ref.number`` is an unambiguous supplied Number for a single
    actual copy, or None. It does not establish a current native menu action.
    Stage selection belongs to the same adopted plan and never changes its ID.
    """
    if not isinstance(plan, DeckPlan):
        return DeckPlanMatching(None, None, None, (), "no-observed-plan")
    try:
        target = deck_plan_for_stage(plan, stage)
        cards, refs = _cards_with_refs(deck)
        gap, assignments = _match_cards(cards, target, refs)
        target_stage = _AUDITION_STAGES.get(stage, stage) if target is not plan else "Final"
        return DeckPlanMatching(plan.plan_id, target_stage, gap, assignments,
                                "same-plan-structural-requirement-progress")
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return DeckPlanMatching(plan.plan_id, None, None, (), f"current-deck-unavailable:{type(error).__name__}:{error}")


@dataclass(frozen=True)
class DeckPlanSelection:
    plan: DeckPlan | None
    gap: DeckPlanGap | None
    reason: str
    matched_scope: str = "none"
    candidate_count: int = 0

    def to_dict(self):
        return {"status": "selected" if self.plan else "no-plan", "reason": self.reason,
                "plan": self.plan.to_dict() if self.plan else None, "gap": self.gap.to_dict() if self.gap else None,
                "matched_scope": self.matched_scope, "candidate_count": self.candidate_count,
                "selection_method": "closest-observed-exemplar; same-idol-first",
                "limitations": list(_LIMITATIONS), "owns_input": False}


def select_deck_plan(context, current_deck, *, library):
    """Adopt one entire observed exemplar. Do not call separately per action."""
    try:
        scope = _scope(context)
        idol = _text(context.get("idol_card_id"), "idol_card_id")
        cards = _cards(current_deck)
        if not cards:
            return DeckPlanSelection(None, None, "native-current-deck-unavailable")
        candidates = [plan for plan in library.plans if plan.scope == scope
                      and plan.reference.get("historical_rank") == 1
                      and plan.reference.get("historical_terminal_score", 0) > 0]
        if not candidates:
            return DeckPlanSelection(None, None, "no-reference-for-exact-mode-plan-mainEffect")
        same_idol = [plan for plan in candidates if plan.idol_card_id == idol]
        if not same_idol:
            return DeckPlanSelection(None, None, "other-idol-reference-not-adopted; exclusive-card-reachability-unresolved",
                                     "same-flow-other-idol", len(candidates))
        candidates = same_idol
        evaluated = [(plan, _gap(cards, plan)) for plan in candidates]
        from .portable_behavior_assets import selection_tie_breaker
        plan, gap = min(evaluated, key=lambda item: (1 - item[1].completion, item[1].unplanned_cards,
                                                    item[1].unplanned_customize_steps, selection_tie_breaker(item[0])))
        return DeckPlanSelection(plan, gap, "observed-whole-deck-target", "same-idol", len(candidates))
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return DeckPlanSelection(None, None, f"plan-unavailable:{type(error).__name__}:{error}")


@dataclass(frozen=True)
class DeckPlanProgress:
    plan_id: str | None
    before: DeckPlanGap | None
    after: DeckPlanGap | None
    reason: str
    target_stage: str | None = None

    @property
    def value(self):
        return self.after.completion - self.before.completion if self.before and self.after else 0.0

    def to_dict(self):
        return {"status": "compared" if self.before and self.after else "unavailable", "plan_id": self.plan_id,
                "value": self.value, "before": self.before.to_dict() if self.before else None,
                "after": self.after.to_dict() if self.after else None, "reason": self.reason,
                "target_stage": self.target_stage,
                "predicts_exam_score": False, "limitations": list(_LIMITATIONS)}


def evaluate_plan_progress(before_deck, after_deck, plan, *, stage=None):
    """Compare alternatives against one fixed plan; never reselect or execute."""
    if not isinstance(plan, DeckPlan):
        return DeckPlanProgress(None, None, None, "no-observed-plan")
    before = match_plan_requirements(before_deck, plan, stage=stage)
    after = match_plan_requirements(after_deck, plan, stage=stage)
    if before.gap is None or after.gap is None:
        return DeckPlanProgress(plan.plan_id, None, None, before.reason if before.gap is None else after.reason)
    return DeckPlanProgress(plan.plan_id, before.gap, after.gap, before.reason, before.target_stage)
