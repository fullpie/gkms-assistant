"""Bounded, pure drink timing over the same legal PLAY continuation.

No actor logits, Q values, input controller or automatic second action lives
here. A recommendation consumes one *observed* slot; callers must observe and
guard again after drinking, rather than blindly execute the simulated PLAY.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Protocol


SCHEMA = "gkms.exam-drink-before-play.v1"
_STAGES = {"Mid1": 2, "Mid2": 1, "Final": 0}


def _kind(candidate):
    value = candidate.get("kind")
    return {"use-hand": "play", "card": "play", "use-drink": "drink", "use_drink": "drink"}.get(value, value)


@dataclass(frozen=True)
class DrinkDecisionContext:
    produce_id: str
    plan_type: str
    main_effect_type: str
    stage: str
    boundary_digest: str
    # Explicit game/rule target only; never populate from Q or rank guesses.
    required_score: int | None = None
    coverage_blockers: tuple[str, ...] = ()
    projection_complete: bool = False


@dataclass(frozen=True)
class DrinkStateView:
    score: int
    stamina: int
    max_stamina: int
    current_turn: int
    turns_remaining: int
    plays_remaining: int
    exam_card_play_count: int
    terminal: bool
    resources: tuple[tuple[str, int], ...]
    drink_tokens: tuple[str, ...]
    zone_digest: str

    def summary(self):
        result = asdict(self)
        result["resources"] = dict(self.resources)
        return result


@dataclass(frozen=True)
class DrinkTransition:
    after: object | None = None
    blockers: tuple[str, ...] = ()
    comparison: Mapping | None = None

    @property
    def supported(self):
        return self.after is not None and not self.blockers


class DrinkComparisonAdapter(Protocol):
    """Plan rules own all effects. This interface only compares their results."""

    def view(self, state: object) -> DrinkStateView: ...
    def transition(self, state: object, candidate: Mapping) -> DrinkTransition: ...
    def drink_roles(self, state: object, candidate: Mapping) -> tuple[str, ...]: ...


def compare_drink_before_play(*, state, legal_candidates: Sequence[Mapping], baseline_action_id: str,
                             adapter: DrinkComparisonAdapter, context: DrinkDecisionContext):
    """Return a single exact drink candidate or an explained abstention.

    v1 scope is NIA Pro auditions. Reserve one bottle per later audition, except
    a proven low-stamina rescue or crossing an explicitly supplied score target.
    This is a timing heuristic around exact local transitions, not a score
    forecast or full-horizon optimizer. Unknown branches retain their blockers.
    """
    result = {"schema": SCHEMA, "boundary_digest": context.boundary_digest, "scope": asdict(context),
              "baseline_action_id": baseline_action_id, "candidate": None, "branches": [], "blockers": [],
              "continuation_is_advisory_only": True, "value_kind": "ranking_proxy",
              "forecast_available": False, "max_bottles_per_decision": 1}
    if (context.produce_id != "produce-004" or context.stage not in _STAGES or not context.boundary_digest
            or context.plan_type not in ("ProducePlanType_Plan1", "ProducePlanType_Plan2", "ProducePlanType_Plan3")):
        result["blockers"] = ["drink-comparison-mode-stage-scope-unavailable"]
        return result
    if context.coverage_blockers:
        result["blockers"] = list(context.coverage_blockers)
        return result
    if context.projection_complete is not True:
        result["blockers"] = ["strict-native-projection-not-confirmed"]
        return result
    if context.required_score is not None and (type(context.required_score) is not int or context.required_score < 0):
        raise ValueError("required_score must be an explicit nonnegative integer or unknown")
    candidates = [dict(value) for value in legal_candidates]
    ids = [value.get("action_id") for value in candidates]
    if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
        result["blockers"] = ["drink-comparison-invalid-legal-identities"]
        return result
    baseline_action = next((value for value in candidates if value["action_id"] == baseline_action_id), None)
    if baseline_action is None or _kind(baseline_action) != "play" or not baseline_action.get("card_guid") or baseline_action.get("legal") is False:
        result["blockers"] = ["drink-comparison-requires-one-legal-guid-play"]
        return result
    root = adapter.view(state)
    baseline_transition = adapter.transition(state, baseline_action)
    if not baseline_transition.supported:
        result["blockers"] = ["baseline-play-unsupported", *baseline_transition.blockers]
        return result
    baseline = adapter.view(baseline_transition.after)
    result["baseline_after"] = baseline.summary()
    reserve = _STAGES[context.stage]
    result["bottles_reserved_for_later_auditions"] = reserve
    eligible = []
    for candidate in candidates:
        if _kind(candidate) != "drink":
            continue
        branch = {"candidate": candidate, "continuation_guid": baseline_action["card_guid"],
                  "eligible": False, "reasons": []}
        result["branches"].append(branch)
        if candidate.get("legal") is False:
            branch["reasons"] = ["drink-explicitly-not-in-native-legal-set"]
            continue
        roles = adapter.drink_roles(state, candidate)
        branch["effect_roles"] = list(roles)
        if not roles or "unsupported" in roles:
            branch["reasons"] = ["drink-effects-outside-bounded-comparison-scope"]
            continue
        drank_transition = adapter.transition(state, candidate)
        if not drank_transition.supported:
            branch["reasons"] = ["drink-transition-unsupported", *drank_transition.blockers]
            continue
        drank = adapter.view(drank_transition.after)
        slot = candidate.get("slot_index", candidate.get("drink_slot_index"))
        if type(slot) is not int or slot < 0 or slot >= len(root.drink_tokens):
            branch["reasons"] = ["drink-slot-outside-observed-inventory"]
            continue
        if drank.drink_tokens != root.drink_tokens[:slot] + root.drink_tokens[slot + 1:]:
            branch["reasons"] = ["drink-did-not-consume-exactly-observed-slot"]
            continue
        if (drank.terminal or drank.current_turn != root.current_turn or drank.turns_remaining != root.turns_remaining
                or drank.exam_card_play_count != root.exam_card_play_count):
            branch["reasons"] = ["drink-does-not-preserve-same-card-play-opportunity"]
            continue
        opportunity = {"supported": drank.plays_remaining == root.plays_remaining and drank.zone_digest == root.zone_digest,
                       "extra_plays": 0}
        verifier = getattr(adapter, "verify_drink_opportunity", None)
        if verifier is not None:
            opportunity = verifier(state, drank_transition.after, candidate, baseline_action)
            branch["opportunity_proof"] = opportunity
        if not isinstance(opportunity, Mapping) or opportunity.get("supported") is not True:
            details = opportunity.get("blockers", ()) if isinstance(opportunity, Mapping) else ("invalid-adapter-opportunity-proof",)
            branch["reasons"] = ["drink-does-not-preserve-same-card-play-opportunity", *details]
            continue
        extra_budget = opportunity.get("extra_plays", 0)
        if type(extra_budget) is not int or not 0 <= extra_budget <= 1:
            branch["reasons"] = ["drink-extra-play-proof-outside-bounded-comparison"]
            continue
        rebind = opportunity.get("continuation_rebind_required") is True
        rebinder = getattr(adapter, "compare_rebound_continuation", None)
        continuation = (rebinder(state, drank_transition.after, baseline_action, baseline_transition.after)
                        if rebind and rebinder is not None else adapter.transition(drank_transition.after, baseline_action))
        if not continuation.supported:
            branch["reasons"] = ["rebound-continuation-unsupported" if rebind else "same-guid-continuation-unsupported", *continuation.blockers]
            continue
        after = adapter.view(continuation.after)
        branch["after_rebound_play" if rebind else "after_same_play"] = after.summary()
        if rebind:
            branch["continuation_comparison"] = dict(continuation.comparison or {})
            branch["continuation_guid"] = (continuation.comparison or {}).get("card_guid")
        extra_used = 0
        if extra_budget:
            align = getattr(adapter, "align_extra_play", None)
            aligned = None if align is None else align(continuation.after, baseline_transition.after)
            if aligned is None or not aligned.supported:
                branch["reasons"] = ["extra-play-aligned-continuation-unsupported", *(aligned.blockers if aligned is not None else ())]
                continue
            continuation = aligned
            after = adapter.view(aligned.after)
            branch["extra_play_comparison"] = dict(aligned.comparison or {})
            extra_used = (aligned.comparison or {}).get("extra_play_count", 0)
            branch["after_aligned_continuation"] = after.summary()
        if (type(extra_used) is not int or not 0 <= extra_used <= extra_budget
                or (after.current_turn, after.turns_remaining, after.plays_remaining, after.terminal)
                != (baseline.current_turn, baseline.turns_remaining, baseline.plays_remaining, baseline.terminal)
                or after.exam_card_play_count != baseline.exam_card_play_count + extra_used):
            branch["reasons"] = ["same-play-continuation-boundary-diverged"]
            continue
        before_resources, after_resources = dict(baseline.resources), dict(after.resources)
        if set(before_resources) != set(after_resources):
            branch["reasons"] = ["comparison-resource-projection-mismatch"]
            continue
        deltas = {key: after_resources[key] - value for key, value in before_resources.items()}
        score_gain, stamina_gain = after.score - baseline.score, after.stamina - baseline.stamina
        interaction_score_gain = score_gain - (drank.score - root.score)
        branch.update(score_gain=score_gain, interaction_score_gain=interaction_score_gain,
                      stamina_gain=stamina_gain, resource_deltas=deltas)
        resource_regression = stamina_gain < 0 or any(value < 0 for value in deltas.values())
        final_score_improvement = context.stage == "Final" and baseline.terminal and after.terminal and score_gain > 0 and after.stamina >= 0
        extra_play_improvement = extra_used > 0 and score_gain > 0 and after.stamina >= max(1, after.max_stamina // 4)
        if score_gain < 0 or (resource_regression and not (final_score_improvement or extra_play_improvement)):
            branch["reasons"] = ["observed-continuation-resource-or-score-regression"]
            continue
        target_rescue = context.required_score is not None and baseline.score < context.required_score <= after.score
        stamina_rescue = not baseline.terminal and stamina_gain > 0 and baseline.stamina <= max(1, baseline.max_stamina // 4)
        reasons = []
        if resource_regression and final_score_improvement:
            reasons.append("known-resource-cost-for-higher-final-terminal-score")
        elif resource_regression and extra_play_improvement:
            reasons.append("proven-extra-play-score-with-stamina-reserve")
        if extra_used and score_gain > 0:
            reasons.append("extra-play-improves-aligned-boundary-score")
        if target_rescue:
            reasons.append("crosses-explicit-score-target")
        if stamina_rescue:
            reasons.append("low-stamina-rescue-on-same-play")
        if score_gain > 0 and baseline.terminal:
            reasons.append("positive-terminal-play-comparison")
        elif interaction_score_gain > 0 and not extra_used:
            # Includes e.g. a Block drink before a Block converter. Subtract
            # the drink's standalone score so plain water cannot pretend to
            # improve the chosen card or its automatic lifecycle.
            reasons.append("drink-improves-rebound-play-score" if rebind else "drink-improves-same-play-score")
        if ("persistent-review" in roles and context.plan_type == "ProducePlanType_Plan2"
                and context.main_effect_type == "ProduceExamEffectType_ExamReview" and root.turns_remaining >= 2
                and not baseline.terminal and deltas.get("review", 0) > 0):
            reasons.append("early-review-survives-for-proven-end-turn-payoff")
        if ("persistent-focus" in roles and context.plan_type == "ProducePlanType_Plan1"
                and root.turns_remaining >= 2 and not baseline.terminal and deltas.get("lesson_buff", 0) > 0):
            reasons.append("installed-focus-listener-produced-a-proven-end-turn-gain")
        # A plain Aggressive stack is not sufficient. Require the selected
        # card to already produce extra Block beyond any Block added directly
        # by the drink, while this strategy still has a later turn to use it.
        direct_block = dict(drank.resources).get("block", 0) - dict(root.resources).get("block", 0)
        block_production_gain = deltas.get("block", 0) - direct_block
        branch["block_production_gain"] = block_production_gain
        if ("aggressive" in roles and context.plan_type == "ProducePlanType_Plan2"
                and context.main_effect_type == "ProduceExamEffectType_ExamCardPlayAggressive"
                and root.turns_remaining >= 2 and not baseline.terminal and block_production_gain > 0):
            reasons.append("aggressive-improves-current-block-production")
        if not reasons:
            branch["reasons"] = ["no-useful-current-timing-witness"]
            continue
        if len(root.drink_tokens) <= reserve and not (stamina_rescue or target_rescue):
            branch["reasons"] = ["reserve-bottle-for-later-audition", *reasons]
            continue
        branch["eligible"], branch["reasons"] = True, reasons
        # Lexicographic priorities; do not add incomparable buff/HP/score units.
        priority = (int(target_rescue), int(stamina_rescue), score_gain,
                    deltas.get("review", 0) if "persistent-review" in roles else 0,
                    block_production_gain if "aggressive" in roles else 0, stamina_gain, -slot)
        eligible.append((priority, candidate))
    if eligible:
        result["candidate"] = dict(max(eligible, key=lambda pair: pair[0])[1])
    return result


class Plan2DrinkComparisonAdapter:
    """Thin adapter to the existing immutable, strict Plan2 lifecycle reducer."""

    _ROLES = {"lesson": "direct-score", "lesson_depend_review": "direct-score", "review": "persistent-review",
              "block": "resource", "aggressive": "aggressive", "stamina_recover_fix": "resource",
              "lesson_value_multiple": "score-buff", "stamina_consumption_down": "resource",
              "stamina_reduce_fix": "immediate-cost"}

    def __init__(self, catalog, *, transitioner=None):
        from .plan2_native_horizon import simulate_plan2_native_action_lifecycle
        self.catalog = catalog
        self.transitioner = transitioner or simulate_plan2_native_action_lifecycle

    def view(self, state):
        import hashlib
        scalar = state.scalar
        return DrinkStateView(scalar.score, scalar.stamina, scalar.max_stamina, scalar.current_turn,
            max(0, state.limit_turn + state.extra_turn - scalar.current_turn + 1) if not state.terminal else 0,
            state.plays_remaining, scalar.exam_card_play_count, state.terminal,
            tuple((key, getattr(scalar, key)) for key in ("review", "card_play_aggressive", "block")),
            tuple(value.instance_id for value in state.drink_runtime.inventory),
            hashlib.sha256(repr(state.zones).encode("utf-8")).hexdigest())

    def _action(self, state, candidate):
        from .plan2_native_horizon import Plan2NativeAction, Plan2NativeDrinkAction
        if _kind(candidate) == "play":
            action = Plan2NativeAction("play", candidate.get("card_guid", ""))
        elif _kind(candidate) == "drink":
            action = Plan2NativeDrinkAction(candidate.get("slot_index", candidate.get("drink_slot_index")),
                candidate.get("instance_id", ""), candidate.get("drink_id", ""), candidate.get("selected_card_guid", "") or "")
            state.drink_runtime.resolve(action.slot_index, instance_id=action.instance_id, drink_id=action.drink_id)
        else:
            raise ValueError("unsupported comparison action")
        if action.action_id != candidate.get("action_id"):
            raise ValueError("candidate action ID and native identity disagree")
        return action

    def transition(self, state, candidate):
        try:
            action = self._action(state, candidate)
            transition = self.transitioner(state, action, self.catalog)
            return DrinkTransition(transition.after if transition.supported else None,
                tuple(f"{value.code}:{value.detail}" for value in transition.blockers))
        except (KeyError, TypeError, ValueError) as error:
            return DrinkTransition(blockers=(f"plan2-drink-comparison:{type(error).__name__}:{error}",))

    def drink_roles(self, state, candidate):
        try:
            action = self._action(state, candidate)
            drink = state.drink_runtime.resolve(action.slot_index, instance_id=action.instance_id, drink_id=action.drink_id)
            return tuple(dict.fromkeys(self._ROLES.get(effect.handler, "unsupported") for effect in drink.effects))
        except (AttributeError, KeyError, TypeError, ValueError):
            return ("unsupported",)


def recommend_plan2_drink_before_play(root, catalog, *, legal_candidates, baseline_action_id, context,
                                     transitioner=None):
    """Use the exact observed legal set; this helper never enumerates extra input targets."""
    from dataclasses import replace
    if context.plan_type != "ProducePlanType_Plan2" or context.main_effect_type not in (
        "ProduceExamEffectType_ExamReview", "ProduceExamEffectType_ExamCardPlayAggressive"
    ):
        context = replace(context, coverage_blockers=(*context.coverage_blockers, "plan2-drink-main-effect-unavailable"))
    if root.exam_mode.is_lesson or root.exam_mode.step_type_value != {"Mid1": 16, "Mid2": 17, "Final": 18}.get(context.stage):
        context = replace(context, coverage_blockers=(*context.coverage_blockers, "plan2-drink-native-stage-mismatch"))
    bound_candidates = []
    for candidate in legal_candidates:
        candidate = dict(candidate)
        # The shared canonical snapshot omits Plan2's internal instance token.
        # Resolve it from this observed ordered runtime, never by parsing or
        # inventing an action-ID fragment. The adapter verifies the full ID.
        slot = candidate.get("slot_index", candidate.get("drink_slot_index"))
        if (_kind(candidate) == "drink" and "instance_id" not in candidate and type(slot) is int
                and 0 <= slot < len(root.drink_runtime.inventory)):
            instance = root.drink_runtime.inventory[slot]
            if instance.drink_id == candidate.get("drink_id"):
                candidate["instance_id"] = instance.instance_id
        bound_candidates.append(candidate)
    return compare_drink_before_play(state=root, legal_candidates=bound_candidates, baseline_action_id=baseline_action_id,
        adapter=Plan2DrinkComparisonAdapter(catalog, transitioner=transitioner), context=context)
