#include "outer_adapter.hpp"
#include "memory_auto_adapter.hpp"
#include "exam_model_revision.hpp"
#include "screen_context.hpp"
#include "native_trace.hpp"
#include "produce_card_ui_adapter.hpp"
#include "produce_lifecycle_adapter.hpp"
#include "produce_result_adapter.hpp"
#include "result_score_notice_adapter.hpp"
#include "customize_confirmation_adapter.hpp"
#include "produce_effect_view_adapter.hpp"
#include "story_adapter.hpp"
#include "pointer_identity.hpp"
#include "effect_confirmation.hpp"
#include "login_touch.hpp"
#include "audition_retry_adapter.hpp"
#include "error_adapter.hpp"
#include "mode_navigation_adapter.hpp"
#include "selection_memory_adapter.hpp"
#include "interval_adapter.hpp"
#include "drink_inventory_adapter.hpp"
#include "reward_drink_capacity_adapter.hpp"
#include "live_presentation_adapter.hpp"
#include "live_loading_adapter.hpp"
#include "shop_adapter.hpp"
#include "audition_result_adapter.hpp"
#include "recommended_replay_adapter.hpp"
#include "outer_pointer_guard.hpp"
#include "business_start_gate.hpp"
#include "event_choices_contract.hpp"
#include <array>
#include <cstddef>
#include <unordered_set>

