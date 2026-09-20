#pragma once
#include "runtime.hpp"
#include "effect_confirmation.hpp"
#include <optional>

namespace gkms::bridge {
inline bool outer_pointer_blocked(int count,bool input_known,int input_sorting_value,
    std::optional<int> input_order,int blocking_sorting_value,int blocking_order){
    if(count<=0)return false;
    if(!input_known)return true;
    if(blocking_sorting_value!=input_sorting_value)return blocking_sorting_value>input_sorting_value;
    return !input_order.has_value()||blocking_order>=*input_order;
}
inline json foreground_effect_pointer_target(const json& snapshot){
    // This only selects a recipient for native revalidation; it never grants
    // input. Card-difference projection can replace ui_state while preserving
    // this native effect target, so mutable diagnostics are not its authority.
    try{
        if(!snapshot.at("legal_actions").is_array()||snapshot.at("legal_actions").size()!=1)return nullptr;
        const auto& action=snapshot.at("legal_actions").front();const auto& target=action.at("target");
        const auto screen=snapshot.at("screen_type").get<std::string>();
        if(action.at("action_id")!="effect.advance"||target.at("action_id")!="effect.advance"||
           target.at("button_source")!="produce-screen-touch"||!has_effect_confirmation_lifecycle(screen)||
           target.at("presenter_type")!=screen||target.at("screen_instance_id")!=snapshot.at("screen_instance_id"))return nullptr;
        for(const auto* key:{"button_instance_id","wait_callback_instance_id"})
            if(!target.at(key).is_string()||target.at(key).get<std::string>().empty()||target.at(key)=="0x0")return nullptr;
        for(const auto* key:{"produce_id","week","step_type"})if(target.at(key)!=snapshot.at("state").at(key))return nullptr;
        return target;
    }catch(const json::exception&){return nullptr;}
}
inline json effective_pointer_canvas(const json& ancestors){
    // Input is ordered by actual Transform ancestry, not by component array
    // ordering. rootCanvas alone would skip a nearer overrideSorting boundary.
    if(!ancestors.is_array()||ancestors.empty())return nullptr;
    int previous=-1;
    try{
        for(const auto& canvas:ancestors){
            if(!canvas.at("hierarchy_distance").is_number_integer())return nullptr;
            const int distance=canvas.at("hierarchy_distance").get<int>();
            if(distance<=previous||!canvas.at("active_and_enabled").is_boolean())return nullptr;
            previous=distance;
            // GetComponentsInParent includes disabled Canvas components.
            // Like Graphic.CacheCanvas, do not use their sorting boundary.
            if(canvas.at("active_and_enabled")!=true)continue;
            if(!canvas.at("override_sorting").is_boolean()||!canvas.at("is_root_canvas").is_boolean())return nullptr;
            if(canvas.at("override_sorting")!=true&&canvas.at("is_root_canvas")!=true)continue;
            for(const auto* key:{"instance_id","root_canvas_instance_id"})
                if(!canvas.at(key).is_string()||canvas.at(key).get<std::string>().empty()||canvas.at(key)=="0x0")return nullptr;
            if(canvas.at("is_root_canvas")==true&&canvas.at("root_canvas_instance_id")!=canvas.at("instance_id"))return nullptr;
            for(const auto* key:{"sorting_layer_id","sorting_layer_value","sorting_order","render_mode"})
                if(!canvas.at(key).is_number_integer())return nullptr;
            return canvas;
        }
    }catch(const json::exception&){return nullptr;}
    return nullptr;
}
inline void apply_outer_pointer_guard(json& snapshot,const json& guard){
    snapshot["pointer_blocking"]=guard;
    if(guard.at("input_ready")==true)return;
    snapshot["legal_actions"]=json::array();
    snapshot["busy"]=true;
    snapshot["ui_state"]["automatic_transition"]=true;
}
json read_outer_pointer_guard(Runtime&,void* presenter,bool is_screen_layer,const json& input_target=nullptr);
}
