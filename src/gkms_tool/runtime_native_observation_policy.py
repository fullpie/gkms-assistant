"""Shared immutable primary action/decision view; no actor or input loop."""
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

from .runtime_native_primary_nn import RuntimeNativePrimaryAction

RULE_SOURCE = "native-drink-before-play-v1"
SECONDARY_POLICY = "native-card-choice-value-v1"


@dataclass(frozen=True, slots=True)
class RuntimeNativeObservationAction(RuntimeNativePrimaryAction):
    secondary_policy: str | None = SECONDARY_POLICY

    def to_dict(self):
        return {**RuntimeNativePrimaryAction.to_dict(self), "secondary_policy": self.secondary_policy}


@dataclass(frozen=True, slots=True)
class RuntimeNativeObservationDecision:
    actor: object
    best_action: RuntimeNativeObservationAction | None
    drink_comparison: Mapping | None = None
    rule_applied: bool = False
    metadata_schema: ClassVar[str] = "gkms.runtime-native-observation-policy.v1"

    @property
    def blockers(self):
        return self.actor.blockers

    @property
    def decision(self):
        return self.actor.decision

    @property
    def policy_source(self):
        return RULE_SOURCE if self.rule_applied else self.actor.source

    @property
    def policy_probability(self):
        return None if self.rule_applied else self.actor.probability

    @property
    def policy_legal_action_ids(self):
        return () if self.actor.learned_snapshot is None else self.actor.learned_snapshot.action_ids

    @property
    def metadata(self):
        return {"schema": self.metadata_schema, "actor": self.actor.to_dict(),
            "action_selection": {"source": self.policy_source, "probability": self.policy_probability,
                "actor_action_id": None if self.actor.action is None else self.actor.action.action_id,
                "selected_action_id": None if self.best_action is None else self.best_action.action_id,
                "rule_applied": self.rule_applied},
            "drink_comparison": self.drink_comparison, "forecast_available": False,
            "owns_input": False, "secondary_selection_complete": False, "training_promotion_allowed": False}
