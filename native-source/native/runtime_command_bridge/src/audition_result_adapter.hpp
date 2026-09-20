#pragma once
#include "runtime.hpp"

namespace gkms::bridge {
inline const char* audition_result_action_id(bool complete,bool win,int remaining,int free,bool has_item){
    if(complete)return nullptr;
    if(win)return "audition.continue_after_win";
    if(remaining>0&&(free>0||has_item))return "audition.open_retry_result";
    return "audition.finish_failed";
}
bool append_audition_result_actions(Runtime&,void* presenter,const std::string& screen,json& snapshot);
bool submit_audition_result_action(Runtime&,void* presenter,const json& target,const json& before);
}
