#pragma once
#include "runtime.hpp"

namespace gkms::bridge {
json observe_save_reference_presence(Runtime&,void* save,void* sequence,void* parameter,const json& serialized);
json observe_parameter_reference_sources(Runtime&,void* sequence,void* parameter);
json bind_parameter_reference_sources(json save_presence,const json& before,const json& after);
json observe_command_reference_presence(Runtime&,void* command,const json& serialized);
int observe_total_effect_draw_count(Runtime&,void* parameter);
}
