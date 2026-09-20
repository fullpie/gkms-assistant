#include "native_trace.hpp"
#include "public_runtime_paths.hpp"
#include <filesystem>
#include <mutex>

namespace gkms::bridge {
namespace {
std::mutex trace_mutex;
std::string request_id;
std::string command;
std::uint64_t sequence{};
}
void trace_request(const std::string& id,const std::string& operation) noexcept {
    try{std::lock_guard lock(trace_mutex);request_id=id;command=operation;}catch(...){ }
}
void trace_mark(const char* stage,json detail) noexcept {
    (void)stage;(void)detail;
}
}
