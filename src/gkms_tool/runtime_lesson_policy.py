"""Native lesson candidates and explicitly rule-based policy entry points.

The active audition BC/RL bundle is never consulted here. All input remains
owned by the existing DLL gateway; these functions only project and rank.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping

from .audition_local_save_state import parse_local_save_exam_state
from .master_db import DEFAULT_DATABASE
from .runtime_lesson_context import NativeLessonContext, evaluate_native_lesson, parse_native_lesson_context


@dataclass(frozen=True)
class NativeLessonCandidates:
    context: NativeLessonContext
    enumeration: object
    projection: object
    prepared: object | None = None
    compilation: tuple[object, ...] = ()
    root_search: object | None = None

    @property
    def complete(self):
        return bool(getattr(self.enumeration, "complete", False))

    @property
    def candidates(self):
        return tuple(getattr(self.enumeration, "candidates", ()))

    @property
    def blockers(self):
        return tuple(getattr(self.enumeration, "blockers", ()))


def _require_lesson_main(raw):
    if raw.get("phase") != 6 or raw.get("commandList") != [] or raw.get("isExamEndComplete") is not False:
        raise ValueError("native lesson candidate projection requires a current settled Main decision")


def prepare_runtime_plan1_lesson(raw, native_context=None, *, expected_produce_id=None,
                                 expected_idol_card_id=None, database=DEFAULT_DATABASE, master_dir=None):
    from .plan1_exact_learned_exam import _compile_current_cards
    from .plan1_native_policy_adapter import build_plan1_master_drink_candidate_provider, require_plan1_policy_projection
    from .plan1_native_core import load_plan1_native_settings
    from .plan1_runtime_simulator_bridge import _drink_inventory, project_plan1_runtime_state
    from .nia_plan1_native_sidecar import enumerate_plan1_native_legal_candidates
    context = parse_native_lesson_context(raw, native_context, expected_produce_id=expected_produce_id,
        expected_idol_card_id=expected_idol_card_id, master_dir=master_dir)
    if context.plan_type != "ProducePlanType_Plan1":
        raise ValueError("native lesson is not Plan1")
    _require_lesson_main(raw)
    parsed = parse_local_save_exam_state(raw)
    projection = project_plan1_runtime_state(raw, stage_label="Lesson")
    state = require_plan1_policy_projection(projection)
    compilation = _compile_current_cards(SimpleNamespace(state=parsed), database=database)
    drinks = _drink_inventory(parsed)
    provider = build_plan1_master_drink_candidate_provider(projection, database=database) if drinks else None
    settings = load_plan1_native_settings(context.setting_id, **({"master_dir": Path(master_dir)} if master_dir is not None else {}))
    enumeration = enumerate_plan1_native_legal_candidates(state, compilation, settings=settings,
        drink_inventory=drinks, drink_candidate_provider=provider, include_end_turn=True, end_turn_available=True)
    return NativeLessonCandidates(context, enumeration, projection, compilation=compilation)


class RuntimeLessonPlan3CandidateProvider:
    def __init__(self, *, expected_produce_id=None, expected_idol_card_id=None,
                 database=DEFAULT_DATABASE, master_dir=None, support_card_master=None):
        from .plan3_engine import DEFAULT_MASTER_DIR
        from .plan3_native_search_bridge import DEFAULT_SUPPORT_CARD_MASTER
        self.expected_produce_id, self.expected_idol_card_id = expected_produce_id, expected_idol_card_id
        self.database = Path(database)
        self.master_dir = DEFAULT_MASTER_DIR if master_dir is None else Path(master_dir)
        self.support_card_master = DEFAULT_SUPPORT_CARD_MASTER if support_card_master is None else Path(support_card_master)
        self._key, self._result = None, None

    def __call__(self, decoded):
        from .plan3_drink import load_plan3_drink_inventory
        from .plan3_engine import load_plan3_exam_settings
        from .plan3_native_search import enumerate_plan3_native_legal_candidates
        from .plan3_native_search_bridge import search_plan3_native_decoded_local_save
        key = (decoded.envelope.plaintext, json.dumps(getattr(decoded.envelope, "native_context", None), sort_keys=True))
        if key == self._key:
            return self._result
        raw = json.loads(decoded.envelope.plaintext)
        context = parse_native_lesson_context(raw, getattr(decoded.envelope, "native_context", None),
            expected_produce_id=self.expected_produce_id, expected_idol_card_id=self.expected_idol_card_id,
            master_dir=self.master_dir)
        if context.plan_type != "ProducePlanType_Plan3":
            raise ValueError("native lesson is not Plan3")
        _require_lesson_main(raw)
        prepared = search_plan3_native_decoded_local_save(decoded, raw_exam_save=raw, depth=0, beam_width=1,
            database=self.database, support_card_master=self.support_card_master)
        if prepared.issues or prepared.native_state is None or not prepared.projection.exact:
            raise ValueError("native lesson projection incomplete: " + ";".join(prepared.semantic_gaps))
        settings = load_plan3_exam_settings(context.setting_id, master_dir=self.master_dir)
        inventory = load_plan3_drink_inventory(tuple(item["_id"] for item in raw["drinkList"]),
                                              master_dir=self.master_dir, database=self.database)
        roots = []
        enumeration = enumerate_plan3_native_legal_candidates(prepared.projection.state, prepared.native_state,
            settings=settings, database=self.database, support_upgrades=prepared.support_upgrades,
            support_card_searches=dict(prepared.support_card_searches), gimmick_profile=prepared.gimmick_profile,
            include_drinks=True, include_turn_end=True, drink_inventory=inventory,
            force_end_score=context.limit_border, force_end_stamina_recovery=settings.turn_end_stamina_recovery,
            root_result_observer=roots.append)
        result = NativeLessonCandidates(context, enumeration, prepared.projection, prepared=prepared,
                                         root_search=roots[0] if roots else None)
        self._key, self._result = key, result
        return result


def build_runtime_plan3_lesson_advisor(provider: RuntimeLessonPlan3CandidateProvider, *, beam_width=16):
    from .plan3_lesson_advisor import Plan3LessonAdvisorIssue, Plan3LessonAdvisorReport, advise_plan3_lesson_decoded

    def advisor(decoded):
        try:
            candidates = provider(decoded)
            if not candidates.complete:
                raise ValueError("lesson legal set incomplete: " + ";".join(candidates.blockers))
            report = advise_plan3_lesson_decoded(decoded, source="dll-native-lesson", beam_width=beam_width,
                database=provider.database, master_dir=provider.master_dir, support_card_master=provider.support_card_master,
                native_preparation=candidates.prepared)
            if not report.available:
                report = _bounded_plan3_lesson_report(candidates, report)
            return replace(report, current_state={**(report.current_state or {}),
                "native_lesson_context": candidates.context.to_dict()},
                search={**report.search, "decision_owner": "rule-planner", "learned_policy_used": False,
                        "policy": ("native-lesson-bounded-root-v1" if report.search.get("algorithm") == "bounded-native-root"
                                   else "native-lesson-beam-v1"),
                        "root_legal_candidate_count": len(candidates.candidates)})
        except (KeyError, TypeError, ValueError, OSError) as error:
            return Plan3LessonAdvisorReport("unavailable", (Plan3LessonAdvisorIssue("native-lesson-unavailable", str(error)),),
                None, (), None, None, {"decision_owner": "rule-planner", "learned_policy_used": False}, (), "dll-native-lesson")
    return advisor


def _bounded_plan3_lesson_report(candidates, full_report):
    from .plan3_audition_advisor import serialize_plan3_audition_decision
    from .plan3_lesson_advisor import Plan3LessonAdvisorReport
    from .plan3_native_search import _canonical_root_candidate
    root = candidates.root_search
    if root is None or root.diagnostics:
        return full_report
    legal_ids = {candidate.action_id for candidate in candidates.candidates}
    paths = []
    for path in root.candidates:
        candidate, blockers = _canonical_root_candidate(path)
        if blockers or candidate is None or candidate.action_id not in legal_ids:
            raise ValueError("bounded lesson path does not match its complete root legal set")
        state = path.state
        evaluated = evaluate_native_lesson(candidates.context, score=state.score,
                                           remaining_turns=state.turns_remaining, native_complete=False)
        key = (int(evaluated.perfect), int(evaluated.cleared), state.score, state.stamina, state.block)
        paths.append((key, candidate, path, evaluated))
    if not paths:
        return full_report
    ordinary = [item for item in paths if item[1].kind != "drink"]
    best_ordinary = max(ordinary, key=lambda item: item[0]) if ordinary else None
    # Preserve drinks unless the current final turn needs them, or a drink
    # itself proves PERFECT beyond the best ordinary root action.
    eligible = [item for item in paths if item[1].kind != "drink" or candidates.context.remaining_turns <= 1
                or (item[3].perfect and (best_ordinary is None or not best_ordinary[3].perfect))]
    key, candidate, path, evaluated = max(eligible or paths, key=lambda item: (item[0], item[1].action_id))
    decision = serialize_plan3_audition_decision(path.decision_steps[0], 0)
    return Plan3LessonAdvisorReport("ready", (), {"native_lesson_context": candidates.context.to_dict()},
        (decision,), decision,
        {"complete": evaluated.terminal, "clear": evaluated.cleared, "perfect": evaluated.perfect,
         "score": evaluated.score, "prediction_scope": "one-native-action",
         "full_lesson_score_prediction_available": False},
        {"algorithm": "bounded-native-root", "decision_scope": "current-complete-legal-set",
         "decision_owner": "rule-planner", "learned_policy_used": False,
         "full_horizon_unavailable": [issue.to_dict() for issue in full_report.issues]}, (), "dll-native-lesson")


def choose_runtime_plan1_lesson(candidates: NativeLessonCandidates, *, max_depth=2, max_nodes=64,
                                database=DEFAULT_DATABASE):
    """Bounded rule planning over the actual Hand; no forecast/model claim."""
    from .plan1_native_core import load_plan1_native_settings
    from .plan1_native_search import Plan1SearchLimits, search_plan1_stage
    from .plan1_runtime_simulator_bridge import _apply_plan1_runtime_drink
    from .leaderboard_replay import LeaderboardReplayAction
    if candidates.context.plan_type != "ProducePlanType_Plan1" or not candidates.complete:
        raise ValueError("Plan1 lesson needs a complete native legal set")
    state = candidates.projection.state
    searched = search_plan1_stage(state, candidates.compilation,
        settings=load_plan1_native_settings(candidates.context.setting_id), limits=Plan1SearchLimits(max_depth, max_nodes))
    if searched.blockers:
        raise ValueError("Plan1 lesson rule search blocked: " + ";".join(item.code for item in searched.blockers))
    selected_id = "END_TURN" if searched.first_action is None else "PLAY:" + searched.first_action.guid
    selected = next((candidate for candidate in candidates.candidates if candidate.action_id == selected_id), None)
    if selected is None:
        raise ValueError("Plan1 lesson rule search did not choose a legal native action")
    def value(search):
        after = search.final_state.scalar
        evaluation = evaluate_native_lesson(candidates.context, score=after.score, native_complete=False)
        return (int(evaluation.perfect), int(evaluation.cleared), after.score, after.stamina), evaluation
    best_value, best_evaluation = value(searched)
    deferred = []
    for drink in (candidate for candidate in candidates.candidates if candidate.kind == "drink"):
        blockers = []
        _slot, _definition, _applied, after = _apply_plan1_runtime_drink(candidates.projection,
            LeaderboardReplayAction(drink.drink_slot_index, "use-drink", (drink.drink_slot_index,)),
            database=Path(database), settings=None, blockers=blockers)
        if blockers or after is None:
            raise ValueError("current legal drink lost its native projection")
        future = search_plan1_stage(after, candidates.compilation,
            settings=load_plan1_native_settings(candidates.context.setting_id), limits=Plan1SearchLimits(max_depth, max_nodes))
        if future.blockers:
            deferred.extend(item.code for item in future.blockers)
            continue
        candidate_value, candidate_evaluation = value(future)
        justified = (candidates.context.remaining_turns <= 1
                     or (candidate_evaluation.perfect and not best_evaluation.perfect))
        if justified and candidate_value > best_value:
            selected, searched, best_value, best_evaluation = drink, future, candidate_value, candidate_evaluation
    return {"action": selected.to_dict(), "decision_owner": "rule-planner", "learned_policy_used": False,
            "policy": "native-lesson-bounded-hand-v1", "stage": "Lesson",
            "search_depth": max_depth, "search_nodes": searched.nodes, "search_budget_complete": searched.complete,
            "predicted_hand_sequence_score": searched.final_state.scalar.score,
            "drink_policy": "preserve-unless-final-turn-or-bounded-perfect",
            "unscored_future_branches": sorted(set(deferred)),
            "full_lesson_score_prediction_available": False,
            "current_lesson": as_evaluation_dict(candidates.context)}


def as_evaluation_dict(context):
    from dataclasses import asdict
    return asdict(evaluate_native_lesson(context))
