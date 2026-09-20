#include "memory_auto_adapter.hpp"
#include "inventory_adapter.hpp"
#include "native_layer_context.hpp"
#include "pc_method_binding.hpp"
#include "pointer_identity.hpp"
#include "screen_context.hpp"
#include <array>
#include <limits>

namespace gkms::bridge {
namespace {
constexpr const char* memory_screen="ProduceMemorySelectScreenPresenter";
constexpr const char* confirm_screen="ProduceMemoryDeckAutoSetConfirmSheetPresenter";
void require(bool ok,const char* message){if(!ok)throw std::runtime_error(std::string("memory-auto:")+message);}
bool type(Runtime& r,void* object,const char* name,const char* ns="Campus.OutGame"){
    return object&&r.class_name(r.object_class(object))==name&&r.class_namespace(r.object_class(object))==ns;
}
bool flag(Runtime& r,void* object,const char* name){return r.unbox<bool>(r.getter(object,name));}
std::string text(Runtime& r,void* object,const char* name){auto v=r.string(r.getter(object,name));require(!v.empty(),"empty-scope-identity");return v;}
bool active(Runtime& r,void* value){return value&&flag(r,r.getter(value,"get_gameObject"),"get_activeInHierarchy");}
bool clickable(Runtime& r,void* button){return button&&active(r,button)&&flag(r,button,"get_IsEnabled")&&!flag(r,button,"get_IsDisabled")&&r.read_object_field(button,"onClickedCallback");}
bool callback_owner(Runtime& r,void* callback,void* owner,const char* method_name){
    if(!callback||!owner)return false;
    int matches=0;
    for(auto entry:r.enumerate(r.getter(callback,"GetInvocationList"),64)){
        if(r.getter(entry,"get_Target")!=owner)continue;
        auto method=r.getter(entry,"get_Method");
        if(method&&r.string(r.getter(method,"get_Name"))==method_name)++matches;
    }
    return matches==1;
}
struct Context {void* owner{};void* panel{};void* info{};void* edit{};void* deck{};void* button{};void* callback{};void* updated{};json binding;};
Context context(Runtime& r){
    Context c;c.owner=active_screen(r);require(type(r,c.owner,memory_screen),"not-memory-selection-screen");
    auto model=r.read_object_field(c.owner,"_model");require(type(r,model,"ProduceMemorySelectScreenModel"),"screen-model-type");
    c.info=r.read_object_field(model,"_selectInfo");require(type(r,c.info,"ProduceSelectInfo"),"selection-info-type");
    c.panel=r.read_object_field(c.owner,"_deckPanel");require(type(r,c.panel,"ProduceMemoryDeckPanelPresenter"),"deck-panel-type");
    auto pm=r.read_object_field(c.panel,"_model");require(type(r,pm,"ProduceMemoryDeckPanelModel"),"panel-model-type");
    require(r.getter(pm,"get_SelectInfo")==c.info,"panel-selection-owner");
    c.edit=r.getter(pm,"get_DeckEditInfo");require(type(r,c.edit,"ProduceMemoryDeckEditInfo"),"edit-info-type");
    require(r.getter(c.info,"get_MemoryDeckEditInfo")==c.edit&&r.getter(model,"get_DeckEditInfo")==c.edit,"edit-info-owner");
    c.deck=r.getter(c.edit,"get_CurrentDeck");require(c.deck!=nullptr,"current-deck-unavailable");
    auto view=r.read_object_field(c.panel,"_view");require(type(r,view,"ProduceMemoryDeckPanelView"),"panel-view-type");
    auto common=r.getter(view,"get_CommonView");require(type(r,common,"ProduceDeckPanelCommonView"),"panel-common-view-type");
    c.button=r.getter(common,"get_AutoSetButton");c.callback=c.button?r.read_object_field(c.button,"onClickedCallback"):nullptr;
    c.updated=r.read_object_field(c.panel,"onDeckUpdated");
    require(callback_owner(r,c.callback,c.panel,"<SetEvent>b__13_3")&&
        callback_owner(r,c.updated,c.owner,"<SetEvent>b__12_4"),"normal-panel-callback-owner");
    auto manager_type=r.klass("Assembly-CSharp.dll","Campus.Common.User","UserDataManager");
    auto manager=r.invoke(r.method(manager_type,"get_Instance",0,0x06016C2A),nullptr);
    auto user=r.getter(manager,"get_User");
    c.binding={{"account_scope","sha256:"+sha256(text(r,user,"get_PublicUserId"))},
        {"produce_id",text(r,r.getter(c.info,"get_Produce"),"get_Id")},
        {"idol_card_id",text(r,r.getter(c.info,"get_UserIdolCard"),"get_CardId")},
        {"owner_instance_id",pointer_identity(c.owner)},{"panel_instance_id",pointer_identity(c.panel)},
        {"select_info_instance_id",pointer_identity(c.info)},{"edit_instance_id",pointer_identity(c.edit)},
        {"deck_instance_id",pointer_identity(c.deck)},
        {"button_instance_id",pointer_identity(c.button)},{"callback_instance_id",pointer_identity(c.callback)},
        {"deck_updated_callback_instance_id",pointer_identity(c.updated)}};
    return c;
}
struct Operation {
    std::uint64_t serial{};bool started{},confirmed{},consumed{},override_started{};
    std::string phase="idle",task_status="not-started",failure;
    enum Root {Owner,Panel,Info,Edit,Deck,Callback,Updated,Task,Sheet,Count};
    std::array<GCHandle,Count> roots{};json binding=json::object(),auto_resources=json::array();
} operation;
void release(Runtime& r){for(auto& handle:operation.roots){r.free_handle(handle);handle=nullptr;}}
void keep(Runtime& r,Operation::Root slot,void* object){require(object!=nullptr,"null-operation-root");operation.roots[slot]=r.strong_handle(object);}
void* object(Runtime& r,Operation::Root slot){auto result=r.handle_target(operation.roots[slot]);require(result!=nullptr,"operation-root-lost");return result;}
void fail(const std::string& reason){operation.phase="failed";if(operation.failure.empty())operation.failure=reason;}
void reset_terminal(Runtime& r,const Context& c){
    if(!operation.started||!operation.consumed)return;
    bool same=true;
    for(const auto* key:{"account_scope","produce_id","idol_card_id","owner_instance_id","panel_instance_id","select_info_instance_id","edit_instance_id","deck_instance_id"})
        same=same&&operation.binding.at(key)==c.binding.at(key);
    if(same)return;
    const auto serial=operation.serial;release(r);operation=Operation{};operation.serial=serial;
}
void verify_context(Runtime& r,const Context& c){require(operation.binding==c.binding,"operation-scope-or-owner-changed");
    require(c.owner==object(r,Operation::Owner)&&c.panel==object(r,Operation::Panel)&&c.info==object(r,Operation::Info)&&
        c.edit==object(r,Operation::Edit)&&c.deck==object(r,Operation::Deck)&&c.callback==object(r,Operation::Callback)&&c.updated==object(r,Operation::Updated),"rooted-context-changed");}
void* task_class(Runtime& r){return r.klass("UniTask.dll","Cysharp.Threading.Tasks","UniTask");}
void poll(Runtime& r){
    if(!operation.started||operation.consumed||!operation.roots[Operation::Task])return;
    try{
        auto task=object(r,Operation::Task);require(r.object_class(task)==task_class(r),"boxed-task-type-changed");
        auto status=r.getter(task,"get_Status");
        require(r.object_class(status)==r.klass("UniTask.dll","Cysharp.Threading.Tasks","UniTaskStatus"),"task-status-type");
        auto enum_type=r.klass("mscorlib.dll","System","Enum");
        operation.task_status=r.string(r.invoke(r.method(enum_type,"ToString",0),status));
        require(operation.task_status=="Pending"||operation.task_status=="Succeeded"||operation.task_status=="Faulted"||operation.task_status=="Canceled","unknown-task-status");
        if(operation.task_status=="Pending")return;
        // GetResult may return its source to a pool. Latch before any awaiter
        // access and never read or consume this task again, including errors.
        operation.consumed=true;
        auto awaiter=r.getter(task,"GetAwaiter");
        require(r.object_class(awaiter)==r.nested_class(task_class(r),"Awaiter"),"awaiter-type");
        require(flag(r,awaiter,"get_IsCompleted"),"task-awaiter-status-disagrees");
        r.invoke(r.method(r.object_class(awaiter),"GetResult",0),awaiter);
        require(operation.task_status=="Succeeded","task-not-successful");
        require(operation.confirmed,"task-completed-without-owned-confirmation");
        require(operation.failure.empty(),"operation-already-failed");
        auto c=context(r);verify_context(r,c);
        operation.auto_resources=read_memory_resources(r,c.edit);
        require(memory_auto_resources_complete(operation.auto_resources),"completed-task-resource-shape");
        operation.phase="succeeded";
    }catch(const std::exception& error){fail(error.what());}
}
json confirmation(Runtime& r,const Context& c,void* sheet,bool require_ready=true){
    require(type(r,sheet,confirm_screen),"confirmation-type");
    // OpenAsync resolves the caller's Transform to ICampusScreen.SheetRoot
    // before instantiation. GetParent reads this physical root, not the panel
    // Transform. All existing rooted operation/panel/task checks remain active.
    auto parent=native_layer_parent(r,sheet);
    auto expected_parent=native_screen_sheet_root(r,c.owner);
    require(parent&&expected_parent&&parent==expected_parent,"confirmation-parent-not-owned-screen-root");
    auto model=r.getter(sheet,"get_Model");require(type(r,model,"ProduceMemorySelectAutoSetConfirmSheetModel"),"confirmation-model-type");
    auto group=r.getter(c.info,"get_ProduceGroup");require(text(r,model,"get_ProduceGroupId")==text(r,group,"get_Id"),"confirmation-group-changed");
    auto view=r.read_object_field(sheet,"_view");require(type(r,view,"ProduceMemorySelectAutoSetConfirmSheetView"),"confirmation-view-type");
    auto common=r.getter(view,"get_CommonView");require(type(r,common,"SheetCommonView","Campus.Common"),"confirmation-common-view-type");
    auto button=r.getter(common,"get_ExecuteButton");auto callback=button?r.read_object_field(button,"onClickedCallback"):nullptr;
    const bool ready=active(r,sheet)&&!flag(r,sheet,"get_IsClosing")&&!flag(r,sheet,"get_IsDisableInteraction")&&clickable(r,button);
    if(!ready&&!require_ready)return nullptr;
    require(ready,"confirmation-not-ready");
    json target=operation.binding;target["operation_serial"]=operation.serial;target["action_id"]="loadout.memory_auto_confirm";
    target["sheet_instance_id"]=pointer_identity(sheet);target["sheet_model_instance_id"]=pointer_identity(model);
    target["confirm_button_instance_id"]=pointer_identity(button);target["confirm_callback_instance_id"]=pointer_identity(callback);
    target["is_choose_rental"]=flag(r,model,"get_IsChooseRental");
    return target;
}
}

json read_memory_auto(Runtime& r){
    poll(r);
    Context c;bool current=false,owner_bound=false;
    try{
        auto owner=active_screen(r);
        if(type(r,owner,memory_screen)){c=context(r);current=true;reset_terminal(r,c);if(operation.started)verify_context(r,c);owner_bound=true;}
        else if(operation.started&&!operation.consumed)fail("memory-auto:owner-left-during-operation");
        if(operation.started&&!operation.consumed&&operation.failure.empty()&&current){
            auto layer=active_layer(r);
            if(type(r,layer,confirm_screen)&&!operation.confirmed&&screen_context_snapshot(r).at("tree_busy")==false){
                if(!confirmation(r,c,layer,false).is_null()){
                    if(!operation.roots[Operation::Sheet])keep(r,Operation::Sheet,layer);
                    require(object(r,Operation::Sheet)==layer,"confirmation-instance-changed");
                    operation.phase="awaiting_confirm";
                }
            }else operation.phase="running";
        }
    }catch(const std::exception& error){if(operation.started)fail(error.what());else throw;}
    json result=operation.started?operation.binding:(current?c.binding:json::object());
    result.update({{"schema","gkms.memory-auto-selection.v1"},{"operation_serial",operation.serial},
        {"phase",operation.phase},{"task_status",operation.task_status},{"confirmed",operation.confirmed},{"owner_bound",owner_bound},
        {"task_consumed",operation.consumed},{"memory_overrides_started",operation.override_started},
        {"failure",operation.failure.empty()?json(nullptr):json(operation.failure)},
        {"resources",operation.phase=="succeeded"?operation.auto_resources:(current?read_memory_resources(r,c.edit):json::array())},
        {"auto_resources",operation.auto_resources}});
    return result;
}

bool append_memory_auto_actions(Runtime& r,void* presenter,const std::string& screen,json& snapshot){
    const auto observed=read_memory_auto(r);snapshot["ui_state"]["memory_auto"]=observed;
    if(snapshot.contains("selection")&&snapshot["selection"].is_object())snapshot["selection"]["memory_auto"]=observed;
    if(screen!=memory_screen&&screen!=confirm_screen)return false;
    if(snapshot.at("busy")!=false)return true;
    auto c=context(r);
    if(operation.started&&operation.phase!="succeeded")snapshot["legal_actions"]=json::array();
    if(operation.phase=="failed"){snapshot["actions_complete"]=false;return true;}
    json target;
    if(screen==confirm_screen){
        if(operation.phase!="awaiting_confirm"||operation.confirmed)return true;
        verify_context(r,c);require(presenter==object(r,Operation::Sheet),"confirmation-owner-changed");
        target=confirmation(r,c,presenter);
    }else if(!operation.started){
        if(!clickable(r,c.button)||active_layer(r))return true;
        require(operation.serial<std::numeric_limits<std::uint64_t>::max(),"operation-serial-exhausted");
        target=c.binding;target["operation_serial"]=operation.serial+1;target["action_id"]="loadout.memory_auto";
    }else return true;
    target["screen_instance_id"]=snapshot.at("screen_instance_id");
    snapshot["legal_actions"].push_back({{"action_id",target["action_id"]},{"target",target}});
    snapshot["surface"]="navigation";snapshot["actions_complete"]=snapshot.at("blockers").empty();return true;
}

bool submit_memory_auto_action(Runtime& r,void* presenter,const json& target,const json& before){
    const auto action=target.value("action_id",std::string());
    if(action!="loadout.memory_auto"&&action!="loadout.memory_auto_confirm")return false;
    json fresh=before;fresh["legal_actions"]=json::array();
    append_memory_auto_actions(r,presenter,r.class_name(r.object_class(presenter)),fresh);
    bool found=false;for(const auto& row:fresh["legal_actions"])if(row["target"]==target){require(!found,"ambiguous-action");found=true;}
    require(found,"current-action-binding-changed");auto c=context(r);
    if(action=="loadout.memory_auto_confirm"){
        require(!operation.confirmed&&operation.phase=="awaiting_confirm","confirmation-already-attempted");
        auto sheet=object(r,Operation::Sheet);auto model=r.getter(sheet,"get_Model");
        require(flag(r,model,"get_IsChooseRental")==target.at("is_choose_rental").get<bool>(),"rental-checkbox-changed");
        auto common=r.getter(r.read_object_field(sheet,"_view"),"get_CommonView");auto button=r.getter(common,"get_ExecuteButton");
        operation.confirmed=true;operation.phase="running";
        try{r.invoke(r.method(r.object_class(button),"OnClickedHandler",0),button);}catch(const std::exception& e){fail(e.what());throw;}
        return true;
    }
    require(!operation.started,"operation-already-started");
    require(verified_pc_binding_identity().at("metadata_sha256")=="9349bc965fb08a434ab1c9547a3440a8ee02a05e761723792aabe5f4a9ecb635","current-PC-identity-required");
    auto method=r.method(r.object_class(c.panel),"AutoSetDeckResourceAsync",1);
    auto contract=r.method_parameter_contract(method);const auto& parameter=contract.at("parameters").at(0);
    require(contract.at("parameter_count")==1&&parameter.at("type_name")=="System.Threading.CancellationToken"&&parameter.at("type_kind")==17&&parameter.at("byref")==false,"auto-method-parameter-contract");
    // Resolve all owned-task operations before starting the game operation.
    // Runtime::invoke itself handles boxed value-type receivers; no guessed
    // UniTask/source/token offsets or direct native calling convention is used.
    auto task_type=task_class(r),awaiter_type=r.nested_class(task_type,"Awaiter");
    for(const auto& spec:{std::pair{task_type,"get_Status"},std::pair{task_type,"GetAwaiter"},
                         std::pair{awaiter_type,"get_IsCompleted"},std::pair{awaiter_type,"GetResult"}})
        require(r.method_parameter_contract(r.method(spec.first,spec.second,0)).at("parameter_count")==0,"task-method-parameter-contract");
    // Exact normal AutoButton lambda: panel.gameObject -> GameObjectExtensions.GetCt.
    // Retain its destroy-linked lifetime rather than substituting CancellationToken.None.
    auto ct_class=r.klass("mscorlib.dll","System.Threading","CancellationToken");
    auto extensions=r.klass("campus-submodule.Runtime.dll","Campus.Common.Extensions","GameObjectExtensions");
    auto get_ct=r.method(extensions,"GetCt",1);auto ct_contract=r.method_parameter_contract(get_ct);
    const auto& ct_parameter=ct_contract.at("parameters").at(0);
    require(ct_contract.at("parameter_count")==1&&ct_parameter.at("type_name")=="UnityEngine.GameObject"&&
        ct_parameter.at("type_kind")==18&&ct_parameter.at("byref")==false,"normal-cancellation-helper-contract");
    auto game_object=r.getter(c.panel,"get_gameObject");
    require(r.object_class(game_object)==r.klass("UnityEngine.CoreModule.dll","UnityEngine","GameObject"),"normal-cancellation-owner-type");
    auto ct=r.invoke(get_ct,nullptr,{game_object});require(r.object_class(ct)==ct_class,"cancellation-token-type");
    operation.serial=target.at("operation_serial").get<std::uint64_t>();operation.binding=c.binding;
    try{
        keep(r,Operation::Owner,c.owner);keep(r,Operation::Panel,c.panel);keep(r,Operation::Info,c.info);keep(r,Operation::Edit,c.edit);keep(r,Operation::Deck,c.deck);
        keep(r,Operation::Callback,c.callback);keep(r,Operation::Updated,c.updated);
    }catch(...){release(r);throw;}
    operation.started=true;operation.phase="running";
    try{
        auto task=r.invoke(method,c.panel,{r.unbox_pointer(ct)});require(task&&r.object_class(task)==task_class(r),"returned-task-type");
        keep(r,Operation::Task,task);
    }catch(const std::exception& error){fail(error.what());throw;}
    return true;
}

void validate_memory_auto_override(Runtime& r,const json& target,const json& loadout){
    const auto state=read_memory_auto(r);require(state.at("phase")=="succeeded"&&state.at("confirmed")==true&&state.at("owner_bound")==true&&!operation.override_started,"locks-require-owned-auto-success");
    require(target.at("memory_auto_operation_serial")==operation.serial,"lock-operation-serial-changed");
    for(const auto* key:{"account_scope","produce_id","idol_card_id"})require(target.at(key)==state.at(key)&&loadout.at(key)==state.at(key),"lock-scope-changed");
    require(loadout.at("active_section")=="memory"&&memory_auto_same_resources(loadout.at("memories"),operation.auto_resources),"auto-resources-changed-before-locks");
    require(!active_layer(r)&&screen_context_snapshot(r).at("busy")==false,"lock-owner-not-idle");
    verify_context(r,context(r));
}
void mark_memory_auto_override_started(Runtime&){require(!operation.override_started,"locks-already-attempted");operation.override_started=true;}
}
