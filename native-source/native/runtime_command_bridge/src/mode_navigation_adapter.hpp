#pragma once
#include "runtime.hpp"
#include "effect_confirmation.hpp"
#include <array>
#include <string_view>

namespace gkms::bridge {
struct ModeNavigationBinding {
    const char* screen;
    const char* view;
    const char* button_id;
    const char* getter;
    std::uint32_t getter_token;
    const char* phase;
    bool reward_overlay=false;
    bool close_only=false;
    const char* action_id="ui.navigation";
};
inline constexpr std::array<ModeNavigationBinding,9> mode_navigation_bindings={{{
    "ScheduleOpenLessonScreenPresenter","ScheduleOpenLessonScreenView","lesson.result_continue","get_ResultCompleteButton",0x06002CD4,"open_lesson_result"},
    {"ProduceBeforeLiveEvaluateScreenPresenter","ProduceBeforeLiveEvaluateScreenView","produce.evaluation_continue","get_ConfirmButton",0x060021AB,"initial_before_live"},
    {"ProduceResultLastScreenPresenter","ProduceResultLastScreenView","produce.result_finish","get_EndButton",0x0600264D,"initial_produce_result"},
    {"ProduceResultLastHifScreenPresenter","ProduceResultLastHifScreenView","produce.result_finish","get_EndButton",0x0600266C,"hif_final_result"},
    {"ProduceResultLastHifQualifyScreenPresenter","ProduceResultLastHifQualifyScreenView","produce.result_finish","get_EndButton",0x06002699,"hif_selection_result"},
    {"AuditionBattleResultHifScreenPresenter","AuditionBattleResultHifScreenView","audition.result_continue","get_GoNextButton",0x06006868,"hif_audition_result"},
    {"RewardGetOverlayPresenter","RewardGetOverlayView","reward.continue","get_ExecuteButton",0x06015FE6,"reward_acknowledgement",true},
    {"ProduceAuditionUnlockOverlayPresenter","ProduceAuditionUnlockOverlayView","audition.unlock_continue","get_ExecuteButton",0x06015FE6,"audition_unlock_acknowledgement",true},
    {"StartupImageOverlayPresenter","StartupImageOverlayView","startup.notice_dismiss","get_CircleCloseButton",0x06015FE7,
        "startup_advertisement",true,true,"notice.dismiss_startup_image"}}};
inline const ModeNavigationBinding* mode_navigation_binding(std::string_view screen){
    for(const auto& binding:mode_navigation_bindings)if(screen==binding.screen)return &binding;
    return nullptr;
}
inline bool reward_overlay_input_ready(std::string_view screen,bool active,bool closing,
    bool canvas_accepts_pointer,int input_layer,int content_blocking_count,int dialog_blocking_count){
    // PC UILayer/BlockingOrder: content blocking = 13, dialog blocking = 17.
    // A blocker below an overlay does not intercept that overlay's pointer.
    // ScreenLayerManager subscribes to onClose only after OpenAsync finishes;
    // its ContentBlocking must therefore also gate direct button callbacks.
    // Explicitly registered acknowledgement overlays share OverlayViewBase /
    // OverlayCommonView controls. Their occurrence is not tied to a week or mode.
    const auto* binding=mode_navigation_binding(screen);
    return binding&&binding->reward_overlay&&active&&!closing&&canvas_accepts_pointer&&
        !(content_blocking_count>0&&input_layer<13)&&!(dialog_blocking_count>0&&input_layer<17);
}
inline json mode_navigation_action(const ModeNavigationBinding& binding,const std::string& owner,
    const std::string& button,const std::string& callback,const json& observed,bool input_ready=true){
    if(!input_ready||!native_button_actionable(observed))return nullptr;
    return {{"action_id",binding.action_id},{"target",{{"action_id",binding.action_id},
        {"button_id",binding.button_id},{"mode_navigation",true},{"owner_type",binding.screen},
        {"owner_instance_id",owner},{"button_instance_id",button},{"callback_instance_id",callback}}}};
}
inline bool mode_navigation_target_matches(const json& target,const ModeNavigationBinding& binding,
    const std::string& owner,const std::string& button,const std::string& callback){
    return (!binding.close_only||target.value("button_source",std::string())=="close")&&
        target.value("mode_navigation",false)&&target.value("action_id",std::string())==binding.action_id&&
        target.value("owner_type",std::string())==binding.screen&&target.value("button_id",std::string())==binding.button_id&&
        target.value("owner_instance_id",std::string())==owner&&target.value("button_instance_id",std::string())==button&&
        target.value("callback_instance_id",std::string())==callback;
}
json native_mode_progress_fields(Runtime&,void* progress);
bool append_mode_navigation_actions(Runtime&,void* presenter,const std::string& screen,json& snapshot);
bool submit_mode_navigation_action(Runtime&,void* presenter,const json& target,const json& before);
}
