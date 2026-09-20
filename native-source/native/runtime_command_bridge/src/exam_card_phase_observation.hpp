#pragma once
#include "runtime.hpp"
namespace gkms::bridge {
json observe_state_card_phase_counters(Runtime&,void* sequence);
json observe_command_card_phase_counters(Runtime&,void* command);
}
