#pragma once
#include "runtime.hpp"
#include "effect_confirmation.hpp"

namespace gkms::bridge {
inline const char* customize_entry_notice_reason(bool has_description,int points,int threshold){
    return has_description&&points>=0&&threshold>=0&&points<=threshold?"low_produce_points_alert":"unclassified";
}
inline bool customize_entry_parent_matches(const std::string& screen,const std::string& parent,
    std::uintptr_t actual_parent,std::uintptr_t expected_parent,bool in_progress,int status,const json& offered){
    if(screen!="ProduceCustomizeConfirmSheetPresenter"||parent!="ScheduleScreenPresenter"||
        actual_parent==0||actual_parent!=expected_parent||!in_progress||status!=4||!offered.is_array())return false;
    for(const auto& step:offered)if(step.is_number_integer()&&step.get<int>()==28)return true;
    return false;
}
inline bool customize_entry_button_ready(bool active,bool closing,bool disabled,const json& canvas,const json& button){
    return active&&!closing&&!disabled&&canvas.value("active",false)&&canvas.value("alpha",0.0)>.001&&
        canvas.value("interactable",false)&&canvas.value("blocks_raycasts",false)&&native_button_actionable(button);
}
bool append_customize_confirmation_actions(Runtime&,void*,const std::string&,json&);
bool submit_customize_confirmation_action(Runtime&,void*,const json&,const json&);
}