namespace gkms::bridge {
namespace {
bool boolean(Runtime& runtime,void* object,const char* getter){return runtime.unbox<bool>(runtime.getter(object,getter));}
int integer(Runtime& runtime,void* object,const char* getter){return runtime.unbox<int>(runtime.getter(object,getter));}
json protobuf(Runtime& runtime,void* object){
    if(!object)return nullptr;
    return json::parse(runtime.string(runtime.getter(object,"ToString")));
}
void* user_static(Runtime& runtime,const char* getter){
    auto manager=runtime.klass("Assembly-CSharp","Campus.Common.User","UserDataManager");
    return runtime.invoke(runtime.method(manager,getter,0),nullptr);
}
std::vector<void*> collection(Runtime& runtime,const char* getter){
    auto values=user_static(runtime,getter);
    auto all=runtime.getter(values,"GetAll");
    const auto rows=runtime.enumerate(all,4096);
    if(integer(runtime,values,"get_Count")!=static_cast<int>(rows.size()))throw std::runtime_error("outer collection count changed");
    return rows;
}
bool active_component(Runtime& runtime,void* component){
    return component&&boolean(runtime,runtime.getter(component,"get_gameObject"),"get_activeInHierarchy");
}
void* presenter_view(Runtime& runtime,void* presenter){return runtime.read_object_field(presenter,"_view");}
json native_action(const char* id,json target,json evidence=json::object()){
    target["action_id"]=id;
    return {{"action_id",id},{"target",target},{"evidence",evidence}};
}
struct ButtonBinding {const char* screen;const char* id;const char* getter;const char* field;};
const std::array<ButtonBinding,20> navigation_buttons={{{"DailyLoginBonusScreenPresenter","login.continue",nullptr,"_overlayButton"},
    {"TitlePresenter","title.start","get_StartButton",nullptr},
    {"EventSimpleLoginBonusScreenPresenter","login.continue",nullptr,"_overlayButton"},
    {"EventSpecialLoginBonusScreenPresenter","login.continue",nullptr,"_overlayButton"},
    {"ProduceTopScreenPresenter","produce.select","get_ProduceSelectButton",nullptr},
    {"ProduceTopScreenPresenter","produce.next_mode","get_NextButton",nullptr},
    {"ProduceTopScreenPresenter","produce.previous_mode","get_PrevButton",nullptr},
    {"ProduceTopScreenPresenter","produce.toggle_difficulty","get_DifficultyToggleButton",nullptr},
    {"ProduceIdolSelectScreenPresenter","produce.idol_continue","get_EnterButton",nullptr},
    {"ProduceSupportCardSelectScreenPresenter","produce.support_continue","get_EnterButton",nullptr},
    {"ProduceMemorySelectScreenPresenter","produce.memory_continue","get_EnterButton",nullptr},
    {"ProduceStartScreenPresenter","produce.start","get_EnterButton",nullptr},
    {"ScheduleScreenPresenter","schedule.refresh","get_RefreshButton",nullptr},
    {"HomeTopScreenPresenter","home.produce",nullptr,nullptr},
    {"ScheduleSelfLessonScreenPresenter","lesson.result_continue","get_ResultCompleteButton",nullptr},
    {"AuditionBattleStartScreenPresenter","audition.start","get_StartButton",nullptr},
    {"AuditionBattleResultScreenPresenter","audition.result_continue","get_GoNextButton",nullptr},
    {"ProduceResultLastNiaScreenPresenter","produce.result_finish","get_EndButton",nullptr},
    {"ProduceBeforeLiveEvaluateNiaScreenPresenter","produce.evaluation_continue","get_ConfirmButton",nullptr},
    {"ExamResultScreenPresenter","exam.result_continue","get_ResultCompleteButton",nullptr}}};
void* navigation_button(Runtime& runtime,void* presenter,const ButtonBinding& binding){
    auto view=presenter_view(runtime,presenter);
    if(std::string(binding.id)=="home.produce"){
        auto group=runtime.read_object_field(presenter,"_pageGroup");
        auto page=runtime.getter(group,"GetCurrentPage");
        auto page_view=runtime.read_object_field(page,"_view");
        auto produce=runtime.getter(page_view,"get_ProduceButton");
        auto home_button=runtime.read_object_field(produce,"_homePageButton");
        return runtime.read_object_field(home_button,"_button");
    }
    return binding.getter?runtime.getter(view,binding.getter):runtime.read_object_field(view,binding.field);
}
bool clickable(Runtime& runtime,void* button){
    return active_component(runtime,button)&&boolean(runtime,button,"get_IsEnabled")&&
        !boolean(runtime,button,"get_IsDisabled")&&runtime.read_object_field(button,"onClickedCallback");
}
json login_touch_state(Runtime& runtime,void* presenter,void* button,const std::string& screen){
    json state={{"schema","gkms.login-touch.v1"},{"owner_type",screen},
        {"owner_instance_id",pointer_identity(presenter)},{"button_instance_id",pointer_identity(button)},
        {"active",active_component(runtime,button)},
        {"enabled",button&&boolean(runtime,button,"get_IsEnabled")},
        {"disabled",!button||boolean(runtime,button,"get_IsDisabled")},{"waiters",json::array()}};
    auto callback=button?runtime.read_object_field(button,"onClickedCallback"):nullptr;
    if(callback){
        // LoginBonusScreenViewBase.WaitScreenTouchAsync registers WaitClickAsync
        // on this exact overlay button. A generic delegate pointer is not a
        // stage: bind only that named closure and its actual completion source.
        for(auto entry:runtime.enumerate(runtime.getter(callback,"GetInvocationList"),64)){
            auto method=runtime.getter(entry,"get_Method");
            if(!method||runtime.string(runtime.getter(method,"get_Name"))!="<WaitClickAsync>g__OnClicked|0")continue;
            auto closure=runtime.getter(entry,"get_Target");
            if(!closure||!runtime.has_field(runtime.object_class(closure),"completionSource")||
               !runtime.has_field(runtime.object_class(closure),"<>4__this"))
                throw std::runtime_error("login WaitClickAsync closure contract unavailable");
            if(runtime.read_object_field(closure,"<>4__this")!=button)continue;
            auto source=runtime.read_object_field(closure,"completionSource");
            if(!source||runtime.class_name(runtime.object_class(source))!="UniTaskCompletionSource")
                throw std::runtime_error("login WaitClickAsync completion source unavailable");
            const int status=integer(runtime,source,"UnsafeGetStatus");
            if(status<0||status>3)throw std::runtime_error("unknown login touch completion status");
            state["waiters"].push_back({{"source","CampusButtonBase.WaitClickAsync"},
                {"closure_instance_id",pointer_identity(closure)},
                {"completion_source_instance_id",pointer_identity(source)},{"status",status}});
        }
    }
    state["ready"]=!native_login_touch_target(state).is_null();
    return state;
}
struct IdolSelectionRow {void* identity;void* card;bool eligible;};
std::vector<IdolSelectionRow> idol_selection_rows(Runtime& runtime,void* model,bool inherited){
    std::vector<IdolSelectionRow> rows;
    if(inherited){
        auto data=runtime.getter(model,"get_CardListData");
        for(auto item:runtime.enumerate(runtime.getter(data,"get_FilteredItemModels"),4096)){
            const bool eligible=!boolean(runtime,item,"get_IsHeading")&&!boolean(runtime,item,"get_IsUnreleased")&&
                !boolean(runtime,item,"get_IsDisableChallengeMode")&&!boolean(runtime,item,"get_IsDisableProduceCharacterCondition");
            rows.push_back({item,eligible?runtime.getter(item,"get_UserCard"):nullptr,eligible});
        }
        return rows;
    }
    // Item-model caches are rebuilt after display filtering. Read the actual
    // owned collection instead: UserIdolCard itself implements IUserCard.
    auto manager_type=runtime.klass("Assembly-CSharp.dll","Campus.Common.User","UserDataManager");
    auto manager=runtime.invoke(runtime.method(manager_type,"get_Instance",0,0x06016C2A),nullptr);
    auto owned=runtime.getter(manager,"get_UserIdolCardList");
    const int expected=integer(runtime,owned,"get_Count");
    const auto cards=runtime.enumerate(runtime.getter(owned,"GetAll"),4096);
    if(expected<0||cards.size()!=static_cast<std::size_t>(expected))throw std::runtime_error("owned idol count changed");
    auto info=runtime.getter(model,"get_SelectInfo");
    auto produce=runtime.getter(info,"get_Produce");
    auto disable_type=runtime.klass("Assembly-CSharp.dll","Campus.OutGame","ProduceIdolSelectDisableData");
    auto disable_ctor=runtime.method(disable_type,".ctor",2,0x0600EC46);
    std::unordered_set<std::string> ids;
    for(auto card:cards){
        if(runtime.class_name(runtime.object_class(card))!="UserIdolCard")throw std::runtime_error("owned idol type changed");
        const auto id=runtime.string(runtime.getter(card,"get_CardId"));
        if(id.empty()||!ids.insert(id).second)throw std::runtime_error("owned idol identity missing or duplicated");
        auto character=runtime.getter(card,"get_Character");
        if(runtime.class_name(runtime.object_class(character))!="Character")throw std::runtime_error("owned idol character type changed");
        // Use the game's normal eligibility calculation. GetDisableData on
        // the screen model can miss characters absent from its filtered map.
        auto disabled=runtime.new_object(disable_type);
        runtime.invoke(disable_ctor,disabled,{character,produce});
        if(runtime.string(runtime.getter(disabled,"get_CharacterId"))!=runtime.string(runtime.getter(character,"get_Id")))
            throw std::runtime_error("owned idol eligibility identity changed");
        rows.push_back({card,card,!boolean(runtime,disabled,"get_IsDisable")});
    }
    return rows;
}
void append_navigation(Runtime& runtime,void* presenter,const std::string& screen,json& actions,json& blockers,json& state){
    for(const auto& binding:navigation_buttons){
        if(screen!=binding.screen)continue;
        if(screen=="ScheduleScreenPresenter"&&!schedule_input_eligible(runtime.field<bool>(presenter,"_isStepDeciding"),
            runtime.field<bool>(presenter,"_isStepDecided"),runtime.field<bool>(presenter,"_isStartTutorial")))continue;
        try{
            auto button=navigation_button(runtime,presenter,binding);
            if(std::string(binding.id)=="login.continue"){
                auto touch=login_touch_state(runtime,presenter,button,screen);
                state["login_touch"]=touch;
                auto target=native_login_touch_target(touch);
                if(!target.is_null())actions.push_back(native_action("ui.navigation",target));
            }else if(clickable(runtime,button))actions.push_back(native_action("ui.navigation",{{"button_id",binding.id}}));
        }catch(const std::exception& error){blockers.push_back(std::string(binding.id)+":"+error.what());}
    }
}
json selection_state(Runtime& runtime,void* presenter,const std::string& screen,json& actions){
    if(screen=="ProduceTopScreenPresenter"){
        auto model=runtime.read_object_field(presenter,"_model");
        const int current_index=integer(runtime,model,"get_CurrentIndex");
        json state={{"current_index",current_index},{"is_high_score_rush",boolean(runtime,model,"get_IsSelectedHighScoreRushMode")},
            {"is_research",boolean(runtime,model,"get_IsSelectedResearchEventMode")},{"available_produces",json::array()}};
        auto items=runtime.enumerate(runtime.getter(model,"GetItemModels"),64);
        for(auto item:items){
            if(integer(runtime,item,"get_Index")!=current_index)continue;
            auto group=runtime.getter(item,"get_ProduceGroup");
            const auto group_id=runtime.string(runtime.getter(group,"get_Id"));
            state["produce_group_id"]=group_id;
            const auto produces=runtime.enumerate(runtime.getter(item,"GetCurrentIndexProduces"),32);
            const bool unlocked=boolean(runtime,item,"get_IsUnlockConditionValid");
            // The exact presenter handler owns all campaign/tutorial/normal
            // navigation, just as the actual mode-card event does.
            runtime.method(runtime.object_class(presenter),"TryMoveToNextScreenAsync",3);
            for(auto produce:produces){
                const auto id=runtime.string(runtime.getter(produce,"get_Id"));
                state["available_produces"].push_back(protobuf(runtime,produce));
                if(unlocked)actions.push_back(native_action("produce.choose_mode",{{"produce_group_id",group_id},{"produce_id",id}},{{"produce",protobuf(runtime,produce)}}));
            }
        }
        return state;
    }
    if(screen=="ProduceIdolSelectScreenPresenter"||screen=="ProduceSupportCardSelectScreenPresenter"||
        screen=="ProduceMemorySelectScreenPresenter"||screen=="ProduceStartScreenPresenter"){
        auto model=runtime.read_object_field(presenter,"_model");
        auto info=(screen=="ProduceIdolSelectScreenPresenter"||screen=="ProduceStartScreenPresenter")?
            runtime.getter(model,"get_SelectInfo"):runtime.read_object_field(model,"_selectInfo");
        auto produce=runtime.getter(info,"get_Produce");
        auto idol=runtime.getter(info,"get_UserIdolCard");
        json state={{"produce_id",runtime.string(runtime.getter(produce,"get_Id"))},
            {"idol_card_id",idol?json(runtime.string(runtime.getter(idol,"get_CardId"))):json(nullptr)}};
        if(screen=="ProduceSupportCardSelectScreenPresenter"||screen=="ProduceMemorySelectScreenPresenter"){
            auto support_edit=runtime.getter(info,"get_SupportCardDeckEditInfo");
            auto rentals=runtime.getter(support_edit,"get_RentalCards");
            const bool enabled=boolean(runtime,support_edit,"get_IsRentalEnabled");
            state["rental_enabled"]=enabled;state["rental_loaded"]=rentals!=nullptr;
            state["rental_count"]=rentals?runtime.enumerate(rentals,10000).size():0;
            if(enabled&&!rentals)actions.push_back(native_action("loadout.refresh_rentals",json::object()));
        }
        if(screen=="ProduceIdolSelectScreenPresenter"){
            const bool transitioning=boolean(runtime,model,"get_IsTransitioning");
            const bool blocked=boolean(runtime,model,"get_IsBlockSetCurrentCard");
            state["is_transitioning"]=transitioning;state["is_block_set_current_card"]=blocked;
            state["offered_idol_card_ids"]=json::array();
            const bool inherited=boolean(runtime,model,"get_IsHifFinalRound");
            const auto items=idol_selection_rows(runtime,model,inherited);
            state["idol_pool_source"]=inherited?"filtered-inherited-preparation":"account-owned-idols";
            state["idol_pool_count"]=items.size();
            if(inherited)runtime.method(runtime.object_class(presenter),"TrySetCurrentCard",2,0x0600EC95);
            else runtime.method(runtime.object_class(model),"SetCurrentCard",1,0x0600EC56);
            for(std::size_t index=0;index<items.size();++index){
                const auto& item=items[index];
                if(!item.eligible)continue;
                auto user_card=item.card;
                if(!user_card)continue;
                const auto id=runtime.string(runtime.getter(user_card,"get_CardId"));
                if(id.empty())throw std::runtime_error("offered idol identity missing");
                state["offered_idol_card_ids"].push_back(id);
                if(!transitioning&&!blocked)actions.push_back(native_action("produce.choose_idol",{{"index",index},{"idol_card_id",id},
                    {"source_pool",state.at("idol_pool_source")},{"model_instance_id",pointer_identity(model)},
                    {"item_instance_id",pointer_identity(item.identity)}}));
            }
            if(transitioning||blocked){
                for(auto iterator=actions.begin();iterator!=actions.end();){
                    if(iterator->at("action_id")=="ui.navigation")iterator=actions.erase(iterator);else ++iterator;
                }
            }
        }
        append_selection_memory_prepare(runtime,presenter,model,info,produce,screen,state,actions);
        return state;
    }
    return nullptr;
}
bool supported_sheet(const std::string& name){
    const std::array<const char*,10> names={"ProduceRefreshConfirmSheetPresenter","ScheduleShopConfirmSheetPresenter","ScheduleSelectChangeConfirmSheetPresenter",
        "ExamTurnEndConfirmSheetPresenter","ProducePresentSkipConfirmSheetPresenter","ProduceAuditionContinueConfirmSheetPresenter","CardPlayWarningSheetPresenter","ProduceCardSelectShortageSheetPresenter","StartExchangeItemExpireSheetPresenter","ProduceMemorySelectRentalEnableConfirmSheetPresenter"};
    for(auto value:names)if(name==value)return true;return false;
}
json event_confirmation_actions(Runtime& runtime,void* layer,json& state){
    json actions=json::array();
    auto event=active_screen(runtime);
    if(!event||runtime.class_name(runtime.object_class(event))!="ScheduleEventScreenPresenter")return actions;
    auto model=runtime.read_object_field(event,"_model");
    if(!model||boolean(runtime,model,"get_IsEventExecuted"))return actions;
    auto choices=runtime.read_object_field(event,"_choicesView");
    if(!choices||runtime.class_name(runtime.object_class(choices))!="ProduceChoicesView")return actions;
    const auto selected=runtime.nullable_int32_field(choices,"_selectedIndex");
    if(!selected||*selected<0)return actions;
    auto suggestions=runtime.enumerate(runtime.getter(model,"get_Suggestions"),64);
    if(static_cast<std::size_t>(*selected)>=suggestions.size())return actions;
    const auto event_id=runtime.string(runtime.getter(model,"get_StepEventDetailId"));
    const auto suggestion_id=runtime.string(runtime.getter(suggestions.at(*selected),"get_Id"));
    auto button=runtime.getter(layer,"get_ExecuteButton");
    state.update({{"event_detail_id",event_id},{"selected_index",*selected},{"suggestion_id",suggestion_id}});
    if(clickable(runtime,button))actions.push_back(native_action("event.confirm_choice",{{"event_detail_id",event_id},
        {"index",*selected},{"suggestion_id",suggestion_id},{"button_instance_id",pointer_identity(button)},
        {"callback_instance_id",pointer_identity(runtime.read_object_field(button,"onClickedCallback"))}}));
    return actions;
}
json layer_actions(Runtime& runtime,void* layer,json& state){
    json actions=json::array();
    const auto name=runtime.class_name(runtime.object_class(layer));
    const bool closing=boolean(runtime,layer,"get_IsClosing");
    state={{"closing",closing}};
    if(closing)return actions;
    if(name=="SimpleSheetPresenter")return event_confirmation_actions(runtime,layer,state);
    if(!supported_sheet(name))return actions;
    const bool disabled=boolean(runtime,layer,"get_IsDisableInteraction");state["disabled"]=disabled;
    if(disabled)return actions;
    auto view=presenter_view(runtime,layer);
    if(name=="ProduceMemorySelectRentalEnableConfirmSheetPresenter"){
        // The game's dialog utility returns Model.IsExecute. The caller
        // proceeds to ProduceStart only when true, preserving the current
        // non-rental memories; false returns to the memory-selection page.
        auto common=runtime.getter(view,"get_CommonView");
        auto execute=runtime.getter(common,"get_ExecuteButton");
        state["continue_without_rental_enabled"]=clickable(runtime,execute);
        if(state["continue_without_rental_enabled"]==true)
            actions.push_back(native_action("loadout.memory_continue_without_rental",json::object(),{{"native_result","SheetModelBase.IsExecute=true"}}));
        return actions;
    }
    if(name=="StartExchangeItemExpireSheetPresenter"){
        // The expiry notice has its own ButtonGroup. Its Execute callback can
        // navigate into an exchange event; only the normal dismiss control is
        // part of the cultivation startup route.
        auto group=runtime.getter(view,"get_ButtonGroup");
        auto cancel=runtime.getter(group,"get_CancelButton");
        if(clickable(runtime,cancel))actions.push_back(native_action("ui.sheet_cancel",{{"button_source","expiry-notice"}},{{"semantics","dismiss-startup-expiry-notice"}}));
        else{
            auto common=runtime.getter(view,"get_CommonView");
            cancel=runtime.getter(common,"get_CancelButton");
            if(clickable(runtime,cancel))actions.push_back(native_action("ui.sheet_cancel",{{"button_source","common"}},{{"semantics","dismiss-startup-expiry-notice"}}));
        }
        return actions;
    }
    auto common=runtime.getter(view,"get_CommonView");
    if(boolean(runtime,layer,"get_IsShowExecuteButton")&&clickable(runtime,runtime.getter(common,"get_ExecuteButton")))
        actions.push_back(native_action("ui.sheet_confirm",{{"button_source","common"}}));
    if(boolean(runtime,layer,"get_IsShowCancelButton")&&clickable(runtime,runtime.getter(common,"get_CancelButton")))
        actions.push_back(native_action("ui.sheet_cancel",{{"button_source","common"}}));
    return actions;
}
json schedule_actions(Runtime& runtime,void* presenter,json& state){
    auto view=presenter_view(runtime,presenter);
    if(!active_component(runtime,view))throw std::runtime_error("schedule view inactive");
    const bool deciding=runtime.field<bool>(presenter,"_isStepDeciding");
    const bool decided=runtime.field<bool>(presenter,"_isStepDecided");
    const bool tutorial=runtime.field<bool>(presenter,"_isStartTutorial");
    state={{"deciding",deciding},{"decided",decided},{"tutorial",tutorial}};
    auto step_data=runtime.read_object_field(view,"_stepDataList");
    state["data_ready"]=step_data!=nullptr;
    json actions=json::array();
    if(!step_data)return actions; // The normal view initialization has not populated this list yet.
    auto values=runtime.enumerate(step_data,64);
    if(!schedule_input_eligible(deciding,decided,tutorial))return actions;
    if(!runtime.read_object_field(view,"onStepButtonDecide"))throw std::runtime_error("schedule decision delegate unavailable");
    for(std::size_t index=0;index<values.size();++index){
        auto step=values[index];
        const auto step_type=integer(runtime,step,"get_StepType");
        actions.push_back(native_action("schedule.choose",{{"index",index},{"step_type",step_type}},
            {{"parameter_type",integer(runtime,step,"get_ParameterType")}}));
    }
    return actions;
}
json event_actions(Runtime& runtime,void* presenter,json& state){
    auto model=runtime.read_object_field(presenter,"_model");
    const bool busy=runtime.field<bool>(presenter,"_isBusy")||boolean(runtime,model,"get_IsEventExecuting");
    auto choices=runtime.read_object_field(presenter,"_choicesView");
    const auto detail_id=runtime.string(runtime.getter(model,"get_StepEventDetailId"));
    state={{"busy",busy},{"event_detail_id",detail_id},{"executed",boolean(runtime,model,"get_IsEventExecuted")}};
    state["selected_index"]=nullptr;
    const auto choices_type=choices?runtime.class_name(runtime.object_class(choices)):std::string();
    state["choices_type"]=choices_type;
    if(choices&&choices_type=="ProduceChoicesView"){
        const auto selected=runtime.nullable_int32_field(choices,"_selectedIndex");
        if(selected)state["selected_index"]=*selected;
    }
    json actions=json::array();
    if(busy||state["executed"]==true||!choices||!active_component(runtime,choices))return actions;
    const bool ordinary=ordinary_event_choices(choices_type);
    const bool choices_shown=boolean(runtime,choices,"get_IsShow");
    state["choices_shown"]=choices_shown;
    state["choices_owner"]=ordinary?"native-adv":"native-produce-event";
    // The same presenter keeps a normal ADV choices component. It is not a
    // ProduceStepEventSuggestion control, even though both implement IChoicesView.
    // Hidden ordinary choices belong to the existing story lifecycle; a visible
    // ordinary choice needs its own ADV data contract and cannot borrow server IDs.
    if(!event_choices_use_produce_suggestions(choices_type,choices_shown))return actions;
    const int count=runtime.field<int>(choices,"_choicesCount");
    const auto buttons=runtime.enumerate(runtime.read_object_field(choices,"_buttons"),64);
    const auto suggestions=runtime.enumerate(runtime.getter(model,"get_Suggestions"),64);
    if(count<0||static_cast<std::size_t>(count)>buttons.size()||static_cast<std::size_t>(count)!=suggestions.size())throw std::runtime_error("event choices identity count differs");
    for(int index=0;index<count;++index){
        auto button=buttons[index];
        if(!active_component(runtime,button)||!boolean(runtime,button,"get_IsValid")||boolean(runtime,button,"get_IsDisabled"))continue;
        const auto suggestion=protobuf(runtime,suggestions[index]);
        const auto id=runtime.string(runtime.getter(suggestions[index],"get_Id"));
        if(id.empty())throw std::runtime_error("event suggestion ID unavailable");
        if(!runtime.read_object_field(button,"OnExecuted")||!runtime.read_object_field(button,"OnPressed"))throw std::runtime_error("event choice callback unavailable");
        actions.push_back(native_action("event.choose",{{"index",index},{"suggestion_id",id},{"event_detail_id",detail_id}},{{"suggestion",suggestion}}));
    }
    return actions;
}
json business_start_lifecycle(Runtime& runtime,const json& observed){
    json result={{"input_ready",false},{"context",nullptr},{"phase","awaiting_business_start"},
        {"source","native UserProduceProgress.InProgressStep; Business NonBlockOpenInAsync Start branch"}};
    try{
        auto progress=user_static(runtime,"get_UserProduceProgress");
        if(!progress){result["phase"]="awaiting_native_progress";return result;}
        const json current={{"produce_id",runtime.string(runtime.getter(progress,"get_ProduceId"))},
            {"idol_card_id",runtime.string(runtime.getter(progress,"get_IdolCardId"))},
            {"week",integer(runtime,progress,"get_StepNumber")},{"step_type",integer(runtime,progress,"get_StepType")},
            {"progress_status",integer(runtime,progress,"get_Status")},{"in_progress",boolean(runtime,progress,"get_IsInProgress")},
            {"in_progress_step",runtime.unbox<bool>(runtime.invoke(runtime.method(runtime.object_class(progress),"get_InProgressStep",0,0x06019FFD),progress))}};
        result["context"]=current;
        result["input_ready"]=business_start_profile_ready(current,business_snapshot_profile(observed));
        result["phase"]=result.at("input_ready")==true?"business_step_started":"business_start_unverified";
    }catch(const std::exception& error){
        // This only suppresses the Start action. Local folder selection and
        // separately owned story/effect acknowledgement remain available.
        result["phase"]="business_start_unavailable";result["reason"]=error.what();
    }
    return result;
}
json business_actions(Runtime& runtime,void* presenter,json& state,const json& observed){
    auto view=presenter_view(runtime,presenter);
    const bool folders_active=runtime.field<bool>(view,"_isFoldersActive");
    auto gauge=runtime.read_object_field(view,"_produceParameterGaugeCanvasGroup");
    const bool visible=active_component(runtime,gauge)&&runtime.unbox<float>(runtime.getter(gauge,"get_alpha"))>.001f;
    const int selected_index=runtime.field<int>(view,"_currentSelectStepIndex");
    auto folder_values=runtime.read_object_field(view,"_folderViews");
    auto select_callback=runtime.read_object_field(view,"onSelectFolderCallback");
    state={{"folders_active",folders_active},{"selection_visible",visible},{"selected_index",selected_index},
        {"data_ready",folder_values!=nullptr},{"folders",json::array()}};
    const auto lifecycle=business_start_lifecycle(runtime,observed);
    state["start_lifecycle"]=lifecycle;
    json actions=json::array();
    if(!folder_values||!active_component(runtime,view))return actions;
    auto folders=runtime.enumerate(folder_values,32);
    const auto businesses=collection(runtime,"get_UserProduceProgressBusinessList");
    auto predicate=runtime.method(runtime.object_class(view),"CheckIfGoBusiness",1);
    for(std::size_t index=0;index<folders.size();++index){
        auto folder=folders[index];
        auto canvas=runtime.read_object_field(folder,"_folderCanvasGroup");
        const bool folder_visible=active_component(runtime,folder)&&active_component(runtime,canvas)&&
            runtime.unbox<float>(runtime.getter(canvas,"get_alpha"))>.001f;
        const auto type=integer(runtime,folder,"get_BusinessType");
        void* found{};
        for(auto business:businesses)if(integer(runtime,business,"get_BusinessType")==type){if(found)throw std::runtime_error("business identity ambiguous");found=business;}
        if(!found)continue;
        const bool valid=runtime.unbox<bool>(runtime.invoke(predicate,view,{found}));
        auto start=runtime.getter(folder,"get_BusinessStartButton");
        auto start_canvas=runtime.read_object_field(folder,"_businessStartButtonCanvasGroup");
        const bool start_visible=active_component(runtime,start_canvas)&&runtime.unbox<float>(runtime.getter(start_canvas,"get_alpha"))>.001f;
        const bool native_start_ready=visible&&folder_visible&&start_visible&&valid&&clickable(runtime,start);
        const bool start_ready=native_start_ready&&lifecycle.at("input_ready")==true;
        const auto row=protobuf(runtime,found);
        const int business_number=integer(runtime,found,"get_Number");
        state["folders"].push_back({{"index",index},{"business_type",type},{"business_number",business_number},
            {"selected",static_cast<int>(index)==selected_index},{"visible",folder_visible},{"can_go",valid},
            {"start_visible",start_visible},{"native_start_ready",native_start_ready},{"start_ready",start_ready},{"business",row}});
        json target={{"index",index},{"business_type",type},{"business_number",business_number},{"folder_instance_id",pointer_identity(folder)}};
        if(start_ready){
            target["button_instance_id"]=pointer_identity(start);
            target["callback_instance_id"]=pointer_identity(runtime.read_object_field(start,"onClickedCallback"));
            target["start_context"]=lifecycle.at("context");
            actions.push_back(native_action("business.start",target,{{"business",row}}));
        }
        if(visible&&folder_visible&&valid&&select_callback&&static_cast<int>(index)!=selected_index){
            target={{"index",index},{"business_type",type},{"business_number",business_number},{"folder_instance_id",pointer_identity(folder)},
                {"callback_instance_id",pointer_identity(select_callback)}};
            actions.push_back(native_action("business.choose",target,{{"business",row}}));
        }
    }
    return actions;
}
}
json native_outer_progress_state(Runtime& runtime,void* progress_object){
    if(!progress_object)return nullptr;
    json state={{"week",integer(runtime,progress_object,"get_StepNumber")},
        {"in_progress",boolean(runtime,progress_object,"get_IsInProgress")},
        {"produce_id",runtime.string(runtime.getter(progress_object,"get_ProduceId"))},
        {"step_type",integer(runtime,progress_object,"get_StepType")},{"progress_status",integer(runtime,progress_object,"get_Status")},
        {"stamina",integer(runtime,progress_object,"get_Stamina")},{"max_stamina",integer(runtime,progress_object,"get_MaxStamina")},
        {"produce_points",integer(runtime,progress_object,"get_ProducePoint")},
        {"vocal",integer(runtime,progress_object,"get_Vocal")},{"dance",integer(runtime,progress_object,"get_Dance")},
        {"visual",integer(runtime,progress_object,"get_Visual")},{"vote_count",integer(runtime,progress_object,"get_VoteCount")}};
    state.update(native_mode_progress_fields(runtime,progress_object));
    return state;
}
json read_outer_snapshot(Runtime& runtime,const std::string& generation){
    if(auto error=read_error_snapshot(runtime,generation);!error.is_null())return error;
    trace_mark("outer.screen_context.before");
    json context;void* presenter{};void* layer{};
    try{
        context=screen_context_snapshot(runtime);
        presenter=active_screen(runtime);layer=active_layer(runtime);
    }catch(const std::runtime_error& error){
        if(std::string(error.what())!="native-screen-root-unavailable")throw;
        auto live=read_live_presentation_snapshot(runtime,generation);
        if(!live.is_null())return live;
        throw;
    }
    trace_mark("outer.screen_context.after",context);
    if(layer)presenter=layer;
    const auto screen=runtime.class_name(runtime.object_class(presenter));
    if(context.at("screen_type")=="TitlePresenter"){
        // Title is its own QuaUIBehaviour lifecycle, before WindowNode and
        // logged-in UserData exist. Pure navigation does not consume stale
        // ProduceProgress from the previous account/run.
        json snapshot={{"schema","gkms.outer-runtime-snapshot.v1"},{"surface",layer?"layer":"navigation"},
            {"screen_type",screen},{"underlying_screen_type","TitlePresenter"},{"busy",context.at("tree_busy")},
            {"screen_instance_id",std::to_string(reinterpret_cast<std::uintptr_t>(presenter))},
            {"state",nullptr},{"progress",nullptr},{"state_available",false},{"progress_available",false},
            {"collections",json::object()},{"legal_actions",json::array()},{"blockers",json::array()},
            {"ui_state",json::object()},{"actions_complete",!layer}};
        if(context.at("tree_busy")==false){
            if(layer){snapshot["legal_actions"]=layer_actions(runtime,layer,snapshot["ui_state"]);snapshot["actions_complete"]=supported_sheet(screen);}
            else append_navigation(runtime,presenter,screen,snapshot["legal_actions"],snapshot["blockers"],snapshot["ui_state"]);
        }
        if(!snapshot["blockers"].empty())snapshot["actions_complete"]=false;
        apply_outer_pointer_guard(snapshot,read_outer_pointer_guard(runtime,presenter,layer!=nullptr));
        snapshot["revision"]=sha256(generation+exam_model_revision_view(snapshot).dump());snapshot["captured_at"]=utc_now();
        trace_mark("outer.title_snapshot.complete",{{"actions",snapshot["legal_actions"].size()}});return snapshot;
    }
    trace_mark("outer.progress.before");
    const auto progress=protobuf(runtime,user_static(runtime,"get_UserProduceProgress"));
    trace_mark("outer.progress.after");
    auto progress_object=user_static(runtime,"get_UserProduceProgress");
    json scalar_state=nullptr;
    if(progress_object){
        trace_mark("outer.scalar_state.before");
        scalar_state=native_outer_progress_state(runtime,progress_object);
        trace_mark("outer.scalar_state.after");
    }
    json collections=json::object();
    const std::array<std::pair<const char*,const char*>,10> getters={{{"schedule","get_UserProduceProgressScheduleList"},{"event","get_UserProduceProgressEventList"},
        {"business","get_UserProduceProgressBusinessList"},{"present","get_UserProduceProgressPresentList"},{"shop","get_UserProduceProgressShopList"},
        {"interval","get_UserProduceProgressIntervalList"},{"cards","get_UserProduceProgressProduceCardList"},{"effects","get_UserProduceProgressEffectList"},
        {"support_cards","get_UserProduceProgressSupportCardList"},{"memories","get_UserProduceProgressMemoryList"}}};
    for(const auto& [name,getter]:getters){
        trace_mark("outer.collection.before",{{"collection",name}});
        json rows=json::array();for(auto row:collection(runtime,getter))rows.push_back(protobuf(runtime,row));
        trace_mark("outer.collection.after",{{"collection",name},{"count",rows.size()}});collections[name]=std::move(rows);
    }
    json snapshot={{"schema","gkms.outer-runtime-snapshot.v1"},{"screen_type",screen},{"underlying_screen_type",context.at("screen_type")},{"busy",context.at("tree_busy")},
        {"progress",progress},{"state",scalar_state},{"screen_instance_id",std::to_string(reinterpret_cast<std::uintptr_t>(presenter))},
        {"collections",collections},{"surface","unsupported"},{"legal_actions",json::array()},{"actions_complete",false}};
    json state=json::object();
    json blockers=json::array();
    trace_mark("outer.actions.before");
    if(screen=="ExamScreenPresenter"&&!layer){snapshot["surface"]="exam";snapshot["actions_complete"]=true;}
    if(context.at("tree_busy")==false){
        if(layer){snapshot["surface"]="layer";snapshot["legal_actions"]=layer_actions(runtime,layer,state);snapshot["actions_complete"]=supported_sheet(screen)||!snapshot["legal_actions"].empty();}
        else if(screen=="ScheduleScreenPresenter"){snapshot["surface"]="schedule";snapshot["legal_actions"]=schedule_actions(runtime,presenter,state);snapshot["actions_complete"]=state.at("data_ready");}
        else if(screen=="ScheduleEventScreenPresenter"){snapshot["surface"]="event";snapshot["legal_actions"]=event_actions(runtime,presenter,state);snapshot["actions_complete"]=true;}
        else if(screen=="ScheduleBusinessScreenPresenter"){snapshot["surface"]="business";snapshot["legal_actions"]=business_actions(runtime,presenter,state,snapshot);snapshot["actions_complete"]=state.at("data_ready");}
        if(!layer){
            append_navigation(runtime,presenter,screen,snapshot["legal_actions"],blockers,state);
            if(native_login_screen(screen)){snapshot["surface"]="navigation";snapshot["actions_complete"]=true;}
            if(snapshot["surface"]=="unsupported"&&!snapshot["legal_actions"].empty()){
                snapshot["surface"]="navigation";snapshot["actions_complete"]=true;
            }
        }
    }
    if(!blockers.empty())snapshot["actions_complete"]=false;
    snapshot["blockers"]=blockers;
    snapshot["ui_state"]=state;
    if(!layer&&context.at("tree_busy")==false){
        try{
            snapshot["selection"]=selection_state(runtime,presenter,screen,snapshot["legal_actions"]);
            if(!snapshot["selection"].is_null()&&snapshot["surface"]=="unsupported"&&snapshot["blockers"].empty()){
                snapshot["surface"]="navigation";snapshot["actions_complete"]=true;
            }
        }
        catch(const std::exception& error){snapshot["blockers"].push_back(std::string("selection:")+error.what());snapshot["actions_complete"]=false;}
    }
    if(context.at("tree_busy")==false){
        append_produce_lifecycle_actions(runtime,presenter,screen,snapshot);
        append_mode_navigation_actions(runtime,presenter,screen,snapshot);
            append_recommended_replay_actions(runtime,presenter,screen,snapshot);
        append_audition_result_actions(runtime,presenter,screen,snapshot);
        append_interval_actions(runtime,presenter,screen,snapshot);
        append_drink_inventory_actions(runtime,presenter,screen,snapshot);
        append_shop_actions(runtime,presenter,screen,snapshot);
        append_produce_result_actions(runtime,presenter,screen,snapshot);
        append_result_score_notice_actions(runtime,presenter,screen,snapshot);
        append_customize_confirmation_actions(runtime,presenter,screen,snapshot);
        append_story_actions(runtime,presenter,screen,snapshot);
        append_audition_retry_actions(runtime,presenter,screen,snapshot);
        project_card_then_effect_views(
            [&]{append_produce_card_ui_actions(runtime,presenter,screen,snapshot);},
            [&]{append_effect_confirmation(runtime,presenter,screen,snapshot);},
            [&]{append_produce_effect_view_actions(runtime,presenter,screen,snapshot);});
        append_reward_drink_capacity_actions(runtime,presenter,screen,snapshot);
    }
    // Observe only our explicitly owned task, including while its UI is busy.
    try{append_memory_auto_actions(runtime,presenter,screen,snapshot);}
    catch(const std::exception& error){snapshot["blockers"].push_back(std::string("memory-auto:")+error.what());snapshot["actions_complete"]=false;}
    // Retain the exact original completion source even if a normal loading
    // press has already moved to a standard screen. This adds no legal input.
    snapshot["live_loading_receipt"]=read_live_loading_receipt(runtime);
    if(protobuf(runtime,user_static(runtime,"get_UserProduceProgress"))!=progress)throw std::runtime_error("outer progress changed while copying");
    apply_outer_pointer_guard(snapshot,read_outer_pointer_guard(runtime,presenter,layer!=nullptr,foreground_effect_pointer_target(snapshot)));
    snapshot["revision"]=sha256(generation+exam_model_revision_view(snapshot).dump());
    snapshot["captured_at"]=utc_now();
    trace_mark("outer.snapshot.complete",{{"actions",snapshot["legal_actions"].size()},{"surface",snapshot["surface"]}});
    return snapshot;
}
void submit_outer_action(Runtime& runtime,const json& target,const json& before){
    bool found=false;
    for(const auto& action:before.at("legal_actions"))if(action.at("target")==target){if(found)throw std::runtime_error("ambiguous outer target");found=true;}
    if(!found||before.at("busy")!=false)throw std::runtime_error("outer target not present in native candidate set");
    if(target.at("action_id")=="error.return_title"){submit_error_return(runtime,target,before);return;}
    if(submit_live_presentation_action(runtime,target,before))return;
    auto presenter=active_screen(runtime);
    auto layer=active_layer(runtime);if(layer)presenter=layer;
    if(runtime.class_name(runtime.object_class(presenter))!=before.at("screen_type").get<std::string>())throw std::runtime_error("outer screen changed before command");
    if(target.at("action_id")=="effect.advance"&&foreground_effect_pointer_target(before)!=target)
        throw std::runtime_error("effect pointer target is not the sole foreground recipient");
    if(read_outer_pointer_guard(runtime,presenter,layer!=nullptr,target).at("input_ready")!=true)
        throw std::runtime_error("native pointer blocking intercepts the foreground outer control");
    if(submit_memory_auto_action(runtime,presenter,target,before))return;
    if(submit_mode_navigation_action(runtime,presenter,target,before))return;
    if(submit_recommended_replay_action(runtime,presenter,target,before))return;
    if(submit_audition_result_action(runtime,presenter,target,before))return;
    if(submit_selection_memory_action(runtime,presenter,target,before))return;
    if(submit_interval_action(runtime,presenter,target,before))return;
    if(submit_drink_inventory_action(runtime,presenter,target,before))return;
    if(submit_reward_drink_capacity_action(runtime,presenter,target,before))return;
    if(submit_shop_action(runtime,presenter,target,before))return;
    if(submit_produce_card_ui_action(runtime,presenter,target,before))return;
    if(submit_produce_lifecycle_action(runtime,presenter,target,before))return;
    if(submit_result_score_notice_action(runtime,presenter,target,before))return;
    if(submit_customize_confirmation_action(runtime,presenter,target,before))return;
    if(submit_produce_result_action(runtime,presenter,target,before))return;
    if(submit_produce_effect_view_action(runtime,presenter,target,before))return;
    if(submit_story_action(runtime,presenter,target,before))return;
    if(submit_audition_retry_action(runtime,presenter,target,before))return;
    auto view=presenter_view(runtime,presenter);
    const auto action=target.at("action_id").get<std::string>();
    if(action=="schedule.choose"){
        auto callback=runtime.read_object_field(view,"onStepButtonDecide");
        int step=target.at("step_type").get<int>();
        runtime.invoke(runtime.method(runtime.object_class(callback),"Invoke",1),callback,{&step});
    }else if(action=="event.choose"){
        auto choices=runtime.read_object_field(presenter,"_choicesView");
        const auto buttons=runtime.enumerate(runtime.read_object_field(choices,"_buttons"),64);
        auto button=buttons.at(target.at("index").get<std::size_t>());
        // The real press selects the index; release updates clickability.
        // The normal inner button click then runs OnExecuteAsync, including
        // OnCanExecuteTask. OnExecuted alone has no selected-index contract.
        auto pressed=runtime.read_object_field(button,"OnPressed");
        auto press_method=runtime.method(runtime.object_class(pressed),"Invoke",1);
        bool down=true;runtime.invoke(press_method,pressed,{&down});
        down=false;runtime.invoke(press_method,pressed,{&down});
        auto inner=runtime.read_object_field(button,"_button");
        runtime.invoke(runtime.method(runtime.object_class(inner),"OnClickedHandler",0),inner);
    }else if(action=="event.confirm_choice"){
        if(before.at("screen_type")!="SimpleSheetPresenter"||before.at("underlying_screen_type")!="ScheduleEventScreenPresenter")
            throw std::runtime_error("event confirmation owner changed");
        auto button=runtime.getter(presenter,"get_ExecuteButton");
        if(!clickable(runtime,button)||pointer_identity(button)!=target.at("button_instance_id").get<std::string>()||
            pointer_identity(runtime.read_object_field(button,"onClickedCallback"))!=target.at("callback_instance_id").get<std::string>())
            throw std::runtime_error("event affirmative button changed");
        runtime.invoke(runtime.method(runtime.object_class(button),"OnClickedHandler",0),button);
    }else if(action=="business.choose"||action=="business.start"){
        int index=target.at("index").get<int>();
        auto folders=runtime.enumerate(runtime.read_object_field(view,"_folderViews"),32);
        auto folder=folders.at(static_cast<std::size_t>(index));
        if(pointer_identity(folder)!=target.at("folder_instance_id").get<std::string>()||integer(runtime,folder,"get_BusinessType")!=target.at("business_type").get<int>())
            throw std::runtime_error("business folder identity changed");
        if(action=="business.choose"){
            auto callback=runtime.read_object_field(view,"onSelectFolderCallback");
            if(pointer_identity(callback)!=target.at("callback_instance_id").get<std::string>())throw std::runtime_error("business selection callback changed");
            runtime.invoke(runtime.method(runtime.object_class(callback),"Invoke",1),callback,{&index});
        }else{
            const auto lifecycle=business_start_lifecycle(runtime,before);
            if(lifecycle.at("input_ready")!=true||
               !business_start_target_matches(target,lifecycle.at("context"),business_snapshot_profile(before)))
                throw std::runtime_error("native Business Start is not complete for this current produce/step/profile");
            void* business{};
            for(auto candidate:collection(runtime,"get_UserProduceProgressBusinessList"))
                if(integer(runtime,candidate,"get_BusinessType")==target.at("business_type").get<int>()&&
                   integer(runtime,candidate,"get_Number")==target.at("business_number").get<int>()){
                    if(business)throw std::runtime_error("business start item is ambiguous");business=candidate;
                }
            if(!business||!runtime.unbox<bool>(runtime.invoke(runtime.method(runtime.object_class(view),"CheckIfGoBusiness",1),view,{business})))
                throw std::runtime_error("business start item or native resource eligibility changed");
            auto button=runtime.getter(folder,"get_BusinessStartButton");
            if(!clickable(runtime,button)||pointer_identity(button)!=target.at("button_instance_id").get<std::string>()||
                pointer_identity(runtime.read_object_field(button,"onClickedCallback"))!=target.at("callback_instance_id").get<std::string>())
                throw std::runtime_error("business start button changed");
            runtime.invoke(runtime.method(runtime.object_class(button),"OnClickedHandler",0),button);
        }
    }else if(action=="produce.choose_mode"){
        auto model=runtime.read_object_field(presenter,"_model");
        const int current_index=integer(runtime,model,"get_CurrentIndex");
        void* group{};void* produce{};
        for(auto item:runtime.enumerate(runtime.getter(model,"GetItemModels"),64)){
            if(integer(runtime,item,"get_Index")!=current_index)continue;
            auto candidate_group=runtime.getter(item,"get_ProduceGroup");
            if(runtime.string(runtime.getter(candidate_group,"get_Id"))!=target.at("produce_group_id").get<std::string>())continue;
            for(auto candidate:runtime.enumerate(runtime.getter(item,"GetCurrentIndexProduces"),32)){
                if(runtime.string(runtime.getter(candidate,"get_Id"))==target.at("produce_id").get<std::string>()){
                    if(produce)throw std::runtime_error("produce mode ambiguous");group=candidate_group;produce=candidate;
                }
            }
        }
        if(!group||!produce)throw std::runtime_error("produce mode no longer offered");
        auto ct=runtime.getter(presenter,"GetActiveCt");
        runtime.invoke(runtime.method(runtime.object_class(presenter),"TryMoveToNextScreenAsync",3),presenter,{group,produce,runtime.unbox_pointer(ct)});
    }else if(action=="produce.choose_idol"){
        if(before.at("screen_type")!="ProduceIdolSelectScreenPresenter")throw std::runtime_error("idol selection presenter changed");
        auto model=runtime.read_object_field(presenter,"_model");
        if(boolean(runtime,model,"get_IsTransitioning")||boolean(runtime,model,"get_IsBlockSetCurrentCard"))
            throw std::runtime_error("idol selection is transitioning");
        const bool inherited=boolean(runtime,model,"get_IsHifFinalRound");
        const auto source=inherited?"filtered-inherited-preparation":"account-owned-idols";
        if(target.at("source_pool")!=source||target.at("model_instance_id")!=pointer_identity(model))
            throw std::runtime_error("native idol source changed");
        const auto items=idol_selection_rows(runtime,model,inherited);
        const auto& item=items.at(target.at("index").get<std::size_t>());
        if(target.at("item_instance_id")!=pointer_identity(item.identity)||!item.eligible||!item.card)
            throw std::runtime_error("native idol is unavailable");
        auto card=item.card;
        auto id=runtime.getter(card,"get_CardId");
        if(runtime.string(id)!=target.at("idol_card_id").get<std::string>())throw std::runtime_error("native idol index identity changed");
        if(inherited){
            bool immediate=false;
            runtime.invoke(runtime.method(runtime.object_class(presenter),"TrySetCurrentCard",2,0x0600EC95),presenter,{&immediate,id});
        }else{
            runtime.invoke(runtime.method(runtime.object_class(model),"SetCurrentCard",1,0x0600EC56),model,{card});
        }
    }else if(action=="loadout.refresh_rentals"){
        const auto screen=runtime.class_name(runtime.object_class(presenter));
        if(screen!="ProduceSupportCardSelectScreenPresenter"&&screen!="ProduceMemorySelectScreenPresenter")throw std::runtime_error("rental loading requires the current selection screen");
        auto model=runtime.read_object_field(presenter,"_model");
        auto info=runtime.read_object_field(model,"_selectInfo");
        auto edit=runtime.getter(info,"get_SupportCardDeckEditInfo");
        auto ct=runtime.getter(presenter,"GetActiveCt");
        bool include_current=true;
        runtime.invoke(runtime.method(runtime.object_class(edit),"UpdateRentalCardsAsync",2,0x0600E5EE),edit,{&include_current,runtime.unbox_pointer(ct)});
    }else if(action=="loadout.memory_continue_without_rental"){
        if(before.at("screen_type")!="ProduceMemorySelectRentalEnableConfirmSheetPresenter")throw std::runtime_error("wrong memory-rental confirmation scene");
        auto common=runtime.getter(view,"get_CommonView");auto button=runtime.getter(common,"get_ExecuteButton");
        if(!clickable(runtime,button))throw std::runtime_error("memory continue button is no longer actionable");
        runtime.invoke(runtime.method(runtime.object_class(button),"OnClickedHandler",0),button);
    }else if(action=="ui.navigation"){
        const auto screen=before.at("screen_type").get<std::string>();
        const auto id=target.at("button_id").get<std::string>();
        const ButtonBinding* binding{};
        for(const auto& candidate:navigation_buttons)if(screen==candidate.screen&&id==candidate.id)binding=&candidate;
        if(!binding)throw std::runtime_error("navigation binding unavailable");
        auto button=navigation_button(runtime,presenter,*binding);
        if(id=="login.continue"){
            const auto touch=login_touch_state(runtime,presenter,button,screen);
            if(native_login_touch_target(touch)!=target)
                throw std::runtime_error("login pending touch waiter changed");
        }
        if(!clickable(runtime,button))throw std::runtime_error("native button not actionable");
        runtime.invoke(runtime.method(runtime.object_class(button),"OnClickedHandler",0),button);
    }else if(action=="ui.sheet_confirm"||action=="ui.sheet_cancel"){
        const auto source=target.at("button_source").get<std::string>();
        void* group{};
        if(source=="expiry-notice"&&before.at("screen_type")=="StartExchangeItemExpireSheetPresenter")group=runtime.getter(view,"get_ButtonGroup");
        else if(source=="common")group=runtime.getter(view,"get_CommonView");
        else throw std::runtime_error("unsupported sheet button source");
        auto button=runtime.getter(group,action=="ui.sheet_confirm"?"get_ExecuteButton":"get_CancelButton");
        if(!clickable(runtime,button))throw std::runtime_error("native sheet button no longer actionable");
        runtime.invoke(runtime.method(runtime.object_class(button),"OnClickedHandler",0),button);
    }else throw std::runtime_error("unsupported native outer action");
}
}
