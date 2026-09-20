"""Audition policy inputs from one DLL observation and its mode's Master.

No outer LocalSave, synthetic play log, replay poller or baseline action is
needed. Native Produce context, when available, belongs to that same snapshot.
Otherwise identity must match exactly once from observed serializer fields.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from typing import Mapping

from .master_db import DEFAULT_DATABASE
from .nia_audition_advisor import _nia_search_extensions, _validate_static_runtime_context
from .nia_exam_save_bridge import NiaExamSaveDifficultyKey, project_runtime_exam_save_audition
from .nia_outer_exam_save_adapter import (_collect_produce_card_ids, _master_rows,
                                         _runtime_gimmick_group_ids, _serialized_turn_types, _step_type)
from .nia_plan3_native_candidates import NiaPlan3NativeCandidateResult
from .nia_static_adapter import DEFAULT_MASTER_DIR
from .runtime_mode_profile import load_runtime_mode_profile
from .plan3_audition_advisor import Plan3AuditionAdvisorIssue, Plan3AuditionAdvisorReport
from .plan3_audition_search_bridge import bind_plan3_audition_state, parse_plan3_audition_context
from .plan3_drink import load_plan3_drink_inventory
from .plan3_engine import load_plan3_exam_settings
from .plan3_local_save_bridge import DecodedPlan3LocalSave
from .plan3_native_search import (PLAN3_NATIVE_ENUMERATOR_AUTHORITY, Plan3NativeLegalCandidateEnumeration,
                                  enumerate_plan3_native_legal_candidates)
from .plan3_native_search_bridge import DEFAULT_SUPPORT_CARD_MASTER, search_plan3_native_decoded_local_save


@dataclass(frozen=True)
class RuntimeNiaScope:
    produce_id: str
    idol_card_id: str
    step_type: str
    audition_number: int
    difficulty_row_id: str
    gimmick_group_id: str
    exam_sha256: str
    identity_source: str
    produce_type: str = ""
    scoring_authority: str = "native serialized per-attribute bonuses and NPC score rows"

    def to_dict(self):
        return dict(vars(self))


@dataclass(frozen=True)
class RuntimeNiaComposition:
    idol_card_id: str
    difficulty_key: NiaExamSaveDifficultyKey
    serialized_turn_parameter_types: tuple[str, ...]
    projection: object
    native_scope: RuntimeNiaScope


def bind_runtime_audition_identity(raw: Mapping[str, object], native_context=None, *, expected_produce_id=None,
                                   expected_idol_card_id=None, master_dir=DEFAULT_MASTER_DIR):
    """Bind observed identity once, shared by Plan1 and Plan3 DLL loops."""
    if not isinstance(raw, Mapping):
        raise ValueError("native Exam serializer root must be an object")
    native = native_context
    if native is not None and not isinstance(native, Mapping):
        raise ValueError("same-snapshot native produce context is invalid")
    native = native or {}
    produce_id = raw.get("produceId")
    if not isinstance(produce_id, str) or not produce_id:
        raise ValueError("native Exam produceId is missing")
    mode = load_runtime_mode_profile(produce_id, master_dir=Path(master_dir))
    if raw.get("examType") == 0 or raw.get("stepType") in {*range(1, 10), *range(29, 35)}:
        raise ValueError("native-lesson-projection-unavailable: examType=0 uses a fixed lesson axis, empty audition schedule, "
                         "score clear/limit borders, and a lesson policy stage; it cannot be bound to Mid1")
    if raw.get("examType") != 1:
        raise ValueError("native audition requires examType=1")
    step_number, step_type = _step_type(raw.get("stepType"))
    if native.get("produce_id") is not None and native["produce_id"] != produce_id:
        raise ValueError("native Produce and Exam produce identity disagree")
    if native.get("step_type") is not None and native["step_type"] != step_number:
        raise ValueError("native Produce and Exam stage disagree")
    number = native.get("audition_number")
    if number is not None and (type(number) is not int or number <= 0):
        raise ValueError("native current audition number is invalid")
    idol_identity = native.get("idol_card_id")
    if idol_identity is not None and (not isinstance(idol_identity, str) or not idol_identity):
        raise ValueError("native current idol-card identity is invalid")
    directory = Path(master_dir).resolve()
    cards = _collect_produce_card_ids(raw)
    idol_rows = [row for row in _master_rows(directory / "IdolCard.yaml")
                 if row.get("characterId") == raw.get("characterId")
                 and ({"ProducePlanType_Plan1": 2, "ProducePlanType_Plan2": 3, "ProducePlanType_Plan3": 4}.get(row.get("planType")) == raw.get("planType"))
                 and ((idol_identity is not None and row.get("id") == idol_identity)
                      or (idol_identity is None and (row.get("produceCardId") in cards or row.get("secondProduceCardId") in cards)))]
    difficulty_rows = _master_rows(directory / "ProduceStepAuditionDifficulty.yaml")
    configs = {row["id"]: row for row in _master_rows(directory / "ProduceExamBattleConfig.yaml")}
    gimmicks = _runtime_gimmick_group_ids(raw)
    npc_rows = raw.get("npcDataList")
    if not isinstance(npc_rows, list) or any(not isinstance(row, Mapping) for row in npc_rows):
        raise ValueError("native NPC rows are incomplete")
    npc_groups = {row.get("_id") for row in npc_rows if row.get("_id")}
    matches = []
    for idol in idol_rows:
        for row in difficulty_rows:
            if (row.get("id") != idol.get("produceStepAuditionDifficultyId") or row.get("produceId") != produce_id
                    or row.get("stepType") != step_type or (number is not None and row.get("number") != number)):
                continue
            config = configs.get(row.get("produceExamBattleConfigId"))
            if config is None:
                continue
            if gimmicks and gimmicks != {row.get("produceExamGimmickEffectGroupId")}:
                continue
            if npc_groups and npc_groups != {row.get("produceExamBattleNpcGroupId")}:
                continue
            # Native selected Number is authoritative; without it the actual
            # config is additional identity evidence, never an assumed default.
            if number is None and any(raw.get(raw_key) != config.get(master_key) for raw_key, master_key in
                                      (("limitTurn", "turn"), ("vocalConfigParameter", "vocal"),
                                       ("danceConfigParameter", "dance"), ("visualConfigParameter", "visual"))):
                continue
            matches.append((idol, row))
    if len(matches) != 1:
        raise ValueError(f"native Exam and Master do not resolve exactly one idol/audition identity: {len(matches)} matches")
    idol, row = matches[0]
    # Expectations validate observed identity; they never narrow an ambiguous
    # candidate set or supply absent runtime fields.
    if expected_produce_id is not None and produce_id != expected_produce_id:
        raise ValueError("native produce_id differs from selected mode")
    if expected_idol_card_id is not None and idol["id"] != expected_idol_card_id:
        raise ValueError("native idol_card_id differs from selected idol")
    return {"idol_card_id": idol["id"], "produce_id": produce_id, "step_type": step_type,
            "audition_number": row["number"], "difficulty_row_id": row["id"],
            "gimmick_group_id": row["produceExamGimmickEffectGroupId"], "produce_type": mode.produce_type,
            "identity_source": ("same-snapshot-native-produce-context+Master" if number is not None else "native-Exam-unique-Master-match")}


def compose_runtime_audition(decoded: DecodedPlan3LocalSave, *, expected_produce_id=None,
                              expected_idol_card_id=None, master_dir=DEFAULT_MASTER_DIR):
    raw = json.loads(decoded.envelope.plaintext)
    identity = bind_runtime_audition_identity(raw, getattr(decoded.envelope, "native_context", None),
        expected_produce_id=expected_produce_id, expected_idol_card_id=expected_idol_card_id, master_dir=master_dir)
    key = NiaExamSaveDifficultyKey(identity["difficulty_row_id"], identity["produce_id"], identity["step_type"], identity["audition_number"])
    projection = project_runtime_exam_save_audition({"examSaveData": raw, "idolCardId": identity["idol_card_id"],
        "difficultyKey": {"id": key.row_id, "produceId": key.produce_id, "stepType": key.step_type, "number": key.number}},
        master_dir=Path(master_dir))
    digest = hashlib.sha256(decoded.envelope.plaintext).hexdigest()
    scope = RuntimeNiaScope(identity["produce_id"], identity["idol_card_id"], identity["step_type"], key.number, key.row_id,
                            identity["gimmick_group_id"], digest, identity["identity_source"], identity["produce_type"])
    return RuntimeNiaComposition(identity["idol_card_id"], key, _serialized_turn_types(raw, raw["limitTurn"]), projection, scope)


def compose_runtime_nia(decoded: DecodedPlan3LocalSave, **kwargs):
    """Compatibility name for the shared native audition composer."""
    return compose_runtime_audition(decoded, **kwargs)


class RuntimeNiaPlan3CandidateProvider:
    def __init__(self, *, expected_produce_id=None, expected_idol_card_id=None,
                 database=DEFAULT_DATABASE, master_dir=DEFAULT_MASTER_DIR,
                 support_card_master=DEFAULT_SUPPORT_CARD_MASTER):
        self.expected_produce_id, self.expected_idol_card_id = expected_produce_id, expected_idol_card_id
        self.database, self.master_dir, self.support_card_master = Path(database), Path(master_dir), Path(support_card_master)
        self._cached_key, self._cached_result = None, None
        self.context = None
        self.search_extensions = None
        self.root_result = None

    def _compose(self, decoded):
        return compose_runtime_nia(decoded, expected_produce_id=self.expected_produce_id,
            expected_idol_card_id=self.expected_idol_card_id, master_dir=self.master_dir)

    def __call__(self, decoded):
        if not isinstance(decoded, DecodedPlan3LocalSave):
            raise TypeError("native Plan3 candidates require one decoded native observation")
        key = (decoded.envelope.plaintext, json.dumps(getattr(decoded.envelope, "native_context", None), sort_keys=True))
        if key == self._cached_key:
            return self._cached_result
        scope = None
        self.context, self.search_extensions, self.root_result = None, None, None
        try:
            composition = self._compose(decoded)
            scope = composition.native_scope
            raw = json.loads(decoded.envelope.plaintext)
            self.context = _validate_static_runtime_context(composition, raw, prefer_exam_save_runtime=True,
                                                            database=self.database, master_dir=self.master_dir)
            if decoded.exam_state.phase != 6:
                raise ValueError("native Exam is outside its decision phase")
            # The shared gateway owns native Main/queue readiness. In
            # particular an exhausted play budget may still offer END_TURN;
            # the old disk-save 'not isTurnCardPlayEnd' gate is not authority.
            turn_start, accepted_play, runtime = _nia_search_extensions(composition, decoded, raw,
                                                                       database=self.database, master_dir=self.master_dir)
            self.search_extensions = (turn_start, accepted_play, runtime)
            prepared = search_plan3_native_decoded_local_save(decoded, raw_exam_save=raw, beam_width=1, depth=0,
                database=self.database, support_card_master=self.support_card_master,
                turn_start_extension=turn_start, accepted_play_extension=accepted_play,
                initial_turn_start_extension_state=runtime)
            if prepared.issues or prepared.native_state is None or not prepared.projection.exact:
                raise ValueError("native Plan3 projection incomplete: " + ",".join(f"{issue.code}:{issue.detail}" for issue in prepared.issues))
            settings = load_plan3_exam_settings(decoded.exam_state.setting_id, master_dir=self.master_dir)
            context = parse_plan3_audition_context(raw, turn_end_stamina_recovery=settings.turn_end_stamina_recovery)
            if context.full_horizon_gaps:
                raise ValueError("native Plan3 horizon gaps: " + ",".join(gap.code for gap in context.full_horizon_gaps))
            logical = bind_plan3_audition_state(prepared.projection.state, context)
            enumeration = enumerate_plan3_native_legal_candidates(logical, prepared.native_state,
                root_result_observer=lambda root: setattr(self, "root_result", root),
                settings=settings, database=self.database, support_upgrades=prepared.support_upgrades,
                support_card_searches=dict(prepared.support_card_searches),
                battle_parameter_schedule=tuple(frame.parameter_type for frame in context.remaining_frames),
                battle_ranking_resolved=True, include_drinks=True, include_turn_end=True,
                force_end_score=context.force_end_score if context.force_end_score > 0 else 0,
                force_end_stamina_recovery=context.turn_end_stamina_recovery if context.force_end_score > 0 else 0,
                drink_inventory=load_plan3_drink_inventory(context.drink_ids, master_dir=self.master_dir, database=self.database),
                turn_start_extension=prepared.turn_start_extension, accepted_play_extension=prepared.accepted_play_extension,
                initial_turn_start_extension_state=prepared.initial_turn_start_extension_state)
        except (KeyError, TypeError, ValueError, OSError) as error:
            enumeration = Plan3NativeLegalCandidateEnumeration((), PLAN3_NATIVE_ENUMERATOR_AUTHORITY, False,
                                                              (f"native-audition-preparation:{type(error).__name__}:{error}",))
        result = NiaPlan3NativeCandidateResult(enumeration, scope)  # type: ignore[arg-type]
        self._cached_key, self._cached_result = key, result
        return result


def build_runtime_nia_learned_advisor(provider, *, bundle, offline_rl=None, source="dll"):
    """Exact enumeration -> RL/BC, without a redundant full-horizon advisor."""
    from .plan3_learned_policy_runtime import _stage, _state_before, select_plan3_learned_policy_action

    def advisor(decoded):
        result = provider(decoded)
        context = None if result.scope is None else result.scope.to_dict()
        current = {**decoded.exam_state.to_dict(), "native_audition_context": context}
        if context is not None and context.get("produce_type") == "ProduceType_NextIdolAudition":
            current["nia_context"] = context  # Existing NIA dashboard compatibility.
        stage = _stage(decoded)
        search = {"decision_owner": "learned-policy", "state_source": "same-native-observation",
                  "enumeration_complete": result.complete}
        try:
            if not result.complete:
                raise ValueError(",".join(result.blockers))
            if stage is None:
                raise ValueError("canonical-stage-unavailable")
            selection = select_plan3_learned_policy_action(source=decoded.exam_state, state_before=_state_before(decoded),
                enumeration=result.enumeration, stage=stage, bundle=bundle, offline_rl=offline_rl)
            search["learned_policy"] = selection.to_dict()
            if not selection.ready or selection.executor_action is None:
                raise ValueError(",".join(selection.blockers) or "no learned decision")
            action = dict(selection.executor_action)
            return Plan3AuditionAdvisorReport("ready", (), current, (action,), action,
                {"forecast_available": False, "source": "learned-action-only"}, search, (), source)
        except (KeyError, TypeError, ValueError) as error:
            return Plan3AuditionAdvisorReport("unavailable", (Plan3AuditionAdvisorIssue("native-learned-decision-unavailable", str(error)),),
                                             current, (), None, None, search, (), source)
    return advisor


class RuntimeAuditionPlan3CandidateProvider(RuntimeNiaPlan3CandidateProvider):
    def _compose(self, decoded):
        return compose_runtime_audition(decoded, expected_produce_id=self.expected_produce_id,
            expected_idol_card_id=self.expected_idol_card_id, master_dir=self.master_dir)


RuntimeAuditionScope = RuntimeNiaScope
RuntimeAuditionComposition = RuntimeNiaComposition
build_runtime_audition_learned_advisor = build_runtime_nia_learned_advisor


def choose_runtime_audition_rule_action(decoded, *, provider=None):
    """Choose one evaluated present action; wider-horizon failures stay separate.

    This uses the unpruned root paths already produced by the same native
    projection. It does not turn simulator coverage into game legality; the
    input owner must intersect/revalidate the returned identity with its fresh
    native legal pool. Unsupported local paths remain explicitly unscored.
    """
    from .plan3_advisor import classify_plan3_phase, score_plan3_transition, score_plan3_state_delta
    from .plan3_native_search import _canonical_root_candidate

    detail = {"action": None, "decision_owner": "rule-planner", "learned_policy_used": False,
        "policy": "native-plan3-fullpower-present-action-v1", "state_source": "same-native-observation",
        "evaluation_scope": "one-present-action", "predicts_exam_score": False, "full_horizon_success": False,
        "future_evaluation_status": "not-requested",
        "local_evaluations": [], "unscored_candidates": [], "root_diagnostics": [], "future_diagnostics": [],
        "native_legal_revalidation_required": True}
    try:
        raw = json.loads(decoded.envelope.plaintext)
        if raw.get("produceId") != "produce-004" or raw.get("planType") != 4 or raw.get("mainEffectType") != 47:
            raise ValueError("present-action rule requires NIA Pro / Plan3 / true ExamFullPower=47")
        provider = RuntimeAuditionPlan3CandidateProvider(expected_produce_id="produce-004") if provider is None else provider
        result = provider(decoded)
        detail["enumeration_complete"] = result.complete
        detail["preparation_gaps"] = list(result.blockers)
        detail["native_audition_context"] = None if result.scope is None else result.scope.to_dict()
        root = provider.root_result
        if root is None:
            raise ValueError("same-observation root outcomes are unavailable")
        detail["root_diagnostics"] = [{"stage": diagnostic.stage, "gaps": list(diagnostic.semantic_gaps),
                                       "card_guid": diagnostic.card_guid} for diagnostic in root.diagnostics]
        legal_ids = {candidate.action_id for candidate in result.enumeration.candidates}
        scored = []
        for index, path in enumerate(root.candidates):
            candidate, blockers = _canonical_root_candidate(path)
            if candidate is None:
                detail["unscored_candidates"].append({"path_index": index, "gaps": list(blockers)})
                continue
            identity = candidate.action_id
            if identity not in legal_ids:
                detail["unscored_candidates"].append({"action_id": identity, "gaps": ["not-in-projected-root-candidate-set"]})
                continue
            # A present-action result must represent one actual native step.
            # Branch-specific dispatcher limitations are reported independently
            # of the local arithmetic for the future native input owner.
            step = path.decision_steps[0]
            try:
                phase = classify_plan3_phase(step.before, decoded.exam_state.limit_turn)
                if step.kind == "card":
                    if step.card_transition is None:
                        raise ValueError("card outcome is unavailable")
                    value = score_plan3_transition(step.card_transition, phase)
                elif step.kind in {"drink", "skip"}:
                    value = score_plan3_state_delta(step.before, step.after, phase,
                        extra_plays=max(0, step.after.plays_remaining - step.before.plays_remaining))
                else:
                    raise ValueError("unknown current action kind")
                local = {"action_id": identity, "action": candidate.to_dict(), "value": value.total,
                    "ranking_key": [value.total, candidate.kind != "drink", -index], "path_index": index,
                    "phase": phase, "components": [{"key": item.key, "value": item.value, "reason": item.reason}
                                                    for item in value.components],
                    "score_delta": step.after.score - step.before.score,
                    "stamina_delta": step.after.stamina - step.before.stamina,
                    "dispatch_gaps": list(blockers), "path_diagnostics": [path.stopped_reason] if path.stopped_reason else [],
                    "value_kind": "local-rule-ranking-proxy", "terminal_prediction": None,
                    "automatic_next_turn_evaluated": False}
                detail["local_evaluations"].append(local)
                # Keep an evaluated branch with expressible secondary targets.
                # The legacy Maa driver blocker is not a simulator failure;
                # native intersection/continuation binding remains mandatory.
                scored.append((value.total, candidate.kind != "drink", -index, candidate))
            except (KeyError, TypeError, ValueError, AttributeError) as error:
                detail["unscored_candidates"].append({"action_id": identity, "gaps": [str(error)]})
        if not scored:
            raise ValueError("no candidate has a usable present-action evaluation")
        evaluated_ids = {row["action_id"] for row in detail["local_evaluations"]}
        unscored_ids = {row.get("action_id") for row in detail["unscored_candidates"]}
        for diagnostic in root.diagnostics:
            if diagnostic.card_guid is not None:
                identity = "PLAY:" + diagnostic.card_guid
                if identity not in evaluated_ids and identity not in unscored_ids:
                    detail["unscored_candidates"].append({"action_id": identity,
                        "gaps": [f"{diagnostic.stage}:{gap}" for gap in diagnostic.semantic_gaps]})
                    unscored_ids.add(identity)
        chosen = max(scored, key=lambda item: item[:3])[-1]
        detail.update(action=chosen.to_dict(), status="ready", chosen_action_id=chosen.action_id)
        return detail
    except (KeyError, TypeError, ValueError, OSError, AttributeError, IndexError) as error:
        return {**detail, "status": "unavailable", "reason": str(error)}


def build_runtime_audition_rule_advisor(provider, *, beam_width=8, source="dll-native-audition-rule"):
    """Explicit NIA Pro FullPower rule actor; no model fallback or flow alias.

    The same provider must first prove the root projection and full legal set.
    The existing horizon engine receives its exact mode/gimmick extensions.
    Unimplemented mechanics and an unbound first action remain unavailable.
    This interface alone does not grant a run-start or live-acceptance claim.
    """
    from .plan3_audition_advisor import build_plan3_audition_advisor_report
    from .plan3_audition_search_bridge import search_plan3_audition_horizon_decoded
    from .plan3_learned_policy_runtime import _executor_action

    if type(beam_width) is not int or not 1 <= beam_width <= 64:
        raise ValueError("native rule beam width must be 1..64")

    def advisor(decoded):
        raw = json.loads(decoded.envelope.plaintext)
        current = decoded.exam_state.to_dict()
        search = {"decision_owner": "rule-planner", "learned_policy_used": False,
                  "policy": "native-plan3-fullpower-audition-rules", "beam_width": beam_width,
                  "state_source": "same-native-observation", "full_rollout_verified": False}
        try:
            if raw.get("produceId") != "produce-004" or raw.get("mainEffectType") != 47 or raw.get("planType") != 4:
                raise ValueError("explicit rule route requires NIA Pro / Plan3 / ExamFullPower=47")
            result = provider(decoded)
            scope = None if result.scope is None else result.scope.to_dict()
            current = {**current, "native_audition_context": scope, "nia_context": scope}
            search["enumeration_complete"] = result.complete
            if not result.complete:
                raise ValueError(",".join(result.blockers))
            if provider.search_extensions is None:
                raise ValueError("same-observation search extensions are unavailable")
            turn_start, accepted_play, runtime = provider.search_extensions
            planned = search_plan3_audition_horizon_decoded(decoded, raw_exam_save=raw,
                beam_width=beam_width, include_drinks=True, database=provider.database,
                master_dir=provider.master_dir, support_card_master=provider.support_card_master,
                turn_start_extension=turn_start, accepted_play_extension=accepted_play,
                initial_turn_start_extension_state=runtime)
            report = build_plan3_audition_advisor_report(planned, source=source,
                                                       beam_width=beam_width, include_drinks=True)
            if not report.available:
                return replace(report, current_state=current, search={**report.search, **search})
            step = planned.search.best.decision_steps[0]
            matches = []
            for candidate in result.enumeration.candidates:
                if candidate.native_kind != step.kind:
                    continue
                if step.kind == "card" and (candidate.card_guid != step.action.guid or
                        candidate.selected_card_guids != step.action.selected_card_guids):
                    continue
                if step.kind == "drink" and (candidate.drink_slot_index != step.drink_action.slot_index or
                        candidate.drink_id != step.drink_action.drink_id or
                        candidate.selected_card_guid != step.drink_action.selected_card_guid):
                    continue
                matches.append(candidate)
            if len(matches) != 1:
                raise ValueError("rule first action does not bind one exact native candidate, including secondary targets")
            action = _executor_action(matches[0])
            return replace(report, current_state=current, first_action=action,
                best_decision_steps=(action, *report.best_decision_steps[1:]),
                search={**report.search, **search, "native_action_id": matches[0].action_id},
                terminal={**report.terminal, "forecast_available": False,
                          "estimate_kind": "unvalidated-rule-rollout"})
        except (KeyError, TypeError, ValueError, OSError, AttributeError) as error:
            return Plan3AuditionAdvisorReport("unavailable",
                (Plan3AuditionAdvisorIssue("native-rule-decision-unavailable", str(error)),),
                current, (), None, None, search, (), source)

    return advisor
