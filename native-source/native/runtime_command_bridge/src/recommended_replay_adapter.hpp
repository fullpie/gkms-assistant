#pragma once
#include "runtime.hpp"
#include "callback_method_contract.hpp"
#include <string_view>

namespace gkms::bridge {
inline json recommended_replay_callback_source(bool button){
    return button?callback_method_source("Assembly-CSharp.dll","Campus.OutGame",
        {"ProducerRankingRecommendReplayAuditionScoreRow"},"OnReplayClicked",0,0x0600F889):
        callback_method_source("Assembly-CSharp.dll","Campus.OutGame",
        {"ProducerRankingRecommendReplayListCellView","<>c__DisplayClass27_0"},"<SetAuditionScores>b__0",0,0x0600F8A7);
}
inline bool recommended_replay_callback_matches(int replay_token,std::uint32_t verified_replay_token,bool view_matches,int membership_count,
    int button_token,std::uint32_t verified_button_token,bool button_owner_matches){
    return verified_replay_token&&replay_token>0&&static_cast<std::uint32_t>(replay_token)==verified_replay_token&&view_matches&&membership_count==1&&
        verified_button_token&&button_token>0&&static_cast<std::uint32_t>(button_token)==verified_button_token&&button_owner_matches;
}
inline bool recommended_replay_scope(std::string_view screen,const json& page,bool produce_active){
    return screen=="ProducerRankingRecommendReplayScreenPresenter"&&!produce_active&&
        page.value("produce_id",std::string())=="produce-004"&&
        page.value("idol_card_id",std::string())=="i_card-hume-3-006"&&
        !page.value("is_high_score_rush",true)&&!page.value("is_research",true);
}
inline bool recommended_replay_has_actions(const json& audition){
    const auto situation=audition.find("examContestSituation");
    if(situation==audition.end()||!situation->is_object())return false;
    const auto stages=situation->find("stages");
    if(stages==situation->end()||!stages->is_array())return false;
    for(const auto& stage:*stages){
        if(!stage.is_object()||!stage.contains("selfSections")||!stage.at("selfSections").is_array())continue;
        for(const auto& section:stage.at("selfSections")){
            if(!section.is_object()||!section.contains("player")||!section.at("player").is_object())continue;
            const auto& player=section.at("player");
            if(player.contains("examActions")&&player.at("examActions").is_array()&&!player.at("examActions").empty())return true;
        }
    }
    return false;
}
inline json recommended_replay_action(const json& target,bool ready){
    const int step=target.value("step_type",0);
    if(!ready||step<16||step>18||target.value("history_digest",std::string()).empty()||
        target.value("source_history_digest",std::string()).empty()||target.value("user_memory_id",std::string()).empty())return nullptr;
    auto bound=target;bound["action_id"]="replay.start";
    return {{"action_id","replay.start"},{"target",bound}};
}
bool append_recommended_replay_actions(Runtime&,void*,const std::string&,json&);
bool submit_recommended_replay_action(Runtime&,void*,const json&,const json&);
}
