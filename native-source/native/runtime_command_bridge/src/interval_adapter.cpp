#include "interval_adapter.hpp"
#include "pointer_identity.hpp"
#include "screen_context.hpp"
#include <array>
#include <set>

namespace gkms::bridge {
namespace {
struct Binding {const char* group;const char* products;const char* buttons;bool direct;};
constexpr std::array<Binding,6> bindings={{{"buy_card","get_BuyCardProductList","get_BuyCardButtons",false},
    {"change_card","get_ChangeCardProductList","get_ChangeCardButtons",false},
    {"buy_drink","get_BuyDrinkProductList","get_BuyDrinkButtons",false},
    {"upgrade_card","get_CardUpgradeProduct","get_UpgradeCardButton",true},
    {"customize_card","get_CardCustomizeProduct","get_CustomizeCardButton",true},
    {"recover_stamina","get_RecoverStaminaProduct","get_RecoverStaminaButton",true}}};
bool flag(Runtime& r,void* o,const char* getter){return r.unbox<bool>(r.getter(o,getter));}
int number(Runtime& r,void* o,const char* getter){return r.unbox<int>(r.getter(o,getter));}
std::string text(Runtime& r,void* o,const char* getter){return r.string(r.getter(o,getter));}
bool active(Runtime& r,void* o){return o&&flag(r,r.getter(o,"get_gameObject"),"get_activeInHierarchy");}
bool clickable(Runtime& r,void* b){return active(r,b)&&flag(r,b,"get_IsEnabled")&&!flag(r,b,"get_IsDisabled")&&r.read_object_field(b,"onClickedCallback");}
void* reactive(Runtime& r,void* model,const char* getter){auto value=r.getter(model,getter);return value?r.getter(value,"get_Value"):nullptr;}
json project(Runtime& r,void* product){
    auto data=r.read_object_field(product,"_product");
    if(!data)throw std::runtime_error("interval product has no current transaction data");
    json value={{"resource_type",number(r,product,"get_ResourceType")},{"position_number",number(r,product,"get_PositionNumber")},
        {"resource_id",text(r,data,"get_ResourceId")},{"price",number(r,product,"get_Price")},
        {"purchased",flag(r,product,"get_Purchased")},{"can_buy",flag(r,product,"get_CanBuy")},
        {"product_instance_id",pointer_identity(product)},{"data",json::parse(text(r,data,"ToString"))},
        {"remaining_count",nullptr}};
    const auto type=r.class_name(r.object_class(product));
    if(type=="ScheduleIntervalCardEditProduct")value["remaining_count"]=number(r,product,"get_RemainCount");
    if(type=="ScheduleIntervalRecoverStaminaProduct")value["stamina_recover_value"]=number(r,product,"get_StaminaRecoverValue");
    return value;
}
std::string key(const json& value){return json::array({value.at("resource_type"),value.at("position_number")}).dump();}
json control_target(Runtime& r,void* presenter,void* button){
    return {{"owner_instance_id",pointer_identity(presenter)},{"button_instance_id",pointer_identity(button)},
        {"callback_instance_id",pointer_identity(r.read_object_field(button,"onClickedCallback"))}};
}
json action(const char* id,json target){target["action_id"]=id;return {{"action_id",id},{"target",target}};}
void verify_control(Runtime& r,void* presenter,void* button,const json& target){
    if(!clickable(r,button)||control_target(r,presenter,button)!=json{{"owner_instance_id",target.at("owner_instance_id")},
        {"button_instance_id",target.at("button_instance_id")},{"callback_instance_id",target.at("callback_instance_id")}})
        throw std::runtime_error("interval control identity or readiness changed");
}
void click(Runtime& r,void* button){r.invoke(r.method(r.object_class(button),"OnClickedHandler",0),button);}
struct ObservedProduct {json value;void* product;void* button;bool direct;};
std::vector<ObservedProduct> products(Runtime& r,void* model,void* view,bool& ready){
    std::vector<ObservedProduct> result;std::set<std::string> identities;ready=true;
    for(const auto& binding:bindings){
        auto current=reactive(r,model,binding.products);
        if(!current){if(!binding.direct)ready=false;continue;}
        const auto list=binding.direct?std::vector<void*>{current}:r.enumerate(current,256);
        const auto buttons=binding.direct?std::vector<void*>{r.getter(view,binding.buttons)}:r.enumerate(r.getter(view,binding.buttons),256);
        for(std::size_t index=0;index<list.size();++index){
            if(!list[index])throw std::runtime_error("interval product list has a null row");
            auto value=project(r,list[index]);value["group"]=binding.group;value["index"]=index;
            if(!identities.insert(key(value)).second)throw std::runtime_error("interval product identity repeats");
            result.push_back({value,list[index],index<buttons.size()?buttons[index]:nullptr,binding.direct});
        }
    }
    return result;
}
void* finish_button(Runtime& r,void* presenter,bool sheet){
    auto view=r.read_object_field(presenter,"_view");
    return sheet?r.getter(r.getter(view,"get_CommonView"),"get_ExecuteButton"):r.getter(view,"get_CloseButton");
}
void* confirmation_button(Runtime& r,void* presenter,bool accept){
    auto common=r.getter(r.read_object_field(presenter,"_view"),"get_CommonView");
    return r.getter(common,accept?"get_ExecuteButton":"get_CancelButton");
}
struct Interval {std::vector<ObservedProduct> products;json state;void* execute;bool execute_allowed;bool ready;};
Interval read_interval(Runtime& r,void* presenter,const char* phase){
    auto model=r.read_object_field(presenter,"_model");auto view=r.read_object_field(presenter,"_view");
    bool ready{};auto observed=products(r,model,view,ready);
    auto selected=reactive(r,model,"get_SelectedProduct");
    const auto selected_state=selected?project(r,selected):json(nullptr);
    const auto selected_key=selected?key(selected_state):std::string();
    const int points=r.unbox<int>(r.getter(model,"GetProducePoint"));
    auto execute_model=reactive(r,model,"get_ExecuteButtonModel");
    const bool execute_allowed=execute_model&&!flag(r,execute_model,"get_IsDisabled");
    auto execute_button=r.getter(view,"get_ExecuteButton");
    json state={{"data_ready",ready},{"phase",phase},{"produce_points",points},
        {"parent_instance_id",pointer_identity(presenter)},
        {"selected_product",selected_state},{"products",json::array()},
        {"execute_allowed",execute_allowed},{"execute_ready",execute_allowed&&clickable(r,execute_button)},
        {"finish_ready",clickable(r,finish_button(r,presenter,false))},
        {"source","native ScheduleIntervalScreenModel reactive products and normal view controls"}};
    for(auto& product:observed){
        auto& row=product.value;row["selected"]=key(row)==selected_key;row["direct_open"]=product.direct;
        row["enabled"]=clickable(r,product.button);row["eligible"]=interval_product_eligible(row,points);
        state["products"].push_back(row);
    }
    return {std::move(observed),std::move(state),execute_button,execute_allowed,ready};
}
}

bool append_interval_actions(Runtime& r,void* presenter,const std::string& screen,json& snapshot){
    if(screen=="ProduceShopEndWarningSheetPresenter"){
        auto parent=active_screen(r);
        if(!parent||r.class_name(r.object_class(parent))!="ScheduleIntervalScreenPresenter")return false;
        snapshot["surface"]="interval";snapshot["legal_actions"]=json::array();
        snapshot["ui_state"]={{"phase","confirm_finish"},{"parent_instance_id",pointer_identity(parent)}};
        snapshot["actions_complete"]=snapshot.at("blockers").empty();
        auto button=finish_button(r,presenter,true);
        if(!flag(r,presenter,"get_IsClosing")&&!flag(r,presenter,"get_IsDisableInteraction")&&clickable(r,button)){
            auto target=control_target(r,presenter,button);target["parent_instance_id"]=pointer_identity(parent);
            snapshot["legal_actions"].push_back(action("interval.confirm_finish",target));
        }
        return true;
    }
    const bool confirmation=screen=="ScheduleShopConfirmSheetPresenter";
    if(screen!="ScheduleIntervalScreenPresenter"&&!confirmation)return false;
    auto owner=confirmation?active_screen(r):presenter;
    if(!owner||r.class_name(r.object_class(owner))!="ScheduleIntervalScreenPresenter")return false;
    auto model=r.read_object_field(owner,"_model");
    snapshot["surface"]="interval";snapshot["legal_actions"]=json::array();
    if(!model){snapshot["actions_complete"]=false;snapshot["ui_state"]={{"data_ready",false}};return true;}
    const auto interval=read_interval(r,owner,confirmation?"confirm_purchase":"products");
    auto state=interval.state;
    snapshot["actions_complete"]=interval.ready&&snapshot.at("blockers").empty();
    if(confirmation){
        // Direct edit callbacks do not establish a pending quote in the old
        // SelectedProduct preview. Preserve it only as observed UI state.
        state["confirmation_binding"]="requires-host-operation-context";
        state["products_fingerprint"]=interval_confirmation_fingerprint(state);
        const json base={{"parent_instance_id",pointer_identity(owner)},
            {"products_fingerprint",state.at("products_fingerprint")}};
        if(interval.ready&&!flag(r,presenter,"get_IsClosing")&&!flag(r,presenter,"get_IsDisableInteraction")){
            for(const bool accept:{true,false}){
                auto button=confirmation_button(r,presenter,accept);if(!clickable(r,button))continue;
                auto target=base;target.update(control_target(r,presenter,button));
                snapshot["legal_actions"].push_back(action(accept?"interval.confirm_operation":"interval.cancel_operation",target));
            }
        }
        snapshot["ui_state"]=std::move(state);return true;
    }
    for(const auto& observed_product:interval.products){
        const auto& row=observed_product.value;const bool is_selected=row.at("selected");
        if(!interval.ready||row.at("eligible")!=true)continue;
        const char* id=nullptr;void* button{};
        if(observed_product.direct){id="interval.open_product";button=observed_product.button;}
        else if(!is_selected){id="interval.select";button=observed_product.button;}
        else if(interval.execute_allowed){id="interval.execute";button=interval.execute;}
        if(!id||!clickable(r,button))continue;
        auto target=control_target(r,presenter,button);
        for(const auto* field:{"group","index","resource_type","position_number","resource_id","price","product_instance_id"})target[field]=row.at(field);
        snapshot["legal_actions"].push_back(action(id,target));
    }
    auto close=finish_button(r,presenter,false);
    if(clickable(r,close))snapshot["legal_actions"].push_back(action("interval.finish",control_target(r,presenter,close)));
    snapshot["ui_state"]=std::move(state);
    return true;
}

bool submit_interval_action(Runtime& r,void* presenter,const json& target,const json& before){
    const auto id=target.value("action_id",std::string());
    if(id.rfind("interval.",0)!=0)return false;
    if(id=="interval.confirm_operation"||id=="interval.cancel_operation"){
        auto owner=active_screen(r);
        if(before.at("screen_type")!="ScheduleShopConfirmSheetPresenter"||!owner||
            r.class_name(r.object_class(owner))!="ScheduleIntervalScreenPresenter")
            throw std::runtime_error("interval purchase confirmation parent changed");
        const auto current=read_interval(r,owner,"confirm_purchase");
        if(!current.ready||!interval_confirmation_matches(target,current.state,pointer_identity(owner))||
            flag(r,presenter,"get_IsClosing")||flag(r,presenter,"get_IsDisableInteraction"))
            throw std::runtime_error("interval confirmation product/price pool or readiness changed");
        auto button=confirmation_button(r,presenter,id=="interval.confirm_operation");
        verify_control(r,presenter,button,target);click(r,button);return true;
    }
    const bool sheet=id=="interval.confirm_finish";
    if(before.at("screen_type")!=(sheet?"ProduceShopEndWarningSheetPresenter":"ScheduleIntervalScreenPresenter"))throw std::runtime_error("interval action owner changed");
    if(id=="interval.finish"||sheet){
        if(sheet){
            auto parent=active_screen(r);
            if(!parent||r.class_name(r.object_class(parent))!="ScheduleIntervalScreenPresenter"||
                pointer_identity(parent)!=target.at("parent_instance_id").get<std::string>()||flag(r,presenter,"get_IsClosing")||flag(r,presenter,"get_IsDisableInteraction"))
                throw std::runtime_error("interval finish confirmation parent changed");
        }
        auto button=finish_button(r,presenter,sheet);verify_control(r,presenter,button,target);click(r,button);return true;
    }
    auto model=r.read_object_field(presenter,"_model");auto view=r.read_object_field(presenter,"_view");bool ready{};
    const auto observed=products(r,model,view,ready);
    for(const auto& product:observed){
        if(!interval_target_matches_product(target,product.value))continue;
        if(!ready||!interval_product_eligible(product.value,r.unbox<int>(r.getter(model,"GetProducePoint"))))throw std::runtime_error("interval product is no longer available or affordable");
        void* button{};
        if(id=="interval.open_product"&&product.direct)button=product.button;
        else if(id=="interval.select"&&!product.direct)button=product.button;
        else if(id=="interval.execute"&&!product.direct){
            auto selected=reactive(r,model,"get_SelectedProduct");auto execute_model=reactive(r,model,"get_ExecuteButtonModel");
            if(!selected||key(project(r,selected))!=key(product.value)||!execute_model||flag(r,execute_model,"get_IsDisabled"))
                throw std::runtime_error("interval execute no longer binds the selected product");
            button=r.getter(view,"get_ExecuteButton");
        }else throw std::runtime_error("interval product action differs from normal callback kind");
        verify_control(r,presenter,button,target);click(r,button);return true;
    }
    throw std::runtime_error("interval product target is absent from current model");
}
}
