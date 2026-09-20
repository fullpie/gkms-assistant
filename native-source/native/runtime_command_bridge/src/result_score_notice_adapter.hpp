#pragma once
#include "runtime.hpp"
#include "effect_confirmation.hpp"
#include <array>

namespace gkms::bridge {
struct ResultNoticeBinding{const char* screen;const char* view;const char* action;const char* stage;bool sheet;bool exact_parent;};
inline constexpr std::array<ResultNoticeBinding,5> result_notice_bindings={{{
    "ProducerRankingProduceUpdatedScoreOverlayPresenter","ProducerRankingProduceUpdatedScoreOverlayView","result.acknowledge_new_record","new_record_notice",false,false},
    {"ProduceHighScoreRankingResultOverlayPresenter","ProduceHighScoreRankingResultOverlayView","result.acknowledge_high_score_ranking","high_score_ranking_notice",false,true},
    {"ProduceHighScoreCharacterRewardOverlayPresenter","ProduceHighScoreCharacterRewardOverlayView","result.acknowledge_high_score_reward","high_score_reward_notice",false,true},
    {"EasyModeSettingSheetPresenter","EasyModeSettingSheetView","result.keep_easy_mode_setting","easy_mode_offer",true,true},
    {"ContinueEasyModeConfirmSheetPresenter","ContinueEasyModeConfirmSheetView","result.keep_easy_mode_setting","easy_mode_continue_offer",true,true}}};
inline const ResultNoticeBinding* result_notice_binding(const std::string& type){
    for(const auto& binding:result_notice_bindings)if(type==binding.screen)return &binding;
    return nullptr;
}
inline bool result_notice_parent_matches(const ResultNoticeBinding& binding,std::uintptr_t warning_parent,std::uintptr_t result_transform){
    return !binding.exact_parent||(warning_parent!=0&&warning_parent==result_transform);
}
inline bool result_score_notice_ready(const std::string& owner,const std::string& parent,bool closing,const json& canvas){
    return result_notice_binding(owner)!=nullptr&&parent=="ProduceResultLastNiaScreenPresenter"&&
        !closing&&canvas.value("active",false)&&canvas.value("alpha",0.0)>.001&&
        canvas.value("interactable",false)&&canvas.value("blocks_raycasts",false);
}
inline json result_score_notice_action(const std::string& owner,const std::string& parent,const std::string& button,
    const std::string& callback,const std::string& source,const json& observed,
    const std::string& owner_type="ProducerRankingProduceUpdatedScoreOverlayPresenter"){
    const auto* binding=result_notice_binding(owner_type);
    if(!binding||(binding->sheet&&source!="cancel"))return nullptr;
    if(owner.empty()||parent.empty()||button.empty()||callback.empty()||!native_button_actionable(observed))return nullptr;
    if(source!="execute"&&source!="cancel"&&source!="circle-close")return nullptr;
    return {{"action_id",binding->action},{"target",{{"action_id",binding->action},
        {"owner_type",owner_type},{"owner_instance_id",owner},
        {"parent_screen_type","ProduceResultLastNiaScreenPresenter"},{"parent_instance_id",parent},
        {"button_source",source},{"button_instance_id",button},{"callback_instance_id",callback}}}};
}
bool append_result_score_notice_actions(Runtime&,void*,const std::string&,json&);
bool submit_result_score_notice_action(Runtime&,void*,const json&,const json&);
}
