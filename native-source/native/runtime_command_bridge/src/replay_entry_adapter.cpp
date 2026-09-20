#include "replay_entry_adapter.hpp"
#include "screen_context.hpp"
#include "outer_pointer_guard.hpp"
#include "pointer_identity.hpp"

namespace gkms::bridge {
namespace {
constexpr const char* picker_type="ProduceIdolSelectScreenPresenter";
constexpr const char* sheet_type="ProduceIdolGrowthInfoNiaSheetPresenter";
bool flag(Runtime& r,void* object,const char* name){return r.unbox<bool>(r.getter(object,name));}
std::string text(Runtime& r,void* object,const char* name){return r.string(r.getter(object,name));}
void* exact_get(Runtime& r,void* object,const char* name,std::uint32_t token){
    return r.invoke(r.method(r.object_class(object),name,0,token),object);
}
bool active(Runtime& r,void* object){return object&&flag(r,r.getter(object,"get_gameObject"),"get_activeInHierarchy");}
void expect(Runtime& r,void* object,const char* type){
    if(!object||r.class_name(r.object_class(object))!=type)
        throw std::runtime_error(std::string("replay entry object type changed: ")+type);
}
std::vector<void*> sequence(Runtime& r,void* object,std::size_t limit){
    if(!object)throw std::runtime_error("replay entry sequence is absent");
    for(auto type=r.object_class(object);type;type=r.parent(type)){
        const auto name=r.class_name(type);
        if(name=="Array"||name=="List`1"||name=="RepeatedField`1"||name=="ReadOnlyCollection`1")
            return r.enumerate(object,limit);
    }
    throw std::runtime_error("replay entry collection is not a verified sequence");
}
bool button_ready(Runtime& r,void* button){
    if(!active(r,button)||!flag(r,button,"get_IsEnabled")||flag(r,button,"get_IsDisabled"))return false;
    auto requested=r.reflection_type(r.klass("UnityEngine.UIModule","UnityEngine","CanvasGroup"));
    auto groups=r.invoke(r.method(r.object_class(button),"GetComponentsInParent",1,0x060013CF),button,{requested});
    float alpha=1.f;
    for(auto canvas:sequence(r,groups,64)){
        alpha*=r.unbox<float>(r.getter(canvas,"get_alpha"));
        if(!flag(r,canvas,"get_interactable")||!flag(r,canvas,"get_blocksRaycasts")||alpha<=.001f)return false;
        if(flag(r,canvas,"get_ignoreParentGroups"))break;
    }
    return true;
}
struct Projection {json state=json::object();json candidate=nullptr;void* button{};};
Projection project(Runtime& r,void* presenter,const std::string& screen){
    Projection result;
    const bool sheet=screen==sheet_type;
    if(screen!=picker_type&&!sheet)return result;
    expect(r,presenter,screen.c_str());
    auto picker=active_screen(r);
    expect(r,picker,picker_type);
    if((!sheet&&picker!=presenter)||(sheet&&active_layer(r)!=presenter))
        throw std::runtime_error("replay entry is not the actual foreground picker or sheet");
    auto model=r.read_object_field(picker,"_model");
    expect(r,model,"ProduceIdolSelectScreenModel");
    auto info=exact_get(r,model,"get_SelectInfo",0x0600EC47);
    expect(r,info,"ProduceSelectInfo");
    auto produce=exact_get(r,info,"get_Produce",0x0600E431);
    auto selected_card=exact_get(r,info,"get_UserIdolCard",0x0600E43F);
    // CurrentCard is IReadOnlyReactiveProperty<IUserCard>. Its wrapper is not
    // a card identity; read the actual Value and compare both observed IDs.
    auto reactive=exact_get(r,model,"get_CurrentCard",0x0600EC4A);
    auto current_card=reactive?r.getter(reactive,"get_Value"):nullptr;
    auto user=r.klass("Assembly-CSharp","Campus.Common.User","UserDataManager");
    auto progress=r.invoke(r.method(user,"get_UserProduceProgress",0),nullptr);
    const bool no_active=progress&&!flag(r,progress,"get_IsInProgress");
    auto& state=result.state;
    state={{"source","actual-native-picker-selection-and-normal-info-buttons"},
        {"native_no_active_produce",no_active},{"produce_id",produce?text(r,produce,"get_Id"):std::string()},
        {"idol_card_id",selected_card?text(r,selected_card,"get_CardId"):std::string()},
        {"current_card_id",current_card?text(r,current_card,"get_CardId"):std::string()},
        {"selection_identity_matches",selected_card&&current_card&&text(r,selected_card,"get_CardId")==text(r,current_card,"get_CardId")},
        {"is_high_score_rush",flag(r,info,"get_IsHighScoreRush")},{"is_research",flag(r,info,"get_IsResearch")},
        {"is_hif_final_round",flag(r,model,"get_IsHifFinalRound")},
        {"picker_transitioning",flag(r,model,"get_IsTransitioning")},{"picker_card_blocked",flag(r,model,"get_IsBlockSetCurrentCard")},
        {"presenter_active",active(r,presenter)&&active(r,picker)},
        {"scope_ready",false},{"ready",false}};
    state["scope_ready"]=replay_entry_scope(state);
    // Outside the requested capture scope, leave all normal navigation alone.
    if(state.at("scope_ready")!=true)return result;
    auto view=r.read_object_field(presenter,"_view");
    expect(r,view,sheet?"ProduceIdolGrowthInfoNiaSheetView":"ProduceIdolSelectScreenView");
    result.button=sheet?exact_get(r,view,"get_LatestHistoryButton",0x0600EC22):
        exact_get(r,view,"get_GrowthInfoButton",0x0600ECE7);
    auto callback=result.button?r.read_object_field(result.button,"onClickedCallback"):nullptr;
    auto entries=callback?sequence(r,r.getter(callback,"GetInvocationList"),16):std::vector<void*>{};
    void* owner{};int token{};std::uint32_t verified_token{};std::string callback_name;
    if(entries.size()==1){
        owner=r.getter(entries.front(),"get_Target");
        auto method=r.getter(entries.front(),"get_Method");
        token=method?r.unbox<int>(r.getter(method,"get_MetadataToken")):0;
        callback_name=method?text(r,method,"get_Name"):std::string();
        verified_token=read_profiled_callback_token(r,method,replay_entry_callback_source(screen));
    }
    const auto pointer=read_outer_pointer_guard(r,presenter,sheet);
    state.update({{"button_ready",active(r,view)&&result.button&&button_ready(r,result.button)},
        {"callback_count",entries.size()},{"callback_method_token",token},{"callback_owner_instance_id",pointer_identity(owner)},
        {"callback_method_name",callback_name},{"callback_owner_type",owner?r.class_name(r.object_class(owner)):std::string()},
        {"callback_ready",replay_entry_callback_matches(screen,token,verified_token,owner==picker)},
        {"sheet_input_ready",!sheet||(!flag(r,presenter,"get_IsClosing")&&!flag(r,presenter,"get_IsDisableInteraction"))},
        {"pointer_ready",pointer.at("input_ready")},{"pointer_guard",pointer}});
    const auto progress_json=json::parse(text(r,progress,"ToString"));
    json target={{"presenter_type",screen},{"presenter_instance_id",pointer_identity(presenter)},
        {"picker_instance_id",pointer_identity(picker)},{"picker_model_instance_id",pointer_identity(model)},
        {"select_info_instance_id",pointer_identity(info)},{"produce_instance_id",pointer_identity(produce)},
        {"selected_card_instance_id",pointer_identity(selected_card)},{"current_card_instance_id",pointer_identity(current_card)},
        {"view_instance_id",pointer_identity(view)},{"button_instance_id",pointer_identity(result.button)},
        {"callback_instance_id",pointer_identity(callback)},{"callback_owner_instance_id",pointer_identity(owner)},
        {"callback_method_token",token},{"progress_digest",sha256(progress_json.dump())},
        {"produce_id",state.at("produce_id")},{"idol_card_id",state.at("idol_card_id")},
        {"native_no_active_produce",no_active},{"is_high_score_rush",state.at("is_high_score_rush")},
        {"is_research",state.at("is_research")},{"is_hif_final_round",state.at("is_hif_final_round")},
        {"picker_transitioning",state.at("picker_transitioning")},{"picker_card_blocked",state.at("picker_card_blocked")}};
    result.candidate=replay_entry_action(screen,std::move(target),state);
    state["ready"]=!result.candidate.is_null();
    return result;
}
}
bool append_replay_entry_actions(Runtime& r,void* presenter,const std::string& screen,json& snapshot){
    if(screen!=picker_type&&screen!=sheet_type)return false;
    try{
        auto projection=project(r,presenter,screen);
        snapshot["ui_state"]["replay_entry"]=std::move(projection.state);
        if(screen==sheet_type){
            snapshot["surface"]="replay_entry";
            snapshot["actions_complete"]=snapshot.at("blockers").empty();
        }
        if(!projection.candidate.is_null())snapshot["legal_actions"].push_back(std::move(projection.candidate));
    }catch(const std::exception& error){
        mark_replay_entry_unavailable(snapshot,error.what());
    }
    return true;
}
bool submit_replay_entry_action(Runtime& r,void* presenter,const json& target,const json& before){
    const auto id=target.value("action_id",std::string());
    if(id!="replay.open_info"&&id!="replay.open_recommendations")return false;
    auto projection=project(r,presenter,before.at("screen_type").get<std::string>());
    if(projection.candidate.is_null()||projection.candidate.at("target")!=target)
        throw std::runtime_error("replay entry selection, owner, callback or button changed");
    // Only the currently installed normal UI callback may open the next page.
    // No utility/API call, filter change, synthetic callback or state setter.
    r.invoke(r.method(r.object_class(projection.button),"OnClickedHandler",0),projection.button);
    return true;
}
}
