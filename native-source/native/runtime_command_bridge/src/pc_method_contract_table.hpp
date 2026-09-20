#pragma once
#include <cstdint>
#include <stdexcept>
#include <nlohmann/json.hpp>

namespace gkms::bridge {
inline std::uint32_t select_profiled_pc_token(const nlohmann::json& entries,
    const nlohmann::json& owner, const char* name, int arity, std::uint32_t source_token) {
    const nlohmann::json* selected = nullptr;
    for (const auto& entry : entries) {
        const auto& source = entry.at("source");
        if (source.at("token") != source_token || source.at("image") != owner.at("image") ||
            source.at("namespace") != owner.at("namespace") || source.at("type_path") != owner.at("type_path")) continue;
        if (selected) throw std::runtime_error("Ambiguous source PC method binding");
        if (source.at("name") != name || source.at("arity") != arity)
            throw std::runtime_error("PC method caller differs from its declared source identity");
        selected = &entry;
    }
    return selected ? selected->at("expected").at("token").get<std::uint32_t>() : 0;
}
}
