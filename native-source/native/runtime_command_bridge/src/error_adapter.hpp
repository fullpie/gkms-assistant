#pragma once
#include "runtime.hpp"
namespace gkms::bridge {
json read_error_snapshot(Runtime&,const std::string& generation);
void submit_error_return(Runtime&,const json&,const json&);
}
