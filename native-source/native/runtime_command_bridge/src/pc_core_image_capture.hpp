#pragma once
#include <filesystem>
#include <string>
#include <nlohmann/json.hpp>

namespace gkms::bridge {
// Read-only extraction of the already loaded, fixed PC GameAssembly module.
// No managed calls, pointer traversal into heaps, page changes or game input.
// A capture is a live observation, NOT a coherent or reusable initialized VM.
nlohmann::json capture_pc_core_image(const std::filesystem::path& bridge_root,
                                    const std::string& request_id);
}
