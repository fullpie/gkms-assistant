#pragma once

// Offline contract for the current-PC ExamCardUtility.ValidateUseHandCard
// return value.  This header deliberately contains no IL2CPP invocation,
// hook, thread, writer, or game bootstrap.  The recorder may include it for
// compile-time layout checks, while the actual validator remains bound-only
// until a controlled managed-thread probe proves the ABI and purity.

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <type_traits>

namespace gkms::il2cpp {
struct MethodInfo;
}

namespace gkms::runtime_exam_validator {

inline constexpr std::uint32_t kCurrentPcMethodToken = 0x060043FB;
inline constexpr std::uint32_t kCurrentPcMethodDefinitionIndex = 17402;
inline constexpr std::uint32_t kCurrentPcParameterCount = 2;
inline constexpr std::uint32_t kCurrentPcReturnSize = 8;
inline constexpr std::uint32_t kCurrentPcErrorMin = 0;
inline constexpr std::uint32_t kCurrentPcErrorMax = 5;

// This is the C++ shape emitted by IL2CPP for
// System.ValueTuple<Boolean, ExamUseHandErrorType>.  The Android generated
// header retained in the repository declares exactly these two fields.  The
// current PC metadata identifies the same System.ValueTuple`2 return type;
// current-PC executable code/ABI is still a runtime-probe question.
struct ValueTupleBoolExamUseHandErrorType final {
    bool item1;
    std::int32_t item2;
};

static_assert(std::is_standard_layout_v<ValueTupleBoolExamUseHandErrorType>);
static_assert(std::is_trivially_copyable_v<ValueTupleBoolExamUseHandErrorType>);
static_assert(sizeof(bool) == 1);
static_assert(sizeof(ValueTupleBoolExamUseHandErrorType) == 8);
static_assert(alignof(ValueTupleBoolExamUseHandErrorType) == 4);
static_assert(offsetof(ValueTupleBoolExamUseHandErrorType, item1) == 0);
static_assert(offsetof(ValueTupleBoolExamUseHandErrorType, item2) == 4);

// IL2CPP appends const MethodInfo* to generated managed method signatures.
// __cdecl names the Microsoft x64 convention explicitly; on Win64 it is
// normalized to the platform's single x64 convention by the compiler.
#if defined(_MSC_VER)
#define GKMS_RUNTIME_EXAM_MS_X64_CALL __cdecl
#else
#define GKMS_RUNTIME_EXAM_MS_X64_CALL
#endif

using validate_use_hand_card_fn = ValueTupleBoolExamUseHandErrorType
    (GKMS_RUNTIME_EXAM_MS_X64_CALL *)(
        std::int32_t index,
        void* context,
        const gkms::il2cpp::MethodInfo* method);

// The aggregate is eight bytes, so the Microsoft x64 ABI returns it directly
// in RAX (there is no hidden sret pointer for this shape).  This function is
// only a typed declaration for a future managed-thread probe; no caller is
// present in the current recorder.

struct DecodedReturn final {
    std::uint64_t raw{};
    std::uint8_t valid_byte{};
    std::uint32_t error_bits{};
    std::int32_t error{};
    bool bool_encoding_valid{};
    bool error_encoding_valid{};
    bool abi_shape_valid{};
    bool valid{};
};

inline DecodedReturn decode_raw_return(std::uint64_t raw) noexcept {
    DecodedReturn result;
    result.raw = raw;
    result.valid_byte = static_cast<std::uint8_t>(raw & 0xFFU);
    result.error_bits = static_cast<std::uint32_t>(raw >> 32U);
    std::memcpy(&result.error, &result.error_bits, sizeof(result.error));
    result.bool_encoding_valid = result.valid_byte == 0U ||
        result.valid_byte == 1U;
    result.error_encoding_valid = result.error >=
            static_cast<std::int32_t>(kCurrentPcErrorMin) &&
        result.error <= static_cast<std::int32_t>(kCurrentPcErrorMax);
    result.abi_shape_valid = result.bool_encoding_valid &&
        result.error_encoding_valid;
    result.valid = result.abi_shape_valid && result.valid_byte == 1U;
    return result;
}

inline DecodedReturn decode_return(
    const ValueTupleBoolExamUseHandErrorType& value) noexcept {
    std::uint64_t raw{};
    static_assert(sizeof(raw) == sizeof(value));
    std::memcpy(&raw, &value, sizeof(raw));
    return decode_raw_return(raw);
}

// A future managed-thread adapter fills this with copies made before and
// after one complete context lifetime.  The byte span is borrowed only for
// the immediate comparison; no pointer may escape to a writer thread.
struct PuritySnapshot final {
    const std::uint8_t* state_bytes{};
    std::size_t state_size{};
    bool state_captured{};

