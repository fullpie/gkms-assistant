#pragma once
#include <cstdint>
#include <nlohmann/json.hpp>

namespace gkms::bridge {
// Runs on the already managed thread before any version-dependent invocation.
// Version admission is limited to the compiled default-flow method profile;
// it does not establish model quality or completed gameplay.
void verify_current_pc_method_profile();

// Source token is an identity in the retained source version, never a delta.
// A zero result on the updated version means this declaring class has no row;
// callers may continue their existing base-class walk and must otherwise fail.
std::uint32_t pc_binding_token(void* declaring_class, const char* name,
    int arity, std::uint32_t source_token);
nlohmann::json verified_pc_binding_identity();
bool updated_pc_binding_active();
}
