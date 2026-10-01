"""Present-action FullPower policy, with native legality independent of forecasts."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path

from .master_db import DEFAULT_DATABASE
from .passive_catalog import DEFAULT_MASTER_DIR
from .runtime_native_legal_inputs import parse_runtime_native_legal_inputs


FULLPOWER_FLOW = "produce-004|ProducePlanType_Plan3|ProduceExamEffectType_ExamFullPower"
FULLPOWER_NATIVE_SCOPE_SCHEMA = "gkms.native-primary-trained-scope.v1"
FULLPOWER_STAGES = ("Mid1", "Mid2", "Final")


def _verified_fullpower_component_scope(bundle, role, required_stages):
    """A declaration grants only its hash-bound, evaluated true47 component."""
    component = bundle.component(role)
    declaration = _fullpower_component_declaration(component, role, bundle.payload.get("training_spec_sha256"), required_stages)
    path = Path(declaration.get("evaluation_path", ""))
    if not path.is_absolute():
        path = Path(bundle.project_root) / path
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != declaration.get("evaluation_sha256"):
        raise ValueError("FullPower stage evaluation hash mismatch")
    evaluation = json.loads(content)
    if (evaluation.get("schema") != "gkms.fullpower-candidate-evaluation.v1"
            or evaluation.get("offline_candidate_ready") is not True
            or evaluation.get("all_stage_heldout_nonregression") is not True
            or evaluation.get("blockers") != []
            or any(evaluation.get(key) != declaration[key] for key in ("model_sha256", "report_sha256", "training_spec_sha256"))):
        raise ValueError("FullPower stage evaluation is not ready or bound to this component")
    for stage in required_stages:
        for split in ("train", "validation", "test"):
            cell = evaluation.get("stage_split_metrics", {}).get(stage, {}).get(split, {})
            if (type(cell.get("count")) is not int or cell["count"] <= 0
                    or cell.get("illegal_rate") != 0.0
                    or type(cell.get("improvement_pp")) not in (int, float)
                    or not math.isfinite(cell["improvement_pp"])
                    or split != "train" and cell["improvement_pp"] < -5.0):
                raise ValueError("FullPower stage evaluation lacks qualified heldout support: " + stage + ":" + split)
    return declaration


def _fullpower_component_declaration(component, role, spec_sha256, required_stages):
    """Check declaration compatibility before loading an optional artifact."""
    declaration = component.get("native_primary_scope")
    if not isinstance(declaration, Mapping):
        raise ValueError(role + ": explicit native_primary_scope is absent")
    expected = {"schema": FULLPOWER_NATIVE_SCOPE_SCHEMA, "candidate_scope": "primary-inputs-only",
        "stage_kind": "audition", "flow": FULLPOWER_FLOW, "validation_status": "offline-validated",
        "model_sha256": component.get("model_sha256"), "report_sha256": component.get("report_sha256"),
        "training_spec_sha256": spec_sha256}
    if any(not expected[k] or declaration.get(k) != expected[k] for k in expected):
        raise ValueError(role + ": native primary declaration differs from component/spec/flow")
    if component.get("role") != role or any(component.get(key) is not True for key in ("enabled", "shadow_ready")):
        raise ValueError(role + ": component role/readiness is not enabled")
    stages = declaration.get("stages")
    if (not isinstance(stages, list) or not stages or len(stages) != len(set(stages))
            or any(stage not in FULLPOWER_STAGES for stage in stages) or not set(required_stages).issubset(stages)):
        raise ValueError(role + ": native primary declaration lacks evaluated stages")
    return dict(declaration)


def resolve_fullpower_native_primary_route(bundle, *, required_stages=FULLPOWER_STAGES):
    """Pure before-run check; a partial/absent training cohort keeps rules.

    The bundle must already be loaded through PolicyBundle's component hash
    and model/report validators. This extra route checks the actual training
    inventory and the explicit candidate-only deployment declaration. It does
    not activate a model or equate offline validation with a live pass.
    """
    result = {"status": "native-rules", "flow": FULLPOWER_FLOW, "available": False,
        "learned_model_used": False, "allow_offline_rl": False, "live_pass_verified": False,
        "predicts_exam_score": False, "required_stages": list(required_stages) if isinstance(required_stages, (tuple, list)) else [], "blockers": []}
    try:
        from .canonical_training_labels import STAGE_BY_NAME
        if not isinstance(required_stages, (tuple, list)) or not required_stages:
            raise ValueError("FullPower route requires explicit known audition stages")
        required_stages = tuple(STAGE_BY_NAME.get(stage, stage) for stage in required_stages)
        if any(stage not in FULLPOWER_STAGES for stage in required_stages) or len(set(required_stages)) != len(required_stages):
            raise ValueError("FullPower route requires explicit known audition stages")
        result["required_stages"] = list(required_stages)
        declaration = _verified_fullpower_component_scope(bundle, "exact_exam_policy", required_stages)
        from .policy_mode_coverage import resolve_exam_policy_mode_route

        trained = resolve_exam_policy_mode_route(bundle, produce_id="produce-004",
            plan_type="ProducePlanType_Plan3", main_effect_type="ProduceExamEffectType_ExamFullPower",
            required_stages=required_stages)
        if not trained.available or trained.status != "trained" or not trained.validated_for_target:
            raise ValueError("FullPower needs its own train/validation/test stage coverage: " + "; ".join(trained.blockers))
        path = Path(bundle.payload["training_spec_path"])
        if not path.is_absolute():
            path = Path(bundle.project_root) / path
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != bundle.payload["training_spec_sha256"]:
            raise ValueError("FullPower training spec changed")
        spec = json.loads(content)
        training_scope = spec.get("training_scope", {})
        if (training_scope.get("schema") != "gkms.training-scope.v1"
                or FULLPOWER_FLOW not in training_scope.get("flows", ())
                or "ProducePlanType_Plan3" not in training_scope.get("plan_types", ())
                or training_scope.get("split_unit") != "trajectory-and-capture-source"):
            raise ValueError("FullPower source-separated training scope is missing")
        result.update(status="native-learned", available=True, declaration=declaration,
            bundle_id=getattr(bundle, "bundle_id", None), training_scope=dict(training_scope))
        try:
            rl = _verified_fullpower_component_scope(bundle, "offline_rl_policy", required_stages)
            component = bundle.component("offline_rl_policy")
            if component.get("runtime_enabled") is not True:
                raise ValueError("FullPower offline RL runtime is disabled")
            result.update(allow_offline_rl=True, offline_rl_declaration=rl)
        except (KeyError, ValueError, TypeError, AttributeError, OSError) as error:
            # An old five-flow RL component must never lend its prior to 47.
            result["offline_rl_reason"] = str(error)
    except (KeyError, ValueError, TypeError, AttributeError, OSError) as error:
        result["blockers"].append(str(error))
    return result


def build_runtime_fullpower_policy(*, produce_id, idol_card_id, policy_bundle_path=None):
    """Explicit selected bundle only; active defaults cannot silently promote 47."""
    if policy_bundle_path is None:
        return RuntimeFullPowerPolicy(produce_id=produce_id, idol_card_id=idol_card_id,
            model_route={"status": "native-rules", "reason": "no-explicit-FullPower-bundle"})
    selected_path = Path(policy_bundle_path)
    selected_payload = json.loads(selected_path.read_text(encoding="utf-8-sig")) if selected_path.is_file() else {}
    if "fullpower_current_state_trial" in selected_payload:
        from .runtime_fullpower_current_bc import RuntimeFullPowerCurrentBCPolicy
        return RuntimeFullPowerCurrentBCPolicy(selected_payload["fullpower_current_state_trial"],
            project_root=Path(__file__).resolve().parents[2], produce_id=produce_id, idol_card_id=idol_card_id)
    from .policy_bundle import PolicyBundle

    bundle = PolicyBundle.load(Path(policy_bundle_path), component_roles=("exact_exam_policy",))
    route = resolve_fullpower_native_primary_route(bundle)
    if not route["available"]:
        return RuntimeFullPowerPolicy(produce_id=produce_id, idol_card_id=idol_card_id, model_route=route)
    offline_rl = None
    # Only validate/load optional RL if its explicit scope matches first.
    raw_rl = (bundle.payload.get("components") or {}).get("offline_rl_policy", {})
    try:
        _fullpower_component_declaration(raw_rl, "offline_rl_policy", bundle.payload.get("training_spec_sha256"), FULLPOWER_STAGES)
    except (KeyError, ValueError, TypeError, AttributeError) as error:
        route["offline_rl_reason"] = str(error)
    else:
        candidate = PolicyBundle.load(Path(policy_bundle_path), component_roles=("exact_exam_policy", "offline_rl_policy"))
        candidate_route = resolve_fullpower_native_primary_route(candidate)
        if candidate_route["allow_offline_rl"]:
            from .offline_rl_runtime_adapter import build_offline_rl_artifact_selector

            offline_rl = build_offline_rl_artifact_selector(candidate.component("offline_rl_policy"), project_root=candidate.project_root)
            bundle, route = candidate, candidate_route
    return RuntimeFullPowerNativeObservationPolicy(bundle, produce_id=produce_id,
        idol_card_id=idol_card_id, offline_rl=offline_rl, model_route=route)


class RuntimeFullPowerNativeObservationPolicy:
    """Scoped NN and optional local drink refinement; the shared loop executes."""

    def __init__(self, bc_bundle, *, produce_id, idol_card_id, offline_rl=None,
                 model_route=None, drink_evaluator=None):
        self.produce_id, self.idol_card_id, self.bc_bundle = produce_id, idol_card_id, bc_bundle
        # Revalidate at construction even if a caller has cached route metadata.
        route = resolve_fullpower_native_primary_route(bc_bundle)
        if produce_id != "produce-004" or not route["available"]:
            raise ValueError("FullPower learned route is not validated: " + "; ".join(route["blockers"]))
        self.model_route = route
        self.offline_rl = offline_rl if route["allow_offline_rl"] else None
        self.drink_evaluator = drink_evaluator
        self.last_drink_decision = None

    def choose_observation(self, observed):
        from .exact_bc_live_canary import ExactBCCanaryPolicy
        from .runtime_native_primary_nn import NativePrimaryNNScope, select_runtime_native_primary_nn
        from .runtime_native_observation_policy import RuntimeNativeObservationAction, SECONDARY_POLICY

        raw = observed.raw_state
        actual_idol = (observed.native_context or {}).get("idol_card_id") or (raw or {}).get("idolCardId")
        if actual_idol != self.idol_card_id:
            raise ValueError("FullPower learned policy idol differs from the observed game")
        scope = NativePrimaryNNScope("ProducePlanType_Plan3", ("ProduceExamEffectType_ExamFullPower",),
            "gkms.runtime-fullpower-native-primary-nn.v1", "FullPower-NN-requires-NIA-Pro-and-true47",
            ("produce-004",), native_plan_value=4, native_main_effect_values=(47,))
        actor = select_runtime_native_primary_nn(observed, scope=scope, bc_bundle=self.bc_bundle,
            offline_rl=self.offline_rl, bc_policy=ExactBCCanaryPolicy(enabled=True,
                allowed_flows=(FULLPOWER_FLOW,), minimum_probability=0.0, minimum_margin=0.0))
        self.last_drink_decision = None
        if not actor.ready:
            return FullPowerLearnedDecision(actor, None, self.model_route)
        action = RuntimeNativeObservationAction(actor.action.action_id, actor.action.native_input,
            None if actor.action.kind == "end_turn" else SECONDARY_POLICY)
        # Optional value work cannot weaken the native NN boundary. Both the
        # proposed PLAY and a replacement DRINK must have successful local
        # evaluations in the same units; a Master-only estimate is never used.
        if action.kind == "play" and any(value.kind == "drink" for value in actor.native_pool.actions):
            try:
                from .runtime_plan3_executor import decoded_runtime_plan3

                if self.drink_evaluator is None:
                    rule = RuntimeFullPowerPolicy(produce_id=self.produce_id, idol_card_id=self.idol_card_id)
                    self.drink_evaluator = rule.evaluator
                report = self.drink_evaluator(decoded_runtime_plan3(observed))
                rows = report.get("local_evaluations", ())
                selected_rows = [row for row in rows if row.get("action", {}).get("card_guid") == action.card_guid
                    and not row.get("action", {}).get("selected_card_guid") and not row.get("action", {}).get("selected_card_guids")]
                comparison = {"source": "same-observation-FullPower-local-drink-refinement",
                    "candidate": None, "native_revision": actor.native_pool.revision,
                    "native_sequence_id": actor.native_pool.sequence_key,
                    "boundary_digest": actor.boundary_digest, "predicts_exam_score": False,
                    "simulator_assessment": report, "ranking_units_mixed": False}
                if (len(selected_rows) != 1 or selected_rows[0].get("dispatch_gaps")
                        or type(selected_rows[0].get("value")) not in (int, float) or not math.isfinite(selected_rows[0]["value"])):
                    comparison["reason"] = "NN-play-has-no-unique-local-evaluation"
                else:
                    baseline = selected_rows[0]["value"]
                    secondary_guids = tuple(card["_guid"] for zone in ("handList", "deckList", "graveList", "lostList", "holdList")
                        for card in raw.get(zone, ()) if isinstance(card, Mapping) and isinstance(card.get("_guid"), str))
                    reserve = {16: 2, 17: 1, 18: 0}.get(raw.get("stepType"), 3)
                    options = []
                    for index, row in enumerate(rows):
                        proposed = row.get("action", {})
                        if proposed.get("kind") not in {"drink", "use-drink"}:
                            continue
                        bound = _bind(actor.native_pool, proposed, secondary_guids)
                        value = row.get("value")
                        if (bound is None or bound.selected_card_guid is not None
                                or type(value) not in (int, float) or not math.isfinite(value)
                                or value <= max(0, baseline)
                                or row.get("dispatch_gaps")
                                or _reserve_drink(raw, len(actor.native_pool.drinks), reserve, row.get("stamina_delta", 0))):
                            continue
                        options.append((value, -index, bound))
                    comparison.update(actor_local_value=baseline, reserve_count=reserve)
                    if options:
                        value, _, selected = max(options, key=lambda row: row[:2])
                        native = actor.native_pool.lookup("drink", slot=selected.slot_index, drink_id=selected.drink_id)
                        matches = [row for row in actor.learned_snapshot.candidates if row["kind"] == "drink"
                            and row["slot_index"] == selected.slot_index and row["drink_id"] == selected.drink_id]
                        if len(matches) != 1:
                            raise ValueError("local drink has no unique current NN primary identity")
                        action = RuntimeNativeObservationAction(matches[0]["action_id"], native, SECONDARY_POLICY)
                        comparison.update(candidate=action.to_dict(), drink_local_value=value,
                            reason="strictly-better-evaluated-local-drink-before-NN-play")
                self.last_drink_decision = comparison
            except (ValueError, TypeError, KeyError, AttributeError, OSError) as error:
                self.last_drink_decision = {"candidate": None, "reason": "optional-FullPower-drink-refinement-unavailable",
                    "blockers": [f"{type(error).__name__}:{error}"]}
        return FullPowerLearnedDecision(actor, action, self.model_route, self.last_drink_decision)


@dataclass(frozen=True)
class FullPowerLearnedDecision:
    actor: object
    best_action: object | None
    model_route: Mapping
    drink_comparison: Mapping | None = None

    @property
    def blockers(self):
        return self.actor.blockers

    @property
    def policy_source(self):
        return ("native-fullpower-local-drink-refinement-v1" if self.drink_comparison
            and self.drink_comparison.get("candidate") is not None else self.actor.source)

    @property
    def policy_legal_action_ids(self):
        return () if self.actor.learned_snapshot is None else self.actor.learned_snapshot.action_ids

    @property
    def rule_applied(self):
        return self.drink_comparison is not None and self.drink_comparison.get("candidate") is not None

    @property
    def policy_probability(self):
        return None if self.rule_applied else self.actor.probability

    @property
    def metadata(self):
        refined = self.drink_comparison is not None and self.drink_comparison.get("candidate") is not None
        return {"schema": "gkms.runtime-fullpower-native-observation-policy.v1",
            "actor": self.actor.to_dict(), "model_route": dict(self.model_route),
            "learned_model_used": self.actor.ready, "predicts_exam_score": False,
            "action_selection": {"source": self.policy_source, "rule_applied": refined,
                "actor_action_id": None if self.actor.action is None else self.actor.action.action_id,
                "selected_action_id": None if self.best_action is None else self.best_action.action_id,
                "probability": None if refined else self.actor.probability},
            "drink_comparison": self.drink_comparison, "secondary_selection_complete": False,
            "training_promotion_allowed": False, "owns_input": False}


@dataclass(frozen=True)
class NativeRuleAction:
    kind: str
    card_guid: str | None = None
    slot_index: int | None = None
    drink_id: str | None = None
    selected_card_guid: str | None = None
    secondary_policy: str | None = None

    @property
    def action_id(self):
        if self.kind == "play":
            return f"PLAY:{self.card_guid}"
        if self.kind == "drink":
            return f"DRINK:{self.slot_index}:{self.drink_id}"
        return "END_TURN"


@dataclass(frozen=True)
class NativeRuleDecision:
    best_action: NativeRuleAction | None
    policy_source: str
    policy_legal_action_ids: tuple[str, ...]
    metadata: Mapping
    blockers: tuple = ()


def _bind(pool, proposed, secondary_guids=()):
    kind = {"card": "play", "use-hand": "play", "use-drink": "drink", "skip": "end_turn",
            "turn-end": "end_turn"}.get(proposed.get("kind"), proposed.get("kind"))
    if kind == "play":
        actual = pool.lookup("play", card_guid=proposed.get("card_guid"))
    elif kind == "drink":
        actual = pool.lookup("drink", slot=proposed.get("slot_index", proposed.get("drink_slot_index")),
                             drink_id=proposed.get("drink_id"))
    elif kind == "end_turn":
        actual = pool.lookup("end_turn")
    else:
        return None
    if actual is None:
        return None
    selected = proposed.get("selected_card_guid") or None
    selected_many = proposed.get("selected_card_guids", ())
    if selected_many:
        if not isinstance(selected_many, (list, tuple)) or len(selected_many) != 1 or selected is not None and selected != selected_many[0]:
            return None
        selected = selected_many[0]
    if selected is not None and (not isinstance(selected, str) or secondary_guids.count(selected) != 1 or kind == "end_turn"):
        return None
    return NativeRuleAction(kind, actual.target.get("card_guid"), actual.target.get("slot"),
                            actual.target.get("drink_id"), selected)


def _master_feature_value(features, raw):
    """Shared fallback scale; separate from a simulated present-action value."""
    turns = max(1, int(raw.get("remainTurn", 1)))
    missing_hp = max(0, raw.get("maxStamina", 0) - raw.get("stamina", 0))
    value = (features.direct + features.gains.get("full_power", 0) * min(6, turns) * 2
             + features.gains.get("enthusiasm", 0) * min(6, turns)
             + features.extra_play * 12 + features.draw * 3 + min(missing_hp, features.recovery) * 2
             + features.gains.get("block", 0) * .4 - features.stamina - features.force_stamina * 2
             + features.residual_prior)
    return value


def _master_card_fallback(card, raw):
    """A labeled ranking proxy when the simulator cannot evaluate this card.

    The game's validator already checked affordability and play restrictions.
    No unknown listener is assumed to execute or to be a no-op in a forecast.
    """
    from .deck_value import _features

    features = _features({"card_id": card.card_id, "upgrade": card.effective_upgrade}, DEFAULT_DATABASE, DEFAULT_MASTER_DIR)
    value = _master_feature_value(features, raw)
    return value, {"value_kind": "master-semantic-ranking-proxy", "value": value,
                   "unscored_effects": sorted(features.unknown),
                   "limitations": ["runtime-grow-and-listener-interactions-not-forecast", "not-an-exam-score"]}


def _master_drink_fallback(identity, raw):
    """Do not erase a native-legal drink because another root effect is unknown.

    This reuses the same Master feature scale as card fallback, retains every
    unknown effect, and never claims to have simulated recovery or listeners.
    """
    from .deck_value import _drink_features

    features = _drink_features(identity, DEFAULT_DATABASE)
    value = _master_feature_value(features, raw)
    recovery_capacity = min(max(0, raw.get("maxStamina", 0) - raw.get("stamina", 0)), max(0, features.recovery))
    return value, {"value_kind": "master-semantic-ranking-proxy", "value": value,
        "unscored_effects": sorted(features.unknown), "known_recovery_capacity": recovery_capacity,
        "observed_stamina_delta_available": False,
        "limitations": ["drink-and-listener-interactions-not-forecast", "not-an-exam-score",
                        "known-effects-only; unknown-effects-not-assumed-no-op"]}


def _reserve_drink(raw, count, reserve, recovery):
    return count <= reserve and not (recovery > 0 and raw.get("stamina", 0) <= raw.get("maxStamina", 0) // 4)


class RuntimeFullPowerPolicy:
    """A pure policy consumed by the existing shared native executor loop."""

    def __init__(self, *, produce_id, idol_card_id, evaluator=None, model_route=None):
        self.produce_id, self.idol_card_id = produce_id, idol_card_id
        self.model_route = model_route
        if evaluator is None:
            from .runtime_nia_plan3 import RuntimeAuditionPlan3CandidateProvider, choose_runtime_audition_rule_action
            provider = RuntimeAuditionPlan3CandidateProvider(expected_produce_id=produce_id, expected_idol_card_id=idol_card_id)
            self.evaluator = lambda decoded: choose_runtime_audition_rule_action(decoded, provider=provider)
        else:
            self.evaluator = evaluator

    def choose_observation(self, observed):
        from .runtime_plan3_executor import decoded_runtime_plan3
        from .runtime_exam_executor import native_step_context_digest

        raw, evidence = observed.raw_state, observed.evidence
        if (not isinstance(raw, Mapping) or evidence is None or self.produce_id != "produce-004"
                or raw.get("produceId") != self.produce_id or raw.get("planType") != 4 or raw.get("mainEffectType") != 47):
            raise ValueError("native rule policy requires true NIA Pro FullPower identity")
        actual_idol = (observed.native_context or {}).get("idol_card_id") or raw.get("idolCardId")
        if actual_idol != self.idol_card_id:
            raise ValueError("native rule policy idol differs from the observed game")
        pool = parse_runtime_native_legal_inputs(observed.native_snapshot,
            session_generation=observed.native_session_generation,
            expected_sequence_id=evidence.session_transition_id.removeprefix("native-sequence:"))
        if pool.exam_save_sha256 != evidence.source_sha256 or not pool.boundary_ready:
            raise ValueError("native input pool does not belong to the current actionable evidence")
        if evidence.step_context_digest != native_step_context_digest(observed.native_snapshot, observed.native_session_generation):
            raise ValueError("native input pool session/stage identity differs from its evidence")
        metadata = {"flow": FULLPOWER_FLOW, "native_legality": pool.to_dict(),
                    "predicts_exam_score": False, "learned_model_used": False, "evaluations": []}
        if self.model_route is not None:
            metadata["model_route"] = self.model_route
        try:
            evaluated = self.evaluator(decoded_runtime_plan3(observed))
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            evaluated = {"local_evaluations": [], "root_diagnostics": [f"{type(error).__name__}:{error}"]}
        metadata["simulator_assessment"] = evaluated
        choices, fallbacks, seen, excluded = [], [], set(), set()
        secondary_guids = tuple(card["_guid"] for zone in ("handList", "deckList", "graveList", "lostList", "holdList")
            for card in raw.get(zone, ()) if isinstance(card, Mapping) and isinstance(card.get("_guid"), str))
        unknown_hand = any(row.get("can_use") is None for row in pool.hand)
        reserve = {16: 2, 17: 1, 18: 0}.get(raw.get("stepType"), 3)
        metadata["drink_policy"] = {"inventory_count": len(pool.drinks), "reserve_count": reserve,
            "scope": "current native slots", "missing_simulation_uses_explicit_Master_fallback": True}
        for index, row in enumerate(evaluated.get("local_evaluations", ())):
            proposed = row.get("action", {})
            action = _bind(pool, proposed, secondary_guids)
            if action is None and (proposed.get("selected_card_guid") or proposed.get("selected_card_guids")):
                # Known broken continuation must not reappear as a Master-only
                # fallback with its required GUID silently discarded.
                if proposed.get("card_guid"):
                    excluded.add("PLAY:" + proposed["card_guid"])
                elif proposed.get("kind") in {"drink", "use-drink"}:
                    primary = _bind(pool, {key: value for key, value in proposed.items()
                                         if key not in {"selected_card_guid", "selected_card_guids"}}, secondary_guids)
                    if primary is not None:
                        excluded.add(primary.action_id)
                metadata["evaluations"].append({"action": proposed, "reason": "secondary-target-not-bound-to-current-zones"})
            value = row.get("value")
            if action is None or type(value) not in (int, float) or not math.isfinite(value):
                continue
            seen.add(action.action_id)
            if action.kind == "end_turn" and unknown_hand:
                metadata["evaluations"].append({"action_id": action.action_id, "reason": "unresolved-hand-validation"})
                continue
            if action.kind == "drink" and _reserve_drink(raw, len(pool.drinks), reserve, row.get("stamina_delta", 0)):
                metadata["evaluations"].append({"action_id": action.action_id, "reason": "reserve-for-later-auditions"})
                continue
            choices.append((float(value), action.kind == "play", -index, action))
            metadata["evaluations"].append({"action_id": action.action_id, "value": value, "source": "evaluated-present-action"})
        # Partial simulation is explicitly visible. Current native-legal cards
        # still receive a cheap Master estimate instead of freezing the game.
        for native in pool.actions:
            if native.kind != "play":
                continue
            action = NativeRuleAction("play", native.target["card_guid"], native.target["slot"])
            if action.action_id in seen or action.action_id in excluded:
                continue
            card = evidence.state.zones.hand[native.target["slot"]]
            value, detail = _master_card_fallback(card, raw)
            fallbacks.append((value, True, -native.target["slot"], action))
            metadata["evaluations"].append({"action_id": action.action_id, **detail})
        for native in pool.actions:
            if native.kind != "drink":
                continue
            action = NativeRuleAction("drink", slot_index=native.target["slot"], drink_id=native.target["drink_id"])
            if action.action_id in seen or action.action_id in excluded:
                continue
            try:
                value, detail = _master_drink_fallback(action.drink_id, raw)
            except (KeyError, OSError, ValueError, TypeError) as error:
                metadata["evaluations"].append({"action_id": action.action_id, "source": "master-drink-fallback",
                    "value": None, "reason": "unscored-native-drink", "unscored_effects": [f"{type(error).__name__}:{error}"]})
                continue
            reserved = _reserve_drink(raw, len(pool.drinks), reserve, detail["known_recovery_capacity"])
            metadata["evaluations"].append({"action_id": action.action_id, "source": "master-drink-fallback", **detail,
                **({"reason": "reserve-for-later-auditions"} if reserved else {})})
            if not reserved and math.isfinite(value):
                fallbacks.append((value, False, -native.target["slot"], action))
        positive = [row for row in choices if row[0] > 0 and row[-1].kind != "end_turn"]
        if positive:
            chosen = max(positive, key=lambda row: row[:3])[-1]
            metadata["ranking_source"] = "evaluated-present-action"
        elif fallbacks and max(row[0] for row in fallbacks) > 0:
            chosen = max(fallbacks, key=lambda row: row[:3])[-1]
            metadata["ranking_source"] = "master-semantic-fallback"
        elif choices:
            chosen = max(choices, key=lambda row: row[:3])[-1]
            metadata["ranking_source"] = "evaluated-present-action"
        elif pool.lookup("end_turn") is not None and not unknown_hand:
            chosen = NativeRuleAction("end_turn")
        else:
            chosen = None
            metadata["reason"] = "no-valued-native-action-and-unresolved-hand-validation"
        metadata["ranking_units_mixed"] = False
        if chosen is not None and chosen.kind in {"play", "drink"} and chosen.selected_card_guid is None:
            chosen = replace(chosen, secondary_policy="native-card-choice-value-v1")
        legal_ids = tuple(NativeRuleAction(action.kind, action.target.get("card_guid"), action.target.get("slot"),
                                           action.target.get("drink_id")).action_id for action in pool.actions)
        return NativeRuleDecision(chosen, "native-fullpower-present-action-v1", legal_ids, metadata)
