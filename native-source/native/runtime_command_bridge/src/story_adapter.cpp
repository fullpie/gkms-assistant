#include "story_adapter.hpp"
#include "pointer_identity.hpp"
#include "effect_confirmation.hpp"
#include "story_prompt.hpp"
#include <algorithm>

namespace gkms::bridge {
namespace {
bool flag(Runtime& r,void* object,const char* getter){return r.unbox<bool>(r.getter(object,getter));}
bool active(Runtime& r,void* object){return object&&flag(r,r.getter(object,"get_gameObject"),"get_activeInHierarchy");}
void* current_adv(Runtime& r,void* owner,json* live_owner=nullptr,json* live_players=nullptr){
    // Ownership is the current native screen/layer's actual typed field, never
    // a previous story or a singleton discovered elsewhere in the scene.
    if(r.class_name(r.object_class(owner))=="LiveScenePresenter"){
        void* selected{};json binding;
        // Live owns separate SimpleHorizontal players around the movie. Do
        // not discover an unrelated ADV or treat before_live as a Start button.
        for(const auto* field:{"_beforeLiveStoryPlayer","_afterLiveStoryPlayer"}){
            auto player=r.read_object_field(owner,field);
            if(player&&r.class_name(r.object_class(player))!="LiveStoryPlayerPresenter")
                throw std::runtime_error("Live story player type changed");
            auto adv=player?r.read_object_field(player,"_advPresenter"):nullptr;
            if(adv&&r.class_name(r.object_class(adv))!="SimpleHorizontalAdvPresenter")
                throw std::runtime_error("Live story ADV type changed");
            const bool is_active=adv&&active(r,adv),is_playing=adv&&r.field<bool>(adv,"_isPlay"),
                is_end=player&&r.field<bool>(player,"_isEnd");
            if(live_players)live_players->push_back({{"field",field},{"player_instance_id",pointer_identity(player)},
                {"adv_instance_id",pointer_identity(adv)},{"active",is_active},{"story_run_active",is_playing},{"player_is_end",is_end}});
            if(!adv||!is_active||!is_playing||is_end)continue;
            if(selected)throw std::runtime_error("Live has ambiguous owned active story players");
            selected=adv;binding={{"live_story_field",field},{"live_story_player_instance_id",pointer_identity(player)}};
        }
        if(selected&&live_owner)*live_owner=std::move(binding);
        return selected;
    }
    if(!r.has_field(r.object_class(owner),"_advPresenter"))return nullptr;
    auto adv=r.read_object_field(owner,"_advPresenter");
    if(!adv||r.class_name(r.object_class(adv))!="VerticalAdvPresenter"||!active(r,adv))return nullptr;
    return adv;
}
json button_state(Runtime& r,void* button){
    auto callback=button?r.read_object_field(button,"onClickedCallback"):nullptr;
    return {{"active",active(r,button)},{"enabled",button&&flag(r,button,"get_IsEnabled")},
        {"disabled",!button||flag(r,button,"get_IsDisabled")},{"has_callback",callback!=nullptr},
        {"button_instance_id",pointer_identity(button)},{"wait_callback_instance_id",pointer_identity(callback)}};
}
void* button(Runtime& r,void* adv,const std::string& source){
    if(source=="message-touch"||source=="player-skip-only"){
        if(source=="player-skip-only"&&r.class_name(r.object_class(adv))!="SimpleHorizontalAdvPresenter")
            throw std::runtime_error("skip-only control requires the owned SimpleHorizontal ADV");
        auto ui=r.getter(r.getter(adv,"get_Engine"),"get_UI");
        auto player=r.read_object_field(ui,"_playerControlView");
        return player?r.read_object_field(player,source=="message-touch"?"_touchScreen":"_skipButton"):nullptr;
    }
    if(source=="menu-skip"||source=="overlay-menu-skip"){
        auto view=r.read_object_field(adv,"_view");
        auto menu=r.getter(view,source=="menu-skip"?"get_MenuView":"get_OverlayMenuView");
        if(!menu)return nullptr;
        auto skip=r.getter(menu,"get_SkipButton");
        return skip?r.getter(skip,"get_Button"):nullptr;
    }
    throw std::runtime_error("unknown native story control");
}
bool skip_visible(Runtime& r,void* adv,const char* source){
    auto view=r.read_object_field(adv,"_view");
    auto menu=r.getter(view,std::string(source)=="menu-skip"?"get_MenuView":"get_OverlayMenuView");
    if(!active(r,view)||!active(r,menu))return false;
    auto skip=r.getter(menu,"get_SkipButton");
    if(!active(r,skip))return false;
    auto fade=r.read_object_field(skip,"_rootFade");
    return fade&&r.unbox<float>(r.getter(fade,"get_alpha"))>.001f;
}
json action(const char* id,json target){target["action_id"]=id;return {{"action_id",id},{"target",target}};}
json simple_prompt(Runtime& r,void* sheet){
    auto view=r.read_object_field(sheet,"_view");
    auto common=r.getter(view,"get_CommonView");
    const auto title=r.string(r.getter(r.getter(common,"get_Title"),"get_text"));
    const auto description=r.string(r.getter(r.read_object_field(view,"_description"),"get_text"));
    return {{"title",title},{"description",description},{"digest",sha256(json::array({title,description}).dump())}};
}
}

bool append_story_actions(Runtime& r,void* owner,const std::string& owner_type,json& snapshot){
    if(owner_type=="SimpleSheetPresenter"){
        const auto prompt=simple_prompt(r,owner);
        if(!is_unread_story_skip_prompt(prompt.at("title").get<std::string>(),prompt.at("description").get<std::string>()))return false;
        const auto control=r.getter(owner,"get_ExecuteButton");
        const auto observed=button_state(r,control);
        snapshot["ui_state"]["story_skip_confirmation"]=prompt;
        snapshot["legal_actions"]=json::array();
        snapshot["actions_complete"]=snapshot.at("blockers").empty();
        if(native_button_actionable(observed))snapshot["legal_actions"].push_back(action("story.confirm_skip",{
            {"sheet_instance_id",pointer_identity(owner)},{"prompt_digest",prompt.at("digest")},
            {"button_instance_id",observed.at("button_instance_id")},{"wait_callback_instance_id",observed.at("wait_callback_instance_id")}}));
        return true;
    }
    json live_owner=json::object(),live_players=json::array();auto adv=current_adv(r,owner,&live_owner,&live_players);
    if(owner_type=="LiveScenePresenter")snapshot["ui_state"]["live_story_players"]=live_players;
    if(!adv)return false;
    auto engine=r.getter(adv,"get_Engine");
    if(!engine){snapshot["ui_state"]["story"]={{"data_ready",false}};return true;}
    if(!live_owner.empty()){
        // SimpleHorizontal uses EnableSkipOnlyMode: its ordinary touch panel
        // is inactive and only this same native skip button is reparented.
        auto skip=button(r,adv,"player-skip-only");const auto observed=button_state(r,skip);
        const bool engine_active=active(r,engine),run_active=r.field<bool>(adv,"_isPlay");
        const bool skip_only=flag(r,r.getter(engine,"get_UI"),"get_IsSkipOnlyMode");
        const auto permission=r.read_object_field(adv,"onCanSkipAsync");
        const bool ready=engine_active&&run_active&&skip_only&&!permission&&native_button_actionable(observed);
        json state={{"data_ready",true},{"owner_type",owner_type},{"owner_instance_id",pointer_identity(owner)},
            {"adv_instance_id",pointer_identity(adv)},{"engine_active",engine_active},{"story_run_active",run_active},
            {"skip_permission_callback_instance_id",pointer_identity(permission)},
            {"skip_only_mode",skip_only},{"skip_only_button",observed},{"skip_only_ready",ready}};
        state.update(live_owner);snapshot["ui_state"]["story"]=state;
        if(ready){
            auto target=live_owner;target.update({{"owner_type",owner_type},{"owner_instance_id",pointer_identity(owner)},
                {"adv_instance_id",pointer_identity(adv)},{"button_source","player-skip-only"},
                {"button_instance_id",observed.at("button_instance_id")},
                {"wait_callback_instance_id",observed.at("wait_callback_instance_id")}});
            snapshot["legal_actions"].push_back(action("story.skip",target));
            snapshot["actions_complete"]=snapshot.at("blockers").empty();
        }
        return true;
    }
    auto timeline=r.getter(engine,"get_Timeline");
    auto ui=r.getter(engine,"get_UI");
    auto clip=r.read_object_field(timeline,"_waitTargetClip");
    const bool waiting=flag(r,timeline,"get_IsWaiting");
    const bool engine_active=active(r,engine);
    const bool story_run_active=r.field<bool>(adv,"_isPlay");
    auto touch=button(r,adv,"message-touch");
    auto touch_state=button_state(r,touch);
    auto message=r.getter(ui,"get_MessageContainer");
    json state={{"data_ready",true},{"owner_type",owner_type},{"owner_instance_id",pointer_identity(owner)},
        {"adv_instance_id",pointer_identity(adv)},{"adv_playing",flag(r,adv,"get_IsPlaying")},
        {"engine_active",engine_active},{"story_run_active",story_run_active},
        {"timeline_waiting",waiting},{"wait_clip_instance_id",pointer_identity(clip)},
        {"wait_clip_type",clip?json(r.class_name(r.object_class(clip))):json(nullptr)},
        {"message_button",touch_state},{"message_touch_ready",engine_active&&adv_message_touch_ready(waiting,touch_state)},
        {"message_animating",flag(r,r.getter(message,"get_MessageText"),"get_IsAnimationPlaying")},
        {"ui_interactive",r.field<bool>(ui,"_isInteractiveMode")},{"ui_fast_forward",flag(r,ui,"get_IsFastForward")},
        {"ui_auto",flag(r,ui,"get_IsAuto")},{"ui_auto_wait",flag(r,ui,"get_IsAutoWait")}};
    const bool choosing=std::any_of(snapshot["legal_actions"].begin(),snapshot["legal_actions"].end(),
        [](const auto& value){return value.at("action_id")=="event.choose";});
    const json identity={{"owner_type",owner_type},{"owner_instance_id",state.at("owner_instance_id")},
        {"adv_instance_id",state.at("adv_instance_id")}};
    bool actionable=false;
    if(!choosing&&state.at("message_touch_ready")==true){
        auto target=identity;target["button_source"]="message-touch";
        target["button_instance_id"]=touch_state.at("button_instance_id");
        target["wait_callback_instance_id"]=touch_state.at("wait_callback_instance_id");
        target["wait_clip_instance_id"]=state.at("wait_clip_instance_id");
        snapshot["legal_actions"].push_back(action("story.advance",target));actionable=true;
    }
    state["skip_buttons"]=json::array();
    for(const auto* source:{"menu-skip","overlay-menu-skip"}){
        auto skip=button(r,adv,source);auto observed=button_state(r,skip);
        observed["visible"]=skip_visible(r,adv,source);observed["source"]=source;
        observed["ready"]=story_skip_ready(engine_active,story_run_active,observed.at("visible").get<bool>(),observed);
        state["skip_buttons"].push_back(observed);
        if(!choosing&&observed.at("ready")==true){
            auto target=identity;target["button_source"]=source;
            target["button_instance_id"]=observed.at("button_instance_id");
            target["wait_callback_instance_id"]=observed.at("wait_callback_instance_id");
            snapshot["legal_actions"].push_back(action("story.skip",target));actionable=true;
        }
    }
    snapshot["ui_state"]["story"]=state;
    if(actionable){
        if(snapshot["surface"]=="unsupported")snapshot["surface"]="story";
        snapshot["actions_complete"]=snapshot.at("blockers").empty();
    }
    return true;
}
bool submit_story_action(Runtime& r,void* owner,const json& target,const json& before){
    const auto id=target.at("action_id").get<std::string>();
    if(id=="story.confirm_skip"){
        if(before.at("screen_type")!="SimpleSheetPresenter"||pointer_identity(owner)!=target.at("sheet_instance_id").get<std::string>())
            throw std::runtime_error("unread story confirmation sheet changed");
        const auto prompt=simple_prompt(r,owner);
        if(!is_unread_story_skip_prompt(prompt.at("title").get<std::string>(),prompt.at("description").get<std::string>())||
            prompt.at("digest")!=target.at("prompt_digest"))throw std::runtime_error("unread story confirmation prompt changed");
        auto control=r.getter(owner,"get_ExecuteButton");const auto observed=button_state(r,control);
        if(!native_button_actionable(observed)||observed.at("button_instance_id")!=target.at("button_instance_id")||
            observed.at("wait_callback_instance_id")!=target.at("wait_callback_instance_id"))throw std::runtime_error("story confirmation button changed");
        r.invoke(r.method(r.object_class(control),"OnClickedHandler",0),control);return true;
    }
    if(id!="story.advance"&&id!="story.skip")return false;
    if(before.at("screen_type")!=target.at("owner_type")||pointer_identity(owner)!=target.at("owner_instance_id").get<std::string>())
        throw std::runtime_error("story owner changed before input");
    json live_owner=json::object();auto adv=current_adv(r,owner,&live_owner);
    if(!adv||pointer_identity(adv)!=target.at("adv_instance_id").get<std::string>())throw std::runtime_error("current story changed before input");
    if(!live_owner.empty()){
        if(id!="story.skip"||target.value("button_source",std::string())!="player-skip-only"||
            r.read_object_field(adv,"onCanSkipAsync")!=nullptr||
            !flag(r,r.getter(r.getter(adv,"get_Engine"),"get_UI"),"get_IsSkipOnlyMode"))
            throw std::runtime_error("Live story skip-only control or permission callback changed");
        for(auto it=live_owner.begin();it!=live_owner.end();++it)
            if(!target.contains(it.key())||target.at(it.key())!=it.value())throw std::runtime_error("Live story player owner changed");
    }
    auto engine=r.getter(adv,"get_Engine");
    if(!active(r,engine))throw std::runtime_error("story engine is no longer active");
    if(id=="story.skip"&&!r.field<bool>(adv,"_isPlay"))throw std::runtime_error("story play lifecycle already ended");
    if(id=="story.advance"){
        auto timeline=r.getter(r.getter(adv,"get_Engine"),"get_Timeline");
        if(!flag(r,timeline,"get_IsWaiting")||pointer_identity(r.read_object_field(timeline,"_waitTargetClip"))!=target.at("wait_clip_instance_id").get<std::string>())
            throw std::runtime_error("story wait changed before advancement");
    }
    auto control=button(r,adv,target.at("button_source").get<std::string>());
    const auto observed=button_state(r,control);
    if(!native_button_actionable(observed)||observed.at("button_instance_id")!=target.at("button_instance_id")||
        observed.at("wait_callback_instance_id")!=target.at("wait_callback_instance_id"))throw std::runtime_error("story input callback changed");
    r.invoke(r.method(r.object_class(control),"OnClickedHandler",0),control);
    return true;
}
}
