#pragma once
#include "runtime.hpp"

namespace gkms::bridge {
inline bool shop_step_started(bool in_progress_step,int step_type,int progress_status){
    // Shop NonBlockOpenInAsync tests this native progress flag before its
    // independent StepShopStart request; ShopModel.InProgress is unrelated.
    return in_progress_step&&step_type==13&&progress_status==8;
}
inline bool shop_main_start_allowed(const std::string& screen,const json& lifecycle){
    return screen!="ScheduleShopScreenPresenter"||lifecycle.at("input_ready")==true;
}
inline void apply_shop_main_start_gate(const std::string& screen,json& state,json& actions,const json& lifecycle){
    if(screen!="ScheduleShopScreenPresenter")return;
    state["step_start_lifecycle"]=lifecycle;
    state["operation_input_ready"]=state.value("input_ready",false);
    if(shop_main_start_allowed(screen,lifecycle))return;
    state["input_ready"]=false;
    state["automatic_transition"]=true;
    for(auto item=actions.begin();item!=actions.end();){
        const auto id=item->at("action_id").get<std::string>();
        if(id=="shop.select"||id=="shop.open_product"||id=="shop.buy"||id=="shop.finish")item=actions.erase(item);
        else ++item;
    }
    // No global busy flag: native effects and separately owned child sheets
    // may need their existing acknowledgements while the main page waits.
}
inline bool shop_direct_card_operation(int resource_type){return resource_type==997||resource_type==998;}
inline bool shop_product_available(const json& row,int points,int legend_remaining,int deck_count){
    const int price=row.at("price").get<int>();
    return row.at("can_buy")==true&&row.at("locked")==false&&row.at("purchased")==false&&price>=0&&price<=points&&
        (!row.value("is_legend",false)||legend_remaining>0)&&(row.at("resource_type")!=998||deck_count>1)&&
        !row.value("drink_capacity_blocked",false);
}
bool append_shop_actions(Runtime&,void* presenter,const std::string& screen,json& snapshot);
bool submit_shop_action(Runtime&,void* presenter,const json& target,const json& before);
}
