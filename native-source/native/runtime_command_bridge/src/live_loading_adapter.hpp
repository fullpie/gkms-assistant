#pragma once
#include "runtime.hpp"
#include "effect_confirmation.hpp"

namespace gkms::bridge {
// Observation never creates a LoadingManager, registers a waiter or consumes
// a UniTask. The receipt is projected separately from per-screen UI state.
json read_live_loading(Runtime&,void* live_presenter,void* fixed);
json read_live_loading_receipt(Runtime&);
bool submit_live_loading(Runtime&,void* live_presenter,void* fixed,const json& target,const json& before);

inline bool live_loading_canvas_ready(const json& canvas){
    try{
        return canvas.at("active")==true&&
            canvas.at("interactable")==true&&canvas.at("blocks_raycasts")==true;
    }catch(const json::exception&){return false;}
}
inline bool live_loading_press_ready(const json& state){
    try{
        if(state.at("owner_bound")!=true||state.at("loading_active")!=true||state.at("hiding")!=false||
           state.at("now_loading_active")!=false||state.at("tap_to_start_active")!=true||
           state.at("no_active_layer")!=true||state.at("pointer_blocking").at("input_ready")!=true||
           !state.at("read_errors").empty()||!native_button_actionable(state.at("button"))||
           !live_loading_canvas_ready(state.at("canvas").at("root"))||
           !live_loading_canvas_ready(state.at("canvas").at("button_root")))return false;
        const auto& waiters=state.at("waiters");
        if(!waiters.is_array()||waiters.size()!=1||!waiters[0].at("status").is_number_integer()||waiters[0].at("status")!=0)return false;
        for(const auto* name:{"closure_id","source_id"})
            if(!waiters[0].at(name).is_string()||waiters[0].at(name)=="0x0"||waiters[0].at(name).get<std::string>().empty())return false;
        if(state.contains("last_press")&&!state.at("last_press").is_null()){
            const auto& previous=state.at("last_press");
            if(!previous.at("error").is_null()||previous.at("status")!=1||previous.at("handler_returned")!=true)return false;
            if(previous.at("target").at("source_id")==waiters[0].at("source_id"))return false;
        }
        return true;
    }catch(const json::exception&){return false;}
}
// Alpha is animation-only diagnostic data. It does not grant or remove
// native input permission and is excluded from CAS; all interaction flags stay.
inline json live_loading_revision_view(json state){
    if(!state.is_object()||!state.contains("canvas")||!state.at("canvas").is_object())return state;
    for(const auto* name:{"root","button_root"}){
        auto& groups=state["canvas"];
        if(groups.contains(name)&&groups.at(name).is_object())groups[name].erase("alpha");
    }
    return state;
}
}