    bool rng_captured{};
    std::uint32_t rng_state{};

    bool phase_captured{};
    std::int32_t phase{};
    bool command_playing_captured{};
    bool command_playing{};
    bool command_stack_empty_captured{};
    bool command_stack_empty{};
    bool turn_card_play_end_captured{};
    bool turn_card_play_end{};

    // The context is rented/returned by the probe itself.  These fields are
    // lifetime guards, not managed state substitutes.
    bool context_created{};
    bool dispose_attempted{};
    bool dispose_succeeded{};
};

enum PurityFailure : std::uint32_t {
    kPurityClean = 0U,
    kPurityBeforeStateMissing = 1U << 0,
    kPurityAfterStateMissing = 1U << 1,
    kPurityStateBytesDiffer = 1U << 2,
    kPurityBeforeRngMissing = 1U << 3,
    kPurityAfterRngMissing = 1U << 4,
    kPurityRngDiffer = 1U << 5,
    kPurityBeforeScalarMissing = 1U << 6,
    kPurityAfterScalarMissing = 1U << 7,
    kPurityScalarDiffer = 1U << 8,
    kPurityContextNotCreated = 1U << 9,
    kPurityDisposeNotAttempted = 1U << 10,
    kPurityDisposeFailed = 1U << 11,
};

inline bool scalar_snapshot_captured(const PuritySnapshot& value) noexcept {
    return value.phase_captured && value.command_playing_captured &&
        value.command_stack_empty_captured &&
        value.turn_card_play_end_captured;
}

inline bool scalar_snapshot_equal(
    const PuritySnapshot& before,
    const PuritySnapshot& after) noexcept {
    return before.phase == after.phase &&
        before.command_playing == after.command_playing &&
        before.command_stack_empty == after.command_stack_empty &&
        before.turn_card_play_end == after.turn_card_play_end;
}

inline std::uint32_t purity_failures(
    const PuritySnapshot& before,
    const PuritySnapshot& after) noexcept {
    std::uint32_t failures = kPurityClean;
    if (!before.state_captured) failures |= kPurityBeforeStateMissing;
    if (!after.state_captured) failures |= kPurityAfterStateMissing;
    if (before.state_captured && after.state_captured &&
        (before.state_size != after.state_size ||
         (before.state_size != 0U &&
          (before.state_bytes == nullptr || after.state_bytes == nullptr ||
           std::memcmp(before.state_bytes, after.state_bytes,
               before.state_size) != 0)))) {
        failures |= kPurityStateBytesDiffer;
    }

    if (!before.rng_captured) failures |= kPurityBeforeRngMissing;
    if (!after.rng_captured) failures |= kPurityAfterRngMissing;
    if (before.rng_captured && after.rng_captured &&
        before.rng_state != after.rng_state) {
        failures |= kPurityRngDiffer;
    }

    if (!scalar_snapshot_captured(before)) failures |= kPurityBeforeScalarMissing;
    if (!scalar_snapshot_captured(after)) failures |= kPurityAfterScalarMissing;
    if (scalar_snapshot_captured(before) && scalar_snapshot_captured(after) &&
        !scalar_snapshot_equal(before, after)) {
        failures |= kPurityScalarDiffer;
    }

    // ``context_created`` is an operation result recorded with the after
    // snapshot; the before snapshot is taken before CreateEffectResolver and
    // therefore must not be required to claim that a context already exists.
    if (!after.context_created) {
        failures |= kPurityContextNotCreated;
    }
    if (!after.dispose_attempted) failures |= kPurityDisposeNotAttempted;
    if (after.dispose_attempted && !after.dispose_succeeded) {
        failures |= kPurityDisposeFailed;
    }
    return failures;
}

inline bool purity_equal(
    const PuritySnapshot& before,
    const PuritySnapshot& after) noexcept {
    return purity_failures(before, after) == kPurityClean;
}

#undef GKMS_RUNTIME_EXAM_MS_X64_CALL

}  // namespace gkms::runtime_exam_validator
