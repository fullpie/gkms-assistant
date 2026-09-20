#pragma once
#include "runtime.hpp"
#include <filesystem>
#include <optional>

namespace gkms::bridge {
void atomic_json(const std::filesystem::path& path,const json& value);
void validate_request(const json& request,const std::string& generation);
class Mailbox final {
public:
    void initialize(std::filesystem::path root);
    std::optional<json> claim_next();
    void finish(const json& request,const json& response);
    void publish_status(const json& status);
    json result_for(const std::string& request_id) const;
private:
    std::filesystem::path root_;
    void recover();
};
}
