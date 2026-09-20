#include "mode_navigation_adapter.hpp"
#include "pointer_identity.hpp"

namespace gkms::bridge {
namespace {
bool flag(Runtime& r,void* object,const char* getter){return r.unbox<bool>(r.getter(object,getter));}
bool active(Runtime& r,void* object){return object&&flag(r,r.getter(object,"get_gameObject"),"get_activeInHierarchy");}
void* getter(Runtime& r,void* object,const char* name,std::uint32_t token){
    return r.invoke(r.method(r.object_class(object),name,0,token),object);
}
void* button_for(Runtime& r,void* presenter,const ModeNavigationBinding& binding,const std::string& source="execute"){
    auto view=r.read_object_field(presenter,"_view");
    if(!view||r.class_name(r.object_class(view))!=binding.view)throw std::runtime_error("mode navigation view contract changed");
    if(binding.reward_overlay){
        auto common=getter(r,view,"get_CommonView",0x0601600F);
        if(binding.close_only){
            if(source!="close")throw std::runtime_error("startup advertisement only permits its normal circle close");
            return getter(r,common,"get_CircleCloseButton",0x06015FE7);
        }
        if(source=="execute")return getter(r,common,"get_ExecuteButton",0x06015FE6);
        if(source=="cancel")return getter(r,common,"get_CancelButton",0x06015FE5);
        if(source=="circle-close")return getter(r,common,"get_CircleCloseButton",0x06015FE7);
        throw std::runtime_error("reward overlay acknowledgement source unavailable");
    }
    return getter(r,view,binding.getter,binding.getter_token);
}
json observed_button(Runtime& r,void* button){
    return {{"active",active(r,button)},{"enabled",button&&flag(r,button,"get_IsEnabled")},
        {"disabled",!button||flag(r,button,"get_IsDisabled")},
        {"has_callback",button&&r.read_object_field(button,"onClickedCallback")!=nullptr}};
}
json reward_overlay_interaction(Runtime& r,void* presenter){
    const auto screen=r.class_name(r.object_class(presenter));
    const auto* binding=mode_navigation_binding(screen);
    if(!binding||!binding->reward_overlay)throw std::runtime_error("acknowledgement overlay interaction contract changed");
    auto current_view=r.read_object_field(presenter,"_view");
    auto common=getter(r,current_view,"get_CommonView",0x0601600F);
    if(r.class_name(r.object_class(common))!="OverlayCommonView")
        throw std::runtime_error("reward overlay common-view contract changed");
    auto canvas=r.read_object_field(common,"_rootFade");
    const bool canvas_active=active(r,canvas);
    const float alpha=canvas?r.unbox<float>(r.getter(canvas,"get_alpha")):0.f;
    const bool interactable=canvas&&flag(r,canvas,"get_interactable");
    const bool raycasts=canvas&&flag(r,canvas,"get_blocksRaycasts");
    const int input_layer=r.unbox<int>(getter(r,common,"GetCanvasLayer",0x06015FEB));
    int content_count=0,dialog_count=0;
    auto manager_class=r.klass("Assembly-CSharp","Campus.Common","CampusBlockingManager");
    // ExistInstance avoids creating a singleton while observing an idle UI.
    if(r.unbox<bool>(r.invoke(r.method(manager_class,"get_ExistInstance",0,0x06000093),nullptr))){
        auto manager=r.invoke(r.method(manager_class,"get_Instance",0,0x06000092),nullptr);
        auto counters=r.read_object_field(manager,"_blockingCounter");
        auto lookup=r.method(r.object_class(counters),"TryGetValue",2);
        int content_order=13,dialog_order=17;
        r.invoke(lookup,counters,{&content_order,&content_count});
        r.invoke(lookup,counters,{&dialog_order,&dialog_count});
    }
    const bool owner_active=active(r,presenter),closing=flag(r,presenter,"get_IsClosing");
    const bool ready=reward_overlay_input_ready(screen,owner_active,closing,
        canvas_active&&alpha>.001f&&interactable&&raycasts,input_layer,content_count,dialog_count);
    return {{"active",owner_active},{"closing",closing},{"input_layer",input_layer},
        {"content_blocking_count",content_count},{"dialog_blocking_count",dialog_count},
        {"root_canvas",{{"active",canvas_active},{"alpha",alpha},{"interactable",interactable},{"blocks_raycasts",raycasts}}},
        {"close_callback_instance_id",pointer_identity(r.read_object_field(presenter,"onClose"))},
        {"input_ready",ready}};
}
}

json native_mode_progress_fields(Runtime& r,void* progress){
    if(!progress)return json::object();
    // Explicit getters preserve observed zero values omitted by protobuf JSON.
    json state={{"star",r.unbox<int>(getter(r,progress,"get_Star",0x0601A040))},
        {"star_permil",r.unbox<int>(getter(r,progress,"get_StarPermil",0x0601A042))},
        {"user_selection_memory_id",r.string(getter(r,progress,"get_UserSelectionMemoryId",0x06019FDA))},
        {"result_selection_memory",nullptr}};
    auto memory=getter(r,progress,"get_ResultSelectionMemory",0x0601A016);
    if(memory){
        state["result_selection_memory"]={{"user_selection_memory_id",r.string(r.getter(memory,"get_UserSelectionMemoryId"))},
            {"produce_id",r.string(r.getter(memory,"get_ProduceId"))},
            {"idol_card_id",r.string(r.getter(memory,"get_IdolCardId"))},
            {"character_id",r.string(r.getter(memory,"get_CharacterId"))},
            {"star",r.unbox<int>(r.getter(memory,"get_Star"))},
            {"source","native UserProduceProgress.ResultSelectionMemory"}};
    }
    return state;
}

bool append_mode_navigation_actions(Runtime& r,void* presenter,const std::string& screen,json& snapshot){
    const auto* binding=mode_navigation_binding(screen);
    if(!binding)return false;
    std::string source=binding->close_only?"close":"execute";
    auto button=button_for(r,presenter,*binding,source);
    if(binding->reward_overlay&&!binding->close_only&&!native_button_actionable(observed_button(r,button))){
        for(const auto* alternative:{"cancel","circle-close"}){
            auto candidate=button_for(r,presenter,*binding,alternative);
            if(native_button_actionable(observed_button(r,candidate))){button=candidate;source=alternative;break;}
        }
    }
    auto callback=button?r.read_object_field(button,"onClickedCallback"):nullptr;
    auto observed=observed_button(r,button);
    const auto interaction=binding->reward_overlay?reward_overlay_interaction(r,presenter):json::object();
    const bool input_ready=(!binding->reward_overlay||interaction.at("input_ready")==true)&&
        (!binding->close_only||interaction.at("close_callback_instance_id")!="0x0");
    snapshot["surface"]="navigation";snapshot["legal_actions"]=json::array();
    snapshot["actions_complete"]=snapshot.at("blockers").empty();
    snapshot["ui_state"]={{"phase",binding->phase},{"navigation_button",observed},
        {"automatic_transition",!input_ready||!native_button_actionable(observed)}};
    if(binding->reward_overlay)snapshot["ui_state"]["overlay_interaction"]=interaction;
    auto action=mode_navigation_action(*binding,pointer_identity(presenter),pointer_identity(button),pointer_identity(callback),observed,input_ready);
    if(!action.is_null()){
        if(binding->reward_overlay)action["target"]["button_source"]=source;
        if(binding->close_only){
            // Startup's own MoveButton opens the advertisement destination.
            // Only OverlayCommonView's normal circle close is exposed. Bind
            // the current ScreenLayer close subscriber as the completion owner.
            action["target"]["screen_instance_id"]=snapshot.at("screen_instance_id");
            action["target"]["close_callback_instance_id"]=interaction.at("close_callback_instance_id");
        }
        snapshot["legal_actions"].push_back(action);
    }
    return true;
}

bool submit_mode_navigation_action(Runtime& r,void* presenter,const json& target,const json& before){
    if(!target.value("mode_navigation",false))return false;
    const auto screen=before.at("screen_type").get<std::string>();
    const auto* binding=mode_navigation_binding(screen);
    if(!binding)throw std::runtime_error("mode navigation screen is no longer supported");
    if(binding->reward_overlay){
        const auto interaction=reward_overlay_interaction(r,presenter);
        if(interaction.at("input_ready")!=true)
            throw std::runtime_error("reward overlay is closing, blocked or not interactive");
        if(binding->close_only&&(target.value("screen_instance_id",std::string())!=before.at("screen_instance_id").get<std::string>()||
            interaction.at("close_callback_instance_id")=="0x0"||
            target.value("close_callback_instance_id",std::string())!=interaction.at("close_callback_instance_id").get<std::string>()))
            throw std::runtime_error("startup overlay close owner changed");
    }
    auto button=button_for(r,presenter,*binding,target.value("button_source",std::string("execute")));
    auto callback=button?r.read_object_field(button,"onClickedCallback"):nullptr;
    if(!native_button_actionable(observed_button(r,button))||
       !mode_navigation_target_matches(target,*binding,pointer_identity(presenter),pointer_identity(button),pointer_identity(callback)))
        throw std::runtime_error("mode navigation callback identity or readiness changed");
    r.invoke(r.method(r.object_class(button),"OnClickedHandler",0),button);
    return true;
}
}
