#pragma once
#include "runtime.hpp"

namespace gkms::bridge {
inline bool business_start_profile_ready(const json& current,const json& expected){
    if(!current.is_object()||!expected.is_object())return false;
    for(const auto* key:{"in_progress","in_progress_step"})
        if(!current.contains(key)||!current.at(key).is_boolean()||current.at(key)!=true)return false;
    if(!expected.contains("in_progress")||!expected.at("in_progress").is_boolean()||expected.at("in_progress")!=true)return false;
    for(const auto* key:{"produce_id","idol_card_id"})
        if(!current.contains(key)||!current.at(key).is_string()||current.at(key).get<std::string>().empty()||
           !expected.contains(key)||current.at(key)!=expected.at(key))return false;
    for(const auto* key:{"week","step_type","progress_status"})
        if(!current.contains(key)||!current.at(key).is_number_integer()||!expected.contains(key)||
           !expected.at(key).is_number_integer()||current.at(key)!=expected.at(key))return false;
    // Current PC metadata: Business=25, EventBusiness=26, StepAction=8.
    // This gate is used only by the exact ScheduleBusinessScreenPresenter.
    return current.at("week").get<int>()>0&&
        (current.at("step_type")==25||current.at("step_type")==26)&&current.at("progress_status")==8;
}
inline json business_snapshot_profile(const json& snapshot){
    if(!snapshot.is_object()||!snapshot.contains("screen_type")||snapshot.at("screen_type")!="ScheduleBusinessScreenPresenter"||
       !snapshot.contains("state")||!snapshot.at("state").is_object()||
       !snapshot.contains("progress")||!snapshot.at("progress").is_object())return nullptr;
    const auto& state=snapshot.at("state");const auto& progress=snapshot.at("progress");
    for(const auto* key:{"produce_id","week","step_type","progress_status","in_progress"})if(!state.contains(key))return nullptr;
    if(!progress.contains("idolCardId"))return nullptr;
    return {{"produce_id",state.at("produce_id")},{"idol_card_id",progress.at("idolCardId")},
        {"week",state.at("week")},{"step_type",state.at("step_type")},
        {"progress_status",state.at("progress_status")},{"in_progress",state.at("in_progress")}};
}
inline bool business_start_target_matches(const json& target,const json& current,const json& expected){
    return target.is_object()&&target.contains("start_context")&&target.at("start_context")==current&&
        business_start_profile_ready(current,expected);
}
}
