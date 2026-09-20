#include "customize_confirmation_adapter.hpp"
#include "pointer_identity.hpp"
#include "screen_context.hpp"

namespace gkms::bridge {
namespace {
bool flag(Runtime& r,void* object,const char* name){return r.unbox<bool>(r.getter(object,name));}
int number(Runtime& r,void* object,const char* name){return r.unbox<int>(r.getter(object,name));}
std::string text(Runtime& r,void* object,const char* name){return r.string(r.getter(object,name));}
bool active(Runtime& r,void* object){return object&&flag(r,r.getter(object,"get_gameObject"),"get_activeInHierarchy");}
void* common_view(Runtime& r,void* presenter){
    auto view=r.read_object_field(presenter,"_view");
    if(!view||r.class_name(r.object_class(view))!="ProduceCustomizeConfirmSheetView")
        throw std::runtime_error("customize entry confirmation view changed");
    return r.invoke(r.method(r.object_class(view),"get_CommonView",0,0x060161EB),view);
}
void* button_for(Runtime& r,void* common,bool confirm){
    return r.invoke(r.method(r.object_class(common),confirm?"get_ExecuteButton":"get_CancelButton",0,confirm?0x0601614F:0x0601614E),common);
}
json observed_button(Runtime& r,void* button){
    return {{"active",active(r,button)},{"enabled",button&&flag(r,button,"get_IsEnabled")},
        {"disabled",!button||flag(r,button,"get_IsDisabled")},
        {"has_callback",button&&r.read_object_field(button,"onClickedCallback")!=nullptr}};
}
}
bool append_customize_confirmation_actions(Runtime& r,void* presenter,const std::string& screen,json& snapshot){
    if(screen!="ProduceCustomizeConfirmSheetPresenter")return false;
    auto parent=active_screen(r);
    const auto parent_type=parent?r.class_name(r.object_class(parent)):std::string();
    if(parent_type!="ScheduleScreenPresenter")return false;
    auto actual_parent=r.invoke(r.method(r.object_class(presenter),"Campus.Common.IScreenLayer.GetParent",0,0x06015B4E),presenter);
    auto expected_parent=r.getter(parent,"get_transform");
    auto parent_view=r.read_object_field(parent,"_view");
    auto step_data=r.read_object_field(parent_view,"_stepDataList");
    json offered=json::array();
    if(step_data)for(auto row:r.enumerate(step_data,64))offered.push_back(number(r,row,"get_StepType"));
    auto user=r.klass("Assembly-CSharp","Campus.Common.User","UserDataManager");
    auto progress=r.invoke(r.method(user,"get_UserProduceProgress",0),nullptr);
    auto model=r.getter(presenter,"get_Model");
    const bool bound=progress&&model&&customize_entry_parent_matches(screen,parent_type,
        reinterpret_cast<std::uintptr_t>(actual_parent),reinterpret_cast<std::uintptr_t>(expected_parent),
        flag(r,progress,"get_IsInProgress"),number(r,progress,"get_Status"),offered);
    auto common=common_view(r,presenter);auto fade=r.read_object_field(common,"_rootFade");
    auto sheet_view=r.read_object_field(presenter,"_view");
    auto description=r.read_object_field(sheet_view,"_descripiton");
    const auto description_text=description?r.string(r.invoke(r.method(r.object_class(description),"get_text",0,0x06001056),description)):std::string();
    auto produce=progress?r.getter(progress,"GetProduce"):nullptr;
    auto setting=produce?r.getter(produce,"GetProduceSetting"):nullptr;
    const int points=progress?number(r,progress,"get_ProducePoint"):-1;
    const int threshold=setting?number(r,setting,"get_StepCustomizeStartAlertProducePointThreshold"):-1;
    auto refresh=r.getter(parent_view,"get_RefreshButton");
    auto refresh_observed=observed_button(r,refresh);
    refresh_observed["has_disabled_callback"]=refresh&&r.read_object_field(refresh,"onClickedCallbackForDisableState")!=nullptr;
    refresh_observed["stamina_is_full"]=progress?json(number(r,progress,"get_Stamina")==number(r,progress,"get_MaxStamina")):json(nullptr);
    refresh_observed["is_limit_refresh_count"]=progress?json(flag(r,progress,"IsLimitRefreshCount")):json(nullptr);
    refresh_observed["remaining_refresh_count"]=progress?json(number(r,progress,"GetRemainRefreshCount")):json(nullptr);
    refresh_observed["source"]="actual RefreshButton flags and current Progress; no disabled callback is treated as a rest action";
    const json canvas={{"active",active(r,fade)},{"alpha",fade?r.unbox<float>(r.getter(fade,"get_alpha")):0.f},
        {"interactable",fade&&flag(r,fade,"get_interactable")},{"blocks_raycasts",fade&&flag(r,fade,"get_blocksRaycasts")}};
    json state={{"family","customize_entry_confirmation"},{"stage","confirm_customize_entry"},{"parent_bound",bound},
        {"parent_screen_type",parent_type},{"parent_instance_id",pointer_identity(parent)},
        {"parent_transform_id",pointer_identity(expected_parent)},{"actual_parent_transform_id",pointer_identity(actual_parent)},
        {"sheet_instance_id",pointer_identity(presenter)},{"model_instance_id",pointer_identity(model)},
        {"requested_step_type",28},{"offered_step_types",offered},{"offered_fingerprint",sha256(offered.dump())},
        {"is_skip_next",model?json(flag(r,model,"get_IsSkipNext")):json(nullptr)},
        {"parent_deciding",r.field<bool>(parent,"_isStepDeciding")},{"parent_decided",r.field<bool>(parent,"_isStepDecided")},
        {"selected_view_index",r.field<int>(parent_view,"_selectedStepIndex")},
        {"produce_id",progress?json(text(r,progress,"get_ProduceId")):json(nullptr)},
        {"week",progress?json(number(r,progress,"get_StepNumber")):json(nullptr)},
        {"progress_status",progress?json(number(r,progress,"get_Status")):json(nullptr)},
        {"description_text",description_text},{"description_source","native ProduceCustomizeConfirmSheetView._descripiton CampusText.get_text"},
        {"produce_points",points>=0?json(points):json(nullptr)},
        {"customize_alert_threshold",threshold>=0?json(threshold):json(nullptr)},
        {"notice_reason",customize_entry_notice_reason(!description_text.empty(),points,threshold)},
        {"notice_reason_source","native wallet <= native ProduceSetting.StepCustomizeStartAlertProducePointThreshold; APK typed-sheet initializer uses description_cost"},
        {"refresh_observation",refresh_observed},
        {"canvas",canvas},{"buttons",json::object()},
        {"source","Schedule.OpenConfirmSheetForCustomizeIfNeededAsync; actual current parent and offered step"},
        {"semantics","confirm returns true to the original Customize(28) request; it does not prove affordable customization or a skipped week; downstream server/view outcome is not inferred"},
        {"checkbox_policy","read current IsSkipNext; never change checkbox"}};
    snapshot["surface"]="customize_entry_confirmation";snapshot["legal_actions"]=json::array();
    for(bool confirm:{true,false}){
        auto button=button_for(r,common,confirm);const auto observed=observed_button(r,button);
        state["buttons"][confirm?"confirm":"cancel"]=observed;
        if(!bound||!customize_entry_button_ready(active(r,presenter),flag(r,presenter,"get_IsClosing"),
            flag(r,presenter,"get_IsDisableInteraction"),canvas,observed))continue;
        json target=json::object();
        for(const auto* key:{"parent_screen_type","parent_instance_id","parent_transform_id","sheet_instance_id","model_instance_id",
            "produce_id","week","progress_status","requested_step_type","offered_fingerprint","is_skip_next"})target[key]=state.at(key);
        target["button_instance_id"]=pointer_identity(button);target["callback_instance_id"]=pointer_identity(r.read_object_field(button,"onClickedCallback"));
        const char* id=confirm?"customize_entry.confirm":"customize_entry.cancel";target["action_id"]=id;
        snapshot["legal_actions"].push_back({{"action_id",id},{"target",target}});
    }
    snapshot["ui_state"]=state;snapshot["actions_complete"]=bound&&snapshot.at("blockers").empty();
    return true;
}
bool submit_customize_confirmation_action(Runtime& r,void* presenter,const json& target,const json& before){
    const auto id=target.value("action_id",std::string());
    if(id!="customize_entry.confirm"&&id!="customize_entry.cancel")return false;
    json fresh=before;
    if(!append_customize_confirmation_actions(r,presenter,r.class_name(r.object_class(presenter)),fresh))
        throw std::runtime_error("customize entry confirmation parent changed");
    bool found=false;for(const auto& action:fresh.at("legal_actions"))if(action.at("target")==target)found=true;
    if(!found)throw std::runtime_error("customize entry confirmation context or callback changed");
    auto button=button_for(r,common_view(r,presenter),id=="customize_entry.confirm");
    r.invoke(r.method(r.object_class(button),"OnClickedHandler",0),button);return true;
}
}
