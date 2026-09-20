#pragma once
#include "runtime.hpp"
#include "exam_save_serializer.hpp"

namespace gkms::bridge {
class ExamAdapter final {
public:
    void initialize(Runtime& runtime);
    json snapshot(const std::string& session);
    // Validation does not enqueue. Submit must receive the same snapshot and
    // the gateway must have durably claimed the request before this call.
    void validate(const std::string& command, const json& target, const json& before);
    void submit(const std::string& command, const json& target, const json& before,json& action_receipt);
private:
    Runtime* runtime_{};
    void* presenter_method_{};
    void* sequence_{};
    ExamSaveSerializer save_serializer_;
    void* hand_factory_{};
    void* drink_factory_{};
    void* end_factory_{};
    void* enqueue_{};
    void* validate_hand_{};
    void* can_use_drink_{};
    void* create_context_{};
    json capture(const std::string& session);
    json hand_validation(std::int32_t index);
    bool drink_validation(void* presenter,void* drink);
    json legal_inputs(void* presenter,const json& boundary);
    void* target_object(const std::string& command, const json& target);
};
} // namespace gkms::bridge
