#pragma once
#include "runtime.hpp"

namespace gkms::bridge {
inline bool selection_memory_matches_prepare(const json& memory,const std::string& paired_produce,const std::string& idol){
    return memory.is_object()&&!memory.value("user_selection_memory_id",std::string()).empty()&&
        memory.value("produce_id",std::string())==paired_produce&&memory.value("idol_card_id",std::string())==idol;
}
void append_selection_memory_prepare(Runtime&,void* presenter,void* model,void* info,void* produce,
    const std::string& screen,json& selection,json& actions);
bool submit_selection_memory_action(Runtime&,void* presenter,const json& target,const json& before);
}
