#pragma once
#include <array>
#include <charconv>
#include <cstdint>
#include <stdexcept>
#include <string>

namespace gkms::bridge {
// The game changes the process-wide C++ locale. Stream formatting can then
// insert grouping separators even for std::hex, breaking recorder identity.
// to_chars has a locale-independent ASCII grammar and preserves all pointer bits.
inline std::string pointer_identity(const void* object){
    std::array<char,2+sizeof(std::uintptr_t)*2> buffer{};
    buffer[0]='0';buffer[1]='x';
    const auto converted=std::to_chars(buffer.data()+2,buffer.data()+buffer.size(),
        reinterpret_cast<std::uintptr_t>(object),16);
    if(converted.ec!=std::errc{})throw std::runtime_error("native pointer identity conversion failed");
    return {buffer.data(),converted.ptr};
}
}
