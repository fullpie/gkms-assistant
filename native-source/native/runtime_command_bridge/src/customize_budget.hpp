#pragma once
#include "runtime.hpp"
#include <map>

namespace gkms::bridge {
inline bool native_customization_available(const json& card,int remaining_new_card_slots){
    // GridListItem.RemainCount is the page's NEW-card quota, copied to every
    // row. A card already marked Customizing has consumed its slot and may
    // continue up to CustomizeCardInfo.GetRemainCustomizeCount.
    const auto remaining=card.find("card_customizations_remaining");
    return remaining!=card.end()&&remaining->is_number_integer()&&remaining->get<int>()>0&&
        card.value("can_customize",false)&&!card.value("customize_locked",true)&&
        !card.value("deleted",false)&&!card.value("lack_cost",false)&&
        (card.value("customizing",false)||remaining_new_card_slots>0);
}
inline void apply_customization_budget(json& state,json& actions){
    // Preserve observed button readiness separately; this guard can only
    // remove ineligible input, never manufacture a ready native callback.
    state["customization_budget_schema"]="gkms.customize-budget.v2";
    state["native_open_options_enabled"]=state.value("open_options_enabled",false);
    state["native_execute_enabled"]=state.value("execute_enabled",false);
    const int page_remaining=state.at("remaining_customize_count").get<int>();
    state["remaining_new_card_slots"]=page_remaining;
    std::map<int,const json*> cards;
    for(auto& card:state.at("candidates")){
        const int number=card.at("deck_number").get<int>();
        if(!cards.emplace(number,&card).second)throw std::runtime_error("duplicate native customization card Number");
        card["remaining_count_scope"]="page-new-card-slots";
        card["customization_available"]=native_customization_available(card,page_remaining);
    }
    // Legacy value remains the original menu quota; the new field below is
    // the actual per-card level capacity. Do not silently redefine old data.
    state["selected_card_remaining_count"]=nullptr;
    state["selected_card_customizations_remaining"]=nullptr;
    state["selected_card_customization_available"]=false;
    if(state.at("selected_card").is_object()){
        const int number=state.at("selected_card").at("deck_number").get<int>();
        if(cards.contains(number)){
            const auto& card=*cards.at(number);
            state["selected_card_remaining_count"]=card.at("remaining_count");
            if(card.contains("card_customizations_remaining"))
                state["selected_card_customizations_remaining"]=card.at("card_customizations_remaining");
            state["selected_card_customization_available"]=card.at("customization_available");
            for(const auto* key:{"current_customize_count","max_customize_count","card_customizations_remaining",
                                "can_customize","customize_locked","customization_available"})
                if(card.contains(key))state["selected_card"][key]=card.at(key);
        }
    }
    if(state["selected_card_customization_available"]!=true){
        state["open_options_enabled"]=false;state["execute_enabled"]=false;
    }
    for(auto item=actions.begin();item!=actions.end();){
        const auto name=item->at("action_id").get<std::string>();
        const bool requires_budget=name=="customize.select_card"||name=="customize.reveal_card"||
            name=="customize.select_option"||name=="customize.open_options"||name=="customize.execute";
        if(requires_budget){
            const int number=item->at("target").at("deck_number").get<int>();
            if(!cards.contains(number)||cards.at(number)->at("customization_available")!=true){item=actions.erase(item);continue;}
        }
        ++item;
    }
}
}
