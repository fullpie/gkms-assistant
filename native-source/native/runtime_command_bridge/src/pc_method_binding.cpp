#include "pc_method_binding.hpp"
#include "pc_version_profile.hpp"
#include "pc_method_contract_table.hpp"
#include "pc_public_method_profile.hpp"
#include <Windows.h>
#include <array>
#include <stdexcept>
#include <string>
#include <vector>

namespace gkms::bridge {
namespace {
using json = nlohmann::json;
json observed_version;
json verified_profile;
bool verified = false;
bool updated = false;
DWORD owner_thread = 0;

template<class T> T api(const char* name) {
    auto module = GetModuleHandleW(L"GameAssembly.dll");
    auto function = module ? GetProcAddress(module, name) : nullptr;
    if (!function) throw std::runtime_error(std::string("PC method binding export unavailable: ") + name);
    return reinterpret_cast<T>(function);
}
void require(bool value, const char* reason) {
    if (!value) throw std::runtime_error(reason);
}
bool same_identity(const json& actual, const json& wanted) {
    for (const auto* key : {"metadata_sha256", "game_assembly_image_size", "game_assembly_timestamp"})
        if (!actual.contains(key) || !wanted.contains(key) || actual.at(key) != wanted.at(key)) return false;
    return true;
}
json class_identity(void* klass) {
    require(klass != nullptr, "PC binding declaring class missing");
    const auto parent = api<void*(*)(void*)>("il2cpp_class_get_declaring_type");
    const auto name = api<const char*(*)(void*)>("il2cpp_class_get_name");
    auto outer = klass;
    std::vector<std::string> names;
    for (auto current = klass; current; current = parent(current)) {
        require(names.size() < 32, "PC binding declaring chain exceeds bound");
        auto value = name(current);
        require(value != nullptr, "PC binding declaring name missing");
        names.emplace_back(value);
        outer = current;
    }
    auto image = api<void*(*)(void*)>("il2cpp_class_get_image")(klass);
    auto image_name = api<const char*(*)(void*)>("il2cpp_image_get_name")(image);
    auto ns = api<const char*(*)(void*)>("il2cpp_class_get_namespace")(outer);
    require(image_name && ns, "PC binding image or namespace missing");
    json path = json::array();
    for (auto it = names.rbegin(); it != names.rend(); ++it) path.push_back(*it);
    return {{"image",image_name},{"namespace",ns},{"type_path",path}};
}
void require_owner() {
    require(verified && GetCurrentThreadId() == owner_thread &&
        api<void*(*)()>("il2cpp_thread_current")() != nullptr,
        "PC method profile is not verified on this managed owner thread");
}
}

void verify_current_pc_method_profile() {
    require(api<void*(*)()>("il2cpp_thread_current")() != nullptr,
        "PC method profile needs an existing managed thread");
    const auto observed = observe_current_pc_version();
    if (verified) {
        require_owner();
        require(observed == observed_version, "PC version changed after method profile verification");
        return;
    }
    require(observed.value("recognized",false), "Unknown PC cannot enter method binding");
    const auto profile = json::parse(kPc9349MethodProfileJson);
    require(profile.at("schema") == "gkms.pc-version-method-contract-profile.v1",
        "Compiled PC method profile schema differs");
    const auto identity = observed.at("engine_identity");
    const bool is_updated = same_identity(identity, profile.at("target_identity"));
    require(is_updated || same_identity(identity, profile.at("source_identity")),
        "Observed PC differs from both explicit method profile identities");
    if (is_updated) {
#if defined(GKMS_DIRECT_REPLAY_RESEARCH) || defined(GKMS_OFFICIAL_REPLAY_CORE) || defined(GKMS_RUNTIME_LEGAL_CANDIDATE_PROBE) || defined(GKMS_RUNTIME_LEGAL_VERIFIED_PROBE)
        throw std::runtime_error("Updated PC profile qualifies default cultivation only; research adapters remain unqualified");
#endif
        const auto& entries = profile.at("entries");
        require(entries.is_array() && !entries.empty() && entries.size() <= 512,
            "Compiled PC method profile size invalid");
        json queries = json::array();
        for (const auto& entry : entries) {
            const auto& expected = entry.at("expected");
            json query;
            for (const auto* key : {"image","namespace","type_path","name","arity","token"})
                query[key] = expected.at(key);
            queries.push_back(std::move(query));
        }
        json classes = json::array();
        const auto& layouts = profile.at("class_layouts");
        require(layouts.is_array() && layouts.size() <= 512, "Compiled class profile size invalid");
        for (const auto& row : layouts) {
            json query;
            for (const auto* key : {"image","namespace","type_path"}) query[key] = row.at("observed").at(key);
            classes.push_back(std::move(query));
        }
        const auto result = inspect_current_pc_contracts({
            {"schema","gkms.pc-readonly-contract-request.v1"},
            {"methods",queries},{"classes",classes}});
        require(result.at("complete") == true && result.at("version_stable") == true &&
            result.at("errors").empty() && result.at("methods").size() == entries.size() &&
            result.at("classes").size() == layouts.size(),
            "Current PC method profile inspection incomplete");
        for (std::size_t index = 0; index < entries.size(); ++index) {
            const auto& actual = result.at("methods").at(index);
            const auto& expected = entries.at(index).at("expected");
            require(actual.at("query_index") == index, "Current PC method profile row order differs");
            require(pc_method_contract_matches(actual,expected), "Current PC exact method signature differs");
            for (const auto* key : {"flags","implementation_flags"})
                require(actual.at(key) == expected.at(key), "Current PC method flags differ");
        }
        for (std::size_t index = 0; index < layouts.size(); ++index) {
            const auto& actual = result.at("classes").at(index);
            const auto& expected = layouts.at(index).at("observed");
            require(actual.at("query_index") == index, "Current PC class profile row order differs");
            for (const auto* key : {"image","namespace","type_path","fields","instance_size","is_value_type","is_enum"})
                require(actual.at(key) == expected.at(key), "Current PC field layout differs");
            if (expected.at("is_value_type") == true) {
                require(actual.at("value_size") == expected.at("value_size") &&
                    actual.at("value_alignment") == expected.at("value_alignment"),
                    "Current PC value-type layout differs");
            }
        }
    }
    observed_version = observed;
    verified_profile = profile;
    updated = is_updated;
    owner_thread = GetCurrentThreadId();
    verified = true;
}

std::uint32_t pc_binding_token(void* klass, const char* name, int arity, std::uint32_t source_token) {
    require_owner();
    if (!updated || !source_token) return source_token;
    const auto owner = class_identity(klass);
    return select_profiled_pc_token(verified_profile.at("entries"),owner,name,arity,source_token);
}

nlohmann::json verified_pc_binding_identity() {
    require_owner();
    auto identity = observed_version.at("engine_identity");
    identity["identity_source"] = "ExamAdapter.initialize current-PC image/header and full metadata verification";
    identity["method_profile"] = updated ? "pc-9349bc96-default" : "pc-9a6bf153-original";
    return identity;
}

bool updated_pc_binding_active() {
    require_owner();
    return updated;
}
}
