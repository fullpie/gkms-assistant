"""Compatible Plan2 scope wrapper around the shared native-primary NN core."""
from .runtime_native_primary_nn import (
    AUTHORITY, NativePrimaryNNScope,
    RuntimeNativePrimaryAction as RuntimePlan2PrimaryAction,
    RuntimeNativePrimaryResult as RuntimePlan2NativePrimaryResult,
    select_runtime_native_primary_nn,
)

SCHEMA = "gkms.runtime-plan2-native-primary-nn.v1"
_SCOPE = NativePrimaryNNScope("ProducePlanType_Plan2",
    ("ProduceExamEffectType_ExamReview", "ProduceExamEffectType_ExamCardPlayAggressive"),
    SCHEMA, "native-plan2-audition-flow-or-stage-unavailable")


def select_runtime_plan2_native_primary(observation, *, bc_bundle=None, offline_rl=None, bc_policy=None) -> RuntimePlan2NativePrimaryResult:
    return select_runtime_native_primary_nn(observation, scope=_SCOPE, bc_bundle=bc_bundle,
        offline_rl=offline_rl, bc_policy=bc_policy)
