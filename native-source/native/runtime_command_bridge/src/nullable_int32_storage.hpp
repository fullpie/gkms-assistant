#pragma once
#include <nlohmann/json.hpp>
#include <cstdint>
#include <cstring>
#include <span>
#include <stdexcept>

namespace gkms::bridge {
// Offsets and sizes come from the current IL2CPP exports, not a C++ mirror
// struct. Inactive Nullable storage remains observable (false/value17 != 0).
inline nlohmann::json decode_nullable_int32_storage(std::span<const unsigned char> bytes,
    std::size_t header,std::size_t has_offset,std::size_t value_offset){
    if(bytes.empty()||bytes.size()>64||header<2*sizeof(void*)||header>256||
        has_offset<header||value_offset<header)
        throw std::runtime_error("nullable storage layout is outside the typed boundary");
    const auto has=has_offset-header,value=value_offset-header;
    if(has>=bytes.size()||value>bytes.size()||bytes.size()-value<sizeof(std::int32_t)||
        (has>=value&&has<value+sizeof(std::int32_t))||bytes[has]>1)
        throw std::runtime_error("nullable storage member bounds or Boolean representation differ");
    std::int32_t number{};std::memcpy(&number,bytes.data()+value,sizeof(number));
    return {{"hasValue",bytes[has]!=0},{"value",number}};
}
}
