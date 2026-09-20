#pragma once
#include "runtime.hpp"
#include "exam_selector_close_owner.hpp"

namespace gkms::bridge {
json read_live_model_context(Runtime& runtime);
// Read-only DTOs for host-owned model inference. They grant no input or model
// compatibility authority; the existing revision/pending transaction does.
json capture_main_model_observation(Runtime& runtime,void* sequence,void* parameter,
    const json& before,const json& legal_inputs,const json& reference_presence,int draw_count_before);
json capture_secondary_model_observation(Runtime& runtime,void* sequence,void* parameter,
    void* selector,const ExamCloseSelectorOwner& owner,const json& parent_context,const json& ui_state);
}
