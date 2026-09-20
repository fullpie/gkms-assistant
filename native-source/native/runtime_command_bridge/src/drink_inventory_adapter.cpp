#include "drink_inventory_adapter.hpp"
#include "pointer_identity.hpp"
#include "native_layer_context.hpp"
#include <map>
#include <set>

namespace gkms::bridge {
namespace {
using Slot=std::pair<bool,int>;
bool flag(Runtime& r,void* o,const char* name){return r.unbox<bool>(r.getter(o,name));}
int number(Runtime& r,void* o,const char* name){return r.unbox<int>(r.getter(o,name));}
std::string text(Runtime& r,void* o,const char* name){return r.string(r.getter(o,name));}
bool active(Runtime& r,void* o){return o&&flag(r,r.getter(o,"get_gameObject"),"get_activeInHierarchy");}
bool clickable(Runtime& r,void* b){return active(r,b)&&flag(r,b,"get_IsEnabled")&&!flag(r,b,"get_IsDisabled")&&r.read_object_field(b,"onClickedCallback");}
bool sheet_ready(Runtime& r,void* sheet){return !flag(r,sheet,"get_IsClosing")&&!flag(r,sheet,"get_IsDisableInteraction");}
void* common_button(Runtime& r,void* sheet,bool execute){
    return r.getter(r.getter(r.read_object_field(sheet,"_view"),"get_CommonView"),execute?"get_ExecuteButton":"get_CancelButton");
}
void* current_drink_sheet(Runtime& r,void* confirmation,json* diagnostic=nullptr){
    // OnCanExecuteAsync passes DrinkMax.transform, but ScreenLayerManager
    // normalizes it to ICampusScreen.SheetRoot before Instantiate. The two
    // layers are siblings; their native stack order records the open nesting.
    json binding;
    auto candidate=registered_layer_predecessor(r,confirmation,&binding);
    auto warning_root=native_layer_parent(r,confirmation);
    auto owner_root=candidate?native_layer_parent(r,candidate):nullptr;
    const bool bound=candidate&&drink_inventory_warning_owner_matches(r.class_name(r.object_class(candidate)),
        reinterpret_cast<std::uintptr_t>(warning_root),reinterpret_cast<std::uintptr_t>(owner_root));
    binding.update({{"warning_parent_transform_id",pointer_identity(warning_root)},
        {"owner_parent_transform_id",pointer_identity(owner_root)},{"bound",bound}});
    if(diagnostic)*diagnostic=std::move(binding);
    return bound?candidate:nullptr;
}
struct Inventory {json state;void* list;void* callback;std::map<Slot,void*> items;};
Inventory read_inventory(Runtime& r,void* sheet){
    auto model=r.getter(sheet,"get_Model");
    auto list=r.read_object_field(sheet,"_list");
    if(!model||!list)throw std::runtime_error("drink inventory selector is not initialized");
    const int limit=number(r,model,"get_DrinkLimitCount");
    if(limit<1||limit>64)throw std::runtime_error("native drink inventory capacity is outside supported bounds");
    std::map<Slot,std::string> identities;
    json owned=json::array(),added=json::array();
    for(bool is_new:{false,true}){
        const auto drinks=r.enumerate(r.getter(model,is_new?"get_AddDrinkList":"get_HasDrinkList"),128);
        for(int index=0;index<static_cast<int>(drinks.size());++index){
            const auto id=text(r,drinks[index],"get_Id");
            if(id.empty())throw std::runtime_error("drink inventory candidate has no ID");
            identities.emplace(Slot{is_new,index},id);
            auto value=json{{"index",index},{"drink_id",id},{"is_new",is_new},{"source",is_new?"new":"owned"}};
            (is_new?added:owned).push_back(value);
        }
    }
    std::set<Slot> selected;std::vector<Slot> selected_order;
    for(auto value:r.enumerate(r.getter(model,"get_SelectedDrinkIndexList"),128)){
        const Slot slot{r.field<bool>(value,"Item1"),r.field<int>(value,"Item2")};
        if(!identities.contains(slot)||!selected.insert(slot).second)throw std::runtime_error("native kept drink slot is invalid or repeated");
        selected_order.push_back(slot);
    }
    std::map<Slot,void*> items;json rows=json::array();
    const auto models=r.enumerate(r.getter(list,"get_ItemModels"),256);
    for(std::size_t list_index=0;list_index<models.size();++list_index){
        auto item=models[list_index];
        if(!item||r.class_name(r.object_class(item))!="DrinkMaxDrinkMixCellItemModel")continue;
        const Slot slot{flag(r,item,"get_IsGetDrink"),number(r,item,"get_DrinkIndex")};
        auto drink=r.getter(item,"get_Drink");
        if(!identities.contains(slot)||text(r,drink,"get_Id")!=identities.at(slot)||!items.emplace(slot,item).second)
            throw std::runtime_error("drink inventory mixed-list item differs from current owned/new pool");
        rows.push_back({{"list_index",list_index},{"index",slot.second},{"is_new",slot.first},
            {"source",slot.first?"new":"owned"},{"drink_id",identities.at(slot)},
            {"selected",selected.contains(slot)},{"ui_selected",flag(r,item,"get_IsSelected")},
            {"toggle_allowed",drink_inventory_toggle_allowed(selected.contains(slot),static_cast<int>(selected.size()),limit)}});
    }
    if(items.size()!=identities.size())throw std::runtime_error("drink selector mixed list is incomplete");
    json kept=json::array();bool keeps_new=false;
    for(const auto& slot:selected_order){
        kept.push_back({{"is_new",slot.first},{"index",slot.second},{"drink_id",identities.at(slot)}});
        keeps_new=keeps_new||slot.first;
    }
    json state={{"family","drink_inventory"},{"limit_count",limit},{"selected_count",selected.size()},
        {"owned_drinks",owned},{"new_drinks",added},{"items",rows},{"selected",kept},{"keeps_new_drink",keeps_new},
        {"selection_semantics","selected slots are kept; exactly limit_count slots required"},
        {"selection_fingerprint",sha256(json::array({owned,added,kept,limit}).dump())},
        {"data_ready",true},{"source","native ProduceDrinkMaxSheetModel and current mixed-list item models"}};
    return {state,list,r.read_object_field(list,"onDrinkClickedCallback"),items};
}
json target_base(void* parent,const Inventory& inventory){
    return {{"parent_instance_id",pointer_identity(parent)},
        {"selection_fingerprint",inventory.state.at("selection_fingerprint")},{"kept_drinks",inventory.state.at("selected")}};
}
json action(const char* id,json target){target["action_id"]=id;return {{"action_id",id},{"target",target}};}
void append_button(Runtime& r,void* parent,void* owner,void* button,const Inventory& inventory,const char* id,json& actions){
    if(!sheet_ready(r,owner)||!clickable(r,button))return;
    auto target=target_base(parent,inventory);
    target["sheet_instance_id"]=pointer_identity(owner);target["button_instance_id"]=pointer_identity(button);
    target["callback_instance_id"]=pointer_identity(r.read_object_field(button,"onClickedCallback"));
    actions.push_back(action(id,target));
}
}

bool append_drink_inventory_actions(Runtime& r,void* presenter,const std::string& screen,json& snapshot){
    const bool skip=screen=="ProducePresentSkipConfirmSheetPresenter";
    if(screen!="ProduceDrinkMaxSheetPresenter"&&!skip)return false;
    json parent_diagnostic;
    auto parent=skip?current_drink_sheet(r,presenter,&parent_diagnostic):presenter;
    if(skip)snapshot["ui_state"]["drink_inventory_parent_binding"]=parent_diagnostic;
    if(!parent)return false;
    auto inventory=read_inventory(r,parent);
    auto state=inventory.state;state["phase"]=skip?"confirm_no_new_drinks":"select_kept_drinks";
    if(skip)state["parent_binding"]=parent_diagnostic;
    snapshot["surface"]="drink_inventory";snapshot["ui_state"]=state;snapshot["legal_actions"]=json::array();
    snapshot["actions_complete"]=snapshot.at("blockers").empty();
    if(skip){
        if(state.at("keeps_new_drink")==false&&drink_inventory_confirm_allowed(state.at("selected_count").get<int>(),state.at("limit_count").get<int>()))
            append_button(r,parent,presenter,common_button(r,presenter,true),inventory,"drink_choice.confirm_skip_new",snapshot["legal_actions"]);
        append_button(r,parent,presenter,common_button(r,presenter,false),inventory,"drink_choice.cancel_skip",snapshot["legal_actions"]);
        return true;
    }
    if(sheet_ready(r,parent)&&active(r,inventory.list)&&inventory.callback){
        for(const auto& row:state.at("items")){
            if(row.at("toggle_allowed")!=true)continue;
            const Slot slot{row.at("is_new").get<bool>(),row.at("index").get<int>()};
            auto target=target_base(parent,inventory);
            for(const auto* key:{"list_index","index","is_new","source","drink_id","selected"})target[key]=row.at(key);
            target["item_instance_id"]=pointer_identity(inventory.items.at(slot));
            target["list_instance_id"]=pointer_identity(inventory.list);target["callback_instance_id"]=pointer_identity(inventory.callback);
            snapshot["legal_actions"].push_back(action("drink_choice.toggle",target));
        }
    }
    if(drink_inventory_confirm_allowed(state.at("selected_count").get<int>(),state.at("limit_count").get<int>()))
        append_button(r,parent,presenter,common_button(r,presenter,true),inventory,"drink_choice.confirm",snapshot["legal_actions"]);
    return true;
}

bool submit_drink_inventory_action(Runtime& r,void* presenter,const json& target,const json& before){
    const auto id=target.value("action_id",std::string());
    if(id.rfind("drink_choice.",0)!=0)return false;
    const bool skip=id=="drink_choice.confirm_skip_new"||id=="drink_choice.cancel_skip";
    if(before.at("screen_type")!=(skip?"ProducePresentSkipConfirmSheetPresenter":"ProduceDrinkMaxSheetPresenter"))
        throw std::runtime_error("drink inventory action owner changed");
    auto parent=skip?current_drink_sheet(r,presenter):presenter;
    if(!parent||pointer_identity(parent)!=target.at("parent_instance_id").get<std::string>())throw std::runtime_error("drink inventory parent changed");
    auto inventory=read_inventory(r,parent);
    if(inventory.state.at("selection_fingerprint")!=target.at("selection_fingerprint")||inventory.state.at("selected")!=target.at("kept_drinks"))
        throw std::runtime_error("drink inventory kept set or candidate pool changed");
    if(id=="drink_choice.toggle"){
        const Slot slot{target.at("is_new").get<bool>(),target.at("index").get<int>()};
        if(!inventory.items.contains(slot)||!sheet_ready(r,parent)||!active(r,inventory.list)||!inventory.callback||
            pointer_identity(inventory.list)!=target.at("list_instance_id").get<std::string>()||
            pointer_identity(inventory.callback)!=target.at("callback_instance_id").get<std::string>()||
            pointer_identity(inventory.items.at(slot))!=target.at("item_instance_id").get<std::string>())throw std::runtime_error("drink inventory toggle callback changed");
        r.invoke(r.method(r.object_class(inventory.callback),"Invoke",1),inventory.callback,{inventory.items.at(slot)});
        return true;
    }
    if(id!="drink_choice.cancel_skip"&&!drink_inventory_confirm_allowed(inventory.state.at("selected_count").get<int>(),inventory.state.at("limit_count").get<int>()))
        throw std::runtime_error("drink confirmation does not keep exactly the native capacity");
    if(id=="drink_choice.confirm_skip_new"&&inventory.state.at("keeps_new_drink")!=false)throw std::runtime_error("drink skip confirmation retained a new drink");
    if(id!="drink_choice.confirm"&&id!="drink_choice.confirm_skip_new"&&id!="drink_choice.cancel_skip")throw std::runtime_error("unknown drink inventory action");
    auto button=common_button(r,presenter,id!="drink_choice.cancel_skip");
    if(!sheet_ready(r,presenter)||!clickable(r,button)||pointer_identity(presenter)!=target.at("sheet_instance_id").get<std::string>()||
        pointer_identity(button)!=target.at("button_instance_id").get<std::string>()||
        pointer_identity(r.read_object_field(button,"onClickedCallback"))!=target.at("callback_instance_id").get<std::string>())
        throw std::runtime_error("drink inventory confirmation callback changed");
    r.invoke(r.method(r.object_class(button),"OnClickedHandler",0),button);return true;
}
}
