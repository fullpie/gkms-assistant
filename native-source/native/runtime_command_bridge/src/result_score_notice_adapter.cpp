#include "result_score_notice_adapter.hpp"
#include "pointer_identity.hpp"
#include "screen_context.hpp"
#include <array>

namespace gkms::bridge {
namespace {
bool flag(Runtime& r,void* object,const char* name){return r.unbox<bool>(r.getter(object,name));}
bool active(Runtime& r,void* object){return object&&flag(r,r.getter(object,"get_gameObject"),"get_activeInHierarchy");}
void* common_view(Runtime& r,void* presenter,const ResultNoticeBinding& binding){
    auto view=r.read_object_field(presenter,"_view");
    if(!view||r.class_name(r.object_class(view))!=binding.view)
        throw std::runtime_error("result notice view contract changed");
    return r.invoke(r.method(r.object_class(view),"get_CommonView",0,binding.sheet?0x060161EB:0x0601600F),view);
}
void* button_for(Runtime& r,void* common,const std::string& source,bool sheet){
    if(sheet){
        if(source!="cancel")throw std::runtime_error("EasyMode notice only permits normal cancel without changing the flag");
        return r.invoke(r.method(r.object_class(common),"get_CancelButton",0,0x0601614E),common);
    }
    const auto* name=source=="execute"?"get_ExecuteButton":source=="cancel"?"get_CancelButton":"get_CircleCloseButton";
    const auto token=source=="execute"?0x06015FE6U:source=="cancel"?0x06015FE5U:0x06015FE7U;
    return r.invoke(r.method(r.object_class(common),name,0,token),common);
}
json observed_button(Runtime& r,void* button){
    return {{"active",active(r,button)},{"enabled",button&&flag(r,button,"get_IsEnabled")},
        {"disabled",!button||flag(r,button,"get_IsDisabled")},
        {"has_callback",button&&r.read_object_field(button,"onClickedCallback")!=nullptr}};
}
}
bool append_result_score_notice_actions(Runtime& r,void* presenter,const std::string& screen,json& snapshot){
    const auto* binding=result_notice_binding(screen);if(!binding)return false;
    auto parent=active_screen(r);
    const auto parent_type=parent?r.class_name(r.object_class(parent)):std::string();
    if(parent_type!="ProduceResultLastNiaScreenPresenter")return false;
    auto common=common_view(r,presenter,*binding);auto fade=r.read_object_field(common,"_rootFade");
    auto root_transform=r.getter(parent,"get_transform");
    auto actual_parent=binding->exact_parent?r.invoke(r.method(r.object_class(presenter),"Campus.Common.IScreenLayer.GetParent",0,0x06015B4E),presenter):nullptr;
    const bool parent_bound=result_notice_parent_matches(*binding,reinterpret_cast<std::uintptr_t>(actual_parent),reinterpret_cast<std::uintptr_t>(root_transform));
    const json canvas={{"active",active(r,fade)},{"alpha",fade?r.unbox<float>(r.getter(fade,"get_alpha")):0.f},
        {"interactable",fade&&flag(r,fade,"get_interactable")},{"blocks_raycasts",fade&&flag(r,fade,"get_blocksRaycasts")}};
    json state={{"family","result_score_notice"},{"stage",binding->stage},
        {"parent_screen_type",parent_type},{"parent_instance_id",pointer_identity(parent)},
        {"owner_instance_id",pointer_identity(presenter)},{"canvas",canvas},{"buttons",json::object()},
        {"parent_bound",parent_bound},{"parent_transform_id",pointer_identity(root_transform)},
        {"actual_parent_transform_id",actual_parent?json(pointer_identity(actual_parent)):json(nullptr)},
        {"source","current NIA result; normal close or no-setting-change cancel callbacks"}};
    if(binding->sheet){state["setting_policy"]="preserve_existing_easy_mode_flag";state["native_result"]="SheetModelBase.IsExecute=false";}
    snapshot["surface"]="produce_result";snapshot["legal_actions"]=json::array();
    const bool ready=parent_bound&&active(r,presenter)&&result_score_notice_ready(screen,parent_type,flag(r,presenter,"get_IsClosing"),canvas)&&
        (!binding->sheet||!flag(r,presenter,"get_IsDisableInteraction"));
    for(const auto* source:{"execute","cancel","circle-close"}){
        if(binding->sheet&&std::string(source)!="cancel")continue;
        auto button=button_for(r,common,source,binding->sheet);const auto observed=observed_button(r,button);
        state["buttons"][source]=observed;
        if(!ready||!snapshot["legal_actions"].empty())continue;
        auto callback=button?r.read_object_field(button,"onClickedCallback"):nullptr;
        auto action=result_score_notice_action(pointer_identity(presenter),pointer_identity(parent),
            pointer_identity(button),callback?pointer_identity(callback):std::string(),source,observed,screen);
        if(!action.is_null()&&binding->exact_parent)action["target"]["parent_transform_id"]=pointer_identity(actual_parent);
        if(!action.is_null())snapshot["legal_actions"].push_back(action);
    }
    snapshot["ui_state"]=state;snapshot["actions_complete"]=parent_bound&&snapshot.at("blockers").empty();
    return true;
}
bool submit_result_score_notice_action(Runtime& r,void* presenter,const json& target,const json& before){
    const auto id=target.value("action_id",std::string());bool recognized=false;
    for(const auto& binding:result_notice_bindings)recognized=recognized||id==binding.action;
    if(!recognized)return false;
    json fresh=before;
    if(!append_result_score_notice_actions(r,presenter,r.class_name(r.object_class(presenter)),fresh))
        throw std::runtime_error("new record notice no longer belongs to current NIA result");
    bool found=false;for(const auto& action:fresh.at("legal_actions"))if(action.at("target")==target)found=true;
    if(!found)throw std::runtime_error("new record notice parent/control changed");
    const auto* binding=result_notice_binding(r.class_name(r.object_class(presenter)));
    auto button=button_for(r,common_view(r,presenter,*binding),target.at("button_source").get<std::string>(),binding->sheet);
    r.invoke(r.method(r.object_class(button),"OnClickedHandler",0),button);
    return true;
}
}
