#include "produce_lifecycle_adapter.hpp"
#include "audition_selection.hpp"
#include "pointer_identity.hpp"
#include "effect_confirmation.hpp"
#include "audition_identity.hpp"
#include "mode_navigation_adapter.hpp"
#include "native_layer_context.hpp"
#include <set>

namespace gkms::bridge {
namespace {
int integer(Runtime& r,void* object,const char* getter){return r.unbox<int>(r.getter(object,getter));}
bool boolean(Runtime& r,void* object,const char* getter){return r.unbox<bool>(r.getter(object,getter));}
std::string text(Runtime& r,void* object,const char* getter){return r.string(r.getter(object,getter));}
void* progress(Runtime& r){
    auto manager=r.klass("Assembly-CSharp","Campus.Common.User","UserDataManager");
    return r.invoke(r.method(manager,"get_UserProduceProgress",0),nullptr);
}
bool active(Runtime& r,void* component){return component&&boolean(r,r.getter(component,"get_gameObject"),"get_activeInHierarchy");}
bool clickable(Runtime& r,void* button){return active(r,button)&&boolean(r,button,"get_IsEnabled")&&
    !boolean(r,button,"get_IsDisabled")&&r.read_object_field(button,"onClickedCallback");}
std::string instance(void* object){return pointer_identity(object);}
json action(const char* id,json target){target["action_id"]=id;return {{"action_id",id},{"target",target}};}
void* common_execute(Runtime& r,void* presenter){return r.getter(r.getter(r.read_object_field(presenter,"_view"),"get_CommonView"),"get_ExecuteButton");}
void* scene_touch_button(Runtime& r){
    auto type=r.klass("Assembly-CSharp","Campus.InGame.Produce","ProduceScenePresenter");
    auto scene=r.invoke(r.method(type,"get_Instance",0),nullptr);
    if(!scene||r.class_name(r.object_class(scene))!="ProduceScenePresenter"||!active(r,scene))return nullptr;
    return r.getter(r.read_object_field(scene,"_view"),"get_OverlayButton");
}
void* audition_drag(Runtime& r,void* presenter){
    return r.read_object_field(r.read_object_field(presenter,"_view"),"_auditionSelectDragView");
}
json audition_state(Runtime& r,void* presenter){
    auto model=r.read_object_field(presenter,"_model");
    auto view=r.read_object_field(presenter,"_view");
    auto drag=audition_drag(r,presenter);
    auto current=progress(r);
    const int votes=integer(r,current,"get_VoteCount");
    const int stage=integer(r,model,"get_CurrentAuditionStepType");
    const int selected=integer(r,model,"get_SelectIndex");
    const bool callbacks=r.read_object_field(drag,"onSelectStepCallback")&&r.read_object_field(drag,"onDecideStepCallback");
    const bool input_active=active(r,view)&&active(r,drag)&&callbacks;
    // SetView fills this list from the game's actual current dearness data.
    // It is in model candidate order, not the drag geometry's reversed order.
    std::set<int> disabled;
    if(boolean(r,drag,"get_IsLimitSelect")){
        for(auto value:r.enumerate(r.getter(drag,"get_LimitSelectDisableIndexList"),64))
            disabled.insert(r.unbox<int>(value));
    }
    auto offered=r.enumerate(r.getter(model,"get_CandidateAuditionList"),64);
    json state={{"selected_index",selected},{"step_type",stage},{"vote_count",votes},
        {"input_active",input_active},{"candidates",json::array()}};
    for(std::size_t index=0;index<offered.size();++index){
        auto row=offered[index];
        json identity={{"difficulty_id",text(r,row,"get_Id")},{"produce_id",text(r,row,"get_ProduceId")},
            {"number",integer(r,row,"get_Number")},{"step_type",integer(r,row,"get_StepType")},
            {"audition_type",integer(r,row,"get_AuditionType")}};
        if(identity.at("step_type")!=stage||identity.at("produce_id")!=text(r,current,"get_ProduceId"))
            throw std::runtime_error("audition candidate belongs to another current mode or stage");
        const int required_votes=integer(r,row,"get_VoteCount");
        const bool locked=disabled.contains(static_cast<int>(index));
        state["candidates"].push_back({{"index",index},{"identity",identity},
            {"selected",static_cast<int>(index)==selected},{"dearness_locked",locked},
            {"required_vote_count",required_votes},{"enabled",input_active&&!locked&&votes>=required_votes},
            {"difficulty",json::parse(text(r,row,"ToString"))}});
    }
    return state;
}
bool retire_pointer_ready(Runtime& r,void* button){
    if(!clickable(r,button))return false;
    auto type=r.reflection_type(r.klass("UnityEngine.UIModule","UnityEngine","CanvasGroup"));
    auto groups=r.invoke(r.method(r.object_class(button),"GetComponentsInParent",1,0x060013CF),button,{type});
    float alpha=1.f;
    // GetComponentsInParent(Type) returns an Array, not a positional Dictionary.
    for(auto group:r.enumerate(groups,64)){
        alpha*=r.unbox<float>(r.getter(group,"get_alpha"));
        if(!boolean(r,group,"get_interactable")||!boolean(r,group,"get_blocksRaycasts")||alpha<=.001f)return false;
        if(boolean(r,group,"get_ignoreParentGroups"))break;
    }
    return true;
}
struct RetireProjection {void* button{};json observed=json::object();json candidate=nullptr;};
RetireProjection retire_projection(Runtime& r,void* presenter,const std::string& screen,const json& before){
    RetireProjection result;
    if(screen!="HomeProduceProgressSheetPresenter"&&screen!="ProduceRetireSheetPresenter")return result;
    if(!presenter||r.class_name(r.object_class(presenter))!=screen)throw std::runtime_error("retire presenter type changed");
    auto current=progress(r);
    if(!current||!boolean(r,current,"get_IsInProgress"))return result;
    auto view=r.read_object_field(presenter,"_view");
    const auto expected_view=screen=="HomeProduceProgressSheetPresenter"?"HomeProduceProgressSheetView":"ProduceRetireSheetView";
    if(!view||r.class_name(r.object_class(view))!=expected_view)throw std::runtime_error("retire view type changed");
    void* origin=presenter;
    if(screen=="ProduceRetireSheetPresenter"){
        origin=native_layer_parent(r,presenter);
        if(!origin||r.class_name(r.object_class(origin))!="HomeProduceProgressSheetPresenter")
            origin=registered_layer_predecessor(r,presenter);
    }
    const bool origin_ready=origin&&r.class_name(r.object_class(origin))=="HomeProduceProgressSheetPresenter"&&
        before.value("underlying_screen_type",std::string())=="HomeTopScreenPresenter";
    result.button=screen=="HomeProduceProgressSheetPresenter"?
        r.invoke(r.method(r.object_class(view),"get_RetireButton",0,0x0600AAC6),view):common_execute(r,presenter);
    auto callback=result.button?r.read_object_field(result.button,"onClickedCallback"):nullptr;
    auto entries=callback?r.enumerate(r.getter(callback,"GetInvocationList"),16):std::vector<void*>{};
    int token=0;std::uint32_t verified_token{};bool owner_matches=false;
    if(entries.size()==1){
        auto method=r.getter(entries.front(),"get_Method");
        token=method?integer(r,method,"get_MetadataToken"):0;
        verified_token=read_profiled_callback_token(r,method,produce_retire_callback_source(screen));
        owner_matches=r.getter(entries.front(),"get_Target")==presenter;
    }
    const bool closing=boolean(r,presenter,"get_IsClosing");
    const bool disabled=boolean(r,presenter,"get_IsDisableInteraction")||!result.button||boolean(r,result.button,"get_IsDisabled");
    result.observed={{"in_progress",true},{"active",active(r,presenter)&&active(r,view)&&active(r,result.button)},
        {"enabled",result.button&&boolean(r,result.button,"get_IsEnabled")},{"disabled",disabled},{"closing",closing},
        {"origin_ready",origin_ready},{"origin_presenter_instance_id",instance(origin)},
        {"callback_method_token",token},{"callback_contract_ready",produce_retire_callback_matches(screen,token,verified_token,owner_matches)},
        {"pointer_ready",result.button&&retire_pointer_ready(r,result.button)},{"manual_only",true}};
    if(screen=="ProduceRetireSheetPresenter"&&!boolean(r,presenter,"get_IsShowExecuteButton"))result.observed["enabled"]=false;
    const auto progress_json=json::parse(text(r,current,"ToString"));
    json target={{"presenter_type",screen},{"presenter_instance_id",instance(presenter)},{"view_instance_id",instance(view)},
        {"origin_presenter_instance_id",instance(origin)},{"button_instance_id",instance(result.button)},
        {"callback_instance_id",instance(callback)},{"callback_method_token",token},{"progress_digest",sha256(progress_json.dump())},
        {"produce_id",text(r,current,"get_ProduceId")},{"idol_card_id",text(r,current,"get_IdolCardId")},
        {"week",integer(r,current,"get_StepNumber")},{"step_type",integer(r,current,"get_StepType")}};
    result.candidate=produce_retire_manual_action(screen,std::move(target),result.observed);
    result.observed["ready"]=!result.candidate.is_null();
    return result;
}
}

json read_produce_context(Runtime& r){
    auto current=progress(r);
    if(!current)return nullptr;
    const int step=integer(r,current,"get_StepType");
    json result={{"in_progress",boolean(r,current,"get_IsInProgress")},
        {"produce_id",text(r,current,"get_ProduceId")},{"idol_card_id",text(r,current,"get_IdolCardId")},
        {"week",integer(r,current,"get_StepNumber")},{"step_type",step},
        {"step_select_number",integer(r,current,"get_StepSelectNumber")},
        {"entry_number",integer(r,current,"get_StepSelectNumber")},
        {"audition_number",nullptr},{"current_schedule",nullptr},
        {"source","current managed UserProduceProgress and CurrentSchedule getters"}};
    result.update(native_mode_progress_fields(r,current));
    if(result["in_progress"]==true){
        auto schedule=r.getter(current,"get_CurrentSchedule");
        if(schedule){
            json value={{"step_number",integer(r,schedule,"get_StepNumber")},
                {"selected_step_type",integer(r,schedule,"get_SelectedStepType")},
                {"step_select_number",integer(r,schedule,"get_StepSelectNumber")}};
            result["current_schedule"]=value;
            result["audition_number"]=current_audition_number(result.at("week").get<int>(),step,value);
        }
    }
    return result;
}

void append_effect_confirmation(Runtime& r,void* presenter,const std::string& screen,json& snapshot){
    if(!has_effect_confirmation_lifecycle(screen))return;
    auto button=scene_touch_button(r);
    auto callback=button?r.read_object_field(button,"onClickedCallback"):nullptr;
    json observed={{"active",active(r,button)},{"enabled",button&&boolean(r,button,"get_IsEnabled")},
        {"disabled",!button||boolean(r,button,"get_IsDisabled")},{"has_callback",callback!=nullptr},
        {"button_instance_id",instance(button)},{"wait_callback_instance_id",instance(callback)}};
    const auto& state=snapshot.at("state");
    json target={{"presenter_type",screen},{"screen_instance_id",snapshot.at("screen_instance_id")},
        {"button_source","produce-screen-touch"},{"button_instance_id",observed.at("button_instance_id")},
        {"wait_callback_instance_id",observed.at("wait_callback_instance_id")},
        {"produce_id",state.at("produce_id")},{"week",state.at("week")},{"step_type",state.at("step_type")}};
    auto candidate=effect_confirmation_action(screen,target,observed);
    observed["ready"]=!candidate.is_null();
    snapshot["ui_state"]["effect_confirmation"]=observed;
    if(!candidate.is_null()){
        // The game's active full-screen overlay intercepts underlying buttons.
        // Its await callback identity distinguishes successive waits even when
        // the persistent OverlayButton and the Produce step do not change.
        snapshot["legal_actions"]=json::array({candidate});
        snapshot["actions_complete"]=snapshot.at("blockers").empty();
        snapshot["ui_state"]["automatic_transition"]=false;
        snapshot["ui_state"]["phase"]="await_effect_acknowledgement";
    }
    (void)presenter;
}

bool append_produce_lifecycle_actions(Runtime& r,void* presenter,const std::string& screen,json& snapshot){
    if(screen=="DearnessLevelUpReceiveOverlayPresenter"){
        auto view=r.read_object_field(presenter,"_view");
        if(!view||r.class_name(r.object_class(view))!="DearnessLevelReceiveOverlayView")
            throw std::runtime_error("dearness level-up view contract changed");
        auto button=r.getter(view,"get_FrontTapButton");
        auto callback=button?r.read_object_field(button,"onClickedCallback"):nullptr;
        const bool ready=active(r,presenter)&&active(r,view)&&!boolean(r,presenter,"get_IsClosing")&&clickable(r,button);
        snapshot["surface"]="notice";
        snapshot["ui_state"]["dearness_notice"]={{"ready",ready},{"button_instance_id",instance(button)},
            {"wait_callback_instance_id",instance(callback)},{"source","normal DearnessLevelReceiveOverlayView.FrontTapButton"}};
        snapshot["actions_complete"]=snapshot.at("blockers").empty();
        if(ready)snapshot["legal_actions"].push_back(action("notice.dearness_advance",{
            {"presenter_type",screen},{"presenter_instance_id",instance(presenter)},
            {"view_instance_id",instance(view)},{"button_instance_id",instance(button)},
            {"wait_callback_instance_id",instance(callback)}}));
        return true;
    }
    if(screen=="ProduceRetireSheetPresenter"){
        auto retire=retire_projection(r,presenter,screen,snapshot);
        snapshot["surface"]="produce_retire";
        snapshot["ui_state"]["retire"]=std::move(retire.observed);
        // Never expose this irreversible control as generic ui.sheet_confirm.
        snapshot["legal_actions"]=json::array();
        if(!retire.candidate.is_null())snapshot["legal_actions"].push_back(std::move(retire.candidate));
        snapshot["actions_complete"]=snapshot.at("blockers").empty();
        return true;
    }
    if(screen=="RemainActivateEffectScreenPresenter"||screen=="ScheduleRefreshScreenPresenter"||
        screen=="AuditionBattleResultNiaRewardOverlayPresenter"||screen=="ProduceBeforeLiveEvaluateHifScreenPresenter"){
        // NonBlockOpenInAsync owns ActivateEffectAsync -> ResolveEffectAsync
        // -> SwitchCurrentScheduleStepScreen. This View has no input button.
        // The selector adapter, called after this one, can still replace this
        // surface if the normal effect resolver presents a reward choice.
        snapshot["surface"]="effect_resolution";
        snapshot["actions_complete"]=snapshot.at("blockers").empty();
        const auto phase=screen=="RemainActivateEffectScreenPresenter"?"resolve_remaining_effects":
            screen=="ScheduleRefreshScreenPresenter"?"complete_refresh":
            screen=="ProduceBeforeLiveEvaluateHifScreenPresenter"?"before_live_hif_story":"audition_reward_presentation";
        snapshot["ui_state"]={{"automatic_transition",true},{"phase",phase}};
        return true;
    }
    if(screen=="HomeProduceProgressSheetPresenter"||screen=="ProduceStorySkipSettingSheetPresenter"||
        screen=="StoryVoiceSettingSheetPresenter"||screen=="StoryFastForwardSettingSheetPresenter"){
        auto button=common_execute(r,presenter);
        snapshot["surface"]="layer";snapshot["actions_complete"]=snapshot.at("blockers").empty();
        if(screen=="HomeProduceProgressSheetPresenter"){
            auto current=progress(r);
            snapshot["ui_state"]["produce_id"]=text(r,current,"get_ProduceId");
            snapshot["ui_state"]["idol_card_id"]=text(r,current,"get_IdolCardId");
            snapshot["ui_state"]["in_progress"]=boolean(r,current,"get_IsInProgress");
            if(snapshot["ui_state"]["in_progress"]==true&&clickable(r,button))
                snapshot["legal_actions"].push_back(action("produce.resume",{{"produce_id",snapshot["ui_state"]["produce_id"]},
                    {"idol_card_id",snapshot["ui_state"]["idol_card_id"]}}));
            auto retire=retire_projection(r,presenter,screen,snapshot);
            snapshot["ui_state"]["retire"]=std::move(retire.observed);
            if(!retire.candidate.is_null())snapshot["legal_actions"].push_back(std::move(retire.candidate));
        }else if(clickable(r,button))snapshot["legal_actions"].push_back(action("produce.confirm_settings",{{"settings_screen",screen}}));
        return true;
    }
    if(screen!="ProduceAuditionSelectScreenPresenter")return false;
    auto state=audition_state(r,presenter);
    snapshot["surface"]="audition_select";
    snapshot["ui_state"]=state;
    snapshot["legal_actions"]=audition_selection_actions(state.at("candidates"),state.at("selected_index").get<int>());
    snapshot["actions_complete"]=snapshot.at("blockers").empty();
    return true;
}

bool submit_produce_lifecycle_action(Runtime& r,void* presenter,const json& target,const json& before){
    const auto action=target.at("action_id").get<std::string>();
    if(action=="notice.dearness_advance"){
        if(before.at("screen_type")!="DearnessLevelUpReceiveOverlayPresenter"||
            r.class_name(r.object_class(presenter))!="DearnessLevelUpReceiveOverlayPresenter")
            throw std::runtime_error("dearness notice is no longer current");
        json fresh=before;fresh["legal_actions"]=json::array();
        append_produce_lifecycle_actions(r,presenter,"DearnessLevelUpReceiveOverlayPresenter",fresh);
        bool found=false;for(const auto& row:fresh.at("legal_actions"))if(row.at("target")==target)found=true;
        if(!found)throw std::runtime_error("dearness notice button or waiter changed");
        auto button=r.getter(r.read_object_field(presenter,"_view"),"get_FrontTapButton");
        r.invoke(r.method(r.object_class(button),"OnClickedHandler",0),button);
        return true;
    }
    if(action=="produce.retire.open"||action=="produce.retire.confirm"){
        const auto screen=before.at("screen_type").get<std::string>();
        auto retire=retire_projection(r,presenter,screen,before);
        if(retire.candidate.is_null()||retire.candidate.at("target")!=target)
            throw std::runtime_error("retire sheet, progress or real button callback changed");
        // The normal button starts ExecuteRetireAsync or confirms its awaited
        // ProduceRetireSheet. No API, IsExecute setter or direct async call.
        r.invoke(r.method(r.object_class(retire.button),"OnClickedHandler",0),retire.button);
        return true;
    }
    if(action=="effect.advance"){
        const auto screen=before.at("screen_type").get<std::string>();
        if(!has_effect_confirmation_lifecycle(screen))throw std::runtime_error("screen has no verified effect acknowledgement lifecycle");
        auto button=scene_touch_button(r);
        auto callback=button?r.read_object_field(button,"onClickedCallback"):nullptr;
        if(!clickable(r,button)||instance(button)!=target.at("button_instance_id").get<std::string>()||
            instance(callback)!=target.at("wait_callback_instance_id").get<std::string>())
            throw std::runtime_error("game effect wait changed before acknowledgement");
        r.invoke(r.method(r.object_class(button),"OnClickedHandler",0),button);
        return true;
    }
    if(action=="produce.resume"||action=="produce.confirm_settings"){
        auto button=common_execute(r,presenter);
        if(!clickable(r,button))throw std::runtime_error("produce sheet execute is no longer actionable");
        r.invoke(r.method(r.object_class(button),"OnClickedHandler",0),button);
        return true;
    }
    if(action!="audition.choose"&&action!="audition.enter")return false;
    if(before.at("screen_type")!="ProduceAuditionSelectScreenPresenter")
        throw std::runtime_error("audition action requires current selection presenter");
    auto drag=audition_drag(r,presenter);
    auto callback=r.read_object_field(drag,action=="audition.choose"?"onSelectStepCallback":"onDecideStepCallback");
    int index=target.at("index").get<int>();
    // Caller already requires an exact target match against the fresh native
    // candidate set. Invoke the event installed by the game's SetEvent; the
    // decide handler owns validation, animation, request and scene switching.
    r.invoke(r.method(r.object_class(callback),"Invoke",1),callback,{&index});
    return true;
}
}
