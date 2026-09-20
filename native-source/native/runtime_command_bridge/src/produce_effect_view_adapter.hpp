#pragma once
#include "runtime.hpp"
namespace gkms::bridge {
bool append_produce_effect_view_actions(Runtime&,void*,const std::string&,json&);
bool submit_produce_effect_view_action(Runtime&,void*,const json&,const json&);
}
