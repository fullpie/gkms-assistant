#include "shop_adapter.hpp"
#include "screen_context.hpp"
#include "pointer_identity.hpp"
#include <set>

namespace gkms::bridge {
namespace {
bool flag(Runtime& r,void* o,const char* name){return r.unbox<bool>(r.getter(o,name));}
int number(Runtime& r,void* o,const char* name){return r.unbox<int>(r.getter(o,name));}
std::string text(Runtime& r,void* o,const char* name){return r.string(r.getter(o,name));}
bool active(Runtime& r,void* o){return o&&flag(r,r.getter(o,"get_gameObject"),"get_activeInHierarchy");}
bool clickable(Runtime& r,void* b){return active(r,b)&&flag(r,b,"get_IsEnabled")&&!flag(r,b,"get_IsDisabled")&&r.read_object_field(b,"onClickedCallback");}
void* current_progress(Runtime& r){auto k=r.klass("Assembly-CSharp","Campus.Common.User","UserDataManager");return r.invoke(r.method(k,"get_UserProduceProgress",0),nullptr);}
json shop_start_lifecycle(Runtime& r,void* progress){
    const bool started=r.unbox<bool>(r.invoke(r.method(r.object_class(progress),"get_InProgressStep",0,0x06019FFD),progress));
    const int step=number(r,progress,"get_StepType"),status=number(r,progress,"get_Status");
    const bool ready=shop_step_started(started,step,status);
    return {{"input_ready",ready},{"in_progress_step",started},{"step_type",step},{"progress_status",status},
        {"produce_id",text(r,progress,"get_ProduceId")},{"week",number(r,progress,"get_StepNumber")},
        {"phase",ready?"shop_step_started":status==4?"awaiting_shop_exit":"awaiting_shop_start"},
        {"source","native UserProduceProgress.InProgressStep; Shop NonBlockOpenInAsync StepShopStart branch"}};
}
json card(Runtime& r,void* data){
    if(!data)return nullptr;
    return {{"card_id",text(r,data,"get_Id")},{"upgrade",number(r,data,"get_UpgradeCount")},{"rarity",number(r,data,"get_Rarity")}};
}
json legend_state(Runtime& r,void* progress){
    const int limit=r.unbox<int>(r.invoke(r.method(r.object_class(progress),"GetMaxLegendProduceCardCount",0,0x0601A0D1),progress));
    auto deck=r.invoke(r.method(r.object_class(progress),"CreateDeckProduceCardMasters",0,0x0601A0A9),progress);
    int total=0,legend=0;json cards=json::array();
    for(auto value:r.enumerate(deck,4096)){
        ++total;auto row=card(r,value);if(row.at("rarity")==100){++legend;cards.push_back(row);}
    }
    return {{"limit",limit},{"owned_count",legend},{"remaining_count",limit-legend},{"deck_count",total},{"owned_cards",cards},
        {"source","native GetMaxLegendProduceCardCount and CreateDeckProduceCardMasters"}};
}
json action(const char* name,json target){target["action_id"]=name;return {{"action_id",name},{"target",target}};}
json control(Runtime& r,void* owner,void* button){
    return {{"owner_instance_id",pointer_identity(owner)},{"button_instance_id",pointer_identity(button)},
        {"callback_instance_id",pointer_identity(r.read_object_field(button,"onClickedCallback"))}};
}
void verify_control(Runtime& r,void* owner,void* button,const json& target){
    if(!clickable(r,button))throw std::runtime_error("shop control is no longer actionable");
    const auto current=control(r,owner,button);
    for(const auto* key:{"owner_instance_id","button_instance_id","callback_instance_id"})
        if(current.at(key)!=target.at(key))throw std::runtime_error("shop control identity changed");
}
void click(Runtime& r,void* button){r.invoke(r.method(r.object_class(button),"OnClickedHandler",0),button);}
void* sheet_button(Runtime& r,void* sheet,bool execute){return r.getter(r.getter(r.read_object_field(sheet,"_view"),"get_CommonView"),execute?"get_ExecuteButton":"get_CancelButton");}
void append_sheet_control(Runtime& r,void* sheet,bool execute,const char* name,json target,json& actions){
    if(flag(r,sheet,"get_IsClosing")||flag(r,sheet,"get_IsDisableInteraction"))return;
    auto button=sheet_button(r,sheet,execute);if(!clickable(r,button))return;
    target.update(control(r,sheet,button));actions.push_back(action(name,target));
}
struct Product {json value;void* object;void* button;};
struct Shop {void* model;void* view;int selected;int points;json legend;bool input_ready;bool data_ready;json start_lifecycle;std::vector<Product> products;};
Shop read_shop(Runtime& r,void* presenter){
    auto model=r.read_object_field(presenter,"_model");auto view=r.read_object_field(presenter,"_view");
    auto progress=current_progress(r);
    if(!model||!progress)throw std::runtime_error("shop model/progress is not initialized");
    auto value_list=r.getter(model,"get_ProductList");auto button_list=r.getter(view,"get_ProductButtonList");
    auto selected_value=r.getter(model,"get_SelectIndex");
    const int selected=selected_value?number(r,selected_value,"get_Value"):-1;
    Shop shop{model,view,selected,number(r,progress,"get_ProducePoint"),legend_state(r,progress),
        !flag(r,model,"get_InProgress")&&!flag(r,model,"get_IsComplete"),value_list&&button_list&&selected_value,shop_start_lifecycle(r,progress),{}};
    if(!shop.data_ready)return shop;
    const auto values=r.enumerate(value_list,256);const auto buttons=r.enumerate(button_list,256);
    const bool drink_full=flag(r,progress,"get_IsDrinkMax");
    std::set<int> positions;
    for(std::size_t index=0;index<values.size();++index){
        auto product=values[index];if(!product)throw std::runtime_error("shop product array contains a null row");
        auto widget=index<buttons.size()?buttons[index]:nullptr;
        auto button=widget?r.getter(widget,"get_Button"):nullptr;
        const int position=number(r,product,"get_PositionNumber");
        if(!positions.insert(position).second)throw std::runtime_error("shop repeats a product position");
        const auto card_data=card(r,r.getter(product,"get_Card"));
        json row={{"index",index},{"position_number",position},{"resource_type",number(r,product,"get_ResourceType")},
            {"resource_id",text(r,product,"get_ResourceId")},{"price",r.unbox<int>(r.invoke(r.method(r.object_class(product),"GetCurrentPrice",0,0x0601AE4A),product))},
            {"base_price",number(r,product,"get_Price")},{"upgrade",number(r,product,"get_UpgradeCount")},
            {"purchased",flag(r,product,"get_Purchased")||(widget&&r.field<bool>(widget,"_isSoldout"))},
            {"locked",flag(r,product,"get_Lock")||!widget||r.field<bool>(widget,"_isLock")},
            {"can_buy",widget&&r.field<bool>(widget,"_canBuy")},{"card",card_data},
            {"is_legend",card_data.is_object()&&card_data.at("rarity")==100},
            {"product_instance_id",pointer_identity(product)},{"selected",static_cast<int>(index)==selected},
            {"control_ready",clickable(r,button)},{"data",json::parse(text(r,product,"ToString"))}};
        row["direct_open"]=shop_direct_card_operation(row.at("resource_type").get<int>());
        row["drink_capacity_blocked"]=row.at("resource_type")==3&&drink_full;
        row["eligible"]=shop_product_available(row,shop.points,shop.legend.at("remaining_count").get<int>(),shop.legend.at("deck_count").get<int>());
        shop.products.push_back({row,product,button});
    }
    return shop;
}
json product_target(const json& row){
    json result=json::object();
    for(const auto* key:{"index","position_number","resource_type","resource_id","price","upgrade","product_instance_id"})result[key]=row.at(key);
    return result;
}
json shop_ui(const Shop& shop,const char* phase){
    json rows=json::array();for(const auto& product:shop.products)rows.push_back(product.value);
    return {{"phase",phase},{"data_ready",shop.data_ready},{"products",rows},{"selected_index",shop.data_ready?json(shop.selected):json(nullptr)},
        {"produce_points",shop.points},{"input_ready",shop.input_ready},{"legend",shop.legend},
        {"source","native Shop model ProductList, current GetCurrentPrice and actual product-button availability"}};
}
}

bool append_shop_actions(Runtime& r,void* presenter,const std::string& screen,json& snapshot){
    if(screen=="ProduceCardLegendGetConfirmSheetPresenter"){
        auto model=r.getter(presenter,"get_Model");auto data=r.getter(model,"get_CardData");
        const auto candidate=card(r,data);const int remaining=number(r,model,"get_RemainLimitCount");
        const auto current=legend_state(r,current_progress(r));
        snapshot["surface"]="legend_card_confirmation";snapshot["legal_actions"]=json::array();
        snapshot["ui_state"]={{"family","legend_card_confirmation"},{"card",candidate},{"remaining_count",remaining},{"legend",current},
            {"character_id",text(r,model,"get_CharacterId")},{"skip_next_unchanged",true}};
        snapshot["actions_complete"]=snapshot.at("blockers").empty();
        const json target={{"card",candidate},{"remaining_count",remaining},{"legend_limit",current.at("limit")},{"legend_owned_count",current.at("owned_count")}};
        if(candidate.is_object()&&candidate.at("rarity")==100&&remaining>0&&remaining==current.at("remaining_count").get<int>())
            append_sheet_control(r,presenter,true,"legend_card.confirm_acquire",target,snapshot["legal_actions"]);
        append_sheet_control(r,presenter,false,"legend_card.cancel_acquire",target,snapshot["legal_actions"]);
        return true;
    }
    const bool confirmation=screen=="ScheduleShopConfirmSheetPresenter";
    const bool finish=screen=="ProduceShopEndWarningSheetPresenter";
    if(screen!="ScheduleShopScreenPresenter"&&!confirmation&&!finish)return false;
    auto owner=(confirmation||finish)?active_screen(r):presenter;
    if(!owner||r.class_name(r.object_class(owner))!="ScheduleShopScreenPresenter")return false;
    if(!r.read_object_field(owner,"_model")){
        snapshot["surface"]="shop";snapshot["ui_state"]={{"data_ready",false}};
        snapshot["legal_actions"]=json::array();snapshot["actions_complete"]=false;return true;
    }
    const auto shop=read_shop(r,owner);
    snapshot["surface"]="shop";snapshot["ui_state"]=shop_ui(shop,finish?"confirm_finish":confirmation?"confirm_purchase":"products");
    snapshot["legal_actions"]=json::array();snapshot["actions_complete"]=shop.data_ready&&snapshot.at("blockers").empty();
    if(!shop.data_ready)return true;
    if(finish){
        append_sheet_control(r,presenter,true,"shop.confirm_finish",{{"parent_instance_id",pointer_identity(owner)}},snapshot["legal_actions"]);return true;
    }
    if(confirmation){
        // This game's confirmation model does not retain its price/product.
        // A direct upgrade/delete does not update SelectIndex, so even a valid
        // old ordinary selection must not be presented as this modal's quote.
        // The host must retain its chosen operation and card-selector context.
        const auto fingerprint=sha256(snapshot.at("ui_state").dump());
        snapshot["ui_state"]["confirmation_binding"]="requires-host-operation-context";
        snapshot["ui_state"]["products_fingerprint"]=fingerprint;
        const json target={{"parent_instance_id",pointer_identity(owner)},{"products_fingerprint",fingerprint}};
        append_sheet_control(r,presenter,true,"shop.confirm_operation",target,snapshot["legal_actions"]);
        append_sheet_control(r,presenter,false,"shop.cancel_operation",target,snapshot["legal_actions"]);
        return true;
    }
    apply_shop_main_start_gate(screen,snapshot["ui_state"],snapshot["legal_actions"],shop.start_lifecycle);
    if(!shop_main_start_allowed(screen,shop.start_lifecycle))return true;
    if(!shop.input_ready)return true;
    for(const auto& product:shop.products){
        if(product.value.at("eligible")!=true)continue;
        const bool selected=product.value.at("selected");const bool direct=product.value.at("direct_open");
        auto button=selected&&!direct?r.getter(shop.view,"get_BuyButton"):product.button;
        if(!clickable(r,button))continue;
        auto target=product_target(product.value);target.update(control(r,owner,button));
        snapshot["legal_actions"].push_back(action(direct?"shop.open_product":selected?"shop.buy":"shop.select",target));
    }
    auto close=r.getter(shop.view,"get_CloseButton");
    if(clickable(r,close))snapshot["legal_actions"].push_back(action("shop.finish",control(r,owner,close)));
    return true;
}

bool submit_shop_action(Runtime& r,void* presenter,const json& target,const json& before){
    const auto id=target.value("action_id",std::string());
    if(id.rfind("shop.",0)!=0&&id.rfind("legend_card.",0)!=0)return false;
    const auto screen=before.at("screen_type").get<std::string>();
    if(id.rfind("legend_card.",0)==0){
        if(screen!="ProduceCardLegendGetConfirmSheetPresenter")throw std::runtime_error("Legend acquisition sheet changed");
        auto model=r.getter(presenter,"get_Model");const auto current=legend_state(r,current_progress(r));
        if(card(r,r.getter(model,"get_CardData"))!=target.at("card")||number(r,model,"get_RemainLimitCount")!=target.at("remaining_count").get<int>()||
            current.at("limit")!=target.at("legend_limit")||current.at("owned_count")!=target.at("legend_owned_count"))throw std::runtime_error("Legend acquisition card or actual capacity changed");
        const bool accept=id=="legend_card.confirm_acquire";
        if(!accept&&id!="legend_card.cancel_acquire")throw std::runtime_error("unknown Legend confirmation action");
        if(accept&&current.at("remaining_count").get<int>()<=0)throw std::runtime_error("Legend card capacity exhausted");
        auto button=sheet_button(r,presenter,accept);verify_control(r,presenter,button,target);click(r,button);return true;
    }
    const bool sheet=screen=="ProduceShopEndWarningSheetPresenter"||screen=="ScheduleShopConfirmSheetPresenter";
    auto owner=sheet?active_screen(r):presenter;
    if(!owner||r.class_name(r.object_class(owner))!="ScheduleShopScreenPresenter")throw std::runtime_error("shop operation parent changed");
    const auto shop=read_shop(r,owner);void* button{};
    if(!shop_main_start_allowed(screen,shop.start_lifecycle))
        throw std::runtime_error("native Shop start is not complete or the Shop step has already ended");
    if(sheet&&pointer_identity(owner)!=target.at("parent_instance_id").get<std::string>())throw std::runtime_error("shop sheet parent instance changed");
    if(id=="shop.confirm_operation"||id=="shop.cancel_operation"){
        if(screen!="ScheduleShopConfirmSheetPresenter"||sha256(shop_ui(shop,"confirm_purchase").dump())!=target.at("products_fingerprint").get<std::string>())
            throw std::runtime_error("shop pending-operation inventory/price context changed");
        button=sheet_button(r,presenter,id=="shop.confirm_operation");
    }else if(id=="shop.finish"||id=="shop.confirm_finish"){
        if((id=="shop.confirm_finish")!=(screen=="ProduceShopEndWarningSheetPresenter"))throw std::runtime_error("shop finish callback kind changed");
        button=sheet?sheet_button(r,presenter,true):r.getter(shop.view,"get_CloseButton");
    }else{
        const Product* chosen{};
        for(const auto& product:shop.products)if(product_target(product.value)==json{{"index",target.at("index")},{"position_number",target.at("position_number")},
            {"resource_type",target.at("resource_type")},{"resource_id",target.at("resource_id")},{"price",target.at("price")},
            {"upgrade",target.at("upgrade")},{"product_instance_id",target.at("product_instance_id")}})chosen=&product;
        if(!chosen||chosen->value.at("eligible")!=true)throw std::runtime_error("shop product identity, affordability or limit changed");
        const bool direct=chosen->value.at("direct_open");const bool selected=chosen->value.at("selected");
        if(id=="shop.select"&&!direct&&!selected&&!sheet)button=chosen->button;
        else if(id=="shop.open_product"&&direct&&!sheet)button=chosen->button;
        else if(id=="shop.buy"&&!direct&&selected&&!sheet)button=r.getter(shop.view,"get_BuyButton");
        else throw std::runtime_error("shop operation no longer binds the selected product callback");
    }
    if(!sheet&&!shop.input_ready)throw std::runtime_error("shop already has an active operation");
    if(sheet&&(flag(r,presenter,"get_IsClosing")||flag(r,presenter,"get_IsDisableInteraction")))throw std::runtime_error("shop sheet is no longer ready");
    verify_control(r,presenter,button,target);click(r,button);return true;
}
}
