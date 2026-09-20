#pragma once
#include "effect_confirmation.hpp"

namespace gkms::bridge {
inline nlohmann::json native_reward_group_target(const nlohmann::json& state){
    // ProduceRewardPanelPresenter.<SetEvents>b__18_3 checks these three
    // fields before clearing WaitingOpen and starting its own OpenBoxAsync.
    if(state.at("owner_type")!="ScheduleFanPresentScreenPresenter"||
        state.at("is_completed")!=false||state.at("is_receiving")!=false||
        state.at("is_selecting")!=false||state.at("is_waiting_open")!=true||
        !native_button_actionable(state.at("button")))return nullptr;
    return {{"action_id","reward.open_group"},{"owner_type",state.at("owner_type")},
        {"owner_instance_id",state.at("owner_instance_id")},
        {"panel_instance_id",state.at("panel_instance_id")},
        {"group_index",state.at("group_index")},
        {"button_instance_id",state.at("button_instance_id")}};
}
}
