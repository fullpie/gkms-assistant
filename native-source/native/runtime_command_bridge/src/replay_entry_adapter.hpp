#pragma once
#include "runtime.hpp"
#include "callback_method_contract.hpp"

namespace gkms::bridge {
inline json replay_entry_callback_source(const std::string& screen){
    const char* name=screen=="ProduceIdolSelectScreenPresenter"?"<SetEvent>b__11_8":
        screen=="ProduceIdolGrowthInfoNiaSheetPresenter"?"<OpenGrowthDetailSheetAsync>b__21_0":nullptr;
    if(!name)return nullptr;
    return callback_method_source("Assembly-CSharp.dll","Campus.OutGame",{"ProduceIdolSelectScreenPresenter"},name,0,
        screen=="ProduceIdolSelectScreenPresenter"?0x0600ECA7:0x0600ECB8);
}
inline bool replay_entry_callback_matches(const std::string& screen,int token,std::uint32_t verified_token,bool owner_is_current_picker){
    return owner_is_current_picker&&verified_token&&token>0&&static_cast<std::uint32_t>(token)==verified_token&&
        (screen=="ProduceIdolSelectScreenPresenter"||screen=="ProduceIdolGrowthInfoNiaSheetPresenter");
}
inline bool replay_entry_scope(const json& state){
    return state.value("native_no_active_produce",false)&&state.value("selection_identity_matches",false)&&
        state.value("produce_id",std::string())=="produce-004"&&
        state.value("idol_card_id",std::string())=="i_card-hume-3-006"&&
        !state.value("is_high_score_rush",true)&&!state.value("is_research",true)&&
        !state.value("is_hif_final_round",true);
}
inline json replay_entry_action(const std::string& screen,json target,const json& state){
    const bool picker=screen=="ProduceIdolSelectScreenPresenter";
    const bool sheet=screen=="ProduceIdolGrowthInfoNiaSheetPresenter";
    if((!picker&&!sheet)||!replay_entry_scope(state)||!state.value("presenter_active",false)||
       !state.value("callback_ready",false)||!state.value("button_ready",false)||
       !state.value("pointer_ready",false))return nullptr;
    if(picker&&(state.value("picker_transitioning",true)||state.value("picker_card_blocked",true)))return nullptr;
    if(sheet&&!state.value("sheet_input_ready",false))return nullptr;
    const char* id=picker?"replay.open_info":"replay.open_recommendations";
    target["action_id"]=id;
    return {{"action_id",id},{"manual_only",true},{"target",std::move(target)}};
}
inline void mark_replay_entry_unavailable(json& snapshot,const std::string& error){
    // Optional replay navigation must not veto normal picker operations.
    snapshot["ui_state"]["replay_entry"]={{"ready",false},{"error",error}};
}
bool append_replay_entry_actions(Runtime&,void* presenter,const std::string& screen,json& snapshot);
bool submit_replay_entry_action(Runtime&,void* presenter,const json& target,const json& before);
}
