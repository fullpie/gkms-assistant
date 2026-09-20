#pragma once
#include <cstdint>
#include <filesystem>
#include <string>
#include <nlohmann/json.hpp>

namespace gkms::bridge {
// Version recognition is distinct from method/ABI/gameplay qualification.
// The updated PC profile intentionally grants metadata inspection only.
struct PcVersionIdentity final {
    std::uint32_t image_size{};
    std::uint32_t timestamp{};
    std::string metadata_sha256;
};
struct PcVersionProfile final {
    std::string id;
    bool recognized{};
    bool read_only_inspection_allowed{};
    bool input_qualified{};
};
inline PcVersionProfile classify_pc_version(const PcVersionIdentity& value) {
    if(value.image_size==0x0BE0E000 && value.timestamp==0x6A73E78D &&
       value.metadata_sha256=="9a6bf153c0c42a2768e619cc7d96fa79fcc1d341e9cf9bcbdc8d6bca48812668")
        return {"pc-9a6bf153",true,true,true};
    if(value.image_size==0x0BE54000 && value.timestamp==0x6A996E38 &&
       value.metadata_sha256=="9349bc965fb08a434ab1c9547a3440a8ee02a05e761723792aabe5f4a9ecb635")
        return {"pc-9349bc96-readonly",true,true,false};
    return {"unknown",false,false,false};
}

// File-only helper also reusable by recorder startup. It holds an open file
// with no write/delete sharing while hashing and checks stable file identity.
nlohmann::json inspect_pc_metadata_file(const std::filesystem::path& path);

// Reads the loaded GameAssembly PE header and the actual installed metadata
// file. No engine loading, managed invocation, hooks or input occurs.
nlohmann::json observe_current_pc_version();

// Bounded metadata-export inspection on the already attached managed thread.
// Does not invoke a method, read object/field values, create objects, install
// hooks or return a callable pointer. Every returned row remains ABI-unqualified.
nlohmann::json inspect_current_pc_contracts(const nlohmann::json& request);

// Exact full identity/signature comparison for read-only records. A match
// never grants mutation or supplies a replacement Runtime.method token.
bool pc_method_contract_matches(const nlohmann::json& observed,const nlohmann::json& expected);
} // namespace gkms::bridge
