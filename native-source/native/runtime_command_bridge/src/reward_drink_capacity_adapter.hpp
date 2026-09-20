#pragma once
#include "runtime.hpp"

namespace gkms::bridge {
inline bool reward_drink_capacity_panel_ready(bool active,bool completed,bool receiving,bool selecting,bool canvas){
    return active&&!completed&&!receiving&&selecting&&canvas;
}
inline bool reward_capacity_child_parent_matches(bool current_top,std::uintptr_t actual_parent,
    std::uintptr_t requested_parent,std::uintptr_t normalized_root,std::uintptr_t predecessor,std::uintptr_t expected_predecessor){
    return current_top&&actual_parent!=0&&
        ((requested_parent!=0&&actual_parent==requested_parent)||(normalized_root!=0&&actual_parent==normalized_root))&&
        predecessor==expected_predecessor;
}
// Quantity in ProduceRewardData may be zero. Bind the actual current panel
// PositionNumber and every ordered drink candidate to one raw Present.
inline json reward_panel_drink_quantities(const json& presents,int position,const json& rewards){
    if(!presents.is_array()||!rewards.is_array()||rewards.empty())return nullptr;
    const json* match=nullptr;
    for(const auto& present:presents){
        if(!present.is_object()||present.value("positionNumber",0)!=position||present.value("received",false))continue;
        if(match)return nullptr;
        match=&present;
    }
    if(!match||!match->contains("rewards")||!(*match)["rewards"].is_array()||(*match)["rewards"].size()!=rewards.size())return nullptr;
    auto out=rewards;
    for(std::size_t i=0;i<rewards.size();++i){
        const auto& raw=(*match)["rewards"][i];const auto& row=rewards[i];
        if(!raw.is_object()||row.value("index",-1)!=static_cast<int>(i)||
            raw.value("resourceType",std::string())!="ProduceResourceType_ProduceDrink"||
            raw.value("resourceId",std::string())!=row.value("drink_id",std::string())||
            !raw.contains("quantity")||!raw["quantity"].is_number_integer()||raw["quantity"].get<int>()<=0)return nullptr;
        const int actual=raw["quantity"].get<int>();
        if(!row.contains("quantity")||!row["quantity"].is_number_integer()||
            (row["quantity"]!=0&&row["quantity"]!=actual))return nullptr;
        out[i]["ui_quantity"]=row["quantity"];out[i]["quantity"]=actual;
    }
    return out;
}
// Custom overlays do not inherit SheetPresenterBase.IsDisableInteraction.
// Lazy branches ensure the sheet-only getter is never resolved on a reward.
template<class OverlayReady,class SheetReady>
bool reward_drink_capacity_owner_ready(const std::string& type,bool active,bool closing,
    OverlayReady overlay_ready,SheetReady sheet_ready) {
    if(!active||closing)return false;
    if(type=="ProduceRewardSelectorDialogPresenter")return overlay_ready();
    if(type=="ProduceDrinkConfirmSheetPresenter"||type=="SimpleSheetPresenter"||
        type=="ProducePresentSkipConfirmSheetPresenter")return sheet_ready();
    return false;
}
// Removing one owned drink is authorized only for a currently selected single
// drink reward at the actual native capacity. No guessed default capacity.
inline bool reward_drink_capacity_can_open(const json& state) {
    if(!state.is_object()||!state.value("all_rewards_are_drinks",false)||
        !state.value("is_drink_max",false)||state.value("select_status",0)!=1||
        state.value("selected_reward_quantity",0)!=1||state.value("selected_reward_index",-1)<0||
        state.value("selected_reward_id",std::string()).empty())return false;
    const int limit=state.value("limit_count",0);
    return limit>0&&limit<=64&&state.contains("owned_drinks")&&state["owned_drinks"].is_array()&&
        state["owned_drinks"].size()==static_cast<std::size_t>(limit);
}
inline bool reward_drink_capacity_same_identity(const json& expected,const json& current) {
    for(const auto* key:{"parent_instance_id","reward_view_instance_id","selected_reward_index",
        "selected_reward_id","selected_reward_quantity","limit_count","inventory_fingerprint",
        "footer_instance_id","reward_pool_fingerprint"})
        if(!expected.contains(key)||!current.contains(key)||expected[key]!=current[key])return false;
    if(expected.contains("parent_screen_type")||current.contains("parent_screen_type"))
        for(const auto* key:{"parent_screen_type","panel_instance_id","reward_group_index","reward_group_position_number","footer_callback_name"})
            if(!expected.contains(key)||!current.contains(key)||expected[key]!=current[key])return false;
    return true;
}
inline bool reward_drink_capacity_binding_matches(const json& expected,const json& current) {
    return reward_drink_capacity_same_identity(expected,current)&&reward_drink_capacity_can_open(current);
}
inline bool reward_drink_capacity_slot_matches(const json& state,int slot,const std::string& id) {
    if(!state.contains("owned_drinks")||!state["owned_drinks"].is_array()||slot<0||
        slot>=static_cast<int>(state["owned_drinks"].size())||id.empty())return false;
    const auto& row=state["owned_drinks"][slot];
    return row.value("index",-1)==slot&&row.value("drink_id",std::string())==id;
}
bool append_reward_drink_capacity_actions(Runtime&,void*,const std::string&,json&);
bool submit_reward_drink_capacity_action(Runtime&,void*,const json&,const json&);
}
