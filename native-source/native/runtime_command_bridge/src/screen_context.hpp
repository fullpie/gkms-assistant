#pragma once
#include "runtime.hpp"
namespace gkms::bridge {
void initialize_screen_context(Runtime& runtime);
void* active_screen(Runtime& runtime);
void* active_layer(Runtime& runtime);
json screen_context_snapshot(Runtime& runtime);
}
