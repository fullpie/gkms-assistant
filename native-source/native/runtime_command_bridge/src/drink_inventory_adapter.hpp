#pragma once
#include "runtime.hpp"

namespace gkms::bridge {
inline bool drink_inventory_toggle_allowed(bool selected,int selected_count,int limit){
    return limit>0&&selected_count>=0&&(selected||selected_count<limit);
}
inline bool drink_inventory_confirm_allowed(int selected_count,int limit){return limit>0&&selected_count==limit;}
inline bool drink_inventory_warning_owner_matches(const std::string& type,std::uintptr_t warning_root,std::uintptr_t owner_root){
    return type=="ProduceDrinkMaxSheetPresenter"&&warning_root!=0&&warning_root==owner_root;
}
bool append_drink_inventory_actions(Runtime&,void* presenter,const std::string& screen,json& snapshot);
bool submit_drink_inventory_action(Runtime&,void* presenter,const json& target,const json& before);
}
