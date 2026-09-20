#pragma once
#include "runtime.hpp"
namespace gkms::bridge {
bool append_story_actions(Runtime&,void*,const std::string&,json&);
bool submit_story_action(Runtime&,void*,const json&,const json&);
}
