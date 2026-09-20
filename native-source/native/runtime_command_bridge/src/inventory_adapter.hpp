#pragma once
#include "runtime.hpp"

namespace gkms::bridge {
// Called on the bridge's managed player-loop thread, never on its pipe worker.
// Any partial traversal throws; callers must not promote partial inventories.
json read_inventory(Runtime& runtime);
void initialize_inventory(Runtime& runtime);
json read_loadout(Runtime& runtime);
json read_memory_resources(Runtime& runtime, void* edit);
json apply_loadout(Runtime& runtime, const json& target, const std::string& expected_revision);
}
