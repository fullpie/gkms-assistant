#pragma once
#include "runtime.hpp"
namespace gkms::bridge {
void trace_request(const std::string& request_id,const std::string& command) noexcept;
void trace_mark(const char* stage,json detail=json::object()) noexcept;
}
