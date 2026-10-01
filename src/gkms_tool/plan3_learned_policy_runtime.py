"""Fail-closed Plan3 RL -> BC decision owner over exact native actions.

The module deliberately has no controller implementation.  It converts one
complete Plan3 native root enumeration into the shared plan-neutral learned
selector contract and returns the exact action mapping already consumed by
``MaaPlan3ActionDriver``.  When neither learned policy returns a decision the
advisor is made unavailable, so the existing executor sends zero input.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, Final

from .canonical_training_labels import KNOWN_READINESS_V5_FLOWS, STAGE_BY_NATIVE_VALUE
from .exact_bc_live_canary import ExactBCCanaryPolicy, ExactBCPolicyBundle
from .exact_exam_state_projection import project_exact_exam_native_state
from .learned_policy_selector import (
    LearnedPolicyDecision,
    OfflineRLSelector,
    select_plan_neutral_learned_action,
)
from .plan3_native_search import (
    Plan3NativeLegalCandidate,
    Plan3NativeLegalCandidateEnumeration,
)
from .runtime_canonical_json import canonical_sha256
from .unified_legal_action_snapshot import (
    UnifiedLegalActionSnapshot,
    build_unified_legal_action_snapshot,
)


SCHEMA: Final = "gkms.plan3-learned-policy-runtime.v1"


@dataclass(frozen=True, slots=True)
class Plan3LearnedPolicyIssue:
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class Plan3LearnedPolicySelection:
    decision: LearnedPolicyDecision | None
    snapshot: UnifiedLegalActionSnapshot | None
    state_before: Mapping[str, object] | None
    native_candidate: Plan3NativeLegalCandidate | None
    executor_action: Mapping[str, object] | None
    blockers: tuple[str, ...] = ()
    schema: str = SCHEMA

    @property
    def ready(self) -> bool:
        return bool(
            self.decision is not None
            and self.decision.ready
            and self.snapshot is not None
            and self.snapshot.complete
            and self.native_candidate is not None
            and self.executor_action is not None
            and not self.blockers
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "ready": self.ready,
            "source": None if self.decision is None else self.decision.source,
            "confidence": (
                None if self.decision is None else self.decision.probability
            ),
            "action_id": (
                None if self.decision is None else self.decision.action_id
            ),
            "policy_id": (
                None if self.decision is None else self.decision.policy_id
            ),
            "blockers": list(self.blockers),
            "decision": (
                None if self.decision is None else self.decision.to_dict()
            ),
            "snapshot": (
                None if self.snapshot is None else self.snapshot.to_dict()
            ),
        }


def _executor_action(candidate: Plan3NativeLegalCandidate) -> dict[str, object]:
    if candidate.kind == "play":
        return {
            "kind": "card",
            "hand_index": candidate.hand_index,
            "card_guid": candidate.card_guid,
            "card_id": candidate.card_id,
            "card_upgrade": candidate.card_upgrade,
            **({"selected_card_guids": list(candidate.selected_card_guids)} if candidate.selected_card_guids else {}),
        }
    if candidate.kind == "drink":
        return {
            "kind": "drink",
            "drink_slot_index": candidate.drink_slot_index,
            "drink_id": candidate.drink_id,
            "selected_card_guid": candidate.selected_card_guid,
        }
    return {"kind": "skip"}


def select_plan3_learned_policy_action(
    *,
    source: object,
    state_before: Mapping[str, Any],
    enumeration: Plan3NativeLegalCandidateEnumeration,
    stage: str,
    bundle: ExactBCPolicyBundle,
    offline_rl: OfflineRLSelector | None = None,
    flow: str | None = None,
    policy: ExactBCCanaryPolicy | None = None,
) -> Plan3LearnedPolicySelection:
    """Select RL first, BC second, and bind the exact Plan3 typed action."""

    if not isinstance(enumeration, Plan3NativeLegalCandidateEnumeration):
        raise TypeError("Plan3 learned runtime requires a typed enumeration")
    if policy is None:
        policy = ExactBCCanaryPolicy(
            enabled=True,
            allowed_flows=tuple(sorted(KNOWN_READINESS_V5_FLOWS)),
            minimum_probability=0.0,
            minimum_margin=0.0,
        )
    try:
        snapshot = build_unified_legal_action_snapshot(
            source,
            flow=flow,
            native_enumerator=lambda _source: enumeration,
            source_label="plan3-exact-native-learned-runtime",
        )
    except Exception as error:
        return Plan3LearnedPolicySelection(
            None,
            None,
            dict(state_before),
            None,
            None,
            (f"unified-snapshot-failed:{type(error).__name__}:{error}",),
        )
    learned = select_plan_neutral_learned_action(
        state_before=state_before,
        snapshot=snapshot,
        stage=stage,
        offline_rl=offline_rl,
        bc_bundle=bundle,
        bc_policy=policy,
    )
    if not learned.ready or learned.action_id is None:
        return Plan3LearnedPolicySelection(
            learned,
            snapshot,
            dict(state_before),
            None,
            None,
            learned.blockers or ("learned-policy-no-decision",),
        )
    matches = tuple(
        value
        for value in enumeration.candidates
        if value.action_id == learned.action_id
    )
    if len(matches) != 1:
        return Plan3LearnedPolicySelection(
            learned,
            snapshot,
            dict(state_before),
            None,
            None,
            ("learned-action-native-identity-mismatch",),
        )
    candidate = matches[0]
    return Plan3LearnedPolicySelection(
        learned,
        snapshot,
        dict(state_before),
        candidate,
        _executor_action(candidate),
    )


def _stage(decoded: object) -> str | None:
    state = getattr(decoded, "exam_state", None)
    value = getattr(state, "step_type_value", None)
    return STAGE_BY_NATIVE_VALUE.get(value) if isinstance(value, int) else None


def _state_before(decoded: object) -> dict[str, object]:
    state = getattr(decoded, "exam_state", None)
    to_dict = getattr(state, "to_dict", None)
    if not callable(to_dict):
        raise ValueError("Plan3 decoded state has no typed serialization")
    return project_exact_exam_native_state(to_dict())


def _native_card_payload(card: object) -> dict[str, object]:
    return {
        "_guid": getattr(card, "guid"),
        "_playCount": getattr(card, "play_count", 0),
        "_baseUpgradeCount": getattr(card, "base_upgrade", 0),
        "_tmpUpgradeCount": getattr(card, "temporary_upgrade", 0),
        "_supportUpgradeIdList": list(getattr(card, "support_upgrade_ids", ())),
        "_fixedDeckOrder": getattr(card, "fixed_deck_order", 0),
        "_statusEffect": {},
        "_affectGrowEffectIdList": list(
            getattr(card, "runtime_grow_effect_ids", ())
        ),
        "_growEffectExamStartAfterList": [],
        "_isMoveProduceExamEffectUseInTurn": getattr(
            card, "move_effect_used_in_turn", False
        ),
        "_staminaConsumptionSpecifyEffectList": [],
        "_cardData": {
            "_id": getattr(card, "card_id"),
            "_upgradeCount": getattr(card, "effective_upgrade", 0),
            "_customizeCountList": [],
            "_produceCardSkinId": "",
            "_produceCardSkinAssetId": "",
        },
    }


def project_plan3_replay_learned_state(
    decoded: object,
    logical_state: object,
    native_state: object,
) -> dict[str, object]:
    """Project a replay-proven next decision without consulting an advisor."""

    result = deepcopy(_state_before(decoded))
    scalar_fields = {
        "parameter": "score",
        "score": "score",
        "stamina": "stamina",
        "maxStamina": "max_stamina",
        "block": "block",
        "remainTurn": "turns_remaining",
        "playsRemaining": "plays_remaining",
        "fullPowerPoint": "full_power_points",
        "fullPowerPointGetSumCount": "full_power_points_total",
        "enthusiasm": "enthusiasm",
        "enthusiasmAdditive": "enthusiasm_additive",
    }
    for target, source in scalar_fields.items():
        value = getattr(logical_state, source, None)
        if value is not None:
            result[target] = value
    result["isTurnCardPlayEnd"] = getattr(logical_state, "plays_remaining", 0) <= 0
    result["random"] = getattr(native_state, "random_state")
    result["totalDrawCardCount"] = getattr(
        native_state, "total_effect_draw_card_count", 0
    )
    for target, source in (
        ("handList", "hand"),
        ("deckList", "deck"),
        ("graveList", "grave"),
        ("lostList", "lost"),
        ("holdList", "hold"),
    ):
        result[target] = [
            _native_card_payload(card) for card in getattr(native_state, source)
        ]
    result["playingCard"] = None
    return result


def _apply_selection_to_report(base: object, selection: Plan3LearnedPolicySelection):
    search = dict(getattr(base, "search", {}) or {})
    search["decision_owner"] = "learned-policy"
    search["learned_policy"] = selection.to_dict()
    if not selection.ready or selection.executor_action is None:
        detail = ",".join(selection.blockers) or "no learned decision"
        return replace(
            base,
            status="unavailable",
            issues=(Plan3LearnedPolicyIssue("learned-policy-no-decision", detail),),
            best_decision_steps=(),
            first_action=None,
            terminal=None,
            diagnostics=(),
            search=search,
        )
    action = dict(selection.executor_action)
    return replace(
        base,
        issues=(),
        best_decision_steps=(action,),
        first_action=action,
        diagnostics=(),
        search=search,
    )


def build_nia_plan3_learned_executor_options(
    options: Mapping[str, object],
    *,
    current_candidate_provider: Callable[[object], object],
    replay_candidate_provider: Callable[[object, object], tuple[object, object, object]],
    bundle: ExactBCPolicyBundle,
    offline_rl: OfflineRLSelector | None = None,
) -> dict[str, object]:
    """Replace NIA advisor action ownership while preserving Maa execution."""

    base_advisor = options.get("advisor_factory")
    base_replay_advisor = options.get("replay_advisor_factory")
    if not callable(base_advisor) or not callable(base_replay_advisor):
        raise TypeError("NIA Plan3 executor options lack advisor seams")

    def advisor(decoded: object):
        base = base_advisor(decoded)
        if not getattr(base, "available", False):
            return base
        stage = _stage(decoded)
        if stage is None:
            selection = Plan3LearnedPolicySelection(
                None, None, None, None, None, ("canonical-stage-unavailable",)
            )
        else:
            result = current_candidate_provider(decoded)
            enumeration = getattr(result, "enumeration", result)
            try:
                selection = select_plan3_learned_policy_action(
                    source=getattr(decoded, "exam_state"),
                    state_before=_state_before(decoded),
                    enumeration=enumeration,
                    stage=stage,
                    bundle=bundle,
                    offline_rl=offline_rl,
                )
            except Exception as error:
                selection = Plan3LearnedPolicySelection(
                    None,
                    None,
                    None,
                    None,
                    None,
                    (f"learned-selection-failed:{type(error).__name__}:{error}",),
                )
        return _apply_selection_to_report(base, selection)

    def replay_advisor(decoded: object, replay: object):
        base = base_replay_advisor(decoded, replay)
        if not getattr(base, "available", False):
            return base
        stage = _stage(decoded)
        try:
            enumeration, logical, native = replay_candidate_provider(decoded, replay)
            state_before = project_plan3_replay_learned_state(
                decoded, logical, native
            )
            flow = "|".join(
                str(state_before[key])
                for key in ("produceId", "planType", "mainEffectType")
            )
            # Native integer enums must use the canonical flow spelling.
            from .canonical_training_labels import (
                EFFECT_BY_NATIVE_VALUE,
                PLAN_BY_NATIVE_VALUE,
            )

            parts = flow.split("|")
            parts[1] = PLAN_BY_NATIVE_VALUE.get(state_before["planType"], parts[1])
            parts[2] = EFFECT_BY_NATIVE_VALUE.get(
                state_before["mainEffectType"], parts[2]
            )

            class _ReplayBoundary:
                state = logical

                @staticmethod
                def digest() -> str:
                    return canonical_sha256(state_before)

            if stage is None:
                raise ValueError("canonical-stage-unavailable")
            selection = select_plan3_learned_policy_action(
                source=_ReplayBoundary(),
                state_before=state_before,
                enumeration=enumeration,
                stage=stage,
                bundle=bundle,
                offline_rl=offline_rl,
                flow="|".join(parts),
            )
        except Exception as error:
            selection = Plan3LearnedPolicySelection(
                None,
                None,
                None,
                None,
                None,
                (f"learned-replay-selection-failed:{type(error).__name__}:{error}",),
            )
        return _apply_selection_to_report(base, selection)

    result = dict(options)
    result["advisor_factory"] = advisor
    result["replay_advisor_factory"] = replay_advisor
    return result


__all__ = [
    "Plan3LearnedPolicyIssue",
    "Plan3LearnedPolicySelection",
    "SCHEMA",
    "build_nia_plan3_learned_executor_options",
    "project_plan3_replay_learned_state",
    "select_plan3_learned_policy_action",
]
