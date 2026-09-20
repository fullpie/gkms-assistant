#pragma once
#include "runtime.hpp"
namespace gkms::bridge {
inline bool schedule_customize_step_ready(bool in_progress_step,int step_type,int progress_status){
    // Native NonBlockOpenInAsync calls StepCustomizeStart when this server
    // progress flag is false. Merely entering StepAction does not start it.
    return in_progress_step&&step_type==28&&progress_status==8;
}
inline bool schedule_customize_finish_canvas_ready(bool active,float alpha,bool interactable,bool raycasts){
    return active&&alpha>.001f&&interactable&&raycasts;
}
inline void apply_schedule_customize_start_gate(json& state,json& actions,const json& lifecycle){
    if(state.value("selector_type",std::string())!="ScheduleCustomizeScreenPresenter")return;
    state["step_start_lifecycle"]=lifecycle;
    if(lifecycle.at("input_ready")==true)return;
    state["automatic_transition"]=true;
    state["native_finish_enabled"]=state.value("native_finish_enabled",state.value("finish_enabled",false));
    state["finish_enabled"]=false;
    for(auto item=actions.begin();item!=actions.end();){
        if(item->at("action_id").get<std::string>().rfind("customize.",0)==0)item=actions.erase(item);
        else ++item;
    }
    // Do not mark the whole snapshot busy: real Start-effect acknowledgement
    // controls must remain available to the game's current effect waiter.
}
// parent is supplied only from the current screen context, never inferred
// from a selector type or copied from a previous Shop/Interval operation.
inline void bind_card_selector_parent(json& snapshot,const std::string& type,const std::string& parent){
    if(snapshot.value("surface",std::string())!="card_choice")return;
    const json identity=parent.empty()?json(nullptr):json(parent);
    snapshot["underlying_screen_instance_id"]=identity;
    snapshot["ui_state"]["parent_instance_id"]=identity;
    snapshot["ui_state"]["parent_screen_type"]=type.empty()?json(nullptr):json(type);
    for(auto& candidate:snapshot["legal_actions"]){
        candidate["target"]["parent_instance_id"]=identity;
        candidate["target"]["parent_screen_type"]=type.empty()?json(nullptr):json(type);
    }
}
// Called only for the active TopLayer/TopScreen inside ManagedOperation.
bool append_produce_card_ui_actions(Runtime&, void* presenter, const std::string& screen, json& snapshot);
bool submit_produce_card_ui_action(Runtime&, void* presenter, const json& target, const json& before);
}
