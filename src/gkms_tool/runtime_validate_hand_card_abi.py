"""Offline decoder and purity guard for the current-PC hand validator.

This module mirrors the C++ ABI contract in
``native/runtime_exam_recorder/src/validate_use_hand_card_abi.hpp``.  It is
deliberately pure: it reads bytes/scalars supplied by a caller and never
loads a DLL, opens a process, invokes IL2CPP, or infers legality from a card
identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


ABI_SCHEMA = "gkms.runtime-exam-validate-use-hand-card-abi.v1"
CURRENT_PC_METHOD_TOKEN = 0x060043FB
CURRENT_PC_METHOD_DEFINITION_INDEX = 17_402
CURRENT_PC_PARAMETER_COUNT = 2
RETURN_SIZE = 8
ERROR_MIN = 0
ERROR_MAX = 5
ERROR_VALUES = {
    0: "None",
    1: "CardRestrict",
    2: "PlayCardLimit",
    3: "StaminaShort",
    4: "CostShort",
    5: "NotPlayable",
}


@dataclass(frozen=True, slots=True)
class DecodedReturn:
    """One raw RAX value decoded as ValueTuple<bool, enum>.

    Bytes 1..3 are ValueTuple padding and intentionally ignored.  The low
    byte is the managed ``Item1`` bool; bytes 4..7 are the little-endian
    Int32 underlying ``ExamUseHandErrorType``.
    """

    raw: int
    valid_byte: int
    error_bits: int
    error: int
    bool_encoding_valid: bool
    error_encoding_valid: bool
    abi_shape_valid: bool
    valid: bool


def decode_raw_return(raw: int) -> DecodedReturn:
    """Decode an unsigned 64-bit Microsoft x64 RAX return value.

    ``valid`` is never true for an invalid bool/enum encoding.  This strict
    behavior prevents a wrong return declaration from silently turning an
    arbitrary register value into a legal hand action.
    """

    if not isinstance(raw, int) or raw < 0 or raw >= (1 << 64):
        raise ValueError("raw return must be an unsigned 64-bit integer")
    valid_byte = raw & 0xFF
    error_bits = (raw >> 32) & 0xFFFFFFFF
    error = error_bits if error_bits < (1 << 31) else error_bits - (1 << 32)
    bool_encoding_valid = valid_byte in (0, 1)
    error_encoding_valid = ERROR_MIN <= error <= ERROR_MAX
    abi_shape_valid = bool_encoding_valid and error_encoding_valid
    return DecodedReturn(
        raw=raw,
        valid_byte=valid_byte,
        error_bits=error_bits,
        error=error,
        bool_encoding_valid=bool_encoding_valid,
        error_encoding_valid=error_encoding_valid,
        abi_shape_valid=abi_shape_valid,
        valid=abi_shape_valid and valid_byte == 1,
    )


def decode_return_bytes(raw: bytes | bytearray | memoryview) -> DecodedReturn:
    """Decode exactly the eight bytes copied from a native return value."""

    data = bytes(raw)
    if len(data) != RETURN_SIZE:
        raise ValueError(f"return must contain exactly {RETURN_SIZE} bytes")
    return decode_raw_return(int.from_bytes(data, "little", signed=False))


@dataclass(frozen=True, slots=True)
class PuritySnapshot:
    """Copied state/scalar evidence around one context lifetime.

    ``state_bytes`` must be the exact UTF-8 bytes produced by the managed
    snapshot serializer.  A caller must copy those bytes on the managed
    thread; this class never retains managed pointers.
    """

    state_bytes: bytes | None
    rng_state: int | None
    phase: int | None
    command_playing: bool | None
    command_stack_empty: bool | None
    turn_card_play_end: bool | None
    context_created: bool
    dispose_attempted: bool
    dispose_succeeded: bool


def compare_purity(
    before: PuritySnapshot, after: PuritySnapshot
) -> dict[str, object]:
    """Return an explicit fail-closed before/after purity result.

    The result is diagnostic evidence only.  In particular, ``equal=True``
    does not promote ``legal``; the native validator's return ABI and the
    two-pass decoded result agreement are separate gates.
    """

    blockers: list[str] = []
    if before.state_bytes is None:
        blockers.append("validator-purity-before-snapshot-unavailable")
    if after.state_bytes is None:
        blockers.append("validator-purity-after-snapshot-unavailable")
    state_equal = (
        before.state_bytes is not None
        and after.state_bytes is not None
        and before.state_bytes == after.state_bytes
    )
    if before.state_bytes is not None and after.state_bytes is not None and not state_equal:
        blockers.append("validator-enumeration-mutated-state")

    if before.rng_state is None:
        blockers.append("validator-purity-before-rng-unavailable")
    if after.rng_state is None:
        blockers.append("validator-purity-after-rng-unavailable")
    rng_equal = (
        before.rng_state is not None
        and after.rng_state is not None
        and before.rng_state == after.rng_state
    )
    if before.rng_state is not None and after.rng_state is not None and not rng_equal:
        blockers.append("validator-enumeration-mutated-rng")

    scalar_before = (
        before.phase,
        before.command_playing,
        before.command_stack_empty,
        before.turn_card_play_end,
    )
    scalar_after = (
        after.phase,
        after.command_playing,
        after.command_stack_empty,
        after.turn_card_play_end,
    )
    scalar_captured = all(value is not None for value in scalar_before + scalar_after)
    scalar_equal = scalar_captured and scalar_before == scalar_after
    if not scalar_captured:
        blockers.append("validator-purity-scalar-unavailable")
    elif not scalar_equal:
        blockers.append("validator-enumeration-mutated-settlement-scalars")

    # The pre-snapshot is captured before CreateEffectResolver and must not
    # claim that a context already exists.  ``context_created`` is therefore
    # an operation result recorded alongside the post-snapshot.
    if not after.context_created:
        blockers.append("validator-context-not-created")
    if not after.dispose_attempted:
        blockers.append("validator-context-dispose-not-attempted")
    elif not after.dispose_succeeded:
        blockers.append("validator-context-dispose-failed")

    return {
        "before_captured": before.state_bytes is not None,
        "after_captured": after.state_bytes is not None,
        "state_equal": state_equal,
        "before_rng_captured": before.rng_state is not None,
        "after_rng_captured": after.rng_state is not None,
        "rng_equal": rng_equal,
        "scalar_captured": scalar_captured,
        "scalar_equal": scalar_equal,
        "context_created": after.context_created,
        "dispose_attempted": after.dispose_attempted,
        "dispose_succeeded": after.dispose_succeeded,
        "equal": not blockers,
        "blockers": blockers,
    }


def contract_dict() -> dict[str, object]:
    """Return a JSON-ready immutable-contract summary for offline audits."""

    return {
        "schema": ABI_SCHEMA,
        "status": "offline-contract-only",
        "method": {
            "type": "Campus.InGame.Exam.ExamCardUtility",
            "name": "ValidateUseHandCard",
            "token": f"0x{CURRENT_PC_METHOD_TOKEN:08X}",
            "method_definition_index": CURRENT_PC_METHOD_DEFINITION_INDEX,
            "parameter_count": CURRENT_PC_PARAMETER_COUNT,
            "managed_signature": (
                "static System.ValueTuple<System.Boolean, "
                "Campus.InGame.Exam.ExamUseHandErrorType> "
                "ValidateUseHandCard(System.Int32 index, "
                "Campus.InGame.Exam.ExamEffectCalculateContext context)"
            ),
            "native_signature": (
                "System_ValueTuple_bool__ExamUseHandErrorType__o "
                "(int32_t index, "
                "Campus_InGame_Exam_ExamEffectCalculateContext_o* context, "
                "const MethodInfo* method)"
            ),
        },
        "windows_x64": {
            "calling_convention": "Microsoft x64 (__cdecl spelling is normalized)",
            "arguments": {
                "RCX": "int32_t index",
                "RDX": "ExamEffectCalculateContext* context",
                "R8": "const MethodInfo* method",
                "R9": "unused",
            },
            "return_register": "RAX",
            "return_size_bytes": RETURN_SIZE,
            "hidden_sret_pointer": False,
            "stack_home_space_bytes": 32,
        },
        "value_tuple_layout": {
            "size_bytes": RETURN_SIZE,
            "alignment_bytes": 4,
            "Item1": {"offset": 0, "size_bytes": 1, "type": "bool"},
            "padding": {"offset": 1, "size_bytes": 3, "must_be_zero": False},
            "Item2": {
                "offset": 4,
                "size_bytes": 4,
                "type": "int32 enum underlying type",
                "little_endian": True,
            },
        },
        "return_decode": {
            "valid_byte": "raw & 0xFF; accepted values 0 or 1",
            "error_bits": "(raw >> 32) & 0xFFFFFFFF",
            "error": "signed Int32(error_bits), accepted values 0..5",
            "padding_policy": "ignore bytes 1..3; never use them for legality",
            "legal_policy": "only decoded valid=true may enter a future hand set",
        },
        "error_enum_values": [
            {"value": value, "name": name}
            for value, name in ERROR_VALUES.items()
        ],
        "purity_guard": {
            "before": "serialized state bytes + RNG + phase/busy/stack/turn-end scalars",
            "after": "same copies after Dispose(context)",
            "must_be_equal": [
                "state bytes",
                "RNG state",
                "phase",
                "command_playing",
                "command_stack_empty",
                "turn_card_play_end",
            ],
            "lifetime": [
                "CreateEffectResolver exactly once per pass",
                "Dispose exactly once in finally/cleanup",
                "no managed pointer retained by writer",
            ],
            "two_pass_rule": "fresh context per pass; compare decoded (valid,error) per slot",
            "failure_policy": "legal=null and legal_actions_complete=false",
        },
        "runtime_status": {
            "validator_called": False,
            "hand_legal_promoted": False,
            "static_pc_rva_authority": False,
        },
    }


def fixture_decode_rows() -> tuple[Mapping[str, object], ...]:
    """Small deterministic rows used by static/offline tests."""

    return (
        {"raw": 0x0000000000000001, "valid": True, "error": 0},
        {"raw": 0x0000000300000000, "valid": False, "error": 3},
        # Non-zero tuple padding is ignored; Item1/Item2 still decode cleanly.
        {"raw": 0x0000000500A1FF00, "valid": False, "error": 5},
        {"raw": 0x0000000000000002, "valid": False, "error": 0},
        {"raw": 0x0000000600000000, "valid": False, "error": 6},
    )
