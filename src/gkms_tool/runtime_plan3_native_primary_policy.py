"""NIA Pro Plan3 Concentration NN scope; FullPower has no model alias here."""
from .runtime_native_primary_nn import NativePrimaryNNScope, select_runtime_native_primary_nn
from .runtime_native_observation_policy import (
    SECONDARY_POLICY, RuntimeNativeObservationAction, RuntimeNativeObservationDecision,
)

SCHEMA = "gkms.runtime-plan3-concentration-native-primary-nn.v1"
_SCOPE = NativePrimaryNNScope("ProducePlanType_Plan3", ("ProduceExamEffectType_ExamConcentration",),
    SCHEMA, "native-plan3-concentration-requires-NIA-Pro-and-true-effect-45", ("produce-004",),
    native_plan_value=4, native_main_effect_values=(45,))


def select_runtime_plan3_concentration_native_primary(observation, *, bc_bundle=None, offline_rl=None, bc_policy=None):
    return select_runtime_native_primary_nn(observation, scope=_SCOPE, bc_bundle=bc_bundle,
        offline_rl=offline_rl, bc_policy=bc_policy)


class RuntimePlan3ConcentrationObservationDecision(RuntimeNativeObservationDecision):
    __slots__ = ()
    metadata_schema = "gkms.runtime-plan3-concentration-native-observation-policy.v1"


class RuntimePlan3ConcentrationNativeObservationPolicy:
    def __init__(self, bc_bundle, *, offline_rl=None, bc_policy=None):
        self.bc_bundle, self.offline_rl, self.bc_policy = bc_bundle, offline_rl, bc_policy

    def choose_observation(self, observed):
        actor = select_runtime_plan3_concentration_native_primary(observed, bc_bundle=self.bc_bundle,
            offline_rl=self.offline_rl, bc_policy=self.bc_policy)
        action = None if not actor.ready else RuntimeNativeObservationAction(actor.action.action_id,
            actor.action.native_input, None if actor.action.kind == "end_turn" else SECONDARY_POLICY)
        return RuntimePlan3ConcentrationObservationDecision(actor, action)
