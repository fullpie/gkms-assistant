"""Backward-compatible Plan2 names for trigger-effect serializer progress.

The implementation is plan-neutral and lives in
:mod:`gkms_tool.trigger_effect_serializer_progress`.  This facade preserves
all established Plan2 imports as exact aliases.
"""

from __future__ import annotations

from .trigger_effect_serializer_progress import (
    INT32_MAX,
    INT32_MIN,
    Plan2NativeSerializerLifecyclePolicy,
    Plan2NativeSerializerProgress,
    Plan2NativeSerializerProgressError,
    TriggerEffectSerializerLifecyclePolicy,
    TriggerEffectSerializerProgress,
    TriggerEffectSerializerProgressError,
    is_identity_empty_trigger_item_scratch,
    restore_plan2_native_serializer_progress,
    restore_trigger_effect_serializer_progress,
)


__all__ = [
    "INT32_MAX",
    "INT32_MIN",
    "Plan2NativeSerializerLifecyclePolicy",
    "Plan2NativeSerializerProgress",
    "Plan2NativeSerializerProgressError",
    "TriggerEffectSerializerLifecyclePolicy",
    "TriggerEffectSerializerProgress",
    "TriggerEffectSerializerProgressError",
    "is_identity_empty_trigger_item_scratch",
    "restore_plan2_native_serializer_progress",
    "restore_trigger_effect_serializer_progress",
]
