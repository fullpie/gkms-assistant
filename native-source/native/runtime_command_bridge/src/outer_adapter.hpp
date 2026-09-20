#pragma once
#include "runtime.hpp"
namespace gkms::bridge {
constexpr bool schedule_input_eligible(bool deciding,bool decided,bool tutorial){return !deciding&&!decided&&!tutorial;}
json native_outer_progress_state(Runtime&,void* progress);
json read_outer_snapshot(Runtime& runtime,const std::string& generation);
void submit_outer_action(Runtime& runtime,const json& target,const json& before);
}
