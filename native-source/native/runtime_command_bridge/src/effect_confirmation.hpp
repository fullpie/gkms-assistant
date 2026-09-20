#pragma once
#include <nlohmann/json.hpp>
#include <string>

namespace gkms::bridge {
inline bool native_button_actionable(const nlohmann::json& observed){
    return observed.at("active")==true&&observed.at("enabled")==true&&
        observed.at("disabled")==false&&observed.at("has_callback")==true;
}
inline bool adv_message_touch_ready(bool timeline_waiting,const nlohmann::json& observed){
    // UIManager.OnClickedScreen uses the timeline wait and the actual input
    // control; AdvPresenterBase.IsPlaying is the playback clock, not readiness.
    return timeline_waiting&&native_button_actionable(observed);
}
inline bool story_skip_ready(bool engine_active,bool story_run_active,bool control_visible,
    const nlohmann::json& observed){
    return engine_active&&story_run_active&&control_visible&&native_button_actionable(observed);
}
inline bool has_effect_confirmation_lifecycle(const std::string& screen){
    return screen=="RemainActivateEffectScreenPresenter"||screen=="ScheduleRefreshScreenPresenter"||
        screen=="ScheduleSelfLessonScreenPresenter"||screen=="ScheduleOpenLessonScreenPresenter"||screen=="ScheduleEventScreenPresenter"||
        screen=="AuditionBattleResultScreenPresenter"||screen=="AuditionBattleResultNiaRewardOverlayPresenter"||
        screen=="ProduceResultScreenPresenter"||screen=="ScheduleCustomizeScreenPresenter"||screen=="ProduceEvaluateScreenPresenter"||
        screen=="ScheduleIntervalScreenPresenter"||screen=="ScheduleFanPresentScreenPresenter"||
        screen=="ScheduleShopScreenPresenter";
}
template<class CardUI,class GlobalEffect,class CardDifference>
inline void project_card_then_effect_views(CardUI card_ui,GlobalEffect global_effect,CardDifference card_difference){
    card_ui();
    global_effect();
    card_difference();
}
inline nlohmann::json effect_confirmation_action(const std::string& screen,
    const nlohmann::json& target,const nlohmann::json& observed){
    if(!has_effect_confirmation_lifecycle(screen)||!native_button_actionable(observed))
        return nullptr;
    auto bound=target;bound["action_id"]="effect.advance";
    return {{"action_id","effect.advance"},{"target",bound}};
}
}
