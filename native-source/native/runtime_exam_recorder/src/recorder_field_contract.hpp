#pragma once
#include <cstddef>
#include <cstdint>

namespace gkms::runtime_exam_recorder {
// Only the four normal-recorder backing fields use this boundary: String
// references, Int32, or an enum whose actual underlying type is Int32.
struct RecorderFieldShape {
    int kind{};
    int enum_underlying_kind{};
    bool byref{};
    bool is_static{};
    bool is_value_type{};
    bool is_enum{};
    std::size_t value_size{};
    std::size_t offset{};
    std::size_t parent_instance_size{};
};

constexpr bool recorder_field_copy_allowed(const RecorderFieldShape& shape,
    std::size_t destination_size, bool reference_destination) noexcept {
    if (shape.byref || shape.is_static || !shape.parent_instance_size ||
        shape.offset > shape.parent_instance_size ||
        shape.value_size > shape.parent_instance_size - shape.offset ||
        shape.value_size != destination_size) return false;
    if (reference_destination)
        return shape.kind == 14 && !shape.is_value_type && !shape.is_enum &&
            shape.value_size == sizeof(void*);
    return shape.is_value_type && shape.value_size == sizeof(std::int32_t) &&
        ((shape.kind == 8 && !shape.is_enum) ||
         (shape.kind == 17 && shape.is_enum && shape.enum_underlying_kind == 8));
}
} // namespace gkms::runtime_exam_recorder
