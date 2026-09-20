#include "produce_effect_view_adapter.hpp"
#include "screen_context.hpp"
#include "pointer_identity.hpp"
#include "effect_confirmation.hpp"

namespace gkms::bridge {
namespace {
bool flag(Runtime& r,void* object,const char* getter){return r.unbox<bool>(r.getter(object,getter));}
bool active(Runtime& r,void* object){return object&&flag(r,r.getter(object,"get_gameObject"),"get_activeInHierarchy");}
bool screen_view(Runtime& r,void* object){
    for(auto type=r.object_class(object);type;type=r.parent(type))
        if(r.class_name(type)=="CampusScreenViewBase")return true;
    return false;
}
void* current_difference_view(Runtime& r,void* presenter){
    // A real WindowLayer masks effects underneath it. Card difference views
    // themselves are instantiated under the owning screen's ContentRoot.
    if(active_layer(r))return nullptr;
    auto view=r.read_object_field(presenter,"_view");
    if(!view||!screen_view(r,view))return nullptr;
    auto root=r.getter(view,"get_ContentRoot");
    if(!root)return nullptr;
    auto component=r.klass("UnityEngine.CoreModule","UnityEngine","Component");
    auto requested=r.reflection_type(r.klass("Assembly-CSharp","Campus.InGame.Produce","ProduceCardDifferenceView"));
    bool include_inactive=false;
    auto list=r.invoke(r.method(component,"GetComponentsInChildren",2,0x060013C4),root,{requested,&include_inactive});
    void* current{};
    for(auto candidate:r.enumerate(list,32)){
        if(!active(r,candidate))continue;
        auto message=r.read_object_field(candidate,"_messageCanvasGroup");
        auto background=r.read_object_field(candidate,"_bg");
        const bool shown=(message&&r.unbox<float>(r.getter(message,"get_alpha"))>.001f)||
            (background&&r.unbox<float>(r.getter(background,"get_alpha"))>.001f);
        if(!shown)continue;
        if(current)throw std::runtime_error("multiple card difference views in current screen content root");
        current=candidate;
    }
    return current;
}
json card(Runtime& r,void* data){
    if(!data)return nullptr;
    return {{"card_id",r.string(r.getter(data,"get_Id"))},{"upgrade",r.unbox<int>(r.getter(data,"get_UpgradeCount"))}};
}
json changes(Runtime& r,void* difference){
    auto values=r.read_object_field(difference,"_currentAnimationCardData");
    if(!values)return nullptr;
    json result=json::array();
    for(auto pair:r.enumerate(values,128)){
        json after=json::array();
        auto after_values=r.read_object_field(pair,"Item2");
        if(after_values)for(auto value:r.enumerate(after_values,128))after.push_back(card(r,value));
        result.push_back({{"before",card(r,r.read_object_field(pair,"Item1"))},{"after",after}});
    }
    return result;
}
json button_state(Runtime& r,void* button){
    auto callback=button?r.read_object_field(button,"onClickedCallback"):nullptr;
    return {{"active",active(r,button)},{"enabled",button&&flag(r,button,"get_IsEnabled")},
        {"disabled",!button||flag(r,button,"get_IsDisabled")},{"has_callback",callback!=nullptr},
        {"button_instance_id",pointer_identity(button)},{"wait_callback_instance_id",pointer_identity(callback)}};
}
}
bool append_produce_effect_view_actions(Runtime& r,void* presenter,const std::string&,json& snapshot){
    auto difference=current_difference_view(r,presenter);
    if(!difference)return false;
    auto button=r.read_object_field(difference,"_screenButton");
    const auto observed=button_state(r,button);
    const auto change_list=changes(r,difference);
    const int effect=r.field<int>(difference,"_effectType");
    auto cancel=r.read_object_field(difference,"_cancelButton");
    const auto cancel_state=button_state(r,cancel);
    auto cancel_canvas=r.read_object_field(difference,"_cancelButtonCanvasGroup");
    const bool cancel_visible=active(r,cancel_canvas)&&r.unbox<float>(r.getter(cancel_canvas,"get_alpha"))>.001f;
    json state={{"view_type","ProduceCardDifferenceView"},{"view_instance_id",pointer_identity(difference)},
        {"effect_type",effect},{"changes",change_list},{"confirmation_button",observed},
        {"is_cancelled",r.field<bool>(difference,"_isCancelled")},{"cancel_button",cancel_state},
        {"cancel_visible",cancel_visible},{"can_cancel",cancel_visible&&native_button_actionable(cancel_state)},
        {"can_cancel_source","actual cancel-control visibility and readiness; ShowEffectAsync canCancel argument is not retained as a field"}};
    snapshot["surface"]="card_effect_result";
    snapshot["ui_state"]=state;
    // Preserve an already verified global effect.advance for the canCancel=false
    // branch. The own screen button belongs to canCancel=true, and is unrelated
    // to the separate revert/cancel control.
    if(native_button_actionable(observed)){
        json target={{"action_id","effect.confirm_card_change"},{"view_type","ProduceCardDifferenceView"},
            {"view_instance_id",state.at("view_instance_id")},{"effect_type",effect},
            {"changes_digest",sha256(change_list.dump())},{"button_instance_id",observed.at("button_instance_id")},
            {"wait_callback_instance_id",observed.at("wait_callback_instance_id")}};
        snapshot["legal_actions"]=json::array({{{"action_id","effect.confirm_card_change"},{"target",target}}});
    }else{
        auto& actions=snapshot["legal_actions"];
        for(auto it=actions.begin();it!=actions.end();)if(it->at("action_id")!="effect.advance")it=actions.erase(it);else ++it;
    }
    snapshot["actions_complete"]=snapshot.at("blockers").empty();
    return true;
}
bool submit_produce_effect_view_action(Runtime& r,void* presenter,const json& target,const json&){
    if(target.at("action_id")!="effect.confirm_card_change")return false;
    auto difference=current_difference_view(r,presenter);
    if(!difference||pointer_identity(difference)!=target.at("view_instance_id").get<std::string>())
        throw std::runtime_error("current card difference view changed");
    auto button=r.read_object_field(difference,"_screenButton");
    const auto observed=button_state(r,button);
    if(!native_button_actionable(observed)||observed.at("button_instance_id")!=target.at("button_instance_id")||
        observed.at("wait_callback_instance_id")!=target.at("wait_callback_instance_id")||
        r.field<int>(difference,"_effectType")!=target.at("effect_type").get<int>()||
        sha256(changes(r,difference).dump())!=target.at("changes_digest").get<std::string>())
        throw std::runtime_error("card difference acknowledgement identity changed");
    r.invoke(r.method(r.object_class(button),"OnClickedHandler",0),button);
    return true;
}
}
