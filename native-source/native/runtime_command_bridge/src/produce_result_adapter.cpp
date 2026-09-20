#include "produce_result_adapter.hpp"
#include "pointer_identity.hpp"
#include "screen_context.hpp"
#include <array>

namespace gkms::bridge {
namespace {
bool flag(Runtime& r,void* object,const char* getter){return r.unbox<bool>(r.getter(object,getter));}
int number(Runtime& r,void* object,const char* getter){return r.unbox<int>(r.getter(object,getter));}
std::string text(Runtime& r,void* object,const char* getter){return r.string(r.getter(object,getter));}
bool active(Runtime& r,void* object){return object&&flag(r,r.getter(object,"get_gameObject"),"get_activeInHierarchy");}
bool clickable(Runtime& r,void* button){return active(r,button)&&flag(r,button,"get_IsEnabled")&&!flag(r,button,"get_IsDisabled")&&r.read_object_field(button,"onClickedCallback");}
bool canvas_shown(Runtime& r,void* canvas){return active(r,canvas)&&r.unbox<float>(r.getter(canvas,"get_alpha"))>.001f;}
json canvas_state(Runtime& r,void* canvas){
    return {{"active",active(r,canvas)},{"alpha",canvas?r.unbox<float>(r.getter(canvas,"get_alpha")):0.f},
        {"interactable",canvas&&flag(r,canvas,"get_interactable")},
        {"blocks_raycasts",canvas&&flag(r,canvas,"get_blocksRaycasts")}};
}
bool canvas_accepts_pointer(const json& state){
    return state.at("active")==true&&state.at("alpha").get<float>()>.001f&&
        state.at("interactable")==true&&state.at("blocks_raycasts")==true;
}
void* view(Runtime& r,void* presenter){return r.read_object_field(presenter,"_view");}
json action(const char* id,json target){target["action_id"]=id;return {{"action_id",id},{"target",target}};}
void click(Runtime& r,void* button){
    if(!clickable(r,button))throw std::runtime_error("result button is no longer actionable");
    r.invoke(r.method(r.object_class(button),"OnClickedHandler",0),button);
}
void* photo_cell(Runtime& r,void* list,int index,void* expected){
    auto cell=r.invoke(r.method(r.object_class(list),"GetCellFromIndex",1),list,{&index});
    if(!active(r,cell))return nullptr;
    if(r.getter(cell,"GetItemModel")!=expected)throw std::runtime_error("result photo cell was recycled");
    return cell;
}
void* photo_button(Runtime& r,void* cell){return r.getter(r.getter(cell,"get_View"),"get_Button");}
void* confirmation_button(Runtime& r,void* presenter,bool cancel=false){
    return r.getter(r.getter(view(r,presenter),"get_CommonView"),cancel?"get_CancelButton":"get_ExecuteButton");
}
json confirmation_interaction(Runtime& r,void* presenter){
    const auto screen=r.class_name(r.object_class(presenter));
    if(screen!="MemoryCreateConfirmSheetPresenter")throw std::runtime_error("memory sheet interaction contract changed");
    auto common=r.getter(view(r,presenter),"get_CommonView");
    if(r.class_name(r.object_class(common))!="SheetCommonView")throw std::runtime_error("memory sheet common-view contract changed");
    const bool owner_active=active(r,presenter),closing=flag(r,presenter,"get_IsClosing"),disabled=flag(r,presenter,"get_IsDisableInteraction");
    const auto canvas=canvas_state(r,r.read_object_field(common,"_rootFade"));
    return {{"active",owner_active},{"closing",closing},{"disable_interaction",disabled},{"root_canvas",canvas},
        {"input_ready",result_memory_confirmation_ready(screen,owner_active,closing,disabled,canvas_accepts_pointer(canvas))}};
}
json confirmation_context(Runtime& r,void* parent,void* presenter,void* model){
    auto selected=r.getter(model,"get_SelectPhotoModel");
    auto data=selected?r.getter(selected,"get_Data"):nullptr;
    return {{"parent_instance_id",pointer_identity(parent)},{"model_instance_id",pointer_identity(model)},
        {"sheet_instance_id",pointer_identity(presenter)},{"created_memory_id",text(r,model,"get_CreateMemoryId")},
        {"selected_photo_guid",data?json(text(r,data,"get_Guid")):json(nullptr)}};
}
struct Binding{const char* id;const char* getter;const char* stage;};
constexpr std::array<Binding,5> buttons={{{"result.confirm_selection","get_MemorySelectCompleteButton","photo_selection"},
    {"result.continue_memory","get_MemoryDetailCompleteButton","memory_detail"},
    {"result.continue_selection_memory","get_SelectMemoryDetailCompleteButton","selection_memory_detail"},
    {"result.continue_rewards","get_RewardCompleteButton","rewards"},
    {"result.continue_achievements","get_AchievementCompleteButton","achievements"}}};
}

bool append_produce_result_actions(Runtime& r,void* presenter,const std::string& screen,json& snapshot){
    if(screen=="MemoryCreateConfirmSheetPresenter"){
        auto parent=active_screen(r);
        if(r.class_name(r.object_class(parent))!="ProduceResultScreenPresenter")return false;
        auto model=r.read_object_field(parent,"_model");
        auto context=confirmation_context(r,parent,presenter,model);
        const auto assigned=context.at("created_memory_id").get<std::string>();
        const bool cancel=!assigned.empty();
        const auto id=result_memory_confirmation_action(assigned);
        snapshot["surface"]="produce_result";
        snapshot["legal_actions"]=json::array();
        snapshot["ui_state"]=context;
        snapshot["ui_state"]["stage"]=cancel?"memory_already_created":"confirm_memory_creation";
        // CreateMemoryAsync assigns this ID before awaiting ProduceEndAsync.
        // It forbids re-entry but is not itself a completion receipt.
        snapshot["ui_state"]["memory_id_is_completion_proof"]=false;
        const auto interaction=confirmation_interaction(r,presenter);
        snapshot["ui_state"]["sheet_interaction"]=interaction;
        snapshot["actions_complete"]=snapshot.at("blockers").empty();
        auto button=confirmation_button(r,presenter,cancel);
        if(interaction.at("input_ready")==true&&(cancel||!context.at("selected_photo_guid").is_null())&&clickable(r,button)){
            context["button_instance_id"]=pointer_identity(button);
            context["callback_instance_id"]=pointer_identity(r.read_object_field(button,"onClickedCallback"));
            snapshot["legal_actions"].push_back(action(id,context));
        }
        return true;
    }
    if(screen!="ProduceResultScreenPresenter")return false;
    auto model=r.read_object_field(presenter,"_model");
    snapshot["surface"]="produce_result";snapshot["legal_actions"]=json::array();
    if(!model){snapshot["ui_state"]={{"data_ready",false}};snapshot["actions_complete"]=false;return true;}
    auto current_view=view(r,presenter);
    auto selected=r.getter(model,"get_SelectPhotoModel");
    auto selected_data=selected?r.getter(selected,"get_Data"):nullptr;
    json state={{"data_ready",true},{"created_memory_id",text(r,model,"get_CreateMemoryId")},
        {"current_select_memory_number",number(r,model,"get_CurrentSelectMemoryNumber")},
        {"current_photo_guid",text(r,model,"get_CurrentShowPhotoGuid")},
        {"selected_photo_guid",selected_data?json(text(r,selected_data,"get_Guid")):json(nullptr)},
        {"is_selection_memory",flag(r,model,"get_IsSelectionMemory")},
        {"is_memory_sold",flag(r,model,"get_IsMemorySold")},
        {"has_photo_select",flag(r,model,"get_HasPhotoSelect")},
        {"is_show_memory_detail",flag(r,model,"get_IsShowMemoryDetail")},
        {"is_memory_photo_upload_success",flag(r,model,"get_IsMemoryPhotoUploadSuccess")},
        {"is_skip_memory_list",flag(r,model,"get_IsSkipMemoryList")},
        {"is_reroll_memory",flag(r,model,"get_IsRerollMemory")},
        {"stage_buttons",json::object()},
        {"photos",json::array()},{"stages",json::object()}};
    auto create=r.read_object_field(current_view,"_memoryCreate");
    auto reward=r.read_object_field(current_view,"_reward");
    auto& stages=state["stages"];
    state["photo_selection_canvas"]=canvas_state(r,r.read_object_field(current_view,"_memorySelectCanvasGroup"));
    state["content_canvas"]=canvas_state(r,r.read_object_field(current_view,"_contentCanvasGroup"));
    state["photo_selection_visible"]=state["photo_selection_canvas"].at("active")==true&&
        state["photo_selection_canvas"].at("alpha").get<float>()>.001f;
    const bool photo_pointer_ready=canvas_accepts_pointer(state["photo_selection_canvas"])&&canvas_accepts_pointer(state["content_canvas"]);
    auto selection_complete=r.getter(current_view,"get_MemorySelectCompleteButton");
    state["photo_selection_input_enabled"]=result_photo_selection_allowed(photo_pointer_ready,
        state.at("created_memory_id").get<std::string>(),selected_data!=nullptr,clickable(r,selection_complete));
    stages["photo_selection"]=state.at("photo_selection_input_enabled");
    stages["memory_creation"]=create&&canvas_shown(r,r.read_object_field(create,"_canvasGroup"));
    stages["memory_detail"]=active(r,r.read_object_field(current_view,"_memoryDetail"));
    stages["selection_memory_detail"]=active(r,r.read_object_field(current_view,"_selectionMemoryDetail"));
    const bool reward_shown=reward&&canvas_shown(r,r.read_object_field(reward,"_canvasGroup"));
    stages["rewards"]=reward_shown&&canvas_shown(r,r.read_object_field(reward,"_rewardListCanvasGroup"));
    stages["achievements"]=reward_shown&&canvas_shown(r,r.read_object_field(reward,"_achievementListCanvasGroup"));
    const json context={{"created_memory_id",state.at("created_memory_id")},
        {"selected_photo_guid",state.at("selected_photo_guid")},{"current_select_memory_number",state.at("current_select_memory_number")}};
    if(stages["photo_selection"]==true){
        auto list=r.read_object_field(presenter,"_memorySelectList");
        auto values=list?r.getter(list,"get_ItemModels"):nullptr;
        state["photo_data_ready"]=values!=nullptr;
        if(values){
            auto models=r.enumerate(values,4096);
            for(int index=0;index<static_cast<int>(models.size());++index){
                auto item=models.at(static_cast<std::size_t>(index));auto data=r.getter(item,"get_Data");
                const auto guid=text(r,data,"get_Guid");
                if(guid.empty())throw std::runtime_error("result photo identity unavailable");
                auto cell=photo_cell(r,list,index,item);const bool selected_item=flag(r,item,"get_IsSelect");
                state["photos"].push_back({{"index",index},{"photo_guid",guid},{"selected",selected_item},{"realized",cell!=nullptr}});
                if(selected_item)continue;
                json target={{"index",index},{"photo_guid",guid}};
                if(!cell)snapshot["legal_actions"].push_back(action("result.reveal_photo",target));
                else if(auto button=photo_button(r,cell);clickable(r,button)){
                    target["cell_instance_id"]=pointer_identity(cell);
                    snapshot["legal_actions"].push_back(action("result.select_photo",target));
                }
            }
        }
    }
    for(const auto& binding:buttons){
        auto button=r.getter(current_view,binding.getter);
        state["stage_buttons"][binding.id]={{"stage",binding.stage},{"active",active(r,button)},
            {"enabled",button&&flag(r,button,"get_IsEnabled")},{"disabled",!button||flag(r,button,"get_IsDisabled")},
            {"has_callback",button&&r.read_object_field(button,"onClickedCallback")!=nullptr},
            {"button_instance_id",pointer_identity(button)},
            {"callback_instance_id",pointer_identity(button?r.read_object_field(button,"onClickedCallback"):nullptr)}};
        if(stages.at(binding.stage)!=true)continue;
        if(std::string(binding.id)=="result.confirm_selection"&&!selected_data)continue;
        if(!clickable(r,button))continue;
        auto target=context;target["button_instance_id"]=pointer_identity(button);
        snapshot["legal_actions"].push_back(action(binding.id,target));
    }
    if(stages["memory_creation"]==true){
        auto button=r.read_object_field(create,"_startButton");
        if(clickable(r,button)){
            auto target=context;target["button_instance_id"]=pointer_identity(button);
            snapshot["legal_actions"].push_back(action("result.start_memory_animation",target));
        }
    }
    snapshot["ui_state"]=state;
    snapshot["actions_complete"]=snapshot.at("blockers").empty()&&state.value("photo_data_ready",true);
    return true;
}

bool submit_produce_result_action(Runtime& r,void* presenter,const json& target,const json& before){
    const auto id=target.at("action_id").get<std::string>();
    if(id.rfind("result.",0)!=0)return false;
    const auto screen=before.at("screen_type").get<std::string>();
    if(id=="result.confirm_create_memory"||id=="result.cancel_create_memory"){
        if(screen!="MemoryCreateConfirmSheetPresenter")throw std::runtime_error("memory confirmation scene changed");
        if(confirmation_interaction(r,presenter).at("input_ready")!=true)
            throw std::runtime_error("memory confirmation sheet is closing or not interactive");
        auto parent=active_screen(r);
        if(r.class_name(r.object_class(parent))!="ProduceResultScreenPresenter")throw std::runtime_error("memory confirmation parent changed");
        auto model=r.read_object_field(parent,"_model");
        const auto context=confirmation_context(r,parent,presenter,model);
        for(auto it=context.begin();it!=context.end();++it)
            if(!target.contains(it.key())||target.at(it.key())!=it.value())throw std::runtime_error("memory confirmation context changed");
        if(id!=result_memory_confirmation_action(context.at("created_memory_id").get<std::string>()))
            throw std::runtime_error("memory creation is already in progress; only normal cancellation is allowed");
        auto button=confirmation_button(r,presenter,id=="result.cancel_create_memory");
        if(pointer_identity(button)!=target.at("button_instance_id").get<std::string>()||
            pointer_identity(r.read_object_field(button,"onClickedCallback"))!=target.at("callback_instance_id").get<std::string>())
            throw std::runtime_error("memory confirmation callback changed");
        click(r,button);return true;
    }
    if(screen!="ProduceResultScreenPresenter")throw std::runtime_error("result scene changed");
    if(id=="result.select_photo"||id=="result.reveal_photo"){
        auto model=r.read_object_field(presenter,"_model");
        auto current=view(r,presenter);
        auto selected=r.getter(model,"get_SelectPhotoModel");
        const bool pointer_ready=canvas_accepts_pointer(canvas_state(r,r.read_object_field(current,"_memorySelectCanvasGroup")))&&
            canvas_accepts_pointer(canvas_state(r,r.read_object_field(current,"_contentCanvasGroup")));
        if(!result_photo_selection_allowed(pointer_ready,text(r,model,"get_CreateMemoryId"),selected!=nullptr,
            clickable(r,r.getter(current,"get_MemorySelectCompleteButton"))))
            throw std::runtime_error("result photo selection is no longer interactive");
        auto list=r.read_object_field(presenter,"_memorySelectList");
        auto models=r.enumerate(r.getter(list,"get_ItemModels"),4096);
        int index=target.at("index").get<int>();auto item=models.at(static_cast<std::size_t>(index));
        if(text(r,r.getter(item,"get_Data"),"get_Guid")!=target.at("photo_guid").get<std::string>())throw std::runtime_error("result photo index identity changed");
        if(id=="result.reveal_photo")r.invoke(r.method(r.object_class(list),"MoveScrollByHeadIndex",1),list,{&index});
        else{
            auto cell=photo_cell(r,list,index,item);
            if(!cell||pointer_identity(cell)!=target.at("cell_instance_id").get<std::string>())throw std::runtime_error("result photo cell identity changed");
            click(r,photo_button(r,cell));
        }
        return true;
    }
    void* button{};
    if(id=="result.start_memory_animation")button=r.read_object_field(r.read_object_field(view(r,presenter),"_memoryCreate"),"_startButton");
    else for(const auto& binding:buttons)if(id==binding.id)button=r.getter(view(r,presenter),binding.getter);
    if(!button||pointer_identity(button)!=target.at("button_instance_id").get<std::string>())throw std::runtime_error("result button identity changed");
    click(r,button);return true;
}
}
